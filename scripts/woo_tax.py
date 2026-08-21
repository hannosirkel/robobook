#!/usr/bin/env python3
"""Build and validate reviewed, fixed-gross WooCommerce VAT allocations."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
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
        goods_vat_type_id = str(item.get("goods_vat_type_id") or "").strip()
        shipping_vat_type_id = str(item.get("shipping_vat_type_id") or "").strip()
        if not goods_vat_type_id.isdigit() or not shipping_vat_type_id.isdigit():
            raise WooTaxError("VAT profiles require usable integer-like goods and shipping VAT type IDs.")
        periods.append(
            VatPeriod(
                start=start,
                end=end,
                rate=rate,
                goods_vat_type_id=goods_vat_type_id,
                shipping_vat_type_id=shipping_vat_type_id,
            )
        )
    if not periods:
        raise WooTaxError("At least one effective VAT profile is required.")
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
    source_files = payload.get("source_files") or []
    source_rows = payload.get("source_rows") or []
    allocations = payload.get("allocations") or []
    if not isinstance(source_files, list):
        return ["source_files must be a list"]
    if not isinstance(source_rows, list):
        return ["source_rows must be a list"]
    if not isinstance(allocations, list):
        return ["allocations must be a list"]
    if not source_rows:
        errors.append("source_rows must contain canonical Woo tax evidence")
    if not allocations:
        errors.append("allocations must contain every tax-evidenced order")

    source_file_ids: set[str] = set()
    for index, item in enumerate(source_files):
        if not isinstance(item, dict):
            errors.append(f"source file {index} must be an object")
            continue
        source_id = str(item.get("source_id") or "").strip()
        sha256 = str(item.get("sha256") or "").strip()
        if not source_id:
            errors.append(f"source file {index} has no source_id")
        elif source_id in source_file_ids:
            errors.append(f"source file {source_id} appears more than once")
        else:
            source_file_ids.add(source_id)
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            errors.append(f"source file {source_id or index} has invalid sha256")

    try:
        periods = vat_periods_from_payload(payload)
    except WooTaxError as error:
        errors.append(str(error))
        periods = []

    source_by_id: dict[str, dict[str, Any]] = {}
    source_countries: dict[str, str] = {}
    for index, row in enumerate(source_rows):
        if not isinstance(row, dict) or not str(row.get("source_row_id") or ""):
            errors.append(f"source row {index} has no source_row_id")
            continue
        row_id = str(row["source_row_id"])
        if row_id in source_by_id:
            errors.append(f"source row {row_id} appears more than once")
        source_by_id[row_id] = row
        tax_code = str(row.get("tax_code") or "").strip()
        country_match = re.fullmatch(r"([A-Z]{2})-[A-Z]{2}-VAT-[A-Za-z0-9-]+", tax_code)
        if not country_match:
            errors.append(f"source row {row_id} has invalid tax_code")
        else:
            source_countries[row_id] = country_match.group(1)
        try:
            configured_rate = decimal_value(row.get("configured_rate"))
            if configured_rate < 0:
                raise WooTaxError("negative rate")
        except WooTaxError:
            errors.append(f"source row {row_id} has invalid configured_rate")

    grouped: dict[str, list[dict[str, Any]]] = {row_id: [] for row_id in source_by_id}
    seen_orders: set[str] = set()
    seen_processor_refs: set[str] = set()
    artifact_year = payload.get("year")
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

        processor_ref = str(allocation.get("processor_ref") or "").strip()
        if not processor_ref:
            errors.append(f"allocation {label} has no processor_ref")
        elif processor_ref in seen_processor_refs:
            errors.append(f"processor_ref is allocated more than once: {processor_ref}")
        else:
            seen_processor_refs.add(processor_ref)

        row_id = str(allocation.get("source_row_id") or "")
        if row_id not in source_by_id:
            errors.append(f"allocation {label} references unknown source row {row_id or '<empty>'}")
        else:
            grouped[row_id].append(allocation)
            expected_country = source_countries.get(row_id)
            if expected_country and str(allocation.get("country_code") or "") != expected_country:
                errors.append(f"allocation {label} country does not match source tax row")
            try:
                if decimal_value(allocation.get("configured_rate")) != decimal_value(
                    source_by_id[row_id].get("configured_rate")
                ):
                    errors.append(f"allocation {label} configured_rate does not match source tax row")
            except WooTaxError:
                errors.append(f"allocation {label} has invalid configured_rate")

        source_refs = allocation.get("source_refs")
        if not isinstance(source_refs, list) or not source_refs:
            errors.append(f"allocation {label} requires source_refs")
        else:
            for source_ref in source_refs:
                if not isinstance(source_ref, dict):
                    errors.append(f"allocation {label} source_refs must contain objects")
                    continue
                source_id = str(source_ref.get("source_id") or "").strip()
                if not source_id or source_id not in source_file_ids:
                    errors.append(f"allocation {label} source_ref is not listed in source_files")

        product_gross = _decimal_field(allocation, "fixed_product_gross", errors, label)
        shipping_gross = _decimal_field(allocation, "fixed_shipping_gross", errors, label)
        product_vat = _decimal_field(allocation, "corrected_product_vat", errors, label)
        shipping_vat = _decimal_field(allocation, "corrected_shipping_vat", errors, label)
        _decimal_field(allocation, "original_order_tax", errors, label)
        _decimal_field(allocation, "original_shipping_tax", errors, label)

        try:
            event_date = date_value(allocation.get("event_date"))
            if not isinstance(artifact_year, int) or event_date.year != artifact_year:
                errors.append(f"allocation {label} event_date is outside allocation year")
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


def validate_allocation_against_evidence(
    payload: dict[str, Any], tax_evidence: list[dict[str, Any]]
) -> list[str]:
    """Compare an allocation with independently parsed canonical Woo tax CSV evidence."""
    errors: list[str] = []
    if not tax_evidence:
        return ["canonical Woo tax evidence is empty"]

    allocation_files = {
        str(item.get("source_id") or ""): item
        for item in payload.get("source_files") or []
        if isinstance(item, dict) and str(item.get("source_id") or "")
    }
    allocation_rows = {
        str(item.get("source_row_id") or ""): item
        for item in payload.get("source_rows") or []
        if isinstance(item, dict) and str(item.get("source_row_id") or "")
    }
    actual_rows: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    actual_source_ids: set[str] = set()
    artifact_year = payload.get("year")

    for evidence in tax_evidence:
        if not isinstance(evidence, dict):
            errors.append("canonical Woo tax evidence entries must be objects")
            continue
        source_id = str(evidence.get("source_id") or "").strip()
        source_path = str(evidence.get("path") or "").strip()
        source_hash = str(evidence.get("sha256") or "").strip()
        if not source_id or source_id in actual_source_ids:
            errors.append(f"canonical Woo tax source ID is missing or duplicated: {source_id or '<empty>'}")
            continue
        actual_source_ids.add(source_id)
        if evidence.get("year") != artifact_year:
            errors.append(f"canonical Woo tax source {source_id} year does not match allocation year")
        allocation_file = allocation_files.get(source_id)
        if allocation_file is None:
            errors.append(f"allocation is bound to the wrong source; missing canonical source {source_id}")
        elif str(allocation_file.get("sha256") or "") != source_hash:
            errors.append(f"canonical Woo tax source {source_id} hash does not match allocation")
        elif allocation_file.get("path") not in (None, "", source_path):
            errors.append(f"canonical Woo tax source {source_id} path does not match allocation")

        rows = evidence.get("rows")
        if not isinstance(rows, list) or not rows:
            errors.append(f"canonical Woo tax source {source_id} has no rows")
            continue
        for row in rows:
            if not isinstance(row, dict):
                errors.append(f"canonical Woo tax source {source_id} has a non-object row")
                continue
            row_id = str(row.get("source_row_id") or "").strip()
            if not row_id or row_id in actual_rows:
                errors.append(f"canonical Woo tax row ID is missing or duplicated: {row_id or '<empty>'}")
                continue
            actual_rows[row_id] = (evidence, row)

    if set(allocation_rows) != set(actual_rows):
        missing = sorted(set(actual_rows) - set(allocation_rows))
        stale = sorted(set(allocation_rows) - set(actual_rows))
        if missing:
            errors.append("allocation is missing canonical Woo tax row(s): " + ", ".join(missing))
        if stale:
            errors.append("allocation contains stale Woo tax row(s): " + ", ".join(stale))

    decimal_fields = ("configured_rate", "order_tax", "shipping_tax", "total_tax")
    for row_id in sorted(set(allocation_rows) & set(actual_rows)):
        supplied = allocation_rows[row_id]
        _, actual = actual_rows[row_id]
        for field in decimal_fields:
            try:
                matches = decimal_value(supplied.get(field)) == decimal_value(actual.get(field))
            except WooTaxError:
                matches = False
            if not matches:
                errors.append(f"canonical Woo tax row {row_id} {field} does not match allocation")
        if str(supplied.get("tax_code") or "") != str(actual.get("tax_code") or ""):
            errors.append(f"canonical Woo tax row {row_id} tax_code does not match allocation")
        try:
            matches_orders = int(supplied.get("orders")) == int(actual.get("orders"))
        except (TypeError, ValueError):
            matches_orders = False
        if not matches_orders:
            errors.append(f"canonical Woo tax row {row_id} orders does not match allocation")

    for allocation in payload.get("allocations") or []:
        if not isinstance(allocation, dict):
            continue
        label = str(allocation.get("order_id") or "<unknown>")
        row_id = str(allocation.get("source_row_id") or "")
        actual_pair = actual_rows.get(row_id)
        if actual_pair is None:
            continue
        evidence, actual_row = actual_pair
        if str(allocation.get("country_code") or "") != str(actual_row.get("country_code") or ""):
            errors.append(f"allocation {label} country does not match canonical Woo tax evidence")
        try:
            configured_matches = decimal_value(allocation.get("configured_rate")) == decimal_value(
                actual_row.get("configured_rate")
            )
        except WooTaxError:
            configured_matches = False
        if not configured_matches:
            errors.append(f"allocation {label} configured_rate does not match canonical Woo tax evidence")
        expected_ref = (
            str(evidence.get("source_id") or ""),
            str(evidence.get("path") or ""),
            str(actual_row.get("row_ref") or ""),
        )
        actual_refs = {
            (
                str(item.get("source_id") or ""),
                str(item.get("path") or ""),
                str(item.get("row_ref") or ""),
            )
            for item in allocation.get("source_refs") or []
            if isinstance(item, dict)
        }
        if expected_ref not in actual_refs:
            errors.append(f"allocation {label} lacks an exact canonical Woo tax source reference")

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


def load_allocation(
    path: Path,
    *,
    company_slug: str,
    year: int,
    tax_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Load a reviewed annual allocation only when it is safe for the company and year."""
    payload = load_json(path)
    validation = payload.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "pass":
        raise WooTaxError("Woo tax allocation validation status must be pass.")

    errors = validate_allocation(payload)
    if payload.get("company_slug") != company_slug:
        errors.append(f"allocation company_slug does not match {company_slug}")
    if payload.get("year") != year:
        errors.append(f"allocation year does not match {year}")
    if tax_evidence is not None:
        errors.extend(validate_allocation_against_evidence(payload, tax_evidence))
    if errors:
        raise WooTaxError(f"Woo tax allocation validation failed: {'; '.join(sorted(set(errors)))}")
    payload["_allocation_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if tax_evidence is not None:
        payload["_tax_evidence"] = copy.deepcopy(tax_evidence)
    return payload


def record_link_values(record: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return source-scoped Woo order IDs and processor identifiers for a sale."""
    attributes = record.get("attributes")
    order_refs: set[str] = set()
    processor_refs: set[str] = set()
    source_system = str(record.get("source_system") or "").lower()
    channel = str(record.get("channel") or "").lower()
    is_woo = source_system == "woo" or channel == "woo"
    is_processor = is_processor_sale(record)
    if isinstance(attributes, dict) and is_woo:
        for key in ("order_id", "order_key"):
            value = str(attributes.get(key) or "").strip()
            if value:
                order_refs.add(value)
    if isinstance(attributes, dict) and is_processor:
        for key in ("stripe_source_id", "stripe_balance_transaction_id", "transaction_id"):
            value = str(attributes.get(key) or "").strip()
            if value:
                processor_refs.add(value)
    external_ref = str(record.get("external_ref") or "").strip()
    if external_ref and is_woo:
        order_refs.add(external_ref)
    if external_ref and is_processor:
        processor_refs.add(external_ref)
    return order_refs, processor_refs


def linked_allocation_order_ids(
    sale: dict[str, Any], allocations: list[dict[str, Any]]
) -> set[str]:
    order_refs, processor_refs = record_link_values(sale)
    return {
        str(item.get("order_id") or "")
        for item in allocations
        if str(item.get("order_id") or "") in order_refs
        or str(item.get("processor_ref") or "") in processor_refs
    } - {""}


def is_processor_sale(sale: dict[str, Any]) -> bool:
    source_system = str(sale.get("source_system") or "").lower()
    channel = str(sale.get("channel") or "").lower()
    event_type = str(sale.get("event_type") or "").lower()
    if source_system:
        return source_system in {"stripe", "paypal"}
    if channel:
        return channel in {"stripe", "paypal"}
    return event_type.startswith(("stripe_", "paypal_"))


def match_allocations_to_sales(
    sales: list[dict[str, Any]], allocations: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Match reviewed allocations once, without trusting processor order metadata."""
    matched: dict[str, dict[str, Any]] = {}
    matched_sale_ids: dict[int, str] = {}
    for allocation in allocations:
        order_id = str(allocation.get("order_id") or "").strip()
        processor_ref = str(allocation.get("processor_ref") or "").strip()
        candidates: list[dict[str, Any]] = []
        for sale in sales:
            order_refs, processor_refs = record_link_values(sale)
            if order_id in order_refs or processor_ref in processor_refs:
                candidates.append(sale)
        if len(candidates) > 1:
            raise WooTaxError(f"Woo tax allocation for order {order_id} matched more than one sale.")
        if not candidates:
            continue
        sale = candidates[0]
        sale_identity = id(sale)
        if sale_identity in matched_sale_ids:
            raise WooTaxError(
                "Woo tax allocations for orders "
                f"{matched_sale_ids[sale_identity]} and {order_id} matched the same sale."
            )
        matched[order_id] = sale
        matched_sale_ids[sale_identity] = order_id
    return matched


def is_woo_source_sale(sale: dict[str, Any]) -> bool:
    return str(sale.get("source_system") or "").lower() == "woo" or str(
        sale.get("channel") or ""
    ).lower() == "woo"


def allocation_components(items: list[dict[str, Any]]) -> dict[str, Decimal]:
    product_gross = sum(
        (decimal_value(item.get("fixed_product_gross")) for item in items), Decimal("0")
    )
    shipping_gross = sum(
        (decimal_value(item.get("fixed_shipping_gross")) for item in items), Decimal("0")
    )
    product_vat = sum(
        (decimal_value(item.get("corrected_product_vat")) for item in items), Decimal("0")
    )
    shipping_vat = sum(
        (decimal_value(item.get("corrected_shipping_vat")) for item in items), Decimal("0")
    )
    return {
        "product_gross": product_gross,
        "shipping_gross": shipping_gross,
        "product_vat": product_vat,
        "shipping_vat": shipping_vat,
        "product_net": product_gross - product_vat,
        "shipping_net": shipping_gross - shipping_vat,
    }


def is_monthly_woo_summary(sale: dict[str, Any], period: str) -> bool:
    attributes = sale.get("attributes")
    return (
        sale.get("source_system") == "woo"
        and str(sale.get("event_date") or "").startswith(period)
        and (
            sale.get("event_type") == "woo_monthly_sales"
            or isinstance(attributes, dict) and attributes.get("is_monthly_summary") is True
        )
    )


def apply_allocation_to_sale(
    sale: dict[str, Any], items: list[dict[str, Any]], allocation: dict[str, Any], label: str
) -> None:
    components = allocation_components(items)
    expected_gross = components["product_gross"] + components["shipping_gross"]
    sale_gross = decimal_value(sale.get("gross_amount"))
    if not same_money(expected_gross, sale_gross):
        raise WooTaxError(
            f"Woo tax allocation gross for {label} does not match processor gross "
            f"({money(expected_gross)} != {money(sale_gross)})."
        )

    set_allocated_sale_components(sale, items, allocation, components)


def set_allocated_sale_components(
    sale: dict[str, Any],
    items: list[dict[str, Any]],
    allocation: dict[str, Any],
    components: dict[str, Decimal],
) -> None:
    allocated_gross = components["product_gross"] + components["shipping_gross"]
    periods = vat_periods_from_payload(allocation)
    allocated_source_row_ids = {str(item.get("source_row_id") or "") for item in items}
    tax_source_refs: list[dict[str, Any]] = []
    for source in allocation.get("_tax_evidence") or []:
        if not isinstance(source, dict):
            continue
        row_refs = sorted(
            str(row.get("row_ref") or "")
            for row in source.get("rows") or []
            if isinstance(row, dict)
            and str(row.get("source_row_id") or "") in allocated_source_row_ids
            and str(row.get("row_ref") or "")
        )
        if row_refs:
            tax_source_refs.append(
                {
                    "source_id": str(source.get("source_id") or ""),
                    "path": str(source.get("path") or ""),
                    "sha256": str(source.get("sha256") or ""),
                    "row_refs": row_refs,
                }
            )

    allocation_ref = {
        "path": allocation.get("_allocation_path"),
        "sha256": allocation.get("_allocation_sha256"),
    }

    attributes = sale.setdefault("attributes", {})
    if not isinstance(attributes, dict):
        raise WooTaxError("Woo sale has invalid attributes.")
    sale["gross_amount"] = decimal_number(allocated_gross)
    sale["vat_amount"] = decimal_number(components["product_vat"] + components["shipping_vat"])
    sale["net_amount"] = decimal_number(components["product_net"] + components["shipping_net"])
    sale["shipping_amount"] = decimal_number(components["shipping_gross"])
    attributes["vat_allocation"] = {
        "fixed_product_gross": decimal_number(components["product_gross"]),
        "fixed_shipping_gross": decimal_number(components["shipping_gross"]),
        "product_net": decimal_number(components["product_net"]),
        "shipping_net": decimal_number(components["shipping_net"]),
        "product_vat": decimal_number(components["product_vat"]),
        "shipping_vat": decimal_number(components["shipping_vat"]),
        "allocation_path": allocation.get("_allocation_path"),
        "allocation_ref": allocation_ref,
        "tax_source_refs": tax_source_refs,
        "allocated_order_ids": sorted(str(item["order_id"]) for item in items),
        "component_vat_evidence": [
            {
                "order_id": str(item["order_id"]),
                "event_date": str(item.get("event_date") or ""),
                "source_row_id": str(item.get("source_row_id") or ""),
                "processor_ref": str(item.get("processor_ref") or ""),
                "country_code": str(item.get("country_code") or ""),
                "configured_rate": decimal_number(decimal_value(item.get("configured_rate"))),
                "corrected_rate": decimal_number(decimal_value(item.get("corrected_rate"))),
                "fixed_product_gross": decimal_number(decimal_value(item.get("fixed_product_gross"))),
                "fixed_shipping_gross": decimal_number(decimal_value(item.get("fixed_shipping_gross"))),
                "product_vat": decimal_number(decimal_value(item.get("corrected_product_vat"))),
                "shipping_vat": decimal_number(decimal_value(item.get("corrected_shipping_vat"))),
                "source_refs": copy.deepcopy(item.get("source_refs") or []),
                "vat_profile": {
                    "start": profile.start.isoformat(),
                    "end": profile.end.isoformat() if profile.end is not None else None,
                    "rate": decimal_number(profile.rate),
                    "goods_vat_type_id": profile.goods_vat_type_id,
                    "shipping_vat_type_id": profile.shipping_vat_type_id,
                },
            }
            for item in sorted(items, key=lambda item: str(item["order_id"]))
            for profile in [select_vat_period(date_value(item.get("event_date")), periods)]
        ],
    }


def apply_allocation_to_monthly_summary(
    sale: dict[str, Any],
    items: list[dict[str, Any]],
    allocation: dict[str, Any],
    label: str,
) -> dict[str, Any] | None:
    """Apply taxable components and return an explicit zero-rated residual, if any."""
    components = allocation_components(items)
    allocated_gross = components["product_gross"] + components["shipping_gross"]
    summary_gross = decimal_value(sale.get("gross_amount"))
    if money(allocated_gross) > money(summary_gross):
        raise WooTaxError(
            f"Woo tax allocation gross for {label} exceeds monthly summary gross "
            f"({money(allocated_gross)} > {money(summary_gross)})."
        )
    if same_money(allocated_gross, summary_gross):
        set_allocated_sale_components(sale, items, allocation, components)
        return None

    source_product_net = decimal_value(sale.get("net_amount"))
    source_shipping_net = decimal_value(sale.get("shipping_amount"))
    source_vat = decimal_value(sale.get("vat_amount"))
    original_product_vat = sum(
        (decimal_value(item.get("original_order_tax")) for item in items), Decimal("0")
    )
    original_shipping_vat = sum(
        (decimal_value(item.get("original_shipping_tax")) for item in items), Decimal("0")
    )
    original_vat = original_product_vat + original_shipping_vat
    if not same_money(source_vat, original_vat) or not same_money(
        source_product_net + source_shipping_net + source_vat, summary_gross
    ):
        raise WooTaxError(
            f"Woo monthly summary component evidence does not reconcile for {label}."
        )

    source_product_gross = source_product_net + original_product_vat
    source_shipping_gross = source_shipping_net + original_shipping_vat
    residual_product_gross = source_product_gross - components["product_gross"]
    residual_shipping_gross = source_shipping_gross - components["shipping_gross"]
    if residual_product_gross < 0 or residual_shipping_gross < 0:
        raise WooTaxError(
            f"Woo monthly summary component evidence does not reconcile for {label}."
        )
    residual_gross = residual_product_gross + residual_shipping_gross
    if not same_money(allocated_gross + residual_gross, summary_gross):
        raise WooTaxError(
            f"Woo monthly summary component evidence does not reconcile for {label}."
        )

    residual = copy.deepcopy(sale)
    original_attributes = sale.get("attributes")
    total_orders = (
        int(original_attributes.get("orders"))
        if isinstance(original_attributes, dict)
        and isinstance(original_attributes.get("orders"), int)
        else None
    )
    if total_orders is not None and total_orders < len(items):
        raise WooTaxError(
            f"Woo monthly summary component evidence does not reconcile for {label}."
        )

    set_allocated_sale_components(sale, items, allocation, components)
    sale_attributes = sale.get("attributes")
    if isinstance(sale_attributes, dict):
        sale_attributes["orders"] = len(items)

    residual["record_id"] = f"{residual.get('record_id')}:zero-rated-residual"
    residual["description"] = f"{residual.get('description') or 'Woo monthly summary'} zero-rated residual"
    residual["external_ref"] = f"{residual.get('external_ref') or label}:zero-rated-residual"
    residual["gross_amount"] = decimal_number(residual_gross)
    residual["net_amount"] = decimal_number(residual_gross)
    residual["vat_amount"] = 0.0
    residual["shipping_amount"] = decimal_number(residual_shipping_gross)
    residual["quantity"] = None
    residual_attributes = residual.setdefault("attributes", {})
    if not isinstance(residual_attributes, dict):
        raise WooTaxError("Woo monthly summary residual has invalid attributes.")
    residual_attributes.pop("vat_allocation", None)
    if total_orders is not None:
        residual_attributes["orders"] = total_orders - len(items)
    residual_attributes["zero_rated_residual"] = {
        "fixed_product_gross": decimal_number(residual_product_gross),
        "fixed_shipping_gross": decimal_number(residual_shipping_gross),
        "allocated_order_ids": sorted(str(item["order_id"]) for item in items),
    }
    return residual


def zero_unsupported_sale(sale: dict[str, Any]) -> None:
    """Preserve customer gross while ensuring unsupported sales are zero-rated."""
    gross = decimal_value(sale.get("gross_amount"))
    sale["vat_amount"] = 0.0
    sale["net_amount"] = decimal_number(gross)
    attributes = sale.get("attributes")
    if isinstance(attributes, dict):
        attributes.pop("vat_allocation", None)


def apply_period_allocation(
    records: dict[str, list[dict[str, Any]]], allocation: dict[str, Any], period: str
) -> None:
    """Apply reviewed fixed-gross VAT allocations to matching processor sales in one period."""
    sales = [sale for sale in records.get("sales") or [] if isinstance(sale, dict)]
    annual_allocations = [
        item for item in allocation.get("allocations") or [] if isinstance(item, dict)
    ]
    period_allocations = [
        item for item in annual_allocations if item.get("period") == period
    ]
    if not period_allocations:
        for sale in sales:
            if is_woo_source_sale(sale):
                zero_unsupported_sale(sale)
        return

    summary_sales = [
        sale
        for sale in sales
        if is_monthly_woo_summary(sale, period)
    ]
    if len(summary_sales) > 1:
        raise WooTaxError(f"Woo tax allocation found multiple monthly summary sales for {period}.")
    if summary_sales:
        residual = apply_allocation_to_monthly_summary(
            summary_sales[0], period_allocations, allocation, f"monthly summary {period}"
        )
        if residual is not None:
            records.setdefault("sales", []).append(residual)
        duplicates = match_allocations_to_sales(
            [sale for sale in sales if sale is not summary_sales[0]], period_allocations
        )
        for sale in duplicates.values():
            zero_unsupported_sale(sale)
        return

    allocations_by_order = {
        str(item.get("order_id") or ""): [item]
        for item in period_allocations
        if str(item.get("order_id") or "")
    }
    matched_sales = match_allocations_to_sales(sales, period_allocations)
    for sale in sales:
        if is_woo_source_sale(sale) and sale not in matched_sales.values():
            zero_unsupported_sale(sale)
    for order_id, sale in matched_sales.items():
        apply_allocation_to_sale(sale, allocations_by_order[order_id], allocation, f"order {order_id}")

    missing_orders = sorted(set(allocations_by_order) - set(matched_sales))
    if missing_orders:
        raise WooTaxError("Woo tax allocation has no matching sale for order(s): " + ", ".join(missing_orders))


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
        import bookprep

        tax_evidence = bookprep.discover_canonical_woo_tax_evidence(
            source_dir=args.company_dir / "source",
            root_dir=Path.cwd(),
            year=args.year,
        )
        if not tax_evidence:
            raise WooTaxError("No canonical Woo tax-summary evidence was found for allocation validation.")
        path = args.company_dir / "artifacts" / "vat" / f"{args.year}-woo-tax-allocation.json"
        artifact = load_allocation(
            path,
            company_slug=company_slug,
            year=args.year,
            tax_evidence=tax_evidence,
        )
        errors: list[str] = []
        totals = annual_totals(artifact)
        print(json.dumps({"gross": decimal_number(totals["gross"]), "original_vat": decimal_number(totals["original_vat"]), "corrected_vat": decimal_number(totals["corrected_vat"]), "errors": sorted(set(errors))}, sort_keys=True))
        return 1 if errors else 0
    except WooTaxError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
