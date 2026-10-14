from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from demethflow_core.cli import main
from demethflow_core.manifest import load_reference
from demethflow_core.offline_assets import (
    apply_lock_to_selection,
    compare_upgrade_locks,
    create_bundle,
    create_plan,
    install_lock,
    load_lock,
    remove_module,
    repair_lock,
    verify_lock,
    verify_locked_external_runtimes,
)
from demethflow_core.packaging import export_reference
from demethflow_core.release import _build_module_archive
from demethflow_core.util import data_home_override


class OfflineAssetInstallTests(unittest.TestCase):
    def _reference_archive(self, root: Path, *, tool: str = "Houseman", data_modules: list[str] | None = None) -> Path:
        reference_root = root / "reference"
        reference_root.mkdir()
        manifest = {
            "schema": "demethflow-reference-v1",
            "reference_id": "fixture.offline.450k",
            "version": "1.0.0",
            "scenario_id": "fixture",
            "source_platform": "450k",
            "analysis_contract": "array_450k_cpg",
            "compatible_input_routes": ["450k"],
            "genome_build": "not_applicable",
            "status": "READY",
            "cell_types": [{"cell_type_id": "cell_a", "display_name": "Cell A"}],
            "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
            "tool_capabilities": {
                tool: {
                    "status": "READY",
                    "artifacts": {},
                    "runtime_modules": ["array-common"],
                    "data_modules": data_modules or [],
                }
            },
            "artifact_checksums": {},
        }
        (reference_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        archive, _sidecar = export_reference(load_reference(reference_root / "manifest.json"), root / "archives")
        return archive

    def _profile_reference_archive(self, root: Path) -> Path:
        reference_root = root / "profile-reference"
        reference_root.mkdir()
        manifest = {
            "schema": "demethflow-reference-v1", "reference_id": "fixture.profile.450k", "version": "1.0.0",
            "scenario_id": "profile", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
            "compatible_input_routes": ["450k"], "genome_build": "not_applicable", "status": "PARTIAL",
            "cell_types": [{"cell_type_id": "cell_a", "display_name": "Cell A"}],
            "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
            "tool_capabilities": {
                "Houseman": {
                    "status": "READY", "artifacts": {}, "runtime_modules": ["wrong-fallback"],
                    "execution_profiles": {"gpu": {"status": "READY", "runtime_modules": ["array-common"]}},
                }
            }, "artifact_checksums": {},
        }
        (reference_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        archive, _sidecar = export_reference(load_reference(reference_root / "manifest.json"), root / "archives")
        return archive

    def _runtime_archive(self, root: Path) -> Path:
        source = root / "runtime-source"
        source.mkdir()
        (source / "array_env.sif").write_bytes(b"synthetic-sif")
        archive = root / "archives" / "array-common-1.0.0.tar.gz"
        _build_module_archive(
            archive,
            module_id="array-common",
            module_type="runtime",
            module_version="1.0.0",
            sources=[(source / "array_env.sif", "sifs/array_env.sif")],
        )
        return archive

    def test_plan_install_verify_and_exact_lock_binding(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-offline-") as tmp:
            root = Path(tmp)
            reference = self._reference_archive(root)
            runtime = self._runtime_archive(root)
            bundle_root = root / "offline-kit"
            create_bundle(bundle_root, [reference, runtime], distribution_status="LICENSE_APPROVED")
            lock_path = root / "fixture.assets.lock.json"
            lock, code = create_plan(
                bundle_root / "bundle-index.json", scenario="fixture", platform="450k",
                genome_build=None, analysis_contract="array_450k_cpg", input_type="array",
                tools="all", accelerator="cpu", mode="decon", output=lock_path,
            )
            self.assertEqual(code, 0)
            self.assertTrue(lock["installable"])
            self.assertEqual([row["module_id"] for row in lock["modules"]], ["reference.fixture.offline.450k", "array-common"])
            home = root / "asset-home"
            with data_home_override(home):
                report, code = install_lock(lock_path, bundle_root)
                self.assertEqual(code, 0)
                self.assertEqual(report["status"], "COMPLETE")
                report, code = verify_lock(lock_path)
                self.assertEqual(code, 0)
                values = {"scenario": None, "platform": None, "genome_build": None, "analysis_contract": None,
                          "accelerator": None, "mode": None, "tools": "all", "reference": None,
                          "reference_root": None, "runtime_root": None}
                apply_lock_to_selection(lock_path, values)
                self.assertEqual(values["reference"], "fixture.offline.450k@1.0.0")
                self.assertEqual(values["runtime_root"], home)
                with self.assertRaisesRegex(Exception, "conflicts with --platform"):
                    apply_lock_to_selection(lock_path, {**values, "platform": "wgbs"})

                post_install_lock, post_install_code = create_plan(
                    bundle_root / "bundle-index.json", scenario="fixture", platform="450k",
                    genome_build=None, analysis_contract="array_450k_cpg", input_type="array",
                    tools="all", accelerator="cpu", mode="decon", output=root / "post-install.lock.json",
                )
                self.assertEqual(post_install_code, 0)
                self.assertEqual(
                    [row["installation_state"] for row in post_install_lock["modules"]],
                    ["INSTALLED_HEALTHY", "INSTALLED_HEALTHY"],
                )
                estimate = post_install_lock["installation_estimate"]
                self.assertEqual(estimate["download_bytes_required"], 0)
                self.assertEqual(estimate["installed_bytes_required"], 0)
                self.assertGreater(estimate["estimated_peak_additional_bytes"], 0)

    def test_missing_module_produces_a_non_installable_lock(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-offline-missing-") as tmp:
            root = Path(tmp)
            reference = self._reference_archive(root, data_modules=["MethylBERT-hg19-data"])
            bundle_root = root / "offline-kit"
            create_bundle(bundle_root, [reference])
            lock, code = create_plan(
                bundle_root / "bundle-index.json", scenario="fixture", platform="450k",
                genome_build=None, analysis_contract="array_450k_cpg", input_type="array",
                tools="Houseman", accelerator="cpu", mode="decon", output=root / "missing.lock.json",
            )
            self.assertEqual(code, 2)
            self.assertFalse(lock["installable"])
            self.assertTrue(any(item["module_id"] == "array-common" for item in lock["missing"]))

    def test_upgrade_comparison_is_read_only_and_never_retires_old_modules(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-offline-upgrade-") as tmp:
            root = Path(tmp)
            reference = self._reference_archive(root)
            runtime = self._runtime_archive(root)
            bundle_root = root / "offline-kit"
            create_bundle(bundle_root, [reference, runtime])
            old_path = root / "old.lock.json"
            old, code = create_plan(
                bundle_root / "bundle-index.json", scenario="fixture", platform="450k",
                genome_build=None, analysis_contract="array_450k_cpg", input_type="array",
                tools="Houseman", accelerator="cpu", mode="decon", output=old_path,
            )
            self.assertEqual(code, 0)
            new = copy.deepcopy(old)
            new["reference"]["selector"] = "fixture.offline.450k@2.0.0"
            new["reference"]["content_digest"] = "a" * 64
            new["modules"] = [row for row in new["modules"] if row["module_id"] != "array-common"]
            new["modules"].append({
                "module_id": "array-common", "version": "2.0.0", "sha256": "b" * 64,
            })
            comparison = compare_upgrade_locks(old_path, new)
            self.assertEqual(comparison["status"], "UPGRADE_REQUIRES_EXPLICIT_NEW_LOCK")
            self.assertTrue(comparison["reference"]["changed"])
            self.assertEqual(comparison["modules"]["added"], ["array-common@2.0.0"])
            self.assertEqual(comparison["modules"]["removed_from_new_lock"], ["array-common@1.0.0"])
            self.assertIn("asset remove never runs automatically", comparison["actions"][2])
            self.assertEqual(comparison["scientific_status"], "NOT_EVALUATED_NO_PROMOTION")
            self.assertEqual(json.loads(old_path.read_text(encoding="utf-8"))["reference"]["selector"], old["reference"]["selector"])

    def test_input_contract_errors_cannot_be_misreported_as_installable(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-offline-contract-") as tmp:
            root = Path(tmp)
            reference = self._reference_archive(root)
            runtime = self._runtime_archive(root)
            bundle_root = root / "offline-kit"
            create_bundle(bundle_root, [reference, runtime])
            lock, code = create_plan(
                bundle_root / "bundle-index.json", scenario="fixture", platform="450k",
                genome_build=None, analysis_contract="array_450k_cpg", input_type="bed",
                tools="Houseman", accelerator="cpu", mode="decon", output=root / "bad-input.lock.json",
            )
            self.assertEqual(code, 2)
            self.assertFalse(lock["installable"])
            self.assertEqual(lock["input_contract_errors"], ["450k input requires --input-type array"])

    def test_execution_profile_does_not_fall_back_to_other_device(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-offline-profile-") as tmp:
            root = Path(tmp)
            reference = self._profile_reference_archive(root)
            runtime = self._runtime_archive(root)
            bundle_root = root / "offline-kit"
            create_bundle(bundle_root, [reference, runtime])
            with self.assertRaisesRegex(Exception, "no cpu execution profile"):
                create_plan(
                    bundle_root / "bundle-index.json", scenario="profile", platform="450k",
                    genome_build=None, analysis_contract="array_450k_cpg", input_type="array",
                    tools="Houseman", accelerator="cpu", mode="decon", output=root / "cpu.lock.json",
                )

    def test_cli_plan_requires_complete_selection_when_a_bundle_is_used(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-offline-cli-") as tmp:
            root = Path(tmp)
            bundle_root = root / "offline-kit"
            bundle_root.mkdir()
            (bundle_root / "bundle-index.json").write_text(json.dumps({
                "schema": "methunmix-offline-bundle-v1", "modules": [],
            }), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                exit_code = main(["asset", "plan", "--bundle", str(bundle_root / "bundle-index.json")])
            self.assertEqual(exit_code, 2)

    def test_cli_plan_install_verify_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-offline-cli-roundtrip-") as tmp:
            root = Path(tmp)
            reference = self._reference_archive(root)
            runtime = self._runtime_archive(root)
            bundle_root = root / "offline-kit"
            create_bundle(bundle_root, [reference, runtime])
            home = root / "asset-home"
            lock_path = root / "roundtrip.lock.json"
            command_prefix = ["--home", str(home), "asset"]
            plan = [
                *command_prefix, "plan", "--bundle", str(bundle_root / "bundle-index.json"),
                "--scenario", "fixture", "--platform", "450k",
                "--analysis-contract", "array_450k_cpg", "--input-type", "array",
                "--tools", "Houseman", "--output", str(lock_path),
            ]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(plan), 0)
                self.assertEqual(main([*command_prefix, "install", "--lock", str(lock_path), "--bundle-root", str(bundle_root)]), 0)
                self.assertEqual(main([*command_prefix, "verify", "--lock", str(lock_path)]), 0)

    def test_remove_is_receipt_aware_and_repair_restores_exact_locked_module(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-offline-repair-") as tmp:
            root = Path(tmp)
            reference = self._reference_archive(root)
            runtime = self._runtime_archive(root)
            bundle_root = root / "offline-kit"
            create_bundle(bundle_root, [reference, runtime], distribution_status="LICENSE_APPROVED")
            lock_path = root / "fixture.lock.json"
            create_plan(
                bundle_root / "bundle-index.json", scenario="fixture", platform="450k",
                genome_build=None, analysis_contract="array_450k_cpg", input_type="array",
                tools="Houseman", accelerator="cpu", mode="decon", output=lock_path,
            )
            home = root / "asset-home"
            with data_home_override(home):
                self.assertEqual(install_lock(lock_path, bundle_root)[1], 0)
                preview, preview_code = remove_module("array-common@1.0.0", dry_run=True)
                self.assertEqual(preview_code, 0)
                self.assertEqual(preview["status"], "DRY_RUN")
                self.assertEqual(preview["active_use_state"], "UNKNOWN_NO_MANAGED_LEASE_REGISTRY")
                blocked, blocked_code = remove_module("array-common@1.0.0", dry_run=False)
                self.assertEqual(blocked_code, 2)
                self.assertEqual(blocked["status"], "BLOCKED_MAINTENANCE_OPERATION_REQUIRED")
                removed, removed_code = remove_module(
                    "array-common@1.0.0", dry_run=False, force=True,
                    maintenance=True, maintenance_window_confirmed=True,
                )
                self.assertEqual(removed_code, 0)
                self.assertEqual(removed["status"], "RETIRED_TO_RECOVERY_QUARANTINE")
                self.assertTrue(Path(removed["recovery_path"]).is_dir())
                self.assertEqual(verify_lock(lock_path)[1], 2)
                repaired, repaired_code = repair_lock(lock_path, bundle_root)
                self.assertEqual(repaired_code, 0)
                self.assertEqual(repaired["status"], "REPAIRED")
                self.assertEqual(verify_lock(lock_path)[1], 0)

    def test_locked_external_runtime_metadata_is_checked(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-offline-external-") as tmp:
            root = Path(tmp)
            runtime_root = root / "external"
            tool_root = runtime_root / "CelFiE"
            (tool_root / "scripts").mkdir(parents=True)
            (tool_root / "scripts" / "celfie_new.py").write_text("print('fixture')\n", encoding="utf-8")
            (runtime_root / "methunmix-external-runtime.json").write_text(json.dumps({
                "schema": "methunmix-external-runtime-v1",
                "runtimes": {"CelFiE": {"path": "CelFiE", "identity": "celfie-fixture", "version": "1.0", "license": "AGPL-3.0", "required_files": ["scripts/celfie_new.py"]}},
            }), encoding="utf-8")
            lock = {"external_runtimes": {"CelFiE": {"identity": "celfie-fixture", "version": "1.0", "license": "AGPL-3.0", "required_files": ["scripts/celfie_new.py"]}}}
            verify_locked_external_runtimes(lock, runtime_root)
            lock["external_runtimes"]["CelFiE"]["version"] = "2.0"
            with self.assertRaisesRegex(Exception, "does not match"):
                verify_locked_external_runtimes(lock, runtime_root)
