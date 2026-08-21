from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any


class PostingPolicyError(RuntimeError):
    pass


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


def normalize_id(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        raise PostingPolicyError(f"{field_name} must be an integer-like Simplbooks ID, got {value!r}.")
    return text


def validate_posting_policy(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != "1.0":
        raise PostingPolicyError("Posting policy schema_version must be '1.0'.")
    if not slugify(payload.get("company_slug", "")):
        raise PostingPolicyError("Posting policy requires company_slug.")
    for section in ("bank_accounts", "contacts", "mappings", "supplier_aliases"):
        if not isinstance(payload.get(section), dict):
            raise PostingPolicyError(f"Posting policy requires object section {section!r}.")

    for source_account, account_id in payload["bank_accounts"].items():
        if not str(source_account).strip():
            raise PostingPolicyError("Posting policy bank account keys cannot be empty.")
        normalize_id(account_id, field_name=f"bank_accounts[{source_account!r}]")

    contacts = payload["contacts"]
    for role, mappings in contacts.items():
        if not isinstance(mappings, dict):
            raise PostingPolicyError(f"contacts[{role!r}] must be an object.")
        for label, contact_id in mappings.items():
            normalize_id(contact_id, field_name=f"contacts[{role!r}][{label!r}]")


def load_posting_policy(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PostingPolicyError(f"Could not load posting policy {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PostingPolicyError(f"Posting policy {path} must contain a JSON object.")
    validate_posting_policy(payload)
    return payload


def resolve_bank_account(policy: dict[str, Any], *, customer_account: str) -> str:
    source_account = re.sub(r"\s+", "", str(customer_account or "")).upper()
    mappings = policy.get("bank_accounts") or {}
    normalized_mappings = {
        re.sub(r"\s+", "", str(key)).upper(): value
        for key, value in mappings.items()
    }
    if source_account not in normalized_mappings:
        raise PostingPolicyError(f"No exact bank-account mapping exists for source account {customer_account!r}.")
    return normalize_id(normalized_mappings[source_account], field_name=f"bank_accounts[{customer_account!r}]")


def resolve_contact(policy: dict[str, Any], *, role: str, label: str) -> str:
    role_mappings = (policy.get("contacts") or {}).get(slugify(role)) or {}
    normalized = {slugify(key): value for key, value in role_mappings.items()}
    key = slugify(label)
    if key not in normalized:
        raise PostingPolicyError(f"No explicit {role!r} contact mapping exists for {label!r}.")
    return normalize_id(normalized[key], field_name=f"contacts[{role!r}][{label!r}]")


def resolve_mapping(policy: dict[str, Any], *, family: str, field_name: str) -> str:
    family_mapping = (policy.get("mappings") or {}).get(slugify(family)) or {}
    value = family_mapping.get(field_name)
    if value in (None, ""):
        raise PostingPolicyError(f"Posting family {family!r} has no explicit {field_name!r} mapping.")
    return normalize_id(value, field_name=f"mappings[{family!r}][{field_name!r}]")


def resolve_supplier_alias(policy: dict[str, Any], value: str) -> str:
    supplier = slugify(value)
    aliases = {slugify(key): slugify(alias) for key, alias in (policy.get("supplier_aliases") or {}).items()}
    return aliases.get(supplier, supplier)
