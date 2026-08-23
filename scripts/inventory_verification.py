#!/usr/bin/env python3
from __future__ import annotations  # noqa: EXE001, I001

import argparse
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from simplbooks_api import SimplbooksClient, SimplbooksError, load_token, resolve_company_id, utc_now_iso


MANUAL_ACTION_TYPE = "manual_inventory_writeoff"
MANUAL_ACTION_TYPES = frozenset({"manual_inventory_writeoff", "warehouse_transfer", "year_end_adjustment"})
MANUAL_ACTION_STATUSES = frozenset({"required", "completed", "verified", "complete"})

WRITEOFF_FIELDS = (
    "effective_date", "article_id", "warehouse_id", "quantity", "expense_account_id",
    "expected_remnant_after", "reason", "approval", "status", "source_refs",
)

TRANSFER_FIELDS = (
    "effective_date", "article_id", "source_warehouse_id", "destination_warehouse_id",
    "quantity", "remnant_before", "remnant_after", "reason", "approval", "status", "source_refs",
)

ADJUSTMENT_FIELDS = (
    "effective_date", "article_id", "warehouse_id", "direction", "quantity",
    "expense_account_id", "reason", "approval", "status", "source_refs",
)

MANUAL_ACTION_FIELDS = {
    "manual_inventory_writeoff": WRITEOFF_FIELDS,
    "warehouse_transfer": TRANSFER_FIELDS,
    "year_end_adjustment": ADJUSTMENT_FIELDS,
}

EQUATION_TERMS = ("opening", "purchases", "transfers_in", "transfers_out", "sales", "writeoffs", "adjustments")


def decimal_value(value: Any, *, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SimplbooksError(f"Manual inventory action {field_name} must be numeric.") from exc
    if not parsed.is_finite():
        raise SimplbooksError(f"Manual inventory action {field_name} must be finite.")
    return parsed


def _remnant_map(value: Any, *, field_name: str) -> dict[str, Decimal]:
    if not isinstance(value, dict) or not value:
        raise SimplbooksError(f"Manual inventory action {field_name} must map warehouse IDs to quantities.")
    return {
        str(warehouse_id): decimal_value(quantity, field_name=f"{field_name}[{warehouse_id}]")
        for warehouse_id, quantity in value.items()
    }


def _validate_writeoff(action: dict[str, Any]) -> None:
    if decimal_value(action["expected_remnant_after"], field_name="expected_remnant_after") < 0:
        raise SimplbooksError("Manual inventory action expected_remnant_after cannot be negative.")


def _validate_transfer_shape(action: dict[str, Any]) -> None:
    if str(action["source_warehouse_id"]) == str(action["destination_warehouse_id"]):
        raise SimplbooksError("A warehouse transfer cannot move stock to the same warehouse it came from.")
    _remnant_map(action["remnant_before"], field_name="remnant_before")
    _remnant_map(action["remnant_after"], field_name="remnant_after")


def _validate_adjustment(action: dict[str, Any]) -> None:
    if str(action["direction"]) not in {"increase", "decrease"}:
        raise SimplbooksError("Year-end adjustment direction must be increase or decrease.")


TYPE_VALIDATORS = {
    "manual_inventory_writeoff": _validate_writeoff,
    "warehouse_transfer": _validate_transfer_shape,
    "year_end_adjustment": _validate_adjustment,
}


def validate_manual_inventory_action(action: dict[str, Any]) -> None:
    """Validate one typed manual inventory action against the fields its type requires."""
    action_type = str(action.get("action_type") or "")
    if action_type not in MANUAL_ACTION_TYPES:
        raise SimplbooksError(
            f"Manual inventory action_type must be one of {sorted(MANUAL_ACTION_TYPES)}, got {action_type!r}."
        )
    missing = [field for field in MANUAL_ACTION_FIELDS[action_type] if action.get(field) in (None, "")]
    if missing:
        raise SimplbooksError(f"Manual inventory action is missing required fields: {', '.join(missing)}.")
    try:
        date.fromisoformat(str(action["effective_date"]))
    except ValueError as exc:
        raise SimplbooksError("Manual inventory action effective_date must be YYYY-MM-DD.") from exc
    if decimal_value(action["quantity"], field_name="quantity") <= 0:
        raise SimplbooksError("Manual inventory action quantity must be positive.")
    if str(action["status"]) not in MANUAL_ACTION_STATUSES:
        raise SimplbooksError(f"Manual inventory action status must be one of {sorted(MANUAL_ACTION_STATUSES)}.")
    if not isinstance(action["source_refs"], list) or not action["source_refs"]:
        raise SimplbooksError("Manual inventory action requires at least one source reference.")
    TYPE_VALIDATORS[action_type](action)


def evaluate_transfer(action: dict[str, Any]) -> dict[str, Any]:
    """Prove one historical transfer moved exactly the reviewed quantity and created none.

    A transfer only relocates stock, so the totals before and after must be equal. A
    transfer that changes the total is a write-off or a receipt wearing a transfer's name.
    """
    validate_manual_inventory_action(action)
    before = _remnant_map(action["remnant_before"], field_name="remnant_before")
    after = _remnant_map(action["remnant_after"], field_name="remnant_after")
    source = str(action["source_warehouse_id"])
    destination = str(action["destination_warehouse_id"])
    quantity = decimal_value(action["quantity"], field_name="quantity")

    errors: list[str] = []
    named = {source, destination}
    for label, snapshot in (("remnant_before", before), ("remnant_after", after)):
        if not named <= set(snapshot):
            errors.append(
                f"{label} does not cover both the source and destination warehouse {sorted(named)}."
            )
    if errors:
        return {"errors": errors, "moved": None}

    total_before = sum(before.values(), Decimal(0))
    total_after = sum(after.values(), Decimal(0))
    if total_before != total_after:
        errors.append(
            f"Transfer changed total stock from {total_before} to {total_after}; a transfer only relocates it."
        )
    moved_out = before[source] - after[source]
    moved_in = after[destination] - before[destination]
    if moved_out != quantity or moved_in != quantity:
        errors.append(
            f"Transfer moved {moved_out} out and {moved_in} in, not the reviewed quantity {quantity}."
        )
    return {"errors": errors, "moved": moved_in if not errors else None}


def _warehouse_equation(terms: Any, *, warehouse_id: str) -> tuple[Decimal, list[str]]:
    if not isinstance(terms, dict):
        return Decimal(0), [f"Warehouse {warehouse_id} has no stock-movement evidence."]
    values = {
        term: decimal_value(terms.get(term, 0), field_name=f"{warehouse_id}.{term}")
        for term in EQUATION_TERMS
    }
    closing = (
        values["opening"] + values["purchases"] + values["transfers_in"]
        - values["transfers_out"] - values["sales"] - values["writeoffs"] + values["adjustments"]
    )
    return closing, []


def evaluate_stock_equation(
    evidence: dict[str, Any], *, inventory_change_account_id: str | None = None
) -> dict[str, Any]:
    """Compare computed closing stock with the selected count, per warehouse and in total.

    Per-warehouse first: two offsetting warehouse errors leave the aggregate at zero, so
    an aggregate match alone would declare a wrong inventory correct.
    """
    warehouses = evidence.get("warehouses")
    if not isinstance(warehouses, dict) or not warehouses:
        raise SimplbooksError("Stock equation evidence requires per-warehouse movement terms.")
    selected = evidence.get("selected_closing")
    if not isinstance(selected, dict):
        raise SimplbooksError("Stock equation evidence requires the selected closing count.")

    errors: list[str] = []
    results: dict[str, dict[str, Decimal]] = {}
    for warehouse_id in sorted(warehouses):
        closing, warehouse_errors = _warehouse_equation(warehouses[warehouse_id], warehouse_id=warehouse_id)
        errors.extend(warehouse_errors)
        if warehouse_id not in selected:
            errors.append(f"Warehouse {warehouse_id} has no selected closing count to reconcile against.")
            results[warehouse_id] = {"closing": closing, "selected": Decimal(0), "difference": Decimal(0)}
            continue
        selected_closing = decimal_value(selected[warehouse_id], field_name=f"selected_closing[{warehouse_id}]")
        results[warehouse_id] = {
            "closing": closing,
            "selected": selected_closing,
            "difference": selected_closing - closing,
        }
    for warehouse_id in sorted(set(selected) - set(warehouses)):
        errors.append(f"Selected closing count names warehouse {warehouse_id} with no movement evidence.")

    aggregate_closing = sum((item["closing"] for item in results.values()), Decimal(0))
    aggregate_selected = sum((item["selected"] for item in results.values()), Decimal(0))
    differing = [
        warehouse_id for warehouse_id, item in results.items() if item["difference"] != 0
    ]
    for warehouse_id in differing:
        errors.append(
            f"Warehouse {warehouse_id} closing {results[warehouse_id]['closing']} differs from the "
            f"selected count {results[warehouse_id]['selected']}."
        )
    return {
        "article_id": str(evidence.get("article_id") or ""),
        "warehouses": results,
        "aggregate": {
            "closing": aggregate_closing,
            "selected": aggregate_selected,
            "difference": aggregate_selected - aggregate_closing,
        },
        "errors": errors,
        "instruction": _adjustment_instruction(
            evidence,
            results=results,
            differing=differing,
            inventory_change_account_id=inventory_change_account_id,
        ),
    }


def _adjustment_instruction(
    evidence: dict[str, Any],
    *,
    results: dict[str, dict[str, Decimal]],
    differing: list[str],
    inventory_change_account_id: str | None,
) -> dict[str, Any] | None:
    """Describe the single correction a reviewer would have to approve, never execute it."""
    if len(differing) != 1 or inventory_change_account_id is None:
        return None
    warehouse_id = differing[0]
    difference = results[warehouse_id]["difference"]
    return {
        "action_type": "year_end_adjustment",
        "effective_date": str(evidence.get("effective_date") or ""),
        "article_id": str(evidence.get("article_id") or ""),
        "warehouse_id": warehouse_id,
        "direction": "increase" if difference > 0 else "decrease",
        "quantity": abs(difference),
        "expense_account_id": str(inventory_change_account_id),
        "status": "requires_separate_approval",
    }


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
    """Read one dated remnant, treating "no stock rows at all" as the zero it is.

    SimplBooks answers an article with no stock anywhere with an empty list rather than
    a zero. Left unhandled that is indistinguishable from a genuine lookup failure, so a
    warehouse that has simply never held the article would look like a wrong ID.
    """
    try:
        data = remnant_response["data"]
        article = data[str(action["article_id"])]
    except (KeyError, TypeError) as exc:
        raise SimplbooksError("Dated inventory remnant response did not contain the requested article and warehouse.") from exc
    if isinstance(article, list):
        if article:
            raise SimplbooksError("Dated inventory remnant response has an unexpected article payload.")
        return Decimal(0)
    try:
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
