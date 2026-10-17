# Declared caches

Kildespor ships a few small files in `kildespor/data/`, read by
`kildespor/caches.py`. They are declared here as the challenge rules ask
("Cached public-universe material is allowed … External caches must be
declared").

**The caches hold candidates only.** A row is a candidate URL (a website or a
social profile that a public dataset links to a company) or a screening count
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
| `shared-phones.tsv.gz` | Brreg Enhetsregisteret bulk (`enheter` + `underenheter`, `telefon`, `mobil`), 2,047,495 entities | 2026-10-17 | NLOD-2.0 | 2,325 | — | 10 KB | `python3 tools/build_brreg.py` |
| `shared-email-domains.tsv.gz` | same (`epostadresse`), plus a fixed list of mailbox providers | 2026-10-17 | NLOD-2.0 | 2,844 | — | 16 KB | `python3 tools/build_brreg.py` |
| `register-contacts.tsv.gz` | same: phones and e-mails of each universe company and its sub-units, sub-unit websites | 2026-10-17 | NLOD-2.0 | 156,873 | 156,873 | 2.4 MB | `python3 tools/build_brreg.py` |

Total 4.9 MB. Overture's data is a mix of contributed sources; each place keeps
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

## Rebuilding

The scripts in `tools/` need Python 3.10+; `build_overture.py` also needs
`duckdb` (and `ijson` is recommended for the 2 GB register file). They download
their inputs when no local copy is passed.

```bash
python3 tools/build_wikidata.py                   # WDQS query, ~30 s
python3 tools/build_brreg.py                      # Brreg bulk (~310 MB download)
python3 tools/build_overture.py --release 2026-09-23.1   # Overture places scan + Brreg bulk
```

The shipped files were built on Kaggle CPU sessions that run these same
scripts unchanged (the kernels embed `tools/` at push time).
