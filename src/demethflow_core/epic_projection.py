from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import DeMethFlowError, ManifestError
from .manifest import ReferenceBundle
from .util import atomic_write_json, sha256_file


CONTRACT = "array_epic_from_450k_common_cpg_v1"
PROJECTION_SCHEMA = "demethflow-epic-from-450k-projection-v1"
_CG = re.compile(r"^cg[0-9]+$")


@dataclass(frozen=True)
class ProjectionAssets:
    epic_manifest: Path
    hm450_manifest: Path
    common_cpgs: Path
    epic_probe_ids: frozenset[str]
    common_probe_order: tuple[str, ...]
    common_order: tuple[str, ...]
    tool_features: dict[str, tuple[frozenset[str], dict[str, frozenset[str]], dict]]
    minimum_epic_v1_probe_count: int


def is_projection_contract(contract: str) -> bool:
    return contract == CONTRACT


def validate_projection_declaration(bundle: ReferenceBundle) -> None:
    """Validate the immutable declaration; file digests are always contractual."""
    raw = bundle.payload.get("platform_projection")
    if not isinstance(raw, dict) or raw.get("schema") != PROJECTION_SCHEMA:
        raise ManifestError(f"{bundle.selector}: missing {PROJECTION_SCHEMA} platform_projection")
    required_literals = {
        "input_platform": "epic",
        "input_array_version": "EPIC_v1",
        "source_reference_platform": "450k",
        "marker_selection_from_user_data": False,
        "missing_cpg_imputation": False,
    }
    for key, expected in required_literals.items():
        if raw.get(key) != expected:
            raise ManifestError(f"{bundle.selector}: platform_projection.{key} must be {expected!r}")
    for key in ("epic_manifest", "hm450_manifest", "common_cpgs", "source_reference_manifest"):
        item = raw.get(key)
        if not isinstance(item, dict) or not isinstance(item.get("artifact"), str) or not isinstance(item.get("sha256"), str):
            raise ManifestError(f"{bundle.selector}: invalid platform_projection.{key}")
        path = _projection_artifact(bundle, item["artifact"], f"platform_projection.{key}")
        observed = sha256_file(path)
        if observed != item["sha256"]:
            raise ManifestError(
                f"{bundle.selector}: {key} digest mismatch: expected {item['sha256']}, got {observed}"
            )
    source = raw.get("source_reference")
    if not isinstance(source, dict) or source.get("manifest_sha256") != raw["source_reference_manifest"]["sha256"]:
        raise ManifestError(
            f"{bundle.selector}: source reference digest does not match source_reference_manifest"
        )
    if not all(isinstance(source.get(key), str) and source[key] for key in ("reference_id", "version", "selector")):
        raise ManifestError(f"{bundle.selector}: incomplete source_reference identity")
    source_manifest_path = _projection_artifact(
        bundle,
        raw["source_reference_manifest"]["artifact"],
        "platform_projection.source_reference_manifest",
    )
    try:
        source_payload = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{bundle.selector}: invalid source reference manifest: {exc}") from exc
    if (
        source_payload.get("reference_id") != source["reference_id"]
        or source_payload.get("version") != source["version"]
        or f"{source_payload.get('reference_id')}@{source_payload.get('version')}" != source["selector"]
        or source_payload.get("scenario_id") != bundle.scenario_id
        or source_payload.get("source_platform") != "450k"
        or source_payload.get("analysis_contract") != "array_450k_cpg"
    ):
        raise ManifestError(f"{bundle.selector}: source reference identity/scenario/contract mismatch")
    common_count = raw.get("common_cpg_count")
    if not isinstance(common_count, int) or common_count != 452999:
        raise ManifestError(f"{bundle.selector}: common_cpg_count must be frozen at 452999")
    minimum_input = raw.get("minimum_epic_v1_probe_count")
    if not isinstance(minimum_input, int) or minimum_input < 400000:
        raise ManifestError(f"{bundle.selector}: minimum_epic_v1_probe_count must be at least 400000")
    tool_features = raw.get("tool_features")
    if not isinstance(tool_features, dict) or set(tool_features) != set(bundle.tools):
        raise ManifestError(f"{bundle.selector}: platform_projection.tool_features must cover every declared tool")
    for tool, item in tool_features.items():
        if not isinstance(item, dict) or not isinstance(item.get("artifact"), str):
            raise ManifestError(f"{bundle.selector}: invalid tool feature declaration for {tool}")
        path = _projection_artifact(bundle, item["artifact"], f"platform_projection.tool_features.{tool}")
        expected = item.get("sha256")
        if not isinstance(expected, str) or sha256_file(path) != expected:
            raise ManifestError(f"{bundle.selector}: tool feature digest mismatch for {tool}")
        for count_key in ("original_feature_count", "common_feature_count", "minimum_input_overlap"):
            if not isinstance(item.get(count_key), int) or item[count_key] < 0:
                raise ManifestError(f"{bundle.selector}: invalid {tool}.{count_key}")
        fraction = item.get("minimum_input_overlap_fraction")
        if not isinstance(fraction, (int, float)) or not 0 <= float(fraction) <= 1:
            raise ManifestError(f"{bundle.selector}: invalid {tool}.minimum_input_overlap_fraction")
        cell_fraction = item.get("minimum_per_cell_type_retention_fraction")
        if not isinstance(cell_fraction, (int, float)) or not 0 <= float(cell_fraction) <= 1:
            raise ManifestError(
                f"{bundle.selector}: invalid {tool}.minimum_per_cell_type_retention_fraction"
            )
    activation = bundle.payload.get("provenance", {}).get("structural_test_activation")
    structural_test = (
        isinstance(activation, dict)
        and activation.get("test_only") is True
        and activation.get("scientific_use_prohibited") is True
    )
    if structural_test:
        if not (
            bundle.reference_id.startswith("test.structural.")
            and bundle.version.endswith("-structural-test")
            and bundle.payload.get("release_validation", {}).get("publishable") is False
        ):
            raise ManifestError(
                f"{bundle.selector}: structural_test_activation is restricted to non-publishable test bundles"
            )
    else:
        _validate_active_scientific_evidence(bundle)


def _validate_active_scientific_evidence(bundle: ReferenceBundle) -> None:
    """Reject inherited 450K reports masquerading as derived EPIC evidence."""
    release = bundle.payload.get("release_validation", {})
    released_tools = {
        tool for tool, capability in bundle.tools.items()
        if capability.status == "RELEASED_UNVALIDATED"
    }
    released_unvalidated = bool(released_tools)
    if released_unvalidated:
        if bundle.status not in {"RELEASED_UNVALIDATED", "PARTIAL"}:
            raise ManifestError(
                f"{bundle.selector}: released-unvalidated tools require a matching bundle status"
            )
        authorized = release.get("authorized_tools")
        required_release_values = {
            "stage": "formal_offline_release_without_independent_truth",
            "publishable": True,
            "execution_authorized_without_independent_truth": True,
            "independent_epic_v1_truth_available": False,
            "scientific_claims_permitted": False,
            "mandatory_runtime_warning": True,
            "exact_reference_selector_required": True,
            "explicit_analysis_contract_required": True,
            "cross_platform_opt_in_required": True,
        }
        for key, expected in required_release_values.items():
            if release.get(key) != expected:
                raise ManifestError(
                    f"{bundle.selector}: RELEASED_UNVALIDATED requires release_validation.{key}={expected!r}"
                )
        if not isinstance(authorized, list) or set(authorized) != released_tools:
            raise ManifestError(
                f"{bundle.selector}: release_validation.authorized_tools must exactly match "
                "RELEASED_UNVALIDATED capabilities"
            )
        if bundle.status == "RELEASED_UNVALIDATED" and released_tools != set(bundle.tools):
            raise ManifestError(
                f"{bundle.selector}: a RELEASED_UNVALIDATED bundle cannot contain differently labelled tools"
            )
    for tool, capability in bundle.tools.items():
        relative = capability.artifacts.get("scientific_validation")
        if not relative:
            raise ManifestError(
                f"{bundle.selector}: {tool} must declare active EPIC-from-450K scientific_validation"
            )
        path = _projection_artifact(bundle, relative, f"{tool}.scientific_validation")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestError(
                f"{bundle.selector}: invalid active scientific record for {tool}: {exc}"
            ) from exc
        required = {
            "schema": "demethflow-epic-from-450k-scientific-validation-v1",
            "scenario_id": bundle.scenario_id,
            "tool": tool,
            "analysis_contract": CONTRACT,
            "input_array_version": "EPIC_v1",
            "source_reference_platform": "450k",
            "source_450k_result_is_not_inherited": True,
        }
        for key, expected in required.items():
            if record.get(key) != expected:
                raise ManifestError(
                    f"{bundle.selector}: {tool} active scientific record has invalid {key}"
                )
        status = record.get("status")
        if status not in {"NOT_AVAILABLE", "PASS", "FAIL"}:
            raise ManifestError(
                f"{bundle.selector}: {tool} active scientific record has invalid status {status!r}"
            )
        required_gates = {
            "fixed_seed_repeatability",
            "independent_truth_accuracy",
            "canonical_cell_or_anonymous_component_axis",
            "sample_axis",
            "finite_nonnegative_normalized_output",
            "reference_and_projection_provenance",
            "tool_specific_input_overlap",
            "installed_archive_offline_execution",
            "zero_submission_resume",
        }
        declared_gates = record.get("required_gates")
        if record.get("required_repeats") != 2 or not isinstance(declared_gates, list) or not required_gates.issubset(declared_gates):
            raise ManifestError(
                f"{bundle.selector}: {tool} active scientific record omits required repeat or release gates"
            )
        independent = record.get("independent_epic_v1_truth_available") is True
        eligible = record.get("promotion_eligible") is True
        if status == "PASS" and not (independent and eligible):
            raise ManifestError(
                f"{bundle.selector}: {tool} PASS requires independent EPIC-v1 truth and promotion eligibility"
            )
        if status != "PASS" and eligible:
            raise ManifestError(
                f"{bundle.selector}: {tool} non-PASS scientific record cannot be promotion eligible"
            )
        if capability.status == "READY" and status != "PASS":
            raise ManifestError(
                f"{bundle.selector}: READY {tool} requires an active PASS EPIC scientific record"
            )
        if capability.status == "RELEASED_UNVALIDATED":
            if not released_unvalidated or status != "NOT_AVAILABLE" or independent or eligible:
                raise ManifestError(
                    f"{bundle.selector}: RELEASED_UNVALIDATED {tool} must retain an explicit "
                    "NOT_AVAILABLE, non-promoting independent-EPIC science record"
                )
        elif bundle.status == "RELEASED_UNVALIDATED":
            raise ManifestError(
                f"{bundle.selector}: every tool in a RELEASED_UNVALIDATED bundle must use the same status"
            )


def load_projection_assets(
    bundle: ReferenceBundle, selected_tools: Iterable[str] | None = None
) -> ProjectionAssets:
    validate_projection_declaration(bundle)
    raw = bundle.payload["platform_projection"]
    epic_manifest = _projection_artifact(bundle, raw["epic_manifest"]["artifact"], "EPIC manifest")
    hm450_manifest = _projection_artifact(bundle, raw["hm450_manifest"]["artifact"], "HM450 manifest")
    common_cpgs = _projection_artifact(bundle, raw["common_cpgs"]["artifact"], "common CpGs")
    epic_ids = frozenset(_read_manifest_probe_ids(epic_manifest))
    common_probe_order = tuple(_read_probe_list(common_cpgs))
    if len(common_probe_order) != 452999 or len(set(common_probe_order)) != len(common_probe_order):
        raise ManifestError(f"{bundle.selector}: frozen common CpG backbone is not 452,999 unique probes")
    if not set(common_probe_order).issubset(epic_ids):
        raise ManifestError(f"{bundle.selector}: common CpG backbone is not a subset of EPIC v1")
    # The platform manifests contain cg probes plus shared SNP/control probes.
    # Freeze all 452,999 platform IDs for identity/provenance, but only cg IDs
    # enter a methylation deconvolution matrix.
    common_order = tuple(probe for probe in common_probe_order if _CG.fullmatch(probe))
    tool_features = {}
    feature_cache: dict[str, tuple[set[str], dict[str, frozenset[str]]]] = {}
    selected = set(selected_tools) if selected_tools is not None else set(raw["tool_features"])
    for tool, declaration in raw["tool_features"].items():
        if tool not in selected:
            continue
        feature_path = _projection_artifact(bundle, declaration["artifact"], f"{tool} tool features")
        cache_key = declaration["sha256"]
        if cache_key not in feature_cache:
            feature_cache[cache_key] = _read_tool_features(
                feature_path, tuple(cell["cell_type_id"] for cell in bundle.cell_types)
            )
        features, cells = feature_cache[cache_key]
        tool_features[tool] = (frozenset(features), cells, declaration)
    return ProjectionAssets(
        epic_manifest=epic_manifest,
        hm450_manifest=hm450_manifest,
        common_cpgs=common_cpgs,
        epic_probe_ids=epic_ids,
        common_probe_order=common_probe_order,
        common_order=common_order,
        tool_features=tool_features,
        minimum_epic_v1_probe_count=int(raw["minimum_epic_v1_probe_count"]),
    )


def inspect_projection_inputs(
    bundle: ReferenceBundle, input_path: Path, selected_tools: Iterable[str], *,
    allow_structural_fixture: bool = False,
) -> dict:
    selected = tuple(selected_tools)
    assets = load_projection_assets(bundle, selected)
    files = _csv_inputs(input_path)
    duplicate_samples = _global_duplicate_sample_ids(files)
    reports = [
        _inspect_matrix(path, assets, selected, allow_structural_fixture=allow_structural_fixture)
        for path in files
    ]
    status = (
        "PASS"
        if not duplicate_samples and all(item["status"] == "PASS" for item in reports)
        else "FAIL"
    )
    return {
        "schema": "demethflow-epic-from-450k-preflight-v1",
        "status": status,
        "analysis_contract": CONTRACT,
        "input_array_version": "EPIC_v1",
        "epic_manifest_sha256": sha256_file(assets.epic_manifest),
        "hm450_manifest_sha256": sha256_file(assets.hm450_manifest),
        "common_cpg_sha256": sha256_file(assets.common_cpgs),
        "common_probe_count": len(assets.common_probe_order),
        "analysis_common_cpg_count": len(assets.common_order),
        "global_duplicate_sample_ids": duplicate_samples,
        "inputs": reports,
    }


def project_inputs(
    bundle: ReferenceBundle,
    input_path: Path,
    selected_tools: Iterable[str],
    output_root: Path,
    *,
    resume: bool = False,
    allow_structural_fixture: bool = False,
) -> tuple[Path, dict]:
    """Materialize validated matrices without marker selection or CpG imputation."""
    selected = tuple(selected_tools)
    assets = load_projection_assets(bundle, selected)
    files = _csv_inputs(input_path)
    duplicate_samples = _global_duplicate_sample_ids(files)
    if duplicate_samples:
        raise DeMethFlowError(
            "EPIC input sample IDs must be unique across all matrices; duplicated IDs: "
            + ",".join(duplicate_samples)
        )
    safe_names = [_safe_stem(path.stem) for path in files]
    if len(safe_names) != len(set(safe_names)):
        raise DeMethFlowError("EPIC input filenames collide after safe-name normalization")
    cached = _cached_projection(
        bundle=bundle,
        files=files,
        selected=selected,
        output_root=output_root,
        resume=resume,
    )
    if cached is not None:
        return output_root / "nextflow_inputs", cached
    if output_root.exists() and any(output_root.iterdir()):
        raise DeMethFlowError(
            f"projection output already exists without a reusable provenance contract: {output_root}; "
            "use a new output directory"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    nextflow_inputs = output_root / "nextflow_inputs"
    nextflow_inputs.mkdir(parents=True, exist_ok=True)
    reports = []
    for source in files:
        matrix = _read_matrix(source)
        report = _matrix_report(
            source, matrix, assets, selected,
            allow_structural_fixture=allow_structural_fixture,
        )
        if report["status"] != "PASS":
            raise DeMethFlowError(_projection_failure(report))
        sample_root = output_root / _safe_stem(source.stem)
        sample_root.mkdir(parents=True, exist_ok=True)
        retained = set(matrix["values"]).intersection(assets.common_order)
        projected_path = sample_root / "projected_matrix.csv"
        _write_projected(projected_path, matrix["header"], matrix["values"], assets.common_order, retained)
        retained_path = sample_root / "retained_probes.tsv"
        dropped_path = sample_root / "dropped_probes.tsv"
        _write_probe_table(retained_path, ((probe, "EPIC_v1_HM450_common") for probe in assets.common_order if probe in retained))
        _write_probe_table(dropped_path, _dropped_probe_rows(matrix, retained, assets))
        report.update(
            projected_matrix=str(projected_path),
            projected_matrix_sha256=sha256_file(projected_path),
            retained_probes=str(retained_path),
            retained_probes_sha256=sha256_file(retained_path),
            dropped_probes=str(dropped_path),
            dropped_probes_sha256=sha256_file(dropped_path),
        )
        atomic_write_json(sample_root / "projection_qc.json", report)
        staged = nextflow_inputs / f"{_safe_stem(source.stem)}.csv"
        if staged.exists():
            raise DeMethFlowError(f"duplicate projected input name: {staged.name}")
        try:
            os.link(projected_path, staged)
        except OSError:
            staged.write_bytes(projected_path.read_bytes())
        reports.append(report)
    aggregate = {
        "schema": "demethflow-epic-from-450k-projection-run-v1",
        "status": "PASS",
        "analysis_contract": CONTRACT,
        "reference": bundle.selector,
        "reference_digest": bundle.content_digest(),
        "selected_tools": list(selected),
        "input_array_version": "EPIC_v1",
        "source_reference_platform": "450k",
        "marker_selection_from_user_data": False,
        "missing_cpg_imputation": False,
        "global_duplicate_sample_ids": [],
        "epic_manifest": {"path": str(assets.epic_manifest), "sha256": sha256_file(assets.epic_manifest)},
        "hm450_manifest": {"path": str(assets.hm450_manifest), "sha256": sha256_file(assets.hm450_manifest)},
        "common_cpgs": {
            "path": str(assets.common_cpgs),
            "frozen_platform_probe_count": len(assets.common_probe_order),
            "analysis_cg_probe_count": len(assets.common_order),
            "sha256": sha256_file(assets.common_cpgs),
        },
        "inputs": reports,
    }
    atomic_write_json(output_root / "projection_qc.json", aggregate)
    return nextflow_inputs, aggregate


def _cached_projection(
    *, bundle: ReferenceBundle, files: list[Path], selected: tuple[str, ...],
    output_root: Path, resume: bool,
) -> dict | None:
    report_path = output_root / "projection_qc.json"
    if not report_path.is_file():
        return None
    if not resume:
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"cannot resume invalid projection provenance: {report_path}: {exc}") from exc
    expected_inputs = {str(path.resolve()): sha256_file(path) for path in files}
    observed_inputs = {
        str(item.get("input_path")): str(item.get("input_sha256"))
        for item in report.get("inputs", []) if isinstance(item, dict)
    }
    identity_ok = (
        report.get("schema") == "demethflow-epic-from-450k-projection-run-v1"
        and report.get("status") == "PASS"
        and report.get("reference") == bundle.selector
        and report.get("reference_digest") == bundle.content_digest()
        and report.get("selected_tools") == list(selected)
        and observed_inputs == expected_inputs
    )
    if not identity_ok:
        raise DeMethFlowError(
            "projection resume provenance mismatch; input/reference/tools changed and existing outputs will not be overwritten"
        )
    nextflow_inputs = output_root / "nextflow_inputs"
    for item in report["inputs"]:
        projected = Path(str(item.get("projected_matrix", "")))
        staged = nextflow_inputs / f"{_safe_stem(Path(item['input_path']).stem)}.csv"
        expected = item.get("projected_matrix_sha256")
        if (
            not projected.is_file() or not staged.is_file() or not isinstance(expected, str)
            or sha256_file(projected) != expected or sha256_file(staged) != expected
        ):
            raise DeMethFlowError("projection resume output digest mismatch; refusing partial/stale cache")
    cached = json.loads(json.dumps(report))
    cached["cache_reused"] = True
    return cached


def _projection_artifact(bundle: ReferenceBundle, relative: str, label: str) -> Path:
    from .util import resolve_artifact

    return resolve_artifact(bundle.root, relative, label)


def _read_manifest_probe_ids(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames or "probeID" not in reader.fieldnames:
            raise ManifestError(f"platform manifest has no probeID column: {path}")
        values = [str(row["probeID"]).strip() for row in reader if str(row.get("probeID", "")).strip()]
    if len(values) != len(set(values)):
        raise ManifestError(f"duplicate probeID in platform manifest: {path}")
    return values


def _global_duplicate_sample_ids(files: Iterable[Path]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for path in files:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            header = next(csv.reader(handle), [])
        for sample in (value.strip() for value in header[1:]):
            if not sample:
                continue
            if sample in seen:
                duplicates.add(sample)
            seen.add(sample)
    return sorted(duplicates)


def _read_probe_list(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle, delimiter="\t")
        header = next(reader, [])
        if not header or header[0] != "probe_id":
            raise ManifestError(f"invalid common CpG header: {path}")
        values = [row[0].strip() for row in reader if row and row[0].strip()]
    return values


def _read_tool_features(path: Path, cell_types: tuple[str, ...]) -> tuple[set[str], dict[str, frozenset[str]]]:
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
    wildcard_frozen = frozenset(wildcard)
    return features, {
        cell: (wildcard_frozen if not values else frozenset(wildcard.union(values)))
        for cell, values in cells.items()
    }


def _csv_inputs(path: Path) -> list[Path]:
    resolved = path.expanduser().resolve()
    files = [resolved] if resolved.is_file() else sorted(resolved.glob("*.csv")) if resolved.is_dir() else []
    if not files:
        raise DeMethFlowError(f"no CSV input matrices found for EPIC projection: {resolved}")
    return files


def _read_matrix(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as binary:
        for block in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(block)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        if len(header) < 2:
            raise DeMethFlowError(f"EPIC matrix needs a probe column and at least one sample: {path}")
        sample_ids = [value.strip() for value in header[1:]]
        values: dict[str, list[str]] = {}
        order: list[str] = []
        all_order: list[str] = []
        seen_probe_ids: set[str] = set()
        duplicate = non_cg = missing = nonnumeric = nonfinite = out_of_range = malformed = raw_rows = 0
        for row in reader:
            if not row:
                continue
            raw_rows += 1
            if len(row) != len(header):
                malformed += 1
                continue
            probe = row[0].strip()
            if not probe:
                missing += 1
                continue
            if probe in seen_probe_ids:
                duplicate += 1
                continue
            seen_probe_ids.add(probe)
            if not _CG.fullmatch(probe):
                non_cg += 1
            parsed_ok = True
            for raw in row[1:]:
                if raw.strip() == "":
                    missing += 1
                    parsed_ok = False
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    nonnumeric += 1
                    parsed_ok = False
                    continue
                if not math.isfinite(value):
                    nonfinite += 1
                    parsed_ok = False
                elif not 0 <= value <= 1:
                    out_of_range += 1
                    parsed_ok = False
            if parsed_ok:
                all_order.append(probe)
                if _CG.fullmatch(probe):
                    values[probe] = row[1:]
                    order.append(probe)
    return {
        "path": path,
        "header": header,
        "sample_ids": sample_ids,
        "values": values,
        "order": order,
        "all_order": all_order,
        "input_sha256": digest.hexdigest(),
        "raw_probe_count": raw_rows,
        "valid_cg_probe_count": len(values),
        "valid_platform_probe_count": len(all_order),
        "duplicate_probe_count": duplicate,
        "non_cg_probe_count": non_cg,
        "missing_value_count": missing,
        "non_numeric_value_count": nonnumeric,
        "non_finite_value_count": nonfinite,
        "out_of_range_value_count": out_of_range,
        "malformed_row_count": malformed,
        "sample_id_duplicate_count": len(sample_ids) - len(set(sample_ids)),
        "sample_id_missing_count": sum(not value for value in sample_ids),
    }


def _inspect_matrix(
    path: Path, assets: ProjectionAssets, selected_tools: Iterable[str], *,
    allow_structural_fixture: bool = False,
) -> dict:
    return _matrix_report(
        path, _read_matrix(path), assets, tuple(selected_tools),
        allow_structural_fixture=allow_structural_fixture,
    )


def _matrix_report(
    path: Path, matrix: dict, assets: ProjectionAssets, selected_tools: tuple[str, ...], *,
    allow_structural_fixture: bool = False,
) -> dict:
    observed = set(matrix["values"])
    observed_platform = set(matrix["all_order"])
    common = observed.intersection(assets.common_order)
    outside_epic = observed_platform.difference(assets.epic_probe_ids)
    errors = []
    for key, label in (
        ("duplicate_probe_count", "duplicate probe IDs"),
        ("missing_value_count", "missing values"),
        ("non_numeric_value_count", "non-numeric values"),
        ("non_finite_value_count", "non-finite values"),
        ("out_of_range_value_count", "values outside [0,1]"),
        ("malformed_row_count", "malformed rows"),
        ("sample_id_duplicate_count", "duplicate sample IDs"),
        ("sample_id_missing_count", "missing sample IDs"),
    ):
        if matrix[key]:
            errors.append(f"{matrix[key]} {label}")
    if outside_epic:
        errors.append(
            f"{len(outside_epic)} probe IDs are absent from the frozen EPIC v1 manifest; EPIC v2 is unsupported"
        )
    minimum_epic_probes = assets.minimum_epic_v1_probe_count
    if matrix["valid_platform_probe_count"] < minimum_epic_probes and not allow_structural_fixture:
        errors.append(
            f"only {matrix['valid_platform_probe_count']} valid platform probes were supplied; at least "
            f"{minimum_epic_probes} are required to establish EPIC v1 platform identity"
        )
    tools = {}
    for tool in selected_tools:
        if tool not in assets.tool_features:
            errors.append(f"no frozen projection features declared for {tool}")
            continue
        features, cell_features, declaration = assets.tool_features[tool]
        retained = observed.intersection(features)
        original_count = int(declaration["original_feature_count"])
        minimum_count = int(declaration["minimum_input_overlap"])
        minimum_fraction = float(declaration["minimum_input_overlap_fraction"])
        expected_common = int(declaration["common_feature_count"])
        fraction = len(retained) / expected_common if expected_common else 1.0
        cell_retention = {}
        cell_overlap_cache: dict[int, int] = {}
        for cell, probes in cell_features.items():
            cache_key = id(probes)
            if cache_key not in cell_overlap_cache:
                cell_overlap_cache[cache_key] = len(observed.intersection(probes))
            overlap_count = cell_overlap_cache[cache_key]
            cell_retention[cell] = {
                "reference_feature_count": len(probes),
                "input_retained_count": overlap_count,
                "retention_fraction": overlap_count / len(probes) if probes else None,
            }
        minimum_cell_fraction = float(declaration["minimum_per_cell_type_retention_fraction"])
        failed_cells = [
            cell for cell, item in cell_retention.items()
            if item["retention_fraction"] is not None
            and item["retention_fraction"] < minimum_cell_fraction
        ]
        passed = len(retained) >= minimum_count and fraction >= minimum_fraction
        if not passed:
            errors.append(
                f"{tool} overlap {len(retained)}/{expected_common} ({fraction:.6f}) is below "
                f"{minimum_count} and {minimum_fraction:.6f}"
            )
        if failed_cells:
            passed = False
            errors.append(
                f"{tool} per-cell marker retention is below {minimum_cell_fraction:.6f} for: "
                + ",".join(failed_cells)
            )
        tools[tool] = {
            "status": "PASS" if passed else "FAIL",
            "reference_original_feature_count": original_count,
            "reference_common_feature_count": expected_common,
            "input_overlap_count": len(retained),
            "input_overlap_fraction_of_common_reference": fraction,
            "minimum_input_overlap": minimum_count,
            "minimum_input_overlap_fraction": minimum_fraction,
            "minimum_per_cell_type_retention_fraction": minimum_cell_fraction,
            "failed_cell_types": failed_cells,
            "per_cell_type_marker_retention": cell_retention,
        }
    return {
        "schema": PROJECTION_SCHEMA,
        "status": "PASS" if not errors else "FAIL",
        "input_path": str(path.resolve()),
        "input_sha256": matrix["input_sha256"],
        "input_array_version": "EPIC_v1" if not outside_epic else "UNSUPPORTED_OR_NOT_EPIC_V1",
        "platform_identity_gate": (
            "STRUCTURAL_FIXTURE_ONLY" if allow_structural_fixture and matrix["valid_platform_probe_count"] < minimum_epic_probes
            else "PASS" if matrix["valid_platform_probe_count"] >= minimum_epic_probes and not outside_epic
            else "FAIL"
        ),
        "minimum_epic_v1_probe_count": minimum_epic_probes,
        "sample_count": len(matrix["sample_ids"]),
        "raw_epic_probe_count": matrix["raw_probe_count"],
        "valid_platform_probe_count": matrix["valid_platform_probe_count"],
        "valid_cg_probe_count": matrix["valid_cg_probe_count"],
        "hm450_common_probe_count": len(common),
        "overall_retention_fraction": len(common) / len(observed) if observed else 0.0,
        "unsupported_epic_probe_count": len(outside_epic),
        "na_or_missing_value_count": matrix["missing_value_count"],
        "duplicate_probe_count": matrix["duplicate_probe_count"],
        "known_non_cg_platform_probe_count": matrix["non_cg_probe_count"] - len(
            [probe for probe in outside_epic if not _CG.fullmatch(probe)]
        ),
        "unsupported_non_cg_probe_count": len(
            [probe for probe in outside_epic if not _CG.fullmatch(probe)]
        ),
        "non_numeric_value_count": matrix["non_numeric_value_count"],
        "non_finite_value_count": matrix["non_finite_value_count"],
        "out_of_range_value_count": matrix["out_of_range_value_count"],
        "malformed_row_count": matrix["malformed_row_count"],
        "duplicate_sample_id_count": matrix["sample_id_duplicate_count"],
        "missing_sample_id_count": matrix["sample_id_missing_count"],
        "tool_reference_overlap": tools,
        "errors": errors,
    }


def _write_projected(path: Path, header: list[str], values: dict[str, list[str]], order: tuple[str, ...], retained: set[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for probe in order:
            if probe in retained:
                writer.writerow([probe, *values[probe]])


def _dropped_probe_rows(
    matrix: dict, retained: set[str], assets: ProjectionAssets
) -> Iterable[tuple[str, str]]:
    common_platform = set(assets.common_probe_order)
    for probe in matrix["all_order"]:
        if probe in retained:
            continue
        if probe in common_platform and not _CG.fullmatch(probe):
            yield probe, "known_non_cg_platform_probe_not_used_for_deconvolution"
        elif probe not in common_platform:
            yield probe, "not_in_frozen_HM450_common_backbone"


def _write_probe_table(path: Path, rows: Iterable[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["probe_id", "reason"])
        writer.writerows(rows)


def _projection_failure(report: dict) -> str:
    return "EPIC-from-450K projection preflight failed for {}:\n  - {}".format(
        report["input_path"], "\n  - ".join(report["errors"])
    )


def _safe_stem(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe or "matrix"
