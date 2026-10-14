from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from demethflow_core import cli
from demethflow_core.errors import DeMethFlowError
from demethflow_core.reporting import prepare_replay
from demethflow_core.safe_replay import _source_configuration, execute_safe_replay, validate_safe_replay
from demethflow_core.software_identity import EXECUTION_CONTENT_SCHEMA
from demethflow_core.util import sha256_file


class _Bundle:
    def __init__(self, digest: str): self.digest = digest
    def content_digest(self): return self.digest


class _Preflight:
    def __init__(self, digest: str, runtime_files: dict[str, Path]):
        self.bundle = _Bundle(digest)
        self.selected_tools = ["Houseman"]
        self.runtime_files = runtime_files
        self.external_runtime_metadata = {}
        self.external_runtime_paths = {}


class SafeReplayTests(unittest.TestCase):
    @staticmethod
    def _software() -> dict[str, object]:
        return {
            "core_version": "fixture",
            "source_commit": None,
            "execution_content_identity": {"schema": EXECUTION_CONTENT_SCHEMA, "sha256": "a" * 64},
        }

    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path, dict[str, Path]]:
        source = root / "source"; (source / ".demethflow").mkdir(parents=True)
        input_path = root / "input.csv"; input_path.write_text("SampleID,A\ns1,0.1\n", encoding="utf-8")
        runtimes = {}
        for name in ("Houseman", "array-common"):
            path = root / f"{name}.sif"; path.write_text(name, encoding="utf-8"); runtimes[name] = path
        manifest = {
            "status": "SUCCEEDED", "execution_status": "SUCCEEDED", "scenario_id": "brain",
            "input_platform": "450k", "analysis_contract": "array_450k_cpg", "reference_id": "fixture.ref",
            "reference_version": "1.0.0", "reference_digest": "reference", "selected_tools": ["Houseman"],
            "accelerator": "cpu", "random_seed": 7, "input_identity": {"type": "file", "algorithm": "sha256", "sha256": sha256_file(input_path), "bytes": input_path.stat().st_size, "quick_identity": None, "identity_scope": "full file content"},
            "software_identity": self._software(),
            "runtime_images": {name: {"path": str(path), "sha256": sha256_file(path)} for name, path in runtimes.items()},
            "external_runtime": {"paths": {}, "metadata": {}, "contract": "methunmix-external-runtime-v1"},
            "run_lease": {"state": "IDLE_CONFIRMED"},
            # Controlled copy of the r01 source manifest's relevant fields.
            # Original source manifest SHA256:
            # 7a97682abe35e44acc6509b1b2649b2c90761636362a60e5edbc4e7cce2866f2.
            "genome_build": "not_applicable",
            "execution_plan": {"output_directory": str(source), "effective_request": {"schema": "methunmix-resolved-analysis-request-v1", "effective_values": {
                "scenario": "brain", "input_platform": "450k", "analysis_contract": "array_450k_cpg",
                "reference": "fixture.ref@1.0.0", "tools_requested": "Houseman", "accelerator": "cpu",
                "runtime": "container", "container_engine": "/fixture/apptainer", "executor": "local",
                "allow_cross_platform": False, "genome_build": None,
                "parameters": {"random_seed": 7, "medecom_k": None, "medecom_lambda": "auto", "medecom_nfolds": None, "methylcibersort_permutations": 1000, "preprocess_threads": 1, "preprocess_sort_memory": "768M", "bam_contig_policy": "strict"},
            }}},
        }
        manifest_path = source / "run_manifest.json"; manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (source / ".demethflow" / "task_receipt.json").write_text(json.dumps({"process_start_state": "CONFIRMED_STARTED", "orphan_free": True, "remaining_group_members": []}), encoding="utf-8")
        target = root / "replay"
        plan_path = root / "replay-plan.json"
        plan_path.write_text(json.dumps(prepare_replay(manifest_path, target, input_path=input_path, verify_input_identity=True)), encoding="utf-8")
        return source, manifest_path, plan_path, input_path, runtimes

    def _kwargs(self, root: Path, manifest: Path, plan: Path, input_path: Path, target: Path) -> dict[str, object]:
        value = json.loads(plan.read_text(encoding="utf-8"))
        value["new_output_directory"] = str(target.resolve(strict=False))
        plan.write_text(json.dumps(value), encoding="utf-8")
        return {"replay_plan_path": plan, "source_manifest_path": manifest, "input_path": input_path, "outdir": target, "reference_root": root / "references", "runtime_root": root / "runtimes", "external_runtime_root": None, "timeout_seconds": 600}

    @staticmethod
    def _rebind_plan(plan: Path, manifest: Path) -> None:
        value = json.loads(plan.read_text(encoding="utf-8"))
        value["source_manifest"]["sha256"] = sha256_file(manifest)
        plan.write_text(json.dumps(value), encoding="utf-8")

    def test_preview_is_nonexecuting_and_ignores_malicious_command_strings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, manifest, plan, input_path, _ = self._fixture(root)
            raw = json.loads(plan.read_text()); raw["suggested_command_argv"] = ["/bin/sh", "-c", f"touch {root / 'pwned'}"]; plan.write_text(json.dumps(raw))
            result = validate_safe_replay(**self._kwargs(root, manifest, plan, input_path, root / "target"), current_software=self._software)
            self.assertEqual(result["decision"], "SAFE_REPLAY_REQUIRES_EXPLICIT_EXECUTE")
            self.assertFalse((root / "target").exists()); self.assertFalse((root / "pwned").exists())
            self.assertEqual((source / "run_manifest.json").read_text(), manifest.read_text())

    def test_identity_legacy_input_and_target_risks_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, manifest, plan, input_path, _ = self._fixture(root); kwargs = self._kwargs(root, manifest, plan, input_path, root / "target")
            with self.assertRaisesRegex(DeMethFlowError, "execution content differs"):
                validate_safe_replay(**kwargs, current_software=lambda: {**self._software(), "execution_content_identity": {"schema": EXECUTION_CONTENT_SCHEMA, "sha256": "b" * 64}})
            raw = json.loads(manifest.read_text()); raw["software_identity"].pop("execution_content_identity"); manifest.write_text(json.dumps(raw))
            self._rebind_plan(plan, manifest)
            with self.assertRaisesRegex(DeMethFlowError, "no execution-content identity"):
                validate_safe_replay(**kwargs, current_software=self._software)
            raw["software_identity"] = self._software(); manifest.write_text(json.dumps(raw)); self._rebind_plan(plan, manifest)
            input_path.write_text("SampleID,A\ns1,0.2\n", encoding="utf-8")
            with self.assertRaisesRegex(DeMethFlowError, "input content identity"):
                validate_safe_replay(**kwargs, current_software=self._software)
            input_path.write_text("SampleID,A\ns1,0.1\n", encoding="utf-8")
            with self.assertRaisesRegex(DeMethFlowError, "overlap"):
                validate_safe_replay(**self._kwargs(root, manifest, plan, input_path, source / "nested"), current_software=self._software)
            existing = root / "existing"; existing.mkdir()
            with self.assertRaisesRegex(DeMethFlowError, "already exists"):
                validate_safe_replay(**self._kwargs(root, manifest, plan, input_path, existing), current_software=self._software)

    def test_execute_calls_public_runner_after_revalidating_assets_and_records_terminal_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); _, manifest, plan, input_path, runtimes = self._fixture(root)
            for label, status in (("success", "SUCCEEDED"), ("failed", "FAILED"), ("timeout", "TIMEOUT")):
                target = root / label; calls = []
                def fake_runner(request, status=status):
                    calls.append(request)
                    state = request.outdir / ".demethflow"; state.mkdir(parents=True, exist_ok=True)
                    (state / "task_receipt.json").write_text(json.dumps({"process_start_state": "CONFIRMED_STARTED"}), encoding="utf-8")
                    (request.outdir / "run_manifest.json").write_text(json.dumps({"status": status, "execution_status": status}), encoding="utf-8")
                    return {"status": status, "execution_status": status}
                result = execute_safe_replay(**self._kwargs(root, manifest, plan, input_path, target), current_software=self._software, preflight=lambda _: (_Preflight("reference", runtimes), [], {}), runner=fake_runner)
                self.assertEqual(result["status"], status); self.assertEqual(len(calls), 1); self.assertFalse(calls[0].resume); self.assertIsNone(calls[0].step_cache_root)
                self.assertIsNone(calls[0].genome_build); self.assertIsNone(calls[0].requested_genome_build)
                attempt = json.loads((target / "safe-replay-attempt.json").read_text())
                self.assertEqual(attempt["state"], "RUNNER_RETURNED"); self.assertEqual(attempt["workflow_status"], status); self.assertTrue(attempt["workflow_started"])

    def test_reference_runtime_and_report_failure_are_recorded_without_rewriting_runner_fact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); _, manifest, plan, input_path, runtimes = self._fixture(root); kwargs = self._kwargs(root, manifest, plan, input_path, root / "drift")
            with self.assertRaisesRegex(DeMethFlowError, "reference content digest"):
                execute_safe_replay(**kwargs, current_software=self._software, preflight=lambda _: (_Preflight("other", runtimes), [], {}))
            target = root / "report-failure"; kwargs = self._kwargs(root, manifest, plan, input_path, target)
            def fake_runner(request):
                state = request.outdir / ".demethflow"; state.mkdir(parents=True, exist_ok=True)
                (state / "task_receipt.json").write_text(json.dumps({"process_start_state": "CONFIRMED_STARTED"}), encoding="utf-8")
                return {"status": "SUCCEEDED", "execution_status": "SUCCEEDED"}
            with self.assertRaisesRegex(RuntimeError, "report"):
                execute_safe_replay(**kwargs, current_software=self._software, preflight=lambda _: (_Preflight("reference", runtimes), [], {}), runner=fake_runner, report_output_dir=root / "derived", reporter=lambda *_: (_ for _ in ()).throw(RuntimeError("report failed")))
            attempt = json.loads((target / "safe-replay-attempt.json").read_text())
            self.assertEqual(attempt["state"], "RUNNER_RETURNED"); self.assertEqual(attempt["report_status"], "FAILED")

    def test_r01_array_null_is_authoritative_and_legacy_display_is_not_an_executable_parameter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); _, manifest, plan, input_path, _ = self._fixture(root)
            source = _source_configuration(json.loads(manifest.read_text(encoding="utf-8")))
            self.assertIsNone(source["genome_build"])
            self.assertIsNone(source["requested_genome_build"])
            self.assertIsNone(json.loads(plan.read_text(encoding="utf-8"))["controlled_configuration"]["genome_build"])
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            raw["execution_plan"]["effective_request"]["effective_values"].pop("genome_build")
            manifest.write_text(json.dumps(raw), encoding="utf-8")
            self._rebind_plan(plan, manifest)
            # A legacy array record can normalize only its documented display marker.
            self.assertIsNone(_source_configuration(raw)["genome_build"])
            raw["genome_build"] = "hg38"
            with self.assertRaisesRegex(DeMethFlowError, "legacy array source genome build is ambiguous"):
                _source_configuration(raw)

    def test_wgbs_route_build_rules_and_falsey_fields_are_preserved_or_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); _, manifest, _, _, _ = self._fixture(root)
            raw = json.loads(manifest.read_text(encoding="utf-8")); values = raw["execution_plan"]["effective_request"]["effective_values"]
            values.update({"input_platform": "wgbs", "analysis_contract": "wgbs_native_hg19", "genome_build": "hg19", "genome_build_requested": "hg19", "allow_cross_platform": False})
            values["parameters"]["random_seed"] = 0
            values["parameters"]["preprocess_threads"] = 0
            source = _source_configuration(raw)
            self.assertEqual((source["genome_build"], source["requested_genome_build"]), ("hg19", "hg19"))
            self.assertFalse(source["allow_cross_platform"])
            self.assertEqual(source["random_seed"], 0)
            self.assertEqual(source["parameters"]["preprocess_threads"], 0)
            values["analysis_contract"] = "wgbs_derived_450k_cpg_v1"; values["genome_build_requested"] = "hg38"
            with self.assertRaisesRegex(DeMethFlowError, "conflicts"):
                _source_configuration(raw)
            values["genome_build_requested"] = "hg19"
            self.assertEqual(_source_configuration(raw)["genome_build"], "hg19")
            values["genome_build"] = "not_applicable"
            with self.assertRaisesRegex(DeMethFlowError, "must be hg19 or hg38"):
                _source_configuration(raw)

    def test_public_cli_execute_passes_only_structured_replay_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); _, manifest, plan, input_path, _ = self._fixture(root); target = root / "public-cli-target"
            output = root / "result.json"
            lock, locators = root / "lock.json", root / "locators.json"
            lock.write_text("{}", encoding="utf-8"); locators.write_text("{}", encoding="utf-8")
            with patch("demethflow_core.cli.execute_safe_replay", return_value={"state": "synthetic"}) as execute:
                code = cli.main([
                    "replay-execute", "--replay-plan", str(plan), "--source-manifest", str(manifest), "--input", str(input_path),
                    "--outdir", str(target), "--reference-root", str(root / "refs"), "--runtime-root", str(root / "runtimes"),
                    "--workflow-timeout-seconds", "600", "--local-dependency-lock", str(lock),
                    "--local-dependency-locators", str(locators), "--execute", "--output", str(output),
                ])
            self.assertEqual(code, 0); self.assertTrue(output.is_file()); self.assertTrue(execute.called)
            kwargs = execute.call_args.kwargs
            self.assertEqual(kwargs["timeout_seconds"], 600); self.assertNotIn("suggested_command_argv", kwargs)
            self.assertEqual(kwargs["local_dependency_lock"], lock)
            self.assertEqual(kwargs["local_dependency_locators"], locators)

    def test_bound_source_requires_current_lock_and_revalidates_before_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); _, manifest, plan, input_path, runtimes = self._fixture(root)
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            raw["installation_dependency_plan"] = {
                "state": "RESOLVED", "plan_identity": {"sha256": "a" * 64},
                "local_dependencies": [{"name": "Houseman", "kind": "logical_runtime_module", "identity": {"sha256": sha256_file(runtimes["Houseman"]), "bytes": runtimes["Houseman"].stat().st_size}}],
            }
            manifest.write_text(json.dumps(raw), encoding="utf-8"); self._rebind_plan(plan, manifest)
            kwargs = self._kwargs(root, manifest, plan, input_path, root / "bound-target")
            with self.assertRaisesRegex(DeMethFlowError, "provide both current binding lock"):
                validate_safe_replay(**kwargs, current_software=self._software)
            lock, locators = root / "lock.json", root / "locators.json"
            lock.write_text("{}", encoding="utf-8"); locators.write_text("{}", encoding="utf-8")
            current = {"installable": True, "plan_identity": {"sha256": "a" * 64}, "local_dependencies": [{"name": "Houseman", "kind": "logical_runtime_module"}]}
            with patch("demethflow_core.safe_replay.create_catalog_plan", return_value=(current, 0)) as resolver:
                preview = validate_safe_replay(**kwargs, local_dependency_lock=lock, local_dependency_locators=locators, current_software=self._software)
            self.assertEqual(preview["local_dependency_revalidation"]["state"], "REVALIDATED_MATCH")
            self.assertEqual(resolver.call_args.kwargs["local_dependency_lock"], lock)
            calls = []
            def fake_runner(request):
                calls.append(request)
                state = request.outdir / ".demethflow"; state.mkdir(parents=True)
                (state / "task_receipt.json").write_text(json.dumps({"process_start_state": "CONFIRMED_STARTED"}), encoding="utf-8")
                return {"status": "SUCCEEDED", "execution_status": "SUCCEEDED"}
            target = root / "bound-execute"; execute_kwargs = self._kwargs(root, manifest, plan, input_path, target)
            with patch("demethflow_core.safe_replay.create_catalog_plan", return_value=(current, 0)):
                execute_safe_replay(**execute_kwargs, local_dependency_lock=lock, local_dependency_locators=locators,
                                    current_software=self._software, preflight=lambda _: (_Preflight("reference", runtimes), [], {}), runner=fake_runner)
            self.assertEqual(calls[0].installation_dependency_plan, current)
            drifted = {**current, "plan_identity": {"sha256": "b" * 64}}
            with patch("demethflow_core.safe_replay.create_catalog_plan", return_value=(drifted, 0)):
                with self.assertRaisesRegex(DeMethFlowError, "plan identity differs"):
                    validate_safe_replay(**self._kwargs(root, manifest, plan, input_path, root / "bound-drift"), local_dependency_lock=lock, local_dependency_locators=locators, current_software=self._software)


if __name__ == "__main__":
    unittest.main()
