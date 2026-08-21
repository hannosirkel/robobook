#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bookbuilder import write_yaml
from bookchecker import load_yaml
from exchange_rates import ExchangeRateError, lookup_rate
from posting_policy import PostingPolicyError, action_policy_errors, load_posting_policy
from reference_artifacts import ReferenceArtifactError, validate_discovery, verify_file_binding
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
        return Decimal("0")
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
    if vat_amount in (None, Decimal("0")):
        return 0.0 if vat_amount == Decimal("0") else None
    taxable_base = gross_amount - vat_amount
    if taxable_base <= 0:
        return None
    return float((vat_amount / taxable_base * Decimal("100")).quantize(Decimal("0.01")))


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
                "name": str(line.get("description") or f"Line {index}"),
                "unit": "summary",
                "amount": 1,
                "price_per_unit": decimal_number(gross_amount),
                "vat": rounded_rate(abs(gross_amount), vat_amount),
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
        invoice_dependency = next(
            (
                str(dependency)
                for dependency in action.get("depends_on") or []
                if lookup.get(str(dependency), {}).get("action_type") in {"create_invoice_summary", "create_credit_invoice_summary"}
            ),
            None,
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
        if invoice_dependency:
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
        linked_purchase_action = str(payload.get("linked_purchase_action") or "")
        if not linked_purchase_action:
            linked_purchase_action = next((str(dep) for dep in action.get("depends_on") or []), "")
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
        if linked_purchase_action:
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
    if response_body.get("errors") or response_body.get("error"):
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

    return existing


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
                lookup[key] = action
    return lookup


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
) -> tuple[dict[str, Any], dict[str, Any]]:
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
    for action in ordered_actions:
        validate_action_shape(action)
        if posting_policy is not None:
            try:
                policy_errors = action_policy_errors(action, posting_policy)
            except PostingPolicyError as exc:
                policy_errors = [str(exc)]
            if policy_errors:
                raise SimplbooksError(f"{action_id(action)} posting-policy mismatch: {policy_errors[0]}")

        if action_successfully_submitted(action):
            continue

        translated = translate_action_for_api(
            action,
            lookup=lookup,
            allow_unresolved_dependencies=(mode == "dry-run"),
            exchange_rate_cache=exchange_rate_cache,
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
    if mode == "write":
        if company_dir is None:
            raise SimplbooksError("Write mode requires --company-dir for bound reference verification.")
        resolved_company_id = resolve_company_id(company_id, company_dir=str(company_dir))
        bindings = {str(item.get("kind") or ""): item for item in action_batch.get("reference_artifacts") or []}
        required_bindings = ["posting_policy", "discovery_overview"]
        if any(
            str((action.get("payload") or {}).get("currency") or "EUR").upper() != "EUR"
            for action in action_batch.get("actions") or []
        ):
            required_bindings.append("exchange_rates")
        for kind in required_bindings:
            if kind not in bindings:
                raise SimplbooksError(f"Write mode action batch is not bound to required {kind} artifact.")
            try:
                bound_path = verify_file_binding(bindings[kind], cwd=cwd)
                if kind == "discovery_overview":
                    validate_discovery(
                        load_json(bound_path),
                        year=int(period[:4]),
                        company_id=resolved_company_id,
                    )
            except ReferenceArtifactError as exc:
                raise SimplbooksError(str(exc)) from exc

    company_slug = str(
        action_batch.get("company_slug")
        or (resolve_company_slug(company_dir=str(company_dir)) if company_dir is not None else "")
        or (company_dir.name if company_dir is not None else action_path.stem)
    )
    existing_submission = load_existing_submission(
        output_path=output_path,
        batch_id=str(action_batch.get("batch_id") or ""),
        company_slug=company_slug,
        period=period,
    )
    reference_lookup = load_prior_action_lookup(
        company_dir=company_dir,
        action_path=action_path,
        period=period,
    )
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
    )
    prior_request_count = len((existing_submission or {}).get("request_log") or [])
    current_request_log = submission["request_log"][prior_request_count:]

    write_yaml(action_path, updated_batch)
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
