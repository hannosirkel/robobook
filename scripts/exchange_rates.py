#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib import error, parse, request


FRANKFURTER_RATES_URL = "https://api.frankfurter.dev/v2/rates"


class ExchangeRateError(RuntimeError):
    pass


@dataclass(frozen=True)
class RateResolution:
    requested_date: date
    effective_date: date
    base: str
    quote: str
    rate: Decimal
    provider: str
    source_url: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalized_currency(value: str) -> str:
    currency = str(value or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ExchangeRateError(f"Invalid ISO currency code {value!r}.")
    return currency


def build_frankfurter_url(year: int, base: str, quote: str) -> str:
    base = normalized_currency(base)
    quote = normalized_currency(quote)
    query = parse.urlencode(
        {
            "from": f"{year:04d}-01-01",
            "to": f"{year:04d}-12-31",
            "base": base,
            "quotes": quote,
            "providers": "ECB",
        }
    )
    return f"{FRANKFURTER_RATES_URL}?{query}"


def build_frankfurter_request(year: int, base: str, quote: str) -> request.Request:
    return request.Request(
        build_frankfurter_url(year, base, quote),
        headers={"Accept": "application/json", "User-Agent": "robobook-exchange-rates/1.0"},
        method="GET",
    )


def decimal_rate(value: Any) -> Decimal:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeRateError(f"Invalid exchange rate {value!r}.") from exc
    if not rate.is_finite() or rate <= 0:
        raise ExchangeRateError(f"Exchange rate must be a positive finite number, got {value!r}.")
    return rate


def validate_cache(payload: dict[str, Any], *, year: int, base: str, quote: str) -> None:
    base = normalized_currency(base)
    quote = normalized_currency(quote)
    if payload.get("provider") != "ECB":
        raise ExchangeRateError("Exchange-rate cache must be pinned to provider ECB.")
    if payload.get("year") != year:
        raise ExchangeRateError(f"Exchange-rate cache year mismatch: expected {year}, got {payload.get('year')!r}.")
    if payload.get("base") != base or payload.get("quote") != quote:
        raise ExchangeRateError(
            f"Exchange-rate cache pair mismatch: expected {base}/{quote}, "
            f"got {payload.get('base')!r}/{payload.get('quote')!r}."
        )
    if not str(payload.get("source_url") or "").startswith(FRANKFURTER_RATES_URL):
        raise ExchangeRateError("Exchange-rate cache source_url is not a Frankfurter v2 rates URL.")
    rows = payload.get("rates")
    if not isinstance(rows, list) or not rows:
        raise ExchangeRateError("Exchange-rate cache contains no daily rates.")

    seen_dates: set[date] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ExchangeRateError("Exchange-rate cache rows must be objects.")
        try:
            row_date = date.fromisoformat(str(row.get("date") or ""))
        except ValueError as exc:
            raise ExchangeRateError(f"Invalid exchange-rate date {row.get('date')!r}.") from exc
        carry_in_start = date(year - 1, 12, 1)
        year_end = date(year, 12, 31)
        if row_date < carry_in_start or row_date > year_end:
            raise ExchangeRateError(
                f"Exchange-rate row {row_date} falls outside the allowed carry-in/{year} window."
            )
        if row_date in seen_dates:
            raise ExchangeRateError(f"Duplicate exchange-rate date {row_date}.")
        seen_dates.add(row_date)
        if row.get("base") != base or row.get("quote") != quote:
            raise ExchangeRateError(f"Exchange-rate row {row_date} has an inverted or unexpected currency pair.")
        decimal_rate(row.get("rate"))


def lookup_rate(
    payload: dict[str, Any],
    *,
    requested_date: date,
    base: str,
    quote: str,
) -> RateResolution:
    validate_cache(payload, year=requested_date.year, base=base, quote=quote)
    candidates: list[tuple[date, Decimal]] = []
    for row in payload["rates"]:
        row_date = date.fromisoformat(row["date"])
        if row_date <= requested_date:
            candidates.append((row_date, decimal_rate(row["rate"])))
    if not candidates:
        raise ExchangeRateError(
            f"No ECB {base}/{quote} rate exists on or before {requested_date.isoformat()} in the cache."
        )
    effective_date, rate = max(candidates, key=lambda item: item[0])
    return RateResolution(
        requested_date=requested_date,
        effective_date=effective_date,
        base=normalized_currency(base),
        quote=normalized_currency(quote),
        rate=rate,
        provider="ECB",
        source_url=str(payload["source_url"]),
    )


def normalize_api_rows(
    rows: Any,
    *,
    year: int,
    base: str,
    quote: str,
    source_url: str,
) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise ExchangeRateError("Frankfurter response must be an array of rate rows.")
    normalized: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ExchangeRateError("Frankfurter response contains a non-object rate row.")
        normalized.append(
            {
                "date": str(row.get("date") or ""),
                "base": str(row.get("base") or "").upper(),
                "quote": str(row.get("quote") or "").upper(),
                "rate": format(decimal_rate(row.get("rate")), "f"),
            }
        )
    payload = {
        "schema_version": "1.0",
        "provider": "ECB",
        "year": year,
        "base": normalized_currency(base),
        "quote": normalized_currency(quote),
        "source_url": source_url,
        "retrieved_at": utc_now_iso(),
        "rates": sorted(normalized, key=lambda item: item["date"]),
    }
    validate_cache(payload, year=year, base=base, quote=quote)
    return payload


def fetch_annual_rates(*, year: int, base: str, quote: str, timeout: float = 30.0) -> dict[str, Any]:
    req = build_frankfurter_request(year, base, quote)
    source_url = req.full_url
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except (error.HTTPError, error.URLError) as exc:
        raise ExchangeRateError(f"Failed to fetch ECB rates from Frankfurter: {exc}") from exc
    try:
        rows = json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ExchangeRateError("Frankfurter returned invalid JSON.") from exc
    return normalize_api_rows(rows, year=year, base=base, quote=quote, source_url=source_url)


def write_cache_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def load_cache(path: Path, *, year: int, base: str, quote: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExchangeRateError(f"Could not load exchange-rate cache {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExchangeRateError(f"Exchange-rate cache {path} must contain a JSON object.")
    validate_cache(payload, year=year, base=base, quote=quote)
    return payload


def default_cache_path(company_dir: Path, year: int) -> Path:
    return company_dir / "artifacts" / "reference" / f"ecb-rates-{year}.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch and cache annual ECB exchange rates through Frankfurter.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch_parser = subparsers.add_parser("fetch", help="Fetch or validate an annual historical cache")
    fetch_parser.add_argument("--company-dir", required=True)
    fetch_parser.add_argument("--year", required=True, type=int)
    fetch_parser.add_argument("--base", required=True)
    fetch_parser.add_argument("--quote", default="EUR")
    fetch_parser.add_argument("--output")
    fetch_parser.add_argument("--refresh", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    company_dir = Path(args.company_dir)
    output = Path(args.output) if args.output else default_cache_path(company_dir, args.year)
    if output.exists() and not args.refresh:
        payload = load_cache(output, year=args.year, base=args.base, quote=args.quote)
        reused = True
    else:
        payload = fetch_annual_rates(year=args.year, base=args.base, quote=args.quote)
        write_cache_atomic(output, payload)
        reused = False
    print(
        json.dumps(
            {
                "output": str(output),
                "year": args.year,
                "base": normalized_currency(args.base),
                "quote": normalized_currency(args.quote),
                "provider": "ECB",
                "rate_count": len(payload["rates"]),
                "reused": reused,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
