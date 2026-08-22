"""Typed, immutable evidence for manual SimplBooks statement-import transactions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from reference_artifacts import ReferenceArtifactError, validate_discovery


class StatementImportEvidenceError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _resolved(path_value: Any, *, cwd: Path) -> Path:
    path = Path(_text(path_value))
    return path if path.is_absolute() else cwd / path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_binding(binding: Any, *, label: str) -> dict[str, str]:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise StatementImportEvidenceError(f"{label} must contain only path and sha256.")
    path = _text(binding.get("path"))
    sha = _text(binding.get("sha256"))
    if not path or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise StatementImportEvidenceError(f"{label} requires a path and SHA-256 hash.")
    return {"path": path, "sha256": sha}


def _validate_source_identity(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "record_ref"}:
        raise StatementImportEvidenceError(
            f"{label} must contain only path, sha256, and record_ref."
        )
    binding = _validate_binding(
        {"path": value.get("path"), "sha256": value.get("sha256")}, label=label
    )
    record_ref = _text(value.get("record_ref"))
    if not record_ref:
        raise StatementImportEvidenceError(f"{label} requires record_ref.")
    return {**binding, "record_ref": record_ref}


def validate_evidence_shape(payload: Any) -> dict[str, Any]:
    required = {
        "schema_version", "company_slug", "company_id", "period", "statement_id", "record_id",
        "transaction_date", "iban", "currency", "signed_amount", "simplbooks_transaction_id",
        "evidence_kind", "captured_at", "source_identity", "evidence_source",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise StatementImportEvidenceError("Statement-import evidence has missing or unsupported fields.")
    if payload.get("schema_version") != "1.0":
        raise StatementImportEvidenceError("Statement-import evidence schema_version must be 1.0.")
    if not re.fullmatch(r"[a-z0-9-]+", _text(payload.get("company_slug"))):
        raise StatementImportEvidenceError("Statement-import evidence company_slug is invalid.")
    for field in ("company_id", "statement_id", "record_id", "iban", "simplbooks_transaction_id"):
        if not _text(payload.get(field)):
            raise StatementImportEvidenceError(f"Statement-import evidence requires {field}.")
    if not re.fullmatch(r"\d{4}-\d{2}", _text(payload.get("period"))):
        raise StatementImportEvidenceError("Statement-import evidence period is invalid.")
    try:
        datetime.strptime(_text(payload.get("transaction_date")), "%Y-%m-%d")
        datetime.fromisoformat(_text(payload.get("captured_at")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise StatementImportEvidenceError("Statement-import evidence date/timestamp is invalid.") from exc
    if not re.fullmatch(r"[A-Z]{3}", _text(payload.get("currency"))):
        raise StatementImportEvidenceError("Statement-import evidence currency is invalid.")
    try:
        amount = Decimal(str(payload.get("signed_amount")))
    except (InvalidOperation, ValueError) as exc:
        raise StatementImportEvidenceError("Statement-import evidence signed_amount is invalid.") from exc
    if not amount.is_finite() or amount.quantize(Decimal("0.01")) != amount:
        raise StatementImportEvidenceError("Statement-import evidence signed_amount must be cent exact.")
    if payload.get("evidence_kind") != "simplbooks_discovery":
        raise StatementImportEvidenceError("Statement-import evidence kind is unsupported.")
    _validate_source_identity(payload.get("source_identity"), label="source_identity")
    _validate_source_identity(payload.get("evidence_source"), label="evidence_source")
    return payload


def discovery_cash_evidence_errors(
    evidence: dict[str, Any], *, discovery_payloads: list[dict[str, Any]],
    now: datetime | None = None, require_fresh: bool = True,
) -> list[str]:
    """Match typed evidence to one concrete SimplBooks cash discovery entry."""
    errors: list[str] = []
    transaction_year = int(str(evidence.get("transaction_date") or "")[:4] or 0)
    matching_overviews: list[dict[str, Any]] = []
    for overview in discovery_payloads:
        if int(overview.get("year") or 0) != transaction_year:
            continue
        if require_fresh:
            try:
                validate_discovery(
                    overview, year=transaction_year,
                    company_id=_text(evidence.get("company_id")), now=now,
                )
            except (ReferenceArtifactError, ValueError) as exc:
                errors.append(f"Statement-import discovery evidence is not fresh/valid: {exc}")
                continue
        elif (
            _text(overview.get("company_id")) != _text(evidence.get("company_id"))
            or not _text(overview.get("retrieved_at"))
            or not isinstance(overview.get("document_index"), list)
        ):
            errors.append("Statement-import discovery evidence source is not a concrete SimplBooks overview.")
            continue
        matching_overviews.append(overview)
    candidates = [
        item
        for overview in matching_overviews
        for item in overview.get("document_index") or []
        if isinstance(item, dict)
        and item.get("document_type") in {"incoming", "payment"}
        and _text(item.get("simplbooks_id")) == _text(evidence.get("simplbooks_transaction_id"))
    ]
    if len(candidates) != 1:
        errors.append("Statement-import discovery must contain exactly one matching cash transaction.")
        return errors
    item = candidates[0]
    try:
        discovered_amount = Decimal(str(item.get("gross_amount")))
        evidence_amount = Decimal(str(evidence.get("signed_amount")))
    except (InvalidOperation, ValueError):
        errors.append("Statement-import discovery cash amount is invalid.")
        return errors
    if item.get("document_type") == "payment":
        discovered_amount = -abs(discovered_amount)
    if (
        _text(item.get("document_date")) != _text(evidence.get("transaction_date"))
        or _text(item.get("currency")) != _text(evidence.get("currency"))
        or discovered_amount != evidence_amount
    ):
        errors.append("Statement-import discovery cash transaction economics do not match evidence.")
    return errors


def load_bound_evidence(
    binding: Any, *, cwd: Path, now: datetime | None = None,
    require_fresh_discovery: bool = False,
) -> dict[str, Any]:
    checked = _validate_binding(binding, label="statement-import evidence binding")
    path = _resolved(checked["path"], cwd=cwd)
    if not path.is_file():
        raise StatementImportEvidenceError(f"Statement-import evidence does not exist: {path}")
    if _sha256(path) != checked["sha256"]:
        raise StatementImportEvidenceError("Statement-import evidence SHA-256 binding does not match.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatementImportEvidenceError(f"Unable to parse statement-import evidence: {exc}") from exc
    validate_evidence_shape(payload)
    for field in ("source_identity", "evidence_source"):
        item = payload[field]
        source_path = _resolved(item["path"], cwd=cwd)
        if not source_path.is_file() or _sha256(source_path) != item["sha256"]:
            raise StatementImportEvidenceError(f"Statement-import {field} file/hash binding is invalid.")
    source_path = _resolved(payload["evidence_source"]["path"], cwd=cwd)
    try:
        discovery_payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatementImportEvidenceError(f"Statement-import discovery evidence cannot be parsed: {exc}") from exc
    discovery_errors = discovery_cash_evidence_errors(
        payload, discovery_payloads=[discovery_payload], now=now or datetime.now(UTC),
        require_fresh=require_fresh_discovery,
    )
    if discovery_errors:
        raise StatementImportEvidenceError(discovery_errors[0])
    return payload


def evidence_identity_errors(
    evidence: dict[str, Any], *, dependency: dict[str, Any], expected_company_id: str | None,
    expected_transaction_id: str,
) -> list[str]:
    errors: list[str] = []
    comparisons = (
        ("statement ID", evidence.get("statement_id"), dependency.get("statement_id")),
        ("record ID", evidence.get("record_id"), dependency.get("record_id")),
        ("transaction date", evidence.get("transaction_date"), dependency.get("date")),
        ("IBAN", evidence.get("iban"), dependency.get("iban")),
        ("currency", evidence.get("currency"), dependency.get("currency")),
        ("transaction ID", evidence.get("simplbooks_transaction_id"), expected_transaction_id),
    )
    for label, actual, expected in comparisons:
        if _text(actual) != _text(expected):
            errors.append(f"Statement-import evidence {label} does not match reviewed dependency.")
    if expected_company_id is not None and _text(evidence.get("company_id")) != _text(expected_company_id):
        errors.append("Statement-import evidence company ID does not match company metadata.")
    try:
        if Decimal(str(evidence.get("signed_amount"))) != Decimal(str(dependency.get("physical_signed_amount"))):
            errors.append("Statement-import evidence signed amount does not match reviewed dependency.")
    except InvalidOperation:
        errors.append("Statement-import evidence signed amount cannot be compared.")
    if _text(evidence.get("period")) != _text(dependency.get("date"))[:7]:
        errors.append("Statement-import evidence period does not match transaction date.")
    source_identity = evidence.get("source_identity") or {}
    if _text(source_identity.get("record_ref")) != _text(dependency.get("record_id")):
        errors.append("Statement-import evidence source record identity does not match reviewed dependency.")
    evidence_source = evidence.get("evidence_source") or {}
    if _text(evidence_source.get("record_ref")) != expected_transaction_id:
        errors.append("Statement-import evidence source transaction identity does not match proof.")
    return errors
