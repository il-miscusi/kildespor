"""Build the shared-identifier screen and the register-contacts cache from the Brreg bulk files (NLOD 2.0).

    python3 tools/build_brreg.py [--universe PATH] [--enheter PATH] [--underenheter PATH] [--out DIR] [--stats PATH]

Missing inputs are downloaded (main file ~210 MB, sub-unit file ~100 MB): run it
on a build machine, not inside the agent. Writes to ``kildespor/data/`` by default:

* ``shared-phones.tsv.gz``: every phone number (last eight digits) listed by five or
  more distinct companies, counting a sub-unit under its parent company;
* ``shared-email-domains.tsv.gz``: every e-mail domain used by five or more distinct
  companies, plus the generic mailbox providers (count ``generic``);
* ``register-contacts.tsv.gz``: for each universe company, the phones and e-mail
  addresses of the company and its sub-units, and sub-unit websites that differ from
  the company's own ``hjemmeside``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cachelib import DATA, universe_orgnrs, write_tsv_gz  # noqa: E402
from register import ENHETER_URL, GENERIC_MAIL, UNDERENHETER_URL, download, host_of, load_units  # noqa: E402

MIN_SHARE = 5
MAX_PER_FIELD = 8


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe")
    ap.add_argument("--enheter")
    ap.add_argument("--underenheter")
    ap.add_argument("--out", default=str(DATA))
    ap.add_argument("--stats")
    ap.add_argument("--min-share", type=int, default=MIN_SHARE)
    args = ap.parse_args()
    t0 = time.time()
    tmp = tempfile.mkdtemp()
    enheter = args.enheter or download(ENHETER_URL, os.path.join(tmp, "enheter.json.gz"))
    underenheter = args.underenheter or download(UNDERENHETER_URL, os.path.join(tmp, "underenheter.json.gz"))
    universe = universe_orgnrs(args.universe)
    units = load_units(enheter, underenheter)
    print(f"units {len(units)} ({sum(u.subunit for u in units)} sub-units) in {time.time() - t0:.0f}s", flush=True)

    phone_pairs: set[tuple[str, str]] = set()
    domain_pairs: set[tuple[str, str]] = set()
    by_parent: dict[str, list] = defaultdict(list)
    main_site: dict[str, str] = {}
    for u in units:
        for p in u.phones:
            phone_pairs.add((p, u.parent))
        if u.email:
            domain_pairs.add((u.email.rsplit("@", 1)[1].removeprefix("www."), u.parent))
        if u.parent in universe:
            by_parent[u.parent].append(u)
            if not u.subunit and u.website:
                main_site[u.orgnr] = host_of(u.website) or ""
    phones = Counter(p for p, _ in phone_pairs)
    domains = Counter(d for d, _ in domain_pairs)
    shared_phones = {p: n for p, n in phones.items() if n >= args.min_share}
    shared_domains = {d: str(n) for d, n in domains.items() if n >= args.min_share}
    for d in GENERIC_MAIL:
        shared_domains.setdefault(d, "generic")

    out = Path(args.out)
    n_phone = write_tsv_gz(out / "shared-phones.tsv.gz", ("phone8", "count"), ((p, str(n)) for p, n in shared_phones.items()))
    n_dom = write_tsv_gz(out / "shared-email-domains.tsv.gz", ("domain", "count"), shared_domains.items())

    rows = []
    stats_c = Counter()
    for org, us in by_parent.items():
        us.sort(key=lambda u: (u.subunit, u.orgnr))
        ph: list[str] = []
        em: list[str] = []
        ws: list[str] = []
        own = main_site.get(org, "")
        for u in us:
            ph += [p for p in u.phones if p not in ph]
            if u.email and u.email not in em:
                em.append(u.email)
            if u.subunit and u.website:
                h = host_of(u.website)
                if h and h != own and u.website not in ws:
                    ws.append(u.website)
        if ph or em or ws:
            rows.append((org, "|".join(ph[:MAX_PER_FIELD]), "|".join(em[:MAX_PER_FIELD]), "|".join(ws[:MAX_PER_FIELD])))
            stats_c["any"] += 1
            stats_c["phone"] += bool(ph)
            stats_c["email"] += bool(em)
            stats_c["subunit_website"] += bool(ws)
    n_contacts = write_tsv_gz(out / "register-contacts.tsv.gz", ("orgnr", "phones", "emails", "websites"), rows)

    stats = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "units": len(units), "subunits": sum(u.subunit for u in units), "universe": len(universe),
        "shared_phones": n_phone, "shared_email_domains": n_dom, "register_contacts_rows": n_contacts,
        "universe_with": dict(stats_c),
        "sizes": {p.name: p.stat().st_size for p in out.glob("*.tsv.gz")},
        "top_shared_domains": domains.most_common(40),
        "top_shared_phones": phones.most_common(20),
        "seconds": round(time.time() - t0),
    }
    print(json.dumps(stats, indent=1), flush=True)
    if args.stats:
        Path(args.stats).write_text(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
