---
name: bookchecker
description: Use this skill to run a pre-submit review over a draft Simplbooks action batch, verify source references and arithmetic against normalized and recon evidence, and write `companies/<company>/artifacts/actions/<period>.check.md` with a pass or fail decision.
---

# Bookchecker

## Overview

Use this skill after `bookbuilder` and before any submit-capable step.
It is a deterministic gate over the draft action batch, not a post-submit audit.

## When To Use It

Use this skill when the task involves:

- reviewing `companies/<company>/artifacts/actions/<period>.yaml`
- checking duplicate risk, source reference coverage, arithmetic consistency, and recon alignment
- writing `companies/<company>/artifacts/actions/<period>.check.md`
- deciding whether the batch is ready for `booksend`

Do not use this skill for source parsing, draft generation, submission, or post-submit audit.

## Workflow

1. Start from `actions/<period>.yaml` and `recon/<period>.json`.
2. Load `policy_memo.md` when it exists.
3. Use `scripts/bookchecker.py` as the main entrypoint.
4. Treat any `fail` result as a stop signal for submit-capable work.
5. Fix the upstream artifact or draft action file, then rerun the checker.

## Command

```bash
python3 scripts/bookchecker.py \
  --company-dir companies/example \
  --period 2024-01
```

For local review against ad hoc artifacts, pass the paths explicitly:

```bash
python3 scripts/bookchecker.py \
  --period 2024-01 \
  --actions /tmp/bookbuilder-2024-01.yaml \
  --recon /tmp/bookrecon-2024-01.json \
  --output /tmp/bookbuilder-2024-01.check.md
```

## Current Checks

Implemented deterministic checks:

- duplicate idempotency-key and duplicate payload risk
- missing or unverifiable source references
- draft arithmetic against referenced normalized records
- missing account, VAT, and bank mapping hints
- recon gate alignment
- policy-driven historical outlier warnings

## Guardrails

- `bookchecker` is a pre-submit gate and should run before `booksend`.
- A blocked recon month should fail the checker.
- Do not treat low-confidence draft actions as submit-ready.
- Keep the report with the action batch so reruns remain auditable.
- Fail company batches whose normalized evidence still points to disposable `temp/` source paths.

## References

- Read `references/bookchecker.md` for check semantics, report sections, and current limits.
