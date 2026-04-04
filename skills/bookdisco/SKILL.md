---
name: bookdisco
description: Use this skill to inspect historical Simplbooks activity for a company, generate read-only discovery artifacts such as yearly overviews and findings, and produce `policy_memo.md`, `entity_map.json`, and `historical_patterns.md` for later bookkeeping skills.
---

# Bookdisco

## Overview

Use this skill for the historical discovery step that comes after `simplbooks-api` and before
source normalization or draft action building.
It turns prior-year Simplbooks data into company-local policy and mapping artifacts.

## When To Use It

Use this skill when the user wants to:

- inspect 1 or more prior years in Simplbooks
- produce `companies/<company>/artifacts/discovery/<year>-overview.json`
- produce `companies/<company>/artifacts/discovery/<year>-findings.md`
- derive `companies/<company>/artifacts/policy_memo.md`
- derive `companies/<company>/artifacts/entity_map.json`
- derive `companies/<company>/artifacts/company_profile.json`
- derive `companies/<company>/artifacts/historical_patterns.md`

Do not use this skill for source-file parsing, reconciliation, action-batch generation, or
submission. Those belong to later skills.

## Workflow

1. Resolve the target company from `companies/<company>/METADATA.md`.
2. Stay read-only.
   This skill should not mutate Simplbooks.
3. Use `scripts/bookdisco.py` as the primary entrypoint.
   It reuses `scripts/examine_simplbooks_year.py` logic and writes the planned artifacts directly.
4. If yearly overviews already exist and should be reused, pass `--reuse-existing-overviews`.
5. Review the generated policy memo for suspicious historical behavior.
   Historical practice is evidence, not a rule set to copy blindly.

## Command

```bash
python3 scripts/bookdisco.py \
  --company-dir companies/example \
  --years 2022 2023
```

Reuse existing overview JSON files when they are already present:

```bash
python3 scripts/bookdisco.py \
  --company-dir companies/example \
  --years 2022 2023 \
  --reuse-existing-overviews
```

## Outputs

The script writes these artifacts:

- `companies/<company>/artifacts/discovery/<year>-overview.json`
- `companies/<company>/artifacts/discovery/<year>-findings.md`
- `companies/<company>/artifacts/policy_memo.md`
- `companies/<company>/artifacts/entity_map.json`
- `companies/<company>/artifacts/company_profile.json`
- `companies/<company>/artifacts/historical_patterns.md`

The output shapes should remain compatible with:

- `schemas/year-overview.schema.json`
- `schemas/entity-map.schema.json`

## Guardrails

- Keep the skill read-only.
- Flag inconsistent historical patterns instead of normalizing them away.
- Preserve warehouse, VAT, and account evidence when it appears.
- Keep the output company-local under `companies/<company>/artifacts/`.
- Treat date semantics explicitly:
  - invoices and purchases: `created`
  - receipts: `income_date`
  - payments: `payment_date`

## References

- Read `references/bookdisco.md` for artifact details and the current heuristic limits.
