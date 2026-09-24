#!/usr/bin/env python3
"""Dynamic DeMethFlow result standardizer for Tsisal."""

import os
import sys
import numpy as np
import pandas as pd

TOOL = 'Tsisal'
ORIENTATION = 'rows'
METRIC_COLUMNS = {
    "p-value", "p.value", "pvalue", "rmse", "correlation", "pearson", "error",
    "residual", "residuals",
}


def read_table(path):
    attempts = [
        dict(sep="\t", index_col=0),
        dict(sep=",", index_col=0),
        dict(sep=None, engine="python", index_col=0),
    ]
    error = None
    for options in attempts:
        try:
            frame = pd.read_csv(path, **options)
            if frame.shape[1] > 0:
                return frame
        except Exception as exc:
            error = exc
    raise RuntimeError(f"cannot parse {path}: {error}")


def process(input_path, output_path):
    frame = read_table(input_path)
    if ORIENTATION == "transpose":
        frame = frame.T
    frame.index = frame.index.astype(str).str.replace('"', '', regex=False).str.replace("'", "", regex=False)
    keep = [column for column in frame.columns if str(column).strip().lower() not in METRIC_COLUMNS]
    frame = frame.loc[:, keep]
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame = frame.loc[:, frame.notna().any(axis=0)]
    if frame.columns.duplicated().any():
        frame = frame.T.groupby(level=0, sort=False).sum().T
    if frame.empty or frame.shape[1] == 0:
        raise RuntimeError(f"{TOOL} produced no numeric cell-type proportions")
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise RuntimeError(f"{TOOL} produced NA or non-finite proportions")
    if values.min() < -1e-8:
        raise RuntimeError(f"{TOOL} produced materially negative proportions")
    # TOAST::Tsisal estimates non-negative component weights but does not
    # guarantee compositional closure.  Close each sample to one in the tool
    # adapter; this is part of the canonical proportion contract, not a method
    # substitution or fallback.
    frame = frame.clip(lower=0.0)
    row_sums = frame.sum(axis=1)
    if (row_sums <= 0).any():
        raise RuntimeError(f"{TOOL} produced a sample with zero total proportion")
    frame = frame.div(row_sums, axis=0)
    if len(frame) == 1 and (not str(frame.index[0]).strip() or str(frame.index[0]).lower() in {"0", "nan"}):
        frame.index = [os.path.basename(output_path).split("_")[0]]
    frame.index.name = "SampleID"
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    frame.to_csv(output_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} INPUT OUTPUT", file=sys.stderr)
        raise SystemExit(2)
    try:
        process(sys.argv[1], sys.argv[2])
    except Exception as exc:
        print(f"[{TOOL}] standardization failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
