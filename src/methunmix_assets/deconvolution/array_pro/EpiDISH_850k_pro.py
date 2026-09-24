#!/usr/bin/env python3
"""Strict EpiDISH standardizer for EPIC and dynamic bundle references."""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd


TARGETS = [
    "B cells", "CD4+ T cells", "CD8+ T cells", "Monocytes",
    "Neutrophils", "Natural Killer cells",
]
IMMUNE6_GROUPS = {
    "B cells": {"bnv", "bmem"},
    "CD4+ T cells": {"cd4tnv", "cd4tmem", "treg"},
    "CD8+ T cells": {"cd8tnv", "cd8tmem"},
    "Monocytes": {"mono"},
    "Neutrophils": {"neu", "baso", "eos"},
    "Natural Killer cells": {"nk"},
}
METRICS = {"p-value", "p.value", "pvalue", "rmse", "correlation", "pearson", "error", "residual", "residuals"}


def _read(path: str) -> pd.DataFrame:
    error: Exception | None = None
    for options in ({"sep": "\t"}, {"sep": ","}, {"sep": None, "engine": "python"}):
        try:
            frame = pd.read_csv(path, index_col=0, **options)
            if frame.shape[1]:
                return frame
        except Exception as exc:  # pragma: no cover
            error = exc
    raise RuntimeError(f"cannot parse {path}: {error}")


def _clean(frame: pd.DataFrame) -> pd.DataFrame:
    frame.index = frame.index.astype(str).str.replace('"', "", regex=False).str.replace("'", "", regex=False)
    normalized = [str(column).strip().lower() for column in frame.columns]
    if len(set(normalized)) != len(normalized):
        raise RuntimeError("EpiDISH output contains duplicate cell-type columns")
    frame.columns = normalized
    frame = frame.loc[:, [column for column in frame.columns if column not in METRICS]]
    frame = frame.apply(pd.to_numeric, errors="coerce")
    if frame.empty or frame.isna().any().any() or not np.isfinite(frame.to_numpy()).all():
        raise RuntimeError("EpiDISH output contains missing/non-finite values")
    return frame


def _immune6(frame: pd.DataFrame, *, normalize: bool = True) -> pd.DataFrame:
    output = pd.DataFrame(index=frame.index)
    for target, sources in IMMUNE6_GROUPS.items():
        missing = sorted(sources - set(frame.columns))
        if missing:
            raise RuntimeError(
                f"EpiDISH EPIC built-in output is missing source columns for {target}: {', '.join(missing)}"
            )
        output[target] = frame[sorted(sources)].sum(axis=1)
    if (output.to_numpy() < -1e-10).any() or (output.to_numpy() > 1 + 1e-10).any():
        raise RuntimeError("EpiDISH output contains proportions outside [0,1]")
    totals = output.sum(axis=1)
    if (totals <= 0).any() or not np.isfinite(totals.to_numpy()).all():
        raise RuntimeError("EpiDISH immune6 output contains an invalid row total")
    if normalize:
        output = output.div(totals, axis=0)
        if not np.allclose(output.sum(axis=1).to_numpy(), 1.0, rtol=0, atol=1e-4):
            raise RuntimeError("EpiDISH immune6 proportions do not sum to one within 1e-4")
    return output[TARGETS]


def _close_dynamic(frame: pd.DataFrame) -> pd.DataFrame:
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise RuntimeError("EpiDISH output contains non-finite proportions")
    if values.min() < -1e-8:
        raise RuntimeError("EpiDISH output contains materially negative proportions")
    frame = frame.clip(lower=0.0)
    totals = frame.sum(axis=1)
    if (totals <= 0).any() or not np.isfinite(totals.to_numpy()).all():
        raise RuntimeError("EpiDISH output contains an invalid row total")
    return frame.div(totals, axis=0)


def process(input_path: str, output_path: str, profile: str = "dynamic") -> None:
    frame = _clean(_read(input_path))
    if profile == "immune6_epic_builtin_v1":
        frame = _immune6(frame)
    elif profile == "immune6_epic_builtin_legacy_v1":
        frame = _immune6(frame, normalize=False)
    elif profile == "dynamic":
        # Dynamic references expose cell-proportion outputs.  EpiDISH methods
        # can leave a small unclosed residual, so close each non-negative row
        # here as part of the declared canonical contract.
        frame = _close_dynamic(frame)
    else:
        raise RuntimeError(f"unsupported EpiDISH EPIC postprocess profile: {profile}")
    frame.index.name = "SampleID"
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    frame.to_csv(output_path)


if __name__ == "__main__":
    if len(sys.argv) not in {3, 4}:
        print("Usage: EpiDISH_850k_pro.py INPUT OUTPUT [PROFILE]", file=sys.stderr)
        raise SystemExit(2)
    try:
        process(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else "dynamic")
    except Exception as exc:
        print(f"[EpiDISH] standardization failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
