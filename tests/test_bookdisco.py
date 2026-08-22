from __future__ import annotations  # noqa: I001

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookdisco  # noqa: E402


def make_overview(
    year: int,
    *,
    invoice_income: dict[str, int] | None = None,
    invoice_vat: dict[str, int] | None = None,
    purchase_expense: dict[str, int] | None = None,
    purchase_vat: dict[str, int] | None = None,
    warehouse_ids: dict[str, int] | None = None,
    invoice_articles: dict[str, int] | None = None,
    purchase_articles: dict[str, int] | None = None,
) -> dict[str, object]:
    return {
        "year": year,
        "company_id": "CID",
        "technical_findings": [],
        "counts": {
            "financial_accounts": 4,
            "income_accounts": 1,
            "vat_types": 2,
            "warehouses": 1,
            "invoices": 10,
            "invoice_rows": 12,
            "purchases": 5,
            "purchase_rows": 6,
            "receipts": 3,
            "payments": 2,
        },
        "monthly": {
            "invoices": {"2024-01": {"count": 1, "sum": 10.0, "vat": 2.0, "total_sum": 12.0}},
            "purchases": {"2024-01": {"count": 1, "sum": 5.0, "vat": 1.0, "total_sum": 6.0}},
            "receipts": {},
            "payments": {},
        },
        "patterns": {
            "invoice_income_account_ids": invoice_income or {},
            "invoice_vat_type_ids": invoice_vat or {},
            "invoice_article_ids": invoice_articles or {},
            "invoice_warehouse_ids": warehouse_ids or {},
            "purchase_expense_account_ids": purchase_expense or {},
            "purchase_vat_type_ids": purchase_vat or {},
            "purchase_article_ids": purchase_articles or {},
        },
        "samples": {},
    }


class BookdiscoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entity_map = {
            "financial_accounts": [
                {"id": "4000", "name": "Product sales", "code": "4000", "status": None},
                {"id": "4010", "name": "Shipping income", "code": "4010", "status": None},
                {"id": "5000", "name": "Processor fees", "code": "5000", "status": None},
                {"id": "5010", "name": "Fulfillment costs", "code": "5010", "status": None},
            ],
            "vat_types": [
                {"id": "20", "name": "VAT 22%", "code": None, "status": None},
            ],
            "warehouses": [
                {"id": "1", "name": "Main warehouse", "code": None, "status": None},
            ],
        }

    def test_entity_map_notes_missing_item_resolution(self) -> None:
        overview = make_overview(
            2023,
            invoice_articles={"100": 3},
            purchase_articles={"101": 2},
        )
        entity_map = bookdisco.build_entity_map_document(
            company_slug="example",
            generated_at="2026-04-04T00:00:00Z",
            as_of_period="2023-12",
            records_by_category={"financial_accounts": [], "income_accounts": [], "vat_types": [], "warehouses": []},
            overviews=[overview],
        )

        notes = entity_map.get("notes", [])
        self.assertTrue(any("Observed article IDs" in note for note in notes))
        self.assertFalse(entity_map.get("items"))

    def test_policy_memo_mentions_revenue_split_and_warehouse_identity(self) -> None:
        overviews = [
            make_overview(
                2022,
                invoice_income={"4000": 10, "4010": 3},
                invoice_vat={"20": 13},
                purchase_expense={"5000": 2},
                warehouse_ids={"1": 8},
            ),
            make_overview(
                2023,
                invoice_income={"4000": 12, "4010": 4},
                invoice_vat={"20": 16},
                purchase_expense={"5000": 3},
                warehouse_ids={"1": 9},
            ),
        ]

        memo = bookdisco.build_policy_memo_markdown(
            company_name="Example Company OÜ",
            years=[2022, 2023],
            overviews=overviews,
            entity_map=self.entity_map,
        )

        self.assertIn("multiple income accounts", memo)
        self.assertIn("Warehouse IDs appear on invoice rows across all lookback years", memo)
        self.assertIn("Top historical invoice VAT types", memo)

    def test_company_profile_is_derived_from_metadata_and_entity_map(self) -> None:
        profile = bookdisco.build_company_profile_document(
            company_name="Example Company OÜ",
            company_slug="example",
            company_id="CID",
            metadata={
                "description": "Test company",
                "vat registered": "yes",
            },
            entity_map={
                "income_accounts": [
                    {"id": "101", "name": "Main bank", "code": "1010", "status": None},
                    {"id": "102", "name": "Cash register", "code": "1020", "status": None},
                ],
                "warehouses": [
                    {"id": "1", "name": "Main warehouse", "code": None, "status": None},
                ],
            },
        )

        self.assertEqual(profile["bank_account_ids"], ["101"])
        self.assertEqual(profile["cash_account_ids"], ["102"])
        self.assertEqual(profile["default_warehouse_ids"], ["1"])
        self.assertTrue(profile["vat_registered"])
        self.assertEqual(profile["base_currency"], "EUR")

    def test_suspicious_patterns_detects_dominant_account_change(self) -> None:
        overviews = [
            make_overview(2022, invoice_income={"4000": 10, "4010": 1}),
            make_overview(2023, invoice_income={"4010": 11, "4000": 2}),
        ]

        suspicious = bookdisco.build_suspicious_patterns(
            overviews,
            bookdisco.build_entity_index(self.entity_map),
        )

        self.assertTrue(any("dominant invoice income account changes" in line for line in suspicious))

    def test_year_findings_has_clean_fallback_implication_line(self) -> None:
        overview = make_overview(2023)
        findings = bookdisco.build_year_findings_markdown(
            company_name="Example Company OÜ",
            year=2023,
            overview=overview,
        )

        self.assertIn("- No downstream implications were generated.", findings)
        self.assertNotIn("- - No downstream implications were generated.", findings)


if __name__ == "__main__":
    unittest.main()
