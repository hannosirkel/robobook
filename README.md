# Books Tooling

This repository contains reusable scripts, schemas, templates, and AI agent skills
for month-by-month bookkeeping work in Simplbooks.

It is built for a practical split:

- Simplbooks holds the accounting system of record.
- Bank transactions are imported to Simplbooks beforehand.
- One-off sales invoices are created directly in Simplbooks.
- One-off purchase invoices may already exist in Simplbooks or may be created by the flow.
- Recurring webshop data is prepared and reviewed through the repo workflow.

The public example company is `companies/example/`. Real company work stays in
ignored `companies/<company>/` folders.

## What The Repo Handles

The current shared flow is designed around these components:

- `Simplbooks`: read-only discovery, dry-run validation, controlled writes, and post-submit audit
- `WooCommerce`: recurring monthly sales basis
- `PayPal`: payout settlement evidence and processor fee evidence
- `Stripe`: payout settlement evidence and processor fee evidence
- `Printful`: fulfillment and purchase-cost evidence
- bank statements: imported cash evidence used for reconciliation and settlement drafts

The repo is intentionally conservative. It separates source evidence,
reconciliation, draft generation, pre-submit review, submit execution, and
post-submit audit instead of collapsing everything into one step.

## Workflow

The intended pipeline is:

1. `simplbooks-api`
   Read Simplbooks safely, inspect endpoints, and save discovery outputs.
2. `bookdisco`
   Inspect historical years and build company-local policy and entity artifacts.
3. `bookprep`
   Normalize source files into deterministic month artifacts.
4. `bookrecon`
   Reconcile Woo, processors, fulfillment, and bank evidence before any draft build.
5. `bookbuilder`
   Turn approved month evidence into a reviewable Simplbooks action batch.
6. `bookchecker`
   Validate draft actions against normalized and reconciled evidence.
7. `booksend`
   Dry-run or execute an approved batch in stable dependency order.
8. `bookaudit`
   Recompute results from source-derived artifacts and final Simplbooks state.

In practice:

- Woo sales normally become the recurring monthly sales invoice basis.
- Stripe and PayPal normally behave as settlement layers, not a second invoice basis.
- Printful normally becomes purchase-side evidence and can also drive payment drafts when the bank debit is present.
- Printful refund-only months remain separate supplier-credit drafts instead of being netted into expenses.
- Foreign-currency month summaries use one period-end ECB rate (or the latest prior business day) from an annual Frankfurter cache.

## Repository Layout

- `scripts/`: reusable Python logic
- `skills/`: skill packages for each step of the bookkeeping workflow (Codex, Claude Code, and opencode compatible)
- `.claude/skills/`: Claude Code project-level entry points (symlinks into `skills/`)
- `schemas/`: JSON schemas for shared artifacts
- `templates/`: starter artifact files
- `plans/`: design and implementation plans
- `companies/example/`: publishable synthetic example workspace
- `companies/<company>/`: ignored local workspaces for real companies
- `temp/`: disposable local scratch intake area

## Example Workspace

`companies/example/` is a synthetic, publishable worked example. It includes:

- non-secret example metadata
- a tiny example source pack
- worked artifacts for discovery, normalization, reconciliation, action building,
  check review, dry-run submission, and audit

Use it to understand the shape of the workflow and the expected artifact layout.
Do not treat it as live accounting data.

## Skill Compatibility

Each skill under `skills/` works with Codex, Claude Code, and opencode:

- **Codex** — reads `skills/<name>/SKILL.md` and uses `skills/<name>/agents/openai.yaml`
- **Claude Code** — reads `.claude/skills/<name>/SKILL.md`, which symlinks back to the same content
- **opencode** — reads `.opencode/skills/<name>/SKILL.md`, which symlinks back to the same content

No duplication; one source of truth per skill.

## Setup

Set up a virtual environment when PDF parsing is needed:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run shared Python scripts with `.venv/bin/python` when they depend on the
repo-managed environment.

## Typical Usage

Read-only Simplbooks discovery:

```bash
python3 scripts/examine_simplbooks_year.py \
  --company-dir companies/example \
  --year 2024 \
  --output companies/example/artifacts/discovery/2024-overview.json
```

Month normalization:

```bash
.venv/bin/python scripts/bookprep.py \
  --company-dir companies/example \
  --period 2024-01 \
  --output companies/example/artifacts/normalized/2024-01.json
```

Draft build:

```bash
python3 scripts/bookbuilder.py \
  --company-dir companies/example \
  --period 2024-01 \
  --output companies/example/artifacts/actions/2024-01.yaml
```

With `--company-dir`, the builder automatically looks for:

- `artifacts/posting_policy.json` for exact bank, contact, and posting mappings
- `artifacts/reference/ecb-rates-<year>.json` for reviewed foreign-currency rates
- `artifacts/discovery/<year>-overview.json` for live duplicate suppression

Missing explicit contacts remain blocking dependencies. Master-data creation is kept in a separately approved draft and is never performed implicitly.

Dry-run submit:

```bash
python3 scripts/booksend.py \
  --company-dir companies/example \
  --period 2024-01 \
  --mode dry-run \
  --output companies/example/artifacts/submissions/2024-01.json
```

Full-year dry run:

```bash
.venv/bin/python scripts/full_year_dry_run.py \
  --company-dir companies/example \
  --year 2024 \
  --source-dir companies/example/source
```

Annual ECB exchange-rate cache through Frankfurter:

```bash
.venv/bin/python scripts/exchange_rates.py fetch \
  --company-dir companies/example \
  --year 2024 \
  --base USD \
  --quote EUR
```
