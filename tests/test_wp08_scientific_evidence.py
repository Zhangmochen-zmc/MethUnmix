from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from demethflow_core.benchmark import benchmark
from demethflow_core.cli import main
from demethflow_core.errors import DeMethFlowError
from demethflow_core.scientific_evidence import add_review, audit, import_record, initialize, query
from demethflow_core.asset_lifecycle import create_catalog_plan
from demethflow_core.util import data_home_override


def digest(letter: str) -> str:
    return letter * 64


class WP08ScientificEvidenceTests(unittest.TestCase):
    def record(self, evidence_id: str = "evidence-1") -> dict[str, object]:
        return {
            "evidence_id": evidence_id, "schema_version": "methunmix-scientific-evidence-v1", "evidence_type": "FIXED_SIMULATED_TRUTH",
            "validation_object": {"reference_selector": "fixture.array@1.0.0", "reference_content_digest": digest("a"),
                                  "tool": "Houseman", "adapter": "houseman-array-v1", "profile": "cpu",
                                  "scenario": "fixture", "platform": "450k", "genome_build": "not_applicable", "analysis_contract": "array_450k_cpg"},
            "input_identity": {"sha256": digest("b")}, "truth_identity": {"sha256": digest("c")},
            "software_identity": {"commit": digest("d")}, "runtime_identity": {"sha256": digest("e")},
            "parameters": {"fixed": True}, "seed": 1, "sample_axis": ["s1"], "output_axis": ["A", "B"],
            "metrics": {"overall_mae": 0.1}, "acceptance_policy": {"policy_id": "fixture"}, "limitations": ["synthetic"],
            "source": {"path": "historical.json", "sha256": digest("f")},
        }

    def test_import_is_idempotent_conflict_safe_and_reviews_are_separate(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp08-evidence-") as tmp:
            registry = Path(tmp) / "registry.json"; initialize(registry)
            self.assertEqual(import_record(registry, self.record())["status"], "IMPORTED_UNREVIEWED")
            self.assertEqual(import_record(registry, self.record())["status"], "IDEMPOTENT")
            changed = self.record(); changed["metrics"] = {"overall_mae": 0.2}
            with self.assertRaisesRegex(DeMethFlowError, "EVIDENCE_ID_CONFLICT"):
                import_record(registry, changed)
            review = {"review_id": "review-1", "evidence_id": "evidence-1", "decision": "ACCEPTED", "reviewer_claim": "owner", "source": "fixture"}
            self.assertEqual(add_review(registry, review)["status"], "RECORDED_UNAUTHENTICATED_REVIEW")
            item = query(registry, selector="fixture.array@1.0.0")
            self.assertEqual(item["record_count"], 1)
            self.assertEqual(item["records"][0]["record"]["review_status"], "IMPORTED_UNREVIEWED")
            self.assertEqual(item["records"][0]["reviews"][0]["authentication"], "UNVERIFIED_DECLARATION")
            self.assertEqual(audit(registry)["records_missing_reference_identity"], 0)

    def test_missing_identity_is_retained_not_inferred(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp08-evidence-") as tmp:
            registry = Path(tmp) / "registry.json"; initialize(registry)
            record = self.record(); record["validation_object"]["reference_content_digest"] = None
            import_record(registry, record)
            self.assertEqual(audit(registry)["records_missing_reference_identity"], 1)

    def test_schema_relationships_and_atomic_write_failure_are_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp08-evidence-") as tmp:
            registry = Path(tmp) / "registry.json"; initialize(registry)
            bad = self.record(); bad["schema_version"] = "unrecognized-v9"
            with self.assertRaisesRegex(DeMethFlowError, "unsupported record schema"):
                import_record(registry, bad)
            related = self.record("evidence-2")
            related["relationships"] = [{"relation": "SUPPLEMENTS", "target_evidence_id": "missing-evidence"}]
            import_record(registry, related)
            self.assertEqual(audit(registry)["problems"][0]["code"], "MISSING_RELATED_EVIDENCE")
            source_before = registry.read_bytes()
            record = self.record("evidence-3")
            with patch("demethflow_core.scientific_evidence.atomic_write_json", side_effect=OSError("simulated interruption")):
                with self.assertRaises(OSError):
                    import_record(registry, record)
            self.assertEqual(registry.read_bytes(), source_before)
            original = self.record("missing-evidence")
            original["relationships"] = [{"relation": "WITHDRAWS", "target_evidence_id": "evidence-2"}]
            import_record(registry, original)
            self.assertEqual(audit(registry)["problems"], [])

    def test_catalog_binding_rejects_selector_and_digest_mismatches(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp08-evidence-") as tmp:
            root = Path(tmp); registry = root / "registry.json"; catalog = root / "catalog.json"
            initialize(registry); import_record(registry, self.record())
            catalog.write_text(json.dumps({
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "catalog_release": "fixture",
                "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [{"asset_id": "reference.fixture.array", "module_type": "reference",
                             "selector": "fixture.array@1.0.0", "content_digest": digest("9"),
                             "distribution_status": "USER_SUPPLIED_SUPPORTED"}],
            }), encoding="utf-8")
            self.assertEqual(audit(registry, catalog_path=catalog)["problems"][0]["code"], "REFERENCE_DIGEST_MISMATCH")

    def test_revoked_catalog_blocks_new_plan_without_deleting_local_content(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp08-revocation-") as tmp:
            root = Path(tmp); catalog = root / "catalog.json"; home = root / "home"
            home.mkdir(); preserved = home / "already-installed-marker"; preserved.write_text("keep", encoding="utf-8")
            catalog.write_text(json.dumps({
                "schema": "methunmix-static-catalog-v1", "product": "MethUnmix", "catalog_release": "fixture",
                "catalog_sequence": 1, "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [{
                    "asset_id": "reference.fixture.array", "version": "1.0.0", "module_type": "reference",
                    "selector": "fixture.array@1.0.0", "reference_id": "fixture.array", "scenario": "fixture",
                    "source_platform": "450k", "compatible_input_routes": ["450k"], "analysis_contract": "array_450k_cpg",
                    "genome_build": "not_applicable", "content_digest": digest("a"),
                    "distribution_status": "USER_SUPPLIED_SUPPORTED", "revoked": True, "revocation_reason": "fixture withdrawal",
                    "tool_capabilities": {"Houseman": {"status": "READY", "artifacts": {}, "runtime_modules": []}},
                }],
            }), encoding="utf-8")
            with data_home_override(home), patch.dict(os.environ, {"METHUNMIX_CATALOG_PATH": str(catalog)}, clear=False):
                plan, code = create_catalog_plan(
                    scenario="fixture", platform="450k", genome_build=None, analysis_contract="array_450k_cpg",
                    tools="Houseman", output=root / "plan.json",
                )
            self.assertEqual(code, 2)
            self.assertIn("REVOKED", [item["code"] for item in plan["blockers"]])
            self.assertEqual(preserved.read_text(encoding="utf-8"), "keep")

    def test_policy_requires_exact_scope_and_never_promotes(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp08-metric-") as tmp:
            root = Path(tmp)
            estimate = root / "estimate.csv"; truth = root / "truth.csv"
            estimate.write_text("SampleID,A,B\ns1,0.7,0.3\n", encoding="utf-8")
            truth.write_text("SampleID,A,B\ns1,0.8,0.2\n", encoding="utf-8")
            policy = {"policy_id": "houseman-fixture", "scope": {"tool": "Houseman", "profile": "cpu", "output_contract": "cell_proportions_v1"}, "metrics": {"overall_mae_max": 0.11}}
            result = benchmark(estimate, truth, context={"tool": "Houseman", "profile": "cpu"}, policy=policy)
            self.assertEqual(result["policy_evaluation"]["conclusion"], "PASS")
            self.assertFalse(result["interpretation"]["scientific_status_changed"])
            mismatch = benchmark(estimate, truth, context={"tool": "Other", "profile": "cpu"}, policy=policy)
            self.assertEqual(mismatch["policy_evaluation"]["status"], "POLICY_NOT_APPLICABLE")
            malformed = benchmark(estimate, truth, context={"tool": "Houseman", "profile": "cpu"}, policy={"scope": {}, "metrics": {"overall_mae_max": "not-a-number"}})
            self.assertEqual(malformed["policy_evaluation"]["status"], "POLICY_INVALID")

    def test_cli_registry_and_benchmark_are_usable_outside_a_workflow(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp08-cli-") as tmp:
            root = Path(tmp); registry = root / "registry.json"; record = root / "record.json"
            record.write_text(json.dumps(self.record()), encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["evidence", "init", "--registry", str(registry)]), 0)
                self.assertEqual(main(["evidence", "import", "--registry", str(registry), "--record", str(record)]), 0)
                self.assertEqual(main(["evidence", "audit", "--registry", str(registry)]), 0)
