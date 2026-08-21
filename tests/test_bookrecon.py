from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
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

        checks, ready = bookrecon.build_clearing_continuity_checks(
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


if __name__ == "__main__":
    unittest.main()
