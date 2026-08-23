from __future__ import annotations  # noqa: I001

import json
import sys
import tempfile
import unittest
from decimal import Decimal
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


def transfer_action_fixture(**overrides: object) -> dict:
    action = {
        "action_type": "warehouse_transfer",
        "effective_date": "2024-08-29",
        "article_id": "3",
        "source_warehouse_id": "1",
        "destination_warehouse_id": "9",
        "quantity": 100,
        "remnant_before": {"1": 1000, "9": 0},
        "remnant_after": {"1": 900, "9": 100},
        "reason": "Reviewed historical distributor transfer",
        "approval": "reviewed",
        "status": "complete",
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
    action.update(overrides)
    return action


def stock_evidence_fixture(**overrides: object) -> dict:
    evidence = {
        "article_id": "3",
        "selected_closing": {"1": 900, "9": 176},
        "warehouses": {
            "1": {"opening": 1000, "purchases": 0, "transfers_in": 0, "transfers_out": 100,
                  "sales": 0, "writeoffs": 0, "adjustments": 0},
            "9": {"opening": 0, "purchases": 100, "transfers_in": 100, "transfers_out": 0,
                  "sales": 24, "writeoffs": 0, "adjustments": 0},
        },
    }
    evidence.update(overrides)
    return evidence


class WarehouseTransferTests(unittest.TestCase):
    def test_transfer_preserves_total_and_moves_reviewed_quantity(self) -> None:
        result = inventory_verification.evaluate_transfer(transfer_action_fixture())

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["moved"], Decimal(100))

    def test_a_transfer_that_changes_the_total_is_rejected(self) -> None:
        action = transfer_action_fixture(remnant_after={"1": 900, "9": 120})

        errors = inventory_verification.evaluate_transfer(action)["errors"]

        self.assertIn("total stock", " ".join(errors))

    def test_a_transfer_moving_a_different_quantity_is_rejected(self) -> None:
        action = transfer_action_fixture(remnant_after={"1": 950, "9": 50})

        errors = inventory_verification.evaluate_transfer(action)["errors"]

        self.assertIn("reviewed quantity", " ".join(errors))

    def test_a_transfer_between_unnamed_warehouses_is_rejected(self) -> None:
        action = transfer_action_fixture(remnant_before={"1": 1000, "7": 0})

        errors = inventory_verification.evaluate_transfer(action)["errors"]

        self.assertIn("warehouse", " ".join(errors))

    def test_a_transfer_to_its_own_source_is_rejected(self) -> None:
        action = transfer_action_fixture(destination_warehouse_id="1")

        with self.assertRaisesRegex(inventory_verification.SimplbooksError, "same warehouse"):
            inventory_verification.validate_manual_inventory_action(action)

    def test_a_transfer_validates_as_a_typed_manual_action(self) -> None:
        inventory_verification.validate_manual_inventory_action(transfer_action_fixture())

    def test_an_unknown_manual_action_type_is_rejected(self) -> None:
        action = transfer_action_fixture(action_type="stock_move")

        with self.assertRaisesRegex(inventory_verification.SimplbooksError, "action_type"):
            inventory_verification.validate_manual_inventory_action(action)


class StockEquationTests(unittest.TestCase):
    def test_stock_equation_is_evaluated_per_warehouse_and_in_aggregate(self) -> None:
        result = inventory_verification.evaluate_stock_equation(stock_evidence_fixture())

        self.assertEqual(result["warehouses"]["9"]["closing"], Decimal(176))
        self.assertEqual(result["warehouses"]["1"]["closing"], Decimal(900))
        self.assertEqual(result["aggregate"]["difference"], Decimal(0))
        self.assertIsNone(result["instruction"])

    def test_a_warehouse_difference_is_reported_even_when_the_total_agrees(self) -> None:
        evidence = stock_evidence_fixture(selected_closing={"1": 890, "9": 186})

        result = inventory_verification.evaluate_stock_equation(evidence)

        self.assertEqual(result["aggregate"]["difference"], Decimal(0))
        self.assertEqual(result["warehouses"]["1"]["difference"], Decimal(-10))
        self.assertEqual(result["warehouses"]["9"]["difference"], Decimal(10))
        self.assertNotEqual(result["errors"], [])

    def test_a_shortfall_emits_a_non_executable_decrease_instruction(self) -> None:
        evidence = stock_evidence_fixture(selected_closing={"1": 900, "9": 170})

        result = inventory_verification.evaluate_stock_equation(evidence, inventory_change_account_id="115")

        instruction = result["instruction"]
        self.assertEqual(instruction["action_type"], "year_end_adjustment")
        self.assertEqual(instruction["direction"], "decrease")
        self.assertEqual(instruction["quantity"], Decimal(6))
        self.assertEqual(instruction["warehouse_id"], "9")
        self.assertEqual(instruction["expense_account_id"], "115")
        self.assertEqual(instruction["status"], "requires_separate_approval")

    def test_a_surplus_emits_an_increase_instruction(self) -> None:
        evidence = stock_evidence_fixture(selected_closing={"1": 900, "9": 180})

        instruction = inventory_verification.evaluate_stock_equation(
            evidence, inventory_change_account_id="115"
        )["instruction"]

        self.assertEqual(instruction["direction"], "increase")
        self.assertEqual(instruction["quantity"], Decimal(4))

    def test_an_instruction_is_never_an_executable_api_action(self) -> None:
        evidence = stock_evidence_fixture(selected_closing={"1": 900, "9": 170})

        instruction = inventory_verification.evaluate_stock_equation(
            evidence, inventory_change_account_id="115"
        )["instruction"]

        self.assertNotIn("endpoint", instruction)
        self.assertNotIn("method", instruction)

    def test_a_warehouse_without_a_selected_count_is_an_error(self) -> None:
        evidence = stock_evidence_fixture(selected_closing={"1": 900})

        result = inventory_verification.evaluate_stock_equation(evidence)

        self.assertIn("no selected closing count", " ".join(result["errors"]))


REMNANT_ACTION = {"article_id": "3", "warehouse_id": "9"}


class RemnantParsingTests(unittest.TestCase):

    def test_a_warehouse_that_never_held_the_article_reads_as_zero(self) -> None:
        # SimplBooks returns an empty list, not 0, for an article with no stock rows.
        self.assertEqual(
            inventory_verification.remnant_value(REMNANT_ACTION, {"data": {"3": []}}), Decimal(0)
        )

    def test_a_warehouse_emptied_to_zero_still_reads_as_zero(self) -> None:
        self.assertEqual(
            inventory_verification.remnant_value(REMNANT_ACTION, {"data": {"3": {"9": 0}}}), Decimal(0)
        )

    def test_an_actual_quantity_is_read_exactly(self) -> None:
        self.assertEqual(
            inventory_verification.remnant_value(REMNANT_ACTION, {"data": {"3": {"9": 640}}}), Decimal(640)
        )

    def test_an_unknown_article_is_still_a_lookup_failure(self) -> None:
        with self.assertRaisesRegex(inventory_verification.SimplbooksError, "did not contain"):
            inventory_verification.remnant_value(REMNANT_ACTION, {"data": {"5": {"9": 1}}})

    def test_an_unknown_warehouse_is_still_a_lookup_failure(self) -> None:
        with self.assertRaisesRegex(inventory_verification.SimplbooksError, "did not contain"):
            inventory_verification.remnant_value(REMNANT_ACTION, {"data": {"3": {"1": 972}}})


if __name__ == "__main__":
    unittest.main()
