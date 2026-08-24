from __future__ import annotations  # noqa: I001

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import live_month_run  # noqa: E402


def write_live_context(company_dir: Path, *, period: str = "2024-03", allocations: list[dict] | None = None) -> None:
    year = int(period[:4])
    normalized_dir = company_dir / "artifacts" / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    normalized_paths = []
    for month in range(1, 13):
        path = normalized_dir / f"{year}-{month:02d}.json"
        month_period = f"{year}-{month:02d}"
        rows = []
        for item in allocations or []:
            if item["period"] != month_period:
                continue
            statement_id = str(item["statement_id"])
            rows.append({
                "record_id": item["record_id"], "source_system": "bank",
                "event_date": f"{month_period}-15", "currency": item["currency"],
                "gross_amount": item["amount"], "description": "Reviewed row",
                "external_ref": statement_id.removeprefix("archive:"),
                "attributes": {"customer_account": item["iban"]},
            })
        path.write_text(json.dumps({"period": month_period, "records": {"bank_transactions": rows}}), encoding="utf-8")
        normalized_paths.append(path)
    allocation_path = company_dir / "artifacts" / "bank" / f"{year}-allocations.json"
    allocation_path.parent.mkdir(parents=True, exist_ok=True)
    allocation_path.write_text(json.dumps({
        "schema_version": "1.0", "company_slug": "example", "year": year,
        "normalized_bindings": [
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in normalized_paths
        ],
        "allocations": allocations or [],
    }), encoding="utf-8")


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


def bind_discovery(action_path: Path, discovery_path: Path) -> None:
    payload = json.loads(action_path.read_text(encoding="utf-8"))
    payload["reference_artifacts"] = [{
        "kind": "discovery_overview",
        "path": str(discovery_path),
        "sha256": hashlib.sha256(discovery_path.read_bytes()).hexdigest(),
    }]
    action_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def bind_discoveries(action_path: Path, discovery_paths: list[Path]) -> None:
    payload = json.loads(action_path.read_text(encoding="utf-8"))
    payload["reference_artifacts"] = [{
        "kind": "discovery_overview", "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    } for path in discovery_paths]
    action_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


class LiveMonthRunTests(unittest.TestCase):
    def test_discovery_evidence_must_match_refreshed_cash_index(self) -> None:
        evidence = {
            "evidence_kind": "simplbooks_discovery", "company_id": "123",
            "simplbooks_transaction_id": "txn-1", "transaction_date": "2024-03-15",
            "currency": "EUR", "signed_amount": -7.0,
        }
        overview = {
            "year": 2024, "company_id": "123",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "document_index": [{
            "document_type": "payment", "simplbooks_id": "txn-1",
            "document_date": "2024-03-15", "currency": "EUR", "gross_amount": 8.0,
        }]}

        with self.assertRaisesRegex(live_month_run.SimplbooksError, "economics"):
            live_month_run._validate_refreshed_cash_evidence(
                evidence=evidence, discovery_payloads=[overview], expected_company_id="123"
            )

    def test_refreshed_discovery_evidence_failure_stops_before_builder(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            company_dir = root / "companies" / "example"
            allocation = {
                "statement_id": "archive:fee-1", "record_id": "fee-1", "iban": "EE123",
                "period": "2024-03", "disposition": "bank_fee_payment", "amount": -7.0,
                "currency": "EUR", "target": {"financial_transaction_kind": "bank-fee"},
                "review": {"status": "approved", "rationale": "Reviewed bank fee."},
            }
            write_live_context(company_dir, allocations=[allocation])
            company_dir.mkdir(parents=True, exist_ok=True)
            (company_dir / "METADATA.md").write_text(
                "Company name: Example\nCompany slug: example\nSimplbooks company ID: 123\n",
                encoding="utf-8",
            )
            normalized = company_dir / "artifacts" / "normalized" / "2024-03.json"
            snapshot = root / "discovery-snapshot.json"
            snapshot.write_text(json.dumps({
                "year": 2024, "company_id": "123",
                "retrieved_at": datetime.now(UTC).isoformat(),
                "document_index": [{
                    "document_type": "payment", "simplbooks_id": "txn-1",
                    "document_date": "2024-03-15", "currency": "EUR", "gross_amount": 7.0,
                }],
            }), encoding="utf-8")
            evidence = {
                "schema_version": "1.0", "company_slug": "example", "company_id": "123",
                "period": "2024-03", "statement_id": "archive:fee-1", "record_id": "fee-1",
                "transaction_date": "2024-03-15", "iban": "EE123", "currency": "EUR",
                "signed_amount": -7.0, "simplbooks_transaction_id": "txn-1",
                "evidence_kind": "simplbooks_discovery", "captured_at": "2026-08-22T00:00:00Z",
                "source_identity": {
                    "path": str(normalized), "sha256": hashlib.sha256(normalized.read_bytes()).hexdigest(),
                    "record_ref": "fee-1",
                },
                "evidence_source": {
                    "path": str(snapshot), "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                    "record_ref": "txn-1",
                },
            }
            evidence_path = root / "evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            allocation_path = company_dir / "artifacts" / "bank" / "2024-allocations.json"
            allocation_payload = json.loads(allocation_path.read_text(encoding="utf-8"))
            allocation_payload["allocations"][0]["target"]["statement_import_proof"] = {
                "status": "verified", "required_evidence": "live_discovery_or_audit",
                "simplbooks_transaction_id": "txn-1",
                "evidence_binding": {
                    "path": str(evidence_path),
                    "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                },
            }
            allocation_path.write_text(json.dumps(allocation_payload), encoding="utf-8")

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                output = Path(cmd[cmd.index("--output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({
                    "year": 2024, "company_id": "123",
                    "retrieved_at": datetime.now(UTC).isoformat(), "document_index": [],
                }), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=json.dumps({"year": 2024}), stderr="")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "contain exactly one"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True, run_command=fake_run,
                    approval_checkpoint=lambda _path: None,
                )

        self.assertEqual([Path(cmd[1]).name for cmd in calls], ["examine_simplbooks_year.py"])

    def test_pending_manual_proof_stops_before_discovery(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            allocation = {
                "statement_id": "archive:fee-1", "record_id": "fee-1", "iban": "EE123",
                "period": "2024-03", "disposition": "bank_fee_payment", "amount": -7.0,
                "currency": "EUR", "target": {"financial_transaction_kind": "bank-fee"},
                "review": {"status": "approved", "rationale": "Reviewed bank fee."},
            }
            write_live_context(company_dir, allocations=[allocation])

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "before live discovery/build"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True,
                    run_command=lambda cmd, **kwargs: calls.append(cmd),
                    approval_checkpoint=lambda _path: None,
                )

        self.assertEqual(calls, [])
    def test_dependency_resolution_accepts_verified_manual_and_nonblocking_information(self) -> None:
        batch = {"unresolved_dependencies": [
            {
                "kind": "manual_statement_import_financial_transaction",
                "blocking": False,
                "statement_import_proof": {
                    "status": "verified",
                    "required_evidence": "live_discovery_or_audit",
                    "simplbooks_transaction_id": "txn-1",
                    "evidence_binding": {"path": "evidence.json", "sha256": "a" * 64},
                },
            },
            {"kind": "informational_note", "blocking": False, "reason": "Reviewed context."},
        ]}

        self.assertTrue(live_month_run._dependencies_are_resolved(batch))

    def test_required_discovery_years_include_existing_and_generated_target_years(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            discovery = company_dir / "artifacts" / "discovery" / "2023-overview.json"
            discovery.parent.mkdir(parents=True)
            discovery.write_text(json.dumps({
                "year": 2023,
                "document_index": [{"simplbooks_id": "58", "document_type": "invoice"}],
            }), encoding="utf-8")
            allocations = [{
                "target": {"simplbooks_id": "58", "document_type": "invoice"},
            }, {
                "target": {"action_key": "example-2024-12-purchase-abc"},
            }]

            self.assertEqual(
                live_month_run._required_discovery_years(
                    company_dir=company_dir, period="2025-01", allocations=allocations
                ),
                [2023, 2024, 2025],
            )

    def test_multiyear_discovery_failure_stops_before_builder(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            allocation = {
                "statement_id": "archive:receipt-1", "record_id": "receipt-1", "iban": "EE123",
                "period": "2024-01", "disposition": "existing_invoice_receipt", "amount": 330.0,
                "currency": "EUR", "target": {"simplbooks_id": "58", "document_type": "invoice"},
                "review": {"status": "approved", "rationale": "Prior-year invoice receipt."},
            }
            write_live_context(company_dir, period="2024-01", allocations=[allocation])
            old_discovery = company_dir / "artifacts" / "discovery" / "2023-overview.json"
            old_discovery.parent.mkdir(parents=True)
            old_discovery.write_text(json.dumps({
                "year": 2023,
                "document_index": [{"simplbooks_id": "58", "document_type": "invoice"}],
            }), encoding="utf-8")

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                year = cmd[cmd.index("--year") + 1]
                if year == "2024":
                    return SimpleNamespace(returncode=1, stdout="", stderr="network unavailable")
                return SimpleNamespace(returncode=0, stdout=json.dumps({"year": int(year)}), stderr="")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "network unavailable"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-01", python_executable="python3",
                    cwd=ROOT, confirm_write=True, run_command=fake_run,
                    approval_checkpoint=lambda _path: None,
                )

        self.assertEqual([Path(cmd[1]).name for cmd in calls], [
            "examine_simplbooks_year.py", "examine_simplbooks_year.py",
        ])
        self.assertEqual([cmd[cmd.index("--year") + 1] for cmd in calls], ["2023", "2024"])

    def test_multiyear_builder_receives_every_refreshed_discovery(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            allocation = {
                "statement_id": "archive:receipt-1", "record_id": "receipt-1", "iban": "EE123",
                "period": "2024-01", "disposition": "existing_invoice_receipt", "amount": 330.0,
                "currency": "EUR", "target": {"simplbooks_id": "58", "document_type": "invoice"},
                "review": {"status": "approved", "rationale": "Prior-year invoice receipt."},
            }
            write_live_context(company_dir, period="2024-01", allocations=[allocation])
            discovery_paths = [
                company_dir / "artifacts" / "discovery" / f"{year}-overview.json"
                for year in (2023, 2024)
            ]
            discovery_paths[0].parent.mkdir(parents=True)
            discovery_paths[0].write_text(json.dumps({
                "year": 2023,
                "document_index": [{"simplbooks_id": "58", "document_type": "invoice"}],
            }), encoding="utf-8")
            action_path = company_dir / "artifacts" / "actions" / "2024-01.yaml"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                script = Path(cmd[1]).name
                if script == "examine_simplbooks_year.py":
                    output = Path(cmd[cmd.index("--output") + 1])
                    output.write_text(json.dumps({"year": int(cmd[cmd.index("--year") + 1])}), encoding="utf-8")
                    result = {"year": int(cmd[cmd.index("--year") + 1])}
                elif script == "bookbuilder.py":
                    write_action(action_path, period="2024-01")
                    bind_discoveries(action_path, discovery_paths)
                    result = {"approval_status": "draft", "output": str(action_path)}
                else:
                    result = {"result": "pass", "error_count": 0, "warning_count": 1}
                return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "warning"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-01", python_executable="python3",
                    cwd=ROOT, confirm_write=True, run_command=fake_run,
                    approval_checkpoint=lambda _path: None,
                )

        builder = next(cmd for cmd in calls if Path(cmd[1]).name == "bookbuilder.py")
        bound = [builder[index + 1] for index, value in enumerate(builder) if value == "--discovery-overview"]
        self.assertEqual(bound, list(map(str, discovery_paths)))
    def test_requires_explicit_confirmation_before_any_command(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:  # noqa: SIM117
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
            write_live_context(company_dir)
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            check_path = company_dir / "artifacts" / "actions" / "2024-03.check.md"
            discovery_path = company_dir / "artifacts" / "discovery" / "2024-overview.json"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    write_action(action_path, status="draft")
                    bind_discovery(action_path, discovery_path)
                    payload = {"approval_status": "draft", "output": str(action_path)}
                elif script == "bookchecker.py":
                    write_check(check_path, action_path)
                    payload = {"result": "pass", "error_count": 0, "warning_count": 0}
                elif script == "booksend.py":
                    payload = {"mode": "write", "approval_status": "submitted"}
                else:
                    discovery_path.parent.mkdir(parents=True, exist_ok=True)
                    discovery_path.write_text('{"year":2024}', encoding="utf-8")
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
            write_live_context(company_dir)
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            check_path = company_dir / "artifacts" / "actions" / "2024-03.check.md"
            discovery_path = company_dir / "artifacts" / "discovery" / "2024-overview.json"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    write_action(action_path)
                    bind_discovery(action_path, discovery_path)
                    payload = {"approval_status": "draft", "output": str(action_path)}
                elif script == "bookchecker.py":
                    write_check(check_path, action_path)
                    payload = {"result": "pass", "error_count": 0, "warning_count": 0}
                else:
                    discovery_path.parent.mkdir(parents=True, exist_ok=True)
                    discovery_path.write_text('{"year":2024}', encoding="utf-8")
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
            write_live_context(company_dir)
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            discovery_path = company_dir / "artifacts" / "discovery" / "2024-overview.json"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    write_action(action_path)
                    bind_discovery(action_path, discovery_path)
                    payload = {"approval_status": "draft", "output": str(action_path)}
                elif script == "bookchecker.py":
                    payload = {"result": "pass", "error_count": 0, "warning_count": 1}
                else:
                    discovery_path.parent.mkdir(parents=True, exist_ok=True)
                    discovery_path.write_text('{"year":2024}', encoding="utf-8")
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
            write_live_context(company_dir)
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            discovery_path = company_dir / "artifacts" / "discovery" / "2024-overview.json"
            write_action(action_path)

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                calls.append(cmd)
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    payload = json.loads(action_path.read_text(encoding="utf-8"))
                    payload["rebuilt_with_fresh_discovery"] = True
                    action_path.write_text(json.dumps(payload), encoding="utf-8")
                    bind_discovery(action_path, discovery_path)
                    result = {"approval_status": "draft", "output": str(action_path)}
                elif script == "bookchecker.py":
                    result = {"result": "pass", "error_count": 0, "warning_count": 1}
                else:
                    discovery_path.parent.mkdir(parents=True, exist_ok=True)
                    discovery_path.write_text('{"year":2024}', encoding="utf-8")
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
            write_live_context(company_dir)
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            check_path = company_dir / "artifacts" / "actions" / "2024-03.check.md"
            discovery_path = company_dir / "artifacts" / "discovery" / "2024-overview.json"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                script = Path(cmd[1]).name
                if script == "bookbuilder.py":
                    write_action(action_path, status="approved")
                    result = {"approval_status": "approved", "output": str(action_path)}
                elif script == "bookchecker.py":
                    write_check(check_path, action_path)
                    result = {"result": "pass", "error_count": 0, "warning_count": 0}
                else:
                    discovery_path.parent.mkdir(parents=True, exist_ok=True)
                    discovery_path.write_text('{"year":2024}', encoding="utf-8")
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

    def test_rejects_builder_that_reports_a_different_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            write_live_context(company_dir)
            discovery_path = company_dir / "artifacts" / "discovery" / "2024-overview.json"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                script = Path(cmd[1]).name
                if script == "examine_simplbooks_year.py":
                    discovery_path.parent.mkdir(parents=True)
                    discovery_path.write_text('{"year":2024}', encoding="utf-8")
                    result = {"year": 2024}
                elif script == "bookbuilder.py":
                    write_action(action_path)
                    bind_discovery(action_path, discovery_path)
                    result = {"approval_status": "draft", "output": str(action_path.with_name("wrong.yaml"))}
                else:
                    self.fail("checker/write must not run for a misdirected builder")
                return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "output"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True, run_command=fake_run,
                    approval_checkpoint=lambda _path: None,
                )

    def test_rejects_noop_builder_that_leaves_stale_discovery_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            write_live_context(company_dir)
            discovery_path = company_dir / "artifacts" / "discovery" / "2024-overview.json"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            discovery_path.parent.mkdir(parents=True)
            discovery_path.write_text('{"year":2024,"old":true}', encoding="utf-8")
            write_action(action_path)
            bind_discovery(action_path, discovery_path)

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                script = Path(cmd[1]).name
                if script == "examine_simplbooks_year.py":
                    discovery_path.write_text('{"year":2024,"fresh":true}', encoding="utf-8")
                    result = {"year": 2024}
                elif script == "bookbuilder.py":
                    result = {"approval_status": "draft", "output": str(action_path)}
                else:
                    self.fail("checker/write must not run when builder leaves stale YAML")
                return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "discovery"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True, run_command=fake_run,
                    approval_checkpoint=lambda _path: None,
                )


if __name__ == "__main__":
    unittest.main()


def statement_import_policy() -> dict:
    """Minimal policy declaring statement-import cash posting."""
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "bank_accounts": {"EE001234567890": {"EUR": "3", "USD": "3"}},
        "contacts": {},
        "mappings": {},
        "supplier_aliases": {},
        "cash_posting": {
            "mode": "statement_import",
            "bank_income_account_ids": ["3"],
            "processor_income_account_ids": {"paypal": "6", "stripe": "7"},
            "bank_financial_accounts": {"EE001234567890": {"EUR": "10", "USD": "11"}},
            "clearing_provider_roles": {"paypal": "paypal", "stripe": "stripe_clearing"},
            "financial_accounts": {
                "stripe_clearing": "30", "paypal": "31", "bank_fees": "32",
                "reporting_person_payable": "33", "platform_prepayment": "34",
                "fx_gain": "35", "fx_loss": "36", "customer_receivable": "37",
                "supplier_payable": "38", "bank": "10",
            },
        },
    }


class StatementImportModeGateTests(unittest.TestCase):
    """In statement-import mode the API posts no cash for these rows.

    The orchestrator predates that mode (guard added 2026-08-22, mode added
    2026-08-23) and still demands a verified per-row proof before anything is
    built, while bookbuilder, bookchecker, booksend and full_year_dry_run all
    treat a pending proof as non-blocking. That divergence is why every dry run
    passes while the live run refuses.
    """

    def _manual_allocation(self) -> dict:
        return {
            "statement_id": "archive:fee-1", "record_id": "fee-1", "iban": "EE123",
            "period": "2024-03", "disposition": "bank_fee_payment", "amount": -7.0,
            "currency": "EUR", "target": {"financial_transaction_kind": "bank-fee"},
            "review": {"status": "approved", "rationale": "Reviewed bank fee."},
        }

    def test_statement_import_mode_does_not_demand_a_verified_proof(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            write_live_context(company_dir, allocations=[self._manual_allocation()])
            (company_dir / "artifacts" / "posting_policy.json").write_text(
                json.dumps(statement_import_policy()), encoding="utf-8"
            )

            # It must get past the proof gate; whatever it fails on later is not this gate.
            with self.assertRaises(live_month_run.SimplbooksError) as caught:
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True,
                    run_command=lambda cmd, **kwargs: calls.append(cmd),
                    approval_checkpoint=lambda _path: None,
                )

        self.assertNotIn("before live discovery/build", str(caught.exception))

    def test_api_mode_still_demands_a_verified_proof(self) -> None:
        """Fail closed: no policy on disk means API mode, and API mode still blocks."""
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            write_live_context(company_dir, allocations=[self._manual_allocation()])

            with self.assertRaisesRegex(live_month_run.SimplbooksError, "before live discovery/build"):
                live_month_run.run_live_month(
                    company_dir=company_dir, period="2024-03", python_executable="python3",
                    cwd=ROOT, confirm_write=True,
                    run_command=lambda cmd, **kwargs: None,
                    approval_checkpoint=lambda _path: None,
                )

    def test_a_pending_proof_resolves_in_a_statement_import_batch(self) -> None:
        batch = {
            "cash_posting_mode": "statement_import",
            "unresolved_dependencies": [{
                "kind": "manual_statement_import_financial_transaction",
                "blocking": False,
                "statement_import_proof": {"status": "pending"},
            }],
        }

        self.assertTrue(live_month_run._dependencies_are_resolved(batch))

    def test_a_pending_proof_still_blocks_an_api_mode_batch(self) -> None:
        batch = {
            "unresolved_dependencies": [{
                "kind": "manual_statement_import_financial_transaction",
                "blocking": False,
                "statement_import_proof": {"status": "pending"},
            }],
        }

        self.assertFalse(live_month_run._dependencies_are_resolved(batch))

    def test_a_blocking_dependency_stops_a_statement_import_batch_too(self) -> None:
        batch = {
            "cash_posting_mode": "statement_import",
            "unresolved_dependencies": [{
                "kind": "manual_statement_import_financial_transaction",
                "blocking": True,
                "statement_import_proof": {"status": "pending"},
            }],
        }

        self.assertFalse(live_month_run._dependencies_are_resolved(batch))
