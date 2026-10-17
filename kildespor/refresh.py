"""Refresh as a diff against the previous envelope.

Claims have stable keys, so a refresh is a comparison of two keyed sets:

* a single-valued field whose value differs is ``changed``;
* a key present now and not before is ``added`` (new role, new job, new
  workplace, new filing, new profile, new news item);
* a key present before and not now is ``removed`` (closed job, ended role),
  but only when this run successfully checked that family. When the family
  failed this run, the previous claims are carried forward unchanged and
  marked stale rather than reported as removals.

Every change carries the previous and current values and both sides'
evidence records, so earlier evidence is preserved. Running the same
snapshot twice yields no changes (idempotent), and ``first_observed_at`` is
inherited from the earliest run that saw the claim.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .http import utc_now
from .contract import internal
from .model import CompanyRecord, canonical

SINGLE_VALUED = {
    "legal_name", "legal_form", "registered_business_address", "postal_address", "municipality", "registered_employees",
    "share_capital", "statutory_purpose", "registered_activity", "vat_registered", "in_business_register", "part_of_group",
    "registry_status", "latest_filed_accounts_year", "registry_website", "official_website", "public_brand_name", "self_description",
}
FIELD_CHECKED = {"job_posting", "careers_page", "social_profile", "company_news", "website_phone", "website_email", "public_brand_name", "self_description", "official_website"}
HIGH = {"legal_name", "registry_status", "registered_business_address", "official_website", "latest_filed_accounts_year", "legal_form"}
CHANGE_NAMES = {
    "role": ("new_role", "ended_role"),
    "registered_workplace": ("new_location", "closed_location"),
    "job_posting": ("new_job", "closed_job"),
    "social_profile": ("new_profile", "removed_profile"),
    "company_news": ("new_activity", "removed_activity"),
    "registry_update": ("new_registry_update", "removed_registry_update"),
    "registry_role_change": ("new_role_change", "removed_role_change"),
    "group_relationship": ("new_group_link", "ended_group_link"),
}


def load_previous(path: str | None) -> dict[str, dict[str, Any]]:
    if not path or not Path(path).exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
            except ValueError:
                continue
            org = envelope.get("organisation_number")
            if org:
                out[org] = internal(envelope)
    return out


def _comparable(field: str, value: Any) -> str:
    if field == "official_website" and isinstance(value, dict):
        return str(value.get("domain") or value.get("url"))
    return canonical(value)


def _short(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("title", "name", "url", "amount", "event"):
            if key in value:
                return str(value[key])[:80]
    return str(value)[:80]


def diff(rec: CompanyRecord, previous: dict[str, Any] | None, run_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now = utc_now()
    if not previous:
        for claim in rec.claims:
            claim["first_observed_at"] = now
            claim["last_observed_at"] = now
        return [], {"previous_run_id": None, "baseline": True, "history": [run_id]}

    prev_claims = {c["key"]: c for c in previous.get("claims", []) if c.get("availability") == "available"}
    prev_evidence = {e["id"]: e for e in previous.get("evidence", [])}
    cur_claims = {c["key"]: c for c in rec.claims}
    # A family checked this run (including an explicit identity mismatch, "ambiguous") reports removals;
    # only a failed or blocked check carries the last supported value forward.
    family_ok = {family: (state.get("state") in ("available", "not_available", "not_applicable", "ambiguous")) for family, state in rec.family_state.items()}

    def removable(claim: dict[str, Any]) -> bool:
        # Fields fed by several sources are removed only when their own source was checked
        # completely this run; a family that is "available" for another reason is not enough.
        if claim["field"] in FIELD_CHECKED:
            return claim["field"] in rec.checked_fields
        return bool(family_ok.get(claim["family"]))
    changes: list[dict[str, Any]] = []

    def evidence_of(claim: dict[str, Any], pool: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [pool[e] for e in claim.get("evidence_ids", []) if e in pool]

    # Carry first_observed_at forward.
    for key, claim in cur_claims.items():
        old = prev_claims.get(key)
        claim["first_observed_at"] = (old or {}).get("first_observed_at") or now
        claim["last_observed_at"] = now

    # Single-valued fields compare by field name.
    prev_by_field: dict[str, list[dict[str, Any]]] = {}
    for claim in prev_claims.values():
        prev_by_field.setdefault(claim["field"], []).append(claim)
    cur_by_field: dict[str, list[dict[str, Any]]] = {}
    for claim in cur_claims.values():
        cur_by_field.setdefault(claim["field"], []).append(claim)

    for field in sorted(SINGLE_VALUED):
        before = prev_by_field.get(field, [])
        after = cur_by_field.get(field, [])
        if before and after:
            b, a = before[0], after[0]
            if _comparable(field, b["value"]) != _comparable(field, a["value"]):
                changes.append(
                    {
                        "change_type": f"changed_{field}",
                        "field": field,
                        "family": a["family"],
                        "previous": b["value"],
                        "current": a["value"],
                        "previous_claim_id": b["id"],
                        "current_claim_id": a["id"],
                        "previous_evidence_ids": b.get("evidence_ids", []),
                        "current_evidence_ids": a.get("evidence_ids", []),
                        "previous_evidence": evidence_of(b, prev_evidence),
                        "materiality": "high" if field in HIGH else "medium",
                        "summary": f"{field.replace('_', ' ')} changed from {_short(b['value'])} to {_short(a['value'])}",
                        "detected_at": now,
                    }
                )
        elif after and not before:
            a = after[0]
            changes.append({"change_type": f"new_{field}", "field": field, "family": a["family"], "previous": None, "current": a["value"], "current_claim_id": a["id"], "current_evidence_ids": a.get("evidence_ids", []), "previous_evidence_ids": [], "materiality": "medium" if field in HIGH else "low", "summary": f"{field.replace('_', ' ')} now found: {_short(a['value'])}", "detected_at": now})
        elif before and not after:
            b = before[0]
            if removable(b):
                changes.append({"change_type": f"removed_{field}", "field": field, "family": b["family"], "previous": b["value"], "current": None, "previous_claim_id": b["id"], "previous_evidence_ids": b.get("evidence_ids", []), "previous_evidence": evidence_of(b, prev_evidence), "current_evidence_ids": [], "materiality": "high" if field in HIGH else "medium", "summary": f"{field.replace('_', ' ')} no longer found (was {_short(b['value'])})", "detected_at": now})
            else:
                _carry(rec, b, prev_evidence)

    # Multi-valued fields compare by key; the same key with a different value is an update.
    for key, claim in cur_claims.items():
        if claim["field"] in SINGLE_VALUED:
            continue
        if key in prev_claims:
            old = prev_claims[key]
            if canonical(old["value"]) != canonical(claim["value"]):
                restated = claim["field"].startswith("financial.")
                changes.append({"change_type": "restated_figure" if restated else f"updated_{claim['field']}", "field": claim["field"], "family": claim["family"], "previous": old["value"], "current": claim["value"], "previous_claim_id": old["id"], "current_claim_id": claim["id"], "previous_evidence_ids": old.get("evidence_ids", []), "previous_evidence": evidence_of(old, prev_evidence), "current_evidence_ids": claim.get("evidence_ids", []), "materiality": "high" if restated else "low", "summary": f"{claim['field'].replace('_', ' ')} updated: {_short(old['value'])} -> {_short(claim['value'])}", "detected_at": now})
            continue
        if claim["field"].startswith("financial."):
            change_type, materiality = "new_filing", "high"
        else:
            change_type = CHANGE_NAMES.get(claim["field"], (f"new_{claim['field']}", ""))[0]
            materiality = "medium" if claim["field"] in ("role", "registered_workplace", "job_posting", "group_relationship") else "low"
        changes.append({"change_type": change_type, "field": claim["field"], "family": claim["family"], "previous": None, "current": claim["value"], "current_claim_id": claim["id"], "current_evidence_ids": claim.get("evidence_ids", []), "previous_evidence_ids": [], "materiality": materiality, "summary": f"{change_type.replace('_', ' ')}: {_short(claim['value'])}", "detected_at": now})
    for key, claim in prev_claims.items():
        if claim["field"] in SINGLE_VALUED or key in cur_claims:
            continue
        if claim["field"].startswith("financial."):
            # Older filings roll out of the API's three-year window; keep them as history, not as removals.
            _carry(rec, claim, prev_evidence, stale=False)
            continue
        if not removable(claim):
            _carry(rec, claim, prev_evidence)
            continue
        change_type = CHANGE_NAMES.get(claim["field"], ("", f"removed_{claim['field']}"))[1]
        materiality = "medium" if claim["field"] in ("role", "registered_workplace", "job_posting", "group_relationship") else "low"
        changes.append({"change_type": change_type, "field": claim["field"], "family": claim["family"], "previous": claim["value"], "current": None, "previous_claim_id": claim["id"], "previous_evidence_ids": claim.get("evidence_ids", []), "previous_evidence": evidence_of(claim, prev_evidence), "current_evidence_ids": [], "materiality": materiality, "summary": f"{change_type.replace('_', ' ')}: {_short(claim['value'])}", "detected_at": now})

    prev_refresh = previous.get("refresh") or {}
    history = list(prev_refresh.get("history") or [])
    prev_run = (previous.get("run") or {}).get("run_id")
    if prev_run and prev_run not in history:
        history.append(prev_run)
    if run_id not in history:
        history.append(run_id)
    meta = {
        "previous_run_id": prev_run,
        "previous_completed_at": (previous.get("run") or {}).get("completed_at"),
        "baseline": False,
        "history": history[-20:],
        "change_count": len(changes),
    }
    changes.sort(key=lambda c: ({"high": 0, "medium": 1, "low": 2}.get(c["materiality"], 3), c["change_type"], canonical(c.get("current") or c.get("previous"))))
    return changes, meta


def _carry(rec: CompanyRecord, claim: dict[str, Any], prev_evidence: dict[str, dict[str, Any]], stale: bool = True) -> None:
    """Keep the last known supported value when this run could not re-check it."""
    carried = dict(claim)
    if stale:
        carried["stale"] = True
        carried["carried_forward_reason"] = "this run could not re-check the source; last supported value kept"
    else:
        carried["historical"] = True
    with rec._lock:
        if any(c["id"] == carried["id"] for c in rec.claims):
            return
        rec.claims.append(carried)
        for ev_id in carried.get("evidence_ids", []):
            if ev_id in prev_evidence and ev_id not in rec.evidence:
                rec.evidence[ev_id] = prev_evidence[ev_id]
