#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import booksend
from full_year_dry_run import parse_json_output, submitted_month_state
from reference_artifacts import ReferenceArtifactError, verify_file_binding
from simplbooks_api import SimplbooksError


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


def _verify_checker_summary(summary: dict[str, Any], *, label: str) -> None:
    if summary.get("result") != "pass" or int(summary.get("error_count") or 0):
        raise SimplbooksError(f"{label} did not pass without errors.")
    warning_count = int(summary.get("warning_count") or 0)
    if warning_count:
        raise SimplbooksError(f"{label} returned {warning_count} unresolved warning(s).")


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
    for dependency in batch.get("unresolved_dependencies") or []:
        if not isinstance(dependency, dict):
            return False
        proof = dependency.get("proof") or {}
        if (
            dependency.get("kind") != "manual_statement_import_financial_transaction"
            or dependency.get("blocking") is not False
            or not isinstance(proof, dict)
            or proof.get("status") != "verified"
        ):
            return False
    return True


def _interactive_approval_checkpoint(action_path: Path) -> None:
    input(
        f"Review {action_path}, change only approval_status from draft to approved, "
        "then press Enter to continue: "
    )


def _verify_builder_output(
    *, summary: dict[str, Any], action_path: Path, discovery_path: Path, cwd: Path
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
    matching = []
    for binding in discovery_bindings:
        try:
            bound_path = verify_file_binding(binding, cwd=cwd)
        except ReferenceArtifactError as exc:
            raise SimplbooksError(f"Fresh discovery binding is missing or changed: {exc}") from exc
        if bound_path.resolve() == discovery_path.resolve():
            matching.append(binding)
    if len(matching) != 1:
        raise SimplbooksError("Freshly rebuilt batch must bind the newly refreshed discovery artifact exactly once.")
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
    discovery_path = company_dir / "artifacts" / "discovery" / f"{period[:4]}-overview.json"
    allocation_path = company_dir / "artifacts" / "bank" / f"{period[:4]}-allocations.json"

    state = submitted_month_state(company_dir=company_dir, period=period)
    if state == "submitted":
        raise SimplbooksError(f"Period {period} is already submitted and immutable.")
    if state == "partial_submission":
        raise SimplbooksError(f"Period {period} has a partial submission; resume it without regeneration.")
    if action_path.exists():
        booksend.validate_predecessor_submission(action_path=action_path, period=period)

    commands: list[list[str]] = []
    discovery_command = [
        python_executable, "scripts/examine_simplbooks_year.py",
        "--company-dir", str(company_dir), "--year", period[:4],
        "--output", str(discovery_path),
    ]
    commands.append(discovery_command)
    _run_step(command=discovery_command, cwd=cwd, runner=runner, label="Discovery refresh")

    builder_command = [
        python_executable, "scripts/bookbuilder.py", "--company-dir", str(company_dir),
        "--period", period,
        "--posting-policy", str(company_dir / "artifacts" / "posting_policy.json"),
        "--exchange-rates", str(company_dir / "artifacts" / "reference" / f"ecb-rates-{period[:4]}.json"),
        "--discovery-overview", str(discovery_path),
        "--bank-allocations", str(allocation_path),
    ]
    commands.append(builder_command)
    builder_summary = _run_step(command=builder_command, cwd=cwd, runner=runner, label="Action build")
    _verify_builder_output(
        summary=builder_summary,
        action_path=action_path,
        discovery_path=discovery_path,
        cwd=cwd,
    )
    booksend.validate_predecessor_submission(action_path=action_path, period=period)

    checker_command = [
        python_executable, "scripts/bookchecker.py", "--company-dir", str(company_dir),
        "--period", period,
        "--posting-policy", str(company_dir / "artifacts" / "posting_policy.json"),
        "--exchange-rates", str(company_dir / "artifacts" / "reference" / f"ecb-rates-{period[:4]}.json"),
        "--bank-allocations", str(allocation_path),
    ]
    commands.append(checker_command)
    first_check = _run_step(command=checker_command, cwd=cwd, runner=runner, label="Initial checker")
    _verify_checker_summary(first_check, label="Initial checker")
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
    _verify_checker_summary(final_check, label="Approved checker rerun")
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
