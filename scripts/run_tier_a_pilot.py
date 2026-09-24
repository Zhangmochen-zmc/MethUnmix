#!/usr/bin/env python3
"""Run a small, frozen-baseline Tier A MethUnmix compatibility pilot.

The runner is deliberately local-only, bounded, no-retry, CPU-only, and rejects
cases outside the frozen READY/default-call array baseline. It never changes
scientific status. Truth metrics are reported only unless an exact approved
policy is bound to the case.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import resource
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATE = ROOT / "evidence/baseline_candidate_inventory.json"
DEFAULT_FROZEN = ROOT / "evidence/baseline_frozen.json"
DEFAULT_MATRIX = ROOT / "evidence/scientific_compatibility_coverage_matrix_rc.json"
DEFAULT_CASES = ROOT / "evidence/tier_a_pilot_cases_rc.json"
SCHEMA = "methunmix-scientific-compatibility-pilot-v1"
MAX_PILOT_CASES = 20
MAX_CONCURRENCY = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def elapsed_seconds(start: str | None, finish: str | None) -> float:
    if not start or not finish:
        return 0.0
    left = datetime.fromisoformat(start.replace("Z", "+00:00"))
    right = datetime.fromisoformat(finish.replace("Z", "+00:00"))
    return max(0.0, (right - left).total_seconds())


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def norm_label(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def aliases_from_manifest(manifest: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    cell_types = manifest.get("cell_types")
    if not isinstance(cell_types, list) or not cell_types:
        raise ValueError("reference manifest has no registered cell_types axis")
    ids: list[str] = []
    aliases: dict[str, str] = {}
    for item in cell_types:
        if not isinstance(item, dict) or not item.get("cell_type_id"):
            raise ValueError("reference manifest contains an invalid cell_types entry")
        cell_id = str(item["cell_type_id"])
        ids.append(cell_id)
        for label in [cell_id, item.get("display_name", ""), *item.get("synonyms", [])]:
            key = norm_label(str(label))
            if key:
                previous = aliases.setdefault(key, cell_id)
                if previous != cell_id:
                    raise ValueError(f"ambiguous registered cell-type alias: {label}")
    return ids, aliases


def read_matrix(path: Path, aliases: dict[str, str]) -> dict[str, Any]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        rows = list(reader)
    if not fields or not rows:
        raise ValueError(f"empty proportion matrix: {path}")
    sample_field = "SampleID" if "SampleID" in fields else fields[0]
    cell_fields: list[tuple[str, str]] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for field in fields:
        if field == sample_field:
            continue
        cell_id = aliases.get(norm_label(field))
        if cell_id is None:
            unknown.append(field)
            continue
        if cell_id in seen:
            raise ValueError(f"duplicate cell-type column after alias mapping: {field}")
        seen.add(cell_id)
        cell_fields.append((field, cell_id))
    values: dict[str, dict[str, float]] = {}
    ordered_samples: list[str] = []
    all_numbers: list[float] = []
    for row in rows:
        sample = str(row.get(sample_field, "")).strip()
        if not sample or sample in values:
            raise ValueError(f"missing or duplicate sample id {sample!r}: {path}")
        mapped: dict[str, float] = {}
        for field, cell_id in cell_fields:
            value = float(row[field])
            mapped[cell_id] = value
            all_numbers.append(value)
        values[sample] = mapped
        ordered_samples.append(sample)
    finite = all(math.isfinite(value) for value in all_numbers)
    minimum = min(all_numbers) if all_numbers else None
    maximum = max(all_numbers) if all_numbers else None
    max_row_sum_error = max(
        abs(sum(row.values()) - 1.0) for row in values.values()
    ) if values else None
    return {
        "samples": ordered_samples,
        "cells": [cell_id for _, cell_id in cell_fields],
        "values": values,
        "unknown_columns": unknown,
        "all_finite": finite,
        "minimum": minimum,
        "maximum": maximum,
        "all_in_unit_interval": bool(all_numbers) and minimum >= 0.0 and maximum <= 1.0,
        "maximum_row_sum_to_one_error": max_row_sum_error,
    }


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    mean_l = sum(left) / len(left)
    mean_r = sum(right) / len(right)
    numerator = sum((a - mean_l) * (b - mean_r) for a, b in zip(left, right))
    denom = math.sqrt(
        sum((a - mean_l) ** 2 for a in left)
        * sum((b - mean_r) ** 2 for b in right)
    )
    return numerator / denom if denom else None


def compare_to_truth(
    estimate_path: Path,
    truth_path: Path,
    canonical_cells: list[str],
    aliases: dict[str, str],
    expected_samples: list[str],
) -> dict[str, Any]:
    estimate = read_matrix(estimate_path, aliases)
    truth = read_matrix(truth_path, aliases)
    sample_order_match = estimate["samples"] == truth["samples"] == expected_samples
    cell_order_match = estimate["cells"] == truth["cells"] == canonical_cells
    sample_identity_match = (
        set(estimate["samples"]) == set(truth["samples"]) == set(expected_samples)
    )
    truth_cell_identity_match = set(truth["cells"]) == set(canonical_cells)
    estimate_cell_axis_match = estimate["cells"] == canonical_cells
    axes_match = (
        sample_identity_match
        and estimate_cell_axis_match
        and truth_cell_identity_match
        and not estimate["unknown_columns"]
        and not truth["unknown_columns"]
    )
    result: dict[str, Any] = {
        "estimate": str(estimate_path),
        "estimate_sha256": digest(estimate_path),
        "truth": str(truth_path),
        "truth_sha256": digest(truth_path),
        "sample_axis_order_identical": sample_order_match,
        "cell_type_axis_order_identical": cell_order_match,
        "sample_identity_sets_match": sample_identity_match,
        "truth_cell_type_identity_set_matches_canonical": truth_cell_identity_match,
        "estimate_cell_type_axis_matches_canonical_order": estimate_cell_axis_match,
        "truth_sample_order_reordered_to_expected": truth["samples"] != expected_samples,
        "truth_cell_type_order_reordered_to_canonical": truth["cells"] != canonical_cells,
        "unknown_estimate_columns": estimate["unknown_columns"],
        "unknown_truth_columns": truth["unknown_columns"],
        "estimate_sample_axis": estimate["samples"],
        "truth_sample_axis": truth["samples"],
        "estimate_cell_type_axis": estimate["cells"],
        "truth_cell_type_axis": truth["cells"],
        "estimate_all_finite": estimate["all_finite"],
        "estimate_all_in_unit_interval": estimate["all_in_unit_interval"],
        "estimate_maximum_row_sum_to_one_error": estimate["maximum_row_sum_to_one_error"],
        "truth_all_finite": truth["all_finite"],
        "truth_all_in_unit_interval": truth["all_in_unit_interval"],
        "truth_maximum_row_sum_to_one_error": truth["maximum_row_sum_to_one_error"],
        "axes_schema_structural_status": "PASS" if axes_match and estimate["all_finite"] and truth["all_finite"] else "FAIL",
        "acceptance_disposition": "REPORT_ONLY_NO_UNAPPROVED_NUMERIC_THRESHOLD",
    }
    if axes_match:
        samples = expected_samples
        values_e = [estimate["values"][s][c] for s in samples for c in canonical_cells]
        values_t = [truth["values"][s][c] for s in samples for c in canonical_cells]
        per_cell = {
            cell: sum(abs(estimate["values"][s][cell] - truth["values"][s][cell]) for s in samples) / len(samples)
            for cell in canonical_cells
        }
        absolute = [abs(a - b) for a, b in zip(values_e, values_t)]
        result["truth_metrics"] = {
            "overall_mae": sum(absolute) / len(absolute),
            "overall_pearson": pearson(values_e, values_t),
            "maximum_cell_type_mae": max(per_cell.values()),
            "per_cell_type_mae": per_cell,
        }
    else:
        result["truth_metrics"] = None
    return result


def compare_replicates(
    left_result: dict[str, Any], right_result: dict[str, Any],
    aliases: dict[str, str], canonical_cells: list[str], expected_samples: list[str],
) -> dict[str, Any]:
    if left_result.get("execution_status") != "SUCCEEDED" or right_result.get("execution_status") != "SUCCEEDED":
        return {"status": "UNRESOLVED_EXECUTION_FAILURE", "policy": "NO_THRESHOLD_INFERRED"}
    left_outputs = {Path(item["estimate"]).name: item["estimate"] for item in left_result.get("outputs", []) if item.get("estimate")}
    right_outputs = {Path(item["estimate"]).name: item["estimate"] for item in right_result.get("outputs", []) if item.get("estimate")}
    if set(left_outputs) != set(right_outputs) or not left_outputs:
        return {"status": "OUTPUT_FILE_SET_MISMATCH", "policy": "NO_THRESHOLD_INFERRED"}
    comparisons = []
    for filename in sorted(left_outputs):
        left = read_matrix(Path(left_outputs[filename]), aliases)
        right = read_matrix(Path(right_outputs[filename]), aliases)
        axes_match = (
            left["samples"] == right["samples"] == expected_samples
            and left["cells"] == right["cells"] == canonical_cells
            and not left["unknown_columns"] and not right["unknown_columns"]
        )
        if not axes_match:
            comparisons.append({"file": filename, "axes_identical": False, "status": "AXIS_MISMATCH"})
            continue
        a = [left["values"][sample][cell] for sample in expected_samples for cell in canonical_cells]
        b = [right["values"][sample][cell] for sample in expected_samples for cell in canonical_cells]
        diffs = [abs(x - y) for x, y in zip(a, b)]
        comparisons.append({
            "file": filename,
            "axes_identical": True,
            "maximum_absolute_difference": max(diffs),
            "mae_between_runs": sum(diffs) / len(diffs),
            "pearson_between_runs": pearson(a, b),
            "status": "MEASURED_REPORT_ONLY",
        })
    measured = [item for item in comparisons if item.get("maximum_absolute_difference") is not None]
    return {
        "status": "MEASURED_NO_PREDECLARED_THRESHOLD" if len(measured) == len(comparisons) else "PARTIAL_OR_AXIS_MISMATCH",
        "policy": "NO_THRESHOLD_INFERRED; repeat Pearson is run-to-run agreement, not truth accuracy",
        "output_comparisons": comparisons,
    }


def _limit_child(cpu_cores: int, memory_gb: int, cpu_seconds: int) -> None:
    if hasattr(os, "sched_getaffinity") and hasattr(os, "sched_setaffinity"):
        allowed = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, set(allowed[:cpu_cores]))
    memory = memory_gb * 1024**3
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))


def run_command(
    command: list[str], env: dict[str, str], log_path: Path,
    timeout_seconds: int, cpu_cores: int, memory_gb: int,
) -> dict[str, Any]:
    start = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    limiter = [
        sys.executable, str(Path(__file__).resolve()), "_exec_limited",
        str(cpu_cores), str(memory_gb), str(timeout_seconds), "--", *command,
    ]
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            limiter,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=timeout_seconds)
            status = "SUCCEEDED" if return_code == 0 else "FAILED"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            return_code = process.returncode
            status = "TIMED_OUT"
    return {
        "status": status,
        "exit_code": return_code,
        "wall_seconds": round(time.monotonic() - start, 3),
        "log": str(log_path),
        "log_sha256": digest(log_path),
    }


def verify_runner_self_test() -> dict[str, Any]:
    if os.name != "posix" or not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("runner self-test requires Linux CPU affinity and POSIX resource limits")
    with TemporaryDirectory(prefix="methunmix-pilot-runner-test-") as temp:
        base = Path(temp)
        env = dict(os.environ)
        probe = [
            sys.executable,
            "-c",
            "import json,os,resource; print(json.dumps({'cpus':len(os.sched_getaffinity(0)),'as':resource.getrlimit(resource.RLIMIT_AS)[0]}))",
        ]
        probe_result = run_command(probe, env, base / "probe.log", 20, 1, 8)
        if probe_result["status"] != "SUCCEEDED":
            raise RuntimeError("resource-limit probe did not run")
        probe_value = json.loads((base / "probe.log").read_text(encoding="utf-8").splitlines()[-1])
        if probe_value["cpus"] > 1 or probe_value["as"] > 8 * 1024**3:
            raise RuntimeError(f"runner CPU/memory limits were not inherited: {probe_value}")
        failed = run_command(
            [sys.executable, "-c", "raise SystemExit(17)"], env, base / "fail.log", 20, 1, 8
        )
        passed = run_command(
            [sys.executable, "-c", "print('still-ran')"], env, base / "after-failure.log", 20, 1, 8
        )
        timed = run_command(
            [sys.executable, "-c", "import time; time.sleep(3)"], env, base / "timeout.log", 1, 1, 8
        )
        if failed["status"] != "FAILED" or failed["exit_code"] != 17:
            raise RuntimeError(f"expected controlled failure, observed {failed}")
        if passed["status"] != "SUCCEEDED" or "still-ran" not in (base / "after-failure.log").read_text():
            raise RuntimeError("a prior case failure contaminated the subsequent case")
        if timed["status"] != "TIMED_OUT":
            raise RuntimeError(f"timeout did not terminate the case: {timed}")
        estimate_path = base / "estimate.csv"
        truth_path = base / "truth.csv"
        estimate_path.write_text("SampleID,b,a\nS1,0.7,0.3\nS2,0.2,0.8\n", encoding="utf-8")
        truth_path.write_text("SampleID,a,b\nS1,0.3,0.7\nS2,0.8,0.2\n", encoding="utf-8")
        aligned = compare_to_truth(
            estimate_path, truth_path, ["b", "a"], {"a": "a", "b": "b"}, ["S1", "S2"]
        )
        if (
            aligned["axes_schema_structural_status"] != "PASS"
            or not aligned["truth_cell_type_order_reordered_to_canonical"]
            or aligned["truth_metrics"]["overall_mae"] != 0.0
        ):
            raise RuntimeError("name-based truth-axis alignment self-test failed")
        fixture_id = "brain.450k-native.not_applicable"
        pilot_case = {"method": "EpiDISH", "fixture_id": fixture_id}
        fixture = {"fixture_id": fixture_id, "route": "array-native"}
        capability = {
            "tool": "EpiDISH",
            "fixture_id": fixture_id,
            "capability_id": "cap-1",
            "fixture_route": "array-native",
            "reference_contract": "array_450k_cpg",
            "scientific_status": "READY",
            "input_content_digest_ready": True,
            "truth_content_digest_ready": True,
            "sample_mapping_valid": True,
        }
        reference = {
            "capability_id": "cap-1",
            "tool": "EpiDISH",
            "selector": "builtin.brain.450k@1.5.0",
            "scientific_status": "READY",
            "manifest_sha256": "a" * 64,
            "analysis_contract": "array_450k_cpg",
        }
        matrix_row = {
            "method": "EpiDISH",
            "fixture_id": fixture_id,
            "baseline_status": "READY",
            "stratum": "A",
            "execution_status": "NOT_RUN",
            "route": "array-native",
            "analysis_contract": "array_450k_cpg",
            "reference_selector": "builtin.brain.450k@1.5.0",
            "reference_digest": "a" * 64,
        }
        joined, joined_row = find_case_records(
            {
                "fixtures": [fixture],
                "fixture_capability_candidates": [capability],
                "latest_reference_tool_candidates": [reference],
            },
            {"rows": [matrix_row]},
            pilot_case,
        )
        if joined["fixture"] != fixture or joined["selector"] != reference["selector"] or joined_row != matrix_row:
            raise RuntimeError("candidate-inventory fixture/reference join self-test failed")
        return {
            "status": "PASS",
            "cpu_affinity_enforced": True,
            "per_process_address_space_limit_enforced": True,
            "wall_timeout_terminates_process_group": True,
            "failure_isolation": True,
            "candidate_inventory_join": True,
            "truth_axis_name_alignment": True,
            "retry_attempts": 0,
            "output_logs_isolated": True,
        }


def find_case_records(candidate: dict[str, Any], matrix: dict[str, Any], case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [
        row for row in matrix["rows"]
        if row.get("method") == case["method"]
        and row.get("fixture_id") == case["fixture_id"]
    ]
    fixtures = [
        fixture for fixture in candidate.get("fixtures", [])
        if fixture.get("fixture_id") == case["fixture_id"]
    ]
    capabilities = [
        capability for capability in candidate.get("fixture_capability_candidates", [])
        if capability.get("tool") == case["method"]
        and capability.get("fixture_id") == case["fixture_id"]
    ]
    if len(fixtures) != 1 or len(capabilities) != 1 or len(rows) != 1:
        raise ValueError(f"case must resolve to exactly one frozen baseline pair: {case}")
    row = rows[0]
    if (row.get("baseline_status"), row.get("stratum"), row.get("execution_status")) != ("READY", "A", "NOT_RUN"):
        raise ValueError(f"case is not an untested Tier A READY/default pair: {case} -> {row}")
    fixture = fixtures[0]
    capability = capabilities[0]
    if (
        capability.get("scientific_status") != row.get("baseline_status")
        or capability.get("fixture_route") != row.get("route")
        or capability.get("reference_contract") != row.get("analysis_contract")
        or not capability.get("input_content_digest_ready")
        or not capability.get("truth_content_digest_ready")
        or not capability.get("sample_mapping_valid")
    ):
        raise ValueError(f"fixture capability does not agree with the frozen matrix row: {case}")
    reference_candidates = [
        reference for reference in candidate.get("latest_reference_tool_candidates", [])
        if reference.get("capability_id") == capability.get("capability_id")
        and reference.get("tool") == case["method"]
        and reference.get("selector") == row.get("reference_selector")
    ]
    if len(reference_candidates) != 1:
        raise ValueError(f"case must resolve to exactly one reference selector candidate: {case}")
    reference = reference_candidates[0]
    if (
        reference.get("scientific_status") != row.get("baseline_status")
        or reference.get("manifest_sha256") != row.get("reference_digest")
        or reference.get("analysis_contract") != row.get("analysis_contract")
    ):
        raise ValueError(f"reference candidate does not agree with the frozen matrix row: {case}")
    return {"tool": case["method"], "fixture": fixture, "selector": reference["selector"]}, row


def build_case_command(
    *, cli: Path, input_path: Path, case: dict[str, Any], fixture: dict[str, Any],
    selector: str, outdir: Path, reference_root: Path, runtime_root: Path,
    container_engine: Path, seed: int,
) -> list[str]:
    return [
        str(cli), "run",
        "--scenario", str(fixture["scenario"]),
        "--platform", str(fixture["platform"]),
        "--reference", selector,
        "--tools", str(case["method"]),
        "--input", str(input_path),
        "--reference-root", str(reference_root),
        "--runtime-root", str(runtime_root),
        "--runtime", "singularity",
        "--container-engine", str(container_engine),
        "--executor", "local",
        "--accelerator", "cpu",
        "--random-seed", str(seed),
        "--verify-reference-checksums",
        "--outdir", str(outdir),
    ]


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_exec_limited":
        if "--" not in sys.argv[5:]:
            raise SystemExit("internal launcher missing command delimiter")
        delimiter = sys.argv.index("--", 5)
        cpu_cores, memory_gb, cpu_seconds = map(int, sys.argv[2:5])
        command = sys.argv[delimiter + 1:]
        if not command:
            raise SystemExit("internal launcher received an empty command")
        _limit_child(cpu_cores, memory_gb, cpu_seconds)
        os.execvpe(command[0], command, os.environ)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="exercise timeout, resource limits, and failure isolation only")
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--frozen", type=Path, default=DEFAULT_FROZEN)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--nextflow", type=Path)
    parser.add_argument("--reference-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--container-engine", type=Path)
    parser.add_argument("--outdir", type=Path)
    parser.add_argument("--output-evidence", type=Path)
    parser.add_argument("--output-matrix", type=Path)
    parser.add_argument("--source-sha256")
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--max-cases", type=int, default=MAX_PILOT_CASES)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--cpu-cores", type=int, default=2)
    parser.add_argument("--memory-gb", type=int, default=64)
    parser.add_argument("--random-seed", type=int, default=20260914)
    parser.add_argument("--reuse-existing-runs", action="store_true", help="validate and report existing pilot outputs without rerunning workflows")
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps(verify_runner_self_test(), indent=2))
        return 0
    if not all((args.cli, args.nextflow, args.reference_root, args.runtime_root, args.container_engine, args.outdir, args.output_evidence, args.output_matrix, args.source_sha256, args.wheel_sha256)):
        parser.error("real pilot requires --cli, --nextflow, --reference-root, --runtime-root, --container-engine, --outdir, --output-evidence, --output-matrix, --source-sha256, and --wheel-sha256")
    if not 1 <= args.max_cases <= MAX_PILOT_CASES:
        parser.error(f"--max-cases must be in [1,{MAX_PILOT_CASES}]")
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        parser.error(f"--concurrency is hard-limited to [1,{MAX_CONCURRENCY}]")
    if min(args.timeout_seconds, args.cpu_cores, args.memory_gb) < 1:
        parser.error("timeout, cpu cores, and memory limit must be positive")
    for path in (args.cli, args.nextflow, args.container_engine):
        if path is None or not path.is_file():
            parser.error(f"required executable is not a regular file: {path}")
    outroot = args.outdir.resolve()
    if outroot.exists() and not args.reuse_existing_runs:
        parser.error(f"pilot output root already exists; refusing to overwrite: {outroot}")
    if outroot.is_symlink():
        parser.error(f"pilot output root may not be a symlink: {outroot}")
    if args.reuse_existing_runs and not outroot.is_dir():
        parser.error(f"--reuse-existing-runs requires an existing output directory: {outroot}")
    if args.cases.is_symlink():
        parser.error("case-list path may not be a symlink")
    candidate = load_json(args.candidate)
    frozen = load_json(args.frozen)
    matrix = load_json(args.matrix)
    case_list = load_json(args.cases)
    cases = case_list.get("cases", [])
    if not isinstance(cases, list) or not 1 <= len(cases) <= args.max_cases:
        parser.error("case list must contain 1..--max-cases cases")
    if len({case.get("case_id") for case in cases}) != len(cases):
        parser.error("case_id values must be unique")
    candidate_binding = frozen.get("candidate_binding", {})
    inventory_digest = digest(args.candidate)
    expected_inventory_digest = candidate_binding.get("candidate_inventory_sha256")
    if expected_inventory_digest and inventory_digest != expected_inventory_digest:
        parser.error("candidate inventory digest does not match the owner-frozen inventory digest")
    freeze_candidate_path = args.frozen.parent / Path(
        candidate_binding.get("path", "")
    ).name
    expected_freeze_candidate_digest = candidate_binding.get("file_sha256")
    if expected_freeze_candidate_digest:
        if not freeze_candidate_path.is_file() or digest(freeze_candidate_path) != expected_freeze_candidate_digest:
            parser.error("freeze-candidate document does not match the owner-frozen digest")
    env = dict(os.environ)
    nextflow_bin = str(args.nextflow.resolve())
    env["METHUNMIX_NEXTFLOW_CMD"] = nextflow_bin
    env["PATH"] = str(args.nextflow.resolve().parent) + os.pathsep + env.get("PATH", "")
    env["NXF_OPTS"] = "-Xms128m -Xmx8g"
    env["NXF_OFFLINE"] = "true"
    env["NXF_DISABLE_CHECK_LATEST"] = "true"

    tasks: list[dict[str, Any]] = []
    for case in cases:
        record, matrix_row = find_case_records(candidate, matrix, case)
        fixture = record["fixture"]
        input_files = fixture.get("input_files", [])
        truth_files = fixture.get("truth_files", [])
        if len(input_files) != 1 or len(truth_files) != 1:
            parser.error(f"pilot case requires one exact input and one truth file: {case['case_id']}")
        input_path = Path(input_files[0]["path"]).resolve(strict=True)
        truth_path = Path(truth_files[0]["path"]).resolve(strict=True)
        if digest(input_path) != input_files[0].get("sha256") or digest(truth_path) != truth_files[0].get("sha256"):
            parser.error(f"input/truth digest differs from frozen candidate inventory: {case['case_id']}")
        selector = str(record["selector"])
        reference_id, version = selector.rsplit("@", 1)
        manifest_path = args.reference_root / reference_id / version / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            parser.error(f"missing or symlinked reference manifest: {manifest_path}")
        manifest = load_json(manifest_path)
        manifest_sha256 = digest(manifest_path)
        if manifest_sha256 != matrix_row.get("reference_digest"):
            parser.error(f"reference manifest digest differs from frozen baseline pair: {case['case_id']}")
        canonical_cells, aliases = aliases_from_manifest(manifest)
        expected_samples = list(fixture.get("sample_mapping_audit", {}).get("expected_ids", []))
        for replicate in range(1, int(case.get("repeat_count", 1)) + 1):
            name = f"{case['case_id']}/replicate-{replicate}"
            case_out = outroot / name
            if (case_out.exists() or case_out.is_symlink()) and not args.reuse_existing_runs:
                parser.error(f"case output path collision: {case_out}")
            if args.reuse_existing_runs and (
                case_out.is_symlink()
                or not case_out.is_dir()
                or not (case_out / "run_manifest.json").is_file()
                or (case_out / "run_manifest.json").is_symlink()
            ):
                parser.error(f"existing run lacks a regular run manifest: {case_out}")
            command = build_case_command(
                cli=args.cli.resolve(), input_path=input_path, case=case, fixture=fixture,
                selector=selector, outdir=case_out, reference_root=args.reference_root.resolve(),
                runtime_root=args.runtime_root.resolve(), container_engine=args.container_engine.resolve(),
                seed=args.random_seed,
            )
            tasks.append({
                "case": case,
                "record": record,
                "matrix_row": matrix_row,
                "fixture": fixture,
                "input_path": input_path,
                "truth_path": truth_path,
                "selector": selector,
                "manifest_path": manifest_path,
                "manifest_sha256": manifest_sha256,
                "canonical_cells": canonical_cells,
                "aliases": aliases,
                "expected_samples": expected_samples,
                "replicate": replicate,
                "outdir": case_out,
                "command": command,
            })
    if len(tasks) > args.max_cases * 2:
        parser.error("replicate expansion exceeds bounded pilot task limit")

    if not args.reuse_existing_runs:
        outroot.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        run_manifest_path = task["outdir"] / "run_manifest.json"
        log_path = task["outdir"] / "pilot.stdout.log"
        if args.reuse_existing_runs:
            run_manifest = load_json(run_manifest_path)
            reference_id, reference_version = task["selector"].rsplit("@", 1)
            expected = {
                "input_sha256": digest(task["input_path"]),
                "selected_tools": [task["case"]["method"]],
                "reference_id": reference_id,
                "reference_version": reference_version,
                "accelerator": "cpu",
                "random_seed": args.random_seed,
            }
            mismatches = {
                key: {"expected": value, "observed": run_manifest.get(key)}
                for key, value in expected.items()
                if run_manifest.get(key) != value
            }
            if mismatches:
                raise ValueError(f"existing run manifest does not match frozen pilot request: {mismatches}")
            if run_manifest.get("status") not in {"SUCCEEDED", "FAILED"}:
                raise ValueError(f"existing run has a nonterminal status: {run_manifest.get('status')}")
            if log_path.is_symlink() or not log_path.is_file():
                raise ValueError(f"existing run lacks a regular runner log: {log_path}")
            process = {
                "status": "SUCCEEDED" if run_manifest.get("status") == "SUCCEEDED" and run_manifest.get("exit_code") == 0 else "FAILED",
                "exit_code": run_manifest.get("exit_code"),
                "wall_seconds": round(elapsed_seconds(run_manifest.get("started_at"), run_manifest.get("finished_at")), 3),
                "log": str(log_path),
                "log_sha256": digest(log_path),
                "reused_existing_run": True,
            }
            started_at = run_manifest.get("started_at") or utc_now()
            finished_at = run_manifest.get("finished_at") or started_at
        else:
            started_at = utc_now()
            process = run_command(
                task["command"], env, log_path,
                args.timeout_seconds, args.cpu_cores, args.memory_gb,
            )
            run_manifest = None
            if run_manifest_path.is_file() and not run_manifest_path.is_symlink():
                run_manifest = load_json(run_manifest_path)
            finished_at = utc_now()
        effective = process["status"] == "SUCCEEDED" and isinstance(run_manifest, dict) and run_manifest.get("status") == "SUCCEEDED"
        outputs = []
        if effective:
            for output in run_manifest.get("canonical_outputs", []):
                output_path = Path(output)
                if output_path.is_file() and not output_path.is_symlink():
                    outputs.append(compare_to_truth(
                        output_path, task["truth_path"], task["canonical_cells"],
                        task["aliases"], task["expected_samples"],
                    ))
                else:
                    outputs.append({"estimate": str(output_path), "status": "MISSING_OR_SYMLINKED"})
        return {
            "case_id": task["case"]["case_id"],
            "method": task["case"]["method"],
            "fixture_id": task["case"]["fixture_id"],
            "stratum": task["matrix_row"]["stratum"],
            "baseline_status": task["matrix_row"]["baseline_status"],
            "execution_policy": task["matrix_row"].get("execution_policy"),
            "acceptance_policy_availability": task["matrix_row"].get("acceptance_policy_availability"),
            "execution_status": "SUCCEEDED" if effective else process["status"],
            "process": process,
            "started_at": started_at,
            "finished_at": finished_at,
            "command": task["command"],
            "input": str(task["input_path"]),
            "input_sha256": digest(task["input_path"]),
            "truth": str(task["truth_path"]),
            "truth_sha256": digest(task["truth_path"]),
            "reference_selector": task["selector"],
            "reference_manifest": str(task["manifest_path"]),
            "reference_manifest_sha256": task["manifest_sha256"],
            "reference_digest": task["matrix_row"]["reference_digest"],
            "replicate": task["replicate"],
            "run_manifest": str(run_manifest_path) if run_manifest_path.is_file() else None,
            "run_manifest_sha256": digest(run_manifest_path) if run_manifest_path.is_file() else None,
            "run_manifest_status": run_manifest.get("status") if isinstance(run_manifest, dict) else None,
            "runtime_images": run_manifest.get("runtime_images", {}) if isinstance(run_manifest, dict) else {},
            "outputs": outputs,
            "canonical_cell_axis": task["canonical_cells"],
            "expected_sample_axis": task["expected_samples"],
            "old_vs_new_regression_status": "NO_AUTHORITATIVE_PRIOR_OUTPUT" if not task["matrix_row"].get("authoritative_prior_output_paths") else "PENDING_EXACT_BINDING_REVIEW",
            "repeatability_disposition": "MEASURED_NO_GENERAL_THRESHOLD_INFERRED" if task["case"].get("repeat_count", 1) > 1 else "NOT_REPEATED_IN_PILOT",
            "scientific_state_promotion": "PROHIBITED",
        }

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(execute, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # Keep a failed case isolated; remaining cases continue.
                results.append({
                    "case_id": task["case"]["case_id"],
                    "replicate": task["replicate"],
                    "execution_status": "RUNNER_ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                    "outdir": str(task["outdir"]),
                    "scientific_state_promotion": "PROHIBITED",
                })
    results.sort(key=lambda row: (row.get("case_id", ""), row.get("replicate", 0)))
    successful = sum(row.get("execution_status") == "SUCCEEDED" for row in results)
    repeats_by_case: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        repeats_by_case.setdefault(row.get("case_id", ""), []).append(row)
    repeatability = {}
    for case in cases:
        group = sorted(repeats_by_case.get(case["case_id"], []), key=lambda row: row.get("replicate", 0))
        if len(group) < 2:
            repeatability[case["case_id"]] = {"status": "NOT_REPEATED_IN_PILOT"}
            continue
        record, _ = find_case_records(candidate, matrix, case)
        reference_id, version = record["selector"].rsplit("@", 1)
        manifest = load_json(args.reference_root / reference_id / version / "manifest.json")
        canonical_cells, aliases = aliases_from_manifest(manifest)
        expected_samples = list(record["fixture"]["sample_mapping_audit"]["expected_ids"])
        repeatability[case["case_id"]] = compare_replicates(group[0], group[1], aliases, canonical_cells, expected_samples)
    report = {
        "schema": SCHEMA,
        "generated_at": utc_now(),
        "status": "PILOT_COMPLETE" if successful == len(results) else "PILOT_PARTIAL_OR_FAILED",
        "scope": "READY + default-call native array Tier A only; selected cases from frozen baseline; no WGBS/derived/GPU/Slurm.",
        "baseline": {
            "candidate_inventory_path": str(args.candidate.resolve()),
            "candidate_inventory_sha256": inventory_digest,
            "candidate_inventory_declared_sha256": expected_inventory_digest,
            "freeze_candidate_path": str(freeze_candidate_path.resolve()),
            "freeze_candidate_sha256": digest(freeze_candidate_path),
            "pair_count": frozen.get("candidate_binding", {}).get("record_count"),
        },
        "software": {
            "repository_commit": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip() or None,
            "cli_path": str(args.cli.resolve()),
            "source_archive_sha256": args.source_sha256,
            "normalized_wheel_sha256": args.wheel_sha256,
        },
        "runtime": {
            "nextflow": str(args.nextflow.resolve()),
            "container_engine": str(args.container_engine.resolve()),
            "runtime_root": str(args.runtime_root.resolve()),
            "accelerator": "cpu",
            "executor": "local",
            "slurm": "NOT_USED",
            "network": "OFFLINE",
        },
        "runner_controls": {
            "max_cases": args.max_cases,
            "submitted_case_count": len(cases),
            "run_count_including_repeats": len(tasks),
            "concurrency": args.concurrency,
            "timeout_seconds_per_run": args.timeout_seconds,
            "cpu_affinity_cores_per_run": args.cpu_cores,
            "memory_limit_gb_per_process_rlimit_as": args.memory_gb,
            "cpu_time_limit_seconds_per_process": args.timeout_seconds,
            "retry_attempts": 0,
            "unique_output_directory_per_replicate": True,
            "failure_isolation": "A failed case is recorded; remaining scheduled cases continue.",
            "reused_existing_runs_without_rerun": args.reuse_existing_runs,
            "peak_memory": "NOT_RELIABLY_MEASURED",
        },
        "summary": {
            "case_count": len(cases),
            "run_count": len(results),
            "succeeded": successful,
            "failed_or_blocked": len(results) - successful,
            "truth_metrics_policy": "REPORT_ONLY_WHERE_NO_EXACT_OWNER_APPROVED_THRESHOLD_IS_BOUND",
            "ready_ru_status_changed": False,
        },
        "repeatability": repeatability,
        "results": results,
    }
    matrix_out = json.loads(json.dumps(matrix))
    updates = {
        (case["method"], case["fixture_id"]): case["case_id"]
        for case in cases
    }
    for matrix_row in matrix_out["rows"]:
        case_id = updates.get((matrix_row.get("method"), matrix_row.get("fixture_id")))
        if not case_id:
            continue
        group = repeats_by_case.get(case_id, [])
        statuses = [item.get("execution_status") for item in group]
        matrix_row["execution_status"] = "SUCCEEDED" if statuses and all(s == "SUCCEEDED" for s in statuses) else "FAILED_OR_BLOCKED"
        matrix_row["repeatability_status"] = repeatability.get(case_id, {}).get("status", "NOT_REPEATED_IN_PILOT")
        matrix_row["old_vs_new_regression_status"] = "NO_AUTHORITATIVE_PRIOR_OUTPUT" if not matrix_row.get("authoritative_prior_output_paths") else "PENDING_EXACT_BINDING_REVIEW"
        matrix_row["final_compatibility_status"] = "EXECUTION_PILOT_ONLY_NOT_ASSESSED"
        matrix_row["pilot_evidence"] = {
            "path": str(args.output_evidence.resolve()),
            "case_id": case_id,
        }
    matrix_out["status"] = "PARTIAL_LOCAL_TIER_A_PILOT; SCIENTIFIC_ACCEPTANCE_POLICY_GAPS_REMAIN"
    matrix_out["pilot_update"] = {
        "evidence_path": str(args.output_evidence.resolve()),
        "updated_case_count": len(updates),
        "baseline_status_promotion": "PROHIBITED",
    }
    args.output_matrix.parent.mkdir(parents=True, exist_ok=True)
    args.output_matrix.write_text(json.dumps(matrix_out, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["coverage_matrix"] = {"path": str(args.output_matrix.resolve()), "sha256": digest(args.output_matrix)}
    args.output_evidence.parent.mkdir(parents=True, exist_ok=True)
    args.output_evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "cases": len(cases), "runs": len(results), "succeeded": successful, "failed_or_blocked": len(results) - successful, "evidence": str(args.output_evidence)}, indent=2))
    return 0 if successful == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
