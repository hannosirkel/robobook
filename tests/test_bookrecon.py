from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookrecon  # noqa: E402


def base_normalized(period: str = "2024-01") -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "period": period,
        "base_currency": "EUR",
        "generated_at": "2026-04-04T00:00:00Z",
        "sources": [],
        "records": {category: [] for category in bookrecon.RECORD_CATEGORIES},
        "exceptions": [],
    }


def record(
    *,
    record_id: str,
    source_system: str,
    event_type: str,
    gross_amount: float,
    net_amount: float | None = None,
    description: str = "record",
    channel: str | None = None,
    currency: str = "EUR",
    quantity: float | None = None,
    attributes: dict | None = None,
) -> dict:
    return {
        "record_id": record_id,
        "source_system": source_system,
        "source_type": "csv",
        "event_type": event_type,
        "event_date": "2024-01-15",
        "settlement_date": "2024-01-15",
        "description": description,
        "external_ref": None,
        "currency": currency,
        "gross_amount": gross_amount,
        "net_amount": gross_amount if net_amount is None else net_amount,
        "vat_amount": 0.0,
        "fee_amount": 0.0,
        "shipping_amount": 0.0,
        "quantity": quantity,
        "sku": None,
        "warehouse_id": None,
        "channel": channel,
        "country_code": None,
        "attributes": attributes or {},
        "source_refs": [{"source_id": "src-1", "path": "companies/example/source/file.csv", "row_ref": "csv:2", "page_ref": None, "notes": None}],
    }


def find_check(document: dict, check_id: str) -> dict:
    for check in document["checks"]:
        if check["check_id"] == check_id:
            return check
    raise AssertionError(f"Missing check {check_id}")


class BookreconTests(unittest.TestCase):
    def test_processor_classifier_ignores_refs_nested_in_woo_vat_evidence(self) -> None:
        woo_sale = record(
            record_id="woo:1",
            source_system="woo",
            channel="woo",
            event_type="woo_daily_sales",
            gross_amount=113.0,
            attributes={
                "vat_allocation": {
                    "component_vat_evidence": [
                        {"order_id": "EXAMPLE-1", "processor_ref": "paypal-reference"}
                    ]
                }
            },
        )

        self.assertIsNone(bookrecon.infer_processor(woo_sale))
        stripe_sale = dict(woo_sale, source_system="stripe", channel="stripe", event_type="stripe_charge")
        self.assertEqual(bookrecon.infer_processor(stripe_sale), "stripe")

    def test_supplier_credit_check_reports_total_by_currency(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_credits"].append(
            record(
                record_id="printful:credit:1",
                source_system="printful",
                event_type="printful_supplier_credit",
                gross_amount=113.12,
                description="Printful supplier credit",
            )
        )

        checks = bookrecon.build_purchase_credit_checks(
            normalized_path_display="normalized/2024-07.json",
            records=normalized["records"],
        )

        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], "pass")
        self.assertEqual(checks[0]["lhs_amount"], 113.12)
        self.assertEqual(checks[0]["lhs_label"], "Supplier credits (EUR)")

    def test_blocking_normalized_exception_blocks_build(self) -> None:
        normalized = base_normalized()
        normalized["exceptions"].append(
            {
                "exception_id": "source-gap",
                "severity": "error",
                "reason": "Source export is missing.",
                "blocking": True,
                "suggested_follow_up": "Add the export.",
                "source_refs": [{"source_id": "src-1", "path": "x.csv", "row_ref": None, "page_ref": None, "notes": None}],
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        self.assertFalse(document["approve_for_build"])
        self.assertEqual(document["blocking_issue_count"], 1)
        self.assertTrue(any(item["exception_id"] == "normalized:source-gap" for item in document["exceptions"]))

    def test_woo_sales_vs_processor_gross_passes(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="woo:1",
                source_system="woo",
                channel="woo",
                event_type="woo_daily_sales",
                gross_amount=113.0,
                net_amount=90.0,
                description="Woo daily sales",
            )
        )
        normalized["records"]["sales"].append(
            record(
                record_id="paypal:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_website_payment",
                gross_amount=113.0,
                net_amount=108.0,
                description="PayPal Website Payment",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        check = find_check(document, "woo-sales-vs-processor-gross")
        self.assertEqual(check["status"], "pass")
        self.assertTrue(any("posting basis should stay" in note.lower() for note in check["notes"]))
        self.assertTrue(document["approve_for_build"])

    def test_missing_processor_evidence_from_bank_blocks(self) -> None:
        normalized = base_normalized()
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:stripe:1",
                source_system="bank",
                event_type="bank_credit",
                gross_amount=85.0,
                description="Stripe payout January",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        self.assertFalse(document["approve_for_build"])
        self.assertTrue(any(item["exception_id"] == "bookrecon:missing-processor-evidence:stripe" for item in document["exceptions"]))

    def test_processor_payouts_vs_bank_receipts_passes(self) -> None:
        normalized = base_normalized()
        normalized["records"]["payouts"].append(
            record(
                record_id="paypal:payout:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_withdrawal",
                gross_amount=85.0,
                description="PayPal transfer to bank",
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:paypal:1",
                source_system="bank",
                event_type="bank_credit",
                gross_amount=85.0,
                description="PayPal transfer to bank",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        check = find_check(document, "processor-payouts-vs-bank:paypal")
        self.assertEqual(check["status"], "pass")

    def test_continuity_warns_on_missing_previous_source_system(self) -> None:
        current = base_normalized("2024-01")
        current["records"]["bank_transactions"].append(
            record(
                record_id="bank:1",
                source_system="bank",
                event_type="bank_credit",
                gross_amount=20.0,
                description="Bank receipt",
            )
        )

        previous = base_normalized("2023-12")
        previous["records"]["sales"].append(
            record(
                record_id="paypal:sale:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_website_payment",
                gross_amount=20.0,
                description="PayPal Website Payment",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=current,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
                previous_payload=previous,
                previous_path=Path(tmp) / "2023-12.json",
            )

        check = find_check(document, "continuity-vs-previous-period")
        self.assertEqual(check["status"], "warn")
        self.assertTrue(any("paypal" in note for note in check["notes"]))

    def test_fulfillment_checks_are_currency_aware(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="printful:eur:expense",
                source_system="printful",
                channel="printful",
                event_type="printful_order_charge",
                gross_amount=7.9,
                description="Printful EUR expense",
                currency="EUR",
            )
        )
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="printful:usd:expense",
                source_system="printful",
                channel="printful",
                event_type="printful_service_charge",
                gross_amount=306.32,
                description="Printful USD expense",
                currency="USD",
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="printful:usd:bank",
                source_system="printful",
                channel="printful",
                event_type="printful_wallet_deposit",
                gross_amount=-306.32,
                description="Printful wallet funding",
                currency="USD",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        usd_check = find_check(document, "fulfillment-expenses-vs-bank:printful:usd")
        eur_check = find_check(document, "fulfillment-expenses-vs-bank:printful:eur")
        self.assertEqual(usd_check["status"], "pass")
        self.assertEqual(eur_check["status"], "warn")

    def test_quartermaster_is_recognized_as_fulfillment_partner(self) -> None:
        normalized = base_normalized("2024-10")
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="quartermaster:expense",
                source_system="quartermaster",
                channel="quartermaster",
                event_type="quartermaster_service_invoice",
                gross_amount=21.0,
                description="Quartermaster storage invoice",
                currency="USD",
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="quartermaster:bank",
                source_system="bank",
                channel="quartermaster",
                event_type="bank_debit",
                gross_amount=-21.0,
                description="Quartermaster payment",
                currency="USD",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-10.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        check = find_check(document, "fulfillment-expenses-vs-bank:quartermaster:usd")
        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["lhs_amount"], 21.0)
        self.assertEqual(check["rhs_amount"], 21.0)


if __name__ == "__main__":
    unittest.main()
