"""Build kildespor/data/candidates-wikidata.tsv.gz from Wikidata (CC0).

One SPARQL query to the Wikidata Query Service: every item with a Norwegian
organisation number (P2333) and its official website (P856), Facebook (P2013),
LinkedIn company id (P4264), Instagram (P2003), X (P2002) and YouTube channel
(P2397). Only organisation numbers in the Signalpost universe are kept.

    python3 tools/build_wikidata.py [--universe PATH] [--raw-out PATH]

Every row cites its statement (``https://www.wikidata.org/wiki/<QID>#<property>``)
and carries the query date. ``--universe`` reads a local copy of signalpost-company-universe-2025.jsonl.gz
instead of downloading it; ``--raw-out`` keeps the query response.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cachelib import CANDIDATE_HEADER, DATA, UA, universe_orgnrs, write_tsv_gz  # noqa: E402

ENDPOINT = "https://query.wikidata.org/sparql"
QUERY = """
SELECT ?item ?o ?w ?fb ?li ?ig ?tw ?yt WHERE {
  ?item wdt:P2333 ?o .
  OPTIONAL { ?item wdt:P856 ?w }
  OPTIONAL { ?item wdt:P2013 ?fb }
  OPTIONAL { ?item wdt:P4264 ?li }
  OPTIONAL { ?item wdt:P2003 ?ig }
  OPTIONAL { ?item wdt:P2002 ?tw }
  OPTIONAL { ?item wdt:P2397 ?yt }
}
"""
RULE = "wikidata_p2333_exact_orgnr"
SOCIAL = (
    ("fb", "facebook", "https://facebook.com/{}", "P2013"),
    ("li", "linkedin", "https://www.linkedin.com/company/{}", "P4264"),
    ("ig", "instagram", "https://instagram.com/{}", "P2003"),
    ("tw", "x", "https://x.com/{}", "P2002"),
    ("yt", "youtube", "https://www.youtube.com/channel/{}", "P2397"),
)


def fetch() -> dict:
    body = urllib.parse.urlencode({"query": QUERY, "format": "json"}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "User-Agent": UA, "Accept": "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(req, timeout=120))


def rows_from(result: dict, universe: set[str], date: str) -> list[tuple[str, ...]]:
    out = []
    seen_hosts: dict[tuple[str, str], tuple[str, ...]] = {}
    for b in result["results"]["bindings"]:
        org = re.sub(r"\D", "", b["o"]["value"])
        if org not in universe:
            continue
        item = "https://www.wikidata.org/wiki/" + b["item"]["value"].rsplit("/", 1)[-1]
        vals = lambda k: [b[k]["value"].strip()] if b.get(k, {}).get("value", "").strip() else []  # noqa: E731
        for w in vals("w"):
            m = re.match(r"^https?://(?:www\.)?([^/\s:]+\.[^/\s:]+)", w, re.I)
            if m:  # one URL per host: language paths (/en, /de/) are the same site
                row = (org, "website", "", w, RULE, f"{item}#P856", date, "CC0-1.0")
                key = (org, m.group(1).lower())
                if key not in seen_hosts or len(w) < len(seen_hosts[key][3]):
                    seen_hosts[key] = row
        for key, platform, fmt, prop in SOCIAL:
            for v in vals(key):
                if re.fullmatch(r"[A-Za-z0-9._\-%@]+", v):
                    out.append((org, "social", platform, fmt.format(v), RULE, f"{item}#{prop}", date, "CC0-1.0"))
    return out + list(seen_hosts.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe")
    ap.add_argument("--raw-out")
    ap.add_argument("--raw-in", help="reuse a saved query response instead of querying")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d", time.gmtime()), help="query date recorded on every row")
    args = ap.parse_args()
    result = json.load(open(args.raw_in)) if args.raw_in else fetch()
    if args.raw_out:
        Path(args.raw_out).write_text(json.dumps(result))
    universe = universe_orgnrs(args.universe)
    rows = rows_from(result, universe, args.date)
    n = write_tsv_gz(DATA / "candidates-wikidata.tsv.gz", CANDIDATE_HEADER, rows)
    orgs = {r[0] for r in rows}
    print(json.dumps({"items": len(result["results"]["bindings"]), "universe_orgs": len(orgs), "rows": n,
                      "by_platform": {p or "website": sum(1 for r in rows if r[2] == p) for p in ("", "facebook", "linkedin", "instagram", "x", "youtube")}}))


if __name__ == "__main__":
    main()
