from __future__ import annotations  # noqa: I001

import csv
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookprep  # noqa: E402
import stripe_api  # noqa: E402


def charge(**overrides: object) -> dict:
    """A Stripe balance_transaction as the API returns it: minor units, unix seconds."""
    base = {
        "id": "txn_3EXAMPLE0000000000balance",
        "object": "balance_transaction",
        "amount": 2985,
        "fee": 70,
        "net": 2915,
        "currency": "eur",
        "created": 1767979919,
        "available_on": 1768416000,
        "type": "charge",
        "description": "Example Company - Order 820",
        "source": "py_3EXAMPLE0000000000charge",
    }
    base.update(overrides)
    return base


class LoadSecretKey(unittest.TestCase):
    def _write(self, text: str) -> Path:
        with tempfile.NamedTemporaryFile("w", suffix=".apikey", delete=False) as handle:
            handle.write(text)
            return Path(handle.name)

    def test_reads_the_secret_key_and_ignores_the_publishable_one(self) -> None:
        path = self._write("stripePublishableKey=pk_live_\nstripeSecretKey=sk_live_\n")
        self.assertEqual(stripe_api.load_stripe_secret_key(path), "sk_live_")

    def test_a_bare_key_on_its_own_line_is_accepted(self) -> None:
        self.assertEqual(stripe_api.load_stripe_secret_key(self._write("sk_live_\n")), "sk_live_")

    def test_a_file_without_a_secret_key_is_refused(self) -> None:
        path = self._write("stripePublishableKey=pk_live_\n")
        with self.assertRaises(stripe_api.StripeError):
            stripe_api.load_stripe_secret_key(path)

    def test_a_missing_file_is_refused_by_name(self) -> None:
        with self.assertRaises(stripe_api.StripeError) as ctx:
            stripe_api.load_stripe_secret_key(Path("/nonexistent/.apikey-stripe"))
        self.assertIn(".apikey-stripe", str(ctx.exception))


class BalanceRows(unittest.TestCase):
    def test_minor_units_become_decimal_amounts(self) -> None:
        row = stripe_api.stripe_balance_rows([charge()])[0]
        self.assertEqual(row["Amount"], "29.85")
        self.assertEqual(row["Fee"], "0.70")
        self.assertEqual(row["Net"], "29.15")

    def test_unix_seconds_become_utc_timestamps(self) -> None:
        row = stripe_api.stripe_balance_rows([charge()])[0]
        self.assertEqual(row["Created (UTC)"], "2026-01-09 17:31:59")
        self.assertEqual(row["Available On (UTC)"], "2026-01-14 18:40:00")

    def test_a_zero_decimal_currency_is_not_divided(self) -> None:
        """JPY has no minor unit. Dividing by 100 would understate it a hundredfold."""
        row = stripe_api.stripe_balance_rows([charge(currency="jpy", amount=2985, fee=70, net=2915)])[0]
        self.assertEqual(row["Amount"], "2985")
        self.assertEqual(row["Currency"], "JPY")

    def test_the_signed_amount_is_preserved_so_the_parser_can_classify(self) -> None:
        row = stripe_api.stripe_balance_rows([charge(type="refund", amount=-3968, fee=0, net=-3968)])[0]
        self.assertEqual(row["Amount"], "-39.68")
        self.assertEqual(row["Type"], "refund")

    def test_every_column_the_balance_history_parser_reads_is_present(self) -> None:
        row = stripe_api.stripe_balance_rows([charge()])[0]
        for column in ("id", "Created (UTC)", "Available On (UTC)", "Amount", "Fee", "Net",
                       "Currency", "Type", "Source"):
            self.assertIn(column, row, f"{column} missing from the rendered row")

    def test_rows_come_back_in_chronological_order(self) -> None:
        rows = stripe_api.stripe_balance_rows([
            charge(id="txn_late", created=1767979919),
            charge(id="txn_early", created=1767200000),
        ])
        self.assertEqual([r["id"] for r in rows], ["txn_early", "txn_late"])


class ParsesThroughBookprep(unittest.TestCase):
    """The rendered CSV must be readable by the existing, untouched parser."""

    def test_a_rendered_export_classifies_as_sales_fees_and_payouts(self) -> None:
        rows = stripe_api.stripe_balance_rows([
            charge(),
            charge(id="txn_payout", type="payout", amount=-3556, fee=0, net=-3556, created=1768000000),
            charge(id="txn_refund", type="refund", amount=-3968, fee=0, net=-3968, created=1768100000),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stripe_balance_history.csv"
            stripe_api.write_balance_history_csv(path, rows)

            source = bookprep.inspect_source_file(
                path=path, root_dir=Path(tmp),
                period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            )
            self.assertEqual(source.parser_name, "parse_stripe_balance_csv")

            result, exceptions = bookprep.parse_stripe_balance_csv(
                source, period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
                base_currency="EUR",
            )

        blocking = [e for e in exceptions if e.get("blocking")]
        self.assertEqual(blocking, [], f"unexpected blocking exceptions: {blocking}")
        self.assertEqual(len(result["sales"]), 1)
        self.assertEqual(len(result["payouts"]), 1)
        self.assertEqual(len(result["refunds"]), 1)
        # bookprep stores money as float so the artifact stays JSON-serializable.
        self.assertEqual(result["sales"][0]["gross_amount"], 29.85)
        self.assertEqual(result["payouts"][0]["gross_amount"], 35.56)

    def test_the_written_file_is_a_csv_with_a_header(self) -> None:
        rows = stripe_api.stripe_balance_rows([charge()])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            stripe_api.write_balance_history_csv(path, rows)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                parsed = list(csv.DictReader(handle))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["id"], "txn_3EXAMPLE0000000000balance")


class QueryWindow(unittest.TestCase):
    def test_a_year_becomes_an_inclusive_unix_range(self) -> None:
        start, end = stripe_api.year_window(2026)
        self.assertEqual(start, 1767225600)   # 2026-01-01 00:00:00 UTC
        self.assertEqual(end, 1798761599)     # 2026-12-31 23:59:59 UTC


if __name__ == "__main__":
    unittest.main()
