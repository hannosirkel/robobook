---
name: booksend
description: Use this skill to dry-run or execute an approved Simplbooks action batch in stable dependency order, update `companies/<company>/artifacts/actions/<period>.yaml` with response metadata, and write `companies/<company>/artifacts/submissions/<period>.json` with request logs and a batch-level rollback plan.
---

# Booksend

## Overview

Use this skill after `bookchecker` and before `bookaudit`.
It is the submit-capable step in the workflow, with a dry-run default and explicit write guardrails.

## When To Use It

Use this skill when the task involves:

- reviewing or executing `companies/<company>/artifacts/actions/<period>.yaml`
- writing `companies/<company>/artifacts/submissions/<period>.json`
- resuming a partially submitted month without resending already successful actions
- capturing exact request and response metadata for the batch

Do not use this skill for source parsing, reconciliation, draft generation, or post-submit audit.

## Workflow

1. Start from `actions/<period>.yaml` and `actions/<period>.check.md`.
2. Default to `--mode dry-run`.
3. Require `approval_status: approved` or `submitted`, a passing check report that matches the batch ID and current action-file SHA, and `--confirm-write` before `--mode write`. The Markdown report is audit evidence; write mode also reruns the full independent checker from the exact bound normalized, recon, posting-policy, discovery, rate, and allocation inputs.
   The first configured action period has no predecessor; every later configured period requires the immediately preceding batch to have a successful immutable write log.
4. Use `scripts/booksend.py` as the main entrypoint.
5. Let the runner translate draft schemas into live Simplbooks `create` payloads instead of posting the draft payloads directly.
   Foreign-currency drafts must carry the checker-reviewed ECB rate and provenance; the runner copies that rate and never invents one.
   Non-inventory supplier-credit drafts are translated into purchase invoices with negative line sums.
6. Keep execution order dependency-stable and stop on the first hard failure unless `--continue-on-error` is explicit.
7. Preserve the updated action file and submission log together so reruns remain auditable.
   A fully successful write stores the resulting action-file SHA and any later YAML mismatch is rejected.

## Commands

Dry-run a checked batch:

```bash
python3 scripts/booksend.py \
  --company-dir companies/example \
  --period 2024-01
```

Execute a checked and approved batch:

```bash
python3 scripts/booksend.py \
  --company-dir companies/example \
  --period 2024-01 \
  --mode write \
  --confirm-write
```

## Current Submit Scope

Implemented write targets:

- `invoices/create`
- `purchases/create`
- `incomings/create`
- `payments/create`

## Guardrails

- `booksend` defaults to dry-run.
- Write mode requires a passing `bookchecker` report and explicit confirmation.
- Already successful actions are skipped on rerun instead of being resent.
- Automatic rollback is not implemented; the submission log carries a manual reversal plan only.
- Master-data creation endpoints stay blocked unless separately approved.
- Reject foreign-currency actions without a positive reviewed rate.
- Reject supplier credits with non-positive draft magnitudes or inventory/article links; those need original stock-batch handling.
- Reject pending, blocking, or invalid manual statement-import financial dependencies before
  translating any API action. A complete verified proof may become non-blocking only when it retains
  the SimplBooks transaction ID and audit/discovery evidence reference; the remaining API actions may
  then proceed.
- Recompute the full checker evaluation from hash-bound inputs before translating any action, constructing a client, or calling the API; any error or approved-batch warning blocks.
- Reject out-of-order writes and any mutation of a successfully submitted action YAML.

## References

- Read `references/booksend.md` for the execution model, output semantics, and current limits.
