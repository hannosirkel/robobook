# Bookbuilder Reference

## Primary Script

- `scripts/bookbuilder.py`

Required inputs:

- `--period`
- either `--company-dir` or `--normalized`

Optional inputs:

- `--recon`
- `--policy-memo`
- `--entity-map`
- `--company-profile`
- `--output`
- `--force`

## Output Contract

Primary output:

- `companies/<company>/artifacts/actions/<period>.yaml`

The file follows `schemas/action-batch.schema.json` and includes:

- batch metadata
- `recon_ref`
- one or more draft actions with source refs, reasons, confidence, and review notes

## Current Draft Shapes

The current implementation keeps the target endpoints concrete while leaving uncertain company
mapping choices explicit inside the draft payload:

- `invoices/create`
  - monthly sales summaries
  - monthly refund / credit-note summaries
- `purchases/create`
  - processor fee summaries
  - purchase-expense summaries
- `incomings/create`
  - payout settlement summaries
- `payments/create`
  - bank-payment summaries linked to purchase summaries

Each payload currently carries a `draft_schema` marker such as `invoice_summary_v1`,
`purchase_summary_v1`, or `cash_settlement_v1`.

## Current Heuristics

### Sales And Refunds

- grouped by channel or source system
- shipping is split into a dedicated draft line when shipping evidence exists and the action does
  not mix taxable and non-taxable records
- mixed tax profiles stay in separate revenue lines instead of forcing one VAT assumption

### Fees

- explicit fee rows are preferred when they exist
- otherwise the builder derives fee totals from embedded `fee_amount` values on sales or payout
  records

### Purchases

- purchase-expense rows are grouped by fulfillment partner or source system
- taxable and non-taxable costs are kept in separate draft lines

### Cash Actions

- payout rows produce draft `incomings/create` actions
- partner-tagged bank debits can produce draft `payments/create` actions when there is a matching
  purchase-summary action
- bank account ID selection prefers `company_profile.json`, then falls back to `entity_map.json`
  heuristics

## Current Blocking Rule

By default the script refuses to build a batch when `recon/<period>.json` does not approve the
month. `--force` exists only for explicit review of blocked months.

## Current Limits

- the draft payloads are reviewable action plans, not yet guaranteed submit-ready API bodies
- contact/client resolution is still a hint, not a deterministic mapping
- VAT and account ID suggestions rely on name/code heuristics from `entity_map.json`
- shipping VAT allocation remains a review item when shipping is split into its own draft line
