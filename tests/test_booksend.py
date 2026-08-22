from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import booksend  # noqa: E402
import reference_artifacts  # noqa: E402
from simplbooks_api import SimplbooksError  # noqa: E402


def invoice_action(
    *,
    key: str = "example-2024-01-sales-paypal",
    response_status: int | None = None,
    executed_at: str | None = None,
    response_body: dict | None = None,
    inserted_id: str | int | None = None,
) -> dict:
    return {
        "idempotency_key": key,
        "period": "2024-01",
        "action_type": "create_invoice_summary",
        "method": "POST",
        "endpoint": "invoices/create",
        "payload": {
            "draft_schema": "invoice_summary_v1",
            "document_type": "invoice",
            "document_date": "2024-01-31",
            "currency": "EUR",
            "counterparty": {
                "contact_id": "2001",
                "display_name_hint": "Monthly paypal sales summary",
            },
            "totals": {
                "gross_amount": 120.0,
                "vat_amount": 20.0,
                "shipping_amount": 10.0,
            },
            "line_items": [
                {
                    "line_role": "sales_revenue",
                    "description": "paypal taxable sales summary",
                    "gross_amount": 110.0,
                    "vat_amount_hint": 20.0,
                    "suggested_income_account_id": "3000",
                    "suggested_vat_type_id": "22",
                    "warehouse_id_hint": None,
                },
                {
                    "line_role": "sales_shipping",
                    "description": "paypal shipping summary",
                    "gross_amount": 10.0,
                    "vat_amount_hint": 0.0,
                    "suggested_income_account_id": "3010",
                    "suggested_vat_type_id": "0",
                    "warehouse_id_hint": None,
                },
            ],
        },
        "source_refs": [
            {
                "path": "companies/example/artifacts/normalized/2024-01.json",
                "record_ref": "paypal:sale:1",
                "note": None,
            }
        ],
        "reason": "test",
        "confidence": "high",
        "depends_on": [],
        "expected_effect": "create invoice",
        "review_notes": [],
        "executed_at": executed_at,
        "response_status": response_status,
        "response_body": response_body,
        "inserted_id": inserted_id,
    }


def purchase_action(*, key: str = "example-2024-01-purchase-printful") -> dict:
    return {
        "idempotency_key": key,
        "period": "2024-01",
        "action_type": "create_purchase_summary",
        "method": "POST",
        "endpoint": "purchases/create",
        "payload": {
            "draft_schema": "purchase_summary_v1",
            "document_type": "purchase",
            "document_date": "2024-01-31",
            "currency": "EUR",
            "counterparty": {
                "contact_id": "2002",
                "display_name_hint": "printful purchase summary",
            },
            "totals": {
                "gross_amount": 30.0,
                "vat_amount": 0.0,
            },
            "line_items": [
                {
                    "line_role": "purchase_expense",
                    "description": "printful fulfillment summary",
                    "gross_amount": 30.0,
                    "vat_amount_hint": 0.0,
                    "suggested_expense_account_id": "6020",
                    "suggested_vat_type_id": "0",
                    "article_id_hint": None,
                }
            ],
        },
        "source_refs": [
            {
                "path": "companies/example/artifacts/normalized/2024-01.json",
                "record_ref": "printful:expense:1",
                "note": None,
            }
        ],
        "reason": "test",
        "confidence": "high",
        "depends_on": [],
        "expected_effect": "create purchase",
        "review_notes": [],
        "executed_at": None,
        "response_status": None,
        "response_body": None,
        "inserted_id": None,
    }


def purchase_credit_action() -> dict:
    action = purchase_action(key="example-2024-07-purchase-credit-printful")
    action["period"] = "2024-07"
    action["action_type"] = "create_purchase_credit_summary"
    action["payload"]["draft_schema"] = "purchase_credit_summary_v1"
    action["payload"]["document_type"] = "purchase_credit"
    action["payload"]["document_date"] = "2024-07-31"
    action["payload"]["currency"] = "USD"
    action["payload"]["currency_rate"] = 0.9241
    action["payload"]["currency_rate_provider"] = "ECB"
    action["payload"]["currency_rate_effective_date"] = "2024-07-31"
    action["payload"]["totals"]["gross_amount"] = 113.12
    action["payload"]["line_items"][0].update(
        {
            "line_role": "purchase_credit",
            "gross_amount": 113.12,
            "warehouse_id_hint": None,
        }
    )
    return action


def incoming_action(
    *,
    key: str = "example-2024-01-incoming-paypal",
    depends_on: list[str] | None = None,
    response_status: int | None = None,
    executed_at: str | None = None,
    response_body: dict | None = None,
    inserted_id: str | int | None = None,
) -> dict:
    return {
        "idempotency_key": key,
        "period": "2024-01",
        "action_type": "create_incoming_summary",
        "method": "POST",
        "endpoint": "incomings/create",
        "payload": {
            "draft_schema": "cash_settlement_v1",
            "document_type": "incoming",
            "document_date": "2024-01-31",
            "currency": "EUR",
            "counterparty": {
                "contact_id": "2001",
                "display_name_hint": "paypal incoming summary",
            },
            "counterparty_hint": "paypal",
            "bank_account_id": "101",
            "amount": 116.0,
            "record_count": 1,
        },
        "source_refs": [
            {
                "path": "companies/example/artifacts/normalized/2024-01.json",
                "record_ref": "paypal:payout:1",
                "note": None,
            }
        ],
        "reason": "test",
        "confidence": "high",
        "depends_on": depends_on or ["example-2024-01-sales-paypal"],
        "expected_effect": "create incoming",
        "review_notes": [],
        "executed_at": executed_at,
        "response_status": response_status,
        "response_body": response_body,
        "inserted_id": inserted_id,
    }


def payment_action(
    *,
    key: str = "example-2024-01-payment-printful",
    depends_on: list[str] | None = None,
) -> dict:
    return {
        "idempotency_key": key,
        "period": "2024-01",
        "action_type": "create_payment_summary",
        "method": "POST",
        "endpoint": "payments/create",
        "payload": {
            "draft_schema": "cash_settlement_v1",
            "document_type": "payment",
            "document_date": "2024-01-31",
            "currency": "EUR",
            "counterparty": {
                "contact_id": "2002",
                "display_name_hint": "printful payment summary",
            },
            "counterparty_hint": "printful",
            "bank_account_id": "101",
            "amount": 30.0,
            "linked_purchase_action": "example-2024-01-purchase-printful",
            "record_count": 1,
        },
        "source_refs": [
            {
                "path": "companies/example/artifacts/normalized/2024-01.json",
                "record_ref": "bank:printful:1",
                "note": None,
            }
        ],
        "reason": "test",
        "confidence": "high",
        "depends_on": depends_on or ["example-2024-01-purchase-printful"],
        "expected_effect": "create payment",
        "review_notes": [],
        "executed_at": None,
        "response_status": None,
        "response_body": None,
        "inserted_id": None,
    }


def existing_invoice_incoming() -> dict:
    action = incoming_action(depends_on=[])
    action["depends_on"] = []
    action["payload"]["linked_invoice_id"] = "119"
    return action


def existing_purchase_payment() -> dict:
    action = payment_action(depends_on=[])
    action["depends_on"] = []
    action["payload"].pop("linked_purchase_action")
    action["payload"]["linked_purchase_id"] = "88"
    return action


def make_batch(*, approval_status: str = "approved", actions: list[dict] | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "period": "2024-01",
        "generated_at": "2026-04-04T00:00:00Z",
        "batch_id": "example-2024-01-draft",
        "approval_status": approval_status,
        "source_summary": "test",
        "recon_ref": "companies/example/artifacts/recon/2024-01.json",
        "actions": actions or [invoice_action(), incoming_action()],
    }


class FakeClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        self.calls.append({"path": path, "method": method, "payload": payload})
        if not self.responses:
            raise AssertionError("No fake responses left for request")
        return self.responses.pop(0)


class BooksendTests(unittest.TestCase):
    def test_sender_rejects_existing_invoice_id_with_generated_invoice_dependency(self) -> None:
        action = existing_invoice_incoming()
        action["depends_on"] = ["example-2024-01-sales-paypal"]

        with self.assertRaisesRegex(SimplbooksError, "both linked_invoice_id and generated invoice dependency"):
            booksend.translate_cash_settlement_payload(
                action,
                lookup={"example-2024-01-sales-paypal": invoice_action()},
            )

    def test_sender_accepts_bound_prior_year_discovery_overview(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            discovery_path = root / "2023-overview.json"
            discovery_path.write_text(
                json.dumps({
                    "year": 2023,
                    "company_id": "CID",
                    "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }),
                encoding="utf-8",
            )
            policy_path = root / "posting-policy.json"
            policy_path.write_text("{}", encoding="utf-8")
            batch = make_batch(actions=[invoice_action()])
            batch["reference_artifacts"] = [
                reference_artifacts.bind_file(policy_path, kind="posting_policy", cwd=root),
                reference_artifacts.bind_file(discovery_path, kind="discovery_overview", cwd=root),
            ]

            booksend.verify_submission_reference_artifacts(
                batch,
                cwd=root,
                period="2024-01",
                company_id="CID",
            )

    def test_translate_incoming_accepts_existing_invoice_id(self) -> None:
        translated = booksend.translate_cash_settlement_payload(existing_invoice_incoming(), lookup={})

        self.assertEqual(translated["payload"]["invoice_id"], 119)

    def test_translate_payment_accepts_existing_purchase_id(self) -> None:
        translated = booksend.translate_cash_settlement_payload(existing_purchase_payment(), lookup={})

        self.assertEqual(translated["payload"]["purchase_id"], 88)

    def test_sender_rejects_manual_inventory_writeoff_before_translation(self) -> None:
        action = invoice_action()
        action["action_type"] = "manual_inventory_writeoff"

        with self.assertRaisesRegex(SimplbooksError, "manual inventory"):
            booksend.translate_action(action, lookup={})

        client = FakeClient(responses=[])
        with self.assertRaisesRegex(SimplbooksError, "manual inventory"):
            booksend.execute_batch(action_batch=make_batch(actions=[action]), mode="write", client=client)
        self.assertEqual(client.calls, [])

    def test_sender_prevalidates_entire_batch_before_any_client_call(self) -> None:
        manual_action = invoice_action(key="example-2024-01-manual-inventory")
        manual_action["action_type"] = "manual_inventory_writeoff"

        for mode in ("dry-run", "write"):
            with self.subTest(mode=mode):
                client = FakeClient(responses=[{"_http_status": 201, "invoice_id": 501}])
                with self.assertRaisesRegex(SimplbooksError, "manual inventory"):
                    booksend.execute_batch(
                        action_batch=make_batch(actions=[invoice_action(), manual_action]),
                        mode=mode,
                        client=client,
                    )
                self.assertEqual(client.calls, [])

    def test_sender_copies_reviewed_rate_and_translates_supplier_credit(self) -> None:
        translated = booksend.translate_action_for_api(purchase_credit_action(), lookup={})

        self.assertEqual(translated["payload"]["Purchase"]["currency_rate"], 0.9241)
        self.assertEqual(translated["payload"]["PurchaseRows"][0]["PurchaseRow"]["sum"], -113.12)

    def test_sender_preserves_reviewed_rate_and_per_order_rounding_lines(self) -> None:
        action = invoice_action()
        action["payload"]["totals"].update({"gross_amount": 0.06, "vat_amount": 0.02, "shipping_amount": 0.0})
        action["payload"]["line_items"] = [
            {
                "line_role": "sales_revenue",
                "description": f"Woo order EXAMPLE-{index} goods",
                "gross_amount": 0.03,
                "vat_amount_hint": 0.01,
                "suggested_income_account_id": "3000",
                "suggested_vat_type_id": "34",
                "warehouse_id_hint": None,
                "vat_allocation_component": "goods",
                "vat_profile_rate": 24,
                "vat_profile_period": "2025-07-01/open",
                "vat_allocation_component_evidence": [
                    {
                        "order_id": f"EXAMPLE-{index}", "gross_amount": 0.03, "vat_amount": 0.01,
                        "event_date": "2025-11-27",
                        "vat_profile": {
                            "start": "2025-01-01", "end": "2025-12-31", "rate": 24,
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
            for index in (1, 2)
        ]

        translated = booksend.translate_action_for_api(action, lookup={})
        tasks = [item["Task"] for item in translated["payload"]["Tasks"]]

        self.assertEqual([task["vat"] for task in tasks], [24.0, 24.0])
        self.assertEqual([task["price_per_unit"] for task in tasks], [0.03, 0.03])

        unbound = copy.deepcopy(action)
        unbound["payload"]["line_items"][0].pop("vat_evidence_binding")
        with self.assertRaisesRegex(SimplbooksError, "evidence binding"):
            booksend.translate_action_for_api(unbound, lookup={})

        action["payload"]["line_items"] = [{
            **action["payload"]["line_items"][0],
            "gross_amount": 0.06,
            "vat_amount_hint": 0.02,
            "vat_allocation_component_evidence": [
                {"order_id": "EXAMPLE-1", "gross_amount": 0.03, "vat_amount": 0.01},
                {"order_id": "EXAMPLE-2", "gross_amount": 0.03, "vat_amount": 0.01},
            ],
        }]
        with self.assertRaisesRegex(SimplbooksError, "one order component"):
            booksend.translate_action_for_api(action, lookup={})

    def test_write_reference_verification_requires_woo_tax_bindings(self) -> None:
        action = invoice_action()
        action["payload"]["line_items"][0]["vat_allocation_component"] = "goods"
        batch = make_batch(actions=[action])
        batch["reference_artifacts"] = []

        with self.assertRaisesRegex(SimplbooksError, "woo_tax_allocation"):
            booksend.verify_submission_reference_artifacts(
                batch,
                cwd=ROOT,
                period="2024-01",
                company_id="EXAMPLE-ID",
            )

    def test_sender_rejects_foreign_action_without_reviewed_rate(self) -> None:
        action = purchase_action()
        action["payload"]["currency"] = "USD"

        with self.assertRaisesRegex(SimplbooksError, "reviewed currency_rate"):
            booksend.translate_action_for_api(action, lookup={})

    def test_sender_rejects_reviewed_rate_that_differs_from_cache(self) -> None:
        action = purchase_credit_action()
        action["payload"]["currency_rate"] = 999
        action["payload"]["currency_rate_requested_date"] = "2024-07-31"
        action["payload"]["currency_rate_effective_date"] = "2024-07-31"
        action["payload"]["currency_rate_source_url"] = "https://api.frankfurter.dev/v2/rates?providers=ECB"
        cache = {
            "provider": "ECB",
            "year": 2024,
            "base": "USD",
            "quote": "EUR",
            "source_url": "https://api.frankfurter.dev/v2/rates?providers=ECB",
            "rates": [{"date": "2024-07-31", "base": "USD", "quote": "EUR", "rate": "0.92"}],
        }

        with self.assertRaisesRegex(SimplbooksError, "does not match"):
            booksend.translate_action_for_api(action, lookup={}, exchange_rate_cache=cache)

    def test_sender_rejects_rate_requested_for_different_document_date(self) -> None:
        action = purchase_credit_action()
        action["payload"]["currency_rate_requested_date"] = "2024-01-31"
        action["payload"]["currency_rate_effective_date"] = "2024-01-31"
        source_url = "https://api.frankfurter.dev/v2/rates?providers=ECB"
        action["payload"]["currency_rate_source_url"] = source_url
        cache = {
            "provider": "ECB",
            "year": 2024,
            "base": "USD",
            "quote": "EUR",
            "source_url": source_url,
            "rates": [{"date": "2024-01-31", "base": "USD", "quote": "EUR", "rate": "0.9241"}],
        }

        with self.assertRaisesRegex(SimplbooksError, "document date"):
            booksend.translate_action_for_api(action, lookup={}, exchange_rate_cache=cache)

    def test_sender_rejects_inventory_linked_supplier_credit(self) -> None:
        action = purchase_credit_action()
        action["payload"]["line_items"][0]["warehouse_id_hint"] = "6"

        with self.assertRaisesRegex(SimplbooksError, "inventory-linked"):
            booksend.translate_action_for_api(action, lookup={})

    def test_dry_run_updates_actions_and_uses_translated_payloads(self) -> None:
        batch = make_batch(approval_status="draft")

        updated_batch, submission = booksend.execute_batch(
            action_batch=copy.deepcopy(batch),
            mode="dry-run",
        )

        self.assertEqual(updated_batch["approval_status"], "draft")
        self.assertEqual(submission["mode"], "dry-run")
        self.assertEqual(submission["summary"]["attempted_actions"], 2)
        self.assertEqual(submission["summary"]["successful_actions"], 2)
        self.assertEqual(submission["summary"]["failed_actions"], 0)
        self.assertEqual(len(submission["request_log"]), 2)
        self.assertEqual(submission["request_log"][0]["endpoint"], "invoices/create")
        self.assertEqual(submission["request_log"][0]["payload"]["Invoice"]["client_id"], 2001)
        self.assertEqual(submission["request_log"][1]["endpoint"], "incomings/create")
        self.assertEqual(submission["request_log"][1]["payload"]["invoice_id"], 0)
        self.assertEqual(updated_batch["actions"][0]["response_status"], 0)
        self.assertTrue(updated_batch["actions"][0]["response_body"]["dry_run"])
        self.assertFalse(submission["rollback_plan"]["supported"])
        self.assertEqual(submission["rollback_plan"]["reversal_candidates"], [])

    def test_dry_run_resolves_payment_purchase_id_from_prior_action_lookup(self) -> None:
        prior_purchase = purchase_action(key="example-2024-01-purchase-jajaa")
        prior_purchase["payload"]["counterparty"]["display_name_hint"] = "jajaa purchase summary"
        prior_purchase["payload"]["vendor_hint"] = "jajaa"
        prior_purchase["inserted_id"] = 712

        action = payment_action(key="example-2024-03-payment-jajaa", depends_on=[])
        action["period"] = "2024-03"
        action["depends_on"] = []
        action["payload"]["document_date"] = "2024-03-31"
        action["payload"]["counterparty"]["display_name_hint"] = "jajaa payment summary"
        action["payload"]["counterparty_hint"] = "jajaa"
        action["payload"]["amount"] = 12.0
        action["payload"]["linked_purchase_action"] = "example-2024-01-purchase-jajaa"

        batch = make_batch(actions=[action])
        batch["period"] = "2024-03"
        batch["batch_id"] = "example-2024-03-draft"

        _, submission = booksend.execute_batch(
            action_batch=copy.deepcopy(batch),
            mode="dry-run",
            reference_lookup={"example-2024-01-purchase-jajaa": prior_purchase},
        )

        self.assertEqual(submission["request_log"][0]["endpoint"], "payments/create")
        self.assertEqual(submission["request_log"][0]["payload"]["purchase_id"], 712)

    def test_prior_lookup_uses_successful_submission_id_without_mutating_action_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            company_dir = root / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            submissions_dir = company_dir / "artifacts" / "submissions"
            actions_dir.mkdir(parents=True)
            submissions_dir.mkdir(parents=True)
            prior = purchase_action(key="example-2024-01-purchase-prior")
            booksend.write_yaml(actions_dir / "2024-01.yaml", {"actions": [prior]})
            booksend.write_json(
                submissions_dir / "2024-01.json",
                {
                    "request_log": [{
                        "action_idempotency_key": "example-2024-01-purchase-prior",
                        "mode": "write",
                        "success": True,
                        "inserted_id": 712,
                    }]
                },
            )

            lookup = booksend.load_prior_action_lookup(
                company_dir=company_dir,
                action_path=actions_dir / "2024-03.yaml",
                period="2024-03",
            )

            self.assertEqual(lookup["example-2024-01-purchase-prior"]["inserted_id"], 712)
            self.assertIsNone(prior["inserted_id"])

    def test_run_submission_reports_only_current_run_api_calls_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            company_dir = tmp / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            submissions_dir = company_dir / "artifacts" / "submissions"
            actions_dir.mkdir(parents=True)
            submissions_dir.mkdir(parents=True)

            action_path = actions_dir / "2024-01.yaml"
            batch = make_batch(approval_status="draft")
            booksend.write_yaml(action_path, batch)
            action_sha = booksend.file_sha256(action_path)
            (actions_dir / "2024-01.check.md").write_text(
                "\n".join(
                    [
                        "- Result: `pass`",
                        f"- Batch ID: `{batch['batch_id']}`",
                        f"- Action file SHA256: `{action_sha}`",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            first = booksend.run_submission(
                period="2024-01",
                company_dir=company_dir,
                company_id=None,
                action_override=None,
                check_override=None,
                output_override=None,
                request_log_override=None,
                token_file=".apikey",
                mode="dry-run",
                confirm_write=False,
                continue_on_error=False,
                cwd=tmp,
            )
            second = booksend.run_submission(
                period="2024-01",
                company_dir=company_dir,
                company_id=None,
                action_override=None,
                check_override=None,
                output_override=None,
                request_log_override=None,
                token_file=".apikey",
                mode="dry-run",
                confirm_write=False,
                continue_on_error=False,
                cwd=tmp,
            )

            submission = booksend.load_json(submissions_dir / "2024-01.json")

        self.assertEqual(len(first["api_calls"]), 2)
        self.assertEqual(len(second["api_calls"]), 2)
        self.assertEqual(len(submission["request_log"]), 4)
        self.assertEqual(second["api_calls"][0]["endpoint"], "invoices/create")
        self.assertEqual(second["api_calls"][1]["endpoint"], "incomings/create")

    def test_load_prior_action_lookup_reads_earlier_action_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            company_dir = tmp / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            actions_dir.mkdir(parents=True)

            booksend.write_yaml(
                actions_dir / "2024-01.yaml",
                {
                    "actions": [
                        purchase_action(key="example-2024-01-purchase-jajaa"),
                    ]
                },
            )
            current_action_path = actions_dir / "2024-03.yaml"
            booksend.write_yaml(current_action_path, {"actions": [payment_action(key="example-2024-03-payment-jajaa")]})

            lookup = booksend.load_prior_action_lookup(
                company_dir=company_dir,
                action_path=current_action_path,
                period="2024-03",
            )

        self.assertIn("example-2024-01-purchase-jajaa", lookup)
        self.assertNotIn("example-2024-03-payment-jajaa", lookup)

    def test_write_mode_preconditions_require_confirmation_and_fresh_check_binding(self) -> None:
        batch = make_batch(approval_status="approved")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            action_path = tmp / "actions.yaml"
            action_path.write_text("batch: current\n", encoding="utf-8")
            check_path = tmp / "actions.check.md"

            with self.assertRaisesRegex(SimplbooksError, "confirm-write"):
                booksend.validate_run_preconditions(
                    action_batch=batch,
                    action_path=action_path,
                    period="2024-01",
                    mode="write",
                    confirm_write=False,
                    check_report={"result": "pass", "batch_id": batch["batch_id"], "action_file_sha256": booksend.file_sha256(action_path)},
                    check_path=check_path,
                )

            with self.assertRaisesRegex(SimplbooksError, "fresh check report"):
                booksend.validate_run_preconditions(
                    action_batch=batch,
                    action_path=action_path,
                    period="2024-01",
                    mode="write",
                    confirm_write=True,
                    check_report={"result": "pass", "batch_id": "stale-batch", "action_file_sha256": booksend.file_sha256(action_path)},
                    check_path=check_path,
                )

            with self.assertRaisesRegex(SimplbooksError, "current action file contents"):
                booksend.validate_run_preconditions(
                    action_batch=batch,
                    action_path=action_path,
                    period="2024-01",
                    mode="write",
                    confirm_write=True,
                    check_report={"result": "pass", "batch_id": batch["batch_id"], "action_file_sha256": "0" * 64},
                    check_path=check_path,
                )

    def test_write_stops_on_first_failure_in_stable_order(self) -> None:
        batch = make_batch(
            actions=[
                invoice_action(),
                purchase_action(),
                payment_action(),
            ]
        )
        client = FakeClient(
            responses=[
                {"_http_status": 201, "invoice_id": 501},
                {"_http_status": 400, "error": "bad payload"},
            ]
        )

        updated_batch, submission = booksend.execute_batch(
            action_batch=copy.deepcopy(batch),
            mode="write",
            client=client,
        )

        self.assertEqual([call["path"] for call in client.calls], ["invoices/create", "purchases/create"])
        self.assertEqual(submission["summary"]["attempted_actions"], 2)
        self.assertEqual(submission["summary"]["successful_actions"], 1)
        self.assertEqual(submission["summary"]["failed_actions"], 1)
        self.assertTrue(submission["summary"]["stopped_on_failure"])
        self.assertTrue(submission["request_log"][-1]["stopped_batch"])
        self.assertEqual(updated_batch["actions"][2]["executed_at"], None)
        self.assertEqual(updated_batch["approval_status"], "approved")

    def test_write_rerun_skips_successful_actions_and_marks_batch_submitted(self) -> None:
        prior_success = {
            "_http_status": 201,
            "invoice_id": 501,
        }
        batch = make_batch(
            actions=[
                invoice_action(
                    response_status=201,
                    executed_at="2026-04-04T00:00:00Z",
                    response_body=prior_success,
                    inserted_id=501,
                ),
                incoming_action(),
            ]
        )
        client = FakeClient(
            responses=[
                {"_http_status": 201, "incoming_id": 601},
            ]
        )

        updated_batch, submission = booksend.execute_batch(
            action_batch=copy.deepcopy(batch),
            mode="write",
            client=client,
        )

        self.assertEqual([call["path"] for call in client.calls], ["incomings/create"])
        self.assertEqual(client.calls[0]["payload"]["invoice_id"], 501)
        self.assertEqual(updated_batch["approval_status"], "submitted")
        self.assertEqual(submission["summary"]["attempted_actions"], 1)
        self.assertEqual(submission["summary"]["successful_actions"], 1)
        self.assertEqual(len(submission["rollback_plan"]["reversal_candidates"]), 2)

        candidates = {
            item["action_idempotency_key"]: item
            for item in submission["rollback_plan"]["reversal_candidates"]
        }
        self.assertIn("example-2024-01-incoming-paypal", candidates)
        self.assertIn("example-2024-01-sales-paypal", candidates)
        self.assertEqual(candidates["example-2024-01-incoming-paypal"]["depends_on"], [])
        self.assertEqual(candidates["example-2024-01-sales-paypal"]["depends_on"], ["example-2024-01-incoming-paypal"])


if __name__ == "__main__":
    unittest.main()
