"""Build kildespor/data/candidates-noindex.tsv.gz from the `.no` reverse organisation-number index.

    python3 tools/build_noindex.py SRC.tsv.gz [--universe PATH] [--out PATH]

SRC is the crawl's merged ``noindex.tsv.gz`` (one row per organisation number and
domain whose public page printed that number next to an ownership label; built on
Kaggle by the campaign's ``noindex_*`` kernels, see CACHE.md). This step keeps only
the columns the agent reads and drops the page snippets, so no page text is shipped:

    orgnr rank domain page_url rule domain_orgnrs retrieved_at

Rows are kept only for universe organisation numbers (``--universe`` reads a local
copy of the universe file; without it the file is downloaded).
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cachelib import DATA, universe_orgnrs, write_tsv_gz  # noqa: E402

COLUMNS = ("orgnr", "rank", "domain", "page_url", "rule", "domain_orgnrs", "retrieved_at")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--universe")
    ap.add_argument("--out", default=str(DATA / "candidates-noindex.tsv.gz"))
    args = ap.parse_args()
    universe = universe_orgnrs(args.universe)
    rows = []
    with gzip.open(args.src, "rt", encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        col = {name: header.index(name) for name in COLUMNS}
        for line in fh:
            f = line.rstrip("\n").split("\t")
            row = tuple(f[col[name]] for name in COLUMNS)
            if row[0] in universe and re.fullmatch(r"\d{9}", row[0]) and re.match(r"^https?://", row[3], re.I):
                rows.append(row)
    n = write_tsv_gz(Path(args.out), COLUMNS, rows)
    print(json.dumps({"rows": n, "orgnrs": len({r[0] for r in rows}), "rules": dict(Counter(r[4] for r in rows)),
                      "size": Path(args.out).stat().st_size}))


if __name__ == "__main__":
    main()
