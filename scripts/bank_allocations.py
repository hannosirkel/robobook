"""Validate source-bound, reviewed allocations for physical bank statement rows."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from statement_import_evidence import (
    StatementImportEvidenceError,
    evidence_identity_errors,
    load_bound_evidence,
)


DISPOSITIONS = {
    "generated_invoice_receipt",
    "existing_invoice_receipt",
    "generated_purchase_payment",
    "existing_purchase_payment",
    "direct_sale_receipt",
    "bank_fee_payment",
    "expense_reimbursement_payment",
    "clearing_transfer",
    "reviewed_split",
}
CENT = Decimal("0.01")


class BankAllocationError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _attribute_text(record: dict[str, Any], *names: str) -> str:
    attributes = record.get("attributes") or {}
    if not isinstance(attributes, dict):
        return ""
    for name in names:
        value = _text(attributes.get(name))
        if value:
            return value
    return ""


def _decimal(value: Any, *, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BankAllocationError(f"{label} must be a decimal amount.") from exc
    if not number.is_finite() or number.quantize(CENT, rounding=ROUND_HALF_UP) != number:
        raise BankAllocationError(f"{label} must be exact to €0.01.")
    return number


def _currency(record: dict[str, Any]) -> str:
    currency = _text(record.get("currency")).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise BankAllocationError(f"Bank record {record.get('record_id') or '<unknown>'} has invalid currency.")
    return currency


def _normalized_iban(value: Any, *, label: str) -> str:
    iban = re.sub(r"\s+", "", _text(value)).upper()
    if not iban:
        raise BankAllocationError(f"{label} requires an IBAN or source account.")
    return iban


def _economic_tuple(record: dict[str, Any]) -> tuple[str, str, Decimal]:
    event_date = _text(record.get("event_date"))
    return event_date, _currency(record), _decimal(record.get("gross_amount"), label="Bank record gross_amount")


def bank_ledger_key(record: dict[str, Any]) -> tuple[str, str]:
    """Return the normalized physical-bank ledger key of `(IBAN, currency)`."""
    if _text(record.get("source_system")) != "bank":
        raise BankAllocationError("Bank ledger key requires a physical bank record.")
    iban = _normalized_iban(
        _attribute_text(record, "iban", "account_iban", "customer_account"),
        label="Physical bank record",
    )
    return iban, _currency(record)


def allocation_key(allocation: dict[str, Any]) -> tuple[str, str, str]:
    """Return the canonical `(statement_id, IBAN, currency)` allocation key."""
    statement_id = _text(allocation.get("statement_id"))
    if not statement_id:
        raise BankAllocationError("Bank allocation requires statement_id.")
    iban = _normalized_iban(allocation.get("iban"), label="Bank allocation")
    currency = _text(allocation.get("currency")).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise BankAllocationError("Bank allocation currency is invalid.")
    return statement_id, iban, currency


def statement_identity(record: dict[str, Any]) -> str:
    """Return the strongest immutable identity available for one bank record."""
    archive = _attribute_text(record, "archive_identifier", "archive_id", "archiving_identifier") or _text(
        record.get("external_ref")
    )
    if archive:
        return f"archive:{archive}"
    account_servicer = _attribute_text(
        record, "account_servicer_reference", "account_servicer_ref", "acct_svcr_ref"
    )
    if account_servicer:
        return f"account-servicer:{account_servicer}"
    entry = _attribute_text(record, "entry_reference", "entry_ref", "ntry_ref")
    if entry:
        return f"entry:{entry}"

    iban = _attribute_text(record, "iban", "account_iban", "customer_account")
    event_date, currency, amount = _economic_tuple(record)
    counterparty = _attribute_text(record, "counterparty_name", "counterparty", "beneficiary_name")
    description = " ".join(_text(record.get("description")).split())
    return f"composite:{iban}|{currency}|{event_date}|{amount:.2f}|{counterparty}|{description}"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BankAllocationError(f"Unable to read bank allocation JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BankAllocationError("Bank allocation artifact must be an object.")
    return payload


def _path_key(path: Path) -> str:
    return str(path.resolve())


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_normalized_bindings(payload: dict[str, Any], normalized_year_paths: list[Path]) -> None:
    bindings = payload.get("normalized_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise BankAllocationError("Bank allocations require normalized_bindings.")
    expected = {_path_key(path): _hash(path) for path in normalized_year_paths}
    actual: dict[str, str] = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            raise BankAllocationError("Normalized binding must be an object.")
        path = Path(_text(binding.get("path")))
        sha256 = _text(binding.get("sha256"))
        if not path or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise BankAllocationError("Normalized binding requires a path and SHA-256 hash.")
        key = _path_key(path)
        if key in actual:
            raise BankAllocationError(f"Normalized binding is duplicated: {path}")
        actual[key] = sha256
    if set(actual) != set(expected):
        raise BankAllocationError("Normalized bindings do not exactly match the supplied annual normalized inputs.")
    for path, expected_hash in expected.items():
        if actual[path] != expected_hash:
            raise BankAllocationError(f"Normalized input changed after allocation review: {path}")


def _bank_records(paths: list[Path], *, year: int) -> dict[tuple[str, str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in paths:
        payload = _load_json(path)
        period = _text(payload.get("period"))
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", period) or int(period[:4]) != year:
            raise BankAllocationError(f"Normalized input {path} does not belong to allocation year {year}.")
        records = ((payload.get("records") or {}).get("bank_transactions") or [])
        if not isinstance(records, list):
            raise BankAllocationError(f"Normalized input {path} has invalid bank_transactions.")
        for record in records:
            if not isinstance(record, dict):
                raise BankAllocationError(f"Normalized input {path} has a non-object bank record.")
            if _text(record.get("source_system")) != "bank":
                continue
            identity = statement_identity(record)
            iban, currency = bank_ledger_key(record)
            key = identity, iban, currency
            if key in indexed:
                raise BankAllocationError(f"Bank allocation key occurs in multiple normalized bank records: {key}")
            indexed[key] = record
    return indexed


def validate_unique_record_ids(allocations: list[dict[str, Any]]) -> None:
    allocation_keys: set[tuple[str, str, str]] = set()
    record_ids: set[str] = set()
    for allocation in allocations:
        if not isinstance(allocation, dict):
            raise BankAllocationError("Bank allocation must be an object.")
        record_id = _text(allocation.get("record_id"))
        key = allocation_key(allocation)
        if not record_id:
            raise BankAllocationError("Bank allocation requires record_id.")
        if key in allocation_keys:
            raise BankAllocationError(f"Bank allocation key is duplicated: {key}")
        if record_id in record_ids:
            raise BankAllocationError(f"Bank allocation record_id is duplicated: {record_id}")
        allocation_keys.add(key)
        record_ids.add(record_id)


def allocation_amounts(allocation: dict[str, Any]) -> list[Decimal]:
    if _text(allocation.get("disposition")) == "reviewed_split":
        parts = allocation.get("parts")
        if not isinstance(parts, list) or not parts:
            raise BankAllocationError("reviewed_split allocation requires non-empty parts.")
        amounts: list[Decimal] = []
        for part in parts:
            if not isinstance(part, dict):
                raise BankAllocationError("reviewed_split parts must be objects.")
            amounts.append(_decimal(part.get("amount"), label="reviewed_split part amount"))
        return amounts
    return [_decimal(allocation.get("amount"), label="Bank allocation amount")]


def validate_reviewed_amounts(allocations: list[dict[str, Any]]) -> None:
    for allocation in allocations:
        disposition = _text(allocation.get("disposition"))
        if disposition not in DISPOSITIONS:
            raise BankAllocationError(f"Unsupported bank allocation disposition: {disposition or '<empty>'}")
        amount = _decimal(allocation.get("amount"), label="Bank allocation amount")
        amounts = allocation_amounts(allocation)
        if disposition == "reviewed_split":
            if sum(amounts, Decimal("0")).quantize(CENT) != amount.quantize(CENT):
                raise BankAllocationError("reviewed_split part amounts must sum to the allocation amount at €0.01 precision.")
        elif "parts" in allocation:
            raise BankAllocationError("Only reviewed_split allocations may contain parts.")


def _validate_shape(payload: dict[str, Any]) -> list[dict[str, Any]]:
    required = {"schema_version", "company_slug", "year", "normalized_bindings", "allocations"}
    if set(payload) != required:
        raise BankAllocationError("Bank allocation artifact has missing or unsupported top-level fields.")
    if payload.get("schema_version") != "1.0":
        raise BankAllocationError("Bank allocation schema_version must be 1.0.")
    if not re.fullmatch(r"[a-z0-9-]+", _text(payload.get("company_slug"))):
        raise BankAllocationError("Bank allocation company_slug is invalid.")
    if not isinstance(payload.get("year"), int) or isinstance(payload.get("year"), bool) or not 1000 <= payload["year"] <= 9999:
        raise BankAllocationError("Bank allocation year must be an integer.")
    allocations = payload.get("allocations")
    if not isinstance(allocations, list):
        raise BankAllocationError("Bank allocations must be a list.")
    allowed = {"statement_id", "record_id", "iban", "period", "disposition", "amount", "currency", "target", "parts", "review"}
    required_allocation = allowed - {"parts"}
    for allocation in allocations:
        if not isinstance(allocation, dict) or not required_allocation.issubset(allocation) or set(allocation) - allowed:
            raise BankAllocationError("Bank allocation has missing or unsupported fields.")
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", _text(allocation.get("period"))):
            raise BankAllocationError("Bank allocation period is invalid.")
        if int(str(allocation["period"])[:4]) != payload["year"]:
            raise BankAllocationError("Bank allocation period does not belong to the allocation year.")
        if not re.fullmatch(r"[A-Z]{3}", _text(allocation.get("currency"))):
            raise BankAllocationError("Bank allocation currency is invalid.")
        allocation_key(allocation)
        if not isinstance(allocation.get("target"), dict) or not allocation["target"]:
            raise BankAllocationError("Bank allocation target must be a non-empty object.")
        proof = allocation["target"].get("statement_import_proof")
        if proof is not None:
            required_proof = {"status", "required_evidence", "simplbooks_transaction_id", "evidence_binding"}
            if not isinstance(proof, dict) or set(proof) != required_proof:
                raise BankAllocationError(
                    "statement_import_proof must be a complete reviewed proof object."
                )
            if (
                proof.get("status") != "verified"
                or proof.get("required_evidence") != "live_discovery_or_audit"
                or not _text(proof.get("simplbooks_transaction_id"))
                or not isinstance(proof.get("evidence_binding"), dict)
            ):
                raise BankAllocationError("statement_import_proof is not verified and complete.")
        review = allocation.get("review")
        if not isinstance(review, dict) or set(review) != {"status", "rationale"}:
            raise BankAllocationError("Bank allocation review must contain only status and rationale.")
        if review.get("status") != "approved" or not _text(review.get("rationale")):
            raise BankAllocationError("Bank allocation review must be approved with a rationale.")
    return allocations


def _validate_against_normalized(
    allocations: list[dict[str, Any]], records: dict[tuple[str, str, str], dict[str, Any]], *, year: int
) -> None:
    for allocation in allocations:
        key = allocation_key(allocation)
        statement_id = key[0]
        record = records.get(key)
        if record is None:
            raise BankAllocationError(f"Bank allocation key is not present in the normalized inputs: {key}")
        if _text(record.get("record_id")) != _text(allocation.get("record_id")):
            raise BankAllocationError(f"Bank allocation record_id is not the current locator for {statement_id}")
        event_date, currency, amount = _economic_tuple(record)
        try:
            event_year = date.fromisoformat(event_date).year
        except ValueError as exc:
            raise BankAllocationError(f"Bank statement date is invalid for {statement_id}") from exc
        if event_year != year:
            raise BankAllocationError(f"Bank statement date does not belong to allocation year for {statement_id}")
        if _text(allocation.get("period")) != event_date[:7]:
            raise BankAllocationError(f"Bank allocation period does not match statement date for {statement_id}")
        if key[2] != currency or _decimal(allocation.get("amount"), label="Bank allocation amount") != amount:
            raise BankAllocationError(f"Bank allocation economic fields do not match statement row {statement_id}")


def load_bank_allocations(path: Path, *, normalized_year_paths: list[Path]) -> dict[str, Any]:
    """Load only an approved allocation artifact bound to current normalized inputs."""
    payload = _load_json(path)
    allocations = _validate_shape(payload)
    verify_normalized_bindings(payload, normalized_year_paths)
    validate_unique_record_ids(allocations)
    validate_reviewed_amounts(allocations)
    _validate_against_normalized(
        allocations, _bank_records(normalized_year_paths, year=payload["year"]), year=payload["year"]
    )
    indexed_records = _bank_records(normalized_year_paths, year=payload["year"])
    normalized_by_record_id: dict[str, Path] = {}
    for normalized_path in normalized_year_paths:
        for record in ((_load_json(normalized_path).get("records") or {}).get("bank_transactions") or []):
            if isinstance(record, dict) and _text(record.get("source_system")) == "bank":
                normalized_by_record_id[_text(record.get("record_id"))] = normalized_path
    for allocation in allocations:
        proof = (allocation.get("target") or {}).get("statement_import_proof")
        if proof is None:
            continue
        try:
            evidence = load_bound_evidence(proof["evidence_binding"], cwd=Path.cwd())
        except StatementImportEvidenceError as exc:
            raise BankAllocationError(str(exc)) from exc
        dependency = {
            "statement_id": allocation.get("statement_id"), "record_id": allocation.get("record_id"),
            "date": str(allocation.get("period")) + "-01", "iban": allocation.get("iban"),
            "currency": allocation.get("currency"), "physical_signed_amount": allocation.get("amount"),
        }
        record = indexed_records[allocation_key(allocation)]
        dependency["date"] = record.get("event_date")
        errors = evidence_identity_errors(
            evidence, dependency=dependency, expected_company_id=None,
            expected_transaction_id=_text(proof.get("simplbooks_transaction_id")),
        )
        normalized_path = normalized_by_record_id.get(_text(allocation.get("record_id")))
        source_identity = evidence.get("source_identity") or {}
        if normalized_path is None or Path(_text(source_identity.get("path"))).resolve() != normalized_path.resolve():
            errors.append("Statement-import evidence normalized source path does not match reviewed record.")
        elif _text(source_identity.get("sha256")) != _hash(normalized_path):
            errors.append("Statement-import evidence normalized source SHA does not match reviewed record.")
        if _text(evidence.get("company_slug")) != _text(payload.get("company_slug")):
            errors.append("Statement-import evidence company_slug does not match bank allocations.")
        if errors:
            raise BankAllocationError(errors[0])
    return payload


def period_allocations(payload: dict[str, Any], period: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    allocations = payload.get("allocations") or []
    if not isinstance(allocations, list):
        raise BankAllocationError("Bank allocations must be a list.")
    selected = [item for item in allocations if isinstance(item, dict) and item.get("period") == period]
    validate_unique_record_ids(selected)
    return {allocation_key(item): item for item in selected}


def bank_allocation_coverage_errors(
    payload: dict[str, Any], *, normalized_year_paths: list[Path]
) -> list[str]:
    """Return deterministic exact-coverage findings for physical bank statement identities.

    This deliberately remains separate from `load_bank_allocations()` during Phase A,
    allowing downstream reconciliation to report partial annual review artifacts.
    """
    allocations = _validate_shape(payload)
    verify_normalized_bindings(payload, normalized_year_paths)
    physical_keys = set(_bank_records(normalized_year_paths, year=payload["year"]))
    allocation_keys = [allocation_key(item) for item in allocations]
    unique_allocation_keys = set(allocation_keys)
    duplicates = sorted({key for key in allocation_keys if allocation_keys.count(key) > 1})
    missing = sorted(physical_keys - unique_allocation_keys)
    extra = sorted(unique_allocation_keys - physical_keys)
    errors: list[str] = []
    if duplicates:
        errors.append("duplicate bank allocation key(s): " + ", ".join(map(str, duplicates)))
    if missing:
        errors.append("missing bank allocation key(s): " + ", ".join(map(str, missing)))
    if extra:
        errors.append("extra bank allocation key(s): " + ", ".join(map(str, extra)))
    return errors


def prove_exact_bank_allocation_coverage(payload: dict[str, Any], *, normalized_year_paths: list[Path]) -> None:
    """Raise deterministic findings unless every physical statement ID has one allocation."""
    errors = bank_allocation_coverage_errors(payload, normalized_year_paths=normalized_year_paths)
    if errors:
        raise BankAllocationError("; ".join(errors))


def _bindings_for(paths: list[Path]) -> list[dict[str, str]]:
    return [{"path": str(path), "sha256": _hash(path)} for path in paths]


def rebind_bank_allocations(payload: dict[str, Any], normalized_year_paths: list[Path]) -> dict[str, Any]:
    """Refresh locators and hashes after proving statement identities and economics unchanged.

    The returned artifact deliberately has unapproved reviews and must be reviewed before loading.
    """
    allocations = _validate_shape(payload)
    validate_unique_record_ids(allocations)
    validate_reviewed_amounts(allocations)
    old_paths = [Path(_text(binding.get("path"))) for binding in payload.get("normalized_bindings") or []]
    verify_normalized_bindings(payload, old_paths)
    old_records = _bank_records(old_paths, year=payload["year"])
    new_records = _bank_records(normalized_year_paths, year=payload["year"])
    if set(old_records) != set(new_records):
        raise BankAllocationError("Cannot rebind because the statement-ID set changed.")
    for key in old_records:
        if _economic_tuple(old_records[key]) != _economic_tuple(new_records[key]):
            raise BankAllocationError(f"Cannot rebind because statement economics changed: {key}")
    rebound = copy.deepcopy(payload)
    rebound["normalized_bindings"] = _bindings_for(normalized_year_paths)
    for allocation in rebound["allocations"]:
        key = allocation_key(allocation)
        allocation["record_id"] = str(new_records[key].get("record_id") or "")
        allocation["review"]["status"] = "needs_review"
    return rebound
