from __future__ import annotations  # noqa: I001

import copy
import json
import tempfile
import hashlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookbuilder  # noqa: E402, I001
import bookchecker  # noqa: E402
import posting_policy  # noqa: E402
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
            "article_id": "3",
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
                "article_id": "3",
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


STATEMENT_IMPORT_CASH_POSTING = {
    "mode": "statement_import",
    "bank_income_account_ids": ["3"],
    "processor_income_account_ids": {"paypal": "6", "stripe": "7"},
    "bank_financial_accounts": {"EE123": {"EUR": "10"}},
    "clearing_provider_roles": {"paypal": "paypal", "stripe": "stripe_clearing"},
    "financial_accounts": {
        "bank": "10",
        "stripe_clearing": "30",
        "paypal": "31",
        "bank_fees": "32",
        "reporting_person_payable": "33",
        "platform_prepayment": "34",
        "customer_receivable": "37",
        "supplier_payable": "38",
        "fx_gain": "35",
        "fx_loss": "36",
    },
}


def statement_import_policy() -> dict:
    return dict(direct_sale_policy(), cash_posting=STATEMENT_IMPORT_CASH_POSTING)


def api_cash_policy() -> dict:
    return dict(direct_sale_policy(), cash_posting={"mode": "api"})


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


ZERO_RATE_VAT_TYPES = {
    "vat_types": [
        {"id": "11", "name": "0% Teenuste uhendusesisene soetamine",
         "extra": {"is_purchase": True, "vat_percent": 0, "reverse_vat_percent": 0}},
        {"id": "18", "name": "Ei ole kaive",
         "extra": {"is_purchase": True, "vat_percent": None, "reverse_vat_percent": 0}},
        {"id": "26", "name": "22% Eesti",
         "extra": {"is_purchase": True, "vat_percent": 22, "reverse_vat_percent": 0}},
    ]
}


def storage_fee_action(*, vat_amount: float = 31.5, gross: float = 181.5) -> dict:
    action = purchase_summary_action(
        period="2024-01", key="example-2024-01-purchase-printful",
        vendor_hint="printful", amount=gross,
    )
    payload = action["payload"]
    payload["totals"] = {"gross_amount": gross, "vat_amount": vat_amount}
    payload["line_items"][0]["description"] = "Storage fee for warehoused products"
    payload["line_items"][0]["vat_amount_hint"] = vat_amount
    return action


def printful_policy(*, vat_type_id: str, vat_deductible: bool | None = None) -> dict:
    line: dict = {"expense_account_id": "258", "vat_type_id": vat_type_id}
    if vat_deductible is not None:
        line["vat_deductible"] = vat_deductible
    return {
        "contacts": {"sales": {}, "processors": {}, "suppliers": {"printful": "41"}},
        "mappings": {"purchase-printful": {
            "expense_account_id": "258", "vat_type_id": vat_type_id,
            "lines": {"storage-fee-for-warehoused-products": line},
        }},
    }


class NonDeductibleVatTests(unittest.TestCase):
    """Foreign VAT a company cannot reclaim is part of the cost, not a receivable."""

    def test_a_non_deductible_line_expenses_its_vat_instead_of_reclaiming_it(self) -> None:
        action = storage_fee_action()

        bookbuilder.apply_posting_policy(
            [action],
            posting_policy=printful_policy(vat_type_id="18", vat_deductible=False),
            entity_map=ZERO_RATE_VAT_TYPES,
        )

        line = action["payload"]["line_items"][0]
        self.assertEqual(line["vat_amount_hint"], 0.0)
        self.assertEqual(line["gross_amount"], 181.5)
        self.assertEqual(action["payload"]["totals"]["vat_amount"], 0.0)
        self.assertEqual(action["payload"]["totals"]["gross_amount"], 181.5)

    def test_a_zero_rated_vat_type_may_not_carry_a_vat_amount(self) -> None:
        """The defect this guards: a line declared 0% intra-Community services while
        carrying real foreign VAT, which both misdeclares the return and reclaims tax."""
        action = storage_fee_action()

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "zero-rated VAT type"):
            bookbuilder.apply_posting_policy(
                [action],
                posting_policy=printful_policy(vat_type_id="11"),
                entity_map=ZERO_RATE_VAT_TYPES,
            )

    def test_a_rated_vat_type_still_carries_its_vat(self) -> None:
        action = storage_fee_action()

        bookbuilder.apply_posting_policy(
            [action],
            posting_policy=printful_policy(vat_type_id="26"),
            entity_map=ZERO_RATE_VAT_TYPES,
        )

        self.assertEqual(action["payload"]["line_items"][0]["vat_amount_hint"], 31.5)
        self.assertEqual(action["payload"]["totals"]["vat_amount"], 31.5)


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
    def test_direct_sales_create_one_invoice_per_receipt_with_exact_receipts(self) -> None:
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
        self.assertEqual(len(invoices), 2)
        invoice = invoices[0]
        self.assertEqual(invoice["payload"]["counterparty"]["contact_id"], "42")
        self.assertEqual(invoice["payload"]["totals"]["gross_amount"], 20.0)
        self.assertEqual(invoice["payload"]["line_items"][0]["suggested_income_account_id"], "107")
        self.assertEqual(invoice["payload"]["line_items"][0]["suggested_vat_type_id"], "25")
        self.assertEqual(invoice["payload"]["line_items"][0]["warehouse_id_hint"], "6")
        self.assertEqual(invoice["payload"]["line_items"][0]["article_id_hint"], "3")
        self.assertEqual(
            sorted(ref["record_ref"] for item in invoices for ref in item["source_refs"]),
            ["direct-a", "direct-b"],
        )
        self.assertEqual([item["payload"]["document_date"] for item in receipts], ["2024-08-27", "2024-08-30"])
        self.assertEqual([item["payload"]["amount"] for item in receipts], [20.0, 20.0])
        self.assertTrue(all(len(item["source_refs"]) == 1 for item in receipts))
        self.assertTrue(all(item["source_refs"][0]["source_kind"] == "physical_bank" for item in receipts))
        self.assertEqual(
            {tuple(item["depends_on"]) for item in receipts},
            {(item["idempotency_key"],) for item in invoices},
        )
        allocation_by_record = {allocation["record_id"]: allocation for allocation in allocations.values()}
        resolved = [{"record_ref": rows[0]["record_id"], "record": rows[0], "payload": normalized}]
        self.assertEqual(
            bookchecker.evaluate_inventory_quantities(
                action=invoice, resolved_sources=resolved,
                reviewed_allocations=allocation_by_record,
            ),
            [],
        )
        mutated = {key: dict(value) for key, value in allocation_by_record.items()}
        mutated["direct-a"] = {
            **mutated["direct-a"],
            "target": {**mutated["direct-a"]["target"], "quantity": 2},
        }
        mutation_findings = bookchecker.evaluate_inventory_quantities(
            action=invoice, resolved_sources=resolved, reviewed_allocations=mutated,
        )
        self.assertTrue(any("complete contributor set" in item["summary"] for item in mutation_findings))

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
                            "article_id": "3",
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

        # One invoice per physical receipt, even when both share a resolved posting tuple.
        self.assertEqual(len(actions_of_type(batch, "create_invoice_summary")), 2)

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

    def test_manual_dependency_propagates_reviewed_statement_import_proof(self) -> None:
        normalized = base_normalized("2024-08")
        row = bank_row(record_id="reviewed-fee", amount=-7.0, event_date="2024-08-30")
        normalized["records"]["bank_transactions"] = [row]
        allocation = manual_allocation(row=row, disposition="bank_fee_payment")
        allocation["target"]["statement_import_proof"] = {
            "status": "verified",
            "required_evidence": "live_discovery_or_audit",
            "simplbooks_transaction_id": "txn-501",
            "evidence_binding": {"path": "evidence.json", "sha256": "a" * 64},
        }
        allocations = {(allocation["statement_id"], allocation["iban"], allocation["currency"]): allocation}

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

        dependency = batch["unresolved_dependencies"][0]
        self.assertFalse(dependency["blocking"])
        self.assertEqual(
            dependency["statement_import_proof"],
            allocation["target"]["statement_import_proof"],
        )

    def test_expense_reimbursement_payment_creates_atomic_manual_dependency(self) -> None:
        normalized = base_normalized("2024-08")
        row = bank_row(record_id="employee-reimbursement", amount=-50.30, event_date="2024-08-01")
        normalized["records"]["bank_transactions"] = [row]
        allocation = manual_allocation(
            row=row,
            disposition="expense_reimbursement_payment",
            target={"document_type": "financial_transaction", "transaction_family": "expense_reimbursement"},
        )
        allocations = {(allocation["statement_id"], allocation["iban"], allocation["currency"]): allocation}

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
        self.assertEqual(len(batch["unresolved_dependencies"]), 1)
        self.assertEqual(batch["unresolved_dependencies"][0]["disposition"], "expense_reimbursement_payment")

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

    def test_foreign_currency_purchase_payment_adds_blocking_first_live_pilot(self) -> None:
        row = bank_row(record_id="wise-usd-purchase", amount=-30.2, event_date="2024-11-01")
        allocation = manual_allocation(row=row, disposition="generated_purchase_payment")
        allocation["target"] = {
            "document_type": "purchase", "action_key": "prior-usd-purchase",
            "target_currency": "USD", "foreign_currency_pilot_required": True,
            "pilot_requirements": ["applied_ecb_rate", "linked_purchase_balance", "realized_fx_and_fee_treatment"],
        }

        dependencies = bookbuilder.build_foreign_currency_payment_pilot_dependencies(
            records={"bank_transactions": [row]},
            allocations={(allocation["statement_id"], allocation["iban"], allocation["currency"]): allocation},
        )

        self.assertEqual(len(dependencies), 1)
        self.assertTrue(dependencies[0]["blocking"])
        self.assertEqual(dependencies[0]["target_currency"], "USD")

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

    def test_existing_invoice_receipt_uses_sales_contact_policy(self) -> None:
        allocation = existing_invoice_allocation(record_id="receipt", invoice_id="119")
        allocation["target"].update({"contact_id": "31", "counterparty_hint": "brain-games"})
        policy = {
            "bank_accounts": {"EE123": {"EUR": "3"}},
            "contacts": {
                "sales": {"brain-games": "31"},
                "processors": {},
                "suppliers": {},
            },
            "mappings": {},
        }

        batch = build_with(
            bank=bank_row(record_id="receipt", amount=330.0, event_date="2024-01-08"),
            allocation=allocation,
            posting_policy=policy,
            discovery_overviews=[{"document_index": [{"simplbooks_id": "119", "document_type": "invoice"}]}],
        )

        action = find_action(batch, "create_incoming_summary")
        self.assertEqual(action["payload"]["counterparty"]["contact_id"], "31")
        self.assertFalse(any(item.get("kind") == "contact_mapping" for item in batch["unresolved_dependencies"]))

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

    def test_generated_purchase_payment_inherits_target_purchase_contact(self) -> None:
        row = bank_row(record_id="dpd-payment", amount=-43.4, event_date="2025-09-12")
        allocation = manual_allocation(row=row, disposition="generated_purchase_payment")
        allocation["target"] = {
            "document_type": "purchase",
            "action_key": "example-2025-09-purchase-dpd",
        }
        target_action = purchase_summary_action(
            period="2025-09",
            key="example-2025-09-purchase-dpd",
            vendor_hint="dpd",
            amount=43.4,
            contact_id="37",
        )

        actions = bookbuilder.build_exact_cash_actions(
            company_slug="example",
            period="2025-09",
            normalized_path_display="normalized.json",
            records={"bank_transactions": [row]},
            base_currency="EUR",
            default_bank_account_id="3",
            bank_account_notes=[],
            entity_map=None,
            posting_policy=None,
            allocations={(allocation["statement_id"], allocation["iban"], allocation["currency"]): allocation},
            current_actions=[target_action],
            historical_actions={},
            discovery_overviews=[],
            forced_note=None,
        )

        self.assertEqual(actions[0]["payload"]["counterparty"]["contact_id"], "37")
        self.assertEqual(actions[0]["payload"]["counterparty_hint"], "dpd")
        self.assertFalse(any("No contact/client mapping matched" in note for note in actions[0]["review_notes"]))
        unresolved = bookbuilder.apply_posting_policy(
            actions,
            posting_policy={
                "contacts": {"sales": {}, "processors": {}, "suppliers": {"dpd": "37"}},
                "mappings": {},
            },
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(actions[0]["payload"]["counterparty"]["contact_id"], "37")

    def test_generated_payment_rejects_contact_conflicting_with_linked_purchase(self) -> None:
        row = bank_row(record_id="dpd-conflict", amount=-43.4, event_date="2025-09-12")
        allocation = manual_allocation(row=row, disposition="generated_purchase_payment")
        allocation["target"] = {
            "document_type": "purchase", "action_key": "example-2025-09-purchase-dpd",
            "contact_id": "99", "counterparty_hint": "other-vendor",
        }
        target_action = purchase_summary_action(
            period="2025-09", key="example-2025-09-purchase-dpd",
            vendor_hint="dpd", amount=43.4, contact_id="37",
        )

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "conflicts with linked generated target"):
            bookbuilder.build_exact_cash_actions(
                company_slug="example", period="2025-09", normalized_path_display="normalized.json",
                records={"bank_transactions": [row]}, base_currency="EUR",
                default_bank_account_id="3", bank_account_notes=[], entity_map=None,
                posting_policy=None,
                allocations={(allocation["statement_id"], allocation["iban"], allocation["currency"]): allocation},
                current_actions=[target_action], historical_actions={}, discovery_overviews=[], forced_note=None,
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

    def test_inventory_sales_line_preserves_quantity_and_reviewed_article(self) -> None:
        normalized = base_normalized()
        sale = record(
            record_id="woo:sale:inventory", source_system="woo", channel="woo",
            event_type="woo_daily_sales", gross_amount=60.0, net_amount=60.0,
            description="Two Lunar Base games",
        )
        sale["quantity"] = 2
        normalized["records"]["sales"] = [sale]
        policy = policy_with_mixed_22_percent_profile()
        policy["mappings"]["woo-non-taxable"]["article_id"] = "3"

        batch = build_batch_with_policy(normalized, policy)
        invoice = find_action(batch, "create_invoice_summary")
        line = invoice["payload"]["line_items"][0]

        self.assertEqual(line["quantity"], 2.0)
        self.assertEqual(line["article_id_hint"], "3")
        self.assertEqual(line["inventory_quantity_proof"]["quantity"], 2.0)
        self.assertEqual(line["inventory_quantity_proof"]["scope"]["kind"], "normalized_sales_group")
        self.assertEqual(line["inventory_quantity_proof"]["contributor_count"], 1)
        self.assertRegex(line["inventory_quantity_proof"]["scope_sha256"], r"^[a-f0-9]{64}$")
        self.assertRegex(line["inventory_quantity_proof"]["contributor_set_sha256"], r"^[a-f0-9]{64}$")

    def test_inventory_article_is_blocked_for_mixed_known_and_missing_quantity(self) -> None:
        normalized = base_normalized()
        known = record(record_id="woo:known", source_system="woo", channel="woo",
                       event_type="woo_daily_sales", gross_amount=30.0, description="Known")
        known["quantity"] = 1
        missing = record(record_id="woo:missing", source_system="woo", channel="woo",
                         event_type="woo_daily_sales", gross_amount=30.0, description="Missing")
        normalized["records"]["sales"] = [known, missing]
        policy = policy_with_mixed_22_percent_profile()
        policy["mappings"]["woo-non-taxable"]["article_id"] = "3"

        batch = build_batch_with_policy(normalized, policy)
        line = find_action(batch, "create_invoice_summary")["payload"]["line_items"][0]

        self.assertIsNone(line["article_id_hint"])
        self.assertTrue(any(item.get("kind") == "inventory_quantity" for item in batch["unresolved_dependencies"]))

    def test_quartermaster_exact_goods_quantity_is_proven_and_shipping_is_not_inventory(self) -> None:
        normalized = base_normalized()
        sale = record(
            record_id="quartermaster:sale:exact", source_system="quartermaster",
            channel="quartermaster", event_type="quartermaster_sales_report",
            gross_amount=120.0, description="Two Lunar Base plus shipping",
        )
        sale.update({"quantity": 2, "shipping_amount": 20.0})
        normalized["records"]["sales"] = [sale]
        policy = policy_with_mixed_22_percent_profile()
        policy["contacts"]["sales"]["quartermaster"] = "77"
        policy["mappings"]["quartermaster-non-taxable"] = {
            "income_account_id": "109", "shipping_income_account_id": "255",
            "vat_type_id": "12", "shipping_vat_type_id": "13",
            "warehouse_id": "6", "article_id": "3",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = bookbuilder.build_action_batch(
                normalized_payload=normalized, recon_payload=base_recon(),
                normalized_path=root / "normalized.json", recon_path=root / "recon.json",
                repo_root=root, posting_policy=policy,
                policy_text="Shipping revenue separate.",
            )
        lines = find_action(batch, "create_invoice_summary")["payload"]["line_items"]
        goods = next(line for line in lines if line["line_role"] == "sales_revenue")
        shipping = next(line for line in lines if line["line_role"] == "sales_shipping")

        self.assertEqual(goods["article_id_hint"], "3")
        self.assertEqual(goods["quantity"], 2.0)
        self.assertEqual(goods["inventory_quantity_proof"]["contributors"][0]["record_id"], sale["record_id"])
        self.assertIsNone(shipping["article_id_hint"])
        self.assertNotIn("inventory_quantity_proof", shipping)

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

        with tempfile.TemporaryDirectory() as tmp:  # noqa: SIM117
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

    def test_a_storage_fee_and_a_fulfillment_service_get_separate_lines(self) -> None:
        """They are taxed differently -- a Latvian storage fee bears Latvian VAT while a
        fulfillment service is acquired VAT-free -- so one policy line cannot serve both."""
        normalized = base_normalized()
        storage = record(
            record_id="printful:storage:1",
            source_system="printful",
            event_type="printful_other_charge",
            gross_amount=181.5,
            description="Printful Custom Product Keeping",
            channel="printful",
        )
        storage["vat_amount"] = 31.5
        service = record(
            record_id="printful:service:1",
            source_system="printful",
            event_type="printful_service_charge",
            gross_amount=306.32,
            description="Printful Warehousing & Fulfillment Stock Removal",
            channel="printful",
        )
        normalized["records"]["purchase_expenses"].extend([storage, service])
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
                recon_payload=base_recon(),
                normalized_path=Path(tmp) / "normalized.json",
                recon_path=Path(tmp) / "recon.json",
                repo_root=Path(tmp),
                entity_map=entity_map,
            )

        purchase = next(
            action for action in batch["actions"]
            if action["action_type"] == "create_purchase_summary"
            and "printful" in action["idempotency_key"]
        )
        descriptions = sorted(line["description"] for line in purchase["payload"]["line_items"])
        self.assertEqual(
            descriptions,
            ["Storage fee for warehoused products", "Warehousing and fulfillment services"],
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


INVOICE_58_DISCOVERY = [{"document_index": [{"simplbooks_id": "58", "document_type": "invoice"}]}]


class StatementImportBuilderTests(unittest.TestCase):
    def receipt_batch(self, policy: dict | None) -> dict:
        row = bank_row(record_id="r1", amount=330.0, event_date="2024-01-15")
        overrides: dict = {"discovery_overviews": INVOICE_58_DISCOVERY}
        if policy is not None:
            overrides["posting_policy"] = policy
        return build_with(
            bank=row,
            allocation=existing_invoice_allocation(record_id="r1", invoice_id="58"),
            **overrides,
        )

    def test_batch_declares_the_cash_posting_mode_it_was_built_under(self) -> None:
        self.assertEqual(self.receipt_batch(statement_import_policy())["cash_posting_mode"], "statement_import")
        self.assertEqual(self.receipt_batch(api_cash_policy())["cash_posting_mode"], "api")

    def test_a_batch_built_without_a_policy_stays_in_api_cash_mode(self) -> None:
        self.assertEqual(self.receipt_batch(None)["cash_posting_mode"], "api")

    def test_api_cash_mode_still_settles_a_physical_bank_row(self) -> None:
        batch = self.receipt_batch(api_cash_policy())

        self.assertEqual(len(actions_of_type(batch, "create_incoming_summary")), 1)

    def test_statement_import_mode_omits_bank_cash(self) -> None:
        batch = self.receipt_batch(statement_import_policy())

        self.assertEqual(actions_of_type(batch, "create_incoming_summary"), [])

    def test_statement_import_mode_keeps_the_document_it_settles(self) -> None:
        row = bank_row(record_id="r2", amount=120.0, event_date="2024-01-16")
        batch = build_with(
            bank=row,
            allocation=direct_sale_allocation(row=row),
            posting_policy=statement_import_policy(),
        )

        self.assertEqual(len(actions_of_type(batch, "create_invoice_summary")), 1)
        self.assertEqual(actions_of_type(batch, "create_incoming_summary"), [])

    def test_statement_import_mode_generates_no_prohibited_action(self) -> None:
        policy = statement_import_policy()
        batch = self.receipt_batch(policy)

        self.assertEqual(
            [
                action
                for action in batch["actions"]
                if posting_policy.prohibited_bank_cash_action(action, policy)
            ],
            [],
        )


def woo_sale(*, record_id: str, order_id: str | int | None, gross: float = 60.0, quantity: int = 1) -> dict:
    sale = record(
        record_id=record_id,
        source_system="woo",
        event_type="woo_order",
        gross_amount=gross,
        vat_amount=round(gross - gross / 1.22, 2),
        channel="woo",
        attributes={} if order_id is None else {"order_id": str(order_id)},
    )
    sale["quantity"] = quantity
    return sale


def warehouse_routing_policy(**routing: object) -> dict:
    policy = dict(statement_import_policy())
    policy["warehouse_routing"] = {
        "woo": {"before_order": 771, "before_warehouse_id": "6", "from_warehouse_id": "1"},
        "direct_sale_warehouse_id": "1",
        "distributor_warehouse_id": None,
        **routing,
    }
    policy["contacts"] = {
        "sales": {"woo": "42", "direct-sale": "42"},
        "processors": {"paypal": "63", "stripe": "29"},
        "suppliers": {},
    }
    policy["mappings"] = {
        **policy["mappings"],
        "woo-taxable": {
            "income_account_id": "107", "shipping_income_account_id": "253",
            "vat_type_id": "25", "shipping_vat_type_id": "24", "warehouse_id": "6", "article_id": "3",
        },
    }
    return policy


class SalesWarehouseRoutingTests(unittest.TestCase):
    def test_each_side_of_the_boundary_routes_to_its_reviewed_warehouse(self) -> None:
        policy = warehouse_routing_policy()

        self.assertEqual(
            bookbuilder.routed_sales_warehouse(policy, group_label="woo", record=woo_sale(record_id="a", order_id=770)),
            "6",
        )
        self.assertEqual(
            bookbuilder.routed_sales_warehouse(policy, group_label="woo", record=woo_sale(record_id="b", order_id=771)),
            "1",
        )

    def test_a_channel_without_a_routing_rule_keeps_the_existing_mapping(self) -> None:
        policy = warehouse_routing_policy()

        self.assertIsNone(
            bookbuilder.routed_sales_warehouse(
                policy, group_label="stripe", record=woo_sale(record_id="c", order_id=900)
            )
        )

    def test_a_routed_contributor_without_an_order_number_blocks(self) -> None:
        policy = warehouse_routing_policy()

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "order number"):
            bookbuilder.routed_sales_warehouse(
                policy, group_label="woo", record=woo_sale(record_id="d", order_id=None)
            )

    def test_a_policy_without_routing_leaves_every_record_unrouted(self) -> None:
        self.assertIsNone(
            bookbuilder.routed_sales_warehouse(
                policy_with_24_percent_profile(), group_label="woo", record=woo_sale(record_id="e", order_id=1)
            )
        )

    def test_contributors_on_opposite_sides_never_share_an_inventory_line(self) -> None:
        normalized = base_normalized("2024-01")
        normalized["records"]["sales"] = [
            woo_sale(record_id="woo:770", order_id=770),
            woo_sale(record_id="woo:771", order_id=771),
        ]

        batch = build_batch_with_policy(normalized, warehouse_routing_policy())

        warehouses = sorted(
            line["warehouse_id_hint"]
            for action in actions_of_type(batch, "create_invoice_summary")
            for line in action["payload"]["line_items"]
        )
        self.assertEqual(warehouses, ["1", "6"])

    def test_each_routed_group_records_the_rule_it_applied(self) -> None:
        normalized = base_normalized("2024-01")
        normalized["records"]["sales"] = [woo_sale(record_id="woo:770", order_id=770)]

        batch = build_batch_with_policy(normalized, warehouse_routing_policy())
        scope = actions_of_type(batch, "create_invoice_summary")[0]["payload"]["summary_scope"]

        self.assertEqual(scope["warehouse_routing"]["warehouse_id"], "6")
        self.assertEqual(scope["warehouse_routing"]["order_numbers"], [770])


def build_direct_sales(rows: list[dict], *, policy: dict | None = None, **overrides: object) -> dict:
    normalized = base_normalized(rows[0]["event_date"][:7])
    normalized["records"]["bank_transactions"] = rows
    allocations = {}
    for row in rows:
        item = direct_sale_allocation(row=row)
        allocations[(item["statement_id"], item["iban"], item["currency"])] = item
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        return bookbuilder.build_action_batch(
            normalized_payload=normalized,
            recon_payload=base_recon(normalized["period"]),
            normalized_path=root / "normalized.json",
            recon_path=root / "recon.json",
            repo_root=root,
            company_profile={"bank_account_ids": ["101"]},
            posting_policy=policy or direct_sale_policy(),
            bank_allocations=allocations,
            **overrides,
        )


class DirectSaleInvoicePerReceiptTests(unittest.TestCase):
    def rows(self) -> list[dict]:
        return [
            bank_row(record_id="d1", amount=60.0, event_date="2024-01-10"),
            bank_row(record_id="d2", amount=60.0, event_date="2024-01-20"),
        ]

    def test_each_physical_receipt_gets_its_own_invoice(self) -> None:
        batch = build_direct_sales(self.rows())

        invoices = actions_of_type(batch, "create_invoice_summary")
        self.assertEqual(len(invoices), 2)

    def test_identical_direct_receipts_stay_distinct(self) -> None:
        batch = build_direct_sales(self.rows())

        keys = [action["idempotency_key"] for action in actions_of_type(batch, "create_invoice_summary")]
        self.assertEqual(len(set(keys)), 2)

    def test_each_direct_invoice_covers_exactly_one_physical_row(self) -> None:
        batch = build_direct_sales(self.rows())

        for action in actions_of_type(batch, "create_invoice_summary"):
            self.assertEqual(len(action["source_refs"]), 1)
            self.assertEqual(action["payload"]["summary_scope"]["record_count"], 1)


class DistributorTransferProofTests(unittest.TestCase):
    def policy(self) -> dict:
        policy = warehouse_routing_policy(distributor_warehouse_id="9")
        policy["mappings"]["distributor-taxable"] = {
            "income_account_id": "107", "vat_type_id": "25", "warehouse_id": "9", "article_id": "3",
        }
        policy["contacts"]["sales"]["distributor"] = "44"
        return policy

    def normalized_with_distributor_sale(self) -> dict:
        normalized = base_normalized("2024-01")
        sale = record(
            record_id="distributor:1",
            source_system="distributor",
            event_type="distributor_order",
            gross_amount=100.0,
            vat_amount=18.03,
            channel="distributor",
            attributes={"order_id": "5001"},
        )
        sale["quantity"] = 2
        normalized["records"]["sales"] = [sale]
        return normalized

    def test_distributor_sales_require_bound_transfer_evidence(self) -> None:
        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "warehouse transfer evidence"):
            build_batch_with_policy(self.normalized_with_distributor_sale(), self.policy())

    def test_an_unbound_distributor_warehouse_blocks_before_any_document(self) -> None:
        policy = self.policy()
        policy["warehouse_routing"]["distributor_warehouse_id"] = None

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "distributor"):
            build_batch_with_policy(self.normalized_with_distributor_sale(), policy)

    def test_bound_transfer_evidence_allows_the_distributor_document(self) -> None:
        evidence = [{
            "action_type": "warehouse_transfer",
            "destination_warehouse_id": "9",
            "source_warehouse_id": "1",
            "article_id": "3",
            "status": "complete",
        }]

        batch = bookbuilder.build_action_batch(
            normalized_payload=self.normalized_with_distributor_sale(),
            recon_payload=base_recon("2024-01"),
            normalized_path=Path("normalized.json"),
            recon_path=Path("recon.json"),
            repo_root=Path("."),
            posting_policy=self.policy(),
            inventory_transfer_evidence=evidence,
        )

        self.assertEqual(len(actions_of_type(batch, "create_invoice_summary")), 1)

    def test_transfer_evidence_for_another_warehouse_does_not_count(self) -> None:
        evidence = [{
            "action_type": "warehouse_transfer",
            "destination_warehouse_id": "7",
            "source_warehouse_id": "1",
            "article_id": "3",
            "status": "complete",
        }]

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "warehouse transfer evidence"):
            bookbuilder.build_action_batch(
                normalized_payload=self.normalized_with_distributor_sale(),
                recon_payload=base_recon("2024-01"),
                normalized_path=Path("normalized.json"),
                recon_path=Path("recon.json"),
                repo_root=Path("."),
                posting_policy=self.policy(),
                inventory_transfer_evidence=evidence,
            )


class AggregateSalesRoutingTests(unittest.TestCase):
    def aggregate(self) -> dict:
        row = record(record_id="woo:2024-01", source_system="woo", event_type="woo_daily_sales",
                     gross_amount=137.46, vat_amount=24.8, channel="woo",
                     attributes={"is_monthly_summary": True, "orders": 3})
        row["quantity"] = 4
        return row

    def test_an_aggregate_is_routed_from_the_periods_order_numbers(self) -> None:
        policy = warehouse_routing_policy()

        self.assertEqual(
            bookbuilder.routed_sales_warehouse(
                policy, group_label="woo", record=self.aggregate(), fallback_order_numbers=(762, 765)
            ),
            "6",
        )
        self.assertEqual(
            bookbuilder.routed_sales_warehouse(
                policy, group_label="woo", record=self.aggregate(), fallback_order_numbers=(776, 777)
            ),
            "1",
        )

    def test_an_aggregate_straddling_the_boundary_blocks(self) -> None:
        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "boundary"):
            bookbuilder.routed_sales_warehouse(
                warehouse_routing_policy(), group_label="woo", record=self.aggregate(),
                fallback_order_numbers=(770, 771),
            )

    def test_an_aggregate_with_no_order_evidence_at_all_blocks(self) -> None:
        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "order number"):
            bookbuilder.routed_sales_warehouse(
                warehouse_routing_policy(), group_label="woo", record=self.aggregate(),
                fallback_order_numbers=(),
            )

    def test_a_records_own_order_number_still_wins(self) -> None:
        own = woo_sale(record_id="woo:900", order_id=900)

        self.assertEqual(
            bookbuilder.routed_sales_warehouse(
                warehouse_routing_policy(), group_label="woo", record=own, fallback_order_numbers=(1,)
            ),
            "1",
        )

    def test_an_aggregate_declares_the_orders_its_routing_actually_used(self) -> None:
        normalized = base_normalized("2024-01")
        aggregate = self.aggregate()
        # The order number lives on the processor-side charge, which groups separately --
        # exactly how a real month looks.
        charge = record(record_id="stripe:1", source_system="stripe", event_type="stripe_charge",
                        gross_amount=60.0, channel="stripe", attributes={"order_id": "762"})
        normalized["records"]["sales"] = [aggregate, charge]

        batch = build_batch_with_policy(normalized, warehouse_routing_policy())
        scopes = [a["payload"]["summary_scope"] for a in actions_of_type(batch, "create_invoice_summary")]
        routed = [s["warehouse_routing"] for s in scopes if "warehouse_routing" in s]

        self.assertTrue(routed)
        for routing in routed:
            self.assertTrue(routing["order_numbers"], "declared routing must name the orders it used")
            self.assertEqual(routing["warehouse_id"], "6")

    def test_the_reviewed_vat_allocation_also_names_orders(self) -> None:
        sale = record(record_id="woo:1", source_system="woo", event_type="woo_monthly_sales",
                      gross_amount=30.0, channel="woo",
                      attributes={"vat_allocation": {"allocated_order_ids": ["763", "765"]}})

        self.assertEqual(bookbuilder.period_order_numbers({"sales": [sale]}), [763, 765])

    def test_the_period_order_numbers_are_gathered_from_every_category(self) -> None:
        records = {
            "sales": [self.aggregate(), woo_sale(record_id="stripe:1", order_id=762)],
            "refunds": [woo_sale(record_id="stripe:2", order_id=765)],
        }

        self.assertEqual(bookbuilder.period_order_numbers(records), [762, 765])


class StatementImportDependencyTests(unittest.TestCase):
    def batch(self, policy: dict | None) -> dict:
        row = bank_row(record_id="fee1", amount=-2.0, event_date="2024-01-10")
        overrides: dict = {"posting_policy": policy} if policy else {}
        return build_with(bank=row, allocation=manual_allocation(row=row, disposition="bank_fee_payment"),
                          **overrides)

    def deps(self, policy: dict | None) -> list[dict]:
        return [d for d in self.batch(policy)["unresolved_dependencies"]
                if d.get("kind") == "manual_statement_import_financial_transaction"]

    def test_api_cash_mode_still_blocks_on_per_row_live_proof(self) -> None:
        deps = self.deps(api_cash_policy())

        self.assertEqual([d["blocking"] for d in deps], [True])

    def test_statement_import_mode_does_not_block_the_document_batch(self) -> None:
        deps = self.deps(statement_import_policy())

        # The API never posts this row; the annual plan carries it and post-import ledger
        # evidence proves it, so it must not block unrelated document creation.
        self.assertEqual([d["blocking"] for d in deps], [False])

    def test_statement_import_mode_still_records_the_row_and_why(self) -> None:
        dep = self.deps(statement_import_policy())[0]

        self.assertEqual(dep["record_id"], "fee1")
        self.assertIn("statement-import plan", dep["reason"])

    def test_a_verified_proof_is_still_honoured(self) -> None:
        deps = self.deps(api_cash_policy())

        self.assertTrue(all(d["kind"] == "manual_statement_import_financial_transaction" for d in deps))


def allocated_order_sale(*, quantity: float | None) -> dict:
    """A monthly Woo summary whose VAT allocation names one order, optionally with a quantity."""
    sale = record(record_id="woo:2024-01", source_system="woo", event_type="woo_monthly_sales",
                  gross_amount=35.82, vat_amount=6.46, shipping_amount=5.82, channel="woo",
                  attributes={"is_monthly_summary": True, "orders": 1})
    sale["event_date"] = "2024-01-31"
    entry = {
        "order_id": "763", "event_date": "2024-01-08",
        "fixed_product_gross": 30.0, "product_vat": 5.41,
        "fixed_shipping_gross": 5.82, "shipping_vat": 1.05,
        "source_row_id": "woo-tax:2", "source_refs": [],
    }
    if quantity is not None:
        entry["quantity"] = quantity
    sale["attributes"]["vat_allocation"] = {
        "fixed_product_gross": 30.0, "fixed_shipping_gross": 5.82,
        "product_vat": 5.41, "shipping_vat": 1.05,
        "allocation_path": "companies/example/artifacts/vat/2024-woo-tax-allocation.json",
        "allocated_order_ids": ["763"],
        "component_vat_evidence": [entry],
    }
    return sale


class AllocatedOrderQuantityTests(unittest.TestCase):
    def goods_line(self, *, quantity: float | None) -> dict:
        normalized = base_normalized("2024-01")
        normalized["records"]["sales"] = [allocated_order_sale(quantity=quantity)]
        batch = build_batch_with_policy(normalized, warehouse_routing_policy())
        action = actions_of_type(batch, "create_invoice_summary")[0]
        return next(
            line for line in action["payload"]["line_items"]
            if line.get("vat_allocation_component") == "goods"
        )

    def test_a_reviewed_order_quantity_reaches_the_goods_line(self) -> None:
        line = self.goods_line(quantity=2)

        self.assertEqual(line["quantity"], 2.0)

    def test_the_goods_line_carries_an_exact_proof_naming_the_order(self) -> None:
        proof = self.goods_line(quantity=2)["inventory_quantity_proof"]

        self.assertEqual(proof["status"], "exact")
        self.assertEqual(proof["quantity"], 2.0)
        self.assertEqual(proof["contributor_count"], 1)
        self.assertEqual(proof["contributors"][0]["record_id"], "763")
        self.assertEqual(proof["contributors"][0]["quantity_source"], "reviewed_woo_tax_allocation")

    def test_an_order_without_a_reviewed_quantity_gets_no_proof(self) -> None:
        line = self.goods_line(quantity=None)

        self.assertIsNone(line.get("quantity"))
        self.assertIsNone(line.get("inventory_quantity_proof"))

    def test_a_zero_quantity_is_refused_rather_than_treated_as_one(self) -> None:
        line = self.goods_line(quantity=0)

        self.assertIsNone(line.get("inventory_quantity_proof"))

    def test_the_shipping_line_never_claims_a_quantity(self) -> None:
        normalized = base_normalized("2024-01")
        normalized["records"]["sales"] = [allocated_order_sale(quantity=2)]
        batch = build_batch_with_policy(normalized, warehouse_routing_policy())
        action = actions_of_type(batch, "create_invoice_summary")[0]
        shipping = next(
            line for line in action["payload"]["line_items"]
            if line.get("vat_allocation_component") == "shipping"
        )

        self.assertIsNone(shipping.get("quantity"))
        self.assertIsNone(shipping.get("inventory_quantity_proof"))


class NonInventoryRefundTests(unittest.TestCase):
    def normalized(self) -> dict:
        normalized = base_normalized("2024-05")
        normalized["records"]["refunds"] = [
            record(record_id="pp:chargeback", source_system="paypal", event_type="paypal_chargeback",
                   gross_amount=-32.85, channel="woo"),
            record(record_id="pp:disputefee", source_system="paypal", event_type="paypal_dispute_fee",
                   gross_amount=-14.0, channel="woo"),
        ]
        return normalized

    def policy(self, *, declared: bool) -> dict:
        policy = warehouse_routing_policy()
        policy["mappings"]["woo-non-taxable"] = {
            "income_account_id": "109", "vat_type_id": "12", "warehouse_id": "6", "article_id": "3",
        }
        if declared:
            policy["non_inventory_event_types"] = ["paypal_chargeback", "paypal_dispute_fee"]
        return policy

    def refund_lines(self, *, declared: bool) -> list[dict]:
        batch = build_batch_with_policy(self.normalized(), self.policy(declared=declared))
        actions = [a for a in batch["actions"] if "refund" in a["idempotency_key"]]
        return [line for a in actions for line in a["payload"]["line_items"]]

    def test_a_declared_cash_reversal_takes_no_article(self) -> None:
        for line in self.refund_lines(declared=True):
            self.assertIsNone(line.get("article_id_hint"))

    def test_a_declared_cash_reversal_raises_no_quantity_dependency(self) -> None:
        batch = build_batch_with_policy(self.normalized(), self.policy(declared=True))
        quantity_blocks = [
            d for d in batch["unresolved_dependencies"] if d.get("kind") == "inventory_quantity"
        ]

        self.assertEqual(quantity_blocks, [])

    def test_an_undeclared_event_still_blocks_on_its_missing_quantity(self) -> None:
        batch = build_batch_with_policy(self.normalized(), self.policy(declared=False))
        quantity_blocks = [
            d for d in batch["unresolved_dependencies"] if d.get("kind") == "inventory_quantity"
        ]

        self.assertTrue(quantity_blocks)

    def test_the_line_records_the_event_types_it_was_built_from(self) -> None:
        lines = self.refund_lines(declared=True)

        self.assertTrue(any(line.get("contributor_event_types") for line in lines))
        for line in lines:
            for event_type in line.get("contributor_event_types") or []:
                self.assertIn(event_type, {"paypal_chargeback", "paypal_dispute_fee"})


class ForeignCurrencyPilotGateTests(unittest.TestCase):
    def inputs(self) -> tuple[dict, dict]:
        row = bank_row(record_id="fx1", amount=-30.2, event_date="2024-11-01")
        item = manual_allocation(row=row, disposition="generated_purchase_payment")
        item["target"] = {
            "document_type": "purchase",
            "action_key": "example-2024-10-purchase-distributor",
            "foreign_currency_pilot_required": True,
            "target_currency": "USD",
            "pilot_requirements": ["applied_ecb_rate"],
        }
        records = {"bank_transactions": [row]}
        allocations = {(item["statement_id"], item["iban"], item["currency"]): item}
        return records, allocations

    def deps(self, policy: dict | None) -> list[dict]:
        records, allocations = self.inputs()
        return bookbuilder.build_foreign_currency_payment_pilot_dependencies(
            records=records, allocations=allocations, posting_policy=policy,
        )

    def test_a_pilot_is_required_when_no_policy_says_otherwise(self) -> None:
        self.assertEqual([d["blocking"] for d in self.deps(None)], [True])

    def test_api_cash_mode_still_requires_the_pilot(self) -> None:
        self.assertEqual([d["blocking"] for d in self.deps(api_cash_policy())], [True])

    def test_import_mode_does_not_require_a_pilot_for_a_payment_it_never_posts(self) -> None:
        # The API creates no payment for this row; the imported statement settles it.
        self.assertEqual(self.deps(statement_import_policy()), [])

    def test_a_row_on_an_unmanaged_account_still_requires_the_pilot(self) -> None:
        policy = statement_import_policy()
        policy["cash_posting"] = dict(policy["cash_posting"], bank_income_account_ids=["999"])

        self.assertEqual([d["blocking"] for d in self.deps(policy)], [True])

    def test_a_row_without_the_reviewed_flag_needs_no_pilot(self) -> None:
        records, allocations = self.inputs()
        for item in allocations.values():
            item["target"].pop("foreign_currency_pilot_required")

        self.assertEqual(
            bookbuilder.build_foreign_currency_payment_pilot_dependencies(
                records=records, allocations=allocations, posting_policy=None), [])


class TransferEvidencePathTests(unittest.TestCase):
    def test_the_default_path_sits_with_the_other_company_artifacts(self) -> None:
        path = bookbuilder.resolve_inventory_transfer_evidence_path(
            company_dir=Path("companies/example"), normalized_path=Path("x"), period="2024-11", override=None,
        )

        self.assertEqual(path, Path("companies/example/artifacts/actions/2024-inventory-transfers.json"))

    def test_an_explicit_override_wins(self) -> None:
        path = bookbuilder.resolve_inventory_transfer_evidence_path(
            company_dir=Path("companies/example"), normalized_path=Path("x"),
            period="2024-11", override="elsewhere.json",
        )

        self.assertEqual(path, Path("elsewhere.json"))

    def test_a_missing_file_loads_as_no_evidence_rather_than_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                bookbuilder.load_inventory_transfer_evidence(Path(tmp) / "absent.json"), []
            )

    def test_a_single_reviewed_transfer_loads_as_one_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({
                "action_type": "warehouse_transfer", "destination_warehouse_id": "9",
                "source_warehouse_id": "1", "article_id": "3", "status": "complete",
            }), encoding="utf-8")

            evidence = bookbuilder.load_inventory_transfer_evidence(path)

            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["destination_warehouse_id"], "9")

    def test_a_list_of_transfers_loads_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps([
                {"action_type": "warehouse_transfer", "destination_warehouse_id": "9"},
                {"action_type": "warehouse_transfer", "destination_warehouse_id": "7"},
            ]), encoding="utf-8")

            self.assertEqual(len(bookbuilder.load_inventory_transfer_evidence(path)), 2)


if __name__ == "__main__":
    unittest.main()


def processor_settlement_policy() -> dict:
    """Statement-import policy where PayPal and Stripe hold separate reviewed accounts."""
    cash = json.loads(json.dumps(STATEMENT_IMPORT_CASH_POSTING))
    cash["processor_income_account_ids"] = {"paypal": "6", "stripe": "7"}
    cash["clearing_provider_roles"] = {"paypal": "paypal", "stripe": "stripe_clearing"}
    policy = dict(direct_sale_policy(), cash_posting=cash)
    policy["contacts"] = {
        "sales": {"woo": "42", "direct-sale": "42"},
        "processors": {"paypal": "63", "stripe": "29"},
        "suppliers": {},
    }
    policy["mappings"]["woo-taxable"] = {
        "income_account_id": "107", "shipping_income_account_id": "253",
        "vat_type_id": "25", "shipping_vat_type_id": "24", "warehouse_id": "6",
    }
    return policy


def processor_settlement_normalized() -> dict:
    """One month whose Woo invoice is settled partly by PayPal and partly by Stripe."""
    normalized = base_normalized("2024-01")
    woo = record(record_id="woo:2024-01", source_system="woo", event_type="woo_monthly_sales",
                 gross_amount=137.46, vat_amount=24.79, shipping_amount=17.46, channel="woo")
    woo["event_date"] = "2024-01-31"
    paypal = record(record_id="pp:1", source_system="paypal", event_type="paypal_website_payment",
                    gross_amount=35.82, fee_amount=1.57, channel="paypal")
    stripe = record(record_id="st:1", source_system="stripe", event_type="stripe_charge",
                    gross_amount=101.64, fee_amount=2.03, channel="stripe")
    normalized["records"]["sales"] = [woo, paypal, stripe]
    return normalized


class ProcessorSettlementTests(unittest.TestCase):
    """A processor-held sale is settled inside the processor account, never by the bank.

    The bank statement only ever shows the net payout, so no imported bank row can pay a
    gross sales invoice. Without these actions the monthly invoice stays open forever.
    """

    def batch(self) -> dict:
        return build_batch_with_policy(processor_settlement_normalized(), processor_settlement_policy())

    def test_each_processor_receives_its_own_gross_into_its_own_account(self) -> None:
        receipts = {
            action["payload"]["bank_account_id"]: action["payload"]["amount"]
            for action in actions_of_type(self.batch(), "create_incoming_summary")
        }

        self.assertEqual(receipts, {"6": 35.82, "7": 101.64})

    def test_each_processor_fee_is_paid_out_of_the_same_account(self) -> None:
        payments = {
            action["payload"]["bank_account_id"]: action["payload"]["amount"]
            for action in actions_of_type(self.batch(), "create_payment_summary")
        }

        self.assertEqual(payments, {"6": 1.57, "7": 2.03})

    def test_processor_settlement_is_not_prohibited_bank_cash(self) -> None:
        policy = processor_settlement_policy()
        batch = self.batch()
        cash = [a for a in batch["actions"]
                if a["action_type"] in ("create_incoming_summary", "create_payment_summary")]

        self.assertTrue(cash)
        for action in cash:
            self.assertFalse(posting_policy.prohibited_bank_cash_action(action, policy))

    def test_a_processor_without_a_reviewed_account_raises_rather_than_guessing(self) -> None:
        policy = processor_settlement_policy()
        del policy["cash_posting"]["processor_income_account_ids"]["stripe"]

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "stripe"):
            build_batch_with_policy(processor_settlement_normalized(), policy)

    def test_legacy_api_cash_posting_generates_no_processor_settlement(self) -> None:
        # Without a cash_posting section the policy is legacy API mode, which settles from
        # the bank side. Emitting a processor receipt there would double the cash.
        batch = build_batch_with_policy(processor_settlement_normalized(), direct_sale_policy())

        self.assertEqual(actions_of_type(batch, "create_incoming_summary"), [])
        self.assertEqual(actions_of_type(batch, "create_payment_summary"), [])

    def test_the_fee_payment_keeps_the_processor_contact(self) -> None:
        # apply_posting_policy re-resolves every contact; a processor fee payment must not
        # fall through to the suppliers role, which would blank the contact it was given.
        payments = actions_of_type(self.batch(), "create_payment_summary")

        self.assertEqual(
            {a["payload"]["counterparty_hint"]: a["payload"]["counterparty"]["contact_id"] for a in payments},
            {"paypal": "63", "stripe": "29"},
        )

    def test_a_receipt_carries_the_invoice_customer_not_the_processor(self) -> None:
        # The processor decides which cash account the money sits in; the invoice decides
        # whose receivable is being cleared. A receipt against the processor's own contact
        # can never settle a customer invoice.
        receipts = actions_of_type(self.batch(), "create_incoming_summary")

        self.assertEqual({a["payload"]["counterparty"]["contact_id"] for a in receipts}, {"42"})

    def test_each_receipt_links_to_the_invoice_it_settles(self) -> None:
        batch = self.batch()
        invoice_key = actions_of_type(batch, "create_invoice_summary")[0]["idempotency_key"]
        receipts = actions_of_type(batch, "create_incoming_summary")

        self.assertEqual({a["payload"]["linked_invoice_action"] for a in receipts}, {invoice_key})

    def test_distributor_invoices_are_not_settled_out_of_a_processor_account(self) -> None:
        # A distributor pays by bank transfer, so its invoice must never absorb money the
        # card processors are holding.
        normalized = processor_settlement_normalized()
        distributor = record(record_id="qm:1", source_system="quartermaster",
                             event_type="quartermaster_sales", gross_amount=792.12,
                             channel="quartermaster")
        distributor["event_date"] = "2024-01-31"
        normalized["records"]["sales"].append(distributor)

        receipts = actions_of_type(
            build_batch_with_policy(normalized, processor_settlement_policy()),
            "create_incoming_summary",
        )

        self.assertEqual(sum(a["payload"]["amount"] for a in receipts), 137.46)


DATED_VAT_BANDS = [
    {"start": "1900-01-01", "end": "2023-12-31", "vat_type_id": "3"},
    {"start": "2024-01-01", "end": None, "vat_type_id": "26"},
]


class DatedPurchasePinBuilderTests(unittest.TestCase):
    """The builder must emit the VAT type in force on the document date.

    A static pin keeps declaring a superseded rate on correct amounts; only the
    document date can decide which rate a purchase should have carried.
    """

    def test_a_2024_document_takes_the_rate_that_replaced_the_old_one(self) -> None:
        action = storage_fee_action()

        bookbuilder.apply_posting_policy(
            [action],
            posting_policy=printful_policy(vat_type_id=DATED_VAT_BANDS),
            entity_map=ZERO_RATE_VAT_TYPES,
        )

        self.assertEqual(action["payload"]["line_items"][0]["suggested_vat_type_id"], "26")

    def test_a_document_from_before_the_change_still_takes_the_old_rate(self) -> None:
        action = storage_fee_action()
        action["payload"]["document_date"] = "2023-11-28"

        bookbuilder.apply_posting_policy(
            [action],
            posting_policy=printful_policy(vat_type_id=DATED_VAT_BANDS),
            entity_map=ZERO_RATE_VAT_TYPES,
        )

        self.assertEqual(action["payload"]["line_items"][0]["suggested_vat_type_id"], "3")

    def test_a_document_covered_by_no_band_is_refused(self) -> None:
        action = storage_fee_action()
        action["payload"]["document_date"] = "2024-01-28"
        bands = [{"start": "2025-01-01", "end": None, "vat_type_id": "26"}]

        with self.assertRaises(bookbuilder.SimplbooksError):
            bookbuilder.apply_posting_policy(
                [action],
                posting_policy=printful_policy(vat_type_id=bands),
                entity_map=ZERO_RATE_VAT_TYPES,
            )


def article_line(*, order: str, article: str | None, gross: float, vat: float, qty: float | None) -> dict:
    return {
        "line_role": "sales_revenue",
        "description": f"woo allocated sales revenue summary - order {order}",
        "gross_amount": gross,
        "vat_amount_hint": vat,
        "article_id_hint": article,
        "warehouse_id_hint": "6",
        "record_count": 1,
        "quantity": qty,
        "inventory_quantity_proof": (
            {
                "status": "exact",
                "quantity": qty,
                "scope": {"kind": "reviewed_allocated_order"},
                "contributors": [{
                    "record_id": order,
                    "quantity": qty,
                    "quantity_source": "reviewed_woo_tax_allocation",
                    "record_sha256": bookbuilder.canonical_value_sha256({"order_id": order}),
                }],
            }
            if qty is not None else None
        ),
        "vat_allocation_component": "goods" if article else "shipping",
        "vat_allocation_component_evidence": [{"order_id": order}],
    }


def woo_invoice_action(lines: list[dict], *, physical_bank: bool = False) -> dict:
    return {
        "idempotency_key": "example-2024-01-sales-woo-wh6",
        "action_type": "create_invoice_summary",
        "payload": {"draft_schema": "invoice_summary_v1", "line_items": lines},
        "source_refs": (
            [{"path": "n.json", "record_ref": "r1", "source_kind": "physical_bank"}]
            if physical_bank else [{"path": "n.json", "record_ref": "r1"}]
        ),
    }


class UniqueArticlePerInvoiceTests(unittest.TestCase):
    """Simplbooks rejects an invoice repeating an article across rows.

    The defect this guards: `invoices/create` returns HTTP 400 with
    "Arverea artikkel peab arve piires unikaalne olema." A Woo month emits one
    line per order and every goods line carries the same article, so the invoice
    is unpostable. No dry run catches it, because a dry run never POSTs.
    """

    def test_lines_sharing_an_article_become_one_line(self) -> None:
        action = woo_invoice_action([
            article_line(order="762", article="3", gross=60.0, vat=10.82, qty=2.0),
            article_line(order="763", article="3", gross=30.0, vat=5.41, qty=1.0),
            article_line(order="765", article="3", gross=30.0, vat=5.41, qty=1.0),
        ])

        bookbuilder.merge_same_article_invoice_lines(action)

        lines = action["payload"]["line_items"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["gross_amount"], 120.0)
        self.assertEqual(lines[0]["vat_amount_hint"], 21.64)
        self.assertEqual(lines[0]["quantity"], 4.0)

    def test_the_inventory_proof_keeps_every_contributor(self) -> None:
        action = woo_invoice_action([
            article_line(order="762", article="3", gross=60.0, vat=10.82, qty=2.0),
            article_line(order="763", article="3", gross=30.0, vat=5.41, qty=1.0),
        ])

        bookbuilder.merge_same_article_invoice_lines(action)

        proof = action["payload"]["line_items"][0]["inventory_quantity_proof"]
        self.assertEqual(proof["status"], "exact")
        self.assertEqual(proof["quantity"], 3.0)
        self.assertEqual(len(proof["contributors"]), 2)

    def test_lines_without_an_article_are_left_alone(self) -> None:
        action = woo_invoice_action([
            article_line(order="762", article="3", gross=60.0, vat=10.82, qty=2.0),
            article_line(order="762", article=None, gross=5.82, vat=1.05, qty=None),
            article_line(order="763", article=None, gross=5.82, vat=1.05, qty=None),
        ])

        bookbuilder.merge_same_article_invoice_lines(action)

        lines = action["payload"]["line_items"]
        self.assertEqual(len(lines), 3)
        self.assertEqual([line["gross_amount"] for line in lines], [60.0, 5.82, 5.82])

    def test_different_articles_do_not_merge(self) -> None:
        action = woo_invoice_action([
            article_line(order="762", article="3", gross=60.0, vat=10.82, qty=2.0),
            article_line(order="763", article="9", gross=30.0, vat=5.41, qty=1.0),
        ])

        bookbuilder.merge_same_article_invoice_lines(action)

        self.assertEqual(len(action["payload"]["line_items"]), 2)

    def test_a_single_article_line_is_untouched(self) -> None:
        action = woo_invoice_action([
            article_line(order="762", article="3", gross=60.0, vat=10.82, qty=2.0),
        ])
        before = copy.deepcopy(action)

        bookbuilder.merge_same_article_invoice_lines(action)

        self.assertEqual(action, before)

    def test_an_index_coupled_invoice_is_refused_not_silently_merged(self) -> None:
        """A direct-sale invoice maps physical rows to lines by position; merging
        would shift those indices and break the mapping without saying so."""
        action = woo_invoice_action([
            article_line(order="762", article="3", gross=60.0, vat=10.82, qty=2.0),
            article_line(order="763", article="3", gross=30.0, vat=5.41, qty=1.0),
        ], physical_bank=True)

        with self.assertRaises(bookbuilder.SimplbooksError):
            bookbuilder.merge_same_article_invoice_lines(action)


class MergedArticleLineEvidenceTests(unittest.TestCase):
    """A merged line's inventory proof must still prove itself.

    The checker rebuilds the expected contributor set from the line's own
    component evidence, then compares contributors, count and hash. Summing the
    contributors while leaving the count and hash stale makes the proof
    self-inconsistent, which is what broke 2024-01 after the first merge.
    """

    def _line(self, *, order: str, qty: float) -> dict:
        proof = bookbuilder.inventory_proof_envelope(
            scope={"kind": "reviewed_allocated_order"},
            contributors=[{
                "record_id": order,
                "quantity": qty,
                "quantity_source": "reviewed_woo_tax_allocation",
                "record_sha256": bookbuilder.canonical_value_sha256({"order_id": order}),
            }],
            quantity=Decimal(str(qty)),
        )
        return {
            "line_role": "sales_revenue",
            "description": f"woo allocated sales revenue summary - order {order}",
            "gross_amount": 30.0 * qty,
            "vat_amount_hint": 5.41 * qty,
            "article_id_hint": "3",
            "record_count": 1,
            "quantity": qty,
            "inventory_quantity_proof": proof,
            "vat_allocation_component_evidence": [{"order_id": order}],
        }

    def test_the_merged_proof_recounts_and_rehashes_its_contributors(self) -> None:
        action = woo_invoice_action([
            self._line(order="765", qty=1.0),
            self._line(order="762", qty=2.0),
        ])

        bookbuilder.merge_same_article_invoice_lines(action)

        proof = action["payload"]["line_items"][0]["inventory_quantity_proof"]
        expected = bookbuilder.inventory_proof_envelope(
            scope={"kind": "reviewed_allocated_order"},
            contributors=[
                {
                    "record_id": order,
                    "quantity": qty,
                    "quantity_source": "reviewed_woo_tax_allocation",
                    "record_sha256": bookbuilder.canonical_value_sha256({"order_id": order}),
                }
                for order, qty in (("765", 1.0), ("762", 2.0))
            ],
            quantity=Decimal(3),
        )
        self.assertEqual(proof["contributor_count"], expected["contributor_count"])
        self.assertEqual(proof["contributor_set_sha256"], expected["contributor_set_sha256"])
        self.assertEqual(proof["contributors"], expected["contributors"])

    def test_the_merged_contributors_are_sorted_not_concatenated(self) -> None:
        """Order 762 was added second; the proof must still list it first."""
        action = woo_invoice_action([
            self._line(order="765", qty=1.0),
            self._line(order="762", qty=2.0),
        ])

        bookbuilder.merge_same_article_invoice_lines(action)

        proof = action["payload"]["line_items"][0]["inventory_quantity_proof"]
        self.assertEqual([c["record_id"] for c in proof["contributors"]], ["762", "765"])


class MergeRefusesUnfaithfulRoundingTests(unittest.TestCase):
    """Merging is only faithful when the rate applied to the merged gross agrees.

    Simplbooks recomputes each row's VAT from its rate. Four orders of 0.03 at
    24% carry 0.04 of VAT between them but yield 0.02 on a merged 0.12 row, so
    merging them would post a document contradicting its own evidence.
    """

    def _line(self, *, order: str, gross: float, vat: float, rate: int) -> dict:
        return {
            "line_role": "sales_revenue",
            "description": f"woo allocated sales revenue summary - order {order}",
            "gross_amount": gross,
            "vat_amount_hint": vat,
            "article_id_hint": "3",
            "record_count": 1,
            "quantity": 1.0,
            "vat_profile_rate": rate,
            "inventory_quantity_proof": None,
            "vat_allocation_component_evidence": [{"order_id": order}],
        }

    def test_merging_is_refused_when_rounding_would_shift(self) -> None:
        action = woo_invoice_action([
            self._line(order=f"EXAMPLE-{index}", gross=0.03, vat=0.01, rate=24)
            for index in range(1, 5)
        ])

        with self.assertRaisesRegex(bookbuilder.SimplbooksError, "rounding"):
            bookbuilder.merge_same_article_invoice_lines(action)

    def test_merging_proceeds_when_rounding_agrees(self) -> None:
        action = woo_invoice_action([
            self._line(order="762", gross=60.0, vat=10.82, rate=22),
            self._line(order="763", gross=30.0, vat=5.41, rate=22),
            self._line(order="765", gross=30.0, vat=5.41, rate=22),
        ])

        bookbuilder.merge_same_article_invoice_lines(action)

        lines = action["payload"]["line_items"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["vat_amount_hint"], 21.64)


class PrintfulVatFollowsShippingOriginTests(unittest.TestCase):
    """VAT jurisdiction follows where a shipment left from, not where stock sits.

    Printful fulfils from Latvia, so `warehouse_id` is always LV. But a shipment
    routed through GB carries UK VAT that this company expenses rather than
    reclaims, and the policy expresses that on a per-origin line key. Bucketing
    on the warehouse collapsed every origin onto `-lv`, so the GB line lost its
    `vat_deductible: false` and the UK VAT tripped the zero-rated guard.
    """

    def _record(self, *, warehouse: str, shipped_from: str | None) -> dict:
        return {
            "record_id": "r1",
            "warehouse_id": warehouse,
            "attributes": {"shipped_from": shipped_from},
        }

    def test_the_reported_origin_decides_the_vat_bucket(self) -> None:
        record = self._record(warehouse="LV", shipped_from="GB")

        self.assertEqual(bookbuilder.printful_vat_origin(record), "GB")

    def test_it_falls_back_to_the_warehouse_when_no_origin_is_reported(self) -> None:
        record = self._record(warehouse="LV", shipped_from=None)

        self.assertEqual(bookbuilder.printful_vat_origin(record), "LV")

    def test_the_origin_is_upper_cased(self) -> None:
        record = self._record(warehouse="LV", shipped_from="gb")

        self.assertEqual(bookbuilder.printful_vat_origin(record), "GB")
