from __future__ import annotations  # noqa: I001

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib import error


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import simplbooks_api  # noqa: E402, I001
import examine_simplbooks_year  # noqa: E402


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body

    def __enter__(self) -> "FakeResponse":  # noqa: PYI034, UP037
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.body.encode("utf-8")


class SimplbooksApiTests(unittest.TestCase):
    def test_discovery_indexes_every_cash_transaction(self) -> None:
        class FakeClient:
            company_id = "CID"

            def paginate(self, path: str, **_kwargs: object) -> list[dict]:
                pages = {
                    "financial_accounts/list": [],
                    "income_accounts/list": [],
                    "warehouses/list": [],
                    "invoices/list": [],
                    "purchases/list": [],
                    "incomings/list": [
                        {"Incoming": {
                            "id": "601", "income_date": "2024-05-10", "transaction_date": "2024-05-09", "income_sum": 42.5,
                            "currency_name": "EUR", "income_account_id": "11", "invoice_id": "101",
                            "client_name": "Buyer OÜ", "description": "Invoice receipt",
                        }},
                        {"Incoming": {"id": "old", "income_date": "2023-12-31", "income_sum": 1}},
                    ],
                    "payments/list": [
                        {"Payment": {
                            "id": "701", "payment_date": "2024-05-11", "payment_sum": 12.25,
                            "currency_name": "EUR", "income_account_id": "11", "purchase_id": "201",
                            "client_name": "Supplier OÜ", "description": "Purchase payment",
                        }},
                    ],
                }
                return pages[path]

            def request(self, _path: str, **_kwargs: object) -> dict:
                return {"data": []}

        overview = examine_simplbooks_year.build_year_overview(FakeClient(), year=2024)

        cash = [
            item for item in overview["document_index"]
            if item["document_type"] in {"incoming", "payment"}
        ]
        self.assertEqual({item["simplbooks_id"] for item in cash}, {"601", "701"})
        self.assertTrue(all("document_date" in item and "gross_amount" in item for item in cash))
        incoming = next(item for item in cash if item["document_type"] == "incoming")
        self.assertEqual(incoming["document_date"], "2024-05-10")
        self.assertEqual(incoming["linked_document_id"], "101")
        self.assertEqual(incoming["income_account_id"], "11")
        self.assertEqual(incoming["supplier_name"], "buyer oü")
        self.assertEqual(incoming["description"], "Invoice receipt")

    def test_discovery_filters_invoices_and_purchases_by_transaction_date(self) -> None:
        class FakeClient:
            company_id = "CID"

            def __init__(self) -> None:
                self.paginate_calls: list[tuple[str, dict]] = []
                self.detail_calls: list[str] = []

            def paginate(self, path: str, **kwargs: object) -> list[dict]:
                self.paginate_calls.append((path, dict(kwargs)))
                pages = {
                    "financial_accounts/list": [],
                    "income_accounts/list": [],
                    "warehouses/list": [],
                    "incomings/list": [],
                    "payments/list": [],
                    "invoices/list": [
                        {"invoices": {"id": "inv-in", "created": "2023-12-31", "transaction_date": "2024-01-01"}},
                        {"invoices": {"id": "inv-out", "created": "2024-01-01", "transaction_date": "2023-12-31"}},
                        {"invoices": {"id": "inv-fallback", "created": "2024-01-02"}},
                    ],
                    "purchases/list": [
                        {"Purchase": {"id": "pur-in", "created": "2023-12-31", "transaction_date": "2024-01-01"}},
                        {"Purchase": {"id": "pur-out", "created": "2024-01-01", "transaction_date": "2023-12-31"}},
                        {"Purchase": {"id": "pur-fallback", "created": "2024-01-02"}},
                    ],
                }
                return pages[path]

            def request(self, path: str, **_kwargs: object) -> dict:
                if path != "vat_types/list":
                    self.detail_calls.append(path)
                return {"data": {"Task": [], "PurchaseRow": []}}

        client = FakeClient()
        overview = examine_simplbooks_year.build_year_overview(client, year=2024)

        self.assertEqual(overview["counts"]["invoices"], 2)
        self.assertEqual(overview["counts"]["purchases"], 2)
        self.assertEqual(
            set(client.detail_calls),
            {"invoices/get/inv-in", "invoices/get/inv-fallback", "purchases/get/pur-in", "purchases/get/pur-fallback"},
        )
        list_payloads = {path: kwargs.get("payload") for path, kwargs in client.paginate_calls}
        self.assertIsNone(list_payloads["invoices/list"])
        self.assertIsNone(list_payloads["purchases/list"])

    def test_request_retries_transient_http_error_and_logs_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "transport.jsonl"
            client = simplbooks_api.SimplbooksClient(
                "123",
                "token",
                request_log_path=log_path,
                max_attempts=2,
                retry_backoff_seconds=0.5,
                rate_limit_per_minute=1_000_000,
            )
            http_error = error.HTTPError(
                url="https://example.test/invoices/list",
                code=429,
                msg="Too Many Requests",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"rate limit"}'),
            )

            with mock.patch.object(
                simplbooks_api.request,
                "urlopen",
                side_effect=[http_error, FakeResponse(200, '{"status":200,"data":{}}')],
            ), mock.patch.object(simplbooks_api.time, "sleep") as sleep_mock:
                response = client.request("invoices/list")

            self.assertEqual(response["_http_status"], 200)
            sleep_mock.assert_called_once_with(0.5)
            lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["http_status"], 429)
            self.assertTrue(lines[0]["will_retry"])
            self.assertEqual(lines[1]["http_status"], 200)
            self.assertFalse(lines[1]["will_retry"])

    def test_request_raises_after_network_retries_exhausted_and_logs_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "transport.jsonl"
            client = simplbooks_api.SimplbooksClient(
                "123",
                "token",
                request_log_path=log_path,
                max_attempts=2,
                retry_backoff_seconds=0.25,
                rate_limit_per_minute=1_000_000,
            )

            with mock.patch.object(  # noqa: SIM117
                simplbooks_api.request,
                "urlopen",
                side_effect=error.URLError("offline"),
            ), mock.patch.object(simplbooks_api.time, "sleep") as sleep_mock:
                with self.assertRaisesRegex(simplbooks_api.SimplbooksError, "Network error"):
                    client.request("payments/list")

            sleep_mock.assert_called_once_with(0.25)
            lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["network_error"], "<urlopen error offline>")
            self.assertTrue(lines[0]["will_retry"])
            self.assertFalse(lines[1]["will_retry"])


if __name__ == "__main__":
    unittest.main()
