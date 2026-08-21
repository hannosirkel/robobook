# Bookrecon Reference

## Primary Script

- `scripts/bookrecon.py`

Required inputs:

- `--period`
- either `--company-dir` or `--normalized`

Optional inputs:

- `--policy-memo`
- `--entity-map`
- `--bank-allocations`
- `--previous-normalized`
- `--output`
- `--amount-threshold`
- `--quantity-threshold`

## Output Contract

Primary output:

- `companies/<company>/artifacts/recon/<period>.json`

The file follows `schemas/recon-period.schema.json` and includes:

- `approve_for_build`
  - whether downstream draft building should proceed
- `blocking_issue_count`
  - count of failing deterministic checks plus blocking exceptions
- `bank_coverage`
  - report-only physical-bank and clearing write-readiness summary; `coverage_ready` is not the legacy build gate
- `checks`
  - deterministic reconciliation results with evidence refs
- `exceptions`
  - imported prep exceptions plus recon-specific missing-evidence exceptions

## Current Check Semantics

### Woo Sales Vs Processor Gross

- compares Woo-derived `sales` gross totals against processor-side `sales` gross totals
- only runs when both sides exist
- fails when the absolute delta exceeds `--amount-threshold`

### Processor Payouts Vs Bank Receipts

- runs per inferred processor such as `paypal` or `stripe`
- compares normalized `payouts` against positive bank transactions whose text matches that processor
- fails on amount mismatch or when payout rows exist without a matching bank receipt
- warns when bank receipts exist but payout rows are missing

### Processor Settlement Bridge

- runs per inferred processor
- compares same-month net cash from `sales`, `refunds`, and `fees` against normalized `payouts`
- warns instead of failing on large deltas because processor balances can roll across months
- this is the current deterministic approximation of refund-vs-payout-deduction checking

### Fulfillment Totals Vs Bank Debits

- compares partner-tagged `purchase_expenses` rows against bank debits tagged to the same fulfillment partner
- currently recognizes `printful`, `shipmonk`, and `omnipack` by text heuristics
- warns on missing evidence or same-month mismatches because payment timing can vary

### Inventory Quantity Evidence

- uses explicit quantity fields only
- compares sales quantity against inventory-movement quantity when both exist
- otherwise falls back to fulfillment-side quantity evidence when available
- warns when inventory seems relevant from `policy_memo.md` or `entity_map.json` but comparable quantity evidence is absent

### Continuity With Previous Period

- auto-loads the prior month’s normalized file when it is present next to the current one
- compares canonical source-system coverage between months
- warns when a previously seen source system disappears

### Physical Bank Coverage

- loads the current period's approved allocations from an annual, source-bound allocation artifact
- compares immutable statement identity, record locator, period, signed amount, currency, and physical `(IBAN, currency)` ledger
- totals credits, debits, and net movement per ledger; when CAMT opening and closing balances are both present, verifies opening plus movement equals closing
- missing, duplicate, stale, malformed, or incomplete allocation evidence produces `physical-bank-coverage: warn` and `bank_coverage.coverage_ready: false` in Phase A

### Clearing Continuity

- groups `clearing_transactions` by structured `clearing_provider`, `clearing_account`, and currency
- requires each movement to be referenced by a reviewed allocation or a normalized bridge reference
- when opening and closing clearing balances are present, verifies opening plus movements equals closing; otherwise the check records the precise missing evidence
- unresolved clearing emits `clearing-continuity:<provider>:<currency>: warn` and makes report-only bank write readiness false

## Current Blocking Rules

The current implementation blocks build when:

- imported normalized exceptions are already blocking
- bank receipts indicate processor activity but no processor-side export was normalized
- a deterministic hard check fails

Physical-bank coverage and clearing continuity warnings are intentionally excluded from this list in Phase A. They are decision-list evidence for later write-capable validation, so callers must not infer bank write readiness from `approve_for_build`.

## Current Limits

- processor inference is keyword-based and currently focused on `paypal` and `stripe`
- fulfillment inference is keyword-based and currently focused on a small partner set
- payout timing can cross period boundaries, so settlement-bridge warnings still need human review
- inventory quantity checks only use explicit quantity-bearing rows; aggregate sales rows without item quantities remain limited evidence
