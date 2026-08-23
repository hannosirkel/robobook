#!/usr/bin/env python3
from __future__ import annotations  # noqa: EXE001, I001

import argparse
import copy
import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from bookbuilder import write_yaml
from bookchecker import (
    evaluate_action_batch,
    evaluate_bank_statement_completeness,
    evaluate_inventory_quantities,
    load_reviewed_allocation_index,
    load_bound_discovery_payloads,
    load_yaml,
    manual_financial_dependency_errors,
    resolve_action_sources,
)
from exchange_rates import ExchangeRateError, lookup_rate
from posting_policy import (
    PostingPolicyError,
    action_policy_errors,
    cash_posting_mode,
    load_posting_policy,
    prohibited_bank_cash_action,
)
from reference_artifacts import (
    ReferenceArtifactError,
    required_action_binding_kinds,
    validate_discovery,
    verify_file_binding,
)
from simplbooks_api import (
    SimplbooksClient,
    SimplbooksError,
    load_token,
    resolve_company_id,
    resolve_company_name,
    resolve_company_slug,
)


ALLOWED_ENDPOINTS = frozenset(
    {
        "invoices/create",
        "invoices/save",
        "purchases/create",
        "purchases/save",
        "incomings/create",
        "incomings/save",
        "payments/create",
        "payments/save",
    }
)

CHECK_RESULT_PATTERN = re.compile(r"^- Result: `(pass|fail)`$", re.MULTILINE)
CHECK_BATCH_ID_PATTERN = re.compile(r"^- Batch ID: `([^`]+)`$", re.MULTILINE)
CHECK_ACTION_SHA_PATTERN = re.compile(r"^- Action file SHA256: `([a-f0-9]{64})`$", re.MULTILINE)

LEGACY_ENDPOINT_MAP = {
    "invoices/save": "invoices/create",
    "purchases/save": "purchases/create",
    "incomings/save": "incomings/create",
    "payments/save": "payments/create",
}


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def display_path(path: Path, root_dir: Path) -> str:
    try:
        return str(path.relative_to(root_dir))
    except ValueError:
        return str(path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def inferred_artifacts_dir(path: Path) -> Path | None:
    if path.parent.name == "actions":
        return path.parent.parent
    return None


def resolve_action_directory(*, company_dir: Path | None, action_path: Path) -> Path | None:
    if company_dir is not None:
        return company_dir / "artifacts" / "actions"
    if action_path.parent.name == "actions":
        return action_path.parent
    artifacts_dir = inferred_artifacts_dir(action_path)
    if artifacts_dir is not None:
        return artifacts_dir / "actions"
    return None


def resolve_action_path(*, company_dir: Path | None, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is None:
        raise SimplbooksError("Pass --actions when --company-dir is not provided.")
    return company_dir / "artifacts" / "actions" / f"{period}.yaml"


def resolve_check_report_path(*, company_dir: Path | None, action_path: Path, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "actions" / f"{period}.check.md"
    return action_path.with_suffix(".check.md")


def resolve_output_path(*, company_dir: Path | None, action_path: Path, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "submissions" / f"{period}.json"
    artifacts_dir = inferred_artifacts_dir(action_path)
    if artifacts_dir is not None:
        return artifacts_dir / "submissions" / f"{period}.json"
    return action_path.with_suffix(".submission.json")


def resolve_api_request_log_path(
    *,
    company_dir: Path | None,
    period: str,
    output_path: Path,
    override: str | None,
) -> Path:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "submissions" / f"{period}.api.jsonl"
    return output_path.with_suffix(".api.jsonl")


def parse_check_result(report_text: str) -> str | None:
    match = CHECK_RESULT_PATTERN.search(report_text)
    return match.group(1) if match else None


def parse_check_report(report_text: str) -> dict[str, str | None]:
    result = parse_check_result(report_text)
    batch_match = CHECK_BATCH_ID_PATTERN.search(report_text)
    sha_match = CHECK_ACTION_SHA_PATTERN.search(report_text)
    return {
        "result": result,
        "batch_id": batch_match.group(1) if batch_match else None,
        "action_file_sha256": sha_match.group(1) if sha_match else None,
    }


def load_check_report(path: Path) -> dict[str, str | None]:
    if not path.exists():
        return {"result": None, "batch_id": None, "action_file_sha256": None}
    metadata = parse_check_report(path.read_text(encoding="utf-8"))
    if metadata["result"] is None:
        raise SimplbooksError(f"Could not parse check result from {path}.")
    return metadata


def action_id(action: dict[str, Any]) -> str:
    value = str(action.get("idempotency_key") or "").strip()
    if not value:
        raise SimplbooksError("Action is missing idempotency_key.")
    return value


def normalized_endpoint(endpoint: str) -> str:
    return endpoint.strip().lstrip("/")


def action_successfully_submitted(action: dict[str, Any]) -> bool:
    status = action.get("response_status")
    return isinstance(status, int) and 200 <= status < 300


def validate_action_shape(action: dict[str, Any]) -> None:
    if str(action.get("action_type") or "") == "manual_inventory_writeoff":
        raise SimplbooksError(
            "manual inventory write-off actions are UI-only and must not be submitted through Simplbooks API."
        )
    endpoint = normalized_endpoint(str(action.get("endpoint") or ""))
    if endpoint not in ALLOWED_ENDPOINTS:
        raise SimplbooksError(
            f"Action {action_id(action)} targets unsupported endpoint {endpoint!r}. "
            "Submit-capable work is limited to current document-save endpoints."
        )

    source_refs = action.get("source_refs") or []
    if not source_refs:
        raise SimplbooksError(f"Action {action_id(action)} has no source references.")

    try:
        json.dumps(action.get("payload"))
    except TypeError as exc:
        raise SimplbooksError(f"Action {action_id(action)} payload is not JSON serializable.") from exc


def api_id(value: Any, *, field_name: str, optional: bool = False) -> int | None:
    if value in (None, ""):
        if optional:
            return None
        raise SimplbooksError(f"{field_name} is required for live Simplbooks submission.")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if optional:
        return None
    raise SimplbooksError(f"{field_name} must be an integer-like Simplbooks ID, got {value!r}.")


def rounded_rate(gross_amount: Decimal, vat_amount: Decimal | None) -> float | None:
    if vat_amount in (None, Decimal("0")):  # noqa: FURB157
        return 0.0 if vat_amount == Decimal("0") else None  # noqa: FURB157
    taxable_base = gross_amount - vat_amount
    if taxable_base <= 0:
        return None
    return float((vat_amount / taxable_base * Decimal("100")).quantize(Decimal("0.01")))  # noqa: FURB157


def reviewed_allocated_rate(
    line: dict[str, Any], *, gross_amount: Decimal, vat_amount: Decimal | None
) -> float:
    evidence = line.get("vat_allocation_component_evidence")
    if not isinstance(evidence, list) or len(evidence) != 1:
        raise SimplbooksError(
            "Woo VAT API line must represent exactly one order component so reviewed rounding can be preserved."
        )
    if vat_amount is None:
        raise SimplbooksError("Woo VAT API line requires a reviewed vat_amount_hint.")
    item = evidence[0]
    if not isinstance(item, dict):
        raise SimplbooksError("Woo VAT API line has invalid per-order component evidence.")
    binding = line.get("vat_evidence_binding")
    allocation_ref = binding.get("allocation_ref") if isinstance(binding, dict) else None
    tax_source_refs = binding.get("tax_source_refs") if isinstance(binding, dict) else None
    valid_binding = (
        isinstance(allocation_ref, dict)
        and bool(str(allocation_ref.get("path") or "").strip())
        and bool(re.fullmatch(r"[a-f0-9]{64}", str(allocation_ref.get("sha256") or "")))
        and isinstance(tax_source_refs, list)
        and bool(tax_source_refs)
        and all(
            isinstance(source_ref, dict)
            and bool(str(source_ref.get("source_id") or "").strip())
            and bool(str(source_ref.get("path") or "").strip())
            and bool(re.fullmatch(r"[a-f0-9]{64}", str(source_ref.get("sha256") or "")))
            and isinstance(source_ref.get("row_refs"), list)
            and bool(source_ref.get("row_refs"))
            for source_ref in tax_source_refs
        )
    )
    if not valid_binding:
        raise SimplbooksError("Woo VAT API line lacks a usable allocation and tax-source evidence binding.")
    evidence_gross = abs(decimal_value(item.get("gross_amount")))
    evidence_vat = abs(decimal_value(item.get("vat_amount")))
    if evidence_gross != gross_amount or evidence_vat != vat_amount:
        raise SimplbooksError("Woo VAT API line does not match its reviewed per-order component evidence.")
    rate = decimal_value(line.get("vat_profile_rate"))
    if rate < 0:
        raise SimplbooksError("Woo VAT API line has an invalid reviewed VAT profile rate.")
    item_profile = item.get("vat_profile")
    if not isinstance(item_profile, dict):
        raise SimplbooksError("Woo VAT API line lacks usable VAT profile provenance.")
    component = str(line.get("vat_allocation_component") or "")
    vat_type_field = "shipping_vat_type_id" if component == "shipping" else "goods_vat_type_id"
    try:
        event_date = date.fromisoformat(str(item.get("event_date") or ""))
        item_start = date.fromisoformat(str(item_profile.get("start") or ""))
        item_end = (
            date.fromisoformat(str(item_profile["end"]))
            if item_profile.get("end") not in (None, "")
            else None
        )
        line_start_text, line_end_text = str(line.get("vat_profile_period") or "").split("/", 1)
        line_start = date.fromisoformat(line_start_text)
        line_end = None if line_end_text == "open" else date.fromisoformat(line_end_text)
    except (TypeError, ValueError) as exc:
        raise SimplbooksError("Woo VAT API line has invalid VAT profile dates.") from exc
    if (
        event_date < item_start
        or item_end is not None and event_date > item_end
        or event_date < line_start
        or line_end is not None and event_date > line_end
    ):
        raise SimplbooksError("Woo VAT API line event date is outside its reviewed VAT profile.")
    if (
        decimal_value(item_profile.get("rate")) != rate
        or str(item_profile.get(vat_type_field) or "") != str(line.get("suggested_vat_type_id") or "")
    ):
        raise SimplbooksError("Woo VAT API line VAT profile provenance does not match the reviewed line.")
    expected_vat = (gross_amount * rate / (Decimal("100") + rate)).quantize(  # noqa: FURB157
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if vat_amount != expected_vat:
        raise SimplbooksError(
            "Woo VAT API payload cannot preserve the approved per-order VAT total at the reviewed rate."
        )
    return float(rate)


def compact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def action_lookup(actions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {action_id(action): action for action in actions}


def dependency_inserted_id(
    lookup: dict[str, dict[str, Any]],
    dependency_key: str,
    *,
    field_name: str,
    allow_placeholder: bool = False,
) -> int:
    dependency = lookup.get(dependency_key)
    if dependency is None:
        raise SimplbooksError(
            f"Dependency {dependency_key!r} required for {field_name} is missing from the available action lookup."
        )
    inserted = dependency.get("inserted_id")
    if inserted in (None, ""):
        if allow_placeholder:
            return 0
        raise SimplbooksError(
            f"Dependency {dependency_key!r} has not produced an inserted_id yet, so {field_name} cannot be resolved."
        )
    resolved = api_id(inserted, field_name=field_name, optional=False)
    assert resolved is not None
    return resolved


def translate_invoice_payload(
    action: dict[str, Any],
    *,
    lookup: dict[str, dict[str, Any]],
    allow_unresolved_dependencies: bool = False,
) -> dict[str, Any]:
    payload = action.get("payload") or {}
    counterparty = payload.get("counterparty") or {}
    document_date = str(payload.get("document_date") or "")
    currency = str(payload.get("currency") or "EUR")
    document_type = str(payload.get("document_type") or "invoice")

    if not document_date:
        raise SimplbooksError(f"{action_id(action)} is missing document_date.")

    tasks = []
    for index, line in enumerate(payload.get("line_items") or [], start=1):
        gross_amount = abs(decimal_value(line.get("gross_amount")))
        if line.get("article_id_hint") not in (None, ""):
            proof_errors = inventory_quantity_proof_errors(action, line)
            if proof_errors:
                raise SimplbooksError(
                    f"{action_id(action)} line {index} inventory quantity proof is invalid: {proof_errors[0]}"
                )
        quantity = abs(decimal_value(line.get("quantity") if line.get("quantity") not in (None, "") else 1))
        if quantity <= 0:
            raise SimplbooksError(f"{action_id(action)} line {index} quantity must be positive.")
        vat_amount_hint = line.get("vat_amount_hint")
        vat_amount = None if vat_amount_hint in (None, "") else abs(decimal_value(vat_amount_hint))
        task = compact_dict(
            {
                "income_account_id": api_id(
                    line.get("suggested_income_account_id"),
                    field_name=f"{action_id(action)} line {index} income_account_id",
                ),
                "vat_type_id": api_id(
                    line.get("suggested_vat_type_id"),
                    field_name=f"{action_id(action)} line {index} vat_type_id",
                    optional=True,
                ),
                "warehouse_id": api_id(
                    line.get("warehouse_id_hint"),
                    field_name=f"{action_id(action)} line {index} warehouse_id",
                    optional=True,
                ),
                "article_id": api_id(
                    line.get("article_id_hint"),
                    field_name=f"{action_id(action)} line {index} article_id",
                    optional=True,
                ),
                "name": str(line.get("description") or f"Line {index}"),
                "unit": "summary",
                "amount": decimal_number(quantity),
                "price_per_unit": decimal_number(gross_amount / quantity),
                "vat": (
                    reviewed_allocated_rate(
                        line,
                        gross_amount=abs(gross_amount),
                        vat_amount=vat_amount,
                    )
                    if line.get("vat_allocation_component") not in (None, "")
                    else rounded_rate(abs(gross_amount), vat_amount)
                ),
                "discount": 0,
                "contents": str(line.get("description") or f"Line {index}"),
            }
        )
        tasks.append({"Task": task})
    if not tasks:
        raise SimplbooksError(f"{action_id(action)} has no invoice line_items to submit.")

    invoice = compact_dict(
        {
            "client_id": api_id(counterparty.get("contact_id"), field_name=f"{action_id(action)} client_id"),
            "created": document_date,
            "transaction_date": document_date,
            "currency_name": currency,
            "currency_rate": reviewed_currency_rate(payload),
            "row_sum_with_vat": True,
            "additional_info": str(counterparty.get("display_name_hint") or action_id(action)),
        }
    )
    if document_type == "credit_note":
        parent_invoice_key = next(
            (
                str(dep)
                for dep in action.get("depends_on") or []
                if lookup.get(str(dep), {}).get("action_type") == "create_invoice_summary"
            ),
            "",
        )
        if not parent_invoice_key:
            raise SimplbooksError(
                f"{action_id(action)} is a credit-note draft without a linked prior invoice action."
            )
        invoice["credit_invoice_for"] = dependency_inserted_id(
            lookup,
            parent_invoice_key,
            field_name=f"{action_id(action)} credit_invoice_for",
            allow_placeholder=allow_unresolved_dependencies,
        )
    return {
        "endpoint": "invoices/create",
        "payload": {
            "Invoice": invoice,
            "Tasks": tasks,
        },
    }


def inventory_quantity_action_contract_error(action: dict[str, Any], line: dict[str, Any]) -> str | None:
    proof = line.get("inventory_quantity_proof")
    scope = proof.get("scope") if isinstance(proof, dict) else None
    category = str((scope or {}).get("record_category") or "") if isinstance(scope, dict) else ""
    action_type = str(action.get("action_type") or "")
    document_type = str((action.get("payload") or {}).get("document_type") or "")
    line_role = str(line.get("line_role") or "")
    sales_role = line_role == "sales_revenue" or ("sales" in line_role and "product" in line_role)
    refund_role = line_role == "refund_revenue" or ("refund" in line_role and "product" in line_role)
    contracts = {
        "sales": action_type == "create_invoice_summary" and document_type == "invoice" and sales_role,
        "refunds": action_type == "create_credit_invoice_summary" and document_type == "credit_note" and refund_role,
        "bank_transactions": (
            action_type == "create_invoice_summary"
            and document_type == "invoice"
            and line_role == "direct_sale_revenue"
            and (scope or {}).get("kind") == "reviewed_direct_sale_allocation"
        ),
    }
    if category in contracts and not contracts[category]:
        return f"{category} inventory scope does not match the action contract"
    return None


def inventory_quantity_proof_errors(action: dict[str, Any], line: dict[str, Any]) -> list[str]:
    if "shipping" in str(line.get("line_role") or "").lower():
        return ["shipping line must not carry an inventory article"]
    proof = line.get("inventory_quantity_proof")
    proof_fields = {
        "status", "quantity", "scope", "scope_sha256", "contributor_count",
        "contributor_set_sha256", "contributors",
    }
    if not isinstance(proof, dict) or set(proof) != proof_fields:
        return ["exact proof object is required"]
    if proof.get("status") != "exact":
        return ["status must be exact"]
    try:
        line_quantity = decimal_value(line.get("quantity"))
        proof_quantity = decimal_value(proof.get("quantity"))
    except SimplbooksError as exc:
        return [str(exc)]
    if line_quantity <= 0 or proof_quantity != line_quantity:
        return ["positive line quantity must equal proof quantity"]
    contributors = proof.get("contributors")
    if not isinstance(contributors, list) or not contributors:
        return ["contributors are required"]
    total = Decimal("0")  # noqa: FURB157
    seen: set[str] = set()
    for contributor in contributors:
        if not isinstance(contributor, dict) or set(contributor) != {
            "record_id", "quantity", "quantity_source", "record_sha256"
        }:
            return ["contributor shape is invalid"]
        record_id = str(contributor.get("record_id") or "").strip()
        if not record_id:
            return ["contributor record_id is required"]
        if record_id in seen:
            return ["contributor record IDs must be unique"]
        seen.add(record_id)
        if contributor.get("quantity_source") not in {"normalized_record", "reviewed_allocation_target"}:
            return ["contributor quantity_source is invalid"]
        if not re.fullmatch(r"[a-f0-9]{64}", str(contributor.get("record_sha256") or "")):
            return ["contributor record SHA-256 is invalid"]
        amount = decimal_value(contributor.get("quantity"))
        if amount <= 0:
            return ["contributor quantity must be positive"]
        total += amount
    if total != line_quantity:
        return ["contributor quantities do not equal line quantity"]
    scope = proof.get("scope")
    if not isinstance(scope, dict):
        return ["semantic scope is required"]
    scope_kind = scope.get("kind")
    expected_scope_fields = (
        {"kind", "period", "record_category", "group_label", "currency", "tax_profile"}
        if scope_kind == "normalized_sales_group"
        else {"kind", "period", "record_category", "statement_id"}
        if scope_kind == "reviewed_direct_sale_allocation"
        else set()
    )
    if not expected_scope_fields or set(scope) != expected_scope_fields:
        return ["semantic scope shape is invalid"]
    if scope.get("record_category") not in {"sales", "refunds", "bank_transactions"}:
        return ["semantic scope record category is invalid"]
    canonical = lambda value: hashlib.sha256(  # noqa: E731, RUF100
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    if canonical(scope) != str(proof.get("scope_sha256") or ""):
        return ["semantic scope hash is invalid"]
    ordered = sorted(contributors, key=lambda item: (str(item["record_id"]), str(item["record_sha256"])))
    if contributors != ordered:
        return ["contributors must use canonical sorted order"]
    if int(proof.get("contributor_count") or -1) != len(ordered):
        return ["contributor count is invalid"]
    if canonical(ordered) != str(proof.get("contributor_set_sha256") or ""):
        return ["contributor set hash is invalid"]
    required_source = (
        "normalized_record" if scope_kind == "normalized_sales_group"
        else "reviewed_allocation_target"
    )
    if any(item.get("quantity_source") != required_source for item in contributors):
        return ["contributor quantity source does not match semantic scope"]
    contract_error = inventory_quantity_action_contract_error(action, line)
    if contract_error:
        return [contract_error]
    return []


def reviewed_currency_rate(payload: dict[str, Any], *, base_currency: str = "EUR") -> float:
    currency = str(payload.get("currency") or base_currency).strip().upper()
    if currency == base_currency.upper():
        return 1.0
    raw_rate = payload.get("currency_rate")
    if raw_rate in (None, ""):
        raise SimplbooksError(f"Foreign-currency action is missing a reviewed currency_rate for {currency}.")
    rate = decimal_value(raw_rate)
    if rate <= 0:
        raise SimplbooksError(f"Foreign-currency action has invalid reviewed currency_rate {raw_rate!r} for {currency}.")
    return decimal_number(rate)


def translate_purchase_payload(action: dict[str, Any], *, credit: bool = False) -> dict[str, Any]:
    payload = action.get("payload") or {}
    counterparty = payload.get("counterparty") or {}
    document_date = str(payload.get("document_date") or "")
    currency = str(payload.get("currency") or "EUR")

    rows = []
    for index, line in enumerate(payload.get("line_items") or [], start=1):
        gross_amount = decimal_value(line.get("gross_amount"))
        if credit:
            if gross_amount <= 0:
                raise SimplbooksError(f"{action_id(action)} credit line {index} must contain a positive reviewed magnitude.")
            if line.get("article_id_hint") not in (None, "") or line.get("warehouse_id_hint") not in (None, ""):
                raise SimplbooksError(
                    f"{action_id(action)} credit line {index} is inventory-linked and requires original stock-batch handling."
                )
        vat_amount_hint = line.get("vat_amount_hint")
        vat_amount = None if vat_amount_hint in (None, "") else abs(decimal_value(vat_amount_hint))
        posted_amount = -gross_amount if credit else gross_amount
        row = compact_dict(
            {
                "expense_account_id": api_id(
                    line.get("suggested_expense_account_id"),
                    field_name=f"{action_id(action)} line {index} expense_account_id",
                ),
                "vat_type_id": api_id(
                    line.get("suggested_vat_type_id"),
                    field_name=f"{action_id(action)} line {index} vat_type_id",
                    optional=True,
                ),
                "article_id": api_id(
                    line.get("article_id_hint"),
                    field_name=f"{action_id(action)} line {index} article_id",
                    optional=True,
                ),
                "name": str(line.get("description") or f"Line {index}"),
                "unit": "summary",
                "amount": 1,
                "sum": decimal_number(posted_amount),
                "vat": rounded_rate(gross_amount, vat_amount),
                "discount": 0,
            }
        )
        rows.append({"PurchaseRow": row})
    if not rows:
        raise SimplbooksError(f"{action_id(action)} has no purchase line_items to submit.")

    purchase = compact_dict(
        {
            "client_id": api_id(counterparty.get("contact_id"), field_name=f"{action_id(action)} client_id"),
            "created": document_date,
            "transaction_date": document_date,
            "currency_name": currency,
            "currency_rate": reviewed_currency_rate(payload),
            "row_sum_with_vat": True,
            "vat": (
                -abs(decimal_number(decimal_value(payload.get("totals", {}).get("vat_amount"))) or 0)
                if credit
                else payload.get("totals", {}).get("vat_amount")
            ),
            "comments": str(counterparty.get("display_name_hint") or action_id(action)),
        }
    )
    return {
        "endpoint": "purchases/create",
        "payload": {
            "Purchase": purchase,
            "PurchaseRows": rows,
        },
    }


def translate_cash_settlement_payload(
    action: dict[str, Any],
    *,
    lookup: dict[str, dict[str, Any]],
    allow_unresolved_dependencies: bool = False,
) -> dict[str, Any]:
    payload = action.get("payload") or {}
    counterparty = payload.get("counterparty") or {}
    document_date = str(payload.get("document_date") or "")
    currency = str(payload.get("currency") or "EUR")
    document_type = str(payload.get("document_type") or "")
    counterparty_hint = str(payload.get("counterparty_hint") or counterparty.get("display_name_hint") or action_id(action))

    common = {
        "income_account_id": api_id(payload.get("bank_account_id"), field_name=f"{action_id(action)} bank_account_id"),
        "currency_name": currency,
        "currency_rate": reviewed_currency_rate(payload),
        "client_id": api_id(counterparty.get("contact_id"), field_name=f"{action_id(action)} client_id"),
    }

    if document_type == "incoming":
        linked_invoice_id = str(payload.get("linked_invoice_id") or "").strip()
        linked_invoice_action = str(payload.get("linked_invoice_action") or "").strip()
        if linked_invoice_id and linked_invoice_action:
            raise SimplbooksError(
                f"{action_id(action)} cannot carry both linked_invoice_id and linked_invoice_action."
            )
        invoice_dependency = next(
            (
                str(dependency)
                for dependency in action.get("depends_on") or []
                if lookup.get(str(dependency), {}).get("action_type") in {"create_invoice_summary", "create_credit_invoice_summary"}
            ),
            None,
        )
        if linked_invoice_id and invoice_dependency:
            raise SimplbooksError(
                f"{action_id(action)} cannot carry both linked_invoice_id and generated invoice dependency."
            )
        translated = {
            "Incoming": compact_dict(
                {
                    **common,
                    "income_sum": payload.get("amount"),
                    "income_date": document_date,
                    "description": f"{counterparty_hint} incoming summary",
                }
            )
        }
        if linked_invoice_id:
            translated["invoice_id"] = api_id(
                linked_invoice_id,
                field_name=f"{action_id(action)} invoice_id",
            )
        elif linked_invoice_action:
            translated["invoice_id"] = dependency_inserted_id(
                lookup,
                linked_invoice_action,
                field_name=f"{action_id(action)} invoice_id",
                allow_placeholder=allow_unresolved_dependencies,
            )
        elif invoice_dependency:
            translated["invoice_id"] = dependency_inserted_id(
                lookup,
                invoice_dependency,
                field_name=f"{action_id(action)} invoice_id",
                allow_placeholder=allow_unresolved_dependencies,
            )
        return {
            "endpoint": "incomings/create",
            "payload": translated,
        }

    if document_type == "payment":
        linked_purchase_id = str(payload.get("linked_purchase_id") or "").strip()
        linked_purchase_action = str(payload.get("linked_purchase_action") or "")
        if linked_purchase_id and linked_purchase_action:
            raise SimplbooksError(
                f"{action_id(action)} cannot carry both linked_purchase_id and linked_purchase_action."
            )
        if not linked_purchase_action:
            linked_purchase_action = next((str(dep) for dep in action.get("depends_on") or []), "")
        if linked_purchase_id and linked_purchase_action:
            raise SimplbooksError(
                f"{action_id(action)} cannot carry both linked_purchase_id and generated purchase dependency."
            )
        translated = {
            "Payment": compact_dict(
                {
                    **common,
                    "payment_sum": payload.get("amount"),
                    "payment_date": document_date,
                    "description": f"{counterparty_hint} payment summary",
                }
            )
        }
        if linked_purchase_id:
            translated["purchase_id"] = api_id(
                linked_purchase_id,
                field_name=f"{action_id(action)} purchase_id",
            )
        elif linked_purchase_action:
            translated["purchase_id"] = dependency_inserted_id(
                lookup,
                linked_purchase_action,
                field_name=f"{action_id(action)} purchase_id",
                allow_placeholder=allow_unresolved_dependencies,
            )
        return {
            "endpoint": "payments/create",
            "payload": translated,
        }

    raise SimplbooksError(f"Unsupported cash-settlement document_type for {action_id(action)}: {document_type!r}")


def translate_action_for_api(
    action: dict[str, Any],
    *,
    lookup: dict[str, dict[str, Any]],
    allow_unresolved_dependencies: bool = False,
    exchange_rate_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if str(action.get("action_type") or "") == "manual_statement_import_financial_transaction":
        raise SimplbooksError(
            "manual statement-import financial transactions are UI-only and must not be translated into SimplBooks API calls."
        )
    if str(action.get("action_type") or "") == "manual_inventory_writeoff":
        raise SimplbooksError(
            "manual inventory write-off actions are UI-only and must not be translated for Simplbooks API submission."
        )
    payload = action.get("payload") or {}
    draft_schema = str(payload.get("draft_schema") or "")
    currency = str(payload.get("currency") or "EUR").upper()
    if currency != "EUR" and exchange_rate_cache is not None:
        requested_date = str(payload.get("currency_rate_requested_date") or payload.get("document_date") or "")
        document_date = str(payload.get("document_date") or "")
        if requested_date != document_date:
            raise SimplbooksError(f"{action_id(action)} reviewed rate requested date must equal document date.")
        try:
            resolution = lookup_rate(
                exchange_rate_cache,
                requested_date=date.fromisoformat(requested_date),
                base=currency,
                quote="EUR",
            )
        except (ExchangeRateError, ValueError) as exc:
            raise SimplbooksError(f"{action_id(action)} ECB cache validation failed: {exc}") from exc
        if (
            decimal_value(payload.get("currency_rate")) != resolution.rate
            or str(payload.get("currency_rate_effective_date") or "") != resolution.effective_date.isoformat()
            or str(payload.get("currency_rate_provider") or "") != resolution.provider
            or str(payload.get("currency_rate_source_url") or "") != resolution.source_url
        ):
            raise SimplbooksError(f"{action_id(action)} reviewed rate does not match the annual ECB cache.")

    if draft_schema == "invoice_summary_v1":
        return translate_invoice_payload(
            action,
            lookup=lookup,
            allow_unresolved_dependencies=allow_unresolved_dependencies,
        )
    if draft_schema == "purchase_summary_v1":
        return translate_purchase_payload(action)
    if draft_schema == "purchase_credit_summary_v1":
        return translate_purchase_payload(action, credit=True)
    if draft_schema == "cash_settlement_v1":
        return translate_cash_settlement_payload(
            action,
            lookup=lookup,
            allow_unresolved_dependencies=allow_unresolved_dependencies,
        )

    raise SimplbooksError(
        f"Action {action_id(action)} uses unsupported draft_schema {draft_schema!r} for live Simplbooks submission."
    )


def translate_action(action: dict[str, Any], *, lookup: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compatibility entry point for local action translation checks."""
    return translate_action_for_api(action, lookup=lookup)


def validate_run_preconditions(
    *,
    action_batch: dict[str, Any],
    action_path: Path,
    period: str,
    mode: str,
    confirm_write: bool,
    check_report: dict[str, str | None],
    check_path: Path,
) -> None:
    batch_period = str(action_batch.get("period") or "")
    if batch_period != period:
        raise SimplbooksError(f"Action batch period mismatch: expected {period}, got {batch_period!r}")

    if mode != "write":
        return

    if not confirm_write:
        raise SimplbooksError("Write mode requires --confirm-write.")

    batch_status = str(action_batch.get("approval_status") or "")
    if batch_status == "reversed":
        raise SimplbooksError("Reversed action batches must not be submitted again.")
    if batch_status not in {"approved", "submitted"}:
        raise SimplbooksError(
            f"Write mode requires an approved action batch; current approval_status is {batch_status!r}."
        )

    check_result = check_report.get("result")
    if check_result != "pass":
        if check_result is None:
            raise SimplbooksError(
                f"Write mode requires a passing check report. Expected to find Result: `pass` in {check_path}."
            )
        raise SimplbooksError(
            f"Write mode requires a passing check report, but {check_path} reports {check_result!r}."
        )

    report_batch_id = check_report.get("batch_id")
    if report_batch_id != str(action_batch.get("batch_id") or ""):
        raise SimplbooksError(
            f"Write mode requires a fresh check report for this batch. {check_path} batch_id is {report_batch_id!r}, "
            f"but the action batch is {action_batch.get('batch_id')!r}."
        )

    report_sha = check_report.get("action_file_sha256")
    if not report_sha or report_sha != file_sha256(action_path):
        raise SimplbooksError(
            f"Write mode requires a check report that matches the current action file contents. Re-run bookchecker for {action_path}."
        )


def stable_execution_order(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    index: dict[str, int] = {}
    reverse_edges: dict[str, list[str]] = defaultdict(list)
    remaining_deps: dict[str, int] = {}

    for position, action in enumerate(actions):
        key = action_id(action)
        if key in lookup:
            raise SimplbooksError(f"Duplicate action idempotency_key in batch: {key}")
        lookup[key] = action
        index[key] = position
        remaining_deps[key] = 0

    for action in actions:
        key = action_id(action)
        for dependency in action.get("depends_on") or []:
            dep_key = str(dependency)
            if dep_key not in lookup:
                raise SimplbooksError(f"Action {key} depends on missing action {dep_key!r}.")
            reverse_edges[dep_key].append(key)
            remaining_deps[key] += 1

    ready = sorted((key for key, count in remaining_deps.items() if count == 0), key=index.get)
    ordered: list[dict[str, Any]] = []

    while ready:
        current = ready.pop(0)
        ordered.append(lookup[current])
        released: list[str] = []
        for child in reverse_edges.get(current, []):
            remaining_deps[child] -= 1
            if remaining_deps[child] == 0:
                released.append(child)
        if released:
            ready.extend(sorted(released, key=index.get))
            ready.sort(key=index.get)

    if len(ordered) != len(actions):
        unresolved = sorted((key for key, count in remaining_deps.items() if count > 0), key=index.get)
        raise SimplbooksError(f"Action dependency cycle detected in batch: {', '.join(unresolved)}")

    return ordered


def find_value_by_key(payload: Any, target_key: str) -> str | int | None:
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key == target_key and isinstance(value, (str, int)) and str(value).strip():
                    return value
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            for value in reversed(current):
                if isinstance(value, (dict, list)):
                    stack.append(value)
    return None


def extract_inserted_id(response_body: dict[str, Any]) -> str | int | None:
    for key in ("inserted_id", "invoice_id", "purchase_id", "payment_id", "incoming_id", "id"):
        found = find_value_by_key(response_body, key)
        if found is not None:
            return found
    return None


def response_is_success(http_status: int, response_body: dict[str, Any]) -> bool:
    if http_status < 200 or http_status >= 300:
        return False
    if response_body.get("success") is False:
        return False
    status_text = str(response_body.get("status") or response_body.get("result") or "").strip().lower()
    if status_text in {"error", "errors", "fail", "failed"}:
        return False
    if response_body.get("errors") or response_body.get("error"):  # noqa: SIM103
        return False
    return True


def dry_run_response(
    action: dict[str, Any],
    *,
    translated_endpoint: str,
    translated_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "dry_run": True,
        "validated": True,
        "endpoint": translated_endpoint,
        "method": str(action.get("method") or ""),
        "idempotency_key": action_id(action),
        "payload_preview": copy.deepcopy(translated_payload),
    }


def api_calls_from_request_log(
    request_log: list[dict[str, Any]],
    *,
    period: str | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for entry in request_log:
        if not isinstance(entry, dict):
            continue
        call = {
            "action_idempotency_key": str(entry.get("action_idempotency_key") or ""),
            "method": str(entry.get("method") or "POST"),
            "endpoint": normalized_endpoint(str(entry.get("endpoint") or "")),
            "payload": copy.deepcopy(entry.get("payload") or {}),
        }
        if period is not None:
            call["period"] = period
        calls.append(call)
    return calls


def apply_action_result(action: dict[str, Any], entry: dict[str, Any], *, mode: str) -> None:
    if mode == "dry-run":
        status = action.get("response_status")
        if isinstance(status, int) and status != 0:
            return

    action["executed_at"] = entry["sent_at"]
    action["response_status"] = entry["http_status"]
    action["response_body"] = copy.deepcopy(entry["response_body"])
    action["inserted_id"] = entry["inserted_id"]


def build_rollback_plan(action_batch: dict[str, Any], ordered_actions: list[dict[str, Any]]) -> dict[str, Any]:
    reverse_deps: dict[str, list[str]] = defaultdict(list)
    order_index = {action_id(action): index for index, action in enumerate(ordered_actions)}

    for action in ordered_actions:
        key = action_id(action)
        for dependency in action.get("depends_on") or []:
            reverse_deps[str(dependency)].append(key)

    candidates: list[dict[str, Any]] = []
    for action in reversed(ordered_actions):
        if not action_successfully_submitted(action):
            continue

        key = action_id(action)
        inserted_id = action.get("inserted_id")
        notes = [
            "Automatic rollback is not implemented; confirm the appropriate reversal or delete operation in Simplbooks before changing live data.",
        ]
        if inserted_id in (None, ""):
            notes.append("No inserted_id was captured for this action; manual lookup in Simplbooks may be required.")
        else:
            notes.append(f"Use inserted_id {inserted_id} to locate the live document if manual reversal is required.")

        candidates.append(
            {
                "action_idempotency_key": key,
                "original_endpoint": normalized_endpoint(str(action.get("endpoint") or "")),
                "inserted_id": inserted_id,
                "suggested_method": None,
                "suggested_endpoint": None,
                "depends_on": sorted(reverse_deps.get(key, []), key=order_index.get),
                "notes": notes,
            }
        )

    if candidates:
        notes = [
            "Rollback support is manual-only in the current repository state.",
            "Reverse the batch in reverse dependency order and confirm the correct Simplbooks reversal path before changing live data.",
        ]
    else:
        notes = [
            "No successful write actions are recorded in this batch yet.",
            "There is currently nothing to reverse.",
        ]

    return {
        "supported": False,
        "notes": notes,
        "reversal_candidates": candidates,
    }


def load_existing_submission(
    *,
    output_path: Path,
    action_path: Path,
    batch_id: str,
    company_slug: str,
    period: str,
) -> dict[str, Any] | None:
    if not output_path.exists():
        return None

    existing = load_json(output_path)
    if str(existing.get("batch_id") or "") != batch_id:
        raise SimplbooksError(
            f"Existing submission log batch_id {existing.get('batch_id')!r} does not match current batch_id {batch_id!r}: {output_path}"
        )
    if str(existing.get("company_slug") or "") != company_slug:
        raise SimplbooksError(
            f"Existing submission log company_slug {existing.get('company_slug')!r} does not match current batch company_slug {company_slug!r}: {output_path}"
        )
    if str(existing.get("period") or "") != period:
        raise SimplbooksError(
            f"Existing submission log period {existing.get('period')!r} does not match current period {period!r}: {output_path}"
        )

    request_log = existing.get("request_log") or []
    if not isinstance(request_log, list):
        raise SimplbooksError(f"Existing submission log request_log must be a list: {output_path}")

    # Backfill older local logs created before per-entry mode existed.
    if existing.get("mode") in {"dry-run", "write"}:
        normalized_log = []
        for entry in request_log:
            if isinstance(entry, dict) and "mode" not in entry:
                patched = dict(entry)
                patched["mode"] = existing["mode"]
                normalized_log.append(patched)
            else:
                normalized_log.append(entry)
        existing["request_log"] = normalized_log

    successful_write_sha = str(existing.get("action_file_sha256") or "").strip()
    current_batch_status = str(load_yaml(action_path).get("approval_status") or "")
    freeze_required = existing.get("mode") == "write" and (
        bool(successful_write_sha) or current_batch_status == "submitted"
    )
    if freeze_required:  # noqa: SIM102
        if not successful_write_sha or successful_write_sha != file_sha256(action_path):
            raise SimplbooksError(
                "The submitted batch is immutable: its successful action-file SHA does not match "
                f"the current YAML at {action_path}."
            )

    return existing


def validate_predecessor_submission(*, action_path: Path, period: str) -> None:
    """Require the immediately preceding configured action batch to be immutable and successful."""
    action_dir = action_path.parent
    configured = sorted(
        path.stem
        for path in action_dir.glob("*.yaml")
        if re.fullmatch(r"\d{4}-\d{2}", path.stem)
    )
    if period not in configured:
        raise SimplbooksError(f"Current period {period} is absent from the configured action sequence.")
    position = configured.index(period)
    if position == 0:
        return
    predecessor = configured[position - 1]
    predecessor_action_path = action_dir / f"{predecessor}.yaml"
    submissions_dir = action_dir.parent / "submissions"
    predecessor_submission_path = submissions_dir / f"{predecessor}.json"
    if not predecessor_submission_path.exists():
        raise SimplbooksError(
            f"Write mode requires the previous month/configured period {predecessor} to have a successful submission."
        )
    predecessor_batch = load_yaml(predecessor_action_path)
    predecessor_submission = load_json(predecessor_submission_path)
    summary = predecessor_submission.get("summary") or {}
    valid = (
        str(predecessor_batch.get("approval_status") or "") == "submitted"
        and predecessor_submission.get("mode") == "write"
        and str(predecessor_submission.get("period") or "") == predecessor
        and str(predecessor_submission.get("batch_id") or "") == str(predecessor_batch.get("batch_id") or "")
        and str(predecessor_submission.get("action_file_sha256") or "") == file_sha256(predecessor_action_path)
        and int(summary.get("failed_actions") or 0) == 0
        and summary.get("stopped_on_failure") is False
    )
    if not valid:
        raise SimplbooksError(
            f"Write mode requires the previous month/configured period {predecessor} to be successfully submitted and immutable."
        )


def load_prior_action_lookup(
    *,
    company_dir: Path | None,
    action_path: Path,
    period: str,
) -> dict[str, dict[str, Any]]:
    action_dir = resolve_action_directory(company_dir=company_dir, action_path=action_path)
    if action_dir is None or not action_dir.exists():
        return {}

    lookup: dict[str, dict[str, Any]] = {}
    current_action_path = action_path.resolve()
    for path in sorted(action_dir.glob("*.yaml")):
        if path.resolve() == current_action_path:
            continue
        batch_period = path.stem
        if not re.fullmatch(r"\d{4}-\d{2}", batch_period):
            continue
        if batch_period >= period:
            continue
        batch = load_yaml(path)
        for action in batch.get("actions") or []:
            if not isinstance(action, dict):
                continue
            key = action_id(action)
            if key not in lookup:
                lookup[key] = copy.deepcopy(action)

    submissions_dir = (
        company_dir / "artifacts" / "submissions"
        if company_dir is not None
        else ((inferred_artifacts_dir(action_path) / "submissions") if inferred_artifacts_dir(action_path) else None)
    )
    if submissions_dir is None or not submissions_dir.exists():
        return lookup
    for path in sorted(submissions_dir.glob("*.json")):
        if not re.fullmatch(r"\d{4}-\d{2}", path.stem) or path.stem >= period:
            continue
        submission = load_json(path)
        for entry in submission.get("request_log") or []:
            if not isinstance(entry, dict) or entry.get("mode") != "write" or not entry.get("success"):
                continue
            inserted_id = entry.get("inserted_id")
            key = str(entry.get("action_idempotency_key") or "").strip()
            if key in lookup and inserted_id not in (None, ""):
                lookup[key]["inserted_id"] = inserted_id
    return lookup


def verify_submission_reference_artifacts(
    action_batch: dict[str, Any],
    *,
    cwd: Path,
    period: str,
    company_id: str,
) -> dict[str, list[Path]]:
    bindings = action_batch.get("reference_artifacts") or []
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for binding in bindings:
        if not isinstance(binding, dict):
            raise SimplbooksError("Action reference binding must be an object.")
        by_kind[str(binding.get("kind") or "")].append(binding)

    missing = [
        kind
        for kind in sorted(required_action_binding_kinds(action_batch))
        if not by_kind.get(kind)
    ]
    if missing:
        raise SimplbooksError(
            "Write mode action batch is not bound to required artifact(s): " + ", ".join(missing)
        )

    singleton_kinds = {
        "posting_policy",
        "normalized_period",
        "reconciliation",
        "exchange_rates",
        "bank_allocations",
        "woo_tax_allocation",
    }
    duplicated = [kind for kind in sorted(singleton_kinds) if len(by_kind.get(kind) or []) > 1]
    if duplicated:
        raise SimplbooksError(
            "Write mode action batch has duplicate singleton artifact binding(s): "
            + ", ".join(duplicated)
        )

    verified: dict[str, list[Path]] = defaultdict(list)
    for binding in bindings:
        kind = str(binding.get("kind") or "")
        try:
            bound_path = verify_file_binding(binding, cwd=cwd)
            verified[kind].append(bound_path)
            if kind == "discovery_overview":
                overview = load_json(bound_path)
                validate_discovery(
                    overview,
                    year=int(overview.get("year") or 0),
                    company_id=company_id,
                )
        except ReferenceArtifactError as exc:
            raise SimplbooksError(str(exc)) from exc
    return dict(verified)


def prove_no_prohibited_bank_cash(
    action_batch: dict[str, Any], posting_policy: dict[str, Any] | None
) -> None:
    """Refuse a statement-import batch that would move cash the import already moves.

    This runs before translation and before any client call, so a prohibited action
    cannot reach SimplBooks even partially.
    """
    declared = str(action_batch.get("cash_posting_mode") or "api")
    if posting_policy is None:
        if declared == "statement_import":
            raise SimplbooksError(
                "Batch declares statement-import mode but no posting policy is bound to prove "
                "which accounts the statement-import mode forbids."
            )
        return
    try:
        expected = cash_posting_mode(posting_policy)
    except PostingPolicyError as exc:
        raise SimplbooksError(str(exc)) from exc
    if declared != expected:
        raise SimplbooksError(
            f"Batch cash_posting_mode {declared!r} does not match the bound posting policy "
            f"mode {expected!r}; statement-import mode must be agreed by both."
        )
    for action in action_batch.get("actions") or []:
        if not isinstance(action, dict):
            continue
        try:
            prohibited = prohibited_bank_cash_action(action, posting_policy)
        except PostingPolicyError as exc:
            raise SimplbooksError(str(exc)) from exc
        if prohibited:
            raise SimplbooksError(
                f"Action {action_id(action)} posts bank cash in statement-import mode; the "
                "imported statement settles this account, so no API cash action is sent."
            )


def execute_batch(
    *,
    action_batch: dict[str, Any],
    mode: str,
    continue_on_error: bool = False,
    client: Any | None = None,
    existing_request_log: list[dict[str, Any]] | None = None,
    reference_lookup: dict[str, dict[str, Any]] | None = None,
    exchange_rate_cache: dict[str, Any] | None = None,
    posting_policy: dict[str, Any] | None = None,
    action_path: Path | None = None,
    cwd: Path | None = None,
    expected_company_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prove_no_prohibited_bank_cash(action_batch, posting_policy)
    inventory_actions = [
        action for action in action_batch.get("actions") or []
        if any(
            isinstance(line, dict) and line.get("article_id_hint") not in (None, "")
            for line in (action.get("payload") or {}).get("line_items") or []
        )
    ]
    if inventory_actions and (action_path is None or cwd is None):
        raise SimplbooksError(
            "Inventory article prevalidation requires the bound action/source context before any client call."
        )
    if inventory_actions and action_path is not None and cwd is not None:
        payload_cache: dict[Path, dict[str, Any]] = {}
        index_cache: dict[Path, dict[str, tuple[str, dict[str, Any]]]] = {}
        reviewed_allocations, allocation_findings = load_reviewed_allocation_index(action_batch, cwd=cwd)
        inventory_findings = list(allocation_findings)
        for action in inventory_actions:
            resolved_sources, source_findings = resolve_action_sources(
                action=action, action_path=action_path, cwd=cwd,
                payload_cache=payload_cache, index_cache=index_cache,
            )
            inventory_findings.extend(source_findings)
            inventory_findings.extend(evaluate_inventory_quantities(
                action=action, resolved_sources=resolved_sources,
                reviewed_allocations=reviewed_allocations,
            ))
        inventory_errors = [item for item in inventory_findings if item.get("severity") == "error"]
        if inventory_errors:
            raise SimplbooksError(
                "Inventory quantity source prevalidation failed before translation or API calls: "
                + str(inventory_errors[0].get("summary") or "unknown inventory proof error")
            )
    if action_path is not None and cwd is not None:
        coverage_errors = [
            item
            for item in evaluate_bank_statement_completeness(
                action_batch,
                action_path=action_path,
                cwd=cwd,
                posting_policy=posting_policy,
            )
            if item.get("severity") == "error"
        ]
        if coverage_errors:
            raise SimplbooksError(
                "Action batch bank statement completeness prevalidation failed before translation or API calls: "
                + str(coverage_errors[0].get("summary") or "unknown coverage error")
            )
    manual_dependencies = [
        dependency
        for dependency in action_batch.get("unresolved_dependencies") or []
        if isinstance(dependency, dict)
        and str(dependency.get("kind") or "") == "manual_statement_import_financial_transaction"
    ]
    discovery_payloads: list[dict[str, Any]] = []
    discovery_errors: list[str] = []
    if manual_dependencies and cwd is not None:
        discovery_payloads, discovery_errors = load_bound_discovery_payloads(
            action_batch, cwd=cwd, expected_company_id=expected_company_id,
        )
    for dependency in manual_dependencies:
        dependency_errors = manual_financial_dependency_errors(
            dependency, cwd=cwd, expected_company_id=expected_company_id,
            require_typed_context=True, discovery_payloads=discovery_payloads,
        )
        dependency_errors.extend(discovery_errors)
        if dependency_errors:
            raise SimplbooksError(
                "Action batch contains an invalid manual statement-import financial dependency "
                f"{dependency.get('record_id') or '<unknown>'}: {dependency_errors[0]}"
            )
        proof = dependency.get("statement_import_proof") or {}
        if dependency.get("blocking") is not False or proof.get("status") != "verified":
            raise SimplbooksError(
                "Action batch contains a pending manual statement-import financial dependency; "
                "no API action is translated or sent until live discovery/audit proves the import."
            )
    ordered_actions = stable_execution_order(list(action_batch.get("actions") or []))
    lookup = dict(reference_lookup or {})
    lookup.update(action_lookup(ordered_actions))
    appended_entries: list[dict[str, Any]] = []
    attempted_actions = 0
    successful_actions = 0
    failed_actions = 0
    stopped_on_failure = False

    has_foreign_actions = any(
        str((action.get("payload") or {}).get("currency") or "EUR").upper() != "EUR"
        for action in ordered_actions
    )
    if mode == "write" and has_foreign_actions and exchange_rate_cache is None:
        raise SimplbooksError("Write mode requires the annual ECB cache for foreign-currency actions.")

    pretranslated: dict[str, dict[str, Any]] = {}
    for action in ordered_actions:
        validate_action_shape(action)
        if posting_policy is not None:
            try:
                policy_errors = action_policy_errors(action, posting_policy)
            except PostingPolicyError as exc:
                policy_errors = [str(exc)]
            if policy_errors:
                raise SimplbooksError(f"{action_id(action)} posting-policy mismatch: {policy_errors[0]}")

        pretranslated[action_id(action)] = translate_action_for_api(
            action,
            lookup=lookup,
            allow_unresolved_dependencies=True,
            exchange_rate_cache=exchange_rate_cache,
        )

    for action in ordered_actions:
        if action_successfully_submitted(action):
            continue

        translated = (
            pretranslated[action_id(action)]
            if mode == "dry-run"
            else translate_action_for_api(
                action,
                lookup=lookup,
                allow_unresolved_dependencies=False,
                exchange_rate_cache=exchange_rate_cache,
            )
        )
        translated_endpoint = normalized_endpoint(str(translated.get("endpoint") or ""))
        translated_payload = copy.deepcopy(translated.get("payload") or {})

        sent_at = utc_now_iso()
        if mode == "dry-run":
            response_body = dry_run_response(
                action,
                translated_endpoint=translated_endpoint,
                translated_payload=translated_payload,
            )
            http_status = 0
            success = True
            inserted_id = None
            notes = ["Dry-run only: request shape validated locally; no Simplbooks mutation was attempted."]
        else:
            if client is None:
                raise SimplbooksError("Write mode requires a Simplbooks API client.")
            response_body = client.request(
                translated_endpoint,
                method=str(action.get("method") or "POST"),
                payload=translated_payload,
            )
            http_status = int(response_body.get("_http_status") or 0)
            success = response_is_success(http_status, response_body)
            inserted_id = extract_inserted_id(response_body)
            notes = []
            if success and inserted_id in (None, ""):
                notes.append("Request succeeded but no inserted_id was detected in the response body.")

        entry = {
            "mode": mode,
            "action_idempotency_key": action_id(action),
            "sent_at": sent_at,
            "method": str(action.get("method") or "POST"),
            "endpoint": translated_endpoint,
            "payload": translated_payload,
            "http_status": http_status,
            "response_body": copy.deepcopy(response_body),
            "inserted_id": inserted_id,
            "success": success,
            "stopped_batch": False,
            "notes": notes,
        }
        apply_action_result(action, entry, mode=mode)
        appended_entries.append(entry)
        attempted_actions += 1

        if success:
            successful_actions += 1
        else:
            failed_actions += 1
            if not continue_on_error:
                entry["stopped_batch"] = True
                stopped_on_failure = True
                break

    if mode == "write" and all(action_successfully_submitted(action) for action in ordered_actions):
        action_batch["approval_status"] = "submitted"

    request_log = list(existing_request_log or []) + appended_entries
    submission = {
        "schema_version": "1.0",
        "company_slug": str(action_batch.get("company_slug") or ""),
        "period": str(action_batch.get("period") or ""),
        "generated_at": utc_now_iso(),
        "batch_id": str(action_batch.get("batch_id") or ""),
        "mode": mode,
        "request_log": request_log,
        "rollback_plan": build_rollback_plan(action_batch, ordered_actions),
        "summary": {
            "attempted_actions": attempted_actions,
            "successful_actions": successful_actions,
            "failed_actions": failed_actions,
            "stopped_on_failure": stopped_on_failure,
        },
    }
    return action_batch, submission


def run_submission(
    *,
    period: str,
    company_dir: Path | None,
    company_id: str | None,
    action_override: str | None,
    check_override: str | None,
    output_override: str | None,
    request_log_override: str | None,
    token_file: str,
    mode: str,
    confirm_write: bool,
    continue_on_error: bool,
    cwd: Path,
    client: Any | None = None,
) -> dict[str, Any]:
    action_path = resolve_action_path(company_dir=company_dir, period=period, override=action_override)
    check_path = resolve_check_report_path(company_dir=company_dir, action_path=action_path, period=period, override=check_override)
    output_path = resolve_output_path(company_dir=company_dir, action_path=action_path, period=period, override=output_override)
    api_request_log_path = resolve_api_request_log_path(
        company_dir=company_dir,
        period=period,
        output_path=output_path,
        override=request_log_override,
    )

    action_batch = load_yaml(action_path)
    check_report = load_check_report(check_path)
    validate_run_preconditions(
        action_batch=action_batch,
        action_path=action_path,
        period=period,
        mode=mode,
        confirm_write=confirm_write,
        check_report=check_report,
        check_path=check_path,
    )
    verified_reference_paths: dict[str, list[Path]] = {}
    exchange_rate_cache: dict[str, Any] | None = None
    posting_policy: dict[str, Any] | None = None
    if mode == "write":
        if company_dir is None:
            raise SimplbooksError("Write mode requires --company-dir for bound reference verification.")
        resolved_company_id = resolve_company_id(company_id, company_dir=str(company_dir))
        verified_reference_paths = verify_submission_reference_artifacts(
            action_batch,
            cwd=cwd,
            period=period,
            company_id=resolved_company_id,
        )
        validate_predecessor_submission(action_path=action_path, period=period)
        recon_path = verified_reference_paths["reconciliation"][0]
        normalized_path = verified_reference_paths["normalized_period"][0]
        recon_payload = load_json(recon_path)
        normalized_payload = load_json(normalized_path)
        if str(recon_payload.get("period") or "") != period:
            raise SimplbooksError("Bound reconciliation period does not match the action batch period.")
        if str(normalized_payload.get("period") or "") != period:
            raise SimplbooksError("Bound normalized period does not match the action batch period.")
        if str(normalized_payload.get("company_slug") or "") != str(action_batch.get("company_slug") or ""):
            raise SimplbooksError("Bound normalized company_slug does not match the action batch.")
        try:
            posting_policy = load_posting_policy(verified_reference_paths["posting_policy"][0])
        except PostingPolicyError as exc:
            raise SimplbooksError(str(exc)) from exc
        if verified_reference_paths.get("exchange_rates"):
            exchange_rate_cache = load_json(verified_reference_paths["exchange_rates"][0])
        policy_memo_path = company_dir / "artifacts" / "policy_memo.md"
        policy_text = policy_memo_path.read_text(encoding="utf-8") if policy_memo_path.exists() else None
        evaluation = evaluate_action_batch(
            action_batch=action_batch,
            action_path=action_path,
            recon_payload=recon_payload,
            recon_path=recon_path,
            policy_text=policy_text,
            cwd=cwd,
            company_dir=company_dir,
            exchange_rate_cache=exchange_rate_cache,
            posting_policy=posting_policy,
            expected_company_id=resolved_company_id,
        )
        if evaluation["error_count"] or evaluation["warning_count"]:
            first_finding = (evaluation.get("findings") or [{}])[0]
            raise SimplbooksError(
                "Write mode full checker prevalidation failed before any API call: "
                + str(first_finding.get("summary") or "unknown checker finding")
            )

    company_slug = str(
        action_batch.get("company_slug")
        or (resolve_company_slug(company_dir=str(company_dir)) if company_dir is not None else "")
        or (company_dir.name if company_dir is not None else action_path.stem)
    )
    existing_submission = load_existing_submission(
        output_path=output_path,
        action_path=action_path,
        batch_id=str(action_batch.get("batch_id") or ""),
        company_slug=company_slug,
        period=period,
    )
    reference_lookup = load_prior_action_lookup(
        company_dir=company_dir,
        action_path=action_path,
        period=period,
    )
    if mode != "write":
        exchange_rate_cache_path = (
            company_dir / "artifacts" / "reference" / f"ecb-rates-{period[:4]}.json"
            if company_dir is not None
            else None
        )
        exchange_rate_cache = (
            load_json(exchange_rate_cache_path)
            if exchange_rate_cache_path is not None and exchange_rate_cache_path.exists()
            else None
        )
        posting_policy_path = company_dir / "artifacts" / "posting_policy.json" if company_dir is not None else None
        posting_policy = (
            load_posting_policy(posting_policy_path)
            if posting_policy_path is not None and posting_policy_path.exists()
            else None
        )

    if mode == "write" and client is None:
        resolved_company_id = resolve_company_id(company_id, company_dir=str(company_dir) if company_dir else None)
        token = load_token(token_file)
        client = SimplbooksClient(
            resolved_company_id,
            token,
            request_log_path=api_request_log_path,
        )

    updated_batch, submission = execute_batch(
        action_batch=action_batch,
        mode=mode,
        continue_on_error=continue_on_error,
        client=client,
        existing_request_log=(existing_submission or {}).get("request_log") or [],
        reference_lookup=reference_lookup,
        exchange_rate_cache=exchange_rate_cache,
        posting_policy=posting_policy,
        action_path=action_path,
        cwd=cwd,
        expected_company_id=resolved_company_id if mode == "write" else None,
    )
    prior_request_count = len((existing_submission or {}).get("request_log") or [])
    current_request_log = submission["request_log"][prior_request_count:]

    write_yaml(action_path, updated_batch)
    if mode == "write" and str(updated_batch.get("approval_status") or "") == "submitted":
        submission["action_file_sha256"] = file_sha256(action_path)
    write_json(output_path, submission)

    company_name = resolve_company_name(company_dir=str(company_dir)) if company_dir is not None else None
    company_name = company_name or company_slug
    return {
        "company_name": company_name,
        "company_slug": company_slug,
        "period": period,
        "actions": str(action_path),
        "check_report": str(check_path),
        "check_result": check_report.get("result"),
        "output": str(output_path),
        "api_request_log": str(api_request_log_path) if mode == "write" else None,
        "mode": mode,
        "approval_status": updated_batch.get("approval_status"),
        "attempted_actions": submission["summary"]["attempted_actions"],
        "successful_actions": submission["summary"]["successful_actions"],
        "failed_actions": submission["summary"]["failed_actions"],
        "stopped_on_failure": submission["summary"]["stopped_on_failure"],
        "rollback_candidates": len(submission["rollback_plan"]["reversal_candidates"]),
        "api_calls": api_calls_from_request_log(current_request_log, period=period),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute or dry-run an approved Simplbooks action batch")
    parser.add_argument("--company-dir", help="Company folder, e.g. companies/example")
    parser.add_argument("--company-id", help="Explicit Simplbooks company ID for write mode")
    parser.add_argument("--period", required=True, help="Target month in YYYY-MM format")
    parser.add_argument("--actions", help="Path to actions YAML. Defaults to companies/<company>/artifacts/actions/<period>.yaml")
    parser.add_argument("--check-report", help="Path to actions check report. Defaults to actions/<period>.check.md")
    parser.add_argument("--output", help="Optional output path for the submission log JSON")
    parser.add_argument("--request-log", help="Optional JSONL path for low-level Simplbooks request/response logging in write mode")
    parser.add_argument("--mode", choices=("dry-run", "write"), default="dry-run", help="Execution mode")
    parser.add_argument("--confirm-write", action="store_true", help="Required alongside --mode write")
    parser.add_argument("--continue-on-error", action="store_true", help="Attempt later actions after a failed request")
    parser.add_argument("--token-file", default=".apikey", help="API token file for write mode")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    company_dir = Path(args.company_dir) if args.company_dir else None
    summary = run_submission(
        period=args.period,
        company_dir=company_dir,
        company_id=args.company_id,
        action_override=args.actions,
        check_override=args.check_report,
        output_override=args.output,
        request_log_override=args.request_log,
        token_file=args.token_file,
        mode=args.mode,
        confirm_write=args.confirm_write,
        continue_on_error=args.continue_on_error,
        cwd=Path.cwd(),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
