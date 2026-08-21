from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import full_year_dry_run  # noqa: E402


class FullYearDryRunTests(unittest.TestCase):
    def test_full_year_runner_propagates_reference_artifacts(self) -> None:
        cmd = full_year_dry_run.build_step_command(
            python_executable="python3",
            company_dir=Path("companies/example"),
            period="2024-01",
            step_name="bookbuilder",
            script_name="bookbuilder.py",
            source_dir=Path("companies/example/source"),
            force_build=False,
        )

        self.assertIn("--posting-policy", cmd)
        self.assertIn("companies/example/artifacts/posting_policy.json", cmd)
        self.assertIn("--exchange-rates", cmd)
        self.assertIn("companies/example/artifacts/reference/ecb-rates-2024.json", cmd)
        self.assertIn("--discovery-overview", cmd)
        self.assertIn("companies/example/artifacts/discovery/2024-overview.json", cmd)

    def test_periods_for_year_lists_all_months(self) -> None:
        self.assertEqual(
            full_year_dry_run.periods_for_year(2024),
            [
                "2024-01",
                "2024-02",
                "2024-03",
                "2024-04",
                "2024-05",
                "2024-06",
                "2024-07",
                "2024-08",
                "2024-09",
                "2024-10",
                "2024-11",
                "2024-12",
            ],
        )

    def test_build_step_command_adds_mode_and_force_only_when_needed(self) -> None:
        company_dir = Path("companies/example")
        source_dir = Path("companies/example/source")

        prep_cmd = full_year_dry_run.build_step_command(
            python_executable=".venv/bin/python3",
            company_dir=company_dir,
            period="2024-01",
            step_name="bookprep",
            script_name="bookprep.py",
            source_dir=source_dir,
            force_build=False,
        )
        self.assertEqual(
            prep_cmd,
            [
                ".venv/bin/python3",
                "scripts/bookprep.py",
                "--company-dir",
                "companies/example",
                "--period",
                "2024-01",
                "--source-dir",
                "companies/example/source",
            ],
        )

        send_cmd = full_year_dry_run.build_step_command(
            python_executable=".venv/bin/python3",
            company_dir=company_dir,
            period="2024-01",
            step_name="booksend",
            script_name="booksend.py",
            source_dir=source_dir,
            force_build=False,
        )
        self.assertEqual(send_cmd[-2:], ["--mode", "dry-run"])

        builder_cmd = full_year_dry_run.build_step_command(
            python_executable=".venv/bin/python3",
            company_dir=company_dir,
            period="2024-01",
            step_name="bookbuilder",
            script_name="bookbuilder.py",
            source_dir=source_dir,
            force_build=True,
        )
        self.assertEqual(builder_cmd[-1], "--force")

    def test_default_output_path_targets_submissions_folder(self) -> None:
        company_dir = Path("companies/example")
        self.assertEqual(
            full_year_dry_run.default_output_path(company_dir, 2024),
            Path("companies/example/artifacts/submissions/2024-dry-run-summary.json"),
        )

    def test_reference_summary_reports_rates_credits_and_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            actions_dir.mkdir(parents=True)
            (actions_dir / "2024-05.yaml").write_text(
                json.dumps(
                    {
                        "already_present": [{"external_ref": "INV-1"}],
                        "unresolved_dependencies": [{"blocking": True, "label": "paypal"}],
                        "actions": [
                            {
                                "action_type": "create_purchase_credit_summary",
                                "payload": {
                                    "currency": "USD",
                                    "currency_rate": 0.92,
                                    "currency_rate_provider": "ECB",
                                    "totals": {"gross_amount": 11.4},
                                },
                                "source_refs": [
                                    {"path": "companies/example/artifacts/normalized/2024-05.json"}
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = full_year_dry_run.summarize_action_artifacts(company_dir=company_dir, year=2024)

        self.assertEqual(summary["foreign_action_count"], 1)
        self.assertEqual(summary["ecb_provenance_count"], 1)
        self.assertEqual(summary["supplier_credit_totals"]["2024-05"]["USD"], 11.4)
        self.assertEqual(summary["suppressed_document_count"], 1)
        self.assertEqual(summary["blocking_dependency_count"], 1)
        self.assertEqual(summary["canonical_source_reference_count"], 1)

    def test_acceptance_requires_configured_credit_and_document_outcomes(self) -> None:
        summary = {
            "foreign_action_count": 1,
            "ecb_provenance_count": 1,
            "source_reference_count": 1,
            "canonical_source_reference_count": 1,
            "raw_source_reference_count": 1,
            "canonical_raw_source_reference_count": 1,
            "unsafe_paypal_stripe_count": 0,
            "policy_mapping_mismatch_count": 0,
            "supplier_credit_totals": {},
            "suppressed_external_refs": [],
            "blocking_dependencies": [{"kind": "contact_mapping", "label": "paypal"}],
        }
        expectations = {
            "supplier_credit_totals": {"2024-05": {"EUR": 11.4}},
            "suppressed_external_refs": ["EE24111268"],
            "allowed_blocking_dependencies": [{"kind": "contact_mapping", "label": "paypal"}],
        }

        issues = full_year_dry_run.reference_acceptance_issues(summary, expectations=expectations)

        self.assertTrue(any("11.4" in issue for issue in issues))
        self.assertTrue(any("EE24111268" in issue for issue in issues))

    def test_parse_json_output_accepts_json_objects_only(self) -> None:
        self.assertEqual(full_year_dry_run.parse_json_output('{"ok": true}'), {"ok": True})
        self.assertIsNone(full_year_dry_run.parse_json_output(""))
        self.assertIsNone(full_year_dry_run.parse_json_output("[1, 2, 3]"))

    def test_run_full_year_dry_run_collects_api_calls_from_booksend(self) -> None:
        company_dir = Path("companies/example")

        def fake_run(cmd: list[str], cwd: Path, capture_output: bool, text: bool) -> SimpleNamespace:
            del cwd, capture_output, text
            script = Path(cmd[1]).name
            period = cmd[cmd.index("--period") + 1]
            payload = {"step": script}
            if script == "bookchecker.py":
                payload["result"] = "pass"
            if script == "booksend.py":
                payload = {
                    "output": f"companies/example/artifacts/submissions/{period}.json",
                    "api_calls": [
                        {
                            "action_idempotency_key": f"{period}-invoice",
                            "method": "POST",
                            "endpoint": "invoices/create",
                            "payload": {"Invoice": {"created": f"{period}-28"}},
                        }
                    ],
                }
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )

        original_run = full_year_dry_run.subprocess.run
        original_periods = full_year_dry_run.periods_for_year
        original_resolve = full_year_dry_run.resolve_company_name
        try:
            full_year_dry_run.subprocess.run = fake_run
            full_year_dry_run.periods_for_year = lambda year: ["2024-01", "2024-02"]
            full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OU"
            summary = full_year_dry_run.run_full_year_dry_run(
                company_dir=company_dir,
                year=2024,
                source_dir=None,
                python_executable=".venv/bin/python3",
                continue_on_error=False,
                force_build=False,
                cwd=Path.cwd(),
            )
        finally:
            full_year_dry_run.subprocess.run = original_run
            full_year_dry_run.periods_for_year = original_periods
            full_year_dry_run.resolve_company_name = original_resolve

        self.assertTrue(summary["overall_success"])
        self.assertEqual(len(summary["api_calls"]), 2)
        self.assertEqual(summary["api_calls"][0]["period"], "2024-01")
        self.assertEqual(summary["api_calls"][1]["period"], "2024-02")
        self.assertEqual(summary["api_calls"][0]["endpoint"], "invoices/create")

    def test_full_year_runner_does_not_call_booksend_after_failed_check(self) -> None:
        called_scripts: list[str] = []

        def fake_run(cmd: list[str], cwd: Path, capture_output: bool, text: bool) -> SimpleNamespace:
            del cwd, capture_output, text
            script = Path(cmd[1]).name
            called_scripts.append(script)
            payload = {"result": "fail"} if script == "bookchecker.py" else {"ok": True}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        original_run = full_year_dry_run.subprocess.run
        original_periods = full_year_dry_run.periods_for_year
        original_resolve = full_year_dry_run.resolve_company_name
        try:
            full_year_dry_run.subprocess.run = fake_run
            full_year_dry_run.periods_for_year = lambda year: ["2024-01"]
            full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OU"
            summary = full_year_dry_run.run_full_year_dry_run(
                company_dir=Path("companies/example"),
                year=2024,
                source_dir=None,
                python_executable="python3",
                continue_on_error=True,
                force_build=False,
                cwd=Path.cwd(),
            )
        finally:
            full_year_dry_run.subprocess.run = original_run
            full_year_dry_run.periods_for_year = original_periods
            full_year_dry_run.resolve_company_name = original_resolve

        self.assertFalse(summary["overall_success"])
        self.assertNotIn("booksend.py", called_scripts)

    def test_main_writes_summary_to_default_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company_dir = root / "companies" / "example"
            (company_dir / "artifacts" / "submissions").mkdir(parents=True)

            def fake_run_full_year_dry_run(**_: object) -> dict[str, object]:
                return {
                    "company_dir": str(company_dir),
                    "company_name": "Example Company OU",
                    "company_slug": "example",
                    "year": 2024,
                    "python_executable": ".venv/bin/python3",
                    "source_dir": None,
                    "mode": "dry-run",
                    "force_build": False,
                    "continue_on_error": False,
                    "overall_success": True,
                    "months": [],
                }

            original_runner = full_year_dry_run.run_full_year_dry_run
            original_resolve = full_year_dry_run.resolve_company_name
            try:
                full_year_dry_run.run_full_year_dry_run = fake_run_full_year_dry_run
                full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OU"
                exit_code = full_year_dry_run.main(
                    [
                        "--company-dir",
                        str(company_dir),
                        "--year",
                        "2024",
                    ]
                )
            finally:
                full_year_dry_run.run_full_year_dry_run = original_runner
                full_year_dry_run.resolve_company_name = original_resolve

            self.assertEqual(exit_code, 0)
            summary_path = company_dir / "artifacts" / "submissions" / "2024-dry-run-summary.json"
            self.assertTrue(summary_path.exists())


if __name__ == "__main__":
    unittest.main()
