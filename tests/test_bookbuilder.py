from __future__ import annotations

import tempfile
import hashlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookbuilder  # noqa: E402
import bookchecker  # noqa: E402
import woo_tax  # noqa: E402


RECORD_CATEGORIES = (
    "sales",
    "refunds",
    "fees",
    "payouts",
    "bank_transactions",
    "purchase_expenses",
    "purchase_credits",
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
        "sources": [],
        "records": {category: [] for category in RECORD_CATEGORIES},
        "exceptions": [],
    }


def base_recon(period: str = "2024-01", approve: bool = True) -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "period": period,
        "generated_at": "2026-04-04T00:00:00Z",
        "currency": "EUR",
        "approve_for_build": approve,
        "blocking_issue_count": 0 if approve else 2,
        "checks": [],
        "exceptions": [],
        "notes": [],
    }


def record(
    *,
    record_id: str,
    source_system: str,
    source_type: str = "csv",
    event_type: str,
    gross_amount: float,
    net_amount: float | None = None,
    vat_amount: float = 0.0,
    fee_amount: float = 0.0,
    shipping_amount: float = 0.0,
    description: str = "record",
    channel: str | None = None,
    external_ref: str | None = None,
    attributes: dict | None = None,
) -> dict:
    return {
        "record_id": record_id,
        "source_system": source_system,
        "source_type": source_type,
        "event_type": event_type,
        "event_date": "2024-01-15",
        "settlement_date": "2024-01-15",
        "description": description,
        "external_ref": external_ref,
        "currency": "EUR",
        "gross_amount": gross_amount,
        "net_amount": gross_amount if net_amount is None else net_amount,
        "vat_amount": vat_amount,
        "fee_amount": fee_amount,
        "shipping_amount": shipping_amount,
        "quantity": None,
        "sku": None,
        "warehouse_id": None,
        "channel": channel,
        "country_code": None,
        "attributes": attributes or {},
        "source_refs": [{"source_id": "src-1", "path": "companies/example/source/file.csv", "row_ref": "csv:2", "page_ref": None, "notes": None}],
    }


def find_action(batch: dict, action_type: str, *, endpoint: str | None = None) -> dict:
    for action in batch["actions"]:
        if action["action_type"] != action_type:
            continue
        if endpoint is not None and action["endpoint"] != endpoint:
            continue
        return action
    raise AssertionError(f"Missing action {action_type} {endpoint or ''}")


def actions_of_type(batch: dict, action_type: str) -> list[dict]:
    return [action for action in batch["actions"] if action["action_type"] == action_type]


def bank_row(*, record_id: str, amount: float, event_date: str) -> dict:
    result = record(
        record_id=record_id,
        source_system="bank",
        event_type="bank_credit" if amount > 0 else "bank_debit",
        gross_amount=amount,
        description=f"Bank movement {record_id}",
        attributes={"iban": "EE123", "archive_identifier": record_id},
    )
    result["event_date"] = event_date
    result["settlement_date"] = event_date
    return result


def existing_invoice_allocation(*, record_id: str, invoice_id: str) -> dict:
    return {
        "statement_id": f"archive:{record_id}",
        "record_id": record_id,
        "iban": "EE123",
        "period": "2024-01",
        "disposition": "existing_invoice_receipt",
        "amount": 330.0,
        "currency": "EUR",
        "target": {"simplbooks_id": invoice_id, "document_type": "invoice"},
        "review": {"status": "approved", "rationale": "Exact manual invoice match."},
    }


def direct_sale_allocation(*, row: dict, warehouse_id: str | None = "6") -> dict:
    return {
        "statement_id": f"archive:{row['record_id']}",
        "record_id": row["record_id"],
        "iban": row["attributes"]["iban"],
        "period": row["event_date"][:7],
        "disposition": "direct_sale_receipt",
        "amount": row["gross_amount"],
        "currency": row["currency"],
        "target": {
            "document_type": "invoice",
            "contact_label": "direct-sale",
            "posting_family": "direct-sale-taxable",
            "vat_profile": "taxable",
            "product_description": "Reviewed direct sale",
            "quantity": 1,
            "gross_amount": row["gross_amount"],
            "warehouse_id": warehouse_id,
        },
        "review": {"status": "approved", "rationale": "Reviewed direct bank sale."},
    }


def direct_sale_policy() -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "bank_accounts": {"EE123": {"EUR": "3"}},
        "contacts": {"sales": {"direct-sale": "42"}, "processors": {}, "suppliers": {}},
        "mappings": {
            "direct-sale-taxable": {
                "income_account_id": "107",
                "vat_type_id": "25",
                "warehouse_id": "6",
            }
        },
        "sales_vat_profiles": [{
            "start": "2024-01-01",
            "end": "2024-12-31",
            "rate": 22,
            "goods_vat_type_id": "25",
            "shipping_vat_type_id": "24",
        }],
        "supplier_aliases": {},
    }


def manual_allocation(*, row: dict, disposition: str, target: dict | None = None) -> dict:
    return {
        "statement_id": f"archive:{row['record_id']}",
        "record_id": row["record_id"],
        "iban": row["attributes"]["iban"],
        "period": row["event_date"][:7],
        "disposition": disposition,
        "amount": row["gross_amount"],
        "currency": row["currency"],
        "target": target or {"financial_transaction_kind": disposition},
        "review": {"status": "approved", "rationale": f"Reviewed {disposition}."},
    }


def build_with(*, bank: dict, allocation: dict, **overrides: object) -> dict:
    normalized = base_normalized(bank["event_date"][:7])
    normalized["records"]["bank_transactions"] = [bank]
    allocation = dict(allocation, period=normalized["period"], amount=bank["gross_amount"])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        return bookbuilder.build_action_batch(
            normalized_payload=normalized,
            recon_payload=base_recon(normalized["period"]),
            normalized_path=root / "normalized.json",
            recon_path=root / "recon.json",
            repo_root=root,
            company_profile={"bank_account_ids": ["101"]},
            bank_allocations={
                (allocation["statement_id"], allocation["iban"], allocation["currency"]): allocation,
            },
            **overrides,
        )


def policy_with_24_percent_profile() -> dict:
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


def policy_with_mixed_22_percent_profile() -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "bank_accounts": {},
        "contacts": {"sales": {"woo": "42"}, "processors": {}, "suppliers": {}},
        "mappings": {
            "woo-taxable": {
                "income_account_id": "107", "shipping_income_account_id": "253",
                "vat_type_id": "25", "shipping_vat_type_id": "24", "warehouse_id": "6",
            },
            "woo-non-taxable": {
                "income_account_id": "109", "shipping_income_account_id": "255",
                "vat_type_id": "12", "shipping_vat_type_id": "13", "warehouse_id": "6",
            },
        },
        "sales_vat_profiles": [{
            "start": "2024-01-01", "end": "2024-12-31", "rate": 22,
            "goods_vat_type_id": "25", "shipping_vat_type_id": "24",
        }],
        "supplier_aliases": {},
    }


def allocated_sale_fixture(*, product_gross: float, shipping_gross: float,
                           product_vat: float, shipping_vat: float) -> dict:
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


def build_batch_with_policy(normalized: dict, policy: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        return bookbuilder.build_action_batch(
            normalized_payload=normalized, recon_payload=base_recon(normalized["period"]),
            normalized_path=root / "normalized.json", recon_path=root / "recon.json",
            repo_root=root, posting_policy=policy,
        )


def purchase_summary_action(
    *,
    period: str,
    key: str,
    vendor_hint: str,
    amount: float,
    contact_id: str = "2002",
) -> dict:
    return {
        "idempotency_key": key,
        "period": period,
        "action_type": "create_purchase_summary",
        "method": "POST",
        "endpoint": "purchases/create",
        "payload": {
            "draft_schema": "purchase_summary_v1",
            "document_type": "purchase",
            "document_date": f"{period}-28",
            "currency": "EUR",
            "counterparty": {
                "contact_id": contact_id,
                "display_name_hint": f"{vendor_hint} purchase summary",
            },
            "vendor_hint": vendor_hint,
            "totals": {
                "gross_amount": amount,
                "vat_amount": 0.0,
            },
            "line_items": [
                {
                    "line_role": "purchase_expense",
                    "description": f"{vendor_hint} expense summary",
                    "gross_amount": amount,
                    "vat_amount_hint": 0.0,
                    "suggested_expense_account_id": "6020",
                    "suggested_vat_type_id": "0",
                    "warehouse_id_hint": None,
                    "record_count": 1,
                }
            ],
        },
    }


class BookbuilderTests(unittest.TestCase):
    def test_review_confidence_uses_open_issues_not_informational_notes(self) -> None:
        self.assertEqual(
            bookbuilder.review_confidence(open_issues=[], required_ids=["42"]),
            "high",
        )
        self.assertEqual(
            bookbuilder.review_confidence(open_issues=["Exact unresolved judgment."], required_ids=["42"]),
            "medium",
        )
        self.assertEqual(
            bookbuilder.review_confidence(open_issues=[], required_ids=[None]),
            "low",
        )

    def test_resolved_policy_removes_obsolete_contact_and_shipping_review_notes(self) -> None:
        notes = [
            "Used 'stripe' contact mapping as a fallback for 'paypal'.",
            "No contact/client mapping matched 'paypal'.",
            "Shipping is split into a dedicated draft line; exact VAT allocation between revenue and shipping still needs review.",
            "Built from 2 normalized record(s).",
        ]

        cleaned = bookbuilder.clean_resolved_review_notes(
            notes,
            contact_resolved=True,
            vat_resolved=True,
        )

        self.assertEqual(cleaned, ["Built from 2 normalized record(s)."])
    def test_direct_sales_group_one_monthly_invoice_and_keep_exact_receipts(self) -> None:
        normalized = base_normalized("2024-08")
        rows = [
            bank_row(record_id="direct-a", amount=20.0, event_date="2024-08-27"),
            bank_row(record_id="direct-b", amount=20.0, event_date="2024-08-30"),
        ]
        normalized["records"]["bank_transactions"] = rows
        allocations = {
            (f"archive:{row['record_id']}", "EE123", "EUR"): direct_sale_allocation(row=row)
            for row in rows
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon("2024-08"),
                normalized_path=root / "normalized.json",
                recon_path=root / "recon.json",
                repo_root=root,
                posting_policy=direct_sale_policy(),
                bank_allocations=allocations,
            )

        invoices = actions_of_type(batch, "create_invoice_summary")
        receipts = actions_of_type(batch, "create_incoming_summary")
        self.assertEqual(len(invoices), 1)
        invoice = invoices[0]
        self.assertEqual(invoice["payload"]["counterparty"]["contact_id"], "42")
        self.assertEqual(invoice["payload"]["totals"]["gross_amount"], 40.0)
        self.assertEqual(invoice["payload"]["line_items"][0]["suggested_income_account_id"], "107")
        self.assertEqual(invoice["payload"]["line_items"][0]["suggested_vat_type_id"], "25")
        self.assertEqual(invoice["payload"]["line_items"][0]["warehouse_id_hint"], "6")
        self.assertEqual({ref["record_ref"] for ref in invoice["source_refs"]}, {"direct-a", "direct-b"})
        self.assertEqual([item["payload"]["document_date"] for item in receipts], ["2024-08-27", "2024-08-30"])
        self.assertEqual([item["payload"]["amount"] for item in receipts], [20.0, 20.0])
        self.assertTrue(all(len(item["source_refs"]) == 1 for item in receipts))
        self.assertTrue(all(item["source_refs"][0]["source_kind"] == "physical_bank" for item in receipts))
        self.assertEqual({tuple(item["depends_on"]) for item in receipts}, {(invoice["idempotency_key"],)})

    def test_direct_sale_grouping_does_not_cross_reviewed_posting_dimensions(self) -> None:
        normalized = base_normalized("2024-08")
        rows = [
            bank_row(record_id="warehouse-a", amount=20.0, event_date="2024-08-27"),
            bank_row(record_id="warehouse-b", amount=20.0, event_date="2024-08-30"),
        ]
        normalized["records"]["bank_transactions"] = rows
        first = direct_sale_allocation(row=rows[0])
        second = direct_sale_allocation(row=rows[1], warehouse_id=None)
        second["target"]["posting_family"] = "direct-sale-taxable-no-warehouse"
        allocations = {
            (first["statement_id"], first["iban"], first["currency"]): first,
            (second["statement_id"], second["iban"], second["currency"]): second,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon("2024-08"),
                normalized_path=root / "normalized.json",
                recon_path=root / "recon.json",
                repo_root=root,
                posting_policy={
                    **direct_sale_policy(),
                    "mappings": {
                        **direct_sale_policy()["mappings"],
                        "direct-sale-taxable-no-warehouse": {
                            "income_account_id": "107",
                            "vat_type_id": "25",
                        },
                    },
                },
                bank_allocations=allocations,
            )

        self.assertEqual(len(actions_of_type(batch, "create_invoice_summary")), 2)

    def test_direct_sale_grouping_uses_resolved_posting_tuple_not_policy_key(self) -> None:
        normalized = base_normalized("2024-08")
        rows = [
            bank_row(record_id="family-a", amount=20.0, event_date="2024-08-27"),
            bank_row(record_id="family-b", amount=20.0, event_date="2024-08-30"),
        ]
        normalized["records"]["bank_transactions"] = rows
        first = direct_sale_allocation(row=rows[0])
        second = direct_sale_allocation(row=rows[1])
        second["target"]["posting_family"] = "direct-sale-taxable-alias"
        policy = direct_sale_policy()
        policy["mappings"]["direct-sale-taxable-alias"] = dict(policy["mappings"]["direct-sale-taxable"])
        allocations = {
            (first["statement_id"], first["iban"], first["currency"]): first,
            (second["statement_id"], second["iban"], second["currency"]): second,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon("2024-08"),
                normalized_path=root / "normalized.json",
                recon_path=root / "recon.json",
                repo_root=root,
                posting_policy=policy,
                bank_allocations=allocations,
            )

        self.assertEqual(len(actions_of_type(batch, "create_invoice_summary")), 1)

    def test_bank_fees_create_one_manual_dependency_per_physical_row_and_no_actions(self) -> None:
        normalized = base_normalized("2024-08")
        rows = [
            bank_row(record_id="monthly-card", amount=-2.0, event_date="2024-08-27"),
            bank_row(record_id="transfer-fee", amount=-7.0, event_date="2024-08-30"),
        ]
        normalized["records"]["bank_transactions"] = rows
        allocations = {}
        for row in rows:
            allocation = manual_allocation(row=row, disposition="bank_fee_payment")
            allocations[(allocation["statement_id"], allocation["iban"], allocation["currency"])] = allocation

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon("2024-08"),
                normalized_path=root / "normalized.json",
                recon_path=root / "recon.json",
                repo_root=root,
                bank_allocations=allocations,
            )

        self.assertEqual(batch["actions"], [])
        dependencies = batch["unresolved_dependencies"]
        self.assertEqual(len(dependencies), 2)
        self.assertEqual({item["physical_signed_amount"] for item in dependencies}, {-2.0, -7.0})
        self.assertTrue(all(item["source_ref"]["source_kind"] == "physical_bank" for item in dependencies))
        self.assertTrue(all(item["statement_import_proof"]["status"] == "pending" for item in dependencies))

    def test_failed_payment_transfer_and_return_are_manual_and_zero_net(self) -> None:
        normalized = base_normalized("2024-09")
        rows = [
            bank_row(record_id="failed-out", amount=-30.0, event_date="2024-09-02"),
            bank_row(record_id="failed-return", amount=30.0, event_date="2024-09-04"),
        ]
        normalized["records"]["bank_transactions"] = rows
        allocations = {}
        for row in rows:
            allocation = manual_allocation(
                row=row,
                disposition="clearing_transfer",
                target={"financial_transaction_kind": "failed-payment-transfer-reversal", "pair_ref": "pair-1"},
            )
            allocations[(allocation["statement_id"], allocation["iban"], allocation["currency"])] = allocation

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon("2024-09"),
                normalized_path=root / "normalized.json",
                recon_path=root / "recon.json",
                repo_root=root,
                bank_allocations=allocations,
            )

        self.assertEqual(batch["actions"], [])
        dependencies = batch["unresolved_dependencies"]
        self.assertEqual(sum(item["physical_signed_amount"] for item in dependencies), 0.0)
        self.assertEqual({item["target"]["pair_ref"] for item in dependencies}, {"pair-1"})

    def test_netted_foreign_receipt_is_one_atomic_manual_dependency(self) -> None:
        row = bank_row(record_id="foreign-net", amount=723.32, event_date="2024-08-30")
        row["currency"] = "USD"
        allocation = manual_allocation(row=row, disposition="reviewed_split")
        allocation["parts"] = [
            {"amount": 738.32, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119", "document_type": "invoice"}},
            {"amount": -15.0, "disposition": "bank_fee_payment", "target": {"financial_transaction_kind": "correspondent-fee"}},
        ]

        batch = build_with(bank=row, allocation=allocation)

        self.assertEqual(batch["actions"], [])
        self.assertEqual(len(batch["unresolved_dependencies"]), 1)
        dependency = batch["unresolved_dependencies"][0]
        self.assertEqual([part["signed_amount"] for part in dependency["split_parts"]], [738.32, -15.0])
        self.assertEqual(dependency["split_proof"]["signed_parts_total"], 723.32)
        self.assertEqual(dependency["split_proof"]["physical_signed_amount"], 723.32)

    def test_netted_foreign_receipt_rejects_incorrect_split_arithmetic(self) -> None:
        row = bank_row(record_id="foreign-bad", amount=723.32, event_date="2024-08-30")
        row["currency"] = "USD"
        allocation = manual_allocation(row=row, disposition="reviewed_split")
        allocation["parts"] = [
            {"amount": 738.32, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119", "document_type": "invoice"}},
            {"amount": -14.0, "disposition": "bank_fee_payment", "target": {"financial_transaction_kind": "correspondent-fee"}},
        ]

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "split.*sum"):
            build_with(bank=row, allocation=allocation)

    def test_manual_bank_fee_rejects_positive_physical_amount(self) -> None:
        row = bank_row(record_id="positive-fee", amount=7.0, event_date="2024-08-30")
        allocation = manual_allocation(row=row, disposition="bank_fee_payment")

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "bank_fee_payment.*negative"):
            build_with(bank=row, allocation=allocation)

    def test_netted_foreign_receipt_rejects_reversed_split_signs(self) -> None:
        row = bank_row(record_id="foreign-reversed", amount=-723.32, event_date="2024-08-30")
        row["currency"] = "USD"
        allocation = manual_allocation(row=row, disposition="reviewed_split")
        allocation["parts"] = [
            {"amount": -738.32, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119"}},
            {"amount": 15.0, "disposition": "bank_fee_payment", "target": {"financial_transaction_kind": "fee"}},
        ]

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "existing_invoice_receipt.*positive"):
            build_with(bank=row, allocation=allocation)

    def test_existing_cash_target_requires_discovery_proof(self) -> None:
        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "discovery overview"):
            build_with(
                bank=bank_row(record_id="receipt", amount=330.0, event_date="2024-01-08"),
                allocation=existing_invoice_allocation(record_id="receipt", invoice_id="119"),
            )

    def test_existing_cash_target_rejects_generated_target_field(self) -> None:
        allocation = existing_invoice_allocation(record_id="receipt", invoice_id="119")
        allocation["target"]["action_key"] = "example-2024-01-sales-direct"
        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "both existing and generated"):
            build_with(
                bank=bank_row(record_id="receipt", amount=330.0, event_date="2024-01-08"),
                allocation=allocation,
                discovery_overviews=[{"document_index": [{"simplbooks_id": "119", "document_type": "invoice"}]}],
            )

    def test_exact_cash_actions_reject_disposition_with_wrong_sign(self) -> None:
        receipt = existing_invoice_allocation(record_id="receipt", invoice_id="119")
        receipt["amount"] = -20.0
        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "positive"):
            build_with(
                bank=bank_row(record_id="receipt", amount=-20.0, event_date="2024-01-08"),
                allocation=receipt,
                discovery_overviews=[{"document_index": [{"simplbooks_id": "119", "document_type": "invoice"}]}],
            )

        payment = existing_invoice_allocation(record_id="payment", invoice_id="88")
        payment.update({"disposition": "existing_purchase_payment", "amount": 20.0})
        payment["target"] = {"simplbooks_id": "88", "document_type": "purchase"}
        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "negative"):
            build_with(
                bank=bank_row(record_id="payment", amount=20.0, event_date="2024-01-08"),
                allocation=payment,
                discovery_overviews=[{"document_index": [{"simplbooks_id": "88", "document_type": "purchase"}]}],
            )

    def test_existing_manual_invoice_receipt_uses_statement_date_and_id(self) -> None:
        batch = build_with(
            bank=bank_row(record_id="receipt", amount=330.0, event_date="2024-01-08"),
            allocation=existing_invoice_allocation(record_id="receipt", invoice_id="119"),
            discovery_overviews=[{"document_index": [{"simplbooks_id": "119", "document_type": "invoice"}]}],
        )

        action = find_action(batch, "create_incoming_summary")
        self.assertEqual(action["payload"]["document_date"], "2024-01-08")
        self.assertEqual(action["payload"]["linked_invoice_id"], "119")
        self.assertEqual(action["source_refs"][0]["record_ref"], "receipt")

    def test_multiple_bank_receipts_create_multiple_actions_against_one_invoice(self) -> None:
        normalized = base_normalized("2024-08")
        rows = [
            bank_row(record_id="receipt-a", amount=20.0, event_date="2024-08-04"),
            bank_row(record_id="receipt-b", amount=20.0, event_date="2024-08-05"),
        ]
        normalized["records"]["bank_transactions"] = rows
        normalized["records"]["sales"] = [
            record(
                record_id="direct-sale-summary",
                source_system="direct",
                channel="direct",
                event_type="sales_summary",
                gross_amount=40.0,
            )
        ]
        allocations = {
            (f"archive:{row['record_id']}", "EE123", "EUR"): {
                "statement_id": f"archive:{row['record_id']}",
                "record_id": row["record_id"],
                "iban": "EE123",
                "period": "2024-08",
                "disposition": "generated_invoice_receipt",
                "amount": 20.0,
                "currency": "EUR",
                "target": {"action_key": "example-2024-08-sales-direct", "document_type": "invoice"},
                "review": {"status": "approved", "rationale": "Exact direct-sales allocation."},
            }
            for row in rows
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon("2024-08"),
                normalized_path=root / "normalized.json",
                recon_path=root / "recon.json",
                repo_root=root,
                company_profile={"bank_account_ids": ["101"]},
                bank_allocations=allocations,
            )

        receipts = actions_of_type(batch, "create_incoming_summary")
        self.assertEqual([action["payload"]["amount"] for action in receipts], [20.0, 20.0])
        self.assertEqual(
            {action["payload"]["linked_invoice_action"] for action in receipts},
            {"example-2024-08-sales-direct"},
        )

    def test_exact_cash_actions_map_each_physical_bank_account(self) -> None:
        normalized = base_normalized("2024-01")
        rows = [
            bank_row(record_id="receipt-one", amount=20.0, event_date="2024-01-08"),
            bank_row(record_id="receipt-two", amount=20.0, event_date="2024-01-09"),
        ]
        rows[0]["attributes"]["iban"] = "EE111"
        rows[1]["attributes"]["iban"] = "EE222"
        normalized["records"]["bank_transactions"] = rows
        allocations = {
            (f"archive:{row['record_id']}", row["attributes"]["iban"], "EUR"): {
                "statement_id": f"archive:{row['record_id']}",
                "record_id": row["record_id"],
                "iban": row["attributes"]["iban"],
                "period": "2024-01",
                "disposition": "existing_invoice_receipt",
                "amount": 20.0,
                "currency": "EUR",
                "target": {"simplbooks_id": "119", "document_type": "invoice"},
                "review": {"status": "approved", "rationale": "Exact invoice match."},
            }
            for row in rows
        }
        policy = {
            "schema_version": "1.0", "company_slug": "example",
            "bank_accounts": {"EE111": {"EUR": "3"}, "EE222": {"EUR": "4"}},
            "contacts": {}, "mappings": {}, "supplier_aliases": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon("2024-01"),
                normalized_path=root / "normalized.json",
                recon_path=root / "recon.json",
                repo_root=root,
                posting_policy=policy,
                bank_allocations=allocations,
                discovery_overviews=[{"document_index": [{"simplbooks_id": "119", "document_type": "invoice"}]}],
            )

        self.assertEqual(
            [action["payload"]["bank_account_id"] for action in actions_of_type(batch, "create_incoming_summary")],
            ["3", "4"],
        )

    def test_exact_cash_actions_map_same_iban_by_currency(self) -> None:
        normalized = base_normalized("2024-01")
        rows = [
            bank_row(record_id="receipt-eur", amount=20.0, event_date="2024-01-08"),
            bank_row(record_id="receipt-usd", amount=20.0, event_date="2024-01-09"),
        ]
        rows[1]["currency"] = "USD"
        normalized["records"]["bank_transactions"] = rows
        allocations = {}
        for row in rows:
            currency = row["currency"]
            allocations[(f"archive:{row['record_id']}", "EE123", currency)] = {
                "statement_id": f"archive:{row['record_id']}", "record_id": row["record_id"],
                "iban": "EE123", "period": "2024-01", "disposition": "existing_invoice_receipt",
                "amount": 20.0, "currency": currency,
                "target": {"simplbooks_id": "119", "document_type": "invoice"},
                "review": {"status": "approved", "rationale": "Exact invoice match."},
            }
        policy = {
            "schema_version": "1.0", "company_slug": "example",
            "bank_accounts": {"EE123": {"EUR": "3", "USD": "4"}},
            "contacts": {}, "mappings": {}, "supplier_aliases": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized, recon_payload=base_recon("2024-01"),
                normalized_path=root / "normalized.json", recon_path=root / "recon.json", repo_root=root,
                posting_policy=policy, bank_allocations=allocations,
                discovery_overviews=[{"document_index": [{"simplbooks_id": "119", "document_type": "invoice"}]}],
            )
        self.assertEqual(
            [action["payload"]["bank_account_id"] for action in actions_of_type(batch, "create_incoming_summary")],
            ["3", "4"],
        )

    def test_exact_cash_actions_block_missing_currency_specific_bank_mapping(self) -> None:
        normalized = base_normalized("2024-01")
        row = bank_row(record_id="receipt-usd", amount=20.0, event_date="2024-01-09")
        row["currency"] = "USD"
        normalized["records"]["bank_transactions"] = [row]
        allocation = existing_invoice_allocation(record_id="receipt-usd", invoice_id="119")
        allocation.update({"amount": 20.0, "currency": "USD"})
        policy = {
            "schema_version": "1.0", "company_slug": "example",
            "bank_accounts": {"EE123": {"EUR": "3"}},
            "contacts": {}, "mappings": {}, "supplier_aliases": {},
        }
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(bookbuilder.SimplbooksError, "USD"):
            root = Path(tmp)
            bookbuilder.build_action_batch(
                normalized_payload=normalized, recon_payload=base_recon("2024-01"),
                normalized_path=root / "normalized.json", recon_path=root / "recon.json", repo_root=root,
                posting_policy=policy,
                bank_allocations={("archive:receipt-usd", "EE123", "USD"): allocation},
                discovery_overviews=[{"document_index": [{"simplbooks_id": "119", "document_type": "invoice"}]}],
            )

    def test_processor_classifier_ignores_refs_nested_in_woo_vat_evidence(self) -> None:
        woo_sale = record(
            record_id="woo:sale:1",
            source_system="woo",
            channel="woo",
            event_type="woo_daily_sales",
            gross_amount=110.0,
            attributes={
                "vat_allocation": {
                    "component_vat_evidence": [
                        {"order_id": "EXAMPLE-1", "processor_ref": "paypal-reference"}
                    ]
                }
            },
        )

        self.assertIsNone(bookbuilder.infer_processor(woo_sale))
        stripe_sale = dict(woo_sale, source_system="stripe", channel="stripe", event_type="stripe_charge")
        self.assertEqual(bookbuilder.infer_processor(stripe_sale), "stripe")

    def test_builder_binds_action_batch_to_allocation_and_tax_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allocation_path = root / "allocation.json"
            tax_path = root / "woocommerce-taxes.csv"
            allocation_path.write_text("{}\n", encoding="utf-8")
            tax_path.write_text("tax evidence\n", encoding="utf-8")
            normalized = base_normalized("2025-11")
            sale = allocated_sale_fixture(
                product_gross=62.0, shipping_gross=62.0, product_vat=12.0, shipping_vat=12.0
            )
            sale["attributes"]["vat_allocation"].update({
                "allocation_ref": {
                    "path": "allocation.json", "sha256": hashlib.sha256(allocation_path.read_bytes()).hexdigest()
                },
                "tax_source_refs": [{
                    "source_id": "woo-tax", "path": "woocommerce-taxes.csv",
                    "sha256": hashlib.sha256(tax_path.read_bytes()).hexdigest(), "row_refs": ["csv:2"],
                }],
            })
            normalized["records"]["sales"] = [sale]

            bindings = bookbuilder.bind_woo_tax_reference_artifacts(normalized, cwd=root)

            self.assertEqual([item["kind"] for item in bindings], ["woo_tax_allocation", "woo_tax_source"])
            tax_path.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(bookbuilder.SimplbooksError, "changed"):
                bookbuilder.bind_woo_tax_reference_artifacts(normalized, cwd=root)

    def test_builder_blocks_allocated_vat_without_effective_profile(self) -> None:
        normalized = base_normalized("2025-11")
        normalized["records"]["sales"] = [allocated_sale_fixture(
            product_gross=62.00, shipping_gross=62.00, product_vat=12.00, shipping_vat=12.00
        )]
        policy = policy_with_24_percent_profile()
        policy["sales_vat_profiles"][0]["start"] = "2025-12-01"

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "sales VAT profile"):
            build_batch_with_policy(normalized, policy)

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
        self.assertEqual([line["vat_profile_rate"] for line in lines], [24, 24])
        self.assertEqual([line["vat_profile_period"] for line in lines], ["2025-07-01/open", "2025-07-01/open"])

    def test_builder_emits_one_api_line_per_order_component_for_rounding(self) -> None:
        normalized = base_normalized("2025-11")
        sale = allocated_sale_fixture(
            product_gross=0.06, shipping_gross=0.06, product_vat=0.02, shipping_vat=0.02
        )
        sale["gross_amount"] = 0.12
        sale["net_amount"] = 0.08
        allocation = sale["attributes"]["vat_allocation"]
        allocation.update({
            "allocated_order_ids": ["EXAMPLE-1", "EXAMPLE-2"],
            "allocation_ref": {"path": "companies/example/artifacts/vat/2025-woo-tax-allocation.json", "sha256": "c" * 64},
            "tax_source_refs": [{
                "source_id": "woo-tax", "path": "companies/example/source/2025-pack/woocommerce-taxes.csv",
                "sha256": "a" * 64, "row_refs": ["csv:2"],
            }],
            "component_vat_evidence": [
                {
                    "order_id": f"EXAMPLE-{index}", "source_row_id": "woo-tax:2",
                    "processor_ref": f"pi_example_{index}", "country_code": "DE",
                    "event_date": "2025-11-27",
                    "configured_rate": 22, "corrected_rate": 24,
                    "fixed_product_gross": 0.03, "fixed_shipping_gross": 0.03,
                    "product_vat": 0.01, "shipping_vat": 0.01,
                    "source_refs": [],
                    "vat_profile": {
                        "start": "2025-07-01", "end": None, "rate": 24,
                        "goods_vat_type_id": "34", "shipping_vat_type_id": "33",
                    },
                }
                for index in (1, 2)
            ],
        })
        normalized["records"]["sales"] = [sale]

        batch = build_batch_with_policy(normalized, policy_with_24_percent_profile())
        action = batch["actions"][0]
        lines = action["payload"]["line_items"]

        self.assertEqual(len(lines), 4)
        self.assertEqual(
            [(line["vat_allocation_component"], line["gross_amount"], line["vat_amount_hint"])
             for line in lines],
            [("goods", 0.03, 0.01), ("goods", 0.03, 0.01),
             ("shipping", 0.03, 0.01), ("shipping", 0.03, 0.01)],
        )
        self.assertTrue(all(len(line["vat_allocation_component_evidence"]) == 1 for line in lines))
        self.assertTrue(all(line["vat_allocation_component_evidence"][0]["event_date"] == "2025-11-27" for line in lines))
        self.assertTrue(all(line["vat_evidence_binding"]["allocation_ref"]["sha256"] == "c" * 64 for line in lines))
        self.assertEqual(action["payload"]["totals"]["vat_amount"], 0.04)

    def test_builder_and_checker_preserve_mixed_taxable_and_zero_rated_month_total(self) -> None:
        normalized = base_normalized("2024-04")
        summary = record(
            record_id="woo:2024-04", source_system="woo", event_type="woo_monthly_sales",
            gross_amount=135.54, net_amount=100.0, vat_amount=13.14,
            shipping_amount=22.40, channel="woo",
        )
        summary["event_date"] = "2024-04-30"
        summary["attributes"] = {"is_monthly_summary": True, "orders": 4}
        normalized["records"]["sales"] = [summary]
        allocation_item = {
            "source_row_id": "woo-tax:2", "order_id": "774", "period": "2024-04",
            "event_date": "2024-04-20", "country_code": "FR", "processor_ref": "pi_774",
            "configured_rate": 22, "corrected_rate": 22,
            "original_order_tax": 5.50, "original_shipping_tax": 1.07,
            "fixed_product_gross": 30.50, "fixed_shipping_gross": 5.92,
            "corrected_product_vat": 5.50, "corrected_shipping_vat": 1.07,
            "source_refs": [],
        }
        second = dict(allocation_item)
        second.update({"order_id": "777", "event_date": "2024-04-23", "processor_ref": "pi_777"})

        woo_tax.apply_period_allocation(
            normalized["records"], {
                "allocations": [allocation_item, second],
                "_allocation_path": "companies/example/artifacts/vat/2024-woo-tax-allocation.json",
                "_allocation_sha256": "c" * 64,
                "_tax_evidence": [{
                    "source_id": "woo-tax", "path": "companies/example/source/2024-pack/woocommerce-taxes.csv",
                    "sha256": "a" * 64,
                    "rows": [{"source_row_id": "woo-tax:2", "row_ref": "csv:2"}],
                }],
                "vat_periods": [{
                    "start": "2024-01-01", "end": "2024-12-31", "rate": 22,
                    "goods_vat_type_id": "25", "shipping_vat_type_id": "24",
                }],
            }, "2024-04"
        )
        policy = policy_with_mixed_22_percent_profile()
        batch = build_batch_with_policy(normalized, policy)
        sales_actions = [
            action for action in batch["actions"] if action["action_type"] == "create_invoice_summary"
        ]
        self.assertEqual(len(sales_actions), 2)
        taxable = next(
            action for action in sales_actions
            if action["payload"]["posting_policy_family"] == "woo-taxable"
        )
        zero_rated = next(
            action for action in sales_actions
            if action["payload"]["posting_policy_family"] == "woo-non-taxable"
        )
        self.assertEqual(taxable["payload"]["totals"], {
            "gross_amount": 72.84, "vat_amount": 13.14,
            "shipping_amount": 11.84, "fee_amount_observed": 0.0,
        })
        self.assertEqual(
            [(line["gross_amount"], line["suggested_vat_type_id"]) for line in taxable["payload"]["line_items"]],
            [(30.5, "25"), (30.5, "25"), (5.92, "24"), (5.92, "24")],
        )
        self.assertEqual(zero_rated["payload"]["totals"], {
            "gross_amount": 62.70, "vat_amount": 0.0,
            "shipping_amount": 12.70, "fee_amount_observed": 0.0,
        })
        self.assertEqual(
            [(line["gross_amount"], line["suggested_vat_type_id"]) for line in zero_rated["payload"]["line_items"]],
            [(50.0, "12"), (12.70, "13")],
        )
        self.assertEqual(
            sum(Decimal(str(action["payload"]["totals"]["gross_amount"])) for action in sales_actions),
            Decimal("135.54"),
        )

        records_by_id = {item["record_id"]: item for item in normalized["records"]["sales"]}
        findings = bookchecker.evaluate_posting_policy(batch, policy)
        findings.extend(bookchecker.evaluate_vat_profiles(batch["actions"], policy))
        for action in sales_actions:
            resolved = [
                {"category": "sales", "record": records_by_id[str(ref["record_ref"])]}
                for ref in action["source_refs"]
            ]
            findings.extend(bookchecker.evaluate_arithmetic(action=action, resolved_sources=resolved))
        self.assertFalse([item for item in findings if item["severity"] == "error"], findings)

    def test_builder_applies_single_month_end_ecb_rate(self) -> None:
        normalized = base_normalized(period="2024-03")
        usd_purchase = record(
            record_id="quartermaster:usd:1",
            source_system="quartermaster",
            event_type="quartermaster_service_invoice",
            gross_amount=31.0,
            description="Quartermaster March invoice",
            channel="quartermaster",
        )
        usd_purchase["currency"] = "USD"
        normalized["records"]["purchase_expenses"].append(usd_purchase)
        rate_cache = {
            "schema_version": "1.0",
            "provider": "ECB",
            "year": 2024,
            "base": "USD",
            "quote": "EUR",
            "source_url": "https://api.frankfurter.dev/v2/rates?provider=ECB",
            "retrieved_at": "2026-08-21T00:00:00Z",
            "rates": [
                {"date": "2024-03-28", "base": "USD", "quote": "EUR", "rate": "0.9241"}
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon(period="2024-03"),
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                exchange_rate_cache=rate_cache,
            )

        action = find_action(batch, "create_purchase_summary")
        self.assertEqual(action["payload"]["currency_rate"], 0.9241)
        self.assertEqual(action["payload"]["currency_rate_effective_date"], "2024-03-28")
        self.assertEqual(action["payload"]["currency_rate_provider"], "ECB")

    def test_builder_creates_credit_and_suppresses_exact_existing_purchase(self) -> None:
        normalized = base_normalized(period="2024-07")
        normalized["records"]["purchase_credits"].append(
            record(
                record_id="printful:credit:1",
                source_system="printful",
                event_type="printful_supplier_credit",
                gross_amount=113.12,
                vat_amount=13.12,
                description="Printful supplier credit",
                channel="printful",
                external_ref="105211877",
                attributes={"vendor_name": "Printful Inc."},
            )
        )
        existing_record = record(
            record_id="simplbooks:invoice:1",
            source_system="document",
            source_type="pdf",
            event_type="purchase_invoice_pdf",
            gross_amount=206.18,
            description="SimplBooks invoice",
            channel="simplbooks-ou",
            external_ref="EE24111268",
            attributes={"vendor_name": "SimplBooks OÜ"},
        )
        existing_record["event_date"] = "2024-11-18"
        normalized["records"]["purchase_expenses"].append(existing_record)
        discovery = {
            "document_index": [
                {
                    "document_type": "purchase",
                    "supplier_name": "simplbooks oü",
                    "external_number": "EE24111268",
                    "document_date": "2024-11-18",
                    "currency": "EUR",
                    "gross_amount": 206.18,
                    "simplbooks_id": "157",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon(period="2024-07"),
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                discovery_overview=discovery,
            )

        credit = find_action(batch, "create_purchase_credit_summary")
        self.assertEqual(credit["payload"]["totals"]["gross_amount"], 113.12)
        self.assertEqual(credit["payload"]["totals"]["vat_amount"], 13.12)
        self.assertEqual(credit["payload"]["line_items"][0]["vat_amount_hint"], 13.12)
        self.assertTrue(any(item["external_ref"] == "EE24111268" for item in batch["already_present"]))
        self.assertFalse(any(action["payload"].get("vendor_hint") == "simplbooks-ou" for action in batch["actions"]))

    def test_builder_splits_supplier_credits_by_tax_profile(self) -> None:
        normalized = base_normalized(period="2024-07")
        normalized["records"]["purchase_credits"].extend(
            [
                record(record_id="credit:zero", source_system="printful", event_type="supplier_credit", gross_amount=10, vat_amount=0, channel="printful"),
                record(record_id="credit:taxable", source_system="printful", event_type="supplier_credit", gross_amount=12, vat_amount=2, channel="printful"),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon(period="2024-07"),
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
            )

        credits = [a for a in batch["actions"] if a["action_type"] == "create_purchase_credit_summary"]
        self.assertEqual(len(credits), 2)
        self.assertEqual(sorted(a["payload"]["totals"]["vat_amount"] for a in credits), [0.0, 2.0])

    def test_builder_clears_legacy_ids_when_policy_family_is_missing(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_expenses"].append(
            record(record_id="vendor:1", source_system="vendor", event_type="purchase", gross_amount=10, channel="vendor")
        )
        policy = {
            "schema_version": "1.0",
            "company_slug": "example",
            "bank_accounts": {},
            "contacts": {"suppliers": {"vendor": "18"}},
            "mappings": {},
            "supplier_aliases": {},
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon(),
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                posting_policy=policy,
            )

        action = find_action(batch, "create_purchase_summary")
        line = action["payload"]["line_items"][0]
        self.assertIsNone(line["suggested_expense_account_id"])
        self.assertIsNone(line["suggested_vat_type_id"])
        self.assertTrue(any(dep["kind"] == "posting_mapping" for dep in batch["unresolved_dependencies"]))

    def test_builder_does_not_create_unallocated_cash_actions_under_policy(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="woo:sale:1",
                source_system="woocommerce",
                event_type="merchant_sales_summary",
                gross_amount=10.0,
                channel="woo",
            )
        )
        payout = record(
            record_id="stripe:payout:1",
            source_system="stripe",
            event_type="stripe_payout",
            gross_amount=10.0,
            channel="stripe",
        )
        normalized["records"]["payouts"].append(payout)
        bank = record(
            record_id="bank:1",
            source_system="bank",
            event_type="bank_credit",
            gross_amount=10.0,
            channel="stripe",
            attributes={"customer_account": "EE001234567890"},
        )
        normalized["records"]["bank_transactions"].append(bank)
        policy = {
            "schema_version": "1.0",
            "company_slug": "example",
            "bank_accounts": {"EE001234567890": "3"},
            "contacts": {"sales": {"woo": "42"}, "processors": {"stripe": "29"}, "suppliers": {}},
            "mappings": {
                "woo-non-taxable": {
                    "income_account_id": "109",
                    "shipping_income_account_id": "255",
                    "vat_type_id": "12",
                    "shipping_vat_type_id": "13",
                    "warehouse_id": "6",
                }
            },
            "supplier_aliases": {},
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon(),
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                posting_policy=policy,
            )

        sales = find_action(batch, "create_invoice_summary")
        self.assertEqual(sales["payload"]["counterparty"]["contact_id"], "42")
        self.assertEqual(sales["payload"]["line_items"][0]["suggested_income_account_id"], "109")
        self.assertEqual(sales["payload"]["line_items"][0]["suggested_vat_type_id"], "12")
        self.assertEqual(sales["payload"]["line_items"][0]["warehouse_id_hint"], "6")
        self.assertNotEqual(sales["confidence"], "low")
        self.assertFalse(actions_of_type(batch, "create_incoming_summary"))

    def test_builder_rejects_bank_row_without_source_account_under_policy(self) -> None:
        normalized = base_normalized()
        normalized["records"]["bank_transactions"].extend(
            [
                record(
                    record_id="bank:identified",
                    source_system="bank",
                    event_type="bank_credit",
                    gross_amount=10.0,
                    attributes={"customer_account": "EE001234567890"},
                ),
                record(
                    record_id="bank:missing",
                    source_system="bank",
                    event_type="bank_credit",
                    gross_amount=5.0,
                ),
            ]
        )
        policy = {
            "schema_version": "1.0",
            "company_slug": "example",
            "bank_accounts": {"EE001234567890": "3"},
            "contacts": {},
            "mappings": {},
            "supplier_aliases": {},
        }

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            bookbuilder.SimplbooksError, "missing source bank account"
        ):
            bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=base_recon(),
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                posting_policy=policy,
            )

    def test_builder_blocks_when_recon_not_approved(self) -> None:
        normalized = base_normalized()
        recon = base_recon(approve=False)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bookbuilder.SimplbooksError):
                bookbuilder.build_action_batch(
                    normalized_payload=normalized,
                    recon_payload=recon,
                    normalized_path=Path(tmp) / "normalized.json",
                    recon_path=Path(tmp) / "recon.json",
                    repo_root=Path(tmp),
                )

    def test_builder_generates_business_documents_without_unallocated_cash_actions(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="paypal:sale:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_website_payment",
                gross_amount=120.0,
                net_amount=116.0,
                vat_amount=20.0,
                fee_amount=4.0,
                shipping_amount=10.0,
                description="PayPal Website Payment",
            )
        )
        normalized["records"]["payouts"].append(
            record(
                record_id="paypal:payout:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_withdrawal",
                gross_amount=116.0,
                description="PayPal transfer to bank",
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:paypal:1",
                source_system="bank",
                event_type="bank_credit",
                gross_amount=116.0,
                description="PayPal transfer to bank",
            )
        )
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="printful:1",
                source_system="printful",
                event_type="fulfillment_cost",
                gross_amount=30.0,
                description="Printful monthly charge",
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:printful:1",
                source_system="bank",
                event_type="bank_debit",
                gross_amount=-30.0,
                description="Printful monthly charge",
            )
        )

        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "3000", "name": "Product Sales", "code": "3000", "status": None},
                {"id": "3010", "name": "Shipping Revenue", "code": "3010", "status": None},
                {"id": "6010", "name": "Payment Processor Fees", "code": "6010", "status": None},
                {"id": "6020", "name": "Fulfillment Costs", "code": "6020", "status": None},
            ],
            "income_accounts": [{"id": "1010", "name": "Main Bank", "code": "1010", "status": None}],
            "vat_types": [
                {"id": "22", "name": "22% VAT", "code": "22", "status": None},
                {"id": "0", "name": "0% Export", "code": "0", "status": None},
            ],
            "contacts": [
                {"id": "2001", "name": "PayPal", "status": None},
                {"id": "2002", "name": "Printful", "status": None},
            ],
        }
        company_profile = {"bank_account_ids": ["101"]}

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                policy_text="Shipping revenue may be kept separate from product revenue.",
                entity_map=entity_map,
                company_profile=company_profile,
            )

        self.assertEqual(batch["approval_status"], "draft")
        self.assertEqual(len(batch["actions"]), 3)

        sales_action = find_action(batch, "create_invoice_summary", endpoint="invoices/create")
        line_roles = [line["line_role"] for line in sales_action["payload"]["line_items"]]
        self.assertIn("sales_shipping", line_roles)
        self.assertEqual(sales_action["payload"]["counterparty"]["contact_id"], "2001")
        shipping_line = next(line for line in sales_action["payload"]["line_items"] if line["line_role"] == "sales_shipping")
        self.assertEqual(shipping_line["suggested_vat_type_id"], "22")

        fee_action = find_action(batch, "create_purchase_summary", endpoint="purchases/create")
        self.assertEqual(fee_action["payload"]["line_items"][0]["line_role"], "processor_fee")
        self.assertEqual(fee_action["payload"]["totals"]["gross_amount"], 4.0)

        self.assertFalse(actions_of_type(batch, "create_incoming_summary"))
        self.assertFalse(actions_of_type(batch, "create_payment_summary"))

    def test_builder_uses_channel_sales_once_when_processor_totals_match(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="woo:sale:1",
                source_system="woo",
                channel="woo",
                event_type="woo_daily_sales",
                gross_amount=110.0,
                net_amount=100.0,
                vat_amount=20.0,
                shipping_amount=10.0,
                description="Woo daily sales summary",
            )
        )
        normalized["records"]["sales"].append(
            record(
                record_id="paypal:sale:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_website_payment",
                gross_amount=110.0,
                net_amount=106.0,
                vat_amount=20.0,
                fee_amount=4.0,
                shipping_amount=10.0,
                description="PayPal Website Payment",
            )
        )
        normalized["records"]["payouts"].append(
            record(
                record_id="paypal:payout:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_withdrawal",
                gross_amount=106.0,
                description="PayPal transfer to bank",
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:paypal:1",
                source_system="bank",
                event_type="bank_credit",
                gross_amount=106.0,
                description="PayPal transfer to bank",
            )
        )

        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "3000", "name": "Product Sales", "code": "3000", "status": None},
                {"id": "3010", "name": "Shipping Revenue", "code": "3010", "status": None},
                {"id": "6010", "name": "Payment Processor Fees", "code": "6010", "status": None},
            ],
            "income_accounts": [{"id": "1010", "name": "Main Bank", "code": "1010", "status": None}],
            "vat_types": [
                {"id": "22", "name": "22% VAT", "code": "22", "status": None},
                {"id": "0", "name": "0% Export", "code": "0", "status": None},
            ],
            "contacts": [
                {"id": "2001", "name": "PayPal", "status": None},
            ],
        }
        company_profile = {"bank_account_ids": ["101"]}

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
                company_profile=company_profile,
            )

        invoice_actions = [action for action in batch["actions"] if action["action_type"] == "create_invoice_summary"]
        self.assertEqual(len(invoice_actions), 1)
        self.assertEqual(invoice_actions[0]["idempotency_key"], "example-2024-01-sales-woo")
        self.assertEqual(invoice_actions[0]["payload"]["counterparty"]["contact_id"], "2001")
        self.assertTrue(any("settlement evidence" in note for note in invoice_actions[0]["review_notes"]))
        self.assertFalse(any(line["line_role"] == "sales_shipping" for line in invoice_actions[0]["payload"]["line_items"]))

        self.assertFalse(actions_of_type(batch, "create_incoming_summary"))

    def test_force_allows_blocked_recon_and_marks_review_notes(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="paypal:sale:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_website_payment",
                gross_amount=20.0,
                net_amount=19.0,
                vat_amount=3.0,
                fee_amount=1.0,
                description="PayPal Website Payment",
            )
        )
        recon = base_recon(approve=False)

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                force=True,
            )

        sales_action = find_action(batch, "create_invoice_summary")
        self.assertTrue(any("forced" in note.lower() for note in sales_action["review_notes"]))

    def test_builder_applies_historical_online_sales_mappings_when_available(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].extend(
            [
                record(
                    record_id="woo:taxable:1",
                    source_system="woo",
                    channel="woo",
                    event_type="woo_daily_sales",
                    gross_amount=120.0,
                    net_amount=100.0,
                    vat_amount=20.0,
                    shipping_amount=20.0,
                    description="Woo taxable day",
                ),
                record(
                    record_id="woo:export:1",
                    source_system="woo",
                    channel="woo",
                    event_type="woo_daily_sales",
                    gross_amount=80.0,
                    net_amount=80.0,
                    vat_amount=0.0,
                    shipping_amount=10.0,
                    description="Woo export day",
                ),
                record(
                    record_id="stripe:sale:1",
                    source_system="stripe",
                    channel="stripe",
                    event_type="stripe_charge",
                    gross_amount=120.0,
                    net_amount=117.0,
                    vat_amount=0.0,
                    description="Stripe charge",
                ),
                record(
                    record_id="paypal:sale:1",
                    source_system="paypal",
                    channel="paypal",
                    event_type="paypal_website_payment",
                    gross_amount=80.0,
                    net_amount=77.0,
                    vat_amount=0.0,
                    description="PayPal payment",
                ),
            ]
        )
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "107", "name": "Kauba müük EL (Eesti km.)", "code": "4530", "status": None},
                {"id": "109", "name": "Kauba eksport (km.0%)", "code": "4610", "status": None},
                {"id": "253", "name": "Kauba saatekulu müügil - EL ühendusesisene (KM 20%)", "code": "4992", "status": None},
                {"id": "255", "name": "Kauba saatekulu müügil - EL välised riigid (KM 0%)", "code": "4994", "status": None},
            ],
            "vat_types": [
                {"id": "21", "name": "20% Kauba müük, EU ühendusesisene", "extra": {"is_sales": True, "vat_percent": 20}},
                {"id": "22", "name": "20% Teenuste müük, EU ühendusesisene", "extra": {"is_sales": True, "vat_percent": 20}},
                {"id": "12", "name": "0% Kauba eksport", "extra": {"is_sales": True, "vat_percent": 0}},
                {"id": "13", "name": "0% Teenuste eksport", "extra": {"is_sales": True, "vat_percent": 0}},
            ],
            "warehouses": [
                {"id": "6", "name": "Printful EU", "status": None},
            ],
            "contacts": [
                {"id": "29", "name": "Stripe Technology Europe, Limited", "status": None},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
            )

        invoice_actions = [action for action in batch["actions"] if action["action_type"] == "create_invoice_summary"]
        self.assertEqual(len(invoice_actions), 2)
        taxable = next(action for action in invoice_actions if action["idempotency_key"].endswith("taxable"))
        export = next(action for action in invoice_actions if action["idempotency_key"].endswith("non-taxable"))
        self.assertEqual(taxable["payload"]["counterparty"]["contact_id"], "29")
        self.assertEqual(export["payload"]["counterparty"]["contact_id"], "29")
        self.assertEqual(
            [(line["suggested_income_account_id"], line["suggested_vat_type_id"], line["warehouse_id_hint"]) for line in taxable["payload"]["line_items"]],
            [("107", "21", "6"), ("253", "22", "6")],
        )
        self.assertEqual(
            [(line["suggested_income_account_id"], line["suggested_vat_type_id"], line["warehouse_id_hint"]) for line in export["payload"]["line_items"]],
            [("109", "12", "6"), ("255", "13", "6")],
        )

    def test_builder_uses_historical_purchase_mappings_for_processor_and_printful_costs(self) -> None:
        normalized = base_normalized()
        normalized["records"]["fees"].append(
            record(
                record_id="stripe:fee:1",
                source_system="stripe",
                channel="stripe",
                event_type="stripe_processing_fee_invoice",
                gross_amount=3.5,
                description="Stripe fee invoice",
            )
        )
        normalized["records"]["purchase_expenses"].extend(
            [
                record(
                    record_id="printful:order:1",
                    source_system="printful",
                    channel="printful",
                    event_type="printful_order_charge",
                    gross_amount=12.0,
                    description="Printful order charges for Order 734",
                ),
                record(
                    record_id="printful:storage:1",
                    source_system="printful",
                    channel="printful",
                    event_type="printful_storage_invoice",
                    gross_amount=5.0,
                    description="Printful storage invoice #123",
                ),
            ]
        )
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "257", "name": "EL-st soetatud transpordi- ja saatekulud müügil", "code": "5521", "status": None},
                {"id": "258", "name": "EL-st soetatud teenused", "code": "5201", "status": None},
            ],
            "vat_types": [
                {"id": "11", "name": "0% Teenuste ühendusesisene soetamine", "extra": {"is_purchase": True, "vat_percent": 0}},
                {"id": "19", "name": "Mitte KM-kohustuslane", "extra": {"is_purchase": True}},
            ],
            "contacts": [
                {"id": "29", "name": "Stripe Technology Europe, Limited", "status": None},
                {"id": "41", "name": "Printful Inc.", "status": None},
            ],
            "income_accounts": [{"id": "101", "name": "Main Bank", "code": "101"}],
        }
        company_profile = {"bank_account_ids": ["101"]}

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
                company_profile=company_profile,
            )

        fee_action = next(action for action in batch["actions"] if action["idempotency_key"].endswith("fees-stripe"))
        self.assertEqual(fee_action["payload"]["counterparty"]["contact_id"], "29")
        self.assertEqual(fee_action["payload"]["line_items"][0]["suggested_expense_account_id"], "258")
        self.assertEqual(fee_action["payload"]["line_items"][0]["suggested_vat_type_id"], "11")

        purchase_action = next(action for action in batch["actions"] if action["idempotency_key"].endswith("purchase-printful"))
        self.assertEqual(purchase_action["payload"]["counterparty"]["contact_id"], "41")
        self.assertEqual(
            [(line["description"], line["suggested_expense_account_id"], line["suggested_vat_type_id"]) for line in purchase_action["payload"]["line_items"]],
            [
                ("Orders shipping and fullfilment", "257", "11"),
                ("Storage fee for warehoused products", "258", "11"),
            ],
        )

    def test_builder_nets_printful_purchase_corrections_instead_of_summing_absolute_values(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_expenses"].extend(
            [
                record(
                    record_id="printful:order:1",
                    source_system="printful",
                    channel="printful",
                    event_type="printful_order_charge",
                    gross_amount=7.9,
                    description="Printful order charge",
                ),
                record(
                    record_id="printful:storage:1",
                    source_system="printful",
                    channel="printful",
                    event_type="printful_other_charge",
                    gross_amount=181.5,
                    vat_amount=31.5,
                    description="Printful Custom Product Keeping",
                ),
                record(
                    record_id="printful:storage:2",
                    source_system="printful",
                    channel="printful",
                    event_type="printful_other_charge",
                    gross_amount=-181.5,
                    vat_amount=-31.5,
                    description="Printful Custom Product Keeping correction",
                ),
            ]
        )
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "257", "name": "EL-st soetatud transpordi- ja saatekulud müügil", "code": "5521", "status": None},
                {"id": "258", "name": "EL-st soetatud teenused", "code": "5201", "status": None},
            ],
            "vat_types": [
                {"id": "11", "name": "0% Teenuste ühendusesisene soetamine", "extra": {"is_purchase": True, "vat_percent": 0}},
            ],
            "contacts": [
                {"id": "41", "name": "Printful Inc.", "status": None},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
            )

        purchase_action = next(action for action in batch["actions"] if action["idempotency_key"].endswith("purchase-printful"))
        self.assertEqual(purchase_action["payload"]["totals"]["gross_amount"], 7.9)
        self.assertEqual(purchase_action["payload"]["totals"]["vat_amount"], 0.0)
        self.assertEqual(
            [(line["description"], line["gross_amount"]) for line in purchase_action["payload"]["line_items"]],
            [("Orders shipping and fullfilment", 7.9)],
        )

    def test_builder_does_not_infer_supplier_payment_from_bank_counterparty(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="purchase:example-supplier:1",
                source_system="document",
                source_type="pdf",
                event_type="purchase_invoice_pdf",
                gross_amount=123.45,
                description="Acme Supplier OU invoice #INV-001",
                channel="acme-supplier-ou",
                external_ref="INV-001",
                attributes={"invoice_number": "INV-001", "vendor_name": "Acme Supplier OU"},
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:example-supplier:1",
                source_system="bank",
                event_type="bank_debit",
                gross_amount=-123.45,
                description="Invoice #INV-001",
                attributes={"counterparty_name": "ACME SUPPLIER OU"},
            )
        )
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "126", "name": "Mitmesugused tegevuskulud", "code": "5200", "status": None},
            ],
            "vat_types": [
                {"id": "19", "name": "Mitte KM-kohustuslane", "extra": {"is_purchase": True}},
            ],
            "contacts": [
                {"id": "59", "name": "Acme Supplier OU", "status": None},
            ],
            "income_accounts": [{"id": "101", "name": "Main Bank", "code": "101"}],
        }
        company_profile = {"bank_account_ids": ["101"]}

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
                company_profile=company_profile,
            )

        self.assertFalse(actions_of_type(batch, "create_payment_summary"))

    def test_builder_treats_quartermaster_as_fulfillment_partner_without_inferred_payment(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="quartermaster:invoice:1",
                source_system="quartermaster",
                source_type="pdf",
                event_type="quartermaster_service_invoice",
                gross_amount=21.0,
                description="Quartermaster monthly storage invoice",
                channel="quartermaster",
                external_ref="00635-00002",
                attributes={"invoice_number": "00635-00002", "vendor_name": "Quartermaster Logistics LLC"},
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:quartermaster:1",
                source_system="bank",
                event_type="bank_debit",
                gross_amount=-21.0,
                description="Quartermaster Logistics LLC invoice 00635-00002",
                attributes={"counterparty_name": "Quartermaster Logistics LLC"},
            )
        )
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "612", "name": "Fulfillment and logistics", "code": "6120", "status": None},
            ],
            "vat_types": [
                {"id": "11", "name": "0% Teenuste ühendusesisene soetamine", "extra": {"is_purchase": True, "vat_percent": 0}},
            ],
            "contacts": [
                {"id": "77", "name": "Quartermaster Logistics LLC", "status": None},
            ],
            "income_accounts": [{"id": "101", "name": "Main Bank", "code": "101"}],
        }
        company_profile = {"bank_account_ids": ["101"]}

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
                company_profile=company_profile,
            )

        purchase_action = next(action for action in batch["actions"] if action["idempotency_key"].endswith("purchase-quartermaster"))
        self.assertEqual(purchase_action["payload"]["counterparty"]["contact_id"], "77")
        self.assertEqual(purchase_action["payload"]["line_items"][0]["description"], "quartermaster fulfillment cost summary")
        self.assertEqual(purchase_action["payload"]["line_items"][0]["suggested_expense_account_id"], "612")
        self.assertEqual(purchase_action["payload"]["line_items"][0]["suggested_vat_type_id"], "11")

        self.assertFalse(actions_of_type(batch, "create_payment_summary"))

    def test_builder_adds_currency_suffix_when_same_purchase_group_repeats(self) -> None:
        normalized = base_normalized()
        eur_record = record(
            record_id="printful:eur:1",
            source_system="printful",
            event_type="printful_order_charge",
            gross_amount=18.0,
            description="Printful EUR order charge",
            channel="printful",
        )
        usd_record = record(
            record_id="printful:usd:1",
            source_system="printful",
            event_type="printful_service_charge",
            gross_amount=30.0,
            description="Printful USD storage charge",
            channel="printful",
        )
        usd_record["currency"] = "USD"
        normalized["records"]["purchase_expenses"].extend([eur_record, usd_record])
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "257", "name": "Imported transport", "code": "5521", "status": None},
                {"id": "258", "name": "Imported services", "code": "5201", "status": None},
            ],
            "vat_types": [
                {"id": "11", "name": "0% Imported services", "extra": {"is_purchase": True, "vat_percent": 0}},
            ],
            "contacts": [{"id": "41", "name": "Printful, Inc.", "status": None}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
            )

        purchase_keys = sorted(
            action["idempotency_key"]
            for action in batch["actions"]
            if action["action_type"] == "create_purchase_summary"
        )
        self.assertEqual(
            purchase_keys,
            [
                "example-2024-01-purchase-printful-eur",
                "example-2024-01-purchase-printful-usd",
            ],
        )

    def test_builder_uses_omniva_alias_for_eesti_post_contact(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="purchase:omniva:1",
                source_system="document",
                source_type="manual",
                event_type="purchase_note",
                gross_amount=82.1,
                description="Omniva paid by employee",
                channel="omniva",
                attributes={"vendor_name": "Omniva"},
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:omniva:1",
                source_system="bank",
                event_type="bank_debit",
                gross_amount=-82.1,
                description="Omniva 09.10.2024",
            )
        )
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "126", "name": "General expenses", "code": "5200", "status": None},
            ],
            "vat_types": [
                {"id": "19", "name": "No VAT", "extra": {"is_purchase": True, "vat_percent": 0}},
            ],
            "contacts": [
                {"id": "17", "name": "Aktsiaselts Eesti Post", "status": None},
            ],
            "income_accounts": [{"id": "101", "name": "Main Bank", "code": "101"}],
        }
        company_profile = {"bank_account_ids": ["101"]}

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
                company_profile=company_profile,
            )

        purchase_action = next(action for action in batch["actions"] if action["idempotency_key"].endswith("purchase-omniva"))
        self.assertEqual(purchase_action["payload"]["counterparty"]["contact_id"], "17")
        self.assertFalse(actions_of_type(batch, "create_payment_summary"))
        self.assertTrue(any("Eesti Post" in note for note in purchase_action["review_notes"]))

    def test_builder_posts_processor_refunds_using_merchant_sales_mapping(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="woo:sale:1",
                source_system="woocommerce",
                source_type="csv",
                event_type="merchant_sales_summary",
                gross_amount=50.0,
                description="Woo monthly sales",
                channel="woo",
            )
        )
        normalized["records"]["sales"].append(
            record(
                record_id="paypal:sale:1",
                source_system="paypal",
                event_type="paypal_website_payment",
                gross_amount=50.0,
                description="PayPal captured sale",
                channel="paypal",
            )
        )
        normalized["records"]["refunds"].append(
            record(
                record_id="paypal:refund:1",
                source_system="paypal",
                event_type="paypal_refund",
                gross_amount=10.0,
                description="PayPal refund",
                channel="paypal",
            )
        )
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "109", "name": "Export sales", "code": "4610", "status": None},
                {"id": "255", "name": "Export shipping", "code": "4994", "status": None},
            ],
            "vat_types": [
                {"id": "12", "name": "0% Kauba eksport", "extra": {"is_sales": True, "vat_percent": 0}},
                {"id": "13", "name": "0% Teenuste eksport", "extra": {"is_sales": True, "vat_percent": 0}},
            ],
            "contacts": [
                {"id": "29", "name": "Stripe Technology Europe, Limited", "status": None},
            ],
            "warehouses": [],
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
            )

        refund_action = next(action for action in batch["actions"] if action["action_type"] == "create_credit_invoice_summary")
        self.assertEqual(refund_action["idempotency_key"], "example-2024-01-refund-woo")
        self.assertEqual(refund_action["payload"]["counterparty"]["contact_id"], "29")
        self.assertEqual(refund_action["payload"]["line_items"][0]["suggested_income_account_id"], "109")
        self.assertTrue(any("posted using woo sales mapping" in note for note in refund_action["review_notes"]))

    def test_builder_uses_quartermaster_sales_mapping_and_vendor_contact(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="quartermaster:sale:1",
                source_system="quartermaster",
                source_type="pdf",
                event_type="quartermaster_sales_report",
                gross_amount=792.12,
                description="Quartermaster sales report for October 2024",
                channel="quartermaster",
                attributes={"vendor_name": "Quartermaster Direct"},
            )
        )
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "109", "name": "Export sales", "code": "4610", "status": None},
                {"id": "255", "name": "Export shipping", "code": "4994", "status": None},
            ],
            "vat_types": [
                {"id": "12", "name": "0% Kauba eksport", "extra": {"is_sales": True, "vat_percent": 0}},
                {"id": "13", "name": "0% Teenuste eksport", "extra": {"is_sales": True, "vat_percent": 0}},
            ],
            "contacts": [
                {"id": "77", "name": "Quartermaster Direct", "status": None},
            ],
            "warehouses": [{"id": "6", "name": "Printful EU", "status": None}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
            )

        sales_action = next(action for action in batch["actions"] if action["idempotency_key"].endswith("sales-quartermaster"))
        line = sales_action["payload"]["line_items"][0]
        self.assertEqual(sales_action["payload"]["counterparty"]["contact_id"], "77")
        self.assertEqual(line["suggested_income_account_id"], "109")
        self.assertEqual(line["suggested_vat_type_id"], "12")
        self.assertIsNone(line["warehouse_id_hint"])

    def test_builder_does_not_link_reimbursement_debit_to_supplier_purchase_without_supplier_match(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="purchase:example-travel:1",
                source_system="document",
                source_type="pdf",
                event_type="purchase_invoice_pdf",
                gross_amount=26.0,
                description="Transit Vendor AS invoice #1",
                channel="transit-vendor-as",
                external_ref="1",
                attributes={"invoice_number": "1", "vendor_name": "Transit Vendor AS"},
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:reimbursement:1",
                source_system="bank",
                event_type="bank_debit",
                gross_amount=-26.0,
                description="Travel reimbursement",
                attributes={"counterparty_name": "Employee Reimbursement"},
            )
        )
        recon = base_recon()
        entity_map = {
            "financial_accounts": [
                {"id": "126", "name": "Mitmesugused tegevuskulud", "code": "5200", "status": None},
            ],
            "vat_types": [
                {"id": "3", "name": "20% Eesti", "extra": {"is_purchase": True, "vat_percent": 20}},
            ],
            "contacts": [
                {"id": "51", "name": "Transit Vendor AS", "status": None},
            ],
            "income_accounts": [{"id": "101", "name": "Main Bank", "code": "101"}],
        }
        company_profile = {"bank_account_ids": ["101"]}

        with tempfile.TemporaryDirectory() as tmp:
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
                company_profile=company_profile,
            )

        self.assertFalse(any(action["action_type"] == "create_payment_summary" for action in batch["actions"]))

    def test_builder_does_not_infer_cross_month_supplier_payments(self) -> None:
        normalized = base_normalized("2024-03")
        normalized["records"]["bank_transactions"].extend(
            [
                record(
                    record_id="bank:example-vendor:settlement",
                    source_system="bank",
                    event_type="bank_debit",
                    gross_amount=-17.0,
                    description="Invoice INV-101, INV-102",
                    attributes={"counterparty_name": "Example Vendor OU"},
                ),
                record(
                    record_id="bank:example-vendor:advance",
                    source_system="bank",
                    event_type="bank_debit",
                    gross_amount=-250.0,
                    description="Advance payment",
                    attributes={"counterparty_name": "Example Vendor OU"},
                ),
            ]
        )
        recon = base_recon("2024-03")
        entity_map = {
            "contacts": [
                {"id": "18", "name": "Example Vendor OU", "status": None},
            ],
            "income_accounts": [{"id": "101", "name": "Main Bank", "code": "101"}],
        }
        company_profile = {"bank_account_ids": ["101"]}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            artifacts_dir = tmp / "companies" / "example" / "artifacts"
            actions_dir = artifacts_dir / "actions"
            actions_dir.mkdir(parents=True)
            bookbuilder.write_yaml(
                actions_dir / "2024-01.yaml",
                {
                    "actions": [
                        purchase_summary_action(
                            period="2024-01",
                            key="example-2024-01-purchase-example-vendor",
                            vendor_hint="example-vendor",
                            amount=12.0,
                            contact_id="18",
                        )
                    ]
                },
            )
            bookbuilder.write_yaml(
                actions_dir / "2024-02.yaml",
                {
                    "actions": [
                        purchase_summary_action(
                            period="2024-02",
                            key="example-2024-02-purchase-example-vendor",
                            vendor_hint="example-vendor",
                            amount=5.0,
                            contact_id="18",
                        )
                    ]
                },
            )

            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized,
                recon_payload=recon,
                normalized_path=artifacts_dir / "normalized" / "2024-03.json",
                recon_path=artifacts_dir / "recon" / "2024-03.json",
                repo_root=tmp,
                entity_map=entity_map,
                company_profile=company_profile,
            )

        payment_actions = [action for action in batch["actions"] if action["action_type"] == "create_payment_summary"]
        self.assertEqual(payment_actions, [])

    def test_yaml_writer_emits_parseable_document(self) -> None:
        payload = {
            "schema_version": "1.0",
            "company_slug": "example",
            "period": "2024-01",
            "generated_at": "2026-04-04T00:00:00Z",
            "batch_id": "example-2024-01-draft",
            "approval_status": "draft",
            "source_summary": "summary",
            "recon_ref": "companies/example/artifacts/recon/2024-01.json",
            "actions": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actions.yaml"
            bookbuilder.write_yaml(path, payload)
            text = path.read_text(encoding="utf-8")

        self.assertIn("schema_version", text)
        self.assertIn("approval_status", text)


if __name__ == "__main__":
    unittest.main()
