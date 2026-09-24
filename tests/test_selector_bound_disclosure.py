from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from demethflow_core.runtime import release_disclosures_for_tools


class SelectorBoundDisclosureTests(unittest.TestCase):
    def _bundle(self, tmp: str, report: dict | None, tool: str = "CelFiE"):
        root = Path(tmp)
        validation = root / "validation"
        validation.mkdir()
        if report is not None:
            (validation / "report.json").write_text(json.dumps(report), encoding="utf-8")
        cap = SimpleNamespace(
            status="READY",
            execution_policy="normal",
            effective_status=lambda _accelerator: ("READY", ""),
            artifacts={"scientific_validation": "validation/report.json"},
            validation_policy={},
            validation_scope="fixed_truth_simulation",
            reason="",
        )
        bundle = SimpleNamespace(
            payload={
                "release_validation": {"tool_warnings": {}, "tool_validation_evidence": {}},
                "tool_capabilities": {tool: {}},
                "genome_build": "hg19",
            },
            tools={tool: cap},
            root=root,
            selector="builtin.fixture.wgbs-native-hg19@1.0.0",
            scenario_id="fixture",
            source_platform="wgbs",
            content_digest=lambda: "current-digest",
        )
        return bundle

    @staticmethod
    def _report(**overrides):
        report = {
            "tool": "CelFiE",
            "scenario": "fixture",
            "platform": "wgbs",
            "genome_build": "hg19",
            "selector": "builtin.fixture.wgbs-native-hg19@1.0.0",
            "reference_digest": "current-digest",
            "truth": {"path": "truth.csv", "sha256": "truth-digest"},
            "metrics": {
                "overall_pearson": 0.91,
                "overall_mae": 0.04,
                "maximum_cell_type_mae": 0.07,
            },
        }
        report.update(overrides)
        return report

    def test_matching_selector_and_digest_returns_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            disclosures = release_disclosures_for_tools(
                self._bundle(tmp, self._report()), ["CelFiE"]
            )
        item = disclosures["CelFiE"]
        self.assertEqual(item["disclosure_status"], "BOUND_CURRENT")
        self.assertEqual(item["observed_metrics"]["overall_pearson"], 0.91)
        self.assertEqual(item["status"], "READY")
        self.assertEqual(item["scientific_evidence_status"], "BOUND_CURRENT")
        self.assertEqual(item["scientific_status"], "READY")

    def test_selector_mismatch_fails_closed_without_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            disclosures = release_disclosures_for_tools(
                self._bundle(tmp, self._report(selector="builtin.fixture.wgbs-native-hg19@0.9.0")),
                ["CelFiE"],
            )
        item = disclosures["CelFiE"]
        self.assertEqual(item["disclosure_status"], "NO_SELECTOR_BOUND_VALIDATION_EVIDENCE")
        self.assertEqual(item["observed_metrics"], {})
        self.assertEqual(item["scientific_evidence_status"], "INCOMPLETE")
        self.assertEqual(item["scientific_status"], "NOT_PROMOTED")
        self.assertEqual(item["status"], "READY")
        self.assertIn("NO_SELECTOR_BOUND_VALIDATION_EVIDENCE", item["warning"])

    def test_reference_digest_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = release_disclosures_for_tools(
                self._bundle(tmp, self._report(reference_digest="old-digest")), ["CelFiE"]
            )["CelFiE"]
        self.assertEqual(item["disclosure_status"], "NO_SELECTOR_BOUND_VALIDATION_EVIDENCE")
        self.assertEqual(item["observed_metrics"], {})
        self.assertEqual(item["scientific_evidence_status"], "INCOMPLETE")

    def test_missing_binding_field_fails_closed(self):
        report = self._report()
        del report["reference_digest"]
        with tempfile.TemporaryDirectory() as tmp:
            item = release_disclosures_for_tools(
                self._bundle(tmp, report), ["CelFiE"]
            )["CelFiE"]
        self.assertEqual(item["disclosure_status"], "NO_SELECTOR_BOUND_VALIDATION_EVIDENCE")
        self.assertEqual(item["observed_metrics"], {})
        self.assertEqual(item["scientific_evidence_status"], "INCOMPLETE")

    def test_celfeer_missing_validation_report_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = release_disclosures_for_tools(
                self._bundle(tmp, None, tool="CelFEER"), ["CelFEER"]
            )["CelFEER"]
        self.assertEqual(item["disclosure_status"], "NO_VALIDATION_EVIDENCE")
        self.assertEqual(item["scientific_evidence_status"], "INCOMPLETE")
        self.assertEqual(item["scientific_status"], "NOT_PROMOTED")
        self.assertEqual(item["observed_metrics"], {})
        self.assertIn("NO_VALIDATION_EVIDENCE", item["warning"])

    def test_metdecode_disclosure_path_is_unchanged(self):
        report = self._report(tool="MetDecode")
        with tempfile.TemporaryDirectory() as tmp:
            item = release_disclosures_for_tools(
                self._bundle(tmp, report, tool="MetDecode"), ["MetDecode"]
            )["MetDecode"]
        self.assertEqual(item["disclosure_status"], "VALIDATION_METRICS_AVAILABLE")
        self.assertEqual(item["observed_metrics"]["overall_pearson"], 0.91)
        self.assertEqual(item["status"], "READY")
        self.assertEqual(item["scientific_status"], "READY")

    def test_ready_without_scientific_evidence_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = release_disclosures_for_tools(
                self._bundle(tmp, None, tool="MethylBERT"), ["MethylBERT"]
            )["MethylBERT"]
        self.assertEqual(item["status"], "READY")
        self.assertEqual(item["execution_status"], "PASS")
        self.assertEqual(item["scientific_evidence_status"], "NOT_CONFIGURED")
        self.assertEqual(item["scientific_status"], "NOT_PROMOTED")
        self.assertEqual(item["disclosure_status"], "NO_VALIDATION_METRICS")

    def test_celfeer_generic_capability_policy_is_not_projected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(tmp, self._report(tool="CelFEER"), tool="CelFEER")
            bundle.tools["CelFEER"].validation_policy = {
                "overall_pearson_min": 0.8,
                "overall_mae_max": 0.1,
            }
            item = release_disclosures_for_tools(bundle, ["CelFEER"])["CelFEER"]
        self.assertIsNone(item["validation_policy"])
        self.assertEqual(item["failed_gates"], [])
        self.assertEqual(item["observed_metrics"]["overall_pearson"], 0.91)


if __name__ == "__main__":
    unittest.main()
