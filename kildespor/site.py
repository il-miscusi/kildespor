"""Static, self-contained HTML viewer for a run's envelopes.

One file (``index.html``) with the data embedded, so it opens from disk or
any static host with no server. Users can search and filter companies, read
the evidence-linked brief, open every fact's source with its retrieval date
and content hash, compare up to four companies side by side, and see what
changed and what remains unknown. The layout is responsive for mobile.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import __version__


def _compact(env: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        e["id"]: {
            "u": e.get("final_url") or e.get("source_url"),
            "c": e.get("source_class"),
            "t": e.get("retrieved_at"),
            "h": (e.get("content_sha256") or "")[:12],
            "s": e.get("claim_span"),
            "m": e.get("extraction_method"),
            "p": e.get("reporting_period"),
        }
        for e in env.get("evidence", [])
    }
    claims = []
    for c in env.get("claims", []):
        item = {"f": c.get("field"), "fa": c.get("family"), "a": c.get("availability"), "v": c.get("value"), "e": c.get("evidence_ids", [])}
        for src, dst in (("label", "l"), ("effective_date", "d"), ("reporting_period", "rp"), ("relationship", "rel"), ("reason", "r"), ("confidence", "cf"), ("stale", "st")):
            if c.get(src) is not None:
                item[dst] = c[src]
        claims.append(item)
    return {
        "org": env["organisation_number"],
        "state": env.get("state"),
        "id": env.get("legal_identity", {}),
        "sec": {k: {"s": v["state"], "f": {fk: {"s": fv["state"], "r": fv.get("reason"), "n": fv.get("claim_count", 0)} for fk, fv in v["families"].items()}} for k, v in env.get("sections", {}).items()},
        "cl": claims,
        "ev": evidence,
        "syn": env.get("synthesis", {}),
        "ch": [
            {k: ch.get(k) for k in ("change_type", "field", "summary", "materiality", "previous", "current", "current_evidence_ids", "previous_evidence_ids", "detected_at")}
            for ch in env.get("changes", [])
        ],
        "rf": env.get("refresh", {}),
        "run": {k: env.get("run", {}).get(k) for k in ("run_id", "completed_at")},
    }


def build_site(envelopes: list[dict[str, Any]], site_dir: Path, report: dict[str, Any] | None = None) -> Path:
    site_dir.mkdir(parents=True, exist_ok=True)
    data = [_compact(env) for env in envelopes]
    meta = {
        "version": __version__,
        "run_id": (report or {}).get("run", {}).get("run_id"),
        "completed_at": (report or {}).get("run", {}).get("completed_at"),
        "count": len(envelopes),
        "validation": (report or {}).get("validation", {}).get("passed"),
        "coverage": {k: v.get("share_available") for k, v in (report or {}).get("coverage_by_family", {}).items()},
        "claims": (report or {}).get("available_claims"),
        "requests": (report or {}).get("operations", {}).get("requests"),
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).replace("</", "<\\/")
    meta_json = json.dumps(meta, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", payload).replace("__META__", meta_json)
    path = site_dir / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kildespor — Norwegian company research</title>
<meta name="description" content="Evidence-linked profiles of Norwegian companies: identity, accounts, leadership, workplaces, website, hiring and activity, with a source for every fact.">
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#14181f;--muted:#5b6472;--line:#e3e6eb;--accent:#0b5cad;--accent-soft:#e7f0fb;--ok:#137a3a;--ok-soft:#e5f5eb;--warn:#9a5b00;--warn-soft:#fff3e0;--bad:#b42318;--bad-soft:#fdecea;--chip:#eef1f5;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--panel:#171b23;--ink:#e8ebf0;--muted:#9aa4b2;--line:#2a303b;--accent:#6aa9ff;--accent-soft:#1b2a40;--ok:#4cc27a;--ok-soft:#14301f;--warn:#f0b04f;--warn-soft:#33270f;--bad:#ff7b6e;--bad-soft:#3a1714;--chip:#222833;--shadow:none}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent)}button,input,select{font:inherit;color:inherit}
header.top{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header.top h1{font-size:17px;margin:0;letter-spacing:.2px}header.top .run{color:var(--muted);font-size:13px}
.searchbar{flex:1;min-width:220px;display:flex;gap:8px}
.searchbar input{flex:1;padding:9px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg)}
.searchbar select{padding:9px;border:1px solid var(--line);border-radius:10px;background:var(--bg)}
.layout{display:grid;grid-template-columns:minmax(260px,360px) 1fr;gap:16px;padding:16px;max-width:1500px;margin:0 auto}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.listpanel{position:sticky;top:70px;max-height:calc(100vh - 86px);display:flex;flex-direction:column}
.filters{display:flex;flex-wrap:wrap;gap:6px;padding:10px;border-bottom:1px solid var(--line)}
.filters button{border:1px solid var(--line);background:var(--chip);border-radius:999px;padding:4px 10px;font-size:13px;cursor:pointer}
.filters button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.count{padding:6px 12px;color:var(--muted);font-size:13px}
#list{overflow:auto;padding:0 6px 8px}
.row{display:flex;justify-content:space-between;gap:8px;width:100%;text-align:left;padding:9px 10px;border:0;border-radius:10px;background:transparent;cursor:pointer}
.row:hover,.row:focus-visible{background:var(--accent-soft);outline:none}.row.active{background:var(--accent-soft);box-shadow:inset 3px 0 0 var(--accent)}
.row .n{font-weight:600;display:block}.row .m{color:var(--muted);font-size:12.5px;display:block}
.dots{display:flex;gap:3px;align-items:center;flex-shrink:0}.dot{width:8px;height:8px;border-radius:50%;background:var(--line)}.dot.on{background:var(--ok)}
main{min-width:0}
.profile{padding:18px 20px}
.ph{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start}
.ph h2{margin:0;font-size:22px}.ph .sub{color:var(--muted)}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.btn{border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:7px 12px;cursor:pointer;text-decoration:none;color:var(--ink);font-size:14px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin:14px 0}
.kv div{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.kv b{display:block;font-size:12px;color:var(--muted);font-weight:500}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-top:10px;overflow-x:auto}
.tabs button{border:0;background:transparent;padding:9px 12px;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;color:var(--muted)}
.tabs button[aria-selected=true]{border-color:var(--accent);color:var(--ink);font-weight:600}
.card{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:12px 0;background:var(--panel)}
.card h3{margin:0 0 6px;font-size:15px}
.state{display:inline-block;font-size:12px;padding:1px 8px;border-radius:999px;background:var(--chip);color:var(--muted);margin-left:6px;vertical-align:middle}
.state.available{background:var(--ok-soft);color:var(--ok)}.state.failed,.state.blocked{background:var(--bad-soft);color:var(--bad)}.state.ambiguous{background:var(--warn-soft);color:var(--warn)}
sup.src a{text-decoration:none;font-size:11px;padding:0 3px;border-radius:4px;background:var(--accent-soft)}
.signal{display:inline-block;margin:3px 4px 3px 0;padding:3px 9px;border-radius:999px;font-size:13px;background:var(--chip)}
.signal.positive{background:var(--ok-soft);color:var(--ok)}.signal.negative{background:var(--bad-soft);color:var(--bad)}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12.5px}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tablewrap{overflow-x:auto}
details.ev{margin:6px 0;border:1px solid var(--line);border-radius:10px;padding:6px 10px}details.ev summary{cursor:pointer;font-size:13.5px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;word-break:break-all}
.muted{color:var(--muted)}.small{font-size:13px}
#compareTray{position:fixed;bottom:12px;left:50%;transform:translateX(-50%);z-index:30;background:var(--panel);border:1px solid var(--line);box-shadow:0 6px 24px rgba(0,0,0,.18);border-radius:14px;padding:8px 12px;display:none;gap:8px;align-items:center;max-width:calc(100vw - 24px);flex-wrap:wrap}
#compareTray .pill{background:var(--chip);border-radius:999px;padding:3px 10px;font-size:13px}
.empty{padding:30px;color:var(--muted);text-align:center}
.backbtn{display:none}
.timeline li{margin:4px 0}
@media (max-width:860px){header.top{position:static}header.top a.btn{display:none}.layout{grid-template-columns:1fr;padding:10px}.listpanel{position:static;max-height:none}#list{max-height:none}
 body.viewing .listpanel{display:none} body.viewing .backbtn{display:inline-block} body:not(.viewing) main{display:none}
 .profile{padding:14px}.ph h2{font-size:19px}.kv{grid-template-columns:1fr 1fr}}
@media print{header.top,.listpanel,#compareTray,.actions{display:none}.layout{display:block}}
</style>
</head>
<body>
<header class="top">
  <h1>Kildespor</h1>
  <span class="run" id="runinfo"></span>
  <div class="searchbar" role="search">
    <label for="q" class="muted small" style="position:absolute;left:-9999px">Search companies</label>
    <input id="q" type="search" placeholder="Search name, brand, org. number, industry, place…" autocomplete="off">
    <label for="sort" style="position:absolute;left:-9999px">Sort</label>
    <select id="sort"><option value="data">Most data</option><option value="name">Name A–Z</option><option value="revenue">Revenue</option><option value="employees">Employees</option><option value="jobs">Active jobs</option></select>
  </div>
  <a class="btn" href="../envelopes.jsonl" download>Envelopes (JSONL)</a>
  <a class="btn" href="../run-report.json">Run report</a>
</header>
<div class="layout">
  <aside class="panel listpanel" aria-label="Companies">
    <div class="filters" id="filters"></div>
    <div class="count" id="count" aria-live="polite"></div>
    <div id="list" role="list"></div>
  </aside>
  <main><div class="panel" id="view"><div class="empty">Select a company.</div></div></main>
</div>
<div id="compareTray" role="region" aria-label="Compare tray"></div>
<script>
const DATA=__DATA__;
const META=__META__;
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl=u=>/^https?:\/\/[^\s"'<>]+$/i.test(String(u??''))?String(u):'#';
const hesc=v=>esc(safeUrl(v));
const norm=v=>String(v??'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase();
const SECTIONS=[["legal_identity_and_brand","Identity"],["annual_accounts","Accounts"],["leadership_and_workplaces","People & places"],["website_and_profiles","Website & profiles"],["hiring_and_activity","Hiring & activity"]];
const FAM={legal_identity:"Legal identity",public_brand:"Public brand",annual_accounts:"Annual accounts",leadership:"Leadership",workplaces:"Registered workplaces",official_website:"Official website",company_profiles:"Company profiles",contact_channels:"Contact channels",hiring:"Hiring",public_activity:"Public activity"};
const fmt=new Intl.NumberFormat('en-GB',{maximumFractionDigits:0});
function money(v,c){if(v==null)return'<span class="muted">not reported</span>';return esc(fmt.format(v)+' '+(c||'NOK'))}
function compact(v){if(v==null)return'—';const a=Math.abs(v);const s=v<0?'-':'';return a>=1e9?s+(a/1e9).toFixed(1)+' bn':a>=1e6?s+(a/1e6).toFixed(1)+' m':a>=1e3?s+(a/1e3).toFixed(0)+' k':s+a.toFixed(0)}
DATA.forEach((d,i)=>{d._i=i;d._claims=d.cl.filter(c=>c.a==='available');
  const by=f=>d._claims.filter(c=>c.f===f);d._by=by;
  d._score=SECTIONS.filter(([k])=>d.sec[k]&&d.sec[k].s==='available').length;
  const fin=by('financial.revenue').filter(c=>c.v.account_type==='SELSKAP').sort((a,b)=>b.v.fiscal_year.localeCompare(a.v.fiscal_year));
  d._rev=fin.length?fin[0].v.amount:null;d._emp=(by('registered_employees')[0]||{}).v??null;d._jobs=by('job_posting').length;
  d._site=by('official_website').length>0;d._social=by('social_profile').length;d._changes=(d.ch||[]).length;
  d._text=norm([d.id.legal_name,d.id.public_brand,d.org,d.id.municipality,d.id.industry&&d.id.industry.description,d.id.industry&&d.id.industry.code,d.id.official_website].join(' '));});
const FILTERS=[["site","Has website",d=>d._site],["social","Social profiles",d=>d._social>0],["jobs","Hiring now",d=>d._jobs>0],["fin","Has accounts",d=>d._rev!=null],["changes","Changed",d=>d._changes>0],["full","All 5 areas",d=>d._score===5]];
const active=new Set();let compare=[];let current=null;let tab='brief';
function renderFilters(){$('#filters').innerHTML=FILTERS.map(([k,l])=>`<button type="button" data-k="${k}" aria-pressed="${active.has(k)}">${l} (${DATA.filter(FILTERS.find(f=>f[0]===k)[2]).length})</button>`).join('');
  $('#filters').querySelectorAll('button').forEach(b=>b.onclick=()=>{active.has(b.dataset.k)?active.delete(b.dataset.k):active.add(b.dataset.k);renderFilters();renderList()})}
function rows(){const q=norm($('#q').value.trim());let r=DATA.filter(d=>(!q||q.split(/\s+/).every(t=>d._text.includes(t)))&&[...active].every(k=>FILTERS.find(f=>f[0]===k)[2](d)));
  const s=$('#sort').value;const nm=d=>d.id.legal_name||d.org;
  r.sort((a,b)=>s==='name'?nm(a).localeCompare(nm(b),'nb'):s==='revenue'?(b._rev??-1e18)-(a._rev??-1e18):s==='employees'?(b._emp??-1)-(a._emp??-1):s==='jobs'?b._jobs-a._jobs:(b._score-a._score)||(b._claims.length-a._claims.length));return r}
function dots(d){return '<span class="dots" aria-label="'+d._score+' of 5 areas with data">'+SECTIONS.map(([k,l])=>`<span class="dot ${d.sec[k]&&d.sec[k].s==='available'?'on':''}" title="${l}"></span>`).join('')+'</span>'}
function renderList(){const r=rows();$('#count').textContent=`${r.length} of ${DATA.length} companies`;
  $('#list').innerHTML=r.slice(0,600).map(d=>`<button type="button" role="listitem" class="row ${current===d?'active':''}" data-i="${d._i}"><span><span class="n">${esc(d.id.legal_name||d.org)}</span><span class="m">${esc(d.org)} · ${esc((d.id.municipality||'').toString())} · ${esc(d.id.industry?d.id.industry.description:'')}</span></span>${dots(d)}</button>`).join('')+(r.length>600?'<div class="empty">Refine the search to see more.</div>':'');
  $('#list').querySelectorAll('.row').forEach(b=>b.onclick=()=>{open(DATA[b.dataset.i]);})}
function src(d,ids){if(!ids||!ids.length)return'';return '<sup class="src">'+ids.slice(0,4).map(id=>`<a href="#ev-${esc(id)}" data-ev="${esc(id)}" data-org="${esc(d.org)}" title="${esc((d.ev[id]||{}).u||'')}">src</a>`).join(' ')+'</sup>'}
function openEvidence(org,id){const d=DATA.find(x=>x.org===org);if(!d)return;tab='evidence';open(d,false);const el=document.getElementById('ev-'+id);if(el){el.open=true;el.scrollIntoView({block:'center'})}}
function evLink(d,ids){if(!ids||!ids.length)return'';const e=d.ev[ids[0]];if(!e)return'';return `<a class="small" href="${hesc(e.u)}" target="_blank" rel="noopener noreferrer">source</a> <span class="muted small">${esc((e.t||'').slice(0,10))}</span> ${src(d,ids)}`}
function stateBadge(s){return `<span class="state ${esc(s)}">${esc((s||'').replace('_',' '))}</span>`}
function famNote(d,fam){for(const [k] of SECTIONS){const f=d.sec[k]&&d.sec[k].f[fam];if(f)return f}return null}
function unknownLine(d,fam){const f=famNote(d,fam);if(!f||f.s==='available')return'';return `<p class="muted small">${FAM[fam]}: ${stateBadge(f.s)} ${esc(f.r||'')}</p>`}
function brief(d){const syn=d.syn||{};let h='';
  if(syn.signals&&syn.signals.length)h+='<div>'+syn.signals.map(s=>`<span class="signal ${esc(s.tone)}">${esc(s.text)} ${src(d,s.evidence_ids)}</span>`).join('')+'</div>';
  (syn.sections||[]).forEach(s=>{let body=s.sentences&&s.sentences.length?s.sentences.map(x=>esc(x.text)+' '+src(d,x.evidence_ids)).join(' '):esc(s.text);h+=`<div class="card"><h3>${esc(s.heading)}</h3><p style="margin:0">${body}</p></div>`});return h}
function identity(d){const by=d._by;const rowsH=[];const add=(label,c,val)=>{if(c)rowsH.push(`<tr><th>${esc(label)}</th><td>${val??esc(typeof c.v==='object'?JSON.stringify(c.v):c.v)}</td><td>${evLink(d,c.e)}</td></tr>`)};
  const one=f=>by(f)[0];
  add('Organisation number',one('organisation_number'));add('Legal name',one('legal_name'));add('Public brand',one('public_brand_name'));
  const lf=one('legal_form');add('Legal form',lf,lf&&esc(lf.v.description+' ('+lf.v.code+')'));
  add('Business address',one('registered_business_address'));add('Postal address',one('postal_address'));
  by('industry_code').forEach(c=>add('Industry',c,esc(c.v.code+' '+c.v.description)));
  add('Registered employees',one('registered_employees'));add('Founded',one('founded_date'));add('Registered',one('registered_date'));
  const sc=one('share_capital');add('Share capital',sc,sc&&money(sc.v.amount,sc.v.currency));
  add('Registry status',one('registry_status'));add('VAT registered',one('vat_registered'));add('Part of a group',one('part_of_group'));
  add('Registered activity',one('registered_activity'));add('Statutory purpose',one('statutory_purpose'));add('Self-description (website)',one('self_description'));
  by('former_name').forEach(c=>add('Former name',c,esc(c.v.name+' ('+c.v.from+' – '+c.v.to+')')));
  by('group_relationship').forEach(c=>add('Group: '+c.v.relationship.replace('_',' '),c,esc((c.v.name||'')+' ('+c.v.organisation_number+')'+(c.v.ownership?' · '+c.v.ownership:''))));
  return `<div class="card"><h3>Legal identity and brand</h3><div class="tablewrap"><table>${rowsH.join('')}</table></div>${unknownLine(d,'public_brand')}</div>`}
function accounts(d){const fin=d._claims.filter(c=>c.f.startsWith('financial.')&&c.v&&c.v.amount!==undefined);
  if(!fin.length)return `<div class="card"><h3>Annual accounts</h3>${unknownLine(d,'annual_accounts')||'<p class="muted">No figures.</p>'}</div>`;
  let h='';for(const t of ['KONSERN','SELSKAP']){const f=fin.filter(c=>c.v.account_type===t);if(!f.length)continue;
    const years=[...new Set(f.map(c=>c.v.fiscal_year))].sort().reverse();const metrics=[...new Set(f.map(c=>c.f))];
    const label=c=>c.l||c.f.replace('financial.','');
    h+=`<div class="card"><h3>${t==='KONSERN'?'Group (consolidated)':'Company'} accounts</h3><div class="tablewrap"><table><tr><th>Metric</th>${years.map(y=>`<th class="num">${y}</th>`).join('')}</tr>`+
      metrics.map(m=>{const any=f.find(c=>c.f===m);return `<tr><td>${esc(label(any))}</td>`+years.map(y=>{const c=f.find(x=>x.f===m&&x.v.fiscal_year===y);return `<td class="num">${c?esc(compact(c.v.amount))+' '+src(d,c.e):'—'}</td>`}).join('')+'</tr>'}).join('')+
      `</table></div><p class="muted small">Amounts in ${esc(f[0].v.currency)}; source: Register of Company Accounts (Regnskapsregisteret). Missing values are shown as —, never as zero.</p></div>`}
  return h}
function people(d){const roles=d._by('role');const units=d._by('registered_workplace');let h='<div class="card"><h3>Leadership and board</h3>';
  h+=roles.length?'<div class="tablewrap"><table><tr><th>Role</th><th>Name</th><th>Since change</th><th>Source</th></tr>'+roles.map(c=>`<tr><td>${esc(c.v.role)}</td><td>${esc(c.v.name)}${c.v.organisation_number?' <span class="muted">('+esc(c.v.organisation_number)+')</span>':''}</td><td>${esc(c.d||'')}</td><td>${evLink(d,c.e)}</td></tr>`).join('')+'</table></div>':unknownLine(d,'leadership');
  h+='</div><div class="card"><h3>Registered workplaces</h3>';
  h+=units.length?'<div class="tablewrap"><table><tr><th>Name</th><th>Address</th><th>Employees</th><th>Source</th></tr>'+units.map(c=>`<tr><td>${esc(c.v.name)} <span class="muted">${esc(c.v.organisation_number)}</span></td><td>${esc(c.v.address||'')}</td><td class="num">${c.v.employees??'—'}</td><td>${evLink(d,c.e)}</td></tr>`).join('')+'</table></div>':unknownLine(d,'workplaces');
  return h+'</div>'}
function web(d){const site=d._by('official_website')[0];let h='<div class="card"><h3>Official website</h3>';
  if(site){h+=`<p><a href="${hesc(site.v.url)}" target="_blank" rel="noopener noreferrer">${esc(site.v.url)}</a> ${src(d,site.e)}<br><span class="muted small">Identity: ${esc(site.v.identity_method.replace(/_/g,' '))}${site.rel?' · labelled '+esc(site.rel.replace(/_/g,' ')):''}</span></p>`}else h+=unknownLine(d,'official_website');
  const reg=d._by('registry_website')[0];if(reg)h+=`<p class="small">Listed in the Entity Register: ${esc(reg.v)} ${src(d,reg.e)}</p>`;
  h+='</div><div class="card"><h3>Company-owned profiles</h3>';const so=d._by('social_profile');
  h+=so.length?'<ul>'+so.map(c=>`<li><b>${esc(c.v.platform)}</b>: <a href="${hesc(c.v.url)}" target="_blank" rel="noopener noreferrer">${esc(c.v.url)}</a> ${src(d,c.e)}</li>`).join('')+'</ul>':unknownLine(d,'company_profiles');
  h+='</div><div class="card"><h3>Contact channels</h3>';const cc=d._claims.filter(c=>c.fa==='contact_channels');
  h+=cc.length?'<ul>'+cc.map(c=>`<li>${esc(c.l||c.f)}: ${esc(c.v)} ${src(d,c.e)}</li>`).join('')+'</ul>':unknownLine(d,'contact_channels');
  return h+'</div>'}
function hiring(d){const jobs=d._by('job_posting');const car=d._by('careers_page');let h='<div class="card"><h3>Hiring</h3>';
  if(car.length)h+=`<p>Careers page: <a href="${hesc(car[0].v)}" target="_blank" rel="noopener noreferrer">${esc(car[0].v)}</a> ${src(d,car[0].e)}</p>`;
  h+=jobs.length?'<div class="tablewrap"><table><tr><th>Job</th><th>Location</th><th>Published</th><th>Expires</th><th>Source</th></tr>'+jobs.map(c=>`<tr><td><a href="${hesc(c.v.url)}" target="_blank" rel="noopener noreferrer">${esc(c.v.title)}</a></td><td>${esc(c.v.location||'')}</td><td>${esc(c.v.published||c.v.date_posted||'')}</td><td>${esc(c.v.expires||c.v.valid_through||'')}</td><td>${evLink(d,c.e)}</td></tr>`).join('')+'</table></div>':unknownLine(d,'hiring');
  h+='</div><div class="card"><h3>Dated public activity</h3>';const act=d._claims.filter(c=>c.fa==='public_activity').sort((a,b)=>String(b.d||b.v.date||'').localeCompare(String(a.d||a.v.date||'')));
  h+=act.length?'<ul class="timeline">'+act.map(c=>`<li><b>${esc(c.v.date||c.d||'')}</b> · ${c.v.url?`<a href="${hesc(c.v.url)}" target="_blank" rel="noopener noreferrer">${esc(c.v.title||c.v.event)}</a>`:esc(c.v.title||c.v.event)} <span class="muted small">${esc(c.l||'')}</span> ${src(d,c.e)}</li>`).join('')+'</ul>':unknownLine(d,'public_activity');
  return h+'</div>'}
function changes(d){let h='<div class="card"><h3>What changed since the previous run</h3>';
  if(!d.rf||d.rf.baseline){h+='<p class="muted">This run is the baseline snapshot: no previous run was supplied for this company.</p>'}
  else if(!d.ch.length){h+=`<p>No changes since run ${esc(d.rf.previous_run_id||'')}.</p>`}
  else h+='<div class="tablewrap"><table><tr><th>Change</th><th>Before</th><th>Now</th><th>Materiality</th></tr>'+d.ch.map(c=>`<tr><td>${esc(c.change_type.replace(/_/g,' '))}</td><td class="small">${esc(c.previous==null?'—':JSON.stringify(c.previous).slice(0,140))} ${src(d,c.previous_evidence_ids)}</td><td class="small">${esc(c.current==null?'—':JSON.stringify(c.current).slice(0,140))} ${src(d,c.current_evidence_ids)}</td><td>${esc(c.materiality)}</td></tr>`).join('')+'</table></div>';
  h+='</div><div class="card"><h3>Availability of every area</h3><div class="tablewrap"><table><tr><th>Area</th><th>State</th><th>Reason</th></tr>';
  for(const [k] of SECTIONS){const s=d.sec[k];if(!s)continue;for(const [fk,fv] of Object.entries(s.f))h+=`<tr><td>${esc(FAM[fk]||fk)}</td><td>${stateBadge(fv.s)}</td><td class="small">${esc(fv.r||'')} ${fv.n?'<span class="muted">('+fv.n+' facts)</span>':''}</td></tr>`}
  return h+'</table></div></div>'}
function evidence(d){const ids=Object.keys(d.ev);return `<div class="card"><h3>Evidence (${ids.length})</h3><p class="muted small">Every fact links here. Each record keeps the source URL, retrieval time, content hash and the span the fact was read from.</p>`+ids.map(id=>{const e=d.ev[id];return `<details class="ev" id="ev-${esc(id)}"><summary>${esc(e.c||'')} · ${esc((e.t||'').slice(0,16).replace('T',' '))} · <span class="mono">${esc(id)}</span></summary><div class="small"><a class="mono" href="${hesc(e.u)}" target="_blank" rel="noopener noreferrer">${esc(e.u)}</a><br>Span: <span class="mono">${esc(e.s||'—')}</span><br>Method: ${esc(e.m||'')} · sha256 ${esc(e.h)}…${e.p?' · period '+esc(e.p):''}</div></details>`}).join('')+'</div>'}
const TABS=[["brief","Brief",brief],["identity","Identity",identity],["accounts","Accounts",accounts],["people","People & places",people],["web","Website & profiles",web],["hiring","Hiring & activity",hiring],["changes","Changes & gaps",changes],["evidence","Evidence",evidence]];
function open(d,push=true){current=d;document.body.classList.add('viewing');const by=d._by;const site=by('official_website')[0];
  const kv=[['Org. number',d.org],['Legal form',d.id.legal_form&&d.id.legal_form.code],['Municipality',d.id.municipality],['Employees (register)',d._emp],['Revenue (latest, company)',d._rev!=null?compact(d._rev)+' NOK':null],['Active jobs',d._jobs||null]].filter(x=>x[1]!=null&&x[1]!=='');
  $('#view').innerHTML=`<div class="profile"><div class="ph"><div><button class="btn backbtn" id="back" type="button">← Companies</button><h2>${esc(d.id.legal_name||d.org)}</h2><div class="sub">${esc(d.id.public_brand&&d.id.public_brand!==d.id.legal_name?d.id.public_brand+' · ':'')}${esc(d.id.industry?d.id.industry.description:'')}${site?' · <a href="'+esc(site.v.url)+'" target="_blank" rel="noopener noreferrer">'+esc(site.v.url.replace(/^https?:\/\//,'').replace(/\/$/,''))+'</a>':''}</div></div>
   <div class="actions"><button class="btn ${compare.includes(d.org)?'primary':''}" id="cmpbtn" type="button">${compare.includes(d.org)?'In comparison':'Compare'}</button><button class="btn" id="copylink" type="button">Copy link</button></div></div>
   <div class="kv">${kv.map(([k,v])=>`<div><b>${esc(k)}</b>${esc(v)}</div>`).join('')}</div>
   <div class="tabs" role="tablist">${TABS.map(([k,l])=>`<button role="tab" type="button" data-t="${k}" aria-selected="${tab===k}">${l}</button>`).join('')}</div><div id="tabbody" role="tabpanel"></div></div>`;
  renderTab();$('#view').querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{tab=b.dataset.t;$('#view').querySelectorAll('.tabs button').forEach(x=>x.setAttribute('aria-selected',x===b));renderTab()});
  $('#cmpbtn').onclick=()=>{toggleCompare(d.org);open(d,false)};$('#copylink').onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText(location.href.split('#')[0]+'#org='+d.org)};
  $('#back').onclick=()=>{document.body.classList.remove('viewing');history.replaceState(null,'','#')};
  if(push)history.replaceState(null,'','#org='+d.org);renderList();window.scrollTo({top:0})}
function renderTab(){const d=current;const t=TABS.find(x=>x[0]===tab)||TABS[0];$('#tabbody').innerHTML=t[2](d);
  $('#tabbody').querySelectorAll('a[data-ev]').forEach(a=>a.onclick=e=>{e.preventDefault();openEvidence(a.dataset.org||d.org,a.dataset.ev)})}
function toggleCompare(org){compare=compare.includes(org)?compare.filter(x=>x!==org):[...compare,org].slice(-4);renderTray()}
function renderTray(){const t=$('#compareTray');if(!compare.length){t.style.display='none';return}t.style.display='flex';
  t.innerHTML='<b class="small">Compare:</b>'+compare.map(o=>{const d=DATA.find(x=>x.org===o);return `<span class="pill">${esc((d.id.legal_name||o).slice(0,28))}</span>`}).join('')+`<button class="btn primary" id="docmp" type="button" ${compare.length<2?'disabled':''}>Compare ${compare.length}</button><button class="btn" id="clrcmp" type="button">Clear</button>`;
  $('#docmp').onclick=()=>showCompare();$('#clrcmp').onclick=()=>{compare=[];renderTray();if(current)open(current,false)}}
function latest(d,metric,type){const c=d._by('financial.'+metric).filter(c=>c.v.account_type===(type||'SELSKAP')).sort((a,b)=>b.v.fiscal_year.localeCompare(a.v.fiscal_year))[0];return c}
function showCompare(){document.body.classList.add('viewing');const ds=compare.map(o=>DATA.find(x=>x.org===o));
  const row=(label,fn)=>`<tr><th>${esc(label)}</th>${ds.map(d=>`<td>${fn(d)??'—'}</td>`).join('')}</tr>`;
  const fin=(m)=>d=>{const c=latest(d,m);return c?esc(compact(c.v.amount))+' <span class="muted small">('+esc(c.v.fiscal_year)+')</span> '+src(d,c.e):'<span class="muted">not reported</span>'};
  const eqr=d=>{const e=latest(d,'equity'),a=latest(d,'total_assets');return e&&a&&a.v.amount?(e.v.amount/a.v.amount*100).toFixed(0)+'%':'—'};
  const role=code=>d=>{const c=d._by('role').find(c=>c.v.role_code===code);return c?esc(c.v.name)+' '+src(d,c.e):'—'};
  $('#view').innerHTML=`<div class="profile"><div class="ph"><div><button class="btn backbtn" id="back" type="button">← Companies</button><h2>Comparison</h2><div class="sub">Every cell links to its source.</div></div><div class="actions"><button class="btn" id="copycmp" type="button">Copy link</button></div></div><div class="tablewrap"><table>
   <tr><th></th>${ds.map(d=>`<th><a href="#org=${d.org}" data-open="${d.org}">${esc(d.id.legal_name||d.org)}</a></th>`).join('')}</tr>
   ${row('Org. number',d=>esc(d.org))}${row('Legal form',d=>esc(d.id.legal_form&&d.id.legal_form.description))}${row('Municipality',d=>esc(d.id.municipality))}${row('Industry',d=>esc(d.id.industry&&d.id.industry.description))}
   ${row('Employees (register)',d=>d._emp??'—')}${row('Revenue',fin('revenue'))}${row('Operating result',fin('operating_result'))}${row('Annual result',fin('annual_result'))}${row('Equity',fin('equity'))}${row('Equity ratio',eqr)}
   ${row('General manager',role('DAGL'))}${row('Chair',role('LEDE'))}${row('Workplaces',d=>d._by('registered_workplace').length)}
   ${row('Website',d=>{const s=d._by('official_website')[0];return s?`<a href="${hesc(s.v.url)}" target="_blank" rel="noopener noreferrer">${esc(s.v.url.replace(/^https?:\/\//,''))}</a>`:'—'})}${row('Social profiles',d=>d._by('social_profile').map(c=>esc(c.v.platform)).join(', ')||'—')}
   ${row('Active job ads',d=>d._jobs)}${row('Data areas',d=>d._score+' / 5')}</table></div></div>`;
  $('#view').querySelectorAll('a[data-open]').forEach(a=>a.onclick=e=>{e.preventDefault();open(DATA.find(x=>x.org===a.dataset.open))});
  $('#view').querySelectorAll('a[data-ev]').forEach(a=>a.onclick=e=>{e.preventDefault();openEvidence(a.dataset.org,a.dataset.ev)});
  $('#back').onclick=()=>{document.body.classList.remove('viewing')};$('#copycmp').onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText(location.href.split('#')[0]+'#compare='+compare.join(','))};
  history.replaceState(null,'','#compare='+compare.join(','));}
function route(){const h=location.hash.slice(1);const p=new URLSearchParams(h);if(p.get('org')){const d=DATA.find(x=>x.org===p.get('org'));if(d){open(d,false);return}}
  if(p.get('compare')){compare=p.get('compare').split(',').filter(o=>DATA.some(x=>x.org===o)).slice(0,4);renderTray();if(compare.length>1){showCompare();return}}
  if(window.innerWidth>860&&DATA.length){open(rows()[0]||DATA[0],false)}}
$('#runinfo').textContent=`${META.count} companies · run ${META.run_id||''} · ${String(META.completed_at||'').slice(0,16).replace('T',' ')} UTC`;
$('#q').addEventListener('input',renderList);$('#sort').addEventListener('change',renderList);
document.addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement!==$('#q')){e.preventDefault();$('#q').focus()}});
renderFilters();renderList();route();
</script>
</body>
</html>
"""
