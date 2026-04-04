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

### Arithmetic Consistency

- compares invoice and purchase draft totals against referenced normalized records
- checks line-item totals against payload totals
- checks incoming/payment settlement amounts against referenced payouts or bank records

### Account And VAT Review

- fails when low-confidence actions are present
- fails when draft lines lack suggested account or VAT IDs
- fails when cash-settlement drafts lack a bank account ID
- warns when invoice drafts still lack a concrete contact/client ID

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
- contact resolution is still treated as a warning because the current builder only emits hints
- historical outlier checks are policy-driven heuristics, not a full recomputation of prior years
