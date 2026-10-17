# Envelope format (`kildespor.envelope.v1`)

One JSON object per line in `envelopes.jsonl`, one line per input, in input
order.

```json
{
  "schema_version": "kildespor.envelope.v1",
  "organisation_number": "938702675",
  "state": "available",
  "run": {"run_id": "...", "agent": "kildespor", "agent_version": "1.0.0", "started_at": "...", "completed_at": "...", "terminal_status": "completed", "time_budget_s": 900, "input_count": 100},
  "legal_identity": {"organisation_number": "...", "legal_name": "...", "public_brand": "...", "legal_form": {"code": "AS", "description": "Aksjeselskap"}, "business_address": "...", "municipality": "...", "industry": {"code": "...", "description": "...", "rank": 1}, "registered_employees": 12, "registry_status": "active", "official_website": "https://..."},
  "sections": {
    "legal_identity_and_brand": {"state": "available", "families": {"legal_identity": {"state": "available", "reason": "...", "checked_sources": [], "claim_count": 21}, "public_brand": {...}}},
    "annual_accounts": {...},
    "leadership_and_workplaces": {...},
    "website_and_profiles": {...},
    "hiring_and_activity": {...}
  },
  "claims": [
    {"id": "cl-...", "key": "financial.revenue:[\"revenue\",\"SELSKAP\",\"2025-12-31\"]", "family": "annual_accounts", "field": "financial.revenue",
     "value": {"amount": 914000000.0, "currency": "NOK", "account_type": "SELSKAP", "fiscal_year": "2025"},
     "availability": "available", "confidence": 0.99, "evidence_ids": ["ev-..."], "extraction_method": "official_register",
     "reporting_period": "2025-01-01/2025-12-31", "label": "Operating revenue",
     "first_observed_at": "...", "last_observed_at": "..."},
    {"id": "na-...-hiring", "family": "hiring", "field": "hiring", "value": null, "availability": "not_available",
     "reason": "no active ad in NAV's vacancy feed (last 45 days) names this organisation number or its subunits as employer", "checked_sources": ["..."], "evidence_ids": []}
  ],
  "source_snapshots": [
    {"id": "snap-...", "url": "https://data.brreg.no/regnskapsregisteret/regnskap/938702675", "requested_url": "...", "retrieved_at": "...", "content_sha256": "...", "http_status": 200, "source_class": "official_annual_accounts", "stored_body": "state/snapshots/ab/abcd....gz"}
  ],
  "evidence": [
    {"id": "ev-...", "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/938702675", "source_class": "official_annual_accounts",
     "retrieved_at": "...", "content_sha256": "...", "claim_span": "SELSKAP 2025 driftsinntekter/sumDriftsinntekter = 914000000.0",
     "extraction_method": "brreg_accounts_api", "reporting_period": "2025-01-01/2025-12-31", "http_status": 200, "access_policy": "NLOD 2.0 open data, data.brreg.no", "snapshot_path": "state/snapshots/ab/abcd....gz"}
  ],
  "synthesis": {"headline": "...", "sections": [{"heading": "What the company does", "text": "...", "sentences": [{"text": "...", "claim_ids": [], "evidence_ids": []}], "claim_ids": [], "evidence_ids": []}], "signals": [], "unknowns": [], "method": "..."},
  "changes": [{"change_type": "new_job", "field": "job_posting", "previous": null, "current": {...}, "previous_evidence_ids": [], "current_evidence_ids": ["ev-..."], "previous_evidence": [], "materiality": "medium", "summary": "...", "detected_at": "..."}],
  "refresh": {"previous_run_id": null, "baseline": true, "history": ["..."]},
  "errors": [{"module": "website", "error": "...", "url": "...", "at": "..."}],
  "operations": {"requests": 14, "runtime_ms": 8120, "third_party_cost_usd": 0.0, "agent_version": "1.0.0"}
}
```

## Field families and fields

| family | fields |
|---|---|
| `legal_identity` | `organisation_number`, `legal_name`, `legal_form`, `registered_business_address`, `postal_address`, `municipality`, `industry_code`, `registered_employees`, `founded_date`, `registered_date`, `share_capital`, `statutory_purpose`, `registered_activity`, `vat_registered`, `in_business_register`, `part_of_group`, `registry_status`, `former_name`, `group_relationship`, `registry_website` (the website string as registered, unverified) |
| `public_brand` | `public_brand_name`, `self_description` |
| `annual_accounts` | `latest_filed_accounts_year`, `financial.<metric>` for revenue, operating_expenses, operating_result, net_financial_items, profit_before_tax, tax_expense, annual_result, total_assets, fixed_assets, current_assets, equity, total_debt, short_term_debt, long_term_debt; `financial.audit_opt_out` |
| `leadership` | `role` |
| `workplaces` | `registered_workplace` |
| `official_website` | `official_website` (verified: identity method and discovery route in the value) |
| `company_profiles` | `social_profile` (facebook, instagram, linkedin, youtube, x, tiktok, pinterest, vimeo), from the verified site, the register's website field, or the employer block of an org-number-verified NAV ad |
| `contact_channels` | `registry_phone`, `website_phone`, `website_email` |
| `hiring` | `job_posting`, `careers_page` |
| `public_activity` | `company_news`, `registry_role_change`, `registry_update` |

Claims carried forward from a previous run because their source failed this
time have `"stale": true`. Financial years that have rolled out of the
accounts API's window are kept with `"historical": true`.
