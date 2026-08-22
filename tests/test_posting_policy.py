from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
