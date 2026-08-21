#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from exchange_rates import ExchangeRateError, lookup_rate
from posting_policy import PostingPolicyError, action_policy_errors, load_posting_policy, resolve_sales_vat_profile
from reference_artifacts import ReferenceArtifactError, validate_discovery, verify_file_binding
from simplbooks_api import SimplbooksError, resolve_company_id, resolve_company_name


TOLERANCE = Decimal("0.01")

SECTIONS = (
    "duplicate_risk",
    "source_reference_coverage",
    "arithmetic_consistency",
    "account_and_vat_review",
    "exchange_rate_review",
    "recon_alignment",
    "historical_outliers",
)

SECTION_TITLES = {
    "duplicate_risk": "Duplicate Risk",
    "source_reference_coverage": "Source Reference Coverage",
    "arithmetic_consistency": "Arithmetic Consistency",
    "account_and_vat_review": "Account And VAT Review",
    "exchange_rate_review": "Exchange Rate Review",
    "recon_alignment": "Recon Alignment",
    "historical_outliers": "Historical Outliers",
}


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def display_path(path: Path, root_dir: Path) -> str:
    try:
        return str(path.relative_to(root_dir))
    except ValueError:
        return str(path)


def normalize_ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii")


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", normalize_ascii(str(value or "")).strip().lower())


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


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SimplbooksError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SimplbooksError(f"Invalid JSON in {path}: {exc}") from exc


def load_optional_text(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        try:
            completed = subprocess.run(
                [
                    "ruby",
                    "-ryaml",
                    "-rjson",
                    "-e",
                    "puts JSON.generate(YAML.load_file(ARGV[0]))",
                    str(path),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise SimplbooksError(
                "Could not load YAML: PyYAML is unavailable and Ruby is not installed."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise SimplbooksError(f"Could not parse YAML {path}: {stderr or exc}") from exc
        try:
            loaded = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SimplbooksError(f"Ruby YAML fallback produced invalid JSON for {path}.") from exc
    else:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # type: ignore[attr-defined]
            raise SimplbooksError(f"Could not parse YAML {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise SimplbooksError(f"YAML top level must be an object: {path}")
    return loaded


def make_finding(
    *,
    section: str,
    severity: str,
    summary: str,
    action_id: str | None = None,
) -> dict[str, Any]:
    return {
        "section": section,
        "severity": severity,
        "summary": summary,
        "action_id": action_id,
    }


def action_label(action: dict[str, Any]) -> str:
    return str(action.get("idempotency_key") or "<unknown-action>")


def is_temp_source_path(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    parts = [part for part in text.split("/") if part not in {"", "."}]
    return "temp" in parts


def evaluate_source_locations(
    action_batch: dict[str, Any],
    *,
    company_dir: Path | None,
    cwd: Path | None = None,
    action_path: Path | None = None,
) -> list[dict[str, Any]]:
    if company_dir is None:
        return []
    findings: list[dict[str, Any]] = []
    cwd = (cwd or Path.cwd()).resolve()
    action_path = action_path or cwd / company_dir / "artifacts" / "actions" / "batch.yaml"
    company_root = (company_dir if company_dir.is_absolute() else cwd / company_dir).resolve()
    allowed_roots = (company_root / "source", company_root / "artifacts")
    for action in action_batch.get("actions") or []:
        for ref in action.get("source_refs") or []:
            path_text = str(ref.get("path") or "")
            resolved = resolve_path(path_text, cwd=cwd, action_path=action_path).resolve()
            outside_company = not any(resolved.is_relative_to(root) for root in allowed_roots)
            has_traversal = ".." in Path(path_text).parts
            parts = Path(path_text).parts
            explicit_other_company = (
                len(parts) >= 2 and parts[0] == "companies" and parts[1] != company_dir.name
            )
            if is_temp_source_path(path_text) or has_traversal or explicit_other_company or outside_company:
                findings.append(
                    make_finding(
                        section="source_reference_coverage",
                        severity="error",
                        summary=f"Company action references non-canonical source path {path_text!r}; company-local evidence is required.",
                        action_id=action_label(action),
                    )
                )
    return findings


def evaluate_resolved_record_source_locations(
    *,
    action: dict[str, Any],
    resolved_sources: list[dict[str, Any]],
    company_dir: Path | None,
    cwd: Path | None = None,
    action_path: Path | None = None,
) -> list[dict[str, Any]]:
    if company_dir is None:
        return []
    findings: list[dict[str, Any]] = []
    cwd = (cwd or Path.cwd()).resolve()
    action_path = action_path or cwd / company_dir / "artifacts" / "actions" / "batch.yaml"
    company_root = (company_dir if company_dir.is_absolute() else cwd / company_dir).resolve()
    allowed_root = (company_root / "source").resolve()
    seen: set[str] = set()
    for resolved in resolved_sources:
        record = resolved.get("record") or {}
        for ref in record.get("source_refs") or []:
            path_text = str(ref.get("path") or "")
            resolved = resolve_path(path_text, cwd=cwd, action_path=action_path).resolve()
            unsafe = (
                is_temp_source_path(path_text)
                or ".." in Path(path_text).parts
                or not resolved.is_relative_to(allowed_root)
            )
            if not unsafe or path_text in seen:
                continue
            seen.add(path_text)
            findings.append(
                make_finding(
                    section="source_reference_coverage",
                    severity="error",
                    summary=f"Normalized evidence points to disposable source path {path_text!r}; regenerate from canonical company source.",
                    action_id=action_label(action),
                )
            )
    return findings


def resolve_path(path_text: str, *, cwd: Path, action_path: Path) -> Path:
    candidate = Path(path_text.split("#", 1)[0])
    if candidate.is_absolute():
        return candidate
    cwd_candidate = cwd / candidate
    if cwd_candidate.exists():
        return cwd_candidate
    action_candidate = action_path.parent / candidate
    return action_candidate


def build_record_index(payload: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    index: dict[str, tuple[str, dict[str, Any]]] = {}
    for category, records in (payload.get("records") or {}).items():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            record_id = record.get("record_id")
            if record_id:
                index[str(record_id)] = (str(category), record)
    return index


def load_record_payload(
    path: Path,
    *,
    cache: dict[Path, dict[str, Any]],
    index_cache: dict[Path, dict[str, tuple[str, dict[str, Any]]]],
) -> tuple[dict[str, Any] | None, dict[str, tuple[str, dict[str, Any]]] | None]:
    if path.suffix.lower() != ".json":
        return None, None
    if path not in cache:
        cache[path] = load_json(path)
    payload = cache[path]
    if path not in index_cache:
        if "records" in payload and isinstance(payload.get("records"), dict):
            index_cache[path] = build_record_index(payload)
        else:
            index_cache[path] = {}
    return payload, index_cache[path]


def resolve_action_sources(
    *,
    action: dict[str, Any],
    action_path: Path,
    cwd: Path,
    payload_cache: dict[Path, dict[str, Any]],
    index_cache: dict[Path, dict[str, tuple[str, dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str | None]] = set()
    source_refs = action.get("source_refs") or []

    if not source_refs:
        findings.append(
            make_finding(
                section="source_reference_coverage",
                severity="error",
                summary="Action has no source references.",
                action_id=action_label(action),
            )
        )
        return resolved, findings

    for ref in source_refs:
        path_text = str(ref.get("path") or "").strip()
        record_ref = ref.get("record_ref")
        if not path_text:
            findings.append(
                make_finding(
                    section="source_reference_coverage",
                    severity="error",
                    summary="Action contains a source reference without a path.",
                    action_id=action_label(action),
                )
            )
            continue

        resolved_path = resolve_path(path_text, cwd=cwd, action_path=action_path)
        pair = (str(resolved_path), str(record_ref) if record_ref is not None else None)
        if pair in seen_pairs:
            findings.append(
                make_finding(
                    section="duplicate_risk",
                    severity="warn",
                    summary=f"Action repeats the same source reference {path_text} {record_ref!r}.",
                    action_id=action_label(action),
                )
            )
        seen_pairs.add(pair)

        if not resolved_path.exists():
            findings.append(
                make_finding(
                    section="source_reference_coverage",
                    severity="error",
                    summary=f"Referenced source path does not exist: {path_text}.",
                    action_id=action_label(action),
                )
            )
            continue

        payload, record_index = load_record_payload(
            resolved_path,
            cache=payload_cache,
            index_cache=index_cache,
        )
        category = None
        record = None
        if record_ref in (None, ""):
            findings.append(
                make_finding(
                    section="source_reference_coverage",
                    severity="error",
                    summary=f"Source reference {path_text} is missing a record_ref.",
                    action_id=action_label(action),
                )
            )
        elif record_index is None:
            findings.append(
                make_finding(
                    section="source_reference_coverage",
                    severity="error",
                    summary=f"Could not verify record_ref {record_ref!r} because {path_text} is not a normalized JSON artifact.",
                    action_id=action_label(action),
                )
            )
        else:
            category_record = record_index.get(str(record_ref))
            if category_record is None:
                findings.append(
                    make_finding(
                        section="source_reference_coverage",
                        severity="error",
                        summary=f"record_ref {record_ref!r} was not found in {path_text}.",
                        action_id=action_label(action),
                    )
                )
            else:
                category, record = category_record

        resolved.append(
            {
                "path_text": path_text,
                "resolved_path": resolved_path,
                "record_ref": record_ref,
                "note": ref.get("note"),
                "payload": payload,
                "category": category,
                "record": record,
            }
        )

    return resolved, findings


def action_line_items(action: dict[str, Any]) -> list[dict[str, Any]]:
    payload = action.get("payload") or {}
    line_items = payload.get("line_items") or []
    return [item for item in line_items if isinstance(item, dict)]


def line_total(action: dict[str, Any]) -> Decimal:
    total = Decimal("0")
    for item in action_line_items(action):
        total += decimal_value(item.get("gross_amount"))
    return total


def payload_total(action: dict[str, Any], key: str) -> Decimal:
    payload = action.get("payload") or {}
    totals = payload.get("totals") or {}
    return decimal_value(totals.get(key))


def fee_amount_for_record(category: str, record: dict[str, Any]) -> Decimal:
    if category == "fees":
        gross = abs(decimal_value(record.get("gross_amount")))
        if gross != 0:
            return gross
        return abs(decimal_value(record.get("net_amount")))
    return abs(decimal_value(record.get("fee_amount")))


def compare_amount(
    *,
    findings: list[dict[str, Any]],
    section: str,
    action: dict[str, Any],
    label: str,
    expected: Decimal,
    actual: Decimal,
) -> None:
    if abs(expected - actual) <= TOLERANCE:
        return
    findings.append(
        make_finding(
            section=section,
            severity="error",
            summary=(
                f"{label} mismatch for {action_label(action)}: expected {decimal_number(expected)}, "
                f"found {decimal_number(actual)}."
            ),
            action_id=action_label(action),
        )
    )


def evaluate_duplicates(action_batch: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    seen_signatures: dict[str, str] = {}

    for action in action_batch.get("actions") or []:
        action_id = action_label(action)
        seen_ids[action_id] = seen_ids.get(action_id, 0) + 1

        signature = json.dumps(
            {
                "method": action.get("method"),
                "endpoint": action.get("endpoint"),
                "payload": action.get("payload"),
            },
            sort_keys=True,
        )
        prior = seen_signatures.get(signature)
        if prior and prior != action_id:
            findings.append(
                make_finding(
                    section="duplicate_risk",
                    severity="error",
                    summary=f"Action duplicates endpoint and payload of {prior}.",
                    action_id=action_id,
                )
            )
        else:
            seen_signatures[signature] = action_id

    for action_id, count in sorted(seen_ids.items()):
        if count > 1:
            findings.append(
                make_finding(
                    section="duplicate_risk",
                    severity="error",
                    summary=f"idempotency_key {action_id!r} appears {count} times.",
                    action_id=action_id,
                )
            )
    return findings


def evaluate_arithmetic(
    *,
    action: dict[str, Any],
    resolved_sources: list[dict[str, Any]],
    split_payment_action_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    payload = action.get("payload") or {}
    draft_schema = str(payload.get("draft_schema") or "")
    categories = [item["category"] for item in resolved_sources if item.get("category")]
    records = [item["record"] for item in resolved_sources if item.get("record")]
    paired_records = [
        (item["category"], item["record"])
        for item in resolved_sources
        if item.get("category") and item.get("record")
    ]

    if draft_schema in {"invoice_summary_v1", "purchase_summary_v1"}:
        compare_amount(
            findings=findings,
            section="arithmetic_consistency",
            action=action,
            label="Line-item total",
            expected=line_total(action),
            actual=payload_total(action, "gross_amount"),
        )

    if draft_schema == "invoice_summary_v1":
        expected_gross = sum(abs(decimal_value(record.get("gross_amount"))) for record in records)
        expected_shipping = sum(abs(decimal_value(record.get("shipping_amount"))) for record in records)
        compare_amount(
            findings=findings,
            section="arithmetic_consistency",
            action=action,
            label="Invoice gross total",
            expected=expected_gross,
            actual=payload_total(action, "gross_amount"),
        )
        compare_amount(
            findings=findings,
            section="arithmetic_consistency",
            action=action,
            label="Invoice shipping total",
            expected=expected_shipping,
            actual=payload_total(action, "shipping_amount"),
        )
        return findings

    if draft_schema == "purchase_summary_v1":
        line_items = action_line_items(action)
        if any(item.get("line_role") == "processor_fee" for item in line_items):
            expected_gross = sum(fee_amount_for_record(category, record) for category, record in paired_records)
        else:
            expected_gross = sum(abs(decimal_value(record.get("gross_amount"))) for record in records)
        compare_amount(
            findings=findings,
            section="arithmetic_consistency",
            action=action,
            label="Purchase gross total",
            expected=expected_gross,
            actual=payload_total(action, "gross_amount"),
        )
        return findings

    if draft_schema == "cash_settlement_v1":
        document_type = str(payload.get("document_type") or "")
        if document_type == "incoming":
            payout_records = [record for category, record in paired_records if category == "payouts"]
            expected_amount = sum(decimal_value(record.get("gross_amount")) for record in payout_records)
        elif document_type == "payment":
            bank_records = [record for category, record in paired_records if category == "bank_transactions"]
            expected_amount = sum(abs(decimal_value(record.get("gross_amount"))) for record in bank_records)
        else:
            expected_amount = Decimal("0")

        if not (
            document_type == "payment"
            and split_payment_action_ids is not None
            and action_label(action) in split_payment_action_ids
        ):
            compare_amount(
                findings=findings,
                section="arithmetic_consistency",
                action=action,
                label="Cash settlement amount",
                expected=expected_amount,
                actual=decimal_value(payload.get("amount")),
            )
        if document_type == "incoming" and "payouts" not in categories:
            findings.append(
                make_finding(
                    section="arithmetic_consistency",
                    severity="error",
                    summary="Incoming action does not reference any payout records.",
                    action_id=action_label(action),
                )
            )
        if document_type == "payment" and "bank_transactions" not in categories:
            findings.append(
                make_finding(
                    section="arithmetic_consistency",
                    severity="error",
                    summary="Payment action does not reference any bank transaction records.",
                    action_id=action_label(action),
                )
            )
    return findings


def payment_source_signature(
    action: dict[str, Any],
    resolved_sources: list[dict[str, Any]],
) -> tuple[tuple[str, str], ...] | None:
    payload = action.get("payload") or {}
    if str(payload.get("draft_schema") or "") != "cash_settlement_v1":
        return None
    if str(payload.get("document_type") or "") != "payment":
        return None

    refs = sorted(
        (
            str(item.get("path_text") or ""),
            str(item.get("record_ref") or ""),
        )
        for item in resolved_sources
        if item.get("category") == "bank_transactions" and item.get("record_ref")
    )
    if not refs:
        return None
    return tuple(refs)


def evaluate_split_payment_groups(
    *,
    action_batch: dict[str, Any],
    resolved_sources_by_action: dict[str, list[dict[str, Any]]],
) -> tuple[set[str], list[dict[str, Any]]]:
    grouped_actions: dict[tuple[tuple[str, str], ...], list[dict[str, Any]]] = defaultdict(list)
    actions_by_id = {
        action_label(action): action
        for action in action_batch.get("actions") or []
        if isinstance(action, dict)
    }

    for action_id, resolved_sources in resolved_sources_by_action.items():
        action = actions_by_id.get(action_id)
        if action is None:
            continue
        signature = payment_source_signature(action, resolved_sources)
        if signature is None:
            continue
        grouped_actions[signature].append(action)

    split_action_ids: set[str] = set()
    findings: list[dict[str, Any]] = []
    for signature, actions in grouped_actions.items():
        if len(actions) <= 1:
            continue

        split_action_ids.update(action_label(action) for action in actions)
        first_sources = resolved_sources_by_action[action_label(actions[0])]
        bank_records = [
            item["record"]
            for item in first_sources
            if item.get("category") == "bank_transactions" and item.get("record")
        ]
        expected_total = sum(abs(decimal_value(record.get("gross_amount"))) for record in bank_records)
        actual_total = sum(decimal_value((action.get("payload") or {}).get("amount")) for action in actions)
        if abs(expected_total - actual_total) <= TOLERANCE:
            continue

        action_ids = ", ".join(action_label(action) for action in actions)
        findings.append(
            make_finding(
                section="arithmetic_consistency",
                severity="error",
                summary=(
                    f"Split payment allocation mismatch across {action_ids}: expected "
                    f"{decimal_number(expected_total)}, found {decimal_number(actual_total)}."
                ),
                action_id=action_label(actions[0]),
            )
        )

    return split_action_ids, findings


def evaluate_account_vat(
    *,
    action: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    payload = action.get("payload") or {}
    draft_schema = str(payload.get("draft_schema") or "")
    confidence = str(action.get("confidence") or "")

    if confidence == "low":
        findings.append(
            make_finding(
                section="account_and_vat_review",
                severity="error",
                summary="Action confidence is low and should not be submitted without manual correction.",
                action_id=action_label(action),
            )
        )
    elif confidence == "medium":
        findings.append(
            make_finding(
                section="account_and_vat_review",
                severity="warn",
                summary="Action confidence is medium; review the mapping hints before submit.",
                action_id=action_label(action),
            )
        )

    if "forced" in normalize_text(" ".join(str(note) for note in action.get("review_notes") or [])):
        findings.append(
            make_finding(
                section="account_and_vat_review",
                severity="error",
                summary="Action batch was forced despite blocked recon evidence.",
                action_id=action_label(action),
            )
        )

    for line in action_line_items(action):
        line_role = str(line.get("line_role") or "line")
        gross_amount = abs(decimal_value(line.get("gross_amount")))
        vat_amount_hint = abs(decimal_value(line.get("vat_amount_hint")))
        income_account_id = line.get("suggested_income_account_id")
        expense_account_id = line.get("suggested_expense_account_id")
        vat_type_id = line.get("suggested_vat_type_id")

        if draft_schema == "invoice_summary_v1" and gross_amount > 0 and income_account_id in (None, ""):
            findings.append(
                make_finding(
                    section="account_and_vat_review",
                    severity="error",
                    summary=f"{line_role} is missing a suggested income account ID.",
                    action_id=action_label(action),
                )
            )
        if draft_schema in {"purchase_summary_v1", "purchase_credit_summary_v1"} and gross_amount > 0 and expense_account_id in (None, ""):
            findings.append(
                make_finding(
                    section="account_and_vat_review",
                    severity="error",
                    summary=f"{line_role} is missing a suggested expense account ID.",
                    action_id=action_label(action),
                )
            )
        if draft_schema == "purchase_credit_summary_v1":
            if decimal_value(line.get("gross_amount")) <= 0:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="Supplier-credit lines must use positive reviewed magnitudes; sender applies the posting sign.",
                        action_id=action_label(action),
                    )
                )
            if line.get("article_id_hint") not in (None, "") or line.get("warehouse_id_hint") not in (None, ""):
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="Inventory-linked supplier credit requires original stock-batch handling.",
                        action_id=action_label(action),
                    )
                )
        if gross_amount > 0 and vat_type_id in (None, ""):
            findings.append(
                make_finding(
                    section="account_and_vat_review",
                    severity="error",
                    summary=f"{line_role} is missing a suggested VAT type ID.",
                    action_id=action_label(action),
                )
            )
        if line_role.endswith("shipping") and vat_amount_hint == 0:
            findings.append(
                make_finding(
                    section="account_and_vat_review",
                    severity="warn",
                    summary="Shipping line has no VAT hint; shipping VAT treatment still needs manual review.",
                    action_id=action_label(action),
                )
            )

    counterparty = payload.get("counterparty") or {}
    if draft_schema in {"invoice_summary_v1", "purchase_summary_v1", "purchase_credit_summary_v1", "cash_settlement_v1"} and counterparty.get("contact_id") in (None, ""):
        findings.append(
            make_finding(
                section="account_and_vat_review",
                severity="error",
                summary=f"{draft_schema} draft does not resolve a concrete contact/client ID.",
                action_id=action_label(action),
            )
        )
    if draft_schema == "cash_settlement_v1" and payload.get("bank_account_id") in (None, ""):
        findings.append(
            make_finding(
                section="account_and_vat_review",
                severity="error",
                summary="Cash-settlement draft does not resolve a bank account ID.",
                action_id=action_label(action),
            )
        )
    return findings


def evaluate_exchange_rates(
    action_batch: dict[str, Any],
    *,
    base_currency: str = "EUR",
    exchange_rate_cache: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for action in action_batch.get("actions") or []:
        payload = action.get("payload") or {}
        currency = str(payload.get("currency") or base_currency).strip().upper()
        if currency == base_currency.upper():
            continue
        action_id = action_label(action)
        rate = decimal_value(payload.get("currency_rate"))
        provider = str(payload.get("currency_rate_provider") or "")
        source_url = str(payload.get("currency_rate_source_url") or "")
        requested_date = str(payload.get("currency_rate_requested_date") or payload.get("document_date") or "")
        document_date = str(payload.get("document_date") or "")
        effective_date = str(payload.get("currency_rate_effective_date") or "")
        errors: list[str] = []
        if rate <= 0:
            errors.append("a positive currency_rate")
        if provider != "ECB":
            errors.append("currency_rate_provider=ECB")
        if not source_url.startswith("https://api.frankfurter.dev/v2/rates"):
            errors.append("Frankfurter source provenance")
        if requested_date != document_date:
            errors.append("a requested date equal to the summary document date")
        try:
            requested = datetime.fromisoformat(requested_date).date()
            effective = datetime.fromisoformat(effective_date).date()
            if effective > requested:
                errors.append("an effective rate date on or before the requested date")
        except ValueError:
            errors.append("valid requested/effective rate dates")
        if errors:
            findings.append(
                make_finding(
                    section="exchange_rate_review",
                    severity="error",
                    summary=f"Foreign-currency action lacks {', '.join(errors)}.",
                    action_id=action_id,
                )
            )
            continue
        if exchange_rate_cache is None:
            findings.append(
                make_finding(
                    section="exchange_rate_review",
                    severity="error",
                    summary="Foreign-currency action cannot be verified because the annual ECB cache is unavailable.",
                    action_id=action_id,
                )
            )
            continue
        try:
            resolution = lookup_rate(
                exchange_rate_cache,
                requested_date=datetime.fromisoformat(requested_date).date(),
                base=currency,
                quote=base_currency,
            )
        except (ExchangeRateError, ValueError) as exc:
            findings.append(
                make_finding(
                    section="exchange_rate_review",
                    severity="error",
                    summary=f"ECB cache lookup failed: {exc}",
                    action_id=action_id,
                )
            )
            continue
        if (
            rate != resolution.rate
            or effective_date != resolution.effective_date.isoformat()
            or provider != resolution.provider
            or source_url != resolution.source_url
        ):
            findings.append(
                make_finding(
                    section="exchange_rate_review",
                    severity="error",
                    summary="Foreign-currency rate provenance does not exactly match the annual ECB cache.",
                    action_id=action_id,
                )
            )
    return findings


def evaluate_unresolved_dependencies(action_batch: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for dependency in action_batch.get("unresolved_dependencies") or []:
        if not dependency.get("blocking"):
            continue
        findings.append(
            make_finding(
                section="account_and_vat_review",
                severity="error",
                summary=str(dependency.get("reason") or "Action batch has a blocking unresolved dependency."),
                action_id=str(dependency.get("action_id") or "") or None,
            )
        )
    return findings


def evaluate_posting_policy(
    action_batch: dict[str, Any], posting_policy: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if posting_policy is None:
        return []
    findings: list[dict[str, Any]] = []
    for action in action_batch.get("actions") or []:
        try:
            errors = action_policy_errors(action, posting_policy)
        except PostingPolicyError as exc:
            errors = [str(exc)]
        for error in errors:
            findings.append(
                make_finding(
                    section="account_and_vat_review",
                    severity="error",
                    summary=error,
                    action_id=action_label(action),
                )
            )
    return findings


def evaluate_vat_profiles(actions: list[dict[str, Any]], posting_policy: dict[str, Any] | None) -> list[dict[str, Any]]:
    if posting_policy is None:
        return []
    findings: list[dict[str, Any]] = []
    for action in actions:
        payload = action.get("payload") or {}
        for line in payload.get("line_items") or []:
            has_profile_provenance = any(
                field in line for field in ("vat_profile_rate", "vat_profile_period", "vat_allocation_component")
            )
            if not has_profile_provenance:
                continue
            try:
                event_date = date.fromisoformat(str(payload.get("document_date") or ""))
                profile = resolve_sales_vat_profile(posting_policy, event_date=event_date)
            except (PostingPolicyError, ValueError) as exc:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary=f"VAT profile cannot be resolved for allocated line: {exc}",
                        action_id=action_label(action),
                    )
                )
                continue

            expected_period = f"{profile['start']}/{profile['end'] or 'open'}"
            line_role = str(line.get("line_role") or "")
            component = str(line.get("vat_allocation_component") or "")
            is_shipping = component == "shipping" or (not component and line_role.endswith("_shipping"))
            expected_vat_type_id = profile["shipping_vat_type_id"] if is_shipping else profile["goods_vat_type_id"]
            if line.get("vat_profile_rate") != profile["rate"]:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="VAT profile rate does not match the effective posting-policy profile.",
                        action_id=action_label(action),
                    )
                )
            if str(line.get("vat_profile_period") or "") != expected_period:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="VAT profile period does not match the effective posting-policy profile.",
                        action_id=action_label(action),
                    )
                )
            if str(line.get("suggested_vat_type_id") or "") != expected_vat_type_id:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="VAT profile VAT type does not match the effective goods or shipping mapping.",
                        action_id=action_label(action),
                    )
                )
            evidence = line.get("vat_allocation_component_evidence")
            if not isinstance(evidence, list) or not evidence:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="VAT profile line lacks per-order component rounding evidence.",
                        action_id=action_label(action),
                    )
                )
                continue

            try:
                gross_amount = abs(decimal_value(line.get("gross_amount")))
                vat_amount = abs(decimal_value(line.get("vat_amount_hint")))
                rate = Decimal(str(profile["rate"]))
                evidence_gross = Decimal("0")
                evidence_vat = Decimal("0")
                seen_order_ids: set[str] = set()
                rounding_error = False
                for item in evidence:
                    if not isinstance(item, dict):
                        raise SimplbooksError("component evidence item must be an object")
                    order_id = str(item.get("order_id") or "").strip()
                    if not order_id or order_id in seen_order_ids:
                        raise SimplbooksError("component evidence order IDs must be unique and non-empty")
                    seen_order_ids.add(order_id)
                    item_gross = abs(decimal_value(item.get("gross_amount")))
                    item_vat = abs(decimal_value(item.get("vat_amount")))
                    if item_gross != item_gross.quantize(TOLERANCE) or item_vat != item_vat.quantize(TOLERANCE):
                        raise SimplbooksError("component evidence amounts must use whole cents")
                    evidence_gross += item_gross
                    evidence_vat += item_vat
                    expected_item_vat = (item_gross * rate / (Decimal("100") + rate)).quantize(
                        TOLERANCE, rounding=ROUND_HALF_UP
                    )
                    rounding_error = rounding_error or item_vat != expected_item_vat
            except (InvalidOperation, SimplbooksError) as exc:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary=f"VAT profile line has invalid component rounding evidence: {exc}",
                        action_id=action_label(action),
                    )
                )
                continue
            if gross_amount != evidence_gross or vat_amount != evidence_vat:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="VAT profile line does not exactly match per-order component rounding evidence.",
                        action_id=action_label(action),
                    )
                )
            if rounding_error:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="VAT profile component rounding evidence does not match the effective rate.",
                        action_id=action_label(action),
                    )
                )
    return findings


def evaluate_reference_artifacts(
    action_batch: dict[str, Any],
    *,
    cwd: Path,
    company_dir: Path | None,
    expected_company_id: str | None,
) -> list[dict[str, Any]]:
    if company_dir is None:
        return []
    findings: list[dict[str, Any]] = []
    bindings = action_batch.get("reference_artifacts") or []
    by_kind = {str(item.get("kind") or ""): item for item in bindings}
    required = {"posting_policy", "discovery_overview"}
    if any(str((action.get("payload") or {}).get("currency") or "EUR").upper() != "EUR" for action in action_batch.get("actions") or []):
        required.add("exchange_rates")
    for kind in sorted(required):
        binding = by_kind.get(kind)
        if binding is None:
            findings.append(make_finding(section="duplicate_risk", severity="error", summary=f"Action batch is not bound to required {kind} artifact."))
            continue
        try:
            path = verify_file_binding(binding, cwd=cwd)
            if kind == "discovery_overview":
                if expected_company_id is None:
                    raise ReferenceArtifactError("Cannot verify discovery without company metadata ID.")
                validate_discovery(
                    load_json(path),
                    year=int(str(action_batch.get("period") or "0")[:4]),
                    company_id=expected_company_id,
                )
        except (ReferenceArtifactError, SimplbooksError) as exc:
            findings.append(make_finding(section="duplicate_risk", severity="error", summary=str(exc)))
    return findings


def evaluate_recon_alignment(
    *,
    action_batch: dict[str, Any],
    recon_payload: dict[str, Any],
    recon_path_display: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if action_batch.get("recon_ref") not in (None, "", recon_path_display):
        findings.append(
            make_finding(
                section="recon_alignment",
                severity="warn",
                summary=(
                    f"Action batch recon_ref {action_batch.get('recon_ref')!r} does not match the checked recon file "
                    f"{recon_path_display!r}."
                ),
            )
        )
    if not recon_payload.get("approve_for_build"):
        findings.append(
            make_finding(
                section="recon_alignment",
                severity="error",
                summary="Reconciliation does not approve this month for build.",
            )
        )
    if int(recon_payload.get("blocking_issue_count") or 0) > 0:
        findings.append(
            make_finding(
                section="recon_alignment",
                severity="error",
                summary=f"Reconciliation still reports {recon_payload.get('blocking_issue_count')} blocking issues.",
            )
        )
    fail_checks = [item for item in recon_payload.get("checks") or [] if item.get("status") == "fail"]
    blocking_exceptions = [item for item in recon_payload.get("exceptions") or [] if item.get("blocking")]
    if fail_checks:
        findings.append(
            make_finding(
                section="recon_alignment",
                severity="error",
                summary=f"Recon contains {len(fail_checks)} failed check(s).",
            )
        )
    if blocking_exceptions:
        findings.append(
            make_finding(
                section="recon_alignment",
                severity="error",
                summary=f"Recon contains {len(blocking_exceptions)} blocking exception(s).",
            )
        )
    warn_checks = [item for item in recon_payload.get("checks") or [] if item.get("status") == "warn"]
    if warn_checks:
        findings.append(
            make_finding(
                section="recon_alignment",
                severity="warn",
                summary=f"Recon still carries {len(warn_checks)} warning check(s).",
            )
        )
    return findings


def referenced_records_with_values(
    resolved_sources: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (item["category"], item["record"])
        for item in resolved_sources
        if item.get("category") and item.get("record")
    ]


def evaluate_historical_outliers(
    *,
    action: dict[str, Any],
    resolved_sources: list[dict[str, Any]],
    policy_text: str | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not policy_text:
        return findings

    policy = normalize_text(policy_text)
    payload = action.get("payload") or {}
    draft_schema = str(payload.get("draft_schema") or "")
    line_items = action_line_items(action)
    referenced = referenced_records_with_values(resolved_sources)
    referenced_records = [record for _, record in referenced]

    if draft_schema == "invoice_summary_v1":
        shipping_total = payload_total(action, "shipping_amount")
        if shipping_total > 0 and "shipping revenue may be kept separate" in policy:
            if not any(str(line.get("line_role") or "").endswith("shipping") for line in line_items):
                findings.append(
                    make_finding(
                        section="historical_outliers",
                        severity="warn",
                        summary="Policy memo suggests separate shipping treatment, but this sales action does not include a dedicated shipping line.",
                        action_id=action_label(action),
                    )
                )

        if "warehouse identity should be preserved" in policy:
            if any(record.get("warehouse_id") not in (None, "") for record in referenced_records):
                if any(line.get("warehouse_id_hint") in (None, "") for line in line_items):
                    findings.append(
                        make_finding(
                            section="historical_outliers",
                            severity="warn",
                            summary="Policy memo says warehouse identity matters, but one or more invoice lines do not preserve a warehouse hint.",
                            action_id=action_label(action),
                        )
                    )
    if draft_schema == "purchase_summary_v1" and "fees and fulfillment costs should be mapped from observed buckets" in policy:
        expense_ids = {
            line.get("suggested_expense_account_id")
            for line in line_items
            if line.get("suggested_expense_account_id") not in (None, "")
        }
        if len(expense_ids) <= 1:
            findings.append(
                make_finding(
                    section="historical_outliers",
                    severity="warn",
                    summary="Policy memo suggests bucketed purchase mappings; review whether this draft collapses historically distinct cost buckets.",
                    action_id=action_label(action),
                )
            )
    return findings


def evaluate_action_batch(
    *,
    action_batch: dict[str, Any],
    action_path: Path,
    recon_payload: dict[str, Any],
    recon_path: Path,
    policy_text: str | None,
    cwd: Path,
    company_dir: Path | None = None,
    exchange_rate_cache: dict[str, Any] | None = None,
    posting_policy: dict[str, Any] | None = None,
    expected_company_id: str | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    payload_cache: dict[Path, dict[str, Any]] = {}
    index_cache: dict[Path, dict[str, tuple[str, dict[str, Any]]]] = {}
    resolved_sources_by_action: dict[str, list[dict[str, Any]]] = {}

    findings.extend(evaluate_duplicates(action_batch))
    findings.extend(evaluate_exchange_rates(action_batch, exchange_rate_cache=exchange_rate_cache))
    findings.extend(evaluate_unresolved_dependencies(action_batch))
    findings.extend(evaluate_posting_policy(action_batch, posting_policy))
    findings.extend(evaluate_vat_profiles(action_batch.get("actions") or [], posting_policy))
    findings.extend(
        evaluate_reference_artifacts(
            action_batch,
            cwd=cwd,
            company_dir=company_dir,
            expected_company_id=expected_company_id,
        )
    )
    findings.extend(
        evaluate_source_locations(
            action_batch,
            company_dir=company_dir,
            cwd=cwd,
            action_path=action_path,
        )
    )
    findings.extend(
        evaluate_recon_alignment(
            action_batch=action_batch,
            recon_payload=recon_payload,
            recon_path_display=display_path(recon_path, cwd),
        )
    )

    for action in action_batch.get("actions") or []:
        resolved_sources, source_findings = resolve_action_sources(
            action=action,
            action_path=action_path,
            cwd=cwd,
            payload_cache=payload_cache,
            index_cache=index_cache,
        )
        resolved_sources_by_action[action_label(action)] = resolved_sources
        findings.extend(source_findings)
    split_payment_action_ids, split_payment_findings = evaluate_split_payment_groups(
        action_batch=action_batch,
        resolved_sources_by_action=resolved_sources_by_action,
    )
    findings.extend(split_payment_findings)

    for action in action_batch.get("actions") or []:
        resolved_sources = resolved_sources_by_action.get(action_label(action), [])
        findings.extend(
            evaluate_resolved_record_source_locations(
                action=action,
                resolved_sources=resolved_sources,
                company_dir=company_dir,
                cwd=cwd,
                action_path=action_path,
            )
        )
        findings.extend(
            evaluate_arithmetic(
                action=action,
                resolved_sources=resolved_sources,
                split_payment_action_ids=split_payment_action_ids,
            )
        )
        findings.extend(evaluate_account_vat(action=action))
        findings.extend(
            evaluate_historical_outliers(
                action=action,
                resolved_sources=resolved_sources,
                policy_text=policy_text,
            )
        )

    error_count = sum(1 for item in findings if item["severity"] == "error")
    warning_count = sum(1 for item in findings if item["severity"] == "warn")
    result = "fail" if error_count else "pass"

    section_buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in SECTIONS}
    for item in findings:
        section_buckets.setdefault(item["section"], []).append(item)

    return {
        "checked_at": utc_now_iso(),
        "result": result,
        "error_count": error_count,
        "warning_count": warning_count,
        "findings": findings,
        "sections": section_buckets,
    }


def render_findings(findings: list[dict[str, Any]]) -> list[str]:
    if not findings:
        return ["- none"]
    lines = []
    for item in findings:
        prefix = f"`{item['severity']}`"
        if item.get("action_id"):
            lines.append(f"- {prefix} `{item['action_id']}`: {item['summary']}")
        else:
            lines.append(f"- {prefix} {item['summary']}")
    return lines


def render_report(
    *,
    action_batch: dict[str, Any],
    action_path: Path,
    recon_path: Path,
    policy_path: Path | None,
    evaluation: dict[str, Any],
    company_name: str,
    cwd: Path,
) -> str:
    lines = [
        "# Check Report",
        "",
        "## Batch",
        f"- Company: {company_name}",
        f"- Period: {action_batch.get('period')}",
        f"- Batch ID: `{action_batch.get('batch_id')}`",
        f"- Action file: `{display_path(action_path, cwd)}`",
        f"- Action file SHA256: `{file_sha256(action_path)}`",
        f"- Recon file: `{display_path(recon_path, cwd)}`",
        f"- Policy memo: `{display_path(policy_path, cwd)}`" if policy_path else "- Policy memo: not provided",
        "",
        "## Decision",
        f"- Result: `{evaluation['result']}`",
        f"- Errors: {evaluation['error_count']}",
        f"- Warnings: {evaluation['warning_count']}",
        "",
        "## Findings",
        *render_findings(sorted(evaluation["findings"], key=lambda item: (item["severity"], item["section"], item.get("action_id") or ""))),
        "",
    ]

    for section in SECTIONS:
        lines.append(f"## {SECTION_TITLES[section]}")
        lines.extend(render_findings(evaluation["sections"].get(section, [])))
        lines.append("")

    lines.extend(
        [
            "## Follow-Up",
            "- Review all `error` findings before any submit-capable step.",
            "- Re-run `bookbuilder` or upstream skills after fixing mappings, totals, or source-pack gaps.",
            "- Keep this report with the draft batch and do not mark the batch approved until this gate passes.",
            "",
        ]
    )
    return "\n".join(lines)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def inferred_artifacts_dir(path: Path) -> Path | None:
    if path.parent.name in {"actions", "recon", "normalized"}:
        return path.parent.parent
    return None


def resolve_action_path(*, company_dir: Path | None, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is None:
        raise SimplbooksError("Pass --actions when --company-dir is not provided.")
    return company_dir / "artifacts" / "actions" / f"{period}.yaml"


def resolve_recon_path(*, company_dir: Path | None, action_path: Path, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "recon" / f"{period}.json"
    artifacts_dir = inferred_artifacts_dir(action_path)
    if artifacts_dir is None:
        raise SimplbooksError("Could not infer recon path; pass --recon explicitly.")
    return artifacts_dir / "recon" / f"{period}.json"


def resolve_policy_path(*, company_dir: Path | None, action_path: Path, override: str | None) -> Path | None:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "policy_memo.md"
    artifacts_dir = inferred_artifacts_dir(action_path)
    if artifacts_dir is None:
        return None
    return artifacts_dir / "policy_memo.md"


def resolve_output_path(*, company_dir: Path | None, action_path: Path, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "actions" / f"{period}.check.md"
    return action_path.with_suffix(".check.md")


def resolve_exchange_rates_path(*, company_dir: Path | None, period: str, override: str | None) -> Path | None:
    if override:
        return Path(override)
    if company_dir is None:
        return None
    return company_dir / "artifacts" / "reference" / f"ecb-rates-{period[:4]}.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a pre-submit review over a draft Simplbooks action batch")
    parser.add_argument("--company-dir", help="Company folder, e.g. companies/example")
    parser.add_argument("--period", required=True, help="Target month in YYYY-MM format")
    parser.add_argument("--actions", help="Path to actions YAML. Defaults to companies/<company>/artifacts/actions/<period>.yaml")
    parser.add_argument("--recon", help="Path to recon JSON. Defaults to companies/<company>/artifacts/recon/<period>.json")
    parser.add_argument("--policy-memo", help="Optional path to policy memo markdown")
    parser.add_argument("--exchange-rates", help="Annual ECB cache used to independently verify foreign rates")
    parser.add_argument("--posting-policy", help="Explicit posting policy used to independently verify every submit-capable ID")
    parser.add_argument("--output", help="Optional output path for the Markdown check report")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    company_dir = Path(args.company_dir) if args.company_dir else None
    action_path = resolve_action_path(company_dir=company_dir, period=args.period, override=args.actions)
    recon_path = resolve_recon_path(company_dir=company_dir, action_path=action_path, period=args.period, override=args.recon)
    policy_path = resolve_policy_path(company_dir=company_dir, action_path=action_path, override=args.policy_memo)
    output_path = resolve_output_path(company_dir=company_dir, action_path=action_path, period=args.period, override=args.output)
    exchange_rates_path = resolve_exchange_rates_path(
        company_dir=company_dir,
        period=args.period,
        override=args.exchange_rates,
    )

    action_batch = load_yaml(action_path)
    recon_payload = load_json(recon_path)
    policy_text = load_optional_text(policy_path)
    exchange_rate_cache = load_json(exchange_rates_path) if exchange_rates_path and exchange_rates_path.exists() else None
    posting_policy_path = Path(args.posting_policy) if args.posting_policy else (
        company_dir / "artifacts" / "posting_policy.json" if company_dir is not None else None
    )
    posting_policy = load_posting_policy(posting_policy_path) if posting_policy_path and posting_policy_path.exists() else None
    expected_company_id = resolve_company_id(None, company_dir=str(company_dir)) if company_dir is not None else None

    if action_batch.get("period") != args.period:
        raise SimplbooksError(
            f"Action batch period mismatch: expected {args.period}, got {action_batch.get('period')!r}"
        )
    if recon_payload.get("period") != args.period:
        raise SimplbooksError(
            f"Recon period mismatch: expected {args.period}, got {recon_payload.get('period')!r}"
        )

    cwd = Path.cwd()
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
        expected_company_id=expected_company_id,
    )

    company_slug = str(action_batch.get("company_slug") or (company_dir.name if company_dir else action_path.stem))
    company_name = resolve_company_name(company_dir=args.company_dir) if args.company_dir else None
    company_name = company_name or company_slug
    report = render_report(
        action_batch=action_batch,
        action_path=action_path,
        recon_path=recon_path,
        policy_path=policy_path if policy_text is not None else None,
        evaluation=evaluation,
        company_name=company_name,
        cwd=cwd,
    )
    write_text(output_path, report)

    summary = {
        "company_name": company_name,
        "company_slug": company_slug,
        "period": args.period,
        "actions": str(action_path),
        "recon": str(recon_path),
        "output": str(output_path),
        "result": evaluation["result"],
        "error_count": evaluation["error_count"],
        "warning_count": evaluation["warning_count"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
