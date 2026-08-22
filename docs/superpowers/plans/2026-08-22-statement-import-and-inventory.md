# Statement Import and Inventory Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace API-first bank cash posting with a complete statement-import manifest, finish reviewed processor and inventory mappings, and prove post-import/post-posting SimplBooks state for configured years.

**Architecture:** A new canonical annual statement-import plan is derived from normalized physical bank rows plus reviewed allocations. Builder/checker/sender treat that plan as the cash source of truth: API batches create documents and explicitly forbid configured bank cash actions, while deterministic CSV/Markdown instructions drive SimplBooks statement matching. Separate immutable ledger-export and inventory-equation evidence closes the audit after manual UI operations.

**Tech Stack:** Python 3 standard library, JSON Schema draft 2020-12, existing YAML fallback, `unittest`, SimplBooks REST API, Frankfurter/ECB rate cache.

**Spec:** `docs/superpowers/specs/2026-08-22-statement-import-and-inventory-design.md`

## Global Constraints

- Preserve all private company data under ignored `companies/<company>/`; never commit it.
- Use the complete CAMT/XML statement as the canonical physical cash source, partitioned by IBAN and currency.
- Every physical statement row must have exactly one reviewed assignment; no ignore disposition is allowed.
- In statement-import mode, no configured bank incoming or payment may reach API translation or client construction.
- API master-data creation remains separately approved; financial accounts and inventory transfers use the SimplBooks UI because the published API lacks those endpoints.
- Use the reviewed single Frankfurter/ECB rate and retain exact rate-file bindings.
- Never infer quantity, warehouse, tax, document target, or financial account from gross amount alone.
- Preserve chronological document posting, submitted-month immutability, resume safety, and batch-wide prevalidation.
- Preserve every completed historical inventory action through immutable completion evidence.
- Canonical verification uses `python3 -m unittest discover -s tests -v`.

## File Structure

- Create `scripts/statement_import_plan.py`: construct, validate, render, and verify annual statement-import plans.
- Create `schemas/statement-import-plan.schema.json`: canonical plan contract.
- Create `templates/statement-import-plan.template.json`: publishable Example Company template.
- Create `tests/test_statement_import_plan.py`: exact coverage, mapping, split, rendering, and evidence tests.
- Create `scripts/ledger_export_evidence.py`: load immutable SimplBooks account-ledger exports and match plan rows independently.
- Create `schemas/ledger-export-evidence.schema.json` and `templates/ledger-export-evidence.template.json`: post-import evidence contract.
- Create `tests/test_ledger_export_evidence.py`: hash, identity, economics, account, and duplicate-proof tests.
- Modify `scripts/posting_policy.py`, `schemas/posting-policy.schema.json`, and `templates/posting-policy.template.json`: statement-import and warehouse-routing policy.
- Modify `scripts/bookbuilder.py`, `scripts/bookchecker.py`, and `scripts/booksend.py`: document-only bank batches, plan bindings, warehouse routing, and independent safety gates.
- Modify `scripts/bookprep.py`: parse the reviewed Printful wallet printout without weakening structured-source precedence.
- Modify `scripts/bookrecon.py`: empty-month inventory warning and statement-plan alignment.
- Modify `scripts/inventory_verification.py`: transfer/remnant snapshots and article/warehouse stock equations.
- Modify `scripts/live_month_run.py` and `scripts/full_year_dry_run.py`: statement-import orchestration and chronological evidence handling.
- Modify affected schemas/templates/tests alongside each production change.
- Update ignored `companies/<company>/` policy, sources, allocations, manifests, readiness report, and runbook only after generic logic passes.

---

### Task 1: Statement-Import Posting Policy

**Files:**
- Modify: `scripts/posting_policy.py`
- Modify: `schemas/posting-policy.schema.json`
- Modify: `templates/posting-policy.template.json`
- Test: `tests/test_posting_policy.py`
- Test: `tests/test_schema_contracts.py`

**Interfaces:**
- Consumes: posting-policy JSON.
- Produces: `cash_posting_mode(policy) -> str`, `statement_import_policy(policy) -> dict[str, Any]`, and `resolve_sales_warehouse(policy, *, channel: str, order_number: int | None) -> str`.

- [ ] **Step 1: Add failing policy tests**

```python
def test_statement_import_policy_requires_bank_and_financial_accounts(self):
    policy = valid_policy() | {
        "cash_posting": {
            "mode": "statement_import",
            "bank_income_account_ids": ["3"],
            "processor_income_account_ids": {"paypal": "6", "stripe": "7"},
            "financial_accounts": {
                "stripe_clearing": "30", "paypal": "31", "bank_fees": "32",
                "reporting_person_payable": "33", "platform_prepayment": "34",
                "fx_gain": "35", "fx_loss": "36"
            }
        }
    }
    posting_policy.validate_posting_policy(policy)
    self.assertEqual(posting_policy.cash_posting_mode(policy), "statement_import")

def test_woo_warehouse_boundary_is_inclusive(self):
    policy = valid_policy_with_warehouse_rules()
    self.assertEqual(posting_policy.resolve_sales_warehouse(policy, channel="woo", order_number=999), "6")
    self.assertEqual(posting_policy.resolve_sales_warehouse(policy, channel="woo", order_number=1000), "1")
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 -m unittest tests.test_posting_policy tests.test_schema_contracts -v`

Expected: failures because the new policy sections and resolver functions do not exist.

- [ ] **Step 3: Implement strict policy parsing**

Add schema and runtime validation for this shape:

```json
{
  "cash_posting": {
    "mode": "statement_import",
    "bank_income_account_ids": ["3"],
    "processor_income_account_ids": {"paypal": "6", "stripe": "7"},
    "financial_accounts": {
      "stripe_clearing": "30",
      "paypal": "31",
      "bank_fees": "32",
      "reporting_person_payable": "33",
      "platform_prepayment": "34",
      "fx_gain": "35",
      "fx_loss": "36"
    }
  },
  "warehouse_routing": {
    "woo": {"before_order": 1000, "before_warehouse_id": "6", "from_warehouse_id": "1"},
    "direct_sale_warehouse_id": "1",
    "distributor_warehouse_id": "9"
  }
}
```

Reject missing account roles, non-numeric IDs, unknown modes, missing order numbers, and an unbound distributor warehouse.

- [ ] **Step 4: Run focused tests and schema parsing**

Run: `python3 -m unittest tests.test_posting_policy tests.test_schema_contracts -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add scripts/posting_policy.py schemas/posting-policy.schema.json templates/posting-policy.template.json tests/test_posting_policy.py tests/test_schema_contracts.py
git commit -m "feat: define statement import posting policy"
```

### Task 2: Canonical Annual Statement-Import Plan

**Files:**
- Create: `scripts/statement_import_plan.py`
- Create: `schemas/statement-import-plan.schema.json`
- Create: `templates/statement-import-plan.template.json`
- Create: `tests/test_statement_import_plan.py`
- Modify: `scripts/reference_artifacts.py`
- Test: `tests/test_reference_artifacts.py`

**Interfaces:**
- Consumes: `load_bank_allocations(path, normalized_year_paths=...)`, posting policy, normalized annual physical rows, and ECB bindings.
- Produces: `build_statement_import_plan(...) -> dict[str, Any]`, `validate_statement_import_plan(...) -> None`, `render_csv(plan) -> str`, `render_markdown(plan) -> str`, and immutable `statement_import_plan` action references.

- [ ] **Step 1: Add RED tests for exact plan construction**

```python
def test_plan_covers_every_physical_row_once_and_maps_manual_families(self):
    plan = statement_import_plan.build_statement_import_plan(
        year=2024,
        company_slug="example",
        normalized_payloads=[normalized_with_rows()],
        allocation_payload=reviewed_allocations(),
        policy=statement_import_policy(),
        rate_bindings=[],
    )
    self.assertEqual([row["statement_id"] for row in plan["rows"]], ["archive:a", "archive:b"])
    self.assertEqual(plan["rows"][0]["financial_accounts"], {"debit": "2", "credit": "3"})
    self.assertEqual(plan["coverage"]["uncovered_count"], 0)

def test_plan_rejects_duplicate_statement_identity_and_unbalanced_split(self):
    with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "duplicate"):
        statement_import_plan.validate_statement_import_plan(plan_with_duplicate_key())
    with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "split"):
        statement_import_plan.validate_statement_import_plan(plan_with_split(100, [90, -5]))
```

- [ ] **Step 2: Run the new tests and confirm RED**

Run: `python3 -m unittest tests.test_statement_import_plan tests.test_reference_artifacts -v`

Expected: import failure for `statement_import_plan`.

- [ ] **Step 3: Implement the canonical schema and builder**

Use immutable key `(statement_id, iban, currency)`. Each row must include source binding, period/date, signed amount, disposition, UI operation, exact financial-account direction, document target, optional ECB binding, optional split parts, review state, and evidence state.

Map reviewed dispositions explicitly:

```python
MANUAL_FAMILY = {
    "bank_fee_payment": "bank_fee",
    "expense_reimbursement_payment": "reporting_person_reimbursement",
    "clearing_transfer": "processor_or_internal_transfer",
    "reviewed_split": "reviewed_split",
}
DOCUMENT_FAMILY = {
    "generated_invoice_receipt", "existing_invoice_receipt", "direct_sale_receipt",
    "generated_purchase_payment", "existing_purchase_payment",
}
```

Do not infer account roles from description text; resolve them from reviewed allocation targets plus policy.

- [ ] **Step 4: Implement deterministic CSV and Markdown renderers**

CSV column order must be fixed:

```python
CSV_FIELDS = (
    "statement_id", "iban", "date", "currency", "signed_amount", "counterparty",
    "description", "disposition", "ui_action", "debit_account", "credit_account",
    "document_refs", "ecb_rate", "split_equation", "status",
)
```

Write bytes only when changed; changed outputs use same-directory temporary files plus `os.replace`.

Expose a CLI that requires `--company-dir`, `--year`, and optional explicit normalized/allocation/policy/output paths. Its JSON output reports the three artifact paths, hashes, coverage counts, and family counts so orchestration never parses prose.

- [ ] **Step 5: Bind the plan as a required reference artifact**

When `cash_posting.mode == statement_import`, `required_action_binding_kinds()` must include `statement_import_plan`. Validate exact path and SHA before checker or sender logic.

```python
if batch.get("cash_posting_mode") == "statement_import":
    required.add("statement_import_plan")
```

- [ ] **Step 6: Run focused tests**

Run: `python3 -m unittest tests.test_statement_import_plan tests.test_reference_artifacts tests.test_schema_contracts -v`

Expected: PASS, including stable output on an unchanged rerun.

- [ ] **Step 7: Commit Task 2**

```bash
git add scripts/statement_import_plan.py scripts/reference_artifacts.py schemas/statement-import-plan.schema.json templates/statement-import-plan.template.json tests/test_statement_import_plan.py tests/test_reference_artifacts.py tests/test_schema_contracts.py
git commit -m "feat: generate exact statement import plans"
```

### Task 3: Document-Only Bank Builder, Checker, and Sender Gates

**Files:**
- Modify: `scripts/bookbuilder.py`
- Modify: `scripts/bookchecker.py`
- Modify: `scripts/booksend.py`
- Modify: `schemas/action-batch.schema.json`
- Modify: `templates/actions-period.template.yaml`
- Test: `tests/test_bookbuilder.py`
- Test: `tests/test_bookchecker.py`
- Test: `tests/test_booksend.py`
- Test: `tests/test_schema_contracts.py`

**Interfaces:**
- Consumes: `cash_posting_mode(policy)`, configured bank income-account IDs, and the bound annual statement-import plan.
- Produces: document actions, reviewed processor-account settlements, and zero configured-bank API cash actions.

- [ ] **Step 1: Add RED builder and checker tests**

```python
def test_statement_import_mode_omits_bank_cash_but_keeps_documents(self):
    batch = build_batch(policy=statement_mode_policy(), records=invoice_and_bank_receipt())
    self.assertIn("create_invoice_summary", action_types(batch))
    self.assertNotIn("create_incoming_summary", bank_action_types(batch))
    self.assertEqual(batch["cash_posting_mode"], "statement_import")

def test_checker_rejects_bank_cash_action_in_statement_import_mode(self):
    findings = bookchecker.evaluate_batch(batch_with_bank_incoming(), ...)
    self.assertTrue(any("bank cash action" in item["summary"] for item in findings))
```

- [ ] **Step 2: Add RED sender zero-call tests**

```python
def test_sender_rejects_bank_cash_before_client_construction(self):
    with self.assertRaisesRegex(SimplbooksError, "statement-import mode"):
        booksend.execute_batch(batch_with_bank_payment(), client_factory=forbidden_client)
    self.assertEqual(client_calls, [])
```

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `python3 -m unittest tests.test_bookbuilder tests.test_bookchecker tests.test_booksend -v`

Expected: new cases fail because cash actions are still generated or accepted.

- [ ] **Step 4: Suppress configured-bank cash generation and add independent gates**

Builder identifies statement-import bank accounts by policy-bound income-account ID, not by action label. Checker and sender independently reject any incoming/payment whose `income_account_id` is in `bank_income_account_ids`. Reviewed processor accounts remain allowed only when document target and currency proof pass.

```python
def prohibited_bank_cash_action(action, policy):
    return (
        action.get("action_type") in {"create_incoming_summary", "create_payment_summary"}
        and str((action.get("payload") or {}).get("income_account_id"))
        in set(statement_import_policy(policy)["bank_income_account_ids"])
    )
```

- [ ] **Step 5: Require exact annual-plan coverage in each batch**

For the period, compare physical normalized rows with the annual plan. Document support actions do not count as cash coverage; the plan row is the terminal coverage item. Reject a cash action and plan row claiming the same physical key.

```python
expected = {physical_bank_allocation_key(row) for row in physical_rows}
planned = {statement_plan_key(row) for row in plan_rows if row["period"] == period}
if expected != planned:
    raise SimplbooksError(f"Statement-plan coverage mismatch: missing={sorted(expected-planned)}, extra={sorted(planned-expected)}")
```

- [ ] **Step 6: Run focused tests and schemas**

Run: `python3 -m unittest tests.test_bookbuilder tests.test_bookchecker tests.test_booksend tests.test_schema_contracts -v`

Expected: PASS and zero client calls for every prohibited-cash case.

- [ ] **Step 7: Commit Task 3**

```bash
git add scripts/bookbuilder.py scripts/bookchecker.py scripts/booksend.py schemas/action-batch.schema.json templates/actions-period.template.yaml tests/test_bookbuilder.py tests/test_bookchecker.py tests/test_booksend.py tests/test_schema_contracts.py
git commit -m "fix: forbid bank API cash in import mode"
```

### Task 4: Printful Personal-Card Wallet and Processor Clearing

**Files:**
- Modify: `scripts/bookprep.py`
- Modify: `scripts/bookrecon.py`
- Modify: `schemas/normalized-period.schema.json`
- Test: `tests/test_bookprep.py`
- Test: `tests/test_bookrecon.py`

**Interfaces:**
- Consumes: canonical `wallet-printout.txt`, reviewed card-owner policy, PayPal/Stripe clearing records.
- Produces: typed wallet funding/refund movements with `card_last4`, `funding_owner`, exact amount/currency/date, and source binding.

- [ ] **Step 1: Add RED parser tests for both card owners**

```python
def test_wallet_printout_routes_reviewed_personal_and_company_cards(self):
    result = bookprep.parse_printful_wallet_printout(source_descriptor(wallet_text()))
    personal = [row for row in result["clearing_transactions"] if row["card_last4"] == "1111"]
    company = [row for row in result["clearing_transactions"] if row["card_last4"] == "2222"]
    self.assertEqual({row["funding_owner"] for row in personal}, {"reporting_person"})
    self.assertEqual({row["funding_owner"] for row in company}, {"company"})
```

- [ ] **Step 2: Add RED clearing-equation tests**

Assert April-July personal deposits debit Printful prepayment/credit reporting-person payable, refunds reverse it, and all rows are claimed once. Reject an unknown card suffix or a refund exceeding the referenced funding group.

```python
def test_personal_wallet_refund_cannot_exceed_bound_deposits(self):
    errors = bookrecon.printful_wallet_equation_errors([deposit("10.00"), refund("11.00")], wallet_policy())
    self.assertIn("refund exceeds reviewed personal-card funding", " ".join(errors))
```

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `python3 -m unittest tests.test_bookprep tests.test_bookrecon -v`

Expected: parser missing or unrecognized structured source.

- [ ] **Step 4: Implement deterministic text parsing**

Detect the printout by stable headings and parse each movement without treating arbitrary text files as wallet evidence. Preserve existing structured CSV precedence. Add `card_last4` and `funding_owner` only from an exact policy mapping.

```python
WALLET_LINE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}).*?(?P<amount>-?\d+[.,]\d{2}).*?(?P<last4>\d{4})$")
owner = card_owners.get(match.group("last4"))
if owner is None:
    raise SimplbooksError(f"Printful wallet card {match.group('last4')} has no reviewed owner")
```

- [ ] **Step 5: Add the reporting-person/prepayment equation**

Reconciliation reports deposits, refunds, expense consumption, and remaining wallet balance separately. It must not claim these non-bank rows as physical statement coverage.

```python
closing = opening + personal_deposits - personal_refunds - wallet_consumption
liability_change = personal_deposits - personal_refunds
```

- [ ] **Step 6: Run focused tests**

Run: `python3 -m unittest tests.test_bookprep tests.test_bookrecon tests.test_schema_contracts -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add scripts/bookprep.py scripts/bookrecon.py schemas/normalized-period.schema.json tests/test_bookprep.py tests/test_bookrecon.py tests/test_schema_contracts.py
git commit -m "feat: account for reviewed Printful wallet funding"
```

### Task 5: Exact Warehouse Routing and Woo Quantity Proof

**Files:**
- Modify: `scripts/bookbuilder.py`
- Modify: `scripts/bookchecker.py`
- Modify: `scripts/booksend.py`
- Modify: `schemas/action-batch.schema.json`
- Test: `tests/test_bookbuilder.py`
- Test: `tests/test_bookchecker.py`
- Test: `tests/test_booksend.py`

**Interfaces:**
- Consumes: `resolve_sales_warehouse(...)`, exact order-number/quantity contributors, direct-sale reviewed allocation, distributor warehouse binding.
- Produces: article-3 invoice lines whose quantity and warehouse are independently provable.

- [ ] **Step 1: Add RED route and quantity tests**

```python
def test_woo_boundary_routes_each_side_to_its_reviewed_warehouse(self):
    lines = build_sales_lines(records=[woo(999, 1), woo(1000, 1)], policy=warehouse_policy())
    self.assertEqual([(line["order_number"], line["warehouse_id_hint"]) for line in lines], [(999, "6"), (1000, "1")])

def test_exact_order_quantity_is_preserved(self):
    line = only(build_sales_lines(records=[woo(900, 2)], policy=warehouse_policy()))
    self.assertEqual((line["quantity"], line["warehouse_id_hint"]), (2, "6"))
```

- [ ] **Step 2: Add RED direct-sale and distributor tests**

Assert direct sales use the reviewed warehouse and one invoice per physical receipt. Assert distributor document generation blocks until a discovered warehouse ID matches policy and transfer evidence is bound.

```python
def test_distributor_sales_require_bound_transfer(self):
    with self.assertRaisesRegex(SimplbooksError, "warehouse transfer evidence"):
        build_sales_actions(records=[distributor_sale()], policy=distributor_policy(), transfer_evidence=None)
```

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `python3 -m unittest tests.test_bookbuilder tests.test_bookchecker tests.test_booksend -v`

Expected: current static warehouse policy fails the threshold and direct-sale cases.

- [ ] **Step 4: Implement per-contributor grouping**

Do not combine order contributors across the configured boundary. Keep inventory proof scope/hash/contributor equality intact. Add exact order number and selected routing rule to the proof envelope so checker and sender can recompute it independently.

```python
warehouse_id = resolve_sales_warehouse(policy, channel="woo", order_number=record["order_number"])
group_key = (taxable_profile(record), record_currency(record), warehouse_id)
```

- [ ] **Step 5: Implement direct-sale one-receipt/one-invoice generation**

Use immutable statement identity in the invoice action key and external number. The matching plan row points to exactly that action. Duplicate same-value direct receipts remain distinct.

```python
statement_id = statement_identity(bank_record)
action_key = f"direct-sale-invoice:{period}:{canonical_value_sha256(statement_id)[:16]}"
```

- [ ] **Step 6: Run focused tests**

Run: `python3 -m unittest tests.test_bookbuilder tests.test_bookchecker tests.test_booksend tests.test_schema_contracts -v`

Expected: PASS, including missing-order-number and wrong-warehouse zero-call failures.

- [ ] **Step 7: Commit Task 5**

```bash
git add scripts/bookbuilder.py scripts/bookchecker.py scripts/booksend.py schemas/action-batch.schema.json tests/test_bookbuilder.py tests/test_bookchecker.py tests/test_booksend.py tests/test_schema_contracts.py
git commit -m "fix: route sales to evidenced warehouses"
```

### Task 6: Warehouse Transfer and Inventory Equation Evidence

**Files:**
- Modify: `scripts/inventory_verification.py`
- Modify: `schemas/manual-inventory-action.schema.json`
- Modify: `templates/manual-inventory-action.template.json`
- Test: `tests/test_inventory_verification.py`
- Modify: `scripts/bookaudit.py`
- Test: `tests/test_bookaudit.py`

**Interfaces:**
- Consumes: dated remnant API responses, manual transfer evidence, posted invoice rows, purchase/adjustment evidence.
- Produces: `evaluate_stock_equation(...) -> dict[str, Any]` and exact manual adjustment instructions when closing differs from the selected count.

- [ ] **Step 1: Add RED tests for an exact warehouse transfer**

```python
def test_transfer_preserves_total_and_moves_reviewed_quantity(self):
    result = inventory_verification.evaluate_transfer(
        before={"main": 1000, "distributor": 0},
        after={"main": 900, "distributor": 100},
        quantity=Decimal("100"),
    )
    self.assertEqual(result["errors"], [])
```

Add mutations for wrong date, wrong article, total change, and a transfer applied after the first distributor sale.

- [ ] **Step 2: Add RED article/warehouse equation tests**

```python
def test_stock_equation_is_evaluated_per_warehouse_and_in_aggregate(self):
    result = inventory_verification.evaluate_stock_equation(evidence_fixture())
    self.assertEqual(result["warehouses"]["distributor"]["closing"], Decimal("176"))
    self.assertEqual(result["aggregate"]["difference"], Decimal("0"))
```

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `python3 -m unittest tests.test_inventory_verification tests.test_bookaudit -v`

Expected: missing transfer/equation functions.

- [ ] **Step 4: Extend manual inventory evidence types**

Support typed `warehouse_transfer` and `year_end_adjustment` actions. Require effective date, article ID, source/destination warehouse IDs, positive quantity, before/after remnant bindings, approval, status, and immutable source references.

```python
MANUAL_ACTION_TYPES = frozenset({"manual_inventory_writeoff", "warehouse_transfer", "year_end_adjustment"})
```

- [ ] **Step 5: Implement equation and instruction generation**

Compute each term using `Decimal`. If the selected closing differs, emit a non-executable instruction with exact direction, quantity, warehouse, article, date, and account 115 / code 5000. Never call a write endpoint.

```python
calculated = opening + purchases + transfers_in - transfers_out - sales - writeoffs + adjustments
difference = selected_closing - calculated
instruction = None if difference == 0 else {
    "action_type": "year_end_adjustment", "quantity": abs(difference),
    "direction": "increase" if difference > 0 else "decrease", "expense_account_id": "115",
}
```

- [ ] **Step 6: Integrate independent audit findings**

`bookaudit` must fail if warehouse totals, aggregate totals, COGS-bearing invoice rows, or the selected closing do not reconcile. Preserve every completed historical inventory-action exclusion.

```python
if equation["aggregate"]["difference"] != Decimal("0"):
    findings.append(error_finding("Inventory closing differs from selected count", equation["aggregate"]))
```

- [ ] **Step 7: Run focused tests**

Run: `python3 -m unittest tests.test_inventory_verification tests.test_bookaudit tests.test_schema_contracts -v`

Expected: PASS.

- [ ] **Step 8: Commit Task 6**

```bash
git add scripts/inventory_verification.py scripts/bookaudit.py schemas/manual-inventory-action.schema.json templates/manual-inventory-action.template.json tests/test_inventory_verification.py tests/test_bookaudit.py tests/test_schema_contracts.py
git commit -m "feat: prove warehouse inventory equations"
```

### Task 7: Immutable SimplBooks Ledger-Export Evidence

**Files:**
- Create: `scripts/ledger_export_evidence.py`
- Create: `schemas/ledger-export-evidence.schema.json`
- Create: `templates/ledger-export-evidence.template.json`
- Create: `tests/test_ledger_export_evidence.py`
- Modify: `scripts/bookchecker.py`
- Modify: `scripts/bookaudit.py`
- Modify: `scripts/live_month_run.py`

**Interfaces:**
- Consumes: immutable SimplBooks CSV/XLSX-derived CSV account-ledger exports, statement plan, discovery bindings.
- Produces: `load_ledger_export(binding, *, cwd)`, `match_plan_rows(plan, ledger_rows)`, and a typed annual evidence summary.

- [ ] **Step 1: Add RED trust-boundary tests**

```python
def test_evidence_requires_hash_bound_supported_export_and_exact_company(self):
    with self.assertRaisesRegex(LedgerEvidenceError, "SHA"):
        ledger_export_evidence.load_ledger_export(bad_binding(), cwd=root)
    with self.assertRaisesRegex(LedgerEvidenceError, "company"):
        ledger_export_evidence.match_plan_rows(plan("example"), export("other"))

def test_duplicate_or_economically_wrong_ledger_match_fails(self):
    errors = ledger_export_evidence.match_plan_rows(plan_one_row(), export_duplicate_amount())
    self.assertTrue(any("duplicate" in error or "amount" in error for error in errors))
```

- [ ] **Step 2: Run the new tests and confirm RED**

Run: `python3 -m unittest tests.test_ledger_export_evidence -v`

Expected: module import failure.

- [ ] **Step 3: Implement a typed, strict CSV parser**

Require company, export period, ledger account ID/code, transaction ID, business date, currency, debit, credit, description, and document reference columns. Reject locale-ambiguous amounts, missing currency, unsupported formats, malformed dates, or duplicate transaction/account identities.

```python
REQUIRED_COLUMNS = {
    "company_id", "period", "account_id", "account_code", "transaction_id",
    "business_date", "currency", "debit", "credit", "description", "document_ref",
}
```

- [ ] **Step 4: Implement plan-to-ledger matching**

Match exact transaction identity where available, then independently verify date, currency, signed amount, financial-account direction, and document reference. Compare annual movement by account and currency and prove that clearing, receivable, payable, fee, FX, and reporting-person accounts have no unexplained difference.

```python
ledger_signed = decimal_value(row["debit"]) - decimal_value(row["credit"])
if ledger_signed != decimal_value(planned_part["signed_amount"]):
    errors.append(f"Ledger amount mismatch for {planned_part['statement_id']}")
```

- [ ] **Step 5: Integrate checker, audit, and runner gates**

Pending or unsupported evidence blocks final readiness but does not prevent document-only dry runs. Write eligibility after statement processing requires exact current evidence bindings before any further submit-capable stage.

```python
if phase == "post_import" and evidence_errors:
    raise SimplbooksError("Post-import ledger evidence is not complete: " + "; ".join(evidence_errors))
```

- [ ] **Step 6: Run focused tests**

Run: `python3 -m unittest tests.test_ledger_export_evidence tests.test_bookchecker tests.test_bookaudit tests.test_live_month_run tests.test_schema_contracts -v`

Expected: PASS and no acceptance of arbitrary PDF/JSON/free-form assertions.

- [ ] **Step 7: Commit Task 7**

```bash
git add scripts/ledger_export_evidence.py schemas/ledger-export-evidence.schema.json templates/ledger-export-evidence.template.json tests/test_ledger_export_evidence.py scripts/bookchecker.py scripts/bookaudit.py scripts/live_month_run.py tests/test_bookchecker.py tests/test_bookaudit.py tests/test_live_month_run.py tests/test_schema_contracts.py
git commit -m "feat: verify imported statement ledger evidence"
```

### Task 8: Reconciliation and Chronological Orchestration

**Files:**
- Modify: `scripts/bookrecon.py`
- Modify: `scripts/full_year_dry_run.py`
- Modify: `scripts/live_month_run.py`
- Modify: `schemas/recon-period.schema.json`
- Test: `tests/test_bookrecon.py`
- Test: `tests/test_full_year_dry_run.py`
- Test: `tests/test_live_month_run.py`

**Interfaces:**
- Consumes: annual statement plan, current normalized/allocation bindings, document submission logs, ledger evidence.
- Produces: chronological document-only runs plus explicit statement-import and post-import checkpoints.

- [ ] **Step 1: Add RED empty-month warning test**

```python
def test_inventory_relevant_company_has_no_quantity_warning_in_empty_month(self):
    check = bookrecon.build_inventory_check(normalized_empty_month(), policy_memo="inventory", entity_map=warehouses())
    self.assertEqual(check["status"], "pass")
```

Retain a warning when an actual sales, refund, purchase, transfer, write-off, or adjustment record lacks quantity proof.

- [ ] **Step 2: Add RED full-year orchestration tests**

Assert the runner generates and binds the annual statement plan before monthly builder/checker work, never invokes configured-bank cash submission, freezes submitted documents, and returns explicit `statement_import_pending` and `ledger_evidence_pending` phases.

```python
self.assertEqual(summary["phase"], "statement_import_pending")
self.assertEqual(summary["bank_api_cash_action_count"], 0)
self.assertLess(commands.index("statement_import_plan.py"), commands.index("bookbuilder.py"))
```

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `python3 -m unittest tests.test_bookrecon tests.test_full_year_dry_run tests.test_live_month_run -v`

Expected: empty-month warning and missing statement-plan orchestration failures.

- [ ] **Step 4: Make inventory warnings activity-sensitive**

Define inventory-affecting categories explicitly and require at least one such record before warning. General company warehouse configuration alone is not activity.

```python
INVENTORY_ACTIVITY = {"sales", "refunds", "purchase_expenses", "inventory_movements"}
has_activity = any(normalized.get(category) for category in INVENTORY_ACTIVITY)
```

- [ ] **Step 5: Add statement-import orchestration phases**

The annual runner sequence is: refresh discovery, validate master data, normalize, load allocations, build plan, reconcile, build/check document months chronologically, stop for statement import/matching, validate ledger evidence, run inventory audit, refresh discovery, final checks. `--force-build` continues to skip submitted months.

```python
PHASES = (
    "source_ready", "master_data_ready", "documents_ready", "statement_import_pending",
    "ledger_evidence_pending", "inventory_audit_pending", "final_checks_ready",
)
```

- [ ] **Step 6: Run focused tests**

Run: `python3 -m unittest tests.test_bookrecon tests.test_full_year_dry_run tests.test_live_month_run -v`

Expected: PASS with zero warnings in the empty-month fixture.

- [ ] **Step 7: Commit Task 8**

```bash
git add scripts/bookrecon.py scripts/full_year_dry_run.py scripts/live_month_run.py schemas/recon-period.schema.json tests/test_bookrecon.py tests/test_full_year_dry_run.py tests/test_live_month_run.py
git commit -m "feat: orchestrate statement import bookkeeping"
```

### Task 9: Company-Local Migration and Exact Import Instructions

**Files:**
- Modify ignored: `companies/<company>/source/<year-pack>/`
- Modify ignored: `companies/<company>/artifacts/posting_policy.json`
- Modify ignored: `companies/<company>/artifacts/bank/<year>-allocations.json`
- Create ignored: `companies/<company>/artifacts/statement-import/<year>-plan.json`
- Create ignored: `companies/<company>/artifacts/statement-import/<year>-plan.csv`
- Create ignored: `companies/<company>/artifacts/statement-import/<year>-plan.md`
- Modify ignored: `companies/<company>/artifacts/pre-submit-readiness.md`
- Create ignored: `companies/<company>/artifacts/statement-import-runbook.md`

**Interfaces:**
- Consumes: all generic contracts from Tasks 1-8 and the user's reviewed facts.
- Produces: exact private annual plans, regenerated drafts/checks, and UI instructions. No live writes occur in this task.

- [ ] **Step 1: Preserve and hash the new source evidence**

Copy supplied evidence into the canonical year pack using source-intake rules. Record each hash and source type. Apply exact company-local order quantities, routing boundary, direct-sale warehouse, distributor transfer, card ownership, and document targets from the ignored supplement.

- [ ] **Step 2: Update private policy without guessing live IDs**

Add statement-import roles whose current IDs are known. Keep every missing processor cash account or distributor warehouse ID unresolved until the UI prerequisite and fresh discovery prove it.

- [ ] **Step 3: Regenerate normalized data and allocations**

Run every configured month using the repository interpreter with required PDF support. Rebind allocations only when normalized content changes. Confirm each private year's expected physical-row and exceptional-family counts from the ignored supplement, with zero ignored or uncovered rows.

- [ ] **Step 4: Generate exact annual import plans**

The Markdown instructions must list every exceptional row with its debit/credit accounts and every document row with its invoice/purchase target. Verify annual signed movement separately for EUR and USD against CAMT opening/closing balances.

- [ ] **Step 5: Regenerate document-only action batches and checks**

Refresh discovery inside the freshness window, rebuild chronologically, and prove no current batch contains a configured-bank incoming/payment. Do not approve or write. Do not regenerate a previously submitted document batch.

- [ ] **Step 6: Write the live UI runbook**

Include exact company-local steps for processor/master-data setup, separately approved warehouse creation, historical transfer, statement import, row matching, split processing, ledger export, and inventory adjustment.

- [ ] **Step 7: Update private readiness honestly**

List each remaining external operation and distinguish generic readiness, document dry-run readiness, statement-import readiness, post-import evidence, and final report readiness. Never claim imported evidence or inventory proof before it exists.

- [ ] **Step 8: Run private invariant checks**

Run loaders/checkers against all annual plans, normalized/recon artifacts, current document batches, and source hashes. Confirm private files remain ignored with `git status --ignored` and no private path appears in a staged diff.

### Task 10: Full Verification, Review, and Handoff

**Files:**
- Modify: documentation or tests only if verification exposes a concrete defect.
- Record ignored: private task report and readiness evidence.

**Interfaces:**
- Consumes: completed Tasks 1-9.
- Produces: reviewed generic commits and an exact no-write readiness verdict.

- [ ] **Step 1: Run the complete generic test suite**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass with no skips hiding money-sensitive behavior.

- [ ] **Step 2: Run static and contract checks**

```bash
python3 -m compileall -q scripts tests
python3 -m json.tool schemas/statement-import-plan.schema.json
python3 -m json.tool schemas/ledger-export-evidence.schema.json
python3 -m json.tool schemas/posting-policy.schema.json
python3 -m json.tool schemas/action-batch.schema.json
python3 scripts/statement_import_plan.py --help
python3 scripts/inventory_verification.py --help
git diff --check
```

Expected: every command exits zero.

- [ ] **Step 3: Run focused money-sensitive verification again**

Run: `python3 -m unittest tests.test_statement_import_plan tests.test_ledger_export_evidence tests.test_bank_allocations tests.test_bookprep tests.test_bookrecon tests.test_bookbuilder tests.test_bookchecker tests.test_booksend tests.test_inventory_verification tests.test_bookaudit tests.test_full_year_dry_run tests.test_live_month_run tests.test_schema_contracts -v`

Expected: PASS.

- [ ] **Step 4: Review privacy and write safety**

Inspect `git diff --cached --name-only`, `git status --ignored`, sender prevalidation order, and the private artifact paths. Confirm no company data is committed and prohibited bank cash fails before client construction.

- [ ] **Step 5: Request two-stage review**

First request a generic code review for correctness, evidence trust boundaries, schema/runtime parity, idempotency, and zero-call guarantees. Then request a private accounting review for exact annual row/family counts, account assignments, warehouse routing, inventory equations, and honest blockers.

- [ ] **Step 6: Fix every Critical or Important finding with RED/GREEN evidence**

For each accepted finding, add the smallest reproducing test, run it to RED, implement the fix, rerun focused and full suites, and record the fix commit. Do not waive a finding solely because existing tests pass.

- [ ] **Step 7: Produce the execution handoff**

Report generic commit SHAs, exact test counts, private coverage counts, current blockers, and the next live operation. Explicitly state that statement imports, UI master data, warehouse transfers, approvals, and SimplBooks writes have not occurred unless independently evidenced.
