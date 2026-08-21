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
