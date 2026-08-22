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


def normalized_payload(*records: dict[str, Any], period: str = "2024-01") -> dict[str, Any]:
    return {"period": period, "records": {"bank_transactions": list(records)}}


def allocation(*, statement_id: str, record_id: str, amount: float = 330.0, disposition: str = "existing_invoice_receipt", **overrides: Any) -> dict[str, Any]:
    result = {
        "statement_id": statement_id,
        "record_id": record_id,
        "iban": "EE123",
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
    def test_bank_ledger_key_normalizes_physical_bank_iban_and_currency(self) -> None:
        record = bank_record()
        record["currency"] = "eur"
        record["attributes"] = {"customer_account": " ee 123 "}

        self.assertEqual(bank_allocations.bank_ledger_key(record), ("EE123", "EUR"))

    def test_bank_ledger_key_rejects_nonphysical_or_unidentified_rows(self) -> None:
        record = bank_record()
        record["source_system"] = "printful"
        with self.assertRaises(bank_allocations.BankAllocationError):
            bank_allocations.bank_ledger_key(record)

    def test_loader_accepts_only_four_digit_years(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for year in (1000, 9999):
                with self.subTest(year=year):
                    record = bank_record()
                    record["event_date"] = f"{year}-01-02"
                    normalized_path = root / f"{year}-01.json"
                    normalized_path.write_text(
                        json.dumps(normalized_payload(record, period=f"{year}-01")), encoding="utf-8"
                    )
                    payload = {
                        "schema_version": "1.0",
                        "company_slug": "example",
                        "year": year,
                        "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                        "allocations": [
                            allocation(
                                statement_id="archive:2024010212345678",
                                record_id="bank-source:bank:2",
                                period=f"{year}-01",
                            )
                        ],
                    }
                    allocation_path = root / f"{year}-allocations.json"
                    allocation_path.write_text(json.dumps(payload), encoding="utf-8")
                    bank_allocations.load_bank_allocations(allocation_path, normalized_year_paths=[normalized_path])
            for year in (999, 10000):
                with self.subTest(year=year):
                    payload = {
                        "schema_version": "1.0",
                        "company_slug": "example",
                        "year": year,
                        "normalized_bindings": [{"path": str(root / "unused.json"), "sha256": "a" * 64}],
                        "allocations": [],
                    }
                    allocation_path = root / f"{year}-allocations.json"
                    allocation_path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(bank_allocations.BankAllocationError):
                        bank_allocations.load_bank_allocations(allocation_path, normalized_year_paths=[])
        record = bank_record()
        record["attributes"] = {}
        with self.assertRaises(bank_allocations.BankAllocationError):
            bank_allocations.bank_ledger_key(record)

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

    def test_loader_rejects_incomplete_reviewed_statement_import_proof(self) -> None:
        record = bank_record()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized_path = root / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized_payload(record)), encoding="utf-8")
            item = allocation(statement_id="archive:2024010212345678", record_id=record["record_id"])
            item["target"]["statement_import_proof"] = {
                "status": "verified",
                "required_evidence": "live_discovery_or_audit",
            }
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{
                    "path": str(normalized_path),
                    "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest(),
                }],
                "allocations": [item],
            }
            allocation_path = root / "allocations.json"
            allocation_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(bank_allocations.BankAllocationError, "statement_import_proof"):
                bank_allocations.load_bank_allocations(
                    allocation_path, normalized_year_paths=[normalized_path]
                )

    def test_same_archive_rows_in_distinct_currencies_load_and_prove_complete(self) -> None:
        eur = bank_record(record_id="bank-source:bank:eur", archive_id="transfer-1", amount=330.0)
        usd = bank_record(record_id="bank-source:bank:usd", archive_id="transfer-1", amount=-2.0)
        usd["currency"] = "USD"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized_path = root / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized_payload(eur, usd)), encoding="utf-8")
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                "allocations": [
                    allocation(statement_id="archive:transfer-1", record_id="bank-source:bank:eur", amount=330.0),
                    allocation(statement_id="archive:transfer-1", record_id="bank-source:bank:usd", amount=-2.0, currency="USD"),
                ],
            }
            allocation_path = root / "allocations.json"
            allocation_path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = bank_allocations.load_bank_allocations(allocation_path, normalized_year_paths=[normalized_path])
            bank_allocations.prove_exact_bank_allocation_coverage(loaded, normalized_year_paths=[normalized_path])

        self.assertEqual(
            set(bank_allocations.period_allocations(loaded, "2024-01")),
            {("archive:transfer-1", "EE123", "EUR"), ("archive:transfer-1", "EE123", "USD")},
        )

    def test_duplicate_full_allocation_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(bank_allocations.BankAllocationError, "allocation key is duplicated"):
            bank_allocations.validate_unique_record_ids([
                allocation(statement_id="archive:transfer-1", record_id="bank-source:bank:eur"),
                allocation(statement_id="archive:transfer-1", record_id="bank-source:bank:replacement"),
            ])

    def test_wrong_allocation_iban_is_rejected_by_loading_and_coverage(self) -> None:
        record = bank_record(archive_id="transfer-1")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized_path = root / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized_payload(record)), encoding="utf-8")
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                "allocations": [allocation(statement_id="archive:transfer-1", record_id="bank-source:bank:2", iban="EE999")],
            }
            allocation_path = root / "allocations.json"
            allocation_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(bank_allocations.BankAllocationError, "EE999"):
                bank_allocations.load_bank_allocations(allocation_path, normalized_year_paths=[normalized_path])
            with self.assertRaisesRegex(bank_allocations.BankAllocationError, "missing bank allocation"):
                bank_allocations.prove_exact_bank_allocation_coverage(payload, normalized_year_paths=[normalized_path])

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

    def test_loader_allows_partial_phase_a_artifact_but_completeness_proof_reports_missing_ids(self) -> None:
        first = bank_record(record_id="bank-source:bank:2", archive_id="one")
        second = bank_record(record_id="bank-source:bank:3", archive_id="two")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized_path = root / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized_payload(first, second)), encoding="utf-8")
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                "allocations": [allocation(statement_id="archive:one", record_id="bank-source:bank:2")],
            }
            allocation_path = root / "allocations.json"
            allocation_path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = bank_allocations.load_bank_allocations(allocation_path, normalized_year_paths=[normalized_path])

            self.assertEqual(
                bank_allocations.bank_allocation_coverage_errors(loaded, normalized_year_paths=[normalized_path]),
                ["missing bank allocation key(s): ('archive:two', 'EE123', 'EUR')"],
            )
            with self.assertRaisesRegex(bank_allocations.BankAllocationError, "missing bank allocation key"):
                bank_allocations.prove_exact_bank_allocation_coverage(loaded, normalized_year_paths=[normalized_path])

    def test_completeness_proof_reports_duplicate_and_extra_ids_deterministically(self) -> None:
        record = bank_record(archive_id="one")
        with tempfile.TemporaryDirectory() as tmp:
            normalized_path = Path(tmp) / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized_payload(record)), encoding="utf-8")
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                "allocations": [
                    allocation(statement_id="archive:one", record_id="bank-source:bank:2"),
                    allocation(statement_id="archive:one", record_id="bank-source:bank:3"),
                    allocation(statement_id="archive:extra", record_id="bank-source:bank:4"),
                ],
            }
            self.assertEqual(
                bank_allocations.bank_allocation_coverage_errors(payload, normalized_year_paths=[normalized_path]),
                [
                    "duplicate bank allocation key(s): ('archive:one', 'EE123', 'EUR')",
                    "extra bank allocation key(s): ('archive:extra', 'EE123', 'EUR')",
                ],
            )

    def test_nonbank_rows_do_not_enter_allocation_or_completeness_proof(self) -> None:
        physical = bank_record(archive_id="physical")
        wallet = bank_record(record_id="printful:wallet:2", archive_id="wallet")
        wallet["source_system"] = "BANK"
        with tempfile.TemporaryDirectory() as tmp:
            normalized_path = Path(tmp) / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized_payload(physical, wallet)), encoding="utf-8")
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                "allocations": [allocation(statement_id="archive:physical", record_id="bank-source:bank:2")],
            }
            self.assertEqual(bank_allocations.bank_allocation_coverage_errors(payload, normalized_year_paths=[normalized_path]), [])
            payload["allocations"] = [allocation(statement_id="archive:wallet", record_id="printful:wallet:2")]
            allocation_path = Path(tmp) / "allocations.json"
            allocation_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(bank_allocations.BankAllocationError):
                bank_allocations.load_bank_allocations(allocation_path, normalized_year_paths=[normalized_path])

    def test_loader_rejects_allocation_or_row_outside_artifact_year(self) -> None:
        record = bank_record()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized_path = root / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized_payload(record)), encoding="utf-8")
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                "allocations": [allocation(statement_id="archive:2024010212345678", record_id="bank-source:bank:2", period="2023-12")],
            }
            allocation_path = root / "allocations.json"
            allocation_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(bank_allocations.BankAllocationError):
                bank_allocations.load_bank_allocations(allocation_path, normalized_year_paths=[normalized_path])

            normalized_path.write_text(json.dumps(normalized_payload(record, period="2023-12")), encoding="utf-8")
            payload["allocations"] = [allocation(statement_id="archive:2024010212345678", record_id="bank-source:bank:2")]
            payload["normalized_bindings"][0]["sha256"] = hashlib.sha256(normalized_path.read_bytes()).hexdigest()
            allocation_path.write_text(json.dumps(payload), encoding="utf-8")
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

    def test_rebind_retains_same_archive_rows_for_each_currency(self) -> None:
        old_eur = bank_record(record_id="bank-source:bank:eur", archive_id="transfer-1", amount=330.0)
        old_usd = bank_record(record_id="bank-source:bank:usd", archive_id="transfer-1", amount=-2.0)
        old_usd["currency"] = "USD"
        refreshed_eur = bank_record(record_id="bank-source:bank:eur:9", archive_id="transfer-1", amount=330.0)
        refreshed_usd = bank_record(record_id="bank-source:bank:usd:9", archive_id="transfer-1", amount=-2.0)
        refreshed_usd["currency"] = "USD"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_path = root / "old.json"
            old_path.write_text(json.dumps(normalized_payload(old_eur, old_usd)), encoding="utf-8")
            refreshed_path = root / "refreshed.json"
            refreshed_path.write_text(json.dumps(normalized_payload(refreshed_eur, refreshed_usd)), encoding="utf-8")
            payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(old_path), "sha256": hashlib.sha256(old_path.read_bytes()).hexdigest()}],
                "allocations": [
                    allocation(statement_id="archive:transfer-1", record_id="bank-source:bank:eur", amount=330.0),
                    allocation(statement_id="archive:transfer-1", record_id="bank-source:bank:usd", amount=-2.0, currency="USD"),
                ],
            }

            rebound = bank_allocations.rebind_bank_allocations(payload, normalized_year_paths=[refreshed_path])

        self.assertEqual(
            {item["currency"]: item["record_id"] for item in rebound["allocations"]},
            {"EUR": "bank-source:bank:eur:9", "USD": "bank-source:bank:usd:9"},
        )
        self.assertTrue(all(item["review"]["status"] == "needs_review" for item in rebound["allocations"]))

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
        self.assertEqual(set(bank_allocations.period_allocations(payload, "2024-01")), {("archive:one", "EE123", "EUR")})


if __name__ == "__main__":
    unittest.main()
