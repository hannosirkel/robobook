from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
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


def parse_profile_date(value: Any, *, field_name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise PostingPolicyError(f"{field_name} must use ISO YYYY-MM-DD format, got {value!r}.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PostingPolicyError(f"{field_name} must be a real calendar date, got {value!r}.") from exc


def normalize_rate(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise PostingPolicyError(f"{field_name} must be a non-negative number, got {value!r}.")
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PostingPolicyError(f"{field_name} must be a non-negative number, got {value!r}.") from exc
    if not rate.is_finite() or rate < 0:
        raise PostingPolicyError(f"{field_name} must be a non-negative number, got {value!r}.")
    return rate


def validated_sales_vat_profiles(policy: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = policy.get("sales_vat_profiles")
    if not isinstance(profiles, list):
        raise PostingPolicyError("Posting policy requires sales_vat_profiles to resolve taxable sales VAT.")
    normalized: list[dict[str, Any]] = []
    for index, profile in enumerate(profiles):
        path = f"sales_vat_profiles[{index}]"
        if not isinstance(profile, dict):
            raise PostingPolicyError(f"{path} must be an object.")
        start = parse_profile_date(profile.get("start"), field_name=f"{path}.start")
        end_value = profile.get("end")
        end = None if end_value is None else parse_profile_date(end_value, field_name=f"{path}.end")
        if end is not None and end < start:
            raise PostingPolicyError(f"{path}.end cannot be before {path}.start.")
        rate = normalize_rate(profile.get("rate"), field_name=f"{path}.rate")
        normalized.append(
            {
                "start": start,
                "end": end,
                "rate": rate,
                "goods_vat_type_id": normalize_id(profile.get("goods_vat_type_id"), field_name=f"{path}.goods_vat_type_id"),
                "shipping_vat_type_id": normalize_id(profile.get("shipping_vat_type_id"), field_name=f"{path}.shipping_vat_type_id"),
                "source": profile,
            }
        )
    for index, profile in enumerate(normalized):
        for other in normalized[index + 1 :]:
            profile_end = profile["end"] or date.max
            other_end = other["end"] or date.max
            if profile["start"] <= other_end and other["start"] <= profile_end:
                raise PostingPolicyError("sales_vat_profiles must not overlap.")
    return normalized


def resolve_sales_vat_profile(policy: dict[str, Any], *, event_date: date) -> dict[str, Any]:
    matches = [
        profile
        for profile in validated_sales_vat_profiles(policy)
        if profile["start"] <= event_date and (profile["end"] is None or event_date <= profile["end"])
    ]
    if len(matches) != 1:
        raise PostingPolicyError(f"Expected exactly one sales VAT profile for {event_date.isoformat()}, found {len(matches)}.")
    profile = matches[0]
    return {
        "start": profile["start"].isoformat(),
        "end": profile["end"].isoformat() if profile["end"] is not None else None,
        "rate": int(profile["rate"]) if profile["rate"] == profile["rate"].to_integral_value() else float(profile["rate"]),
        "goods_vat_type_id": profile["goods_vat_type_id"],
        "shipping_vat_type_id": profile["shipping_vat_type_id"],
    }


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
        if isinstance(account_id, dict):
            if not account_id:
                raise PostingPolicyError(f"bank_accounts[{source_account!r}] requires at least one currency mapping.")
            for currency, currency_account_id in account_id.items():
                normalized_currency = str(currency or "").strip()
                if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
                    raise PostingPolicyError(f"bank_accounts[{source_account!r}] has invalid currency {currency!r}.")
                normalize_id(currency_account_id, field_name=f"bank_accounts[{source_account!r}][{currency!r}]")
        else:
            normalize_id(account_id, field_name=f"bank_accounts[{source_account!r}]")

    contacts = payload["contacts"]
    for role, mappings in contacts.items():
        if not isinstance(mappings, dict):
            raise PostingPolicyError(f"contacts[{role!r}] must be an object.")
        for label, contact_id in mappings.items():
            normalize_id(contact_id, field_name=f"contacts[{role!r}][{label!r}]")

    def validate_mapping_ids(value: Any, *, path: str) -> None:
        if not isinstance(value, dict):
            raise PostingPolicyError(f"{path} must be an object.")
        for key, child in value.items():
            child_path = f"{path}[{key!r}]"
            if isinstance(child, dict):
                validate_mapping_ids(child, path=child_path)
            elif str(key).endswith("_id"):
                normalize_id(child, field_name=child_path)

    validate_mapping_ids(payload["mappings"], path="mappings")
    if "sales_vat_profiles" in payload:
        validated_sales_vat_profiles(payload)


def load_posting_policy(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PostingPolicyError(f"Could not load posting policy {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PostingPolicyError(f"Posting policy {path} must contain a JSON object.")
    validate_posting_policy(payload)
    return payload


def resolve_bank_account(
    policy: dict[str, Any],
    *,
    customer_account: str,
    currency: str | None = None,
    allow_legacy_single_currency: bool = False,
) -> str:
    source_account = re.sub(r"\s+", "", str(customer_account or "")).upper()
    mappings = policy.get("bank_accounts") or {}
    normalized_mappings = {
        re.sub(r"\s+", "", str(key)).upper(): value
        for key, value in mappings.items()
    }
    if source_account not in normalized_mappings:
        raise PostingPolicyError(f"No exact bank-account mapping exists for source account {customer_account!r}.")
    account_mapping = normalized_mappings[source_account]
    if isinstance(account_mapping, dict):
        normalized_currency = str(currency or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
            raise PostingPolicyError(f"Bank-account mapping for {customer_account!r} requires a three-letter currency.")
        currency_mappings = {str(key).strip().upper(): value for key, value in account_mapping.items()}
        if normalized_currency not in currency_mappings:
            raise PostingPolicyError(
                f"No exact bank-account mapping exists for source account {customer_account!r} and currency {normalized_currency}."
            )
        return normalize_id(
            currency_mappings[normalized_currency],
            field_name=f"bank_accounts[{customer_account!r}][{normalized_currency!r}]",
        )
    if currency is not None and not allow_legacy_single_currency:
        raise PostingPolicyError(
            f"Bank-account mapping for {customer_account!r} must specify currency {str(currency).upper()!r}."
        )
    return normalize_id(account_mapping, field_name=f"bank_accounts[{customer_account!r}]")


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


def configured_bank_account_ids(policy: dict[str, Any]) -> set[str]:
    """Flatten legacy and `(IBAN, currency)` mappings to their allowed SimplBooks IDs."""
    resolved: set[str] = set()
    for source_account, value in (policy.get("bank_accounts") or {}).items():
        if isinstance(value, dict):
            for currency, account_id in value.items():
                resolved.add(
                    normalize_id(
                        account_id,
                        field_name=f"bank_accounts[{source_account!r}][{currency!r}]",
                    )
                )
        else:
            resolved.add(normalize_id(value, field_name=f"bank_accounts[{source_account!r}]"))
    return resolved


def action_policy_errors(action: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    """Independently compare every submit-capable ID in an action with explicit policy."""
    payload = action.get("payload") or {}
    action_type = str(action.get("action_type") or "")
    if action_type in {"create_invoice_summary", "create_credit_invoice_summary"}:
        role = "sales"
        label = str((payload.get("summary_scope") or {}).get("channel_or_source") or "")
    elif action_type == "create_incoming_summary" and str(payload.get("settlement_family") or "") == "direct-sale":
        role = "sales"
        label = str(payload.get("counterparty_hint") or "")
    elif action_type == "create_incoming_summary" or (
        action_type == "create_purchase_summary" and slugify(str(payload.get("vendor_hint") or "")) in {"paypal", "stripe"}
    ):
        role = "processors"
        label = str(payload.get("counterparty_hint") or payload.get("vendor_hint") or "")
    elif action_type in {"create_purchase_summary", "create_purchase_credit_summary", "create_payment_summary"}:
        role = "suppliers"
        label = str(payload.get("vendor_hint") or payload.get("counterparty_hint") or "")
    else:
        return []

    errors: list[str] = []
    try:
        expected_contact = resolve_contact(policy, role=role, label=label)
    except PostingPolicyError as exc:
        errors.append(str(exc))
    else:
        actual_contact = str((payload.get("counterparty") or {}).get("contact_id") or "")
        if actual_contact != expected_contact:
            errors.append(f"Contact ID {actual_contact!r} does not match policy ID {expected_contact!r} for {role}/{label}.")

    if action_type in {"create_incoming_summary", "create_payment_summary"}:
        allowed = configured_bank_account_ids(policy)
        if str(payload.get("bank_account_id") or "") not in allowed:
            errors.append("Cash action bank_account_id is not one of the explicit posting-policy accounts.")

    family = ""
    if role == "sales":
        if action_type in {"create_invoice_summary", "create_credit_invoice_summary"}:
            explicit_family = str((payload.get("summary_scope") or {}).get("posting_family") or "")
            tax_profile = str((payload.get("summary_scope") or {}).get("tax_profile") or "")
            if not tax_profile:
                tax_profile = "taxable" if float((payload.get("totals") or {}).get("vat_amount") or 0) else "non-taxable"
            family = slugify(explicit_family) or f"{slugify(label)}-{slugify(tax_profile)}"
    elif action_type == "create_purchase_summary" and role == "processors":
        family = f"fees-{slugify(label)}"
    elif action_type in {"create_purchase_summary", "create_purchase_credit_summary"}:
        family = f"purchase-{slugify(label)}"
    if not family:
        return errors

    family_values = (policy.get("mappings") or {}).get(family)
    if not isinstance(family_values, dict):
        errors.append(f"Posting family {family!r} is absent from the explicit posting policy.")
        return errors
    if str(payload.get("posting_policy_family") or "") != family:
        errors.append(f"Action is not bound to posting-policy family {family!r}.")

    for line in payload.get("line_items") or []:
        line_role = str(line.get("line_role") or "")
        if role == "sales":
            shipping = line_role.endswith("_shipping")
            account_field = "shipping_income_account_id" if shipping else "income_account_id"
            vat_field = "shipping_vat_type_id" if shipping else "vat_type_id"
            expected_account = normalize_id(family_values.get(account_field), field_name=f"mappings[{family}][{account_field}]")
            expected_vat = normalize_id(family_values.get(vat_field), field_name=f"mappings[{family}][{vat_field}]")
            if line.get("vat_profile_rate") not in (None, ""):
                try:
                    document_date = parse_profile_date(payload.get("document_date"), field_name="payload.document_date")
                    profile = resolve_sales_vat_profile(policy, event_date=document_date)
                    expected_vat = profile["shipping_vat_type_id"] if shipping else profile["goods_vat_type_id"]
                except PostingPolicyError as exc:
                    errors.append(str(exc))
            if str(line.get("suggested_income_account_id") or "") != expected_account:
                errors.append(f"Line income account does not match policy family {family!r}.")
        else:
            line_key = slugify(str(line.get("description") or line_role))
            line_values = (family_values.get("lines") or {}).get(line_key, family_values)
            expected_account = normalize_id(line_values.get("expense_account_id"), field_name=f"mappings[{family}][{line_key}].expense_account_id")
            expected_vat = normalize_id(line_values.get("vat_type_id"), field_name=f"mappings[{family}][{line_key}].vat_type_id")
            if str(line.get("posting_policy_line_key") or "") != line_key:
                errors.append(f"Line is not bound to posting-policy key {line_key!r}.")
            if str(line.get("suggested_expense_account_id") or "") != expected_account:
                errors.append(f"Line expense account does not match policy family {family!r}.")
        if str(line.get("suggested_vat_type_id") or "") != expected_vat:
            errors.append(f"Line VAT type does not match policy family {family!r}.")
        expected_warehouse = family_values.get("warehouse_id") if role == "sales" else line_values.get("warehouse_id")
        actual_warehouse = line.get("warehouse_id_hint")
        if str(actual_warehouse or "") != str(expected_warehouse or ""):
            errors.append(f"Line warehouse does not match policy family {family!r}.")
    return errors
