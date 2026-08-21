# Bank Statement Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every physical bank-statement row resolve exactly once to an accounting transaction or verified existing SimplBooks transaction before a month can be approved or submitted.

**Architecture:** Introduce a source-bound reviewed bank-allocation contract, separate physical bank rows from clearing-ledger movements, and make reconciliation, action generation, checking, and submission consume the same exact allocations independently. Physical settlement actions retain statement date, currency, amount, and source identity; monthly business documents remain possible without aggregating away the underlying cash rows.

**Tech Stack:** Python 3 standard library, JSON Schema Draft 2020-12, PyYAML/Ruby YAML fallback, `unittest`, SimplBooks REST API wrappers.

**Spec:** `plans/2026-08-22-BANK-COMPLETENESS-DESIGN.md`

## Global Constraints

- Real-company data remains only in ignored `companies/<company>/` artifacts.
- `bank_transactions` contains only canonical physical bank-statement rows.
- `clearing_transactions` contains processor and supplier-wallet movements.
- Every physical bank row has exactly one reviewed disposition; there is no `ignore` disposition.
- Physical bank coverage and balances are keyed by `(IBAN, currency)`.
- Stable allocation identity uses the bank's archive/account-servicer/entry reference before a row-number locator.
- Cash actions preserve statement business date, source currency, amount, source account, and immutable record reference.
- Foreign-currency actions bind the annual ECB rate cache.
- No undocumented journal or statement-import endpoint is assumed.
- No live SimplBooks write occurs during implementation or verification.
- Existing master-data creation remains separately approved.
- Submitted YAML batches are immutable and must never be regenerated.
- Stripe fees remain transaction-history based; monthly invoices are supporting evidence only.
- FX revaluation and work requiring an unconfirmed journal endpoint are out of scope.

---

## Phase A: Report-Only Coverage Foundation

Phase A changes normalization, discovery, allocation validation, and reconciliation reporting. It does not make new settlement actions write-capable and ends with a reviewable list of every unresolved decision.

### Task 1: Reviewed Bank-Allocation Contract

**Files:**
- Create: `scripts/bank_allocations.py`
- Create: `schemas/bank-allocation.schema.json`
- Create: `templates/bank-allocation.template.json`
- Modify: `tests/test_schema_contracts.py`
- Create: `tests/test_bank_allocations.py`
- Modify: `scripts/reference_artifacts.py`
- Modify: `tests/test_reference_artifacts.py`

**Interfaces:**
- Produces: `load_bank_allocations(path: Path, *, normalized_year_paths: list[Path]) -> dict[str, Any]`
- Produces: `period_allocations(payload: dict[str, Any], period: str) -> dict[str, dict[str, Any]]`
- Produces: `statement_identity(record: dict[str, Any]) -> str`.
- Produces: `rebind_bank_allocations(payload: dict[str, Any], normalized_year_paths: list[Path]) -> dict[str, Any]` for reviewable locator/hash refresh only when identities and economic fields are unchanged.
- Produces: `allocation_amounts(allocation: dict[str, Any]) -> list[Decimal]`
- Produces: reference-artifact kind `bank_allocations`.

- [ ] **Step 1: Add failing schema tests for supported dispositions and rejected escape hatches**

```python
def test_bank_allocation_schema_accepts_exact_reviewed_disposition(self) -> None:
    payload = bank_allocation_payload(
        allocations=[{
            "statement_id": "archive:2024010212345678",
            "record_id": "bank-source:bank:2",
            "period": "2024-01",
            "disposition": "existing_invoice_receipt",
            "amount": 330.0,
            "currency": "EUR",
            "target": {"simplbooks_id": "119", "document_type": "invoice"},
            "review": {"status": "approved", "rationale": "Exact invoice number and amount."},
        }]
    )
    validate_schema("bank-allocation.schema.json", payload)

def test_bank_allocation_schema_rejects_ignore(self) -> None:
    payload = bank_allocation_payload(allocations=[allocation(disposition="ignore")])
    with self.assertRaises(ValidationError):
        validate_schema("bank-allocation.schema.json", payload)
```

- [ ] **Step 2: Run the contract tests and verify they fail because the schema/module is absent**

Run: `python3 -m unittest tests.test_schema_contracts tests.test_bank_allocations -v`

Expected: FAIL for missing `bank-allocation.schema.json` or `bank_allocations` import.

- [ ] **Step 3: Add the schema, template, and strict loader**

The top-level contract is:

```json
{
  "schema_version": "1.0",
  "company_slug": "example",
  "year": 2024,
  "normalized_bindings": [
    {"path": "companies/example/artifacts/normalized/2024-01.json", "sha256": "<64 hex>"}
  ],
  "allocations": []
}
```

Implement strict validation in `scripts/bank_allocations.py`:

```python
DISPOSITIONS = {
    "generated_invoice_receipt", "existing_invoice_receipt",
    "generated_purchase_payment", "existing_purchase_payment",
    "direct_sale_receipt", "bank_fee_payment",
    "clearing_transfer", "reviewed_split",
}

def load_bank_allocations(path: Path, *, normalized_year_paths: list[Path]) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    verify_normalized_bindings(payload, normalized_year_paths)
    validate_unique_record_ids(payload["allocations"])
    validate_reviewed_amounts(payload["allocations"])
    return payload
```

Require exact `statement_id`, current `record_id`, `period`, `disposition`, `amount`, `currency`, `target`, and approved review metadata. Resolve identities in this order: archive identifier, account-servicer reference, entry reference, then deterministic composite. A changed row number with the same immutable `statement_id` remains resolvable; a changed economic identity does not. `rebind_bank_allocations` may update normalized hashes and row locators only when the full statement-ID set and every date/currency/signed-amount tuple are unchanged, and its output must be reviewed before replacing the prior artifact. `reviewed_split` requires non-empty parts whose sum equals `amount` to €0.01 precision.

- [ ] **Step 4: Bind bank allocations as a first-class reference artifact**

Add `bank_allocations` to the allowed reference-artifact kinds and required binding calculation when a batch contains a physical-bank source reference.

- [ ] **Step 5: Run focused tests and verify they pass**

Run: `python3 -m unittest tests.test_schema_contracts tests.test_bank_allocations tests.test_reference_artifacts -v`

Expected: PASS.

- [ ] **Step 6: Commit the contract**

```bash
git add scripts/bank_allocations.py schemas/bank-allocation.schema.json templates/bank-allocation.template.json tests/test_bank_allocations.py tests/test_schema_contracts.py scripts/reference_artifacts.py tests/test_reference_artifacts.py
git commit -m "feat: define reviewed bank allocation contract"
```

### Task 2: Separate Physical Bank And Clearing Ledgers

**Files:**
- Modify: `schemas/normalized-period.schema.json`
- Modify: `templates/normalized-period.template.json`
- Modify: `scripts/bookprep.py`
- Modify: `tests/test_bookprep.py`
- Modify: `tests/test_schema_contracts.py`
- Modify: `scripts/examine_simplbooks_year.py`
- Modify: `tests/test_simplbooks_api.py`
- Modify: `schemas/year-overview.schema.json`
- Modify: `skills/bookprep/SKILL.md`
- Modify: `skills/bookprep/references/bookprep.md`

**Interfaces:**
- Consumes: normalized-record contract already used by `bank_transactions`.
- Produces: `records["clearing_transactions"]: list[dict[str, Any]]`.
- Produces: `records["bank_balances"]: list[dict[str, Any]]` from CAMT XML.
- Guarantees: every `bank_transactions` record has `source_system == "bank"`.
- Produces: complete discovery `document_index` entries for invoices, purchases, incomings, and payments.

- [ ] **Step 1: Add failing normalization tests for Printful wallet movements**

```python
def test_printful_wallet_rows_are_clearing_not_physical_bank(self) -> None:
    records = bookprep.empty_records()
    bookprep.parse_printful_wallet_csv(source(), fixture_path, records, ...)
    self.assertEqual(records["bank_transactions"], [])
    self.assertEqual(len(records["clearing_transactions"]), 2)
    self.assertEqual(records["clearing_transactions"][0]["attributes"]["clearing_provider"], "printful")

def test_bank_csv_still_emits_only_physical_bank_rows(self) -> None:
    records = parse_bank_fixture()
    self.assertTrue(records["bank_transactions"])
    self.assertTrue(all(row["source_system"] == "bank" for row in records["bank_transactions"]))

def test_camt_xml_emits_balances_without_duplicate_bank_rows(self) -> None:
    records = parse_paired_csv_and_camt()
    self.assertTrue(records["bank_balances"])
    self.assertEqual(len(records["bank_transactions"]), 2)  # CSV rows only

def test_discovery_indexes_every_cash_transaction(self) -> None:
    overview = examine_year(fake_client_with_incoming_and_payment())
    cash = [item for item in overview["document_index"] if item["document_type"] in {"incoming", "payment"}]
    self.assertEqual({item["simplbooks_id"] for item in cash}, {"601", "701"})
    self.assertTrue(all("document_date" in item and "gross_amount" in item for item in cash))
```

- [ ] **Step 2: Run the focused tests and verify the first test fails**

Run: `python3 -m unittest tests.test_bookprep.BookprepTests.test_printful_wallet_rows_are_clearing_not_physical_bank -v`

Expected: FAIL because wallet rows are currently appended to `bank_transactions`.

- [ ] **Step 3: Add `clearing_transactions` and `bank_balances` to empty records, schema, and template**

Add the category to every explicit normalized-record category list. Keep schema version `1.0`; readers use `records.get("clearing_transactions", [])` for compatibility with historical fixtures.

- [ ] **Step 4: Route Printful wallet funding/refunds to clearing records**

Preserve current signed amounts and immutable IDs, adding:

```python
attributes={
    **existing_attributes,
    "clearing_provider": "printful",
    "clearing_account": "printful_wallet",
}
```

Do not synthesize another physical-bank row from the wallet export.

- [ ] **Step 5: Parse CAMT balances as supporting evidence without duplicating CSV rows**

Retain CSV as canonical transaction rows. Parse CAMT `Bal` nodes into `bank_balances` keyed by IBAN, currency, balance type, and date even when the paired XML transaction representation is non-canonical. Cross-check CSV immutable references against CAMT `AcctSvcrRef` where available.

- [ ] **Step 6: Index every live incoming and payment in discovery**

Extend `document_index` entries with `document_type`, `simplbooks_id`, linked invoice/purchase ID when exposed, business date, amount, currency, income account, counterparty, and description. Preserve indices from prior discovery years so allocations may target earlier live invoices or cash transactions.

- [ ] **Step 7: Update bookprep documentation and run focused tests**

Run: `python3 -m unittest tests.test_bookprep tests.test_simplbooks_api tests.test_schema_contracts -v`

Expected: PASS.

- [ ] **Step 8: Commit physical/clearing separation and full cash discovery**

```bash
git add schemas/normalized-period.schema.json schemas/year-overview.schema.json templates/normalized-period.template.json scripts/bookprep.py scripts/examine_simplbooks_year.py tests/test_bookprep.py tests/test_simplbooks_api.py tests/test_schema_contracts.py skills/bookprep/SKILL.md skills/bookprep/references/bookprep.md
git commit -m "fix: separate bank ledgers and index live cash"
```

### Task 3: Physical-Bank Coverage And Clearing Reconciliation

**Files:**
- Modify: `scripts/bookrecon.py`
- Modify: `schemas/recon-period.schema.json`
- Modify: `templates/recon-period.template.json`
- Modify: `tests/test_bookrecon.py`
- Modify: `skills/bookrecon/SKILL.md`
- Modify: `skills/bookrecon/references/bookrecon.md`

**Interfaces:**
- Consumes: `period_allocations(...)` and normalized physical/clearing categories.
- Produces: recon checks `physical-bank-coverage` and `clearing-continuity:<provider>:<currency>`.
- Produces: `bank_coverage` summary with row counts and movement totals.

- [ ] **Step 1: Add failing tests for missing, duplicate, split, and complete coverage**

```python
def test_recon_fails_when_one_physical_bank_row_has_no_allocation(self) -> None:
    recon = build_recon(bank_rows=[bank_row("a", 20), bank_row("b", -2)], allocations=[alloc("a", 20)])
    check = find_check(recon, "physical-bank-coverage")
    self.assertEqual(check["status"], "fail")
    self.assertIn("b", check["notes"][0])
    self.assertFalse(recon["approve_for_build"])

def test_recon_passes_exact_reviewed_split(self) -> None:
    recon = build_recon(
        bank_rows=[bank_row("a", -30)],
        allocations=[split_alloc("a", -30, parts=[-10, -20])],
    )
    self.assertEqual(find_check(recon, "physical-bank-coverage")["status"], "pass")
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `python3 -m unittest tests.test_bookrecon -v`

Expected: FAIL because reconciliation currently has no allocation-aware coverage check.

- [ ] **Step 3: Add allocation CLI/path resolution and coverage evaluation**

Add `--bank-allocations`, defaulting to `artifacts/bank/<year>-allocations.json`. Implement:

```python
def build_physical_bank_coverage_check(
    *, normalized_path_display: str,
    bank_records: list[dict[str, Any]],
    allocations: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ...
```

Compare exact signed amount, currency, period, immutable statement identity, and `(IBAN, currency)` ledger. Missing or extra allocations fail. Report credit total, debit total, net movement, CAMT opening balance, computed closing balance, and CAMT closing balance by `(IBAN, currency)`.

- [ ] **Step 4: Add clearing continuity checks**

Group by structured `clearing_provider`, clearing account, and currency. Require each movement to be referenced by an allocation or an existing processor bridge. When balance fields exist, prove opening + movements = closing; otherwise emit an evidence-specific warning that blocks approval until reviewed by policy.

- [ ] **Step 5: Update recon schema/docs and run focused tests**

Run: `python3 -m unittest tests.test_bookrecon tests.test_schema_contracts -v`

Expected: PASS.

- [ ] **Step 6: Commit reconciliation invariants**

```bash
git add scripts/bookrecon.py schemas/recon-period.schema.json templates/recon-period.template.json tests/test_bookrecon.py skills/bookrecon/SKILL.md skills/bookrecon/references/bookrecon.md
git commit -m "feat: require complete bank row reconciliation"
```

- [ ] **Step 7: Stop for the Phase A decision-list review**

Regenerate report-only 2024/2025 normalization and reconciliation. Provide the complete unresolved allocation/master-data list before beginning write-capable generation.

## Phase B: Reviewed Cash And Business-Document Generation

Phase B implements only reviewed families and exact target resolution. It does not enable final send hard-blocks until the generated calls and server-side pilot requirements are reviewed.

### Task 4: Exact Cash Actions And Document Targets

**Files:**
- Modify: `scripts/bookbuilder.py`
- Modify: `scripts/booksend.py`
- Modify: `tests/test_bookbuilder.py`
- Modify: `tests/test_booksend.py`
- Modify: `schemas/action-batch.schema.json`
- Modify: `skills/bookbuilder/SKILL.md`
- Modify: `skills/bookbuilder/references/bookbuilder.md`

**Interfaces:**
- Consumes: approved allocation entries plus current/prior action batches and discovery document index.
- Produces: one exact `cash_settlement_v1` action per physical row or reviewed split part.
- Adds payload targets: `linked_invoice_action`, `linked_invoice_id`, `linked_purchase_action`, `linked_purchase_id`.

- [ ] **Step 1: Add failing builder tests for exact dates and existing document IDs**

```python
def test_existing_manual_invoice_receipt_uses_statement_date_and_id(self) -> None:
    batch = build_with(
        bank=bank_row("receipt", 330, event_date="2024-01-08"),
        allocation=existing_invoice_alloc("receipt", invoice_id="119"),
    )
    action = find_action(batch, "create_incoming_summary")
    self.assertEqual(action["payload"]["document_date"], "2024-01-08")
    self.assertEqual(action["payload"]["linked_invoice_id"], "119")
    self.assertEqual(action["source_refs"][0]["record_ref"], "receipt")

def test_multiple_bank_receipts_create_multiple_actions_against_one_invoice(self) -> None:
    batch = build_with_two_receipts_one_generated_invoice()
    receipts = actions_of_type(batch, "create_incoming_summary")
    self.assertEqual([a["payload"]["amount"] for a in receipts], [20.0, 20.0])
    self.assertEqual({a["payload"]["linked_invoice_action"] for a in receipts}, {"example-2024-08-sales-direct"})
```

- [ ] **Step 2: Add failing translation tests for existing target IDs**

```python
def test_translate_incoming_accepts_existing_invoice_id(self) -> None:
    translated = booksend.translate_cash_settlement_payload(existing_invoice_incoming(), lookup={})
    self.assertEqual(translated["payload"]["invoice_id"], 119)

def test_translate_payment_accepts_existing_purchase_id(self) -> None:
    translated = booksend.translate_cash_settlement_payload(existing_purchase_payment(), lookup={})
    self.assertEqual(translated["payload"]["purchase_id"], 88)
```

- [ ] **Step 3: Run focused tests and verify failure**

Run: `python3 -m unittest tests.test_bookbuilder tests.test_booksend -v`

Expected: FAIL because settlement generation is payout/purchase-heuristic based and existing target IDs are unsupported.

- [ ] **Step 4: Replace physical settlement heuristics with allocation-driven generation**

Add `--bank-allocations` to `bookbuilder`. Resolve every allocation target exactly. Preserve current processor payout and purchase records as supporting references, but let the physical row control `document_date`, `amount`, `currency`, and bank account.

Use stable keys containing the physical record identity hash so two same-day same-counterparty rows remain distinct:

```python
def settlement_action_key(company_slug: str, period: str, role: str, record_id: str, part: int | None = None) -> str:
    digest = hashlib.sha256(record_id.encode()).hexdigest()[:12]
    suffix = f"-{part}" if part is not None else ""
    return f"{company_slug}-{period}-{role}-{digest}{suffix}"
```

- [ ] **Step 5: Resolve generated targets across prior months without mutating them**

Build a read-only historical action index from earlier action YAML and successful submission logs. Generated target actions must either be in the current batch or have a successful earlier submission with an inserted ID. Do not regenerate earlier YAML.

Existing live targets may come from any bound discovery year. Bind every discovery overview used to prove an invoice, purchase, incoming, or payment target.

- [ ] **Step 6: Translate direct existing IDs safely**

In `translate_cash_settlement_payload`, prefer explicit reviewed `linked_invoice_id`/`linked_purchase_id`; otherwise resolve generated action dependencies. Reject simultaneous existing and generated target fields.

- [ ] **Step 7: Bind the allocation artifact in the action batch and run focused tests**

Run: `python3 -m unittest tests.test_bookbuilder tests.test_booksend tests.test_reference_artifacts -v`

Expected: PASS.

- [ ] **Step 8: Commit exact settlement generation**

```bash
git add scripts/bookbuilder.py scripts/booksend.py tests/test_bookbuilder.py tests/test_booksend.py schemas/action-batch.schema.json skills/bookbuilder/SKILL.md skills/bookbuilder/references/bookbuilder.md
git commit -m "feat: build exact bank settlement actions"
```

### Task 5: Direct Sales, Bank Fees, And Netted Foreign Fees

**Files:**
- Modify: `scripts/bookbuilder.py`
- Modify: `scripts/bookchecker.py`
- Modify: `scripts/booksend.py`
- Modify: `tests/test_bookbuilder.py`
- Modify: `tests/test_bookchecker.py`
- Modify: `tests/test_booksend.py`
- Modify: `schemas/posting-policy.schema.json`
- Modify: `templates/posting-policy.template.json`

**Interfaces:**
- Consumes: `direct_sale_receipt`, `bank_fee_payment`, and foreign net-fee allocations.
- Produces: monthly direct-sale invoices plus per-row receipts.
- Produces: supported fee purchases plus per-row payments.

- [ ] **Step 1: Add failing direct-sale tests**

```python
def test_direct_sales_group_one_monthly_invoice_but_keep_exact_receipts(self) -> None:
    batch = build_direct_sales(
        rows=[bank_credit("a", 20, "2024-08-27"), bank_credit("b", 20, "2024-08-30")],
        contact_id="42",
    )
    invoices = actions_of_type(batch, "create_invoice_summary")
    receipts = actions_of_type(batch, "create_incoming_summary")
    self.assertEqual(len(invoices), 1)
    self.assertEqual(invoices[0]["payload"]["counterparty"]["contact_id"], "42")
    self.assertEqual(invoices[0]["payload"]["totals"]["gross_amount"], 40.0)
    self.assertEqual([r["payload"]["document_date"] for r in receipts], ["2024-08-27", "2024-08-30"])
```

- [ ] **Step 2: Add failing fee and foreign-net tests**

```python
def test_bank_fee_purchase_can_have_multiple_exact_payments(self) -> None:
    batch = build_fee_rows([bank_debit("monthly-card", -2), bank_debit("transfer", -7)])
    self.assertEqual(len(actions_of_type(batch, "create_purchase_summary")), 1)
    self.assertEqual(len(actions_of_type(batch, "create_payment_summary")), 2)

def test_netted_foreign_receipt_closes_gross_and_preserves_net_bank_amount(self) -> None:
    batch = build_foreign_receipt(invoice_amount=Decimal("738.32"), bank_amount=Decimal("723.32"), fee=Decimal("15.00"))
    receipt = find_action(batch, "create_incoming_summary")
    fee = find_action(batch, "create_purchase_summary", family="bank-correspondent-fee")
    self.assertEqual(receipt["payload"]["amount"], 723.32)
    self.assertEqual(fee["payload"]["totals"]["gross_amount"], 15.0)
```

- [ ] **Step 3: Run focused tests and verify failure**

Run: `python3 -m unittest tests.test_bookbuilder tests.test_bookchecker tests.test_booksend -v`

Expected: FAIL because these allocation families do not yet generate documents.

- [ ] **Step 4: Add direct-sale and fee posting families**

Extend posting-policy validation with ordinary mapping families rather than company-specific keys. Direct-sale allocation supplies product description, quantity, gross amount, reviewed VAT profile, sales contact, and optional warehouse. Bank-fee allocation supplies expense account, VAT type, and supplier contact.

Add `(IBAN, currency)` bank-account resolution using policy key `<IBAN>|<currency>`. An IBAN-only legacy mapping is accepted only for the base currency. Current discovery has no separate USD income account, so the private reviewed mapping uses the existing LHV account ID with `currency_name="USD"` and the bound ECB `currency_rate`, subject to the Task 9 first-live-use pilot.

- [ ] **Step 5: Build monthly direct-sale documents and exact receipts**

Group only allocations sharing period, currency, contact, VAT profile, income account, and warehouse. Each physical receipt depends on the grouped invoice. Preserve all physical row refs on the invoice and exactly one row ref on each receipt.

- [ ] **Step 6: Build fee documents, physical payments, and netted-fee bridge**

For statement fees, group compatible expense support monthly but keep one payment per physical row. For netted foreign fees, verify `gross settlement - supported fee == physical receipt`; bind the ECB rate for every foreign action.

- [ ] **Step 7: Make checker prove the bridge and run focused tests**

Run: `python3 -m unittest tests.test_bookbuilder tests.test_bookchecker tests.test_booksend tests.test_posting_policy -v`

Expected: PASS.

- [ ] **Step 8: Commit direct sales and fee support**

```bash
git add scripts/bookbuilder.py scripts/bookchecker.py scripts/booksend.py tests/test_bookbuilder.py tests/test_bookchecker.py tests/test_booksend.py schemas/posting-policy.schema.json templates/posting-policy.template.json
git commit -m "feat: account for direct sales and bank fees"
```

- [ ] **Step 9: Stop for the Phase B translated-call review**

Produce exact dry-run calls for every new disposition family and identify which first-live-use pilots remain unverified.

## Phase C: Hard Eligibility, Freeze, And Final Readiness

### Task 6: Independent Checker And Submission Freeze

**Files:**
- Modify: `scripts/bookchecker.py`
- Modify: `scripts/booksend.py`
- Modify: `tests/test_bookchecker.py`
- Modify: `tests/test_booksend.py`
- Modify: `skills/bookchecker/SKILL.md`
- Modify: `skills/bookchecker/references/bookchecker.md`
- Modify: `skills/booksend/SKILL.md`
- Modify: `skills/booksend/references/booksend.md`

**Interfaces:**
- Consumes: normalized rows, allocation artifact, recon proof, discovery binding, action batch, checker hash, and earlier submission logs.
- Produces: blocking `bank_statement_completeness` findings and immutable submitted-batch enforcement.

- [ ] **Step 1: Add failing checker tests for exact-once coverage and physical attributes**

```python
def test_checker_errors_when_action_batch_omits_one_bank_row(self) -> None:
    report = check_batch(normalized=two_bank_rows(), actions=one_settlement(), allocations=two_allocations())
    self.assertFinding(report, severity="error", text="uncovered physical bank row")

def test_checker_errors_on_month_end_date_substitution(self) -> None:
    report = check_batch(normalized=bank_row_on("2024-08-27"), actions=receipt_on("2024-08-31"))
    self.assertFinding(report, severity="error", text="statement date")
```

- [ ] **Step 2: Add failing submission tests for stale predecessor and submitted YAML mutation**

```python
def test_write_rejects_when_previous_required_month_is_not_successful(self) -> None:
    with self.assertRaisesRegex(SimplbooksError, "previous month"):
        run_write(period="2025-02", prior_submission=None)

def test_write_rejects_changed_yaml_after_successful_submission(self) -> None:
    with self.assertRaisesRegex(SimplbooksError, "submitted batch is immutable"):
        run_write(actions=changed_after_submission())
```

- [ ] **Step 3: Run focused tests and verify failure**

Run: `python3 -m unittest tests.test_bookchecker tests.test_booksend -v`

Expected: FAIL because complete bank proof and submitted-file immutability are not enforced.

- [ ] **Step 4: Add independent bank coverage evaluation to checker**

Resolve every physical normalized record and allocation without trusting recon conclusions. Support reviewed splits by comparing the sum of action allocations to the signed physical amount. Error on missing, duplicate, extra, stale, wrong-date, wrong-currency, or wrong-account references.

- [ ] **Step 5: Add batch-wide submission prevalidation**

Before the first API request, validate reference hashes, approval/checker binding, discovery freshness, complete bank proof, target dependencies, and predecessor submission state. Store the successful action-file SHA in the submission log and reject future mismatches.

- [ ] **Step 6: Remove obsolete generic warnings**

Delete fallback-contact and zero-VAT shipping warnings when an exact policy mapping resolves them. An approved batch may contain informational notes, but no unresolved low/medium-confidence accounting judgment.

Redefine confidence so informational notes do not lower it:

```python
def review_confidence(*, open_issues: list[str], required_ids: list[str | None]) -> str:
    if any(value in (None, "") for value in required_ids):
        return "low"
    if open_issues:
        return "medium"
    return "high"
```

Keep provenance in `review_notes`; pass only actual unresolved judgments as `open_issues`.

- [ ] **Step 7: Run focused tests and commit**

Run: `python3 -m unittest tests.test_bookchecker tests.test_booksend -v`

Expected: PASS.

```bash
git add scripts/bookchecker.py scripts/booksend.py tests/test_bookchecker.py tests/test_booksend.py skills/bookchecker/SKILL.md skills/bookchecker/references/bookchecker.md skills/booksend/SKILL.md skills/booksend/references/booksend.md
git commit -m "fix: block incomplete or mutated bank batches"
```

### Task 7: Chronological Full-Year Orchestration

**Files:**
- Modify: `scripts/full_year_dry_run.py`
- Modify: `tests/test_full_year_dry_run.py`
- Create: `scripts/live_month_run.py`
- Create: `tests/test_live_month_run.py`
- Modify: `plans/SKILLPLAN.md`

**Interfaces:**
- Consumes: annual allocation path and existing month artifacts.
- Produces: dry-run summaries with physical-bank coverage totals.
- Produces: a fail-closed single-month live orchestrator; it does not bypass explicit write confirmation.

- [ ] **Step 1: Add failing orchestration tests**

```python
def test_full_year_passes_bank_allocations_to_recon_builder_and_checker(self) -> None:
    calls = planned_calls(year=2024, bank_allocations="alloc.json")
    self.assertTrue(all("--bank-allocations" in call for call in relevant_calls(calls)))

def test_live_runner_refuses_to_regenerate_submitted_month(self) -> None:
    with self.assertRaisesRegex(SimplbooksError, "already submitted"):
        live_month_run.run(period="2024-03", submission=successful_submission())

def test_full_year_dry_run_skips_submitted_month_even_with_force_build(self) -> None:
    result = run_year(force_build=True, submission=successful_submission("2024-03"))
    self.assertEqual(result.month("2024-03")["status"], "skipped_submitted")
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python3 -m unittest tests.test_full_year_dry_run tests.test_live_month_run -v`

Expected: FAIL because the allocation argument and live runner do not exist.

- [ ] **Step 3: Thread annual allocations through full-year dry runs**

Resolve `artifacts/bank/<year>-allocations.json`, pass it to recon/builder/checker, and include physical row count, allocated count, uncovered count, clearing movement count, and unresolved clearing count in the year summary. Skip successfully submitted months whose action SHA is unchanged; `--force-build` must not override that freeze.

- [ ] **Step 4: Implement fail-closed single-month live orchestration**

`live_month_run.py` performs exactly: discovery refresh, build, checker, approval-state verification, checker rerun, and explicitly confirmed booksend write. It refuses submitted periods, a missing predecessor, stale discovery, draft state, checker mismatch, or any unresolved warning. It never edits approval automatically.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.test_full_year_dry_run tests.test_live_month_run -v`

Expected: PASS.

```bash
git add scripts/full_year_dry_run.py scripts/live_month_run.py tests/test_full_year_dry_run.py tests/test_live_month_run.py plans/SKILLPLAN.md
git commit -m "feat: enforce chronological live month runs"
```

### Task 8: Build And Review The Private 2024/2025 Allocations

**Files:**
- Create ignored: `companies/<company>/artifacts/bank/2024-allocations.json`
- Create ignored: `companies/<company>/artifacts/bank/2025-allocations.json`
- Modify ignored: `companies/<company>/artifacts/posting_policy.json`
- Modify ignored: `companies/<company>/artifacts/pre-submit-readiness.md`
- Regenerate ignored: normalized, recon, action, checker, and dry-run artifacts for both years.

**Interfaces:**
- Consumes: all physical bank rows, clearing movements, discovery document index, source documents, posting policy, and ECB caches.
- Produces: reviewed dispositions for every physical row and explicit blockers for missing evidence/master data.

- [ ] **Step 1: Regenerate normalized artifacts with physical/clearing separation**

Run month-by-month `bookprep` for 2024 and 2025. Confirm the physical count equals the canonical statement count and Printful wallet rows appear only in clearing records.

- [ ] **Step 2: Generate allocation candidates without approving fuzzy matches**

Populate exact candidates from document number, immutable reference, amount/currency, and current/prior action identity. Leave ambiguous rows without approved dispositions.

- [ ] **Step 3: Classify the reviewed direct Lunar Base sales to `Eraisik` contact ID `42`**

The private allocation must cover these eight physical rows representing nine units:

- 2024-08-27, one unit, EUR 20;
- 2024-08-30, two separate rows, one unit and EUR 20 each;
- 2024-09-02, one unit, EUR 20;
- 2024-12-20, one unit, EUR 20;
- 2025-01-18, two units, EUR 40;
- 2025-12-18, two separate rows, one unit and EUR 20 each.

Use `direct_sale_receipt`, the transaction-date Estonian sales VAT profile, the Lunar Base item/revenue mapping, and contact ID `42`. Generate monthly direct-sale invoices and exact per-row receipts.

- [ ] **Step 4: Bind known manual-invoice receipts to discovered IDs**

Use exact invoice number, amount, currency, counterparty, and current discovery ID. Do not create replacement invoices.

- [ ] **Step 5: Allocate distributor receipts, supplier/card debits, and bank fees**

Link distributor receipts to the corresponding prior generated sales actions. Link supplier/card rows to source-supported current/prior purchases. Represent every physical bank/card fee and supported netted correspondent fee. Keep unsupported expense-report or ambiguous merchant rows blocking with an exact evidence request.

- [ ] **Step 6: Review required master data separately**

Run master-data preflight before rebuilding actions. Confirm the generic private-customer policy maps direct sales to contact ID `42`. No bank-fee supplier contact currently exists, so prepare but do not execute the LHV contact-creation action and request explicit approval before that master-data write.

- [ ] **Step 7: Refresh read-only discovery and regenerate both dry runs**

Run 2024 first, then 2025. Do not approve batches. Confirm every action remains draft and no live request is sent.

- [ ] **Step 8: Update private readiness notes**

Remove the resolved inventory blocker and obsolete fallback/shipping notes. List only exact unresolved accounting decisions, master-data requirements, and the chronological live sequence.

### Task 9: Full Verification And Final Live-Call Review

**Files:**
- Verify all generic and ignored artifacts; modify only defects found through a new failing test.

**Interfaces:**
- Consumes: completed Tasks 1–8.
- Produces: test evidence, two successful full-year dry-run summaries, and exact translated API-call inventories.

- [ ] **Step 1: Run the complete automated suite**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests PASS with zero failures/errors.

- [ ] **Step 2: Validate every shared schema and template**

Run the repository schema-contract test suite and `git diff --check`.

Expected: PASS and no whitespace errors.

- [ ] **Step 3: Run exact 2024 dry run with fresh read-only discovery**

Expected: 12/12 months pass; physical bank coverage is 100%; clearing coverage is complete; no live writes occur.

- [ ] **Step 4: Run exact 2025 dry run after 2024**

Expected: 12/12 months pass; cross-year targets resolve from prior action/submission identity; physical and clearing coverage are complete; no live writes occur.

- [ ] **Step 5: Independently enumerate physical source rows versus action references**

Verify by `(IBAN, currency)` that every immutable `statement_id` appears exactly once across settlement allocations/actions, the unreferenced physical row count is zero, and action-derived net movement equals statement net movement. For each year and currency assert `Σ(signed action cash movements) == Σ(signed physical statement rows)`.

- [ ] **Step 6: Review all warnings and translated API calls**

Require zero unresolved accounting warnings. Produce a private month-by-month list of invoices, purchases, receipts, payments, dates, currencies, amounts, contacts, VAT mappings, target document IDs, and cross-period dependencies for human approval.

- [ ] **Step 7: Define and execute first-live-use pilots only after separate live approval**

Before bulk live continuation, use refreshed discovery to look for historical proof of partial/multiple receipts and foreign-currency cash behavior. If proof is absent, stop at the first applicable action and request approval for a one-action pilot. Immediately refresh discovery and verify the created cash ID, linked document balance, currency rate, and `(IBAN, currency)` bank-account effect. Do not continue that behavior family unless the pilot passes.

- [ ] **Step 8: Commit any final generic documentation corrections**

```bash
git add AGENTS.md plans/SKILLPLAN.md skills scripts schemas templates tests
git commit -m "docs: finalize complete bank workflow"
```

Do not include ignored company artifacts in the commit and do not perform a live SimplBooks write.
