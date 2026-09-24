#!/usr/bin/env python3
"""Strictly standardize one explicit MEnet MajorGroup result."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-id", required=True)
    args = parser.parse_args()
    if args.input.name != "cell_proportion_MajorGroup.csv":
        raise SystemExit(f"refusing non-MajorGroup MEnet output: {args.input}")
    frame = pd.read_csv(args.input, index_col=0)
    if frame.empty or frame.shape[1] != 1:
        raise SystemExit(f"expected one-sample MEnet MajorGroup matrix, observed {frame.shape}")
    if frame.index.isna().any() or frame.index.duplicated().any():
        raise SystemExit("MEnet MajorGroup labels must be non-empty and unique")
    values = frame.apply(pd.to_numeric, errors="raise").to_numpy(float)
    if not np.isfinite(values).all() or (values < -1e-10).any():
        raise SystemExit("MEnet MajorGroup output contains non-finite or negative values")
    if not np.allclose(values.sum(axis=0), 1.0, rtol=1e-5, atol=1e-7):
        raise SystemExit("MEnet MajorGroup output is not normalized")
    output = frame.T
    output.index = [args.sample_id]
    output.index.name = "SampleID"
    output.to_csv(args.output)
    if not args.output.is_file() or args.output.stat().st_size == 0:
        raise SystemExit("MEnet standardizer failed to create a non-empty result")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
