from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import statement_import_evidence  # noqa: E402


def evidence(*, amount: float = -7.0, transaction_id: str = "txn-501") -> dict:
    return {
        "schema_version": "1.0", "company_slug": "example", "company_id": "99",
        "period": "2024-01", "statement_id": "archive:fee-1", "record_id": "bank:fee-1",
        "transaction_date": "2024-01-15", "iban": "EE123", "currency": "EUR",
        "signed_amount": amount, "simplbooks_transaction_id": transaction_id,
        "evidence_kind": "simplbooks_discovery", "captured_at": "2026-08-22T10:00:00Z",
        "source_identity": {
            "path": "normalized.json", "sha256": "a" * 64, "record_ref": "bank:fee-1",
        },
        "evidence_source": {
            "path": "ui-export.json", "sha256": "b" * 64, "record_ref": transaction_id,
        },
    }


class StatementImportEvidenceTests(unittest.TestCase):
    def test_unsupported_ui_evidence_cannot_be_verified(self) -> None:
        item = evidence()
        item["evidence_kind"] = "simplbooks_ui_export"

        with self.assertRaisesRegex(statement_import_evidence.StatementImportEvidenceError, "unsupported"):
            statement_import_evidence.validate_evidence_shape(item)

    def test_discovery_evidence_source_must_be_a_matching_cash_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized = root / "normalized.json"
            discovery = root / "discovery.json"
            normalized.write_text("{}\n", encoding="utf-8")
            discovery.write_text("{}\n", encoding="utf-8")
            item = evidence()
            item["source_identity"].update({
                "path": str(normalized), "sha256": hashlib.sha256(normalized.read_bytes()).hexdigest(),
            })
            item["evidence_source"].update({
                "path": str(discovery), "sha256": hashlib.sha256(discovery.read_bytes()).hexdigest(),
            })
            path = root / "proof.json"
            path.write_text(json.dumps(item), encoding="utf-8")
            binding = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

            with self.assertRaisesRegex(statement_import_evidence.StatementImportEvidenceError, "discovery"):
                statement_import_evidence.load_bound_evidence(binding, cwd=root)

    def test_binding_rejects_nonexistent_and_wrong_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = {"path": str(root / "missing.json"), "sha256": "0" * 64}
            with self.assertRaisesRegex(statement_import_evidence.StatementImportEvidenceError, "does not exist"):
                statement_import_evidence.load_bound_evidence(binding, cwd=root)
            path = root / "proof.json"
            path.write_text(json.dumps(evidence()), encoding="utf-8")
            with self.assertRaisesRegex(statement_import_evidence.StatementImportEvidenceError, "SHA"):
                statement_import_evidence.load_bound_evidence(binding | {"path": str(path)}, cwd=root)

    def test_exact_dependency_comparison_rejects_wrong_economics_and_transaction(self) -> None:
        item = evidence(amount=-2.0, transaction_id="txn-wrong")
        dependency = {
            "statement_id": "archive:fee-1", "record_id": "bank:fee-1", "date": "2024-01-15",
            "iban": "EE123", "currency": "EUR", "physical_signed_amount": -7.0,
        }
        errors = statement_import_evidence.evidence_identity_errors(
            item, dependency=dependency, expected_company_id="99", expected_transaction_id="txn-501"
        )
        self.assertTrue(any("signed amount" in error for error in errors))
        self.assertTrue(any("transaction ID" in error for error in errors))
