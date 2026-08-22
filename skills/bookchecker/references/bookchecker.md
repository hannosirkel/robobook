# Bookchecker Reference

## Primary Script

- `scripts/bookchecker.py`

Required inputs:

- `--period`
- either `--company-dir` or `--actions`

Optional inputs:

- `--recon`
- `--policy-memo`
- `--output`

## Output Contract

Primary output:

- `companies/<company>/artifacts/actions/<period>.check.md`

The report is Markdown and follows the sections in `templates/check_report.template.md`.

## Current Check Semantics

### Duplicate Risk

- fails on duplicate `idempotency_key`
- fails on duplicate method/endpoint/payload combinations across actions
- warns on repeated source refs inside a single action

### Source Reference Coverage

- fails when action source refs are missing
- fails when referenced paths do not exist
- fails when `record_ref` is missing or cannot be resolved back to a normalized record

### Bank Statement Completeness

- reloads the bound annual bank-allocation artifact and its exact normalized hash bindings
- fails on missing, duplicate, extra, stale, or wrong-ledger physical coverage
- requires every cash-settlement action to bind exactly one physical bank row
- compares statement business date, currency, signed amount, and exact source-account mapping
- treats a complete verified manual statement-import dependency as one terminal coverage item
- requires manual-financial rows to have exactly one verified manual item and zero API cash actions
- keeps pending, blocking, malformed, or source-mismatched manual proof as a hard error
- accepts reviewed API-only splits only through a bijective assignment by signed amount, disposition, and exact target
- resolves generated invoice/purchase targets to the correct current action/schema with an exact dependency, or to immutable successful prior-action evidence with an inserted ID
- binds each direct-sale receipt to its actual generated invoice, source-row line, and reviewed grouping/target values

### Arithmetic Consistency

- compares invoice and purchase draft totals against referenced normalized records
- checks line-item totals against payload totals
- checks each reviewed-split incoming/payment against its assigned part and proves the whole signed row sum once
- excludes bijectively assigned reviewed parts from the legacy same-source multi-payment whole-row check

### Account And VAT Review

- fails when low-confidence actions are present
- fails approved batches that contain medium-confidence accounting judgments
- fails when draft lines lack suggested account or VAT IDs
- fails when cash-settlement drafts lack a bank account ID
- fails when document drafts lack a concrete contact/client ID

### Recon Alignment

- fails when recon does not approve the month
- fails when recon still carries blocking issues
- warns when recon still carries warning checks

### Historical Outliers

- warns when the action batch conflicts with policy-memo cues such as separate shipping treatment
- warns when the policy memo implies warehouse or bucket preservation that the draft does not clearly preserve

## YAML Loading

The checker prefers `PyYAML` when available.
If `PyYAML` is absent, it falls back to Ruby’s `YAML` loader and converts the result through JSON.

## Current Limits

- the checker only validates draft payload families currently emitted by `bookbuilder`
- confidence reflects missing IDs and explicit open accounting judgments, not informational provenance notes
- historical outlier checks are policy-driven heuristics, not a full recomputation of prior years
