"""Write envelopes in the evaluator's claim vocabulary, and read them back.

Internally a website, profile, careers page, job ad or news item is one claim
whose value is a dict (url, platform, title, dates ...). Builderr's feedback
keys these facts as plain strings: a website by its domain, a profile by
``facebook.com/<handle>``, a hiring signal by the careers page or the ad's URL,
a news item as ``"<title> (<published>)"``. On disk every such claim therefore
carries a scalar ``value`` in one of four fields (``official_website``,
``social_profile``, ``hiring_signal``, ``dated_news``) and keeps the full dict
in ``detail``. ``internal()`` reverses this, so refresh compares like with
like.

The envelope also carries ``profile``: the starter kit's ``evidence.website``
record (final URL, identity verdict, social links, bounded pages), built from
the same verified claims, for readers that consume the kit's shape.
"""
from __future__ import annotations

from typing import Any
import re
from urllib.parse import urlsplit

NEWS_PATH = re.compile(r"/(?:news|press|aktuelt|nyheter|artikler|blog)(?:/|$)", re.I)

# internal field -> published field
PUBLISHED_FIELD = {
    "official_website": "official_website",
    "social_profile": "social_profile",
    "careers_page": "hiring_signal",
    "job_posting": "hiring_signal",
    "company_news": "dated_news",
}
# internal family -> published availability field (rows for families with nothing found)
FAMILY_FIELD = {
    "official_website": "official_website",
    "company_profiles": "social_profile",
    "hiring": "hiring_signal",
    "public_activity": "dated_news",
}
SOCIAL_HOSTS = {
    "facebook.com": "facebook", "fb.com": "facebook", "instagram.com": "instagram", "linkedin.com": "linkedin",
    "twitter.com": "x", "x.com": "x", "youtube.com": "youtube", "tiktok.com": "tiktok", "pinterest.com": "pinterest",
    "vimeo.com": "vimeo", "threads.net": "threads", "github.com": "github",
}
CANONICAL_HOST = {
    "facebook": "facebook.com", "instagram": "instagram.com", "linkedin": "linkedin.com", "x": "x.com",
    "youtube": "youtube.com", "tiktok": "tiktok.com", "pinterest": "pinterest.com", "vimeo": "vimeo.com",
    "threads": "threads.net", "github": "github.com",
}


def canonical_social(url: str) -> str:
    """``https://<platform host>/<path>``: www/m/country subdomains, query and fragment dropped, case kept."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return url
    segments = [s for s in parts.path.split("/") if s]
    if platform == "facebook" and segments[:1] == ["profile.php"]:
        page_id = next((v for k, v in (pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair) if k == "id"), None)
        return f"https://facebook.com/profile.php?id={page_id}" if page_id else url
    if platform == "linkedin":
        segments = segments[:2]
    elif platform == "youtube" and segments:
        segments = segments[:1] if segments[0].startswith("@") else segments[:2]
    if not segments:
        return url
    return f"https://{CANONICAL_HOST[platform]}/{'/'.join(segments)}"


# A group's site lends the company none of its profiles, job ads or news (the organiser's rule). The
# harvest already withholds them; this is the last check before anything reaches disk.
GROUP_RELATIONSHIPS = {"group_site", "parent", "ultimate_parent", "subsidiary", "sister"}
GROUP_WITHHELD_FIELDS = {"social_profile", "hiring_signal", "dated_news"}


def group_content(claim: dict[str, Any]) -> bool:
    """A profile, hiring signal or news claim labelled with a group relationship: never published."""
    field = claim.get("field")
    return (PUBLISHED_FIELD.get(field, field) in GROUP_WITHHELD_FIELDS) and claim.get("relationship") in GROUP_RELATIONSHIPS


def _scalar(field: str, value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if field == "official_website":
        return value.get("url") or value.get("domain")
    if field == "social_profile":
        return canonical_social(str(value.get("url") or ""))
    if field == "job_posting":
        return value.get("url") or value.get("title")
    if field == "company_news":
        title = " ".join(str(value.get("title") or "").split())
        when = value.get("published") or value.get("date") or value.get("published_at")
        return f"{title} ({when})" if when else title
    return value.get("url") or value.get("title") or value


def publish(envelope: dict[str, Any]) -> dict[str, Any]:
    """The on-disk form of an envelope (a shallow copy; the input is not modified)."""
    out = dict(envelope)
    claims: list[dict[str, Any]] = []
    published_available: set[str] = set()
    for claim in envelope.get("claims", []):
        field = claim.get("field")
        if group_content(claim):
            continue
        if field in PUBLISHED_FIELD and claim.get("availability") == "available":
            item = dict(claim)
            item["field"] = PUBLISHED_FIELD[field]
            item["value"] = _scalar(field, claim.get("value"))
            if isinstance(claim.get("value"), dict):
                item["detail"] = claim["value"]
            if field != item["field"]:
                item["internal_field"] = field
            if field in ("careers_page", "job_posting"):
                item["signal_type"] = field
                if isinstance(claim.get("value"), dict) and claim["value"].get("title"):
                    item["title"] = claim["value"]["title"]
            if field == "company_news" and isinstance(claim.get("value"), dict):
                item["url"] = claim["value"].get("url")
                item["published_at"] = claim["value"].get("published") or claim["value"].get("date")
            if item.get("value") in (None, ""):
                continue
            if item["field"] == "social_profile":
                twin = next((c for c in claims if c.get("field") == "social_profile" and c.get("availability") == "available"
                             and str(c.get("value")).lower() == str(item["value"]).lower()), None)
                if twin is not None:  # same profile from two routes (site link and Wikidata): one claim, both evidence trails
                    twin["evidence_ids"] = sorted(set(twin["evidence_ids"]) | set(item.get("evidence_ids", [])))
                    twin["confidence"] = max(twin.get("confidence") or 0, item.get("confidence") or 0)
                    continue
            published_available.add(item["field"])
            claims.append(item)
        elif field in FAMILY_FIELD and claim.get("value") is None and claim.get("availability") != "available":
            # Availability row for an internal family: name it by the published field.
            item = dict(claim)
            item["field"] = FAMILY_FIELD[field]
            item["internal_field"] = field
            claims.append(item)
        else:
            claims.append(claim)
    # Every published family gets an explicit row when nothing was found, even when the internal
    # family is "available" for another reason (register updates are not dated news).
    present = {c["field"] for c in claims}
    sections = envelope.get("sections") or {}
    family_states = {fam: info for sec in sections.values() for fam, info in (sec.get("families") or {}).items()}
    for family, pfield in FAMILY_FIELD.items():
        if pfield in published_available or pfield in present:
            continue
        state = family_states.get(family) or {}
        availability = state.get("state") if state.get("state") not in (None, "available") else "not_available"
        claims.append({
            "id": f"na-{envelope.get('organisation_number')}-{pfield}",
            "key": f"{pfield}:availability",
            "family": family,
            "field": pfield,
            "value": None,
            "availability": availability,
            "reason": state.get("reason") if state.get("state") != "available" else f"no {pfield.replace('_', ' ')} found on the sources checked",
            "checked_sources": state.get("checked_sources", []),
            "evidence_ids": [],
            "internal_field": family,
        })
    out["claims"] = claims
    out["profile"] = kit_profile(envelope)
    return out


def internal(envelope: dict[str, Any]) -> dict[str, Any]:
    """Undo ``publish`` for one envelope read back from disk (refresh baseline)."""
    if not any("internal_field" in c or "detail" in c for c in envelope.get("claims", [])):
        return envelope
    out = dict(envelope)
    claims = []
    for claim in envelope.get("claims", []):
        item = dict(claim)
        origin = item.pop("internal_field", None)
        if origin:
            item["field"] = origin
        if "detail" in item:
            item["value"] = item.pop("detail")
        if origin in ("careers_page", "job_posting", "company_news"):
            for extra in ("signal_type", "title", "url", "published_at"):
                item.pop(extra, None)
        claims.append(item)
    out["claims"] = claims
    out.pop("profile", None)
    return out


def kit_profile(envelope: dict[str, Any]) -> dict[str, Any]:
    """The starter kit's ``profile.evidence.website`` record, from our verified claims only."""
    org = envelope.get("organisation_number")
    claims = [c for c in envelope.get("claims", []) if c.get("availability") == "available" and not group_content(c)]
    evidence = {e["id"]: e for e in envelope.get("evidence", [])}
    site = next((c for c in claims if c.get("field") == "official_website" and isinstance(c.get("value"), dict)), None)
    if site is None:
        row = next((c for c in envelope.get("claims", []) if c.get("field") == "official_website"), {})
        status = {"blocked": "blocked", "not_available": "not_found", "ambiguous": "not_found", "failed": "source_error"}.get(row.get("availability"), "not_found")
        return {"organisation_number": org, "evidence": {"website": {"field": "website", "status": status, "source_type": "company_website", "source_class": "company_website", "source_url": None, "retrieved_at": None, "value": None, "note": row.get("reason")}}}
    value = site["value"]
    def _same_page(a: Any, b: Any) -> bool:
        return bool(a) and bool(b) and str(a).rstrip("/").lower() == str(b).rstrip("/").lower()

    home = value.get("url")
    # The website record pairs the homepage URL with the homepage's own hash; the identity page (often
    # /kontakt) is listed in pages with its own hash, never presented as the homepage.
    site_ev = next((evidence[e] for e in site.get("evidence_ids", []) if e in evidence
                    and (_same_page(evidence[e].get("final_url"), home) or _same_page(evidence[e].get("source_url"), home))), None)
    if site_ev is None:
        page = next((p for p in (envelope.get("crawl") or {}).get("pages_checked") or [] if _same_page(p.get("url"), home)), None)
        if page:
            site_ev = {"source_url": page["url"], "retrieved_at": page.get("retrieved_at"), "content_sha256": page.get("content_sha256")}
    site_ev = site_ev or {"source_url": home, "retrieved_at": None, "content_sha256": None}
    social = []
    for claim in claims:
        if claim.get("field") == "social_profile" and isinstance(claim.get("value"), dict):
            url = canonical_social(str(claim["value"].get("url") or ""))
            platform = claim["value"].get("platform")
            if url and platform and {"platform": platform, "url": url} not in social:
                social.append({"platform": platform, "url": url})
    pages = []
    seen: set[str] = set()
    home_url = site_ev.get("final_url") or site_ev.get("source_url") or value.get("url")
    if home_url and site_ev.get("content_sha256"):
        pages.append({"url": home_url, "title": value.get("domain") or "", "content_sha256": site_ev["content_sha256"]})
        seen.add(home_url)
    # News-path pages first (the kit's site-news reader takes the deepest one), then pages behind claims.
    site_host = (urlsplit(str(value.get("url") or "")).hostname or "").lower().removeprefix("www.")
    candidates = list((envelope.get("crawl") or {}).get("pages_checked") or [])
    candidates += [{"url": e.get("final_url") or e.get("source_url"), "title": "", "content_sha256": e.get("content_sha256")}
                   for e in envelope.get("evidence", []) if str(e.get("source_class", "")).startswith("company_owned")]
    for page in sorted(candidates, key=lambda p: 0 if NEWS_PATH.search(urlsplit(str(p.get("url") or "")).path) else 1):
        url = str(page.get("url") or "")
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        if not url or url in seen or len(str(page.get("content_sha256") or "")) != 64 or not site_host or not (host == site_host or host.endswith("." + site_host)):
            continue
        pages.append({"url": url, "title": str(page.get("title") or "")[:500], "content_sha256": page["content_sha256"]})
        seen.add(url)
        if len(pages) >= 12:
            break
    for claim in claims:
        if len(pages) >= 12:
            break
        if claim.get("field") not in ("company_news", "careers_page", "job_posting"):
            continue
        detail = claim.get("value")
        for ev_id in claim.get("evidence_ids", []):
            ev = evidence.get(ev_id) or {}
            url = ev.get("final_url") or ev.get("source_url")
            if not url or url in seen or not ev.get("content_sha256") or len(ev["content_sha256"]) != 64:
                continue
            title = detail.get("title") if isinstance(detail, dict) else ""
            pages.append({"url": url, "title": str(title or "")[:500], "content_sha256": ev["content_sha256"]})
            seen.add(url)
        if len(pages) >= 12:
            break
    method = value.get("identity_method")
    return {
        "organisation_number": org,
        "evidence": {
            "website": {
                "field": "website",
                "status": "available",
                "source_type": "company_website",
                "source_class": "company_website",
                "source_url": value.get("url"),
                "retrieved_at": site_ev.get("retrieved_at"),
                "content_sha256": site_ev.get("content_sha256"),
                "value": {
                    "final_url": value.get("url"),
                    "registered_domain": value.get("domain"),
                    "identity_assessment": {"publishable": True, "method": method, "score": site.get("confidence")},
                    "social_links": social,
                    "pages": pages,
                },
                "note": "Built from Kildespor's verified claims; see claims/evidence for spans.",
            }
        },
    }
