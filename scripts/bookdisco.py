#!/usr/bin/env python3
from __future__ import annotations  # noqa: EXE001, I001

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from examine_simplbooks_year import build_year_overview, unwrap_single_key
from simplbooks_api import (
    SimplbooksClient,
    SimplbooksError,
    load_token,
    parse_metadata_file,
    resolve_company_id,
    resolve_company_name,
    resolve_company_slug,
)


LIST_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "financial_accounts": ("financial_accounts/list",),
    "income_accounts": ("income_accounts/list",),
    "vat_types": ("vat_types/list",),
    "warehouses": ("warehouses/list",),
    "items": ("articles/list", "items/list"),
    "contacts": ("clients/list", "contacts/list"),
}

DIRECT_LIST_ENDPOINTS = {"vat_types/list"}


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def first_non_empty(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def company_metadata(
    *,
    metadata_file: str | None = None,
    company_dir: str | None = None,
) -> dict[str, str]:
    if metadata_file:
        return parse_metadata_file(metadata_file)
    if company_dir:
        path = Path(company_dir) / "METADATA.md"
        if path.exists():
            return parse_metadata_file(path)
    return {}


def output_paths(company_dir: Path, years: list[int]) -> dict[str, Any]:
    artifacts_dir = company_dir / "artifacts"
    discovery_dir = artifacts_dir / "discovery"
    return {
        "artifacts_dir": artifacts_dir,
        "discovery_dir": discovery_dir,
        "year_overviews": {
            year: discovery_dir / f"{year}-overview.json" for year in years
        },
        "year_findings": {
            year: discovery_dir / f"{year}-findings.md" for year in years
        },
        "policy_memo": artifacts_dir / "policy_memo.md",
        "historical_patterns": artifacts_dir / "historical_patterns.md",
        "entity_map": artifacts_dir / "entity_map.json",
        "company_profile": artifacts_dir / "company_profile.json",
    }


def ensure_output_dirs(paths: dict[str, Any]) -> None:
    paths["artifacts_dir"].mkdir(parents=True, exist_ok=True)
    paths["discovery_dir"].mkdir(parents=True, exist_ok=True)


def load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_list_records(client: SimplbooksClient, path: str) -> list[dict[str, Any]]:
    if path in DIRECT_LIST_ENDPOINTS:
        response = client.request(path)
        if response.get("_http_status") != 200 or response.get("status") not in (None, 200):
            raise SimplbooksError(f"List request failed for {path}: {json.dumps(response)}")
        data = response.get("data") or []
    else:
        data = client.paginate(path)

    if not isinstance(data, list):
        raise SimplbooksError(f"List request for {path} did not return a list")
    return [unwrap_single_key(item)[1] for item in data if isinstance(item, dict)]


def try_fetch_records(client: SimplbooksClient, endpoints: tuple[str, ...]) -> tuple[list[dict[str, Any]], str | None]:
    last_error: SimplbooksError | None = None
    for endpoint in endpoints:
        try:
            return fetch_list_records(client, endpoint), endpoint
        except SimplbooksError as exc:
            last_error = exc
    if last_error:
        return [], None
    return [], None


def normalize_entity(record: dict[str, Any]) -> dict[str, Any] | None:
    entity_id = first_non_empty(
        record,
        (
            "id",
            "account_id",
            "vat_type_id",
            "warehouse_id",
            "article_id",
            "client_id",
            "contact_id",
        ),
    )
    if entity_id in (None, ""):
        return None

    name = first_non_empty(
        record,
        (
            "name",
            "title",
            "description",
            "article_name",
            "client_name",
            "warehouse_name",
            "account_name",
            "value",
        ),
    )
    code = first_non_empty(record, ("account_number", "code", "number", "reference"))
    status = first_non_empty(record, ("status", "active", "is_active"))

    entity: dict[str, Any] = {
        "id": str(entity_id),
        "code": str(code) if code is not None else None,
        "name": str(name) if name is not None else f"ID {entity_id}",
        "status": str(status) if status is not None else None,
    }

    extra = {
        key: value
        for key, value in record.items()
        if key not in {
            "id",
            "account_id",
            "vat_type_id",
            "warehouse_id",
            "article_id",
            "client_id",
            "contact_id",
            "name",
            "title",
            "description",
            "article_name",
            "client_name",
            "warehouse_name",
            "account_name",
            "value",
            "account_number",
            "code",
            "number",
            "reference",
            "status",
            "active",
            "is_active",
        }
    }
    if extra:
        entity["extra"] = extra
    return entity


def normalize_entities(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        entity = normalize_entity(record)
        if not entity:
            continue
        entity_id = entity["id"]
        if entity_id in seen:
            continue
        seen.add(entity_id)
        entities.append(entity)
    return sorted(entities, key=lambda item: (item.get("code") or "", item["name"], item["id"]))


def aggregate_pattern_counts(overviews: list[dict[str, Any]], pattern_key: str) -> Counter[str]:
    counter: Counter[str] = Counter()
    for overview in overviews:
        for key, count in overview.get("patterns", {}).get(pattern_key, {}).items():
            counter[str(key)] += int(count)
    return counter


def top_pattern_id(overview: dict[str, Any], pattern_key: str) -> str | None:
    pattern = overview.get("patterns", {}).get(pattern_key, {})
    if not pattern:
        return None
    return next(iter(pattern.keys()))


def build_entity_index(entity_map: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for key in ("financial_accounts", "income_accounts", "vat_types", "warehouses", "items", "contacts"):
        entries = entity_map.get(key, [])
        index[key] = {entry["id"]: entry for entry in entries}
    return index


def entity_label(entity_index: dict[str, dict[str, dict[str, Any]]], category: str, entity_id: str) -> str:
    entity = entity_index.get(category, {}).get(str(entity_id))
    if not entity:
        return f"id {entity_id}"
    code = entity.get("code")
    if code:
        return f"{entity['name']} ({code}, id {entity_id})"
    return f"{entity['name']} (id {entity_id})"


def format_top_counts(
    counter: Counter[str],
    *,
    entity_index: dict[str, dict[str, dict[str, Any]]],
    category: str,
    limit: int = 3,
) -> str:
    if not counter:
        return "none observed"
    parts = [
        f"{entity_label(entity_index, category, entity_id)} x{count}"
        for entity_id, count in counter.most_common(limit)
    ]
    return ", ".join(parts)


def collect_observed_ids(overviews: list[dict[str, Any]], pattern_keys: tuple[str, ...]) -> list[str]:
    ids: set[str] = set()
    for overview in overviews:
        for pattern_key in pattern_keys:
            ids.update(str(key) for key in overview.get("patterns", {}).get(pattern_key, {}))
    return sorted(ids)


def build_entity_map_document(
    *,
    company_slug: str,
    generated_at: str,
    as_of_period: str,
    records_by_category: dict[str, list[dict[str, Any]]],
    overviews: list[dict[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "company_slug": company_slug,
        "generated_at": generated_at,
        "as_of_period": as_of_period,
        "notes": [],
    }

    for category, records in records_by_category.items():
        normalized = normalize_entities(records)
        if normalized:
            document[category] = normalized

    observed_article_ids = collect_observed_ids(
        overviews,
        ("invoice_article_ids", "purchase_article_ids"),
    )
    if observed_article_ids and not document.get("items"):
        document["notes"].append(
            "Observed article IDs in historical documents but did not resolve item names from the "
            f"current API wrapper: {', '.join(observed_article_ids)}."
        )

    observed_warehouse_ids = collect_observed_ids(overviews, ("invoice_warehouse_ids",))
    if observed_warehouse_ids and not document.get("warehouses"):
        document["notes"].append(
            "Observed warehouse IDs in invoice rows but did not resolve warehouse names from the "
            f"current API wrapper: {', '.join(observed_warehouse_ids)}."
        )

    if not document["notes"]:
        document.pop("notes")
    return document


def parse_boolish(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"yes", "true", "1", "y"}:
        return True
    if text in {"no", "false", "0", "n"}:
        return False
    return None


def profile_entity_text(entry: dict[str, Any]) -> str:
    return " ".join(str(entry.get(key) or "").strip().lower() for key in ("name", "code", "status"))


def infer_income_account_ids(entity_map: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    bank_ids: list[str] = []
    cash_ids: list[str] = []
    notes: list[str] = []

    for entry in entity_map.get("income_accounts", []):
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id:
            continue
        text = profile_entity_text(entry)
        if any(keyword in text for keyword in ("cash", "sularaha", "kassa")):
            cash_ids.append(entry_id)
            continue
        if any(keyword in text for keyword in ("bank", "konto", "account", "swedbank", "lhv", "seb", "iban")):
            bank_ids.append(entry_id)

    if not bank_ids:
        income_accounts = entity_map.get("income_accounts", [])
        if len(income_accounts) == 1:
            only_id = str(income_accounts[0].get("id") or "").strip()
            if only_id and only_id not in cash_ids:
                bank_ids.append(only_id)
                notes.append("Only one income account was available, so it was assumed to be the default bank account.")

    if len(bank_ids) > 1:
        notes.append("Multiple income accounts look bank-like; review bank_account_ids before live submission.")

    return bank_ids, cash_ids, notes


def build_company_profile_document(
    *,
    company_name: str,
    company_slug: str,
    company_id: str,
    metadata: dict[str, str],
    entity_map: dict[str, Any],
) -> dict[str, Any]:
    notes: list[str] = []
    vat_registered = parse_boolish(
        first_non_empty(metadata, ("vat registered", "vat_registered"))
    )
    if vat_registered is None:
        vat_registered = False
        notes.append("VAT registration status was not available in METADATA.md; defaulted vat_registered to false.")

    oss_registered = parse_boolish(
        first_non_empty(metadata, ("oss registered", "oss_registered"))
    )

    bank_account_ids, cash_account_ids, income_account_notes = infer_income_account_ids(entity_map)
    notes.extend(income_account_notes)

    warehouse_entries = entity_map.get("warehouses", [])
    default_warehouse_ids = [str(entry.get("id")) for entry in warehouse_entries if entry.get("id") not in (None, "")]
    if len(default_warehouse_ids) > 1:
        default_warehouse_ids = []
        notes.append("Multiple warehouses were discovered; leave default_warehouse_ids empty until one default is confirmed.")

    return {
        "schema_version": "1.0",
        "company_name": company_name,
        "company_slug": company_slug,
        "simplbooks_company_id": company_id,
        "description": str(metadata.get("description") or ""),
        "base_currency": "EUR",
        "vat_registered": vat_registered,
        "oss_registered": oss_registered,
        "default_warehouse_ids": default_warehouse_ids,
        "bank_account_ids": bank_account_ids,
        "cash_account_ids": cash_account_ids,
        "policy_refs": [],
        "notes": notes,
    }


def build_stable_patterns(
    overviews: list[dict[str, Any]],
    entity_index: dict[str, dict[str, dict[str, Any]]],
) -> list[str]:
    stable: list[str] = []

    if any(len(overview.get("patterns", {}).get("invoice_income_account_ids", {})) > 1 for overview in overviews):
        stable.append(
            "Historical invoice rows use multiple income accounts, so revenue should not be forced into a single bucket."
        )

    if all(overview.get("patterns", {}).get("invoice_warehouse_ids", {}) for overview in overviews):
        stable.append(
            "Warehouse IDs appear on invoice rows across all lookback years, so warehouse identity should be preserved."
        )

    if any(overview.get("patterns", {}).get("purchase_expense_account_ids", {}) for overview in overviews):
        stable.append(
            "Purchase rows carry explicit expense-account choices, so fees and fulfillment costs should be mapped from observed buckets."
        )

    invoice_common = set(collect_observed_ids(overviews, ("invoice_income_account_ids",)))
    if invoice_common:
        stable.append(
            "Observed invoice income account IDs across the lookback period: "
            + ", ".join(entity_label(entity_index, "financial_accounts", entity_id) for entity_id in sorted(invoice_common))
            + "."
        )

    vat_common = aggregate_pattern_counts(overviews, "invoice_vat_type_ids")
    if vat_common:
        stable.append(
            "Invoice VAT types seen historically: "
            + format_top_counts(vat_common, entity_index=entity_index, category="vat_types")
            + "."
        )

    return stable


def build_suspicious_patterns(
    overviews: list[dict[str, Any]],
    entity_index: dict[str, dict[str, dict[str, Any]]],
) -> list[str]:
    suspicious: list[str] = []

    invoice_top_ids = {top_pattern_id(overview, "invoice_income_account_ids") for overview in overviews}
    invoice_top_ids.discard(None)
    if len(invoice_top_ids) > 1:
        suspicious.append(
            "The dominant invoice income account changes between lookback years: "
            + ", ".join(entity_label(entity_index, "financial_accounts", entity_id) for entity_id in sorted(invoice_top_ids))
            + "."
        )

    purchase_top_ids = {top_pattern_id(overview, "purchase_expense_account_ids") for overview in overviews}
    purchase_top_ids.discard(None)
    if len(purchase_top_ids) > 1:
        suspicious.append(
            "The dominant purchase expense account changes between lookback years: "
            + ", ".join(entity_label(entity_index, "financial_accounts", entity_id) for entity_id in sorted(purchase_top_ids))
            + "."
        )

    if any("unknown" in overview.get("monthly", {}).get("invoices", {}) for overview in overviews):
        suspicious.append("At least one invoice summary bucket uses an unknown date key, which suggests date-field cleanup is needed.")
    if any("unknown" in overview.get("monthly", {}).get("purchases", {}) for overview in overviews):
        suspicious.append("At least one purchase summary bucket uses an unknown date key, which suggests date-field cleanup is needed.")

    if not suspicious:
        suspicious.append("No cross-year contradictions were detected from the current summary heuristics. Manual review is still required.")
    return suspicious


def build_open_questions(overviews: list[dict[str, Any]], entity_map: dict[str, Any]) -> list[str]:
    questions: list[str] = []
    observed_article_ids = collect_observed_ids(overviews, ("invoice_article_ids", "purchase_article_ids"))
    if observed_article_ids and not entity_map.get("items"):
        questions.append("Verify which API endpoint should be used to resolve article/item names for the observed article IDs.")
    if any(overview.get("counts", {}).get("payments", 0) == 0 for overview in overviews):
        questions.append("Check whether zero-payment years reflect actual activity or list-endpoint/filter limitations.")
    return questions


def build_policy_memo_markdown(
    *,
    company_name: str,
    years: list[int],
    overviews: list[dict[str, Any]],
    entity_map: dict[str, Any],
) -> str:
    entity_index = build_entity_index(entity_map)
    stable = build_stable_patterns(overviews, entity_index)
    suspicious = build_suspicious_patterns(overviews, entity_index)
    questions = build_open_questions(overviews, entity_map)

    revenue_lines: list[str] = []
    invoice_income_accounts = aggregate_pattern_counts(overviews, "invoice_income_account_ids")
    if invoice_income_accounts:
        revenue_lines.append(
            "Top historical invoice income accounts: "
            + format_top_counts(invoice_income_accounts, entity_index=entity_index, category="financial_accounts")
            + "."
        )
    invoice_vat_types = aggregate_pattern_counts(overviews, "invoice_vat_type_ids")
    if invoice_vat_types:
        revenue_lines.append(
            "Top historical invoice VAT types: "
            + format_top_counts(invoice_vat_types, entity_index=entity_index, category="vat_types")
            + "."
        )
    if not revenue_lines:
        revenue_lines.append("No invoice row pattern data was captured yet.")

    expense_lines: list[str] = []
    purchase_expense_accounts = aggregate_pattern_counts(overviews, "purchase_expense_account_ids")
    if purchase_expense_accounts:
        expense_lines.append(
            "Top historical purchase expense accounts: "
            + format_top_counts(purchase_expense_accounts, entity_index=entity_index, category="financial_accounts")
            + "."
        )
    purchase_vat_types = aggregate_pattern_counts(overviews, "purchase_vat_type_ids")
    if purchase_vat_types:
        expense_lines.append(
            "Top historical purchase VAT types: "
            + format_top_counts(purchase_vat_types, entity_index=entity_index, category="vat_types")
            + "."
        )
    if not expense_lines:
        expense_lines.append("No purchase row pattern data was captured yet.")

    inventory_lines: list[str] = []
    warehouse_counter = aggregate_pattern_counts(overviews, "invoice_warehouse_ids")
    if warehouse_counter:
        inventory_lines.append(
            "Warehouse usage seen in invoice rows: "
            + format_top_counts(warehouse_counter, entity_index=entity_index, category="warehouses")
            + "."
        )
    article_ids = collect_observed_ids(overviews, ("invoice_article_ids", "purchase_article_ids"))
    if article_ids:
        inventory_lines.append(
            "Historical documents reference article IDs, so item-level patterns are present and should be preserved in later skills."
        )
    if not inventory_lines:
        inventory_lines.append("No warehouse or article evidence was observed in the current summaries.")

    lines = [
        "# Policy Memo",
        "",
        "## Scope",
        "",
        f"- Company: {company_name}",
        f"- Covered years: {', '.join(str(year) for year in years)}",
        "- Prepared from: Simplbooks read-only historical discovery",
        "",
        "## Stable Patterns",
        "",
    ]
    if stable:
        lines.extend(f"- {line}" for line in stable)
    else:
        lines.append("- No stable multi-year patterns were inferred yet.")
    lines.extend(
        [
            "",
            "## Suspicious Or Inconsistent Patterns",
            "",
        ]
    )
    lines.extend(f"- {line}" for line in suspicious)
    lines.extend(
        [
            "",
            "## Date Field Rules",
            "",
            "- Invoices: use `created` for historical period discovery.",
            "- Purchases: use `created` for historical period discovery.",
            "- Receipts: use `income_date` for historical period discovery.",
            "- Payments: use `payment_date` for historical period discovery.",
            "",
            "## Revenue And VAT Handling",
            "",
        ]
    )
    lines.extend(f"- {line}" for line in revenue_lines)
    lines.extend(
        [
            "",
            "## Expense And Fee Handling",
            "",
        ]
    )
    lines.extend(f"- {line}" for line in expense_lines)
    lines.extend(
        [
            "",
            "## Inventory And Warehouse Handling",
            "",
        ]
    )
    lines.extend(f"- {line}" for line in inventory_lines)
    lines.extend(
        [
            "",
            "## Open Questions",
            "",
        ]
    )
    if questions:
        lines.extend(f"- {line}" for line in questions)
    else:
        lines.append("- No unresolved questions were generated by the current heuristics.")
    lines.append("")
    return "\n".join(lines)


def build_historical_patterns_markdown(
    *,
    overviews: list[dict[str, Any]],
    entity_map: dict[str, Any],
) -> str:
    entity_index = build_entity_index(entity_map)
    invoice_income_accounts = aggregate_pattern_counts(overviews, "invoice_income_account_ids")
    invoice_vat_types = aggregate_pattern_counts(overviews, "invoice_vat_type_ids")
    purchase_expense_accounts = aggregate_pattern_counts(overviews, "purchase_expense_account_ids")
    purchase_vat_types = aggregate_pattern_counts(overviews, "purchase_vat_type_ids")
    warehouse_counter = aggregate_pattern_counts(overviews, "invoice_warehouse_ids")
    article_ids = collect_observed_ids(overviews, ("invoice_article_ids", "purchase_article_ids"))

    lines = [
        "# Historical Patterns",
        "",
        "## Revenue",
        "",
        "- Top invoice income accounts: "
        + format_top_counts(invoice_income_accounts, entity_index=entity_index, category="financial_accounts")
        + ".",
        "- Top invoice VAT types: "
        + format_top_counts(invoice_vat_types, entity_index=entity_index, category="vat_types")
        + ".",
        "",
        "## Shipping",
        "",
        "- Multiple invoice income accounts may reflect separate shipping treatment; verify row descriptions in spot checks before builder rules are finalized.",
        "",
        "## Fees And Processor Costs",
        "",
        "- Top purchase expense accounts: "
        + format_top_counts(purchase_expense_accounts, entity_index=entity_index, category="financial_accounts")
        + ".",
        "- Top purchase VAT types: "
        + format_top_counts(purchase_vat_types, entity_index=entity_index, category="vat_types")
        + ".",
        "",
        "## Fulfillment And Supplier Costs",
        "",
        "- Purchase expense-account diversity indicates costs should stay bucketed by historical pattern rather than merged into one generic expense line.",
        "",
        "## Inventory And Warehouses",
        "",
        "- Warehouse usage in invoice rows: "
        + format_top_counts(warehouse_counter, entity_index=entity_index, category="warehouses")
        + ".",
        "- Observed article IDs: "
        + (", ".join(article_ids) if article_ids else "none observed")
        + ".",
        "",
        "## Refunds And Adjustments",
        "",
        "- Refund treatment still needs document-shape inspection; this summary only confirms that later reconciliation should treat refunds explicitly.",
        "",
        "## Date Semantics",
        "",
        "- Invoices and purchases summarize by `created`.",
        "- Receipts summarize by `income_date`.",
        "- Payments summarize by `payment_date`.",
        "- `created_time` should stay out of accounting-period logic.",
        "",
        "## Confidence Notes",
        "",
        "- This file is derived from year-overview summaries and current entity-list lookups.",
        "- Historical behavior is evidence, not a rule set to copy blindly.",
        "",
    ]
    return "\n".join(lines)


def build_year_findings_markdown(
    *,
    company_name: str,
    year: int,
    overview: dict[str, Any],
) -> str:
    confirmed: list[str] = []
    suspicious: list[str] = []
    implications: list[str] = []

    counts = overview.get("counts", {})
    patterns = overview.get("patterns", {})

    confirmed.append(
        f"Captured {counts.get('invoices', 0)} invoices / {counts.get('invoice_rows', 0)} invoice rows and "
        f"{counts.get('purchases', 0)} purchases / {counts.get('purchase_rows', 0)} purchase rows."
    )

    if patterns.get("invoice_income_account_ids"):
        confirmed.append("Invoice row account usage is available for policy discovery.")
        implications.append("`bookbuilder` should choose posting buckets from discovered invoice income accounts.")
    if patterns.get("invoice_warehouse_ids"):
        confirmed.append("Warehouse IDs appear on invoice rows.")
        implications.append("`bookprep` and `bookrecon` should preserve warehouse identity when source data contains it.")
    if patterns.get("purchase_expense_account_ids"):
        confirmed.append("Purchase expense-account usage is available for policy discovery.")
        implications.append("`bookbuilder` should not collapse fees and fulfillment costs into one generic expense account.")
    if patterns.get("purchase_article_ids"):
        confirmed.append("Purchase rows include article IDs.")
        implications.append("`bookaudit` should keep item/inventory checks available where inventory exists.")

    if len(patterns.get("invoice_income_account_ids", {})) > 1:
        suspicious.append("Multiple invoice income accounts were used in the same year.")
    if len(patterns.get("purchase_expense_account_ids", {})) > 1:
        suspicious.append("Multiple purchase expense accounts were used in the same year.")
    if "unknown" in overview.get("monthly", {}).get("receipts", {}):
        suspicious.append("Some receipts landed in an unknown month bucket.")
    if "unknown" in overview.get("monthly", {}).get("payments", {}):
        suspicious.append("Some payments landed in an unknown month bucket.")
    if not suspicious:
        suspicious.append("No year-specific contradictions were detected by the current summary heuristics.")

    lines = [
        "# Discovery Findings",
        "",
        "## Scope",
        "",
        f"- Company: {company_name}",
        f"- Year: {year}",
        f"- Discovery overview: companies/<company>/artifacts/discovery/{year}-overview.json",
        "",
        "## Confirmed Patterns",
        "",
    ]
    lines.extend(f"- {line}" for line in confirmed)
    lines.extend(
        [
            "",
            "## Suspicious Patterns",
            "",
        ]
    )
    lines.extend(f"- {line}" for line in suspicious)
    lines.extend(
        [
            "",
            "## Data Quality Concerns",
            "",
            "- Receipts and payments are filtered client-side by year because published endpoint filtering is incomplete.",
            "",
            "## Implications For Later Skills",
            "",
        ]
    )
    if implications:
        lines.extend(f"- {line}" for line in implications)
    else:
        lines.append("- No downstream implications were generated.")
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only historical Simplbooks discovery")
    parser.add_argument("--company-id", help="Simplbooks company ID")
    parser.add_argument("--company-dir", required=True, help="Company folder, e.g. companies/example")
    parser.add_argument("--metadata-file", help="Path to company METADATA.md")
    parser.add_argument("--token-file", default=".apikey")
    parser.add_argument("--request-log", help="Optional JSONL path for low-level Simplbooks request/response logging")
    parser.add_argument("--years", type=int, nargs="+", required=True, help="Years to inspect, e.g. 2022 2023")
    parser.add_argument(
        "--reuse-existing-overviews",
        action="store_true",
        help="Load an existing discovery overview file when present instead of refetching that year.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    company_dir = Path(args.company_dir)
    years = sorted(set(args.years))
    paths = output_paths(company_dir, years)
    ensure_output_dirs(paths)

    metadata = company_metadata(metadata_file=args.metadata_file, company_dir=args.company_dir)
    company_id = resolve_company_id(
        args.company_id,
        metadata_file=args.metadata_file,
        company_dir=args.company_dir,
    )
    company_name = resolve_company_name(
        metadata_file=args.metadata_file,
        company_dir=args.company_dir,
    ) or metadata.get("company name") or company_dir.name
    company_slug = resolve_company_slug(
        metadata_file=args.metadata_file,
        company_dir=args.company_dir,
    ) or metadata.get("company slug") or company_dir.name

    client = SimplbooksClient(
        company_id=company_id,
        token=load_token(args.token_file),
        request_log_path=args.request_log,
    )

    overviews: list[dict[str, Any]] = []
    for year in years:
        overview_path = paths["year_overviews"][year]
        if args.reuse_existing_overviews and overview_path.exists():
            overview = load_json_file(overview_path)
        else:
            overview = build_year_overview(client, year=year)
            overview["company_name"] = company_name
            write_json(overview_path, overview)
        overviews.append(overview)

        findings_path = paths["year_findings"][year]
        write_text(
            findings_path,
            build_year_findings_markdown(
                company_name=company_name,
                year=year,
                overview=overview,
            ),
        )

    records_by_category: dict[str, list[dict[str, Any]]] = {}
    entity_endpoint_notes: list[str] = []
    for category, endpoints in LIST_ENDPOINTS.items():
        records, endpoint = try_fetch_records(client, endpoints)
        records_by_category[category] = records
        if endpoint:
            entity_endpoint_notes.append(f"{category} resolved from {endpoint}.")
        else:
            entity_endpoint_notes.append(f"{category} could not be resolved from candidate endpoints: {', '.join(endpoints)}.")

    generated_at = utc_now_iso()
    as_of_period = f"{max(years)}-12"
    entity_map = build_entity_map_document(
        company_slug=company_slug,
        generated_at=generated_at,
        as_of_period=as_of_period,
        records_by_category=records_by_category,
        overviews=overviews,
    )
    notes = entity_map.get("notes", [])
    entity_map["notes"] = [*notes, *entity_endpoint_notes]
    write_json(paths["entity_map"], entity_map)

    company_profile = build_company_profile_document(
        company_name=company_name,
        company_slug=company_slug,
        company_id=company_id,
        metadata=metadata,
        entity_map=entity_map,
    )
    write_json(paths["company_profile"], company_profile)

    write_text(
        paths["policy_memo"],
        build_policy_memo_markdown(
            company_name=company_name,
            years=years,
            overviews=overviews,
            entity_map=entity_map,
        ),
    )
    write_text(
        paths["historical_patterns"],
        build_historical_patterns_markdown(
            overviews=overviews,
            entity_map=entity_map,
        ),
    )

    result = {
        "company_id": company_id,
        "company_name": company_name,
        "company_slug": company_slug,
        "years": years,
        "outputs": {
            "year_overviews": {str(year): str(path) for year, path in paths["year_overviews"].items()},
            "year_findings": {str(year): str(path) for year, path in paths["year_findings"].items()},
            "entity_map": str(paths["entity_map"]),
            "company_profile": str(paths["company_profile"]),
            "policy_memo": str(paths["policy_memo"]),
            "historical_patterns": str(paths["historical_patterns"]),
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
