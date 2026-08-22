from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookchecker  # noqa: E402, I001
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

    if isinstance(instance, list):
        if "minItems" in schema:
            assert len(instance) >= schema["minItems"], f"{path}: too few items"
        if "items" in schema:
            for index, item in enumerate(instance):
                validate(item, schema["items"], root_schema=root_schema, path=f"{path}[{index}]")

    if isinstance(instance, dict):
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

    def test_posting_policy_template_matches_strict_schema(self) -> None:
        artifact = json.loads((ROOT / "templates/posting-policy.template.json").read_text(encoding="utf-8"))
        self.assert_artifact_valid(schema_name="posting-policy.schema.json", artifact=artifact)

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
            "sales", "refunds", "fees", "payouts", "bank_transactions", "purchase_expenses",
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
        ]
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
