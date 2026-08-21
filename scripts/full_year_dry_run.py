#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from simplbooks_api import SimplbooksError, resolve_company_name


STEP_SPECS = (
    ("bookprep", "bookprep.py"),
    ("bookrecon", "bookrecon.py"),
    ("bookbuilder", "bookbuilder.py"),
    ("bookchecker", "bookchecker.py"),
    ("booksend", "booksend.py"),
)


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


def build_step_command(
    *,
    python_executable: str,
    company_dir: Path,
    period: str,
    step_name: str,
    script_name: str,
    source_dir: Path | None,
    force_build: bool,
) -> list[str]:
    cmd = [python_executable, f"scripts/{script_name}", "--company-dir", str(company_dir), "--period", period]
    if step_name == "bookprep" and source_dir is not None:
        cmd.extend(["--source-dir", str(source_dir)])
    if step_name == "bookbuilder" and force_build:
        cmd.append("--force")
    if step_name == "booksend":
        cmd.extend(["--mode", "dry-run"])
    return cmd


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
    months: list[dict[str, Any]] = []
    api_calls: list[dict[str, Any]] = []
    overall_success = True

    for period in periods_for_year(year):
        step_results: list[dict[str, Any]] = []
        month_success = True
        for step_name, script_name in STEP_SPECS:
            cmd = build_step_command(
                python_executable=python_executable,
                company_dir=company_dir,
                period=period,
                step_name=step_name,
                script_name=script_name,
                source_dir=source_dir,
                force_build=force_build,
            )
            run = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
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
            if run.returncode != 0:
                month_success = False
                overall_success = False
                break
        months.append({"period": period, "ok": month_success, "steps": step_results})
        if not month_success and not continue_on_error:
            break

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
