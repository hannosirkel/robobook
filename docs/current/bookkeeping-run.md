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

## Cash posting modes

A company declares `cash_posting.mode` in its posting policy, and a batch carries
the mode it was built under.

| Mode | Cash comes from |
| --- | --- |
| `api` | the batch posts receipts and payments itself |
| `statement_import` | the bank statement is imported in the Simplbooks UI, and the batch posts no cash for an imported account |

Under `statement_import` a plan row is the terminal coverage item for a physical
bank row. A batch declaring one mode may not be written under a policy declaring
the other, and an API bank-cash action is refused outright.

Processor accounts have no import queue, so a payment against one *is* the
settlement. Bank accounts do have one, so a bank settlement is made by matching
the statement row, never by posting a payment through the API.

## Bank statement completeness

**Every canonical physical statement row has exactly one reviewed disposition**,
leading to an exact receipt, an exact payment, a verified match to an existing
Simplbooks cash transaction, or a reviewed transfer identified on both sides. An
unresolved, ignored, or merely inferred row blocks the month. A row referenced by
more than one settlement group also blocks it, unless a split allocation proves
the parts sum to the row.

**Processor balances and supplier wallets are not physical bank accounts.** They
normalize separately and reconcile to card charges, payouts, expenses, refunds
and opening/closing balances. A clearing movement may support a physical-bank
disposition, but never counts as an extra statement row.

**A physical bank ledger is identified by `(IBAN, currency)`, not IBAN alone.**
One IBAN may hold EUR and USD sub-ledgers. Posting policy resolves
`<IBAN>|<ISO-4217 currency>`; an IBAN-only mapping is valid for base currency
only and never silently authorizes a foreign-currency row.

Cash actions take the statement row's own business date, currency, amount and
source reference. A month-end date is not permitted for a physical cash row.

## Evidence binding and ordering

A batch records each input it was built from as a `reference_artifacts` entry
with a sha256. `booksend` re-verifies every one before writing, so a rebuilt
input invalidates the batch rather than being silently accepted.

Write eligibility requires all of: recon approves the month; physical bank
coverage is complete and arithmetically exact; clearing reconciles with no
unexplained movement; `bookchecker` reports no errors and no unreviewed
warnings; the batch is approved and bound to the current check report; discovery
is fresh; no earlier required month is unsubmitted; and **a submitted action YAML
is never regenerated or mutated**.

Periods are written in order. Each configured period requires the immediately
preceding one to carry a successful, immutable write log.

## Annual evidence and effective-dated VAT

Some evidence is annual rather than monthly, and is reviewed once for the year:

```text
artifacts/bank/<year>-allocations.json          reviewed disposition per physical row
artifacts/vat/<year>-woo-tax-allocation.json    reported taxable order → monthly sale
artifacts/reference/ecb-rates-<year>.json       reviewed rate cache
artifacts/statement-import/<year>-plan.json     physical rows to match in the UI
```

A Woo tax summary is parsed as supporting evidence, then a reviewed annual
allocation links every reported taxable order to a monthly sale. That boundary
between source fact and accounting policy is what keeps a rerun deterministic.
The allocation requires annual coverage, which comes from the source-pack
directory name or the filename.

A VAT rate that changes mid-year is expressed as **dated bands**, not a single
value. Customer-paid gross is preserved; the VAT split is derived from it.

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
