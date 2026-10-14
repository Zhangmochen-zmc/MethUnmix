"""Synthetic Stage 3--8 integration checks.

The suite deliberately exercises production control paths with only marked
fixture archives and a Python test executor.  It neither starts Nextflow nor
claims a model/scientific E2E result.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from demethflow_core.asset_lifecycle import create_catalog_plan, import_catalog_plan, verify_catalog_plan
from demethflow_core.benchmark import benchmark
from demethflow_core.cli import main as cli_main
from demethflow_core.errors import DeMethFlowError
from demethflow_core.manifest import load_reference
from demethflow_core.release import _build_module_archive
from demethflow_core.reporting import build_report
from demethflow_core.runner import DeconRequest, _offline_env, run_decon
from demethflow_core.runtime import DoctorResult, execution_plan_for_doctor
from demethflow_core.scientific_evidence import audit, import_record, initialize, query
from demethflow_core.util import data_home_override, sha256_file


def _url(digest: str, name: str) -> str:
    return f"https://fixtures.invalid/objects/sha256/{digest}/{name}"


class Stage3To8SyntheticIntegrationTests(unittest.TestCase):
    """One success chain plus fail-closed boundary cases."""

    def _bundle_and_archives(self, root: Path):
        reference_root = root / "reference"
        reference_root.mkdir()
        manifest = {
            "schema": "demethflow-reference-v1", "reference_id": "fixture.integration.450k", "version": "1.0.0",
            "scenario_id": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
            "compatible_input_routes": ["450k"], "genome_build": "not_applicable", "status": "READY",
            "cell_types": [
                {"cell_type_id": "cell_a", "display_name": "Cell A"},
                {"cell_type_id": "cell_b", "display_name": "Cell B"},
            ],
            "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
            "tool_capabilities": {
                "Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": ["runtime.fixture"]},
            },
            "artifact_checksums": {},
        }
        manifest_path = reference_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        bundle = load_reference(manifest_path)
        reference_archive, _ = __import__("demethflow_core.packaging", fromlist=["export_reference"]).export_reference(
            bundle, root / "archives"
        )
        runtime_source = root / "runtime-source"
        runtime_source.mkdir()
        (runtime_source / "entrypoint.txt").write_text("synthetic only\n", encoding="utf-8")
        runtime_archive = root / "archives" / "runtime.fixture-1.0.0.tar.gz"
        _build_module_archive(
            runtime_archive, module_id="runtime.fixture", module_type="runtime", module_version="1.0.0",
            sources=[(runtime_source / "entrypoint.txt", "entrypoint.txt")],
        )
        return bundle, reference_archive, runtime_archive

    def _catalog(self, root: Path, bundle, reference_archive: Path, runtime_archive: Path) -> Path:
        ref_sha, runtime_sha = sha256_file(reference_archive), sha256_file(runtime_archive)
        catalog = {
            "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "core_version": "2.1.0-dev",
            "catalog_release": "synthetic-stage3-8-only", "catalog_sequence": 1,
            "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [
                {
                    "asset_id": "reference.fixture.integration.450k", "version": "1.0.0", "module_type": "reference",
                    "selector": bundle.selector, "reference_id": bundle.reference_id, "scenario": "fixture",
                    "source_platform": "450k", "compatible_input_routes": ["450k"], "analysis_contract": "array_450k_cpg",
                    "genome_build": "not_applicable", "content_digest": bundle.content_digest(),
                    "tool_capabilities": {"Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": ["runtime.fixture"]}},
                    "distribution_status": "PUBLIC_DOWNLOADABLE", "license_status": "LICENSE_APPROVED", "license_evidence": "synthetic only",
                    "immutable_object": True, "sha256": ref_sha, "length": reference_archive.stat().st_size,
                    "installed_bytes": 4096, "url": _url(ref_sha, reference_archive.name), "dependencies": [],
                    "tool_dependencies": {"Houseman": [{"asset_id": "runtime.fixture", "version": "1.0.0", "sha256": runtime_sha}]},
                },
                {
                    "asset_id": "runtime.fixture", "version": "1.0.0", "module_type": "runtime",
                    "distribution_status": "PUBLIC_DOWNLOADABLE", "license_status": "LICENSE_APPROVED", "license_evidence": "synthetic only",
                    "immutable_object": True, "sha256": runtime_sha, "length": runtime_archive.stat().st_size,
                    "installed_bytes": 1024, "url": _url(runtime_sha, runtime_archive.name), "dependencies": [],
                    "compatibility": {"core_api": "2.0"},
                },
            ],
        }
        path = root / "synthetic-catalog.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        return path

    def _doctor_result(self, bundle, input_path: Path) -> DoctorResult:
        result = DoctorResult(
            bundle=bundle, selected_tools=["Houseman"], accelerator="cpu",
            nextflow_command=["synthetic-executor"], container_engine="fixture",
        )
        result.add("reference-resolution", True, bundle.selector)
        result.add("asset-installation", True, "synthetic exact locked assets imported")
        result.execution_plan = execution_plan_for_doctor(
            result, mode="decon", scenario="fixture", platform_name="450k", genome_build=None,
            analysis_contract="array_450k_cpg", input_path=input_path, requested_tools="Houseman",
            allow_cross_platform=False,
            request_provenance={"schema": "methunmix-resolved-analysis-request-v1", "entrypoint": "synthetic-integration"},
            check_mode="execution_preflight",
        )
        return result

    def _executor(self, root: Path) -> Path:
        path = root / "synthetic_executor.py"
        path.write_text(
            "import pathlib, sys, time\n"
            "mode, outdir = sys.argv[1], pathlib.Path(sys.argv[2])\n"
            "if mode == 'timeout': time.sleep(2)\n"
            "elif mode == 'success':\n"
            "  d = outdir / 'results'; d.mkdir(parents=True, exist_ok=True)\n"
            "  (d / 'Houseman_results.csv').write_text('SampleID,Cell A,Cell B\\ns1,0.7,0.3\\ns2,0.2,0.8\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        return path

    def _request(self, root: Path, input_path: Path, *, timeout: int = 5) -> DeconRequest:
        return DeconRequest(
            input_path=input_path, outdir=root / "run", platform="450k", scenario="fixture",
            reference="fixture.integration.450k@1.0.0", tools="Houseman", runtime="container",
            random_seed=17, preprocess_threads=1, workflow_timeout_seconds=timeout,
            request_provenance={"schema": "methunmix-resolved-analysis-request-v1", "entrypoint": "synthetic-integration"},
        )

    def test_complete_synthetic_asset_to_evidence_chain(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-stage3-8-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                bundle, reference_archive, runtime_archive = self._bundle_and_archives(root)
                catalog = self._catalog(root, bundle, reference_archive, runtime_archive)
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None, analysis_contract="array_450k_cpg",
                        tools="Houseman", output=root / "installation-plan.json",
                    )
                self.assertEqual(code, 0); self.assertTrue(plan["installable"])
                self.assertEqual({row["asset_id"] for row in plan["assets"]}, {"reference.fixture.integration.450k", "runtime.fixture"})
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    imported, code = import_catalog_plan(root / "installation-plan.json", [reference_archive, runtime_archive])
                    self.assertEqual(code, 0); self.assertEqual(imported["status"], "COMPLETE")
                    verified, code = verify_catalog_plan(root / "installation-plan.json")
                self.assertEqual(code, 0); self.assertTrue(all(row["ok"] for row in verified["assets"]))

                input_path = root / "input.csv"
                input_path.write_text("SampleID,marker\ns1,0.1\ns2,0.2\n", encoding="utf-8")
                doctor_result = self._doctor_result(bundle, input_path)
                self.assertTrue(doctor_result.ok)
                self.assertEqual(doctor_result.execution_plan["tools"]["selected"][0]["tool"], "Houseman")
                executor = self._executor(root)
                request = self._request(root, input_path)
                command = [sys.executable, str(executor), "success", str(request.outdir)]
                with patch("demethflow_core.runner.prepare_decon", return_value=(doctor_result, command, _offline_env(request.outdir))):
                    manifest = run_decon(request)
                self.assertEqual(manifest["execution_status"], "SUCCEEDED")
                canonical = request.outdir / "canonical" / "Houseman_results.csv"
                self.assertTrue(canonical.is_file())
                summary = json.loads((request.outdir / "result_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["tools"]["Houseman"]["output_contract_status"], "CONFORMANT")

                truth = root / "truth.csv"
                truth.write_text("SampleID,cell_a,cell_b\ns1,0.8,0.2\ns2,0.1,0.9\n", encoding="utf-8")
                measured = benchmark(canonical, truth, context={"tool": "Houseman", "profile": "cpu"})
                self.assertEqual(measured["status"], "METRICS_COMPUTED_NO_STATUS_PROMOTION")
                self.assertEqual(measured["metrics"]["overall_mae"], 0.1)
                report = build_report(request.outdir, root / "derived-report", truth=truth, tool="Houseman")
                self.assertEqual(report["truth_metrics"]["status"], "METRICS_COMPUTED_REPORT_ONLY")
                self.assertEqual(report["truth_metrics"]["metrics"], measured["metrics"])

                registry = root / "local-test-evidence-registry.json"
                initialize(registry, registry_id="synthetic-stage3-8")
                record = {
                    "evidence_id": "synthetic-stage3-8-houseman", "schema_version": "methunmix-scientific-evidence-v1",
                    "evidence_type": "FIXED_SIMULATED_TRUTH",
                    "validation_object": {"reference_selector": bundle.selector, "reference_content_digest": bundle.content_digest(),
                        "tool": "Houseman", "adapter": "houseman-array-v1", "profile": "cpu", "scenario": "fixture",
                        "platform": "450k", "genome_build": "not_applicable", "analysis_contract": "array_450k_cpg"},
                    "input_identity": manifest["input_identity"], "truth_identity": {"sha256": sha256_file(truth)},
                    "software_identity": manifest["software_identity"], "runtime_identity": {"synthetic_runtime_archive_sha256": sha256_file(runtime_archive)},
                    "parameters": manifest["resolved_execution_parameters"], "seed": 17, "sample_axis": ["s1", "s2"],
                    "output_axis": ["cell_a", "cell_b"], "metrics": measured["metrics"], "acceptance_policy": None,
                    "limitations": ["synthetic executor and synthetic truth only", "report-only; no scientific status promotion"],
                    "source": {"run_manifest_sha256": sha256_file(request.outdir / "run_manifest.json"), "report_sha256": sha256_file(root / "derived-report" / "report.json")},
                }
                self.assertEqual(import_record(registry, record)["status"], "IMPORTED_UNREVIEWED")
                self.assertEqual(query(registry, selector=bundle.selector)["record_count"], 1)
                audited = audit(registry, catalog_path=catalog)
                self.assertEqual(audited["status"], "PASS_STRUCTURAL_NO_STATUS_PROMOTION")

    def test_preexecution_dependency_block_does_not_start_executor(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-stage3-8-block-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                bundle, reference_archive, runtime_archive = self._bundle_and_archives(root)
                catalog = self._catalog(root, bundle, reference_archive, runtime_archive)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                payload["targets"][0]["tool_dependencies"]["Houseman"] = [{"asset_id": "missing.runtime", "version": "1.0.0"}]
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(scenario="fixture", platform="450k", genome_build=None,
                        analysis_contract="array_450k_cpg", tools="Houseman", output=root / "blocked-plan.json")
                self.assertEqual(code, 2); self.assertFalse(plan["installable"])
                self.assertIn("DEPENDENCY_MISSING_FROM_CATALOG", [item["code"] for item in plan["blockers"]])
                self.assertFalse((root / "executor-ran").exists())

    def test_local_runtime_drift_after_doctor_blocks_before_workflow_launch(self):
        """The runner must not treat a successful doctor as launch proof.

        This uses the production ``run_decon`` path and a Python fixture
        executor.  The fixture never runs: its sole purpose is to prove that
        a selected runtime changed between preflight and launch is recorded as
        a prelaunch failure rather than silently used by a workflow.
        """
        with tempfile.TemporaryDirectory(prefix="methunmix-local-binding-launch-") as temporary:
            root = Path(temporary)
            bundle, _reference_archive, _runtime_archive = self._bundle_and_archives(root)
            input_path = root / "input.csv"
            input_path.write_text("SampleID,marker\ns1,0.1\n", encoding="utf-8")
            runtime = root / "runtime.fixture.sif"
            runtime.write_bytes(b"locked-runtime\n")
            expected = {"type": "file", "sha256": sha256_file(runtime), "bytes": runtime.stat().st_size}
            request = self._request(root, input_path)
            request.installation_dependency_plan = {
                "state": "RESOLVED",
                "local_dependencies": [{
                    "name": "runtime.fixture", "kind": "runtime_module",
                    "identity": expected, "locator_key": "fixture-runtime",
                }],
            }
            result = self._doctor_result(bundle, input_path)
            result.runtime_files["runtime.fixture"] = runtime
            executor = self._executor(root)
            command = [sys.executable, str(executor), "success", str(request.outdir)]

            def stale_after_preflight(_request, **_kwargs):
                runtime.write_bytes(b"runtime-changed-after-doctor\n")
                return result, command, _offline_env(request.outdir)

            with patch("demethflow_core.runner.prepare_decon", side_effect=stale_after_preflight):
                with self.assertRaisesRegex(DeMethFlowError, "PRELAUNCH_BINDING_FAILED"):
                    run_decon(request)
            manifest = json.loads((request.outdir / "run_manifest.json").read_text(encoding="utf-8"))
            summary = json.loads((request.outdir / "result_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["execution_status"], "PRELAUNCH_BINDING_FAILED")
            self.assertFalse(manifest["workflow_started"])
            self.assertEqual(summary["execution_status"], "PRELAUNCH_BINDING_FAILED")
            self.assertFalse((request.outdir / ".demethflow" / "task_receipt.json").exists())
            self.assertFalse((request.outdir / "results").exists())

    def test_timeout_and_missing_output_are_separately_recorded(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-stage3-8-failures-") as temporary:
            root = Path(temporary)
            bundle, _reference_archive, _runtime_archive = self._bundle_and_archives(root)
            input_path = root / "input.csv"; input_path.write_text("SampleID,marker\ns1,0.1\n", encoding="utf-8")
            executor = self._executor(root)
            for mode, expected in (("timeout", "TIMEOUT"), ("missing", "OUTPUT_CONTRACT_FAILED")):
                run_root = root / mode
                request = self._request(run_root, input_path, timeout=1)
                result = self._doctor_result(bundle, input_path)
                command = [sys.executable, str(executor), mode, str(request.outdir)]
                with patch("demethflow_core.runner.prepare_decon", return_value=(result, command, _offline_env(request.outdir))):
                    with self.assertRaises(DeMethFlowError):
                        run_decon(request)
                manifest = json.loads((request.outdir / "run_manifest.json").read_text(encoding="utf-8"))
                summary = json.loads((request.outdir / "result_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["execution_status"], expected)
                self.assertEqual(summary["execution_status"], expected)

    def test_public_cli_fresh_retry_uses_a_new_real_runner_attempt(self):
        """Exercise retry parsing, validation, runner records, and lineage.

        The source failure and both retry attempts are generated by
        ``run_decon`` with a Python fixture executor.  The executor is test
        only: this is an engineering integration test, not a Houseman or any
        other algorithm execution claim.
        """
        with tempfile.TemporaryDirectory(prefix="methunmix-fresh-retry-chain-") as temporary:
            root = Path(temporary)
            bundle, _reference_archive, _runtime_archive = self._bundle_and_archives(root)
            input_path = root / "input.csv"
            input_path.write_text("SampleID,marker\ns1,0.1\ns2,0.2\n", encoding="utf-8")
            executor = self._executor(root)

            def prepared(request, **_kwargs):
                mode = "success" if request.outdir.name == "retry-success" else "missing"
                command = [sys.executable, str(executor), mode, str(request.outdir)]
                result = self._doctor_result(bundle, input_path)
                # The normal CLI resolves this object before runner entry;
                # reproduce that public-request provenance in the fixture.
                result.execution_plan["effective_request"] = request.request_provenance
                return result, command, _offline_env(request.outdir)

            source_request = self._request(root / "source-parent", input_path, timeout=10)
            source_request = DeconRequest(**{
                **source_request.__dict__, "outdir": root / "source-failed",
                "reference_root": root / "references", "runtime_root": root / "runtime",
                "request_provenance": {"source": "synthetic-integration", "effective_values": {
                    "scenario": "fixture", "input_platform": "450k",
                    "reference": "fixture.integration.450k@1.0.0", "runtime": "container",
                    "container_engine": "auto", "executor": "local", "accelerator": "cpu",
                    "parameters": {"random_seed": 17},
                }},
            })
            with patch("demethflow_core.runner.prepare_decon", side_effect=prepared):
                with self.assertRaises(DeMethFlowError):
                    run_decon(source_request)
            source = source_request.outdir
            source_manifest_path = source / "run_manifest.json"
            source_before = source_manifest_path.read_bytes()
            source_manifest = json.loads(source_before)
            self.assertEqual(source_manifest["execution_status"], "OUTPUT_CONTRACT_FAILED")
            self.assertEqual(source_manifest["run_lease"]["state"], "IDLE_CONFIRMED")
            self.assertEqual(
                json.loads((source / ".demethflow" / "task_receipt.json").read_text(encoding="utf-8"))["process_start_state"],
                "CONFIRMED_STARTED",
            )

            effective = source_manifest["execution_plan"]["effective_request"]["effective_values"]
            retry_request = {
                "scenario": effective["scenario"], "platform": effective["input_platform"],
                "reference": effective["reference"], "input": str(input_path),
                "runtime": effective["runtime"], "container_engine": effective["container_engine"],
                "executor": effective["executor"], "accelerator": effective["accelerator"],
                "random_seed": effective["parameters"]["random_seed"], "tools": ["Houseman"],
                "reference_root": str(root / "references"), "runtime_root": str(root / "runtime"),
                "workflow_timeout_seconds": source_manifest["execution_environment"]["workflow_timeout_seconds"],
                "verify_reference_checksums": True,
            }
            request_path = root / "retry-request.json"
            request_path.write_text(json.dumps(retry_request), encoding="utf-8")
            success = root / "retry-success"
            success_record = root / "retry-success-result.json"
            with patch("demethflow_core.runner.prepare_decon", side_effect=prepared):
                with redirect_stdout(StringIO()):
                    code = cli_main([
                        "retry-failed-tool", "--source-run-dir", str(source), "--tool", "Houseman",
                        "--outdir", str(success), "--request-json", str(request_path), "--execute",
                        "--output", str(success_record),
                    ])
            self.assertEqual(code, 0)
            self.assertEqual((source / "run_manifest.json").read_bytes(), source_before)
            success_manifest = json.loads((success / "run_manifest.json").read_text(encoding="utf-8"))
            attempt = json.loads((success / "fresh-retry-attempt.json").read_text(encoding="utf-8"))
            self.assertEqual(success_manifest["execution_status"], "SUCCEEDED")
            self.assertEqual(attempt["terminal_state"], "RUNNER_RETURNED")
            self.assertEqual(attempt["prior_result_reference"]["source_run_dir"], str(source.resolve()))
            self.assertTrue((success / ".demethflow" / "task_receipt.json").is_file())
            self.assertTrue((success / "canonical" / "Houseman_results.csv").is_file())
            self.assertNotIn("-resume", success_manifest["executed_command"])
            self.assertNotIn("step_cache", json.dumps(success_manifest, sort_keys=True))

            failed = root / "retry-failed-again"
            failed_record = root / "retry-failed-result.json"
            with patch("demethflow_core.runner.prepare_decon", side_effect=prepared):
                with redirect_stdout(StringIO()):
                    code = cli_main([
                        "retry-failed-tool", "--source-run-dir", str(source), "--tool", "Houseman",
                        "--outdir", str(failed), "--request-json", str(request_path), "--execute",
                        "--output", str(failed_record),
                    ])
            self.assertEqual(code, 2)
            failed_manifest = json.loads((failed / "run_manifest.json").read_text(encoding="utf-8"))
            failed_attempt = json.loads((failed / "fresh-retry-attempt.json").read_text(encoding="utf-8"))
            self.assertEqual(failed_manifest["execution_status"], "OUTPUT_CONTRACT_FAILED")
            self.assertEqual(failed_attempt["terminal_state"], "RUNNER_RAISED")
            self.assertTrue((failed / ".demethflow" / "task_receipt.json").is_file())
