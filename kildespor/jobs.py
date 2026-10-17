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


def search_names(rec: CompanyRecord) -> list[str]:
    """Names an ad's ``businessName`` may carry: the legal name, the registered subunits' names and
    the brand the verified website uses (an ad is often posted under the brand). The names only
    choose which entries to open; the entry's employer orgnr decides."""
    entity = rec.context.get("entity") or {}
    names = [entity.get("navn") or ""] + [s.get("name") or "" for s in rec.context.get("subunits") or []]
    brands = [c["value"] for c in rec.claims if c["field"] == "public_brand_name" and isinstance(c["value"], str)]
    brands += list(rec.context.get("site_brand_names") or [])
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
        self.thread.start()
        return self

    def _read(self) -> None:
        started = time.monotonic()
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
            for uuid, entry in self.active.items():
                key = name_key(entry["business"])
                if key:
                    self.by_key.setdefault(key, set()).add(uuid)
        except Exception as exc:  # the batch must finish even when the feed does not
            self.error = f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            self.done.set()

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


def research_jobs(fetcher: Fetcher, rec: CompanyRecord, deadline: float, feed: NavFeed | None) -> None:
    entity = rec.context.get("entity") or {}
    legal_name = entity.get("navn") or ""
    if feed is None:
        if not rec.claims_for("hiring"):
            rec.set_state("hiring", "failed", "the job feed was not started")
        return
    if not feed.wait(deadline - 2):
        if not rec.claims_for("hiring"):
            rec.set_state("hiring", "failed", "the NAV vacancy feed had not finished loading within the time budget")
        return
    if feed.token is None or (feed.error and not feed.active):
        if not rec.claims_for("hiring"):
            rec.set_state("hiring", "failed", f"NAV vacancy feed unavailable: {feed.error}")
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
        if not str(link).startswith("https://arbeidsplassen.nav.no/"):
            link = f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
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
        rec.set_state("hiring", "available", f"{published} active job ad(s) in NAV's vacancy feed verified to this organisation number", checked_sources=checked)
    elif not rec.claims_for("hiring"):
        rec.set_state(
            "hiring",
            "not_available",
            f"no active ad in NAV's vacancy feed (last {feed.days} days) names this organisation number or its subunits as employer"
            + (f"; {len(candidates)} name match(es) belonged to other organisation numbers" if candidates else ""),
            checked_sources=checked,
        )
