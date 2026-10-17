"""Command line: ``python -m kildespor --input batch.jsonl --output out/envelopes.jsonl``."""
from __future__ import annotations

import argparse
import os

from .runner import run_batch


def main() -> int:
    parser = argparse.ArgumentParser(prog="kildespor", description="Research a batch of Norwegian companies by organisation number.")
    parser.add_argument("--input", "-i", required=True, help="JSONL (one {\"organisation_number\": ...} per line), JSON list, CSV or plain text of organisation numbers")
    parser.add_argument("--output", "-o", default="out/envelopes.jsonl", help="terminal envelope JSONL, one line per input (default: out/envelopes.jsonl)")
    parser.add_argument("--report", default=None, help="machine-readable run report (default: run-report.json next to --output)")
    parser.add_argument("--site", default=None, help="directory for the static HTML viewer (default: site/ next to --output)")
    parser.add_argument("--no-site", action="store_true", help="skip the HTML viewer")
    parser.add_argument("--app-data", default=None, help="where to write the web app's dataset (default: profiles.json next to --output; the app reads public/data/profiles.json)")
    parser.add_argument("--previous", default=None, help="previous envelopes JSONL to refresh against (default: <state-dir>/latest.jsonl when present)")
    parser.add_argument("--state-dir", default=os.environ.get("KILDESPOR_STATE_DIR", "state"), help="where run snapshots are kept for refresh (default: state/)")
    parser.add_argument("--registry-snapshot", default=os.environ.get("KILDESPOR_REGISTRY_SNAPSHOT"), help="optional Brreg bulk CSV(.gz) used if the live register is unreachable")
    parser.add_argument("--time-budget", type=float, default=float(os.environ.get("KILDESPOR_TIME_BUDGET_S", "900")), help="wall-clock budget in seconds; research stops at 88%% of it and the complete output is always on disk (default 900)")
    parser.add_argument("--workers", type=int, default=int(os.environ.get("KILDESPOR_WORKERS", "32")), help="concurrent companies (default 32)")
    parser.add_argument("--per-company-seconds", type=float, default=90.0, help="cap on website+jobs research per company (default 90)")
    parser.add_argument("--checkpoint-seconds", type=float, default=90.0, help="how often the complete output is rewritten during enrichment")
    parser.add_argument("--max-requests", type=int, default=int(os.environ.get("KILDESPOR_MAX_REQUESTS", "0")), help="hard cap on outbound requests for the run, redirects and retries included (default: --requests-per-company x inputs)")
    parser.add_argument("--requests-per-company", type=float, default=float(os.environ.get("KILDESPOR_REQUESTS_PER_COMPANY", "19")), help="request budget per input used when --max-requests is not set (default 19)")
    parser.add_argument("--company-request-cap", type=int, default=16, help="cap on website+jobs requests for one company (default 16)")
    parser.add_argument("--official-only", action="store_true", help="official registers only (fast mode)")
    parser.add_argument("--no-snapshots", action="store_true", help="do not store raw source snapshots under <state-dir>/snapshots")
    parser.add_argument("--run-id", default=os.environ.get("KILDESPOR_RUN_ID"))
    args = parser.parse_args()
    return run_batch(args)


if __name__ == "__main__":
    raise SystemExit(main())
