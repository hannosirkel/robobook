from __future__ import annotations  # noqa: I001

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ledger_export_evidence  # noqa: E402, I001


HEADER = (
    "company_id,period,account_id,account_code,transaction_id,business_date,"
    "currency,debit,credit,description,document_ref"
)


def export_text(rows: list[str], *, header: str = HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


def posting(
    *,
    transaction_id: str,
    account_id: str,
    debit: str = "0.00",
    credit: str = "0.00",
    business_date: str = "2024-01-15",
    currency: str = "EUR",
    company_id: str = "42",
    document_ref: str = "",
) -> str:
    return (
        f"{company_id},2024,{account_id},CODE{account_id},{transaction_id},{business_date},"
        f"{currency},{debit},{credit},Statement row,{document_ref}"
    )


FEE_ROWS = [
    posting(transaction_id="t1", account_id="32", debit="12.50"),
    posting(transaction_id="t1", account_id="10", credit="12.50"),
]


def written_export(root: Path, text: str, *, name: str = "ledger.csv") -> dict[str, str]:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def plan_row(
    *,
    statement_id: str = "archive:a",
    signed_amount: str = "-12.50",
    debit: str = "32",
    credit: str = "10",
    date: str = "2024-01-15",
    currency: str = "EUR",
    family: str = "bank_fee",
    document_refs: list[dict[str, str]] | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "statement_id": statement_id,
        "record_id": "rec",
        "iban": "EE001234567890",
        "currency": currency,
        "period": date[:7],
        "date": date,
        "signed_amount": signed_amount,
        "counterparty": "",
        "description": "",
        "disposition": "bank_fee_payment",
        "family": family,
        "ui_action": "assign_general_ledger",
        "financial_accounts": {} if parts else {"debit": debit, "credit": credit},
        "financial_account_roles": {} if parts else {"debit": "bank_fees", "credit": "bank"},
        "document_refs": document_refs or [],
        "ecb": None,
        "parts": parts or [],
        "split_equation": "",
        "source": {"source_id": "s", "path": "camt.xml", "sha256": "a" * 64, "row_ref": "rec"},
        "status": "pending",
        "evidence": None,
    }


def plan(company_slug: str = "example", rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    resolved = rows if rows is not None else [plan_row()]
    return {
        "schema_version": "1.0",
        "company_slug": company_slug,
        "year": 2024,
        "cash_posting_mode": "statement_import",
        "rate_bindings": [],
        "coverage": {
            "physical_row_count": len(resolved),
            "planned_row_count": len(resolved),
            "uncovered_count": 0,
            "extra_count": 0,
            "families": {},
            "movement": {},
        },
        "rows": resolved,
    }


class LedgerExportTrustBoundaryTests(unittest.TestCase):
    def test_evidence_requires_a_hash_bound_export(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binding = written_export(root, export_text(FEE_ROWS))
            binding["sha256"] = "0" * 64

            with self.assertRaisesRegex(ledger_export_evidence.LedgerEvidenceError, "SHA"):
                ledger_export_evidence.load_ledger_export(binding, cwd=root)

    def test_a_missing_export_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            with self.assertRaisesRegex(ledger_export_evidence.LedgerEvidenceError, "missing"):
                ledger_export_evidence.load_ledger_export({"path": "absent.csv", "sha256": "a" * 64}, cwd=root)

    def test_an_export_missing_required_columns_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binding = written_export(root, export_text(["42,2024,32,C32,t1,2024-01-15,EUR,12.50"], header="company_id,period,account_id,account_code,transaction_id,business_date,currency,debit"))

            with self.assertRaisesRegex(ledger_export_evidence.LedgerEvidenceError, "column"):
                ledger_export_evidence.load_ledger_export(binding, cwd=root)

    def test_a_locale_ambiguous_amount_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binding = written_export(root, export_text([posting(transaction_id="t1", account_id="32", debit="1.234,56")]))

            with self.assertRaisesRegex(ledger_export_evidence.LedgerEvidenceError, "amount"):
                ledger_export_evidence.load_ledger_export(binding, cwd=root)

    def test_a_malformed_date_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binding = written_export(
                root, export_text([posting(transaction_id="t1", account_id="32", debit="12.50", business_date="15/01/2024")])
            )

            with self.assertRaisesRegex(ledger_export_evidence.LedgerEvidenceError, "date"):
                ledger_export_evidence.load_ledger_export(binding, cwd=root)

    def test_a_missing_currency_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binding = written_export(
                root, export_text([posting(transaction_id="t1", account_id="32", debit="12.50", currency="")])
            )

            with self.assertRaisesRegex(ledger_export_evidence.LedgerEvidenceError, "currency"):
                ledger_export_evidence.load_ledger_export(binding, cwd=root)

    def test_a_repeated_transaction_and_account_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rows = [posting(transaction_id="t1", account_id="32", debit="12.50")] * 2
            binding = written_export(root, export_text(rows))

            with self.assertRaisesRegex(ledger_export_evidence.LedgerEvidenceError, "duplicate"):
                ledger_export_evidence.load_ledger_export(binding, cwd=root)

    def test_a_row_that_is_both_debit_and_credit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binding = written_export(
                root, export_text([posting(transaction_id="t1", account_id="32", debit="12.50", credit="12.50")])
            )

            with self.assertRaisesRegex(ledger_export_evidence.LedgerEvidenceError, "debit"):
                ledger_export_evidence.load_ledger_export(binding, cwd=root)

    def test_a_valid_export_loads_its_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            export = ledger_export_evidence.load_ledger_export(
                written_export(root, export_text(FEE_ROWS)), cwd=root
            )

            self.assertEqual(len(export["rows"]), 2)
            self.assertEqual(export["company_ids"], {"42"})


class PlanToLedgerMatchingTests(unittest.TestCase):
    def load(self, root: Path, rows: list[str]) -> dict[str, Any]:
        return ledger_export_evidence.load_ledger_export(written_export(root, export_text(rows)), cwd=root)

    def test_a_matching_export_reports_no_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            self.assertEqual(
                ledger_export_evidence.match_plan_rows(plan(), self.load(root, FEE_ROWS), company_id="42"), []
            )

    def test_a_different_company_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            export = self.load(root, FEE_ROWS)

            with self.assertRaisesRegex(ledger_export_evidence.LedgerEvidenceError, "company"):
                ledger_export_evidence.match_plan_rows(plan(), export, company_id="99")

    def test_a_wrong_ledger_amount_is_reported(self) -> None:
        rows = [
            posting(transaction_id="t1", account_id="32", debit="12.05"),
            posting(transaction_id="t1", account_id="10", credit="12.05"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            errors = ledger_export_evidence.match_plan_rows(plan(), self.load(root, rows), company_id="42")

            self.assertTrue(any("no ledger posting" in error for error in errors))

    def test_a_duplicated_posting_is_reported(self) -> None:
        rows = [
            *FEE_ROWS,
            posting(transaction_id="t2", account_id="32", debit="12.50"),
            posting(transaction_id="t2", account_id="10", credit="12.50"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            errors = ledger_export_evidence.match_plan_rows(plan(), self.load(root, rows), company_id="42")

            self.assertTrue(any("Unexplained" in error for error in errors))

    def test_a_wrong_account_is_reported(self) -> None:
        rows = [
            posting(transaction_id="t1", account_id="33", debit="12.50"),
            posting(transaction_id="t1", account_id="10", credit="12.50"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            errors = ledger_export_evidence.match_plan_rows(plan(), self.load(root, rows), company_id="42")

            self.assertTrue(any("account 32" in error for error in errors))

    def test_a_wrong_business_date_is_reported(self) -> None:
        rows = [
            posting(transaction_id="t1", account_id="32", debit="12.50", business_date="2024-01-16"),
            posting(transaction_id="t1", account_id="10", credit="12.50", business_date="2024-01-16"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            errors = ledger_export_evidence.match_plan_rows(plan(), self.load(root, rows), company_id="42")

            self.assertTrue(any("no ledger posting" in error for error in errors))

    def test_a_wrong_currency_is_reported(self) -> None:
        rows = [
            posting(transaction_id="t1", account_id="32", debit="12.50", currency="USD"),
            posting(transaction_id="t1", account_id="10", credit="12.50", currency="USD"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            errors = ledger_export_evidence.match_plan_rows(plan(), self.load(root, rows), company_id="42")

            self.assertTrue(any("no ledger posting" in error for error in errors))

    def test_each_split_part_is_matched_separately(self) -> None:
        parts = [
            {
                "part_number": 1, "signed_amount": "-10.00", "disposition": "bank_fee_payment",
                "family": "bank_fee", "financial_accounts": {"debit": "32", "credit": "10"},
                "financial_account_roles": {"debit": "bank_fees", "credit": "bank"}, "document_refs": [],
            },
            {
                "part_number": 2, "signed_amount": "-90.00", "disposition": "existing_purchase_payment",
                "family": "document_settlement", "financial_accounts": {"debit": "38", "credit": "10"},
                "financial_account_roles": {"debit": "supplier_payable", "credit": "bank"},
                "document_refs": [{"document_type": "purchase", "simplbooks_id": "77"}],
            },
        ]
        rows = [
            posting(transaction_id="s1", account_id="32", debit="10.00"),
            posting(transaction_id="s1", account_id="10", credit="10.00"),
            posting(transaction_id="s2", account_id="38", debit="90.00"),
            posting(transaction_id="s2", account_id="10", credit="90.00"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan_with_split = plan(
                rows=[plan_row(signed_amount="-100.00", family="reviewed_split", parts=parts)]
            )

            self.assertEqual(
                ledger_export_evidence.match_plan_rows(plan_with_split, self.load(root, rows), company_id="42"), []
            )

    def test_a_split_part_missing_from_the_ledger_is_reported(self) -> None:
        parts = [
            {
                "part_number": 1, "signed_amount": "-10.00", "disposition": "bank_fee_payment",
                "family": "bank_fee", "financial_accounts": {"debit": "32", "credit": "10"},
                "financial_account_roles": {"debit": "bank_fees", "credit": "bank"}, "document_refs": [],
            },
            {
                "part_number": 2, "signed_amount": "-90.00", "disposition": "existing_purchase_payment",
                "family": "document_settlement", "financial_accounts": {"debit": "38", "credit": "10"},
                "financial_account_roles": {"debit": "supplier_payable", "credit": "bank"}, "document_refs": [],
            },
        ]
        rows = [
            posting(transaction_id="s1", account_id="32", debit="10.00"),
            posting(transaction_id="s1", account_id="10", credit="10.00"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan_with_split = plan(
                rows=[plan_row(signed_amount="-100.00", family="reviewed_split", parts=parts)]
            )

            errors = ledger_export_evidence.match_plan_rows(
                plan_with_split, self.load(root, rows), company_id="42"
            )

            self.assertTrue(any("part 2" in error for error in errors))


class AnnualEvidenceSummaryTests(unittest.TestCase):
    def test_summary_reports_movement_by_account_and_currency(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            export = ledger_export_evidence.load_ledger_export(
                written_export(root, export_text(FEE_ROWS)), cwd=root
            )

            summary = ledger_export_evidence.build_evidence_summary(
                plan(), export, company_id="42"
            )

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["movement"]["32|EUR"], "12.50")
            self.assertEqual(summary["movement"]["10|EUR"], "-12.50")
            self.assertEqual(summary["errors"], [])
            self.assertEqual(summary["binding"]["sha256"], export["sha256"])

    def test_summary_fails_when_a_posting_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            export = ledger_export_evidence.load_ledger_export(
                written_export(root, export_text(FEE_ROWS[:1])), cwd=root
            )

            summary = ledger_export_evidence.build_evidence_summary(plan(), export, company_id="42")

            self.assertEqual(summary["status"], "fail")
            self.assertNotEqual(summary["errors"], [])


class MultiLineFieldTests(unittest.TestCase):
    def test_a_quoted_newline_in_a_description_does_not_split_the_row(self) -> None:
        rows = [
            '42,2024,32,C32,t1,2024-01-15,EUR,12.50,0.00,"Fee\nsecond line",',
            "42,2024,10,C10,t1,2024-01-15,EUR,0.00,12.50,Fee,",
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            export = ledger_export_evidence.load_ledger_export(
                written_export(root, export_text(rows)), cwd=root
            )

            self.assertEqual(len(export["rows"]), 2)
            # The embedded newline survives rather than being silently swallowed.
            self.assertEqual(export["rows"][0]["description"], "Fee\nsecond line")


if __name__ == "__main__":
    unittest.main()
