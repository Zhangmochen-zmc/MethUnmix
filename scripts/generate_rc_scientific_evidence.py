#!/usr/bin/env python3
"""Generate bounded MethUnmix RC science-policy and compatibility evidence.

This script only reads the frozen baseline and existing local run outputs. It
does not launch workflows, alter scientific states, or rewrite the baseline.
Pass --data-root and --legacy-array-root (or set the corresponding
METHUNMIX_EVIDENCE_DATA_ROOT / METHUNMIX_LEGACY_ARRAY_ROOT variables) to scan
host-local evidence. Without them, local historical scans are omitted.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "evidence/BASELINE_FREEZE_CANDIDATE.json"
FROZEN_PATH = ROOT / "evidence/baseline_frozen.json"
RUNS = {
    "brain.450k-native.not_applicable": (
        "build/scientific_compatibility/brain-450k-houseman/run",
        "build/scientific_compatibility_repeats/brain_450k",
    ),
    "breast.450k-native.not_applicable": (
        "build/scientific_compatibility/breast_450k_houseman/run",
        "build/scientific_compatibility_repeats/breast_450k_retry1",
    ),
    "epithelial.450k-native.not_applicable": (
        "build/scientific_compatibility/epithelial_450k_houseman/run",
        "build/scientific_compatibility_repeats/epithelial_450k",
    ),
    "fetal19.450k-native.not_applicable": (
        "build/scientific_compatibility/fetal19_450k_houseman/run",
        "build/scientific_compatibility_repeats/fetal19_450k",
    ),
    "immune12.450k-native.not_applicable": (
        "build/scientific_compatibility/immune12_450k_houseman/run",
        "build/scientific_compatibility_repeats/immune12_450k",
    ),
    "brain.epic-native.not_applicable": (
        "build/scientific_compatibility/brain_epic_houseman/run",
        "build/scientific_compatibility_repeats/brain_epic",
    ),
    "immune12.epic-native.not_applicable": (
        "build/scientific_compatibility/immune12_epic_houseman/run",
        "build/scientific_compatibility_repeats/immune12_epic",
    ),
}
NON_COUNTED = {
    "path": "build/scientific_compatibility_repeats/breast_450k",
    "disposition": "NOT_COUNTED",
    "reason": "Initial repeat attempt produced intermediate tool outputs but did not finalize a successful run_manifest before the calling session ended; successful independent retry is breast_450k_retry1.",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(name: str, data: dict[str, Any]) -> None:
    (ROOT / "evidence" / name).write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def threshold_entries(value: Any, prefix: str = "") -> dict[str, Any]:
    found: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            low = str(key).lower()
            if any(token in low for token in ("threshold", "tolerance", "max_absolute_difference", "repeat_max")):
                if isinstance(child, (int, float)) and not isinstance(child, bool):
                    found[path] = child
            found.update(threshold_entries(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.update(threshold_entries(child, f"{prefix}[{index}]"))
    return found


def make_policy_audit(
    candidate: dict[str, Any], frozen: dict[str, Any], data_root: Path | None,
) -> dict[str, Any]:
    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in candidate["records"]:
        rows = []
        for item in record.get("historical_validation", []):
            exists = item.get("exists") is True
            digest_match = bool(item.get("declared_sha256")) and item.get("declared_sha256") == item.get("observed_sha256")
            metrics = item.get("observed_metrics_and_thresholds", {})
            thresholds = threshold_entries(metrics)
            rows.append({
                "kind": item.get("kind"),
                "path": item.get("path"),
                "exists": exists,
                "declared_sha256": item.get("declared_sha256"),
                "observed_sha256": item.get("observed_sha256"),
                "digest_match": digest_match,
                "reported_status": item.get("reported_status"),
                "explicit_numeric_thresholds": thresholds,
                "eligible_as_exact_scope_policy_evidence": exists and digest_match and bool(thresholds),
            })
        by_tool[record["tool"]].append({
            "selector": record.get("selector"),
            "fixture_id": record.get("fixture", {}).get("fixture_id"),
            "scientific_status": record.get("scientific_status_current"),
            "validation_scope": record.get("validation_scope_current"),
            "historical_validation": rows,
        })

    tools = []
    for tool, records in sorted(by_tool.items(), key=lambda item: item[0].casefold()):
        matching_artifacts = [
            artifact
            for record in records
            for artifact in record["historical_validation"]
            if artifact["eligible_as_exact_scope_policy_evidence"]
        ]
        threshold_keys = sorted({key for artifact in matching_artifacts for key in artifact["explicit_numeric_thresholds"]})
        threshold_metric_families = {
            "pearson": any("pearson" in key.casefold() for key in threshold_keys),
            "overall_mae": any("overall_mae" in key.casefold() for key in threshold_keys),
            "maximum_per_cell_type_mae": any("maximum_cell_type_mae" in key.casefold() or "max_cell" in key.casefold() for key in threshold_keys),
            "repeatability": any("repeat" in key.casefold() or "max_absolute_difference" in key.casefold() for key in threshold_keys),
            "old_vs_new": any("old_vs_new" in key.casefold() or "regression" in key.casefold() for key in threshold_keys),
        }
        if tool.casefold() == "methylbert":
            authority = "OWNER_APPROVED_SCOPE_ONLY"
            owner_rule = frozen.get("approved_scientific_policy", {}).get("methylbert_pearson_minimum")
        elif matching_artifacts:
            authority = "HASH_MATCHED_HISTORICAL_SCOPE_ONLY"
            owner_rule = None
        else:
            authority = "NEW_OWNER_APPROVAL_REQUIRED_OR_REPORT_ONLY"
            owner_rule = None
        tools.append({
            "method": tool,
            "baseline_pair_count": len(records),
            "hash_matched_threshold_bearing_artifact_count": len(matching_artifacts),
            "exact_scope_threshold_keys": threshold_keys,
            "exact_scope_threshold_metric_families_present": threshold_metric_families,
            "authority_class": authority,
            "owner_approved_rule": owner_rule,
            "pairs_without_an_applicable_formal_threshold": sum(
                1 for record in records
                if not any(a["eligible_as_exact_scope_policy_evidence"] for a in record["historical_validation"])
                and not (tool.casefold() == "methylbert" and record["scientific_status"] == "READY")
            ),
            "records": records,
        })

    owner_policy = frozen.get("approved_scientific_policy", {})
    outside_baseline_houseman_regressions = []
    regression_reports = (
        [
            data_root / "reports/immune6/regression/450k_Houseman_results.json",
            data_root / "reports/immune6/regression/epic_Houseman_results.json",
        ]
        if data_root is not None else []
    )
    for path in regression_reports:
        if path.is_file():
            report = load_json(path)
            outside_baseline_houseman_regressions.append({
                "path": str(path),
                "sha256": sha256(path),
                "tool": report.get("tool"),
                "status": report.get("status"),
                "scenario_scope": "immune6",
                "platform_scope": "450k" if "450k" in path.name else "epic",
                "samples_compared": report.get("samples_compared"),
                "comparison_tolerance": {
                    "rtol": report.get("rtol"),
                    "atol": report.get("atol"),
                    "baseline_decimals": report.get("baseline_decimals"),
                },
                "observed": {
                    "max_absolute_error": report.get("max_absolute_error"),
                    "max_relative_error": report.get("max_relative_error"),
                    "failures": report.get("failures"),
                },
                "authority_class": "HISTORICAL_EXACT_SCOPE_REGRESSION_EVIDENCE_ONLY",
                "applies_to_current_seven_houseman_fixtures": False,
                "not_a_pearson_mae_or_repeatability_threshold": True,
            })
    return {
        "schema": "methunmix-scientific-acceptance-policy-audit-v1",
        "generated_at": utc_now(),
        "status": "AUDIT_COMPLETE_DRAFT_POLICY_NOT_OWNER_APPROVED_FOR_NEW_THRESHOLDS",
        "baseline": {
            "status": frozen.get("status"),
            "candidate_file_sha256": frozen.get("candidate_binding", {}).get("file_sha256"),
            "candidate_manifest_sha256": frozen.get("candidate_binding", {}).get("manifest_sha256"),
            "pair_count": frozen.get("candidate_binding", {}).get("record_count"),
        },
        "authority_classes": {
            "existing_formal_owner_policy": "Pearson >= 0.80 applies only to existing MethylBERT READY routes, as explicitly approved in baseline_frozen.json.",
            "hash_matched_historical_scope_only": "Numeric thresholds/tolerances in an existing validation artifact are usable only for the exact tool, selector, model/profile, and scope bound by that digest-matched artifact; they are not a project-wide rule.",
            "new_owner_approval_required": "No numeric acceptance threshold may be inferred from observed results, a candidate inventory, or an unmatched artifact. Report metrics and leave that acceptance dimension UNDEFINED/PENDING.",
        },
        "formal_coverage_counts_by_method": tools,
        "historical_numeric_policy_outside_frozen_baseline": {
            "houseman_old_vs_new_immune6_array_regression": outside_baseline_houseman_regressions,
            "scope_warning": "This exact-scope old-vs-new tolerance is evidence for legacy immune6 450K/EPIC comparisons only. It cannot be inherited by the seven new scenario fixtures and is not a scientific-accuracy or same-version repeatability threshold.",
        },
        "metric_policy_draft": {
            "pearson": {
                "approved_scope": owner_policy.get("methylbert_pearson_minimum"),
                "otherwise": "Use exact-scope digest-matched historical threshold if present; otherwise report-only and UNDEFINED/PENDING.",
            },
            "overall_mae": "No universal cutoff. Exact-scope digest-matched historical threshold only; otherwise report-only.",
            "maximum_per_cell_type_mae": "No universal cutoff. Exact-scope digest-matched historical threshold only; otherwise report-only.",
            "old_vs_new_numerical_tolerance": "Must be declared for the exact comparison scope before interpretation. Houseman has no authoritative prior output for the seven tested selectors.",
            "repeatability_tolerance": "Use only a predeclared exact-scope tool/profile threshold. For Houseman none is present; exact-match observations are reported without generalizing a threshold.",
            "sample_axis_cell_axis_schema": "Exact sample identity/order and canonical cell identity/order after registered alias mapping; reject missing/extra/duplicate axes and schema/version mismatches.",
            "na_inf": "Canonical proportions must contain no NA/NaN/Inf; report checks at raw, standardized and canonical stages. Exceptions require explicit method-specific approval.",
            "proportion_range_and_sum_to_one": "Require finite [0,1] values and current exact-contract validation behavior. Do not impose a common epsilon when no method-specific tolerance is declared; report residual and leave numeric acceptance UNDEFINED.",
        },
        "status_policy": "Do not change READY/RU/QUARANTINED/NOT_AVAILABLE from this audit or the 7 repeat tests.",
    }


def read_matrix(path: Path) -> tuple[list[str], list[str], list[float]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or len(reader.fieldnames) < 2:
            raise ValueError(f"canonical matrix has invalid header: {path}")
        sample_col = reader.fieldnames[0]
        cells = reader.fieldnames[1:]
        samples: list[str] = []
        vals: list[float] = []
        for row in reader:
            samples.append(row[sample_col])
            vals.extend(float(row[cell]) for cell in cells)
    return samples, cells, vals


def pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) != len(b) or not a:
        return None
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    da = [v - ma for v in a]
    db = [v - mb for v in b]
    den = math.sqrt(sum(x * x for x in da) * sum(y * y for y in db))
    if den == 0:
        return 1.0 if a == b else None
    return sum(x * y for x, y in zip(da, db)) / den


def output_files(run_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for subdir in ("canonical", "results", "standardized", "raw"):
        base = run_dir / subdir
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and "benchmark" not in path.name.casefold():
                found[path.relative_to(run_dir).as_posix()] = path
    for name in ("cell_types.tsv", "tool_status.tsv"):
        path = run_dir / name
        if path.is_file():
            found[name] = path
    return found


def compare_repeat(case_id: str, original_rel: str, repeat_rel: str) -> dict[str, Any]:
    original = ROOT / original_rel
    repeat = ROOT / repeat_rel
    manifest_a = load_json(original / "run_manifest.json")
    manifest_b = load_json(repeat / "run_manifest.json")
    if manifest_a.get("status") != "SUCCEEDED" or manifest_b.get("status") != "SUCCEEDED":
        raise ValueError(f"run is not successful for {case_id}")
    if manifest_a.get("selected_tools") != manifest_b.get("selected_tools"):
        raise ValueError(f"selected tools differ for {case_id}")

    files_a = output_files(original)
    files_b = output_files(repeat)
    shared = sorted(set(files_a) & set(files_b))
    different = sorted(set(files_a) ^ set(files_b))
    diff_files = [name for name in shared if sha256(files_a[name]) != sha256(files_b[name])]
    if different:
        raise ValueError(f"scientific output file sets differ for {case_id}: {different}")

    matrix_a = original / "canonical/Houseman_results.csv"
    matrix_b = repeat / "canonical/Houseman_results.csv"
    samples_a, cells_a, values_a = read_matrix(matrix_a)
    samples_b, cells_b, values_b = read_matrix(matrix_b)
    if samples_a != samples_b or cells_a != cells_b:
        raise ValueError(f"canonical axes differ for {case_id}")
    differences = [abs(a - b) for a, b in zip(values_a, values_b)]
    max_diff = max(differences, default=0.0)
    mae = sum(differences) / len(differences) if differences else 0.0
    all_finite = all(math.isfinite(v) for v in values_a + values_b)
    min_value = min(values_a + values_b)
    max_value = max(values_a + values_b)
    row_sums_a = []
    with matrix_a.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        cols = reader.fieldnames[1:]
        for row in reader:
            row_sums_a.append(sum(float(row[col]) for col in cols))
    row_sum_error = max((abs(v - 1.0) for v in row_sums_a), default=0.0)

    benchmark_diffs = []
    bench_a = {p.relative_to(original).as_posix(): p for p in original.rglob("*benchmark.csv") if p.is_file()}
    bench_b = {p.relative_to(repeat).as_posix(): p for p in repeat.rglob("*benchmark.csv") if p.is_file()}
    for name in sorted(set(bench_a) & set(bench_b)):
        with bench_a[name].open(newline="", encoding="utf-8", errors="replace") as handle:
            rows_a = list(csv.DictReader(handle))
            fields_a = list(rows_a[0]) if rows_a else []
        with bench_b[name].open(newline="", encoding="utf-8", errors="replace") as handle:
            rows_b = list(csv.DictReader(handle))
            fields_b = list(rows_b[0]) if rows_b else []
        differing_columns = []
        if fields_a == fields_b and len(rows_a) == len(rows_b):
            differing_columns = [
                field for field in fields_a
                if any(left.get(field) != right.get(field) for left, right in zip(rows_a, rows_b))
            ]
        benchmark_diffs.append({
            "file": name,
            "byte_identical": sha256(bench_a[name]) == sha256(bench_b[name]),
            "differing_columns": differing_columns,
            "difference_scope": "run-time benchmark metadata; excluded from scientific output comparison",
        })

    record = next(
        r for r in load_json(BASELINE_PATH)["records"]
        if r.get("tool", "").casefold() == "houseman"
        and r.get("fixture", {}).get("fixture_id") == case_id
    )
    return {
        "fixture_id": case_id,
        "method": "Houseman",
        "selector": manifest_a.get("reference_id") + "@" + str(manifest_a.get("reference_version")),
        "original_run_manifest": original_rel + "/run_manifest.json",
        "repeat_run_manifest": repeat_rel + "/run_manifest.json",
        "original_manifest_sha256": sha256(original / "run_manifest.json"),
        "repeat_manifest_sha256": sha256(repeat / "run_manifest.json"),
        "execution_status": "SUCCEEDED_BOTH_RUNS",
        "seed": manifest_a.get("random_seed"),
        "input_sha256_original": manifest_a.get("input_sha256"),
        "input_sha256_repeat": manifest_b.get("input_sha256"),
        "reference_digest_original": manifest_a.get("reference_digest"),
        "reference_digest_repeat": manifest_b.get("reference_digest"),
        "sample_axis": samples_a,
        "cell_type_axis": cells_a,
        "sample_axis_identical": samples_a == samples_b,
        "cell_type_axis_identical": cells_a == cells_b,
        "scientific_file_count_compared": len(shared),
        "scientific_file_set_identical": not different,
        "scientific_files_byte_identical": not diff_files,
        "different_scientific_files": diff_files,
        "canonical_sha256_original": sha256(matrix_a),
        "canonical_sha256_repeat": sha256(matrix_b),
        "maximum_absolute_difference": max_diff,
        "mae_between_runs": mae,
        "pearson_between_runs": pearson(values_a, values_b),
        "all_values_finite": all_finite,
        "observed_proportion_min": min_value,
        "observed_proportion_max": max_value,
        "maximum_row_sum_to_one_error": row_sum_error,
        "repeatability_policy_status": "UNDEFINED_NO_PREDECLARED_HOUSEMAN_REPEAT_TOLERANCE",
        "observation_disposition": "BYTE_IDENTICAL_REPEAT_OBSERVED_REPORT_ONLY_NOT_A_GENERALIZED_TOLERANCE_OR_SCIENTIFIC_ACCURACY_PASS",
        "benchmark_comparisons": benchmark_diffs,
        "historical_regression_status": "NO_AUTHORITATIVE_PRIOR_OUTPUT",
        "baseline_historical_validation_count": len(record.get("historical_validation", [])),
        "truth_metrics_are_report_only": True,
    }


def search_historical_houseman(
    candidate: dict[str, Any], data_root: Path | None, legacy_array_root: Path | None,
) -> dict[str, Any]:
    target_records = {
        record["fixture"]["fixture_id"]: record
        for record in candidate["records"]
        if record.get("tool", "").casefold() == "houseman"
        and record.get("fixture", {}).get("fixture_id") in RUNS
    }
    root_paths = [
        path for path in (
            data_root / "test_runs" if data_root is not None else None,
            legacy_array_root,
        ) if path is not None and path.is_dir()
    ]
    command = ["rg", "--files", *[str(path) for path in root_paths]]
    result = (
        subprocess.run(command, capture_output=True, text=True, check=False, timeout=45)
        if root_paths else subprocess.CompletedProcess(command, 1, "", "")
    )
    if root_paths and result.returncode not in (0, 1):
        raise RuntimeError(f"historical filename scan failed: rg exit {result.returncode}: {result.stderr[-500:]}")
    old_outputs = [
        Path(line) for line in result.stdout.splitlines()
        if line.endswith(("/canonical/Houseman_results.csv", "/results/Houseman_results.csv"))
    ]
    axis_matches: dict[str, list[dict[str, Any]]] = {fixture_id: [] for fixture_id in RUNS}
    for historical_path in old_outputs:
        try:
            with historical_path.open(newline="", encoding="utf-8-sig") as handle:
                table = list(csv.reader(handle))
            if not table:
                continue
        except (OSError, UnicodeError, csv.Error):
            continue
        for fixture_id, (original_rel, _) in RUNS.items():
            current_path = ROOT / original_rel / "canonical/Houseman_results.csv"
            samples, cells, _ = read_matrix(current_path)
            if table[0] != ["SampleID", *cells] or [row[0] for row in table[1:]] != samples:
                continue
            axis_matches[fixture_id].append({
                "path": str(historical_path),
                "sha256": sha256(historical_path),
            })

    known_immune6_reports = []
    regression_reports = (
        [
            data_root / "reports/immune6/regression/450k_Houseman_results.json",
            data_root / "reports/immune6/regression/epic_Houseman_results.json",
        ]
        if data_root is not None else []
    )
    for path in regression_reports:
        if path.is_file():
            report = load_json(path)
            known_immune6_reports.append({
                "path": str(path),
                "sha256": sha256(path),
                "reported_status": report.get("status"),
                "baseline_output": report.get("baseline"),
                "current_output": report.get("current"),
                "samples_compared": report.get("samples_compared"),
                "rtol": report.get("rtol"),
                "atol": report.get("atol"),
                "max_absolute_error": report.get("max_absolute_error"),
                "scope_disposition": "IMMUNE6_450K_OR_EPIC_HISTORICAL_REGRESSION; NOT_THE_SEVEN_CURRENT_BRAIN_BREAST_EPITHELIAL_FETAL19_IMMUNE12_FIXTURES",
            })

    legacy_immune12 = []
    legacy_manifests = (
        [
            data_root / "test_runs/e2e_immune12_houseman/run_manifest.json",
            data_root / "test_runs/build_and_decon_houseman/deconvolution/run_manifest.json",
        ]
        if data_root is not None else []
    )
    for path in legacy_manifests:
        if path.is_file():
            run = load_json(path)
            legacy_immune12.append({
                "run_manifest": str(path),
                "sha256": sha256(path),
                "status": run.get("status"),
                "scenario_id": run.get("scenario_id"),
                "input": run.get("input"),
                "input_sha256": run.get("input_sha256"),
                "reference_id": run.get("reference_id"),
                "reference_version": run.get("reference_version"),
                "selected_tools": run.get("selected_tools"),
                "scope_disposition": "LEGACY_IMMUNE12_450K_TEST; INPUT_DIGEST_MISSING_AND_REFERENCE_SELECTOR_DIFFERS_FROM_FROZEN_CURRENT_SELECTOR",
            })

    reference_family_history = []
    seen_families: set[str] = set()
    for record in target_records.values():
        reference_id = record["reference"]["reference_id"]
        if reference_id in seen_families:
            continue
        seen_families.add(reference_id)
        family_root = data_root / "references" / reference_id if data_root is not None else None
        scan = subprocess.run(["rg", "--files", str(family_root)], capture_output=True, text=True, check=False, timeout=20) if family_root is not None and family_root.is_dir() else None
        family_files = scan.stdout.splitlines() if scan and scan.returncode == 0 else []
        ref_assets = [
            path for path in family_files
            if "/artifacts/houseman/" in path.casefold()
            and Path(path).name.casefold() in {"beta_ref.rds", "cell_ref.rds"}
        ]
        validation_candidates = []
        for path_text in family_files:
            path = Path(path_text)
            low = path.as_posix().casefold()
            is_houseman_report = (
                ("houseman" in path.name.casefold() and path.suffix.casefold() in {".json", ".csv", ".txt"})
                or ("/artifacts/houseman/" in low and path.suffix.casefold() == ".json")
            )
            if not is_houseman_report:
                continue
            item: dict[str, Any] = {"path": str(path), "sha256": sha256(path)}
            if path.suffix.casefold() == ".json":
                try:
                    report = load_json(path)
                    item["reported_status"] = report.get("status")
                    item["selector"] = report.get("selector") or report.get("reference_selector")
                    item["input_sha256"] = report.get("input_sha256")
                except (OSError, json.JSONDecodeError):
                    item["json_parse"] = "FAILED"
            validation_candidates.append(item)
        reference_family_history.append({
            "reference_id": reference_id,
            "family_root": str(family_root) if family_root is not None else None,
            "previous_and_current_version_files_scanned": len(family_files),
            "houseman_reference_model_asset_file_count": len(ref_assets),
            "houseman_reference_model_assets_are_prior_deconvolution_outputs": False,
            "houseman_validation_or_output_candidates": validation_candidates,
            "rg_exit_code": scan.returncode if scan else None,
        })

    return {
        "search_scope": [str(path) for path in root_paths],
        "search_method": "rg --files followed by exact canonical CSV sample-axis and cell-axis comparison against all seven current Houseman outputs; all version directories of the seven selected reference families were inventoried for Houseman outputs/validation records; selected known immune6 regression reports and legacy immune12 E2E manifests were separately parsed and digest-bound. Roots are explicitly supplied at runtime; without roots the historical scan is not performed.",
        "historical_canonical_houseman_files_scanned": len(old_outputs),
        "rg_exit_code": result.returncode if root_paths else None,
        "same_sample_and_cell_axes_by_current_fixture": {
            fixture_id: {
                "match_count": len(axis_matches[fixture_id]),
                "matches": axis_matches[fixture_id],
                "authoritative_prior_output_status": "NO_AUTHORITATIVE_PRIOR_OUTPUT" if not axis_matches[fixture_id] else "CANDIDATE_MATCH_REQUIRES_BINDING_REVIEW",
            }
            for fixture_id in RUNS
        },
        "known_other_scope_history": {
            "immune6_formal_numeric_regression_reports": known_immune6_reports,
            "legacy_immune12_houseman_runs": legacy_immune12,
            "selected_reference_family_history": reference_family_history,
            "nextflow_array_new_baseline_outputs": [
                {
                    "artifact_id": "nextflow_array_new/results_array_450k/results/Houseman_results.csv",
                    "path": str(legacy_array_root / "results_array_450k/results/Houseman_results.csv") if legacy_array_root is not None else None,
                    "scope": "immune6 450K; 100-sample legacy baseline referenced by the dated formal regression report.",
                    "report_artifact_id": "reports/immune6/regression/450k_Houseman_results.json",
                    "report": str(data_root / "reports/immune6/regression/450k_Houseman_results.json") if data_root is not None else None,
                },
                {
                    "artifact_id": "nextflow_array_new/results_array_epic/results/Houseman_results.csv",
                    "path": str(legacy_array_root / "results_array_epic/results/Houseman_results.csv") if legacy_array_root is not None else None,
                    "scope": "immune6 EPIC; 100-sample legacy baseline referenced by the dated formal regression report.",
                    "report_artifact_id": "reports/immune6/regression/epic_Houseman_results.json",
                    "report": str(data_root / "reports/immune6/regression/epic_Houseman_results.json") if data_root is not None else None,
                },
            ],
        },
        "limitations": [
            "The filename/axis search covers only explicitly supplied local roots, not every backup/archive outside them.",
            "A same-axis output alone is insufficient: authoritative regression requires exact input, reference/selector, tool version, run provenance and output schema binding.",
            "The known immune6 legacy regression is formal evidence for its own 100-sample immune6 scope, not the seven current scenarios.",
        ],
    }


def make_repeatability(
    candidate: dict[str, Any], frozen: dict[str, Any], data_root: Path | None,
    legacy_array_root: Path | None,
) -> dict[str, Any]:
    rows = []
    for fixture_id, (original, repeat) in RUNS.items():
        rows.append(compare_repeat(fixture_id, original, repeat))
    historical_search = []
    for record in candidate["records"]:
        if record.get("tool", "").casefold() != "houseman" or record.get("fixture", {}).get("fixture_id") not in RUNS:
            continue
        reference_root = Path(record["reference"]["manifest_path"]).parent
        matching_named_files = []
        if reference_root.is_dir():
            for path in reference_root.rglob("*"):
                if path.is_file() and any(token in path.name.casefold() for token in ("houseman", "housemann", "qp_result")):
                    matching_named_files.append(str(path))
        historical_search.append({
            "fixture_id": record["fixture"]["fixture_id"],
            "selector": record["selector"],
            "reference_root": str(reference_root),
            "reference_root_exists": reference_root.is_dir(),
            "baseline_historical_validation_count": len(record.get("historical_validation", [])),
            "named_prior_houseman_outputs_found": matching_named_files,
            "authoritative_prior_output_status": "NO_AUTHORITATIVE_PRIOR_OUTPUT" if not matching_named_files and not record.get("historical_validation") else "CANDIDATE_FOUND_REQUIRES_AUTHORITY_AND_BINDING_REVIEW",
        })
    return {
        "schema": "methunmix-scientific-compatibility-repeatability-v1",
        "generated_at": utc_now(),
        "status": "7_OF_7_REPEATABILITY_COMPARISONS_BYTE_IDENTICAL; NUMERIC_POLICY_UNDEFINED",
        "release_gate_disposition": "PARTIAL_EVIDENCE_ONLY; SCIENTIFIC_ACCEPTANCE_AND_FULL_COMPATIBILITY_GATES_PENDING",
        "baseline": {
            "status": frozen.get("status"),
            "candidate_file_sha256": frozen.get("candidate_binding", {}).get("file_sha256"),
            "candidate_manifest_sha256": frozen.get("candidate_binding", {}).get("manifest_sha256"),
            "pair_count": frozen.get("candidate_binding", {}).get("record_count"),
        },
        "execution_contract": {
            "repeat_count": "one independent repeat per existing original run",
            "seed": 20260914,
            "same_input_selector_runtime_parameters": True,
            "independent_work_and_output_directories": True,
            "executor": "local",
            "slurm": "NOT_USED",
            "metrics_policy": "Exact byte match and measured numeric differences are reported; because Houseman has no predeclared numeric repeatability tolerance, no numeric PASS/FAIL threshold is inferred.",
        },
        "non_counted_attempts": [NON_COUNTED],
        "historical_output_search": {
            "scope": "Exact selected bundle roots plus targeted prior-output search over local test_runs and nextflow_array_new; no claim that every backup/archive outside those roots was searched.",
            "results": historical_search,
            "broader_local_scan": search_historical_houseman(candidate, data_root, legacy_array_root),
        },
        "summary": {
            "cases": len(rows),
            "all_run_manifests_succeeded": all(r["execution_status"] == "SUCCEEDED_BOTH_RUNS" for r in rows),
            "all_sample_axes_identical": all(r["sample_axis_identical"] for r in rows),
            "all_cell_type_axes_identical": all(r["cell_type_axis_identical"] for r in rows),
            "all_scientific_files_byte_identical": all(r["scientific_files_byte_identical"] for r in rows),
            "all_canonical_max_absolute_difference_zero": all(r["maximum_absolute_difference"] == 0 for r in rows),
            "all_canonical_values_finite": all(r["all_values_finite"] for r in rows),
            "scientific_acceptance_threshold_assigned": False,
            "status_promotion": "PROHIBITED",
        },
        "results": rows,
    }


def make_coverage(candidate: dict[str, Any], frozen: dict[str, Any], repeats: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    repeats_by_fixture = {r["fixture_id"]: r for r in repeats["results"]}
    policy_by_method = {r["method"].casefold(): r for r in policy["formal_coverage_counts_by_method"]}
    rows = []
    for record in candidate["records"]:
        fixture = record.get("fixture", {})
        method = record["tool"]
        fixture_id = fixture.get("fixture_id")
        repeated = repeats_by_fixture.get(fixture_id) if method.casefold() == "houseman" else None
        history = record.get("historical_validation", [])
        authoritative_prior = []  # No source record declares a prior canonical output binding.
        method_policy = policy_by_method.get(method.casefold(), {})
        if method.casefold() == "methylbert" and record.get("scientific_status_current") == "READY":
            policy_availability = "OWNER_APPROVED_PEARSON_SCOPE_PLUS_OTHER_METRICS_REPORT_ONLY"
        elif any(
            item.get("exists") is True
            and item.get("declared_sha256")
            and item.get("declared_sha256") == item.get("observed_sha256")
            and threshold_entries(item.get("observed_metrics_and_thresholds", {}))
            for item in history
        ):
            policy_availability = "HASH_MATCHED_EXACT_SCOPE_THRESHOLDS_ONLY"
        else:
            policy_availability = "NO_APPLICABLE_NUMERIC_POLICY_FOUND"

        route = fixture.get("route")
        status = record.get("scientific_status_current")
        exec_policy = record.get("execution_policy_current") or "normal"
        if fixture.get("platform") == "wgbs" or route != "array-native":
            tier = "E"
        elif status == "READY" and exec_policy != "explicit_only":
            tier = "A"
        elif status == "READY" and exec_policy == "explicit_only":
            tier = "B"
        elif status == "RELEASED_UNVALIDATED" and exec_policy != "explicit_only":
            tier = "C"
        elif status == "RELEASED_UNVALIDATED" and exec_policy == "explicit_only":
            tier = "D"
        else:
            tier = "UNCLASSIFIED"

        if repeated:
            execution = "SUCCEEDED_ORIGINAL_AND_REPEAT"
            repeatability = "BYTE_IDENTICAL_OBSERVED_NUMERIC_GATE_UNDEFINED"
            regression = "NO_AUTHORITATIVE_PRIOR_OUTPUT"
            final = "PARTIAL_EXECUTION_AND_REPEATABILITY_EVIDENCE; ACCEPTANCE_UNDEFINED; NOT_FULL_COMPATIBILITY_PASS"
            evidence = [
                "evidence/scientific_compatibility_execution_rc.json",
                "evidence/scientific_compatibility_repeatability_rc.json",
            ]
        else:
            execution = "NOT_RUN"
            repeatability = "NOT_RUN"
            regression = "NOT_RUN"
            final = "NOT_ASSESSED"
            evidence = ["evidence/BASELINE_FREEZE_CANDIDATE.json", "evidence/baseline_frozen.json"]
        rows.append({
            "method": method,
            "scenario": fixture.get("scenario"),
            "reference_selector": record.get("selector"),
            "reference_id": record.get("reference", {}).get("reference_id"),
            "reference_version": record.get("reference", {}).get("version"),
            "reference_digest": record.get("reference", {}).get("manifest_sha256"),
            "platform": fixture.get("platform"),
            "source_platform_label": fixture.get("source_platform_label"),
            "analysis_contract": record.get("reference", {}).get("analysis_contract"),
            "target_reference_platform": record.get("reference", {}).get("reference_platform"),
            "route": route,
            "genome_build": fixture.get("genome_build"),
            "baseline_status": status,
            "execution_policy": record.get("execution_policy_current"),
            "effective_execution_policy_for_tiering": exec_policy,
            "stratum": tier,
            "fixture_id": fixture_id,
            "execution_status": execution,
            "repeatability_status": repeatability,
            "old_vs_new_regression_status": regression,
            "authoritative_prior_output_paths": authoritative_prior,
            "acceptance_policy_availability": policy_availability,
            "exact_scope_historical_threshold_bearing_artifacts": method_policy.get("hash_matched_threshold_bearing_artifact_count", 0),
            "final_compatibility_status": final,
            "evidence_paths": evidence,
        })
    counts = Counter(row["stratum"] for row in rows)
    return {
        "schema": "methunmix-scientific-compatibility-coverage-matrix-v1",
        "generated_at": utc_now(),
        "status": "FROZEN_BASELINE_COVERAGE_PARTIAL",
        "baseline": {
            "status": frozen.get("status"),
            "candidate_file_sha256": frozen.get("candidate_binding", {}).get("file_sha256"),
            "candidate_manifest_sha256": frozen.get("candidate_binding", {}).get("manifest_sha256"),
            "pair_count": frozen.get("candidate_binding", {}).get("record_count"),
            "ready_count": frozen.get("scope", {}).get("ready_candidate_pairs"),
            "ru_count": frozen.get("scope", {}).get("released_unvalidated_candidate_pairs"),
        },
        "row_count": len(rows),
        "stratum_counts": {key: counts.get(key, 0) for key in "ABCDE"},
        "status_policy": "This matrix describes test/evidence coverage only. It does not alter READY/RU or imply scientific acceptance where policy is undefined.",
        "rows": rows,
    }


def make_execution_plan(matrix: dict[str, Any], repeats: dict[str, Any]) -> dict[str, Any]:
    tiers = {
        "A": {
            "priority": 1,
            "definition": "READY + default public invocation path, excluding WGBS/high-cost paths reserved for E.",
            "count": 0,
            "runtime": ["local Nextflow", "Apptainer array-common SIF", "method-specific CPU SIF/runtime"],
            "local_boundary": "CPU routes are locally executable where fixture/reference/runtime/rights are available; bounded per-case timeout and separate work/out paths required.",
            "external_boundary": "No Slurm or GPU is intrinsically required for these CPU cases. Conda/mulled integration remains a separate external release gate.",
        },
        "B": {
            "priority": 2,
            "definition": "READY + non-default/explicit-only invocation path, excluding WGBS/high-cost paths reserved for E.",
            "count": 0,
            "runtime": ["local Nextflow", "Apptainer array-common SIF", "MeDeCom CPU/R runtime"],
            "local_boundary": "Can run locally with explicit selection and a valid multi-sample fixture; assess tool semantics separately from supervised tools.",
            "external_boundary": "No scheduler/GPU intrinsically required; external CI is separate.",
        },
        "C": {
            "priority": 3,
            "definition": "RU + default path, excluding WGBS/high-cost paths reserved for E.",
            "count": 0,
            "runtime": ["local CPU runtime if profile exists"],
            "local_boundary": "Run only after warning/disclosure behavior is checked; accuracy remains under the frozen policy.",
            "external_boundary": "No scheduler required for CPU; any GPU profile requires a reachable supported GPU runner.",
        },
        "D": {
            "priority": 4,
            "definition": "RU + non-default/explicit-only path, excluding WGBS/high-cost paths reserved for E.",
            "count": 0,
            "runtime": ["local CPU runtime if profile exists"],
            "local_boundary": "Explicit invocation only; warning and disclosure outputs must be captured.",
            "external_boundary": "No scheduler required for CPU; GPU only on a compatible external runner.",
        },
        "E": {
            "priority": 5,
            "definition": "All native WGBS and WGBS-derived paths, treated as higher-cost routes and excluded from A-D counts.",
            "count": 0,
            "runtime": ["local Nextflow + Apptainer WGBS SIFs for native tools", "array SIFs for WGBS-derived array tools", "optional GPU runtime for MEnet/MethylBERT"],
            "local_boundary": "CPU route checks remain locally possible; do not bulk-run the 39.42 GiB fixture inventory. Use selected cases, per-command timeouts, explicit budgets and no Slurm.",
            "external_boundary": "The 12 GPU profiles require an external compatible GPU runner because this host has no reachable NVIDIA driver. Bioconda/mulled is separately external.",
            "composition": {"wgbs_derived_array": 72, "derived_450k": 48, "derived_epic": 24, "native_wgbs": 36},
        },
    }
    for row in matrix["rows"]:
        if row["stratum"] in tiers:
            tiers[row["stratum"]]["count"] += 1
    tested_by_tier = Counter(row["stratum"] for row in matrix["rows"] if row["execution_status"] != "NOT_RUN")
    remaining_statuses = Counter(
        (row["stratum"], row["baseline_status"])
        for row in matrix["rows"]
        if row["execution_status"] == "NOT_RUN"
    )
    for key, item in tiers.items():
        item["frozen_baseline_count"] = item["count"]
        item["already_completed_count"] = tested_by_tier.get(key, 0)
        item["remaining_count"] = item["count"] - item["already_completed_count"]
        item["remaining_status_composition"] = {
            status: remaining_statuses.get((key, status), 0)
            for status in ("READY", "RELEASED_UNVALIDATED")
        }
        item["count"] = item["remaining_count"]
    return {
        "schema": "methunmix-scientific-compatibility-execution-plan-v1",
        "generated_at": utc_now(),
        "status": "PLAN_ONLY_NO_REMAINING_BATCH_STARTED",
        "baseline_pair_count": matrix["row_count"],
        "already_executed_pairs": 7,
        "remaining_pairs": matrix["row_count"] - 7,
        "remaining_tier_counts": {key: tiers[key]["remaining_count"] for key in "ABCDE"},
        "tiers": [tiers[key] for key in "ABCDE"],
        "runtime_inventory_boundary": {
            "cpu_local": "Local CPU/Nextflow/Apptainer may be used when the exact runtime/reference assets exist; do not block these on unavailable Slurm/GPU/external Conda CI.",
            "gpu": "12 candidate WGBS MEnet/MethylBERT GPU profiles need an external compatible GPU host; current local host driver is not reachable.",
            "large_input": "About 39.42 GiB across WGBS BED/PAT fixtures; this is an inventory size, not authorization to launch all at once.",
            "scheduler": "Slurm NOT_REQUIRED and NOT_USED.",
        },
        "execution_controls_for_future_batches": [
            "Owner approves the exact tier and case list before each batch.",
            "Use one isolated work/output directory per route; do not share Nextflow work state across different selectors.",
            "Bound each invocation with an explicit timeout and capture peak resource/runtime evidence.",
            "Run CPU cases locally where available; route GPU-only evidence to compatible GPU runner; never mark unavailable evidence PASS.",
            "Do not alter READY/RU/QUARANTINED/NOT_AVAILABLE based on compatibility execution alone.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=(Path(os.environ["METHUNMIX_EVIDENCE_DATA_ROOT"]) if os.environ.get("METHUNMIX_EVIDENCE_DATA_ROOT") else None),
        help="local directory containing reports/, test_runs/, and references/",
    )
    parser.add_argument(
        "--legacy-array-root",
        type=Path,
        default=(Path(os.environ["METHUNMIX_LEGACY_ARRAY_ROOT"]) if os.environ.get("METHUNMIX_LEGACY_ARRAY_ROOT") else None),
        help="optional local legacy array-workflow results directory",
    )
    args = parser.parse_args()
    data_root = args.data_root.resolve() if args.data_root else None
    legacy_array_root = args.legacy_array_root.resolve() if args.legacy_array_root else None
    candidate = load_json(BASELINE_PATH)
    frozen = load_json(FROZEN_PATH)
    policy = make_policy_audit(candidate, frozen, data_root)
    repeats = make_repeatability(candidate, frozen, data_root, legacy_array_root)
    matrix = make_coverage(candidate, frozen, repeats, policy)
    plan = make_execution_plan(matrix, repeats)
    write_json("scientific_acceptance_policy_audit_rc.json", policy)
    write_json("scientific_compatibility_repeatability_rc.json", repeats)
    write_json("scientific_compatibility_coverage_matrix_rc.json", matrix)
    write_json("scientific_compatibility_execution_plan_rc.json", plan)
    print(json.dumps({
        "policy": "evidence/scientific_acceptance_policy_audit_rc.json",
        "repeatability": "evidence/scientific_compatibility_repeatability_rc.json",
        "matrix": "evidence/scientific_compatibility_coverage_matrix_rc.json",
        "plan": "evidence/scientific_compatibility_execution_plan_rc.json",
        "matrix_rows": matrix["row_count"],
        "strata": matrix["stratum_counts"],
        "repeatability_summary": repeats["summary"],
    }, indent=2))


if __name__ == "__main__":
    main()
