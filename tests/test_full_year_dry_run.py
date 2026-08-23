from __future__ import annotations  # noqa: I001

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import full_year_dry_run  # noqa: E402, I001
import bookprep  # noqa: E402
import reference_artifacts  # noqa: E402


def bound_allocation_for_tax_source(path: Path, *, root_dir: Path, sha256: str) -> dict:
    source_id = bookprep.source_id_for_path(path, root_dir=root_dir)
    source_path = bookprep.display_path(path, root_dir)
    return {
        "schema_version": "1.0", "company_slug": "example", "year": 2025,
        "source_files": [{"source_id": source_id, "sha256": sha256}],
        "policy": {"oss_registered": False, "dispatch_origin": "EE", "merchant_absorbs_vat": True},
        "vat_periods": [{
            "start": "2025-01-01", "end": None, "rate": 22,
            "goods_vat_type_id": "25", "shipping_vat_type_id": "24",
        }],
        "source_rows": [{
            "source_row_id": f"{source_id}:woo-tax:2", "tax_code": "DE-DE-VAT-1",
            "configured_rate": 20, "order_tax": 10.0, "shipping_tax": 10.0,
            "total_tax": 20.0, "orders": 1,
        }],
        "allocations": [{
            "source_row_id": f"{source_id}:woo-tax:2", "order_id": "EXAMPLE-EU-1",
            "period": "2025-05", "event_date": "2025-05-18", "country_code": "DE",
            "processor_ref": "pi_example", "configured_rate": 20, "corrected_rate": 22,
            "original_order_tax": 10.0, "original_shipping_tax": 10.0,
            "fixed_product_gross": 60.0, "fixed_shipping_gross": 60.0,
            "corrected_product_vat": 10.82, "corrected_shipping_vat": 10.82,
            "source_refs": [{
                "source_id": source_id, "path": source_path, "row_ref": "csv:2",
                "page_ref": None, "notes": None,
            }],
        }],
        "monthly_totals": {"2025-05": {"gross": 120.0, "original_vat": 20.0, "corrected_vat": 21.64}},
        "validation": {"status": "pass", "errors": []},
    }


def write_bound_recon(company_dir: Path, period: str, *, cwd: Path) -> None:
    normalized_path = company_dir / "artifacts" / "normalized" / f"{period}.json"
    allocation_path = company_dir / "artifacts" / "bank" / f"{period[:4]}-allocations.json"
    recon_path = company_dir / "artifacts" / "recon" / f"{period}.json"
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    allocation_path.parent.mkdir(parents=True, exist_ok=True)
    recon_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_text(json.dumps({"company_slug": "example", "period": period}), encoding="utf-8")
    allocation_path.write_text(json.dumps({"company_slug": "example", "year": int(period[:4])}), encoding="utf-8")
    recon_path.write_text(json.dumps({
        "company_slug": "example",
        "period": period,
        "reference_artifacts": [
            reference_artifacts.bind_file(normalized_path, kind="normalized_period", cwd=cwd),
            reference_artifacts.bind_file(allocation_path, kind="bank_allocations", cwd=cwd),
        ],
        "bank_coverage": {
            "physical_bank_row_count": 0,
            "allocated_row_count": 0,
            "unallocated_row_count": 0,
            "clearing_movement_count": 0,
            "resolved_clearing_count": 0,
            "unresolved_clearing_count": 0,
            "clearing_movement_record_ids": [],
            "resolved_clearing_record_ids": [],
            "unresolved_clearing_record_ids": [],
        },
        "checks": [],
    }), encoding="utf-8")


def write_action_bound_to_recon(
    company_dir: Path,
    period: str,
    *,
    cwd: Path,
    approval_status: str = "draft",
    bound_recon_path: Path | None = None,
) -> Path:
    action_path = company_dir / "artifacts" / "actions" / f"{period}.yaml"
    recon_path = bound_recon_path or company_dir / "artifacts" / "recon" / f"{period}.json"
    action_path.parent.mkdir(parents=True, exist_ok=True)
    action_path.write_text(json.dumps({
        "batch_id": f"example-{period}",
        "company_slug": "example",
        "period": period,
        "approval_status": approval_status,
        "reference_artifacts": [
            reference_artifacts.bind_file(recon_path, kind="reconciliation", cwd=cwd),
        ],
        "actions": [],
    }), encoding="utf-8")
    return action_path


class FullYearDryRunTests(unittest.TestCase):
    def test_reference_summary_blocks_required_manual_inventory_action(self) -> None:
        action = {
            "action_type": "manual_inventory_writeoff",
            "effective_date": "2024-06-30",
            "article_id": "10",
            "warehouse_id": "20",
            "quantity": 5,
            "expense_account_id": "30",
            "expected_remnant_after": 0,
            "reason": "Obsolete inventory",
            "approval": "reviewed",
            "status": "required",
            "source_refs": [{"source_id": "inventory-decision", "path": "artifacts/inventory-decision.json", "row_ref": None, "page_ref": None, "notes": None}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            actions_dir.mkdir(parents=True)
            (actions_dir / "2024-inventory-manual.json").write_text(json.dumps(action), encoding="utf-8")

            summary = full_year_dry_run.summarize_action_artifacts(company_dir=company_dir, year=2024)
            issues = full_year_dry_run.reference_acceptance_issues(summary)

        self.assertEqual(summary["manual_inventory_status"], "required")
        self.assertTrue(any("manual inventory" in issue.lower() for issue in issues))

    def test_reference_summary_accepts_completed_manual_action_with_matching_remnant_evidence(self) -> None:
        action = {
            "action_type": "manual_inventory_writeoff",
            "effective_date": "2024-06-30",
            "article_id": "10",
            "warehouse_id": "20",
            "quantity": 5,
            "expense_account_id": "30",
            "expected_remnant_after": 0,
            "reason": "Obsolete inventory",
            "approval": "reviewed",
            "status": "completed",
            "source_refs": [{"source_id": "inventory-decision", "path": "artifacts/inventory-decision.json", "row_ref": None, "page_ref": None, "notes": None}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            discovery_dir = company_dir / "artifacts" / "discovery"
            actions_dir.mkdir(parents=True)
            discovery_dir.mkdir(parents=True)
            (actions_dir / "2024-inventory-manual.json").write_text(json.dumps(action), encoding="utf-8")
            (discovery_dir / "2024-inventory-remnant-verification.json").write_text(
                json.dumps({
                    "action_type": "manual_inventory_writeoff",
                    "effective_date": "2024-06-30",
                    "article_id": "10",
                    "warehouse_id": "20",
                    "expected_remnant_after": 0,
                    "verified_at": "2026-08-21T00:00:00Z",
                    "remnant_response": {"data": {"10": {"20": 0}}},
                }),
                encoding="utf-8",
            )

            summary = full_year_dry_run.summarize_action_artifacts(company_dir=company_dir, year=2024)
            issues = full_year_dry_run.reference_acceptance_issues(summary)

        self.assertEqual(summary["manual_inventory_status"], "completed")
        self.assertTrue(summary["manual_inventory_remnant_verified"])
        self.assertFalse(any("manual inventory" in issue.lower() for issue in issues))

    def test_reference_summary_rejects_unbound_remnant_evidence(self) -> None:
        action = {
            "action_type": "manual_inventory_writeoff",
            "effective_date": "2024-06-30",
            "article_id": "10",
            "warehouse_id": "20",
            "quantity": 5,
            "expense_account_id": "30",
            "expected_remnant_after": 0,
            "reason": "Obsolete inventory",
            "approval": "reviewed",
            "status": "completed",
            "source_refs": [{"source_id": "inventory-decision", "path": "artifacts/inventory-decision.json", "row_ref": None, "page_ref": None, "notes": None}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            discovery_dir = company_dir / "artifacts" / "discovery"
            actions_dir.mkdir(parents=True)
            discovery_dir.mkdir(parents=True)
            (actions_dir / "2024-inventory-manual.json").write_text(json.dumps(action), encoding="utf-8")
            (discovery_dir / "2024-inventory-remnant-verification.json").write_text(
                json.dumps({"verified_at": "2026-08-21T00:00:00Z", "remnant_response": {"data": {"10": {"20": 0}}}}),
                encoding="utf-8",
            )

            summary = full_year_dry_run.summarize_action_artifacts(company_dir=company_dir, year=2024)

        self.assertFalse(summary["manual_inventory_remnant_verified"])

    def test_reference_summary_keeps_malformed_remnant_evidence_unverified(self) -> None:
        action = {
            "action_type": "manual_inventory_writeoff",
            "effective_date": "2024-06-30",
            "article_id": "10",
            "warehouse_id": "20",
            "quantity": 5,
            "expense_account_id": "30",
            "expected_remnant_after": 0,
            "reason": "Obsolete inventory",
            "approval": "reviewed",
            "status": "completed",
            "source_refs": [{"source_id": "inventory-decision", "path": "artifacts/inventory-decision.json", "row_ref": None, "page_ref": None, "notes": None}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            discovery_dir = company_dir / "artifacts" / "discovery"
            actions_dir.mkdir(parents=True)
            discovery_dir.mkdir(parents=True)
            (actions_dir / "2024-inventory-manual.json").write_text(json.dumps(action), encoding="utf-8")
            (discovery_dir / "2024-inventory-remnant-verification.json").write_text("[]", encoding="utf-8")

            summary = full_year_dry_run.summarize_action_artifacts(company_dir=company_dir, year=2024)

        self.assertEqual(summary["manual_inventory_status"], "completed")
        self.assertTrue(summary["manual_inventory_error"])

    def test_full_year_runner_processes_months_before_required_manual_inventory_blocks_acceptance(self) -> None:
        action = {
            "action_type": "manual_inventory_writeoff",
            "effective_date": "2024-06-30",
            "article_id": "10",
            "warehouse_id": "20",
            "quantity": 5,
            "expense_account_id": "30",
            "expected_remnant_after": 0,
            "reason": "Obsolete inventory",
            "approval": "reviewed",
            "status": "required",
            "source_refs": [{"source_id": "inventory-decision", "path": "artifacts/inventory-decision.json", "row_ref": None, "page_ref": None, "notes": None}],
        }
        called_scripts: list[str] = []

        def fake_run(cmd: list[str], cwd: Path, capture_output: bool, text: bool) -> SimpleNamespace:
            del capture_output, text
            script = Path(cmd[1]).name
            called_scripts.append(script)
            if script == "bookrecon.py":
                write_bound_recon(company_dir, cmd[cmd.index("--period") + 1], cwd=cwd)
            if script == "bookbuilder.py":
                write_action_bound_to_recon(company_dir, cmd[cmd.index("--period") + 1], cwd=cwd)
            payload = {"result": "pass"} if script == "bookchecker.py" else {"ok": True}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            actions_dir.mkdir(parents=True)
            (actions_dir / "2024-inventory-manual.json").write_text(json.dumps(action), encoding="utf-8")
            original_run = full_year_dry_run.subprocess.run
            original_periods = full_year_dry_run.periods_for_year
            original_resolve = full_year_dry_run.resolve_company_name
            try:
                full_year_dry_run.subprocess.run = fake_run
                full_year_dry_run.periods_for_year = lambda _year: ["2024-01", "2024-02"]
                full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OÜ"
                summary = full_year_dry_run.run_full_year_dry_run(
                    company_dir=company_dir,
                    year=2024,
                    source_dir=None,
                    python_executable="python3",
                    continue_on_error=False,
                    force_build=False,
                    cwd=Path.cwd(),
                )
            finally:
                full_year_dry_run.subprocess.run = original_run
                full_year_dry_run.periods_for_year = original_periods
                full_year_dry_run.resolve_company_name = original_resolve

        self.assertEqual([month["period"] for month in summary["months"]], ["2024-01", "2024-02"])
        self.assertEqual(called_scripts.count("booksend.py"), 2)
        self.assertFalse(summary["overall_success"])
        self.assertTrue(any("manual inventory" in issue.lower() for issue in summary["acceptance_issues"]))

    def test_full_year_runner_blocks_before_months_when_tax_evidence_has_no_valid_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            source_dir = company_dir / "source" / "2025-pack"
            source_dir.mkdir(parents=True)
            (source_dir / "woocommerce-taxes.csv").write_text(
                "Tax code,Rate,Total tax,Order tax,Shipping tax,Orders\n"
                "DE-DE-VAT-1,19,19.00,15.00,4.00,1\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "Woo tax allocation"):
                full_year_dry_run.validate_woo_tax_preflight(
                    company_dir=company_dir, year=2025, source_dir=source_dir
                )

    def test_full_year_preflight_rejects_stale_allocation_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company_dir = root / "companies" / "example"
            source_dir = company_dir / "source" / "2025-pack"
            allocation_dir = company_dir / "artifacts" / "vat"
            source_dir.mkdir(parents=True)
            allocation_dir.mkdir(parents=True)
            tax_path = source_dir / "woocommerce-taxes.csv"
            tax_path.write_text(
                "Tax code,Rate,Total tax,Order tax,Shipping tax,Orders\n"
                "DE-DE-VAT-1,20,20.00,10.00,10.00,1\n",
                encoding="utf-8",
            )
            actual_hash = hashlib.sha256(tax_path.read_bytes()).hexdigest()
            payload = bound_allocation_for_tax_source(
                tax_path, root_dir=Path.cwd(), sha256="b" * 64
            )
            self.assertNotEqual(actual_hash, payload["source_files"][0]["sha256"])
            (allocation_dir / "2025-woo-tax-allocation.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "hash does not match"):
                full_year_dry_run.validate_woo_tax_preflight(
                    company_dir=company_dir, year=2025, source_dir=source_dir
                )

    def test_full_year_runner_does_not_block_year_without_woo_tax_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            source_dir = company_dir / "source" / "2025-pack"
            source_dir.mkdir(parents=True)
            (source_dir / "stripe.csv").write_text("id,amount\nch_1,10.00\n", encoding="utf-8")

            self.assertIsNone(
                full_year_dry_run.validate_woo_tax_preflight(
                    company_dir=company_dir, year=2025, source_dir=source_dir
                )
            )

    def test_full_year_runner_passes_validated_allocation_to_bookprep(self) -> None:
        allocation_path = Path("companies/example/artifacts/vat/2025-woo-tax-allocation.json")
        cmd = full_year_dry_run.build_step_command(
            python_executable="python3",
            company_dir=Path("companies/example"),
            period="2025-01",
            step_name="bookprep",
            script_name="bookprep.py",
            source_dir=Path("companies/example/source/2025-pack"),
            force_build=False,
            woo_tax_allocation=allocation_path,
        )

        self.assertEqual(cmd[-2:], ["--woo-tax-allocation", str(allocation_path)])

    def test_full_year_runner_propagates_reference_artifacts(self) -> None:
        allocation_path = Path("companies/example/artifacts/bank/2024-allocations.json")
        cmd = full_year_dry_run.build_step_command(
            python_executable="python3",
            company_dir=Path("companies/example"),
            period="2024-01",
            step_name="bookbuilder",
            script_name="bookbuilder.py",
            source_dir=Path("companies/example/source"),
            force_build=False,
            bank_allocations=allocation_path,
        )

        self.assertIn("--posting-policy", cmd)
        self.assertIn("companies/example/artifacts/posting_policy.json", cmd)
        self.assertIn("--exchange-rates", cmd)
        self.assertIn("companies/example/artifacts/reference/ecb-rates-2024.json", cmd)
        self.assertIn("--discovery-overview", cmd)
        self.assertIn("companies/example/artifacts/discovery/2024-overview.json", cmd)
        self.assertIn("--bank-allocations", cmd)
        self.assertIn(str(allocation_path), cmd)

    def test_full_year_passes_bank_allocations_to_recon_builder_and_checker(self) -> None:
        allocation_path = Path("companies/example/artifacts/bank/2024-allocations.json")
        relevant = [
            full_year_dry_run.build_step_command(
                python_executable="python3",
                company_dir=Path("companies/example"),
                period="2024-03",
                step_name=step_name,
                script_name=f"{step_name}.py",
                source_dir=None,
                force_build=False,
                bank_allocations=allocation_path,
            )
            for step_name in ("bookrecon", "bookbuilder", "bookchecker")
        ]

        self.assertTrue(all(call[call.index("--bank-allocations") + 1] == str(allocation_path) for call in relevant))

    def test_full_year_summary_reports_bank_and_clearing_coverage_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company_dir = Path(tmp) / "companies" / "example"
            recon_dir = company_dir / "artifacts" / "recon"
            normalized_path = company_dir / "artifacts" / "normalized" / "2024-01.json"
            allocation_path = company_dir / "artifacts" / "bank" / "2024-allocations.json"
            recon_dir.mkdir(parents=True)
            normalized_path.parent.mkdir(parents=True)
            allocation_path.parent.mkdir(parents=True)
            normalized_path.write_text('{"period":"2024-01"}', encoding="utf-8")
            allocation_path.write_text('{"year":2024}', encoding="utf-8")
            (recon_dir / "2024-01.json").write_text(json.dumps({
                "company_slug": "example",
                "period": "2024-01",
                "reference_artifacts": [
                    reference_artifacts.bind_file(normalized_path, kind="normalized_period", cwd=root),
                    reference_artifacts.bind_file(allocation_path, kind="bank_allocations", cwd=root),
                ],
                "bank_coverage": {
                    "physical_bank_row_count": 3,
                    "allocated_row_count": 2,
                    "unallocated_row_count": 1,
                    "clearing_movement_count": 2,
                    "resolved_clearing_count": 1,
                    "unresolved_clearing_count": 1,
                    "clearing_movement_record_ids": ["wallet-1", "wallet-2"],
                    "resolved_clearing_record_ids": ["wallet-1"],
                    "unresolved_clearing_record_ids": ["wallet-2"],
                },
                "checks": [{
                    "check_id": "clearing-continuity:printful:wallet:eur",
                    "notes": ["Wording and punctuation are deliberately unrelated!"],
                }],
            }), encoding="utf-8")
            write_action_bound_to_recon(company_dir, "2024-01", cwd=root)

            summary = full_year_dry_run.summarize_bank_reconciliation_artifacts(
                company_dir=company_dir, year=2024, expected_periods=["2024-01"], cwd=root
            )

        self.assertEqual(summary, {
            "physical_bank_row_count": 3,
            "allocated_row_count": 2,
            "uncovered_row_count": 1,
            "clearing_movement_count": 2,
            "unresolved_clearing_count": 1,
        })

    def test_full_year_summary_requires_every_exact_period_and_matching_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            recon_dir = company_dir / "artifacts" / "recon"
            recon_dir.mkdir(parents=True)
            (recon_dir / "2024-01.json").write_text(json.dumps({
                "company_slug": "example", "period": "2024-02",
                "reference_artifacts": [], "bank_coverage": {},
            }), encoding="utf-8")
            write_action_bound_to_recon(company_dir, "2024-01", cwd=Path(tmp))

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "period mismatch"):
                full_year_dry_run.summarize_bank_reconciliation_artifacts(
                    company_dir=company_dir, year=2024,
                    expected_periods=["2024-01"], cwd=Path(tmp),
                )

            (recon_dir / "2024-01.json").unlink()
            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "missing"):
                full_year_dry_run.summarize_bank_reconciliation_artifacts(
                    company_dir=company_dir, year=2024,
                    expected_periods=["2024-01", "2024-02"], cwd=Path(tmp),
                )

    def test_full_year_summary_rejects_inconsistent_structured_clearing_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company_dir = root / "companies" / "example"
            write_bound_recon(company_dir, "2024-01", cwd=root)
            write_action_bound_to_recon(company_dir, "2024-01", cwd=root)
            recon_path = company_dir / "artifacts" / "recon" / "2024-01.json"
            payload = json.loads(recon_path.read_text(encoding="utf-8"))
            payload["bank_coverage"].update({
                "clearing_movement_count": 1,
                "resolved_clearing_count": 0,
                "unresolved_clearing_count": 0,
                "clearing_movement_record_ids": ["wallet-1"],
                "resolved_clearing_record_ids": [],
                "unresolved_clearing_record_ids": [],
            })
            recon_path.write_text(json.dumps(payload), encoding="utf-8")
            write_action_bound_to_recon(company_dir, "2024-01", cwd=root)

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "partition"):
                full_year_dry_run.summarize_bank_reconciliation_artifacts(
                    company_dir=company_dir, year=2024,
                    expected_periods=["2024-01"], cwd=root,
                )

    def test_full_year_summary_rejects_reconciliation_edited_after_action_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company_dir = root / "companies" / "example"
            write_bound_recon(company_dir, "2024-01", cwd=root)
            write_action_bound_to_recon(company_dir, "2024-01", cwd=root)
            recon_path = company_dir / "artifacts" / "recon" / "2024-01.json"
            payload = json.loads(recon_path.read_text(encoding="utf-8"))
            payload["bank_coverage"]["physical_bank_row_count"] = 99
            recon_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "reconciliation.*changed|SHA"):
                full_year_dry_run.summarize_bank_reconciliation_artifacts(
                    company_dir=company_dir, year=2024,
                    expected_periods=["2024-01"], cwd=root,
                )

    def test_full_year_summary_rejects_wrong_action_reconciliation_path_or_sha(self) -> None:
        for defect in ("path", "sha"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                company_dir = root / "companies" / "example"
                write_bound_recon(company_dir, "2024-01", cwd=root)
                if defect == "path":
                    wrong_path = company_dir / "artifacts" / "recon" / "wrong.json"
                    wrong_path.write_text(
                        (company_dir / "artifacts" / "recon" / "2024-01.json").read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    action_path = write_action_bound_to_recon(
                        company_dir, "2024-01", cwd=root, bound_recon_path=wrong_path
                    )
                else:
                    action_path = write_action_bound_to_recon(company_dir, "2024-01", cwd=root)
                    action = json.loads(action_path.read_text(encoding="utf-8"))
                    action["reference_artifacts"][0]["sha256"] = "0" * 64
                    action_path.write_text(json.dumps(action), encoding="utf-8")

                with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "reconciliation"):
                    full_year_dry_run.summarize_bank_reconciliation_artifacts(
                        company_dir=company_dir, year=2024,
                        expected_periods=["2024-01"], cwd=root,
                    )

    def test_full_year_summary_rejects_changed_bound_normalized_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company_dir = root / "companies" / "example"
            recon_dir = company_dir / "artifacts" / "recon"
            normalized_path = company_dir / "artifacts" / "normalized" / "2024-01.json"
            allocation_path = company_dir / "artifacts" / "bank" / "2024-allocations.json"
            recon_dir.mkdir(parents=True)
            normalized_path.parent.mkdir(parents=True)
            allocation_path.parent.mkdir(parents=True)
            normalized_path.write_text('{"period":"2024-01"}', encoding="utf-8")
            allocation_path.write_text('{"year":2024}', encoding="utf-8")
            bindings = [
                reference_artifacts.bind_file(normalized_path, kind="normalized_period", cwd=root),
                reference_artifacts.bind_file(allocation_path, kind="bank_allocations", cwd=root),
            ]
            (recon_dir / "2024-01.json").write_text(json.dumps({
                "company_slug": "example", "period": "2024-01",
                "reference_artifacts": bindings,
                "bank_coverage": {
                    "physical_bank_row_count": 1, "allocated_row_count": 1,
                    "unallocated_row_count": 0, "clearing_movement_count": 0,
                    "resolved_clearing_count": 0, "unresolved_clearing_count": 0,
                    "clearing_movement_record_ids": [], "resolved_clearing_record_ids": [],
                    "unresolved_clearing_record_ids": [],
                },
            }), encoding="utf-8")
            write_action_bound_to_recon(company_dir, "2024-01", cwd=root)
            normalized_path.write_text('{"period":"2024-01","changed":true}', encoding="utf-8")

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "changed"):
                full_year_dry_run.summarize_bank_reconciliation_artifacts(
                    company_dir=company_dir, year=2024,
                    expected_periods=["2024-01"], cwd=root,
                )

    def test_full_year_dry_run_skips_unchanged_submitted_month_even_with_force_build(self) -> None:
        called_scripts: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_dir = company_dir / "artifacts" / "actions"
            submission_dir = company_dir / "artifacts" / "submissions"
            action_dir.mkdir(parents=True)
            submission_dir.mkdir(parents=True)
            write_bound_recon(company_dir, "2024-03", cwd=ROOT)
            action_path = write_action_bound_to_recon(
                company_dir, "2024-03", cwd=ROOT, approval_status="submitted"
            )
            (submission_dir / "2024-03.json").write_text(json.dumps({
                "batch_id": "example-2024-03", "company_slug": "example", "period": "2024-03",
                "mode": "write", "action_file_sha256": hashlib.sha256(action_path.read_bytes()).hexdigest(),
                "summary": {"failed_actions": 0, "stopped_on_failure": False},
                "request_log": [],
            }), encoding="utf-8")

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                called_scripts.append(Path(cmd[1]).name)
                return SimpleNamespace(returncode=0, stdout='{"result":"pass"}', stderr="")

            original_run = full_year_dry_run.subprocess.run
            original_periods = full_year_dry_run.periods_for_year
            original_resolve = full_year_dry_run.resolve_company_name
            try:
                full_year_dry_run.subprocess.run = fake_run
                full_year_dry_run.periods_for_year = lambda _year: ["2024-03"]
                full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OÜ"
                result = full_year_dry_run.run_full_year_dry_run(
                    company_dir=company_dir, year=2024, source_dir=None,
                    python_executable="python3", continue_on_error=False,
                    force_build=True, cwd=ROOT,
                )
            finally:
                full_year_dry_run.subprocess.run = original_run
                full_year_dry_run.periods_for_year = original_periods
                full_year_dry_run.resolve_company_name = original_resolve

        self.assertEqual(result["months"][0]["status"], "skipped_submitted")
        self.assertEqual(called_scripts, [])

    def test_full_year_refuses_changed_successfully_submitted_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_dir = company_dir / "artifacts" / "actions"
            submission_dir = company_dir / "artifacts" / "submissions"
            action_dir.mkdir(parents=True)
            submission_dir.mkdir(parents=True)
            (action_dir / "2024-03.yaml").write_text('{"approval_status":"submitted"}', encoding="utf-8")
            (submission_dir / "2024-03.json").write_text(json.dumps({
                "period": "2024-03", "mode": "write", "action_file_sha256": "0" * 64,
                "summary": {"failed_actions": 0, "stopped_on_failure": False},
            }), encoding="utf-8")

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "immutable|SHA"):
                full_year_dry_run.submitted_month_state(company_dir=company_dir, period="2024-03")

    def test_full_year_refuses_success_log_for_another_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_dir = company_dir / "artifacts" / "actions"
            submission_dir = company_dir / "artifacts" / "submissions"
            action_dir.mkdir(parents=True)
            submission_dir.mkdir(parents=True)
            action_path = action_dir / "2024-03.yaml"
            action_path.write_text(json.dumps({
                "batch_id": "example-2024-03", "company_slug": "example",
                "period": "2024-03", "approval_status": "submitted",
            }), encoding="utf-8")
            (submission_dir / "2024-03.json").write_text(json.dumps({
                "batch_id": "example-2024-03", "company_slug": "other", "period": "2024-03",
                "mode": "write", "action_file_sha256": hashlib.sha256(action_path.read_bytes()).hexdigest(),
                "summary": {"failed_actions": 0, "stopped_on_failure": False},
            }), encoding="utf-8")

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "identities"):
                full_year_dry_run.submitted_month_state(company_dir=company_dir, period="2024-03")

    def test_submitted_yaml_rejects_dry_run_or_partial_write_log(self) -> None:
        for mode, summary in (
            ("dry-run", {"failed_actions": 0, "stopped_on_failure": False}),
            ("write", {"failed_actions": 1, "stopped_on_failure": True}),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                company_dir = Path(tmp) / "companies" / "example"
                action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
                submission_path = company_dir / "artifacts" / "submissions" / "2024-03.json"
                action_path.parent.mkdir(parents=True)
                submission_path.parent.mkdir(parents=True)
                action_path.write_text(json.dumps({
                    "batch_id": "example-2024-03", "company_slug": "example",
                    "period": "2024-03", "approval_status": "submitted", "actions": [],
                }), encoding="utf-8")
                submission_path.write_text(json.dumps({
                    "batch_id": "example-2024-03", "company_slug": "example", "period": "2024-03",
                    "mode": mode, "action_file_sha256": hashlib.sha256(action_path.read_bytes()).hexdigest(),
                    "summary": summary, "request_log": [],
                }), encoding="utf-8")

                with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "submitted|successful write"):
                    full_year_dry_run.submitted_month_state(company_dir=company_dir, period="2024-03")

    def test_submitted_result_requires_successful_write_evidence_for_every_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            submission_path = company_dir / "artifacts" / "submissions" / "2024-03.json"
            action_path.parent.mkdir(parents=True)
            submission_path.parent.mkdir(parents=True)
            action_path.write_text(json.dumps({
                "batch_id": "example-2024-03", "company_slug": "example", "period": "2024-03",
                "approval_status": "submitted",
                "actions": [{"idempotency_key": "a-1", "response_status": 201}],
            }), encoding="utf-8")
            submission_path.write_text(json.dumps({
                "batch_id": "example-2024-03", "company_slug": "example", "period": "2024-03",
                "mode": "write", "action_file_sha256": hashlib.sha256(action_path.read_bytes()).hexdigest(),
                "summary": {"failed_actions": 0, "stopped_on_failure": False},
                "request_log": [],
            }), encoding="utf-8")

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "action.*evidence"):
                full_year_dry_run.submitted_month_state(company_dir=company_dir, period="2024-03")

    def test_submitted_result_rejects_write_evidence_for_wrong_action_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            action_path = company_dir / "artifacts" / "actions" / "2024-03.yaml"
            submission_path = company_dir / "artifacts" / "submissions" / "2024-03.json"
            action_path.parent.mkdir(parents=True)
            submission_path.parent.mkdir(parents=True)
            action_path.write_text(json.dumps({
                "batch_id": "example-2024-03", "company_slug": "example", "period": "2024-03",
                "approval_status": "submitted",
                "actions": [{
                    "idempotency_key": "a-1", "method": "POST", "endpoint": "invoices/create",
                    "response_status": 201, "inserted_id": "501",
                }],
            }), encoding="utf-8")
            submission_path.write_text(json.dumps({
                "batch_id": "example-2024-03", "company_slug": "example", "period": "2024-03",
                "mode": "write", "action_file_sha256": hashlib.sha256(action_path.read_bytes()).hexdigest(),
                "summary": {"failed_actions": 0, "stopped_on_failure": False},
                "request_log": [{
                    "mode": "write", "action_idempotency_key": "a-1", "method": "POST",
                    "endpoint": "payments/create", "http_status": 201, "inserted_id": "501", "success": True,
                }],
            }), encoding="utf-8")

            with self.assertRaisesRegex(full_year_dry_run.SimplbooksError, "action.*evidence"):
                full_year_dry_run.submitted_month_state(company_dir=company_dir, period="2024-03")

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
        def fake_run(cmd: list[str], cwd: Path, capture_output: bool, text: bool) -> SimpleNamespace:
            del capture_output, text
            script = Path(cmd[1]).name
            period = cmd[cmd.index("--period") + 1]
            if script == "bookrecon.py":
                write_bound_recon(company_dir, period, cwd=cwd)
            if script == "bookbuilder.py":
                write_action_bound_to_recon(company_dir, period, cwd=cwd)
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

        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
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
            del capture_output, text
            script = Path(cmd[1]).name
            called_scripts.append(script)
            if script == "bookrecon.py":
                write_bound_recon(company_dir, cmd[cmd.index("--period") + 1], cwd=cwd)
            payload = {"result": "fail"} if script == "bookchecker.py" else {"ok": True}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            original_run = full_year_dry_run.subprocess.run
            original_periods = full_year_dry_run.periods_for_year
            original_resolve = full_year_dry_run.resolve_company_name
            try:
                full_year_dry_run.subprocess.run = fake_run
                full_year_dry_run.periods_for_year = lambda year: ["2024-01"]
                full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OU"
                summary = full_year_dry_run.run_full_year_dry_run(
                    company_dir=company_dir,
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

    def test_full_year_runner_returns_summary_when_january_fails_before_two_month_run_finishes(self) -> None:
        def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
            period = cmd[cmd.index("--period") + 1]
            return SimpleNamespace(returncode=1, stdout="", stderr=f"failed {period}")

        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            original_run = full_year_dry_run.subprocess.run
            original_periods = full_year_dry_run.periods_for_year
            original_resolve = full_year_dry_run.resolve_company_name
            try:
                full_year_dry_run.subprocess.run = fake_run
                full_year_dry_run.periods_for_year = lambda _year: ["2024-01", "2024-02"]
                full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OU"
                summary = full_year_dry_run.run_full_year_dry_run(
                    company_dir=company_dir, year=2024, source_dir=None,
                    python_executable="python3", continue_on_error=False,
                    force_build=False, cwd=Path(tmp),
                )
            finally:
                full_year_dry_run.subprocess.run = original_run
                full_year_dry_run.periods_for_year = original_periods
                full_year_dry_run.resolve_company_name = original_resolve

        self.assertFalse(summary["overall_success"])
        self.assertEqual(summary["aggregated_periods"], [])
        self.assertEqual(summary["unprocessed_periods"], ["2024-01", "2024-02"])
        self.assertEqual(summary["bank_reconciliation_summary"]["physical_bank_row_count"], 0)
        self.assertTrue(any("2024-01" in issue and "2024-02" in issue for issue in summary["acceptance_issues"]))

    def test_full_year_runner_does_not_fall_back_to_stale_artifacts_for_failed_period(self) -> None:
        def fake_run(_cmd: list[str], **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout="", stderr="failed before generation")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company_dir = root / "companies" / "example"
            write_bound_recon(company_dir, "2024-01", cwd=root)
            recon_path = company_dir / "artifacts" / "recon" / "2024-01.json"
            stale = json.loads(recon_path.read_text(encoding="utf-8"))
            stale["bank_coverage"].update({
                "physical_bank_row_count": 77,
                "allocated_row_count": 77,
            })
            recon_path.write_text(json.dumps(stale), encoding="utf-8")
            write_action_bound_to_recon(company_dir, "2024-01", cwd=root)
            original_run = full_year_dry_run.subprocess.run
            original_periods = full_year_dry_run.periods_for_year
            original_resolve = full_year_dry_run.resolve_company_name
            try:
                full_year_dry_run.subprocess.run = fake_run
                full_year_dry_run.periods_for_year = lambda _year: ["2024-01"]
                full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OU"
                summary = full_year_dry_run.run_full_year_dry_run(
                    company_dir=company_dir, year=2024, source_dir=None,
                    python_executable="python3", continue_on_error=False,
                    force_build=False, cwd=root,
                )
            finally:
                full_year_dry_run.subprocess.run = original_run
                full_year_dry_run.periods_for_year = original_periods
                full_year_dry_run.resolve_company_name = original_resolve

        self.assertFalse(summary["overall_success"])
        self.assertEqual(summary["aggregated_periods"], [])
        self.assertEqual(summary["bank_reconciliation_summary"]["physical_bank_row_count"], 0)

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


STATEMENT_IMPORT_POLICY = {
    "schema_version": "1.0",
    "company_slug": "example",
    "bank_accounts": {"EE123": {"EUR": "3"}},
    "contacts": {},
    "mappings": {},
    "supplier_aliases": {},
    "cash_posting": {
        "mode": "statement_import",
        "bank_income_account_ids": ["3"],
        "processor_income_account_ids": {},
        "bank_financial_accounts": {"EE123": {"EUR": "10"}},
        "clearing_provider_roles": {},
        "financial_accounts": {
            "bank": "10", "stripe_clearing": "30", "paypal": "31", "bank_fees": "32",
            "reporting_person_payable": "33", "platform_prepayment": "34",
            "customer_receivable": "37", "supplier_payable": "38",
            "fx_gain": "35", "fx_loss": "36",
        },
    },
}


class StatementImportOrchestrationTests(unittest.TestCase):
    def run_year(self, company_dir: Path, *, called: list[str]) -> dict:
        def fake_run(cmd: list[str], cwd: Path, capture_output: bool, text: bool) -> SimpleNamespace:
            del capture_output, text
            script = Path(cmd[1]).name
            called.append(script)
            if script == "bookrecon.py":
                write_bound_recon(company_dir, cmd[cmd.index("--period") + 1], cwd=cwd)
            if script == "bookbuilder.py":
                write_action_bound_to_recon(company_dir, cmd[cmd.index("--period") + 1], cwd=cwd)
            payload = {"result": "pass"} if script == "bookchecker.py" else {"ok": True}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        original_run = full_year_dry_run.subprocess.run
        original_periods = full_year_dry_run.periods_for_year
        original_resolve = full_year_dry_run.resolve_company_name
        try:
            full_year_dry_run.subprocess.run = fake_run
            full_year_dry_run.periods_for_year = lambda _year: ["2024-01"]
            full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OÜ"
            return full_year_dry_run.run_full_year_dry_run(
                company_dir=company_dir,
                year=2024,
                source_dir=None,
                python_executable="python3",
                continue_on_error=False,
                force_build=False,
                cwd=Path.cwd(),
            )
        finally:
            full_year_dry_run.subprocess.run = original_run
            full_year_dry_run.periods_for_year = original_periods
            full_year_dry_run.resolve_company_name = original_resolve

    def company(self, tmp: Path, policy: dict) -> Path:
        company_dir = tmp / "companies" / "example"
        (company_dir / "artifacts").mkdir(parents=True)
        (company_dir / "artifacts" / "posting_policy.json").write_text(json.dumps(policy), encoding="utf-8")
        return company_dir

    def test_the_annual_plan_is_generated_before_any_monthly_build(self) -> None:
        called: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            self.run_year(self.company(Path(tmp), STATEMENT_IMPORT_POLICY), called=called)

        self.assertIn("statement_import_plan.py", called)
        self.assertLess(called.index("statement_import_plan.py"), called.index("bookbuilder.py"))

    def test_a_statement_import_year_stops_at_the_import_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_year(self.company(Path(tmp), STATEMENT_IMPORT_POLICY), called=[])

        self.assertEqual(summary["phase"], "statement_import_pending")

    def test_a_statement_import_year_submits_no_bank_api_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_year(self.company(Path(tmp), STATEMENT_IMPORT_POLICY), called=[])

        self.assertEqual(summary["bank_api_cash_action_count"], 0)

    def test_an_api_cash_year_runs_no_statement_import_plan_step(self) -> None:
        called: list[str] = []
        policy = dict(STATEMENT_IMPORT_POLICY, cash_posting={"mode": "api"})
        with tempfile.TemporaryDirectory() as tmp:
            summary = self.run_year(self.company(Path(tmp), policy), called=called)

        self.assertNotIn("statement_import_plan.py", called)
        self.assertEqual(summary["phase"], "documents_ready")


class RunPhaseTests(unittest.TestCase):
    def resolve(self, **overrides: object) -> str:
        kwargs: dict = {
            "statement_import_mode": True,
            "master_data_resolved": True,
            "documents_ready": True,
            "ledger_evidence_status": None,
            "inventory_audit_status": None,
            "fx_revaluation_settled": False,
        }
        kwargs.update(overrides)
        return full_year_dry_run.resolve_run_phase(full_year_dry_run.YearGates(**kwargs))

    def test_every_phase_it_can_return_is_a_declared_phase(self) -> None:
        phases = {
            self.resolve(master_data_resolved=False),
            self.resolve(documents_ready=False),
            self.resolve(statement_import_mode=False),
            self.resolve(),
            self.resolve(ledger_evidence_status="fail"),
            self.resolve(ledger_evidence_status="pass"),
            self.resolve(ledger_evidence_status="pass", inventory_audit_status="pass"),
        }

        self.assertTrue(phases <= set(full_year_dry_run.PHASES))

    def test_unresolved_master_data_holds_the_run_at_source_ready(self) -> None:
        self.assertEqual(self.resolve(master_data_resolved=False), "source_ready")

    def test_incomplete_documents_hold_the_run_before_the_import(self) -> None:
        self.assertEqual(self.resolve(documents_ready=False), "master_data_ready")

    def test_a_failing_ledger_export_holds_the_run_at_ledger_evidence(self) -> None:
        self.assertEqual(self.resolve(ledger_evidence_status="fail"), "ledger_evidence_pending")

    def test_a_passing_ledger_export_moves_on_to_the_inventory_audit(self) -> None:
        self.assertEqual(self.resolve(ledger_evidence_status="pass"), "inventory_audit_pending")

    def test_everything_proven_reaches_the_final_checks(self) -> None:
        self.assertEqual(
            self.resolve(
                ledger_evidence_status="pass",
                inventory_audit_status="pass",
                fx_revaluation_settled=True,
            ),
            "final_checks_ready",
        )

    def test_an_unanswered_fx_revaluation_holds_an_otherwise_proven_year(self) -> None:
        self.assertEqual(
            self.resolve(ledger_evidence_status="pass", inventory_audit_status="pass"),
            "fx_revaluation_pending",
        )


class FxRevaluationGateTests(unittest.TestCase):
    def status(self, **overrides: object) -> dict:
        evidence = {
            "year": 2024,
            "required": True,
            "status": "pending",
            "balances": {"USD": "4670.50"},
        }
        evidence.update(overrides)
        return full_year_dry_run.fx_revaluation_state(evidence)

    def test_a_year_with_no_foreign_balance_needs_no_revaluation(self) -> None:
        state = self.status(required=False, balances={}, status="not_required")

        self.assertEqual(state["verdict"], "not_required")
        self.assertTrue(state["settled"])

    def test_a_required_but_unposted_revaluation_is_not_settled(self) -> None:
        state = self.status()

        self.assertEqual(state["verdict"], "pending")
        self.assertFalse(state["settled"])

    def test_a_posted_revaluation_settles_the_year(self) -> None:
        state = self.status(status="posted")

        self.assertEqual(state["verdict"], "posted")
        self.assertTrue(state["settled"])

    def test_absent_evidence_is_treated_as_unanswered_not_as_done(self) -> None:
        state = full_year_dry_run.fx_revaluation_state(None)

        self.assertEqual(state["verdict"], "unknown")
        self.assertFalse(state["settled"])

    def test_a_foreign_balance_with_a_not_required_claim_is_contradictory(self) -> None:
        state = self.status(required=False, status="not_required")

        self.assertEqual(state["verdict"], "contradictory")
        self.assertFalse(state["settled"])


class FxRevaluationPhaseTests(unittest.TestCase):
    def resolve(self, **overrides: object) -> str:
        kwargs: dict = {
            "statement_import_mode": True,
            "master_data_resolved": True,
            "documents_ready": True,
            "ledger_evidence_status": "pass",
            "inventory_audit_status": "pass",
            "fx_revaluation_settled": True,
        }
        kwargs.update(overrides)
        return full_year_dry_run.resolve_run_phase(full_year_dry_run.YearGates(**kwargs))

    def test_an_unsettled_revaluation_holds_the_year_open(self) -> None:
        self.assertEqual(self.resolve(fx_revaluation_settled=False), "fx_revaluation_pending")

    def test_a_settled_revaluation_reaches_the_final_checks(self) -> None:
        self.assertEqual(self.resolve(), "final_checks_ready")

    def test_the_revaluation_gate_sits_after_the_inventory_audit(self) -> None:
        phases = full_year_dry_run.PHASES

        self.assertLess(phases.index("inventory_audit_pending"), phases.index("fx_revaluation_pending"))
        self.assertLess(phases.index("fx_revaluation_pending"), phases.index("final_checks_ready"))

    def test_an_earlier_gate_still_wins_over_the_revaluation_gate(self) -> None:
        self.assertEqual(
            self.resolve(inventory_audit_status="fail", fx_revaluation_settled=False),
            "inventory_audit_pending",
        )


class PlanOrderingTests(unittest.TestCase):
    def test_every_month_is_normalized_before_the_annual_plan_is_built(self) -> None:
        called: list[str] = []

        def fake_run(cmd: list[str], cwd: Path, capture_output: bool, text: bool) -> SimpleNamespace:
            del capture_output, text
            script = Path(cmd[1]).name
            called.append(script)
            if script == "bookrecon.py":
                write_bound_recon(company_dir, cmd[cmd.index("--period") + 1], cwd=cwd)
            if script == "bookbuilder.py":
                write_action_bound_to_recon(company_dir, cmd[cmd.index("--period") + 1], cwd=cwd)
            payload = {"result": "pass"} if script == "bookchecker.py" else {"ok": True}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            (company_dir / "artifacts").mkdir(parents=True)
            (company_dir / "artifacts" / "posting_policy.json").write_text(
                json.dumps(STATEMENT_IMPORT_POLICY), encoding="utf-8")
            original_run = full_year_dry_run.subprocess.run
            original_periods = full_year_dry_run.periods_for_year
            original_resolve = full_year_dry_run.resolve_company_name
            try:
                full_year_dry_run.subprocess.run = fake_run
                full_year_dry_run.periods_for_year = lambda _y: ["2024-01", "2024-02"]
                full_year_dry_run.resolve_company_name = lambda company_dir: "Example Company OU"
                full_year_dry_run.run_full_year_dry_run(
                    company_dir=company_dir, year=2024, source_dir=None,
                    python_executable="python3", continue_on_error=True,
                    force_build=False, cwd=Path.cwd(),
                )
            finally:
                full_year_dry_run.subprocess.run = original_run
                full_year_dry_run.periods_for_year = original_periods
                full_year_dry_run.resolve_company_name = original_resolve

        plan_at = called.index("statement_import_plan.py")
        # The plan is derived from the normalized artifacts, so every month must be
        # normalized before it is built -- otherwise it describes the previous run.
        # Once per month, all before the plan, and not repeated inside the month loop.
        self.assertEqual(called.count("bookprep.py"), 2)
        self.assertLess(plan_at, called.index("bookbuilder.py"))
        self.assertEqual([s for s in called[:plan_at] if s == "bookprep.py"], ["bookprep.py"] * 2)


if __name__ == "__main__":
    unittest.main()


class ProcessorSettlementAccountTests(unittest.TestCase):
    """A reviewed processor account is a legitimate cash target, not a policy mismatch.

    The narrow bank_accounts map names only real bank accounts, so a settlement posted
    into PayPal or Stripe would otherwise be counted as a mismatch on every month.
    """

    def summary_for(self, bank_account_id: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / "companies" / "example"
            actions_dir = company_dir / "artifacts" / "actions"
            actions_dir.mkdir(parents=True)
            (company_dir / "artifacts" / "posting_policy.json").write_text(
                json.dumps({
                    "bank_accounts": {"EE123": "3"},
                    "cash_posting": {
                        "mode": "statement_import",
                        "bank_income_account_ids": ["3"],
                        "processor_income_account_ids": {"paypal": "6", "stripe": "7"},
                        "bank_financial_accounts": {"EE123": {"EUR": "2"}},
                        "clearing_provider_roles": {"paypal": "paypal"},
                        "financial_accounts": {
                            "bank": "2", "stripe_clearing": "263", "paypal": "260",
                            "bank_fees": "141", "reporting_person_payable": "232",
                            "platform_prepayment": "256", "customer_receivable": "5",
                            "supplier_payable": "52", "fx_gain": "113", "fx_loss": "184",
                        },
                    },
                }),
                encoding="utf-8",
            )
            (actions_dir / "2024-01.yaml").write_text(
                json.dumps({"actions": [{
                    "action_type": "create_incoming_summary",
                    "payload": {"currency": "EUR", "settlement_family": "processor-held",
                                "counterparty_hint": "stripe", "bank_account_id": bank_account_id},
                    "source_refs": [],
                }]}),
                encoding="utf-8",
            )
            return full_year_dry_run.summarize_action_artifacts(company_dir=company_dir, year=2024)

    def test_a_reviewed_processor_account_is_not_a_mismatch(self) -> None:
        self.assertEqual(self.summary_for("7")["policy_mapping_mismatch_count"], 0)

    def test_an_unreviewed_cash_account_is_still_a_mismatch(self) -> None:
        self.assertEqual(self.summary_for("99")["policy_mapping_mismatch_count"], 1)
