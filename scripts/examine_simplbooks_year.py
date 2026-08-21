#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from document_identity import document_identity

from simplbooks_api import (
    SimplbooksClient,
    SimplbooksError,
    load_token,
    resolve_company_id,
    resolve_company_name,
)


def unwrap_single_key(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if len(item) != 1:
        return "raw", item
    key = next(iter(item.keys()))
    value = item[key]
    if isinstance(value, dict):
        return key, value
    return key, {"value": value}


def date_in_year(value: str, year: int) -> bool:
    return isinstance(value, str) and value.startswith(f"{year}-")


def month_key(value: str) -> str:
    return value[:7] if isinstance(value, str) and len(value) >= 7 else "unknown"


def summarise_documents(records: list[dict[str, Any]], *, date_field: str, sum_field: str) -> dict[str, Any]:
    monthly: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "sum": 0.0, "vat": 0.0, "total_sum": 0.0})
    for record in records:
        bucket = monthly[month_key(record.get(date_field, ""))]
        bucket["count"] += 1
        bucket["sum"] += float(record.get(sum_field, 0) or 0)
        bucket["vat"] += float(record.get("vat", 0) or 0)
        bucket["total_sum"] += float(record.get("total_sum", record.get(sum_field, 0)) or 0)
    return dict(sorted(monthly.items()))


def scan_year_from_unfiltered_pages(
    client: SimplbooksClient,
    path: str,
    *,
    wrapper_key: str,
    date_field: str,
    year: int,
    per_page: int = 1000,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in client.paginate(path, per_page=per_page):
        key, value = unwrap_single_key(item)
        if key != wrapper_key:
            continue
        if date_in_year(value.get(date_field, ""), year):
            records.append(value)
    return records


def count_row_patterns(rows: list[dict[str, Any]], field_name: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        value = row.get(field_name)
        if value not in (None, "", 0, "0"):
            counter[str(value)] += 1
    return dict(counter.most_common())


def build_document_index(
    *,
    invoices: list[dict[str, Any]],
    purchases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    index = [document_identity(record, document_type="invoice").to_dict() for record in invoices]
    index.extend(document_identity(record, document_type="purchase").to_dict() for record in purchases)
    return index


def build_year_overview(client: SimplbooksClient, *, year: int) -> dict[str, Any]:
    start = f"{year}-01-01"
    end = f"{year}-12-31"

    financial_accounts = [
        unwrap_single_key(item)[1]
        for item in client.paginate("financial_accounts/list")
    ]
    income_accounts = [
        unwrap_single_key(item)[1]
        for item in client.paginate("income_accounts/list")
    ]
    vat_types = [
        unwrap_single_key(item)[1]
        for item in client.request("vat_types/list").get("data", [])
    ]
    warehouses = [
        unwrap_single_key(item)[1]
        for item in client.paginate("warehouses/list")
    ]

    invoice_list_raw = client.paginate(
        "invoices/list",
        payload={"created_from": start, "created_until": end},
    )
    invoices = [unwrap_single_key(item)[1] for item in invoice_list_raw]

    purchase_list_raw = client.paginate(
        "purchases/list",
        payload={"created_from": start, "created_until": end},
    )
    purchases = [unwrap_single_key(item)[1] for item in purchase_list_raw]

    receipts = scan_year_from_unfiltered_pages(
        client,
        "incomings/list",
        wrapper_key="Incoming",
        date_field="income_date",
        year=year,
    )
    payments = scan_year_from_unfiltered_pages(
        client,
        "payments/list",
        wrapper_key="Payment",
        date_field="payment_date",
        year=year,
    )

    invoice_rows: list[dict[str, Any]] = []
    purchase_rows: list[dict[str, Any]] = []

    for invoice in invoices:
        detail = client.request(f"invoices/get/{invoice['id']}")
        invoice_rows.extend(detail.get("data", {}).get("Task", []))

    for purchase in purchases:
        detail = client.request(f"purchases/get/{purchase['id']}")
        purchase_rows.extend(detail.get("data", {}).get("PurchaseRow", []))

    return {
        "year": year,
        "company_id": client.company_id,
        "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "technical_findings": [
            "Invoices and purchases support year-bounded list queries by created date.",
            "Receipts and payments do not show year range filters in the published spec, so this overview scans paginated lists and filters by year client-side.",
            "Invoice row details are available via invoices/get/{id}.",
            "Purchase row details are available via purchases/get/{id}.",
        ],
        "counts": {
            "financial_accounts": len(financial_accounts),
            "income_accounts": len(income_accounts),
            "vat_types": len(vat_types),
            "warehouses": len(warehouses),
            "invoices": len(invoices),
            "invoice_rows": len(invoice_rows),
            "purchases": len(purchases),
            "purchase_rows": len(purchase_rows),
            "receipts": len(receipts),
            "payments": len(payments),
        },
        "monthly": {
            "invoices": summarise_documents(invoices, date_field="created", sum_field="sum"),
            "purchases": summarise_documents(purchases, date_field="created", sum_field="sum"),
            "receipts": summarise_documents(receipts, date_field="income_date", sum_field="income_sum"),
            "payments": summarise_documents(payments, date_field="payment_date", sum_field="payment_sum"),
        },
        "patterns": {
            "invoice_income_account_ids": count_row_patterns(invoice_rows, "income_account_id"),
            "invoice_vat_type_ids": count_row_patterns(invoice_rows, "vat_type_id"),
            "invoice_article_ids": count_row_patterns(invoice_rows, "article_id"),
            "invoice_warehouse_ids": count_row_patterns(invoice_rows, "warehouse_id"),
            "purchase_expense_account_ids": count_row_patterns(purchase_rows, "expense_account_id"),
            "purchase_vat_type_ids": count_row_patterns(purchase_rows, "vat_type_id"),
            "purchase_article_ids": count_row_patterns(purchase_rows, "article_id"),
        },
        "document_index": build_document_index(invoices=invoices, purchases=purchases),
        "samples": {
            "financial_accounts": financial_accounts[:10],
            "income_accounts": income_accounts[:10],
            "vat_types": vat_types[:10],
            "warehouses": warehouses[:10],
            "invoices": invoices[:10],
            "purchases": purchases[:10],
            "receipts": receipts[:10],
            "payments": payments[:10],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Simplbooks year overview")
    parser.add_argument("--company-id", help="Simplbooks company ID")
    parser.add_argument("--company-dir", help="Company folder, e.g. companies/example")
    parser.add_argument("--metadata-file", help="Path to company METADATA.md")
    parser.add_argument("--token-file", default=".apikey")
    parser.add_argument("--request-log", help="Optional JSONL path for low-level Simplbooks request/response logging")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output", help="Optional path to write the JSON overview")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    company_id = resolve_company_id(
        args.company_id,
        metadata_file=args.metadata_file,
        company_dir=args.company_dir,
    )
    company_name = resolve_company_name(
        metadata_file=args.metadata_file,
        company_dir=args.company_dir,
    )
    client = SimplbooksClient(
        company_id=company_id,
        token=load_token(args.token_file),
        request_log_path=args.request_log,
    )
    overview = build_year_overview(client, year=args.year)
    if company_name:
        overview["company_name"] = company_name

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(overview, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps(overview, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
