---
name: bookbuilder
description: Use this skill to turn approved month-level normalized and reconciliation artifacts into a reviewable Simplbooks draft action batch under `companies/<company>/artifacts/actions/<period>.yaml`, with concrete draft endpoints, source references, and review notes for unresolved mappings.
---

# Bookbuilder

## Overview

Use this skill after `bookrecon` and before `bookchecker`.
It converts normalized month evidence into a draft action batch for review, not direct submission.

## When To Use It

Use this skill when the task involves:

- building `companies/<company>/artifacts/actions/<period>.yaml`
- turning normalized sales, refunds, fees, purchases, and payout evidence into draft Simplbooks
  actions
- attaching source references and reasoning to every proposed action
- preparing a month batch for `bookchecker`

Do not use this skill for raw source parsing, reconciliation, direct submission, or post-submit
audit.

## Workflow

1. Start from `normalized/<period>.json` and `recon/<period>.json`.
2. Default to stopping when recon does not approve the month.
3. Load `policy_memo.md`, `entity_map.json`, and `company_profile.json` when they exist.
4. Use `scripts/bookbuilder.py` as the main entrypoint.
5. Keep the batch in `draft` status and push unresolved mapping gaps into `review_notes`.
6. Hand the resulting action file to `bookchecker` before any write-capable skill is used.

## Command

```bash
python3 scripts/bookbuilder.py \
  --company-dir companies/example \
  --period 2024-01
```

To intentionally inspect a blocked month without approving it for submit, force draft generation:

```bash
python3 scripts/bookbuilder.py \
  --company-dir companies/example \
  --period 2024-01 \
  --force
```

## Current Draft Families

Implemented draft actions:

- sales invoice summaries via `invoices/create`
- refund credit-note summaries via `invoices/create`
- processor fee summaries via `purchases/create`
- purchase-expense summaries via `purchases/create`
- payout incoming summaries via `incomings/create`
- purchase-linked payment summaries via `payments/create`

## Guardrails

- Stop on blocked recon unless `--force` is explicit.
- Keep `approval_status` as `draft`.
- Never hide missing account, VAT, contact, or bank mappings; carry them in `review_notes`.
- Prefer month summaries over per-order drafts unless later evidence requires finer granularity.

## References

- Read `references/bookbuilder.md` for payload shapes, current heuristics, and limits.
