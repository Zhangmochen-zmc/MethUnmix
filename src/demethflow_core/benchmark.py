"""Strict user-supplied truth benchmarking for named proportion outputs.

This module is deliberately separate from release validation.  It calculates
metrics for an explicitly supplied estimate/truth pair but never changes a
reference/tool scientific status or assumes that anonymous components map to
cell types.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

from .errors import DeMethFlowError
from .util import atomic_write_json, sha256_file


SCHEMA = "methunmix-user-benchmark-v1"


@dataclass(frozen=True)
class Matrix:
    path: Path
    samples: tuple[str, ...]
    cells: tuple[str, ...]
    values: dict[str, dict[str, float]]


def _delimiter(header: str) -> str:
    return "\t" if header.count("\t") > header.count(",") else ","


def _read_matrix(path: Path, *, label: str) -> Matrix:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise DeMethFlowError(f"BENCHMARK_INPUT_MISSING: {label} matrix does not exist: {resolved}")
    try:
        with resolved.open(newline="", encoding="utf-8-sig") as handle:
            first = handle.readline()
            if not first:
                raise DeMethFlowError(f"BENCHMARK_SCHEMA_INVALID: {label} matrix is empty")
            handle.seek(0)
            reader = csv.DictReader(handle, delimiter=_delimiter(first))
            fields = reader.fieldnames or []
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise DeMethFlowError(f"BENCHMARK_SCHEMA_INVALID: cannot read {label} matrix: {exc}") from exc
    if len(fields) < 2 or fields[0] != "SampleID":
        raise DeMethFlowError(f"BENCHMARK_SCHEMA_INVALID: {label} matrix must begin with SampleID plus cell-type columns")
    cells = tuple(field.strip() for field in fields[1:])
    if any(not cell for cell in cells) or len(set(cells)) != len(cells):
        raise DeMethFlowError(f"BENCHMARK_SCHEMA_INVALID: {label} matrix has empty or duplicate cell-type columns")
    values: dict[str, dict[str, float]] = {}
    for number, row in enumerate(rows, start=2):
        sample = str(row.get("SampleID", "")).strip()
        if not sample or sample in values:
            raise DeMethFlowError(f"BENCHMARK_SCHEMA_INVALID: {label} matrix has missing or duplicate SampleID at row {number}")
        parsed: dict[str, float] = {}
        for cell in cells:
            try:
                parsed[cell] = float(str(row.get(cell, "")))
            except ValueError as exc:
                raise DeMethFlowError(
                    f"BENCHMARK_SCHEMA_INVALID: {label} matrix has non-numeric value for {sample}/{cell}"
                ) from exc
        values[sample] = parsed
    if not values:
        raise DeMethFlowError(f"BENCHMARK_SCHEMA_INVALID: {label} matrix has no data rows")
    return Matrix(resolved, tuple(values), cells, values)


def _truth_contract(matrix: Matrix) -> None:
    for sample in matrix.samples:
        row = matrix.values[sample]
        values = [row[cell] for cell in matrix.cells]
        if any(not math.isfinite(value) or value < 0.0 or value > 1.0 for value in values):
            raise DeMethFlowError(f"BENCHMARK_TRUTH_INVALID: truth proportions are not finite/in-range for {sample}")
        if not math.isclose(sum(values), 1.0, rel_tol=1e-6, abs_tol=1e-6):
            raise DeMethFlowError(f"BENCHMARK_TRUTH_INVALID: truth proportions do not sum to one for {sample}")


def _pearson(left: list[float], right: list[float]) -> tuple[float | None, str]:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    if denominator == 0.0:
        return None, "NOT_COMPUTABLE_CONSTANT_VECTOR"
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    return numerator / denominator, "COMPUTED"


def _policy_evaluation(policy: object, *, context: dict[str, object], metrics: dict[str, object]) -> dict[str, object]:
    """Evaluate only an explicitly supplied, exact-scope policy.

    The mechanism deliberately refuses partial scope matches and never changes
    a reference status.  It supports existing per-tool metric bounds without
    inventing a default numerical threshold.
    """
    if policy is None:
        return {"status": "REPORT_ONLY_NO_POLICY", "conclusion": None}
    if not isinstance(policy, dict) or not isinstance(policy.get("scope"), dict) or not isinstance(policy.get("metrics"), dict) or not policy["metrics"]:
        return {"status": "POLICY_INVALID", "conclusion": None, "reason": "policy needs object scope and metrics"}
    mismatches = []
    for key, expected in policy["scope"].items():
        if expected is None:
            continue
        if context.get(key) != expected:
            mismatches.append({"field": key, "expected": expected, "observed": context.get(key)})
    if mismatches:
        return {"status": "POLICY_NOT_APPLICABLE", "conclusion": None, "mismatches": mismatches}
    checks = []
    for key, limit in policy["metrics"].items():
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(float(limit)):
            return {"status": "POLICY_INVALID", "conclusion": None, "reason": f"metric limit must be finite numeric: {key}"}
        if key.endswith("_max"):
            metric = key[:-4]
            observed = metrics.get(metric)
            checks.append({"rule": key, "observed": observed, "limit": limit, "passed": isinstance(observed, (int, float)) and observed <= limit})
        elif key.endswith("_min"):
            metric = key[:-4]
            observed = metrics.get(metric)
            checks.append({"rule": key, "observed": observed, "limit": limit, "passed": isinstance(observed, (int, float)) and observed >= limit})
        else:
            return {"status": "POLICY_INVALID", "conclusion": None, "reason": f"unsupported metric rule {key}"}
    return {"status": "POLICY_EVALUATED_NO_STATUS_PROMOTION", "conclusion": "PASS" if checks and all(item["passed"] for item in checks) else "FAIL", "checks": checks,
            "policy_identity": policy.get("policy_id") or "UNIDENTIFIED_POLICY"}


def benchmark(
    estimate_path: Path,
    truth_path: Path,
    *,
    output: Path | None = None,
    output_contract: str = "cell_proportions_v1",
    context: dict[str, object] | None = None,
    policy: dict[str, object] | None = None,
) -> dict[str, object]:
    """Calculate observational metrics for an explicitly aligned truth pair."""
    if output_contract != "cell_proportions_v1":
        raise DeMethFlowError(
            "BENCHMARK_REFERENCE_FREE_UNMAPPABLE: anonymous reference-free components cannot be "
            "benchmarked as named cell proportions without an explicit, separately validated mapping"
        )
    estimate = _read_matrix(estimate_path, label="estimate")
    truth = _read_matrix(truth_path, label="truth")
    _truth_contract(truth)
    if set(estimate.samples) != set(truth.samples):
        missing_estimate = sorted(set(truth.samples) - set(estimate.samples))
        missing_truth = sorted(set(estimate.samples) - set(truth.samples))
        raise DeMethFlowError(
            "BENCHMARK_AXIS_MISMATCH: estimate/truth SampleID sets differ; "
            f"missing_estimate={missing_estimate[:10]}; missing_truth={missing_truth[:10]}"
        )
    if set(estimate.cells) != set(truth.cells):
        missing_estimate = sorted(set(truth.cells) - set(estimate.cells))
        missing_truth = sorted(set(estimate.cells) - set(truth.cells))
        raise DeMethFlowError(
            "BENCHMARK_AXIS_MISMATCH: estimate/truth cell-type sets differ; "
            f"missing_estimate={missing_estimate}; missing_truth={missing_truth}"
        )
    samples = truth.samples
    cells = truth.cells
    estimate_values = [estimate.values[sample][cell] for sample in samples for cell in cells]
    truth_values = [truth.values[sample][cell] for sample in samples for cell in cells]
    invalid_rows = 0
    nonfinite = 0
    for sample in samples:
        values = [estimate.values[sample][cell] for cell in cells]
        finite = all(math.isfinite(value) for value in values)
        in_range = finite and all(0.0 <= value <= 1.0 for value in values)
        normalized = finite and math.isclose(sum(values), 1.0, rel_tol=1e-4, abs_tol=1e-6)
        nonfinite += sum(not math.isfinite(value) for value in values)
        if not (finite and in_range and normalized):
            invalid_rows += 1
    if any(not math.isfinite(value) for value in estimate_values):
        pearson, pearson_status = None, "NOT_COMPUTABLE_NONFINITE_ESTIMATE"
        overall_mae: float | None = None
        per_cell: dict[str, float | None] = {cell: None for cell in cells}
    else:
        pearson, pearson_status = _pearson(estimate_values, truth_values)
        overall_mae = sum(abs(a - b) for a, b in zip(estimate_values, truth_values)) / len(estimate_values)
        per_cell = {
            cell: sum(abs(estimate.values[sample][cell] - truth.values[sample][cell]) for sample in samples) / len(samples)
            for cell in cells
        }
    numeric_cells = {cell: value for cell, value in per_cell.items() if value is not None}
    worst = max(numeric_cells, key=numeric_cells.get) if numeric_cells else None
    benchmark_context = {"output_contract": output_contract, **(context or {})}
    metric_payload = {
        "overall_pearson": pearson,
        "overall_pearson_status": pearson_status,
        "overall_mae": overall_mae,
        "maximum_cell_type_mae": max(numeric_cells.values()) if numeric_cells else None,
        "worst_cell_type": worst,
        "per_cell_type_mae": per_cell,
        "valid_output_rate": (len(samples) - invalid_rows) / len(samples),
        "invalid_output_row_count": invalid_rows,
        "nonfinite_estimate_value_count": nonfinite,
    }
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "status": "METRICS_COMPUTED_NO_STATUS_PROMOTION",
        "scope": "user-supplied named cell-proportion estimate/truth pair only",
        "output_contract": output_contract,
        "input": {
            "estimate": {"path": str(estimate.path), "sha256": sha256_file(estimate.path)},
            "truth": {"path": str(truth.path), "sha256": sha256_file(truth.path)},
        },
        "axis": {
            "sample_count": len(samples),
            "cell_type_count": len(cells),
            "sample_axis": list(samples),
            "cell_type_axis": list(cells),
            "sample_order_matched": estimate.samples == truth.samples,
            "cell_type_order_matched": estimate.cells == truth.cells,
        },
        "metrics": metric_payload,
        "context": benchmark_context,
        "policy_evaluation": _policy_evaluation(policy, context=benchmark_context, metrics=metric_payload),
        "interpretation": {
            "scientific_status_changed": False,
            "note": "Metrics are descriptive unless bound to a declared reference/tool/profile and an approved acceptance policy.",
        },
    }
    if output is not None:
        atomic_write_json(output.expanduser().resolve(), payload)
    return payload
