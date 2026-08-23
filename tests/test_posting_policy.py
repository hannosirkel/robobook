from __future__ import annotations  # noqa: I001

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import posting_policy  # noqa: E402


def posting_policy_fixture_with_profiles() -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "bank_accounts": {},
        "contacts": {"sales": {"woo": "42"}},
        "mappings": {
            "woo-taxable": {
                "income_account_id": "107",
                "shipping_income_account_id": "253",
                "vat_type_id": "25",
                "shipping_vat_type_id": "24",
                "warehouse_id": "9",
            }
        },
        "sales_vat_profiles": [
            {"start": "2025-01-01", "end": "2025-06-30", "rate": 22,
             "goods_vat_type_id": "25", "shipping_vat_type_id": "24"},
            {"start": "2025-07-01", "end": None, "rate": 24,
             "goods_vat_type_id": "34", "shipping_vat_type_id": "33"},
        ],
        "supplier_aliases": {},
    }


def statement_import_policy_fixture() -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "bank_accounts": {"EE001234567890": {"EUR": "3", "USD": "3"}},
        "contacts": {},
        "mappings": {},
        "supplier_aliases": {},
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
                "fx_loss": "36",
            },
        },
        "warehouse_routing": {
            "woo": {"before_order": 1000, "before_warehouse_id": "6", "from_warehouse_id": "1"},
            "direct_sale_warehouse_id": "1",
            "distributor_warehouse_id": "9",
        },
    }


class PostingPolicyTests(unittest.TestCase):
    def test_linked_invoice_receipt_uses_sales_contact_role(self) -> None:
        policy = {
            "bank_accounts": {"EE123": {"EUR": "3"}},
            "contacts": {"sales": {"brain-games": "31"}, "processors": {}, "suppliers": {}},
            "mappings": {},
        }
        action = {
            "action_type": "create_incoming_summary",
            "payload": {
                "draft_schema": "cash_settlement_v1",
                "document_type": "incoming",
                "linked_invoice_id": "58",
                "counterparty_hint": "brain-games",
                "counterparty": {"contact_id": "31"},
                "bank_account_id": "3",
            },
        }

        self.assertEqual(posting_policy.action_policy_errors(action, policy), [])

    def test_resolve_sales_vat_profile_changes_on_effective_date(self) -> None:
        policy = posting_policy_fixture_with_profiles()

        self.assertEqual(
            posting_policy.resolve_sales_vat_profile(policy, event_date=date(2025, 6, 30))["rate"], 22
        )
        self.assertEqual(
            posting_policy.resolve_sales_vat_profile(policy, event_date=date(2025, 7, 1))["rate"], 24
        )

    def test_policy_rejects_overlapping_sales_vat_profiles(self) -> None:
        policy = posting_policy_fixture_with_profiles()
        policy["sales_vat_profiles"][1]["start"] = "2025-06-30"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "overlap"):
            posting_policy.validate_posting_policy(policy)

    def test_bank_account_resolution_requires_exact_source_account(self) -> None:
        policy = {"bank_accounts": {"EE-LHV": "3"}}

        self.assertEqual(posting_policy.resolve_bank_account(policy, customer_account="EE-LHV"), "3")
        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.resolve_bank_account(policy, customer_account="UNKNOWN")

    def test_bank_account_resolution_requires_exact_currency_mapping(self) -> None:
        policy = {"bank_accounts": {"EE-LHV": {"EUR": "3", "USD": "4"}}}

        self.assertEqual(
            posting_policy.resolve_bank_account(policy, customer_account="ee-lhv", currency="eur"),
            "3",
        )
        self.assertEqual(
            posting_policy.resolve_bank_account(policy, customer_account="EE-LHV", currency="USD"),
            "4",
        )
        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "GBP"):
            posting_policy.resolve_bank_account(policy, customer_account="EE-LHV", currency="GBP")

        legacy = {"bank_accounts": {"EE-LHV": "3"}}
        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "specify currency 'USD'"):
            posting_policy.resolve_bank_account(legacy, customer_account="EE-LHV", currency="USD")
        self.assertEqual(
            posting_policy.resolve_bank_account(
                legacy,
                customer_account="EE-LHV",
                currency="USD",
                allow_legacy_single_currency=True,
            ),
            "3",
        )

    def test_policy_rejects_non_uppercase_currency_bank_mapping(self) -> None:
        policy = posting_policy_fixture_with_profiles()
        policy["bank_accounts"] = {"EE-LHV": {"usd": "4"}}

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "invalid currency"):
            posting_policy.validate_posting_policy(policy)

    def test_woo_uses_eraisik_and_paypal_never_falls_back_to_stripe(self) -> None:
        policy = {
            "contacts": {
                "sales": {"woo": "42"},
                "processors": {"stripe": "29"},
            }
        }

        self.assertEqual(posting_policy.resolve_contact(policy, role="sales", label="woo"), "42")
        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.resolve_contact(policy, role="processors", label="paypal")

    def test_load_policy_rejects_missing_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posting_policy.json"
            path.write_text('{"schema_version":"1.0"}', encoding="utf-8")

            with self.assertRaises(posting_policy.PostingPolicyError):
                posting_policy.load_posting_policy(path)

    def test_policy_rejects_non_numeric_submit_mapping_id(self) -> None:
        policy = {
            "schema_version": "1.0",
            "company_slug": "example",
            "bank_accounts": {},
            "contacts": {},
            "mappings": {"purchase-printful": {"warehouse_id": "LV"}},
            "supplier_aliases": {},
        }

        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.validate_posting_policy(policy)

    def test_supplier_alias_resolution_is_explicit(self) -> None:
        policy = {"supplier_aliases": {"omniva": "as-eesti-post"}}

        self.assertEqual(posting_policy.resolve_supplier_alias(policy, "Omniva"), "as-eesti-post")
        self.assertEqual(posting_policy.resolve_supplier_alias(policy, "Unknown Supplier"), "unknown-supplier")


class StatementImportPolicyTests(unittest.TestCase):
    def test_statement_import_policy_requires_bank_and_financial_accounts(self) -> None:
        policy = statement_import_policy_fixture()

        posting_policy.validate_posting_policy(policy)

        self.assertEqual(posting_policy.cash_posting_mode(policy), "statement_import")
        resolved = posting_policy.statement_import_policy(policy)
        self.assertEqual(resolved["bank_income_account_ids"], ["3"])
        self.assertEqual(resolved["processor_income_account_ids"], {"paypal": "6", "stripe": "7"})
        self.assertEqual(resolved["financial_accounts"]["bank_fees"], "32")

    def test_cash_posting_mode_defaults_to_api_when_section_is_absent(self) -> None:
        policy = statement_import_policy_fixture()
        del policy["cash_posting"]

        posting_policy.validate_posting_policy(policy)

        self.assertEqual(posting_policy.cash_posting_mode(policy), "api")

    def test_statement_import_policy_rejects_api_mode(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"] = {"mode": "api"}

        posting_policy.validate_posting_policy(policy)

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "statement_import"):
            posting_policy.statement_import_policy(policy)

    def test_unknown_cash_posting_mode_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["mode"] = "manual"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "mode"):
            posting_policy.validate_posting_policy(policy)

    def test_missing_financial_account_role_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        del policy["cash_posting"]["financial_accounts"]["fx_loss"]

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "fx_loss"):
            posting_policy.validate_posting_policy(policy)

    def test_unknown_financial_account_role_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["financial_accounts"]["bank_fee"] = "99"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "bank_fee"):
            posting_policy.validate_posting_policy(policy)

    def test_non_numeric_financial_account_id_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["financial_accounts"]["bank_fees"] = "5350-fees"

        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.validate_posting_policy(policy)

    def test_empty_bank_income_account_ids_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["bank_income_account_ids"] = []

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "bank_income_account_ids"):
            posting_policy.validate_posting_policy(policy)

    def test_bank_and_processor_income_accounts_must_be_disjoint(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["processor_income_account_ids"]["stripe"] = "3"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "disjoint"):
            posting_policy.validate_posting_policy(policy)


class SalesWarehouseRoutingTests(unittest.TestCase):
    def test_woo_warehouse_boundary_is_inclusive(self) -> None:
        policy = statement_import_policy_fixture()

        self.assertEqual(posting_policy.resolve_sales_warehouse(policy, channel="woo", order_number=999), "6")
        self.assertEqual(posting_policy.resolve_sales_warehouse(policy, channel="woo", order_number=1000), "1")

    def test_woo_routing_requires_an_exact_order_number(self) -> None:
        policy = statement_import_policy_fixture()

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "order number"):
            posting_policy.resolve_sales_warehouse(policy, channel="woo", order_number=None)

    def test_direct_sale_uses_the_reviewed_warehouse(self) -> None:
        policy = statement_import_policy_fixture()

        self.assertEqual(
            posting_policy.resolve_sales_warehouse(policy, channel="direct-sale", order_number=None), "1"
        )

    def test_bound_distributor_warehouse_resolves(self) -> None:
        policy = statement_import_policy_fixture()

        self.assertEqual(
            posting_policy.resolve_sales_warehouse(policy, channel="distributor", order_number=None), "9"
        )

    def test_unbound_distributor_warehouse_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["warehouse_routing"]["distributor_warehouse_id"] = None

        posting_policy.validate_posting_policy(policy)

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "distributor"):
            posting_policy.resolve_sales_warehouse(policy, channel="distributor", order_number=None)

    def test_unknown_sales_channel_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "channel"):
            posting_policy.resolve_sales_warehouse(policy, channel="amazon", order_number=1)

    def test_incomplete_woo_routing_rule_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        del policy["warehouse_routing"]["woo"]["before_warehouse_id"]

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "before_warehouse_id"):
            posting_policy.validate_posting_policy(policy)


if __name__ == "__main__":
    unittest.main()
