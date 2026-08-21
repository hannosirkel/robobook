# 2024 Bookkeeping Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Plepic 2024 safe for human approval by adding audited ECB rates, exact mappings, canonical source references, supplier credits, and live duplicate suppression.

**Architecture:** Add focused reference-data and posting-policy modules, then pass their validated outputs through the existing prep → recon → builder → checker → sender pipeline. Fail closed at every boundary: rates, mappings, credits, source paths, and live identities must be explicit before a batch can pass.

**Tech Stack:** Python standard library, `Decimal`, JSON/JSON Schema contracts, `unittest`, Frankfurter v2, existing Simplbooks API wrapper.

**Spec:** `docs/superpowers/specs/2026-08-21-2024-bookkeeping-readiness-design.md`

## Global Constraints

- Do not perform live bookkeeping or master-data writes.
- Fetch Frankfurter data with one annual query per required base/quote set and `providers=ECB`.
- Use one foreign-to-EUR rate per summary document, resolved on the document date or latest prior ECB date.
- Never substitute a numeric fallback for a missing exchange rate.
- Keep Plepic identifiers in ignored company-local artifacts, not reusable code.
- Use exact source-account identifiers and explicit posting-policy mappings for payloads.
- Post Printful refunds as supplier credits in May and July.
- Copy the 2024 source pack; do not delete or move `temp/2024`.
- Keep all generated monthly action batches in `draft` status.

---

### Task 1: Annual ECB Rate Cache

**Files:**
- Create: `scripts/exchange_rates.py`
- Create: `tests/test_exchange_rates.py`
- Create: `schemas/exchange-rate-cache.schema.json`
- Create: `templates/exchange-rate-cache.template.json`
- Modify: `README.md`

**Interfaces:**
- Produces: `build_frankfurter_url(year: int, base: str, quote: str) -> str`
- Produces: `validate_cache(payload: dict[str, Any], *, year: int, base: str, quote: str) -> None`
- Produces: `lookup_rate(payload: dict[str, Any], *, requested_date: date, base: str, quote: str) -> RateResolution`
- Produces: CLI `exchange_rates.py fetch --company-dir PATH --year YEAR --base USD --quote EUR [--refresh]`

- [ ] **Step 1: Write failing cache and lookup tests**

```python
class ExchangeRateTests(unittest.TestCase):
    def test_url_requests_one_year_from_ecb(self):
        url = exchange_rates.build_frankfurter_url(2024, "USD", "EUR")
        self.assertIn("from=2024-01-01", url)
        self.assertIn("to=2024-12-31", url)
        self.assertIn("providers=ECB", url)

    def test_lookup_uses_latest_prior_ecb_date(self):
        payload = cache_with_rates({"2024-03-28": "0.9241", "2024-04-02": "0.9280"})
        result = exchange_rates.lookup_rate(
            payload, requested_date=date(2024, 3, 31), base="USD", quote="EUR"
        )
        self.assertEqual(result.effective_date.isoformat(), "2024-03-28")
        self.assertEqual(result.rate, Decimal("0.9241"))

    def test_validation_rejects_inverted_pair(self):
        with self.assertRaises(exchange_rates.ExchangeRateError):
            exchange_rates.validate_cache(
                cache_with_pair("EUR", "USD"), year=2024, base="USD", quote="EUR"
            )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_exchange_rates -v`

Expected: import failure because `scripts/exchange_rates.py` does not exist.

- [ ] **Step 3: Implement Decimal-safe fetching, validation, atomic cache writes, lookup, and CLI**

Use `urllib.request`, `json.loads(..., parse_float=Decimal)`, immutable historical cache reuse, and a temporary sibling file followed by `Path.replace()` so a failed refresh cannot corrupt the prior cache.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_exchange_rates -v`

Expected: all exchange-rate tests pass.

- [ ] **Step 5: Commit the ECB cache deliverable**

```bash
git add scripts/exchange_rates.py tests/test_exchange_rates.py schemas/exchange-rate-cache.schema.json templates/exchange-rate-cache.template.json README.md
git commit -m "feat: add annual ECB exchange rate cache"
```

### Task 2: Explicit Posting Policy And Exact Entity Resolution

**Files:**
- Create: `scripts/posting_policy.py`
- Create: `tests/test_posting_policy.py`
- Create: `schemas/posting-policy.schema.json`
- Create: `templates/posting-policy.template.json`
- Modify: `scripts/bookbuilder.py`
- Modify: `tests/test_bookbuilder.py`

**Interfaces:**
- Produces: `load_posting_policy(path: Path) -> dict[str, Any]`
- Produces: `resolve_bank_account(policy: dict, *, customer_account: str) -> str`
- Produces: `resolve_contact(policy: dict, *, role: str, label: str) -> str`
- Consumes: normalized bank record `attributes.customer_account`

- [ ] **Step 1: Write failing exact-mapping tests**

```python
def test_bank_account_resolution_requires_exact_source_account(self):
    policy = {"bank_accounts": {"EE-LHV": "3"}}
    self.assertEqual(posting_policy.resolve_bank_account(policy, customer_account="EE-LHV"), "3")
    with self.assertRaises(posting_policy.PostingPolicyError):
        posting_policy.resolve_bank_account(policy, customer_account="UNKNOWN")

def test_woo_uses_eraisik_and_paypal_never_falls_back_to_stripe(self):
    policy = {"contacts": {"sales": {"woo": "42"}, "processors": {"stripe": "29"}}}
    self.assertEqual(posting_policy.resolve_contact(policy, role="sales", label="woo"), "42")
    with self.assertRaises(posting_policy.PostingPolicyError):
        posting_policy.resolve_contact(policy, role="processors", label="paypal")
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_posting_policy tests.test_bookbuilder -v`

Expected: posting-policy import failure and builder exact-mapping assertions fail.

- [ ] **Step 3: Implement schema validation and exact resolution**

Remove `preferred_bank_account_id` and contact fallback use from submit-capable payload construction when a posting policy is loaded. Preserve fuzzy candidates only in diagnostic notes.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_posting_policy tests.test_bookbuilder -v`

- [ ] **Step 5: Commit the posting-policy deliverable**

```bash
git add scripts/posting_policy.py tests/test_posting_policy.py schemas/posting-policy.schema.json templates/posting-policy.template.json scripts/bookbuilder.py tests/test_bookbuilder.py
git commit -m "feat: require explicit bookkeeping mappings"
```

### Task 3: Canonical Source Reference Enforcement

**Files:**
- Modify: `scripts/bookchecker.py`
- Modify: `tests/test_bookchecker.py`
- Modify: `skills/bookprep/SKILL.md`
- Modify: `skills/bookchecker/SKILL.md`

**Interfaces:**
- Produces: `evaluate_source_locations(action_batch: dict, *, company_dir: Path | None) -> list[dict]`
- Consumes: action `source_refs[].path`

- [ ] **Step 1: Write a failing checker test**

```python
def test_checker_fails_company_batch_with_temp_source_reference(self):
    action = invoice_action()
    action["source_refs"][0]["path"] = "temp/2024/woo.csv"
    result = bookchecker.evaluate_source_locations(
        {"actions": [action]}, company_dir=Path("companies/example")
    )
    self.assertEqual(result[0]["severity"], "error")
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/python -m unittest tests.test_bookchecker.BookcheckerTests.test_checker_fails_company_batch_with_temp_source_reference -v`

- [ ] **Step 3: Implement canonical-path evaluation and wire it into checker results**

Company batches accept company-local source and artifact references but reject `temp/` evidence. Standalone fixture tests without a company directory keep existing behavior.

- [ ] **Step 4: Run checker tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_bookchecker -v`

- [ ] **Step 5: Commit the canonical-source gate**

```bash
git add scripts/bookchecker.py tests/test_bookchecker.py skills/bookprep/SKILL.md skills/bookchecker/SKILL.md
git commit -m "feat: enforce canonical company source references"
```

### Task 4: Printful Supplier-Credit Normalization

**Files:**
- Modify: `scripts/bookprep.py`
- Modify: `tests/test_bookprep.py`
- Modify: `scripts/bookrecon.py`
- Modify: `tests/test_bookrecon.py`
- Modify: `schemas/normalized-period.schema.json`
- Modify: `templates/normalized-period.template.json`

**Interfaces:**
- Adds normalized record category: `purchase_credits`
- Produces event type: `printful_supplier_credit`
- Preserves `external_ref`, refund date, currency, negative source amount, positive credit magnitude, and source row

- [ ] **Step 1: Write failing normalization tests for cross-period and refund-only rows**

```python
def test_printful_refund_only_rows_become_supplier_credits(self):
    records, exceptions = parse_printful_fixture(
        completed_in_prior_month=True, refund_date="2024-05-15", amount="-11.40"
    )
    self.assertFalse([e for e in exceptions if e["blocking"]])
    self.assertEqual(records["purchase_credits"][0]["gross_amount"], 11.40)
    self.assertEqual(records["purchase_credits"][0]["external_ref"], "105211877")
```

- [ ] **Step 2: Run prep/recon tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_bookprep tests.test_bookrecon -v`

Expected: missing category or old refund-overage exception behavior.

- [ ] **Step 3: Implement supplier-credit records and recon visibility**

Do not net credits into positive expenses. Recon reports credit totals by supplier and currency and carries their evidence without treating them as unsupported exceptions.

- [ ] **Step 4: Run prep/recon tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_bookprep tests.test_bookrecon -v`

- [ ] **Step 5: Commit the normalization deliverable**

```bash
git add scripts/bookprep.py tests/test_bookprep.py scripts/bookrecon.py tests/test_bookrecon.py schemas/normalized-period.schema.json templates/normalized-period.template.json
git commit -m "feat: normalize Printful supplier credits"
```

### Task 5: Supplier Parsing And Live Document Identity

**Files:**
- Modify: `scripts/bookprep.py`
- Modify: `tests/test_bookprep.py`
- Create: `scripts/document_identity.py`
- Create: `tests/test_document_identity.py`
- Modify: `scripts/examine_simplbooks_year.py`
- Modify: `schemas/year-overview.schema.json`

**Interfaces:**
- Produces: `document_identity(record: dict, *, document_type: str) -> DocumentIdentity`
- Produces: `match_existing(candidate: DocumentIdentity, existing: list[DocumentIdentity]) -> MatchResult`
- Adds discovery field: `document_index`

- [ ] **Step 1: Write failing supplier and identity tests**

```python
def test_simplbooks_invoice_uses_supplier_not_recipient(self):
    record = parse_purchase_pdf_text(SIMPLBOOKS_FIXTURE)
    self.assertEqual(record["attributes"]["vendor_name"], "Simplbooks OÜ")
    self.assertEqual(record["external_ref"], "EE24111268")

def test_external_number_and_supplier_match_suppresses_existing_purchase(self):
    candidate = identity("purchase", "Simplbooks OÜ", "EE24111268", "2024-11-18", "EUR", "206.18")
    self.assertEqual(document_identity.match_existing(candidate, [candidate]).status, "exact")
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_bookprep tests.test_document_identity -v`

- [ ] **Step 3: Implement layout-specific supplier parsing and normalized discovery identities**

External number plus compatible type/supplier is exact. Without a number, require type/name/date/currency/amount. A same-number incompatible match is ambiguous, never exact.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_bookprep tests.test_document_identity tests.test_simplbooks_api -v`

- [ ] **Step 5: Commit the identity deliverable**

```bash
git add scripts/bookprep.py tests/test_bookprep.py scripts/document_identity.py tests/test_document_identity.py scripts/examine_simplbooks_year.py schemas/year-overview.schema.json
git commit -m "feat: detect existing Simplbooks documents"
```

### Task 6: Builder Rates, Credits, Policy, And Duplicate Suppression

**Files:**
- Modify: `scripts/bookbuilder.py`
- Modify: `tests/test_bookbuilder.py`
- Modify: `schemas/action-batch.schema.json`
- Modify: `templates/actions-period.template.yaml`

**Interfaces:**
- Builder CLI adds `--posting-policy`, `--exchange-rates`, and `--discovery-overview`
- Foreign payloads include validated rate provenance fields
- Credit payload schema: `purchase_credit_summary_v1`
- Batch adds `already_present: list[dict]`

- [ ] **Step 1: Write failing builder integration tests**

```python
def test_builder_applies_single_month_end_ecb_rate(self):
    batch = build_usd_batch(rate_cache=cache_with_rate("2024-03-28", "0.9241"))
    action = find_action(batch, "create_purchase_summary")
    self.assertEqual(action["payload"]["currency_rate"], 0.9241)
    self.assertEqual(action["payload"]["currency_rate_effective_date"], "2024-03-28")

def test_builder_creates_credit_and_suppresses_exact_existing_purchase(self):
    batch = build_with_credit_and_existing_simplbooks_purchase()
    self.assertEqual(find_action(batch, "create_purchase_credit_summary")["payload"]["totals"]["gross_amount"], 113.12)
    self.assertTrue(any(x["external_ref"] == "EE24111268" for x in batch["already_present"]))
```

- [ ] **Step 2: Run builder tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_bookbuilder -v`

- [ ] **Step 3: Implement the builder integrations**

Load defaults from company artifacts when `--company-dir` is present. Missing PayPal contact emits a blocking unresolved dependency and master-data draft reference, never contact ID `29`.

- [ ] **Step 4: Run builder tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_bookbuilder -v`

- [ ] **Step 5: Commit builder integration**

```bash
git add scripts/bookbuilder.py tests/test_bookbuilder.py schemas/action-batch.schema.json templates/actions-period.template.yaml
git commit -m "feat: build audited 2024 bookkeeping actions"
```

### Task 7: Checker And Sender Fail-Closed Gates

**Files:**
- Modify: `scripts/bookchecker.py`
- Modify: `tests/test_bookchecker.py`
- Modify: `scripts/booksend.py`
- Modify: `tests/test_booksend.py`
- Modify: `skills/booksend/SKILL.md`

**Interfaces:**
- Produces: `validate_exchange_rate_payload(action: dict) -> list[Finding]`
- Produces: `translate_purchase_credit_payload(action: dict) -> dict`
- Sender copies action `currency_rate`; it never creates one

- [ ] **Step 1: Write failing checker/sender tests**

```python
def test_checker_fails_unproven_foreign_rate(self):
    action = usd_purchase_action(currency_rate=1, provider=None)
    findings = bookchecker.evaluate_exchange_rates({"actions": [action]})
    self.assertTrue(any(x["severity"] == "error" for x in findings))

def test_sender_copies_reviewed_rate_and_translates_supplier_credit(self):
    action = purchase_credit_action(currency_rate="0.9241")
    translated = booksend.translate_action_for_api(action, lookup={})
    self.assertEqual(translated["Purchase"]["currency_rate"], 0.9241)
    self.assertEqual(translated["PurchaseRows"][0]["PurchaseRow"]["sum"], -113.12)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_bookchecker tests.test_booksend -v`

- [ ] **Step 3: Implement checker errors and sender translation/preconditions**

Translate non-inventory supplier credits as purchase invoices with negative line sums, matching Simplbooks' documented manual credit-note behavior. Reject zero/positive credit lines and any inventory-linked credit that would require an original stock batch.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_bookchecker tests.test_booksend -v`

- [ ] **Step 5: Commit the fail-closed gates**

```bash
git add scripts/bookchecker.py tests/test_bookchecker.py scripts/booksend.py tests/test_booksend.py skills/booksend/SKILL.md
git commit -m "feat: enforce reviewed rates mappings and credits"
```

### Task 8: Full-Year Orchestration, Company Artifacts, And Verification

**Files:**
- Modify: `scripts/full_year_dry_run.py`
- Modify: `tests/test_full_year_dry_run.py`
- Modify: `README.md`
- Create ignored: `companies/plepic/source/2024-pack/**`
- Create ignored: `companies/plepic/artifacts/reference/ecb-rates-2024.json`
- Create ignored: `companies/plepic/artifacts/posting_policy.json`
- Create ignored: `companies/plepic/artifacts/actions/master-data-paypal.yaml`
- Regenerate ignored: `companies/plepic/artifacts/{normalized,recon,actions,submissions}/2024-*`

**Interfaces:**
- Full-year runner passes company-default policy, cache, and discovery artifacts through existing CLIs
- Summary reports rates, credits, suppressed documents, unresolved master data, and canonical-reference counts

- [ ] **Step 1: Write a failing orchestration regression test**

```python
def test_full_year_runner_propagates_reference_artifacts(self):
    cmd = full_year_dry_run.build_step_command(
        python_executable="python3",
        company_dir=Path("companies/example"),
        period="2024-01",
        step_name="bookbuilder",
        script_name="bookbuilder.py",
        source_dir=Path("companies/example/source"),
        force_build=False,
    )
    self.assertIn("--posting-policy", cmd)
    self.assertIn("--exchange-rates", cmd)
    self.assertIn("--discovery-overview", cmd)
```

- [ ] **Step 2: Run orchestration tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_full_year_dry_run -v`

- [ ] **Step 3: Implement orchestration and create ignored company artifacts**

Copy `temp/2024` to canonical storage without deleting scratch files. Fetch the annual ECB cache. Build the Plepic posting policy from approved decisions and discovered IDs. Generate a PayPal master-data draft because the contact is absent.

- [ ] **Step 4: Refresh discovery read-only**

Run:

```bash
.venv/bin/python scripts/examine_simplbooks_year.py \
  --company-dir companies/plepic \
  --year 2024 \
  --output companies/plepic/artifacts/discovery/2024-overview.json
```

Expected: exit 0, no write endpoints, updated existing-document index.

- [ ] **Step 5: Run full-year dry-run and artifact assertions**

Run:

```bash
.venv/bin/python scripts/full_year_dry_run.py \
  --company-dir companies/plepic \
  --year 2024 \
  --source-dir companies/plepic/source/2024-pack \
  --python .venv/bin/python
```

Assert with a read-only verification script:

- 12 months completed
- May credits total EUR 11.40
- July credits total EUR 113.12
- every USD action has ECB provenance and a non-placeholder rate
- every cash action resolves source LHV account to income account ID `3`
- every Woo sales/refund action uses contact ID `42`
- no PayPal action uses Stripe contact ID `29`
- Simplbooks invoice `EE24111268` appears under `already_present`, not create actions
- every source manifest/ref points under `companies/plepic/source/2024-pack`

- [ ] **Step 6: Run the complete verification suite**

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q scripts tests
git diff --check
git status --short
```

Expected: all tests pass, compilation exits 0, no whitespace errors, and only intended reusable files are tracked changes.

- [ ] **Step 7: Commit the final reusable changes**

```bash
git add README.md scripts tests schemas templates skills
git commit -m "feat: make 2024 bookkeeping approval ready"
```
