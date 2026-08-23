from __future__ import annotations  # noqa: I001

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

import statement_import_plan  # noqa: E402, I001


IBAN = "EE001234567890"
SOURCE_SHA = "a" * 64


def policy() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "bank_accounts": {IBAN: {"EUR": "3", "USD": "3"}},
        "contacts": {},
        "mappings": {},
        "supplier_aliases": {},
        "cash_posting": {
            "mode": "statement_import",
            "bank_income_account_ids": ["3"],
            "processor_income_account_ids": {"paypal": "6", "stripe": "7"},
            "bank_financial_accounts": {IBAN: {"EUR": "10", "USD": "11"}},
            "clearing_provider_roles": {"paypal": "paypal", "stripe": "stripe_clearing"},
            "financial_accounts": {
                "bank": "10",
                "stripe_clearing": "30",
                "paypal": "31",
                "bank_fees": "32",
                "reporting_person_payable": "33",
                "platform_prepayment": "34",
                "customer_receivable": "37",
                "supplier_payable": "38",
                "fx_gain": "35",
                "fx_loss": "36",
            },
        },
    }


def bank_record(
    *,
    record_id: str,
    archive: str,
    event_date: str,
    amount: str,
    currency: str = "EUR",
    counterparty: str = "Acme OU",
    description: str = "Statement row",
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "source_system": "bank",
        "source_type": "xml",
        "event_type": "bank_transaction",
        "event_date": event_date,
        "description": description,
        "currency": currency,
        "gross_amount": float(amount),
        "net_amount": float(amount),
        "vat_amount": 0.0,
        "fee_amount": 0.0,
        "shipping_amount": 0.0,
        "attributes": {"iban": IBAN, "archive_identifier": archive, "counterparty_name": counterparty},
        "source_refs": [{"source_id": "camt-2024", "path": "source/2024/camt.xml", "row_ref": record_id}],
    }


def normalized(period: str, records: list[dict[str, Any]], *, base_currency: str = "EUR") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "period": period,
        "base_currency": base_currency,
        "generated_at": "2026-08-23T00:00:00Z",
        "sources": [
            {
                "source_id": "camt-2024",
                "path": "source/2024/camt.xml",
                "sha256": SOURCE_SHA,
                "source_type": "xml",
                "covered_from": f"{period}-01",
                "covered_until": f"{period}-28",
                "canonical": True,
                "parser_name": "camt053",
            }
        ],
        "exceptions": [],
        "records": {"bank_transactions": records},
    }


def allocation(
    *,
    statement_id: str,
    record_id: str,
    period: str,
    amount: str,
    disposition: str,
    target: dict[str, Any],
    currency: str = "EUR",
    parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved = {
        "statement_id": statement_id,
        "record_id": record_id,
        "iban": IBAN,
        "period": period,
        "disposition": disposition,
        "amount": float(amount),
        "currency": currency,
        "target": target,
        "review": {"status": "approved", "rationale": "Reviewed."},
    }
    if parts is not None:
        resolved["parts"] = parts
    return resolved


def allocations(items: list[dict[str, Any]], *, year: int = 2024) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "year": year,
        "normalized_bindings": [{"path": "artifacts/normalized/2024-01.json", "sha256": "b" * 64}],
        "allocations": items,
    }


FEE_RECORD = bank_record(
    record_id="rec-fee", archive="a", event_date="2024-01-15", amount="-12.50", description="Service fee"
)
RECEIPT_RECORD = bank_record(
    record_id="rec-receipt", archive="b", event_date="2024-02-20", amount="100.00", counterparty="Brain Games"
)

FEE_ALLOCATION = allocation(
    statement_id="archive:a",
    record_id="rec-fee",
    period="2024-01",
    amount="-12.50",
    disposition="bank_fee_payment",
    target={"document_type": "financial_transaction", "transaction_family": "bank-fee"},
)
RECEIPT_ALLOCATION = allocation(
    statement_id="archive:b",
    record_id="rec-receipt",
    period="2024-02",
    amount="100.00",
    disposition="existing_invoice_receipt",
    target={"document_type": "invoice", "simplbooks_id": "58"},
)


def build(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "year": 2024,
        "normalized_payloads": [
            normalized("2024-01", [FEE_RECORD]),
            normalized("2024-02", [RECEIPT_RECORD]),
        ],
        "allocation_payload": allocations([FEE_ALLOCATION, RECEIPT_ALLOCATION]),
        "policy": policy(),
        "rate_bindings": [],
    }
    kwargs.update(overrides)
    return statement_import_plan.build_statement_import_plan(**kwargs)


class PlanCoverageTests(unittest.TestCase):
    def test_plan_covers_every_physical_row_once_and_maps_manual_families(self) -> None:
        plan = build()

        self.assertEqual([row["statement_id"] for row in plan["rows"]], ["archive:a", "archive:b"])
        self.assertEqual(plan["coverage"]["uncovered_count"], 0)
        self.assertEqual(plan["coverage"]["extra_count"], 0)
        self.assertEqual(plan["coverage"]["physical_row_count"], 2)
        self.assertEqual(plan["coverage"]["families"], {"bank_fee": 1, "document_settlement": 1})

    def test_bank_fee_row_debits_the_expense_and_credits_the_statement_account(self) -> None:
        fee = build()["rows"][0]

        self.assertEqual(fee["financial_accounts"], {"debit": "32", "credit": "10"})
        self.assertEqual(fee["financial_account_roles"], {"debit": "bank_fees", "credit": "bank"})
        self.assertEqual(fee["ui_action"], "assign_general_ledger")
        self.assertEqual(fee["family"], "bank_fee")

    def test_receipt_row_debits_the_statement_account_and_names_its_document(self) -> None:
        receipt = build()["rows"][1]

        self.assertEqual(receipt["financial_accounts"], {"debit": "10", "credit": "37"})
        self.assertEqual(receipt["ui_action"], "match_document")
        self.assertEqual(receipt["document_refs"], [{"document_type": "invoice", "simplbooks_id": "58"}])

    def test_every_row_binds_its_immutable_source(self) -> None:
        row = build()["rows"][0]

        self.assertEqual(row["source"]["path"], "source/2024/camt.xml")
        self.assertEqual(row["source"]["sha256"], SOURCE_SHA)
        self.assertEqual(row["source"]["row_ref"], "rec-fee")

    def test_uncovered_physical_row_blocks_plan_construction(self) -> None:
        with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "missing"):
            build(allocation_payload=allocations([FEE_ALLOCATION]))

    def test_allocation_without_a_physical_row_blocks_plan_construction(self) -> None:
        extra = allocation(
            statement_id="archive:z",
            record_id="rec-z",
            period="2024-03",
            amount="1.00",
            disposition="bank_fee_payment",
            target={"document_type": "financial_transaction", "transaction_family": "bank-fee"},
        )

        with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "extra"):
            build(allocation_payload=allocations([FEE_ALLOCATION, RECEIPT_ALLOCATION, extra]))

    def test_plan_reports_annual_movement_per_account_and_currency(self) -> None:
        plan = build()

        self.assertEqual(plan["coverage"]["movement"], {f"{IBAN}|EUR": "87.50"})

    def test_building_the_same_inputs_twice_is_identical(self) -> None:
        self.assertEqual(build(), build())

    def test_a_plan_takes_its_company_from_the_reviewed_allocations(self) -> None:
        payload = allocations([FEE_ALLOCATION, RECEIPT_ALLOCATION])
        payload["company_slug"] = "other-company"

        self.assertEqual(build(allocation_payload=payload)["company_slug"], "other-company")


class PlanAccountResolutionTests(unittest.TestCase):
    def test_clearing_row_resolves_its_role_from_the_reviewed_provider(self) -> None:
        record = bank_record(record_id="rec-c", archive="c", event_date="2024-03-04", amount="-40.00")
        clearing = allocation(
            statement_id="archive:c",
            record_id="rec-c",
            period="2024-03",
            amount="-40.00",
            disposition="clearing_transfer",
            target={
                "document_type": "financial_transaction",
                "transaction_family": "processor-funding",
                "clearing_record_ids": ["clr-1"],
                "bridge_record_ids": ["rec-c"],
                "bridge_direction": "same_as_physical",
                "clearing_relation": "exact_amount",
                "bridge_amount": -40.0,
                "clearing_totals": {"EUR": -40.0},
                "clearing_evidence": [
                    {
                        "record_id": "clr-1",
                        "period": "2024-03",
                        "currency": "EUR",
                        "amount": -40.0,
                        "provider": "PayPal",
                        "account": "paypal-eur",
                        "source_system": "paypal",
                    }
                ],
            },
        )
        plan = build(
            normalized_payloads=[normalized("2024-03", [record])],
            allocation_payload=allocations([clearing]),
        )

        row = plan["rows"][0]
        self.assertEqual(row["financial_accounts"], {"debit": "31", "credit": "10"})
        self.assertEqual(row["financial_account_roles"], {"debit": "paypal", "credit": "bank"})
        self.assertEqual(row["family"], "processor_or_internal_transfer")

    def test_unreviewed_clearing_provider_blocks_the_plan(self) -> None:
        record = bank_record(record_id="rec-c", archive="c", event_date="2024-03-04", amount="-40.00")
        clearing = allocation(
            statement_id="archive:c",
            record_id="rec-c",
            period="2024-03",
            amount="-40.00",
            disposition="clearing_transfer",
            target={
                "document_type": "financial_transaction",
                "transaction_family": "processor-funding",
                "clearing_record_ids": ["clr-1"],
                "bridge_record_ids": ["rec-c"],
                "bridge_direction": "same_as_physical",
                "clearing_relation": "exact_amount",
                "bridge_amount": -40.0,
                "clearing_totals": {"EUR": -40.0},
                "clearing_evidence": [
                    {
                        "record_id": "clr-1",
                        "period": "2024-03",
                        "currency": "EUR",
                        "amount": -40.0,
                        "provider": "Wise",
                        "account": "wise-eur",
                        "source_system": "wise",
                    }
                ],
            },
        )

        with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "clearing provider"):
            build(
                normalized_payloads=[normalized("2024-03", [record])],
                allocation_payload=allocations([clearing]),
            )

    def test_document_row_without_a_document_target_blocks_the_plan(self) -> None:
        broken = dict(RECEIPT_ALLOCATION, target={"note": "no document"})

        with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "document target"):
            build(allocation_payload=allocations([FEE_ALLOCATION, broken]))

    def test_account_roles_are_never_inferred_from_description_text(self) -> None:
        record = bank_record(
            record_id="rec-fee", archive="a", event_date="2024-01-15", amount="-12.50", description="PayPal fee"
        )
        plan = build(
            normalized_payloads=[normalized("2024-01", [record]), normalized("2024-02", [RECEIPT_RECORD])]
        )

        self.assertEqual(plan["rows"][0]["financial_account_roles"]["debit"], "bank_fees")


class PlanSplitTests(unittest.TestCase):
    def split_plan(self, part_amounts: list[str]) -> dict[str, Any]:
        record = bank_record(record_id="rec-s", archive="s", event_date="2024-04-02", amount="-100.00")
        parts = [
            {
                "amount": float(part_amounts[0]),
                "disposition": "bank_fee_payment",
                "target": {"document_type": "financial_transaction", "transaction_family": "bank-fee"},
            },
            {
                "amount": float(part_amounts[1]),
                "disposition": "existing_purchase_payment",
                "target": {"document_type": "purchase", "simplbooks_id": "77"},
            },
        ]
        split = allocation(
            statement_id="archive:s",
            record_id="rec-s",
            period="2024-04",
            amount="-100.00",
            disposition="reviewed_split",
            target={"document_type": "financial_transaction", "transaction_family": "reviewed-split"},
            parts=parts,
        )
        return build(
            normalized_payloads=[normalized("2024-04", [record])],
            allocation_payload=allocations([split]),
        )

    def test_balanced_split_states_an_exact_equation_per_part(self) -> None:
        row = self.split_plan(["-10.00", "-90.00"])["rows"][0]

        self.assertEqual(row["ui_action"], "split_and_assign")
        self.assertEqual(row["split_equation"], "-100.00 = -10.00 + -90.00")
        self.assertEqual(
            [part["financial_accounts"] for part in row["parts"]],
            [{"debit": "32", "credit": "10"}, {"debit": "38", "credit": "10"}],
        )

    def test_unbalanced_split_is_rejected(self) -> None:
        plan = self.split_plan(["-10.00", "-90.00"])
        plan["rows"][0]["parts"][1]["signed_amount"] = "-85.00"

        with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "split"):
            statement_import_plan.validate_statement_import_plan(plan)

    def test_duplicate_statement_identity_is_rejected(self) -> None:
        plan = build()
        plan["rows"].append(dict(plan["rows"][0]))

        with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "duplicate"):
            statement_import_plan.validate_statement_import_plan(plan)

    def test_a_built_plan_validates(self) -> None:
        statement_import_plan.validate_statement_import_plan(build())


class PlanCurrencyTests(unittest.TestCase):
    def rate_binding(self) -> dict[str, Any]:
        return {
            "path": "artifacts/rates/2024-USD-EUR.json",
            "sha256": "c" * 64,
            "cache": {
                "schema_version": "1.0",
                "provider": "ECB",
                "year": 2024,
                "base": "USD",
                "quote": "EUR",
                "source_url": "https://api.frankfurter.dev/v2/rates",
                "retrieved_at": "2026-08-23T00:00:00Z",
                "rates": [{"date": "2024-05-02", "base": "USD", "quote": "EUR", "rate": "0.9000"}],
            },
        }

    def usd_inputs(self) -> dict[str, Any]:
        record = bank_record(
            record_id="rec-u", archive="u", event_date="2024-05-06", amount="200.00", currency="USD"
        )
        usd = allocation(
            statement_id="archive:u",
            record_id="rec-u",
            period="2024-05",
            amount="200.00",
            currency="USD",
            disposition="existing_invoice_receipt",
            target={"document_type": "invoice", "simplbooks_id": "59"},
        )
        return {
            "normalized_payloads": [normalized("2024-05", [record])],
            "allocation_payload": allocations([usd]),
        }

    def test_foreign_currency_row_carries_its_reviewed_rate_and_conversion(self) -> None:
        plan = build(**self.usd_inputs(), rate_bindings=[self.rate_binding()])

        row = plan["rows"][0]
        self.assertEqual(row["financial_accounts"], {"debit": "11", "credit": "37"})
        self.assertEqual(row["ecb"]["rate"], "0.9000")
        self.assertEqual(row["ecb"]["effective_date"], "2024-05-02")
        self.assertEqual(row["ecb"]["converted_amount"], "180.00")
        self.assertEqual(row["ecb"]["binding"], {"path": "artifacts/rates/2024-USD-EUR.json", "sha256": "c" * 64})

    def test_foreign_currency_row_without_a_bound_rate_blocks_the_plan(self) -> None:
        with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "rate"):
            build(**self.usd_inputs(), rate_bindings=[])


class PlanRenderingTests(unittest.TestCase):
    def test_csv_column_order_is_fixed(self) -> None:
        csv_text = statement_import_plan.render_csv(build())

        self.assertEqual(
            csv_text.splitlines()[0],
            "statement_id,iban,date,currency,signed_amount,counterparty,description,disposition,"
            "ui_action,debit_account,credit_account,document_refs,ecb_rate,split_equation,status",
        )

    def test_csv_writes_one_line_per_physical_row(self) -> None:
        csv_text = statement_import_plan.render_csv(build())

        self.assertEqual(len(csv_text.splitlines()), 3)
        self.assertIn("archive:a,EE001234567890,2024-01-15,EUR,-12.50", csv_text)

    def test_markdown_names_the_accounts_of_every_exceptional_row(self) -> None:
        markdown = statement_import_plan.render_markdown(build())

        self.assertIn("archive:a", markdown)
        self.assertIn("debit 32", markdown)
        self.assertIn("credit 10", markdown)

    def test_markdown_names_the_document_target_of_every_document_row(self) -> None:
        markdown = statement_import_plan.render_markdown(build())

        self.assertIn("invoice 58", markdown)


class PlanArtifactTests(unittest.TestCase):
    def test_writing_artifacts_reports_paths_hashes_and_counts(self) -> None:
        plan = build()
        with tempfile.TemporaryDirectory() as raw:
            output_dir = Path(raw)
            result = statement_import_plan.write_plan_artifacts(plan, output_dir=output_dir)

            self.assertEqual(
                sorted(result["artifacts"]), ["plan_csv", "plan_json", "plan_markdown"]
            )
            written = output_dir / "2024-plan.json"
            self.assertEqual(
                result["artifacts"]["plan_json"]["sha256"],
                hashlib.sha256(written.read_bytes()).hexdigest(),
            )
            self.assertEqual(result["coverage"]["uncovered_count"], 0)
            self.assertEqual(result["coverage"]["families"], {"bank_fee": 1, "document_settlement": 1})
            self.assertEqual(json.loads(written.read_text(encoding="utf-8")), plan)

    def test_rewriting_an_unchanged_plan_leaves_the_bytes_alone(self) -> None:
        plan = build()
        with tempfile.TemporaryDirectory() as raw:
            output_dir = Path(raw)
            statement_import_plan.write_plan_artifacts(plan, output_dir=output_dir)
            written = output_dir / "2024-plan.json"
            before = written.stat().st_mtime_ns

            statement_import_plan.write_plan_artifacts(plan, output_dir=output_dir)

            self.assertEqual(written.stat().st_mtime_ns, before)


class PlanDecimalTests(unittest.TestCase):
    def test_amounts_stay_exact_strings_rather_than_floats(self) -> None:
        row = build()["rows"][0]

        self.assertEqual(row["signed_amount"], "-12.50")
        self.assertEqual(Decimal(row["signed_amount"]), Decimal("-12.50"))


class DirectSaleTargetTests(unittest.TestCase):
    def direct_sale(self, target_extra: dict[str, Any] | None = None) -> dict[str, Any]:
        record = bank_record(record_id="rec-d", archive="d", event_date="2024-06-04", amount="60.00")
        target = {
            "document_type": "invoice",
            "contact_label": "direct-sale",
            "posting_family": "direct-sale-taxable",
            "vat_profile": "taxable",
            "product_description": "Reviewed direct sale",
            "quantity": 1,
            "gross_amount": 60.0,
            "warehouse_id": "1",
            "article_id": "3",
        }
        target.update(target_extra or {})
        item = allocation(
            statement_id="archive:d", record_id="rec-d", period="2024-06", amount="60.00",
            disposition="direct_sale_receipt", target=target,
        )
        return build(
            normalized_payloads=[normalized("2024-06", [record])],
            allocation_payload=allocations([item]),
        )

    def test_a_direct_sale_names_the_invoice_generated_for_its_own_row(self) -> None:
        # One invoice is created per physical receipt, so the statement identity names it.
        row = self.direct_sale()["rows"][0]

        self.assertEqual(
            row["document_refs"],
            [{"document_type": "invoice", "generated_for_statement_id": "archive:d"}],
        )
        self.assertEqual(row["ui_action"], "match_document")

    def test_a_direct_sale_still_settles_the_customer_receivable(self) -> None:
        row = self.direct_sale()["rows"][0]

        self.assertEqual(row["financial_accounts"], {"debit": "10", "credit": "37"})

    def test_an_explicit_direct_sale_target_id_wins(self) -> None:
        row = self.direct_sale({"simplbooks_id": "512"})["rows"][0]

        self.assertEqual(row["document_refs"], [{"document_type": "invoice", "simplbooks_id": "512"}])

    def test_a_generated_receipt_without_an_action_key_still_blocks(self) -> None:
        broken = dict(RECEIPT_ALLOCATION, disposition="generated_invoice_receipt",
                      target={"document_type": "invoice"})

        with self.assertRaisesRegex(statement_import_plan.StatementImportPlanError, "document target"):
            build(allocation_payload=allocations([FEE_ALLOCATION, broken]))


class RatePathResolutionTests(unittest.TestCase):
    def test_the_company_rate_cache_is_found_without_being_named(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            company_dir = Path(raw)
            reference = company_dir / "artifacts" / "reference"
            reference.mkdir(parents=True)
            cache = reference / "ecb-rates-2024.json"
            cache.write_text("{}", encoding="utf-8")

            self.assertEqual(
                statement_import_plan.resolve_rate_paths(company_dir=company_dir, year=2024, override=None),
                [cache],
            )

    def test_another_years_cache_is_not_picked_up(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            company_dir = Path(raw)
            reference = company_dir / "artifacts" / "reference"
            reference.mkdir(parents=True)
            (reference / "ecb-rates-2025.json").write_text("{}", encoding="utf-8")

            self.assertEqual(
                statement_import_plan.resolve_rate_paths(company_dir=company_dir, year=2024, override=None), []
            )

    def test_an_explicit_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            company_dir = Path(raw)
            reference = company_dir / "artifacts" / "reference"
            reference.mkdir(parents=True)
            (reference / "ecb-rates-2024.json").write_text("{}", encoding="utf-8")
            chosen = company_dir / "elsewhere.json"
            chosen.write_text("{}", encoding="utf-8")

            self.assertEqual(
                statement_import_plan.resolve_rate_paths(company_dir=company_dir, year=2024, override=[chosen]),
                [chosen],
            )

    def test_a_company_with_no_cache_resolves_to_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(
                statement_import_plan.resolve_rate_paths(company_dir=Path(raw), year=2024, override=None), []
            )


if __name__ == "__main__":
    unittest.main()
