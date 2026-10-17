# Sources, rights, secrets and outbound URL policy

## Sources and access rights

| source | used for | access | rights |
|---|---|---|---|
| `data.brreg.no/enhetsregisteret/api` (entities, roles, subunits, group structure, update feed) | identity, leadership, workplaces, group links, register activity | official open API, no key | NLOD 2.0 |
| `data.brreg.no/regnskapsregisteret/regnskap` | filed annual accounts | official open API, no key | NLOD 2.0 |
| company websites (register-listed or verified by organisation number) | brand, self-description, contacts, profiles, careers page, JSON-LD jobs and articles, RSS/Atom | static HTTP GET of a few pages per site; robots.txt honoured | company-owned material, cited with source and date |
| `arbeidsplassen.nav.no` | active job ads | the public search endpoint the site itself uses, and the public ad page; robots.txt: `Allow: /` for all agents | public job ads published by NAV |

Not used: search engines, LinkedIn, Meta (Facebook/Instagram), Glassdoor,
Indeed, Google, review sites, or any unofficial client for them. A profile on
those platforms appears only as an outbound link from a verified company site.
`w2.brreg.no/kunngjoring/` is not fetched because its robots.txt disallows it.

## Rate and politeness

- User-Agent `KildesporBot/<version> (+https://github.com/il-miscusi/kildespor; company-research agent)`.
- robots.txt is fetched once per origin and honoured for every page.
- Concurrency limits per host: data.brreg.no 10, arbeidsplassen.nav.no 4, any
  other host 2. Retries back off on 429/5xx.
- Per company, at most a handful of pages per website are read (home, up to
  three contact/about/privacy pages, one careers page, one feed).

## Secrets

The agent needs none. It reads no environment secrets and writes no
credentials. Optional settings are plain environment variables:
`KILDESPOR_TIME_BUDGET_S`, `KILDESPOR_WORKERS`, `KILDESPOR_STATE_DIR`,
`KILDESPOR_REGISTRY_SNAPSHOT`, `KILDESPOR_RUN_ID`.

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
are recorded, never fetched.

## Caches

No external cache is used. The optional `--registry-snapshot` (a Brreg bulk
CSV) is used only if the live Entity Register cannot be reached, and the
evidence then names the bulk source.
