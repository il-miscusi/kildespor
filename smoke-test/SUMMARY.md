# 100-company smoke test

Code commit: `48551ac296578b5566e4787b64f3bff1dd74bb1a` (this folder was added in the following commit; it changes no code).

Run on a Kaggle CPU machine: the repository at that commit in a fresh directory, an empty
virtualenv, `pip install -r requirements.txt`, the unit tests, then the documented run command
on 100 organisation numbers drawn at random (seed
20261017) from the public 411,160-company universe (`smoke-companies.jsonl`).
The same batch was then run a second time in the same folder to check refresh. To fit Kaggle's
script size limit, the per-company cache files in `kildespor/data/` were cut to the rows for these
100 companies and the companies sharing a phone or e-mail domain with them; lookups for these
companies return exactly what the full files return.

## Contract

- Inputs: 100; envelopes emitted: 100; validation: **passed**
- Checks: exact_expected_count = True, same_order_and_membership = True, unique_organisation_numbers = True, all_states_terminal = True, every_available_claim_has_evidence = True, every_evidence_id_resolves = True, every_external_claim_span_in_source = True
- Envelope states: {'available': 100}
- Wall clock: 73.3 s; requests: 1010 (10.1 per company); request latency p50 854 ms, p95 3718 ms
- Third-party cost: $0.00; language-model calls: 0
- Evidence-backed claims: 7040; evidence records: 7062

## Coverage (companies with at least one evidence-backed fact)

| area | companies |
|---|---|
| legal_identity | 100 / 100 |
| public_brand | 13 / 100 |
| annual_accounts | 100 / 100 |
| leadership | 100 / 100 |
| workplaces | 71 / 100 |
| official_website | 13 / 100 |
| company_profiles | 8 / 100 |
| contact_channels | 30 / 100 |
| hiring | 0 / 100 |
| public_activity | 100 / 100 |

| external fact | companies | facts |
|---|---|---|
| official_website | 13 | 13 |
| social_profile | 8 | 19 |
| hiring_signal (all) | 0 | 0 |
| hiring_signal: job ad | 0 | 0 |
| hiring_signal: careers page | 0 | 0 |
| dated_news | 3 | 29 |

## Refresh re-run

- Second run `smoke-100-b` against the first run's snapshot: 100 envelopes, validation passed
- Changes reported: 0 (same sources, so none expected: the refresh is idempotent)

## Files

- `envelopes.jsonl`: one terminal envelope per input
- `run-report.json`, `refresh-report.json`: machine-readable run reports
- `index.html` and `site/`: the viewer for this run. Open `index.html` (the research page) in a browser, or serve the folder with `python -m kildespor.serve --dir smoke-test`; pages sit at `site/research/`, `site/companies/<org>/`, `site/compare/?orgs=a,b` and `site/refresh/` (the old `#/` routes still work)
