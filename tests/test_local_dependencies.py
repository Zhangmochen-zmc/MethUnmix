from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from demethflow_core.asset_lifecycle import create_catalog_plan, import_catalog_plan, verify_catalog_plan
from demethflow_core.cli import main as cli_main
from demethflow_core.local_dependencies import BINDING_SCHEMA, LOCATOR_SCHEMA
from demethflow_core.local_dependencies import resolve_local_dependencies
from demethflow_core.manifest import load_reference
from demethflow_core.modules import ModuleStore
from demethflow_core.packaging import export_reference
from demethflow_core.runner import _offline_env, run_decon
from demethflow_core.runtime import DoctorResult, doctor, execution_plan_for_doctor, nextflow_discovery
from demethflow_core.util import data_home_override, sha256_file


class LocalDependencyBindingTests(unittest.TestCase):
    def _fixture(self, root: Path, *, kind: str = "logical_runtime_module") -> tuple[Path, Path, Path, Path]:
        ref_root = root / "reference"
        ref_root.mkdir()
        (ref_root / "manifest.json").write_text(json.dumps({
            "schema": "demethflow-reference-v1", "reference_id": "fixture.local", "version": "1.0.0",
            "scenario_id": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
            "compatible_input_routes": ["450k"], "genome_build": "not_applicable", "status": "READY",
            "cell_types": [{"cell_type_id": "cell_a", "display_name": "Cell A"}],
            "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
            "tool_capabilities": {"Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": ["Houseman", "array-common"]}},
            "artifact_checksums": {},
        }), encoding="utf-8")
        archive, _ = export_reference(load_reference(ref_root / "manifest.json"), root / "archives")
        runtime = root / "houseman_env.sif"
        runtime.write_bytes(b"local-runtime-fixture\n")
        common = root / "array_env.sif"
        common.write_bytes(b"local-common-fixture\n")
        catalog = root / "catalog.json"
        catalog.write_text(json.dumps({
            "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "core_version": "2.1.0.dev7",
            "catalog_release": "fixture-local", "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
            "targets": [{
                "asset_id": "reference.fixture.local", "module_type": "reference", "version": "1.0.0",
                "selector": "fixture.local@1.0.0", "reference_id": "fixture.local", "scenario": "fixture",
                "source_platform": "450k", "compatible_input_routes": ["450k"], "analysis_contract": "array_450k_cpg",
                "genome_build": "not_applicable", "content_digest": load_reference(ref_root / "manifest.json").content_digest(),
                "tool_capabilities": {"Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": ["Houseman", "array-common"]}},
                "distribution_status": "PUBLIC_DOWNLOADABLE", "sha256": sha256_file(archive), "length": archive.stat().st_size,
                "installed_bytes": 100, "immutable_object": True, "license_status": "LICENSE_APPROVED", "license_evidence": "fixture",
                "url": f"https://fixtures.invalid/objects/sha256/{sha256_file(archive)}/reference.fixture.local.tar.gz", "dependencies": [], "tool_dependencies": {},
                "dependency_requirements": {"Houseman": [
                    {"name": "Houseman", "kind": kind, "identity_state": "IDENTITY_INSUFFICIENT"},
                    {"name": "array-common", "kind": kind, "identity_state": "IDENTITY_INSUFFICIENT"},
                ]},
            }],
        }), encoding="utf-8")
        return archive, runtime, common, catalog

    def _documents(self, root: Path, runtime: Path, common: Path, *, kind: str = "logical_runtime_module") -> tuple[Path, Path]:
        lock = root / "bindings.json"
        lock.write_text(json.dumps({
            "schema": BINDING_SCHEMA,
            "bindings": [
                {"name": "Houseman", "kind": kind, "scope": {"scenario": "fixture", "platform": "450k", "analysis_contract": "array_450k_cpg", "accelerator": "cpu", "tools": ["Houseman"]}, "identity": {"type": "file", "sha256": sha256_file(runtime), "bytes": runtime.stat().st_size}, "locator_key": "houseman", "registration_state": "LOCAL_REGISTERED"},
                {"name": "array-common", "kind": kind, "scope": {"scenario": "fixture", "platform": "450k", "analysis_contract": "array_450k_cpg", "accelerator": "cpu", "tools": ["Houseman"]}, "identity": {"type": "file", "sha256": sha256_file(common), "bytes": common.stat().st_size}, "locator_key": "common", "registration_state": "LOCAL_REGISTERED"},
            ],
        }), encoding="utf-8")
        locators = root / "locators.json"
        locators.write_text(json.dumps({"schema": LOCATOR_SCHEMA, "locators": {"houseman": {"path": str(runtime)}, "common": {"path": str(common)}}}), encoding="utf-8")
        return lock, locators

    def _plan(self, root: Path, lock: Path | None, locators: Path | None):
        with patch.dict("os.environ", {"METHUNMIX_CATALOG_PATH": str(root / "catalog.json")}, clear=False):
            return create_catalog_plan(
                scenario="fixture", platform="450k", genome_build=None, analysis_contract="array_450k_cpg", tools="Houseman",
                local_dependency_lock=lock, local_dependency_locators=locators,
            )

    def test_exact_local_files_resolve_logical_requirements_without_distribution_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                archive, runtime, common, _catalog = self._fixture(root)
                ModuleStore().install(archive)
                lock, locators = self._documents(root, runtime, common)
                plan, code = self._plan(root, lock, locators)
                self.assertEqual(code, 0)
                self.assertTrue(plan["installable"])
                self.assertEqual([row["name"] for row in plan["local_dependencies"]], ["Houseman", "array-common"])
                self.assertTrue(all(row["distribution_status"] == "USER_SUPPLIED_LOCAL_ONLY" for row in plan["local_dependencies"]))

    def test_catalog_import_rechecks_local_bindings_before_complete_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                archive, runtime, common, _catalog = self._fixture(root)
                lock, locators = self._documents(root, runtime, common)
                plan_path = root / "plan.json"
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(root / "catalog.json")}, clear=False):
                    _plan, code = create_catalog_plan(scenario="fixture", platform="450k", genome_build=None,
                        analysis_contract="array_450k_cpg", tools="Houseman", output=plan_path,
                        local_dependency_lock=lock, local_dependency_locators=locators)
                self.assertEqual(code, 0)
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(root / "catalog.json")}, clear=False):
                    imported, code = import_catalog_plan(plan_path, [archive], local_dependency_lock=lock, local_dependency_locators=locators)
                self.assertEqual(code, 0)
                self.assertEqual(imported["status"], "COMPLETE")
                self.assertTrue(Path(imported["receipt"]).is_file())
                runtime.write_bytes(b"drift\n")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(root / "catalog.json")}, clear=False):
                    checked, code = verify_catalog_plan(plan_path, local_dependency_lock=lock, local_dependency_locators=locators)
                self.assertEqual(code, 2)
                self.assertIn("LOCAL_DEPENDENCY_IDENTITY_MISMATCH", [row["code"] for row in checked["local_dependency_blockers"]])

    def test_missing_or_drifted_local_file_blocks_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                archive, runtime, common, _catalog = self._fixture(root)
                ModuleStore().install(archive)
                lock, locators = self._documents(root, runtime, common)
                runtime.write_bytes(b"drift\n")
                plan, code = self._plan(root, lock, locators)
                self.assertEqual(code, 2)
                self.assertIn("LOCAL_DEPENDENCY_IDENTITY_MISMATCH", [row["code"] for row in plan["blockers"]])
                locators.write_text(json.dumps({"schema": LOCATOR_SCHEMA, "locators": {"houseman": {"path": str(root / "missing.sif")}, "common": {"path": str(common)}}}), encoding="utf-8")
                plan, code = self._plan(root, lock, locators)
                self.assertEqual(code, 2)
                self.assertIn("LOCAL_DEPENDENCY_NOT_FOUND", [row["code"] for row in plan["blockers"]])

    def test_conflicting_or_partial_binding_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                archive, runtime, common, _catalog = self._fixture(root)
                ModuleStore().install(archive)
                lock, locators = self._documents(root, runtime, common)
                payload = json.loads(lock.read_text())
                payload["bindings"].append({**payload["bindings"][0], "identity": {"type": "file", "sha256": "0" * 64, "bytes": runtime.stat().st_size}})
                lock.write_text(json.dumps(payload), encoding="utf-8")
                plan, code = self._plan(root, lock, locators)
                self.assertEqual(code, 2)
                self.assertIn("LOCAL_DEPENDENCY_IDENTITY_CONFLICT", [row["code"] for row in plan["blockers"]])
                plan, code = self._plan(root, lock, None)
                self.assertEqual(code, 2)
                self.assertIn("LOCAL_DEPENDENCY_BINDING_REQUIRED", [row["code"] for row in plan["blockers"]])

    def test_directory_member_replacement_or_extra_file_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            cache.mkdir()
            member = cache / "model.bin"
            member.write_bytes(b"fixed-cache")
            members = [{"path": "model.bin", "sha256": sha256_file(member), "bytes": member.stat().st_size}]
            digest = hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            lock = root / "directory-binding.json"
            lock.write_text(json.dumps({"schema": BINDING_SCHEMA, "bindings": [{
                "name": "fixture-cache", "kind": "logical_cache", "scope": {"scenario": "fixture", "platform": "450k", "analysis_contract": "array_450k_cpg", "accelerator": "cpu", "tools": ["Houseman"]},
                "identity": {"type": "directory", "sha256": digest, "bytes": member.stat().st_size, "members": members}, "locator_key": "cache",
            }]}), encoding="utf-8")
            locators = root / "directory-locators.json"
            locators.write_text(json.dumps({"schema": LOCATOR_SCHEMA, "locators": {"cache": {"path": str(cache)}}}), encoding="utf-8")
            requirement = [{"name": "fixture-cache", "kind": "logical_cache", "profile": None, "required_by": ["Houseman"]}]
            selection = {"scenario": "fixture", "platform": "450k", "analysis_contract": "array_450k_cpg", "accelerator": "cpu"}
            rows, blockers = resolve_local_dependencies(requirement, selection, lock, locators)
            self.assertEqual(blockers, [])
            self.assertEqual(len(rows), 1)
            (cache / "extra.bin").write_bytes(b"extra")
            _rows, blockers = resolve_local_dependencies(requirement, selection, lock, locators)
            self.assertIn("LOCAL_DEPENDENCY_MEMBER_SET_MISMATCH", [row["code"] for row in blockers])

    def test_locator_must_be_absolute_and_may_not_traverse_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _archive, runtime, common, _catalog = self._fixture(root)
                lock, locators = self._documents(root, runtime, common)
                requirement = [{"name": "Houseman", "kind": "logical_runtime_module", "profile": None, "required_by": ["Houseman"]}]
                selection = {"scenario": "fixture", "platform": "450k", "analysis_contract": "array_450k_cpg", "accelerator": "cpu"}
                payload = json.loads(locators.read_text())
                payload["locators"]["houseman"]["path"] = "relative/houseman_env.sif"
                locators.write_text(json.dumps(payload), encoding="utf-8")
                _rows, blockers = resolve_local_dependencies(requirement, selection, lock, locators)
                self.assertIn("LOCAL_DEPENDENCY_LOCATOR_INVALID", [row["code"] for row in blockers])
                link_dir = root / "runtime-link"
                link_dir.symlink_to(root, target_is_directory=True)
                payload["locators"]["houseman"]["path"] = str(link_dir / runtime.name)
                locators.write_text(json.dumps(payload), encoding="utf-8")
                _rows, blockers = resolve_local_dependencies(requirement, selection, lock, locators)
                self.assertIn("LOCAL_DEPENDENCY_LOCATOR_INVALID", [row["code"] for row in blockers])

    def test_public_run_binds_catalog_plan_to_selected_runtime_before_fixture_launch(self):
        """Exercise the public run parser through the production runner.

        The executor is a tiny Python fixture, not Nextflow or Houseman.  The
        point is the complete engineering chain: catalog lock and locator
        overlay become a request plan, doctor selects those exact files, and
        the runner rehashes them immediately before the fixture command.
        """
        with tempfile.TemporaryDirectory(prefix="methunmix-local-binding-cli-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _archive, runtime, common, catalog = self._fixture(root, kind="runtime_module")
                lock, locators = self._documents(root, runtime, common, kind="runtime_module")
                input_path = root / "input.csv"
                input_path.write_text("SampleID,marker\ns1,0.1\ns2,0.2\n", encoding="utf-8")
                executor = root / "fixture_executor.py"
                executor.write_text(
                    "import pathlib, sys\n"
                    "outdir = pathlib.Path(sys.argv[1])\n"
                    "(outdir / 'results').mkdir(parents=True, exist_ok=True)\n"
                    "(outdir / 'results' / 'Houseman_results.csv').write_text("
                    "'SampleID,Cell A\\ns1,1.0\\ns2,1.0\\n', encoding='utf-8')\n",
                    encoding="utf-8",
                )
                bundle = load_reference(root / "reference" / "manifest.json")
                result = DoctorResult(
                    bundle=bundle, selected_tools=["Houseman"], accelerator="cpu",
                    nextflow_command=["synthetic-executor"], container_engine="fixture",
                )
                result.add("reference-resolution", True, bundle.selector)
                result.add("runtime-selection", True, "fixture local runtime files")
                result.runtime_files = {"Houseman": runtime, "array-common": common}
                result.execution_plan = execution_plan_for_doctor(
                    result, mode="decon", scenario="fixture", platform_name="450k",
                    genome_build=None, analysis_contract="array_450k_cpg", input_path=input_path,
                    requested_tools="Houseman", allow_cross_platform=False,
                    request_provenance={"schema": "methunmix-resolved-analysis-request-v1", "entrypoint": "test"},
                    check_mode="execution_preflight",
                )
                captured = {}

                def through_runner(request):
                    captured["request"] = request
                    return run_decon(request)

                argv = [
                    "run", "--scenario", "fixture", "--platform", "450k",
                    "--analysis-contract", "array_450k_cpg", "--reference", "fixture.local@1.0.0",
                    "--tools", "Houseman", "--input", str(input_path), "--outdir", str(root / "run"),
                    "--reference-root", str(root / "reference"), "--runtime-root", str(root),
                    "--local-dependency-lock", str(lock), "--local-dependency-locators", str(locators),
                    "--workflow-timeout-seconds", "5", "--random-seed", "17",
                ]
                command = [sys.executable, str(executor), str(root / "run")]
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False), \
                     patch("demethflow_core.cli.run_decon", side_effect=through_runner), \
                     patch("demethflow_core.runner.prepare_decon", return_value=(result, command, _offline_env(root / "run"))):
                    with redirect_stdout(StringIO()):
                        self.assertEqual(cli_main(argv), 0)
                request = captured["request"]
                plan = request.installation_dependency_plan
                self.assertEqual(plan["state"], "RESOLVED")
                self.assertEqual([row["name"] for row in plan["local_dependencies"]], ["Houseman", "array-common"])
                self.assertEqual(request.local_runtime_files, {"Houseman": runtime, "array-common": common})
                manifest = json.loads((root / "run" / "run_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["execution_status"], "SUCCEEDED")
                self.assertEqual(manifest["launch_dependency_verification"]["state"], "VERIFIED_AT_LAUNCH")
                self.assertTrue((root / "run" / "canonical" / "Houseman_results.csv").is_file())

    def test_runtime_kind_aliases_match_but_unknown_kind_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-runtime-kind-alias-") as temporary:
            root = Path(temporary)
            _archive, runtime, common, _catalog = self._fixture(root, kind="runtime_module")
            # A historical lock remains usable for a current catalog request.
            lock, locators = self._documents(root, runtime, common, kind="logical_runtime_module")
            requirement = [{"name": "Houseman", "kind": "runtime_module", "profile": None, "required_by": ["Houseman"]}]
            selection = {"scenario": "fixture", "platform": "450k", "analysis_contract": "array_450k_cpg", "accelerator": "cpu"}
            runtime_files = {}
            rows, blockers = resolve_local_dependencies(requirement, selection, lock, locators, runtime_files=runtime_files)
            self.assertEqual(blockers, [])
            self.assertEqual(rows[0]["kind"], "runtime_module")
            self.assertEqual(runtime_files, {"Houseman": runtime})
            unknown = [{**requirement[0], "kind": "runtime_module_typo"}]
            _rows, blockers = resolve_local_dependencies(unknown, selection, lock, locators, runtime_files={})
            self.assertIn("LOCAL_DEPENDENCY_KIND_UNSUPPORTED", [row["code"] for row in blockers])

    def test_verified_binding_context_is_consumed_by_doctor_not_registry_search(self):
        """A public catalog plan passes the verified local SIFs into doctor.

        The fixture has no ``runtimes/sifs`` registry tree.  If doctor falls
        back to the old filename resolver rather than consuming its ephemeral
        binding context, both required runtime checks fail.
        """
        with tempfile.TemporaryDirectory(prefix="methunmix-local-binding-doctor-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                archive, runtime, common, catalog = self._fixture(root)
                ModuleStore().install(archive)
                lock, locators = self._documents(root, runtime, common)
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None,
                        analysis_contract="array_450k_cpg", tools="Houseman",
                        local_dependency_lock=lock, local_dependency_locators=locators,
                        capture_local_runtime_files=True,
                    )
                self.assertEqual(code, 0)
                context = plan.pop("_ephemeral_local_runtime_files")
                self.assertEqual(context, {"Houseman": runtime, "array-common": common})
                public_plan = {
                    "state": "RESOLVED", "local_dependencies": plan["local_dependencies"],
                }
                input_path = root / "input.csv"
                input_path.write_text("SampleID,marker\ns1,0.1\ns2,0.2\n", encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False), \
                     patch("demethflow_core.runtime.bundled_nextflow", return_value=["fixture-nextflow"]):
                    result = doctor(
                        mode="decon", reference="fixture.local@1.0.0", scenario="fixture",
                        platform_name="450k", genome_build=None, tools="Houseman", input_path=input_path,
                        reference_root=root / "reference", runtime_root=root / "no-registry", runtime="conda",
                        container_engine="auto", allow_cross_platform=False,
                        analysis_contract="array_450k_cpg", installation_dependency_plan=public_plan,
                        local_runtime_files=context,
                    )
                self.assertEqual(result.runtime_files, context)
                checks = {check.name: check for check in result.checks}
                self.assertTrue(checks["runtime:Houseman"].ok)
                self.assertTrue(checks["runtime:array-common"].ok)
                self.assertTrue(checks["local-dependency:Houseman"].ok)
                self.assertTrue(checks["local-dependency:array-common"].ok)

    def test_invalid_nextflow_override_does_not_fall_back_to_path(self):
        with patch.dict(os.environ, {"METHUNMIX_NEXTFLOW_CMD": "/missing/nextflow"}, clear=False), \
             patch("demethflow_core.runtime.shutil.which", return_value="/trusted/system/nextflow"):
            command, detail = nextflow_discovery()
        self.assertIsNone(command)
        self.assertIn("configured Nextflow executable is unavailable", detail)

    def test_explicit_nextflow_override_resolves_exact_executable(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-nextflow-override-") as temporary:
            executable = Path(temporary) / "nextflow"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            with patch.dict(os.environ, {"METHUNMIX_NEXTFLOW_CMD": str(executable)}, clear=False):
                command, detail = nextflow_discovery()
            self.assertEqual(command, [str(executable)])
            self.assertIn(str(executable), detail)
