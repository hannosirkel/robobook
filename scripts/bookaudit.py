#!/usr/bin/env python3
from __future__ import annotations  # noqa: EXE001, I001

import argparse
import hashlib
import json
import re
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bookbuilder import planned_sales_groups
from simplbooks_api import (
    SimplbooksClient,
    SimplbooksError,
    load_token,
    resolve_company_id,
    resolve_company_name,
)


PROCESSOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "paypal": ("paypal",),
    "stripe": ("stripe",),
}

FULFILLMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "printful": ("printful",),
    "quartermaster": ("quartermaster",),
    "shipmonk": ("shipmonk",),
    "omnipack": ("omnipack",),
}

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

SECTIONS = (
    "source_vs_simplbooks_totals",
    "bank_and_processor_completeness",
    "vat_review",
    "inventory_review",
    "continuity_review",
    "date_semantics_review",
    "spot_checks",
)

SECTION_TITLES = {
    "source_vs_simplbooks_totals": "Source Vs Simplbooks Totals",
    "bank_and_processor_completeness": "Bank And Processor Completeness",
    "vat_review": "VAT Review",
    "inventory_review": "Inventory Review",
    "continuity_review": "Continuity Review",
    "date_semantics_review": "Date Semantics Review",
    "spot_checks": "Spot Checks",
}

SEVERITY_ORDER = {
    "error": 0,
    "warn": 1,
    "info": 2,
}

TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class AuditScope:
    label: str
    kind: str
    start: date
    end: date


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def display_path(path: Path, root_dir: Path) -> str:
    try:
        return str(path.relative_to(root_dir))
    except ValueError:
        return str(path)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def decimal_value(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")  # noqa: FURB157
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise SimplbooksError(f"Could not parse decimal value: {value!r}") from exc


def decimal_number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def decimal_text(value: Decimal | int | float | str) -> str:  # noqa: PYI041
    normalized = decimal_value(value).normalize()
    if normalized == normalized.to_integral():
        return format(normalized.quantize(Decimal("1")), "f")  # noqa: FURB157
    return format(normalized, "f")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SimplbooksError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SimplbooksError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SimplbooksError(f"JSON top level must be an object: {path}")
    return loaded


def load_optional_text(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def parse_scope(value: str) -> AuditScope:
    month_match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if month_match:
        year = int(month_match.group(1))
        month = int(month_match.group(2))
        if not 1 <= month <= 12:
            raise SimplbooksError(f"Invalid month in period: {value}")
        start = date(year, month, 1)
        end = date(year, month, monthrange(year, month)[1])
        return AuditScope(label=value, kind="month", start=start, end=end)

    year_match = re.fullmatch(r"\d{4}", value)
    if year_match:
        year = int(value)
        return AuditScope(label=value, kind="year", start=date(year, 1, 1), end=date(year, 12, 31))

    raise SimplbooksError(f"Audit scope must use YYYY-MM or YYYY format, got: {value}")


def previous_scope_label(scope: AuditScope) -> str:
    if scope.kind == "month":
        if scope.start.month == 1:
            return f"{scope.start.year - 1}-12"
        return f"{scope.start.year}-{scope.start.month - 1:02d}"
    return str(scope.start.year - 1)


def inferred_artifacts_dir(path: Path) -> Path | None:
    if path.parent.name == "normalized":
        return path.parent.parent
    return None


def resolve_normalized_paths(*, company_dir: Path | None, scope: AuditScope, override: str | None) -> list[Path]:
    if override:
        candidate = Path(override)
        if scope.kind == "month":
            return [candidate]
        if candidate.is_dir():
            return sorted(candidate.glob(f"{scope.label}-*.json"))
        raise SimplbooksError("Year audits require --normalized to point to a directory, or use --company-dir.")

    if company_dir is None:
        raise SimplbooksError("Pass --company-dir or --normalized.")

    base_dir = company_dir / "artifacts" / "normalized"
    if scope.kind == "month":
        return [base_dir / f"{scope.label}.json"]
    return sorted(base_dir.glob(f"{scope.label}-*.json"))


def resolve_previous_normalized_paths(*, company_dir: Path | None, scope: AuditScope) -> list[Path]:
    if company_dir is None:
        return []
    previous_label = previous_scope_label(scope)
    previous_scope = parse_scope(previous_label)
    paths = resolve_normalized_paths(company_dir=company_dir, scope=previous_scope, override=None)
    return [path for path in paths if path.exists()]


def resolve_policy_path(*, company_dir: Path | None, normalized_paths: list[Path], override: str | None) -> Path | None:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "policy_memo.md"
    artifacts_dir = inferred_artifacts_dir(normalized_paths[0]) if normalized_paths else None
    if artifacts_dir is None:
        return None
    return artifacts_dir / "policy_memo.md"


def resolve_output_path(*, company_dir: Path | None, normalized_paths: list[Path], scope: AuditScope, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "audits" / f"{scope.label}.md"
    artifacts_dir = inferred_artifacts_dir(normalized_paths[0]) if normalized_paths else None
    if artifacts_dir is not None:
        return artifacts_dir / "audits" / f"{scope.label}.md"
    if len(normalized_paths) == 1:
        return normalized_paths[0].with_suffix(".audit.md")
    return Path.cwd() / f"{scope.label}.audit.md"


def load_normalized_payloads(paths: list[Path], *, scope: AuditScope) -> list[dict[str, Any]]:
    if not paths:
        raise SimplbooksError(f"No normalized artifacts were found for {scope.label}.")

    payloads = []
    company_slug: str | None = None
    for path in paths:
        payload = load_json(path)
        period = str(payload.get("period") or "")
        if scope.kind == "month" and period != scope.label:
            raise SimplbooksError(f"Normalized artifact period mismatch in {path}: expected {scope.label}, got {period!r}")
        if scope.kind == "year" and not period.startswith(f"{scope.label}-"):
            raise SimplbooksError(f"Normalized artifact {path} does not belong to audit year {scope.label}.")
        payload["_artifact_path"] = str(path)
        slug = str(payload.get("company_slug") or "")
        if company_slug is None:
            company_slug = slug
        elif slug != company_slug:
            raise SimplbooksError(f"Normalized artifacts do not share one company_slug: {company_slug!r} vs {slug!r}")
        payloads.append(payload)
    return sorted(payloads, key=lambda item: str(item.get("period") or ""))


def unwrap_single_key(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if len(item) != 1:
        return "raw", item
    key = next(iter(item.keys()))
    value = item[key]
    if isinstance(value, dict):
        return key, value
    return key, {"value": value}


def parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return None
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        return None


def date_in_scope(value: Any, scope: AuditScope) -> bool:
    parsed = parse_iso_date(value)
    return parsed is not None and scope.start <= parsed <= scope.end


def created_time_outside_scope(document: dict[str, Any], scope: AuditScope) -> bool:
    created_time = parse_iso_date(document.get("created_time") or document.get("createdTime"))
    return created_time is not None and not (scope.start <= created_time <= scope.end)


def request_data_or_raise(client: SimplbooksClient, path: str) -> dict[str, Any]:
    response = client.request(path)
    if response.get("_http_status") != 200 or response.get("status") not in (None, 200):
        raise SimplbooksError(f"Request failed for {path}: {json.dumps(response, ensure_ascii=True)}")
    data = response.get("data") or {}
    if not isinstance(data, dict):
        raise SimplbooksError(f"Unexpected response payload for {path}: {json.dumps(response, ensure_ascii=True)}")
    return data


def scan_accounting_documents(
    client: SimplbooksClient,
    path: str,
    *,
    wrapper_keys: tuple[str, ...],
    scope: AuditScope,
) -> tuple[list[dict[str, Any]], dict[str, int], int, dict[str, int]]:
    documents: list[dict[str, Any]] = []
    missing_business_dates = {"created": 0, "transaction_date": 0}
    scope_date_mismatches = {"created_only": 0, "transaction_only": 0}
    created_time_outside = 0

    for item in client.paginate(path):
        key, value = unwrap_single_key(item)
        if key not in wrapper_keys:
            continue
        created_in_scope = date_in_scope(value.get("created"), scope)
        transaction_in_scope = date_in_scope(value.get("transaction_date"), scope)
        if not created_in_scope and not transaction_in_scope:
            continue
        if parse_iso_date(value.get("created")) is None:
            missing_business_dates["created"] += 1
        if parse_iso_date(value.get("transaction_date")) is None:
            missing_business_dates["transaction_date"] += 1
        if created_in_scope and not transaction_in_scope:
            scope_date_mismatches["created_only"] += 1
        if transaction_in_scope and not created_in_scope:
            scope_date_mismatches["transaction_only"] += 1
        documents.append(value)
        if created_time_outside_scope(value, scope):
            created_time_outside += 1

    return documents, missing_business_dates, created_time_outside, scope_date_mismatches


def scan_scoped_documents(
    client: SimplbooksClient,
    path: str,
    *,
    wrapper_keys: tuple[str, ...],
    date_field: str,
    scope: AuditScope,
) -> tuple[list[dict[str, Any]], int, int]:
    documents: list[dict[str, Any]] = []
    missing_business_date = 0
    created_time_outside = 0

    for item in client.paginate(path):
        key, value = unwrap_single_key(item)
        if key not in wrapper_keys:
            continue
        business_date = value.get(date_field)
        if parse_iso_date(business_date) is None:
            missing_business_date += 1
            continue
        if not date_in_scope(business_date, scope):
            continue
        documents.append(value)
        if created_time_outside_scope(value, scope):
            created_time_outside += 1

    return documents, missing_business_date, created_time_outside


def collect_live_state(client: SimplbooksClient, *, scope: AuditScope) -> dict[str, Any]:
    invoices, invoices_missing_business_dates, invoices_created_time_outside, invoices_scope_mismatches = scan_accounting_documents(
        client,
        "invoices/list",
        wrapper_keys=("Invoice", "invoices"),
        scope=scope,
    )
    purchases, purchases_missing_business_dates, purchases_created_time_outside, purchases_scope_mismatches = scan_accounting_documents(
        client,
        "purchases/list",
        wrapper_keys=("Purchase",),
        scope=scope,
    )
    incomings, incomings_missing_business_date, incomings_created_time_outside = scan_scoped_documents(
        client,
        "incomings/list",
        wrapper_keys=("Incoming",),
        date_field="income_date",
        scope=scope,
    )
    payments, payments_missing_business_date, payments_created_time_outside = scan_scoped_documents(
        client,
        "payments/list",
        wrapper_keys=("Payment",),
        date_field="payment_date",
        scope=scope,
    )

    invoice_rows: list[dict[str, Any]] = []
    purchase_rows: list[dict[str, Any]] = []

    for invoice in invoices:
        if invoice.get("id") in (None, ""):
            continue
        detail = request_data_or_raise(client, f"invoices/get/{invoice['id']}")
        rows = detail.get("Task") or []
        if isinstance(rows, list):
            invoice_rows.extend(rows)

    for purchase in purchases:
        if purchase.get("id") in (None, ""):
            continue
        detail = request_data_or_raise(client, f"purchases/get/{purchase['id']}")
        rows = detail.get("PurchaseRow") or []
        if isinstance(rows, list):
            purchase_rows.extend(rows)

    return {
        "invoices": invoices,
        "purchases": purchases,
        "incomings": incomings,
        "payments": payments,
        "invoice_rows": invoice_rows,
        "purchase_rows": purchase_rows,
        "missing_business_dates": {
            "invoices": invoices_missing_business_dates,
            "purchases": purchases_missing_business_dates,
            "incomings": {"income_date": incomings_missing_business_date},
            "payments": {"payment_date": payments_missing_business_date},
        },
        "created_time_outside_scope": {
            "invoices": invoices_created_time_outside,
            "purchases": purchases_created_time_outside,
            "incomings": incomings_created_time_outside,
            "payments": payments_created_time_outside,
        },
        "scope_date_mismatches": {
            "invoices": invoices_scope_mismatches,
            "purchases": purchases_scope_mismatches,
            "incomings": {"income_date_only": 0},
            "payments": {"payment_date_only": 0},
        },
    }


def sum_amount(records: list[dict[str, Any]], field: str) -> Decimal:
    total = Decimal("0")  # noqa: FURB157
    for record in records:
        total += decimal_value(record.get(field))
    return total


def sum_abs_amount(records: list[dict[str, Any]], field: str) -> Decimal:
    total = Decimal("0")  # noqa: FURB157
    for record in records:
        total += abs(decimal_value(record.get(field)))
    return total


def fee_total_from_record(record: dict[str, Any]) -> Decimal:
    fee_amount = abs(decimal_value(record.get("fee_amount")))
    if fee_amount != 0:
        return fee_amount
    gross_amount = abs(decimal_value(record.get("gross_amount")))
    if gross_amount != 0:
        return gross_amount
    return abs(decimal_value(record.get("net_amount")))


def record_quantity(record: dict[str, Any]) -> Decimal:
    value = record.get("quantity")
    if value not in (None, ""):
        return decimal_value(value)
    attributes = record.get("attributes") or {}
    for key in ("quantity", "qty"):
        candidate = attributes.get(key)
        if candidate not in (None, ""):
            try:
                return decimal_value(candidate)
            except SimplbooksError:
                return Decimal("0")  # noqa: FURB157
    return Decimal("0")  # noqa: FURB157


def record_haystack(record: dict[str, Any]) -> str:
    attributes = record.get("attributes") or {}
    parts = [
        str(record.get("source_system") or ""),
        str(record.get("source_type") or ""),
        str(record.get("event_type") or ""),
        str(record.get("description") or ""),
        str(record.get("channel") or ""),
        str(record.get("external_ref") or ""),
    ]
    parts.extend(str(value) for value in attributes.values())
    return normalize_text(" ".join(piece for piece in parts if piece))


def classify_record(record: dict[str, Any], keyword_map: dict[str, tuple[str, ...]]) -> str | None:
    haystack = record_haystack(record)
    for label, keywords in keyword_map.items():
        if all(keyword in haystack for keyword in keywords):
            return label
    return None


def infer_processor(record: dict[str, Any]) -> str | None:
    return classify_record(record, PROCESSOR_KEYWORDS)


def infer_fulfillment_partner(record: dict[str, Any]) -> str | None:
    return classify_record(record, FULFILLMENT_KEYWORDS)


def record_group_label(record: dict[str, Any], *, default: str) -> str:
    for value in (
        record.get("channel"),
        infer_processor(record),
        infer_fulfillment_partner(record),
        record.get("source_system"),
    ):
        if value:
            return str(value)
    return default


def record_currency(record: dict[str, Any], default_currency: str) -> str:
    currency = str(record.get("currency") or "").strip().upper()
    return currency or default_currency


def combine_records(payloads: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    records = {category: [] for category in RECORD_CATEGORIES}
    for payload in payloads:
        for category in RECORD_CATEGORIES:
            values = (payload.get("records") or {}).get(category) or []
            if isinstance(values, list):
                records[category].extend(value for value in values if isinstance(value, dict))
    return records


def effective_fee_groups(
    records: dict[str, list[dict[str, Any]]],
    *,
    base_currency: str,
) -> list[tuple[tuple[str, str], list[dict[str, Any]], Decimal]]:
    grouped_explicit: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records["fees"]:
        key = (record_group_label(record, default="fees"), record_currency(record, base_currency))
        grouped_explicit.setdefault(key, []).append(record)

    grouped_embedded: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for category in ("sales", "refunds", "payouts"):
        for record in records[category]:
            if fee_total_from_record(record) == 0 or decimal_value(record.get("fee_amount")) == 0:
                continue
            key = (record_group_label(record, default="fees"), record_currency(record, base_currency))
            grouped_embedded.setdefault(key, []).append(record)

    effective: list[tuple[tuple[str, str], list[dict[str, Any]], Decimal]] = []
    for key in sorted(set(grouped_explicit) | set(grouped_embedded)):
        explicit_records = grouped_explicit.get(key, [])
        embedded_records = grouped_embedded.get(key, [])
        if explicit_records:
            effective.append((key, explicit_records, sum(fee_total_from_record(record) for record in explicit_records)))
            continue
        effective.append((key, embedded_records, sum(abs(decimal_value(record.get("fee_amount"))) for record in embedded_records)))
    return effective


def effective_invoice_posting_records(
    records: dict[str, list[dict[str, Any]]],
    *,
    base_currency: str,
) -> tuple[
    dict[tuple[str, str], list[dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], list[str]],
]:
    sales_groups, sales_notes, sales_matched_processors, refund_posting_basis = planned_sales_groups(
        records["sales"],
        base_currency=base_currency,
        amount_tolerance=Decimal("0.50"),
    )
    refund_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records["refunds"]:
        evidence_key = (record_group_label(record, default="refunds"), record_currency(record, base_currency))
        posting_key = refund_posting_basis.get(evidence_key, evidence_key)
        refund_groups[posting_key].append(record)
    effective_sales = [record for group_records in sales_groups.values() for record in group_records]
    effective_refunds = [record for group_records in refund_groups.values() for record in group_records]
    return sales_groups, effective_sales, effective_refunds, sales_notes, sales_matched_processors


def build_source_snapshot(payloads: list[dict[str, Any]], *, policy_text: str | None) -> dict[str, Any]:
    records = combine_records(payloads)
    policy = normalize_text(policy_text)
    base_currency = str(payloads[0].get("base_currency") or "EUR")
    fee_groups = effective_fee_groups(records, base_currency=base_currency)
    sales_groups, effective_sales, effective_refunds, sales_notes, sales_matched_processors = effective_invoice_posting_records(
        records,
        base_currency=base_currency,
    )

    processors = sorted(
        {
            label
            for category in ("sales", "refunds", "fees", "payouts", "bank_transactions")
            for record in records[category]
            for label in [infer_processor(record)]
            if label
        }
    )
    fulfillment_partners = sorted(
        {
            label
            for category in ("purchase_expenses", "bank_transactions")
            for record in records[category]
            for label in [infer_fulfillment_partner(record)]
            if label
        }
    )

    processor_bank_receipts = [
        record
        for record in records["bank_transactions"]
        if decimal_value(record.get("gross_amount")) > 0 and infer_processor(record)
    ]
    fulfillment_bank_payments = [
        record
        for record in records["bank_transactions"]
        if decimal_value(record.get("gross_amount")) < 0 and infer_fulfillment_partner(record)
    ]

    source_warehouse_ids = sorted(
        {
            str(record.get("warehouse_id"))
            for category in RECORD_CATEGORIES
            for record in records[category]
            if record.get("warehouse_id") not in (None, "")
        }
    )
    source_quantity_signals = any(
        record_quantity(record) != 0 or record.get("sku") not in (None, "") or record.get("warehouse_id") not in (None, "")
        for category in ("sales", "refunds", "purchase_expenses", "inventory_movements")
        for record in records[category]
    )
    inventory_expected = (
        bool(records["inventory_movements"])
        or source_quantity_signals
        or "inventory" in policy
        or "warehouse identity matters" in policy
    )

    canonical_sources = sum(1 for payload in payloads for source in payload.get("sources", []) if source.get("canonical"))

    return {
        "currency": base_currency,
        "periods": [str(payload.get("period") or "") for payload in payloads],
        "canonical_source_count": canonical_sources,
        "invoice_total": sum_amount(effective_sales, "gross_amount") + sum_amount(effective_refunds, "gross_amount"),
        "purchase_total": sum_amount(records["purchase_expenses"], "gross_amount")
        + sum(total for _, _, total in fee_groups),
        "incoming_total": sum_amount(records["payouts"], "gross_amount"),
        "payment_total": sum_abs_amount(fulfillment_bank_payments, "gross_amount"),
        "bank_processor_receipt_total": sum_amount(processor_bank_receipts, "gross_amount"),
        "output_vat_total": sum_amount(effective_sales, "vat_amount") + sum_amount(effective_refunds, "vat_amount"),
        "input_vat_total": sum_amount(records["purchase_expenses"], "vat_amount") + sum_amount(records["fees"], "vat_amount"),
        "processors": processors,
        "fulfillment_partners": fulfillment_partners,
        "inventory_expected": inventory_expected,
        "warehouse_ids": source_warehouse_ids,
        "inventory_quantity_total": sum((abs(record_quantity(record)) for record in records["inventory_movements"]), Decimal("0")),  # noqa: FURB157
        "invoice_record_count": len(effective_sales) + len(effective_refunds),
        "purchase_record_count": len(records["purchase_expenses"]) + sum(len(source_records) for _, source_records, _ in fee_groups),
        "payout_record_count": len(records["payouts"]),
        "bank_record_count": len(records["bank_transactions"]),
        "invoice_posting_basis_groups": [
            {"group_label": group_label, "currency": currency, "matched_processors": sales_matched_processors.get((group_label, currency), [])}
            for group_label, currency in sorted(sales_groups.keys())
        ],
        "suppressed_processor_sales_group_count": sum(len(labels) for labels in sales_matched_processors.values()),
        "invoice_posting_basis_notes": [
            note
            for key in sorted(sales_notes)
            for note in sales_notes[key]
        ],
    }


def document_total(document: dict[str, Any], *keys: str) -> Decimal:
    for key in keys:
        if document.get(key) not in (None, ""):
            return decimal_value(document.get(key))
    return Decimal("0")  # noqa: FURB157


def nonempty_ids(rows: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(row.get(key)) for row in rows if row.get(key) not in (None, "")})


def build_live_snapshot(live_state: dict[str, Any]) -> dict[str, Any]:
    invoices = live_state["invoices"]
    purchases = live_state["purchases"]
    incomings = live_state["incomings"]
    payments = live_state["payments"]
    invoice_rows = live_state["invoice_rows"]
    purchase_rows = live_state["purchase_rows"]

    return {
        "invoice_total": sum(document_total(document, "total_sum", "sum") for document in invoices),
        "purchase_total": sum(document_total(document, "total_sum", "sum") for document in purchases),
        "incoming_total": sum(document_total(document, "income_sum", "sum", "total_sum") for document in incomings),
        "payment_total": sum(document_total(document, "payment_sum", "sum", "total_sum") for document in payments),
        "output_vat_total": sum(decimal_value(document.get("vat")) for document in invoices),
        "input_vat_total": sum(decimal_value(document.get("vat")) for document in purchases),
        "invoice_count": len(invoices),
        "purchase_count": len(purchases),
        "incoming_count": len(incomings),
        "payment_count": len(payments),
        "invoice_row_missing_vat_type_count": sum(1 for row in invoice_rows if row.get("vat_type_id") in (None, "")),
        "purchase_row_missing_vat_type_count": sum(1 for row in purchase_rows if row.get("vat_type_id") in (None, "")),
        "article_ids": nonempty_ids(invoice_rows, "article_id") + [item for item in nonempty_ids(purchase_rows, "article_id") if item not in nonempty_ids(invoice_rows, "article_id")],
        "warehouse_ids": nonempty_ids(invoice_rows, "warehouse_id") + [item for item in nonempty_ids(purchase_rows, "warehouse_id") if item not in nonempty_ids(invoice_rows, "warehouse_id")],
        "created_time_outside_scope": dict(live_state["created_time_outside_scope"]),
        "missing_business_dates": dict(live_state["missing_business_dates"]),
        "scope_date_mismatches": dict(live_state["scope_date_mismatches"]),
        "documents": {
            "invoices": invoices,
            "purchases": purchases,
            "incomings": incomings,
            "payments": payments,
        },
    }


def nested_counts(mapping: dict[str, Any]) -> int:
    total = 0
    for value in mapping.values():
        if isinstance(value, dict):
            total += nested_counts(value)
        else:
            total += int(value)
    return total


def make_finding(*, section: str, severity: str, summary: str, evidence: list[str] | None = None) -> dict[str, Any]:
    return {
        "section": section,
        "severity": severity,
        "summary": summary,
        "evidence": evidence or [],
    }


def maybe_total_mismatch(
    *,
    section: str,
    label: str,
    source_total: Decimal,
    live_total: Decimal,
    currency: str,
    severity: str = "error",
) -> list[dict[str, Any]]:
    if source_total == 0 and live_total == 0:
        return []
    delta = source_total - live_total
    if abs(delta) <= TOLERANCE:
        return []
    return [
        make_finding(
            section=section,
            severity=severity,
            summary=f"{label} do not match between source evidence and live Simplbooks state.",
            evidence=[
                f"Source total: {decimal_text(source_total)} {currency}",
                f"Live total: {decimal_text(live_total)} {currency}",
                f"Delta: {decimal_text(delta)} {currency}",
            ],
        )
    ]


def evaluate_source_vs_simplbooks_totals(source: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    currency = source["currency"]
    findings: list[dict[str, Any]] = []
    findings.extend(
        maybe_total_mismatch(
            section="source_vs_simplbooks_totals",
            label="Sales and refund totals vs invoices",
            source_total=source["invoice_total"],
            live_total=live["invoice_total"],
            currency=currency,
        )
    )
    findings.extend(
        maybe_total_mismatch(
            section="source_vs_simplbooks_totals",
            label="Fee and purchase totals vs purchases",
            source_total=source["purchase_total"],
            live_total=live["purchase_total"],
            currency=currency,
        )
    )
    findings.extend(
        maybe_total_mismatch(
            section="source_vs_simplbooks_totals",
            label="Payout totals vs incomings",
            source_total=source["incoming_total"],
            live_total=live["incoming_total"],
            currency=currency,
        )
    )
    findings.extend(
        maybe_total_mismatch(
            section="source_vs_simplbooks_totals",
            label="Fulfillment bank debit totals vs payments",
            source_total=source["payment_total"],
            live_total=live["payment_total"],
            currency=currency,
        )
    )
    return findings


def evaluate_bank_and_processor_completeness(source: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    currency = source["currency"]

    if source["invoice_total"] != 0 and live["invoice_count"] == 0:
        findings.append(
            make_finding(
                section="bank_and_processor_completeness",
                severity="error",
                summary="Source pack contains invoice-worthy activity but no live invoices were found.",
                evidence=[f"Source invoice total: {decimal_text(source['invoice_total'])} {currency}"],
            )
        )
    if source["purchase_total"] != 0 and live["purchase_count"] == 0:
        findings.append(
            make_finding(
                section="bank_and_processor_completeness",
                severity="error",
                summary="Source pack contains purchase-worthy activity but no live purchases were found.",
                evidence=[f"Source purchase total: {decimal_text(source['purchase_total'])} {currency}"],
            )
        )
    if source["incoming_total"] != 0 and live["incoming_count"] == 0:
        findings.append(
            make_finding(
                section="bank_and_processor_completeness",
                severity="error",
                summary="Source pack contains payout activity but no live incomings were found.",
                evidence=[
                    f"Source payout total: {decimal_text(source['incoming_total'])} {currency}",
                    f"Processors seen: {', '.join(source['processors']) or 'none'}",
                ],
            )
        )
    if source["payment_total"] != 0 and live["payment_count"] == 0:
        findings.append(
            make_finding(
                section="bank_and_processor_completeness",
                severity="error",
                summary="Source pack contains fulfillment-related bank debits but no live payments were found.",
                evidence=[
                    f"Source payment total: {decimal_text(source['payment_total'])} {currency}",
                    f"Fulfillment partners seen: {', '.join(source['fulfillment_partners']) or 'none'}",
                ],
            )
        )

    findings.extend(
        maybe_total_mismatch(
            section="bank_and_processor_completeness",
            label="Processor-attributed bank receipts vs incomings",
            source_total=source["bank_processor_receipt_total"],
            live_total=live["incoming_total"],
            currency=currency,
            severity="warn",
        )
    )

    if source["processors"] and not live["incoming_count"] and source["incoming_total"] == 0:
        findings.append(
            make_finding(
                section="bank_and_processor_completeness",
                severity="warn",
                summary="Processor signals exist in source records, but no payout evidence was normalized for the audited scope.",
                evidence=[f"Processors seen: {', '.join(source['processors'])}"],
            )
        )
    return findings


def evaluate_vat_review(source: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    currency = source["currency"]
    findings: list[dict[str, Any]] = []
    findings.extend(
        maybe_total_mismatch(
            section="vat_review",
            label="Output VAT totals",
            source_total=source["output_vat_total"],
            live_total=live["output_vat_total"],
            currency=currency,
        )
    )
    findings.extend(
        maybe_total_mismatch(
            section="vat_review",
            label="Input VAT totals",
            source_total=source["input_vat_total"],
            live_total=live["input_vat_total"],
            currency=currency,
        )
    )

    if source["output_vat_total"] != 0 and live["invoice_row_missing_vat_type_count"] > 0:
        findings.append(
            make_finding(
                section="vat_review",
                severity="warn",
                summary="Live invoice rows are missing VAT type IDs while source sales show non-zero VAT.",
                evidence=[f"Missing invoice row VAT type count: {live['invoice_row_missing_vat_type_count']}"],
            )
        )
    if source["input_vat_total"] != 0 and live["purchase_row_missing_vat_type_count"] > 0:
        findings.append(
            make_finding(
                section="vat_review",
                severity="warn",
                summary="Live purchase rows are missing VAT type IDs while source purchases show non-zero VAT.",
                evidence=[f"Missing purchase row VAT type count: {live['purchase_row_missing_vat_type_count']}"],
            )
        )
    return findings


def evaluate_stock_equation_review(equation: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Fail the audit unless posted stock reconciles, per warehouse and in aggregate.

    Both are checked because they fail independently: two offsetting warehouse errors
    leave the aggregate at zero, and an aggregate difference can hide inside warehouses
    that each look plausible on their own.
    """
    if not equation:
        return []
    findings = [
        make_finding(section="inventory_review", severity="error", summary=message)
        for message in equation.get("errors") or []
    ]
    aggregate = equation.get("aggregate") or {}
    if decimal_value(aggregate.get("difference")) != 0:
        findings.append(
            make_finding(
                section="inventory_review",
                severity="error",
                summary="Posted inventory closing differs from the selected count.",
                evidence=[
                    f"Computed closing: {decimal_text(decimal_value(aggregate.get('closing')))}",
                    f"Selected closing: {decimal_text(decimal_value(aggregate.get('selected')))}",
                ],
            )
        )
    for warehouse_id, item in sorted((equation.get("warehouses") or {}).items()):
        if decimal_value(item.get("difference")) == 0:
            continue
        findings.append(
            make_finding(
                section="inventory_review",
                severity="error",
                summary=f"Posted inventory in warehouse {warehouse_id} differs from the selected count.",
                evidence=[
                    f"Computed closing: {decimal_text(decimal_value(item.get('closing')))}",
                    f"Selected closing: {decimal_text(decimal_value(item.get('selected')))}",
                ],
            )
        )
    instruction = equation.get("instruction")
    if findings and isinstance(instruction, dict):
        findings[-1]["evidence"].append(
            f"Proposed {instruction.get('direction')} of {decimal_text(decimal_value(instruction.get('quantity')))} "
            f"in warehouse {instruction.get('warehouse_id')} requires separate approval before it is executed."
        )
    return findings


def evaluate_inventory_review(source: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    if not source["inventory_expected"]:
        return []

    findings: list[dict[str, Any]] = []
    live_has_inventory_signals = bool(live["article_ids"] or live["warehouse_ids"])

    if not live_has_inventory_signals:
        findings.append(
            make_finding(
                section="inventory_review",
                severity="warn",
                summary="Source pack carries inventory or warehouse signals, but live invoice and purchase rows do not show article or warehouse IDs.",
                evidence=[
                    f"Source warehouse IDs: {', '.join(source['warehouse_ids']) or 'none'}",
                    f"Source inventory quantity total: {decimal_text(source['inventory_quantity_total'])}",
                ],
            )
        )
        return findings

    if source["warehouse_ids"] and not live["warehouse_ids"]:
        findings.append(
            make_finding(
                section="inventory_review",
                severity="warn",
                summary="Source pack has warehouse IDs, but live rows do not preserve them.",
                evidence=[f"Source warehouse IDs: {', '.join(source['warehouse_ids'])}"],
            )
        )
    elif len(source["warehouse_ids"]) > len(live["warehouse_ids"]):
        findings.append(
            make_finding(
                section="inventory_review",
                severity="warn",
                summary="Live rows preserve fewer warehouse IDs than source evidence suggests.",
                evidence=[
                    f"Source warehouse IDs: {', '.join(source['warehouse_ids']) or 'none'}",
                    f"Live warehouse IDs: {', '.join(live['warehouse_ids']) or 'none'}",
                ],
            )
        )
    return findings


def evaluate_continuity_review(source: dict[str, Any], previous_source: dict[str, Any] | None) -> list[dict[str, Any]]:
    if previous_source is None:
        return []

    currency = source["currency"]
    findings: list[dict[str, Any]] = []
    comparisons = (
        ("invoice_total", "Sales and refund activity"),
        ("purchase_total", "Fee and purchase activity"),
        ("incoming_total", "Payout activity"),
    )

    for field, label in comparisons:
        if previous_source[field] != 0 and source[field] == 0:
            findings.append(
                make_finding(
                    section="continuity_review",
                    severity="warn",
                    summary=f"{label} dropped to zero relative to the previous audited scope.",
                    evidence=[
                        f"Previous scope total: {decimal_text(previous_source[field])} {currency}",
                        f"Current scope total: {decimal_text(source[field])} {currency}",
                    ],
                )
            )
    return findings


def evaluate_date_semantics_review(live: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    missing_total = nested_counts(live["missing_business_dates"])
    if missing_total:
        findings.append(
            make_finding(
                section="date_semantics_review",
                severity="warn",
                summary="Some live documents were ignored because they lacked the business-date field used for audit scoping.",
                evidence=[
                    f"invoices missing created: {live['missing_business_dates']['invoices']['created']}",
                    f"invoices missing transaction_date: {live['missing_business_dates']['invoices']['transaction_date']}",
                    f"purchases missing created: {live['missing_business_dates']['purchases']['created']}",
                    f"purchases missing transaction_date: {live['missing_business_dates']['purchases']['transaction_date']}",
                    f"incomings missing income_date: {live['missing_business_dates']['incomings']['income_date']}",
                    f"payments missing payment_date: {live['missing_business_dates']['payments']['payment_date']}",
                ],
            )
        )

    created_time_outside = sum(int(count) for count in live["created_time_outside_scope"].values())
    if created_time_outside:
        findings.append(
            make_finding(
                section="date_semantics_review",
                severity="info",
                summary="Some audited live documents were inserted outside the audited scope, confirming why business dates must drive the audit.",
                evidence=[
                    f"invoices with created_time outside scope: {live['created_time_outside_scope']['invoices']}",
                    f"purchases with created_time outside scope: {live['created_time_outside_scope']['purchases']}",
                    f"incomings with created_time outside scope: {live['created_time_outside_scope']['incomings']}",
                    f"payments with created_time outside scope: {live['created_time_outside_scope']['payments']}",
                ],
            )
        )

    mismatch_total = nested_counts(live["scope_date_mismatches"])
    if mismatch_total:
        findings.append(
            make_finding(
                section="date_semantics_review",
                severity="warn",
                summary="Some invoices or purchases only matched the audited scope on one business-date field, so both created and transaction_date need review.",
                evidence=[
                    f"invoices created-only: {live['scope_date_mismatches']['invoices']['created_only']}",
                    f"invoices transaction-only: {live['scope_date_mismatches']['invoices']['transaction_only']}",
                    f"purchases created-only: {live['scope_date_mismatches']['purchases']['created_only']}",
                    f"purchases transaction-only: {live['scope_date_mismatches']['purchases']['transaction_only']}",
                ],
            )
        )

    return findings


def sample_document_records(live: dict[str, Any], scope: AuditScope) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    doc_specs = (
        ("invoice", "invoices", ("created", "transaction_date"), ("total_sum", "sum")),
        ("purchase", "purchases", ("created", "transaction_date"), ("total_sum", "sum")),
        ("incoming", "incomings", ("income_date",), ("income_sum", "sum", "total_sum")),
        ("payment", "payments", ("payment_date",), ("payment_sum", "sum", "total_sum")),
    )

    for doc_type, key, date_fields, amount_keys in doc_specs:
        documents = list(live["documents"][key])
        if not documents:
            continue
        decorated = []
        for document in documents:
            identifier = str(document.get("id") or document.get("number") or json.dumps(document, sort_keys=True))
            stable_hash = hashlib.sha256(f"{scope.label}:{doc_type}:{identifier}".encode("utf-8")).hexdigest()  # noqa: UP012
            decorated.append((stable_hash, document))
        decorated.sort(key=lambda item: item[0])
        chosen = decorated[0][1]
        samples.append(
            {
                "doc_type": doc_type,
                "id": chosen.get("id"),
                "business_dates": {field: chosen.get(field) for field in date_fields},
                "created_time": chosen.get("created_time") or chosen.get("createdTime"),
                "amount": decimal_text(document_total(chosen, *amount_keys)),
            }
        )
    return samples


def evaluate_spot_checks(live: dict[str, Any], scope: AuditScope) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for sample in sample_document_records(live, scope):
        business_dates = sample["business_dates"]
        if not any(date_in_scope(value, scope) for value in business_dates.values()):
            findings.append(
                make_finding(
                    section="spot_checks",
                    severity="error",
                    summary=f"Sampled {sample['doc_type']} {sample['id']} falls outside the requested audit scope by business date.",
                    evidence=[f"{field}: {value}" for field, value in business_dates.items()],
                )
            )
            continue
        findings.append(
            make_finding(
                section="spot_checks",
                severity="info",
                summary=f"Sampled {sample['doc_type']} {sample['id']} stayed inside scope by business date.",
                evidence=[
                    *[f"{field}: {value}" for field, value in business_dates.items()],
                    f"Amount: {sample['amount']}",
                    f"created_time: {sample['created_time'] or 'n/a'}",
                ],
            )
        )
    return findings


def evaluate_audit(
    *,
    source_payloads: list[dict[str, Any]],
    live_state: dict[str, Any],
    scope: AuditScope,
    policy_text: str | None,
    previous_source_payloads: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    source_snapshot = build_source_snapshot(source_payloads, policy_text=policy_text)
    live_snapshot = build_live_snapshot(live_state)
    previous_source_snapshot = (
        build_source_snapshot(previous_source_payloads, policy_text=policy_text)
        if previous_source_payloads
        else None
    )

    findings: list[dict[str, Any]] = []
    findings.extend(evaluate_source_vs_simplbooks_totals(source_snapshot, live_snapshot))
    findings.extend(evaluate_bank_and_processor_completeness(source_snapshot, live_snapshot))
    findings.extend(evaluate_vat_review(source_snapshot, live_snapshot))
    findings.extend(evaluate_inventory_review(source_snapshot, live_snapshot))
    findings.extend(evaluate_continuity_review(source_snapshot, previous_source_snapshot))
    findings.extend(evaluate_date_semantics_review(live_snapshot))
    findings.extend(evaluate_spot_checks(live_snapshot, scope))

    error_count = sum(1 for item in findings if item["severity"] == "error")
    warning_count = sum(1 for item in findings if item["severity"] == "warn")
    info_count = sum(1 for item in findings if item["severity"] == "info")
    result = "fail" if error_count else "warn" if warning_count else "pass"

    sections = {section: [] for section in SECTIONS}
    for finding in findings:
        sections[finding["section"]].append(finding)

    evaluation = {
        "checked_at": utc_now_iso(),
        "result": result,
        "error_count": error_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "findings": findings,
        "sections": sections,
    }
    return evaluation, source_snapshot, previous_source_snapshot


def render_findings(findings: list[dict[str, Any]]) -> list[str]:
    if not findings:
        return ["- none"]

    ordered = sorted(findings, key=lambda item: (SEVERITY_ORDER[item["severity"]], item["summary"]))
    lines = []
    for item in ordered:
        suffix = ""
        if item["evidence"]:
            suffix = " Evidence: " + "; ".join(item["evidence"])
        lines.append(f"- `{item['severity']}` {item['summary']}{suffix}")
    return lines


def render_report(
    *,
    scope: AuditScope,
    evaluation: dict[str, Any],
    source_snapshot: dict[str, Any],
    live_snapshot: dict[str, Any],
    previous_source_snapshot: dict[str, Any] | None,
    normalized_paths: list[Path],
    output_path: Path,
    policy_path: Path | None,
    company_name: str,
    cwd: Path,
) -> str:
    lines = [
        "# Audit Report",
        "",
        "## Scope",
        f"- Company: {company_name}",
        f"- Period: {scope.label}",
        f"- Scope type: {scope.kind}",
        f"- Sources reviewed: {source_snapshot['canonical_source_count']} canonical source file(s) across {len(normalized_paths)} normalized artifact(s)",
        f"- Normalized artifacts: {', '.join(display_path(path, cwd) for path in normalized_paths)}",
        f"- Policy memo: `{display_path(policy_path, cwd)}`" if policy_path and policy_path.exists() else "- Policy memo: not provided",
        "",
        "## Overall Result",
        f"- Result: `{evaluation['result']}`",
        f"- Errors: {evaluation['error_count']}",
        f"- Warnings: {evaluation['warning_count']}",
        f"- Infos: {evaluation['info_count']}",
        f"- Live invoices/purchases/incomings/payments: {live_snapshot['invoice_count']}/{live_snapshot['purchase_count']}/{live_snapshot['incoming_count']}/{live_snapshot['payment_count']}",
        "",
        "## Findings",
        *render_findings(evaluation["findings"]),
        "",
    ]

    for section in SECTIONS:
        lines.append(f"## {SECTION_TITLES[section]}")
        lines.extend(render_findings(evaluation["sections"].get(section, [])))
        lines.append("")

    follow_up_lines = [
        "- Investigate every `error` before treating the audited period as closed.",
        "- Re-run `bookaudit` after fixing any live Simplbooks entries or rebuilding source artifacts.",
        "- Keep this report with the company-local artifacts for the audited scope.",
    ]
    if previous_source_snapshot is None:
        follow_up_lines.append("- Previous-period normalized artifacts were not available, so continuity review stayed limited.")
    lines.extend(
        [
            "## Follow-Up Actions",
            *follow_up_lines,
            "",
        ]
    )
    return "\n".join(lines)


def run_audit(
    *,
    scope_label: str,
    company_dir: Path | None,
    company_id: str | None,
    normalized_override: str | None,
    policy_override: str | None,
    output_override: str | None,
    request_log_override: str | None,
    token_file: str,
    cwd: Path,
    client: SimplbooksClient | None = None,
) -> dict[str, Any]:
    scope = parse_scope(scope_label)
    normalized_paths = resolve_normalized_paths(company_dir=company_dir, scope=scope, override=normalized_override)
    normalized_payloads = load_normalized_payloads(normalized_paths, scope=scope)
    previous_paths = resolve_previous_normalized_paths(company_dir=company_dir, scope=scope)
    previous_payloads = load_normalized_payloads(previous_paths, scope=parse_scope(previous_scope_label(scope))) if previous_paths else []
    policy_path = resolve_policy_path(company_dir=company_dir, normalized_paths=normalized_paths, override=policy_override)
    policy_text = load_optional_text(policy_path)
    output_path = resolve_output_path(company_dir=company_dir, normalized_paths=normalized_paths, scope=scope, override=output_override)

    if client is None:
        resolved_company_id = resolve_company_id(company_id, company_dir=str(company_dir) if company_dir else None)
        client = SimplbooksClient(
            resolved_company_id,
            load_token(token_file),
            request_log_path=request_log_override,
        )

    live_state = collect_live_state(client, scope=scope)
    evaluation, source_snapshot, previous_source_snapshot = evaluate_audit(
        source_payloads=normalized_payloads,
        live_state=live_state,
        scope=scope,
        policy_text=policy_text,
        previous_source_payloads=previous_payloads,
    )
    live_snapshot = build_live_snapshot(live_state)

    company_slug = str(normalized_payloads[0].get("company_slug") or (company_dir.name if company_dir else scope.label))
    company_name = resolve_company_name(company_dir=str(company_dir)) if company_dir is not None else None
    company_name = company_name or company_slug
    report = render_report(
        scope=scope,
        evaluation=evaluation,
        source_snapshot=source_snapshot,
        live_snapshot=live_snapshot,
        previous_source_snapshot=previous_source_snapshot,
        normalized_paths=normalized_paths,
        output_path=output_path,
        policy_path=policy_path,
        company_name=company_name,
        cwd=cwd,
    )
    write_text(output_path, report)

    return {
        "company_name": company_name,
        "company_slug": company_slug,
        "period": scope.label,
        "normalized_artifacts": [str(path) for path in normalized_paths],
        "output": str(output_path),
        "result": evaluation["result"],
        "error_count": evaluation["error_count"],
        "warning_count": evaluation["warning_count"],
        "info_count": evaluation["info_count"],
        "invoice_count": live_snapshot["invoice_count"],
        "purchase_count": live_snapshot["purchase_count"],
        "incoming_count": live_snapshot["incoming_count"],
        "payment_count": live_snapshot["payment_count"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit live Simplbooks state against source-derived normalized artifacts")
    parser.add_argument("--company-dir", help="Company folder, e.g. companies/example")
    parser.add_argument("--company-id", help="Explicit Simplbooks company ID")
    parser.add_argument("--period", required=True, help="Audit scope in YYYY-MM or YYYY format")
    parser.add_argument("--normalized", help="Path to normalized JSON for month audits, or a normalized directory for year audits")
    parser.add_argument("--policy-memo", help="Optional path to policy memo markdown")
    parser.add_argument("--output", help="Optional output path for the Markdown audit report")
    parser.add_argument("--request-log", help="Optional JSONL path for low-level Simplbooks request/response logging")
    parser.add_argument("--token-file", default=".apikey", help="API token file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    company_dir = Path(args.company_dir) if args.company_dir else None
    summary = run_audit(
        scope_label=args.period,
        company_dir=company_dir,
        company_id=args.company_id,
        normalized_override=args.normalized,
        policy_override=args.policy_memo,
        output_override=args.output,
        request_log_override=args.request_log,
        token_file=args.token_file,
        cwd=Path.cwd(),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
