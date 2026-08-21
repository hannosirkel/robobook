#!/usr/bin/env python3
"""Build and validate reviewed, fixed-gross WooCommerce VAT allocations."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Sequence

from simplbooks_api import SimplbooksError, resolve_company_slug


CENT = Decimal("0.01")


class WooTaxError(SimplbooksError):
    """Raised when a reviewed Woo VAT allocation cannot be used safely."""


@dataclass(frozen=True)
class VatPeriod:
    start: date
    end: date | None
    rate: Decimal
    goods_vat_type_id: str
    shipping_vat_type_id: str


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_value(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise WooTaxError("Boolean values are not valid monetary amounts.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise WooTaxError(f"Invalid decimal value: {value!r}.") from error
    if not result.is_finite():
        raise WooTaxError(f"Invalid decimal value: {value!r}.")
    return result


def date_value(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise WooTaxError(f"Invalid ISO date: {value!r}.") from error


def corrected_component(fixed_gross: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    if fixed_gross < 0 or rate < 0:
        raise WooTaxError("Fixed gross and VAT rate must be non-negative.")
    vat = money(fixed_gross * rate / (Decimal("100") + rate))
    return fixed_gross - vat, vat


def select_vat_period(event_date: date, periods: Sequence[VatPeriod]) -> VatPeriod:
    matches = [item for item in periods if item.start <= event_date and (item.end is None or event_date <= item.end)]
    if len(matches) != 1:
        raise WooTaxError(f"Expected exactly one VAT profile for {event_date}, found {len(matches)}.")
    return matches[0]


def vat_periods_from_payload(payload: dict[str, Any]) -> list[VatPeriod]:
    periods: list[VatPeriod] = []
    for item in payload.get("vat_periods") or []:
        if not isinstance(item, dict):
            raise WooTaxError("VAT periods must be objects.")
        start = date_value(item.get("start"))
        end = date_value(item["end"]) if item.get("end") not in (None, "") else None
        if end is not None and end < start:
            raise WooTaxError(f"VAT profile ending {end} precedes its start {start}.")
        rate = decimal_value(item.get("rate"))
        if rate < 0:
            raise WooTaxError("VAT profile rate must be non-negative.")
        periods.append(
            VatPeriod(
                start=start,
                end=end,
                rate=rate,
                goods_vat_type_id=str(item.get("goods_vat_type_id") or ""),
                shipping_vat_type_id=str(item.get("shipping_vat_type_id") or ""),
            )
        )
    return periods


def same_money(left: Decimal, right: Decimal) -> bool:
    return money(left) == money(right)


def build_month_totals(allocations: list[dict[str, Any]]) -> dict[str, dict[str, Decimal]]:
    totals: dict[str, dict[str, Decimal]] = {}
    for allocation in allocations:
        period = str(allocation.get("period") or "")
        if not period:
            raise WooTaxError("Allocation period is required to build monthly totals.")
        bucket = totals.setdefault(period, {"gross": Decimal("0"), "original_vat": Decimal("0"), "corrected_vat": Decimal("0")})
        bucket["gross"] += decimal_value(allocation.get("fixed_product_gross"))
        bucket["gross"] += decimal_value(allocation.get("fixed_shipping_gross"))
        bucket["original_vat"] += decimal_value(allocation.get("original_order_tax"))
        bucket["original_vat"] += decimal_value(allocation.get("original_shipping_tax"))
        bucket["corrected_vat"] += decimal_value(allocation.get("corrected_product_vat"))
        bucket["corrected_vat"] += decimal_value(allocation.get("corrected_shipping_vat"))
    return {
        period: {name: money(amount) for name, amount in values.items()}
        for period, values in sorted(totals.items())
    }


def _decimal_field(allocation: dict[str, Any], field: str, errors: list[str], label: str) -> Decimal | None:
    try:
        value = decimal_value(allocation.get(field))
    except WooTaxError:
        errors.append(f"allocation {label} has invalid {field}")
        return None
    if value < 0:
        errors.append(f"allocation {label} has negative {field}")
        return None
    if value != money(value):
        errors.append(f"allocation {label} has fractional-cent {field}")
        return None
    return value


def validate_allocation(payload: dict[str, Any]) -> list[str]:
    """Return deterministic errors for an annual allocation; do not alter *payload*."""
    errors: list[str] = []
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        errors.append("policy must be an object")
    elif policy.get("merchant_absorbs_vat") is not True:
        errors.append("merchant_absorbs_vat must be true")
    source_rows = payload.get("source_rows") or []
    allocations = payload.get("allocations") or []
    if not isinstance(source_rows, list):
        return ["source_rows must be a list"]
    if not isinstance(allocations, list):
        return ["allocations must be a list"]

    try:
        periods = vat_periods_from_payload(payload)
    except WooTaxError as error:
        errors.append(str(error))
        periods = []

    source_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(source_rows):
        if not isinstance(row, dict) or not str(row.get("source_row_id") or ""):
            errors.append(f"source row {index} has no source_row_id")
            continue
        row_id = str(row["source_row_id"])
        if row_id in source_by_id:
            errors.append(f"source row {row_id} appears more than once")
        source_by_id[row_id] = row

    grouped: dict[str, list[dict[str, Any]]] = {row_id: [] for row_id in source_by_id}
    seen_orders: set[str] = set()
    for index, allocation in enumerate(allocations):
        if not isinstance(allocation, dict):
            errors.append(f"allocation {index} must be an object")
            continue
        label = str(allocation.get("order_id") or f"#{index}")
        order_id = str(allocation.get("order_id") or "")
        if not order_id:
            errors.append(f"allocation {label} has no order_id")
        elif order_id in seen_orders:
            errors.append(f"taxable order is allocated more than once: {order_id}")
        else:
            seen_orders.add(order_id)

        row_id = str(allocation.get("source_row_id") or "")
        if row_id not in source_by_id:
            errors.append(f"allocation {label} references unknown source row {row_id or '<empty>'}")
        else:
            grouped[row_id].append(allocation)

        product_gross = _decimal_field(allocation, "fixed_product_gross", errors, label)
        shipping_gross = _decimal_field(allocation, "fixed_shipping_gross", errors, label)
        product_vat = _decimal_field(allocation, "corrected_product_vat", errors, label)
        shipping_vat = _decimal_field(allocation, "corrected_shipping_vat", errors, label)
        _decimal_field(allocation, "original_order_tax", errors, label)
        _decimal_field(allocation, "original_shipping_tax", errors, label)

        try:
            event_date = date_value(allocation.get("event_date"))
            expected_period = event_date.strftime("%Y-%m")
            if allocation.get("period") != expected_period:
                errors.append(f"allocation {label} period does not match event_date")
            profile = select_vat_period(event_date, periods)
            corrected_rate = decimal_value(allocation.get("corrected_rate"))
            if corrected_rate != profile.rate:
                errors.append(f"allocation {label} corrected_rate does not match effective VAT profile")
        except WooTaxError as error:
            errors.append(f"allocation {label}: {error}")
            profile = None

        if profile is not None and product_gross is not None and product_vat is not None:
            _, expected_vat = corrected_component(product_gross, profile.rate)
            if product_vat != expected_vat:
                errors.append(f"allocation {label} corrected product VAT does not match fixed-gross calculation")
        if profile is not None and shipping_gross is not None and shipping_vat is not None:
            _, expected_vat = corrected_component(shipping_gross, profile.rate)
            if shipping_vat != expected_vat:
                errors.append(f"allocation {label} corrected shipping VAT does not match fixed-gross calculation")

    for row_id, source_row in source_by_id.items():
        matched = grouped[row_id]
        source_amounts: dict[str, Decimal] = {}
        for field in ("order_tax", "shipping_tax", "total_tax"):
            try:
                amount = decimal_value(source_row.get(field))
                if amount < 0:
                    errors.append(f"source row {row_id} has negative {field}")
                elif amount != money(amount):
                    errors.append(f"source row {row_id} has fractional-cent {field}")
                else:
                    source_amounts[field] = amount
            except WooTaxError:
                errors.append(f"source row {row_id} has invalid {field}")
        if len(source_amounts) == 3 and source_amounts["total_tax"] != source_amounts["order_tax"] + source_amounts["shipping_tax"]:
            errors.append(f"source row {row_id} total_tax does not equal order_tax plus shipping_tax")
        try:
            expected_orders = int(source_row.get("orders"))
            if decimal_value(source_row.get("orders")) != Decimal(expected_orders) or expected_orders <= 0:
                raise ValueError
        except (ValueError, WooTaxError):
            errors.append(f"source row {row_id} has invalid Orders count")
            continue
        if len(matched) != expected_orders:
            errors.append(f"source row {row_id} allocated order count {len(matched)} does not equal source Orders {expected_orders}")

        for allocation_field, source_field, description in (
            ("original_order_tax", "order_tax", "original order tax"),
            ("original_shipping_tax", "shipping_tax", "original shipping tax"),
        ):
            try:
                actual = sum((decimal_value(item.get(allocation_field)) for item in matched), Decimal("0"))
                expected = decimal_value(source_row.get(source_field))
                if not same_money(actual, expected):
                    errors.append(f"source row {row_id} allocated {description} does not equal source {source_field}")
            except WooTaxError:
                errors.append(f"source row {row_id} has invalid {source_field}")
        try:
            original_total = sum(
                (decimal_value(item.get("original_order_tax")) + decimal_value(item.get("original_shipping_tax")) for item in matched),
                Decimal("0"),
            )
            expected_total = decimal_value(source_row.get("total_tax"))
            if not same_money(original_total, expected_total):
                errors.append(f"source row {row_id} allocated original total tax does not equal source total_tax")
        except WooTaxError:
            errors.append(f"source row {row_id} has invalid total_tax")

    try:
        actual_totals = build_month_totals(allocations)
        supplied_totals = payload.get("monthly_totals")
        if not isinstance(supplied_totals, dict):
            errors.append("monthly_totals must be an object")
        elif set(supplied_totals) != set(actual_totals):
            errors.append("monthly totals periods do not match allocations")
        if isinstance(supplied_totals, dict):
            for period, actual in actual_totals.items():
                supplied = supplied_totals.get(period) or {}
                if not isinstance(supplied, dict):
                    errors.append(f"monthly totals {period} must be an object")
                    continue
                for field, amount in actual.items():
                    supplied_amount = decimal_value(supplied.get(field))
                    if supplied_amount < 0:
                        errors.append(f"monthly totals {period} has negative {field}")
                    elif supplied_amount != money(supplied_amount):
                        errors.append(f"monthly totals {period} has fractional-cent {field}")
                    elif supplied_amount != amount:
                        errors.append(f"monthly totals {period} {field} do not match allocations")
    except WooTaxError as error:
        errors.append(str(error))

    return sorted(set(errors))


def decimal_number(value: Decimal) -> float:
    return float(money(value))


def json_month_totals(totals: dict[str, dict[str, Decimal]]) -> dict[str, dict[str, float]]:
    return {period: {name: decimal_number(amount) for name, amount in values.items()} for period, values in totals.items()}


def build_allocation(review: dict[str, Any]) -> dict[str, Any]:
    """Derive policy-controlled VAT fields from explicit reviewed order mappings."""
    artifact = copy.deepcopy(review)
    periods = vat_periods_from_payload(artifact)
    allocations = artifact.get("allocations") or []
    if not isinstance(allocations, list):
        raise WooTaxError("allocations must be a list.")
    for allocation in allocations:
        if not isinstance(allocation, dict):
            raise WooTaxError("allocations must contain objects.")
        event_date = date_value(allocation.get("event_date"))
        profile = select_vat_period(event_date, periods)
        product_gross = decimal_value(allocation.get("fixed_product_gross"))
        shipping_gross = decimal_value(allocation.get("fixed_shipping_gross"))
        _, product_vat = corrected_component(product_gross, profile.rate)
        _, shipping_vat = corrected_component(shipping_gross, profile.rate)
        allocation["period"] = event_date.strftime("%Y-%m")
        allocation["corrected_rate"] = float(profile.rate)
        allocation["corrected_product_vat"] = decimal_number(product_vat)
        allocation["corrected_shipping_vat"] = decimal_number(shipping_vat)
    artifact["monthly_totals"] = json_month_totals(build_month_totals(allocations))
    errors = validate_allocation(artifact)
    artifact["validation"] = {"status": "pass" if not errors else "fail", "errors": errors}
    return artifact


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WooTaxError(f"Unable to read allocation JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise WooTaxError(f"Allocation JSON {path} must contain an object.")
    return payload


def annual_totals(payload: dict[str, Any]) -> dict[str, Decimal]:
    totals = build_month_totals(payload.get("allocations") or [])
    return {
        field: money(sum((monthly[field] for monthly in totals.values()), Decimal("0")))
        for field in ("gross", "original_vat", "corrected_vat")
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and validate reviewed fixed-gross Woo VAT allocations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="derive allocation VAT from a reviewed JSON mapping")
    build.add_argument("--review", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    validate = subparsers.add_parser("validate", help="validate a company annual allocation")
    validate.add_argument("--company-dir", required=True, type=Path)
    validate.add_argument("--year", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            artifact = build_allocation(load_json(args.review))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return 0 if artifact["validation"]["status"] == "pass" else 1

        company_slug = resolve_company_slug(company_dir=str(args.company_dir))
        if not company_slug:
            raise WooTaxError(f"Company slug is missing from {args.company_dir / 'METADATA.md'}.")
        path = args.company_dir / "artifacts" / "vat" / f"{args.year}-woo-tax-allocation.json"
        artifact = load_json(path)
        errors = validate_allocation(artifact)
        if artifact.get("company_slug") != company_slug:
            errors.append(f"allocation company_slug does not match {company_slug}")
        if artifact.get("year") != args.year:
            errors.append(f"allocation year does not match {args.year}")
        totals = annual_totals(artifact)
        print(json.dumps({"gross": decimal_number(totals["gross"]), "original_vat": decimal_number(totals["original_vat"]), "corrected_vat": decimal_number(totals["corrected_vat"]), "errors": sorted(set(errors))}, sort_keys=True))
        return 1 if errors else 0
    except WooTaxError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
