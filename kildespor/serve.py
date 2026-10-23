"""Serve a run's output over HTTP at Builderr's preferred routes (standard library only).

    python -m kildespor.serve --dir out --port 8000

    /                          the research page: directory, search, questions
    /research                  the same; ?q=<question> answers it, from the run's cited claims only
    /companies/<org>           one company's profile
    /compare?orgs=<a>,<b>      side by side
    /refresh                   refresh status: what changed in this run

Every route also answers JSON with ``?format=json`` (or ``Accept: application/json``):
the company's terminal envelope, the research index or a question's cited answer
(``kildespor.ask``), the compared companies' summary rows, and the refresh report. The
site's assets and data (``/site/...``) and the run's own files (``/envelopes.jsonl``,
``/run-report.json``) are served as files. Read-only: nothing is fetched, nothing is written.
"""
from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .site import ROUTE_TITLES, _esc, company_body, data_index, page_html, research_body

ORG = re.compile(r'"organisation_number"\s*:\s*"([^"]+)"')


class RunFiles:
    """A run's output folder: the site (assets, data) and the envelopes, read lazily."""

    def __init__(self, out_dir: Path) -> None:
        self.dir = Path(out_dir).resolve()
        if (self.dir / "site" / "data" / "index.js").exists():
            self.site = self.dir / "site"
        elif (self.dir / "data" / "index.js").exists():
            self.site = self.dir
        else:
            raise SystemExit(f"no viewer data under {self.dir}: expected site/data/index.js (run the agent, or python -m kildespor.site)")
        self.site_prefix = "/" + (self.site.relative_to(self.dir).as_posix() + "/" if self.site != self.dir else "")
        self.index = data_index(self.site) or {"meta": {}, "rows": []}
        self.rows = {str(r["o"]): r for r in self.index["rows"]}
        self.envelopes_path = next((p for p in (self.dir / "envelopes.jsonl", self.dir / "envelopes.jsonl.gz") if p.exists()), None)
        self._offsets: dict[str, int] | None = None
        self._lock = threading.Lock()
        self._run = None

    # -- envelopes, by byte offset so a 1,100-company run is never parsed whole -----------------
    def _open(self):
        assert self.envelopes_path
        return gzip.open(self.envelopes_path, "rb") if self.envelopes_path.suffix == ".gz" else open(self.envelopes_path, "rb")

    def offsets(self) -> dict[str, int]:
        with self._lock:
            if self._offsets is None:
                self._offsets = {}
                if self.envelopes_path:
                    with self._open() as fh:
                        pos = fh.tell()
                        for line in iter(fh.readline, b""):
                            m = ORG.search(line[:400].decode("utf-8", "replace"))
                            if m:
                                self._offsets.setdefault(m.group(1), pos)
                            pos = fh.tell()
            return self._offsets

    def envelope(self, org: str) -> dict[str, Any] | None:
        pos = self.offsets().get(org)
        if pos is None:
            return None
        with self._open() as fh:
            fh.seek(pos)
            return json.loads(fh.readline())

    def payload(self, org: str) -> dict[str, Any] | None:
        path = self.site / "data" / "companies" / f"{org}.js"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        start = text.index(",", text.index(",") + 1) + 1  # kildesporLoad("company","<org>",{...});
        return json.loads(text[start:].rstrip().rstrip(";").rstrip(")"))

    def ask(self):
        from .ask import Run

        with self._lock:
            if self._run is None:
                self._run = Run.from_index(self.index["rows"], self.envelope, baseline=bool(self.index["meta"].get("baseline", True)))
            return self._run

    def refresh(self) -> dict[str, Any]:
        rows = self.index["rows"]
        report = next((json.loads(p.read_text(encoding="utf-8")) for p in (self.dir / "refresh-report.json", self.dir / "run-report.json") if p.exists()), None)
        return {
            "meta": self.index["meta"],
            "baseline": bool(self.index["meta"].get("baseline", True)),
            "changed": [{"organisation_number": r["o"], "name": r.get("n"), "changes": r.get("ch"), "summaries": r.get("cs") or []} for r in rows if r.get("ch")],
            "carried_forward": [{"organisation_number": r["o"], "name": r.get("n"), "facts": r.get("st")} for r in rows if r.get("st")],
            "not_fully_checked": [{"organisation_number": r["o"], "name": r.get("n"), "families": [{"family": f, "state": s} for f, s in r.get("fl") or []]} for r in rows if r.get("fl")],
            "report": {k: (report or {}).get(k) for k in ("run", "expected_count", "emitted_envelopes", "validation", "coverage_by_family", "operations") if report},
        }


def summary_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "organisation_number": r["o"], "name": r.get("n"), "brand": r.get("b"), "municipality": r.get("m"), "industry_code": r.get("i"), "industry": r.get("ie") or r.get("il"),
        "employees": r.get("e"), "fiscal_year": r.get("y"), "revenue": r.get("r"), "net_result": r.get("nr"), "website": r.get("w"), "social": r.get("so"), "job_ads": r.get("j"),
        "news_items": r.get("nw"), "data_areas_of_5": r.get("cv"), "changes": r.get("ch"), "url": f"/companies/{r['o']}",
    }


def make_handler(run: RunFiles):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"kildespor/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet unless asked
            if getattr(self.server, "verbose", False):
                super().log_message(fmt, *args)

        def send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, data: Any, status: int = 200) -> None:
            self.send(status, json.dumps(data, ensure_ascii=False, indent=1, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def page(self, route: str, body: str, status: int = 200, org: str | None = None, title: str | None = None) -> None:
            text = page_html(route, root="/", site=run.site_prefix, org=org, server=True, body=body, page_title=title)
            self.send(status, text.encode("utf-8"), "text/html; charset=utf-8")

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            path = unquote(parts.path)
            want_json = query.get("format") == "json" or "application/json" in (self.headers.get("Accept") or "") and "text/html" not in (self.headers.get("Accept") or "")
            route = path.strip("/").removesuffix("/index.html").removesuffix("index.html").strip("/")
            segs = [s for s in route.split("/") if s]
            if not segs or segs == ["research"]:
                return self.research(query, want_json)
            if segs[0] == "companies" and len(segs) == 2:
                return self.company(segs[1], want_json)
            if segs == ["compare"]:
                return self.compare(query, want_json)
            if segs == ["refresh"]:
                return self.refresh(want_json)
            return self.file(path)

        def research(self, query: dict[str, str], want_json: bool) -> None:
            q = (query.get("q") or query.get("ask") or "").strip()
            if want_json:
                if q:
                    return self.json(run.ask().answer(q))
                return self.json({"meta": run.index["meta"], "companies": [summary_row(r) for r in run.index["rows"]]})
            self.page("research", research_body(run.index["rows"], "/", run.site_prefix, server=True))

        def company(self, org: str, want_json: bool) -> None:
            org = re.sub(r"\s", "", org)
            row = run.rows.get(org)
            if not row:
                if want_json:
                    return self.json({"error": "not in this run", "organisation_number": org, "state": "not_available"}, 404)
                return self.page("companies", f'<section class="card"><h1>Not in this run</h1><p>No profile for organisation number {_esc(org)}. <span class="state not_available">Not available</span></p></section>', 404, org=org, title="Not in this run")
            if want_json:
                env = run.envelope(org)
                return self.json(env if env is not None else {"organisation_number": org, "profile": run.payload(org)})
            payload = run.payload(org) or {}
            env = {"organisation_number": org}
            self.page("companies", company_body(env, payload, row, "/", run.site_prefix, server=True), org=org, title=f"{row.get('n')} ({org})")

        def compare(self, query: dict[str, str], want_json: bool) -> None:
            orgs = [o for o in re.split(r"[,\s]+", query.get("orgs", "")) if o]
            if want_json:
                return self.json({"organisation_numbers": orgs, "companies": [summary_row(run.rows[o]) if o in run.rows else {"organisation_number": o, "state": "not_available"} for o in orgs]})
            self.page("compare", f'<section class="card"><h1>{_esc(ROUTE_TITLES["compare"])}</h1><p>{_esc(", ".join(run.rows[o].get("n") for o in orgs if o in run.rows) or "Choose two to four companies.")}</p></section>')

        def refresh(self, want_json: bool) -> None:
            data = run.refresh()
            if want_json:
                return self.json(data)
            self.page("refresh", f'<section class="card"><h1>{_esc(ROUTE_TITLES["refresh"])}</h1><p>{len(data["changed"])} companies changed; {len(data["carried_forward"])} with facts carried forward; {len(data["not_fully_checked"])} not fully checked.</p></section>')

        def file(self, path: str) -> None:
            target = (run.dir / path.lstrip("/")).resolve()
            if target.is_dir():
                target = target / "index.html"
            try:
                target.relative_to(run.dir)
            except ValueError:
                return self.send(404, b"not found\n", "text/plain; charset=utf-8")
            if not target.is_file():
                return self.send(404, b"not found\n", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix in (".jsonl",):
                ctype = "application/x-ndjson"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            self.send(200, target.read_bytes(), ctype)

    return Handler


def serve(out_dir: str, host: str = "127.0.0.1", port: int = 8000, verbose: bool = False) -> ThreadingHTTPServer:
    run = RunFiles(Path(out_dir))
    httpd = ThreadingHTTPServer((host, port), make_handler(run))
    httpd.verbose = verbose  # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kildespor.serve", description="Serve a run's viewer at /companies/:organisation_number, /research, /refresh and /compare, with ?format=json")
    parser.add_argument("--dir", default="out", help="the run's output folder (the one holding envelopes.jsonl and site/)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)
    httpd = serve(args.dir, args.host, args.port, args.verbose)
    print(f"serving {Path(args.dir).resolve()} at http://{args.host}:{httpd.server_address[1]}/  (routes /research, /companies/<org>, /compare?orgs=a,b, /refresh; add ?format=json)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
