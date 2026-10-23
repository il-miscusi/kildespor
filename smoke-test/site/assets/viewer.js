/* Kildespor viewer: find, compare and verify the companies of one run.
 *
 * Every page of a run's site (research/, companies/<org>/, compare/, refresh/, the root index)
 * is a small HTML shell that sets window.KD_CONF and loads this script and data/index.js.
 * Company profiles load on demand from data/companies/<org>.js, so the viewer works from
 * file://, any static host and kildespor.serve alike. No network calls, no dependencies.
 *
 * KD_CONF: {root: prefix for route links, site: prefix for assets and data, route, org, server}
 */
(function () {
"use strict";
var C = window.KD_CONF || {root: "", site: "", route: "research", org: null, server: false};
var KD = window.KD = {meta: {}, rows: [], byOrg: {}, cache: {}, waiting: {}, compare: [], shown: 50, filtered: []};
window.kildesporLoad = function (kind, key, data) {
  if (kind === "index") {
    KD.loaded = true; KD.meta = data.meta || {}; KD.rows = data.rows || [];
    KD.rows.forEach(function (r) { KD.byOrg[r.o] = r; });
    if (KD.onIndex) KD.onIndex();
  } else if (kind === "company") {
    KD.cache[key] = data;
    (KD.waiting[key] || []).forEach(function (f) { f(data); });
    delete KD.waiting[key];
  }
};

/* ---------- small helpers ---------- */
var $ = function (s, el) { return (el || document).querySelector(s); };
var $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };
function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
function title(s) { return String(s || "").toLowerCase().replace(/(^|[\s-])([a-zæøåéü])/g, function (m, a, b) { return a + b.toUpperCase(); }); }
function money(a, cur) { if (a == null) return "not reported"; cur = cur || "NOK"; var s = a < 0 ? "-" : "", v = Math.abs(a), t; if (v >= 1e9) t = (v / 1e9).toFixed(2) + " bn"; else if (v >= 1e6) t = (v / 1e6).toFixed(1) + " m"; else if (v >= 1e4) t = Math.round(v / 1e3) + " k"; else t = Math.round(v).toLocaleString("en-US"); return cur + " " + s + t; }
function pct(x) { return x == null ? "" : (x >= 0 ? "+" : "") + Math.round(x * 100) + "%"; }
function safeUrl(u) { u = String(u || ""); return /^https?:\/\//i.test(u) ? u : null; }
function link(u, label) { var s = safeUrl(u); return s ? '<a href="' + esc(s) + '" target="_blank" rel="noopener noreferrer">' + esc(label || u) + '</a>' : esc(label || u || ""); }
function host(u) { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } }
function store(k, v) { try { if (v === undefined) return JSON.parse(localStorage.getItem(k) || "null"); localStorage.setItem(k, JSON.stringify(v)); } catch (e) { return null; } }
function stateName(s) { return {available: "Available", not_available: "Not available", ambiguous: "Ambiguous", failed: "Failed", blocked: "Blocked", not_applicable: "Not applicable"}[s] || (s ? String(s).replace(/_/g, " ") : "Not checked"); }
function stateBadge(s) { return '<span class="state ' + esc(s || "") + '">' + esc(stateName(s)) + '</span>'; }
function parseQS(q) { var o = {}; String(q || "").split("&").forEach(function (kv) { if (!kv) return; var p = kv.split("="); try { o[decodeURIComponent(p[0])] = decodeURIComponent((p[1] || "").replace(/\+/g, " ")); } catch (e) {} }); return o; }
function qs(o) { return Object.keys(o).filter(function (k) { return o[k] !== "" && o[k] != null; }).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(o[k]).replace(/%2C/g, ","); }).join("&"); }
/* A link to a route page: /companies/<org> on the server, companies/<org>/index.html from disk. */
function href(route, query) {
  var q = query ? "?" + query : "";
  if (C.server) return C.root + route + q;
  return C.root + route + "/" + (location.protocol === "file:" ? "index.html" : "") + q;
}
function companyHref(org) { return href("companies/" + encodeURIComponent(org)); }
function compareHref(orgs) { return href("compare", orgs && orgs.length ? "orgs=" + orgs.join(",") : ""); }

var AREAS = ["Company record", "Financials", "People & locations", "Company website", "Public footprint"];
var FAMILY = {legal_identity: "Legal identity", public_brand: "Brand & self-description", annual_accounts: "Annual accounts", leadership: "Leadership & board", workplaces: "Registered workplaces", official_website: "Official website", company_profiles: "Social profiles", contact_channels: "Contact channels", hiring: "Hiring (single job ads)", public_activity: "Dated news & activity"};
var METRICS = [["revenue", "Revenue"], ["operating_result", "Operating result"], ["annual_result", "Net result"], ["equity", "Equity"], ["total_assets", "Total assets"], ["total_debt", "Total liabilities"]];
var ROLE_EN = {DAGL: "general manager", LEDE: "board chair", NEST: "deputy chair", MEDL: "board member", VARA: "deputy board member", REVI: "auditor", REGN: "accountant", INNH: "owner", DTPR: "partner", DTSO: "partner", KOMP: "general partner", "FFØR": "business manager", KONT: "contact person", HFOR: "head office abroad"};
var SECTION_TITLES = {what: "Company overview", money: "Latest financials", who: "Who runs it?", hiring: "Working here", activity: "Recent activity", changed: "What changed", unknown: "What we could not establish"};

/* ---------- data loading ---------- */
function loadCompany(org, cb) {
  if (KD.cache[org]) return cb(KD.cache[org]);
  (KD.waiting[org] = KD.waiting[org] || []).push(cb);
  if (KD.waiting[org].length > 1) return;
  var s = document.createElement("script");
  s.src = C.site + "data/companies/" + encodeURIComponent(org) + ".js";
  s.onerror = function () { var w = KD.waiting[org] || []; delete KD.waiting[org]; w.forEach(function (f) { f(null); }); };
  document.head.appendChild(s);
}
function loadMany(orgs, cb) { var out = {}, n = orgs.length; if (!n) return cb(out); orgs.forEach(function (o) { loadCompany(o, function (d) { out[o] = d; if (--n === 0) cb(out); }); }); }

/* ---------- evidence ---------- */
function evOf(d, id) { var e = d.ev[id]; if (!e) return null; var s = (d.src || [])[e.r] || {}; return {u: s.u, c: s.c, t: s.t, h: s.h, hs: s.hs, a: s.a, ru: s.ru, prev: s.prev, s: e.s, m: e.m, p: e.p}; }
function claimText(c) { var v = c.v; if (v == null) return ""; if (typeof v !== "object") return String(v); if (v.amount != null) return money(v.amount, v.currency) + (v.fiscal_year ? " (" + v.fiscal_year + (v.account_type === "KONSERN" ? ", group" : "") + ")" : ""); return v.title || v.name && (v.name + (v.role ? " — " + v.role : "")) || v.url || v.description || v.event || JSON.stringify(v); }
function evBlock(d, id) {
  var e = evOf(d, id); if (!e) return "";
  return '<div class="evi"><dl>' +
    '<dt>Source</dt><dd>' + (safeUrl(e.u) ? link(e.u) : esc(e.u || "not recorded")) + (e.prev ? ' <span class="badge warn">previous run</span>' : "") + '</dd>' +
    '<dt>Retrieved</dt><dd>' + esc(e.t || "not recorded") + '</dd>' +
    '<dt>Quoted span</dt><dd>' + (e.s ? '<blockquote>' + esc(e.s) + '</blockquote>' : '<span class="muted">none: the value was read from a structured field</span>') + '</dd>' +
    '<dt>Method</dt><dd>' + esc(e.m || "") + '</dd>' +
    (e.c ? '<dt>Source type</dt><dd>' + esc(e.c) + '</dd>' : "") +
    (e.p ? '<dt>Period</dt><dd>' + esc(e.p) + '</dd>' : "") +
    (e.hs ? '<dt>HTTP status</dt><dd>' + esc(e.hs) + '</dd>' : "") +
    (e.ru ? '<dt>Requested</dt><dd>' + esc(e.ru) + '</dd>' : "") +
    (e.a ? '<dt>Terms</dt><dd>' + esc(e.a) + '</dd>' : "") +
    '<dt>SHA-256</dt><dd class="mono">' + esc(e.h || "") + '</dd>' +
    '<dt>Evidence id</dt><dd class="mono">' + esc(id) + '</dd></dl></div>';
}
/* Inline evidence control for one fact: a disclosure with the source URL, retrieval time and span. */
function viewSource(d, c, label) {
  var ids = (c && c.e) || []; if (!ids.length) return "";
  return '<details class="evidence"><summary>View source<span class="sr"> for ' + esc(label || (c.l || c.f)) + '</span></summary>' + ids.map(function (id) { return evBlock(d, id); }).join("") + (c.st ? '<p class="small"><span class="stale-mark">not re-checked this run</span> carried forward from the previous run</p>' : "") + (c.fo ? '<p class="small muted">First observed ' + esc(c.fo) + '</p>' : "") + '</details>';
}
/* Compact evidence button (tables, sentences): opens the evidence dialog. */
function chip(org, n, ev, cl, basis, label, checked) {
  ev = ev || []; cl = cl || []; checked = checked || [];
  if (!ev.length && !checked.length && !cl.length) return "";
  var cls = "cite" + (basis === "computed" ? " computed" : basis === "carried_forward" ? " stale" : "");
  return '<button type="button" class="' + cls + '" data-org="' + esc(org) + '" data-ev="' + esc(ev.join(",")) + '" data-cl="' + esc(cl.join(",")) + '" data-basis="' + esc(basis || "") + '" data-label="' + esc(label || "") + '" data-k="' + esc(checked.join("|")) + '" aria-haspopup="dialog" aria-label="View source for ' + esc(String(label || "this statement").slice(0, 80)) + '">' + esc(n) + '</button>';
}
function factChip(org, c, n) { return chip(org, n || "✓", c.e, [c.id], c.st ? "carried_forward" : "fact", (c.l || c.f) + ": " + claimText(c)); }
function sentences(org, list, counter) { return (list || []).map(function (s) { counter.n++; return esc(s.t) + (s.b === "carried_forward" ? '<span class="stale-mark">carried forward</span>' : "") + " " + chip(org, counter.n, s.e, s.c, s.b, s.t, s.k); }).join(" "); }
var lastFocus = null;
function openDialog(html, heading) {
  var dlg = $("#evidence-dialog"); lastFocus = document.activeElement;
  $("#dlg-title").textContent = heading || "Evidence"; $("#dlg-body").innerHTML = html;
  if (dlg.showModal) { try { dlg.showModal(); } catch (e) { dlg.setAttribute("open", ""); } } else dlg.setAttribute("open", "");
  var b = $("#dlg-close"); if (b) b.focus();
}
function closeDialog() { var dlg = $("#evidence-dialog"); if (!dlg) return; if (dlg.close && dlg.open) dlg.close(); else dlg.removeAttribute("open"); if (lastFocus && lastFocus.focus) lastFocus.focus(); }
function verify(el) {
  var org = el.getAttribute("data-org"), ev = (el.getAttribute("data-ev") || "").split(",").filter(Boolean), cl = (el.getAttribute("data-cl") || "").split(",").filter(Boolean), k = (el.getAttribute("data-k") || "").split("|").filter(Boolean), label = el.getAttribute("data-label") || "", basis = el.getAttribute("data-basis");
  loadCompany(org, function (d) {
    if (!d) { openDialog('<p class="state failed">The company file could not be loaded.</p>'); return; }
    var claims = d.cl.filter(function (c) { return cl.indexOf(c.id) >= 0; });
    var h = label ? '<p><b>' + esc(label) + '</b></p>' : "";
    if (basis === "computed") h += '<p class="small muted">Computed from the filed figures cited below (arithmetic only).</p>';
    if (basis === "absence" || basis === "unknown") h += '<p class="small muted">A statement about what the checked sources did not contain. Missing data is not a negative fact.</p>';
    h += '<h3>Sources</h3>';
    if (!ev.length && !k.length) h += '<p class="muted">No source record: this statement describes the run itself.</p>';
    ev.forEach(function (id) { h += evBlock(d, id); });
    if (k.length) h += '<p class="small">Sources checked:</p><ul class="small">' + k.map(function (u) { return '<li>' + link(u) + '</li>'; }).join("") + '</ul>';
    if (claims.length) h += '<h3>Facts this rests on</h3><ul class="plain">' + claims.map(function (c) { return '<li><b>' + esc(c.l || c.f) + '</b>: ' + esc(claimText(c)) + (c.st ? '<span class="stale-mark">not re-checked this run</span>' : "") + (c.fo ? '<div class="small muted">first observed ' + esc(c.fo) + '</div>' : "") + (c.cr ? '<div class="small muted">' + esc(c.cr) + '</div>' : "") + '</li>'; }).join("") + '</ul>';
    openDialog(h, "Evidence: " + (KD.byOrg[org] ? KD.byOrg[org].n : org));
  });
}
document.addEventListener("click", function (e) {
  var t = e.target.closest ? e.target.closest("[data-close]") : null; if (t) { closeDialog(); return; }
  var c = e.target.closest ? e.target.closest("button[data-ev]") : null; if (c) { e.preventDefault(); verify(c); return; }
  var p = e.target.closest ? e.target.closest("button[data-pick]") : null; if (p) { e.preventDefault(); toggleCompare(p.getAttribute("data-pick")); }
});
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") closeDialog();
  var tag = (e.target && e.target.tagName) || "";
  if (((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") || (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(tag))) { var s = $("#search"); if (s) { e.preventDefault(); s.focus(); } }
});

/* ---------- compare selection ---------- */
function setCompare(list) { KD.compare = list.filter(function (o, i) { return list.indexOf(o) === i; }).slice(0, 4); store("kd-compare", KD.compare); renderTray(); syncPicks(); }
function toggleCompare(org) { var on = KD.compare.indexOf(org) >= 0; setCompare(on ? KD.compare.filter(function (o) { return o !== org; }) : KD.compare.concat([org])); }
function pickButton(org, name) { var on = KD.compare.indexOf(org) >= 0; return '<button type="button" class="btn small compare-toggle" data-pick="' + esc(org) + '" aria-pressed="' + on + '" aria-label="Compare ' + esc(name) + '">' + (on ? "In compare" : "Compare") + '</button>'; }
function syncPicks() {
  $$("button[data-pick]").forEach(function (b) { var on = KD.compare.indexOf(b.getAttribute("data-pick")) >= 0; b.setAttribute("aria-pressed", String(on)); b.textContent = on ? "In compare" : "Compare"; b.disabled = !on && KD.compare.length >= 4; });
  var nav = $('nav.main a[data-route=compare]'); if (nav) { nav.textContent = "Compare" + (KD.compare.length ? " (" + KD.compare.length + ")" : ""); nav.href = compareHref(KD.compare); }
}
function renderTray() {
  var t = $("#tray"); if (!t) return;
  if (!KD.compare.length || KD.route.name === "compare") { t.classList.remove("show"); t.innerHTML = ""; return; }
  t.innerHTML = '<span class="small">' + KD.compare.length + ' selected</span><a class="btn primary" href="' + esc(compareHref(KD.compare)) + '">Compare now</a><button type="button" class="btn" id="tray-clear">Clear</button>';
  t.classList.add("show");
  $("#tray-clear").onclick = function () { setCompare([]); };
}

/* ---------- directory: one search box, sort, filters, result rows ---------- */
function rowMatchesText(r, t) {
  if (!t) return true;
  var compact = t.replace(/\s+/g, "");
  if (/^\d{3,9}$/.test(compact)) return String(r.o).indexOf(compact) === 0;
  var hay = [r.n, r.b, r.o, r.m, r.il, r.ie, r.i, r.ceo, r.chair, r.w].join(" ").toLowerCase();
  return t.split(/\s+/).every(function (w) { return hay.indexOf(w) >= 0; });
}
function filterState() {
  var o = {text: ($("#search") ? $("#search").value : "").trim().toLowerCase(), sort: $("#sort") ? $("#sort").value : "cov", full: $("#complete-only") && $("#complete-only").getAttribute("aria-pressed") === "true"};
  ["muni", "div", "rmin", "rmax", "emin", "emax"].forEach(function (k) { var el = $("#f-" + k); o[k] = el ? el.value : ""; });
  $$("[data-f]").forEach(function (i) { o[i.getAttribute("data-f")] = i.checked; });
  return o;
}
function applyFilters() {
  var o = filterState(), rmin = o.rmin === "" ? null : +o.rmin * 1e6, rmax = o.rmax === "" ? null : +o.rmax * 1e6, emin = o.emin === "" ? null : +o.emin, emax = o.emax === "" ? null : +o.emax;
  var rows = KD.rows.filter(function (r) {
    if (!rowMatchesText(r, o.text)) return false;
    if (o.full && r.cv !== 5) return false;
    if (o.muni && r.m !== o.muni) return false; if (o.div && r.dv !== o.div) return false;
    if (o.web && !r.w) return false; if (o.social && !(r.so && r.so.length)) return false; if (o.hire && !r.j) return false; if (o.news && !r.nw) return false; if (o.chg && !r.ch) return false;
    if (rmin != null && !(r.r != null && r.r >= rmin)) return false; if (rmax != null && !(r.r != null && r.r <= rmax)) return false;
    if (emin != null && !(r.e != null && r.e >= emin)) return false; if (emax != null && !(r.e != null && r.e <= emax)) return false;
    return true;
  });
  var cmp = {name: function (a, b) { return a.n.localeCompare(b.n, "nb"); }, rev: function (a, b) { return (b.r == null ? -Infinity : b.r) - (a.r == null ? -Infinity : a.r); }, emp: function (a, b) { return (b.e == null ? -1 : b.e) - (a.e == null ? -1 : a.e); }, recent: function (a, b) { return String(b.ld || "").localeCompare(String(a.ld || "")); }, cov: function (a, b) { return (b.cv || 0) - (a.cv || 0) || a.n.localeCompare(b.n, "nb"); }}[o.sort] || function () { return 0; };
  KD.filtered = rows.sort(cmp);
  var app = $(".app"); if (app) app.classList.toggle("searching", !!o.text && KD.route.name !== "research");
  drawList();
}
function rowHtml(r) {
  var meta = [r.o, r.m ? title(r.m) : null, r.ie || r.il || "Industry not reported"].filter(Boolean).join(" · ");
  var active = KD.route.org === r.o;
  return '<li class="company-row' + (active ? " active" : "") + '"><a class="company-link" href="' + esc(companyHref(r.o)) + '" aria-label="' + esc(r.n + ", organisation number " + r.o) + '"' + (active ? ' aria-current="page"' : "") + '><span class="company-name">' + esc(r.n) + (r.b ? ' <span class="muted">(' + esc(r.b) + ')</span>' : "") + '</span><span class="company-meta">' + esc(meta) + '</span>' + (r.j ? '<span class="badge ok">Hiring: ' + r.j + ' job ad' + (r.j > 1 ? "s" : "") + '</span>' : "") + '</a><div class="row-side"><span class="coverage-pill' + ((r.cv || 0) < 4 ? " mid" : "") + '" aria-label="' + (r.cv || 0) + ' of 5 data areas">' + (r.cv == null ? "?" : r.cv) + '/5 data</span>' + pickButton(r.o, r.n) + '</div></li>';
}
function drawList() {
  var l = $("#company-list"); if (!l) return;
  var rows = KD.filtered;
  l.innerHTML = rows.length ? rows.slice(0, KD.shown).map(rowHtml).join("") : '<li class="empty">No matching company. Search by name, organisation number, town or industry.</li>';
  $("#result-count").textContent = rows.length + (rows.length === 1 ? " company" : " companies");
  var more = $("#more"); if (more) more.style.display = rows.length > KD.shown ? "" : "none";
  syncPicks();
}
function directoryHtml() {
  var munis = {}, divs = {};
  KD.rows.forEach(function (r) { if (r.m) munis[r.m] = (munis[r.m] || 0) + 1; if (r.dv) { divs[r.dv] = divs[r.dv] || {n: 0, l: r.ie || r.il}; divs[r.dv].n++; } });
  var tog = function (k, l) { return '<label class="toggle"><input type="checkbox" data-f="' + k + '"> ' + l + '</label>'; };
  return '<aside class="card directory" aria-labelledby="dir-title"><div class="overline">Company directory</div><div class="dir-head"><h2 id="dir-title">Companies</h2><span id="result-count" aria-live="polite"></span></div>' +
    '<p class="note">Profiles with the most verified data appear first. Use the search box above to find a company by name or organisation number.</p>' +
    '<div class="sort-row"><select id="sort" aria-label="Sort companies"><option value="cov">Most data found</option><option value="name">Name A–Z</option><option value="rev">Revenue (high first)</option><option value="emp">Employees (high first)</option><option value="recent">Latest dated event</option></select><button type="button" class="btn" id="complete-only" aria-pressed="false">All five areas</button></div>' +
    '<details class="more"><summary>More filters</summary><label class="field" for="f-muni">Municipality</label><select id="f-muni"><option value="">Any</option>' + Object.keys(munis).sort().map(function (m) { return '<option value="' + esc(m) + '">' + esc(title(m)) + ' (' + munis[m] + ')</option>'; }).join("") + '</select>' +
    '<label class="field" for="f-div">Industry</label><select id="f-div"><option value="">Any</option>' + Object.keys(divs).sort().map(function (k) { return '<option value="' + esc(k) + '">' + esc(k + " " + String(divs[k].l || "").slice(0, 44)) + ' (' + divs[k].n + ')</option>'; }).join("") + '</select>' +
    '<div style="margin-top:6px">' + tog("web", "Verified website") + tog("social", "Social profile") + tog("hire", "Hiring (open job ad)") + tog("news", "Dated news") + tog("chg", "Changed this run") + '</div>' +
    '<label class="field" for="f-rmin">Revenue, NOK million (latest year)</label><div class="pair"><input id="f-rmin" type="number" inputmode="decimal" placeholder="min" aria-label="Minimum revenue, NOK million"><input id="f-rmax" type="number" inputmode="decimal" placeholder="max" aria-label="Maximum revenue, NOK million"></div>' +
    '<label class="field" for="f-emin">Registered employees</label><div class="pair"><input id="f-emin" type="number" inputmode="numeric" placeholder="min" aria-label="Minimum employees"><input id="f-emax" type="number" inputmode="numeric" placeholder="max" aria-label="Maximum employees"></div>' +
    '<div class="row" style="margin:12px 0"><button type="button" class="btn" id="reset">Reset</button><button type="button" class="btn" id="csv">Download CSV</button></div></details>' +
    '<ul id="company-list" class="company-list" aria-label="Company search results"></ul><div style="text-align:center;margin-top:10px"><button type="button" class="btn" id="more">Show more</button></div></aside>';
}
function bindDirectory() {
  var s = $("#search"), f = $("#search-form");
  if (s && !KD.searchBound) s.addEventListener("input", function () { KD.shown = 50; applyFilters(); });
  if (f && !KD.searchBound) f.addEventListener("submit", function (e) {
    e.preventDefault(); KD.shown = 50; applyFilters();
    var q = s.value.trim(), compact = q.replace(/\s+/g, "");
    var exact = KD.byOrg[compact] || (KD.filtered.length === 1 ? KD.filtered[0] : null);
    if (exact && q) { location.href = companyHref(exact.o); return; }
    var first = $("#company-list a.company-link"); if (first) first.focus();
  });
  KD.searchBound = true;
  $$("#sort,#f-muni,#f-div,[data-f]").forEach(function (i) { i.addEventListener("change", function () { KD.shown = 50; applyFilters(); }); });
  $$("#f-rmin,#f-rmax,#f-emin,#f-emax").forEach(function (i) { i.addEventListener("input", function () { KD.shown = 50; applyFilters(); }); });
  $("#complete-only").onclick = function () { var b = $("#complete-only"), on = b.getAttribute("aria-pressed") !== "true"; b.setAttribute("aria-pressed", String(on)); b.textContent = on ? "Show every profile" : "All five areas"; applyFilters(); };
  $("#more").onclick = function () { KD.shown += 50; drawList(); };
  $("#reset").onclick = function () { $$("[data-f]").forEach(function (i) { i.checked = false; }); $$("#f-muni,#f-div,#f-rmin,#f-rmax,#f-emin,#f-emax").forEach(function (i) { i.value = ""; }); if (s) s.value = ""; applyFilters(); };
  $("#csv").onclick = function () {
    var cols = [["o", "organisation_number"], ["n", "legal_name"], ["m", "municipality"], ["i", "industry_code"], ["ie", "industry"], ["f", "legal_form"], ["e", "registered_employees"], ["y", "latest_fiscal_year"], ["r", "revenue_nok"], ["nr", "net_result_nok"], ["eq", "equity_nok"], ["w", "website"], ["j", "job_ads"], ["nw", "news_items"], ["cv", "data_areas_of_5"], ["ch", "changes_this_run"]];
    var csv = [cols.map(function (c) { return c[1]; }).join(",")].concat(KD.filtered.map(function (r) { return cols.map(function (c) { var v = r[c[0]]; v = v == null ? "" : String(v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }).join(","); })).join("\n");
    var a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], {type: "text/csv"})); a.download = "kildespor-companies.csv"; document.body.appendChild(a); a.click(); a.remove();
  };
}

/* ---------- questions over the run's evidence (same grammar as kildespor/ask.py) ---------- */
var UNSUPPORTED = {sentiment: "sentiment is not measured", review: "reviews and ratings are not collected", rating: "reviews and ratings are not collected", glassdoor: "Glassdoor is not a permitted source", salary: "salaries are not collected", traffic: "website traffic is not collected", linkedin: "LinkedIn data is used only where a verified company site links to it", stock: "share prices are not collected", price: "prices are not collected"};
var SECTORS = {construction: ["41", "42", "43"], builder: ["41", "42", "43"], restaurant: ["56"], food: ["10", "56"], "real estate": ["68"], property: ["68"], software: ["58", "62", "63"], it: ["62", "63"], tech: ["58", "62", "63"], retail: ["47"], shop: ["47"], wholesale: ["46"], transport: ["49", "50", "51", "52", "53"], shipping: ["50", "52"], health: ["86", "87", "88"], consult: ["70", "71", "74"], holding: ["64"], finance: ["64", "65", "66"], accounting: ["69"], legal: ["69"], hotel: ["55"], farming: ["01"], fishing: ["03"], aquaculture: ["03"], energy: ["35"], manufactur: ["10", "11", "13", "14", "15", "16", "17", "18", "20", "22", "23", "24", "25", "26", "27", "28", "29", "30", "31", "32", "33"], education: ["85"], cleaning: ["81"], media: ["58", "59", "60"], research: ["72"], marketing: ["73"], car: ["45"], housing: ["97", "98", "68"]};
function num(s, unit) { var v = parseFloat(String(s).replace(/\s/g, "").replace(",", ".")); unit = (unit || "").toLowerCase(); if (/^(b|bn|billion|mrd|bnok)/.test(unit)) v *= 1e9; else if (/^(m|mn|mill|million|mnok|mill\.)/.test(unit)) v *= 1e6; else if (/^(k|thousand|tnok)/.test(unit)) v *= 1e3; return v; }
function findCompany(q) { var m = q.match(/\b(\d{3}\s?\d{3}\s?\d{3})\b/); if (m) { var o = m[1].replace(/\s/g, ""); if (KD.byOrg[o]) return KD.byOrg[o]; } var ql = " " + q.toLowerCase().replace(/[^a-z0-9æøåéü&]+/g, " ") + " "; var best = null, score = 0; KD.rows.forEach(function (r) { [r.n, r.b].forEach(function (nm) { if (!nm) return; var core = nm.toLowerCase().replace(/\b(as|asa|ans|da|sa|enk|nuf|iks|ks|ba|sti|fli|brl|esek|bbl|kf|sf)\b/g, "").replace(/[^a-z0-9æøåéü&]+/g, " ").trim(); if (core.length < 3) return; if (ql.indexOf(" " + core + " ") >= 0 && core.length > score) { best = r; score = core.length; } }); }); return best; }
function parseScreen(q) {
  var l = q.toLowerCase(), f = [], un = [];
  Object.keys(UNSUPPORTED).forEach(function (k) { if (l.indexOf(k) >= 0 && un.indexOf(UNSUPPORTED[k]) < 0) un.push(UNSUPPORTED[k]); });
  var munis = {}; KD.rows.forEach(function (r) { if (r.m) munis[r.m.toLowerCase()] = r.m; });
  var mm = l.match(/\b(?:in|from|based in|located in)\s+([a-zæøåéü][a-zæøåéü .'-]*?)(?=\s+(?:with|and|that|having|which|who|where|over|under|above|below|more|less|by)\b|[,?.!]|$)/);
  if (mm && munis[mm[1].trim()]) f.push({k: "m", v: munis[mm[1].trim()], t: "municipality is " + title(mm[1])});
  else Object.keys(munis).forEach(function (n) { if (n.length > 3 && new RegExp("\\b" + n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b").test(l) && !f.some(function (x) { return x.k === "m"; })) f.push({k: "m", v: munis[n], t: "municipality is " + title(n)}); });
  if (/\b(hiring|recruit|job ads?|vacanc|open roles?|jobs)\b/.test(l)) f.push({k: "hire", t: "hiring signal (open job ad)"});
  if (/\b(without|no|lacking|missing)\s+(an?\s+)?(official\s+)?(web\s?site|homepage)/.test(l)) f.push({k: "nw", t: "no verified website found (absence is not proof)"}); else if (/\b(with|has|have|having)\s+(an?\s+)?(official\s+|verified\s+)?(web\s?site|homepage)/.test(l) || /\bwebsites?\b/.test(l) && !/\bno\b/.test(l)) f.push({k: "w", t: "verified website"});
  if (/\b(without|no|lacking|missing|but no)\s+(an?\s+)?social/.test(l)) f.push({k: "nso", t: "no social profile found"}); else if (/\bsocial\b|facebook|instagram|linkedin|youtube|tiktok/.test(l)) f.push({k: "so", t: "social profile"});
  if (/\b(news|press|announce|articles?)\b/.test(l)) f.push({k: "news", t: "dated news on its own channels"});
  var rv = l.match(/(?:revenue|turnover|omsetning|sales)[^0-9<>]*?(over|above|more than|greater than|at least|>=|>|under|below|less than|at most|<=|<)\s*(?:nok\s*)?([\d.,\s]+?)\s*(bn|billion|mrd|m|mn|mill(?:ion)?s?|mnok|k|thousand|tnok|nok)?\b/) || l.match(/(over|above|more than|at least|under|below|less than|at most)\s*(?:nok\s*)?([\d.,]+)\s*(bn|billion|m|mn|million|mnok|k|thousand)?\s*(?:nok\s*)?(?:in\s+)?(?:revenue|turnover|sales)/);
  if (rv) { var op = /over|above|more|greater|least|>/.test(rv[1]) ? ">" : "<", val = num(rv[2], rv[3]); f.push({k: "r", op: op, v: val, t: "revenue " + (op === ">" ? "over" : "under") + " " + money(val)}); }
  var em = l.match(/(over|above|more than|at least|under|below|fewer than|less than|at most)\s*(\d+)\s*(?:registered\s+)?(?:employees|staff|people|workers)/) || l.match(/employees?\s*(>=|<=|>|<)\s*(\d+)/);
  if (em) { var eo = /over|above|more|least|>/.test(em[1]) ? ">" : "<"; f.push({k: "e", op: eo, v: +em[2], t: "employees " + (eo === ">" ? "over" : "under") + " " + em[2]}); }
  if (/\b(loss[- ]making|unprofitable|losses|negative (net )?result)\b/.test(l)) f.push({k: "loss", t: "net loss in the latest filed year"}); else if (/\bprofitable\b|\bprofit\b/.test(l)) f.push({k: "profit", t: "net profit in the latest filed year"});
  if (/negative equity/.test(l)) f.push({k: "negeq", t: "negative equity"});
  if (/\b(changed|changes|what changed|new since|updated)\b/.test(l)) f.push({k: "ch", t: "changed since the previous run"});
  if (/\b(stale|carried forward|not re-?checked)\b/.test(l)) f.push({k: "st", t: "facts carried forward"});
  if (/all five|5\/5|complete profiles?|full coverage/.test(l)) f.push({k: "cv5", t: "data in all five areas"});
  var fm = l.match(/\b(asa|enk|nuf|ans|sole proprietorships?|public limited|cooperatives?|foundations?)\b/); if (fm) { var code = {"sole proprietorship": "ENK", "sole proprietorships": "ENK", "public limited": "ASA", cooperative: "SA", cooperatives: "SA", foundation: "STI", foundations: "STI"}[fm[1]] || fm[1].toUpperCase(); f.push({k: "f", v: code, t: "legal form " + code}); }
  Object.keys(SECTORS).forEach(function (s) { if (new RegExp("\\b" + s).test(l) && !f.some(function (x) { return x.k === "dv"; })) f.push({k: "dv", v: SECTORS[s], t: "industry: " + s}); });
  var ind = l.match(/industry\s+(?:code\s+)?(\d{2}(?:\.\d{1,3})?)/); if (ind) f.push({k: "ic", v: ind[1], t: "industry code starts " + ind[1]});
  var top = l.match(/\btop\s+(\d+)(?:\s+\w+)?\s+by\s+(revenue|employees|growth)/) || l.match(/\b(largest|biggest)\b/); var sort = null; if (top) sort = {k: top[2] === "employees" ? "e" : top[2] === "growth" ? "rg" : "r", n: top[1] && +top[1] || 10};
  return {filters: f, unsupported: un, sort: sort};
}
function rowMatch(r, x) { switch (x.k) { case "m": return r.m === x.v; case "hire": return r.j > 0; case "w": return !!r.w; case "nw": return !r.w; case "so": return r.so && r.so.length > 0; case "nso": return !(r.so && r.so.length); case "news": return r.nw > 0; case "r": return r.r != null && (x.op === ">" ? r.r > x.v : r.r < x.v); case "e": return r.e != null && (x.op === ">" ? r.e > x.v : r.e < x.v); case "loss": return r.nr != null && r.nr < 0; case "profit": return r.nr != null && r.nr > 0; case "negeq": return r.eq != null && r.eq < 0; case "ch": return r.ch > 0; case "st": return r.st > 0; case "cv5": return r.cv === 5; case "f": return r.f === x.v; case "dv": return x.v.indexOf(r.dv) >= 0; case "ic": return String(r.i || "").indexOf(x.v) === 0; } return false; }
var CITE_FOR = {m: "municipality", hire: "hiring", w: "website", so: "social", news: "news", r: "revenue", e: "employees", loss: "result", profit: "result", negeq: "equity", dv: "industry", ic: "industry", f: "name"};
function topicOf(l) {
  if (/\b(brief|summary|overview|useful|tell me about|in short)\b/.test(l)) return "brief";
  if (/\b(ceo|chief executive|daglig leder|general manager|managing director)\b/.test(l)) return "ceo";
  if (/\b(who runs|who leads|who is behind|chair|board|owner|owns|owned|parent|leader|leadership|auditor|accountant|runs)\b/.test(l)) return "who";
  if (/\b(hiring|hire|jobs?|vacanc|recruit|careers?|work there|work here|working here|apply|employees|staff|join)\b/.test(l)) return "hiring";
  if (/\b(publish|published|recent|recently|activity|news|posts?|online|website|web site|homepage|social|facebook|instagram)\b/.test(l)) return "activity";
  if (/\b(revenue|turnover|profit|loss|result|equity|financ|accounts|filing|doing|money|earn|size|big|perform)\b/.test(l)) return "money";
  if (/\b(changed?|changes|new|latest|happen|refresh)\b/.test(l)) return "changed";
  if (/\b(unknown|missing|not found|gaps?|could not)\b/.test(l)) return "unknown";
  if (/\b(what does|what do|do they|business|industry|about|what is|who is|describe)\b/.test(l)) return "what";
  return null;
}
function firstSource(d, list) { for (var i = 0; i < (list || []).length; i++) { var s = list[i]; for (var j = 0; j < (s.e || []).length; j++) { var e = evOf(d, s.e[j]); if (e && safeUrl(e.u)) return e.u; } } return null; }
function answerCompany(r, q, cb) {
  var l = q.toLowerCase();
  loadCompany(r.o, function (d) {
    if (!d) return cb('<div class="unsupported">The profile of ' + esc(r.n) + ' could not be loaded.</div>');
    for (var u in UNSUPPORTED) if (l.indexOf(u) >= 0) return cb('<div class="unsupported"><h3>Not enough evidence</h3><p>' + esc(UNSUPPORTED[u]) + '. The run covers registry identity, accounts, roles, workplaces, the verified website, social profiles, single job ads and dated news.</p></div>');
    var sec = function (k) { var all = d.syn.sections || [], f = function (x) { return all.filter(function (s) { return s.key === x; })[0]; }; return f(k) || (k === "activity" || k === "hiring" ? f("signals") : null); };  /* "signals": briefs written before 24 Oct */
    var topic = topicOf(l), n = {n: 0}, h = "", heading = "", ok = true, used = [];
    if (topic === "brief" || !topic) { heading = "Company brief"; used = d.syn.summary || []; h = sentences(r.o, used, n); }
    else if (topic === "ceo") {
      heading = "Leadership";
      var c = d.cl.filter(function (c) { return c.f === "role" && c.a === "available" && c.v && c.v.role_code === "DAGL"; })[0];
      if (c) { h = "The general manager (daglig leder) is " + esc(c.v.name) + ". " + factChip(r.o, c, 1); used = [{e: c.e}]; }
      else { ok = false; h = "The register lists no general manager for this entity" + (d.fam.leadership ? " (" + esc(stateName(d.fam.leadership.s)) + ": " + esc(d.fam.leadership.r || "") + ")" : "") + "."; }
    } else if (topic === "hiring") {
      heading = "Working here";
      var jobs = d.cl.filter(function (c) { return c.f === "job_posting" && c.a === "available"; });
      var hs = sec("hiring"); used = hs ? hs.s : [];
      h = (hs ? sentences(r.o, hs.s, n) : "");
      if (jobs.length) h += '<ul>' + jobs.slice(0, 6).map(function (c) { n.n++; return '<li>' + link(c.v.url, c.v.title || c.v.url) + (c.v.published ? " (published " + esc(String(c.v.published).slice(0, 10)) + ")" : "") + " " + factChip(r.o, c, n.n) + '</li>'; }).join("") + '</ul>';
      else { ok = false; var fh = d.fam.hiring || {}; h += ' <span class="small">Single job ads: ' + stateBadge(fh.s === "available" ? "not_available" : fh.s || "not_available") + ' ' + esc(fh.r || "") + " " + chip(r.o, ++n.n, [], [], "absence", "Single job ads: " + (fh.r || ""), fh.k || []) + '</span>'; }
    } else if (sec(topic)) { heading = SECTION_TITLES[topic]; used = sec(topic).s; h = sentences(r.o, used, n); }
    else { ok = false; heading = "Not enough evidence"; h = "Nothing in this company's profile answers that."; }
    var src = firstSource(d, used);
    cb('<div' + (ok ? "" : ' class="unsupported"') + '><h3>' + esc(heading) + ' · <a href="' + esc(companyHref(r.o)) + '">' + esc(r.n) + '</a></h3><p>' + h + '</p>' + (src ? '<p><a href="' + esc(src) + '" target="_blank" rel="noopener noreferrer">Open supporting source ↗</a></p>' : "") + '<p class="small muted">Answered from this run\'s evidence only; the numbered buttons open each source.</p></div>');
  });
}
function answer(q, current, cb) {
  q = String(q || "").trim(); if (!q) return cb("");
  var company = findCompany(q), p = parseScreen(q);
  var personal = /\b(who|what does|what do|is it|are they|does it|tell me|ceo|daglig leder|chair|hiring|revenue|website|doing|owner|changed|brief|working|published|recent)\b/i.test(q);
  var screening = p.filters.filter(function (f) { return f.k !== "m" && f.k !== "dv"; }).length > 1 || /\b(companies|which|list|top \d+|largest|biggest)\b/i.test(q);
  if (company && personal && !screening) return answerCompany(company, q, cb);
  if (current && !company && !screening) return answerCompany(current, q, cb);
  if (p.unsupported.length && !p.filters.length) return cb('<div class="unsupported"><h3>Not enough evidence</h3><p>' + esc(p.unsupported.join("; ")) + '.</p></div>');
  if (!p.filters.length && !p.sort) { if (company) return answerCompany(company, q, cb); return cb('<div class="unsupported"><h3>Not enough evidence</h3><p>The checked sources do not support an answer to that. Ask about one company (by name or organisation number): what it does, how it is doing, who runs it, whether it is hiring, what it published recently, what changed and what is unknown. Or screen the run by municipality, industry, legal form, revenue, employees, profit or loss, website, social profiles, hiring, news, coverage and changes.</p></div>'); }
  var res = KD.rows.filter(function (r) { return p.filters.every(function (x) { return rowMatch(r, x); }); });
  if (p.sort) res = res.filter(function (r) { return r[p.sort.k] != null; }).sort(function (a, b) { return b[p.sort.k] - a[p.sort.k]; }).slice(0, p.sort.n);
  var keys = []; p.filters.forEach(function (x) { var k = CITE_FOR[x.k]; if (k && keys.indexOf(k) < 0) keys.push(k); }); if (p.sort) keys.push(p.sort.k === "e" ? "employees" : "revenue");
  var h = '<div><h3>' + res.length + ' of ' + KD.rows.length + ' companies</h3><p>Match: ' + esc(p.filters.map(function (x) { return x.t; }).join("; ") || "all") + (p.sort ? " — sorted by " + ({r: "revenue", e: "employees", rg: "revenue growth"}[p.sort.k]) : "") + '.</p>';
  if (p.unsupported.length) h += '<p class="small">Ignored: ' + esc(p.unsupported.join("; ")) + '.</p>';
  if (p.filters.some(function (x) { return x.k === "nw" || x.k === "nso"; })) h += '<p class="small muted">"No website" means none was verified in this run; it is not proof that none exists.</p>';
  if (p.filters.some(function (x) { return x.k === "ch"; }) && KD.meta.baseline) h += '<p class="small muted">This run has no previous snapshot, so no company can have changed; see Refresh.</p>';
  h += '<ul>' + res.slice(0, 25).map(function (r) { var cites = keys.map(function (k, i) { var c = r.src && r.src[k]; return c ? chip(r.o, i + 1, [c[1]], [c[0]], "fact", k) : ""; }).join(""); var val = [r.m ? title(r.m) : null, r.r != null ? "revenue " + money(r.r) + (r.y ? " (" + r.y + ")" : "") : null, r.e != null ? r.e + " employees" : null, r.j ? r.j + " job ad" + (r.j > 1 ? "s" : "") : null, r.w ? host(r.w) : null, r.so && r.so.length ? r.so.join("/") : null, r.nw ? r.nw + " news" : null, r.ch ? r.ch + " changes" : null].filter(Boolean).join(" · "); return '<li><a href="' + esc(companyHref(r.o)) + '">' + esc(r.n) + '</a> <span class="small muted">' + esc(r.o) + (val ? " · " + esc(val) : "") + '</span> ' + cites + '</li>'; }).join("") + '</ul>' + (res.length > 25 ? '<p class="small muted">Showing the first 25.</p>' : "") + '<p class="small muted">Each number opens the claim and source used for that criterion.</p></div>';
  cb(h);
}
KD.answer = answer; KD.parseScreen = parseScreen;
function agentHtml(r) {
  var ex = KD.rows.filter(function (x) { return x.ceo; })[0] || KD.rows[0];
  var prompts = r ? [["Give me a useful company brief", "Company brief"], ["How is the company doing?", "Latest financials"], ["Who runs the company?", "Who runs it?"], ["Should I consider working here?", "Working here"], ["What has the company published recently?", "Recent activity"], ["What changed?", "What changed"], ["What could not be established?", "Unknowns"]]
    : [["hiring companies", "Hiring companies"], ["revenue over 10 MNOK", "Revenue over 10 MNOK"], ["companies without a website", "No website found"], ["top 5 by revenue", "Top 5 by revenue"], ["what changed", "What changed"]].concat(ex ? [["who runs " + ex.n, "Who runs " + ex.n + "?"], ["is " + ex.n + " hiring", "Is " + ex.n + " hiring?"]] : []);
  return '<aside class="card agent" aria-labelledby="agent-title"><div class="overline">Company research agent</div><h2 id="agent-title" class="agent-title">Ask Signalpost</h2>' +
    '<p class="small">' + (r ? "Ask what " + esc(r.n) + " does, who runs it, where it operates, how the latest filing looks, or whether it is hiring." : "Ask about one company by name or organisation number, or screen this run's " + KD.rows.length + " companies.") + ' Answers come from this run\'s evidence, kildespor\'s deterministic question grammar; no language model.</p>' +
    '<form class="ask-form" id="agent-form"><label for="question" class="sr">Question</label><textarea id="question" aria-label="' + (r ? "Ask about the selected company" : "Ask about the companies in this run") + '" placeholder="' + (r ? "How is this company doing? Is it hiring? Who runs it?" : "e.g. hiring companies in Oslo, or: who runs " + esc(ex ? ex.n : "…")) + '"></textarea><button class="btn primary ask-button" type="submit">Check the evidence</button></form>' +
    '<div class="prompts" role="group" aria-label="Suggested questions">' + prompts.map(function (p) { return '<button type="button" class="prompt" data-question="' + esc(p[0]) + '">' + esc(p[1]) + '</button>'; }).join("") + '</div>' +
    '<div class="answer" id="answer" aria-live="polite"></div><div class="agent-foot">Every answer is limited to the sources in this run. Missing evidence stays missing.</div></aside>';
}
function bindAgent(r) {
  var ask = function (q, keep) { if (!keep) $("#question").value = q; var b = $(".ask-button"); b.disabled = true; b.textContent = "Checking sources…"; answer(q, r, function (h) { $("#answer").innerHTML = h; b.disabled = false; b.textContent = "Check the evidence"; syncPicks(); }); };
  $("#agent-form").onsubmit = function (e) { e.preventDefault(); var q = $("#question").value.trim(); if (q) ask(q, true); };
  $("#question").addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); var q = $("#question").value.trim(); if (q) ask(q, true); } });
  $$(".prompt").forEach(function (b) { b.onclick = function () { ask(b.getAttribute("data-question")); }; });
  var q = KD.route.query.ask; if (q) ask(q); else if (r) { $("#question").value = ""; ask("Give me a useful company brief", true); }
}

/* ---------- route: research ---------- */
function runProof() {
  var m = KD.meta, full = KD.rows.filter(function (r) { return r.cv === 5; }).length, web = KD.rows.filter(function (r) { return r.w; }).length, hire = KD.rows.filter(function (r) { return r.j; }).length, news = KD.rows.filter(function (r) { return r.nw; }).length;
  return '<ul class="quick-grid" aria-label="This run">' + [["Companies in this run", KD.rows.length, "one profile per input"], ["Data in all five areas", full, "of " + KD.rows.length], ["Verified website", web, "companies"], ["Hiring (open job ad)", hire, "companies"], ["Dated news", news, "companies"], ["Run", m.run_id || "not recorded", String(m.completed_at || "").slice(0, 16).replace("T", " ") + " UTC"]].map(function (x) { return '<li class="quick-stat"><span>' + esc(x[0]) + '</span><strong>' + esc(x[1]) + '</strong><small>' + esc(x[2]) + '</small></li>'; }).join("") + '</ul>';
}
function renderResearch() {
  var main = '<section class="card" aria-labelledby="page-title"><h1 id="page-title">Research Norwegian companies</h1><p class="muted">Find a company by name or organisation number in the search box, open its profile, compare companies side by side, and check the source of every fact: its URL, retrieval time and the quoted span it was read from.</p>' + runProof() + '</section>';
  return main;
}

/* ---------- route: company ---------- */
function claimsOf(d, f) { return d.cl.filter(function (c) { return c.f === f && c.a === "available"; }); }
function finRows(d, type) { var rows = {}; d.cl.forEach(function (c) { if (c.f.indexOf("financial.") === 0 && c.a === "available" && c.v && c.v.amount != null && (c.v.account_type || "SELSKAP") === type) { var y = String(c.v.fiscal_year); (rows[y] = rows[y] || {})[c.f.slice(10)] = c; } }); return rows; }
function finTable(org, d, type) {
  var rows = finRows(d, type), years = Object.keys(rows).sort().slice(-4).reverse(); if (!years.length) return "";
  var h = '<h3>' + (type === "KONSERN" ? "Group (consolidated) accounts" : "Company accounts") + '</h3><div class="tablewrap"><table><caption class="sr">' + (type === "KONSERN" ? "Group" : "Company") + ' accounts by year, NOK</caption><thead><tr><th scope="col">NOK</th>' + years.map(function (y) { return '<th class="num" scope="col">' + esc(y) + '</th>'; }).join("") + '</tr></thead><tbody>';
  METRICS.forEach(function (m) { if (!years.some(function (y) { return rows[y][m[0]]; })) return; h += '<tr><th scope="row">' + m[1] + '</th>' + years.map(function (y) { var c = rows[y][m[0]]; return '<td class="num">' + (c ? '<span class="nw">' + esc(money(c.v.amount, c.v.currency)) + '</span> ' + chip(org, "✓", c.e, [c.id], c.st ? "carried_forward" : "fact", m[1] + " " + y + ": " + money(c.v.amount, c.v.currency)) : '<span class="state not_available">Not reported</span>') + '</td>'; }).join("") + '</tr>'; });
  h += '<tr><th scope="row">Equity ratio <span class="small muted">(computed)</span></th>' + years.map(function (y) { var e = rows[y].equity, a = rows[y].total_assets; return '<td class="num">' + (e && a && a.v.amount ? Math.round(100 * e.v.amount / a.v.amount) + "% " + chip(org, "✓", e.e.concat(a.e), [e.id, a.id], "computed", "Equity / total assets, " + y) : '<span class="muted">not computable</span>') + '</td>'; }).join("") + '</tr>';
  h += '<tr><th scope="row">Revenue change on the year before</th>' + years.map(function (y) { var c = rows[y].revenue, p = rows[String(+y - 1)] ? rows[String(+y - 1)].revenue : null; return '<td class="num">' + (c && p && p.v.amount ? pct(c.v.amount / p.v.amount - 1) + " " + chip(org, "✓", c.e.concat(p.e), [c.id, p.id], "computed", "Revenue " + (+y - 1) + " → " + y) : '<span class="muted">no earlier year</span>') + '</td>'; }).join("") + '</tr></tbody></table></div>';
  return h;
}
function famLine(d, f, what) { var x = d.fam[f] || {}; return '<p>' + stateBadge(x.s || "failed") + ' <span class="small muted">' + esc(what ? what + ": " : "") + esc(x.r || "not checked in this run") + '</span></p>'; }
function factList(d, list, render) { return '<ul class="plain">' + list.map(function (c) { return '<li>' + render(c) + (c.st ? '<span class="stale-mark">not re-checked this run</span>' : "") + " " + viewSource(d, c) + '</li>'; }).join("") + '</ul>'; }
function latestFin(d, metric) { var rows = finRows(d, "SELSKAP"), years = Object.keys(rows).sort(); if (!years.length) { rows = finRows(d, "KONSERN"); years = Object.keys(rows).sort(); } for (var i = years.length - 1; i >= 0; i--) if (rows[years[i]][metric]) return rows[years[i]][metric]; return null; }
function quickStat(label, value, small, evidence) { return '<li class="quick-stat"><span>' + esc(label) + '</span><strong>' + value + '</strong><small>' + small + '</small>' + (evidence || "") + '</li>'; }
function renderCompany(org, cb) {
  var r = KD.byOrg[org];
  loadCompany(org, function (d) {
    if (!d) return cb('<section class="card profile" id="profile"><h1>Not in this run</h1><p>No profile for organisation number ' + esc(org) + ' in this run. <span class="state not_available">Not available</span></p><p><a class="btn" href="' + esc(href("research")) + '">Back to research</a></p></section>', null);
    r = r || {o: org, n: org, ca: [], so: []};
    var id = d.id || {}, syn = d.syn || {}, cov = syn.coverage || {}, areas = cov.areas || [], n = {n: 0}, ind = id.industry || {}, form = id.legal_form || {};
    var lastChecked = (d.src || []).map(function (s) { return s.t || ""; }).sort().pop() || "";
    var sec = function (k) { return (syn.sections || []).filter(function (s) { return s.key === k; })[0]; };
    var secText = function (k) { var s = sec(k) || (k === "activity" ? sec("signals") : null); return s ? '<p>' + sentences(org, s.s, {n: 0}) + '</p>' : ""; };
    var jobs = claimsOf(d, "job_posting").sort(function (a, b) { return String(b.v.published || "").localeCompare(String(a.v.published || "")); }), news = claimsOf(d, "company_news").sort(function (a, b) { return String(b.v.published || b.v.date || "").localeCompare(String(a.v.published || a.v.date || "")); }), soc = claimsOf(d, "social_profile"), site = claimsOf(d, "official_website")[0], roles = claimsOf(d, "role"), units = claimsOf(d, "registered_workplace"), groups = claimsOf(d, "group_relationship");
    var rev = latestFin(d, "revenue"), net = latestFin(d, "annual_result"), emp = claimsOf(d, "registered_employees")[0], tl = syn.timeline || [];
    var chips = [[!!rev, "Latest annual filing", "Annual filing not available"], [!!site, "Verified website", "Website not verified"], [soc.length, soc.length + " verified profile" + (soc.length === 1 ? "" : "s"), "No verified profiles"], [news.length, news.length + " dated news item" + (news.length === 1 ? "" : "s"), "No dated news found"], [jobs.length, jobs.length + " open role" + (jobs.length === 1 ? "" : "s") + " found", "No open roles found"]];
    var h = '<article class="card profile" id="profile" aria-labelledby="company-title">' +
      '<header class="profile-hero"><div style="min-width:0"><div class="overline">' + esc(r.ie || ind.description || "Norwegian company") + '</div><h1 id="company-title">' + esc(r.n) + '</h1>' +
      '<div class="company-subtitle">Organisation number <span class="mono">' + esc(org) + '</span> · ' + esc(form.code ? (form.description || form.code) + " (" + form.code + ")" : "Legal form not reported") + ' · ' + esc(id.municipality ? title(id.municipality) : "Location not reported") + (r.b ? ' · trades as ' + esc(r.b) : "") + '</div>' +
      '<p class="profile-deck">' + sentences(org, (syn.summary || []).slice(0, 2), n) + '</p>' +
      '<ul class="evidence-chips" aria-label="What Kildespor found">' + chips.map(function (c) { return '<li class="' + (c[0] ? "" : "muted") + '">' + esc(c[0] ? c[1] : c[2]) + '</li>'; }).join("") + '</ul>' +
      '<div class="profile-freshness">Last checked ' + esc(lastChecked.slice(0, 16).replace("T", " ")) + ' UTC · run ' + esc((d.run || {}).run_id || "") + ' · open each section to inspect its source</div>' +
      '<div class="row" style="margin-top:10px">' + pickButton(org, r.n) + '<button type="button" class="btn" id="download-json">Download JSON</button></div></div>' +
      '<div class="coverage-box" aria-label="Data areas found"><div class="coverage-score">' + (cov.score == null ? "?" : cov.score) + ' <small>of ' + (cov.total || 5) + ' data areas</small></div><div class="dots" aria-hidden="true">' + [0, 1, 2, 3, 4].map(function (i) { return '<span class="dot' + (areas[i] && areas[i].available ? " on" : "") + '"></span>'; }).join("") + '</div><ul>' + areas.map(function (a) { return '<li><span>' + esc(a.label) + '</span>' + stateBadge(a.available ? "available" : "not_available") + '</li>'; }).join("") + '</ul></div></header>' +
      '<nav class="section-nav" aria-label="Company profile sections">' + [["brief", "Brief"], ["overview", "Overview"], ["financials", "Financials"], ["leadership", "Leadership"], ["locations", "Locations"], ["hiring", "Working here"], ["activity", "Recent activity"], ["changes", "What changed"], ["unknown", "Not established"], ["evidence", "Check the evidence"], ["coverage", "Sources found"]].map(function (x) { return '<a href="#' + x[0] + '" data-jump="' + x[0] + '">' + x[1] + '</a>'; }).join("") + '</nav>' +
      '<ul class="quick-grid" aria-label="Key figures">' +
        quickStat("Latest reported revenue", rev ? esc(money(rev.v.amount, rev.v.currency)) : stateBadge("not_available"), rev ? esc(rev.v.fiscal_year) + (rev.v.account_type === "KONSERN" ? " group" : "") + " annual accounts" : esc((d.fam.annual_accounts || {}).r || "no filed accounts"), rev ? viewSource(d, rev, "latest revenue") : "") +
        quickStat("Latest net result", net ? esc(money(net.v.amount, net.v.currency)) : stateBadge("not_available"), net ? esc(net.v.fiscal_year) + " annual accounts" : "not reported", net ? viewSource(d, net, "latest net result") : "") +
        quickStat("Employees in register", emp ? esc(emp.v) : '<span class="state not_available">Not reported</span>', "Entity Register", emp ? viewSource(d, emp, "employees") : "") +
        quickStat("Registered workplaces", units.length ? esc(units.length) : stateBadge((d.fam.workplaces || {}).s || "not_available"), "official subunits", "") +
        quickStat("Open roles found", jobs.length ? esc(jobs.length) : stateBadge((d.fam.hiring || {}).s === "available" ? "not_available" : (d.fam.hiring || {}).s || "not_available"), jobs.length ? "single job ads" : "no single job ad", "") +
        quickStat("Latest dated activity", tl.length ? esc(tl[0].date) : stateBadge("not_available"), tl.length ? esc(String(tl[0].text).slice(0, 60)) : "nothing dated found", "") + '</ul>';
    h += '<section class="section brief" id="brief" aria-labelledby="h-brief"><div class="section-head"><h2 id="h-brief">Company brief</h2><span class="section-kicker">What to know, with sources</span></div><p>' + sentences(org, syn.summary || [], {n: 0}) + '</p>' + (syn.signals && syn.signals.length ? '<div class="badges">' + syn.signals.map(function (s) { return '<span class="badge ' + (s.tone === "positive" ? "ok" : s.tone === "negative" ? "bad" : "info") + '">' + esc(s.text) + '</span>'; }).join("") + '</div>' : "") + '<p class="small muted">' + esc(syn.method || "") + '. Grey buttons are computed figures; amber marks facts carried forward from an earlier run.</p></section>';
    var identity = [["Legal name", claimsOf(d, "legal_name")[0]], ["Legal form", claimsOf(d, "legal_form")[0]], ["Industry", claimsOf(d, "industry_code").sort(function (a, b) { return (a.v.rank || 9) - (b.v.rank || 9); })[0]], ["Business address", claimsOf(d, "registered_business_address")[0]], ["Founded", claimsOf(d, "founded_date")[0]], ["Registry status", claimsOf(d, "registry_status")[0]], ["Public brand", claimsOf(d, "public_brand_name")[0]]];
    h += '<section class="section" id="overview" aria-labelledby="h-overview"><div class="section-head"><h2 id="h-overview">Company overview</h2><span class="section-kicker">What the company does</span></div>' + secText("what") + '<div class="tablewrap"><table class="facts-table"><tbody>' + identity.map(function (x) { var c = x[1]; return '<tr><th scope="row">' + esc(x[0]) + '</th><td>' + (c ? esc(c.f === "industry_code" ? (r.ie ? r.ie + " — " : "") + c.v.description + " (" + c.v.code + ")" : c.f === "legal_form" ? (c.v.description || "") + " (" + c.v.code + ")" : claimText(c)) + " " + viewSource(d, c, x[0]) : '<span class="state not_available">Not available</span>') + '</td></tr>'; }).join("") + '</tbody></table></div></section>';
    h += '<section class="section" id="financials" aria-labelledby="h-fin"><div class="section-head"><h2 id="h-fin">Latest financials</h2><span class="section-kicker">Financial snapshot from the filed annual accounts</span></div>' + secText("money") + (finTable(org, d, "SELSKAP") + finTable(org, d, "KONSERN") || famLine(d, "annual_accounts", "Annual accounts")) + '</section>';
    h += '<section class="section" id="leadership" aria-labelledby="h-who"><div class="section-head"><h2 id="h-who">Who runs it?</h2><span class="section-kicker">Public roles in the Entity Register</span></div>' + secText("who") + (roles.length ? factList(d, roles, function (c) { return '<b>' + esc(c.v.name) + '</b> — ' + esc(c.v.role || "") + (ROLE_EN[c.v.role_code] ? ' <span class="small muted">(' + ROLE_EN[c.v.role_code] + ')</span>' : "") + (c.d ? ' <span class="small muted">since ' + esc(c.d) + '</span>' : ""); }) : famLine(d, "leadership", "Leadership")) +
      (groups.length ? '<h3>Group</h3>' + factList(d, groups, function (c) { return esc(c.v.relationship === "subsidiary" ? "Subsidiary" : c.v.relationship === "parent" ? "Parent" : "Ultimate parent") + ': ' + (KD.byOrg[c.v.organisation_number] ? '<a href="' + esc(companyHref(c.v.organisation_number)) + '">' + esc(c.v.name) + '</a>' : esc(c.v.name)) + ' (' + esc(c.v.organisation_number) + ')' + (c.v.ownership ? " · " + esc(c.v.ownership) : ""); }) : "") + '</section>';
    h += '<section class="section" id="locations" aria-labelledby="h-loc"><div class="section-head"><h2 id="h-loc">Locations</h2><span class="section-kicker">Registered operating presence</span></div>' + (id.business_address ? '<p>Business address: ' + esc(id.business_address) + '</p>' : "") + (units.length ? factList(d, units.slice(0, 40), function (c) { return '<b>' + esc(c.v.name) + '</b> — ' + esc(c.v.address || "address not reported") + (c.v.employees != null ? ' · ' + esc(c.v.employees) + ' employees' : ""); }) + (units.length > 40 ? '<p class="small muted">' + (units.length - 40) + ' more in Check the evidence.</p>' : "") : famLine(d, "workplaces", "Registered workplaces")) + '</section>';
    h += '<section class="section" id="hiring" aria-labelledby="h-hire"><div class="section-head"><h2 id="h-hire">Working here</h2><span class="section-kicker">Open job ads and the people the register counts</span></div>' + secText("hiring") +
      (jobs.length ? '<h3>Open job ads (' + jobs.length + ')</h3>' + factList(d, jobs, function (c) { return '<b>' + link(c.v.url, c.v.title || c.v.url) + '</b><div class="small muted">' + esc([c.v.location ? title(c.v.location) : null, c.v.published ? "published " + String(c.v.published).slice(0, 10) : null, (c.v.application_due || c.v.expires) ? "apply by " + String(c.v.application_due || c.v.expires).slice(0, 10) : null, c.v.board || (c.v.url ? host(c.v.url) : null)].filter(Boolean).join(" · ")) + '</div>'; }) : '<h3>Open job ads</h3>' + famLine(d, "hiring", "Single job ads")) + '</section>';
    h += '<section class="section" id="activity" aria-labelledby="h-act"><div class="section-head"><h2 id="h-act">Recent activity</h2><span class="section-kicker">Dated news on its own channels, verified website and profiles</span></div>' + secText("activity") +
      '<h3>Dated news</h3>' + (news.length ? factList(d, news.slice(0, 15), function (c) { return '<span class="mono">' + esc(String(c.v.published || c.v.date || "").slice(0, 10)) + '</span> ' + link(c.v.url, c.v.title); }) : (/^(failed|blocked|ambiguous)$/.test((d.fam.public_activity || {}).s) ? famLine(d, "public_activity", "Dated news") : '<p>' + stateBadge("not_available") + ' <span class="small muted">no dated news item was found on the company\'s own channels</span></p>')) +
      '<h3>Official website</h3>' + (site ? factList(d, [site], function (c) { return link(c.v.url || c.v) + '<div class="small muted">' + esc(String(c.v.identity_method || "").replace(/_/g, " ")) + '</div>'; }) : famLine(d, "official_website", "Official website")) +
      '<h3>Social profiles</h3>' + (soc.length ? factList(d, soc, function (c) { var u = c.v.url || c.v; return esc(title(c.v.platform || host(u))) + ': ' + link(u); }) : famLine(d, "company_profiles", "Social profiles")) + '</section>';
    var rf = d.rf || {};
    var failedNow = Object.keys(d.fam).filter(function (f) { return d.fam[f].s === "failed" || d.fam[f].s === "blocked"; });
    var stale = d.cl.filter(function (c) { return c.st; });
    h += '<section class="section" id="changes" aria-labelledby="h-chg"><div class="section-head"><h2 id="h-chg">What changed</h2><span class="section-kicker">Refresh status, changes since the previous run, dated events on record</span></div>' +
      '<div class="refresh-status" role="status"><b>Refresh status:</b> ' + (rf.previous_run_id ? "compared with run " + esc(rf.previous_run_id) + ": " + (d.ch.length ? d.ch.length + " change" + (d.ch.length > 1 ? "s" : "") + " found" : "no published fact changed") : "first run for this company (baseline); the next run reports changes against it") + '. ' + (stale.length ? stale.length + " fact" + (stale.length > 1 ? "s" : "") + " carried forward, not re-checked. " : "") + (failedNow.length ? "Not fully checked this run: " + failedNow.map(function (f) { return FAMILY[f] + " (" + stateName(d.fam[f].s) + ")"; }).join(", ") + "." : "Every source family was checked this run.") + '</div>' +
      secText("changed") + (d.ch.length ? '<h3>Changes since the previous run</h3>' + d.ch.map(function (c) { return changeCard(org, c); }).join("") : "") +
      (tl.length ? '<h3>Dated events in the sources, newest first</h3><ul class="plain timeline">' + tl.map(function (t) { return '<li><span class="mono">' + esc(t.date) + '</span><span>' + esc(t.text) + " " + chip(org, "✓", t.evidence_ids, t.claim_ids, "fact", t.text) + '</span></li>'; }).join("") + '</ul>' : "") + '</section>';
    var unk = syn.unknowns || [];
    h += '<section class="section" id="unknown" aria-labelledby="h-unk"><div class="section-head"><h2 id="h-unk">What we could not establish</h2><span class="section-kicker">Each missing area, its state and why</span></div>' + (unk.length ? '<div class="tablewrap"><table><thead><tr><th scope="col">Area</th><th scope="col">State</th><th scope="col">Why</th></tr></thead><tbody>' + unk.map(function (u) { return '<tr><td>' + esc(u.label) + '</td><td>' + stateBadge(u.state) + '</td><td class="small">' + esc(u.reason || "") + ((u.checked_sources || []).length ? '<br>Checked: ' + u.checked_sources.map(function (x) { return link(x, host(x)); }).join(", ") : "") + '</td></tr>'; }).join("") + '</tbody></table></div>' : '<p>Every area has at least one evidence-backed fact.</p>') + '<p class="small muted">"Not available" means the sources were checked and held nothing; "failed" and "blocked" mean they could not be checked; "ambiguous" means a candidate was found but not proven to be this company, so it is withheld.</p></section>';
    var byFam = {}; d.cl.forEach(function (c) { (byFam[c.fa] = byFam[c.fa] || []).push(c); });
    h += '<section class="section" id="evidence" aria-labelledby="h-ev"><div class="section-head"><h2 id="h-ev">Check the evidence</h2><span class="section-kicker">Every published fact with its source, retrieval time and quoted span</span></div>' + Object.keys(FAMILY).map(function (f) { var list = (byFam[f] || []).filter(function (c) { return c.a === "available"; }), st = d.fam[f] || {}; return '<details class="more"><summary>' + esc(FAMILY[f]) + ' &nbsp;' + stateBadge(st.s) + ' <span class="small muted">&nbsp;' + list.length + ' fact' + (list.length === 1 ? "" : "s") + '</span></summary>' + (list.length ? '<div class="tablewrap"><table><tbody>' + list.map(function (c) { return '<tr><th scope="row" style="width:34%">' + esc(c.l || c.f) + (c.pf ? '<div class="small muted mono">' + esc(c.pf) + '</div>' : "") + '</th><td>' + esc(claimText(c)) + (c.d ? ' <span class="small muted">' + esc(c.d) + '</span>' : "") + (c.rp ? ' <span class="small muted">' + esc(c.rp) + '</span>' : "") + (c.st ? '<span class="stale-mark">not re-checked this run</span>' : "") + (c.hi ? '<span class="badge">earlier filing</span>' : "") + " " + viewSource(d, c) + '</td></tr>'; }).join("") + '</tbody></table></div>' : '<p class="small">' + stateBadge(st.s) + ' ' + esc(st.r || "") + '</p>') + '</details>'; }).join("") + '</section>';
    h += '<section class="section" id="coverage" aria-labelledby="h-cov"><div class="section-head"><h2 id="h-cov">Sources found</h2><span class="section-kicker">' + (cov.score == null ? "?" : cov.score) + ' of ' + (cov.total || 5) + ' data areas</span></div><ul class="plain">' + areas.map(function (a, i) { return '<li>' + esc(a.label) + ' ' + stateBadge(a.available ? "available" : "not_available") + '</li>'; }).join("") + '</ul><p class="small muted">This count shows areas with at least one evidence-backed fact. It does not mean every fact was found, and it is not a rating of the company.</p></section></article>';
    cb(h, d);
  });
}
function valueText(v) { if (v == null) return "—"; if (typeof v !== "object") return String(v); if (v.amount != null) return money(v.amount, v.currency) + (v.fiscal_year ? " (" + v.fiscal_year + ")" : ""); return v.title || v.name && (v.name + (v.role ? " (" + v.role + ")" : "")) || v.url || v.event || v.description || JSON.stringify(v); }
function changeCard(org, c) { return '<div class="change"><div><span class="badge ' + (c.mat === "high" ? "bad" : c.mat === "medium" ? "warn" : "") + '">' + esc(c.mat || "") + '</span> <b>' + esc(String(c.type || "").replace(/_/g, " ")) + '</b>' + (c.summary ? ' <span class="small muted">' + esc(c.summary) + '</span>' : "") + '</div><div class="side"><div><div class="small muted">Before ' + chip(org, "src", c.pe, c.pc ? [c.pc] : [], "fact", "Previous value") + '</div>' + (c.prev == null ? '<span class="muted">not listed in the previous run</span>' : esc(valueText(c.prev))) + '</div><div><div class="small muted">Now ' + chip(org, "src", c.ce, c.cc ? [c.cc] : [], "fact", "Current value") + '</div>' + (c.cur == null ? '<span class="muted">no longer listed</span>' : esc(valueText(c.cur))) + '</div></div></div>'; }

/* ---------- route: compare ---------- */
function renderCompare(orgs, cb) {
  orgs = orgs.filter(function (o) { return KD.byOrg[o]; }).slice(0, 4);
  var picker = '<form id="addform" class="ask-form" style="grid-template-columns:minmax(0,1fr) auto;margin-top:12px"><label for="add" class="sr">Add a company to the comparison</label><input id="add" list="addlist" type="search" placeholder="Add a company by name or organisation number"><datalist id="addlist">' + KD.rows.slice(0, 2000).map(function (r) { return '<option value="' + esc(r.o) + '">' + esc(r.n) + '</option>'; }).join("") + '</datalist><button class="btn" type="submit">Add</button></form>';
  if (orgs.length < 2) return cb('<section class="card" aria-labelledby="page-title"><h1 id="page-title">Compare companies</h1><p>Select two to four companies with their <b>Compare</b> button in the directory, or add them here. ' + (orgs.length ? "Selected: " + orgs.map(function (o) { return esc(KD.byOrg[o].n); }).join(", ") + "." : "") + '</p>' + picker + '</section>', orgs);
  loadMany(orgs, function (all) {
    var ds = orgs.map(function (o) { return all[o]; }).filter(Boolean);
    var yearsOf = function (d) { var s = {}; d.cl.forEach(function (c) { if (c.f.indexOf("financial.") === 0 && c.v && (c.v.account_type || "SELSKAP") === "SELSKAP") s[c.v.fiscal_year] = 1; }); return Object.keys(s).sort(); };
    var common = ds.map(yearsOf).reduce(function (a, b) { return a.filter(function (y) { return b.indexOf(y) >= 0; }); }); var Y = common.length ? common[common.length - 1] : null;
    var fin = function (d, m, y) { return d.cl.filter(function (c) { return c.f === "financial." + m && c.a === "available" && c.v && (c.v.account_type || "SELSKAP") === "SELSKAP" && String(c.v.fiscal_year) === String(y); })[0]; };
    var latestY = function (d) { var y = yearsOf(d); return y[y.length - 1]; };
    var na = function (d, f) { var x = d.fam[f] || {}; return stateBadge(x.s === "available" ? "not_available" : x.s || "not_available"); };
    var cell = function (d, c, text, fam) { return c ? esc(text != null ? text : claimText(c)) + (c.st ? '<span class="stale-mark">not re-checked</span>' : "") + " " + factChip(d.org, c, "✓") : (fam ? na(d, fam) : '<span class="state not_available">Not available</span>'); };
    var one = function (d, f, filter) { return d.cl.filter(function (c) { return c.f === f && c.a === "available" && (!filter || filter(c)); })[0]; };
    var rows = [
      ["Industry", function (d) { var c = one(d, "industry_code", function (c) { return c.v.rank === 1; }) || one(d, "industry_code"); return cell(d, c, c ? ((KD.byOrg[d.org] || {}).ie || c.v.description) + " (" + c.v.code + ")" : null, "legal_identity"); }],
      ["Municipality", function (d) { var c = one(d, "municipality"); return cell(d, c, c ? title(c.v) : null, "legal_identity"); }],
      ["Legal form", function (d) { var c = one(d, "legal_form"); return cell(d, c, c ? c.v.code : null, "legal_identity"); }],
      ["Founded", function (d) { return cell(d, one(d, "founded_date"), null, "legal_identity"); }],
      ["Employees (register)", function (d) { return cell(d, one(d, "registered_employees"), null, "legal_identity"); }]];
    METRICS.forEach(function (m) { rows.push([m[1] + " " + (Y || "(latest)"), function (d) { var y = Y || latestY(d), c = fin(d, m[0], y); return c ? cell(d, c, money(c.v.amount, c.v.currency) + (Y ? "" : " (" + y + ")")) : (y ? '<span class="state not_available">Not reported</span>' : na(d, "annual_accounts")); }]); });
    rows.push(["Revenue change on previous year", function (d) { var y = Y || latestY(d), c = fin(d, "revenue", y), p = fin(d, "revenue", String(+y - 1)); return c && p && p.v.amount ? pct(c.v.amount / p.v.amount - 1) + " " + chip(d.org, "✓", c.e.concat(p.e), [c.id, p.id], "computed", "Revenue " + (+y - 1) + " → " + y) : '<span class="muted">no comparable year</span>'; }]);
    rows.push(["Equity ratio (computed)", function (d) { var y = Y || latestY(d), e = fin(d, "equity", y), a = fin(d, "total_assets", y); return e && a && a.v.amount ? Math.round(100 * e.v.amount / a.v.amount) + "% " + chip(d.org, "✓", e.e.concat(a.e), [e.id, a.id], "computed", "Equity / total assets " + y) : '<span class="muted">not computable</span>'; }]);
    rows.push(["General manager", function (d) { var c = one(d, "role", function (c) { return c.v.role_code === "DAGL"; }); return cell(d, c, c ? c.v.name : null, "leadership"); }]);
    rows.push(["Board chair", function (d) { var c = one(d, "role", function (c) { return c.v.role_code === "LEDE"; }); return cell(d, c, c ? c.v.name : null, "leadership"); }]);
    rows.push(["Parent company", function (d) { var c = one(d, "group_relationship", function (c) { return c.v.relationship === "parent" || c.v.relationship === "ultimate_parent"; }); return c ? cell(d, c, c.v.name) : '<span class="muted">none registered</span>'; }]);
    rows.push(["Website", function (d) { var c = one(d, "official_website"); return c ? link(c.v.url || c.v, host(c.v.url || c.v)) + " " + factChip(d.org, c, "✓") : na(d, "official_website"); }]);
    rows.push(["Social profiles", function (d) { var l = d.cl.filter(function (c) { return c.f === "social_profile" && c.a === "available"; }); return l.length ? l.map(function (c) { return link(c.v.url || c.v, title(c.v.platform || "profile")) + " " + factChip(d.org, c, "✓"); }).join("<br>") : na(d, "company_profiles"); }]);
    rows.push(["Open job ads", function (d) { var j = d.cl.filter(function (c) { return c.f === "job_posting" && c.a === "available"; }); return j.length ? j.length + " job ad" + (j.length > 1 ? "s" : "") + " " + factChip(d.org, j[0], "✓") : na(d, "hiring"); }]);
    rows.push(["Latest news", function (d) { var l = d.cl.filter(function (c) { return c.f === "company_news" && c.a === "available"; }).sort(function (a, b) { return String(b.v.published || b.v.date || "").localeCompare(String(a.v.published || a.v.date || "")); }); return l.length ? esc(String(l[0].v.published || l[0].v.date || "").slice(0, 10)) + " " + link(l[0].v.url, l[0].v.title) + " " + factChip(d.org, l[0], "✓") : na(d, "public_activity"); }]);
    rows.push(["Data areas", function (d) { var r = KD.byOrg[d.org] || {}; return esc((r.cv == null ? "?" : r.cv) + " of 5"); }]);
    rows.push(["Changes this run", function (d) { return d.ch.length ? d.ch.length + " — " + esc(d.ch.slice(0, 2).map(function (c) { return String(c.type).replace(/_/g, " "); }).join(", ")) : '<span class="muted">none</span>'; }]);
    var name = function (d) { return (KD.byOrg[d.org] || {n: d.org}).n; };
    var h = '<section class="card" aria-labelledby="page-title"><h1 id="page-title">Compare companies</h1><p class="small muted">' + (Y ? "Figures for financial year " + esc(Y) + ", the latest year filed by every company shown." : "These companies have no filed year in common; each column shows its own latest year.") + ' Every value has a button that opens its source.</p>' +
      '<div class="row">' + ds.map(function (d) { return '<a class="btn" href="' + esc(companyHref(d.org)) + '">' + esc(name(d)) + '</a><button type="button" class="btn" data-remove="' + esc(d.org) + '" aria-label="Remove ' + esc(name(d)) + ' from the comparison">✕</button>'; }).join("") + '</div>' +
      '<div class="tablewrap" style="margin-top:10px"><table class="compare"><caption class="sr">Side-by-side comparison</caption><thead><tr><th scope="col">&nbsp;</th>' + ds.map(function (d) { return '<th scope="col"><a href="' + esc(companyHref(d.org)) + '">' + esc(name(d)) + '</a><div class="small muted">' + esc(d.org) + '</div></th>'; }).join("") + '</tr></thead><tbody>' +
      '<tr><th scope="row">Company</th>' + ds.map(function (d) { return '<td data-label="' + esc(name(d)) + '"><span><a href="' + esc(companyHref(d.org)) + '">' + esc(name(d)) + '</a> <span class="small muted">' + esc(d.org) + '</span></span></td>'; }).join("") + '</tr>' +
      rows.map(function (row) { return '<tr><th scope="row">' + esc(row[0]) + '</th>' + ds.map(function (d) { return '<td data-label="' + esc(name(d)) + '"><span>' + row[1](d) + '</span></td>'; }).join("") + '</tr>'; }).join("") + '</tbody></table></div>' + (orgs.length < 4 ? picker : "") + '</section>';
    cb(h, orgs);
  });
}
function bindCompare(orgs) {
  $$("[data-remove]").forEach(function (b) { b.onclick = function () { var l = orgs.filter(function (o) { return o !== b.getAttribute("data-remove"); }); setCompare(l); location.href = compareHref(l); }; });
  var f = $("#addform"); if (f) f.onsubmit = function (e) { e.preventDefault(); var v = $("#add").value.trim(), r = KD.byOrg[v.replace(/\s/g, "")] || findCompany(v) || KD.rows.filter(function (x) { return x.n.toLowerCase().indexOf(v.toLowerCase()) >= 0; })[0]; if (!r) return; var l = orgs.filter(function (o) { return o !== r.o; }).concat([r.o]).slice(0, 4); setCompare(l); location.href = compareHref(l); };
}

/* ---------- route: refresh ---------- */
function renderRefresh() {
  var changed = KD.rows.filter(function (r) { return r.ch > 0; }).sort(function (a, b) { return b.ch - a.ch; }), stale = KD.rows.filter(function (r) { return r.st > 0; }), failed = KD.rows.filter(function (r) { return r.fl && r.fl.length; });
  var m = KD.meta, total = changed.reduce(function (s, r) { return s + r.ch; }, 0);
  var h = '<section class="card" aria-labelledby="page-title"><h1 id="page-title">Refresh: what changed in this run</h1>' +
    '<div class="refresh-status" role="status"><b>Refresh status:</b> run ' + esc(m.run_id || "") + ' completed ' + esc(String(m.completed_at || "").slice(0, 16).replace("T", " ")) + ' UTC. ' + (m.baseline ? "No previous snapshot was supplied, so this run is the baseline: every fact is recorded with its first-observed time and the next run reports changes against it." : "Compared with run " + esc((m.previous_runs || []).join(", ")) + ".") + '</div>' +
    '<ul class="quick-grid" aria-label="Refresh totals">' + quickStat("Companies changed", esc(changed.length), "since the previous run", "") + quickStat("Changes", esc(total), "both sides kept with evidence", "") + quickStat("Facts carried forward", esc(stale.length), "companies with facts not re-checked", "") + quickStat("Sources not fully checked", esc(failed.length), "companies with a failed or blocked area", "") + '</ul>' +
    '<p class="small muted">A change keeps both values and both values\' evidence; nothing is overwritten. A fact that could not be re-checked is carried forward and marked, never silently dropped or presented as new. To refresh, run the agent again with the same <span class="mono">--state-dir</span>.</p></section>';
  if (changed.length) h += '<section class="card"><h2>Companies with changes</h2><div class="list">' + changed.map(function (r) { return '<details class="change" data-org="' + esc(r.o) + '"><summary style="min-height:44px;cursor:pointer"><b>' + esc(r.n) + '</b> <span class="small muted">' + esc(r.o) + '</span> <span class="badge warn">' + r.ch + ' change' + (r.ch > 1 ? "s" : "") + '</span><div class="small muted">' + esc((r.cs || []).join("; ")) + '</div></summary><div class="body">Loading…</div></details>'; }).join("") + '</div></section>';
  else if (!m.baseline) h += '<section class="card"><p>No published fact changed since the previous run in the sources that were re-checked.</p></section>';
  if (stale.length) h += '<section class="card"><h2>Facts carried forward (not re-checked this run)</h2><ul class="plain">' + stale.map(function (r) { return '<li><a href="' + esc(companyHref(r.o)) + '">' + esc(r.n) + '</a> — ' + r.st + ' fact' + (r.st > 1 ? "s" : "") + ' kept from the previous run and marked as such</li>'; }).join("") + '</ul></section>';
  if (failed.length) h += '<section class="card"><h2>Sources that could not be fully checked this run</h2><p class="small muted">A failed or blocked area is a gap in this run, not a change in the company.</p><ul class="plain">' + failed.slice(0, 200).map(function (r) { return '<li><a href="' + esc(companyHref(r.o)) + '">' + esc(r.n) + '</a> <span class="small muted">' + esc(r.o) + '</span> — ' + r.fl.map(function (x) { return esc(FAMILY[x[0]] || x[0]) + " " + stateBadge(x[1]); }).join(", ") + '</li>'; }).join("") + '</ul></section>';
  return h;
}
function bindRefresh() {
  $$("details.change[data-org]").forEach(function (det) { det.addEventListener("toggle", function () { if (!det.open || det.getAttribute("data-done")) return; det.setAttribute("data-done", "1"); var org = det.getAttribute("data-org"); loadCompany(org, function (d) { $(".body", det).innerHTML = d ? d.ch.map(function (c) { return changeCard(org, c); }).join("") + '<a class="btn" href="' + esc(companyHref(org)) + '">Open profile</a>' : "Could not load."; }); }); });
}

/* ---------- router ---------- */
function routeOf() {
  var h = location.hash;
  if (/^#\//.test(h)) { var path = h.slice(1).split("?")[0], parts = path.split("/").filter(Boolean); return {name: parts[0] || "research", org: parts[1] || null, query: parseQS(h.split("?")[1] || "")}; }
  return {name: C.route || "research", org: C.org || null, query: parseQS(location.search.slice(1))};
}
function render() {
  var R = KD.route = routeOf();
  $$("nav.main a").forEach(function (a) { if (a.getAttribute("data-route") === (R.name === "companies" ? "research" : R.name)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  var main = $("#main"), r = R.name === "companies" && R.org ? KD.byOrg[R.org] : null;
  var finish = function (inner, after) {
    var company = R.name === "companies";
    var cls = "app route-" + R.name + (company ? "" : " wide");
    main.innerHTML = '<div class="' + cls + '">' + directoryHtml() + (company ? inner + agentHtml(r) : '<div class="page">' + inner + (R.name === "research" ? agentHtml(null) : "") + '</div>') + '</div>';
    bindDirectory();
    if (R.query.q && $("#search") && !$("#search").value) $("#search").value = R.query.q;
    applyFilters();
    if (R.name === "companies" || R.name === "research") bindAgent(r);
    if (after) after();
    renderTray(); syncPicks();
  };
  if (R.name === "companies" && R.org) {
    renderCompany(R.org, function (h, d) {
      finish(h, function () {
        document.title = (r ? r.n : R.org) + " (" + R.org + ") — Kildespor";
        $$("[data-jump]").forEach(function (a) { a.onclick = function (e) { e.preventDefault(); var t = document.getElementById(a.getAttribute("data-jump")); if (t) { t.scrollIntoView({behavior: "smooth", block: "start"}); var hd = t.querySelector("h2"); if (hd) { hd.setAttribute("tabindex", "-1"); hd.focus({preventScroll: true}); } } }; });
        var dl = $("#download-json"); if (dl && d) dl.onclick = function () { var a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 1)], {type: "application/json"})); a.download = R.org + ".json"; document.body.appendChild(a); a.click(); a.remove(); };
        if (location.hash && !/^#\//.test(location.hash)) { var t = document.getElementById(location.hash.slice(1)); if (t) t.scrollIntoView(); }
      });
    });
  } else if (R.name === "compare") {
    var orgs = (R.query.orgs || KD.compare.join(",")).split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    renderCompare(orgs, function (h, list) { finish(h, function () { if (list.length) setCompare(list); bindCompare(list); document.title = "Compare companies — Kildespor"; }); });
  } else if (R.name === "refresh") {
    finish(renderRefresh(), function () { bindRefresh(); document.title = "Refresh — Kildespor"; });
  } else {
    finish(renderResearch(), function () { document.title = "Research Norwegian companies — Kildespor"; });
  }
}
function start() {
  if (KD.indexError || !KD.loaded) { $("#main").innerHTML = '<section class="card"><h1>Data not found</h1><p>The viewer expects <span class="mono">data/index.js</span> in the run\'s <span class="mono">site/</span> folder. <span class="state failed">Failed</span></p></section>'; return; }
  KD.compare = (store("kd-compare") || []).filter(function (o) { return KD.byOrg[o]; });
  var proof = $("#run-proof"); if (proof) { var m = KD.meta, full = KD.rows.filter(function (r) { return r.cv === 5; }).length; proof.innerHTML = '<span><strong>' + KD.rows.length + '</strong> companies in this run</span><span><strong>' + full + '</strong> have data in all five areas</span><span>run <strong>' + esc(m.run_id || "") + '</strong> · ' + esc(String(m.completed_at || "").slice(0, 16).replace("T", " ")) + ' UTC</span>'; }
  window.addEventListener("hashchange", function () { if (/^#\//.test(location.hash)) render(); });
  render();
}
KD.indexFailed = function () { KD.indexError = true; start(); };
KD.onIndex = start;
})();
