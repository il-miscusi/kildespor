"""Audit helpers for the news gate's labels.

    python3 tools/news_audit.py join  --items I.jsonl.gz --labels L.jsonl.gz --old C_news_labels.tsv --out joined.tsv
    python3 tools/news_audit.py sample --items I.jsonl.gz --labels L.jsonl.gz --n 200 --seed 20261023 --out sample.tsv
    python3 tools/news_audit.py agree --labels L.jsonl.gz --audit audit_labels.tsv

The old 23 October labels (campaign/audit/oct23/C_news_labels.tsv) map to the teacher's classes as
A -> A, A2 -> D, B -> G, C -> B, D -> C, E -> E, G -> F, H -> H. Items are joined on the company and
the article URL (scheme, www, query and trailing slash ignored).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
from collections import Counter
from urllib.parse import urlsplit

OLD_TO_NEW = {"A": "A", "A2": "D", "B": "G", "C": "B", "D": "C", "E": "E", "G": "F", "H": "H"}
NAMES = {"A": "company_event", "B": "product_or_service", "C": "evergreen_guide_or_marketing", "D": "commentary_or_case",
         "E": "recruitment", "F": "other_entity", "G": "listing_or_navigation", "H": "uncertain"}


def read_jsonl(path: str) -> list[dict]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def url_key(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{(parts.hostname or '').lower().removeprefix('www.')}{parts.path.rstrip('/')}".lower()


def row_out(r: dict, lab: dict | None, extra: dict | None = None) -> dict:
    p = (lab or {}).get("p") or []
    out = {
        "id": r["id"], "org": r.get("org"), "legal_name": r.get("legal_name"), "host": r.get("host"), "method": r.get("method"),
        "teacher": (lab or {}).get("label", ""), "p_teacher": max(p) if p else "", "pA": p[0] if p else "",
        "rule": r.get("rule") or "", "published": r.get("published"), "categories": ",".join(r.get("categories") or []),
        "title": r.get("title"), "url": r.get("url"), "lead": (r.get("lead") or "")[:300].replace("\t", " ").replace("\n", " "),
    }
    out.update(extra or {})
    return out


def write_tsv(path: str, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter="\t")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["join", "sample", "agree"])
    ap.add_argument("--items")
    ap.add_argument("--labels")
    ap.add_argument("--old")
    ap.add_argument("--audit")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20261023)
    ap.add_argument("--out")
    args = ap.parse_args()
    labels = {r["id"]: r for r in read_jsonl(args.labels)} if args.labels else {}

    if args.cmd == "join":
        items = read_jsonl(args.items)
        by_key = {(r.get("org"), url_key(r.get("url") or "")): r for r in items if r.get("url")}
        rows, miss = [], 0
        conf = Counter()
        with open(args.old, encoding="utf-8") as fh:
            for old in csv.DictReader(fh, delimiter="\t"):
                r = by_key.get((old["orgnr"], url_key(old["url"])))
                if r is None:
                    miss += 1
                    continue
                lab = labels.get(r["id"])
                mapped = OLD_TO_NEW.get(old["class"], "H")
                conf[(mapped, (lab or {}).get("label", "-"))] += 1
                rows.append(row_out(r, lab, {"old": old["class"], "old_mapped": mapped, "agree": int(mapped == (lab or {}).get("label"))}))
        write_tsv(args.out, rows)
        n = len(rows)
        agree = sum(r["agree"] for r in rows)
        binary = sum((r["old_mapped"] == "A") == (r["teacher"] == "A") for r in rows)
        print(f"joined {n} of {n + miss} old labels; class agreement {agree}/{n}; company_event yes/no agreement {binary}/{n}")
        for (a, b), k in sorted(conf.items(), key=lambda kv: -kv[1]):
            print(f"  old {a} teacher {b}: {k}")
    elif args.cmd == "sample":
        items = [r for r in read_jsonl(args.items) if r["id"] in labels]
        random.seed(args.seed)
        pick = random.sample(items, min(args.n, len(items)))
        write_tsv(args.out, [row_out(r, labels.get(r["id"])) for r in pick])
        print(f"sampled {len(pick)} of {len(items)}")
    else:
        mine = {}
        with open(args.audit, encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row.get("label"):
                    mine[row["id"]] = (row["label"].strip().upper()[:1], row.get("set", ""))
        for subset in sorted({s for _, s in mine.values()}):
            ids = [k for k, (_, s) in mine.items() if s == subset and k in labels]
            agree = sum(labels[k]["label"] == mine[k][0] for k in ids)
            binary = sum((labels[k]["label"] == "A") == (mine[k][0] == "A") for k in ids)
            print(f"{subset or '(none)'}: {len(ids)} items; class agreement {agree}/{len(ids)}; company_event yes/no {binary}/{len(ids)}")
            print("   " + ", ".join(f"me {a}/teacher {b}: {n}" for (a, b), n in Counter((mine[k][0], labels[k]['label']) for k in ids if mine[k][0] != labels[k]['label']).most_common(12)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
