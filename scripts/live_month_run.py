#!/usr/bin/env python3
from __future__ import annotations  # noqa: I001

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable  # noqa: UP035

import booksend
from bank_allocations import BankAllocationError, load_bank_allocations, period_allocations
from full_year_dry_run import parse_json_output, submitted_month_state
from posting_policy import (
    PostingPolicyError,
    accepted_checker_warnings,
    cash_posting_mode,
    load_posting_policy,
    posting_scope_first_period,
)
from reference_artifacts import ReferenceArtifactError, verify_file_binding
from simplbooks_api import SimplbooksError
from simplbooks_api import resolve_company_id
from statement_import_evidence import (
    StatementImportEvidenceError,
    discovery_cash_evidence_errors,
    load_bound_evidence,
)


CommandRunner = Callable[..., Any]
ApprovalCheckpoint = Callable[[Path], None]


def _run_step(*, command: list[str], cwd: Path, runner: CommandRunner, label: str) -> dict[str, Any]:
    completed = runner(command, cwd=cwd, capture_output=True, text=True)
    if not hasattr(completed, "returncode"):
        raise SimplbooksError(f"{label} command runner returned no process result.")
    payload = parse_json_output(str(completed.stdout or ""))
    if int(completed.returncode) != 0:
        detail = str(completed.stderr or "").strip() or str(completed.stdout or "").strip()
        raise SimplbooksError(f"{label} failed: {detail or f'exit {completed.returncode}'}")
    if payload is None:
        raise SimplbooksError(f"{label} did not return a JSON object summary.")
    return payload


def _verify_checker_summary(
    summary: dict[str, Any], *, label: str, accepted: list[str] | None = None
) -> None:
    """Refuse to proceed on a warning nobody has reviewed.

    Some warnings are structural and never clear, so the company declares those in
    `accepted_checker_warnings` and they are matched as substrings. Anything else
    still stops the run: this narrows the gate, it does not remove it.
    """
    if summary.get("result") != "pass" or int(summary.get("error_count") or 0):
        raise SimplbooksError(f"{label} did not pass without errors.")
    warning_count = int(summary.get("warning_count") or 0)
    if not warning_count:
        return
    warnings = summary.get("warnings")
    if not isinstance(warnings, list) or len(warnings) != warning_count:
        # Fail closed: without the texts a reviewed warning cannot be told apart
        # from one nobody has seen.
        raise SimplbooksError(
            f"{label} reported {warning_count} warning(s) without their texts; cannot reconcile."
        )
    declared = accepted or []
    unreviewed = [
        str((item or {}).get("summary") or "")
        for item in warnings
        if not any(pattern in str((item or {}).get("summary") or "") for pattern in declared)
    ]
    if unreviewed:
        raise SimplbooksError(
            f"{label} returned {len(unreviewed)} unreviewed warning(s): " + "; ".join(unreviewed)
        )


def _verify_check_binding(*, action_path: Path, check_path: Path) -> None:
    batch = booksend.load_yaml(action_path)
    report = booksend.load_check_report(check_path)
    if report.get("result") != "pass":
        raise SimplbooksError("Checker report does not record a pass result.")
    if report.get("batch_id") != str(batch.get("batch_id") or ""):
        raise SimplbooksError("Checker report batch binding does not match the action YAML.")
    if report.get("action_file_sha256") != booksend.file_sha256(action_path):
        raise SimplbooksError("Checker report SHA binding does not match the action YAML.")


def _approval_only_change(before: dict[str, Any], after: dict[str, Any]) -> bool:
    expected = copy.deepcopy(before)
    expected["approval_status"] = "approved"
    return expected == after


def _dependencies_are_resolved(batch: dict[str, Any]) -> bool:
    # Mirrors booksend.py: in a statement-import batch the API sends no cash for these
    # rows, so a pending proof stops nothing. The annual plan carries the row instead.
    statement_import_batch = str(batch.get("cash_posting_mode") or "") == "statement_import"
    for dependency in batch.get("unresolved_dependencies") or []:
        if not isinstance(dependency, dict):
            return False
        if dependency.get("blocking") is True:
            return False
        if dependency.get("kind") == "manual_statement_import_financial_transaction":
            if statement_import_batch:
                continue
            proof = dependency.get("statement_import_proof") or {}
            if (
                dependency.get("blocking") is not False
                or not isinstance(proof, dict)
                or proof.get("status") != "verified"
            ):
                return False
    return True


def _allocation_targets(allocation: dict[str, Any]) -> list[dict[str, Any]]:
    targets = []
    target = allocation.get("target")
    if isinstance(target, dict):
        targets.append(target)
    for part in allocation.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("target"), dict):
            targets.append(part["target"])
    return targets


def _required_discovery_years(
    *, company_dir: Path, period: str, allocations: list[dict[str, Any]]
) -> list[int]:
    years = {int(period[:4])}
    existing_targets = {
        (str(target.get("simplbooks_id")), str(target.get("document_type") or ""))
        for allocation in allocations
        for target in _allocation_targets(allocation)
        if target.get("simplbooks_id") not in (None, "")
    }
    for allocation in allocations:
        for target in _allocation_targets(allocation):
            for value in target.values():
                if isinstance(value, str):
                    match = re.search(r"-(\d{4})-\d{2}-", value)
                    if match:
                        years.add(int(match.group(1)))
    found_existing: set[tuple[str, str]] = set()
    if existing_targets:
        for path in sorted((company_dir / "artifacts" / "discovery").glob("[0-9][0-9][0-9][0-9]-overview.json")):
            try:
                overview = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            matching = {
                (str(item.get("simplbooks_id") or ""), str(item.get("document_type") or ""))
                for item in overview.get("document_index") or []
                if isinstance(item, dict)
            } & existing_targets
            if matching:
                years.add(int(path.name[:4]))
                found_existing.update(matching)
        missing = sorted(existing_targets - found_existing)
        if missing:
            raise SimplbooksError(
                "Could not infer discovery year for existing SimplBooks target(s): "
                + ", ".join(f"{kind}:{item_id}" for item_id, kind in missing)
            )
    return sorted(years)


def _posting_scope_first_period(company_dir: Path) -> str | None:
    """Read the declared posting scope, so an out-of-scope year cannot enter the chain."""
    policy_path = company_dir / "artifacts" / "posting_policy.json"
    if not policy_path.exists():
        return None
    return posting_scope_first_period(load_posting_policy(policy_path))


def _accepted_checker_warnings(company_dir: Path) -> list[str]:
    """Warning texts this company has reviewed. Fail closed: unreadable policy accepts none."""
    policy_path = company_dir / "artifacts" / "posting_policy.json"
    if not policy_path.exists():
        return []
    try:
        return accepted_checker_warnings(load_posting_policy(policy_path))
    except (PostingPolicyError, SimplbooksError, ValueError, OSError):
        return []


def _statement_import_company(company_dir: Path) -> bool:
    """Whether this company posts cash by manual statement import.

    Fail closed: an absent or unreadable policy is treated as API mode, which keeps
    every existing guard in force.
    """
    policy_path = company_dir / "artifacts" / "posting_policy.json"
    if not policy_path.exists():
        return False
    try:
        return cash_posting_mode(load_posting_policy(policy_path)) == "statement_import"
    except (PostingPolicyError, SimplbooksError, ValueError, OSError):
        return False


def _load_live_allocations(*, company_dir: Path, period: str) -> list[dict[str, Any]]:
    year = period[:4]
    normalized_paths = sorted((company_dir / "artifacts" / "normalized").glob(f"{year}-[0-1][0-9].json"))
    allocation_path = company_dir / "artifacts" / "bank" / f"{year}-allocations.json"
    try:
        payload = load_bank_allocations(allocation_path, normalized_year_paths=normalized_paths)
        selected = list(period_allocations(payload, period).values())
    except BankAllocationError as exc:
        raise SimplbooksError(f"Live bank allocation preflight failed: {exc}") from exc
    statement_import = _statement_import_company(company_dir)
    for allocation in selected:
        manual = str(allocation.get("disposition") or "") in {
            "bank_fee_payment", "expense_reimbursement_payment", "clearing_transfer"
        }
        manual = manual or any(
            str(part.get("disposition") or "") in {
                "bank_fee_payment", "expense_reimbursement_payment", "clearing_transfer"
            }
            for part in allocation.get("parts") or [] if isinstance(part, dict)
        )
        if manual and not statement_import:
            proof = (allocation.get("target") or {}).get("statement_import_proof")
            if not isinstance(proof, dict) or proof.get("status") != "verified":
                raise SimplbooksError(
                    "Manual statement import and reviewed statement_import_proof must exist before live discovery/build."
                )
    return selected


def _validate_refreshed_cash_evidence(
    *, evidence: dict[str, Any], discovery_payloads: list[dict[str, Any]],
    expected_company_id: str,
) -> None:
    if str(evidence.get("company_id") or "") != str(expected_company_id):
        raise SimplbooksError("Statement-import evidence company identity does not match live company metadata.")
    errors = discovery_cash_evidence_errors(
        evidence, discovery_payloads=discovery_payloads, require_fresh=True,
    )
    if errors:
        raise SimplbooksError(errors[0])


def _validate_live_statement_evidence(
    *, allocations: list[dict[str, Any]], discovery_paths: list[Path],
    company_dir: Path, cwd: Path,
) -> None:
    proofs = [
        target.get("statement_import_proof")
        for allocation in allocations
        for target in _allocation_targets(allocation)
        if isinstance(target.get("statement_import_proof"), dict)
        and target.get("statement_import_proof", {}).get("status") == "verified"
    ]
    if not proofs:
        return
    try:
        company_id = resolve_company_id(None, company_dir=str(company_dir))
        discovery_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in discovery_paths]
    except (OSError, json.JSONDecodeError, SimplbooksError) as exc:
        raise SimplbooksError(f"Cannot validate refreshed statement-import evidence: {exc}") from exc
    for proof in proofs:
        try:
            evidence = load_bound_evidence(proof.get("evidence_binding"), cwd=cwd)
        except StatementImportEvidenceError as exc:
            raise SimplbooksError(f"Statement-import evidence binding is invalid: {exc}") from exc
        _validate_refreshed_cash_evidence(
            evidence=evidence, discovery_payloads=discovery_payloads,
            expected_company_id=company_id,
        )


def _interactive_approval_checkpoint(action_path: Path) -> None:
    input(
        f"Review {action_path}, change only approval_status from draft to approved, "
        "then press Enter to continue: "
    )


def _verify_builder_output(
    *, summary: dict[str, Any], action_path: Path, discovery_paths: list[Path], cwd: Path
) -> dict[str, Any]:
    if summary.get("approval_status") != "draft":
        raise SimplbooksError("Action builder must report approval_status draft for a live run.")
    reported_output = Path(str(summary.get("output") or ""))
    if not str(summary.get("output") or "").strip():
        raise SimplbooksError("Action builder did not report its output path.")
    reported_resolved = reported_output if reported_output.is_absolute() else cwd / reported_output
    if reported_resolved.resolve() != action_path.resolve():
        raise SimplbooksError("Action builder output does not resolve to the expected action path.")
    if not action_path.exists():
        raise SimplbooksError(f"Action build did not create {action_path}.")
    batch = booksend.load_yaml(action_path)
    if str(batch.get("approval_status") or "") != "draft":
        raise SimplbooksError("A freshly rebuilt live batch must be draft before checking.")
    discovery_bindings = [
        item
        for item in batch.get("reference_artifacts") or []
        if isinstance(item, dict) and item.get("kind") == "discovery_overview"
    ]
    actual_paths = []
    for binding in discovery_bindings:
        try:
            bound_path = verify_file_binding(binding, cwd=cwd)
        except ReferenceArtifactError as exc:
            raise SimplbooksError(f"Fresh discovery binding is missing or changed: {exc}") from exc
        actual_paths.append(bound_path.resolve())
    expected_paths = [path.resolve() for path in discovery_paths]
    if sorted(map(str, actual_paths)) != sorted(map(str, expected_paths)) or len(actual_paths) != len(set(actual_paths)):
        raise SimplbooksError("Freshly rebuilt batch must bind every newly refreshed discovery artifact exactly once.")
    return batch


def run_live_month(
    *,
    company_dir: Path,
    period: str,
    python_executable: str,
    cwd: Path,
    confirm_write: bool,
    run_command: CommandRunner | None = None,
    approval_checkpoint: ApprovalCheckpoint | None = None,
) -> dict[str, Any]:
    if not confirm_write:
        raise SimplbooksError("Live month orchestration requires explicit --confirm-write.")
    if not re.fullmatch(r"\d{4}-\d{2}", period) or not 1 <= int(period[5:]) <= 12:
        raise SimplbooksError(f"Period must use YYYY-MM with a valid month: {period!r}")

    runner = run_command or subprocess.run
    checkpoint = approval_checkpoint or _interactive_approval_checkpoint
    action_path = company_dir / "artifacts" / "actions" / f"{period}.yaml"
    check_path = company_dir / "artifacts" / "actions" / f"{period}.check.md"
    allocation_path = company_dir / "artifacts" / "bank" / f"{period[:4]}-allocations.json"

    state = submitted_month_state(company_dir=company_dir, period=period)
    if state == "submitted":
        raise SimplbooksError(f"Period {period} is already submitted and immutable.")
    if state == "partial_submission":
        raise SimplbooksError(f"Period {period} has a partial submission; resume it without regeneration.")
    if action_path.exists():
        booksend.validate_predecessor_submission(
            action_path=action_path,
            period=period,
            first_period=_posting_scope_first_period(company_dir),
        )

    live_allocations = _load_live_allocations(company_dir=company_dir, period=period)
    discovery_years = _required_discovery_years(
        company_dir=company_dir, period=period, allocations=live_allocations
    )
    discovery_paths = [
        company_dir / "artifacts" / "discovery" / f"{year}-overview.json"
        for year in discovery_years
    ]

    commands: list[list[str]] = []
    for discovery_year, discovery_path in zip(discovery_years, discovery_paths):
        discovery_command = [
            python_executable, "scripts/examine_simplbooks_year.py",
            "--company-dir", str(company_dir), "--year", str(discovery_year),
            "--output", str(discovery_path),
        ]
        commands.append(discovery_command)
        _run_step(command=discovery_command, cwd=cwd, runner=runner, label=f"Discovery refresh {discovery_year}")

    _validate_live_statement_evidence(
        allocations=live_allocations, discovery_paths=discovery_paths,
        company_dir=company_dir, cwd=cwd,
    )

    builder_command = [
        python_executable, "scripts/bookbuilder.py", "--company-dir", str(company_dir),
        "--period", period,
        "--posting-policy", str(company_dir / "artifacts" / "posting_policy.json"),
        "--exchange-rates", str(company_dir / "artifacts" / "reference" / f"ecb-rates-{period[:4]}.json"),
        "--bank-allocations", str(allocation_path),
    ]
    for discovery_path in discovery_paths:
        builder_command.extend(["--discovery-overview", str(discovery_path)])
    commands.append(builder_command)
    builder_summary = _run_step(command=builder_command, cwd=cwd, runner=runner, label="Action build")
    _verify_builder_output(
        summary=builder_summary,
        action_path=action_path,
        discovery_paths=discovery_paths,
        cwd=cwd,
    )
    booksend.validate_predecessor_submission(
        action_path=action_path,
        period=period,
        first_period=_posting_scope_first_period(company_dir),
    )

    checker_command = [
        python_executable, "scripts/bookchecker.py", "--company-dir", str(company_dir),
        "--period", period,
        "--posting-policy", str(company_dir / "artifacts" / "posting_policy.json"),
        "--exchange-rates", str(company_dir / "artifacts" / "reference" / f"ecb-rates-{period[:4]}.json"),
        "--bank-allocations", str(allocation_path),
    ]
    commands.append(checker_command)
    reviewed_warnings = _accepted_checker_warnings(company_dir)
    first_check = _run_step(command=checker_command, cwd=cwd, runner=runner, label="Initial checker")
    _verify_checker_summary(first_check, label="Initial checker", accepted=reviewed_warnings)
    _verify_check_binding(action_path=action_path, check_path=check_path)

    before_approval = booksend.load_yaml(action_path)
    if str(before_approval.get("approval_status") or "") != "draft":
        raise SimplbooksError("A freshly rebuilt live batch must be draft before human approval.")
    checkpoint(action_path)
    approved_batch = booksend.load_yaml(action_path)
    if str(approved_batch.get("approval_status") or "") != "approved":
        raise SimplbooksError("Human approval checkpoint did not set approval_status to approved.")
    if not _approval_only_change(before_approval, approved_batch):
        raise SimplbooksError("Human checkpoint may change only approval_status to approved.")
    if not _dependencies_are_resolved(approved_batch):
        raise SimplbooksError("Action batch still contains an unresolved dependency.")

    commands.append(checker_command)
    final_check = _run_step(command=checker_command, cwd=cwd, runner=runner, label="Approved checker rerun")
    _verify_checker_summary(final_check, label="Approved checker rerun", accepted=reviewed_warnings)
    _verify_check_binding(action_path=action_path, check_path=check_path)
    booksend.validate_run_preconditions(
        action_batch=approved_batch,
        action_path=action_path,
        period=period,
        mode="write",
        confirm_write=True,
        check_report=booksend.load_check_report(check_path),
        check_path=check_path,
    )

    send_command = [
        python_executable, "scripts/booksend.py", "--company-dir", str(company_dir),
        "--period", period, "--mode", "write", "--confirm-write",
    ]
    commands.append(send_command)
    send_summary = _run_step(command=send_command, cwd=cwd, runner=runner, label="Confirmed write")
    if send_summary.get("approval_status") != "submitted":
        raise SimplbooksError("Confirmed booksend write did not finish with submitted status.")
    return {"period": period, "status": "submitted", "commands": commands, "booksend": send_summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one fail-closed, human-approved live SimplBooks month")
    parser.add_argument("--company-dir", required=True, help="Company folder, e.g. companies/example")
    parser.add_argument("--period", required=True, help="Target month in YYYY-MM format")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used for workflow scripts")
    parser.add_argument("--confirm-write", action="store_true", help="Required before any live orchestration begins")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_live_month(
        company_dir=Path(args.company_dir), period=args.period,
        python_executable=args.python, cwd=Path.cwd(), confirm_write=args.confirm_write,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SimplbooksError as exc:
        raise SystemExit(f"error: {exc}")
