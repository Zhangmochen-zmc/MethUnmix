#!/usr/bin/env python3
"""CelFiE native-WGBS interval contract utilities.

The CelFiE atlas is an explicit BED-like, 0-based half-open interval table.
It is deliberately independent from the legacy ``start + 2`` convention:
every marker must supply a real end coordinate and every WGBS record is one
CpG represented as ``[start, start + 1)``.  The module is dependency-free so
it can be staged into the immutable ``wgbs-common`` runtime and called from a
Nextflow work directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Iterable


class ContractError(ValueError):
    """Raised when an atlas or mapped WGBS file violates the CelFiE contract."""


COORDINATE_ALIASES = {
    "chrom": {"chrom", "chr", "chromosome"},
    "start": {"start", "pos"},
    "end": {"end", "stop"},
}
COORDINATE_COLUMNS = ("chrom", "start", "end")


def _normalized_header(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _column_map(fields: Iterable[str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for field in fields:
        normalized = _normalized_header(field)
        for canonical, aliases in COORDINATE_ALIASES.items():
            if normalized in aliases:
                if canonical in mapped:
                    raise ContractError(f"atlas has multiple columns for {canonical!r}")
                mapped[canonical] = field
    missing = [column for column in COORDINATE_COLUMNS if column not in mapped]
    if missing:
        if "end" in missing:
            raise ContractError(
                "CelFiE atlas lacks a formal end column; legacy start+2 inference is forbidden"
            )
        raise ContractError("CelFiE atlas lacks coordinate column(s): " + ", ".join(missing))
    return mapped


def _integer(value: str, *, label: str, row_number: int) -> int:
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"atlas row {row_number}: {label} is not an integer: {value!r}") from exc
    if str(integer) != str(value).strip() and not str(value).strip().startswith("+"):
        # Avoid accepting values such as 12.0; BED coordinates are integers.
        raise ContractError(f"atlas row {row_number}: {label} is not an integer: {value!r}")
    return integer


def _finite_nonnegative(value: str, *, label: str, row_number: int) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"mapped row {row_number}: {label} is not numeric: {value!r}") from exc
    if not math.isfinite(numeric) or numeric < 0:
        raise ContractError(f"mapped row {row_number}: {label} must be finite and non-negative")
    return numeric


def _sort_key(row: dict[str, object]) -> tuple[str, int, int]:
    return (str(row["chrom"]), int(row["start"]), int(row["end"]))


def _reference_pairs(fields: list[str], coordinate_map: dict[str, str]) -> list[tuple[str, str, str]]:
    coordinate_fields = set(coordinate_map.values())
    meth: dict[str, str] = {}
    depth: dict[str, str] = {}
    unexpected: list[str] = []
    for field in fields:
        if field in coordinate_fields:
            continue
        normalized = _normalized_header(field)
        if normalized.endswith("_meth"):
            key = normalized[:-5]
            if not key or key in meth:
                raise ContractError(f"invalid or duplicate methylated reference column {field!r}")
            meth[key] = field
        elif normalized.endswith("_depth"):
            key = normalized[:-6]
            if not key or key in depth:
                raise ContractError(f"invalid or duplicate depth reference column {field!r}")
            depth[key] = field
        else:
            unexpected.append(field)
    if unexpected:
        raise ContractError("atlas has non-CelFiE reference columns: " + ", ".join(unexpected))
    if not meth or set(meth) != set(depth):
        missing_meth = sorted(set(depth) - set(meth))
        missing_depth = sorted(set(meth) - set(depth))
        raise ContractError(
            "atlas methylated/depth reference columns are not paired; "
            f"missing_meth={missing_meth}; missing_depth={missing_depth}"
        )
    return [(key, meth[key], depth[key]) for key in sorted(meth)]


def read_atlas(path: Path) -> tuple[list[dict[str, object]], list[tuple[str, str, str]], dict[str, object]]:
    """Read and validate an explicit CelFiE atlas without modifying it."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = list(reader.fieldnames or [])
        if not fields:
            raise ContractError("CelFiE atlas is empty or has no header")
        coordinate_map = _column_map(fields)
        pairs = _reference_pairs(fields, coordinate_map)
        rows: list[dict[str, object]] = []
        seen: set[tuple[str, int, int]] = set()
        widths: Counter[int] = Counter()
        for row_number, source in enumerate(reader, start=2):
            if not source or all(value in (None, "") for value in source.values()):
                continue
            chrom = str(source.get(coordinate_map["chrom"], "")).strip()
            if not chrom:
                raise ContractError(f"atlas row {row_number}: chrom is empty")
            start = _integer(str(source.get(coordinate_map["start"], "")), label="start", row_number=row_number)
            end = _integer(str(source.get(coordinate_map["end"], "")), label="end", row_number=row_number)
            if start < 0 or end <= start:
                raise ContractError(
                    f"atlas row {row_number}: invalid half-open interval [{start}, {end})"
                )
            key = (chrom, start, end)
            if key in seen:
                raise ContractError(f"atlas row {row_number}: exact duplicate interval {key}")
            seen.add(key)
            for cell, meth_field, depth_field in pairs:
                meth = _finite_nonnegative(str(source.get(meth_field, "")), label=f"{cell}_meth", row_number=row_number)
                depth = _finite_nonnegative(str(source.get(depth_field, "")), label=f"{cell}_depth", row_number=row_number)
                if meth > depth:
                    raise ContractError(
                        f"atlas row {row_number}: {cell}_meth exceeds {cell}_depth"
                    )
            rows.append({"chrom": chrom, "start": start, "end": end, "source": source})
            widths[end - start] += 1
    if not rows:
        raise ContractError("CelFiE atlas has no data rows")
    rows.sort(key=_sort_key)
    return rows, pairs, {
        "row_count": len(rows),
        "width_distribution": {str(width): count for width, count in sorted(widths.items())},
        "cell_types": [cell for cell, _, _ in pairs],
        "coordinate_system": "bed_0_based_half_open",
        "interval_semantics": "explicit_atlas_start_end",
        "overlapping_intervals": "allowed_per_marker",
        "exact_duplicate_intervals": "forbidden",
        "legacy_start_plus_2": "forbidden",
    }


def write_bed(rows: list[dict[str, object]], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        for row in rows:
            writer.writerow((row["chrom"], row["start"], row["end"]))


def read_mapped(path: Path) -> tuple[dict[tuple[str, int, int], tuple[float, float]], dict[str, object]]:
    """Read coordinate-preserving bedtools output and validate the one-row-per-marker invariant."""
    records: dict[tuple[str, int, int], tuple[float, float]] = {}
    nonzero = 0
    meth_total = 0.0
    depth_total = 0.0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row_number, fields in enumerate(reader, start=1):
            if not fields:
                continue
            if len(fields) != 5:
                raise ContractError(
                    f"mapped row {row_number}: expected chrom,start,end,methylated_sum,depth_sum; "
                    f"found {len(fields)} columns"
                )
            chrom = fields[0].strip()
            if not chrom:
                raise ContractError(f"mapped row {row_number}: chrom is empty")
            start = _integer(fields[1], label="start", row_number=row_number)
            end = _integer(fields[2], label="end", row_number=row_number)
            if start < 0 or end <= start:
                raise ContractError(f"mapped row {row_number}: invalid interval [{start}, {end})")
            meth = _finite_nonnegative(fields[3], label="methylated_sum", row_number=row_number)
            depth = _finite_nonnegative(fields[4], label="depth_sum", row_number=row_number)
            if meth > depth:
                raise ContractError(f"mapped row {row_number}: methylated_sum exceeds depth_sum")
            key = (chrom, start, end)
            if key in records:
                raise ContractError(f"mapped row {row_number}: exact duplicate interval {key}")
            records[key] = (meth, depth)
            nonzero += int(depth > 0)
            meth_total += meth
            depth_total += depth
    if not records:
        raise ContractError("mapped file has no data rows")
    return records, {
        "mapped_row_count": len(records),
        "nonzero_window_count": nonzero,
        "zero_coverage_window_count": len(records) - nonzero,
        "methylated_sum_total": meth_total,
        "depth_sum_total": depth_total,
    }


def merge_atlas_and_mapped(atlas_path: Path, mapped_path: Path, output_path: Path, qc_path: Path | None = None) -> dict[str, object]:
    rows, pairs, contract = read_atlas(atlas_path)
    mapped, coverage = read_mapped(mapped_path)
    atlas_keys = {(str(row["chrom"]), int(row["start"]), int(row["end"])) for row in rows}
    mapped_keys = set(mapped)
    missing = sorted(atlas_keys - mapped_keys)
    unexpected = sorted(mapped_keys - atlas_keys)
    if missing or unexpected:
        raise ContractError(
            "atlas/mapped coordinate sets differ; "
            f"missing_mapped={len(missing)} unexpected_mapped={len(unexpected)}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        header = ["chrom", "start", "end", "sample_METH", "sample_DEPTH"]
        for cell, _, _ in pairs:
            header.extend((f"{cell}_meth", f"{cell}_depth"))
        writer.writerow(header)
        for row in rows:
            key = (str(row["chrom"]), int(row["start"]), int(row["end"]))
            meth, depth = mapped[key]
            source = row["source"]
            values: list[object] = [*key, meth, depth]
            for _, meth_field, depth_field in pairs:
                values.extend((source[meth_field], source[depth_field]))
            writer.writerow(values)
    report = {
        "schema": "demethflow-celfie-marker-coverage-v1",
        "status": "PASS",
        "atlas": str(atlas_path),
        "mapped": str(mapped_path),
        "output": str(output_path),
        "contract": contract,
        "coverage": coverage,
        "coordinate_set": {
            "atlas_row_count": len(atlas_keys),
            "mapped_row_count": len(mapped_keys),
            "missing_mapped_count": len(missing),
            "unexpected_mapped_count": len(unexpected),
        },
    }
    if qc_path is not None:
        qc_path.parent.mkdir(parents=True, exist_ok=True)
        qc_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def prepare_atlas(atlas_path: Path, output_path: Path, qc_path: Path | None = None) -> dict[str, object]:
    rows, _, contract = read_atlas(atlas_path)
    write_bed(rows, output_path)
    report = {
        "schema": "demethflow-celfie-atlas-contract-v1",
        "status": "PASS",
        "atlas": str(atlas_path),
        "prepared_bed": str(output_path),
        "contract": contract,
    }
    if qc_path is not None:
        qc_path.parent.mkdir(parents=True, exist_ok=True)
        qc_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-atlas")
    prepare.add_argument("--atlas", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--qc", type=Path)
    merge = subparsers.add_parser("merge")
    merge.add_argument("--atlas", type=Path, required=True)
    merge.add_argument("--mapped", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--qc", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare-atlas":
            report = prepare_atlas(args.atlas, args.output, args.qc)
        else:
            report = merge_atlas_and_mapped(args.atlas, args.mapped, args.output, args.qc)
    except ContractError as exc:
        raise SystemExit(f"CelFiE contract error: {exc}") from exc
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
