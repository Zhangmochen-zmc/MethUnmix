from __future__ import annotations

import hashlib
import json
import os
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from demethflow_core import PROJECT_ROOT, version
from demethflow_core.asset_manager import gc_assets, load_catalog, fetch_asset
from demethflow_core.modules import ModuleStore
from demethflow_core.errors import InstallError
from demethflow_core.errors import DeMethFlowError
from demethflow_core.manifest import ReferenceBundle
from demethflow_core.packaging import export_reference


class MethUnmixControlPlaneTests(unittest.TestCase):
    def test_packaged_python_helpers_do_not_depend_on_executable_mode(self):
        interpreter_invocations = {
            "450k_main.nf": ("aric_decon.py", "menet.py", "methatlas_decon.py"),
            "epic_main.nf": ("aric_decon.py", "menet.py", "methatlas_decon.py"),
            "wgbs_epic_main.nf": ("aric_decon.py", "methatlas_decon.py"),
            "wgbs_main.nf": ("menet_standardize.py",),
        }
        for workflow_name, helper_names in interpreter_invocations.items():
            workflow = (PROJECT_ROOT / "deconvolution" / workflow_name).read_text(encoding="utf-8")
            with self.subTest(workflow=workflow_name):
                for helper in helper_names:
                    escaped = f'python3 \\\"${{projectDir}}/bin/{helper}\\\"'
                    multiline = f'python3 "${{projectDir}}/bin/{helper}"'
                    self.assertTrue(escaped in workflow or multiline in workflow)
                    self.assertNotIn(f'script: "{helper} ', workflow)
        wgbs_main = (PROJECT_ROOT / "deconvolution" / "wgbs_main.nf").read_text(encoding="utf-8")
        self.assertIn('python3 "${projectDir}/bin/menet.py" predict', wgbs_main)
        self.assertNotIn("No such variable: thread_env", wgbs_main)
        self.assertNotIn("    menet.py predict", wgbs_main)

    def test_resource_root_and_version(self):
        self.assertEqual(version(), "2.0.0rc1")
        self.assertTrue((PROJECT_ROOT / "deconvolution" / "wgbs_preprocess.nf").is_file())

    def test_methylbert_fix_process_passes_declared_output_without_placeholder(self):
        workflow = (PROJECT_ROOT / "deconvolution" / "wgbs_main.nf").read_text(encoding="utf-8")
        process_start = workflow.index("process PRE_METHYLBERT_FIX {")
        process_end = workflow.index("\nprocess RUN_METHYLBERT {", process_start)
        process = workflow[process_start:process_end]
        self.assertIn('path("fixed_${reads_csv}")', process)
        self.assertIn("python ${fix_script} '${reads_csv}' 'fixed_${reads_csv}'", process)
        self.assertNotIn("placeholder", process)

    def test_conda_environment_tool_discovery_finds_non_path_executable(self):
        from scripts.audit_conda_environment import locate_tool

        with tempfile.TemporaryDirectory(prefix="methunmix-conda-env-probe-") as temporary:
            prefix = Path(temporary)
            bindir = prefix / "bin"
            bindir.mkdir()
            tool = bindir / "conda-build"
            tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            tool.chmod(0o755)
            self.assertEqual(
                locate_tool("conda-build", None, [prefix]),
                (str(tool), "CONDA_ENV", str(prefix)),
            )

    def test_absolute_container_engine_path_is_preserved_and_version_probed(self):
        from demethflow_core.runtime import choose_container_engine, inspect_container_engine

        self.assertEqual(choose_container_engine("not-a-container-engine"), (None, None))
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as temporary:
            executable = Path(temporary) / "apptainer"
            executable.write_text("#!/bin/sh\nprintf 'apptainer version test\\n'\n", encoding="utf-8")
            executable.chmod(0o755)
            engine, resolved = choose_container_engine(str(executable))
            self.assertEqual(engine, "apptainer")
            self.assertEqual(resolved, str(executable))
            ok, version_text, source, detail = inspect_container_engine(str(executable), resolved)
            self.assertTrue(ok)
            self.assertEqual(version_text, "apptainer version test")
            self.assertEqual(source, "explicit")
            self.assertIn(str(executable), detail)
            from demethflow_core.cli import build_parser
            parsed = build_parser().parse_args(["doctor", "--container-engine", str(executable)])
            self.assertEqual(parsed.container_engine, str(executable))

    def test_container_sif_smoke_is_bounded_and_uses_selected_executable(self):
        from demethflow_core.runtime import check_container_sif_smoke
        import subprocess

        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as temporary:
            sif = Path(temporary) / "runtime.sif"
            sif.write_bytes(b"test")
            with patch("demethflow_core.runtime.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "", "")
                ok, detail = check_container_sif_smoke(
                    executable="/opt/apptainer/bin/apptainer",
                    engine="apptainer",
                    runtime_file=sif,
                )
            self.assertTrue(ok)
            self.assertIn("exit_code=0", detail)
            args, kwargs = run.call_args
            self.assertEqual(args[0][0:3], ["/opt/apptainer/bin/apptainer", "exec", str(sif)])
            self.assertEqual(args[0][-1], "true")
            self.assertEqual(kwargs["timeout"], 30)

    def test_core_sbom_binds_staged_artifacts_and_discloses_license_gaps(self):
        from scripts.generate_sbom import DEFAULT_INVENTORY, DEFAULT_SOURCE, DEFAULT_WHEEL, build_sbom

        bom = build_sbom(DEFAULT_SOURCE, DEFAULT_WHEEL, DEFAULT_INVENTORY)
        self.assertEqual(bom["bomFormat"], "CycloneDX")
        self.assertEqual(bom["specVersion"], "1.6")
        props = {item["name"]: item["value"] for item in bom["metadata"]["component"]["properties"]}
        self.assertEqual(props["methunmix:sourceArchiveSha256"], hashlib.sha256(DEFAULT_SOURCE.read_bytes()).hexdigest())
        self.assertEqual(props["methunmix:wheelSha256"], hashlib.sha256(DEFAULT_WHEEL.read_bytes()).hexdigest())
        self.assertEqual(props["methunmix:firstPartyLicense"], "MIT; does not grant bundled third-party rights")
        self.assertNotIn("licenses", bom["metadata"]["component"])
        self.assertEqual(props["methunmix:licenseAssessment"], "PENDING_MIXED_VENDOR_LICENSE_REVIEW")
        vendor = {item["name"]: item for item in bom["components"] if item.get("type") == "library"}
        self.assertIn("CelFEER", vendor)
        self.assertTrue(any(prop["name"] == "methunmix:distributionStatus" and prop["value"] == "BLOCKED" for prop in vendor["CelFEER"]["properties"]))
        self.assertTrue(any(item["name"] == "methunmix:sbomCompleteness" and item["value"] == "PARTIAL_RC_CORE_ONLY" for item in bom["metadata"]["properties"]))

    def test_catalog_is_offline_static_staging(self):
        catalog = load_catalog()
        self.assertEqual(catalog["product"], "MethUnmix")
        self.assertEqual(catalog["schema"], "methunmix-static-catalog-v1")
        self.assertEqual(catalog["trust_model"], "HTTPS_STATIC_CATALOG_SHA256")
        self.assertEqual(catalog["metadata_signatures"], "NOT_USED_TUF_DEFERRED")
        self.assertEqual(catalog["targets"], [])
        self.assertTrue(catalog["empty_catalog_policy"]["allowed"])
        self.assertEqual(catalog["empty_catalog_policy"]["fallback"], "USER_SUPPLIED_SUPPORTED_OR_BLOCKED")

    def test_manifest_derived_capability_matrix_does_not_overclaim_routes(self):
        matrix_path = PROJECT_ROOT.parent.parent / "evidence" / "capability_matrix_rc.json"
        registry_path = PROJECT_ROOT / "capabilities" / "capability_registry.json"
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(matrix["schema"], "methunmix-capability-matrix-v2")
        self.assertEqual(len(registry["logical_tools"]), 21)
        self.assertEqual(matrix["summary"]["logical_tool_count"], 21)
        self.assertFalse(matrix["summary"]["unrepresented_logical_tools"])
        self.assertTrue(matrix["source_manifest_audit"])
        self.assertTrue(all(item["status"] == "MATCH" for item in matrix["source_manifest_audit"]))

        root = PROJECT_ROOT.parent.parent
        baseline = json.loads((root / "evidence" / "baseline_candidate_inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["tolerance_review"]["tools_without_reference_candidates"], [])
        license_inventory = json.loads((root / "evidence" / "LICENSE_GAP_INVENTORY.json").read_text(encoding="utf-8"))
        blocked_tool_code = [
            item for item in license_inventory["items"]
            if item.get("asset_id", "").startswith("tool-code:")
            and item.get("proposed_distribution_status") == "BLOCKED"
        ]
        self.assertEqual(len(blocked_tool_code), 5)

        candidates = matrix["technical_candidates"]
        ids = [row["capability_id"] for row in candidates]
        self.assertEqual(len(ids), len(set(ids)))
        for row in candidates:
            self.assertRegex(row["evidence_id"], r"^evidence:[0-9a-f]{20}$")
            self.assertEqual(row["required_assets"]["reference_selector"], row["selector"])
            self.assertEqual(row["cpu_conda_status"], "PENDING_EXTERNAL_CONDA_CI")
            expected_gpu_state = "PENDING_GPU_CONDA_CI" if row["tool"] in registry["gpu_tools"] else "NOT_APPLICABLE"
            self.assertEqual(row["gpu_conda_status"], expected_gpu_state)
            self.assertTrue(row["public_reproducibility"])
            self.assertEqual(row["distribution_approval_status"], "PENDING_OWNER_REVIEW")
            self.assertIn(row["code_distribution_status"], {"PUBLIC_BUNDLED", "BLOCKED"})
        prmeth = next(row for row in candidates if row["tool"] == "PRMeth")
        self.assertEqual(prmeth["code_distribution_status"], "BLOCKED")

        self.assertEqual(prmeth["public_reproducibility"], "BLOCKED_PENDING_LICENSE_OR_CAPABILITY_REVIEW")
        methylbert_cpu = next(
            row for row in candidates
            if row["scenario"] == "immune6" and row["route_id"] == "wgbs-native-hg19"
            and row["tool"] == "MethylBERT" and row["device"] == "cpu"
        )
        methylbert_gpu = next(
            row for row in candidates
            if row["scenario"] == "immune6" and row["route_id"] == "wgbs-native-hg19"
            and row["tool"] == "MethylBERT" and row["device"] == "gpu"
        )
        self.assertIn("MethylBERT-cpu", methylbert_cpu["required_assets"]["runtime_modules"])
        self.assertIn("MethylBERT-hg19-data", methylbert_cpu["required_assets"]["data_modules"])
        self.assertIn("MethylBERT-gpu", methylbert_gpu["required_assets"]["runtime_modules"])
        # The old WGBS-sourced array_epic_cpg bundle is not a native EPIC
        # selector or the explicit, build-aware derived projection route.
        self.assertFalse(any(
            row["source_platform"] == "wgbs" and row["analysis_contract"] == "array_epic_cpg"
            for row in candidates
        ))
        self.assertTrue(matrix["legacy_quarantined_candidates"])

        derived = [row for row in candidates if row["route_family"] in {"wgbs-to-epic", "wgbs-to-450k"}]
        self.assertTrue(derived)
        self.assertTrue(all(row["input_type"] in {"bed", "bam"} for row in derived))
        self.assertFalse(any(row["input_type"] == "pat" for row in derived))

        native = [row for row in candidates if row["route_family"] == "wgbs-native"]
        self.assertTrue(native)
        self.assertFalse(any(row["tool"] == "CelFiE" and row["input_type"] == "pat" for row in native))
        self.assertFalse(any(row["tool"] in {"CelFEER", "UXM", "MethylBERT"} and row["input_type"] == "bed" for row in native))
        self.assertTrue(all(row["conda_compatibility_status"] == "PENDING" for row in candidates))
        self.assertTrue(all(row["distribution_approval_status"] == "PENDING_OWNER_REVIEW" for row in candidates))

    def test_baseline_fixture_join_respects_input_route_not_reference_platform(self):
        from scripts.collect_baseline_candidates import fixture_reference_route_matches

        epic_fixture = {
            "scenario": "immune12", "platform": "epic", "route": "array-native",
            "genome_build": "not_applicable",
        }
        projected_epic_reference = {
            "scenario": "immune12", "source_platform": "epic",
            "analysis_contract": "wgbs_derived_epic_cpg_v1", "genome_build": "hg38",
        }
        self.assertFalse(fixture_reference_route_matches(epic_fixture, projected_epic_reference))

        epic_to_450k_reference = {
            "scenario": "immune12", "source_platform": "450k",
            "analysis_contract": "array_epic_from_450k_common_cpg_v1", "genome_build": "not_applicable",
        }
        self.assertTrue(fixture_reference_route_matches(epic_fixture, epic_to_450k_reference))
        self.assertFalse(fixture_reference_route_matches(
            epic_fixture, {**epic_to_450k_reference, "source_platform": "epic"}
        ))

        wgbs_fixture = {
            "scenario": "epithelial", "platform": "wgbs", "route": "wgbs-native", "genome_build": "hg19",
        }
        projected_450k_reference = {
            "scenario": "epithelial", "source_platform": "450k",
            "analysis_contract": "wgbs_derived_450k_cpg_v1", "genome_build": "hg19",
        }
        self.assertTrue(fixture_reference_route_matches(wgbs_fixture, projected_450k_reference))
        self.assertFalse(fixture_reference_route_matches(
            wgbs_fixture, {**projected_450k_reference, "source_platform": "epic"}
        ))

        wrong_build_reference = {**projected_450k_reference, "genome_build": "hg38"}
        self.assertFalse(fixture_reference_route_matches(wgbs_fixture, wrong_build_reference))

    def test_package_payload_audit_rejects_private_paths_and_build_bytecode(self):
        from scripts.audit_conda_payload import allowed_source_member, allowed_wheel_member, check_names, safe_archive_path, scan_private_paths
        from scripts.build_source_snapshot import files as source_allowlist_files
        self.assertTrue(check_names(["pkg/__pycache__/tool.cpython-312.pyc"]))
        hits = scan_private_paths([
            ("pkg/helper.py", b'ROOT = "' + b"/data/" + b"zhangmch" + b'/private-worktree"'),
            ("pkg/readme.txt", b"generic relative path only"),
        ])
        self.assertEqual(hits, ["pkg/helper.py"])
        self.assertTrue(safe_archive_path("methunmix-2.0.0rc1/src/module.py"))
        self.assertFalse(safe_archive_path("methunmix-2.0.0rc1/../../outside"))
        self.assertFalse(safe_archive_path(r"methunmix-2.0.0rc1\src\module.py"))
        self.assertTrue(allowed_source_member("methunmix-2.0.0rc1/src/methunmix_assets/nextflow_ref/tools/.gitkeep"))
        self.assertTrue(allowed_source_member("methunmix-2.0.0rc1/src/methunmix_assets/deconvolution/wgbs_scripts/uxm"))
        self.assertFalse(allowed_source_member("methunmix-2.0.0rc1/src/methunmix.egg-info/PKG-INFO"))
        self.assertTrue(allowed_wheel_member("demethflow_core/cli.py"))
        self.assertTrue(allowed_wheel_member("methunmix_assets/assets/catalog.json"))
        self.assertFalse(allowed_wheel_member("unexpected/payload.py"))
        self.assertFalse(allowed_wheel_member("methunmix_assets/runtimes/tool.sif"))
        self.assertTrue(check_names(["src/methunmix.egg-info/PKG-INFO"]))
        self.assertFalse(allowed_wheel_member("methunmix_assets/methunmix.egg-info/PKG-INFO"))
        self.assertFalse(any(any(part.endswith(".egg-info") for part in path.parts) for path in source_allowlist_files()))

    def test_wheel_normalization_removes_build_timestamp_variance(self):
        import zipfile
        from scripts.normalize_wheel import normalize_wheel

        with tempfile.TemporaryDirectory(prefix="methunmix-wheel-normalize-") as tmp:
            root = Path(tmp)
            archives = [root / "first.whl", root / "second.whl"]
            payloads = {
                "methunmix_core/__init__.py": b"VERSION = '2.0.0rc1'\n",
                "methunmix-2.0.0rc1.dist-info/RECORD": b"",
            }
            timestamps = ((2026, 9, 1, 1, 2, 4), (2026, 9, 13, 15, 30, 0))
            for archive_path, timestamp in zip(archives, timestamps):
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, content in payloads.items():
                        info = zipfile.ZipInfo(name, date_time=timestamp)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        archive.writestr(info, content)
            outputs = [root / "first.normalized.whl", root / "second.normalized.whl"]
            digests = [normalize_wheel(source, output) for source, output in zip(archives, outputs)]
            self.assertEqual(digests[0], digests[1])
            with zipfile.ZipFile(outputs[0]) as normalized:
                self.assertEqual(normalized.namelist(), sorted(payloads))
                self.assertTrue(all(normalized.getinfo(name).date_time == (1980, 1, 1, 0, 0, 0) for name in payloads))
                self.assertEqual({name: normalized.read(name) for name in payloads}, payloads)

    def test_workflow_execution_is_rejected_early_off_linux_x86_64(self):
        from demethflow_core import builder, runner, util
        from demethflow_core.builder import BuildRequest
        from demethflow_core.runner import DeconRequest

        with patch("demethflow_core.util.platform.system", return_value="Darwin"), patch(
            "demethflow_core.util.platform.machine", return_value="arm64"
        ):
            with self.assertRaisesRegex(DeMethFlowError, "WORKFLOW_PLATFORM_UNSUPPORTED"):
                util.require_linux_x86_64_workflow()
            with self.assertRaisesRegex(DeMethFlowError, "Linux x86_64"):
                runner.run_decon(DeconRequest(Path("input.bed"), Path("out"), "450k"))
            with self.assertRaisesRegex(DeMethFlowError, "Linux x86_64"):
                builder.run_build(BuildRequest(
                    reference_input=None, metadata=None, outdir=Path("out"),
                    scenario="immune6", platform="wgbs", reference_id="fixture",
                    reference_version="1.0.0",
                ))

        with patch("demethflow_core.util.platform.system", return_value="Linux"), patch(
            "demethflow_core.util.platform.machine", return_value="amd64"
        ):
            util.require_linux_x86_64_workflow()

    def test_gpu_static_audit_reports_lineage_without_claiming_runtime_pass(self):
        from scripts import audit_gpu_claims

        with tempfile.TemporaryDirectory(prefix="methunmix-gpu-audit-") as tmp:
            root = Path(tmp)
            selector = "builtin.fixture.wgbs@1.0.0"
            bundle = root / "references" / "builtin.fixture.wgbs" / "1.0.0"
            evidence_path = bundle / "validation" / "gpu.json"
            evidence_path.parent.mkdir(parents=True)
            evidence = {"status": "PASS", "reference": "builtin.fixture.wgbs@0.9.0"}
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            evidence_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            manifest = {
                "status": "PARTIAL",
                "artifact_checksums": {"validation/gpu.json": evidence_digest},
                "tool_capabilities": {
                    "MEnet": {
                        "status": "RELEASED_UNVALIDATED",
                        "artifacts": {"gpu_validation": "validation/gpu.json"},
                        "execution_profiles": {"gpu": {"status": "RELEASED_UNVALIDATED", "runtime_modules": ["MEnet"]}},
                    },
                },
            }
            manifest_path = bundle / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            matrix = {
                "schema": "methunmix-capability-matrix-v2",
                "technical_candidates": [{
                    "selector": selector, "tool": "MEnet", "device": "gpu",
                    "capability_id": "fixture.wgbs.MEnet.gpu",
                    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    "scientific_status": "RELEASED_UNVALIDATED",
                    "device_profile_status": "RELEASED_UNVALIDATED",
                }],
            }
            matrix_path = root / "matrix.json"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            with patch.object(audit_gpu_claims, "probe_gpu", return_value={"status": "NOT_AVAILABLE", "hardware_inference_run": False}):
                report = audit_gpu_claims.audit(matrix_path, root / "references")
            self.assertEqual(report["status"], "STATIC_HASHES_PASS_WITH_SELECTOR_REVIEW_RUNTIME_E2E_PENDING")
            self.assertEqual(report["summary"]["manifest_digest_mismatches"], 0)
            self.assertEqual(report["summary"]["evidence_hash_or_path_failures"], 0)
            self.assertEqual(report["summary"]["historical_selector_reports_without_explicit_byte_identity_binding"], 1)
            self.assertEqual(report["runtime_conda_e2e"], "PENDING")

    def test_source_snapshot_refuses_symlink_escape(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from scripts import build_source_snapshot

        with tempfile.TemporaryDirectory() as root_name, tempfile.TemporaryDirectory() as outside_name:
            root = Path(root_name)
            outside = Path(outside_name) / "external.py"
            outside.write_text("not part of the project", encoding="utf-8")
            (root / "README.md").write_text("fixture", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "escape.py").symlink_to(outside)
            with (
                patch.object(build_source_snapshot, "ROOT", root),
                patch.object(build_source_snapshot, "INCLUDE_FILES", {"README.md"}),
                patch.object(build_source_snapshot, "INCLUDE_DIRS", {"src"}),
                patch.object(build_source_snapshot, "EXCLUDE_PARTS", set()),
            ):
                with self.assertRaisesRegex(ValueError, "symlinked source allowlist member"):
                    build_source_snapshot.files()

    def test_matrix_preserves_ru_explicit_only_and_device_profile_boundaries(self):
        matrix_path = PROJECT_ROOT.parent.parent / "evidence" / "capability_matrix_rc.json"
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        candidates = matrix["technical_candidates"]
        medecom = [row for row in candidates if row["tool"] == "MeDeCom"]
        self.assertTrue(medecom)
        self.assertTrue(all(row["execution_policy"] == "explicit_only" for row in medecom))
        self.assertTrue(all(row["requires_explicit_tool_selection"] for row in medecom))
        methylbert = [row for row in candidates if row["tool"] == "MethylBERT"]
        self.assertTrue({row["device"] for row in methylbert} >= {"cpu", "gpu"})
        self.assertFalse(any(row["device_profile_status"] == "UNVALIDATED" for row in methylbert))
        self.assertTrue(any(row["reason_code"] == "DEVICE_PROFILE_UNVALIDATED" for row in matrix["blocked_candidates"]))
        self.assertEqual(matrix["platform_support"]["package_platform_support"], ["noarch"])
        self.assertEqual(matrix["platform_support"]["workflow_execution_platform_support"], ["linux-64"])
        self.assertTrue(all(row["warning_required"] for row in medecom if row["scientific_status"] == "RELEASED_UNVALIDATED"))
        self.assertTrue(all(row["tools_all_policy"] == "EXCLUDE_EXPLICIT_ONLY" for row in medecom))
        self.assertTrue(all(row["tools_all_policy"] == "INCLUDE" for row in methylbert if not row["requires_explicit_route_opt_in"]))
        projected = [row for row in candidates if row["requires_explicit_route_opt_in"]]
        self.assertTrue(projected)
        self.assertTrue(all(row["tools_all_policy"] == "INCLUDE_AFTER_ROUTE_OPT_IN" for row in projected if row["execution_policy"] == "normal"))
        self.assertTrue(all(row["route_disclosures"] == ["EXPLICIT_ROUTE_OPT_IN_REQUIRED"] for row in projected))
        self.assertTrue(all(row["warning_required"] == bool(row["release_disclosures"]) for row in candidates))

    def test_ru_disclosure_and_tools_all_policy_are_not_science_promotion(self):
        from types import SimpleNamespace
        from demethflow_core.runtime import release_disclosures_for_tools, release_warning_for_tools

        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            ru_warning = "MEnet is RELEASED_UNVALIDATED: scientific gates not met."
            menet = SimpleNamespace(
                status="RELEASED_UNVALIDATED",
                execution_policy="normal",
                effective_status=lambda _accelerator: ("RELEASED_UNVALIDATED", "overall_pearson"),
                artifacts={},
                validation_policy={},
                validation_scope="fixed_truth_simulation",
                reason="overall_pearson",
            )
            medecom = SimpleNamespace(
                status="READY",
                execution_policy="explicit_only",
                effective_status=lambda _accelerator: ("READY", ""),
                artifacts={},
                validation_policy={},
                validation_scope="reference_free",
                reason="",
            )
            bundle = SimpleNamespace(
                payload={
                    "release_validation": {
                        "tool_warnings": {"MEnet": ru_warning},
                        "tool_validation_evidence": {},
                    },
                    "tool_capabilities": {
                        "MEnet": {"execution_profiles": {"gpu": {"status": "RELEASED_UNVALIDATED"}}},
                        "MeDeCom": {"execution_profiles": {"cpu": {"status": "READY"}}},
                    },
                },
                tools={"MEnet": menet, "MeDeCom": medecom},
                root=Path(tmp),
                selector="fixture@1.0.0",
                scenario_id="fixture",
            )
            disclosures = release_disclosures_for_tools(bundle, ["MEnet", "MeDeCom"], accelerator="gpu")
            self.assertEqual(disclosures["MEnet"]["status"], "RELEASED_UNVALIDATED")
            self.assertEqual(disclosures["MEnet"]["scientific_status"], "NOT_PROMOTED")
            self.assertTrue(disclosures["MEnet"]["allow_tools_all"])
            self.assertEqual(disclosures["MEnet"]["warning"], ru_warning)
            self.assertFalse(disclosures["MeDeCom"]["allow_tools_all"])
            self.assertIsNone(disclosures["MeDeCom"]["warning"])
            self.assertEqual(release_warning_for_tools({"tool_warnings": {"MEnet": ru_warning}}, ["MEnet"]), ru_warning)

    def test_catalog_rejects_duplicate_or_incomplete_public_target(self):
        import demethflow_core.asset_manager as assets
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text('{"schema":"wrong","schema":"methunmix-static-catalog-v1"}', encoding="utf-8")
            with self.assertRaisesRegex(DeMethFlowError, "duplicate JSON object key"):
                assets.load_catalog(path)
            path.write_text("{}", encoding="utf-8")
            with patch.object(assets, "MAX_CATALOG_BYTES", 1), self.assertRaisesRegex(
                DeMethFlowError, "exceeds the 8 MiB metadata size limit"
            ):
                assets.load_catalog(path)
            target = {"asset_id": "fixture", "distribution_status": "PUBLIC_DOWNLOADABLE", "sha256": "a" * 64}
            path.write_text(json.dumps({"schema": assets.CATALOG_SCHEMA, "targets": [target]}), encoding="utf-8")
            with self.assertRaises(DeMethFlowError):
                assets.load_catalog(path)
            target["length"] = 1
            path.write_text(json.dumps({"schema": assets.CATALOG_SCHEMA, "targets": [target, target]}), encoding="utf-8")
            with self.assertRaises(DeMethFlowError):
                assets.load_catalog(path)

    def test_empty_catalog_requires_explicit_user_supplied_fallback(self):
        import demethflow_core.asset_manager as assets
        valid = {
            "schema": assets.CATALOG_SCHEMA,
            "product": "MethUnmix",
            "catalog_release": "rc-test",
            "catalog_sequence": 1,
            "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
            "targets": [],
            "empty_catalog_policy": {
                "allowed": True,
                "fallback": "USER_SUPPLIED_SUPPORTED_OR_BLOCKED",
                "reason": "No approved public assets; compatible user-owned modules may be imported.",
            },
        }
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(valid), encoding="utf-8")
            self.assertEqual(assets.load_catalog(path)["targets"], [])
            invalid = dict(valid)
            invalid.pop("empty_catalog_policy")
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(DeMethFlowError, "empty_catalog_policy"):
                assets.load_catalog(path)

    def test_absolute_home_is_supported_and_relative_home_rejected(self):
        from demethflow_core.cli import main
        previous = os.environ.get("METHUNMIX_HOME")
        try:
            self.assertEqual(main(["--home", "/tmp/methunmix-test-home", "asset", "list"]), 0)
            self.assertEqual(os.environ.get("METHUNMIX_HOME"), previous)
            self.assertEqual(main(["--home", "relative-store", "asset", "list"]), 2)
        finally:
            if previous is None:
                os.environ.pop("METHUNMIX_HOME", None)
            else:
                os.environ["METHUNMIX_HOME"] = previous

    def test_legacy_cli_warning_is_once_per_process_and_suppressible(self):
        import demethflow_core.legacy as legacy
        original = legacy._WARNING_EMITTED
        try:
            legacy._WARNING_EMITTED = False
            captured = StringIO()
            with patch.object(legacy, "_main", return_value=0), redirect_stderr(captured):
                legacy.main(["--version"])
                legacy.main(["--version"])
            self.assertEqual(captured.getvalue().count("WARNING: demethflow"), 1)

            legacy._WARNING_EMITTED = False
            captured = StringIO()
            with patch.dict(os.environ, {"METHUNMIX_SILENCE_LEGACY_WARNING": "1"}), \
                 patch.object(legacy, "_main", return_value=0), redirect_stderr(captured):
                legacy.main(["--version"])
            self.assertEqual(captured.getvalue(), "")
        finally:
            legacy._WARNING_EMITTED = original

    def test_legacy_cli_forwards_arguments_streams_and_exit_status(self):
        import demethflow_core.legacy as legacy
        original = legacy._WARNING_EMITTED
        args = ["run", "--platform", "450k", "--input", "input.tsv", "--outdir", "result"]
        observed: dict[str, object] = {}

        def fake_main(argv):
            observed["argv"] = argv
            print("canonical stdout")
            print("canonical stderr", file=sys.stderr)
            return 17

        try:
            legacy._WARNING_EMITTED = False
            stdout = StringIO()
            stderr = StringIO()
            with patch.object(legacy, "_main", side_effect=fake_main), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                result = legacy.main(args)
            self.assertEqual(observed["argv"], args)
            self.assertEqual(result, 17)
            self.assertEqual(stdout.getvalue(), "canonical stdout\n")
            self.assertIn("canonical stderr\n", stderr.getvalue())
            self.assertEqual(stderr.getvalue().count("WARNING: demethflow"), 1)
        finally:
            legacy._WARNING_EMITTED = original

    def test_slurm_is_out_of_scope_and_blocked_before_execution(self):
        from demethflow_core.cli import build_parser
        from demethflow_core.util import require_local_executor

        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args([
                "run", "--platform", "450k", "--outdir", "results", "--executor", "slurm"
            ])
        self.assertEqual(raised.exception.code, 2)
        with self.assertRaisesRegex(DeMethFlowError, "EXECUTOR_UNSUPPORTED.*Slurm is out of scope"):
            require_local_executor("slurm")
        require_local_executor("local")

        nextflow_config = (PROJECT_ROOT / "nextflow_ref" / "nextflow.config").read_text(encoding="utf-8")
        launcher = (PROJECT_ROOT / "nextflow_ref" / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("slurm {", nextflow_config)
        self.assertIn('PROFILE="${NEXTFLOW_PROFILE:-standard,conda}"', launcher)

    def test_relative_environment_store_is_rejected(self):
        from demethflow_core.util import default_data_home
        previous = os.environ.get("METHUNMIX_HOME")
        try:
            os.environ["METHUNMIX_HOME"] = "relative-store"
            with self.assertRaises(DeMethFlowError):
                default_data_home()
        finally:
            if previous is None:
                os.environ.pop("METHUNMIX_HOME", None)
            else:
                os.environ["METHUNMIX_HOME"] = previous

    def test_home_precedence_legacy_alias_and_unicode_paths(self):
        from demethflow_core.util import default_data_home
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            root = Path(tmp)
            new_path = root / "新目录 with spaces"
            old_path = root / "legacy store"
            xdg = root / "xdg data"
            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(new_path),
                "DEMETHFLOW_HOME": str(old_path),
                "XDG_DATA_HOME": str(xdg),
            }, clear=True):
                with self.assertRaisesRegex(DeMethFlowError, "point to different stores"):
                    default_data_home()
            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(new_path),
                "DEMETHFLOW_HOME": str(new_path),
            }, clear=True):
                self.assertEqual(default_data_home(), new_path.resolve())
            with patch.dict(os.environ, {"DEMETHFLOW_HOME": str(old_path)}, clear=True):
                self.assertEqual(default_data_home(), old_path.resolve())
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg)}, clear=True):
                self.assertEqual(default_data_home(), (xdg / "methunmix").resolve())

    def test_explicit_cli_home_overrides_conflicting_env_without_mutating_it(self):
        from demethflow_core.cli import main
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            selected = str(Path(tmp) / "selected store")
            env = {
                "METHUNMIX_HOME": str(Path(tmp) / "new store"),
                "DEMETHFLOW_HOME": str(Path(tmp) / "old store"),
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(main(["--home", selected, "asset", "list"]), 0)
                self.assertEqual(os.environ["METHUNMIX_HOME"], env["METHUNMIX_HOME"])
                self.assertEqual(os.environ["DEMETHFLOW_HOME"], env["DEMETHFLOW_HOME"])

    def test_help_and_version_do_not_validate_store_environment(self):
        from demethflow_core.cli import main
        for argv in (["--help"], ["--version"]):
            output = StringIO()
            with patch.dict(os.environ, {"METHUNMIX_HOME": "relative-store"}, clear=True), \
                 redirect_stderr(output), redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    main(list(argv))
            self.assertEqual(raised.exception.code, 0)
            self.assertNotIn("must be an absolute path", output.getvalue())

    def test_implicit_store_is_private_but_explicit_store_is_not_chmodded(self):
        import stat
        from demethflow_core.util import prepare_data_home
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            root = Path(tmp)
            xdg = root / "xdg"
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg)}, clear=True):
                private = prepare_data_home()
                self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)

            shared = root / "shared store"
            shared.mkdir()
            shared.chmod(0o755)
            with patch.dict(os.environ, {"METHUNMIX_HOME": str(shared)}, clear=True):
                self.assertEqual(prepare_data_home(), shared.resolve())
            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)

    def test_configured_store_rejects_symlink_components_before_creation(self):
        from demethflow_core.util import prepare_data_home
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            link = root / "linked-store"
            link.symlink_to(outside, target_is_directory=True)
            with patch.dict(os.environ, {"METHUNMIX_HOME": str(link / "new-store")}, clear=True):
                with self.assertRaisesRegex(DeMethFlowError, "symlink in MethUnmix managed path"):
                    prepare_data_home()
            with patch.dict(os.environ, {"METHUNMIX_HOME": str(link / ".." / "dotdot-store")}, clear=True):
                with self.assertRaisesRegex(DeMethFlowError, "must not contain '..'"):
                    prepare_data_home()
            self.assertEqual(list(outside.iterdir()), [])

    def test_safe_module_import_and_verify(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            root = Path(tmp) / "home"
            archive = Path(tmp) / "module.tar.gz"
            payload = Path(tmp) / "payload"
            payload.mkdir()
            (payload / "hello.txt").write_text("hello\n", encoding="utf-8")
            digest = hashlib.sha256((payload / "hello.txt").read_bytes()).hexdigest()
            manifest = {
                "schema": "demethflow-module-v2",
                "module_id": "fixture",
                "version": "1.0.0",
                "module_type": "tool-data",
                "platform": "linux-x86_64",
                "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
                "files": [{"path": "hello.txt", "sha256": digest, "bytes": 6}],
                "exports": {"hello": "hello.txt"},
            }
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo("module.json")
                raw = json.dumps(manifest).encode()
                info.size = len(raw)
                import io
                handle.addfile(info, io.BytesIO(raw))
                handle.add(payload / "hello.txt", arcname="payload/hello.txt")
            archive.with_name(archive.name + ".sha256").write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n", encoding="utf-8"
            )
            module, changed = ModuleStore(root).install(archive)
            self.assertTrue(changed)
            self.assertEqual(ModuleStore(root).verify(module), [])
            (module.install_root / "unexpected.txt").write_text("tampered\n", encoding="utf-8")
            self.assertIn("undeclared: unexpected.txt", ModuleStore(root).verify(module))

    def test_reference_export_is_noarch_and_importable_on_non_linux_host(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            root = Path(tmp)
            bundle_root = root / "reference"
            bundle_root.mkdir()
            (bundle_root / "manifest.json").write_text("{}\n", encoding="utf-8")
            (bundle_root / "cell_types.tsv").write_text("cell_type\nBcell\n", encoding="utf-8")
            bundle = ReferenceBundle(
                manifest_path=bundle_root / "manifest.json",
                payload={"reference_id": "fixture-reference", "version": "1.0.0"},
                tools={},
            )
            archive, _sidecar = export_reference(bundle, root / "exports")
            with tarfile.open(archive, "r:gz") as handle:
                module_manifest = json.load(handle.extractfile("module.json"))
            self.assertEqual(module_manifest["platform"], "noarch")

            with patch("demethflow_core.modules.platform.system", return_value="Darwin"):
                with patch("demethflow_core.modules.platform.machine", return_value="arm64"):
                    installed, changed = ModuleStore(root / "darwin-home").install(archive)
            self.assertTrue(changed)
            self.assertEqual(ModuleStore(root / "darwin-home").verify(installed), [])

    def test_release_module_architecture_matches_asset_vs_runtime_role(self):
        from demethflow_core.release import _build_module_archive

        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            root = Path(tmp)
            source = root / "asset.txt"
            source.write_text("fixture\n", encoding="utf-8")
            for module_type, expected in (
                ("tool-data", "noarch"),
                ("build-assets", "noarch"),
                ("runtime", "linux-x86_64"),
                ("build-runtime", "linux-x86_64"),
            ):
                archive = root / f"{module_type}.tar.gz"
                _build_module_archive(
                    archive,
                    module_id=f"module-{module_type}",
                    module_type=module_type,
                    module_version="1.0.0",
                    sources=[(source, "fixture.txt")],
                )
                with tarfile.open(archive, "r:gz") as handle:
                    manifest = json.load(handle.extractfile("module.json"))
                self.assertEqual(manifest["platform"], expected, module_type)

    def test_linux_runtime_module_is_rejected_on_non_linux_host(self):
        from demethflow_core.modules import _validate_module_payload

        manifest = {
            "schema": "demethflow-module-v2",
            "module_id": "runtime",
            "version": "1.0.0",
            "module_type": "runtime",
            "platform": "linux-x86_64",
            "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
            "files": [],
        }
        with patch("demethflow_core.modules.platform.system", return_value="Darwin"):
            with patch("demethflow_core.modules.platform.machine", return_value="arm64"):
                with self.assertRaisesRegex(InstallError, "incompatible with host"):
                    _validate_module_payload(manifest)
                manifest["platform"] = "noarch"
                with self.assertRaisesRegex(InstallError, "cannot be declared noarch"):
                    _validate_module_payload(manifest)
                manifest.pop("platform")
                with self.assertRaisesRegex(InstallError, "platform declaration is required"):
                    _validate_module_payload(manifest)

    def test_module_store_lock_recovers_only_provably_stale_local_owner(self):
        import socket
        from demethflow_core.modules import _store_lock
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            lock = Path(tmp) / "install.lock"
            lock.mkdir()
            (lock / "owner.json").write_text(json.dumps({
                "pid": 2_147_483_000,
                "host": socket.gethostname(),
                "created_at": 1,
                "operation": "interrupted-test-install",
                "stale_timeout_seconds": 21600,
            }), encoding="utf-8")
            with _store_lock(lock, operation="test-recovery"):
                owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
                self.assertEqual(owner["operation"], "test-recovery")
                self.assertEqual(owner["pid"], os.getpid())
                self.assertEqual(owner["host"], socket.gethostname())
            self.assertFalse(lock.exists())

    def test_module_store_lock_does_not_steal_live_or_remote_lock(self):
        import socket
        from demethflow_core.modules import _store_lock
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            for owner in (
                {
                    "pid": os.getpid(), "host": socket.gethostname(), "created_at": 1,
                    "operation": "active", "stale_timeout_seconds": 21600,
                },
                {
                    "pid": 2_147_483_000, "host": "another-host", "created_at": 1,
                    "operation": "remote", "stale_timeout_seconds": 21600,
                },
            ):
                lock = Path(tmp) / f"lock-{owner['operation']}"
                lock.mkdir()
                (lock / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
                with self.assertRaises(InstallError):
                    with _store_lock(lock):
                        pass
                self.assertTrue(lock.exists())

    def test_store_mutations_fail_fast_while_another_operation_holds_store_lock(self):
        from demethflow_core.asset_manager import audit_online, fetch_asset
        from demethflow_core.modules import _store_lock
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            home = Path(tmp) / "home"
            state = home / ".methunmix"
            (state / "downloads").mkdir(parents=True)
            with patch.dict(os.environ, {"METHUNMIX_HOME": str(home)}, clear=True), \
                 _store_lock(state / "store.lock", operation="held-test-operation"), \
                 patch("demethflow_core.asset_manager.urllib.request.urlopen") as urlopen:
                with self.assertRaises(InstallError):
                    fetch_asset("anything")
                with self.assertRaises(InstallError):
                    audit_online("https://assets.example/catalogs/release/catalog.json")
                with self.assertRaises(InstallError):
                    gc_assets(dry_run=False)
                urlopen.assert_not_called()

    def test_module_store_refuses_symlinked_managed_directories(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            root = Path(tmp) / "home"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            state = root / ".methunmix"
            state.mkdir()
            (state / "staging").symlink_to(outside, target_is_directory=True)
            archive = Path(tmp) / "not-used.tar.gz"
            archive.write_bytes(b"fixture")
            archive.with_name(archive.name + ".sha256").write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest() + "\n", encoding="utf-8"
            )
            with self.assertRaises(InstallError):
                ModuleStore(root).install(archive)
            self.assertEqual(list(outside.iterdir()), [])

    def test_module_store_refuses_symlinked_root_before_creating_store(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            link = root / "linked-home"
            link.symlink_to(outside, target_is_directory=True)
            archive = root / "not-used.tar.gz"
            archive.write_bytes(b"fixture")
            archive.with_name(archive.name + ".sha256").write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest() + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(InstallError, "symlink in MethUnmix managed path"):
                ModuleStore(link / "nested-home").install(archive)
            with self.assertRaisesRegex(InstallError, "must not contain '..'"):
                ModuleStore(link / ".." / "dotdot-home").install(archive)
            self.assertEqual(list(outside.iterdir()), [])

    def test_unsafe_archive_member_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            archive = Path(tmp) / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo("../escape.txt")
                info.size = 1
                import io
                handle.addfile(info, io.BytesIO(b"x"))
            archive.with_name(archive.name + ".sha256").write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest() + "\n", encoding="utf-8"
            )
            with self.assertRaises(InstallError):
                ModuleStore(Path(tmp) / "home").install(archive)

    def test_module_import_rejects_symlinked_archive_path(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            root = Path(tmp)
            target = root / "real.tar.gz"
            target.write_bytes(b"not a module archive")
            target.with_name(target.name + ".sha256").write_text(
                hashlib.sha256(target.read_bytes()).hexdigest() + "\n", encoding="utf-8"
            )
            link = root / "linked.tar.gz"
            link.symlink_to(target)
            with self.assertRaisesRegex(InstallError, "symlink in MethUnmix managed path"):
                ModuleStore(root / "home").install(link)

    def test_archive_case_collisions_are_rejected_before_extraction(self):
        import io
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            archive = Path(tmp) / "case-collision.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                for name, data in (
                    ("module.json", b"{}"),
                    ("payload/Marker.tsv", b"A"),
                    ("payload/marker.tsv", b"B"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    handle.addfile(info, io.BytesIO(data))
            archive.with_name(archive.name + ".sha256").write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest() + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(InstallError, "Duplicate archive path"):
                ModuleStore(Path(tmp) / "home").install(archive)

    def test_archive_rejects_unexpected_root_payload(self):
        import io
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            archive = Path(tmp) / "unexpected-root.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                for name, data in (("module.json", b"{}"), ("payload/file.txt", b"ok"), ("extra.txt", b"not part of the module")):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    handle.addfile(info, io.BytesIO(data))
            archive.with_name(archive.name + ".sha256").write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest() + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(InstallError, "Unexpected path outside"):
                ModuleStore(Path(tmp) / "home").install(archive)

    def test_special_file_archive_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            archive = Path(tmp) / "special.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo("payload/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                handle.addfile(info)
            archive.with_name(archive.name + ".sha256").write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest() + "\n", encoding="utf-8"
            )
            with self.assertRaises(InstallError):
                ModuleStore(Path(tmp) / "home").install(archive)

    def test_module_paths_reject_windows_traversal_and_cross_platform_collisions(self):
        from demethflow_core.modules import _safe_member_path, _validate_module_payload

        for unsafe in (
            r"payload\..\escape.txt", r"C:\outside\payload", "payload/CON.txt",
            "payload/NUL", "payload/a.", "payload/a ", "payload//file.txt",
            "payload/./file.txt", "payload:a.txt", "payload/cafe\u0301.tsv",
        ):
            with self.subTest(path=unsafe), self.assertRaises(InstallError):
                _safe_member_path(unsafe)

        base = {
            "schema": "demethflow-module-v2",
            "module_id": "fixture",
            "version": "1.0.0",
            "module_type": "tool-data",
            "platform": "noarch",
            "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
            "exports": {},
        }
        for paths in (
            ["payload/Marker.tsv", "payload/marker.tsv"],
            ["payload/reference", "payload/reference/cpg.tsv"],
            ["payload/reference/cpg.tsv", "payload/reference"],
        ):
            payload = dict(base)
            payload["files"] = [{"path": path, "sha256": "0" * 64} for path in paths]
            with self.subTest(paths=paths), self.assertRaises(InstallError):
                _validate_module_payload(payload)

    def test_static_catalog_audit_requires_https_and_versioned_path(self):
        from demethflow_core.asset_manager import audit_online

        with self.assertRaises(DeMethFlowError):
            audit_online("http://assets.example/catalogs/2.0.0rc1-static.1/catalog.json")

        catalog = {
            "schema": "methunmix-static-catalog-v1", "product": "MethUnmix",
            "catalog_release": "2.0.0rc1-static.1", "catalog_sequence": 1,
            "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [],
            "empty_catalog_policy": {"allowed": True, "fallback": "USER_SUPPLIED_SUPPORTED_OR_BLOCKED", "reason": "No public targets."},
        }
        raw = json.dumps(catalog).encode()

        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def geturl(self): return "https://assets.example/catalogs/2.0.0rc1-static.1/catalog.json"
            def read(self, _size=-1): return raw

        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp, \
             patch.dict(os.environ, {"METHUNMIX_HOME": str(Path(tmp) / "home")}), \
             patch("demethflow_core.asset_manager.urllib.request.urlopen", return_value=Response()):
            self.assertEqual(audit_online(
                "https://assets.example/catalogs/2.0.0rc1-static.1/catalog.json",
                metadata_dir=Path(tmp) / "catalog-cache",
            ), 0)
            state = json.loads((Path(tmp) / "catalog-cache" / "audit_state.json").read_text())
            self.assertEqual(state["catalog_sequence"], 1)
            self.assertEqual(state["metadata_signature"], "NOT_USED_TUF_DEFERRED")

    def test_static_catalog_blocks_rewrite_at_same_sequence_and_rollback(self):
        from demethflow_core.asset_manager import audit_online

        def response_for(payload, release):
            raw = json.dumps(payload).encode()
            class Response:
                headers = {}
                def __enter__(self): return self
                def __exit__(self, *_args): return False
                def geturl(self): return f"https://assets.example/catalogs/{release}/catalog.json"
                def read(self, _size=-1): return raw
            return Response()

        def catalog(sequence, release, note):
            return {
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix",
                "catalog_release": release, "catalog_sequence": sequence,
                "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [], "notes": [note],
                "empty_catalog_policy": {"allowed": True, "fallback": "USER_SUPPLIED_SUPPORTED_OR_BLOCKED", "reason": "No public targets."},
            }

        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp, \
             patch.dict(os.environ, {"METHUNMIX_HOME": str(Path(tmp) / "home")}):
            cache = Path(tmp) / "catalog-cache"
            first = catalog(2, "release-2", "first")
            with patch("demethflow_core.asset_manager.urllib.request.urlopen", return_value=response_for(first, "release-2")):
                audit_online("https://assets.example/catalogs/release-2/catalog.json", metadata_dir=cache)
            rewritten = catalog(2, "release-2", "rewritten")
            with patch("demethflow_core.asset_manager.urllib.request.urlopen", return_value=response_for(rewritten, "release-2")):
                with self.assertRaises(DeMethFlowError):
                    audit_online("https://assets.example/catalogs/release-2/catalog.json", metadata_dir=cache)
            older = catalog(1, "release-1", "older")
            with patch("demethflow_core.asset_manager.urllib.request.urlopen", return_value=response_for(older, "release-1")):
                with self.assertRaises(DeMethFlowError):
                    audit_online("https://assets.example/catalogs/release-1/catalog.json", metadata_dir=cache)

    def test_public_download_target_requires_https_hash_path_and_license_approval(self):
        import demethflow_core.asset_manager as assets
        digest = "a" * 64
        target = {
            "asset_id": "fixture", "version": "1.0.0", "distribution_status": "PUBLIC_DOWNLOADABLE",
            "url": f"http://assets.example/{digest}/fixture.tar.gz", "sha256": digest, "length": 1,
            "immutable_object": True, "license_status": "LICENSE_APPROVED", "license_evidence": "review-123",
        }
        catalog = {
            "schema": assets.CATALOG_SCHEMA, "product": "MethUnmix", "catalog_release": "test-release",
            "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [target],
        }
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaises(DeMethFlowError):
                assets.load_catalog(path)
            target["url"] = f"https://assets.example/objects/{digest}/fixture.tar.gz"
            target["license_evidence"] = "review-123"
            for invalid_length in (True, 1.0, "1"):
                target["length"] = invalid_length
                path.write_text(json.dumps(catalog), encoding="utf-8")
                with self.assertRaises(DeMethFlowError):
                    assets.load_catalog(path)
            target["length"] = 1
            target.pop("license_evidence")
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaises(DeMethFlowError):
                assets.load_catalog(path)

    def test_static_catalog_fetch_verifies_exact_length_and_sha256(self):
        from demethflow_core.asset_manager import fetch_asset
        import demethflow_core.asset_manager as assets

        payload = b"signedness-is-not-claimed; digest-is-verified\n"
        digest = hashlib.sha256(payload).hexdigest()
        target = {
            "asset_id": "fixture", "version": "1.0.0", "distribution_status": "PUBLIC_DOWNLOADABLE",
            "url": f"https://assets.example/objects/{digest}/fixture.tar.gz", "sha256": digest,
            "length": len(payload), "immutable_object": True,
            "license_status": "LICENSE_APPROVED", "license_evidence": "owner-review-id",
        }
        catalog = {
            "schema": assets.CATALOG_SCHEMA, "product": "MethUnmix", "catalog_release": "fetch-test",
            "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [target],
        }

        class Response:
            headers = {"Content-Length": str(len(payload))}
            def __init__(self): self.sent = False
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def geturl(self): return target["url"]
            def read(self, _size=-1):
                if self.sent: return b""
                self.sent = True
                return payload

        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            catalog_path = Path(tmp) / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            home = Path(tmp) / "home"
            downloads = home / ".methunmix" / "downloads"
            downloads.mkdir(parents=True)
            sentinel = Path(tmp) / "outside-sentinel.bin"
            sentinel.write_bytes(b"must remain unchanged")
            legacy_part = downloads / f".{digest}.part"
            legacy_part.symlink_to(sentinel)
            with patch.dict(os.environ, {"METHUNMIX_HOME": str(Path(tmp) / "home"), "METHUNMIX_CATALOG_PATH": str(catalog_path)}), \
                 patch("demethflow_core.asset_manager.urllib.request.urlopen", return_value=Response()):
                self.assertEqual(fetch_asset("fixture"), 0)
                fetched = Path(tmp) / "home" / ".methunmix" / "downloads" / digest
                self.assertEqual(fetched.read_bytes(), payload)
                self.assertEqual(sentinel.read_bytes(), b"must remain unchanged")
                self.assertTrue(legacy_part.is_symlink())
                fetched.unlink()
                fetched.symlink_to(sentinel)
                with patch("demethflow_core.asset_manager.urllib.request.urlopen") as urlopen:
                    with self.assertRaises(DeMethFlowError):
                        fetch_asset("fixture")
                    urlopen.assert_not_called()
                self.assertEqual(sentinel.read_bytes(), b"must remain unchanged")

    def test_static_catalog_fetch_falls_back_to_declared_mirror(self):
        import urllib.error
        from demethflow_core.asset_manager import fetch_asset
        import demethflow_core.asset_manager as assets

        payload = b"mirror fallback fixture"
        digest = hashlib.sha256(payload).hexdigest()
        mirror = f"https://mirror.example/objects/{digest}/fixture.tar.gz"
        target = {
            "asset_id": "fixture", "version": "1.0.0", "distribution_status": "PUBLIC_DOWNLOADABLE",
            "url": f"https://primary.example/objects/{digest}/fixture.tar.gz", "mirrors": [mirror],
            "sha256": digest, "length": len(payload), "immutable_object": True,
            "license_status": "LICENSE_APPROVED", "license_evidence": "owner-review-id",
        }
        catalog = {
            "schema": assets.CATALOG_SCHEMA, "product": "MethUnmix", "catalog_release": "fetch-mirror-test",
            "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [target],
        }

        class Response:
            headers = {"Content-Length": str(len(payload))}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def geturl(self): return mirror
            def read(self, _size=-1):
                if hasattr(self, "sent"):
                    return b""
                self.sent = True
                return payload

        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            catalog_path = Path(tmp) / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with patch.dict(os.environ, {"METHUNMIX_HOME": str(Path(tmp) / "home"), "METHUNMIX_CATALOG_PATH": str(catalog_path)}), \
                 patch("demethflow_core.asset_manager.urllib.request.urlopen", side_effect=[urllib.error.URLError("primary unavailable"), Response()]) as urlopen:
                self.assertEqual(fetch_asset("fixture"), 0)
                self.assertEqual(urlopen.call_count, 2)
                installed = Path(tmp) / "home" / ".methunmix" / "downloads" / digest
                self.assertEqual(installed.read_bytes(), payload)

    def test_static_catalog_fetch_rejects_content_hash_mismatch(self):
        from demethflow_core.asset_manager import fetch_asset
        import demethflow_core.asset_manager as assets

        payload = b"tampered payload\n"
        expected = "a" * 64
        target = {
            "asset_id": "fixture", "version": "1.0.0", "distribution_status": "PUBLIC_DOWNLOADABLE",
            "url": f"https://assets.example/objects/{expected}/fixture.tar.gz", "sha256": expected,
            "length": len(payload), "immutable_object": True,
            "license_status": "LICENSE_APPROVED", "license_evidence": "owner-review-id",
        }
        catalog = {
            "schema": assets.CATALOG_SCHEMA, "product": "MethUnmix", "catalog_release": "fetch-test",
            "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [target],
        }

        class Response:
            headers = {"Content-Length": str(len(payload))}
            def __init__(self): self.sent = False
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def geturl(self): return target["url"]
            def read(self, _size=-1):
                if self.sent: return b""
                self.sent = True
                return payload

        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            catalog_path = Path(tmp) / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with patch.dict(os.environ, {"METHUNMIX_HOME": str(Path(tmp) / "home"), "METHUNMIX_CATALOG_PATH": str(catalog_path)}), \
                 patch("demethflow_core.asset_manager.urllib.request.urlopen", return_value=Response()):
                with self.assertRaises(DeMethFlowError):
                    fetch_asset("fixture")
                self.assertFalse((Path(tmp) / "home" / ".methunmix" / "downloads" / f".{expected}.part").exists())

    def test_static_catalog_fetch_rejects_revoked_target_before_network(self):
        from demethflow_core.asset_manager import fetch_asset
        import demethflow_core.asset_manager as assets

        digest = "c" * 64
        target = {
            "asset_id": "fixture", "version": "1.0.0", "distribution_status": "PUBLIC_DOWNLOADABLE",
            "url": f"https://assets.example/objects/{digest}/fixture.tar.gz", "sha256": digest,
            "length": 1, "immutable_object": True,
            "license_status": "LICENSE_APPROVED", "license_evidence": "owner-review-id",
            "revoked": True, "revocation_reason": "security withdrawal test",
        }
        catalog = {
            "schema": assets.CATALOG_SCHEMA, "product": "MethUnmix", "catalog_release": "revocation-test",
            "catalog_sequence": 2, "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [target],
        }

        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            catalog_path = Path(tmp) / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(Path(tmp) / "home"),
                "METHUNMIX_CATALOG_PATH": str(catalog_path),
            }), patch("demethflow_core.asset_manager.urllib.request.urlopen") as urlopen:
                with self.assertRaisesRegex(DeMethFlowError, "Asset is revoked: fixture"):
                    fetch_asset("fixture")
                urlopen.assert_not_called()

    def test_gc_protects_installed_and_incomplete_downloads(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-test-") as tmp:
            old_home = os.environ.get("METHUNMIX_HOME")
            try:
                os.environ["METHUNMIX_HOME"] = tmp
                downloads = Path(tmp) / ".methunmix" / "downloads"
                downloads.mkdir(parents=True)
                protected = "b" * 64
                (downloads / protected).write_bytes(b"protected")
                (downloads / ".incomplete.part").write_bytes(b"partial")
                unreferenced = "c" * 64
                (downloads / unreferenced).write_bytes(b"remove")
                (downloads / "user-note.txt").write_text("keep", encoding="utf-8")
                metadata = Path(tmp) / ".methunmix" / "modules"
                metadata.mkdir(parents=True)
                (metadata / "fixture@1.0.0.json").write_text(json.dumps({
                    "module_id": "fixture", "version": "1.0.0", "archive_sha256": protected,
                    "installed_path": str(Path(tmp) / "modules" / "fixture"),
                    "files": [],
                }), encoding="utf-8")
                self.assertEqual(gc_assets(dry_run=False), 0)
                self.assertTrue((downloads / protected).exists())
                self.assertTrue((downloads / ".incomplete.part").exists())
                self.assertFalse((downloads / unreferenced).exists())
                self.assertTrue((downloads / "user-note.txt").exists())
            finally:
                if old_home is None:
                    os.environ.pop("METHUNMIX_HOME", None)
                else:
                    os.environ["METHUNMIX_HOME"] = old_home


if __name__ == "__main__":
    unittest.main()
