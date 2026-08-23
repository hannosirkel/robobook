from __future__ import annotations  # noqa: I001

import hashlib
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookrecon  # noqa: E402


def base_normalized(period: str = "2024-01") -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "period": period,
        "base_currency": "EUR",
        "generated_at": "2026-04-04T00:00:00Z",
        "sources": [],
        "records": {category: [] for category in bookrecon.RECORD_CATEGORIES},
        "exceptions": [],
    }


def record(
    *,
    record_id: str,
    source_system: str,
    event_type: str,
    gross_amount: float,
    net_amount: float | None = None,
    description: str = "record",
    channel: str | None = None,
    currency: str = "EUR",
    quantity: float | None = None,
    attributes: dict | None = None,
) -> dict:
    return {
        "record_id": record_id,
        "source_system": source_system,
        "source_type": "csv",
        "event_type": event_type,
        "event_date": "2024-01-15",
        "settlement_date": "2024-01-15",
        "description": description,
        "external_ref": None,
        "currency": currency,
        "gross_amount": gross_amount,
        "net_amount": gross_amount if net_amount is None else net_amount,
        "vat_amount": 0.0,
        "fee_amount": 0.0,
        "shipping_amount": 0.0,
        "quantity": quantity,
        "sku": None,
        "warehouse_id": None,
        "channel": channel,
        "country_code": None,
        "attributes": attributes or {},
        "source_refs": [{"source_id": "src-1", "path": "companies/example/source/file.csv", "row_ref": "csv:2", "page_ref": None, "notes": None}],
    }


def find_check(document: dict, check_id: str) -> dict:
    for check in document["checks"]:
        if check["check_id"] == check_id:
            return check
    raise AssertionError(f"Missing check {check_id}")


def bank_row(*, record_id: str, amount: float, iban: str = "EE123", currency: str = "EUR") -> dict:
    return record(
        record_id=record_id,
        source_system="bank",
        event_type="bank_credit" if amount >= 0 else "bank_debit",
        gross_amount=amount,
        currency=currency,
        description=f"Bank movement {record_id}",
        attributes={"iban": iban, "archive_identifier": record_id},
    )


def allocation(*, statement_id: str, record_id: str, amount: float, currency: str = "EUR", **overrides: object) -> dict:
    result: dict = {
        "statement_id": statement_id,
        "record_id": record_id,
        "iban": "EE123",
        "period": "2024-01",
        "disposition": "existing_invoice_receipt",
        "amount": amount,
        "currency": currency,
        "target": {"simplbooks_id": "119", "document_type": "invoice"},
        "review": {"status": "approved", "rationale": "Reviewed against the statement row."},
    }
    result.update(overrides)
    return result


class BookreconTests(unittest.TestCase):
    def test_default_bank_allocation_path_is_year_specific_under_artifacts_bank(self) -> None:
        normalized_path = Path("companies/example/artifacts/normalized/2024-01.json")

        path = bookrecon.resolve_bank_allocations_path(
            company_dir=None,
            normalized_path=normalized_path,
            period="2024-01",
            override=None,
        )

        self.assertEqual(path, Path("companies/example/artifacts/bank/2024-allocations.json"))

    def test_missing_physical_bank_allocation_warns_without_changing_legacy_build_approval(self) -> None:
        normalized = base_normalized()
        normalized["records"]["bank_transactions"] = [
            bank_row(record_id="a", amount=20.0),
            bank_row(record_id="b", amount=-2.0),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
                bank_allocations={"archive:a": allocation(statement_id="archive:a", record_id="a", amount=20.0)},
            )

        check = find_check(document, "physical-bank-coverage")
        self.assertEqual(check["status"], "warn")
        self.assertTrue(any("archive:b" in note for note in check["notes"]))
        self.assertFalse(document["bank_coverage"]["coverage_ready"])
        self.assertTrue(document["approve_for_build"])

    def test_whitespace_padded_bank_source_system_is_malformed_not_physical_bank(self) -> None:
        malformed = bank_row(record_id="padded", amount=20.0)
        malformed["source_system"] = " bank "

        check, coverage = bookrecon.build_physical_bank_coverage_check(
            normalized_path_display="normalized/2024-01.json",
            target_period="2024-01",
            bank_records=[malformed],
            allocations={},
        )

        self.assertEqual(check["status"], "warn")
        self.assertFalse(coverage["coverage_ready"])
        self.assertEqual(coverage["physical_bank_row_count"], 0)
        self.assertTrue(any("' bank '" in note for note in check["notes"]))
        self.assertEqual(check["evidence_refs"][0]["record_refs"], ["padded"])

    def test_duplicate_reviewed_allocation_is_report_only_warning(self) -> None:
        normalized = base_normalized()
        normalized["records"]["bank_transactions"] = [bank_row(record_id="a", amount=20.0)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normalized_path = root / "2024-01.json"
            normalized_path.write_text(json.dumps(normalized), encoding="utf-8")
            allocation_path = root / "2024-allocations.json"
            allocation_payload = {
                "schema_version": "1.0",
                "company_slug": "example",
                "year": 2024,
                "normalized_bindings": [{"path": str(normalized_path), "sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest()}],
                "allocations": [
                    allocation(statement_id="archive:a", record_id="a", amount=20.0),
                    allocation(statement_id="archive:a", record_id="replacement", amount=20.0),
                ],
            }
            allocation_path.write_text(json.dumps(allocation_payload), encoding="utf-8")
            allocations, allocation_errors = bookrecon.load_period_bank_allocations(
                allocation_path=allocation_path,
                normalized_path=normalized_path,
                period="2024-01",
            )
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=normalized_path,
                repo_root=root,
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
                bank_allocations=allocations,
                bank_allocation_errors=allocation_errors,
            )

        check = find_check(document, "physical-bank-coverage")
        self.assertEqual(check["status"], "warn")
        self.assertTrue(any("duplicated" in note for note in check["notes"]))
        self.assertFalse(document["bank_coverage"]["coverage_ready"])
        self.assertTrue(document["approve_for_build"])

    def test_stale_reviewed_allocation_is_report_only_warning(self) -> None:
        check, coverage = bookrecon.build_physical_bank_coverage_check(
            normalized_path_display="normalized/2024-01.json",
            target_period="2024-01",
            bank_records=[bank_row(record_id="a", amount=20.0)],
            allocations={"archive:obsolete": allocation(statement_id="archive:obsolete", record_id="obsolete", amount=20.0)},
        )

        self.assertEqual(check["status"], "warn")
        self.assertTrue(any("stale" in note for note in check["notes"]))
        self.assertFalse(coverage["coverage_ready"])

    def test_exact_reviewed_split_passes_physical_bank_coverage(self) -> None:
        bank_records = [bank_row(record_id="a", amount=-30.0)]
        check, coverage = bookrecon.build_physical_bank_coverage_check(
            normalized_path_display="normalized/2024-01.json",
            target_period="2024-01",
            bank_records=bank_records,
            allocations={
                "archive:a": allocation(
                    statement_id="archive:a",
                    record_id="a",
                    amount=-30.0,
                    disposition="reviewed_split",
                    parts=[{"amount": -10.0}, {"amount": -20.0}],
                )
            },
        )

        self.assertEqual(check["status"], "pass")
        self.assertTrue(coverage["coverage_ready"])
        self.assertEqual(coverage["physical_bank_row_count"], 1)
        self.assertEqual(coverage["allocated_row_count"], 1)

    def test_same_archive_rows_in_each_currency_are_two_exact_physical_rows(self) -> None:
        bank_records = [
            bank_row(record_id="transfer-eur", amount=330.0, iban=" ee 123 ", currency="EUR"),
            bank_row(record_id="transfer-usd", amount=-2.0, iban="EE123", currency="USD"),
        ]
        for row in bank_records:
            row["attributes"]["archive_identifier"] = "transfer-1"
        check, coverage = bookrecon.build_physical_bank_coverage_check(
            normalized_path_display="normalized/2024-01.json",
            target_period="2024-01",
            bank_records=bank_records,
            allocations={
                ("archive:transfer-1", "EE123", "EUR"): allocation(
                    statement_id="archive:transfer-1", record_id="transfer-eur", amount=330.0, iban="EE123"
                ),
                ("archive:transfer-1", "EE123", "USD"): allocation(
                    statement_id="archive:transfer-1", record_id="transfer-usd", amount=-2.0, currency="USD", iban=" EE 123 "
                ),
            },
        )

        self.assertEqual(check["status"], "pass")
        self.assertTrue(coverage["coverage_ready"])
        self.assertEqual(coverage["physical_bank_row_count"], 2)
        self.assertEqual(coverage["allocated_row_count"], 2)
        self.assertEqual(coverage["unallocated_row_count"], 0)

    def test_physical_bank_coverage_separates_iban_currency_ledgers_and_proves_camt_balances(self) -> None:
        bank_records = [
            bank_row(record_id="eur", amount=20.0, iban="EE123", currency="EUR"),
            bank_row(record_id="usd", amount=-5.0, iban="EE123", currency="USD"),
        ]
        balances = [
            record(record_id="open-eur", source_system="bank", event_type="bank_balance", gross_amount=100.0, currency="EUR", attributes={"iban": "EE123", "balance_type": "OPBD", "statement_from": "2024-01-01", "statement_to": "2024-01-31"}),
            record(record_id="close-eur", source_system="bank", event_type="bank_balance", gross_amount=120.0, currency="EUR", attributes={"iban": "EE123", "balance_type": "CLBD", "statement_from": "2024-01-01", "statement_to": "2024-01-31"}),
            record(record_id="open-usd", source_system="bank", event_type="bank_balance", gross_amount=30.0, currency="USD", attributes={"iban": "EE123", "balance_type": "OPBD", "statement_from": "2024-01-01", "statement_to": "2024-01-31"}),
            record(record_id="close-usd", source_system="bank", event_type="bank_balance", gross_amount=25.0, currency="USD", attributes={"iban": "EE123", "balance_type": "CLBD", "statement_from": "2024-01-01", "statement_to": "2024-01-31"}),
        ]
        check, coverage = bookrecon.build_physical_bank_coverage_check(
            normalized_path_display="normalized/2024-01.json",
            target_period="2024-01",
            bank_records=bank_records,
            allocations={
                "archive:eur": allocation(statement_id="archive:eur", record_id="eur", amount=20.0),
                "archive:usd": allocation(statement_id="archive:usd", record_id="usd", amount=-5.0, currency="USD"),
            },
            bank_balance_records=balances,
        )

        self.assertEqual(check["status"], "pass")
        self.assertEqual(
            coverage["ledgers"],
            [
                {"iban": "EE123", "currency": "EUR", "physical_bank_row_count": 1, "allocated_row_count": 1, "unallocated_row_count": 0, "credit_total": 20.0, "debit_total": 0.0, "net_movement": 20.0, "camt_opening_balance": 100.0, "computed_closing_balance": 120.0, "camt_closing_balance": 120.0},
                {"iban": "EE123", "currency": "USD", "physical_bank_row_count": 1, "allocated_row_count": 1, "unallocated_row_count": 0, "credit_total": 0.0, "debit_total": -5.0, "net_movement": -5.0, "camt_opening_balance": 30.0, "computed_closing_balance": 25.0, "camt_closing_balance": 25.0},
            ],
        )

    def test_balance_only_camt_ledger_is_reported_with_zero_movement(self) -> None:
        balances = [
            record(record_id="open", source_system="bank", event_type="bank_balance", gross_amount=100.0, currency="EUR", attributes={"iban": "EE999", "balance_type": "OPBD"}),
            record(record_id="close", source_system="bank", event_type="bank_balance", gross_amount=100.0, currency="EUR", attributes={"iban": "EE999", "balance_type": "CLBD"}),
        ]

        check, coverage = bookrecon.build_physical_bank_coverage_check(
            normalized_path_display="normalized/2024-01.json",
            target_period="2024-01",
            bank_records=[],
            allocations={},
            bank_balance_records=balances,
        )

        self.assertEqual(check["status"], "pass")
        self.assertTrue(coverage["coverage_ready"])
        self.assertEqual(
            coverage["ledgers"],
            [{"iban": "EE999", "currency": "EUR", "physical_bank_row_count": 0, "allocated_row_count": 0, "unallocated_row_count": 0, "credit_total": 0.0, "debit_total": 0.0, "net_movement": 0.0, "camt_opening_balance": 100.0, "computed_closing_balance": 100.0, "camt_closing_balance": 100.0}],
        )

    def test_annual_camt_balances_are_deferred_from_monthly_continuity(self) -> None:
        balances = [
            record(record_id="annual-open", source_system="bank", event_type="bank_balance", gross_amount=100.0, attributes={"iban": "EE123", "balance_type": "OPBD", "statement_from": "2024-01-01", "statement_to": "2024-12-31"}),
            record(record_id="annual-close", source_system="bank", event_type="bank_balance", gross_amount=150.0, attributes={"iban": "EE123", "balance_type": "CLBD", "statement_from": "2024-01-01", "statement_to": "2024-12-31"}),
        ]

        normalized = base_normalized()
        normalized["records"]["bank_transactions"] = [bank_row(record_id="jan", amount=20.0)]
        normalized["records"]["bank_balances"] = balances
        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
                bank_allocations={"archive:jan": allocation(statement_id="archive:jan", record_id="jan", amount=20.0)},
            )

        check = find_check(document, "physical-bank-coverage")
        coverage = document["bank_coverage"]
        self.assertEqual(check["status"], "pass")
        self.assertTrue(coverage["coverage_ready"])
        ledger = coverage["ledgers"][0]
        self.assertIsNone(ledger["camt_opening_balance"])
        self.assertIsNone(ledger["computed_closing_balance"])
        self.assertIsNone(ledger["camt_closing_balance"])
        self.assertEqual(ledger["camt_evidence_scopes"][0]["statement_to"], "2024-12-31")
        self.assertTrue(any("2024-01-01 through 2024-12-31" in note for note in check["notes"]))

    def test_half_present_camt_scope_is_not_ready_evidence(self) -> None:
        check, coverage = bookrecon.build_physical_bank_coverage_check(
            normalized_path_display="normalized/2024-01.json",
            target_period="2024-01",
            bank_records=[],
            allocations={},
            bank_balance_records=[
                record(record_id="partial", source_system="bank", event_type="bank_balance", gross_amount=100.0, attributes={"iban": "EE123", "balance_type": "OPBD", "statement_from": "2024-01-01"}),
            ],
        )

        self.assertEqual(check["status"], "warn")
        self.assertFalse(coverage["coverage_ready"])
        self.assertTrue(any("incomplete statement scope" in note for note in check["notes"]))

    def test_month_scoped_balances_win_when_annual_evidence_coexists(self) -> None:
        balances = [
            record(record_id="annual-open", source_system="bank", event_type="bank_balance", gross_amount=100.0, attributes={"iban": "EE123", "balance_type": "OPBD", "statement_from": "2024-01-01", "statement_to": "2024-12-31"}),
            record(record_id="annual-close", source_system="bank", event_type="bank_balance", gross_amount=150.0, attributes={"iban": "EE123", "balance_type": "CLBD", "statement_from": "2024-01-01", "statement_to": "2024-12-31"}),
            record(record_id="month-open", source_system="bank", event_type="bank_balance", gross_amount=100.0, attributes={"iban": "EE123", "balance_type": "OPBD", "statement_from": "2024-01-01", "statement_to": "2024-01-31"}),
            record(record_id="month-close", source_system="bank", event_type="bank_balance", gross_amount=120.0, attributes={"iban": "EE123", "balance_type": "CLBD", "statement_from": "2024-01-01", "statement_to": "2024-01-31"}),
        ]

        check, coverage = bookrecon.build_physical_bank_coverage_check(
            normalized_path_display="normalized/2024-01.json",
            target_period="2024-01",
            bank_records=[bank_row(record_id="jan", amount=20.0)],
            allocations={"archive:jan": allocation(statement_id="archive:jan", record_id="jan", amount=20.0)},
            bank_balance_records=balances,
        )

        self.assertEqual(check["status"], "pass")
        self.assertTrue(coverage["coverage_ready"])
        self.assertEqual(coverage["ledgers"][0]["computed_closing_balance"], 120.0)

    def test_clearing_accounts_with_same_provider_currency_have_separate_checks(self) -> None:
        records = base_normalized()["records"]
        records["clearing_transactions"] = [
            record(record_id="wallet-one", source_system="printful", event_type="wallet", gross_amount=5.0, attributes={"clearing_provider": "printful", "clearing_account": "wallet:one", "opening_balance": 10.0, "closing_balance": 15.0}),
            record(record_id="wallet-two", source_system="printful", event_type="wallet", gross_amount=-3.0, attributes={"clearing_provider": "printful", "clearing_account": "wallet-two", "opening_balance": 20.0, "closing_balance": 17.0}),
        ]
        records["other"] = [
            record(record_id="bridge", source_system="printful", event_type="bridge", gross_amount=0.0, attributes={"clearing_record_ids": ["wallet-one", "wallet-two"]}),
        ]

        checks, ready, coverage = bookrecon.build_clearing_continuity_checks(
            normalized_path_display="normalized/2024-01.json",
            records=records,
            allocations={},
        )

        self.assertTrue(ready)
        self.assertEqual([check["check_id"] for check in checks], [
            "clearing-continuity:printful:wallet-two:eur",
            "clearing-continuity:printful:wallet%3Aone:eur",
        ])
        self.assertEqual([check["lhs_amount"] for check in checks], [-3.0, 5.0])
        self.assertTrue(all(check["status"] == "pass" for check in checks))
        self.assertEqual(coverage["clearing_movement_count"], 2)
        self.assertEqual(coverage["resolved_clearing_count"], 2)

    def test_unresolved_clearing_warns_without_changing_legacy_build_approval(self) -> None:
        normalized = base_normalized()
        normalized["records"]["clearing_transactions"] = [
            record(
                record_id="printful:wallet:1",
                source_system="printful",
                event_type="printful_wallet_deposit",
                gross_amount=-8.21,
                currency="EUR",
                attributes={"clearing_provider": "printful", "clearing_account": "printful_wallet"},
            )
        ]
        normalized["records"]["clearing_transactions"][0]["event_date"] = "2024-02-29"

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        check = find_check(document, "clearing-continuity:printful:printful_wallet:eur")
        self.assertEqual(check["status"], "warn")
        self.assertFalse(document["bank_coverage"]["coverage_ready"])
        self.assertTrue(document["approve_for_build"])
        self.assertEqual(document["bank_coverage"]["clearing_movement_record_ids"], ["printful:wallet:1"])
        self.assertEqual(document["bank_coverage"]["resolved_clearing_record_ids"], [])
        self.assertEqual(document["bank_coverage"]["unresolved_clearing_record_ids"], ["printful:wallet:1"])
        self.assertEqual(document["bank_coverage"]["clearing_movement_count"], 1)
        self.assertEqual(document["bank_coverage"]["unresolved_clearing_count"], 1)

    def test_cross_period_annual_allocation_resolves_current_clearing_movement(self) -> None:
        normalized = base_normalized(period="2024-02")
        normalized["records"]["clearing_transactions"] = [
            record(
                record_id="printful:wallet:feb29",
                source_system="printful",
                event_type="printful_wallet_deposit",
                gross_amount=-7.9,
                attributes={"clearing_provider": "printful", "clearing_account": "printful_wallet"},
            )
        ]
        normalized["records"]["clearing_transactions"][0]["event_date"] = "2024-02-29"
        march_allocation = allocation(
            statement_id="archive:march1",
            record_id="march1",
            amount=-7.9,
            period="2024-03",
            disposition="generated_purchase_payment",
            target={
                "document_type": "purchase",
                "action_key": "example-2024-02-purchase-printful",
                "clearing_record_ids": ["printful:wallet:feb29"],
                "bridge_record_ids": ["printful:wallet:feb29"],
                "bridge_direction": "same_as_physical",
                "clearing_evidence": [{
                    "record_id": "printful:wallet:feb29", "period": "2024-02",
                    "currency": "EUR", "amount": -7.9,
                    "provider": "printful", "account": "printful_wallet", "source_system": "printful",
                }],
                "clearing_totals": {"EUR": -7.9},
                "clearing_relation": "exact_amount",
                "bridge_amount": -7.9,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-02.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
                bank_allocations={},
                clearing_allocations={"archive:march1": march_allocation},
            )

        self.assertEqual(document["bank_coverage"]["resolved_clearing_record_ids"], ["printful:wallet:feb29"])
        self.assertEqual(document["bank_coverage"]["unresolved_clearing_count"], 0)

    def test_arbitrary_or_economically_wrong_clearing_hint_does_not_resolve_movement(self) -> None:
        normalized = base_normalized(period="2024-02")
        movement = record(
            record_id="printful:wallet:feb29", source_system="printful",
            event_type="printful_wallet_deposit", gross_amount=-7.9,
            attributes={"clearing_provider": "printful", "clearing_account": "printful_wallet"},
        )
        movement["event_date"] = "2024-02-29"
        normalized["records"]["clearing_transactions"] = [movement]
        reviewed = allocation(
            statement_id="archive:march1", record_id="march1", amount=-7.9,
            period="2024-03", disposition="generated_purchase_payment",
            target={"document_type": "purchase", "action_key": "purchase", "note": "printful:wallet:feb29"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            arbitrary = bookrecon.build_recon_document(
                normalized_payload=normalized, normalized_path=Path(tmp) / "2024-02.json",
                repo_root=Path(tmp), amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"), clearing_allocations={"a": reviewed},
            )
            reviewed["target"].update({
                "clearing_record_ids": ["printful:wallet:feb29"],
                "bridge_record_ids": ["printful:wallet:feb29"],
                "bridge_direction": "same_as_physical",
                "clearing_evidence": [{
                    "record_id": "printful:wallet:feb29", "period": "2024-03",
                    "currency": "EUR", "amount": -7.9,
                    "provider": "printful", "account": "printful_wallet", "source_system": "printful",
                }],
                "clearing_totals": {"EUR": -7.9}, "clearing_relation": "exact_amount",
                "bridge_amount": -7.9,
            })
            wrong_period = bookrecon.build_recon_document(
                normalized_payload=normalized, normalized_path=Path(tmp) / "2024-02.json",
                repo_root=Path(tmp), amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"), clearing_allocations={"a": reviewed},
            )

        self.assertEqual(arbitrary["bank_coverage"]["unresolved_clearing_count"], 1)
        self.assertEqual(wrong_period["bank_coverage"]["unresolved_clearing_count"], 1)

    def test_reviewed_group_requires_cent_exact_signed_bridge_leg_equation(self) -> None:
        eur = record(
            record_id="paypal:eur", source_system="paypal", event_type="paypal_card_deposit",
            gross_amount=13.27, attributes={"clearing_provider": "paypal", "clearing_account": "wallet"},
        )
        usd = record(
            record_id="paypal:unrelated-usd", source_system="paypal", event_type="paypal_conversion",
            gross_amount=999.0, currency="USD",
            attributes={"clearing_provider": "paypal", "clearing_account": "wallet"},
        )
        reviewed = allocation(
            statement_id="archive:debit", record_id="bank:debit", amount=-13.27,
            disposition="clearing_transfer",
            target={
                "document_type": "financial_transaction", "transaction_family": "failed_transfer_and_return",
                "clearing_record_ids": ["paypal:eur", "paypal:unrelated-usd"],
                "bridge_record_ids": ["paypal:unrelated-usd"], "bridge_direction": "opposite_physical",
                "clearing_evidence": [
                    {"record_id": "paypal:eur", "period": "2024-01", "currency": "EUR", "amount": 13.27,
                     "provider": "paypal", "account": "wallet", "source_system": "paypal"},
                    {"record_id": "paypal:unrelated-usd", "period": "2024-01", "currency": "USD", "amount": 999.0,
                     "provider": "paypal", "account": "wallet", "source_system": "paypal"},
                ],
                "clearing_totals": {"EUR": 13.27, "USD": 999.0},
                "clearing_relation": "reviewed_group", "bridge_amount": -13.27,
            },
        )

        resolved = bookrecon._validated_clearing_allocation_references(
            {"a": reviewed}, {"paypal:eur": eur, "paypal:unrelated-usd": usd},
            allocation_company_slug="example", normalized_company_slug="example",
        )

        self.assertEqual(resolved, set())

    def test_reviewed_group_rejects_unexplained_claim_outside_valid_bridge(self) -> None:
        eur = record(
            record_id="wallet:bridge", source_system="wallet", event_type="deposit",
            gross_amount=13.27, attributes={"clearing_provider": "wallet", "clearing_account": "wallet"},
        )
        unrelated = record(
            record_id="wallet:unrelated", source_system="wallet", event_type="conversion",
            gross_amount=999.0, currency="USD",
            attributes={"clearing_provider": "wallet", "clearing_account": "wallet"},
        )
        reviewed = allocation(
            statement_id="archive:debit", record_id="bank:debit", amount=-13.27,
            disposition="clearing_transfer",
            target={
                "document_type": "financial_transaction", "transaction_family": "internal_transfer",
                "clearing_record_ids": ["wallet:bridge", "wallet:unrelated"],
                "bridge_record_ids": ["wallet:bridge"], "bridge_direction": "opposite_physical",
                "clearing_evidence": [
                    {"record_id": "wallet:bridge", "period": "2024-01", "currency": "EUR",
                     "amount": 13.27, "provider": "wallet", "account": "wallet", "source_system": "wallet"},
                    {"record_id": "wallet:unrelated", "period": "2024-01", "currency": "USD",
                     "amount": 999.0, "provider": "wallet", "account": "wallet", "source_system": "wallet"},
                ],
                "clearing_totals": {"EUR": 13.27, "USD": 999.0},
                "clearing_relation": "reviewed_group", "bridge_amount": -13.27,
            },
        )

        resolved = bookrecon._validated_clearing_allocation_references(
            {"a": reviewed}, {"wallet:bridge": eur, "wallet:unrelated": unrelated},
            allocation_company_slug="example", normalized_company_slug="example",
        )

        self.assertEqual(resolved, set())

    def test_clearing_proof_rejects_company_mismatch(self) -> None:
        movement = record(
            record_id="paypal:leg", source_system="paypal", event_type="paypal_card_deposit",
            gross_amount=13.27, attributes={"clearing_provider": "paypal", "clearing_account": "wallet"},
        )
        reviewed = allocation(
            statement_id="archive:debit", record_id="bank:debit", amount=-13.27,
            disposition="clearing_transfer",
            target={
                "document_type": "financial_transaction", "transaction_family": "failed_transfer_and_return",
                "clearing_record_ids": ["paypal:leg"], "bridge_record_ids": ["paypal:leg"],
                "bridge_direction": "opposite_physical",
                "clearing_evidence": [{"record_id": "paypal:leg", "period": "2024-01", "currency": "EUR",
                    "amount": 13.27, "provider": "paypal", "account": "wallet", "source_system": "paypal"}],
                "clearing_totals": {"EUR": 13.27}, "clearing_relation": "reviewed_group", "bridge_amount": -13.27,
            },
        )

        resolved = bookrecon._validated_clearing_allocation_references(
            {"a": reviewed}, {"paypal:leg": movement},
            allocation_company_slug="other", normalized_company_slug="example",
        )

        self.assertEqual(resolved, set())

    def test_cross_currency_group_requires_bound_rate_and_cent_exact_equation(self) -> None:
        movement = record(
            record_id="printful:usd", source_system="printful", event_type="printful_wallet_deposit",
            gross_amount=-306.32, currency="USD",
            attributes={"clearing_provider": "printful", "clearing_account": "wallet"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_path = root / "statement.csv"
            evidence_path.write_text("306.32 USD, fee 2.82 EUR, rate 0.9198876", encoding="utf-8")
            evidence = {
                "path": "statement.csv",
                "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            }
            target = {
                "document_type": "purchase", "action_key": "purchase",
                "clearing_record_ids": ["printful:usd"], "bridge_record_ids": ["printful:usd"],
                "bridge_direction": "same_as_physical",
                "clearing_evidence": [{"record_id": "printful:usd", "period": "2024-01", "currency": "USD",
                    "amount": -306.32, "provider": "printful", "account": "wallet", "source_system": "printful"}],
                "clearing_totals": {"USD": -306.32}, "clearing_relation": "reviewed_group",
                "bridge_amount": -284.60,
                "fx_proof": {
                    "equation": "absolute_clearing_times_rate_plus_fee_equals_physical",
                    "physical_record_id": "bank:card", "physical_currency": "EUR", "physical_amount": -284.60,
                    "clearing_currency": "USD", "clearing_amount": -306.32,
                    "rate": 0.9198876, "fee_amount": 2.82, "rate_evidence": evidence,
                },
            }
            reviewed = allocation(
                statement_id="archive:card", record_id="bank:card", amount=-284.60,
                disposition="generated_purchase_payment", target=target,
            )

            valid = bookrecon._validated_clearing_allocation_references(
                {"a": reviewed}, {"printful:usd": movement},
                allocation_company_slug="example", normalized_company_slug="example", repo_root=root,
            )
            reviewed["target"]["fx_proof"]["rate_evidence"]["sha256"] = "0" * 64
            bad_binding = bookrecon._validated_clearing_allocation_references(
                {"a": reviewed}, {"printful:usd": movement},
                allocation_company_slug="example", normalized_company_slug="example", repo_root=root,
            )

        self.assertEqual(valid, {"printful:usd"})
        self.assertEqual(bad_binding, set())

    def test_cross_currency_group_rejects_mixed_currency_bridge_legs(self) -> None:
        usd = record(
            record_id="wallet:usd", source_system="wallet", event_type="conversion",
            gross_amount=-10.0, currency="USD",
            attributes={"clearing_provider": "wallet", "clearing_account": "wallet"},
        )
        eur = record(
            record_id="wallet:eur", source_system="wallet", event_type="fee",
            gross_amount=-1.0, currency="EUR",
            attributes={"clearing_provider": "wallet", "clearing_account": "wallet"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_path = root / "rate.txt"
            evidence_path.write_text("reviewed rate", encoding="utf-8")
            target = {
                "document_type": "purchase", "action_key": "purchase",
                "clearing_record_ids": ["wallet:usd", "wallet:eur"],
                "bridge_record_ids": ["wallet:usd", "wallet:eur"],
                "bridge_direction": "same_as_physical",
                "clearing_evidence": [
                    {"record_id": "wallet:usd", "period": "2024-01", "currency": "USD",
                     "amount": -10.0, "provider": "wallet", "account": "wallet", "source_system": "wallet"},
                    {"record_id": "wallet:eur", "period": "2024-01", "currency": "EUR",
                     "amount": -1.0, "provider": "wallet", "account": "wallet", "source_system": "wallet"},
                ],
                "clearing_totals": {"USD": -10.0, "EUR": -1.0},
                "clearing_relation": "reviewed_group", "bridge_amount": -10.0,
                "fx_proof": {
                    "equation": "absolute_clearing_times_rate_plus_fee_equals_physical",
                    "physical_record_id": "bank:card", "physical_currency": "EUR", "physical_amount": -10.0,
                    "clearing_currency": "USD", "clearing_amount": -11.0,
                    "rate": 0.818181818, "fee_amount": -1.0,
                    "rate_evidence": {"path": "rate.txt", "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()},
                },
            }
            reviewed = allocation(
                statement_id="archive:card", record_id="bank:card", amount=-10.0,
                disposition="generated_purchase_payment", target=target,
            )

            resolved = bookrecon._validated_clearing_allocation_references(
                {"a": reviewed}, {"wallet:usd": usd, "wallet:eur": eur},
                allocation_company_slug="example", normalized_company_slug="example", repo_root=root,
            )

        self.assertEqual(resolved, set())

    def test_duplicate_clearing_claims_reject_every_claimant_independent_of_order(self) -> None:
        movement = record(
            record_id="paypal:leg", source_system="paypal", event_type="paypal_card_deposit",
            gross_amount=13.27, attributes={"clearing_provider": "paypal", "clearing_account": "wallet"},
        )
        target = {
            "document_type": "financial_transaction", "transaction_family": "failed_transfer_and_return",
            "clearing_record_ids": ["paypal:leg"], "bridge_record_ids": ["paypal:leg"],
            "bridge_direction": "opposite_physical",
            "clearing_evidence": [{"record_id": "paypal:leg", "period": "2024-01", "currency": "EUR",
                "amount": 13.27, "provider": "paypal", "account": "wallet", "source_system": "paypal"}],
            "clearing_totals": {"EUR": 13.27}, "clearing_relation": "reviewed_group", "bridge_amount": -13.27,
        }
        first = allocation(statement_id="a", record_id="a", amount=-13.27, disposition="clearing_transfer", target=target)
        second = allocation(statement_id="b", record_id="b", amount=-13.27, disposition="clearing_transfer", target=target)

        forward = bookrecon._validated_clearing_allocation_references(
            {"a": first, "b": second}, {"paypal:leg": movement},
            allocation_company_slug="example", normalized_company_slug="example",
        )
        reverse = bookrecon._validated_clearing_allocation_references(
            {"b": second, "a": first}, {"paypal:leg": movement},
            allocation_company_slug="example", normalized_company_slug="example",
        )

        self.assertEqual(forward, set())
        self.assertEqual(reverse, set())

    def test_duplicate_evidence_for_one_clearing_reference_is_rejected(self) -> None:
        movement = record(
            record_id="wallet:leg", source_system="wallet", event_type="deposit",
            gross_amount=13.27, attributes={"clearing_provider": "wallet", "clearing_account": "wallet"},
        )
        evidence = {"record_id": "wallet:leg", "period": "2024-01", "currency": "EUR",
                    "amount": 13.27, "provider": "wallet", "account": "wallet", "source_system": "wallet"}
        reviewed = allocation(
            statement_id="archive:debit", record_id="bank:debit", amount=-13.27,
            disposition="clearing_transfer",
            target={
                "document_type": "financial_transaction", "transaction_family": "internal_transfer",
                "clearing_record_ids": ["wallet:leg"], "bridge_record_ids": ["wallet:leg"],
                "bridge_direction": "opposite_physical", "clearing_evidence": [evidence, dict(evidence)],
                "clearing_totals": {"EUR": 13.27}, "clearing_relation": "reviewed_group",
                "bridge_amount": -13.27,
            },
        )

        resolved = bookrecon._validated_clearing_allocation_references(
            {"a": reviewed}, {"wallet:leg": movement},
            allocation_company_slug="example", normalized_company_slug="example",
        )

        self.assertEqual(resolved, set())

    def test_clearing_equations_validate_conversion_and_fee_roles(self) -> None:
        records = {
            "conversion:eur": record(
                record_id="conversion:eur", source_system="wallet", event_type="conversion",
                gross_amount=-13.27, attributes={"clearing_provider": "wallet", "clearing_account": "wallet", "reference_transaction_id": "PAIR1"},
            ),
            "conversion:usd": record(
                record_id="conversion:usd", source_system="wallet", event_type="conversion",
                gross_amount=14.94, currency="USD",
                attributes={"clearing_provider": "wallet", "clearing_account": "wallet", "reference_transaction_id": "PAIR1"},
            ),
            "fee:eur": record(
                record_id="fee:eur", source_system="wallet", event_type="fee",
                gross_amount=-1.0, attributes={"clearing_provider": "wallet", "clearing_account": "wallet"},
            ),
        }
        target = {
            "clearing_equations": [
                {
                    "equation": "absolute_source_times_rate_equals_destination",
                    "role": "currency_conversion",
                    "record_ids": ["conversion:eur", "conversion:usd"],
                    "source_record_id": "conversion:eur", "destination_record_id": "conversion:usd",
                    "reference_id": "PAIR1", "rate": 1.1258477769,
                },
                {
                    "equation": "single_record_equals_reviewed_fee", "role": "fee_leg",
                    "record_ids": ["fee:eur"], "currency": "EUR", "reviewed_amount": -1.0,
                },
            ]
        }

        valid = bookrecon._clearing_equations_explain_claimed_records(
            target, records,
            claimed_record_ids={"bridge", "conversion:eur", "conversion:usd", "fee:eur"},
            bridge_record_ids={"bridge"},
        )
        target["clearing_equations"][0]["rate"] = 2.0
        bad_rate = bookrecon._clearing_equations_explain_claimed_records(
            target, records,
            claimed_record_ids={"bridge", "conversion:eur", "conversion:usd", "fee:eur"},
            bridge_record_ids={"bridge"},
        )
        target["clearing_equations"][0].update({"rate": 1.1258477769, "reference_id": "WRONG"})
        bad_reference = bookrecon._clearing_equations_explain_claimed_records(
            target, records,
            claimed_record_ids={"bridge", "conversion:eur", "conversion:usd", "fee:eur"},
            bridge_record_ids={"bridge"},
        )

        self.assertTrue(valid)
        self.assertFalse(bad_rate)
        self.assertFalse(bad_reference)

    def test_processor_payout_record_can_prove_exact_clearing_transfer_bridge(self) -> None:
        payout = record(
            record_id="processor:payout", source_system="processor", event_type="processor_payout",
            gross_amount=20.0, channel="processor", attributes={},
        )
        reviewed = allocation(
            statement_id="archive:credit", record_id="bank:credit", amount=20.0,
            disposition="clearing_transfer",
            target={
                "document_type": "financial_transaction", "transaction_family": "processor_payout_transfer",
                "clearing_record_ids": ["processor:payout"], "bridge_record_ids": ["processor:payout"],
                "bridge_direction": "same_as_physical",
                "clearing_evidence": [{"record_id": "processor:payout", "period": "2024-01",
                    "currency": "EUR", "amount": 20.0, "provider": "processor",
                    "account": "processor_payout", "source_system": "processor"}],
                "clearing_totals": {"EUR": 20.0}, "clearing_relation": "exact_amount", "bridge_amount": 20.0,
            },
        )

        resolved = bookrecon._validated_clearing_allocation_references(
            {"a": reviewed}, {"processor:payout": payout},
            allocation_company_slug="example", normalized_company_slug="example",
        )

        self.assertEqual(resolved, {"processor:payout"})

    def test_supported_transfer_families_are_supplier_neutral(self) -> None:
        rendered = " ".join(sorted(bookrecon.SUPPORTED_CLEARING_TRANSFER_FAMILIES)).lower()
        self.assertNotIn("quartermaster", rendered)
        self.assertNotIn("paypal", rendered)

    def test_processor_classifier_ignores_refs_nested_in_woo_vat_evidence(self) -> None:
        woo_sale = record(
            record_id="woo:1",
            source_system="woo",
            channel="woo",
            event_type="woo_daily_sales",
            gross_amount=113.0,
            attributes={
                "vat_allocation": {
                    "component_vat_evidence": [
                        {"order_id": "EXAMPLE-1", "processor_ref": "paypal-reference"}
                    ]
                }
            },
        )

        self.assertIsNone(bookrecon.infer_processor(woo_sale))
        stripe_sale = dict(woo_sale, source_system="stripe", channel="stripe", event_type="stripe_charge")
        self.assertEqual(bookrecon.infer_processor(stripe_sale), "stripe")

    def test_supplier_credit_check_reports_total_by_currency(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_credits"].append(
            record(
                record_id="printful:credit:1",
                source_system="printful",
                event_type="printful_supplier_credit",
                gross_amount=113.12,
                description="Printful supplier credit",
            )
        )

        checks = bookrecon.build_purchase_credit_checks(
            normalized_path_display="normalized/2024-07.json",
            records=normalized["records"],
        )

        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], "pass")
        self.assertEqual(checks[0]["lhs_amount"], 113.12)
        self.assertEqual(checks[0]["lhs_label"], "Supplier credits (EUR)")

    def test_blocking_normalized_exception_blocks_build(self) -> None:
        normalized = base_normalized()
        normalized["exceptions"].append(
            {
                "exception_id": "source-gap",
                "severity": "error",
                "reason": "Source export is missing.",
                "blocking": True,
                "suggested_follow_up": "Add the export.",
                "source_refs": [{"source_id": "src-1", "path": "x.csv", "row_ref": None, "page_ref": None, "notes": None}],
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        self.assertFalse(document["approve_for_build"])
        self.assertEqual(document["blocking_issue_count"], 1)
        self.assertTrue(any(item["exception_id"] == "normalized:source-gap" for item in document["exceptions"]))

    def test_woo_sales_vs_processor_gross_passes(self) -> None:
        normalized = base_normalized()
        normalized["records"]["sales"].append(
            record(
                record_id="woo:1",
                source_system="woo",
                channel="woo",
                event_type="woo_daily_sales",
                gross_amount=113.0,
                net_amount=90.0,
                description="Woo daily sales",
            )
        )
        normalized["records"]["sales"].append(
            record(
                record_id="paypal:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_website_payment",
                gross_amount=113.0,
                net_amount=108.0,
                description="PayPal Website Payment",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        check = find_check(document, "woo-sales-vs-processor-gross")
        self.assertEqual(check["status"], "pass")
        self.assertTrue(any("posting basis should stay" in note.lower() for note in check["notes"]))
        self.assertTrue(document["approve_for_build"])

    def test_missing_processor_evidence_from_bank_blocks(self) -> None:
        normalized = base_normalized()
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:stripe:1",
                source_system="bank",
                event_type="bank_credit",
                gross_amount=85.0,
                description="Stripe payout January",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        self.assertFalse(document["approve_for_build"])
        self.assertTrue(any(item["exception_id"] == "bookrecon:missing-processor-evidence:stripe" for item in document["exceptions"]))

    def test_reviewed_processor_named_transfer_return_does_not_require_sales_export(self) -> None:
        normalized = base_normalized()
        bank = bank_row(record_id="paypal-return", amount=13.27)
        bank["description"] = "PAYPAL *PAYPAL returned card transfer"
        normalized["records"]["bank_transactions"].append(bank)
        clearing = record(
            record_id="paypal:clearing:return", source_system="paypal",
            event_type="paypal_payment_reversal", gross_amount=13.27,
            attributes={"clearing_provider": "paypal", "clearing_account": "paypal_wallet"},
        )
        normalized["records"]["clearing_transactions"].append(clearing)
        reviewed = allocation(
            statement_id="archive:paypal-return",
            record_id="paypal-return",
            amount=13.27,
            disposition="clearing_transfer",
            target={
                "document_type": "financial_transaction",
                "transaction_family": "failed_transfer_and_return",
                "clearing_record_ids": ["paypal:clearing:return"],
                "bridge_record_ids": ["paypal:clearing:return"],
                "bridge_direction": "same_as_physical",
                "clearing_evidence": [{
                    "record_id": "paypal:clearing:return", "period": "2024-01",
                    "currency": "EUR", "amount": 13.27,
                    "provider": "paypal", "account": "paypal_wallet", "source_system": "paypal",
                }],
                "clearing_totals": {"EUR": 13.27},
                "clearing_relation": "exact_amount", "bridge_amount": 13.27,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
                bank_allocations={"archive:paypal-return": reviewed},
            )

        self.assertFalse(any(item["exception_id"] == "bookrecon:missing-processor-evidence:paypal" for item in document["exceptions"]))

    def test_arbitrary_reviewed_clearing_transfer_does_not_exempt_missing_processor_evidence(self) -> None:
        normalized = base_normalized()
        bank = bank_row(record_id="paypal-return", amount=13.27)
        bank["description"] = "PAYPAL transfer"
        normalized["records"]["bank_transactions"].append(bank)
        reviewed = allocation(
            statement_id="archive:paypal-return", record_id="paypal-return", amount=13.27,
            disposition="clearing_transfer",
            target={"document_type": "financial_transaction", "transaction_family": "arbitrary"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized, normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp), amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
                bank_allocations={"archive:paypal-return": reviewed},
            )

        self.assertTrue(any(item["exception_id"] == "bookrecon:missing-processor-evidence:paypal" for item in document["exceptions"]))

    def test_processor_payouts_vs_bank_receipts_passes(self) -> None:
        normalized = base_normalized()
        normalized["records"]["payouts"].append(
            record(
                record_id="paypal:payout:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_withdrawal",
                gross_amount=85.0,
                description="PayPal transfer to bank",
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="bank:paypal:1",
                source_system="bank",
                event_type="bank_credit",
                gross_amount=85.0,
                description="PayPal transfer to bank",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        check = find_check(document, "processor-payouts-vs-bank:paypal")
        self.assertEqual(check["status"], "pass")

    def test_continuity_warns_on_missing_previous_source_system(self) -> None:
        current = base_normalized("2024-01")
        current["records"]["bank_transactions"].append(
            record(
                record_id="bank:1",
                source_system="bank",
                event_type="bank_credit",
                gross_amount=20.0,
                description="Bank receipt",
            )
        )

        previous = base_normalized("2023-12")
        previous["records"]["sales"].append(
            record(
                record_id="paypal:sale:1",
                source_system="paypal",
                channel="paypal",
                event_type="paypal_website_payment",
                gross_amount=20.0,
                description="PayPal Website Payment",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=current,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
                previous_payload=previous,
                previous_path=Path(tmp) / "2023-12.json",
            )

        check = find_check(document, "continuity-vs-previous-period")
        self.assertEqual(check["status"], "warn")
        self.assertTrue(any("paypal" in note for note in check["notes"]))

    def test_fulfillment_checks_are_currency_aware(self) -> None:
        normalized = base_normalized()
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="printful:eur:expense",
                source_system="printful",
                channel="printful",
                event_type="printful_order_charge",
                gross_amount=7.9,
                description="Printful EUR expense",
                currency="EUR",
            )
        )
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="printful:usd:expense",
                source_system="printful",
                channel="printful",
                event_type="printful_service_charge",
                gross_amount=306.32,
                description="Printful USD expense",
                currency="USD",
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="printful:usd:bank",
                source_system="printful",
                channel="printful",
                event_type="printful_wallet_deposit",
                gross_amount=-306.32,
                description="Printful wallet funding",
                currency="USD",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-01.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        usd_check = find_check(document, "fulfillment-expenses-vs-bank:printful:usd")
        eur_check = find_check(document, "fulfillment-expenses-vs-bank:printful:eur")
        self.assertEqual(usd_check["status"], "pass")
        self.assertEqual(eur_check["status"], "warn")

    def test_quartermaster_is_recognized_as_fulfillment_partner(self) -> None:
        normalized = base_normalized("2024-10")
        normalized["records"]["purchase_expenses"].append(
            record(
                record_id="quartermaster:expense",
                source_system="quartermaster",
                channel="quartermaster",
                event_type="quartermaster_service_invoice",
                gross_amount=21.0,
                description="Quartermaster storage invoice",
                currency="USD",
            )
        )
        normalized["records"]["bank_transactions"].append(
            record(
                record_id="quartermaster:bank",
                source_system="bank",
                channel="quartermaster",
                event_type="bank_debit",
                gross_amount=-21.0,
                description="Quartermaster payment",
                currency="USD",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            document = bookrecon.build_recon_document(
                normalized_payload=normalized,
                normalized_path=Path(tmp) / "2024-10.json",
                repo_root=Path(tmp),
                amount_threshold=bookrecon.Decimal("0.5"),
                quantity_threshold=bookrecon.Decimal("1"),
            )

        check = find_check(document, "fulfillment-expenses-vs-bank:quartermaster:usd")
        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["lhs_amount"], 21.0)
        self.assertEqual(check["rhs_amount"], 21.0)


def wallet_row(*, record_id: str, amount: str, owner: str, deposit: bool = True) -> dict:
    return {
        "record_id": record_id,
        "source_system": "printful",
        "event_type": "printful_wallet_deposit" if deposit else "printful_wallet_withdrawal",
        "event_date": "2024-04-10",
        "currency": "EUR",
        "gross_amount": float(amount),
        "attributes": {
            "clearing_provider": "printful",
            "clearing_account": "printful_wallet",
            "card_last4": "1111" if owner == "reporting_person" else "2222",
            "funding_owner": owner,
        },
    }


def deposit(amount: str, *, owner: str = "reporting_person", record_id: str = "d1") -> dict:
    return wallet_row(record_id=record_id, amount=f"-{amount}", owner=owner, deposit=True)


def refund(amount: str, *, owner: str = "reporting_person", record_id: str = "r1") -> dict:
    return wallet_row(record_id=record_id, amount=amount, owner=owner, deposit=False)


class WalletEquationTests(unittest.TestCase):
    def test_personal_wallet_refund_cannot_exceed_bound_deposits(self) -> None:
        errors = bookrecon.printful_wallet_equation_errors([deposit("10.00"), refund("11.00")])

        self.assertIn("refund exceeds reviewed personal-card funding", " ".join(errors))

    def test_a_balanced_personal_wallet_group_has_no_error(self) -> None:
        self.assertEqual(bookrecon.printful_wallet_equation_errors([deposit("10.00"), refund("10.00")]), [])

    def test_company_card_refunds_are_measured_against_company_deposits(self) -> None:
        rows = [deposit("10.00"), refund("11.00", owner="company", record_id="r2")]

        self.assertIn("company-card funding", " ".join(bookrecon.printful_wallet_equation_errors(rows)))

    def test_a_row_without_a_reviewed_owner_is_an_error(self) -> None:
        row = deposit("10.00")
        del row["attributes"]["funding_owner"]

        self.assertIn("no reviewed funding owner", " ".join(bookrecon.printful_wallet_equation_errors([row])))

    def test_wallet_summary_reports_each_term_separately(self) -> None:
        rows = [
            deposit("45.00"),
            deposit("30.12", owner="company", record_id="d2"),
            refund("15.00"),
        ]

        summary = bookrecon.printful_wallet_summary(rows, consumption=Decimal("20.00"))

        self.assertEqual(summary["personal"]["deposits"], Decimal("45.00"))
        self.assertEqual(summary["personal"]["refunds"], Decimal("15.00"))
        self.assertEqual(summary["personal"]["liability_change"], Decimal("30.00"))
        self.assertEqual(summary["company"]["deposits"], Decimal("30.12"))
        self.assertEqual(summary["consumption"], Decimal("20.00"))
        self.assertEqual(summary["closing"], Decimal("40.12"))

    def test_wallet_rows_are_never_counted_as_physical_statement_coverage(self) -> None:
        rows = [deposit("45.00"), refund("15.00")]

        self.assertEqual(bookrecon.physical_statement_rows(rows), [])


EMPTY_RECORDS = {
    "sales": [], "refunds": [], "fees": [], "payouts": [], "bank_transactions": [],
    "clearing_transactions": [], "bank_balances": [], "purchase_expenses": [],
    "purchase_credits": [], "inventory_movements": [], "manual_adjustments": [], "other": [],
}


def inventory_check(records: dict, *, inventory_expected: bool = True) -> dict:
    return bookrecon.build_inventory_check(
        normalized_path_display="normalized.json",
        records=records,
        inventory_expected=inventory_expected,
        quantity_threshold=Decimal(0),
    )


class EmptyMonthInventoryTests(unittest.TestCase):
    def test_an_inventory_relevant_company_has_no_warning_in_an_empty_month(self) -> None:
        check = inventory_check(dict(EMPTY_RECORDS))

        self.assertEqual(check["status"], "pass")

    def test_a_sale_without_quantity_proof_still_warns(self) -> None:
        records = dict(EMPTY_RECORDS, sales=[{"record_id": "s1", "gross_amount": 10.0, "quantity": None}])

        check = inventory_check(records)

        self.assertEqual(check["status"], "warn")

    def test_a_purchase_without_quantity_proof_still_warns(self) -> None:
        records = dict(
            EMPTY_RECORDS, purchase_expenses=[{"record_id": "p1", "gross_amount": 10.0, "quantity": None}]
        )

        self.assertEqual(inventory_check(records)["status"], "warn")

    def test_an_inventory_movement_without_quantity_proof_still_warns(self) -> None:
        records = dict(
            EMPTY_RECORDS, inventory_movements=[{"record_id": "m1", "gross_amount": 0.0, "quantity": None}]
        )

        self.assertEqual(inventory_check(records)["status"], "warn")

    def test_warehouse_configuration_alone_is_not_activity(self) -> None:
        check = inventory_check(dict(EMPTY_RECORDS))

        self.assertIn("no inventory-affecting activity", " ".join(check["notes"]))

    def test_a_company_without_inventory_still_skips_an_empty_month(self) -> None:
        check = inventory_check(dict(EMPTY_RECORDS), inventory_expected=False)

        self.assertEqual(check["status"], "skipped")


class WalletFundingCheckTests(unittest.TestCase):
    def check(self, rows: list[dict]) -> dict:
        return bookrecon.build_wallet_funding_check(
            normalized_path_display="normalized.json",
            records={"clearing_transactions": rows},
        )

    def test_a_period_with_no_wallet_rows_is_skipped(self) -> None:
        self.assertEqual(self.check([])["status"], "skipped")

    def test_balanced_personal_funding_passes(self) -> None:
        self.assertEqual(self.check([deposit("10.00"), refund("10.00")])["status"], "pass")

    def test_a_refund_exceeding_its_funding_fails(self) -> None:
        result = self.check([deposit("10.00"), refund("11.00")])

        self.assertEqual(result["status"], "fail")
        self.assertIn("refund exceeds", " ".join(result["notes"]).lower())

    def test_a_refund_of_an_earlier_months_funding_is_not_a_failure(self) -> None:
        # Deposits in April, refunds in July: within July alone the refunds exceed that
        # month's deposits, but the year's funding covers them.
        annual = {
            "d-apr": deposit("45.00", record_id="d-apr"),
            "r-jul": refund("15.00", record_id="r-jul"),
        }
        result = bookrecon.build_wallet_funding_check(
            normalized_path_display="normalized.json",
            records={"clearing_transactions": [annual["r-jul"]]},
            annual_clearing_records=annual,
        )

        self.assertEqual(result["status"], "pass")

    def test_the_annual_view_still_catches_an_impossible_refund(self) -> None:
        annual = {
            "d1": deposit("10.00", record_id="d1"),
            "r1": refund("11.00", record_id="r1"),
        }
        result = bookrecon.build_wallet_funding_check(
            normalized_path_display="normalized.json",
            records={"clearing_transactions": [annual["r1"]]},
            annual_clearing_records=annual,
        )

        self.assertEqual(result["status"], "fail")

    def test_a_row_without_a_reviewed_owner_is_reported_not_blocked(self) -> None:
        # The printout parser already refuses an unknown card; a source with no card data
        # at all is named here rather than blocking a period that predates attribution.
        row = deposit("10.00")
        del row["attributes"]["funding_owner"]

        result = self.check([row])

        self.assertEqual(result["status"], "pass")
        self.assertIn("no reviewed funding owner", " ".join(result["notes"]))

    def test_the_check_reports_each_term_separately(self) -> None:
        result = self.check([deposit("45.00"), refund("15.00")])

        notes = " ".join(result["notes"])
        self.assertIn("45", notes)
        self.assertIn("15", notes)


if __name__ == "__main__":
    unittest.main()
