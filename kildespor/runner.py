"""Batch runner: one terminal envelope per input, inside a wall-clock budget.

Phase 1 reads the official registers for every company and immediately
writes a complete output file. Phase 2 (website, profiles, jobs, news)
improves that file in place: a full, valid output is rewritten atomically
every checkpoint, so if the run is cut short the file on disk still holds one
envelope per input. Work still running at the deadline is abandoned and its
unfinished families are reported as ``failed`` with the reason.
"""
from __future__ import annotations

import copy
import csv
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from . import __version__, brreg, jobs, model, website
from .contract import publish
from .envelope import build_envelope, placeholder_envelope
from .http import Fetcher, utc_now
from .model import FAMILIES, STATES, CompanyRecord
from .refresh import diff, load_previous
from .synthesis import build_synthesis


def log(message: str) -> None:
    print(f"[{utc_now()}] {message}", file=sys.stderr, flush=True)


# -- input ------------------------------------------------------------------------------
def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def read_inputs(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8-sig")
    raw: list[Any] = []
    stripped = text.strip()
    if path.endswith(".csv"):
        reader = csv.DictReader(text.splitlines())
        for row in reader:
            raw.append(row.get("organisation_number") or row.get("orgnr") or row.get("organisasjonsnummer") or next(iter(row.values()), ""))
    elif stripped.startswith("["):
        raw = json.loads(stripped)
    elif stripped.startswith("{") and "\n" not in stripped:
        body = json.loads(stripped)
        raw = body.get("organisation_numbers") or body.get("companies") or [body]
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    raw.append(json.loads(line))
                    continue
                except ValueError:
                    pass
            raw.append(line)
    orgs: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get("organisation_number") or item.get("organisasjonsnummer") or item.get("orgnr") or item.get("org") or item.get("id")
        else:
            value = item
        org = _digits(value)
        if not org:
            org = str(value).strip()
        if org and org not in orgs:
            orgs.append(org)
    return orgs


# -- per-company work -----------------------------------------------------------------------
def official_phase(fetcher: Fetcher, rec: CompanyRecord, snapshot_row: dict[str, Any] | None) -> None:
    started = time.monotonic()
    before = fetcher.thread_requests()
    try:
        if len(rec.organisation_number) != 9:
            for family in FAMILIES:
                rec.set_state(family, "not_applicable", "input is not a nine-digit Norwegian organisation number")
            return
        entity = brreg.fetch_entity(fetcher, rec, snapshot_row)
        if entity is None:
            for family in FAMILIES:
                if family != "legal_identity":
                    rec.set_state(family, "failed" if rec.family_state.get("legal_identity", {}).get("state") == "failed" else "not_applicable", "no entity record to anchor identity")
            return
        brreg.fetch_roles(fetcher, rec)
        brreg.fetch_subunits(fetcher, rec)
        brreg.fetch_accounts(fetcher, rec)
        brreg.fetch_group(fetcher, rec)
        brreg.fetch_updates(fetcher, rec)
    except Exception as exc:  # never let one company break the batch
        rec.error("official", f"{type(exc).__name__}: {exc}")
    finally:
        rec.requests += fetcher.thread_requests() - before
        rec.runtime_ms += int((time.monotonic() - started) * 1000)


def website_phase(fetcher: Fetcher, rec: CompanyRecord, deadline: float, per_company_s: float, request_cap: int | None = None) -> None:
    if not rec.context.get("entity"):
        return
    started = time.monotonic()
    before = fetcher.thread_requests()
    company_deadline = min(deadline, started + per_company_s)
    try:
        fetcher.begin_company(request_cap)
        try:
            website.research_website(fetcher, rec, company_deadline - 5)
        except Exception as exc:
            rec.error("website", f"{type(exc).__name__}: {exc}")
            if rec.family_state.get("official_website", {}).get("state") != "available":
                rec.set_state("official_website", "failed", f"website research stopped: {type(exc).__name__}: {str(exc)[:80]}")
    finally:
        fetcher.begin_company(None)
        rec.requests += fetcher.thread_requests() - before
        rec.runtime_ms += int((time.monotonic() - started) * 1000)


def jobs_phase(fetcher: Fetcher, rec: CompanyRecord, deadline: float, request_cap: int | None, feed: "jobs.NavFeed | None") -> None:
    """Runs only once the NAV feed has loaded, so no worker sits waiting for it."""
    if not rec.context.get("entity"):
        return
    started = time.monotonic()
    before = fetcher.thread_requests()
    try:
        fetcher.begin_company(None if request_cap is None else max(6, request_cap // 2))
        try:
            jobs.research_jobs(fetcher, rec, deadline, feed)
        except Exception as exc:
            rec.error("hiring", f"{type(exc).__name__}: {exc}")
            if not rec.claims_for("hiring"):
                rec.set_state("hiring", "failed", f"job research stopped: {type(exc).__name__}: {str(exc)[:80]}")
        # A verified ad can name the employer's homepage: check it if no website was verified yet.
        if (rec.context.get("nav_employer") or {}).get("homepage") and rec.family_state.get("official_website", {}).get("state") != "available":
            fetcher.begin_company(8)
            try:
                website.research_website(fetcher, rec, min(deadline, time.monotonic() + 30), only_nav=True)
            except Exception as exc:
                rec.error("website", f"NAV homepage check stopped: {type(exc).__name__}: {exc}")
    finally:
        fetcher.begin_company(None)
        rec.requests += fetcher.thread_requests() - before
        rec.runtime_ms += int((time.monotonic() - started) * 1000)


def frozen_copy(rec: CompanyRecord) -> CompanyRecord:
    with rec._lock:
        clone = CompanyRecord(
            organisation_number=rec.organisation_number,
            claims=copy.deepcopy(rec.claims),
            evidence=copy.deepcopy(rec.evidence),
            family_state=copy.deepcopy(rec.family_state),
            errors=list(rec.errors),
            context={},
            checked_fields=set(rec.checked_fields),
            pages_checked=list(rec.pages_checked),
            requests=rec.requests,
            runtime_ms=rec.runtime_ms,
        )
    return clone


# -- output ---------------------------------------------------------------------------------
def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    os.replace(tmp, path)


def assemble(orgs: list[str], recs: dict[str, CompanyRecord], previous: dict[str, dict[str, Any]], run: dict[str, Any], final: bool) -> list[dict[str, Any]]:
    out = []
    for org in orgs:
        try:
            snap = frozen_copy(recs[org])
            if final:
                snap.finalize_states()
            else:
                for family in FAMILIES:
                    if family not in snap.family_state and not snap.claims_for(family):
                        snap.set_state(family, "failed", "research still in progress at this checkpoint")
            changes, meta = diff(snap, previous.get(org), run["run_id"])
            synthesis = build_synthesis(snap, changes if meta.get("previous_run_id") else None, baseline=bool(meta.get("baseline")))
            out.append(build_envelope(snap, run=run, synthesis=synthesis, changes=changes, refresh_meta=meta))
        except Exception as exc:
            log(f"envelope build failed for {org}: {type(exc).__name__}: {exc}")
            out.append(placeholder_envelope(org, run, f"envelope assembly failed: {type(exc).__name__}"))
    return out


def validate(envelopes: list[dict[str, Any]], orgs: list[str]) -> dict[str, Any]:
    got = [e["organisation_number"] for e in envelopes]
    bad_states = [
        (e["organisation_number"], c.get("field"), c.get("availability"))
        for e in envelopes
        for c in e["claims"]
        if c.get("availability") not in STATES
    ]
    dangling = [
        (e["organisation_number"], c.get("field"), ev)
        for e in envelopes
        for ids in [{x["id"] for x in e.get("evidence", [])}]
        for c in e["claims"]
        if c.get("availability") == "available"
        for ev in c.get("evidence_ids", [])
        if ev not in ids
    ]
    checks = {
        "exact_expected_count": len(envelopes) == len(orgs),
        "same_order_and_membership": got == orgs,
        "unique_organisation_numbers": len(set(got)) == len(got),
        "all_states_terminal": not bad_states and all(e["state"] in STATES for e in envelopes),
        "every_available_claim_has_evidence": all(c.get("evidence_ids") for e in envelopes for c in e["claims"] if c.get("availability") == "available"),
        "every_evidence_id_resolves": not dangling,
    }
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": bad_states[:20], "dangling_evidence": dangling[:20]}


def run_report(envelopes: list[dict[str, Any]], orgs: list[str], run: dict[str, Any], fetcher: Fetcher, started: float, recs: dict[str, CompanyRecord]) -> dict[str, Any]:
    family_counts: dict[str, dict[str, int]] = {family: {} for family in FAMILIES}
    claims_by_field: dict[str, int] = {}
    for env in envelopes:
        for section in env["sections"].values():
            for family, info in section["families"].items():
                family_counts[family][info["state"]] = family_counts[family].get(info["state"], 0) + 1
        for claim in env["claims"]:
            if claim.get("availability") == "available":
                claims_by_field[claim["field"]] = claims_by_field.get(claim["field"], 0) + 1
    runtimes = sorted(r.runtime_ms for r in recs.values())
    def pct(p: float) -> int | None:
        return runtimes[min(len(runtimes) - 1, int(len(runtimes) * p))] if runtimes else None
    n = max(1, len(envelopes))
    return {
        "agent": "kildespor",
        "agent_version": __version__,
        "run": run,
        "expected_count": len(orgs),
        "emitted_envelopes": len(envelopes),
        "validation": validate(envelopes, orgs),
        "coverage_by_family": {
            family: {"available": counts.get("available", 0), "share_available": round(counts.get("available", 0) / n, 4), "states": counts}
            for family, counts in family_counts.items()
        },
        "available_claims": sum(claims_by_field.values()),
        "claims_by_field": dict(sorted(claims_by_field.items())),
        "evidence_records": sum(len(e["evidence"]) for e in envelopes),
        "changes_detected": sum(len(e["changes"]) for e in envelopes),
        "operations": {
            **fetcher.stats(),
            "wall_clock_s": round(time.monotonic() - started, 1),
            "per_company_runtime_ms": {"p50": pct(0.5), "p95": pct(0.95)},
            "third_party_cost_usd": 0.0,
            "language_model_calls": 0,
        },
    }


# -- main -----------------------------------------------------------------------------------
def run_batch(args: Any) -> int:
    started = time.monotonic()
    budget = float(args.time_budget)
    deadline = started + budget
    orgs = read_inputs(args.input)
    if not orgs:
        log("no organisation numbers in the input")
    run_id = args.run_id or f"kildespor-{utc_now().replace(':', '').replace('-', '')}"
    run = {
        "run_id": run_id,
        "agent": "kildespor",
        "agent_version": __version__,
        "started_at": utc_now(),
        "completed_at": None,
        "terminal_status": "running",
        "time_budget_s": budget,
        "input_count": len(orgs),
    }
    output = Path(args.output)
    state_dir = Path(args.state_dir) if args.state_dir else None
    previous_path = args.previous
    if not previous_path and state_dir and (state_dir / "latest.jsonl").exists():
        previous_path = str(state_dir / "latest.jsonl")
    previous = load_previous(previous_path)
    log(f"run {run_id}: {len(orgs)} companies, budget {budget:.0f}s, workers {args.workers}, previous={previous_path or 'none'} ({len(previous)} envelopes)")

    snapshot_rows: dict[str, dict[str, Any]] = {}
    if args.registry_snapshot:
        try:
            snapshot_rows = brreg.load_registry_snapshot(args.registry_snapshot, orgs)
            log(f"registry snapshot: {len(snapshot_rows)} of {len(orgs)} rows found")
        except Exception as exc:
            log(f"registry snapshot unreadable, live register only: {exc}")

    # arbeidsplassen.nav.no rate-limits bursts (HTTP 429 seen at 4 concurrent), so it gets one connection.
    if state_dir and not args.no_snapshots:
        model.SNAPSHOT_DIR = str(state_dir / "snapshots")
        model.SNAPSHOT_LABEL = f"{state_dir.as_posix().rstrip('/')}/snapshots"
    fetcher = Fetcher(per_host_limit=2, host_limits={"data.brreg.no": 10, "arbeidsplassen.nav.no": 1, "pam-stilling-feed.nav.no": 3})
    max_requests = int(args.max_requests) if args.max_requests else int(args.requests_per_company * max(1, len(orgs)))
    fetcher.max_requests = max_requests
    run["max_requests"] = max_requests
    recs = {org: CompanyRecord(org) for org in orgs}
    write_lock = threading.Lock()

    def checkpoint(final: bool) -> list[dict[str, Any]]:
        run_now = dict(run)
        run_now["completed_at"] = utc_now()
        run_now["terminal_status"] = "completed" if final else "checkpoint"
        envelopes = assemble(orgs, recs, previous, run_now, final)
        with write_lock:
            write_jsonl_atomic(output, [publish(e) for e in envelopes])
        return envelopes

    feed = None
    if not args.official_only:
        feed = jobs.NavFeed(fetcher, max_seconds=max(60.0, min(300.0, budget * 0.35))).start()
    # A complete, valid output exists before any network work starts.
    checkpoint(final=False)
    workers = max(1, int(args.workers))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kildespor")
    # Phase 1: official records (must finish well inside the budget).
    official_deadline = started + budget * 0.40
    futures: dict[Future[None], str] = {pool.submit(official_phase, fetcher, recs[org], snapshot_rows.get(org)): org for org in orgs}
    pending = set(futures)
    while pending and time.monotonic() < official_deadline:
        done, pending = wait(pending, timeout=min(5.0, max(0.1, official_deadline - time.monotonic())), return_when=FIRST_COMPLETED)
    if pending:
        log(f"official phase: {len(pending)} companies unfinished at the phase deadline")
    checkpoint(final=False)
    log(f"official phase done in {time.monotonic() - started:.0f}s; checkpoint written ({len(orgs)} envelopes)")

    # Phase 2: website, profiles and news for every company; job ads are queued only once NAV's
    # vacancy feed has loaded, so website workers never sit waiting for it.
    if not args.official_only:
        enrich_deadline = deadline - max(30.0, budget * 0.12)
        per_company = float(args.per_company_seconds)
        queue = [org for org in orgs if recs[org].context.get("entity")]
        site_futures = {pool.submit(website_phase, fetcher, recs[org], enrich_deadline, per_company, args.company_request_cap): org for org in queue}
        pending2 = set(site_futures)
        jobs_queued = feed is None
        jobs_total = 0
        last_checkpoint = time.monotonic()
        while (pending2 or not jobs_queued) and time.monotonic() < enrich_deadline:
            if not jobs_queued and feed is not None and feed.done.is_set():
                job_futures = {pool.submit(jobs_phase, fetcher, recs[org], enrich_deadline, args.company_request_cap, feed): org for org in queue}
                pending2 |= set(job_futures)
                jobs_total = len(job_futures)
                jobs_queued = True
                log(f"NAV feed loaded ({feed.pages} pages, {len(feed.active)} active ads{', error: ' + feed.error if feed.error else ''}); job research queued")
            if pending2:
                done, pending2 = wait(pending2, timeout=min(5.0, max(0.1, enrich_deadline - time.monotonic())), return_when=FIRST_COMPLETED)
            else:
                time.sleep(min(2.0, max(0.1, enrich_deadline - time.monotonic())))
            if time.monotonic() - last_checkpoint > args.checkpoint_seconds:
                checkpoint(final=False)
                last_checkpoint = time.monotonic()
                log(f"enrichment: {len(site_futures) + jobs_total - len(pending2)}/{len(site_futures) + jobs_total} tasks done")
        if not jobs_queued:
            for org in queue:
                if not recs[org].claims_for("hiring"):
                    recs[org].set_state("hiring", "failed", "NAV's vacancy feed had not loaded before the time budget ran out")
        if pending2:
            log(f"enrichment: {len(pending2)} tasks unfinished at the deadline; their open families are reported as failed")
        for org in queue:
            if not recs[org].claims_for("public_activity"):
                recs[org].set_state("public_activity", "not_available", "no dated activity found in the register feeds or on the company's own channels")
    pool.shutdown(wait=False, cancel_futures=True)

    envelopes = checkpoint(final=True)
    report = run_report(envelopes, orgs, {**run, "completed_at": utc_now(), "terminal_status": "completed"}, fetcher, started, recs)
    if feed is not None:
        report["nav_feed"] = {"days": feed.days, "pages": feed.pages, "active_ads": len(feed.active), "error": feed.error, "loaded": feed.done.is_set()}
    report_path = Path(args.report) if args.report else output.with_name("run-report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.no_site:
        try:
            from .site import build_site

            site_dir = Path(args.site) if args.site else output.parent / "site"
            build_site(envelopes, site_dir, report)
            log(f"site written to {site_dir}/index.html")
        except Exception as exc:
            log(f"site build failed (envelopes unaffected): {type(exc).__name__}: {exc}")
    try:
        from .export_profiles import write as write_profiles

        app_data = Path(args.app_data) if args.app_data else output.parent / "profiles.json"
        write_profiles(envelopes, app_data)
        log(f"web-app dataset written to {app_data}")
    except Exception as exc:
        log(f"web-app dataset not written (envelopes unaffected): {type(exc).__name__}: {exc}")
    if state_dir:
        try:
            (state_dir / "runs").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output, state_dir / "runs" / f"{run_id}.jsonl")
            shutil.copyfile(output, state_dir / "latest.jsonl")
        except OSError as exc:
            log(f"state snapshot not saved: {exc}")
    log(
        f"done: {len(envelopes)} envelopes, validation {'passed' if report['validation']['passed'] else 'FAILED'}, "
        f"{report['available_claims']} claims, {report['operations']['requests']} requests, {report['operations']['wall_clock_s']}s"
    )
    sys.stdout.flush()
    sys.stderr.flush()
    # Abandoned worker threads may still be blocked on sockets; do not wait for them.
    os._exit(0 if report["validation"]["passed"] else 1)
