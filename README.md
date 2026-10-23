# Kildespor: Norwegian company research agent (Builderr Signalpost)

Give it Norwegian organisation numbers; it returns one profile per company
(register identity, filed accounts, people, verified website, social profiles,
hiring signals and dated news), a plain-English brief, and what changed since
the last run, with a source URL, retrieval time and quoted span for every fact.

```bash
pip install -r requirements.txt
python -m kildespor --input companies.jsonl --output out/envelopes.jsonl
```

**Viewer:** every run writes `out/index.html` (next to the envelopes) and
`out/site/`. Open `out/index.html` in a browser straight from disk (no server,
no network, nothing to install), put `out/` on any static host, or serve it at
Builderr's preferred routes with `python -m kildespor.serve --dir out --port 8000`
(standard library only; every route also answers `?format=json`). The
committed 100-company smoke run is in [`smoke-test/index.html`](smoke-test/index.html).

| task | page on disk (`out/site/…`) | route with `kildespor.serve` |
|---|---|---|
| **Find** a company: one labelled search box (name or organisation number; Enter on an exact match opens it), a directory sorted by data found, filters for website, social profile, hiring, news, changes, revenue and employees | `research/index.html` (also `out/index.html`) | `/research` |
| **Read** one company in the sections of Builderr's sample: Company brief, Company overview, Latest financials, Who runs it?, Locations, Working here, Recent activity, What changed, What we could not establish, Check the evidence, Sources found | `companies/<org>/index.html` | `/companies/<organisation_number>` |
| **Compare** two to four companies on the same financial year, from the Compare button on any row | `compare/index.html?orgs=<org>,<org>` | `/compare?orgs=<org>,<org>` |
| **Verify** any fact: "View source" and the numbered buttons show the source URL, retrieval time, quoted span, extraction method and SHA-256; missing data shows its state (not available, ambiguous, failed, blocked) | on every page | |
| **Refresh**: refresh status, what changed in this run with both sides and their evidence, facts carried forward, sources not fully checked | `refresh/index.html` | `/refresh` |
| **Ask Signalpost**: Company brief, Latest financials, Who runs it?, Working here, Recent activity, or a typed question; answers come only from the run's cited claims | on research and company pages | `/research?q=<question>&format=json` |

`index.html` keeps the older hash routes (`#/companies/<org>`, `#/compare?orgs=`, `#/refresh`).
`python3 tools/ux_tasks.py --envelopes out/envelopes.jsonl` drives these tasks in
Chrome at 375 px and 1280 px, with the pointer and with the keyboard only, and
fails on any miss (it needs `pip install playwright`, and uses the installed Chrome
when Playwright's own Chromium is not downloaded); its screenshots and results are
in [`smoke-test/ux/`](smoke-test/ux/).

The same questions work from the command line:
`python -m kildespor.ask --envelopes out/envelopes.jsonl "hiring companies in Oslo"`.

Every published fact names its source URL, retrieval time, content hash and
the span it was read from. Anything the agent could not establish is reported
as `not_available`, `blocked`, `ambiguous`, `not_applicable` or `failed`, with
the reason. It is never filled in, and absence is never written as zero.

Entry for the Builderr Signalpost challenge. Author: miscusi
(il.miscusi@gmail.com).

## Run it

Python 3.10 or newer. In a fresh clone:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

The run command:

```bash
python -m kildespor --input companies.jsonl --output out/envelopes.jsonl
```

`companies.jsonl` holds one `{"organisation_number": "123456789"}` per line.
A JSON list, a CSV with an `organisation_number` column, or a plain text file
with one number per line also work.

No API key, account or paid service is needed. The agent uses no language
model. Expected third-party cost per official run: **$0**.

### What a run writes

| path | content |
|---|---|
| `out/envelopes.jsonl` | one terminal envelope per input, in input order |
| `out/run-report.json` | validation checks, coverage by field family, request count, p50/p95 latency, runtime, cost |
| `out/index.html` | the viewer's research page, reading `out/site/` |
| `out/site/` | the viewer (pages above): `assets/viewer.js` and `viewer.css` shared by every page, `data/index.js` (one row per company) and `data/companies/<org>.js` (one profile, loaded on demand); about 50 KB per company (49 MB for a 1,000-company run) |
| `out/profiles.json` | the same run in the JBOX web app's format (see "Web app") |
| `state/latest.jsonl`, `state/runs/<run_id>.jsonl` | envelope snapshots used by the next run to refresh profiles |
| `state/snapshots/<sha[:2]>/<sha>.gz` | the raw source bodies behind every claim, content-addressed; each envelope's `source_snapshots` and each evidence record's `snapshot_path` point here |

### Time budget

The agent keeps its own wall-clock budget (`--time-budget`, default 900 s,
or `KILDESPOR_TIME_BUDGET_S`). It reads the official registers for every
company first and writes a complete output file straight away (a few seconds
per 100 companies). Website, profile and job research then improves that file,
and the complete file is rewritten atomically every 90 s. Research stops at 88% of the budget. If the budget runs
out, unfinished work is abandoned and its areas are reported as `failed` with
the reason, so the output on disk always has exactly one envelope per input.
Lower the budget if the evaluator's limit is shorter. `--workers` (default 32)
sets concurrency. `--max-requests` caps outbound requests for the whole run,
redirects and retries included (default 19 per input company).

### Refresh

Run the same command again in the same folder: the agent loads
`state/latest.jsonl` and reports what changed. Or pass a previous run
explicitly with `--previous old/envelopes.jsonl`.

- Each change carries the previous and current value and the evidence for
  both sides (`changes[]`, typed: `new_role`, `ended_role`, `new_job`,
  `closed_job`, `new_location`, `new_filing`, `restated_figure`,
  `changed_<field>`, and others).
- Re-running the same source snapshot produces no changes and no duplicate
  records: claim and evidence ids are deterministic hashes.
  `first_observed_at` is kept from the first run that saw a claim.
- If a source fails during a refresh, the last supported value is kept and
  marked `stale`. It is not reported as removed.

## What it researches

| envelope section | sources | publication gate |
|---|---|---|
| Legal identity and public brand | Brreg Entity Register API (name, form, addresses, NACE, employees, capital, status, purpose, activity, former names, phone, listed website); group structure API; brand and self-description from the verified website | organisation number is the anchor |
| Latest annual accounts and history | Brreg Register of Company Accounts API: up to three fiscal years, company and group accounts, 14 line items each | official figures only; missing items stay missing |
| Leadership and registered workplaces | Brreg roles API (general manager, board, auditor, accountant); registered subunits with address, industry and employees | active roles only; birth dates are never stored |
| Verified official website and company-owned profiles | register-listed website (entity and subunits), the employer homepage in a verified NAV ad, the register e-mail domain, domains derived from the legal, former and subunit names, and declared candidate caches (Wikidata P2333, Overture Maps places; see `CACHE.md`); social and video profiles linked from the verified site (anchors, `data-href`, embedded page plugins, JSON-LD `sameAs`), the verified ad's employer block, or a Wikidata statement on the item that carries this organisation number | a register-listed site is published unless it is parked or names a different, unrelated entity; any other candidate needs one of three proofs on the site itself: this organisation number next to an ownership label, the registered phone or e-mail address (not shared by five or more register entities, plus a second signal or a word of the name), or every word of the legal name in the site's identity text plus a register signal (postcode and town, phone, e-mail domain, or the daglig leder's or chair's name). Group, chain and housing co-op sites stay `ambiguous`. Cache candidates are re-fetched and re-verified live; the agent runs the same without the cache files |
| Hiring and dated public activity | NAV's official vacancy feed (pam-stilling-feed.nav.no, read once per run, matched on the ad's `employer.orgnr`, including subunits, with the verified brand name as a pre-filter; active ads only); single job ads linked from the verified site's careers page and the applicant-tracking pages it links to (Teamtailor, Webcruiter, Jobylon, ReachMee, Recman, HR-manager, Easycruit, Jobbnorge and others), one claim per ad after the ad's own page is read (a careers page, list, login or search page is never a claim); dated news from the site's own WordPress posts API, RSS/Atom feeds, news sitemaps and news listings, with up to three articles read for their own publication time; nothing from a site rated a group, chain or umbrella site; dated role-register changes and register update events (kept separate from news) | a job ad needs this entity or one of its subunits as employer (NAV `employer.orgnr`, JSON-LD `hiringOrganization`, or an ATS tenant tied to the verified site); the ad page must name this entity (or a subunit, or its legal or brand name) as employer, and filled, expired and open-application pages are refused; roles another employer posts, including a parent's or a sister company's, are never published. A news item needs a publication-specific date (published-time metadata, a byline or date block, a feed `pubDate`, the WordPress post date, or the listing date beside the item); event, deadline and update dates and future dates are refused, and so are product, service, guide, template and job-ad posts, posts about the parent group, and bursts of posts sharing one import date |
| Claim-level evidence and availability | every claim → evidence (URL, retrieval time, sha256, span, method) | |
| Refresh metadata and material changes | previous snapshot diff | |

Each envelope also carries a `synthesis` brief written for a buyer, investor
or applicant. It opens with two sentences to act on: what the company does
(legal form, town and Statistics Norway's English industry label), its size
and which way its latest filed figures moved (revenue and net result with
their years and the change on the year before, from the group accounts when a
parent files them), then whether it is hiring now (single job ads only, never
a careers page) and its latest dated activity. Then who runs it, what changed
(run to run, or on a first run the latest filing against the year before and
the latest register change) and what could not be established. The sections
follow Builderr's sample: Company overview, Latest financials, Who runs it?,
Working here, Recent activity, What changed, What we could not establish
(each missing area with its state and reason). Every sentence carries the
claim ids, evidence ids and source URLs it rests on and must pass a
sentence-support check: each number, quotation and name it states has to be
in the claims it cites (or be arithmetic on two of them), or the sentence is
dropped. The template is deterministic and uses no model.
`docs/DATA_SCHEMA.md` describes the fields.

See [`docs/AGENT.md`](docs/AGENT.md) for the research and abstention policy,
[`docs/SOURCES.md`](docs/SOURCES.md) for source rights, secrets and the
outbound URL policy, [`docs/DATA_SCHEMA.md`](docs/DATA_SCHEMA.md) for the
envelope format and [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) for known
gaps.

## Web app (built with JBOX)

The Next.js app at the repository root (`app/`, `components/`, `lib/`, `public/`)
was generated with [JBOX](https://jboxai.com) and exported from JBOX into this
repository. Its history is kept: the commits "Initialize JBOX sandbox base"
(JBOX) and "Publish latest changes" (JBOXAI) come from JBOX, merged in
`Merge JBOX web app`. It is a read-only explorer for the agent's output:
- a directory with search, filters and per-area coverage;
- company profiles where every fact and brief sentence opens its evidence
  record;
- a side-by-side comparison of up to four companies with shareable links;
- light and dark modes, and a mobile layout.

It has no login, database or secrets, and makes no external calls. It reads
`public/data/profiles.json` at runtime. Every agent run writes that dataset
next to its output (`out/profiles.json`), or wherever `--app-data` points, so
to show the run you just made:

```bash
python -m kildespor --input companies.jsonl --output runs/latest/envelopes.jsonl --app-data public/data/profiles.json
pnpm install && pnpm export        # static site in out/; see STATIC-DATA.md
```

or convert an existing run:
`python -m kildespor.export_profiles --input runs/latest/envelopes.jsonl --output public/data/profiles.json`.
`pnpm export` writes to `out/`, so give the agent another output folder (as
above) when you use both. The committed `public/data/profiles.json` is the
100-company smoke run below (run id `smoke-100-a`, shown in the app header),
not demo data. `kildespor/export_profiles.py` defines the mapping: each of the
app's five areas is decided by its own facts (the Hiring area needs a single
job ad), carried-forward facts keep their marker, and both sides of
a change keep their evidence. The equity ratio it adds is labelled as computed
from equity and total assets, and it cites both figures.

The agent itself needs none of this. The evaluator's install step and run
command are only `pip install -r requirements.txt` and `python -m kildespor ...`.

## 100-company smoke test

[`smoke-test/`](smoke-test/) holds the result of a 100-company run on a clean
machine (fresh virtualenv, only `pip install -r requirements.txt`, then the run
command), plus a refresh re-run of the same batch:
`smoke-test/run-report.json`, `smoke-test/envelopes.jsonl`,
`smoke-test/refresh-report.json` and the viewer `smoke-test/index.html` with
`smoke-test/site/` (rebuilt with the current viewer by `python -m kildespor.site
--input smoke-test/envelopes.jsonl --output smoke-test/site --root-page`; it shows
each envelope's own brief). Screenshots and results of the viewer's task checks
at 375 px and 1280 px are in [`smoke-test/ux/`](smoke-test/ux/).
[`smoke-test/SUMMARY.md`](smoke-test/SUMMARY.md) summarises it.

## Tests

```bash
python -m unittest discover -s tests
```

Offline tests cover the organisation-number checksum and page matching, domain
guessing, social-link parsing, the URL safety policy, the one-envelope-per-input
contract, evidence resolution, refresh idempotency, change detection,
stale carry-forward, the brief (every sentence cited with real ids and passing
the sentence-support check, hiring only from job evidence, states kept apart),
the viewer's pages, data files and script, the server's routes, the app export
and the question answering.

## Licences

Code: MIT (see `LICENSE`). Dependencies: requests (Apache-2.0),
beautifulsoup4 (MIT), lxml (BSD-3-Clause), soupsieve (MIT), urllib3 (MIT),
idna (BSD-3-Clause), charset-normalizer (MIT), certifi (MPL-2.0),
typing_extensions (PSF). Brreg data is NLOD 2.0.
