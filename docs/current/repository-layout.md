# Repository layout

Every directory and what it holds. The company workspace is described in
[`bookkeeping-run.md`](./bookkeeping-run.md).

| Path | Holds |
| --- | --- |
| `scripts/` | reusable Python logic for deterministic work |
| `tests/` | focused automated checks for the shared logic |
| `schemas/` | JSON schemas for the artifacts the skills exchange |
| `templates/` | starter JSON, YAML, and Markdown artifact files |
| `skills/` | the eight skill packages; the single source of truth |
| `.claude/skills/` | Claude Code entry points; `SKILL.md` symlinks into `skills/` |
| `.opencode/skills/` | opencode entry points; the same symlinks |
| `companies/example/` | the publishable synthetic example workspace |
| `companies/<company>/` | an ignored local workspace for a real company |
| `temp/` | disposable local scratch intake; ignored |
| `docs/current/` | how the repository behaves today |
| `docs/working/` | active design and implementation plans |

## Skill packages

```text
skills/<name>/SKILL.md              the skill, tool-neutral
skills/<name>/agents/openai.yaml    the Codex interface definition
skills/<name>/references/<name>.md  the detail SKILL.md points at
```

Three runtimes read the same skill:

- **Codex** reads `skills/<name>/SKILL.md` and `skills/<name>/agents/openai.yaml`.
- **Claude Code** reads `.claude/skills/<name>/SKILL.md`, a symlink into
  `skills/`.
- **opencode** reads `.opencode/skills/<name>/SKILL.md`, the same symlink.

Only `SKILL.md` is linked. `agents/` and `references/` are not, so a runtime
that resolves a relative path against the adapter directory rather than the
symlink target cannot reach `references/<name>.md`.

## Setup and commands

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover --start-directory tests
```

`requirements.txt` pins `pypdf`, which the PDF parsing needs. Run a script that
depends on it with `.venv/bin/python`.

```bash
.venv/bin/python scripts/bookprep.py --company-dir companies/example \
  --period 2024-01 \
  --output companies/example/artifacts/normalized/2024-01.json

python3 scripts/bookbuilder.py --company-dir companies/example \
  --period 2024-01 \
  --output companies/example/artifacts/actions/2024-01.yaml

python3 scripts/booksend.py --company-dir companies/example \
  --period 2024-01 --mode dry-run \
  --output companies/example/artifacts/submissions/2024-01.json

.venv/bin/python scripts/full_year_dry_run.py --company-dir companies/example \
  --year 2024 --source-dir companies/example/source

.venv/bin/python scripts/exchange_rates.py fetch \
  --company-dir companies/example --year 2024 --base USD --quote EUR
```

With `--company-dir`, `bookbuilder` looks for:

- `artifacts/posting_policy.json` for bank, contact, and posting mappings;
- `artifacts/reference/ecb-rates-<year>.json` for reviewed currency rates;
- `artifacts/discovery/<year>-overview.json` for live duplicate suppression.

A missing explicit contact stays a blocking dependency. Master-data creation
belongs in a separately approved draft, never implicit.

## Script policy

A script under `scripts/` is reusable across companies, Python, deterministic
where correctness matters, and conservative about dependencies.

Testing is risk-based. Money-sensitive logic — parsing, normalization,
reconciliation, VAT and account mapping, action generation — must be
test-backed. A thin API wrapper may rely on a focused check plus manual
validation.
