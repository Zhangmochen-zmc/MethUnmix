from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from demethflow_core.cli import build_parser
from demethflow_core.errors import DeMethFlowError
from demethflow_core.manifest import load_reference
from demethflow_core.offline_assets import create_bundle, create_plan
from demethflow_core.packaging import export_reference
from demethflow_core.release import _build_module_archive
from demethflow_core.runtime import Check, DoctorResult, execution_plan_for_doctor
from demethflow_core.runner import (
    _failure_classification,
    _input_identity,
    _json_identity,
    _selected_tool_output,
    _write_result_summary,
    canonicalize_outputs,
)


def _manifest() -> dict:
    return {
        "schema": "demethflow-reference-v1",
        "reference_id": "fixture.execution-plan.450k",
        "version": "1.0.0",
        "scenario_id": "fixture",
        "source_platform": "450k",
        "analysis_contract": "array_450k_cpg",
        "compatible_input_routes": ["450k"],
        "genome_build": "not_applicable",
        "status": "PARTIAL",
        "cell_types": [{"cell_type_id": "cell_a", "display_name": "Cell A"}],
        "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
        "tool_capabilities": {
            "Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": ["array-common"]},
            "MEnet": {
                "status": "RELEASED_UNVALIDATED",
                "artifacts": {},
                "runtime_modules": ["array-common"],
                "reason": "fixed simulated truth only",
            },
            "MeDeCom": {
                "status": "READY",
                "artifacts": {},
                "runtime_modules": ["array-common"],
                "reference_requirement": "none",
                "output_contract": "reference_free_components_v1",
                "execution_policy": "explicit_only",
            },
        },
        "artifact_checksums": {},
    }


class ExecutionPlanTests(unittest.TestCase):
    def test_doctor_and_run_accept_the_same_bam_resource_controls(self):
        parser = build_parser()
        common = [
            "--preprocess-threads", "7",
            "--preprocess-sort-memory", "2G",
            "--bam-contig-policy", "primary-only",
        ]
        doctor_args = parser.parse_args(["doctor", "--platform", "wgbs", *common])
        run_args = parser.parse_args(["run", "--platform", "wgbs", "--outdir", "/tmp/out", *common])
        for args in (doctor_args, run_args):
            self.assertEqual(args.preprocess_threads, 7)
            self.assertEqual(args.preprocess_sort_memory, "2G")
            self.assertEqual(args.bam_contig_policy, "primary-only")

    def test_provenance_identity_is_stable_and_distinguishes_full_content_from_quick_identity(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-provenance-identity-") as temporary:
            root = Path(temporary)
            single = root / "input.csv"
            single.write_text("sample,probe\ns1,cg00000029\n", encoding="utf-8")
            single_identity = _input_identity(single)
            self.assertEqual(single_identity["type"], "file")
            self.assertEqual(single_identity["algorithm"], "sha256")
            self.assertIsNone(single_identity["quick_identity"])
            self.assertEqual(single_identity["identity_scope"], "full file content")

            cohort = root / "cohort"
            cohort.mkdir()
            (cohort / "a.csv").write_text("a\n", encoding="utf-8")
            (cohort / "b.csv").write_text("b\n", encoding="utf-8")
            cohort_identity = _input_identity(cohort)
            self.assertEqual(cohort_identity["type"], "directory")
            self.assertEqual(cohort_identity["algorithm"], "sha256-tree-v1")
            self.assertEqual(cohort_identity["file_count"], 2)
            self.assertIsNone(cohort_identity["quick_identity"])

            plan = {"schema": "methunmix-execution-plan-v1", "steps": ["validate", "run"]}
            self.assertEqual(
                _json_identity(plan, scope="test")["sha256"],
                _json_identity({"steps": ["validate", "run"], "schema": "methunmix-execution-plan-v1"}, scope="test")["sha256"],
            )

    def test_failure_classification_is_conservative_and_never_claims_science(self):
        self.assertEqual(_failure_classification("TIMEOUT")["category"], "TIMEOUT")
        self.assertEqual(
            _failure_classification("OUTPUT_CONTRACT_FAILED", message="canonical output missing")["category"],
            "OUTPUT_CONTRACT",
        )
        self.assertEqual(
            _failure_classification("FAILED", message="BAM_BUILD_MISMATCH: selected build differs")["category"],
            "INPUT_CONTRACT",
        )
        self.assertEqual(
            _failure_classification("FAILED", message="BAM_PREPROCESS_FAILED: WGBSTOOLS_REFERENCE_MISSING")["category"],
            "ASSET",
        )
        self.assertEqual(
            _failure_classification("FAILED", message="BAM_PREPROCESS_FAILED: no space left on device")["category"],
            "INFRASTRUCTURE",
        )
        unknown = _failure_classification("FAILED", message="Nextflow returned a non-zero exit code", exit_code=1)
        self.assertEqual(unknown["category"], "EXECUTION_FAILURE_UNCLASSIFIED")
        self.assertEqual(unknown["confidence"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(unknown["scientific_status_promotion"], "PROHIBITED")

    def test_offline_plan_and_manifest_share_tool_selection_policy(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-selection-parity-") as temporary:
            root = Path(temporary)
            reference_root = root / "reference"
            reference_root.mkdir()
            (reference_root / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
            bundle = load_reference(reference_root / "manifest.json")
            archive, _ = export_reference(bundle, root / "archives")
            runtime_source = root / "runtime"
            runtime_source.mkdir()
            (runtime_source / "array_env.sif").write_bytes(b"synthetic")
            runtime_archive = root / "archives" / "array-common-1.0.0.tar.gz"
            _build_module_archive(
                runtime_archive,
                module_id="array-common",
                module_type="runtime",
                module_version="1.0.0",
                sources=[(runtime_source / "array_env.sif", "sifs/array_env.sif")],
            )
            kit = root / "kit"
            create_bundle(kit, [archive, runtime_archive])
            expected_selected, expected_skipped = bundle.selected_tools("all", accelerator="cpu")
            lock, status = create_plan(
                kit / "bundle-index.json",
                scenario="fixture",
                platform="450k",
                genome_build=None,
                analysis_contract="array_450k_cpg",
                input_type="array",
                tools="all",
                accelerator="cpu",
                mode="decon",
                output=root / "fixture.lock.json",
            )
            self.assertEqual(status, 0)
            self.assertEqual(lock["selection"]["tools"], expected_selected)
            self.assertEqual(lock["skipped_tools"], expected_skipped)
            self.assertEqual(expected_selected, ["Houseman", "MEnet"])
            self.assertEqual(expected_skipped[0]["tool"], "MeDeCom")
            self.assertEqual(expected_skipped[0]["status"], "EXPLICIT_ONLY")

    def test_execution_plan_is_observational_and_has_explicit_blockers(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-execution-plan-") as temporary:
            root = Path(temporary)
            reference_root = root / "reference"
            reference_root.mkdir()
            (reference_root / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
            bundle = load_reference(reference_root / "manifest.json")
            result = DoctorResult(
                bundle=bundle,
                selected_tools=["Houseman", "MEnet"],
                skipped_tools=[{"tool": "MeDeCom", "status": "EXPLICIT_ONLY", "reason": "explicit selection required"}],
                accelerator="cpu",
                release_disclosures={"MEnet": {"status": "RELEASED_UNVALIDATED", "warning": "simulated truth only"}},
            )
            result.checks = [
                Check("reference-resolution", True, bundle.selector),
                Check("runtime:array-common", False, "install runtime module array-common"),
                Check("release-disclosure:MEnet", True, "simulated truth only", required=False),
            ]
            plan = execution_plan_for_doctor(
                result,
                mode="decon",
                scenario="fixture",
                platform_name="450k",
                genome_build=None,
                analysis_contract="array_450k_cpg",
                input_path=root / "sample.csv",
                requested_tools="all",
                allow_cross_platform=False,
            )
            self.assertEqual(plan["schema"], "methunmix-execution-plan-v1")
            self.assertEqual(plan["execution_readiness"], "BLOCKED")
            self.assertEqual(plan["scientific_status"], "OBSERVATIONAL_ONLY_NO_PROMOTION")
            self.assertEqual(plan["tools"]["selected"][1]["status"], "RELEASED_UNVALIDATED")
            self.assertEqual(plan["tools"]["skipped"][0]["status"], "EXPLICIT_ONLY")
            self.assertEqual(plan["constraints"]["blocking_checks"][0]["name"], "runtime:array-common")
            self.assertIn("validate-array-input", plan["steps"])
            self.assertEqual(plan["resource_contract"]["workflow_tasks"]["host_core_autodetection"], "PROHIBITED")
            self.assertEqual(plan["resource_contract"]["memory_estimate"]["status"], "NOT_ESTIMATED")
            self.assertFalse(plan["resource_contract"]["bam_preprocess"]["applicable"])

    def test_bam_plan_records_exact_preprocess_resource_choices_without_claiming_peak_memory(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-execution-plan-bam-") as temporary:
            root = Path(temporary)
            reference_root = root / "reference"
            reference_root.mkdir()
            (reference_root / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
            bundle = load_reference(reference_root / "manifest.json")
            result = DoctorResult(bundle=bundle, selected_tools=["Houseman"], accelerator="cpu")
            plan = execution_plan_for_doctor(
                result,
                mode="decon",
                scenario="fixture",
                platform_name="wgbs",
                genome_build="hg38",
                analysis_contract="wgbs_native_hg38",
                input_path=root / "sample.bam",
                requested_tools="Houseman",
                allow_cross_platform=False,
                preprocess_threads=7,
                preprocess_sort_memory="2G",
                bam_contig_policy="primary-only",
            )
            resources = plan["resource_contract"]
            self.assertTrue(resources["bam_preprocess"]["applicable"])
            self.assertEqual(resources["bam_preprocess"]["threads"], 7)
            self.assertEqual(resources["bam_preprocess"]["per_thread_sort_memory"], "2G")
            self.assertEqual(resources["bam_preprocess"]["contig_policy"], "primary-only")
            self.assertEqual(resources["bam_preprocess"]["disk_preflight"], "DEFERRED_TO_RUN_BAM_PREPROCESS_PHASE")
            self.assertEqual(resources["memory_estimate"]["status"], "NOT_ESTIMATED")

    def test_result_summary_reports_per_tool_output_without_claiming_runtime_truth(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-result-summary-") as temporary:
            root = Path(temporary)
            reference_root = root / "reference"
            reference_root.mkdir()
            (reference_root / "manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
            bundle = load_reference(reference_root / "manifest.json")
            result = DoctorResult(
                bundle=bundle,
                selected_tools=["Houseman", "MEnet"],
                skipped_tools=[{"tool": "MeDeCom", "status": "EXPLICIT_ONLY", "reason": "explicit selection required"}],
                accelerator="cpu",
                release_disclosures={"MEnet": {"status": "RELEASED_UNVALIDATED", "warning": "simulated truth only"}},
            )
            canonical_dir = root / "canonical"
            canonical_dir.mkdir()
            houseman = canonical_dir / "Houseman_results.csv"
            houseman.write_text("SampleID,cell_a\nsample_1,1\n", encoding="utf-8")
            manifest = {
                "core_version": "2.0.0rc3",
                "reference_id": bundle.reference_id,
                "reference_version": bundle.version,
                "reference_digest": bundle.content_digest(),
                "input": str(root / "input.csv"),
                "input_digest": "path:fixture",
                "analysis_contract": bundle.contract,
                "accelerator": "cpu",
                "random_seed": 1,
                "execution_status": "SUCCEEDED",
                "execution_plan": {"schema": "methunmix-execution-plan-v1"},
                "execution_plan_identity": {"algorithm": "sha256", "sha256": "a" * 64},
                "input_identity": {"type": "file", "algorithm": "sha256", "sha256": "b" * 64},
                "output_identities": {"canonical": [{"path": str(houseman), "sha256": "c" * 64}]},
            }
            summary = _write_result_summary(root, manifest, result, canonical=[houseman])
            self.assertEqual(summary["schema"], "methunmix-result-summary-v1")
            self.assertEqual(summary["tools"]["Houseman"]["output_contract_status"], "CONFORMANT")
            self.assertEqual(summary["tools"]["Houseman"]["adapter"]["output_assessment"]["status"], "CONFORMANT")
            self.assertEqual(summary["tools"]["Houseman"]["adapter"]["output_assessment"]["axis_semantics"], "named_cell_axis")
            self.assertEqual(summary["tools"]["MEnet"]["output_contract_status"], "MISSING")
            self.assertEqual(summary["tools"]["MeDeCom"]["execution_status"], "NOT_SELECTED")
            self.assertEqual(summary["truth_metrics"]["status"], "NOT_COMPUTED_NO_RUNTIME_TRUTH_INPUT")
            self.assertEqual(summary["failure_classification"]["category"], "NONE")
            self.assertEqual(summary["provenance"]["execution_plan_identity"]["sha256"], "a" * 64)
            self.assertEqual(summary["provenance"]["input_identity"]["sha256"], "b" * 64)
            self.assertEqual(summary["provenance"]["output_identities"]["canonical"][0]["sha256"], "c" * 64)
            self.assertTrue((root / "result_summary.json").is_file())

    def test_result_summary_requires_both_emeth_variants_and_preserves_other_tools(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-emeth-summary-") as temporary:
            root = Path(temporary)
            reference_root = root / "reference"
            reference_root.mkdir()
            manifest_payload = _manifest()
            manifest_payload["tool_capabilities"]["EMeth"] = {
                "status": "READY", "artifacts": {}, "runtime_modules": ["array-common"],
            }
            (reference_root / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
            bundle = load_reference(reference_root / "manifest.json")
            result = DoctorResult(bundle=bundle, selected_tools=["EMeth", "Houseman"], accelerator="cpu")
            canonical_dir = root / "canonical"
            canonical_dir.mkdir()
            normal = canonical_dir / "EMeth_Normal_results.csv"
            laplace = canonical_dir / "EMeth_Laplace_results.csv"
            houseman = canonical_dir / "Houseman_results.csv"
            for path in (normal, laplace, houseman):
                path.write_text("SampleID,cell_a\nsample_1,1\n", encoding="utf-8")
            manifest = {
                "reference_id": bundle.reference_id,
                "reference_version": bundle.version,
                "execution_status": "SUCCEEDED",
            }

            complete = _write_result_summary(root, manifest, result, canonical=[normal, laplace, houseman])
            emeth = complete["tools"]["EMeth"]
            self.assertEqual(emeth["execution_status"], "EXECUTION_SUCCESS")
            self.assertEqual(emeth["output_contract_status"], "CONFORMANT")
            self.assertEqual(emeth["variants"]["Normal"]["output_contract_status"], "CONFORMANT")
            self.assertEqual(emeth["variants"]["Laplace"]["output_contract_status"], "CONFORMANT")
            self.assertEqual(complete["tools"]["Houseman"]["output_contract_status"], "CONFORMANT")

            missing = _write_result_summary(root, manifest, result, canonical=[normal, houseman])
            emeth_missing = missing["tools"]["EMeth"]
            self.assertEqual(emeth_missing["execution_status"], "SUCCEEDED_WITHOUT_DECLARED_OUTPUT")
            self.assertEqual(emeth_missing["output_contract_status"], "MISSING")
            self.assertEqual(emeth_missing["missing_variants"], ["Laplace"])
            self.assertEqual(emeth_missing["variants"]["Laplace"]["output_contract_status"], "MISSING")

            failed_manifest = {
                **manifest,
                "execution_status": "OUTPUT_CONTRACT_FAILED",
                "failure_classification": {
                    "message": "Empty or malformed result: /tmp/results/EMeth_Laplace_results.csv",
                },
            }
            malformed = _write_result_summary(root, failed_manifest, result, canonical=[])
            emeth_malformed = malformed["tools"]["EMeth"]
            self.assertEqual(emeth_malformed["output_contract_status"], "INVALID")
            self.assertEqual(emeth_malformed["variants"]["Laplace"]["output_contract_status"], "INVALID")
            self.assertIn("EMeth_Laplace_results.csv", emeth_malformed["variants"]["Laplace"]["reason"])

    def test_emeth_malformed_variant_fails_canonicalization_before_summary(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-emeth-malformed-") as temporary:
            root = Path(temporary)
            reference_root = root / "reference"
            reference_root.mkdir()
            manifest_payload = _manifest()
            manifest_payload["tool_capabilities"]["EMeth"] = {
                "status": "READY", "artifacts": {}, "runtime_modules": ["array-common"],
            }
            (reference_root / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
            bundle = load_reference(reference_root / "manifest.json")
            results = root / "results"
            results.mkdir()
            (results / "EMeth_Normal_results.csv").write_text("Sample,Cell A\nsample_1,1\n", encoding="utf-8")
            malformed = results / "EMeth_Laplace_results.csv"
            malformed.write_text("not,a,valid,proportion,result\n", encoding="utf-8")
            with self.assertRaisesRegex(DeMethFlowError, "EMeth_Laplace_results.csv"):
                canonicalize_outputs(root, bundle)

    def test_emeth_missing_normal_and_duplicate_variant_are_reported_separately(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-emeth-variant-boundary-") as temporary:
            root = Path(temporary)
            canonical = root / "canonical"; canonical.mkdir()
            laplace = canonical / "EMeth_Laplace_results.csv"
            laplace.write_text("SampleID,cell_a\nsample_1,1\n", encoding="utf-8")
            missing_normal = _selected_tool_output(
                outdir=root, tool="EMeth", output_contract="cell_proportions_v1",
                canonical=[laplace], reference_free=[], run_status="SUCCEEDED",
            )
            self.assertEqual(missing_normal["missing_variants"], ["Normal"])
            self.assertEqual(missing_normal["variants"]["Normal"]["output_contract_status"], "MISSING")
            duplicate_normal = _selected_tool_output(
                outdir=root, tool="EMeth", output_contract="cell_proportions_v1",
                canonical=[laplace, canonical / "EMeth_Normal_results.csv", canonical / "EMeth_Normal_results.csv"],
                reference_free=[], run_status="SUCCEEDED",
            )
            self.assertEqual(duplicate_normal["invalid_variants"], ["Normal"])
            self.assertEqual(duplicate_normal["variants"]["Normal"]["output_contract_status"], "INVALID")
            failed_with_partial = _selected_tool_output(
                outdir=root, tool="EMeth", output_contract="cell_proportions_v1",
                canonical=[laplace], reference_free=[], run_status="FAILED", failure_message="synthetic executor failure",
            )
            self.assertEqual(failed_with_partial["outputs"], [])
            self.assertEqual(failed_with_partial["variants"]["Laplace"]["execution_status"], "FAILED")

    def test_emeth_cross_platform_nonfinite_and_proportion_constraints_fail_canonicalization(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-emeth-cross-platform-output-") as temporary:
            root = Path(temporary)
            reference_root = root / "reference"; reference_root.mkdir()
            payload = _manifest()
            payload["tool_capabilities"] = {
                "EMeth": {"status": "READY", "artifacts": {}, "runtime_modules": ["array-common"]},
            }
            (reference_root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
            bundle = load_reference(reference_root / "manifest.json")
            # The canonicalizer's existing numeric guard is route dependent.
            # Keep the fixture manifest simple, then exercise that already
            # declared cross-platform output rule without constructing a full
            # projection asset graph.
            bundle.payload["analysis_contract"] = "array_epic_from_450k_common_cpg_v1"
            results = root / "results"; results.mkdir()
            (results / "EMeth_Laplace_results.csv").write_text("Sample,Cell A\ns1,1\n", encoding="utf-8")
            (results / "EMeth_Normal_results.csv").write_text("Sample,Cell A\ns1,nan\n", encoding="utf-8")
            with self.assertRaisesRegex(DeMethFlowError, "invalid canonical proportion"):
                canonicalize_outputs(root, bundle)
