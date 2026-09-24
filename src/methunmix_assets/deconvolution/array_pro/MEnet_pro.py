#!/usr/bin/env python3
"""Dynamic DeMethFlow result standardizer for MEnet."""

import os
import sys
import pandas as pd

TOOL = 'MEnet'
ORIENTATION = 'transpose'
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
