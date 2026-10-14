from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from demethflow_core.asset_qc import validate_asset_qc_record
from demethflow_core.cli import main
from demethflow_core.construction_record import construction_record_identity, validate_construction_record
from demethflow_core.errors import DeMethFlowError
from demethflow_core.scientific_evidence import add_review, audit, import_record, initialize


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def construction() -> dict:
    return {
        "schema": "methunmix-construction-record-v1", "record_id": "fixture-construction-v1",
        "record_source": {"kind": "synthetic-test", "schema_version": "1"},
        "reference_content_digest": digest("reference-content"),
        "construction_inputs": [{"identifier": "matrix", "sha256": digest("matrix")}],
        "pipeline": {"identifier": "fixture-builder", "version": "1", "sha256": digest("pipeline")},
        "parameters": {"normalization": "none"},
        "semantic_scope": {"genome_build": "not_applicable", "coordinate_system": "array_probe_id",
                           "sample_axis_sha256": digest("samples"), "cell_type_axis_sha256": digest("cells")},
        "marker_sources": [{"identifier": "markers", "sha256": digest("markers")}],
        "model_or_cache_sources": [], "known_limitations": ["synthetic fixture only"],
    }


def qc(asset_type: str) -> dict:
    common = {"schema": "methunmix-asset-qc-v1", "asset_type": asset_type, "content_digest": digest(asset_type)}
    data = {
        "array_cpg": {"cpg_ids": ["cg0001", "cg0002"], "sample_axis": ["S1", "S2"], "values": [[0.1, 0.2], [0.3, 0.4]]},
        "wgbs_interval": {"genome_build": "hg38", "coordinate_system": "bed_0_based_half_open", "intervals": [{"chrom": "chr1", "start": 2, "end": 3}]},
        "read_level": {"format": "native_cpg_bed", "records": [{"chrom": "chr1", "start": 2, "end": 3, "methylated": 1, "depth": 2}]},
        "marker": {"source_sha256": digest("source"), "target_content_digest": digest("target"), "marker_ids": ["cg0001"]},
        "model_cache": {"format": "opaque_binary_no_execution", "members": [{"path": "weights.bin", "sha256": digest("weights"), "bytes": 7}]},
        "metadata_cell_axis": {"sample_axis": ["S1"], "cell_type_axis": ["astrocyte", "neuron"], "mapping": {"astrocyte": "Astrocyte"}},
    }
    return {**common, "data": data[asset_type]}


class ConstructionAndQcTests(unittest.TestCase):
    def test_construction_record_has_stable_non_circular_identity(self):
        record = validate_construction_record(construction())
        self.assertEqual(record["construction_record_sha256"], construction_record_identity(record))
        evidence_attachment = {"evidence_digest": digest("later-qc"), "construction_record_sha256": record["construction_record_sha256"]}
        self.assertEqual(evidence_attachment["construction_record_sha256"], construction_record_identity(record))
        tampered = dict(record); tampered["parameters"] = {"normalization": "changed"}
        with self.assertRaisesRegex(DeMethFlowError, "does not match"):
            validate_construction_record(tampered)

    def test_each_declared_asset_type_has_safe_structural_qc(self):
        for category in ("array_cpg", "wgbs_interval", "read_level", "marker", "model_cache", "metadata_cell_axis"):
            with self.subTest(category=category):
                result = validate_asset_qc_record(qc(category))
                self.assertEqual(result["structural_qc"], "PASS")
                self.assertEqual(result["engineering_validation"], "NOT_EXECUTED")
                self.assertEqual(result["scientific_validation"], "NOT_EXECUTED")

    def test_qc_rejects_axis_coordinate_and_unsafe_member_errors_via_public_cli(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-stage8-qc-") as temporary:
            root = Path(temporary); record = qc("array_cpg"); record["data"]["sample_axis"] = ["S1", "S1"]
            path = root / "bad.json"; path.write_text(json.dumps(record), encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(["reference", "qc", "--record", str(path)]), 2)
            interval = qc("wgbs_interval"); interval["data"]["intervals"][0]["end"] = 2
            with self.assertRaisesRegex(DeMethFlowError, "invalid half-open"):
                validate_asset_qc_record(interval)
            model = qc("model_cache"); model["data"]["members"][0]["path"] = "../unsafe.bin"
            with self.assertRaisesRegex(DeMethFlowError, "unsafe"):
                validate_asset_qc_record(model)


class ReviewBoundaryTests(unittest.TestCase):
    def _record(self) -> dict:
        return {"evidence_id": "synthetic-evidence", "schema_version": "methunmix-scientific-evidence-v1", "evidence_type": "TECHNICAL_SMOKE",
                "validation_object": {"reference_selector": "fixture.ref@1.0.0", "reference_content_digest": digest("reference"), "tool": "Houseman", "adapter": "fixture", "profile": "cpu", "scenario": "fixture", "platform": "450k", "genome_build": "not_applicable", "analysis_contract": "array_450k_cpg"},
                "source": {"kind": "synthetic-test"}}

    def test_reviews_keep_reasons_and_audit_contradictions(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-stage8-review-") as temporary:
            registry = Path(temporary) / "registry.json"; initialize(registry); import_record(registry, self._record())
            add_review(registry, {"review_id": "accept", "evidence_id": "synthetic-evidence", "decision": "ACCEPTED", "reviewer_claim": "synthetic", "source": "test"})
            add_review(registry, {"review_id": "reject", "evidence_id": "synthetic-evidence", "decision": "REJECTED", "reason": "contradictory fixture", "reviewer_claim": "synthetic", "source": "test"})
            self.assertIn("CONTRADICTORY_REVIEW_DECISIONS", [p["code"] for p in audit(registry)["problems"]])
            add_review(registry, {"review_id": "supersede-accept", "evidence_id": "synthetic-evidence", "decision": "SUPERSEDED", "reason": "later synthetic review replaces acceptance", "supersedes_review_id": "accept", "reviewer_claim": "synthetic", "source": "test"})
            self.assertNotIn("CONTRADICTORY_REVIEW_DECISIONS", [p["code"] for p in audit(registry)["problems"]])
            with self.assertRaisesRegex(DeMethFlowError, "requires non-empty reason"):
                add_review(registry, {"review_id": "bad-revoke", "evidence_id": "synthetic-evidence", "decision": "REVOKED"})
            with self.assertRaisesRegex(DeMethFlowError, "supersedes_review_id"):
                add_review(registry, {"review_id": "bad-super", "evidence_id": "synthetic-evidence", "decision": "SUPERSEDED", "reason": "fixture"})
