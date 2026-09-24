#!/usr/bin/env python3
"""Compatibility copier for already-extracted dynamic MeDeCom CSV results.

RDS introspection was deliberately removed.  The authoritative extraction now
happens inside medecom_decon.R via MeDeCom::getProportions/getLMCs.
"""

from __future__ import annotations

import csv
import math
import shutil
import sys
from pathlib import Path


def process(input_path: str, output_path: str) -> None:
    source = Path(input_path)
    if source.suffix.lower() != ".csv":
        raise RuntimeError("MeDeCom RDS guessing is unsupported; provide MeDeCom_results.csv")
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fields = reader.fieldnames or []
    components = fields[1:] if fields and fields[0] == "SampleID" else []
    if not rows or not components or components != [f"Component_{i}" for i in range(1, len(components) + 1)]:
        raise RuntimeError("MeDeCom result does not satisfy the dynamic component contract")
    for row in rows:
        values = [float(row[name]) for name in components]
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise RuntimeError("MeDeCom result contains invalid component weights")
        if not math.isclose(sum(values), 1.0, rel_tol=1e-4, abs_tol=1e-6):
            raise RuntimeError("MeDeCom component weights do not sum to one")
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} INPUT_CSV OUTPUT_CSV", file=sys.stderr)
        raise SystemExit(2)
    process(sys.argv[1], sys.argv[2])
