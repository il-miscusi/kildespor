"""Deterministic, evidence-bounded company briefs.

Every sentence is built from published claims and carries the ids of the
claims and evidence it rests on. Nothing is inferred beyond arithmetic on
filed figures (growth, margins, equity ratio), and anything the run could not
establish is listed as unknown with the reason, never filled in.
"""
from __future__ import annotations

from typing import Any

from .model import FAMILIES, CompanyRecord

FAMILY_LABELS = {
    "legal_identity": "legal identity",
    "public_brand": "public brand and self-description",
    "annual_accounts": "filed annual accounts",
    "leadership": "leadership and board",
    "workplaces": "registered workplaces",
    "official_website": "official website",
    "company_profiles": "company-owned social profiles",
    "contact_channels": "contact channels",
    "hiring": "hiring",
    "public_activity": "dated public activity",
}


def _nok(amount: float | None, currency: str = "NOK") -> str:
    if amount is None:
        return "not reported"
    value = float(amount)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1e9:
        text = f"{value / 1e9:.1f} bn"
    elif value >= 1e6:
        text = f"{value / 1e6:.1f} m"
    elif value >= 1e3:
        text = f"{value / 1e3:.0f} k"
    else:
        text = f"{value:.0f}"
    return f"{sign}{currency} {text}"


def _pct(value: float) -> str:
    return f"{value * 100:+.0f}%"


class Builder:
    def __init__(self, rec: CompanyRecord) -> None:
        self.rec = rec
        self.sections: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []

    def claims(self, field: str) -> list[dict[str, Any]]:
        return [c for c in self.rec.claims if c["field"] == field]

    def first(self, field: str) -> dict[str, Any] | None:
        items = self.claims(field)
        return items[0] if items else None

    def add(self, heading: str, sentences: list[tuple[str, list[dict[str, Any]]]]) -> None:
        sentences = [(text, claims) for text, claims in sentences if text]
        if not sentences:
            return
        claim_ids = sorted({c["id"] for _, claims in sentences for c in claims})
        evidence_ids = sorted({e for _, claims in sentences for c in claims for e in c["evidence_ids"]})
        self.sections.append(
            {
                "heading": heading,
                "text": " ".join(text for text, _ in sentences),
                "sentences": [
                    {"text": text, "claim_ids": sorted({c["id"] for c in claims}), "evidence_ids": sorted({e for c in claims for e in c["evidence_ids"]})}
                    for text, claims in sentences
                ],
                "claim_ids": claim_ids,
                "evidence_ids": evidence_ids,
            }
        )

    def signal(self, text: str, claims: list[dict[str, Any]], tone: str = "neutral") -> None:
        self.signals.append({"text": text, "tone": tone, "claim_ids": sorted({c["id"] for c in claims}), "evidence_ids": sorted({e for c in claims for e in c["evidence_ids"]})})


def build_synthesis(rec: CompanyRecord, changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    b = Builder(rec)
    name_c = b.first("legal_name")
    name = name_c["value"] if name_c else rec.organisation_number
    brand_c = b.first("public_brand_name")
    form_c = b.first("legal_form")
    muni_c = b.first("municipality")
    industry = sorted(b.claims("industry_code"), key=lambda c: c["value"].get("rank", 9))
    activity_c = b.first("registered_activity")
    purpose_c = b.first("statutory_purpose")
    desc_c = b.first("self_description")
    founded_c = b.first("founded_date")
    employees_c = b.first("registered_employees")
    status_c = b.first("registry_status")

    # 1. What the company does
    s: list[tuple[str, list[dict[str, Any]]]] = []
    intro = name
    used = [c for c in (name_c,) if c]
    if brand_c and brand_c["value"].strip().lower() not in name.lower():
        intro += f' (public brand "{brand_c["value"]}")'
        used.append(brand_c)
    if form_c:
        intro += f' ({form_c["value"].get("description")}, {form_c["value"].get("code")})'
        used.append(form_c)
    intro += " is registered in " + (muni_c["value"].title() if muni_c else "Norway")
    if muni_c:
        used.append(muni_c)
    if founded_c:
        intro += f", founded {founded_c['value']}"
        used.append(founded_c)
    if industry:
        top = industry[0]["value"]
        intro += f", in the industry {top.get('description')} (NACE {top.get('code')})"
        used.append(industry[0])
    s.append((intro + ".", used))
    if desc_c:
        s.append((f'Its own website describes it as: "{desc_c["value"][:280]}"', [desc_c]))
    if activity_c:
        s.append((f'Registered activity: "{activity_c["value"][:280]}"', [activity_c]))
    elif purpose_c:
        s.append((f'Statutory purpose: "{purpose_c["value"][:280]}"', [purpose_c]))
    if employees_c:
        s.append((f"The Entity Register lists {employees_c['value']} employees" + (f" (as of {employees_c.get('effective_date')})." if employees_c.get("effective_date") else "."), [employees_c]))
    if status_c and status_c["value"] != "active":
        s.append((f"Registry status: {status_c['value']}.", [status_c]))
        b.signal(f"Registry status: {status_c['value']}", [status_c], "negative")
    group = b.claims("group_relationship")
    parents = [c for c in group if c["value"].get("relationship") in ("parent", "ultimate_parent")]
    subs = [c for c in group if c["value"].get("relationship") == "subsidiary"]
    if parents:
        p = parents[0]["value"]
        s.append((f"It belongs to a corporate group; its {p['relationship'].replace('_', ' ')} is {p.get('name')} ({p.get('organisation_number')}).", parents[:1]))
    if subs:
        s.append((f"It has {len(subs)} registered subsidiar{'y' if len(subs) == 1 else 'ies'}, including {', '.join(c['value'].get('name') or '' for c in subs[:3])}.", subs[:3]))
    b.add("What the company does", s)

    # 2. Leadership
    roles = b.claims("role")
    ceo = [c for c in roles if c["value"].get("role_code") == "DAGL"]
    chair = [c for c in roles if c["value"].get("role_code") == "LEDE"]
    board = [c for c in roles if c["value"].get("group_code") == "STYR"]
    auditor = [c for c in roles if c["value"].get("role_code") in ("REVI",)]
    accountant = [c for c in roles if c["value"].get("role_code") in ("REGN",)]
    s = []
    if ceo:
        s.append((f"General manager (daglig leder): {ceo[0]['value']['name']}.", ceo[:1]))
    if chair:
        s.append((f"Chair of the board: {chair[0]['value']['name']}.", chair[:1]))
    if board:
        s.append((f"The register lists {len(board)} board role(s).", board))
    if auditor:
        s.append((f"Auditor: {auditor[0]['value']['name']}.", auditor[:1]))
    if accountant:
        s.append((f"Accountant: {accountant[0]['value']['name']}.", accountant[:1]))
    if not roles:
        s.append(("The register lists no active public roles.", []))
    b.add("Who runs it", s)

    # 3. Where it operates
    addr_c = b.first("registered_business_address")
    units = b.claims("registered_workplace")
    s = []
    if addr_c:
        s.append((f"Business address: {addr_c['value']}.", [addr_c]))
    if units:
        places = sorted({(c["value"].get("municipality") or "").title() for c in units if c["value"].get("municipality")})
        s.append((f"{len(units)} registered workplace(s)" + (f" in {', '.join(places[:6])}" if places else "") + ".", units))
        staffed = [c for c in units if c["value"].get("employees")]
        if staffed:
            top = max(staffed, key=lambda c: c["value"]["employees"])
            s.append((f"The largest registered workplace is {top['value'].get('name')} ({top['value'].get('address')}) with {top['value']['employees']} employees.", [top]))
    b.add("Where it operates", s)

    # 4. Financials
    stmts: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for c in rec.claims:
        if c["field"].startswith("financial.") and "amount" in c["value"]:
            metric = c["field"].split(".", 1)[1]
            key = (c["value"]["account_type"], c["value"]["fiscal_year"])
            stmts.setdefault(key, {})[metric] = c
    s = []
    for account_type in ("KONSERN", "SELSKAP"):
        years = sorted(year for (t, year) in stmts if t == account_type)
        if not years:
            continue
        latest = years[-1]
        cur = stmts[(account_type, latest)]
        label = "company" if account_type == "SELSKAP" else "group (consolidated)"
        parts = []
        used = []
        for metric, text in (("revenue", "revenue"), ("operating_result", "operating result"), ("annual_result", "annual result")):
            if metric in cur:
                parts.append(f"{text} {_nok(cur[metric]['value']['amount'], cur[metric]['value']['currency'])}")
                used.append(cur[metric])
        if parts:
            s.append((f"Latest filed {label} accounts ({latest}): " + ", ".join(parts) + ".", used))
        if "equity" in cur and "total_assets" in cur and cur["total_assets"]["value"]["amount"]:
            ratio = cur["equity"]["value"]["amount"] / cur["total_assets"]["value"]["amount"]
            s.append((f"Equity ratio {ratio * 100:.0f}% (equity {_nok(cur['equity']['value']['amount'])} of total assets {_nok(cur['total_assets']['value']['amount'])}).", [cur["equity"], cur["total_assets"]]))
            if cur["equity"]["value"]["amount"] < 0:
                b.signal(f"Negative equity in {latest} ({label})", [cur["equity"]], "negative")
        if "annual_result" in cur and cur["annual_result"]["value"]["amount"] < 0:
            b.signal(f"Annual loss of {_nok(-cur['annual_result']['value']['amount'])} in {latest} ({label})", [cur["annual_result"]], "negative")
        if len(years) >= 2:
            prev = stmts[(account_type, years[-2])]
            if "revenue" in cur and "revenue" in prev and prev["revenue"]["value"]["amount"] > 0 and cur["revenue"]["value"]["amount"] >= 0:
                growth = cur["revenue"]["value"]["amount"] / prev["revenue"]["value"]["amount"] - 1
                s.append((f"Revenue changed {_pct(growth)} from {years[-2]} ({_nok(prev['revenue']['value']['amount'])}) to {latest}.", [cur["revenue"], prev["revenue"]]))
                if abs(growth) >= 0.2:
                    b.signal(f"Revenue {_pct(growth)} in {latest} vs {years[-2]} ({label})", [cur["revenue"], prev["revenue"]], "positive" if growth > 0 else "negative")
            if "annual_result" in cur and "annual_result" in prev:
                a, p = cur["annual_result"]["value"]["amount"], prev["annual_result"]["value"]["amount"]
                if (a < 0) != (p < 0):
                    s.append((f"The annual result moved from {_nok(p)} in {years[-2]} to {_nok(a)} in {latest}.", [cur["annual_result"], prev["annual_result"]]))
        if "revenue" in cur and "operating_result" in cur and cur["revenue"]["value"]["amount"] > 0:
            margin = cur["operating_result"]["value"]["amount"] / cur["revenue"]["value"]["amount"]
            s.append((f"Operating margin {margin * 100:.1f}% in {latest}.", [cur["operating_result"], cur["revenue"]]))
    if not s:
        filed = b.first("latest_filed_accounts_year")
        if filed:
            s.append((f"The register records annual accounts filed for {filed['value']}, but no normalised figures were returned by the accounts API.", [filed]))
    b.add("Latest filed numbers", s)

    # 5. Hiring
    jobs = b.claims("job_posting")
    careers = b.claims("careers_page")
    s = []
    if jobs:
        titles = "; ".join(c["value"].get("title", "")[:70] for c in jobs[:3])
        s.append((f"{len(jobs)} active job ad(s) verified to this organisation number, e.g. {titles}.", jobs))
        b.signal(f"Hiring: {len(jobs)} active job ad(s)", jobs, "positive")
    if careers:
        s.append((f"Careers page: {careers[0]['value']}.", careers[:1]))
    b.add("Working here", s)

    # 6. Public footprint and activity
    site = b.first("official_website")
    socials = b.claims("social_profile")
    news = sorted(b.claims("company_news"), key=lambda c: c["value"].get("date") or "", reverse=True)
    reg_events = sorted(b.claims("registry_role_change") + b.claims("registry_update"), key=lambda c: c.get("effective_date") or "", reverse=True)
    s = []
    if site:
        method = site["value"].get("identity_method", "")
        if "organisation_number" in method:
            proof = "the site shows its organisation number"
        elif method.startswith("nav_listed"):
            proof = "the employer gave it as its homepage in a job ad verified to this organisation number"
        else:
            proof = "it is the website listed in the Entity Register"
        rel = " It is labelled as a group site, not an entity-specific site." if site.get("relationship") == "group_site" else ""
        s.append((f"Official website {site['value']['url']} ({proof}).{rel}", [site]))
    if socials:
        s.append(("Company-owned profiles linked from the website: " + ", ".join(sorted({c["value"]["platform"].title() for c in socials})) + ".", socials))
    if news:
        latest = news[0]["value"]
        s.append((f"Latest dated item on the company's own channels: \"{latest.get('title', '')[:120]}\" ({latest.get('date')}).", news[:1]))
    if reg_events:
        ev = reg_events[0]
        s.append((f"Most recent register change: {ev['value'].get('event')} on {ev['value'].get('date')}.", [ev]))
    b.add("Public footprint and recent activity", s)

    # 7. What changed: since the previous run, and dated changes visible in the sources themselves.
    dated: list[tuple[str, str, list[dict[str, Any]]]] = []
    for c in b.claims("registry_role_change"):
        dated.append((c["value"].get("date") or "", f"{c['value'].get('event')} ({c['value'].get('date')})", [c]))
    for c in b.claims("registered_workplace"):
        started = c["value"].get("started")
        if started:
            dated.append((started, f"workplace {c['value'].get('name')} registered as started {started}", [c]))
    for c in b.claims("former_name"):
        if c["value"].get("to"):
            dated.append((c["value"]["to"], f"name changed from {c['value'].get('name')} ({c['value'].get('to')})", [c]))
    cap = b.first("share_capital")
    if cap and cap.get("effective_date"):
        dated.append((cap["effective_date"], f"share capital registered as {_nok(cap['value'].get('amount'), cap['value'].get('currency') or 'NOK')} from {cap['effective_date']}", [cap]))
    if employees_c and employees_c.get("effective_date"):
        dated.append((employees_c["effective_date"], f"registered employee count updated to {employees_c['value']} ({employees_c['effective_date']})", [employees_c]))
    for c in b.claims("job_posting"):
        when = c["value"].get("published") or c["value"].get("date_posted")
        if when:
            dated.append((when, f"job ad published: {c['value'].get('title', '')[:60]} ({when})", [c]))
    for c in b.claims("company_news"):
        if c["value"].get("date"):
            dated.append((c["value"]["date"], f"news: {c['value'].get('title', '')[:70]} ({c['value']['date']})", [c]))
    filed = sorted({(t, y) for (t, y) in stmts})
    for account_type in ("KONSERN", "SELSKAP"):
        years = sorted(y for (t, y) in filed if t == account_type)
        if len(years) >= 2:
            cur, prev = stmts[(account_type, years[-1])], stmts[(account_type, years[-2])]
            parts, used = [], []
            for metric, word in (("revenue", "revenue"), ("annual_result", "annual result"), ("equity", "equity")):
                if metric in cur and metric in prev:
                    a, p_ = cur[metric]["value"]["amount"], prev[metric]["value"]["amount"]
                    parts.append(f"{word} {_nok(p_)} → {_nok(a)}")
                    used += [cur[metric], prev[metric]]
            if parts:
                label = "group" if account_type == "KONSERN" else "company"
                dated.append((f"{years[-1]}-12-31", f"{years[-1]} {label} accounts vs {years[-2]}: " + ", ".join(parts), used))
                break
    dated.sort(key=lambda item: item[0], reverse=True)
    s = []
    if changes:
        material = [c for c in changes if c.get("materiality") in ("high", "medium")]
        lines = [c.get("summary") for c in (material or changes)[:5] if c.get("summary")]
        ev_ids = sorted({e for c in changes for e in c.get("current_evidence_ids", []) + c.get("previous_evidence_ids", [])})
        s.append((f"Since the previous run: {len(changes)} change(s)" + (f" — {'; '.join(lines)}." if lines else "."), [{"id": c.get("current_claim_id") or c.get("previous_claim_id") or "", "evidence_ids": ev_ids} for c in changes[:1]]))
    elif rec.context.get("refresh_baseline", True):
        s.append(("This run is the first snapshot for this company, so no run-to-run changes are reported yet.", []))
    if dated:
        s.append(("Recent dated changes in the sources: " + "; ".join(text for _, text, _ in dated[:6]) + ".", [c for _, _, cs in dated[:6] for c in cs]))
    b.add("What changed", s)

    # 8. Unknowns
    unknown = []
    for family in FAMILIES:
        state = rec.family_state.get(family, {})
        if state.get("state") and state["state"] != "available":
            unknown.append({"family": family, "label": FAMILY_LABELS.get(family, family), "state": state["state"], "reason": state.get("reason")})
    if unknown:
        text = "Not established in this run: " + "; ".join(f"{u['label']} ({u['state']}: {u['reason']})" for u in unknown) + "."
    else:
        text = "Every profile area has at least one evidence-backed fact."
    b.sections.append({"heading": "What remains unknown", "text": text, "sentences": [], "claim_ids": [], "evidence_ids": []})

    headline_parts = [name]
    if industry:
        headline_parts.append(industry[0]["value"].get("description") or "")
    if muni_c:
        headline_parts.append(muni_c["value"].title())
    return {
        "headline": " · ".join(p for p in headline_parts if p),
        "sections": b.sections,
        "signals": b.signals,
        "unknowns": unknown,
        "method": "deterministic template over published claims; no language model",
    }
