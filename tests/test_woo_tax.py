from __future__ import annotations

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import woo_tax  # noqa: E402


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


def monthly_woo_summary_fixture(*, gross: Decimal, vat: Decimal, period: str) -> dict[str, list[dict[str, Any]]]:
    records = normalized_sales_fixture(gross=gross, vat=vat, order_id=f"{period}-30")
    sale = records["sales"][0]
    sale["record_id"] = f"woo:{period}"
    sale["source_system"] = "woo"
    sale["event_type"] = "woo_monthly_sales"
    sale["event_date"] = f"{period}-30"
    sale["external_ref"] = f"{period}-30"
    sale["channel"] = "woo"
    sale["attributes"] = {"orders": 2}
    return records


class WooTaxTests(unittest.TestCase):
    def test_apply_period_allocation_changes_vat_not_customer_gross(self) -> None:
        records = normalized_sales_fixture(
            gross=Decimal("124.00"), vat=Decimal("22.36"), order_id="EXAMPLE-1"
        )

        woo_tax.apply_period_allocation(records, period_allocation_fixture(), "2025-11")

        sale = records["sales"][0]
        self.assertEqual(Decimal(str(sale["gross_amount"])), Decimal("124.00"))
        self.assertEqual(Decimal(str(sale["vat_amount"])), Decimal("24.00"))
        self.assertEqual(Decimal(str(sale["net_amount"])), Decimal("100.00"))
        self.assertEqual(sale["attributes"]["vat_allocation"]["shipping_vat"], 12.00)
        self.assertEqual(sale["attributes"]["vat_allocation"]["product_net"], 50.00)
        self.assertEqual(sale["attributes"]["vat_allocation"]["shipping_net"], 50.00)
        self.assertEqual(
            Decimal(str(sale["gross_amount"])),
            Decimal(str(sale["net_amount"])) + Decimal(str(sale["vat_amount"])),
        )
        allocation_details = sale["attributes"]["vat_allocation"]
        self.assertEqual(
            Decimal(str(allocation_details["fixed_product_gross"])),
            Decimal(str(allocation_details["product_net"])) + Decimal(str(allocation_details["product_vat"])),
        )
        self.assertEqual(
            Decimal(str(allocation_details["fixed_shipping_gross"])),
            Decimal(str(allocation_details["shipping_net"])) + Decimal(str(allocation_details["shipping_vat"])),
        )

    def test_apply_period_allocation_does_not_tax_unlisted_export_order(self) -> None:
        records = normalized_sales_fixture(gross=Decimal("50.00"), vat=Decimal("5.00"), order_id="EXAMPLE-US-1")
        records["sales"].append(
            normalized_sales_fixture(gross=Decimal("124.00"), vat=Decimal("22.36"), order_id="EXAMPLE-EU-1")["sales"][0]
        )

        woo_tax.apply_period_allocation(
            records,
            period_allocation_fixture(period="2024-02", order_id="EXAMPLE-EU-1"),
            "2024-02",
        )

        self.assertEqual(records["sales"][0]["vat_amount"], 0.0)
        self.assertEqual(records["sales"][0]["net_amount"], 50.0)
        self.assertEqual(records["sales"][1]["vat_amount"], 24.0)
        self.assertEqual(records["sales"][1]["net_amount"], 100.0)

    def test_apply_period_allocation_zero_rates_woo_linked_order_when_period_has_no_allocations(self) -> None:
        records = normalized_sales_fixture(
            gross=Decimal("50.00"), vat=Decimal("5.00"), order_id="EXAMPLE-US-1"
        )

        woo_tax.apply_period_allocation(records, allocation_fixture(), "2025-11")

        sale = records["sales"][0]
        self.assertEqual(sale["gross_amount"], 50.0)
        self.assertEqual(sale["vat_amount"], 0.0)
        self.assertEqual(sale["net_amount"], 50.0)

    def test_apply_period_summary_allocation_preserves_unrelated_non_woo_sale(self) -> None:
        records = monthly_woo_summary_fixture(
            gross=Decimal("124.00"), vat=Decimal("22.36"), period="2025-11"
        )
        unrelated = normalized_sales_fixture(
            gross=Decimal("75.00"), vat=Decimal("15.00"), order_id="MARKETPLACE-1"
        )["sales"][0]
        unrelated.update({"source_system": "marketplace", "channel": "marketplace"})
        unrelated["attributes"] = {}
        unrelated["external_ref"] = "pi_example"
        linked_processor = normalized_sales_fixture(
            gross=Decimal("124.00"), vat=Decimal("22.36"), order_id="EXAMPLE-1"
        )["sales"][0]
        records["sales"].extend([unrelated, linked_processor])

        woo_tax.apply_period_allocation(records, period_allocation_fixture(), "2025-11")

        self.assertEqual(unrelated["gross_amount"], 75.0)
        self.assertEqual(unrelated["vat_amount"], 15.0)
        self.assertEqual(unrelated["net_amount"], 60.0)
        self.assertEqual(linked_processor["gross_amount"], 124.0)
        self.assertEqual(linked_processor["vat_amount"], 0.0)
        self.assertEqual(linked_processor["net_amount"], 124.0)

    def test_apply_period_summary_allocation_blocks_ambiguous_processor_vat(self) -> None:
        records = monthly_woo_summary_fixture(
            gross=Decimal("124.00"), vat=Decimal("22.36"), period="2025-11"
        )
        ambiguous = normalized_sales_fixture(
            gross=Decimal("75.00"), vat=Decimal("15.00"), order_id="UNPROVEN-1"
        )["sales"][0]
        ambiguous["attributes"] = {}
        ambiguous["external_ref"] = "ch_unproven"
        records["sales"].append(ambiguous)

        with self.assertRaisesRegex(woo_tax.WooTaxError, "ambiguous processor sale"):
            woo_tax.apply_period_allocation(records, period_allocation_fixture(), "2025-11")

        self.assertEqual(ambiguous["vat_amount"], 15.0)
        self.assertEqual(ambiguous["net_amount"], 60.0)

    def test_apply_period_allocation_aggregates_monthly_woo_summary(self) -> None:
        records = monthly_woo_summary_fixture(gross=Decimal("248.00"), vat=Decimal("44.72"), period="2025-11")
        allocation = period_allocation_fixture()
        allocation["allocations"].append({
            **period_allocation_fixture(order_id="EXAMPLE-2")["allocations"][0],
            "order_id": "EXAMPLE-2",
        })

        woo_tax.apply_period_allocation(records, allocation, "2025-11")

        sale = records["sales"][0]
        self.assertEqual(sale["vat_amount"], 48.0)
        self.assertEqual(sale["net_amount"], 200.0)
        self.assertEqual(sale["attributes"]["vat_allocation"]["allocated_order_ids"], ["EXAMPLE-1", "EXAMPLE-2"])
        self.assertEqual(sale["attributes"]["vat_allocation"]["fixed_product_gross"], 124.0)
        self.assertEqual(sale["attributes"]["vat_allocation"]["fixed_shipping_gross"], 124.0)

    def test_apply_period_allocation_leaves_unallocated_month_summary_zero_rated(self) -> None:
        records = monthly_woo_summary_fixture(gross=Decimal("50.00"), vat=Decimal("10.00"), period="2025-11")

        woo_tax.apply_period_allocation(records, allocation_fixture(), "2025-11")

        sale = records["sales"][0]
        self.assertEqual(sale["gross_amount"], 50.0)
        self.assertEqual(sale["net_amount"], 50.0)
        self.assertEqual(sale["vat_amount"], 0.0)
        self.assertNotIn("vat_allocation", sale["attributes"])

    def test_apply_period_allocation_blocks_unmatched_allocated_order(self) -> None:
        records = normalized_sales_fixture(gross=Decimal("50.00"), vat=Decimal("0"), order_id="EXAMPLE-US-1")

        with self.assertRaisesRegex(woo_tax.WooTaxError, "no matching sale"):
            woo_tax.apply_period_allocation(records, period_allocation_fixture(), "2025-11")

    def test_apply_period_allocation_does_not_match_unrelated_sale_by_external_ref(self) -> None:
        records = normalized_sales_fixture(
            gross=Decimal("124.00"), vat=Decimal("15.00"), order_id="MARKETPLACE-1"
        )
        unrelated = records["sales"][0]
        unrelated.update({"source_system": "marketplace", "channel": "marketplace"})
        unrelated["attributes"] = {}
        unrelated["external_ref"] = "EXAMPLE-1"
        original = copy.deepcopy(unrelated)

        with self.assertRaisesRegex(woo_tax.WooTaxError, "no matching sale.*EXAMPLE-1"):
            woo_tax.apply_period_allocation(records, period_allocation_fixture(), "2025-11")

        self.assertEqual(unrelated, original)

    def test_apply_period_allocation_rejects_gross_mismatch(self) -> None:
        records = normalized_sales_fixture(gross=Decimal("123.99"), vat=Decimal("22.36"), order_id="EXAMPLE-1")

        with self.assertRaisesRegex(woo_tax.WooTaxError, "does not match processor gross"):
            woo_tax.apply_period_allocation(records, period_allocation_fixture(), "2025-11")

    def test_load_allocation_blocks_failed_validation(self) -> None:
        payload = allocation_fixture()
        payload["validation"]["status"] = "failed"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allocation.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(woo_tax.WooTaxError, "validation status"):
                woo_tax.load_allocation(path, company_slug="example", year=2025)

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

    def test_validate_allocation_rejects_duplicate_and_unallocated_counts(self) -> None:
        payload = allocation_fixture()
        payload["allocations"].append(copy.deepcopy(payload["allocations"][0]))
        errors = woo_tax.validate_allocation(payload)
        self.assertIn("taxable order is allocated more than once", " ".join(errors))
        self.assertIn("allocated order count", " ".join(errors))

    def test_validate_allocation_keeps_non_taxable_orders_outside_allocation(self) -> None:
        # Completeness is tied to tax-summary Orders counts, not every Woo/processor order in the year.
        self.assertEqual(woo_tax.validate_allocation(allocation_fixture()), [])

    def test_validate_allocation_rejects_corrected_vat_that_changes_fixed_gross_split(self) -> None:
        payload = allocation_fixture()
        payload["allocations"][0]["corrected_product_vat"] = 10.81
        errors = woo_tax.validate_allocation(payload)
        self.assertIn("corrected product VAT does not match fixed-gross calculation", " ".join(errors))

    def test_validate_allocation_rejects_fractional_cent_source_evidence(self) -> None:
        payload = allocation_fixture()
        payload["source_rows"][0]["total_tax"] = 20.001
        errors = woo_tax.validate_allocation(payload)
        self.assertIn("source row woo-tax:2 has fractional-cent total_tax", " ".join(errors))

    def test_validate_allocation_returns_error_for_non_object_monthly_totals(self) -> None:
        payload = allocation_fixture()
        payload["monthly_totals"] = []
        self.assertIn("monthly_totals must be an object", woo_tax.validate_allocation(payload))

    def test_validate_allocation_rejects_policy_without_fixed_gross_absorption(self) -> None:
        payload = allocation_fixture()
        payload["policy"]["merchant_absorbs_vat"] = False
        self.assertIn("merchant_absorbs_vat must be true", woo_tax.validate_allocation(payload))

    def test_build_allocation_marks_non_absorbed_policy_as_failed(self) -> None:
        review = allocation_fixture()
        review["policy"]["merchant_absorbs_vat"] = False
        self.assertEqual(woo_tax.build_allocation(review)["validation"]["status"], "fail")

    def test_validate_allocation_rejects_fractional_cent_monthly_total(self) -> None:
        payload = allocation_fixture()
        payload["monthly_totals"]["2025-05"]["corrected_vat"] = 21.641
        errors = woo_tax.validate_allocation(payload)
        self.assertIn("monthly totals 2025-05 has fractional-cent corrected_vat", errors)

    def test_build_allocation_derives_rate_components_and_month_totals(self) -> None:
        review = allocation_fixture()
        allocation = review["allocations"][0]
        del allocation["period"]
        del allocation["corrected_rate"]
        del allocation["corrected_product_vat"]
        del allocation["corrected_shipping_vat"]

        built = woo_tax.build_allocation(review)

        self.assertEqual(built["allocations"][0]["period"], "2025-05")
        self.assertEqual(built["allocations"][0]["corrected_rate"], 22.0)
        self.assertEqual(built["allocations"][0]["corrected_product_vat"], 10.82)
        self.assertEqual(built["monthly_totals"], {
            "2025-05": {"gross": 120.0, "original_vat": 20.0, "corrected_vat": 21.64}
        })
        self.assertEqual(built["validation"], {"status": "pass", "errors": []})

    def test_validate_cli_reports_recalculated_annual_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "example"
            allocation_path = company_dir / "artifacts" / "vat" / "2025-woo-tax-allocation.json"
            allocation_path.parent.mkdir(parents=True)
            (company_dir / "METADATA.md").write_text("Company slug: example\n", encoding="utf-8")
            allocation_path.write_text(json.dumps(allocation_fixture()), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = woo_tax.main(["validate", "--company-dir", str(company_dir), "--year", "2025"])

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {
            "corrected_vat": 21.64, "errors": [], "gross": 120.0, "original_vat": 20.0
        })


if __name__ == "__main__":
    unittest.main()
