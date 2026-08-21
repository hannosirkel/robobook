from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookbuilder  # noqa: E402
import bookchecker  # noqa: E402


RECORD_CATEGORIES = (
    "sales",
    "refunds",
    "fees",
    "payouts",
    "bank_transactions",
    "purchase_expenses",
    "inventory_movements",
    "manual_adjustments",
    "other",
)


def base_normalized(period: str = "2024-01") -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "period": period,
        "base_currency": "EUR",
        "generated_at": "2026-04-04T00:00:00Z",
        "sources": [],
        "records": {category: [] for category in RECORD_CATEGORIES},
        "exceptions": [],
    }


def base_recon(period: str = "2024-01", approve: bool = True) -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "period": period,
        "generated_at": "2026-04-04T00:00:00Z",
        "currency": "EUR",
        "approve_for_build": approve,
        "blocking_issue_count": 0 if approve else 3,
        "checks": [] if approve else [{"check_id": "gate", "name": "Gate", "status": "fail"}],
        "exceptions": [] if approve else [{"exception_id": "blocked", "severity": "error", "summary": "blocked", "blocking": True}],
        "notes": [],
    }


def record(
    *,
    record_id: str,
    source_system: str,
    event_type: str,
    gross_amount: float,
    net_amount: float | None = None,
    vat_amount: float = 0.0,
    fee_amount: float = 0.0,
    description: str = "record",
    channel: str | None = None,
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
        "currency": "EUR",
        "gross_amount": gross_amount,
        "net_amount": gross_amount if net_amount is None else net_amount,
        "vat_amount": vat_amount,
        "fee_amount": fee_amount,
        "shipping_amount": 0.0,
        "quantity": None,
        "sku": None,
        "warehouse_id": None,
        "channel": channel,
        "country_code": None,
        "attributes": {},
        "source_refs": [{"source_id": "src-1", "path": "source.csv", "row_ref": "csv:2", "page_ref": None, "notes": None}],
    }


def policy_with_24_percent_profile() -> dict:
    return {
        "schema_version": "1.0", "company_slug": "example", "bank_accounts": {},
        "contacts": {"sales": {"woo": "42"}, "processors": {}, "suppliers": {}},
        "mappings": {"woo-taxable": {"income_account_id": "107", "shipping_income_account_id": "253",
                                      "vat_type_id": "34", "shipping_vat_type_id": "33",
                                      "warehouse_id": "9"}},
        "sales_vat_profiles": [{"start": "2025-07-01", "end": None, "rate": 24,
                                "goods_vat_type_id": "34", "shipping_vat_type_id": "33"}],
        "supplier_aliases": {},
    }


def allocated_action_fixture(*, line_rate: int, vat_type_id: str) -> dict:
    return {"actions": [{
        "idempotency_key": "example-2025-11-woo", "action_type": "create_invoice_summary",
        "payload": {"document_date": "2025-11-30", "posting_policy_family": "woo-taxable",
                    "line_items": [{"line_role": "sales_revenue", "gross_amount": 62.00,
                                    "vat_amount_hint": 12.00, "suggested_vat_type_id": vat_type_id,
                                    "vat_profile_rate": line_rate,
                                    "vat_profile_period": "2025-07-01/open"}]}
    }]}


def payment_action(
    *,
    key: str,
    period: str,
    amount: float,
    linked_purchase_action: str,
    source_path: str,
    record_ref: str,
) -> dict:
    return {
        "idempotency_key": key,
        "period": period,
        "action_type": "create_payment_summary",
        "method": "POST",
        "endpoint": "payments/create",
        "payload": {
            "draft_schema": "cash_settlement_v1",
            "document_type": "payment",
            "document_date": f"{period}-31",
            "currency": "EUR",
            "counterparty": {
                "contact_id": "18",
                "display_name_hint": "jajaa payment summary",
            },
            "counterparty_hint": "jajaa",
            "bank_account_id": "101",
            "amount": amount,
            "linked_purchase_action": linked_purchase_action,
            "linked_purchase_period": linked_purchase_action.split("-")[1] + "-" + linked_purchase_action.split("-")[2],
            "record_count": 1,
        },
        "source_refs": [
            {
                "path": source_path,
                "record_ref": record_ref,
                "note": None,
            }
        ],
        "reason": "test",
        "confidence": "high",
        "depends_on": [],
        "expected_effect": "create payment",
        "review_notes": [],
        "executed_at": None,
        "response_status": None,
        "response_body": None,
        "inserted_id": None,
    }


def build_clean_artifacts(tmp: Path, *, recon_approve: bool = True, force: bool = False) -> tuple[Path, Path, Path]:
    normalized = base_normalized()
    normalized["records"]["sales"].append(
        record(
            record_id="paypal:sale:1",
            source_system="paypal",
            channel="paypal",
            event_type="paypal_website_payment",
            gross_amount=24.0,
            net_amount=23.0,
            vat_amount=4.0,
            fee_amount=1.0,
            description="PayPal Website Payment",
        )
    )
    normalized["records"]["payouts"].append(
        record(
            record_id="paypal:payout:1",
            source_system="paypal",
            channel="paypal",
            event_type="paypal_withdrawal",
            gross_amount=23.0,
            description="PayPal transfer to bank",
        )
    )
    normalized["records"]["bank_transactions"].append(
        record(
            record_id="bank:paypal:1",
            source_system="bank",
            event_type="bank_credit",
            gross_amount=23.0,
            description="PayPal transfer to bank",
        )
    )

    recon = base_recon(approve=recon_approve)
    entity_map = {
        "financial_accounts": [
            {"id": "3000", "name": "Product Sales", "code": "3000", "status": None},
            {"id": "6010", "name": "Payment Processor Fees", "code": "6010", "status": None},
        ],
        "income_accounts": [{"id": "1010", "name": "Main Bank", "code": "1010", "status": None}],
        "vat_types": [
            {"id": "22", "name": "22% VAT", "code": "22", "status": None},
            {"id": "0", "name": "0% Export", "code": "0", "status": None},
        ],
        "contacts": [
            {"id": "2001", "name": "PayPal", "status": None},
        ],
    }
    company_profile = {"bank_account_ids": ["101"]}

    normalized_path = tmp / "normalized.json"
    recon_path = tmp / "recon.json"
    action_path = tmp / "actions.yaml"
    normalized_path.write_text(json.dumps(normalized), encoding="utf-8")
    recon_path.write_text(json.dumps(recon), encoding="utf-8")

    batch = bookbuilder.build_action_batch(
        normalized_payload=normalized,
        recon_payload=recon,
        normalized_path=normalized_path,
        recon_path=recon_path,
        repo_root=tmp,
        entity_map=entity_map,
        company_profile=company_profile,
        force=force,
    )
    bookbuilder.write_yaml(action_path, batch)
    return normalized_path, recon_path, action_path


class BookcheckerTests(unittest.TestCase):
    def test_checker_blocks_vat_type_rate_mismatch(self) -> None:
        batch = allocated_action_fixture(line_rate=24, vat_type_id="25")

        report = bookchecker.evaluate_vat_profiles(batch["actions"], policy_with_24_percent_profile())

        self.assertTrue(any(item["severity"] == "error" and "VAT profile" in item["summary"] for item in report))

    def test_checker_requires_one_api_line_per_order_rounding_component(self) -> None:
        batch = allocated_action_fixture(line_rate=24, vat_type_id="34")
        line = batch["actions"][0]["payload"]["line_items"][0]
        line.update(
            {
                "vat_allocation_component": "goods",
                "gross_amount": 0.12,
                "vat_amount_hint": 0.04,
                "vat_allocation_component_evidence": [
                    {"order_id": f"EXAMPLE-{index}", "gross_amount": 0.03, "vat_amount": 0.01}
                    for index in range(1, 5)
                ],
            }
        )

        report = bookchecker.evaluate_vat_profiles(batch["actions"], policy_with_24_percent_profile())
        self.assertTrue(any("one order component" in item["summary"] for item in report))

        batch["actions"][0]["payload"]["line_items"] = [
            {
                **line,
                "gross_amount": 0.03,
                "vat_amount_hint": 0.01,
                "vat_allocation_component_evidence": [
                    {
                        "order_id": f"EXAMPLE-{index}", "gross_amount": 0.03, "vat_amount": 0.01,
                        "vat_profile": {
                            "start": "2025-07-01", "end": None, "rate": 24,
                            "goods_vat_type_id": "34", "shipping_vat_type_id": "33",
                        },
                    }
                ],
                "vat_evidence_binding": {
                    "allocation_ref": {"path": "allocation.json", "sha256": "c" * 64},
                    "tax_source_refs": [{
                        "source_id": "woo-tax", "path": "woocommerce-taxes.csv",
                        "sha256": "a" * 64, "row_refs": ["csv:2"],
                    }],
                },
            }
            for index in range(1, 5)
        ]
        self.assertFalse(bookchecker.evaluate_vat_profiles(batch["actions"], policy_with_24_percent_profile()))

        batch["actions"][0]["payload"]["line_items"][0].pop("vat_evidence_binding")
        report = bookchecker.evaluate_vat_profiles(batch["actions"], policy_with_24_percent_profile())
        self.assertTrue(any("evidence binding" in item["summary"] for item in report))

    def test_checker_fails_unproven_foreign_rate(self) -> None:
        action = payment_action(
            key="example-2024-01-payment-vendor",
            period="2024-01",
            amount=10.0,
            linked_purchase_action="example-2024-01-purchase-vendor",
            source_path="companies/example/artifacts/normalized/2024-01.json",
            record_ref="bank:1",
        )
        action["payload"]["currency"] = "USD"
        action["payload"]["currency_rate"] = 1

        findings = bookchecker.evaluate_exchange_rates({"actions": [action]})

        self.assertTrue(any(item["severity"] == "error" for item in findings))

    def test_checker_rejects_rate_that_does_not_match_cache(self) -> None:
        action = payment_action(
            key="example-2024-03-payment-vendor",
            period="2024-03",
            amount=10.0,
            linked_purchase_action="example-2024-03-purchase-vendor",
            source_path="companies/example/artifacts/normalized/2024-03.json",
            record_ref="bank:1",
        )
        action["payload"].update(
            {
                "currency": "USD",
                "currency_rate": 999,
                "currency_rate_provider": "ECB",
                "currency_rate_requested_date": "2024-03-31",
                "currency_rate_effective_date": "2024-03-28",
                "currency_rate_source_url": "https://api.frankfurter.dev/v2/rates?providers=ECB",
            }
        )
        cache = {
            "provider": "ECB",
            "year": 2024,
            "base": "USD",
            "quote": "EUR",
            "source_url": "https://api.frankfurter.dev/v2/rates?providers=ECB",
            "rates": [{"date": "2024-03-28", "base": "USD", "quote": "EUR", "rate": "0.92498"}],
        }

        findings = bookchecker.evaluate_exchange_rates({"actions": [action]}, exchange_rate_cache=cache)

        self.assertTrue(any("cache" in item["summary"].lower() for item in findings))

    def test_checker_rejects_rate_requested_for_different_document_date(self) -> None:
        action = payment_action(
            key="example-2024-03-payment-vendor",
            period="2024-03",
            amount=10.0,
            linked_purchase_action="example-2024-03-purchase-vendor",
            source_path="companies/example/artifacts/normalized/2024-03.json",
            record_ref="bank:1",
        )
        action["payload"].update(
            {
                "currency": "USD",
                "currency_rate": 0.92498,
                "currency_rate_provider": "ECB",
                "currency_rate_requested_date": "2024-01-31",
                "currency_rate_effective_date": "2024-01-31",
                "currency_rate_source_url": "https://api.frankfurter.dev/v2/rates?providers=ECB",
            }
        )
        cache = {
            "provider": "ECB",
            "year": 2024,
            "base": "USD",
            "quote": "EUR",
            "source_url": "https://api.frankfurter.dev/v2/rates?providers=ECB",
            "rates": [{"date": "2024-01-31", "base": "USD", "quote": "EUR", "rate": "0.92498"}],
        }

        findings = bookchecker.evaluate_exchange_rates({"actions": [action]}, exchange_rate_cache=cache)

        self.assertTrue(any("document date" in item["summary"].lower() for item in findings))

    def test_checker_fails_blocking_unresolved_dependency(self) -> None:
        findings = bookchecker.evaluate_unresolved_dependencies(
            {
                "unresolved_dependencies": [
                    {
                        "action_id": "example-2024-01-incoming-paypal",
                        "kind": "contact_mapping",
                        "blocking": True,
                        "reason": "PayPal contact requires creation.",
                    }
                ]
            }
        )

        self.assertEqual(findings[0]["severity"], "error")

    def test_checker_requires_file_bindings_for_allocated_woo_tax_actions(self) -> None:
        action = allocated_action_fixture(line_rate=24, vat_type_id="34")["actions"][0]
        action["payload"]["line_items"][0]["vat_allocation_component"] = "goods"
        action_batch = {
            "period": "2025-11",
            "actions": [action],
            "reference_artifacts": [],
        }

        findings = bookchecker.evaluate_reference_artifacts(
            action_batch,
            cwd=ROOT,
            company_dir=ROOT / "companies" / "example",
            expected_company_id="EXAMPLE-ID",
        )

        summaries = " ".join(item["summary"] for item in findings)
        self.assertIn("woo_tax_allocation", summaries)
        self.assertIn("woo_tax_source", summaries)

    def test_checker_fails_company_batch_with_temp_source_reference(self) -> None:
        action = payment_action(
            key="example-2024-01-payment-vendor",
            period="2024-01",
            amount=10.0,
            linked_purchase_action="example-2024-01-purchase-vendor",
            source_path="temp/2024/bank.csv",
            record_ref="bank:1",
        )

        findings = bookchecker.evaluate_source_locations(
            {"actions": [action]},
            company_dir=Path("companies/example"),
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "error")
        self.assertIn("canonical", findings[0]["summary"].lower())

    def test_checker_fails_cross_company_source_reference(self) -> None:
        action = payment_action(
            key="example-2024-01-payment-vendor",
            period="2024-01",
            amount=10.0,
            linked_purchase_action="example-2024-01-purchase-vendor",
            source_path="companies/another-company/artifacts/normalized/2024-01.json",
            record_ref="bank:1",
        )

        findings = bookchecker.evaluate_source_locations(
            {"actions": [action]}, company_dir=Path("companies/example")
        )

        self.assertTrue(any(item["severity"] == "error" for item in findings))

    def test_checker_rejects_absolute_and_traversal_source_references(self) -> None:
        for source_path in ("/tmp/outside.json", "../other-company/source/file.csv"):
            with self.subTest(source_path=source_path):
                action = payment_action(
                    key="example-2024-01-payment-vendor",
                    period="2024-01",
                    amount=10.0,
                    linked_purchase_action="example-2024-01-purchase-vendor",
                    source_path=source_path,
                    record_ref="bank:1",
                )
                findings = bookchecker.evaluate_source_locations(
                    {"actions": [action]},
                    company_dir=Path("companies/example"),
                    cwd=ROOT,
                    action_path=ROOT / "companies/example/artifacts/actions/2024-01.yaml",
                )
                self.assertTrue(any(item["severity"] == "error" for item in findings))

    def test_checker_accepts_company_source_fragment_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            company_dir = root / "companies" / "example"
            source_path = company_dir / "source" / "README.md"
            source_path.parent.mkdir(parents=True)
            source_path.write_text("evidence", encoding="utf-8")
            action = payment_action(
                key="example-2024-01-payment-vendor",
                period="2024-01",
                amount=10.0,
                linked_purchase_action="example-2024-01-purchase-vendor",
                source_path=f"{source_path}#invoice.jpg",
                record_ref="bank:1",
            )

            findings = bookchecker.evaluate_source_locations(
                {"actions": [action]},
                company_dir=company_dir,
                cwd=root,
                action_path=company_dir / "artifacts/actions/2024-01.yaml",
            )

        self.assertEqual(findings, [])

    def test_checker_requires_supplier_credit_contact(self) -> None:
        action = {
            "idempotency_key": "example-2024-05-purchase-credit-printful",
            "confidence": "high",
            "review_notes": [],
            "payload": {
                "draft_schema": "purchase_credit_summary_v1",
                "counterparty": {"contact_id": None},
                "line_items": [
                    {
                        "line_role": "purchase_credit",
                        "gross_amount": 11.4,
                        "vat_amount_hint": 0,
                        "suggested_expense_account_id": "257",
                        "suggested_vat_type_id": "11",
                    }
                ],
            },
        }

        findings = bookchecker.evaluate_account_vat(action=action)

        self.assertTrue(any("contact" in item["summary"].lower() for item in findings))

    def test_checker_passes_clean_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _, recon_path, action_path = build_clean_artifacts(tmp)
            action_batch = bookchecker.load_yaml(action_path)
            evaluation = bookchecker.evaluate_action_batch(
                action_batch=action_batch,
                action_path=action_path,
                recon_payload=json.loads(recon_path.read_text()),
                recon_path=recon_path,
                policy_text=None,
                cwd=tmp,
            )

        self.assertEqual(evaluation["result"], "pass")
        self.assertEqual(evaluation["error_count"], 0)
        self.assertGreaterEqual(evaluation["warning_count"], 1)

    def test_checker_fails_when_recon_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _, recon_path, action_path = build_clean_artifacts(tmp, recon_approve=False, force=True)
            action_batch = bookchecker.load_yaml(action_path)
            evaluation = bookchecker.evaluate_action_batch(
                action_batch=action_batch,
                action_path=action_path,
                recon_payload=json.loads(recon_path.read_text()),
                recon_path=recon_path,
                policy_text=None,
                cwd=tmp,
            )

        self.assertEqual(evaluation["result"], "fail")
        self.assertTrue(any("Reconciliation does not approve" in item["summary"] for item in evaluation["findings"]))

    def test_checker_detects_duplicate_idempotency_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _, recon_path, action_path = build_clean_artifacts(tmp)
            action_batch = bookchecker.load_yaml(action_path)
            action_batch["actions"].append(dict(action_batch["actions"][0]))
            evaluation = bookchecker.evaluate_action_batch(
                action_batch=action_batch,
                action_path=action_path,
                recon_payload=json.loads(recon_path.read_text()),
                recon_path=recon_path,
                policy_text=None,
                cwd=tmp,
            )

        self.assertEqual(evaluation["result"], "fail")
        self.assertTrue(any("idempotency_key" in item["summary"] for item in evaluation["findings"]))

    def test_checker_detects_arithmetic_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _, recon_path, action_path = build_clean_artifacts(tmp)
            action_batch = bookchecker.load_yaml(action_path)
            action_batch["actions"][0]["payload"]["totals"]["gross_amount"] = 25.0
            evaluation = bookchecker.evaluate_action_batch(
                action_batch=action_batch,
                action_path=action_path,
                recon_payload=json.loads(recon_path.read_text()),
                recon_path=recon_path,
                policy_text=None,
                cwd=tmp,
            )

        self.assertEqual(evaluation["result"], "fail")
        self.assertTrue(any("gross total" in item["summary"].lower() for item in evaluation["findings"]))

    def test_checker_allows_split_payment_allocation_when_group_total_matches_bank_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            normalized = base_normalized("2024-03")
            normalized["records"]["bank_transactions"].append(
                record(
                    record_id="bank:jajaa:settlement",
                    source_system="bank",
                    event_type="bank_debit",
                    gross_amount=-17.0,
                    description="Example Vendor settlement",
                )
            )
            recon = base_recon("2024-03")
            normalized_path = tmp / "normalized.json"
            recon_path = tmp / "recon.json"
            action_path = tmp / "actions.yaml"
            normalized_path.write_text(json.dumps(normalized), encoding="utf-8")
            recon_path.write_text(json.dumps(recon), encoding="utf-8")

            action_batch = {
                "schema_version": "1.0",
                "company_slug": "example",
                "period": "2024-03",
                "generated_at": "2026-04-04T00:00:00Z",
                "batch_id": "example-2024-03-draft",
                "approval_status": "draft",
                "source_summary": "test",
                "recon_ref": "recon.json",
                "actions": [
                    payment_action(
                        key="example-2024-03-payment-jajaa-2024-01",
                        period="2024-03",
                        amount=12.0,
                        linked_purchase_action="example-2024-01-purchase-jajaa",
                        source_path="normalized.json",
                        record_ref="bank:jajaa:settlement",
                    ),
                    payment_action(
                        key="example-2024-03-payment-jajaa-2024-02",
                        period="2024-03",
                        amount=5.0,
                        linked_purchase_action="example-2024-02-purchase-jajaa",
                        source_path="normalized.json",
                        record_ref="bank:jajaa:settlement",
                    ),
                ],
            }
            bookbuilder.write_yaml(action_path, action_batch)

            evaluation = bookchecker.evaluate_action_batch(
                action_batch=action_batch,
                action_path=action_path,
                recon_payload=json.loads(recon_path.read_text()),
                recon_path=recon_path,
                policy_text=None,
                cwd=tmp,
            )

        self.assertEqual(evaluation["result"], "pass")
        self.assertEqual(evaluation["error_count"], 0)


if __name__ == "__main__":
    unittest.main()
