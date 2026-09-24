#!/usr/bin/env python3
"""Build immutable pre-fix resource audit and frozen-199 impact inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METHODS = ("ARIC", "EMeth", "EpiSCORE", "MethAtlas", "PRmeth")
ARRAY_CONTRACTS = (
    "array_450k_cpg",
    "array_epic_cpg",
    "wgbs_derived_450k_cpg_v1",
    "wgbs_derived_epic_cpg_v1",
)
PRE_FIX_COMMIT = "7201efd60cedb524d6f47e3cf45693729885f901"
THREAD_VARIABLES = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS",
)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def at_commit(relative: str) -> str:
    return subprocess.run(
        ["git", "show", f"{PRE_FIX_COMMIT}:{relative}"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout


def disposition(row: dict) -> str:
    status = row.get("execution_status")
    if status == "SUCCEEDED":
        return "already_executed"
    if status == "TIMEOUT":
        return "timeout"
    if status in {"BLOCKED", "INPUT_CONTRACT_BLOCKED"} or row.get("blocker"):
        return "input_contract_blocked"
    return "unattempted"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, default=ROOT / "evidence/scientific_compatibility_coverage_matrix_tier_e_e1a_rc.json")
    parser.add_argument("--execution", type=Path, default=ROOT / "evidence/tier_e_e1a_execution_rc.json")
    parser.add_argument("--audit-output", type=Path, default=ROOT / "evidence/resource_contract_audit_rc.json")
    parser.add_argument("--inventory-output", type=Path, default=ROOT / "evidence/resource_contract_impacted_combinations_rc.json")
    parser.add_argument("--policy-output", type=Path, default=ROOT / "evidence/resource_contract_policy_rc.json")
    args = parser.parse_args()

    matrix = load(args.matrix)
    execution = load(args.execution)
    pre_450k = at_commit("src/methunmix_assets/deconvolution/450k_main.nf")
    pre_epic = at_commit("src/methunmix_assets/deconvolution/epic_main.nf")
    pre_methatlas = at_commit("src/methunmix_assets/deconvolution/bin/MethAtlas.py")
    pre_configs = {
        "450k": at_commit("src/methunmix_assets/deconvolution/450k.config"),
        "epic": at_commit("src/methunmix_assets/deconvolution/epic.config"),
    }
    assert all("cpus   = 1" in value for value in pre_configs.values())
    assert not any(variable in pre_450k or variable in pre_epic for variable in THREAD_VARIABLES)
    assert "with Pool() as p:" in pre_methatlas

    observed: dict[str, list[dict]] = {method: [] for method in METHODS}
    for case in execution["case_results"]:
        if case["method"] not in METHODS:
            continue
        for record in case["process_resource_records"]:
            observed[case["method"]].append({
                "combination_id": case["combination_id"],
                "scenario": case["scenario"],
                "declared_cpus": case["declared_cpu_request"],
                "average_cpu_percent": record["average_cpu_percent"],
                "average_cpu_equivalents": record["average_cpu_percent"] / 100.0,
                "realtime": record["realtime"],
                "peak_rss": record["peak_rss"],
                "peak_vmem": record["peak_vmem"],
            })

    common_r = {
        "parallel_backend": "R linear algebra through pthread OpenBLAS 0.3.30",
        "worker_thread_source": "OpenBLAS chose host-visible threads because no supported thread variable was set",
        "runtime_audit": {
            "thread_environment": {name: "UNSET" for name in THREAD_VARIABLES},
            "blas_library": "/opt/env/lib/libopenblasp-r0.3.30.so",
            "explicit_R_worker_pool_found": False,
        },
        "current_thread_control_mechanism": "NONE_AT_PRE_FIX_COMMIT",
        "task_cpus_propagated": False,
    }
    details = {
        "ARIC": {
            "nextflow_process": "RUN_ARIC",
            "parallel_backend": "NumPy/SciPy pthread OpenBLAS 0.3.30",
            "worker_thread_source": "OpenBLAS reported 96 default threads because no supported thread variable was set",
            "runtime_audit": {
                "threadpoolctl_num_threads": 96,
                "numpy_blas": "/opt/env/lib/libopenblasp-r0.3.30.so; pthreads",
                "thread_environment": {name: "UNSET" for name in THREAD_VARIABLES},
                "explicit_worker_pool_found": False,
            },
            "current_thread_control_mechanism": "NONE_AT_PRE_FIX_COMMIT",
            "task_cpus_propagated": False,
            "root_cause": "ARIC numerical kernels used the container's 96-thread default OpenBLAS pool; E1A-01 averaged 1.767 CPU equivalents against cpus=1.",
            "affected_source_files": ["450k_main.nf", "epic_main.nf", "bin/aric_decon.py"],
            "proposed_remediation": "export the six supported thread limits from task.cpus before invoking Python",
        },
        "EMeth": {
            **common_r,
            "nextflow_process": "RUN_EMeth",
            "root_cause": "EMeth performs repeated matrix decompositions/CV through unbounded pthread OpenBLAS; its adapter and package audit found no R worker pool.",
            "affected_source_files": ["450k_main.nf", "epic_main.nf", "bin/emeth_decon.R"],
            "proposed_remediation": "export the six supported thread limits from task.cpus before invoking R",
        },
        "EpiSCORE": {
            **common_r,
            "nextflow_process": "RUN_EpiSCORE",
            "root_cause": "Sequential MASS::rlm fits invoke unbounded pthread OpenBLAS; the adapter contains a serial sample loop and no R worker pool.",
            "affected_source_files": ["450k_main.nf", "epic_main.nf", "bin/episcore_decon_common.R"],
            "proposed_remediation": "export the six supported thread limits from task.cpus before invoking R",
        },
        "MethAtlas": {
            "nextflow_process": "RUN_MethAtlas",
            "parallel_backend": "Python multiprocessing.Pool plus NumPy/SciPy pthread OpenBLAS 0.3.29",
            "worker_thread_source": "Pool() defaulted to host-visible CPU count (96 in audited container); each worker also inherited unbounded numerical-library threads",
            "runtime_audit": {
                "host_visible_python_cpu_count": 96,
                "numpy_blas": "openblas 0.3.29 pthread build; MAX_THREADS=128",
                "thread_environment": {name: "UNSET" for name in THREAD_VARIABLES},
                "explicit_pool_found": True,
                "explicit_pool_process_limit_found": False,
            },
            "current_thread_control_mechanism": "NONE_AT_PRE_FIX_COMMIT",
            "task_cpus_propagated": False,
            "root_cause": "MethAtlas used multiprocessing.Pool() without processes= and did not bound worker-local BLAS threads.",
            "affected_source_files": ["450k_main.nf", "epic_main.nf", "bin/methatlas_decon.py", "bin/MethAtlas.py"],
            "proposed_remediation": "pass task.cpus as --workers, use Pool(processes=workers), and keep worker-local numerical libraries single-threaded",
        },
        "PRmeth": {
            **common_r,
            "nextflow_process": "RUN_PRmeth",
            "root_cause": "PRmeth matrix operations use unbounded pthread OpenBLAS; vendor and adapter source audit found no R worker pool.",
            "affected_source_files": ["450k_main.nf", "epic_main.nf", "bin/prmeth_decon.R", "vendor/prmeth_R"],
            "proposed_remediation": "export the six supported thread limits from task.cpus before invoking R",
        },
    }

    affected = [row for row in matrix["rows"] if row["method"] in METHODS]
    common_python_affected = [
        row for row in matrix["rows"] if row.get("analysis_contract") in ARRAY_CONTRACTS
    ]
    method_rows = []
    for method in METHODS:
        rows = [row for row in affected if row["method"] == method]
        counters = Counter(disposition(row) for row in rows)
        method_rows.append({
            "method": method,
            "declared_cpus": 1,
            "intended_cpu_policy": "ENFORCE_EXISTING_NEXTFLOW_TASK_CPUS_DECLARATION",
            "observed_e1a": observed[method],
            "affected_frozen_combination_count": len(rows),
            "affected_frozen_combination_disposition": dict(sorted(counters.items())),
            **details[method],
        })

    timestamp = datetime.now(timezone.utc).isoformat()
    audit = {
        "schema": "methunmix-resource-contract-audit-v2",
        "generated_at": timestamp,
        "audit_basis": {
            "pre_fix_commit": PRE_FIX_COMMIT,
            "e1a_execution": str(args.execution),
            "e1a_execution_sha256": sha256(args.execution),
            "frozen_matrix": str(args.matrix),
            "frozen_matrix_sha256": sha256(args.matrix),
        },
        "status": "ROOT_CAUSE_CONFIRMED_REMEDIATION_REQUIRED",
        "methods": method_rows,
        "additional_method_found": "ARIC",
        "common_array_python_backend": {
            "affected_processes": ["POST_PROCESS_*", "MERGE_TOOL_RESULTS"],
            "affected_frozen_combination_count": len(common_python_affected),
            "parallel_backend": "pandas/NumPy linked to pthread OpenBLAS 0.3.33 in array_env.sif",
            "direct_runtime_observation": {
                "host_visible_cpu_count": 96,
                "openblas_get_num_threads": 96,
                "thread_environment": {name: "UNSET" for name in THREAD_VARIABLES},
            },
            "root_cause": "Canonical post-processing imported pandas/NumPy without propagating task.cpus, so OpenBLAS initialized a 96-thread pool even for one-CPU processes.",
            "observed_post_fix_candidate_symptom": "targeted rerun RUN processes were bounded, but common post-processing and merge tasks still averaged about 3.7 to 6.9 CPU equivalents",
            "proposed_remediation": "propagate all six thread limits from task.cpus to every Python array post-processing and merge process",
        },
        "other_method_audit_scope": "ARIC was added because E1A showed 1.767 CPU equivalents and direct runtime inspection confirmed an unbounded numerical backend. The common Python canonicalization backend additionally affects every array and WGBS-derived-array combination, regardless of method. MethylCIBERSORT and MeDeCom retain their separate RUN-process resource contracts.",
        "scientific_status_change": "PROHIBITED",
    }
    inventory_rows = []
    for row in common_python_affected:
        components = ["COMMON_ARRAY_PYTHON_POSTPROCESS", "COMMON_ARRAY_MERGE"]
        if row["method"] in METHODS:
            components.insert(0, f"RUN_{row['method'].upper()}")
        inventory_rows.append({
            key: row.get(key) for key in (
                "method", "scenario", "platform", "route", "genome_build",
                "analysis_contract", "reference_selector", "baseline_status",
                "execution_status", "final_compatibility_status", "stratum",
            )
        } | {
            "resource_sensitive_components": components,
            "resource_regression_disposition": disposition(row),
        })
    summary = Counter(item["resource_regression_disposition"] for item in inventory_rows)
    inventory = {
        "schema": "methunmix-resource-contract-impacted-combinations-v2",
        "generated_at": timestamp,
        "frozen_matrix_sha256": sha256(args.matrix),
        "frozen_row_count": matrix["row_count"],
        "run_backend_affected_methods": list(METHODS),
        "common_python_backend_scope": list(ARRAY_CONTRACTS),
        "affected_count": len(inventory_rows),
        "disposition_counts": dict(sorted(summary.items())),
        "rows": inventory_rows,
        "rerun_scope": ["E1A-01", "E1A-02", "E1A-03", "E1A-04", "E1A-05", "E1A-07", "E1A-08", "E1A-10", "E1A-12", "E1A-13"],
        "rerun_scope_reason": "seven specified oversubscription cases, both ARIC cases discovered by the audit, E1A-13 because it uses the same affected PRmeth backend, and both 450K workflow executions exercise the common Python post-processing and merge backend without expanding scientific coverage",
        "scientific_status_change": "PROHIBITED",
    }
    policy = {
        "schema": "methunmix-resource-contract-policy-v2",
        "generated_at": timestamp,
        "policy": "Nextflow task.cpus is the total process CPU budget and must be propagated to every worker/thread backend.",
        "method_policies": {
            "ARIC": {"task_cpus": 1, "blas_threads": "task.cpus", "worker_pool": "none"},
            "EMeth": {"task_cpus": 1, "blas_threads": "task.cpus", "worker_pool": "none"},
            "EpiSCORE": {"task_cpus": 1, "blas_threads": "task.cpus", "worker_pool": "none"},
            "MethAtlas": {"task_cpus": 1, "sample_workers": "task.cpus", "blas_threads_per_worker": 1},
            "PRmeth": {"task_cpus": 1, "blas_threads": "task.cpus", "worker_pool": "none"},
            "array_python_postprocessing": {"task_cpus": 1, "blas_threads": "task.cpus", "worker_pool": "none"},
            "array_result_merge": {"task_cpus": 1, "blas_threads": "task.cpus", "worker_pool": "none"},
        },
        "approval_disposition": "OWNER_RESOURCE_POLICY_APPROVAL_NOT_REQUIRED_EXISTING_DECLARATION_ENFORCED",
        "approval_rationale": "Both packaged 450K/EPIC configs and immutable E1A declared one CPU for these processes; this remediation enforces rather than changes that policy.",
        "forbidden_workarounds": ["taskset-only", "runner-only environment", "host-specific hard coding", "E1A-only special case"],
        "scientific_status_change": "PROHIBITED",
    }
    write(args.audit_output, audit)
    write(args.inventory_output, inventory)
    write(args.policy_output, policy)
    print(json.dumps({
        "audit": str(args.audit_output), "inventory": str(args.inventory_output),
        "policy": str(args.policy_output), "affected": len(inventory_rows),
        "dispositions": dict(sorted(summary.items())),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
