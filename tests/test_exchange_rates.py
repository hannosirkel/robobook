from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import exchange_rates  # noqa: E402


def cache_with_rates(rates: dict[str, str], *, base: str = "USD", quote: str = "EUR") -> dict:
    return {
        "schema_version": "1.0",
        "provider": "ECB",
        "year": 2024,
        "base": base,
        "quote": quote,
        "source_url": "https://api.frankfurter.dev/v2/rates?providers=ECB",
        "retrieved_at": "2026-08-21T00:00:00Z",
        "rates": [
            {"date": rate_date, "base": base, "quote": quote, "rate": rate}
            for rate_date, rate in sorted(rates.items())
        ],
    }


class ExchangeRateTests(unittest.TestCase):
    def test_url_requests_one_year_from_ecb(self) -> None:
        url = exchange_rates.build_frankfurter_url(2024, "USD", "EUR")

        self.assertIn("from=2024-01-01", url)
        self.assertIn("to=2024-12-31", url)
        self.assertIn("base=USD", url)
        self.assertIn("quotes=EUR", url)
        self.assertIn("providers=ECB", url)

    def test_lookup_uses_latest_prior_ecb_date(self) -> None:
        payload = cache_with_rates({"2024-03-28": "0.9241", "2024-04-02": "0.9280"})

        result = exchange_rates.lookup_rate(
            payload,
            requested_date=date(2024, 3, 31),
            base="USD",
            quote="EUR",
        )

        self.assertEqual(result.requested_date, date(2024, 3, 31))
        self.assertEqual(result.effective_date, date(2024, 3, 28))
        self.assertEqual(result.rate, Decimal("0.9241"))
        self.assertEqual(result.provider, "ECB")

    def test_validation_rejects_inverted_pair(self) -> None:
        with self.assertRaises(exchange_rates.ExchangeRateError):
            exchange_rates.validate_cache(
                cache_with_rates({"2024-03-28": "1.0821"}, base="EUR", quote="USD"),
                year=2024,
                base="USD",
                quote="EUR",
            )

    def test_lookup_rejects_date_before_cache_coverage(self) -> None:
        payload = cache_with_rates({"2024-01-02": "0.91"})

        with self.assertRaises(exchange_rates.ExchangeRateError):
            exchange_rates.lookup_rate(
                payload,
                requested_date=date(2024, 1, 1),
                base="USD",
                quote="EUR",
            )


if __name__ == "__main__":
    unittest.main()
