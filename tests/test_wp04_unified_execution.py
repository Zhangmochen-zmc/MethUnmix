from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from demethflow_core.cli import _resolve_analysis_request, build_parser, main
from demethflow_core.errors import DeMethFlowError
from demethflow_core.manifest import load_reference
from demethflow_core.offline_assets import remove_module
from demethflow_core.runner import DeconRequest, prepare_decon
from demethflow_core.runtime import Check, DoctorResult, execution_plan_for_doctor
from demethflow_core.util import data_home_override


def _bundle(root: Path):
    manifest = {
        "schema": "demethflow-reference-v1", "reference_id": "fixture.wp04.450k", "version": "1.0.0",
        "scenario_id": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
        "compatible_input_routes": ["450k"], "genome_build": "not_applicable", "status": "READY",
        "cell_types": [{"cell_type_id": "cell_a", "display_name": "Cell A"}],
        "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
        "tool_capabilities": {
            "Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": []},
            "MEnet": {"status": "RELEASED_UNVALIDATED", "artifacts": {}, "runtime_modules": []},
        },
        "artifact_checksums": {},
    }
    path = root / "reference" / "manifest.json"
    path.parent.mkdir()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return load_reference(path)


def _result(bundle, *, ok: bool = True, tools: list[str] | None = None, accelerator: str = "cpu") -> DoctorResult:
    result = DoctorResult(
        bundle=bundle,
        selected_tools=tools or ["Houseman"],
        accelerator=accelerator,
        nextflow_command=["fixture-nextflow"],
        container_engine="apptainer",
    )
    result.add("reference-resolution", ok, bundle.selector)
    result.execution_plan = execution_plan_for_doctor(
        result, mode="decon", scenario="fixture", platform_name="450k", genome_build=None,
        analysis_contract="array_450k_cpg", input_path=Path("/fixture/input.csv"), requested_tools="Houseman",
        allow_cross_platform=False,
        request_provenance={"schema": "methunmix-resolved-analysis-request-v1", "entrypoint": "test"},
        check_mode="execution_preflight",
    )
    return result


class WP04UnifiedExecutionTests(unittest.TestCase):
    def test_derived_route_keeps_wgbs_input_and_declares_array_target(self):
        args = build_parser().parse_args([
            "plan", "--scenario", "fixture", "--platform", "wgbs", "--genome-build", "hg19",
            "--analysis-contract", "wgbs_derived_epic_cpg_v1", "--reference", "fixture@1.0.0",
            "--tools", "Houseman",
        ])
        request = _resolve_analysis_request(args, entrypoint="plan", require_platform=True)
        values = request["effective_values"]
        self.assertEqual(values["input_platform"], "wgbs")
        self.assertEqual(values["target_platform"], "epic")

    def test_cli_plan_doctor_and_run_share_effective_selection_fields(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp04-cli-") as temporary:
            root = Path(temporary)
            bundle = _bundle(root)
            fixture = _result(bundle)
            input_path = root / "input.csv"
            input_path.write_text("sample,cell_a\ns1,1\n", encoding="utf-8")
            common = [
                "--scenario", "fixture", "--platform", "450k", "--reference", bundle.selector,
                "--tools", "Houseman", "--accelerator", "cpu", "--input", str(input_path),
            ]
            captured: dict[str, object] = {}
            def fake_doctor(**kwargs):
                captured.setdefault("doctor_calls", []).append(kwargs)
                return fixture
            def fake_run(request):
                captured["run_request"] = request
                return {"status": "TEST_DOUBLE_NO_WORKFLOW"}
            with patch("demethflow_core.cli.doctor", side_effect=fake_doctor), patch(
                "demethflow_core.cli.run_decon", side_effect=fake_run
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(["plan", *common, "--json"]), 0)
                self.assertEqual(main(["doctor", *common, "--check-mode", "deep", "--json"]), 0)
                self.assertEqual(main(["run", *common, "--outdir", str(root / "out")]), 0)
            plan_call, doctor_call = captured["doctor_calls"]
            for field in ("scenario", "platform_name", "reference", "tools", "accelerator", "analysis_contract"):
                self.assertEqual(plan_call[field], doctor_call[field])
            run_request = captured["run_request"]
            self.assertEqual(run_request.scenario, plan_call["scenario"])
            self.assertEqual(run_request.platform, plan_call["platform_name"])
            self.assertEqual(run_request.reference, plan_call["reference"])
            self.assertEqual(run_request.tools, plan_call["tools"])
            self.assertEqual(run_request.request_provenance["entrypoint"], "run")
            self.assertTrue(doctor_call["verify_reference_checksums"])

    def test_plan_identity_excludes_observations_but_changes_with_effective_selection(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp04-plan-") as temporary:
            bundle = _bundle(Path(temporary))
            first = _result(bundle, tools=["Houseman"], accelerator="cpu").execution_plan
            second = _result(bundle, tools=["Houseman"], accelerator="cpu").execution_plan
            changed = _result(bundle, tools=["MEnet"], accelerator="cpu").execution_plan
            self.assertEqual(first["semantic_identity"]["sha256"], second["semantic_identity"]["sha256"])
            self.assertNotEqual(first["semantic_identity"]["sha256"], changed["semantic_identity"]["sha256"])
            self.assertEqual(first["constraints"]["checks"][0]["status"], "PASS")
            self.assertEqual(first["constraints"]["check_mode"], "execution_preflight")

    def test_prepare_passes_shared_plan_selection_to_executor_boundary(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp04-boundary-") as temporary:
            root = Path(temporary)
            bundle = _bundle(root)
            input_path = root / "input.csv"
            input_path.write_text("sample,cell_a\ns1,1\n", encoding="utf-8")
            request = DeconRequest(
                input_path=input_path, outdir=root / "out", platform="450k", scenario="fixture",
                reference=bundle.selector, tools="Houseman", runtime="container", request_provenance={"entrypoint": "run"},
            )
            result = _result(bundle)
            with patch("demethflow_core.runner.doctor", return_value=result):
                prepared, command, _env = prepare_decon(request)
            self.assertIs(prepared, result)
            self.assertEqual(prepared.execution_plan["tools"]["selected"][0]["tool"], "Houseman")
            self.assertEqual(command[command.index("--tools") + 1], "Houseman")
            self.assertEqual(command[command.index("--gpu_enabled") + 1], "false")
            self.assertEqual(command[command.index("--menet_device") + 1], "cpu")
            self.assertEqual(
                prepared.adapter_execution["Houseman"]["execution_path"],
                "PRODUCTION_BUILTIN_ADAPTER",
            )

    def test_blocked_preflight_never_constructs_executor_command(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp04-blocked-") as temporary:
            root = Path(temporary)
            bundle = _bundle(root)
            input_path = root / "input.csv"
            input_path.write_text("sample,cell_a\ns1,1\n", encoding="utf-8")
            request = DeconRequest(input_path=input_path, outdir=root / "out", platform="450k", scenario="fixture", reference=bundle.selector)
            blocked = _result(bundle, ok=False)
            with patch("demethflow_core.runner.doctor", return_value=blocked):
                with self.assertRaisesRegex(DeMethFlowError, "Preflight failed"):
                    prepare_decon(request)

    def test_catalog_user_supplied_runtime_block_stops_cli_before_executor(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp04-catalog-block-") as temporary:
            root = Path(temporary)
            bundle = _bundle(root)
            input_path = root / "input.csv"
            input_path.write_text("sample,cell_a\ns1,1\n", encoding="utf-8")
            blocked_catalog = {
                "blockers": [{
                    "code": "USER_SUPPLIED_RUNTIME_REQUIRED",
                    "detail": "CelFEER needs an explicitly configured external runtime",
                }],
            }
            args = [
                "run", "--scenario", "fixture", "--platform", "450k", "--analysis-contract", "array_450k_cpg", "--reference", bundle.selector,
                "--tools", "Houseman", "--accelerator", "cpu", "--input", str(input_path),
                "--outdir", str(root / "out"), "--catalog-metadata-dir", str(root / "catalog"),
            ]
            with patch("demethflow_core.cli.create_catalog_plan", return_value=(blocked_catalog, 2)), patch(
                "demethflow_core.cli.run_decon"
            ) as run_decon, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(args), 2)
            run_decon.assert_not_called()

    def test_known_active_use_cannot_be_overridden_by_maintenance_force(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp04-use-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                leases = root / "home" / ".methunmix" / "active-uses"
                leases.mkdir(parents=True)
                (leases / "fixture.json").write_text(json.dumps({
                    "schema": "methunmix-module-active-use-v1", "state": "ACTIVE",
                    "modules": [{"module_id": "runtime.fixture", "version": "1.0.0"}],
                }), encoding="utf-8")
                report, code = remove_module(
                    "runtime.fixture@1.0.0", dry_run=False, force=True,
                    maintenance=True, maintenance_window_confirmed=True,
                )
                self.assertEqual(code, 2)
                self.assertEqual(report["status"], "BLOCKED_KNOWN_ACTIVE_USE")
                self.assertEqual(report["active_use_state"], "KNOWN_IN_USE")

    def test_confirmed_idle_is_limited_to_the_managed_lease_scope(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp04-idle-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                leases = root / "home" / ".methunmix" / "active-uses"
                leases.mkdir(parents=True)
                (leases / "fixture.json").write_text(json.dumps({
                    "schema": "methunmix-module-active-use-v1", "state": "IDLE_CONFIRMED",
                    "modules": [{"module_id": "runtime.fixture", "version": "1.0.0"}],
                }), encoding="utf-8")
                report, code = remove_module("runtime.fixture@1.0.0", dry_run=True)
                self.assertEqual(code, 0)
                self.assertEqual(report["active_use_state"], "MANAGED_SCOPE_CONFIRMED_IDLE")
                self.assertEqual(report["known_active_uses"][0]["scope"], "managed lease scope only")
