"""Terminal envelope: one per input organisation number."""
from __future__ import annotations

from typing import Any

from . import __version__
from .model import FAMILIES, SECTIONS, CompanyRecord

SCHEMA = "kildespor.envelope.v1"
PRIORITY = ["available", "failed", "blocked", "ambiguous", "not_available", "not_applicable"]


def _section_state(states: list[str]) -> str:
    for state in PRIORITY:
        if state in states:
            return state
    return "failed"


def identity_summary(rec: CompanyRecord) -> dict[str, Any]:
    def value(field: str) -> Any:
        for claim in rec.claims:
            if claim["field"] == field and not claim.get("historical"):
                return claim["value"]
        return None

    industry = [c["value"] for c in rec.claims if c["field"] == "industry_code"]
    industry.sort(key=lambda v: v.get("rank", 9))
    website = value("official_website")
    return {
        "organisation_number": rec.organisation_number,
        "legal_name": value("legal_name"),
        "public_brand": value("public_brand_name"),
        "legal_form": value("legal_form"),
        "business_address": value("registered_business_address"),
        "municipality": value("municipality"),
        "industry": industry[0] if industry else None,
        "registered_employees": value("registered_employees"),
        "registry_status": value("registry_status"),
        "official_website": website.get("url") if isinstance(website, dict) else None,
    }


def build_envelope(
    rec: CompanyRecord,
    *,
    run: dict[str, Any],
    synthesis: dict[str, Any],
    changes: list[dict[str, Any]],
    refresh_meta: dict[str, Any],
) -> dict[str, Any]:
    with rec._lock:
        claims = [dict(c) for c in rec.claims]
        evidence = dict(rec.evidence)
        family_state = {k: dict(v) for k, v in rec.family_state.items()}
        errors = list(rec.errors)

    sections: dict[str, Any] = {}
    for section, families in SECTIONS.items():
        fam_out = {}
        for family in families:
            state = family_state.get(family) or {"state": "failed", "reason": "not attempted within the run's time budget"}
            fam_out[family] = {
                "state": state["state"],
                "reason": state.get("reason"),
                "checked_sources": state.get("checked_sources", []),
                "claim_count": sum(1 for c in claims if c["family"] == family),
            }
        sections[section] = {"state": _section_state([f["state"] for f in fam_out.values()]), "families": fam_out}

    # Explicit availability rows for every family that has no published claim.
    for family in FAMILIES:
        if any(c["family"] == family for c in claims):
            continue
        state = family_state.get(family) or {"state": "failed", "reason": "not attempted within the run's time budget"}
        claims.append(
            {
                "id": f"na-{rec.organisation_number}-{family}",
                "key": f"{family}:availability",
                "family": family,
                "field": family,
                "value": None,
                "availability": state["state"],
                "reason": state.get("reason"),
                "checked_sources": state.get("checked_sources", []),
                "evidence_ids": [],
            }
        )

    used = {e for c in claims for e in c.get("evidence_ids", [])}
    used |= {e for change in changes for e in change.get("current_evidence_ids", [])}
    evidence_list = [evidence[e] for e in sorted(evidence) if e in used]
    identity_state = (family_state.get("legal_identity") or {}).get("state", "failed")
    return {
        "schema_version": SCHEMA,
        "organisation_number": rec.organisation_number,
        "state": "available" if identity_state == "available" else identity_state,
        "run": run,
        "legal_identity": identity_summary(rec),
        "sections": sections,
        "claims": claims,
        "evidence": evidence_list,
        "synthesis": synthesis,
        "changes": changes,
        "refresh": refresh_meta,
        "errors": errors,
        "operations": {
            "requests": rec.requests,
            "runtime_ms": rec.runtime_ms,
            "third_party_cost_usd": 0.0,
            "agent_version": __version__,
        },
    }


def placeholder_envelope(org: str, run: dict[str, Any], reason: str) -> dict[str, Any]:
    """Envelope for an input the agent could not process at all (still terminal)."""
    rec = CompanyRecord(org)
    for family in FAMILIES:
        rec.set_state(family, "failed", reason)
    rec.error("run", reason)
    return build_envelope(rec, run=run, synthesis={"headline": org, "sections": [], "signals": [], "unknowns": [], "method": "none"}, changes=[], refresh_meta={"baseline": True, "history": [run["run_id"]]})
