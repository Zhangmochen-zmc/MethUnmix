#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import pandas as pd
import numpy as np


def read_cell_types(path: Path) -> list[str]:
    values = [line.strip().split("\t", 1)[0] for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if values and values[0].lower().replace(" ", "_") in {"cell_type", "celltype"}:
        values = values[1:]
    if not values:
        raise ValueError(f"cell-types file is empty: {path}")
    duplicates = sorted(name for name, count in Counter(value.lower() for value in values).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate cell-type labels: {', '.join(duplicates)}")
    return values


def coordinates(frame: pd.DataFrame, label: str) -> pd.MultiIndex:
    if frame.shape[1] < 3:
        raise ValueError(f"{label} has fewer than three coordinate columns")
    index = pd.MultiIndex.from_frame(frame.iloc[:, :3])
    if index.has_duplicates:
        raise ValueError(f"{label} contains duplicate coordinates")
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a dynamic CelFEER mixture/reference matrix")
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_file", type=Path)
    parser.add_argument("marker_reference", type=Path)
    parser.add_argument("cell_types", type=Path)
    args = parser.parse_args()

    labels = read_cell_types(args.cell_types)
    marker = pd.read_csv(args.marker_reference, sep="\t", header=None)
    expected_width = 3 + 5 * len(labels)
    if marker.shape[1] != expected_width:
        raise ValueError(
            f"marker reference width is {marker.shape[1]}, expected {expected_width}=3+5*{len(labels)}"
        )
    marker_index = coordinates(marker, "marker reference")
    marker_data = marker.iloc[:, 3:].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(marker_data.to_numpy(dtype=float)).all() or (marker_data < 0).any().any():
        raise ValueError("marker reference contains NA, infinity, or negative values")

    files = sorted(args.input_dir.glob("*_binned.txt"))
    if not files:
        files = sorted(args.input_dir.glob("*.txt"))
    if not files:
        raise ValueError(f"no binned PAT inputs found under {args.input_dir}")

    sample_ids: list[str] = []
    sample_blocks: list[pd.DataFrame] = []
    for path in files:
        sample_id = path.name.removesuffix("_binned.txt").removesuffix(".txt")
        if not sample_id:
            raise ValueError(f"empty sample ID derived from {path}")
        sample_ids.append(sample_id)
        frame = pd.read_csv(path, sep="\t", header=None)
        if frame.shape[1] != 8:
            raise ValueError(f"{path} has {frame.shape[1]} columns, expected 8=3+5")
        sample_index = coordinates(frame, str(path))
        missing = marker_index.difference(sample_index)
        if len(missing):
            raise ValueError(f"{path} is missing {len(missing)} marker coordinates")
        frame.index = sample_index
        block = frame.loc[marker_index].iloc[:, 3:].apply(pd.to_numeric, errors="raise").reset_index(drop=True)
        if not np.isfinite(block.to_numpy(dtype=float)).all() or (block < 0).any().any():
            raise ValueError(f"{path} contains NA, infinity, or negative values")
        sample_blocks.append(block)

    duplicates = sorted(name for name, count in Counter(sample_ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate CelFEER sample IDs: {', '.join(duplicates)}")

    common = marker.iloc[:, :3].reset_index(drop=True)
    final = pd.concat([common, *sample_blocks, common, marker_data.reset_index(drop=True)], axis=1)
    header = ["chrom", "start", "end", *sample_ids, "chrom", "start", "end", *labels]
    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    with args.output_file.open("w", encoding="utf-8", newline="") as handle:
        handle.write("\t".join(header) + "\n")
        final.to_csv(handle, sep="\t", header=False, index=False)

    metadata = {
        "schema": "demethflow-celfeer-input-v1",
        "sample_count": len(sample_ids),
        "sample_ids": sample_ids,
        "cell_type_count": len(labels),
        "cell_types": labels,
    }
    metadata_path = args.output_file.with_name(f"{args.output_file.stem}_metadata.json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
