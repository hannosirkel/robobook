# Bookdisco Reference

## Purpose

This reference describes the concrete artifact flow for the `bookdisco` skill and the current
limits of the deterministic discovery implementation.

## Primary Script

- `scripts/bookdisco.py`

Inputs:

- `--company-dir`
- `--years`
- optional `--company-id`
- optional `--metadata-file`
- optional `--reuse-existing-overviews`

Outputs:

- `companies/<company>/artifacts/discovery/<year>-overview.json`
- `companies/<company>/artifacts/discovery/<year>-findings.md`
- `companies/<company>/artifacts/policy_memo.md`
- `companies/<company>/artifacts/entity_map.json`
- `companies/<company>/artifacts/historical_patterns.md`

## How It Works

1. It resolves company identity from `METADATA.md`.
2. It fetches or reuses yearly overview JSON files.
3. It derives year findings markdown per year.
4. It fetches current reference lists for accounts, VAT types, warehouses, and candidate item/contact endpoints.
5. It builds an entity map plus policy and pattern markdown files from the combined evidence.

## Current Heuristics

The current implementation is intentionally conservative and summary-driven:

- it treats multiple invoice income accounts as evidence that revenue may be split across buckets
- it treats invoice warehouse IDs as evidence that warehouse identity matters
- it treats purchase expense-account diversity as evidence that fees and fulfillment costs should not be collapsed blindly
- it flags cross-year changes in dominant income or expense accounts as suspicious

## Current Limits

- It does not yet inspect full historical row descriptions for shipping or refund classification.
- It tries multiple candidate endpoints for items and contacts, but item resolution may still remain incomplete.
- It produces a useful first-pass `policy_memo.md`, but manual review is still required before downstream builder rules are considered stable.

## Related Contracts

- `schemas/year-overview.schema.json`
- `schemas/entity-map.schema.json`
- `templates/policy_memo.template.md`
- `templates/historical_patterns.template.md`
- `templates/discovery-findings.template.md`
