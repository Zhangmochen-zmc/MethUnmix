from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_reference_lifecycle_declarations.py"
SPEC = importlib.util.spec_from_file_location("reference_lifecycle_readiness", SCRIPT)
assert SPEC and SPEC.loader
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)


class ReferenceLifecycleReadinessTests(unittest.TestCase):
    selector = "fixture.reference@1.0.0"

    def _catalog(self, root: Path) -> Path:
        path = root / "catalog.json"
        path.write_text(json.dumps({
            "schema": "methunmix-static-catalog-v1",
            "product": "MethUnmix",
            "catalog_release": "fixture-static.1",
            "catalog_sequence": 1,
            "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
            "targets": [{
                "asset_id": "reference.fixture.reference.1.0.0",
                "version": "1.0.0",
                "module_type": "reference",
                "distribution_status": "PUBLIC_DOWNLOADABLE",
                "license_status": "LICENSE_APPROVED",
                "license_evidence": "fixture",
                "selector": self.selector,
                "reference_id": "fixture.reference",
                "content_digest": "1" * 64,
                "scenario": "fixture",
                "source_platform": "450k",
                "analysis_contract": "array_450k_cpg",
                "genome_build": "not_applicable",
                "url": "https://assets.example/objects/sha256/" + "a" * 64 + "/fixture.tar.gz",
                "length": 1,
                "sha256": "a" * 64,
                "immutable_object": True,
            }],
        }), encoding="utf-8")
        return path

    @staticmethod
    def _file(root: Path, name: str) -> str:
        (root / name).write_text(name, encoding="utf-8")
        return name

    def _declaration(self, root: Path, *, science: list[dict] | None = None, engineering: list[dict] | None = None) -> Path:
        scope = {
            "scenario": "fixture", "source_platform": "450k",
            "analysis_contract": "array_450k_cpg", "genome_build": "not_applicable", "route": "450k",
        }
        declaration = {
            "schema": "methunmix-reference-lifecycle-declarations-v1",
            "product": "MethUnmix",
            "registry_release": "fixture-lifecycle.1",
            "catalog_binding": {"catalog_release": "fixture-static.1", "catalog_sequence": 1},
            "coverage": "COMPLETE_CATALOG",
            "references": [{
                "selector": self.selector,
                "construction": {
                    "input_paths": [self._file(root, "construction-input")],
                    "pipeline": {
                        "identifier": "fixture-pipeline", "version": "1",
                        "path": self._file(root, "pipeline.py"),
                    },
                },
                "quality_control": [{
                    "evidence_id": "qc", "kind": "COORDINATE_QC",
                    "evidence_path": self._file(root, "qc.json"), "scope": scope,
                }],
                "engineering_evidence": engineering if engineering is not None else [{
                    "evidence_id": "engineering", "kind": "TECHNICAL_SMOKE",
                    "evidence_path": self._file(root, "engineering.json"), "scope": scope,
                }],
                "scientific_evidence": science if science is not None else [],
            }],
        }
        path = root / "declarations.json"
        path.write_text(json.dumps(declaration), encoding="utf-8")
        return path

    def test_complete_declarations_with_relative_paths_are_ready_without_scientific_promotion(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-readiness-") as temporary:
            root = Path(temporary)
            report = readiness.audit_declarations(self._declaration(root), self._catalog(root))
            self.assertEqual(report["status"], "READY_TO_BUILD_COMPLETE_CATALOG_NO_PROMOTION")
            self.assertTrue(report["registry_build_permitted"])
            self.assertTrue(report["complete_catalog_registry_build_permitted"])
            self.assertEqual(report["scientific_status"], "NOT_EVALUATED_NO_PROMOTION")
            self.assertEqual(report["references"][0]["scientific_evidence"], "NOT_DECLARED_NO_PROMOTION")

    def test_missing_or_invalid_evidence_blocks_registry_build_without_inferring_science(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-readiness-") as temporary:
            root = Path(temporary)
            science = [{
                "evidence_id": "science", "kind": "FIXED_SIMULATED_TRUTH",
                "evidence_path": "missing.json", "input_path": "input.csv", "truth_path": "truth.csv",
                "sample_axis_path": "samples.csv", "cell_type_axis_path": "cells.csv",
                "metric_definitions_path": "metrics.json", "metrics": {},
                "acceptance_policy": "", "validation_scope": "",
                "scope": {
                    "scenario": "wrong", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
                    "genome_build": "not_applicable", "route": "450k", "tool": "Houseman", "execution_profile": "cpu",
                },
            }]
            report = readiness.audit_declarations(
                self._declaration(root, science=science, engineering=[]), self._catalog(root)
            )
            self.assertEqual(report["status"], "INCOMPLETE_DECLARATIONS_NO_REGISTRY")
            self.assertFalse(report["registry_build_permitted"])
            self.assertEqual(report["scientific_status"], "NOT_EVALUATED_NO_PROMOTION")
            blockers = report["references"][0]["blockers"]
            self.assertTrue(any("engineering_evidence" in item for item in blockers))
            self.assertTrue(any("scientific_evidence" in item for item in blockers))


if __name__ == "__main__":
    unittest.main()
