from __future__ import annotations  # noqa: I001

import copy
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookaudit  # noqa: E402, I001


RECORD_CATEGORIES = (
    "sales",
    "refunds",
    "fees",
    "payouts",
    "bank_transactions",
    "purchase_expenses",
    "inventory_movements",
    "manual_adjustments",
    "other",
)


def base_normalized(period: str = "2024-01") -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "period": period,
        "base_currency": "EUR",
        "generated_at": "2026-04-04T00:00:00Z",
        "sources": [
            {
                "source_id": "src-1",
                "path": "companies/example/source/source.csv",
                "sha256": "a" * 64,
                "source_type": "csv",
                "source_system": "paypal",
                "covered_from": f"{period}-01",
                "covered_until": f"{period}-31" if period.endswith("-01") else f"{period}-28",
                "canonical": True,
                "parser_name": "test",
            }
        ],
        "records": {category: [] for category in RECORD_CATEGORIES},
        "exceptions": [],
    }


def record(
    *,
    record_id: str,
    source_system: str,
    event_type: str,
    gross_amount: float,
    net_amount: float | None = None,
    vat_amount: float = 0.0,
    fee_amount: float = 0.0,
    quantity: float | None = None,
    sku: str | None = None,
    warehouse_id: str | None = None,
    description: str = "record",
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
        "currency": "EUR",
        "gross_amount": gross_amount,
        "net_amount": gross_amount if net_amount is None else net_amount,
        "vat_amount": vat_amount,
        "fee_amount": fee_amount,
        "shipping_amount": 0.0,
        "quantity": quantity,
        "sku": sku,
        "warehouse_id": warehouse_id,
        "channel": None,
        "country_code": None,
        "attributes": {},
        "source_refs": [{"source_id": "src-1", "path": "source.csv", "row_ref": "csv:2", "page_ref": None, "notes": None}],
    }


def live_state(
    *,
    invoice_total: float = 24.0,
    output_vat: float = 4.0,
    invoice_rows: list[dict] | None = None,
    purchase_total: float = 0.0,
    input_vat: float = 0.0,
    purchase_rows: list[dict] | None = None,
    incoming_total: float = 0.0,
) -> dict:
    invoices = []
    if invoice_total or output_vat:
        invoices.append(
            {
                "id": "inv-1",
                "created": "2024-01-15",
                "transaction_date": "2024-01-15",
                "total_sum": invoice_total,
                "vat": output_vat,
            }
        )
    purchases = []
    if purchase_total or input_vat:
        purchases.append(
            {
                "id": "pur-1",
                "created": "2024-01-16",
                "transaction_date": "2024-01-16",
                "total_sum": purchase_total,
                "vat": input_vat,
            }
        )
    incomings = []
    if incoming_total:
        incomings.append(
            {
                "id": "inc-1",
                "income_date": "2024-01-20",
                "income_sum": incoming_total,
            }
        )
    return {
        "invoices": invoices,
        "purchases": purchases,
        "incomings": incomings,
        "payments": [],
        "invoice_rows": invoice_rows or [],
        "purchase_rows": purchase_rows or [],
        "missing_business_dates": {
            "invoices": {"created": 0, "transaction_date": 0},
            "purchases": {"created": 0, "transaction_date": 0},
            "incomings": {"income_date": 0},
            "payments": {"payment_date": 0},
        },
        "created_time_outside_scope": {"invoices": 0, "purchases": 0, "incomings": 0, "payments": 0},
        "scope_date_mismatches": {
            "invoices": {"created_only": 0, "transaction_only": 0},
            "purchases": {"created_only": 0, "transaction_only": 0},
            "incomings": {"income_date_only": 0},
            "payments": {"payment_date_only": 0},
        },
    }


class FakeClient:
    def __init__(self, *, paginated: dict[str, list[dict]], requested: dict[str, dict]) -> None:
        self.paginated = paginated
        self.requested = requested

    def paginate(self, path: str, *, payload=None, per_page=1000, start_page=1, max_pages=None, get_mode=None):
        return copy.deepcopy(self.paginated.get(path, []))

    def request(self, path: str, *, method: str = "GET", payload=None):
        return copy.deepcopy(self.requested[path])


class BookauditTests(unittest.TestCase):
    def test_evaluate_source_vs_simplbooks_totals_accepts_integer_live_totals(self) -> None:
        findings = bookaudit.evaluate_source_vs_simplbooks_totals(
            {"currency": "EUR", "invoice_total": 24.0, "purchase_total": 0.0, "incoming_total": 0.0, "payment_total": 0.0},
            {"invoice_total": 20, "purchase_total": 0, "incoming_total": 0, "payment_total": 0},
        )

        self.assertEqual(len(findings), 1)
        self.assertIn("Sales and refund totals vs invoices", findings[0]["summary"])

    def test_collect_live_state_filters_by_business_date_not_created_time(self) -> None:
        client = FakeClient(
            paginated={
                "invoices/list": [{"Invoice": {"id": "inv-1", "created": "2024-01-15", "transaction_date": "2024-01-15", "total_sum": 24.0, "vat": 4.0}}],
                "purchases/list": [{"Purchase": {"id": "pur-1", "created": "2024-01-16", "transaction_date": "2024-01-16", "total_sum": 5.0, "vat": 0.0}}],
                "incomings/list": [
                    {"Incoming": {"id": "inc-1", "income_date": "2024-01-31", "income_sum": 23.0, "created_time": "2024-02-03 10:00:00"}},
                    {"Incoming": {"id": "inc-2", "income_date": "2024-02-01", "income_sum": 99.0, "created_time": "2024-01-31 10:00:00"}},
                ],
                "payments/list": [
                    {"Payment": {"id": "pay-1", "payment_date": "2024-01-25", "payment_sum": 5.0, "created_time": "2024-02-01 08:00:00"}},
                    {"Payment": {"id": "pay-2", "payment_date": "2024-02-01", "payment_sum": 7.0, "created_time": "2024-01-28 08:00:00"}},
                ],
            },
            requested={
                "invoices/get/inv-1": {"_http_status": 200, "data": {"Task": [{"vat_type_id": "vat-std", "article_id": "art-1", "warehouse_id": "wh-1"}]}},
                "purchases/get/pur-1": {"_http_status": 200, "data": {"PurchaseRow": [{"vat_type_id": "vat-zero", "article_id": "art-2", "warehouse_id": "wh-1"}]}},
            },
        )

        state = bookaudit.collect_live_state(client, scope=bookaudit.parse_scope("2024-01"))

        self.assertEqual([item["id"] for item in state["incomings"]], ["inc-1"])
        self.assertEqual([item["id"] for item in state["payments"]], ["pay-1"])
        self.assertEqual(state["created_time_outside_scope"]["incomings"], 1)
        self.assertEqual(state["created_time_outside_scope"]["payments"], 1)
        self.assertEqual(len(state["invoice_rows"]), 1)
        self.assertEqual(len(state["purchase_rows"]), 1)

    def test_collect_live_state_accepts_plural_invoice_wrapper(self) -> None:
        client = FakeClient(
            paginated={
                "invoices/list": [{"invoices": {"id": "inv-1", "created": "2024-01-15", "transaction_date": "2024-01-15", "total_sum": 24.0, "vat": 4.0}}],
                "purchases/list": [],
                "incomings/list": [],
                "payments/list": [],
            },
            requested={
                "invoices/get/inv-1": {"_http_status": 200, "data": {"Task": [{"vat_type_id": "vat-std"}]}},
            },
        )

        state = bookaudit.collect_live_state(client, scope=bookaudit.parse_scope("2024-01"))

        self.assertEqual([item["id"] for item in state["invoices"]], ["inv-1"])
        self.assertEqual(len(state["invoice_rows"]), 1)

    def test_collect_live_state_includes_transaction_date_only_documents(self) -> None:
        client = FakeClient(
            paginated={
                "invoices/list": [
                    {"Invoice": {"id": "inv-1", "created": "2024-02-02", "transaction_date": "2024-01-31", "total_sum": 24.0, "vat": 4.0}},
                ],
                "purchases/list": [
                    {"Purchase": {"id": "pur-1", "created": "2024-02-03", "transaction_date": "2024-01-30", "total_sum": 5.0, "vat": 0.0}},
                ],
                "incomings/list": [],
                "payments/list": [],
            },
            requested={
                "invoices/get/inv-1": {"_http_status": 200, "data": {"Task": []}},
                "purchases/get/pur-1": {"_http_status": 200, "data": {"PurchaseRow": []}},
            },
        )

        state = bookaudit.collect_live_state(client, scope=bookaudit.parse_scope("2024-01"))

        self.assertEqual([item["id"] for item in state["invoices"]], ["inv-1"])
        self.assertEqual([item["id"] for item in state["purchases"]], ["pur-1"])
        self.assertEqual(state["scope_date_mismatches"]["invoices"]["transaction_only"], 1)
        self.assertEqual(state["scope_date_mismatches"]["purchases"]["transaction_only"], 1)

    def test_evaluate_audit_flags_invoice_total_mismatch(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="paypal:sale:1",
                source_system="paypal",
                event_type="paypal_website_payment",
                gross_amount=24.0,
                net_amount=20.0,
                vat_amount=4.0,
                description="PayPal sale",
            )
        )

        evaluation, _, _ = bookaudit.evaluate_audit(
            source_payloads=[normalized],
            live_state=live_state(invoice_total=20.0, output_vat=4.0),
            scope=bookaudit.parse_scope("2024-01"),
            policy_text=None,
        )

        self.assertEqual(evaluation["result"], "fail")
        self.assertTrue(any("Sales and refund totals vs invoices" in item["summary"] for item in evaluation["findings"]))

    def test_evaluate_audit_warns_when_inventory_signals_are_not_preserved(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="woo:sale:1",
                source_system="woo",
                event_type="woo_sales_day",
                gross_amount=24.0,
                net_amount=20.0,
                vat_amount=4.0,
                quantity=1.0,
                sku="BOOK-1",
                warehouse_id="wh-1",
                description="Woo sale",
            )
        )

        evaluation, _, _ = bookaudit.evaluate_audit(
            source_payloads=[normalized],
            live_state=live_state(
                invoice_total=24.0,
                output_vat=4.0,
                invoice_rows=[{"vat_type_id": "vat-std", "article_id": None, "warehouse_id": None}],
            ),
            scope=bookaudit.parse_scope("2024-01"),
            policy_text="Warehouse identity matters materially.",
        )

        self.assertEqual(evaluation["result"], "warn")
        self.assertTrue(any(item["section"] == "inventory_review" for item in evaluation["findings"]))

    def test_source_snapshot_counts_embedded_fees_when_no_explicit_fee_rows_exist(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="paypal:sale:1",
                source_system="paypal",
                event_type="paypal_website_payment",
                gross_amount=24.0,
                net_amount=20.0,
                vat_amount=4.0,
                fee_amount=4.0,
                description="PayPal sale",
            )
        )

        snapshot = bookaudit.build_source_snapshot([normalized], policy_text=None)

        self.assertEqual(snapshot["purchase_total"], 4.0)
        self.assertEqual(snapshot["purchase_record_count"], 1)

    def test_source_snapshot_uses_merchant_posting_basis_when_processor_sales_duplicate_it(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="woo:sale:1",
                source_system="woo",
                event_type="woo_daily_sales",
                gross_amount=24.0,
                vat_amount=4.0,
                description="Woo sale",
            )
        )
        normalized["records"]["sales"].append(
            record(
                record_id="paypal:sale:1",
                source_system="paypal",
                event_type="paypal_website_payment",
                gross_amount=24.0,
                vat_amount=4.0,
                description="PayPal sale",
            )
        )

        snapshot = bookaudit.build_source_snapshot([normalized], policy_text=None)

        self.assertEqual(snapshot["invoice_total"], 24.0)
        self.assertEqual(snapshot["output_vat_total"], 4.0)
        self.assertEqual(snapshot["invoice_record_count"], 1)
        self.assertEqual(snapshot["suppressed_processor_sales_group_count"], 1)
        self.assertTrue(any("settlement evidence" in note for note in snapshot["invoice_posting_basis_notes"]))

    def test_source_snapshot_nets_negative_purchase_corrections(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_expenses"].extend(
            [
                record(
                    record_id="printful:storage:1",
                    source_system="printful",
                    event_type="printful_other_charge",
                    gross_amount=181.5,
                    vat_amount=31.5,
                    description="Printful storage charge",
                ),
                record(
                    record_id="printful:storage:2",
                    source_system="printful",
                    event_type="printful_other_charge",
                    gross_amount=-181.5,
                    vat_amount=-31.5,
                    description="Printful storage correction",
                ),
                record(
                    record_id="printful:order:1",
                    source_system="printful",
                    event_type="printful_order_charge",
                    gross_amount=7.9,
                    vat_amount=0.0,
                    description="Printful order charge",
                ),
            ]
        )

        snapshot = bookaudit.build_source_snapshot([normalized], policy_text=None)

        self.assertEqual(snapshot["purchase_total"], Decimal("7.9"))
        self.assertEqual(snapshot["input_vat_total"], Decimal("0"))  # noqa: FURB157


if __name__ == "__main__":
    unittest.main()
