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
- `--bank-allocations`
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
  - one exact reviewed physical-bank receipt, linked to an invoice action or existing invoice ID
- `payments/create`
  - one exact reviewed physical-bank payment, linked to a purchase action or existing purchase ID

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

- only allocations with `review.status: approved` create cash actions
- each physical row (or each reviewed split part) gets a distinct stable action key and keeps the
  statement date, amount, currency, source reference, and mapped bank account
- generated targets may be current actions or successful prior submissions; existing IDs must be
  proven by a bound discovery overview
- processor payouts and purchase records remain supporting evidence, never settlement heuristics
- `clearing_transfer`, direct-sale, and bank-fee dispositions do not create surrogate business
  documents in this phase

## Current Blocking Rule

By default the script refuses to build a batch when `recon/<period>.json` does not approve the
month. `--force` exists only for explicit review of blocked months.

## Current Limits

- the draft payloads are reviewable action plans, not yet guaranteed submit-ready API bodies
- contact/client resolution is still a hint, not a deterministic mapping
- VAT and account ID suggestions rely on name/code heuristics from `entity_map.json`
- shipping VAT allocation remains a review item when shipping is split into its own draft line
