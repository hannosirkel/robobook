from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bank_allocations  # noqa: E402


def bank_record(*, record_id: str = "bank-source:bank:2", archive_id: str = "2024010212345678", amount: float = 330.0) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "source_system": "bank",
        "event_date": "2024-01-02",
        "currency": "EUR",
        "gross_amount": amount,
        "description": "Invoice 119 settlement",
        "external_ref": archive_id,
        "attributes": {"customer_account": "EE123"},
    }


def normalized_payload(*records: dict[str, Any]) -> dict[str, Any]:
    return {"records": {"bank_transactions": list(records)}}


def allocation(*, statement_id: str, record_id: str, amount: float = 330.0, disposition: str = "existing_invoice_receipt", **overrides: Any) -> dict[str, Any]:
    result = {
        "statement_id": statement_id,
        "record_id": record_id,
        "period": "2024-01",
        "disposition": disposition,
        "amount": amount,
        "currency": "EUR",
        "target": {"simplbooks_id": "119", "document_type": "invoice"},
        "review": {"status": "approved", "rationale": "Exact invoice number and amount."},
    }
    result.update(overrides)
    return result


class BankAllocationTests(unittest.TestCase):
    def test_statement_identity_prefers_archive_then_account_servicer_then_entry_reference(self) -> None:
        record = bank_record()
        self.assertEqual(bank_allocations.statement_identity(record), "archive:2024010212345678")
        record["external_ref"] = None
        record["attributes"] = {"account_servicer_reference": "ASR-1", "entry_reference": "ENTRY-1"}
        self.assertEqual(bank_allocations.statement_identity(record), "account-servicer:ASR-1")
        record["attributes"] = {"entry_reference": "ENTRY-1"}
        self.assertEqual(bank_allocations.statement_identity(record), "entry:ENTRY-1")

    def test_statement_identity_uses_deterministic_economic_composite_as_last_resort(self) -> None:
        record = bank_record(archive_id="")
        record["attributes"] = {"customer_account": "EE123", "counterparty_name": "Acme", "counterparty_account": "EE999"}
        record["external_ref"] = None
        self.assertEqual(
            bank_allocations.statement_identity(record),
            "composite:EE123|EUR|2024-01-02|330.00|Acme|Invoice 119 settlement",
        )

    def test_load_requires_bound_current_statement_identity_and_approved_review(self) -> None:
        record = bank_record()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized_path = root / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized_payload(record)), encoding="utf-8")
            allocation_path = root / "allocations.json"
            allocation_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "company_slug": "example",
                        "year": 2024,
                        "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                        "allocations": [allocation(statement_id="archive:2024010212345678", record_id="bank-source:bank:2")],
                    }
                ),
                encoding="utf-8",
            )
            loaded = bank_allocations.load_bank_allocations(allocation_path, normalized_year_paths=[normalized_path])

        self.assertEqual(loaded["allocations"][0]["statement_id"], "archive:2024010212345678")

    def test_load_rejects_changed_economic_identity_despite_matching_statement_id(self) -> None:
        record = bank_record(amount=331.0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized_path = root / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized_payload(record)), encoding="utf-8")
            allocation_path = root / "allocations.json"
            allocation_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "company_slug": "example",
                        "year": 2024,
                        "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                        "allocations": [allocation(statement_id="archive:2024010212345678", record_id="bank-source:bank:2")],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(bank_allocations.BankAllocationError):
                bank_allocations.load_bank_allocations(allocation_path, normalized_year_paths=[normalized_path])

    def test_rebind_refreshes_hash_and_record_locator_only_when_statement_economics_match(self) -> None:
        old_record = bank_record(record_id="bank-source:bank:2")
        refreshed_record = bank_record(record_id="bank-source:bank:9")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_path = root / "old.json"
            old_path.write_text(json.dumps(normalized_payload(old_record)), encoding="utf-8")
            refreshed_path = root / "refreshed.json"
            refreshed_path.write_text(json.dumps(normalized_payload(refreshed_record)), encoding="utf-8")
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(old_path), "sha256": hashlib.sha256(old_path.read_bytes()).hexdigest()}],
                "allocations": [allocation(statement_id="archive:2024010212345678", record_id="bank-source:bank:2")],
            }
            rebound = bank_allocations.rebind_bank_allocations(payload, normalized_year_paths=[refreshed_path])

        self.assertEqual(rebound["allocations"][0]["record_id"], "bank-source:bank:9")
        self.assertEqual(rebound["allocations"][0]["review"]["status"], "needs_review")

    def test_rebind_rejects_changed_statement_economics(self) -> None:
        old_record = bank_record()
        changed_record = bank_record(amount=331.0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_path = root / "old.json"
            old_path.write_text(json.dumps(normalized_payload(old_record)), encoding="utf-8")
            changed_path = root / "changed.json"
            changed_path.write_text(json.dumps(normalized_payload(changed_record)), encoding="utf-8")
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(old_path), "sha256": hashlib.sha256(old_path.read_bytes()).hexdigest()}],
                "allocations": [allocation(statement_id="archive:2024010212345678", record_id="bank-source:bank:2")],
            }
            with self.assertRaises(bank_allocations.BankAllocationError):
                bank_allocations.rebind_bank_allocations(payload, normalized_year_paths=[changed_path])

    def test_reviewed_split_requires_cent_exact_nonempty_parts(self) -> None:
        reviewed_split = allocation(
            statement_id="archive:2024010212345678",
            record_id="bank-source:bank:2",
            disposition="reviewed_split",
            parts=[{"amount": 100.0}, {"amount": 230.0}],
        )
        self.assertEqual(bank_allocations.allocation_amounts(reviewed_split), [Decimal("100.0"), Decimal("230.0")])
        reviewed_split["parts"] = [{"amount": 100.0}, {"amount": 229.99}]
        with self.assertRaises(bank_allocations.BankAllocationError):
            bank_allocations.validate_reviewed_amounts([reviewed_split])

    def test_period_allocations_indexes_each_statement_once(self) -> None:
        payload = {
            "allocations": [
                allocation(statement_id="archive:one", record_id="bank-source:bank:2"),
                allocation(statement_id="archive:two", record_id="bank-source:bank:3", period="2024-02"),
            ]
        }
        self.assertEqual(set(bank_allocations.period_allocations(payload, "2024-01")), {"archive:one"})


if __name__ == "__main__":
    unittest.main()
