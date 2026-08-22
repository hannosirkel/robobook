from __future__ import annotations  # noqa: I001

import copy
import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookbuilder  # noqa: E402, I001
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


def manual_financial_dependency(*, blocking: bool = False, status: str = "verified") -> dict:
    proof = {"status": status, "required_evidence": "live_discovery_or_audit"}
    if status == "verified":
        proof.update({
            "simplbooks_transaction_id": "txn-501",
            "evidence_binding": {"path": "evidence.json", "sha256": "a" * 64},
        })
    return {
        "kind": "manual_statement_import_financial_transaction",
        "blocking": blocking,
        "reason": "Statement import required.",
        "disposition": "bank_fee_payment",
        "statement_id": "archive:fee-1",
        "record_id": "bank:fee:1",
        "date": "2024-01-15",
        "iban": "EE123",
        "currency": "EUR",
        "physical_signed_amount": -7.0,
        "source_ref": {"path": "normalized.json", "record_ref": "bank:fee:1", "source_kind": "physical_bank"},
        "reviewed_rationale": "Reviewed bank fee.",
        "target": {"financial_transaction_kind": "bank-fee"},
        "split_parts": [],
        "split_proof": None,
        "statement_import_proof": proof,
    }


def physical_bank_record(*, record_id: str, amount: float, event_date: str = "2024-01-15") -> dict:
    item = record(
        record_id=record_id,
        source_system="bank",
        event_type="bank_credit" if amount > 0 else "bank_debit",
        gross_amount=amount,
    )
    item.update({
        "event_date": event_date,
        "settlement_date": event_date,
        "attributes": {"iban": "EE123", "archive_identifier": record_id},
    })
    return item


def write_bank_coverage_fixture(tmp: Path, rows: list[dict]) -> tuple[Path, Path]:
    normalized = base_normalized(rows[0]["event_date"][:7] if rows else "2024-01")
    normalized["records"]["bank_transactions"] = rows
    normalized_path = tmp / "normalized.json"
    normalized_path.write_text(json.dumps(normalized), encoding="utf-8")
    normalized_sha = hashlib.sha256(normalized_path.read_bytes()).hexdigest()
    allocations = {
        "schema_version": "1.0",
        "company_slug": "example",
        "year": 2024,
        "normalized_bindings": [{"path": str(normalized_path), "sha256": normalized_sha}],
        "allocations": [
            {
                "statement_id": f"archive:{row['record_id']}",
                "record_id": row["record_id"],
                "iban": "EE123",
                "period": row["event_date"][:7],
                "disposition": "existing_invoice_receipt" if row["gross_amount"] > 0 else "existing_purchase_payment",
                "amount": row["gross_amount"],
                "currency": "EUR",
                "target": {
                    "simplbooks_id": "119",
                    "document_type": "invoice" if row["gross_amount"] > 0 else "purchase",
                },
                "review": {"status": "approved", "rationale": "Exact reviewed settlement."},
            }
            for row in rows
        ],
    }
    allocation_path = tmp / "bank-allocations.json"
    allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
    return normalized_path, allocation_path


def exact_settlement_action(*, row: dict, normalized_path: Path) -> dict:
    incoming = row["gross_amount"] > 0
    return {
        "idempotency_key": f"settlement-{row['record_id']}",
        "action_type": "create_incoming_summary" if incoming else "create_payment_summary",
        "payload": {
            "draft_schema": "cash_settlement_v1",
            "document_type": "incoming" if incoming else "payment",
            "document_date": row["event_date"],
            "currency": row["currency"],
            "bank_account_id": "3",
            "amount": abs(row["gross_amount"]),
            ("linked_invoice_id" if incoming else "linked_purchase_id"): "119",
        },
        "source_refs": [{
            "path": str(normalized_path),
            "record_ref": row["record_id"],
            "source_kind": "physical_bank",
        }],
        "confidence": "high",
    }


def bank_coverage_batch(*, period: str, allocation_path: Path, actions: list[dict], dependencies: list[dict] | None = None) -> dict:
    return {
        "company_slug": "example",
        "period": period,
        "approval_status": "approved",
        "reference_artifacts": [{
            "kind": "bank_allocations",
            "path": str(allocation_path),
            "sha256": hashlib.sha256(allocation_path.read_bytes()).hexdigest(),
        }],
        "actions": actions,
        "unresolved_dependencies": dependencies or [],
    }


class BookcheckerTests(unittest.TestCase):
    def test_inventory_quantity_checker_rejects_invoice_with_refund_scope(self) -> None:
        record = {
            "record_id": "woo:refund:1", "quantity": 1, "gross_amount": -20.0,
            "event_date": "2024-01-15", "currency": "EUR", "channel": "woo", "vat_amount": 0,
        }
        proof = bookbuilder.normalized_inventory_quantity_proof(
            [record], group_label="woo", direction="refunds"
        )
        action = {
            "idempotency_key": "example-2024-01-wrong-refund-scope",
            "action_type": "create_invoice_summary",
            "payload": {"document_type": "invoice", "line_items": [{
                "line_role": "sales_revenue", "article_id_hint": "3", "quantity": 1,
                "inventory_quantity_proof": proof,
            }]},
        }
        normalized = base_normalized()
        normalized["records"]["refunds"] = [record]

        findings = bookchecker.evaluate_inventory_quantities(
            action=action,
            resolved_sources=[{"record_ref": record["record_id"], "record": record, "payload": normalized}],
            reviewed_allocations={},
        )

        self.assertTrue(any("action contract" in item["summary"] for item in findings), findings)

    def test_inventory_quantity_checker_rejects_credit_note_with_sales_scope(self) -> None:
        record = {
            "record_id": "woo:sale:wrong-credit", "quantity": 1, "gross_amount": 20.0,
            "event_date": "2024-01-15", "currency": "EUR", "channel": "woo", "vat_amount": 0,
        }
        proof = bookbuilder.normalized_inventory_quantity_proof(
            [record], group_label="woo", direction="sales"
        )
        action = {
            "idempotency_key": "example-2024-01-wrong-sales-scope",
            "action_type": "create_credit_invoice_summary",
            "payload": {"document_type": "credit_note", "line_items": [{
                "line_role": "refund_revenue", "article_id_hint": "3", "quantity": 1,
                "inventory_quantity_proof": proof,
            }]},
        }
        normalized = base_normalized()
        normalized["records"]["sales"] = [record]

        findings = bookchecker.evaluate_inventory_quantities(
            action=action,
            resolved_sources=[{"record_ref": record["record_id"], "record": record, "payload": normalized}],
            reviewed_allocations={},
        )

        self.assertTrue(any("action contract" in item["summary"] for item in findings), findings)

    def test_inventory_quantity_checker_rejects_tampered_normalized_contributor(self) -> None:
        record = {
            "record_id": "woo:sale:1", "quantity": 2, "gross_amount": 40.0,
            "event_date": "2024-01-15", "currency": "EUR", "channel": "woo", "vat_amount": 0,
        }
        proof = bookbuilder.normalized_inventory_quantity_proof(
            [record], group_label="woo", direction="sales"
        )
        proof["contributors"][0]["record_sha256"] = "0" * 64
        action = {"idempotency_key": "example-2024-01-inventory", "payload": {"line_items": [{
            "line_role": "sales_revenue", "article_id_hint": "3", "quantity": 2,
            "inventory_quantity_proof": proof,
        }]}}
        normalized = base_normalized()
        normalized["records"]["sales"] = [record]

        findings = bookchecker.evaluate_inventory_quantities(
            action=action,
            resolved_sources=[{"record_ref": "woo:sale:1", "record": record, "payload": normalized}],
            reviewed_allocations={},
        )

        self.assertTrue(any("SHA-256" in item["summary"] for item in findings))

    def test_inventory_quantity_checker_uses_builder_canonical_unicode_hash(self) -> None:
        record = {
            "record_id": "woo:sale:unicode", "quantity": 1, "description": "Lunar mäng",
            "event_date": "2024-01-15", "currency": "EUR", "channel": "woo", "vat_amount": 0,
        }
        proof = bookbuilder.normalized_inventory_quantity_proof(
            [record], group_label="woo", direction="sales"
        )
        action = {"idempotency_key": "example-2024-01-unicode", "action_type": "create_invoice_summary", "payload": {"document_type": "invoice", "line_items": [{
            "line_role": "sales_revenue", "article_id_hint": "3", "quantity": 1,
            "inventory_quantity_proof": proof,
        }]}}
        normalized = base_normalized()
        normalized["records"]["sales"] = [record]

        findings = bookchecker.evaluate_inventory_quantities(
            action=action,
            resolved_sources=[{"record_ref": record["record_id"], "record": record, "payload": normalized}],
            reviewed_allocations={},
        )

        self.assertEqual(findings, [])

    def test_inventory_quantity_checker_rejects_omitted_record_from_declared_group(self) -> None:
        first = record(record_id="woo:first", source_system="woo", channel="woo",
                       event_type="woo_sale", gross_amount=20.0)
        second = record(record_id="woo:second", source_system="woo", channel="woo",
                        event_type="woo_sale", gross_amount=20.0)
        first["quantity"] = second["quantity"] = 1
        proof = bookbuilder.normalized_inventory_quantity_proof(
            [first], group_label="woo", direction="sales"
        )
        action = {"idempotency_key": "example-2024-01-omitted", "payload": {"line_items": [{
            "line_role": "sales_revenue", "article_id_hint": "3", "quantity": 1,
            "inventory_quantity_proof": proof,
        }]}}
        normalized = base_normalized()
        normalized["records"]["sales"] = [first, second]

        findings = bookchecker.evaluate_inventory_quantities(
            action=action,
            resolved_sources=[{
                "record_ref": first["record_id"], "record": first, "payload": normalized,
            }],
            reviewed_allocations={},
        )

        self.assertTrue(any("complete contributor set" in item["summary"] for item in findings), findings)

    def test_inventory_quantity_checker_rejects_extra_or_reassigned_group_contributor(self) -> None:
        woo = record(record_id="woo:one", source_system="woo", channel="woo",
                     event_type="woo_sale", gross_amount=20.0)
        quartermaster = record(record_id="qm:one", source_system="quartermaster", channel="quartermaster",
                               event_type="quartermaster_sale", gross_amount=20.0)
        woo["quantity"] = quartermaster["quantity"] = 1
        normalized = base_normalized()
        normalized["records"]["sales"] = [woo, quartermaster]
        base_proof = bookbuilder.normalized_inventory_quantity_proof(
            [woo], group_label="woo", direction="sales"
        )
        extra = copy.deepcopy(base_proof)
        extra_contributor = bookbuilder.normalized_inventory_quantity_proof(
            [quartermaster], group_label="quartermaster", direction="sales"
        )["contributors"][0]
        extra["contributors"].append(extra_contributor)
        extra["contributors"].sort(key=lambda item: (item["record_id"], item["record_sha256"]))
        extra["quantity"] = 2.0
        extra["contributor_count"] = 2
        extra["contributor_set_sha256"] = bookbuilder.canonical_value_sha256(extra["contributors"])
        reassigned = copy.deepcopy(base_proof)
        reassigned["scope"]["group_label"] = "quartermaster"
        reassigned["scope_sha256"] = bookbuilder.canonical_value_sha256(reassigned["scope"])

        for label, proof, quantity in (("extra", extra, 2), ("reassigned", reassigned, 1)):
            with self.subTest(label=label):
                action = {"idempotency_key": f"example-{label}", "payload": {"line_items": [{
                    "line_role": "sales_revenue", "article_id_hint": "3", "quantity": quantity,
                    "inventory_quantity_proof": proof,
                }]}}
                findings = bookchecker.evaluate_inventory_quantities(
                    action=action,
                    resolved_sources=[
                        {"record_ref": woo["record_id"], "record": woo, "payload": normalized},
                        {"record_ref": quartermaster["record_id"], "record": quartermaster, "payload": normalized},
                    ],
                    reviewed_allocations={},
                )
                self.assertTrue(any("complete contributor set" in item["summary"] for item in findings), findings)

    def test_inventory_quantity_checker_accepts_multiple_complete_groups(self) -> None:
        records = []
        lines = []
        for label in ("woo", "quartermaster"):
            item = record(record_id=f"{label}:one", source_system=label, channel=label,
                          event_type=f"{label}_sale", gross_amount=20.0)
            item["quantity"] = 1
            records.append(item)
            lines.append({
                "line_role": "sales_revenue", "article_id_hint": "3", "quantity": 1,
                "inventory_quantity_proof": bookbuilder.normalized_inventory_quantity_proof(
                    [item], group_label=label, direction="sales"
                ),
            })
        normalized = base_normalized()
        normalized["records"]["sales"] = records
        action = {
            "idempotency_key": "example-multiple", "action_type": "create_invoice_summary",
            "payload": {"document_type": "invoice", "line_items": lines},
        }

        findings = bookchecker.evaluate_inventory_quantities(
            action=action,
            resolved_sources=[
                {"record_ref": item["record_id"], "record": item, "payload": normalized}
                for item in records
            ],
            reviewed_allocations={},
        )

        self.assertEqual(findings, [])

    def test_explicit_bank_allocation_must_match_bound_artifact_path(self) -> None:
        batch = {
            "reference_artifacts": [{
                "kind": "bank_allocations",
                "path": "companies/example/artifacts/bank/2024-allocations.json",
                "sha256": "a" * 64,
            }]
        }
        bookchecker.validate_explicit_bank_allocation_path(
            action_batch=batch,
            requested_path=ROOT / "companies/example/artifacts/bank/2024-allocations.json",
            cwd=ROOT,
        )
        with self.assertRaisesRegex(bookchecker.SimplbooksError, "does not match"):
            bookchecker.validate_explicit_bank_allocation_path(
                action_batch=batch,
                requested_path=ROOT / "companies/example/artifacts/bank/2025-allocations.json",
                cwd=ROOT,
            )

    def test_manual_required_row_rejects_api_cash_coverage_without_verified_manual_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="fee-api", amount=-7.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "bank_fee_payment",
                "target": {"financial_transaction_kind": "bank-fee"},
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            action = exact_settlement_action(row=row, normalized_path=normalized_path)
            batch = bank_coverage_batch(
                period="2024-01", allocation_path=allocation_path, actions=[action]
            )

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any("manual atomicity" in item["summary"].lower() for item in findings))

    def test_reviewed_split_reimbursement_requires_atomic_manual_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="reimbursement-api", amount=-50.30)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "reviewed_split",
                "target": {"document_type": "financial_transaction", "transaction_family": "reviewed_group"},
                "parts": [{
                    "amount": -50.30,
                    "disposition": "expense_reimbursement_payment",
                    "target": {"document_type": "financial_transaction", "transaction_family": "expense_reimbursement"},
                }],
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            action = exact_settlement_action(row=row, normalized_path=normalized_path)
            batch = bank_coverage_batch(
                period="2024-01", allocation_path=allocation_path, actions=[action]
            )

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any("manual atomicity" in item["summary"].lower() for item in findings))

    def test_generated_receipt_target_requires_correct_current_type_and_dependency(self) -> None:
        mutations = ("wrong_type", "missing_dependency")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                row = physical_bank_record(record_id=f"generated-{mutation}", amount=20.0)
                normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
                allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
                allocations["allocations"][0].update({
                    "disposition": "generated_invoice_receipt",
                    "target": {"action_key": "generated-target", "document_type": "invoice"},
                })
                allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
                target = {
                    "idempotency_key": "generated-target",
                    "action_type": "create_invoice_summary",
                    "payload": {"draft_schema": "invoice_summary_v1"},
                    "source_refs": [],
                    "confidence": "high",
                }
                receipt = exact_settlement_action(row=row, normalized_path=normalized_path)
                receipt["payload"].pop("linked_invoice_id")
                receipt["payload"]["linked_invoice_action"] = "generated-target"
                receipt["depends_on"] = ["generated-target"]
                if mutation == "wrong_type":
                    target["action_type"] = "create_purchase_summary"
                    target["payload"]["draft_schema"] = "purchase_summary_v1"
                else:
                    receipt["depends_on"] = []
                batch = bank_coverage_batch(
                    period="2024-01", allocation_path=allocation_path, actions=[target, receipt]
                )

                findings = bookchecker.evaluate_bank_statement_completeness(
                    batch, action_path=tmp / "actions.yaml", cwd=tmp
                )

                self.assertTrue(any("generated target" in item["summary"].lower() for item in findings))

    def test_generated_receipt_accepts_only_sha_bound_successful_historical_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            actions_dir = tmp / "artifacts" / "actions"
            submissions_dir = tmp / "artifacts" / "submissions"
            actions_dir.mkdir(parents=True)
            submissions_dir.mkdir(parents=True)
            prior_action = {
                "idempotency_key": "prior-invoice",
                "action_type": "create_invoice_summary",
                "payload": {"draft_schema": "invoice_summary_v1"},
            }
            prior_path = actions_dir / "2024-01.yaml"
            bookbuilder.write_yaml(prior_path, {
                "company_slug": "example", "period": "2024-01",
                "batch_id": "example-2024-01", "actions": [prior_action],
            })
            submission_path = submissions_dir / "2024-01.json"
            submission = {
                "company_slug": "example", "period": "2024-01",
                "batch_id": "example-2024-01",
                "action_file_sha256": bookchecker.file_sha256(prior_path),
                "request_log": [{
                    "mode": "write", "success": True, "inserted_id": "501",
                    "endpoint": "invoices/create",
                    "action_idempotency_key": "prior-invoice",
                }],
            }
            submission_path.write_text(json.dumps(submission), encoding="utf-8")
            row = physical_bank_record(
                record_id="historical-receipt", amount=20.0, event_date="2024-02-15"
            )
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "generated_invoice_receipt",
                "target": {"action_key": "prior-invoice", "document_type": "invoice"},
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            receipt = exact_settlement_action(row=row, normalized_path=normalized_path)
            receipt["payload"].pop("linked_invoice_id")
            receipt["payload"]["linked_invoice_action"] = "prior-invoice"
            receipt["depends_on"] = []
            batch = bank_coverage_batch(
                period="2024-02", allocation_path=allocation_path, actions=[receipt]
            )
            action_path = actions_dir / "2024-02.yaml"

            valid_findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=action_path, cwd=tmp
            )
            submission["action_file_sha256"] = "0" * 64
            submission_path.write_text(json.dumps(submission), encoding="utf-8")
            stale_findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=action_path, cwd=tmp
            )

        self.assertFalse(any("generated target" in item["summary"].lower() for item in valid_findings))
        self.assertTrue(any("generated target" in item["summary"].lower() for item in stale_findings))

    def test_generated_payment_target_requires_purchase_action_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="generated-payment", amount=-10.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "generated_purchase_payment",
                "target": {"action_key": "generated-purchase", "document_type": "purchase"},
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            target = {
                "idempotency_key": "generated-purchase",
                "action_type": "create_purchase_summary",
                "payload": {"draft_schema": "purchase_summary_v1"},
            }
            payment = exact_settlement_action(row=row, normalized_path=normalized_path)
            payment["payload"].pop("linked_purchase_id")
            payment["payload"]["linked_purchase_action"] = "generated-purchase"
            payment["depends_on"] = ["generated-purchase"]
            batch = bank_coverage_batch(
                period="2024-01", allocation_path=allocation_path, actions=[target, payment]
            )

            valid_findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )
            target["action_type"] = "create_invoice_summary"
            target["payload"]["draft_schema"] = "invoice_summary_v1"
            invalid_findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertFalse(any("generated target" in item["summary"].lower() for item in valid_findings))
        self.assertTrue(any("generated target" in item["summary"].lower() for item in invalid_findings))

    def test_generated_settlement_contact_must_match_linked_document(self) -> None:
        target = {
            "idempotency_key": "generated-purchase", "action_type": "create_purchase_summary",
            "payload": {
                "draft_schema": "purchase_summary_v1", "vendor_hint": "dpd",
                "counterparty": {"contact_id": "37", "display_name_hint": "DPD"},
            },
        }
        settlement = {
            "payload": {"counterparty": {"contact_id": "99"}, "counterparty_hint": "other"},
            "depends_on": ["generated-purchase"],
        }
        part = {
            "disposition": "generated_purchase_payment",
            "target": {"document_type": "purchase", "action_key": "generated-purchase"},
        }

        errors = bookchecker._generated_target_errors(
            part=part, settlement=settlement,
            current_actions={"generated-purchase": target}, historical_actions={},
        )

        self.assertTrue(any("contact" in item.lower() for item in errors))
        self.assertTrue(any("label" in item.lower() for item in errors))

    def test_existing_target_external_number_is_supporting_identity(self) -> None:
        row = physical_bank_record(record_id="invoice-receipt", amount=20.0)
        action = exact_settlement_action(row=row, normalized_path=Path("normalized.json"))
        part = {
            "disposition": "existing_invoice_receipt", "amount": 20.0,
            "target": {"document_type": "invoice", "simplbooks_id": "119", "external_number": "INV-119"},
        }

        self.assertTrue(bookchecker._allocation_part_matches_action(part, action=action, signed_amount=bookchecker.Decimal("20")))

    def test_clearing_and_fx_proof_fields_are_supporting_not_target_identity(self) -> None:
        row = physical_bank_record(record_id="purchase-payment", amount=-284.60)
        action = exact_settlement_action(row=row, normalized_path=Path("normalized.json"))
        action["payload"].pop("linked_purchase_id")
        action["payload"]["linked_purchase_action"] = "purchase"
        part = {
            "disposition": "generated_purchase_payment", "amount": -284.60,
            "target": {
                "document_type": "purchase", "action_key": "purchase",
                "clearing_record_ids": ["wallet:usd"], "bridge_record_ids": ["wallet:usd"],
                "bridge_direction": "same_as_physical", "clearing_evidence": [],
                "clearing_totals": {"USD": -306.32}, "clearing_relation": "reviewed_group",
                "bridge_amount": -284.60, "fx_proof": {"rate": 0.9198876},
                "clearing_equations": [{"equation": "signed_sum_equals_zero"}],
            },
        }

        self.assertTrue(bookchecker._allocation_part_matches_action(
            part, action=action, signed_amount=bookchecker.Decimal("-284.60")
        ))

    def test_checker_errors_when_action_batch_omits_one_bank_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rows = [
                physical_bank_record(record_id="receipt-1", amount=20.0),
                physical_bank_record(record_id="receipt-2", amount=30.0),
            ]
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, rows)
            batch = bank_coverage_batch(
                period="2024-01",
                allocation_path=allocation_path,
                actions=[exact_settlement_action(row=rows[0], normalized_path=normalized_path)],
            )

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any(
            item["severity"] == "error" and "uncovered physical bank row" in item["summary"]
            for item in findings
        ))

    def test_checker_errors_on_duplicate_physical_bank_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="receipt-1", amount=20.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            action = exact_settlement_action(row=row, normalized_path=normalized_path)
            duplicate = dict(action, idempotency_key="settlement-receipt-1-copy")
            batch = bank_coverage_batch(
                period="2024-01", allocation_path=allocation_path, actions=[action, duplicate]
            )

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any("duplicate physical bank coverage" in item["summary"] for item in findings))

    def test_checker_rejects_cash_settlement_without_physical_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="receipt-1", amount=20.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            action = exact_settlement_action(row=row, normalized_path=normalized_path)
            action["source_refs"][0].pop("source_kind")
            batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[action])

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any("must reference exactly one physical bank row" in item["summary"] for item in findings))

    def test_checker_accepts_exact_reviewed_split_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="split-1", amount=15.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "reviewed_split",
                "parts": [
                    {"amount": 20.0, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119", "document_type": "invoice"}},
                    {"amount": -5.0, "disposition": "existing_purchase_payment", "target": {"simplbooks_id": "88", "document_type": "purchase"}},
                ],
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            incoming = exact_settlement_action(row=row, normalized_path=normalized_path)
            incoming["payload"]["amount"] = 20.0
            payment = exact_settlement_action(row=row, normalized_path=normalized_path)
            payment.update({"idempotency_key": "settlement-split-1-part-2", "action_type": "create_payment_summary"})
            payment["payload"].update({"document_type": "payment", "amount": 5.0, "linked_purchase_id": "88"})
            batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[incoming, payment])

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertEqual(findings, [])

    def test_full_checker_uses_assigned_split_parts_for_legacy_cash_arithmetic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="split-full", amount=15.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "reviewed_split",
                "parts": [
                    {"amount": 20.0, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119", "document_type": "invoice"}},
                    {"amount": -5.0, "disposition": "existing_purchase_payment", "target": {"simplbooks_id": "88", "document_type": "purchase"}},
                ],
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            incoming = exact_settlement_action(row=row, normalized_path=normalized_path)
            incoming["payload"].update({"amount": 20.0, "counterparty": {"contact_id": "42"}})
            payment = exact_settlement_action(row=row, normalized_path=normalized_path)
            payment.update({"idempotency_key": "split-full-payment", "action_type": "create_payment_summary"})
            payment["payload"].update({
                "document_type": "payment", "amount": 5.0,
                "linked_purchase_id": "88", "counterparty": {"contact_id": "18"},
            })
            batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[incoming, payment])
            recon_path = tmp / "recon.json"
            recon = base_recon()
            recon_path.write_text(json.dumps(recon), encoding="utf-8")

            evaluation = bookchecker.evaluate_action_batch(
                action_batch=batch,
                action_path=tmp / "actions.yaml",
                recon_payload=recon,
                recon_path=recon_path,
                policy_text=None,
                cwd=tmp,
            )

            mutated = copy.deepcopy(batch)
            mutated["actions"][0]["payload"]["amount"] = 19.99
            mutated_evaluation = bookchecker.evaluate_action_batch(
                action_batch=mutated,
                action_path=tmp / "actions.yaml",
                recon_payload=recon,
                recon_path=recon_path,
                policy_text=None,
                cwd=tmp,
            )

        arithmetic_errors = [
            item for item in evaluation["findings"]
            if item["section"] == "arithmetic_consistency" and item["severity"] == "error"
        ]
        self.assertEqual(arithmetic_errors, [])
        self.assertEqual(mutated_evaluation["result"], "fail")
        self.assertTrue(any(
            item["section"] in {"bank_statement_completeness", "arithmetic_consistency"}
            and item["severity"] == "error"
            for item in mutated_evaluation["findings"]
        ))

    def test_full_checker_excludes_bijectively_assigned_multi_payment_parts_from_legacy_grouping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="split-three", amount=20.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "reviewed_split",
                "parts": [
                    {"amount": 30.0, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119", "document_type": "invoice"}},
                    {"amount": -5.0, "disposition": "existing_purchase_payment", "target": {"simplbooks_id": "88", "document_type": "purchase"}},
                    {"amount": -5.0, "disposition": "existing_purchase_payment", "target": {"simplbooks_id": "89", "document_type": "purchase"}},
                ],
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            incoming = exact_settlement_action(row=row, normalized_path=normalized_path)
            incoming["payload"]["amount"] = 30.0
            payments = []
            for index, purchase_id in enumerate(("88", "89"), start=1):
                payment = exact_settlement_action(row=row, normalized_path=normalized_path)
                payment["idempotency_key"] = f"split-three-payment-{index}"
                payment["payload"].update({
                    "document_type": "payment", "amount": 5.0,
                    "linked_purchase_id": purchase_id,
                })
                payments.append(payment)
            batch = bank_coverage_batch(
                period="2024-01", allocation_path=allocation_path,
                actions=[incoming, *payments],
            )
            recon_path = tmp / "recon.json"
            recon = base_recon()
            recon_path.write_text(json.dumps(recon), encoding="utf-8")

            evaluation = bookchecker.evaluate_action_batch(
                action_batch=batch,
                action_path=tmp / "actions.yaml",
                recon_payload=recon,
                recon_path=recon_path,
                policy_text=None,
                cwd=tmp,
            )

            mutated = copy.deepcopy(batch)
            mutated["actions"][2]["payload"]["amount"] = 4.99
            mutated_evaluation = bookchecker.evaluate_action_batch(
                action_batch=mutated,
                action_path=tmp / "actions.yaml",
                recon_payload=recon,
                recon_path=recon_path,
                policy_text=None,
                cwd=tmp,
            )

        self.assertEqual([
            item for item in evaluation["findings"]
            if item["section"] == "arithmetic_consistency" and item["severity"] == "error"
        ], [])
        self.assertEqual(mutated_evaluation["result"], "fail")

    def test_checker_rejects_cash_action_target_changed_after_allocation_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="receipt-1", amount=20.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            action = exact_settlement_action(row=row, normalized_path=normalized_path)
            action["payload"]["linked_invoice_id"] = "120"
            batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[action])

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any("reviewed target" in item["summary"] for item in findings))

    def test_checker_rejects_direct_sale_receipt_linked_to_wrong_generated_invoice(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="direct-1", amount=20.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "direct_sale_receipt",
                "target": {
                    "document_type": "invoice", "contact_label": "direct-sale",
                    "posting_family": "direct-sale-taxable", "vat_profile": "taxable",
                    "product_description": "MoonBall", "quantity": 1,
                    "gross_amount": 20.0, "warehouse_id": "6",
                },
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            invoice = {
                "idempotency_key": "correct-direct-invoice", "action_type": "create_invoice_summary",
                "payload": {
                    "draft_schema": "invoice_summary_v1", "document_type": "invoice",
                    "currency": "EUR", "posting_policy_family": "direct-sale-taxable",
                    "summary_scope": {"channel_or_source": "direct-sale", "tax_profile": "taxable", "posting_family": "direct-sale-taxable"},
                    "counterparty": {"contact_id": "42"},
                    "line_items": [{"line_role": "direct_sale_revenue", "description": "MoonBall", "quantity": 1,
                                    "gross_amount": 20.0, "warehouse_id_hint": "6"}],
                },
                "source_refs": [{"path": str(normalized_path), "record_ref": "direct-1", "source_kind": "physical_bank"}],
            }
            receipt = exact_settlement_action(row=row, normalized_path=normalized_path)
            receipt["payload"].pop("linked_invoice_id")
            receipt["payload"].update({"linked_invoice_action": "correct-direct-invoice", "settlement_family": "direct-sale"})
            batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[invoice, receipt])

            valid_findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )
            receipt["payload"]["linked_invoice_action"] = "wrong-direct-invoice"
            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertFalse(any("direct-sale invoice" in item["summary"] for item in valid_findings))
        self.assertTrue(any("direct-sale invoice" in item["summary"] for item in findings))

    def test_checker_rejects_duplicate_split_target_when_equal_amount_part_remains_unmatched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="split-equal", amount=40.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "reviewed_split",
                "amount": 40.0,
                "parts": [
                    {"amount": 20.0, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119", "document_type": "invoice"}},
                    {"amount": 20.0, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "120", "document_type": "invoice"}},
                ],
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            first = exact_settlement_action(row=row, normalized_path=normalized_path)
            first["payload"]["amount"] = 20.0
            duplicate = copy.deepcopy(first)
            duplicate["idempotency_key"] = "duplicate-target"
            batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[first, duplicate])

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any("bijective" in item["summary"] for item in findings))

    def test_checker_errors_on_month_end_date_substitution_and_currency_amount_changes(self) -> None:
        mutations = {
            "statement date": ("document_date", "2024-01-31"),
            "currency": ("currency", "USD"),
            "signed amount": ("amount", 19.99),
        }
        for expected, (field, value) in mutations.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                row = physical_bank_record(record_id="receipt-1", amount=20.0)
                normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
                action = exact_settlement_action(row=row, normalized_path=normalized_path)
                action["payload"][field] = value
                batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[action])

                findings = bookchecker.evaluate_bank_statement_completeness(
                    batch, action_path=tmp / "actions.yaml", cwd=tmp
                )

                self.assertTrue(any(expected in item["summary"] for item in findings), findings)

    def test_checker_counts_verified_manual_dependency_as_exact_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="bank:fee:1", amount=-7.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "bank_fee_payment",
                "target": {"financial_transaction_kind": "bank-fee"},
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            dependency = manual_financial_dependency(blocking=False, status="verified")
            dependency["source_ref"]["path"] = str(normalized_path)
            dependency["statement_id"] = "archive:bank:fee:1"
            batch = bank_coverage_batch(
                period="2024-01", allocation_path=allocation_path, actions=[], dependencies=[dependency]
            )

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertEqual(findings, [])

    def test_checker_rejects_verified_manual_dependency_that_changed_reviewed_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="bank:fee:1", amount=-7.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["allocations"][0].update({
                "disposition": "bank_fee_payment",
                "target": {"financial_transaction_kind": "bank-fee"},
            })
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            dependency = manual_financial_dependency(blocking=False, status="verified")
            dependency.update({
                "statement_id": "archive:bank:fee:1",
                "target": {"financial_transaction_kind": "internal-transfer"},
            })
            dependency["source_ref"]["path"] = str(normalized_path)
            batch = bank_coverage_batch(
                period="2024-01", allocation_path=allocation_path, actions=[], dependencies=[dependency]
            )

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any("manual coverage does not match" in item["summary"] for item in findings))

    def test_checker_rejects_clearing_record_masquerading_as_physical_bank(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="receipt-1", amount=20.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            clearing = dict(row, record_id="wallet-1", source_system="printful")
            normalized["records"]["clearing_transactions"] = [clearing]
            normalized_path.write_text(json.dumps(normalized), encoding="utf-8")
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["normalized_bindings"][0]["sha256"] = hashlib.sha256(normalized_path.read_bytes()).hexdigest()
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            action = exact_settlement_action(row=row, normalized_path=normalized_path)
            action["source_refs"][0]["record_ref"] = "wallet-1"
            batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[action])

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any("masquerade" in item["summary"] for item in findings))

    def test_checker_rejects_wrong_bank_account_for_physical_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="receipt-1", amount=20.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            action = exact_settlement_action(row=row, normalized_path=normalized_path)
            action["payload"]["bank_account_id"] = "999"
            batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[action])

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch,
                action_path=tmp / "actions.yaml",
                cwd=tmp,
                posting_policy={"bank_accounts": {"EE123": {"EUR": "3"}}},
            )

        self.assertTrue(any("source account" in item["summary"] for item in findings))

    def test_checker_requires_allocation_binding_when_normalized_period_has_bank_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            actions_dir = root / "artifacts" / "actions"
            normalized_dir = root / "artifacts" / "normalized"
            actions_dir.mkdir(parents=True)
            normalized_dir.mkdir(parents=True)
            normalized = base_normalized()
            normalized["records"]["bank_transactions"] = [physical_bank_record(record_id="receipt-1", amount=20.0)]
            (normalized_dir / "2024-01.json").write_text(json.dumps(normalized), encoding="utf-8")
            batch = {"period": "2024-01", "reference_artifacts": [], "actions": [], "unresolved_dependencies": []}

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=actions_dir / "2024-01.yaml", cwd=root
            )

        self.assertTrue(any("bound bank allocation" in item["summary"] for item in findings))

    def test_checker_rejects_allocation_bound_to_another_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            row = physical_bank_record(record_id="receipt-1", amount=20.0)
            normalized_path, allocation_path = write_bank_coverage_fixture(tmp, [row])
            allocations = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocations["company_slug"] = "another-company"
            allocation_path.write_text(json.dumps(allocations), encoding="utf-8")
            action = exact_settlement_action(row=row, normalized_path=normalized_path)
            batch = bank_coverage_batch(period="2024-01", allocation_path=allocation_path, actions=[action])
            batch["company_slug"] = "example"

            findings = bookchecker.evaluate_bank_statement_completeness(
                batch, action_path=tmp / "actions.yaml", cwd=tmp
            )

        self.assertTrue(any("company_slug" in item["summary"] for item in findings))

    def test_approved_batch_rejects_medium_confidence_but_information_notes_do_not(self) -> None:
        action = {"idempotency_key": "a", "confidence": "medium", "payload": {}, "review_notes": ["provenance"]}
        findings = bookchecker.evaluate_account_vat(action=action, batch_approved=True)
        self.assertTrue(any(item["severity"] == "error" and "medium" in item["summary"] for item in findings))

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
            for index in range(1, 5)
        ]
        self.assertFalse(bookchecker.evaluate_vat_profiles(batch["actions"], policy_with_24_percent_profile()))

        batch["actions"][0]["payload"]["line_items"][0]["vat_allocation_component_evidence"][0]["event_date"] = "2026-01-01"
        report = bookchecker.evaluate_vat_profiles(batch["actions"], policy_with_24_percent_profile())
        self.assertTrue(any("event date" in item["summary"] for item in report))
        batch["actions"][0]["payload"]["line_items"][0]["vat_allocation_component_evidence"][0]["event_date"] = "2025-11-27"

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

    def test_checker_rejects_manual_financial_dependency_without_statement_proof(self) -> None:
        dependency = {
            "kind": "manual_statement_import_financial_transaction",
            "blocking": True,
            "statement_id": "archive:fee-1",
            "record_id": "fee-1",
            "date": "2024-08-30",
            "iban": "EE123",
            "currency": "EUR",
            "physical_signed_amount": -7.0,
            "source_ref": {
                "path": "normalized.json",
                "record_ref": "fee-1",
                "source_kind": "physical_bank",
            },
            "reviewed_rationale": "Reviewed bank fee.",
            "split_parts": [],
            "split_proof": None,
        }

        findings = bookchecker.evaluate_unresolved_dependencies({"unresolved_dependencies": [dependency]})

        self.assertTrue(any("statement import proof" in item["summary"].lower() for item in findings))

    def test_checker_rejects_manual_financial_dependency_without_statement_ref_binding(self) -> None:
        dependency = {
            "kind": "manual_statement_import_financial_transaction",
            "blocking": True,
            "statement_id": "archive:fee-1",
            "record_id": "fee-1",
            "date": "2024-08-30",
            "iban": "EE123",
            "currency": "EUR",
            "physical_signed_amount": -7.0,
            "source_ref": {"path": "normalized.json", "record_ref": None},
            "reviewed_rationale": "Reviewed bank fee.",
            "split_parts": [],
            "split_proof": None,
            "statement_import_proof": {"status": "pending", "required_evidence": "live_discovery_or_audit"},
        }

        findings = bookchecker.evaluate_unresolved_dependencies({"unresolved_dependencies": [dependency]})

        self.assertTrue(any("statement ref" in item["summary"].lower() for item in findings))

    def test_checker_rejects_manual_financial_dependency_with_bad_split_proof(self) -> None:
        dependency = {
            "kind": "manual_statement_import_financial_transaction",
            "blocking": True,
            "statement_id": "archive:net-1",
            "record_id": "net-1",
            "date": "2024-08-30",
            "iban": "EE123",
            "currency": "USD",
            "physical_signed_amount": 723.32,
            "source_ref": {
                "path": "normalized.json",
                "record_ref": "net-1",
                "source_kind": "physical_bank",
            },
            "reviewed_rationale": "Reviewed net settlement.",
            "split_parts": [
                {"signed_amount": 738.32, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119"}},
                {"signed_amount": -15.0, "disposition": "bank_fee_payment", "target": {"financial_transaction_kind": "fee"}},
            ],
            "split_proof": {"signed_parts_total": 722.32, "physical_signed_amount": 723.32},
            "statement_import_proof": {"status": "pending", "required_evidence": "live_discovery_or_audit"},
        }

        findings = bookchecker.evaluate_unresolved_dependencies({"unresolved_dependencies": [dependency]})

        self.assertTrue(any("split" in item["summary"].lower() for item in findings))

    def test_checker_treats_pending_manual_proof_as_blocking_even_when_flag_is_false(self) -> None:
        dependency = manual_financial_dependency(blocking=False, status="pending")

        findings = bookchecker.evaluate_unresolved_dependencies({"unresolved_dependencies": [dependency]})

        self.assertTrue(any("pending" in item["summary"].lower() for item in findings))

    def test_checker_independently_rejects_reversed_manual_split_signs(self) -> None:
        dependency = manual_financial_dependency(blocking=True, status="pending")
        dependency.update({
            "disposition": "reviewed_split",
            "currency": "USD",
            "physical_signed_amount": -723.32,
            "split_parts": [
                {"signed_amount": -738.32, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119"}},
                {"signed_amount": 15.0, "disposition": "bank_fee_payment", "target": {"financial_transaction_kind": "fee"}},
            ],
            "split_proof": {"signed_parts_total": -723.32, "physical_signed_amount": -723.32, "equation": "-738.32 + 15.00 = -723.32"},
        })

        findings = bookchecker.evaluate_unresolved_dependencies({"unresolved_dependencies": [dependency]})
        summaries = " ".join(item["summary"] for item in findings)

        self.assertIn("existing_invoice_receipt", summaries)
        self.assertIn("bank_fee_payment", summaries)

    def test_checker_independently_rejects_positive_manual_bank_fee(self) -> None:
        dependency = manual_financial_dependency(blocking=True, status="pending")
        dependency["physical_signed_amount"] = 7.0

        findings = bookchecker.evaluate_unresolved_dependencies({"unresolved_dependencies": [dependency]})

        self.assertTrue(any("bank_fee_payment" in item["summary"] and "negative" in item["summary"] for item in findings))

    def test_checker_independently_rejects_positive_expense_reimbursement(self) -> None:
        dependency = manual_financial_dependency(blocking=True, status="pending")
        dependency.update({
            "disposition": "expense_reimbursement_payment",
            "physical_signed_amount": 50.30,
            "target": {"transaction_family": "expense_reimbursement"},
        })

        findings = bookchecker.evaluate_unresolved_dependencies({"unresolved_dependencies": [dependency]})

        self.assertTrue(any(
            "expense_reimbursement_payment" in item["summary"] and "negative" in item["summary"]
            for item in findings
        ))

    def test_checker_rejects_api_owned_top_level_manual_dependency_dispositions(self) -> None:
        cases = (("existing_invoice_receipt", 7.0), ("generated_purchase_payment", -7.0))
        for disposition, amount in cases:
            with self.subTest(disposition=disposition):
                dependency = manual_financial_dependency()
                dependency.update({"disposition": disposition, "physical_signed_amount": amount})

                findings = bookchecker.evaluate_unresolved_dependencies({"unresolved_dependencies": [dependency]})

                self.assertTrue(any("top-level" in item["summary"] for item in findings))

    def test_checker_requires_manual_financial_part_in_reviewed_split_dependency(self) -> None:
        dependency = manual_financial_dependency()
        dependency.update({
            "disposition": "reviewed_split",
            "physical_signed_amount": 10.0,
            "split_parts": [
                {"signed_amount": 20.0, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119"}},
                {"signed_amount": -10.0, "disposition": "generated_purchase_payment", "target": {"action_key": "purchase-1"}},
            ],
            "split_proof": {"signed_parts_total": 10.0, "physical_signed_amount": 10.0, "equation": "20.00 + -10.00 = 10.00"},
        })

        findings = bookchecker.evaluate_unresolved_dependencies({"unresolved_dependencies": [dependency]})

        self.assertTrue(any("manual-financial part" in item["summary"] for item in findings))

    def test_checker_resolves_manual_dependency_against_physical_bank_record(self) -> None:
        mutations = {
            "statement_id": lambda dep: dep.update(statement_id="archive:forged"),
            "record_id": lambda dep: (dep.update(record_id="bank:forged"), dep["source_ref"].update(record_ref="bank:forged")),
            "date": lambda dep: dep.update(date="2024-01-16"),
            "physical_signed_amount": lambda dep: dep.update(physical_signed_amount=-8.0),
            "iban": lambda dep: dep.update(iban="EE999"),
            "currency": lambda dep: dep.update(currency="USD"),
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                normalized = base_normalized()
                fee = record(record_id="bank:fee:1", source_system="bank", event_type="bank_debit", gross_amount=-7.0)
                fee["attributes"] = {"iban": "EE123", "archive_id": "fee-1"}
                normalized["records"]["bank_transactions"] = [fee]
                (root / "normalized.json").write_text(json.dumps(normalized), encoding="utf-8")
                recon = base_recon()
                recon_path = root / "recon.json"
                recon_path.write_text(json.dumps(recon), encoding="utf-8")
                dependency = manual_financial_dependency()
                mutate(dependency)

                report = bookchecker.evaluate_action_batch(
                    action_batch={
                        "period": "2024-01",
                        "recon_ref": "recon.json",
                        "unresolved_dependencies": [dependency],
                        "reference_artifacts": [],
                        "actions": [],
                    },
                    action_path=root / "actions.yaml",
                    recon_payload=recon,
                    recon_path=recon_path,
                    policy_text=None,
                    cwd=root,
                )

                self.assertEqual(report["result"], "fail")
                self.assertTrue(any(field.replace("physical_signed_amount", "signed amount") in item["summary"].lower() for item in report["findings"]))

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
        self.assertEqual(evaluation["warning_count"], 0)

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

    def test_distinct_physical_cash_rows_with_identical_payload_are_not_duplicates(self) -> None:
        first = {
            "idempotency_key": "incoming-one", "method": "POST", "endpoint": "incomings/create",
            "payload": {"draft_schema": "cash_settlement_v1", "amount": 20.0},
            "source_refs": [{"path": "normalized.json", "record_ref": "bank:one", "source_kind": "physical_bank"}],
        }
        second = copy.deepcopy(first)
        second["idempotency_key"] = "incoming-two"
        second["source_refs"][0]["record_ref"] = "bank:two"

        self.assertEqual(bookchecker.evaluate_duplicates({"actions": [first, second]}), [])

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
