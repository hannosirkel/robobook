#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from document_identity import document_identity, match_existing
from exchange_rates import ExchangeRateError, lookup_rate
from posting_policy import PostingPolicyError, load_posting_policy, resolve_bank_account, resolve_contact, resolve_mapping
from reference_artifacts import ReferenceArtifactError, bind_file, validate_discovery
from simplbooks_api import SimplbooksError, resolve_company_id, resolve_company_name


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

LEGAL_ENTITY_TOKENS = {
    "ab",
    "as",
    "bv",
    "eu",
    "gmbh",
    "inc",
    "llc",
    "ltd",
    "mtu",
    "ou",
    "oy",
    "sa",
    "sarl",
    "sca",
}

COUNTERPARTY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "paypal": ("paypal",),
    "stripe": ("stripe",),
    "woo": ("woo", "webshop", "shop", "store"),
    "printful": ("printful",),
    "quartermaster": ("quartermaster", "qmlogistics", "qmdirect"),
    "shipmonk": ("shipmonk",),
    "omnipack": ("omnipack",),
}

CONTACT_ALIASES: dict[str, tuple[str, ...]] = {
    "omniva": ("AS Eesti Post", "Aktsiaselts Eesti Post", "Eesti Post"),
}

CONTACT_FALLBACKS: dict[str, tuple[str, ...]] = {
    "paypal": ("stripe",),
    "woo": ("stripe", "paypal"),
}

ONLINE_SALES_CHANNELS = {"woo", "quartermaster"}

DEFAULT_ACTION_STATUS = {
    "executed_at": None,
    "response_status": None,
    "response_body": None,
    "inserted_id": None,
}

TOLERANCE = Decimal("0.50")
SETTLEMENT_MATCH_TOLERANCE = Decimal("0.01")

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised by runtime fallback
    yaml = None


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


def normalize_ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii")


def slugify(value: str) -> str:
    collapsed = re.sub(r"[^a-z0-9]+", "-", normalize_ascii(value).lower())
    return collapsed.strip("-") or "item"


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", normalize_ascii(str(value or "")).strip().lower())


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
    return classify_record(record, PROCESSOR_KEYWORDS)


def infer_fulfillment_partner(record: dict[str, Any]) -> str | None:
    return classify_record(record, FULFILLMENT_KEYWORDS)


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    else:
        text = simple_yaml_dump(payload)
    path.write_text(text, encoding="utf-8")


def simple_yaml_dump(value: Any, indent: int = 0) -> str:
    lines = simple_yaml_lines(value, indent=indent)
    return "\n".join(lines) + "\n"


def simple_yaml_lines(value: Any, *, indent: int) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{{}}"]
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                if isinstance(item, list) and not item:
                    lines.append(f"{prefix}{key}: []")
                elif isinstance(item, dict) and not item:
                    lines.append(f"{prefix}{key}: {{}}")
                else:
                    lines.append(f"{prefix}{key}:")
                    lines.extend(simple_yaml_lines(item, indent=indent + 2))
            else:
                lines.append(f"{prefix}{key}: {yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}[]"]
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                if isinstance(item, dict) and not item:
                    lines.append(f"{prefix}- {{}}")
                elif isinstance(item, list) and not item:
                    lines.append(f"{prefix}- []")
                else:
                    lines.append(f"{prefix}-")
                    lines.extend(simple_yaml_lines(item, indent=indent + 2))
            else:
                lines.append(f"{prefix}- {yaml_scalar(item)}")
        return lines
    return [f"{prefix}{yaml_scalar(value)}"]


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value), ensure_ascii=False)


def entity_text(entry: dict[str, Any]) -> str:
    return normalize_text(" ".join(str(entry.get(key) or "") for key in ("name", "code", "status")))


def choose_entity(
    entries: list[dict[str, Any]],
    *,
    include_keywords: tuple[str, ...],
    exclude_keywords: tuple[str, ...] = (),
) -> tuple[str | None, str | None]:
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for entry in entries:
        text = entity_text(entry)
        if exclude_keywords and any(keyword in text for keyword in exclude_keywords):
            continue
        score = sum(3 if keyword in normalize_text(entry.get("name")) else 1 for keyword in include_keywords if keyword in text)
        if score <= 0:
            continue
        scored.append((score, str(entry.get("id")), entry))

    if not scored:
        return None, None

    scored.sort(key=lambda item: (-item[0], entity_text(item[2]), item[1]))
    top_score = scored[0][0]
    top_entries = [item for item in scored if item[0] == top_score]
    if len(top_entries) > 1:
        labels = ", ".join(str(item[2].get("name") or item[1]) for item in top_entries[:3])
        return None, f"Ambiguous entity mapping candidates: {labels}."
    return scored[0][1], None


def entity_extra(entry: dict[str, Any]) -> dict[str, Any]:
    extra = entry.get("extra")
    return extra if isinstance(extra, dict) else {}


def boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = normalize_text(value)
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def find_entity_id(
    entries: list[dict[str, Any]],
    *,
    code: str | None = None,
    include_keywords: tuple[str, ...] = (),
    exclude_keywords: tuple[str, ...] = (),
    is_sales: bool | None = None,
    is_purchase: bool | None = None,
    vat_percent: int | None = None,
) -> str | None:
    candidates: list[dict[str, Any]] = []
    for entry in entries:
        text = entity_text(entry)
        if code is not None and str(entry.get("code") or "").strip() != code:
            continue
        if include_keywords and any(keyword not in text for keyword in include_keywords):
            continue
        if exclude_keywords and any(keyword in text for keyword in exclude_keywords):
            continue
        extra = entity_extra(entry)
        if is_sales is not None:
            extra_value = boolish(extra.get("is_sales"))
            if extra_value is not None and extra_value != is_sales:
                continue
        if is_purchase is not None:
            extra_value = boolish(extra.get("is_purchase"))
            if extra_value is not None and extra_value != is_purchase:
                continue
        if vat_percent is not None:
            raw_vat_percent = extra.get("vat_percent")
            if raw_vat_percent in (None, ""):
                continue
            try:
                parsed_vat_percent = int(raw_vat_percent)
            except (TypeError, ValueError):
                continue
            if parsed_vat_percent != vat_percent:
                continue
        candidates.append(entry)

    if not candidates:
        return None
    candidates.sort(key=lambda item: (str(item.get("code") or ""), entity_text(item), str(item.get("id") or "")))
    return str(candidates[0].get("id"))


def merge_mapping_hints(
    base: dict[str, tuple[str | None, list[str]]],
    overrides: dict[str, tuple[str | None, list[str]]],
) -> dict[str, tuple[str | None, list[str]]]:
    merged = dict(base)
    merged.update(overrides)
    return merged


def unique_labels(values: list[str]) -> tuple[str, ...]:
    labels: list[str] = []
    seen: set[str] = set()
    for value in values:
        label = str(value or "").strip()
        if not label:
            continue
        normalized = normalize_text(label)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        labels.append(label)
    return tuple(labels)


def contact_candidate_labels(
    *,
    group_label: str,
    records: list[dict[str, Any]] | None = None,
    extra_labels: tuple[str, ...] = (),
) -> tuple[str, ...]:
    labels = [group_label, *CONTACT_ALIASES.get(slugify(group_label), ()), *extra_labels]
    for record in records or []:
        attributes = record.get("attributes") or {}
        labels.extend(
            str(attributes.get(key) or "")
            for key in ("vendor_name", "counterparty_name", "customer_name", "name")
        )
    return unique_labels(labels)


def preferred_bank_account_id(
    company_profile: dict[str, Any] | None,
    entity_map: dict[str, Any] | None,
) -> tuple[str | None, list[str]]:
    notes: list[str] = []
    if company_profile:
        bank_ids = [str(item) for item in company_profile.get("bank_account_ids") or [] if str(item).strip()]
        if len(bank_ids) == 1:
            return bank_ids[0], notes
        if len(bank_ids) > 1:
            notes.append("Company profile lists multiple bank account IDs; pick the posting target manually.")

    income_accounts = list((entity_map or {}).get("income_accounts") or [])
    if len(income_accounts) == 1:
        return str(income_accounts[0]["id"]), notes

    account_id, ambiguity_note = choose_entity(
        income_accounts,
        include_keywords=("bank", "swedbank", "lhv", "seb", "konto", "account"),
        exclude_keywords=("cash", "sularaha"),
    )
    if ambiguity_note:
        notes.append(ambiguity_note)
    return account_id, notes


def preferred_contact_id(
    entity_map: dict[str, Any] | None,
    *,
    group_label: str,
    candidate_labels: tuple[str, ...] = (),
) -> tuple[str | None, list[str]]:
    contacts = list((entity_map or {}).get("contacts") or [])
    if not contacts:
        return None, []

    for label in contact_candidate_labels(group_label=group_label, extra_labels=candidate_labels):
        slug = slugify(label)
        tokens = tuple(token for token in slug.split("-") if token)
        include_keywords = COUNTERPARTY_KEYWORDS.get(slug, ()) + tokens
        if not include_keywords:
            include_keywords = (slug,)

        contact_id, ambiguity_note = choose_entity(
            contacts,
            include_keywords=include_keywords,
        )
        if contact_id is None:
            if ambiguity_note:
                continue
            continue
        notes: list[str] = []
        if label != group_label:
            notes.append(f"Used contact mapping label {label!r} for {group_label!r}.")
        if ambiguity_note:
            notes.append(ambiguity_note)
        return contact_id, notes

    notes: list[str] = []
    notes.append(f"No contact/client mapping matched {group_label!r}.")
    return None, notes


def preferred_contact_id_with_fallbacks(
    entity_map: dict[str, Any] | None,
    *,
    group_label: str,
    candidate_labels: tuple[str, ...] = (),
    fallback_labels: tuple[str, ...] = (),
) -> tuple[str | None, list[str]]:
    contact_id, contact_notes = preferred_contact_id(
        entity_map,
        group_label=group_label,
        candidate_labels=candidate_labels,
    )
    if contact_id is not None:
        return contact_id, contact_notes

    notes = [note for note in contact_notes if note != f"No contact/client mapping matched {group_label!r}."]
    for fallback_label in fallback_labels:
        fallback_contact_id, fallback_notes = preferred_contact_id(
            entity_map,
            group_label=fallback_label,
        )
        if fallback_contact_id is None:
            continue
        notes.extend(
            note
            for note in fallback_notes
            if note != f"No contact/client mapping matched {fallback_label!r}."
        )
        notes.append(f"Used {fallback_label!r} contact mapping as a fallback for {group_label!r}.")
        return fallback_contact_id, notes

    return contact_id, contact_notes


def preferred_mapping_hints(entity_map: dict[str, Any] | None) -> dict[str, tuple[str | None, list[str]]]:
    entity_map = entity_map or {}
    financial_accounts = list(entity_map.get("financial_accounts") or [])
    vat_types = list(entity_map.get("vat_types") or [])
    warehouses = list(entity_map.get("warehouses") or [])

    revenue_id, revenue_note = choose_entity(
        financial_accounts,
        include_keywords=("sales", "revenue", "turnover", "muuk", "müük", "tulu"),
        exclude_keywords=("shipping", "transport", "postage", "fee", "commission"),
    )
    shipping_id, shipping_note = choose_entity(
        financial_accounts,
        include_keywords=("shipping", "transport", "delivery", "postage", "tarne"),
    )
    fee_id, fee_note = choose_entity(
        financial_accounts,
        include_keywords=("fee", "commission", "processor", "payment", "paypal", "stripe"),
    )
    fulfillment_id, fulfillment_note = choose_entity(
        financial_accounts,
        include_keywords=("fulfillment", "shipping", "postage", "logistics", "printful", "quartermaster", "shipmonk", "omnipack", "warehouse"),
    )
    standard_vat_id, standard_vat_note = choose_entity(
        vat_types,
        include_keywords=("22", "20", "standard", "km"),
        exclude_keywords=("0", "zero", "export", "outside", "exempt"),
    )
    zero_vat_id, zero_vat_note = choose_entity(
        vat_types,
        include_keywords=("0", "zero", "export", "outside", "exempt"),
    )

    def tuple_with_note(value: str | None, note: str | None) -> tuple[str | None, list[str]]:
        return value, [note] if note else []

    return {
        "revenue_account": tuple_with_note(revenue_id, revenue_note),
        "shipping_account": tuple_with_note(shipping_id, shipping_note),
        "fee_account": tuple_with_note(fee_id, fee_note),
        "fulfillment_account": tuple_with_note(fulfillment_id, fulfillment_note),
        "standard_vat_type": tuple_with_note(standard_vat_id, standard_vat_note),
        "zero_vat_type": tuple_with_note(zero_vat_id, zero_vat_note),
        "shipping_standard_vat_type": tuple_with_note(standard_vat_id, standard_vat_note),
        "shipping_zero_vat_type": tuple_with_note(zero_vat_id, zero_vat_note),
        "default_warehouse": tuple_with_note(
            find_entity_id(warehouses, include_keywords=("printful", "eu")),
            None,
        ),
    }


def standard_online_sales_mapping(
    entity_map: dict[str, Any] | None,
    *,
    profile_name: str,
    default_warehouse_keywords: tuple[str, ...] = ("printful", "eu"),
) -> dict[str, tuple[str | None, list[str]]]:
    entity_map = entity_map or {}
    financial_accounts = list(entity_map.get("financial_accounts") or [])
    vat_types = list(entity_map.get("vat_types") or [])
    warehouses = list(entity_map.get("warehouses") or [])
    default_warehouse_id = (
        find_entity_id(warehouses, include_keywords=default_warehouse_keywords)
        if default_warehouse_keywords
        else None
    )

    if profile_name == "taxable":
        return {
            "revenue_account": (find_entity_id(financial_accounts, code="4530"), []),
            "shipping_account": (find_entity_id(financial_accounts, code="4992"), []),
            "standard_vat_type": (
                find_entity_id(
                    vat_types,
                    include_keywords=("20%", "kauba", "eu", "uhendusesisene"),
                    is_sales=True,
                    vat_percent=20,
                ),
                [],
            ),
            "shipping_standard_vat_type": (
                find_entity_id(
                    vat_types,
                    include_keywords=("20%", "teenuste", "eu", "uhendusesisene"),
                    is_sales=True,
                    vat_percent=20,
                ),
                [],
            ),
            "default_warehouse": (default_warehouse_id, []),
        }

    return {
        "revenue_account": (find_entity_id(financial_accounts, code="4610"), []),
        "shipping_account": (find_entity_id(financial_accounts, code="4994"), []),
        "zero_vat_type": (
            find_entity_id(vat_types, include_keywords=("0%", "kauba", "eksport"), is_sales=True, vat_percent=0),
            [],
        ),
        "shipping_zero_vat_type": (
            find_entity_id(vat_types, include_keywords=("0%", "teenuste", "eksport"), is_sales=True, vat_percent=0),
            [],
        ),
        "default_warehouse": (default_warehouse_id, []),
    }


def processor_purchase_mapping(
    entity_map: dict[str, Any] | None,
    mapping_hints: dict[str, tuple[str | None, list[str]]],
) -> tuple[str | None, str | None]:
    entity_map = entity_map or {}
    financial_accounts = list(entity_map.get("financial_accounts") or [])
    vat_types = list(entity_map.get("vat_types") or [])
    expense_account_id = find_entity_id(financial_accounts, code="5201") or mapping_hints["fee_account"][0]
    vat_type_id = (
        find_entity_id(vat_types, include_keywords=("0%", "teenuste", "soetamine"), is_purchase=True, vat_percent=0)
        or mapping_hints["zero_vat_type"][0]
    )
    return expense_account_id, vat_type_id


def generic_purchase_mapping(
    entity_map: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    entity_map = entity_map or {}
    financial_accounts = list(entity_map.get("financial_accounts") or [])
    vat_types = list(entity_map.get("vat_types") or [])
    return (
        find_entity_id(financial_accounts, code="5200"),
        find_entity_id(vat_types, include_keywords=("20%", "eesti"), is_purchase=True, vat_percent=20),
        find_entity_id(vat_types, include_keywords=("mitte", "km")),
    )


def printful_purchase_mapping(
    entity_map: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    entity_map = entity_map or {}
    financial_accounts = list(entity_map.get("financial_accounts") or [])
    vat_types = list(entity_map.get("vat_types") or [])
    return (
        find_entity_id(financial_accounts, code="5521"),
        find_entity_id(vat_types, include_keywords=("0%", "teenuste", "soetamine"), is_purchase=True, vat_percent=0),
    )


def printful_storage_mapping(
    entity_map: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    entity_map = entity_map or {}
    financial_accounts = list(entity_map.get("financial_accounts") or [])
    vat_types = list(entity_map.get("vat_types") or [])
    return (
        find_entity_id(financial_accounts, code="5201"),
        find_entity_id(vat_types, include_keywords=("0%", "teenuste", "soetamine"), is_purchase=True, vat_percent=0),
    )


def record_group_label(record: dict[str, Any], *, default: str) -> str:
    for value in (
        record.get("channel"),
        infer_processor(record),
        infer_fulfillment_partner(record),
        record.get("source_system"),
    ):
        if value:
            return str(value)
    return default


def record_currency(record: dict[str, Any], default_currency: str) -> str:
    currency = str(record.get("currency") or "").strip().upper()
    return currency or default_currency


def source_refs_for_records(
    artifact_path: str,
    records: list[dict[str, Any]],
    *,
    note: str | None = None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for record in records:
        refs.append(
            {
                "path": artifact_path,
                "record_ref": record.get("record_id"),
                "note": note,
            }
        )
    return refs


def unique_values(records: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(record.get(key)) for record in records if record.get(key) not in (None, "")})


def summarize_countries(records: list[dict[str, Any]]) -> list[str]:
    return unique_values(records, "country_code")


def summarize_warehouses(records: list[dict[str, Any]]) -> list[str]:
    return unique_values(records, "warehouse_id")


def sum_amount(records: list[dict[str, Any]], field: str) -> Decimal:
    total = Decimal("0")
    for record in records:
        total += decimal_value(record.get(field))
    return total


def sum_abs_amount(records: list[dict[str, Any]], field: str) -> Decimal:
    total = Decimal("0")
    for record in records:
        total += abs(decimal_value(record.get(field)))
    return total


def fee_total_from_record(record: dict[str, Any]) -> Decimal:
    fee_amount = abs(decimal_value(record.get("fee_amount")))
    if fee_amount != 0:
        return fee_amount
    gross_amount = abs(decimal_value(record.get("gross_amount")))
    if gross_amount != 0:
        return gross_amount
    return abs(decimal_value(record.get("net_amount")))


def record_count_note(records: list[dict[str, Any]]) -> str:
    return f"Built from {len(records)} normalized record(s)."


def review_confidence(*, notes: list[str], required_ids: list[str | None]) -> str:
    missing_required = any(item in (None, "") for item in required_ids)
    if missing_required:
        return "low"
    if notes:
        return "medium"
    return "high"


def policy_prefers_shipping_split(policy_text: str | None, shipping_total: Decimal) -> bool:
    if shipping_total == 0:
        return False
    if not policy_text:
        return False
    text = normalize_text(policy_text)
    return any(
        phrase in text
        for phrase in (
            "shipping revenue may be kept separate",
            "separate shipping",
            "shipping revenue separate",
            "shipping treated separately",
        )
    )


def taxable_profile(record: dict[str, Any]) -> str:
    return "taxable" if abs(decimal_value(record.get("vat_amount"))) > 0 else "non_taxable"


def maybe_single_warehouse(records: list[dict[str, Any]]) -> str | None:
    values = summarize_warehouses(records)
    if len(values) == 1:
        return values[0]
    return None


def significant_group_tokens(label: str) -> tuple[str, ...]:
    tokens = [token for token in slugify(label).split("-") if token]
    significant = [token for token in tokens if token not in LEGAL_ENTITY_TOKENS and len(token) >= 3]
    if significant:
        return tuple(significant)
    if tokens:
        return (tokens[0],)
    return ()


def matched_purchase_group_for_bank_record(
    record: dict[str, Any],
    *,
    available_group_labels: set[str],
) -> tuple[str | None, str | None]:
    partner = infer_fulfillment_partner(record)
    if partner and partner in available_group_labels:
        return partner, None

    channel = str(record.get("channel") or "").strip()
    if channel:
        channel_slug = slugify(channel)
        if channel_slug in available_group_labels:
            return channel_slug, None

    haystack = record_haystack(record)
    counterparty_name = normalize_text((record.get("attributes") or {}).get("counterparty_name"))
    scored: list[tuple[int, str]] = []

    for label in sorted(available_group_labels):
        tokens = significant_group_tokens(label)
        if not tokens:
            continue
        if not all(token in haystack for token in tokens):
            continue
        score = 10 + sum(len(token) for token in tokens)
        if counterparty_name and all(token in counterparty_name for token in tokens):
            score += 20
        if counterparty_name and label.replace("-", " ") in counterparty_name:
            score += 10
        scored.append((score, label))

    if not scored:
        return None, None

    scored.sort(key=lambda item: (-item[0], item[1]))
    top_score = scored[0][0]
    top_labels = [label for score, label in scored if score == top_score]
    if len(top_labels) > 1:
        labels = ", ".join(top_labels[:3])
        return None, f"Bank debit matched multiple purchase groups: {labels}."

    label = top_labels[0]
    return label, f"Matched bank debits to purchase group {label!r} using supplier text on bank records."


def purchase_candidate_from_action(
    action: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any] | None:
    if str(action.get("action_type") or "") != "create_purchase_summary":
        return None

    payload = action.get("payload") or {}
    group_label = str(payload.get("vendor_hint") or "").strip()
    if not group_label:
        return None

    totals = payload.get("totals") or {}
    gross_amount = abs(decimal_value(totals.get("gross_amount")))
    if gross_amount == 0:
        return None

    counterparty = payload.get("counterparty") or {}
    action_key = str(action.get("idempotency_key") or "").strip()
    if not action_key:
        return None
    return {
        "action_id": action_key,
        "action_period": str(action.get("period") or ""),
        "group_label": group_label,
        "currency": str(payload.get("currency") or "EUR"),
        "gross_amount": gross_amount,
        "contact_id": counterparty.get("contact_id"),
        "source": source,
    }


def payment_linked_purchase_action(action: dict[str, Any]) -> str:
    payload = action.get("payload") or {}
    linked_purchase_action = str(payload.get("linked_purchase_action") or "").strip()
    if linked_purchase_action:
        return linked_purchase_action
    return next((str(dependency) for dependency in action.get("depends_on") or []), "")


def historical_purchase_candidates(
    *,
    actions_dir: Path | None,
    current_period: str,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    if actions_dir is None or not actions_dir.exists():
        return {}

    from bookchecker import load_yaml

    purchases_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    settled_purchase_ids: set[str] = set()

    for path in sorted(actions_dir.glob("*.yaml")):
        batch_period = path.stem
        if not re.fullmatch(r"\d{4}-\d{2}", batch_period):
            continue
        if batch_period >= current_period:
            continue

        batch = load_yaml(path)
        for action in batch.get("actions") or []:
            if not isinstance(action, dict):
                continue
            candidate = purchase_candidate_from_action(action, source="prior")
            if candidate is not None:
                purchases_by_key[(candidate["group_label"], candidate["currency"])].append(candidate)
                continue
            if str(action.get("action_type") or "") == "create_payment_summary":
                linked_purchase_action = payment_linked_purchase_action(action)
                if linked_purchase_action:
                    settled_purchase_ids.add(linked_purchase_action)

    unresolved: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, candidates in purchases_by_key.items():
        remaining = [candidate for candidate in candidates if candidate["action_id"] not in settled_purchase_ids]
        if remaining:
            unresolved[key] = sorted(
                remaining,
                key=lambda candidate: (candidate["action_period"], candidate["action_id"]),
            )
    return unresolved


def grouped_purchase_candidates(
    actions: list[dict[str, Any]],
    *,
    source: str,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        candidate = purchase_candidate_from_action(action, source=source)
        if candidate is None:
            continue
        grouped[(candidate["group_label"], candidate["currency"])].append(candidate)
    return grouped


def matched_purchase_candidates_for_total(
    candidates: list[dict[str, Any]],
    *,
    target_total: Decimal,
) -> list[dict[str, Any]]:
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            0 if candidate["source"] == "current" else 1,
            candidate["action_period"],
            candidate["action_id"],
        ),
    )
    if not ordered:
        return []
    if len(ordered) > 16:
        return []

    remaining_totals = [Decimal("0")] * (len(ordered) + 1)
    for index in range(len(ordered) - 1, -1, -1):
        remaining_totals[index] = remaining_totals[index + 1] + ordered[index]["gross_amount"]

    best_match: tuple[tuple[Any, ...], list[dict[str, Any]]] | None = None

    def search(index: int, chosen: list[dict[str, Any]], running_total: Decimal) -> None:
        nonlocal best_match
        if chosen:
            difference = abs(running_total - target_total)
            if difference <= SETTLEMENT_MATCH_TOLERANCE:
                score = (
                    difference,
                    len(chosen),
                    sum(1 for candidate in chosen if candidate["source"] != "current"),
                    tuple(candidate["action_period"] for candidate in chosen),
                    tuple(candidate["action_id"] for candidate in chosen),
                )
                if best_match is None or score < best_match[0]:
                    best_match = (score, list(chosen))

        if index >= len(ordered):
            return
        if running_total > target_total + SETTLEMENT_MATCH_TOLERANCE:
            return
        if running_total + remaining_totals[index] < target_total - SETTLEMENT_MATCH_TOLERANCE:
            return

        candidate = ordered[index]
        chosen.append(candidate)
        search(index + 1, chosen, running_total + candidate["gross_amount"])
        chosen.pop()
        search(index + 1, chosen, running_total)

    search(0, [], Decimal("0"))
    if best_match is None:
        return []
    return best_match[1]


def payment_action_key(
    *,
    company_slug: str,
    period: str,
    group_label: str,
    purchase_candidate: dict[str, Any],
    multiple_matches: bool,
    used_keys: set[str],
) -> str:
    base = f"{company_slug}-{period}-payment-{slugify(group_label)}"
    if not multiple_matches and purchase_candidate["source"] == "current" and purchase_candidate["action_period"] == period:
        key = base
    else:
        key = f"{base}-{slugify(purchase_candidate['action_id'])}"

    candidate_key = key
    suffix = 2
    while candidate_key in used_keys:
        candidate_key = f"{key}-{suffix}"
        suffix += 1
    used_keys.add(candidate_key)
    return candidate_key


def idempotency_suffix(
    *,
    group_label: str,
    currency: str,
    repeated_labels: set[str],
    extra_suffix: str | None = None,
) -> str:
    parts = [slugify(group_label)]
    if group_label in repeated_labels:
        parts.append(slugify(currency))
    if extra_suffix:
        parts.append(slugify(extra_suffix))
    return "-".join(part for part in parts if part)


def processor_group_for_record(record: dict[str, Any]) -> str | None:
    return infer_processor(record)


def planned_sales_groups(
    records: list[dict[str, Any]],
    *,
    base_currency: str,
    amount_tolerance: Decimal | None = None,
) -> tuple[
    dict[tuple[str, str], list[dict[str, Any]]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], tuple[str, str]],
]:
    tolerance = TOLERANCE if amount_tolerance is None else amount_tolerance
    grouped_sales: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = (record_group_label(record, default="sales"), record_currency(record, base_currency))
        grouped_sales[key].append(record)

    review_notes_by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    matched_processors_by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    posting_basis_by_suppressed_key: dict[tuple[str, str], tuple[str, str]] = {}
    keep_keys = set(grouped_sales)

    grouped_by_currency: dict[str, dict[str, dict[tuple[str, str], list[dict[str, Any]]]]] = defaultdict(
        lambda: {"merchant": {}, "processor": {}}
    )
    for key, group_records in grouped_sales.items():
        currency = key[1]
        bucket = "processor" if all(processor_group_for_record(record) for record in group_records) else "merchant"
        grouped_by_currency[currency][bucket][key] = group_records

    for currency, buckets in grouped_by_currency.items():
        merchant_groups = buckets["merchant"]
        processor_groups = buckets["processor"]
        if len(merchant_groups) != 1 or not processor_groups:
            continue

        merchant_key, merchant_records = next(iter(merchant_groups.items()))
        merchant_total = sum_abs_amount(merchant_records, "gross_amount")
        processor_total = sum(sum_abs_amount(group_records, "gross_amount") for group_records in processor_groups.values())
        if abs(merchant_total - processor_total) > tolerance:
            continue

        suppressed_labels = sorted(key[0] for key in processor_groups)
        matched_processors_by_key[merchant_key].extend(suppressed_labels)
        review_notes_by_key[merchant_key].append(
            f"Processor-side sales totals matched this {merchant_key[0]} revenue stream in {currency} and were kept as settlement evidence instead of creating a second invoice summary."
        )
        for key in processor_groups:
            keep_keys.discard(key)
            posting_basis_by_suppressed_key[key] = merchant_key

    planned = {key: grouped_sales[key] for key in sorted(keep_keys)}
    return planned, review_notes_by_key, matched_processors_by_key, posting_basis_by_suppressed_key


def resolve_sales_contact_id(
    entity_map: dict[str, Any] | None,
    *,
    group_label: str,
    records: list[dict[str, Any]],
    matched_processor_labels: list[str],
) -> tuple[str | None, list[str]]:
    ordered_fallbacks: list[str] = []
    if "stripe" in matched_processor_labels:
        ordered_fallbacks.append("stripe")
    ordered_fallbacks.extend(label for label in matched_processor_labels if label not in ordered_fallbacks)
    ordered_fallbacks.extend(
        label for label in CONTACT_FALLBACKS.get(slugify(group_label), ()) if label not in ordered_fallbacks
    )
    return preferred_contact_id_with_fallbacks(
        entity_map,
        group_label=group_label,
        candidate_labels=contact_candidate_labels(group_label=group_label, records=records),
        fallback_labels=tuple(ordered_fallbacks),
    )


def build_sales_lines(
    *,
    records: list[dict[str, Any]],
    group_label: str,
    direction: str,
    shipping_split: bool,
    mapping_hints: dict[str, tuple[str | None, list[str]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    review_notes: list[str] = []
    lines: list[dict[str, Any]] = []
    warehouses = summarize_warehouses(records)
    countries = summarize_countries(records)
    revenue_account_id, revenue_notes = mapping_hints["revenue_account"]
    shipping_account_id, shipping_notes = mapping_hints["shipping_account"]
    standard_vat_id, standard_vat_notes = mapping_hints["standard_vat_type"]
    zero_vat_id, zero_vat_notes = mapping_hints["zero_vat_type"]
    shipping_standard_vat_id, shipping_standard_vat_notes = mapping_hints.get(
        "shipping_standard_vat_type",
        mapping_hints["standard_vat_type"],
    )
    shipping_zero_vat_id, shipping_zero_vat_notes = mapping_hints.get(
        "shipping_zero_vat_type",
        mapping_hints["zero_vat_type"],
    )
    default_warehouse_id, default_warehouse_notes = mapping_hints.get("default_warehouse", (None, []))
    review_notes.extend(
        revenue_notes
        + shipping_notes
        + standard_vat_notes
        + zero_vat_notes
        + shipping_standard_vat_notes
        + shipping_zero_vat_notes
        + default_warehouse_notes
    )

    grouped_by_profile: dict[str, list[dict[str, Any]]] = {"taxable": [], "non_taxable": []}
    for record in records:
        grouped_by_profile[taxable_profile(record)].append(record)

    nonempty_profiles = [name for name, values in grouped_by_profile.items() if values]
    total_shipping = sum_abs_amount(records, "shipping_amount")

    if shipping_split and len(nonempty_profiles) > 1:
        review_notes.append("Shipping was not split into a dedicated line because taxable and non-taxable sales are mixed in the same action.")
        shipping_split = False

    if shipping_split and total_shipping != 0:
        total_gross = sum_abs_amount(records, "gross_amount")
        total_vat = sum_abs_amount(records, "vat_amount")
        revenue_gross = total_gross - total_shipping
        shipping_vat_type_id = shipping_standard_vat_id if total_vat != 0 else shipping_zero_vat_id
        lines.append(
            {
                "line_role": f"{direction}_revenue",
                "description": f"{group_label} {direction} revenue summary",
                "gross_amount": decimal_number(revenue_gross),
                "vat_amount_hint": decimal_number(total_vat),
                "shipping_component_gross_amount": 0.0,
                "suggested_income_account_id": revenue_account_id,
                "suggested_vat_type_id": standard_vat_id if total_vat != 0 else zero_vat_id,
                "warehouse_id_hint": maybe_single_warehouse(records) or default_warehouse_id,
                "record_count": len(records),
            }
        )
        lines.append(
            {
                "line_role": f"{direction}_shipping",
                "description": f"{group_label} shipping charged summary",
                "gross_amount": decimal_number(total_shipping),
                "vat_amount_hint": None,
                "shipping_component_gross_amount": decimal_number(total_shipping),
                "suggested_income_account_id": shipping_account_id,
                "suggested_vat_type_id": shipping_vat_type_id,
                "warehouse_id_hint": maybe_single_warehouse(records) or default_warehouse_id,
                "record_count": len(records),
            }
        )
        review_notes.append("Shipping is split into a dedicated draft line; exact VAT allocation between revenue and shipping still needs review.")
    else:
        for profile_name in ("taxable", "non_taxable"):
            profile_records = grouped_by_profile[profile_name]
            if not profile_records:
                continue
            gross_amount = sum_abs_amount(profile_records, "gross_amount")
            vat_amount = sum_abs_amount(profile_records, "vat_amount")
            shipping_amount = sum_abs_amount(profile_records, "shipping_amount")
            lines.append(
                {
                    "line_role": f"{direction}_revenue",
                    "description": f"{group_label} {profile_name.replace('_', ' ')} {direction} summary",
                    "gross_amount": decimal_number(gross_amount),
                    "vat_amount_hint": decimal_number(vat_amount),
                    "shipping_component_gross_amount": decimal_number(shipping_amount),
                    "suggested_income_account_id": revenue_account_id,
                    "suggested_vat_type_id": standard_vat_id if profile_name == "taxable" else zero_vat_id,
                    "warehouse_id_hint": maybe_single_warehouse(profile_records) or default_warehouse_id,
                    "record_count": len(profile_records),
                }
            )

    if len(warehouses) > 1:
        review_notes.append(f"Multiple warehouse IDs appear in this action: {', '.join(warehouses)}.")
    if len(countries) > 1:
        review_notes.append(f"Multiple customer country codes appear in this action: {', '.join(countries)}.")
    return lines, review_notes


def make_action(
    *,
    period: str,
    idempotency_key: str,
    action_type: str,
    endpoint: str,
    payload: dict[str, Any],
    source_refs: list[dict[str, Any]],
    reason: str,
    confidence: str,
    depends_on: list[str],
    expected_effect: str,
    review_notes: list[str],
) -> dict[str, Any]:
    action = {
        "idempotency_key": idempotency_key,
        "period": period,
        "action_type": action_type,
        "method": "POST",
        "endpoint": endpoint,
        "payload": payload,
        "source_refs": source_refs,
        "reason": reason,
        "confidence": confidence,
        "depends_on": depends_on,
        "expected_effect": expected_effect,
        "review_notes": review_notes,
    }
    action.update(DEFAULT_ACTION_STATUS)
    return action


def build_sales_actions(
    *,
    company_slug: str,
    period: str,
    period_end: date,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    base_currency: str,
    policy_text: str | None,
    entity_map: dict[str, Any] | None,
    mapping_hints: dict[str, tuple[str | None, list[str]]],
    forced_note: str | None,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str], dict[str, list[str]]]:
    actions: list[dict[str, Any]] = []
    action_ids: dict[tuple[str, str], str] = {}
    action_ids_by_currency: dict[str, list[str]] = defaultdict(list)
    grouped_sales, planned_notes, matched_processors, posting_basis_by_suppressed_key = planned_sales_groups(
        records.get("sales", []),
        base_currency=base_currency,
    )
    repeated_sales_labels = {
        label
        for label, count in Counter(group_label for group_label, _currency in grouped_sales).items()
        if count > 1
    }

    for (group_label, currency), group_records in sorted(grouped_sales.items()):
        grouped_by_profile = {
            "taxable": [record for record in group_records if taxable_profile(record) == "taxable"],
            "non_taxable": [record for record in group_records if taxable_profile(record) == "non_taxable"],
        }
        nonempty_profiles = [profile_name for profile_name, profile_records in grouped_by_profile.items() if profile_records]
        split_profiles = len(nonempty_profiles) > 1
        matched_processor_labels = matched_processors.get((group_label, currency), [])

        for profile_name in nonempty_profiles or ["non_taxable"]:
            profile_records = grouped_by_profile.get(profile_name) or list(group_records)
            if not profile_records:
                continue

            profile_mapping_hints = mapping_hints
            online_sales_override_applied = False
            if slugify(group_label) in ONLINE_SALES_CHANNELS:
                default_warehouse_keywords = ("printful", "eu") if slugify(group_label) == "woo" else ()
                online_sales_override = standard_online_sales_mapping(
                    entity_map,
                    profile_name=profile_name,
                    default_warehouse_keywords=default_warehouse_keywords,
                )
                online_sales_override_applied = any(
                    value[0]
                    for key, value in online_sales_override.items()
                    if key in {"revenue_account", "shipping_account"}
                )
                profile_mapping_hints = merge_mapping_hints(mapping_hints, online_sales_override)

            shipping_total = sum_abs_amount(profile_records, "shipping_amount")
            lines, review_notes = build_sales_lines(
                records=profile_records,
                group_label=group_label,
                direction="sales",
                shipping_split=policy_prefers_shipping_split(policy_text, shipping_total)
                or (online_sales_override_applied and shipping_total != 0),
                mapping_hints=profile_mapping_hints,
            )
            review_notes.extend(planned_notes.get((group_label, currency), []))
            contact_id, contact_notes = resolve_sales_contact_id(
                entity_map,
                group_label=group_label,
                records=profile_records,
                matched_processor_labels=matched_processor_labels,
            )
            review_notes.extend(contact_notes)
            if split_profiles:
                review_notes.append(
                    f"This action only covers the {profile_name.replace('_', ' ')} portion of the {group_label!r} sales stream."
                )
            if forced_note:
                review_notes.append(forced_note)
            review_notes.append(record_count_note(profile_records))

            action_key_suffix = idempotency_suffix(
                group_label=group_label,
                currency=currency,
                repeated_labels=repeated_sales_labels,
                extra_suffix=profile_name.replace("_", "-") if split_profiles else None,
            )
            idempotency_key = f"{company_slug}-{period}-sales-{action_key_suffix}"
            payload = {
                "draft_schema": "invoice_summary_v1",
                "document_type": "invoice",
                "document_date": period_end.isoformat(),
                "currency": currency,
                "summary_scope": {
                    "channel_or_source": group_label,
                    "record_count": len(profile_records),
                },
                "totals": {
                    "gross_amount": decimal_number(sum_abs_amount(profile_records, "gross_amount")),
                    "vat_amount": decimal_number(sum_abs_amount(profile_records, "vat_amount")),
                    "shipping_amount": decimal_number(shipping_total),
                    "fee_amount_observed": decimal_number(sum_abs_amount(profile_records, "fee_amount")),
                },
                "counterparty": {
                    "contact_id": contact_id,
                    "display_name_hint": f"Monthly {group_label} sales summary",
                },
                "line_items": lines,
            }
            if split_profiles:
                payload["summary_scope"]["tax_profile"] = profile_name
            confidence = review_confidence(
                notes=review_notes,
                required_ids=[profile_mapping_hints["revenue_account"][0]],
            )
            action = make_action(
                period=period,
                idempotency_key=idempotency_key,
                action_type="create_invoice_summary",
                endpoint="invoices/create",
                payload=payload,
                source_refs=source_refs_for_records(normalized_path_display, profile_records),
                reason=f"Aggregate {len(profile_records)} normalized {group_label} sales record(s) into a month-level draft invoice summary.",
                confidence=confidence,
                depends_on=[],
                expected_effect=f"Create a draft monthly sales summary for {group_label} in Simplbooks.",
                review_notes=review_notes,
            )
            actions.append(action)
            action_ids_by_currency[currency].append(idempotency_key)

        if not split_profiles and action_ids_by_currency[currency]:
            action_ids[(group_label, currency)] = action_ids_by_currency[currency][-1]

    grouped_refunds: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    refund_evidence_labels: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in records.get("refunds", []):
        evidence_key = (record_group_label(record, default="refunds"), record_currency(record, base_currency))
        posting_key = posting_basis_by_suppressed_key.get(evidence_key, evidence_key)
        grouped_refunds[posting_key].append(record)
        if posting_key != evidence_key:
            refund_evidence_labels[posting_key].add(evidence_key[0])

    repeated_refund_labels = {
        label
        for label, count in Counter(group_label for group_label, _currency in grouped_refunds).items()
        if count > 1
    }

    for (group_label, currency), group_records in sorted(grouped_refunds.items()):
        shipping_total = sum_abs_amount(group_records, "shipping_amount")
        refund_mapping_hints = mapping_hints
        online_sales_override_applied = False
        if slugify(group_label) in ONLINE_SALES_CHANNELS:
            default_warehouse_keywords = ("printful", "eu") if slugify(group_label) == "woo" else ()
            online_sales_override = standard_online_sales_mapping(
                entity_map,
                profile_name="taxable" if any(taxable_profile(record) == "taxable" for record in group_records) else "non_taxable",
                default_warehouse_keywords=default_warehouse_keywords,
            )
            online_sales_override_applied = any(
                value[0]
                for key, value in online_sales_override.items()
                if key in {"revenue_account", "shipping_account"}
            )
            refund_mapping_hints = merge_mapping_hints(mapping_hints, online_sales_override)
        lines, review_notes = build_sales_lines(
            records=group_records,
            group_label=group_label,
            direction="refund",
            shipping_split=policy_prefers_shipping_split(policy_text, shipping_total)
            or (online_sales_override_applied and shipping_total != 0),
            mapping_hints=refund_mapping_hints,
        )
        evidence_labels = sorted(refund_evidence_labels.get((group_label, currency), set()))
        if evidence_labels:
            review_notes.append(
                f"Processor-side refund evidence from {', '.join(evidence_labels)} was posted using {group_label} sales mapping."
            )
        contact_id, contact_notes = resolve_sales_contact_id(
            entity_map,
            group_label=group_label,
            records=group_records,
            matched_processor_labels=evidence_labels,
        )
        review_notes.extend(contact_notes)
        if forced_note:
            review_notes.append(forced_note)
        review_notes.append(record_count_note(group_records))

        depends_on = []
        prior_sales_action = action_ids.get((group_label, currency))
        if prior_sales_action:
            depends_on.append(prior_sales_action)
        else:
            depends_on.extend(action_ids_by_currency.get(currency, []))

        action_key_suffix = idempotency_suffix(
            group_label=group_label,
            currency=currency,
            repeated_labels=repeated_refund_labels,
        )
        idempotency_key = f"{company_slug}-{period}-refund-{action_key_suffix}"
        payload = {
            "draft_schema": "invoice_summary_v1",
            "document_type": "credit_note",
            "document_date": period_end.isoformat(),
            "currency": currency,
            "summary_scope": {
                "channel_or_source": group_label,
                "record_count": len(group_records),
            },
            "totals": {
                "gross_amount": decimal_number(sum_abs_amount(group_records, "gross_amount")),
                "vat_amount": decimal_number(sum_abs_amount(group_records, "vat_amount")),
                "shipping_amount": decimal_number(shipping_total),
            },
            "counterparty": {
                "contact_id": contact_id,
                "display_name_hint": f"Monthly {group_label} refund summary",
            },
            "line_items": lines,
        }
        confidence = review_confidence(
            notes=review_notes,
            required_ids=[refund_mapping_hints["revenue_account"][0]],
        )
        actions.append(
            make_action(
                period=period,
                idempotency_key=idempotency_key,
                action_type="create_credit_invoice_summary",
                endpoint="invoices/create",
                payload=payload,
                source_refs=source_refs_for_records(normalized_path_display, group_records),
                reason=f"Aggregate {len(group_records)} normalized {group_label} refund record(s) into a draft credit-note summary.",
                confidence=confidence,
                depends_on=depends_on,
                expected_effect=f"Create a draft monthly refund summary for {group_label} in Simplbooks.",
                review_notes=review_notes,
            )
        )

    return actions, action_ids, dict(action_ids_by_currency)


def build_fee_actions(
    *,
    company_slug: str,
    period: str,
    period_end: date,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    base_currency: str,
    entity_map: dict[str, Any] | None,
    mapping_hints: dict[str, tuple[str | None, list[str]]],
    forced_note: str | None,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str]]:
    actions: list[dict[str, Any]] = []
    action_ids: dict[tuple[str, str], str] = {}

    grouped_explicit: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records.get("fees", []):
        key = (record_group_label(record, default="fees"), record_currency(record, base_currency))
        grouped_explicit[key].append(record)

    grouped_embedded: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for category in ("sales", "refunds", "payouts"):
        for record in records.get(category, []):
            if fee_total_from_record(record) == 0:
                continue
            if decimal_value(record.get("fee_amount")) == 0:
                continue
            key = (record_group_label(record, default="fees"), record_currency(record, base_currency))
            grouped_embedded[key].append(record)

    all_keys = sorted(set(grouped_explicit) | set(grouped_embedded))
    repeated_fee_labels = {
        label
        for label, count in Counter(group_label for group_label, _currency in all_keys).items()
        if count > 1
    }
    fee_account_id, fee_account_notes = mapping_hints["fee_account"]
    standard_vat_type_id, standard_vat_notes = mapping_hints["standard_vat_type"]

    for group_label, currency in all_keys:
        explicit_records = grouped_explicit.get((group_label, currency), [])
        embedded_records = grouped_embedded.get((group_label, currency), [])
        fallback_labels = CONTACT_FALLBACKS.get(slugify(group_label), ())
        contact_id, contact_notes = preferred_contact_id_with_fallbacks(
            entity_map,
            group_label=group_label,
            candidate_labels=contact_candidate_labels(
                group_label=group_label,
                records=[*explicit_records, *embedded_records],
            ),
            fallback_labels=fallback_labels,
        )
        if explicit_records:
            source_records = explicit_records
            total_fee = sum(fee_total_from_record(record) for record in explicit_records)
            review_notes = list(fee_account_notes + standard_vat_notes + contact_notes)
            if embedded_records:
                review_notes.append("Embedded fee amounts on sales or payout rows were ignored because explicit fee rows exist for this processor.")
        else:
            source_records = embedded_records
            total_fee = sum(abs(decimal_value(record.get("fee_amount"))) for record in embedded_records)
            review_notes = list(fee_account_notes + standard_vat_notes + contact_notes)
            review_notes.append("Processor fee total was inferred from embedded fee amounts on sales or payout rows.")

        if total_fee == 0 or not source_records:
            continue
        processor_expense_account_id, processor_vat_type_id = processor_purchase_mapping(entity_map, mapping_hints)
        if forced_note:
            review_notes.append(forced_note)
        review_notes.append(record_count_note(source_records))

        action_key_suffix = idempotency_suffix(
            group_label=group_label,
            currency=currency,
            repeated_labels=repeated_fee_labels,
        )
        idempotency_key = f"{company_slug}-{period}-fees-{action_key_suffix}"
        payload = {
            "draft_schema": "purchase_summary_v1",
            "document_type": "purchase",
            "document_date": period_end.isoformat(),
            "currency": currency,
            "counterparty": {
                "contact_id": contact_id,
                "display_name_hint": f"{group_label} fee summary",
            },
            "vendor_hint": group_label,
            "totals": {
                "gross_amount": decimal_number(total_fee),
                "vat_amount": 0.0,
            },
            "line_items": [
                {
                    "line_role": "processor_fee",
                    "description": f"{group_label} processor fee summary",
                    "gross_amount": decimal_number(total_fee),
                    "vat_amount_hint": 0.0,
                    "suggested_expense_account_id": processor_expense_account_id or fee_account_id,
                    "suggested_vat_type_id": processor_vat_type_id or standard_vat_type_id,
                    "record_count": len(source_records),
                }
            ],
        }
        confidence = review_confidence(notes=review_notes, required_ids=[processor_expense_account_id or fee_account_id, contact_id])
        action = make_action(
            period=period,
            idempotency_key=idempotency_key,
            action_type="create_purchase_summary",
            endpoint="purchases/create",
            payload=payload,
            source_refs=source_refs_for_records(normalized_path_display, source_records),
            reason=f"Aggregate {group_label} fee evidence into one draft purchase summary.",
            confidence=confidence,
            depends_on=[],
            expected_effect=f"Create a draft processor fee purchase summary for {group_label} in Simplbooks.",
            review_notes=review_notes,
        )
        actions.append(action)
        action_ids[(group_label, currency)] = idempotency_key

    return actions, action_ids


def build_purchase_actions(
    *,
    company_slug: str,
    period: str,
    period_end: date,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    base_currency: str,
    entity_map: dict[str, Any] | None,
    mapping_hints: dict[str, tuple[str | None, list[str]]],
    forced_note: str | None,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str]]:
    actions: list[dict[str, Any]] = []
    action_ids: dict[tuple[str, str], str] = {}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records.get("purchase_expenses", []):
        key = (record_group_label(record, default="expenses"), record_currency(record, base_currency))
        grouped[key].append(record)
    repeated_purchase_labels = {
        label
        for label, count in Counter(group_label for group_label, _currency in grouped).items()
        if count > 1
    }

    expense_account_id, expense_account_notes = mapping_hints["fulfillment_account"]
    standard_vat_type_id, standard_vat_notes = mapping_hints["standard_vat_type"]
    zero_vat_type_id, zero_vat_notes = mapping_hints["zero_vat_type"]
    generic_expense_account_id, domestic_purchase_vat_type_id, no_vat_purchase_vat_type_id = generic_purchase_mapping(entity_map)
    printful_expense_account_id, printful_vat_type_id = printful_purchase_mapping(entity_map)
    printful_storage_account_id, printful_storage_vat_type_id = printful_storage_mapping(entity_map)

    for (group_label, currency), group_records in sorted(grouped.items()):
        contact_id, contact_notes = preferred_contact_id(
            entity_map,
            group_label=group_label,
            candidate_labels=contact_candidate_labels(group_label=group_label, records=group_records),
        )
        review_notes = list(expense_account_notes + standard_vat_notes + zero_vat_notes + contact_notes)
        if forced_note:
            review_notes.append(forced_note)
        review_notes.append(record_count_note(group_records))

        bucketed_records: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in group_records:
            haystack = record_haystack(record)
            if group_label == "printful":
                if any(keyword in haystack for keyword in ("order_charge", "shipping", "fullfil")):
                    warehouse_profile = str(record.get("warehouse_id") or "").upper()
                    description = "Orders shipping and fullfilment"
                    if warehouse_profile:
                        description += f" {warehouse_profile}"
                    bucket = (
                        printful_expense_account_id or expense_account_id or "",
                        printful_vat_type_id or zero_vat_type_id or "",
                        description,
                        warehouse_profile,
                    )
                else:
                    bucket = (
                        printful_storage_account_id or expense_account_id or "",
                        printful_storage_vat_type_id or zero_vat_type_id or "",
                        "Storage fee for warehoused products",
                        "",
                    )
            elif group_label in FULFILLMENT_KEYWORDS:
                bucket = (
                    expense_account_id or generic_expense_account_id or "",
                    (
                        domestic_purchase_vat_type_id or standard_vat_type_id or ""
                        if taxable_profile(record) == "taxable"
                        else no_vat_purchase_vat_type_id or zero_vat_type_id or ""
                    ),
                    f"{group_label} fulfillment cost summary",
                    "",
                )
            else:
                bucket = (
                    generic_expense_account_id or expense_account_id or "",
                    (
                        domestic_purchase_vat_type_id or standard_vat_type_id or ""
                        if taxable_profile(record) == "taxable"
                        else no_vat_purchase_vat_type_id or zero_vat_type_id or ""
                    ),
                    f"{group_label} {'taxable' if taxable_profile(record) == 'taxable' else 'non taxable'} cost summary",
                    "",
                )
            bucketed_records[bucket].append(record)

        lines: list[dict[str, Any]] = []
        for (bucket_expense_account_id, bucket_vat_type_id, description, warehouse_profile), bucket_records in sorted(bucketed_records.items()):
            gross_total = sum_amount(bucket_records, "gross_amount")
            vat_total = sum_amount(bucket_records, "vat_amount")
            if gross_total == 0 and vat_total == 0:
                continue
            lines.append(
                {
                    "line_role": "purchase_expense",
                    "description": description,
                    "gross_amount": decimal_number(gross_total),
                    "vat_amount_hint": decimal_number(vat_total),
                    "suggested_expense_account_id": bucket_expense_account_id or None,
                    "suggested_vat_type_id": bucket_vat_type_id or None,
                    "warehouse_id_hint": warehouse_profile or None,
                    "record_count": len(bucket_records),
                }
            )

        if not lines:
            continue

        if len(summarize_warehouses(group_records)) > 1:
            review_notes.append("Multiple warehouse IDs appear in this expense action.")

        action_key_suffix = idempotency_suffix(
            group_label=group_label,
            currency=currency,
            repeated_labels=repeated_purchase_labels,
        )
        idempotency_key = f"{company_slug}-{period}-purchase-{action_key_suffix}"
        payload = {
            "draft_schema": "purchase_summary_v1",
            "document_type": "purchase",
            "document_date": period_end.isoformat(),
            "currency": currency,
            "counterparty": {
                "contact_id": contact_id,
                "display_name_hint": f"{group_label} purchase summary",
            },
            "vendor_hint": group_label,
            "totals": {
                "gross_amount": decimal_number(sum_amount(group_records, "gross_amount")),
                "vat_amount": decimal_number(sum_amount(group_records, "vat_amount")),
            },
            "line_items": lines,
        }
        required_ids = [contact_id]
        required_ids.extend(line.get("suggested_expense_account_id") for line in lines)
        confidence = review_confidence(notes=review_notes, required_ids=required_ids)
        actions.append(
            make_action(
                period=period,
                idempotency_key=idempotency_key,
                action_type="create_purchase_summary",
                endpoint="purchases/create",
                payload=payload,
                source_refs=source_refs_for_records(normalized_path_display, group_records),
                reason=f"Aggregate {group_label} purchase-expense evidence into a draft purchase summary.",
                confidence=confidence,
                depends_on=[],
                expected_effect=f"Create a draft purchase summary for {group_label} in Simplbooks.",
                review_notes=review_notes,
            )
        )
        action_ids[(group_label, currency)] = idempotency_key

    return actions, action_ids


def build_incoming_actions(
    *,
    company_slug: str,
    period: str,
    period_end: date,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    base_currency: str,
    bank_account_id: str | None,
    bank_account_notes: list[str],
    entity_map: dict[str, Any] | None,
    sales_action_ids: dict[tuple[str, str], str],
    sales_action_ids_by_currency: dict[str, list[str]],
    fee_action_ids: dict[tuple[str, str], str],
    forced_note: str | None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    grouped_payouts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records.get("payouts", []):
        key = (record_group_label(record, default="payouts"), record_currency(record, base_currency))
        grouped_payouts[key].append(record)

    grouped_bank: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records.get("bank_transactions", []):
        if decimal_value(record.get("gross_amount")) <= 0:
            continue
        key = (record_group_label(record, default="receipts"), record_currency(record, base_currency))
        grouped_bank[key].append(record)
    repeated_incoming_labels = {
        label
        for label, count in Counter(group_label for group_label, _currency in grouped_payouts).items()
        if count > 1
    }

    for (group_label, currency), payout_records in sorted(grouped_payouts.items()):
        payout_total = sum_amount(payout_records, "gross_amount")
        if payout_total == 0:
            continue
        bank_records = grouped_bank.get((group_label, currency), [])
        bank_total = sum_amount(bank_records, "gross_amount")
        fallback_labels = CONTACT_FALLBACKS.get(slugify(group_label), ())
        contact_id, contact_notes = preferred_contact_id_with_fallbacks(
            entity_map,
            group_label=group_label,
            candidate_labels=contact_candidate_labels(
                group_label=group_label,
                records=[*payout_records, *bank_records],
            ),
            fallback_labels=fallback_labels,
        )
        review_notes = list(bank_account_notes + contact_notes)
        if forced_note:
            review_notes.append(forced_note)
        review_notes.append(record_count_note(payout_records))
        if bank_records:
            review_notes.append(f"Observed matching bank receipts total {decimal_number(bank_total)} {currency}.")
        else:
            review_notes.append("No matching bank receipt rows were attached to this draft incoming action.")

        depends_on = []
        if (group_label, currency) in sales_action_ids:
            depends_on.append(sales_action_ids[(group_label, currency)])
        else:
            depends_on.extend(sales_action_ids_by_currency.get(currency, []))
        if (group_label, currency) in fee_action_ids:
            depends_on.append(fee_action_ids[(group_label, currency)])
        depends_on = list(dict.fromkeys(depends_on))

        action_key_suffix = idempotency_suffix(
            group_label=group_label,
            currency=currency,
            repeated_labels=repeated_incoming_labels,
        )
        idempotency_key = f"{company_slug}-{period}-incoming-{action_key_suffix}"
        payload = {
            "draft_schema": "cash_settlement_v1",
            "document_type": "incoming",
            "document_date": period_end.isoformat(),
            "currency": currency,
            "counterparty": {
                "contact_id": contact_id,
                "display_name_hint": f"{group_label} incoming summary",
            },
            "counterparty_hint": group_label,
            "bank_account_id": bank_account_id,
            "amount": decimal_number(payout_total),
            "bank_receipt_total_hint": decimal_number(bank_total) if bank_records else None,
            "record_count": len(payout_records),
        }
        confidence = review_confidence(notes=review_notes, required_ids=[bank_account_id])
        source_refs = source_refs_for_records(normalized_path_display, payout_records)
        source_refs.extend(source_refs_for_records(normalized_path_display, bank_records, note="Matching bank receipt evidence."))
        actions.append(
            make_action(
                period=period,
                idempotency_key=idempotency_key,
                action_type="create_incoming_summary",
                endpoint="incomings/create",
                payload=payload,
                source_refs=source_refs,
                reason=f"Draft the processor payout incoming for {group_label} based on normalized payout evidence.",
                confidence=confidence,
                depends_on=depends_on,
                expected_effect=f"Create a draft incoming/receipt summary for {group_label} in Simplbooks.",
                review_notes=review_notes,
            )
        )
    return actions


def build_payment_actions(
    *,
    company_slug: str,
    period: str,
    period_end: date,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    base_currency: str,
    bank_account_id: str | None,
    bank_account_notes: list[str],
    entity_map: dict[str, Any] | None,
    purchase_actions: list[dict[str, Any]],
    prior_purchase_candidates: dict[tuple[str, str], list[dict[str, Any]]],
    forced_note: str | None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    current_purchase_candidates = grouped_purchase_candidates(purchase_actions, source="current")
    purchase_candidates_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for key, candidates in prior_purchase_candidates.items():
        purchase_candidates_by_key[key].extend(candidates)
    for key, candidates in current_purchase_candidates.items():
        purchase_candidates_by_key[key].extend(candidates)

    available_purchase_labels_by_currency: dict[str, set[str]] = defaultdict(set)
    for group_label, currency in purchase_candidates_by_key:
        available_purchase_labels_by_currency[currency].add(group_label)

    grouped_bank: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    grouped_match_notes: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for index, record in enumerate(records.get("bank_transactions", []), start=1):
        if decimal_value(record.get("gross_amount")) >= 0:
            continue
        currency = record_currency(record, base_currency)
        label, match_note = matched_purchase_group_for_bank_record(
            record,
            available_group_labels=available_purchase_labels_by_currency.get(currency, set()),
        )
        if not label:
            continue
        if label in FULFILLMENT_KEYWORDS:
            key = (label, currency, "aggregate")
        else:
            key = (
                label,
                currency,
                str(
                    record.get("record_id")
                    or record.get("external_ref")
                    or f"{record.get('event_date') or ''}:{record.get('description') or ''}:{index}"
                ),
            )
        grouped_bank[key].append(record)
        if match_note and match_note not in grouped_match_notes[key]:
            grouped_match_notes[key].append(match_note)

    used_purchase_actions: set[str] = set()
    used_action_keys: set[str] = set()

    for (group_label, currency, group_token), bank_records in sorted(grouped_bank.items()):
        available_candidates = [
            candidate
            for candidate in purchase_candidates_by_key.get((group_label, currency), [])
            if candidate["action_id"] not in used_purchase_actions
        ]
        if not available_candidates:
            continue

        payment_total = sum_abs_amount(bank_records, "gross_amount")
        matched_candidates = matched_purchase_candidates_for_total(
            available_candidates,
            target_total=payment_total,
        )
        if not matched_candidates:
            continue

        for candidate in matched_candidates:
            used_purchase_actions.add(candidate["action_id"])

        allocated_amounts: list[Decimal] = []
        remaining_total = payment_total
        for index, candidate in enumerate(matched_candidates, start=1):
            if index == len(matched_candidates):
                amount = remaining_total
            else:
                amount = candidate["gross_amount"]
                remaining_total -= amount
            allocated_amounts.append(amount)

        split_note = None
        if len(matched_candidates) > 1:
            linked_ids = ", ".join(candidate["action_id"] for candidate in matched_candidates)
            split_note = (
                f"Bank debit total {decimal_number(payment_total)} {currency} was allocated across "
                f"{len(matched_candidates)} purchase summaries: {linked_ids}."
            )

        for candidate, allocated_amount in zip(matched_candidates, allocated_amounts, strict=True):
            contact_id, contact_notes = preferred_contact_id(
                entity_map,
                group_label=group_label,
                candidate_labels=contact_candidate_labels(group_label=group_label, records=bank_records),
            )
            if contact_id in (None, "") and candidate.get("contact_id") not in (None, ""):
                contact_id = str(candidate["contact_id"])
                contact_notes = list(contact_notes) + [
                    f"Used contact_id {contact_id} from linked purchase action {candidate['action_id']}."
                ]

            review_notes = list(bank_account_notes + contact_notes + grouped_match_notes.get((group_label, currency, group_token), []))
            if forced_note:
                review_notes.append(forced_note)
            if split_note:
                review_notes.append(split_note)
            if candidate["source"] == "prior":
                review_notes.append(
                    f"Linked to prior-period purchase action {candidate['action_id']} from {candidate['action_period']}."
                )
            review_notes.append(record_count_note(bank_records))

            idempotency_key = payment_action_key(
                company_slug=company_slug,
                period=period,
                group_label=group_label,
                purchase_candidate=candidate,
                multiple_matches=(len(matched_candidates) > 1),
                used_keys=used_action_keys,
            )
            payload = {
                "draft_schema": "cash_settlement_v1",
                "document_type": "payment",
                "document_date": period_end.isoformat(),
                "currency": currency,
                "counterparty": {
                    "contact_id": contact_id,
                    "display_name_hint": f"{group_label} payment summary",
                },
                "counterparty_hint": group_label,
                "bank_account_id": bank_account_id,
                "amount": decimal_number(allocated_amount),
                "linked_purchase_action": candidate["action_id"],
                "linked_purchase_period": candidate["action_period"],
                "record_count": len(bank_records),
            }
            confidence = review_confidence(notes=review_notes, required_ids=[bank_account_id])
            depends_on = [candidate["action_id"]] if candidate["source"] == "current" else []
            actions.append(
                make_action(
                    period=period,
                    idempotency_key=idempotency_key,
                    action_type="create_payment_summary",
                    endpoint="payments/create",
                    payload=payload,
                    source_refs=source_refs_for_records(normalized_path_display, bank_records),
                    reason=f"Draft a payment summary for {group_label} bank debits linked to the matching purchase summary.",
                    confidence=confidence,
                    depends_on=depends_on,
                    expected_effect=f"Create a draft payment summary for {group_label} in Simplbooks.",
                    review_notes=review_notes,
                )
            )
    return actions


def summarize_actions(actions: list[dict[str, Any]], *, period: str) -> str:
    counter = Counter(action["action_type"] for action in actions)
    if not actions:
        return f"No draft actions were generated for {period}."
    parts = [f"{count} {action_type}" for action_type, count in sorted(counter.items())]
    return f"Draft batch for {period}: " + ", ".join(parts) + "."


def suppress_existing_purchase_records(
    records: dict[str, list[dict[str, Any]]],
    discovery_overview: dict[str, Any] | None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    copied = {category: list(category_records) for category, category_records in records.items()}
    existing = [
        document_identity(item, document_type=str(item.get("document_type") or "purchase"))
        for item in (discovery_overview or {}).get("document_index") or []
        if str(item.get("document_type") or "") == "purchase"
    ]
    if not existing:
        return copied, []

    kept: list[dict[str, Any]] = []
    already_present: list[dict[str, Any]] = []
    for record in copied.get("purchase_expenses", []):
        candidate = document_identity(record, document_type="purchase")
        result = match_existing(candidate, existing)
        if result.status == "ambiguous":
            raise SimplbooksError(
                f"Ambiguous existing Simplbooks purchase match for {record.get('external_ref') or record.get('record_id')}."
            )
        if result.status == "exact":
            matched = result.matches[0]
            already_present.append(
                {
                    "record_ref": record.get("record_id"),
                    "external_ref": record.get("external_ref"),
                    "document_type": "purchase",
                    "simplbooks_id": matched.simplbooks_id,
                    "reason": "Exact document identity already exists in refreshed Simplbooks discovery.",
                }
            )
            continue
        kept.append(record)
    copied["purchase_expenses"] = kept
    return copied, already_present


def build_purchase_credit_actions(
    *,
    company_slug: str,
    period: str,
    period_end: date,
    normalized_path_display: str,
    records: dict[str, list[dict[str, Any]]],
    base_currency: str,
    entity_map: dict[str, Any] | None,
    mapping_hints: dict[str, tuple[str | None, list[str]]],
    forced_note: str | None,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records.get("purchase_credits", []):
        key = (
            record_group_label(record, default="supplier"),
            record_currency(record, base_currency),
            taxable_profile(record),
        )
        grouped[key].append(record)

    actions: list[dict[str, Any]] = []
    expense_account_id, _notes = mapping_hints["fulfillment_account"]
    zero_vat_type_id, _vat_notes = mapping_hints["zero_vat_type"]
    printful_expense_account_id, printful_vat_type_id = printful_purchase_mapping(entity_map)
    repeated_labels = {
        label
        for label, count in Counter(group_label for group_label, _currency, _tax_profile in grouped).items()
        if count > 1
    }
    for (group_label, currency, tax_profile), group_records in sorted(grouped.items()):
        contact_id, contact_notes = preferred_contact_id(
            entity_map,
            group_label=group_label,
            candidate_labels=contact_candidate_labels(group_label=group_label, records=group_records),
        )
        total = sum_abs_amount(group_records, "gross_amount")
        vat_total = sum_abs_amount(group_records, "vat_amount")
        if total == 0:
            continue
        account_id = printful_expense_account_id if group_label == "printful" else expense_account_id
        vat_type_id = printful_vat_type_id if group_label == "printful" else zero_vat_type_id
        review_notes = list(contact_notes)
        if forced_note:
            review_notes.append(forced_note)
        review_notes.append(record_count_note(group_records))
        payload = {
            "draft_schema": "purchase_credit_summary_v1",
            "document_type": "purchase_credit",
            "document_date": period_end.isoformat(),
            "currency": currency,
            "counterparty": {
                "contact_id": contact_id,
                "display_name_hint": f"{group_label} supplier credit summary",
            },
            "vendor_hint": group_label,
            "totals": {"gross_amount": decimal_number(total), "vat_amount": decimal_number(vat_total)},
            "line_items": [
                {
                    "line_role": "purchase_credit",
                    "description": f"{group_label} supplier credit {tax_profile} summary",
                    "gross_amount": decimal_number(total),
                    "vat_amount_hint": decimal_number(vat_total),
                    "suggested_expense_account_id": account_id,
                    "suggested_vat_type_id": vat_type_id,
                    "warehouse_id_hint": None,
                    "record_count": len(group_records),
                }
            ],
        }
        actions.append(
            make_action(
                period=period,
                idempotency_key=(
                    f"{company_slug}-{period}-purchase-credit-{slugify(group_label)}"
                    + (f"-{slugify(tax_profile)}" if group_label in repeated_labels else "")
                ),
                action_type="create_purchase_credit_summary",
                endpoint="purchases/create",
                payload=payload,
                source_refs=source_refs_for_records(normalized_path_display, group_records),
                reason=f"Preserve {group_label} refund evidence as a separate supplier credit.",
                confidence=review_confidence(notes=review_notes, required_ids=[contact_id, account_id, vat_type_id]),
                depends_on=[],
                expected_effect=f"Create a supplier credit for {group_label} in Simplbooks.",
                review_notes=review_notes,
            )
        )
    return actions


def apply_exchange_rate_provenance(
    actions: list[dict[str, Any]],
    *,
    base_currency: str,
    exchange_rate_cache: dict[str, Any] | None,
) -> None:
    for action in actions:
        payload = action.get("payload") or {}
        currency = str(payload.get("currency") or base_currency).upper()
        if currency == base_currency.upper() or exchange_rate_cache is None:
            continue
        try:
            resolution = lookup_rate(
                exchange_rate_cache,
                requested_date=date.fromisoformat(str(payload["document_date"])),
                base=currency,
                quote=base_currency,
            )
        except (ExchangeRateError, ValueError, KeyError) as exc:
            raise SimplbooksError(f"Could not resolve audited ECB rate for {currency}: {exc}") from exc
        payload.update(
            {
                "currency_rate": decimal_number(resolution.rate),
                "currency_rate_requested_date": resolution.requested_date.isoformat(),
                "currency_rate_effective_date": resolution.effective_date.isoformat(),
                "currency_rate_provider": resolution.provider,
                "currency_rate_source_url": resolution.source_url,
            }
        )


def apply_posting_policy(
    actions: list[dict[str, Any]],
    *,
    posting_policy: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if posting_policy is None:
        return []
    unresolved: list[dict[str, Any]] = []
    for action in actions:
        payload = action.get("payload") or {}
        action_type = str(action.get("action_type") or "")
        if action_type in {"create_invoice_summary", "create_credit_invoice_summary"}:
            role = "sales"
            label = str((payload.get("summary_scope") or {}).get("channel_or_source") or "")
        elif action_type in {"create_incoming_summary"} or action_type == "create_purchase_summary" and str(payload.get("vendor_hint")) in PROCESSOR_KEYWORDS:
            role = "processors"
            label = str(payload.get("counterparty_hint") or payload.get("vendor_hint") or "")
        elif action_type in {"create_purchase_summary", "create_purchase_credit_summary", "create_payment_summary"}:
            role = "suppliers"
            label = str(payload.get("vendor_hint") or payload.get("counterparty_hint") or "")
        else:
            continue
        try:
            contact_id = resolve_contact(posting_policy, role=role, label=label)
        except PostingPolicyError as exc:
            (payload.get("counterparty") or {})["contact_id"] = None
            dependency = {
                "action_id": action.get("idempotency_key"),
                "kind": "contact_mapping",
                "role": role,
                "label": label,
                "blocking": True,
                "reason": str(exc),
            }
            if slugify(label) == "paypal":
                dependency["master_data_draft_ref"] = "artifacts/actions/master-data-paypal.yaml"
            unresolved.append(dependency)
            action.setdefault("review_notes", []).append(str(exc))
        else:
            (payload.get("counterparty") or {})["contact_id"] = contact_id

        family = ""
        if role == "sales":
            tax_profile = str((payload.get("summary_scope") or {}).get("tax_profile") or "")
            if not tax_profile:
                tax_profile = "taxable" if decimal_value((payload.get("totals") or {}).get("vat_amount")) else "non-taxable"
            family = f"{slugify(label)}-{slugify(tax_profile)}"
        elif action_type == "create_purchase_summary" and role == "processors":
            family = f"fees-{slugify(label)}"
        elif action_type in {"create_purchase_summary", "create_purchase_credit_summary"}:
            family = f"purchase-{slugify(label)}"

        if not family:
            continue
        family_values = (posting_policy.get("mappings") or {}).get(family)
        if not isinstance(family_values, dict):
            for line in payload.get("line_items") or []:
                for field_name in (
                    "suggested_income_account_id",
                    "suggested_expense_account_id",
                    "suggested_vat_type_id",
                    "warehouse_id_hint",
                ):
                    if field_name in line:
                        line[field_name] = None
            unresolved.append(
                {
                    "action_id": action.get("idempotency_key"),
                    "kind": "posting_mapping",
                    "family": family,
                    "blocking": True,
                    "reason": f"Posting family {family!r} is absent from the explicit posting policy.",
                }
            )
            continue
        payload["posting_policy_family"] = family
        for line in payload.get("line_items") or []:
            line_role = str(line.get("line_role") or "")
            if role == "sales":
                is_shipping = line_role.endswith("_shipping")
                income_field = "shipping_income_account_id" if is_shipping else "income_account_id"
                vat_field = "shipping_vat_type_id" if is_shipping else "vat_type_id"
                line["suggested_income_account_id"] = resolve_mapping(
                    posting_policy, family=family, field_name=income_field
                )
                line["suggested_vat_type_id"] = resolve_mapping(
                    posting_policy, family=family, field_name=vat_field
                )
                if family_values.get("warehouse_id") not in (None, ""):
                    line["warehouse_id_hint"] = resolve_mapping(
                        posting_policy, family=family, field_name="warehouse_id"
                    )
                else:
                    line["warehouse_id_hint"] = None
            else:
                line_key = slugify(str(line.get("description") or line_role))
                line_values = (family_values.get("lines") or {}).get(line_key)
                if line_values is None:
                    line_values = family_values
                if not isinstance(line_values, dict):
                    raise SimplbooksError(f"Posting family {family!r} line {line_key!r} must be an object.")
                line["posting_policy_line_key"] = line_key
                try:
                    line["suggested_expense_account_id"] = str(
                        int(str(line_values.get("expense_account_id") or ""))
                    )
                    line["suggested_vat_type_id"] = str(int(str(line_values.get("vat_type_id") or "")))
                except ValueError as exc:
                    raise SimplbooksError(
                        f"Posting family {family!r} line {line_key!r} requires integer-like expense_account_id and vat_type_id."
                    ) from exc
                warehouse_id = line_values.get("warehouse_id")
                line["warehouse_id_hint"] = str(warehouse_id) if warehouse_id not in (None, "") else None
        review_notes = [
            note
            for note in action.get("review_notes") or []
            if not str(note).startswith("Ambiguous entity mapping candidates:")
        ]
        action["review_notes"] = review_notes
        required_ids: list[str | None] = [str((payload.get("counterparty") or {}).get("contact_id") or "")]
        for line in payload.get("line_items") or []:
            if role == "sales":
                required_ids.extend(
                    [line.get("suggested_income_account_id"), line.get("suggested_vat_type_id")]
                )
            else:
                required_ids.extend(
                    [line.get("suggested_expense_account_id"), line.get("suggested_vat_type_id")]
                )
        action["confidence"] = review_confidence(notes=review_notes, required_ids=required_ids)
    return unresolved


def build_action_batch(
    *,
    normalized_payload: dict[str, Any],
    recon_payload: dict[str, Any],
    normalized_path: Path,
    recon_path: Path,
    repo_root: Path,
    policy_text: str | None = None,
    policy_path: Path | None = None,
    entity_map: dict[str, Any] | None = None,
    company_profile: dict[str, Any] | None = None,
    posting_policy: dict[str, Any] | None = None,
    exchange_rate_cache: dict[str, Any] | None = None,
    discovery_overview: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if normalized_payload.get("period") != recon_payload.get("period"):
        raise SimplbooksError(
            f"Period mismatch between normalized and recon artifacts: {normalized_payload.get('period')!r} vs {recon_payload.get('period')!r}"
        )
    if normalized_payload.get("company_slug") != recon_payload.get("company_slug"):
        raise SimplbooksError(
            "Company slug mismatch between normalized and recon artifacts."
        )
    if not recon_payload.get("approve_for_build") and not force:
        raise SimplbooksError(
            f"Reconciliation does not approve period {recon_payload.get('period')}; rerun with --force only if you intentionally want a draft from blocked evidence."
        )

    company_slug = str(normalized_payload["company_slug"])
    period = str(normalized_payload["period"])
    _, period_end = parse_period(period)
    base_currency = str(normalized_payload.get("base_currency") or normalized_payload.get("currency") or "EUR")
    normalized_path_display = display_path(normalized_path, repo_root)
    recon_path_display = display_path(recon_path, repo_root)
    forced_note = None
    if force and not recon_payload.get("approve_for_build"):
        forced_note = "Draft was forced even though recon did not approve this month."

    mapping_hints = preferred_mapping_hints(entity_map)
    records, already_present = suppress_existing_purchase_records(
        normalized_payload.get("records") or {},
        discovery_overview,
    )
    bank_account_id, bank_account_notes = preferred_bank_account_id(company_profile, entity_map)
    if posting_policy is not None and records.get("bank_transactions"):
        source_bank_records = [
            record
            for record in records["bank_transactions"]
            if slugify(str(record.get("source_system") or "")) == "bank"
        ]
        missing_source_accounts = [
            str(record.get("record_id") or "<unknown>")
            for record in source_bank_records
            if not str((record.get("attributes") or {}).get("customer_account") or "").strip()
        ]
        if missing_source_accounts:
            raise SimplbooksError(
                f"Posting policy found bank row(s) missing source bank account: {', '.join(missing_source_accounts[:3])}."
            )
        source_accounts = {
            re.sub(r"\s+", "", str((record.get("attributes") or {}).get("customer_account") or "")).upper()
            for record in source_bank_records
        }
        source_accounts.discard("")
        if len(source_accounts) != 1:
            raise SimplbooksError("Posting policy requires exactly one source bank account in normalized bank rows.")
        try:
            bank_account_id = resolve_bank_account(posting_policy, customer_account=next(iter(source_accounts)))
        except PostingPolicyError as exc:
            raise SimplbooksError(str(exc)) from exc
        bank_account_notes = ["Applied exact source-bank-account mapping from posting policy."]
    artifacts_dir = inferred_artifacts_dir(normalized_path)
    prior_purchase_candidates = historical_purchase_candidates(
        actions_dir=(artifacts_dir / "actions") if artifacts_dir is not None else None,
        current_period=period,
    )

    sales_actions, sales_action_ids, sales_action_ids_by_currency = build_sales_actions(
        company_slug=company_slug,
        period=period,
        period_end=period_end,
        normalized_path_display=normalized_path_display,
        records=records,
        base_currency=base_currency,
        policy_text=policy_text,
        entity_map=entity_map,
        mapping_hints=mapping_hints,
        forced_note=forced_note,
    )
    fee_actions, fee_action_ids = build_fee_actions(
        company_slug=company_slug,
        period=period,
        period_end=period_end,
        normalized_path_display=normalized_path_display,
        records=records,
        base_currency=base_currency,
        entity_map=entity_map,
        mapping_hints=mapping_hints,
        forced_note=forced_note,
    )
    purchase_actions, purchase_action_ids = build_purchase_actions(
        company_slug=company_slug,
        period=period,
        period_end=period_end,
        normalized_path_display=normalized_path_display,
        records=records,
        base_currency=base_currency,
        entity_map=entity_map,
        mapping_hints=mapping_hints,
        forced_note=forced_note,
    )
    purchase_credit_actions = build_purchase_credit_actions(
        company_slug=company_slug,
        period=period,
        period_end=period_end,
        normalized_path_display=normalized_path_display,
        records=records,
        base_currency=base_currency,
        entity_map=entity_map,
        mapping_hints=mapping_hints,
        forced_note=forced_note,
    )
    incoming_actions = build_incoming_actions(
        company_slug=company_slug,
        period=period,
        period_end=period_end,
        normalized_path_display=normalized_path_display,
        records=records,
        base_currency=base_currency,
        bank_account_id=bank_account_id,
        bank_account_notes=bank_account_notes,
        entity_map=entity_map,
        sales_action_ids=sales_action_ids,
        sales_action_ids_by_currency=sales_action_ids_by_currency,
        fee_action_ids=fee_action_ids,
        forced_note=forced_note,
    )
    payment_actions = build_payment_actions(
        company_slug=company_slug,
        period=period,
        period_end=period_end,
        normalized_path_display=normalized_path_display,
        records=records,
        base_currency=base_currency,
        bank_account_id=bank_account_id,
        bank_account_notes=bank_account_notes,
        entity_map=entity_map,
        purchase_actions=purchase_actions,
        prior_purchase_candidates=prior_purchase_candidates,
        forced_note=forced_note,
    )

    actions = sales_actions + fee_actions + purchase_actions + purchase_credit_actions + incoming_actions + payment_actions
    unresolved_dependencies = apply_posting_policy(actions, posting_policy=posting_policy)
    apply_exchange_rate_provenance(
        actions,
        base_currency=base_currency,
        exchange_rate_cache=exchange_rate_cache,
    )
    summary_parts = [summarize_actions(actions, period=period)]
    if policy_path and policy_text is not None:
        summary_parts.append(f"Policy memo: {display_path(policy_path, repo_root)}.")
    summary_parts.append(f"Recon ref: {recon_path_display}.")
    if recon_payload.get("checks"):
        warn_count = sum(1 for check in recon_payload["checks"] if check.get("status") == "warn")
        if warn_count:
            summary_parts.append(f"Recon still carries {warn_count} warning check(s).")
    source_summary = " ".join(summary_parts)

    return {
        "schema_version": "1.0",
        "company_slug": company_slug,
        "period": period,
        "generated_at": utc_now_iso(),
        "batch_id": f"{company_slug}-{period}-draft",
        "approval_status": "draft",
        "source_summary": source_summary,
        "recon_ref": recon_path_display,
        "already_present": already_present,
        "unresolved_dependencies": unresolved_dependencies,
        "actions": actions,
    }


def inferred_artifacts_dir(path: Path) -> Path | None:
    if path.parent.name in {"normalized", "recon", "actions"}:
        return path.parent.parent
    return None


def resolve_normalized_path(*, company_dir: Path | None, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is None:
        raise SimplbooksError("Pass --normalized when --company-dir is not provided.")
    return company_dir / "artifacts" / "normalized" / f"{period}.json"


def resolve_recon_path(*, company_dir: Path | None, normalized_path: Path, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "recon" / f"{period}.json"
    artifacts_dir = inferred_artifacts_dir(normalized_path)
    if artifacts_dir is None:
        raise SimplbooksError("Could not infer recon path; pass --recon explicitly.")
    return artifacts_dir / "recon" / f"{period}.json"


def resolve_policy_path(*, company_dir: Path | None, normalized_path: Path, override: str | None) -> Path | None:
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


def resolve_company_profile_path(*, company_dir: Path | None, normalized_path: Path, override: str | None) -> Path | None:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "company_profile.json"
    artifacts_dir = inferred_artifacts_dir(normalized_path)
    if artifacts_dir is None:
        return None
    return artifacts_dir / "company_profile.json"


def resolve_reference_path(
    *,
    company_dir: Path | None,
    normalized_path: Path,
    override: str | None,
    filename: str,
) -> Path | None:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / filename
    artifacts_dir = inferred_artifacts_dir(normalized_path)
    return (artifacts_dir / filename) if artifacts_dir is not None else None


def resolve_output_path(*, company_dir: Path | None, normalized_path: Path, period: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if company_dir is not None:
        return company_dir / "artifacts" / "actions" / f"{period}.yaml"
    artifacts_dir = inferred_artifacts_dir(normalized_path)
    if artifacts_dir is not None:
        return artifacts_dir / "actions" / f"{period}.yaml"
    return normalized_path.with_name(f"{period}.actions.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build draft Simplbooks action batches from reconciled month artifacts")
    parser.add_argument("--company-dir", help="Company folder, e.g. companies/example")
    parser.add_argument("--period", required=True, help="Target month in YYYY-MM format")
    parser.add_argument("--normalized", help="Path to normalized JSON. Defaults to companies/<company>/artifacts/normalized/<period>.json")
    parser.add_argument("--recon", help="Path to recon JSON. Defaults to companies/<company>/artifacts/recon/<period>.json")
    parser.add_argument("--policy-memo", help="Optional path to policy memo markdown")
    parser.add_argument("--entity-map", help="Optional path to entity map JSON")
    parser.add_argument("--company-profile", help="Optional path to company profile JSON")
    parser.add_argument("--posting-policy", help="Posting policy JSON; defaults to company artifacts/posting_policy.json")
    parser.add_argument("--exchange-rates", help="Annual ECB cache; defaults to company artifacts/reference/ecb-rates-<year>.json")
    parser.add_argument("--discovery-overview", help="Refreshed Simplbooks overview; defaults to company artifacts/discovery/<year>-overview.json")
    parser.add_argument("--output", help="Optional output path for actions YAML")
    parser.add_argument("--force", action="store_true", help="Allow draft generation even when recon does not approve the month")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    company_dir = Path(args.company_dir) if args.company_dir else None
    normalized_path = resolve_normalized_path(company_dir=company_dir, period=args.period, override=args.normalized)
    recon_path = resolve_recon_path(company_dir=company_dir, normalized_path=normalized_path, period=args.period, override=args.recon)
    policy_path = resolve_policy_path(company_dir=company_dir, normalized_path=normalized_path, override=args.policy_memo)
    entity_map_path = resolve_entity_map_path(company_dir=company_dir, normalized_path=normalized_path, override=args.entity_map)
    company_profile_path = resolve_company_profile_path(
        company_dir=company_dir,
        normalized_path=normalized_path,
        override=args.company_profile,
    )
    year = int(args.period[:4])
    posting_policy_path = resolve_reference_path(
        company_dir=company_dir,
        normalized_path=normalized_path,
        override=args.posting_policy,
        filename="posting_policy.json",
    )
    exchange_rates_path = resolve_reference_path(
        company_dir=company_dir,
        normalized_path=normalized_path,
        override=args.exchange_rates,
        filename=f"reference/ecb-rates-{year}.json",
    )
    discovery_overview_path = resolve_reference_path(
        company_dir=company_dir,
        normalized_path=normalized_path,
        override=args.discovery_overview,
        filename=f"discovery/{year}-overview.json",
    )
    output_path = resolve_output_path(company_dir=company_dir, normalized_path=normalized_path, period=args.period, override=args.output)

    normalized_payload = load_json(normalized_path)
    recon_payload = load_json(recon_path)
    policy_text = load_optional_text(policy_path)
    entity_map = load_optional_json(entity_map_path)
    company_profile = load_optional_json(company_profile_path)
    reference_artifacts_required = company_dir is not None or any(
        (args.posting_policy, args.exchange_rates, args.discovery_overview)
    )
    if reference_artifacts_required and (posting_policy_path is None or not posting_policy_path.exists()):
        raise SimplbooksError(f"Required posting policy not found: {posting_policy_path}")
    if reference_artifacts_required and (discovery_overview_path is None or not discovery_overview_path.exists()):
        raise SimplbooksError(f"Required discovery overview not found: {discovery_overview_path}")
    foreign_currencies = {
        str(record.get("currency") or normalized_payload.get("base_currency") or "EUR").upper()
        for category_records in (normalized_payload.get("records") or {}).values()
        for record in category_records
        if isinstance(record, dict)
        and str(record.get("currency") or normalized_payload.get("base_currency") or "EUR").upper()
        != str(normalized_payload.get("base_currency") or "EUR").upper()
    }
    if foreign_currencies and (exchange_rates_path is None or not exchange_rates_path.exists()):
        raise SimplbooksError(f"Required annual ECB exchange-rate cache not found: {exchange_rates_path}")

    posting_policy = load_posting_policy(posting_policy_path) if posting_policy_path and posting_policy_path.exists() else None
    exchange_rate_cache = load_optional_json(exchange_rates_path)
    discovery_overview = load_optional_json(discovery_overview_path)
    if posting_policy and posting_policy.get("company_slug") != normalized_payload.get("company_slug"):
        raise SimplbooksError("Posting policy company_slug does not match normalized company_slug.")
    if discovery_overview and company_dir is not None:
        try:
            validate_discovery(
                discovery_overview,
                year=year,
                company_id=resolve_company_id(None, company_dir=str(company_dir)),
            )
        except ReferenceArtifactError as exc:
            raise SimplbooksError(str(exc)) from exc
    repo_root = Path.cwd()

    batch = build_action_batch(
        normalized_payload=normalized_payload,
        recon_payload=recon_payload,
        normalized_path=normalized_path,
        recon_path=recon_path,
        repo_root=repo_root,
        policy_text=policy_text,
        policy_path=policy_path if policy_text is not None else None,
        entity_map=entity_map,
        company_profile=company_profile,
        posting_policy=posting_policy,
        exchange_rate_cache=exchange_rate_cache,
        discovery_overview=discovery_overview,
        force=args.force,
    )
    bound_paths = [
        ("posting_policy", posting_policy_path),
        ("discovery_overview", discovery_overview_path),
    ]
    if foreign_currencies:
        bound_paths.append(("exchange_rates", exchange_rates_path))
    batch["reference_artifacts"] = [
        bind_file(path, kind=kind, cwd=repo_root)
        for kind, path in bound_paths
        if path is not None
    ]
    write_yaml(output_path, batch)

    company_slug = str(normalized_payload.get("company_slug") or (company_dir.name if company_dir else normalized_path.stem))
    company_name = resolve_company_name(company_dir=args.company_dir) if args.company_dir else None
    company_name = company_name or company_slug
    summary = {
        "company_name": company_name,
        "company_slug": company_slug,
        "period": args.period,
        "normalized": str(normalized_path),
        "recon": str(recon_path),
        "output": str(output_path),
        "action_count": len(batch["actions"]),
        "action_types": dict(sorted(Counter(action["action_type"] for action in batch["actions"]).items())),
        "approval_status": batch["approval_status"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
