"""Small public-CLI lifecycle exercise; all archives and records are synthetic."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from demethflow_core.cli import main
from demethflow_core.manifest import load_reference
from demethflow_core.modules import ModuleStore
from demethflow_core.packaging import export_reference
from demethflow_core.util import data_home_override, sha256_file


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class LifecycleEndToEndTests(unittest.TestCase):
    def _archive(self, root: Path, version: str) -> tuple[Path, str]:
        source = root / f"source-{version}"; source.mkdir()
        manifest = {
            "schema": "demethflow-reference-v1", "reference_id": "fixture.lifecycle.450k", "version": version,
            "scenario_id": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
            "compatible_input_routes": ["450k"], "genome_build": "not_applicable", "status": "READY",
            "cell_types": [{"cell_type_id": "cell_a", "display_name": "Cell A"}],
            "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
            "tool_capabilities": {"Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": []}},
            "artifact_checksums": {},
        }
        (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        reference = load_reference(source / "manifest.json")
        archive, _sidecar = export_reference(reference, root / "archives")
        stage = root / f"stage-{version}"; stage.mkdir()
        with tarfile.open(archive, "r:gz") as handle: handle.extractall(stage, filter="data")
        module_path = stage / "module.json"; module = json.loads(module_path.read_text(encoding="utf-8"))
        module["module_id"] = f"reference.fixture.lifecycle.450k.v{version}"; module_path.write_text(json.dumps(module), encoding="utf-8")
        archive.unlink(); Path(f"{archive}.sha256").unlink()
        with tarfile.open(archive, "w:gz", compresslevel=1) as handle:
            handle.add(stage / "module.json", arcname="module.json", recursive=False); handle.add(stage / "payload", arcname="payload", recursive=True)
        Path(f"{archive}.sha256").write_text(f"{sha256_file(archive)}  {archive.name}\n", encoding="utf-8")
        shutil.rmtree(stage)
        return archive, reference.content_digest()

    def _catalog(self, root: Path, targets: list[dict], sequence: int) -> Path:
        catalog = {"schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "core_version": "2.0.0rc3",
                   "catalog_release": f"synthetic-lifecycle-{sequence}", "catalog_sequence": sequence,
                   "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": targets}
        path = root / f"catalog-{sequence}.json"; path.write_text(json.dumps(catalog), encoding="utf-8"); return path

    def _target(self, archive: Path, content_digest: str, version: str, *, revoked: bool = False) -> dict:
        return {"asset_id": f"reference.fixture.lifecycle.450k.v{version}", "version": version, "module_type": "reference",
                "selector": f"fixture.lifecycle.450k@{version}", "reference_id": "fixture.lifecycle.450k",
                "scenario": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
                "genome_build": "not_applicable", "content_digest": content_digest,
                "tool_capabilities": {"Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": []}},
                "distribution_status": "PUBLIC_DOWNLOADABLE", "license_status": "LICENSE_APPROVED", "license_evidence": "synthetic",
                "immutable_object": True, "sha256": sha256_file(archive), "length": archive.stat().st_size,
                "installed_bytes": 4096, "url": f"https://fixtures.invalid/{sha256_file(archive)}/{archive.name}",
                "dependencies": [], "revoked": revoked, **({"revocation_reason": "synthetic withdrawal"} if revoked else {})}

    def _record(self, target: dict, *, state: str = "ACTIVE", replacement: str | None = None) -> dict:
        digest = target["content_digest"]; selector = target["selector"]
        scope = {"scenario": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg", "genome_build": "not_applicable", "route": "native"}
        construction = {"schema": "methunmix-construction-record-v1", "record_id": f"construction-{selector}",
                        "record_source": {"kind": "synthetic-test", "schema_version": "1"}, "reference_content_digest": digest,
                        "construction_inputs": [{"identifier": "matrix", "sha256": _sha("matrix-" + selector)}],
                        "pipeline": {"identifier": "fixture", "version": "1", "sha256": _sha("pipeline")}, "parameters": {},
                        "semantic_scope": {"genome_build": "not_applicable", "coordinate_system": "array_probe_id", "sample_axis_sha256": _sha("samples"), "cell_type_axis_sha256": _sha("cells")},
                        "marker_sources": [], "model_or_cache_sources": [], "known_limitations": ["synthetic only"]}
        record = {"selector": selector, "reference_id": "fixture.lifecycle.450k", "version": target["version"],
                  "content_digest": digest, "archive_sha256": target["sha256"], "lifecycle_state": state,
                  "lifecycle_stage": "REVOKED" if state == "REVOKED" else "LOCALLY_REGISTERED",
                  "construction": {"input_digests": [_sha("matrix-" + selector)], "pipeline": {"identifier": "fixture", "version": "1", "digest": _sha("pipeline")}, "record": construction},
                  "quality_control": [{"evidence_id": "qc", "kind": "STRUCTURAL_OUTPUT", "reference_content_digest": digest, "evidence_digest": _sha("qc-" + selector), "scope": scope}],
                  "engineering_evidence": [{"evidence_id": "engineering", "kind": "TECHNICAL_SMOKE", "reference_content_digest": digest, "evidence_digest": _sha("engineering-" + selector), "scope": scope}],
                  "scientific_evidence": []}
        if replacement: record["replacement_selector"] = replacement
        if state == "REVOKED": record["revocation_reason"] = "synthetic withdrawal"
        return record

    def _cli(self, argv: list[str]) -> int:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return main(argv)

    def test_public_catalog_install_revocation_replacement_and_review_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-stage8-lifecycle-e2e-") as temporary:
            root = Path(temporary); archive_v1, digest_v1 = self._archive(root, "1.0.0")
            v1 = self._target(archive_v1, digest_v1, "1.0.0")
            catalog_v1 = self._catalog(root, [v1], 1); plan_v1 = root / "plan-v1.json"
            with data_home_override(root / "asset-home"), patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog_v1)}, clear=False):
                self.assertEqual(self._cli(["asset", "plan", "--scenario", "fixture", "--platform", "450k", "--analysis-contract", "array_450k_cpg", "--tools", "Houseman", "--reference", v1["selector"], "--output", str(plan_v1)]), 0)
                self.assertEqual(self._cli(["asset", "import", "--plan", str(plan_v1), str(archive_v1)]), 0)
                self.assertEqual(self._cli(["asset", "verify", "--plan", str(plan_v1), "--json"]), 0)
                registry = root / "evidence.json"; record = root / "record.json"; review = root / "review.json"
                record.write_text(json.dumps({"evidence_id": "technical-v1", "schema_version": "methunmix-scientific-evidence-v1", "evidence_type": "TECHNICAL_SMOKE", "validation_object": {"reference_selector": v1["selector"], "reference_content_digest": digest_v1, "tool": "Houseman", "adapter": "fixture", "profile": "cpu", "scenario": "fixture", "platform": "450k", "genome_build": "not_applicable", "analysis_contract": "array_450k_cpg"}, "source": {"kind": "synthetic-test"}}), encoding="utf-8")
                review.write_text(json.dumps({"review_id": "synthetic-review-v1", "evidence_id": "technical-v1", "decision": "ACCEPTED", "reviewer_claim": "synthetic", "source": "test"}), encoding="utf-8")
                self.assertEqual(self._cli(["evidence", "init", "--registry", str(registry)]), 0)
                self.assertEqual(self._cli(["evidence", "import", "--registry", str(registry), "--record", str(record)]), 0)
                self.assertEqual(self._cli(["evidence", "review", "--registry", str(registry), "--review", str(review)]), 0)
                self.assertEqual(self._cli(["evidence", "audit", "--registry", str(registry), "--catalog", str(catalog_v1)]), 0)
                self.assertEqual(len(ModuleStore().list()), 1)

                archive_v2, digest_v2 = self._archive(root, "1.1.0")
                revoked_v1 = self._target(archive_v1, digest_v1, "1.0.0", revoked=True); v2 = self._target(archive_v2, digest_v2, "1.1.0")
                catalog_v2 = self._catalog(root, [revoked_v1, v2], 2); plan_v2 = root / "plan-v2.json"
                lifecycle = root / "lifecycle.json"
                lifecycle.write_text(json.dumps({"schema": "methunmix-reference-lifecycle-v1", "product": "MethUnmix", "registry_release": "synthetic-v2", "catalog_binding": {"catalog_release": "synthetic-lifecycle-2", "catalog_sequence": 2}, "coverage": "COMPLETE_CATALOG", "references": [self._record(revoked_v1, state="REVOKED", replacement=v2["selector"]), self._record(v2)]}), encoding="utf-8")
                with patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog_v2)}, clear=False):
                    self.assertEqual(self._cli(["asset", "plan", "--scenario", "fixture", "--platform", "450k", "--analysis-contract", "array_450k_cpg", "--tools", "Houseman", "--reference", revoked_v1["selector"], "--output", str(root / "revoked.json")]), 2)
                    self.assertEqual(self._cli(["asset", "plan", "--scenario", "fixture", "--platform", "450k", "--analysis-contract", "array_450k_cpg", "--tools", "Houseman", "--reference", v2["selector"], "--output", str(plan_v2)]), 0)
                    self.assertEqual(self._cli(["asset", "import", "--plan", str(plan_v2), str(archive_v2)]), 0)
                    self.assertEqual(self._cli(["reference", "lifecycle", str(lifecycle), "--catalog", str(catalog_v2), "--json"]), 0)
                self.assertEqual({module.version for module in ModuleStore().list()}, {"1.0.0", "1.1.0"})
