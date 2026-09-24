from __future__ import annotations

import csv
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import PROJECT_ROOT
from .util import atomic_write_json


METHYLBERT_READY_POLICY = {"overall_pearson_min": 0.80}
EPISCORE_ARRAY_CROSS_PLATFORM_V2 = {
    "overall_mae_max": 0.10,
    "overall_pearson_min": 0.80,
    "maximum_cell_type_mae_max": 0.23,
    "valid_output_rate_min": 1.0,
    "missing_cell_types_max": 0,
    "repeat_max_absolute_difference_max": 1e-6,
}


def thresholds_for(tool: str, policy_id: str | None = None) -> dict[str, float]:
    """Return a named tool policy; there is deliberately no global default."""
    if tool == "EpiSCORE" and policy_id == "episcore-array-cross-platform-v2":
        return dict(EPISCORE_ARRAY_CROSS_PLATFORM_V2)
    if tool == "MethylBERT":
        return dict(METHYLBERT_READY_POLICY)
    return {}


def metrics_pass(item: dict, *, tool: str, policy_id: str | None = None) -> bool | None:
    thresholds = thresholds_for(tool, policy_id)
    if not thresholds:
        return None
    for name, threshold in thresholds.items():
        metric, direction = name.rsplit("_", 1)
        value = item.get(metric)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            return False
        if direction == "min" and float(value) < threshold:
            return False
        if direction == "max" and float(value) > threshold:
            return False
    return True


def approved_policy_result(metrics: dict, tool: str, policy: object) -> str:
    """Evaluate only a matching, explicitly owner-approved tool policy.

    With no approved policy, scientific metrics are report-only. Malformed
    approved policy fails closed and cannot promote a generated reference.
    """
    if policy is None:
        return "NOT_ASSESSED"
    if not isinstance(policy, dict):
        raise ValueError("approved scientific policy must be a JSON object")
    if policy.get("approval_status") != "OWNER_APPROVED":
        return "NOT_ASSESSED"
    if policy.get("tool") != tool or not isinstance(policy.get("policy_id"), str) or not policy["policy_id"].strip():
        raise ValueError("approved scientific policy must identify the matching tool and policy_id")
    thresholds = policy.get("thresholds")
    if not isinstance(thresholds, dict) or not thresholds:
        raise ValueError("approved scientific policy must contain non-empty thresholds")
    directions = {
        "overall_mae": "max",
        "overall_pearson": "min",
        "maximum_cell_type_mae": "max",
        "valid_output_rate": "min",
        "missing_cell_types": "max",
        "repeat_max_absolute_difference": "max",
    }
    for metric, rule in thresholds.items():
        direction = directions.get(metric)
        if direction is None or not isinstance(rule, dict) or set(rule) != {direction}:
            raise ValueError(f"unsupported or malformed scientific policy metric: {metric}")
        threshold = rule[direction]
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not math.isfinite(float(threshold)):
            raise ValueError(f"{metric} policy threshold must be finite numeric")
        observed = metrics.get(metric)
        if not isinstance(observed, (int, float)) or isinstance(observed, bool) or not math.isfinite(float(observed)):
            return "FAIL"
        if direction == "max" and float(observed) > float(threshold):
            return "FAIL"
        if direction == "min" and float(observed) < float(threshold):
            return "FAIL"
    return "PASS"
MARKER_STABILITY_THRESHOLDS = {
    "signature_cpg_jaccard_min": 0.30,
    "low_replicate_cell_type_jaccard_min": 0.20,
}
METHYLCIBERSORT_VALIDATION_PERMUTATIONS = 0
# User-built references have no implicit accuracy/repeatability thresholds.
# Add an entry only after a tool-specific policy has been explicitly approved
# and frozen; the container-produced QC file is not an authority for thresholds.
BUILD_VALIDATION_POLICIES: dict[tuple[str, str], dict] = {}


def validate_user_array_build(
    *,
    build_output: Path,
    reference_root: Path,
    metadata: Path,
    platform: str,
    selected_tools: list[str],
    tools_root: Path,
    build_sif: Path,
    runtime_files: dict[str, Path | None],
    container_executable: str,
) -> dict[str, dict]:
    selected = [
        tool for tool in selected_tools
        if tool in {"ARIC", "MethylCIBERSORT", "EDec", "EMeth", "EpiSCORE"}
    ]
    if not selected:
        return {}
    reports: dict[str, dict] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="demethflow-user-science-") as temporary:
            fixture = Path(temporary) / "fixture"
            command = [
                container_executable,
                "exec",
                str(build_sif),
                "Rscript",
                str(tools_root / "common" / "make_user_truth_fixture.R"),
                "--reference-root",
                str(reference_root),
                "--metadata",
                str(metadata),
                "--build-output",
                str(build_output),
                "--output",
                str(fixture),
                "--platform",
                platform,
                "--seed",
                "20260826",
            ]
            fixture_env = os.environ.copy()
            fixture_env["TOOL_COMMON_DIR"] = str((tools_root / "common").resolve())
            subprocess.run(command, check=True, env=fixture_env)
            truth_rows, truth_columns, truth = _read_matrix(fixture / "truth.csv")
            marker_stability = _prepare_array3_training_assets(
                selected=selected,
                fixture=fixture,
                build_output=build_output,
                tools_root=tools_root,
                runtime_files=runtime_files,
                container_executable=container_executable,
                work_root=Path(temporary),
            )
            for tool in selected:
                qc_path, report_path = _build_paths(build_output, tool)
                qc = _read_qc(qc_path)
                approved_policy = BUILD_VALIDATION_POLICIES.get((tool, platform))
                if qc.get("structural_status") != "PASS" or qc.get("marker_gate_status", "PASS") != "PASS":
                    report = _failed_report(tool, "construction structural/marker gate did not pass")
                elif not runtime_files.get(tool):
                    report = _failed_report(tool, f"offline {tool} runtime is unavailable", status="ERROR")
                else:
                    try:
                        report = _run_tool_validation(
                            tool=tool,
                            fixture=fixture,
                            platform=platform,
                            runtime=runtime_files[tool],
                            executable=container_executable,
                            truth_rows=truth_rows,
                            truth_columns=truth_columns,
                            truth=truth,
                            work_root=Path(temporary),
                            approved_policy=approved_policy,
                        )
                        if tool == "MethylCIBERSORT":
                            report["marker_stability"] = marker_stability
                            if marker_stability.get("status") != "PASS":
                                report["status"] = "FAIL"
                                report["tool_specific_gate_status"] = "FAIL"
                                report["reason"] = "marker stability gate did not pass"
                    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, KeyError) as exc:
                        report = _failed_report(tool, f"scientific validation execution failed: {exc}", status="ERROR")
                _write_report(report_path, report)
                qc["scientific_validation"] = {
                    "status": report["status"],
                    "engineering_status": report.get("engineering_status"),
                    "policy_result": report.get("policy_result", "NOT_ASSESSED"),
                    "approved_policy": report.get("approved_policy"),
                    "tool_specific_gate_status": report.get("tool_specific_gate_status"),
                    "metrics": report.get("metrics", {}),
                    "report": report_path.name,
                    "reason": report.get("reason"),
                }
                if tool == "MethylCIBERSORT":
                    qc["marker_stability"] = marker_stability
                atomic_write_json(qc_path, qc)
                reports[tool] = report
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        for tool in selected:
            qc_path, report_path = _build_paths(build_output, tool)
            report = _failed_report(tool, f"could not create held-out fixture: {exc}", status="ERROR")
            _write_report(report_path, report)
            if qc_path.is_file():
                qc = _read_qc(qc_path)
                qc["scientific_validation"] = {
                    "status": "ERROR", "engineering_status": "FAIL",
                    "policy_result": "NOT_ASSESSED", "metrics": {},
                    "report": report_path.name, "reason": report["reason"]
                }
                atomic_write_json(qc_path, qc)
            reports[tool] = report
    return reports


def _prepare_array3_training_assets(
    *, selected: list[str], fixture: Path, build_output: Path, tools_root: Path,
    runtime_files: dict[str, Path | None], container_executable: str, work_root: Path,
) -> dict:
    """Rebuild supervised references inside each training fold, never on holdout data."""
    if not {"ARIC", "MethylCIBERSORT"}.intersection(selected):
        return {"status": "NOT_APPLICABLE"}
    matrix = build_output / "common/reference_matrix.tsv"
    metadata = build_output / "common/reference_metadata.csv"
    if not matrix.is_file() or not metadata.is_file():
        raise RuntimeError("common reference_matrix.tsv/reference_metadata.csv are required for leakage-free validation")
    fold_signatures: list[Path] = []
    for fold in (1, 2):
        include = fixture / f"fold{fold}_training_samples.txt"
        if "ARIC" in selected and fold == 1:
            output = work_root / "training_fold_1/aric"
            output.mkdir(parents=True)
            environment = {
                **os.environ,
                "REFERENCE_ROOT": str(build_output / "common"),
                "REFERENCE_MATRIX": str(matrix),
                "REFERENCE_METADATA": str(metadata),
                "REFERENCE_SAMPLE_INCLUDE": str(include),
                "OUTPUT_DIR": str(output),
                "DECONVOLUTION_SEED": "20260826",
            }
            subprocess.run(
                [sys.executable, str(tools_root / "aric/run_marker.py")],
                check=True, env=environment,
            )
            (fixture / "aric_train.csv").write_bytes((output / "cell_type_centroids.csv").read_bytes())
        if "MethylCIBERSORT" in selected:
            runtime = runtime_files.get("MethylCIBERSORT")
            if runtime is None:
                raise RuntimeError("offline MethylCIBERSORT runtime is unavailable")
            output = work_root / f"training_fold_{fold}/methylcibersort"
            output.mkdir(parents=True)
            subprocess.run([
                container_executable, "exec", str(runtime), "env",
                f"REFERENCE_ROOT={build_output / 'common'}", f"REFERENCE_MATRIX={matrix}",
                f"REFERENCE_METADATA={metadata}", f"REFERENCE_SAMPLE_INCLUDE={include}",
                f"OUTPUT_DIR={output}", "DECONVOLUTION_SEED=20260826",
                "Rscript", str(tools_root / "methylcibersort/run_marker.R"),
            ], check=True)
            fold_signatures.append(output / "MethylCIBERSORT.txt")
            if fold == 1:
                (fixture / "methylcibersort_train.txt").write_bytes(fold_signatures[-1].read_bytes())
    if not fold_signatures:
        return {"status": "NOT_APPLICABLE"}
    qc = _read_qc(build_output / "methylcibersort/output/construction_qc.json")
    return _marker_stability(fold_signatures[0], fold_signatures[1], qc)


def _read_signature(path: Path) -> tuple[list[str], dict[str, list[float]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle, delimiter="\t")
        header = next(reader)
        rows = [row for row in reader if row]
    if len(header) < 2 or any(len(row) != len(header) for row in rows):
        raise RuntimeError(f"invalid MethylCIBERSORT signature: {path}")
    return header[1:], {row[0]: [float(value) for value in row[1:]] for row in rows}


def _marker_stability(first_path: Path, second_path: Path, qc: dict) -> dict:
    first_columns, first = _read_signature(first_path)
    second_columns, second = _read_signature(second_path)
    if first_columns != second_columns:
        return {"status": "FAIL", "reason": "training folds produced different cell-type axes"}
    union = set(first) | set(second)
    global_jaccard = len(set(first) & set(second)) / len(union) if union else 1.0
    raw_low = qc.get("low_replicate_cell_types", [])
    low_replicate = {raw_low} if isinstance(raw_low, str) else set(raw_low)
    per_cell = {}
    for index, cell in enumerate(first_columns):
        if cell not in low_replicate:
            continue
        first_markers = {
            cpg for cpg, values in first.items()
            if max(abs(values[index] - value) for other, value in enumerate(values) if other != index) >= 20
        }
        second_markers = {
            cpg for cpg, values in second.items()
            if max(abs(values[index] - value) for other, value in enumerate(values) if other != index) >= 20
        }
        current_union = first_markers | second_markers
        per_cell[cell] = len(first_markers & second_markers) / len(current_union) if current_union else 1.0
    status = "PASS" if (
        global_jaccard >= MARKER_STABILITY_THRESHOLDS["signature_cpg_jaccard_min"]
        and all(value >= MARKER_STABILITY_THRESHOLDS["low_replicate_cell_type_jaccard_min"] for value in per_cell.values())
    ) else "FAIL"
    return {
        "status": status,
        "method": "two source-sample-stratified training folds with complete FeatureSelect.V4 reselection",
        "signature_cpg_jaccard": global_jaccard,
        "low_replicate_cell_type_jaccard": per_cell,
        "thresholds": MARKER_STABILITY_THRESHOLDS,
        "fold1_cpg_count": len(first), "fold2_cpg_count": len(second),
    }


def _build_paths(build_output: Path, tool: str) -> tuple[Path, Path]:
    if tool == "ARIC":
        root, name = build_output / "aric/output", "aric_qc.json"
    elif tool == "MethylCIBERSORT":
        root, name = build_output / "methylcibersort/output", "construction_qc.json"
    elif tool == "EDec":
        root, name = build_output / "edec/output", "edec_qc.json"
    elif tool == "EMeth":
        root, name = build_output / "emeth/output", "emeth_qc.json"
    else:
        root, name = build_output / "episcore/output", "episcore_qc.json"
    return root / name, root / "scientific_validation.json"


def _read_qc(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"QC is not a JSON object: {path}")
    return payload


def _write_report(path: Path, report: dict) -> None:
    report = {
        "schema": "demethflow-scientific-validation-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": 20260826,
        "design": "one held-out pure sample per cell type; train-only centroids; dominant, balanced and sparse mixtures",
        "reference_centroids_exclude_heldout": True,
        "metric_policy": report.get("metric_policy", "REPORT_ONLY_UNLESS_OWNER_APPROVED_TOOL_POLICY"),
        **report,
    }
    atomic_write_json(path, report)


def _failed_report(tool: str, reason: str, status: str = "FAIL") -> dict:
    return {
        "tool": tool,
        "status": status,
        "engineering_status": "FAIL",
        "policy_result": "NOT_ASSESSED",
        "metric_policy": "REPORT_ONLY_UNLESS_OWNER_APPROVED_TOOL_POLICY",
        "metrics": {},
        "models": {},
        "reason": reason,
    }


def _run_tool_validation(
    *, tool: str, fixture: Path, platform: str, runtime: Path,
    executable: str, truth_rows: list[str], truth_columns: list[str],
    truth: list[list[float]], work_root: Path, approved_policy: dict | None = None,
) -> dict:
    outputs: list[dict[str, Path]] = []
    methyl_diagnostics: list[dict] = []
    runtime_env = os.environ.copy()
    runtime_env["METHUNMIX_EPISCORE_BIN_DIR"] = str((PROJECT_ROOT / "deconvolution" / "bin").resolve())
    runtime_env["DEMETHFLOW_EPISCORE_BIN_DIR"] = runtime_env["METHUNMIX_EPISCORE_BIN_DIR"]
    for repeat in (1, 2):
        run_dir = work_root / f"{tool.lower()}-{repeat}"
        run_dir.mkdir()
        subprocess.run(
            _tool_command(tool, run_dir, fixture, platform, runtime, executable),
            check=True,
            env=runtime_env,
        )
        if tool in {"ARIC", "MethylCIBERSORT"}:
            outputs.append(_standardize_array3_output(tool, run_dir))
            if tool == "MethylCIBERSORT":
                methyl_diagnostics.append(_validate_methyl_diagnostics(run_dir / "validation_res.txt"))
        else:
            outputs.append(_output_paths(tool, run_dir))
    aligned_truth_columns = _canonical_cell_ids(truth_columns) if tool in {"ARIC", "MethylCIBERSORT"} else truth_columns
    models = {}
    for model, first in outputs[0].items():
        delimiter = "\t" if first.suffix == ".txt" else ","
        rows, columns, values = _read_matrix(first, delimiter)
        repeat_rows, repeat_columns, repeat_values = _read_matrix(outputs[1][model], delimiter)
        estimate = _align(rows, columns, values, truth_rows, aligned_truth_columns)
        repeated = _align(repeat_rows, repeat_columns, repeat_values, truth_rows, aligned_truth_columns)
        item = _metrics(estimate, truth, repeated, aligned_truth_columns)
        item["engineering_status"] = "PASS" if _output_contract_passed(item) else "FAIL"
        item["policy_result"] = approved_policy_result(item, tool, approved_policy)
        item["status"] = item["policy_result"] if item["engineering_status"] == "PASS" else "FAIL"
        models[model] = item
    aggregate = {
        "overall_mae": max(item["overall_mae"] for item in models.values()),
        "overall_pearson": min(item["overall_pearson"] for item in models.values()),
        "maximum_cell_type_mae": max(item["maximum_cell_type_mae"] for item in models.values()),
        "valid_output_rate": min(item["valid_output_rate"] for item in models.values()),
        "missing_cell_types": max(item["missing_cell_types"] for item in models.values()),
        "repeat_max_absolute_difference": max(item["repeat_max_absolute_difference"] for item in models.values()),
    }
    if tool == "ARIC":
        run_qc = json.loads((work_root / "aric-1/validation_aric_run_qc.json").read_text(encoding="utf-8"))
        overlap = int(run_qc["informative_overlap_cpg_count"])
        aggregate["minimum_validated_overlap_cpg"] = overlap
        aggregate["frozen_minimum_overlap_cpg"] = max(500, math.floor(overlap * 0.9))
    engineering_status = "PASS" if all(item["engineering_status"] == "PASS" for item in models.values()) else "FAIL"
    policy_result = approved_policy_result(aggregate, tool, approved_policy)
    payload = {
        "tool": tool,
        "status": "FAIL" if engineering_status != "PASS" else policy_result,
        "engineering_status": engineering_status,
        "policy_result": policy_result,
        "approved_policy": approved_policy,
        "metric_policy": "OWNER_APPROVED_TOOL_POLICY" if policy_result != "NOT_ASSESSED" else "REPORT_ONLY_UNLESS_OWNER_APPROVED_TOOL_POLICY",
        "metrics": aggregate,
        "models": models,
    }
    if tool == "MethylCIBERSORT":
        payload["execution_parameters"] = {
            "seed": 20260826,
            "permutations": METHYLCIBERSORT_VALIDATION_PERMUTATIONS,
            "note": "permutations affect empirical P-values, not SVR proportions used by the truth gate",
        }
        payload["cibersort_diagnostics"] = methyl_diagnostics
    return payload


def _validate_methyl_diagnostics(path: Path) -> dict:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = reader.fieldnames or []
        required = {"P-value", "Correlation", "RMSE"}
        if not required <= set(fields):
            raise RuntimeError(f"MethylCIBERSORT diagnostics are incomplete: {path}")
        rows = list(reader)
    if not rows:
        raise RuntimeError(f"MethylCIBERSORT diagnostics are empty: {path}")
    values = {
        name: [float(row[name]) for row in rows]
        for name in required
    }
    if any(not math.isfinite(value) for name in required for value in values[name]):
        raise RuntimeError(f"MethylCIBERSORT diagnostics contain non-finite values: {path}")
    if METHYLCIBERSORT_VALIDATION_PERMUTATIONS == 0:
        pvalue_ok = all(value == 9999 for value in values["P-value"])
    else:
        pvalue_ok = all(0 <= value <= 1 for value in values["P-value"])
    if not pvalue_ok:
        raise RuntimeError(f"MethylCIBERSORT empirical P-value contract failed: {path}")
    return {
        "permutations": METHYLCIBERSORT_VALIDATION_PERMUTATIONS,
        "p_value_status": "PASS",
        "correlation_finite": True,
        "rmse_finite": True,
    }


def _tool_command(tool: str, run_dir: Path, fixture: Path, platform: str, runtime: Path, executable: str) -> list[str]:
    prefix = [executable, "exec", "--pwd", str(run_dir), str(runtime)]
    bulk = str(fixture / "bulk.csv")
    project_root = PROJECT_ROOT
    if tool == "ARIC":
        return prefix + ["python3", str(project_root / "deconvolution/bin/aric_decon.py"), "--mix", bulk,
                         "--ref", str(fixture / "aric_train.csv"), "--sample_id", "validation",
                         "--min_overlap", "500", "--seed", "20260826"]
    if tool == "MethylCIBERSORT":
        return prefix + ["Rscript", str(project_root / "deconvolution/bin/methylcibersort_decon.R"),
                         "--mix", bulk, "--ref", str(fixture / "methylcibersort_train.txt"),
                         "--sample_id", "validation", "--perm", str(METHYLCIBERSORT_VALIDATION_PERMUTATIONS),
                         "--seed", "20260826"]
    if tool == "EDec":
        return prefix + ["Rscript", str(project_root / "deconvolution/bin/edec_decon.R"), "--mix", bulk,
                         "--markers", str(fixture / "edec_markers.rds"), "--tref", str(fixture / "edec_tref_train.rds"),
                         "--sample_id", "validation", "--seed", "20260826"]
    if tool == "EMeth":
        return prefix + ["Rscript", str(project_root / "deconvolution/bin/emeth_decon.R"), "--mix", bulk,
                         "--ref_rdata", str(fixture / "emeth_train.RData"), "--sample_id", "validation", "--seed", "20260826"]
    script = "episcore_decon_450k.R" if platform == "450k" else "episcore_decon_850k.R"
    return prefix + ["Rscript", str(project_root / "deconvolution/bin" / script), "--mix_cpg", bulk,
                     "--ref_gene", str(fixture / "episcore_gene_train.rds"), "--sample_id", "validation"]


def _standardize_array3_output(tool: str, run_dir: Path) -> dict[str, Path]:
    raw = run_dir / ("validation_prop.csv" if tool == "ARIC" else "validation_res.txt")
    destination = run_dir / f"{tool}.csv"
    script = PROJECT_ROOT / "deconvolution/array_pro" / f"{tool}_pro.py"
    subprocess.run([sys.executable, str(script), str(raw), str(destination)], check=True)
    return {tool: destination}


def _canonical_cell_ids(labels: list[str]) -> list[str]:
    output = []
    for label in labels:
        normalized = "Astrocyte" if label.strip().lower() == "atrocyte" else label.strip()
        if normalized.lower() == "epithelial":
            output.append("epithelium")
            continue
        value = "".join(character.lower() if character.isalnum() else "-" for character in normalized)
        output.append("-".join(part for part in value.split("-") if part) or "cell")
    if len(output) != len(set(output)):
        raise RuntimeError("cell labels do not map to unique canonical IDs")
    return output


def _output_paths(tool: str, run_dir: Path) -> dict[str, Path]:
    if tool == "EDec":
        return {"EDec": run_dir / "validation_Proportions_Labeled.csv"}
    if tool == "EMeth":
        return {"EMeth_Laplace": run_dir / "validation_EMeth_rho_laplace.txt",
                "EMeth_Normal": run_dir / "validation_EMeth_rho_normal.txt"}
    return {"EpiSCORE": run_dir / "Result_validation.csv"}


def _read_matrix(path: Path, delimiter: str = ",") -> tuple[list[str], list[str], list[list[float]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        header = next(reader)
        raw_rows = [row for row in reader if row]
    if not raw_rows:
        raise RuntimeError(f"empty result matrix: {path}")
    columns = header if len(raw_rows[0]) == len(header) + 1 else header[1:]
    return [row[0].strip('"') for row in raw_rows], [value.strip('"') for value in columns], [
        [float(value) for value in row[1:]] for row in raw_rows
    ]


def _align(rows: list[str], columns: list[str], values: list[list[float]], truth_rows: list[str], truth_columns: list[str]) -> list[list[float]]:
    row_index = {name: index for index, name in enumerate(rows)}
    column_index = {name: index for index, name in enumerate(columns)}
    return [[values[row_index[row]][column_index[column]] for column in truth_columns] for row in truth_rows]


def _metrics(estimate: list[list[float]], truth: list[list[float]], repeated: list[list[float]], columns: list[str]) -> dict:
    flat_estimate = [value for row in estimate for value in row]
    flat_truth = [value for row in truth for value in row]
    differences = [[abs(a - b) for a, b in zip(est_row, truth_row)] for est_row, truth_row in zip(estimate, truth)]
    per_cell = {cell: sum(row[index] for row in differences) / len(differences) for index, cell in enumerate(columns)}
    valid_rows = sum(all(math.isfinite(value) and value >= -1e-10 for value in row)
                     and math.isclose(sum(row), 1.0, rel_tol=1e-4, abs_tol=1e-6) for row in estimate)
    return {
        "overall_mae": sum(value for row in differences for value in row) / len(flat_truth),
        "overall_pearson": _pearson(flat_estimate, flat_truth),
        "maximum_cell_type_mae": max(per_cell.values()),
        "per_cell_type_mae": per_cell,
        "valid_output_rate": valid_rows / len(truth),
        "missing_cell_types": 0,
        "repeat_max_absolute_difference": max(abs(a - b) for row_a, row_b in zip(estimate, repeated) for a, b in zip(row_a, row_b)),
    }


def _pearson(left: list[float], right: list[float]) -> float:
    left_mean, right_mean = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    denominator = math.sqrt(sum((a - left_mean) ** 2 for a in left) * sum((b - right_mean) ** 2 for b in right))
    return numerator / denominator if denominator else 0.0


def _output_contract_passed(item: dict) -> bool:
    """Check output integrity independently of scientific accuracy/tolerance."""
    try:
        return item["valid_output_rate"] == 1.0 and item["missing_cell_types"] == 0
    except (KeyError, TypeError):
        return False
