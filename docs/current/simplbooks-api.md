# The Simplbooks API boundary

Simplbooks is the accounting system of record. This repository reads it, drafts
against it, and writes only an approved action batch. It never becomes a second
system of record.

## Access

| Item | Value |
| --- | --- |
| Auth header | `X-Simplbooks-Token` |
| Base URL | `https://app.simplbooks.com/{company_id}/api` |
| Token location | `.apikey`, local and ignored by Git |
| Published rate limit | 60 requests per minute |

Reusable access lives in [`scripts/simplbooks_api.py`](../../scripts/simplbooks_api.py).
Read-only year discovery lives in
[`scripts/examine_simplbooks_year.py`](../../scripts/examine_simplbooks_year.py).

```bash
python3 scripts/simplbooks_api.py --company-dir companies/<company> \
  call financial_accounts/get/101

python3 scripts/examine_simplbooks_year.py --company-dir companies/<company> \
  --year 2023 \
  --output companies/<company>/artifacts/discovery/2023-overview.json
```

## Known quirks

The public documentation and local validation have shown these. Do not
generalise past them.

- Some list endpoints are documented as `GET` with a request body.
- Published date formats are inconsistent — the reference says `yyyy-MM-dd`, several
  list examples show `01-01-2021`. Verify what an endpoint accepts; do not assume.
- **The endpoint names do not mean what they look like**, and two of them are
  separate ID spaces that are easy to confuse:

  | Endpoint | Actually holds |
  | --- | --- |
  | `financial_accounts` | the chart of accounts (ledger codes) |
  | `income_accounts` | bank accounts and cash registers (payment accounts) |
  | `incomings` | receipts |

  A payment carries an `income_account_id`, which is a *payment* account — not a
  ledger account id. Passing a ledger id there resolves to the wrong account or
  fails obscurely.
- The wrapper names are inconsistent by document type:

  | Document | Wrapper key |
  | --- | --- |
  | invoice list item | `invoices` |
  | purchase | `Purchase` |
  | receipt | `Incoming` |
  | payment | `Payment` |

- The period-relevant date differs by document type: `created`,
  `transaction_date`, `income_date`, `payment_date`, `created_time`.
- **Never use `created_time` as the accounting-period signal.**
- Never assume document numbering is monotonic within a period.

## What the API exposes

Business documents, items, warehouses, VAT types, and accounts.

No generic journal-entry endpoint and no trial-balance endpoint are confirmed
from the public specification. There is no inventory write-off endpoint, which
is why a write-off uses a manual-decision contract instead. Never translate a
manual write-off into an invoice, a purchase, or another unrelated call.
