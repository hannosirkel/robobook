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

from bank_allocations import (
    BankAllocationError,
    bank_ledger_key,
    load_bank_allocations,
    period_allocations,
    prove_exact_bank_allocation_coverage,
    statement_identity,
)
from exchange_rates import ExchangeRateError, lookup_rate
from posting_policy import (
    PostingPolicyError,
    action_policy_errors,
    load_posting_policy,
    resolve_bank_account,
    resolve_sales_vat_profile,
)
from reference_artifacts import (
    ReferenceArtifactError,
    required_action_binding_kinds,
    validate_discovery,
    verify_file_binding,
)
from simplbooks_api import SimplbooksError, resolve_company_id, resolve_company_name
from statement_import_evidence import (
    StatementImportEvidenceError,
    discovery_cash_evidence_errors,
    evidence_identity_errors,
    load_bound_evidence,
)


TOLERANCE = Decimal("0.01")

SECTIONS = (
    "duplicate_risk",
    "source_reference_coverage",
    "bank_statement_completeness",
    "arithmetic_consistency",
    "account_and_vat_review",
    "exchange_rate_review",
    "recon_alignment",
    "historical_outliers",
)

SECTION_TITLES = {
    "duplicate_risk": "Duplicate Risk",
    "source_reference_coverage": "Source Reference Coverage",
    "bank_statement_completeness": "Bank Statement Completeness",
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
                "source_kind": ref.get("source_kind"),
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
                "physical_bank_identity": sorted(
                    (
                        str(ref.get("path") or ""),
                        str(ref.get("record_ref") or ""),
                    )
                    for ref in action.get("source_refs") or []
                    if isinstance(ref, dict) and ref.get("source_kind") == "physical_bank"
                ) or None,
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
    physical_expected_amount: Decimal | None = None,
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
        if physical_expected_amount is not None:
            expected_amount = physical_expected_amount
            physical_bank_records = [
                item["record"]
                for item in resolved_sources
                if item.get("category") == "bank_transactions"
                and item.get("record")
                and item.get("source_kind") == "physical_bank"
            ]
            payout_records = []
        elif document_type == "incoming":
            physical_bank_records = [
                item["record"]
                for item in resolved_sources
                if item.get("category") == "bank_transactions"
                and item.get("record")
                and item.get("source_kind") == "physical_bank"
            ]
            payout_records = [record for category, record in paired_records if category == "payouts"]
            expected_amount = sum(
                decimal_value(record.get("gross_amount"))
                for record in (physical_bank_records or payout_records)
            )
        elif document_type == "payment":
            bank_records = [record for category, record in paired_records if category == "bank_transactions"]
            expected_amount = sum(abs(decimal_value(record.get("gross_amount"))) for record in bank_records)
        else:
            expected_amount = Decimal("0")

        if physical_expected_amount is not None or not (
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
        if document_type == "incoming" and not payout_records and not physical_bank_records:
            findings.append(
                make_finding(
                    section="arithmetic_consistency",
                    severity="error",
                    summary="Incoming action does not reference a payout or exact physical bank record.",
                    action_id=action_label(action),
                )
            )
        if document_type == "incoming" and physical_bank_records and len(physical_bank_records) != 1:
            findings.append(
                make_finding(
                    section="source_reference_coverage",
                    severity="error",
                    summary="Exact physical-bank incoming action must reference exactly one statement row.",
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
    reviewed_assignment_action_ids: set[str] | None = None,
) -> tuple[set[str], list[dict[str, Any]]]:
    grouped_actions: dict[tuple[tuple[str, str], ...], list[dict[str, Any]]] = defaultdict(list)
    actions_by_id = {
        action_label(action): action
        for action in action_batch.get("actions") or []
        if isinstance(action, dict)
    }

    for action_id, resolved_sources in resolved_sources_by_action.items():
        if action_id in (reviewed_assignment_action_ids or set()):
            continue
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
    batch_approved: bool = False,
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
                severity="error" if batch_approved else "warn",
                summary=(
                    "Action confidence is medium and contains an unresolved accounting judgment; "
                    "an approved batch must contain only high-confidence actions."
                    if batch_approved
                    else "Action confidence is medium; review the specific open accounting judgment before approval."
                ),
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


def _bank_allocation_binding(action_batch: dict[str, Any]) -> dict[str, Any] | None:
    bindings = [
        item
        for item in action_batch.get("reference_artifacts") or []
        if isinstance(item, dict) and str(item.get("kind") or "") == "bank_allocations"
    ]
    if len(bindings) != 1:
        return None
    return bindings[0]


def _normalized_binding_paths(payload: dict[str, Any], *, cwd: Path) -> list[Path]:
    paths: list[Path] = []
    for binding in payload.get("normalized_bindings") or []:
        if not isinstance(binding, dict):
            continue
        path = Path(str(binding.get("path") or ""))
        paths.append(path if path.is_absolute() else cwd / path)
    return paths


def _physical_record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    iban, currency = bank_ledger_key(record)
    return statement_identity(record), iban, currency


def _settlement_signed_amount(action: dict[str, Any]) -> Decimal:
    payload = action.get("payload") or {}
    amount = decimal_value(payload.get("amount"))
    document_type = str(payload.get("document_type") or "")
    if document_type == "incoming":
        return amount
    if document_type == "payment":
        return -amount
    raise SimplbooksError("Physical cash action must be an incoming or payment settlement.")


def _allocation_part_matches_action(
    part: dict[str, Any],
    *,
    action: dict[str, Any],
    signed_amount: Decimal,
) -> bool:
    payload = action.get("payload") or {}
    disposition = str(part.get("disposition") or "")
    document_type = str(payload.get("document_type") or "")
    allowed = (
        {"generated_invoice_receipt", "existing_invoice_receipt", "direct_sale_receipt"}
        if document_type == "incoming"
        else {"generated_purchase_payment", "existing_purchase_payment"}
    )
    if disposition not in allowed or decimal_value(part.get("amount")) != signed_amount:
        return False
    target = part.get("target") or {}
    expected_target_document_type = (
        "invoice" if disposition in {
            "generated_invoice_receipt", "existing_invoice_receipt", "direct_sale_receipt"
        } else "purchase"
    )
    if target.get("document_type") not in (None, "", expected_target_document_type):
        return False
    allowed_common_target_fields = {
        "document_type", "contact_id", "counterparty_hint", "external_number",
        "clearing_record_ids", "clearing_evidence", "clearing_totals",
        "clearing_relation", "bridge_amount", "bridge_record_ids", "bridge_direction", "fx_proof",
        "clearing_equations",
        "target_currency", "foreign_currency_pilot_required", "pilot_requirements",
    }
    if target.get("contact_id") not in (None, "") and str(
        (payload.get("counterparty") or {}).get("contact_id") or ""
    ) != str(target.get("contact_id")):
        return False
    if target.get("counterparty_hint") not in (None, "") and str(
        payload.get("counterparty_hint") or ""
    ) != str(target.get("counterparty_hint")):
        return False
    if disposition == "existing_invoice_receipt":
        return set(target) <= allowed_common_target_fields | {"simplbooks_id"} and str(
            payload.get("linked_invoice_id") or ""
        ) == str(target.get("simplbooks_id") or "")
    if disposition == "existing_purchase_payment":
        return set(target) <= allowed_common_target_fields | {"simplbooks_id"} and str(
            payload.get("linked_purchase_id") or ""
        ) == str(target.get("simplbooks_id") or "")
    if disposition == "generated_invoice_receipt":
        if not set(target) <= allowed_common_target_fields | {"action_key", "idempotency_key", "action_id"}:
            return False
        target_keys = {
            str(target.get(name) or "")
            for name in ("action_key", "idempotency_key", "action_id")
        } - {""}
        return len(target_keys) == 1 and str(payload.get("linked_invoice_action") or "") in target_keys
    if disposition == "generated_purchase_payment":
        if not set(target) <= allowed_common_target_fields | {"action_key", "idempotency_key", "action_id"}:
            return False
        target_keys = {
            str(target.get(name) or "")
            for name in ("action_key", "idempotency_key", "action_id")
        } - {""}
        return len(target_keys) == 1 and str(payload.get("linked_purchase_action") or "") in target_keys
    return disposition == "direct_sale_receipt" and bool(str(payload.get("linked_invoice_action") or ""))


def _direct_sale_invoice_errors(
    *,
    receipt: dict[str, Any],
    allocation: dict[str, Any],
    record: dict[str, Any],
    actions_by_key: dict[str, dict[str, Any]],
) -> list[str]:
    payload = receipt.get("payload") or {}
    invoice_key = str(payload.get("linked_invoice_action") or "")
    invoice = actions_by_key.get(invoice_key)
    if invoice is None or str(invoice.get("action_type") or "") != "create_invoice_summary":
        return ["Direct-sale receipt does not resolve to the actual generated direct-sale invoice action."]
    invoice_payload = invoice.get("payload") or {}
    if str(invoice_payload.get("draft_schema") or "") != "invoice_summary_v1":
        return ["Direct-sale invoice action has the wrong draft schema."]
    physical_refs = [
        ref for ref in invoice.get("source_refs") or []
        if isinstance(ref, dict) and ref.get("source_kind") == "physical_bank"
    ]
    record_id = str(record.get("record_id") or "")
    matching_indexes = [
        index for index, ref in enumerate(physical_refs)
        if str(ref.get("record_ref") or "") == record_id
    ]
    if len(matching_indexes) != 1:
        return ["Direct-sale invoice must contain the receipt's physical source row exactly once."]
    lines = invoice_payload.get("line_items") or []
    line_index = matching_indexes[0]
    if line_index >= len(lines) or not isinstance(lines[line_index], dict):
        return ["Direct-sale invoice source-row membership does not resolve to one invoice line."]
    line = lines[line_index]
    target = allocation.get("target") or {}
    scope = invoice_payload.get("summary_scope") or {}
    errors: list[str] = []
    supported_target_fields = {
        "document_type", "contact_label", "posting_family", "vat_profile",
        "product_description", "description", "quantity", "gross_amount",
        "warehouse_id", "contact_id", "income_account_id", "vat_type_id",
    }
    unsupported_fields = sorted(set(target) - supported_target_fields)
    if unsupported_fields:
        errors.append(
            "Direct-sale invoice cannot prove unsupported reviewed allocation target field(s): "
            + ", ".join(unsupported_fields)
        )
    expected_values = (
        ("contact label", scope.get("channel_or_source"), target.get("contact_label") or "direct-sale"),
        ("posting family", scope.get("posting_family"), target.get("posting_family")),
        ("VAT profile", scope.get("tax_profile"), target.get("vat_profile")),
        ("product description", line.get("description"), target.get("product_description") or target.get("description")),
        ("warehouse", line.get("warehouse_id_hint"), target.get("warehouse_id")),
        ("article", line.get("article_id_hint"), target.get("article_id")),
    )
    for label, actual, expected in expected_values:
        if str(actual if actual is not None else "") != str(expected if expected is not None else ""):
            errors.append(f"Direct-sale invoice {label} does not match the reviewed allocation target.")
    for label, actual, expected in (
        ("quantity", line.get("quantity"), target.get("quantity")),
        ("gross amount", line.get("gross_amount"), target.get("gross_amount")),
    ):
        if decimal_value(actual) != decimal_value(expected):
            errors.append(f"Direct-sale invoice {label} does not match the reviewed allocation target.")
    if str(invoice_payload.get("currency") or "") != str(payload.get("currency") or ""):
        errors.append("Direct-sale invoice currency does not match its receipt.")
    if str(target.get("contact_id") or "") and str((invoice_payload.get("counterparty") or {}).get("contact_id") or "") != str(target.get("contact_id")):
        errors.append("Direct-sale invoice contact does not match the reviewed allocation target.")
    if str(target.get("income_account_id") or "") and str(line.get("suggested_income_account_id") or "") != str(target.get("income_account_id")):
        errors.append("Direct-sale invoice income account does not match the reviewed allocation target.")
    if str(target.get("vat_type_id") or "") and str(line.get("suggested_vat_type_id") or "") != str(target.get("vat_type_id")):
        errors.append("Direct-sale invoice VAT type does not match the reviewed allocation target.")
    return errors


def _historical_generated_targets(
    *, action_path: Path, period: str
) -> dict[str, tuple[dict[str, Any], str, str]]:
    """Load prior action identity plus successful inserted-ID proof without trusting the cash draft."""
    if action_path.parent.name != "actions":
        return {}
    submissions_dir = action_path.parent.parent / "submissions"
    targets: dict[str, tuple[dict[str, Any], str, str]] = {}
    for prior_path in sorted(action_path.parent.glob("*.yaml")):
        if not re.fullmatch(r"\d{4}-\d{2}", prior_path.stem) or prior_path.stem >= period:
            continue
        prior_batch = load_yaml(prior_path)
        submission_path = submissions_dir / f"{prior_path.stem}.json"
        if not submission_path.exists():
            continue
        submission = load_json(submission_path)
        if (
            str(submission.get("period") or "") != prior_path.stem
            or not str(prior_batch.get("batch_id") or "")
            or str(submission.get("batch_id") or "") != str(prior_batch.get("batch_id") or "")
            or not str(prior_batch.get("company_slug") or "")
            or str(submission.get("company_slug") or "") != str(prior_batch.get("company_slug") or "")
            or str(submission.get("action_file_sha256") or "") != file_sha256(prior_path)
        ):
            continue
        successful_proofs: dict[str, tuple[str, str]] = {}
        for entry in submission.get("request_log") or []:
            if not isinstance(entry, dict) or entry.get("mode") != "write" or not entry.get("success"):
                continue
            inserted_id = entry.get("inserted_id")
            key = str(entry.get("action_idempotency_key") or "")
            if key and inserted_id not in (None, ""):
                successful_proofs[key] = (
                    str(inserted_id), str(entry.get("endpoint") or "")
                )
        for action in prior_batch.get("actions") or []:
            if not isinstance(action, dict):
                continue
            key = action_label(action)
            if key in successful_proofs:
                inserted_id, endpoint = successful_proofs[key]
                targets[key] = (action, inserted_id, endpoint)
    return targets


def _generated_target_errors(
    *,
    part: dict[str, Any],
    settlement: dict[str, Any],
    current_actions: dict[str, dict[str, Any]],
    historical_actions: dict[str, tuple[dict[str, Any], str, str]],
) -> list[str]:
    disposition = str(part.get("disposition") or "")
    target_kind = "invoice" if disposition == "generated_invoice_receipt" else "purchase"
    target = part.get("target") or {}
    target_keys = {
        str(target.get(name) or "")
        for name in ("action_key", "idempotency_key", "action_id")
    } - {""}
    if len(target_keys) != 1:
        return ["Generated target must contain exactly one action key."]
    target_key = next(iter(target_keys))
    expected_action_type = "create_invoice_summary" if target_kind == "invoice" else "create_purchase_summary"
    expected_schema = "invoice_summary_v1" if target_kind == "invoice" else "purchase_summary_v1"
    expected_endpoint = "invoices/create" if target_kind == "invoice" else "purchases/create"
    current = current_actions.get(target_key)
    if current is not None:
        errors: list[str] = []
        if (
            str(current.get("action_type") or "") != expected_action_type
            or str((current.get("payload") or {}).get("draft_schema") or "") != expected_schema
        ):
            errors.append(f"Generated target {target_key!r} is not the required {target_kind} action/schema type.")
        if list(settlement.get("depends_on") or []).count(target_key) != 1:
            errors.append(f"Current-batch generated target {target_key!r} must appear exactly once in depends_on.")
        errors.extend(_generated_settlement_contact_errors(target, settlement, current, target_key))
        return errors
    historical = historical_actions.get(target_key)
    if historical is None:
        return [f"Generated target {target_key!r} is not resolved by current or successful historical evidence."]
    historical_action, inserted_id, endpoint = historical
    if (
        str(historical_action.get("action_type") or "") != expected_action_type
        or str((historical_action.get("payload") or {}).get("draft_schema") or "") != expected_schema
    ):
        return [f"Historical generated target {target_key!r} has the wrong {target_kind} action/schema type."]
    if not inserted_id:
        return [f"Historical generated target {target_key!r} lacks successful inserted-ID proof."]
    if endpoint != expected_endpoint:
        return [f"Historical generated target {target_key!r} has the wrong successful object endpoint proof."]
    return _generated_settlement_contact_errors(target, settlement, historical_action, target_key)


def _generated_settlement_contact_errors(
    reviewed_target: dict[str, Any],
    settlement: dict[str, Any],
    document_action: dict[str, Any],
    target_key: str,
) -> list[str]:
    settlement_payload = settlement.get("payload") or {}
    document_payload = document_action.get("payload") or {}
    document_counterparty = document_payload.get("counterparty") or {}
    expected_contact = str(document_counterparty.get("contact_id") or "")
    expected_label = str(
        document_payload.get("vendor_hint")
        or (document_payload.get("summary_scope") or {}).get("channel_or_source")
        or document_counterparty.get("display_name_hint")
        or ""
    )
    errors: list[str] = []
    actual_contact = str((settlement_payload.get("counterparty") or {}).get("contact_id") or "")
    actual_label = str(settlement_payload.get("counterparty_hint") or "")
    if expected_contact and actual_contact != expected_contact:
        errors.append(f"Settlement contact does not match linked generated target {target_key!r}.")
    if expected_label and normalize_text(actual_label) != normalize_text(expected_label):
        errors.append(f"Settlement counterparty label does not match linked generated target {target_key!r}.")
    if str(reviewed_target.get("contact_id") or "") not in {"", expected_contact}:
        errors.append(f"Reviewed allocation contact conflicts with linked generated target {target_key!r}.")
    reviewed_label = str(reviewed_target.get("counterparty_hint") or "")
    if reviewed_label and expected_label and normalize_text(reviewed_label) != normalize_text(expected_label):
        errors.append(f"Reviewed allocation label conflicts with linked generated target {target_key!r}.")
    return errors


def evaluate_bank_statement_completeness(
    action_batch: dict[str, Any],
    *,
    action_path: Path,
    cwd: Path,
    posting_policy: dict[str, Any] | None = None,
    assigned_cash_amounts: dict[str, Decimal] | None = None,
) -> list[dict[str, Any]]:
    """Independently prove exact-once terminal coverage for this period's physical rows."""
    findings: list[dict[str, Any]] = []
    period = str(action_batch.get("period") or "")
    physical_action_refs = [
        ref
        for action in action_batch.get("actions") or []
        if isinstance(action, dict)
        for ref in action.get("source_refs") or []
        if isinstance(ref, dict) and ref.get("source_kind") == "physical_bank"
    ]
    manual_dependencies = [
        item
        for item in action_batch.get("unresolved_dependencies") or []
        if isinstance(item, dict)
        and str(item.get("kind") or "") == "manual_statement_import_financial_transaction"
    ]
    binding = _bank_allocation_binding(action_batch)
    if binding is None:
        inferred_normalized = (
            action_path.parent.parent / "normalized" / f"{period}.json"
            if action_path.parent.name == "actions"
            else None
        )
        inferred_has_physical = False
        if inferred_normalized is not None and inferred_normalized.exists():
            inferred_payload = load_json(inferred_normalized)
            inferred_has_physical = any(
                isinstance(record, dict) and str(record.get("source_system") or "") == "bank"
                for record in (inferred_payload.get("records") or {}).get("bank_transactions") or []
            )
        if physical_action_refs or manual_dependencies or inferred_has_physical:
            findings.append(make_finding(
                section="bank_statement_completeness",
                severity="error",
                summary="Physical bank coverage requires exactly one bound bank allocation artifact.",
            ))
        return findings

    try:
        allocation_path = verify_file_binding(binding, cwd=cwd)
        raw_allocations = load_json(allocation_path)
        normalized_paths = _normalized_binding_paths(raw_allocations, cwd=cwd)
        allocation_payload = load_bank_allocations(
            allocation_path,
            normalized_year_paths=normalized_paths,
        )
        if str(allocation_payload.get("company_slug") or "") != str(
            action_batch.get("company_slug") or ""
        ):
            raise BankAllocationError("Bank allocation company_slug does not match the action batch.")
        if str(allocation_payload.get("year") or "") != period[:4]:
            raise BankAllocationError("Bank allocation year does not match the action period.")
        prove_exact_bank_allocation_coverage(
            allocation_payload,
            normalized_year_paths=normalized_paths,
        )
        allocations = period_allocations(allocation_payload, period)
    except (BankAllocationError, ReferenceArtifactError, SimplbooksError, OSError) as exc:
        return [make_finding(
            section="bank_statement_completeness",
            severity="error",
            summary=f"Bank statement allocation proof is invalid or stale: {exc}",
        )]

    physical_records: dict[tuple[str, str, str], tuple[Path, dict[str, Any]]] = {}
    record_indexes: dict[Path, dict[str, tuple[str, dict[str, Any]]]] = {}
    for normalized_path in normalized_paths:
        payload = load_json(normalized_path)
        record_indexes[normalized_path.resolve()] = build_record_index(payload)
        if str(payload.get("period") or "") != period:
            continue
        for record in (payload.get("records") or {}).get("bank_transactions") or []:
            if not isinstance(record, dict) or str(record.get("source_system") or "") != "bank":
                continue
            try:
                key = _physical_record_key(record)
            except BankAllocationError as exc:
                findings.append(make_finding(
                    section="bank_statement_completeness", severity="error",
                    summary=f"Physical bank row has invalid identity: {exc}",
                    action_id=str(record.get("record_id") or "") or None,
                ))
                continue
            physical_records[key] = (normalized_path.resolve(), record)

    actions_by_key = {
        action_label(action): action
        for action in action_batch.get("actions") or []
        if isinstance(action, dict)
    }
    historical_actions = _historical_generated_targets(action_path=action_path, period=period)
    coverage: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for action in action_batch.get("actions") or []:
        if not isinstance(action, dict) or str((action.get("payload") or {}).get("draft_schema") or "") != "cash_settlement_v1":
            continue
        refs = [
            ref for ref in action.get("source_refs") or []
            if isinstance(ref, dict) and ref.get("source_kind") == "physical_bank"
        ]
        if len(refs) != 1:
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary="Each physical cash settlement action must reference exactly one physical bank row.",
                action_id=action_label(action),
            ))
            continue
        ref = refs[0]
        source_path = resolve_path(str(ref.get("path") or ""), cwd=cwd, action_path=action_path).resolve()
        resolved = record_indexes.get(source_path, {}).get(str(ref.get("record_ref") or ""))
        if resolved is None:
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary="Physical settlement source reference is extra, stale, or cannot be resolved.",
                action_id=action_label(action),
            ))
            continue
        category, record = resolved
        if category != "bank_transactions" or str(record.get("source_system") or "") != "bank":
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary="A clearing record must not masquerade as a physical bank row.",
                action_id=action_label(action),
            ))
            continue
        try:
            key = _physical_record_key(record)
            signed_amount = _settlement_signed_amount(action)
        except (BankAllocationError, SimplbooksError) as exc:
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error", summary=str(exc),
                action_id=action_label(action),
            ))
            continue
        payload = action.get("payload") or {}
        if str(payload.get("document_date") or "") != str(record.get("event_date") or ""):
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary="Physical settlement statement date does not match the normalized bank row.",
                action_id=action_label(action),
            ))
        if str(payload.get("currency") or "").upper() != key[2]:
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary="Physical settlement currency does not match the normalized bank row.",
                action_id=action_label(action),
            ))
        if posting_policy is not None:
            try:
                expected_bank_account_id = resolve_bank_account(
                    posting_policy,
                    customer_account=key[1],
                    currency=key[2],
                    allow_legacy_single_currency=False,
                )
            except PostingPolicyError as exc:
                findings.append(make_finding(
                    section="bank_statement_completeness", severity="error",
                    summary=f"Physical settlement source account cannot be resolved exactly: {exc}",
                    action_id=action_label(action),
                ))
            else:
                if str(payload.get("bank_account_id") or "") != str(expected_bank_account_id):
                    findings.append(make_finding(
                        section="bank_statement_completeness", severity="error",
                        summary="Physical settlement source account does not match the exact (IBAN, currency) policy mapping.",
                        action_id=action_label(action),
                    ))
        coverage[key].append({"kind": "action", "label": action_label(action), "amount": signed_amount, "action": action})

    payload_cache: dict[Path, dict[str, Any]] = {}
    index_cache: dict[Path, dict[str, tuple[str, dict[str, Any]]]] = {}
    for dependency in manual_dependencies:
        proof = dependency.get("statement_import_proof") or {}
        valid = (
            dependency.get("blocking") is False
            and proof.get("status") == "verified"
            and not manual_financial_dependency_errors(dependency)
            and not manual_financial_source_errors(
                dependency,
                action_path=action_path,
                cwd=cwd,
                payload_cache=payload_cache,
                index_cache=index_cache,
            )
        )
        if not valid:
            continue
        try:
            key = (
                str(dependency.get("statement_id") or ""),
                re.sub(r"\s+", "", str(dependency.get("iban") or "")).upper(),
                str(dependency.get("currency") or "").upper(),
            )
            allocation = allocations.get(key)
            expected_split_parts = [
                {
                    "signed_amount": decimal_number(decimal_value(part.get("amount"))),
                    "disposition": str(part.get("disposition") or ""),
                    "target": part.get("target") or {},
                }
                for part in (allocation or {}).get("parts") or []
                if isinstance(part, dict)
            ]
            manual_matches_allocation = (
                isinstance(allocation, dict)
                and str(dependency.get("disposition") or "") == str(allocation.get("disposition") or "")
                and (dependency.get("target") or {}) == (allocation.get("target") or {})
                and (dependency.get("split_parts") or []) == expected_split_parts
            )
            if not manual_matches_allocation:
                findings.append(make_finding(
                    section="bank_statement_completeness", severity="error",
                    summary="verified manual coverage does not match the reviewed allocation disposition, target, or split parts.",
                    action_id=str(dependency.get("record_id") or "") or None,
                ))
                continue
            coverage[key].append({
                "kind": "manual",
                "label": str(dependency.get("record_id") or "manual dependency"),
                "amount": decimal_value(dependency.get("physical_signed_amount")),
                "dependency": dependency,
            })
        except SimplbooksError:
            continue

    for key, (_source_path, record) in physical_records.items():
        items = coverage.get(key, [])
        allocation = allocations.get(key)
        if allocation is None:
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary="Physical bank row has no current-period reviewed allocation.",
                action_id=str(record.get("record_id") or "") or None,
            ))
            continue
        manual_required = str(allocation.get("disposition") or "") in {"bank_fee_payment", "expense_reimbursement_payment", "clearing_transfer"}
        if str(allocation.get("disposition") or "") == "reviewed_split":
            manual_required = any(
                str(part.get("disposition") or "") in {"bank_fee_payment", "expense_reimbursement_payment", "clearing_transfer"}
                for part in allocation.get("parts") or [] if isinstance(part, dict)
            )
        allocation_parts = (
            [part for part in allocation.get("parts") or [] if isinstance(part, dict)]
            if str(allocation.get("disposition") or "") == "reviewed_split"
            else [allocation]
        )
        expected_count = 1 if manual_required else len(allocation_parts)
        if not items:
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary="uncovered physical bank row has no exact settlement action or verified manual import proof.",
                action_id=str(record.get("record_id") or "") or None,
            ))
            continue
        if len(items) != expected_count:
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary=f"duplicate physical bank coverage: expected {expected_count} terminal coverage item(s), found {len(items)}.",
                action_id=str(record.get("record_id") or "") or None,
            ))
        actual_total = sum((item["amount"] for item in items), Decimal("0"))
        if actual_total != decimal_value(record.get("gross_amount")):
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary="Physical settlement signed parts do not sum to the whole reviewed bank row.",
                action_id=str(record.get("record_id") or "") or None,
            ))
        if manual_required:
            manual_items = [item for item in items if item.get("kind") == "manual"]
            api_items = [item for item in items if item.get("kind") == "action"]
            if len(manual_items) != 1 or api_items:
                findings.append(make_finding(
                    section="bank_statement_completeness",
                    severity="error",
                    summary=(
                        "Manual atomicity requires exactly one verified manual coverage item "
                        "and zero API cash actions for the physical row."
                    ),
                    action_id=str(record.get("record_id") or "") or None,
                ))
            continue
        unmatched_parts = set(range(len(allocation_parts)))
        assignment_failed = False
        for item in items:
            action = item.get("action")
            matching = [
                index for index in sorted(unmatched_parts)
                if isinstance(action, dict)
                and _allocation_part_matches_action(
                    allocation_parts[index], action=action, signed_amount=item["amount"]
                )
            ]
            if not matching:
                payload = (action or {}).get("payload") or {}
                document_type = str(payload.get("document_type") or "")
                allowed_dispositions = (
                    {"generated_invoice_receipt", "existing_invoice_receipt", "direct_sale_receipt"}
                    if document_type == "incoming"
                    else {"generated_purchase_payment", "existing_purchase_payment"}
                )
                if any(
                    str(allocation_parts[index].get("disposition") or "") in allowed_dispositions
                    and decimal_value(allocation_parts[index].get("amount")) == item["amount"]
                    for index in unmatched_parts
                ):
                    findings.append(make_finding(
                        section="bank_statement_completeness",
                        severity="error",
                        summary="Cash settlement does not match the exact reviewed target for its allocation part.",
                        action_id=str(item["label"]),
                    ))
                assignment_failed = True
                continue
            assigned_index = matching[0]
            unmatched_parts.remove(assigned_index)
            if assigned_cash_amounts is not None:
                assigned_cash_amounts[str(item["label"])] = abs(
                    decimal_value(allocation_parts[assigned_index].get("amount"))
                )
            if str(allocation_parts[assigned_index].get("disposition") or "") == "direct_sale_receipt":
                for error in _direct_sale_invoice_errors(
                    receipt=action,
                    allocation=allocation,
                    record=record,
                    actions_by_key=actions_by_key,
                ):
                    findings.append(make_finding(
                        section="bank_statement_completeness", severity="error",
                        summary=error, action_id=str(item["label"]),
                    ))
            if str(allocation_parts[assigned_index].get("disposition") or "") in {
                "generated_invoice_receipt", "generated_purchase_payment"
            }:
                for error in _generated_target_errors(
                    part=allocation_parts[assigned_index],
                    settlement=action,
                    current_actions=actions_by_key,
                    historical_actions=historical_actions,
                ):
                    findings.append(make_finding(
                        section="bank_statement_completeness", severity="error",
                        summary=error, action_id=str(item["label"]),
                    ))
        if assignment_failed or unmatched_parts:
            findings.append(make_finding(
                section="bank_statement_completeness", severity="error",
                summary="Reviewed split/action coverage is not a bijective assignment by signed amount, disposition, and exact target.",
                action_id=str(record.get("record_id") or "") or None,
            ))

    for key in sorted(set(coverage) - set(physical_records)):
        findings.append(make_finding(
            section="bank_statement_completeness", severity="error",
            summary=f"Extra physical bank coverage does not belong to period {period}: {key}.",
        ))
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


def _canonical_record_sha256(record: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _canonical_value_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _inventory_group_label(record: dict[str, Any]) -> str:
    attributes = record.get("attributes") or {}
    return str(
        record.get("channel")
        or attributes.get("processor")
        or attributes.get("fulfillment_partner")
        or record.get("source_system")
        or "sales"
    )


def _inventory_scope_records(
    scope: dict[str, Any], *, normalized_payloads: list[dict[str, Any]],
    reviewed_allocations: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    category = str(scope.get("record_category") or "")
    candidates = [
        record
        for payload in normalized_payloads
        for record in ((payload.get("records") or {}).get(category) or [])
        if isinstance(record, dict)
    ]
    if scope.get("kind") == "normalized_sales_group":
        selected = [
            record for record in candidates
            if str(record.get("event_date") or "")[:7] == str(scope.get("period") or "")
            and str(record.get("currency") or "EUR").upper() == str(scope.get("currency") or "").upper()
            and _inventory_group_label(record) == str(scope.get("group_label") or "")
            and ("taxable" if abs(decimal_value(record.get("vat_amount"))) > 0 else "non_taxable")
            == str(scope.get("tax_profile") or "")
        ]
        return selected, "normalized_record"
    if scope.get("kind") == "reviewed_direct_sale_allocation":
        matches = [
            (record, allocation)
            for record in candidates
            for allocation in [reviewed_allocations.get(str(record.get("record_id") or ""))]
            if isinstance(allocation, dict)
            and str(allocation.get("statement_id") or "") == str(scope.get("statement_id") or "")
            and str(allocation.get("period") or "") == str(scope.get("period") or "")
            and str(allocation.get("disposition") or "") == "direct_sale_receipt"
        ]
        return [record for record, _allocation in matches], "reviewed_allocation_target"
    return [], ""


def evaluate_inventory_quantities(
    *, action: dict[str, Any], resolved_sources: list[dict[str, Any]],
    reviewed_allocations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Independently prove each article line's quantity against immutable contributors."""
    findings: list[dict[str, Any]] = []
    records = {
        str(item.get("record_ref") or ""): item.get("record")
        for item in resolved_sources
        if isinstance(item.get("record"), dict) and str(item.get("record_ref") or "")
    }
    for line in action_line_items(action):
        if line.get("article_id_hint") in (None, ""):
            continue
        proof = line.get("inventory_quantity_proof")
        problems: list[str] = []
        if "shipping" in str(line.get("line_role") or "").lower():
            problems.append("Shipping line must not carry an inventory article.")
        proof_fields = {
            "status", "quantity", "scope", "scope_sha256", "contributor_count",
            "contributor_set_sha256", "contributors",
        }
        if not isinstance(proof, dict) or set(proof) != proof_fields:
            problems.append("Inventory article line requires an exact quantity proof object.")
            contributors: list[Any] = []
        else:
            contributors = proof.get("contributors") if isinstance(proof.get("contributors"), list) else []
            scope = proof.get("scope") if isinstance(proof.get("scope"), dict) else {}
            category = str(scope.get("record_category") or "")
            action_type = str(action.get("action_type") or "")
            document_type = str((action.get("payload") or {}).get("document_type") or "")
            line_role = str(line.get("line_role") or "")
            sales_role = line_role == "sales_revenue" or ("sales" in line_role and "product" in line_role)
            refund_role = line_role == "refund_revenue" or ("refund" in line_role and "product" in line_role)
            action_contracts = {
                "sales": action_type == "create_invoice_summary" and document_type == "invoice" and sales_role,
                "refunds": (
                    action_type == "create_credit_invoice_summary"
                    and document_type == "credit_note"
                    and refund_role
                ),
                "bank_transactions": (
                    action_type == "create_invoice_summary"
                    and document_type == "invoice"
                    and line_role == "direct_sale_revenue"
                    and scope.get("kind") == "reviewed_direct_sale_allocation"
                ),
            }
            if category in action_contracts and not action_contracts[category]:
                problems.append(f"{category} inventory scope does not match the action contract.")
            try:
                line_quantity = decimal_value(line.get("quantity"))
                proof_quantity = decimal_value(proof.get("quantity"))
            except SimplbooksError:
                line_quantity = proof_quantity = Decimal("0")
            if proof.get("status") != "exact" or line_quantity <= 0 or proof_quantity != line_quantity:
                problems.append("Inventory article line and proof require the same exact positive quantity.")
            if not contributors:
                problems.append("Inventory quantity proof requires contributors.")
            if not scope or _canonical_value_sha256(scope) != str(proof.get("scope_sha256") or ""):
                problems.append("Inventory quantity proof scope hash does not match its declared scope.")
        seen: set[str] = set()
        contributor_total = Decimal("0")
        for contributor in contributors:
            if not isinstance(contributor, dict) or set(contributor) != {
                "record_id", "quantity", "quantity_source", "record_sha256"
            }:
                problems.append("Inventory quantity contributor shape is invalid.")
                continue
            record_id = str(contributor.get("record_id") or "")
            if not record_id or record_id in seen:
                problems.append("Inventory quantity contributors require unique record IDs.")
                continue
            seen.add(record_id)
            try:
                quantity = decimal_value(contributor.get("quantity"))
            except SimplbooksError:
                quantity = Decimal("0")
            if quantity <= 0:
                problems.append("Inventory quantity contributor must be positive.")
                continue
            contributor_total += quantity
            record = records.get(record_id)
            if not isinstance(record, dict):
                problems.append(f"Inventory quantity contributor {record_id} is not an action source.")
                continue
            if _canonical_record_sha256(record) != str(contributor.get("record_sha256") or ""):
                problems.append(f"Inventory quantity contributor {record_id} SHA-256 does not match its source record.")
            source = contributor.get("quantity_source")
            if source == "normalized_record":
                try:
                    source_quantity = decimal_value(record.get("quantity"))
                except SimplbooksError:
                    source_quantity = Decimal("0")
                if source_quantity <= 0 or source_quantity != quantity:
                    problems.append(f"Inventory quantity contributor {record_id} does not match normalized quantity.")
            elif source == "reviewed_allocation_target":
                allocation = reviewed_allocations.get(record_id)
                target = allocation.get("target") if isinstance(allocation, dict) else None
                try:
                    allocated_quantity = decimal_value((target or {}).get("quantity"))
                except SimplbooksError:
                    allocated_quantity = Decimal("0")
                if not isinstance(target, dict) or allocated_quantity <= 0 or allocated_quantity != quantity:
                    problems.append(f"Inventory quantity contributor {record_id} does not match reviewed allocation target.")
            else:
                problems.append(f"Inventory quantity contributor {record_id} has unsupported quantity source.")
        try:
            if contributors and contributor_total != decimal_value(line.get("quantity")):
                problems.append("Inventory contributor quantities do not reconcile to the article line.")
        except SimplbooksError:
            pass
        normalized_payloads = []
        seen_payload_ids: set[int] = set()
        for item in resolved_sources:
            payload = item.get("payload")
            if isinstance(payload, dict) and id(payload) not in seen_payload_ids:
                normalized_payloads.append(payload)
                seen_payload_ids.add(id(payload))
        if isinstance(proof, dict) and isinstance(proof.get("scope"), dict):
            expected_records, quantity_source = _inventory_scope_records(
                proof["scope"], normalized_payloads=normalized_payloads,
                reviewed_allocations=reviewed_allocations,
            )
            expected_contributors: list[dict[str, Any]] = []
            for record in expected_records:
                if quantity_source == "reviewed_allocation_target":
                    target = (reviewed_allocations.get(str(record.get("record_id") or "")) or {}).get("target") or {}
                    quantity_value = target.get("quantity")
                else:
                    quantity_value = record.get("quantity")
                try:
                    quantity = decimal_value(quantity_value)
                except SimplbooksError:
                    quantity = Decimal("0")
                if quantity <= 0:
                    problems.append("Inventory semantic scope contains a record without exact positive quantity.")
                    continue
                expected_contributors.append({
                    "record_id": str(record.get("record_id") or ""),
                    "quantity": decimal_number(quantity),
                    "quantity_source": quantity_source,
                    "record_sha256": _canonical_record_sha256(record),
                })
            expected_contributors.sort(key=lambda item: (item["record_id"], item["record_sha256"]))
            if (
                contributors != expected_contributors
                or int(proof.get("contributor_count") or -1) != len(expected_contributors)
                or str(proof.get("contributor_set_sha256") or "") != _canonical_value_sha256(expected_contributors)
            ):
                problems.append("Inventory proof does not contain the complete contributor set for its semantic scope.")
        for problem in problems:
            findings.append(make_finding(
                section="account_and_vat_review", severity="error", summary=problem,
                action_id=action_label(action),
            ))
    return findings


def load_reviewed_allocation_index(
    action_batch: dict[str, Any], *, cwd: Path,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    binding = _bank_allocation_binding(action_batch)
    if binding is None:
        return {}, []
    try:
        allocation_path = verify_file_binding(binding, cwd=cwd)
        raw = load_json(allocation_path)
        normalized_paths = _normalized_binding_paths(raw, cwd=cwd)
        payload = load_bank_allocations(allocation_path, normalized_year_paths=normalized_paths)
        allocations = period_allocations(payload, str(action_batch.get("period") or ""))
        by_record_id: dict[str, dict[str, Any]] = {}
        for allocation in allocations.values():
            record_id = str(allocation.get("record_id") or "")
            if not record_id or record_id in by_record_id:
                raise BankAllocationError(
                    f"Inventory allocation record_id index is missing or duplicated: {record_id!r}"
                )
            by_record_id[record_id] = allocation
        return by_record_id, []
    except (BankAllocationError, ReferenceArtifactError, SimplbooksError, OSError, json.JSONDecodeError) as exc:
        return {}, [make_finding(
            section="account_and_vat_review", severity="error",
            summary=f"Inventory allocation proof cannot be loaded: {exc}",
        )]


def manual_financial_dependency_errors(
    dependency: dict[str, Any], *, cwd: Path | None = None,
    expected_company_id: str | None = None, require_typed_context: bool = False,
    discovery_payloads: list[dict[str, Any]] | None = None,
) -> list[str]:
    errors: list[str] = []
    allowed_top_level_dispositions = {
        "bank_fee_payment",
        "expense_reimbursement_payment",
        "clearing_transfer",
        "reviewed_split",
    }
    allowed_split_dispositions = {
        "generated_invoice_receipt",
        "existing_invoice_receipt",
        "generated_purchase_payment",
        "existing_purchase_payment",
        "direct_sale_receipt",
        "bank_fee_payment",
        "expense_reimbursement_payment",
        "clearing_transfer",
    }
    required_text = (
        "reason",
        "disposition",
        "statement_id",
        "record_id",
        "date",
        "iban",
        "currency",
        "reviewed_rationale",
    )
    for field in required_text:
        if not str(dependency.get(field) or "").strip():
            errors.append(f"Manual financial dependency requires {field}.")
    if str(dependency.get("disposition") or "") not in allowed_top_level_dispositions:
        errors.append(
            "Manual financial dependency top-level disposition must be bank_fee_payment, "
            "expense_reimbursement_payment, clearing_transfer, or reviewed_split."
        )
    try:
        date.fromisoformat(str(dependency.get("date") or ""))
    except ValueError:
        errors.append("Manual financial dependency date must be ISO YYYY-MM-DD.")
    if not re.fullmatch(r"[A-Z]{3}", str(dependency.get("currency") or "")):
        errors.append("Manual financial dependency currency must be an uppercase ISO code.")
    try:
        if dependency.get("physical_signed_amount") in (None, ""):
            raise ValueError
        physical_amount = decimal_value(dependency.get("physical_signed_amount"))
        if not physical_amount.is_finite() or physical_amount.quantize(Decimal("0.01")) != physical_amount:
            raise ValueError
    except (SimplbooksError, ValueError):
        physical_amount = Decimal("0")
        errors.append("Manual financial dependency requires an exact physical signed amount.")

    source_ref = dependency.get("source_ref")
    if not isinstance(source_ref, dict) or (
        not str(source_ref.get("path") or "").strip()
        or str(source_ref.get("record_ref") or "") != str(dependency.get("record_id") or "")
        or source_ref.get("source_kind") != "physical_bank"
    ):
        errors.append("Manual financial dependency requires an exact physical-bank statement ref binding.")

    if not isinstance(dependency.get("target"), dict):
        errors.append("Manual financial dependency target must be an object.")
    if not isinstance(dependency.get("blocking"), bool):
        errors.append("Manual financial dependency blocking flag must be boolean.")

    proof = dependency.get("statement_import_proof")
    if not isinstance(proof, dict) or proof.get("status") not in {"pending", "verified"} or (
        proof.get("required_evidence") != "live_discovery_or_audit"
    ):
        errors.append("Manual financial dependency requires a statement import proof contract.")
    elif proof.get("status") == "verified" and (
        not str(proof.get("simplbooks_transaction_id") or "").strip()
        or not isinstance(proof.get("evidence_binding"), dict)
        or set(proof.get("evidence_binding") or {}) != {"path", "sha256"}
    ):
        errors.append("Verified statement import proof requires a SimplBooks transaction ID and evidence binding.")
    elif proof.get("status") == "pending" and dependency.get("blocking") is not True:
        errors.append("Pending statement import proof must remain blocking.")
    elif dependency.get("blocking") is False and proof.get("status") != "verified":
        errors.append("Only verified statement import proof may be non-blocking.")

    split_parts = dependency.get("split_parts")
    split_proof = dependency.get("split_proof")
    if not isinstance(split_parts, list):
        errors.append("Manual financial dependency split_parts must be a list.")
    elif split_parts:
        if str(dependency.get("disposition") or "") != "reviewed_split":
            errors.append("Manual financial dependency with split parts must use reviewed_split disposition.")
        signed_total = Decimal("0")
        signed_part_amounts: list[Decimal] = []
        has_manual_financial_part = False
        for part in split_parts:
            if not isinstance(part, dict) or not str(part.get("disposition") or "") or not isinstance(part.get("target"), dict):
                errors.append("Manual financial dependency split parts require disposition and target.")
                continue
            try:
                if part.get("signed_amount") in (None, ""):
                    raise ValueError
                part_amount = decimal_value(part.get("signed_amount"))
                signed_total += part_amount
                signed_part_amounts.append(part_amount)
            except (SimplbooksError, ValueError):
                errors.append("Manual financial dependency split part requires an exact signed amount.")
                continue
            disposition = str(part.get("disposition") or "")
            if disposition not in allowed_split_dispositions:
                errors.append("Manual financial dependency split part disposition is invalid.")
            if disposition in {"bank_fee_payment", "expense_reimbursement_payment", "clearing_transfer"}:
                has_manual_financial_part = True
            if disposition in {"generated_invoice_receipt", "existing_invoice_receipt", "direct_sale_receipt"} and part_amount <= 0:
                errors.append(f"Manual financial dependency {disposition} split part must be positive.")
            if disposition in {"generated_purchase_payment", "existing_purchase_payment", "bank_fee_payment", "expense_reimbursement_payment"} and part_amount >= 0:
                errors.append(f"Manual financial dependency {disposition} split part must be negative.")
        if not has_manual_financial_part:
            errors.append(
                "Manual financial dependency reviewed_split requires at least one manual-financial part."
            )
        if signed_total != physical_amount:
            errors.append("Manual financial dependency split parts do not sum to the physical signed amount.")
        try:
            split_proof_valid = (
                isinstance(split_proof, dict)
                and split_proof.get("signed_parts_total") not in (None, "")
                and split_proof.get("physical_signed_amount") not in (None, "")
                and bool(str(split_proof.get("equation") or "").strip())
                and decimal_value(split_proof.get("signed_parts_total")) == signed_total
                and decimal_value(split_proof.get("physical_signed_amount")) == physical_amount
                and str(split_proof.get("equation") or "")
                == " + ".join(f"{amount:.2f}" for amount in signed_part_amounts) + f" = {physical_amount:.2f}"
            )
        except SimplbooksError:
            split_proof_valid = False
        if not split_proof_valid:
            errors.append("Manual financial dependency split proof does not prove the signed arithmetic.")
    elif split_proof is not None:
        errors.append("Manual financial dependency without split parts must not carry split proof.")

    disposition = str(dependency.get("disposition") or "")
    if not split_parts and disposition == "reviewed_split":
        errors.append("Manual financial dependency reviewed_split disposition requires split parts.")
    if (
        not split_parts
        and disposition in {"generated_invoice_receipt", "existing_invoice_receipt", "direct_sale_receipt"}
        and physical_amount <= 0
    ):
        errors.append(f"Manual financial dependency {disposition} amount must be positive.")
    if (
        not split_parts
        and disposition in {"generated_purchase_payment", "existing_purchase_payment", "bank_fee_payment", "expense_reimbursement_payment"}
        and physical_amount >= 0
    ):
        errors.append(f"Manual financial dependency {disposition} amount must be negative.")
    if isinstance(proof, dict) and proof.get("status") == "verified" and not errors:
        if cwd is None and require_typed_context:
            errors.append("Verified statement import proof requires typed evidence validation context.")
        elif cwd is not None:
            try:
                evidence = load_bound_evidence(proof.get("evidence_binding"), cwd=cwd)
            except StatementImportEvidenceError as exc:
                errors.append(str(exc))
            else:
                errors.extend(evidence_identity_errors(
                    evidence, dependency=dependency, expected_company_id=expected_company_id,
                    expected_transaction_id=str(proof.get("simplbooks_transaction_id") or ""),
                ))
                if not errors:
                    if not discovery_payloads:
                        errors.append(
                            "Verified statement import proof requires fresh bound SimplBooks discovery evidence."
                        )
                    else:
                        errors.extend(discovery_cash_evidence_errors(
                            evidence, discovery_payloads=discovery_payloads,
                            require_fresh=True,
                        ))
    return errors


def load_bound_discovery_payloads(
    action_batch: dict[str, Any], *, cwd: Path, expected_company_id: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if expected_company_id is None:
        return [], ["Fresh bound discovery validation requires the SimplBooks company ID."]
    payloads: list[dict[str, Any]] = []
    errors: list[str] = []
    bindings = [
        item for item in action_batch.get("reference_artifacts") or []
        if isinstance(item, dict) and item.get("kind") == "discovery_overview"
    ]
    if not bindings:
        return [], ["Action batch has no bound SimplBooks discovery overview."]
    for binding in bindings:
        try:
            path = verify_file_binding(binding, cwd=cwd)
            payload = load_json(path)
            validate_discovery(
                payload, year=int(payload.get("year") or 0),
                company_id=expected_company_id,
            )
        except (ReferenceArtifactError, SimplbooksError, OSError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
            continue
        payloads.append(payload)
    return payloads, errors


def manual_financial_source_errors(
    dependency: dict[str, Any],
    *,
    action_path: Path,
    cwd: Path,
    payload_cache: dict[Path, dict[str, Any]],
    index_cache: dict[Path, dict[str, tuple[str, dict[str, Any]]]],
) -> list[str]:
    source_ref = dependency.get("source_ref")
    if not isinstance(source_ref, dict):
        return ["Manual financial dependency source ref cannot be resolved."]
    path_text = str(source_ref.get("path") or "")
    record_ref = str(source_ref.get("record_ref") or "")
    if not path_text or not record_ref:
        return ["Manual financial dependency record_id/source ref cannot be resolved."]
    try:
        source_path = resolve_path(path_text, cwd=cwd, action_path=action_path)
        _payload, record_index = load_record_payload(
            source_path,
            cache=payload_cache,
            index_cache=index_cache,
        )
    except (OSError, json.JSONDecodeError, SimplbooksError) as exc:
        return [f"Manual financial dependency source ref cannot be resolved: {exc}"]
    resolved = (record_index or {}).get(record_ref)
    if resolved is None:
        return ["Manual financial dependency record_id/source ref does not resolve to a normalized record."]
    category, record = resolved
    errors: list[str] = []
    if category != "bank_transactions" or str(record.get("source_system") or "") != "bank":
        errors.append("Manual financial dependency source ref is not a normalized physical bank record.")
        return errors
    if str(record.get("record_id") or "") != str(dependency.get("record_id") or ""):
        errors.append("Manual financial dependency record_id does not match the normalized physical bank record.")
    try:
        normalized_statement_id = statement_identity(record)
        normalized_iban, normalized_currency = bank_ledger_key(record)
    except BankAllocationError as exc:
        return [f"Manual financial dependency physical bank identity is invalid: {exc}"]
    if normalized_statement_id != str(dependency.get("statement_id") or ""):
        errors.append("Manual financial dependency statement_id does not match the normalized physical bank record.")
    if str(record.get("event_date") or "") != str(dependency.get("date") or ""):
        errors.append("Manual financial dependency date does not match the normalized physical bank record.")
    try:
        dependency_amount = decimal_value(dependency.get("physical_signed_amount"))
        record_amount = decimal_value(record.get("gross_amount"))
        if dependency_amount != record_amount:
            errors.append("Manual financial dependency signed amount does not match the normalized physical bank record.")
    except SimplbooksError as exc:
        errors.append(f"Manual financial dependency signed amount cannot be compared: {exc}")
    if normalized_iban != str(dependency.get("iban") or ""):
        errors.append("Manual financial dependency IBAN does not match the normalized physical bank record.")
    if normalized_currency != str(dependency.get("currency") or ""):
        errors.append("Manual financial dependency currency does not match the normalized physical bank record.")
    return errors


def evaluate_unresolved_dependencies(
    action_batch: dict[str, Any],
    *,
    action_path: Path | None = None,
    cwd: Path | None = None,
    payload_cache: dict[Path, dict[str, Any]] | None = None,
    index_cache: dict[Path, dict[str, tuple[str, dict[str, Any]]]] | None = None,
    expected_company_id: str | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    discovery_payloads: list[dict[str, Any]] = []
    discovery_errors: list[str] = []
    if cwd is not None and any(
        isinstance(item, dict)
        and item.get("kind") == "manual_statement_import_financial_transaction"
        and (item.get("statement_import_proof") or {}).get("status") == "verified"
        for item in action_batch.get("unresolved_dependencies") or []
    ):
        discovery_payloads, discovery_errors = load_bound_discovery_payloads(
            action_batch, cwd=cwd, expected_company_id=expected_company_id,
        )
    for dependency in action_batch.get("unresolved_dependencies") or []:
        if str(dependency.get("kind") or "") == "manual_statement_import_financial_transaction":
            dependency_errors = manual_financial_dependency_errors(
                dependency, cwd=cwd, expected_company_id=expected_company_id,
                require_typed_context=True, discovery_payloads=discovery_payloads,
            )
            dependency_errors.extend(discovery_errors)
            if action_path is not None and cwd is not None:
                dependency_errors.extend(
                    manual_financial_source_errors(
                        dependency,
                        action_path=action_path,
                        cwd=cwd,
                        payload_cache=payload_cache if payload_cache is not None else {},
                        index_cache=index_cache if index_cache is not None else {},
                    )
                )
            for error in dependency_errors:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary=error,
                        action_id=str(dependency.get("record_id") or "") or None,
                    )
                )
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
            if str(line.get("line_role") or "") == "direct_sale_revenue":
                continue
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
            if len(evidence) != 1:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="VAT profile API line must represent exactly one order component.",
                        action_id=action_label(action),
                    )
                )
                continue
            evidence_binding = line.get("vat_evidence_binding")
            allocation_ref = (
                evidence_binding.get("allocation_ref")
                if isinstance(evidence_binding, dict)
                else None
            )
            tax_source_refs = (
                evidence_binding.get("tax_source_refs")
                if isinstance(evidence_binding, dict)
                else None
            )
            valid_allocation_ref = (
                isinstance(allocation_ref, dict)
                and bool(str(allocation_ref.get("path") or "").strip())
                and bool(re.fullmatch(r"[a-f0-9]{64}", str(allocation_ref.get("sha256") or "")))
            )
            valid_tax_refs = isinstance(tax_source_refs, list) and bool(tax_source_refs)
            if valid_tax_refs:
                valid_tax_refs = all(
                    isinstance(item, dict)
                    and bool(str(item.get("source_id") or "").strip())
                    and bool(str(item.get("path") or "").strip())
                    and bool(re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256") or "")))
                    and isinstance(item.get("row_refs"), list)
                    and bool(item.get("row_refs"))
                    for item in tax_source_refs
                )
            if not valid_allocation_ref or not valid_tax_refs:
                findings.append(
                    make_finding(
                        section="account_and_vat_review",
                        severity="error",
                        summary="VAT profile API line lacks a usable allocation and tax-source evidence binding.",
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
                    item_profile = item.get("vat_profile")
                    if not isinstance(item_profile, dict):
                        raise SimplbooksError("component evidence lacks VAT profile provenance")
                    item_event_date = date.fromisoformat(str(item.get("event_date") or ""))
                    item_profile_start = date.fromisoformat(str(item_profile.get("start") or ""))
                    item_profile_end = (
                        date.fromisoformat(str(item_profile["end"]))
                        if item_profile.get("end") not in (None, "")
                        else None
                    )
                    if item_event_date < item_profile_start or (
                        item_profile_end is not None and item_event_date > item_profile_end
                    ):
                        raise SimplbooksError("component evidence event date is outside its VAT profile")
                    if (item_event_date.year, item_event_date.month) != (event_date.year, event_date.month):
                        raise SimplbooksError("component evidence event date is outside the action period")
                    item_policy_profile = resolve_sales_vat_profile(
                        posting_policy,
                        event_date=item_event_date,
                    )
                    item_vat_type_id = item_profile.get(
                        "shipping_vat_type_id" if is_shipping else "goods_vat_type_id"
                    )
                    item_policy_vat_type_id = item_policy_profile[
                        "shipping_vat_type_id" if is_shipping else "goods_vat_type_id"
                    ]
                    if (
                        decimal_value(item_profile.get("rate")) != rate
                        or str(item_vat_type_id or "") != expected_vat_type_id
                        or Decimal(str(item_policy_profile["rate"])) != rate
                        or str(item_policy_vat_type_id or "") != expected_vat_type_id
                    ):
                        raise SimplbooksError("component evidence VAT profile provenance does not match policy")
                    if item_gross != item_gross.quantize(TOLERANCE) or item_vat != item_vat.quantize(TOLERANCE):
                        raise SimplbooksError("component evidence amounts must use whole cents")
                    evidence_gross += item_gross
                    evidence_vat += item_vat
                    expected_item_vat = (item_gross * rate / (Decimal("100") + rate)).quantize(
                        TOLERANCE, rounding=ROUND_HALF_UP
                    )
                    rounding_error = rounding_error or item_vat != expected_item_vat
            except (InvalidOperation, PostingPolicyError, SimplbooksError, ValueError) as exc:
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
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in bindings:
        if isinstance(item, dict):
            by_kind[str(item.get("kind") or "")].append(item)
    required = required_action_binding_kinds(action_batch)
    for kind in sorted(required):
        if not by_kind.get(kind):
            findings.append(make_finding(section="duplicate_risk", severity="error", summary=f"Action batch is not bound to required {kind} artifact."))
    for binding in bindings:
        if not isinstance(binding, dict):
            findings.append(make_finding(section="duplicate_risk", severity="error", summary="Action reference binding must be an object."))
            continue
        kind = str(binding.get("kind") or "")
        try:
            path = verify_file_binding(binding, cwd=cwd)
            if kind == "discovery_overview":
                if expected_company_id is None:
                    raise ReferenceArtifactError("Cannot verify discovery without company metadata ID.")
                validate_discovery(
                    (overview := load_json(path)),
                    year=int(overview.get("year") or 0),
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

    assigned_cash_amounts: dict[str, Decimal] = {}
    findings.extend(evaluate_duplicates(action_batch))
    findings.extend(
        evaluate_bank_statement_completeness(
            action_batch,
            action_path=action_path,
            cwd=cwd,
            posting_policy=posting_policy,
            assigned_cash_amounts=assigned_cash_amounts,
        )
    )
    findings.extend(evaluate_exchange_rates(action_batch, exchange_rate_cache=exchange_rate_cache))
    findings.extend(
        evaluate_unresolved_dependencies(
            action_batch,
            action_path=action_path,
            cwd=cwd,
            payload_cache=payload_cache,
            index_cache=index_cache,
            expected_company_id=expected_company_id,
        )
    )
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
    reviewed_allocations, allocation_findings = load_reviewed_allocation_index(action_batch, cwd=cwd)
    findings.extend(allocation_findings)
    split_payment_action_ids, split_payment_findings = evaluate_split_payment_groups(
        action_batch=action_batch,
        resolved_sources_by_action=resolved_sources_by_action,
        reviewed_assignment_action_ids=set(assigned_cash_amounts),
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
                physical_expected_amount=assigned_cash_amounts.get(action_label(action)),
            )
        )
        findings.extend(
            evaluate_inventory_quantities(
                action=action,
                resolved_sources=resolved_sources,
                reviewed_allocations=reviewed_allocations,
            )
        )
        findings.extend(
            evaluate_account_vat(
                action=action,
                batch_approved=str(action_batch.get("approval_status") or "") in {"approved", "submitted"},
            )
        )
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


def validate_explicit_bank_allocation_path(
    *, action_batch: dict[str, Any], requested_path: Path, cwd: Path
) -> None:
    bound_paths = [
        Path(str(item.get("path") or ""))
        for item in action_batch.get("reference_artifacts") or []
        if isinstance(item, dict) and item.get("kind") == "bank_allocations"
    ]
    requested = requested_path.resolve()
    if bound_paths and not any(
        (path if path.is_absolute() else cwd / path).resolve() == requested
        for path in bound_paths
    ):
        raise SimplbooksError("Explicit bank allocation path does not match the action batch binding.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a pre-submit review over a draft Simplbooks action batch")
    parser.add_argument("--company-dir", help="Company folder, e.g. companies/example")
    parser.add_argument("--period", required=True, help="Target month in YYYY-MM format")
    parser.add_argument("--actions", help="Path to actions YAML. Defaults to companies/<company>/artifacts/actions/<period>.yaml")
    parser.add_argument("--recon", help="Path to recon JSON. Defaults to companies/<company>/artifacts/recon/<period>.json")
    parser.add_argument("--policy-memo", help="Optional path to policy memo markdown")
    parser.add_argument("--exchange-rates", help="Annual ECB cache used to independently verify foreign rates")
    parser.add_argument("--posting-policy", help="Explicit posting policy used to independently verify every submit-capable ID")
    parser.add_argument(
        "--bank-allocations",
        help="Reviewed annual bank allocation artifact; the action batch's hash binding remains authoritative",
    )
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
    if args.bank_allocations:
        validate_explicit_bank_allocation_path(
            action_batch=action_batch,
            requested_path=Path(args.bank_allocations),
            cwd=Path.cwd(),
        )
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
