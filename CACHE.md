# Declared caches

Kildespor ships a few small files in `kildespor/data/`, read by
`kildespor/caches.py`. They are declared here as the challenge rules ask
("Cached public-universe material is allowed … External caches must be
declared").

**The caches hold candidates only.** A row is a candidate URL (a website or a
social profile that a public dataset links to a company, or a website whose own
page printed the company's organisation number) or a screening count
(how many register entities share a phone number or an e-mail domain). No row
is a published fact or a precomputed profile. The agent fetches each candidate
website live and publishes it only after its own identity checks, with the
live retrieval time as evidence. A Wikidata social row is published only after
the agent fetches that Wikidata item live (`Special:EntityData/<QID>.json`,
which wikidata.org's robots.txt allows) and the item states both this
organisation number (P2333) and the profile's own property value; that
snapshot, its hash and retrieval time are the claim's evidence, and without
the live confirmation nothing is published. Overture rows never publish a
social profile: they only propose websites for live verification. Every row
carries a citable `source_record` and the source's licence.

**The agent runs correctly without them.** Delete any or all of
`kildespor/data/*.tsv.gz` and every lookup returns an empty result
(`candidates()` → `[]`, `shared_phone()`/`shared_email_domain()` → `False`,
`register_contacts()` → empty lists); discovery falls back to the live
register, search and site crawl. `tests/test_caches.py` covers that case.

Each cache is built over the whole public universe
(`signalpost-company-universe-2025.jsonl.gz`, 411,160 organisations) and
restricted to it. Nothing is keyed to an evaluation draw.

## Files

| file | source | release / query date | licence | rows | companies | size | build |
|---|---|---|---|---|---|---|---|
| `candidates-wikidata.tsv.gz` | Wikidata Query Service: items with a Norwegian organisation number (P2333) and their website and social ids | queried 2026-10-17 | CC0-1.0 | 6,869 (3,690 websites, 1,507 Facebook, 1,003 Instagram, 607 X, 395 LinkedIn, 390 YouTube) | 3,600 | 124 KB | `python3 tools/build_wikidata.py` |
| `candidates-overture.tsv.gz` | Overture Maps places, release `2026-09-23.1` (249,910 places with a Norwegian address), matched to the Brreg bulk register of 2026-10-17 | 2026-09-23 | per place: CDLA-Permissive-2.0 (Overture, Meta, Microsoft, PinMeTo), Apache-2.0 (Foursquare), CC0-1.0 (AllThePlaces); the row's `licence` lists its place's | 72,067 (25,458 websites, 46,285 Facebook, 151 Instagram, 80 X, 44 LinkedIn, 30 YouTube, 19 TikTok) | 45,506 | 2.4 MB | `python3 tools/build_overture.py --release 2026-09-23.1` |
| `candidates-noindex.tsv.gz` | our own crawl of public `.no` websites: domains whose page printed the organisation number next to an ownership label (see below) | crawled 2026-10-17 08:19–13:08 UTC | facts we extracted, shipped under this repository's MIT licence; no page content | 24,842 websites (up to 3 per company) | 23,305 | 0.5 MB | `python3 tools/build_noindex.py noindex.tsv.gz` |
| `shared-phones.tsv.gz` | Brreg Enhetsregisteret bulk (`enheter` + `underenheter`, `telefon`, `mobil`), 2,047,495 entities | 2026-10-17 | NLOD-2.0 | 2,325 | — | 10 KB | `python3 tools/build_brreg.py` |
| `shared-email-domains.tsv.gz` | same (`epostadresse`), plus a fixed list of mailbox providers | 2026-10-17 | NLOD-2.0 | 2,844 | — | 16 KB | `python3 tools/build_brreg.py` |
| `register-contacts.tsv.gz` | same: phones and e-mails of each universe company and its sub-units, sub-unit websites | 2026-10-17 | NLOD-2.0 | 156,873 | 156,873 | 2.4 MB | `python3 tools/build_brreg.py` |

Total 5.4 MB. Overture's data is a mix of contributed sources; each place keeps
its `sources[].dataset` list and the row's `licence` field names every licence
that applies to it. Attribution: Overture Maps Foundation and its places
contributors (Meta, Microsoft, Foursquare, AllThePlaces, PinMeTo), as listed at
<https://docs.overturemaps.org/attribution/>. Wikidata is CC0. Brreg data is
published by Brønnøysundregistrene under NLOD 2.0
(<https://data.norge.no/nlod/en/2.0>).

Coverage of the 411,160-company universe: 11.5% of companies have at least one
Overture or Wikidata candidate (10.7% a social candidate, 6.8% a website
candidate). On a random 400 (`dev400`): 10.75% any, 10.0% social, 4.75% a
website not already in the register.

## Format

Gzipped TSV, header line first, sorted, written with a fixed gzip timestamp
so a rebuild from the same inputs gives the same bytes.

| file | columns |
|---|---|
| `candidates-<source>.tsv.gz` | `orgnr kind platform url match_rule source_record release_date licence` |
| `candidates-noindex.tsv.gz` | `orgnr rank domain page_url rule domain_orgnrs retrieved_at` |
| `shared-phones.tsv.gz` | `phone8 count` (last eight digits) |
| `shared-email-domains.tsv.gz` | `domain count` (`generic` for mailbox providers) |
| `register-contacts.tsv.gz` | `orgnr phones emails websites` (`\|`-separated) |

`kind` is `website` or `social`; `platform` is set for social rows
(`facebook`, `instagram`, `linkedin`, `x`, `youtube`, `tiktok`, `pinterest`).

## Match rules

| rule | meaning | strength |
|---|---|---|
| `wikidata_p2333_exact_orgnr` | the Wikidata item states this organisation number (P2333) and the URL (P856 website, P2013 Facebook, P4264 LinkedIn, P2003 Instagram, P2002 X, P2397 YouTube) | strong |
| `overture_email_exact` | a place e-mail equals the company's registered e-mail (`epostadresse`) and the address belongs to no other register entity | strong when corroborated (below) |
| `overture_website_domain` | a place website has the company's registered `hjemmeside` domain, held by no other entity; the rows are the place's social links | strong when corroborated |
| `overture_email_domain` | a place website has the domain of the company's registered e-mail (not a mailbox provider, held by no other entity) | medium |
| `overture_name_postcode` | normalised place name equals the normalised register name (legal suffixes and connector words removed, æøå folded) and a place postcode equals the business, postal or location postcode; the key points at one company only | weak (verify on the site) |
| `…_subunit` | the same rule matched on a sub-unit's register data, credited to its parent company | medium at best |
| `…_name_only` | e-mail/domain rule; the place name shares one distinctive token with the company but no postcode (often a sister company's location) | weak |
| `…_postcode_only` | e-mail/domain rule; the place shares a postcode but no distinctive name token (a holding company's restaurant, a landlord's tenant) | weak |
| `…_uncorroborated` | e-mail/domain rule with neither (an accountant's or lawyer's office registered as the client's contact) | weak |

"Corroborated" means the place name shares a distinctive token with the
company or one of its sub-units (four or more letters, in at most 300 register
names, not a post-town or municipality name) and, in addition, the place shares
a postcode with the company, or two such tokens, or the same normalised name.
Strong means exactly `wikidata_p2333_exact_orgnr`, `overture_email_exact` and
`overture_website_domain`; any suffix makes a rule weaker.

Overture places are never matched on phone numbers alone. A website domain,
e-mail or social URL that appears on more than a handful of Norwegian places
(chains, directories) is not used, directory, booking and marketplace domains
are blocked, a place that matches two companies at its strongest rule is
dropped, and each company keeps at most three places.

## The `.no` reverse organisation-number index (`candidates-noindex.tsv.gz`)

Built from our own polite crawl of public websites on 17 October 2026
(08:19–13:08 UTC), from Kaggle CPU sessions:

- Hosts: `.no` registered domains in Common Crawl's domain-level web graph
  (releases 2025 Jul–Sep to 2026 Jul–Sep, 379,950 domains), the website and
  e-mail domains of universe companies in the Brreg bulk file (55,783), and
  `.no` names derived from universe companies' legal names (most do not
  resolve). Common Crawl supplied only the host list, used under its terms of
  use; no Common Crawl page content was used.
- Fetching: User-Agent `KildesporBot/0.3 (+https://github.com/il-miscusi/kildespor)`;
  robots.txt fetched once per origin and honoured (a robots.txt that times
  out, refuses the connection or answers 401/403/429/5xx means the host is not
  crawled; Crawl-delay honoured); at most the homepage plus one
  contact/privacy/about/terms page per host, at most two connections per
  origin, 8 s timeouts, no retries; 970,628 requests in all.
- Kept: an (organisation number, domain) pair when the page printed a
  mod-11-valid number in the universe next to an ownership label
  (`org.nr`, `organisasjonsnummer`, `foretaksregisteret`, `MVA`, `VAT`, …) and
  the page's owner was unambiguous. At most three domains per company, ranked;
  domains printed as their own by more than five companies are dropped.

What ships is only the pair, the page URL that printed the number, how the
owner was decided and the crawl time: no page text. The pairs are facts we
extracted, shipped under this repository's licence. Like every other cache row
they are candidates: `caches.candidates()` returns them as `kind="website"`,
`source="noindex"` with the page's `scheme://host/` as `url` and the page URL
as `source_record`, after every other source's rows, and the agent fetches the
site live and runs its own identity checks before it publishes anything. A
company page that prints the number is good evidence but not proof that it is
the main site (gold set: 28 of 30 top-ranked domains were Builderr's; the two
misses were a sister brand's site and an information site, both owned by the
company).

| `rule` | how the page's owner was decided | order |
|---|---|---|
| `single` | exactly one labelled number on the page | by rank |
| `footer` | one labelled number inside `<footer>` | by rank |
| `copyright` | one labelled number within 150 characters of © | by rank |
| `ld` | JSON-LD `vatID` / `taxID` / `orgNumber` | by rank |
| `raw` | one labelled number found only in script or attribute text | last |

A row whose domain several companies print as their own (`domain_orgnrs` > 1: a
shared platform such as `styret.com`, a group site, a franchise) also goes last.

## Rebuilding

The scripts in `tools/` need Python 3.10+; `build_overture.py` also needs
`duckdb` (and `ijson` is recommended for the 2 GB register file). They download
their inputs when no local copy is passed.

```bash
python3 tools/build_wikidata.py                   # WDQS query, ~30 s
python3 tools/build_brreg.py                      # Brreg bulk (~310 MB download)
python3 tools/build_overture.py --release 2026-09-23.1   # Overture places scan + Brreg bulk
python3 tools/build_noindex.py noindex.tsv.gz     # trims the crawl's merged index to the shipped columns
```

The crawl behind `noindex.tsv.gz` ran as Kaggle kernels in the campaign
workspace (`noindex_*` kernels, merged by `noindex_m`), not from this
repository; `tools/build_noindex.py` keeps universe rows and the columns above
and drops the crawl's page snippets.

The shipped files were built on Kaggle CPU sessions that run these same
scripts unchanged (the kernels embed `tools/` at push time).

## The ATS tenant index (`ats-tenants.tsv.gz`)

| file | source | build date | licence | rows | companies | size | build |
|---|---|---|---|---|---|---|---|
| `ats-tenants.tsv.gz` | Brreg bulk register (NLOD 2.0), public DNS, NAV's vacancy feed, and each tenant's public job list | 2026-10-23 | NLOD-2.0 for the register fields; the rest are facts we derived | 269 (154 Teamtailor and 3 Recruitee by DNS alias, 112 Teamtailor by NAV ad) | 257 | 15 KB | `python3 tools/build_ats.py` (Kaggle CPU, 56 min) |

Columns: `orgnr platform list_url match_rule match_evidence names built_at`.

A row says that one universe company has an applicant-tracking tenant whose
open ads the agent can read in one keyless request (`kildespor/ats.py`). It
is a pointer, never a fact: at run time the agent reads the list live, and
an ad is published only when it names this entity exactly (legal, subunit or
verified brand name) or, on a list that names no employer at all, when its
workplace is in Norway. The list response is the claim's evidence, with the
row's match rule in its access policy. A company the index does not name
loses nothing: its website's careers page and NAV's feed are read as before.

| `match_rule` | how the tenant was tied to the organisation number |
|---|---|
| `register_domain_cname` | a careers host on the company's own domain (`karriere.`, `jobb.`, `jobs.`, `career.`, `careers.`, `jobber.`) is a DNS alias of Teamtailor (`ext.teamtailor.com`) or Recruitee (`secure.recruitee.com`), and that domain is the company's Brreg `hjemmeside`, registered e-mail domain or a sub-unit's website, stated by no other register entity; a domain on which a random name also resolves (a wildcard) is skipped |
| `nav_ad_employer_orgnr` | an active NAV ad names the company or one of its sub-units by `employer.orgnr` and applies through the tenant, and the tenant's own list names that employer (NAV's employer name or the legal name, exactly); 9 ties failed that check (agencies recruiting for clients) and were dropped |

Inputs of the 23 Oct build: 42,559 universe companies with a domain of their
own (45,108 domains, 270,648 DNS queries, 212 Teamtailor and 3 Recruitee
aliases; SuccessFactors, Homerun, ReachMee and Emply aliases were found but
have no keyless list); 10,683 active NAV ads read entry by entry (840 feed
pages). 230 Teamtailor and 3 Recruitee lists answered; 59 Teamtailor hosts
did not serve a feed and were left out. Coverage is small (257 companies,
0.06% of the universe): most companies on a random draw have no ATS at all,
and the index's job is the few whose website is not verified at run time or
does not link its career site.

### NAV employer names (`ats-nav-names.tsv.gz`)

| file | source | build date | licence | rows | companies | size | build |
|---|---|---|---|---|---|---|---|
| `ats-nav-names.tsv.gz` | NAV's vacancy feed (`pam-stilling-feed.nav.no`, public token): every active ad's `businessName` and `employer.orgnr`, with sub-units mapped to their company by the Brreg bulk register | 2026-10-23 | facts we derived from NAV's redistributed ads | 1,474 | 620 | 22 KB | `python3 tools/build_ats.py --nav-names-only` (Kaggle CPU, 40 min) |

Columns: `orgnr business_name ads name_ads built_at`.

NAV's feed lists an ad only by the name the employer advertised under, and
the agent opens a feed entry only when that name is one of the company's own
(legal, sub-unit or verified brand). A row adds a name under which NAV listed
an ad naming this company (or one of its sub-units) by `employer.orgnr` on
23 October 2026: Norasondegruppen's ads name NORASONDE KARRIERE AS's Bergen
sub-unit and were never opened. Kept only when the name is none of the
company's legal or sub-unit names, and when the company's ads are at least half
of the name's ads or the name has at most eight (76 shared chain names were
dropped, so a chain's brand does not send every member to open dozens of
entries). The names choose which live entries to open; an ad is published
only on its live `employer.orgnr`. They are never used to match Jobbnorge or
ATS employers.

### Active NAV ads by company (`ats-nav-active.tsv.gz`)

| file | source | build date | licence | rows | companies | size | build |
|---|---|---|---|---|---|---|---|
| `ats-nav-active.tsv.gz` | NAV's vacancy feed, read in full (840 pages) and every active entry fetched (10,970 ads); each ad's `employer.orgnr` mapped to its universe company through the Brreg bulk register's sub-units | 2026-10-23 | facts we derived from NAV's redistributed ads | 5,528 | 2,411 | 265 KB | same run as above (`--nav-names-only` writes both) |

Columns: `orgnr uuid employer_orgnr business_name published expires changed built_at`.

NAV's feed returns what changed in the window the agent reads (45 days), and a
feed entry is opened only when its listed name is one of the company's own. An
ad published in June and unchanged since, or listed under a department's name
("Medisinsk avdeling Flekkefjord, Sørlandet sykehus HF"), was never seen: the
pool missed Sørlandet Sykehus, Madsen og Brekke, B.A. Gruppen, Mercell, REMA
Dusavik, Norlandia Eltonåsen and Flexibemanning for these two reasons. A row
names one ad that was active on the build date and the company it was
advertised for. At run time the agent fetches those entries live (at most 30 per
company) unless the window has seen the ad close or its expiry has passed, and
publishes an entry only when it is ACTIVE and names this entity or one of its
sub-units by `employer.orgnr`. The live feed supplies what changed after the
build: a snapshot plus the changes since is the whole active set, and an index
older than the 45-day window widens the window to its age (at most 120 days).
Ads whose employer is shown as "Anonym arbeidsgiver" are not published.

