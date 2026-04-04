# Bookaudit Reference

## Purpose

This reference keeps the `bookaudit` skill lean. Use it when you need the concrete audit model,
scope rules, or current implementation limits.

## Main Entrypoint

- `scripts/bookaudit.py`
  - loads one normalized month or a year of monthly normalized artifacts
  - fetches final Simplbooks invoices, purchases, incomings, and payments for the audited scope
  - fetches invoice and purchase row detail for VAT and inventory-style checks
  - writes `audits/<period>.md`

## Scope Resolution

- `--period YYYY-MM` audits one month
- `--period YYYY` audits one full year
- Month audits load `normalized/<period>.json`
- Year audits aggregate `normalized/YYYY-*.json`
- Continuity review uses the previous month or previous year normalized artifacts when available

## Live Endpoint Usage

Current live data collection uses:

- `invoices/list` scanned client-side and included when either `created` or `transaction_date` falls inside scope
- `purchases/list` scanned client-side and included when either `created` or `transaction_date` falls inside scope
- `incomings/list` scanned and filtered client-side by `income_date`
- `payments/list` scanned and filtered client-side by `payment_date`
- `invoices/get/{id}` for invoice-row detail
- `purchases/get/{id}` for purchase-row detail

This stays aligned with the current Simplbooks quirks already observed in the repo.

## Current Audit Model

The implemented audit compares:

- normalized source totals vs live invoices, purchases, incomings, and payments
- normalized VAT vs live document VAT
- embedded processor-fee evidence on sales or payout rows vs live purchase totals when no explicit fee rows exist
- processor and fulfillment signals in bank or payout evidence vs live document families
- source inventory or warehouse signals vs live row-level article or warehouse signals
- current scope vs previous normalized scope for basic continuity warnings
- `created`, `transaction_date`, `income_date`, and `payment_date` vs `created_time` drift

The audit also emits deterministic spot-check notes over sampled live documents.

## Current Limits

- Continuity is currently source-driven; it does not yet fetch previous-period live Simplbooks state for a second independent comparison.
- Inventory reasonableness currently relies on row-level article and warehouse signals, not a confirmed remnant endpoint.
- Spot checks are deterministic samples, not cryptographically random draws.
- The audit recomputes from normalized artifacts and live state, but it does not re-parse raw PDFs or CSVs itself.
- Full client-side invoice and purchase scans may be slower on large historical companies because list filtering by `transaction_date` is not assumed from the API.
