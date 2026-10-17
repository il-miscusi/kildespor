"""Read facts from a verified company website, one claim per fact.

``website._publish_site`` calls ``_extract_site_facts`` once a site's identity
holds. From the pages the identity check already fetched, plus a small page
plan, it publishes:

* social profiles: every company-owned profile the site links (anchors,
  ``data-href``, embed iframes, JSON-LD ``sameAs``, profile URLs in script
  data) on Facebook, Instagram, LinkedIn, YouTube, X, TikTok, Pinterest,
  Vimeo, Threads and GitHub, in one canonical form, bound to the company by
  name or domain;
* hiring: the careers page (own site or a known applicant-tracking system),
  and one ``job_posting`` claim per open role listed there;
* dated news: RSS/Atom, the WordPress post API, sitemaps, news listings and
  article pages, one ``company_news`` claim per item from the last 24 months;
* brand, self-description, phones and e-mail addresses.

Every claim's evidence points at the page that shows the fact: the linking
page for a profile, the careers or ATS page for a role, the article (or the
feed, sitemap or listing that dates it) for a news item.

Page plan: robots.txt is already cached by the fetcher, so its ``Sitemap:``
lines cost nothing, and one sitemap read names the contact, careers and news
pages even on sites that draw their menus with JavaScript. The harvest spends
at most ``HARVEST_REQUESTS`` requests per verified site (env
``KILDESPOR_HARVEST_REQUESTS``) in this order: sitemap, careers page and its
ATS listing, news feed or listing, a contact page, then up to three article
pages for their own published datetime.
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .http import Snapshot
from .model import CompanyRecord
from .website import (
    CREDIT_WORDS,
    EMAIL,
    GENERIC_NAME_WORDS,
    SiteVisit,
    _ld_employer_is_ours,
    _ld_org_is_ours,
    _names_of,
    _types,
    json_ld_items,
    name_tokens,
    normalise_phone,
    registered_domain,
    safe_link,
    strip_accents,
    utc_today,
)

try:  # Norwegian sites publish in local time; feeds and post APIs give UTC.
    from zoneinfo import ZoneInfo

    OSLO: Any = ZoneInfo("Europe/Oslo")
except Exception:  # pragma: no cover - no tz database
    OSLO = None

HARVEST_REQUESTS = int(os.environ.get("KILDESPOR_HARVEST_REQUESTS", "12"))
NEWS_MAX_AGE_DAYS = 730
MAX_NEWS = 30
MAX_JOBS = 25
MAX_ARTICLE_FETCHES = 3
MAX_PROFILES_PER_PLATFORM = 3

# ----------------------------------------------------------------------------------------------
# Text helpers


def _clean(text: Any) -> str:
    """Visible text: tags dropped, entities decoded (twice when a feed double-encoded them)."""
    raw = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", str(text or ""), flags=re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_lib.unescape(raw)
    if re.search(r"&(?:#\d+|#x[0-9a-f]+|[a-z]+);", raw, re.I):
        raw = html_lib.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def _fold_a(text: str) -> str:
    return strip_accents(text.lower().replace("æ", "ae").replace("ø", "o").replace("å", "a"))


def _fold_b(text: str) -> str:
    return strip_accents(text.lower().replace("æ", "ae").replace("ø", "oe").replace("å", "aa"))


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold_a(text))


def _norm_url(url: str) -> str:
    """Comparison key for a page URL: no scheme, www, query, fragment or trailing slash."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}".lower()


def _strip_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


_TRACKING_PARAM = re.compile(r"^(?:utm_\w+|_gl|_ga|gclid|fbclid|mc_\w+|hs\w*|_hs\w+|ref|source|src|trk|link_source\w*|validator)$", re.I)


def _clean_url(url: str) -> str:
    """``url`` without its fragment and tracking parameters (?_gl=..., ?utm_...)."""
    parts = urlsplit(url)
    params = [q for q in parts.query.split("&") if q and "=" in q and not _TRACKING_PARAM.match(q.split("=", 1)[0])]
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "&".join(params), ""))


def _segments(url: str) -> list[str]:
    return [unquote(s) for s in urlsplit(url).path.split("/") if s]


def _site(url: str) -> str:
    return registered_domain(urlsplit(url).hostname or "")


# ----------------------------------------------------------------------------------------------
# Dates

MONTHS = {
    "januar": 1, "january": 1, "jan": 1, "februar": 2, "february": 2, "feb": 2, "mars": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "june": 6, "jun": 6, "juli": 7, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11, "desember": 12, "december": 12, "des": 12, "dec": 12,
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DMY_RE = re.compile(r"(?<![\d.])(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})(?![\d.])")
_D_MONTH_Y_RE = re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({_MONTH_RE})\.?,?\s+(\d{{4}})(?!\d)", re.I)
_MONTH_D_Y_RE = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)", re.I)
_ISO_STAMP_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s*(Z|[+-]\d{2}:?\d{2})?)?\s*$", re.I)


def _today() -> date:
    return date.fromisoformat(utc_today())


def _valid(year: int, month: int, day: int) -> str | None:
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.year >= 2000 else None


def text_date(text: str) -> str | None:
    """The first date written in ``text`` (ISO, 14.10.2026, 14. oktober 2026, October 14, 2026)."""
    found: list[tuple[int, str]] = []
    for m in _ISO_RE.finditer(text):
        iso = _valid(int(m[1]), int(m[2]), int(m[3]))
        if iso:
            found.append((m.start(), iso))
    for m in _DMY_RE.finditer(text):
        year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        iso = _valid(year, int(m[2]), int(m[1]))
        if iso:
            found.append((m.start(), iso))
    for m in _D_MONTH_Y_RE.finditer(text):
        iso = _valid(int(m[3]), MONTHS[m[2].lower()], int(m[1]))
        if iso:
            found.append((m.start(), iso))
    for m in _MONTH_D_Y_RE.finditer(text):
        iso = _valid(int(m[3]), MONTHS[m[1].lower()], int(m[2]))
        if iso:
            found.append((m.start(), iso))
    return min(found)[1] if found else None


def _iso_from_datetime(dt: datetime, to_local: bool) -> str:
    if dt.tzinfo is not None and to_local and OSLO is not None and dt.utcoffset() == timedelta(0):
        dt = dt.astimezone(OSLO)
    return dt.replace(microsecond=0).isoformat()


def stamp(raw: Any, *, to_local: bool = False) -> tuple[str, str] | None:
    """(published, date) from a machine timestamp.

    An ISO stamp keeps its own form (time and offset as the page wrote them); an
    RFC 822 feed date is converted to Norwegian local time when it is UTC, because the
    article page shows the local offset."""
    text = str(raw or "").strip()
    if not text:
        return None
    m = _ISO_STAMP_RE.match(text)
    if m:
        day = m.group(1)
        if not _valid(*map(int, day.split("-"))):
            return None
        if not m.group(2):
            return day, day
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return day, day
        return _iso_from_datetime(parsed, to_local), day
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is not None:
        published = _iso_from_datetime(parsed, True)
        return published, published[:10]
    day = text_date(text)
    return (day, day) if day else None


def _news_window(day: str) -> bool:
    try:
        parsed = date.fromisoformat(day[:10])
    except ValueError:
        return False
    today = _today()
    return today - timedelta(days=NEWS_MAX_AGE_DAYS) <= parsed <= today + timedelta(days=1)


# ----------------------------------------------------------------------------------------------
# Request plan


class Plan:
    """The harvest's own request allowance for one verified site."""

    def __init__(self, rec: CompanyRecord, visit: SiteVisit, limit: int = HARVEST_REQUESTS) -> None:
        self.rec = rec
        self.visit = visit
        self.limit = limit
        fetcher = visit.fetcher
        self.start = fetcher.thread_requests() if hasattr(fetcher, "thread_requests") else 0
        if hasattr(fetcher, "begin_company"):
            fetcher.begin_company(limit)

    def used(self) -> int:
        fetcher = self.visit.fetcher
        return (fetcher.thread_requests() if hasattr(fetcher, "thread_requests") else 0) - self.start

    def left(self) -> int:
        return self.limit - self.used()

    def can(self, need: int = 1, seconds: float = 4.0) -> bool:
        return self.left() >= need and self.visit.time_left() > seconds

    def known(self, url: str) -> tuple[Snapshot, BeautifulSoup, str] | None:
        key = _norm_url(url)
        for page in self.visit.pages:
            if key in (_norm_url(page[0].final_url), _norm_url(page[0].requested_url)):
                return page
        return None

    def page(self, url: str) -> tuple[Snapshot, BeautifulSoup, str] | None:
        url = url.split("#")[0]
        existing = self.known(url)
        if existing is not None:
            return existing
        if not self.can():
            return None
        try:
            return self.visit.fetch_page(url)
        except Exception as exc:  # robots, budget, network: the plan just moves on
            self.rec.error("website", f"harvest fetch skipped: {type(exc).__name__}: {str(exc)[:120]}", url)
            return None

    def raw(self, url: str, headers: dict[str, str] | None = None) -> Snapshot | None:
        if not self.can():
            return None
        try:
            snap = self.visit.fetcher.get(url, retries=0, timeout=(5.0, min(10.0, max(3.0, self.visit.time_left()))), headers=headers)
        except Exception as exc:
            self.rec.error("website", f"harvest fetch skipped: {type(exc).__name__}: {str(exc)[:120]}", url)
            return None
        return snap if snap.ok else None


# ----------------------------------------------------------------------------------------------
# Social profiles

SOCIAL_HOSTS = {
    "facebook.com": "facebook", "fb.com": "facebook", "fb.me": "facebook",
    "instagram.com": "instagram", "instagr.am": "instagram",
    "linkedin.com": "linkedin",
    "youtube.com": "youtube",
    "twitter.com": "x", "x.com": "x",
    "tiktok.com": "tiktok",
    "pinterest.com": "pinterest", "pinterest.no": "pinterest",
    "vimeo.com": "vimeo",
    "threads.net": "threads", "threads.com": "threads",
    "github.com": "github",
}
CANONICAL_HOST = {
    "facebook": "facebook.com", "instagram": "instagram.com", "linkedin": "linkedin.com", "youtube": "youtube.com",
    "x": "x.com", "tiktok": "tiktok.com", "pinterest": "pinterest.com", "vimeo": "vimeo.com", "threads": "threads.net",
    "github": "github.com",
}
# Subdomains that still serve the public profile; developers., l., business., player. and the like do not.
_PROFILE_SUBDOMAIN = re.compile(r"^(?:|www|m|mobile|web|mbasic|touch|[a-z]{2}|[a-z]{2}-[a-z]{2})$")
RESERVED = {
    "facebook": {
        "sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "events", "groups", "watch", "hashtag",
        "photo", "photos", "photo.php", "permalink.php", "story.php", "login", "login.php", "policies", "policy.php", "help",
        "home", "home.php", "marketplace", "gaming", "stories", "reel", "reels", "video", "videos", "public", "l.php", "flx",
        "ads", "privacy", "terms", "cookies", "legal", "settings", "business", "about", "recover", "checkpoint", "r.php",
        "notes", "media", "search", "fundraisers", "bookmarks", "friends", "messages", "notifications", "live", "pages",
        "people", "profile", "dialog", "v2.0", "v3.0", "page", "your-page", "yourpage", "username",
    },
    "instagram": {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "direct", "about", "legal", "web", "developer", "embed.js", "embed"},
    "x": {"intent", "share", "home", "search", "hashtag", "i", "explore", "login", "settings", "privacy", "tos", "widgets.js", "signup", "notifications", "messages", "compose", "share.php"},
    "tiktok": set(),
    "pinterest": {"pin", "search", "ideas", "today", "explore", "_", "categories", "business", "login", "settings", "about", "topics", "videos", "js"},
    "vimeo": {
        "video", "videos", "ondemand", "showcase", "album", "albums", "groups", "categories", "blog", "upgrade", "features", "log_in",
        "join", "watch", "manage", "channels", "help", "about", "solutions", "enterprise", "create", "stock", "user", "home", "event",
        "events", "live", "search", "jobs", "site_map", "terms", "privacy", "cookie_policy", "purchases", "settings", "api",
    },
    "github": {
        "features", "about", "login", "join", "pricing", "marketplace", "topics", "sponsors", "settings", "explore", "site",
        "enterprise", "security", "team", "customer-stories", "readme", "collections", "trending", "events", "apps", "contact",
        "search", "notifications", "new", "organizations", "solutions", "resources", "open-source",
    },
}
FB_TABS = {"about", "about_details", "timeline", "info", "community", "reviews", "photos", "videos", "events", "posts", "services", "shop", "menu", "mentions", "followers"}
# Platform-generic accounts, site-builder and theme accounts, and template placeholders.
GENERIC_ACCOUNTS = {
    "facebook", "instagram", "linkedin", "youtube", "twitter", "x", "tiktok", "pinterest", "vimeo", "github", "threads", "meta",
    "google", "apple", "microsoft", "wix", "wixcom", "squarespace", "wordpress", "wordpressdotcom", "wordpresscom", "shopify",
    "onecom", "webflow", "jimdo", "weebly", "godaddy", "hubspot", "mailchimp", "elementor", "envato", "themeforest", "yoast",
    "joomla", "drupal", "duda", "strikingly", "webnode", "zyro", "hostinger", "domeneshop", "proisp", "wpengine", "divi",
    "elegantthemes", "themeisle", "generatepress", "astra", "wpastra", "oceanwp", "cookiebot", "trustpilot", "elfsight",
    "username", "yourpage", "yourcompany", "yourname", "company", "page", "profile", "example", "brukernavn", "mittfirma",
    "dittfirma", "share", "home", "pages", "groups", "test", "demo",
}
_HANDLE_CHARS = re.compile(r"^[\w.\-%@]{2,100}$")


def canonical_profile(raw: str) -> tuple[str, str, str] | None:
    """(platform, canonical URL, handle) for a link to a company profile, else None.

    The canonical URL is ``https://<platform host>/<handle>`` with www/m/country
    subdomains, query, fragment and trailing slash dropped and the handle's case kept
    (Facebook's ``profile.php?id=`` keeps its id). Share, intent and plugin links,
    single posts and videos, personal LinkedIn pages (/in/) and platform pages are None."""
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    match = next(((d, p) for d, p in SOCIAL_HOSTS.items() if host == d or host.endswith("." + d)), None)
    if match is None:
        return None
    domain, platform = match
    if not _PROFILE_SUBDOMAIN.match(host[: -len(domain)].rstrip(".")):
        return None
    # Path segments keep the form the site wrote (percent-encoding included, as Builderr's own
    # normaliser does); checks run on the decoded text.
    segs = [s.strip() for s in parts.path.split("/") if s.strip()]
    if platform == "x" and not segs and parts.fragment.startswith("!/"):
        segs = [s for s in parts.fragment[2:].split("/") if s]
    if not segs:
        return None
    first = unquote(segs[0]).lower()
    handle: str | None = None
    if platform == "facebook":
        if first == "profile.php":
            ident = re.search(r"(?:^|&)id=(\d{5,})", parts.query)
            handle = f"profile.php?id={ident.group(1)}" if ident else None
        elif first == "pages" and len(segs) >= 4 and segs[1].lower() == "category":
            handle = "/".join(segs[:4])  # pages/category/<category>/<Name-123456789>
        elif first == "pages" and len(segs) >= 3 and segs[2].isdigit():
            handle = f"pages/{segs[1]}/{segs[2]}"
        elif first == "people" and len(segs) >= 3 and segs[2].isdigit():
            handle = f"people/{segs[1]}/{segs[2]}"
        elif first == "p" and len(segs) >= 2:
            handle = f"p/{segs[1]}"
        elif first == "pg" and len(segs) >= 2:
            handle = segs[1]
        elif first not in RESERVED["facebook"] and not re.fullmatch(r"[\d_]+", first) and (len(segs) == 1 or segs[1].lower() in FB_TABS):
            handle = segs[0]
    elif platform == "instagram":
        if first not in RESERVED["instagram"] and (len(segs) == 1 or segs[1].lower() in ("reels", "tagged", "guides")):
            handle = segs[0]
    elif platform == "linkedin":
        if first in ("company", "school", "showcase") and len(segs) >= 2:
            handle = f"{first}/{segs[1]}"
    elif platform == "youtube":
        if first in ("channel", "c", "user") and len(segs) >= 2:
            handle = f"{first}/{segs[1]}"
        elif first.startswith("@") and len(first) > 2:
            handle = segs[0]
    elif platform == "x":
        if len(segs) == 1 and first.lstrip("@") not in RESERVED["x"] and re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", segs[0]):
            handle = segs[0].lstrip("@")
    elif platform in ("tiktok", "threads"):
        if first.startswith("@") and len(first) > 2 and len(segs) == 1:
            handle = segs[0]
    elif platform == "pinterest":
        if first not in RESERVED["pinterest"] and len(segs) <= 2 and re.fullmatch(r"[A-Za-z0-9_]{3,30}", segs[0]):
            handle = segs[0]
    elif platform == "vimeo":
        if len(segs) == 1 and first not in RESERVED["vimeo"] and re.fullmatch(r"[A-Za-z][\w]{1,40}", segs[0]):
            handle = segs[0]
    elif platform == "github":
        if first == "orgs" and len(segs) >= 2:
            handle = segs[1]
        elif len(segs) == 1 and first not in RESERVED["github"] and re.fullmatch(r"[A-Za-z0-9-]{1,39}", segs[0]):
            handle = segs[0]
    if not handle:
        return None
    if not handle.startswith("profile.php") and not _HANDLE_CHARS.match(unquote(handle.split("/")[-1])):
        return None
    if account_name(handle) in GENERIC_ACCOUNTS:
        return None
    return platform, f"https://{CANONICAL_HOST[platform]}/{handle}", handle


def account_name(handle: str) -> str:
    """The account's name part, folded to [a-z0-9] ('' when the account is known only by an id)."""
    if handle.startswith("profile.php"):
        return ""
    segs = [s for s in handle.split("/") if s]
    if not segs:
        return ""
    head = segs[0].lower()
    if head == "pages" and len(segs) > 3 and segs[1].lower() == "category":
        return _alnum(re.sub(r"-?\d{6,}$", "", unquote(segs[3])))
    if head in ("company", "school", "showcase", "channel", "c", "user", "pages", "people") and len(segs) > 1:
        name = segs[1]
    elif head == "p" and len(segs) > 1:
        name = re.sub(r"-?\d{6,}$", "", segs[1])
    else:
        name = segs[0]
    return _alnum(unquote(name).lstrip("@"))


def id_like(handle: str) -> bool:
    if handle.startswith("profile.php"):
        return True
    if re.match(r"channel/UC[\w-]{15,}$", handle):
        return True
    name = account_name(handle)
    return bool(name) and name.isdigit()


IDENTITY_STOP = {_alnum(w) for w in GENERIC_NAME_WORDS} | {
    "limited", "scandinavia", "international", "nordisk", "norden", "group", "gruppen", "holding", "invest", "as", "asa",
}


def identity_tokens(names: list[str], site_url: str) -> tuple[set[str], set[str]]:
    """(substring tokens, prefix tokens) an account name for this company may contain or start with."""
    contains: set[str] = set()
    prefixes: set[str] = set()
    for name in names:
        words = name_tokens(name or "")
        for fold in (_fold_a, _fold_b):
            ws = [re.sub(r"[^a-z0-9]", "", fold(w)) for w in words]
            ws = [w for w in ws if w]
            if not ws:
                continue
            joined = "".join(ws)
            if len(joined) >= 4:
                contains.add(joined)
            core = [w for w in ws if w not in ("og", "and", "the")]
            if core and "".join(core) != joined and len("".join(core)) >= 4:
                contains.add("".join(core))
            for w in core:
                if w in IDENTITY_STOP:
                    continue
                if len(w) >= 5 or (len(w) == 4 and len(core) <= 2):
                    contains.add(w)
            if len(core) >= 3 and len(core[0] + core[1]) >= 6:
                contains.add(core[0] + core[1])
            if len(core) >= 2:
                acronym = "".join(w[0] for w in core)
                if len(acronym) >= 3:
                    prefixes.add(acronym)
    label = re.sub(r"[^a-z0-9]", "", registered_domain(urlsplit(site_url if "://" in site_url else f"https://{site_url}").hostname or "").split(".")[0])
    if len(label) >= 4:
        contains.add(label)
    if len(label) >= 3:
        prefixes.add(label)
    return contains, prefixes


def resembles(account: str, contains: set[str], prefixes: set[str]) -> bool:
    if len(account) < 3:
        return False
    if any(len(t) >= 4 and t in account for t in contains):
        return True
    if len(account) >= 5 and any(account in t for t in contains):
        return True
    return any(account.startswith(p) for p in prefixes)


FOREIGN_MARKERS = (
    "sweden", "sverige", "svenska", "danmark", "denmark", "finland", "suomi", "germany", "deutschland", "polska", "poland",
    "estonia", "eesti", "latvia", "lithuania", "lietuva", "netherlands", "nederland", "france", "spain", "espana", "italia",
    "italy", "unitedkingdom", "brasil", "brazil", "india", "china", "iceland", "baltic", "baltics",
)
FOREIGN_CODES = ("se", "dk", "fi", "de", "uk", "us", "usa", "pl", "ee", "lt", "lv", "nl", "fr", "es", "it", "is")


def foreign_sibling(account: str, contains: set[str], own_text: str) -> bool:
    """An account for a sister company abroad (headenergysweden, acmedk) on a group's shared site."""
    for marker in FOREIGN_MARKERS:
        if marker in account and marker not in own_text:
            return True
    for token in contains:
        if len(token) >= 4 and account.startswith(token):
            rest = account[len(token):].removesuffix("as").removesuffix("ab")
            if rest in FOREIGN_CODES and rest not in own_text.split():
                return True
    return False


def foreign_page(url: str, own_text: str) -> bool:
    """A URL whose host or path names another country (a sister company's careers site abroad)."""
    parts = urlsplit(url)
    words = _fold_a(f"{parts.hostname or ''} {unquote(parts.path)}")
    return any(marker in words and marker not in own_text for marker in FOREIGN_MARKERS)


def owned_profile(handle: str, contains: set[str], prefixes: set[str], *, allow_id: bool = True, own_text: str = "") -> bool:
    """A profile the company's site links is published as the company's own only when its account
    name resembles the company's names or domain and does not name a sister company abroad (an
    id-only account cannot be compared and is kept when it came from a link the company placed)."""
    if id_like(handle) or not account_name(handle):
        return allow_id
    account = account_name(handle)
    return resembles(account, contains, prefixes) and not foreign_sibling(account, contains, own_text)


_SCRIPT_PROFILE_RE = re.compile(
    r"https?:(?:\\?/){2}(?:[\w-]+\.)*(?:linkedin|facebook|instagram|twitter|x|youtube|tiktok|pinterest|vimeo|threads|github)\.(?:com|net|no)(?:\\?/)(?:[^\s\"'<>)\\]|\\/)+",
    re.I,
)


def _credit_link(anchor: Any) -> bool:
    """A link in the web agency's credit line ("Nettside levert av ..."), not the company's own."""
    node = anchor
    for _ in range(2):
        node = node.parent
        if node is None or len(node.find_all("a", href=True)) > 2:
            return False
        text = node.get_text(" ", strip=True)
        if len(text) > 160:
            return False
        if CREDIT_WORDS.search(text):
            return True
    return False


@dataclass
class Found:
    platform: str
    url: str
    handle: str
    snap: Snapshot
    span: str
    via: str


def profiles_on_page(snap: Snapshot, soup: BeautifulSoup, html: str, contains: set[str], prefixes: set[str], rec: CompanyRecord | None = None, own_text: str = "") -> list[Found]:
    """Company profiles a page of the company's own site links, in page order."""
    base = snap.final_url
    found: list[Found] = []

    def take(raw: str, via: str, *, allow_id: bool = True, span: str | None = None) -> None:
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            return
        try:
            url = urljoin(base, raw.strip())
        except ValueError:
            return
        profile = canonical_profile(url)
        if profile is None:
            return
        platform, canonical, handle = profile
        if not owned_profile(handle, contains, prefixes, allow_id=allow_id, own_text=own_text):
            return
        found.append(Found(platform, canonical, handle, snap, span or raw.strip()[:300], via))

    for anchor in soup.find_all("a", href=True):
        if _credit_link(anchor):
            continue
        take(anchor["href"], "anchor")
    for node in soup.find_all(attrs={"data-href": True}):
        take(str(node.get("data-href") or ""), "data_href")
    for frame in soup.find_all("iframe", src=True):
        src = str(frame.get("src") or "")
        try:
            params = parse_qs(urlsplit(urljoin(base, src)).query)
        except ValueError:
            continue
        for key in ("href", "u", "url"):
            for value in params.get(key, [])[:1]:
                take(value, "embed_iframe", span=src[:300])
    for item in json_ld_items(soup):
        if _types(item) & {"Person", "Product", "Event", "Offer", "JobPosting", "Review"}:
            continue
        if rec is not None and not _ld_org_is_ours(item, rec):
            continue
        same_as = item.get("sameAs")
        for url in same_as if isinstance(same_as, list) else [same_as] if same_as else []:
            if isinstance(url, str):
                take(url, "json_ld_sameAs")
    for script in soup.find_all("script"):
        if "ld+json" in (script.get("type") or "").lower():
            continue
        text = script.string or script.get_text() or ""
        for m in _SCRIPT_PROFILE_RE.finditer(text):
            take(m.group(0).replace("\\/", "/"), "script_data", allow_id=False, span=m.group(0)[:300])
    return found


# ----------------------------------------------------------------------------------------------
# Hiring

CAREER_SEGMENT = re.compile(
    r"^(?:karrierer?|careers?|jobb(?:er)?|jobs|(?:ledige?-?)?stillinger|ledige-jobber|ledig-stilling|vacanc(?:y|ies)|join-?us|"
    r"bli-med(?:-oss|-p[aå]a?-laget|-i-(?:teamet|laget))?|bli-en-av-oss|jobbe?-som-[\w-]+|work-(?:with|for|at)-[\w-]+|rekruttering|recruitment|open-positions|jobbmuligheter|"
    r"(?:kom-og-)?jobbe?-(?:hos|i|med|for)-[\w-]+|karriere-og-jobb|jobb-og-karriere|job-opportunities|career-opportunities|"
    r"arbeid-hos-oss|vi-ansetter|we-are-hiring|karriere-(?:i|hos)-[\w-]+|(?:ledige-)?stillinger-(?:i|hos)-[\w-]+|jobb-her|jobba-(?:hos-oss|har)|arbeta-hos-oss)(?:\.html?|\.php|\.aspx)?$",
    re.I,
)
CAREER_TEXT = re.compile(
    r"^(?:(?:job\s*)?(?:careers?|karriere|jobs?|jobb|stillinger|vacancies|job\s+vacancies|open\s+positions)"
    r"|ledige\s+stillinger|ledige\s+jobber|ledig\s+stilling|jobb(?:e)?\s+(?:hos|i|med|for)\s+\S+(?:\s+\S+)?"
    r"|work\s+(?:with|at|for)\s+\S+(?:\s+\S+)?|join\s+(?:us|our\s+team|the\s+team)|bli\s+med(?:\s+på\s+laget|\s+oss)?"
    r"|jobb\s*(?:&|og)\s*karriere|karriere\s*(?:&|og)\s*jobb|kom\s+og\s+jobb\s+(?:hos|for)\s+\S+|vi\s+ansetter!?|we'?re\s+hiring!?)$",
    re.I,
)
CAREER_HEADING = re.compile(r"careers?|karriere|\bjobs?\b|jobb|ledige\s+stillinger|stillinger|vacanc|open\s+positions|work\s+(with|at|for)|join\s+(us|our)|bli\s+med|vi\s+ansetter|rekrutter", re.I)
CAREER_HOST_LABEL = re.compile(r"^(?:karriere|career|careers|jobb|jobs|jobber|stillinger)$", re.I)
ERROR_PAGE = re.compile(r"\b404\b|not\s+found|finner\s+ikke|finnes\s+ikke|ikke\s+funnet|page\s+(?:does\s+not|doesn.t)\s+exist", re.I)
NO_OPENINGS = re.compile(
    r"ingen\s+(?:ledige|åpne)\s+(?:stillinger|jobber)|har\s+(?:for\s+øyeblikket\s+)?ingen\s+ledige|no\s+(?:open\s+)?(?:positions|vacancies|openings)\s+(?:at\s+the\s+moment|right\s+now|currently)?|"
    r"there\s+are\s+no\s+(?:open\s+)?(?:positions|vacancies|openings)|not\s+(?:currently\s+)?(?:hiring|recruiting)",
    re.I,
)
ATS_DOMAINS = {
    "teamtailor.com": "teamtailor", "webcruiter.no": "webcruiter", "webcruiter.com": "webcruiter", "jobylon.com": "jobylon",
    "reachmee.com": "reachmee", "recman.no": "recman", "recman.page": "recman", "hr-manager.net": "hr-manager",
    "hrmanager.no": "hr-manager", "easycruit.com": "easycruit", "jobbnorge.no": "jobbnorge", "varbi.com": "varbi",
    "smartrecruiters.com": "smartrecruiters", "myworkdayjobs.com": "workday", "lever.co": "lever", "greenhouse.io": "greenhouse",
    "cvpartner.com": "cvpartner", "talentech.com": "talentech", "emply.com": "emply", "emply.net": "emply",
    "workable.com": "workable", "recruitee.com": "recruitee", "homerun.co": "homerun", "personio.de": "personio",
    "personio.com": "personio", "ashbyhq.com": "ashby", "breezy.hr": "breezy", "finn.no": "finn",
}
_ATS_NOT_PUBLIC = re.compile(r"/(?:dashboard|admin|login|logon|sign[_-]?in|sign[_-]?up|auth|account|settings|embed|widget|assets|static|cdn|api)(?:/|$|\?)|\.(?:js|css|png|jpe?g|svg|gif|webp|ico|woff2?)$", re.I)
_ATS_GENERIC_LABELS = {
    "www", "jobs", "job", "apply", "candidate", "candidates", "career", "careers", "boards", "job-boards", "emp", "app", "api",
    "cdn", "static", "assets", "media", "img", "images", "help", "support", "blog", "web", "site", "karriere", "jobb", "en", "no",
    "nb", "sv", "da", "go", "my", "portal", "recruit", "rekruttering", "public",
}
_ATS_GENERIC_SEGMENTS = {
    "jobs", "job", "companies", "company", "arbeidsgiver", "arbeidsgivere", "employer", "employers", "vacancies", "vacancy",
    "ledige-stillinger", "stilling", "stillinger", "careers", "career", "home", "en", "no", "nb", "nb-no", "en-us", "en-gb", "sv",
    "da", "o", "j", "ad", "ads", "list", "list.aspx", "vacancies.aspx", "cv", "ext", "main", "positions", "p", "embed", "candidate",
}
ROLE_URL = re.compile(
    r"/[\w%-]*(?:stilling|position|vacanc|ledige?-?jobb|opening|annonse|jobpost|job-post)[\w%-]*/[^/?#]+"
    r"|/(?:jobs?|jobb|jobber|careers?|karriere|stillinger)/\d{3,}[^/?#]*"
    r"|[?&](?:jobid|job_id|adid|advertid|positionid|projectid|vacancyid|annonseid|rmjob)=\d+"
    r"|/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|jobID:\d+|/vacancy/\d+|/o/[\w-]{3,}|/j/[0-9A-F]{6,}|/\d{5,}(?=[-_/?#.]|$)",
    re.I,
)
NOT_A_ROLE_PATH = re.compile(r"/(?:people|employees|ansatte|team|departments|avdelinger|locations|lokasjoner|blog|blogg|posts|news|nyheter|stories|events|kategori|category|tag|produkt|produkter|product|products|shop|nettbutikk|butikk|prosjekter|projects|referanser)(?:/|$)", re.I)
ROLE_NAV_TEXT = re.compile(
    r"^(?:se\s+alle|vis\s+alle|view\s+all|see\s+(?:all|available|open|our)|all\s+(?:jobs|positions)|read\s+more|les\s+mer|apply(?:\s+now)?|"
    r"søk(?:\s+nå|\s+her)?|send\s+(?:inn\s+)?søknad|open\s+application|åpen\s+søknad|more|mer|next|neste|previous|forrige|back|"
    r"tilbake|home|hjem|careers?|karriere|jobs?|jobb|stillinger|ledige\s+stillinger|contact|kontakt|about|om\s+oss|login|logg\s+inn|"
    r"vis\s+stilling|se\s+stilling(?:en)?|til\s+annonsen|mer\s+info(?:rmasjon)?|more\s+info|klikk\s+her|click\s+here)\b",
    re.I,
)
ROLE_BOILERPLATE = re.compile(r"vilkår|personvern|privacy|cookie|terms|informasjonskapsl|tilgjengelighet|accessibility|sitemap|nettkart|facebook|linkedin|instagram|youtube|twitter", re.I)
ROLE_SECTION = re.compile(
    r"^(?:vi\s+(?:kan\s+)?tilbyr?|vi\s+søker|arbeidsoppgaver|ansvar(?:sområder)?|kvalifikasjoner|ønskede\s+kvalifikasjoner|personlige\s+egenskaper|"
    r"om\s+(?:oss|stillingen|selskapet)|hvem\s+er\s+vi|søknad(?:sfrist)?|kontakt(?:person)?|we\s+offer|responsibilities|qualifications|"
    r"about\s+(?:us|the\s+role)|how\s+to\s+apply)\b|:$",
    re.I,
)
DEADLINE = re.compile(r"(?:søknadsfrist|frist|deadline|apply\s+by|application\s+deadline|siste\s+søknadsdag)\s*:?\s*([^|\n]{0,40})", re.I)
LOCATION = re.compile(r"\b(?i:arbeidssted|sted|lokasjon|location|kontorsted)\s*:\s*([A-ZÆØÅ][\wæøåÆØÅ.\-]{1,30}(?:,? [A-ZÆØÅ][\wæøåÆØÅ.\-]{1,30})?)")


def ats_of(url: str) -> tuple[str, str] | None:
    """(ATS name, matched domain) when ``url`` is on a known applicant-tracking host."""
    host = (urlsplit(url).hostname or "").lower()
    for domain, name in ATS_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return name, domain
    return None


def ats_tenant(url: str) -> str | None:
    """The employer slug an ATS URL names (subdomain, query or path), folded to [a-z0-9]."""
    found = ats_of(url)
    if not found:
        return None
    _name, domain = found
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    labels = [l for l in host[: -len(domain)].rstrip(".").split(".") if l]
    for label in labels:
        if label not in _ATS_GENERIC_LABELS and not re.fullmatch(r"(?:site|web|wd|career)\d+", label):
            return _alnum(label)
    for key, values in parse_qs(parts.query).items():
        if key.lower() in ("customer", "company", "companyname", "org", "employer", "kunde") and values and re.search(r"[a-z]", values[0], re.I):
            return _alnum(values[0])
    for seg in _segments(url):
        low = seg.lower()
        if low in _ATS_GENERIC_SEGMENTS or re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?|\d+|[0-9a-f-]{20,}", low):
            continue
        return _alnum(re.sub(r"^\d+-", "", low))
    return None


def is_careers_link(url: str, text: str, site_host: str) -> int | None:
    """Rank of a link as the company's careers page (0 best), or None.

    A careers word must be a whole path segment (the last one), a careers subdomain, a
    known applicant-tracking host, or the whole link text: a blog post whose slug or
    title merely mentions jobs is not a careers page."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.path.lower().endswith((".pdf", ".jpg", ".png", ".zip", ".xml", ".docx")):
        return None
    host = (parts.hostname or "").lower()
    text = re.sub(r"\s+", " ", text or "").strip()
    if registered_domain(host) == site_host:
        segs = _segments(url)
        sub = host.removesuffix(site_host).rstrip(".").removeprefix("www.").split(".")[0] if host != site_host else ""
        if sub and CAREER_HOST_LABEL.match(sub) and len(segs) <= 2:
            return 0
        if segs and CAREER_SEGMENT.match(segs[-1]) and len(segs) <= 3:
            return 0
        if len(segs) >= 2 and CAREER_SEGMENT.match(segs[-2]) and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?|ledige(?:-stillinger)?|stillinger|jobs|vacancies|openings", segs[-1], re.I):
            return 0
        if 0 < len(text) <= 40 and CAREER_TEXT.match(text) and not NOT_A_ROLE_PATH.search(parts.path):
            return 2
        return None
    ats = ats_of(url)
    if ats is None or _ATS_NOT_PUBLIC.search(parts.path):
        return None
    if ats[0] == "finn":
        return 1 if parts.path.lower().startswith("/job/") else None
    if not ats_tenant(url) or re.search(r"utm_campaign=poweredby|powered", parts.query, re.I):
        return None  # the vendor's own site ("Powered by Teamtailor"), not one employer's page
    return 1


def rss_roles(snap: Snapshot, careers_url: str) -> list["Role"]:
    """Open roles from a careers site's own job feed (Teamtailor publishes /jobs.rss)."""
    host = _site(careers_url)
    roles: list[Role] = []
    for match in _FEED_ITEM.finditer(snap.text()):
        block = match.group(2)
        title_m = _FEED_TITLE.search(block)
        link_m = _FEED_LINK_TEXT.search(block)
        title = _clean(title_m.group(1)) if title_m else ""
        link = link_m.group(1).strip() if link_m else None
        if not title or not link or _site(link) != host:
            continue
        dates = {m.group(1).lower(): _clean(m.group(2)) for m in _FEED_DATE.finditer(block)}
        posted = stamp(dates.get("pubdate") or dates.get("published") or "", to_local=True)
        city = re.search(r"<tt:city>(.*?)</tt:city>", block, re.S)
        roles.append(Role(title[:200], _clean_url(link), posted[0] if posted else None, None, _clean(city.group(1)) if city else None, "careers_site_job_feed", title, "careers_job_feed"))
    return roles[:MAX_JOBS]


@dataclass
class Role:
    title: str
    url: str
    published: str | None
    expires: str | None
    location: str | None
    source: str
    span: str
    method: str


_TITLE_CLASS = re.compile(r"title|heading|name|tittel|overskrift", re.I)


def _role_title(anchor: Any) -> str | None:
    for tag in anchor.find_all(class_=_TITLE_CLASS):
        text = tag.get_text(" ", strip=True)
        if 4 <= len(text) <= 120:
            return text
    for tag in anchor.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"]):
        text = tag.get_text(" ", strip=True)
        if 4 <= len(text) <= 120:
            return text
    if anchor.find(["p", "div", "li", "ul", "br"]) is not None:
        # A whole card: its first line is the title, the rest is place, unit and deadline.
        first = next((line.strip() for line in anchor.get_text("\n", strip=True).split("\n") if line.strip()), "")
        return first if 4 <= len(first) <= 120 and not ROLE_NAV_TEXT.match(first) else None
    text = anchor.get_text(" ", strip=True)
    if text and not ROLE_NAV_TEXT.match(text) and 4 <= len(text) <= 120:
        return text
    if not text or ROLE_NAV_TEXT.match(text):
        node = anchor
        for _ in range(3):
            node = node.parent
            if node is None:
                break
            heading = node.find(["h2", "h3", "h4", "h5"])
            if heading is not None:
                title = heading.get_text(" ", strip=True)
                return title if 4 <= len(title) <= 120 else None
        return None
    first = re.split(r"\s{2,}|\n", anchor.get_text("\n", strip=True))[0].strip()
    return first if 4 <= len(first) <= 120 else None


def _card_text(anchor: Any) -> str:
    node = anchor
    best = anchor.get_text(" ", strip=True)
    for _ in range(3):
        node = node.parent
        if node is None or node.name in ("body", "html", "main"):
            break
        hrefs = {a.get("href") for a in node.find_all("a", href=True)}
        if len(hrefs) > 2:
            break
        best = node.get_text(" ", strip=True)
    return best[:600]


def _ld_location(item: dict[str, Any]) -> str | None:
    loc = item.get("jobLocation")
    for entry in loc if isinstance(loc, list) else [loc]:
        if isinstance(entry, dict):
            address = entry.get("address")
            if isinstance(address, dict):
                city = address.get("addressLocality") or address.get("addressRegion")
                if city:
                    return str(city)[:80]
            elif isinstance(address, str) and address.strip():
                return address.strip()[:80]
    return None


def roles_on_page(snap: Snapshot, soup: BeautifulSoup, rec: CompanyRecord, *, own_site: bool, tenant_ok: bool, site_wide: set[str] | None = None) -> list[Role]:
    """Open roles listed on a careers page (own site) or an ATS listing page.

    JSON-LD JobPosting must name this employer (or name none on a page that is the
    company's own). Role links must look like a single ad (an id, an ad word or an ATS
    detail path); on the company's own page two of them are needed, or one that points to
    an ATS or job board, so a team page or a testimonial is not read as a vacancy."""
    page_url = snap.final_url
    page_site = _site(page_url)
    today = utc_today()
    roles: list[Role] = []
    seen: set[str] = set()
    for item in json_ld_items(soup):
        if "JobPosting" not in _types(item) or not item.get("title"):
            continue
        employer = item.get("hiringOrganization")
        if employer and not _ld_employer_is_ours(item, rec):
            continue
        if not employer and not (own_site or tenant_ok):
            continue
        expires = str(item.get("validThrough") or "")[:10] or None
        if expires and expires < today:
            continue
        title = _clean(item["title"])[:200]
        url = urljoin(page_url, str(item.get("url") or "")) if item.get("url") else page_url
        if not safe_link(url):
            url = page_url
        key = _norm_url(url) + "|" + title.lower()
        if key in seen:
            continue
        seen.add(key)
        posted = stamp(item.get("datePosted"))
        roles.append(Role(title, url, posted[0] if posted else None, expires, _ld_location(item), "json_ld_jobposting", f'JSON-LD JobPosting title = "{title}"', "json_ld_jobposting"))
    if not own_site and not tenant_ok:
        return roles
    links: list[Role] = []
    for anchor in soup.find_all("a", href=True):
        raw = anchor["href"].strip()
        if raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            url = urljoin(page_url, raw).split("#")[0]
        except ValueError:
            continue
        if _norm_url(url) == _norm_url(page_url):
            continue
        ats = ats_of(url)
        on_page_site = _site(url) == page_site
        if not on_page_site and ats is None:
            continue
        if ats is not None and ats[0] == "finn":
            if not re.search(r"finnkode=\d+|/ad/\d+|/job/(?:fulltime|parttime|management)/ad", url, re.I):
                continue
        elif not ROLE_URL.search(url) or NOT_A_ROLE_PATH.search(urlsplit(url).path):
            continue
        title = _role_title(anchor)
        if not title or ROLE_NAV_TEXT.match(title) or ROLE_BOILERPLATE.search(title) or ROLE_SECTION.match(title) or re.fullmatch(r"[\d.\s/:-]+", title):
            continue
        key = _norm_url(url)
        if key in seen or title.lower() in {r.title.lower() for r in links} or (site_wide and key in site_wide):
            continue  # a link the homepage also carries is navigation, not a listed role
        seen.add(key)
        card = _card_text(anchor)
        expires = None
        deadline = DEADLINE.search(card)
        if deadline:
            expires = text_date(deadline.group(1))
            if expires and expires < today:
                continue
        location = LOCATION.search(card)
        located = anchor.find(class_=re.compile(r"location|sted|place|city", re.I))
        if located is not None and not location and 2 <= len(located.get_text(" ", strip=True)) <= 60:
            location = re.match(r"(.+)", located.get_text(" ", strip=True))
        source = ats[0] if ats else ("company_careers_page" if own_site else "ats_listing")
        links.append(Role(title, _clean_url(url), None, expires, location.group(1).strip() if location else None, source, title, "careers_page_role_link"))
    off_site = [r for r in links if _site(r.url) != page_site]
    if not own_site or len(links) >= 2 or off_site:
        roles.extend(links)
    return roles[:MAX_JOBS]


# ----------------------------------------------------------------------------------------------
# News

NEWS_SEGMENT = re.compile(
    r"^(?:nyheter|nyhet|nyheiter|aktuelt|news|newsroom|presse|press|pressemeldinger|pressemelding|press-releases?|presserom|"
    r"blogg|blog|artikler|artikkel|articles?|nyhetsarkiv|siste-nytt|aktuelle-saker|fagartikler|innsikt|insights|"
    r"aktuelt-og-nyheter|nyheter-og-aktuelt|media|mediesenter|innlegg|posts?)$"
    r"|^(?:[\w]+-){0,3}(?:nyheter|aktuelt|news|blogg|blog|artikler|presse|pressemeldinger|newsroom)(?:-[\w]+){0,3}$",
    re.I,
)
NEWS_TEXT = re.compile(
    r"^(?:news|nyheter|aktuelt|siste\s+nytt|nytt|presse|press|pressemeldinger|press\s+releases?|newsroom|media|blogg?|"
    r"nyhetsarkiv|nyhetsrom|aktuelle\s+saker|artikler|insights|innsikt|fagartikler|alle\s+nyheter|flere\s+nyheter|se\s+alle\s+nyheter|"
    r"more\s+news|all\s+news)$",
    re.I,
)
# Shop and catalogue pages: a product feed or a product listing is not news.
NOT_NEWS_PATH = re.compile(
    r"/(?:nettbutikk|butikk|shop|store|produkt|produkter|products?|varer?|sortiment|kategori|category|categories|tag|tags|"
    r"tips_and_guides|tips-og-guider|oppskrifter|oppskrift|recipes?|jobs?|stillinger|ledige-stillinger|karriere|careers?)(?:/|$)",
    re.I,
)
NOT_ARTICLE_SEGMENT = re.compile(r"^(?:kategori|category|categories|tag|tags|emne|author|forfatter|page|side|arkiv|archive|feed|rss|search|sok)$", re.I)
NOT_HEADLINE = re.compile(
    r"^(?:les\s+mer|read\s+more|se\s+alle|vis\s+alle|view\s+all|all\s+news|alle\s+nyheter|nyheter|aktuelt|news|presse|press(?:\s*room)?|"
    r"blogg?|siste\s+nytt|latest\s+news|more|mer|neste|forrige|next|previous|hello\s+world!?|hei\s*,?\s+verden!?)$",
    re.I,
)
CARD_CLASS = re.compile(r"news|nyhet|post|article|artikkel|card|teaser|entry|story|blog|item|aktuelt|press", re.I)


def is_headline(title: str) -> bool:
    if not title or NOT_HEADLINE.match(title.strip()):
        return False
    letters = re.sub(r"[^A-Za-zÆØÅæøåÄÖäöÜüÉé]", "", title)
    return len(letters) >= 8 and 8 <= len(title) <= 250 and text_date(title) != title.strip()


def is_article_url(url: str, site_host: str) -> bool:
    """A single article under a news section (/nyheter/<slug>), not the section or a category."""
    if _site(url) != site_host:
        return False
    segs = _segments(url)
    if len(segs) < 2 or any(NOT_ARTICLE_SEGMENT.match(s) for s in segs):
        return False
    if not any(NEWS_SEGMENT.match(s) for s in segs[:-1]):
        return False
    last = segs[-1]
    if NEWS_SEGMENT.match(last) or re.fullmatch(r"\d{1,4}", last) or last.lower().endswith((".pdf", ".jpg", ".png", ".xml")):
        return False
    return "-" in last or "_" in last or len(last) >= 12 or bool(re.search(r"\d{4,}", last))


def is_listing_url(url: str, site_host: str) -> bool:
    segs = _segments(url)
    return _site(url) == site_host and 1 <= len(segs) <= 3 and bool(NEWS_SEGMENT.match(segs[-1])) and segs[-1].lower() not in ("posts", "post", "media", "innlegg")


@dataclass
class NewsItem:
    title: str
    url: str | None
    published: str
    date: str
    snap: Snapshot
    span: str
    method: str
    source: str
    source_class: str = "company_owned_website"


def listing_items(snap: Snapshot, soup: BeautifulSoup, site_host: str, *, require_news_path: bool) -> list[NewsItem]:
    """Dated items on a listing page (or the homepage): each article/list/card block with one
    article link, a headline and a date (``<time datetime>`` or a written date)."""
    base = snap.final_url
    base_key = _norm_url(base)
    items: list[NewsItem] = []
    seen: set[str] = set()
    blocks = soup.find_all(["article", "li"]) + soup.find_all(["div", "section"], class_=CARD_CLASS)
    for block in blocks:
        if block.find_parent(["nav", "header"]) is not None:
            continue
        links: dict[str, Any] = {}
        for anchor in block.find_all("a", href=True):
            raw = anchor["href"].strip()
            if raw.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            try:
                url = urljoin(base, raw).split("#")[0]
            except ValueError:
                continue
            if _site(url) != site_host or _norm_url(url) == base_key:
                continue
            segs = _segments(url)
            if not segs or any(NOT_ARTICLE_SEGMENT.match(s) for s in segs) or NEWS_SEGMENT.match(segs[-1]):
                continue
            links.setdefault(_norm_url(url), anchor)
        if len(links) != 1:
            continue
        key, anchor = next(iter(links.items()))
        if key in seen:
            continue
        url = urljoin(base, anchor["href"].strip()).split("#")[0]
        if require_news_path and not is_article_url(url, site_host):
            continue
        published = None
        time_tag = block.find("time")
        if time_tag is not None:
            published = stamp(time_tag.get("datetime")) or stamp(time_tag.get_text(" ", strip=True))
        if published is None:
            day = text_date(block.get_text(" ", strip=True)[:400])
            published = (day, day) if day else None
        if published is None:
            continue
        title = None
        for heading in block.find_all(["h1", "h2", "h3", "h4", "h5"]):
            text = heading.get_text(" ", strip=True)
            if is_headline(text):
                title = text
                break
        if title is None:
            text = anchor.get_text(" ", strip=True)
            title = text if is_headline(text) else None
        if title is None:
            continue
        seen.add(key)
        items.append(NewsItem(_clean(title), url, published[0], published[1], snap, title, "news_listing_item", "company_website"))
    return items


_PUBLISHED_META = (
    ("property", "article:published_time"), ("property", "og:article:published_time"), ("name", "article:published_time"),
    ("itemprop", "datePublished"), ("name", "pubdate"), ("name", "publishdate"), ("name", "publish_date"), ("name", "publish-date"),
    ("name", "date"), ("name", "dc.date.issued"), ("name", "DC.date.issued"), ("name", "dcterms.created"), ("name", "parsely-pub-date"),
    ("name", "sailthru.date"), ("property", "og:published_time"),
)


def _strip_site_suffix(title: str, brands: list[str]) -> str:
    parts = re.split(r"\s+[|\-–—:»]\s+", title)
    if len(parts) > 1:
        tail = _alnum(parts[-1])
        if any(tail and (tail in _alnum(b) or _alnum(b) in tail) for b in brands if b) or len(parts[-1]) <= 30:
            return " - ".join(parts[:-1]).strip()
    return title.strip()


def article_meta(snap: Snapshot, soup: BeautifulSoup, brands: list[str]) -> NewsItem | None:
    """Headline and published datetime read from an article page itself."""
    published = None
    span = None
    for attr, name in _PUBLISHED_META:
        tag = soup.find("meta", attrs={attr: name})
        if tag is not None and tag.get("content"):
            published = stamp(tag["content"])
            if published:
                span = f'{attr}="{name}" content="{tag["content"].strip()}"'
                break
    if published is None:
        for item in json_ld_items(soup):
            types = _types(item)
            if item.get("datePublished") and (any("Article" in t or "Posting" in t or "Release" in t for t in types) or "WebPage" in types):
                published = stamp(item["datePublished"])
                if published:
                    span = f'JSON-LD datePublished "{item["datePublished"]}"'
                    break
    if published is None:
        scope = soup.find("article") or soup.find("main") or soup
        time_tag = scope.find("time", attrs={"datetime": True})
        if time_tag is not None:
            published = stamp(time_tag["datetime"])
            span = f'<time datetime="{time_tag["datetime"]}">' if published else None
        if published is None:
            h1 = scope.find("h1")
            text = " ".join(t.get_text(" ", strip=True) for t in ([h1.parent] if h1 is not None and h1.parent is not None else [scope]))[:600]
            day = text_date(text)
            if day:
                published, span = (day, day), f"date written near the headline: {day}"
    if published is None:
        return None
    h1 = (soup.find("article") or soup).find("h1")
    heading = _clean(h1.get_text(" ", strip=True)) if h1 is not None else ""
    og = soup.find("meta", attrs={"property": "og:title"})
    declared = _strip_site_suffix(_clean(og["content"]), brands) if og is not None and og.get("content") else ""
    if not declared and soup.title is not None:
        declared = _strip_site_suffix(_clean(soup.title.get_text(" ", strip=True)), brands)
    title = None
    if is_headline(heading) and (not declared or _alnum(heading) in _alnum(declared) or _alnum(declared) in _alnum(heading)):
        title = heading
    elif is_headline(declared):
        title = declared  # the page's own title when its <h1> is a section banner
    elif is_headline(heading):
        title = heading
    if title is None:
        return None
    title = _clean(title)
    return NewsItem(title, _strip_url(snap.final_url), published[0], published[1], snap, f"{title} | {span}", "article_page_meta", "company_website")


_FEED_ITEM = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
_FEED_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.S | re.I)
_FEED_LINK_TEXT = re.compile(r"<link\b[^>]*>\s*(?:<!\[CDATA\[)?\s*(https?://[^<\s\]]+)", re.S | re.I)
_FEED_LINK_HREF = re.compile(r"<link\b([^>]*?)/?>", re.S | re.I)
_FEED_DATE = re.compile(r"<(pubDate|published|dc:date|updated|a10:updated)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)


def feed_items(snap: Snapshot, site_host: str) -> list[NewsItem]:
    text = snap.text()
    items: list[NewsItem] = []
    for match in _FEED_ITEM.finditer(text):
        block = match.group(2)
        title_m = _FEED_TITLE.search(block)
        title = _clean(title_m.group(1)) if title_m else ""
        link = None
        lm = _FEED_LINK_TEXT.search(block)
        if lm:
            link = lm.group(1).strip()
        else:
            for attrs in _FEED_LINK_HREF.findall(block):
                href = re.search(r'href\s*=\s*["\']([^"\']+)', attrs)
                rel = re.search(r'rel\s*=\s*["\']([^"\']+)', attrs)
                if href and (not rel or rel.group(1).lower() == "alternate"):
                    link = html_lib.unescape(href.group(1))
                    break
        dates = {m.group(1).lower(): _clean(m.group(2)) for m in _FEED_DATE.finditer(block)}
        raw = dates.get("pubdate") or dates.get("published") or dates.get("dc:date") or dates.get("updated") or dates.get("a10:updated")
        published = stamp(raw, to_local=True) if raw else None
        if not published or not is_headline(title):
            continue
        link = safe_link(link, site_host) if link else None
        items.append(NewsItem(title[:300], link, published[0], published[1], snap, f"{title[:200]} | {raw}", "rss_atom_feed", "company_feed", "company_owned_feed"))
    return items


def wordpress_items(snap: Snapshot, site_host: str) -> list[NewsItem]:
    try:
        posts = snap.json()
    except ValueError:
        return []
    items: list[NewsItem] = []
    for post in posts if isinstance(posts, list) else []:
        if not isinstance(post, dict):
            continue
        rendered = (post.get("title") or {}).get("rendered") if isinstance(post.get("title"), dict) else post.get("title")
        title = _clean(rendered)
        local = str(post.get("date") or "")
        gmt = str(post.get("date_gmt") or "")
        if not is_headline(title) or not re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", local):
            continue
        published = local[:19]
        try:
            naive = datetime.fromisoformat(local[:19])
            if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", gmt):
                minutes = round((naive - datetime.fromisoformat(gmt[:19])).total_seconds() / 900) * 15
                published = naive.replace(tzinfo=timezone(timedelta(minutes=minutes))).isoformat()
            elif OSLO is not None:
                published = naive.replace(tzinfo=OSLO).isoformat()
        except ValueError:
            pass
        link = safe_link(post.get("link"), site_host)
        if not link:
            continue
        items.append(NewsItem(title[:300], link, published, local[:10], snap, f"{rendered} | date {local}", "wordpress_posts_api", "company_website"))
    return items


# ----------------------------------------------------------------------------------------------
# Sitemaps

_URL_BLOCK = re.compile(r"<url\b[^>]*>(.*?)</url\s*>", re.S | re.I)
_SITEMAP_BLOCK = re.compile(r"<sitemap\b[^>]*>(.*?)</sitemap\s*>", re.S | re.I)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.S | re.I)
_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S | re.I)
_NEWS_TITLE = re.compile(r"<(?:news:)?title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</(?:news:)?title>", re.S | re.I)
_NEWS_DATE = re.compile(r"<(?:news:)?publication_date>\s*(.*?)\s*</(?:news:)?publication_date>", re.S | re.I)
_CHILD_NEWS = re.compile(r"post|news|nyhet|artik|article|blog|aktuelt|press", re.I)
_CHILD_PAGE = re.compile(r"page|side|sider", re.I)
_CHILD_SKIP = re.compile(r"product|produkt|attachment|category|kategori|tag|author|forfatter|image|video|media|portfolio|faq|shop|store|location|landing|wc_|elementor|template", re.I)


@dataclass
class SitemapEntry:
    loc: str
    lastmod: str | None
    news_title: str | None = None
    news_date: str | None = None


def parse_sitemap(text: str) -> tuple[list[SitemapEntry], list[SitemapEntry]]:
    """(page entries, child sitemaps) of a sitemap or sitemap index."""
    children = []
    for block in _SITEMAP_BLOCK.findall(text):
        loc = _LOC.search(block)
        if loc:
            lastmod = _LASTMOD.search(block)
            children.append(SitemapEntry(html_lib.unescape(loc.group(1).strip()), lastmod.group(1).strip() if lastmod else None))
    pages = []
    for block in _URL_BLOCK.findall(text):
        loc = _LOC.search(block)
        if not loc:
            continue
        lastmod = _LASTMOD.search(block)
        title = _NEWS_TITLE.search(block)
        when = _NEWS_DATE.search(block)
        pages.append(SitemapEntry(html_lib.unescape(loc.group(1).strip()), lastmod.group(1).strip() if lastmod else None, _clean(title.group(1)) if title else None, when.group(1).strip() if when else None))
    return pages, children


def _child_rank(entry: SitemapEntry) -> int:
    name = urlsplit(entry.loc).path.lower()
    if _CHILD_SKIP.search(name) and not _CHILD_NEWS.search(name.rsplit("/", 1)[-1]):
        return 9
    if _CHILD_NEWS.search(name):
        return 0
    if _CHILD_PAGE.search(name):
        return 1
    return 2


def read_sitemaps(plan: Plan, home: Snapshot, site_host: str, wordpress: bool) -> tuple[list[SitemapEntry], list[Snapshot]]:
    """Page entries from the site's sitemap: robots.txt's Sitemap lines (already cached), else
    /sitemap.xml (or /wp-sitemap.xml on WordPress); an index is followed into its news child and its
    page child. At most three requests."""
    parts = urlsplit(home.final_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    declared: list[str] = []
    robots = getattr(plan.visit.fetcher, "_robots", {}) or {}
    parser = robots.get(origin)
    if parser is not None:
        try:
            declared = [u for u in (parser.site_maps() or []) if _site(u) == site_host]
        except Exception:
            declared = []
    candidates = declared[:2] or [origin + "/sitemap.xml"] + ([origin + "/wp-sitemap.xml"] if wordpress else [origin + "/sitemap_index.xml"])
    entries: list[SitemapEntry] = []
    snaps: list[Snapshot] = []
    children: list[SitemapEntry] = []
    for url in candidates:
        if url.endswith(".gz") or not plan.can(1, 6.0):
            continue
        snap = plan.raw(url, headers={"Accept": "application/xml,text/xml;q=0.9,*/*;q=0.5"})
        if snap is None or b"<" not in snap.body[:2000]:
            continue
        pages, kids = parse_sitemap(snap.text())
        if not pages and not kids:
            continue
        snaps.append(snap)
        entries.extend(pages)
        children.extend(kids)
        break
    chosen: list[SitemapEntry] = []
    ranked = sorted((c for c in children if _child_rank(c) < 9 and _site(c.loc) == site_host and not c.loc.endswith(".gz")), key=lambda c: (_child_rank(c), -(int(re.sub(r"\D", "", c.lastmod or "0")[:14] or 0)), c.loc))
    for rank in (0, 1):
        pick = next((c for c in ranked if _child_rank(c) == rank), None)
        if pick is not None:
            chosen.append(pick)
    if not chosen and ranked:
        chosen.append(ranked[0])
    for child in chosen[:2]:
        if not plan.can(1, 6.0):
            break
        snap = plan.raw(child.loc, headers={"Accept": "application/xml,text/xml;q=0.9,*/*;q=0.5"})
        if snap is None:
            continue
        pages, _kids = parse_sitemap(snap.text())
        if pages:
            snaps.append(snap)
            entries.extend(pages)
    return [e for e in entries if _site(e.loc) == site_host], snaps


# ----------------------------------------------------------------------------------------------
# The harvest

CONTACT_SEGMENT = re.compile(r"^(?:kontakt(?:-oss)?|contact(?:-us)?|om-oss|om|about(?:-us)?|selskapet|firma|bedriften|hvem-er-vi|who-we-are|kontaktinformasjon)(?:\.html?|\.php)?$", re.I)
CONTACT_TEXT = re.compile(r"^(?:kontakt(?:\s+oss)?|contact(?:\s+us)?|om\s+oss|about(?:\s+us)?|selskapet|hvem\s+er\s+vi)$", re.I)
WORDPRESS = re.compile(r"/wp-content/|/wp-includes/|<meta[^>]+generator[^>]+WordPress|api\.w\.org", re.I)
ORG_TYPES = {
    "Organization", "Corporation", "LocalBusiness", "ProfessionalService", "Store", "Restaurant", "AutomotiveBusiness",
    "HomeAndConstructionBusiness", "MedicalBusiness", "LegalService", "FinancialService",
}


def _extract_site_facts(rec: CompanyRecord, visit: SiteVisit, home: Snapshot, exact: bool, relationship: str | None) -> None:
    site_host = _site(home.final_url)
    company_label = "group_site" if relationship == "group_site" else None
    plan = Plan(rec, visit)

    def own_pages() -> list[tuple[Snapshot, BeautifulSoup, str]]:
        return [p for p in visit.pages if _site(p[0].final_url) == site_host]

    home_page = next((p for p in visit.pages if p[0] is home), None) or (own_pages()[0] if own_pages() else None)
    if home_page is None:
        return
    home_soup = home_page[1]
    home_html = home.text()
    wordpress = bool(WORDPRESS.search(home_html[:600000]))

    # -- brand, description, contacts (homepage and the pages the identity check read) ------------
    brand_names = _brand_and_contacts(rec, home, own_pages(), exact, company_label)
    names = _names_of(rec) + brand_names
    rec.context["site_brand_names"] = brand_names
    contains, prefixes = identity_tokens(names, home.final_url)
    own_text = " ".join(_fold_a(n) for n in names) + " " + " ".join(re.sub(r"[^a-z0-9 ]", " ", _fold_a(n)) for n in names)

    # -- page plan -----------------------------------------------------------------------------------
    sitemap, sitemap_snaps = read_sitemaps(plan, home, site_host, wordpress)
    # Contact and about pages first: careers links, profiles and contacts often sit only there.
    have = {_kind_of_info(p[0].final_url) for p in own_pages()}
    for kind, url in _info_links(_anchors(own_pages()), sitemap, site_host):
        if kind in have or not plan.can(1, 6.0):
            continue
        page = plan.page(url)
        if page is not None and _site(page[0].final_url) == site_host:
            have.add(kind)
            _brand_and_contacts(rec, home, [page], exact, company_label, contacts_only=True)
    anchors = _anchors(own_pages())

    # Hiring: the careers page (own site first), then the ATS listing it links.
    careers: list[tuple[str, Snapshot, str, str]] = []  # (url, evidence snapshot, span, method)
    roles: list[tuple[Role, Snapshot]] = []
    site_wide = _site_wide_links(home_soup, home.final_url)
    candidates = _careers_candidates(anchors, sitemap, sitemap_snaps, site_host)
    own = next((c for c in candidates if c[0] in (0, 2)), None)
    ats_links = [c for c in candidates if c[0] == 1]
    # True only when every careers source this site points to was read in full this run: refresh
    # may then close a site-found role that is gone ("job_posting:site").
    jobs_complete: bool | None = None
    if own is not None:
        rank, url, snap, span, _text = own
        page = plan.page(url)
        if page is None:
            jobs_complete = False
        if page is not None and _is_careers_page(page, rank, site_host):
            jobs_complete = True
            c_snap, c_soup, _ = page
            title = (c_soup.title.get_text(" ", strip=True) if c_soup.title else "") or url
            careers.append((_clean_url(c_snap.final_url), c_snap, f"careers page: {title[:200]}", "careers_page_on_verified_site"))
            roles += [(r, c_snap) for r in roles_on_page(c_snap, c_soup, rec, own_site=True, tenant_ok=True, site_wide=site_wide)]
            listing = None if roles else _job_listing_link(c_snap, c_soup)
            if listing and listing.endswith(".rss") and plan.can(1, 6.0):
                feed = plan.raw(listing, headers={"Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.5"})
                roles += [(r, feed) for r in rss_roles(feed, c_snap.final_url)] if feed is not None else []
                jobs_complete = feed is not None
            elif listing and plan.can(1, 6.0):
                l_page = plan.page(listing)
                if l_page is not None and _site(l_page[0].final_url) == _site(c_snap.final_url):
                    roles += [(r, l_page[0]) for r in roles_on_page(l_page[0], l_page[1], rec, own_site=True, tenant_ok=True, site_wide=site_wide)]
                else:
                    jobs_complete = False
            elif listing:
                jobs_complete = False  # the job list was not read: the budget ran out
            for a in c_soup.find_all(["a", "iframe"]):
                href = a.get("href") or a.get("src")
                if not href:
                    continue
                try:
                    link = urljoin(c_snap.final_url, str(href)).split("#")[0]
                except ValueError:
                    continue
                if ats_of(link) and is_careers_link(link, a.get_text(" ", strip=True) if a.name == "a" else "", site_host) == 1 and not ROLE_URL.search(link):
                    ats_links.insert(0, (1, link, c_snap, f"link on the careers page {c_snap.final_url}: {link[:200]}", "@careers-page"))
        elif page is None and rank == 0 and snap is not None:
            careers.append((_clean_url(url), snap, span, "careers_link_on_verified_site"))
    seen_ats: set[str] = set()
    fetched_ats = False
    for _rank, url, snap, span, text in ats_links:
        key = _norm_url(url)
        if key in seen_ats or len(seen_ats) >= 2:
            continue
        seen_ats.add(key)
        tenant = ats_tenant(url) or ""
        tenant_ok = bool(tenant) and resembles(tenant, contains, prefixes)
        ats_name = (ats_of(url) or ("", ""))[0]
        page = None
        if ats_name != "finn" and not fetched_ats and plan.can(2, 6.0):
            fetched_ats = True
            page = plan.page(url)
        if ats_name != "finn" and (page is None or ERROR_PAGE.search(page[0].final_url)):
            jobs_complete = False  # an ATS listing this site points to was not read
        elif ats_name != "finn" and jobs_complete is None:
            jobs_complete = True
        if page is not None and not ERROR_PAGE.search(page[0].final_url + " " + ((page[1].title.get_text(" ", strip=True) if page[1].title else "")[:200])):
            a_snap, a_soup, _ = page
            careers.append((_clean_url(a_snap.final_url), a_snap, f"applicant-tracking page ({ats_name}): {(a_soup.title.get_text(' ', strip=True) if a_soup.title else '')[:160]}", "ats_page_linked_from_verified_site"))
            roles += [(r, a_snap) for r in roles_on_page(a_snap, a_soup, rec, own_site=False, tenant_ok=tenant_ok)]
        elif snap is not None and (tenant_ok or text == "@careers-page" or ats_name == "finn" or CAREER_TEXT.match(text or "")):
            careers.append((_clean_url(url), snap, span, "ats_link_on_verified_site"))

    # A group careers site also lists its foreign subsidiaries' roles (career-lithuania.kitron.com).
    roles = [(r, s) for r, s in roles if not foreign_page(r.url, own_text)]

    # News: WordPress post API or the declared feed, then a listing page, then article pages.
    news: dict[str, NewsItem] = {}

    def add_news(item: NewsItem) -> None:
        if not item.title or not _news_window(item.date) or (item.url and (NOT_NEWS_PATH.search(urlsplit(item.url).path) or foreign_page(item.url, own_text))):
            return
        key = _norm_url(item.url) if item.url else f"{item.title.lower()}|{item.date}"
        current = news.get(key)
        if current is None:
            for other_key, other in news.items():
                if other.title.lower() == item.title.lower() and other.date == item.date:
                    current, key = other, other_key
                    break
        if current is None or item.method == "article_page_meta" or (current.method != "article_page_meta" and len(item.published) > len(current.published)):
            news[key] = item

    for snap, soup, _ in own_pages():
        for item in json_ld_items(soup):
            if _types(item) & {"NewsArticle", "BlogPosting", "Article", "PressRelease", "Report"} and item.get("datePublished"):
                title = _clean(item.get("headline") or item.get("name") or "")
                when = stamp(item["datePublished"])
                link = safe_link(urljoin(snap.final_url, str(item.get("url") or "")), site_host) if item.get("url") else snap.final_url
                if when and is_headline(title):
                    add_news(NewsItem(title, link, when[0], when[1], snap, f'JSON-LD "{title[:150]}" datePublished {item["datePublished"]}', "json_ld_article", "company_website"))
    for entry in sitemap:
        if entry.news_title and entry.news_date:
            when = stamp(entry.news_date)
            snap = sitemap_snaps[0] if sitemap_snaps else home
            if when and is_headline(entry.news_title):
                add_news(NewsItem(entry.news_title, entry.loc, when[0], when[1], snap, f"{entry.news_title} | news:publication_date {entry.news_date}", "news_sitemap", "company_website"))
    feeds = _declared_feeds(own_pages(), site_host)
    if wordpress and plan.can(1, 6.0):
        api = _wordpress_api(home_soup, home.final_url)
        snap = plan.raw(api + "wp/v2/posts?per_page=20&_fields=date,date_gmt,link,title", headers={"Accept": "application/json"})
        found = wordpress_items(snap, site_host) if snap is not None else []
        for item in found:
            add_news(item)
        if not found and not feeds:
            feeds = [urlunsplit((urlsplit(home.final_url).scheme, urlsplit(home.final_url).netloc, "/feed/", "", ""))]
    if len(news) < 3 and feeds and plan.can(1, 6.0):
        snap = plan.raw(feeds[0], headers={"Accept": "application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.5"})
        for item in feed_items(snap, site_host) if snap is not None else []:
            add_news(item)
    for item in listing_items(home, home_soup, site_host, require_news_path=True):
        add_news(item)
    if len(news) < 3:
        listing = _news_listing(anchors, sitemap, site_host, home.final_url)
        if listing and plan.can(1, 6.0):
            page = plan.page(listing)
            if page is not None and _site(page[0].final_url) == site_host:
                for item in listing_items(page[0], page[1], site_host, require_news_path=False):
                    add_news(item)

    # Article pages: the headline and published datetime as the article itself states them.
    for url in _articles_to_read(anchors, news, sitemap, site_host)[: MAX_ARTICLE_FETCHES]:
        if not plan.can(1, 6.0):
            break
        page = plan.page(url)
        if page is None or _site(page[0].final_url) != site_host:
            continue
        if len(listing_items(page[0], page[1], site_host, require_news_path=False)) >= 3:
            continue  # a listing, not an article
        item = article_meta(page[0], page[1], brand_names + names)
        if item is not None:
            add_news(item)

    # -- social profiles from every page of the site that was read --------------------------------
    profiles: dict[str, Found] = {}
    per_platform: dict[str, int] = {}
    for snap, soup, _ in own_pages():
        for found in profiles_on_page(snap, soup, snap.text(), contains, prefixes, rec, own_text):
            key = found.url.lower()
            if key in profiles or per_platform.get(found.platform, 0) >= MAX_PROFILES_PER_PLATFORM:
                continue
            profiles[key] = found
            per_platform[found.platform] = per_platform.get(found.platform, 0) + 1
    _publish_profiles(rec, list(profiles.values()), exact, company_label, own_pages())
    _publish_hiring(rec, careers, roles, company_label)
    _publish_news(rec, list(news.values()), company_label)

    _note_pages(rec, own_pages(), home, careers, news, site_host)
    if jobs_complete:
        rec.checked_fields.add("job_posting:site")
    rec.checked_fields.update({"social_profile", "website_phone", "website_email", "public_brand_name", "self_description", "careers_page"})
    if visit.time_left() > 5:
        rec.checked_fields.add("company_news")
    rec.context["harvest"] = {"requests": plan.used(), "sitemap_entries": len(sitemap), "profiles": len(profiles), "careers": len(careers), "roles": len(roles), "news": len(news)}


_CHROME = re.compile(r"menu|nav|footer|header|mega", re.I)


def _site_wide_links(soup: BeautifulSoup, base: str) -> set[str]:
    """Links in the homepage's navigation, header and footer: they sit on every page."""
    keys: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        chrome = anchor.find_parent(["nav", "header", "footer"]) is not None or any(
            _CHROME.search(" ".join(parent.get("class") or []) + " " + str(parent.get("id") or "")) for parent in anchor.parents if getattr(parent, "name", None) not in (None, "[document]", "html", "body")
        )
        if chrome:
            try:
                keys.add(_norm_url(urljoin(base, anchor["href"].strip())))
            except ValueError:
                continue
    return keys


def _note_pages(rec: CompanyRecord, pages: list[tuple[Snapshot, BeautifulSoup, str]], home: Snapshot, careers: list[tuple[str, Snapshot, str, str]], news: dict[str, NewsItem], site_host: str) -> None:
    """Every page read on the verified site, with its <title> and why it was read (the envelope's
    crawl.pages_checked and the kit profile's page list are built from these)."""
    if not hasattr(rec, "note_page"):
        return
    career_keys = {_norm_url(c[1].final_url) for c in careers}
    article_keys = {_norm_url(n.snap.final_url) for n in news.values() if n.method == "article_page_meta"}
    for snap, soup, _ in pages:
        key = _norm_url(snap.final_url)
        segs = _segments(snap.final_url)
        if snap is home:
            purpose = "home"
        elif key in career_keys:
            purpose = "careers"
        elif key in article_keys or is_article_url(snap.final_url, site_host):
            purpose = "news_article"
        elif is_listing_url(snap.final_url, site_host):
            purpose = "news_listing"
        elif segs and CONTACT_SEGMENT.match(segs[-1]):
            purpose = "contact"
        else:
            purpose = "page"
        title = soup.title.get_text(" ", strip=True) if soup.title else None
        rec.note_page(snap, title, purpose)


def _anchors(pages: list[tuple[Snapshot, BeautifulSoup, str]]) -> list[tuple[str, str, Snapshot, str]]:
    """(absolute url, link text, page, raw href) for every anchor on the site's own pages."""
    out = []
    for snap, soup, _ in pages:
        for anchor in soup.find_all("a", href=True):
            raw = anchor["href"].strip()
            if raw.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            try:
                url = urljoin(snap.final_url, raw).split("#")[0]
            except ValueError:
                continue
            out.append((url, anchor.get_text(" ", strip=True), snap, raw))
    return out


def _careers_candidates(anchors: list[tuple[str, str, Snapshot, str]], sitemap: list[SitemapEntry], sitemap_snaps: list[Snapshot], site_host: str) -> list[tuple[int, str, Snapshot | None, str, str]]:
    """(rank, url, linking page, span, link text) for each careers link, best first."""
    found: dict[str, tuple[int, int, str, Snapshot | None, str, str]] = {}
    for order, (url, text, snap, raw) in enumerate(anchors):
        rank = is_careers_link(url, text, site_host)
        if rank is None:
            continue
        if rank == 1 and not (ats_tenant(url) or CAREER_TEXT.match(text or "")) and ats_of(url) and (ats_of(url) or ("",))[0] != "finn":
            continue
        key = _norm_url(url)
        span = f"link '{text[:60]}' -> {raw[:160]}" if rank != 1 else f"careers link on {snap.final_url}: {text[:60]}: {raw[:160]}"
        if key not in found or found[key][0] > rank:
            found[key] = (rank, order, url, snap, span, text)
    for order, entry in enumerate(sitemap):
        rank = is_careers_link(entry.loc, "", site_host)
        if rank == 0 and _norm_url(entry.loc) not in found:
            found[_norm_url(entry.loc)] = (0, 10000 + order, entry.loc, sitemap_snaps[0] if sitemap_snaps else None, f"sitemap <loc>{entry.loc[:200]}</loc>", "")
    ranked = sorted(found.values(), key=lambda f: (f[0], len(_segments(f[2])), f[1]))
    return [(rank, url, snap, span, text) for rank, _order, url, snap, span, text in ranked]


JOB_LISTING_SEGMENT = re.compile(r"^(?:jobs|jobb|stillinger|ledige-stillinger|ledige-jobber|vacancies|open-positions|positions|openings|alle-stillinger)$", re.I)


def _job_listing_link(snap: Snapshot, soup: BeautifulSoup) -> str | None:
    """The job list a careers landing page links to on its own host (Teamtailor's jobs.rss, a
    /jobs or /ledige-stillinger sub-page), when the landing page itself lists no roles."""
    host = (urlsplit(snap.final_url).hostname or "").lower()
    for node in soup.find_all(["a", "link"], href=True):
        try:
            url = urljoin(snap.final_url, node["href"].strip())
        except ValueError:
            continue
        if (urlsplit(url).hostname or "").lower() == host and urlsplit(url).path.lower().endswith("/jobs.rss"):
            return url
    for anchor in soup.find_all("a", href=True):
        try:
            url = urljoin(snap.final_url, anchor["href"].strip()).split("#")[0]
        except ValueError:
            continue
        segs = _segments(url)
        if (urlsplit(url).hostname or "").lower() == host and segs and JOB_LISTING_SEGMENT.match(segs[-1]) and _norm_url(url) != _norm_url(snap.final_url):
            return url
    return None


def _is_careers_page(page: tuple[Snapshot, BeautifulSoup, str], rank: int, site_host: str) -> bool:
    snap, soup, _text = page
    if _site(snap.final_url) != site_host and not ats_of(snap.final_url):
        return False
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1 = soup.find("h1")
    heading = f"{title} {h1.get_text(' ', strip=True) if h1 else ''}"
    if ERROR_PAGE.search(heading):
        return False
    segs = _segments(snap.final_url)
    sub = (urlsplit(snap.final_url).hostname or "").lower().removesuffix(site_host).rstrip(".").removeprefix("www.").split(".")[0]
    if not segs and not CAREER_HOST_LABEL.match(sub or "-"):
        return False  # redirected to the homepage
    by_url = is_careers_link(snap.final_url, "", site_host) == 0
    if rank == 2:
        return bool(CAREER_HEADING.search(heading)) or by_url
    return by_url or bool(CAREER_HEADING.search(heading))


def _declared_feeds(pages: list[tuple[Snapshot, BeautifulSoup, str]], site_host: str) -> list[str]:
    feeds: list[str] = []
    for snap, soup, _ in pages:
        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            kind = (link.get("type") or "").lower()
            if "alternate" in rel and ("rss" in kind or "atom" in kind):
                feed = urljoin(snap.final_url, link["href"])
                if _site(feed) == site_host and not re.search(r"comment|kommentar|product|produkt|shop|butikk|jobs\.rss", feed, re.I) and feed not in feeds:
                    feeds.append(feed)
    return feeds


def _wordpress_api(soup: BeautifulSoup, home_url: str) -> str:
    link = soup.find("link", attrs={"rel": "https://api.w.org/"})
    href = link.get("href") if link is not None else None
    if href and _site(urljoin(home_url, href)) == _site(home_url):
        api = urljoin(home_url, href)
        if "rest_route=" in api:
            return urlunsplit((urlsplit(home_url).scheme, urlsplit(home_url).netloc, "/wp-json/", "", ""))
        return api if api.endswith("/") else api + "/"
    parts = urlsplit(home_url)
    return urlunsplit((parts.scheme, parts.netloc, "/wp-json/", "", ""))


def _news_listing(anchors: list[tuple[str, str, Snapshot, str]], sitemap: list[SitemapEntry], site_host: str, home_url: str) -> str | None:
    best: tuple[int, int, str] | None = None
    for order, (url, text, _snap, _raw) in enumerate(anchors):
        if _norm_url(url) == _norm_url(home_url) or _site(url) != site_host:
            continue
        rank = 0 if is_listing_url(url, site_host) else 1 if (0 < len(text) <= 30 and NEWS_TEXT.match(text) and len(_segments(url)) <= 3) else None
        if rank is not None and (best is None or (rank, order) < best[:2]):
            best = (rank, order, url)
    if best is None:
        for entry in sitemap:
            if is_listing_url(entry.loc, site_host):
                return entry.loc
    return best[2] if best else None


def _kind_of_info(url: str) -> str | None:
    segs = _segments(url)
    if not segs or not CONTACT_SEGMENT.match(segs[-1]):
        return None
    return "contact" if re.search(r"kontakt|contact", segs[-1], re.I) else "about"


def _info_links(anchors: list[tuple[str, str, Snapshot, str]], sitemap: list[SitemapEntry], site_host: str) -> list[tuple[str, str]]:
    """(kind, url) for the site's contact page and its about page, best link of each kind first."""
    best: dict[str, tuple[tuple[int, int], str]] = {}
    for order, (url, text, _snap, _raw) in enumerate(anchors):
        segs = _segments(url)
        if _site(url) != site_host or not segs or len(segs) > 2:
            continue
        by_path = bool(CONTACT_SEGMENT.match(segs[-1]))
        by_text = 0 < len(text) <= 30 and bool(CONTACT_TEXT.match(text))
        if by_path or by_text:
            kind = "contact" if re.search(r"kontakt|contact", segs[-1] + " " + text, re.I) else "about"
            rank = (0 if by_path else 1, order)
            if kind not in best or rank < best[kind][0]:
                best[kind] = (rank, url)
    for entry in sitemap:
        segs = _segments(entry.loc)
        if segs and len(segs) <= 2 and CONTACT_SEGMENT.match(segs[-1]):
            kind = _kind_of_info(entry.loc) or "about"
            best.setdefault(kind, ((2, 0), entry.loc))
    return [(kind, best[kind][1]) for kind in ("contact", "about") if kind in best]


def _articles_to_read(anchors: list[tuple[str, str, Snapshot, str]], news: dict[str, NewsItem], sitemap: list[SitemapEntry], site_host: str) -> list[str]:
    """Article pages worth one request each: those the homepage links under a news section (what
    Builderr's own crawler reads), then dated listing items without a time, then the newest
    sitemap articles."""
    done = {_norm_url(n.url) for n in news.values() if n.url and n.method == "article_page_meta"}
    timed = {_norm_url(n.url) for n in news.values() if n.url and "T" in n.published}
    out: list[str] = []

    def add(url: str) -> None:
        key = _norm_url(url)
        if key not in done and key not in {_norm_url(u) for u in out}:
            out.append(url)

    for url, _text, snap, _raw in anchors:
        if len(_segments(snap.final_url)) == 0 and is_article_url(url, site_host):
            add(url)
    for item in sorted(news.values(), key=lambda n: n.date, reverse=True):
        if item.url and _norm_url(item.url) not in timed and _site(item.url) == site_host:
            add(item.url)
    dated = [e for e in sitemap if is_article_url(e.loc, site_host) and e.lastmod and _news_window(e.lastmod[:10])]
    for entry in sorted(dated, key=lambda e: e.lastmod or "", reverse=True):
        if _norm_url(entry.loc) not in timed:
            add(entry.loc)
    return out


def _brand_and_contacts(rec: CompanyRecord, home: Snapshot, pages: list[tuple[Snapshot, BeautifulSoup, str]], exact: bool, company_label: str | None, contacts_only: bool = False) -> list[str]:
    phones: dict[str, tuple[Snapshot, str]] = {}
    emails: dict[str, tuple[Snapshot, str]] = {}
    brand_candidates: list[tuple[str, Snapshot, str]] = []
    description: tuple[str, Snapshot] | None = None
    for snap, soup, _text in pages:
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            label = anchor.get_text(" ", strip=True)
            if href.lower().startswith("tel:"):
                phone = normalise_phone(href[4:])
                if phone:
                    phones.setdefault(phone, (snap, f"tel link: {href[:40]} {label[:40]}"))
            elif href.lower().startswith("mailto:"):
                address = unquote(href[7:].split("?")[0]).strip().lower()
                if EMAIL.match(address) and not address.endswith((".png", ".jpg")):
                    emails.setdefault(address, (snap, f"mailto link: {address}"))
        if snap is home and not contacts_only:
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
            if types & ORG_TYPES and _ld_org_is_ours(item, rec):
                if item.get("telephone"):
                    phone = normalise_phone(str(item["telephone"]))
                    if phone:
                        phones.setdefault(phone, (snap, f'JSON-LD telephone: {item["telephone"]}'))
                if item.get("email"):
                    address = str(item["email"]).replace("mailto:", "").strip().lower()
                    if EMAIL.match(address):
                        emails.setdefault(address, (snap, f"JSON-LD email: {address}"))
                if item.get("name") and snap is home and not contacts_only:
                    brand_candidates.insert(0, (str(item["name"]).strip(), snap, f'JSON-LD {sorted(types)[0]} name = "{item["name"]}"'))
    brands: list[str] = []
    if not contacts_only:
        for name, snap, span in brand_candidates[:1]:
            clean = re.split(r"\s[|\-–—:]\s", name)[0].strip()
            if 2 <= len(clean) <= 80:
                ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="html_metadata", url=snap.final_url)
                rec.add_claim(family="public_brand", field_name="public_brand_name", value=clean, evidence_ids=[ev], confidence=0.85 if exact else 0.7, method="html_metadata", relationship=company_label, label="Public brand")
        for name, _snap, _span in brand_candidates[:3]:
            clean = re.split(r"\s[|\-–—:]\s", name)[0].strip()
            if 2 <= len(clean) <= 80 and clean not in brands:
                brands.append(clean)
        if description:
            text, snap = description
            ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'meta description = "{text[:300]}"', method="html_metadata", url=snap.final_url)
            rec.add_claim(family="public_brand", field_name="self_description", value=text[:600], evidence_ids=[ev], confidence=0.9, method="html_metadata", relationship=company_label, label="Self-description (company website)")
        has_brand = bool(rec.claims_for("public_brand"))
        rec.set_state("public_brand", "available" if has_brand else "not_available", "brand and description read from the verified website" if has_brand else "the verified website carries no usable brand name or description")
    existing_phones = {c["value"] for c in rec.claims if c["field"] == "website_phone"}
    existing_emails = {c["value"] for c in rec.claims if c["field"] == "website_email"}
    for phone, (snap, span) in list(phones.items())[: max(0, 4 - len(existing_phones))]:
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="tel_link", url=snap.final_url)
        rec.add_claim(family="contact_channels", field_name="website_phone", value=phone, key=phone.replace(" ", ""), evidence_ids=[ev], confidence=0.9, method="tel_link", relationship=company_label, label="Phone (website)")
    for address, (snap, span) in list(emails.items())[: max(0, 4 - len(existing_emails))]:
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="mailto_link", url=snap.final_url)
        rec.add_claim(family="contact_channels", field_name="website_email", value=address, evidence_ids=[ev], confidence=0.9, method="mailto_link", relationship=company_label, label="E-mail (website)")
    if rec.claims_for("contact_channels"):
        rec.set_state("contact_channels", "available", "contact channels found")
    elif not contacts_only:
        rec.set_state("contact_channels", "not_available", "no phone or e-mail in the register or on the verified website")
    return brands


def _publish_profiles(rec: CompanyRecord, profiles: list[Found], exact: bool, company_label: str | None, pages: list[tuple[Snapshot, BeautifulSoup, str]]) -> None:
    for found in profiles:
        ev = rec.evidence_from_snapshot(found.snap, source_class="company_owned_website", span=found.span, method=f"profile_link_on_verified_site:{found.via}", url=found.snap.final_url)
        rec.add_claim(
            family="company_profiles",
            field_name="social_profile",
            value={"platform": found.platform, "url": found.url, "handle": found.handle},
            key=[found.platform, found.url.lower()],
            evidence_ids=[ev],
            confidence=(0.92 if exact else 0.8) if found.via != "script_data" else (0.85 if exact else 0.75),
            method="outbound_link_from_verified_site",
            relationship=company_label,
            label=f"{found.platform.title() if found.platform != 'x' else 'X'} profile",
        )
    if profiles:
        rec.set_state("company_profiles", "available", "company-owned profiles linked from the verified website")
    else:
        rec.set_state("company_profiles", "not_available", "the verified website links to no profile whose account name matches the company", checked_sources=[p[0].final_url for p in pages])


def _publish_hiring(rec: CompanyRecord, careers: list[tuple[str, Snapshot, str, str]], roles: list[tuple[Role, Snapshot]], company_label: str | None) -> None:
    seen: set[str] = set()
    for url, snap, span, method in careers:
        key = _norm_url(url)
        if key in seen or len(seen) >= 3:
            continue
        seen.add(key)
        ev = rec.evidence_from_snapshot(snap, source_class="company_ats_page" if ats_of(snap.final_url) else "company_owned_website", span=span, method=method, url=snap.final_url)
        rec.add_claim(family="hiring", field_name="careers_page", value=url, evidence_ids=[ev], confidence=0.88, method=method, relationship=company_label, label="Careers page")
        rec.context.setdefault("career_pages", []).append(url)
    existing = {_norm_url(c["value"].get("url") or "") for c in rec.claims if c["field"] == "job_posting" and isinstance(c["value"], dict)}
    published = 0
    for role, snap in roles:
        key = _norm_url(role.url)
        if published >= MAX_JOBS or (key in existing and role.url != snap.final_url):
            continue
        existing.add(key)
        value = {"title": role.title[:300], "url": role.url, "published": role.published, "expires": role.expires, "location": role.location, "source": role.source}
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website" if not ats_of(snap.final_url) else "company_ats_page", span=role.span, method=role.method, url=snap.final_url)
        rec.add_claim(
            family="hiring",
            field_name="job_posting",
            value={k: v for k, v in value.items() if v not in (None, "")},
            key=["job", key if role.url != snap.final_url else f"{key}|{role.title.lower()}"],
            evidence_ids=[ev],
            confidence=0.9,
            method=role.method,
            effective_date=(role.published or "")[:10] or None,
            relationship=company_label,
            label="Job posting",
        )
        published += 1
    if seen or published:
        rec.set_state("hiring", "available", f"careers page{'s' if len(seen) > 1 else ''} and {published} open role(s) on the verified website" if published else "careers page linked from the verified website")


def _publish_news(rec: CompanyRecord, items: list[NewsItem], company_label: str | None) -> None:
    items = sorted(items, key=lambda i: (i.date, i.published), reverse=True)[:MAX_NEWS]
    for item in items:
        ev = rec.evidence_from_snapshot(item.snap, source_class=item.source_class, span=item.span, method=item.method, url=item.snap.final_url)
        value = {"title": item.title[:300], "url": item.url, "published": item.published, "date": item.date, "source": item.source}
        rec.add_claim(
            family="public_activity",
            field_name="company_news",
            value={k: v for k, v in value.items() if v},
            key=["news", _norm_url(item.url) if item.url else f"{item.title.lower()}|{item.date}"],
            evidence_ids=[ev],
            confidence=0.92 if item.method in ("article_page_meta", "wordpress_posts_api", "rss_atom_feed", "news_sitemap", "json_ld_article") else 0.88,
            method=item.method,
            effective_date=item.date,
            relationship=company_label,
            label="Company news",
        )
    if items:
        rec.set_state("public_activity", "available", f"{len(items)} dated item(s) on the company's own website")
