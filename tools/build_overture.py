"""Build kildespor/data/candidates-overture.tsv.gz: Overture Maps places matched to universe companies.

    python3 tools/build_overture.py [--release 2026-09-23.1] [--places PATH] [--universe PATH]
        [--enheter PATH] [--underenheter PATH] [--out DIR] [--stats PATH] [--spot PATH]

Reads the public Overture release anonymously with DuckDB (``pip install duckdb``),
keeps places with a Norwegian address, and matches them to register entities
(Brreg bulk files, NLOD 2.0) by, strongest first:

1. ``overture_email_exact``: a place e-mail equals a registered ``epostadresse``;
2. ``overture_website_domain``: a place website's registered domain equals a registered
   ``hjemmeside`` domain (the value is then the place's social links);
3. ``overture_email_domain``: a place website's registered domain equals the domain of a
   registered e-mail address (not a generic mailbox provider);
4. ``overture_name_postcode``: the normalised place name equals the normalised register
   name (legal suffixes and connector words removed, æøå folded) and a place postcode
   equals the entity's business, postal or location postcode.

A ``_subunit`` suffix marks a match on a sub-unit's register data, credited to its
parent company. Rules 1-3 must also be corroborated by the place itself (a name
token of four or more letters shared with the company or one of its sub-units, or a
shared postcode); otherwise the rule carries an ``_uncorroborated`` suffix and is weak
(an accountant's or manager's office registered as a client's contact address). Every key must point at exactly one company; a place that matches
two companies at its strongest rule is dropped, and so is a domain, e-mail or social
URL shared by more than a handful of places (chains, directories). Phones are never
used for matching. At most three places per company. Rows cite
``overture:<release>:<place id>`` and the licence of the place's source datasets.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cachelib import CANDIDATE_HEADER, DATA, universe_orgnrs, write_tsv_gz  # noqa: E402
from register import (ENHETER_URL, GENERIC_MAIL, UNDERENHETER_URL, download, email_of, host_of,  # noqa: E402
                      load_units, norm_name, registered_domain)

RELEASE = "2026-09-23.1"
BBOX = (4.0, 32.5, 57.5, 81.5)  # lon min/max, lat min/max: mainland Norway and Svalbard
MAX_PLACES_PER_ORG = 3
MAX_PLACES_PER_DOMAIN = 5   # a website domain on more places than this is a chain or a directory
MAX_PLACES_PER_EMAIL = 5
MAX_PLACES_PER_SOCIAL = 3
LICENCES = {
    "meta": "CDLA-Permissive-2.0", "microsoft": "CDLA-Permissive-2.0", "foursquare": "Apache-2.0",
    "alltheplaces": "CC0-1.0", "pinmeto": "CDLA-Permissive-2.0", "brightquery": "CDLA-Permissive-2.0",
    "overture": "CDLA-Permissive-2.0",
}
SOCIAL_HOSTS = {
    "facebook.com": "facebook", "fb.com": "facebook", "instagram.com": "instagram", "linkedin.com": "linkedin",
    "twitter.com": "x", "x.com": "x", "youtube.com": "youtube", "tiktok.com": "tiktok", "pinterest.com": "pinterest",
}
# Directories, marketplaces, booking and ordering services, link hubs and map links: never a company's own site.
BLOCKED_DOMAINS = {
    "gulesider.no", "1881.no", "proff.no", "purehelp.no", "finn.no", "brreg.no", "regnskapstall.no", "firmasok.no",
    "bedriftsdatabasen.no", "kompass.com", "eniro.no", "enirokart.no", "eniro.com", "hitta.se", "180.no", "forvalt.no",
    "makeplans.com", "makeplans.no", "makeplans.net", "nordpay.no", "booking.com", "bookings.no", "timma.no", "fresha.com",
    "treatwell.no", "treatwell.com", "mittanbud.no", "foodora.no", "wolt.com", "just-eat.no", "ubereats.com",
    "tripadvisor.com", "tripadvisor.no", "tripadvisor.co.uk", "yelp.com", "yelp.no", "foursquare.com", "hotels.com",
    "expedia.com", "expedia.no", "airbnb.com", "airbnb.no", "opentable.com", "resdiary.com", "dinnerbooking.com",
    "google.com", "google.no", "goo.gl", "g.page", "maps.app.goo.gl", "business.site", "linktr.ee", "linkin.bio",
    "squareup.com", "sumup.com", "sumupstore.com", "vipps.no", "bit.ly", "tinyurl.com", "wa.me", "whatsapp.com",
    "hairlist.no", "bestille.no", "onlinebooking.no", "timebestilling.no", "bookingsystem.no", "easytable.com",
    "eventbrite.com", "ticketmaster.no", "facebook.net", "apple.com", "messenger.com", "t.me", "snapchat.com",
}
# Shared hosting platforms: the registered domain says nothing about the owner, so no domain matching on them.
HOSTING = {"wixsite.com", "wix.com", "github.io", "blogspot.com", "squarespace.com", "webflow.io", "weebly.com",
           "jimdo.com", "jimdosite.com", "wordpress.com", "one.com", "site123.me", "myshopify.com", "netlify.app",
           "vercel.app", "godaddysites.com", "mozello.com", "simplesite.com", "strikingly.com", "carrd.co"}
LEVEL = {"email_exact": 0, "website_domain": 1, "email_domain": 2, "name_postcode": 3}


def blocked(dom: str) -> bool:
    return dom in BLOCKED_DOMAINS or "booking" in dom or dom.endswith(".gov") or dom in SOCIAL_HOSTS


def fetch_places(release: str, out: str) -> None:
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    src = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{src}', hive_partitioning=1)").fetchall()]
    print("columns", cols, flush=True)
    extra = ", operating_status" if "operating_status" in cols else ""
    lo1, lo2, la1, la2 = BBOX
    con.execute(f"""COPY (
        SELECT id, names.primary AS name, websites, socials, emails, phones, addresses,
               list_distinct(list_transform(sources, s -> s.dataset)) AS datasets, confidence{extra}
        FROM read_parquet('{src}', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {lo1} AND {lo2} AND bbox.ymin BETWEEN {la1} AND {la2}
          AND len(list_filter(addresses, a -> a.country = 'NO')) > 0
    ) TO '{out}' (FORMAT parquet)""")


def read_places(path: str) -> list[dict]:
    import duckdb
    con = duckdb.connect()
    cur = con.execute(f"SELECT * FROM read_parquet('{path}')")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def social_of(url: str) -> tuple[str, str] | None:
    host = host_of(url)
    if not host:
        return None
    plat = SOCIAL_HOSTS.get(registered_domain(host))
    if not plat:
        return None
    clean = re.sub(r"^http://", "https://", url.strip())
    if not clean.startswith("https://"):
        clean = "https://" + clean.lstrip("/")
    path = re.sub(r"^https://[^/]+", "", clean).split("?", 1)[0].strip("/")
    if not path or path.lower() in ("pages", "groups", "profile.php", "home", "share", "sharer"):
        return None
    return plat, clean.rstrip("/")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default=RELEASE)
    ap.add_argument("--places")
    ap.add_argument("--universe")
    ap.add_argument("--enheter")
    ap.add_argument("--underenheter")
    ap.add_argument("--out", default=str(DATA))
    ap.add_argument("--stats")
    ap.add_argument("--spot")
    args = ap.parse_args()
    t0 = time.time()
    tmp = tempfile.mkdtemp()
    release_date = args.release.split(".")[0]
    places_path = args.places or os.path.join(tmp, "places_no.parquet")
    if not args.places or not os.path.exists(places_path):
        fetch_places(args.release, places_path)
    places = read_places(places_path)
    print(f"places {len(places)} in {time.time() - t0:.0f}s", flush=True)
    if places and "operating_status" in places[0]:
        print("operating_status", Counter(p.get("operating_status") for p in places).most_common(), flush=True)
        places = [p for p in places if p.get("operating_status") not in ("permanently_closed", "temporarily_closed")]

    enheter = args.enheter or download(ENHETER_URL, os.path.join(tmp, "enheter.json.gz"))
    underenheter = args.underenheter or download(UNDERENHETER_URL, os.path.join(tmp, "underenheter.json.gz"))
    universe = universe_orgnrs(args.universe)
    units = load_units(enheter, underenheter)
    print(f"units {len(units)} in {time.time() - t0:.0f}s", flush=True)

    # register keys -> {parent orgnr: matched on a sub-unit only?}
    idx: dict[str, dict] = {k: defaultdict(dict) for k in LEVEL}
    legal_name: dict[str, str] = {}
    reg_site: dict[str, str] = {}
    org_tokens: dict[str, set] = defaultdict(set)
    org_pcs: dict[str, set] = defaultdict(set)

    def put(kind: str, key: str, u) -> None:
        cur = idx[kind][key].get(u.parent)
        idx[kind][key][u.parent] = u.subunit if cur is None else (cur and u.subunit)

    for u in units:
        if u.parent in universe:
            org_tokens[u.parent].update(t for t in norm_name(u.name).split() if len(t) >= 4)
            org_pcs[u.parent].update(u.postcodes)
        if not u.subunit:
            legal_name[u.orgnr] = u.name
            if u.website and host_of(u.website):
                reg_site[u.orgnr] = registered_domain(host_of(u.website))
        if u.email:
            put("email_exact", u.email, u)
            dom = registered_domain(u.email.rsplit("@", 1)[1])
            if dom not in GENERIC_MAIL and not blocked(dom) and dom not in HOSTING:
                put("email_domain", dom, u)
        host = host_of(u.website)
        if host:
            dom = registered_domain(host)
            if not blocked(dom) and dom not in HOSTING:
                put("website_domain", dom, u)
        key = norm_name(u.name)
        if key:
            for pc in u.postcodes:
                put("name_postcode", f"{key}|{pc}", u)
    del units

    # place-side frequencies: chains and directories
    dom_freq, email_freq, social_freq = Counter(), Counter(), Counter()
    for p in places:
        dom_freq.update({registered_domain(h) for h in (host_of(w) for w in (p.get("websites") or [])) if h})
        email_freq.update({e for e in (email_of(x) for x in (p.get("emails") or [])) if e})
        social_freq.update({s[1].lower() for s in (social_of(x) for x in (p.get("socials") or [])) if s})

    def lookup(kind: str, key: str):
        hit = idx[kind].get(key)
        if hit and len(hit) == 1:
            (org, sub), = hit.items()
            return org, sub
        return None

    matches: dict[str, list] = defaultdict(list)
    rule_counts = Counter()
    ambiguous = 0
    for p in places:
        found = []
        emails = {e for e in (email_of(x) for x in (p.get("emails") or [])) if e and email_freq[e] <= MAX_PLACES_PER_EMAIL}
        doms = set()
        for w in p.get("websites") or []:
            h = host_of(w)
            if h:
                d = registered_domain(h)
                if not blocked(d) and d not in HOSTING and dom_freq[d] <= MAX_PLACES_PER_DOMAIN:
                    doms.add(d)
        for e in emails:
            if (m := lookup("email_exact", e)):
                found.append(("email_exact", *m))
        for d in doms:
            if (m := lookup("website_domain", d)):
                found.append(("website_domain", *m))
            if (m := lookup("email_domain", d)):
                found.append(("email_domain", *m))
        key = norm_name(p.get("name"))
        pcs = {str((a or {}).get("postcode") or "").strip() for a in p.get("addresses") or [] if (a or {}).get("country") == "NO"}
        pcs = {pc for pc in pcs if re.fullmatch(r"\d{4}", pc)}
        if key:
            for pc in pcs:
                if (m := lookup("name_postcode", f"{key}|{pc}")):
                    found.append(("name_postcode", *m))
        if not found:
            continue
        best = min(LEVEL[f[0]] for f in found)
        top = {(f[1]) for f in found if LEVEL[f[0]] == best}
        if len(top) != 1:
            ambiguous += 1
            continue
        org = top.pop()
        if org not in universe:
            continue
        rule, _, sub = min((f for f in found if f[1] == org), key=lambda f: (LEVEL[f[0]], f[2]))
        name = f"overture_{rule}" + ("_subunit" if sub else "")
        if rule != "name_postcode" and not (set(key.split()) & org_tokens[org] or pcs & org_pcs[org]):
            name += "_uncorroborated"
        rule_counts[name] += 1
        matches[org].append((LEVEL[rule], -(p.get("confidence") or 0), name, p))

    rows = []
    spot = []
    per_rule_orgs = Counter()
    for org, ms in matches.items():
        ms.sort(key=lambda m: (m[0], m[1], m[3]["id"]))
        for level, _, name, p in ms[:MAX_PLACES_PER_ORG]:
            per_rule_orgs[name] += 1
            rec = f"overture:{args.release}:{p['id']}"
            lic = "+".join(sorted({LICENCES.get(str(d).lower(), f"LicenseRef-overture-{d}") for d in (p.get("datasets") or [])})) or "CDLA-Permissive-2.0"
            webs = []
            for w in p.get("websites") or []:
                s = social_of(w)
                if s and social_freq[s[1].lower()] <= MAX_PLACES_PER_SOCIAL:
                    rows.append((org, "social", s[0], s[1], name, rec, release_date, lic))
                    continue
                h = host_of(w)
                if not h or s:
                    continue
                d = registered_domain(h)
                if blocked(d) or dom_freq[d] > MAX_PLACES_PER_DOMAIN and d not in HOSTING:
                    continue
                if name.startswith("overture_website_domain") or reg_site.get(org) == d:
                    continue  # the register already lists this site
                url = w.strip() if re.match(r"^https?://", w.strip(), re.I) else "https://" + w.strip()
                rows.append((org, "website", "", url, name, rec, release_date, lic))
                webs.append(url)
            for x in p.get("socials") or []:
                s = social_of(x)
                if s and social_freq[s[1].lower()] <= MAX_PLACES_PER_SOCIAL:
                    rows.append((org, "social", s[0], s[1], name, rec, release_date, lic))
            if True:
                spot.append({"orgnr": org, "legal_name": legal_name.get(org), "place": p.get("name"), "rule": name,
                             "postcodes": [a.get("postcode") for a in p.get("addresses") or [] if a], "websites": webs,
                             "socials": list(p.get("socials") or []), "id": p["id"]})

    out = Path(args.out)
    n = write_tsv_gz(out / "candidates-overture.tsv.gz", CANDIDATE_HEADER, rows)
    orgs_web = {r[0] for r in rows if r[1] == "website"}
    orgs_soc = {r[0] for r in rows if r[1] == "social"}
    stats = {
        "release": args.release, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "places_no": len(places), "places_matched": sum(rule_counts.values()), "ambiguous_places": ambiguous,
        "rule_places": dict(rule_counts), "rule_shipped_places": dict(per_rule_orgs),
        "orgs": len({r[0] for r in rows}), "orgs_website": len(orgs_web), "orgs_social": len(orgs_soc),
        "rows": n, "rows_by_platform": dict(Counter(r[2] or "website" for r in rows)),
        "licences": dict(Counter(r[7] for r in rows)),
        "datasets": dict(Counter(d for p in places for d in (p.get("datasets") or []))),
        "top_place_domains": dom_freq.most_common(60), "top_place_socials": social_freq.most_common(20),
        "size_bytes": (out / "candidates-overture.tsv.gz").stat().st_size, "seconds": round(time.time() - t0),
    }
    print(json.dumps(stats, indent=1, default=str), flush=True)
    if args.stats:
        Path(args.stats).write_text(json.dumps(stats, indent=1, default=str))
    if args.spot:
        random.seed(20261017)
        by_rule = defaultdict(list)
        for x in spot:
            by_rule[x["rule"]].append(x)
        sample = [x for xs in by_rule.values() for x in random.sample(xs, min(150, len(xs)))]
        Path(args.spot).write_text(json.dumps(sample, indent=1, default=str))


if __name__ == "__main__":
    main()
