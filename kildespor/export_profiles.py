"""Write the web app's dataset (``public/data/profiles.json``) from a run's envelopes.

The JBOX-built Next.js app in this repository is read-only and reads one static
file. This module flattens each terminal envelope into that shape, keeping the
evidence ids so every fact and brief sentence in the app links to its source.

    python -m kildespor.export_profiles --input out/envelopes.jsonl --output public/data/profiles.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

AREA_OF_SECTION = {
    "legal_identity_and_brand": "identity",
    "annual_accounts": "accounts",
    "leadership_and_workplaces": "people",
    "website_and_profiles": "web",
    "hiring_and_activity": "hiring",
}


def _reason(section: dict[str, Any]) -> str:
    reasons = [f.get("reason") for f in section.get("families", {}).values() if f.get("state") == section.get("state") and f.get("reason")]
    return reasons[0] if reasons else ""


def company(env: dict[str, Any]) -> dict[str, Any]:
    claims = [c for c in env.get("claims", []) if c.get("availability") == "available"]
    ident = env.get("legal_identity") or {}

    def by(field: str) -> list[dict[str, Any]]:
        return [c for c in claims if c["field"] == field]

    revenue = None
    accounts = []
    for c in claims:
        if c["field"].startswith("financial.") and isinstance(c["value"], dict) and "amount" in c["value"]:
            v = c["value"]
            accounts.append({
                "type": "group" if v.get("account_type") == "KONSERN" else "company",
                "year": v.get("fiscal_year"),
                "metric": c["field"].split(".", 1)[1],
                "label": c.get("label") or c["field"],
                "amount": v.get("amount"),
                "currency": v.get("currency") or "NOK",
                "sources": c["evidence_ids"],
            })
    company_revenue = sorted((a for a in accounts if a["metric"] == "revenue" and a["type"] == "company"), key=lambda a: a["year"] or "", reverse=True)
    if company_revenue:
        revenue = company_revenue[0]["amount"]
    website = (by("official_website") or [{}])[0].get("value") or {}
    industry = ident.get("industry") or {}
    legal_form = ident.get("legal_form") or {}
    facts = []
    for c in claims:
        if c["family"] in ("legal_identity", "public_brand", "contact_channels") and not c["field"].startswith("financial."):
            value = c["value"]
            if isinstance(value, dict):
                value = value.get("description") or value.get("name") or value.get("amount") or json.dumps(value, ensure_ascii=False)
            facts.append({
                "area": c["family"],
                "field": c["field"],
                "label": c.get("label") or c["field"],
                "value": value,
                "date": c.get("effective_date"),
                "period": c.get("reporting_period"),
                "sources": c["evidence_ids"],
            })
    activity = []
    for c in claims:
        if c["family"] == "public_activity":
            v = c["value"]
            activity.append({"date": v.get("date") or c.get("effective_date"), "title": v.get("title") or v.get("event"), "url": v.get("url"), "kind": c["field"], "sources": c["evidence_ids"]})
    activity.sort(key=lambda a: a["date"] or "", reverse=True)
    syn = env.get("synthesis") or {}
    brief = []
    for section in syn.get("sections", []):
        sources = sorted({e for s in section.get("sentences", []) for e in s.get("evidence_ids", [])}) or section.get("evidence_ids", [])
        brief.append({"heading": section.get("heading"), "text": section.get("text"), "sources": sources})
    return {
        "org": env["organisation_number"],
        "name": ident.get("legal_name"),
        "brand": ident.get("public_brand"),
        "legalForm": legal_form.get("description") if isinstance(legal_form, dict) else legal_form,
        "municipality": ident.get("municipality"),
        "industry": industry.get("description") if isinstance(industry, dict) else industry,
        "employees": ident.get("registered_employees"),
        "website": website.get("url"),
        "revenue": revenue,
        "coverage": {AREA_OF_SECTION[k]: {"state": v.get("state"), "reason": _reason(v)} for k, v in (env.get("sections") or {}).items() if k in AREA_OF_SECTION},
        "brief": brief,
        "signals": [{"text": s.get("text"), "tone": s.get("tone"), "sources": s.get("evidence_ids", [])} for s in syn.get("signals", [])],
        "facts": facts,
        "accounts": accounts,
        "roles": [{"role": c["value"].get("role"), "name": c["value"].get("name"), "since": c.get("effective_date"), "sources": c["evidence_ids"]} for c in by("role")],
        "workplaces": [{"name": c["value"].get("name"), "org": c["value"].get("organisation_number"), "address": c["value"].get("address"), "employees": c["value"].get("employees"), "sources": c["evidence_ids"]} for c in by("registered_workplace")],
        "profiles": [{"platform": c["value"].get("platform"), "url": c["value"].get("url"), "sources": c["evidence_ids"]} for c in by("social_profile")],
        "jobs": [{"title": c["value"].get("title"), "url": c["value"].get("url"), "location": c["value"].get("location"), "published": c["value"].get("published") or c["value"].get("date_posted"), "expires": c["value"].get("expires") or c["value"].get("valid_through"), "sources": c["evidence_ids"]} for c in by("job_posting")],
        "careersPages": [{"url": c["value"], "sources": c["evidence_ids"]} for c in by("careers_page")],
        "activity": activity,
        "changes": [{"type": ch.get("change_type"), "summary": ch.get("summary"), "before": ch.get("previous"), "after": ch.get("current"), "materiality": ch.get("materiality")} for ch in env.get("changes", [])],
        "unknowns": [{"area": u.get("label") or u.get("family"), "state": u.get("state"), "reason": u.get("reason")} for u in syn.get("unknowns", [])],
        "evidence": [
            {
                "id": e["id"],
                "url": e.get("final_url") or e.get("source_url"),
                "retrievedAt": e.get("retrieved_at"),
                "sourceClass": e.get("source_class"),
                "sha256": e.get("content_sha256"),
                "span": e.get("claim_span"),
                "method": e.get("extraction_method"),
            }
            for e in env.get("evidence", [])
        ],
    }


def build(envelopes: list[dict[str, Any]], report: dict[str, Any] | None = None) -> dict[str, Any]:
    run = (envelopes[0].get("run") if envelopes else {}) or {}
    return {
        "run": {"runId": run.get("run_id"), "completedAt": run.get("completed_at"), "count": len(envelopes)},
        "companies": [company(env) for env in envelopes],
    }


def write(envelopes: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(build(envelopes), ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export envelopes to the web app's profiles.json")
    parser.add_argument("--input", default="out/envelopes.jsonl")
    parser.add_argument("--output", default="public/data/profiles.json")
    args = parser.parse_args()
    envelopes = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    out = write(envelopes, Path(args.output))
    print(f"wrote {out} ({len(envelopes)} companies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
