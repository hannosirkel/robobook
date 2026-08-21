# Woo Tax Evidence and Fixed-Gross VAT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse annual Woo tax-summary exports, allocate only evidenced taxable orders, recalculate VAT from fixed customer gross under reviewed effective-dated policy, and track manual inventory write-offs without unsafe API translation.

**Architecture:** `bookprep` parses Woo tax rows as non-financial supporting evidence. A focused `woo_tax.py` module creates and validates a year-level allocation artifact before monthly normalization; monthly sales receive component-level fixed-gross VAT attributes that `bookbuilder` and `bookchecker` enforce. Inventory write-offs use a separate manual-decision contract and dated Simplbooks remnant verification because the public API has no write-off endpoint.

**Tech Stack:** Python 3 standard library (`csv`, `dataclasses`, `decimal`, `json`, `pathlib`), JSON Schema draft 2020-12, existing `unittest` suite, existing Simplbooks API wrapper.

**Spec:** `plans/2026-08-21-WOO-TAX-EVIDENCE-DESIGN.md`

## Global Constraints

- Customer-paid gross is immutable; VAT differences are absorbed by the merchant.
- Only orders supported by Woo tax evidence become taxable; absent orders remain zero-rated unless separately reviewed.
- Tax calculations use `Decimal` and `ROUND_HALF_UP`, rounding VAT per order component to cents.
- Tax-summary evidence never creates revenue or duplicates processor/Woo sales.
- Company-specific orders, policy decisions, totals, and inventory details remain inside ignored `companies/<company>/` paths.
- No Simplbooks write occurs during implementation or verification.
- The manual inventory write-off must never be translated into an invoice, purchase, or another unrelated API call.
- Preserve the existing uncommitted changes in `scripts/bookprep.py`, `scripts/bookbuilder.py`, and their tests.

---

### Task 1: Recognize and parse Woo annual tax-summary CSVs

**Files:**
- Modify: `scripts/bookprep.py`
- Modify: `tests/test_bookprep.py`
- Modify: `skills/bookprep/SKILL.md`
- Modify: `skills/bookprep/references/bookprep.md`

**Interfaces:**
- Consumes: `read_csv_rows(path: Path)` and `SourceDescriptor` from `scripts/bookprep.py`.
- Produces: `parse_woo_tax_summary_csv(source, *, period_start, period_end, base_currency) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]`.
- Produces supporting records in `records["other"]` with `event_type == "woo_tax_summary"` and attributes `tax_code`, `configured_rate`, `order_tax`, `shipping_tax`, `total_tax`, `orders`, and `annual_evidence`.

- [ ] **Step 1: Add failing parser-detection and successful-parse tests**

```python
def test_parse_woo_tax_summary_csv_as_annual_supporting_evidence(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "2025-pack"
        root.mkdir()
        path = root / "woocommerce-taxes.csv"
        path.write_text(
            '\ufeff"Tax code",Rate,"Total tax","Order tax","Shipping tax",Orders\n'
            'DE-DE-VAT-1,19,19.00,15.00,4.00,1\n',
            encoding="utf-8",
        )
        start, end = bookprep.parse_period("2025-12")
        source = bookprep.inspect_source_file(path=path, root_dir=root, period_start=start, period_end=end)
        self.assertEqual(source.source_system, "woo")
        self.assertEqual(source.parser_name, "parse_woo_tax_summary_csv")
        self.assertEqual(source.covered_from.isoformat(), "2025-01-01")
        self.assertEqual(source.covered_until.isoformat(), "2025-12-31")
        records, exceptions = bookprep.parse_woo_tax_summary_csv(
            source, period_start=start, period_end=end, base_currency="EUR"
        )
        self.assertFalse(exceptions)
        self.assertEqual(records["sales"], [])
        self.assertEqual(records["other"][0]["country_code"], "DE")
        self.assertEqual(records["other"][0]["attributes"]["orders"], 1)
```

- [ ] **Step 2: Add failing validation and non-December duplication tests**

```python
def parse_tax_fixture(self, row: str, *, period: str = "2025-12"):
    temp_dir = tempfile.TemporaryDirectory()
    self.addCleanup(temp_dir.cleanup)
    root = Path(temp_dir.name) / "2025-pack"
    root.mkdir()
    path = root / "woocommerce-taxes.csv"
    path.write_text(
        'Tax code,Rate,Total tax,Order tax,Shipping tax,Orders\n' + row + '\n',
        encoding="utf-8",
    )
    period_start, period_end = bookprep.parse_period(period)
    source = bookprep.inspect_source_file(
        path=path, root_dir=root, period_start=period_start, period_end=period_end
    )
    assert source is not None
    return bookprep.parse_woo_tax_summary_csv(
        source, period_start=period_start, period_end=period_end, base_currency="EUR"
    )

def test_woo_tax_summary_blocks_component_mismatch(self) -> None:
    # Build a source row with total 19.01 but components 15.00 + 4.00.
    records, exceptions = self.parse_tax_fixture("DE-DE-VAT-1,19,19.01,15.00,4.00,1")
    self.assertEqual(records["other"], [])
    self.assertTrue(any(item["blocking"] for item in exceptions))

def test_woo_tax_summary_emits_only_in_year_end_period(self) -> None:
    records, exceptions = self.parse_tax_fixture(
        "DE-DE-VAT-1,19,19.00,15.00,4.00,1", period="2025-05"
    )
    self.assertFalse(exceptions)
    self.assertEqual(records["other"], [])
```

- [ ] **Step 3: Run the focused tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_bookprep.BookprepTests.test_parse_woo_tax_summary_csv_as_annual_supporting_evidence tests.test_bookprep.BookprepTests.test_woo_tax_summary_blocks_component_mismatch tests.test_bookprep.BookprepTests.test_woo_tax_summary_emits_only_in_year_end_period -v`

Expected: FAIL because the header set, annual parent coverage, and parser do not exist.

- [ ] **Step 4: Implement the exact header detector and annual coverage inference**

```python
ROW_EVENT_HEADERS["woo_tax_summary_csv"] = {
    "tax code", "rate", "total tax", "order tax", "shipping tax", "orders"
}

def infer_parent_year_coverage(path: Path) -> tuple[date, date] | None:
    for parent in path.parents:
        match = re.fullmatch(r"(20\d{2})(?:-pack)?", normalize_ascii(parent.name).lower())
        if match:
            year = int(match.group(1))
            return date(year, 1, 1), date(year, 12, 31)
    return None
```

Update `infer_source_system`, `detect_parser`, and `inspect_source_file` so the exact tax headers select Woo and `parse_woo_tax_summary_csv`, and parent-year coverage is used before the target-period fallback.

- [ ] **Step 5: Implement strict parsing without financial duplication**

```python
def parse_woo_tax_summary_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    if period_end != source.covered_until:
        return parser_result(), []
    for line_no, row in enumerate(rows, start=2):
        total = parse_decimal(row["Total tax"])
        order_tax = parse_decimal(row["Order tax"])
        shipping_tax = parse_decimal(row["Shipping tax"])
        orders = parse_decimal(row["Orders"])
        country_match = re.fullmatch(r"([A-Z]{2})-[A-Z]{2}-VAT-[A-Za-z0-9-]+", row["Tax code"].strip())
        if not country_match or orders <= 0 or orders != orders.to_integral_value() or total != order_tax + shipping_tax:
            exceptions.append(make_exception(
                source=source,
                exception_id=f"{source.source_id}:invalid-tax-row:{line_no}",
                severity="error",
                reason="Woo tax row has an invalid code, count, rate, or component total.",
                blocking=True,
                row_ref=f"csv:{line_no}",
                suggested_follow_up="Export a corrected Woo tax summary before rebuilding this year.",
            ))
            continue
        _, record = make_record(
            source=source,
            category="other",
            record_id=f"{source.source_id}:woo-tax:{line_no}",
            event_type="woo_tax_summary",
            event_date=source.covered_until,
            description=f"Woo annual tax summary {row['Tax code']}",
            currency=base_currency,
            gross_amount=Decimal("0"),
            net_amount=Decimal("0"),
            vat_amount=total,
            external_ref=row["Tax code"],
            channel="woo",
            country_code=country_match.group(1),
            attributes={"tax_code": row["Tax code"], "configured_rate": float(rate),
                        "order_tax": float(order_tax), "shipping_tax": float(shipping_tax),
                        "total_tax": float(total), "orders": int(orders), "annual_evidence": True},
            row_ref=f"csv:{line_no}",
        )
        result["other"].append(record)
```

- [ ] **Step 6: Update Bookprep documentation and run its full tests**

Run: `.venv/bin/python -m unittest tests.test_bookprep -v`

Expected: PASS, and the skill documentation lists Woo tax summaries as supporting evidence rather than sales.

- [ ] **Step 7: Commit the parser task**

```bash
git add scripts/bookprep.py tests/test_bookprep.py skills/bookprep/SKILL.md skills/bookprep/references/bookprep.md
git commit -m "feat: parse Woo tax summary evidence"
```

---

### Task 2: Add the annual tax-allocation contract and pure calculation engine

**Files:**
- Create: `scripts/woo_tax.py`
- Create: `schemas/woo-tax-allocation.schema.json`
- Create: `templates/woo-tax-allocation.template.json`
- Create: `tests/test_woo_tax.py`
- Modify: `tests/test_schema_contracts.py`

**Interfaces:**
- Produces: `VatPeriod(start: date, end: date | None, rate: Decimal, goods_vat_type_id: str, shipping_vat_type_id: str)`.
- Produces: `corrected_component(fixed_gross: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]`, returning `(net, vat)`.
- Produces: `validate_allocation(payload: dict[str, Any]) -> list[str]`.
- Produces: `build_month_totals(allocations: list[dict[str, Any]]) -> dict[str, dict[str, Decimal]]`.
- Produces CLI commands `woo_tax.py build --review PATH --output PATH` and `woo_tax.py validate --company-dir PATH --year YEAR`.

- [ ] **Step 1: Write failing fixed-gross and rate-transition tests**

```python
def allocation_fixture() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "year": 2025,
        "source_files": [{"source_id": "woo-tax", "sha256": "a" * 64}],
        "policy": {"oss_registered": False, "dispatch_origin": "EE", "merchant_absorbs_vat": True},
        "vat_periods": [
            {"start": "2025-01-01", "end": "2025-06-30", "rate": 22,
             "goods_vat_type_id": "25", "shipping_vat_type_id": "24"},
            {"start": "2025-07-01", "end": None, "rate": 24,
             "goods_vat_type_id": "34", "shipping_vat_type_id": "33"},
        ],
        "source_rows": [{"source_row_id": "woo-tax:2", "tax_code": "DE-DE-VAT-1",
                         "configured_rate": 20, "order_tax": 10.00, "shipping_tax": 10.00,
                         "total_tax": 20.00, "orders": 1}],
        "allocations": [{"source_row_id": "woo-tax:2", "order_id": "EXAMPLE-EU-1",
                         "period": "2025-05", "event_date": "2025-05-18", "country_code": "DE",
                         "processor_ref": "pi_example", "configured_rate": 20, "corrected_rate": 22,
                         "original_order_tax": 10.00, "original_shipping_tax": 10.00,
                         "fixed_product_gross": 60.00, "fixed_shipping_gross": 60.00,
                         "corrected_product_vat": 10.82, "corrected_shipping_vat": 10.82,
                         "source_refs": [{"source_id": "woo-tax", "path": "source/woocommerce-taxes.csv",
                                          "row_ref": "csv:2", "page_ref": None, "notes": None}]}],
        "monthly_totals": {"2025-05": {"gross": 120.00, "original_vat": 20.00, "corrected_vat": 21.64}},
        "validation": {"status": "pass", "errors": []},
    }

class WooTaxTests(unittest.TestCase):
    def test_corrected_component_preserves_fixed_gross(self) -> None:
        net, vat = woo_tax.corrected_component(Decimal("124.00"), Decimal("24"))
        self.assertEqual(vat, Decimal("24.00"))
        self.assertEqual(net, Decimal("100.00"))
        self.assertEqual(net + vat, Decimal("124.00"))

    def test_select_vat_period_uses_effective_date(self) -> None:
        periods = [
            woo_tax.VatPeriod(date(2024, 1, 1), date(2025, 6, 30), Decimal("22"), "25", "24"),
            woo_tax.VatPeriod(date(2025, 7, 1), None, Decimal("24"), "34", "33"),
        ]
        self.assertEqual(woo_tax.select_vat_period(date(2025, 6, 30), periods).rate, Decimal("22"))
        self.assertEqual(woo_tax.select_vat_period(date(2025, 7, 1), periods).rate, Decimal("24"))
```

- [ ] **Step 2: Write failing allocation-completeness tests**

```python
def test_validate_allocation_rejects_duplicate_and_unallocated_counts(self) -> None:
    payload = allocation_fixture()
    payload["allocations"].append(dict(payload["allocations"][0]))
    errors = woo_tax.validate_allocation(payload)
    self.assertIn("taxable order is allocated more than once", " ".join(errors))
    self.assertIn("allocated order count", " ".join(errors))

def test_validate_allocation_keeps_non_taxable_orders_outside_allocation(self) -> None:
    payload = allocation_fixture()
    # An unrelated US order is deliberately absent: allocation completeness is tied to
    # tax-summary Orders counts, not to every Woo/processor order in the year.
    self.assertEqual(woo_tax.validate_allocation(payload), [])
```

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_woo_tax -v`

Expected: FAIL because `woo_tax.py` is absent.

- [ ] **Step 4: Implement decimal calculation and effective-date selection**

```python
CENT = Decimal("0.01")

def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)

def corrected_component(fixed_gross: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    vat = money(fixed_gross * rate / (Decimal("100") + rate))
    return fixed_gross - vat, vat

def select_vat_period(event_date: date, periods: Sequence[VatPeriod]) -> VatPeriod:
    matches = [item for item in periods if item.start <= event_date and (item.end is None or event_date <= item.end)]
    if len(matches) != 1:
        raise WooTaxError(f"Expected exactly one VAT profile for {event_date}, found {len(matches)}.")
    return matches[0]
```

- [ ] **Step 5: Implement allocation validation**

Validate unique order IDs, source-row order counts, original order/shipping/total-tax sums, component gross reconciliation, effective profile coverage, corrected component sums, and unchanged order gross. Return stable sorted error strings; never silently repair input.

Add `build_allocation(review: dict[str, Any]) -> dict[str, Any]`. It accepts explicit reviewed
order-to-source-row mappings, derives corrected component VAT using `select_vat_period`, calculates
monthly totals, runs `validate_allocation`, and sets `validation.status` to `pass` only when the
error list is empty. The `build` CLI reads the reviewed JSON and writes this returned artifact. The
`validate` CLI loads the company artifact, reruns all calculations from its fixed gross and policy,
prints annual original/corrected VAT and gross totals, and exits nonzero on any error.

- [ ] **Step 6: Add the JSON schema and template**

The schema requires `schema_version`, `company_slug`, `year`, `source_files`, `policy`, `vat_periods`, `source_rows`, `allocations`, `monthly_totals`, and `validation`. Allocation monetary fields are numbers and order/date/source identifiers are required non-empty strings. Add a minimal valid Example Company fixture to the template.

- [ ] **Step 7: Run calculation and schema tests**

Run: `.venv/bin/python -m unittest tests.test_woo_tax tests.test_schema_contracts -v`

Expected: PASS.

- [ ] **Step 8: Commit the allocation core**

```bash
git add scripts/woo_tax.py schemas/woo-tax-allocation.schema.json templates/woo-tax-allocation.template.json tests/test_woo_tax.py tests/test_schema_contracts.py
git commit -m "feat: add audited Woo VAT allocation contract"
```

---

### Task 3: Build and apply annual allocations before monthly processing

**Files:**
- Modify: `scripts/woo_tax.py`
- Modify: `scripts/bookprep.py`
- Modify: `scripts/full_year_dry_run.py`
- Modify: `tests/test_woo_tax.py`
- Modify: `tests/test_bookprep.py`
- Modify: `tests/test_full_year_dry_run.py`

**Interfaces:**
- Produces: `load_allocation(path: Path, *, company_slug: str, year: int) -> dict[str, Any]`.
- Produces: `apply_period_allocation(records: dict[str, list[dict[str, Any]]], allocation: dict[str, Any], period: str) -> None`.
- Adds optional `bookprep --woo-tax-allocation PATH`.
- Adds the same path to the full-year runner's `bookprep` command when `artifacts/vat/<year>-woo-tax-allocation.json` exists.

- [ ] **Step 1: Write failing period-application tests**

```python
def normalized_sales_fixture(*, gross: Decimal, vat: Decimal, order_id: str) -> dict[str, list[dict[str, Any]]]:
    return {category: [] for category in (
        "sales", "refunds", "fees", "payouts", "bank_transactions", "purchase_expenses",
        "purchase_credits", "inventory_movements", "manual_adjustments", "other"
    )} | {"sales": [{
        "record_id": f"stripe:{order_id}", "source_system": "stripe", "source_type": "csv",
        "event_type": "stripe_charge", "event_date": "2025-11-27", "settlement_date": None,
        "description": f"Order {order_id}", "external_ref": order_id, "currency": "EUR",
        "gross_amount": float(gross), "net_amount": float(gross - vat), "vat_amount": float(vat),
        "fee_amount": 0.0, "shipping_amount": 0.0, "quantity": None, "sku": None,
        "warehouse_id": None, "channel": "stripe", "country_code": "DE",
        "attributes": {"order_id": order_id},
        "source_refs": [{"source_id": "stripe", "path": "source/stripe.csv", "row_ref": "csv:2",
                         "page_ref": None, "notes": None}],
    }]}

def period_allocation_fixture(*, period: str = "2025-11", order_id: str = "EXAMPLE-1") -> dict[str, Any]:
    payload = allocation_fixture()
    payload["allocations"] = [{
        "source_row_id": "woo-tax:2", "order_id": order_id, "period": period,
        "event_date": "2025-11-27", "country_code": "DE", "processor_ref": "pi_example",
        "configured_rate": 22, "corrected_rate": 24, "original_order_tax": 11.18,
        "original_shipping_tax": 11.18, "fixed_product_gross": 62.00,
        "fixed_shipping_gross": 62.00, "corrected_product_vat": 12.00,
        "corrected_shipping_vat": 12.00, "source_refs": payload["allocations"][0]["source_refs"],
    }]
    return payload

def test_apply_period_allocation_changes_vat_not_customer_gross(self) -> None:
    records = normalized_sales_fixture(gross=Decimal("124.00"), vat=Decimal("22.36"), order_id="EXAMPLE-1")
    allocation = period_allocation_fixture()
    woo_tax.apply_period_allocation(records, allocation, "2025-11")
    sale = records["sales"][0]
    self.assertEqual(Decimal(str(sale["gross_amount"])), Decimal("124.00"))
    self.assertEqual(Decimal(str(sale["vat_amount"])), Decimal("24.00"))
    self.assertEqual(sale["attributes"]["vat_allocation"]["shipping_vat"], 12.00)
```

- [ ] **Step 2: Write failing mixed-year and missing-allocation tests**

```python
def test_apply_period_allocation_does_not_tax_unlisted_export_order(self) -> None:
    records = normalized_sales_fixture(gross=Decimal("50.00"), vat=Decimal("0"), order_id="EXAMPLE-US-1")
    woo_tax.apply_period_allocation(records, period_allocation_fixture(period="2024-02", order_id="EXAMPLE-EU-1"), "2024-02")
    self.assertEqual(records["sales"][0]["vat_amount"], 0.0)

def test_load_allocation_blocks_failed_validation(self) -> None:
    payload = allocation_fixture()
    payload["validation"]["status"] = "failed"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "allocation.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(woo_tax.WooTaxError, "validation status"):
            woo_tax.load_allocation(path, company_slug="example", year=2025)

def test_full_year_runner_blocks_before_months_when_tax_evidence_has_no_valid_allocation(self) -> None:
    with self.assertRaisesRegex(SimplbooksError, "Woo tax allocation"):
        full_year_dry_run.validate_woo_tax_preflight(
            company_dir=Path("companies/example"), year=2025,
            source_dir=Path("companies/example/source/2025-pack")
        )
```

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_woo_tax tests.test_bookprep tests.test_full_year_dry_run -v`

Expected: FAIL on missing allocation loading/application and CLI propagation.

- [ ] **Step 4: Implement allocation loading and monthly application**

Apply allocations by stable order ID to processor/order-level records. For 2024 Woo monthly summaries, aggregate all allocations for the month and set:

```python
sale["vat_amount"] = decimal_number(product_vat + shipping_vat)
sale["attributes"]["vat_allocation"] = {
    "fixed_product_gross": decimal_number(product_gross),
    "fixed_shipping_gross": decimal_number(shipping_gross),
    "product_vat": decimal_number(product_vat),
    "shipping_vat": decimal_number(shipping_vat),
    "allocation_path": display_path(allocation_path, repo_root),
    "allocated_order_ids": sorted(order_ids),
}
```

Validate that fixed product plus shipping gross equals the original Woo/processor gross to the cent. Raise `SimplbooksError` before writing normalized output on mismatch.

- [ ] **Step 5: Wire the allocation into `bookprep` and the full-year runner**

Default allocation path: `company_dir / "artifacts" / "vat" / f"{year}-woo-tax-allocation.json"`. If a Woo tax-summary source exists, missing or invalid allocation is blocking. If no Woo tax-summary source exists, preserve current behavior.

Add `validate_woo_tax_preflight(company_dir: Path, year: int, source_dir: Path | None) -> Path | None`
to `full_year_dry_run.py`. It detects the exact tax-summary headers in the annual source directory,
requires the default allocation path when such evidence exists, calls `woo_tax.load_allocation`,
and returns the validated path. `run_full_year_dry_run` calls it before creating the month loop, so
an invalid annual allocation cannot leave a partly regenerated year.

- [ ] **Step 6: Run focused integration tests**

Run: `.venv/bin/python -m unittest tests.test_woo_tax tests.test_bookprep tests.test_full_year_dry_run -v`

Expected: PASS.

- [ ] **Step 7: Commit annual/monthly integration**

```bash
git add scripts/woo_tax.py scripts/bookprep.py scripts/full_year_dry_run.py tests/test_woo_tax.py tests/test_bookprep.py tests/test_full_year_dry_run.py
git commit -m "feat: apply reviewed Woo VAT allocations"
```

---

### Task 4: Add effective-dated VAT profiles and correct goods/shipping draft lines

**Files:**
- Modify: `schemas/posting-policy.schema.json`
- Modify: `templates/posting-policy.template.json`
- Modify: `scripts/posting_policy.py`
- Modify: `scripts/bookbuilder.py`
- Modify: `scripts/bookchecker.py`
- Modify: `tests/test_posting_policy.py`
- Modify: `tests/test_bookbuilder.py`
- Modify: `tests/test_bookchecker.py`

**Interfaces:**
- Adds `sales_vat_profiles` to posting policy, each with `start`, optional `end`, `rate`, `goods_vat_type_id`, and `shipping_vat_type_id`.
- Produces: `resolve_sales_vat_profile(policy: dict[str, Any], *, event_date: date) -> dict[str, Any]`.
- Produces: `evaluate_vat_profiles(actions: list[dict[str, Any]], posting_policy: dict[str, Any]) -> list[dict[str, Any]]`.
- `bookbuilder.build_sales_lines` consumes `attributes.vat_allocation` and emits separate gross/VAT hints for product and shipping.

- [ ] **Step 1: Write failing effective-profile tests**

```python
def test_resolve_sales_vat_profile_changes_on_effective_date(self) -> None:
    policy = posting_policy_fixture_with_profiles()
    self.assertEqual(resolve_sales_vat_profile(policy, event_date=date(2025, 6, 30))["rate"], 22)
    self.assertEqual(resolve_sales_vat_profile(policy, event_date=date(2025, 7, 1))["rate"], 24)
```

- [ ] **Step 2: Write failing builder component tests**

```python
def policy_with_24_percent_profile() -> dict[str, Any]:
    return {
        "schema_version": "1.0", "company_slug": "example", "bank_accounts": {},
        "contacts": {"sales": {"woo": "42"}, "processors": {}, "suppliers": {}},
        "mappings": {"woo-taxable": {"income_account_id": "107", "shipping_income_account_id": "253",
                                      "vat_type_id": "34", "shipping_vat_type_id": "33",
                                      "warehouse_id": "9"}},
        "sales_vat_profiles": [{"start": "2025-07-01", "end": None, "rate": 24,
                                "goods_vat_type_id": "34", "shipping_vat_type_id": "33"}],
        "supplier_aliases": {},
    }

def allocated_sale_fixture(*, product_gross: float, shipping_gross: float,
                           product_vat: float, shipping_vat: float) -> dict[str, Any]:
    sale = record(record_id="woo:2025-11", source_system="woo", event_type="woo_monthly_sales",
                  gross_amount=product_gross + shipping_gross, vat_amount=product_vat + shipping_vat,
                  shipping_amount=shipping_gross, channel="woo")
    sale["event_date"] = "2025-11-30"
    sale["attributes"]["vat_allocation"] = {
        "fixed_product_gross": product_gross, "fixed_shipping_gross": shipping_gross,
        "product_vat": product_vat, "shipping_vat": shipping_vat,
        "allocation_path": "companies/example/artifacts/vat/2025-woo-tax-allocation.json",
        "allocated_order_ids": ["EXAMPLE-1"],
    }
    return sale

def build_batch_with_policy(normalized: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        return bookbuilder.build_action_batch(
            normalized_payload=normalized, recon_payload=base_recon(),
            normalized_path=root / "normalized.json", recon_path=root / "recon.json",
            repo_root=root, posting_policy=policy,
        )

def test_builder_preserves_allocated_goods_and_shipping_vat(self) -> None:
    normalized = base_normalized("2025-11")
    normalized["records"]["sales"] = [allocated_sale_fixture(
        product_gross=62.00, shipping_gross=62.00, product_vat=12.00, shipping_vat=12.00
    )]
    batch = build_batch_with_policy(normalized, policy_with_24_percent_profile())
    lines = batch["actions"][0]["payload"]["line_items"]
    self.assertEqual([(line["gross_amount"], line["vat_amount_hint"]) for line in lines],
                     [(62.00, 12.00), (62.00, 12.00)])
    self.assertEqual([line["suggested_vat_type_id"] for line in lines], ["34", "33"])
```

- [ ] **Step 3: Write failing checker mismatch tests**

```python
def allocated_action_fixture(*, line_rate: int, vat_type_id: str) -> dict[str, Any]:
    return {"actions": [{
        "idempotency_key": "example-2025-11-woo", "action_type": "create_invoice_summary",
        "payload": {"document_date": "2025-11-30", "posting_policy_family": "woo-taxable",
                    "line_items": [{"line_role": "sales_revenue", "gross_amount": 62.00,
                                    "vat_amount_hint": 12.00, "suggested_vat_type_id": vat_type_id,
                                    "vat_profile_rate": line_rate,
                                    "vat_profile_period": "2025-07-01/open"}]}
    }]}

def test_checker_blocks_vat_type_rate_mismatch(self) -> None:
    batch = allocated_action_fixture(line_rate=24, vat_type_id="25")
    report = bookchecker.evaluate_vat_profiles(batch["actions"], policy_with_24_percent_profile())
    self.assertTrue(any(item["severity"] == "error" and "VAT profile" in item["summary"] for item in report))
```

- [ ] **Step 4: Run focused tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_posting_policy tests.test_bookbuilder tests.test_bookchecker -v`

Expected: FAIL because profiles and component allocation are not implemented.

- [ ] **Step 5: Extend policy schema and resolver**

Require non-overlapping profiles, exact date parsing, non-negative rate, and integer-like VAT type IDs. Resolve exactly one profile for a taxable sales date; zero or multiple matches are blocking.

- [ ] **Step 6: Build separate fixed-gross goods and shipping lines**

Use allocation component fields when present. Preserve legacy behavior only for records without Woo allocation evidence. Add `vat_profile_rate` and `vat_profile_period` to each allocated line for checker provenance.

- [ ] **Step 7: Make VAT/date/type disagreements checker errors**

For each allocated line, recompute the percentage from gross and VAT hint, compare it to `vat_profile_rate`, and compare the line's VAT type with the effective profile's goods/shipping ID. Use cent tolerance for monetary reconstruction and exact equality for policy rate and type IDs.

- [ ] **Step 8: Run focused and schema tests**

Run: `.venv/bin/python -m unittest tests.test_posting_policy tests.test_bookbuilder tests.test_bookchecker tests.test_schema_contracts -v`

Expected: PASS.

- [ ] **Step 9: Commit effective VAT posting**

```bash
git add schemas/posting-policy.schema.json templates/posting-policy.template.json scripts/posting_policy.py scripts/bookbuilder.py scripts/bookchecker.py tests/test_posting_policy.py tests/test_bookbuilder.py tests/test_bookchecker.py
git commit -m "feat: enforce effective-dated sales VAT"
```

---

### Task 5: Track manual inventory write-offs and verify completion by remnant

**Files:**
- Create: `schemas/manual-inventory-action.schema.json`
- Create: `templates/manual-inventory-action.template.json`
- Create: `scripts/inventory_verification.py`
- Create: `tests/test_inventory_verification.py`
- Modify: `scripts/full_year_dry_run.py`
- Modify: `scripts/booksend.py`
- Modify: `tests/test_full_year_dry_run.py`
- Modify: `tests/test_booksend.py`
- Modify: `tests/test_schema_contracts.py`

**Interfaces:**
- Produces: `load_manual_inventory_actions(path: Path) -> dict[str, Any]`.
- Produces: `evaluate_inventory_action(action: dict[str, Any], remnant_response: dict[str, Any]) -> list[str]`.
- Manual action status values: `required`, `completed`, `verified`.
- `booksend` rejects `manual_inventory_writeoff` before any network call.
- The full-year acceptance summary reports unverified manual actions after processing all months.

- [ ] **Step 1: Write failing manual-action and sender-safety tests**

```python
def manual_action_fixture(*, quantity: int, expected_after: int, status: str) -> dict[str, Any]:
    return {
        "action_type": "manual_inventory_writeoff", "effective_date": "2024-06-30",
        "article_id": "10", "warehouse_id": "20", "quantity": quantity,
        "expense_account_id": "30", "expected_remnant_after": expected_after,
        "reason": "Obsolete inventory", "approval": "reviewed", "status": status,
        "source_refs": [{"source_id": "inventory-decision", "path": "artifacts/inventory-decision.json",
                         "row_ref": None, "page_ref": None, "notes": None}],
    }

def test_inventory_writeoff_verifies_expected_remnant(self) -> None:
    action = manual_action_fixture(quantity=5, expected_after=0, status="completed")
    self.assertEqual(evaluate_inventory_action(action, {"data": {"10": {"20": 0}}}), [])

def test_sender_rejects_manual_inventory_writeoff(self) -> None:
    action = invoice_action()
    action["action_type"] = "manual_inventory_writeoff"
    with self.assertRaisesRegex(SimplbooksError, "manual inventory"):
        booksend.translate_action(action, lookup={})
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_inventory_verification tests.test_booksend tests.test_full_year_dry_run -v`

Expected: FAIL because the contract and guard are absent.

- [ ] **Step 3: Add the manual-action schema and Example Company template**

Require `effective_date`, `article_id`, `warehouse_id`, positive `quantity`, `expense_account_id`, `expected_remnant_after`, `reason`, `approval`, `status`, and `source_refs`. The template uses generic IDs and names only.

- [ ] **Step 4: Implement read-only verification logic**

Use `SimplbooksClient.request(f"articles/remnant/{article_id}", method="POST", payload={"warehouse_id": warehouse_id, "date": effective_date})`. Save the response and verification timestamp under the company-local discovery directory. Do not add a write method.

- [ ] **Step 5: Add annual acceptance and sender guards**

`summarize_action_artifacts` loads `artifacts/actions/<year>-inventory-manual.json`, reports its
status, and adds an annual acceptance issue while status is `required` or `completed` without
matching remnant evidence. This check runs after all monthly dry-run steps, so useful monthly API
simulations are still produced while year-close readiness remains false. `booksend` rejects a
manual inventory action before endpoint translation in both dry-run and write modes.

- [ ] **Step 6: Run inventory, sender, checker, and schema tests**

Run: `.venv/bin/python -m unittest tests.test_inventory_verification tests.test_booksend tests.test_full_year_dry_run tests.test_schema_contracts -v`

Expected: PASS.

- [ ] **Step 7: Commit manual inventory safeguards**

```bash
git add schemas/manual-inventory-action.schema.json templates/manual-inventory-action.template.json scripts/inventory_verification.py tests/test_inventory_verification.py scripts/full_year_dry_run.py scripts/booksend.py tests/test_full_year_dry_run.py tests/test_booksend.py tests/test_schema_contracts.py
git commit -m "feat: track manual inventory writeoff verification"
```

---

### Task 6: Populate private company policy, rebuild both years, and verify blockers

**Files:**
- Modify, ignored: `companies/<company>/artifacts/posting_policy.json`
- Create, ignored: `companies/<company>/artifacts/vat/2024-woo-tax-allocation.json`
- Create, ignored: `companies/<company>/artifacts/vat/2025-woo-tax-allocation.json`
- Create, ignored: `companies/<company>/artifacts/actions/2024-inventory-manual.json`
- Regenerate, ignored: `companies/<company>/artifacts/normalized/2024-??.json`
- Regenerate, ignored: `companies/<company>/artifacts/normalized/2025-??.json`
- Regenerate, ignored: `companies/<company>/artifacts/recon/*.json`
- Regenerate, ignored: `companies/<company>/artifacts/actions/*.yaml`
- Regenerate, ignored: `companies/<company>/artifacts/submissions/*-dry-run-summary.json`
- Update, ignored: `companies/<company>/artifacts/pre-submit-readiness.md`

**Interfaces:**
- Consumes all generic contracts and scripts from Tasks 1–5.
- Produces reviewed private annual allocations and dry-run evidence; produces no live Simplbooks writes.

- [ ] **Step 1: Create reviewed company-local VAT policy and allocations**

Encode no OSS, reviewed Estonia dispatch origin, fixed-gross merchant absorption, the applicable effective Estonian rate periods, and the reviewed Simplbooks goods/shipping VAT type IDs. Allocate only the taxable orders present in each Woo tax export. Keep all other 2024 Woo orders outside the allocation; allocate all three 2025 Woo orders.

- [ ] **Step 2: Validate expected VAT totals before rebuilding**

Run: `.venv/bin/python scripts/woo_tax.py validate --company-dir companies/<company> --year 2024`

Expected: status `pass`, the original and corrected VAT totals match the reviewed private allocation, customer gross is unchanged, and no tax-source orders are unallocated.

Run: `.venv/bin/python scripts/woo_tax.py validate --company-dir companies/<company> --year 2025`

Expected: status `pass`, the original and corrected VAT totals match the reviewed private allocation, customer gross is unchanged, and every tax-source order is allocated.

- [ ] **Step 3: Record the approved manual inventory write-off**

Create the private action with the approved 2024 date, article, warehouse, quantity, account, reason, and status `required`. Confirm that pre-submit readiness remains blocked until the UI write-off is completed and the historical remnant becomes the expected post-write-off quantity.

- [ ] **Step 4: Run all generic tests before regenerating artifacts**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: PASS with no failures or errors.

- [ ] **Step 5: Run 2024 and 2025 full-year dry runs**

Run: `.venv/bin/python scripts/full_year_dry_run.py --company-dir companies/<company> --year 2024 --source-dir companies/<company>/source/2024-pack --force-build`

Run: `.venv/bin/python scripts/full_year_dry_run.py --company-dir companies/<company> --year 2025 --source-dir companies/<company>/source/2025-pack --force-build`

Expected: every month reaches `booksend --mode dry-run`; annual acceptance has no VAT allocation, source-manifest, or policy mismatch issues. The manual inventory requirement remains explicitly blocked until completed in Simplbooks.

- [ ] **Step 6: Inspect exact full-year invariants**

Verify with `jq` that:

- annual processor/Woo customer gross is unchanged from the pre-change artifacts;
- corrected taxable VAT equals each validated private allocation's annual total;
- no non-EU 2024 order gained VAT;
- 2025 November uses the 24% goods and shipping VAT types;
- each Woo tax source hash appears in the manifest and allocation;
- every tax-source order is allocated exactly once;
- no manual inventory action appears in dry-run API calls.

- [ ] **Step 7: Update private readiness evidence**

Record the final dry-run paths, VAT totals, remaining manual write-off status, and the fact that prior submitted declarations were not used as source evidence.

- [ ] **Step 8: Review repository status without committing private data**

Run: `git status --short --branch`

Expected: generic implementation commits are present; no `companies/<company>/` or raw source path is staged or tracked.

---

### Task 7: Final verification and review handoff

**Files:**
- Review only: all files changed by Tasks 1–6

**Interfaces:**
- Consumes the complete test suite and both private dry-run summaries.
- Produces the evidence needed for code review and later explicit posting approval.

- [ ] **Step 1: Run formatting and diff safety checks**

Run: `git diff --check HEAD~5..HEAD`

Expected: no whitespace errors.

- [ ] **Step 2: Run the full automated test suite again**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: PASS with no failures or errors.

- [ ] **Step 3: Confirm no live write was made**

Inspect both dry-run summaries and request logs. Every proposed API call must be simulated, and there must be no new successful write submission record.

- [ ] **Step 4: Request code review**

Use `superpowers:requesting-code-review` against the implementation commits. Review must cover fixed-gross arithmetic, mixed EU/non-EU classification, effective VAT profiles, allocation completeness, privacy, and inventory-write-off safety.

- [ ] **Step 5: Resolve review findings and rerun affected tests**

Apply only verified findings, then rerun the focused test module plus the full suite. Commit each logically independent correction with a descriptive `fix:` message.

- [ ] **Step 6: Report the remaining operational gate**

State that live bookkeeping posting remains unauthorized until action batches are explicitly approved. State separately whether the manual inventory write-off has been completed and verified; do not represent a dry-run or required manual action as posted accounting data.
