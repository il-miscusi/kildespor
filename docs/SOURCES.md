# Sources, rights, secrets and outbound URL policy

## Sources and access rights

| source | used for | access | rights |
|---|---|---|---|
| `data.brreg.no/enhetsregisteret/api` (entities, roles, subunits, group structure, update feed) | identity, leadership, workplaces, group links, register activity | official open API, no key | NLOD 2.0 |
| `data.brreg.no/regnskapsregisteret/regnskap` | filed annual accounts | official open API, no key | NLOD 2.0 |
| company websites (register-listed or verified by organisation number) | brand, self-description, contacts, profiles, careers page, JSON-LD jobs and articles, RSS/Atom | static HTTP GET of a few pages per site; robots.txt honoured | company-owned material, cited with source and date |
| `pam-stilling-feed.nav.no` (NAV vacancy feed) | active job ads with employer organisation number and homepage | NAV's official feed API with its published public token, fetched at run time; read once per run for the last 45 days | job ads NAV redistributes for reuse |
| applicant-tracking systems' public job lists: Teamtailor `<career host>/jobs.json`, Greenhouse `boards-api.greenhouse.io/v1/boards/<token>/jobs`, Lever `api.lever.co/v0/postings/<tenant>?mode=json`, Ashby `api.ashbyhq.com/posting-api/job-board/<name>`, Personio `<tenant>.jobs.personio.de/xml`, Recruitee `<tenant>.recruitee.com/api/offers/`, Workable `apply.workable.com/api/v1/widget/accounts/<account>` | the open ads of a tenant the verified site is or links, or that the declared ATS index (CACHE.md) ties to the organisation number; one claim per ad naming this employer | one keyless GET per tenant (the list the vendor publishes for career sites and job boards); robots.txt honoured (each host allowed the list path when adopted on 23 Oct 2026); each evidence record carries the access policy | the employer's own published job ads, cited with source and date |
| `publicapi.jobbnorge.no/v3/Jobs` (Jobbnorge) | open ads, mostly public-sector employers, matched on the employer's exact name plus a registered municipality or postcode | keyless public API (Swagger at `/swagger`), read once per run; the host serves no robots.txt | ads published for redistribution by the board's own public API |

Not read by design: SmartRecruiters' API (its robots.txt disallows every agent but LinkedIn's),
Webcruiter's candidate pages (robots.txt disallows every agent but Google's), finn.no (terms forbid
automated use), and Jobylon, Recman and ReachMee lists (per-customer hashes, tokens or login pages).

Not used: search engines, LinkedIn, Meta (Facebook/Instagram), Glassdoor,
Indeed, Google, review sites, or any unofficial client for them; none of those
platforms is ever fetched. A profile on them is published only when a verified
company site links to it, or when Wikidata states it on the item that carries
this exact organisation number (P2333; the claim cites the Wikidata statement).
`w2.brreg.no/kunngjoring/` is not fetched because its robots.txt disallows it.

## Rate and politeness

- User-Agent `KildesporBot/<version> (+https://github.com/il-miscusi/kildespor; company-research agent)`.
- robots.txt is fetched once per origin and honoured for every page.
- Concurrency limits per host: data.brreg.no 10, pam-stilling-feed.nav.no 3,
  any other host 2. On HTTP 429/503 every worker pauses that host (honouring
  `Retry-After`) before retrying.
- A run-wide request cap (default 19 per input company) counts every request,
  redirects and retries included.
- Per company, at most a handful of pages per website are read (home, up to
  three contact/about/privacy pages, one careers page, one feed).

## Secrets

The agent needs none. It reads no environment secrets and writes no
credentials. NAV's vacancy-feed token is the public token NAV publishes at
`/api/publicToken`, fetched fresh each run. Optional settings are plain environment variables:
`KILDESPOR_TIME_BUDGET_S`, `KILDESPOR_WORKERS`, `KILDESPOR_STATE_DIR`,
`KILDESPOR_REGISTRY_SNAPSHOT`, `KILDESPOR_RUN_ID`, `KILDESPOR_MAX_REQUESTS`,
`KILDESPOR_REQUESTS_PER_COMPANY`, `KILDESPOR_NAV_FEED_DAYS`.

## Outbound URL policy (`kildespor/http.py`)

Every request, including each redirect hop, must pass:

- scheme `http` or `https` only; no credentials in the URL; default ports only;
- no literal-IP hosts; the host must resolve, and every resolved address must
  be public (private, loopback, link-local, multicast, reserved and
  unspecified ranges are refused);
- at most 5 redirects, each re-checked;
- response bodies capped at 3 MB; connect and read timeouts on every request;
- robots.txt allows the path.

URLs taken from web pages are only fetched when they stay on the verified
company's registered domain (contact, careers and feed links). Social links
are recorded, never fetched. Any URL published in a claim must be `http(s)` on
a public host (and on the company's domain for company-owned items); the viewer
renders only `http(s)` links, opens them with `rel="noopener noreferrer"`, and
makes no network request except for its own data files under `site/data/`.
It never fetches an evidence URL itself.

## Caches

The agent ships declared discovery caches in `kildespor/data/`, read by
`kildespor/caches.py` and documented in `CACHE.md` (source, release or query
date, licence, row count, size and build command for each file):

| file | source | licence |
|---|---|---|
| `candidates-wikidata.tsv.gz` | Wikidata P2333 items: website and social ids (queried 2026-10-17) | CC0-1.0 |
| `candidates-overture.tsv.gz` | Overture Maps places, release 2026-09-23.1, matched to register companies | CDLA-Permissive-2.0 / Apache-2.0 / CC0-1.0 per place |
| `candidates-noindex.tsv.gz` | our own robots-honouring crawl of public `.no` sites (17 Oct 2026): domains whose page printed the organisation number as its owner's; no page text | facts we extracted, MIT (this repository) |
| `shared-phones.tsv.gz`, `shared-email-domains.tsv.gz` | Brreg bulk register: phone numbers and e-mail domains held by five or more companies | NLOD 2.0 |
| `register-contacts.tsv.gz` | Brreg bulk register: phones, e-mails and sub-unit websites per company | NLOD 2.0 |

They hold candidates and screening counts, never published facts or
precomputed profiles, and are built over the whole public universe, not an
evaluation draw. A candidate website is fetched live and published only after
the identity checks; a row used as a source is cited by its dataset record
(`https://www.wikidata.org/wiki/<QID>#<property>` or
`overture:<release>:<place id>`) with its licence. The agent runs correctly
with every cache file deleted.

The optional `--registry-snapshot` (a Brreg bulk CSV) is used only if the live
Entity Register cannot be reached, and the evidence then names the bulk source.
