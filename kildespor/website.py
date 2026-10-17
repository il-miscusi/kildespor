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

from .http import Fetcher, RobotsDisallowed, Snapshot, UnsafeURL, resolve_public
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
WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)
ORG_CANDIDATE = re.compile(r"(?<![\d])(\d{3})[  .\-]?(\d{3})[  .\-]?(\d{3})(?![\d])")


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


def guess_domains(legal_name: str, limit: int = 6) -> list[str]:
    tokens = name_tokens(legal_name)
    if not tokens or any(token in SKIP_GUESS_WORDS for token in tokens):
        return []
    if len(tokens) == 1 and len(tokens[0]) < 3:
        return []
    if len(tokens) > 4:
        return []
    variants: list[list[str]] = []
    for transform in (0, 1):
        variants.append([(_ascii_variants(t) + _ascii_variants(t))[transform] for t in tokens])
    out: list[str] = []
    for words in variants:
        joined = "".join(words)
        if 3 <= len(joined) <= 40:
            out.append(f"{joined}.no")
            if len(words) > 1:
                out.append(f"{'-'.join(words)}.no")
    unicode_joined = "".join(tokens)
    if re.search(r"[æøå]", unicode_joined):
        try:
            out.append(unicode_joined.encode("idna").decode("ascii") + ".no")
        except UnicodeError:
            pass
    first = variants[0]
    joined = "".join(first)
    if 3 <= len(joined) <= 30:
        out.append(f"{joined}.com")
    if len(first) >= 3:
        out.append(f"{''.join(first[:2])}.no")
    return list(dict.fromkeys(out))[:limit]


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


def research_website(fetcher: Fetcher, rec: CompanyRecord, deadline: float) -> None:
    entity = rec.context.get("entity") or {}
    org = rec.organisation_number
    legal_name = entity.get("navn") or ""
    group_orgs: set[str] = set(rec.context.get("group_orgs") or set())
    subunit_orgs = {s.get("organisation_number") for s in rec.context.get("subunits") or [] if s.get("organisation_number")}
    own_numbers = {org} | subunit_orgs

    candidates: list[tuple[str, str]] = []
    registry_site = normalise_site_url(entity.get("hjemmeside"))
    if registry_site:
        candidates.append((registry_site, "registry_listed"))
    for unit in rec.context.get("subunits") or []:
        site = normalise_site_url(unit.get("website"))
        if site and all(site != c[0] for c in candidates):
            candidates.append((site, "subunit_registry_listed"))
    guessed = guess_domains(legal_name)
    for unit in rec.context.get("subunits") or []:
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
    for url, origin in candidates:
        if visit.time_left() < 5:
            break
        host = urlsplit(url).hostname or ""
        if is_directory(host):
            continue
        if origin == "name_derived_domain" and not resolve_public(host):
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
            except UnsafeURL as exc:
                rec.error("website", f"unsafe or unresolvable URL skipped: {exc}", attempt_url)
                continue
            except Exception as exc:
                rec.error("website", f"fetch failed: {type(exc).__name__}: {str(exc)[:160]}", attempt_url)
                continue
            if page is not None:
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

        if origin == "name_derived_domain":
            if not found:
                continue
            if len(foreign) >= 3:
                rec.error("website", "guessed site shows this organisation number among several others; treated as ambiguous", snap.final_url)
                rec.context["website_ambiguous"] = snap.final_url
                continue
            identity = "organisation_number_on_site"
        else:
            if found:
                identity = "registry_listed_and_organisation_number_on_site"
            elif foreign and not group_on_page:
                identity = "registry_listed_other_entity_on_site"
            elif group_on_page:
                identity = "registry_listed_group_site"
            else:
                identity = "registry_listed"
        _publish_site(rec, visit, snap, identity_snap, identity, origin, span, foreign, group_on_page)
        return

    if blocked_reason and not rec.claims_for("official_website"):
        rec.set_state("official_website", "blocked", blocked_reason, checked_sources=checked)
    elif rec.context.get("website_ambiguous"):
        rec.set_state("official_website", "ambiguous", "a candidate site shows this organisation number among several unrelated ones", checked_sources=checked)
    elif registry_site:
        rec.set_state("official_website", "not_available", "the register lists a website but it could not be reached or is parked", checked_sources=checked)
    else:
        rec.set_state("official_website", "not_available", "no register-listed website and no name-derived domain shows this organisation number", checked_sources=checked)
    rec.set_state("company_profiles", "not_available", "no verified company website to read company-owned profiles from", checked_sources=checked)
    rec.set_state("public_brand", "not_available", "no verified company website to read a public brand or self-description from", checked_sources=checked)
    if not rec.claims_for("contact_channels"):
        rec.set_state("contact_channels", "not_available", "no phone in the register and no verified website", checked_sources=checked)


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
    registry_claims = [c for c in rec.claims_for("official_website") if c["field"] == "registry_website"]
    for claim in registry_claims:
        evidence.extend(claim["evidence_ids"])
    exact = identity in ("organisation_number_on_site", "registry_listed_and_organisation_number_on_site")
    relationship = None
    if identity == "registry_listed_group_site":
        relationship = "group_site"
    elif identity == "registry_listed_other_entity_on_site":
        relationship = "registry_listed_site_names_another_entity"
    confidence = 0.99 if exact else (0.9 if identity == "registry_listed" else 0.75)
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
    if relationship == "registry_listed_site_names_another_entity":
        rec.set_state("company_profiles", "ambiguous", "the register-listed site names a different organisation number; its profiles are not attributed to this entity")
        rec.set_state("public_brand", "ambiguous", "the register-listed site names a different organisation number; its brand is not attributed to this entity")
        rec.set_state("contact_channels", "available" if rec.claims_for("contact_channels") else "ambiguous", "site contact details not attributed: the site names another entity")
        return
    _extract_site_facts(rec, visit, home, exact or identity == "registry_listed", relationship)


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
    for item, snap in jobs_ld[:20]:
        title = item.get("title")
        if not title:
            continue
        posted = str(item.get("datePosted") or "")[:10] or None
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'JSON-LD JobPosting title = "{title}" datePosted = {posted}', method="json_ld_jobposting", url=snap.final_url)
        rec.add_claim(
            family="hiring",
            field_name="job_posting",
            value={"title": title, "url": item.get("url") or snap.final_url, "date_posted": posted, "valid_through": str(item.get("validThrough") or "")[:10] or None, "source": "company_website"},
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
        rec.add_claim(family="public_activity", field_name="company_news", value={"title": title, "url": item.get("url") or snap.final_url, "date": date, "source": "company_website"}, key=["news", title, date], evidence_ids=[ev], confidence=0.9, method="json_ld_article", effective_date=date, label="Company news")

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
                posted = str(item.get("datePosted") or "")[:10] or None
                ev = rec.evidence_from_snapshot(c_snap, source_class="company_owned_website", span=f'JSON-LD JobPosting title = "{item["title"]}"', method="json_ld_jobposting", url=c_snap.final_url)
                rec.add_claim(
                    family="hiring",
                    field_name="job_posting",
                    value={"title": item["title"], "url": item.get("url") or c_snap.final_url, "date_posted": posted, "valid_through": str(item.get("validThrough") or "")[:10] or None, "source": "company_website"},
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
        if date_tag is not None:
            raw = date_tag.get_text(strip=True)
            try:
                date = parsedate_to_datetime(raw).date().isoformat()
            except (TypeError, ValueError, IndexError):
                match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
                date = match.group(1) if match else None
        if not title or not date:
            continue
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_feed", span=f'feed item "{title[:150]}" {date}', method="rss_atom_feed", url=url)
        rec.add_claim(
            family="public_activity",
            field_name="company_news",
            value={"title": title[:300], "url": link, "date": date, "source": "company_feed"},
            key=["news", title[:300], date],
            evidence_ids=[ev],
            confidence=0.92,
            method="rss_atom_feed",
            effective_date=date,
            relationship=relationship,
            label="Company news",
        )
