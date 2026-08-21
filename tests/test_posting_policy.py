from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import posting_policy  # noqa: E402


class PostingPolicyTests(unittest.TestCase):
    def test_bank_account_resolution_requires_exact_source_account(self) -> None:
        policy = {"bank_accounts": {"EE-LHV": "3"}}

        self.assertEqual(posting_policy.resolve_bank_account(policy, customer_account="EE-LHV"), "3")
        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.resolve_bank_account(policy, customer_account="UNKNOWN")

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

    def test_supplier_alias_resolution_is_explicit(self) -> None:
        policy = {"supplier_aliases": {"omniva": "as-eesti-post"}}

        self.assertEqual(posting_policy.resolve_supplier_alias(policy, "Omniva"), "as-eesti-post")
        self.assertEqual(posting_policy.resolve_supplier_alias(policy, "Unknown Supplier"), "unknown-supplier")


if __name__ == "__main__":
    unittest.main()
