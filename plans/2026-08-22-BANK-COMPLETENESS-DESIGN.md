# Bank Statement Completeness Design

Date: 2026-08-22
Status: approved in chat; awaiting written-spec review

## Purpose

Make physical bank-statement completeness a hard invariant of the SimplBooks workflow. A month must not become write-eligible while any physical statement row is absent from the accounting plan, duplicated across actions, or supported only by an unexplained heuristic match.

This design applies generically to `Example Company OÜ`. Company-specific classifications and source references remain in ignored `companies/<company>/` artifacts.

## Scope

The change covers:

- distinguishing physical bank rows from processor or supplier clearing-ledger movements;
- recording a reviewed disposition for every physical bank row;
- generating exact-date, exact-currency cash-settlement actions from approved dispositions;
- linking receipts and payments to generated or already-existing business documents;
- reconciling bank fees, netted correspondent fees, card-funded supplier wallets, direct bank sales, and manual invoices;
- blocking reconciliation, checking, and submission when coverage is incomplete;
- preserving chronological dependencies and freezing submitted action batches;
- replacing stale, generic review notes with evidence-specific findings.

The change does not add a generic journal endpoint, assume an undocumented SimplBooks endpoint, import a statement through the SimplBooks user interface, or permit live submission without the existing explicit approval controls.

## Core Invariants

### Physical bank statement

Every canonical physical statement row must have exactly one reviewed disposition. Its disposition must lead to one of:

1. an exact receipt action;
2. an exact payment action;
3. a verified match to an existing SimplBooks cash transaction; or
4. a reviewed transfer match whose accounting transaction is identified on both sides.

An unresolved, ignored, or merely inferred physical row blocks the month. A physical row referenced by more than one settlement group also blocks the month unless an explicit split allocation proves that the allocated amounts equal the row amount.

### Clearing ledgers

Processor balances and supplier wallets are not physical bank accounts. Their movements must be normalized separately and reconciled to physical card charges, bank payouts, expenses, refunds, and opening or closing balances.

A clearing movement may support a physical-bank disposition, but it must not be counted as an additional physical statement row or generate a duplicate bank transaction.

### Amount, date, currency, and identity

Cash-settlement actions use the statement row's business date, currency, amount, source account, and immutable source reference. Monthly-end dates are not permitted for physical cash rows.

Foreign-currency actions bind the reviewed annual ECB rate cache and retain the source-currency amount. When a receipt is net of a correspondent fee, the invoice settlement and fee allocation must reproduce both the invoiced gross amount and the amount that reached the bank.

### Posting eligibility

Write eligibility requires all of the following:

- reconciliation approves the month;
- physical bank coverage is complete and arithmetically exact;
- clearing-ledger reconciliation has no unexplained movement;
- `bookchecker` reports no errors and no unreviewed accounting warnings;
- the batch is explicitly approved and bound to the current checker report;
- discovery satisfies the existing freshness limit;
- no earlier required month remains unsubmitted;
- a submitted action YAML file is never regenerated or mutated.

## Artifact Model

### Normalized records

`bank_transactions` is reserved for canonical rows from a physical bank statement or CAMT source.

A new `clearing_transactions` category holds processor and supplier-wallet movements such as wallet funding, wallet refunds, processor balance adjustments, and payout bridge entries. Each record identifies its clearing account or provider in structured attributes.

Normalization de-duplicates alternative representations before either category is emitted. A processor export that describes a physical card charge is supporting evidence for the physical bank row, not another bank row.

### Reviewed bank allocation

Each company may provide ignored annual artifacts under:

`companies/<company>/artifacts/bank/<year>-allocations.json`

The shared schema and template define allocations keyed by canonical physical-bank `record_id`. Each allocation contains:

- disposition type;
- exact amount or split amounts;
- target document kind;
- generated action key or existing SimplBooks document ID where applicable;
- counterparty and posting-family hints;
- related source or clearing record references;
- reviewer decision and rationale.

Supported disposition families are deliberately narrow:

- `generated_invoice_receipt`;
- `existing_invoice_receipt`;
- `generated_purchase_payment`;
- `existing_purchase_payment`;
- `direct_sale_receipt`;
- `bank_fee_payment`;
- `clearing_transfer`;
- `reviewed_split`.

There is no generic `ignore` disposition. Unknown cases remain unresolved and block processing.

The allocation file carries a binding to the normalized year inputs so stale decisions cannot silently apply after source data changes.

## Matching And Classification

Rules may propose allocation candidates but may not approve them. Candidate generation uses, in descending order:

1. immutable invoice or payment reference;
2. exact existing SimplBooks document number, currency, and amount;
3. exact generated action identity and expected settlement amount;
4. exact counterparty, amount, and bounded date relationship;
5. descriptive text as a candidate hint only.

Ambiguous candidates remain unresolved. Fuzzy supplier text alone must not create a write-capable settlement.

Existing-document matches are bound to the discovery overview's SimplBooks ID and document identity. Generated-document matches are bound to action idempotency keys, including prior-period keys where settlement timing crosses months.

## Action Generation

### Receipts

One physical credit row normally creates one `incomings/create` action. It may settle:

- a generated sales summary;
- a previously submitted sales summary;
- an existing manual invoice discovered through the API; or
- a direct-sale invoice created for reviewed bank sales.

Multiple physical receipts may partially settle the same invoice. The action keeps only its own bank row as physical cash evidence; related sales or clearing records remain supporting references.

### Payments

One physical debit row normally creates one `payments/create` action. It may settle a generated or existing purchase. The payment carries the statement date rather than the purchase period end.

A physical debit covering several documents uses reviewed split allocations. Each generated payment references the same physical row plus its allocated amount, and the checker proves the split total equals the statement amount.

### Direct bank sales

Reviewed direct sales are grouped into the smallest sensible business document, normally one invoice per product/tax profile/month, while retaining one receipt per statement row. VAT is determined from the transaction date and reviewed sales policy, not from payer identity or bank text alone.

### Bank and card fees

Bank charges are represented as supported expense purchases and exact physical payments. A monthly supporting purchase may be settled by multiple fee rows, but each row remains separately represented. Required bank or card-provider contacts and mappings are master data and remain subject to the existing separate approval rule.

### Netted fees

When a foreign receipt is reduced before it reaches the account, the design records:

- settlement of the customer invoice at the supported gross amount;
- the net physical receipt at the amount shown on the statement; and
- a fee expense for the supported difference.

The combined accounting effect must reproduce the physical bank movement and close only the supported receivable amount.

### Processor fees

Itemized processor transaction history remains the calculation and clearing-reconciliation source. A monthly processor fee summary is posted once. Monthly supplier invoices are retained and manifested as supporting VAT/source documents but do not create duplicate purchase actions.

## Reconciliation

`bookrecon` adds two independent check groups.

### Physical bank coverage

For every physical row, report its disposition, allocated amount, target identity, and status. The check fails on:

- missing allocation;
- stale allocation binding;
- duplicate allocation;
- amount, currency, account, or date mismatch;
- unresolved target document;
- unexplained split residual;
- a transaction classified as physical bank evidence by more than one canonical source.

The period also reports opening movement, credits, debits, and closing movement totals so the action-derived net movement can be compared with the canonical statement movement.

### Clearing-account continuity

For each clearing provider and currency, reconcile opening balance plus inflows minus outflows to closing balance when the source supports balances. Without explicit balances, require all movements to participate in a reviewed bridge and report the limitation.

## Checker And Submission Defences

`bookchecker` independently resolves source references and proves:

- every physical bank record is covered exactly once;
- every settlement action references physical bank evidence;
- physical date, currency, and amount agree with the action;
- split allocations balance;
- existing and generated document targets resolve;
- no clearing record masquerades as a physical bank row;
- no unresolved or medium-confidence accounting decision remains in an approved batch.

`booksend` pre-validates the entire batch before its first write. It refuses a batch if its coverage proof, discovery binding, checker hash, approval state, or chronological predecessor state is invalid.

Live execution order is fixed:

1. refresh discovery;
2. build an unsubmitted month;
3. run checker;
4. review findings and set approved;
5. rerun checker to bind the approved YAML;
6. submit with explicit confirmation;
7. preserve the submitted YAML and submission result unchanged;
8. proceed to the next month.

All months of an earlier year are submitted before a later year when dependencies cross the year boundary.

## Warning Review And Documentation

Generic confidence warnings are replaced with actionable findings. A warning must identify the exact record, decision, and remaining judgment. Obsolete fallback-contact or shipping-review notes are removed when policy and evidence already resolve them.

Private readiness notes are regenerated from current artifacts and API verification. Previously submitted tax reports are not treated as truth sources.

## Error Handling

The pipeline stops before action generation when allocation input is malformed or stale. It stops before approval when coverage or clearing reconciliation fails. It stops before the first API write when any batch-wide invariant fails.

No automatic fallback converts an unresolved bank row into revenue or expense. Missing supporting evidence is reported with the source row identity and the exact decision required.

## Testing

Money-sensitive behavior is test-backed with fixtures covering:

- physical-bank versus clearing-ledger normalization;
- duplicate Printful wallet/card evidence;
- direct-sale receipts against one monthly invoice;
- receipts against discovered manual invoices;
- prior-period and prior-year invoice settlement;
- current- and prior-period purchase payments;
- multiple payments against one purchase;
- split payment allocations;
- netted foreign-currency fees and ECB-rate bindings;
- incomplete, duplicate, stale, and arithmetically invalid allocations;
- exact statement-date preservation;
- batch approval/hash/freshness requirements;
- rejection of regeneration after successful submission.

Integration verification regenerates both target years from source, confirms 100% physical-bank coverage, confirms clearing-ledger completeness, runs the complete test suite, and performs write-mode translation without sending live API requests.

## Success Criteria

The design is complete when:

- every physical statement row in each target year has exactly one valid accounting disposition;
- action-derived bank movements equal canonical statement movements by account and currency;
- processor and supplier clearing movements do not duplicate bank rows and reconcile completely;
- all business documents and settlements resolve in chronological order;
- no unresolved accounting warning remains;
- both full-year dry runs pass using fresh read-only discovery;
- exact translated API calls are available for final human review before any live submission.
