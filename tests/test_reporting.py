from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from demethflow_core.benchmark import benchmark
from demethflow_core.reporting import REPORT_SCHEMA, build_report, prepare_replay
from demethflow_core.errors import DeMethFlowError


def _write_matrix(path: Path, rows: list[tuple[str, float, float]]) -> None:
    path.write_text(
        "SampleID,cell_a,cell_b\n" + "".join(f"{sample},{a},{b}\n" for sample, a, b in rows),
        encoding="utf-8",
    )


def _manifest(run_dir: Path, *, status: str = "SUCCEEDED") -> dict:
    return {
        "schema": "demethflow-run-v2", "core_version": "2.0.0rc3", "execution_status": status,
        "exit_code": 0 if status == "SUCCEEDED" else 1,
        "scenario_id": "fixture", "reference_id": "fixture.ref", "reference_version": "1.0.0",
        "reference_digest": "a" * 64, "analysis_contract": "array_450k_cpg", "input_platform": "450k",
        "input": str(run_dir / "input.csv"),
        "input_identity": {"type": "file", "algorithm": "sha256", "sha256": "b" * 64},
        "selected_tools": ["EMeth", "Houseman"], "accelerator": "cpu", "random_seed": 7,
        "execution_plan": {
            "schema": "methunmix-execution-plan-v1", "output_directory": str(run_dir),
            "effective_request": {"effective_values": {
                "scenario": "fixture", "input_platform": "450k", "analysis_contract": "array_450k_cpg",
                "reference": "fixture.ref@1.0.0", "tools_requested": "EMeth,Houseman", "accelerator": "cpu",
                "parameters": {"random_seed": 7},
            }},
        },
        "execution_plan_identity": {"sha256": "c" * 64},
        "execution_plan_semantic_identity": {"sha256": "d" * 64},
        "scientific_validation_disclosure": {"warning": "fixture scope only", "scientific_status": "NOT_PROMOTED"},
    }


class ReportingTests(unittest.TestCase):
    def test_emeth_variants_and_descriptive_truth_metrics(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-report-") as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            canonical = run / "canonical"
            canonical.mkdir()
            normal, laplace, houseman, truth = [canonical / name for name in (
                "EMeth_Normal_results.csv", "EMeth_Laplace_results.csv", "Houseman_results.csv", "truth.csv",
            )]
            rows = [("s1", 0.2, 0.8), ("s2", 0.7, 0.3)]
            for path in (normal, laplace, houseman, truth):
                _write_matrix(path, rows)
            (run / "run_manifest.json").write_text(json.dumps(_manifest(run)), encoding="utf-8")
            summary = {
                "schema": "methunmix-result-summary-v1", "execution_status": "SUCCEEDED",
                "tools": {
                    "EMeth": {"selected": True, "output_contract": "cell_proportions_v1", "execution_status": "EXECUTION_SUCCESS", "output_contract_status": "CONFORMANT", "declared_status": "READY", "variants": {
                        "Normal": {"outputs": [{"path": str(normal)}], "output_contract_status": "CONFORMANT"},
                        "Laplace": {"outputs": [{"path": str(laplace)}], "output_contract_status": "CONFORMANT"},
                    }},
                    "Houseman": {"selected": True, "output_contract": "cell_proportions_v1", "execution_status": "EXECUTION_SUCCESS", "output_contract_status": "CONFORMANT", "declared_status": "READY", "outputs": [{"path": str(houseman)}]},
                },
            }
            (run / "result_summary.json").write_text(json.dumps(summary), encoding="utf-8")
            derived = Path(temporary) / "derived"
            report = build_report(run, derived, truth=truth, tool="EMeth", variant="Normal")
            self.assertEqual(report["schema"], REPORT_SCHEMA)
            self.assertEqual(report["tools"]["EMeth"]["variants"]["Laplace"]["output_contract_status"], "CONFORMANT")
            self.assertEqual(report["truth_metrics"]["status"], "METRICS_COMPUTED_REPORT_ONLY")
            self.assertEqual(report["truth_metrics"]["metrics"]["overall_mae"], 0.0)
            self.assertEqual(report["truth_metrics"]["metrics"]["overall_pearson"], 1.0)
            # WP-05 reports must expose the exact output of the shared WP-08
            # benchmark mechanism, not a forked metric implementation.
            self.assertEqual(report["truth_metrics"]["metrics"], benchmark(normal, truth)["metrics"])
            self.assertTrue((derived / "report.json").is_file())
            self.assertTrue((derived / "report.html").is_file())

    def test_partial_failure_and_html_escape_remain_reportable(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-report-partial-") as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            manifest = _manifest(run, status="FAILED")
            manifest["failure_classification"] = {"message": "workflow <script>alert(1)</script> failed at /data/private/run; api_token=should-not-appear"}
            (run / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            summary = {"execution_status": "FAILED", "tools": {
                "Houseman": {"selected": True, "execution_status": "EXECUTION_SUCCESS", "output_contract_status": "CONFORMANT", "outputs": []},
                "MEnet": {"selected": True, "execution_status": "TIMEOUT", "output_contract_status": "NOT_ASSESSED", "reason": "<script>alert(1)</script>"},
                "EDec": {"selected": False, "selection_status": "INPUT_CONTRACT_BLOCKED", "execution_status": "NOT_SELECTED", "reason": "N>K required"},
            }}
            (run / "result_summary.json").write_text(json.dumps(summary), encoding="utf-8")
            derived = Path(temporary) / "derived"
            report = build_report(run, derived)
            self.assertEqual(report["overview"]["execution_status"], "FAILED")
            self.assertEqual(report["tools"]["MEnet"]["execution_status"], "TIMEOUT")
            page = (derived / "report.html").read_text(encoding="utf-8")
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertNotIn("/data/private/run", page)
            self.assertNotIn("should-not-appear", page)
            self.assertIn("api_token=REDACTED", page)
            self.assertNotIn("http://", page)
            self.assertNotIn("https://", page)

    def test_unknown_or_anonymous_output_never_gets_named_truth_metrics(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-report-anon-") as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            truth = run / "truth.csv"
            _write_matrix(truth, [("s1", 0.5, 0.5), ("s2", 0.5, 0.5)])
            (run / "run_manifest.json").write_text(json.dumps(_manifest(run)), encoding="utf-8")
            (run / "result_summary.json").write_text(json.dumps({"execution_status": "SUCCEEDED", "tools": {
                "RefFreeEWAS": {"selected": True, "output_contract": "reference_free_components_v1", "execution_status": "EXECUTION_SUCCESS", "outputs": []},
            }}), encoding="utf-8")
            report = build_report(run, Path(temporary) / "derived", truth=truth, tool="RefFreeEWAS")
            self.assertEqual(report["truth_metrics"]["status"], "NOT_COMPUTED_OUTPUT_CONTRACT_UNSUPPORTED")

    def test_emeth_missing_required_variant_is_explicit_without_reclassifying_execution(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-report-emeth-missing-") as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            normal = run / "EMeth_Normal_results.csv"
            _write_matrix(normal, [("s1", 0.2, 0.8)])
            (run / "run_manifest.json").write_text(json.dumps(_manifest(run)), encoding="utf-8")
            (run / "result_summary.json").write_text(json.dumps({"execution_status": "SUCCEEDED", "tools": {
                "EMeth": {
                    "selected": True,
                    "execution_status": "EXECUTION_SUCCESS",
                    "output_contract": "cell_proportions_v1",
                    "output_contract_status": "MISSING",
                    "variants": {"Normal": {"outputs": [{"path": str(normal)}], "output_contract_status": "CONFORMANT"}},
                },
            }}), encoding="utf-8")
            report = build_report(run, Path(temporary) / "derived")
            emeth = report["tools"]["EMeth"]
            self.assertEqual(emeth["execution_status"], "EXECUTION_SUCCESS")
            self.assertEqual(emeth["variants"]["Normal"]["output_contract_status"], "CONFORMANT")
            self.assertEqual(emeth["variants"]["Laplace"]["output_contract_status"], "MISSING")
            self.assertIn("absent", emeth["variants"]["Laplace"]["reason"])

    def test_replay_plan_is_non_executing_and_detects_identity_drift(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-replay-") as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            manifest = _manifest(run)
            manifest["command"] = ["/bin/sh", "-c", "touch should-never-run"]
            manifest_path = run / "run_manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            changed = run / "new-input.csv"
            _write_matrix(changed, [("s1", 0.1, 0.9)])
            plan = prepare_replay(manifest_path, run / "replay-output", input_path=changed, verify_input_identity=True)
            self.assertEqual(plan["status"], "BLOCKED")
            self.assertEqual(plan["input_check"]["status"], "MISMATCH_OR_IDENTITY_INSUFFICIENT")
            self.assertNotIn("touch should-never-run", plan["suggested_command_display"])
            self.assertNotIn("not_applicable", plan["suggested_command_argv"])
            self.assertFalse((run / "should-never-run").exists())

    def test_missing_summary_and_constant_truth_are_explicit(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-report-legacy-") as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            (run / "run_manifest.json").write_text(json.dumps(_manifest(run)), encoding="utf-8")
            (run / "tool_status.tsv").write_text(
                "tool\tstatus\treason\nEMeth\tRU\thistorical disclosure\n",
                encoding="utf-8",
            )
            report = build_report(run, Path(temporary) / "derived")
            self.assertEqual(report["manifest_summary_consistency"]["status"], "NOT_CHECKED_RESULT_SUMMARY_ABSENT")
            self.assertEqual(report["tools"]["EMeth"]["execution_status"], "UNKNOWN_RECORD_INCOMPLETE")
            self.assertEqual(report["tools"]["EMeth"]["declared_scientific_status"], "RU")

    def test_fresh_retry_report_keeps_historical_reference_separate_from_new_outputs(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-report-fresh-retry-") as temporary:
            run = Path(temporary) / "new-run"
            run.mkdir()
            (run / "run_manifest.json").write_text(json.dumps(_manifest(run)), encoding="utf-8")
            (run / "result_summary.json").write_text(json.dumps({"execution_status": "SUCCEEDED", "tools": {}}), encoding="utf-8")
            (run / "fresh-retry-attempt.json").write_text(json.dumps({
                "schema": "methunmix-fresh-failed-tool-retry-v1",
                "source_run_dir": "/historical/failed-run",
                "prior_result_reference": {"source_run_dir": "/historical/failed-run", "mode": "READ_ONLY"},
            }), encoding="utf-8")
            report = build_report(run, Path(temporary) / "derived")
            lineage = report["fresh_retry_lineage"]
            self.assertEqual(lineage["status"], "FRESH_RETRY_LINEAGE_RECORDED")
            self.assertEqual(lineage["historical_run_reference"]["mode"], "READ_ONLY")
            self.assertIn("this report only", lineage["new_results_scope"])

    def test_report_refuses_to_write_inside_the_source_run(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-report-boundary-") as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            (run / "run_manifest.json").write_text(json.dumps(_manifest(run)), encoding="utf-8")
            with self.assertRaisesRegex(DeMethFlowError, "REPORT_OUTPUT_DIRECTORY_CONFLICT"):
                build_report(run, run / "derived")
