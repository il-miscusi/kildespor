"""Build the ATS tenant index (``kildespor/data/ats-tenants.tsv.gz``) on a build machine.

    python3 tools/build_ats.py [--universe P] [--enheter P] [--underenheter P] [--out DIR] \
        [--stats P] [--batches name=path,...] [--dns-minutes N] [--no-nav]

Run it on Kaggle, never inside the agent: it reads the Brreg bulk files (~310 MB), sends a few
hundred thousand DNS queries and reads NAV's vacancy feed in full. Each row ties one universe
company to one applicant-tracking tenant whose public job list the agent can read without a key
(``kildespor/ats.py``), by a hard signal only:

* ``register_domain_cname``: a careers host on the company's own registered domain
  (``karriere.<domain>``, ``jobs.<domain>`` ...) is a DNS alias (CNAME) of the ATS vendor
  (Teamtailor's ``ext.teamtailor.com``, Recruitee's ``secure.recruitee.com``), and the domain is
  the company's Brreg ``hjemmeside``, e-mail domain or a sub-unit's website, held by no other
  universe company. A domain whose every name resolves (a wildcard) is skipped.
* ``nav_ad_employer_orgnr``: an active ad in NAV's vacancy feed names the company (or one of its
  sub-units) by ``employer.orgnr`` and sends applicants to the tenant, and the tenant's own list
  names the company (its display name or an ad's employer equals the NAV employer's or the
  legal name). Without that name check an agency's tenant would be tied to its client.

Every tenant's list is read once here to confirm the endpoint answers and to record its display
name. The agent re-reads the list live at run time and publishes only ads that name the entity;
an index row never publishes anything by itself.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from cachelib import DATA, universe_orgnrs, write_tsv_gz  # noqa: E402
from register import ENHETER_URL, GENERIC_MAIL, UNDERENHETER_URL, download, email_of, host_of, iter_entities, registered_domain  # noqa: E402

from kildespor import ats  # noqa: E402

UA = "KildesporBot/1.0 (+https://github.com/il-miscusi/kildespor; company-research agent; ATS index build)"
NAV = "https://pam-stilling-feed.nav.no"
LABELS = ("karriere", "jobb", "jobs", "career", "careers", "jobber")
VENDORS = {  # CNAME target suffix -> vendor (only teamtailor and recruitee have a list on the custom host)
    "teamtailor.com": "teamtailor", "teamtailor.map.fastly.net": "teamtailor", "recruitee.com": "recruitee",
    "homerun.co": "homerun", "jobylon.com": "jobylon", "webcruiter.no": "webcruiter", "webcruiter.com": "webcruiter",
    "reachmee.com": "reachmee", "hr-manager.net": "hr-manager", "varbi.com": "varbi", "emply.com": "emply", "emply.net": "emply",
    "recman.no": "recman", "easycruit.com": "easycruit", "jobs2web.com": "successfactors", "successfactors.com": "successfactors",
    "myworkdayjobs.com": "workday", "smartrecruiters.com": "smartrecruiters", "greenhouse.io": "greenhouse", "lever.co": "lever",
    "ashbyhq.com": "ashby", "workable.com": "workable", "personio.de": "personio", "personio.com": "personio", "jobbnorge.no": "jobbnorge",
    "talentech.com": "talentech", "breezy.hr": "breezy", "pinpointhq.com": "pinpoint", "join.com": "join", "softgarden.io": "softgarden",
}
LEGAL = {"as", "asa", "ans", "da", "sa", "ba", "nuf", "enk", "ks", "hf", "iks", "sf", "bbl", "fli", "stiftelse", "stiftelsen", "aksjeselskap"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def key(name: Any) -> str:
    """Comparison key for a name: folded, legal form dropped (the agent's ``_employer_key`` rule)."""
    text = str(name or "").lower().replace("&", " og ").replace("æ", "ae").replace("ø", "o").replace("å", "a")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    tokens = re.findall(r"[a-z0-9]+", text)
    while tokens and tokens[-1] in LEGAL:
        tokens.pop()
    return " ".join(tokens)


# -- register ------------------------------------------------------------------------------------


class Company:
    __slots__ = ("orgnr", "name", "names", "employees", "form", "domains")

    def __init__(self, orgnr: str, name: str, employees: int, form: str) -> None:
        self.orgnr, self.name, self.employees, self.form = orgnr, name, employees, form
        self.names: set[str] = {name}
        self.domains: dict[str, str] = {}  # domain -> how the register states it


def load_register(enheter: str, underenheter: str, universe: set[str]) -> tuple[dict[str, Company], dict[str, str]]:
    companies: dict[str, Company] = {}
    owners: dict[str, set[str]] = defaultdict(set)
    sub_parent: dict[str, str] = {}

    def note(domain_host: str | None, org: str, how: str) -> None:
        if not domain_host:
            return
        domain = registered_domain(domain_host)
        if "." not in domain or domain in GENERIC_MAIL:
            return
        owners[domain].add(org)
        if org in companies:
            companies[org].domains.setdefault(domain, how)

    for rec in iter_entities(enheter):
        org = str(rec.get("organisasjonsnummer") or "")
        mail = email_of(rec.get("epostadresse"))
        if org in universe:
            form = str((rec.get("organisasjonsform") or {}).get("kode") or "")
            companies[org] = Company(org, str(rec.get("navn") or ""), int(rec.get("antallAnsatte") or 0), form)
        note(host_of(rec.get("hjemmeside")), org, "hjemmeside")
        note(mail.rsplit("@", 1)[1] if mail else None, org, "e-mail domain")
    for rec in iter_entities(underenheter):
        org = str(rec.get("organisasjonsnummer") or "")
        parent = str(rec.get("overordnetEnhet") or "")
        if not parent:
            continue
        sub_parent[org] = parent
        mail = email_of(rec.get("epostadresse"))
        note(host_of(rec.get("hjemmeside")), parent, "sub-unit website")
        note(mail.rsplit("@", 1)[1] if mail else None, parent, "sub-unit e-mail domain")
        if parent in companies and rec.get("navn"):
            companies[parent].names.add(str(rec["navn"]))
    # A domain two register entities state is no proof of either one's.
    for company in companies.values():
        company.domains = {d: how for d, how in company.domains.items() if len(owners[d]) == 1}
    return companies, sub_parent


# -- DNS ----------------------------------------------------------------------------------------


def make_resolver() -> Any:
    try:
        import dns.resolver  # type: ignore

        res = dns.resolver.Resolver(configure=True)
        res.timeout, res.lifetime = 1.5, 3.0
        return res
    except Exception:
        return None


def cname_of(res: Any, name: str) -> str | None:
    """The first CNAME target of ``name`` (lower case, no trailing dot), or None."""
    if res is not None:
        import dns.exception  # type: ignore
        import dns.resolver  # type: ignore

        try:
            answer = res.resolve(name, "CNAME", raise_on_no_answer=False)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout, dns.resolver.LifetimeTimeout):
            return None
        except Exception:
            return None
        if answer.rrset:
            return str(answer.rrset[0].target).rstrip(".").lower()
        return None
    try:
        info = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP, flags=socket.AI_CANONNAME)
    except OSError:
        return None
    canon = (info[0][3] if info else "") or ""
    return canon.lower().rstrip(".") if canon and canon.lower().rstrip(".") != name else None


def vendor_of(target: str | None) -> str | None:
    if not target:
        return None
    for suffix, vendor in VENDORS.items():
        if target == suffix or target.endswith("." + suffix):
            return vendor
    return None


def probe_dns(companies: dict[str, Company], minutes: float, stats: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    """(orgnr, platform, list_url, rule, evidence) for every careers host aliased to a vendor."""
    res = make_resolver()
    stats["dns_resolver"] = "dnspython" if res is not None else "getaddrinfo"
    # Companies with employees (and ASAs) first: they are the ones that advertise.
    work = sorted(((c.employees <= 0 and c.form != "ASA"), d, c.orgnr) for c in companies.values() for d in c.domains)
    names = [(f"{label}.{domain}", domain, org) for _rest, domain, org in work for label in LABELS]
    stats["dns_domains"] = len(work)
    stats["dns_queries_planned"] = len(names)
    stop = time.time() + minutes * 60
    hits: list[tuple[str, str, str, str]] = []
    vendors = Counter()
    done = 0
    lock = threading.Lock()

    def one(item: tuple[str, str, str]) -> None:
        nonlocal done
        if time.time() > stop:
            return
        name, domain, org = item
        target = cname_of(res, name)
        with lock:
            done += 1
        vendor = vendor_of(target)
        if vendor:
            with lock:
                vendors[vendor] += 1
                hits.append((name, domain, org, target or ""))

    with concurrent.futures.ThreadPoolExecutor(max_workers=256) as pool:
        for i, _ in enumerate(pool.map(one, names, chunksize=64)):
            if i and i % 50000 == 0:
                log(f"dns {i}/{len(names)} queried, {len(hits)} vendor aliases")
    stats["dns_queries_done"] = done
    stats["dns_vendor_aliases"] = dict(vendors.most_common())
    rows = []
    wild: dict[str, bool] = {}
    for name, domain, org, target in sorted(hits):
        if domain not in wild:
            wild[domain] = vendor_of(cname_of(res, f"kildespor-wildcard-probe-7q.{domain}")) is not None
        if wild[domain]:
            continue
        vendor = vendor_of(target)
        host = name
        if vendor == "teamtailor":
            tenant = ats.teamtailor_tenant(host)
        elif vendor == "recruitee":
            tenant = ats.Tenant("recruitee", host, f"https://{host}/api/offers/", f"https://{host}/")
        else:
            continue
        how = companies[org].domains.get(domain, "domain")
        rows.append((org, tenant.platform, tenant.list_url, "register_domain_cname", f"{host} CNAME {target}; {domain} is this company's registered {how}"))
    stats["dns_wildcard_domains"] = sum(wild.values())
    return rows


# -- NAV ------------------------------------------------------------------------------------------


def http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 30.0) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(5_000_000), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:2000] if exc.fp else b"", exc.headers.get("Content-Type", "") if exc.headers else ""
    except Exception:
        return 0, b"", ""


NAV_ACTIVE_ROWS: list[tuple[str, ...]] = []  # (universe orgnr, uuid, employer orgnr, businessName, published, expires, changed)
NAV_PAIRS: Counter = Counter()  # (universe orgnr, businessName) -> active ads naming that orgnr or a subunit
NAV_NAME_ADS: Counter = Counter()  # businessName key -> active ads listed under it


def nav_name_rows(companies: dict[str, Company], built: str, stats: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    """(orgnr, businessName, ads, ads under the name, built) for each name NAV listed an ad of the
    company under that is none of its legal or subunit names. Kept when the company's ads are at
    least half of that name's or the name has at most eight ads, so a chain's shared brand does not
    send every member to open dozens of entries."""
    rows = []
    for (org, name), count in sorted(NAV_PAIRS.items()):
        key = ats.nav_name_key(name)
        if not key or len(key.replace(" ", "")) < 3:
            continue
        own = {ats.nav_name_key(n) for n in companies[org].names}
        if key in own:
            continue
        total = NAV_NAME_ADS.get(key, count)
        if count * 2 < total and total > 8:
            stats["nav_names_dropped_shared"] = stats.get("nav_names_dropped_shared", 0) + 1
            continue
        rows.append((org, " ".join(name.split()), str(count), str(total), built))
    return rows


def read_nav(companies: dict[str, Company], sub_parent: dict[str, str], stats: dict[str, Any], minutes: float, *, days: int = 400) -> list[tuple[str, str, str, str, str, str]]:
    """(orgnr, platform, list_url, rule, evidence, nav employer name) from active NAV ads."""
    status, body, _ = http_get(NAV + "/api/publicToken")
    token = body.decode("utf-8", "replace").split()[-1] if status == 200 and body.split() else None
    if not token:
        stats["nav_error"] = f"no public token (HTTP {status})"
        return []
    auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    since = format_datetime(datetime.now(timezone.utc) - timedelta(days=days), usegmt=True)
    url: str | None = NAV + "/api/v1/feed"
    headers = {**auth, "If-Modified-Since": since}
    active: dict[str, str] = {}
    pages = 0
    stop = time.time() + minutes * 60
    while url and pages < 2000 and time.time() < stop:
        status, body, _ = http_get(url, headers)
        headers = auth
        if status != 200:
            stats["nav_feed_stop"] = f"HTTP {status} after {pages} pages"
            break
        data = json.loads(body)
        pages += 1
        for item in data.get("items") or []:
            entry = item.get("_feed_entry") or {}
            uuid = str(entry.get("uuid") or "")
            if entry.get("status") == "ACTIVE":
                active[uuid] = str(entry.get("businessName") or "")
            else:
                active.pop(uuid, None)
        nxt = data.get("next_url")
        url = NAV + nxt if nxt else None
    stats["nav_pages"] = pages
    stats["nav_active"] = len(active)
    for name in active.values():
        NAV_NAME_ADS[ats.nav_name_key(name)] += 1
    log(f"nav feed: {pages} pages, {len(active)} active ads")
    sources = Counter()
    hosts = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    custom: dict[str, ats.Tenant | None] = {}
    res = make_resolver()
    out = []
    lock = threading.Lock()

    def tenant_of(link: str) -> ats.Tenant | None:
        tenant = ats.tenant_from_url(link)
        if tenant is not None:
            return tenant
        host = (urlsplit(link).hostname or "").lower()
        if not host or registered_domain(host) in ("nav.no", "finn.no", "webcruiter.no", "webcruiter.com", "jobbnorge.no"):
            return None
        if host not in custom:
            vendor = vendor_of(cname_of(res, host))
            custom[host] = ats.teamtailor_tenant(host) if vendor == "teamtailor" else (ats.Tenant("recruitee", host, f"https://{host}/api/offers/", f"https://{host}/") if vendor == "recruitee" else None)
        return custom[host]

    def one(uuid: str) -> None:
        if time.time() > stop:
            return
        for attempt in range(3):
            status, body, _ = http_get(f"{NAV}/api/v1/feedentry/{uuid}", auth)
            if status == 429:
                time.sleep(5 * (attempt + 1))
                continue
            break
        if status != 200:
            return
        try:
            entry = json.loads(body)
        except ValueError:
            return
        ad = entry.get("ad_content") or {}
        employer = ad.get("employer") or {}
        number = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
        parent = sub_parent.get(number, number)
        links = [str(ad.get(k) or "") for k in ("applicationUrl", "sourceurl")]
        with lock:
            sources[str(ad.get("source") or "")] += 1
            for link in links:
                if link.startswith("http"):
                    domain = registered_domain(urlsplit(link).hostname or "")
                    hosts[domain] += 1
                    if vendor_of(domain) and len(examples[domain]) < 6:
                        examples[domain].append(link[:200])
        if parent not in companies:
            return
        with lock:
            NAV_PAIRS[(parent, active.get(uuid, ""))] += 1
            if entry.get("status") == "ACTIVE":
                NAV_ACTIVE_ROWS.append((parent, uuid, number, " ".join(active.get(uuid, "").split()), str(ad.get("published") or "")[:10],
                                        str(ad.get("expires") or "")[:10], str(entry.get("sistEndret") or ad.get("updated") or "")[:19]))
        for link in links:
            tenant = tenant_of(link) if link.startswith("http") else None
            if tenant is not None:
                with lock:
                    out.append((parent, tenant.platform, tenant.list_url, "nav_ad_employer_orgnr", f"NAV ad {uuid} names employer.orgnr {number} and applies via {link[:160]}", str(employer.get("name") or "")))

    todo = sorted(active)
    stats["nav_entries_to_read"] = len(todo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(one, todo))
    stats["nav_sources"] = dict(sources.most_common(30))
    stats["nav_application_hosts"] = hosts.most_common(80)
    stats["nav_ats_examples"] = dict(sorted(examples.items()))
    stats["nav_custom_tenant_hosts"] = sorted(h for h, t in custom.items() if t is not None)[:200]
    return out


# -- confirm each tenant's list -----------------------------------------------------------------


def confirm(rows: list[tuple], stats: dict[str, Any]) -> dict[str, ats.TenantList]:
    lists: dict[str, ats.TenantList] = {}
    lock = threading.Lock()
    by_vendor = Counter()

    def one(list_url: str, platform: str) -> None:
        accept = "application/xml" if platform == "personio" else "application/json"
        status, body, _ctype = http_get(list_url, {"Accept": accept}, timeout=20)
        listing = ats.parse_list(platform, list_url, body.decode("utf-8", "replace")) if status == 200 else ats.TenantList(platform, list_url, error=f"HTTP {status}")
        with lock:
            lists[list_url] = listing
            by_vendor[(platform, listing.error is None)] += 1

    todo = sorted({(r[2], r[1]) for r in rows})
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda t: one(*t), todo))
    stats["lists_read"] = {f"{p}:{'ok' if ok else 'error'}": n for (p, ok), n in sorted(by_vendor.items())}
    return lists


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe")
    ap.add_argument("--enheter")
    ap.add_argument("--underenheter")
    ap.add_argument("--out", default=str(DATA))
    ap.add_argument("--stats")
    ap.add_argument("--batches", default="")
    ap.add_argument("--dns-minutes", type=float, default=80.0)
    ap.add_argument("--nav-minutes", type=float, default=45.0)
    ap.add_argument("--no-nav", action="store_true")
    ap.add_argument("--nav-names-only", action="store_true", help="read NAV only and write ats-nav-names.tsv.gz")
    ap.add_argument("--nav-days", type=int, default=400)
    args = ap.parse_args()
    t0 = time.time()
    built = datetime.now(timezone.utc).date().isoformat()
    stats: dict[str, Any] = {"built_at": built}
    tmp = tempfile.mkdtemp()
    enheter = args.enheter or download(ENHETER_URL, os.path.join(tmp, "enheter.json.gz"))
    underenheter = args.underenheter or download(UNDERENHETER_URL, os.path.join(tmp, "underenheter.json.gz"))
    universe = universe_orgnrs(args.universe)
    companies, sub_parent = load_register(enheter, underenheter, universe)
    stats["universe"] = len(universe)
    stats["companies_with_own_domain"] = sum(1 for c in companies.values() if c.domains)
    log(f"register: {len(companies)} universe companies, {stats['companies_with_own_domain']} with a domain of their own ({time.time() - t0:.0f}s)")

    if args.nav_names_only:
        read_nav(companies, sub_parent, stats, args.nav_minutes, days=args.nav_days)
        rows = nav_name_rows(companies, built, stats)
        out = Path(args.out)
        stats["nav_name_rows"] = write_tsv_gz(out / "ats-nav-names.tsv.gz", ats.NAV_NAMES_COLUMNS, rows)
        active_rows = [r + (built,) for r in NAV_ACTIVE_ROWS]
        stats["nav_active_rows"] = write_tsv_gz(out / "ats-nav-active.tsv.gz", ats.NAV_ACTIVE_COLUMNS, active_rows)
        stats["nav_active_companies"] = len({r[0] for r in active_rows})
        stats["nav_active_size_bytes"] = (out / "ats-nav-active.tsv.gz").stat().st_size
        by_active = defaultdict(list)
        for r in active_rows:
            by_active[r[0]].append(r[1])
        stats["nav_name_companies"] = len({r[0] for r in rows})
        stats["nav_names_size_bytes"] = (out / "ats-nav-names.tsv.gz").stat().st_size
        by_org = defaultdict(list)
        for r in rows:
            by_org[r[0]].append(r[1])
        batch_stats = {}
        for spec in filter(None, args.batches.split(",")):
            name, path = spec.split("=", 1)
            orgs = [json.loads(line)["organisation_number"] for line in Path(path).read_text().splitlines() if line.strip()]
            hits = {o: by_org[o] for o in orgs if o in by_org}
            ads = {o: len(by_active[o]) for o in orgs if o in by_active}
            batch_stats[name] = {"orgs": len(orgs), "with_extra_nav_name": len(hits), "examples": dict(list(hits.items())[:15]),
                                 "with_active_nav_ad": len(ads), "active_nav_ads": sum(ads.values()), "active_by_org": dict(sorted(ads.items())[:60])}
        stats["batches"] = batch_stats
        stats["seconds"] = round(time.time() - t0)
        print(json.dumps({k: v for k, v in stats.items() if k not in ("nav_application_hosts", "nav_ats_examples", "nav_custom_tenant_hosts")}, indent=1, ensure_ascii=False)[:8000], flush=True)
        if args.stats:
            Path(args.stats).write_text(json.dumps(stats, indent=1, ensure_ascii=False))
        return

    nav_rows: list[tuple] = []
    nav_thread = None
    if not args.no_nav:
        nav_out: list[list[tuple]] = []
        nav_thread = threading.Thread(target=lambda: nav_out.append(read_nav(companies, sub_parent, stats, args.nav_minutes)), daemon=True)
        nav_thread.start()
    dns_rows = probe_dns(companies, args.dns_minutes, stats)
    log(f"dns: {len(dns_rows)} careers hosts aliased to a vendor with a public list ({time.time() - t0:.0f}s)")
    if nav_thread is not None:
        nav_thread.join(timeout=args.nav_minutes * 60 + 120)
        nav_rows = nav_out[0] if nav_out else []
    log(f"nav: {len(nav_rows)} ads applying through a known tenant ({time.time() - t0:.0f}s)")

    lists = confirm(dns_rows + [r[:5] for r in nav_rows], stats)
    out_rows = []
    rule_count = Counter()
    for org, platform, list_url, rule, evidence in dns_rows:
        listing = lists.get(list_url)
        if listing is None or listing.error:
            continue
        names = [listing.name or ""] + sorted({a.employer for a in listing.ads if a.employer} - {listing.name})[:3]
        out_rows.append((org, platform, list_url, rule, evidence, "|".join(n for n in names if n), built))
        rule_count[rule] += 1
    seen_nav = set()
    for org, platform, list_url, rule, evidence, nav_name in sorted(nav_rows):
        listing = lists.get(list_url)
        if listing is None or listing.error or (org, list_url) in seen_nav:
            continue
        names = {listing.name or ""} | {a.employer for a in listing.ads if a.employer}
        wanted = {key(nav_name)} | {key(n) for n in companies[org].names}
        if not ({key(n) for n in names if n} & (wanted - {""})):
            rule_count["nav_dropped_name_mismatch"] += 1
            continue
        seen_nav.add((org, list_url))
        out_rows.append((org, platform, list_url, rule, evidence, "|".join(sorted(n for n in names if n)[:4]), built))
        rule_count[rule] += 1
    stats["rows_by_rule"] = dict(rule_count)
    out = Path(args.out)
    n = write_tsv_gz(out / "ats-tenants.tsv.gz", ats.INDEX_COLUMNS, out_rows)
    stats["rows"] = n
    stats["companies"] = len({r[0] for r in out_rows})
    stats["size_bytes"] = (out / "ats-tenants.tsv.gz").stat().st_size

    # What the index gives each measurement batch: companies with a row, and with an open ad on a
    # listed tenant whose employer (or tenant name, for lists naming none) is the company's name.
    by_org = defaultdict(list)
    for r in out_rows:
        by_org[r[0]].append(r)
    batch_stats = {}
    for spec in filter(None, args.batches.split(",")):
        name, path = spec.split("=", 1)
        orgs = [json.loads(line)["organisation_number"] if line.strip().startswith("{") else line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
        with_row = [o for o in orgs if o in by_org]
        hiring = []
        for org in with_row:
            own = {key(n) for n in companies[org].names} if org in companies else set()
            for r in by_org[org]:
                listing = lists[r[2]]
                good = [a for a in listing.open_ads() if key(a.employer or listing.name or "") in own]
                if good:
                    hiring.append({"orgnr": org, "name": companies[org].name, "list": r[2], "ads": [a.title for a in good][:5]})
                    break
        batch_stats[name] = {"orgs": len(orgs), "with_row": len(with_row), "with_matching_open_ad": len(hiring), "examples": hiring[:20]}
    stats["batches"] = batch_stats
    stats["seconds"] = round(time.time() - t0)
    print(json.dumps(stats, indent=1, ensure_ascii=False)[:20000], flush=True)
    if args.stats:
        Path(args.stats).write_text(json.dumps(stats, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
