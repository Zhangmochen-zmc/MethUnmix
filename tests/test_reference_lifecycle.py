from __future__ import annotations

import json
import importlib.util
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from demethflow_core.cli import main
from demethflow_core.errors import DeMethFlowError
from demethflow_core.reference_lifecycle import (
    audit_reference_lifecycle_registry,
    load_reference_lifecycle_registry,
    reference_lifecycle_declaration_plan,
)


BUILDER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_reference_lifecycle_registry.py"
BUILDER_SPEC = importlib.util.spec_from_file_location("reference_lifecycle_builder", BUILDER_SCRIPT)
assert BUILDER_SPEC and BUILDER_SPEC.loader
lifecycle_builder = importlib.util.module_from_spec(BUILDER_SPEC)
BUILDER_SPEC.loader.exec_module(lifecycle_builder)


def _digest(character: str) -> str:
    return character * 64


def _target(selector: str, version: str, digest: str, *, revoked: bool = False) -> dict:
    reference_id = selector.rsplit("@", 1)[0]
    target = {
        "asset_id": f"reference.{reference_id}.{version}", "version": version, "module_type": "reference",
        "distribution_status": "PUBLIC_DOWNLOADABLE", "license_status": "LICENSE_APPROVED",
        "license_evidence": "fixture", "selector": selector, "reference_id": reference_id,
        "content_digest": digest, "scenario": "fixture", "source_platform": "450k",
        "analysis_contract": "array_450k_cpg", "genome_build": "not_applicable",
        "url": f"https://assets.example/objects/sha256/{_digest('a')}/{reference_id}-{version}.tar.gz",
        "length": 1, "sha256": _digest("a"), "immutable_object": True, "revoked": revoked,
    }
    if revoked:
        target["revocation_reason"] = "fixture withdrawal"
    return target


def _record(selector: str, version: str, digest: str, *, state: str = "ACTIVE", replacement: str | None = None) -> dict:
    reference_id = selector.rsplit("@", 1)[0]
    record = {
        "selector": selector, "reference_id": reference_id, "version": version,
        "content_digest": digest, "archive_sha256": _digest("a"), "lifecycle_state": state,
        "construction": {
            "input_digests": [_digest("b")],
            "pipeline": {"identifier": "fixture-pipeline", "version": "1", "digest": _digest("c")},
        },
        "quality_control": [{
            "evidence_id": "qc", "kind": "STRUCTURAL_OUTPUT", "reference_content_digest": digest,
            "evidence_digest": _digest("d"),
            "scope": {"scenario": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg", "genome_build": "not_applicable", "route": "450k"},
        }],
        "engineering_evidence": [{
            "evidence_id": "e2e", "kind": "TECHNICAL_SMOKE", "reference_content_digest": digest,
            "evidence_digest": _digest("e"),
            "scope": {"scenario": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg", "genome_build": "not_applicable", "route": "450k"},
        }],
        "scientific_evidence": [{
            "evidence_id": "truth", "kind": "FIXED_SIMULATED_TRUTH", "reference_content_digest": digest,
            "evidence_digest": _digest("f"), "input_digest": _digest("1"), "truth_digest": _digest("2"),
            "acceptance_policy": "fixture-policy", "validation_scope": "fixed fixture only",
            "sample_axis_digest": _digest("5"), "cell_type_axis_digest": _digest("6"),
            "metric_definitions_digest": _digest("7"), "metrics": {"overall_mae": 0.1, "pearson": 0.9},
            "scope": {"scenario": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg", "genome_build": "not_applicable", "route": "450k", "tool": "Houseman", "execution_profile": "cpu"},
        }],
    }
    if replacement:
        record["replacement_selector"] = replacement
    if state == "REVOKED":
        record["revocation_reason"] = "fixture withdrawal"
    return record


class ReferenceLifecycleTests(unittest.TestCase):
    def _write(self, root: Path, payload: dict, name: str) -> Path:
        path = root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_declaration_plan_is_catalog_bound_and_never_claims_lifecycle_evidence(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-plan-") as tmp:
            root = Path(tmp)
            selector = "fixture.reference@1.0.0"
            catalog = self._write(root, {
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix",
                "catalog_release": "fixture-static.1", "catalog_sequence": 1,
                "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [_target(selector, "1.0.0", _digest("3"))],
            }, "catalog.json")
            plan = reference_lifecycle_declaration_plan(catalog)
            self.assertEqual(plan["status"], "DECLARATIONS_REQUIRED_NO_STATUS_PROMOTION")
            self.assertEqual(plan["reference_count"], 1)
            self.assertEqual(plan["references"][0]["selector"], selector)
            self.assertIn("pipeline.path", plan["references"][0]["required_declaration_fields"]["construction"])
            self.assertNotIn("scientific_evidence", plan["references"][0])

    def test_cli_lifecycle_plan_is_machine_readable_and_non_promoting(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-plan-") as tmp:
            root = Path(tmp)
            selector = "fixture.reference@1.0.0"
            catalog = self._write(root, {
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix",
                "catalog_release": "fixture-static.1", "catalog_sequence": 1,
                "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [_target(selector, "1.0.0", _digest("3"))],
            }, "catalog.json")
            output = StringIO()
            with redirect_stdout(output):
                rc = main(["reference", "lifecycle-plan", "--catalog", str(catalog), "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "DECLARATIONS_REQUIRED_NO_STATUS_PROMOTION")

    def test_complete_registry_binds_identity_and_evidence_without_promotion(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-") as tmp:
            root = Path(tmp)
            old = "fixture.reference@1.0.0"
            new = "fixture.reference@1.1.0"
            catalog = self._write(root, {
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "catalog_release": "fixture-static.2",
                "catalog_sequence": 2, "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [_target(old, "1.0.0", _digest("3"), revoked=True), _target(new, "1.1.0", _digest("4"))],
            }, "catalog.json")
            registry = self._write(root, {
                "schema": "methunmix-reference-lifecycle-v1", "product": "MethUnmix", "registry_release": "fixture-lifecycle.1",
                "catalog_binding": {"catalog_release": "fixture-static.2", "catalog_sequence": 2}, "coverage": "COMPLETE_CATALOG",
                "references": [_record(old, "1.0.0", _digest("3"), state="REVOKED", replacement=new), _record(new, "1.1.0", _digest("4"))],
            }, "lifecycle.json")
            result = audit_reference_lifecycle_registry(registry, catalog_path=catalog)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "PASS_LIFECYCLE_BINDING_NO_STATUS_PROMOTION")
            self.assertEqual(result["scientific_evidence_by_kind"]["FIXED_SIMULATED_TRUTH"], 2)

    def test_cli_lifecycle_audit_returns_machine_readable_pass(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-") as tmp:
            root = Path(tmp)
            selector = "fixture.reference@1.0.0"
            catalog = self._write(root, {
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "catalog_release": "fixture-static.1",
                "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [_target(selector, "1.0.0", _digest("3"))],
            }, "catalog.json")
            registry = self._write(root, {
                "schema": "methunmix-reference-lifecycle-v1", "product": "MethUnmix", "registry_release": "fixture-lifecycle.1",
                "catalog_binding": {"catalog_release": "fixture-static.1", "catalog_sequence": 1}, "coverage": "COMPLETE_CATALOG",
                "references": [_record(selector, "1.0.0", _digest("3"))],
            }, "lifecycle.json")
            output = StringIO()
            with redirect_stdout(output):
                rc = main(["reference", "lifecycle", str(registry), "--catalog", str(catalog), "--json"])
            self.assertEqual(rc, 0)
            self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_builder_calculates_declaration_file_digests_without_source_paths_in_registry(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-") as tmp:
            root = Path(tmp)
            selector = "fixture.reference@1.0.0"
            catalog = self._write(root, {
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "catalog_release": "fixture-static.1",
                "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [_target(selector, "1.0.0", _digest("3"))],
            }, "catalog.json")
            def source_file(name: str) -> str:
                path = root / name
                path.write_text(name, encoding="utf-8")
                return str(path)
            scope = {"scenario": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg", "genome_build": "not_applicable", "route": "450k"}
            declarations = self._write(root, {
                "schema": "methunmix-reference-lifecycle-declarations-v1", "product": "MethUnmix",
                "registry_release": "fixture-lifecycle.1", "catalog_binding": {"catalog_release": "fixture-static.1", "catalog_sequence": 1},
                "coverage": "COMPLETE_CATALOG", "references": [{
                    "selector": selector,
                    "construction": {"input_paths": [source_file("construction-input")], "pipeline": {"identifier": "fixture", "version": "1", "path": source_file("pipeline")}},
                    "quality_control": [{"evidence_id": "qc", "kind": "COORDINATE_QC", "evidence_path": source_file("qc"), "scope": scope}],
                    "engineering_evidence": [{"evidence_id": "e2e", "kind": "TECHNICAL_SMOKE", "evidence_path": source_file("e2e"), "scope": scope}],
                    "scientific_evidence": [{
                        "evidence_id": "truth", "kind": "FIXED_SIMULATED_TRUTH", "evidence_path": source_file("truth-evidence"),
                        "input_path": source_file("input"), "truth_path": source_file("truth"),
                        "sample_axis_path": source_file("sample-axis"), "cell_type_axis_path": source_file("cell-axis"),
                        "metric_definitions_path": source_file("metric-definitions"), "metrics": {"overall_mae": 0.1},
                        "acceptance_policy": "fixture", "validation_scope": "fixed fixture", "scope": scope | {"tool": "Houseman", "execution_profile": "cpu"},
                    }],
                }],
            }, "declarations.json")
            registry = lifecycle_builder.build_registry(declarations, catalog)
            output = root / "nested" / "registry.json"
            output.parent.mkdir()
            output.write_text(json.dumps(registry), encoding="utf-8")
            self.assertTrue(audit_reference_lifecycle_registry(output, catalog_path=catalog)["ok"])
            self.assertNotIn(str(root), json.dumps(registry))

    def test_builder_resolves_relative_declaration_paths_from_the_declaration_directory(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-relative-") as tmp:
            root = Path(tmp)
            selector = "fixture.reference@1.0.0"
            catalog = self._write(root, {
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix",
                "catalog_release": "fixture-static.1", "catalog_sequence": 1,
                "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [_target(selector, "1.0.0", _digest("3"))],
            }, "catalog.json")
            declaration_root = root / "declarations"
            declaration_root.mkdir()
            def source_file(name: str) -> str:
                (declaration_root / name).write_text(name, encoding="utf-8")
                return name
            scope = {
                "scenario": "fixture", "source_platform": "450k", "analysis_contract": "array_450k_cpg",
                "genome_build": "not_applicable", "route": "450k",
            }
            declarations = self._write(declaration_root, {
                "schema": "methunmix-reference-lifecycle-declarations-v1", "product": "MethUnmix",
                "registry_release": "fixture-lifecycle.1",
                "catalog_binding": {"catalog_release": "fixture-static.1", "catalog_sequence": 1},
                "coverage": "COMPLETE_CATALOG", "references": [{
                    "selector": selector,
                    "construction": {"input_paths": [source_file("input")], "pipeline": {
                        "identifier": "fixture", "version": "1", "path": source_file("pipeline"),
                    }},
                    "quality_control": [{"evidence_id": "qc", "kind": "COORDINATE_QC",
                                           "evidence_path": source_file("qc"), "scope": scope}],
                    "engineering_evidence": [{"evidence_id": "e2e", "kind": "TECHNICAL_SMOKE",
                                                 "evidence_path": source_file("e2e"), "scope": scope}],
                    "scientific_evidence": [],
                }],
            }, "declarations.json")
            registry = lifecycle_builder.build_registry(declarations, catalog)
            output = root / "registry.json"
            output.write_text(json.dumps(registry), encoding="utf-8")
            self.assertTrue(audit_reference_lifecycle_registry(output, catalog_path=catalog)["ok"])

    def test_catalog_reference_without_record_fails_complete_coverage(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-") as tmp:
            root = Path(tmp)
            catalog = self._write(root, {
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "catalog_release": "fixture-static.1",
                "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [_target("fixture.reference@1.0.0", "1.0.0", _digest("3"))],
            }, "catalog.json")
            registry = self._write(root, {
                "schema": "methunmix-reference-lifecycle-v1", "product": "MethUnmix", "registry_release": "fixture-lifecycle.1",
                "catalog_binding": {"catalog_release": "fixture-static.1", "catalog_sequence": 1}, "coverage": "COMPLETE_CATALOG", "references": [],
            }, "lifecycle.json")
            result = audit_reference_lifecycle_registry(registry, catalog_path=catalog)
            self.assertFalse(result["ok"])
            self.assertEqual(result["problems"][0]["code"], "MISSING_LIFECYCLE_RECORD")

    def test_registry_rejects_revocation_without_replacement(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-") as tmp:
            root = Path(tmp)
            record = _record("fixture.reference@1.0.0", "1.0.0", _digest("3"), state="REVOKED")
            registry = self._write(root, {
                "schema": "methunmix-reference-lifecycle-v1", "product": "MethUnmix", "registry_release": "fixture-lifecycle.1",
                "catalog_binding": {"catalog_release": "fixture-static.1", "catalog_sequence": 1}, "coverage": "DECLARED_SUBSET", "references": [record],
            }, "lifecycle.json")
            record.pop("replacement_selector", None)
            with self.assertRaisesRegex(DeMethFlowError, "REVOKED reference requires replacement_selector"):
                load_reference_lifecycle_registry(registry)

    def test_replacement_scope_mismatch_is_reported(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-") as tmp:
            root = Path(tmp)
            old = "fixture.reference@1.0.0"
            new = "fixture.reference@1.1.0"
            bad_target = _target(new, "1.1.0", _digest("4"))
            bad_target["scenario"] = "other"
            catalog = self._write(root, {
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "catalog_release": "fixture-static.2",
                "catalog_sequence": 2, "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [_target(old, "1.0.0", _digest("3"), revoked=True), bad_target],
            }, "catalog.json")
            registry = self._write(root, {
                "schema": "methunmix-reference-lifecycle-v1", "product": "MethUnmix", "registry_release": "fixture-lifecycle.1",
                "catalog_binding": {"catalog_release": "fixture-static.2", "catalog_sequence": 2}, "coverage": "COMPLETE_CATALOG",
                "references": [_record(old, "1.0.0", _digest("3"), state="REVOKED", replacement=new), _record(new, "1.1.0", _digest("4"))],
            }, "lifecycle.json")
            result = audit_reference_lifecycle_registry(registry, catalog_path=catalog)
            self.assertFalse(result["ok"])
            self.assertIn("REPLACEMENT_SCOPE_MISMATCH", {item["code"] for item in result["problems"]})

    def test_lifecycle_stage_is_non_scientific_and_replacement_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-lifecycle-") as tmp:
            root = Path(tmp)
            first = "fixture.reference@1.0.0"; second = "fixture.reference@1.1.0"
            left = _record(first, "1.0.0", _digest("3"), state="SUPERSEDED", replacement=second)
            right = _record(second, "1.1.0", _digest("4"), state="SUPERSEDED", replacement=first)
            left["lifecycle_stage"] = "LOCALLY_REGISTERED"
            registry = self._write(root, {
                "schema": "methunmix-reference-lifecycle-v1", "product": "MethUnmix", "registry_release": "fixture-lifecycle.1",
                "catalog_binding": {"catalog_release": "fixture-static.1", "catalog_sequence": 1}, "coverage": "DECLARED_SUBSET", "references": [left, right],
            }, "lifecycle.json")
            with self.assertRaisesRegex(DeMethFlowError, "replacement cycle"):
                load_reference_lifecycle_registry(registry)


if __name__ == "__main__":
    unittest.main()
