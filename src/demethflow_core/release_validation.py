from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


CANONICAL_CELLS = (
    "B cells", "CD4+ T cells", "CD8+ T cells", "Monocytes",
    "Neutrophils", "Natural Killer cells",
)
ALIASES = {
    "b": "B cells", "bcell": "B cells", "b cells": "B cells", "b-cells": "B cells",
    "cd4": "CD4+ T cells", "cd4t": "CD4+ T cells", "cd4+ t cells": "CD4+ T cells",
    "cd4-t-cells": "CD4+ T cells",
    "cd8": "CD8+ T cells", "cd8t": "CD8+ T cells", "cd8+ t cells": "CD8+ T cells",
    "cd8-t-cells": "CD8+ T cells",
    "mono": "Monocytes", "monocyte": "Monocytes", "monocytes": "Monocytes",
    "neu": "Neutrophils", "neutro": "Neutrophils", "neutrophil": "Neutrophils",
    "neutrophils": "Neutrophils", "granulocytes": "Neutrophils",
    "nk": "Natural Killer cells", "nk cells": "Natural Killer cells",
    "natural killer cells": "Natural Killer cells", "natural-killer-cells": "Natural Killer cells",
}


@dataclass(frozen=True)
class ProportionMatrix:
    rows: tuple[str, ...]
    values: dict[str, dict[str, float]]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_policy(path: Path) -> dict:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("schema") != "demethflow-release-validation-policy-v1":
        raise ValueError(f"unsupported validation policy: {path}")
    return policy


def _canonical_cell(value: str) -> str | None:
    cleaned = " ".join(value.strip().lower().replace("_", " ").split())
    if cleaned in {cell.lower() for cell in CANONICAL_CELLS}:
        return next(cell for cell in CANONICAL_CELLS if cell.lower() == cleaned)
    return ALIASES.get(cleaned)


def read_proportions(path: Path) -> ProportionMatrix:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        raw_rows = list(reader)
        fields = reader.fieldnames or []
    if not raw_rows or len(fields) < 2:
        raise ValueError(f"empty/invalid proportion matrix: {path}")
    sample_field = "SampleID" if "SampleID" in fields else fields[0]
    field_by_cell: dict[str, str] = {}
    for field in fields:
        cell = _canonical_cell(field)
        if cell:
            if cell in field_by_cell:
                raise ValueError(f"duplicate canonical cell column {cell!r}: {path}")
            field_by_cell[cell] = field
    missing = [cell for cell in CANONICAL_CELLS if cell not in field_by_cell]
    if missing:
        raise ValueError(f"missing canonical cell columns in {path}: {', '.join(missing)}")
    values: dict[str, dict[str, float]] = {}
    for raw in raw_rows:
        sample = str(raw.get(sample_field, "")).strip().strip('"')
        if not sample or sample in values:
            raise ValueError(f"missing/duplicate SampleID {sample!r}: {path}")
        try:
            values[sample] = {cell: float(raw[field_by_cell[cell]]) for cell in CANONICAL_CELLS}
        except (TypeError, ValueError) as exc:
            raise ValueError(f"non-numeric proportion for {sample!r}: {path}") from exc
    return ProportionMatrix(tuple(values), values)


def structural_metrics(matrix: ProportionMatrix, policy: dict) -> dict:
    finite = 0
    valid = 0
    minimum = math.inf
    maximum = -math.inf
    max_sum_error = 0.0
    structural = policy["structural"]
    for row in matrix.values.values():
        numbers = list(row.values())
        row_finite = all(math.isfinite(value) for value in numbers)
        finite += int(row_finite)
        if row_finite:
            minimum = min(minimum, *numbers)
            maximum = max(maximum, *numbers)
            sum_error = abs(sum(numbers) - 1.0)
            max_sum_error = max(max_sum_error, sum_error)
            if (
                min(numbers) >= structural["minimum_proportion"]
                and max(numbers) <= structural["maximum_proportion"]
                and sum_error <= structural["row_sum_atol"]
            ):
                valid += 1
    total = len(matrix.rows)
    metrics = {
        "sample_count": total,
        "finite_output_rate": finite / total,
        "valid_output_rate": valid / total,
        "minimum_proportion": minimum,
        "maximum_proportion": maximum,
        "maximum_row_sum_error": max_sum_error,
        "missing_cell_types": 0,
    }
    metrics["status"] = "PASS" if (
        metrics["finite_output_rate"] == 1.0
        and metrics["valid_output_rate"] >= structural["valid_output_rate"]
    ) else "FAIL"
    return metrics


def max_absolute_difference(left: ProportionMatrix, right: ProportionMatrix) -> float:
    if set(left.rows) != set(right.rows):
        raise ValueError("proportion matrices have different SampleID sets")
    return max(
        abs(left.values[sample][cell] - right.values[sample][cell])
        for sample in left.rows for cell in CANONICAL_CELLS
    )


def truth_metrics(estimate: ProportionMatrix, truth: ProportionMatrix) -> dict:
    if set(estimate.rows) != set(truth.rows):
        raise ValueError("estimate and truth have different SampleID sets")
    differences = {
        cell: [abs(estimate.values[sample][cell] - truth.values[sample][cell]) for sample in truth.rows]
        for cell in CANONICAL_CELLS
    }
    flat_estimate = [estimate.values[sample][cell] for sample in truth.rows for cell in CANONICAL_CELLS]
    flat_truth = [truth.values[sample][cell] for sample in truth.rows for cell in CANONICAL_CELLS]
    left_mean = sum(flat_estimate) / len(flat_estimate)
    right_mean = sum(flat_truth) / len(flat_truth)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(flat_estimate, flat_truth))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in flat_estimate)
        * sum((b - right_mean) ** 2 for b in flat_truth)
    )
    per_cell = {cell: sum(values) / len(values) for cell, values in differences.items()}
    return {
        "overall_mae": sum(sum(values) for values in differences.values()) / len(flat_truth),
        "overall_pearson": numerator / denominator if denominator else 0.0,
        "maximum_cell_type_mae": max(per_cell.values()),
        "per_cell_type_mae": per_cell,
    }


def truth_status(metrics: dict, policy: dict) -> str:
    limits = policy["truth_accuracy"]
    return "PASS" if (
        metrics["overall_mae"] <= limits["overall_mae_max"]
        and metrics["overall_pearson"] >= limits["overall_pearson_min"]
        and metrics["maximum_cell_type_mae"] <= limits["maximum_cell_type_mae_max"]
    ) else "FAIL"


def quantized_difference(left: ProportionMatrix, right: ProportionMatrix, decimals: int) -> float:
    if set(left.rows) != set(right.rows):
        raise ValueError("proportion matrices have different SampleID sets")
    return max(
        abs(round(left.values[sample][cell], decimals) - round(right.values[sample][cell], decimals))
        for sample in left.rows for cell in CANONICAL_CELLS
    )


def evidence_record(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def write_report(path: Path, payload: dict) -> None:
    report = {
        "schema": "demethflow-release-validation-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
