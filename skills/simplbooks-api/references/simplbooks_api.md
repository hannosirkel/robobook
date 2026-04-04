# Simplbooks API Reference

## Purpose

This reference keeps the `simplbooks-api` skill lean. Use it when you need endpoint reminders,
wrapper behavior, or repo-specific command patterns.

## Shared Scripts

- `scripts/simplbooks_api.py`
  - authenticated direct calls
  - paginated list requests
  - company ID lookup from `companies/<company>/METADATA.md`
  - throttling below the published `60 requests/minute` limit
  - optional per-attempt JSONL request logs with `--request-log`
  - retry/backoff for transient `429` and `5xx` transport failures
- `scripts/examine_simplbooks_year.py`
  - read-only year summary
  - counts, monthly totals, and row-level account/VAT/article patterns
  - intended for discovery outputs under `companies/<company>/artifacts/discovery/`

## Authentication And Company Resolution

- Auth header: `X-Simplbooks-Token`
- Base URL: `https://app.simplbooks.com/{company_id}/api`
- Token source: `.apikey` by default, or `SIMPLBOOKS_API_TOKEN`
- Company ID source order:
  1. explicit CLI `--company-id`
  2. `SIMPLBOOKS_COMPANY_ID`
  3. `companies/<company>/METADATA.md`

## Current Wrapper Behavior

`scripts/simplbooks_api.py` currently supports:

- `call <path>`
- `paginate <path>`
- `--get-mode body`
- `--get-mode query`
- `--request-log`
- `--max-attempts`
- `--retry-backoff-seconds`

The wrapper injects these headers:

- `Accept: application/json`
- `X-Simplbooks-Token: ...`
- `X-Output-Format: JSON`
- `Content-Type: application/json` for payload-bearing requests
- `X-Input-Format: json` for payload-bearing requests

Returned JSON is augmented with:

- `_http_status`
- `_request_url`
- `_request_method`

## Practical Endpoint Map

High-value read-only endpoints already used or expected by the plan:

- `financial_accounts/list`
- `financial_accounts/get/{id}`
- `income_accounts/list`
- `vat_types/list`
- `warehouses/list`
- `invoices/list`
- `invoices/get/{id}`
- `purchases/list`
- `purchases/get/{id}`
- `incomings/list`
- `payments/list`

Likely discovery targets for later skills:

- items/articles endpoints
- remnant or warehouse-balance endpoints
- contact/client endpoints

## Known API Quirks

- Some list endpoints are documented as `GET` while also describing request bodies.
- Response wrappers are inconsistent between endpoints.
- List filters are not proven consistent across all endpoints.
- `created`, `transaction_date`, `income_date`, and `payment_date` can each matter depending on document type.
- `created_time` should not drive accounting-period logic.
- Document numbers are not reliable period ordering signals.

## Command Patterns

Try a filtered `GET` with the default body mode first:

```bash
python3 scripts/simplbooks_api.py \
  --company-dir companies/example \
  paginate purchases/list \
  --payload '{"created_from":"2024-01-01","created_until":"2024-01-31"}'
```

If the endpoint appears to ignore filters, retry with query mode:

```bash
python3 scripts/simplbooks_api.py \
  --company-dir companies/example \
  --get-mode query \
  paginate purchases/list \
  --payload '{"created_from":"2024-01-01","created_until":"2024-01-31"}'
```

## Artifact Expectations

Use the shared contracts when an API task feeds later skills:

- `schemas/company-profile.schema.json`
- `schemas/entity-map.schema.json`
- `schemas/action-batch.schema.json`
- `schemas/submission-log.schema.json`

Starter files live under `templates/`.

## Current Gaps

Remaining shared-layer gaps are narrower now:

- draft-to-live document translation currently lives in `booksend`, not in a standalone shared write-wrapper module
