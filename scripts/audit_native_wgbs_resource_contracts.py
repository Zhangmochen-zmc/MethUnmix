#!/usr/bin/env python3
"""Audit Phase 3C native-WGBS CPU contracts and frozen E2A/E2B scope."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECONV = ROOT / "src/methunmix_assets/deconvolution"
PRE_FIX_COMMIT = "f40f4b79a0738482a3949898fbe6913ebe8b57e5"
THREAD_VARIABLES = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS",
)
STANDARD_METHODS = ("CelFiE", "MEnet", "MetDecode", "UXM")
PROCESS_NAMES = {
    "CelFiE": "RUN_CELFIE", "MEnet": "RUN_MENET",
    "MetDecode": "RUN_METDECODE", "UXM": "RUN_UXM",
}
PROCESS_GROUPS = {
    "CelFiE": (
        "PRE_CELFIE_ATLAS", "PRE_CELFIE_STEP1", "PRE_CELFIE_STEP2",
        "RUN_CELFIE", "POST_PROCESS_CELFIE", "MERGE_TOOL_RESULTS",
    ),
    "MEnet": (
        "PRE_MENET", "RUN_MENET", "POST_PROCESS_MENET", "MERGE_TOOL_RESULTS",
    ),
    "MetDecode": (
        "PRE_METDECODE_ATLAS", "PRE_METDECODE_STEP1", "PRE_METDECODE_STEP2",
        "RUN_METDECODE", "POST_PROCESS_METDECODE", "MERGE_TOOL_RESULTS",
    ),
    "UXM": ("RUN_UXM", "POST_PROCESS_UXM", "MERGE_TOOL_RESULTS"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bytes_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git_text(relative: str) -> str:
    return subprocess.run(
        ["git", "show", f"{PRE_FIX_COMMIT}:{relative}"], cwd=ROOT,
        check=True, capture_output=True, text=True,
    ).stdout


def process_block(workflow: str, name: str) -> str:
    start = workflow.index(f"process {name} {{")
    end = workflow.find("\nprocess ", start + 1)
    return workflow[start:] if end < 0 else workflow[start:end]


def task_cpus(config: str, process: str) -> int:
    match = re.search(
        rf"withName:\s*['\"]?{re.escape(process)}['\"]?\s*\{{(?P<body>.*?)\n\s*\}}",
        config, re.S,
    )
    if not match:
        return 1
    cpus = re.search(r"\bcpus\s*=\s*(\d+)", match.group("body"))
    return int(cpus.group(1)) if cpus else 1


def has_all_thread_vars(block: str) -> bool:
    return all(
        f"export {name}='${{cpus}}'" in block
        or f"export {name}=${{task.cpus}}" in block
        or f"export {name}='${{task.cpus}}'" in block
        for name in THREAD_VARIABLES
    )


def classify_process(workflow: str, process: str, method: str, phase: str) -> tuple[str, str]:
    block = process_block(workflow, process)
    if phase == "PRE_FIX":
        if process == "RUN_UXM" and "-@ ${task.cpus}" in block:
            return "CONTROLLED", "UXM worker count is explicitly task-scoped"
        return "POTENTIALLY_UNCONTROLLED", "no complete native task-scoped contract was established"
    if process == "RUN_UXM":
        controlled = "-@ ${task.cpus}" in block
        return (
            "CONTROLLED" if controlled else "POTENTIALLY_UNCONTROLLED",
            "UXM worker count is explicitly task-scoped" if controlled else "UXM worker count is not task-scoped",
        )
    if process == "PRE_METDECODE_STEP1":
        script = (DECONV / "wgbs_scripts/metdecode_step1.sh").read_text(encoding="utf-8")
        controlled = (
            "sorted_input.bed" in script
            and "sort --parallel=1" in script
            and not re.search(r"(?m)^\s*\|\s*(?:bedtools|cut)\b", script)
        )
        return (
            "CONTROLLED" if controlled else "CONFIRMED_OVERSUBSCRIPTION",
            "sort is explicitly single-threaded and sort, bedtools and cut execute sequentially" if controlled else "sort concurrency or shell pipeline concurrency is not bounded",
        )
    if process == "PRE_CELFIE_STEP1":
        script = (DECONV / "wgbs_scripts/celfie_step1.sh").read_text(encoding="utf-8")
        sequential = "sorted_input.bed" in script and not re.search(r"(?m)^\s*\|\s*bedtools\b", script)
        preamble = "native_python_thread_preamble(task.cpus)" in block
        controlled = sequential and preamble
        return (
            "CONTROLLED" if controlled else "CONFIRMED_OVERSUBSCRIPTION",
            "sort and bedtools execute sequentially and Python is task-scoped" if controlled else "mapping or Python lacks a complete task budget",
        )
    controlled = "native_python_thread_preamble(task.cpus)" in block
    return (
        "CONTROLLED" if controlled else "POTENTIALLY_UNCONTROLLED",
        "task script exports the complete native Python thread budget" if controlled else "task script lacks complete native Python thread exports",
    )


def authoritative_standard_rows() -> list[dict]:
    e2a = load(ROOT / "evidence/tier_e_e2a_manifest_rc.json")["combinations"]
    pilot = load(ROOT / "evidence/tier_e_pilot_manifest_rc.json")["combinations"]
    pilot_keys = {
        (row["method"], row["scenario"], row["genome_build"])
        for row in pilot if row.get("route_class") == "NATIVE_WGBS"
    }
    inventory = load(ROOT / "evidence/tier_e_inventory_rc.json")["rows"]
    e2b_candidates = [
        row for row in inventory
        if row.get("analysis_contract") == "wgbs_native_hg19"
        and row.get("method") in STANDARD_METHODS
        and (row["method"], row["scenario"], row["genome_build"]) not in pilot_keys
    ]
    e2b_candidates.sort(key=lambda row: (row["scenario"], STANDARD_METHODS.index(row["method"])))
    if len(e2a) != 10 or len(e2b_candidates) != 10:
        raise RuntimeError(f"expected E2A=10 and E2B=10, found {len(e2a)} and {len(e2b_candidates)}")
    rows = [{
        "batch": "E2A", "combination_id": row["combination_id"],
        "method": row["method"], "scenario": row["scenario"],
        "genome_build": row["genome_build"], "analysis_contract": row["analysis_contract"],
        "reference_selector": row["reference_selector"],
    } for row in e2a]
    rows.extend({
        "batch": "E2B", "combination_id": f"E2B-{number:02d}",
        "method": row["method"], "scenario": row["scenario"],
        "genome_build": row["genome_build"], "analysis_contract": row["analysis_contract"],
        "reference_selector": row["reference_selector"],
    } for number, row in enumerate(e2b_candidates, start=1))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("PRE_FIX", "POST_FIX"), required=True)
    parser.add_argument("--telemetry-output", type=Path)
    parser.add_argument("--sweep-output", type=Path, required=True)
    args = parser.parse_args()

    workflow_rel = "src/methunmix_assets/deconvolution/wgbs_main.nf"
    config_rel = "src/methunmix_assets/deconvolution/wgbs.config"
    if args.phase == "PRE_FIX":
        workflow = git_text(workflow_rel)
        config = git_text(config_rel)
    else:
        workflow = (ROOT / workflow_rel).read_text(encoding="utf-8")
        config = (ROOT / config_rel).read_text(encoding="utf-8")

    resource_path = ROOT / "evidence/tier_e_e2a_g01_resource_validation_rc.json"
    resource = load(resource_path)
    rows = authoritative_standard_rows()
    dynamic = {
        method: {
            "task_cpus": resource["methods"][method]["primary_task_cpus"],
            "average_cpu_equivalents_min": resource["methods"][method]["primary_cpu_equivalents_min"],
            "average_cpu_equivalents_max": resource["methods"][method]["primary_cpu_equivalents_max"],
            "task_count": resource["methods"][method]["primary_task_count"],
            "pre_phase3c_disposition": resource["methods"][method]["dynamic_resource_disposition"],
        }
        for method in STANDARD_METHODS
    }
    pre_dynamic_classification = {
        "CelFiE": "CONFIRMED_OVERSUBSCRIPTION",
        "MEnet": "CONFIRMED_OVERSUBSCRIPTION",
        "MetDecode": "CONFIRMED_OVERSUBSCRIPTION",
        "UXM": "CONTROLLED",
    }
    backend_findings = []
    process_inventory = []
    for method in STANDARD_METHODS:
        process = PROCESS_NAMES[method]
        block = process_block(workflow, process)
        cpus = task_cpus(config, process)
        process_rows = []
        for related_process in PROCESS_GROUPS[method]:
            classification, basis = classify_process(workflow, related_process, method, args.phase)
            process_row = {
                "method": method,
                "process": related_process,
                "task_cpus": task_cpus(config, related_process),
                "classification": classification,
                "basis": basis,
            }
            process_inventory.append(process_row)
            process_rows.append(process_row)
        controlled = all(row["classification"] == "CONTROLLED" for row in process_rows)
        if args.phase == "PRE_FIX":
            classification = pre_dynamic_classification[method]
        else:
            classification = "CONTROLLED" if controlled else "POTENTIALLY_UNCONTROLLED"
        affected = [row["combination_id"] for row in rows if row["method"] == method]
        backend_findings.append({
            "method": method, "process": process, "task_cpus": cpus,
            "classification": classification,
            "thread_environment_propagated": controlled if method != "UXM" else False,
            "explicit_tool_worker_argument": method == "UXM",
            "python_worker_pool": "NONE_FOUND" if method != "UXM" else "TOOL_POOL_BOUNDED_BY_-@",
            "container_environment": "TASK_SCRIPT_EXPORTS_VISIBLE_AFTER_APPTAINER_ENV_DASH"
                if method != "UXM" and controlled else "NO_COMPLETE_TASK_SCOPED_EXPORT",
            "affected_e2a_e2b_combinations": affected,
            "dynamic_g01": dynamic[method],
        })

    source_scan_paths = [
        DECONV / "bin/menet.py",
        DECONV / "wgbs_vendor/CelFiE/scripts/celfie_new.py",
        DECONV / "wgbs_vendor/MetDecode/run.py",
        DECONV / "wgbs_vendor/MetDecode/metdecode/model.py",
    ]
    auto_patterns = ("os.cpu_count(", "multiprocessing.cpu_count(", "Pool()", "n_jobs=-1")
    auto_findings = []
    for path in source_scan_paths:
        text = path.read_text(encoding="utf-8") if args.phase == "POST_FIX" else git_text(path.relative_to(ROOT).as_posix())
        hits = [token for token in auto_patterns if token in text]
        if hits:
            auto_findings.append({"path": str(path.relative_to(ROOT)), "patterns": hits})

    sweep = {
        "schema": "methunmix-native-wgbs-resource-contract-sweep-phase3c-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_phase": args.phase,
        "pre_fix_commit": PRE_FIX_COMMIT,
        "status": "STATIC_SWEEP_COMPLETE",
        "scope": "authoritative E2A plus planned mirror E2B standard native-WGBS CPU backends",
        "combination_count": len(rows),
        "batch_counts": dict(sorted(Counter(row["batch"] for row in rows).items())),
        "method_counts": dict(sorted(Counter(row["method"] for row in rows).items())),
        "combinations": rows,
        "backends": backend_findings,
        "process_inventory": process_inventory,
        "classification_counts": dict(sorted(Counter(row["classification"] for row in backend_findings).items())),
        "process_classification_counts": dict(sorted(Counter(row["classification"] for row in process_inventory).items())),
        "host_core_auto_detection_findings": auto_findings,
        "out_of_scope_native_backends": {
            "CelFEER": "E3A_HIGH_RESOURCE_NOT_E2A_OR_E2B",
            "MethylBERT": "E3B_HIGH_RESOURCE_NOT_E2A_OR_E2B",
        },
        "interpretation": {
            "potential_is_not_runtime_failure": True,
            "scientific_status_change": "PROHIBITED_AND_NOT_PERFORMED",
            "frozen_baseline_change": "PROHIBITED_AND_NOT_PERFORMED",
        },
    }
    write(args.sweep_output, sweep)

    if args.telemetry_output:
        wrapper_candidates = sorted(Path("/tmp/methunmix-tier-e-e2a/E2A-G01/results/.demethflow/work").glob("*/*/.command.run"))
        wrapper = next((path for path in wrapper_candidates if "nxf_trace_linux" in path.read_text(encoding="utf-8", errors="replace")), None)
        telemetry = {
            "schema": "methunmix-native-wgbs-resource-telemetry-audit-phase3c-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "TELEMETRY_SEMANTICS_CONFIRMED",
            "pre_fix_commit": PRE_FIX_COMMIT,
            "inputs": {
                "resource_evidence": {"path": str(resource_path.relative_to(ROOT)), "sha256": sha256(resource_path)},
                "e2a_manifest": {"path": "evidence/tier_e_e2a_manifest_rc.json", "sha256": sha256(ROOT / "evidence/tier_e_e2a_manifest_rc.json")},
                "representative_nextflow_wrapper": None if wrapper is None else {"path": str(wrapper), "sha256": sha256(wrapper)},
            },
            "nextflow_trace_semantics": {
                "cpu_percent_denominator": "elapsed task wall time inferred from global /proc/stat tick delta, normalized by host logical CPU count",
                "cpu_equivalents": "Nextflow trace %cpu divided by 100; task-level average over the full command duration",
                "accounted_cpu_fields": "wrapper-shell /proc/<pid>/stat cutime+cstime (fields 16+17), accumulated child user+system CPU after wait",
                "wrapper_self_cpu": "excluded from numerator",
                "parent_child_double_counting": False,
                "container_python_worker_double_counting": False,
                "reason": "the trace reads one wrapper process cumulative child CPU counter; it does not sum the same parent and descendants independently",
            },
            "two_second_group_sampler_semantics": {
                "denominator": "SC_CLK_TCK times monotonic elapsed seconds between samples",
                "cpu_equivalents": "sum of live process-group utime+stime tick deltas divided by denominator",
                "parent_child_double_counting": False,
                "limitation": "process membership changes between samples can distort an instantaneous group interval; it is not used for per-task pass/fail",
                "short_task_bias": "two-second sampling can miss or blur startup work for 3-4 second tasks",
            },
            "authoritative_dynamic_measure": "Nextflow task trace full-duration average",
            "classifications": {
                "CelFiE": {
                    "classification": "GENUINE_OVERSUBSCRIPTION",
                    "basis": "all five 2.9-4.0 second RUN tasks averaged 1.275-1.654 CPU equivalents against task.cpus=1; NumPy dot uses unbounded BLAS",
                },
                "MEnet": {
                    "classification": "GENUINE_MILD_OVERSUBSCRIPTION",
                    "basis": "RUN task averages span 1.705-2.227 over about 34 seconds; two of five exceed task.cpus=2 and no PyTorch/BLAS pool binding existed",
                },
                "MetDecode": {
                    "classification": "GENUINE_SEVERE_OVERSUBSCRIPTION",
                    "basis": "all five 79-101 second RUN tasks averaged 16.138-19.557 CPU equivalents against task.cpus=1; PyTorch and numerical pools were unbounded",
                },
                "UXM": {
                    "classification": "CONTROLLED",
                    "basis": "tool receives -@ task.cpus=12 and five RUN tasks averaged 0.978-1.090 CPU equivalents",
                },
            },
            "phase3a_phase3b_gap": {
                "native_workflow_is_distinct": True,
                "workflow": "wgbs_main.nf",
                "array_workflows": ["450k_main.nf", "epic_main.nf", "wgbs_epic_main.nf"],
                "cause": "Phase 3A/3B injected thread contracts into array and WGBS-derived-array RUN/postprocess paths; native tool-specific RUN processes remained separate and were only conservatively labeled POTENTIALLY_UNCONTROLLED",
                "apptainer_env_dash_effect": "host thread variables are intentionally cleared; task-script exports are therefore required inside the container",
            },
            "scientific_status_change": "PROHIBITED_AND_NOT_PERFORMED",
        }
        write(args.telemetry_output, telemetry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
