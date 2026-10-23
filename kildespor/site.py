"""Static viewer for a run: find, compare and verify company information.

Every run writes ``<output dir>/site/`` and an ``index.html`` beside the envelopes:

    index.html                        research page (directory, search, questions); also takes hash routes
    research/index.html               the same page at Builderr's preferred route /research
    companies/<org>/index.html        one company: brief, financials, who runs it, locations, working
                                      here, recent activity, what changed, what is unknown, evidence
    compare/index.html?orgs=a,b,c     side by side, same financial year
    refresh/index.html                refresh status: what changed in this run, both sides with evidence
    assets/viewer.js, viewer.css      the one script and stylesheet every page shares
    data/index.js                     one summary row per company (search, filters, screening)
    data/companies/<org>.js           the full profile of one company, loaded on demand

Each page is a small shell with pre-rendered content (readable without JavaScript) that the
script replaces with the interactive view, so pages work from ``file://``, on any static host
and behind ``python -m kildespor.serve`` (which serves /companies/:organisation_number,
/research, /refresh and /compare with ``?format=json``). Hash routes (``#/companies/<org>``,
``#/compare?orgs=``, ``#/refresh``) keep working in ``index.html``. The whole dataset is never
copied into a page: a company page holds only that company's brief.

Every fact opens its source URL, retrieval time, quoted span, extraction method and content
hash; missing data shows its availability state (not available, ambiguous, failed, blocked).

    python -m kildespor.site --input out/envelopes.jsonl --output out/site --root-page
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import shutil
from pathlib import Path
from typing import Any

from . import __version__
from .model import CompanyRecord
from .synthesis import _clean_brand, build_synthesis, claim_value, internal_field, job_date, money, nace_en, news_date

PUBLISHED_FIELD = {"job_posting": "hiring_signal", "company_news": "dated_news"}
ASSET_DIR = Path(__file__).with_name("viewer")


# -- projection ---------------------------------------------------------------------------------


def claims_of(env: dict[str, Any]) -> list[dict[str, Any]]:
    """Every claim of an envelope in the internal shape (field names and dict values), either input shape.
    A careers page published by an older run is not a hiring fact and is left out."""
    out = []
    for c in env.get("claims", []):
        item = dict(c)
        item["field"] = internal_field(c)
        if item["field"] == "careers_page":
            continue
        item["value"] = claim_value(c)
        out.append(item)
    return out


def _record(env: dict[str, Any]) -> CompanyRecord:
    rec = CompanyRecord(env["organisation_number"])
    rec.claims = [c for c in claims_of(env) if c.get("availability") == "available"]
    rec.evidence = {e["id"]: e for e in env.get("evidence", []) if e.get("id")}
    rec.family_state = {
        fam: {"state": info.get("state"), "reason": info.get("reason"), "checked_sources": info.get("checked_sources", [])}
        for sec in (env.get("sections") or {}).values()
        for fam, info in (sec.get("families") or {}).items()
    }
    return rec


def rebuilt_synthesis(env: dict[str, Any]) -> dict[str, Any]:
    """The brief the current ``synthesis.py`` writes for this envelope's claims."""
    refresh = env.get("refresh") or {}
    return build_synthesis(_record(env), env.get("changes") or None, baseline=bool(refresh.get("baseline", True)))


def synthesis_of(env: dict[str, Any]) -> dict[str, Any]:
    """The envelope's brief; envelopes written before the current brief format get it rebuilt from their claims."""
    syn = env.get("synthesis") or {}
    if "summary_sentences" in syn and "coverage" in syn:
        return syn
    return rebuilt_synthesis(env)


def _compact_sentence(s: dict[str, Any]) -> dict[str, Any]:
    out = {"t": s.get("text"), "c": s.get("claim_ids", []), "e": s.get("evidence_ids", []), "b": s.get("basis", "fact")}
    if s.get("checked_sources"):
        out["k"] = s["checked_sources"]
    return out


def company_payload(env: dict[str, Any]) -> dict[str, Any]:
    """The full profile the viewer shows for one company (compact keys, every fact keeps its evidence ids)."""
    syn = synthesis_of(env)
    # Evidence records share their source snapshot (URL, retrieval time, hash): store each snapshot once.
    sources: list[dict[str, Any]] = []
    source_index: dict[tuple[Any, ...], int] = {}

    def source_ref(e: dict[str, Any], previous: bool = False) -> int:
        key = (e.get("final_url") or e.get("source_url"), e.get("retrieved_at"), e.get("content_sha256"), previous)
        if key not in source_index:
            item = {"u": key[0], "c": e.get("source_class"), "t": e.get("retrieved_at"), "h": e.get("content_sha256")}
            for src, dst in (("http_status", "hs"), ("access_policy", "a")):
                if e.get(src) is not None:
                    item[dst] = e[src]
            if e.get("final_url") and e.get("source_url") != e.get("final_url"):
                item["ru"] = e.get("source_url")
            if previous:
                item["prev"] = True
            source_index[key] = len(sources)
            sources.append(item)
        return source_index[key]

    evidence: dict[str, dict[str, Any]] = {}
    for e in env.get("evidence", []):
        item = {"r": source_ref(e), "s": e.get("claim_span"), "m": e.get("extraction_method")}
        if e.get("reporting_period"):
            item["p"] = e["reporting_period"]
        evidence[e["id"]] = item
    claims = []
    for c in claims_of(env):
        item: dict[str, Any] = {"id": c.get("id"), "f": c.get("field"), "fa": c.get("family"), "a": c.get("availability"), "v": c.get("value"), "e": c.get("evidence_ids", [])}
        if c["field"] in PUBLISHED_FIELD:
            item["pf"] = PUBLISHED_FIELD[c["field"]]
        for src, dst in (("label", "l"), ("effective_date", "d"), ("reporting_period", "rp"), ("relationship", "rel"), ("reason", "r"), ("stale", "st"), ("historical", "hi"), ("first_observed_at", "fo"), ("carried_forward_reason", "cr"), ("checked_sources", "k")):
            if c.get(src) not in (None, [], ""):
                item[dst] = c[src]
        claims.append(item)
    for change in env.get("changes", []):
        for e in change.get("previous_evidence") or []:
            if e.get("id") and e["id"] not in evidence:
                evidence[e["id"]] = {"r": source_ref(e, previous=True), "s": e.get("claim_span"), "m": e.get("extraction_method")}
    families = {
        fam: {"s": info.get("state"), "r": info.get("reason"), "n": info.get("claim_count", 0), "k": (info.get("checked_sources") or [])[:4]}
        for sec in (env.get("sections") or {}).values()
        for fam, info in (sec.get("families") or {}).items()
    }
    return {
        "org": env["organisation_number"],
        "state": env.get("state"),
        "run": {k: (env.get("run") or {}).get(k) for k in ("run_id", "completed_at", "started_at")},
        "id": env.get("legal_identity") or {},
        "fam": families,
        "cl": claims,
        "ev": evidence,
        "src": sources,
        "syn": {
            "headline": syn.get("headline"),
            "summary": [_compact_sentence(s) for s in syn.get("summary_sentences", [])],
            "sections": [{"key": sec.get("key"), "h": sec.get("heading"), "q": sec.get("question"), "s": [_compact_sentence(s) for s in sec.get("sentences", [])]} for sec in syn.get("sections", [])],
            "timeline": syn.get("timeline", []),
            "signals": syn.get("signals", []),
            "unknowns": syn.get("unknowns", []),
            "coverage": syn.get("coverage") or {},
            "method": syn.get("method"),
        },
        "ch": [
            {
                "type": ch.get("change_type"), "field": ch.get("field"), "summary": ch.get("summary"), "mat": ch.get("materiality"),
                "prev": ch.get("previous"), "cur": ch.get("current"), "ce": ch.get("current_evidence_ids", []), "pe": ch.get("previous_evidence_ids", []),
                "cc": ch.get("current_claim_id"), "pc": ch.get("previous_claim_id"), "at": ch.get("detected_at"),
            }
            for ch in env.get("changes", [])
        ],
        "rf": env.get("refresh") or {},
    }


def _first(claims: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    items = [c for c in claims if c["field"] == field and c.get("availability") == "available" and not c.get("historical")]
    return items[0] if items else None


def index_row(env: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """One summary row per company: what search, filters, screening and the compare picker need."""
    claims = [c for c in claims_of(env) if c.get("availability") == "available"]
    evidence = {e["id"]: e for e in env.get("evidence", []) if e.get("id")}
    syn = (payload or {}).get("syn") or {}
    if not syn:
        full = synthesis_of(env)
        syn = {"headline": full.get("headline"), "summary": [_compact_sentence(s) for s in full.get("summary_sentences", [])], "coverage": full.get("coverage") or {}, "timeline": full.get("timeline", [])}
    cite: dict[str, list[Any]] = {}

    def remember(key: str, claim: dict[str, Any] | None) -> None:
        if not claim:
            return
        ev = next((evidence[e] for e in claim.get("evidence_ids", []) if e in evidence), {})
        cite[key] = [claim.get("id"), (claim.get("evidence_ids") or [None])[0], ev.get("final_url") or ev.get("source_url"), ev.get("retrieved_at")]

    name = _first(claims, "legal_name")
    brand = _first(claims, "public_brand_name")
    brand_text = _clean_brand(brand["value"] if brand else None, str(name["value"]) if name else "")
    muni = _first(claims, "municipality")
    form = _first(claims, "legal_form")
    industries = sorted((c for c in claims if c["field"] == "industry_code" and isinstance(c["value"], dict)), key=lambda c: c["value"].get("rank", 9))
    ind = industries[0] if industries else None
    employees = _first(claims, "registered_employees")
    status = _first(claims, "registry_status")
    founded = _first(claims, "founded_date")
    site = _first(claims, "official_website")
    socials = [c for c in claims if c["field"] == "social_profile"]
    jobs = [c for c in claims if c["field"] == "job_posting"]
    news = sorted((c for c in claims if c["field"] == "company_news"), key=lambda c: news_date(c["value"]), reverse=True)
    roles = [c for c in claims if c["field"] == "role" and isinstance(c["value"], dict)]
    ceo = next((c for c in roles if c["value"].get("role_code") == "DAGL"), None)
    chair = next((c for c in roles if c["value"].get("role_code") == "LEDE"), None)
    parent = next((c for c in claims if c["field"] == "group_relationship" and isinstance(c["value"], dict) and c["value"].get("relationship") in ("parent", "ultimate_parent")), None)
    fin: dict[str, dict[str, dict[str, Any]]] = {}
    for c in claims:
        if c["field"].startswith("financial.") and isinstance(c["value"], dict) and c["value"].get("account_type", "SELSKAP") == "SELSKAP" and "amount" in c["value"]:
            fin.setdefault(str(c["value"].get("fiscal_year")), {})[c["field"].split(".", 1)[1]] = c
    years = sorted(y for y in fin if y.isdigit())
    latest = fin.get(years[-1], {}) if years else {}
    prev = fin.get(str(int(years[-1]) - 1), {}) if years else {}

    def amount(row: dict[str, dict[str, Any]], metric: str) -> float | None:
        c = row.get(metric)
        return c["value"].get("amount") if c else None

    revenue, prev_revenue = amount(latest, "revenue"), amount(prev, "revenue")
    equity, assets = amount(latest, "equity"), amount(latest, "total_assets")
    for key, claim in (("name", name), ("municipality", muni), ("industry", ind), ("employees", employees), ("website", site), ("revenue", latest.get("revenue")), ("result", latest.get("annual_result")), ("equity", latest.get("equity")), ("ceo", ceo), ("chair", chair), ("parent", parent)):
        remember(key, claim)
    if jobs:
        remember("hiring", jobs[0])
    if socials:
        remember("social", socials[0])
    if news:
        remember("news", news[0])
    ind_code = ind["value"].get("code") if ind else None
    changes = env.get("changes") or []
    families = [(fam, info.get("state")) for sec in (env.get("sections") or {}).values() for fam, info in (sec.get("families") or {}).items()]
    stale = sum(1 for c in claims if c.get("stale"))
    timeline = syn.get("timeline") or []
    coverage = syn.get("coverage") or {}
    return {
        "o": env["organisation_number"],
        "n": name["value"] if name else env["organisation_number"],
        "b": brand_text,
        "m": muni["value"] if muni else None,
        "f": (form["value"] or {}).get("code") if form and isinstance(form["value"], dict) else None,
        "i": ind_code,
        "il": ind["value"].get("description") if ind else None,
        "ie": nace_en(ind_code) if ind_code and not str(ind_code).startswith("00") else None,
        "dv": str(ind_code)[:2] if ind_code and not str(ind_code).startswith("00") else None,
        "e": employees["value"] if employees else None,
        "fd": str(founded["value"])[:4] if founded else None,
        "rs": status["value"] if status else None,
        "y": years[-1] if years else None,
        "r": revenue,
        "rg": round(revenue / prev_revenue - 1, 4) if revenue is not None and prev_revenue else None,
        "nr": amount(latest, "annual_result"),
        "eq": equity,
        "er": round(equity / assets, 4) if equity is not None and assets else None,
        "w": ((site["value"] or {}).get("url") if isinstance(site["value"], dict) else site["value"]) if site else None,
        "so": sorted({(c["value"].get("platform") if isinstance(c["value"], dict) else "profile") for c in socials}),
        "j": len(jobs),
        "nw": len(news),
        "nd": news_date(news[0]["value"]) if news else None,
        "ceo": ceo["value"].get("name") if ceo else None,
        "chair": chair["value"].get("name") if chair else None,
        "pg": parent["value"].get("name") if parent else None,
        "cv": coverage.get("score"),
        "ca": [1 if a.get("available") else 0 for a in coverage.get("areas", [])],
        "ch": len(changes),
        "cs": [ch.get("summary") for ch in changes[:3]],
        "st": stale,
        "ld": timeline[0]["date"] if timeline else None,
        "lt": timeline[0]["text"] if timeline else None,
        "ws": next((state for fam, state in families if fam == "official_website"), None),
        "fl": [[fam, state] for fam, state in families if state in ("failed", "blocked")],
        "src": cite,
    }


def _js(kind: str, key: str | None, data: Any) -> str:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f"kildesporLoad({json.dumps(kind)},{json.dumps(key)},{body});\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# -- pages ----------------------------------------------------------------------------------------

ROUTE_TITLES = {
    "research": "Research Norwegian companies",
    "compare": "Compare companies",
    "refresh": "Refresh: what changed in this run",
}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _place(name: str | None) -> str:
    return " ".join(w.capitalize() for w in str(name or "").split())


def route_href(route: str, root: str, server: bool = False) -> str:
    """A link to a route page, as written into static HTML: explicit ``index.html`` so it opens from disk too."""
    return f"{root}{route}" if server else f"{root}{route}/index.html"


def page_html(route: str, *, root: str, site: str, org: str | None = None, server: bool = False, body: str = "", page_title: str | None = None, description: str | None = None) -> str:
    """One page of the viewer: the shared header, pre-rendered content for readers without JavaScript,
    and the script that renders the interactive view from ``data/``.

    ``root`` prefixes route links (``companies/<org>/``, ``research/``), ``site`` prefixes assets and
    data. Static pages use relative prefixes so the site works from ``file://`` and any static host;
    ``kildespor.serve`` passes absolute ones."""
    link = lambda r: _esc(route_href(r, root, server))  # noqa: E731
    conf = json.dumps({"root": root, "site": site, "route": route, "org": org, "server": server})
    nav = "".join(f'<a href="{link(r)}" data-route="{r}"{" aria-current=\"page\"" if (route == r or (route == "companies" and r == "research")) else ""}>{label}</a>' for r, label in (("research", "Research"), ("compare", "Compare"), ("refresh", "Refresh")))
    title = page_title or ROUTE_TITLES.get(route, "Kildespor company research")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — Kildespor</title>
<meta name="description" content="{_esc(description or 'Find, compare and verify Norwegian company information. Every fact links to its source, retrieval time and quoted span.')}">
<meta name="generator" content="kildespor {_esc(__version__)}">
<link rel="icon" href="data:,">
<link rel="stylesheet" href="{_esc(site)}assets/viewer.css">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="topbar"><div class="bar">
<a class="brand" href="{link('research')}">Kildespor<small>Norwegian company research</small></a>
<form class="global-search" id="search-form" role="search" aria-label="Search companies" action="{link('research')}" method="get">
<label for="search">Search companies by name or organisation number</label>
<div class="search-row"><input id="search" name="q" type="search" placeholder="Company name, organisation number, industry or place" autocomplete="off" spellcheck="false"><button class="btn primary" type="submit">Search</button></div>
</form>
<nav class="main" aria-label="Main">{nav}</nav>
</div><div class="run-proof" id="run-proof" aria-label="About this run"></div></header>
<main id="main" tabindex="-1">
{body}
</main>
<div id="tray" role="region" aria-label="Companies selected for comparison"></div>
<dialog id="evidence-dialog" aria-labelledby="dlg-title"><div class="dialog-inner"><div class="dialog-head"><h2 id="dlg-title">Evidence</h2><button type="button" class="btn" id="dlg-close" data-close="1">Close</button></div><div id="dlg-body"></div></div></dialog>
<script>window.KD_CONF={conf};</script>
<script src="{_esc(site)}assets/viewer.js"></script>
<script src="{_esc(site)}data/index.js" onerror="window.KD&&KD.indexFailed&&KD.indexFailed()"></script>
</body>
</html>
"""


def _noscript(site: str) -> str:
    return (f'<noscript><p class="card">The interactive viewer needs JavaScript. The same data is in '
            f'<span class="mono">{_esc(site)}data/index.js</span> and <span class="mono">{_esc(site)}data/companies/&lt;organisation number&gt;.js</span>.</p></noscript>')


def research_body(rows: list[dict[str, Any]], root: str, site: str, server: bool = False, limit: int = 2000) -> str:
    """The directory as plain links, for readers and checkers that do not run the script."""
    items = "".join(
        f'<li><a href="{_esc(route_href("companies/" + str(r["o"]), root, server))}">{_esc(r.get("n"))}</a> '
        f'<span class="mono">{_esc(r["o"])}</span> · {_esc(_place(r.get("m")) or "location not reported")} · {_esc(r.get("ie") or r.get("il") or "industry not reported")}</li>'
        for r in rows[:limit]
    )
    return (f'<section class="card"><h1>Research Norwegian companies</h1><p>{len(rows)} companies in this run. '
            f'Open a company for its brief, figures, people, web presence, hiring, changes and what is unknown, each fact with its source.</p>'
            f'<ul class="plain">{items}</ul></section>{_noscript(site)}')


def company_body(env: dict[str, Any], payload: dict[str, Any], row: dict[str, Any], root: str, site: str, server: bool = False) -> str:
    syn = payload.get("syn") or {}
    sources: dict[str, tuple[int, str | None]] = {}
    ev, src = payload.get("ev") or {}, payload.get("src") or []

    def refs(sentence: dict[str, Any]) -> str:
        nums = []
        for ev_id in sentence.get("e", []):
            item = ev.get(ev_id)
            if not item:
                continue
            s = src[item["r"]] if item.get("r") is not None and item["r"] < len(src) else {}
            if s.get("u"):
                sources.setdefault(s["u"], (len(sources) + 1, s.get("t")))
                nums.append(sources[s["u"]][0])
        return "".join(f"<sup>[{n}]</sup>" for n in sorted(set(nums))[:4])

    brief = "".join(f"<p>{_esc(s.get('t'))} {refs(s)}</p>" for s in syn.get("summary") or [])
    unknowns = "".join(f"<li>{_esc(u.get('label'))}: {_esc(str(u.get('state') or '').replace('_', ' '))} ({_esc(u.get('reason'))})</li>" for u in syn.get("unknowns") or [])
    source_list = "".join(f'<li>[{n}] <a href="{_esc(u)}">{_esc(u)}</a> retrieved {_esc(t or "time not recorded")}</li>' for u, (n, t) in sources.items())
    ident = payload.get("id") or {}
    form = (ident.get("legal_form") or {}).get("code") if isinstance(ident.get("legal_form"), dict) else ident.get("legal_form")
    return (f'<article class="card"><p><a href="{_esc(route_href("research", root, server))}">Research</a></p>'
            f'<h1>{_esc(row.get("n"))}</h1><p>Organisation number {_esc(env["organisation_number"])} · {_esc(form or "legal form not reported")} · {_esc(_place(ident.get("municipality")) or "location not reported")}</p>'
            f'<p><b>{_esc(syn.get("headline"))}</b></p><h2>Company brief</h2>{brief}'
            + (f"<h2>What we could not establish</h2><ul>{unknowns}</ul>" if unknowns else "")
            + (f"<h2>Sources</h2><ol class=\"plain\">{source_list}</ol>" if source_list else "")
            + f"</article>{_noscript(site)}")


def build_site(envelopes: list[dict[str, Any]], site_dir: Path, report: dict[str, Any] | None = None, *, rebuild_synthesis: bool = False) -> Path:
    """Write the viewer for a run: shared assets, one page per route and per company, and the data files.

    ``rebuild_synthesis`` rebuilds every brief from the envelope's claims with the current
    ``synthesis.py`` (for viewing an older run); by default the envelope's own brief is shown,
    so the viewer says exactly what the envelope says."""
    site_dir = Path(site_dir)
    data_dir = site_dir / "data"
    companies_dir = data_dir / "companies"
    for stale_dir in (companies_dir, site_dir / "companies"):
        if stale_dir.exists():
            shutil.rmtree(stale_dir)
    companies_dir.mkdir(parents=True, exist_ok=True)
    assets = site_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for name in ("viewer.js", "viewer.css"):
        _write(assets / name, (ASSET_DIR / name).read_text(encoding="utf-8"))
    rows = []
    pages: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for env in envelopes:
        try:
            if rebuild_synthesis:
                env = dict(env, synthesis=rebuilt_synthesis(env))
            payload = company_payload(env)
            row = index_row(env, payload)
        except Exception as exc:  # one malformed envelope must not cost the whole viewer
            payload = {"org": env.get("organisation_number"), "error": f"{type(exc).__name__}: {exc}", "cl": [], "ev": {}, "src": [], "syn": {}, "ch": [], "fam": {}, "id": {}, "run": {}}
            row = {"o": env.get("organisation_number"), "n": env.get("organisation_number"), "err": True, "so": [], "ca": [], "src": {}}
        rows.append(row)
        org = str(payload.get("org"))
        _write(companies_dir / f"{org}.js", _js("company", org, payload))
        pages.append((org, env, payload, row))
    run = (report or {}).get("run") or (envelopes[0].get("run") if envelopes else {}) or {}
    previous = sorted({str((e.get("refresh") or {}).get("previous_run_id")) for e in envelopes if (e.get("refresh") or {}).get("previous_run_id")})
    meta = {
        "agent": "kildespor",
        "version": __version__,
        "run_id": run.get("run_id"),
        "completed_at": run.get("completed_at"),
        "count": len(envelopes),
        "previous_runs": previous,
        "baseline": not previous,
        "validation": ((report or {}).get("validation") or {}).get("passed"),
        "coverage": {k: v.get("share_available") for k, v in ((report or {}).get("coverage_by_family") or {}).items()},
        "requests": ((report or {}).get("operations") or {}).get("requests"),
        "generated_by": f"kildespor {__version__} site.py",
    }
    index = {"meta": meta, "rows": rows}
    _write(data_dir / "index.js", _js("index", None, index))
    stale_json = data_dir / "index.json"
    if stale_json.exists():
        stale_json.unlink()
    for org, env, payload, row in pages:
        body = company_body(env, payload, row, "../../", "../../") if not payload.get("error") else _noscript("../../")
        _write(site_dir / "companies" / org / "index.html", page_html("companies", root="../../", site="../../", org=org, body=body, page_title=f"{row.get('n')} ({org})", description=((payload.get("syn") or {}).get("headline") or None)))
    _write(site_dir / "index.html", page_html("research", root="", site="", body=research_body(rows, "", "")))
    _write(site_dir / "research" / "index.html", page_html("research", root="../", site="../", body=research_body(rows, "../", "../")))
    _write(site_dir / "compare" / "index.html", page_html("compare", root="../", site="../", body=f'<section class="card"><h1>Compare companies</h1><p>Choose two to four companies with their Compare button, or open <span class="mono">compare/index.html?orgs=&lt;org&gt;,&lt;org&gt;</span>.</p></section>{_noscript("../")}'))
    _write(site_dir / "refresh" / "index.html", page_html("refresh", root="../", site="../", body=f'<section class="card"><h1>Refresh: what changed in this run</h1><p>Run {_esc(meta["run_id"])}; {"baseline run, no previous snapshot" if meta["baseline"] else "compared with " + _esc(", ".join(previous))}.</p></section>{_noscript("../")}'))
    return site_dir / "index.html"


def write_root_page(out_dir: Path, site_dir: Path) -> Path | None:
    """``index.html`` beside the envelopes: the research page itself (not a redirect), reading the site's data,
    so the viewer is found and works where a checker looks first."""
    out_dir, site_dir = Path(out_dir), Path(site_dir)
    try:
        rel = os.path.relpath(site_dir, out_dir).replace(os.sep, "/")
    except ValueError:
        return None
    if rel in (".", ""):
        return None
    prefix = rel.rstrip("/") + "/"
    index = data_index(site_dir)
    rows = (index or {}).get("rows") or []
    path = out_dir / "index.html"
    _write(path, page_html("research", root=prefix, site=prefix, body=research_body(rows, prefix, prefix)))
    return path


def data_index(site_dir: Path) -> dict[str, Any] | None:
    """The run's index (meta and one row per company) read back from ``data/index.js``."""
    path = Path(site_dir) / "data" / "index.js"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    prefix = 'kildesporLoad("index",null,'
    if not text.startswith(prefix):
        return None
    return json.loads(text[len(prefix):].rstrip().rstrip(";").rstrip(")"))


def site_size(site_dir: Path) -> dict[str, Any]:
    """Bytes on disk by part of the site, for keeping page weight in check."""
    site_dir = Path(site_dir)
    parts = {"assets": site_dir / "assets", "index": site_dir / "data" / "index.js", "company_data": site_dir / "data" / "companies", "company_pages": site_dir / "companies"}
    out: dict[str, Any] = {}
    for key, p in parts.items():
        out[key] = p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0
    out["route_pages"] = sum((site_dir / r / "index.html").stat().st_size for r in ("research", "compare", "refresh") if (site_dir / r / "index.html").exists()) + ((site_dir / "index.html").stat().st_size if (site_dir / "index.html").exists() else 0)
    out["total"] = sum(v for v in out.values())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the static viewer from a run's envelopes")
    parser.add_argument("--input", default="out/envelopes.jsonl", help="envelopes JSONL (.gz accepted)")
    parser.add_argument("--output", default="out/site")
    parser.add_argument("--report", default=None, help="run-report.json (optional; adds run totals)")
    parser.add_argument("--rebuild-synthesis", action="store_true", help="rebuild every brief with the current synthesis.py (viewing an older run)")
    parser.add_argument("--root-page", action="store_true", help="also write index.html in the output folder's parent (next to the envelopes)")
    args = parser.parse_args()
    opener = gzip.open if args.input.endswith(".gz") else open
    with opener(args.input, "rt", encoding="utf-8") as fh:
        envelopes = [json.loads(line) for line in fh if line.strip()]
    report = json.loads(Path(args.report).read_text(encoding="utf-8")) if args.report and Path(args.report).exists() else None
    path = build_site(envelopes, Path(args.output), report, rebuild_synthesis=args.rebuild_synthesis)
    if args.root_page:
        write_root_page(Path(args.output).parent, Path(args.output))
    print(f"wrote {path} ({len(envelopes)} companies; {site_size(Path(args.output))['total'] / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
