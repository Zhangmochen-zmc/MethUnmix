from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from demethflow_core import cli
from demethflow_core.errors import DeMethFlowError
from demethflow_core.fresh_retry import execute_fresh_retry, validate_fresh_retry
from demethflow_core.software_identity import EXECUTION_CONTENT_SCHEMA
from demethflow_core.util import sha256_file


class FreshRetryTests(unittest.TestCase):
    def test_execution_content_scope_excludes_unshipped_vendor_files(self):
        from demethflow_core.software_identity import _included_asset_path
        self.assertFalse(_included_asset_path(Path("deconvolution/wgbs_vendor/UXM/src/uxm.py")))
        self.assertFalse(_included_asset_path(Path("deconvolution/vendor/prmeth_R/code.R")))
        self.assertTrue(_included_asset_path(Path("deconvolution/bin/houseman_decon.R")))
        self.assertTrue(_included_asset_path(Path("nextflow_ref/modules/tools.nf")))

    def _fixture(self, root: Path) -> tuple[Path, Path, dict[str, object]]:
        source = root / "old"; (source / ".demethflow").mkdir(parents=True)
        input_path = root / "input.csv"; input_path.write_text("SampleID,A\ns1,0.1\n", encoding="utf-8")
        manifest = {
            "status": "TIMEOUT", "selected_tools": ["MeDeCom"],
            "input_identity": {"sha256": sha256_file(input_path)}, "reference_digest": "reference",
            "software_identity": {"core_version": "fixture", "source_commit": "fixture-commit", "execution_content_identity": {"schema": EXECUTION_CONTENT_SCHEMA, "sha256": "a" * 64}},
            "resolved_execution_parameters": {"MeDeCom": {"k": 4, "lambda": "auto", "nfolds": 5}},
            "execution_plan": {"effective_request": {"effective_values": {
                "scenario": "brain", "input_platform": "450k", "reference": "builtin.brain.450k@1.5.0",
                "runtime": "container", "container_engine": "/fixture/apptainer", "executor": "local",
                "accelerator": "cpu", "parameters": {"random_seed": 7},
            }}},
            "execution_environment": {"workflow_timeout_seconds": 43200},
            "run_lease": {"state": "IDLE_CONFIRMED"},
        }
        (source / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (source / ".demethflow" / "task_receipt.json").write_text(json.dumps({"process_start_state": "CONFIRMED_STARTED", "orphan_free": True, "remaining_group_members": []}), encoding="utf-8")
        request = {"scenario":"brain", "platform":"450k", "reference":"builtin.brain.450k@1.5.0", "input":str(input_path), "runtime":"container", "container_engine":"/fixture/apptainer", "executor":"local", "accelerator":"cpu", "random_seed":7, "tools":["MeDeCom"], "medecom_k":4, "medecom_lambda":"auto", "medecom_nfolds":5, "reference_root":str(root / "references"), "runtime_root":str(root / "runtime"), "workflow_timeout_seconds":43200, "verify_reference_checksums":True}
        return source, input_path, request

    @staticmethod
    def _software() -> dict[str, object]:
        return {"core_version": "fixture", "source_commit": "fixture-commit", "execution_content_identity": {"schema": EXECUTION_CONTENT_SCHEMA, "sha256": "a" * 64}}

    def test_preview_requires_explicit_execute_and_does_not_create_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source, _, request=self._fixture(root); target=root / "new"
            result=validate_fresh_retry(source, "MeDeCom", target, request, current_software=self._software)
            self.assertEqual(result["decision"], "FRESH_RUN_REQUIRES_EXPLICIT_EXECUTE")
            self.assertFalse(target.exists())
            self.assertIn("does not pass -resume", result["explicitly_not_done"])

    def test_active_unknown_or_successful_source_and_existing_target_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source, _, request=self._fixture(root); target=root / "new"
            manifest=json.loads((source / "run_manifest.json").read_text()); manifest["run_lease"]["state"]="ACTIVE"; (source / "run_manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(DeMethFlowError, "lease"):
                validate_fresh_retry(source, "MeDeCom", target, request, current_software=self._software)
            manifest["run_lease"]["state"]="UNKNOWN"; (source / "run_manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(DeMethFlowError, "lease"):
                validate_fresh_retry(source, "MeDeCom", target, request, current_software=self._software)
            manifest["run_lease"]["state"]="IDLE_CONFIRMED"; manifest["status"]="SUCCEEDED"; (source / "run_manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(DeMethFlowError, "terminally failed"):
                validate_fresh_retry(source, "MeDeCom", target, request, current_software=self._software)
            manifest["status"]="TIMEOUT"; (source / "run_manifest.json").write_text(json.dumps(manifest)); target.mkdir()
            with self.assertRaisesRegex(DeMethFlowError, "already exists"):
                validate_fresh_retry(source, "MeDeCom", target, request, current_software=self._software)

    def test_drift_and_tool_expansion_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source, _, request=self._fixture(root)
            with self.assertRaisesRegex(DeMethFlowError, "only the failed tool"):
                validate_fresh_retry(source, "MeDeCom", root / "bad-tools", {**request, "tools":["MeDeCom", "Houseman"]}, current_software=self._software)
            with self.assertRaisesRegex(DeMethFlowError, "execution content differs"):
                validate_fresh_retry(source, "MeDeCom", root / "drift", request, current_software=lambda: {"core_version":"fixture", "source_commit":"new", "execution_content_identity": {"schema": EXECUTION_CONTENT_SCHEMA, "sha256": "b" * 64}})
            with self.assertRaisesRegex(DeMethFlowError, "core version is incompatible"):
                validate_fresh_retry(source, "MeDeCom", root / "version-drift", request, current_software=lambda: {"core_version":"new", "source_commit":"fixture-commit", "execution_content_identity": {"schema": EXECUTION_CONTENT_SCHEMA, "sha256": "a" * 64}})
            missing_roots = dict(request); missing_roots.pop("runtime_root")
            with self.assertRaisesRegex(DeMethFlowError, "runtime_root"):
                validate_fresh_retry(source, "MeDeCom", root / "missing-roots", missing_roots, current_software=self._software)

    def test_missing_legacy_execution_identity_is_rejected_and_unknown_commit_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source, _, request=self._fixture(root)
            manifest=json.loads((source / "run_manifest.json").read_text())
            manifest["software_identity"].pop("execution_content_identity")
            (source / "run_manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(DeMethFlowError, "no execution-content identity"):
                validate_fresh_retry(source, "MeDeCom", root / "legacy", request, current_software=self._software)
            manifest["software_identity"]["execution_content_identity"]={"schema": EXECUTION_CONTENT_SCHEMA, "sha256": "a" * 64}
            manifest["software_identity"]["source_commit"]=None
            (source / "run_manifest.json").write_text(json.dumps(manifest))
            installed={"core_version":"fixture", "source_commit":None, "source_commit_status":"UNKNOWN_NOT_EMBEDDED_IN_INSTALLED_ARTIFACT", "execution_content_identity":{"schema": EXECUTION_CONTENT_SCHEMA, "sha256":"a" * 64}}
            self.assertEqual(validate_fresh_retry(source, "MeDeCom", root / "installed", request, current_software=lambda: installed)["decision"], "FRESH_RUN_REQUIRES_EXPLICIT_EXECUTE")

    def test_execute_creates_new_attempt_and_calls_only_public_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source, _, request=self._fixture(root); target=root / "new"; old_manifest=(source / "run_manifest.json").read_bytes(); called=[]
            def fake_run(value):
                called.append(value); return {"status":"COMPLETE"}
            with patch("demethflow_core.fresh_retry.run_decon", side_effect=fake_run):
                result=execute_fresh_retry(source, "MeDeCom", target, request, current_software=self._software)
            self.assertEqual(result["status"], "COMPLETE"); self.assertEqual(len(called), 1)
            self.assertEqual(called[0].tools, "MeDeCom"); self.assertFalse(called[0].resume); self.assertIsNone(called[0].step_cache_root)
            record=json.loads((target / "fresh-retry-attempt.json").read_text())
            self.assertEqual(record["prior_result_reference"]["mode"], "READ_ONLY")
            self.assertTrue((target / "fresh-retry-attempt.json").is_file()); self.assertEqual((source / "run_manifest.json").read_bytes(), old_manifest)

    def test_runner_failure_and_interruption_are_recorded_without_mutating_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source, _, request=self._fixture(root); old_manifest=(source / "run_manifest.json").read_bytes()
            for label, error, expected in (("failure", RuntimeError("nonzero or timeout"), "RUNNER_RAISED"), ("interrupt", KeyboardInterrupt(), "RUNNER_INTERRUPTED")):
                target=root / label
                with patch("demethflow_core.fresh_retry.run_decon", side_effect=error):
                    with self.assertRaises(type(error)):
                        execute_fresh_retry(source, "MeDeCom", target, request, current_software=self._software)
                record=json.loads((target / "fresh-retry-attempt.json").read_text())
                self.assertEqual(record["terminal_state"], expected)
            self.assertEqual((source / "run_manifest.json").read_bytes(), old_manifest)

    def test_delivered_cli_preview_uses_the_public_retry_control_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source, _, request=self._fixture(root); target=root / "new"; output=root / "preview.json"
            from demethflow_core import version
            from demethflow_core.software_identity import current_software_identity
            # A portable source archive deliberately has no .git directory.
            # The retry contract compares execution content; source commits are
            # independent provenance and may therefore be unknown in this test.
            current = current_software_identity()
            manifest=json.loads((source / "run_manifest.json").read_text())
            manifest["software_identity"]={
                "core_version":version(), "source_commit":current["source_commit"],
                "source_commit_status":current["source_commit_status"],
                "execution_content_identity": current["execution_content_identity"],
            }
            (source / "run_manifest.json").write_text(json.dumps(manifest))
            request_path=root / "request.json"; request_path.write_text(json.dumps(request), encoding="utf-8")
            completed=subprocess.run([sys.executable, "-c", "from demethflow_core.cli import main; raise SystemExit(main())", "retry-failed-tool", "--source-run-dir", str(source), "--tool", "MeDeCom", "--outdir", str(target), "--request-json", str(request_path), "--output", str(output)], capture_output=True, text=True, env={**os.environ,"PYTHONPATH":str(Path(__file__).resolve().parents[1]/"src")})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(output.read_text())["decision"], "FRESH_RUN_REQUIRES_EXPLICIT_EXECUTE")
            self.assertFalse(target.exists())

    def test_public_retry_cli_passes_current_binding_documents_only_as_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, _, request = self._fixture(root)
            request_path = root / "request.json"; request_path.write_text(json.dumps(request), encoding="utf-8")
            output = root / "preview.json"; lock, locators = root / "lock.json", root / "locators.json"
            lock.write_text("{}", encoding="utf-8"); locators.write_text("{}", encoding="utf-8")
            with patch("demethflow_core.cli.execute_fresh_retry", return_value={"state": "synthetic"}) as execute:
                with patch("sys.stdout"):
                    code = cli.main([
                        "retry-failed-tool", "--source-run-dir", str(source), "--tool", "MeDeCom",
                        "--outdir", str(root / "target"), "--request-json", str(request_path),
                        "--local-dependency-lock", str(lock), "--local-dependency-locators", str(locators),
                        "--execute", "--output", str(output),
                    ])
            self.assertEqual(code, 0); self.assertTrue(output.is_file())
            kwargs = execute.call_args.kwargs
            self.assertEqual(kwargs["local_dependency_lock"], lock)
            self.assertEqual(kwargs["local_dependency_locators"], locators)

    def test_bound_source_revalidates_current_binding_and_passes_it_to_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, _, request = self._fixture(root)
            manifest = json.loads((source / "run_manifest.json").read_text())
            manifest["execution_plan"]["effective_request"]["effective_values"]["analysis_contract"] = "array_450k_cpg"
            manifest["execution_plan"]["effective_request"]["effective_values"]["genome_build"] = None
            manifest["installation_dependency_plan"] = {
                "state": "RESOLVED", "plan_identity": {"sha256": "a" * 64},
                "local_dependencies": [{"name": "MeDeCom", "kind": "logical_runtime_module"}],
            }
            (source / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(DeMethFlowError, "provide both current binding lock"):
                validate_fresh_retry(source, "MeDeCom", root / "missing", request, current_software=self._software)
            lock, locators = root / "lock.json", root / "locators.json"
            lock.write_text("{}", encoding="utf-8"); locators.write_text("{}", encoding="utf-8")
            current = {"installable": True, "plan_identity": {"sha256": "a" * 64}, "local_dependencies": [{"name": "MeDeCom", "kind": "logical_runtime_module"}]}
            with patch("demethflow_core.fresh_retry.create_catalog_plan", return_value=(current, 0)):
                preview = validate_fresh_retry(source, "MeDeCom", root / "preview", request,
                                               local_dependency_lock=lock, local_dependency_locators=locators,
                                               current_software=self._software)
            self.assertEqual(preview["current_installation_dependency_plan"], current)
            calls = []
            with patch("demethflow_core.fresh_retry.create_catalog_plan", return_value=(current, 0)), patch(
                "demethflow_core.fresh_retry.run_decon", side_effect=lambda value: calls.append(value) or {"status": "COMPLETE"},
            ):
                execute_fresh_retry(source, "MeDeCom", root / "execute", request,
                                    local_dependency_lock=lock, local_dependency_locators=locators,
                                    current_software=self._software)
            self.assertEqual(calls[0].installation_dependency_plan, current)
            mismatch = {**current, "plan_identity": {"sha256": "b" * 64}}
            with patch("demethflow_core.fresh_retry.create_catalog_plan", return_value=(mismatch, 0)):
                with self.assertRaisesRegex(DeMethFlowError, "plan identity differs"):
                    validate_fresh_retry(source, "MeDeCom", root / "mismatch", request,
                                         local_dependency_lock=lock, local_dependency_locators=locators,
                                         current_software=self._software)


if __name__ == "__main__":
    unittest.main()
