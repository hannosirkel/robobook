#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from simplbooks_api import SimplbooksClient, SimplbooksError, load_token, resolve_company_id, utc_now_iso


MANUAL_ACTION_TYPE = "manual_inventory_writeoff"
MANUAL_ACTION_STATUSES = frozenset({"required", "completed", "verified"})


def decimal_value(value: Any, *, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SimplbooksError(f"Manual inventory action {field_name} must be numeric.") from exc
    if not parsed.is_finite():
        raise SimplbooksError(f"Manual inventory action {field_name} must be finite.")
    return parsed


def validate_manual_inventory_action(action: dict[str, Any]) -> None:
    required = (
        "effective_date",
        "article_id",
        "warehouse_id",
        "quantity",
        "expense_account_id",
        "expected_remnant_after",
        "reason",
        "approval",
        "status",
        "source_refs",
    )
    if str(action.get("action_type") or "") != MANUAL_ACTION_TYPE:
        raise SimplbooksError(f"Manual inventory action must use action_type {MANUAL_ACTION_TYPE!r}.")
    missing = [field for field in required if action.get(field) in (None, "")]
    if missing:
        raise SimplbooksError(f"Manual inventory action is missing required fields: {', '.join(missing)}.")
    try:
        date.fromisoformat(str(action["effective_date"]))
    except ValueError as exc:
        raise SimplbooksError("Manual inventory action effective_date must be YYYY-MM-DD.") from exc
    if decimal_value(action["quantity"], field_name="quantity") <= 0:
        raise SimplbooksError("Manual inventory action quantity must be positive.")
    if decimal_value(action["expected_remnant_after"], field_name="expected_remnant_after") < 0:
        raise SimplbooksError("Manual inventory action expected_remnant_after cannot be negative.")
    if str(action["status"]) not in MANUAL_ACTION_STATUSES:
        raise SimplbooksError("Manual inventory action status must be required, completed, or verified.")
    if not isinstance(action["source_refs"], list) or not action["source_refs"]:
        raise SimplbooksError("Manual inventory action requires at least one source reference.")


def load_manual_inventory_actions(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SimplbooksError(f"Manual inventory action file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SimplbooksError(f"Invalid JSON in manual inventory action file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SimplbooksError(f"Manual inventory action file {path} must contain an object.")
    validate_manual_inventory_action(payload)
    return payload


def remnant_value(action: dict[str, Any], remnant_response: dict[str, Any]) -> Decimal:
    try:
        data = remnant_response["data"]
        article = data[str(action["article_id"])]
        value = article[str(action["warehouse_id"])]
    except (KeyError, TypeError) as exc:
        raise SimplbooksError("Dated inventory remnant response did not contain the requested article and warehouse.") from exc
    return decimal_value(value, field_name="dated remnant")


def evaluate_inventory_action(action: dict[str, Any], remnant_response: dict[str, Any]) -> list[str]:
    try:
        validate_manual_inventory_action(action)
        actual = remnant_value(action, remnant_response)
    except SimplbooksError as exc:
        return [str(exc)]
    expected = decimal_value(action["expected_remnant_after"], field_name="expected_remnant_after")
    if actual != expected:
        return [f"Dated remnant {actual} does not match expected remnant {expected}."]
    return []


def verification_evidence_path(*, company_dir: Path, action: dict[str, Any]) -> Path:
    year = str(action["effective_date"])[:4]
    return company_dir / "artifacts" / "discovery" / f"{year}-inventory-remnant-verification.json"


def verify_inventory_action(
    *,
    action: dict[str, Any],
    client: SimplbooksClient,
    company_dir: Path,
    verified_at: str | None = None,
) -> dict[str, Any]:
    validate_manual_inventory_action(action)
    remnant_response = client.request(
        f"articles/remnant/{action['article_id']}",
        method="POST",
        payload={"warehouse_id": str(action["warehouse_id"]), "date": str(action["effective_date"])},
    )
    evidence = {
        "action_type": MANUAL_ACTION_TYPE,
        "effective_date": str(action["effective_date"]),
        "article_id": str(action["article_id"]),
        "warehouse_id": str(action["warehouse_id"]),
        "expected_remnant_after": action["expected_remnant_after"],
        "verified_at": verified_at or utc_now_iso(),
        "remnant_response": remnant_response,
        "errors": evaluate_inventory_action(action, remnant_response),
    }
    path = verification_evidence_path(company_dir=company_dir, action=action)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only dated Simplbooks inventory remnant verification")
    parser.add_argument("--company-dir", required=True, help="Company workspace containing the manual action")
    parser.add_argument("--year", required=True, type=int, help="Year of the manual inventory action")
    parser.add_argument("--action", help="Optional manual inventory action JSON override")
    parser.add_argument("--company-id", help="Optional Simplbooks company ID override")
    parser.add_argument("--token-file", default=".apikey", help="Simplbooks API token file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    company_dir = Path(args.company_dir)
    action_path = Path(args.action) if args.action else company_dir / "artifacts" / "actions" / f"{args.year}-inventory-manual.json"
    action = load_manual_inventory_actions(action_path)
    client = SimplbooksClient(resolve_company_id(args.company_id, company_dir=str(company_dir)), load_token(args.token_file))
    evidence = verify_inventory_action(action=action, client=client, company_dir=company_dir)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if not evidence["errors"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
