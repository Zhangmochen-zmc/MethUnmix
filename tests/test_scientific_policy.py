from __future__ import annotations

import unittest

from demethflow_core.builder import _scientific_validation_disposition, _user_build_science_state
from demethflow_core.science import (
    BUILD_VALIDATION_POLICIES,
    approved_policy_result,
    metrics_pass,
    thresholds_for,
)


class ScientificPolicyTests(unittest.TestCase):
    def test_no_global_numeric_threshold_is_applied_to_non_methylbert_tools(self):
        poor_metrics = {
            "overall_mae": 0.91,
            "overall_pearson": 0.12,
            "maximum_cell_type_mae": 0.88,
            "valid_output_rate": 1.0,
            "missing_cell_types": 0,
            "repeat_max_absolute_difference": 0.7,
        }
        for tool in ("EDec", "EMeth", "EpiSCORE", "ARIC", "MethylCIBERSORT"):
            with self.subTest(tool=tool):
                self.assertEqual(thresholds_for(tool), {})
                self.assertIsNone(metrics_pass(poor_metrics, tool=tool))
                self.assertEqual(approved_policy_result(poor_metrics, tool, None), "NOT_ASSESSED")
                self.assertNotIn((tool, "450k"), BUILD_VALIDATION_POLICIES)

    def test_methylbert_pearson_rule_is_tool_scoped(self):
        self.assertEqual(thresholds_for("MethylBERT"), {"overall_pearson_min": 0.80})
        self.assertTrue(metrics_pass({"overall_pearson": 0.80}, tool="MethylBERT"))
        self.assertFalse(metrics_pass({"overall_pearson": 0.799}, tool="MethylBERT"))

    def test_only_matching_owner_approved_tool_policy_can_assess_metrics(self):
        policy = {
            "approval_status": "OWNER_APPROVED",
            "policy_id": "edec-fixture-policy-v1",
            "tool": "EDec",
            "thresholds": {"overall_pearson": {"min": 0.70}},
        }
        self.assertEqual(approved_policy_result({"overall_pearson": 0.71}, "EDec", policy), "PASS")
        self.assertEqual(approved_policy_result({"overall_pearson": 0.69}, "EDec", policy), "FAIL")
        with self.assertRaisesRegex(ValueError, "matching tool"):
            approved_policy_result({"overall_pearson": 0.99}, "EMeth", policy)
        self.assertEqual(
            approved_policy_result({"overall_pearson": 0.99}, "EDec", {**policy, "approval_status": "CANDIDATE"}),
            "NOT_ASSESSED",
        )

    def test_builder_keeps_report_only_metrics_runnable_but_never_ready(self):
        qc = {
            "structural_status": "PASS",
            "marker_gate_status": "PASS",
            "scientific_validation": {
                "status": "NOT_ASSESSED",
                "engineering_status": "PASS",
                "policy_result": "NOT_ASSESSED",
                "approved_policy": None,
                "metrics": {"overall_mae": 0.9, "overall_pearson": 0.1},
            }
        }
        self.assertEqual(_scientific_validation_disposition(qc, "EDec"), "NOT_ASSESSED")
        state = _user_build_science_state(qc, "EDec")
        self.assertEqual(state["status"], "RELEASED_UNVALIDATED")
        self.assertIn("metrics are reported", state["reason"])
        # A stale/misleading PASS label is insufficient without an approved policy.
        qc["scientific_validation"]["status"] = "PASS"
        self.assertEqual(_scientific_validation_disposition(qc, "EDec"), "NOT_ASSESSED")
        self.assertEqual(_user_build_science_state(qc, "EDec")["status"], "RELEASED_UNVALIDATED")

    def test_engineering_and_tool_specific_failures_still_block(self):
        qc = {
            "structural_status": "PASS",
            "marker_gate_status": "PASS",
            "scientific_validation": {
                "status": "NOT_ASSESSED",
                "engineering_status": "FAIL",
                "approved_policy": None,
                "metrics": {},
            }
        }
        self.assertEqual(_scientific_validation_disposition(qc, "EDec"), "FAIL")
        self.assertEqual(_user_build_science_state(qc, "EDec")["status"], "QUARANTINED")
        qc["structural_status"] = "FAIL"
        self.assertEqual(_user_build_science_state(qc, "EDec")["status"], "INVALID")
        qc["structural_status"] = "PASS"
        qc["scientific_validation"]["engineering_status"] = "PASS"
        qc["marker_stability"] = {"status": "FAIL"}
        self.assertEqual(_scientific_validation_disposition(qc, "MethylCIBERSORT"), "FAIL")


if __name__ == "__main__":
    unittest.main()
