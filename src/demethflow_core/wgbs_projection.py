"""Strict, build-locked projection of native WGBS BED into array CpG space.

This module deliberately contains no deconvolution adapter.  It materializes a
validated CpG x sample CSV and then lets the established 450K/EPIC Nextflow
workflows run unchanged.  A derived route is therefore auditable without
creating a second implementation of ARIC, EDec, EpiSCORE, MEnet, or any other
array method.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import DeMethFlowError, ManifestError
from .manifest import ReferenceBundle, is_wgbs_derived_array_contract
from .util import atomic_write_json, sha256_file, stable_digest


EPIC_CONTRACT = "wgbs_derived_epic_cpg_v1"
HM450_CONTRACT = "wgbs_derived_450k_cpg_v1"
CONTRACTS = {EPIC_CONTRACT: "epic", HM450_CONTRACT: "450k"}
PROJECTION_SCHEMA = "demethflow-wgbs-derived-array-projection-v1"
RUN_SCHEMA = "demethflow-wgbs-derived-array-projection-run-v1"
COORDINATE_SYSTEM = "0_based_half_open_bed_v1"
_CG = re.compile(r"^cg[0-9]+$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
# Mapping tables naturally contain only array-addressable chromosomes, whereas
# a valid WGBS BED can additionally carry mitochondrial CpGs.  The latter must
# be recorded as unmatched rather than silently rewritten or rejected as a
# malformed row.  This fixed declaration is deliberately small: arbitrary
# contig names (for example ``1``, ``MT`` or ``chrUn``) remain invalid.
CANONICAL_INPUT_CHROMOSOMES = frozenset(
    [f"chr{number}" for number in range(1, 23)] + ["chrX", "chrY", "chrM"]
)


@dataclass(frozen=True)
class WgbsProjectionAssets:
    mapping_path: Path
    target_platform: str
    genome_build: str
    probe_order: tuple[str, ...]
    coordinate_to_probes: dict[tuple[str, int, int], tuple[str, ...]]
    allowed_chromosomes: frozenset[str]
    accepted_input_chromosomes: frozenset[str]
    map_summary: dict
    tool_features: dict[str, tuple[frozenset[str], dict[str, frozenset[str]], dict]]


def target_platform_for_contract(contract: str) -> str:
    try:
        return CONTRACTS[contract]
    except KeyError as exc:
        raise ManifestError(f"Unsupported WGBS-derived array contract: {contract}") from exc


def is_wgbs_projection_contract(contract: str) -> bool:
    return contract in CONTRACTS


def validate_wgbs_projection_declaration(
    bundle: ReferenceBundle, *, verify_files: bool = False
) -> None:
    """Validate a derived-reference declaration and every immutable asset."""
    if not is_wgbs_derived_array_contract(bundle.contract):
        raise ManifestError(f"{bundle.selector}: not a WGBS-derived array reference")
    raw = bundle.payload.get("wgbs_projection")
    if not isinstance(raw, dict) or raw.get("schema") != PROJECTION_SCHEMA:
        raise ManifestError(f"{bundle.selector}: missing {PROJECTION_SCHEMA} wgbs_projection")
    target = target_platform_for_contract(bundle.contract)
    required = {
        "input_platform": "wgbs",
        "target_platform": target,
        "coordinate_system": COORDINATE_SYSTEM,
        "missing_cpg_imputation": False,
        "marker_selection_from_user_data": False,
        "automatic_coordinate_shift": False,
        "liftover": False,
    }
    for key, expected in required.items():
        if raw.get(key) != expected:
            raise ManifestError(f"{bundle.selector}: wgbs_projection.{key} must be {expected!r}")
    expected_measurement = f"WGBS_DERIVED_{target.upper()}"
    declared_measurement = raw.get("input_measurement_type")
    manifest_measurement = bundle.payload.get("input_measurement_type")
    if declared_measurement is not None or manifest_measurement is not None:
        if declared_measurement != expected_measurement or manifest_measurement != expected_measurement:
            raise ManifestError(f"{bundle.selector}: manifest input_measurement_type does not match projection contract")
    if raw.get("genome_build") != bundle.payload.get("genome_build"):
        raise ManifestError(f"{bundle.selector}: wgbs_projection genome build does not match manifest")
    mapping = _declared_file(bundle, raw, "mapping", verify_checksum=verify_files)
    source_manifest = _declared_file(
        bundle, raw, "source_reference_manifest", verify_checksum=verify_files
    )
    source = raw.get("source_reference")
    if not isinstance(source, dict):
        raise ManifestError(f"{bundle.selector}: missing wgbs_projection.source_reference")
    try:
        source_payload = json.loads(source_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{bundle.selector}: invalid copied source reference manifest: {exc}") from exc
    selector = f"{source_payload.get('reference_id')}@{source_payload.get('version')}"
    if (
        source.get("selector") != selector
        or source.get("reference_id") != source_payload.get("reference_id")
        or source.get("version") != source_payload.get("version")
        or source_payload.get("scenario_id") != bundle.scenario_id
        or source_payload.get("source_platform") != target
        or source_payload.get("analysis_contract") != f"array_{target}_cpg"
    ):
        raise ManifestError(f"{bundle.selector}: source reference identity/scenario/target mismatch")
    if bundle.payload.get("genome_build") == "hg19" and bundle.version >= "1.0.1":
        audit_path = _declared_file(
            bundle, raw, "hg19_annotation_audit", verify_checksum=verify_files
        )
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"{bundle.selector}: invalid hg19 annotation audit: {exc}") from exc
        comparison = audit.get("comparison", {})
        decision = audit.get("release_decision", {})
        if (
            audit.get("schema") != "demethflow-hg19-wgbs-derived-annotation-audit-v1"
            or audit.get("status") != "PASS"
            or audit.get("genome_build") != "hg19"
            or raw["mapping"]["sha256"] not in {
                item.get("sha256") for item in audit.get("applicable_route_mappings", {}).values()
                if isinstance(item, dict)
            }
            or comparison.get("changed_coordinate_count") != 143
            or comparison.get("new_cg_probe_count") != 14
            or comparison.get("legacy_only_cg_probe_count") != 0
            or decision.get("mixing_prohibited") is not True
            or decision.get("decision") != "supplied_hg19_mapping_replaces_legacy_annotation_for_wgbs_derived_routes"
        ):
            raise ManifestError(f"{bundle.selector}: hg19 annotation audit does not bind the required mapping decision")
    declared_summary = raw.get("mapping_summary")
    if not isinstance(declared_summary, dict):
        raise ManifestError(f"{bundle.selector}: missing wgbs_projection.mapping_summary")
    for key in (
        "raw_row_count", "usable_cg_probe_count", "excluded_non_cg_probe_count",
        "duplicate_cg_probe_count", "duplicate_coordinate_count", "multi_probe_coordinate_count",
    ):
        if not isinstance(declared_summary.get(key), int) or declared_summary[key] < 0:
            raise ManifestError(f"{bundle.selector}: invalid declared mapping summary {key}")
    if verify_files:
        # Full structural recount belongs to explicit integrity checks and
        # release assembly.  A normal catalog lookup only checks immutable
        # paths/declarations, matching the rest of the reference catalog's
        # lazy checksum policy.
        summary = _mapping_summary(mapping, track_duplicates=True)
        for key in (
            "raw_row_count", "usable_cg_probe_count", "excluded_non_cg_probe_count",
            "duplicate_cg_probe_count", "duplicate_coordinate_count", "multi_probe_coordinate_count",
        ):
            if declared_summary.get(key) != summary[key]:
                raise ManifestError(
                    f"{bundle.selector}: mapping summary mismatch for {key}: "
                    f"declared {declared_summary.get(key)!r}, observed {summary[key]!r}"
                )
    expected_chromosomes = raw.get("allowed_chromosomes")
    if not isinstance(expected_chromosomes, list) or not expected_chromosomes or any(
        not isinstance(value, str) or not value.startswith("chr") for value in expected_chromosomes
    ):
        raise ManifestError(f"{bundle.selector}: invalid allowed_chromosomes declaration")
    if verify_files and set(expected_chromosomes) != set(summary["allowed_chromosomes"]):
        raise ManifestError(f"{bundle.selector}: allowed_chromosomes does not match mapping")
    accepted_input = raw.get("accepted_input_chromosomes")
    # Historical immutable candidates predate the explicit input-contig
    # contract and remain listable for forensic reproducibility.  Current
    # release selectors always declare it; an undeclared historical selector
    # is intentionally restricted to its mapping contigs and cannot silently
    # broaden input acceptance.
    if accepted_input is None:
        accepted_input = expected_chromosomes
    elif (
        not isinstance(accepted_input, list)
        or not accepted_input
        or len(accepted_input) != len(set(accepted_input))
        or not set(accepted_input).issubset(CANONICAL_INPUT_CHROMOSOMES | set(expected_chromosomes))
        or not set(expected_chromosomes).issubset(set(accepted_input))
        or "chrM" not in accepted_input
    ):
        raise ManifestError(
            f"{bundle.selector}: accepted_input_chromosomes must explicitly declare canonical WGBS contigs"
        )
    features = raw.get("tool_features")
    if not isinstance(features, dict) or set(features) != set(bundle.tools):
        raise ManifestError(f"{bundle.selector}: tool_features must cover every declared tool")
    for tool, declaration in features.items():
        if not isinstance(declaration, dict):
            raise ManifestError(f"{bundle.selector}: invalid WGBS projection feature declaration for {tool}")
        feature_path = _declared_file(
            bundle, declaration, "artifact", label=f"{tool} tool features", verify_checksum=verify_files
        )
        for key in ("original_feature_count", "target_feature_count", "minimum_input_overlap"):
            if not isinstance(declaration.get(key), int) or declaration[key] < 0:
                raise ManifestError(f"{bundle.selector}: invalid {tool}.{key}")
        for key in ("minimum_input_overlap_fraction", "minimum_per_cell_type_retention_fraction"):
            if not isinstance(declaration.get(key), (int, float)) or not 0 <= float(declaration[key]) <= 1:
                raise ManifestError(f"{bundle.selector}: invalid {tool}.{key}")
        if verify_files:
            feature_summary = _summarize_tool_features(
                feature_path, tuple(cell["cell_type_id"] for cell in bundle.cell_types)
            )
            if not feature_summary["feature_count"] and tool != "MeDeCom":
                raise ManifestError(f"{bundle.selector}: {tool} has no frozen projection features")
            if declaration["target_feature_count"] != feature_summary["feature_count"]:
                raise ManifestError(f"{bundle.selector}: {tool} target_feature_count mismatch")
            if tool != "MeDeCom" and not feature_summary["declared_cell_types"]:
                raise ManifestError(f"{bundle.selector}: {tool} feature declaration is unreadable")
    _validate_release_disclosure(bundle)


def _validate_release_disclosure(bundle: ReferenceBundle) -> None:
    """Derived routes have no target-scenario WGBS truth and must say so honestly."""
    release = bundle.payload.get("release_validation", {})
    expected = {
        "stage": "formal_offline_release_without_target_wgbs_truth",
        "publishable": True,
        "scientific_validation_status": "NOT_PERFORMED",
        "scientific_claims_permitted": False,
        "mandatory_runtime_warning": True,
        "exact_reference_selector_required": True,
        "explicit_analysis_contract_required": True,
        "cross_platform_opt_in_required": True,
    }
    for key, value in expected.items():
        if release.get(key) != value:
            raise ManifestError(f"{bundle.selector}: release_validation.{key} must be {value!r}")
    if bundle.status != "RELEASED_UNVALIDATED":
        raise ManifestError(f"{bundle.selector}: WGBS-derived reference must be RELEASED_UNVALIDATED")
    for tool, capability in bundle.tools.items():
        if capability.status != "RELEASED_UNVALIDATED":
            raise ManifestError(f"{bundle.selector}: {tool} must be RELEASED_UNVALIDATED")
        record_path = capability.artifacts.get("scientific_validation")
        if not record_path:
            raise ManifestError(f"{bundle.selector}: {tool} lacks WGBS-derived validation disclosure")
        path = bundle.artifact(tool, "scientific_validation")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"{bundle.selector}: invalid {tool} validation disclosure: {exc}") from exc
        if (
            record.get("schema") != "demethflow-wgbs-derived-array-validation-v1"
            or record.get("status") != "NOT_PERFORMED"
            or record.get("analysis_contract") != bundle.contract
            or record.get("scenario_id") != bundle.scenario_id
            or record.get("tool") != tool
            or record.get("target_wgbs_truth_available") is not False
            or record.get("promotion_eligible") is not False
        ):
            raise ManifestError(f"{bundle.selector}: invalid WGBS-derived validation disclosure for {tool}")


def load_wgbs_projection_assets(
    bundle: ReferenceBundle, selected_tools: Iterable[str] | None = None
) -> WgbsProjectionAssets:
    validate_wgbs_projection_declaration(bundle)
    raw = bundle.payload["wgbs_projection"]
    mapping = _declared_file(bundle, raw, "mapping")
    summary = _mapping_summary(mapping, include_coordinates=True, track_duplicates=True)
    selected = set(selected_tools) if selected_tools is not None else set(bundle.tools)
    tool_features = {}
    for tool, declaration in raw["tool_features"].items():
        if tool not in selected:
            continue
        path = _declared_file(bundle, declaration, "artifact", label=f"{tool} tool features")
        tool_features[tool] = (*_read_tool_features(path, tuple(cell["cell_type_id"] for cell in bundle.cell_types)), declaration)
    return WgbsProjectionAssets(
        mapping_path=mapping,
        target_platform=str(raw["target_platform"]),
        genome_build=str(raw["genome_build"]),
        probe_order=tuple(summary["probe_order"] or ()),
        coordinate_to_probes=summary["coordinate_to_probes"],
        allowed_chromosomes=frozenset(summary["allowed_chromosomes"]),
        accepted_input_chromosomes=frozenset(raw["accepted_input_chromosomes"]),
        map_summary={key: value for key, value in summary.items() if key not in {"probe_order", "coordinate_to_probes"}},
        tool_features=tool_features,
    )


def inspect_wgbs_projection_inputs(
    bundle: ReferenceBundle, input_path: Path, selected_tools: Iterable[str]
) -> dict:
    assets = load_wgbs_projection_assets(bundle, selected_tools)
    samples = _read_wgbs_samples(input_path, assets)
    return _projection_report(bundle, assets, samples, tuple(selected_tools))


def project_wgbs_inputs(
    bundle: ReferenceBundle,
    input_path: Path,
    selected_tools: Iterable[str],
    output_root: Path,
    *,
    resume: bool = False,
) -> tuple[Path, dict]:
    """Materialize one complete cohort matrix with no CpG imputation."""
    selected = tuple(selected_tools)
    assets = load_wgbs_projection_assets(bundle, selected)
    samples = _read_wgbs_samples(input_path, assets)
    report = _projection_report(bundle, assets, samples, selected)
    if report["status"] != "PASS":
        raise DeMethFlowError(_projection_failure(report))
    cached = _cached_projection(bundle, report, output_root, resume)
    if cached is not None:
        return output_root / "nextflow_inputs", cached
    if output_root.exists() and any(output_root.iterdir()):
        raise DeMethFlowError(
            f"projection output already exists without matching provenance: {output_root}; use a new output directory"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    cohort = output_root / "cohort"
    cohort.mkdir()
    shared = set.intersection(*(set(item["values"]) for item in samples)) if samples else set()
    projected = cohort / "projected_matrix.csv"
    with projected.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["CpG", *(item["sample_id"] for item in samples)])
        for probe in assets.probe_order:
            if probe in shared:
                writer.writerow([probe, *(f"{item['values'][probe]:.12g}" for item in samples)])
    retained = cohort / "retained_probes.tsv"
    _write_probe_table(retained, ((probe, "observed_in_every_cohort_sample") for probe in assets.probe_order if probe in shared))
    missing = cohort / "unobserved_cg_probes.tsv"
    _write_probe_table(missing, ((probe, "not_observed_in_every_cohort_sample_no_imputation") for probe in assets.probe_order if probe not in shared))
    report.update({
        "projected_matrix": str(projected),
        "projected_matrix_sha256": sha256_file(projected),
        "retained_probes": str(retained),
        "retained_probes_sha256": sha256_file(retained),
        "unobserved_cg_probes": str(missing),
        "unobserved_cg_probes_sha256": sha256_file(missing),
        "cache_reused": False,
    })
    atomic_write_json(cohort / "projection_qc.json", report)
    nextflow = output_root / "nextflow_inputs"
    nextflow.mkdir()
    staged = nextflow / "cohort.csv"
    try:
        os.link(projected, staged)
    except OSError:
        staged.write_bytes(projected.read_bytes())
    aggregate = {
        "schema": RUN_SCHEMA,
        "status": "PASS",
        "analysis_contract": bundle.contract,
        "input_measurement_type": f"WGBS_DERIVED_{assets.target_platform.upper()}",
        "reference": bundle.selector,
        "reference_digest": bundle.content_digest(),
        "selected_tools": list(selected),
        "mapping": {"path": str(assets.mapping_path), "sha256": sha256_file(assets.mapping_path)},
        "coordinate_system": COORDINATE_SYSTEM,
        "automatic_coordinate_shift": False,
        "missing_cpg_imputation": False,
        "inputs": report["inputs"],
        "shared_cg_probe_count": len(shared),
        "tool_reference_overlap": report["tool_reference_overlap"],
        "projected_matrix": str(projected),
        "projected_matrix_sha256": sha256_file(projected),
        "cache_reused": False,
    }
    atomic_write_json(output_root / "projection_qc.json", aggregate)
    return nextflow, aggregate


def _declared_file(
    bundle: ReferenceBundle,
    raw: dict,
    key: str,
    *,
    label: str | None = None,
    verify_checksum: bool = True,
) -> Path:
    item = raw if key == "artifact" else raw.get(key)
    if not isinstance(item, dict) or not isinstance(item.get("artifact"), str) or not isinstance(item.get("sha256"), str):
        raise ManifestError(f"{bundle.selector}: invalid {label or key} declaration")
    from .util import resolve_artifact

    path = resolve_artifact(bundle.root, item["artifact"], f"wgbs_projection.{label or key}")
    if not path.is_file() or (verify_checksum and sha256_file(path) != item["sha256"]):
        raise ManifestError(f"{bundle.selector}: digest mismatch for {label or key}")
    return path


def _mapping_summary(
    path: Path,
    *,
    include_coordinates: bool = False,
    include_probe_order: bool = False,
    track_duplicates: bool = False,
) -> dict:
    """Stream mapping metadata, retaining the full probe axis only when needed.

    Catalog discovery validates several immutable 450K/EPIC-derived bundles
    in one interpreter.  It needs counts, not a second 0.5--0.9M probe list;
    making that distinction keeps offline ``reference list`` and release
    assembly bounded in memory.
    """
    probe_order: list[str] | None = [] if (include_coordinates or include_probe_order) else None
    coordinate_to_probes: dict[tuple[str, int, int], list[str]] | None = {} if include_coordinates else None
    seen_coordinates: set[str] | None = set() if (track_duplicates and not include_coordinates) else None
    seen_probes: set[str] | None = set() if track_duplicates else None
    allowed: set[str] = set()
    raw = usable = controls = duplicate_probes = duplicates = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for line_number, row in enumerate(reader, 1):
            if not row:
                continue
            raw += 1
            if len(row) != 4:
                raise ManifestError(f"{path}:{line_number}: mapping requires exactly four tab-separated fields")
            chrom, start_text, end_text, probe = (item.strip() for item in row)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ManifestError(f"{path}:{line_number}: mapping coordinates must be integers") from exc
            if not chrom.startswith("chr") or start < 0 or end < start or not probe:
                raise ManifestError(f"{path}:{line_number}: invalid mapping coordinate or probe")
            allowed.add(chrom)
            if not _CG.fullmatch(probe):
                controls += 1
                continue
            if end != start + 1:
                raise ManifestError(
                    f"{path}:{line_number}: cg probe {probe} violates {COORDINATE_SYSTEM}"
                )
            if seen_probes is not None:
                if probe in seen_probes:
                    duplicate_probes += 1
                    continue
                seen_probes.add(probe)
            usable += 1
            if probe_order is not None:
                probe_order.append(probe)
            coordinate = (chrom, start, end)
            if coordinate_to_probes is not None:
                coordinate_to_probes.setdefault(coordinate, []).append(probe)
            elif seen_coordinates is not None:
                coordinate_key = f"{chrom}\0{start}\0{end}"
                if coordinate_key in seen_coordinates:
                    duplicates += 1
                else:
                    seen_coordinates.add(coordinate_key)
    if not usable:
        raise ManifestError(f"{path}: mapping has no usable cg probes")
    if coordinate_to_probes is not None:
        duplicates = sum(1 for probes in coordinate_to_probes.values() if len(probes) > 1)
    return {
        "raw_row_count": raw,
        "usable_cg_probe_count": usable,
        "excluded_non_cg_probe_count": controls,
        "duplicate_cg_probe_count": duplicate_probes,
        "duplicate_coordinate_count": duplicates,
        "multi_probe_coordinate_count": duplicates,
        "allowed_chromosomes": sorted(allowed),
        "probe_order": probe_order,
        "coordinate_to_probes": (
            {key: tuple(value) for key, value in coordinate_to_probes.items()}
            if coordinate_to_probes is not None else None
        ),
    }


def _read_tool_features(path: Path, cell_types: tuple[str, ...]) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    features: set[str] = set()
    cells: dict[str, set[str]] = {cell: set() for cell in cell_types}
    wildcard: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames or not {"probe_id", "cell_type_id"}.issubset(reader.fieldnames):
            raise ManifestError(f"invalid tool feature table: {path}")
        for row in reader:
            probe = str(row.get("probe_id", "")).strip()
            cell = str(row.get("cell_type_id", "")).strip()
            if not probe:
                continue
            features.add(probe)
            if cell == "*":
                wildcard.add(probe)
            elif cell in cells:
                cells[cell].add(probe)
            else:
                raise ManifestError(f"unknown cell_type_id {cell!r} in {path}")
    universal = frozenset(wildcard)
    return frozenset(features), {
        cell: frozenset(universal.union(values)) for cell, values in cells.items()
    }


def _summarize_tool_features(path: Path, cell_types: tuple[str, ...]) -> dict:
    """Validate a frozen feature table without retaining its large feature axis.

    Immutable table digests are validated separately.  Catalog discovery only
    needs to prove the schema, declared cell identifiers and row count; making
    a 12-way wildcard expansion here used over a gigabyte per EPIC bundle.
    Actual projection still loads the selected tool feature axis and performs
    the full per-cell overlap gate.
    """
    valid_cells = set(cell_types)
    count = 0
    declared_cells: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames or not {"probe_id", "cell_type_id"}.issubset(reader.fieldnames):
            raise ManifestError(f"invalid tool feature table: {path}")
        for row in reader:
            probe = str(row.get("probe_id", "")).strip()
            cell = str(row.get("cell_type_id", "")).strip()
            if not probe:
                continue
            if cell == "*":
                declared_cells.update(valid_cells)
            elif cell in valid_cells:
                declared_cells.add(cell)
            else:
                raise ManifestError(f"unknown cell_type_id {cell!r} in {path}")
            count += 1
    return {"feature_count": count, "declared_cell_types": declared_cells}


def _bed_inputs(path: Path) -> list[Path]:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        files = [resolved] if resolved.name.endswith((".bed", ".bed.gz")) else []
    elif resolved.is_dir():
        root = resolved / "bed" if (resolved / "bed").is_dir() else resolved
        files = sorted(set(root.rglob("*.bed")) | set(root.rglob("*.bed.gz")))
    else:
        files = []
    if not files:
        raise DeMethFlowError(
            "WGBS-derived array projection requires build-matched six-column .bed/.bed.gz input; "
            "BAM, CRAM, and PAT are not direct projection inputs"
        )
    return files


def _read_wgbs_samples(input_path: Path, assets: WgbsProjectionAssets) -> list[dict]:
    paths = _bed_inputs(input_path)
    samples: list[dict] = []
    used: set[str] = set()
    for path in paths:
        sample_id = _safe_stem(_bed_stem(path))
        if sample_id in used:
            raise DeMethFlowError(f"WGBS-derived projection sample IDs collide: {sample_id}")
        used.add(sample_id)
        samples.append(_read_wgbs_sample(path, sample_id, assets))
    return samples


def _read_wgbs_sample(path: Path, sample_id: str, assets: WgbsProjectionAssets) -> dict:
    opener = gzip.open if path.name.endswith(".gz") else open
    digest = hashlib.sha256()
    counts: dict[tuple[str, int, int], list[float]] = {}
    raw = valid = unmatched = duplicate = invalid = multi_probe = 0
    raw_chromosomes: dict[str, int] = {}
    matched_chromosomes: dict[str, int] = {}
    min_depth: float | None = None
    max_depth: float | None = None
    total_depth = 0.0
    beta_sum = 0.0
    min_beta: float | None = None
    max_beta: float | None = None
    with path.open("rb") as binary:
        for block in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(block)
    try:
        handle = opener(path, "rt", encoding="utf-8")
    except OSError as exc:
        raise DeMethFlowError(f"cannot read WGBS BED {path}: {exc}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            raw += 1
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 6:
                raise DeMethFlowError(f"{path}:{line_number}: expected exactly six tab-separated BED columns")
            chrom, start_text, end_text, meth_text, depth_text, beta_text = fields
            raw_chromosomes[chrom] = raw_chromosomes.get(chrom, 0) + 1
            try:
                start, end = int(start_text), int(end_text)
                methylated, depth, beta = float(meth_text), float(depth_text), float(beta_text)
            except ValueError as exc:
                raise DeMethFlowError(f"{path}:{line_number}: non-numeric WGBS BED field") from exc
            if (
                chrom not in assets.accepted_input_chromosomes
                or start < 0
                or end != start + 1
                or not math.isfinite(methylated)
                or not math.isfinite(depth)
                or not math.isfinite(beta)
                or depth <= 0
                or methylated < 0
                or methylated > depth
                or not 0 <= beta <= 1
                or not math.isclose(methylated / depth, beta, abs_tol=0.001)
            ):
                invalid += 1
                raise DeMethFlowError(
                    f"{path}:{line_number}: requires declared canonical chromosome names and "
                    f"{COORDINATE_SYSTEM} six-column BED with beta=methylated_count/total_depth; "
                    "automatic coordinate shifts are prohibited"
                )
            valid += 1
            coordinate = (chrom, start, end)
            if coordinate not in assets.coordinate_to_probes:
                unmatched += 1
                continue
            if coordinate in counts:
                duplicate += 1
                counts[coordinate][0] += methylated
                counts[coordinate][1] += depth
            else:
                counts[coordinate] = [methylated, depth]
            if len(assets.coordinate_to_probes[coordinate]) > 1:
                multi_probe += 1
            matched_chromosomes[chrom] = matched_chromosomes.get(chrom, 0) + 1
            total_depth += depth
            min_depth = depth if min_depth is None else min(min_depth, depth)
            max_depth = depth if max_depth is None else max(max_depth, depth)
            beta_sum += beta
            min_beta = beta if min_beta is None else min(min_beta, beta)
            max_beta = beta if max_beta is None else max(max_beta, beta)
    values: dict[str, float] = {}
    for coordinate, (methylated, depth) in counts.items():
        value = methylated / depth
        for probe in assets.coordinate_to_probes[coordinate]:
            values[probe] = value
    if not values:
        raise DeMethFlowError(
            f"{path}: zero exact matches to the build-locked {assets.genome_build} mapping; "
            "the input may be 1-based or use another genome build, and no automatic shift is allowed"
        )
    return {
        "sample_id": sample_id,
        "input_path": str(path.resolve()),
        "input_sha256": digest.hexdigest(),
        "raw_row_count": raw,
        "valid_row_count": valid,
        "valid_matched_row_count": valid - unmatched,
        "unmatched_row_count": unmatched,
        "duplicate_input_coordinate_count": duplicate,
        "matched_multi_probe_coordinate_row_count": multi_probe,
        "invalid_row_count": invalid,
        "exact_coordinate_match_count": len(counts),
        "matched_probe_count": len(values),
        "mapped_cg_coverage_fraction": len(values) / len(assets.probe_order),
        "depth": {
            "minimum": min_depth,
            "maximum": max_depth,
            "mean": total_depth / (valid - unmatched) if valid > unmatched else None,
            "total": total_depth,
        },
        "beta": {
            "minimum": min_beta,
            "maximum": max_beta,
            "mean_matched_raw_row_beta": beta_sum / (valid - unmatched) if valid > unmatched else None,
        },
        "chromosome_names": {
            "normalization": "none",
            "raw_row_counts": dict(sorted(raw_chromosomes.items())),
            "matched_row_counts": dict(sorted(matched_chromosomes.items())),
        },
        "values": values,
    }


def _projection_report(
    bundle: ReferenceBundle, assets: WgbsProjectionAssets, samples: list[dict], selected: tuple[str, ...]
) -> dict:
    shared = set.intersection(*(set(item["values"]) for item in samples)) if samples else set()
    nonconstant_shared = sum(
        1
        for probe in shared
        if max(sample["values"][probe] for sample in samples)
        - min(sample["values"][probe] for sample in samples) > 1e-12
    )
    minimum_matches = int(bundle.payload["wgbs_projection"].get("minimum_exact_coordinate_matches", 100))
    # Keep failures that make the whole projection impossible separate from
    # per-tool feature-overlap failures.  This lets ``--tools all`` preserve
    # the established offline policy of skipping an input-incompatible tool
    # while still refusing any coordinate/build/coverage violation for the
    # cohort itself.
    structural_errors: list[str] = []
    tool_errors: dict[str, str] = {}
    if len(shared) < minimum_matches:
        structural_errors.append(
            f"only {len(shared)} CpGs are observed in every sample; at least {minimum_matches} exact build-locked matches are required"
        )
    tools = {}
    for tool in selected:
        features, by_cell, declaration = assets.tool_features[tool]
        overlap = shared.intersection(features)
        expected = int(declaration["target_feature_count"])
        minimum = int(declaration["minimum_input_overlap"])
        fraction = len(overlap) / expected if expected else 1.0
        minimum_fraction = float(declaration["minimum_input_overlap_fraction"])
        per_cell = {}
        failed_cells = []
        cell_min = float(declaration["minimum_per_cell_type_retention_fraction"])
        for cell, features_for_cell in by_cell.items():
            count = len(shared.intersection(features_for_cell))
            retention = count / len(features_for_cell) if features_for_cell else None
            per_cell[cell] = {
                "reference_feature_count": len(features_for_cell),
                "input_retained_count": count,
                "retention_fraction": retention,
            }
            if retention is not None and retention < cell_min:
                failed_cells.append(cell)
        passed = len(overlap) >= minimum and fraction >= minimum_fraction and not failed_cells
        if not passed:
            tool_errors[tool] = (
                f"{tool} overlap {len(overlap)}/{expected} ({fraction:.6f}) is below its "
                "frozen projection policy"
            )
        tools[tool] = {
            "status": "PASS" if passed else "FAIL",
            "reference_original_feature_count": int(declaration["original_feature_count"]),
            "reference_target_feature_count": expected,
            "input_overlap_count": len(overlap),
            "input_overlap_fraction_of_target_reference": fraction,
            "minimum_input_overlap": minimum,
            "minimum_input_overlap_fraction": minimum_fraction,
            "minimum_per_cell_type_retention_fraction": cell_min,
            "failed_cell_types": failed_cells,
            "per_cell_type_marker_retention": per_cell,
        }
    return {
        "schema": PROJECTION_SCHEMA,
        "status": "PASS" if not structural_errors and not tool_errors else "FAIL",
        "analysis_contract": bundle.contract,
        "input_measurement_type": f"WGBS_DERIVED_{assets.target_platform.upper()}",
        "target_platform": assets.target_platform,
        "genome_build": assets.genome_build,
        "coordinate_system": COORDINATE_SYSTEM,
        "automatic_coordinate_shift": False,
        "liftover": False,
        "missing_cpg_imputation": False,
        "mapping": {
            "path": str(assets.mapping_path),
            "sha256": sha256_file(assets.mapping_path),
            **assets.map_summary,
        },
        "sample_count": len(samples),
        "shared_cg_probe_count": len(shared),
        "nonconstant_shared_cg_probe_count": nonconstant_shared,
        "shared_cg_coverage_fraction": len(shared) / len(assets.probe_order),
        "minimum_exact_coordinate_matches": minimum_matches,
        "inputs": [{key: value for key, value in item.items() if key != "values"} for item in samples],
        "tool_reference_overlap": tools,
        "structural_errors": structural_errors,
        "tool_errors": tool_errors,
        "errors": [*structural_errors, *tool_errors.values()],
    }


def _cached_projection(bundle: ReferenceBundle, report: dict, root: Path, resume: bool) -> dict | None:
    if not root.exists():
        return None
    report_path = root / "projection_qc.json"
    if not resume:
        return None
    if not report_path.is_file():
        raise DeMethFlowError(f"projection resume requested but provenance is absent: {report_path}")
    try:
        cached = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"cannot read projection resume provenance: {exc}") from exc
    observed = [item["input_sha256"] for item in report["inputs"]]
    previous = [item.get("input_sha256") for item in cached.get("inputs", [])]
    if (
        cached.get("schema") != RUN_SCHEMA
        or cached.get("reference") != bundle.selector
        or cached.get("reference_digest") != bundle.content_digest()
        or observed != previous
        or not (root / "nextflow_inputs" / "cohort.csv").is_file()
    ):
        raise DeMethFlowError("projection resume provenance mismatch; input/reference changed or cache is incomplete")
    expected = cached.get("projected_matrix_sha256")
    matrix = root / "cohort" / "projected_matrix.csv"
    if not isinstance(expected, str) or not matrix.is_file() or sha256_file(matrix) != expected:
        raise DeMethFlowError("projection resume output digest mismatch")
    cached["cache_reused"] = True
    return cached


def _write_probe_table(path: Path, rows: Iterable[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["probe_id", "reason"])
        writer.writerows(rows)


def _bed_stem(path: Path) -> str:
    return path.name[:-7] if path.name.endswith(".bed.gz") else path.stem


def _safe_stem(value: str) -> str:
    safe = _SAFE.sub("_", value).strip("._")
    return safe or "sample"


def _projection_failure(report: dict) -> str:
    return "WGBS-derived array projection preflight failed:\n  - " + "\n  - ".join(report["errors"])
