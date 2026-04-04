from __future__ import annotations

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

import simplbooks_api  # noqa: E402


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.body.encode("utf-8")


class SimplbooksApiTests(unittest.TestCase):
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

            with mock.patch.object(
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
