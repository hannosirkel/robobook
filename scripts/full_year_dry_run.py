#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from simplbooks_api import SimplbooksError, resolve_company_name


ORIGINAL_SUBPROCESS_RUN = subprocess.run

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


def summarize_action_artifacts(*, company_dir: Path, year: int) -> dict[str, Any]:
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

    policy_path = company_dir / "artifacts" / "posting_policy.json"
    posting_policy = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    expected_woo_contact = ((posting_policy.get("contacts") or {}).get("sales") or {}).get("woo")
    stripe_contact = ((posting_policy.get("contacts") or {}).get("processors") or {}).get("stripe")
    allowed_bank_accounts = {str(value) for value in (posting_policy.get("bank_accounts") or {}).values()}

    actions_dir = company_dir / "artifacts" / "actions"
    for path in sorted(actions_dir.glob(f"{year}-??.yaml")) if actions_dir.exists() else []:
        batch = load_action_yaml(path)
        period = str(batch.get("period") or path.stem)
        suppressed_document_count += len(batch.get("already_present") or [])
        blocking_dependency_count += sum(
            1 for item in batch.get("unresolved_dependencies") or [] if item.get("blocking")
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
            if action_type in {"create_incoming_summary", "create_payment_summary"} and allowed_bank_accounts:
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
                if ref_path.startswith(str(company_dir / "artifacts" / "normalized")) or ref_path.startswith(relative_prefix):
                    canonical_source_reference_count += 1

    normalized_dir = company_dir / "artifacts" / "normalized"
    for path in sorted(normalized_dir.glob(f"{year}-??.json")) if normalized_dir.exists() else []:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for source in payload.get("sources") or []:
            raw_source_reference_count += 1
            source_path = str(source.get("path") or "")
            if source_path.startswith(str(company_dir / "source")) or source_path.startswith(
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
    }


def reference_acceptance_issues(reference_summary: dict[str, Any]) -> list[str]:
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
) -> list[str]:
    cmd = [python_executable, f"scripts/{script_name}", "--company-dir", str(company_dir), "--period", period]
    if step_name == "bookprep" and source_dir is not None:
        cmd.extend(["--source-dir", str(source_dir)])
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

    target_periods = periods_for_year(year)
    for period in target_periods:
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
            checker_failed = (
                step_name == "bookchecker"
                and isinstance(step_summary["stdout"], dict)
                and step_summary["stdout"].get("result") != "pass"
            )
            if run.returncode != 0 or checker_failed:
                month_success = False
                overall_success = False
                break
        months.append({"period": period, "ok": month_success, "steps": step_results})
        if not month_success and not continue_on_error:
            break

    reference_summary = summarize_action_artifacts(company_dir=company_dir, year=year)
    acceptance_issues = reference_acceptance_issues(reference_summary)
    if len(months) != len(target_periods):
        acceptance_issues.append(f"Expected {len(target_periods)} processed months, found {len(months)}.")
    overall_success = overall_success and not acceptance_issues
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
        "reference_summary": reference_summary,
        "acceptance_issues": acceptance_issues,
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
