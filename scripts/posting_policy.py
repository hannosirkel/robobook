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


CASH_POSTING_MODES = frozenset({"api", "statement_import"})

CASH_POSTING_KEYS = frozenset(
    {
        "mode",
        "bank_income_account_ids",
        "processor_income_account_ids",
        "bank_financial_accounts",
        "clearing_provider_roles",
        "financial_accounts",
    }
)

REQUIRED_FINANCIAL_ACCOUNT_ROLES = frozenset(
    {
        "bank",
        "stripe_clearing",
        "paypal",
        "bank_fees",
        "reporting_person_payable",
        "platform_prepayment",
        "customer_receivable",
        "supplier_payable",
        "fx_gain",
        "fx_loss",
    }
)

OPTIONAL_FINANCIAL_ACCOUNT_ROLES = frozenset({"inventory_change", "set_off"})

KNOWN_FINANCIAL_ACCOUNT_ROLES = REQUIRED_FINANCIAL_ACCOUNT_ROLES | OPTIONAL_FINANCIAL_ACCOUNT_ROLES

ORDER_ROUTED_CHANNELS = ("woo",)

WAREHOUSE_ROUTING_KEYS = frozenset({*ORDER_ROUTED_CHANNELS, "direct_sale_warehouse_id", "distributor_warehouse_id"})

WAREHOUSE_ROUTING_RULE_KEYS = frozenset({"before_order", "before_warehouse_id", "from_warehouse_id"})


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
                if isinstance(child, list):
                    normalized_dated_bands(child, value_key=str(key), field_name=child_path)
                else:
                    normalize_id(child, field_name=child_path)

    validate_mapping_ids(payload["mappings"], path="mappings")
    if "sales_vat_profiles" in payload:
        validated_sales_vat_profiles(payload)
    validated_cash_posting(payload)
    validated_warehouse_routing(payload)
    non_inventory_event_types(payload)
    accepted_checker_warnings(payload)


def _cash_posting_bank_ids(section: dict[str, Any], *, required: bool) -> list[str]:
    value = section.get("bank_income_account_ids")
    if value is None and not required:
        return []
    if not isinstance(value, list) or not value:
        raise PostingPolicyError(
            "cash_posting.bank_income_account_ids must be a non-empty array of Simplbooks income-account IDs."
        )
    resolved = [
        normalize_id(item, field_name=f"cash_posting.bank_income_account_ids[{index}]")
        for index, item in enumerate(value)
    ]
    if len(set(resolved)) != len(resolved):
        raise PostingPolicyError("cash_posting.bank_income_account_ids must not repeat an ID.")
    return resolved


def _cash_posting_processor_ids(section: dict[str, Any], *, required: bool) -> dict[str, str]:
    value = section.get("processor_income_account_ids")
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise PostingPolicyError("cash_posting.processor_income_account_ids must be an object.")
    resolved: dict[str, str] = {}
    for key, account_id in value.items():
        label = slugify(key)
        if not label:
            raise PostingPolicyError("cash_posting.processor_income_account_ids keys cannot be empty.")
        resolved[label] = normalize_id(
            account_id, field_name=f"cash_posting.processor_income_account_ids[{key!r}]"
        )
    return resolved


def _cash_posting_financial_accounts(section: dict[str, Any], *, required: bool) -> dict[str, str]:
    value = section.get("financial_accounts")
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise PostingPolicyError("cash_posting.financial_accounts must be an object.")
    unknown_roles = sorted(set(value) - KNOWN_FINANCIAL_ACCOUNT_ROLES)
    if unknown_roles:
        raise PostingPolicyError(f"cash_posting.financial_accounts has unknown role(s): {', '.join(unknown_roles)}.")
    resolved = {
        role: normalize_id(account_id, field_name=f"cash_posting.financial_accounts[{role!r}]")
        for role, account_id in value.items()
    }
    missing_roles = sorted(REQUIRED_FINANCIAL_ACCOUNT_ROLES - set(resolved)) if required else []
    if missing_roles:
        raise PostingPolicyError(
            f"cash_posting.financial_accounts is missing required role(s): {', '.join(missing_roles)}."
        )
    return resolved


def _cash_posting_bank_financial_accounts(section: dict[str, Any], *, required: bool) -> dict[str, Any]:
    value = section.get("bank_financial_accounts")
    if value is None and not required:
        return {}
    if not isinstance(value, dict) or not value:
        raise PostingPolicyError(
            "cash_posting.bank_financial_accounts must map each imported statement account to its ledger account."
        )
    for account, mapping in value.items():
        field = f"cash_posting.bank_financial_accounts[{account!r}]"
        if not str(account).strip():
            raise PostingPolicyError("cash_posting.bank_financial_accounts keys cannot be empty.")
        if isinstance(mapping, dict):
            if not mapping:
                raise PostingPolicyError(f"{field} requires at least one currency mapping.")
            for currency, account_id in mapping.items():
                if not re.fullmatch(r"[A-Z]{3}", str(currency or "").strip()):
                    raise PostingPolicyError(f"{field} has invalid currency {currency!r}.")
                normalize_id(account_id, field_name=f"{field}[{currency!r}]")
        else:
            normalize_id(mapping, field_name=field)
    return value


def _cash_posting_clearing_provider_roles(
    section: dict[str, Any], *, required: bool, accounts: dict[str, str]
) -> dict[str, str]:
    value = section.get("clearing_provider_roles")
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise PostingPolicyError("cash_posting.clearing_provider_roles must be an object.")
    resolved: dict[str, str] = {}
    for provider, role in value.items():
        label = slugify(provider)
        if not label:
            raise PostingPolicyError("cash_posting.clearing_provider_roles keys cannot be empty.")
        role_name = str(role or "").strip()
        if role_name not in KNOWN_FINANCIAL_ACCOUNT_ROLES:
            raise PostingPolicyError(
                f"cash_posting.clearing_provider_roles[{provider!r}] names unknown role {role_name!r}."
            )
        if role_name not in accounts:
            raise PostingPolicyError(
                f"cash_posting.clearing_provider_roles[{provider!r}] names role {role_name!r}, "
                "which has no bound financial account."
            )
        resolved[label] = role_name
    return resolved


def validated_cash_posting(policy: dict[str, Any]) -> dict[str, Any]:
    """Normalize the optional cash-posting section; an absent section means legacy API cash posting."""
    section = policy.get("cash_posting")
    if section is None:
        return {"mode": "api", "bank_income_account_ids": [], "processor_income_account_ids": {}, "financial_accounts": {}}
    if not isinstance(section, dict):
        raise PostingPolicyError("cash_posting must be an object.")
    unknown_keys = sorted(set(section) - CASH_POSTING_KEYS)
    if unknown_keys:
        raise PostingPolicyError(f"cash_posting has unknown key(s): {', '.join(unknown_keys)}.")

    mode = str(section.get("mode") or "").strip()
    if mode not in CASH_POSTING_MODES:
        raise PostingPolicyError(
            f"cash_posting.mode must be one of {sorted(CASH_POSTING_MODES)}, got {section.get('mode')!r}."
        )

    required = mode == "statement_import"
    bank_ids = _cash_posting_bank_ids(section, required=required)
    processors = _cash_posting_processor_ids(section, required=required)
    accounts = _cash_posting_financial_accounts(section, required=required)
    bank_ledgers = _cash_posting_bank_financial_accounts(section, required=required)
    clearing_roles = _cash_posting_clearing_provider_roles(section, required=required, accounts=accounts)

    shared = sorted(set(bank_ids) & set(processors.values()))
    if shared:
        raise PostingPolicyError(
            f"cash_posting bank and processor income accounts must be disjoint; shared ID(s): {', '.join(shared)}."
        )
    return {
        "mode": mode,
        "bank_income_account_ids": bank_ids,
        "processor_income_account_ids": processors,
        "bank_financial_accounts": bank_ledgers,
        "clearing_provider_roles": clearing_roles,
        "financial_accounts": accounts,
    }


def cash_posting_mode(policy: dict[str, Any]) -> str:
    return validated_cash_posting(policy)["mode"]


def accepted_checker_warnings(policy: dict[str, Any] | None) -> list[str]:
    """Warning texts this company has reviewed and accepted as permanently true.

    A live run refuses to write while any checker warning is unresolved. Some
    warnings are structural and will never clear, so they are declared here and
    matched as substrings. An undeclared warning still stops the run, which is the
    whole point: this narrows the gate, it does not remove it.
    """
    declared = (policy or {}).get("accepted_checker_warnings")
    if declared is None:
        return []
    if not isinstance(declared, list):
        raise PostingPolicyError("accepted_checker_warnings must be an array of warning texts.")
    accepted: list[str] = []
    for index, entry in enumerate(declared):
        text = entry.strip() if isinstance(entry, str) else ""
        if not text:
            # A blank pattern is a substring of every warning and would disable the gate.
            raise PostingPolicyError(
                f"accepted_checker_warnings[{index}] must be a non-empty warning text."
            )
        accepted.append(text)
    return accepted


def posting_scope_first_period(policy: dict[str, Any] | None) -> str | None:
    """Return the first period this company posts, or None when it declares no scope."""
    scope = (policy or {}).get("posting_scope")
    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise PostingPolicyError("posting_scope must be an object.")
    first_period = str(scope.get("first_period") or "")
    if not re.fullmatch(r"\d{4}-\d{2}", first_period):
        raise PostingPolicyError(
            f"posting_scope.first_period must be a YYYY-MM period, got {first_period!r}."
        )
    return first_period


def statement_import_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Return the normalized cash-posting section, refusing any non-`statement_import` policy."""
    resolved = validated_cash_posting(policy)
    if resolved["mode"] != "statement_import":
        raise PostingPolicyError(
            f"Posting policy cash mode is {resolved['mode']!r}, not 'statement_import'."
        )
    return resolved


def bank_income_account_ids(policy: dict[str, Any]) -> set[str]:
    return set(validated_cash_posting(policy)["bank_income_account_ids"])


def _warehouse_routing_rule(rule: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise PostingPolicyError(f"{path} must be an object.")
    extra = sorted(set(rule) - WAREHOUSE_ROUTING_RULE_KEYS)
    if extra:
        raise PostingPolicyError(f"{path} has unknown key(s): {', '.join(extra)}.")
    for field_name in sorted(WAREHOUSE_ROUTING_RULE_KEYS):
        if field_name not in rule:
            raise PostingPolicyError(f"{path} requires {field_name}.")
    before_order = rule["before_order"]
    if isinstance(before_order, bool) or not isinstance(before_order, int) or before_order <= 0:
        raise PostingPolicyError(
            f"{path}.before_order must be a positive integer order number, got {before_order!r}."
        )
    return {
        "before_order": before_order,
        "before_warehouse_id": normalize_id(rule["before_warehouse_id"], field_name=f"{path}.before_warehouse_id"),
        "from_warehouse_id": normalize_id(rule["from_warehouse_id"], field_name=f"{path}.from_warehouse_id"),
    }


def non_inventory_event_types(policy: dict[str, Any]) -> frozenset[str]:
    """Event types a reviewer has declared never to bear inventory.

    A chargeback reverses cash without returning goods, and a dispute fee is not goods at
    all. Neither should attract an article, and neither should be held back waiting for a
    quantity that will never exist. Which events those are is a reviewed decision, not
    something to read off an event name.
    """
    values = policy.get("non_inventory_event_types")
    if values is None:
        return frozenset()
    if not isinstance(values, list):
        raise PostingPolicyError("non_inventory_event_types must be an array of event types.")
    resolved = {str(value).strip() for value in values}
    if not all(resolved) or len(resolved) != len(values):
        raise PostingPolicyError("non_inventory_event_types entries must be unique and non-empty.")
    return frozenset(resolved)


def validated_warehouse_routing(policy: dict[str, Any]) -> dict[str, Any]:
    """Normalize reviewed sales-warehouse routing; an unbound warehouse stays None until it exists."""
    section = policy.get("warehouse_routing")
    if section is None:
        return {"channels": {}, "direct_sale_warehouse_id": None, "distributor_warehouse_id": None}
    if not isinstance(section, dict):
        raise PostingPolicyError("warehouse_routing must be an object.")
    unknown_keys = sorted(set(section) - WAREHOUSE_ROUTING_KEYS)
    if unknown_keys:
        raise PostingPolicyError(f"warehouse_routing has unknown key(s): {', '.join(unknown_keys)}.")

    channels = {
        channel: _warehouse_routing_rule(section[channel], path=f"warehouse_routing.{channel}")
        for channel in ORDER_ROUTED_CHANNELS
        if section.get(channel) is not None
    }
    resolved: dict[str, Any] = {"channels": channels}
    for field_name in ("direct_sale_warehouse_id", "distributor_warehouse_id"):
        value = section.get(field_name)
        resolved[field_name] = (
            None if value is None else normalize_id(value, field_name=f"warehouse_routing.{field_name}")
        )
    return resolved


def resolve_sales_warehouse(
    policy: dict[str, Any],
    *,
    channel: str,
    order_number: int | None = None,
) -> str:
    """Resolve one reviewed sales warehouse; never guess a warehouse from anything but policy plus source facts."""
    routing = validated_warehouse_routing(policy)
    key = slugify(channel)
    rule = routing["channels"].get(key)
    if rule is not None:
        if isinstance(order_number, bool) or not isinstance(order_number, int):
            raise PostingPolicyError(
                f"Sales channel {channel!r} requires an exact reviewed order number, got {order_number!r}."
            )
        return rule["before_warehouse_id"] if order_number < rule["before_order"] else rule["from_warehouse_id"]
    if key in {"direct-sale", "distributor"}:
        field_name = "direct_sale_warehouse_id" if key == "direct-sale" else "distributor_warehouse_id"
        warehouse_id = routing[field_name]
        if warehouse_id is None:
            raise PostingPolicyError(
                f"warehouse_routing.{field_name} is not bound to a reviewed Simplbooks warehouse."
            )
        return warehouse_id
    raise PostingPolicyError(f"No reviewed warehouse-routing rule exists for sales channel {channel!r}.")


def load_posting_policy(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PostingPolicyError(f"Could not load posting policy {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PostingPolicyError(f"Posting policy {path} must contain a JSON object.")
    validate_posting_policy(payload)
    return payload


def resolve_account_mapping(
    mappings: Any,
    *,
    customer_account: str,
    currency: str | None,
    field: str,
    allow_legacy_single_currency: bool = False,
) -> str:
    """Resolve one `(source account[, currency])` mapping to an exact Simplbooks ID.

    `field` names the policy path the mapping lives at, so a failure points at the
    exact key to fix rather than at a prose description of it.
    """
    source_account = re.sub(r"\s+", "", str(customer_account or "")).upper()
    normalized_mappings = {
        re.sub(r"\s+", "", str(key)).upper(): value
        for key, value in (mappings or {}).items()
    }
    if source_account not in normalized_mappings:
        raise PostingPolicyError(f"No exact {field} mapping exists for source account {customer_account!r}.")
    account_mapping = normalized_mappings[source_account]
    if isinstance(account_mapping, dict):
        normalized_currency = str(currency or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
            raise PostingPolicyError(f"{field} mapping for {customer_account!r} requires a three-letter currency.")
        currency_mappings = {str(key).strip().upper(): value for key, value in account_mapping.items()}
        if normalized_currency not in currency_mappings:
            raise PostingPolicyError(
                f"No exact {field} mapping exists for source account {customer_account!r} and currency {normalized_currency}."
            )
        return normalize_id(
            currency_mappings[normalized_currency],
            field_name=f"{field}[{customer_account!r}][{normalized_currency!r}]",
        )
    if currency is not None and not allow_legacy_single_currency:
        raise PostingPolicyError(
            f"{field} mapping for {customer_account!r} must specify currency {str(currency).upper()!r}."
        )
    return normalize_id(account_mapping, field_name=f"{field}[{customer_account!r}]")


def resolve_bank_account(
    policy: dict[str, Any],
    *,
    customer_account: str,
    currency: str | None = None,
    allow_legacy_single_currency: bool = False,
) -> str:
    return resolve_account_mapping(
        policy.get("bank_accounts"),
        customer_account=customer_account,
        currency=currency,
        field="bank_accounts",
        allow_legacy_single_currency=allow_legacy_single_currency,
    )


def resolve_bank_financial_account(policy: dict[str, Any], *, iban: str, currency: str) -> str:
    """Resolve the ledger account behind one imported statement account, never a similar one."""
    return resolve_account_mapping(
        statement_import_policy(policy)["bank_financial_accounts"],
        customer_account=iban,
        currency=currency,
        field="cash_posting.bank_financial_accounts",
    )


def resolve_clearing_account(policy: dict[str, Any], *, provider: str) -> tuple[str, str]:
    """Resolve one reviewed clearing provider to its `(role, account ID)` pair."""
    resolved = statement_import_policy(policy)
    role = resolved["clearing_provider_roles"].get(slugify(provider))
    if role is None:
        raise PostingPolicyError(f"No reviewed financial-account role exists for clearing provider {provider!r}.")
    return role, resolved["financial_accounts"][role]


def resolve_contact(policy: dict[str, Any], *, role: str, label: str) -> str:
    role_mappings = (policy.get("contacts") or {}).get(slugify(role)) or {}
    normalized = {slugify(key): value for key, value in role_mappings.items()}
    key = slugify(label)
    if key not in normalized:
        raise PostingPolicyError(f"No explicit {role!r} contact mapping exists for {label!r}.")
    return normalize_id(normalized[key], field_name=f"contacts[{role!r}][{label!r}]")


def normalized_dated_bands(bands: Any, *, value_key: str, field_name: str) -> list[dict[str, Any]]:
    """Parse a dated mapping pin into ordered bands, proving the bands never overlap."""
    if not isinstance(bands, list) or not bands:
        raise PostingPolicyError(f"{field_name} must be a non-empty array of dated bands.")
    normalized: list[dict[str, Any]] = []
    for index, band in enumerate(bands):
        path = f"{field_name}[{index}]"
        if not isinstance(band, dict):
            raise PostingPolicyError(f"{path} must be an object.")
        start = parse_profile_date(band.get("start"), field_name=f"{path}.start")
        end_value = band.get("end")
        end = None if end_value is None else parse_profile_date(end_value, field_name=f"{path}.end")
        if end is not None and end < start:
            raise PostingPolicyError(f"{path}.end cannot be before {path}.start.")
        normalized.append(
            {
                "start": start,
                "end": end,
                "value": normalize_id(band.get(value_key), field_name=f"{path}.{value_key}"),
            }
        )
    for index, band in enumerate(normalized):
        for other in normalized[index + 1 :]:
            band_end = band["end"] or date.max
            other_end = other["end"] or date.max
            if band["start"] <= other_end and other["start"] <= band_end:
                raise PostingPolicyError(f"{field_name} bands must not overlap.")
    return normalized


def resolve_dated_mapping_bands(
    bands: list[Any], *, value_key: str, field_name: str, event_date: date | None
) -> str:
    """Select the single band covering event_date from a dated mapping pin."""
    if event_date is None:
        raise PostingPolicyError(f"{field_name} is date-aware and requires an event date to resolve.")
    normalized = normalized_dated_bands(bands, value_key=value_key, field_name=field_name)
    matches = [
        band["value"]
        for band in normalized
        if band["start"] <= event_date and (band["end"] is None or event_date <= band["end"])
    ]
    if len(matches) != 1:
        raise PostingPolicyError(
            f"Expected exactly one {field_name} band for {event_date.isoformat()}, found {len(matches)}."
        )
    return matches[0]


def resolve_mapping(
    policy: dict[str, Any], *, family: str, field_name: str, event_date: date | None = None
) -> str:
    family_mapping = (policy.get("mappings") or {}).get(slugify(family)) or {}
    value = family_mapping.get(field_name)
    if value in (None, "") or (isinstance(value, list) and not value):
        raise PostingPolicyError(f"Posting family {family!r} has no explicit {field_name!r} mapping.")
    located = f"mappings[{family!r}][{field_name!r}]"
    if isinstance(value, list):
        return resolve_dated_mapping_bands(
            value, value_key=field_name, field_name=located, event_date=event_date
        )
    return normalize_id(value, field_name=located)


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


CASH_ACTION_TYPES = frozenset({"create_incoming_summary", "create_payment_summary"})


def prohibited_bank_cash_action(action: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Report whether one action would post API cash against an imported statement account.

    Builder, checker, and sender each ask this independently. In statement-import mode
    the physical row is settled by the import itself, so a second API movement against
    the same account would duplicate the cash rather than record it.
    """
    if cash_posting_mode(policy) != "statement_import":
        return False
    if str(action.get("action_type") or "") not in CASH_ACTION_TYPES:
        return False
    payload = action.get("payload") or {}
    return str(payload.get("bank_account_id") or "") in bank_income_account_ids(policy)


def recomputed_sales_routing(
    payload: dict[str, Any], policy: dict[str, Any]
) -> tuple[str | None, list[str]]:
    """Recompute a declared warehouse routing from policy rather than trusting the batch."""
    declared = (payload.get("summary_scope") or {}).get("warehouse_routing")
    if not isinstance(declared, dict):
        return None, []
    channel = str(declared.get("channel") or "")
    warehouse_id = str(declared.get("warehouse_id") or "")
    order_numbers = declared.get("order_numbers")
    if not channel or not warehouse_id or not isinstance(order_numbers, list) or not order_numbers:
        return None, ["Declared warehouse routing requires a channel, warehouse, and order numbers."]
    errors: list[str] = []
    for order_number in order_numbers:
        if isinstance(order_number, bool) or not isinstance(order_number, int):
            errors.append(f"Declared warehouse routing order number {order_number!r} is not an exact integer.")
            continue
        try:
            expected = resolve_sales_warehouse(policy, channel=channel, order_number=order_number)
        except PostingPolicyError as exc:
            errors.append(str(exc))
            continue
        if expected != warehouse_id:
            errors.append(
                f"Order {order_number} routes to warehouse {expected!r}, not the declared {warehouse_id!r}."
            )
    return (None, errors) if errors else (warehouse_id, [])


def action_policy_errors(action: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    """Independently compare every submit-capable ID in an action with explicit policy."""
    payload = action.get("payload") or {}
    action_type = str(action.get("action_type") or "")
    if action_type in {"create_invoice_summary", "create_credit_invoice_summary"}:
        role = "sales"
        label = str((payload.get("summary_scope") or {}).get("channel_or_source") or "")
    elif action_type == "create_incoming_summary" and str(payload.get("settlement_family") or "") == "direct-sale":  # noqa: SIM114
        role = "sales"
        label = str(payload.get("counterparty_hint") or "")
    elif action_type == "create_incoming_summary" and (
        payload.get("linked_invoice_id") not in (None, "")
        or payload.get("linked_invoice_action") not in (None, "")
    ):
        role = "sales"
        label = str(payload.get("counterparty_hint") or "")
    elif action_type == "create_incoming_summary" or (
        action_type in {"create_purchase_summary", "create_payment_summary"}
        and slugify(str(payload.get("vendor_hint") or "")) in {"paypal", "stripe"}
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
        # Reviewed processor settlement accounts are explicit policy values too, and in
        # statement-import mode they are the only cash accounts the API may still touch.
        cash_posting = validated_cash_posting(policy)
        allowed = configured_bank_account_ids(policy) | set(
            cash_posting["processor_income_account_ids"].values()
        )
        set_off_account = cash_posting["financial_accounts"].get("set_off")
        if set_off_account:
            allowed.add(set_off_account)
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

    routed_warehouse_id, routing_errors = recomputed_sales_routing(payload, policy)
    errors.extend(routing_errors)
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
            vat_pin = line_values.get("vat_type_id")
            vat_field_name = f"mappings[{family}][{line_key}].vat_type_id"
            if isinstance(vat_pin, list):
                # A dated pin declares the rate in force on the document date, so a
                # statutory rate change cannot leave a superseded type on a new document.
                try:
                    document_date = parse_profile_date(payload.get("document_date"), field_name="payload.document_date")
                    expected_vat = resolve_dated_mapping_bands(
                        vat_pin, value_key="vat_type_id", field_name=vat_field_name, event_date=document_date
                    )
                except PostingPolicyError as exc:
                    errors.append(str(exc))
                    expected_vat = str(line.get("suggested_vat_type_id") or "")
            else:
                expected_vat = normalize_id(vat_pin, field_name=vat_field_name)
            if str(line.get("posting_policy_line_key") or "") != line_key:
                errors.append(f"Line is not bound to posting-policy key {line_key!r}.")
            if str(line.get("suggested_expense_account_id") or "") != expected_account:
                errors.append(f"Line expense account does not match policy family {family!r}.")
        if str(line.get("suggested_vat_type_id") or "") != expected_vat:
            errors.append(f"Line VAT type does not match policy family {family!r}.")
        expected_warehouse = family_values.get("warehouse_id") if role == "sales" else line_values.get("warehouse_id")
        if role == "sales" and routed_warehouse_id is not None:
            expected_warehouse = routed_warehouse_id
        actual_warehouse = line.get("warehouse_id_hint")
        if str(actual_warehouse or "") != str(expected_warehouse or ""):
            errors.append(f"Line warehouse does not match policy family {family!r}.")
        declared_event_types = line.get("contributor_event_types")
        declared_non_inventory = bool(declared_event_types) and set(declared_event_types) <= non_inventory_event_types(policy)
        expected_article = (
            # A reviewed cash reversal or processor fee moves no stock, so its line
            # legitimately carries no article and must not carry one.
            None if declared_non_inventory or (role == "sales" and line_role.endswith("_shipping"))
            else family_values.get("article_id") if role == "sales"
            else line_values.get("article_id")
        )
        actual_article = line.get("article_id_hint")
        if str(actual_article or "") != str(expected_article or ""):
            errors.append(f"Line article does not match policy family {family!r}.")
    return errors
