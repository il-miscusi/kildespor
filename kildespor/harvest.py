"""Read facts from a verified company website, one claim per fact.

``website._publish_site`` calls ``_extract_site_facts`` once a site's identity
holds. From the pages the identity check already fetched, plus a small page
plan, it publishes:

* social profiles: every company-owned profile the site links (anchors,
  ``data-href``, embed iframes, JSON-LD ``sameAs``, profile URLs in script
  data) on Facebook, Instagram, LinkedIn, YouTube, X, TikTok, Pinterest,
  Vimeo, Threads and GitHub, in one canonical form, bound to the company by
  name or domain;
* hiring: one ``job_posting`` claim per single open ad by this company. The
  careers page and the applicant-tracking list it links are where ads are
  found, never claims themselves; each listed role link has its own ad page
  read, and the ad must be open and name this company (or no other employer
  on a list that is not a group's);
* dated news: RSS/Atom, the WordPress post API, sitemaps, news listings and
  article pages, one ``company_news`` claim per item from the last 24 months;
* brand, self-description, phones and e-mail addresses.

Every claim's evidence points at the page that shows the fact: the linking
page for a profile, the ad page (and the list that linked it) for a role, the
article (or the feed, sitemap or listing that dates it) for a news item.

Page plan: robots.txt is already cached by the fetcher, so its ``Sitemap:``
lines cost nothing, and one sitemap read names the contact, careers and news
pages even on sites that draw their menus with JavaScript. The harvest spends
at most ``HARVEST_REQUESTS`` requests per verified site (env
``KILDESPOR_HARVEST_REQUESTS``) in this order: sitemap, careers page and its
ATS listing, news feed or listing, a contact page, then up to three article
pages for their own published datetime. Ad pages are read last, on their own
allowance of ``MAX_AD_FETCHES`` requests (env ``KILDESPOR_AD_FETCHES``, default
10); a role link left unread is not published.
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

from . import newsgate
from .http import RequestBudgetExhausted, RobotsDisallowed, Snapshot
from .model import CompanyRecord
from .website import (
    CREDIT_WORDS,
    EMAIL,
    GENERIC_NAME_WORDS,
    SiteVisit,
    _ld_employer_is_ours,
    _ld_numbers,
    _ld_org_is_ours,
    _name_matches,
    _names_of,
    _types,
    json_ld_items,
    name_tokens,
    normalise_phone,
    parse,
    registered_domain,
    safe_link,
    strip_accents,
    utc_today,
    valid_orgnr,
)

try:  # Norwegian sites publish in local time; feeds and post APIs give UTC.
    from zoneinfo import ZoneInfo

    OSLO: Any = ZoneInfo("Europe/Oslo")
except Exception:  # pragma: no cover - no tz database
    OSLO = None

HARVEST_REQUESTS = int(os.environ.get("KILDESPOR_HARVEST_REQUESTS", "12"))
# Ad pages read to confirm role links (on top of HARVEST_REQUESTS, after news and profiles): a
# link that is not read is not published.
MAX_AD_FETCHES = int(os.environ.get("KILDESPOR_AD_FETCHES", "10"))
NEWS_MAX_AGE_DAYS = 730
# One correct item per company is the first goal; ten is the cap (review B, WP-C). Env override for corpus runs.
MAX_NEWS = int(os.environ.get("KILDESPOR_MAX_NEWS", "10"))
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
    today = datetime.now(OSLO).date() if OSLO is not None else _today()
    # A date after today is never a publication date (an event, a deadline, a typo).
    return today - timedelta(days=NEWS_MAX_AGE_DAYS) <= parsed <= today


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
        self.ad_start: int | None = None
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

    def can_read_ad(self) -> bool:
        """Ad pages draw on their own allowance of ``MAX_AD_FETCHES`` requests (redirects
        included), opened on first use so the news and profile pages before them keep theirs."""
        if self.ad_start is None:
            self.ad_start = self.used()
            fetcher = self.visit.fetcher
            if hasattr(fetcher, "raise_company_cap"):
                fetcher.raise_company_cap(max(0, self.left()) + MAX_AD_FETCHES)
        return self.used() - self.ad_start < MAX_AD_FETCHES and self.visit.time_left() > 6.0

    def read(self, url: str, *, ad: bool = False) -> tuple[tuple[Snapshot, BeautifulSoup, str] | None, str]:
        """Like ``page``, but says why nothing came back: "ok", "budget", "blocked" (401/403 or
        robots.txt), "missing" (404/410) or "error". Hiring needs the reason: a list behind a login
        is not an empty list. Ad pages (``ad``) use their own allowance and are not added to the
        site's pages, so they never feed the profile or contact readers."""
        url = url.split("#")[0]
        existing = self.known(url)
        if existing is not None:
            return existing, "ok"
        if not (self.can_read_ad() if ad else self.can()):
            return None, "budget"
        if self.visit.time_left() < 3 or not self.visit.in_section(url):
            return None, "error"
        try:
            snap = self.visit.fetcher.get(url, retries=0, timeout=(5.0, min(10.0 if ad else 12.0, max(3.0, self.visit.time_left()))))
        except RobotsDisallowed:
            return None, "blocked"
        except Exception as exc:  # budget, network: the plan just moves on
            self.rec.error("website", f"harvest fetch skipped: {type(exc).__name__}: {str(exc)[:120]}", url)
            return None, "budget" if isinstance(exc, RequestBudgetExhausted) or "budget" in str(exc) or "cap reached" in str(exc) else "error"
        if snap.status in (401, 403, 407, 451):
            return None, "blocked"
        if not snap.ok:
            return None, "missing" if snap.status in (404, 410) else "error"
        if "html" not in snap.content_type.lower() and not snap.body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            return None, "error"
        soup, _html, text = parse(snap)
        page = (snap, soup, text)
        if not ad:
            self.visit.pages.append(page)
        return page, "ok"

    def raw(self, url: str, headers: dict[str, str] | None = None) -> Snapshot | None:
        # On a site verified as one section of a larger domain, domain-wide sources (root sitemap,
        # WordPress posts API, root feeds) describe the whole domain's owner, not this company.
        in_section = getattr(self.visit, "in_section", None)
        if not self.can() or (in_section is not None and not in_section(url)):
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


# Words a page title or site name uses for the page rather than the company ("Hjem - Fjordbygg",
# "Forside", "Nettbutikk"): dropped from a brand name before it is tokenised.
PAGE_WORDS = {
    "hjem", "hjemmeside", "forside", "forsiden", "startside", "home", "homepage", "front", "frontpage", "page", "velkommen",
    "welcome", "nettbutikk", "nettbutikken", "webshop", "index",
}


def _domain_label(site_url: str) -> str:
    return re.sub(r"[^a-z0-9]", "", registered_domain(urlsplit(site_url if "://" in site_url else f"https://{site_url}").hostname or "").split(".")[0])


def identity_brands(brands: list[str], names: list[str], site_url: str) -> list[str]:
    """The site's brand names (JSON-LD name, og:site_name, <title>) that may name the company in an
    account: page words removed, and only a brand that shares a distinctive word of 4+ characters
    with the legal or subunit names, or with the domain label. A title is often a slogan or a place
    ("Bruktbilforhandler i Mosjøen", "Utleiemegler Oslo"), and its words would let any account that
    contains them pass."""
    label = _domain_label(site_url) if site_url else ""
    words: set[str] = set()
    joined: set[str] = {label} if len(label) >= 4 else set()
    for name in names:
        for fold in (_fold_a, _fold_b):
            ws = [w for w in (re.sub(r"[^a-z0-9]", "", fold(t)) for t in name_tokens(name or "")) if w]
            words |= {w for w in ws if len(w) >= 4}
            if len("".join(ws)) >= 4:
                joined.add("".join(ws))
    out: list[str] = []
    for brand in brands:
        clean = " ".join(w for w in re.findall(r"[^\W_]+", brand or "") if _alnum(w) not in PAGE_WORDS)
        if not clean or clean in out:
            continue
        for fold in (_fold_a, _fold_b):
            ws = [w for w in (re.sub(r"[^a-z0-9]", "", fold(t)) for t in name_tokens(clean)) if w]
            distinct = {w for w in ws if len(w) >= 4 and w not in IDENTITY_STOP and w not in PLACE_WORDS}
            whole = "".join(ws)
            if distinct & words or any(w in j for w in distinct for j in joined) or (
                len(whole) >= 4 and any(whole in j or j in whole for j in joined)
            ):
                out.append(clean)
                break
    return out


def identity_tokens(names: list[str], site_url: str, brands: list[str] | None = None, *, place_tokens: bool = True) -> tuple[set[str], set[str]]:
    """(substring tokens, prefix tokens) an account name for this company may contain or start with.

    ``names`` are the legal and subunit names; ``brands`` are the site's brand names, used only as far
    as ``identity_brands`` admits them. With ``place_tokens=False`` (profiles) a place word (Oslo,
    Trondheim, Norge) is never a token by itself: it names a town account or a local agency as often
    as the company."""
    contains: set[str] = set()
    prefixes: set[str] = set()
    places = set() if place_tokens else PLACE_WORDS
    names = list(names) + (identity_brands(brands, names, site_url) if brands else [])
    for name in names:
        words = name_tokens(name or "")
        for fold in (_fold_a, _fold_b):
            ws = [re.sub(r"[^a-z0-9]", "", fold(w)) for w in words]
            ws = [w for w in ws if w]
            if not ws:
                continue
            joined = "".join(ws)
            if len(joined) >= 4 and joined not in places:
                contains.add(joined)
            core = [w for w in ws if w not in ("og", "and", "the")]
            if core and "".join(core) != joined and len("".join(core)) >= 4 and "".join(core) not in places:
                contains.add("".join(core))
            for w in core:
                if w in IDENTITY_STOP or w in places:
                    continue
                if len(w) >= 5 or (len(w) == 4 and len(core) <= 2):
                    contains.add(w)
            if len(core) >= 3 and len(core[0] + core[1]) >= 6:
                contains.add(core[0] + core[1])
            if len(core) >= 2:
                acronym = "".join(w[0] for w in core)
                if len(acronym) >= 3:
                    prefixes.add(acronym)
    label = _domain_label(site_url) if site_url else ""
    if len(label) >= 4 and label not in places:
        contains.add(label)
    if len(label) >= 3 and label not in places:
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
    """A URL on a host that names another country (career-lithuania.kitron.com: a sister company's
    site abroad). Only the host counts: a news slug naming a country is still the company's news."""
    host = _fold_a(urlsplit(url).hostname or "")
    return any(marker in host and marker not in own_text for marker in FOREIGN_MARKERS)


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


# Words beside a link that say it belongs to someone related, not to the company itself
# (matched on folded text, after the company's own names are removed from it).
RELATION_LABEL = re.compile(
    r"soster(?:selskap|bedrift|firma)|morselskap|datterselskap|konsern|samarbeidspartner|\bpartner|\bkunde(?:r|n|ne)?\b|levert av|"
    r"leverandor|medlem av|member of|sister[- ]?compan|parent[- ]?compan|subsidiar|\bgroup\b|\bgruppen\b|owned by|\beid av\b|\bdel av\b|part of",
    re.I,
)
# What may surround the company's own name in its own account name: legal form, country,
# "official", a number, or a Norwegian place (a regional account).
ALLOWED_AFFIXES = {
    "as", "asa", "ab", "da", "ans", "sa", "ks", "ba", "no", "nor", "norge", "norway", "norden", "nordic", "scandinavia", "official",
    "offisiell", "offisielt", "hq", "go", "the", "team", "tv", "film", "films", "media", "inc", "ltd", "oslo", "bergen", "trondheim",
    "stavanger", "tromso", "kristiansand", "drammen", "fredrikstad", "sandnes", "bodo", "alesund", "haugesund", "tonsberg", "moss",
    "hamar", "lillehammer", "molde", "harstad", "gjovik", "larvik", "halden", "sandefjord", "arendal", "skien", "porsgrunn",
    "kongsberg", "narvik", "steinkjer", "levanger", "namsos", "elverum", "kongsvinger", "honefoss", "asker", "baerum", "sandvika",
    "lillestrom", "jessheim", "ski", "viken", "vestland", "rogaland", "agder", "nordland", "troms", "finnmark", "innlandet",
    "trondelag", "vestfold", "telemark", "ostfold", "akershus", "sunnmore", "romerike", "hordaland", "sorlandet", "vestlandet",
    "ostlandet", "nordnorge", "midtnorge",
}
# The place half of ALLOWED_AFFIXES plus the country words: never an identity token on their own.
PLACE_WORDS = ALLOWED_AFFIXES - {
    "as", "asa", "ab", "da", "ans", "sa", "ks", "ba", "no", "nor", "official", "offisiell", "offisielt", "hq", "go", "the", "team",
    "tv", "film", "films", "media", "inc", "ltd",
}


# Words that name a separate legal entity beside the company's own name (Fjordbygg Eiendom AS,
# Fjordbygg Holding AS): an account "fjordbygg-eiendom" on Fjordbygg AS's site is the sister's.
SISTER_ENTITY_WORDS = (
    "eiendom", "eiendommer", "eiendomsutvikling", "boligutvikling", "holding", "holdings", "invest", "investering",
    "investment", "property", "properties", "kapital", "capital", "finans", "utleie", "drift", "entreprenor", "entreprenoer",
    "bygg", "anlegg", "maskin", "transport", "logistikk", "elektro", "regnskap", "consulting", "konsult", "utvikling",
    "prosjekt", "prosjekter", "shipping", "marine", "energi", "eiendomsforvaltning", "forvaltning", "barnehage", "stiftelse",
)


def names_other_company(account: str, contains: set[str], prefixes: set[str], own_words: set[str]) -> bool:
    """True when the account is the company's name plus a word that names a separate legal entity
    (eiendom, holding, invest, ...) and that word is not in the company's own legal, former, brand
    or domain name."""
    matched = max((t for t in contains if len(t) >= 4 and t in account), key=len, default=None)
    if matched is None:
        matched = max((p for p in prefixes if account.startswith(p)), key=len, default=None)
    if matched is None:
        return False
    rest = account.replace(matched, " ", 1)
    for word in SISTER_ENTITY_WORDS:
        if word in rest and word not in own_words and not any(word in w for w in own_words if len(w) > len(word)):
            return True
    return False


def _related_link(anchor: Any, own_names: list[str]) -> bool:
    """A link labelled as a sister, parent, partner or customer company's ("Vårt søsterselskap")."""
    texts = [anchor.get_text(" ", strip=True), str(anchor.get("title") or ""), str(anchor.get("aria-label") or "")]
    node = anchor
    for _ in range(3):
        node = node.parent
        if node is None or node.name in ("body", "html", "footer", "header", "nav", "main"):
            break
        if len({a.get("href") for a in node.find_all("a", href=True)}) > 3:
            break
        text = node.get_text(" ", strip=True)
        if len(text) > 200:
            break
        texts.append(text)
    joined = " " + _fold_a(" ".join(texts)) + " "
    for name in own_names:
        if len(name) >= 3:
            joined = joined.replace(name, " ")
    return bool(RELATION_LABEL.search(joined))


_CHROME_IDENT = re.compile(r"footer|header|social|sosial|\bsome\b|follow|folg|\bnav|menu|topbar|toolbar", re.I)


def _in_site_chrome(node: Any) -> bool:
    """Inside the site's header, footer, menu or social-icon block (not in body content)."""
    for parent in node.parents:
        name = getattr(parent, "name", None)
        if name in ("footer", "header", "nav"):
            return True
        if name in (None, "[document]", "html", "body"):
            return False
        ident = " ".join(parent.get("class") or []) + " " + str(parent.get("id") or "")
        if _CHROME_IDENT.search(ident):
            return True
    return False


# Phrases beside a block of social icons that say whose icons they are when it is not the company:
# the group the site says it belongs to ("Del av Consto-gruppen", "part of Hexagon"). Bare "group",
# "gruppen", "konsern" and "partner" are not read at this reach: a footer heading such as "Kitron
# Group" or "Consto konsern" over the company's own icons names the company itself.
BLOCK_RELATION = re.compile(
    r"\bdel av\b|\bpart of\b|\bmember of\b|\bmedlem av\b|\beid av\b|\bowned by\b|soster(?:selskap|bedrift|firma)|morselskap|"
    r"datterselskap|sister[- ]?compan|parent[- ]?compan|subsidiar",
    re.I,
)
THEME_CREDIT = re.compile(r"theme by|tema av|template by|wordpress theme", re.I)


def _social_href(href: str) -> bool:
    try:
        host = (urlsplit(href.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in SOCIAL_HOSTS)


def _carries_profile_link(node: Any) -> bool:
    if getattr(node, "name", None) == "a" and _social_href(str(node.get("href") or "")):
        return True
    return hasattr(node, "find_all") and any(_social_href(str(a.get("href") or "")) for a in node.find_all("a", href=True))


def _block_text(parent: Any, came_from: Any) -> str:
    """The parent's own words beside the child we came from: its direct text and the text of the
    children that link to no social platform (a heading, a credit line linking the agency's site).
    A child carrying its own profile link (another icon, an agency's icon with its credit) speaks
    for that link only."""
    parts: list[str] = []
    for child in parent.children:
        if child is came_from:
            continue
        if getattr(child, "name", None) is None:
            if type(child).__name__ == "NavigableString":
                parts.append(str(child).strip())
            continue
        if child.name in ("script", "style", "noscript", "template") or _carries_profile_link(child):
            continue
        parts.append(child.get_text(" ", strip=True))
    return " ".join(p for p in parts if p)


def _icon_block(node: Any) -> tuple[str, str]:
    """What the block of icons a profile link sits in, and a short heading just before that block,
    say about whose icons they are: ("credit", "") for a web agency or theme credit, ("related",
    <name>) for a group the site says it is part of, else ("", "").

    Unlike _credit_link and _related_link this climbs past the footer element and past 3 links, as
    long as the block is still an icon row: at most 200 characters of text and at most one link that
    is not to a social platform (the agency's or the parent's own site). Text that belongs to another
    profile link (an agency's icon in its own credit line) is not read for this one."""
    texts: list[str] = []
    current = node
    for _ in range(6):
        parent = current.parent
        if parent is None or getattr(parent, "name", None) in (None, "[document]", "html", "body", "main"):
            break
        others = {
            str(a.get("href") or "").strip() for a in parent.find_all("a", href=True)
            if not _social_href(str(a.get("href") or "")) and not str(a.get("href") or "").strip().lower().startswith(("#", "mailto:", "tel:", "javascript:"))
        }
        if len(others) > 1 or len(parent.get_text(" ", strip=True)) > 200:
            heading = current.find_previous_sibling()
            if heading is not None and not _carries_profile_link(heading) and len(heading.find_all("a", href=True)) <= 1:
                heading_text = heading.get_text(" ", strip=True)
                if len(heading_text) <= 80:
                    texts.append(heading_text)
            break
        texts.append(_block_text(parent, current))
        current = parent
    raw = " ".join(t for t in texts if t)
    if CREDIT_WORDS.search(raw) or THEME_CREDIT.search(raw):
        return "credit", ""
    folded = " " + _fold_a(raw) + " "
    match = BLOCK_RELATION.search(folded)
    if match:
        rest = re.split(r"[.,;:|•·()\[\]!?]", folded[match.end():].lstrip(" :-–—"))[0]
        return "related", " ".join(rest.split()[:5])
    return "", ""


def _names_company(node: Any, contains: set[str]) -> bool:
    """The link's own text, title or aria-label (or its icon's alt text) names the company."""
    parts = [node.get_text(" ", strip=True), str(node.get("title") or ""), str(node.get("aria-label") or "")]
    parts += [f'{img.get("alt") or ""} {img.get("title") or ""}' for img in node.find_all("img")]
    label = _alnum(" ".join(parts))
    return any(len(t) >= 4 and t in label for t in contains)


def _admin_link(url: str) -> bool:
    """A LinkedIn page's admin URL (/company/<id>/admin/...): whoever edits the site manages the page."""
    return bool(re.search(r"linkedin\.[a-z.]+/company/[^/?#]+/admin(?:/|$|\?)", url, re.I))


def _parent_names(rec: CompanyRecord) -> list[str]:
    """The Brreg parent and ultimate parent names recorded for this company."""
    return [
        str(c["value"]["name"]) for c in rec.claims
        if c.get("field") == "group_relationship" and isinstance(c.get("value"), dict)
        and c["value"].get("relationship") in ("parent", "ultimate_parent") and c["value"].get("name")
    ]


def parent_account(account: str, contains: set[str], others: list[str]) -> bool:
    """The account is a related company's (the Brreg parent, or the group a footer says the site is
    part of), not this one's: it resembles that name, and this company's names carry a token the
    other name lacks (its own legal word, verified brand or domain label) that the account does not.
    A company named like its parent and nothing more (Consto AS under Consto Holding AS) has no such
    token, so its group-named accounts (Constokonsern) stay; Consto Anlegg Nord AS's site does not
    publish linkedin.com/company/consto-as."""
    for other in others:
        theirs, _ = identity_tokens([other], "", place_tokens=False)
        if not theirs or not resembles(account, theirs, set()):
            continue
        marks = {t for t in contains if len(t) >= 4 and not any(t in o for o in theirs)}
        if marks and not any(m in account for m in marks):
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


def profiles_on_page(
    snap: Snapshot,
    soup: BeautifulSoup,
    html: str,
    contains: set[str],
    prefixes: set[str],
    rec: CompanyRecord | None = None,
    own_text: str = "",
    own_names: list[str] | None = None,
    own_words: set[str] | None = None,
    parent_names: list[str] | None = None,
) -> list[Found]:
    """Company profiles a page of the company's own site links, in page order.

    A named account must resemble the company, carry no other company's name, and not be the
    parent group's (``parent_names``: Brreg's parent and ultimate parent, read from ``rec`` when not
    given). An account known only by an id is compared with nothing, so it needs other evidence: a
    label naming the company, a LinkedIn admin URL, the sameAs of the site's own Organization, or,
    for a link in the site's header/footer/social block, a named profile that passed on the same
    page. Sitting in the footer or the menu is not evidence by itself. A link labelled as a sister,
    parent, partner or customer company's is never the company's, and an icon block credited to a
    web agency or theme, or headed by a group the site is part of, publishes no id-only account and
    no account that names only that group."""
    base = snap.final_url
    own_names = own_names or []
    own_words = own_words or set()
    if parent_names is None:
        parent_names = _parent_names(rec) if rec is not None else []
    taken: list[tuple[Found, bool]] = []  # (profile, kept only if a named profile passed on this page)

    def take(raw: str, via: str, *, ids: str = "none", span: str | None = None, related: str = "") -> None:
        """``ids`` says how an id-only account may pass from here: "ok" (the label names the company,
        or the site's own Organization sameAs), "sibling" (site chrome: if a named profile passes on
        this page), "none" (only with a LinkedIn admin URL) or "never" (a credited or group block)."""
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
        item = Found(platform, canonical, handle, snap, span or raw.strip()[:300], via)
        account = account_name(handle)
        if id_like(handle) or not account:
            if ids == "ok" or (ids != "never" and _admin_link(url)):
                taken.append((item, False))
            elif ids == "sibling":
                taken.append((item, True))
            return
        if not owned_profile(handle, contains, prefixes, own_text=own_text):
            return
        if names_other_company(account, contains, prefixes, own_words):
            return
        if parent_account(account, contains, parent_names + ([related] if related else [])):
            return
        taken.append((item, False))

    def route(node: Any) -> tuple[str, str]:
        kind, related = _icon_block(node)
        if kind:
            return "never", related
        if _names_company(node, contains):
            return "ok", ""
        return ("sibling" if _in_site_chrome(node) else "none"), ""

    for anchor in soup.find_all("a", href=True):
        if not _social_href(str(anchor["href"])) or _credit_link(anchor) or _related_link(anchor, own_names):
            continue
        ids, related = route(anchor)
        take(anchor["href"], "anchor", ids=ids, related=related)
    for node in soup.find_all(attrs={"data-href": True}):
        if not _social_href(str(node.get("data-href") or "")) or _related_link(node, own_names):
            continue
        ids, related = route(node)
        take(str(node.get("data-href") or ""), "data_href", ids=ids, related=related)
    for frame in soup.find_all("iframe", src=True):
        if _related_link(frame, own_names):
            continue
        src = str(frame.get("src") or "")
        try:
            params = parse_qs(urlsplit(urljoin(base, src)).query)
        except ValueError:
            continue
        ids, related = route(frame) if any(params.get(k) for k in ("href", "u", "url")) else ("none", "")
        for key in ("href", "u", "url"):
            for value in params.get(key, [])[:1]:
                take(value, "embed_iframe", span=src[:300], ids=ids, related=related)
    for item in json_ld_items(soup):
        if _types(item) & {"Person", "Product", "Event", "Offer", "JobPosting", "Review"}:
            continue
        if rec is not None and not _ld_org_is_ours(item, rec):
            continue
        same_as = item.get("sameAs")
        for url in same_as if isinstance(same_as, list) else [same_as] if same_as else []:
            if isinstance(url, str):
                take(url, "json_ld_sameAs", ids="ok" if _types(item) & ORG_TYPES else "none")
    for script in soup.find_all("script"):
        if "ld+json" in (script.get("type") or "").lower():
            continue
        text = script.string or script.get_text() or ""
        for m in _SCRIPT_PROFILE_RE.finditer(text):
            take(m.group(0).replace("\\/", "/"), "script_data", ids="none", span=m.group(0)[:300])
    named = any(account_name(f.handle) and not id_like(f.handle) for f, _ in taken)
    return [f for f, needs_named in taken if named or not needs_named]


# ----------------------------------------------------------------------------------------------
# Hiring

CAREER_SEGMENT = re.compile(
    r"^(?:karrierer?|careers?|jobb(?:er)?|jobs|(?:ledige?-?)?stillinger|ledige-jobber|ledig-stilling|vacanc(?:y|ies)|join-?us|"
    r"bli-med(?:-oss|-p[aå]a?-laget|-i-(?:teamet|laget))?|bli-en-av-oss|jobbe?-som-[\w-]+|work-(?:with|for|at)-[\w-]+|rekruttering|recruitment|open-positions|jobbmuligheter|"
    r"(?:kom-og-)?jobbe?-(?:hos|i|med|for)-[\w-]+|karriere-og-jobb|jobb-og-karriere|job-opportunities|career-opportunities|"
    r"arbeid-hos-oss|vi-ansetter|we-are-hiring|stilling(?:er)?-ledig|ledige?-stillinger-hos-[\w-]+|karriere-(?:i|hos)-[\w-]+|(?:ledige-)?stillinger-(?:i|hos)-[\w-]+|jobb-her|jobba-(?:hos-oss|har)|arbeta-hos-oss)(?:\.html?|\.php|\.aspx)?$",
    re.I,
)
CAREER_TEXT = re.compile(
    r"^(?:(?:job\s*)?(?:careers?|karriere|jobs?|jobb|stillinger|vacancies|job\s+vacancies|open\s+positions)"
    r"|ledige\s+stillinger|ledige\s+jobber|ledig\s+stilling|stilling(?:er)?\s+ledig|jobb(?:e)?\s+(?:hos|i|med|for)\s+\S+(?:\s+\S+)?"
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
    "personio.com": "personio", "ashbyhq.com": "ashby", "breezy.hr": "breezy",
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


# Open applications and pages about working here sit under the careers path but are not vacancies.
NOT_A_ROLE = re.compile(r"spontan|[aå]pen[- ]s[øo]knad|apen-soknad|open[- ]application|general[- ]applications?|generell[- ]s[øo]knad|karriere|career|jobbe-i-et|om-oss", re.I)


def _role_url(url: str) -> bool:
    """The URL's path or query (never its host: 02365.no is a taxi company, not an ad id) looks like one ad."""
    parts = urlsplit(url)
    return bool(ROLE_URL.search(parts.path + ("?" + parts.query if parts.query else "")))


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


TEXT_LINK_EXCLUDED = re.compile(r"/(?:blog|blogg|posts|news|nyheter|stories|events|kategori|category|tag|produkt|produkter|product|products|shop|nettbutikk|butikk)(?:/|$)", re.I)
_DIRECTORY_OR_SOCIAL = re.compile(r"(?:^|\.)(?:facebook|instagram|linkedin|twitter|x|youtube|tiktok|google|proff|gulesider|1881|purehelp|brreg)\.", re.I)


def is_careers_link(url: str, text: str, site_host: str, tokens: set[str] | None = None) -> int | None:
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
        if TEXT_LINK_EXCLUDED.search(parts.path):
            return None  # a blog post, news item or shop page is never the careers page
        segs = _segments(url)
        sub = host.removesuffix(site_host).rstrip(".").removeprefix("www.").split(".")[0] if host != site_host else ""
        if sub and CAREER_HOST_LABEL.match(sub) and len(segs) <= 2:
            return 0
        if segs and CAREER_SEGMENT.match(segs[-1]) and len(segs) <= 3:
            return 0
        if len(segs) >= 2 and CAREER_SEGMENT.match(segs[-2]) and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?|ledige(?:-stillinger)?|stillinger|jobs|vacancies|openings", segs[-1], re.I):
            return 0
        if 0 < len(text) <= 40 and CAREER_TEXT.match(text) and not TEXT_LINK_EXCLUDED.search(parts.path):
            return 2  # the page itself must then read as a careers page
        return None
    ats = ats_of(url)
    if ats is None and tokens and 0 < len(text) <= 40 and CAREER_TEXT.match(text) and not _DIRECTORY_OR_SOCIAL.search(host):
        # A recruiter's page for this company ("Karriere" -> talangpunkten.se/career/work-system/):
        # the link says careers and the URL names the company.
        words = [_alnum(w) for w in re.split(r"[./\-_]+", f"{host} {unquote(parts.path)}".replace(" ", "/")) if w]
        joined = _alnum(unquote(parts.path))
        if any(len(t) >= 4 and (t in joined or t in words) for t in tokens):
            return 1
        return None
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
    employer: str | None = None  # the employer the listing's structured data names, if any


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


def _title_key(title: Any) -> str:
    return " ".join(re.sub(r"[^\w ]", " ", _clean(title).lower()).split())


def _rejected_title(title: str, rejected: set[str]) -> bool:
    """The title is, or starts with, the title of a posting already refused (expired or another
    employer's): "Snekker – Bergen" after "Snekker" was refused."""
    key = _title_key(title)
    for entry in rejected:
        if entry.startswith("title:"):
            refused = entry[6:]
            if refused and (key == refused or key.startswith(refused + " ")):
                return True
    return False


def roles_on_page(
    snap: Snapshot,
    soup: BeautifulSoup,
    rec: CompanyRecord,
    *,
    own_site: bool,
    tenant_ok: bool,
    site_wide: set[str] | None = None,
    rejected: set[str] | None = None,
) -> list[Role]:
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
    # Postings refused here (another employer, expired) stay refused in every later pass, and a
    # page whose structured data names another employer lends no anchor its authority.
    rejected = rejected if rejected is not None else set()
    contrary = False
    for item in json_ld_items(soup):
        if "JobPosting" not in _types(item) or not item.get("title"):
            continue
        employer = item.get("hiringOrganization")
        expires = str(item.get("validThrough") or "")[:10] or None
        item_url = urljoin(page_url, str(item.get("url") or "")) if item.get("url") else None
        refused = bool(employer and not _ld_employer_is_ours(item, rec)) or bool(expires and expires < today)
        if employer and not _ld_employer_is_ours(item, rec):
            contrary = True
        if refused:
            if item_url and _norm_url(item_url) != _norm_url(page_url):
                rejected.add(_norm_url(item_url))
            rejected.add("title:" + _title_key(item["title"]))
            continue
        if not employer and not (own_site or tenant_ok):
            continue
        title = _clean(item["title"])[:200]
        url = item_url or page_url
        if not safe_link(url):
            url = page_url
        key = _norm_url(url) + "|" + title.lower()
        if key in seen:
            continue
        seen.add(key)
        posted = stamp(item.get("datePosted"))
        named = employer.get("legalName") or employer.get("name") if isinstance(employer, dict) else employer
        roles.append(Role(title, url, posted[0] if posted else None, expires, _ld_location(item), "json_ld_jobposting", f'JSON-LD JobPosting title = "{title}"', "json_ld_jobposting", str(named)[:200] if named else None))
    if (not own_site and not tenant_ok) or contrary:
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
        elif not _role_url(url) or NOT_A_ROLE_PATH.search(urlsplit(url).path) or NOT_A_ROLE.search(_segments(url)[-1] if _segments(url) else ""):
            continue
        title = _role_title(anchor)
        if title and NOT_A_ROLE.search(title):
            continue  # an open application or a page about working here, not a vacancy
        if not title or ROLE_NAV_TEXT.match(title) or ROLE_BOILERPLATE.search(title) or ROLE_SECTION.match(title) or re.fullmatch(r"[\d.\s/:-]+", title):
            continue
        key = _norm_url(url)
        if key in rejected or _rejected_title(title, rejected):
            continue  # refused by the structured data in this or an earlier pass
        if key in seen or title.lower() in {r.title.lower() for r in links} or (site_wide and key in site_wide):
            continue  # a link the homepage also carries is navigation, not a listed role
        seen.add(key)
        card = _card_text(anchor)
        expires = None
        deadline = DEADLINE.search(card)
        if deadline:
            expires = text_date(deadline.group(1))
            if expires and expires < today:
                rejected.add(key)
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
# Ad pages: a role link is a job only once its own page has been read
#
# A listing says that a link exists, not that it is one open vacancy posted by this company. The
# ad page says the rest: whether it is an ad at all (not an open application, a login or a search),
# whether it is still open, and who the employer is. A group's list carries its subsidiaries' ads,
# and only the ad page's "Om <unit>" block, employer field or organisation number tells them apart.

# Words on an ad page that say the vacancy is filled, closed or expired.
AD_CLOSED = re.compile(
    r"stillingen\s+er\s+(?:nå\s+)?besatt|(?:søknads)?fristen\s+er\s+(?:utløpt|gått\s+ut|passert)|annonsen\s+er\s+(?:utløpt|deaktivert|ikke\s+lenger\s+aktiv)|"
    r"stillingen\s+er\s+ikke\s+lenger\s+(?:ledig|aktiv|tilgjengelig)|no\s+longer\s+accepting(?:\s+applications)?|"
    r"(?:this|the)\s+(?:job|position|vacancy)\s+(?:has\s+been\s+filled|has\s+expired|is\s+(?:no\s+longer\s+available|closed|filled))",
    re.I,
)
# A status badge on its own ("Besatt" on finn).
AD_CLOSED_BADGE = re.compile(r"^(?:besatt|utløpt|expired|filled)$", re.I)
# Pages under a careers path that are not one vacancy: open applications, talent pools, CV registration.
AD_NOT_A_ROLE = re.compile(
    r"spontan|(?:[aå]pen|open)[- ]s[øo]knad|apen-soknad|s[øo]knadsportal|talent\s?bank|talent\s?pool|registrer\s+(?:din\s+)?cv|"
    r"open[- ]application|general[- ]applications?|generell[- ]s[øo]knad",
    re.I,
)
AD_LOGIN_TEXT = re.compile(r"^(?:logg?\s+inn|log\s*in|sign\s+(?:in|up)|registrer(?:\s+deg)?|register|opprett\s+(?:bruker|konto|profil)|create\s+(?:an\s+)?account|min\s+side)\b", re.I)
AD_LOGIN_PATH = re.compile(r"/(?:login|logon|log-in|signin|sign-in|sign_in|signup|sign-up|register|registrer|profile|profil|account|konto|auth)(?:/|$|\.|\?)", re.I)
AD_SEARCH_PATH = re.compile(r"/(?:search|sok|søk|soek|sokeresultat|search-results?)(?:/|$|\.html?|\.aspx|\.php)", re.I)
AD_SEARCH_QUERY = re.compile(r"(?:^|&)(?:q|query|search|sok|søk|keyword|keywords|searchterm)=", re.I)
FINN_STALE_DAYS = 90
_ORGNR_LABELLED = re.compile(r"(?:org(?:anisasjons)?\.?\s*-?\s*n(?:umme)?r\.?|organisasjonsnummer|org\.?\s*no\.?)\s*:?\s*(\d{3}[\s.]?\d{3}[\s.]?\d{3})(?!\d)", re.I)
# "Arbeidsgiver: Leirvik AS", or a label whose value sits in the next element (dt/dd, th/td).
_EMPLOYER_LABEL = re.compile(r"^(?:arbeidsgiver|arbeidsgjevar|employer|virksomhet|verksemd)(?:\s*:\s*(.+)|\s*:?)$", re.I)
_BLOCK_TAGS = ["div", "p", "li", "dd", "dt", "ul", "ol", "dl", "table", "tr", "section", "h2", "h3", "h4"]
_ABOUT_HEADING = re.compile(r"^(?:om|about)\s+(.{2,80}?)\s*[:.]?$", re.I)
# "Om oss", "Om stillingen", "About the role": a section of the ad, not an employer's name.
_ABOUT_GENERIC = re.compile(
    r"^(?:oss|us|stillingen|stillinga|jobben|rollen|role|the\s+role|the\s+position|the\s+job|position|deg|you|selskapet|bedriften|firmaet|"
    r"arbeidsgiver(?:en)?|arbeidsgjevar(?:en)?|the\s+company|company|the\s+employer|avdelingen|teamet|team|kontoret|prosjektet|"
    r"arbeidsstedet|arbeidsplassen|søknaden|søknadsprosessen|prosessen|annonsen|tilbudet|vilkår|personvern|informasjonskapsler|cookies)\b",
    re.I,
)
_LEGAL_FORM_WORD = re.compile(r"\b(?:as|asa|sa|ba|ans|da|ks|iks|hf|sf|nuf|ab|gmbh|ltd|inc)\b\.?$", re.I)
_CONTENT_CHROME = re.compile(r"footer|site-?header|\bnav|menu|cookie|breadcrumb", re.I)


def ad_url_ok(url: str) -> bool:
    """``url`` can be one ad: never a search, a listing landing, a login or a profile page.

    finn is held to its ad paths (``/job/ad/<id>``, ``finnkode=<id>``) and arbeidsplassen.nav.no to
    ``/stillinger/stilling/<id>``; on an applicant-tracking host the vendor's landing pages
    (``/index.php``, ``main?site=``) are refused as well. Checked on the link and again on the
    final URL after redirects."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    path = unquote(parts.path or "/")
    if host == "arbeidsplassen.nav.no" or host.endswith(".arbeidsplassen.nav.no"):
        return bool(re.fullmatch(r"/stillinger/stilling/[\w-]{6,}/?", path))
    ats = ats_of(url)
    if ats is not None and ats[0] == "finn":
        if AD_SEARCH_PATH.search(path) or "/search" in path.lower():
            return False
        return bool(re.fullmatch(r"/job/ad/\d+/?", path) or re.search(r"(?:^|&)finnkode=\d+", parts.query))
    if AD_SEARCH_PATH.search(path) or AD_SEARCH_QUERY.search(parts.query) or AD_LOGIN_PATH.search(path):
        return False
    if ats is not None and (_ATS_NOT_PUBLIC.search(path) or re.search(r"/index\.php$|/main$", path, re.I)):
        return False
    return True


@dataclass
class AdPage:
    """What one ad page says about itself."""

    refused: str | None  # why it is not one open ad, or None
    title: str | None
    employers: list[str]  # employer names the page states (JSON-LD, employer field, "Om X")
    numbers: set[str]  # organisation numbers the page states in its own content
    expires: str | None
    published: str | None


def _content_soup(soup: BeautifulSoup) -> BeautifulSoup:
    """The page without its header, footer, menus and cookie banner: a site-wide footer's
    organisation number or "Om AF Gruppen" block is the site owner's, not the ad's employer."""
    copy = BeautifulSoup(str(soup), "lxml")
    for tag in copy(["script", "style", "noscript", "template", "svg", "footer", "nav"]):
        tag.decompose()
    for tag in copy.find_all("header"):
        if tag.find_parent(["article", "main"]) is None:
            tag.decompose()  # the site's header; an article's own header holds the ad's title
    chrome = [
        tag for tag in copy.find_all(True)
        if tag.name not in ("html", "body", "main", "article")
        and _CONTENT_CHROME.search(" ".join(tag.get("class") or []) + " " + str(tag.get("id") or ""))
    ]
    for tag in chrome:
        if not tag.decomposed:
            tag.decompose()
    return copy


def _org_like(name: str, own_names: list[str]) -> bool:
    """An "Om X" heading names an employer when X carries a legal form, is one of our names, or
    shares our names' first word (a group brand's unit: "AF Bygg Øst" beside "AF Gruppen").
    "Om Haugesund" (the town) or "Om produktet" names no employer."""
    words = name_tokens(name)
    if not words:
        return False
    if _LEGAL_FORM_WORD.search(name.strip()):
        return True
    firsts = {name_tokens(n)[0] for n in own_names if name_tokens(n)}
    return words[0] in firsts


def read_ad_page(snap: Snapshot, soup: BeautifulSoup, text: str, own_names: list[str]) -> AdPage:
    """Read one ad page: is it an open ad, and which employer does it name.

    Refused: an error, login, registration or search page (also after redirects); an open
    application or talent pool; a filled or expired ad (status words, a past ``validThrough`` or
    deadline, a finn ad with "Snarest" last edited more than 90 days ago)."""
    today = utc_today()
    h1 = soup.find("h1")
    heading = h1.get_text(" ", strip=True) if h1 else ""
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = heading or re.split(r"\s[|\-–—]\s", page_title)[0].strip() or None
    employers: list[str] = []
    numbers: set[str] = set()
    expires: str | None = None
    published: str | None = None

    def result(refused: str | None) -> AdPage:
        return AdPage(refused, title, list(dict.fromkeys(employers)), numbers, expires, published)

    if not ad_url_ok(snap.final_url) or not ad_url_ok(snap.requested_url):
        return result(f"not an ad URL after redirects: {snap.final_url[:160]}")
    if ERROR_PAGE.search(f"{page_title} {heading}"[:300]):
        return result("error page")
    if title and AD_NOT_A_ROLE.search(title):
        return result(f"open application, not a vacancy: {title[:80]}")
    if AD_LOGIN_TEXT.match(heading or page_title) or (not heading and soup.find("input", attrs={"type": "password"}) is not None):
        return result("login or registration page")
    for item in json_ld_items(soup):
        if "JobPosting" not in _types(item):
            continue
        valid = str(item.get("validThrough") or "")[:10]
        if valid and valid < today:
            return result(f"validThrough {valid} has passed")
        expires = expires or valid or None
        posted = stamp(item.get("datePosted"))
        published = published or (posted[0] if posted else None)
        employer = item.get("hiringOrganization")
        if isinstance(employer, str) and employer.strip():
            employers.append(employer.strip())
        elif isinstance(employer, dict):
            if employer.get("name") or employer.get("legalName"):
                employers.append(str(employer.get("legalName") or employer.get("name")).strip())
            numbers |= set(_ld_numbers(employer))
    content = _content_soup(soup)
    content_text = re.sub(r"\s+", " ", content.get_text(" ", strip=True))
    closed = AD_CLOSED.search(content_text)
    if closed:
        return result(f"filled or closed: \"{closed.group(0)}\"")
    for node in content.find_all(["span", "div", "p", "strong", "b", "li", "dd", "em", "small"]):
        own = node.get_text(" ", strip=True)
        if len(own) <= 20 and AD_CLOSED_BADGE.match(own):
            return result(f"filled or closed: \"{own}\"")
    deadline = DEADLINE.search(content_text)
    # "Søknadsfrist: Snarest" has no date; the next date on the page (published, last edited) is not one.
    rolling = bool(deadline and re.match(r"\s*(?:snarest|fortl[øo]pende|l[øo]pende|asap|as\s+soon\s+as\s+possible)", deadline.group(1), re.I))
    if deadline and not rolling:
        day = text_date(deadline.group(1))
        if day and day < today:
            return result(f"application deadline {day} has passed")
        if day:
            expires = expires or day
    if (ats_of(snap.final_url) or ("",))[0] == "finn" and rolling and re.match(r"\s*snarest", deadline.group(1), re.I):
        edited = re.search(r"sist\s+endret\s*:?\s*(.{0,40})", content_text, re.I)
        day = text_date(edited.group(1)) if edited else None
        if day and day < (date.fromisoformat(today) - timedelta(days=FINN_STALE_DAYS)).isoformat():
            return result(f"finn ad with deadline \"snarest\", last edited {day}")
    for m in _ORGNR_LABELLED.finditer(content_text):
        number = re.sub(r"\D", "", m.group(1))
        if valid_orgnr(number):
            numbers.add(number)
    for node in content.find_all(["dt", "th", "span", "div", "p", "strong", "b", "h2", "h3", "h4", "li", "label"]):
        if node.find(_BLOCK_TAGS) is not None:
            continue  # a container: its text runs several fields together
        own = node.get_text(" ", strip=True)
        if not own or len(own) > 100:
            continue
        label = _EMPLOYER_LABEL.match(own)
        if not label:
            continue
        name = (label.group(1) or "").strip(" :")
        if not name:
            following = node.find_next_sibling()
            name = following.get_text(" ", strip=True) if following is not None and following.find(_BLOCK_TAGS) is None else ""
        if 2 <= len(name) <= 80 and not _EMPLOYER_LABEL.match(name):
            employers.append(name)
    for node in content.find_all(["h1", "h2", "h3", "h4", "h5", "strong"]):
        about = _ABOUT_HEADING.match(node.get_text(" ", strip=True))
        if about and not _ABOUT_GENERIC.match(about.group(1)) and _org_like(about.group(1), own_names):
            employers.append(about.group(1).strip())
    return result(None)


def _employer_key(name: str) -> str:
    return " ".join(_fold_a(t) for t in name_tokens(name))


def ad_employer_is_ours(ad: AdPage, rec: CompanyRecord, extra_names: list[str] | None = None) -> bool | None:
    """True when the ad names this organisation number (or one of its subunits'), or only names
    that are ours (legal, subunit or brand name); False when it names anyone else; None when it
    names no employer at all."""
    own_numbers = {rec.organisation_number} | {s.get("organisation_number") for s in rec.context.get("subunits") or [] if s.get("organisation_number")}
    if ad.numbers:
        return bool(ad.numbers & own_numbers)
    if not ad.employers:
        return None
    own_keys = {_employer_key(n) for n in _names_of(rec) + list(extra_names or []) if _employer_key(n)}
    return all(_name_is_ours(name, own_keys, rec) for name in ad.employers)


@dataclass
class Verified:
    """Roles that passed their ad page (or came from structured data), and what happened to the rest."""

    kept: list[tuple[Role, Snapshot, Snapshot | None]]  # (role, listing page, ad page)
    read: int = 0
    unread: int = 0
    blocked: int = 0
    refused: dict[str, str] | None = None  # ad URL -> reason
    group_list: bool = False


def verify_roles(plan: Plan, rec: CompanyRecord, roles: list[tuple[Role, Snapshot]], own_names: list[str], *, group_site: bool) -> Verified:
    """Keep only roles that are one open ad by this company.

    Structured roles (JSON-LD JobPosting, the ATS's own job feed) were checked where they were
    read. Every role link has its own ad page read (at most ``MAX_AD_FETCHES`` requests); a link
    that is not read is not published. The ad page decides: ``read_ad_page`` refuses closed ads,
    open applications and logins, and the employer it names must be this company. A page naming
    no employer is accepted only on the company's own (or own-tenant) list that is not a group
    list; a list is a group list when the site is a group site or its ad pages name two or more
    employers."""
    out = Verified([], refused={})
    ads: list[tuple[Role, Snapshot, AdPage, Snapshot]] = []
    structured: list[tuple[Role, Snapshot]] = []
    seen: set[str] = set()
    for role, listing in roles:
        if role.method != "careers_page_role_link":
            structured.append((role, listing))
            continue
        if _norm_url(role.url) in seen:
            continue  # the same link on the careers page and its listing: one read
        seen.add(_norm_url(role.url))
        if not ad_url_ok(role.url):
            out.refused[role.url] = "a search, login or landing URL, not one ad"
            continue
        page, why = plan.read(role.url, ad=True)
        if page is None:
            out.unread += 1
            out.blocked += why == "blocked"
            continue
        out.read += 1
        ad = read_ad_page(page[0], page[1], page[2], own_names)
        if ad.refused:
            out.refused[role.url] = ad.refused
            continue
        ads.append((role, listing, ad, page[0]))
    extra = list(rec.context.get("site_brand_names") or [])
    own_keys = {_employer_key(n) for n in _names_of(rec) + extra if _employer_key(n)}
    named: set[str] = set()
    for _role, _listing, ad, _snap in ads:
        verdict = ad_employer_is_ours(ad, rec, extra)
        if ad.numbers:
            named.add("ours" if verdict else "#" + ",".join(sorted(ad.numbers)))
        for name in ad.employers:
            named.add("ours" if _name_is_ours(name, own_keys, rec) else _employer_key(name))
    out.group_list = group_site or len(named) >= 2
    for role, listing, ad, snap in ads:
        verdict = ad_employer_is_ours(ad, rec, extra)
        if verdict is False:
            out.refused[role.url] = "the ad names another employer: " + ", ".join(sorted(ad.numbers) or ad.employers)[:120]
            continue
        if verdict is None and out.group_list:
            out.refused[role.url] = "the ad names no employer and sits on a group's list"
            continue
        role.title = role.title or (ad.title or "")[:200]
        role.expires = role.expires or ad.expires
        role.published = role.published or ad.published
        role.employer = role.employer or (ad.employers[0] if ad.employers else None)
        role.method = "ad_page_read"
        out.kept.append((role, listing, snap))
    for role, listing in structured:
        if not role.employer and out.group_list:
            out.refused[role.url] = "the posting names no employer and sits on a group's list"
            continue
        out.kept.append((role, listing, None))
    return out


def _name_is_ours(name: str, own_keys: set[str], rec: CompanyRecord) -> bool:
    """One of our names, or its leading words ("Om Leirvik" for LEIRVIK SVEIS AS's brand); a name
    with more words than ours ("AF Bygg Øst" beside "AF Gruppen") is another unit."""
    key = _employer_key(name)
    if not key:
        return False
    return key in own_keys or _name_matches(name, rec) or any(own.startswith(key + " ") for own in own_keys)


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
# A theme's slider, test or cloned post ("HeroSliderPost ( Cloned )", "TestPostMobil"): a page element, never news.
TEMPLATE_TITLE = re.compile(r"slider|test\s?post|lorem|ipsum|dummy|placeholder|\(\s*cloned\s*\)|\bcopy\b|\bkopi\b|untitled|uten\s+tittel|hello\s+world", re.I)
# WordPress category slugs (from the post's class_list) for posts a theme uses as page parts, catalogue
# or treatment pages, guides and portfolio entries. Exact slugs, a few with a prefix: "kosmetiske-
# behandlinger" holds a clinic's own news as well as its treatments, so only "behandling..." counts.
NOT_NEWS_CATEGORY = re.compile(
    r"^(?:frontpage[\w-]*|slider|hero[\w-]*|banner[\w-]*|produkt[\w-]*|products?|behandling[\w-]*|tjenest[\w-]*|services?|guides?|"
    r"knowledge|kunnskap[\w-]*|whitepapers?|outlet|faq|kampanje[\w-]*|tilbud[\w-]*)$"
    r"|^[\w-]*(?:investeringsprosjekt|portfolio|portefolj)[\w-]*$",
    re.I,
)
# A job advertisement posted as news: by its title, or by a word run inside a path segment
# (/aktuelt/ledige-stillinger-bli-en-av-oss).
JOB_AD_TITLE = re.compile(
    r"ledige?\s+stilling|søker\s+(?:nye?\s+)?(?:[\wæøå-]+\s+){0,2}(?:medarbeider|teammedlem|kolleg|ansatt|lærling)|vi\s+ansetter|"
    r"bli\s+en\s+(?:av\s+oss|del\s+av)|arbeta\s+i|we.?re\s+hiring|join\s+our\s+team",
    re.I,
)
JOB_AD_SEGMENT = re.compile(
    r"(?:^|[-_])(?:ledige?-stillinger?|ledig-stilling|stillingsannonse|vi-ansetter|jobb(?:e)?-hos-oss|bli-en-av-oss|"
    r"job-openings?|were-hiring|we-are-hiring|join-our-team|vacanc(?:y|ies))(?:[-_]|$)",
    re.I,
)
# Evergreen guides, explainers and white papers: dated, but not news about the company. Matched at the
# start of the title or of a part after a dash or colon ("Lipødem – Hvordan kan behandling hjelpe?").
GUIDE_TITLE = re.compile(
    r"practical\s+guide|whitepaper|\be-?bok\b|sjekkliste|checklist|\btips\b"
    r"|(?:^|[–—:|]\s*|\s-\s)(?:guide\b|report:|slik\s+(?:sjekker|forebygger|feirer|gjør|unngår|velger|fungerer)\b|hvordan\b|how\s+to\b|"
    r"hva\s+er\b|alt\s+(?:du\s+trenger|om)\b|\d+\s+[\wæøå]+\s+(?:feil|tips|grunner|ting)\b)",
    re.I,
)
ELLIPSIS = re.compile(r"(?:\.\.\.|…)\s*$")
# A WordPress site with no news-like category that files its posts under four or more topics (therapy
# areas, treatments, product lines) uses posts as a content library: such a post is news only under a
# news or /YYYY/MM/ path. Measured on the audited set: 25 wrong items, 1 correct.
NEWS_CATEGORY = re.compile(
    r"^(?:nyheter|nyhet|nyheiter|news|aktuelt|presse|press|pressemeldinger|pressemelding|press-releases?|blogg?|blogpost|"
    r"artikler|artikkel|articles?|nyhetsbrev|newsroom|media|events?|arrangementer?)$",
    re.I,
)
GENERIC_CATEGORY = re.compile(r"^(?:uncategori[sz]ed|ukategorisert|okategoriserade|general|generelt|ingen-kategori|annet|other|diverse)$", re.I)
TOPIC_LIBRARY_CATEGORIES = 4


def is_headline(title: str) -> bool:
    if not title or NOT_HEADLINE.match(title.strip()) or TEMPLATE_TITLE.search(title):
        return False
    letters = re.sub(r"[^A-Za-zÆØÅæøåÄÖäöÜüÉé]", "", title)
    return len(letters) >= 8 and 8 <= len(title) <= 250 and text_date(title) != title.strip()


def truncated_title(title: str, url: str | None) -> bool:
    """A card title cut short by the theme ("Astaxanthin: Naturens kraftigste antioksidant o..."):
    a single ellipsis at the end, with the article's slug running on past the words shown, or a
    cut of 40 characters or more. A title that ends in a deliberate "……" is kept."""
    if not ELLIPSIS.search(title) or re.search(r"(?:…|\.\.\.)\s*(?:…|\.\.\.)\s*$", title):
        return False
    stem = _alnum(ELLIPSIS.sub("", title))
    slug = _alnum(_segments(url)[-1]) if url and _segments(url) else ""
    return (bool(stem) and slug.startswith(stem) and len(slug) > len(stem)) or len(ELLIPSIS.sub("", title).strip()) >= 40


def news_path(url: str | None) -> bool:
    """The URL sits under a news section (/nyheter/<slug>) or a dated path (/2026/10/<slug>)."""
    if not url:
        return False
    return any(NEWS_SEGMENT.match(s) for s in _segments(url)[:-1]) or bool(re.search(r"/20\d\d/\d\d?/", urlsplit(url).path))


def topic_library(items: list["NewsItem"]) -> bool:
    """The WordPress posts read are filed by topic, not as news: no news-like category among them and
    four or more distinct topical categories."""
    categories = {c.lower() for item in items for c in item.categories}
    if not categories or any(NEWS_CATEGORY.match(c) for c in categories):
        return False
    return len({c for c in categories if not GENERIC_CATEGORY.match(c)}) >= TOPIC_LIBRARY_CATEGORIES


def _plain_words(text: str) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", _fold_a(text)).split()) + " "


@dataclass
class EntityWords:
    """Who a news title may be about. ``own``: words of our legal, subunit and brand names and our domain;
    ``other``: phrases naming the parent group (the register's parent and ultimate parent, and the group
    word an umbrella brand puts beside our name, "Aderian Netsolution")."""
    own: set[str]
    other: list[str]


def entity_words(rec: CompanyRecord, names: list[str], site_url: str) -> EntityWords:
    def words(name: str) -> list[str]:
        out: list[str] = []
        for token in name_tokens(name or ""):
            for fold in (_fold_a, _fold_b):
                word = re.sub(r"[^a-z0-9]", "", fold(token))
                if word and word not in out:
                    out.append(word)
        return out

    legal = [n for n in [(rec.context.get("entity") or {}).get("navn") or ""] + [s.get("name") or "" for s in rec.context.get("subunits") or []] if n]
    legal_words = {w for n in legal for w in words(n) if w not in IDENTITY_STOP and len(w) >= 3}
    label = re.sub(r"[^a-z0-9]", "", _site(site_url).split(".")[0]) if site_url else ""
    own = set(legal_words) | ({label} if len(label) >= 4 else set())
    other: list[str] = []

    def overlaps(word: str) -> bool:
        return any(word in w or w in word for w in own if len(w) >= 3)

    for name in names:
        brand = [w for w in words(name) if w not in IDENTITY_STOP and len(w) >= 3]
        if len(brand) >= 2 and any(w in legal_words for w in brand):
            # An umbrella brand: our name plus the group's ("Aderian Netsolution"); the extra word is the group's.
            other += [w for w in brand if not overlaps(w) and len(w) >= 4]
        own.update(w for w in brand if w in legal_words or len(brand) == 1)
    for claim in rec.claims:
        value = claim.get("value") if claim.get("field") == "group_relationship" else None
        if not isinstance(value, dict) or value.get("relationship") not in ("parent", "ultimate_parent"):
            continue
        core = [re.sub(r"[^a-z0-9]", "", _fold_a(t)) for t in name_tokens(str(value.get("name") or ""))]
        core = [w for w in core if w and w not in ("holding", "holdings", "group", "gruppen", "konsern", "invest", "og", "and")]
        phrase = " ".join(core)
        if len(phrase.replace(" ", "")) >= 5 and not any(overlaps(w) for w in core) and (len(core) > 1 or core[0] not in IDENTITY_STOP):
            other.append(phrase)
    return EntityWords(own, list(dict.fromkeys(other)))


def names_other_entity(title: str, entity: EntityWords | None) -> bool:
    """The title names the parent group (or the group's brand) and not this company: group press
    re-posted on the company's site ("Searchlight Capital Partners Announces Investment in Aderian Group")."""
    if entity is None or not entity.other:
        return False
    text = _plain_words(title)
    if not any(f" {phrase} " in text for phrase in entity.other):
        return False
    squashed = text.replace(" ", "")
    return not any(len(w) >= 4 and w in squashed for w in entity.own)


def news_drop_reason(item: "NewsItem", entity: EntityWords | None = None) -> str | None:
    """Why a dated item is not news about the company, or None. Each rule is a precision rule measured on
    the audited v1 output (campaign/audit/oct23/C_news_labels.tsv): it removed wrong items only."""
    title = item.title
    if any(NOT_NEWS_CATEGORY.match(c) for c in item.categories):
        return "page element, product, service, guide or portfolio category"
    if not is_headline(title):
        return "template or non-headline title"
    path_segments = _segments(item.url) if item.url else []
    if JOB_AD_TITLE.search(title) or any(JOB_AD_SEGMENT.search(s) for s in path_segments):
        return "job advertisement"
    if GUIDE_TITLE.search(re.sub(r"^[^\wæøåÆØÅ]+", "", title)):
        return "guide or explainer"
    if names_other_entity(title, entity):
        return "about the parent group, not the company"
    return None


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
    categories: tuple[str, ...] = ()  # WordPress category slugs (class_list "category-<slug>")
    # What the news gate reads (kildespor/newsgate.py): the item's own text (a WordPress excerpt, a
    # feed description, a card teaser, the article's opening), where its date was read, and the
    # article page's own signals when that page was read.
    lead: str = ""
    date_source: str = ""
    og_type: str = ""
    ld_types: tuple[str, ...] = ()
    article_time: bool = False
    flags: tuple[str, ...] = ()
    listing: str = ""


def listing_items(snap: Snapshot, soup: BeautifulSoup, site_host: str, *, require_news_path: bool) -> list[NewsItem]:
    """Dated items on a listing page (or the homepage): each article/list/card block with one
    article link, a headline and a date (``<time datetime>`` or a written date)."""
    base = snap.final_url
    base_key = _norm_url(base)
    items: list[NewsItem] = []
    seen: set[str] = set()
    blocks = soup.find_all(["article", "li"]) + soup.find_all(["div", "section"], class_=CARD_CLASS)
    for block in blocks:
        if _site_chrome_block(block):
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
        dated_by = ""
        times = [t for t in block.find_all("time") if not _update_date(t)]
        # A <time> marked as the publication date wins over the card's first <time>.
        time_tag = next((t for t in times if _PUBLISHED_MARK.search(_ident(t))), times[0] if times else None)
        date_source = ""
        if time_tag is not None:
            raw_time = time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
            published = stamp(time_tag.get("datetime")) or stamp(time_tag.get_text(" ", strip=True))
            dated_by = f'<time datetime="{raw_time}">' if time_tag.get("datetime") else f"<time>{raw_time}</time>"
            date_source = "time_datetime" if time_tag.get("datetime") else "time_text"
        if published is None:
            # A dateline beside the item's own title ("14.10.2026", "Publisert 14. oktober 2026"),
            # never a date inside a sentence of the teaser, never an update date.
            for string in block.find_all(string=True):
                text = str(string).strip()
                if 6 <= len(text) <= 40 and string.parent is not None and not _update_date(string.parent) and (day := text_date(text)):
                    published = (day, day)
                    dated_by = text
                    date_source = "card_text"
                    break
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
        if truncated_title(title, url):
            # The card cut its title short: the link's own title or label often carries it in full.
            stem = _alnum(ELLIPSIS.sub("", title))
            full = next((t for t in (str(anchor.get("title") or ""), str(anchor.get("aria-label") or "")) if is_headline(t) and _alnum(t).startswith(stem) and len(_alnum(t)) > len(stem)), None)
            title = full or title
        seen.add(key)
        card = _clean(block.get_text(" ", strip=True))
        lead = card.replace(_clean(title), " ", 1) if _clean(title) in card else card
        lead = re.sub(r"\s+", " ", lead.replace(dated_by, " ")).strip()[: newsgate.LEAD_CHARS]
        items.append(NewsItem(_clean(title), url, published[0], published[1], snap, f"{title} | {dated_by}", "news_listing_item", "company_website",
                              lead=lead, date_source=date_source, flags=newsgate.text_signals(lead), listing=_strip_url(base)))
    return items


_PUBLISHED_MARK = re.compile(r"publi|datepublished|post-?date|entry-?date", re.I)
# Site chrome a dated card can sit in without being news: menus, mega-menus, footers and sidebars.
_CHROME_CLASS = re.compile(r"(?<![a-z])(?:menu|mega|dropdown|submenu|sub-menu|navbar|navigation)(?![a-z])|menu-item|mega-?menu", re.I)


def _ident(node: Any) -> str:
    return " ".join(node.get("class") or []) + " " + str(node.get("id") or "") + " " + str(node.get("itemprop") or "")


def _site_chrome_block(block: Any) -> bool:
    """The block is (or sits in) navigation, a menu, the footer or a sidebar: never a news card."""
    if re.search(r"menu-item", " ".join(block.get("class") or []), re.I):
        return True
    for parent in block.parents:
        if getattr(parent, "name", None) in (None, "[document]", "html", "body"):
            break
        if parent.name in ("nav", "header", "footer", "aside") or str(parent.get("role") or "").lower() == "navigation" or _CHROME_CLASS.search(" ".join(parent.get("class") or []) + " " + str(parent.get("id") or "")):
            return True
    return False


_PUBLISHED_META = (
    ("property", "article:published_time"), ("property", "og:article:published_time"), ("name", "article:published_time"),
    ("itemprop", "datePublished"), ("name", "pubdate"), ("name", "publishdate"), ("name", "publish_date"), ("name", "publish-date"),
    ("name", "dc.date.issued"), ("name", "DC.date.issued"), ("name", "parsely-pub-date"), ("property", "og:published_time"),
)
_BYLINE = re.compile(r"byline|publi|post-?date|entry-?date|entry-?meta|post-?meta|article-?meta|article-?header|entry-?header|post-?header|dateline|dato|(?<![a-z])date(?![a-z])", re.I)
_NOT_PUBLICATION = re.compile(r"event|arrangement|deadline|frist|(?<![a-z])(?:start|end|slutt)(?![a-z])|expir|updated|modified|oppdatert", re.I)


def _dates_article(node: Any) -> bool:
    """The node or one of its three nearest containers is marked as the article's own date or byline."""
    for element in [node] + list(node.parents)[:3]:
        if getattr(element, "name", None) in (None, "[document]", "html", "body"):
            return False
        ident = " ".join(element.get("class") or []) + " " + str(element.get("id") or "") + " " + str(element.get("itemprop") or "")
        if _NOT_PUBLICATION.search(ident):
            return False
        if element.name == "header" or _BYLINE.search(ident) or "datepublished" in ident.lower():
            return True
    return False


UPDATE_LABEL = re.compile(r"oppdatert|endret|revidert|updated|modified|revised|last\s+change", re.I)


def _update_date(node: Any) -> bool:
    """The date is labelled as an update ("Sist oppdatert", "Endret", "Updated", "Last modified"):
    judged on the node's own text and on a short parent (the label usually sits beside the date)."""
    if "datemodified" in str(node.get("itemprop") or "").lower() if hasattr(node, "get") else False:
        return True
    # hentry themes mark the modification time <time class="updated"> (and a never-edited post's
    # single date "entry-date published updated", which is the publication date).
    classes = " ".join(node.get("class") or []) if hasattr(node, "get") else ""
    if re.search(r"(?<![a-z])(?:updated|modified)(?![a-z])", classes, re.I) and not re.search(r"publi", classes, re.I):
        return True
    texts = [node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)]
    parent = node.parent
    if parent is not None and getattr(parent, "name", None) not in (None, "[document]", "html", "body"):
        text = parent.get_text(" ", strip=True)
        if len(text) <= 80:
            texts.append(text)
    return any(UPDATE_LABEL.search(t) for t in texts)


def _byline_time(scope: Any) -> Any:
    """A <time> that dates the article itself: marked datePublished/pubdate, or sitting in the
    article's header, byline or date block. A <time> in the body (an event, a deadline) does not
    count, nor one labelled as an update ("Sist oppdatert")."""
    for tag in scope.find_all("time", attrs={"datetime": True}):
        if _update_date(tag):
            continue
        if (tag.get("itemprop") or "").lower() == "datepublished" or tag.has_attr("pubdate") or _dates_article(tag):
            return tag
    return None


def _byline_text_date(scope: Any) -> tuple[str, str] | None:
    """(date, the text) of a short dateline in an element marked as the article's date or byline
    ("Publisert 14. oktober 2026" in <p class="post-date">)."""
    for string in scope.find_all(string=True):
        text = str(string).strip()
        if 6 <= len(text) <= 40 and string.parent is not None and string.parent.name not in ("script", "style") and _dates_article(string.parent) and not _update_date(string.parent):
            day = text_date(text)
            if day:
                return day, text
    return None


def _strip_site_suffix(title: str, brands: list[str]) -> str:
    parts = re.split(r"\s+[|\-–—:»]\s+", title)
    if len(parts) > 1:
        tail = _alnum(parts[-1])
        if any(tail and (tail in _alnum(b) or _alnum(b) in tail) for b in brands if b) or len(parts[-1]) <= 30:
            return " - ".join(parts[:-1]).strip()
    return title.strip()


def _article_like(item: dict[str, Any]) -> bool:
    return any("Article" in t or "Posting" in t or "Release" in t for t in _types(item)) or "Report" in _types(item)


def _refers_to(item: dict[str, Any], page_url: str) -> bool:
    refs: list[Any] = [item.get("url"), item.get("@id")]
    main = item.get("mainEntityOfPage")
    refs += [main.get("@id"), main.get("url")] if isinstance(main, dict) else [main]
    key = _norm_url(page_url)
    for ref in refs:
        if isinstance(ref, str) and ref.strip():
            try:
                if _norm_url(urljoin(page_url, ref.strip().split("#")[0])) == key:
                    return True
            except ValueError:
                continue
    return False


def page_article(soup: BeautifulSoup, page_url: str) -> dict[str, Any] | None:
    """The JSON-LD article (or WebPage) that describes this page: bound to it by url, @id or
    mainEntityOfPage, or the page's only article object. A related or embedded article never
    lends this page its date. A WebPage counts only on a page that declares og:type article:
    SEO plugins give every page, service and product pages included, a WebPage datePublished."""
    og_type = soup.find("meta", attrs={"property": "og:type"})
    webpage_ok = og_type is not None and str(og_type.get("content") or "").strip().lower() == "article"
    items = [i for i in json_ld_items(soup) if i.get("datePublished") and (_article_like(i) or (webpage_ok and "WebPage" in _types(i)))]
    bound = [i for i in items if _refers_to(i, page_url)]
    if bound:
        return next((i for i in bound if _article_like(i)), bound[0])
    articles = [i for i in items if _article_like(i)]
    if len(articles) == 1 and all(_article_like(i) for i in items):
        return articles[0]
    return None


def page_lead(soup: BeautifulSoup, title: str = "") -> str:
    """The opening of the page's own text (article, else main, else body), after its headline."""
    scope = soup.find("article") or soup.find("main") or soup.body or soup
    parts: list[str] = []
    size = 0
    for string in scope.find_all(string=True):
        parent = string.parent
        if parent is None or parent.name in ("script", "style", "noscript", "template", "svg", "nav", "button"):
            continue
        if any(getattr(a, "name", None) in ("nav", "header", "footer", "aside") for a in list(parent.parents)[:6]) and parent.name not in ("h1", "time"):
            continue
        text = str(string).strip()
        if text:
            parts.append(text)
            size += len(text) + 1
        if size > 3 * newsgate.LEAD_CHARS:
            break
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if title:
        at = text.find(title)
        if 0 <= at < 400:
            text = text[at + len(title):].strip()
    if not text:
        og = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
        text = _clean(og.get("content")) if og is not None else ""
    return text[: newsgate.LEAD_CHARS]


def page_kind(soup: BeautifulSoup, html: str, text: str) -> tuple[str, tuple[str, ...], bool, tuple[str, ...]]:
    """(og:type, JSON-LD types, has article:published_time, page flags) of a page."""
    og = soup.find("meta", attrs={"property": "og:type"})
    og_type = str(og.get("content") or "").strip().lower()[:30] if og is not None else ""
    ld_types: set[str] = set()
    for item in json_ld_items(soup):
        ld_types |= _types(item)
    article_time = soup.find("meta", attrs={"property": "article:published_time"}) is not None
    return og_type, tuple(sorted(ld_types))[:12], article_time, newsgate.page_signals(html, text, ld_types)


def article_meta(snap: Snapshot, soup: BeautifulSoup, brands: list[str], text: str = "") -> NewsItem | None:
    """Headline and published datetime read from an article page itself."""
    published = None
    span = None
    date_source = ""
    for attr, name in _PUBLISHED_META:
        tag = soup.find("meta", attrs={attr: name})
        if tag is not None and tag.get("content"):
            published = stamp(tag["content"])
            if published:
                span = f'{attr}="{name}" content="{tag["content"].strip()}"'
                date_source = "meta_" + name.lower()
                break
    if published is None:
        item = page_article(soup, snap.final_url)
        if item is not None:
            published = stamp(item["datePublished"])
            if published:
                span = f'JSON-LD datePublished "{item["datePublished"]}"'
                date_source = "jsonld_" + (sorted(_types(item)) or ["?"])[0]

    if published is None:
        scope = soup.find("article") or soup.find("main") or soup
        time_tag = _byline_time(scope)
        if time_tag is not None:
            published = stamp(time_tag["datetime"])
            span = f'<time datetime="{time_tag["datetime"]}">' if published else None
            date_source = "time_byline"
        if published is None:
            dateline = _byline_text_date(scope)
            if dateline:
                published, span = (dateline[0], dateline[0]), f"byline: {dateline[1]}"
                date_source = "byline_text"
    # Never a date merely written near the headline: it may be an event, a deadline or a project year.
    if published is None or not _news_window(published[1]):
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
    og_type, ld_types, article_time, flags = page_kind(soup, snap.text(), text or soup.get_text(" ", strip=True))
    return NewsItem(title, _strip_url(snap.final_url), published[0], published[1], snap, f"{title} | {span}", "article_page_meta", "company_website",
                    lead=page_lead(soup, title), date_source=date_source, og_type=og_type, ld_types=ld_types, article_time=article_time, flags=flags)


_FEED_ITEM = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
_FEED_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.S | re.I)
_FEED_LINK_TEXT = re.compile(r"<link\b[^>]*>\s*(?:<!\[CDATA\[)?\s*(https?://[^<\s\]]+)", re.S | re.I)
_FEED_LINK_HREF = re.compile(r"<link\b([^>]*?)/?>", re.S | re.I)
_FEED_DATE = re.compile(r"<(pubDate|published|dc:date|updated|a10:updated)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
_FEED_CATEGORY = re.compile(r"<category\b([^>]*?)(?:/>|>(.*?)</category\s*>)", re.S | re.I)
_FEED_TEXT = re.compile(r"<(description|summary|content:encoded|content)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)


def _feed_categories(block: str) -> tuple[str, ...]:
    """Category slugs of a feed item: RSS <category>Produkter</category>, Atom <category term="produkter"/>."""
    out: list[str] = []
    for attrs, text in _FEED_CATEGORY.findall(block):
        term = re.search(r'term\s*=\s*["\']([^"\']+)', attrs)
        name = _clean(text) or (html_lib.unescape(term.group(1)) if term else "")
        slug = "-".join(re.sub(r"[^a-z0-9]+", " ", _fold_a(name)).split())
        if slug and slug not in out:
            out.append(slug)
    return tuple(out)


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
        # Publication dates only: an Atom entry with nothing but <updated> carries a modification time.
        raw = dates.get("pubdate") or dates.get("published")
        published = stamp(raw, to_local=True) if raw else None
        if not published or not is_headline(title):
            continue
        link = safe_link(link, site_host) if link else None
        texts = {m.group(1).lower(): m.group(2) for m in _FEED_TEXT.finditer(block)}
        lead = _clean(texts.get("description") or texts.get("summary") or texts.get("content:encoded") or texts.get("content") or "")[: newsgate.LEAD_CHARS]
        items.append(NewsItem(title[:300], link, published[0], published[1], snap, f"{title[:200]} | {raw}", "rss_atom_feed", "company_feed", "company_owned_feed", categories=_feed_categories(block),
                              lead=lead, date_source="feed_pubdate" if dates.get("pubdate") else "feed_published", flags=newsgate.text_signals(lead)))
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
        classes = post.get("class_list")
        classes = list(classes.values()) if isinstance(classes, dict) else classes if isinstance(classes, list) else []
        categories = tuple(c[len("category-"):] for c in classes if isinstance(c, str) and c.startswith("category-"))
        span = f"{rendered} | date {local}" + (f" | category {', '.join(categories)}" if categories else "")
        excerpt = post.get("excerpt")
        lead = _clean(excerpt.get("rendered") if isinstance(excerpt, dict) else excerpt or "")
        lead = re.sub(r"\s*(?:\[(?:…|&hellip;|\.\.\.)\]|Les mer.*|Read more.*|Continue reading.*)$", "", lead)[: newsgate.LEAD_CHARS]
        items.append(NewsItem(title[:300], link, published, local[:10], snap, span, "wordpress_posts_api", "company_website", categories=categories,
                              lead=lead, date_source="wp_date_gmt" if re.match(r"\d{4}-\d{2}-\d{2}T", gmt) else "wp_date", flags=newsgate.text_signals(lead)))
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
    snap: Snapshot | None = None  # the sitemap file this entry was read from (its evidence)


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


def _from(entries: list[SitemapEntry], snap: Snapshot) -> list[SitemapEntry]:
    for entry in entries:
        entry.snap = snap
    return entries


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
        entries.extend(_from(pages, snap))
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
            entries.extend(_from(pages, snap))
    in_section = getattr(plan.visit, "in_section", None) or (lambda _url: True)
    return [e for e in entries if _site(e.loc) == site_host and in_section(e.loc)], snaps


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
    # A group, chain or umbrella site (website._publish_site): its brand, profiles, news and job ads are
    # the group's. Contacts are read only from a store's own section; a whole-domain group site has none
    # that are ours.
    group_site = company_label is not None
    site_contacts = not group_site or bool(visit.section)
    plan = Plan(rec, visit)

    def own_pages() -> list[tuple[Snapshot, BeautifulSoup, str]]:
        # A section's root page, read to learn who owns the domain, is not one of this company's pages.
        return [p for p in visit.pages if _site(p[0].final_url) == site_host and visit.in_section(p[0].final_url)]

    home_page = next((p for p in visit.pages if p[0] is home), None) or (own_pages()[0] if own_pages() else None)
    if home_page is None:
        return
    home_soup = home_page[1]
    home_html = home.text()
    wordpress = bool(WORDPRESS.search(home_html[:600000]))

    # -- brand, description, contacts (homepage and the pages the identity check read) ------------
    brand_names = _brand_and_contacts(rec, home, own_pages(), exact, company_label, contacts_only=group_site) if site_contacts else []
    # Current names only (legal, subunits, brand, domain). A former name never approves a profile:
    # the name may now belong to a successor, so an account matching only it is not published.
    names = _names_of(rec) + brand_names
    own_names = sorted({" ".join(re.sub(r"[^a-z0-9 ]", " ", _fold_a(n)).split()) for n in names} | {" ".join(_fold_a(t) for t in name_tokens(n)) for n in names}, key=len, reverse=True)
    own_words = {re.sub(r"[^a-z0-9]", "", f(w)) for n in names for w in name_tokens(n) for f in (_fold_a, _fold_b)} | set(re.split(r"[^a-z0-9]+", _site(home.final_url).split(".")[0]))
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
            if site_contacts:
                _brand_and_contacts(rec, home, [page], exact, company_label, contacts_only=True)
    anchors = _anchors(own_pages())

    # Hiring: the careers page (own site first), then the ATS listing it links. These are where ads
    # are listed, never hiring facts themselves: only a single ad becomes a claim.
    careers: list[tuple[str, Snapshot, str, str]] = []  # (url, evidence snapshot, span, method)
    roles: list[tuple[Role, Snapshot]] = []
    site_wide = _site_wide_links(home_soup, home.final_url)
    candidates = _careers_candidates(anchors, sitemap, sitemap_snaps, site_host, contains)
    own = next((c for c in candidates if c[0] in (0, 2)), None)
    ats_links = [c for c in candidates if c[0] == 1]
    # True only when every careers source this site points to was read in full this run: refresh
    # may then close a site-found role that is gone ("job_posting:site").
    jobs_complete: bool | None = None
    rejected_roles: set[str] = set()
    hiring_notes = {"blocked": [], "unread": [], "no_openings": []}  # careers sources by outcome
    if own is not None:
        rank, url, snap, span, _text = own
        page, why = plan.read(url)
        if page is None:
            jobs_complete = False
            hiring_notes["blocked" if why == "blocked" else "unread"].append(_clean_url(url))
        if page is not None and _is_careers_page(page, rank, site_host):
            jobs_complete = True
            c_snap, c_soup, c_text = page
            title = (c_soup.title.get_text(" ", strip=True) if c_soup.title else "") or url
            careers.append((_clean_url(c_snap.final_url), c_snap, f"careers page: {title[:200]}", "careers_page_on_verified_site"))
            if NO_OPENINGS.search(c_text):
                hiring_notes["no_openings"].append(_clean_url(c_snap.final_url))
            roles += [(r, c_snap) for r in roles_on_page(c_snap, c_soup, rec, own_site=True, tenant_ok=True, site_wide=site_wide, rejected=rejected_roles)]
            listing = None if roles else _job_listing_link(c_snap, c_soup)
            if listing and listing.endswith(".rss") and plan.can(1, 6.0):
                feed = plan.raw(listing, headers={"Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.5"})
                roles += [(r, feed) for r in rss_roles(feed, c_snap.final_url)] if feed is not None else []
                jobs_complete = feed is not None
                if feed is None:
                    hiring_notes["unread"].append(listing)
            elif listing and plan.can(1, 6.0):
                l_page, l_why = plan.read(listing)
                if l_page is not None and _site(l_page[0].final_url) == _site(c_snap.final_url):
                    roles += [(r, l_page[0]) for r in roles_on_page(l_page[0], l_page[1], rec, own_site=True, tenant_ok=True, site_wide=site_wide, rejected=rejected_roles)]
                    if NO_OPENINGS.search(l_page[2]):
                        hiring_notes["no_openings"].append(_clean_url(l_page[0].final_url))
                else:
                    jobs_complete = False
                    hiring_notes["blocked" if l_why == "blocked" else "unread"].append(listing)
            elif listing:
                jobs_complete = False  # the job list was not read: the budget ran out
                hiring_notes["unread"].append(listing)
            for a in c_soup.find_all(["a", "iframe"]):
                href = a.get("href") or a.get("src")
                if not href:
                    continue
                try:
                    link = urljoin(c_snap.final_url, str(href)).split("#")[0]
                except ValueError:
                    continue
                if ats_of(link) and is_careers_link(link, a.get_text(" ", strip=True) if a.name == "a" else "", site_host) == 1 and not _role_url(link):
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
        why = "budget"
        if not ats_name:
            # A recruiter host we do not know: its page need not name the company in static HTML, so
            # it is noted as where the company recruits and never fetched or published.
            if snap is not None and CAREER_TEXT.match(text or ""):
                careers.append((_clean_url(url), snap, span, "recruiter_link_on_verified_site"))
                hiring_notes["unread"].append(_clean_url(url))
            jobs_complete = False
            continue
        if ats_name != "finn" and not fetched_ats and plan.can(2, 6.0):
            fetched_ats = True
            page, why = plan.read(url)
        # A list that redirects to a login or registration page sits behind it (recman.no/index.php
        # -> recman.io/login): it is blocked, not empty, and never a source of roles.
        behind_login = page is not None and (not ad_url_ok(page[0].final_url) or bool(AD_LOGIN_TEXT.match((page[1].title.get_text(" ", strip=True) if page[1].title else "")[:80])))
        if ats_name != "finn" and (page is None or ERROR_PAGE.search(page[0].final_url) or behind_login):
            jobs_complete = False  # an ATS listing this site points to was not read
            if ats_name != "finn":
                hiring_notes["blocked" if why == "blocked" or behind_login else "unread"].append(_clean_url(url))
        elif ats_name != "finn" and jobs_complete is None:
            jobs_complete = True
        if page is not None and not behind_login and not ERROR_PAGE.search(page[0].final_url + " " + ((page[1].title.get_text(" ", strip=True) if page[1].title else "")[:200])):
            a_snap, a_soup, a_text = page
            careers.append((_clean_url(a_snap.final_url), a_snap, f"applicant-tracking page ({ats_name}): {(a_soup.title.get_text(' ', strip=True) if a_soup.title else '')[:160]}", "ats_page_linked_from_verified_site"))
            if NO_OPENINGS.search(a_text):
                hiring_notes["no_openings"].append(_clean_url(a_snap.final_url))
            roles += [(r, a_snap) for r in roles_on_page(a_snap, a_soup, rec, own_site=False, tenant_ok=tenant_ok, rejected=rejected_roles)]
        elif snap is not None and (tenant_ok or text == "@careers-page" or ats_name == "finn" or CAREER_TEXT.match(text or "")):
            careers.append((_clean_url(url), snap, span, "ats_link_on_verified_site"))

    # A group careers site also lists its foreign subsidiaries' roles (career-lithuania.kitron.com).
    roles = [(r, s) for r, s in roles if not foreign_page(r.url, own_text) and _norm_url(r.url) not in rejected_roles and not _rejected_title(r.title, rejected_roles)]

    # News: WordPress post API or the declared feed, then a listing page, then article pages.
    news: dict[str, NewsItem] = {}
    refused: dict[str, str] = {}  # key -> why: a page refused once is not let back in by another route
    news_checked: list[str] = []  # what was read for news, named in the reason when nothing is published
    entity = entity_words(rec, brand_names, home.final_url)
    # Every candidate with the rules' verdict, for the development corpus (newsgate.dump; off by default).
    candidates: list[tuple[NewsItem, str | None]] = []
    corpus = bool(newsgate.dump_dir())
    # The learned content gate (newsgate.py) judges what the rules let through. Its refusals are kept
    # apart from the rules': a richer copy of the same item (its article page) is judged again.
    gate_ctx = {"org": rec.organisation_number, "legal_name": (rec.context.get("entity") or {}).get("navn") or "", "brands": brand_names, "host": site_host,
                "home": home.final_url, "wordpress": wordpress, "group_site": group_site, "own": sorted(entity.own), "other": entity.other}
    gated: dict[str, str] = {}
    scores: dict[int, float | None] = {}

    def add_news(item: NewsItem) -> None:
        early = None
        if not item.title:
            early = "no title"
        elif not _news_window(item.date):
            early = "outside the news window"
        elif item.url and NOT_NEWS_PATH.search(urlsplit(item.url).path):
            early = "shop, catalogue, category or job path"
        elif item.url and foreign_page(item.url, own_text):
            early = "a foreign sibling's page"
        if corpus:
            candidates.append((item, early or refused.get(_norm_url(item.url) if item.url else f"{item.title.lower()}|{item.date}") or news_drop_reason(item, entity)))
        if early:
            return
        key = _norm_url(item.url) if item.url else f"{item.title.lower()}|{item.date}"
        why = refused.get(key) or news_drop_reason(item, entity)
        if why:
            refused[key] = why
            news.pop(key, None)
            return
        keep, score = newsgate.verdict(newsgate.record(item, gate_ctx))
        scores[id(item)] = score
        if not keep:
            if item.method == "article_page_meta" or key not in news:
                # The article page is the fullest evidence there is: when it says no, an earlier copy goes too.
                gated[key] = "below the news gate (product, service, guide, commentary or other entity)"
                news.pop(key, None)
            return
        gated.pop(key, None)
        current = news.get(key)
        if current is None:
            for other_key, other in news.items():
                if other.title.lower() == item.title.lower() and other.date == item.date:
                    current, key = other, other_key
                    break
        if current is None or item.method == "article_page_meta" or (current.method != "article_page_meta" and len(item.published) > len(current.published)):
            news[key] = item

    for snap, soup, _ in own_pages():
        this_page = page_article(soup, snap.final_url)
        for item in json_ld_items(soup):
            if _types(item) & {"NewsArticle", "BlogPosting", "Article", "PressRelease", "Report"} and item.get("datePublished"):
                title = _clean(item.get("headline") or item.get("name") or "")
                when = stamp(item["datePublished"])
                if item.get("url"):
                    link = safe_link(urljoin(snap.final_url, str(item["url"])), site_host)  # its own page, its own date
                elif item is this_page:
                    link = snap.final_url
                else:
                    continue  # an embedded article with no address of its own dates nothing here
                if when and is_headline(title):
                    lead = _clean(item.get("description") or item.get("abstract") or "")[: newsgate.LEAD_CHARS]
                    add_news(NewsItem(title, link, when[0], when[1], snap, f'JSON-LD "{title[:150]}" datePublished {item["datePublished"]}', "json_ld_article", "company_website",
                                      lead=lead, date_source="jsonld_" + sorted(_types(item))[0], ld_types=tuple(sorted(_types(item))), flags=newsgate.text_signals(lead)))
    for entry in sitemap:
        if entry.news_title and entry.news_date:
            when = stamp(entry.news_date)
            snap = entry.snap
            if snap is None:
                continue
            if when and is_headline(entry.news_title):
                add_news(NewsItem(entry.news_title, entry.loc, when[0], when[1], snap, f"{entry.news_title} | news:publication_date {entry.news_date}", "news_sitemap", "company_website", date_source="news_sitemap"))
    feeds = _declared_feeds(own_pages(), site_host)
    if wordpress and plan.can(1, 6.0):
        api = _wordpress_api(home_soup, home.final_url)
        snap = plan.raw(api + "wp/v2/posts?per_page=20&_fields=date,date_gmt,link,title,class_list,excerpt", headers={"Accept": "application/json"})
        found = wordpress_items(snap, site_host) if snap is not None else []
        if snap is not None and snap.ok:
            news_checked.append(f"the WordPress post API ({len(found)} dated post(s))")
        library = topic_library(found)
        for item in found:
            if library and not news_path(item.url):
                refused.setdefault(_norm_url(item.url) if item.url else item.title.lower(), "a post in a topic library (4+ topic categories, no news category)")
                if corpus:
                    candidates.append((item, "a post in a topic library (4+ topic categories, no news category)"))
                continue
            add_news(item)
        if not found and not feeds:
            feeds = [urlunsplit((urlsplit(home.final_url).scheme, urlsplit(home.final_url).netloc, "/feed/", "", ""))]
    if (len(news) < 3 or corpus) and feeds and plan.can(1, 6.0):
        snap = plan.raw(feeds[0], headers={"Accept": "application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.5"})
        found = feed_items(snap, site_host) if snap is not None else []
        if snap is not None and snap.ok:
            news_checked.append(f"the feed {feeds[0]} ({len(found)} dated item(s))")
        for item in found:
            add_news(item)
    for item in listing_items(home, home_soup, site_host, require_news_path=True):
        add_news(item)
    listing_links: list[str] = []  # article pages the news listing links (read when its cards carry no date)
    if len(news) < 3 or corpus:
        listing = _news_listing(anchors, sitemap, site_host, home.final_url)
        if listing and plan.can(1, 6.0):
            page = plan.page(listing)
            if page is not None and _site(page[0].final_url) == site_host:
                found = listing_items(page[0], page[1], site_host, require_news_path=False)
                news_checked.append(f"the news listing {page[0].final_url} ({len(found)} dated item(s))")
                for item in found:
                    add_news(item)
                listing_links = _listing_articles(page[0], page[1], site_host)

    # Article pages: the headline and published datetime as the article itself states them.
    for url in _articles_to_read(anchors, news, sitemap, site_host, listing_links)[: MAX_ARTICLE_FETCHES + (2 if corpus else 0)]:
        if not plan.can(1, 6.0):
            break
        page = plan.page(url)
        if page is None or _site(page[0].final_url) != site_host:
            continue
        if len(listing_items(page[0], page[1], site_host, require_news_path=False)) >= 3:
            continue  # a listing, not an article
        item = article_meta(page[0], page[1], brand_names + names, page[2])
        news_checked.append(f"the article page {page[0].final_url}")
        if item is not None:
            add_news(item)

    # -- social profiles from every page of the site that was read --------------------------------
    profiles: dict[str, Found] = {}
    per_platform: dict[str, int] = {}
    p_contains, p_prefixes, p_text, p_names, p_words = _profile_identity(rec, brand_names, home.final_url)
    parents = [] if company_label == "group_site" else _parent_names(rec)
    for snap, soup, _ in own_pages():
        for found in profiles_on_page(snap, soup, snap.text(), p_contains, p_prefixes, rec, p_text, p_names, p_words, parents):
            key = found.url.lower()
            if key in profiles or per_platform.get(found.platform, 0) >= MAX_PROFILES_PER_PLATFORM:
                continue
            profiles[key] = found
            per_platform[found.platform] = per_platform.get(found.platform, 0) + 1
    if group_site:
        # The organiser's rule: a group's site lends this company none of its social accounts, news or
        # job ads. A profile the page links whose handle carries a word of our own name that the group's
        # brand lacks is this company's own, and is published without the group label; nothing else is.
        profiles = {key: found for key, found in profiles.items() if _own_on_group_site(found.handle, rec)}
        careers, roles, news, jobs_complete = [], [], {}, None
        company_label = None
    _publish_profiles(rec, list(profiles.values()), exact, company_label, own_pages())

    # -- job ads: each role link's own page is read last, on its own allowance ----------------------
    # A subsidiary never takes ads from a group site (its parent's list); the group's parent may,
    # but only ads whose page names it.
    parents_site = company_label == "group_site" and rec.context.get("group_root") != rec.organisation_number
    if parents_site:
        verified = Verified([], refused={r.url: "an ad on a group site is not attributed to a subsidiary" for r, _ in roles}, group_list=True)
    else:
        verified = verify_roles(plan, rec, roles, names, group_site=company_label == "group_site")
    if verified.unread and jobs_complete:
        jobs_complete = False  # a listed ad that was not read can be neither published nor closed
    _publish_hiring(rec, careers, verified.kept, company_label, site_host, complete=jobs_complete, notes=hiring_notes, verified=verified, parents_site=parents_site)
    published = _publish_news(rec, list(news.values()), company_label, ["the homepage"] + news_checked, {**gated, **refused})
    if corpus:
        keys = {_norm_url(i.url) if i.url else f"{i.title.lower()}|{i.date}" for i in published}
        rows = []
        for item, why in candidates:
            row = newsgate.record(item, gate_ctx)
            row["gate"] = scores.get(id(item))
            key = _norm_url(item.url) if item.url else f"{item.title.lower()}|{item.date}"
            row["rule"] = why or ""
            row["published_ok"] = key in keys and not why
            row["final_rule"] = "" if key in keys else (why or refused.get(key) or gated.get(key) or "not published (publish-stage rule, cap or group site)")
            rows.append(row)
        newsgate.dump(rows)
    if group_site:
        reason = (rec.context.get("website") or {}).get("group_reason") or "the verified site is a group site"
        rec.set_state("public_brand", "ambiguous", f"{reason}; its brand and self-description are the group's, not attributed to this entity")
        if not rec.claims_for("company_profiles"):
            rec.set_state("company_profiles", "ambiguous", f"{reason}; its social profiles are the group's, not attributed to this entity")
        for family, what in (("hiring", "careers page and job ads"), ("public_activity", "news")):
            if not rec.claims_for(family):
                rec.set_state(family, "not_available", f"{reason}; its {what} are the group's, not attributed to this entity")
        if not site_contacts and not rec.claims_for("contact_channels"):
            rec.set_state("contact_channels", "ambiguous", f"{reason}; its phone numbers and e-mail addresses are the group's, not attributed to this entity")

    _note_pages(rec, own_pages(), home, careers, news, site_host)
    if jobs_complete:
        rec.checked_fields.add("job_posting:site")
    rec.checked_fields.update({"social_profile", "website_phone", "website_email", "public_brand_name", "self_description"})
    if visit.time_left() > 5:
        rec.checked_fields.add("company_news")
    rec.context["harvest"] = {
        "requests": plan.used(), "ad_requests": plan.used() - plan.ad_start if plan.ad_start is not None else 0, "sitemap_entries": len(sitemap),
        "profiles": len(profiles), "careers": len(careers), "roles": len(roles), "ads_read": verified.read, "ads_unread": verified.unread,
        "ads_refused": len(verified.refused or {}), "jobs": len(verified.kept), "news": len(news),
    }


def _own_on_group_site(handle: str, rec: CompanyRecord) -> bool:
    """On a group site, a profile is this company's own only when its handle carries a word of our legal
    name that the group's brand lacks: facebook.com/husflidenholmestrand on the Holmestrand store's page
    of norskflid.no, never facebook.com/husfliden (the chain's)."""
    group = {_alnum(w) for w in name_tokens(str((rec.context.get("website") or {}).get("group_brand") or ""))}
    key = _alnum(handle)
    words = {_alnum(f(t)) for t in name_tokens((rec.context.get("entity") or {}).get("navn") or "") for f in (_fold_a, _fold_b)}
    return any(len(w) >= 4 and w not in GENERIC_NAME_WORDS and w not in group and w in key for w in words)


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


def _careers_candidates(anchors: list[tuple[str, str, Snapshot, str]], sitemap: list[SitemapEntry], sitemap_snaps: list[Snapshot], site_host: str, tokens: set[str] | None = None) -> list[tuple[int, str, Snapshot | None, str, str]]:
    """(rank, url, linking page, span, link text) for each careers link, best first."""
    found: dict[str, tuple[int, int, str, Snapshot | None, str, str]] = {}
    for order, (url, text, snap, raw) in enumerate(anchors):
        rank = is_careers_link(url, text, site_host, tokens)
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
            found[_norm_url(entry.loc)] = (0, 10000 + order, entry.loc, entry.snap, f"sitemap <loc>{entry.loc[:200]}</loc>", "")
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


def _listing_articles(snap: Snapshot, soup: BeautifulSoup, site_host: str) -> list[str]:
    """Article pages a news listing links, in page order: under a news section, or one level below
    the listing's own path (/nyheter -> /nyheter/<slug>). Menus, footers and sidebars are skipped."""
    base = urlsplit(snap.final_url).path.rstrip("/")
    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        raw = anchor["href"].strip()
        if raw.startswith(("#", "mailto:", "tel:", "javascript:")) or _site_chrome_block(anchor):
            continue
        try:
            url = urljoin(snap.final_url, raw).split("#")[0].split("?")[0]
        except ValueError:
            continue
        path = urlsplit(url).path.rstrip("/")
        below = bool(base) and path.startswith(base + "/") and path.count("/") == base.count("/") + 1
        segs = _segments(url)
        if _site(url) != site_host or not segs or any(NOT_ARTICLE_SEGMENT.match(s) for s in segs) or NEWS_SEGMENT.match(segs[-1]) or re.fullmatch(r"\d{1,4}", segs[-1]):
            continue
        if (is_article_url(url, site_host) or below) and url not in out:
            out.append(url)
    return out[:20]


def _articles_to_read(anchors: list[tuple[str, str, Snapshot, str]], news: dict[str, NewsItem], sitemap: list[SitemapEntry], site_host: str, listing_links: list[str] | None = None) -> list[str]:
    """Article pages worth one request each: those the homepage links under a news section (what
    Builderr's own crawler reads), then dated listing items without a time, then the articles the
    news listing links (its cards may carry no date at all), then the newest sitemap articles."""
    done = {_norm_url(n.url) for n in news.values() if n.url and n.method == "article_page_meta"}
    timed = {_norm_url(n.url) for n in news.values() if n.url and "T" in n.published}
    out: list[str] = []

    def add(url: str) -> None:
        key = _norm_url(url)
        if key not in done and key not in {_norm_url(u) for u in out}:
            out.append(url)

    # A card that cut its title short is published only once its own page gives the full headline.
    for item in sorted(news.values(), key=lambda n: n.date, reverse=True):
        if item.url and truncated_title(item.title, item.url) and _site(item.url) == site_host:
            add(item.url)
    for url, _text, snap, _raw in anchors:
        if len(_segments(snap.final_url)) == 0 and is_article_url(url, site_host):
            add(url)
    for item in sorted(news.values(), key=lambda n: n.date, reverse=True):
        if item.url and _norm_url(item.url) not in timed and _site(item.url) == site_host:
            add(item.url)
    known = {_norm_url(n.url) for n in news.values() if n.url}
    for url in listing_links or []:
        if _norm_url(url) not in known:
            add(url)
    dated = [e for e in sitemap if is_article_url(e.loc, site_host) and e.lastmod and _news_window(e.lastmod[:10])]
    for entry in sorted(dated, key=lambda e: e.lastmod or "", reverse=True):
        if _norm_url(entry.loc) not in timed:
            add(entry.loc)
    return out


VENDOR_WORDS = re.compile(r"\bdesign\s*:|\bwebdesign\b|\bwebbyr[aå]|\breklamebyr[aå]|\bnettbyr[aå]|\bbyr[aå]et\b|hosting\s*:|\bdrift\s*:", re.I)


def _vendor_context(anchor: Any) -> bool:
    """The anchor sits in a credit line or vendor block (levert av, utviklet av, design:, powered by,
    website by ...), judged on its nearest small container."""
    node = anchor
    for _ in range(3):
        node = node.parent
        if node is None or getattr(node, "name", None) in ("body", "html", "[document]"):
            return False
        if len({a.get("href") for a in node.find_all("a", href=True)}) > 4:
            return False
        text = node.get_text(" ", strip=True)
        if len(text) > 250:
            return False
        if CREDIT_WORDS.search(text) or VENDOR_WORDS.search(text):
            return True
    return False


def _brand_and_contacts(rec: CompanyRecord, home: Snapshot, pages: list[tuple[Snapshot, BeautifulSoup, str]], exact: bool, company_label: str | None, contacts_only: bool = False) -> list[str]:
    phones: dict[str, tuple[Snapshot, str]] = {}
    emails: dict[str, tuple[Snapshot, str]] = {}
    brand_candidates: list[tuple[str, Snapshot, str]] = []
    description: tuple[str, Snapshot] | None = None
    site_host = _site(home.final_url)
    for snap, soup, _text in pages:
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            label = anchor.get_text(" ", strip=True)
            if href.lower().startswith(("tel:", "mailto:")) and _vendor_context(anchor):
                # The web agency's contact in a credit line ("Nettside levert av Webbyrå AS, tlf ..."):
                # never the company's, except an address on the company's own domain.
                address = unquote(href[7:].split("?")[0]).strip().lower() if href.lower().startswith("mailto:") else ""
                if not (address and registered_domain(address.rsplit("@", 1)[-1]) == site_host):
                    continue
            if href.lower().startswith("tel:"):
                phone = normalise_phone(href[4:])
                if phone:
                    phones.setdefault(phone, (snap, f"tel link: {href[:40]}"))
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


def _profile_identity(rec: CompanyRecord, brand_names: list[str], site_url: str) -> tuple[set[str], set[str], str, list[str], set[str]]:
    """(contains, prefixes, own_text, own_names, own_words) for judging the site's profile links.

    The legal and subunit names always count; the site's brand names (the published public brand,
    JSON-LD name, og:site_name, <title>) count only as far as ``identity_brands`` admits them, so a
    title such as "Hjem" or "Utleiemegler Oslo" adds no token. Former names never count."""
    entity = rec.context.get("entity") or {}
    legal = [n for n in [entity.get("navn") or ""] + [s.get("name") or "" for s in rec.context.get("subunits") or []] if n]
    published = [c["value"] for c in rec.claims if c["field"] == "public_brand_name" and isinstance(c["value"], str)]
    brands = list(dict.fromkeys(published + list(brand_names)))
    names = legal + identity_brands(brands, legal, site_url)
    contains, prefixes = identity_tokens(names, site_url, place_tokens=False)
    own_text = " ".join(_fold_a(n) for n in names) + " " + " ".join(re.sub(r"[^a-z0-9 ]", " ", _fold_a(n)) for n in names)
    own_names = sorted({" ".join(re.sub(r"[^a-z0-9 ]", " ", _fold_a(n)).split()) for n in names} | {" ".join(_fold_a(t) for t in name_tokens(n)) for n in names}, key=len, reverse=True)
    own_words = {re.sub(r"[^a-z0-9]", "", f(w)) for n in names for w in name_tokens(n) for f in (_fold_a, _fold_b)} | set(re.split(r"[^a-z0-9]+", _site(site_url).split(".")[0]))
    return contains, prefixes, own_text, own_names, own_words


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


def _hiring_source(snap: Snapshot, site_host: str) -> str:
    if _site(snap.final_url) == site_host:
        return "company_owned_website"
    return "company_ats_page" if ats_of(snap.final_url) else "recruitment_page_linked_from_company_site"


def _publish_hiring(
    rec: CompanyRecord,
    careers: list[tuple[str, Snapshot, str, str]],
    roles: list[tuple[Role, Snapshot, Snapshot | None]],
    company_label: str | None,
    site_host: str = "",
    *,
    complete: bool | None = None,
    notes: dict[str, list[str]] | None = None,
    verified: "Verified | None" = None,
    parents_site: bool = False,
) -> None:
    """One ``job_posting`` claim per single ad that passed ``verify_roles``. Careers pages, ATS
    lists and recruiter pages are where ads are listed, not hiring facts: they are never claims,
    only the checked sources of the hiring verdict (and ``context["career_pages"]``)."""
    notes = notes or {}
    sources = list(dict.fromkeys(url for url, *_ in careers))[:6]
    rec.context["career_pages"] = sources
    existing = {_norm_url(c["value"].get("url") or "") for c in rec.claims if c["field"] == "job_posting" and isinstance(c["value"], dict)}
    published = 0
    for role, listing, ad_snap in roles:
        key = _norm_url(role.url)
        title = _clean(role.title)[:300]
        if published >= MAX_JOBS or not title or (key in existing and role.url != listing.final_url):
            continue
        if not ad_url_ok(role.url) or (key == _norm_url(listing.final_url) and not _role_url(listing.final_url)):
            continue  # a search or login link, or a posting whose only address is the careers page
        existing.add(key)
        value = {"title": title, "url": role.url, "published": role.published, "expires": role.expires, "location": role.location, "employer": role.employer, "source": role.source}
        evidence = []
        if ad_snap is not None:
            # The ad page is the fact: it was read, is open, and names (or does not contradict) us.
            span = f'ad page: "{title[:150]}"' + (f"; employer named: {role.employer[:80]}" if role.employer else "; no other employer named")
            evidence.append(rec.evidence_from_snapshot(ad_snap, source_class=_hiring_source(ad_snap, site_host), span=span, method="ad_page_read", url=ad_snap.final_url))
            if _site(ad_snap.final_url) == site_host and hasattr(rec, "note_page"):
                rec.note_page(ad_snap, title, "job_ad")
        evidence.append(rec.evidence_from_snapshot(listing, source_class=_hiring_source(listing, site_host), span=role.span, method="careers_page_role_link" if ad_snap is not None else role.method, url=listing.final_url))
        rec.add_claim(
            family="hiring",
            field_name="job_posting",
            value={k: v for k, v in value.items() if v not in (None, "")},
            key=["job", key if role.url != listing.final_url else f"{key}|{title.lower()}"],
            evidence_ids=evidence,
            confidence=0.9,
            method=role.method,
            effective_date=(role.published or "")[:10] or None,
            relationship=company_label,
            label="Job posting",
        )
        published += 1

    from .jobs import record_site_hiring

    first = sources[0] if sources else None
    refused = (verified.refused if verified is not None else None) or {}
    if published:
        record_site_hiring(rec, "available", f"{published} single job ad(s) read on the company's own careers channel", sources)
    elif not careers and not any(notes.values()):
        return  # no careers page found: the site says nothing either way; NAV decides
    elif parents_site:
        record_site_hiring(rec, "not_available", f"the careers page {first} is on a group site; its ads belong to the group's units and are not attributed to this organisation number", sources)
    elif notes.get("no_openings"):
        record_site_hiring(rec, "not_available", f"careers page {notes['no_openings'][0]} says there are no open positions", sources, explicit=True)
    elif notes.get("blocked") or (verified is not None and verified.blocked):
        where = (notes.get("blocked") or [first])[0]
        what = "its ad list" if notes.get("blocked") else f"{verified.blocked if verified else 0} of its ad pages"
        record_site_hiring(rec, "blocked", f"careers page found ({first}), but {what} sits behind a login, a 401/403 or robots.txt ({where})", sources + notes.get("blocked", []))
    elif complete:
        why = f"; {len(refused)} listed link(s) were not single open ads by this company (filled, expired, open applications or another employer's)" if refused else ""
        record_site_hiring(rec, "not_available", f"careers page {first} lists no open ad{why}", sources)
    else:
        unread = (notes.get("unread") or [])[:2]
        detail = f"; not read: {', '.join(unread)}" if unread else (f"; {verified.unread} ad page(s) not read within the request budget" if verified is not None and verified.unread else "")
        record_site_hiring(rec, "failed", f"careers page found ({first}), but its ad list could not be read in full, so absence is not established{detail}", sources + unread)


COPY_BURST_ITEMS = 3
COPY_BURST_SECONDS = 600


def _instant(item: NewsItem) -> datetime | None:
    """The item's publication instant, when it carries a time of day (a bare date cannot burst)."""
    if "T" not in item.published:
        return None
    try:
        when = datetime.fromisoformat(item.published.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=OSLO or timezone.utc)
    return when


def copy_bursts(items: list[NewsItem]) -> set[int]:
    """Indexes of items whose timestamps fall, three or more together, within ten minutes: a CMS copy
    or bulk-import date (a site migration, a group's stories re-posted in one sitting), not the dates
    the posts were first published."""
    timed = sorted((when, i) for i, item in enumerate(items) if (when := _instant(item)) is not None)
    out: set[int] = set()
    end = 0
    for start in range(len(timed)):
        end = max(end, start)
        while end + 1 < len(timed) and (timed[end + 1][0] - timed[start][0]).total_seconds() <= COPY_BURST_SECONDS:
            end += 1
        # An import stamps posts one after another (seconds apart); posts released together at one
        # scheduled instant share it exactly, so identical instants count once.
        if len({when for when, _ in timed[start:end + 1]}) >= COPY_BURST_ITEMS:
            out.update(i for _, i in timed[start:end + 1])
    return out


def _publish_news(rec: CompanyRecord, items: list[NewsItem], company_label: str | None, checked: list[str] | None = None, refused: dict[str, str] | None = None) -> list[NewsItem]:
    dropped: dict[str, int] = {}
    for why in (refused or {}).values():
        dropped[why] = dropped.get(why, 0) + 1
    burst = copy_bursts(items)
    kept: list[NewsItem] = []
    for index, item in enumerate(items):
        why = "a copy or import date shared by 3+ posts within 10 minutes" if index in burst else "title cut short on the listing card" if truncated_title(item.title, item.url) else None
        if why:
            dropped[why] = dropped.get(why, 0) + 1
        else:
            kept.append(item)
    items = sorted(kept, key=lambda i: (i.date, i.published), reverse=True)[:MAX_NEWS]
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
    elif checked is not None:
        # Recorded even when register updates make the family available: the dated-news row reads it.
        reason = "no dated news about the company on its own website; checked " + ", ".join(checked)
        if dropped:
            reason += "; dated items not published: " + ", ".join(f"{n} {why}" for why, n in sorted(dropped.items(), key=lambda kv: -kv[1]))
        rec.set_state("public_activity", "not_available", reason[:600])
    return items
