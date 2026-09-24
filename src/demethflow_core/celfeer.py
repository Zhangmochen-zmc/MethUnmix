from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path

from .errors import DeMethFlowError
from .util import sha256_file


CANONICAL_LABELS = {"epithelial": "epithelium"}
IMMUNE6_LEGACY_LABELS = {
    "b-cells": "bcell",
    "cd4-t-cells": "cd4",
    "cd8-t-cells": "cd8",
    "monocytes": "mono",
    "neutrophils": "neu",
    "natural-killer-cells": "nk",
}


def read_cell_types(path: Path) -> list[str]:
    """Read a one-column CelFEER cell-types file and reject ambiguity."""
    try:
        with path.open(encoding="utf-8-sig") as handle:
            rows = [line.rstrip("\r\n") for line in handle]
    except OSError as exc:
        raise DeMethFlowError(f"Cannot read CelFEER cell-types file: {path}") from exc
    values = [row.split("\t", 1)[0].strip() for row in rows if row.strip()]
    if values and values[0].lower().replace(" ", "_") in {"cell_type", "celltype"}:
        values = values[1:]
    if not values or any(not value for value in values):
        raise DeMethFlowError(f"CelFEER cell-types file contains no labels: {path}")
    duplicates = sorted(name for name, count in Counter(value.lower() for value in values).items() if count > 1)
    if duplicates:
        raise DeMethFlowError(f"CelFEER cell-types contain duplicate labels: {', '.join(duplicates)}")
    return values


def canonical_cell_types(values: list[str]) -> list[str]:
    output = [CANONICAL_LABELS.get(value.strip().lower(), value.strip().lower()) for value in values]
    duplicates = sorted(name for name, count in Counter(output).items() if count > 1)
    if duplicates:
        raise DeMethFlowError(f"CelFEER labels collide after canonicalization: {', '.join(duplicates)}")
    return output


def labels_from_manifest(cell_types: list[dict[str, object]]) -> list[str]:
    """Create a compatibility sidecar for immutable legacy CelFEER bundles."""
    output: list[str] = []
    for cell in cell_types:
        cell_id = str(cell.get("cell_type_id", "")).strip()
        output.append(IMMUNE6_LEGACY_LABELS.get(cell_id, cell_id))
    if not output or any(not value for value in output) or len(output) != len(set(output)):
        raise DeMethFlowError("Reference manifest cannot provide unambiguous CelFEER labels")
    return output


def inspect_marker_reference(path: Path, cell_types: list[str]) -> dict[str, int]:
    expected_width = 3 + 5 * len(cell_types)
    rows = 0
    duplicate_count = 0
    na_count = 0
    negative_count = 0
    zero_blocks = 0
    coordinates: set[tuple[str, int, int]] = set()
    try:
        handle = path.open(encoding="utf-8-sig")
    except OSError as exc:
        raise DeMethFlowError(f"Cannot read CelFEER marker reference: {path}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) != expected_width:
                raise DeMethFlowError(
                    f"CelFEER marker width mismatch at {path}:{line_number}: "
                    f"observed {len(fields)}, expected {expected_width}=3+5*{len(cell_types)}"
                )
            try:
                coordinate = (fields[0], int(fields[1]), int(fields[2]))
            except ValueError as exc:
                raise DeMethFlowError(f"Invalid CelFEER marker coordinate at {path}:{line_number}") from exc
            if coordinate in coordinates:
                duplicate_count += 1
            coordinates.add(coordinate)
            values: list[float] = []
            for value in fields[3:]:
                try:
                    number = float(value)
                except ValueError:
                    number = math.nan
                if not math.isfinite(number):
                    na_count += 1
                elif number < 0:
                    negative_count += 1
                values.append(number)
            for offset in range(0, len(values), 5):
                block = values[offset : offset + 5]
                if all(math.isfinite(number) and number == 0 for number in block):
                    zero_blocks += 1
            rows += 1
    if rows == 0:
        raise DeMethFlowError(f"CelFEER marker reference is empty: {path}")
    if duplicate_count or na_count or negative_count or zero_blocks:
        raise DeMethFlowError(
            f"CelFEER marker reference failed structural validation: duplicates={duplicate_count}, "
            f"NA_or_inf={na_count}, negative={negative_count}, zero_depth_blocks={zero_blocks}"
        )
    return {
        "marker_count": rows,
        "expected_reference_width": expected_width,
        "observed_reference_width": expected_width,
        "coordinate_duplicate_count": duplicate_count,
        "NA_count": na_count,
        "negative_count": negative_count,
        "zero_depth_cell_marker_block_count": zero_blocks,
    }


def markers_missing_from_read_bins(markers: Path, read_bins: Path) -> set[tuple[str, int, int]]:
    """Return exact marker intervals absent from a build-specific bin index."""
    try:
        coordinates = set(_coordinates(markers, header=False))
        return _missing_from_bins(coordinates, read_bins)
    except OSError as exc:
        raise DeMethFlowError(f"Cannot read CelFEER read bins: {read_bins}") from exc


def discover_pat_samples(path: Path) -> list[tuple[str, Path]]:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        files = [resolved] if resolved.name.endswith((".pat", ".pat.gz")) else []
    elif resolved.is_dir():
        pat_root = resolved / "pat" if (resolved / "pat").is_dir() else resolved
        files = sorted(item for item in pat_root.rglob("*") if item.is_file() and item.name.endswith((".pat", ".pat.gz")))
    else:
        files = []
    samples: list[tuple[str, Path]] = []
    seen: dict[str, Path] = {}
    for item in files:
        sample_id = item.name[:-7] if item.name.endswith(".pat.gz") else item.name[:-4]
        if sample_id in seen:
            raise DeMethFlowError(
                f"Duplicate CelFEER PAT sample ID {sample_id!r}: {seen[sample_id]} and {item}"
            )
        seen[sample_id] = item
        samples.append((sample_id, item))
    return samples


def build_reference_qc(
    *,
    scenario_id: str,
    markers: Path,
    cell_types_file: Path,
    marker_selection: Path,
    unique_regions: Path,
    read_bins: Path,
    genome_build: str = "hg38",
    expected_cell_types: list[str] | None = None,
    expected_markers_per_cell: int = 100,
    cross_scenario: dict | None = None,
) -> dict:
    if genome_build not in {"hg19", "hg38"}:
        raise DeMethFlowError(f"Unsupported CelFEER genome build: {genome_build}")
    cell_types = read_cell_types(cell_types_file)
    if expected_cell_types is not None and cell_types != expected_cell_types:
        raise DeMethFlowError(
            f"CelFEER cell-type order for {scenario_id} is {cell_types}, expected {expected_cell_types}"
        )
    marker_qc = inspect_marker_reference(markers, cell_types)
    marker_coordinates = _coordinates(markers, header=False)
    selection_coordinates, tissue_numbers = _selection_contract(marker_selection)
    unique_coordinates = _coordinates(unique_regions, header=True)
    if marker_coordinates != selection_coordinates or marker_coordinates != unique_coordinates:
        raise DeMethFlowError(f"CelFEER marker coordinate files are not identical for {scenario_id}")
    counts = Counter(tissue_numbers)
    expected_tissues = list(range(len(cell_types)))
    if sorted(counts) != expected_tissues:
        raise DeMethFlowError(
            f"CelFEER tissue number must cover 0..{len(cell_types) - 1}; observed {sorted(counts)}"
        )
    marker_count_per_cell = [counts[index] for index in expected_tissues]
    if any(count != expected_markers_per_cell for count in marker_count_per_cell):
        raise DeMethFlowError(
            f"CelFEER marker count per cell must be {expected_markers_per_cell}; observed {marker_count_per_cell}"
        )
    if marker_qc["marker_count"] != len(cell_types) * expected_markers_per_cell:
        raise DeMethFlowError(
            f"CelFEER marker count mismatch: {marker_qc['marker_count']} != "
            f"{len(cell_types)}*{expected_markers_per_cell}"
        )
    missing = _missing_from_bins(set(marker_coordinates), read_bins)
    if missing:
        raise DeMethFlowError(
            f"CelFEER has {len(missing)} markers absent from {genome_build} read bins"
        )
    return {
        "schema": "demethflow-celfeer-reference-qc-v1",
        "scenario_id": scenario_id,
        "genome_build": genome_build,
        "cell_type_count": len(cell_types),
        "cell_type_order": cell_types,
        **marker_qc,
        "marker_count_per_cell": marker_count_per_cell,
        "markers_present_in_read_bins": True,
        "markers_missing_from_read_bins": 0,
        "marker_file_sha256": sha256_file(markers),
        "cell_types_sha256": sha256_file(cell_types_file),
        "marker_selection_sha256": sha256_file(marker_selection),
        "unique_regions_sha256": sha256_file(unique_regions),
        "common_data_module": {
            "module_id": f"CelFEER-{genome_build}-data",
            "version": "2.0.0-dev",
        },
        "cross_scenario": cross_scenario or {},
        "structural_status": "PASS",
        "scientific_status": "NOT_RUN",
        "scientific_reason": "scenario-specific simulation validation is pending",
    }


def write_reference_qc(path: Path, **kwargs) -> dict:
    payload = build_reference_qc(**kwargs)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _coordinates(path: Path, *, header: bool) -> list[tuple[str, int, int]]:
    output: list[tuple[str, int, int]] = []
    with path.open(encoding="utf-8-sig") as handle:
        if header:
            next(handle, None)
        for line_number, line in enumerate(handle, 2 if header else 1):
            if not line.strip():
                continue
            fields = line.rstrip("\r\n").split("\t")
            try:
                output.append((fields[0], int(fields[1]), int(fields[2])))
            except (IndexError, ValueError) as exc:
                raise DeMethFlowError(f"Invalid CelFEER coordinate at {path}:{line_number}") from exc
    if len(output) != len(set(output)):
        raise DeMethFlowError(f"CelFEER coordinate file contains duplicates: {path}")
    return output


def _selection_contract(path: Path) -> tuple[list[tuple[str, int, int]], list[int]]:
    coordinates: list[tuple[str, int, int]] = []
    tissues: list[int] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"chrom", "start", "end", "tissue number"}
        if not required.issubset(reader.fieldnames or []):
            raise DeMethFlowError(f"CelFEER marker selection lacks required columns: {path}")
        for line_number, row in enumerate(reader, 2):
            try:
                coordinates.append((row["chrom"], int(row["start"]), int(row["end"])))
                tissues.append(int(row["tissue number"]))
            except (TypeError, ValueError) as exc:
                raise DeMethFlowError(f"Invalid CelFEER marker selection at {path}:{line_number}") from exc
    return coordinates, tissues


def _missing_from_bins(markers: set[tuple[str, int, int]], read_bins: Path) -> set[tuple[str, int, int]]:
    missing = set(markers)
    with read_bins.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not missing:
                break
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) >= 3:
                try:
                    missing.discard((fields[0], int(fields[1]), int(fields[2])))
                except ValueError:
                    continue
    return missing
