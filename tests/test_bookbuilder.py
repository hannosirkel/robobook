from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookbuilder  # noqa: E402


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

    def test_builder_generates_sales_fee_purchase_and_cash_actions(self) -> None:
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
        self.assertEqual(len(batch["actions"]), 5)

        sales_action = find_action(batch, "create_invoice_summary", endpoint="invoices/create")
        line_roles = [line["line_role"] for line in sales_action["payload"]["line_items"]]
        self.assertIn("sales_shipping", line_roles)
        self.assertEqual(sales_action["payload"]["counterparty"]["contact_id"], "2001")
        shipping_line = next(line for line in sales_action["payload"]["line_items"] if line["line_role"] == "sales_shipping")
        self.assertEqual(shipping_line["suggested_vat_type_id"], "22")

        fee_action = find_action(batch, "create_purchase_summary", endpoint="purchases/create")
        self.assertEqual(fee_action["payload"]["line_items"][0]["line_role"], "processor_fee")
        self.assertEqual(fee_action["payload"]["totals"]["gross_amount"], 4.0)

        incoming_action = find_action(batch, "create_incoming_summary", endpoint="incomings/create")
        self.assertEqual(incoming_action["payload"]["bank_account_id"], "101")
        self.assertTrue(any(dep.endswith("-sales-paypal") for dep in incoming_action["depends_on"]))

        payment_action = find_action(batch, "create_payment_summary", endpoint="payments/create")
        self.assertEqual(payment_action["depends_on"][0], "example-2024-01-purchase-printful")

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

        incoming_action = find_action(batch, "create_incoming_summary", endpoint="incomings/create")
        self.assertIn("example-2024-01-sales-woo", incoming_action["depends_on"])
        self.assertEqual(incoming_action["payload"]["amount"], 106.0)

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

    def test_builder_creates_supplier_payment_when_bank_counterparty_matches_purchase_group(self) -> None:
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

        payment_action = next(
            action for action in batch["actions"] if action["idempotency_key"].endswith("payment-acme-supplier-ou")
        )
        self.assertEqual(payment_action["payload"]["counterparty"]["contact_id"], "59")
        self.assertEqual(payment_action["payload"]["amount"], 123.45)
        self.assertEqual(payment_action["depends_on"], ["example-2024-01-purchase-acme-supplier-ou"])
        self.assertTrue(any("supplier text" in note for note in payment_action["review_notes"]))

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

    def test_builder_splits_cross_month_supplier_payment_across_prior_purchases(self) -> None:
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
        self.assertEqual(len(payment_actions), 2)
        self.assertEqual(sorted(action["payload"]["amount"] for action in payment_actions), [5.0, 12.0])
        self.assertEqual(
            sorted(action["payload"]["linked_purchase_action"] for action in payment_actions),
            [
                "example-2024-01-purchase-example-vendor",
                "example-2024-02-purchase-example-vendor",
            ],
        )
        self.assertTrue(all(action["depends_on"] == [] for action in payment_actions))
        self.assertTrue(all(any("prior-period" in note for note in action["review_notes"]) for action in payment_actions))
        self.assertTrue(
            all(
                all(ref["record_ref"] == "bank:example-vendor:settlement" for ref in action["source_refs"])
                for action in payment_actions
            )
        )

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
