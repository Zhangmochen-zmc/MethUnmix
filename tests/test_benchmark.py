from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from demethflow_core.benchmark import benchmark
from demethflow_core.cli import main
from demethflow_core.errors import DeMethFlowError


class UserBenchmarkTests(unittest.TestCase):
    def _write(self, root: Path, name: str, text: str) -> Path:
        path = root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_named_truth_metrics_are_axis_bound_and_observational(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-benchmark-") as temporary:
            root = Path(temporary)
            estimate = self._write(
                root, "estimate.csv",
                "SampleID,A,B\ns2,0.1,0.9\ns1,0.8,0.2\n",
            )
            truth = self._write(
                root, "truth.csv",
                "SampleID,A,B\ns1,0.7,0.3\ns2,0.2,0.8\n",
            )
            output = root / "benchmark.json"
            report = benchmark(estimate, truth, output=output)
            self.assertEqual(report["status"], "METRICS_COMPUTED_NO_STATUS_PROMOTION")
            self.assertEqual(report["axis"]["sample_axis"], ["s1", "s2"])
            self.assertFalse(report["axis"]["sample_order_matched"])
            self.assertAlmostEqual(report["metrics"]["overall_mae"], 0.1)
            self.assertEqual(report["metrics"]["overall_pearson_status"], "COMPUTED")
            self.assertEqual(report["metrics"]["valid_output_rate"], 1.0)
            self.assertTrue(output.is_file())
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["benchmark", "--estimate", str(estimate), "--truth", str(truth)]), 0)
            self.assertIn("METRICS_COMPUTED_NO_STATUS_PROMOTION", stdout.getvalue())

    def test_constant_and_reference_free_outputs_do_not_get_false_accuracy_claims(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-benchmark-") as temporary:
            root = Path(temporary)
            estimate = self._write(root, "estimate.csv", "SampleID,A,B\ns1,0.5,0.5\ns2,0.5,0.5\n")
            truth = self._write(root, "truth.csv", "SampleID,A,B\ns1,0.5,0.5\ns2,0.5,0.5\n")
            report = benchmark(estimate, truth)
            self.assertIsNone(report["metrics"]["overall_pearson"])
            self.assertEqual(report["metrics"]["overall_pearson_status"], "NOT_COMPUTABLE_CONSTANT_VECTOR")
            with self.assertRaisesRegex(DeMethFlowError, "REFERENCE_FREE_UNMAPPABLE"):
                benchmark(estimate, truth, output_contract="reference_free_components_v1")

    def test_axis_and_truth_contract_mismatches_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-benchmark-") as temporary:
            root = Path(temporary)
            estimate = self._write(root, "estimate.csv", "SampleID,A,B\ns1,0.5,0.5\n")
            bad_axis = self._write(root, "bad-axis.csv", "SampleID,A,C\ns1,0.5,0.5\n")
            with self.assertRaisesRegex(DeMethFlowError, "AXIS_MISMATCH"):
                benchmark(estimate, bad_axis)
            bad_truth = self._write(root, "bad-truth.csv", "SampleID,A,B\ns1,0.4,0.4\n")
            with self.assertRaisesRegex(DeMethFlowError, "TRUTH_INVALID"):
                benchmark(estimate, bad_truth)

    def test_duplicate_labels_fail_and_nonfinite_values_remain_json_safe(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-benchmark-") as temporary:
            root = Path(temporary)
            truth = self._write(root, "truth.csv", "SampleID,A,B\ns1,0.2,0.8\n")
            duplicate = self._write(root, "duplicate.csv", "SampleID,A,B\ns1,0.2,0.8\ns1,0.3,0.7\n")
            with self.assertRaisesRegex(DeMethFlowError, "BENCHMARK_SCHEMA_INVALID"):
                benchmark(duplicate, truth)
            nonfinite = self._write(root, "nonfinite.csv", "SampleID,A,B\ns1,nan,0.8\n")
            report = benchmark(nonfinite, truth)
            self.assertIsNone(report["metrics"]["overall_mae"])
            self.assertEqual(report["metrics"]["overall_pearson_status"], "NOT_COMPUTABLE_NONFINITE_ESTIMATE")
            self.assertEqual(report["metrics"]["valid_output_rate"], 0.0)
