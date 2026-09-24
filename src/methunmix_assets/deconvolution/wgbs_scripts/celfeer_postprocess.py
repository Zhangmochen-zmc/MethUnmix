#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd


CANONICAL = {"epithelial": "epithelium"}
NEGATIVE_TOLERANCE = 1e-10


def read_cell_types(path: Path) -> list[str]:
    values = [line.strip().split("\t", 1)[0] for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if values and values[0].lower().replace(" ", "_") in {"cell_type", "celltype"}:
        values = values[1:]
    if not values:
        raise ValueError("CelFEER cell-types file is empty")
    lowered = [value.lower() for value in values]
    if any(count > 1 for count in Counter(lowered).values()):
        raise ValueError("CelFEER cell-types file contains duplicate labels")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Strictly canonicalize CelFEER tissue proportions")
    parser.add_argument("raw_result", type=Path)
    parser.add_argument("cell_types", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    labels = read_cell_types(args.cell_types)
    canonical = [CANONICAL.get(label.lower(), label.lower()) for label in labels]
    if len(canonical) != len(set(canonical)):
        raise ValueError("CelFEER labels collide after canonicalization")
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    sample_ids = metadata.get("sample_ids")
    sample_count = metadata.get("sample_count")
    if not isinstance(sample_ids, list) or sample_count != len(sample_ids) or sample_count < 1:
        raise ValueError("CelFEER input metadata has inconsistent sample IDs")
    if metadata.get("cell_types") != labels or metadata.get("cell_type_count") != len(labels):
        raise ValueError("CelFEER input metadata does not match cell-types artifact")

    raw = pd.read_csv(args.raw_result, sep="\t")
    if raw.shape != (sample_count, len(labels) + 1):
        raise ValueError(
            f"CelFEER output shape is {raw.shape}, expected ({sample_count}, {len(labels) + 1})"
        )
    raw_labels = [str(value).strip() for value in raw.columns[1:]]
    if [value.lower() for value in raw_labels] != [value.lower() for value in labels]:
        raise ValueError(f"CelFEER output labels {raw_labels} do not match reference order {labels}")
    observed_samples = raw.iloc[:, 0].astype(str).tolist()
    if observed_samples != sample_ids:
        raise ValueError(f"CelFEER output sample order {observed_samples} does not match {sample_ids}")
    # pandas 3 may expose a read-only NumPy view here.  The strict adapter
    # intentionally clips only floating-point noise in place, so request an
    # owned, writable array explicitly instead of relying on pandas' copy
    # behavior.
    values = raw.iloc[:, 1:].apply(pd.to_numeric, errors="raise").to_numpy(dtype=float, copy=True)
    if not np.isfinite(values).all():
        raise ValueError("CelFEER output contains NA or infinite values")
    if (values < -NEGATIVE_TOLERANCE).any():
        raise ValueError("CelFEER output contains a substantive negative proportion")
    values[(values < 0) & (values >= -NEGATIVE_TOLERANCE)] = 0.0
    totals = values.sum(axis=1)
    if (totals <= 0).any():
        raise ValueError("CelFEER output contains a row with non-positive total")
    values = values / totals[:, None]
    output = pd.DataFrame(values, columns=canonical)
    output.insert(0, "SampleID", sample_ids)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False)


if __name__ == "__main__":
    main()
