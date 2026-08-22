# Skill Plan For Simplbooks Bookkeeping Skills

## Goal

Create a small set of Codex skills that can help prepare, review, submit, and audit month-by-month bookkeeping in Simplbooks for one or more companies. The generic example company used in this plan is `Example Company OÜ`.

The system should optimize for:

- low yearly volume and low materiality
- human approval before any write
- month-by-month execution
- traceability from every draft action back to source data
- "reliable enough with review", not blind full autonomy

This file lives in `plans/` because it is a build/design plan, not an operator-facing artifact.

## Repository Layout

The plan assumes this repository structure:

- `plans/`
  - planning and design documents, including this file
- `scripts/`
  - reusable Python logic for deterministic tasks such as API access, parsing, normalization, reconciliation, and validation
- `temp/`
  - optional scratch intake area for ad hoc review; gitignored and not canonical storage for company source data
- `companies/<company>/`
  - company-specific bookkeeping material
- `companies/<company>/METADATA.md`
  - lightweight company descriptor used by skills to identify the company and basic local conventions
- `companies/<company>/source/`
  - raw bookkeeping inputs for that company and period
- `companies/<company>/artifacts/`
  - derived outputs such as policy memos, normalized data, reconciliations, action files, submissions, and audits
- `companies/<company>/artifacts/discovery/`
  - read-only API examination outputs such as year summaries and pattern scans
- `.apikey`
  - local Simplbooks API token file, not committed to git

`companies/` should be ignored by git by default, with `companies/example/` explicitly allowed so the repository can include a reusable template if published. Real company folders should remain ignored.

Recommended `METADATA.md` role:

- carry the standardized core fields:
  - company name
  - company slug
  - Simplbooks `company_id`
  - VAT registration status
  - short description
- allow additional non-secret fields only when they are useful for the local workflow
- stay intentionally lightweight
- avoid storing secrets there; `.apikey` remains the token source

## Recommendation

The overall approach is reasonable. It is not too many steps.

Keep the split between discovery, source preparation, draft building, checking, sending, and auditing. The only change worth making is to add an explicit reconciliation skill:

- `simplbooks-api`
- `bookdisco`
- `bookprep`
- `bookrecon`
- `bookbuilder`
- `bookchecker`
- `booksend`
- `bookaudit`

Without `bookrecon`, the pipeline can produce outputs that look internally clean but are still wrong because Woo, Stripe, bank, and fulfillment reports do not line up.

## Scope Assumptions

- Business: Estonian OÜ, description in `companies/<company>/METADATA.md`.
- Source systems: bank statements, WooCommerce sales reports, Stripe reports, two fulfillment-center reports, and manual sales data if applicable.
- Historical context: 2022 and 2023 bookkeeping can be inspected via Simplbooks API and treated as examples, not unquestionable truth.
- Inventory exists from an earlier manufacturing batch and is still being sold.
- Local rules and tax guidance may be provided as articles or legal references; skills should use provided references instead of inventing legal rules.
- Raw source files for each company live under `companies/<company>/source/`.
- Derived working files for each company live under `companies/<company>/artifacts/`.
- Each company folder should include `companies/<company>/METADATA.md`.

## Resolved Baseline Assumptions

For the current examples discussed so far:

- the target company is VAT-registered in both 2024 and 2025
- the current example setup does not need OSS handling because cross-border turnover is too low
- inventory is tracked in Simplbooks, not in a separate external inventory system
- fulfillment partner behavior may differ by partner and by year, so the workflow must not hardcode one fulfillment model
- 2022-2023 should be treated as a clue set, not as unquestionable target behavior

Generic requirement:

- the resulting setup must still work for other similar companies, including different VAT-registration states and different partner mixes

## Initial Local Findings

The current repository already shows a few implementation-relevant facts:

- `.apikey` exists locally and appears to contain a bare Simplbooks token.
- `companies/example/METADATA.md` uses `Example Company OÜ` as the generic template company.
- company folders can carry a `METADATA.md` file for local company identity
- `scripts/` exists and is the right place for shared deterministic logic.

This means the reusable API layer should read the token from `.apikey`, and should read `company_id` from `companies/<company>/METADATA.md` by default.

## Validated Local Findings

Read-only examination against an ignored local company confirmed that the generic design is directionally right, but it exposed several concrete constraints that should be reflected in the skills:

- The API token plus `company_id` flow worked as expected for read-only inspection.
- Real company data should stay under ignored `companies/<company>/artifacts/` paths.
- `bookdisco` must inspect row-level invoice and purchase details, not only headers.
- Inventory items and warehouse IDs can materially shape sales patterns.
- Sales revenue may not be a single bucket in practice:
  - different income accounts may be used for domestic, EU, and export sales
  - shipping revenue may appear on separate invoice rows and separate shipping-income accounts
- Purchases are not always stock acquisitions:
  - payment-processor fees may be represented as purchase documents
  - fulfillment-provider costs may be recorded as service or fulfillment purchase rows
- `created_time` in Simplbooks cannot be treated as the accounting-period signal:
  - business documents may be inserted much later than the accounting period they belong to
  - transaction and business dates must drive discovery and audit logic
- Document numbering is not guaranteed to be period-monotonic, so number-based heuristics should not be trusted.

Design consequence:

- `bookbuilder` must be able to draft compound documents with distinct product and shipping rows.
- `bookprep` and `bookrecon` must preserve fulfillment-partner and warehouse identity where available.
- `bookaudit` must compare `created`, `transaction_date`, `income_date`, and `payment_date` instead of relying on insertion timestamps.

## Source-Pack Review Findings

Review of the provided 2023 source pack in `temp/` showed a representative mix of inputs the setup should support:

- bank statements may arrive in multiple parallel forms:
  - CSV
  - CAMT XML
  - PDF
- sales exports may arrive as structured CSV with daily aggregates
- payment-processor exports may arrive as CSV plus PDF summaries
- some vendor costs arrive as monthly PDF invoice/report packs
- supplier invoices may be individual PDFs
- `.gsheet` files are temporary accountant work files, not source data from the underlying systems
- malformed or low-quality PDFs are possible and should be handled as exceptions

Design consequence:

- `bookprep` should maintain a source manifest with file hashes, source type, covered period, and canonical/preferred status
- parsing priority should be:
  - machine-readable structured exports first
  - PDF text extraction second
  - accountant work files such as `.gsheet` should be ignored by processors
- duplicate representations of the same source should be de-duplicated explicitly instead of double-counted
- scratch review folders like `temp/` should not replace canonical storage under `companies/<company>/source/`
- accountant work files such as `.gsheet` should be ignored by processors instead of treated as source inputs

## Simplbooks API Findings

These findings should shape the skill design:

- Auth uses `X-Simplbooks-Token`.
- Base URL is `https://app.simplbooks.com/{company_id}/api`.
- Rate limit is `60 requests per minute`.
- The public OpenAPI spec exposes business-document endpoints for:
  - items/articles
  - clients
  - financial accounts
  - bank accounts and cash
  - receipts/incomings
  - invoices
  - payments
  - purchases
  - sales orders / purchase orders
  - VAT types
  - warehouses
- Invoice and purchase rows include `article_id`, `warehouse_id`, `vat_type_id`, and account IDs. That is enough for pattern discovery from prior years.
- Stock balance endpoints exist via article and warehouse remnant calls.
- The visible spec does not expose a generic journal-entry endpoint, trial balance endpoint, or ledger-report endpoint. Assume the workflow must operate mainly through business documents plus local reconciliation logic.

## API Pitfalls To Design Around

- List endpoints are documented as `GET` but also declare request bodies. Many client libraries handle GET bodies poorly. The API skill must test and standardize how filters are actually sent.
- The docs say dates use `yyyy-MM-dd`, but several list examples show `01-01-2021`. The API skill must verify accepted date formats instead of assuming.
- Endpoint naming is unintuitive:
  - `financial_accounts` = chart of accounts
  - `income_accounts` = bank accounts / cash registers
  - `incomings` = receipts
- Response wrappers are inconsistent across endpoints:
  - invoices list items use `invoices`
  - purchases use `Purchase`
  - receipts use `Incoming`
  - payments use `Payment`
- There is no obvious generic journal/report API in the visible spec, so ledger-level checks may need to be inferred from invoices, purchases, receipts, payments, item balances, and local source files.
- Rate limiting is low enough that batching and throttling should be built into every write-capable skill.
- Period-relevant dates are spread across multiple fields such as `created`, `transaction_date`, `income_date`, `payment_date`, and `created_time`. Skills must know which one is semantically relevant for each document type.

## Shared Design Rules

All bookkeeping skills should follow these rules:

- Default to read-only behavior.
- Require explicit approval before any API write.
- Never create clients, accounts, VAT types, items, or warehouses silently.
- Never post anything without source references.
- Never guess VAT treatment when the customer country, VAT status, or transaction type is unclear.
- Never "fix" inventory by guessing; unresolved stock deltas must stay in exceptions.
- Keep execution idempotent so rerunning a month does not duplicate documents.
- Operate one month at a time.
- Keep a reversible audit trail for every submitted batch.

## Shared Artifacts

All skills should exchange structured files instead of prose-only outputs.

Recommended shared artifacts:

- `companies/<company>/METADATA.md`
  - standardized core metadata:
    - company name
    - company slug
    - Simplbooks `company_id`
    - VAT registration status
    - short description
  - additional small non-secret metadata when justified for the local workflow
- `companies/<company>/artifacts/company_profile.json`
  - company id, base currency, VAT registration status, OSS status, default warehouses, bank account IDs
- `companies/<company>/artifacts/discovery/<year>-overview.json`
  - read-only year summary gathered from Simplbooks before drafting bookkeeping changes
- `companies/<company>/artifacts/discovery/<year>-findings.md`
  - short human-readable synthesis of the year examination and the implications for later skills
- `companies/<company>/artifacts/policy_memo.md`
  - discovered bookkeeping conventions from 2022-2023
- `companies/<company>/artifacts/entity_map.json`
  - known account IDs, VAT type IDs, warehouse IDs, item IDs, contact IDs
- `companies/<company>/artifacts/normalized/<period>.json`
  - normalized source data for a single month
- `companies/<company>/artifacts/recon/<period>.json`
  - reconciliation status, mismatches, exceptions
- `companies/<company>/artifacts/actions/<period>.yaml`
  - draft API actions to review before submit
- `companies/<company>/artifacts/submissions/<period>.json`
  - executed requests and actual API responses
- `companies/<company>/artifacts/audits/<period>.md`
  - post-submit audit report

## Draft Action File Contract

The conversation started with `action`, `reason`, and `response`. Keep that idea, but make the draft file more explicit.

Each draft action should contain:

- `idempotency_key`
- `period`
- `action_type`
- `method`
- `endpoint`
- `payload`
- `source_refs`
- `reason`
- `confidence`
- `depends_on`
- `expected_effect`
- `review_notes`

After `booksend`, append:

- `executed_at`
- `response_status`
- `response_body`
- `inserted_id`

## Skill Definitions

### 1. `simplbooks-api`

Purpose:

- isolate Simplbooks-specific mechanics from bookkeeping logic
- provide safe wrappers for auth, pagination, throttling, retries, and response capture

Inputs:

- `company_id`
- API token
- endpoint/action request
- run mode: `read-only`, `dry-run`, or `write`

Outputs:

- raw response snapshots
- normalized JSON wrapper responses
- endpoint capability notes

What this skill should include:

- `SKILL.md` with endpoint map and usage rules
- `references/simplbooks_api.md` summarizing the relevant endpoints
- reusable Python scripts under `scripts/` for authenticated GET/POST, pagination, throttling, and response logging
- the initial reusable components are `scripts/simplbooks_api.py` and `scripts/examine_simplbooks_year.py`

Critical responsibilities:

- load the API token from `.apikey`
- accept `company_id` explicitly, but default to reading it from `companies/<company>/METADATA.md`
- verify date format behavior
- verify how list filtering works for GET endpoints
- enforce request throttling below the published limit
- capture every request and response to a local log
- normalize endpoint wrapper inconsistencies for downstream scripts

### 2. `bookdisco`

Purpose:

- inspect prior periods in Simplbooks and derive a company-specific bookkeeping policy memo

Inputs:

- target lookback period, initially 2022-2023
- Simplbooks read access

Outputs:

- `companies/<company>/artifacts/policy_memo.md`
- `companies/<company>/artifacts/entity_map.json`
- `companies/<company>/artifacts/historical_patterns.md`
- confidence notes on which patterns are trustworthy and which are suspicious

Responsibilities:

- map the real chart of accounts in use
- map VAT type IDs actually used
- map bank/cash account IDs
- map item and warehouse usage
- inspect invoice, purchase, receipt, and payment shapes
- identify how refunds, Stripe fees, shipping, fulfillment costs, and manual sales were historically handled
- identify whether inventory and COGS were actually tracked or mostly ignored
- detect which date field is semantically relevant for each document type in the target company

Guardrail:

- historical practice is evidence, not law; the skill must flag inconsistent prior-year behavior instead of copying it blindly

### 3. `bookprep`

Purpose:

- convert raw source files into a normalized month-level dataset

Inputs:

- WooCommerce exports
- Stripe exports
- bank statements
- fulfillment partner reports
- any manual sales or adjustment files

Outputs:

- `companies/<company>/artifacts/normalized/<period>.json`
- source manifest with filenames, hashes, covered dates, and parser notes

Responsibilities:

- parse heterogeneous inputs into one consistent shape
- normalize dates, currencies, gross/net/VAT amounts, fees, shipping, refunds, and identifiers
- preserve raw source references down to file and row level where possible
- split ambiguous rows into exception records instead of forcing a classification
- preserve channel or warehouse identity when the source data indicates different fulfillment paths
- assign canonical priority when the same underlying data exists in CSV, XML, and PDF variants

Implementation note:

- this skill will probably need scripts for CSV/XLSX normalization because deterministic parsing matters more than free-form reasoning here

### 4. `bookrecon`

Purpose:

- prove that the normalized source data is internally coherent enough to draft accounting actions

Inputs:

- `normalized/<period>.json`
- `policy_memo.md`
- Simplbooks entity maps when relevant

Outputs:

- `companies/<company>/artifacts/recon/<period>.json`
- exception list
- approve/block status for `bookbuilder`

Minimum checks:

- Woo sales totals vs Stripe gross charges
- Stripe payouts vs bank receipts
- refunds vs payout deductions
- fulfillment report totals vs expected expense buckets
- item quantities vs fulfillment quantities when inventory matters
- continuity with previous month when possible

Guardrail:

- if unresolved mismatches exceed a configured threshold, stop the pipeline for that period

### 5. `bookbuilder`

Purpose:

- transform reconciled month data into a reviewable draft action file for Simplbooks

Inputs:

- `companies/<company>/artifacts/policy_memo.md`
- `companies/<company>/artifacts/entity_map.json`
- `companies/<company>/artifacts/normalized/<period>.json`
- `companies/<company>/artifacts/recon/<period>.json`

Outputs:

- `companies/<company>/artifacts/actions/<period>.yaml`
- human-readable summary of proposed postings

Responsibilities:

- decide posting granularity that matches the company pattern
- generate concrete API calls, not just accounting prose
- attach source references and reasons to every action
- group actions into a reversible month batch
- support separate revenue and shipping rows when the company pattern uses them

Preferred behavior:

- use summary postings when they match the historical pattern and preserve correctness
- avoid per-order posting unless VAT, inventory, or prior-year practice clearly requires it

### 6. `bookchecker`

Purpose:

- run a pre-submit review of the draft batch

Inputs:

- `companies/<company>/artifacts/actions/<period>.yaml`
- `companies/<company>/artifacts/recon/<period>.json`
- `companies/<company>/artifacts/policy_memo.md`

Outputs:

- `check_report.md`
- pass/fail decision

Checks:

- duplicate-risk check
- missing source reference check
- exact-once physical-bank coverage against the reviewed annual allocation artifact
- clearing-ledger continuity and unresolved-movement review
- arithmetic consistency
- obviously wrong account/VAT usage
- mismatch between action totals and reconciliation totals
- outlier checks against historical months

This skill is intentionally narrower than `bookaudit`. It is a pre-submit gate.

### 7. `booksend`

Purpose:

- execute an approved draft action file against Simplbooks and record results

Inputs:

- approved `companies/<company>/artifacts/actions/<period>.yaml`
- Simplbooks write access

Outputs:

- `companies/<company>/artifacts/submissions/<period>.json`
- updated action file with actual response metadata

Responsibilities:

- execute in a stable order
- stop on first hard failure unless told otherwise
- capture each response exactly
- support a dry-run mode that validates request shape without mutating anything when possible
- support a rollback/reversal plan at the batch level

Guardrails:

- no write without explicit confirmation
- write mode reruns the independent checker from the hash-bound normalized, reconciliation, policy, discovery, rate, and bank-allocation inputs
- require the exact preceding configured month, including the prior December across a year boundary, to be successfully submitted and immutable
- never regenerate or mutate a successfully submitted action YAML; a matching successful submission SHA freezes it
- no destructive cleanup by default
- no master-data creation unless separately approved

### Full-year dry run and live-month orchestration

`scripts/full_year_dry_run.py` processes months chronologically, passes the reviewed annual bank allocation to reconciliation, building, and checking, and reports annual physical rows, allocated and uncovered rows, clearing movements, and unresolved clearing movements. A successfully submitted month whose submission log binds the unchanged action YAML is reported as `skipped_submitted`; `--force-build` never overrides that freeze. A changed submitted YAML or a partial write stops orchestration instead of regenerating the batch.

`scripts/live_month_run.py` is the fail-closed operator entry point for one live month. Explicit `--confirm-write` is required before it starts. It refreshes read-only discovery, rebuilds only a never-submitted month, runs the checker, pauses for the human to change only `approval_status` to `approved`, reruns the checker against that exact YAML, and invokes `booksend --mode write --confirm-write`. It refuses submitted or partial periods, missing or failed configured predecessors, checker errors or warnings, stale bindings, non-approval edits at the checkpoint, and unresolved dependencies. It never sets approval itself.

### 8. `bookaudit`

Purpose:

- perform an independent post-submit review of a month or full year already present in Simplbooks

Inputs:

- source files
- final Simplbooks data for target period
- `companies/<company>/artifacts/policy_memo.md`

Outputs:

- `companies/<company>/artifacts/audits/<period>.md`
- issue list with severity and supporting evidence

Checks:

- source totals vs Simplbooks totals
- bank completeness
- processor completeness
- VAT reasonableness
- inventory reasonableness where inventory is tracked
- continuity from previous month / year
- spot-checks of random transactions
- verify that accounting-period dates, not insertion timestamps, are being audited

Design requirement:

- this skill should not simply re-read `bookbuilder` reasoning and declare success; it should recompute from source artifacts and final Simplbooks state

## Why `bookchecker` And `bookaudit` Should Stay Separate

Keep both.

- `bookchecker` is a pre-submit gate over the draft actions.
- `bookaudit` is a post-submit independent review over the resulting Simplbooks state.

Merging them would weaken the control structure.

## Skill Creation Order

Build the skills in this order:

1. `simplbooks-api`
2. shared artifact schemas and templates
3. `bookdisco`
4. `bookprep`
5. `bookrecon`
6. `bookbuilder`
7. `bookchecker`
8. `booksend`
9. `bookaudit`

Reason:

- the read-only skills should stabilize the data model before any write logic exists
- `bookrecon` should exist before `bookbuilder`
- `booksend` should be the last write-capable skill to ship

## Suggested Skill Folder Contents

Each skill should stay lean and push detail into references/scripts only when needed.

Suggested structure per skill:

```text
skill-name/
├── SKILL.md
├── agents/openai.yaml
├── references/
│   └── ...
└── scripts/
    └── ...
```

Use scripts when deterministic behavior matters:

- API requests
- CSV/XLSX normalization
- reconciliation math
- action-file validation
- submission logging

These scripts should live under `scripts/` and be reusable across companies and skills.

## Recommended Prompt Flow

The practical operator flow should be:

1. `Use bookdisco to inspect 2022 and 2023 in Simplbooks for Example Company OÜ and produce companies/example/artifacts/policy_memo.md, entity_map.json, and a list of suspicious historical patterns. Read-only only.`
2. `Use bookprep and bookrecon for January 2024 using the provided files under companies/example/source/. Produce normalized data, reconciliation output, and exceptions under companies/example/artifacts/.`
3. `Use bookbuilder to produce companies/example/artifacts/actions/2024-01.yaml and a short explanation of each action. Do not submit anything.`
4. `Use bookchecker to review companies/example/artifacts/actions/2024-01.yaml against the reconciliation pack and prior-year policy.`
5. `Use booksend to execute the approved January 2024 actions and save all responses.`
6. `Use bookaudit to audit January 2024 in Simplbooks against the source pack.`

After January is stable, repeat month by month.

For year-end review:

1. `Use bookaudit to audit all of 2024.`
2. `Use bookaudit to audit all of 2025.`

## Guardrails Worth Enforcing Even For A Small Hobby Business

Because the turnover is small, the system should stay simple. But these controls are still worth keeping:

- one month per batch
- one reviewed action file per batch
- source manifest with file hashes
- idempotency key per draft action
- explicit exception bucket instead of guessing
- no silent account or VAT code creation
- request/response logging for every API call
- independent audit after submit

These are the high-value controls. Anything more complex than this should be added only if real data shows a need.

## Resolved Conventions

These repository-level conventions are now fixed:

- `companies/<company>/METADATA.md` should standardize these core keys:
  - `Company name`
  - `Company slug`
  - `Simplbooks company ID`
  - `VAT registered`
  - `Description`
- Additional metadata keys are allowed when justified, but the file should remain lightweight and non-secret.
- `temp/` is disposable local scratch space for review and intake, and should stay ignored from git.
- Canonical source data belongs under `companies/<company>/source/`, not under `temp/`.

## Bottom Line

The original approach is good.

Do not collapse it into a single "book the year" skill. Keep the pipeline. Add `bookrecon`. Make `simplbooks-api` the low-level compatibility layer. Make every skill exchange structured artifacts. Keep writes gated behind review.

That should be enough structure to make the bookkeeping workflow practical without overengineering it for a low-volume company.
