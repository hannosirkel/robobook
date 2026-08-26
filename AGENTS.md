# AGENTS

<!-- BEGIN MANAGED ARCHITECTURE BASELINE -->
<!-- Generated from hannosirkel/architecture. Do not edit inside these markers.
     Regenerate with: tooling/universe sync-baseline robobook -->

Governed by [`architecture`](https://github.com/hannosirkel/architecture).

| | |
| --- | --- |
| Profile | `application-public` |
| Visibility | declared public, currently public |
| Languages | python |

**Standards that apply here.** Read a standard before you change something it
governs.

- [Agent operation](https://github.com/hannosirkel/architecture/blob/main/standards/agent-operation.md) — worktrees, branches, multi-agent safety, delegation
- [Security](https://github.com/hannosirkel/architecture/blob/main/standards/security.md) — secrets, public and private boundaries, workflow hardening
- [Code quality](https://github.com/hannosirkel/architecture/blob/main/standards/code-quality.md) — gates, coaching, testing, review cutoff
- [Repository contract](https://github.com/hannosirkel/architecture/blob/main/standards/repository-contract.md) — required files, profiles, skills
- [Work routing](https://github.com/hannosirkel/architecture/blob/main/standards/work-routing.md) — where a change starts, and where a working plan belongs
- Language standards: [python](https://github.com/hannosirkel/architecture/blob/main/standards/languages/python.md)

**Never commit to a default branch.** Work in `~/app/.worktrees/robobook/<task>`.
Branch from `origin/main`. Open a pull request.

**A working plan for this repository goes in `docs/working/`.** A change
spanning several repositories with no clear owner starts in `architecture`
instead.

**This repository must be safe to publish.** Never commit a password, token, key, kubeconfig,
rendered Secret, or live export. No repository in this universe holds a secret
value, and a private one is no exception.

**Run `habit-hooks` before declaring an edit done.** If it is not on `PATH`:

```bash
uv tool install "habit-hooks[python,typescript]"
```

That command names every language plugin **this universe** uses, not this
repository's. Install it whole: a later install naming fewer extras silently
removes the rest.

<!-- END MANAGED ARCHITECTURE BASELINE -->

## Work in the checkout, not a worktree

**This repository overrides the worktree rule in the baseline above.** Branch
inside the main checkout instead. Everything else in that rule still holds:
never commit to a default branch, branch from `origin/main`, open a pull
request.

A worktree cannot do this repository's work. `/companies/*` is git-ignored, so
a fresh worktree has no company workspace at all -- no source packs, no
artifacts, no posting policy, no entity map. Every script in the pipeline needs
them, and none of them are coming.

Reaching back into the checkout by absolute path does not rescue it. A record's
`source_id` is derived from its path relative to the working directory, so the
same source file read from a worktree yields different IDs, and every reviewed
allocation bound to the old ones silently stops matching.

The virtualenv is in the checkout too. **Never symlink `.venv` into a
worktree**: the link resolves to its own location, so checking it out replaces
a real virtualenv with a link to itself.

The cost of this override is that parallel tasks share one working tree. Run
one editing task at a time, and prefer read-only agents for anything
concurrent.

## What this repository is

Reusable skills, Python scripts, JSON schemas, templates, reference artifacts,
and tests for month-by-month bookkeeping in Simplbooks. It owns all of those.

Simplbooks holds the accounting system of record. This repository never becomes
a second one. It does not own a company's accounting data.

## Commands

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover --start-directory tests
```

Run the tests before every handoff. The rest of the command catalogue is in
[`docs/current/repository-layout.md`](docs/current/repository-layout.md).

`ruff check .` also gates a merge. It blocks on new work only: this repository's
existing findings are baselined as `# noqa:` directives in the files themselves.
**Never change a line of logic to satisfy the linter**, and never widen a
directive to cover something your own change introduced. See
[`docs/current/repository-layout.md`](docs/current/repository-layout.md).

## This repository is public, and it must stay publishable

- **No real company details, no real name, no customer data, no credential.**
  Not in a script, a schema, a template, a test, a document, or a commit
  message.
- **The `.gitignore` rules are load-bearing.** `/companies/*` is ignored, and
  `!/companies/example/` re-admits the synthetic example alone. A real company
  workspace stays out of Git because of those two lines. Never weaken them, and
  never add a real company under an exception.
- `companies/example/` is synthetic and publishable. Generic writing uses
  `Example Company OÜ`.
- Company-specific findings belong in that company's ignored `artifacts/`
  directory, never in a root document or a committed generic file.
- The Simplbooks API token lives only in `.apikey`, which is ignored.

## A bookkeeping run is gated

Read [`docs/current/bookkeeping-run.md`](docs/current/bookkeeping-run.md) before
changing a skill or a script in the pipeline. It carries the eight-step order
and the gates.

The four an agent breaks most easily:

- Default to read-only. Never write before a draft review.
- `bookchecker` must pass before any submit-capable step.
- `booksend --mode write` needs explicit confirmation and an `approved` batch.
- Never post without a source reference, and keep every rerun idempotent.

## The Simplbooks API boundary

Read [`docs/current/simplbooks-api.md`](docs/current/simplbooks-api.md) before
touching `scripts/simplbooks_api.py` or a discovery script. Two rules cause
silent wrong numbers: `created_time` is not the accounting-period signal, and
document numbering is not period-monotonic.

## Money-sensitive logic needs a test

Parsing, normalization, reconciliation, VAT and account mapping, and action
generation are test-backed. A thin API wrapper may rely on a focused check.

## Adding or renaming a skill

`skills/<name>/` is the single source of truth. `.claude/skills/<name>/SKILL.md`
and `.opencode/skills/<name>/SKILL.md` are symlinks into it, one per runtime.
Add or rename all three together, or a runtime loses the skill.

## Where things live

| Question | Answer |
| --- | --- |
| How does a run work? | [`docs/current/bookkeeping-run.md`](docs/current/bookkeeping-run.md) |
| What is where? | [`docs/current/repository-layout.md`](docs/current/repository-layout.md) |
| What does the API do? | [`docs/current/simplbooks-api.md`](docs/current/simplbooks-api.md) |
| What is being built? | `docs/working/` |
