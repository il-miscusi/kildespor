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


OWNERSHIP_CONTEXT = re.compile(r"org\.?\s*-?\s*n|organisasjons|foretak|mva|vat|reg\.?\s*nr|registrering|company\s+(reg|no|number)", re.I)
CREDIT_WORDS = re.compile(r"levert av|laget av|designet av|design av|utviklet av|utvikla av|nettside av|webside av|hjemmeside av|webdesign|web design|powered by|made by|website by|site by|developed by|designed by|built by|produsert av|\bbyrå\b", re.I)


def _is_credit_link(anchor: Any) -> bool:
    """A social link that belongs to the web agency credited in the footer, not to the company."""
    node = anchor
    for _ in range(2):
        node = node.parent
        if node is None:
            return False
        text = node.get_text(" ", strip=True)
        if len(text) > 160:
            return False
        if CREDIT_WORDS.search(text):
            return True
    return False


def _names_of(rec: CompanyRecord) -> list[str]:
    entity = rec.context.get("entity") or {}
    names = [entity.get("navn") or ""] + [s.get("name") or "" for s in rec.context.get("subunits") or []]
    names += [c["value"] for c in rec.claims if c["field"] == "public_brand_name" and isinstance(c["value"], str)]
    return [n for n in names if n]


def _name_matches(candidate: str, rec: CompanyRecord) -> bool:
    key = " ".join(_fold(t) for t in name_tokens(candidate))
    if not key:
        return False
    for name in _names_of(rec):
        other = " ".join(_fold(t) for t in name_tokens(name))
        if other and (key == other or key.startswith(other + " ") or other.startswith(key + " ")):
            return True
    return False


def _ld_numbers(item: dict[str, Any]) -> set[str]:
    raw = json.dumps({k: item.get(k) for k in ("taxID", "vatID", "identifier", "leiCode", "duns", "globalLocationNumber")}, ensure_ascii=False)
    return set(org_numbers_in(raw))


def _ld_org_is_ours(item: dict[str, Any], rec: CompanyRecord) -> bool:
    numbers = _ld_numbers(item)
    if numbers:
        own = {rec.organisation_number} | {s.get("organisation_number") for s in rec.context.get("subunits") or []}
        return bool(numbers & own)
    name = item.get("name") or item.get("legalName")
    return not name or _name_matches(str(name), rec)


def _ld_employer_is_ours(item: dict[str, Any], rec: CompanyRecord) -> bool:
    employer = item.get("hiringOrganization")
    if not employer:
        return True
    if isinstance(employer, str):
        return _name_matches(employer, rec)
    if isinstance(employer, dict):
        return _ld_org_is_ours(employer, rec)
    return False


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
            if not OWNERSHIP_CONTEXT.search(span or ""):
                # The number appears, but not in the way a site states its own registration
                # (org.nr / Foretaksregisteret / MVA): a customer or supplier reference.
                rec.error("website", "organisation number found without an ownership label; not treated as this entity's site", snap.final_url)
                rec.context["website_ambiguous"] = snap.final_url
                rec.context["website_ambiguous_reason"] = f"{snap.final_url} mentions this organisation number but not as its own registration"
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
    if not (out_of_time or budget_hit or blocked_reason):
        # Every candidate was checked and none is this entity's site: anything read from a
        # previously verified site no longer stands.
        rec.checked_fields.update({"official_website", "social_profile", "website_phone", "website_email", "public_brand_name", "self_description", "careers_page", "company_news"})
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
    rec.checked_fields.add("official_website")
    if relationship in ("registry_listed_site_names_another_entity", "listed_site_names_another_entity"):
        rec.set_state("company_profiles", "ambiguous", "the register-listed site names a different organisation number; its profiles are not attributed to this entity")
        rec.set_state("public_brand", "ambiguous", "the register-listed site names a different organisation number; its brand is not attributed to this entity")
        rec.set_state("contact_channels", "available" if rec.claims_for("contact_channels") else "ambiguous", "site contact details not attributed: the site names another entity")
        return
    from .harvest import _extract_site_facts

    _extract_site_facts(rec, visit, home, exact or declared, relationship)
