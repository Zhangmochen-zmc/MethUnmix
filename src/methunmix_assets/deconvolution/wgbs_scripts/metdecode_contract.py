#!/usr/bin/env python3
"""Strict native-WGBS interval contract for the MetDecode adapter.

MetDecode consumes region-level methylated and total-CpG counts. This module
keeps the full half-open atlas interval ``[START, END)`` intact from reference
through bedtools aggregation and the final cfDNA input table.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable


class ContractError(ValueError):
    pass


COORDS = ("CHROM", "START", "END")


def _number(value: str, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} is not numeric: {value!r}") from exc
    if not math.isfinite(result) or result < 0:
        raise ContractError(f"{label} must be finite and non-negative: {value!r}")
    return result


def _integer(value: str, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} is not an integer: {value!r}") from exc
    if str(result) != str(value).strip() and not str(value).strip().endswith(".0"):
        raise ContractError(f"{label} is not an integer: {value!r}")
    return result


def _digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalise_header(header: list[str] | None) -> list[str]:
    if not header:
        raise ContractError("atlas is empty or has no header")
    result = [item.strip() for item in header]
    if len(set(result)) != len(result):
        raise ContractError("atlas header contains duplicate column names")
    if [item.upper() for item in result[:3]] != list(COORDS):
        raise ContractError("atlas must begin with CHROM, START, END columns")
    if len(result) < 5 or (len(result) - 3) % 2:
        raise ContractError("atlas requires one or more exact _METH/_DEPTH pairs")
    for index in range(3, len(result), 2):
        meth, depth = result[index:index + 2]
        if not meth.endswith("_METH") or depth != meth[:-5] + "_DEPTH":
            raise ContractError(f"atlas columns {meth!r}, {depth!r} are not an exact pair")
    return result


def read_atlas(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read and validate the exact MetDecode METH/DEPTH table contract."""
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        header = _normalise_header(reader.fieldnames)
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, int, int]] = set()
        for line_number, raw in enumerate(reader, start=2):
            chrom = (raw.get(header[0]) or "").strip()
            start = _integer(raw.get(header[1], ""), f"atlas line {line_number} START")
            end = _integer(raw.get(header[2], ""), f"atlas line {line_number} END")
            if not chrom or start < 0 or end <= start:
                raise ContractError(f"atlas line {line_number}: require non-empty CHROM and 0 <= START < END")
            key = chrom, start, end
            if key in seen:
                raise ContractError(f"atlas line {line_number}: duplicate region {key}")
            seen.add(key)
            row = {column: (raw.get(column) or "").strip() for column in header}
            row[header[0]], row[header[1]], row[header[2]] = chrom, str(start), str(end)
            for index in range(3, len(header), 2):
                methylated = _number(row[header[index]], f"atlas line {line_number} {header[index]}")
                depth = _number(row[header[index + 1]], f"atlas line {line_number} {header[index + 1]}")
                if methylated > depth:
                    raise ContractError(f"atlas line {line_number}: METH exceeds DEPTH")
            rows.append(row)
    if not rows:
        raise ContractError("atlas contains no marker regions")
    rows.sort(key=lambda row: (row[header[0]], int(row[header[1]]), int(row[header[2]])))
    return header, rows


def _key(row: dict[str, str], header: list[str]) -> tuple[str, int, int]:
    return row[header[0]], int(row[header[1]]), int(row[header[2]])


def _write_qc(path: str, payload: dict) -> None:
    with open(path, "w") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")


def prepare_atlas(atlas: str, output: str, qc: str) -> None:
    header, rows = read_atlas(atlas)
    with open(output, "w") as handle:
        for row in rows:
            handle.write("\t".join(map(str, _key(row, header))) + "\n")
    _write_qc(qc, {
        "schema": "demethflow-metdecode-atlas-contract-v1",
        "atlas": os.path.abspath(atlas), "atlas_sha256": _digest(atlas),
        "atlas_marker_count": len(rows),
        "coordinate_semantics": "0-based-half-open-[START,END)",
        "prepared_bed_sha256": _digest(output), "status": "PASS",
    })


def _read_mapped(path: str, expected: set[tuple[str, int, int]]) -> dict[tuple[str, int, int], tuple[float, float]]:
    observed: dict[tuple[str, int, int], tuple[float, float]] = {}
    with open(path, newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for line_number, parts in enumerate(reader, start=1):
            if line_number == 1 and [item.strip().upper() for item in parts] == [*COORDS, "METH", "DEPTH"]:
                continue
            if len(parts) != 5:
                raise ContractError(f"mapped line {line_number}: require CHROM, START, END, METH, DEPTH")
            key = parts[0].strip(), _integer(parts[1], f"mapped line {line_number} START"), _integer(parts[2], f"mapped line {line_number} END")
            if key in observed:
                raise ContractError(f"mapped line {line_number}: duplicate region {key}")
            if key not in expected:
                raise ContractError(f"mapped line {line_number}: unexpected region {key}")
            methylated = _number(parts[3], f"mapped line {line_number} METH")
            depth = _number(parts[4], f"mapped line {line_number} DEPTH")
            if methylated > depth:
                raise ContractError(f"mapped line {line_number}: METH exceeds DEPTH")
            observed[key] = methylated, depth
    missing = expected.difference(observed)
    if missing:
        raise ContractError(f"mapped output omits {len(missing)} atlas regions; first={sorted(missing)[0]}")
    return observed


def finalise_mapped(atlas: str, mapped: str, output: str, qc: str) -> None:
    header, rows = read_atlas(atlas)
    observed = _read_mapped(mapped, {_key(row, header) for row in rows})
    with open(output, "w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow([*COORDS, "METH", "DEPTH"])
        for row in rows:
            chrom, start, end = _key(row, header)
            methylated, depth = observed[(chrom, start, end)]
            writer.writerow([chrom, start, end, f"{methylated:.12g}", f"{depth:.12g}"])
    _write_qc(qc, {
        "schema": "demethflow-metdecode-mapped-contract-v1",
        "atlas_sha256": _digest(atlas), "mapped_sha256": _digest(mapped),
        "marker_count": len(rows), "coordinate_semantics": "0-based-half-open-[START,END)",
        "total_methylated": sum(values[0] for values in observed.values()),
        "total_depth": sum(values[1] for values in observed.values()), "status": "PASS",
    })


def merge_cfdna(atlas: str, mapped: str, output: str, sample_id: str, qc: str) -> None:
    header, rows = read_atlas(atlas)
    observed = _read_mapped(mapped, {_key(row, header) for row in rows})
    sample_columns = [f"{sample_id}_METH", f"{sample_id}_DEPTH"]
    if any(column in header for column in sample_columns):
        raise ContractError(f"sample id {sample_id!r} collides with an atlas profile name")
    with open(output, "w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow([*header[:3], *sample_columns, *header[3:]])
        for row in rows:
            chrom, start, end = _key(row, header)
            methylated, depth = observed[(chrom, start, end)]
            writer.writerow([chrom, start, end, f"{methylated:.12g}", f"{depth:.12g}", *[row[column] for column in header[3:]]])
    _write_qc(qc, {
        "schema": "demethflow-metdecode-cfdna-contract-v1",
        "atlas_sha256": _digest(atlas), "mapped_sha256": _digest(mapped), "cfdna_sha256": _digest(output),
        "sample_id": sample_id, "marker_count": len(rows),
        "coordinate_semantics": "0-based-half-open-[START,END)", "sample_columns": sample_columns,
        "atlas_profile_count": (len(header) - 3) // 2, "status": "PASS",
    })


def independent_oracle(atlas: str, input_bed: str, output: str, qc: str) -> None:
    """Independently aggregate per-CpG values over full atlas intervals.

    The production adapter uses ``bedtools map``.  This verifier instead uses
    a streamed interval sweep after an explicit coordinate sort.  A CpG may
    contribute to every overlapping DMR, which is the required overlapping
    window semantic; it is never replaced by a 2 bp surrogate.
    """
    header, rows = read_atlas(atlas)
    marker_rows = [(_key(row, header), 0.0, 0.0) for row in rows]
    by_chrom: dict[str, list[tuple[tuple[str, int, int], float, float]]] = {}
    for key, methylated, depth in marker_rows:
        by_chrom.setdefault(key[0], []).append((key, methylated, depth))
    totals = {key: [0.0, 0.0] for key, _, _ in marker_rows}
    atlas_chroms = set(by_chrom)
    observed_chroms: set[str] = set()
    input_rows = 0
    matched_cpg_rows = 0
    previous: tuple[str, int, int] | None = None

    # Do not call bedtools here: this is intentionally a distinct oracle.
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    process = subprocess.Popen(
        ["sort", "-k1,1", "-k2,2n", "-k3,3n", input_bed],
        stdout=subprocess.PIPE, text=True, env=environment,
    )
    assert process.stdout is not None
    chrom_state: dict[str, tuple[int, list[tuple[tuple[str, int, int], float, float]]]] = {}
    try:
        for line_number, line in enumerate(process.stdout, start=1):
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                raise ContractError(f"input BED line {line_number}: require at least five tab-separated columns")
            chrom = parts[0].strip()
            start = _integer(parts[1], f"input BED line {line_number} START")
            end = _integer(parts[2], f"input BED line {line_number} END")
            methylated = _number(parts[3], f"input BED line {line_number} METH")
            depth = _number(parts[4], f"input BED line {line_number} DEPTH")
            if not chrom or start < 0 or end <= start or methylated > depth:
                raise ContractError(f"input BED line {line_number}: invalid interval or METH/DEPTH")
            order_key = chrom, start, end
            if previous is not None and order_key < previous:
                raise ContractError("independent oracle input sort invariant failed")
            previous = order_key
            input_rows += 1
            observed_chroms.add(chrom)
            markers = by_chrom.get(chrom)
            if not markers:
                continue
            pointer, active = chrom_state.get(chrom, (0, []))
            active = [item for item in active if item[0][2] > start]
            while pointer < len(markers) and markers[pointer][0][1] < end:
                item = markers[pointer]
                if item[0][2] > start:
                    active.append(item)
                pointer += 1
            chrom_state[chrom] = pointer, active
            overlap = False
            for key, _, _ in active:
                if key[1] < end and key[2] > start:
                    totals[key][0] += methylated
                    totals[key][1] += depth
                    overlap = True
            if overlap:
                matched_cpg_rows += 1
    finally:
        process.stdout.close()
    if process.wait() != 0:
        raise ContractError("external coordinate sort for independent oracle failed")
    if observed_chroms and not atlas_chroms.intersection(observed_chroms):
        raise ContractError(
            "input BED chromosome naming has no overlap with atlas chromosome naming"
        )

    with open(output, "w", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow([*COORDS, "METH", "DEPTH"])
        for row in rows:
            key = _key(row, header)
            methylated, depth = totals[key]
            writer.writerow([*key, f"{methylated:.12g}", f"{depth:.12g}"])
    _write_qc(qc, {
        "schema": "demethflow-metdecode-independent-oracle-v1",
        "atlas_sha256": _digest(atlas), "input_bed_sha256": _digest(input_bed),
        "oracle_sha256": _digest(output), "coordinate_semantics": "0-based-half-open-[START,END)",
        "atlas_marker_count": len(rows), "input_cpg_row_count": input_rows,
        "matched_cpg_row_count": matched_cpg_rows,
        "total_methylated": sum(values[0] for values in totals.values()),
        "total_depth": sum(values[1] for values in totals.values()), "status": "PASS",
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare-atlas", "finalise-mapped", "merge-cfdna", "independent-oracle"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--atlas", required=True)
        sub.add_argument("--output", required=True)
        sub.add_argument("--qc", required=True)
        if command not in ("prepare-atlas", "independent-oracle"):
            sub.add_argument("--mapped", required=True)
        if command == "independent-oracle":
            sub.add_argument("--input-bed", required=True)
        if command == "merge-cfdna":
            sub.add_argument("--sample-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare-atlas":
            prepare_atlas(args.atlas, args.output, args.qc)
        elif args.command == "finalise-mapped":
            finalise_mapped(args.atlas, args.mapped, args.output, args.qc)
        elif args.command == "merge-cfdna":
            merge_cfdna(args.atlas, args.mapped, args.output, args.sample_id, args.qc)
        else:
            independent_oracle(args.atlas, args.input_bed, args.output, args.qc)
    except ContractError as exc:
        print(f"MetDecode contract error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
