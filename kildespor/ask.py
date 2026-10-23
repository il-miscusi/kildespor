"""Answer questions over a run's envelopes, deterministically and with citations.

    python -m kildespor.ask --envelopes out/envelopes.jsonl "hiring companies in Oslo"
    python -m kildespor.ask --envelopes out/envelopes.jsonl "revenue over 10 MNOK"
    python -m kildespor.ask --envelopes out/envelopes.jsonl "companies with a website but no social profile"
    python -m kildespor.ask --envelopes out/envelopes.jsonl "what changed"
    python -m kildespor.ask --envelopes out/envelopes.jsonl "who is the CEO of 925181536"
    python -m kildespor.ask --envelopes out/envelopes.jsonl --json "is SPEDIFY AS hiring"

Two kinds of question are supported, with the same closed grammar as the
viewer's question box (``#/research``):

* screening over every company in the run: municipality, industry, legal form,
  revenue and employee ranges, profit or loss, negative equity, website, social
  profiles, hiring signal, dated news, coverage, changes and carried-forward
  facts, ``top N by revenue|employees``;
* questions about one company named by organisation number or name: a brief,
  what it does, how it is doing, who runs or owns it, whether it is hiring
  (and how many people work there), what it published recently and where it
  is online, what changed and what is unknown. Each topic is answered by the
  matching section of the company's cited brief.

``python -m kildespor.serve`` puts the same answers behind ``/research?q=...``.

Every answer lists the claims it used with their source URL and retrieval
time. A question outside the grammar, or one the evidence cannot answer, gets
an explicit "not supported by the evidence" answer; nothing is guessed, and a
missing value is never read as a negative fact.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .site import claims_of, index_row, synthesis_of

UNSUPPORTED = {
    "sentiment": "sentiment is not measured",
    "review": "reviews and ratings are not collected",
    "rating": "reviews and ratings are not collected",
    "glassdoor": "Glassdoor is not a permitted source",
    "salary": "salaries are not collected",
    "traffic": "website traffic is not collected",
    "linkedin": "LinkedIn data is used only where a verified company site links to it",
    "stock": "share prices are not collected",
    "price": "prices are not collected",
}
SECTORS = {
    "construction": ["41", "42", "43"], "builder": ["41", "42", "43"], "restaurant": ["56"], "food": ["10", "56"],
    "real estate": ["68"], "property": ["68"], "software": ["58", "62", "63"], "it": ["62", "63"], "tech": ["58", "62", "63"],
    "retail": ["47"], "shop": ["47"], "wholesale": ["46"], "transport": ["49", "50", "51", "52", "53"], "shipping": ["50", "52"],
    "health": ["86", "87", "88"], "consult": ["70", "71", "74"], "holding": ["64"], "finance": ["64", "65", "66"],
    "accounting": ["69"], "legal": ["69"], "hotel": ["55"], "farming": ["01"], "fishing": ["03"], "aquaculture": ["03"],
    "energy": ["35"], "manufactur": [f"{n}" for n in range(10, 34)], "education": ["85"], "cleaning": ["81"],
    "media": ["58", "59", "60"], "research": ["72"], "marketing": ["73"], "car": ["45"], "housing": ["97", "98", "68"],
}
FORMS = {"sole proprietorship": "ENK", "sole proprietorships": "ENK", "public limited": "ASA", "cooperative": "SA", "cooperatives": "SA", "foundation": "STI", "foundations": "STI"}
CITE_FOR = {"m": "municipality", "hire": "hiring", "w": "website", "so": "social", "news": "news", "r": "revenue", "e": "employees", "loss": "result", "profit": "result", "negeq": "equity", "dv": "industry", "ic": "industry", "f": "name"}


TOPICS = [
    # (topic, pattern), checked in order; the topic names the brief's section that answers it.
    ("brief", r"\b(brief|summary|overview|useful|tell me about|in short)\b"),
    ("who", r"\b(who runs|who leads|who is behind|chair|board|owner|owns|owned|parent|leader|leadership|auditor|accountant|runs)\b"),
    ("hiring", r"\b(hiring|hire|jobs?|vacanc|recruit|careers?|work there|work here|working here|apply|employees|staff|join)\b"),
    ("activity", r"\b(publish|published|recent|recently|activity|news|posts?|online|website|web site|homepage|social|facebook|instagram)\b"),
    ("money", r"\b(revenue|turnover|profit|loss|result|equity|financ|accounts|filing|doing|money|earn|size|big|perform)\b"),
    ("changed", r"\b(changed?|changes|new|latest|happen|refresh)\b"),
    ("unknown", r"\b(unknown|missing|not found|gaps?|could not)\b"),
    ("what", r"\b(what does|what do|do they|business|industry|about|what is|describe)\b"),
]


def topic_of(question: str) -> str | None:
    """Which section of the brief answers a question about one company (the viewer's topicOf)."""
    l = question.lower()
    return next((topic for topic, pattern in TOPICS if re.search(pattern, l)), None)


def _num(text: str, unit: str | None) -> float:
    value = float(re.sub(r"\s", "", text).replace(",", "."))
    unit = (unit or "").lower()
    if re.match(r"^(b|bn|billion|mrd|bnok)", unit):
        value *= 1e9
    elif re.match(r"^(m|mn|mill|million|mnok)", unit):
        value *= 1e6
    elif re.match(r"^(k|thousand|tnok)", unit):
        value *= 1e3
    return value


def _money(amount: float | None) -> str:
    if amount is None:
        return "not reported"
    a = abs(amount)
    sign = "-" if amount < 0 else ""
    if a >= 1e9:
        return f"NOK {sign}{a / 1e9:.2f} bn"
    if a >= 1e6:
        return f"NOK {sign}{a / 1e6:.1f} m"
    if a >= 1e4:
        return f"NOK {sign}{a / 1e3:.0f} k"
    return f"NOK {sign}{a:,.0f}"


def _title(text: str) -> str:
    return re.sub(r"(^|[\s-])([a-zæøåéü])", lambda m: m.group(1) + m.group(2).upper(), str(text or "").lower())


class _LazyEnvelopes(dict):
    """Envelopes read one at a time from a loader (``kildespor.serve`` reads them by byte offset)."""

    def __init__(self, loader) -> None:
        super().__init__()
        self.loader = loader

    def __missing__(self, org: str) -> dict[str, Any]:
        env = self.loader(org)
        if env is None:
            raise KeyError(org)
        if len(self) > 64:
            self.clear()
        self[org] = env
        return env


class Run:
    def __init__(self, envelopes: list[dict[str, Any]]) -> None:
        self.envelopes = {e["organisation_number"]: e for e in envelopes}
        self.rows = [index_row(e) for e in envelopes]
        self.by_org = {r["o"]: r for r in self.rows}
        self.baseline = not any((e.get("refresh") or {}).get("previous_run_id") for e in envelopes)

    @classmethod
    def from_index(cls, rows: list[dict[str, Any]], loader, baseline: bool = True) -> "Run":
        """A run built from the viewer's index rows, loading an envelope only when a question is about it."""
        run = cls.__new__(cls)
        run.envelopes = _LazyEnvelopes(loader)
        run.rows = list(rows)
        run.by_org = {r["o"]: r for r in run.rows}
        run.baseline = baseline
        return run

    # -- company lookup --------------------------------------------------------------------
    def find_company(self, q: str) -> dict[str, Any] | None:
        m = re.search(r"\b(\d{3}\s?\d{3}\s?\d{3})\b", q)
        if m and m.group(1).replace(" ", "") in self.by_org:
            return self.by_org[m.group(1).replace(" ", "")]
        ql = " " + re.sub(r"[^a-z0-9æøåéü&]+", " ", q.lower()) + " "
        best, score = None, 0
        for r in self.rows:
            for name in (r.get("n"), r.get("b")):
                if not name:
                    continue
                core = re.sub(r"\b(as|asa|ans|da|sa|enk|nuf|iks|ks|ba|sti|fli|brl|esek|bbl|kf|sf)\b", "", name.lower())
                core = re.sub(r"[^a-z0-9æøåéü&]+", " ", core).strip()
                if len(core) >= 3 and f" {core} " in ql and len(core) > score:
                    best, score = r, len(core)
        return best

    # -- screening ---------------------------------------------------------------------------
    def parse_screen(self, q: str) -> dict[str, Any]:
        l = q.lower()
        filters: list[dict[str, Any]] = []
        unsupported = sorted({msg for key, msg in UNSUPPORTED.items() if key in l})
        munis = {r["m"].lower(): r["m"] for r in self.rows if r.get("m")}
        m = re.search(r"\b(?:in|from|based in|located in)\s+([a-zæøåéü][a-zæøåéü .'-]*?)(?=\s+(?:with|and|that|having|which|who|where|over|under|above|below|more|less|by)\b|[,?.!]|$)", l)
        if m and m.group(1).strip() in munis:
            filters.append({"k": "m", "v": munis[m.group(1).strip()], "t": "municipality is " + _title(m.group(1))})
        else:
            for name, original in munis.items():
                if len(name) > 3 and re.search(r"\b" + re.escape(name) + r"\b", l):
                    filters.append({"k": "m", "v": original, "t": "municipality is " + _title(name)})
                    break
        if re.search(r"\b(hiring|recruit|job ads?|vacanc|open roles?|jobs)\b", l):
            filters.append({"k": "hire", "t": "hiring signal (open job ad)"})
        if re.search(r"\b(without|no|lacking|missing)\s+(an?\s+)?(official\s+)?(web\s?site|homepage)", l):
            filters.append({"k": "nw", "t": "no verified website found (absence is not proof)"})
        elif re.search(r"\b(with|has|have|having)\s+(an?\s+)?(official\s+|verified\s+)?(web\s?site|homepage)", l) or (re.search(r"\bwebsites?\b", l) and not re.search(r"\bno\b", l)):
            filters.append({"k": "w", "t": "verified website"})
        if re.search(r"\b(without|no|lacking|missing|but no)\s+(an?\s+)?social", l):
            filters.append({"k": "nso", "t": "no social profile found"})
        elif re.search(r"\bsocial\b|facebook|instagram|linkedin|youtube|tiktok", l):
            filters.append({"k": "so", "t": "social profile"})
        if re.search(r"\b(news|press|announce|articles?)\b", l):
            filters.append({"k": "news", "t": "dated news on its own channels"})
        rv = re.search(r"(?:revenue|turnover|omsetning|sales)[^0-9<>]*?(over|above|more than|greater than|at least|>=|>|under|below|less than|at most|<=|<)\s*(?:nok\s*)?([\d.,\s]+?)\s*(bn|billion|mrd|m|mn|mill(?:ion)?s?|mnok|k|thousand|tnok|nok)?\b", l) or re.search(r"(over|above|more than|at least|under|below|less than|at most)\s*(?:nok\s*)?([\d.,]+)\s*(bn|billion|m|mn|million|mnok|k|thousand)?\s*(?:nok\s*)?(?:in\s+)?(?:revenue|turnover|sales)", l)
        if rv:
            op = ">" if re.search(r"over|above|more|greater|least|>", rv.group(1)) else "<"
            val = _num(rv.group(2), rv.group(3))
            filters.append({"k": "r", "op": op, "v": val, "t": f"revenue {'over' if op == '>' else 'under'} {_money(val)}"})
        em = re.search(r"(over|above|more than|at least|under|below|fewer than|less than|at most)\s*(\d+)\s*(?:registered\s+)?(?:employees|staff|people|workers)", l) or re.search(r"employees?\s*(>=|<=|>|<)\s*(\d+)", l)
        if em:
            op = ">" if re.search(r"over|above|more|least|>", em.group(1)) else "<"
            filters.append({"k": "e", "op": op, "v": int(em.group(2)), "t": f"employees {'over' if op == '>' else 'under'} {em.group(2)}"})
        if re.search(r"\b(loss[- ]making|unprofitable|losses|negative (net )?result)\b", l):
            filters.append({"k": "loss", "t": "net loss in the latest filed year"})
        elif re.search(r"\bprofitable\b|\bprofit\b", l):
            filters.append({"k": "profit", "t": "net profit in the latest filed year"})
        if "negative equity" in l:
            filters.append({"k": "negeq", "t": "negative equity"})
        if re.search(r"\b(changed|changes|what changed|new since|updated)\b", l):
            filters.append({"k": "ch", "t": "changed since the previous run"})
        if re.search(r"\b(stale|carried forward|not re-?checked)\b", l):
            filters.append({"k": "st", "t": "facts carried forward"})
        if re.search(r"all five|5/5|complete profiles?|full coverage", l):
            filters.append({"k": "cv5", "t": "data in all five areas"})
        fm = re.search(r"\b(asa|enk|nuf|ans|sole proprietorships?|public limited|cooperatives?|foundations?)\b", l)
        if fm:
            code = FORMS.get(fm.group(1), fm.group(1).upper())
            filters.append({"k": "f", "v": code, "t": f"legal form {code}"})
        for sector, divisions in SECTORS.items():
            if re.search(r"\b" + re.escape(sector), l) and not any(f["k"] == "dv" for f in filters):
                filters.append({"k": "dv", "v": divisions, "t": f"industry: {sector}"})
        ind = re.search(r"industry\s+(?:code\s+)?(\d{2}(?:\.\d{1,3})?)", l)
        if ind:
            filters.append({"k": "ic", "v": ind.group(1), "t": f"industry code starts {ind.group(1)}"})
        sort = None
        top = re.search(r"\btop\s+(\d+)(?:\s+\w+)?\s+by\s+(revenue|employees|growth)", l) or re.search(r"\b(largest|biggest)\b", l)
        if top:
            key = top.group(2) if top.lastindex and top.lastindex >= 2 else "revenue"
            n = int(top.group(1)) if top.group(1) and top.group(1).isdigit() else 10
            sort = {"k": {"employees": "e", "growth": "rg"}.get(key, "r"), "n": n}
        return {"filters": filters, "unsupported": unsupported, "sort": sort}

    @staticmethod
    def matches(r: dict[str, Any], f: dict[str, Any]) -> bool:
        k = f["k"]
        if k == "m":
            return r.get("m") == f["v"]
        if k == "hire":
            return bool(r.get("j"))
        if k == "w":
            return bool(r.get("w"))
        if k == "nw":
            return not r.get("w")
        if k == "so":
            return bool(r.get("so"))
        if k == "nso":
            return not r.get("so")
        if k == "news":
            return bool(r.get("nw"))
        if k in ("r", "e"):
            v = r.get(k)
            return v is not None and (v > f["v"] if f["op"] == ">" else v < f["v"])
        if k == "loss":
            return r.get("nr") is not None and r["nr"] < 0
        if k == "profit":
            return r.get("nr") is not None and r["nr"] > 0
        if k == "negeq":
            return r.get("eq") is not None and r["eq"] < 0
        if k == "ch":
            return bool(r.get("ch"))
        if k == "st":
            return bool(r.get("st"))
        if k == "cv5":
            return r.get("cv") == 5
        if k == "f":
            return r.get("f") == f["v"]
        if k == "dv":
            return r.get("dv") in f["v"]
        if k == "ic":
            return str(r.get("i") or "").startswith(f["v"])
        return False

    def screen(self, q: str, plan: dict[str, Any]) -> dict[str, Any]:
        rows = [r for r in self.rows if all(self.matches(r, f) for f in plan["filters"])]
        if plan["sort"]:
            key = plan["sort"]["k"]
            rows = sorted((r for r in rows if r.get(key) is not None), key=lambda r: -r[key])[: plan["sort"]["n"]]
        keys = list(dict.fromkeys(CITE_FOR[f["k"]] for f in plan["filters"] if f["k"] in CITE_FOR))
        if plan["sort"]:
            keys.append("employees" if plan["sort"]["k"] == "e" else "revenue")
        results = []
        for r in rows:
            citations = []
            for key in keys:
                c = (r.get("src") or {}).get(key)
                if c:
                    citations.append({"criterion": key, "claim_id": c[0], "evidence_id": c[1], "source_url": c[2], "retrieved_at": c[3]})
            results.append({
                "organisation_number": r["o"], "name": r["n"], "municipality": r.get("m"), "revenue": r.get("r"), "fiscal_year": r.get("y"),
                "employees": r.get("e"), "website": r.get("w"), "social": r.get("so"), "job_ads": r.get("j"),
                "news_items": r.get("nw"), "changes": r.get("ch"), "citations": citations,
            })
        notes = []
        if any(f["k"] in ("nw", "nso") for f in plan["filters"]):
            notes.append('"No website" or "no social profile" means none was verified in this run; it is not proof that none exists.')
        if any(f["k"] == "ch" for f in plan["filters"]) and self.baseline:
            notes.append("This run has no previous snapshot, so no company can have changed yet; run again with the same --state-dir.")
        if plan["unsupported"]:
            notes.append("Ignored (not supported by the evidence): " + "; ".join(plan["unsupported"]))
        return {
            "kind": "screen", "question": q, "supported": True,
            "criteria": [f["t"] for f in plan["filters"]] + ([f"top {plan['sort']['n']} by " + {"r": "revenue", "e": "employees", "rg": "revenue growth"}[plan["sort"]["k"]]] if plan["sort"] else []),
            "count": len(results), "of": len(self.rows), "results": results, "notes": notes,
        }

    # -- one company --------------------------------------------------------------------------
    def company(self, row: dict[str, Any], q: str) -> dict[str, Any]:
        env = self.envelopes[row["o"]]
        l = q.lower()
        claims = [c for c in claims_of(env) if c.get("availability") == "available"]
        evidence = {e["id"]: e for e in env.get("evidence", [])}
        families = {fam: info for sec in (env.get("sections") or {}).values() for fam, info in (sec.get("families") or {}).items()}
        syn = synthesis_of(env)

        def cite(claim_ids: list[str], evidence_ids: list[str]) -> list[dict[str, Any]]:
            out = []
            for ev_id in evidence_ids:
                e = evidence.get(ev_id)
                if e:
                    out.append({"evidence_id": ev_id, "source_url": e.get("final_url") or e.get("source_url"), "retrieved_at": e.get("retrieved_at"), "quoted_span": e.get("claim_span")})
            return [{"claim_ids": claim_ids, **o} for o in out]

        def from_section(key: str) -> dict[str, Any] | None:
            sec = next((s for s in syn.get("sections", []) if s.get("key") == key), None)
            if not sec:
                return None
            return {"answer": sec["text"], "citations": [c for s in sec["sentences"] for c in cite(s["claim_ids"], s["evidence_ids"])]}

        base = {"kind": "company", "question": q, "organisation_number": row["o"], "name": row["n"]}
        for term, message in UNSUPPORTED.items():
            if term in l:
                return {**base, "supported": False, "answer": f"Not supported by the evidence: {message}. The run covers registry identity, accounts, roles, workplaces, the verified website, social profiles, single job ads and dated news.", "citations": []}
        if re.search(r"\b(ceo|chief executive|daglig leder|general manager|managing director)\b", l):
            ceo = next((c for c in claims if c["field"] == "role" and isinstance(c["value"], dict) and c["value"].get("role_code") == "DAGL"), None)
            if ceo:
                return {**base, "supported": True, "answer": f"The general manager (daglig leder) is {ceo['value'].get('name')}.", "citations": cite([ceo["id"]], ceo["evidence_ids"])}
            fam = families.get("leadership") or {}
            return {**base, "supported": False, "answer": f"Not supported by the evidence: the register lists no general manager for this entity ({fam.get('state', 'not checked')}: {fam.get('reason', '')}).", "citations": []}
        topic = topic_of(l)
        if topic == "hiring":
            # Only a single job ad answers "is it hiring"; a careers page is context in the reason.
            jobs = [c for c in claims if c["field"] == "job_posting"]
            if not jobs:
                fam = families.get("hiring") or {}
                return {**base, "supported": False, "answer": f"Not supported by the evidence: no single open job ad was found ({fam.get('state', 'not checked')}: {fam.get('reason', '')}). This is not proof that it is not hiring.", "citations": []}
            answer = f"Yes: {len(jobs)} open job ad(s), each a single ad read at its source: " + "; ".join(f"{(c['value'] or {}).get('title')} ({(c['value'] or {}).get('url')})" for c in jobs[:8] if isinstance(c["value"], dict)) + "."
            return {**base, "supported": True, "answer": answer, "citations": [x for c in jobs[:8] for x in cite([c["id"]], c["evidence_ids"])]}
        if topic in (None, "brief"):
            found = {"answer": syn.get("summary", ""), "citations": [c for s in syn.get("summary_sentences", []) for c in cite(s["claim_ids"], s["evidence_ids"])]}
        else:
            found = from_section(topic) or (from_section("signals") if topic == "activity" else None)  # "signals": briefs written before 24 Oct
        if not found or not found["answer"]:
            return {**base, "supported": False, "answer": "Not supported by the evidence: nothing in this company's profile answers that.", "citations": []}
        return {**base, "supported": True, **found}

    # -- entry -------------------------------------------------------------------------------
    def answer(self, q: str) -> dict[str, Any]:
        q = " ".join(str(q or "").split())
        if not q:
            return {"kind": "none", "question": q, "supported": False, "answer": "Ask a question."}
        company = self.find_company(q)
        plan = self.parse_screen(q)
        personal = re.search(r"\b(who|what does|what do|is it|are they|does it|tell me|ceo|daglig leder|chair|hiring|revenue|website|doing|owner|changed|brief|working|published|recent)\b", q, re.I)
        screening = len([f for f in plan["filters"] if f["k"] not in ("m", "dv")]) > 1 or re.search(r"\b(companies|which|list|top \d+|largest|biggest)\b", q, re.I)
        if company and personal and not screening:
            return self.company(company, q)
        if plan["unsupported"] and not plan["filters"]:
            return {"kind": "screen", "question": q, "supported": False, "answer": "Not supported by the evidence: " + "; ".join(plan["unsupported"]) + "."}
        if not plan["filters"] and not plan["sort"]:
            if company:
                return self.company(company, q)
            return {"kind": "none", "question": q, "supported": False, "answer": "Not supported by the evidence. I can screen by municipality, industry, legal form, revenue, employees, profit or loss, website, social profiles, hiring, news, coverage and changes, or answer questions about one company (name or organisation number): who runs it, what it does, how it is doing, whether it is hiring, what changed and what is unknown."}
        return self.screen(q, plan)


def render(result: dict[str, Any]) -> str:
    lines = []
    if result.get("kind") == "screen" and result.get("supported"):
        lines.append(f"{result['count']} of {result['of']} companies match: " + "; ".join(result["criteria"]) + ".")
        for r in result["results"][:50]:
            facts = [x for x in (
                _title(r.get("municipality") or ""),
                f"revenue {_money(r['revenue'])} ({r['fiscal_year']})" if r.get("revenue") is not None else "",
                f"{r['employees']} employees" if r.get("employees") is not None else "",
                f"{r['job_ads']} job ad(s)" if r.get("job_ads") else "",
                r.get("website") or "",
                "/".join(r.get("social") or []),
                f"{r['news_items']} news" if r.get("news_items") else "",
                f"{r['changes']} change(s)" if r.get("changes") else "",
            ) if x]
            lines.append(f"- {r['name']} ({r['organisation_number']}): " + " · ".join(facts))
            for c in r["citations"]:
                lines.append(f"    [{c['criterion']}] {c['source_url']} (retrieved {c['retrieved_at']}; claim {c['claim_id']})")
        if len(result["results"]) > 50:
            lines.append(f"... and {len(result['results']) - 50} more (use --json for all).")
        lines += result.get("notes", [])
        return "\n".join(lines)
    if result.get("kind") == "company":
        lines.append(f"{result['name']} ({result['organisation_number']}): {result['answer']}")
        seen = set()
        for c in result.get("citations", []):
            if c["evidence_id"] in seen:
                continue
            seen.add(c["evidence_id"])
            span = f' — "{c["quoted_span"][:120]}"' if c.get("quoted_span") else ""
            lines.append(f"  source: {c['source_url']} (retrieved {c['retrieved_at']}){span}")
        return "\n".join(lines)
    return result.get("answer", "")


def load(path: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask a question over a run's envelopes (deterministic, cited, no model)")
    parser.add_argument("--envelopes", default="out/envelopes.jsonl")
    parser.add_argument("--json", action="store_true", help="print the structured answer")
    parser.add_argument("question", nargs="+")
    args = parser.parse_args(argv)
    run = Run(load(args.envelopes))
    result = run.answer(" ".join(args.question))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return 0 if result.get("supported") else 2


if __name__ == "__main__":
    sys.exit(main())
