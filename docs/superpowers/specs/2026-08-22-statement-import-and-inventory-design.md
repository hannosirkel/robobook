# Statement Import and Inventory Completion Design

Date: 2026-08-22

## Purpose

Complete Example Company OÜ bookkeeping without losing any physical bank-statement row, duplicating cash through the SimplBooks API, or posting unsupported inventory quantities or warehouses.

This reusable design replaces API-first bank cash posting with a full-statement-import workflow. Company-specific dates, counts, identifiers, names, account mappings, card ownership, and warehouse decisions belong only in the ignored company workspace.

## API Boundary

The published SimplBooks API supports invoices, purchases, document-linked receipts and payments, warehouses, and read-only inventory remnants. It does not publish generic journal-entry, bank-statement-import, stock-transfer, stock-adjustment, financial-account creation, or cash-account creation endpoints.

Consequently:

- invoices, purchases, supplier credits, and supported processor-account settlements remain API candidates;
- physical bank statements are imported and handled in the SimplBooks user interface;
- ledger-account creation, historical warehouse transfers, and inventory adjustments are manual operations with deterministic instructions;
- read-only discovery and audit remain API-driven wherever the API exposes the resulting state.

References:

- <https://app.simplbooks.com/api-documentation/>
- <https://app.simplbooks.com/api-documentation/oas/api.yaml>

## Posting Architecture

The posting policy gains a `statement_import` cash mode. In that mode:

1. Complete bank statements are imported into SimplBooks.
2. The pipeline creates accounting documents through the API but creates no receipt or payment against a configured statement-import bank account.
3. The pipeline emits an annual statement-assignment manifest covering every physical row.
4. Document-related rows are matched to their created invoice or purchase.
5. General-ledger rows receive reviewed debit and credit account instructions.
6. Netted rows receive an exact balanced split instruction.
7. Post-import ledger exports and fresh SimplBooks discovery prove the resulting state.

`booksend` fails before client construction if a prohibited bank incoming or payment remains. Processor-account actions are allowed only when they use an explicitly reviewed non-bank account and satisfy the existing document, currency, source, and evidence gates.

## Statement Assignment Artifact

The pipeline generates one canonical JSON plan, one CSV, and one Markdown checklist per year. Each physical row includes:

- immutable statement identifier;
- source path and SHA-256 binding;
- IBAN, currency, business date, and signed amount;
- payer/payee and description;
- reviewed disposition and UI operation;
- debit and credit financial-account roles and resolved IDs;
- invoice or purchase target where applicable;
- ECB rate and conversion calculation where applicable;
- split parts and balancing equation where applicable;
- completion state and immutable evidence binding.

The key is `(immutable statement identifier, IBAN, currency)`, never source row number. Duplicate claims, missing rows, extra rows, changed source hashes, and unbalanced splits are errors. Non-bank clearing movements use a separate adjustment checklist and never count as physical statement coverage.

## Financial Account Policy

The reusable contract names roles instead of hard-coding one company's chart:

| Family | Required account role |
| --- | --- |
| Processor payout | Processor clearing to bank |
| Bank card/transfer fee | Bank-fee expense |
| Employee reimbursement | Reporting-person payable |
| Personal-card wallet funding | Platform prepayment against reporting-person payable |
| Wallet refund | Reverse the bound funding movement |
| Failed internal funding and return | Processor clearing; reviewed pair nets to zero |
| Customer receipt | Match invoice and settle customer receivable |
| Supplier payment | Match purchase and settle supplier payable |
| Netted foreign-currency receipt | Gross receivable settlement plus correspondent fee |
| Settlement FX difference | Reviewed realized FX gain or loss account |

The company-local policy supplies exact SimplBooks IDs. Missing roles block plan generation. The workflow must discover and bind newly created IDs before producing write-eligible documents; it never substitutes a similar account.

## Currency Policy

The configured annual Frankfurter/ECB rate file is authoritative. Each applicable settlement carries the reviewed single rate for its transaction date and currency, the converted amount, deterministic rounding, and an immutable rate-file binding. Realized FX and correspondent bank fees remain distinct.

Statement import removes foreign-currency bank cash from the API write surface, eliminating the need for a partial cash-action executor.

## Sales Document and Warehouse Policy

Every inventory-bearing goods line carries an exact article, positive quantity, complete contributor set, and warehouse selected from company policy plus source facts.

The policy supports processor or fulfillment-partner warehouses, a configurable inclusive order-number boundary between fulfillment methods, a reviewed direct-sale warehouse, and a reviewed distributor warehouse. Records without the required order number, quantity, or warehouse evidence block. Contributors on opposite sides of a warehouse boundary cannot be grouped into one inventory line.

Direct bank sales use one invoice per physical receipt for one-to-one statement matching, removing the multiple-receipt API dependency.

## Inventory and COGS

Before affected sales are posted, the workflow captures dated remnants around every manual historical warehouse transfer.

After each year, read-only audit evaluates, per article and warehouse and in aggregate:

`opening + evidenced purchases + transfers in - transfers out - posted sales - write-offs +/- adjustments = closing`

Article-bearing invoice rows are expected to drive stock relief and COGS, but the audit verifies that behavior. If the resulting closing differs from the reviewed inventory-count source, the workflow emits a non-executable manual adjustment instruction containing date, article, warehouse, direction, quantity, and inventory-change account. The adjustment requires separate approval and another fresh remnant check.

Completed historical inventory actions remain excluded through immutable completion evidence.

## Evidence Model

Before import, the pipeline binds canonical statement hashes, normalized artifacts, reviewed allocations, annual plans, API-created document IDs, and final action-batch hashes.

After import, supported SimplBooks account-ledger exports are copied to immutable evidence paths and SHA-256 bound. The audit independently compares company identity, account, transaction identity, business date, currency, amount, direction, and document target. Unsupported free-form assertions remain blocking.

Final acceptance requires row coverage, statement movement, document balances, clearing balances, and inventory equations to reconcile. A zero aggregate difference is insufficient when individual rows are missing or duplicated.

## Live Run Order

1. Canonicalize and hash new source evidence.
2. Refresh read-only SimplBooks discovery.
3. Complete separately approved master-data prerequisites and refresh discovery.
4. Record and verify any required historical warehouse transfer.
5. Regenerate normalized data, allocations, plans, reconciliation, document batches, and checks.
6. Import complete statements as unmatched transactions.
7. Create supported documents chronologically, never regenerating submitted months.
8. Match document rows and assign exceptional rows from the plans.
9. Export ledgers and run independent cash/document audit.
10. Reconcile inventory and process any separately approved correction.
11. Refresh discovery and rerun checks until errors and warnings are zero.
12. Only then mark statutory reporting ready.

The workflow is resume-safe. Submitted document actions are immutable; imported rows use immutable statement identity and completion evidence, preventing duplicate API cash movements.

## Empty-Month Inventory Warning

Inventory-quantity warnings require actual inventory-affecting activity in the period. General warehouse configuration alone does not justify a warning. An empty month with no sales, refunds, purchases, transfers, write-offs, adjustments, statement rows, or actions passes.

## Validation and Failure Behavior

Hard blockers include missing, duplicate, ignored, or extra physical rows; prohibited bank cash API actions; unreviewed processor accounts; plan identity/economic/source mismatches; unbalanced splits; stale ECB evidence; missing document targets; missing inventory facts; distributor documents generated before warehouse/transfer proof; unsupported post-import evidence; unexplained ledger differences; inventory-equation mismatches; and any final checker finding.

Every error identifies the exact statement key or source contributor and corrective action. No fallback assigns tax, quantity, warehouse, document, or account from gross amount or fuzzy text alone.

## Test Strategy

Automated coverage includes statement and wallet parsing; immutable statement allocation; financial-account roles; processor clearing; netted foreign-currency equations; configurable warehouse boundaries; exact quantity/contributor proof; direct-sale one-to-one invoices; transfer prerequisites; inventory equations; annual movement equality; sender zero-call rejection; plan idempotency; submitted-document immutability; empty-month behavior; ledger evidence; and final audit.

Focused tests are followed by the full repository suite, schema parsing, Python compilation, CLI help checks, `git diff --check`, and privacy review.

## Success Criteria

The workflow is ready only when every physical row has exactly one reviewed assignment; statements cannot duplicate API cash; every document batch passes; every exceptional row has an exact account or split instruction; clearing movements reconcile; master-data and transfer evidence are proven; every inventory sale has exact article, quantity, and warehouse evidence; post-import ledgers reconcile; post-posting inventory reconciles; final checker findings are zero; and the runbook uses no undocumented API behavior.
