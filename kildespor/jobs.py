"""Active job ads from arbeidsplassen.nav.no (NAV, the Norwegian Labour and Welfare Administration).

arbeidsplassen.nav.no is the national public job board and also carries ads
imported from FINN and employers' own systems. Its robots.txt allows all
paths. The search endpoint the site itself uses is queried with the legal
name; a hit is kept only when its employer name matches the legal name or one
of the entity's registered subunit names, and it is published only after the
ad page itself shows an organisation number belonging to this entity (the
main unit or one of its subunits).
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

from .http import Fetcher
from .model import CompanyRecord
from .website import name_tokens, strip_accents

SEARCH = "https://arbeidsplassen.nav.no/stillinger/api/search?q={query}"
AD_PAGE = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
ORGNR_IN_AD = re.compile(r'orgnr\\?"\s*:\s*\\?"(\d{9})')
MAX_VERIFIED_ADS = 12


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(name.lower().replace("æ", "ae").replace("ø", "o").replace("å", "a"))).strip()


def employer_matches(employer: str, legal_name: str, subunit_names: set[str]) -> bool:
    e = _norm(employer)
    if not e:
        return False
    legal = _norm(legal_name)
    if e == legal or e.startswith(legal + " avd ") or e.startswith(legal + " avdeling "):
        return True
    return e in subunit_names


EMPLOYER_FIELD = re.compile(r'"(homepage|linkedinPage|twitterAddress|facebookPage)":"([^"]+)"')


def _employer_profile(rec: CompanyRecord, ad: Any, ad_text: str, at: int) -> None:
    """The employer's own homepage and profile links as entered in an org-number-verified ad."""
    window = ad_text[max(0, at - 40): at + 900].replace('\\"', '"').replace("\\/", "/")
    found = {}
    for key, value in EMPLOYER_FIELD.findall(window.split('"descriptionHtml"')[0]):
        if value and value != "null":
            found[key] = value.strip()
    if not found:
        return
    store = rec.context.setdefault("nav_employer", {})
    for key, value in found.items():
        store.setdefault(key, (value, ad, f'employer.{key} = "{value}" in ad {ad.requested_url.rsplit("/", 1)[-1]}'))


def research_jobs(fetcher: Fetcher, rec: CompanyRecord, deadline: float) -> None:
    entity = rec.context.get("entity") or {}
    legal_name = entity.get("navn") or ""
    tokens = name_tokens(legal_name)
    if not tokens:
        rec.set_state("hiring", "not_available", "legal name too short to search job ads")
        return
    # harRegistrertAntallAnsatte false means no employees are registered in the Aa-register for the
    # main unit; employees can still sit on its subunits (a parent's head office), so only skip when
    # there is neither.
    employees = entity.get("antallAnsatte") if entity.get("harRegistrertAntallAnsatte") else 0
    if not employees and not rec.context.get("subunits") and not rec.context.get("career_pages"):
        if not rec.claims_for("hiring"):
            rec.set_state("hiring", "not_available", "no registered employees, no registered workplaces and no careers page; job boards not queried", checked_sources=["data.brreg.no antallAnsatte"])
        return
    query = " ".join(tokens)
    url = SEARCH.format(query=quote(query))
    try:
        snap = fetcher.get(url, retries=1, headers={"Accept": "application/json"})
    except Exception as exc:
        rec.error("hiring", f"job search failed: {exc}", url)
        if not rec.claims_for("hiring"):
            rec.set_state("hiring", "failed", f"job board search failed: {type(exc).__name__}")
        return
    if not snap.ok:
        if not rec.claims_for("hiring"):
            rec.set_state("hiring", "blocked" if snap.status in (401, 403, 429) else "failed", f"job board search returned HTTP {snap.status}", checked_sources=[url])
        return
    try:
        hits = snap.json()["hits"]["hits"]
    except (ValueError, KeyError, TypeError):
        rec.set_state("hiring", "failed", "job board search response was not understood", checked_sources=[url])
        return
    subunits = rec.context.get("subunits") or []
    subunit_names = {_norm(s.get("name") or "") for s in subunits if s.get("name")}
    own_numbers = {rec.organisation_number} | {s.get("organisation_number") for s in subunits if s.get("organisation_number")}
    candidates = []
    for hit in hits:
        source = hit.get("_source") or {}
        names = [(source.get("employer") or {}).get("name") or "", source.get("businessName") or ""]
        if any(employer_matches(name, legal_name, subunit_names) for name in names if name):
            candidates.append(source)
    published = 0
    checked = [url]
    for source in candidates[:MAX_VERIFIED_ADS]:
        if time.monotonic() > deadline - 3:
            break
        uuid = source.get("uuid")
        if not uuid:
            continue
        ad_url = AD_PAGE.format(uuid=uuid)
        try:
            ad = fetcher.get(ad_url, retries=0)
        except Exception as exc:
            rec.error("hiring", f"ad page fetch failed: {exc}", ad_url)
            continue
        checked.append(ad_url)
        if not ad.ok:
            continue
        ad_text = ad.text()
        matches = [m for m in ORGNR_IN_AD.finditer(ad_text) if m.group(1) in own_numbers]
        if not matches:
            continue
        number = matches[0].group(1)
        excerpt = ad_text[max(0, matches[0].start() - 20): matches[0].end() + 60].replace('\\"', '"')
        _employer_profile(rec, ad, ad_text, matches[0].start())
        location = ", ".join(
            dict.fromkeys(filter(None, [loc.get("city") or loc.get("municipal") for loc in source.get("locationList") or []]))
        ) or None
        published_at = (source.get("published") or "")[:10] or None
        expires = (source.get("expires") or "")[:10] or None
        value: dict[str, Any] = {
            "title": source.get("title"),
            "url": ad_url,
            "employer": (source.get("employer") or {}).get("name"),
            "employer_organisation_number": number,
            "location": location,
            "published": published_at,
            "expires": expires,
            "application_due": (source.get("properties") or {}).get("applicationdue"),
            "positions": source.get("positioncount"),
            "original_source": source.get("source"),
            "occupation": ((source.get("occupationList") or [{}])[0] or {}).get("level2"),
            "board": "arbeidsplassen.nav.no",
        }
        ev = rec.evidence_from_snapshot(
            ad,
            source_class="public_job_board",
            span=f'ad {uuid} "{(source.get("title") or "")[:100]}": {excerpt}',
            method="nav_ad_page_orgnr_match",
            access_policy="arbeidsplassen.nav.no public ad page; robots.txt allows all",
        )
        rec.add_claim(
            family="hiring",
            field_name="job_posting",
            value={k: v for k, v in value.items() if v not in (None, "")},
            key=["nav", uuid],
            evidence_ids=[ev],
            confidence=0.97,
            method="nav_ad_page_orgnr_match",
            effective_date=published_at,
            label="Job posting",
        )
        published += 1
    if published:
        rec.set_state("hiring", "available", f"{published} active job ad(s) verified on arbeidsplassen.nav.no", checked_sources=checked)
    elif not rec.claims_for("hiring"):
        rec.set_state(
            "hiring",
            "not_available",
            "no active job ads for this employer on arbeidsplassen.nav.no at retrieval time" + (" (name matches could not be tied to this organisation number)" if candidates else ""),
            checked_sources=checked,
        )
