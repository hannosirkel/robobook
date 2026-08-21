# Bookprep Reference

## Primary Script

- `scripts/bookprep.py`

Required inputs:

- `--company-dir`
- `--period`

Optional inputs:

- `--source-dir`
- `--base-currency`
- `--output`

## Output Contract

Primary output:

- `companies/<company>/artifacts/normalized/<period>.json`

The file follows `schemas/normalized-period.schema.json` and includes:

- `sources`
  - the period source manifest
- `records`
  - normalized records grouped by category
- `exceptions`
  - parsing gaps, unsupported canonical sources, and non-lossless normalization warnings

## Canonical Source Selection

The current implementation chooses one canonical source per duplicate group based on source type
priority:

1. CSV
2. XML
3. XLSX
4. JSON/manual structured exports
5. PDF

`.gsheet` files are ignored entirely because they are accountant work files rather than source
system exports.

## Current Parser Rules

### Woo CSV

- expects daily aggregate columns such as `Date`, `Gross sales`, `Net sales`, `Taxes`, `Shipping`, and `Total sales`
- produces `sales` records
- emits warnings when returns are embedded in aggregate rows because refund-level rows are not available
- recognizes annual tax-summary columns `Tax code`, `Rate`, `Total tax`, `Order tax`, `Shipping tax`, and `Orders`
- validates country-coded tax rows, non-negative rates and amounts, positive integral order counts, and component totals
- produces annual `other` records with `event_type: woo_tax_summary`; these are supporting evidence only and never duplicate sales
- emits the evidence only in the period containing the covered year's final day

### PayPal CSV

- uses transaction rows with `Gross`, `Fee`, `Net`, and transaction metadata
- classifies rows into `sales`, `refunds`, `payouts`, or `fees` using conservative type heuristics

### Stripe Balance History CSV

- uses `Type`, `Amount`, `Fee`, `Net`, `Created (UTC)`, and `Available On (UTC)`
- classifies rows into `sales`, `refunds`, `fees`, or `payouts`
- skips duplicate balance-transaction IDs with warnings

### Printful Billing CSVs

- `Orders.csv`
  - nets same-period completed and refunded rows by `Printful ID`
  - emits `purchase_expenses` only for positive in-period net charges
  - warns and skips refund-overage groups that would become negative purchase-expense rows
- `Wallet.csv`
  - maps `Deposit to wallet` to negative `bank_transactions`
  - maps `Withdrawal from wallet` to positive `bank_transactions`
- `Other.csv`
  - parses category-based monthly service charges into `purchase_expenses`
- `Services.csv`
  - parses recognized service rows into `purchase_expenses`
  - preserves recognized row currency so downstream matching can stay currency-specific

### Bank CSV

- parses transaction rows into `bank_transactions`
- preserves counterparty and reference metadata in `attributes`

### CAMT XML

- parses `<Ntry>` entries into `bank_transactions`
- uses `BookgDt` and `CdtDbtInd`

### PDF Invoices

- uses `pypdf` when available to extract text from text-based PDFs
- parses Stripe monthly fee invoices into explicit `fees` records keyed by service month
- parses Printful monthly VAT reports and in-period storage invoices into `purchase_expenses`
- skips the duplicate Printful monthly-summary PDF record when overlapping `Orders.csv` evidence exists for the same period, while still keeping invoice-only PDF rows such as storage invoices
- parses readable supplier invoice PDFs into `purchase_expenses`
- preserves `page_ref` values in source references

## Current Limits

- scanned or image-only PDFs will remain blocked by exceptions until OCR or a structured export is provided
- `.gsheet` accountant work files are ignored and will not appear in the manifest.
- Woo daily aggregates do not let the current parser split refunds without risking double counting.
