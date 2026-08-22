from __future__ import annotations  # noqa: I001

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


DISCOVERY_MAX_AGE = timedelta(minutes=30)


class ReferenceArtifactError(RuntimeError):
    pass


def parse_utc(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReferenceArtifactError("Discovery overview requires a valid retrieved_at timestamp.") from exc
    if parsed.tzinfo is None:
        raise ReferenceArtifactError("Discovery retrieved_at must be timezone-aware.")
    return parsed.astimezone(UTC)


def validate_discovery(
    payload: dict[str, Any],
    *,
    year: int,
    company_id: str,
    now: datetime | None = None,
    max_age: timedelta = DISCOVERY_MAX_AGE,
) -> None:
    if int(payload.get("year") or 0) != year:
        raise ReferenceArtifactError("Discovery overview year does not match the action year.")
    if str(payload.get("company_id") or "") != str(company_id):
        raise ReferenceArtifactError("Discovery overview company_id does not match company metadata.")
    retrieved_at = parse_utc(payload.get("retrieved_at"))
    current = (now or datetime.now(UTC)).astimezone(UTC)
    age = current - retrieved_at
    if age < timedelta(0) or age > max_age:
        raise ReferenceArtifactError(
            f"Discovery overview is not fresh enough for approval/write (age {age}, maximum {max_age})."
        )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bind_file(path: Path, *, kind: str, cwd: Path) -> dict[str, str]:
    resolved = path.resolve()
    try:
        display = str(resolved.relative_to(cwd.resolve()))
    except ValueError:
        display = str(resolved)
    return {"kind": kind, "path": display, "sha256": file_sha256(resolved)}


def verify_file_binding(binding: dict[str, Any], *, cwd: Path) -> Path:
    path = Path(str(binding.get("path") or ""))
    resolved = path if path.is_absolute() else cwd / path
    if not resolved.exists():
        raise ReferenceArtifactError(f"Bound reference artifact is missing: {path}")
    if file_sha256(resolved) != str(binding.get("sha256") or ""):
        raise ReferenceArtifactError(f"Bound reference artifact changed after draft creation: {path}")
    return resolved


def required_action_binding_kinds(action_batch: dict[str, Any]) -> set[str]:
    required = {"posting_policy", "discovery_overview"}
    actions = action_batch.get("actions") or []
    if any(
        str((action.get("payload") or {}).get("currency") or "EUR").upper() != "EUR"
        for action in actions
        if isinstance(action, dict)
    ):
        required.add("exchange_rates")
    if any(
        line.get("vat_allocation_component") not in (None, "")
        for action in actions
        if isinstance(action, dict)
        for line in (action.get("payload") or {}).get("line_items") or []
        if isinstance(line, dict)
    ):
        required.update({"woo_tax_allocation", "woo_tax_source"})
    return required
