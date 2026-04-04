---
name: simplbooks-api
description: Use this skill when working directly with the Simplbooks API for read-only discovery, endpoint behavior probing, dry-run planning, or controlled write execution backed by the repository's shared scripts and artifact contracts.
---

# Simplbooks API

## Overview

Use this skill to isolate Simplbooks-specific API work from higher-level bookkeeping logic.
It is the compatibility layer for auth, company resolution, throttled requests, year discovery,
and endpoint-behavior checks.

## When To Use It

Use this skill when the task involves any of the following:

- calling Simplbooks endpoints directly
- testing how a list endpoint handles filters
- inspecting prior-year invoices, purchases, payments, receipts, items, VAT types, or warehouses
- saving read-only discovery output under `companies/<company>/artifacts/`
- preparing for write execution by reviewing the draft action batch contract first

Do not use this skill for source-file normalization, reconciliation, draft action generation, or
post-submit audit logic. Those belong in later skills.

## Workflow

1. Resolve company context from `companies/<company>/METADATA.md`.
   The expected core keys are `Company name`, `Company slug`, `Simplbooks company ID`, `VAT registered`, and `Description`.
2. Default to read-only behavior.
   Do not write unless the user has explicitly approved it.
3. Use the shared scripts instead of ad hoc request code:
   - `scripts/simplbooks_api.py` for direct API calls and pagination
   - `scripts/examine_simplbooks_year.py` for read-only yearly discovery
4. When probing a filtered `GET` endpoint, start with the default GET-body behavior and retry with
   `--get-mode query` if the endpoint appears to ignore body filters.
5. Save substantive outputs into company-local artifacts, not loose terminal output.
   Discovery outputs belong under `companies/<company>/artifacts/discovery/`.
6. Before write-oriented work, make sure the proposed batch matches
   `schemas/action-batch.schema.json` and the starter structure in
   `templates/actions-period.template.yaml`.

## Commands

Direct endpoint call:

```bash
python3 scripts/simplbooks_api.py \
  --company-dir companies/example \
  call financial_accounts/get/101
```

Paginated filtered list:

```bash
python3 scripts/simplbooks_api.py \
  --company-dir companies/example \
  paginate invoices/list \
  --payload '{"created_from":"2024-01-01","created_until":"2024-01-31"}'
```

Year overview:

```bash
python3 scripts/examine_simplbooks_year.py \
  --company-dir companies/example \
  --year 2023 \
  --output companies/example/artifacts/discovery/2023-overview.json
```

## Guardrails

- Treat `created_time` as an insertion timestamp, not the accounting-period signal.
- Do not assume list wrappers are consistent across endpoints.
- Do not silently create clients, VAT types, items, accounts, or warehouses.
- Do not submit a batch without source references and explicit approval.
- Keep all write paths idempotent at the action-file level.

## Current Repository Boundaries

The current shared Python layer already handles:

- token loading from `.apikey` or `SIMPLBOOKS_API_TOKEN`
- company ID lookup from `METADATA.md`
- throttled single requests
- persistent JSONL request/response logs when `--request-log` is set
- automatic retries with backoff for transient transport failures
- basic pagination
- read-only year overview generation

The current shared Python layer does not yet fully implement:

- a standalone schema-driven write planner outside the downstream `booksend` translation layer

If a task depends on those behaviors, implement them in `scripts/` before relying on the skill for
write automation.

## References

- Read `references/simplbooks_api.md` for the endpoint map, wrapper behavior, and known quirks.
