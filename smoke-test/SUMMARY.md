# 100-company smoke test

Code commit: `fa5939efe813718b9a90b38f176d79dcde418838` (this folder was added in the following commit; it changes no code).

Run on a clean Kaggle CPU machine: fresh checkout of that commit, `pip install -r requirements.txt`,
then the documented run command on 100 organisation numbers drawn at random (seed
20261017) from the public 411,160-company universe (`smoke-companies.jsonl`).
The same batch was then run a second time in the same folder to check refresh.

## Contract

- Inputs: 100; envelopes emitted: 100; validation: **passed**
- Checks: exact_expected_count = True, same_order_and_membership = True, unique_organisation_numbers = True, all_states_terminal = True, every_available_claim_has_evidence = True
- Envelope states: {'available': 100}
- Wall clock: 46.7 s; requests: 830 (8.3 per company); request latency p50 479 ms, p95 1726 ms
- Third-party cost: $0.00; language-model calls: 0
- Evidence-backed claims: 7000; evidence records: 7000

## Coverage (companies with at least one evidence-backed fact)

| area | companies |
|---|---|
| legal_identity | 100 / 100 |
| public_brand | 9 / 100 |
| annual_accounts | 100 / 100 |
| leadership | 100 / 100 |
| workplaces | 70 / 100 |
| official_website | 9 / 100 |
| company_profiles | 5 / 100 |
| contact_channels | 28 / 100 |
| hiring | 3 / 100 |
| public_activity | 100 / 100 |

| external fact | companies | facts |
|---|---|---|
| official_website | 9 | 9 |
| social_profile | 5 | 13 |
| job_posting | 0 | 0 |
| careers_page | 3 | 4 |
| company_news | 1 | 10 |

## Refresh re-run

- Second run `smoke-100-b` against the first run's snapshot: 100 envelopes, validation passed
- Changes reported: 0 (same sources, so none expected: the refresh is idempotent)

## Files

- `envelopes.jsonl`: one terminal envelope per input
- `run-report.json`, `refresh-report.json`: machine-readable run reports
- `site/index.html`: the viewer for this run (open it in a browser)
