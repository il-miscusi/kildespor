"""Write the web app's dataset (``public/data/profiles.json``) from a run's envelopes.

The JBOX-built Next.js app in this repository is read-only and reads one static
file. This module flattens each terminal envelope into that shape, keeping the
evidence ids so every fact and brief sentence in the app links to its source.
Envelopes are read in either shape: in-process (dict values) or as written to
disk (scalar ``value`` with the dict in ``detail``, fields ``hiring_signal`` and
``dated_news``). Carried-forward facts keep their marker, and both sides of a
change keep their evidence.

Every run writes ``<output dir>/profiles.json``. To show that run in the app:

    python -m kildespor.export_profiles --input out/envelopes.jsonl --output public/data/profiles.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .site import claims_of, synthesis_of
from .synthesis import job_date, news_date

STALE_NOTE = " (not re-checked this run; carried forward)"


def _family_states(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {fam: info for sec in (env.get("sections") or {}).values() for fam, info in (sec.get("families") or {}).items()}


def _coverage(claims: list[dict[str, Any]], families: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    """The app's five areas, each decided by its own facts: register updates do not make a company 'hiring'."""
    def has(*fields: str) -> bool:
        return any(c["field"] in fields or any(c["field"].startswith(f) for f in fields if f.endswith(".")) for c in claims)

    def state(family: str, found: bool, why: str) -> dict[str, str]:
        if found:
            return {"state": "available", "reason": why}
        info = families.get(family) or {}
        st = info.get("state") or "failed"
        reason = info.get("reason") or "not checked in this run"
        if st == "available":  # the family is available for another reason than this area's facts
            st, reason = "not_available", "no evidence-backed fact for this area"
        return {"state": st, "reason": reason}

    people = has("role", "registered_workplace")
    return {
        "identity": state("legal_identity", has("legal_name"), "entity register record"),
        "accounts": state("annual_accounts", has("financial."), "filed annual accounts"),
        "people": state("leadership" if not has("registered_workplace") else "workplaces", people, "registered roles or workplaces"),
        "web": state("official_website", has("official_website", "social_profile"), "verified website or company-linked profiles"),
        "hiring": state("hiring", has("job_posting", "careers_page"), "job ads or a careers page"),
    }


def _text(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("title", "name", "url", "event", "description"):
            if value.get(key):
                return value[key]
        if "amount" in value:
            return value.get("amount")
        return json.dumps(value, ensure_ascii=False)
    return value


def company(env: dict[str, Any]) -> dict[str, Any]:
    claims = [c for c in claims_of(env) if c.get("availability") == "available"]
    ident = env.get("legal_identity") or {}
    families = _family_states(env)

    def by(field: str) -> list[dict[str, Any]]:
        return [c for c in claims if c["field"] == field]

    def label(c: dict[str, Any], default: str) -> str:
        return (c.get("label") or default) + (STALE_NOTE if c.get("stale") else "")

    revenue = None
    accounts = []
    for c in claims:
        if c["field"].startswith("financial.") and isinstance(c["value"], dict) and "amount" in c["value"]:
            v = c["value"]
            accounts.append({
                "type": "group" if v.get("account_type") == "KONSERN" else "company",
                "year": int(v["fiscal_year"]) if str(v.get("fiscal_year") or "").isdigit() else v.get("fiscal_year"),
                "metric": c["field"].split(".", 1)[1],
                "label": c.get("label") or c["field"],
                "amount": v.get("amount"),
                "currency": v.get("currency") or "NOK",
                "sources": c["evidence_ids"],
            })
    # Equity ratio is arithmetic on two filed figures, never a filed figure itself: it is
    # labelled as computed and cites both inputs.
    for kind in ("company", "group"):
        rows = [a for a in accounts if a["type"] == kind]
        for year in sorted({a["year"] for a in rows}, key=str):
            equity = next((a for a in rows if a["year"] == year and a["metric"] == "equity"), None)
            assets = next((a for a in rows if a["year"] == year and a["metric"] == "total_assets"), None)
            if equity and assets and assets["amount"] and equity["currency"] == assets["currency"]:
                accounts.append({
                    "type": kind,
                    "year": year,
                    "metric": "equity_ratio",
                    "label": "Equity ratio (computed: equity / total assets)",
                    "amount": round(100.0 * equity["amount"] / assets["amount"], 1),
                    "currency": "%",
                    "sources": sorted(set(equity["sources"]) | set(assets["sources"])),
                })
    company_revenue = sorted((a for a in accounts if a["metric"] == "revenue" and a["type"] == "company"), key=lambda a: str(a["year"] or ""), reverse=True)
    if company_revenue:
        revenue = company_revenue[0]["amount"]
    website_claim = (by("official_website") or [None])[0]
    website = website_claim["value"] if website_claim else None
    website_url = website.get("url") if isinstance(website, dict) else website
    industry = ident.get("industry") or {}
    legal_form = ident.get("legal_form") or {}
    facts = []
    for c in claims:
        if c["family"] in ("legal_identity", "public_brand", "contact_channels") and not c["field"].startswith("financial."):
            facts.append({
                "area": c["family"],
                "field": c["field"],
                "label": label(c, c["field"]),
                "value": _text(c["value"]),
                "date": c.get("effective_date"),
                "period": c.get("reporting_period"),
                "claimId": c.get("id"),
                "stale": bool(c.get("stale")),
                "sources": c["evidence_ids"],
            })
    # The app resolves header values through facts named like its fields; give it those names.
    def alias(field: str, area: str, name: str, value: Any, claim: dict[str, Any] | None) -> None:
        if claim is not None and value not in (None, ""):
            facts.append({"area": area, "field": field, "label": label(claim, name), "value": value, "date": claim.get("effective_date"), "period": claim.get("reporting_period"), "claimId": claim.get("id"), "stale": bool(claim.get("stale")), "sources": claim["evidence_ids"]})

    industry_claims = sorted((c for c in by("industry_code") if isinstance(c["value"], dict)), key=lambda c: c["value"].get("rank", 9))
    if industry_claims:
        alias("industry", "identity", "Industry", industry_claims[0]["value"].get("description"), industry_claims[0])
    employees_claim = (by("registered_employees") or [None])[0]
    if employees_claim is not None:
        alias("employees", "people", "Employees (register)", employees_claim["value"], employees_claim)
    if website_claim is not None:
        alias("website", "web", "Official website", website_url, website_claim)
    accounts.sort(key=lambda a: (a["type"] != "company",))
    activity = []
    for c in claims:
        if c["field"] == "company_news":
            v = c["value"] if isinstance(c["value"], dict) else {}
            activity.append({"date": news_date(v) or c.get("effective_date"), "title": v.get("title"), "url": v.get("url"), "kind": "dated news (company website)", "sources": c["evidence_ids"]})
        elif c["family"] == "public_activity" and isinstance(c["value"], dict):
            v = c["value"]
            activity.append({"date": v.get("date") or c.get("effective_date"), "title": v.get("title") or v.get("event"), "url": v.get("url"), "kind": "register event", "sources": c["evidence_ids"]})
    activity.sort(key=lambda a: a["date"] or "", reverse=True)
    syn = synthesis_of(env)
    brief = []
    if syn.get("summary_sentences"):
        brief.append({"heading": "In 20 seconds", "text": syn.get("summary"), "sources": sorted({e for s in syn["summary_sentences"] for e in s.get("evidence_ids", [])})})
    for section in syn.get("sections", []):
        sources = sorted({e for s in section.get("sentences", []) for e in s.get("evidence_ids", [])}) or section.get("evidence_ids", [])
        brief.append({"heading": section.get("question") or section.get("heading"), "text": section.get("text"), "sources": sources})
    jobs = []
    for c in by("job_posting"):
        v = c["value"] if isinstance(c["value"], dict) else {"url": c["value"]}
        jobs.append({"title": v.get("title"), "url": v.get("url"), "location": v.get("location"), "published": job_date(v) or None, "expires": v.get("expires") or v.get("valid_through"), "sources": c["evidence_ids"]})
    profiles = []
    for c in by("social_profile"):
        v = c["value"] if isinstance(c["value"], dict) else {"url": c["value"]}
        profiles.append({"platform": v.get("platform"), "url": v.get("url"), "sources": c["evidence_ids"]})
    return {
        "org": env["organisation_number"],
        "name": ident.get("legal_name") or env["organisation_number"],
        "brand": ident.get("public_brand"),
        "legalForm": legal_form.get("description") if isinstance(legal_form, dict) else legal_form,
        "municipality": ident.get("municipality"),
        "industry": industry.get("description") if isinstance(industry, dict) else industry,
        "employees": ident.get("registered_employees"),
        "website": website_url,
        "revenue": revenue,
        "coverage": _coverage(claims, families),
        "brief": brief,
        "signals": [{"text": s.get("text"), "tone": s.get("tone"), "sources": s.get("evidence_ids", [])} for s in syn.get("signals", [])],
        "facts": facts,
        "accounts": accounts,
        "roles": [{"role": c["value"].get("role"), "name": c["value"].get("name"), "since": c.get("effective_date"), "sources": c["evidence_ids"]} for c in by("role") if isinstance(c["value"], dict)],
        "workplaces": [{"name": c["value"].get("name"), "org": c["value"].get("organisation_number"), "address": c["value"].get("address"), "employees": c["value"].get("employees"), "sources": c["evidence_ids"]} for c in by("registered_workplace") if isinstance(c["value"], dict)],
        "profiles": profiles,
        "jobs": jobs,
        "careersPages": [{"url": c["value"] if isinstance(c["value"], str) else (c["value"] or {}).get("url"), "sources": c["evidence_ids"]} for c in by("careers_page")],
        "activity": activity,
        # Both sides of every change keep their evidence: sources lists the current and the previous records.
        "changes": [
            {"type": ch.get("change_type"), "summary": ch.get("summary"), "before": _text(ch.get("previous")), "after": _text(ch.get("current")), "materiality": ch.get("materiality"), "detected": ch.get("detected_at"), "sources": list(dict.fromkeys((ch.get("current_evidence_ids") or []) + (ch.get("previous_evidence_ids") or [])))}
            for ch in env.get("changes", [])
        ],
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
