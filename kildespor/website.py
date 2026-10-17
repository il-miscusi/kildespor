"""Find, verify and read the company's own website.

Candidates come from the register (the entity's and its subunits' listed
websites) and from deterministic domain guesses built from the legal name.
A guessed domain is published only when the site shows this organisation
number (checksum-validated). A register-listed site is the company's own
declaration, so it is published unless it is parked or plainly belongs to a
different, unrelated entity; it still only feeds site facts into the profile
when the identity holds.

Facts read from a verified site: public brand name, self-description,
phone numbers and e-mail addresses, company-owned social profiles,
the careers page, JSON-LD job postings and dated news from the site's own
RSS/Atom feed.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .http import Fetcher, RequestBudgetExhausted, RobotsDisallowed, Snapshot, UnsafeURL, resolve_public, utc_now


def utc_today() -> str:
    return utc_now()[:10]
from .model import CompanyRecord

LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "sa", "ba", "nuf", "enk", "ks", "hf", "iks", "sf", "bbl", "fli", "stiftelse", "stiftelsen", "aksjeselskap",
}
SKIP_GUESS_WORDS = {"holding", "holdings", "invest", "investment", "investering", "kapital", "capital", "eiendom", "eiendommer", "property", "properties"}
DIRECTORY_HOSTS = (
    "proff.no", "purehelp.no", "gulesider.no", "1881.no", "finn.no", "brreg.no", "regnskapstall.no", "firmasok.no", "bedriftsdatabasen.no",
    "facebook.com", "linkedin.com", "instagram.com", "google.com", "wix.com", "wixsite.com", "squarespace.com", "enirokart.no", "kompass.com",
    "allabolag.se", "dun.com", "dnb.com", "forvalt.no", "norske-bedrifter.no", "bedrift.no", "180.no", "opencorporates.com",
)
PARKED_MARKERS = (
    "domain is for sale", "domenet er til salgs", "this domain is parked", "buy this domain", "kjøp dette domenet", "domain parking",
    "parkert hos", "domeneshop", "registrert hos one.com", "this domain has been registered", "web hosting by", "default web site page",
    "website is under construction", "siden er under konstruksjon", "denne siden er parkert", "hostinger", "parked free",
)
CONTACT_HINTS = re.compile(r"kontakt|contact|om-oss|om_oss|omoss|about|personvern|privacy|vilk[aå]r|terms|impressum|informasjonskapsler|cookie|firma|selskap", re.I)
CAREER_HINTS = re.compile(r"karriere|ledige[- ]stillinger|stillinger|jobb|career|jobs|work-with-us|bli-med", re.I)
SOCIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("facebook", re.compile(r"^https?://(?:www\.|m\.|web\.)?facebook\.com/(?!sharer|share|dialog|plugins|tr\b|login|home\.php|groups/?$|events/?$|watch/?$|policy|privacy)([A-Za-z0-9.\-_/%]+?)/?(?:\?.*)?$", re.I)),
    ("instagram", re.compile(r"^https?://(?:www\.)?instagram\.com/(?!p/|reel/|explore/|accounts/|stories/)([A-Za-z0-9._]+)/?(?:\?.*)?$", re.I)),
    ("linkedin", re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|school|showcase)/([A-Za-z0-9\-_%.]+)/?(?:\?.*)?$", re.I)),
    ("youtube", re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/((?:channel|c|user)/[A-Za-z0-9\-_]+|@[A-Za-z0-9\-_.]+)/?(?:\?.*)?$", re.I)),
    ("x", re.compile(r"^https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(?!intent|share|home|search|hashtag|i/)([A-Za-z0-9_]{1,15})/?(?:\?.*)?$", re.I)),
    ("tiktok", re.compile(r"^https?://(?:www\.)?tiktok\.com/(@[A-Za-z0-9._]+)/?(?:\?.*)?$", re.I)),
    ("pinterest", re.compile(r"^https?://(?:[a-z]{2}\.)?pinterest\.(?:com|no)/([A-Za-z0-9_]+)/?(?:\?.*)?$", re.I)),
    ("vimeo", re.compile(r"^https?://(?:www\.)?vimeo\.com/([A-Za-z][A-Za-z0-9_]+)/?(?:\?.*)?$", re.I)),
]
FREE_MAIL = {
    "gmail.com", "hotmail.com", "hotmail.no", "outlook.com", "live.com", "live.no", "yahoo.com", "yahoo.no", "icloud.com", "me.com",
    "online.no", "frisurf.no", "start.no", "broadpark.no", "getmail.no", "c2i.net", "chello.no", "altibox.no", "lyse.net", "norpost.net",
    "msn.com", "aol.com", "protonmail.com", "proton.me", "mail.com", "gmx.com", "gmx.net", "email.com", "telia.no", "hotmail.co.uk", "ymail.com",
    "tele2.no", "kpnmail.no", "bluezone.no", "loqal.no", "netcom.no", "tdcadsl.no", "sensewave.com", "eunet.no", "nextgentel.com", "post.com",
}
WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)
ORG_CANDIDATE = re.compile(r"(?<![\d])(\d{3})[  .\-]?(\d{3})[  .\-]?(\d{3})(?![\d])")


GENERIC_NAME_WORDS = {
    "norge", "norway", "norsk", "nordic", "holding", "invest", "eiendom", "gruppen", "group", "service", "services", "consult",
    "consulting", "bygg", "drift", "utvikling", "handel", "transport", "regnskap", "solutions", "systems", "media", "design",
    "partner", "partners", "energi", "teknikk", "elektro", "maskin", "hotell", "restaurant", "senter", "borettslag", "sameie",
    "stiftelsen", "stiftelse", "forening", "selskap", "company", "management", "capital", "the", "and", "og", "for", "til",
}


def _fold(text: str) -> str:
    return strip_accents(text.lower().replace("æ", "ae").replace("ø", "o").replace("å", "a"))


def name_corroboration(legal_name: str, subunits: list[dict[str, Any]], page_text: str, final_url: str) -> str | None:
    """A short span proving the company name appears on the page or forms the domain, else None."""
    folded_page = _fold(page_text)
    host = registered_domain(urlsplit(final_url).hostname or "").split(".")[0].replace("-", "")
    names = [legal_name] + [s.get("name") or "" for s in subunits[:5]]
    for name in names:
        tokens = [t for t in (_fold(t) for t in name_tokens(name)) if t]
        if not tokens:
            continue
        joined = "".join(tokens)
        if len(joined) >= 4 and joined == host:
            return f"domain {host} spells the name {name}"
        phrase = " ".join(tokens)
        if len(phrase) >= 5 and phrase in folded_page:
            at = folded_page.find(phrase)
            return f"name on page: …{page_text[max(0, at - 40): at + len(phrase) + 40]}…"
        distinctive = [t for t in tokens if len(t) >= 5 and t not in GENERIC_NAME_WORDS]
        if distinctive and all(re.search(rf"\b{re.escape(t)}\b", folded_page) for t in distinctive) and len(distinctive) >= max(1, len(tokens) - 1):
            at = folded_page.find(distinctive[0])
            return f"name words {', '.join(distinctive)} on page: …{page_text[max(0, at - 40): at + 60]}…"
    return None


def safe_link(url: Any, site_host: str | None = None) -> str | None:
    """An http(s) URL on a public-looking host (and on the company's domain when given), else None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".local") or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        return None
    if site_host and registered_domain(host) != site_host:
        return None
    return url


def valid_orgnr(number: str) -> bool:
    if len(number) != 9 or not number.isdigit() or number[0] not in "89":
        return False
    total = sum(int(d) * w for d, w in zip(number[:8], WEIGHTS))
    check = 11 - total % 11
    if check == 11:
        check = 0
    return check != 10 and check == int(number[8])


def org_numbers_in(text: str) -> dict[str, str]:
    """Valid organisation numbers found in text, mapped to a short context span."""
    found: dict[str, str] = {}
    for match in ORG_CANDIDATE.finditer(text):
        number = "".join(match.groups())
        if number in found or not valid_orgnr(number):
            continue
        start, end = max(0, match.start() - 60), min(len(text), match.end() + 30)
        found[number] = re.sub(r"\s+", " ", text[start:end]).strip()
    return found


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def name_tokens(legal_name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9æøåäöüé]+", legal_name.lower().replace("&", " og "))
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] in ("stiftelsen",):
        tokens.pop(0)
    return tokens


def _ascii_variants(token: str) -> list[str]:
    a = token.replace("æ", "ae").replace("ø", "o").replace("å", "a").replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("é", "e")
    b = token.replace("æ", "ae").replace("ø", "oe").replace("å", "aa").replace("ä", "ae").replace("ö", "oe").replace("ü", "u").replace("é", "e")
    return list(dict.fromkeys([a, b]))


GEO_SUFFIXES = {"norge", "norway", "nordic", "norden", "scandinavia", "skandinavia"}


def guess_domains(legal_name: str, limit: int = 6) -> list[str]:
    """Candidate domains derived from a legal name, most likely first."""
    tokens = name_tokens(legal_name)
    if not tokens or any(token in SKIP_GUESS_WORDS for token in tokens):
        return []
    if len(tokens) == 1 and len(tokens[0]) < 3:
        return []
    if len(tokens) > 4:
        return []
    token_sets = [tokens]
    without_og = [t for t in tokens if t != "og"]
    if without_og != tokens and len(without_og) >= 2:
        token_sets.append(without_og)
    if len(tokens) >= 2 and tokens[-1] in GEO_SUFFIXES and len("".join(tokens[:-1])) >= 4:
        token_sets.append(tokens[:-1])

    def ascii_words(token_set: list[str], transform: int) -> list[str]:
        return [(_ascii_variants(t) + _ascii_variants(t))[transform] for t in token_set]

    out: list[str] = []

    def add(domain: str) -> None:
        label = domain.rsplit(".", 1)[0]
        if 3 <= len(label) <= 40 and domain not in out:
            out.append(domain)

    primary = ascii_words(tokens, 0)
    add("".join(primary) + ".no")
    if len(primary) > 1:
        add("-".join(primary) + ".no")
    unicode_joined = "".join(tokens)
    if re.search(r"[æøå]", unicode_joined):
        try:
            add(unicode_joined.encode("idna").decode("ascii") + ".no")
        except UnicodeError:
            pass
    for token_set in token_sets[1:]:
        words = ascii_words(token_set, 0)
        add("".join(words) + ".no")
    alt = ascii_words(tokens, 1)
    if alt != primary:
        add("".join(alt) + ".no")
    add("".join(primary) + ".com")
    for token_set in token_sets[1:]:
        words = ascii_words(token_set, 0)
        if len(words) > 1:
            add("-".join(words) + ".no")
    if len(without_og) >= 3:
        add("".join(ascii_words(without_og[:2], 0)) + ".no")
    return out[:limit]


def normalise_site_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().split()[0]
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value.lstrip("/")
    parts = urlsplit(value)
    if not parts.hostname or "." not in parts.hostname:
        return None
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def registered_domain(host: str) -> str:
    host = host.lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_directory(host: str) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS)


def visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def parse(snap: Snapshot) -> tuple[BeautifulSoup, str, str]:
    html = snap.text()
    soup = BeautifulSoup(html, "lxml")
    text_soup = BeautifulSoup(html, "lxml")
    for tag in text_soup(["script", "style", "noscript", "template", "svg"]):
        if tag.name == "script" and "ld+json" in (tag.get("type") or "").lower():
            continue
        tag.decompose()
    return soup, html, re.sub(r"\s+", " ", text_soup.get_text(" ", strip=True))


def json_ld_items(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    stack.extend(item["@graph"])
                items.append(item)
            elif isinstance(item, list):
                stack.extend(item)
    return items


def _types(item: dict[str, Any]) -> set[str]:
    value = item.get("@type")
    if isinstance(value, list):
        return {str(v) for v in value}
    return {str(value)} if value else set()


def social_profile(href: str) -> tuple[str, str] | None:
    href = href.strip()
    for platform, pattern in SOCIAL_PATTERNS:
        match = pattern.match(href)
        if match:
            parts = urlsplit(href)
            path = parts.path.rstrip("/")
            if platform == "facebook" and (path.count("/") > 3 or path.lower() in ("", "/")):
                return None
            query = parts.query if path.lower().endswith("profile.php") else ""
            clean = urlunsplit(("https", parts.netloc.lower().replace("m.facebook", "www.facebook").replace("web.facebook", "www.facebook"), path, query, ""))
            return platform, clean
    return None


def normalise_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+47"):
        rest = digits[3:]
    elif digits.startswith("+"):
        return digits if 8 <= len(digits) <= 16 else None
    else:
        rest = digits
    if len(rest) == 8 and rest[0] in "23456789":
        return "+47 " + " ".join([rest[0:3], rest[3:5], rest[5:8]]) if rest[0] in "489" else "+47 " + " ".join([rest[0:2], rest[2:4], rest[4:6], rest[6:8]])
    return None


EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class SiteVisit:
    def __init__(self, fetcher: Fetcher, rec: CompanyRecord, deadline: float) -> None:
        self.fetcher = fetcher
        self.rec = rec
        self.deadline = deadline
        self.pages: list[tuple[Snapshot, BeautifulSoup, str]] = []

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    def fetch_page(self, url: str) -> tuple[Snapshot, BeautifulSoup, str] | None:
        if self.time_left() < 3:
            return None
        snap = self.fetcher.get(url, retries=0, timeout=(5.0, min(12.0, max(3.0, self.time_left()))))
        if not snap.ok or "html" not in snap.content_type.lower() and not snap.body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            return None
        soup, html, text = parse(snap)
        page = (snap, soup, text)
        self.pages.append(page)
        return page


def _org_on_page(text: str, html: str, org: str) -> tuple[bool, str | None, dict[str, str]]:
    numbers = org_numbers_in(text)
    if org not in numbers:
        # Some footers keep the number in an attribute; only our own number is taken from markup.
        extra = org_numbers_in(re.sub(r"<[^>]+>", " ", re.sub(r'<(script|style)[^>]*>[\s\S]*?</\1>', " ", html, flags=re.I)))
        if org in extra:
            numbers[org] = extra[org]
    return org in numbers, numbers.get(org), numbers


def research_website(fetcher: Fetcher, rec: CompanyRecord, deadline: float, only_nav: bool = False) -> None:
    entity = rec.context.get("entity") or {}
    org = rec.organisation_number
    legal_name = entity.get("navn") or ""
    group_orgs: set[str] = set(rec.context.get("group_orgs") or set())
    subunit_orgs = {s.get("organisation_number") for s in rec.context.get("subunits") or [] if s.get("organisation_number")}
    own_numbers = {org} | subunit_orgs

    candidates: list[tuple[str, str]] = []
    registry_site = normalise_site_url(entity.get("hjemmeside"))
    if registry_site and social_profile(registry_site):
        _publish_registry_social(rec, registry_site)
        registry_site = None
    if registry_site:
        candidates.append((registry_site, "registry_listed"))
    for unit in rec.context.get("subunits") or []:
        site = normalise_site_url(unit.get("website"))
        if site and not social_profile(site) and all(site != c[0] for c in candidates):
            candidates.append((site, "subunit_registry_listed"))
    nav = rec.context.get("nav_employer") or {}
    _publish_nav_profiles(rec, nav)
    if nav.get("homepage"):
        site = normalise_site_url(nav["homepage"][0])
        if site and not social_profile(site) and all(registered_domain(urlsplit(site).hostname or "") != registered_domain(urlsplit(c[0]).hostname or "") for c in candidates):
            candidates.append((site, "nav_employer_homepage"))
    email = (entity.get("epostadresse") or "").strip().lower()
    if "@" in email:
        domain = email.rsplit("@", 1)[1]
        if domain and domain not in FREE_MAIL and not is_directory(domain) and all(registered_domain(urlsplit(c[0]).hostname or "") != registered_domain(domain) for c in candidates):
            candidates.append((f"https://{registered_domain(domain)}/", "registry_email_domain"))
    if only_nav:
        # Second pass after the job feed: only the employer homepage it named is new.
        candidates = [c for c in candidates if c[1] == "nav_employer_homepage"]
        if not candidates:
            return
    guessed = [] if only_nav else guess_domains(legal_name)
    for unit in [] if only_nav else rec.context.get("subunits") or []:
        unit_name = unit.get("name") or ""
        if unit_name and name_tokens(unit_name) != name_tokens(legal_name):
            guessed.extend(d for d in guess_domains(unit_name, limit=2) if d not in guessed)
    guessed = guessed[:8]
    for domain in guessed:
        url = f"https://{domain}/"
        if all(registered_domain(urlsplit(c[0]).hostname or "") != registered_domain(domain) for c in candidates):
            candidates.append((url, "name_derived_domain"))

    checked: list[str] = []
    visit = SiteVisit(fetcher, rec, deadline)
    blocked_reason = None
    budget_hit = False
    out_of_time = False
    for url, origin in candidates:
        if visit.time_left() < 5:
            out_of_time = True
            break
        host = urlsplit(url).hostname or ""
        if is_directory(host):
            continue
        if origin in ("name_derived_domain", "registry_email_domain") and not resolve_public(host):
            continue
        checked.append(url)
        page = None
        variants = [url]
        parts = urlsplit(url)
        bare = (parts.hostname or "").removeprefix("www.")
        if not (parts.hostname or "").startswith("www."):
            variants.append(urlunsplit(("https", "www." + bare, parts.path or "/", parts.query, "")))
        variants.append(urlunsplit(("http", parts.hostname or bare, parts.path or "/", parts.query, "")))
        for attempt_url in variants:
            if visit.time_left() < 5:
                break
            try:
                page = visit.fetch_page(attempt_url)
            except RobotsDisallowed:
                blocked_reason = f"robots.txt disallows {attempt_url}"
                break
            except RequestBudgetExhausted as exc:
                rec.error("website", f"request budget: {exc}", attempt_url)
                budget_hit = True
                break
            except UnsafeURL as exc:
                rec.error("website", f"unsafe or unresolvable URL skipped: {exc}", attempt_url)
                continue
            except Exception as exc:
                rec.error("website", f"fetch failed: {type(exc).__name__}: {str(exc)[:160]}", attempt_url)
                continue
            # A reachable host that answered (even with an error page) needs no other variant.
            break
        if budget_hit:
            break
        if page is None:
            continue
        snap, soup, text = page
        final_host = urlsplit(snap.final_url).hostname or ""
        if is_directory(final_host):
            continue
        lowered = text.lower()
        if len(text) < 1500 and any(marker in lowered for marker in PARKED_MARKERS):
            rec.error("website", "candidate domain is parked or a hosting placeholder", url)
            continue
        found, span, numbers = _org_on_page(text, snap.text(), org)
        identity_snap = snap
        if not found:
            # Look for the number on the contact/about/privacy pages the homepage links to.
            links = []
            for anchor in soup.find_all("a", href=True):
                href = urljoin(snap.final_url, anchor["href"])
                label = f'{anchor.get_text(" ", strip=True)} {anchor["href"]}'
                if urlsplit(href).hostname == final_host and CONTACT_HINTS.search(label) and href not in links and href.split("#")[0] != snap.final_url:
                    links.append(href.split("#")[0])
            for link in links[:3]:
                try:
                    extra = visit.fetch_page(link)
                except Exception:
                    continue
                if not extra:
                    continue
                e_snap, _e_soup, e_text = extra
                e_found, e_span, e_numbers = _org_on_page(e_text, e_snap.text(), org)
                numbers.update({k: v for k, v in e_numbers.items() if k not in numbers})
                if e_found:
                    found, span, identity_snap = True, e_span, e_snap
                    break
        foreign = {n for n in numbers if n not in own_numbers and n not in group_orgs}
        group_on_page = {n for n in numbers if n in group_orgs and n != org}
        if rec.context.get("group_root") == org:
            # The group's parent: its own site naturally lists its subsidiaries.
            group_on_page = set()
            foreign = {n for n in foreign if n not in group_orgs}

        if origin in ("name_derived_domain", "registry_email_domain"):
            if not found:
                continue
            if len(foreign) >= 3:
                rec.error("website", "guessed site shows this organisation number among several others; treated as ambiguous", snap.final_url)
                rec.context["website_ambiguous"] = snap.final_url
                continue
            identity = "organisation_number_on_site"
        else:
            base = "nav_listed" if origin == "nav_employer_homepage" else "registry_listed"
            if found:
                identity = f"{base}_and_organisation_number_on_site"
            elif foreign and not group_on_page:
                identity = f"{base}_other_entity_on_site"
            elif group_on_page:
                identity = f"{base}_group_site"
            else:
                identity = base
                # No organisation number anywhere: the declaration alone is not enough if the domain may
                # have changed hands. Require the name on the page or in the domain, and treat a redirect
                # to another domain without that corroboration as ambiguous.
                corroboration = name_corroboration(legal_name, rec.context.get("subunits") or [], text, snap.final_url)
                if corroboration:
                    identity = f"{base}_name_on_site"
                    span = corroboration
                else:
                    reason = f"the listed site {snap.final_url} shows neither this organisation number nor the company name"
                    rec.context["website_ambiguous"] = snap.final_url
                    rec.context["website_ambiguous_reason"] = reason
                    continue
        if identity.endswith("_other_entity_on_site") or identity.endswith("_group_site"):
            # The listed site names another entity (an umbrella association, a parent or a sister
            # company). Publishing it as this entity's website risks a wrong-company match, so it is
            # reported as ambiguous and its facts are not attributed.
            other = sorted(foreign or group_on_page)[:3]
            reason = f"the listed site {snap.final_url} shows organisation number(s) {', '.join(other)} and not this one" + (" (a member of the same corporate group)" if identity.endswith("_group_site") else "")
            rec.context["website_ambiguous"] = snap.final_url
            rec.context["website_ambiguous_reason"] = reason
            continue
        _publish_site(rec, visit, snap, identity_snap, identity, origin, span, foreign, group_on_page)
        return

    if only_nav:
        return
    if out_of_time and not rec.claims_for("official_website"):
        rec.set_state("official_website", "failed", "time budget ran out before every website candidate was checked", checked_sources=checked)
    elif budget_hit and not rec.claims_for("official_website"):
        rec.set_state("official_website", "failed", "request budget reached before the website could be verified", checked_sources=checked)
    elif blocked_reason and not rec.claims_for("official_website"):
        rec.set_state("official_website", "blocked", blocked_reason, checked_sources=checked)
    elif rec.context.get("website_ambiguous"):
        rec.set_state("official_website", "ambiguous", rec.context.get("website_ambiguous_reason") or "a candidate site shows this organisation number among several unrelated ones", checked_sources=checked)
    elif registry_site:
        rec.set_state("official_website", "not_available", "the register lists a website but it could not be reached or is parked", checked_sources=checked)
    else:
        rec.set_state("official_website", "not_available", "no register-listed website and no name-derived domain shows this organisation number", checked_sources=checked)
    unverified = "ambiguous" if rec.context.get("website_ambiguous") else "not_available"
    rec.set_state("company_profiles", unverified, "no verified company website to read company-owned profiles from", checked_sources=checked)
    rec.set_state("public_brand", unverified, "no verified company website to read a public brand or self-description from", checked_sources=checked)
    if not rec.claims_for("contact_channels"):
        rec.set_state("contact_channels", "not_available", "no phone in the register and no verified website", checked_sources=checked)


def _publish_registry_social(rec: CompanyRecord, url: str) -> None:
    platform, clean = social_profile(url) or (None, None)
    if not platform:
        return
    evidence = [e for c in rec.claims if c["field"] == "registry_website" for e in c["evidence_ids"]]
    if not evidence:
        return
    rec.add_claim(
        family="company_profiles",
        field_name="social_profile",
        value={"platform": platform, "url": clean, "handle": urlsplit(clean).path.strip("/")},
        key=[platform, clean.lower()],
        evidence_ids=evidence,
        confidence=0.95,
        method="registry_listed_profile",
        label=f"{platform.title()} profile",
    )
    rec.set_state("company_profiles", "available", "profile listed as the website in the Entity Register")


def _publish_nav_profiles(rec: CompanyRecord, nav: dict[str, Any]) -> None:
    for key in ("facebookPage", "linkedinPage", "twitterAddress"):
        if key not in nav:
            continue
        value, snap, span = nav[key]
        url = value if value.startswith("http") else "https://" + value.lstrip("/")
        profile = social_profile(url)
        if not profile:
            continue
        platform, clean = profile
        ev = rec.evidence_from_snapshot(snap, source_class="public_job_board", span=span, method="employer_profile_in_verified_job_ad", access_policy="arbeidsplassen.nav.no public ad page; robots.txt allows all")
        rec.add_claim(
            family="company_profiles",
            field_name="social_profile",
            value={"platform": platform, "url": clean, "handle": urlsplit(clean).path.strip("/")},
            key=[platform, clean.lower()],
            evidence_ids=[ev],
            confidence=0.93,
            method="employer_profile_in_verified_job_ad",
            label=f"{platform.title()} profile",
        )
        rec.set_state("company_profiles", "available", "profile declared by the employer in an org-number-verified NAV job ad")


def _publish_site(
    rec: CompanyRecord,
    visit: SiteVisit,
    home: Snapshot,
    identity_snap: Snapshot,
    identity: str,
    origin: str,
    span: str | None,
    foreign: set[str],
    group_on_page: set[str],
) -> None:
    parts = urlsplit(home.final_url)
    site_url = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    evidence = []
    if span:
        evidence.append(rec.evidence_from_snapshot(identity_snap, source_class="company_owned_website", span=span, method="organisation_number_on_page", url=identity_snap.final_url))
    else:
        evidence.append(rec.evidence_from_snapshot(home, source_class="company_owned_website", span=f"homepage reached: {home.final_url}", method="registry_listed_site_fetch", url=home.final_url))
    registry_claims = [c for c in rec.claims if c["field"] == "registry_website"] if origin in ("registry_listed",) else []
    for claim in registry_claims:
        evidence.extend(claim["evidence_ids"])
    exact = identity.endswith("organisation_number_on_site")
    declared = identity in ("registry_listed_name_on_site", "nav_listed_name_on_site")
    relationship = None
    if identity.endswith("_group_site"):
        relationship = "group_site"
    elif identity.endswith("_other_entity_on_site"):
        relationship = "registry_listed_site_names_another_entity" if identity.startswith("registry") else "listed_site_names_another_entity"
    confidence = 0.99 if exact else (0.9 if declared else 0.75)
    rec.add_claim(
        family="official_website",
        field_name="official_website",
        value={"url": site_url, "domain": registered_domain(parts.hostname or ""), "identity_method": identity, "discovered_via": origin},
        key=registered_domain(parts.hostname or ""),
        evidence_ids=evidence,
        confidence=confidence,
        method=identity,
        relationship=relationship,
        label="Official website",
    )
    rec.context["website"] = {"url": site_url, "identity": identity, "exact": exact, "relationship": relationship}
    rec.set_state("official_website", "available", f"website verified ({identity})")
    if relationship in ("registry_listed_site_names_another_entity", "listed_site_names_another_entity"):
        rec.set_state("company_profiles", "ambiguous", "the register-listed site names a different organisation number; its profiles are not attributed to this entity")
        rec.set_state("public_brand", "ambiguous", "the register-listed site names a different organisation number; its brand is not attributed to this entity")
        rec.set_state("contact_channels", "available" if rec.claims_for("contact_channels") else "ambiguous", "site contact details not attributed: the site names another entity")
        return
    _extract_site_facts(rec, visit, home, exact or declared, relationship)


def _extract_site_facts(rec: CompanyRecord, visit: SiteVisit, home: Snapshot, exact: bool, relationship: str | None) -> None:
    site_host = registered_domain(urlsplit(home.final_url).hostname or "")
    pages = list(visit.pages)
    # Read the contact/about pages too when the identity was proven on the homepage.
    home_page = next((p for p in pages if p[0] is home), None)
    if home_page is not None:
        _, soup, _ = home_page
        extra_links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(home.final_url, anchor["href"]).split("#")[0]
            label = f'{anchor.get_text(" ", strip=True)} {anchor["href"]}'
            if registered_domain(urlsplit(href).hostname or "") == site_host and re.search(r"kontakt|contact|om-oss|about", label, re.I) and href not in extra_links:
                extra_links.append(href)
        fetched = {p[0].final_url for p in pages} | {p[0].requested_url for p in pages}
        for link in extra_links[:2]:
            if link in fetched:
                continue
            try:
                visit.fetch_page(link)
            except Exception:
                continue
    pages = [p for p in visit.pages if registered_domain(urlsplit(p[0].final_url).hostname or "") == site_host]

    socials: dict[str, tuple[str, Snapshot, str]] = {}
    phones: dict[str, tuple[Snapshot, str]] = {}
    emails: dict[str, tuple[Snapshot, str]] = {}
    career_links: list[tuple[str, Snapshot, str]] = []
    feeds: list[str] = []
    brand_candidates: list[tuple[str, Snapshot, str]] = []
    description: tuple[str, Snapshot] | None = None
    jobs_ld: list[tuple[dict[str, Any], Snapshot]] = []
    news_ld: list[tuple[dict[str, Any], Snapshot]] = []

    for snap, soup, _text in pages:
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            label = anchor.get_text(" ", strip=True)
            if href.lower().startswith("tel:"):
                phone = normalise_phone(href[4:])
                if phone:
                    phones.setdefault(phone, (snap, f"tel link: {href[:40]} {label[:40]}"))
                continue
            if href.lower().startswith("mailto:"):
                address = href[7:].split("?")[0].strip().lower()
                if EMAIL.match(address) and not address.endswith((".png", ".jpg")):
                    emails.setdefault(address, (snap, f"mailto link: {address}"))
                continue
            absolute = urljoin(snap.final_url, href)
            profile = social_profile(absolute)
            if profile:
                platform, url = profile
                if platform not in socials:
                    socials[platform] = (url, snap, f"link on {snap.final_url}: {url}")
                continue
            if registered_domain(urlsplit(absolute).hostname or "") == site_host and CAREER_HINTS.search(f"{label} {href}") and len(career_links) < 3:
                if all(absolute.split("#")[0] != c[0] for c in career_links):
                    career_links.append((absolute.split("#")[0], snap, f"link '{label[:60]}' -> {absolute[:120]}"))
        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            ltype = (link.get("type") or "").lower()
            if "alternate" in rel and ("rss" in ltype or "atom" in ltype):
                feed = urljoin(snap.final_url, link["href"])
                if registered_domain(urlsplit(feed).hostname or "") == site_host and feed not in feeds and "comments" not in feed:
                    feeds.append(feed)
        if snap is home:
            og_site = soup.find("meta", attrs={"property": "og:site_name"})
            if og_site and og_site.get("content"):
                brand_candidates.append((og_site["content"].strip(), snap, f'og:site_name = "{og_site["content"].strip()}"'))
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            if title:
                brand_candidates.append((title, snap, f'<title> = "{title[:120]}"'))
            for attrs in ({"name": "description"}, {"property": "og:description"}):
                meta = soup.find("meta", attrs=attrs)
                if meta and meta.get("content") and len(meta["content"].strip()) >= 25 and description is None:
                    description = (meta["content"].strip(), snap)
        for item in json_ld_items(soup):
            types = _types(item)
            if types & {"Organization", "Corporation", "LocalBusiness", "ProfessionalService", "Store", "Restaurant", "AutomotiveBusiness", "HomeAndConstructionBusiness", "MedicalBusiness", "LegalService", "FinancialService"}:
                same_as = item.get("sameAs")
                for url in same_as if isinstance(same_as, list) else [same_as] if same_as else []:
                    profile = social_profile(str(url))
                    if profile and profile[0] not in socials:
                        socials[profile[0]] = (profile[1], snap, f"JSON-LD sameAs: {profile[1]}")
                if item.get("telephone"):
                    phone = normalise_phone(str(item["telephone"]))
                    if phone:
                        phones.setdefault(phone, (snap, f'JSON-LD telephone: {item["telephone"]}'))
                if item.get("email"):
                    address = str(item["email"]).replace("mailto:", "").strip().lower()
                    if EMAIL.match(address):
                        emails.setdefault(address, (snap, f"JSON-LD email: {address}"))
                if item.get("name") and snap is home:
                    brand_candidates.insert(0, (str(item["name"]).strip(), snap, f'JSON-LD {sorted(types)[0]} name = "{item["name"]}"'))
            if "JobPosting" in types:
                jobs_ld.append((item, snap))
            if types & {"NewsArticle", "BlogPosting", "Article", "PressRelease"} and item.get("datePublished"):
                news_ld.append((item, snap))

    company_label = "group_site" if relationship == "group_site" else None
    # Brand
    for name, snap, span in brand_candidates[:1]:
        clean = re.split(r"\s[|\-–—:]\s", name)[0].strip()
        if 2 <= len(clean) <= 80:
            ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="html_metadata", url=snap.final_url)
            rec.add_claim(family="public_brand", field_name="public_brand_name", value=clean, evidence_ids=[ev], confidence=0.85 if exact else 0.7, method="html_metadata", relationship=company_label, label="Public brand")
    if description:
        text, snap = description
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'meta description = "{text[:300]}"', method="html_metadata", url=snap.final_url)
        rec.add_claim(family="public_brand", field_name="self_description", value=text[:600], evidence_ids=[ev], confidence=0.9, method="html_metadata", relationship=company_label, label="Self-description (company website)")
    rec.set_state("public_brand", "available" if rec.claims_for("public_brand") else "not_available", "brand and description read from the verified website" if rec.claims_for("public_brand") else "the verified website carries no usable brand name or description")

    for platform, (url, snap, span) in sorted(socials.items()):
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="outbound_link_from_verified_site", url=snap.final_url)
        handle = urlsplit(url).path.strip("/")
        rec.add_claim(
            family="company_profiles",
            field_name="social_profile",
            value={"platform": platform, "url": url, "handle": handle},
            key=[platform, url.lower()],
            evidence_ids=[ev],
            confidence=0.92 if exact else 0.8,
            method="outbound_link_from_verified_site",
            relationship=company_label,
            label=f"{platform.title()} profile",
        )
    if socials:
        rec.set_state("company_profiles", "available", "company-owned profiles linked from the verified website")
    else:
        rec.set_state("company_profiles", "not_available", "the verified website links to no social or video profiles", checked_sources=[p[0].final_url for p in pages])

    for phone, (snap, span) in list(phones.items())[:4]:
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="tel_link", url=snap.final_url)
        rec.add_claim(family="contact_channels", field_name="website_phone", value=phone, key=phone.replace(" ", ""), evidence_ids=[ev], confidence=0.9, method="tel_link", relationship=company_label, label="Phone (website)")
    for address, (snap, span) in list(emails.items())[:4]:
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="mailto_link", url=snap.final_url)
        rec.add_claim(family="contact_channels", field_name="website_email", value=address, evidence_ids=[ev], confidence=0.9, method="mailto_link", relationship=company_label, label="E-mail (website)")
    if rec.claims_for("contact_channels"):
        rec.set_state("contact_channels", "available", "contact channels found")
    else:
        rec.set_state("contact_channels", "not_available", "no phone or e-mail in the register or on the verified website")

    for url, snap, span in career_links[:2]:
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="careers_link_on_verified_site", url=snap.final_url)
        rec.add_claim(family="hiring", field_name="careers_page", value=url, evidence_ids=[ev], confidence=0.85, method="careers_link_on_verified_site", relationship=company_label, label="Careers page")
        rec.context.setdefault("career_pages", []).append(url)
    today = utc_today()
    for item, snap in jobs_ld[:20]:
        title = item.get("title")
        if not title:
            continue
        valid_through = str(item.get("validThrough") or "")[:10]
        if valid_through and valid_through < today:
            continue
        posted = str(item.get("datePosted") or "")[:10] or None
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'JSON-LD JobPosting title = "{title}" datePosted = {posted}', method="json_ld_jobposting", url=snap.final_url)
        rec.add_claim(
            family="hiring",
            field_name="job_posting",
            value={"title": title, "url": safe_link(item.get("url"), site_host) or snap.final_url, "date_posted": posted, "valid_through": valid_through or None, "source": "company_website"},
            key=["site", title, posted],
            evidence_ids=[ev],
            confidence=0.9,
            method="json_ld_jobposting",
            effective_date=posted,
            label="Job posting",
        )
    for item, snap in news_ld[:10]:
        title = item.get("headline") or item.get("name")
        date = str(item.get("datePublished"))[:10]
        if not title:
            continue
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'JSON-LD article "{title}" datePublished = {date}', method="json_ld_article", url=snap.final_url)
        rec.add_claim(family="public_activity", field_name="company_news", value={"title": title, "url": safe_link(item.get("url"), site_host) or snap.final_url, "date": date, "source": "company_website"}, key=["news", title, date], evidence_ids=[ev], confidence=0.9, method="json_ld_article", effective_date=date, label="Company news")

    # Careers page job postings (JSON-LD only) and the site's own feed.
    for url, _snap, _span in career_links[:1]:
        if visit.time_left() < 6:
            break
        try:
            page = visit.fetch_page(url)
        except Exception:
            page = None
        if not page:
            continue
        c_snap, c_soup, _ = page
        for item in json_ld_items(c_soup):
            if "JobPosting" in _types(item) and item.get("title"):
                expires = str(item.get("validThrough") or "")[:10]
                if expires and expires < today:
                    continue
                posted = str(item.get("datePosted") or "")[:10] or None
                ev = rec.evidence_from_snapshot(c_snap, source_class="company_owned_website", span=f'JSON-LD JobPosting title = "{item["title"]}"', method="json_ld_jobposting", url=c_snap.final_url)
                rec.add_claim(
                    family="hiring",
                    field_name="job_posting",
                    value={"title": item["title"], "url": safe_link(item.get("url"), site_host) or c_snap.final_url, "date_posted": posted, "valid_through": expires or None, "source": "company_website"},
                    key=["site", item["title"], posted],
                    evidence_ids=[ev],
                    confidence=0.9,
                    method="json_ld_jobposting",
                    effective_date=posted,
                    label="Job posting",
                )
    for feed in feeds[:1]:
        if visit.time_left() < 5:
            break
        _read_feed(rec, visit, feed, company_label)
    is_wordpress = any("wp-content" in p[0].text()[:400000] or "wp-json" in p[0].text()[:400000] for p in pages[:1])
    if is_wordpress and not any(c["field"] == "company_news" for c in rec.claims) and visit.time_left() > 5:
        _read_wordpress_posts(rec, visit, home, company_label)
    if not any(c["field"] == "company_news" for c in rec.claims) and visit.time_left() > 5 and home_page is not None:
        _read_news_listing(rec, visit, home, home_page[1], company_label)


def _read_feed(rec: CompanyRecord, visit: SiteVisit, url: str, relationship: str | None) -> None:
    try:
        snap = visit.fetcher.get(url, retries=0, timeout=(5.0, 10.0))
    except Exception as exc:
        rec.error("public_activity", f"feed fetch failed: {exc}", url)
        return
    if not snap.ok:
        return
    soup = BeautifulSoup(snap.body, "xml")
    entries = soup.find_all(["item", "entry"])[:10]
    from email.utils import parsedate_to_datetime

    for entry in entries:
        title_tag = entry.find("title")
        title = title_tag.get_text(" ", strip=True) if title_tag else None
        link_tag = entry.find("link")
        link = None
        if link_tag is not None:
            link = link_tag.get("href") or link_tag.get_text(strip=True)
        date_tag = entry.find(["pubDate", "published", "updated", "dc:date", "date"])
        date = None
        stamp = None
        if date_tag is not None:
            raw = date_tag.get_text(strip=True)
            try:
                parsed = parsedate_to_datetime(raw)
                date, stamp = parsed.date().isoformat(), parsed.isoformat()
            except (TypeError, ValueError, IndexError):
                match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
                date = match.group(1) if match else None
        if not title or not date:
            continue
        link = safe_link(link, registered_domain(urlsplit(url).hostname or ""))
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_feed", span=f'feed item "{title[:150]}" {date}', method="rss_atom_feed", url=url)
        rec.add_claim(
            family="public_activity",
            field_name="company_news",
            value={k: v for k, v in {"title": title[:300], "url": link, "date": date, "datetime": stamp or (raw if date_tag is not None else None), "source": "company_feed"}.items() if v},
            key=["news", title[:300], date],
            evidence_ids=[ev],
            confidence=0.92,
            method="rss_atom_feed",
            effective_date=date,
            relationship=relationship,
            label="Company news",
        )


def _read_wordpress_posts(rec: CompanyRecord, visit: SiteVisit, home: Snapshot, relationship: str | None) -> None:
    parts = urlsplit(home.final_url)
    url = urlunsplit((parts.scheme, parts.netloc, "/wp-json/wp/v2/posts", "per_page=10&_fields=date,link,title", ""))
    try:
        snap = visit.fetcher.get(url, retries=0, timeout=(5.0, 10.0), headers={"Accept": "application/json"})
    except Exception as exc:
        rec.error("public_activity", f"WordPress posts fetch failed: {type(exc).__name__}", url)
        return
    if not snap.ok:
        return
    try:
        posts = snap.json()
    except ValueError:
        return
    if not isinstance(posts, list):
        return
    for post in posts[:10]:
        if not isinstance(post, dict):
            continue
        title = re.sub(r"<[^>]+>", "", str((post.get("title") or {}).get("rendered") or "")).strip()
        title = BeautifulSoup(title, "lxml").get_text(" ", strip=True) if "&" in title else title
        date = str(post.get("date") or "")
        link = post.get("link")
        if not title or not re.match(r"\d{4}-\d{2}-\d{2}", date):
            continue
        link = safe_link(link, registered_domain(parts.hostname or ""))
        if not link:
            continue
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'post "{title[:150]}" date {date}', method="wordpress_posts_api", url=url)
        rec.add_claim(
            family="public_activity",
            field_name="company_news",
            value={"title": title[:300], "url": link, "date": date[:10], "datetime": date, "source": "company_website"},
            key=["news", title[:300], date[:10]],
            evidence_ids=[ev],
            confidence=0.92,
            method="wordpress_posts_api",
            effective_date=date[:10],
            relationship=relationship,
            label="Company news",
        )


NEWS_HINTS = re.compile(r"\b(nyheter|nyhet|aktuelt|news|blogg|blog|artikler|presse|press|siste nytt)\b", re.I)


def _dated_items(soup: BeautifulSoup, base_url: str, site_host: str) -> list[tuple[str, str, str, str]]:
    """(title, url, date, span) for links that sit in the same item as a <time datetime>."""
    items: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for time_tag in soup.find_all("time"):
        stamp = (time_tag.get("datetime") or "").strip()
        match = re.match(r"(\d{4}-\d{2}-\d{2})", stamp)
        if not match:
            continue
        container = time_tag
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent
            if container.name in ("article", "li") or any(k in " ".join(container.get("class") or []).lower() for k in ("post", "news", "article", "entry", "card", "item")):
                break
        link = None
        for anchor in container.find_all("a", href=True):
            text = anchor.get_text(" ", strip=True)
            if len(text) >= 12:
                link = anchor
                break
        if link is None:
            heading = container.find(["h1", "h2", "h3", "h4"])
            link = heading.find("a", href=True) if heading else None
        if link is None:
            continue
        url = urljoin(base_url, link["href"]).split("#")[0]
        if registered_domain(urlsplit(url).hostname or "") != site_host or url in seen:
            continue
        title = link.get_text(" ", strip=True)
        if len(title) < 12 or len(title) > 300:
            continue
        seen.add(url)
        items.append((title, url, match.group(1), f'"{title[:150]}" <time datetime="{stamp}">'))
    return items


def _read_news_listing(rec: CompanyRecord, visit: SiteVisit, home: Snapshot, home_soup: BeautifulSoup, relationship: str | None) -> None:
    site_host = registered_domain(urlsplit(home.final_url).hostname or "")
    found = _dated_items(home_soup, home.final_url, site_host)
    source = home
    if not found:
        target = None
        for anchor in home_soup.find_all("a", href=True):
            href = urljoin(home.final_url, anchor["href"]).split("#")[0]
            label = f'{anchor.get_text(" ", strip=True)} {urlsplit(href).path}'
            if registered_domain(urlsplit(href).hostname or "") == site_host and NEWS_HINTS.search(label) and href.rstrip("/") != home.final_url.rstrip("/"):
                target = href
                break
        if not target:
            return
        try:
            page = visit.fetch_page(target)
        except Exception:
            return
        if not page:
            return
        source = page[0]
        found = _dated_items(page[1], page[0].final_url, site_host)
    for title, url, date, span in found[:10]:
        ev = rec.evidence_from_snapshot(source, source_class="company_owned_website", span=span, method="news_listing_time_element", url=source.final_url)
        rec.add_claim(
            family="public_activity",
            field_name="company_news",
            value={"title": title, "url": url, "date": date, "source": "company_website"},
            key=["news", title[:300], date],
            evidence_ids=[ev],
            confidence=0.88,
            method="news_listing_time_element",
            effective_date=date,
            relationship=relationship,
            label="Company news",
        )
