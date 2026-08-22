from __future__ import annotations  # noqa: I001

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta  # noqa: F401
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import reference_artifacts  # noqa: E402


class ReferenceArtifactTests(unittest.TestCase):
    def test_discovery_must_be_fresh_and_match_company(self) -> None:
        now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        good = {"year": 2024, "company_id": "CID", "retrieved_at": "2026-08-21T11:50:00Z"}
        reference_artifacts.validate_discovery(good, year=2024, company_id="CID", now=now)

        for payload in (
            {**good, "company_id": "OTHER"},
            {**good, "retrieved_at": "2026-08-21T10:00:00Z"},
            {k: v for k, v in good.items() if k != "retrieved_at"},
        ):
            with self.subTest(payload=payload), self.assertRaises(reference_artifacts.ReferenceArtifactError):
                reference_artifacts.validate_discovery(payload, year=2024, company_id="CID", now=now)

    def test_binding_detects_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posting-policy.json"
            path.write_text("one", encoding="utf-8")
            binding = reference_artifacts.bind_file(path, kind="posting_policy", cwd=Path(tmp))
            path.write_text("two", encoding="utf-8")

            with self.assertRaises(reference_artifacts.ReferenceArtifactError):
                reference_artifacts.verify_file_binding(binding, cwd=Path(tmp))

    def test_physical_bank_source_metadata_requires_bank_allocation_binding(self) -> None:
        false_positive = {
            "actions": [
                {
                    "source_refs": [
                        {"path": "normalized.json", "record_ref": "bank-source:bank:2"}
                    ]
                }
            ]
        }
        physical_bank = {
            "actions": [
                {
                    "source_refs": [
                        {"path": "normalized.json", "record_ref": "receipt-1", "source_kind": "physical_bank"}
                    ]
                }
            ]
        }
        self.assertFalse(reference_artifacts.requires_bank_allocation_binding(false_positive))
        self.assertTrue(reference_artifacts.requires_bank_allocation_binding(physical_bank))
        self.assertIn("bank_allocations", reference_artifacts.required_action_binding_kinds(physical_bank))

    def test_manual_statement_import_dependency_requires_bank_allocation_binding(self) -> None:
        batch = {
            "actions": [],
            "unresolved_dependencies": [{
                "kind": "manual_statement_import_financial_transaction",
                "source_ref": {"source_kind": "physical_bank"},
            }],
        }

        self.assertTrue(reference_artifacts.requires_bank_allocation_binding(batch))
        self.assertIn("bank_allocations", reference_artifacts.required_action_binding_kinds(batch))


if __name__ == "__main__":
    unittest.main()
