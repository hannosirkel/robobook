from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookchecker  # noqa: E402
import bookbuilder  # noqa: E402
import examine_simplbooks_year  # noqa: E402


def resolve_ref(root_schema: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise AssertionError(f"Unsupported non-local schema ref: {ref}")
    value: Any = root_schema
    for part in ref[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def validate(instance: Any, schema: dict[str, Any], *, root_schema: dict[str, Any], path: str = "$") -> None:
    if "$ref" in schema:
        validate(instance, resolve_ref(root_schema, schema["$ref"]), root_schema=root_schema, path=path)
        return
    for subschema in schema.get("allOf", []):
        validate(instance, subschema, root_schema=root_schema, path=path)
    if "if" in schema:
        try:
            validate(instance, schema["if"], root_schema=root_schema, path=path)
        except AssertionError:
            pass
        else:
            if "then" in schema:
                validate(instance, schema["then"], root_schema=root_schema, path=path)
    if "const" in schema:
        assert instance == schema["const"], f"{path}: expected const {schema['const']!r}"
    if "enum" in schema:
        assert instance in schema["enum"], f"{path}: {instance!r} not in enum"

    expected_type = schema.get("type")
    allowed = expected_type if isinstance(expected_type, list) else [expected_type] if expected_type else []
    type_matches = {
        "object": lambda value: isinstance(value, dict),
        "array": lambda value: isinstance(value, list),
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda value: isinstance(value, bool),
        "null": lambda value: value is None,
    }
    if allowed:
        assert any(type_matches[name](instance) for name in allowed), f"{path}: wrong type for {allowed}"

    if isinstance(instance, str):
        if "pattern" in schema:
            assert re.search(schema["pattern"], instance), f"{path}: {instance!r} does not match {schema['pattern']}"
        if "minLength" in schema:
            assert len(instance) >= schema["minLength"], f"{path}: string is too short"
        if "maxLength" in schema:
            assert len(instance) <= schema["maxLength"], f"{path}: string is too long"
    if isinstance(instance, (int, float)) and not isinstance(instance, bool) and "minimum" in schema:
        assert instance >= schema["minimum"], f"{path}: below minimum"
    if isinstance(instance, (int, float)) and not isinstance(instance, bool) and "maximum" in schema:
        assert instance <= schema["maximum"], f"{path}: above maximum"

    if isinstance(instance, list):
        if "minItems" in schema:
            assert len(instance) >= schema["minItems"], f"{path}: too few items"
        if "items" in schema:
            for index, item in enumerate(instance):
                validate(item, schema["items"], root_schema=root_schema, path=f"{path}[{index}]")
        if "contains" in schema:
            matching_items = 0
            for index, item in enumerate(instance):
                try:
                    validate(item, schema["contains"], root_schema=root_schema, path=f"{path}[{index}]")
                except AssertionError:
                    continue
                matching_items += 1
            assert matching_items >= schema.get("minContains", 1), f"{path}: contains match count is too small"

    if isinstance(instance, dict):
        if "minProperties" in schema:
            assert len(instance) >= schema["minProperties"], f"{path}: too few properties"
        for required in schema.get("required", []):
            assert required in instance, f"{path}: missing required property {required!r}"
        if "propertyNames" in schema:
            for key in instance:
                validate(key, schema["propertyNames"], root_schema=root_schema, path=f"{path}.<key>")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            child_path = f"{path}.{key}"
            if key in properties:
                validate(value, properties[key], root_schema=root_schema, path=child_path)
            elif additional is False:
                raise AssertionError(f"{child_path}: additional property is forbidden")
            elif isinstance(additional, dict):
                validate(value, additional, root_schema=root_schema, path=child_path)


class SchemaContractTests(unittest.TestCase):
    def assert_artifact_valid(self, *, schema_name: str, artifact: Any) -> None:
        schema = json.loads((ROOT / "schemas" / schema_name).read_text(encoding="utf-8"))
        validate(artifact, schema, root_schema=schema)

    def test_action_batch_template_matches_strict_schema(self) -> None:
        artifact = bookchecker.load_yaml(ROOT / "templates/actions-period.template.yaml")
        self.assert_artifact_valid(schema_name="action-batch.schema.json", artifact=artifact)

    def test_submission_log_schema_accepts_successful_action_file_sha(self) -> None:
        artifact = json.loads((ROOT / "templates/submission-log.template.json").read_text(encoding="utf-8"))
        artifact["mode"] = "write"
        artifact["action_file_sha256"] = "a" * 64
        self.assert_artifact_valid(schema_name="submission-log.schema.json", artifact=artifact)

    def test_posting_policy_template_matches_strict_schema(self) -> None:
        artifact = json.loads((ROOT / "templates/posting-policy.template.json").read_text(encoding="utf-8"))
        self.assert_artifact_valid(schema_name="posting-policy.schema.json", artifact=artifact)

    def test_posting_policy_schema_accepts_currency_qualified_bank_account(self) -> None:
        artifact = json.loads((ROOT / "templates/posting-policy.template.json").read_text(encoding="utf-8"))
        artifact["bank_accounts"] = {"EE123": {"EUR": "3", "USD": "4"}}

        self.assert_artifact_valid(schema_name="posting-policy.schema.json", artifact=artifact)

    def test_normalized_period_template_includes_cash_ledger_categories(self) -> None:
        artifact = json.loads((ROOT / "templates/normalized-period.template.json").read_text(encoding="utf-8"))
        self.assert_artifact_valid(schema_name="normalized-period.schema.json", artifact=artifact)
        self.assertEqual(artifact["records"]["clearing_transactions"], [])
        self.assertEqual(artifact["records"]["bank_balances"], [])

    def test_recon_period_template_exposes_report_only_bank_write_readiness(self) -> None:
        artifact = json.loads((ROOT / "templates/recon-period.template.json").read_text(encoding="utf-8"))

        self.assert_artifact_valid(schema_name="recon-period.schema.json", artifact=artifact)
        self.assertFalse(artifact["bank_coverage"]["coverage_ready"])
        self.assertFalse(artifact["bank_coverage"]["clearing_ready"])

    def test_recon_period_schema_accepts_deferred_camt_evidence_scope(self) -> None:
        artifact = json.loads((ROOT / "templates/recon-period.template.json").read_text(encoding="utf-8"))
        artifact["bank_coverage"]["ledgers"] = [{
            "iban": "EE123",
            "currency": "EUR",
            "physical_bank_row_count": 0,
            "allocated_row_count": 0,
            "unallocated_row_count": 0,
            "credit_total": 0,
            "debit_total": 0,
            "net_movement": 0,
            "camt_opening_balance": None,
            "computed_closing_balance": None,
            "camt_closing_balance": None,
            "camt_evidence_scopes": [{
                "statement_from": "2024-01-01",
                "statement_to": "2024-12-31",
                "balance_type": "OPBD",
            }],
        }]

        self.assert_artifact_valid(schema_name="recon-period.schema.json", artifact=artifact)

    def test_year_overview_template_matches_strict_schema(self) -> None:
        artifact = json.loads((ROOT / "templates/year-overview.template.json").read_text(encoding="utf-8"))
        self.assert_artifact_valid(schema_name="year-overview.schema.json", artifact=artifact)

    def test_woo_tax_allocation_template_matches_strict_schema(self) -> None:
        artifact = json.loads((ROOT / "templates/woo-tax-allocation.template.json").read_text(encoding="utf-8"))
        self.assert_artifact_valid(schema_name="woo-tax-allocation.schema.json", artifact=artifact)

    def test_woo_tax_allocation_schema_requires_merchant_to_absorb_vat(self) -> None:
        artifact = json.loads((ROOT / "templates/woo-tax-allocation.template.json").read_text(encoding="utf-8"))
        artifact["policy"]["merchant_absorbs_vat"] = False
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="woo-tax-allocation.schema.json", artifact=artifact)

    def test_woo_tax_allocation_schema_rejects_empty_evidence_and_allocations(self) -> None:
        artifact = json.loads((ROOT / "templates/woo-tax-allocation.template.json").read_text(encoding="utf-8"))
        for field in ("source_rows", "allocations"):
            with self.subTest(field=field):
                mutated = json.loads(json.dumps(artifact))
                mutated[field] = []
                with self.assertRaises(AssertionError):
                    self.assert_artifact_valid(schema_name="woo-tax-allocation.schema.json", artifact=mutated)

    def test_bank_allocation_schema_accepts_exact_reviewed_disposition(self) -> None:
        artifact = bank_allocation_payload(
            allocations=[
                {
                    "statement_id": "archive:2024010212345678",
                    "record_id": "bank-source:bank:2",
                    "iban": "EE123",
                    "period": "2024-01",
                    "disposition": "existing_invoice_receipt",
                    "amount": 330.0,
                    "currency": "EUR",
                    "target": {"simplbooks_id": "119", "document_type": "invoice"},
                    "review": {"status": "approved", "rationale": "Exact invoice number and amount."},
                }
            ]
        )
        self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=artifact)

    def test_bank_allocation_schema_rejects_ignore(self) -> None:
        artifact = bank_allocation_payload(allocations=[bank_allocation(disposition="ignore")])
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=artifact)

    def test_bank_allocation_schema_requires_iban(self) -> None:
        artifact = bank_allocation_payload(allocations=[bank_allocation()])
        del artifact["allocations"][0]["iban"]
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=artifact)

    def test_bank_allocation_schema_rejects_empty_bindings_and_target(self) -> None:
        empty_bindings = bank_allocation_payload(allocations=[])
        empty_bindings["normalized_bindings"] = []
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=empty_bindings)

        empty_target = bank_allocation_payload(allocations=[bank_allocation(target={})])
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=empty_target)

    def test_bank_allocation_schema_requires_a_four_digit_year(self) -> None:
        for year in (1000, 9999):
            with self.subTest(year=year):
                artifact = bank_allocation_payload(allocations=[])
                artifact["year"] = year
                self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=artifact)
        for year in (999, 10000):
            with self.subTest(year=year):
                artifact = bank_allocation_payload(allocations=[])
                artifact["year"] = year
                with self.assertRaises(AssertionError):
                    self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=artifact)

    def test_bank_allocation_schema_rejects_malformed_clearing_and_fx_proof(self) -> None:
        target = {
            "document_type": "financial_transaction",
            "transaction_family": "failed_transfer_and_return",
            "clearing_record_ids": ["clearing:1"],
        }
        missing_proof = bank_allocation_payload(allocations=[
            bank_allocation(disposition="clearing_transfer", amount=-13.27, target=target)
        ])
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=missing_proof)

        target.update({
            "bridge_record_ids": ["clearing:1"], "bridge_direction": "opposite_physical",
            "clearing_evidence": [{
                "record_id": "clearing:1", "period": "2024-01", "currency": "USD", "amount": 14.94,
                "provider": "processor", "account": "wallet", "source_system": "processor",
            }],
            "clearing_totals": {"USD": 14.94}, "clearing_relation": "reviewed_group",
            "bridge_amount": -13.27,
            "fx_proof": {
                "equation": "absolute_clearing_times_rate_plus_fee_equals_physical",
                "physical_record_id": "bank-source:bank:2", "physical_currency": "EUR", "physical_amount": -13.27,
                "clearing_currency": "USD", "clearing_amount": 14.94, "rate": "bad", "fee_amount": 0,
                "rate_evidence": {"path": "source.csv", "sha256": "a" * 64},
            },
        })
        malformed_fx = bank_allocation_payload(allocations=[
            bank_allocation(disposition="clearing_transfer", amount=-13.27, target=target)
        ])
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=malformed_fx)

    def test_bank_allocation_schema_types_foreign_currency_pilot(self) -> None:
        target = {
            "document_type": "purchase", "action_key": "purchase",
            "foreign_currency_pilot_required": True,
        }
        malformed = bank_allocation_payload(allocations=[
            bank_allocation(disposition="generated_purchase_payment", amount=-30.20, target=target)
        ])
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=malformed)

    def test_bank_allocation_template_matches_strict_schema(self) -> None:
        artifact = json.loads((ROOT / "templates/bank-allocation.template.json").read_text(encoding="utf-8"))
        self.assert_artifact_valid(schema_name="bank-allocation.schema.json", artifact=artifact)

    def test_manual_inventory_action_template_matches_strict_schema(self) -> None:
        artifact = json.loads((ROOT / "templates/manual-inventory-action.template.json").read_text(encoding="utf-8"))
        self.assert_artifact_valid(schema_name="manual-inventory-action.schema.json", artifact=artifact)

    def test_manual_inventory_action_schema_rejects_nonpositive_quantity(self) -> None:
        artifact = json.loads((ROOT / "templates/manual-inventory-action.template.json").read_text(encoding="utf-8"))
        artifact["quantity"] = 0
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="manual-inventory-action.schema.json", artifact=artifact)

    def test_generated_action_batch_matches_strict_schema(self) -> None:
        categories = (
            "sales", "refunds", "fees", "payouts", "bank_transactions", "clearing_transactions", "bank_balances", "purchase_expenses",
            "purchase_credits", "inventory_movements", "manual_adjustments", "other",
        )
        normalized = {
            "schema_version": "1.0",
            "company_slug": "example",
            "period": "2024-01",
            "base_currency": "EUR",
            "records": {category: [] for category in categories},
            "exceptions": [],
        }
        recon = {
            "schema_version": "1.0",
            "company_slug": "example",
            "period": "2024-01",
            "approve_for_build": True,
            "checks": [],
            "exceptions": [],
        }
        batch = bookbuilder.build_action_batch(
            normalized_payload=normalized,
            recon_payload=recon,
            normalized_path=ROOT / "companies/example/artifacts/normalized/2024-01.json",
            recon_path=ROOT / "companies/example/artifacts/recon/2024-01.json",
            repo_root=ROOT,
        )
        batch["reference_artifacts"] = [
            {"kind": "posting_policy", "path": "policy.json", "sha256": "0" * 64},
            {"kind": "discovery_overview", "path": "overview.json", "sha256": "1" * 64},
            {"kind": "normalized_period", "path": "normalized.json", "sha256": "2" * 64},
            {"kind": "reconciliation", "path": "recon.json", "sha256": "3" * 64},
        ]
        self.assert_artifact_valid(schema_name="action-batch.schema.json", artifact=batch)

    def test_action_batch_schema_allows_bank_allocation_reference(self) -> None:
        batch = {
            "schema_version": "1.0",
            "company_slug": "example",
            "period": "2024-01",
            "generated_at": "2024-01-01T00:00:00Z",
            "batch_id": "example-2024-01",
            "approval_status": "draft",
            "already_present": [],
            "unresolved_dependencies": [],
            "reference_artifacts": [
                {"kind": "posting_policy", "path": "policy.json", "sha256": "0" * 64},
                {"kind": "discovery_overview", "path": "overview.json", "sha256": "1" * 64},
                {"kind": "bank_allocations", "path": "allocations.json", "sha256": "2" * 64},
                {"kind": "normalized_period", "path": "normalized.json", "sha256": "3" * 64},
                {"kind": "reconciliation", "path": "recon.json", "sha256": "4" * 64},
            ],
            "actions": [],
        }
        self.assert_artifact_valid(schema_name="action-batch.schema.json", artifact=batch)

    def test_action_batch_schema_rejects_manual_financial_dependency_without_statement_binding(self) -> None:
        batch = {
            "schema_version": "1.0",
            "company_slug": "example",
            "period": "2024-01",
            "generated_at": "2024-01-01T00:00:00Z",
            "batch_id": "example-2024-01",
            "approval_status": "draft",
            "already_present": [],
            "unresolved_dependencies": [{
                "kind": "manual_statement_import_financial_transaction",
                "blocking": True,
                "reason": "Statement import required.",
            }],
            "reference_artifacts": [
                {"kind": "posting_policy", "path": "policy.json", "sha256": "0" * 64},
                {"kind": "discovery_overview", "path": "overview.json", "sha256": "1" * 64},
            ],
            "actions": [],
        }

        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="action-batch.schema.json", artifact=batch)

    def test_action_batch_schema_rejects_pending_manual_dependency_marked_nonblocking(self) -> None:
        dependency = {
            "kind": "manual_statement_import_financial_transaction",
            "blocking": False,
            "reason": "Statement import required.",
            "disposition": "bank_fee_payment",
            "statement_id": "archive:fee-1",
            "record_id": "fee-1",
            "date": "2024-01-15",
            "iban": "EE123",
            "currency": "EUR",
            "physical_signed_amount": -7.0,
            "source_ref": {"path": "normalized.json", "record_ref": "fee-1", "source_kind": "physical_bank"},
            "reviewed_rationale": "Reviewed fee.",
            "target": {"financial_transaction_kind": "bank-fee"},
            "split_parts": [],
            "split_proof": None,
            "statement_import_proof": {"status": "pending", "required_evidence": "live_discovery_or_audit"},
        }
        batch = {
            "schema_version": "1.0", "company_slug": "example", "period": "2024-01",
            "generated_at": "2024-01-01T00:00:00Z", "batch_id": "example-2024-01",
            "approval_status": "draft", "already_present": [], "unresolved_dependencies": [dependency],
            "reference_artifacts": [
                {"kind": "posting_policy", "path": "policy.json", "sha256": "0" * 64},
                {"kind": "discovery_overview", "path": "overview.json", "sha256": "1" * 64},
            ], "actions": [],
        }

        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="action-batch.schema.json", artifact=batch)

        dependency["statement_import_proof"] = {
            "status": "verified",
            "required_evidence": "live_discovery_or_audit",
            "simplbooks_transaction_id": "txn-501",
            "evidence_ref": "audit/2024-01#txn-501",
        }
        self.assert_artifact_valid(schema_name="action-batch.schema.json", artifact=batch)

    def test_action_batch_schema_restricts_top_level_manual_financial_dispositions(self) -> None:
        dependency = {
            "kind": "manual_statement_import_financial_transaction",
            "blocking": False,
            "reason": "Statement import completed.",
            "disposition": "bank_fee_payment",
            "statement_id": "archive:fee-1",
            "record_id": "fee-1",
            "date": "2024-01-15",
            "iban": "EE123",
            "currency": "EUR",
            "physical_signed_amount": -7.0,
            "source_ref": {"path": "normalized.json", "record_ref": "fee-1", "source_kind": "physical_bank"},
            "reviewed_rationale": "Reviewed fee.",
            "target": {"financial_transaction_kind": "bank-fee"},
            "split_parts": [],
            "split_proof": None,
            "statement_import_proof": {
                "status": "verified", "required_evidence": "live_discovery_or_audit",
                "simplbooks_transaction_id": "txn-501", "evidence_ref": "audit/2024-01#txn-501",
            },
        }
        batch = {
            "schema_version": "1.0", "company_slug": "example", "period": "2024-01",
            "generated_at": "2024-01-01T00:00:00Z", "batch_id": "example-2024-01",
            "approval_status": "draft", "already_present": [], "unresolved_dependencies": [dependency],
            "reference_artifacts": [
                {"kind": "posting_policy", "path": "policy.json", "sha256": "0" * 64},
                {"kind": "discovery_overview", "path": "overview.json", "sha256": "1" * 64},
            ], "actions": [],
        }

        for disposition in ("existing_invoice_receipt", "generated_purchase_payment"):
            with self.subTest(disposition=disposition):
                dependency["disposition"] = disposition
                with self.assertRaises(AssertionError):
                    self.assert_artifact_valid(schema_name="action-batch.schema.json", artifact=batch)

        dependency.update({
            "disposition": "reviewed_split",
            "physical_signed_amount": 10.0,
            "split_parts": [
                {"signed_amount": 20.0, "disposition": "existing_invoice_receipt", "target": {"simplbooks_id": "119"}},
                {"signed_amount": -10.0, "disposition": "generated_purchase_payment", "target": {"action_key": "purchase-1"}},
            ],
            "split_proof": {"signed_parts_total": 10.0, "physical_signed_amount": 10.0, "equation": "20.00 + -10.00 = 10.00"},
        })
        with self.assertRaises(AssertionError):
            self.assert_artifact_valid(schema_name="action-batch.schema.json", artifact=batch)

        dependency["split_parts"][1].update({
            "disposition": "bank_fee_payment",
            "target": {"financial_transaction_kind": "fee"},
        })
        self.assert_artifact_valid(schema_name="action-batch.schema.json", artifact=batch)

    def test_generated_year_overview_matches_strict_schema(self) -> None:
        class EmptyClient:
            company_id = "CID"

            def paginate(self, _path: str, **_kwargs: Any) -> list[dict[str, Any]]:
                return []

            def request(self, _path: str, **_kwargs: Any) -> dict[str, Any]:
                return {"data": []}

        overview = examine_simplbooks_year.build_year_overview(EmptyClient(), year=2024)
        self.assert_artifact_valid(schema_name="year-overview.schema.json", artifact=overview)


if __name__ == "__main__":
    unittest.main()


def bank_allocation_payload(*, allocations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "year": 2024,
        "normalized_bindings": [
            {
                "path": "companies/example/artifacts/normalized/2024-01.json",
                "sha256": "a" * 64,
            }
        ],
        "allocations": allocations,
    }


def bank_allocation(**overrides: Any) -> dict[str, Any]:
    allocation = {
        "statement_id": "archive:2024010212345678",
        "record_id": "bank-source:bank:2",
        "iban": "EE123",
        "period": "2024-01",
        "disposition": "existing_invoice_receipt",
        "amount": 330.0,
        "currency": "EUR",
        "target": {"simplbooks_id": "119", "document_type": "invoice"},
        "review": {"status": "approved", "rationale": "Exact invoice number and amount."},
    }
    allocation.update(overrides)
    return allocation
