"""Active job ads from NAV's public vacancy feed (pam-stilling-feed.nav.no).

The feed is NAV's official API for redistributing the job ads on
arbeidsplassen.nav.no. It is read once per run, in a background thread, from
``FEED_DAYS`` back to now: every change in order, so the last status seen for
an ad is its current one. Names in the feed only narrow down which entries to
open; an ad is published only when the entry's own ``employer.orgnr`` is this
entity's organisation number or one of its registered subunits'. The token is
NAV's published public token, fetched at run time (no account or secret).
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

from . import ats
from .http import Fetcher
from .model import CompanyRecord
from .website import name_tokens, strip_accents

FEED = "https://pam-stilling-feed.nav.no"
FEED_DAYS = int(os.environ.get("KILDESPOR_NAV_FEED_DAYS", "45"))
MAX_PAGES = 250
MAX_CANDIDATES = 8
MAX_PUBLISHED = 25
POLICY = "NAV public vacancy feed (pam-stilling-feed.nav.no), public token"


def name_key(name: str) -> str:
    tokens = name_tokens(name or "")
    return " ".join(strip_accents(t.replace("æ", "ae").replace("ø", "o").replace("å", "a")) for t in tokens)


NOT_A_BRAND = {"hjem", "home", "forside", "startside", "velkommen", "welcome", "index", "nettbutikk", "kontakt", "om oss"}


def search_names(rec: CompanyRecord, *, nav_index: bool = True) -> list[str]:
    """Names an ad's ``businessName`` may carry: the legal name, the registered subunits' names and
    the brand the verified website uses (an ad is often posted under the brand). The names only
    choose which entries to open; the entry's employer orgnr decides."""
    entity = rec.context.get("entity") or {}
    names = [entity.get("navn") or ""] + [s.get("name") or "" for s in rec.context.get("subunits") or []]
    brands = [c["value"] for c in rec.claims if c["field"] == "public_brand_name" and isinstance(c["value"], str)]
    brands += list(rec.context.get("site_brand_names") or [])
    if nav_index:  # names NAV ads carried for this orgnr (declared index): only to choose entries to open
        brands += ats.nav_names(rec.organisation_number)
    for brand in brands:
        key = name_key(brand)
        if len(key.replace(" ", "")) >= 3 and key not in NOT_A_BRAND and brand not in names:
            names.append(brand)
    return [n for n in names if n]


class NavFeed:
    """Reads the vacancy feed once; ``candidates`` and ``entry`` are called from worker threads."""

    def __init__(self, fetcher: Fetcher, days: int = FEED_DAYS, max_seconds: float = 300.0) -> None:
        self.fetcher = fetcher
        self.days = days
        self.max_seconds = max_seconds
        self.active: dict[str, dict[str, str]] = {}
        self.by_key: dict[str, set[str]] = {}
        self.token: str | None = None
        self.pages = 0
        self.error: str | None = None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._read, name="nav-feed", daemon=True)

    def start(self) -> "NavFeed":
        self.jobbnorge = JobbnorgeList(self.fetcher).start()
        self.thread.start()
        return self

    jobbnorge: "JobbnorgeList | None" = None

    def _read(self) -> None:
        started = time.monotonic()
        try:
            self._read_nav(started)
        finally:
            if self.jobbnorge is not None:
                self.jobbnorge.done.wait(timeout=max(5.0, self.max_seconds - (time.monotonic() - started)))
            self.done.set()

    def _read_nav(self, started: float) -> None:
        try:
            token = self.fetcher.get(FEED + "/api/publicToken", retries=2, check_robots=False)
            words = token.text().split()
            if not token.ok or not words:
                self.error = f"public token unavailable (HTTP {token.status})"
                return
            self.token = words[-1]
            since = format_datetime(datetime.now(timezone.utc) - timedelta(days=self.days), usegmt=True)
            url: str | None = FEED + "/api/v1/feed"
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "If-Modified-Since": since}
            while url and self.pages < MAX_PAGES:
                if time.monotonic() - started > self.max_seconds:
                    self.error = f"feed read stopped after {self.max_seconds:.0f}s ({self.pages} pages)"
                    break
                snap = self.fetcher.get(url, retries=2, check_robots=False, headers=headers)
                headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
                if not snap.ok:
                    self.error = f"feed page returned HTTP {snap.status} after {self.pages} pages"
                    break
                body = snap.json()
                self.pages += 1
                for item in body.get("items") or []:
                    entry = item.get("_feed_entry") or {}
                    uuid = str(entry.get("uuid") or item.get("id") or "")
                    if not uuid:
                        continue
                    if entry.get("status") == "ACTIVE":
                        self.active[uuid] = {"title": str(entry.get("title") or ""), "business": str(entry.get("businessName") or "")}
                    else:
                        self.active.pop(uuid, None)
                next_url = body.get("next_url")
                url = FEED + next_url if next_url else None
            if url and self.error is None:
                # Stopped at the page cap with pages left: the active set is incomplete, and an
                # empty lookup against it cannot establish that a company has no ad.
                self.error = f"feed read stopped at the {MAX_PAGES}-page cap with more pages left"
            for uuid, entry in self.active.items():
                key = name_key(entry["business"])
                if key:
                    self.by_key.setdefault(key, set()).add(uuid)
        except Exception as exc:  # the batch must finish even when the feed does not
            self.error = f"{type(exc).__name__}: {str(exc)[:160]}"

    def wait(self, deadline: float) -> bool:
        return self.done.wait(timeout=max(0.0, deadline - time.monotonic()))

    def candidates(self, names: list[str]) -> list[str]:
        keys = {name_key(n) for n in names if name_key(n)}
        found: list[str] = []
        for key in keys:
            found.extend(sorted(self.by_key.get(key, ())))
        if len(found) < MAX_CANDIDATES:
            # A trading name often extends the legal name ("Que Pasa" -> "Que Pasa Hamar"); the org-number
            # check on the entry decides, so a loose prefix match only costs a request.
            for key in keys:
                if len(key) < 6 or (" " not in key and len(key) < 8):
                    continue
                for other, uuids in self.by_key.items():
                    if other != key and len(other) >= 6 and (other.startswith(key + " ") or key.startswith(other + " ")):
                        found.extend(sorted(uuids))
        return list(dict.fromkeys(found))

    def entry(self, uuid: str) -> Any:
        return self.fetcher.get(
            f"{FEED}/api/v1/feedentry/{uuid}",
            retries=2,
            check_robots=False,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )


JOBBNORGE = "https://publicapi.jobbnorge.no/v3/Jobs"
JOBBNORGE_POLICY = "Jobbnorge public API v3 (publicapi.jobbnorge.no/v3/Jobs, keyless, documented at /swagger; the host serves no robots.txt), read once per run"


def _fold_place(text: Any) -> str:
    return strip_accents(str(text or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")).strip()


class JobbnorgeList:
    """Jobbnorge's public list of open ads (mostly public-sector employers), read once per run.

    An ad names its employer by name only, never by organisation number. It is published for an
    entity only when the name it gives as employer equals one of the entity's names exactly (legal
    name, a subunit's or the verified brand, legal form dropped) and one of the ad's workplaces
    lies in a municipality or postcode where the entity or one of its subunits is registered."""

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.snap: Any = None
        self.by_key: dict[str, list[dict[str, Any]]] = {}
        self.count = 0
        self.error: str | None = None
        self.done = threading.Event()

    def start(self) -> "JobbnorgeList":
        threading.Thread(target=self._read, name="jobbnorge", daemon=True).start()
        return self

    @staticmethod
    def employer_of(job: dict[str, Any]) -> str:
        """The employer the ad names: its department when Jobbnorge marks the department as the
        employer (a hospital under a health trust), else the employer."""
        if job.get("regardDepartmentAsEmployer") and str(job.get("department") or "").strip():
            return " ".join(str(job["department"]).split())
        return " ".join(str(job.get("employer") or "").split())

    def _read(self) -> None:
        try:
            snap = self.fetcher.get(JOBBNORGE, retries=1, headers={"Accept": "application/json"}, timeout=(5.0, 30.0))
            if not snap.ok:
                self.error = f"HTTP {snap.status}"
                return
            body = snap.json()
            jobs = body.get("jobs") if isinstance(body, dict) else None
            if not isinstance(jobs, list):
                self.error = "no job list in the response"
                return
            self.snap = snap
            self.count = len(jobs)
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                key = name_key(self.employer_of(job))
                if key:
                    self.by_key.setdefault(key, []).append(job)
        except Exception as exc:  # the batch must finish even when Jobbnorge does not answer
            self.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            self.done.set()


def _register_places(rec: CompanyRecord) -> tuple[set[str], set[str]]:
    """Municipalities and postcodes where the entity or one of its subunits is registered."""
    entity = rec.context.get("entity") or {}
    places: set[str] = set()
    postcodes: set[str] = set()
    for key in ("forretningsadresse", "postadresse", "beliggenhetsadresse"):
        address = entity.get(key) or {}
        if address.get("kommune"):
            places.add(_fold_place(address["kommune"]))
        if re.fullmatch(r"\d{4}", str(address.get("postnummer") or "")):
            postcodes.add(str(address["postnummer"]))
    for sub in rec.context.get("subunits") or []:
        if sub.get("municipality"):
            places.add(_fold_place(sub["municipality"]))
        postcodes.update(re.findall(r"\b(\d{4})\b", str(sub.get("address") or "")))
    return places - {""}, postcodes


def research_jobbnorge(rec: CompanyRecord, board: JobbnorgeList) -> tuple[int, list[str]]:
    if board.snap is None:
        return 0, []
    keys = {name_key(n) for n in search_names(rec, nav_index=False) if name_key(n)}
    candidates = [job for key in sorted(keys) for job in board.by_key.get(key, [])]
    if not candidates:
        return 0, []
    places, postcodes = _register_places(rec)
    today = datetime.now(timezone.utc).date().isoformat()
    listed = {str((c["value"] or {}).get("url") or "").rstrip("/") for c in rec.claims if c["field"] == "job_posting" and isinstance(c["value"], dict)}
    published = 0
    for job in candidates[:MAX_PUBLISHED]:
        title = " ".join(str(job.get("title") or "").split())
        link = str(job.get("link") or "")
        if not title or not re.fullmatch(r"https://www\.jobbnorge\.no/ledige-stillinger/stilling/\d+(?:/[\w%-]*)?", link) or job.get("isInternal"):
            continue
        if ats.OPEN_APPLICATION.search(title) or link.rstrip("/") in listed:
            continue
        where = [loc for loc in job.get("locations") or [] if isinstance(loc, dict)]
        place = next((loc for loc in where if _fold_place(loc.get("municipality")) in places or str(loc.get("zipCode") or "") in postcodes), None)
        if place is None:
            continue  # the name matches, but no workplace is where this entity is registered
        due = str(job.get("deadline") or "").strip()
        expires = ats._iso_date(due)
        if expires and expires < today:
            continue
        employer = JobbnorgeList.employer_of(job)
        municipality = " ".join(str(place.get("municipality") or "").split())
        value: dict[str, Any] = {
            "title": title[:300],
            "url": link,
            "employer": employer,
            "location": " ".join(str(place.get("area") or municipality).split()) or None,
            "published": ats._iso_date(job.get("publicationDate")),
            "expires": expires,
            "application_due": due or None,
            "positions": job.get("positionCount"),
            "source": "jobbnorge",
            "board": "jobbnorge.no",
        }
        span = f'"{title[:180]}" employer "{employer[:100]}"' + (f' municipality "{municipality}"' if municipality else "")
        ev = rec.evidence_from_snapshot(
            board.snap,
            source_class="public_job_board_api",
            span=span,
            method="jobbnorge_employer_name_and_registered_place",
            access_policy=JOBBNORGE_POLICY,
        )
        rec.add_claim(
            family="hiring",
            field_name="job_posting",
            value={k: v for k, v in value.items() if v not in (None, "")},
            key=["jobbnorge", str(job.get("id") or link)],
            evidence_ids=[ev],
            confidence=0.9,
            method="jobbnorge_employer_name_and_registered_place",
            effective_date=value["published"],
            label="Job posting",
        )
        published += 1
    return published, [JOBBNORGE + f" ({board.count} open ads)"] if published else []


def _research_ats(fetcher: Fetcher, rec: CompanyRecord, deadline: float, feed: "NavFeed | None") -> None:
    """ATS lists the offline index ties to this entity, and Jobbnorge's run-wide list."""
    published = 0
    sources: list[str] = []
    try:
        count, read = ats.research_index(fetcher, rec, deadline)
        published += count
        sources += read
    except Exception as exc:
        rec.error("hiring", f"ATS index lookup stopped: {type(exc).__name__}: {str(exc)[:120]}")
    board = feed.jobbnorge if feed is not None else None
    if board is not None and board.done.is_set():
        try:
            count, read = research_jobbnorge(rec, board)
            published += count
            sources += read
        except Exception as exc:
            rec.error("hiring", f"Jobbnorge lookup stopped: {type(exc).__name__}: {str(exc)[:120]}")
    if published:
        record_ats_hiring(rec, "available", f"{published} open ad(s) naming this entity in public applicant-tracking or job-board lists", sources)


def research_jobs(fetcher: Fetcher, rec: CompanyRecord, deadline: float, feed: NavFeed | None) -> None:
    entity = rec.context.get("entity") or {}
    legal_name = entity.get("navn") or ""
    _research_ats(fetcher, rec, deadline, feed)
    if feed is None:
        record_nav_hiring(rec, "failed", "the job feed was not started")
        return
    if not feed.wait(deadline - 2):
        record_nav_hiring(rec, "failed", "the NAV vacancy feed had not finished loading within the time budget")
        return
    if feed.token is None or (feed.error and not feed.active):
        record_nav_hiring(rec, "failed", f"NAV vacancy feed unavailable: {feed.error}")
        return
    subunits = rec.context.get("subunits") or []
    names = search_names(rec)
    own_numbers = {rec.organisation_number} | {s.get("organisation_number") for s in subunits if s.get("organisation_number")}
    candidates = feed.candidates(names)
    today = datetime.now(timezone.utc).date().isoformat()
    listed = {str((c["value"] or {}).get("url") or "").rstrip("/") for c in rec.claims if c["field"] == "job_posting" and isinstance(c["value"], dict)}
    published = 0
    complete = True
    checked = [f"{FEED}/api/v1/feed (last {feed.days} days, {feed.pages} pages, {len(feed.active)} active ads)"]
    for uuid in candidates[: MAX_CANDIDATES * 3]:
        if published >= MAX_PUBLISHED or time.monotonic() > deadline - 2:
            complete = False
            break
        try:
            snap = feed.entry(uuid)
        except Exception as exc:
            complete = False
            rec.error("hiring", f"feed entry fetch failed: {type(exc).__name__}: {str(exc)[:100]}", f"{FEED}/api/v1/feedentry/{uuid}")
            if "budget" in str(exc):
                break
            continue
        if not snap.ok:
            complete = complete and snap.status == 404
            continue
        try:
            body = snap.json()
        except ValueError:
            continue
        if body.get("status") != "ACTIVE":
            continue  # NAV's terms: an ad leaves the results as soon as it is inactive
        ad = body.get("ad_content") or {}
        employer = ad.get("employer") or {}
        number = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
        if number not in own_numbers:
            continue
        title = " ".join(str(ad.get("title") or "").split())
        if not title:
            continue
        expires = str(ad.get("expires") or "")[:10]
        if expires and expires < today:
            continue
        link = ad.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
        if not re.fullmatch(r"https://arbeidsplassen\.nav\.no/stillinger/stilling/[\w-]{6,}/?", str(link)):
            link = f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"  # the ad itself, never a search or list
        if str(link).rstrip("/") in listed:
            continue
        locations = ", ".join(dict.fromkeys(filter(None, [(loc or {}).get("city") or (loc or {}).get("municipal") for loc in ad.get("workLocations") or []]))) or None
        published_at = str(ad.get("published") or "")[:10] or None
        value: dict[str, Any] = {
            "title": title[:300],
            "url": link,
            "employer": employer.get("name"),
            "employer_organisation_number": number,
            "location": locations,
            "published": published_at,
            "expires": str(ad.get("expires") or "")[:10] or None,
            "application_due": ad.get("applicationDue"),
            "positions": ad.get("positioncount"),
            "job_title": ad.get("jobtitle"),
            "original_source": ad.get("source"),
            "board": "arbeidsplassen.nav.no",
        }
        ev = rec.evidence_from_snapshot(
            snap,
            source_class="official_public_job_feed",
            span=f'feedentry {uuid}: "{title[:100]}", employer {employer.get("name")} (orgnr {number}), status ACTIVE',
            method="nav_feed_entry_employer_orgnr",
            access_policy=POLICY,
        )
        rec.add_claim(
            family="hiring",
            field_name="job_posting",
            value={k: v for k, v in value.items() if v not in (None, "")},
            key=["nav", uuid],
            evidence_ids=[ev],
            confidence=0.98,
            method="nav_feed_entry_employer_orgnr",
            effective_date=published_at,
            label="Job posting",
        )
        homepage = (employer.get("homepage") or "").strip()
        if homepage:
            rec.context.setdefault("nav_employer", {}).setdefault(
                "homepage", (homepage, snap, f'feedentry {uuid}: employer.homepage = "{homepage}" (employer orgnr {number})')
            )
        published += 1
    if complete and len(candidates) <= MAX_CANDIDATES * 3 and not feed.error:
        rec.checked_fields.add("job_posting")
    if published:
        record_nav_hiring(rec, "available", f"{published} active job ad(s) in NAV's vacancy feed verified to this organisation number", checked)
    elif feed.error:
        # A partly read feed keeps the ads it found, but its silence proves nothing.
        record_nav_hiring(rec, "failed", f"NAV's vacancy feed was read only in part ({feed.error}), so no ad is not the same as no vacancy", checked)
    else:
        record_nav_hiring(
            rec,
            "not_available",
            f"no active ad in NAV's vacancy feed (last {feed.days} days) names this organisation number or its subunits as employer"
            + (f"; {len(candidates)} name match(es) belonged to other organisation numbers" if candidates else ""),
            checked,
        )


# -- one hiring verdict from two sources ---------------------------------------------------------
# The website harvest and the NAV lookup run in separate tasks, in either order and sometimes at
# once. Each records its own verdict here; the family state is recomputed from both, so the one
# that finishes last cannot overwrite the other's ("blocked" on the site is not erased by an empty
# NAV lookup, and a careers page alone never makes the family available).
_SETTLE = threading.Lock()


def record_site_hiring(rec: CompanyRecord, state: str, reason: str, sources: list[str], *, explicit: bool = False) -> None:
    """The website's verdict: ``explicit`` when the careers page itself says there are no openings."""
    with _SETTLE:
        rec.context["site_hiring"] = {"state": state, "reason": reason, "sources": list(sources), "explicit": explicit}
        _settle(rec)


def record_nav_hiring(rec: CompanyRecord, state: str, reason: str, sources: list[str] | None = None) -> None:
    with _SETTLE:
        rec.context["nav_hiring"] = {"state": state, "reason": reason, "sources": list(sources or [])}
        _settle(rec)


def record_ats_hiring(rec: CompanyRecord, state: str, reason: str, sources: list[str] | None = None) -> None:
    """Ads found in an ATS list the index ties to the entity, or on Jobbnorge. Only ever
    ``available``: an empty list here establishes nothing the site and NAV do not."""
    with _SETTLE:
        rec.context["ats_hiring"] = {"state": state, "reason": reason, "sources": list(sources or [])}
        _settle(rec)


def _settle(rec: CompanyRecord) -> None:
    """``available`` needs a single ad from either source. Otherwise ``not_available`` needs both
    sources to establish absence (the careers list read in full and NAV empty), or the careers page
    saying so itself; a site list behind a login is ``blocked``, and anything not established is
    ``failed``. Without a careers page the NAV verdict stands alone."""
    site = rec.context.get("site_hiring")
    nav = rec.context.get("nav_hiring")
    extra = rec.context.get("ats_hiring")
    parts = [x for x in (site, nav) if x]
    if not parts and not extra:
        return
    sources = sorted({s for x in parts + ([extra] if extra else []) for s in x["sources"]})
    available = [x for x in parts + ([extra] if extra else []) if x["state"] == "available"]
    if available or rec.claims_for("hiring"):
        reason = "; ".join(x["reason"] for x in available) or "evidence-backed job ads published"
        rec.set_state("hiring", "available", reason, checked_sources=sources)
        return
    if not parts:
        return
    reason = "; ".join(x["reason"] for x in parts)
    if site is None:
        state = nav["state"]
    elif site["state"] in ("blocked", "failed"):
        state = site["state"]
    elif site.get("explicit"):
        state = "not_available"  # the company's own careers page says it has no openings
    elif nav is None or nav["state"] != "not_available":
        state = "failed"  # the careers list was empty, but NAV was not checked: absence not established
        if nav is None:
            reason += "; NAV's vacancy feed not checked yet"
    else:
        state = "not_available"
    rec.set_state("hiring", state, reason, checked_sources=sources)


# -- one claim per vacancy ------------------------------------------------------------------------


def _title_key(title: Any) -> str:
    return " ".join(re.sub(r"[^\w ]", " ", strip_accents(str(title or "").lower())).split())


def _from_nav(claim: dict[str, Any]) -> bool:
    value = claim.get("value") if isinstance(claim.get("value"), dict) else {}
    return str(claim.get("extraction_method") or "").startswith("nav") or value.get("board") == "arbeidsplassen.nav.no"


def merge_duplicate_ads(rec: CompanyRecord) -> None:
    """The same vacancy read on the company's careers channel and in NAV's feed is one fact.

    Duplicates are matched by normalised title within one envelope; every ad here is already
    attributed to this organisation number (NAV by the employer orgnr, which may be a subunit's;
    the site by its ad page), so the employer side of the match is this entity. The NAV entry is
    kept, because it carries the organisation number and the expiry; the other URL moves to its
    ``also_at`` with its evidence. Runs on the frozen record before refresh and synthesis."""
    jobs = [c for c in rec.claims if c.get("field") == "job_posting" and isinstance(c.get("value"), dict) and not c.get("stale")]
    nav: dict[str, dict[str, Any]] = {}
    for claim in jobs:
        key = _title_key(claim["value"].get("title"))
        if key and _from_nav(claim):
            nav.setdefault(key, claim)
    if not nav:
        return
    dropped: list[dict[str, Any]] = []
    for claim in jobs:
        if _from_nav(claim):
            continue
        twin = nav.get(_title_key(claim["value"].get("title")))
        if twin is None:
            continue
        value = dict(twin["value"])
        also = list(value.get("also_at") or [])
        url = claim["value"].get("url")
        if url and url not in also:
            also.append(url)
        value["also_at"] = sorted(also)
        twin["value"] = value
        twin["evidence_ids"] = sorted(set(twin["evidence_ids"]) | set(claim.get("evidence_ids", [])))
        dropped.append(claim)
    if dropped:
        rec.claims = [c for c in rec.claims if not any(c is d for d in dropped)]
