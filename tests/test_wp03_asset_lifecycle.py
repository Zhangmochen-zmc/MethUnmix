from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from demethflow_core.asset_lifecycle import (
    _is_verified_external_contract_requirement,
    compare_catalog_plans,
    create_catalog_plan,
    import_catalog_plan,
    install_catalog_plan,
    preflight_catalog_plan,
    repair_catalog_plan,
    render_catalog_plan_summary,
    verify_catalog_plan,
)
from demethflow_core.cli import main
from demethflow_core.errors import DeMethFlowError, InstallError
from demethflow_core.manifest import load_reference
from demethflow_core.modules import ModuleStore
from demethflow_core.offline_assets import remove_module
from demethflow_core.packaging import export_reference
from demethflow_core.release import _build_module_archive
from demethflow_core.util import data_home_override, sha256_file


def _url(digest: str, name: str) -> str:
    return f"https://fixtures.invalid/objects/sha256/{digest}/{name}"


class WP03AssetLifecycleTests(unittest.TestCase):
    def _archives(self, root: Path) -> tuple[Path, Path, str]:
        ref_root = root / "reference"
        ref_root.mkdir()
        manifest = {
            "schema": "demethflow-reference-v1", "reference_id": "fixture.wp03.450k", "version": "1.0.0",
            "scenario_id": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
            "compatible_input_routes": ["450k"], "genome_build": "not_applicable", "status": "READY",
            "cell_types": [{"cell_type_id": "cell_a", "display_name": "Cell A"}],
            "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
            "tool_capabilities": {"Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": ["runtime.fixture"]}},
            "artifact_checksums": {},
        }
        (ref_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        reference = load_reference(ref_root / "manifest.json")
        ref_archive, _sidecar = export_reference(reference, root / "archives")
        runtime_source = root / "runtime-source"
        runtime_source.mkdir()
        (runtime_source / "entrypoint.txt").write_text("fixture runtime\n", encoding="utf-8")
        runtime = root / "archives/runtime.fixture-1.0.0.tar.gz"
        _build_module_archive(runtime, module_id="runtime.fixture", module_type="runtime", module_version="1.0.0", sources=[(runtime_source / "entrypoint.txt", "entrypoint.txt")])
        return ref_archive, runtime, reference.content_digest()

    def _catalog(self, root: Path, reference: Path, runtime: Path, digest: str, *, cycle: bool = False) -> Path:
        ref_sha, runtime_sha = sha256_file(reference), sha256_file(runtime)
        base = {
            "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "core_version": "2.0.0rc3",
            "catalog_release": "wp03-fixture", "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
            "targets": [
                {
                    "asset_id": "reference.fixture.wp03.450k", "version": "1.0.0", "module_type": "reference",
                    "selector": "fixture.wp03.450k@1.0.0", "reference_id": "fixture.wp03.450k",
                    "scenario": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
                    "genome_build": "not_applicable", "content_digest": digest,
                    "tool_capabilities": {"Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": ["runtime.fixture"]}},
                    "distribution_status": "PUBLIC_DOWNLOADABLE", "license_status": "LICENSE_APPROVED", "license_evidence": "fixture",
                    "immutable_object": True, "sha256": ref_sha, "length": reference.stat().st_size, "installed_bytes": 4096,
                    "url": _url(ref_sha, reference.name),
                    "tool_dependencies": {"Houseman": [{"asset_id": "runtime.fixture", "version": "1.0.0", "sha256": runtime_sha}]},
                    "dependencies": ["runtime.fixture"] if cycle else [],
                },
                {
                    "asset_id": "runtime.fixture", "version": "1.0.0", "module_type": "runtime",
                    "distribution_status": "PUBLIC_DOWNLOADABLE", "license_status": "LICENSE_APPROVED", "license_evidence": "fixture",
                    "immutable_object": True, "sha256": runtime_sha, "length": runtime.stat().st_size, "installed_bytes": 1024,
                    "url": _url(runtime_sha, runtime.name),
                    "dependencies": ["reference.fixture.wp03.450k"] if cycle else [],
                    "compatibility": {"core_api": "2.0"},
                },
            ],
        }
        path = root / "catalog.json"
        path.write_text(json.dumps(base), encoding="utf-8")
        return path

    def _plan(self, root: Path, *, cycle: bool = False):
        reference, runtime, digest = self._archives(root)
        catalog = self._catalog(root, reference, runtime, digest, cycle=cycle)
        with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
            plan, code = create_catalog_plan(
                scenario="fixture", platform="450k", genome_build=None, analysis_contract="array_450k_cpg",
                tools="Houseman", output=root / "plan.json",
            )
        return plan, code, catalog, reference, runtime

    def test_exact_plan_deduplicates_shared_dependency_and_is_stable(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-plan-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                first, code, catalog, _reference, _runtime = self._plan(root)
                self.assertEqual(code, 0)
                self.assertTrue(first["installable"])
                self.assertEqual([item["asset_id"] for item in first["assets"]], ["reference.fixture.wp03.450k", "runtime.fixture"])
                self.assertEqual(first["assets"][1]["required_by"], ["reference.fixture.wp03.450k"])
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    second, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None, analysis_contract="array_450k_cpg",
                        tools="Houseman", output=root / "plan-2.json",
                    )
                self.assertEqual(code, 0)
                self.assertEqual(first["plan_identity"], second["plan_identity"])
                self.assertEqual(first["space"]["download_bytes_unknown_assets"], [])
                self.assertNotEqual(first["created_at"], "")

    def test_all_uses_shared_selection_before_tool_dependency_lookup_and_summary(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-all-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, _code, catalog, _reference, _runtime = self._plan(root)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                reference = payload["targets"][0]
                reference["tool_capabilities"]["EpiDISH"] = {
                    "status": "READY", "artifacts": {}, "runtime_modules": ["runtime.fixture"],
                }
                reference["tool_dependencies"]["EpiDISH"] = [{"asset_id": "runtime.fixture"}]
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None,
                        analysis_contract="array_450k_cpg", tools="all", output=root / "all-plan.json",
                    )
                self.assertEqual(code, 0)
                self.assertEqual(plan["selection"]["tools_selected"], ["EpiDISH", "Houseman"])
                self.assertEqual([item["asset_id"] for item in plan["assets"]], ["reference.fixture.wp03.450k", "runtime.fixture"])
                summary = render_catalog_plan_summary(plan)
                self.assertIn("TOOLS\trequested=all\tselected=EpiDISH,Houseman", summary)
                self.assertIn("STATUS\tINSTALLABLE", summary)

    def test_cycle_and_conflicting_dependency_identity_block_without_install(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-cycle-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                plan, code, _catalog, _reference, _runtime = self._plan(root, cycle=True)
                self.assertEqual(code, 2)
                self.assertFalse(plan["installable"])
                self.assertIn("DEPENDENCY_CYCLE", [item["code"] for item in plan["blockers"]])

    def test_offline_import_verify_repair_and_explicit_upgrade_comparison(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-import-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                plan, code, catalog, reference, runtime = self._plan(root)
                self.assertEqual(code, 0)
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    report, code = import_catalog_plan(root / "plan.json", [reference, runtime])
                    self.assertEqual(code, 0)
                    self.assertEqual(report["status"], "COMPLETE")
                    verified, code = verify_catalog_plan(root / "plan.json")
                    self.assertEqual(code, 0)
                    self.assertTrue(all(row["ok"] for row in verified["assets"]))
                    # Damage only the managed payload; repair must retain the exact lock identity.
                    payload = root / "home/runtimes/runtime.fixture/1.0.0/entrypoint.txt"
                    payload.write_text("damaged\n", encoding="utf-8")
                    damaged, code = verify_catalog_plan(root / "plan.json")
                    self.assertEqual(code, 2)
                    self.assertIn("CORRUPT", [row["state"] for row in damaged["assets"]])
                    repaired, code = repair_catalog_plan(root / "plan.json", [reference, runtime])
                    self.assertEqual(code, 0)
                    self.assertEqual(repaired["after"]["status"], "COMPLETE")
                changed = copy.deepcopy(plan)
                changed["assets"][1]["version"] = "2.0.0"
                next_path = root / "next-plan.json"
                next_path.write_text(json.dumps(changed), encoding="utf-8")
                comparison = compare_catalog_plans(root / "plan.json", next_path)
                self.assertEqual(comparison["status"], "UPGRADE_REQUIRES_EXPLICIT_INSTALL_AND_RETIREMENT_REVIEW")
                self.assertEqual(comparison["changed"], ["runtime.fixture"])

    def test_explicit_upgrade_installs_target_preserves_old_and_records_shared_receipts(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-upgrade-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, code, catalog, reference, runtime_v1 = self._plan(root)
                self.assertEqual(code, 0)
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    imported, code = import_catalog_plan(root / "plan.json", [reference, runtime_v1])
                self.assertEqual(code, 0)
                self.assertTrue(Path(imported["receipt"]).is_file())

                v2_source = root / "runtime-v2-source"
                v2_source.mkdir()
                (v2_source / "entrypoint.txt").write_text("fixture runtime v2\n", encoding="utf-8")
                runtime_v2 = root / "archives/runtime.fixture-2.0.0.tar.gz"
                _build_module_archive(
                    runtime_v2, module_id="runtime.fixture", module_type="runtime", module_version="2.0.0",
                    sources=[(v2_source / "entrypoint.txt", "entrypoint.txt")],
                )
                v2_payload = json.loads(catalog.read_text(encoding="utf-8"))
                v2_payload["catalog_release"] = "wp03-fixture-v2"
                v2_payload["catalog_sequence"] = 2
                v2_runtime = v2_payload["targets"][1]
                v2_runtime.update({
                    "version": "2.0.0", "sha256": sha256_file(runtime_v2),
                    "length": runtime_v2.stat().st_size, "url": _url(sha256_file(runtime_v2), runtime_v2.name),
                })
                v2_payload["targets"][0]["tool_dependencies"]["Houseman"] = [{
                    "asset_id": "runtime.fixture", "version": "2.0.0", "sha256": sha256_file(runtime_v2),
                }]
                catalog_v2 = root / "catalog-v2.json"
                catalog_v2.write_text(json.dumps(v2_payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog_v2)}, clear=False):
                    next_plan, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None, analysis_contract="array_450k_cpg",
                        tools="Houseman", output=root / "plan-v2.json",
                    )
                    self.assertEqual(code, 0)
                    imported_v2, code = import_catalog_plan(root / "plan-v2.json", [reference, runtime_v2])
                self.assertEqual(code, 0)
                self.assertTrue(Path(imported_v2["receipt"]).is_file())
                installed = {(item.module_id, item.version) for item in ModuleStore().list()}
                self.assertIn(("runtime.fixture", "1.0.0"), installed)
                self.assertIn(("runtime.fixture", "2.0.0"), installed)
                comparison = compare_catalog_plans(root / "plan.json", root / "plan-v2.json")
                self.assertEqual(comparison["changed"], ["runtime.fixture"])
                preview, code = remove_module("runtime.fixture@1.0.0", dry_run=True)
                self.assertEqual(code, 0)
                self.assertTrue(preview["impacts"])
                blocked, code = remove_module("runtime.fixture@1.0.0", dry_run=False)
                self.assertEqual(code, 2)
                self.assertEqual(blocked["status"], "BLOCKED_MAINTENANCE_OPERATION_REQUIRED")
                removed, code = remove_module(
                    "runtime.fixture@1.0.0", dry_run=False, force=True,
                    maintenance=True, maintenance_window_confirmed=True,
                )
                self.assertEqual(code, 0)
                self.assertEqual(removed["status"], "RETIRED_TO_RECOVERY_QUARANTINE")
                with self.assertRaises(InstallError):
                    remove_module(
                        "runtime.fixture@1.0.0", dry_run=False, force=True,
                        maintenance=True, maintenance_window_confirmed=True,
                    )
                self.assertIn(("runtime.fixture", "2.0.0"), {(item.module_id, item.version) for item in ModuleStore().list()})

    def test_derived_input_route_uses_manifest_route_not_target_cpg_platform(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-derived-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, _code, catalog, _reference, _runtime = self._plan(root)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                reference = payload["targets"][0]
                reference.update({
                    "selector": "fixture.wgbs-derived-450k-hg38@1.0.0",
                    "reference_id": "fixture.wgbs-derived-450k-hg38",
                    "asset_id": "reference.fixture.wgbs-derived-450k-hg38",
                    "source_platform": "450k", "genome_build": "hg38",
                    "compatible_input_routes": ["wgbs"],
                })
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(
                        scenario="fixture", platform="wgbs", genome_build="hg38", analysis_contract="array_450k_cpg",
                        tools="Houseman", output=root / "derived-plan.json",
                    )
                self.assertEqual(code, 0)
                self.assertEqual(plan["reference"]["selector"], "fixture.wgbs-derived-450k-hg38@1.0.0")

    def test_catalog_without_dependency_metadata_is_readable_but_disclosed(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-legacy-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                plan, code, catalog, _reference, _runtime = self._plan(root)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                payload["targets"][0].pop("dependencies", None)
                payload["targets"][0].pop("tool_dependencies", None)
                payload["targets"][0].pop("profile_dependencies", None)
                payload["targets"][0].pop("dependency_requirements", None)
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None, analysis_contract="array_450k_cpg",
                        tools="Houseman", output=root / "legacy-plan.json",
                    )
                self.assertEqual(code, 0)
                self.assertIn("DEPENDENCY_METADATA_MISSING", [item["code"] for item in plan["warnings"]])
                # The old catalog remains functionally usable for its exact
                # reference import path; the missing graph is disclosed, not
                # invented as a complete dependency set.
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    report, code = import_catalog_plan(root / "legacy-plan.json", [_reference])
                self.assertEqual(code, 0)
                self.assertEqual(report["status"], "COMPLETE")

    def test_logical_dependency_requirements_are_not_reported_as_missing_metadata(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-requirements-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, _code, catalog, _reference, _runtime = self._plan(root)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                target = payload["targets"][0]
                target.pop("dependencies", None)
                target.pop("tool_dependencies", None)
                target.pop("profile_dependencies", None)
                target["dependency_requirements"] = {
                    "Houseman": [{
                        "name": "runtime.fixture",
                        "kind": "runtime_module",
                        "identity_state": "IDENTITY_INSUFFICIENT",
                    }],
                }
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None,
                        analysis_contract="array_450k_cpg", tools="Houseman",
                        output=root / "requirements-plan.json",
                    )
                self.assertEqual(code, 2)
                self.assertNotIn("DEPENDENCY_METADATA_MISSING", [item["code"] for item in plan["warnings"]])
                self.assertIn("DEPENDENCY_IDENTITY_INSUFFICIENT", [item["code"] for item in plan["blockers"]])

    def test_malformed_logical_dependency_requirements_still_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-malformed-requirements-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, _code, catalog, _reference, _runtime = self._plan(root)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                target = payload["targets"][0]
                target.pop("dependencies", None)
                target.pop("tool_dependencies", None)
                target["dependency_requirements"] = {"Houseman": [{"kind": "runtime_module"}]}
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    with self.assertRaisesRegex(DeMethFlowError, "invalid requirement"):
                        create_catalog_plan(
                            scenario="fixture", platform="450k", genome_build=None,
                            analysis_contract="array_450k_cpg", tools="Houseman",
                            output=root / "malformed-requirements-plan.json",
                        )

    def test_cli_uses_plan_for_exact_offline_import_without_legacy_fallback(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-cli-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, code, catalog, reference, runtime = self._plan(root)
                self.assertEqual(code, 0)
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    from contextlib import redirect_stdout, redirect_stderr
                    from io import StringIO
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        self.assertEqual(main([
                            "asset", "import", "--plan", str(root / "plan.json"),
                            str(reference), str(runtime),
                        ]), 0)
                        self.assertEqual(main([
                            "asset", "verify", "--plan", str(root / "plan.json"), "--json",
                        ]), 0)

    def test_cli_can_inspect_complete_selection_without_writing_a_plan(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-cli-inspect-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, code, catalog, _reference, _runtime = self._plan(root)
                self.assertEqual(code, 0)
                from contextlib import redirect_stdout, redirect_stderr
                from io import StringIO
                stdout, stderr = StringIO(), StringIO()
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False), redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main([
                        "asset", "plan", "--scenario", "fixture", "--platform", "450k",
                        "--analysis-contract", "array_450k_cpg", "--tools", "Houseman", "--summary",
                    ]), 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertIn("STATUS\tINSTALLABLE", stdout.getvalue())

    def test_user_supplied_runtime_is_disclosed_and_never_given_a_download_route(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-user-supplied-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, _code, catalog, _reference, _runtime = self._plan(root)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                reference = payload["targets"][0]
                reference["tool_capabilities"] = {
                    "CelFiE": {"status": "READY", "artifacts": {}, "runtime_modules": []},
                }
                reference["tool_dependencies"] = {"CelFiE": []}
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None,
                        analysis_contract="array_450k_cpg", tools="CelFiE", output=root / "runtime-plan.json",
                    )
                self.assertEqual(code, 2)
                self.assertIn("USER_SUPPLIED_RUNTIME_REQUIRED", [item["code"] for item in plan["blockers"]])
                self.assertEqual(plan["external_runtimes"][0]["acquisition"], "USER_SUPPLIED_SUPPORTED")
                self.assertEqual(plan["external_runtimes"][0]["local_state"], "WAITING_FOR_USER_SUPPLIED")
                self.assertFalse(any(item.get("url") for item in plan["external_runtimes"]))

    def test_verified_external_runtime_contract_satisfies_only_its_precise_requirement(self):
        """A checked third-party contract is not an identity-free catalog asset."""
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-external-contract-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                reference, _runtime, digest = self._archives(root)
                catalog = self._catalog(root, reference, _runtime, digest)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                target = payload["targets"][0]
                target["tool_capabilities"] = {
                    "CelFiE": {"status": "READY", "artifacts": {}, "runtime_modules": []},
                }
                target["tool_dependencies"] = {"CelFiE": []}
                target["dependency_requirements"] = {
                    "CelFiE": [{
                        "name": "methunmix-external-runtime.json",
                        "kind": "external_runtime_contract",
                        "identity_state": "RUNTIME_CONTRACT_REQUIRED",
                        "acquisition": "USER_SUPPLIED_SUPPORTED",
                        "detail": "fixture contract",
                    }],
                }
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                external = root / "external-runtime"
                entrypoint = external / "CelFiE/scripts/celfie_new.py"
                entrypoint.parent.mkdir(parents=True)
                entrypoint.write_text("# fixture; never executed\n", encoding="utf-8")
                (external / "methunmix-external-runtime.json").write_text(json.dumps({
                    "schema": "methunmix-external-runtime-v1",
                    "runtimes": {"CelFiE": {
                        "path": "CelFiE", "identity": "fixture-celfie", "version": "1.0",
                        "license": "user-supplied", "required_files": [],
                    }},
                }), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None,
                        analysis_contract="array_450k_cpg", tools="CelFiE",
                        external_runtime_root=external, output=root / "external-plan.json",
                    )
                    self.assertEqual(code, 0)
                    self.assertTrue(plan["installable"])
                    self.assertEqual(plan["external_runtimes"][0]["contract_schema"], "methunmix-external-runtime-v1")
                    self.assertEqual(plan["external_runtimes"][0]["contract_manifest"], "methunmix-external-runtime.json")
                    imported, code = import_catalog_plan(
                        root / "external-plan.json", [reference], external_runtime_root=external,
                    )
                self.assertEqual(code, 0)
                self.assertEqual(imported["status"], "COMPLETE")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    verified, code = verify_catalog_plan(root / "external-plan.json", external_runtime_root=external)
                self.assertEqual(code, 0)
                self.assertEqual(verified["status"], "COMPLETE")
                self.assertEqual(
                    verified["dependency_requirements"][0]["state"],
                    "EXTERNAL_RUNTIME_CONTRACT_VERIFIED",
                )

                # A changed entrypoint does not retain the old plan's identity.
                entrypoint.write_text("# different fixture; never executed\n", encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    drifted, code = verify_catalog_plan(root / "external-plan.json", external_runtime_root=external)
                self.assertEqual(code, 2)
                self.assertEqual(drifted["external_runtimes"][0]["state"], "IDENTITY_MISMATCH")

                requirement = plan["dependency_requirements"][0]
                self.assertTrue(_is_verified_external_contract_requirement(requirement, {"CelFiE"}))
                self.assertFalse(_is_verified_external_contract_requirement({**requirement, "name": "other.json"}, {"CelFiE"}))
                self.assertFalse(_is_verified_external_contract_requirement({**requirement, "required_by": ["Houseman"]}, {"CelFiE"}))
                self.assertFalse(_is_verified_external_contract_requirement({**requirement, "profile": "gpu"}, {"CelFiE"}))

    def test_dependency_identity_conflict_is_never_resolved_by_guessing(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-conflict-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, _code, catalog, _reference, _runtime = self._plan(root)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                payload["targets"][0]["tool_dependencies"]["Houseman"][0]["sha256"] = "0" * 64
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    plan, code = create_catalog_plan(
                        scenario="fixture", platform="450k", genome_build=None, analysis_contract="array_450k_cpg",
                        tools="Houseman", output=root / "conflict-plan.json",
                    )
                self.assertEqual(code, 2)
                self.assertIn("DEPENDENCY_IDENTITY_CONFLICT", [item["code"] for item in plan["blockers"]])

    def test_space_preflight_blocks_before_download_when_known_capacity_is_insufficient(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-space-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, code, catalog, _reference, _runtime = self._plan(root)
                self.assertEqual(code, 0)
                Usage = namedtuple("Usage", "total used free")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False), patch(
                    "demethflow_core.asset_lifecycle.shutil.disk_usage", return_value=Usage(100, 99, 1)
                ):
                    report, code = preflight_catalog_plan(root / "plan.json")
                self.assertEqual(code, 2)
                self.assertEqual(report["status"], "INSUFFICIENT_SPACE")

    def test_public_install_uses_locked_target_identities_without_real_network(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-online-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, code, catalog, reference, runtime = self._plan(root)
                self.assertEqual(code, 0)
                source = {"reference.fixture.wp03.450k": reference, "runtime.fixture": runtime}
                def fake_fetch(asset_id: str, **_kwargs):
                    return {"asset_id": asset_id, "path": str(source[asset_id]), "network_accessed": False}
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False), patch(
                    "demethflow_core.asset_lifecycle.fetch_asset_record", side_effect=fake_fetch
                ) as fetch:
                    report, code = install_catalog_plan(root / "plan.json")
                self.assertEqual(code, 0)
                self.assertEqual(report["status"], "COMPLETE")
                self.assertEqual([call.args[0] for call in fetch.call_args_list], ["reference.fixture.wp03.450k", "runtime.fixture"])

    def test_catalog_change_rejects_locked_plan_before_any_import(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp03-catalog-change-") as temporary:
            root = Path(temporary)
            with data_home_override(root / "home"):
                _plan, code, catalog, reference, _runtime = self._plan(root)
                self.assertEqual(code, 0)
                payload = json.loads(catalog.read_text(encoding="utf-8"))
                payload["catalog_sequence"] = 2
                catalog.write_text(json.dumps(payload), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                    with self.assertRaisesRegex(Exception, "PLAN_CATALOG_CHANGED"):
                        import_catalog_plan(root / "plan.json", [reference])
