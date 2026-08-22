# How a bookkeeping run works

A run processes one company for one month. It moves source evidence through
eight skills, and a human approves before anything is written to Simplbooks.

```text
simplbooks-api → bookdisco → bookprep → bookrecon → bookbuilder
              → bookchecker → booksend → bookaudit
```

| Skill | Does |
| --- | --- |
| `simplbooks-api` | reads Simplbooks safely and saves discovery output |
| `bookdisco` | inspects historical years, builds policy and entity artifacts |
| `bookprep` | normalizes source files into deterministic month artifacts |
| `bookrecon` | reconciles Woo, processor, fulfillment, and bank evidence |
| `bookbuilder` | turns approved month evidence into a reviewable action batch |
| `bookchecker` | validates draft actions against normalized and reconciled evidence |
| `booksend` | dry-runs or executes an approved batch in dependency order |
| `bookaudit` | recomputes results from artifacts and final Simplbooks state |

## Gates

These stop a run. They are not advisory.

- Work month by month. Default to read-only until an explicit approval.
- Never write before a draft review. Never post without a source reference.
- Block a draft build when bank receipts show processor activity and the
  processor export is missing.
- Stop `bookbuilder` when `bookrecon` does not approve the month.
- `bookchecker` must pass before any submit-capable step.
- `booksend --mode write` needs explicit confirmation and an `approved` action
  batch.
- Block master-data creation endpoints in `booksend` unless separately approved.
- Keep `bookaudit` read-only. Recompute from source-derived artifacts and final
  Simplbooks state, not from draft-action reasoning.
- Keep a rerun idempotent. `booksend` skips an already successful action.
- Preserve a reversible audit trail.

## Evidence roles

- Woo sales are the recurring monthly sales-invoice basis.
- Stripe and PayPal are settlement layers, not a second invoice basis.
- Printful is purchase-side evidence, and drives a payment draft when the bank
  debit is present.
- A Printful refund-only month stays a separate supplier-credit draft. Do not
  net it into expenses.
- A foreign-currency month summary uses one period-end ECB rate, or the latest
  prior business day, from the annual Frankfurter cache.

## Company workspace

```text
companies/<company>/METADATA.md              non-secret company descriptor
companies/<company>/source/                  raw source documents and exports
companies/<company>/artifacts/               derived outputs
```

Expected artifacts:

```text
artifacts/company_profile.json
artifacts/discovery/<year>-overview.json
artifacts/discovery/<year>-findings.md
artifacts/policy_memo.md
artifacts/historical_patterns.md
artifacts/entity_map.json
artifacts/normalized/<period>.json
artifacts/recon/<period>.json
artifacts/actions/<period>.yaml
artifacts/actions/<period>.check.md
artifacts/submissions/<period>.json
artifacts/audits/<period>.md
```

Their contracts live in [`schemas/`](../../schemas) and
[`templates/`](../../templates).

### `METADATA.md`

Keep it lightweight and non-secret. The standardized core keys are:

```text
Company name:            ...
Company slug:            ...
Simplbooks company ID:   ...
VAT registered:          yes|no
Description:             ...
```

A script reads `company_id` from this file by default. Add another non-secret
key only when the local workflow uses it. The API token lives in `.apikey`,
never here.

## Source intake

- Canonical company source data belongs under `companies/<company>/source/`.
- `temp/` is disposable local scratch. It stays out of Git and is not canonical
  storage.
- Prefer a machine-readable source over a PDF when both carry the same data.

Source priority:

1. a structured export, such as CSV or XML;
2. a PDF, when it is the only source or is supporting evidence;
3. an accountant work file, such as `.gsheet`, which a processor ignores.

Treat a `.gsheet` file as an accountant work file, not source data. Keep it out
of a source manifest.

A manifest records the file hash, the source type, the covered period, and
whether the file is canonical. De-duplicate parallel representations of the same
material explicitly. Handle a malformed or unreadable PDF as an exception, never
as a silent failure.

## Validated design lessons

Local validation confirmed these. They constrain how the skills behave today.

- `bookdisco` inspects row-level documents, not headers alone.
- Shipping revenue may stay separate from product revenue.
- A Stripe fee may appear as a purchase document.
- A fulfillment-provider cost may split across several purchase rows.
- Warehouse identity can matter materially.
- Inventory remnant endpoints belong in discovery and audit where inventory
  exists.
- Audit logic uses the accounting or business date, never the insertion
  timestamp.
- A historical year is a clue set, not unquestionable target behaviour.
- Fulfillment-partner behaviour varies by partner and by year.
