"""The accuracy check: draw a sample of published facts, then report how often each source type is right.

    python3 tools/accuracy.py sample --envelopes out/envelopes.jsonl --companies 40 --out docs/accuracy/sheet.csv
    (a reviewer opens every row's live page and fills ``verdict`` and ``note``)
    python3 tools/accuracy.py report --sheet docs/accuracy/sheet.csv

``verdict`` is ``right``, ``wrong`` or ``unopened`` (the page could not be read, so nothing is said about
the fact). The share right is right / (right + wrong); an unopened row is reported, never counted as right.
``report`` writes ``docs/ACCURACY.md`` and ``kildespor/data/accuracy.json``, which the viewer shows.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import random
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_TYPES = {"official_website": "site", "social_profile": "social", "dated_news": "news", "hiring_signal": "jobs"}
LABELS = {"site": "Company website", "social": "Social profile", "news": "Dated news", "jobs": "Job ad"}
QUESTIONS = {
    "site": "Is this the company's own site, not a parent group's, a chain's or a namesake's?",
    "social": "Is this page the company itself, not a parent, a group, an agency or a namesake?",
    "news": "Is this a real post about the company, with this publication date?",
    "jobs": "Is this one open ad posted by this company, naming the company?",
}
FIELDS = ["organisation_number", "company", "source_type", "value", "route", "evidence_url", "quoted_span", "question", "verdict", "note"]


def read_envelopes(path: str) -> list[dict]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def rows_of(env: dict) -> list[dict]:
    evidence = {e["id"]: e for e in env.get("evidence", []) if e.get("id")}
    name = (env.get("legal_identity") or {}).get("legal_name") or ""
    rows = []
    for claim in env.get("claims", []):
        kind = SOURCE_TYPES.get(claim.get("field"))
        if kind is None or claim.get("availability") != "available":
            continue
        first = next((evidence[e] for e in claim.get("evidence_ids", []) if e in evidence), {})
        rows.append({
            "organisation_number": env["organisation_number"], "company": name, "source_type": kind, "value": claim.get("value"),
            "route": claim.get("extraction_method") or "", "evidence_url": first.get("final_url") or first.get("source_url") or "",
            "quoted_span": str(first.get("claim_span") or "")[:240], "question": QUESTIONS[kind], "verdict": "", "note": "",
        })
    return rows


def sample(args: argparse.Namespace) -> None:
    """Whole companies, drawn at random, with those holding a job ad or a social page drawn first: both are
    rare, and a sheet with three of either says nothing about them. At most ``--per-type`` rows of one
    source type per company, so one company's twenty news posts do not fill the sheet."""
    rng = random.Random(args.seed)
    companies: dict[str, list[dict]] = {}
    for path in args.envelopes:
        for env in read_envelopes(path):
            rows = rows_of(env)
            if rows:
                companies.setdefault(env["organisation_number"], rows)
    orgs = sorted(companies)
    rng.shuffle(orgs)
    picked: list[str] = []
    for kind, share in (("jobs", 0.35), ("social", 0.3), ("news", 0.2)):
        have = [o for o in orgs if o not in picked and any(r["source_type"] == kind for r in companies[o])]
        picked += have[: round(args.companies * share)]
    picked += [o for o in orgs if o not in picked][: max(0, args.companies - len(picked))]
    sheet: list[dict] = []
    for org in sorted(picked):
        seen: Counter = Counter()
        for row in companies[org]:
            seen[row["source_type"]] += 1
            if seen[row["source_type"]] <= args.per_type:
                sheet.append(row)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        writer.writerows(sheet)
    print(f"{len(picked)} companies, {len(sheet)} rows: {dict(Counter(r['source_type'] for r in sheet))} -> {out}")


def wilson_low(right: int, total: int, z: float = 1.96) -> float:
    """The lower end of a 95% interval: 20 of 20 is not 100% in any sense a reader should rely on."""
    if not total:
        return 0.0
    share = right / total
    return (share + z * z / (2 * total) - z * math.sqrt(share * (1 - share) / total + z * z / (4 * total * total))) / (1 + z * z / total)


def report(args: argparse.Namespace) -> None:
    with open(args.sheet, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    blank = [r for r in rows if r["verdict"] not in ("right", "wrong", "unopened")]
    if blank:
        raise SystemExit(f"{len(blank)} rows have no verdict")
    out_rows = []
    table = ["| source type | checked | right | wrong | share right | 95% lower bound | could not open |", "|---|---:|---:|---:|---:|---:|---:|"]
    for kind in ("site", "social", "news", "jobs"):
        mine = [r for r in rows if r["source_type"] == kind]
        right = sum(r["verdict"] == "right" for r in mine)
        wrong = sum(r["verdict"] == "wrong" for r in mine)
        unopened = sum(r["verdict"] == "unopened" for r in mine)
        checked = right + wrong
        share = f"{100 * right / checked:.0f}%" if checked else "n/a"
        out_rows.append({"key": kind, "label": LABELS[kind], "checked": checked, "right": right, "wrong": wrong, "unopened": unopened, "share": share})
        table.append(f"| {LABELS[kind]} | {checked} | {right} | {wrong} | {share} | {100 * wilson_low(right, checked):.0f}% | {unopened} |")
    wrong_rows = [r for r in rows if r["verdict"] == "wrong"]
    misses = [f"- **{r['company']}** ({r['organisation_number']}), {r['source_type']}: {r['value']}. {r['note']}" for r in wrong_rows] or ["None."]
    companies = len({r["organisation_number"] for r in rows})
    payload = {"as_of": args.as_of, "run": args.run, "companies": companies, "rows": out_rows, "checked_by": args.checked_by,
               "note": f"{companies} companies checked page by page on {args.as_of} against the live pages. {args.checked_by} Job ads and social pages were sampled more heavily because they are rare."}
    (ROOT / "kildespor" / "data" / "accuracy.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    text = [
        "# How often each source type is right", "",
        f"Page-by-page check of run `{args.run}` on {args.as_of}: {companies} companies, {len(rows)} published facts. Sheet: "
        f"[`accuracy/sheet.csv`](accuracy/sheet.csv). {args.checked_by}", "",
        "Companies were drawn at random, those with a job ad or a social page first, because both are rare. At most "
        "three facts of one type per company. So the shares describe published facts, not coverage: they say how often "
        "a fact the agent printed holds up, and nothing about what it missed.", "",
        *table, "",
        "The share right is right / (right + wrong). A page that could not be opened counts as neither. "
        "The lower bound is a Wilson 95% interval: on a sample this small it is the number to rely on.", "",
        "## What each check asked", "",
        *[f"- **{LABELS[k]}:** {QUESTIONS[k]}" for k in ("site", "social", "news", "jobs")], "",
        "## What was wrong", "", *misses, "",
        *(["## Fixed after this check", "", args.fixed, "", "These fixes are not in the table: it reports the run as it was checked.", ""] if args.fixed else []),
    ]
    (ROOT / "docs" / "ACCURACY.md").write_text("\n".join(text), encoding="utf-8")
    print("\n".join(table))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    draw = sub.add_parser("sample")
    draw.add_argument("--envelopes", nargs="+", required=True)
    draw.add_argument("--companies", type=int, default=40)
    draw.add_argument("--per-type", type=int, default=3)
    draw.add_argument("--seed", type=int, default=20261102)
    draw.add_argument("--out", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("--sheet", required=True)
    rep.add_argument("--run", required=True)
    rep.add_argument("--as-of", required=True)
    rep.add_argument("--checked-by", required=True, help="who opened the pages, stated plainly")
    rep.add_argument("--fixed", default="", help="what changed in the agent after the check")
    args = parser.parse_args()
    (sample if args.command == "sample" else report)(args)


if __name__ == "__main__":
    main()
