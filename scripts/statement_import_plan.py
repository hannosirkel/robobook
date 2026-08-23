"""Build the canonical annual plan that assigns every physical statement row exactly once.

In statement-import mode the complete bank statement is imported in the Simplbooks UI
and the API creates documents only. This module is the bridge: it turns reviewed bank
allocations plus normalized physical rows into one deterministic instruction per row,
so no physical movement is left to judgement at import time and none is claimed twice.
"""

from __future__ import annotations  # noqa: I001

import argparse
import csv
import hashlib
import io
import json
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from bank_allocations import (
    BankAllocationError,
    allocation_key,
    bank_ledger_key,
    load_bank_allocations,
    statement_identity,
)
from exchange_rates import ExchangeRateError, lookup_rate
from posting_policy import (
    PostingPolicyError,
    cash_posting_mode,
    resolve_bank_financial_account,
    resolve_clearing_account,
    slugify,
    statement_import_policy,
)


SCHEMA_VERSION = "1.0"
CENT = Decimal("0.01")

MANUAL_FAMILY = {
    "bank_fee_payment": "bank_fee",
    "expense_reimbursement_payment": "reporting_person_reimbursement",
    "clearing_transfer": "processor_or_internal_transfer",
    "reviewed_split": "reviewed_split",
}

DOCUMENT_FAMILY = frozenset(
    {
        "generated_invoice_receipt",
        "existing_invoice_receipt",
        "direct_sale_receipt",
        "generated_purchase_payment",
        "existing_purchase_payment",
    }
)

CONTRA_ROLE = {
    "generated_invoice_receipt": "customer_receivable",
    "existing_invoice_receipt": "customer_receivable",
    "direct_sale_receipt": "customer_receivable",
    "generated_purchase_payment": "supplier_payable",
    "existing_purchase_payment": "supplier_payable",
    "bank_fee_payment": "bank_fees",
    "expense_reimbursement_payment": "reporting_person_payable",
}

DOCUMENT_KIND = {
    "generated_invoice_receipt": "invoice",
    "existing_invoice_receipt": "invoice",
    "direct_sale_receipt": "invoice",
    "generated_purchase_payment": "purchase",
    "existing_purchase_payment": "purchase",
}

UI_ACTION = {
    "bank_fee": "assign_general_ledger",
    "reporting_person_reimbursement": "assign_general_ledger",
    "processor_or_internal_transfer": "assign_general_ledger",
    "reviewed_split": "split_and_assign",
    "document_settlement": "match_document",
}

CSV_FIELDS = (
    "statement_id",
    "iban",
    "date",
    "currency",
    "signed_amount",
    "counterparty",
    "description",
    "disposition",
    "ui_action",
    "debit_account",
    "credit_account",
    "document_refs",
    "ecb_rate",
    "split_equation",
    "status",
)

TARGET_ID_FIELDS = ("simplbooks_id", "action_key", "idempotency_key", "action_id")


class StatementImportPlanError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _amount(value: Any, *, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StatementImportPlanError(f"{label} must be a decimal amount.") from exc
    if not number.is_finite() or number.quantize(CENT, rounding=ROUND_HALF_UP) != number:
        raise StatementImportPlanError(f"{label} must be exact to 0.01.")
    return number


def _money(value: Decimal) -> str:
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def _attribute(record: dict[str, Any], *names: str) -> str:
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        return ""
    for name in names:
        value = _text(attributes.get(name))
        if value:
            return value
    return ""


# --- inputs ----------------------------------------------------------------


def _source_binding(record: dict[str, Any], manifest: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Bind one row to the exact canonical source file and row it came from."""
    for source_ref in record.get("source_refs") or []:
        if not isinstance(source_ref, dict):
            continue
        entry = manifest.get(_text(source_ref.get("source_id")))
        if entry is None:
            continue
        return {
            "source_id": _text(entry.get("source_id")),
            "path": _text(entry.get("path")),
            "sha256": _text(entry.get("sha256")),
            "row_ref": _text(source_ref.get("row_ref")) or _text(record.get("record_id")),
        }
    raise StatementImportPlanError(
        f"Bank record {_text(record.get('record_id'))!r} has no source_ref bound to the period source manifest."
    )


def _physical_rows(payloads: list[dict[str, Any]], *, year: int) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Index every physical bank row of the year by its immutable `(id, IBAN, currency)` key."""
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for payload in payloads:
        period = _text(payload.get("period"))
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", period) or int(period[:4]) != year:
            raise StatementImportPlanError(f"Normalized payload {period!r} does not belong to plan year {year}.")
        manifest = {
            _text(entry.get("source_id")): entry
            for entry in payload.get("sources") or []
            if isinstance(entry, dict)
        }
        for record in (payload.get("records") or {}).get("bank_transactions") or []:
            if not isinstance(record, dict) or _text(record.get("source_system")) != "bank":
                continue
            try:
                iban, currency = bank_ledger_key(record)
                key = statement_identity(record), iban, currency
            except BankAllocationError as exc:
                raise StatementImportPlanError(str(exc)) from exc
            if key in indexed:
                raise StatementImportPlanError(f"Physical statement key occurs in more than one record: {key}")
            indexed[key] = {
                "record": record,
                "period": period,
                "base_currency": _text(payload.get("base_currency")).upper() or "EUR",
                "source": _source_binding(record, manifest),
            }
    return indexed


def _prove_coverage(
    physical: dict[tuple[str, str, str], dict[str, Any]], allocations: list[dict[str, Any]]
) -> None:
    keys = [allocation_key(item) for item in allocations]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    missing = sorted(set(physical) - set(keys))
    extra = sorted(set(keys) - set(physical))
    errors: list[str] = []
    if duplicates:
        errors.append("duplicate statement key(s): " + ", ".join(map(str, duplicates)))
    if missing:
        errors.append("missing statement key(s): " + ", ".join(map(str, missing)))
    if extra:
        errors.append("extra statement key(s): " + ", ".join(map(str, extra)))
    if errors:
        raise StatementImportPlanError("; ".join(errors))


# --- account direction -----------------------------------------------------


def _clearing_provider(target: dict[str, Any]) -> str:
    providers = {
        slugify(_text(evidence.get("provider")))
        for evidence in target.get("clearing_evidence") or []
        if isinstance(evidence, dict) and _text(evidence.get("provider"))
    }
    if len(providers) != 1:
        raise StatementImportPlanError(
            "A clearing transfer requires exactly one reviewed clearing provider, "
            f"found {sorted(providers) or 'none'}."
        )
    return providers.pop()


def _contra_account(disposition: str, target: dict[str, Any], policy: dict[str, Any]) -> tuple[str, str]:
    """Return the reviewed `(role, account ID)` facing the statement account."""
    if disposition == "clearing_transfer":
        try:
            return resolve_clearing_account(policy, provider=_clearing_provider(target))
        except PostingPolicyError as exc:
            raise StatementImportPlanError(str(exc)) from exc
    role = CONTRA_ROLE.get(disposition)
    if role is None:
        raise StatementImportPlanError(f"Unsupported statement disposition {disposition!r}.")
    accounts = statement_import_policy(policy)["financial_accounts"]
    return role, accounts[role]


def _document_refs(disposition: str, target: dict[str, Any], *, statement_id: str) -> list[dict[str, str]]:
    kind = DOCUMENT_KIND[disposition]
    if _text(target.get("document_type")) != kind:
        raise StatementImportPlanError(
            f"A {disposition} row requires a {kind} document target, got {_text(target.get('document_type'))!r}."
        )
    reference = {
        field: _text(target.get(field)) for field in TARGET_ID_FIELDS if _text(target.get(field))
    }
    if reference:
        return [{"document_type": kind, **reference}]
    if disposition == "direct_sale_receipt":
        # A direct sale creates exactly one invoice for exactly one physical receipt, so
        # the statement identity names the document without inventing an identifier for it.
        return [{"document_type": kind, "generated_for_statement_id": statement_id}]
    raise StatementImportPlanError(f"A {disposition} row requires an exact document target identifier.")


def _direction(signed: Decimal, *, bank: tuple[str, str], contra: tuple[str, str]) -> dict[str, dict[str, str]]:
    incoming = signed > 0
    debit, credit = (bank, contra) if incoming else (contra, bank)
    return {
        "financial_accounts": {"debit": debit[1], "credit": credit[1]},
        "financial_account_roles": {"debit": debit[0], "credit": credit[0]},
    }


# --- currency --------------------------------------------------------------


def _ecb_block(
    *, signed: Decimal, currency: str, base_currency: str, event_date: str, rate_bindings: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if currency == base_currency:
        return None
    requested = date.fromisoformat(event_date)
    for binding in rate_bindings:
        cache = binding.get("cache")
        if not isinstance(cache, dict):
            continue
        if _text(cache.get("base")).upper() != currency or _text(cache.get("quote")).upper() != base_currency:
            continue
        try:
            resolution = lookup_rate(cache, requested_date=requested, base=currency, quote=base_currency)
        except ExchangeRateError as exc:
            raise StatementImportPlanError(str(exc)) from exc
        return {
            "rate": str(resolution.rate),
            "effective_date": resolution.effective_date.isoformat(),
            "base": resolution.base,
            "quote": resolution.quote,
            "converted_amount": _money(signed * resolution.rate),
            "binding": {"path": _text(binding.get("path")), "sha256": _text(binding.get("sha256"))},
        }
    raise StatementImportPlanError(
        f"No bound ECB rate cache covers {currency}/{base_currency} on {event_date}."
    )


# --- rows ------------------------------------------------------------------


def _part_rows(
    allocation: dict[str, Any], *, bank: tuple[str, str], policy: dict[str, Any]
) -> list[dict[str, Any]]:
    statement_id = allocation_key(allocation)[0]
    parts = allocation.get("parts")
    if not isinstance(parts, list) or not parts:
        raise StatementImportPlanError("A reviewed split requires at least one part.")
    resolved: list[dict[str, Any]] = []
    for index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise StatementImportPlanError("Reviewed split parts must be objects.")
        disposition = _text(part.get("disposition"))
        target = part.get("target")
        if not disposition or not isinstance(target, dict):
            raise StatementImportPlanError("Each reviewed split part requires an exact disposition and target.")
        amount = _amount(part.get("amount"), label=f"Reviewed split part {index} amount")
        contra = _contra_account(disposition, target, policy)
        resolved.append(
            {
                "part_number": index,
                "signed_amount": _money(amount),
                "disposition": disposition,
                "family": MANUAL_FAMILY.get(disposition, "document_settlement"),
                **_direction(amount, bank=bank, contra=contra),
                "document_refs": (
                    _document_refs(disposition, target, statement_id=statement_id)
                    if disposition in DOCUMENT_FAMILY
                    else []
                ),
            }
        )
    return resolved


def _split_equation(total: Decimal, parts: list[dict[str, Any]]) -> str:
    return f"{_money(total)} = " + " + ".join(part["signed_amount"] for part in parts)


def _plan_row(
    *,
    allocation: dict[str, Any],
    physical: dict[str, Any],
    policy: dict[str, Any],
    rate_bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    record = physical["record"]
    statement_id, iban, currency = allocation_key(allocation)
    disposition = _text(allocation.get("disposition"))
    signed = _amount(allocation.get("amount"), label=f"Statement row {statement_id} amount")
    target = allocation.get("target")
    if not isinstance(target, dict):
        raise StatementImportPlanError(f"Statement row {statement_id} requires a reviewed target object.")

    try:
        bank_account_id = resolve_bank_financial_account(policy, iban=iban, currency=currency)
    except PostingPolicyError as exc:
        raise StatementImportPlanError(str(exc)) from exc
    bank = ("bank", bank_account_id)

    is_split = disposition == "reviewed_split"
    family = MANUAL_FAMILY.get(disposition, "document_settlement")
    parts = _part_rows(allocation, bank=bank, policy=policy) if is_split else []
    if is_split:
        direction: dict[str, Any] = {"financial_accounts": {}, "financial_account_roles": {}}
        document_refs: list[dict[str, str]] = []
    else:
        direction = _direction(signed, bank=bank, contra=_contra_account(disposition, target, policy))
        document_refs = (
            _document_refs(disposition, target, statement_id=statement_id)
            if disposition in DOCUMENT_FAMILY
            else []
        )

    return {
        "statement_id": statement_id,
        "record_id": _text(allocation.get("record_id")),
        "iban": iban,
        "currency": currency,
        "period": _text(allocation.get("period")),
        "date": _text(record.get("event_date")),
        "signed_amount": _money(signed),
        "counterparty": _attribute(record, "counterparty_name", "counterparty", "beneficiary_name"),
        "description": " ".join(_text(record.get("description")).split()),
        "disposition": disposition,
        "family": family,
        "ui_action": UI_ACTION[family],
        **direction,
        "document_refs": document_refs,
        "ecb": _ecb_block(
            signed=signed,
            currency=currency,
            base_currency=physical["base_currency"],
            event_date=_text(record.get("event_date")),
            rate_bindings=rate_bindings,
        ),
        "parts": parts,
        "split_equation": _split_equation(signed, parts) if is_split else "",
        "source": physical["source"],
        "status": "pending",
        "evidence": None,
    }


def _coverage(rows: list[dict[str, Any]], *, physical_count: int) -> dict[str, Any]:
    families: dict[str, int] = {}
    movement: dict[str, Decimal] = {}
    for row in rows:
        families[row["family"]] = families.get(row["family"], 0) + 1
        ledger = f"{row['iban']}|{row['currency']}"
        movement[ledger] = movement.get(ledger, Decimal(0)) + Decimal(row["signed_amount"])
    return {
        "physical_row_count": physical_count,
        "planned_row_count": len(rows),
        "uncovered_count": max(physical_count - len(rows), 0),
        "extra_count": max(len(rows) - physical_count, 0),
        "families": dict(sorted(families.items())),
        "movement": {ledger: _money(total) for ledger, total in sorted(movement.items())},
    }


def build_statement_import_plan(
    *,
    year: int,
    normalized_payloads: list[dict[str, Any]],
    allocation_payload: dict[str, Any],
    policy: dict[str, Any],
    rate_bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive one reviewed instruction per physical statement row, or refuse to plan at all.

    The company is taken from the reviewed allocations rather than passed alongside them,
    so a plan cannot be built under a slug its own evidence does not carry.
    """
    if cash_posting_mode(policy) != "statement_import":
        raise StatementImportPlanError("A statement-import plan requires a statement_import posting policy.")
    company_slug = _text(allocation_payload.get("company_slug"))
    if not company_slug:
        raise StatementImportPlanError("Reviewed allocations must name the company they belong to.")
    allocations = [item for item in allocation_payload.get("allocations") or [] if isinstance(item, dict)]
    physical = _physical_rows(normalized_payloads, year=year)
    _prove_coverage(physical, allocations)

    rows = [
        _plan_row(
            allocation=allocation,
            physical=physical[allocation_key(allocation)],
            policy=policy,
            rate_bindings=rate_bindings,
        )
        for allocation in sorted(allocations, key=lambda item: (item.get("period"), *allocation_key(item)))
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "company_slug": company_slug,
        "year": year,
        "cash_posting_mode": "statement_import",
        "rate_bindings": [
            {"path": _text(binding.get("path")), "sha256": _text(binding.get("sha256"))}
            for binding in rate_bindings
        ],
        "coverage": _coverage(rows, physical_count=len(physical)),
        "rows": rows,
    }


# --- validation ------------------------------------------------------------


def _validate_row(row: dict[str, Any], *, index: int) -> None:
    label = f"Statement plan row {index} ({_text(row.get('statement_id'))})"
    signed = _amount(row.get("signed_amount"), label=f"{label} signed_amount")
    parts = row.get("parts") or []
    if row.get("family") == "reviewed_split":
        if not parts:
            raise StatementImportPlanError(f"{label} is a reviewed split with no parts.")
        total = sum(
            (_amount(part.get("signed_amount"), label=f"{label} split part") for part in parts), Decimal(0)
        )
        if total != signed:
            raise StatementImportPlanError(
                f"{label} split parts sum to {_money(total)}, not the statement amount {_money(signed)}."
            )
        return
    if parts:
        raise StatementImportPlanError(f"{label} carries split parts without a reviewed_split family.")
    accounts = row.get("financial_accounts") or {}
    if not _text(accounts.get("debit")) or not _text(accounts.get("credit")):
        raise StatementImportPlanError(f"{label} has no exact debit and credit account.")
    if _text(accounts.get("debit")) == _text(accounts.get("credit")):
        raise StatementImportPlanError(f"{label} debits and credits the same account.")
    if row.get("family") == "document_settlement" and not row.get("document_refs"):
        raise StatementImportPlanError(f"{label} settles a document but names no document target.")


def validate_statement_import_plan(plan: dict[str, Any]) -> None:
    """Reject any plan that does not assign every physical row exactly once and exactly."""
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise StatementImportPlanError(f"Statement plan schema_version must be {SCHEMA_VERSION!r}.")
    if plan.get("cash_posting_mode") != "statement_import":
        raise StatementImportPlanError("Statement plan cash_posting_mode must be 'statement_import'.")
    rows = plan.get("rows")
    if not isinstance(rows, list):
        raise StatementImportPlanError("Statement plan rows must be a list.")

    keys = [(_text(row.get("statement_id")), _text(row.get("iban")), _text(row.get("currency"))) for row in rows]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise StatementImportPlanError("Statement plan has duplicate statement key(s): " + ", ".join(map(str, duplicates)))

    coverage = plan.get("coverage") or {}
    if int(coverage.get("uncovered_count") or 0) or int(coverage.get("extra_count") or 0):
        raise StatementImportPlanError(
            f"Statement plan coverage is incomplete: {coverage.get('uncovered_count')} uncovered, "
            f"{coverage.get('extra_count')} extra."
        )
    if int(coverage.get("planned_row_count") or 0) != len(rows):
        raise StatementImportPlanError("Statement plan coverage count does not match the plan rows.")

    for index, row in enumerate(rows, start=1):
        _validate_row(row, index=index)


# --- rendering -------------------------------------------------------------


def _document_ref_text(row: dict[str, Any]) -> str:
    return "; ".join(
        f"{_text(reference.get('document_type'))} "
        + (
            _text(reference.get("simplbooks_id"))
            or _text(reference.get("action_key"))
            or _text(reference.get("idempotency_key"))
            or _text(reference.get("action_id"))
        )
        for reference in row.get("document_refs") or []
    )


def render_csv(plan: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in plan.get("rows") or []:
        accounts = row.get("financial_accounts") or {}
        writer.writerow(
            {
                "statement_id": row.get("statement_id"),
                "iban": row.get("iban"),
                "date": row.get("date"),
                "currency": row.get("currency"),
                "signed_amount": row.get("signed_amount"),
                "counterparty": row.get("counterparty"),
                "description": row.get("description"),
                "disposition": row.get("disposition"),
                "ui_action": row.get("ui_action"),
                "debit_account": accounts.get("debit", ""),
                "credit_account": accounts.get("credit", ""),
                "document_refs": _document_ref_text(row),
                "ecb_rate": (row.get("ecb") or {}).get("rate", ""),
                "split_equation": row.get("split_equation", ""),
                "status": row.get("status"),
            }
        )
    return buffer.getvalue()


def _markdown_instruction(row: dict[str, Any]) -> str:
    if row.get("family") == "reviewed_split":
        parts = "; ".join(
            f"part {part['part_number']} {part['signed_amount']} "
            f"debit {part['financial_accounts']['debit']} credit {part['financial_accounts']['credit']}"
            for part in row.get("parts") or []
        )
        return f"split `{row.get('split_equation')}` — {parts}"
    if row.get("family") == "document_settlement":
        return f"match {_document_ref_text(row)}"
    accounts = row.get("financial_accounts") or {}
    return f"debit {accounts.get('debit')} credit {accounts.get('credit')}"


def render_markdown(plan: dict[str, Any]) -> str:
    coverage = plan.get("coverage") or {}
    lines = [
        f"# Statement Import Plan {plan.get('year')}",
        "",
        f"Company: `{plan.get('company_slug')}`",
        (
            f"Physical rows: {coverage.get('physical_row_count')}; planned: {coverage.get('planned_row_count')}; "
            f"uncovered: {coverage.get('uncovered_count')}; extra: {coverage.get('extra_count')}"
        ),
        "",
        "## Annual movement",
        "",
    ]
    lines += [f"- `{ledger}`: {total}" for ledger, total in (coverage.get("movement") or {}).items()]
    lines += ["", "## Rows", ""]
    for row in plan.get("rows") or []:
        rate = (row.get("ecb") or {}).get("rate")
        suffix = f" (ECB {rate})" if rate else ""
        lines.append(
            f"- [ ] `{row.get('statement_id')}` {row.get('date')} {row.get('signed_amount')} "
            f"{row.get('currency')}{suffix} — {row.get('ui_action')}: {_markdown_instruction(row)}"
        )
    return "\n".join(lines) + "\n"


# --- artifacts -------------------------------------------------------------


def _write_if_changed(path: Path, data: bytes) -> str:
    if not path.exists() or path.read_bytes() != data:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
    return hashlib.sha256(data).hexdigest()


def write_plan_artifacts(plan: dict[str, Any], *, output_dir: Path) -> dict[str, Any]:
    """Write the JSON plan, CSV, and Markdown checklist, reporting exact paths and hashes."""
    validate_statement_import_plan(plan)
    output_dir.mkdir(parents=True, exist_ok=True)
    year = plan.get("year")
    payloads = {
        "plan_json": (output_dir / f"{year}-plan.json", json.dumps(plan, indent=2, sort_keys=False) + "\n"),
        "plan_csv": (output_dir / f"{year}-plan.csv", render_csv(plan)),
        "plan_markdown": (output_dir / f"{year}-plan.md", render_markdown(plan)),
    }
    artifacts = {
        name: {"path": str(path), "sha256": _write_if_changed(path, text.encode("utf-8"))}
        for name, (path, text) in payloads.items()
    }
    return {"artifacts": artifacts, "coverage": plan.get("coverage"), "year": year}


# --- CLI -------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatementImportPlanError(f"Unable to read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StatementImportPlanError(f"{path} must contain a JSON object.")
    return payload


def _rate_bindings(paths: list[Path]) -> list[dict[str, Any]]:
    return [
        {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "cache": _load_json(path),
        }
        for path in paths
    ]


def resolve_rate_paths(
    *, company_dir: Path, year: int, override: list[Path] | None
) -> list[Path]:
    """Find the year's ECB rate caches, so a caller never has to name them to be correct."""
    if override:
        return sorted(override)
    return sorted((company_dir / "artifacts" / "reference").glob(f"ecb-rates-{year}*.json"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the canonical annual statement-import plan.")
    parser.add_argument("--company-dir", required=True, type=Path)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--normalized", nargs="*", type=Path, default=None)
    parser.add_argument("--allocations", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--rates", nargs="*", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    company_dir: Path = args.company_dir
    normalized_paths = sorted(
        args.normalized
        if args.normalized is not None
        else (company_dir / "artifacts" / "normalized").glob(f"{args.year}-*.json")
    )
    allocations_path = args.allocations or company_dir / "artifacts" / "bank" / f"{args.year}-allocations.json"
    policy_path = args.policy or company_dir / "artifacts" / "posting_policy.json"
    output_dir = args.output_dir or company_dir / "artifacts" / "statement-import"

    try:
        allocation_payload = load_bank_allocations(allocations_path, normalized_year_paths=normalized_paths)
        plan = build_statement_import_plan(
            year=args.year,
            normalized_payloads=[_load_json(path) for path in normalized_paths],
            allocation_payload=allocation_payload,
            policy=_load_json(policy_path),
            rate_bindings=_rate_bindings(
                resolve_rate_paths(company_dir=company_dir, year=args.year, override=args.rates)
            ),
        )
        result = write_plan_artifacts(plan, output_dir=output_dir)
    except (BankAllocationError, StatementImportPlanError, PostingPolicyError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"status": "ok", **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
