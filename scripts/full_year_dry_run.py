#!/usr/bin/env python3
from __future__ import annotations  # noqa: EXE001, I001

import argparse
import copy
import csv
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from simplbooks_api import SimplbooksError, resolve_company_name, resolve_company_slug
from inventory_verification import evaluate_inventory_action, load_manual_inventory_actions
import bookprep
import woo_tax
from booksend import action_successfully_submitted, load_yaml, normalized_endpoint
from posting_policy import PostingPolicyError, cash_posting_mode, prohibited_bank_cash_action
from reference_artifacts import ReferenceArtifactError, file_sha256, verify_file_binding


ORIGINAL_SUBPROCESS_RUN = subprocess.run

STEP_SPECS = (
    ("bookprep", "bookprep.py"),
    ("bookrecon", "bookrecon.py"),
    ("bookbuilder", "bookbuilder.py"),
    ("bookchecker", "bookchecker.py"),
    ("booksend", "booksend.py"),
)

# The run advances through these in order. The first three are states reached; the next
# three name what the run is waiting on, each of which is a person doing something in
# the SimplBooks UI that the published API cannot do.
PHASES = (
    "source_ready",
    "master_data_ready",
    "documents_ready",
    "statement_import_pending",
    "ledger_evidence_pending",
    "inventory_audit_pending",
    "fx_revaluation_pending",
    "final_checks_ready",
)


def fx_revaluation_state(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Answer both halves of the year-end FX question: is it needed, and was it done?

    Revaluing a held foreign-currency balance is a journal entry, and the published API
    has no endpoint for one, so it stays a manual step. A manual step with no record is
    indistinguishable from one nobody did, which is why absent evidence reads as
    unanswered rather than as settled.
    """
    if not evidence:
        return {
            "verdict": "unknown",
            "settled": False,
            "reason": "No year-end FX revaluation evidence exists, so it is unknown whether one is needed.",
        }
    balances = evidence.get("balances") or {}
    has_foreign_balance = any(
        Decimal(str(amount)) != 0 for amount in balances.values() if str(amount).strip()
    )
    required = bool(evidence.get("required"))
    status = str(evidence.get("status") or "")

    if has_foreign_balance and not required:
        return {
            "verdict": "contradictory",
            "settled": False,
            "reason": (
                "Evidence claims no revaluation is required while reporting a non-zero "
                f"foreign-currency balance: {balances}."
            ),
        }
    if not required and not has_foreign_balance:
        return {
            "verdict": "not_required",
            "settled": True,
            "reason": "No foreign-currency balance remained at year end, so no revaluation is due.",
        }
    if status == "posted":
        return {
            "verdict": "posted",
            "settled": True,
            "reason": "The year-end FX revaluation entry is recorded as posted.",
        }
    return {
        "verdict": "pending",
        "settled": False,
        "reason": f"A year-end FX revaluation is required for {sorted(balances)} and is not yet posted.",
    }


@dataclass(frozen=True)
class YearGates:
    """What a year's evidence currently proves. Each field is one gate on the way to done."""

    statement_import_mode: bool
    master_data_resolved: bool
    documents_ready: bool
    ledger_evidence_status: str | None
    inventory_audit_status: str | None
    fx_revaluation_settled: bool


def resolve_run_phase(gates: YearGates) -> str:
    """Report the furthest phase the year's evidence actually supports."""
    if not gates.master_data_resolved:
        return "source_ready"
    if not gates.documents_ready:
        return "master_data_ready"
    if not gates.statement_import_mode:
        return "documents_ready"
    if gates.ledger_evidence_status is None:
        return "statement_import_pending"
    if gates.ledger_evidence_status != "pass":
        return "ledger_evidence_pending"
    if gates.inventory_audit_status != "pass":
        return "inventory_audit_pending"
    if not gates.fx_revaluation_settled:
        return "fx_revaluation_pending"
    return "final_checks_ready"


def load_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def statement_import_year(company_dir: Path) -> bool:
    policy = load_optional_json(company_dir / "artifacts" / "posting_policy.json")
    try:
        return cash_posting_mode(policy) == "statement_import"
    except PostingPolicyError:
        return False


def count_bank_api_cash_actions(company_dir: Path, *, year: int) -> int:
    """Count actions that would move bank cash the imported statement already moves."""
    policy = load_optional_json(company_dir / "artifacts" / "posting_policy.json")
    actions_dir = company_dir / "artifacts" / "actions"
    if not actions_dir.exists():
        return 0
    total = 0
    for path in sorted(actions_dir.glob(f"{year}-*.yaml")):
        try:
            batch = load_yaml(path)
        except (OSError, SimplbooksError):
            continue
        for action in (batch or {}).get("actions") or []:
            try:
                if isinstance(action, dict) and prohibited_bank_cash_action(action, policy):
                    total += 1
            except PostingPolicyError:
                continue
    return total


def periods_for_year(year: int) -> list[str]:
    if year < 1900 or year > 2999:
        raise SimplbooksError(f"Unsupported year for full dry run: {year}")
    return [f"{year}-{month:02d}" for month in range(1, 13)]


def default_output_path(company_dir: Path, year: int) -> Path:
    return company_dir / "artifacts" / "submissions" / f"{year}-dry-run-summary.json"


def parse_json_output(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def source_has_woo_tax_summary(source_dir: Path) -> bool:
    required_headers = {"Tax code", "Rate", "Total tax", "Order tax", "Shipping tax", "Orders"}
    if not source_dir.exists():
        return False
    for path in sorted(source_dir.rglob("*.csv")):
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                headers = set(next(csv.reader(handle), []))
        except (OSError, UnicodeDecodeError, csv.Error):
            continue
        if headers >= required_headers:
            return True
    return False


def validate_woo_tax_preflight(company_dir: Path, year: int, source_dir: Path | None) -> Path | None:
    annual_source_dir = source_dir if source_dir is not None else company_dir / "source"
    if not annual_source_dir.exists():
        return None

    tax_evidence = bookprep.discover_canonical_woo_tax_evidence(
        source_dir=annual_source_dir,
        root_dir=Path.cwd(),
        year=year,
    )
    if not tax_evidence:
        return None

    allocation_path = company_dir / "artifacts" / "vat" / f"{year}-woo-tax-allocation.json"
    company_slug = resolve_company_slug(company_dir=str(company_dir)) or company_dir.name
    try:
        woo_tax.load_allocation(
            allocation_path,
            company_slug=company_slug,
            year=year,
            tax_evidence=tax_evidence,
        )
    except woo_tax.WooTaxError as error:
        raise SimplbooksError(f"Woo tax allocation preflight failed: {error}") from error
    return allocation_path


def extract_api_calls(*, period: str, step_summary: dict[str, Any]) -> list[dict[str, Any]]:
    stdout = step_summary.get("stdout")
    if not isinstance(stdout, dict):
        return []
    api_calls = stdout.get("api_calls") or []
    if not isinstance(api_calls, list):
        return []

    collected: list[dict[str, Any]] = []
    for item in api_calls:
        if not isinstance(item, dict):
            continue
        call = copy.deepcopy(item)
        call.setdefault("period", period)
        collected.append(call)
    return collected


def submitted_month_state(*, company_dir: Path, period: str) -> str:
    """Return a freeze state, failing when a success log no longer binds its YAML."""
    action_path = company_dir / "artifacts" / "actions" / f"{period}.yaml"
    submission_path = company_dir / "artifacts" / "submissions" / f"{period}.json"
    action_batch = load_yaml(action_path) if action_path.exists() else None
    yaml_status = str((action_batch or {}).get("approval_status") or "")
    if not submission_path.exists():
        if yaml_status == "submitted":
            raise SimplbooksError(f"Submitted action batch has no submission log: {action_path}")
        return "not_submitted"

    submission = json.loads(submission_path.read_text(encoding="utf-8"))
    if not isinstance(submission, dict):
        raise SimplbooksError(f"Submission log must contain an object: {submission_path}")
    if submission.get("mode") != "write":
        if yaml_status == "submitted":
            raise SimplbooksError(
                f"Submitted action YAML {action_path} lacks a matching successful write submission log."
            )
        return "not_submitted"
    summary = submission.get("summary") or {}
    successful = (
        bool(str(submission.get("action_file_sha256") or ""))
        and int(summary.get("failed_actions") or 0) == 0
        and summary.get("stopped_on_failure") is False
    )
    if not successful:
        if yaml_status == "submitted":
            raise SimplbooksError(
                f"Submitted action YAML {action_path} is backed only by a partial, unsuccessful write log."
            )
        return "partial_submission"
    if not action_path.exists():
        raise SimplbooksError(f"Successfully submitted month has no immutable action YAML: {period}")
    assert action_batch is not None
    expected_sha = str(submission.get("action_file_sha256") or "")
    if expected_sha != file_sha256(action_path):
        raise SimplbooksError(
            f"Successfully submitted month {period} is immutable, but its action-file SHA does not match."
        )
    if (
        str(submission.get("period") or "") != period
        or str(action_batch.get("period") or "") != period
        or str(action_batch.get("approval_status") or "") != "submitted"
        or str(submission.get("batch_id") or "") != str(action_batch.get("batch_id") or "")
        or str(submission.get("company_slug") or "") != str(action_batch.get("company_slug") or "")
    ):
        raise SimplbooksError(f"Successfully submitted month {period} has inconsistent frozen identities.")
    request_log = submission.get("request_log") or []
    for action in action_batch.get("actions") or []:
        if not isinstance(action, dict):
            raise SimplbooksError(
                f"Successfully submitted month {period} lacks successful write action evidence for <unknown>."
            )
        action_key = str(action.get("idempotency_key") or "")
        matching_evidence = [
            entry
            for entry in request_log
            if isinstance(entry, dict)
            and entry.get("mode") == "write"
            and entry.get("success") is True
            and isinstance(entry.get("http_status"), int)
            and 200 <= int(entry["http_status"]) < 300
            and str(entry.get("action_idempotency_key") or "") == action_key
            and str(entry.get("method") or "POST").upper()
            == str(action.get("method") or "POST").upper()
            and normalized_endpoint(str(entry.get("endpoint") or ""))
            == normalized_endpoint(str(action.get("endpoint") or ""))
            and int(entry["http_status"]) == int(action.get("response_status") or 0)
            and (
                action.get("inserted_id") in (None, "")
                or str(entry.get("inserted_id") or "") == str(action.get("inserted_id"))
            )
        ]
        if (
            not action_successfully_submitted(action)
            or not action_key
            or not matching_evidence
        ):
            raise SimplbooksError(
                f"Successfully submitted month {period} lacks successful write action evidence for {action_key or '<unknown>'}."
            )
    return "submitted"


def summarize_bank_reconciliation_artifacts(
    *,
    company_dir: Path,
    year: int,
    expected_periods: list[str] | None = None,
    period_states: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> dict[str, int]:
    totals = {
        "physical_bank_row_count": 0,
        "allocated_row_count": 0,
        "uncovered_row_count": 0,
        "clearing_movement_count": 0,
        "unresolved_clearing_count": 0,
    }
    expected = periods_for_year(year) if expected_periods is None else expected_periods
    root = cwd or Path.cwd()
    expected_company_slug = resolve_company_slug(company_dir=str(company_dir)) or company_dir.name
    recon_dir = company_dir / "artifacts" / "recon"
    actions_dir = company_dir / "artifacts" / "actions"
    expected_normalized_dir = company_dir / "artifacts" / "normalized"
    expected_allocation_path = company_dir / "artifacts" / "bank" / f"{year}-allocations.json"
    for period in expected:
        if not period.startswith(f"{year}-"):
            raise SimplbooksError(f"Expected reconciliation period is outside {year}: {period}")
        action_path = actions_dir / f"{period}.yaml"
        if not action_path.exists():
            raise SimplbooksError(f"Expected action artifact is missing for reconciliation aggregation: {action_path}")
        action_batch = load_yaml(action_path)
        if (
            str(action_batch.get("period") or "") != period
            or str(action_batch.get("company_slug") or "") != expected_company_slug
        ):
            raise SimplbooksError(f"Action artifact identity mismatch for reconciliation period {period}.")
        run_state = (period_states or {}).get(period)
        if period_states is not None and run_state not in {"processed", "skipped_submitted"}:
            raise SimplbooksError(f"Period {period} was not successfully processed or frozen during this run.")
        if run_state == "skipped_submitted" or str(action_batch.get("approval_status") or "") == "submitted":  # noqa: SIM102
            if submitted_month_state(company_dir=company_dir, period=period) != "submitted":
                raise SimplbooksError(f"Frozen period {period} lacks a successful submitted identity.")

        reconciliation_bindings = [
            binding
            for binding in action_batch.get("reference_artifacts") or []
            if isinstance(binding, dict) and binding.get("kind") == "reconciliation"
        ]
        if len(reconciliation_bindings) != 1:
            raise SimplbooksError(f"Action artifact {period} requires exactly one reconciliation binding.")
        try:
            path = verify_file_binding(reconciliation_bindings[0], cwd=root)
        except ReferenceArtifactError as exc:
            raise SimplbooksError(f"Action-bound reconciliation for {period} changed or is missing: {exc}") from exc
        expected_recon_path = recon_dir / f"{period}.json"
        if path.resolve() != expected_recon_path.resolve():
            raise SimplbooksError(
                f"Action-bound reconciliation for {period} does not resolve to the expected artifact."
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if str(payload.get("period") or "") != period:
            raise SimplbooksError(
                f"Reconciliation artifact period mismatch: expected {period}, got {payload.get('period')!r}: {path}"
            )
        if str(payload.get("company_slug") or "") != expected_company_slug:
            raise SimplbooksError(f"Reconciliation artifact company mismatch for {period}: {path}")
        bindings_by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for binding in payload.get("reference_artifacts") or []:
            if isinstance(binding, dict):
                bindings_by_kind[str(binding.get("kind") or "")].append(binding)
        for kind, expected_path in (
            ("normalized_period", expected_normalized_dir / f"{period}.json"),
            ("bank_allocations", expected_allocation_path),
        ):
            bindings = bindings_by_kind.get(kind) or []
            if len(bindings) != 1:
                raise SimplbooksError(f"Reconciliation {period} requires exactly one {kind} binding.")
            try:
                resolved = verify_file_binding(bindings[0], cwd=root)
            except ReferenceArtifactError as exc:
                raise SimplbooksError(f"Reconciliation {period} binding changed or is missing: {exc}") from exc
            if resolved.resolve() != expected_path.resolve():
                raise SimplbooksError(
                    f"Reconciliation {period} {kind} binding does not resolve to the expected artifact."
                )
        coverage = payload.get("bank_coverage") or {}
        totals["physical_bank_row_count"] += int(coverage.get("physical_bank_row_count") or 0)
        totals["allocated_row_count"] += int(coverage.get("allocated_row_count") or 0)
        totals["uncovered_row_count"] += int(coverage.get("unallocated_row_count") or 0)
        movement_ids = coverage.get("clearing_movement_record_ids")
        resolved_ids = coverage.get("resolved_clearing_record_ids")
        unresolved_ids = coverage.get("unresolved_clearing_record_ids")
        if not all(isinstance(values, list) for values in (movement_ids, resolved_ids, unresolved_ids)):
            raise SimplbooksError(f"Reconciliation {period} lacks structured clearing coverage lists.")
        movement_set = set(map(str, movement_ids))
        resolved_set = set(map(str, resolved_ids))
        unresolved_set = set(map(str, unresolved_ids))
        if int(coverage.get("clearing_movement_count") or 0) != len(movement_set):
            raise SimplbooksError(f"Reconciliation {period} clearing movement count does not match its IDs.")
        if int(coverage.get("resolved_clearing_count") or 0) != len(resolved_set):
            raise SimplbooksError(f"Reconciliation {period} resolved clearing count does not match its IDs.")
        if int(coverage.get("unresolved_clearing_count") or 0) != len(unresolved_set):
            raise SimplbooksError(f"Reconciliation {period} unresolved clearing count does not match its IDs.")
        if resolved_set & unresolved_set or resolved_set | unresolved_set != movement_set:
            raise SimplbooksError(f"Reconciliation {period} clearing resolution IDs do not partition movement IDs.")
        totals["clearing_movement_count"] += len(movement_set)
        totals["unresolved_clearing_count"] += len(unresolved_set)
    return totals


def summarize_action_artifacts(
    *, company_dir: Path, year: int, periods: list[str] | None = None
) -> dict[str, Any]:
    def load_action_yaml(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            run = ORIGINAL_SUBPROCESS_RUN(
                ["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load_file(ARGV[0]))", str(path)],
                capture_output=True,
                check=True,
                text=True,
            )
            loaded = json.loads(run.stdout)
        if not isinstance(loaded, dict):
            raise SimplbooksError(f"Action artifact {path} must contain an object.")
        return loaded

    foreign_action_count = 0
    ecb_provenance_count = 0
    suppressed_document_count = 0
    blocking_dependency_count = 0
    source_reference_count = 0
    canonical_source_reference_count = 0
    unsafe_paypal_stripe_count = 0
    policy_mapping_mismatch_count = 0
    raw_source_reference_count = 0
    canonical_raw_source_reference_count = 0
    supplier_credit_totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    suppressed_external_refs: list[str] = []
    blocking_dependencies: list[dict[str, str]] = []
    manual_inventory_status: str | None = None
    manual_inventory_remnant_verified = False
    manual_inventory_error: str | None = None
    manual_inventory_action_loaded = False

    policy_path = company_dir / "artifacts" / "posting_policy.json"
    posting_policy = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    expected_woo_contact = ((posting_policy.get("contacts") or {}).get("sales") or {}).get("woo")
    stripe_contact = ((posting_policy.get("contacts") or {}).get("processors") or {}).get("stripe")
    allowed_bank_accounts = {str(value) for value in (posting_policy.get("bank_accounts") or {}).values()}
    # A reviewed processor account is the one cash target statement-import mode still
    # allows the API, so it belongs beside the bank accounts rather than counting as a
    # mismatch on every settled month.
    allowed_bank_accounts |= {
        str(value)
        for value in ((posting_policy.get("cash_posting") or {}).get("processor_income_account_ids") or {}).values()
        if str(value)
    }

    actions_dir = company_dir / "artifacts" / "actions"
    manual_action_path = actions_dir / f"{year}-inventory-manual.json"
    if manual_action_path.exists():
        try:
            manual_action = load_manual_inventory_actions(manual_action_path)
            manual_inventory_status = str(manual_action["status"])
            manual_inventory_action_loaded = True
            evidence_path = company_dir / "artifacts" / "discovery" / f"{year}-inventory-remnant-verification.json"
            if evidence_path.exists():
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                if not isinstance(evidence, dict):
                    raise SimplbooksError("Manual inventory remnant evidence must contain an object.")
                required_evidence_fields = {
                    "action_type",
                    "effective_date",
                    "article_id",
                    "warehouse_id",
                    "expected_remnant_after",
                    "verified_at",
                    "remnant_response",
                }
                same_action = required_evidence_fields <= evidence.keys() and (
                    str(evidence["action_type"]) == "manual_inventory_writeoff"
                    and str(evidence["effective_date"]) == str(manual_action["effective_date"])
                    and str(evidence["article_id"]) == str(manual_action["article_id"])
                    and str(evidence["warehouse_id"]) == str(manual_action["warehouse_id"])
                    and Decimal(str(evidence["expected_remnant_after"]))
                    == Decimal(str(manual_action["expected_remnant_after"]))
                )
                manual_inventory_remnant_verified = bool(evidence.get("verified_at")) and same_action and not evaluate_inventory_action(
                    manual_action, evidence.get("remnant_response") or {}
                )
        except (SimplbooksError, OSError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
            if not manual_inventory_action_loaded:
                manual_inventory_status = "invalid"
            manual_inventory_error = str(exc)
    action_paths = (
        [actions_dir / f"{period}.yaml" for period in periods]
        if periods is not None
        else (sorted(actions_dir.glob(f"{year}-??.yaml")) if actions_dir.exists() else [])
    )
    for path in action_paths:
        if not path.exists():
            raise SimplbooksError(f"Expected action artifact is missing from annual summary: {path}")
        batch = load_action_yaml(path)
        period = str(batch.get("period") or path.stem)
        suppressed_document_count += len(batch.get("already_present") or [])
        suppressed_external_refs.extend(
            str(item.get("external_ref"))
            for item in batch.get("already_present") or []
            if item.get("external_ref")
        )
        blocking_dependency_count += sum(
            1 for item in batch.get("unresolved_dependencies") or [] if item.get("blocking")
        )
        blocking_dependencies.extend(
            {
                "kind": str(item.get("kind") or ""),
                "label": str(item.get("label") or ""),
                "family": str(item.get("family") or ""),
            }
            for item in batch.get("unresolved_dependencies") or []
            if item.get("blocking")
        )
        for action in batch.get("actions") or []:
            payload = action.get("payload") or {}
            action_type = str(action.get("action_type") or "")
            label = str(
                (payload.get("summary_scope") or {}).get("channel_or_source")
                or payload.get("vendor_hint")
                or payload.get("counterparty_hint")
                or ""
            )
            contact_id = str((payload.get("counterparty") or {}).get("contact_id") or "")
            if label == "paypal" and stripe_contact is not None and contact_id == str(stripe_contact):
                unsafe_paypal_stripe_count += 1
            if label == "woo" and expected_woo_contact is not None and contact_id != str(expected_woo_contact):
                policy_mapping_mismatch_count += 1
            if action_type in {"create_incoming_summary", "create_payment_summary"} and allowed_bank_accounts:  # noqa: SIM102
                if str(payload.get("bank_account_id") or "") not in allowed_bank_accounts:
                    policy_mapping_mismatch_count += 1
            currency = str(payload.get("currency") or "EUR").upper()
            if currency != "EUR":
                foreign_action_count += 1
                if payload.get("currency_rate_provider") == "ECB" and payload.get("currency_rate") not in (None, ""):
                    ecb_provenance_count += 1
            if action.get("action_type") == "create_purchase_credit_summary":
                amount = Decimal(str((payload.get("totals") or {}).get("gross_amount") or 0))
                supplier_credit_totals[period][currency] += amount
            for source_ref in action.get("source_refs") or []:
                source_reference_count += 1
                ref_path = str(source_ref.get("path") or "")
                relative_prefix = f"companies/{company_dir.name}/artifacts/normalized/"
                if ref_path.startswith(str(company_dir / "artifacts" / "normalized")) or ref_path.startswith(relative_prefix):  # noqa: PIE810
                    canonical_source_reference_count += 1

    normalized_dir = company_dir / "artifacts" / "normalized"
    normalized_paths = (
        [normalized_dir / f"{period}.json" for period in periods]
        if periods is not None
        else (sorted(normalized_dir.glob(f"{year}-??.json")) if normalized_dir.exists() else [])
    )
    for path in normalized_paths:
        if not path.exists():
            raise SimplbooksError(f"Expected normalized artifact is missing from annual summary: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        for source in payload.get("sources") or []:
            raw_source_reference_count += 1
            source_path = str(source.get("path") or "")
            if source_path.startswith(str(company_dir / "source")) or source_path.startswith(  # noqa: PIE810
                f"companies/{company_dir.name}/source/"
            ):
                canonical_raw_source_reference_count += 1

    return {
        "foreign_action_count": foreign_action_count,
        "ecb_provenance_count": ecb_provenance_count,
        "supplier_credit_totals": {
            period: {currency: float(amount) for currency, amount in sorted(totals.items())}
            for period, totals in sorted(supplier_credit_totals.items())
        },
        "suppressed_document_count": suppressed_document_count,
        "blocking_dependency_count": blocking_dependency_count,
        "source_reference_count": source_reference_count,
        "canonical_source_reference_count": canonical_source_reference_count,
        "raw_source_reference_count": raw_source_reference_count,
        "canonical_raw_source_reference_count": canonical_raw_source_reference_count,
        "unsafe_paypal_stripe_count": unsafe_paypal_stripe_count,
        "policy_mapping_mismatch_count": policy_mapping_mismatch_count,
        "suppressed_external_refs": sorted(set(suppressed_external_refs)),
        "blocking_dependencies": blocking_dependencies,
        "manual_inventory_status": manual_inventory_status,
        "manual_inventory_remnant_verified": manual_inventory_remnant_verified,
        "manual_inventory_error": manual_inventory_error,
    }


def reference_acceptance_issues(
    reference_summary: dict[str, Any], *, expectations: dict[str, Any] | None = None
) -> list[str]:
    issues: list[str] = []
    if reference_summary["foreign_action_count"] != reference_summary["ecb_provenance_count"]:
        issues.append("Not every foreign-currency action has verified ECB provenance.")
    if reference_summary["source_reference_count"] != reference_summary["canonical_source_reference_count"]:
        issues.append("Not every action source reference is company-local and canonical.")
    if reference_summary["raw_source_reference_count"] != reference_summary["canonical_raw_source_reference_count"]:
        issues.append("Not every normalized source manifest entry is under the company source directory.")
    if reference_summary["unsafe_paypal_stripe_count"]:
        issues.append("One or more PayPal actions reuse the Stripe contact.")
    if reference_summary["policy_mapping_mismatch_count"]:
        issues.append("One or more cash/Woo actions differ from the posting policy.")
    manual_status = reference_summary.get("manual_inventory_status")
    manual_verified = bool(reference_summary.get("manual_inventory_remnant_verified"))
    if manual_status == "required":
        issues.append("Manual inventory write-off remains required before year-close readiness can pass.")
    elif manual_status in {"completed", "verified"} and not manual_verified:
        issues.append("Manual inventory write-off lacks matching dated remnant verification.")
    elif manual_status == "invalid":
        issues.append(
            f"Manual inventory action is invalid: {reference_summary.get('manual_inventory_error') or 'unknown error'}"
        )
    expectations = expectations or {}
    actual_credit_totals = reference_summary.get("supplier_credit_totals") or {}
    for period, expected_currencies in (expectations.get("supplier_credit_totals") or {}).items():
        for currency, expected_amount in expected_currencies.items():
            actual_amount = (actual_credit_totals.get(period) or {}).get(currency)
            if Decimal(str(actual_amount or 0)) != Decimal(str(expected_amount)):
                issues.append(f"Expected supplier credit {period} {currency} {expected_amount}, found {actual_amount or 0}.")
    actual_suppressed = set(reference_summary.get("suppressed_external_refs") or [])
    for external_ref in expectations.get("suppressed_external_refs") or []:
        if str(external_ref) not in actual_suppressed:
            issues.append(f"Expected existing document {external_ref} to be suppressed from draft creation.")
    allowed_dependencies = {
        (str(item.get("kind") or ""), str(item.get("label") or ""), str(item.get("family") or ""))
        for item in expectations.get("allowed_blocking_dependencies") or []
    }
    unexpected_dependencies = [
        item
        for item in reference_summary.get("blocking_dependencies") or []
        if (str(item.get("kind") or ""), str(item.get("label") or ""), str(item.get("family") or ""))
        not in allowed_dependencies
    ]
    if unexpected_dependencies:
        issues.append(f"Found {len(unexpected_dependencies)} unexpected blocking dependency occurrence(s).")
    return issues


def build_step_command(
    *,
    python_executable: str,
    company_dir: Path,
    period: str,
    step_name: str,
    script_name: str,
    source_dir: Path | None,
    force_build: bool,
    woo_tax_allocation: Path | None = None,
    bank_allocations: Path | None = None,
) -> list[str]:
    cmd = [python_executable, f"scripts/{script_name}", "--company-dir", str(company_dir), "--period", period]
    if step_name == "bookprep" and source_dir is not None:
        cmd.extend(["--source-dir", str(source_dir)])
    if step_name == "bookprep" and woo_tax_allocation is not None:
        cmd.extend(["--woo-tax-allocation", str(woo_tax_allocation)])
    if step_name == "bookbuilder":
        year = period[:4]
        cmd.extend(
            [
                "--posting-policy",
                str(company_dir / "artifacts" / "posting_policy.json"),
                "--exchange-rates",
                str(company_dir / "artifacts" / "reference" / f"ecb-rates-{year}.json"),
                "--discovery-overview",
                str(company_dir / "artifacts" / "discovery" / f"{year}-overview.json"),
            ]
        )
        if force_build:
            cmd.append("--force")
    if step_name in {"bookrecon", "bookbuilder", "bookchecker"} and bank_allocations is not None:
        cmd.extend(["--bank-allocations", str(bank_allocations)])
    if step_name == "booksend":
        cmd.extend(["--mode", "dry-run"])
    return cmd


def run_phase_from_evidence(
    *,
    company_dir: Path,
    year: int,
    statement_import_mode: bool,
    master_data_resolved: bool,
    documents_ready: bool,
) -> str:
    """Read the year's post-import evidence and report the phase it supports."""
    ledger_evidence = load_optional_json(
        company_dir / "artifacts" / "ledger" / f"{year}-ledger-evidence.json"
    )
    inventory_audit = load_optional_json(
        company_dir / "artifacts" / "audits" / f"{year}-inventory-equation.json"
    )
    fx_revaluation = load_optional_json(
        company_dir / "artifacts" / "audits" / f"{year}-fx-revaluation.json"
    )
    return resolve_run_phase(
        YearGates(
            statement_import_mode=statement_import_mode,
            master_data_resolved=master_data_resolved,
            documents_ready=documents_ready,
            ledger_evidence_status=str(ledger_evidence.get("status")) if ledger_evidence else None,
            inventory_audit_status=str(inventory_audit.get("status")) if inventory_audit else None,
            fx_revaluation_settled=fx_revaluation_state(fx_revaluation or None)["settled"],
        )
    )


def normalize_all_periods(
    *,
    periods: list[str],
    command_for: Any,
    cwd: Path,
    continue_on_error: bool,
) -> None:
    """Normalize every period before anything derived from the normalized data is built."""
    for period in periods:
        run = subprocess.run(command_for(period), cwd=cwd, capture_output=True, text=True)  # noqa: PLW1510
        if run.returncode != 0 and not continue_on_error:
            raise SimplbooksError(
                f"Normalization failed for {period} before the annual plan could be built: "
                + (run.stderr or run.stdout or "unknown error").strip()
            )


def normalize_then_plan(
    *,
    periods: list[str],
    command_for: Any,
    plan: Any,
    cwd: Path,
    continue_on_error: bool,
) -> dict[str, Any]:
    """Normalize every period, then build the annual plan from what that produced.

    The plan is derived from the normalized artifacts, so building it first would describe
    the previous run's normalization -- and on a fresh company there would be nothing to
    describe at all.
    """
    normalize_all_periods(
        periods=periods, command_for=command_for, cwd=cwd, continue_on_error=continue_on_error,
    )
    return plan()


def generate_annual_statement_plan(
    *,
    company_dir: Path,
    year: int,
    python_executable: str,
    cwd: Path,
    continue_on_error: bool,
) -> dict[str, Any]:
    """Build the annual plan before any month, so no batch is built against a stale one."""
    command = [
        python_executable,
        str(Path("scripts") / "statement_import_plan.py"),
        "--company-dir",
        str(company_dir),
        "--year",
        str(year),
    ]
    run = subprocess.run(command, cwd=cwd, capture_output=True, text=True)  # noqa: PLW1510
    if run.returncode != 0 and not continue_on_error:
        raise SimplbooksError(
            "Annual statement-import plan generation failed; no month is built against a stale plan: "
            + (run.stderr or run.stdout or "unknown error").strip()
        )
    return {
        "step": "statement_import_plan",
        "ok": run.returncode == 0,
        "returncode": run.returncode,
        "summary": parse_json_output(run.stdout),
    }


def run_full_year_dry_run(
    *,
    company_dir: Path,
    year: int,
    source_dir: Path | None,
    python_executable: str,
    continue_on_error: bool,
    force_build: bool,
    cwd: Path,
) -> dict[str, Any]:
    company_name = resolve_company_name(company_dir=str(company_dir))
    woo_tax_allocation = validate_woo_tax_preflight(company_dir, year, source_dir)
    bank_allocations = company_dir / "artifacts" / "bank" / f"{year}-allocations.json"
    months: list[dict[str, Any]] = []
    api_calls: list[dict[str, Any]] = []
    successful_period_states: dict[str, str] = {}
    overall_success = True

    statement_import_mode = statement_import_year(company_dir)
    target_periods = periods_for_year(year)

    plan_step = (
        normalize_then_plan(
            periods=target_periods,
            command_for=lambda period: build_step_command(
                python_executable=python_executable, company_dir=company_dir, period=period,
                step_name="bookprep", script_name="bookprep.py", source_dir=source_dir,
                force_build=force_build, woo_tax_allocation=woo_tax_allocation,
                bank_allocations=bank_allocations,
            ),
            plan=lambda: generate_annual_statement_plan(
                company_dir=company_dir, year=year, python_executable=python_executable,
                cwd=cwd, continue_on_error=continue_on_error,
            ),
            cwd=cwd,
            continue_on_error=continue_on_error,
        )
        if statement_import_mode
        else None
    )
    overall_success = overall_success and (plan_step is None or plan_step["ok"])

    for period in target_periods:
        submission_state = submitted_month_state(company_dir=company_dir, period=period)
        if submission_state == "submitted":
            months.append({"period": period, "ok": True, "status": "skipped_submitted", "steps": []})
            successful_period_states[period] = "skipped_submitted"
            continue
        if submission_state == "partial_submission":
            raise SimplbooksError(
                f"Period {period} has a partial write submission and must be resumed without regenerating its YAML."
            )
        step_results: list[dict[str, Any]] = []
        month_success = True
        month_steps = [
            (step_name, script_name)
            for step_name, script_name in STEP_SPECS
            # Normalization already ran for every month before the annual plan was built.
            if not (statement_import_mode and step_name == "bookprep")
        ]
        for step_name, script_name in month_steps:
            cmd = build_step_command(
                python_executable=python_executable,
                company_dir=company_dir,
                period=period,
                step_name=step_name,
                script_name=script_name,
                source_dir=source_dir,
                force_build=force_build,
                woo_tax_allocation=woo_tax_allocation,
                bank_allocations=bank_allocations,
            )
            run = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)  # noqa: PLW1510
            step_summary = {
                "step": step_name,
                "script": script_name,
                "command": cmd,
                "returncode": run.returncode,
                "stdout": parse_json_output(run.stdout) or run.stdout.strip() or None,
                "stderr": run.stderr.strip() or None,
            }
            step_results.append(step_summary)
            if step_name == "booksend":
                api_calls.extend(extract_api_calls(period=period, step_summary=step_summary))
            checker_failed = (
                step_name == "bookchecker"
                and isinstance(step_summary["stdout"], dict)
                and step_summary["stdout"].get("result") != "pass"
            )
            if run.returncode != 0 or checker_failed:
                month_success = False
                overall_success = False
                break
        months.append({"period": period, "ok": month_success, "status": "processed", "steps": step_results})
        if month_success:
            successful_period_states[period] = "processed"
        if not month_success and not continue_on_error:
            break

    aggregated_periods = [period for period in target_periods if period in successful_period_states]
    unprocessed_periods = [period for period in target_periods if period not in successful_period_states]
    reference_summary = summarize_action_artifacts(
        company_dir=company_dir,
        year=year,
        periods=aggregated_periods,
    )
    bank_reconciliation_summary = summarize_bank_reconciliation_artifacts(
        company_dir=company_dir,
        year=year,
        expected_periods=aggregated_periods,
        period_states=successful_period_states,
        cwd=cwd,
    )
    policy_path = company_dir / "artifacts" / "posting_policy.json"
    posting_policy = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    expectations = ((posting_policy.get("year_expectations") or {}).get(str(year)) or {})
    acceptance_issues = reference_acceptance_issues(reference_summary, expectations=expectations)
    if unprocessed_periods:
        acceptance_issues.append(
            "Periods not successfully processed or frozen: " + ", ".join(unprocessed_periods) + "."
        )
    if len(months) != len(target_periods):
        acceptance_issues.append(f"Expected {len(target_periods)} processed months, found {len(months)}.")
    overall_success = overall_success and not acceptance_issues
    phase = run_phase_from_evidence(
        company_dir=company_dir,
        year=year,
        statement_import_mode=statement_import_mode,
        master_data_resolved=not unprocessed_periods,
        documents_ready=overall_success,
    )
    return {
        "company_dir": str(company_dir),
        "company_name": company_name,
        "company_slug": company_dir.name,
        "year": year,
        "python_executable": python_executable,
        "source_dir": str(source_dir) if source_dir is not None else None,
        "mode": "dry-run",
        "force_build": force_build,
        "continue_on_error": continue_on_error,
        "overall_success": overall_success,
        "aggregated_periods": aggregated_periods,
        "unprocessed_periods": unprocessed_periods,
        "reference_summary": reference_summary,
        "bank_reconciliation_summary": bank_reconciliation_summary,
        "acceptance_issues": acceptance_issues,
        "cash_posting_mode": "statement_import" if statement_import_mode else "api",
        "phase": phase,
        "statement_import_plan_step": plan_step,
        "bank_api_cash_action_count": count_bank_api_cash_actions(company_dir, year=year),
        "api_calls": api_calls,
        "months": months,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the full month-by-month Simplbooks pipeline in dry-run mode for one year")
    parser.add_argument("--company-dir", required=True, help="Company folder, e.g. companies/example")
    parser.add_argument("--year", required=True, type=int, help="Target year, e.g. 2024")
    parser.add_argument("--source-dir", help="Optional source directory override for bookprep")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used to invoke the month scripts")
    parser.add_argument("--output", help="Optional summary JSON path")
    parser.add_argument("--continue-on-error", action="store_true", help="Keep running later months after a failed month")
    parser.add_argument("--force-build", action="store_true", help="Pass --force to bookbuilder even if recon blocks a month")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    company_dir = Path(args.company_dir)
    source_dir = Path(args.source_dir) if args.source_dir else None
    summary = run_full_year_dry_run(
        company_dir=company_dir,
        year=args.year,
        source_dir=source_dir,
        python_executable=args.python,
        continue_on_error=args.continue_on_error,
        force_build=args.force_build,
        cwd=Path.cwd(),
    )
    output_path = Path(args.output) if args.output else default_output_path(company_dir, args.year)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["overall_success"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
