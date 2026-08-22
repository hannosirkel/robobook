from __future__ import annotations  # noqa: I001

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import document_identity  # noqa: E402
import examine_simplbooks_year  # noqa: E402


def purchase_candidate(**overrides: object) -> dict:
    record = {
        "event_date": "2024-11-18",
        "external_ref": "EE24111268",
        "currency": "EUR",
        "gross_amount": 206.18,
        "attributes": {"vendor_name": "SimplBooks OÜ"},
    }
    record.update(overrides)
    return record


def existing_purchase(**overrides: object) -> dict:
    record = {
        "id": 157,
        "client_name": "Simplbooks OÜ",
        "number": "EE24111268",
        "transaction_date": "2024-11-18",
        "currency_name": "EUR",
        "total_sum": 206.18,
    }
    record.update(overrides)
    return record


class DocumentIdentityTests(unittest.TestCase):
    def test_external_number_and_supplier_match_is_exact(self) -> None:
        candidate = document_identity.document_identity(purchase_candidate(), document_type="purchase")
        existing = document_identity.document_identity(existing_purchase(), document_type="purchase")

        result = document_identity.match_existing(candidate, [existing])

        self.assertEqual(result.status, "exact")
        self.assertEqual(result.matches[0].simplbooks_id, "157")

    def test_same_number_with_incompatible_supplier_is_ambiguous(self) -> None:
        candidate = document_identity.document_identity(purchase_candidate(), document_type="purchase")
        existing = document_identity.document_identity(
            existing_purchase(client_name="Different Supplier OÜ"),
            document_type="purchase",
        )

        self.assertEqual(document_identity.match_existing(candidate, [existing]).status, "ambiguous")

    def test_without_number_all_fallback_fields_must_match(self) -> None:
        candidate = document_identity.document_identity(
            purchase_candidate(external_ref=None),
            document_type="purchase",
        )
        exact = document_identity.document_identity(
            existing_purchase(number=""),
            document_type="purchase",
        )
        wrong_amount = document_identity.document_identity(
            existing_purchase(id=158, number="", total_sum=206.19),
            document_type="purchase",
        )

        result = document_identity.match_existing(candidate, [exact, wrong_amount])

        self.assertEqual(result.status, "exact")
        self.assertEqual(len(result.matches), 1)

    def test_year_overview_document_index_contains_all_documents(self) -> None:
        index = examine_simplbooks_year.build_document_index(
            invoices=[
                {
                    "id": 12,
                    "client_name": "Eraisik",
                    "number": "WEB-1",
                    "transaction_date": "2024-01-31",
                    "currency_name": "EUR",
                    "total_sum": 10,
                }
            ],
            purchases=[existing_purchase()],
        )

        self.assertEqual(len(index), 2)
        self.assertEqual(index[1]["external_number"], "EE24111268")
        self.assertEqual(index[1]["simplbooks_id"], "157")


if __name__ == "__main__":
    unittest.main()
