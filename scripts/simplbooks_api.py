#!/usr/bin/env python3
from __future__ import annotations  # noqa: EXE001, I001

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_TOKEN_FILE = ".apikey"
DEFAULT_RATE_LIMIT_PER_MINUTE = 60
DEFAULT_METADATA_FILENAME = "METADATA.md"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
RETRIABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class SimplbooksError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_token(token_file: str = DEFAULT_TOKEN_FILE) -> str:
    env_token = os.environ.get("SIMPLBOOKS_API_TOKEN", "").strip()
    if env_token:
        return env_token

    token_path = Path(token_file)
    if not token_path.exists():
        raise SimplbooksError(
            f"API token file not found: {token_file}. "
            "Provide .apikey or set SIMPLBOOKS_API_TOKEN."
        )

    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise SimplbooksError(f"API token file is empty: {token_file}")
    return token


def parse_metadata_file(metadata_file: str | Path) -> dict[str, str]:
    path = Path(metadata_file)
    if not path.exists():
        raise SimplbooksError(f"Metadata file not found: {path}")

    metadata: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata


def resolve_metadata_path(
    *,
    metadata_file: str | None = None,
    company_dir: str | None = None,
) -> Path | None:
    if metadata_file:
        return Path(metadata_file)
    if company_dir:
        return Path(company_dir) / DEFAULT_METADATA_FILENAME
    return None


def resolve_company_id(
    company_id: str | None,
    *,
    metadata_file: str | None = None,
    company_dir: str | None = None,
) -> str:
    resolved = (company_id or os.environ.get("SIMPLBOOKS_COMPANY_ID", "")).strip()
    if resolved:
        return resolved

    metadata_path = resolve_metadata_path(metadata_file=metadata_file, company_dir=company_dir)
    if metadata_path and metadata_path.exists():
        metadata = parse_metadata_file(metadata_path)
        for key in (
            "simplbooks company id",
            "simplbooks company_id",
            "company id",
            "company_id",
        ):
            value = metadata.get(key, "").strip()
            if value:
                return value

    if not resolved:
        raise SimplbooksError(
            "Simplbooks company_id is required. "
            "Pass --company-id, set SIMPLBOOKS_COMPANY_ID, or place it in companies/<company>/METADATA.md."
        )
    return resolved


def resolve_company_name(
    *,
    metadata_file: str | None = None,
    company_dir: str | None = None,
) -> str | None:
    metadata_path = resolve_metadata_path(metadata_file=metadata_file, company_dir=company_dir)
    if not metadata_path or not metadata_path.exists():
        return None

    metadata = parse_metadata_file(metadata_path)
    for key in ("company name", "name"):
        value = metadata.get(key, "").strip()
        if value:
            return value
    return None


def resolve_company_slug(
    *,
    metadata_file: str | None = None,
    company_dir: str | None = None,
) -> str | None:
    metadata_path = resolve_metadata_path(metadata_file=metadata_file, company_dir=company_dir)
    if not metadata_path or not metadata_path.exists():
        return None

    metadata = parse_metadata_file(metadata_path)
    for key in ("company slug", "slug"):
        value = metadata.get(key, "").strip()
        if value:
            return value
    return None


def normalize_path(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def flatten_query(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            next_prefix = f"{prefix}[{key}]" if prefix else str(key)
            items.extend(flatten_query(value, next_prefix))
        return items
    if isinstance(data, list):
        for value in data:
            next_prefix = f"{prefix}[]"
            items.extend(flatten_query(value, next_prefix))
        return items
    if data is None:
        return items
    items.append((prefix, str(data)))
    return items


class SimplbooksClient:
    def __init__(
        self,
        company_id: str,
        token: str,
        *,
        timeout: float = 30.0,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        default_get_mode: str = "body",
        request_log_path: str | Path | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self.company_id = resolve_company_id(company_id)
        self.token = token.strip()
        self.timeout = timeout
        self.default_get_mode = default_get_mode
        self.min_interval_seconds = 60.0 / max(rate_limit_per_minute, 1)
        self.request_log_path = Path(request_log_path) if request_log_path else None
        self.max_attempts = max(max_attempts, 1)
        self.retry_backoff_seconds = max(retry_backoff_seconds, 0.0)
        self._last_request_started_at = 0.0

    @property
    def base_url(self) -> str:
        return f"https://app.simplbooks.com/{self.company_id}/api"

    def _throttle(self) -> None:
        now = time.monotonic()
        wait_for = self.min_interval_seconds - (now - self._last_request_started_at)
        if wait_for > 0:
            time.sleep(wait_for)
        self._last_request_started_at = time.monotonic()

    def _log_request_attempt(self, entry: dict[str, Any]) -> None:
        if self.request_log_path is None:
            return
        self.request_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.request_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        get_mode: str | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        payload = payload or None
        mode = get_mode or self.default_get_mode
        url = f"{self.base_url}{normalize_path(path)}"
        data: bytes | None = None
        headers = {
            "Accept": "application/json",
            "X-Simplbooks-Token": self.token,
            "X-Output-Format": "JSON",
        }

        if method == "GET" and payload and mode == "query":
            query = parse.urlencode(flatten_query(payload), doseq=True)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"
        elif payload is not None:
            headers["Content-Type"] = "application/json"
            headers["X-Input-Format"] = "json"
            data = json.dumps(payload).encode("utf-8")

        last_network_error: error.URLError | None = None

        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            req = request.Request(url, data=data, headers=headers, method=method)
            attempt_started_at = utc_now_iso()

            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    http_status = response.getcode()
                    raw_body = response.read().decode("utf-8")
            except error.HTTPError as exc:
                http_status = exc.code
                raw_body = exc.read().decode("utf-8", errors="replace")
            except error.URLError as exc:
                last_network_error = exc
                will_retry = attempt < self.max_attempts
                self._log_request_attempt(
                    {
                        "attempt_started_at": attempt_started_at,
                        "attempt": attempt,
                        "method": method,
                        "path": normalize_path(path),
                        "url": url,
                        "payload": payload,
                        "http_status": None,
                        "response_body": None,
                        "network_error": str(exc),
                        "will_retry": will_retry,
                    }
                )
                if will_retry:
                    time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
                    continue
                raise SimplbooksError(f"Network error while calling {url}: {exc}") from exc

            try:
                parsed_body = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                parsed_body = {"raw_body": raw_body}

            if isinstance(parsed_body, dict):
                response_payload = dict(parsed_body)
                response_payload.setdefault("_http_status", http_status)
                response_payload.setdefault("_request_url", url)
                response_payload.setdefault("_request_method", method)
            else:
                response_payload = {
                    "_http_status": http_status,
                    "_request_url": url,
                    "_request_method": method,
                    "data": parsed_body,
                }

            will_retry = http_status in RETRIABLE_HTTP_STATUSES and attempt < self.max_attempts
            self._log_request_attempt(
                {
                    "attempt_started_at": attempt_started_at,
                    "attempt": attempt,
                    "method": method,
                    "path": normalize_path(path),
                    "url": url,
                    "payload": payload,
                    "http_status": http_status,
                    "response_body": response_payload,
                    "network_error": None,
                    "will_retry": will_retry,
                }
            )

            if will_retry:
                time.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
                continue
            return response_payload

        if last_network_error is not None:
            raise SimplbooksError(f"Network error while calling {url}: {last_network_error}") from last_network_error
        raise SimplbooksError(f"Request loop exited unexpectedly for {url}.")

    def paginate(
        self,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        per_page: int = 1000,
        start_page: int = 1,
        max_pages: int | None = None,
        get_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        page = start_page
        collected: list[dict[str, Any]] = []

        while True:
            if max_pages is not None and page >= start_page + max_pages:
                break

            page_payload = dict(payload or {})
            page_payload["page"] = page
            page_payload["per_page"] = per_page
            response = self.request(path, method="GET", payload=page_payload, get_mode=get_mode)

            if response.get("_http_status") != 200 or response.get("status") not in (None, 200):
                raise SimplbooksError(
                    f"Pagination failed for {path} page {page}: "
                    f"{json.dumps(response, ensure_ascii=True)}"
                )

            page_items = response.get("data") or []
            if not isinstance(page_items, list) or not page_items:
                break

            collected.extend(page_items)
            if len(page_items) < per_page:
                break
            page += 1

        return collected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reusable Simplbooks API client")
    parser.add_argument("--company-id", help="Simplbooks company ID")
    parser.add_argument("--company-dir", help="Company folder, e.g. companies/example")
    parser.add_argument("--metadata-file", help="Path to company METADATA.md")
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help=f"Path to Simplbooks token file. Default: {DEFAULT_TOKEN_FILE}",
    )
    parser.add_argument(
        "--get-mode",
        choices=("body", "query"),
        default="body",
        help="How to send GET filters when payload is present.",
    )
    parser.add_argument("--request-log", help="Optional JSONL path for per-attempt request/response logging")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS, help="Maximum transport attempts per request")
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=DEFAULT_RETRY_BACKOFF_SECONDS,
        help="Initial backoff before retrying transient transport failures",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    call_parser = subparsers.add_parser("call", help="Perform a single API request")
    call_parser.add_argument("path", help="API path, e.g. invoices/list")
    call_parser.add_argument("--method", default="GET", help="HTTP method")
    call_parser.add_argument(
        "--payload",
        help="JSON request payload for POST or filtered GET requests",
    )

    paginate_parser = subparsers.add_parser("paginate", help="Fetch all pages for a list endpoint")
    paginate_parser.add_argument("path", help="API path, e.g. invoices/list")
    paginate_parser.add_argument("--payload", help="JSON request payload")
    paginate_parser.add_argument("--per-page", type=int, default=1000)
    paginate_parser.add_argument("--max-pages", type=int)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    token = load_token(args.token_file)
    client = SimplbooksClient(
        company_id=resolve_company_id(
            args.company_id,
            metadata_file=args.metadata_file,
            company_dir=args.company_dir,
        ),
        token=token,
        default_get_mode=args.get_mode,
        request_log_path=args.request_log,
        max_attempts=args.max_attempts,
        retry_backoff_seconds=args.retry_backoff_seconds,
    )

    if args.command == "call":
        payload = json.loads(args.payload) if args.payload else None
        response = client.request(args.path, method=args.method, payload=payload)
        json.dump(response, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    if args.command == "paginate":
        payload = json.loads(args.payload) if args.payload else None
        response = client.paginate(
            args.path,
            payload=payload,
            per_page=args.per_page,
            max_pages=args.max_pages,
        )
        json.dump(response, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    parser.error(f"Unhandled command: {args.command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
