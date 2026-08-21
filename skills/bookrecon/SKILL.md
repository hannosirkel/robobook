---
name: bookrecon
description: Use this skill to reconcile a normalized month-level bookkeeping pack, surface blocking mismatches and missing evidence, and write `companies/<company>/artifacts/recon/<period>.json` with approve or block status for later draft building.
---

# Bookrecon

## Overview

Use this skill after `bookprep` and before `bookbuilder`.
It turns a normalized month pack into a deterministic reconciliation artifact with checks,
exceptions, and an explicit build gate.

## When To Use It

Use this skill when the task involves:

- building `companies/<company>/artifacts/recon/<period>.json`
- checking whether normalized sales, payouts, bank receipts, and expenses line up well enough
  to draft Simplbooks actions
- carrying forward blocking `bookprep` exceptions into a build/no-build decision
- comparing current-month source coverage with the previous month

Do not use this skill for raw source parsing, historical Simplbooks discovery, action-batch
generation, or submission.

## Workflow

1. Start from `companies/<company>/artifacts/normalized/<period>.json`.
2. Load `policy_memo.md` and `entity_map.json` when they exist.
3. Auto-load the previous month’s normalized artifact when available for continuity checks.
4. Auto-load reviewed annual bank allocations from `artifacts/bank/<year>-allocations.json` when available; use `--bank-allocations` to override the path.
5. Use `scripts/bookrecon.py` as the main entrypoint.
6. Treat blocking normalized exceptions as blocking recon exceptions.
7. Stop the pipeline when the recon artifact does not approve the month for build.

## Command

```bash
python3 scripts/bookrecon.py \
  --company-dir companies/example \
  --period 2024-01
```

For local review against scratch normalization output, pass the normalized file explicitly:

```bash
python3 scripts/bookrecon.py \
  --period 2023-01 \
  --normalized /tmp/bookprep-2023-01.json \
  --output /tmp/bookrecon-2023-01.json
```

## Current Checks

Implemented deterministic checks:

- Woo sales totals vs processor gross sales
- processor payouts vs bank receipts
- processor settlement bridge across sales, refunds, fees, and payouts
- fulfillment expense totals vs bank debits when partner signals are present
- inventory quantity evidence when quantity-bearing records exist
- continuity with the previous period
- physical-bank allocation coverage and CAMT movement/balance continuity per `(IBAN, currency)`
- clearing continuity per provider, clearing account, and currency

## Guardrails

- Import blocking `bookprep` exceptions directly into recon output.
- Block the month when bank receipts indicate processor activity but no processor-side export was normalized.
- Keep same-month settlement checks conservative because payout timing can cross month boundaries.
- Do not promote a month to `bookbuilder` when deterministic checks fail.
- Phase A bank allocation and clearing findings are report-only: their `warn` checks make `bank_coverage.coverage_ready` false, but do not change legacy `approve_for_build`. Treat `approve_for_build` as the existing draft-build decision only, never as bank write readiness. Later checker/send stages must independently enforce write-capable coverage.

## References

- Read `references/bookrecon.md` for the concrete check semantics and current limits.
