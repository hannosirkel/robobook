#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bank_allocations import BankAllocationError, allocation_key, bank_ledger_key, load_bank_allocations, period_allocations, statement_identity
from bookbuilder import planned_sales_groups
from reference_artifacts import bind_file
from simplbooks_api import SimplbooksError, resolve_company_name


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
    "clearing_transactions",
    "bank_balances",
    "purchase_expenses",
    "purchase_credits",
    "inventory_movements",
    "manual_adjustments",
    "other",
)


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_period(value: str) -> tuple[date, date]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if not match:
        raise SimplbooksError(f"Period must use YYYY-MM format, got: {value}")
    year = int(match.group(1))
    month = int(match.group(2))
    if not 1 <= month <= 12:
        raise SimplbooksError(f"Invalid month in period: {value}")
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return start, end


def previous_period(value: str) -> str:
    start, _ = parse_period(value)
    if start.month == 1:
        return f"{start.year - 1}-12"
    return f"{start.year}-{start.month - 1:02d}"


def display_path(path: Path, root_dir: Path) -> str:
    try:
        return str(path.relative_to(root_dir))
    except ValueError:
        return str(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SimplbooksError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SimplbooksError(f"Invalid JSON in {path}: {exc}") from exc


def load_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return load_json(path)


def load_optional_text(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def decimal_value(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise SimplbooksError(f"Could not parse decimal value: {value!r}") from exc


def decimal_number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized.quantize(Decimal("1")), "f")
    return format(normalized, "f")


def record_currency(record: dict[str, Any], default_currency: str = "EUR") -> str:
    currency = str(record.get("currency") or "").strip().upper()
    return currency or default_currency


def record_refs(records: list[dict[str, Any]]) -> list[str]:
    refs = sorted({str(record.get("record_id")) for record in records if record.get("record_id")})
    return refs


def make_artifact_ref(
    artifact_path: str,
    *,
    record_refs_list: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"artifact_path": artifact_path}
    if record_refs_list:
        payload["record_refs"] = record_refs_list
    if notes:
        payload["notes"] = notes
    return payload


def make_check(
    *,
    check_id: str,
    name: str,
    status: str,
    lhs_label: str | None = None,
    lhs_amount: Decimal | None = None,
    rhs_label: str | None = None,
    rhs_amount: Decimal | None = None,
    delta: Decimal | None = None,
    threshold: Decimal | None = None,
    notes: list[str] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "name": name,
        "status": status,
        "lhs_label": lhs_label,
        "lhs_amount": decimal_number(lhs_amount),
        "rhs_label": rhs_label,
        "rhs_amount": decimal_number(rhs_amount),
        "delta": decimal_number(delta),
        "threshold": decimal_number(threshold),
        "notes": notes or [],
        "evidence_refs": evidence_refs or [],
    }


def make_exception(
    *,
    exception_id: str,
    severity: str,
    summary: str,
    blocking: bool = False,
    artifact_refs: list[dict[str, Any]] | None = None,
    recommended_action: str | None = None,
) -> dict[str, Any]:
    return {
        "exception_id": exception_id,
        "severity": severity,
        "summary": summary,
        "blocking": blocking,
        "artifact_refs": artifact_refs or [],
        "recommended_action": recommended_action,
    }


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def record_haystack(record: dict[str, Any]) -> str:
    attributes = record.get("attributes") or {}
    pieces: list[str] = [
        str(record.get("source_system") or ""),
        str(record.get("source_type") or ""),
        str(record.get("event_type") or ""),
        str(record.get("description") or ""),
        str(record.get("channel") or ""),
        str(record.get("external_ref") or ""),
    ]
    for value in attributes.values():
        pieces.append(str(value))
    return normalize_text(" ".join(piece for piece in pieces if piece))


def classify_record(record: dict[str, Any], keyword_map: dict[str, tuple[str, ...]]) -> str | None:
    haystack = record_haystack(record)
    for label, keywords in keyword_map.items():
        if all(keyword in haystack for keyword in keywords):
            return label
    return None


def infer_processor(record: dict[str, Any]) -> str | None:
    source_system = str(record.get("source_system") or "").lower()
    channel = str(record.get("channel") or "").lower()
    event_type = str(record.get("event_type") or "").lower()
    if source_system in PROCESSOR_KEYWORDS:
        return source_system
    if channel in PROCESSOR_KEYWORDS:
        return channel
    if source_system == "woo" or channel == "woo" or event_type.startswith("woo_"):
        return None
    return classify_record(record, PROCESSOR_KEYWORDS)


def infer_fulfillment_partner(record: dict[str, Any]) -> str | None:
    return classify_record(record, FULFILLMENT_KEYWORDS)


def sum_amount(records: list[dict[str, Any]], field: str = "gross_amount") -> Decimal:
    total = Decimal("0")
    for record in records:
        total += decimal_value(record.get(field))
    return total


def sum_abs_amount(records: list[dict[str, Any]], field: str = "gross_amount") -> Decimal:
    total = Decimal("0")
    for record in records:
        total += abs(decimal_value(record.get(field)))
    return total


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
                return Decimal("0")
    return Decimal("0")


def canonical_source_systems(payload: dict[str, Any]) -> set[str]:
    systems = {
        str(source.get("source_system"))
        for source in payload.get("sources", [])
        if source.get("canonical") and source.get("source_system")
    }
    if systems:
        return systems

    for category in RECORD_CATEGORIES:
        for record in payload.get("records", {}).get(category, []):
            source_system = str(record.get("source_system") or "").strip()
            if source_system:
                systems.add(source_system)
    return systems


def inventory_is_relevant(policy_memo_text: str | None, entity_map: dict[str, Any] | None) -> bool:
    if entity_map and entity_map.get("warehouses"):
        return True
    if not policy_memo_text:
        return False
    text = normalize_text(policy_memo_text)
    if "inventory" in text and "ignored" not in text:
        return True
    return "warehouse" in text or "stock" in text


def import_normalized_exceptions(
    *,
    normalized_path_display: str,
    normalized_exceptions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for item in normalized_exceptions:
        source_refs = item.get("source_refs") or []
        record_ref_values = []
        for source_ref in source_refs:
            pieces = [str(source_ref.get("source_id") or "").strip()]
            row_ref = str(source_ref.get("row_ref") or "").strip()
            page_ref = str(source_ref.get("page_ref") or "").strip()
            if row_ref:
                pieces.append(row_ref)
            if page_ref:
                pieces.append(page_ref)
            rendered = ":".join(piece for piece in pieces if piece)
            if rendered:
                record_ref_values.append(rendered)

        converted.append(
            make_exception(
                exception_id=f"normalized:{item.get('exception_id')}",
                severity=str(item.get("severity") or "warn"),
                summary=str(item.get("reason") or "Normalized exception"),
                blocking=bool(item.get("blocking")),
                artifact_refs=[
                    make_artifact_ref(
                        normalized_path_display,
                        record_refs_list=sorted(set(record_ref_values)),
                        notes="Imported from normalized artifact.",
                    )
                ],
                recommended_action=item.get("suggested_follow_up"),
            )
        )
    return converted


def missing_processor_evidence_exceptions(
    *,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    bank_allocations: dict[Any, dict[str, Any]] | None = None,
    annual_clearing_records: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    exceptions: list[dict[str, Any]] = []
    processor_records: dict[str, list[dict[str, Any]]] = {name: [] for name in PROCESSOR_KEYWORDS}
    for category in ("sales", "refunds", "fees", "payouts"):
        for record in records.get(category, []):
            processor = infer_processor(record)
            if processor:
                processor_records[processor].append(record)

    for processor in sorted(PROCESSOR_KEYWORDS):
        clearing_index = annual_clearing_records or {
            str(record.get("record_id") or ""): record
            for record in records.get("clearing_transactions", [])
            if str(record.get("record_id") or "")
        }
        supported_families = {
            "processor_payout_transfer",
            "failed_payment_transfer_and_return",
            "failed_quartermaster_payment_and_return",
            "paypal_funded_quartermaster_payment",
            "internal_transfer",
        }
        reviewed_transfer_record_ids = {
            str(item.get("record_id") or "")
            for item in (bank_allocations or {}).values()
            if str(item.get("disposition") or "") == "clearing_transfer"
            and str((item.get("review") or {}).get("status") or "") == "approved"
            and str((item.get("target") or {}).get("document_type") or "") == "financial_transaction"
            and str((item.get("target") or {}).get("transaction_family") or "") in supported_families
            and bool(_validated_clearing_allocation_references({"candidate": item}, clearing_index))
            and all(
                str(((clearing_index.get(str(ref)) or {}).get("attributes") or {}).get("clearing_provider") or "").lower()
                == processor
                for ref in (item.get("target") or {}).get("clearing_record_ids") or []
            )
        }
        bank_records = [
            record
            for record in records.get("bank_transactions", [])
            if infer_processor(record) == processor and decimal_value(record.get("gross_amount")) > 0
            and str(record.get("record_id") or "") not in reviewed_transfer_record_ids
        ]
        if not bank_records or processor_records[processor]:
            continue

        exceptions.append(
            make_exception(
                exception_id=f"bookrecon:missing-processor-evidence:{processor}",
                severity="error",
                summary=(
                    f"Bank receipts indicate {processor} activity, but no normalized {processor} sales, refunds, fees, "
                    "or payout records were found for this period."
                ),
                blocking=True,
                artifact_refs=[
                    make_artifact_ref(
                        normalized_path_display,
                        record_refs_list=record_refs(bank_records),
                        notes="Processor inferred from bank transaction text.",
                    )
                ],
                recommended_action=(
                    f"Add the {processor} export for this month or confirm why {processor} cash activity should be "
                    "booked without a processor-side source pack."
                ),
            )
        )
    return exceptions


def _allocation_amount_matches(allocation: dict[str, Any], expected: Decimal) -> bool:
    """Return whether the reviewed allocation and every split part prove its bank amount."""
    try:
        amount = decimal_value(allocation.get("amount"))
        if amount != expected:
            return False
        if allocation.get("disposition") != "reviewed_split":
            return True
        parts = allocation.get("parts")
        if not isinstance(parts, list) or not parts:
            return False
        return sum((decimal_value(part.get("amount")) for part in parts if isinstance(part, dict)), Decimal("0")) == amount
    except SimplbooksError:
        return False


def _balance_type(record: dict[str, Any]) -> str:
    return str((record.get("attributes") or {}).get("balance_type") or "").strip().upper()


def _bank_balance_values(
    records: list[dict[str, Any]], *, target_period: str
) -> tuple[
    dict[tuple[str, str], list[Decimal]],
    dict[tuple[str, str], list[Decimal]],
    dict[tuple[str, str], list[dict[str, Any]]],
    list[str],
    list[str],
]:
    target_start, target_end = parse_period(target_period)
    openings: dict[tuple[str, str], list[Decimal]] = {}
    closings: dict[tuple[str, str], list[Decimal]] = {}
    supporting_scopes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    errors: list[str] = []
    notes: list[str] = []
    for record in records:
        attributes = record.get("attributes") or {}
        iban = re.sub(r"\s+", "", str(attributes.get("iban") or attributes.get("account_iban") or "")).upper()
        currency = record_currency(record)
        if not iban:
            errors.append(f"CAMT balance {record.get('record_id') or '<unknown>'} has no IBAN.")
            continue
        ledger = iban, currency
        balance_type = _balance_type(record)
        scope_from = attributes.get("statement_from")
        scope_to = attributes.get("statement_to")
        has_from = scope_from not in (None, "")
        has_to = scope_to not in (None, "")
        comparable = not has_from and not has_to
        if has_from != has_to:
            errors.append(
                f"CAMT balance {record.get('record_id') or '<unknown>'} has incomplete statement scope; "
                "statement_from and statement_to must both be present."
            )
        elif has_from and has_to:
            try:
                scope_start = date.fromisoformat(str(scope_from))
                scope_end = date.fromisoformat(str(scope_to))
            except ValueError:
                errors.append(
                    f"CAMT balance {record.get('record_id') or '<unknown>'} has malformed statement scope "
                    f"{scope_from!r} through {scope_to!r}."
                )
            else:
                if scope_end < scope_start:
                    errors.append(
                        f"CAMT balance {record.get('record_id') or '<unknown>'} has reversed statement scope "
                        f"{scope_from} through {scope_to}."
                    )
                elif scope_start == target_start and scope_end == target_end:
                    comparable = True
                else:
                    supporting_scopes.setdefault(ledger, []).append(
                        {
                            "statement_from": scope_start.isoformat(),
                            "statement_to": scope_end.isoformat(),
                            "balance_type": balance_type or "other",
                        }
                    )
                    notes.append(
                        f"CAMT {balance_type or 'other'} evidence for {iban}/{currency} covers "
                        f"{scope_start.isoformat()} through {scope_end.isoformat()} and is deferred to annual verification."
                    )
        if not comparable:
            continue
        if balance_type in {"OPBD", "PRCD", "OPENING"}:
            openings.setdefault(ledger, []).append(decimal_value(record.get("gross_amount")))
        elif balance_type in {"CLBD", "CLOSING"}:
            closings.setdefault(ledger, []).append(decimal_value(record.get("gross_amount")))
    return openings, closings, supporting_scopes, errors, notes


def build_physical_bank_coverage_check(
    *,
    normalized_path_display: str,
    target_period: str,
    bank_records: list[dict[str, Any]],
    allocations: dict[str, dict[str, Any]],
    bank_balance_records: list[dict[str, Any]] | None = None,
    allocation_errors: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Report exact physical-bank allocation coverage without altering the legacy build gate.

    Phase A deliberately exposes deficiencies as a warning and a false readiness bit.
    Later write-capable stages independently turn the same evidence into hard blocks.
    """
    errors = list(allocation_errors or [])
    indexed: dict[tuple[str, str, str], tuple[dict[str, Any], tuple[str, str]]] = {}
    ledger_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in bank_records:
        if record.get("source_system") != "bank":
            errors.append(
                f"Malformed bank transaction {record.get('record_id') or '<unknown>'}: "
                f"source_system must be exactly 'bank', got {record.get('source_system')!r}."
            )
            continue
        try:
            statement_id = statement_identity(record)
            ledger = bank_ledger_key(record)
        except BankAllocationError as exc:
            errors.append(str(exc))
            continue
        key = statement_id, *ledger
        if key in indexed:
            errors.append(f"Physical bank allocation key is duplicated: {key}.")
            continue
        indexed[key] = (record, ledger)
        ledger_rows.setdefault(ledger, []).append(record)

    canonical_allocations: dict[tuple[str, str, str], dict[str, Any]] = {}
    for allocation in allocations.values():
        try:
            key = allocation_key(allocation)
        except BankAllocationError as exc:
            errors.append(str(exc))
            continue
        if key in canonical_allocations:
            errors.append(f"Reviewed bank allocation key is duplicated: {key}.")
            continue
        canonical_allocations[key] = allocation

    exact_allocated_keys: set[tuple[str, str, str]] = set()
    allocation_total = Decimal("0")
    for key, (record, _) in indexed.items():
        statement_id = key[0]
        allocation = canonical_allocations.get(key)
        if allocation is None:
            errors.append(f"Missing reviewed bank allocation for {statement_id}.")
            continue
        expected_amount = decimal_value(record.get("gross_amount"))
        mismatches: list[str] = []
        if allocation_key(allocation) != key:
            mismatches.append("statement identity")
        if str(allocation.get("record_id") or "") != str(record.get("record_id") or ""):
            mismatches.append("record locator")
        if str(allocation.get("period") or "") != str(record.get("event_date") or "")[:7]:
            mismatches.append("period")
        if str(allocation.get("currency") or "").upper() != record_currency(record):
            mismatches.append("currency")
        if not _allocation_amount_matches(allocation, expected_amount):
            mismatches.append("signed amount or split total")
        if mismatches:
            errors.append(f"Reviewed bank allocation does not exactly match {statement_id}: {', '.join(mismatches)}.")
            continue
        exact_allocated_keys.add(key)
        allocation_total += expected_amount

    for key in sorted(set(canonical_allocations) - set(indexed)):
        errors.append(f"Reviewed bank allocation is stale or outside this period: {key}.")

    openings, closings, supporting_scopes, balance_errors, scope_notes = _bank_balance_values(
        bank_balance_records or [], target_period=target_period
    )
    errors.extend(balance_errors)
    ledgers: list[dict[str, Any]] = []
    for ledger in sorted(set(ledger_rows) | set(openings) | set(closings) | set(supporting_scopes)):
        iban, currency = ledger
        rows = ledger_rows.get(ledger, [])
        movement = sum_amount(rows)
        opening_values = openings.get(ledger, [])
        closing_values = closings.get(ledger, [])
        if len(opening_values) > 1:
            errors.append(f"CAMT opening balance is ambiguous for {iban}/{currency}.")
        if len(closing_values) > 1:
            errors.append(f"CAMT closing balance is ambiguous for {iban}/{currency}.")
        opening = opening_values[0] if len(opening_values) == 1 else None
        closing = closing_values[0] if len(closing_values) == 1 else None
        computed = opening + movement if opening is not None else None
        if computed is not None and closing is not None and computed != closing:
            errors.append(
                f"CAMT balance continuity mismatch for {iban}/{currency}: opening plus physical movements "
                f"is {decimal_text(computed)}, closing is {decimal_text(closing)}."
            )
        ledger_summary: dict[str, Any] = {
                "iban": iban,
                "currency": currency,
                "physical_bank_row_count": len(rows),
                "allocated_row_count": sum(
                    1 for record in rows if (statement_identity(record), *bank_ledger_key(record)) in exact_allocated_keys
                ),
                "unallocated_row_count": sum(
                    1 for record in rows if (statement_identity(record), *bank_ledger_key(record)) not in exact_allocated_keys
                ),
                "credit_total": decimal_number(sum((max(decimal_value(row.get("gross_amount")), Decimal("0")) for row in rows), Decimal("0"))),
                "debit_total": decimal_number(sum((min(decimal_value(row.get("gross_amount")), Decimal("0")) for row in rows), Decimal("0"))),
                "net_movement": decimal_number(movement),
                "camt_opening_balance": decimal_number(opening),
                "computed_closing_balance": decimal_number(computed),
                "camt_closing_balance": decimal_number(closing),
        }
        if supporting_scopes.get(ledger):
            ledger_summary["camt_evidence_scopes"] = sorted(
                supporting_scopes[ledger], key=lambda item: (item["statement_from"], item["statement_to"], item["balance_type"])
            )
        ledgers.append(ledger_summary)

    coverage = {
        "coverage_ready": not errors,
        "physical_bank_row_count": len(indexed),
        "allocated_row_count": len(exact_allocated_keys),
        "unallocated_row_count": len(indexed) - len(exact_allocated_keys),
        "credit_total": decimal_number(sum((max(decimal_value(record.get("gross_amount")), Decimal("0")) for record, _ in indexed.values()), Decimal("0"))),
        "debit_total": decimal_number(sum((min(decimal_value(record.get("gross_amount")), Decimal("0")) for record, _ in indexed.values()), Decimal("0"))),
        "net_movement": decimal_number(sum((decimal_value(record.get("gross_amount")) for record, _ in indexed.values()), Decimal("0"))),
        "ledgers": ledgers,
    }
    if errors:
        notes = sorted(set(errors + scope_notes))
        status = "warn"
    elif indexed:
        notes = sorted(set(scope_notes)) or ["Every physical bank row has one exact reviewed allocation; CAMT balances agree where both endpoints were available."]
        status = "pass"
    elif ledgers:
        notes = sorted(set(scope_notes)) or ["No physical bank rows were normalized; CAMT balance-only ledgers agree with zero movement."]
        status = "pass"
    else:
        notes = ["No physical bank rows were normalized for this period."]
        status = "pass"
    return (
        make_check(
            check_id="physical-bank-coverage",
            name="Physical bank allocation coverage",
            status=status,
            lhs_label="Physical bank net movement",
            lhs_amount=sum((decimal_value(record.get("gross_amount")) for record, _ in indexed.values()), Decimal("0")),
            rhs_label="Exactly allocated bank net movement",
            rhs_amount=allocation_total,
            delta=sum((decimal_value(record.get("gross_amount")) for record, _ in indexed.values()), Decimal("0")) - allocation_total,
            notes=notes,
            evidence_refs=[make_artifact_ref(normalized_path_display, record_refs_list=record_refs(list(bank_records)))],
        ),
        coverage,
    )


def _validated_clearing_allocation_references(
    allocations: dict[Any, dict[str, Any]],
    clearing_records: dict[str, dict[str, Any]],
) -> set[str]:
    """Resolve only explicit, economically bound clearing bridge evidence."""
    resolved: set[str] = set()
    claimed: set[str] = set()
    for allocation in allocations.values():
        target = allocation.get("target") or {}
        refs = target.get("clearing_record_ids")
        evidence = target.get("clearing_evidence")
        totals = target.get("clearing_totals")
        relation = str(target.get("clearing_relation") or "")
        if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)):
            continue
        if not isinstance(evidence, list) or not isinstance(totals, dict):
            continue
        evidence_by_id = {
            str(item.get("record_id") or ""): item
            for item in evidence
            if isinstance(item, dict) and str(item.get("record_id") or "")
        }
        if set(refs) != set(evidence_by_id) or set(refs) & claimed:
            continue
        computed_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        valid = True
        for ref in refs:
            record = clearing_records.get(str(ref))
            item = evidence_by_id.get(str(ref))
            if record is None or item is None:
                valid = False
                break
            attributes = record.get("attributes") or {}
            actual = {
                "period": str(record.get("event_date") or "")[:7],
                "currency": record_currency(record),
                "amount": decimal_value(record.get("gross_amount")),
                "provider": str(attributes.get("clearing_provider") or "").strip().lower(),
                "account": str(attributes.get("clearing_account") or "").strip(),
            }
            if (
                str(item.get("period") or "") != actual["period"]
                or str(item.get("currency") or "").upper() != actual["currency"]
                or decimal_value(item.get("amount")) != actual["amount"]
                or str(item.get("provider") or "").strip().lower() != actual["provider"]
                or str(item.get("account") or "").strip() != actual["account"]
                or not actual["provider"]
                or not actual["account"]
            ):
                valid = False
                break
            computed_totals[actual["currency"]] += actual["amount"]
        expected_totals = {str(key).upper(): decimal_value(value) for key, value in totals.items()}
        if not valid or dict(computed_totals) != expected_totals:
            continue
        if decimal_value(target.get("bridge_amount")) != decimal_value(allocation.get("amount")):
            continue
        if relation == "exact_amount":
            if len(refs) != 1:
                continue
            record = clearing_records[str(refs[0])]
            if (
                record_currency(record) != str(allocation.get("currency") or "").upper()
                or decimal_value(record.get("gross_amount")) != decimal_value(allocation.get("amount"))
            ):
                continue
        elif relation != "reviewed_group":
            continue
        claimed.update(refs)
        resolved.update(refs)
    return resolved


def _clearing_balance_value(records: list[dict[str, Any]], names: tuple[str, ...]) -> tuple[Decimal | None, bool]:
    values = {
        decimal_value((record.get("attributes") or {}).get(name))
        for record in records
        for name in names
        if (record.get("attributes") or {}).get(name) not in (None, "")
    }
    return (next(iter(values)), len(values) == 1) if values else (None, True)


def build_clearing_continuity_checks(
    *,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    allocations: dict[str, dict[str, Any]],
    annual_clearing_records: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """Check provider/account/currency clearing evidence, retaining Phase-A report-only status."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records.get("clearing_transactions", []):
        attributes = record.get("attributes") or {}
        provider = str(attributes.get("clearing_provider") or "").strip().lower() or "unidentified"
        account = str(attributes.get("clearing_account") or "").strip() or "unidentified"
        grouped.setdefault((provider, account, record_currency(record)), []).append(record)

    clearing_record_index = annual_clearing_records or {
        str(record.get("record_id") or ""): record
        for record in records.get("clearing_transactions", [])
        if str(record.get("record_id") or "")
    }
    allocation_references = _validated_clearing_allocation_references(allocations, clearing_record_index)
    bridge_references: set[str] = set()
    for category, category_records in records.items():
        if category == "clearing_transactions":
            continue
        for record in category_records:
            explicit_refs = (record.get("attributes") or {}).get("clearing_record_ids")
            if isinstance(explicit_refs, list):
                bridge_references.update(str(item) for item in explicit_refs if str(item) in clearing_record_index)

    results: dict[tuple[str, str, str], tuple[list[dict[str, Any]], list[str]]] = {}
    for (provider, account, currency), group_records in grouped.items():
        missing = [
            str(record.get("record_id") or "<unknown>")
            for record in group_records
            if str(record.get("record_id") or "") not in allocation_references | bridge_references
        ]
        notes: list[str] = []
        if missing:
            notes.append(f"{account}: unresolved clearing movement record(s): {', '.join(sorted(missing))}.")
        opening, opening_unambiguous = _clearing_balance_value(group_records, ("opening_balance", "clearing_opening_balance"))
        closing, closing_unambiguous = _clearing_balance_value(group_records, ("closing_balance", "clearing_closing_balance"))
        movement = sum_amount(group_records)
        if not opening_unambiguous or not closing_unambiguous:
            notes.append(f"{account}: clearing balance evidence is ambiguous.")
        elif opening is not None and closing is not None:
            computed = opening + movement
            if computed != closing:
                notes.append(
                    f"{account}: opening plus movements is {decimal_text(computed)}, closing is {decimal_text(closing)}."
                )
        else:
            notes.append(f"{account}: no paired opening and closing clearing balances were normalized.")
        results[(provider, account, currency)] = group_records, notes

    checks: list[dict[str, Any]] = []
    ready = True
    movement_ids: set[str] = set()
    unresolved_ids: set[str] = set()
    for (provider, account, currency), (group_records, notes) in sorted(results.items()):
        movement_ids.update(str(record.get("record_id")) for record in group_records if record.get("record_id"))
        allocation_and_bridge_refs = allocation_references | bridge_references
        unresolved_ids.update(
            str(record.get("record_id"))
            for record in group_records
            if record.get("record_id") and str(record.get("record_id")) not in allocation_and_bridge_refs
        )
        status = "pass" if not notes else "warn"
        ready = ready and status == "pass"
        checks.append(
            make_check(
                check_id=(
                    f"clearing-continuity:{quote(provider, safe='')}:{quote(account, safe='')}:{quote(currency.lower(), safe='')}"
                ),
                name=f"{provider} {account} clearing continuity ({currency})",
                status=status,
                lhs_label="Clearing net movement",
                lhs_amount=sum_amount(group_records),
                notes=notes or ["Every clearing movement is linked and opening plus movements equals closing balance."],
                evidence_refs=[make_artifact_ref(normalized_path_display, record_refs_list=record_refs(group_records))],
            )
        )
    resolved_ids = movement_ids - unresolved_ids
    return checks, ready, {
        "clearing_movement_count": len(movement_ids),
        "resolved_clearing_count": len(resolved_ids),
        "unresolved_clearing_count": len(unresolved_ids),
        "clearing_movement_record_ids": sorted(movement_ids),
        "resolved_clearing_record_ids": sorted(resolved_ids),
        "unresolved_clearing_record_ids": sorted(unresolved_ids),
    }


def build_woo_sales_vs_processor_check(
    *,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    base_currency: str,
    amount_threshold: Decimal,
) -> dict[str, Any]:
    woo_records = [
        record
        for record in records.get("sales", [])
        if str(record.get("channel") or "") == "woo" or str(record.get("source_system") or "") == "woo"
    ]
    processor_records = [record for record in records.get("sales", []) if infer_processor(record)]

    if not woo_records and not processor_records:
        return make_check(
            check_id="woo-sales-vs-processor-gross",
            name="Woo sales totals vs processor gross sales",
            status="skipped",
            notes=["No Woo or processor-side sales records were normalized for this period."],
            threshold=amount_threshold,
        )
    if not woo_records:
        return make_check(
            check_id="woo-sales-vs-processor-gross",
            name="Woo sales totals vs processor gross sales",
            status="skipped",
            notes=["No Woo-derived sales records were available for channel-to-processor comparison."],
            threshold=amount_threshold,
        )
    if not processor_records:
        return make_check(
            check_id="woo-sales-vs-processor-gross",
            name="Woo sales totals vs processor gross sales",
            status="skipped",
            notes=["No processor-side sales records were normalized for comparison against Woo totals."],
            threshold=amount_threshold,
        )

    lhs_amount = sum_amount(woo_records, "gross_amount")
    rhs_amount = sum_amount(processor_records, "gross_amount")
    delta = lhs_amount - rhs_amount
    status = "pass" if abs(delta) <= amount_threshold else "fail"
    processors = sorted({infer_processor(record) for record in processor_records if infer_processor(record)})

    notes = []
    if processors:
        notes.append(f"Processor-side sales included: {', '.join(processors)}.")
    effective_groups, _, matched_processors_by_key, _ = planned_sales_groups(
        records.get("sales", []),
        base_currency=base_currency,
        amount_tolerance=amount_threshold,
    )
    matched_processor_labels = sorted(
        {
            label
            for labels in matched_processors_by_key.values()
            for label in labels
        }
    )
    posting_basis_labels = sorted({group_label for group_label, _ in effective_groups})
    if matched_processor_labels and posting_basis_labels:
        notes.append(
            "Posting basis should stay on merchant-side sales groups "
            f"({', '.join(posting_basis_labels)}); matched processor-side sales "
            f"({', '.join(matched_processor_labels)}) can remain settlement evidence only."
        )

    return make_check(
        check_id="woo-sales-vs-processor-gross",
        name="Woo sales totals vs processor gross sales",
        status=status,
        lhs_label="Woo gross sales",
        lhs_amount=lhs_amount,
        rhs_label="Processor gross sales",
        rhs_amount=rhs_amount,
        delta=delta,
        threshold=amount_threshold,
        notes=notes,
        evidence_refs=[
            make_artifact_ref(normalized_path_display, record_refs_list=record_refs(woo_records), notes="Woo sales records."),
            make_artifact_ref(
                normalized_path_display,
                record_refs_list=record_refs(processor_records),
                notes="Processor sales records.",
            ),
        ],
    )


def build_processor_payout_checks(
    *,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    amount_threshold: Decimal,
) -> list[dict[str, Any]]:
    observed_processors = {
        infer_processor(record)
        for record in records.get("payouts", []) + records.get("bank_transactions", [])
        if infer_processor(record)
    }
    checks: list[dict[str, Any]] = []

    for processor in sorted(observed_processors):
        payout_records = [record for record in records.get("payouts", []) if infer_processor(record) == processor]
        bank_records = [
            record
            for record in records.get("bank_transactions", [])
            if infer_processor(record) == processor and decimal_value(record.get("gross_amount")) > 0
        ]

        if not payout_records and not bank_records:
            continue

        lhs_amount = sum_amount(payout_records, "gross_amount")
        rhs_amount = sum_amount(bank_records, "gross_amount")
        delta = lhs_amount - rhs_amount
        notes: list[str] = []

        if not payout_records:
            status = "warn"
            notes.append(f"Bank receipts mention {processor}, but no normalized payout rows were available.")
        elif not bank_records:
            status = "fail"
            notes.append(f"{processor.capitalize()} payout rows exist, but no matching bank receipts were found.")
        else:
            status = "pass" if abs(delta) <= amount_threshold else "fail"

        checks.append(
            make_check(
                check_id=f"processor-payouts-vs-bank:{processor}",
                name=f"{processor.capitalize()} payouts vs bank receipts",
                status=status,
                lhs_label=f"{processor.capitalize()} payouts",
                lhs_amount=lhs_amount,
                rhs_label=f"Bank receipts tagged {processor}",
                rhs_amount=rhs_amount,
                delta=delta,
                threshold=amount_threshold,
                notes=notes,
                evidence_refs=[
                    make_artifact_ref(
                        normalized_path_display,
                        record_refs_list=record_refs(payout_records),
                        notes=f"{processor.capitalize()} payout rows.",
                    ),
                    make_artifact_ref(
                        normalized_path_display,
                        record_refs_list=record_refs(bank_records),
                        notes=f"Bank rows tagged as {processor}.",
                    ),
                ],
            )
        )

    return checks


def build_processor_settlement_checks(
    *,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    amount_threshold: Decimal,
) -> list[dict[str, Any]]:
    observed_processors = {
        infer_processor(record)
        for category in ("sales", "refunds", "fees", "payouts")
        for record in records.get(category, [])
        if infer_processor(record)
    }
    checks: list[dict[str, Any]] = []

    for processor in sorted(observed_processors):
        settlement_records = [
            record
            for category in ("sales", "refunds", "fees")
            for record in records.get(category, [])
            if infer_processor(record) == processor
        ]
        payout_records = [record for record in records.get("payouts", []) if infer_processor(record) == processor]

        if not settlement_records and not payout_records:
            continue
        if not payout_records:
            checks.append(
                make_check(
                    check_id=f"processor-settlement-bridge:{processor}",
                    name=f"{processor.capitalize()} settlement bridge",
                    status="skipped",
                    threshold=amount_threshold,
                    notes=[f"No {processor} payout rows were normalized, so same-month settlement bridging was skipped."],
                    evidence_refs=[
                        make_artifact_ref(
                            normalized_path_display,
                            record_refs_list=record_refs(settlement_records),
                            notes=f"{processor.capitalize()} sales, refunds, and fee rows.",
                        )
                    ],
                )
            )
            continue
        if not settlement_records:
            checks.append(
                make_check(
                    check_id=f"processor-settlement-bridge:{processor}",
                    name=f"{processor.capitalize()} settlement bridge",
                    status="warn",
                    threshold=amount_threshold,
                    notes=[f"{processor.capitalize()} payouts exist, but no matching sales, refunds, or fee rows were normalized."],
                    evidence_refs=[
                        make_artifact_ref(
                            normalized_path_display,
                            record_refs_list=record_refs(payout_records),
                            notes=f"{processor.capitalize()} payout rows.",
                        )
                    ],
                )
            )
            continue

        lhs_amount = sum_amount(settlement_records, "net_amount")
        rhs_amount = sum_amount(payout_records, "gross_amount")
        delta = lhs_amount - rhs_amount
        status = "pass" if abs(delta) <= amount_threshold else "warn"

        checks.append(
            make_check(
                check_id=f"processor-settlement-bridge:{processor}",
                name=f"{processor.capitalize()} settlement bridge",
                status=status,
                lhs_label=f"{processor.capitalize()} same-month net cash from sales, refunds, and fees",
                lhs_amount=lhs_amount,
                rhs_label=f"{processor.capitalize()} payouts",
                rhs_amount=rhs_amount,
                delta=delta,
                threshold=amount_threshold,
                notes=["Same-month settlement can drift when processor balances carry across month boundaries."],
                evidence_refs=[
                    make_artifact_ref(
                        normalized_path_display,
                        record_refs_list=record_refs(settlement_records),
                        notes=f"{processor.capitalize()} sales, refunds, and fee rows.",
                    ),
                    make_artifact_ref(
                        normalized_path_display,
                        record_refs_list=record_refs(payout_records),
                        notes=f"{processor.capitalize()} payout rows.",
                    ),
                ],
            )
        )

    return checks


def build_fulfillment_checks(
    *,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    amount_threshold: Decimal,
) -> list[dict[str, Any]]:
    observed_pairs = {
        (infer_fulfillment_partner(record), record_currency(record))
        for record in records.get("purchase_expenses", []) + records.get("bank_transactions", [])
        if infer_fulfillment_partner(record)
    }
    checks: list[dict[str, Any]] = []

    for partner, currency in sorted(observed_pairs):
        expense_records = [
            record
            for record in records.get("purchase_expenses", [])
            if infer_fulfillment_partner(record) == partner and record_currency(record) == currency
        ]
        bank_records = [
            record
            for record in records.get("bank_transactions", [])
            if infer_fulfillment_partner(record) == partner
            and record_currency(record) == currency
            and decimal_value(record.get("gross_amount")) < 0
        ]

        if not expense_records and not bank_records:
            continue

        lhs_amount = sum_abs_amount(expense_records, "gross_amount")
        rhs_amount = sum_abs_amount(bank_records, "gross_amount")
        delta = lhs_amount - rhs_amount

        if not expense_records or not bank_records:
            status = "warn"
        else:
            status = "pass" if abs(delta) <= amount_threshold else "warn"

        notes: list[str] = []
        if not expense_records:
            notes.append(f"Bank debits mention {partner} in {currency}, but no normalized purchase-expense rows were available.")
        if not bank_records:
            notes.append(f"{partner.capitalize()} expense rows exist in {currency}, but no matching bank debits were found.")
        if expense_records and bank_records:
            notes.append("Payment timing and accrued expenses can create benign same-month deltas.")

        checks.append(
            make_check(
                check_id=f"fulfillment-expenses-vs-bank:{partner}:{currency.lower()}",
                name=f"{partner.capitalize()} fulfillment totals vs bank debits ({currency})",
                status=status,
                lhs_label=f"{partner.capitalize()} normalized expense total ({currency})",
                lhs_amount=lhs_amount,
                rhs_label=f"Bank debits tagged {partner} ({currency})",
                rhs_amount=rhs_amount,
                delta=delta,
                threshold=amount_threshold,
                notes=notes,
                evidence_refs=[
                    make_artifact_ref(
                        normalized_path_display,
                        record_refs_list=record_refs(expense_records),
                        notes=f"{partner.capitalize()} purchase-expense rows.",
                    ),
                    make_artifact_ref(
                        normalized_path_display,
                        record_refs_list=record_refs(bank_records),
                        notes=f"Bank debits tagged as {partner}.",
                    ),
                ],
            )
        )
    return checks


def build_purchase_credit_checks(
    *,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    credits = records.get("purchase_credits", [])
    currencies = sorted({record_currency(record) for record in credits})
    checks: list[dict[str, Any]] = []

    for currency in currencies:
        currency_credits = [record for record in credits if record_currency(record) == currency]
        total = sum_amount(currency_credits, "gross_amount")
        checks.append(
            make_check(
                check_id=f"supplier-credits:{currency.lower()}",
                name=f"Supplier credits ({currency})",
                status="pass",
                lhs_label=f"Supplier credits ({currency})",
                lhs_amount=total,
                notes=["Supplier credits are preserved separately from purchase expenses for posting."],
                evidence_refs=[
                    make_artifact_ref(
                        normalized_path_display,
                        record_refs_list=record_refs(currency_credits),
                        notes="Normalized supplier-credit rows.",
                    )
                ],
            )
        )

    return checks


def build_inventory_check(
    *,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    inventory_expected: bool,
    quantity_threshold: Decimal,
) -> dict[str, Any]:
    sales_records = [record for record in records.get("sales", []) if record_quantity(record) != 0]
    inventory_records = [record for record in records.get("inventory_movements", []) if record_quantity(record) != 0]
    fulfillment_records = [
        record
        for record in records.get("purchase_expenses", [])
        if infer_fulfillment_partner(record) and record_quantity(record) != 0
    ]

    sales_quantity = sum(abs(record_quantity(record)) for record in sales_records)
    inventory_quantity = sum(abs(record_quantity(record)) for record in inventory_records)
    fulfillment_quantity = sum(abs(record_quantity(record)) for record in fulfillment_records)

    evidence = [
        make_artifact_ref(normalized_path_display, record_refs_list=record_refs(sales_records), notes="Sales rows with explicit quantity."),
        make_artifact_ref(
            normalized_path_display,
            record_refs_list=record_refs(inventory_records),
            notes="Inventory movement rows with explicit quantity.",
        ),
        make_artifact_ref(
            normalized_path_display,
            record_refs_list=record_refs(fulfillment_records),
            notes="Fulfillment expense rows with explicit quantity.",
        ),
    ]

    if not inventory_expected and sales_quantity == 0 and inventory_quantity == 0 and fulfillment_quantity == 0:
        return make_check(
            check_id="inventory-quantity-evidence",
            name="Inventory quantity evidence",
            status="skipped",
            threshold=quantity_threshold,
            notes=["No quantity-bearing inventory evidence was normalized, and inventory is not flagged as relevant."],
            evidence_refs=evidence,
        )

    if sales_quantity > 0 and inventory_quantity > 0:
        delta = sales_quantity - inventory_quantity
        status = "pass" if abs(delta) <= quantity_threshold else "fail"
        return make_check(
            check_id="inventory-quantity-evidence",
            name="Inventory quantity evidence",
            status=status,
            lhs_label="Sales quantity",
            lhs_amount=sales_quantity,
            rhs_label="Inventory movement quantity",
            rhs_amount=inventory_quantity,
            delta=delta,
            threshold=quantity_threshold,
            notes=["Uses explicit quantity fields only."],
            evidence_refs=evidence,
        )

    if sales_quantity > 0 and fulfillment_quantity > 0:
        delta = sales_quantity - fulfillment_quantity
        status = "pass" if abs(delta) <= quantity_threshold else "warn"
        return make_check(
            check_id="inventory-quantity-evidence",
            name="Inventory quantity evidence",
            status=status,
            lhs_label="Sales quantity",
            lhs_amount=sales_quantity,
            rhs_label="Fulfillment quantity",
            rhs_amount=fulfillment_quantity,
            delta=delta,
            threshold=quantity_threshold,
            notes=["Used fulfillment-side quantity evidence because no inventory movement rows were available."],
            evidence_refs=evidence,
        )

    if inventory_expected:
        return make_check(
            check_id="inventory-quantity-evidence",
            name="Inventory quantity evidence",
            status="warn",
            threshold=quantity_threshold,
            notes=["Inventory appears relevant from policy or entity mapping, but no comparable month-level quantity evidence was normalized."],
            evidence_refs=evidence,
        )

    return make_check(
        check_id="inventory-quantity-evidence",
        name="Inventory quantity evidence",
        status="skipped",
        threshold=quantity_threshold,
        notes=["Comparable quantity evidence was not available for this month."],
        evidence_refs=evidence,
    )


def build_continuity_check(
    *,
    normalized_path_display: str,
    current_payload: dict[str, Any],
    previous_payload: dict[str, Any] | None,
    previous_path_display: str | None,
) -> dict[str, Any]:
    if previous_payload is None or previous_path_display is None:
        return make_check(
            check_id="continuity-vs-previous-period",
            name="Continuity with previous period",
            status="skipped",
            notes=["No previous-period normalized artifact was available."],
        )

    current_systems = canonical_source_systems(current_payload) - {"document"}
    previous_systems = canonical_source_systems(previous_payload) - {"document"}
    missing_systems = sorted(previous_systems - current_systems)
    new_systems = sorted(current_systems - previous_systems)

    status = "warn" if missing_systems else "pass"
    notes: list[str] = []
    if missing_systems:
        notes.append(f"Source systems seen last month but not this month: {', '.join(missing_systems)}.")
    if new_systems:
        notes.append(f"New source systems this month: {', '.join(new_systems)}.")
    if not notes:
        notes.append("Canonical source-system coverage is consistent with the previous period.")

    return make_check(
        check_id="continuity-vs-previous-period",
        name="Continuity with previous period",
        status=status,
        notes=notes,
        evidence_refs=[
            make_artifact_ref(normalized_path_display, notes="Current normalized artifact."),
            make_artifact_ref(previous_path_display, notes="Previous normalized artifact."),
        ],
    )


def build_recon_document(
    *,
    normalized_payload: dict[str, Any],
    normalized_path: Path,
    repo_root: Path,
    amount_threshold: Decimal,
    quantity_threshold: Decimal,
    policy_memo_text: str | None = None,
    policy_memo_path: Path | None = None,
    entity_map: dict[str, Any] | None = None,
    entity_map_path: Path | None = None,
    previous_payload: dict[str, Any] | None = None,
    previous_path: Path | None = None,
    bank_allocations: dict[str, dict[str, Any]] | None = None,
    clearing_allocations: dict[str, dict[str, Any]] | None = None,
    annual_clearing_records: dict[str, dict[str, Any]] | None = None,
    bank_allocation_errors: list[str] | None = None,
    bank_allocations_path: Path | None = None,
) -> dict[str, Any]:
    normalized_path_display = display_path(normalized_path, repo_root)
    previous_path_display = display_path(previous_path, repo_root) if previous_path else None
    records = normalized_payload.get("records") or {}
    inventory_expected = inventory_is_relevant(policy_memo_text, entity_map)

    exceptions = import_normalized_exceptions(
        normalized_path_display=normalized_path_display,
        normalized_exceptions=normalized_payload.get("exceptions") or [],
    )
    exceptions.extend(
        missing_processor_evidence_exceptions(
            normalized_path_display=normalized_path_display,
            records=records,
            bank_allocations=bank_allocations,
            annual_clearing_records=annual_clearing_records,
        )
    )

    physical_bank_check, bank_coverage = build_physical_bank_coverage_check(
        normalized_path_display=normalized_path_display,
        target_period=str(normalized_payload["period"]),
        bank_records=records.get("bank_transactions", []),
        allocations=bank_allocations or {},
        bank_balance_records=records.get("bank_balances", []),
        allocation_errors=bank_allocation_errors,
    )
    clearing_checks, clearing_ready, clearing_coverage = build_clearing_continuity_checks(
        normalized_path_display=normalized_path_display,
        records=records,
        allocations=clearing_allocations if clearing_allocations is not None else (bank_allocations or {}),
        annual_clearing_records=annual_clearing_records,
    )
    bank_coverage["clearing_ready"] = clearing_ready
    bank_coverage.update(clearing_coverage)
    bank_coverage["coverage_ready"] = bool(bank_coverage["coverage_ready"] and clearing_ready)

    checks: list[dict[str, Any]] = [
        physical_bank_check,
        *clearing_checks,
        build_woo_sales_vs_processor_check(
            normalized_path_display=normalized_path_display,
            records=records,
            base_currency=str(normalized_payload.get("base_currency") or "EUR"),
            amount_threshold=amount_threshold,
        ),
        *build_processor_payout_checks(
            normalized_path_display=normalized_path_display,
            records=records,
            amount_threshold=amount_threshold,
        ),
        *build_processor_settlement_checks(
            normalized_path_display=normalized_path_display,
            records=records,
            amount_threshold=amount_threshold,
        ),
        *build_purchase_credit_checks(
            normalized_path_display=normalized_path_display,
            records=records,
        ),
        *build_fulfillment_checks(
            normalized_path_display=normalized_path_display,
            records=records,
            amount_threshold=amount_threshold,
        ),
        build_inventory_check(
            normalized_path_display=normalized_path_display,
            records=records,
            inventory_expected=inventory_expected,
            quantity_threshold=quantity_threshold,
        ),
        build_continuity_check(
            normalized_path_display=normalized_path_display,
            current_payload=normalized_payload,
            previous_payload=previous_payload,
            previous_path_display=previous_path_display,
        ),
    ]

    notes = [
        f"Amount threshold: {decimal_text(amount_threshold)} {normalized_payload.get('base_currency', 'EUR')}.",
        f"Quantity threshold: {decimal_text(quantity_threshold)}.",
    ]
    if policy_memo_path and policy_memo_text is not None:
        notes.append(f"Loaded policy memo from {display_path(policy_memo_path, repo_root)}.")
    else:
        notes.append("Policy memo not available; inventory expectations may be under-specified.")
    if entity_map_path and entity_map is not None:
        warehouse_count = len(entity_map.get("warehouses") or [])
        notes.append(f"Loaded entity map from {display_path(entity_map_path, repo_root)} with {warehouse_count} warehouse entries.")
    else:
        notes.append("Entity map not available; warehouse-sensitive checks rely only on normalized source data.")
    if previous_path_display:
        notes.append(f"Loaded previous normalized artifact from {previous_path_display}.")

    sorted_checks = sorted(checks, key=lambda item: item["check_id"])
    sorted_exceptions = sorted(exceptions, key=lambda item: item["exception_id"])
    blocking_issue_count = sum(1 for item in sorted_checks if item["status"] == "fail")
    blocking_issue_count += sum(1 for item in sorted_exceptions if item.get("blocking"))

    reference_artifacts = []
    if normalized_path.exists():
        reference_artifacts.append(bind_file(normalized_path, kind="normalized_period", cwd=repo_root))
    if bank_allocations_path is not None and bank_allocations_path.exists():
        reference_artifacts.append(bind_file(bank_allocations_path, kind="bank_allocations", cwd=repo_root))

    return {
        "schema_version": "1.0",
        "company_slug": normalized_payload["company_slug"],
        "period": normalized_payload["period"],
        "generated_at": utc_now_iso(),
        "currency": normalized_payload.get("base_currency"),
        "approve_for_build": blocking_issue_count == 0,
        "blocking_issue_count": blocking_issue_count,
        "bank_coverage": bank_coverage,
        "reference_artifacts": reference_artifacts,
        "checks": sorted_checks,
        "exceptions": sorted_exceptions,
        "notes": notes,
    }


def inferred_artifacts_dir(normalized_path: Path) -> Path | None:
    if normalized_path.parent.name == "normalized":
        return normalized_path.parent.parent
    return None


def resolve_normalized_path(*, company_dir: Path | None, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is None:
        raise SimplbooksError("Pass --normalized when --company-dir is not provided.")
    return company_dir / "artifacts" / "normalized" / f"{period}.json"


def resolve_policy_memo_path(*, company_dir: Path | None, normalized_path: Path, override: str | None) -> Path | None:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "policy_memo.md"
    artifacts_dir = inferred_artifacts_dir(normalized_path)
    if artifacts_dir is None:
        return None
    return artifacts_dir / "policy_memo.md"


def resolve_entity_map_path(*, company_dir: Path | None, normalized_path: Path, override: str | None) -> Path | None:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "entity_map.json"
    artifacts_dir = inferred_artifacts_dir(normalized_path)
    if artifacts_dir is None:
        return None
    return artifacts_dir / "entity_map.json"


def resolve_bank_allocations_path(
    *, company_dir: Path | None, normalized_path: Path, period: str, override: str | None
) -> Path | None:
    if override:
        return Path(override)
    year = period[:4]
    if company_dir is not None:
        return company_dir / "artifacts" / "bank" / f"{year}-allocations.json"
    artifacts_dir = inferred_artifacts_dir(normalized_path)
    if artifacts_dir is None:
        return None
    return artifacts_dir / "bank" / f"{year}-allocations.json"


def normalized_year_paths(normalized_path: Path, *, period: str) -> list[Path]:
    """Discover the annual normalized inputs that a reviewed allocation artifact binds."""
    year = period[:4]
    if normalized_path.parent.name != "normalized":
        return [normalized_path]
    paths = sorted(normalized_path.parent.glob(f"{year}-*.json"))
    return paths or [normalized_path]


def load_period_bank_allocations(
    *,
    allocation_path: Path | None,
    normalized_path: Path,
    period: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if allocation_path is None or not allocation_path.exists():
        return {}, ["Reviewed bank allocation artifact was not available for this period."]
    try:
        payload = load_bank_allocations(
            allocation_path,
            normalized_year_paths=normalized_year_paths(normalized_path, period=period),
        )
        return period_allocations(payload, period), []
    except BankAllocationError as exc:
        return {}, [f"Reviewed bank allocation artifact is not usable: {exc}"]


def load_annual_bank_allocations(
    *,
    allocation_path: Path | None,
    normalized_path: Path,
    period: str,
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], list[str]]:
    """Load all reviewed annual allocations for cross-period clearing bridges."""
    if allocation_path is None or not allocation_path.exists():
        return {}, ["Reviewed bank allocation artifact was not available for this period."]
    try:
        payload = load_bank_allocations(
            allocation_path,
            normalized_year_paths=normalized_year_paths(normalized_path, period=period),
        )
        return {allocation_key(item): item for item in payload.get("allocations") or []}, []
    except BankAllocationError as exc:
        return {}, [f"Reviewed bank allocation artifact is not usable: {exc}"]


def resolve_previous_normalized_path(
    *,
    company_dir: Path | None,
    normalized_path: Path,
    period: str,
    override: str | None,
) -> Path | None:
    if override:
        return Path(override)

    previous = previous_period(period)
    if company_dir is not None:
        candidate = company_dir / "artifacts" / "normalized" / f"{previous}.json"
        return candidate if candidate.exists() else None

    if normalized_path.parent.name == "normalized":
        candidate = normalized_path.parent / f"{previous}.json"
        return candidate if candidate.exists() else None
    return None


def resolve_output_path(*, company_dir: Path | None, normalized_path: Path, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "recon" / f"{period}.json"
    artifacts_dir = inferred_artifacts_dir(normalized_path)
    if artifacts_dir is not None:
        return artifacts_dir / "recon" / f"{period}.json"
    return normalized_path.with_name(f"{period}.recon.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile normalized month-level bookkeeping evidence")
    parser.add_argument("--company-dir", help="Company folder, e.g. companies/example")
    parser.add_argument("--period", required=True, help="Target month in YYYY-MM format")
    parser.add_argument("--normalized", help="Path to normalized JSON. Defaults to companies/<company>/artifacts/normalized/<period>.json")
    parser.add_argument("--policy-memo", help="Optional path to policy memo markdown")
    parser.add_argument("--entity-map", help="Optional path to entity map JSON")
    parser.add_argument(
        "--bank-allocations",
        help="Reviewed annual bank allocations. Defaults to artifacts/bank/<year>-allocations.json when available",
    )
    parser.add_argument("--previous-normalized", help="Optional previous-period normalized JSON")
    parser.add_argument("--output", help="Optional output path for recon JSON")
    parser.add_argument("--amount-threshold", default="0.50", help="Allowed absolute amount delta before a deterministic check fails")
    parser.add_argument("--quantity-threshold", default="1", help="Allowed absolute quantity delta before quantity checks fail")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    company_dir = Path(args.company_dir) if args.company_dir else None
    amount_threshold = decimal_value(args.amount_threshold)
    quantity_threshold = decimal_value(args.quantity_threshold)

    normalized_path = resolve_normalized_path(company_dir=company_dir, period=args.period, override=args.normalized)
    normalized_payload = load_json(normalized_path)
    if normalized_payload.get("period") != args.period:
        raise SimplbooksError(
            f"Normalized artifact period mismatch: expected {args.period}, got {normalized_payload.get('period')!r}"
        )

    policy_memo_path = resolve_policy_memo_path(
        company_dir=company_dir,
        normalized_path=normalized_path,
        override=args.policy_memo,
    )
    entity_map_path = resolve_entity_map_path(
        company_dir=company_dir,
        normalized_path=normalized_path,
        override=args.entity_map,
    )
    previous_path = resolve_previous_normalized_path(
        company_dir=company_dir,
        normalized_path=normalized_path,
        period=args.period,
        override=args.previous_normalized,
    )
    bank_allocations_path = resolve_bank_allocations_path(
        company_dir=company_dir,
        normalized_path=normalized_path,
        period=args.period,
        override=args.bank_allocations,
    )
    output_path = resolve_output_path(
        company_dir=company_dir,
        normalized_path=normalized_path,
        period=args.period,
        override=args.output,
    )

    policy_memo_text = load_optional_text(policy_memo_path)
    entity_map = load_optional_json(entity_map_path)
    previous_payload = load_optional_json(previous_path)
    annual_bank_allocations, bank_allocation_errors = load_annual_bank_allocations(
        allocation_path=bank_allocations_path,
        normalized_path=normalized_path,
        period=args.period,
    )
    period_bank_allocations = {
        key: item
        for key, item in annual_bank_allocations.items()
        if str(item.get("period") or "") == args.period
    }
    annual_clearing_records: dict[str, dict[str, Any]] = {}
    for annual_path in normalized_year_paths(normalized_path, period=args.period):
        annual_payload = load_json(annual_path)
        if str(annual_payload.get("company_slug") or "") != str(normalized_payload.get("company_slug") or ""):
            continue
        for item in (annual_payload.get("records") or {}).get("clearing_transactions") or []:
            if isinstance(item, dict) and str(item.get("record_id") or ""):
                annual_clearing_records[str(item["record_id"])] = item

    repo_root = Path.cwd()
    document = build_recon_document(
        normalized_payload=normalized_payload,
        normalized_path=normalized_path,
        repo_root=repo_root,
        amount_threshold=amount_threshold,
        quantity_threshold=quantity_threshold,
        policy_memo_text=policy_memo_text,
        policy_memo_path=policy_memo_path if policy_memo_text is not None else None,
        entity_map=entity_map,
        entity_map_path=entity_map_path if entity_map is not None else None,
        previous_payload=previous_payload,
        previous_path=previous_path if previous_payload is not None else None,
        bank_allocations=period_bank_allocations,
        clearing_allocations=annual_bank_allocations,
        annual_clearing_records=annual_clearing_records,
        bank_allocation_errors=bank_allocation_errors,
        bank_allocations_path=bank_allocations_path,
    )
    write_json(output_path, document)

    company_slug = normalized_payload.get("company_slug") or (company_dir.name if company_dir else normalized_path.stem)
    company_name = resolve_company_name(company_dir=args.company_dir) if args.company_dir else None
    company_name = company_name or company_slug
    status_counts = Counter(check["status"] for check in document["checks"])
    summary = {
        "company_name": company_name,
        "company_slug": company_slug,
        "period": args.period,
        "normalized": str(normalized_path),
        "output": str(output_path),
        "approve_for_build": document["approve_for_build"],
        "blocking_issue_count": document["blocking_issue_count"],
        "check_counts": dict(sorted(status_counts.items())),
        "exception_count": len(document["exceptions"]),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
