"""Match an annual statement-import plan against the ledger SimplBooks actually posted.

The plan says what each imported statement row should become. This reads a supported
account-ledger export back out of SimplBooks and checks, posting by posting, that it
did. The two are deliberately independent: the plan is what was intended, the export is
what happened, and only comparing them proves the import landed as reviewed.
"""

from __future__ import annotations  # noqa: I001

import argparse
import csv
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


CENT = Decimal("0.01")

REQUIRED_COLUMNS = frozenset(
    {
        "company_id",
        "period",
        "account_id",
        "account_code",
        "transaction_id",
        "business_date",
        "currency",
        "debit",
        "credit",
        "description",
        "document_ref",
    }
)

# Deliberately strict: a plain decimal with an optional two-place fraction. Anything
# carrying a thousands separator is locale-ambiguous, and reading it wrongly by a
# factor of a thousand is exactly the failure this evidence exists to catch.
AMOUNT = re.compile(r"^-?\d+(\.\d{1,2})?$")


class LedgerEvidenceError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _money(value: Decimal) -> str:
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def _amount(value: Any, *, label: str) -> Decimal:
    text = _text(value) or "0"
    if not AMOUNT.fullmatch(text):
        raise LedgerEvidenceError(f"{label} is not an unambiguous decimal amount: {value!r}")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise LedgerEvidenceError(f"{label} is not an unambiguous decimal amount: {value!r}") from exc


def _resolved(path_value: Any, *, cwd: Path) -> Path:
    path = Path(_text(path_value))
    return path if path.is_absolute() else cwd / path


# --- loading ---------------------------------------------------------------


def _ledger_row(raw: dict[str, Any], *, line_no: int) -> dict[str, Any]:
    label = f"Ledger export line {line_no}"
    business_date = _text(raw.get("business_date"))
    try:
        parsed_date = date.fromisoformat(business_date)
    except ValueError as exc:
        raise LedgerEvidenceError(f"{label} has an unreadable business date: {business_date!r}") from exc
    currency = _text(raw.get("currency")).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise LedgerEvidenceError(f"{label} has no three-letter currency: {raw.get('currency')!r}")
    debit = _amount(raw.get("debit"), label=f"{label} debit")
    credit = _amount(raw.get("credit"), label=f"{label} credit")
    if debit and credit:
        raise LedgerEvidenceError(f"{label} carries both a debit and a credit; one posting is one side.")
    transaction_id = _text(raw.get("transaction_id"))
    account_id = _text(raw.get("account_id"))
    if not transaction_id or not account_id:
        raise LedgerEvidenceError(f"{label} requires a transaction ID and an account ID.")
    return {
        "company_id": _text(raw.get("company_id")),
        "period": _text(raw.get("period")),
        "account_id": account_id,
        "account_code": _text(raw.get("account_code")),
        "transaction_id": transaction_id,
        "business_date": parsed_date.isoformat(),
        "currency": currency,
        "signed_amount": debit - credit,
        "description": _text(raw.get("description")),
        "document_ref": _text(raw.get("document_ref")),
    }


def load_ledger_export(binding: dict[str, Any], *, cwd: Path) -> dict[str, Any]:
    """Load one hash-bound account-ledger export, refusing anything ambiguous.

    Free-form assertions, screenshots, and unsupported formats are not evidence. Only a
    supported export whose bytes match the binding is read at all.
    """
    path = _resolved(binding.get("path"), cwd=cwd)
    if not path.exists():
        raise LedgerEvidenceError(f"Bound ledger export is missing: {binding.get('path')}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = _text(binding.get("sha256"))
    if digest != expected:
        raise LedgerEvidenceError(
            f"Bound ledger export SHA-256 does not match: expected {expected!r}, found {digest!r}"
        )

    # Read as a file with newline="" so a quoted multi-line field keeps its newlines,
    # the same way the wallet printout is read.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return _ledger_export_rows(reader, binding=binding, digest=digest)


def _ledger_export_rows(
    reader: csv.DictReader, *, binding: dict[str, Any], digest: str
) -> dict[str, Any]:
    columns = {name.strip() for name in reader.fieldnames or []}
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise LedgerEvidenceError(f"Ledger export is missing required column(s): {', '.join(missing)}")

    rows: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for line_no, raw in enumerate(reader, start=2):
        row = _ledger_row(raw, line_no=line_no)
        identity = (row["transaction_id"], row["account_id"])
        if identity in identities:
            raise LedgerEvidenceError(
                f"Ledger export has a duplicate transaction/account identity: {identity}"
            )
        identities.add(identity)
        rows.append(row)

    return {
        "path": _text(binding.get("path")),
        "sha256": digest,
        "rows": rows,
        "company_ids": {row["company_id"] for row in rows},
        "periods": {row["period"] for row in rows},
    }


# --- matching --------------------------------------------------------------


def _expected_postings(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the postings one plan row should have produced, one per side."""
    if row.get("family") == "reviewed_split":
        sources = [
            (f"{_text(row.get('statement_id'))} part {part.get('part_number')}", part)
            for part in row.get("parts") or []
        ]
    else:
        sources = [(_text(row.get("statement_id")), row)]

    postings: list[dict[str, Any]] = []
    for label, source in sources:
        accounts = source.get("financial_accounts") or {}
        amount = abs(_amount(source.get("signed_amount"), label=f"{label} amount"))
        for side, sign in (("debit", Decimal(1)), ("credit", Decimal(-1))):
            postings.append(
                {
                    "label": label,
                    "side": side,
                    "account_id": _text(accounts.get(side)),
                    "business_date": _text(row.get("date")),
                    "currency": _text(row.get("currency")).upper(),
                    "signed_amount": sign * amount,
                }
            )
    return postings


def _posting_key(account_id: str, business_date: str, currency: str, signed: Decimal) -> tuple[str, str, str, str]:
    return account_id, business_date, currency, _money(signed)


def match_plan_rows(
    plan: dict[str, Any], export: dict[str, Any], *, company_id: str
) -> list[str]:
    """Compare every planned posting with the exported ledger, both directions.

    Unmatched expectations and unexplained ledger rows are reported separately: a plan
    row that never posted and a posting nobody planned are different failures, and a
    net-zero difference between them proves nothing.
    """
    company_ids = export.get("company_ids") or set()
    if company_ids and company_ids != {str(company_id)}:
        raise LedgerEvidenceError(
            f"Ledger export belongs to company {sorted(company_ids)}, not {str(company_id)!r}."
        )

    unclaimed: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in export.get("rows") or []:
        key = _posting_key(row["account_id"], row["business_date"], row["currency"], row["signed_amount"])
        unclaimed.setdefault(key, []).append(row)

    errors: list[str] = []
    for plan_row in plan.get("rows") or []:
        for expected in _expected_postings(plan_row):
            if not expected["account_id"]:
                errors.append(f"{expected['label']} has no {expected['side']} account to match.")
                continue
            key = _posting_key(
                expected["account_id"],
                expected["business_date"],
                expected["currency"],
                expected["signed_amount"],
            )
            candidates = unclaimed.get(key) or []
            if not candidates:
                errors.append(
                    f"{expected['label']} has no ledger posting for {expected['side']} account "
                    f"{expected['account_id']} of {_money(expected['signed_amount'])} "
                    f"{expected['currency']} on {expected['business_date']}."
                )
                continue
            candidates.pop(0)

    for key, remaining in sorted(unclaimed.items()):
        for row in remaining:
            errors.append(
                f"Unexplained ledger posting {row['transaction_id']} on account {key[0]} "
                f"of {key[3]} {key[2]} on {key[1]} is not claimed by any plan row."
            )
    return errors


def ledger_movement(export: dict[str, Any]) -> dict[str, str]:
    totals: dict[str, Decimal] = {}
    for row in export.get("rows") or []:
        key = f"{row['account_id']}|{row['currency']}"
        totals[key] = totals.get(key, Decimal(0)) + row["signed_amount"]
    return {key: _money(total) for key, total in sorted(totals.items())}


def build_evidence_summary(
    plan: dict[str, Any], export: dict[str, Any], *, company_id: str
) -> dict[str, Any]:
    """Report the annual post-import verdict, with the export bound by hash."""
    try:
        errors = match_plan_rows(plan, export, company_id=company_id)
    except LedgerEvidenceError as exc:
        errors = [str(exc)]
    return {
        "schema_version": "1.0",
        "company_slug": _text(plan.get("company_slug")),
        "company_id": str(company_id),
        "year": plan.get("year"),
        "binding": {"path": export.get("path"), "sha256": export.get("sha256")},
        "planned_row_count": len(plan.get("rows") or []),
        "ledger_row_count": len(export.get("rows") or []),
        "movement": ledger_movement(export),
        "errors": errors,
        "status": "fail" if errors else "pass",
    }


# --- CLI -------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerEvidenceError(f"Unable to read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LedgerEvidenceError(f"{path} must contain a JSON object.")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Match a statement-import plan against a SimplBooks ledger export.")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        export = load_ledger_export(
            {
                "path": str(args.export),
                "sha256": hashlib.sha256(args.export.read_bytes()).hexdigest(),
            },
            cwd=Path.cwd(),
        )
        summary = build_evidence_summary(_load_json(args.plan), export, company_id=args.company_id)
    except (LedgerEvidenceError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 1
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
