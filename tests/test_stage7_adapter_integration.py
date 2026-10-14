"""Synthetic integration coverage for bounded built-in adapter migrations.

The fixture invokes public core control paths with a test-only Python
executor.  It does not start Nextflow, containers, or a deconvolution model,
and records only engineering evidence.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from demethflow_core.reporting import build_report
from demethflow_core.manifest import load_reference
from demethflow_core.runner import DeconRequest, _offline_env, prepare_decon, run_decon
from demethflow_core.runtime import (
    DoctorResult,
    execution_plan_for_doctor,
    validate_selected_adapter_execution_contracts,
)
from demethflow_core.scientific_evidence import audit, import_record, initialize, query
from demethflow_core.util import sha256_file


# Controlled transcription of capabilities declared by the preserved
# brain/450K 1.5.0 manifest.  The original immutable manifest stays outside
# this test tree; its selector and exact digest make this fixture's provenance
# explicit without packaging a real reference payload.
R01_BRAIN_450K_MANIFEST = {
    "selector": "builtin.brain.450k@1.5.0",
    "manifest_sha256": "a41d2cd3181a604f027ae8e3cf234cb3570651df8fdc05eb6cf548e2cda6e4d9",
    "episcore": {
        "status": "READY",
        "artifacts": {
            "gene_reference": "artifacts/episcore/episcore_gene_reference.rds",
            "qc_report": "artifacts/episcore/episcore_qc.json",
            "scientific_validation": "artifacts/episcore/scientific_validation.json",
        },
        "runtime_modules": ["array-common", "EpiSCORE"],
        "output_contract": "cell_proportions_v1",
    },
    "edec": {
        "status": "READY",
        "artifacts": {
            "tref": "artifacts/edec/edec_tref.rds",
            "markers": "artifacts/edec/edec_stage0_markers.rds",
            "qc_report": "artifacts/edec/edec_qc.json",
            "scientific_validation": "artifacts/edec/scientific_validation.json",
        },
        "runtime_modules": ["array-common", "EDec"],
        "output_contract": "cell_proportions_v1",
    },
    "epidish": {
        "status": "READY",
        "artifacts": {"reference": "artifacts/epidish/EpiDISH_dynamic_reference.csv"},
        "runtime_modules": ["array-common", "EpiDISH"],
        "output_contract": "cell_proportions_v1",
    },
}


class Stage7AdapterIntegrationTests(unittest.TestCase):
    def _bundle(self, root: Path):
        manifest = {
            "schema": "demethflow-reference-v1", "reference_id": "fixture.stage7.array", "version": "1.0.0",
            "scenario_id": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
            "compatible_input_routes": ["450k"], "genome_build": "not_applicable", "status": "READY",
            "cell_types": [
                {"cell_type_id": "cell_a", "display_name": "Cell A"},
                {"cell_type_id": "cell_b", "display_name": "Cell B"},
            ],
            "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
            "tool_capabilities": {
                "EMeth": {"status": "READY", "artifacts": {"reference": "emeth.ref", "source": "emeth.src"}, "runtime_modules": ["array-common"]},
                "EDec": {
                    "status": R01_BRAIN_450K_MANIFEST["edec"]["status"],
                    "artifacts": {"tref": "edec.tref", "markers": "edec.markers"},
                    "runtime_modules": R01_BRAIN_450K_MANIFEST["edec"]["runtime_modules"],
                    "output_contract": R01_BRAIN_450K_MANIFEST["edec"]["output_contract"],
                },
                "EpiDISH": {
                    "status": R01_BRAIN_450K_MANIFEST["epidish"]["status"],
                    "artifacts": {"reference": "epidish.ref"},
                    "runtime_modules": R01_BRAIN_450K_MANIFEST["epidish"]["runtime_modules"],
                    "output_contract": R01_BRAIN_450K_MANIFEST["epidish"]["output_contract"],
                },
                "EpiSCORE": {
                    "status": R01_BRAIN_450K_MANIFEST["episcore"]["status"],
                    "artifacts": {"gene_reference": "episcore.gene"},
                    "runtime_modules": R01_BRAIN_450K_MANIFEST["episcore"]["runtime_modules"],
                    "output_contract": R01_BRAIN_450K_MANIFEST["episcore"]["output_contract"],
                },
            },
            "artifact_checksums": {},
        }
        reference = root / "reference"; reference.mkdir()
        # Manifest loading intentionally resolves every declared artifact.
        # These are inert fixture files, not algorithm/reference payloads.
        for name in ("emeth.ref", "emeth.src", "edec.tref", "edec.markers", "epidish.ref", "episcore.gene"):
            (reference / name).write_text("synthetic fixture only\\n", encoding="utf-8")
        (reference / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return load_reference(reference / "manifest.json")

    def _request(self, root: Path, input_path: Path, tool: str, *, timeout: int = 10) -> DeconRequest:
        return DeconRequest(
            input_path=input_path, outdir=root / tool, platform="450k", scenario="fixture",
            reference="fixture.stage7.array@1.0.0", tools=tool, runtime="container",
            container_engine="apptainer", accelerator="cpu", analysis_contract="array_450k_cpg",
            random_seed=71, workflow_timeout_seconds=timeout,
            request_provenance={"schema": "methunmix-resolved-analysis-request-v1", "entrypoint": "synthetic-stage7"},
        )

    def _prepared(self, bundle, request: DeconRequest) -> tuple[DoctorResult, list[str]]:
        tool = request.tools
        result = DoctorResult(
            bundle=bundle, selected_tools=[tool], accelerator="cpu", nextflow_command=["synthetic-nextflow"],
            container_engine="fixture",
        )
        result.add("reference-resolution", True, bundle.selector)
        result.add("runtime:array-common", True, "synthetic fixture only")
        result.execution_plan = execution_plan_for_doctor(
            result, mode="decon", scenario="fixture", platform_name="450k", genome_build=None,
            analysis_contract="array_450k_cpg", input_path=request.input_path, requested_tools=tool,
            allow_cross_platform=False, request_provenance=request.request_provenance,
            check_mode="execution_preflight",
        )
        with patch("demethflow_core.runner.doctor", return_value=result):
            prepared, command, _env = prepare_decon(request)
        return prepared, command

    def _executor(self, root: Path) -> Path:
        path = root / "fixture_executor.py"
        path.write_text(
            "import pathlib, sys\n"
            "mode, tool, outdir = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])\n"
            "if mode == 'success':\n"
            " d=outdir/'results'; d.mkdir(parents=True, exist_ok=True)\n"
            " if tool == 'EMeth':\n"
            "  (d/'EMeth_Normal_results.csv').write_text('SampleID,Cell A,Cell B\\ns1,0.7,0.3\\ns2,0.2,0.8\\n')\n"
            "  (d/'EMeth_Laplace_results.csv').write_text('SampleID,Cell A,Cell B\\ns1,0.6,0.4\\ns2,0.3,0.7\\n')\n"
            " elif tool == 'EpiDISH':\n"
            "  for method in ('RPC','CBS','CP'): (d/f'EpiDISH_{method}_results.csv').write_text('SampleID,Cell A,Cell B\\ns1,0.7,0.3\\ns2,0.2,0.8\\n')\n"
            " elif tool == 'EDec': (d/'EDec_results.csv').write_text('SampleID,Cell A,Cell B\\ns1,0.7,0.3\\ns2,0.2,0.8\\n')\n"
            " else: (d/'EpiSCORE_results.csv').write_text('SampleID,Cell A,Cell B\\ns1,0.7,0.3\\ns2,0.2,0.8\\n')\n"
            "elif mode == 'bad_axis':\n"
            " d=outdir/'results'; d.mkdir(parents=True, exist_ok=True)\n"
            " for method in ('RPC','CBS','CP'): (d/f'EpiDISH_{method}_results.csv').write_text('SampleID,Cell A,Unknown Cell\\ns1,0.7,0.3\\n')\n"
            "elif mode == 'failure': sys.exit(23)\n",
            encoding="utf-8",
        )
        return path

    def test_effective_request_adapter_runner_canonical_report_and_registry_success(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-stage7-adapter-") as temporary:
            root = Path(temporary)
            input_path = root / "input.csv"; input_path.write_text("SampleID,marker\\ns1,0.1\\ns2,0.2\\n", encoding="utf-8")
            bundle = self._bundle(root); executor = self._executor(root)
            for tool, expected_flags, expected_files in (
                ("EMeth", {"--ref_emeth", "--src_emeth"}, {"EMeth_Normal_results.csv", "EMeth_Laplace_results.csv"}),
                ("EDec", {"--ref_edec_tref", "--ref_edec_mark"}, {"EDec_results.csv"}),
                ("EpiDISH", {"--ref_epidish", "--epidish_reference_mode"}, {"EpiDISH_RPC_results.csv", "EpiDISH_CBS_results.csv", "EpiDISH_CP_results.csv"}),
                ("EpiSCORE", {"--ref_episcore_gene"}, {"EpiSCORE_results.csv"}),
            ):
                request = self._request(root, input_path, tool)
                prepared, production_command = self._prepared(bundle, request)
                self.assertTrue(expected_flags.issubset(production_command))
                self.assertEqual(prepared.execution_plan["tools"]["selected"][0]["adapter"]["status"], "READY_FOR_PRODUCTION_ADAPTER_EXECUTION")
                self.assertEqual(prepared.adapter_execution[tool]["execution_path"], "PRODUCTION_BUILTIN_ADAPTER")
                command = [sys.executable, str(executor), "success", tool, str(request.outdir)]
                with patch("demethflow_core.runner.prepare_decon", return_value=(prepared, command, _offline_env(request.outdir))):
                    manifest = run_decon(request)
                self.assertEqual(manifest["execution_status"], "SUCCEEDED")
                canonical = request.outdir / "canonical"
                self.assertEqual({item.name for item in canonical.glob("*.csv")}, expected_files)
                summary = json.loads((request.outdir / "result_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["tools"][tool]["adapter"]["output_assessment"]["status"], "CONFORMANT")
                report = build_report(request.outdir, root / f"report-{tool}")
                self.assertEqual(report["overview"]["execution_status"], "SUCCEEDED")
                self.assertEqual(report["tools"][tool]["adapter"]["execution_path"], "PRODUCTION_BUILTIN_ADAPTER")

                registry = root / f"evidence-{tool}.json"; initialize(registry, registry_id=f"synthetic-stage7-{tool}")
                record = {
                    "evidence_id": f"synthetic-stage7-{tool.lower()}", "schema_version": "methunmix-scientific-evidence-v1",
                    "evidence_type": "TECHNICAL_SMOKE",
                    "validation_object": {"reference_selector": bundle.selector, "reference_content_digest": bundle.content_digest(),
                        "tool": tool, "adapter": prepared.adapter_execution[tool]["adapter_id"], "profile": "cpu", "scenario": "fixture",
                        "platform": "450k", "genome_build": "not_applicable", "analysis_contract": "array_450k_cpg"},
                    "input_identity": manifest["input_identity"], "software_identity": manifest["software_identity"],
                    "runtime_identity": {"fixture": True}, "parameters": manifest["resolved_execution_parameters"], "seed": 71,
                    "sample_axis": ["s1", "s2"], "output_axis": ["cell_a", "cell_b"], "metrics": None, "acceptance_policy": None,
                    "limitations": ["synthetic controlled executor only", "no scientific status promotion"],
                    "source": {"run_manifest_sha256": sha256_file(request.outdir / "run_manifest.json")},
                }
                self.assertEqual(import_record(registry, record)["status"], "IMPORTED_UNREVIEWED")
                self.assertEqual(query(registry, tool=tool)["record_count"], 1)
                self.assertEqual(audit(registry)["status"], "PASS_STRUCTURAL_NO_STATUS_PROMOTION")

    def test_failure_is_recorded_after_adapter_preparation_without_false_output_or_registry_success(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-stage7-adapter-failure-") as temporary:
            root = Path(temporary)
            input_path = root / "input.csv"; input_path.write_text("SampleID,marker\\ns1,0.1\\n", encoding="utf-8")
            request = self._request(root, input_path, "EpiSCORE")
            prepared, _production_command = self._prepared(self._bundle(root), request)
            executor = self._executor(root)
            command = [sys.executable, str(executor), "failure", "EpiSCORE", str(request.outdir)]
            with patch("demethflow_core.runner.prepare_decon", return_value=(prepared, command, _offline_env(request.outdir))):
                with self.assertRaisesRegex(Exception, "Nextflow failed with exit code 23"):
                    run_decon(request)
            manifest = json.loads((request.outdir / "run_manifest.json").read_text(encoding="utf-8"))
            summary = json.loads((request.outdir / "result_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["execution_status"], "FAILED")
            self.assertEqual(manifest["task_supervisor"]["process_start_state"], "CONFIRMED_STARTED")
            self.assertEqual(summary["tools"]["EpiSCORE"]["output_contract_status"], "NOT_ASSESSED")
            self.assertFalse((request.outdir / "canonical" / "EpiSCORE_results.csv").exists())
            report = build_report(request.outdir, root / "failed-report")
            self.assertEqual(report["overview"]["execution_status"], "FAILED")
            self.assertEqual(report["tools"]["EpiSCORE"]["output_contract_status"], "NOT_ASSESSED")

    def test_exit_zero_without_results_is_output_contract_failure_after_edec_preparation(self):
        """A zero algorithm exit never bypasses the EDec output contract."""
        with tempfile.TemporaryDirectory(prefix="methunmix-stage7-adapter-no-output-") as temporary:
            root = Path(temporary)
            input_path = root / "input.csv"; input_path.write_text("SampleID,marker\\ns1,0.1\\n", encoding="utf-8")
            request = self._request(root, input_path, "EDec")
            prepared, _production_command = self._prepared(self._bundle(root), request)
            executor = self._executor(root)
            command = [sys.executable, str(executor), "no_output", "EDec", str(request.outdir)]
            with patch("demethflow_core.runner.prepare_decon", return_value=(prepared, command, _offline_env(request.outdir))):
                with self.assertRaisesRegex(Exception, "produced no results directory"):
                    run_decon(request)
            manifest = json.loads((request.outdir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["execution_status"], "OUTPUT_CONTRACT_FAILED")
            self.assertEqual(manifest["exit_code"], 0)
            self.assertFalse((request.outdir / "canonical" / "EDec_results.csv").exists())

    def test_invalid_epidish_axis_is_output_contract_failure_after_successful_executor(self):
        """EpiDISH's three-output declaration does not accept an unknown cell axis."""
        with tempfile.TemporaryDirectory(prefix="methunmix-stage7-adapter-bad-axis-") as temporary:
            root = Path(temporary)
            input_path = root / "input.csv"; input_path.write_text("SampleID,marker\\ns1,0.1\\n", encoding="utf-8")
            request = self._request(root, input_path, "EpiDISH")
            prepared, _production_command = self._prepared(self._bundle(root), request)
            executor = self._executor(root)
            command = [sys.executable, str(executor), "bad_axis", "EpiDISH", str(request.outdir)]
            with patch("demethflow_core.runner.prepare_decon", return_value=(prepared, command, _offline_env(request.outdir))):
                with self.assertRaisesRegex(Exception, "cell labels absent"):
                    run_decon(request)
            manifest = json.loads((request.outdir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["execution_status"], "OUTPUT_CONTRACT_FAILED")
            self.assertEqual(manifest["exit_code"], 0)

    def test_gene_only_episcore_manifest_is_execution_ready_at_shared_preflight(self):
        """Mirror the real brain/450K manifest's gene-reference-only mode."""
        with tempfile.TemporaryDirectory(prefix="methunmix-episcore-gene-only-") as temporary:
            root = Path(temporary)
            bundle = self._bundle(root)
            result = DoctorResult(bundle=bundle, selected_tools=["EpiSCORE"], accelerator="cpu")
            validate_selected_adapter_execution_contracts(result)
            check = next(item for item in result.checks if item.name == "adapter-execution-contract:EpiSCORE")
            self.assertTrue(check.ok)
            self.assertEqual(check.detail, "--ref_episcore_gene")
            plan = execution_plan_for_doctor(
                result,
                mode="decon",
                scenario="fixture",
                platform_name="450k",
                genome_build=None,
                analysis_contract="array_450k_cpg",
                input_path=None,
                requested_tools="EpiSCORE",
                allow_cross_platform=False,
                check_mode="execution_preflight",
            )
            self.assertEqual(plan["execution_readiness"], "READY_TO_LAUNCH")
            self.assertEqual(
                plan["tools"]["selected"][0]["adapter"]["argument_flags"],
                ["--ref_episcore_gene"],
            )
            self.assertEqual(
                R01_BRAIN_450K_MANIFEST["episcore"]["artifacts"].get("reference"),
                None,
            )

    def test_missing_episcore_derivative_and_raw_reference_blocks_shared_preflight(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-episcore-missing-") as temporary:
            root = Path(temporary)
            bundle = self._bundle(root)
            capability = bundle.tools["EpiSCORE"]
            missing_capability = capability.__class__(
                name=capability.name,
                status=capability.status,
                artifacts={},
                runtime_modules=capability.runtime_modules,
                reference_requirement=capability.reference_requirement,
                output_contract=capability.output_contract,
            )
            bundle = replace(bundle, tools={**bundle.tools, "EpiSCORE": missing_capability})
            result = DoctorResult(bundle=bundle, selected_tools=["EpiSCORE"], accelerator="cpu")
            validate_selected_adapter_execution_contracts(result)
            check = next(item for item in result.checks if item.name == "adapter-execution-contract:EpiSCORE")
            self.assertFalse(check.ok)
            self.assertIn("gene_reference or reference", check.detail)
            plan = execution_plan_for_doctor(
                result,
                mode="decon",
                scenario="fixture",
                platform_name="450k",
                genome_build=None,
                analysis_contract="array_450k_cpg",
                input_path=None,
                requested_tools="EpiSCORE",
                allow_cross_platform=False,
                check_mode="execution_preflight",
            )
            self.assertEqual(plan["execution_readiness"], "BLOCKED")
