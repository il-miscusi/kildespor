"""Shared helpers for the cache build scripts in tools/ (standard library only)."""
from __future__ import annotations

import gzip
import io
import json
import re
import urllib.request
from pathlib import Path
from typing import Iterable

UNIVERSE_URL = "https://builderr.ai/signalpost-company-universe-2025.jsonl.gz"
DATA = Path(__file__).resolve().parents[1] / "kildespor" / "data"
CANDIDATE_HEADER = ("orgnr", "kind", "platform", "url", "match_rule", "source_record", "release_date", "licence")
UA = "kildespor-cache-builder/1.0 (+https://github.com/il-miscusi/kildespor)"


def universe_orgnrs(path: str | None = None) -> set[str]:
    """Organisation numbers of the Signalpost universe (local file, or downloaded)."""
    if path:
        fh = gzip.open(path, "rt", encoding="utf-8")
    else:
        req = urllib.request.Request(UNIVERSE_URL, headers={"User-Agent": UA})
        fh = io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(urllib.request.urlopen(req, timeout=300).read())), encoding="utf-8")
    with fh:
        return {json.loads(line)["organisation_number"] for line in fh if line.strip()}


def write_tsv_gz(path: Path, header: Iterable[str], rows: Iterable[Iterable[str]]) -> int:
    """Write a sorted, de-duplicated, byte-reproducible gzipped TSV. Returns the row count."""
    clean = sorted({tuple(re.sub(r"[\t\r\n]+", " ", str(v or "")).strip() for v in r) for r in rows})
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    buf.write("\t".join(header) + "\n")
    for r in clean:
        buf.write("\t".join(r) + "\n")
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
        gz.write(buf.getvalue().encode("utf-8"))
    return len(clean)


def registered_domain(host: str) -> str:
    host = host.lower().strip(".").removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
