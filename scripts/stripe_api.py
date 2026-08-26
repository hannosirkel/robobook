"""Read Stripe balance transactions and capture them as source evidence.

A vendor CSV export is the least reliable input this pipeline has. Stripe changed
its export columns from Title Case to snake_case between 2025 and 2026 and
``bookprep`` stopped parsing it, having changed nothing itself. The API contract
is versioned; an export layout is not.

This client fetches the balance-transaction ledger and renders it into the
balance-history CSV shape ``parse_stripe_balance_csv`` already reads, so the
tested parser is untouched. The written file is captured under the company's
source pack and hashed into the evidence chain like any other source.

Read-only by construction: the only endpoint is a GET, and no write path exists.
"""
from __future__ import annotations

import argparse
import csv
import json
from calendar import timegm
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib import error, parse, request

BALANCE_TRANSACTIONS_URL = "https://api.stripe.com/v1/balance_transactions"
PAGE_LIMIT = 100

# Stripe reports these in whole units; every other currency is in minor units.
# Dividing a JPY amount by 100 would understate it a hundredfold.
ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
        "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
)

# The columns parse_stripe_balance_csv reads from a balance-history export.
BALANCE_HISTORY_COLUMNS = (
    "id",
    "Type",
    "Source",
    "Amount",
    "Fee",
    "Net",
    "Currency",
    "Created (UTC)",
    "Available On (UTC)",
    "Description",
)


class StripeError(RuntimeError):
    """A Stripe access or contract problem that must stop the caller."""


def load_stripe_secret_key(path: Path) -> str:
    """Read the secret key, ignoring the publishable one that sits beside it.

    The file carries both keys. Only the secret key authenticates, and only it
    must never be logged or committed, so it is returned and nothing else is.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StripeError(f"Could not read the Stripe key file {path}: {exc}") from exc

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, _, value = line.partition("=")
            if name.strip().lower() == "stripesecretkey" and value.strip():
                return value.strip()
            continue
        if line.startswith("sk_"):
            return line
    raise StripeError(
        f"No Stripe secret key found in {path}. Expected a 'stripeSecretKey=sk_...' line."
    )


def year_window(year: int) -> tuple[int, int]:
    """Inclusive unix-second bounds for a calendar year in UTC."""
    start = timegm((year, 1, 1, 0, 0, 0, 0, 0, 0))
    end = timegm((year, 12, 31, 23, 59, 59, 0, 0, 0))
    return start, end


def currency_exponent(currency: str) -> int:
    return 0 if currency.strip().lower() in ZERO_DECIMAL_CURRENCIES else 2


def minor_units_to_amount(value: int | None, currency: str) -> Decimal:
    amount = Decimal(int(value or 0))
    if currency_exponent(currency) == 0:
        return amount
    return amount / Decimal(100)


def format_amount(value: Decimal, currency: str) -> str:
    """Render at the currency's own precision, keeping the sign the API reported.

    Dividing 70 minor units yields Decimal('0.7'); a money column should read
    '0.70'. Quantizing to the currency exponent keeps the export self-consistent
    rather than depending on whether a value happened to end in a zero.
    """
    exponent = currency_exponent(currency)
    quantum = Decimal(1) if exponent == 0 else Decimal("0.01")
    return format(value.quantize(quantum), "f")


def utc_timestamp(value: int | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(int(value), tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def stripe_balance_rows(transactions: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Render API balance transactions into balance-history export rows.

    The signed amount is passed through untouched. ``stripe_category`` classifies
    on ``Type`` first and falls back to the sign, so preserving both keeps the
    parser's own classification authoritative rather than second-guessing it here.
    """
    rows: list[dict[str, str]] = []
    for txn in sorted(transactions, key=lambda item: (int(item.get("created") or 0), str(item.get("id") or ""))):
        currency = str(txn.get("currency") or "").strip()
        source = txn.get("source")
        rows.append(
            {
                "id": str(txn.get("id") or ""),
                "Type": str(txn.get("type") or ""),
                "Source": "" if source is None else str(source),
                "Amount": format_amount(minor_units_to_amount(txn.get("amount"), currency), currency),
                "Fee": format_amount(minor_units_to_amount(txn.get("fee"), currency), currency),
                "Net": format_amount(minor_units_to_amount(txn.get("net"), currency), currency),
                "Currency": currency.upper(),
                "Created (UTC)": utc_timestamp(txn.get("created")),
                "Available On (UTC)": utc_timestamp(txn.get("available_on")),
                "Description": str(txn.get("description") or ""),
            }
        )
    return rows


def write_balance_history_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(BALANCE_HISTORY_COLUMNS), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in BALANCE_HISTORY_COLUMNS})


def fetch_balance_transactions(
    *, secret_key: str, created_gte: int, created_lte: int, timeout: float = 30.0
) -> list[dict[str, Any]]:
    """Page through the balance-transaction ledger for a closed time window."""
    collected: list[dict[str, Any]] = []
    starting_after: str | None = None
    while True:
        query: dict[str, Any] = {
            "limit": PAGE_LIMIT,
            "created[gte]": created_gte,
            "created[lte]": created_lte,
        }
        if starting_after:
            query["starting_after"] = starting_after
        req = request.Request(f"{BALANCE_TRANSACTIONS_URL}?{parse.urlencode(query)}", method="GET")
        req.add_header("Authorization", f"Bearer {secret_key}")
        try:
            with request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            # Never echo the request: it carries the Authorization header.
            raise StripeError(f"Stripe returned HTTP {exc.code} for balance_transactions.") from exc
        except error.URLError as exc:
            raise StripeError(f"Could not reach Stripe: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise StripeError("Stripe returned invalid JSON.") from exc

        if "error" in payload:
            raise StripeError(str(payload["error"].get("message") or "Stripe reported an error."))
        batch = payload.get("data") or []
        collected.extend(batch)
        if not payload.get("has_more") or not batch:
            return collected
        starting_after = str(batch[-1].get("id") or "")
        if not starting_after:
            raise StripeError("Stripe reported more pages but returned no cursor.")


def resolve_output_path(company_dir: Path, year: int, override: str | None) -> Path:
    if override:
        return Path(override)
    return company_dir / "source" / f"{year}-pack" / "Stripe" / f"stripe_balance_history_{year}.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture Stripe balance transactions as source evidence (read-only)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="Fetch one year and write the balance-history CSV")
    fetch.add_argument("--company-dir", required=True)
    fetch.add_argument("--year", required=True, type=int)
    fetch.add_argument("--key-file", default=".apikey-stripe")
    fetch.add_argument("--output")
    args = parser.parse_args(argv)

    company_dir = Path(args.company_dir)
    secret_key = load_stripe_secret_key(Path(args.key_file))
    created_gte, created_lte = year_window(args.year)
    transactions = fetch_balance_transactions(
        secret_key=secret_key, created_gte=created_gte, created_lte=created_lte
    )
    rows = stripe_balance_rows(transactions)
    output_path = resolve_output_path(company_dir, args.year, args.output)
    write_balance_history_csv(output_path, rows)

    types: dict[str, int] = {}
    for row in rows:
        types[row["Type"]] = types.get(row["Type"], 0) + 1
    print(
        json.dumps(
            {
                "output": str(output_path),
                "year": args.year,
                "transaction_count": len(rows),
                "types": dict(sorted(types.items())),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StripeError as exc:
        raise SystemExit(f"error: {exc}") from exc
