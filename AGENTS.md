# AGENTS

This file is the repo-level working contract for humans and agents.

## Purpose

This repository is for building reusable skills and scripts for month-by-month bookkeeping work in Simplbooks.

Public/generic planning uses `Example Company OÜ`.
Real company work stays inside ignored `companies/<company>/` folders.

## Repository Layout

- `plans/`
  - design and build plans such as `plans/SKILLPLAN.md`
- `scripts/`
  - reusable Python logic for deterministic work
- `tests/`
  - focused automated checks for deterministic shared logic
- `schemas/`
  - shared JSON schemas for structured artifacts exchanged between skills
- `templates/`
  - starter JSON, YAML, and Markdown templates for shared artifacts
- `temp/`
  - optional local scratch intake area for ad hoc review, gitignored and not canonical company storage
- `skills/`
  - skill packages for the bookkeeping workflow; each package contains a `SKILL.md` and an `agents/openai.yaml`
- `.claude/skills/`
  - Claude Code project-level skill entry points; each entry is a directory with a `SKILL.md` symlink into `skills/`
- `companies/<company>/`
  - company-local bookkeeping workspace
- `companies/<company>/METADATA.md`
  - lightweight non-secret company metadata
- `companies/<company>/source/`
  - raw source documents and exports
- `companies/<company>/artifacts/`
  - derived outputs, discovery files, normalized data, recon, actions, submissions, audits

## Privacy And Git Rules

- `.apikey` is local and ignored.
- `/companies/*` is ignored by default.
- `companies/example/` is explicitly allowed in git and acts as the publishable template.
- Real company folders stay ignored.
- Do not place private company data in root docs, plans, or committed generic files.
- Company-specific findings belong under that company’s ignored `artifacts/` folder.

## Company Metadata Convention

`companies/<company>/METADATA.md` is the default place for non-secret per-company identifiers.

Current expected fields:

- `Company name: ...`
- `Company slug: ...`
- `Simplbooks company ID: ...`
- `VAT registered: yes|no`
- `Description: ...`

Rules:

- Keep `METADATA.md` lightweight.
- Treat those fields as the standardized core keys.
- Additional non-secret keys are allowed only when they are useful for the local workflow.
- Do not store secrets there.
- Store the Simplbooks API token only in `.apikey`.
- Scripts should read `company_id` from `METADATA.md` by default.
- VAT registration status should be recorded in `METADATA.md`.

## Credentials And API Access

- Simplbooks auth uses header `X-Simplbooks-Token`.
- Base URL format is `https://app.simplbooks.com/{company_id}/api`.
- Reusable API access lives in [scripts/simplbooks_api.py](/Users/hanno/books/scripts/simplbooks_api.py).
- Read-only year discovery lives in [scripts/examine_simplbooks_year.py](/Users/hanno/books/scripts/examine_simplbooks_year.py).

Example usage:

```bash
python3 scripts/simplbooks_api.py --company-dir companies/<company> call financial_accounts/get/101
python3 scripts/examine_simplbooks_year.py --company-dir companies/<company> --year 2023 --output companies/<company>/artifacts/discovery/2023-overview.json
```

## Script Policy

Scripts under `scripts/` should be:

- reusable across companies
- Python-based
- deterministic where correctness matters
- conservative about dependencies

Testing policy is risk-based, not dogmatic TDD.

- Do not require strict TDD for every small wrapper or integration helper.
- Do require test-backed logic for money-sensitive transformations:
  - parsing
  - normalization
  - reconciliation
  - VAT/account mapping
  - action generation
- Thin API wrappers may rely on focused checks plus manual/integration validation.
- Pure bookkeeping logic should not remain untested.

## Source Intake Rules

- Canonical company source data belongs under `companies/<company>/source/`.
- `temp/` may be used for local review, but it is disposable scratch space and should stay ignored from git.
- Prefer machine-readable sources over PDFs when the same data exists in multiple forms.
- Treat `.gsheet` files as accountant work files, not source data.
- Ignore `.gsheet` files in processors and do not include them in source manifests.
- Maintain a manifest that records file hash, source type, covered period, and canonical/preferred status.
- De-duplicate parallel representations of the same source material explicitly.
- Handle malformed or unreadable PDFs as exceptions, not silent failures.

Preferred source priority:

1. structured exports such as CSV or XML
2. PDFs when they are the only available source or supporting evidence
3. accountant work files such as `.gsheet` should be ignored by processors

## Simplbooks API Notes

The public docs and local validation have shown these quirks:

- Published limit is `60 requests/minute`.
- Some list endpoints are documented as `GET` with request bodies.
- Endpoint wrappers are inconsistent:
  - invoice list items use `invoices`
  - purchases use `Purchase`
  - receipts use `Incoming`
  - payments use `Payment`
- Period-relevant dates differ by document type:
  - `created`
  - `transaction_date`
  - `income_date`
  - `payment_date`
  - `created_time`
- Do not use `created_time` as the accounting-period signal.
- Do not assume document numbering is period-monotonic.
- The visible API exposes business documents, items, warehouses, VAT types, and accounts.
- No generic journal-entry or trial-balance endpoint has been confirmed from the public spec.

## Bookkeeping Workflow

The intended workflow is:

1. `simplbooks-api`
2. `bookdisco`
3. `bookprep`
4. `bookrecon`
5. `bookbuilder`
6. `bookchecker`
7. `booksend`
8. `bookaudit`

Rules:

- work month by month
- default to read-only until explicit approval
- no write before draft review
- no posting without source references
- block draft building when bank receipts indicate processor activity but the processor export is missing
- stop `bookbuilder` by default when `bookrecon` does not approve the month
- require `bookchecker` to pass before any submit-capable step
- require explicit confirmation plus an `approved` action batch before `booksend --mode write`
- keep `booksend` reruns resume-safe by skipping already successful actions unless later logic explicitly says otherwise
- block master-data creation endpoints in `booksend` unless separately approved
- keep `bookaudit` read-only and recompute from source-derived artifacts plus final Simplbooks state instead of reusing draft-action reasoning
- keep reruns idempotent
- preserve a reversible audit trail

## Shared Artifact Layout

Expected company-local artifacts:

- `companies/<company>/artifacts/company_profile.json`
- `companies/<company>/artifacts/discovery/<year>-overview.json`
- `companies/<company>/artifacts/discovery/<year>-findings.md`
- `companies/<company>/artifacts/policy_memo.md`
- `companies/<company>/artifacts/historical_patterns.md`
- `companies/<company>/artifacts/entity_map.json`
- `companies/<company>/artifacts/normalized/<period>.json`
- `companies/<company>/artifacts/recon/<period>.json`
- `companies/<company>/artifacts/actions/<period>.yaml`
- `companies/<company>/artifacts/actions/<period>.check.md`
- `companies/<company>/artifacts/submissions/<period>.json`
- `companies/<company>/artifacts/audits/<period>.md`

Shared contracts for those artifacts live under:

- `schemas/`
- `templates/`

## Important Validated Design Lessons

Local validation has already confirmed these general lessons:

- `bookdisco` must inspect row-level documents, not only headers.
- Shipping revenue may be kept separate from product revenue.
- Stripe fees may be represented as purchase documents.
- Fulfillment-provider costs may be split into multiple purchase rows.
- Warehouse identity can matter materially.
- Inventory remnant endpoints are useful and should be part of discovery/audit when inventory exists.
- Audit logic must use accounting/business dates, not insertion timestamps.
- Historical years are clue sets, not unquestionable target behavior.
- Fulfillment partner behavior may vary by partner and by year.

## Skill Compatibility

Each bookkeeping skill under `skills/` is designed to work with both Codex and Claude Code.

- **Codex** loads skills from `skills/<name>/SKILL.md` and uses `skills/<name>/agents/openai.yaml` for the interface definition.
- **Claude Code** loads skills from `.claude/skills/<name>/SKILL.md`. Each file there is a symlink back into `skills/`, so the content stays in one place.

When adding or renaming a skill:

1. Add or rename the package under `skills/`.
2. Add or rename the corresponding symlink under `.claude/skills/`.
3. Keep both in sync.

## Source Of Truth

- Build/design decisions live in [plans/SKILLPLAN.md](/Users/hanno/books/plans/SKILLPLAN.md).
- `AGENTS.md` captures the working conventions that should be followed during implementation.
