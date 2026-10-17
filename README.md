# Kildespor — Signalpost company-research agent

Kildespor ("source trail") takes a batch of Norwegian organisation numbers and
returns one evidence-backed company profile per input. Every published fact
names its source URL, retrieval time, content hash and the span it was read
from. Anything the agent could not establish is reported as `not_available`,
`blocked`, `ambiguous`, `not_applicable` or `failed`, with the reason. It is
never filled in, and absence is never written as zero.

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
| `out/site/index.html` | self-contained viewer: search, filter, compare, verify each fact (desktop and mobile) |
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
| Verified official website and company-owned profiles | register-listed website (entity and subunits), the employer homepage in a verified NAV ad, the register e-mail domain, then domains derived from the legal and subunit names; social and video profiles linked from the verified site, JSON-LD `sameAs`, or the verified ad's employer block | a derived or e-mail domain is published only when the site shows this organisation number (mod-11 checked); a listed site that shows only another entity's number is reported `ambiguous`, never published |
| Hiring and dated public activity | NAV's official vacancy feed (pam-stilling-feed.nav.no, the last 45 days, read once per run); careers page and unexpired JSON-LD job postings on the verified site; dated posts from the site's own RSS/Atom feed, WordPress posts API or news listing (`<time datetime>`); dated role-register changes and register update events | a job ad is published only when the feed entry's own `employer.orgnr` is this entity's or one of its subunits' organisation number |
| Claim-level evidence and availability | every claim → evidence (URL, retrieval time, sha256, span, method) | |
| Refresh metadata and material changes | previous snapshot diff | |

Each envelope also carries a `synthesis` brief: what the company does, who
runs it, where it operates, the latest filed numbers with year-on-year change
and equity ratio, hiring, footprint and activity, what changed, and what
remains unknown. Every sentence lists the claim and evidence ids it rests on.

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
next to its output (`out/profiles.json`), or wherever `--app-data` points:

```bash
python -m kildespor --input companies.jsonl --output runs/latest/envelopes.jsonl --app-data public/data/profiles.json
pnpm install && pnpm export        # static site in out/; see STATIC-DATA.md
```

`pnpm export` writes to `out/`, so give the agent another output folder (as
above) when you use both. The committed `public/data/profiles.json` is the
100-company smoke run below. `kildespor/export_profiles.py` defines the
mapping. The equity ratio it adds is labelled as computed from equity and total
assets, and it cites both figures.

The agent itself needs none of this. The evaluator's install step and run
command are only `pip install -r requirements.txt` and `python -m kildespor ...`.

## 100-company smoke test

[`smoke-test/`](smoke-test/) holds the result of a 100-company run on a clean
machine (fresh virtualenv, only `pip install -r requirements.txt`, then the run
command), plus a refresh re-run of the same batch:
`smoke-test/run-report.json`, `smoke-test/envelopes.jsonl`,
`smoke-test/refresh-report.json` and the viewer `smoke-test/site/index.html`.
[`smoke-test/SUMMARY.md`](smoke-test/SUMMARY.md) summarises it.

## Tests

```bash
python -m unittest discover -s tests
```

Offline tests cover the organisation-number checksum and page matching, domain
guessing, social-link parsing, the URL safety policy, the one-envelope-per-input
contract, evidence resolution, refresh idempotency, change detection and
stale carry-forward.

## Licences

Code: MIT (see `LICENSE`). Dependencies: requests (Apache-2.0),
beautifulsoup4 (MIT), lxml (BSD-3-Clause), soupsieve (MIT), urllib3 (MIT),
idna (BSD-3-Clause), charset-normalizer (MIT), certifi (MPL-2.0),
typing_extensions (PSF). Brreg data is NLOD 2.0.
