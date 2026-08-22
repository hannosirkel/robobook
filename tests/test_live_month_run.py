from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import live_month_run  # noqa: E402


def write_action(path: Path, *, status: str = "draft", period: str = "2024-03") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0",
        "company_slug": "example",
        "period": period,
        "batch_id": f"example-{period}",
        "approval_status": status,
        "reference_artifacts": [],
        "actions": [],
        "unresolved_dependencies": [],
    }, sort_keys=True), encoding="utf-8")


def write_check(path: Path, action_path: Path) -> None:
    batch = json.loads(action_path.read_text(encoding="utf-8"))
    path.write_text("\n".join([
        "# Check",
        "- Result: `pass`",
        f"- Batch ID: `{batch['batch_id']}`",
        f"- Action file SHA256: `{hashlib.sha256(action_path.read_bytes()).hexdigest()}`",
        "",
    ]), encoding="utf-8")


class LiveMonthRunTests(unittest.TestCase):
    def test_requires_explicit_confirmation_before_any_command(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(live_month_run.SimplbooksError, "confirm-write"):
                live_month_run.run_live_month(
                    company_dir=Path(tmp) / "companies" / "example",
                    period="2024-03",
                    python_executable="python3",
                    cwd=ROOT,
                    confirm_write=False,
                    run_command=lambda cmd, **kwargs: calls.append(cmd),
                    approval_checkpoint=lambda _path: None,
                )
        self.assertEqual(calls, [])

    def test_refuses_successfully_submitted_month_before_discovery_or_build(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            submission_path = company_dir / "artifacts" / "submissions" / "2024-03.json"
            write_action(action_path, status="submitted")
            submission_path.parent.mkdir(parents=True)
            submission_path.write_text(json.dumps({
                "batch_id": "example-2024-03", "company_slug": "example", "period": "2024-03",
                "mode": "write", "action_file_sha256": hashlib.sha256(action_path.read_bytes()).hexdigest(),
                "summary": {"failed_actions": 0, "stopped_on_failure": False}, "request_log": [],
            }), encoding="utf-8")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "already submitted"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True,
                    run_command=lambda cmd, **kwargs: calls.append(cmd),
                    approval_checkpoint=lambda _path: None,
                )
        self.assertEqual(calls, [])

    def test_runs_exact_human_approved_sequence_without_auto_approval(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            check_path = company_dir / "artifacts" / "actions" / "2024-03.check.md"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    write_action(action_path, status="draft")
                    payload = {"approval_status": "draft"}
                elif script == "bookchecker.py":
                    write_check(check_path, action_path)
                    payload = {"result": "pass", "error_count": 0, "warning_count": 0}
                elif script == "booksend.py":
                    payload = {"mode": "write", "approval_status": "submitted"}
                else:
                    payload = {"year": 2024}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            def approve(path: Path) -> None:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["approval_status"], "draft")
                payload["approval_status"] = "approved"
                path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

            summary = live_month_run.run_live_month(
                company_dir=company_dir, period="2024-03", python_executable="python3",
                cwd=ROOT, confirm_write=True, run_command=fake_run,
                approval_checkpoint=approve,
            )

        scripts = [Path(cmd[1]).name for cmd in calls]
        self.assertEqual(scripts, [
            "examine_simplbooks_year.py", "bookbuilder.py", "bookchecker.py",
            "bookchecker.py", "booksend.py",
        ])
        self.assertEqual(calls[-1][-3:], ["--mode", "write", "--confirm-write"])
        self.assertEqual(summary["status"], "submitted")

    def test_refuses_non_approval_mutation_at_human_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            check_path = company_dir / "artifacts" / "actions" / "2024-03.check.md"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    write_action(action_path)
                    payload = {"approval_status": "draft"}
                elif script == "bookchecker.py":
                    write_check(check_path, action_path)
                    payload = {"result": "pass", "error_count": 0, "warning_count": 0}
                else:
                    payload = {"ok": True}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            def mutate(path: Path) -> None:
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["approval_status"] = "approved"
                payload["actions"].append({"idempotency_key": "surprise"})
                path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "only approval_status"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True, run_command=fake_run,
                    approval_checkpoint=mutate,
                )

    def test_cross_year_predecessor_must_be_exact_and_successful(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            actions = company_dir / "artifacts" / "actions"
            write_action(actions / "2024-12.yaml", status="submitted", period="2024-12")
            write_action(actions / "2025-01.yaml", status="approved", period="2025-01")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "2024-12"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2025-01", python_executable="python3",
                    cwd=ROOT, confirm_write=True,
                    run_command=lambda cmd, **kwargs: calls.append(cmd),
                    approval_checkpoint=lambda _path: None,
                )
        self.assertEqual(calls, [])

    def test_checker_warning_stops_before_approval_and_write(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    write_action(action_path)
                    payload = {"approval_status": "draft"}
                elif script == "bookchecker.py":
                    payload = {"result": "pass", "error_count": 0, "warning_count": 1}
                else:
                    payload = {"ok": True}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "warning"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True, run_command=fake_run,
                    approval_checkpoint=lambda _path: self.fail("approval checkpoint must not run"),
                )

        self.assertNotIn("booksend.py", [Path(cmd[1]).name for cmd in calls])

    def test_never_submitted_existing_draft_is_rebuilt_after_discovery_refresh(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            write_action(action_path)

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    payload = json.loads(action_path.read_text(encoding="utf-8"))
                    payload["rebuilt_with_fresh_discovery"] = True
                    action_path.write_text(json.dumps(payload), encoding="utf-8")
                    result = {"approval_status": "draft"}
                elif script == "bookchecker.py":
                    result = {"result": "pass", "error_count": 0, "warning_count": 1}
                else:
                    result = {"ok": True}
                return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "warning"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True, run_command=fake_run,
                    approval_checkpoint=lambda _path: None,
                )

            rebuilt = json.loads(action_path.read_text(encoding="utf-8"))

        self.assertTrue(rebuilt.get("rebuilt_with_fresh_discovery"))
        self.assertEqual(
            [Path(cmd[1]).name for cmd in calls[:2]],
            ["examine_simplbooks_year.py", "bookbuilder.py"],
        )

    def test_rejects_builder_output_that_is_already_approved(self) -> None:
        checkpoint_called = False
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            check_path = company_dir / "artifacts" / "actions" / "2024-03.check.md"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    write_action(action_path, status="approved")
                    result = {"approval_status": "approved"}
                elif script == "bookchecker.py":
                    write_check(check_path, action_path)
                    result = {"result": "pass", "error_count": 0, "warning_count": 0}
                else:
                    result = {"ok": True}
                return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

            def checkpoint(_path: Path) -> None:
                nonlocal checkpoint_called
                checkpoint_called = True

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "draft"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True, run_command=fake_run,
                    approval_checkpoint=checkpoint,
                )

        self.assertFalse(checkpoint_called)


if __name__ == "__main__":
    unittest.main()
