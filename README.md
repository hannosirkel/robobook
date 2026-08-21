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
- `docs/current/`: how the repository behaves today
- `docs/working/`: active design and implementation plans
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

## Testing

```bash
.venv/bin/python -m unittest discover --start-directory tests
```

## Typical Usage

The command catalogue lives in
[`docs/current/repository-layout.md`](docs/current/repository-layout.md): the
per-script invocations, what `bookbuilder` looks for under `--company-dir`, and
the script and testing policy. It has one home so that a command and its
documentation cannot drift apart.

## Ownership and governance

This repository is governed by
[`architecture`](https://github.com/hannosirkel/architecture), which owns the
catalogue, the shared standards, and the generated section of
[`AGENTS.md`](AGENTS.md). Read `AGENTS.md` before changing a skill or a script.

**It owns** the bookkeeping skills, their Python implementations, the JSON
schemas, the templates, the reference artifacts, and its own tests.

**It does not own** the accounting system of record, which is Simplbooks, any
real company's accounting data, its deployable state, or any universe standard.

## Visibility and the ignore rules

The repository is public and must stay safe to publish. It carries no real
company detail, no real name, no customer data, and no credential.

**The `.gitignore` rules are load-bearing.** `/companies/*` ignores every
company workspace, and `!/companies/example/` re-admits the synthetic example
alone. Those two lines are the only thing keeping real bookkeeping material out
of a public repository. Never weaken them.

The Simplbooks API token lives only in `.apikey`, which is ignored.

## Where things live

| Question | Answer |
| --- | --- |
| How do I work here? | [`AGENTS.md`](AGENTS.md) |
| How does a run work? | [`docs/current/bookkeeping-run.md`](docs/current/bookkeeping-run.md) |
| What is where, and how do I run it? | [`docs/current/repository-layout.md`](docs/current/repository-layout.md) |
| What does the Simplbooks API do? | [`docs/current/simplbooks-api.md`](docs/current/simplbooks-api.md) |
| What is being built? | `docs/working/` |
| What rules apply everywhere? | [`architecture`](https://github.com/hannosirkel/architecture) |
