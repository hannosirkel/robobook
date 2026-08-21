# 2024 Bookkeeping Readiness Design

## Goal

Make the Plepic 2024 workflow safe to advance from reproducible dry-run to human approval by fixing foreign-exchange rates, exact entity mappings, canonical source storage, existing-document duplicate suppression, and Printful supplier credits.

The implementation must not post bookkeeping or create master data. Its terminal state is a fresh read-only discovery plus a passing full-year dry-run whose monthly action batches remain `draft` until reviewed.

## Scope

This design changes the reusable workflow rather than adding Plepic-only code. Plepic-specific identifiers and posting choices live in ignored company-local artifacts.

Included:

- annual ECB exchange-rate retrieval and caching through Frankfurter v2
- one exchange rate per foreign-currency summary document
- exact bank-account and contact mapping through a company posting policy
- a separately reviewed PayPal master-data draft when the contact is absent
- canonicalization of the Plepic 2024 source pack
- supplier parsing correction for Simplbooks invoices
- read-only live-document identity indexing and duplicate suppression
- Printful supplier credits in their refund months
- stricter checker and sender preconditions
- full-year regeneration and dry-run verification

Excluded:

- live Simplbooks bookkeeping writes
- live master-data creation
- automatic approval of monthly batches
- legal or tax-policy invention where the company posting policy is incomplete

## Exchange-Rate Architecture

### Retrieval

A focused `scripts/exchange_rates.py` module and command owns exchange-rate mechanics. It uses the Frankfurter v2 time-series endpoint with one query per year and requested foreign-currency set:

```text
https://api.frankfurter.dev/v2/rates?from=2024-01-01&to=2024-12-31&base=USD&quotes=EUR&providers=ECB
```

For multiple foreign currencies, the fetcher requests the required pairs in the smallest number of annual calls supported by the API. Plepic 2024 requires only USD to EUR.

The fetcher uses Python's standard-library HTTP client, parses numeric rates as `Decimal`, validates every returned row, and rejects responses whose provider, base, quote, year, or shape does not match the request.

### Cache

The default cache path is:

```text
companies/<company>/artifacts/reference/ecb-rates-<year>.json
```

The document contains:

- schema version
- provider `ECB`
- year
- base currency and requested quote currencies
- source URL and retrieval timestamp
- daily rows containing `date`, `base`, `quote`, and decimal-string `rate`

Historical cache files are reused indefinitely after validation. Network refresh requires an explicit `--refresh` flag. The cache is a derived reference artifact, not raw company source evidence.

### Lookup

Each foreign-currency monthly summary receives one rate for its `document_date`. If the ECB has no row for that calendar date, lookup walks backward to the latest prior published ECB date. The resolved action records:

- `currency_rate`
- `currency_rate_provider: ECB`
- `currency_rate_requested_date`
- `currency_rate_effective_date`
- `currency_rate_source`

The rate orientation is foreign currency to company base currency. For USD documents in an EUR-base company, the rate is USD to EUR: one USD multiplied by the cached rate yields its EUR value.

There is no numeric fallback. Missing, malformed, inverted, wrong-pair, or out-of-year rates block draft building. `booksend` copies reviewed rate fields into Simplbooks payloads and never substitutes `1`.

## Company Posting Policy

A company-local structured artifact owns deterministic mappings:

```text
companies/<company>/artifacts/posting_policy.json
```

The reusable schema supports:

- bank-account mapping by exact source account identifier such as IBAN
- sales/refund contact mappings by channel
- processor contact mappings
- recurring income-account, expense-account, VAT-type, item, and warehouse mappings
- explicit aliases for known supplier names

Plepic 2024 policy records:

- the source LHV statement account maps to Simplbooks income account ID `3`
- Woo sales and Woo refunds map to `Eraisik`, contact ID `42`
- PayPal requires its own contact and must not fall back to Stripe
- Omniva aliases to the existing Eesti Post contact
- recurring posting families use explicitly reviewed IDs derived from the existing entity map and historical patterns

Exact source identifiers take precedence over names. Fuzzy matching can emit diagnostic suggestions but cannot fill a submit-capable mapping. Missing or ambiguous required mappings lower the action to unresolved status and make `bookchecker` fail.

### PayPal Master Data

If no PayPal contact exists, discovery produces a separate master-data draft for `PayPal Europe S.à r.l. et Cie, S.C.A.`. Monthly PayPal actions depend on that contact being resolved in the entity map. The master-data action remains outside monthly batches and cannot be submitted by the normal `booksend` path without separate explicit approval.

## Canonical Source Intake

The current `temp/2024` pack is copied, not moved, to:

```text
companies/plepic/source/2024-pack/
```

The scratch pack remains untouched. All regenerated manifests and record references use the canonical company path. `.gsheet` files remain ignored, structured files remain preferred over duplicate PDF forms, and README purchase notes remain supporting canonical evidence for unreadable image documents.

`bookchecker` rejects monthly action batches whose source evidence points into `temp/` when a company directory is in use.

## Supplier Parsing And Existing-Document Safety

### Supplier Parsing

Purchase PDF parsing must distinguish supplier from customer/recipient. The Simplbooks invoice dated 2024-11-18 must normalize with Simplbooks OÜ as supplier, not Plepic Games OÜ. Layout-specific extraction wins over generic legal-name fallback.

### Discovery Identity Index

Read-only discovery exposes a normalized existing-document index. Identity fields are:

- document type
- supplier/contact ID when known, plus normalized name
- external invoice or credit number when present
- business date
- currency
- gross amount

An external document number plus compatible type and supplier is the strongest identity. When no external number exists, the full type/name/date/currency/amount tuple is required.

`bookbuilder` loads the current discovery index. Exact existing matches suppress creation and are recorded as `already_present` entries in the batch summary. Ambiguous near-matches block approval and require review. Local idempotency keys remain responsible only for reruns of actions created by this workflow; they are not treated as proof that Simplbooks lacks an independently created document.

The November Simplbooks invoice already present in discovery is therefore suppressed after the parser identifies its supplier correctly.

## Printful Supplier Credits

Refund-only Printful rows become normalized `purchase_credits` records rather than nonblocking exceptions.

Plepic 2024 produces:

- a May supplier credit totaling EUR 11.40
- a July supplier credit totaling EUR 113.12

Every record retains refund date, Printful ID, order/reference text, currency, amount, and source row. Credits are posted in the refund month and do not reopen the original purchase month.

`bookbuilder` groups compatible credits by supplier, currency, tax profile, and month into dedicated `create_purchase_credit_summary` actions. Credits never become ordinary positive purchases and are not silently absorbed into unrelated purchase totals.

`booksend` translates the reviewed credit schema into the Simplbooks-supported purchase-credit request shape. If the visible API cannot represent purchase credits safely, translation fails closed and the actions remain manual-review instructions rather than being posted as negative ordinary purchases.

## Checker And Sender Gates

`bookchecker` fails for:

- foreign actions without validated cached rate provenance
- a foreign action using rate `1` unless the validated ECB cache row is exactly `1`
- source-account evidence that does not map exactly to the action's bank account
- required contacts, accounts, VAT types, items, or warehouses absent from posting policy
- fuzzy fallback mappings used in payloads
- source references under `temp/`
- ordinary purchase actions containing supplier-credit evidence
- unrecognized purchase-credit request shapes
- exact live duplicates still represented as create actions
- ambiguous near-matches against live discovery

`booksend` independently validates rate provenance, mapping resolution, credit schema support, approval status, fresh checker binding, and action-file hash before write mode.

Warnings can remain for informative settlement timing and continuity observations, but no warning may conceal a required posting choice.

## Workflow

The corrected 2024 flow is:

1. Copy the source pack into canonical company storage.
2. Fetch and validate the 2024 ECB rate cache from Frankfurter.
3. Refresh 2024 Simplbooks discovery read-only.
4. Refresh entity maps and produce the separate PayPal master-data draft if needed.
5. Review and complete `posting_policy.json` using discovered IDs.
6. Run `bookprep` for each month, including supplier credits.
7. Run reconciliation with credits and currency-aware checks.
8. Build actions using exact mappings, ECB rates, and live duplicate suppression.
9. Run the stricter checker.
10. Run `booksend --mode dry-run` only.
11. Review all monthly batches and the separate PayPal master-data draft.

No batch becomes `approved` automatically.

## Error Handling

- Frankfurter network or validation failures leave any prior valid historical cache untouched and stop a requested refresh.
- Missing ECB dates fall back only to the latest prior published date; lookup never moves forward.
- A document date before the first cached rate fails with a clear cache-coverage error.
- Unknown source account identifiers block cash actions.
- Missing PayPal contact blocks PayPal payload generation while retaining evidence and a master-data recommendation.
- Unsupported Simplbooks credit semantics fail closed.
- Existing-document ambiguity blocks approval instead of choosing between create and suppress.
- Malformed PDFs remain explicit intake exceptions; supporting purchase notes remain traceable.

## Testing

All production behavior follows test-first development. Focused tests cover:

- annual Frankfurter request construction with `providers=ECB`
- cache validation and decimal preservation
- exact-date and prior-business-day lookup
- pair orientation and rejection of inverted rows
- cache miss and refresh failure behavior
- exact IBAN-to-income-account selection
- prevention of fuzzy bank/contact fallback
- Woo-to-Eraisik and PayPal-contact-required policy behavior
- Simplbooks supplier extraction
- exact live duplicate suppression and ambiguous near-match blocking
- May and July Printful supplier-credit normalization and totals
- purchase-credit build, check, and translation behavior
- canonical source-reference enforcement
- sender rejection of missing/unvalidated rates
- full-year dry-run regression

The full existing unit suite must pass. Scripts must compile. A fresh isolated full-year 2024 dry-run must complete without blocking errors, must use canonical source paths, must contain no unjustified foreign rate of `1`, and must not contain the duplicate Simplbooks purchase.

## Verification And Completion Boundary

Implementation is complete only when:

- the annual ECB cache is fetched and validated
- the canonical source pack exists and artifacts reference it
- focused and full tests pass
- fresh read-only discovery is captured
- every 2024 month completes normalization, reconciliation, build, checker, and dry-run
- all 2024 foreign actions carry audited ECB rate provenance
- cash actions use exact LHV mapping
- Woo actions use Eraisik rather than Stripe
- PayPal has an explicit unresolved dependency or reviewed dedicated contact, never a Stripe fallback
- Printful credits total EUR 11.40 in May and EUR 113.12 in July
- the already-present Simplbooks purchase is suppressed
- no live API mutation was attempted

Human review, PayPal master-data approval, monthly action approval, live submission, and post-submit audit remain subsequent steps.
