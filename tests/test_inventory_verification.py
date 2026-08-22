from __future__ import annotations  # noqa: I001

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import inventory_verification  # noqa: E402


def manual_action_fixture(*, quantity: int = 5, expected_after: int = 0, status: str = "completed") -> dict:
    return {
        "action_type": "manual_inventory_writeoff",
        "effective_date": "2024-06-30",
        "article_id": "10",
        "warehouse_id": "20",
        "quantity": quantity,
        "expense_account_id": "30",
        "expected_remnant_after": expected_after,
        "reason": "Obsolete inventory",
        "approval": "reviewed",
        "status": status,
        "source_refs": [
            {
                "source_id": "inventory-decision",
                "path": "artifacts/inventory-decision.json",
                "row_ref": None,
                "page_ref": None,
                "notes": None,
            }
        ],
    }


class FakeClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def request(self, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        self.calls.append({"path": path, "method": method, "payload": payload})
        return self.response


class InventoryVerificationTests(unittest.TestCase):
    def test_loader_reads_a_single_manual_action_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2024-inventory-manual.json"
            expected = manual_action_fixture(status="required")
            path.write_text(json.dumps(expected), encoding="utf-8")

            self.assertEqual(inventory_verification.load_manual_inventory_actions(path), expected)

    def test_inventory_writeoff_verifies_expected_remnant(self) -> None:
        action = manual_action_fixture(quantity=5, expected_after=0, status="completed")

        self.assertEqual(
            inventory_verification.evaluate_inventory_action(action, {"data": {"10": {"20": 0}}}),
            [],
        )

    def test_inventory_writeoff_rejects_a_different_dated_remnant(self) -> None:
        action = manual_action_fixture(expected_after=0)

        errors = inventory_verification.evaluate_inventory_action(action, {"data": {"10": {"20": 1}}})

        self.assertTrue(any("expected remnant" in error.lower() for error in errors))

    def test_verification_uses_dated_remnant_request_and_persists_response(self) -> None:
        action = manual_action_fixture()
        client = FakeClient({"data": {"10": {"20": 0}}})
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            result = inventory_verification.verify_inventory_action(
                action=action,
                client=client,
                company_dir=company_dir,
                verified_at="2026-08-21T00:00:00Z",
            )
            evidence_path = company_dir / "artifacts" / "discovery" / "2024-inventory-remnant-verification.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(client.calls, [{"path": "articles/remnant/10", "method": "POST", "payload": {"warehouse_id": "20", "date": "2024-06-30"}}])
        self.assertEqual(result["errors"], [])
        self.assertEqual(evidence["verified_at"], "2026-08-21T00:00:00Z")
        self.assertEqual(evidence["remnant_response"], {"data": {"10": {"20": 0}}})


if __name__ == "__main__":
    unittest.main()
