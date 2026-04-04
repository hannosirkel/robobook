---
name: bookaudit
description: Use this skill to perform an independent post-submit audit of a month or full year in Simplbooks against source-derived normalized artifacts, then write `companies/<company>/artifacts/audits/<period>.md` with findings on totals, bank and processor completeness, VAT, inventory signals, continuity, and date semantics.
---

# Bookaudit

## Overview

Use this skill after `booksend`, or for a later year-end review.
It is a read-only post-submit audit over final Simplbooks state, not a pre-submit draft gate.

## When To Use It

Use this skill when the task involves:

- auditing a submitted month in Simplbooks against `normalized/<period>.json`
- auditing a full year by aggregating monthly normalized artifacts
- writing `companies/<company>/artifacts/audits/<period>.md`
- checking totals, VAT, bank/payment completeness, inventory signals, continuity, and date semantics independently of the draft batch

Do not use this skill for source parsing, reconciliation, draft generation, or submission.

## Workflow

1. Start from source-derived normalized artifacts plus final Simplbooks data for the target scope.
2. Keep the audit read-only.
3. Use `scripts/bookaudit.py` as the main entrypoint.
4. Recompute findings from normalized evidence and live Simplbooks documents instead of reusing `bookbuilder` or `bookchecker` conclusions.
5. Keep the audit report with the company-local artifacts for the audited month or year.

## Commands

Audit one month:

```bash
python3 scripts/bookaudit.py \
  --company-dir companies/example \
  --period 2024-01
```

Audit a full year:

```bash
python3 scripts/bookaudit.py \
  --company-dir companies/example \
  --period 2024
```

## Current Checks

Implemented deterministic checks:

- source totals vs live invoices, purchases, incomings, and payments
- bank and processor completeness signals
- VAT totals and missing VAT-type hints on live rows
- inventory and warehouse-signal preservation
- continuity against the previous normalized scope when available
- business-date scoping vs insertion-timestamp drift
- deterministic spot checks over sampled live documents

## Guardrails

- `bookaudit` is read-only.
- Scope by accounting or business dates, not `created_time`.
- Do not treat a passing `bookchecker` report as an audit substitute.
- Keep the audit independent from draft-action reasoning.

## References

- Read `references/bookaudit.md` for scope resolution, live endpoint usage, and current limits.
