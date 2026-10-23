"""Open job ads from applicant-tracking systems' keyless public job lists.

Many companies run their careers page on an applicant-tracking system (ATS), and most of the
systems Norwegian companies use publish a tenant's open ads in one public, keyless response.
One request then returns every open ad of the tenant, with its title, address and publication
time, and for most systems the employer each ad names:

=============  =================================================================  ==============
system         list (one request per tenant)                                      employer field
=============  =================================================================  ==============
Teamtailor     ``<career host>/jobs.json`` (JSON Feed 1.1 with ``_jobposting``)    hiringOrganization
Greenhouse     ``boards-api.greenhouse.io/v1/boards/<token>/jobs``                company_name
Lever          ``api.lever.co/v0/postings/<tenant>?mode=json``                    (none)
Ashby          ``api.ashbyhq.com/posting-api/job-board/<name>``                   (none)
Personio       ``<tenant>.jobs.personio.de/xml``                                  subcompany
Recruitee      ``<tenant>.recruitee.com/api/offers/``                             company_name
Workable       ``apply.workable.com/api/v1/widget/accounts/<account>``            account name
=============  =================================================================  ==============

Each endpoint was read live and its host's robots.txt checked before it was adopted (see
``POLICY``; the fetcher checks robots.txt again on every run). SmartRecruiters' API host
disallows all agents but LinkedIn's, Webcruiter's candidate host disallows all agents but
Google's, and finn.no's terms forbid automated use: none of them is read.

A tenant is found three ways: the verified website's careers page is an ATS career site, or
links one (``harvest``); or the offline tenant index (``kildespor/data/ats-tenants.tsv.gz``,
declared in CACHE.md) ties a tenant to this organisation number by a hard signal. Either way
the list is fetched live and every ad is held to the same rule: it is published only when it
names this employer (its legal, subunit or verified brand name) or, on a system whose list
names no employer, when the tenant itself is this company's own and is not a group's. A list
naming two or more employers is a group's list, and only the ads naming this entity are kept.
Expired, internal, unlisted and open-application entries are refused.

Module-level imports are the standard library only, so the offline index builder
(``tools/build_ats.py``) can use the parsers without the agent's dependencies.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

POLICY = {
    "teamtailor": "Teamtailor career-site job feed (<career host>/jobs.json, JSON Feed 1.1 with schema.org JobPosting), public, no key; robots.txt allows /jobs (disallows /jobs/internal/ only)",
    "greenhouse": "Greenhouse Job Board API (boards-api.greenhouse.io/v1/boards/<token>/jobs), public, no key; robots.txt disallows /embed/ only",
    "lever": "Lever Postings API (api.lever.co/v0/postings/<tenant>?mode=json), public, no key; robots.txt allows all (crawl-delay 1)",
    "ashby": "Ashby public job-board API (api.ashbyhq.com/posting-api/job-board/<name>), public, no key; no robots.txt (401)",
    "personio": "Personio public job XML feed (<tenant>.jobs.personio.de/xml), public, no key; robots.txt allows all",
    "recruitee": "Recruitee careers-site offers API (<tenant>.recruitee.com/api/offers/), public, no key",
    "workable": "Workable public jobs widget API (apply.workable.com/api/v1/widget/accounts/<account>), public, no key; robots.txt allows all",
    "homerun": "Homerun career site: the open vacancies the careers page itself embeds (<job-list> data), no extra request",
}
EMPLOYER_FIELD = {
    "teamtailor": "hiringOrganization", "greenhouse": "company_name", "personio": "subcompany", "recruitee": "company_name", "workable": "name",
}
MAX_ADS = 25
INDEX_PATH = Path(__file__).resolve().parent / "data" / "ats-tenants.tsv.gz"
INDEX_COLUMNS = ("orgnr", "platform", "list_url", "match_rule", "match_evidence", "names", "built_at")
MAX_INDEX_TENANTS = 2

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,62}$")
# Subdomains of the vendor's own site, never a tenant.
_VENDOR_LABELS = {
    "www", "app", "api", "career", "careers", "jobs", "job", "assets", "static", "help", "support", "status", "go", "blog",
    "partners", "partner", "marketplace", "docs", "developer", "developers", "cdn", "images", "img", "media", "secure", "mail",
    "login", "auth", "admin", "dashboard", "ext", "info", "news", "community", "academy", "events", "learn", "careers-site",
}
# Not one vacancy: open applications, talent pools, CV registration, internal postings.
OPEN_APPLICATION = re.compile(
    r"spontan|(?:[aå]pen|open|aapen)[- ]s[øo]knad|apen-soknad|s[øo]knadsportal|open[- ]application|general[- ]applications?|"
    r"generell[- ]s[øo]knad|talent\s?(?:bank|pool|community|network)|registrer\s+(?:din\s+)?cv|send\s+(?:oss\s+)?(?:din\s+)?cv|"
    r"interesseliste|interessemelding|unsolicited|ikke\s+funnet\s+(?:en\s+)?(?:passende|riktig)\s+stilling|"
    r"didn.?t\s+find\s+(?:the\s+)?(?:right|a\s+suitable)|(?:jobb|work)\s+(?:hos|with)\s+oss\s+i\s+fremtiden|framtidige\s+stillinger|fremtidige\s+stillinger",
    re.I,
)
_TT_MARK = re.compile(r"teamtailor-cdn\.com|assets[\w.-]*\.teamtailor-cdn|teamtailor-production", re.I)


@dataclass(frozen=True)
class Tenant:
    platform: str
    key: str  # host (Teamtailor, Personio, Recruitee) or token/slug
    list_url: str
    board_url: str


@dataclass
class Ad:
    title: str
    url: str
    published: str | None = None
    expires: str | None = None
    employer: str | None = None
    location: str | None = None
    country: str | None = None
    ad_id: str | None = None
    refused: str | None = None


@dataclass
class TenantList:
    platform: str
    list_url: str
    name: str | None = None
    ads: list[Ad] = field(default_factory=list)
    error: str | None = None

    def open_ads(self) -> list[Ad]:
        return [a for a in self.ads if not a.refused]


def _clean(text: Any) -> str:
    return " ".join(unescape(str(text or "")).replace(" ", " ").split())


def _token(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if _TOKEN.match(value) else None


# ---------------------------------------------------------------------------------------------
# Detection


def teamtailor_tenant(host: str) -> Tenant:
    host = host.lower().rstrip(".")
    return Tenant("teamtailor", host, f"https://{host}/jobs.json", f"https://{host}/jobs")


def tenant_from_url(url: str) -> Tenant | None:
    """The ATS tenant a URL names, when it is on a system with a keyless list (else None)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    segs = [s for s in parts.path.split("/") if s]
    if host.endswith(".teamtailor.com"):
        sub = host[: -len(".teamtailor.com")]
        if "." in sub or sub in _VENDOR_LABELS or not _token(sub):
            return None
        return teamtailor_tenant(host)
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"):
        token = (parse_qs(parts.query).get("for") or [None])[0] if segs[:1] == ["embed"] else (segs[0] if segs else None)
        token = _token(token)
        if not token:
            return None
        api = "boards-api.eu.greenhouse.io" if ".eu." in host else "boards-api.greenhouse.io"
        return Tenant("greenhouse", token.lower(), f"https://{api}/v1/boards/{token.lower()}/jobs", f"https://{host}/{token.lower()}")
    if host in ("boards-api.greenhouse.io", "boards-api.eu.greenhouse.io") and segs[:2] == ["v1", "boards"] and len(segs) >= 3 and _token(segs[2]):
        board = "job-boards.eu.greenhouse.io" if ".eu." in host else "job-boards.greenhouse.io"
        return Tenant("greenhouse", segs[2].lower(), f"https://{host}/v1/boards/{segs[2].lower()}/jobs", f"https://{board}/{segs[2].lower()}")
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and segs and _token(segs[0]):
        api = "api.eu.lever.co" if ".eu." in host else "api.lever.co"
        return Tenant("lever", segs[0].lower(), f"https://{api}/v0/postings/{segs[0].lower()}?mode=json", f"https://{host}/{segs[0].lower()}")
    if host in ("api.lever.co", "api.eu.lever.co") and segs[:2] == ["v0", "postings"] and len(segs) >= 3 and _token(segs[2]):
        board = "jobs.eu.lever.co" if ".eu." in host else "jobs.lever.co"
        return Tenant("lever", segs[2].lower(), f"https://{host}/v0/postings/{segs[2].lower()}?mode=json", f"https://{board}/{segs[2].lower()}")
    if host == "jobs.ashbyhq.com" and segs and _token(segs[0]):
        return Tenant("ashby", segs[0], f"https://api.ashbyhq.com/posting-api/job-board/{quote(segs[0])}", f"https://jobs.ashbyhq.com/{segs[0]}")
    for suffix in (".jobs.personio.de", ".jobs.personio.com"):
        if host.endswith(suffix):
            sub = host[: -len(suffix)]
            if "." in sub or not _token(sub):
                return None
            return Tenant("personio", host, f"https://{host}/xml", f"https://{host}/")
    if host.endswith(".recruitee.com"):
        sub = host[: -len(".recruitee.com")]
        if "." in sub or sub in _VENDOR_LABELS or not _token(sub):
            return None
        return Tenant("recruitee", host, f"https://{host}/api/offers/", f"https://{host}/")
    if host == "apply.workable.com" and segs and segs[0] not in ("j", "api") and _token(segs[0]):
        return Tenant("workable", segs[0].lower(), f"https://apply.workable.com/api/v1/widget/accounts/{segs[0].lower()}", f"https://apply.workable.com/{segs[0].lower()}/")
    return None


def tenant_for_page(url: str, html: str) -> Tenant | None:
    """The tenant behind a fetched page: its URL, or a Teamtailor career site on the company's own
    domain (karriere.dips.com), which loads Teamtailor's assets and declares its own /jobs.rss."""
    found = tenant_from_url(url)
    if found is not None:
        return found
    host = (urlsplit(url).hostname or "").lower()
    head = html[:600000]
    if host and _TT_MARK.search(head) and re.search(r"<link[^>]+href=[\"']https?://" + re.escape(host) + r"/jobs\.rss[\"']", head, re.I):
        return teamtailor_tenant(host)
    return None


# ---------------------------------------------------------------------------------------------
# Parsing one list response


def _iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})$", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def _stamp(value: Any) -> str | None:
    """A publication time as the source gives it (ISO 8601), or None."""
    text = str(value or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace(" UTC", "Z").replace(" ", "T", 1) if re.match(r"\d{4}-\d{2}-\d{2} \d", text) else text
    return None


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def screen(ad: Ad, today: str | None = None) -> Ad:
    """Refuse what is not one open vacancy: no title or address, an open application or talent
    pool, an internal posting, an expiry that has passed."""
    today = today or _today()
    if ad.refused:
        return ad
    if not ad.title or not ad.url or not ad.url.startswith(("https://", "http://")):
        ad.refused = "no title or no ad address"
    elif OPEN_APPLICATION.search(ad.title):
        ad.refused = f"open application or talent pool, not a vacancy: {ad.title[:80]}"
    elif "/jobs/internal/" in ad.url or "/internal/" in urlsplit(ad.url).path:
        ad.refused = "internal posting"
    elif ad.expires and ad.expires < today:
        ad.refused = f"expired {ad.expires}"
    return ad


def _tt_location(posting: dict[str, Any]) -> tuple[str | None, str | None]:
    loc = posting.get("jobLocation")
    for entry in loc if isinstance(loc, list) else [loc]:
        if isinstance(entry, dict) and isinstance(entry.get("address"), dict):
            address = entry["address"]
            city = _clean(address.get("addressLocality") or address.get("addressRegion")) or None
            return city, _clean(address.get("addressCountry")) or None
    return None, None


def _org_name(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("legalName") or value.get("name")
    name = _clean(value)
    return name or None


def parse_list(platform: str, list_url: str, text: str, today: str | None = None) -> TenantList:
    """Every entry of one tenant's list, each screened (``Ad.refused`` says why one is not a vacancy)."""
    out = TenantList(platform, list_url)
    try:
        if platform == "personio":
            _parse_personio(out, text)
        else:
            body = json.loads(text)
            {"teamtailor": _parse_teamtailor, "greenhouse": _parse_greenhouse, "lever": _parse_lever, "ashby": _parse_ashby,
             "recruitee": _parse_recruitee, "workable": _parse_workable}[platform](out, body)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        out.error = f"{platform} list unreadable: {type(exc).__name__}: {str(exc)[:80]}"
        out.ads = []
        return out
    for ad in out.ads:
        screen(ad, today)
    return out


def _parse_teamtailor(out: TenantList, body: Any) -> None:
    if not isinstance(body, dict) or "jsonfeed.org" not in str(body.get("version") or "") or not isinstance(body.get("items"), list):
        raise ValueError("not a Teamtailor JSON Feed")
    out.name = _clean(body.get("title")) or None
    for item in body["items"]:
        posting = item.get("_jobposting") if isinstance(item.get("_jobposting"), dict) else {}
        city, country = _tt_location(posting)
        valid = _iso_date(posting.get("validThrough"))
        out.ads.append(Ad(
            title=_clean(item.get("title") or posting.get("title")),
            url=str(item.get("url") or posting.get("url") or "").strip(),
            published=_stamp(item.get("date_published") or posting.get("datePosted")),
            expires=valid,
            employer=_org_name(posting.get("hiringOrganization")),
            location=city,
            country=country,
            ad_id=str(item.get("id") or "") or None,
        ))


def _parse_greenhouse(out: TenantList, body: Any) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
        raise ValueError("not a Greenhouse job list")
    for job in body["jobs"]:
        names = {_clean(job.get("company_name"))} - {""}
        if names and out.name is None:
            out.name = next(iter(names))
        out.ads.append(Ad(
            title=_clean(job.get("title")),
            url=str(job.get("absolute_url") or "").strip(),
            published=_stamp(job.get("first_published") or job.get("updated_at")),
            expires=_iso_date(job.get("application_deadline")),
            employer=_clean(job.get("company_name")) or None,
            location=_clean((job.get("location") or {}).get("name")) or None,
            ad_id=str(job.get("id") or "") or None,
        ))


def _parse_lever(out: TenantList, body: Any) -> None:
    if not isinstance(body, list):
        raise ValueError("not a Lever postings list")
    for job in body:
        created = job.get("createdAt")
        published = datetime.fromtimestamp(created / 1000, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if isinstance(created, (int, float)) else None
        cats = job.get("categories") or {}
        out.ads.append(Ad(
            title=_clean(job.get("text")),
            url=str(job.get("hostedUrl") or "").strip(),
            published=published,
            location=_clean(cats.get("location")) or None,
            country=_clean(job.get("country")) or None,
            ad_id=str(job.get("id") or "") or None,
        ))


def _parse_ashby(out: TenantList, body: Any) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
        raise ValueError("not an Ashby job board")
    for job in body["jobs"]:
        address = ((job.get("address") or {}).get("postalAddress") or {}) if isinstance(job.get("address"), dict) else {}
        ad = Ad(
            title=_clean(job.get("title")),
            url=str(job.get("jobUrl") or "").strip(),
            published=_stamp(job.get("publishedAt")),
            location=_clean(job.get("location")) or None,
            country=_clean(address.get("addressCountry")) or None,
            ad_id=str(job.get("id") or "") or None,
        )
        if job.get("isListed") is False:
            ad.refused = "unlisted posting"
        out.ads.append(ad)


_XML_POSITION = re.compile(r"<position>(.*?)</position>", re.S)


def _xml_field(block: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
    if not m:
        return ""
    return _clean(re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.group(1).strip()))


def _parse_personio(out: TenantList, text: str) -> None:
    if "<workzag-jobs" not in text[:2000]:
        raise ValueError("not a Personio job XML feed")
    host = urlsplit(out.list_url).hostname or ""
    for block in _XML_POSITION.findall(text):
        job_id = _xml_field(block, "id")
        if not re.fullmatch(r"\d+", job_id):
            continue
        out.ads.append(Ad(
            title=_xml_field(block, "name"),
            url=f"https://{host}/job/{job_id}",
            published=_stamp(_xml_field(block, "createdAt")),
            employer=_xml_field(block, "subcompany") or None,
            location=_xml_field(block, "office") or None,
            ad_id=job_id,
        ))


def _parse_recruitee(out: TenantList, body: Any) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("offers"), list):
        raise ValueError("not a Recruitee offers list")
    for job in body["offers"]:
        ad = Ad(
            title=_clean(job.get("title")),
            url=str(job.get("careers_url") or job.get("url") or "").strip(),
            published=_stamp(job.get("published_at")),
            expires=_iso_date(job.get("close_at")),
            employer=_clean(job.get("company_name")) or None,
            location=_clean(job.get("city") or job.get("location")) or None,
            country=_clean(job.get("country")) or None,
            ad_id=str(job.get("id") or "") or None,
        )
        if job.get("status") not in (None, "published"):
            ad.refused = f"status {job.get('status')}"
        out.ads.append(ad)
        if out.name is None and ad.employer:
            out.name = ad.employer


def _parse_workable(out: TenantList, body: Any) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
        raise ValueError("not a Workable widget account")
    out.name = _clean(body.get("name")) or None
    for job in body["jobs"]:
        out.ads.append(Ad(
            title=_clean(job.get("title")),
            url=str(job.get("url") or job.get("shortlink") or "").strip(),
            published=_iso_date(job.get("published_on") or job.get("created_at")),
            employer=out.name,
            location=_clean(job.get("city")) or None,
            country=_clean(job.get("country")) or None,
            ad_id=str(job.get("shortcode") or "") or None,
        ))


_HOMERUN_LIST = re.compile(r"<job-list\s+v-bind=\"([^\"]+)\"", re.I)


def inline_list(page_url: str, html: str) -> TenantList | None:
    """The vacancies a Homerun career page embeds in its own HTML (``<job-list v-bind="...">``),
    each with its address and location; Homerun's list names no employer."""
    if "homerun" not in html[:200000].lower():
        return None
    m = _HOMERUN_LIST.search(html)
    if not m:
        return None
    out = TenantList("homerun", page_url)
    try:
        content = json.loads(unescape(m.group(1))).get("content") or {}
        places = {p.get("id"): p for p in content.get("locations") or [] if isinstance(p, dict)}
        for job in content.get("vacancies") or []:
            place = places.get(job.get("location_id")) or {}
            out.ads.append(Ad(
                title=_clean(job.get("title")),
                url=str(job.get("url") or "").replace("\\/", "/").strip(),
                location=_clean(place.get("name")) or None,
                country=_clean(place.get("country")) or None,
                ad_id=str(job.get("id") or "") or None,
            ))
    except (ValueError, AttributeError, TypeError) as exc:
        out.error = f"homerun list unreadable: {type(exc).__name__}"
        out.ads = []
        return out
    for ad in out.ads:
        screen(ad)
    return out


# ---------------------------------------------------------------------------------------------
# Run time: which ads are this employer's

NORWAY = {"no", "nor", "norway", "norge", "noreg"}
FOREIGN_PLACE = re.compile(
    r"\b(?:sweden|sverige|stockholm|g[öo]teborg|malm[öo]|denmark|danmark|copenhagen|k[øo]benhavn|aarhus|finland|helsinki|germany|deutschland|"
    r"berlin|munich|m[üu]nchen|hamburg|france|paris|lyon|united\s+kingdom|\buk\b|england|london|scotland|ireland|dublin|netherlands|"
    r"amsterdam|rotterdam|belgium|brussels|poland|polska|warsaw|gda[nń]sk|lithuania|lietuva|vilnius|latvia|riga|estonia|tallinn|"
    r"spain|madrid|barcelona|portugal|lisbon|italy|milan|rome|switzerland|zurich|austria|vienna|czech|prague|hungary|budapest|"
    r"romania|bulgaria|croatia|serbia|ukraine|usa|united\s+states|canada|toronto|new\s+york|india|china|shanghai|malaysia|"
    r"singapore|japan|korea|australia|brazil|mexico|dubai|uae)\b",
    re.I,
)


def abroad(ad: Ad) -> bool:
    """The ad's workplace is outside Norway, by its country code or a foreign place in its location."""
    if ad.country and ad.country.strip().lower() not in NORWAY:
        return True
    return bool(ad.location and FOREIGN_PLACE.search(ad.location) and not re.search(r"\b(?:norway|norge)\b", ad.location, re.I))


def _keys(rec: Any, extra: list[str] | None) -> set[str]:
    from .harvest import _employer_key
    from .website import _names_of

    return {_employer_key(n) for n in _names_of(rec) + list(extra or []) + list(rec.context.get("site_brand_names") or []) if _employer_key(n)}


def employer_is_ours(name: str, rec: Any, own_keys: set[str]) -> bool:
    """The ad's employer is this entity: one of its names exactly (legal, subunit or verified
    brand) once the legal form is dropped. A longer name ("Kitron Lithuania" beside KITRON ASA)
    is another unit, and a shorter one ("Leirvik" beside LEIRVIK SVEIS AS) may be another company
    (LEIRVIK AS): neither is accepted."""
    from .harvest import _employer_key
    from .website import _name_matches

    key = _employer_key(name)
    return bool(key) and (key in own_keys or _name_matches(name, rec))


def _group_keys(rec: Any) -> dict[str, str]:
    """Name keys of the other members of this entity's registered group (orgnr by key)."""
    from .harvest import _employer_key

    out: dict[str, str] = {}
    for org, node in (rec.context.get("group_tree") or {}).items():
        if org != rec.organisation_number and node.get("name"):
            out.setdefault(_employer_key(str(node["name"])), str(org))
    out.pop("", None)
    return out


def select(rec: Any, listing: TenantList, *, own_tenant: bool, extra_names: list[str] | None = None) -> tuple[list[Ad], dict[str, str]]:
    """The open ads that are this entity's, and why each other entry was refused.

    ``own_tenant``: the tenant itself is tied to this company (its verified, non-group site is the
    career site or links it with a tenant name resembling the company, or the index ties it by a
    hard signal). Only then may an ad that names no employer be published, and only when no ad on
    the list names an employer at all. An ad whose employer name is also the name of another member
    of the entity's registered group is refused: the name cannot tell the two apart."""
    from .harvest import ad_url_ok, foreign_page

    own_keys = _keys(rec, extra_names)
    refused: dict[str, str] = {}
    open_ads = listing.open_ads()
    from .harvest import _employer_key

    named = {_employer_key(a.employer) for a in open_ads if a.employer and _employer_key(a.employer)}
    several = len(named) >= 2
    group = _group_keys(rec)
    own_text = " ".join(sorted(own_keys))
    kept: list[Ad] = []
    seen: set[str] = set()
    for ad in listing.ads:
        if ad.refused:
            refused[ad.url or ad.title] = ad.refused
            continue
        if not ad_url_ok(ad.url) or foreign_page(ad.url, own_text):
            refused[ad.url] = "not one ad's address (search, login, landing or a foreign sister's site)"
            continue
        if ad.employer:
            if not employer_is_ours(ad.employer, rec, own_keys):
                refused[ad.url] = f"the ad names another employer: {ad.employer[:100]}"
                continue
            other = group.get(_employer_key(ad.employer)) if isinstance(group, dict) else None
            if other:
                # KITRON ASA and KITRON AS are both "Kitron": a name two members of one group
                # share cannot say which of them the ad is for.
                refused[ad.url] = f"the employer name {ad.employer[:80]} is also the name of {other} in the same group"
                continue
        elif not own_tenant or several or named:
            refused[ad.url] = "the ad names no employer and the list is not this company's own"
            continue
        elif abroad(ad):
            # A list naming no employer: a workplace abroad is most often a foreign sister's
            # (Zaptec's Paris and Munich ads), and nothing on the list says otherwise.
            refused[ad.url] = f"the ad names no employer and its workplace is abroad: {(ad.location or ad.country or '')[:60]}"
            continue
        key = ad.url.split("#")[0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(ad)
    return kept[:MAX_ADS], refused


def span_of(ad: Ad, platform: str) -> str:
    """A span quoting the response verbatim: the title, and the employer under its field name."""
    title = ad.title.replace('"', "'")
    span = f'"{title[:180]}"'
    if ad.employer:
        span += f' {EMPLOYER_FIELD.get(platform, "employer")} "{ad.employer.replace(chr(34), chr(39))[:100]}"'
    return span


def value_of(ad: Ad, platform: str) -> dict[str, Any]:
    value = {
        "title": ad.title[:300],
        "url": ad.url,
        "published": ad.published,
        "expires": ad.expires,
        "location": ad.location,
        "employer": ad.employer,
        "source": platform,
        "board": (urlsplit(ad.url).hostname or "").lower() or None,
    }
    return {k: v for k, v in value.items() if v not in (None, "")}


def read_list(fetcher: Any, tenant: Tenant, *, timeout: tuple[float, float] = (5.0, 10.0)) -> tuple[Any, TenantList]:
    """Fetch one tenant's list (one request; robots.txt checked by the fetcher)."""
    accept = "application/xml,text/xml;q=0.9,*/*;q=0.5" if tenant.platform == "personio" else "application/json,application/feed+json;q=0.9,*/*;q=0.5"
    snap = fetcher.get(tenant.list_url, retries=0, timeout=timeout, headers={"Accept": accept})
    if not snap.ok:
        return snap, TenantList(tenant.platform, tenant.list_url, error=f"HTTP {snap.status}")
    return snap, parse_list(tenant.platform, tenant.list_url, snap.text())


def publish(rec: Any, kept: list[Ad], snap: Any, tenant: Tenant, *, method: str, match: str, relationship: str | None = None) -> int:
    """One ``job_posting`` claim per kept ad, citing the list response (the dated live fetch)."""
    published = 0
    for ad in kept:
        ev = rec.evidence_from_snapshot(
            snap,
            source_class="company_ats_page",
            span=span_of(ad, tenant.platform),
            method=method,
            access_policy=f"{POLICY[tenant.platform]}; tenant {match}",
            url=tenant.list_url,
        )
        key = urlsplit(ad.url)
        rec.add_claim(
            family="hiring",
            field_name="job_posting",
            value=value_of(ad, tenant.platform),
            key=["job", f"{(key.hostname or '').lower().removeprefix('www.')}{key.path.rstrip('/')}".lower()],
            evidence_ids=[ev],
            confidence=0.93,
            method=method,
            effective_date=(ad.published or "")[:10] or None,
            relationship=relationship,
            label="Job posting",
        )
        published += 1
    return published


# ---------------------------------------------------------------------------------------------
# The offline tenant index (declared cache)

_INDEX: dict[str, list[dict[str, str]]] | None = None
_INDEX_LOCK = threading.Lock()


def load_index(path: Path | str | None = None) -> dict[str, list[dict[str, str]]]:
    """orgnr -> index rows. A missing or unreadable file is an empty index."""
    global _INDEX
    if path is None and _INDEX is not None:
        return _INDEX
    rows: dict[str, list[dict[str, str]]] = {}
    target = Path(path or os.environ.get("KILDESPOR_ATS_INDEX") or INDEX_PATH)
    try:
        with gzip.open(target, "rt", encoding="utf-8") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != len(header):
                    continue
                row = dict(zip(header, parts))
                if row.get("orgnr") and row.get("platform") in POLICY and row.get("list_url", "").startswith("https://"):
                    rows.setdefault(row["orgnr"], []).append(row)
    except (OSError, EOFError, UnicodeDecodeError):
        rows = {}
    if path is None:
        with _INDEX_LOCK:
            _INDEX = rows
    return rows


def index_tenants(orgnr: str) -> list[tuple[Tenant, dict[str, str]]]:
    out = []
    for row in load_index().get(orgnr, [])[:MAX_INDEX_TENANTS * 2]:
        tenant = tenant_from_url(row["list_url"])
        if tenant is None and row["platform"] == "teamtailor":
            host = urlsplit(row["list_url"]).hostname or ""
            tenant = teamtailor_tenant(host) if host else None
        if tenant is not None and tenant.platform == row["platform"]:
            out.append((tenant, row))
    return out[:MAX_INDEX_TENANTS]


def research_index(fetcher: Any, rec: Any, deadline: float) -> tuple[int, list[str]]:
    """Read the lists the index ties to this organisation number (and that the site harvest has not
    read already), and publish the ads that name this entity. Returns (ads published, lists read)."""
    import time

    read: dict[str, int] = rec.context.setdefault("ats_read", {})
    published = 0
    sources: list[str] = []
    for tenant, row in index_tenants(rec.organisation_number):
        if tenant.list_url in read or time.monotonic() > deadline - 4:
            continue
        try:
            snap, listing = read_list(fetcher, tenant)
        except Exception as exc:  # robots, budget, network: the lookup just moves on
            rec.error("hiring", f"ATS list skipped: {type(exc).__name__}: {str(exc)[:120]}", tenant.list_url)
            continue
        read[tenant.list_url] = 0
        if listing.error:
            continue
        sources.append(tenant.list_url)
        kept, _refused = select(rec, listing, own_tenant=True)
        match = f"tied to this organisation number by the declared ATS index ({row.get('match_rule')}: {row.get('match_evidence', '')[:120]}, built {row.get('built_at', '')})"
        count = publish(rec, kept, snap, tenant, method=f"ats_index_{tenant.platform}_list", match=match)
        read[tenant.list_url] = count
        published += count
    return published, sources
