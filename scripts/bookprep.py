#!/usr/bin/env python3
from __future__ import annotations  # noqa: EXE001, I001

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from calendar import month_name
import unicodedata
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable  # noqa: UP035
from xml.etree import ElementTree

from simplbooks_api import (
    SimplbooksError,
    resolve_company_name,
    resolve_company_slug,
)
import woo_tax

try:
    from pypdf import PdfReader  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency
    PdfReader = None


SOURCE_TYPE_PRIORITY = {
    "csv": 100,
    "xml": 90,
    "xlsx": 85,
    "json": 80,
    "manual": 70,
    "pdf": 50,
    "other": 40,
}

ROW_EVENT_HEADERS = {
    "woo_sales_csv": {"date", "orders", "gross sales", "returns", "coupons", "net sales", "taxes", "shipping", "total sales"},
    "woo_monthly_sales_csv": {
        "date",
        "number of items sold",
        "number of orders",
        "average net sales amount",
        "coupon amount",
        "shipping amount",
        "gross sales amount",
        "net sales amount",
        "refund amount",
    },
    "woo_tax_summary_csv": {"tax code", "rate", "total tax", "order tax", "shipping tax", "orders"},
    "woo_order_summary_csv": {
        "date",
        "order",
        "status",
        "customer",
        "customer type",
        "product s",
        "items sold",
        "coupon s",
        "net sales",
        "attribution",
    },
    "paypal_csv": {"date", "time", "timezone", "name", "type", "status", "currency", "gross", "fee", "net", "transaction id"},
    "stripe_balance_csv": {"id", "type", "source", "amount", "fee", "net", "currency", "created (utc)", "available on (utc)"},
    "stripe_payouts_csv": {
        "payout id",
        "effective at utc",
        "currency",
        "gross",
        "fee",
        "net",
        "reporting category",
        "balance transaction id",
        "payout status",
    },
    "quartermaster_orders_csv": {
        "referenceid",
        "qmlorderid",
        "email",
        "name",
        "status",
        "ordertype",
        "carrier",
        "shippingtype",
        "trackingnumber",
        "datesubmitted",
        "dateshipped",
    },
    "printful_orders_csv": {
        "date",
        "order",
        "printful id",
        "shipped from",
        "shipped to",
        "payment instrument",
        "status",
        "products",
        "discount",
        "shipping",
        "digitization",
        "branding",
        "fulfillment fees",
        "vat",
        "total",
    },
    "printful_wallet_csv": {"date", "action", "payment instrument", "amount"},
    "printful_other_csv": {"date", "category", "payment instrument", "status", "amount", "discount", "tax", "vat", "total"},
    "printful_services_csv": {"date", "action", "payment instrument", "status", "total"},
    "bank_csv": {"kliendi konto", "dokumendi number", "kuupaev", "saaja/maksja nimi", "deebet/kreedit (d/c)", "summa", "selgitus", "valuuta"},
}

PDF_LIGATURES = {
    "\ufb01": "fi",
    "\ufb02": "fl",
}


@dataclass
class SourceDescriptor:
    path: Path
    rel_path: str
    source_id: str
    source_type: str
    source_system: str
    covered_from: date
    covered_until: date
    canonical_group: str
    parser_name: str
    canonical: bool = False
    parser_notes: list[str] = field(default_factory=list)
    preferred_over: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def priority(self) -> int:
        return SOURCE_TYPE_PRIORITY.get(self.source_type, SOURCE_TYPE_PRIORITY["other"])

    def overlaps(self, period_start: date, period_end: date) -> bool:
        return not (self.covered_until < period_start or self.covered_from > period_end)

    def manifest_entry(self) -> dict[str, Any]:
        notes = " ".join(note for note in self.parser_notes if note).strip()
        entry = {
            "source_id": self.source_id,
            "path": self.rel_path,
            "sha256": sha256_file(self.path),
            "source_type": self.source_type,
            "source_system": self.source_system,
            "covered_from": self.covered_from.isoformat(),
            "covered_until": self.covered_until.isoformat(),
            "canonical": self.canonical,
            "parser_name": self.parser_name,
            "parser_notes": notes or None,
            "preferred_over": self.preferred_over,
        }
        return entry


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


def normalize_ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii")


def slugify(value: str) -> str:
    collapsed = re.sub(r"[^a-z0-9]+", "-", normalize_ascii(value).lower())
    return collapsed.strip("-") or "source"


def source_type_from_path(path: Path) -> str:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if not suffixes:
        return "other"
    final = suffixes[-1]
    mapping = {
        ".csv": "csv",
        ".xml": "xml",
        ".xlsx": "xlsx",
        ".xls": "xlsx",
        ".pdf": "pdf",
        ".json": "json",
    }
    return mapping.get(final, "other")


def canonical_group_for_path(path: Path) -> str:
    current = Path(path.name)
    while current.suffix.lower() in {".gsheet", ".csv", ".xml", ".xls", ".xlsx", ".pdf", ".json"}:
        current = Path(current.stem)
    return slugify(current.name)


def infer_source_system(path: Path, source_type: str, header_names: set[str] | None = None) -> str:
    normalized = normalize_ascii(str(path)).lower()
    headers = header_names or set()
    if (
        headers >= ROW_EVENT_HEADERS["woo_tax_summary_csv"]
        or headers >= ROW_EVENT_HEADERS["woo_order_summary_csv"]
    ):
        return "woo"
    if "paypal" in normalized or headers >= ROW_EVENT_HEADERS["paypal_csv"]:
        return "paypal"
    if "stripe" in normalized or headers >= ROW_EVENT_HEADERS["stripe_balance_csv"]:
        return "stripe"
    if "quartermaster" in normalized or "qm_sales" in normalized or headers >= ROW_EVENT_HEADERS["quartermaster_orders_csv"]:
        return "quartermaster"
    if (
        "printful" in normalized
        or "billing-report" in normalized
        or "vat_report_" in normalized
        or "lv90011218978" in normalized
        or headers >= ROW_EVENT_HEADERS["printful_orders_csv"]
        or headers >= ROW_EVENT_HEADERS["printful_wallet_csv"]
        or headers >= ROW_EVENT_HEADERS["printful_other_csv"]
        or headers >= ROW_EVENT_HEADERS["printful_services_csv"]
    ):
        return "printful"
    if "simplbooks" in normalized:
        return "simplbooks"
    if (
        "muugiraport" in normalized
        or "sales report" in normalized
        or headers >= ROW_EVENT_HEADERS["woo_tax_summary_csv"]
        or headers >= ROW_EVENT_HEADERS["woo_sales_csv"]
        or headers >= ROW_EVENT_HEADERS["woo_monthly_sales_csv"]
    ):
        return "woo"
    if "kontovv" in normalized or source_type == "xml" or headers >= ROW_EVENT_HEADERS["bank_csv"]:
        return "bank"
    if source_type == "pdf":
        return "document"
    return "manual"


def infer_pdf_source_system(text: str, fallback: str) -> str:
    normalized = normalize_ascii(text).lower()
    if "stripe payments europe" in normalized or "stripe processing fees" in normalized:
        return "stripe"
    if "quartermaster direct" in normalized or "qml picking fee" in normalized:
        return "quartermaster"
    if "quartermaster logistics llc" in normalized and (
        "invoice total" in normalized or "invoice due date" in normalized or "sales report" in normalized
    ):
        return "quartermaster"
    if "printful inc" in normalized or "vat report" in normalized:
        return "printful"
    if "simplbooks" in normalized:
        return "simplbooks"
    return fallback


def is_ignored_work_file(path: Path) -> bool:
    return path.suffix.lower() in {".gsheet", ".md"}


def parse_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")  # noqa: FURB157
    text = str(value).strip().replace("\ufeff", "").replace("\u00a0", "").replace(" ", "")
    if not text:
        return Decimal("0")  # noqa: FURB157
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise SimplbooksError(f"Could not parse decimal value: {value!r}") from exc


def parse_date_value(value: str) -> date:
    text = re.sub(r"\s+", " ", str(value).strip().replace("\ufeff", ""))
    for fmt in (
        "%Y-%m-%d",
        "%Y.%m.%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise SimplbooksError(f"Unsupported date format: {value!r}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_filename_dates(path: Path) -> tuple[date, date] | None:
    name = normalize_ascii(path.name)

    quartermaster_sales_match = re.search(r"qm_sales_(\d{1,2})(?!\d)", normalize_ascii(path.stem).lower())
    if quartermaster_sales_match:
        month = int(quartermaster_sales_match.group(1))
        year = None
        for parent in path.parents:
            parent_match = re.search(r"(20\d{2})", normalize_ascii(parent.name))
            if parent_match:
                year = int(parent_match.group(1))
                break
        if year is not None and 1 <= month <= 12:
            return date(year, month, 1), date(year, month, monthrange(year, month)[1])

    date_match = re.search(r"(20\d{2})[-_.](\d{2})[-_.](\d{2})", name)
    if date_match:
        found = date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
        return found, found

    month_match = re.search(r"(20\d{2})[-_]?(\d{2})(?!\d)", name)
    if month_match:
        year = int(month_match.group(1))
        month = int(month_match.group(2))
        if 1 <= month <= 12:
            return date(year, month, 1), date(year, month, monthrange(year, month)[1])

    year_match = re.search(r"(20\d{2})", name)
    if year_match:
        year = int(year_match.group(1))
        return date(year, 1, 1), date(year, 12, 31)
    return None


def normalize_pdf_text(text: str) -> str:
    normalized = text.replace("\u00a0", " ")
    for source, target in PDF_LIGATURES.items():
        normalized = normalized.replace(source, target)
    return normalized


def extract_pdf_pages(path: Path) -> list[str]:
    if PdfReader is None:
        raise SimplbooksError(
            "PDF parsing requires pypdf. Create .venv in the repo root and install pypdf there."
        )
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # pragma: no cover - depends on malformed PDFs
        raise SimplbooksError(f"Could not open PDF {path}: {exc}") from exc
    pages = [normalize_pdf_text(page.extract_text() or "") for page in reader.pages]
    return pages


def month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def periods_overlap(start: date, end: date, period_start: date, period_end: date) -> bool:
    return not (end < period_start or start > period_end)


def parse_currency_amount(value: str) -> Decimal:
    cleaned = value.replace("€", "").replace("EUR", "").strip()
    return parse_decimal(cleaned)


def parse_money_cell(value: str | None, *, default_currency: str | None = None) -> tuple[Decimal, str]:
    text = str(value or "").strip().replace("\ufeff", "").replace("\u00a0", "")
    if not text or text == "-":
        return Decimal("0"), (default_currency or "")  # noqa: FURB157

    normalized = normalize_ascii(text).upper()
    currency = default_currency or ""
    if "€" in text or "EUR" in normalized:
        currency = "EUR"
    elif "$" in text or "USD" in normalized:
        currency = "USD"
    elif "£" in text or "GBP" in normalized:
        currency = "GBP"

    cleaned = (
        text.replace("+", "")
        .replace("€", "")
        .replace("$", "")
        .replace("£", "")
        .strip()
    )
    cleaned = re.sub(r"\b(?:EUR|USD|GBP)\b", "", cleaned, flags=re.IGNORECASE).strip()
    return parse_decimal(cleaned), currency


def source_period_label(period_start: date) -> str:
    return f"{month_name[period_start.month]} {period_start.year}"


def read_csv_rows(path: Path) -> tuple[list[dict[str, str]], set[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ";"
            if sample.count(",") > sample.count(";"):
                delimiter = ","
        reader = csv.DictReader(handle, delimiter=delimiter)
        fieldnames = [name.strip() for name in (reader.fieldnames or []) if name]
        rows = [{(key or "").strip(): (value or "").strip() for key, value in row.items()} for row in reader]
    normalized_headers = {slugify(name).replace("-", " ") for name in fieldnames}
    return rows, normalized_headers


def detect_parser(path: Path, source_type: str, source_system: str, header_names: set[str] | None = None) -> str:
    headers = header_names or set()
    if source_system == "printful" and slugify(path.name) == "no-activity-during-period":
        return "parse_no_activity_marker"
    if source_type == "csv" and source_system == "woo":
        if headers >= ROW_EVENT_HEADERS["woo_tax_summary_csv"]:
            return "parse_woo_tax_summary_csv"
        if headers >= ROW_EVENT_HEADERS["woo_order_summary_csv"]:
            return "parse_woo_order_summary_csv"
        return "parse_woo_sales_csv"
    if source_type == "csv" and source_system == "paypal":
        return "parse_paypal_csv"
    if source_type == "csv" and source_system == "stripe" and headers >= ROW_EVENT_HEADERS["stripe_payouts_csv"]:
        return "parse_stripe_payouts_csv"
    if source_type == "csv" and source_system == "stripe":
        return "parse_stripe_balance_csv"
    if source_type == "csv" and source_system == "quartermaster":  # noqa: SIM102
        if headers >= ROW_EVENT_HEADERS["quartermaster_orders_csv"]:
            return "parse_quartermaster_orders_csv"
    if source_type == "csv" and source_system == "printful":
        if headers >= ROW_EVENT_HEADERS["printful_orders_csv"]:
            return "parse_printful_orders_csv"
        if headers >= ROW_EVENT_HEADERS["printful_wallet_csv"]:
            return "parse_printful_wallet_csv"
        if headers >= ROW_EVENT_HEADERS["printful_other_csv"]:
            return "parse_printful_other_csv"
        if headers >= ROW_EVENT_HEADERS["printful_services_csv"]:
            return "parse_printful_services_csv"
    if source_type == "csv" and source_system == "bank":
        return "parse_bank_csv"
    if source_type == "xml" and source_system == "bank":
        return "parse_camt_xml"
    if source_type == "pdf":
        if source_system == "stripe":
            return "parse_stripe_invoice_pdf"
        if source_system == "quartermaster":
            return "parse_quartermaster_pdf"
        if source_system == "printful":
            return "parse_printful_pdf"
        return "parse_purchase_invoice_pdf"
    if source_type == "xlsx":
        return "unsupported_xlsx"
    if source_type == "json":
        return "unsupported_json"
    if headers:
        return "unrecognized_structured_source"
    return "unrecognized_source"


def infer_pdf_coverage(text: str, source_system: str) -> tuple[date, date] | None:
    if source_system != "quartermaster" or "sales report" not in normalize_ascii(text).lower():
        return None
    match = re.search(r"Date\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        report_date = datetime.strptime(match.group(1), "%m/%d/%Y").date()  # noqa: DTZ007
    except ValueError:
        return None
    return report_date, report_date


def infer_no_activity_marker_coverage(path: Path, *, root_dir: Path) -> tuple[date, date] | None:
    if slugify(path.name) != "no-activity-during-period":
        return None
    try:
        # Coverage belongs to the logical source-pack path. Resolving a company
        # symlink here would replace that path with its private canonical target
        # and discard a logical parent such as ``2025-pack``.
        relative_parts = path.absolute().relative_to(root_dir.absolute()).parts[:-1]
    except ValueError:
        return None
    directory_names = [*relative_parts, root_dir.name]
    for directory_name in reversed(directory_names):
        normalized = normalize_ascii(directory_name).lower()
        month_match = re.fullmatch(r"(20\d{2})[-_.](0[1-9]|1[0-2])", normalized)
        if month_match:
            year = int(month_match.group(1))
            month = int(month_match.group(2))
            return date(year, month, 1), month_end(year, month)
        year_match = re.fullmatch(r"(20\d{2})(?:-pack)?", normalized)
        if year_match:
            year = int(year_match.group(1))
            return date(year, 1, 1), date(year, 12, 31)
    return None


def infer_parent_year_coverage(path: Path) -> tuple[date, date] | None:
    for parent in path.parents:
        match = re.fullmatch(r"(20\d{2})(?:-pack)?", normalize_ascii(parent.name).lower())
        if match:
            year = int(match.group(1))
            return date(year, 1, 1), date(year, 12, 31)
    return None


def source_id_for_path(path: Path, *, root_dir: Path) -> str:
    return slugify(display_path(path, root_dir).replace("/", "-"))


def display_path(path: Path, root_dir: Path) -> str:
    try:
        return str(path.relative_to(root_dir))
    except ValueError:
        return str(path)


def inspect_source_file(
    *,
    path: Path,
    root_dir: Path,
    period_start: date,
    period_end: date,
) -> SourceDescriptor | None:
    if not path.is_file():
        return None
    if path.name.startswith(".") or path.name == ".gitkeep":
        return None
    if is_ignored_work_file(path):
        return None

    source_type = source_type_from_path(path)
    header_names: set[str] | None = None
    parser_notes: list[str] = []
    content_coverage: tuple[date, date] | None = None

    source_system = infer_source_system(path, source_type)
    if source_type == "csv":
        try:
            _, header_names = read_csv_rows(path)
            source_system = infer_source_system(path, source_type, header_names)
        except UnicodeDecodeError:
            parser_notes.append("CSV could not be decoded as UTF-8.")
    elif source_type == "pdf" and PdfReader is not None:
        try:
            sample_pages = extract_pdf_pages(path)
        except SimplbooksError:
            pass
        else:
            sample_text = "\n".join(sample_pages[:2])
            source_system = infer_pdf_source_system(sample_text, source_system)
            content_coverage = infer_pdf_coverage(sample_text, source_system)

    is_no_activity_marker = source_system == "printful" and slugify(path.name) == "no-activity-during-period"
    marker_coverage = infer_no_activity_marker_coverage(path, root_dir=root_dir) if is_no_activity_marker else None
    is_woo_tax_summary = bool(
        source_type == "csv"
        and header_names is not None
        and header_names >= ROW_EVENT_HEADERS["woo_tax_summary_csv"]
    )
    parent_year_coverage = infer_parent_year_coverage(path) if is_woo_tax_summary else None
    filename_coverage = parse_filename_dates(path)
    coverage = content_coverage or marker_coverage or parent_year_coverage or filename_coverage
    annual_coverage = bool(parent_year_coverage)
    if is_woo_tax_summary and filename_coverage:
        filename_start, filename_end = filename_coverage
        annual_coverage = annual_coverage or (
            filename_start.month == 1
            and filename_start.day == 1
            and filename_end.month == 12
            and filename_end.day == 31
        )
    if coverage is None:
        coverage = (period_start, period_end)
        if is_no_activity_marker:
            parser_notes.append("Zero-activity marker has no explicit period in its path and cannot be used as authoritative evidence.")
        else:
            parser_notes.append("Coverage could not be inferred from the filename; assumed target period.")

    descriptor = SourceDescriptor(
        path=path,
        rel_path=display_path(path, root_dir),
        source_id=source_id_for_path(path, root_dir=root_dir),
        source_type=source_type,
        source_system=source_system,
        covered_from=coverage[0],
        covered_until=coverage[1],
        canonical_group=canonical_group_for_path(path),
        parser_name=(
            "unrecognized_source"
            if is_no_activity_marker and marker_coverage is None
            else detect_parser(path, source_type, source_system, header_names)
        ),
        parser_notes=parser_notes,
        context={"annual_coverage": annual_coverage} if is_woo_tax_summary else {},
    )

    if not descriptor.overlaps(period_start, period_end):
        return None
    return descriptor


def parse_purchase_note_entries(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        heading = lines[0]
        match = re.fullmatch(r"(\d{4}\.\d{2}\.\d{2})\s+([^:]+):", heading)
        if not match:
            continue
        event_date = parse_date_value(match.group(1))
        label = match.group(2).strip()
        body = " ".join(lines[1:]).strip()
        if not body:
            continue
        entries.append(
            {
                "event_date": event_date,
                "label": label,
                "body": body,
            }
        )
    return entries


def purchase_note_tokens(label: str) -> list[str]:
    stopwords = {"by", "paid", "vat"}
    tokens = []
    for token in re.findall(r"[A-Za-z0-9]+", normalize_ascii(label).lower()):
        if token in stopwords:
            continue
        if len(token) == 1:
            continue
        tokens.append(token)
    return tokens


def match_purchase_note_target(readme_path: Path, event_date: date, label: str) -> Path | None:
    candidates = [
        path
        for path in sorted(readme_path.parent.iterdir())
        if path.is_file() and path != readme_path and path.suffix.lower() != ".md"
    ]
    date_token = event_date.strftime("%Y.%m.%d")
    tokens = purchase_note_tokens(label)
    best_score = 0
    best_path: Path | None = None
    for candidate in candidates:
        stem = normalize_ascii(candidate.stem).lower().replace("_", " ")
        score = 0
        if date_token in candidate.stem:
            score += 10
        for token in tokens:
            if token in stem:
                score += 3
        if score > best_score:
            best_score = score
            best_path = candidate
    return best_path if best_score >= 10 else None


def parse_purchase_note_amounts(note_text: str) -> dict[str, Any] | None:
    normalized = re.sub(r"\s+", " ", note_text.strip())
    lower = normalize_ascii(normalized).lower()
    amounts = [parse_decimal(value) for value in re.findall(r"([0-9]+(?:[.,][0-9]+)?)\s*€", normalized)]
    if not amounts:
        return None

    gross_amount: Decimal | None = None
    vat_amount = Decimal("0")  # noqa: FURB157
    reverse_charge = "reverse-charg" in lower

    explicit_total = re.search(r"=\s*([0-9]+(?:[.,][0-9]+)?)\s*€", normalized)
    if explicit_total:
        gross_amount = parse_decimal(explicit_total.group(1))

    vat_match = re.search(r"\+\s*([0-9]+(?:[.,][0-9]+)?)\s*€\s*\(VAT\)", normalized, flags=re.IGNORECASE)
    if vat_match:
        vat_amount = parse_decimal(vat_match.group(1))
        if gross_amount is None and len(amounts) >= 2:
            gross_amount = amounts[0] + vat_amount
    elif "vat 0%" in lower or reverse_charge:
        vat_amount = Decimal("0")  # noqa: FURB157

    if gross_amount is None:
        if vat_amount != 0 and len(amounts) >= 2:
            gross_amount = amounts[0] + vat_amount
        else:
            gross_amount = amounts[-1]

    net_amount = gross_amount - vat_amount
    return {
        "gross_amount": gross_amount,
        "net_amount": net_amount,
        "vat_amount": vat_amount,
        "reverse_charge": reverse_charge,
    }


def inspect_purchase_note_markdown(
    *,
    path: Path,
    root_dir: Path,
    period_start: date,
    period_end: date,
) -> list[SourceDescriptor]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    descriptors: list[SourceDescriptor] = []
    for entry in parse_purchase_note_entries(text):
        event_date = entry["event_date"]
        if event_date < period_start or event_date > period_end:
            continue
        target_path = match_purchase_note_target(path, event_date, entry["label"])
        if target_path is None:
            continue
        amount_data = parse_purchase_note_amounts(entry["body"])
        if amount_data is None:
            continue
        target_display = display_path(target_path, root_dir)
        descriptor = SourceDescriptor(
            path=path,
            rel_path=f"{display_path(path, root_dir)}#{target_path.name}",
            source_id=f"{source_id_for_path(path, root_dir=root_dir)}-{slugify(target_path.stem)}",
            source_type="manual",
            source_system="manual",
            covered_from=event_date,
            covered_until=event_date,
            canonical_group=canonical_group_for_path(target_path),
            parser_name="parse_purchase_note_markdown",
            parser_notes=[f"Manual purchase note covering {target_display}."],
            context={
                "event_date": event_date.isoformat(),
                "label": entry["label"],
                "body": entry["body"],
                "target_path": target_display,
                "target_source_id": source_id_for_path(target_path, root_dir=root_dir),
                **amount_data,
            },
        )
        descriptors.append(descriptor)
    return descriptors


def choose_canonical_sources(sources: list[SourceDescriptor]) -> list[SourceDescriptor]:
    grouped: dict[str, list[SourceDescriptor]] = {}
    for source in sources:
        grouped.setdefault(source.canonical_group, []).append(source)

    for entries in grouped.values():
        structured = list(entries)
        if structured:
            winner = max(
                structured,
                key=lambda entry: (entry.priority, -len(entry.path.suffixes), entry.rel_path),
            )
            winner.canonical = True
            winner.preferred_over = [entry.source_id for entry in entries if entry is not winner]
            for entry in entries:
                if entry is not winner:
                    entry.canonical = False
                    entry.parser_notes.append(f"Superseded by canonical source {winner.source_id}.")
        else:
            for entry in entries:
                entry.canonical = False
                entry.parser_notes.append("Reference-only source group; no canonical machine-readable input available.")
    return sources


def load_company_base_currency(company_dir: Path, override: str | None = None) -> str:
    if override:
        return override.upper()
    profile_path = company_dir / "artifacts" / "company_profile.json"
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            currency = str(profile.get("base_currency") or "").strip().upper()
            if re.fullmatch(r"[A-Z]{3}", currency):
                return currency
        except json.JSONDecodeError:
            pass
    return "EUR"


def make_source_ref(
    source: SourceDescriptor,
    *,
    row_ref: str | None = None,
    page_ref: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "path": source.rel_path,
        "row_ref": row_ref,
        "page_ref": page_ref,
        "notes": notes,
    }


def make_record(
    *,
    source: SourceDescriptor,
    category: str,
    record_id: str,
    event_type: str,
    event_date: date,
    description: str,
    currency: str,
    gross_amount: Decimal,
    net_amount: Decimal,
    vat_amount: Decimal = Decimal("0"),  # noqa: FURB157
    fee_amount: Decimal = Decimal("0"),  # noqa: FURB157
    shipping_amount: Decimal = Decimal("0"),  # noqa: FURB157
    settlement_date: date | None = None,
    external_ref: str | None = None,
    quantity: Decimal | None = None,
    sku: str | None = None,
    warehouse_id: str | None = None,
    channel: str | None = None,
    country_code: str | None = None,
    attributes: dict[str, Any] | None = None,
    row_ref: str | None = None,
    page_ref: str | None = None,
) -> tuple[str, dict[str, Any]]:
    record = {
        "record_id": record_id,
        "source_system": source.source_system,
        "source_type": source.source_type,
        "event_type": event_type,
        "event_date": event_date.isoformat(),
        "settlement_date": settlement_date.isoformat() if settlement_date else None,
        "description": description,
        "external_ref": external_ref,
        "currency": currency,
        "gross_amount": float(gross_amount),
        "net_amount": float(net_amount),
        "vat_amount": float(vat_amount),
        "fee_amount": float(fee_amount),
        "shipping_amount": float(shipping_amount),
        "quantity": float(quantity) if quantity is not None else None,
        "sku": sku,
        "warehouse_id": warehouse_id,
        "channel": channel,
        "country_code": country_code,
        "attributes": attributes or {},
        "source_refs": [make_source_ref(source, row_ref=row_ref, page_ref=page_ref)],
    }
    return category, record


def make_exception(
    *,
    source: SourceDescriptor,
    exception_id: str,
    severity: str,
    reason: str,
    blocking: bool,
    row_ref: str | None = None,
    page_ref: str | None = None,
    suggested_follow_up: str | None = None,
) -> dict[str, Any]:
    return {
        "exception_id": exception_id,
        "severity": severity,
        "reason": reason,
        "blocking": blocking,
        "suggested_follow_up": suggested_follow_up,
        "source_refs": [make_source_ref(source, row_ref=row_ref, page_ref=page_ref)],
    }


def parser_result() -> dict[str, list[dict[str, Any]]]:
    return {
        "sales": [],
        "refunds": [],
        "fees": [],
        "payouts": [],
        "bank_transactions": [],
        "clearing_transactions": [],
        "bank_balances": [],
        "purchase_expenses": [],
        "purchase_credits": [],
        "inventory_movements": [],
        "manual_adjustments": [],
        "other": [],
    }


def parse_no_activity_marker(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    return parser_result(), []


def update_coverage_from_dates(source: SourceDescriptor, dates: list[date]) -> None:
    if dates:
        source.covered_from = min(dates)
        source.covered_until = max(dates)


def parse_woo_sales_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []

    def parse_woo_row_date(value: str) -> tuple[date, date]:
        text = str(value).strip().replace("\ufeff", "")
        if re.fullmatch(r"\d{4}-\d{1,2}", text):
            year_text, month_text = text.split("-", 1)
            year = int(year_text)
            month = int(month_text)
            return date(year, month, 1), date(year, month, monthrange(year, month)[1])
        parsed = parse_date_value(text)
        return parsed, parsed

    for line_no, row in enumerate(rows, start=2):
        row_start, row_end = parse_woo_row_date(row["Date"])
        if not periods_overlap(row_start, row_end, period_start, period_end):
            continue
        event_date = row_end
        seen_dates.append(event_date)

        if row.get("Gross sales") not in (None, "") or row.get("Total sales") not in (None, ""):
            gross_sales = parse_decimal(row.get("Gross sales"))
            returns = parse_decimal(row.get("Returns"))
            coupons = parse_decimal(row.get("Coupons"))
            net_sales = parse_decimal(row.get("Net sales"))
            taxes = parse_decimal(row.get("Taxes"))
            shipping = parse_decimal(row.get("Shipping"))
            total_sales = parse_decimal(row.get("Total sales"))
            orders = parse_decimal(row.get("Orders"))
            quantity = None
        else:
            gross_sales = parse_decimal(row.get("Gross sales amount"))
            returns = parse_decimal(row.get("Refund amount"))
            coupons = parse_decimal(row.get("Coupon amount"))
            net_sales = parse_decimal(row.get("Net sales amount"))
            shipping = parse_decimal(row.get("Shipping amount"))
            total_sales = gross_sales
            taxes = total_sales - net_sales - shipping + returns
            orders = parse_decimal(row.get("Number of orders"))
            quantity = parse_decimal(row.get("Number of items sold"))

        if total_sales == 0 and orders == 0 and gross_sales == 0 and returns == 0:
            continue

        category, record = make_record(
            source=source,
            category="sales",
            record_id=f"{source.source_id}:sales:{line_no}",
            event_type="woo_daily_sales",
            event_date=event_date,
            description=f"Woo sales summary {event_date.isoformat()}",
            currency=base_currency,
            gross_amount=total_sales,
            net_amount=net_sales,
            vat_amount=taxes,
            shipping_amount=shipping,
            external_ref=event_date.isoformat(),
            channel="woo",
            quantity=quantity,
            attributes={
                "orders": int(orders),
                "gross_sales": float(gross_sales),
                "returns": float(returns),
                "coupons": float(coupons),
                "is_monthly_summary": row_start != row_end,
            },
            row_ref=f"csv:{line_no}",
        )
        result[category].append(record)

        if returns != 0:
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:returns:{line_no}",
                    severity="warn",
                    reason="Woo daily aggregate row contains returns but does not provide refund-level detail for separate normalization.",
                    blocking=False,
                    row_ref=f"csv:{line_no}",
                    suggested_follow_up="Use a refund-detail export if separate refund rows are required for this month.",
                )
            )

    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def parse_woo_order_summary_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Retain Woo order-summary rows as nonfinancial supporting evidence.

    The Woo Analytics order export exposes net sales but omits customer-paid
    gross, shipping, and tax. Treating it as a sale would duplicate a complete
    merchant or processor export and invent a gross amount, so its amounts stay
    in attributes for order matching only.
    """
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []

    for line_no, row in enumerate(rows, start=2):
        row_ref = f"csv:{line_no}"
        try:
            event_date = parse_date_value(row.get("Date", ""))
            order_id = str(row.get("Order #") or "").strip()
            net_sales = parse_decimal(row.get("Net sales"))
            items_sold = parse_decimal(row.get("Items sold"))
            if (
                not order_id
                or not net_sales.is_finite()
                or not items_sold.is_finite()
                or items_sold < 0
                or items_sold != items_sold.to_integral_value()
            ):
                raise SimplbooksError("invalid Woo order summary row")
        except (InvalidOperation, SimplbooksError, ValueError):
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:invalid-order-summary:{line_no}",
                    severity="warn",
                    reason="Woo order-summary row has an invalid date, order number, net-sales value, or item count.",
                    blocking=False,
                    row_ref=row_ref,
                    suggested_follow_up="Re-export the Woo order report if this row is needed for order matching.",
                )
            )
            continue

        seen_dates.append(event_date)
        if not periods_overlap(event_date, event_date, period_start, period_end):
            continue

        _, record = make_record(
            source=source,
            category="other",
            record_id=f"{source.source_id}:woo-order:{line_no}",
            event_type="woo_order_summary",
            event_date=event_date,
            description=f"Woo order-summary evidence {order_id}",
            currency=base_currency,
            gross_amount=Decimal("0"),  # noqa: FURB157
            net_amount=Decimal("0"),  # noqa: FURB157
            external_ref=order_id,
            channel="woo",
            attributes={
                "order_id": order_id,
                "status": str(row.get("Status") or "").strip(),
                "product_summary": str(row.get("Product(s)") or "").strip(),
                "items_sold": float(items_sold),
                "observed_net_sales": float(net_sales),
                "customer_type": str(row.get("Customer type") or "").strip(),
                "attribution": str(row.get("Attribution") or "").strip(),
                "nonfinancial_supporting_evidence": True,
            },
            row_ref=row_ref,
        )
        result["other"].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def parse_woo_tax_summary_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    result = parser_result()
    exceptions: list[dict[str, Any]] = []

    if not source.context.get("annual_coverage"):
        exceptions.append(
            make_exception(
                source=source,
                exception_id=f"{source.source_id}:missing-annual-coverage",
                severity="error",
                reason="Woo tax summary lacks annual coverage in its filename or source-pack directory.",
                blocking=True,
                suggested_follow_up="Place the export in a year-bearing source pack or rename it to include the covered year.",
            )
        )
        return result, exceptions

    if period_end != source.covered_until:
        return result, exceptions

    rows, _ = read_csv_rows(source.path)
    cents = Decimal("0.01")
    for line_no, row in enumerate(rows, start=2):
        try:
            rate = parse_decimal(row.get("Rate"))
            total = parse_decimal(row.get("Total tax"))
            order_tax = parse_decimal(row.get("Order tax"))
            shipping_tax = parse_decimal(row.get("Shipping tax"))
            orders = parse_decimal(row.get("Orders"))
            tax_code = row.get("Tax code", "").strip()
            country_match = re.fullmatch(r"([A-Z]{2})-[A-Z]{2}-VAT-[A-Za-z0-9-]+", tax_code)
            valid_numbers = all(value.is_finite() for value in (rate, total, order_tax, shipping_tax, orders))
            valid_row = (
                bool(country_match)
                and valid_numbers
                and rate >= 0
                and total >= 0
                and order_tax >= 0
                and shipping_tax >= 0
                and all(
                    value == value.quantize(cents, rounding=ROUND_HALF_UP)
                    for value in (total, order_tax, shipping_tax)
                )
                and orders > 0
                and orders == orders.to_integral_value()
                and total.quantize(cents, rounding=ROUND_HALF_UP)
                == (order_tax + shipping_tax).quantize(cents, rounding=ROUND_HALF_UP)
            )
        except (InvalidOperation, SimplbooksError, ValueError):
            valid_row = False
            country_match = None

        if not valid_row:
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:invalid-tax-row:{line_no}",
                    severity="error",
                    reason="Woo tax row has an invalid code, count, rate, or component total.",
                    blocking=True,
                    row_ref=f"csv:{line_no}",
                    suggested_follow_up="Export a corrected Woo tax summary before rebuilding this year.",
                )
            )
            continue

        _, record = make_record(
            source=source,
            category="other",
            record_id=f"{source.source_id}:woo-tax:{line_no}",
            event_type="woo_tax_summary",
            event_date=source.covered_until,
            description=f"Woo annual tax summary {tax_code}",
            currency=base_currency,
            gross_amount=Decimal("0"),  # noqa: FURB157
            net_amount=Decimal("0"),  # noqa: FURB157
            vat_amount=total,
            external_ref=tax_code,
            channel="woo",
            country_code=country_match.group(1),
            attributes={
                "tax_code": tax_code,
                "configured_rate": float(rate),
                "order_tax": float(order_tax),
                "shipping_tax": float(shipping_tax),
                "total_tax": float(total),
                "orders": int(orders),
                "annual_evidence": True,
            },
            row_ref=f"csv:{line_no}",
        )
        result["other"].append(record)

    return result, exceptions


def discover_canonical_woo_tax_evidence(
    *, source_dir: Path, root_dir: Path, year: int
) -> list[dict[str, Any]]:
    if not source_dir.exists():
        return []
    tax_sources: list[SourceDescriptor] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".csv":
            continue
        source = inspect_source_file(
            path=path,
            root_dir=root_dir,
            period_start=date(year, 1, 1),
            period_end=date(year, 12, 31),
        )
        if source is not None and source.parser_name == "parse_woo_tax_summary_csv":
            tax_sources.append(source)
    return canonical_woo_tax_evidence(choose_canonical_sources(tax_sources), year=year)


def canonical_woo_tax_evidence(
    sources: list[SourceDescriptor], *, year: int
) -> list[dict[str, Any]]:
    """Parse canonical annual Woo tax CSVs into evidence independent of allocation JSON."""
    period_start = date(year, 12, 1)
    period_end = date(year, 12, 31)
    evidence: list[dict[str, Any]] = []
    tax_sources = [
        source
        for source in sources
        if source.canonical and source.parser_name == "parse_woo_tax_summary_csv"
    ]
    for source in tax_sources:
        if source.covered_from != date(year, 1, 1) or source.covered_until != period_end:
            raise SimplbooksError(
                f"Canonical Woo tax source {source.rel_path} does not cover the requested year {year}."
            )
        records, exceptions = parse_woo_tax_summary_csv(
            source,
            period_start=period_start,
            period_end=period_end,
            base_currency="EUR",
        )
        blocking = [item for item in exceptions if item.get("blocking")]
        if blocking:
            raise SimplbooksError(
                f"Canonical Woo tax source {source.rel_path} is invalid: {blocking[0].get('reason')}"
            )
        rows: list[dict[str, Any]] = []
        for record in records["other"]:
            attributes = record.get("attributes") or {}
            source_ref = (record.get("source_refs") or [{}])[0]
            rows.append(
                {
                    "source_row_id": str(record.get("record_id") or ""),
                    "row_ref": str(source_ref.get("row_ref") or ""),
                    "country_code": str(record.get("country_code") or ""),
                    "tax_code": str(attributes.get("tax_code") or ""),
                    "configured_rate": attributes.get("configured_rate"),
                    "order_tax": attributes.get("order_tax"),
                    "shipping_tax": attributes.get("shipping_tax"),
                    "total_tax": attributes.get("total_tax"),
                    "orders": attributes.get("orders"),
                }
            )
        if not rows:
            raise SimplbooksError(f"Canonical Woo tax source {source.rel_path} contains no tax rows.")
        manifest = source.manifest_entry()
        evidence.append(
            {
                "source_id": source.source_id,
                "path": source.rel_path,
                "sha256": manifest["sha256"],
                "year": year,
                "rows": rows,
            }
        )
    return evidence


def load_bound_woo_tax_allocation(
    *,
    allocation_path: Path,
    sources: list[SourceDescriptor],
    company_slug: str,
    year: int,
    repo_root: Path,
) -> dict[str, Any]:
    tax_evidence = canonical_woo_tax_evidence(sources, year=year)
    if not tax_evidence:
        raise SimplbooksError("Woo tax allocation requires nonempty canonical Woo tax evidence.")
    allocation = woo_tax.load_allocation(
        allocation_path,
        company_slug=company_slug,
        year=year,
        tax_evidence=tax_evidence,
    )
    allocation["_allocation_path"] = display_path(allocation_path, repo_root)
    return allocation


def paypal_category(
    row: dict[str, str],
    gross_amount: Decimal,
    *,
    funded_payment_ids: set[str] | None = None,
    supplier_payment_parties: dict[str, str] | None = None,
    customer_receipt_parties: dict[str, str] | None = None,
) -> str:
    type_value = row.get("Type", "").strip().lower()
    balance_impact = row.get("Balance Impact", "").strip().lower()
    transaction_id = row.get("Transaction ID", "").strip()
    reference_id = row.get("Reference Txn ID", "").strip()
    funded_payment_ids = funded_payment_ids or set()
    supplier_payment_parties = supplier_payment_parties or {}
    customer_receipt_parties = customer_receipt_parties or {}
    counterparty = " ".join(row.get("Name", "").split()).casefold()
    if type_value == "general card withdrawal":
        return "clearing_transactions" if gross_amount < 0 and balance_impact == "debit" and reference_id else "ambiguous"
    if type_value == "general card deposit":
        return "clearing_transactions" if gross_amount > 0 and balance_impact == "credit" and reference_id else "ambiguous"
    if type_value == "general currency conversion":
        sign_matches = (gross_amount < 0 and balance_impact == "debit") or (gross_amount > 0 and balance_impact == "credit")
        return "clearing_transactions" if sign_matches and reference_id else "ambiguous"
    if type_value == "general payment":
        if gross_amount > 0 and balance_impact == "credit":
            return "sales"
        if gross_amount < 0 and balance_impact == "debit" and transaction_id in funded_payment_ids:
            return "clearing_transactions"
        return "ambiguous"
    if type_value == "payment reversal":
        if (
            gross_amount > 0
            and balance_impact == "credit"
            and supplier_payment_parties.get(reference_id)
            and counterparty in {supplier_payment_parties[reference_id], "paypal"}
        ):
            return "clearing_transactions"
        if (
            gross_amount < 0
            and balance_impact == "debit"
            and counterparty
            and customer_receipt_parties.get(reference_id) == counterparty
        ):
            return "refunds"
        return "ambiguous"
    if "withdrawal" in type_value or "payout" in type_value:
        return "payouts"
    if "deposit" in type_value or "currency conversion" in type_value:
        return "other"
    if "transfer" in type_value:
        return "payouts"
    if "refund" in type_value or "reversal" in type_value or "chargeback" in type_value or gross_amount < 0:
        return "refunds"
    if "fee" in type_value and gross_amount == 0:
        return "fees"
    return "sales"


def parse_paypal_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []
    funded_payment_ids = {
        str(row.get("Reference Txn ID") or "").strip()
        for row in rows
        if str(row.get("Type") or "").strip().lower()
        in {"general card withdrawal", "general card deposit", "general currency conversion"}
        and str(row.get("Reference Txn ID") or "").strip()
    }
    def counterparty(row: dict[str, str]) -> str:
        return " ".join(str(row.get("Name") or "").split()).casefold()

    customer_receipt_parties = {
        str(row.get("Transaction ID") or "").strip(): counterparty(row)
        for row in rows
        if str(row.get("Type") or "").strip().lower() == "general payment"
        and str(row.get("Status") or "").strip().lower() in {"", "completed", "refunded"}
        and parse_decimal(row.get("Gross")) > 0
        and str(row.get("Balance Impact") or "").strip().lower() == "credit"
        and str(row.get("Transaction ID") or "").strip()
        and counterparty(row)
    }
    supplier_payment_parties = {
        str(row.get("Transaction ID") or "").strip(): counterparty(row)
        for row in rows
        if str(row.get("Type") or "").strip().lower() == "general payment"
        and parse_decimal(row.get("Gross")) < 0
        and str(row.get("Balance Impact") or "").strip().lower() == "debit"
        and str(row.get("Transaction ID") or "").strip() in funded_payment_ids
        and counterparty(row)
    }

    for line_no, row in enumerate(rows, start=2):
        event_date = parse_date_value(row["Date"])
        if event_date < period_start or event_date > period_end:
            continue
        seen_dates.append(event_date)

        status = row.get("Status", "").strip()
        if status and status.lower() not in {"completed", "refunded"}:
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:status:{line_no}",
                    severity="warn",
                    reason=f"Skipped PayPal row with unsupported status {status!r}.",
                    blocking=False,
                    row_ref=f"csv:{line_no}",
                )
            )
            continue

        gross = parse_decimal(row.get("Gross"))
        fee = abs(parse_decimal(row.get("Fee")))
        net = parse_decimal(row.get("Net"))
        shipping = abs(parse_decimal(row.get("Shipping and Handling Amount")))
        sales_tax = abs(parse_decimal(row.get("Sales Tax")))
        category = paypal_category(
            row,
            gross,
            funded_payment_ids=funded_payment_ids,
            supplier_payment_parties=supplier_payment_parties,
            customer_receipt_parties=customer_receipt_parties,
        )
        if category == "ambiguous":
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:paypal-routing:{line_no}",
                    severity="error",
                    reason="Ambiguous PayPal general movement lacks an exact signed balance-impact and reference bridge.",
                    blocking=True,
                    row_ref=f"csv:{line_no}",
                )
            )
            continue
        if category == "payouts":
            gross = abs(gross)
            net = abs(net)
        type_slug = slugify(row.get("Type", "paypal-event")).replace("-", "_")
        currency = row.get("Currency", "").strip().upper() or base_currency

        attributes = {
            "status": status,
            "item_title": row.get("Item Title"),
            "item_id": row.get("Item ID"),
            "balance_impact": row.get("Balance Impact"),
            "from_email": row.get("From Email Address"),
            "to_email": row.get("To Email Address"),
            "quantity": row.get("Quantity"),
            "reference_transaction_id": row.get("Reference Txn ID"),
        }
        if category == "clearing_transactions":
            attributes.update({
                "clearing_provider": "paypal",
                "clearing_account": "paypal_wallet",
            })

        category_name, record = make_record(
            source=source,
            category=category,
            record_id=f"{source.source_id}:{category}:{line_no}",
            event_type=f"paypal_{type_slug}",
            event_date=event_date,
            settlement_date=event_date,
            description=f"PayPal {row.get('Type', 'event')} - {row.get('Name', '').strip() or 'unknown party'}",
            currency=currency,
            gross_amount=gross,
            net_amount=net,
            vat_amount=sales_tax,
            fee_amount=fee,
            shipping_amount=shipping,
            external_ref=row.get("Transaction ID") or None,
            channel="paypal",
            country_code=(row.get("Country Code") or "").strip().upper() or None,
            attributes=attributes,
            row_ref=f"csv:{line_no}",
        )
        result[category_name].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def stripe_category(row: dict[str, str], amount: Decimal) -> str:
    type_value = row.get("Type", "").strip().lower()
    if "payout" in type_value or "transfer" in type_value:
        return "payouts"
    if "refund" in type_value or "reversal" in type_value or "dispute" in type_value or "chargeback" in type_value:
        return "refunds"
    if "fee" in type_value:
        return "fees"
    if amount < 0:
        return "refunds"
    return "sales"


def parse_stripe_payouts_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []
    seen_payout_ids: set[str] = set()

    for line_no, row in enumerate(rows, start=2):
        payout_id = (row.get("payout_id") or "").strip()
        effective_at = (row.get("effective_at_utc") or "").strip()
        if not effective_at:
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:missing-effective-date:{line_no}",
                    severity="error",
                    reason="Stripe payout row has no effective_at_utc date.",
                    blocking=True,
                    row_ref=f"csv:{line_no}",
                    suggested_follow_up="Re-export Stripe payout history with effective dates.",
                )
            )
            continue

        event_date = parse_date_value(effective_at)
        if event_date < period_start or event_date > period_end:
            continue
        seen_dates.append(event_date)

        if payout_id and payout_id in seen_payout_ids:
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:duplicate:{payout_id}",
                    severity="warn",
                    reason=f"Skipped duplicate Stripe payout row {payout_id}.",
                    blocking=False,
                    row_ref=f"csv:{line_no}",
                )
            )
            continue
        if payout_id:
            seen_payout_ids.add(payout_id)

        status = normalize_ascii(row.get("payout_status") or "").strip().lower()
        reversed_at = (row.get("payout_reversed_at_utc") or "").strip()
        if status in {"failed", "canceled", "cancelled"}:
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:skipped-status:{line_no}",
                    severity="warn",
                    reason=f"Skipped Stripe payout with non-settled status {row.get('payout_status')!r}.",
                    blocking=False,
                    row_ref=f"csv:{line_no}",
                )
            )
            continue
        if reversed_at or status != "paid":
            reason = (
                "Stripe payout was reversed and requires explicit reconciliation."
                if reversed_at
                else f"Stripe payout has pending or unknown status {row.get('payout_status')!r}."
            )
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:unsettled:{line_no}",
                    severity="error",
                    reason=reason,
                    blocking=True,
                    row_ref=f"csv:{line_no}",
                    suggested_follow_up="Resolve the payout status in Stripe or provide evidence of the reversal before reconciliation.",
                )
            )
            continue

        reporting_category = normalize_ascii(row.get("reporting_category") or "").strip().lower()
        if reporting_category != "payout":
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:unexpected-category:{line_no}",
                    severity="error",
                    reason=f"Stripe payout export row has unexpected reporting category {row.get('reporting_category')!r}.",
                    blocking=True,
                    row_ref=f"csv:{line_no}",
                )
            )
            continue

        gross = abs(parse_decimal(row.get("gross")))
        fee = abs(parse_decimal(row.get("fee")))
        net = abs(parse_decimal(row.get("net")))
        currency = (row.get("currency") or base_currency).strip().upper()
        expected_arrival = (row.get("payout_expected_arrival_date") or "").strip()
        settlement_date = parse_date_value(expected_arrival) if expected_arrival else event_date
        description = (row.get("payout_description") or row.get("description") or "Stripe payout").strip()

        category, record = make_record(
            source=source,
            category="payouts",
            record_id=f"{source.source_id}:payout:{payout_id or line_no}",
            event_type="stripe_payout",
            event_date=event_date,
            settlement_date=settlement_date,
            description=description,
            currency=currency,
            gross_amount=gross,
            net_amount=net,
            fee_amount=fee,
            external_ref=payout_id or None,
            channel="stripe",
            attributes={
                "stripe_export_type": "payouts_history",
                "stripe_payout_id": payout_id or None,
                "stripe_balance_transaction_id": row.get("balance_transaction_id") or None,
                "payout_status": row.get("payout_status") or None,
                "payout_type": row.get("payout_type") or None,
                "payout_destination_id": row.get("payout_destination_id") or None,
                "trace_id": row.get("trace_id") or None,
                "trace_id_status": row.get("trace_id_status") or None,
                "application_fee": row.get("application_fee") or None,
            },
            row_ref=f"csv:{line_no}",
        )
        result[category].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def parse_stripe_balance_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []
    seen_transaction_ids: set[str] = set()
    is_charges_export = bool(rows and "Created date (UTC)" in rows[0])

    for line_no, row in enumerate(rows, start=2):
        if is_charges_export:
            transaction_id = (row.get("id") or "").strip()
            if transaction_id and transaction_id in seen_transaction_ids:
                exceptions.append(
                    make_exception(
                        source=source,
                        exception_id=f"{source.source_id}:duplicate:{transaction_id}",
                        severity="warn",
                        reason=f"Skipped duplicate Stripe charges-export row for transaction {transaction_id}.",
                        blocking=False,
                        row_ref=f"csv:{line_no}",
                    )
                )
                continue
            if transaction_id:
                seen_transaction_ids.add(transaction_id)

            event_date = parse_date_value(row["Created date (UTC)"])
            amount = parse_decimal(row.get("Amount"))
            fee = abs(parse_decimal(row.get("Fee")))
            refunded = abs(parse_decimal(row.get("Amount Refunded")))
            refunded_date_raw = (row.get("Refunded date (UTC)") or "").strip()
            refunded_date = parse_date_value(refunded_date_raw) if refunded and refunded_date_raw else None
            charge_in_period = period_start <= event_date <= period_end
            refund_in_period = refunded_date is not None and period_start <= refunded_date <= period_end
            if not charge_in_period and not refund_in_period:
                continue

            status = normalize_ascii(row.get("Status") or "").strip().lower()
            successful_statuses = {"paid", "succeeded", "refunded", "partially refunded"}
            skipped_statuses = {"failed", "canceled", "cancelled"}
            if status in skipped_statuses:
                exceptions.append(
                    make_exception(
                        source=source,
                        exception_id=f"{source.source_id}:skipped-status:{line_no}",
                        severity="warn",
                        reason=f"Skipped Stripe charge with non-successful status {row.get('Status')!r}.",
                        blocking=False,
                        row_ref=f"csv:{line_no}",
                    )
                )
                continue
            if amount != 0 and status not in successful_statuses:
                exceptions.append(
                    make_exception(
                        source=source,
                        exception_id=f"{source.source_id}:unapproved-status:{line_no}",
                        severity="error",
                        reason=f"Stripe charge has pending or unknown status {row.get('Status')!r}; it cannot be normalized as a completed sale.",
                        blocking=True,
                        row_ref=f"csv:{line_no}",
                        suggested_follow_up="Resolve the charge status in Stripe or provide a final-status export.",
                    )
                )
                continue
            if refunded > abs(amount):
                exceptions.append(
                    make_exception(
                        source=source,
                        exception_id=f"{source.source_id}:refund-exceeds-charge:{line_no}",
                        severity="error",
                        reason=f"Stripe refunded amount {refunded} exceeds charge amount {abs(amount)}.",
                        blocking=True,
                        row_ref=f"csv:{line_no}",
                        suggested_follow_up="Verify the Stripe charge and refund export values.",
                    )
                )
                continue
            currency = row.get("Currency", "").strip().upper() or base_currency
            payment_intent_id = (row.get("PaymentIntent ID") or "").strip()
            external_ref = payment_intent_id or transaction_id or None
            customer_ref = (
                row.get("customer_name (metadata)")
                or row.get("customer_email (metadata)")
                or row.get("order_id (metadata)")
                or row.get("Description")
                or "unknown reference"
            )
            description = f"Stripe charge - {customer_ref}"
            attributes = {
                "stripe_balance_transaction_id": transaction_id,
                "stripe_source_id": payment_intent_id or transaction_id,
                "stripe_export_type": "charges",
                "status": row.get("Status"),
                "payment_type": row.get("payment_type (metadata)"),
                "site_url": row.get("site_url (metadata)"),
                "order_id": row.get("order_id (metadata)"),
                "order_key": row.get("order_key (metadata)"),
                "customer_email": row.get("customer_email (metadata)"),
                "customer_name": row.get("customer_name (metadata)"),
            }

            tax_amount = abs(parse_decimal(row.get("tax_amount (metadata)"))) / Decimal("100")  # noqa: FURB157
            if charge_in_period and amount != 0:
                seen_dates.append(event_date)
                category_name, record = make_record(
                    source=source,
                    category="sales",
                    record_id=f"{source.source_id}:sales:{line_no}",
                    event_type="stripe_charge",
                    event_date=event_date,
                    settlement_date=event_date,
                    description=description,
                    currency=currency,
                    gross_amount=amount,
                    net_amount=amount - fee,
                    vat_amount=tax_amount,
                    fee_amount=fee,
                    external_ref=external_ref,
                    channel="stripe",
                    country_code=(row.get("Card Address Country") or row.get("Shipping Address Country") or "").strip().upper() or None,
                    attributes=attributes,
                    row_ref=f"csv:{line_no}",
                )
                result[category_name].append(record)

            if refunded and not refunded_date_raw and charge_in_period:
                exceptions.append(
                    make_exception(
                        source=source,
                        exception_id=f"{source.source_id}:refund-date-missing:{line_no}",
                        severity="error",
                        reason="Stripe charge reports a refunded amount but has no refund date.",
                        blocking=True,
                        row_ref=f"csv:{line_no}",
                        suggested_follow_up="Export Stripe charges with the Refunded date (UTC) column populated.",
                    )
                )
            elif refunded and refunded_date is not None:  # noqa: SIM102
                if refund_in_period:
                    seen_dates.append(refunded_date)
                    refund_tax = Decimal("0")  # noqa: FURB157
                    if amount != 0:
                        refund_tax = (tax_amount * refunded / abs(amount)).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                    category_name, record = make_record(
                        source=source,
                        category="refunds",
                        record_id=f"{source.source_id}:refunds:{line_no}",
                        event_type="stripe_refund",
                        event_date=refunded_date,
                        settlement_date=refunded_date,
                        description=f"Stripe refund - {customer_ref}",
                        currency=currency,
                        gross_amount=-refunded,
                        net_amount=-refunded,
                        vat_amount=-refund_tax,
                        external_ref=external_ref,
                        channel="stripe",
                        country_code=(row.get("Card Address Country") or row.get("Shipping Address Country") or "").strip().upper() or None,
                        attributes=attributes,
                        row_ref=f"csv:{line_no}",
                    )
                    result[category_name].append(record)
            continue

        event_date = parse_date_value(row["Created (UTC)"])
        if event_date < period_start or event_date > period_end:
            continue
        seen_dates.append(event_date)

        transaction_id = (row.get("id") or "").strip()
        if transaction_id:
            if transaction_id in seen_transaction_ids:
                exceptions.append(
                    make_exception(
                        source=source,
                        exception_id=f"{source.source_id}:duplicate:{transaction_id}",
                        severity="warn",
                        reason=f"Skipped duplicate Stripe balance-history row for transaction {transaction_id}.",
                        blocking=False,
                        row_ref=f"csv:{line_no}",
                    )
                )
                continue
            seen_transaction_ids.add(transaction_id)

        amount = parse_decimal(row.get("Amount"))
        fee = abs(parse_decimal(row.get("Fee")))
        net = parse_decimal(row.get("Net"))
        category = stripe_category(row, amount)
        settlement_date = parse_date_value(row["Available On (UTC)"])
        currency = row.get("Currency", "").strip().upper() or base_currency
        description = f"Stripe {row.get('Type', 'event')} - {row.get('customer_name (metadata)') or row.get('customer_email (metadata)') or row.get('order_id (metadata)') or row.get('Source') or 'unknown reference'}"
        external_ref = row.get("Source") or transaction_id or None
        tax_amount = abs(parse_decimal(row.get("tax_amount (metadata)")))

        if category == "payouts":
            category_name, record = make_record(
                source=source,
                category="payouts",
                record_id=f"{source.source_id}:payouts:{line_no}",
                event_type="stripe_payout",
                event_date=event_date,
                settlement_date=settlement_date,
                description=description,
                currency=currency,
                gross_amount=abs(amount),
                net_amount=abs(net),
                external_ref=external_ref,
                channel="stripe",
                attributes={
                    "stripe_balance_transaction_id": transaction_id,
                    "stripe_source_id": row.get("Source"),
                    "stripe_export_type": "balance_history",
                    "order_id": row.get("order_id (metadata)"),
                },
                row_ref=f"csv:{line_no}",
            )
            result[category_name].append(record)
            continue

        if category == "fees":
            fee_total = fee or abs(amount) or abs(net)
            if fee_total == 0:
                continue
            category_name, record = make_record(
                source=source,
                category="fees",
                record_id=f"{source.source_id}:fees:{line_no}",
                event_type=f"stripe_{slugify(row.get('Type', 'fee')).replace('-', '_')}",
                event_date=event_date,
                settlement_date=settlement_date,
                description=description,
                currency=currency,
                gross_amount=fee_total,
                net_amount=fee_total,
                fee_amount=fee_total,
                external_ref=external_ref,
                channel="stripe",
                attributes={
                    "stripe_balance_transaction_id": transaction_id,
                    "stripe_source_id": row.get("Source"),
                    "stripe_export_type": "balance_history",
                    "order_id": row.get("order_id (metadata)"),
                },
                row_ref=f"csv:{line_no}",
            )
            result[category_name].append(record)
            continue

        if category == "refunds":
            amount = -abs(amount)
            net = -abs(net)

        category_name, record = make_record(
            source=source,
            category=category,
            record_id=f"{source.source_id}:{category}:{line_no}",
            event_type=f"stripe_{slugify(row.get('Type', 'event')).replace('-', '_')}",
            event_date=event_date,
            settlement_date=settlement_date,
            description=description,
            currency=currency,
            gross_amount=amount,
            net_amount=net,
            vat_amount=tax_amount,
            fee_amount=fee,
            external_ref=external_ref,
            channel="stripe",
            country_code=None,
            attributes={
                "stripe_balance_transaction_id": transaction_id,
                "stripe_source_id": row.get("Source"),
                "stripe_export_type": "balance_history",
                "reason": row.get("reason (metadata)"),
                "payment_type": row.get("payment_type (metadata)"),
                "site_url": row.get("site_url (metadata)"),
                "order_id": row.get("order_id (metadata)"),
                "order_key": row.get("order_key (metadata)"),
                "customer_email": row.get("customer_email (metadata)"),
                "customer_name": row.get("customer_name (metadata)"),
            },
            row_ref=f"csv:{line_no}",
        )
        result[category_name].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def parse_quartermaster_date_value(value: str) -> date:
    text = re.sub(r"\s+", " ", str(value).strip().replace("\ufeff", ""))
    for fmt in (
        "%m/%d/%Y",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    raise SimplbooksError(f"Unsupported Quartermaster date format: {value!r}")


def parse_quartermaster_orders_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    seen_dates: list[date] = []

    for line_no, row in enumerate(rows, start=2):
        submitted_text = row.get("DateSubmitted") or ""
        shipped_text = row.get("DateShipped") or ""
        if not submitted_text and not shipped_text:
            continue

        submitted_date = parse_quartermaster_date_value(submitted_text) if submitted_text else None
        shipped_date = parse_quartermaster_date_value(shipped_text) if shipped_text else None
        anchor_date = submitted_date or shipped_date
        if anchor_date is None:
            continue
        if anchor_date < period_start or anchor_date > period_end:
            continue
        seen_dates.append(anchor_date)

        reference_id = (row.get("ReferenceID") or "").strip()
        qml_order_id = (row.get("QMLOrderID") or "").strip()
        description_ref = reference_id or qml_order_id or f"line {line_no}"
        category, record = make_record(
            source=source,
            category="other",
            record_id=f"{source.source_id}:order:{line_no}",
            event_type="quartermaster_order_history",
            event_date=anchor_date,
            settlement_date=shipped_date,
            description=f"Quartermaster order history {description_ref}",
            currency=base_currency,
            gross_amount=Decimal("0"),  # noqa: FURB157
            net_amount=Decimal("0"),  # noqa: FURB157
            external_ref=reference_id or qml_order_id or None,
            channel="quartermaster",
            attributes={
                "reference_id": reference_id or None,
                "qml_order_id": qml_order_id or None,
                "status": row.get("Status"),
                "order_type": row.get("OrderType"),
                "carrier": row.get("Carrier"),
                "shipping_type": row.get("ShippingType"),
                "tracking_number": row.get("TrackingNumber"),
                "email": row.get("Email"),
                "name": row.get("Name"),
            },
            row_ref=f"csv:{line_no}",
        )
        result[category].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, []


def parse_bank_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    seen_dates: list[date] = []

    for line_no, row in enumerate(rows, start=2):
        event_date = parse_date_value(row["Kuupäev"])
        if event_date < period_start or event_date > period_end:
            continue
        seen_dates.append(event_date)

        amount = parse_decimal(row.get("Summa"))
        dc_indicator = row.get("Deebet/Kreedit (D/C)", "").strip().upper()
        if dc_indicator == "D" and amount > 0:
            amount = -amount
        if dc_indicator == "C" and amount < 0:
            amount = abs(amount)

        currency = row.get("Valuuta", "").strip().upper() or base_currency
        description = row.get("Selgitus") or row.get("Saaja/maksja nimi") or "Bank transaction"
        archive_identifier = str(row.get("Arhiveerimistunnus") or "").strip() or None
        account_servicer_reference = str(row.get("Konto teenusepakkuja viide") or "").strip() or None
        entry_reference = str(row.get("Kande viide") or "").strip() or None
        document_number = str(row.get("Dokumendi number") or "").strip() or None
        category, record = make_record(
            source=source,
            category="bank_transactions",
            record_id=f"{source.source_id}:bank:{line_no}",
            event_type="bank_credit" if amount >= 0 else "bank_debit",
            event_date=event_date,
            description=description,
            currency=currency,
            gross_amount=amount,
            net_amount=amount,
            external_ref=archive_identifier or account_servicer_reference or entry_reference or document_number,
            attributes={
                "customer_account": row.get("Kliendi konto"),
                "counterparty_account": row.get("Saaja/maksja konto"),
                "counterparty_name": row.get("Saaja/maksja nimi"),
                "reference_number": row.get("Viitenumber"),
                "bank_bic": row.get("Saaja/maksja panga BIC"),
                "archive_identifier": archive_identifier,
                "account_servicer_reference": account_servicer_reference,
                "entry_reference": entry_reference,
            },
            row_ref=f"csv:{line_no}",
        )
        result[category].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, []


def parse_printful_orders_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []
    grouped_rows: dict[str, dict[str, Any]] = {}

    component_columns = (
        "Products",
        "Discount",
        "Shipping",
        "Digitization",
        "Branding",
        "Fulfillment fees",
        "Tax",
        "VAT",
        "Total",
    )

    for line_no, row in enumerate(rows, start=2):
        date_text = row.get("Date", "")
        if normalize_ascii(date_text).lower().startswith("total paid"):
            continue

        event_date = parse_date_value(date_text)
        if event_date < period_start or event_date > period_end:
            continue

        total_amount, total_currency = parse_money_cell(row.get("Total"), default_currency=base_currency)  # noqa: RUF059
        currency = total_currency or base_currency

        components: dict[str, Decimal] = {}
        for column in component_columns:
            amount, component_currency = parse_money_cell(row.get(column), default_currency=currency)
            if component_currency and component_currency != currency:
                raise SimplbooksError(
                    f"Printful Orders.csv row {line_no} mixes currencies in one record, which is unsupported."
                )
            components[column] = amount

        if components["Total"] == 0 and all(components[column] == 0 for column in component_columns if column != "Total"):
            continue

        seen_dates.append(event_date)
        group_key = str(row.get("Printful ID") or row.get("Order") or line_no).strip()
        group = grouped_rows.setdefault(
            group_key,
            {
                "line_nos": [],
                "dates": [],
                "currency": currency,
                "order_labels": [],
                "statuses": set(),
                "payment_instruments": set(),
                "shipping_origins": set(),
                "shipping_destinations": set(),
                "components": {column: Decimal("0") for column in component_columns},  # noqa: FURB157
            },
        )
        if group["currency"] != currency:
            raise SimplbooksError(f"Printful Orders.csv rows for {group_key} use multiple currencies, which is unsupported.")
        group["line_nos"].append(line_no)
        group["dates"].append(event_date)
        group["statuses"].add(str(row.get("Status") or "").strip())
        if row.get("Order"):
            group["order_labels"].append(str(row["Order"]).strip())
        if row.get("Payment Instrument"):
            group["payment_instruments"].add(str(row["Payment Instrument"]).strip())
        if row.get("Shipped from"):
            group["shipping_origins"].add(str(row["Shipped from"]).strip())
        if row.get("Shipped to"):
            group["shipping_destinations"].add(str(row["Shipped to"]).strip())
        for column, amount in components.items():
            group["components"][column] += amount

    for group_key, group in sorted(grouped_rows.items()):
        gross_amount = group["components"]["Total"]
        if gross_amount <= 0:
            if gross_amount < 0:
                representative_label = next((label for label in group["order_labels"] if label), group_key)
                origin = next(iter(sorted(group["shipping_origins"])), None)
                vat_amount = abs(group["components"]["VAT"])
                net_amount = abs(
                    sum(
                        group["components"][column]
                        for column in ("Products", "Discount", "Shipping", "Digitization", "Branding", "Fulfillment fees", "Tax")
                    )
                )
                if net_amount == 0:
                    net_amount = abs(gross_amount) - vat_amount
                category, record = make_record(
                    source=source,
                    category="purchase_credits",
                    record_id=f"{source.source_id}:credit:{slugify(group_key)}",
                    event_type="printful_supplier_credit",
                    event_date=max(group["dates"]),
                    settlement_date=max(group["dates"]),
                    description=f"Printful supplier credit for {representative_label}",
                    currency=group["currency"],
                    gross_amount=abs(gross_amount),
                    net_amount=net_amount,
                    vat_amount=vat_amount,
                    shipping_amount=abs(group["components"]["Shipping"]),
                    external_ref=group_key,
                    warehouse_id=origin,
                    channel="printful",
                    attributes={
                        "vendor_name": "Printful Inc.",
                        "printful_id": group_key,
                        "order_labels": group["order_labels"],
                        "statuses": sorted(item for item in group["statuses"] if item),
                        "source_gross_amount": float(gross_amount),
                        "credit_magnitude": float(abs(gross_amount)),
                        "shipped_from": origin,
                        "shipped_to": sorted(item for item in group["shipping_destinations"] if item),
                    },
                    row_ref=f"csv:{group['line_nos'][0]}",
                )
                record["source_refs"] = [make_source_ref(source, row_ref=f"csv:{line_no}") for line_no in group["line_nos"]]
                result[category].append(record)
            continue

        vat_amount = group["components"]["VAT"]
        net_amount = sum(
            group["components"][column]
            for column in ("Products", "Discount", "Shipping", "Digitization", "Branding", "Fulfillment fees", "Tax")
        )
        if net_amount == 0 and gross_amount != 0:
            net_amount = gross_amount - vat_amount

        representative_label = next((label for label in group["order_labels"] if label), group_key)
        origin = next(iter(sorted(group["shipping_origins"])), None)
        attributes = {
            "printful_id": group_key,
            "order_labels": group["order_labels"],
            "statuses": sorted(item for item in group["statuses"] if item),
            "payment_instruments": sorted(item for item in group["payment_instruments"] if item),
            "shipped_from": origin,
            "shipped_to": sorted(item for item in group["shipping_destinations"] if item),
            "products_amount": float(group["components"]["Products"]),
            "discount_amount": float(group["components"]["Discount"]),
            "shipping_amount_observed": float(group["components"]["Shipping"]),
            "digitization_amount": float(group["components"]["Digitization"]),
            "branding_amount": float(group["components"]["Branding"]),
            "fulfillment_fee_amount_observed": float(group["components"]["Fulfillment fees"]),
            "tax_amount_observed": float(group["components"]["Tax"]),
            "refund_activity": len(group["statuses"]) > 1,
        }
        category, record = make_record(
            source=source,
            category="purchase_expenses",
            record_id=f"{source.source_id}:purchase:{slugify(group_key)}",
            event_type="printful_order_charge",
            event_date=max(group["dates"]),
            settlement_date=max(group["dates"]),
            description=f"Printful order charges for {representative_label}",
            currency=group["currency"],
            gross_amount=gross_amount,
            net_amount=net_amount,
            vat_amount=vat_amount,
            shipping_amount=group["components"]["Shipping"],
            external_ref=group_key,
            warehouse_id=origin,
            channel="printful",
            attributes=attributes,
            row_ref=f"csv:{group['line_nos'][0]}",
        )
        record["source_refs"] = [make_source_ref(source, row_ref=f"csv:{line_no}") for line_no in group["line_nos"]]
        result[category].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def parse_printful_wallet_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []

    for line_no, row in enumerate(rows, start=2):
        date_text = row.get("Date", "")
        normalized_date = normalize_ascii(date_text).lower()
        if normalized_date.startswith("total deposits to wallet") or normalized_date.startswith("total withdrawals from wallet"):  # noqa: PIE810
            continue

        event_date = parse_date_value(date_text)
        if event_date < period_start or event_date > period_end:
            continue

        amount, currency = parse_money_cell(row.get("Amount"), default_currency=base_currency)
        currency = currency or base_currency

        action = normalize_ascii(str(row.get("Action") or "")).lower()
        payment_instrument = str(row.get("Payment Instrument") or "").strip()
        if "deposit to wallet" in action:
            signed_amount = -abs(amount)
            event_type = "printful_wallet_deposit"
            description = f"Printful wallet funding via {payment_instrument or 'wallet'}"
        elif "withdrawal from wallet" in action:
            signed_amount = abs(amount)
            event_type = "printful_wallet_withdrawal"
            description = f"Printful wallet refund via {payment_instrument or 'wallet'}"
        else:
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:action:{line_no}",
                    severity="warn",
                    reason=f"Skipped Printful wallet row with unrecognized action {row.get('Action')!r}.",
                    blocking=False,
                    row_ref=f"csv:{line_no}",
                    suggested_follow_up="Review the Printful wallet export and extend the parser if this action type should affect bookkeeping.",
                )
            )
            continue

        seen_dates.append(event_date)
        category, record = make_record(
            source=source,
            category="clearing_transactions",
            record_id=f"{source.source_id}:wallet:{line_no}",
            event_type=event_type,
            event_date=event_date,
            settlement_date=event_date,
            description=description,
            currency=currency,
            gross_amount=signed_amount,
            net_amount=signed_amount,
            external_ref=f"{slugify(action)}:{line_no}",
            channel="printful",
            attributes={
                "action": row.get("Action"),
                "payment_instrument": payment_instrument,
                "clearing_provider": "printful",
                "clearing_account": "printful_wallet",
            },
            row_ref=f"csv:{line_no}",
        )
        result[category].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def parse_printful_other_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []

    for line_no, row in enumerate(rows, start=2):
        date_text = row.get("Date", "")
        if normalize_ascii(date_text).lower().startswith("total paid"):
            continue

        event_date = parse_date_value(date_text)
        if event_date < period_start or event_date > period_end:
            continue

        gross_amount, currency = parse_money_cell(row.get("Total"), default_currency=base_currency)
        currency = currency or base_currency

        amount, amount_currency = parse_money_cell(row.get("Amount"), default_currency=currency)
        discount, discount_currency = parse_money_cell(row.get("Discount"), default_currency=currency)
        tax_amount, tax_currency = parse_money_cell(row.get("Tax"), default_currency=currency)
        vat_amount, vat_currency = parse_money_cell(row.get("VAT"), default_currency=currency)
        if any(
            component_currency and component_currency != currency
            for component_currency in (amount_currency, discount_currency, tax_currency, vat_currency)
        ):
            raise SimplbooksError(f"Printful Other.csv row {line_no} mixes currencies in one record, which is unsupported.")

        seen_dates.append(event_date)
        category_name = str(row.get("Category") or "other service").strip()
        net_amount = amount + discount + tax_amount
        if net_amount == 0 and gross_amount != 0:
            net_amount = gross_amount - vat_amount
        category, record = make_record(
            source=source,
            category="purchase_expenses",
            record_id=f"{source.source_id}:other:{line_no}",
            event_type="printful_other_charge",
            event_date=event_date,
            settlement_date=event_date,
            description=f"Printful {category_name}",
            currency=currency,
            gross_amount=gross_amount,
            net_amount=net_amount,
            vat_amount=vat_amount,
            external_ref=f"{slugify(category_name)}:{event_date.isoformat()}",
            channel="printful",
            attributes={
                "category": category_name,
                "payment_instrument": row.get("Payment Instrument"),
                "status": row.get("Status"),
                "amount_observed": float(amount),
                "discount_amount": float(discount),
                "tax_amount_observed": float(tax_amount),
            },
            row_ref=f"csv:{line_no}",
        )
        result[category].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def parse_printful_services_csv(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    rows, _ = read_csv_rows(source.path)
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []

    for line_no, row in enumerate(rows, start=2):
        date_text = row.get("Date", "")
        if normalize_ascii(date_text).lower().startswith("total paid"):
            continue

        event_date = parse_date_value(date_text)
        if event_date < period_start or event_date > period_end:
            continue

        gross_amount, currency = parse_money_cell(row.get("Total"), default_currency=base_currency)
        currency = currency or base_currency

        seen_dates.append(event_date)
        action = str(row.get("Action") or "service").strip()
        category, record = make_record(
            source=source,
            category="purchase_expenses",
            record_id=f"{source.source_id}:service:{line_no}",
            event_type="printful_service_charge",
            event_date=event_date,
            settlement_date=event_date,
            description=f"Printful {action}",
            currency=currency,
            gross_amount=gross_amount,
            net_amount=gross_amount,
            external_ref=f"{slugify(action)}:{event_date.isoformat()}",
            channel="printful",
            attributes={
                "action": action,
                "payment_instrument": row.get("Payment Instrument"),
                "status": row.get("Status"),
            },
            row_ref=f"csv:{line_no}",
        )
        result[category].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def parse_camt_xml(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    tree = ElementTree.parse(source.path)
    root = tree.getroot()
    ns_match = re.match(r"\{(.+)\}", root.tag)
    ns = {"ns": ns_match.group(1)} if ns_match else {}

    result = parser_result()
    seen_dates: list[date] = []
    balance_keys: set[tuple[str, str, str, str]] = set()
    balance_index = 0
    entry_index = 0
    for statement in root.findall(".//ns:Stmt" if ns else ".//Stmt", ns):
        account = statement.find("ns:Acct" if ns else "Acct", ns)
        iban = ""
        account_currency = base_currency
        if account is not None:
            iban = (account.findtext("ns:Id/ns:IBAN" if ns else "Id/IBAN", default="", namespaces=ns) or "").strip().upper()
            account_currency = (account.findtext("ns:Ccy" if ns else "Ccy", default=base_currency, namespaces=ns) or base_currency).strip().upper()
        statement_from_text = statement.findtext(
            "ns:FrToDt/ns:FrDtTm" if ns else "FrToDt/FrDtTm",
            default="",
            namespaces=ns,
        )
        statement_to_text = statement.findtext(
            "ns:FrToDt/ns:ToDtTm" if ns else "FrToDt/ToDtTm",
            default="",
            namespaces=ns,
        )
        statement_from = parse_date_value(statement_from_text[:10]).isoformat() if statement_from_text else None
        statement_to = parse_date_value(statement_to_text[:10]).isoformat() if statement_to_text else None

        for balance in statement.findall("ns:Bal" if ns else "Bal", ns):
            balance_index += 1
            amount_node = balance.find("ns:Amt" if ns else "Amt", ns)
            amount = parse_decimal(amount_node.text if amount_node is not None else "0")
            currency = ((amount_node.get("Ccy") if amount_node is not None else None) or account_currency).upper()
            credit_debit = balance.findtext("ns:CdtDbtInd" if ns else "CdtDbtInd", default="", namespaces=ns).strip().upper()
            if credit_debit == "DBIT":
                amount = -abs(amount)
            elif credit_debit == "CRDT":
                amount = abs(amount)
            balance_type = balance.findtext(
                "ns:Tp/ns:CdOrPrtry/ns:Cd" if ns else "Tp/CdOrPrtry/Cd",
                default="",
                namespaces=ns,
            ).strip()
            if not balance_type:
                balance_type = balance.findtext("ns:Tp/ns:Cd" if ns else "Tp/Cd", default="other", namespaces=ns).strip() or "other"
            date_text = balance.findtext("ns:Dt/ns:Dt" if ns else "Dt/Dt", default="", namespaces=ns)
            if not date_text:
                date_text = balance.findtext("ns:Dt/ns:DtTm" if ns else "Dt/DtTm", default="", namespaces=ns)
            balance_date = parse_date_value(date_text)
            key = (iban, currency, balance_type, balance_date.isoformat())
            if key in balance_keys:
                continue
            balance_keys.add(key)
            category, record = make_record(
                source=source,
                category="bank_balances",
                record_id=f"{source.source_id}:camt-balance:{iban or 'unknown'}:{currency}:{balance_type}:{balance_date.isoformat()}",
                event_type="bank_balance",
                event_date=balance_date,
                description=f"CAMT {balance_type} balance for {iban or 'unknown account'}",
                currency=currency,
                gross_amount=amount,
                net_amount=amount,
                attributes={
                    "iban": iban or None,
                    "balance_type": balance_type,
                    "balance_date": balance_date.isoformat(),
                    "credit_debit_indicator": credit_debit or None,
                    "statement_from": statement_from,
                    "statement_to": statement_to,
                },
                row_ref=f"xml:balance:{balance_index}",
            )
            result[category].append(record)

        for entry in statement.findall("ns:Ntry" if ns else "Ntry", ns):
            entry_index += 1
            amount_text = entry.findtext("ns:Amt" if ns else "Amt", default="0", namespaces=ns)
            amount = parse_decimal(amount_text)
            credit_debit = entry.findtext("ns:CdtDbtInd" if ns else "CdtDbtInd", default="", namespaces=ns).strip().upper()
            if credit_debit == "DBIT":
                amount = -abs(amount)
            elif credit_debit == "CRDT":
                amount = abs(amount)

            date_text = entry.findtext("ns:BookgDt/ns:Dt" if ns else "BookgDt/Dt", default="", namespaces=ns)
            event_date = parse_date_value(date_text)
            if event_date < period_start or event_date > period_end:
                continue
            seen_dates.append(event_date)

            amount_node = entry.find("ns:Amt" if ns else "Amt", ns)
            currency = ((amount_node.get("Ccy") if amount_node is not None else None) or account_currency).upper()
            description = entry.findtext(
                "ns:NtryDtls/ns:TxDtls/ns:RmtInf/ns:Ustrd" if ns else "NtryDtls/TxDtls/RmtInf/Ustrd",
                default="CAMT bank transaction",
                namespaces=ns,
            )
            reference = entry.findtext(
                "ns:AcctSvcrRef" if ns else "AcctSvcrRef",
                default="",
                namespaces=ns,
            ) or entry.findtext(
                "ns:NtryDtls/ns:TxDtls/ns:Refs/ns:AcctSvcrRef" if ns else "NtryDtls/TxDtls/Refs/AcctSvcrRef",
                default="",
                namespaces=ns,
            )
            entry_reference = entry.findtext(
                "ns:NtryRef" if ns else "NtryRef",
                default="",
                namespaces=ns,
            )
            category, record = make_record(
                source=source,
                category="bank_transactions",
                record_id=f"{source.source_id}:camt:{entry_index}",
                event_type="bank_credit" if amount >= 0 else "bank_debit",
                event_date=event_date,
                description=description,
                currency=currency,
                gross_amount=amount,
                net_amount=amount,
                external_ref=reference or None,
                attributes={
                    "iban": iban or None,
                    "account_servicer_reference": reference or None,
                    "entry_reference": entry_reference or None,
                    "credit_debit_indicator": credit_debit,
                },
                row_ref=f"xml:{entry_index}",
            )
            result[category].append(record)

    update_coverage_from_dates(source, seen_dates)
    return result, []


def page_ref_containing(pages: list[str], needle: str | None) -> str:
    if needle:
        for index, page in enumerate(pages, start=1):
            if needle in page:
                return f"pdf:{index}"
    return "pdf:1"


def first_match(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def first_decimal_match(text: str, patterns: list[str]) -> Decimal | None:
    found = first_match(text, patterns)
    if found is None:
        return None
    return parse_currency_amount(found)


def parse_month_label_period(value: str) -> tuple[date, date]:
    text = re.sub(r"\s+", " ", value.strip())
    for fmt in ("%b %Y", "%B %Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()  # noqa: DTZ007
            return date(parsed.year, parsed.month, 1), month_end(parsed.year, parsed.month)
        except ValueError:
            continue
    raise SimplbooksError(f"Unsupported month label: {value!r}")


def vendor_name_from_text(text: str, *, fallback: str) -> str:
    provider = first_match(
        text,
        [
            r"Vedaja/Teenuse pakkuja:\s*([^;\n]+)",
            r"\n(AS Eesti Post)\n",
            r"\n(SimplBooks O[ÜU])\n(?=[^\n]*(?:Sõpruse|Sopruse|Reg-nr))",
        ],
    )
    if provider:
        return provider

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    skipped_prefixes = (
        "tax invoice",
        "vat report",
        "invoice date",
        "issue date",
        "kuupaev",
        "maksetahtaeg",
        "client",
        "kli",
        "bill to",
        "company:",
        "full name:",
        "address:",
        "vat id",
        "registration no",
        "reg. #:",
        "invoice #",
        "invoice number",
        "task description",
        "toode/teenus",
    )
    company_pattern = re.compile(r"\b(o[uü]|as|inc|limited|llc|ltd)\b", re.IGNORECASE)
    for line in lines[:20]:
        lowered = normalize_ascii(line).lower()
        if lowered.startswith(skipped_prefixes):
            continue
        if company_pattern.search(lowered) or "express" in lowered:
            return line
    for line in reversed(lines):
        lowered = normalize_ascii(line).lower()
        if lowered.startswith(skipped_prefixes):
            continue
        if company_pattern.search(lowered) or "express" in lowered:
            return line
    return fallback


def make_pdf_dependency_exception(source: SourceDescriptor, *, reason: str, suggested_follow_up: str) -> dict[str, Any]:
    return make_exception(
        source=source,
        exception_id=f"{source.source_id}:pdf",
        severity="error",
        reason=reason,
        blocking=True,
        page_ref="pdf:1",
        suggested_follow_up=suggested_follow_up,
    )


def has_overlapping_printful_structured_sources(
    current_source: SourceDescriptor,
    sources: list[SourceDescriptor] | None,
    *,
    period_start: date,
    period_end: date,
) -> bool:
    if not sources:
        return False
    for source in sources:
        if source is current_source or not source.canonical:
            continue
        if source.source_system != "printful" or source.source_type != "csv":
            continue
        if source.parser_name not in {
            "parse_printful_orders_csv",
            "parse_printful_other_csv",
            "parse_printful_services_csv",
        }:
            continue
        if source.overlaps(period_start, period_end):
            return True
    return False


def printful_other_charge_is_storage_like(category_name: str) -> bool:
    normalized = normalize_ascii(category_name).lower()
    return any(token in normalized for token in ("custom product keeping", "storage", "warehous"))


def overlapping_printful_other_storage_charge_keys(
    current_source: SourceDescriptor,
    sources: list[SourceDescriptor] | None,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> set[tuple[str, Decimal, Decimal]]:
    if not sources:
        return set()

    keys: set[tuple[str, Decimal, Decimal]] = set()
    for source in sources:
        if source is current_source or not source.canonical:
            continue
        if source.source_system != "printful" or source.source_type != "csv":
            continue
        if source.parser_name != "parse_printful_other_csv":
            continue
        if not source.overlaps(period_start, period_end):
            continue

        records, _ = parse_printful_other_csv(
            source,
            period_start=period_start,
            period_end=period_end,
            base_currency=base_currency,
        )
        for record in records["purchase_expenses"]:
            attributes = record.get("attributes") or {}
            if not printful_other_charge_is_storage_like(str(attributes.get("category") or "")):
                continue
            gross_amount = parse_decimal(record.get("gross_amount"))
            vat_amount = parse_decimal(record.get("vat_amount"))
            if gross_amount <= 0:
                continue
            keys.add((str(record.get("event_date") or "")[:7], gross_amount, vat_amount))
    return keys


def printful_pdf_filename_invoice_numbers(path: Path) -> set[str]:
    return set(re.findall(r"_(\d{8,10})(?=_|\.pdf)", normalize_ascii(path.name)))


def preferred_printful_pdf_source_for_invoice(
    invoice_number: str,
    *,
    billing_end: date,
    current_source: SourceDescriptor,
    sources: list[SourceDescriptor] | None,
) -> SourceDescriptor | None:
    if not sources:
        return current_source

    candidates = [
        source
        for source in sources
        if source.canonical
        and source.source_system == "printful"
        and source.source_type == "pdf"
        and invoice_number in printful_pdf_filename_invoice_numbers(source.path)
    ]
    if not candidates:
        return current_source

    return min(
        candidates,
        key=lambda source: (
            0 if source.covered_until == billing_end else 1,
            abs((source.covered_until - billing_end).days),
            abs((source.covered_from - billing_end).days),
            source.rel_path,
        ),
    )


def parse_stripe_invoice_pdf(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    result = parser_result()
    try:
        pages = extract_pdf_pages(source.path)
    except SimplbooksError as exc:
        return result, [
            make_pdf_dependency_exception(
                source,
                reason=str(exc),
                suggested_follow_up="Install pypdf in .venv or provide Stripe fee data in CSV form.",
            )
        ]

    full_text = "\n".join(page for page in pages if page.strip())
    if not full_text.strip():
        return result, [
            make_pdf_dependency_exception(
                source,
                reason="Stripe invoice PDF did not yield readable text.",
                suggested_follow_up="Provide the original text-based PDF or a structured export for Stripe fees.",
            )
        ]

    service_month = first_match(full_text, [r"Service Month\s+([A-Za-z]{3,9}\s+\d{4})"])
    invoice_number = first_match(full_text, [r"Invoice Number\s+([A-Z0-9-]+)"])
    invoice_date_text = first_match(full_text, [r"Invoice Date\s+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})"])
    fee_total = first_decimal_match(
        full_text,
        [
            r"Stripe Fees\s+€([0-9.,]+)",
            r"Total\s+€([0-9.,]+)",
        ],
    )
    if service_month is None or invoice_date_text is None or fee_total is None:
        return result, [
            make_pdf_dependency_exception(
                source,
                reason="Stripe invoice PDF is missing service-month, invoice-date, or total-fee fields.",
                suggested_follow_up="Review the Stripe PDF format or add a parser update for this invoice variant.",
            )
        ]

    scope_start, scope_end = parse_month_label_period(service_month)
    if not periods_overlap(scope_start, scope_end, period_start, period_end):
        return result, []

    invoice_date = parse_date_value(invoice_date_text)
    charge_total = first_decimal_match(full_text, [r"card payments? totaling €([0-9.,]+)"])
    payment_count_text = first_match(full_text, [r"(\d+)\s+card payments? totaling"])
    payment_count = int(payment_count_text) if payment_count_text else None

    category, record = make_record(
        source=source,
        category="fees",
        record_id=f"{source.source_id}:fees:{invoice_number or scope_end.isoformat()}",
        event_type="stripe_processing_fee_invoice",
        event_date=scope_end,
        settlement_date=invoice_date,
        description=f"Stripe processing fees for {service_month}",
        currency=base_currency,
        gross_amount=fee_total,
        net_amount=fee_total,
        fee_amount=fee_total,
        external_ref=invoice_number,
        channel="stripe",
        attributes={
            "invoice_number": invoice_number,
            "invoice_date": invoice_date.isoformat(),
            "service_month": service_month,
            "charge_total": float(charge_total) if charge_total is not None else None,
            "payment_count": payment_count,
            "vendor_name": "Stripe Payments Europe, Limited",
        },
        page_ref=page_ref_containing(pages, invoice_number),
    )
    result[category].append(record)
    update_coverage_from_dates(source, [scope_end])
    return result, []


def parse_quartermaster_pdf(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    try:
        pages = extract_pdf_pages(source.path)
    except SimplbooksError as exc:
        return result, [
            make_pdf_dependency_exception(
                source,
                reason=str(exc),
                suggested_follow_up="Install pypdf in .venv or provide Quartermaster reports in a structured export.",
            )
        ]

    full_text = "\n".join(page for page in pages if page.strip())
    if not full_text.strip():
        return result, [
            make_pdf_dependency_exception(
                source,
                reason="Quartermaster PDF did not yield readable text.",
                suggested_follow_up="Provide the original text-based PDF or an OCR/structured copy of the Quartermaster document.",
            )
        ]

    normalized = normalize_ascii(full_text).lower()

    if "sales report" in normalized and "quartermaster direct" in normalized:
        report_date_text = first_match(full_text, [r"Date\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})"])
        report_number = first_match(full_text, [r"S\.R\.\s*No\.\s*([A-Z0-9-]+)"])
        report_label = first_match(full_text, [r"This represents your sales report for ([A-Za-z]+\s+\d{4})"])
        sold_matches = re.findall(
            r"Sold\s+Copies\s+(\d+)\s+([0-9.,]+)\s+([0-9.,]+)",
            full_text,
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        fee_match = re.search(
            r"Picking Fee.*?(\d+)\s+-?([0-9.,]+)\s+-([0-9.,]+)",
            full_text,
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        report_total_matches = re.findall(r"\$\s*([0-9,]+\.\d{2})", full_text)
        if report_date_text is not None and "no sales for this pay period" in normalized:
            report_date = parse_quartermaster_date_value(report_date_text)
            if period_start <= report_date <= period_end:
                update_coverage_from_dates(source, [report_date])
            return result, []
        if report_date_text is None or not sold_matches or fee_match is None:
            return result, [
                make_pdf_dependency_exception(
                    source,
                    reason="Quartermaster sales report PDF is missing a readable report date, sales total, or picking-fee line.",
                    suggested_follow_up="Review the Quartermaster sales report layout or add another parser variant.",
                )
            ]

        report_date = parse_quartermaster_date_value(report_date_text)
        if report_date < period_start or report_date > period_end:
            return result, []

        sold_quantity = sum((parse_decimal(match[0]) for match in sold_matches), Decimal("0"))  # noqa: FURB157
        sold_amount = sum((parse_decimal(match[2]) for match in sold_matches), Decimal("0"))  # noqa: FURB157
        sold_rate = sold_amount / sold_quantity if sold_quantity else Decimal("0")  # noqa: FURB157
        fee_quantity = parse_decimal(fee_match.group(1))
        fee_rate = abs(parse_decimal(fee_match.group(2)))
        fee_amount = abs(parse_decimal(fee_match.group(3)))
        report_total = parse_decimal(report_total_matches[-1]) if report_total_matches else None
        report_scope = report_label or source_period_label(date(report_date.year, report_date.month, 1))

        category, sales_record = make_record(
            source=source,
            category="sales",
            record_id=f"{source.source_id}:sales:{report_number or report_date.isoformat()}",
            event_type="quartermaster_sales_report",
            event_date=report_date,
            description=f"Quartermaster sales report for {report_scope}",
            currency="USD",
            gross_amount=sold_amount,
            net_amount=sold_amount,
            external_ref=report_number,
            quantity=sold_quantity,
            channel="quartermaster",
            attributes={
                "report_number": report_number,
                "vendor_name": "Quartermaster Direct",
                "unit_rate": float(sold_rate),
                "report_scope": report_scope,
                "report_total": float(report_total) if report_total is not None else None,
            },
            page_ref=page_ref_containing(pages, report_number),
        )
        result[category].append(sales_record)

        category, fee_record = make_record(
            source=source,
            category="fees",
            record_id=f"{source.source_id}:fees:{report_number or report_date.isoformat()}",
            event_type="quartermaster_picking_fee",
            event_date=report_date,
            description=f"Quartermaster picking fees for {report_scope}",
            currency="USD",
            gross_amount=fee_amount,
            net_amount=fee_amount,
            fee_amount=fee_amount,
            external_ref=report_number,
            quantity=fee_quantity,
            channel="quartermaster",
            attributes={
                "report_number": report_number,
                "vendor_name": "Quartermaster Direct",
                "unit_rate": float(fee_rate),
                "report_scope": report_scope,
            },
            page_ref=page_ref_containing(pages, "Picking Fee"),
        )
        result[category].append(fee_record)

        if report_total is not None and abs((sold_amount - fee_amount) - report_total) > Decimal("0.01"):
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:sales-report-total",
                    severity="warn",
                    reason=(
                        f"Quartermaster sales report total {report_total} USD did not match "
                        f"sold copies minus picking fees ({sold_amount - fee_amount} USD)."
                    ),
                    blocking=False,
                    page_ref="pdf:1",
                    suggested_follow_up="Review whether the Quartermaster report contains additional lines that should be normalized.",
                )
            )

        update_coverage_from_dates(source, [report_date])
        return result, exceptions

    if "invoice" in normalized and "quartermaster logistics" in normalized:
        invoice_date_text = first_match(full_text, [r"Invoice Date\s+([0-9]{2}/[0-9]{2}/[0-9]{4})"])
        invoice_number = first_match(full_text, [r"Invoice Number\s+([A-Z0-9-]+)"])
        gross_amount_text = first_match(full_text, [r"Invoice Total\s+\$\s*([0-9.,]+)"])
        notes_text = first_match(full_text, [r"Notes\s+([^\n]+)"])
        due_date_text = first_match(full_text, [r"Invoice Due Date\s+([0-9]{2}/[0-9]{2}/[0-9]{4})"])
        line_labels = re.findall(r"\n([A-Za-z][A-Za-z ]+)\s+Debit\s+\$\s*[0-9.,]+", full_text)

        if invoice_date_text is None or gross_amount_text is None:
            return result, [
                make_pdf_dependency_exception(
                    source,
                    reason="Quartermaster invoice PDF is missing a readable invoice date or invoice total.",
                    suggested_follow_up="Review the Quartermaster invoice layout or add another parser variant.",
                )
            ]

        invoice_date = parse_quartermaster_date_value(invoice_date_text)
        if invoice_date < period_start or invoice_date > period_end:
            return result, []

        gross_amount = parse_decimal(gross_amount_text)
        description_bits = [notes_text] if notes_text else []
        if line_labels:
            description_bits.append(", ".join(label.strip() for label in line_labels))
        description = " - ".join(bit for bit in description_bits if bit) or f"Quartermaster invoice {invoice_number or source.path.stem}"

        category, record = make_record(
            source=source,
            category="purchase_expenses",
            record_id=f"{source.source_id}:purchase:{slugify(invoice_number or invoice_date.isoformat())}",
            event_type="quartermaster_service_invoice",
            event_date=invoice_date,
            settlement_date=parse_quartermaster_date_value(due_date_text) if due_date_text else None,
            description=description,
            currency="USD",
            gross_amount=gross_amount,
            net_amount=gross_amount,
            external_ref=invoice_number,
            channel="quartermaster",
            attributes={
                "invoice_number": invoice_number,
                "invoice_due_date": parse_quartermaster_date_value(due_date_text).isoformat() if due_date_text else None,
                "vendor_name": "Quartermaster Logistics LLC",
                "notes": notes_text,
                "service_labels": line_labels,
            },
            page_ref=page_ref_containing(pages, invoice_number),
        )
        result[category].append(record)
        update_coverage_from_dates(source, [invoice_date])
        return result, []

    return result, [
        make_pdf_dependency_exception(
            source,
            reason="Quartermaster PDF did not match a supported sales-report or supplier-invoice layout.",
            suggested_follow_up="Review the PDF layout and extend the Quartermaster parser for this document type.",
        )
    ]


def parse_printful_pdf(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
    sources: list[SourceDescriptor] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    result = parser_result()
    exceptions: list[dict[str, Any]] = []
    seen_dates: list[date] = []
    suppressed_by_overlap = False
    structured_billing_present = has_overlapping_printful_structured_sources(
        source,
        sources,
        period_start=period_start,
        period_end=period_end,
    )
    overlapping_other_storage_keys = overlapping_printful_other_storage_charge_keys(
        source,
        sources,
        period_start=period_start,
        period_end=period_end,
        base_currency=base_currency,
    )
    try:
        pages = extract_pdf_pages(source.path)
    except SimplbooksError as exc:
        return result, [
            make_pdf_dependency_exception(
                source,
                reason=str(exc),
                suggested_follow_up="Install pypdf in .venv or provide Printful invoices in a structured export.",
            )
        ]

    full_text = "\n".join(page for page in pages if page.strip())
    if not full_text.strip():
        return result, [
            make_pdf_dependency_exception(
                source,
                reason="Printful PDF did not yield readable text.",
                suggested_follow_up="Provide the original text-based PDF or an exported Printful billing report.",
            )
        ]

    report_period = re.search(
        r"Invoice period:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\s*-\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    report_invoice_date_text = first_match(full_text, [r"Invoice date:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})"])
    report_invoice_ref = first_match(full_text, [r"Invoice:\s*#([A-Z0-9-]+)"])
    if report_period and report_invoice_date_text and not structured_billing_present:
        report_start = parse_date_value(report_period.group(1))
        report_end = parse_date_value(report_period.group(2))
        if periods_overlap(report_start, report_end, period_start, period_end):
            summary_text = full_text.split("Please find invoice details", 1)[0]
            grand_totals = [parse_currency_amount(value) for value in re.findall(r"Grand total\s+€([0-9.,]+)", summary_text)]
            if grand_totals:
                total_amount = sum(grand_totals, Decimal("0"))  # noqa: FURB157
                report_invoice_date = parse_date_value(report_invoice_date_text)
                category, record = make_record(
                    source=source,
                    category="purchase_expenses",
                    record_id=f"{source.source_id}:purchase:{report_invoice_ref or report_end.isoformat()}",
                    event_type="printful_monthly_report",
                    event_date=report_end,
                    settlement_date=report_invoice_date,
                    description=f"Printful monthly service summary for {source_period_label(report_start)}",
                    currency=base_currency,
                    gross_amount=total_amount,
                    net_amount=total_amount,
                    external_ref=report_invoice_ref,
                    channel="printful",
                    attributes={
                        "invoice_number": report_invoice_ref,
                        "invoice_date": report_invoice_date.isoformat(),
                        "invoice_period_from": report_start.isoformat(),
                        "invoice_period_until": report_end.isoformat(),
                        "vendor_name": "Printful Inc.",
                    },
                    page_ref="pdf:1",
                )
                result[category].append(record)
                seen_dates.append(report_end)

    invoice_sections = re.finditer(
        r"Invoice #(\d+)\s+Printful Inc\.(.*?)(?=Invoice #\d+\s+Printful Inc\.|\Z)",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in invoice_sections:
        invoice_no = match.group(1)
        body = match.group(2)
        billing_period = re.search(
            r"Billing period:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\s*-\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
            body,
            flags=re.IGNORECASE,
        )
        invoice_date_text = first_match(body, [r"Invoice date:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})"])
        total_amount = first_decimal_match(body, [r"Total amount\s+€([0-9.,]+)"])
        detail_match = re.search(
            r"€([0-9.,]+)\s+\d+%\s*[A-Za-z]*\s*€([0-9.,]+)\s+€([0-9.,]+)",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not billing_period or invoice_date_text is None or total_amount is None:
            continue

        billing_start = parse_date_value(billing_period.group(1))
        billing_end = parse_date_value(billing_period.group(2))
        if not periods_overlap(billing_start, billing_end, period_start, period_end):
            continue
        preferred_source = preferred_printful_pdf_source_for_invoice(
            invoice_no,
            billing_end=billing_end,
            current_source=source,
            sources=sources,
        )
        if preferred_source is not None and preferred_source.source_id != source.source_id:
            suppressed_by_overlap = True
            continue

        net_amount = total_amount
        vat_amount = Decimal("0")  # noqa: FURB157
        if detail_match:
            net_amount = parse_currency_amount(detail_match.group(1))
            vat_amount = parse_currency_amount(detail_match.group(2))
            total_amount = parse_currency_amount(detail_match.group(3))
        month_key = billing_end.strftime("%Y-%m")
        if (month_key, total_amount, vat_amount) in overlapping_other_storage_keys:
            suppressed_by_overlap = True
            continue
        invoice_date = parse_date_value(invoice_date_text)
        category, record = make_record(
            source=source,
            category="purchase_expenses",
            record_id=f"{source.source_id}:storage:{invoice_no}",
            event_type="printful_storage_invoice",
            event_date=billing_end,
            settlement_date=invoice_date,
            description=f"Printful storage invoice #{invoice_no}",
            currency=base_currency,
            gross_amount=total_amount,
            net_amount=net_amount,
            vat_amount=vat_amount,
            external_ref=invoice_no,
            channel="printful",
            attributes={
                "invoice_number": invoice_no,
                "invoice_date": invoice_date.isoformat(),
                "billing_period_from": billing_start.isoformat(),
                "billing_period_until": billing_end.isoformat(),
                "vendor_name": "Printful Inc.",
            },
            page_ref=page_ref_containing(pages, f"Invoice #{invoice_no}"),
        )
        result[category].append(record)
        seen_dates.append(billing_end)

    if not any(result.values()) and not structured_billing_present and not suppressed_by_overlap:
        exceptions.append(
            make_pdf_dependency_exception(
                source,
                reason="Printful PDF did not produce any in-period service or invoice records.",
                suggested_follow_up="Review whether the Printful PDF covers this month or whether the parser needs another layout variant.",
            )
        )
    update_coverage_from_dates(source, seen_dates)
    return result, exceptions


def parse_purchase_invoice_pdf(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    result = parser_result()
    try:
        pages = extract_pdf_pages(source.path)
    except SimplbooksError as exc:
        return result, [
            make_pdf_dependency_exception(
                source,
                reason=str(exc),
                suggested_follow_up="Install pypdf in .venv or provide a structured export for this purchase document.",
            )
        ]

    full_text = "\n".join(page for page in pages if page.strip())
    if not full_text.strip():
        return result, [
            make_pdf_dependency_exception(
                source,
                reason="Purchase-invoice PDF did not yield readable text.",
                suggested_follow_up="Provide the original text-based PDF or an OCR/structured copy of the invoice.",
            )
        ]

    invoice_ref = first_match(
        full_text,
        [
            r"Invoice Number\s+([A-Z0-9-]+)",
            r"Invoice #([A-Z0-9-]+)",
            r"([A-Z0-9-]+)\s+Arve nr\.?:",
            r"Arve number\s+([A-Z0-9-]+)",
            r"Arve nr\s+([A-Z0-9-]+)",
            r"Arve\s+([A-Z0-9-]+)\s+Arve kuupaev",
            r"Arve\s+([A-Z0-9-]+)\s+Arve kuup[aä]ev",
            r"Pileti nr\s+([A-Z0-9-]+)",
        ],
    )
    event_date_text = first_match(
        full_text,
        [
            r"Invoice date:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
            r"Issue date:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
            r"Arve kuupaev:?\s*([0-9.]+)",
            r"Arve kuup[aä]ev:?\s*([0-9.]+)",
            r"Kuupäev\s+([0-9.]+)",
            r"Maksja:\s*[^\n]+\s+([0-9]{2}\.[0-9]{2}\.[0-9]{4})",
            r"Ostetud:\s*([0-9.]+\s+[0-9:]+)",
        ],
    )
    if event_date_text is None:
        return result, [
            make_pdf_dependency_exception(
                source,
                reason="Purchase-invoice PDF is missing a readable document date.",
                suggested_follow_up="Review the PDF layout or provide a structured purchase export.",
            )
        ]
    event_date = parse_date_value(event_date_text)
    if event_date < period_start or event_date > period_end:
        return result, []

    balance_summary_match = re.search(
        r"Euroopa Keskpanga valuutakursid:\s*([0-9.,]+)\s*EUR\s+([0-9.,]+)\s*EUR\s+([0-9.,]+)\s*EUR",
        full_text,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    omniva_summary_match = re.search(
        r"Ridade summa\s+K[aä]ibemaks\s+[UÜ]mardus\s+Kokku\s+EUR\s+([0-9.,]+)\s+([0-9.,]+)\s+([0-9.,]+)\s+([0-9.,]+)",
        full_text,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    dpd_summary_match = re.search(
        r"Summa\s+([0-9.,]+)\s*EUR\s+[0-9.,]+\s*EUR\s+[0-9.,]+\s*EUR\s+([0-9.,]+)\s*EUR\s+([0-9.,]+)\s*EUR",
        full_text,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    jajaa_summary_match = re.search(
        r"KM-ta\s+([0-9.,]+)\s+\d+%\s+KM%\s+KMsumma\s+([0-9.,]+)\s+Kokku\s+([0-9.,]+)",
        full_text,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )

    gross_amount = first_decimal_match(
        full_text,
        [
            r"Maksmisele kuuluv summa\s+([0-9.,]+)",
            r"Arve kokku \(EUR\)\s+([0-9.,]+)",
            r"TOTAL TO BE PAID:\s*€\s*([0-9.,]+)",
            r"Total amount\s+€([0-9.,]+)",
            r"Kokku:\s*([0-9.,]+)\s*EUR",
            r"Kokku tasuda:\s*([0-9.,]+)\s*EUR",
            r"Kogusumma KM-ga\s+[0-9.,]+\s+[0-9.,]+\s*EUR\s+[0-9.,]+\s*EUR\s+[0-9.,]+\s*EUR\s+[0-9.,]+\s*EUR\s+([0-9.,]+)\s*EUR",
        ],
    )
    if gross_amount is None and balance_summary_match:
        gross_amount = parse_currency_amount(balance_summary_match.group(3))
    if gross_amount is None and omniva_summary_match:
        gross_amount = parse_currency_amount(omniva_summary_match.group(4))
    if gross_amount is None and dpd_summary_match:
        gross_amount = parse_currency_amount(dpd_summary_match.group(3))
    if gross_amount is None and jajaa_summary_match:
        gross_amount = parse_currency_amount(jajaa_summary_match.group(3))
    if gross_amount is None:
        return result, [
            make_pdf_dependency_exception(
                source,
                reason="Purchase-invoice PDF is missing a readable gross total.",
                suggested_follow_up="Review the PDF layout or add a parser variant for this supplier document.",
            )
        ]

    vat_amount = first_decimal_match(
        full_text,
        [
            r"KM \(\d+%\):\s*([0-9.,]+)\s*EUR",
            r"VAT \([0-9.%]+\):\s*€\s*([0-9.,]+)",
            r"KM \d+%\s+([0-9.,]+)",
        ],
    ) or Decimal("0")  # noqa: FURB157
    if vat_amount == Decimal("0") and balance_summary_match:  # noqa: FURB157
        vat_amount = parse_currency_amount(balance_summary_match.group(2))
    if vat_amount == Decimal("0") and omniva_summary_match:  # noqa: FURB157
        vat_amount = parse_currency_amount(omniva_summary_match.group(2))
    if vat_amount == Decimal("0") and dpd_summary_match:  # noqa: FURB157
        vat_amount = parse_currency_amount(dpd_summary_match.group(2))
    if vat_amount == Decimal("0") and jajaa_summary_match:  # noqa: FURB157
        vat_amount = parse_currency_amount(jajaa_summary_match.group(2))

    net_amount = first_decimal_match(
        full_text,
        [
            r"SUBTOTAL:\s*€\s*([0-9.,]+)",
            r"Summa km-ta \d+%\s+([0-9.,]+)",
            r"Pileti\(te\) hind .*?:\s*([0-9.,]+)\s*EUR",
        ],
    ) or (gross_amount - vat_amount)
    if balance_summary_match:
        net_amount = parse_currency_amount(balance_summary_match.group(1))
    elif omniva_summary_match:
        net_amount = parse_currency_amount(omniva_summary_match.group(1))
    elif dpd_summary_match:
        net_amount = parse_currency_amount(dpd_summary_match.group(1))
    elif jajaa_summary_match:
        net_amount = parse_currency_amount(jajaa_summary_match.group(1))

    fallback_vendor = normalize_ascii(source.path.stem).strip() or source.source_system or "supplier"
    vendor_name = vendor_name_from_text(full_text, fallback=fallback_vendor)
    description = first_match(
        full_text,
        [
            r"Reisi kokkuvõte:\s*([^\n]+)",
            r"TASK DESCRIPTION LINE TOTAL\s*([^\n]+)",
            r"Toode/Teenus[^\n]*\n([^\n]+)",
        ],
    ) or f"{vendor_name} invoice {invoice_ref or source.path.stem}"

    category, record = make_record(
        source=source,
        category="purchase_expenses",
        record_id=f"{source.source_id}:purchase:{slugify(invoice_ref or vendor_name)}",
        event_type="purchase_invoice_pdf",
        event_date=event_date,
        description=description,
        currency=base_currency,
        gross_amount=gross_amount,
        net_amount=net_amount,
        vat_amount=vat_amount,
        external_ref=invoice_ref,
        channel=slugify(vendor_name),
        attributes={
            "invoice_number": invoice_ref,
            "vendor_name": vendor_name,
        },
        page_ref=page_ref_containing(pages, invoice_ref),
    )
    result[category].append(record)
    update_coverage_from_dates(source, [event_date])
    return result, []


def parse_purchase_note_markdown(
    source: SourceDescriptor,
    *,
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    result = parser_result()
    context = source.context
    event_date = parse_date_value(str(context.get("event_date") or ""))
    if event_date < period_start or event_date > period_end:
        return result, []

    label = str(context.get("label") or "").strip() or source.path.stem
    note_body = str(context.get("body") or "").strip()
    vendor_name = label.split(",", 1)[0].strip()
    gross_amount = parse_decimal(context.get("gross_amount"))
    net_amount = parse_decimal(context.get("net_amount"))
    vat_amount = parse_decimal(context.get("vat_amount"))
    target_source_id = str(context.get("target_source_id") or "")
    payer_match = re.search(r"\bpaid\s+by\s+(.+)$", label, flags=re.IGNORECASE)
    expense_report_payee = payer_match.group(1).strip() if payer_match else None
    category_name = "manual_adjustments" if expense_report_payee else "purchase_expenses"

    category, record = make_record(
        source=source,
        category=category_name,
        record_id=f"{source.source_id}:{'expense-report' if expense_report_payee else 'purchase'}:{slugify(target_source_id or vendor_name)}",
        event_type="expense_report_evidence" if expense_report_payee else "purchase_note_markdown",
        event_date=event_date,
        description=note_body or f"{vendor_name} manual purchase note",
        currency=base_currency,
        gross_amount=gross_amount,
        net_amount=net_amount,
        vat_amount=vat_amount,
        external_ref=target_source_id or None,
        channel=slugify(vendor_name),
        attributes={
            "invoice_number": None,
            "vendor_name": vendor_name,
            "manual_note": True,
            "reverse_charge": bool(context.get("reverse_charge")),
            "target_source_id": target_source_id or None,
            "target_path": context.get("target_path"),
            "expense_report_payee": expense_report_payee,
        },
    )
    result[category].append(record)
    update_coverage_from_dates(source, [event_date])
    return result, []


PARSERS: dict[str, Callable[..., tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]]] = {
    "parse_no_activity_marker": parse_no_activity_marker,
    "parse_woo_sales_csv": parse_woo_sales_csv,
    "parse_woo_order_summary_csv": parse_woo_order_summary_csv,
    "parse_woo_tax_summary_csv": parse_woo_tax_summary_csv,
    "parse_paypal_csv": parse_paypal_csv,
    "parse_stripe_payouts_csv": parse_stripe_payouts_csv,
    "parse_stripe_balance_csv": parse_stripe_balance_csv,
    "parse_quartermaster_orders_csv": parse_quartermaster_orders_csv,
    "parse_printful_orders_csv": parse_printful_orders_csv,
    "parse_printful_wallet_csv": parse_printful_wallet_csv,
    "parse_printful_other_csv": parse_printful_other_csv,
    "parse_printful_services_csv": parse_printful_services_csv,
    "parse_bank_csv": parse_bank_csv,
    "parse_camt_xml": parse_camt_xml,
    "parse_stripe_invoice_pdf": parse_stripe_invoice_pdf,
    "parse_quartermaster_pdf": parse_quartermaster_pdf,
    "parse_printful_pdf": parse_printful_pdf,
    "parse_purchase_invoice_pdf": parse_purchase_invoice_pdf,
    "parse_purchase_note_markdown": parse_purchase_note_markdown,
}


def inspect_sources(
    *,
    source_dir: Path,
    root_dir: Path,
    period_start: date,
    period_end: date,
) -> list[SourceDescriptor]:
    sources: list[SourceDescriptor] = []
    for path in sorted(source_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".md":
            sources.extend(
                inspect_purchase_note_markdown(
                    path=path,
                    root_dir=root_dir,
                    period_start=period_start,
                    period_end=period_end,
                )
            )
            continue
        descriptor = inspect_source_file(
            path=path,
            root_dir=root_dir,
            period_start=period_start,
            period_end=period_end,
        )
        if descriptor:
            sources.append(descriptor)
    return choose_canonical_sources(sources)


def missing_canonical_source_exceptions(sources: list[SourceDescriptor]) -> list[dict[str, Any]]:
    grouped: dict[str, list[SourceDescriptor]] = {}
    for source in sources:
        grouped.setdefault(source.canonical_group, []).append(source)

    exceptions: list[dict[str, Any]] = []
    for entries in grouped.values():
        if any(entry.canonical for entry in entries):
            continue
        first = entries[0]
        source_types = ", ".join(sorted({entry.source_type for entry in entries}))
        exceptions.append(
            make_exception(
                source=first,
                exception_id=f"{first.canonical_group}:no-canonical-source",
                severity="error",
                reason=f"No canonical machine-readable source was available for source group {first.canonical_group}; seen types: {source_types}.",
                blocking=True,
                suggested_follow_up="Add a CSV, XML, or other deterministic export for this source group before relying on month normalization.",
            )
        )
    return exceptions


def physical_bank_ledger(record: dict[str, Any]) -> tuple[str, str] | None:
    attributes = record.get("attributes") or {}
    iban = str(attributes.get("iban") or attributes.get("customer_account") or "").strip()
    currency = str(record.get("currency") or "").strip().upper()
    if not iban or not currency:
        return None
    return re.sub(r"\s+", "", iban).upper(), currency


def physical_bank_references(record: dict[str, Any]) -> set[str]:
    attributes = record.get("attributes") or {}
    return {
        str(value).strip()
        for value in (
            record.get("external_ref"),
            attributes.get("archive_identifier"),
            attributes.get("account_servicer_reference"),
            attributes.get("entry_reference"),
        )
        if str(value or "").strip()
    }


def sources_overlap(left: SourceDescriptor, right: SourceDescriptor) -> bool:
    return not (left.covered_until < right.covered_from or right.covered_until < left.covered_from)


def canonical_csv_records_for_camt_record(
    *,
    source: SourceDescriptor,
    camt_record: dict[str, Any],
    existing_bank_records: list[dict[str, Any]],
    source_by_id: dict[str, SourceDescriptor],
    canonical_csv_source_ids: set[str],
) -> list[dict[str, Any]]:
    csv_records = [
        record
        for record in existing_bank_records
        if any(ref.get("source_id") in canonical_csv_source_ids for ref in record.get("source_refs", []))
    ]
    camt_ledger = physical_bank_ledger(camt_record)
    if camt_ledger is None:
        return []
    matching_csv_records = [
        record
        for record in csv_records
        if physical_bank_ledger(record) == camt_ledger
    ]
    if not matching_csv_records:
        return []
    matching_csv_sources = {
        ref.get("source_id")
        for record in matching_csv_records
        for ref in record.get("source_refs", [])
        if ref.get("source_id") in canonical_csv_source_ids
    }
    if not any(sources_overlap(source, source_by_id[source_id]) for source_id in matching_csv_sources):
        return []
    return matching_csv_records


def aggregate_results(
    *,
    sources: list[SourceDescriptor],
    period_start: date,
    period_end: date,
    base_currency: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    records = parser_result()
    exceptions: list[dict[str, Any]] = missing_canonical_source_exceptions(sources)
    source_by_id = {source.source_id: source for source in sources}
    canonical_csv_source_ids = {
        source.source_id
        for source in sources
        if source.canonical and source.parser_name == "parse_bank_csv"
    }

    for source in sorted(sources, key=lambda candidate: (candidate.parser_name == "parse_camt_xml", candidate.rel_path)):
        if not source.canonical and source.parser_name != "parse_camt_xml":
            continue
        parser = PARSERS.get(source.parser_name)
        if parser is None:
            blocking = source.source_type in {"pdf", "xlsx", "json", "other"}
            exceptions.append(
                make_exception(
                    source=source,
                    exception_id=f"{source.source_id}:unsupported",
                    severity="error" if blocking else "warn",
                    reason=f"Canonical source uses unsupported parser path {source.parser_name}.",
                    blocking=blocking,
                    suggested_follow_up="Provide a structured export or add a deterministic parser for this source type.",
                )
            )
            continue

        if source.parser_name == "parse_printful_pdf":
            parsed_records, parsed_exceptions = parser(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency=base_currency,
                sources=sources,
            )
        else:
            parsed_records, parsed_exceptions = parser(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency=base_currency,
            )
        if source.parser_name == "parse_camt_xml":
            supporting_records: list[dict[str, Any]] = []
            for record in parsed_records["bank_transactions"]:
                matching_csv_records = canonical_csv_records_for_camt_record(
                    source=source,
                    camt_record=record,
                    existing_bank_records=records["bank_transactions"],
                    source_by_id=source_by_id,
                    canonical_csv_source_ids=canonical_csv_source_ids,
                )
                camt_references = physical_bank_references(record)
                csv_references = set().union(
                    *(physical_bank_references(csv_record) for csv_record in matching_csv_records)
                )
                if matching_csv_records and (not camt_references or camt_references & csv_references):
                    supporting_records.append(record)
                    continue
                if matching_csv_records and camt_references:
                    ledger = physical_bank_ledger(record)
                    ledger_label = f"{ledger[0]}/{ledger[1]}" if ledger else "unknown ledger"
                    references = ", ".join(sorted(camt_references))
                    exceptions.append(
                        make_exception(
                            source=source,
                            exception_id=f"{record['record_id']}:duplicate-risk",
                            severity="error",
                            reason=(
                                f"CAMT record {record['record_id']} with AcctSvcrRef {references} overlaps "
                                f"canonical CSV ledger {ledger_label} but does not match its immutable references."
                            ),
                            blocking=True,
                            row_ref=(record.get("source_refs") or [{}])[0].get("row_ref"),
                            suggested_follow_up="Review the paired bank CSV and CAMT statement before assigning this potential duplicate or missing physical-bank row.",
                        )
                    )
            if supporting_records:
                supporting_ids = {id(record) for record in supporting_records}
                records["bank_transactions"].extend(
                    record for record in parsed_records["bank_transactions"] if id(record) not in supporting_ids
                )
                records["bank_balances"].extend(parsed_records["bank_balances"])
                source.parser_notes.append(
                    "CAMT transaction rows with matching canonical CSV ledger evidence were retained only for immutable-reference cross-checking; CAMT balances were kept as supporting evidence."
                )
            else:
                for category, values in parsed_records.items():
                    records[category].extend(values)
        elif source.canonical:
            for category, values in parsed_records.items():
                records[category].extend(values)
        exceptions.extend(parsed_exceptions)

    for category in ("sales", "refunds"):
        charges_records = [
            record
            for record in records[category]
            if record.get("attributes", {}).get("stripe_export_type") == "charges"
        ]
        balance_records = [
            record
            for record in records[category]
            if record.get("attributes", {}).get("stripe_export_type") == "balance_history"
        ]
        if not charges_records or not balance_records:
            continue

        def stripe_immutable_identities(record: dict[str, Any]) -> set[tuple[str, str]]:
            attributes = record.get("attributes", {})
            identities: set[tuple[str, str]] = set()
            for value in (
                attributes.get("stripe_balance_transaction_id"),
                attributes.get("stripe_source_id"),
                record.get("external_ref"),
            ):
                normalized = str(value or "").strip()
                if normalized.startswith("ch_"):
                    identities.add(("charge", normalized))
                elif normalized.startswith("pi_"):
                    identities.add(("payment_intent", normalized))
            return identities

        charge_identities = [stripe_immutable_identities(record) for record in charges_records]
        charge_signatures = {
            (record.get("event_date"), abs(float(record.get("gross_amount") or 0)), record.get("currency"))
            for record in charges_records
        }
        charge_order_ids = {
            str(record.get("attributes", {}).get("order_id") or "").strip()
            for record in charges_records
            if str(record.get("attributes", {}).get("order_id") or "").strip()
        }
        superseded_records = []
        possible_duplicate_records = []
        for record in balance_records:
            identities = stripe_immutable_identities(record)
            signature = (record.get("event_date"), abs(float(record.get("gross_amount") or 0)), record.get("currency"))
            order_id = str(record.get("attributes", {}).get("order_id") or "").strip()
            if any(identities & candidate for candidate in charge_identities):
                superseded_records.append(record)
            elif signature in charge_signatures or (order_id and order_id in charge_order_ids):
                possible_duplicate_records.append(record)

        charges_source_id = charges_records[0]["source_refs"][0]["source_id"]
        charges_source = next(source for source in sources if source.source_id == charges_source_id)
        if possible_duplicate_records:
            exceptions.append(
                make_exception(
                    source=charges_source,
                    exception_id=f"{charges_source.source_id}:{category}:possible-balance-history-duplicate",
                    severity="warn",
                    reason=f"Possible duplicate Stripe {category} rows share a date, amount, and currency across Charges and Balance History exports, but no immutable identifier matched; both records were retained.",
                    blocking=False,
                )
            )
        if not superseded_records:
            continue

        superseded_ids = {id(record) for record in superseded_records}
        records[category] = [record for record in records[category] if id(record) not in superseded_ids]
        removed_source_ids = {
            record["source_refs"][0]["source_id"] for record in superseded_records
        }
        for source in sources:
            if source.source_id in removed_source_ids:
                source.parser_notes.append(
                    f"Stripe Balance History {category} rows were superseded by the Charges export; fee and payout rows remain authoritative."
                )
        exceptions.append(
            make_exception(
                source=charges_source,
                exception_id=f"{charges_source.source_id}:{category}:balance-history-superseded",
                severity="warn",
                reason=f"Stripe Balance History {category} rows were superseded by the Charges export to prevent duplicate normalization; fee and payout evidence was retained.",
                blocking=False,
            )
        )

    payout_history_records = [
        record
        for record in records["payouts"]
        if record.get("attributes", {}).get("stripe_export_type") == "payouts_history"
    ]
    balance_payout_records = [
        record
        for record in records["payouts"]
        if record.get("attributes", {}).get("stripe_export_type") == "balance_history"
    ]
    if payout_history_records and balance_payout_records:
        def payout_immutable_identities(record: dict[str, Any]) -> set[tuple[str, str]]:
            attributes = record.get("attributes", {})
            export_type = attributes.get("stripe_export_type")
            payout_id = (
                attributes.get("stripe_payout_id")
                if export_type == "payouts_history"
                else attributes.get("stripe_source_id")
            )
            balance_transaction_id = attributes.get("stripe_balance_transaction_id")
            identities = set()
            if payout_id and str(payout_id).strip():
                identities.add(("payout", str(payout_id).strip()))
            if balance_transaction_id and str(balance_transaction_id).strip():
                identities.add(("balance_transaction", str(balance_transaction_id).strip()))
            return identities

        authoritative_identities = [
            payout_immutable_identities(record) for record in payout_history_records
        ]
        authoritative_signatures = {
            (record.get("event_date"), abs(float(record.get("gross_amount") or 0)), record.get("currency"))
            for record in payout_history_records
        }
        superseded_payouts = []
        possible_duplicate_payouts = []
        for record in balance_payout_records:
            identities = payout_immutable_identities(record)
            signature = (record.get("event_date"), abs(float(record.get("gross_amount") or 0)), record.get("currency"))
            if any(identities & candidate for candidate in authoritative_identities):
                superseded_payouts.append(record)
            elif signature in authoritative_signatures:
                possible_duplicate_payouts.append(record)

        payout_source_id = payout_history_records[0]["source_refs"][0]["source_id"]
        payout_source = next(source for source in sources if source.source_id == payout_source_id)
        if possible_duplicate_payouts:
            exceptions.append(
                make_exception(
                    source=payout_source,
                    exception_id=f"{payout_source.source_id}:possible-balance-history-payout-duplicate",
                    severity="warn",
                    reason="Possible duplicate Stripe payouts share a date, amount, and currency across Payouts History and Balance History, but no immutable identifier matched; both records were retained.",
                    blocking=False,
                )
            )
        if superseded_payouts:
            superseded_ids = {id(record) for record in superseded_payouts}
            records["payouts"] = [
                record for record in records["payouts"] if id(record) not in superseded_ids
            ]
            removed_source_ids = {
                record["source_refs"][0]["source_id"] for record in superseded_payouts
            }
            for source in sources:
                if source.source_id in removed_source_ids:
                    source.parser_notes.append(
                        "Stripe Balance History payout rows were superseded by matching Payouts History rows; fee rows remain authoritative."
                    )
            exceptions.append(
                make_exception(
                    source=payout_source,
                    exception_id=f"{payout_source.source_id}:balance-history-payouts-superseded",
                    severity="warn",
                    reason="Matching Stripe Balance History payout rows were superseded by the Payouts History export to prevent duplicate normalization; fee evidence was retained.",
                    blocking=False,
                )
            )

    if not any(records.values()):
        exceptions.append(
            {
                "exception_id": "bookprep:no-records",
                "severity": "warn",
                "reason": "No normalized records were produced for the selected period.",
                "blocking": False,
                "suggested_follow_up": "Check whether the source directory contains files overlapping the target period.",
                "source_refs": [],
            }
        )

    return records, exceptions


def sorted_manifest_entries(sources: list[SourceDescriptor]) -> list[dict[str, Any]]:
    entries = [source.manifest_entry() for source in sources]
    return sorted(entries, key=lambda entry: (entry["path"], entry["source_id"]))


def build_normalized_document(
    *,
    company_slug: str,
    period: str,
    base_currency: str,
    sources: list[SourceDescriptor],
    records: dict[str, list[dict[str, Any]]],
    exceptions: list[dict[str, Any]],
) -> dict[str, Any]:
    sorted_records = {category: sorted(values, key=lambda item: item["record_id"]) for category, values in records.items()}
    sorted_exceptions = sorted(exceptions, key=lambda item: item["exception_id"])
    return {
        "schema_version": "1.0",
        "company_slug": company_slug,
        "period": period,
        "base_currency": base_currency,
        "generated_at": utc_now_iso(),
        "sources": sorted_manifest_entries(sources),
        "records": sorted_records,
        "exceptions": sorted_exceptions,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_text: str | None = None
    if path.exists() and "generated_at" in payload:
        try:
            existing_text = path.read_text(encoding="utf-8")
            existing = json.loads(existing_text)
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and "generated_at" in existing:
            existing_content = {key: value for key, value in existing.items() if key != "generated_at"}
            regenerated_content = {key: value for key, value in payload.items() if key != "generated_at"}
            if existing_content == regenerated_content:
                payload = {**payload, "generated_at": existing["generated_at"]}
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if existing_text == serialized:
        return

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize month-level source data for bookkeeping")
    parser.add_argument("--company-dir", required=True, help="Company folder, e.g. companies/example")
    parser.add_argument("--period", required=True, help="Target month in YYYY-MM format")
    parser.add_argument("--source-dir", help="Optional source override. Defaults to companies/<company>/source")
    parser.add_argument("--base-currency", help="Override base currency, e.g. EUR")
    parser.add_argument("--woo-tax-allocation", help="Reviewed annual Woo VAT allocation JSON override")
    parser.add_argument("--output", help="Optional output path for normalized JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    company_dir = Path(args.company_dir)
    period_start, period_end = parse_period(args.period)
    source_dir = Path(args.source_dir) if args.source_dir else company_dir / "source"
    if not source_dir.exists():
        raise SimplbooksError(f"Source directory not found: {source_dir}")

    company_slug = resolve_company_slug(company_dir=args.company_dir) or company_dir.name
    company_name = resolve_company_name(company_dir=args.company_dir) or company_dir.name
    base_currency = load_company_base_currency(company_dir, override=args.base_currency)

    repo_root = Path.cwd()
    sources = inspect_sources(
        source_dir=source_dir,
        root_dir=repo_root,
        period_start=period_start,
        period_end=period_end,
    )
    records, exceptions = aggregate_results(
        sources=sources,
        period_start=period_start,
        period_end=period_end,
        base_currency=base_currency,
    )
    if any(source.parser_name == "parse_woo_tax_summary_csv" for source in sources):
        allocation_path = (
            Path(args.woo_tax_allocation)
            if args.woo_tax_allocation
            else company_dir / "artifacts" / "vat" / f"{period_start.year}-woo-tax-allocation.json"
        )
        allocation = load_bound_woo_tax_allocation(
            allocation_path=allocation_path,
            sources=sources,
            company_slug=company_slug,
            year=period_start.year,
            repo_root=repo_root,
        )
        woo_tax.apply_period_allocation(records, allocation, args.period)
    document = build_normalized_document(
        company_slug=company_slug,
        period=args.period,
        base_currency=base_currency,
        sources=sources,
        records=records,
        exceptions=exceptions,
    )

    output_path = Path(args.output) if args.output else company_dir / "artifacts" / "normalized" / f"{args.period}.json"
    write_json(output_path, document)
    unsupported_canonical_source_ids = [
        source.source_id
        for source in sources
        if source.canonical and source.parser_name not in PARSERS
    ]

    summary = {
        "company_name": company_name,
        "company_slug": company_slug,
        "period": args.period,
        "source_dir": str(source_dir),
        "output": str(output_path),
        "source_count": len(document["sources"]),
        "record_counts": {category: len(values) for category, values in document["records"].items()},
        "exception_count": len(document["exceptions"]),
        "blocking_exception_count": sum(1 for item in document["exceptions"] if item.get("blocking")),
        "unsupported_canonical_source_ids": unsupported_canonical_source_ids,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
