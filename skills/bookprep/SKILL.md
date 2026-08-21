---
name: bookprep
description: Use this skill to convert raw company source files into a normalized month-level dataset with an embedded source manifest, preserving row-level references where possible and producing explicit exceptions instead of guessing when inputs are incomplete.
---

# Bookprep

## Overview

Use this skill for source intake and deterministic normalization after `bookdisco`.
It turns files under `companies/<company>/source/` into a month-level normalized JSON artifact.

## When To Use It

Use this skill when the task involves:

- building `companies/<company>/artifacts/normalized/<period>.json`
- hashing and cataloging source files for a target month
- choosing canonical machine-readable sources when CSV, XML, and PDF variants overlap
- normalizing supported exports into consistent records for later reconciliation

Do not use this skill for historical Simplbooks discovery, reconciliation, action building, or
submission.

## Workflow

1. Default to `companies/<company>/source/` as the intake directory.
2. Prefer machine-readable sources over PDF variants.
3. Use `scripts/bookprep.py` as the main entrypoint.
4. Keep unsupported canonical sources in the manifest and surface them as exceptions.
   Do not silently drop them.
5. Ignore `.gsheet` accountant work files entirely during intake.
   They are not source data and should not appear in the normalized manifest.
6. Treat the `sources` array inside the normalized JSON as the source manifest for the period.
7. For company workflows, keep canonical source references under `companies/<company>/source/`; `temp/` is review-only scratch.

## Command

```bash
.venv/bin/python scripts/bookprep.py \
  --company-dir companies/example \
  --period 2024-01
```

For local review against scratch data, override the intake directory explicitly:

```bash
.venv/bin/python scripts/bookprep.py \
  --company-dir companies/example \
  --source-dir temp \
  --period 2023-01
```

## Current Parser Coverage

Implemented deterministic parsers:

- Woo daily sales CSV
- PayPal transaction CSV
- Stripe balance-history CSV
- Printful `Orders.csv`
- Printful `Wallet.csv`
- Printful `Other.csv`
- Printful `Services.csv`
- bank statement CSV
- CAMT bank XML
- Stripe fee invoice PDFs
- Printful monthly VAT-report and invoice PDFs
- text-based supplier invoice PDFs

Tracked but not yet parsed:

- XLSX inputs
- generic JSON/manual exports without a dedicated parser
- scanned/image-only PDFs that do not expose extractable text

## Guardrails

- Prefer structured exports when duplicates exist.
- When Printful `Orders.csv` exists for the month, treat the PDF monthly summary as duplicate expense evidence and keep only invoice-only PDF rows such as storage invoices.
- Ignore `.gsheet` accountant work files entirely.
- Preserve row references when parsing CSV or XML.
- Preserve page references when parsing PDFs.
- Emit exceptions instead of inventing refund or VAT splits that the source does not support directly.
- Keep canonical source choice explicit via the embedded source manifest.

## References

- Read `references/bookprep.md` for parser rules, current limits, and supported source heuristics.
