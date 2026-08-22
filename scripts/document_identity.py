#!/usr/bin/env python3
from __future__ import annotations  # noqa: EXE001, I001

import re
import unicodedata
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Literal  # noqa: UP035


MatchStatus = Literal["exact", "ambiguous", "none"]


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def normalize_external_number(value: Any) -> str | None:
    normalized = re.sub(r"\s+", "", str(value or "")).upper()
    return normalized or None


def normalize_amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid document amount: {value!r}") from exc


@dataclass(frozen=True)
class DocumentIdentity:
    document_type: str
    supplier_name: str
    external_number: str | None
    document_date: str
    currency: str
    gross_amount: Decimal
    simplbooks_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["gross_amount"] = float(self.gross_amount)
        return payload


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    matches: tuple[DocumentIdentity, ...] = ()


def document_identity(record: dict[str, Any], *, document_type: str) -> DocumentIdentity:
    attributes = record.get("attributes") or {}
    supplier_name = record.get("client_name") or attributes.get("vendor_name") or record.get("supplier_name")
    external_number = record.get("number") or record.get("external_number")
    if external_number in (None, ""):
        external_number = record.get("external_ref") or attributes.get("invoice_number")
    document_date = (
        record.get("transaction_date")
        or record.get("event_date")
        or record.get("document_date")
        or record.get("created")
        or ""
    )
    currency = record.get("currency_name") or record.get("currency") or "EUR"
    gross_amount = record.get("total_sum")
    if gross_amount is None:
        gross_amount = record.get("gross_amount", record.get("sum", 0))
    simplbooks_id = record.get("id") or record.get("simplbooks_id")

    return DocumentIdentity(
        document_type=normalize_text(document_type),
        supplier_name=normalize_text(supplier_name),
        external_number=normalize_external_number(external_number),
        document_date=str(document_date or "")[:10],
        currency=str(currency or "EUR").strip().upper(),
        gross_amount=normalize_amount(gross_amount),
        simplbooks_id=str(simplbooks_id) if simplbooks_id not in (None, "") else None,
    )


def _compatible_number_match(candidate: DocumentIdentity, existing: DocumentIdentity) -> bool:
    return (
        candidate.document_type == existing.document_type
        and candidate.supplier_name == existing.supplier_name
    )


def _fallback_match(candidate: DocumentIdentity, existing: DocumentIdentity) -> bool:
    return (
        candidate.document_type == existing.document_type
        and candidate.supplier_name == existing.supplier_name
        and candidate.document_date == existing.document_date
        and candidate.currency == existing.currency
        and candidate.gross_amount == existing.gross_amount
    )


def match_existing(candidate: DocumentIdentity, existing: Iterable[DocumentIdentity]) -> MatchResult:
    existing_items = tuple(existing)
    if candidate.external_number:
        same_number = tuple(item for item in existing_items if item.external_number == candidate.external_number)
        compatible = tuple(item for item in same_number if _compatible_number_match(candidate, item))
        if len(compatible) == 1:
            return MatchResult("exact", compatible)
        if same_number:
            return MatchResult("ambiguous", same_number)
        return MatchResult("none")

    fallback_matches = tuple(item for item in existing_items if _fallback_match(candidate, item))
    if len(fallback_matches) == 1:
        return MatchResult("exact", fallback_matches)
    if len(fallback_matches) > 1:
        return MatchResult("ambiguous", fallback_matches)
    return MatchResult("none")
