#!/usr/bin/env python3
"""Model-region-driven native-WGBS tiling for the strict MEnet adapter."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd


REGION_RE = re.compile(r"^([^:]+):(\d+)-(\d+)$")
NATIVE_WGBS_TILING_STRATEGY = "model_regions_direct_v1"
NATIVE_WGBS_CHUNK_ROWS = 1_000_000


def aggregate_model_region_values(
    input_path: Path,
    regions: list[str],
    *,
    value_column: int,
    value_name: str,
) -> tuple[pd.DataFrame, dict]:
    """Aggregate one native-WGBS table directly onto ordered model regions.

    This intentionally reproduces ``bedtools map -o mean`` for the methylation
    frequency while avoiding upstream MEnet's hard-coded hg38 whole-genome
    window.  Input is streamed in bounded chunks and only model regions are
    retained in memory.
    """
    model_regions = list(map(str, regions))
    model_set = set(model_regions)
    if not model_regions or len(model_set) != len(model_regions):
        raise RuntimeError("MEnet model regions must be a non-empty unique sequence")
    parsed = [REGION_RE.fullmatch(region) for region in model_regions]
    if any(match is None for match in parsed):
        raise RuntimeError("MEnet model contains an invalid region during native tiling")
    widths = {int(match.group(3)) - int(match.group(2)) for match in parsed if match}
    if widths != {1000}:
        raise RuntimeError(
            f"native MEnet tiling requires 1000 bp model regions; observed {sorted(widths)}"
        )

    tile_bp = 1000
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    raw_regions: set[str] = set()
    finite_regions: set[str] = set()
    total_rows = numeric_coordinate_rows = finite_value_rows = matching_rows = 0
    maximum_value = float("-inf")
    try:
        chunks = pd.read_csv(
            input_path,
            sep="\t",
            header=None,
            comment="#",
            usecols=[0, 1, value_column],
            chunksize=NATIVE_WGBS_CHUNK_ROWS,
            compression="infer",
        )
        for chunk in chunks:
            total_rows += int(len(chunk))
            chrom = chunk.iloc[:, 0].astype(str)
            start = pd.to_numeric(chunk.iloc[:, 1], errors="coerce")
            value = pd.to_numeric(chunk.iloc[:, 2], errors="coerce")
            coordinate_ok = start.notna() & (start >= 0)
            numeric_coordinate_rows += int(coordinate_ok.sum())
            left = (start.fillna(0).astype(np.int64) // tile_bp) * tile_bp
            keys = chrom + ":" + left.astype(str) + "-" + (left + tile_bp).astype(str)
            in_model = coordinate_ok & keys.isin(model_set)
            raw_regions.update(keys.loc[in_model].astype(str))
            value_array = value.to_numpy(float)
            finite = pd.Series(np.isfinite(value_array), index=chunk.index)
            finite_value_rows += int(finite.sum())
            if finite.any():
                maximum_value = max(maximum_value, float(value.loc[finite].max()))
            usable = in_model & finite
            matching_rows += int(usable.sum())
            if not usable.any():
                continue
            grouped = pd.DataFrame({
                "region": keys.loc[usable].astype(str).to_numpy(),
                "value": value.loc[usable].astype(float).to_numpy(),
            }).groupby("region", sort=False)["value"].agg(["sum", "count"])
            for region, row in grouped.iterrows():
                key = str(region)
                sums[key] = sums.get(key, 0.0) + float(row["sum"])
                counts[key] = counts.get(key, 0) + int(row["count"])
                finite_regions.add(key)
    except Exception as exc:
        raise RuntimeError(
            f"cannot stream native MEnet {value_name} values from {input_path}: {exc}"
        ) from exc
    if total_rows == 0:
        raise RuntimeError(f"native MEnet input contains no data rows: {input_path}")
    if maximum_value == float("-inf"):
        raise RuntimeError(f"native MEnet input contains no finite {value_name} values: {input_path}")

    scale = 0.01 if maximum_value > 1.0 else 1.0
    observed = [region for region in model_regions if counts.get(region, 0) > 0]
    sample = input_path.name.split(".bis", 1)[0]
    frame = pd.DataFrame(
        {sample: [sums[region] / counts[region] * scale for region in observed]},
        index=observed,
        dtype=float,
    )
    frame.index.name = "CpGs"
    preprocessing_qc = {
        "schema": "demethflow-menet-native-tiling-qc-v1",
        "tiling_strategy": NATIVE_WGBS_TILING_STRATEGY,
        "input_path": str(input_path),
        "input_value_column": int(value_column),
        "input_value_name": value_name,
        "input_value_scale": "percent_to_fraction" if scale == 0.01 else "fraction",
        "total_input_rows": total_rows,
        "numeric_coordinate_rows": numeric_coordinate_rows,
        "finite_value_rows": finite_value_rows,
        "matching_input_rows": matching_rows,
        "raw_input_overlap_regions": len(raw_regions),
        "actual_model_input_overlap_regions": len(finite_regions),
        "model_region_count": len(model_regions),
        "imputed_region_count": len(model_regions) - len(finite_regions),
        "imputed_region_fraction": (len(model_regions) - len(finite_regions)) / len(model_regions),
    }
    frame.attrs["menet_preprocessing_qc"] = preprocessing_qc
    return frame, preprocessing_qc

