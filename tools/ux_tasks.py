"""Task checks for the viewer a run writes: what a user (or Builderr's fixed checks) must be able to do.

    python3 tools/ux_tasks.py --envelopes out/envelopes.jsonl [--previous earlier/envelopes.jsonl]

Builds the site from the envelopes (with ``--previous``, the envelopes are first refreshed against
the earlier run, so the refresh views have real changes to show), then drives it in Chrome with
Playwright at 375x812 (phone) and 1280x800 (desktop), once with the pointer and once with the
keyboard only:

    find-name      find a company by name in the labelled search box, open its profile
    find-org       find a company by organisation number
    profile        the profile has Builderr's sections, a brief and availability states
    compare        compare two companies from the result rows; the view has a stable URL
    evidence       open a fact's evidence (source URL, retrieval time, quoted span) and a sentence's
    vacancy        find an active vacancy: a hiring company, its job ad, date and source
    refresh        see the refresh status and what changed, both sides of a change
    ask            ask a supported question; the answer cites its source

and, over ``python -m kildespor.serve``, the preferred routes /companies/:organisation_number,
/research and /refresh with ``?format=json``. Every page visited is checked for horizontal scroll
and script errors. Screenshots (JPEG) go to ``smoke-test/ux/``; results to ``smoke-test/ux/results.json``.
Exits 1 on any miss. A local smoke check: it reads files and runs no research.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kildespor.site import build_site, data_index, write_root_page  # noqa: E402

WIDTHS = {"375": {"width": 375, "height": 812}, "1280": {"width": 1280, "height": 800}}
SECTIONS = ["Company brief", "Company overview", "Latest financials", "Who runs it?", "Locations", "Working here", "Recent activity", "What changed", "What we could not establish", "Check the evidence", "Sources found"]


class Miss(Exception):
    pass


def load(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def refreshed(current: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The current envelopes as a refresh of the previous run would publish them (changes, carried facts)."""
    from kildespor.contract import internal
    from kildespor.envelope import build_envelope
    from kildespor.refresh import diff
    from kildespor.site import _record
    from kildespor.synthesis import build_synthesis

    prev = {e["organisation_number"]: internal(e) for e in previous}
    out = []
    for env in current:
        rec = _record(env)
        run = dict(env.get("run") or {}, run_id=(env.get("run") or {}).get("run_id", "current"))
        changes, meta = diff(rec, prev.get(env["organisation_number"]), run["run_id"])
        syn = build_synthesis(rec, changes if meta.get("previous_run_id") else None, baseline=bool(meta.get("baseline")))
        out.append(build_envelope(rec, run=run, synthesis=syn, changes=changes, refresh_meta=meta))
    return out


def build_fixture(envelopes: Path, previous: Path | None, out: Path, rebuild: bool) -> dict[str, Any]:
    envs = load(envelopes)
    if previous:
        envs = refreshed(envs, load(previous))
        rebuild = False  # refreshed envelopes carry the current brief already
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "envelopes.jsonl", "w", encoding="utf-8") as fh:
        for env in envs:
            fh.write(json.dumps(env, ensure_ascii=False) + "\n")
    report_path = envelopes.with_name("run-report.json")
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
    build_site(envs, out / "site", report, rebuild_synthesis=rebuild)
    write_root_page(out, out / "site")
    return data_index(out / "site")


def pick_targets(index: dict[str, Any]) -> dict[str, Any]:
    rows = index["rows"]
    unique = lambda r: sum(1 for x in rows if x["n"].split()[0].lower() == r["n"].split()[0].lower()) == 1  # noqa: E731
    hiring = [r for r in rows if r.get("j") and r.get("r") is not None]
    pool = hiring or [r for r in rows if r.get("r") is not None] or rows
    a = next((r for r in pool if unique(r)), pool[0])
    b = next((r for r in rows if r["o"] != a["o"] and r.get("r") is not None and r.get("cv") == 5 and unique(r)), next(r for r in rows if r["o"] != a["o"]))
    changed = [r for r in rows if r.get("ch")]
    return {"a": a, "b": b, "changed": changed[0] if changed else None, "baseline": index["meta"].get("baseline", True), "hiring": len([r for r in rows if r.get("j")])}


# -- browser helpers ------------------------------------------------------------------------------

class Session:
    def __init__(self, browser, out: Path, shots: Path, width: str, mode: str, errors: list[str]) -> None:
        self.out, self.shots, self.width, self.mode = out, shots, width, mode
        vp = WIDTHS[width]
        mobile = width == "375"
        self.ctx = browser.new_context(viewport=vp, device_scale_factor=1, is_mobile=mobile, has_touch=mobile)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(6000)
        self.errors = errors
        self.page.on("pageerror", lambda e: errors.append(f"{width}/{mode}: {e}"))
        self.page.on("console", lambda m: errors.append(f"{width}/{mode} console: {m.text}") if m.type == "error" else None)

    def url(self, rel: str) -> str:
        return (self.out / rel).as_uri() if "?" not in rel else (self.out / rel.split("?")[0]).as_uri() + "?" + rel.split("?", 1)[1]

    def goto(self, rel: str) -> None:
        self.page.goto(self.url(rel) if not rel.startswith("http") else rel)
        self.page.wait_for_selector("#company-list li", state="attached")
        self.layout()

    def layout(self) -> None:
        """No horizontal page scroll, and nothing poking out of the viewport outside a scrolling box."""
        r = self.page.evaluate("""() => {
          const W = window.innerWidth, bad = [];
          for (const el of document.querySelectorAll('body *')) {
            const b = el.getBoundingClientRect();
            if (!b.width || !b.height || b.right <= W + 1) continue;
            let a = el.parentElement, inside = false;
            while (a && a !== document.body) { const cs = getComputedStyle(a); if (/(auto|scroll|hidden)/.test(cs.overflowX) && a.clientWidth < W + 1) { inside = true; break; } a = a.parentElement; }
            if (!inside) bad.push(el.tagName + '.' + el.className + ' ' + Math.round(b.right));
          }
          return {doc: document.documentElement.scrollWidth, body: document.body.scrollWidth, W, bad: bad.slice(0, 4)};
        }""")
        if r["doc"] > r["W"] + 1 or r["body"] > r["W"] + 1 or r["bad"]:
            raise Miss(f"horizontal overflow at {r['W']} px on {self.page.url.split('/')[-2:]}: page {r['doc']} px, {r['bad']}")

    def shot(self, name: str, full: bool = False) -> None:
        """Every pointer step; for the keyboard pass, only the steps where keyboard use looks different."""
        if self.mode == "keyboard" and not name.startswith(("evidence", "compare", "vacancy", "ask")):
            return
        suffix = "" if self.mode == "pointer" else "-keyboard"
        self.page.screenshot(path=str(self.shots / f"{name}-{self.width}{suffix}.jpg"), full_page=full, type="jpeg", quality=60 if full else 75)

    def focused(self, js: str) -> bool:
        return bool(self.page.evaluate(f"() => {{ const el = document.activeElement; return !!el && el !== document.body && ({js}); }}"))

    def tab_to(self, js: str, limit: int = 400, back: bool = False) -> int:
        for i in range(limit):
            self.page.keyboard.press("Shift+Tab" if back else "Tab")
            if self.focused(js):
                return i + 1
        raise Miss(f"keyboard: no focusable element matching {js[:80]} within {limit} tab presses")

    def close(self) -> None:
        self.ctx.close()


def expect_profile(s: Session, row: dict[str, Any]) -> None:
    p = s.page
    p.wait_for_selector("#company-title")
    title = p.inner_text("#company-title").strip()
    if title != row["n"]:
        raise Miss(f"profile heading is {title!r}, expected {row['n']!r}")
    if not re.search(r"companies/" + row["o"] + r"(/|$|\?|#)", p.url):
        raise Miss(f"profile URL {p.url} does not name /companies/{row['o']}")
    if row["o"] not in p.inner_text(".company-subtitle"):
        raise Miss("organisation number not shown under the profile heading")
    s.layout()


# -- tasks ------------------------------------------------------------------------------------------

def find(s: Session, row: dict[str, Any], query: str, task: str) -> None:
    p = s.page
    s.goto("index.html")
    box = p.get_by_role("searchbox", name=re.compile("search companies", re.I))
    if s.mode == "pointer":
        box.click()
        box.fill(query)
        link = p.get_by_role("link", name=re.compile(re.escape(row["n"]) + r", organisation number " + row["o"]))
        link.wait_for()
        s.layout()
        s.shot(f"{task}-results")
        link.click()
    else:
        s.tab_to("el.id === 'search'")
        p.keyboard.type(query)
        s.layout()
        p.keyboard.press("Enter")
        p.wait_for_timeout(300)
        if not p.url.rstrip("/").split("?")[0].endswith((f"companies/{row['o']}/index.html", f"companies/{row['o']}")):
            if not s.focused(f"el.classList.contains('company-link') && (el.getAttribute('aria-label') || '').indexOf({json.dumps(row['o'])}) >= 0"):
                s.tab_to(f"el.classList.contains('company-link') && (el.getAttribute('aria-label') || '').indexOf({json.dumps(row['o'])}) >= 0")
            p.keyboard.press("Enter")
    expect_profile(s, row)
    s.shot(task)


def task_find_name(s: Session, t: dict[str, Any]) -> str:
    query = " ".join(t["a"]["n"].split()[:2])
    find(s, t["a"], query, "find-name")
    return f"typed {query!r}, opened {t['a']['n']}"


def task_find_org(s: Session, t: dict[str, Any]) -> str:
    org = t["b"]["o"]
    find(s, t["b"], org if s.mode == "pointer" else f"{org[:3]} {org[3:6]} {org[6:]}", "find-org")
    return f"typed {org}, opened {t['b']['n']}"


def task_profile(s: Session, t: dict[str, Any]) -> str:
    a = t["a"]
    s.goto(f"site/companies/{a['o']}/index.html")
    expect_profile(s, a)
    p = s.page
    missing = [h for h in SECTIONS if not p.get_by_role("heading", name=h, exact=True).count()]
    if missing:
        raise Miss(f"profile lacks sections {missing}")
    brief = p.inner_text("#brief")
    if len(brief) < 200:
        raise Miss("the company brief is empty")
    if not p.locator("#unknown .state, #evidence .state").count():
        raise Miss("no availability state is shown")
    if s.mode == "keyboard":
        s.tab_to("el.getAttribute('data-jump') === 'financials'")
        p.keyboard.press("Enter")
        p.wait_for_timeout(400)
        if not s.focused("el.id === 'h-fin'"):
            raise Miss("section link did not move focus to Latest financials")
    s.shot("profile", full=True)
    return f"{len(SECTIONS)} sections, brief {len(brief)} chars"


def task_compare(s: Session, t: dict[str, Any]) -> str:
    a, b = t["a"], t["b"]
    p = s.page
    s.goto("site/research/index.html")
    p.evaluate("() => { try { localStorage.clear(); } catch (e) {} }")
    s.goto("site/research/index.html")
    box = p.get_by_role("searchbox", name=re.compile("search companies", re.I))
    for row in (a, b):
        if s.mode == "pointer":
            box.fill(row["o"])
            p.get_by_role("button", name=f"Compare {row['n']}", exact=True).first.click()
        else:
            if not s.focused("el.id === 'search'"):
                s.tab_to("el.id === 'search'", back=True)
            p.keyboard.press("ControlOrMeta+a")
            p.keyboard.type(row["o"])
            s.tab_to(f"el.getAttribute('data-pick') === {json.dumps(row['o'])}")
            p.keyboard.press("Space")
            if s.focused(f"el.getAttribute('data-pick') === {json.dumps(row['o'])} && el.getAttribute('aria-pressed') === 'true'") is False:
                raise Miss("Compare toggle did not report its pressed state")
            s.tab_to("el.id === 'search'", back=True)
    if s.mode == "pointer":
        s.layout()
        s.shot("compare-selected")
        p.get_by_role("link", name="Compare now").click()
    else:
        s.tab_to("el.getAttribute('data-route') === 'compare'")
        p.keyboard.press("Enter")
    p.wait_for_selector("table.compare")
    head = p.inner_text("table.compare")
    for row in (a, b):
        if row["n"] not in head:
            raise Miss(f"{row['n']} missing from the comparison")
    if "orgs=" not in p.url or a["o"] not in p.url or b["o"] not in p.url:
        raise Miss(f"compare URL {p.url} does not name both companies")
    if not p.locator("table.compare button.cite").count():
        raise Miss("compared values have no evidence buttons")
    s.layout()
    stable = p.url
    p.goto(stable)  # the URL alone reopens the same comparison
    p.wait_for_selector("table.compare")
    if b["n"] not in p.inner_text("table.compare"):
        raise Miss("reopening the compare URL lost a company")
    s.shot("compare", full=False)
    return f"{a['n']} vs {b['n']} at {stable.split('/')[-2]}/{stable.split('/')[-1]}"


def check_evidence_box(text: str, where: str) -> None:
    for words in ("Source", "Retrieved", "Quoted span"):
        if words not in text:
            raise Miss(f"{where} lacks {words!r}")
    if not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text):
        raise Miss(f"{where} shows no retrieval time")


def task_evidence(s: Session, t: dict[str, Any]) -> str:
    a = t["a"]
    p = s.page
    s.goto(f"site/companies/{a['o']}/index.html")
    p.wait_for_selector("#company-title")
    if s.mode == "pointer":
        summary = p.locator("#overview details.evidence > summary").first
        summary.click()
        box = p.locator("#overview details.evidence[open]").first
        if not box.locator("a[href^='http']").count():
            raise Miss("fact evidence has no source link")
        check_evidence_box(box.inner_text(), "fact evidence")
        box.scroll_into_view_if_needed()
        s.shot("evidence-fact")
        p.locator("#brief button.cite").first.click()
    else:
        s.tab_to("el.tagName === 'SUMMARY' && !!el.closest('#overview details.evidence')")
        p.keyboard.press("Enter")
        box = p.locator("#overview details.evidence[open]").first
        check_evidence_box(box.inner_text(), "fact evidence (keyboard)")
        s.tab_to("el.classList.contains('cite') && !!el.closest('#brief')")
        p.keyboard.press("Enter")
    dlg = p.locator("#evidence-dialog")
    dlg.wait_for(state="visible")
    text = dlg.inner_text()
    check_evidence_box(text, "sentence evidence")
    if not dlg.locator("a[href^='http']").count():
        raise Miss("sentence evidence has no source link")
    s.layout()
    s.shot("evidence-sentence")
    p.keyboard.press("Escape")
    dlg.wait_for(state="hidden")
    if s.mode == "keyboard" and not s.focused("el.classList.contains('cite')"):
        raise Miss("closing the evidence dialog did not return focus to its button")
    return "fact and sentence evidence show URL, retrieval time and span"


def task_vacancy(s: Session, t: dict[str, Any]) -> str:
    p = s.page
    if not t["hiring"]:
        raise Miss("no company in this run has an open job ad, so there is no vacancy to find (use a batch with hiring)")
    s.goto("site/research/index.html")
    if s.mode == "pointer":
        p.get_by_text("More filters", exact=True).click()
        p.get_by_role("checkbox", name=re.compile("Hiring")).check()
        rows = p.locator("#company-list a.company-link")
        rows.first.wait_for()
        if rows.count() != t["hiring"]:
            raise Miss(f"hiring filter shows {rows.count()} companies, the run has {t['hiring']}")
        s.shot("vacancy-filter")
        rows.first.click()
    else:
        s.tab_to("el.id === 'question'")
        p.keyboard.type("hiring companies")
        s.tab_to("el.classList.contains('ask-button')")
        p.keyboard.press("Enter")
        p.wait_for_selector("#answer a[href*='companies/']")
        s.tab_to("el.tagName === 'A' && !!el.closest('#answer') && el.getAttribute('href').indexOf('companies/') >= 0")
        p.keyboard.press("Enter")
    p.wait_for_selector("#company-title")
    job = p.locator("#hiring ul.plain > li").first
    job.wait_for()
    if not job.locator("a[href^='http']").count():
        raise Miss("the job ad has no link to the ad")
    text = job.inner_text()
    published = re.search(r"published (\d{4}-\d{2}-\d{2})", text)
    due = re.search(r"apply by (\d{4}-\d{2}-\d{2})", text)
    if not published:
        raise Miss("the job ad shows no publication date")
    run_day = (p.evaluate("() => (window.KD && KD.meta && KD.meta.completed_at) || ''") or "")[:10]
    if due and run_day and due.group(1) < run_day:
        raise Miss(f"the job ad closed {due.group(1)}, before the run ({run_day})")
    if s.mode == "keyboard":
        s.tab_to("el.tagName === 'A' && !!el.closest('#hiring') && /^https?:/.test(el.getAttribute('href'))")
    if not job.locator("details.evidence").count():
        raise Miss("the job ad has no evidence control")
    job.scroll_into_view_if_needed()
    s.layout()
    s.shot("vacancy")
    return f"{p.inner_text('#company-title')}: {text.splitlines()[0][:60]} (published {published.group(1)})"


def task_refresh(s: Session, t: dict[str, Any]) -> str:
    p = s.page
    s.goto("index.html")
    if s.mode == "pointer":
        p.get_by_role("navigation", name="Main").get_by_role("link", name="Refresh").click()
    else:
        s.tab_to("el.getAttribute('data-route') === 'refresh'")
        p.keyboard.press("Enter")
    p.wait_for_selector(".refresh-status")
    status = p.inner_text(".refresh-status")
    if "Refresh status" not in status:
        raise Miss("no refresh status on /refresh")
    detail = "baseline run" if t["baseline"] else "refresh run"
    if not t["baseline"] and not t["changed"]:
        if "No published fact changed" not in p.inner_text("main"):
            raise Miss("a refresh with no change does not say so")
        detail += ", no published fact changed (idempotent refresh)"
    elif not t["baseline"]:
        if s.mode == "pointer":
            p.locator("details.change > summary").first.click()
        else:
            s.tab_to("el.tagName === 'SUMMARY' && !!el.closest('details.change')")
            p.keyboard.press("Enter")
        card = p.locator("details.change[open] .change").first
        card.wait_for()
        text = card.inner_text()
        if "Before" not in text or "Now" not in text:
            raise Miss("a change does not show both sides")
        detail += f", {p.locator('details.change').count()} companies changed"
    s.layout()
    s.shot("refresh")
    a = t["changed"] or t["a"]
    s.goto(f"site/companies/{a['o']}/index.html")
    p.wait_for_selector("#changes .refresh-status")
    if "Refresh status" not in p.inner_text("#changes"):
        raise Miss("profile has no refresh status")
    p.locator("#changes").scroll_into_view_if_needed()
    s.shot("refresh-company")
    return detail


def task_ask(s: Session, t: dict[str, Any]) -> str:
    a = t["a"]
    p = s.page
    s.goto(f"site/companies/{a['o']}/index.html")
    p.wait_for_selector("#answer h3")
    if s.mode == "pointer":
        p.get_by_role("button", name="Who runs it?", exact=True).click()
        p.wait_for_function("() => /Who runs it\\?/.test(document.querySelector('#answer h3').textContent)")
        if not p.get_by_role("link", name=re.compile("Open supporting source")).count():
            raise Miss("answer has no supporting source link")
        p.get_by_role("textbox", name="Ask about the selected company").fill("Is it hiring?")
        p.get_by_role("button", name="Check the evidence").click()
        p.wait_for_function("() => /Working here/.test(document.querySelector('#answer h3').textContent)")
    else:
        s.tab_to("el.id === 'question'")
        p.keyboard.type("How is the company doing?")
        s.tab_to("el.classList.contains('ask-button')")
        p.keyboard.press("Enter")
        p.wait_for_function("() => /Latest financials/.test(document.querySelector('#answer h3').textContent)")
    answer = p.inner_text("#answer")
    if not p.locator("#answer button.cite").count():
        raise Miss("answer cites nothing")
    p.locator("#answer").scroll_into_view_if_needed()
    s.layout()
    s.shot("ask")
    # the run-wide question box answers about a named company too
    s.goto("site/research/index.html")
    p.get_by_role("textbox", name="Ask about the companies in this run").fill(f"who runs {a['n']}")
    p.get_by_role("button", name="Check the evidence").click()
    p.wait_for_function(f"() => document.querySelector('#answer') && document.querySelector('#answer').textContent.indexOf({json.dumps(a['n'])}) >= 0")
    s.shot("ask-research")
    return answer.splitlines()[0][:80]


TASKS: list[tuple[str, Callable[[Session, dict[str, Any]], str]]] = [
    ("find-name", task_find_name), ("find-org", task_find_org), ("profile", task_profile), ("compare", task_compare),
    ("evidence", task_evidence), ("vacancy", task_vacancy), ("refresh", task_refresh), ("ask", task_ask),
]


def server_checks(browser, out: Path, shots: Path, t: dict[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    """The same viewer behind kildespor.serve, at Builderr's preferred routes, with JSON."""
    import urllib.request

    from kildespor.serve import serve

    httpd = serve(str(out), "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    results = []

    def check(name: str, fn: Callable[[], str]) -> None:
        start = time.monotonic()
        try:
            results.append({"task": name, "width": "1280", "mode": "server", "ok": True, "detail": fn(), "ms": round((time.monotonic() - start) * 1000)})
        except Exception as exc:  # noqa: BLE001
            results.append({"task": name, "width": "1280", "mode": "server", "ok": False, "detail": f"{type(exc).__name__}: {exc}", "ms": round((time.monotonic() - start) * 1000)})

    s = Session(browser, out, shots, "1280", "server", errors)

    def routes() -> str:
        a = t["a"]
        s.page.goto(f"{base}/companies/{a['o']}")
        expect_profile(s, a)
        s.page.get_by_role("link", name=re.compile(re.escape(t["b"]["n"]) + ", organisation number")).click()
        s.page.wait_for_url(f"{base}/companies/{t['b']['o']}")
        expect_profile(s, t["b"])
        s.page.goto(f"{base}/research")
        s.page.wait_for_selector("#company-list li")
        s.page.goto(f"{base}/refresh")
        s.page.wait_for_selector(".refresh-status")
        s.page.screenshot(path=str(shots / "server-refresh-1280.png"))
        return f"/companies/{a['o']} -> /companies/{t['b']['o']}, /research, /refresh render"

    def json_routes() -> str:
        a = t["a"]
        get = lambda path: json.loads(urllib.request.urlopen(base + path).read())  # noqa: E731
        env = get(f"/companies/{a['o']}?format=json")
        if env.get("organisation_number") != a["o"] or not env.get("claims"):
            raise Miss("/companies/:org?format=json is not the envelope")
        research = get("/research?format=json")
        if len(research["companies"]) < 2:
            raise Miss("/research?format=json lists no companies")
        q = urllib.request.quote(f"who runs {a['n']}")
        ans = get(f"/research?format=json&q={q}")
        if not ans.get("supported") or not ans.get("citations"):
            raise Miss("/research?q= gave no cited answer")
        refresh = get("/refresh?format=json")
        if "baseline" not in refresh:
            raise Miss("/refresh?format=json has no status")
        return f"envelope with {len(env['claims'])} claims; {len(research['companies'])} companies; cited answer; refresh baseline={refresh['baseline']}"

    check("server-routes", routes)
    check("server-json", json_routes)
    s.close()
    httpd.shutdown()
    httpd.server_close()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--envelopes", default=str(ROOT / "smoke-test" / "envelopes.jsonl"), help="the run's envelopes (.jsonl or .jsonl.gz)")
    parser.add_argument("--previous", default=None, help="an earlier run's envelopes, to show a refresh with changes")
    parser.add_argument("--rebuild-synthesis", action="store_true", help="rebuild the briefs with the current synthesis.py")
    parser.add_argument("--out", default=None, help="where to build the test output (default: a temporary folder)")
    parser.add_argument("--shots", default=str(ROOT / "smoke-test" / "ux"))
    parser.add_argument("--widths", default="375,1280")
    parser.add_argument("--modes", default="pointer,keyboard")
    parser.add_argument("--no-server", action="store_true")
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="kildespor-ux-"))
    shots = Path(args.shots)
    if shots.exists():
        for old in list(shots.glob("*.png")) + list(shots.glob("*.jpg")):
            old.unlink()
    shots.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    index = build_fixture(Path(args.envelopes), Path(args.previous) if args.previous else None, out, args.rebuild_synthesis)
    t = pick_targets(index)
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception:  # Playwright's own Chromium not downloaded: use the installed Chrome
            browser = pw.chromium.launch(channel="chrome")
        for width in args.widths.split(","):
            for mode in args.modes.split(","):
                for name, fn in TASKS:
                    s = Session(browser, out, shots, width, mode, errors)
                    begin = time.monotonic()
                    try:
                        detail = fn(s, t)
                        results.append({"task": name, "width": width, "mode": mode, "ok": True, "detail": detail, "ms": round((time.monotonic() - begin) * 1000)})
                    except Exception as exc:  # noqa: BLE001 - every miss is reported, none stops the run
                        try:
                            s.page.screenshot(path=str(shots / f"FAILED-{name}-{width}-{mode}.png"))
                        except Exception:  # noqa: BLE001
                            pass
                        results.append({"task": name, "width": width, "mode": mode, "ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:300]}", "ms": round((time.monotonic() - begin) * 1000)})
                    finally:
                        s.close()
        if not args.no_server:
            results += server_checks(browser, out, shots, t, errors)
        browser.close()
    if errors:
        results.append({"task": "script-errors", "width": "all", "mode": "all", "ok": False, "detail": "; ".join(errors[:5])})
    summary = {
        "envelopes": Path(args.envelopes).name, "previous": Path(args.previous).name if args.previous else None, "companies": len(index["rows"]), "run_id": index["meta"].get("run_id"),
        "previous_runs": index["meta"].get("previous_runs"),
        "targets": {"a": [t["a"]["o"], t["a"]["n"]], "b": [t["b"]["o"], t["b"]["n"]]}, "seconds": round(time.monotonic() - started, 1),
        "passed": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results), "results": results,
    }
    (shots / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for r in results:
        print(f"{'PASS' if r['ok'] else 'MISS'}  {r['task']:<13} {r['width']:>5} {r['mode']:<9} {r['detail']}")
    print(f"{summary['passed']} passed, {summary['failed']} missed in {summary['seconds']} s; screenshots in {shots}")
    if not args.out:
        shutil.rmtree(out, ignore_errors=True)
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
