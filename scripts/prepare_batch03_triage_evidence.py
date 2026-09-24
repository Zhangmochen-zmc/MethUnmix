#!/usr/bin/env python3
"""Create bounded Batch 03 triage evidence without changing the frozen baseline."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def load(path: Path):
    with path.open() as handle:
        return json.load(handle)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def key(row: dict) -> str:
    return "|".join((row["method"], row["fixture_id"], row["reference_selector"]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--supplement", type=Path, required=True)
    parser.add_argument("--cpu4", type=Path, required=True)
    parser.add_argument("--output-matrix", type=Path, required=True)
    parser.add_argument("--exception-output", type=Path, required=True)
    parser.add_argument("--cpu-output", type=Path, required=True)
    parser.add_argument("--timeout-output", type=Path, required=True)
    parser.add_argument("--reconciliation-output", type=Path, required=True)
    args = parser.parse_args()

    matrix = load(args.matrix)
    frozen = load(args.frozen)
    candidate = load(args.candidate)
    supplement = load(args.supplement)
    cpu4 = load(args.cpu4)
    rows = matrix["rows"]
    fixture_by_id = {item["fixture_id"]: item for item in candidate["fixtures"]}
    assert len(rows) == 199
    assert len({key(row) for row in rows}) == 199

    # Current formalization: all frozen original N=5 rows remain blocked.  Their
    # selectors resolve to 5, 9, and 8 canonical cell types respectively.
    input_rows = [row for row in rows if row.get("execution_status") == "BLOCKED_INPUT_CONTRACT"]
    k_by_selector = {
        "builtin.brain.450k@1.5.0": 5,
        "builtin.brain.epic@1.5.0": 5,
        "builtin.breast.450k@1.5.0": 9,
        "builtin.epithelial.450k@1.6.0": 8,
    }
    exceptions = []
    for row in input_rows:
        k_value = k_by_selector[row["reference_selector"]]
        exceptions.append({
            "method": row["method"], "platform": row["platform"], "fixture_id": row["fixture_id"],
            "sample_count_n": 5, "cell_type_count_k": k_value,
            "minimum_required_n": k_value + 1, "violated_input_contract": "N > K",
            "frozen_fixture_digest": fixture_by_id[row["fixture_id"]]["input_files"][0]["sha256"],
            "reference_selector": row["reference_selector"],
            "reference_digest": row["reference_digest"],
            "primary_state": "INPUT_CONTRACT_BLOCKED",
            "source_execution_status": row["execution_status"],
            "evidence_paths": [
                "evidence/tier_a_remaining_batch_01_execution_escalated_rc.json"
                if "batch_01_case_id" in row else "evidence/tier_a_remaining_batch_02_execution_rc.json",
                "evidence/baseline_frozen.json",
            ],
        })
    write(args.exception_output, {
        "schema": "methunmix-frozen-fixture-exception-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "frozen_baseline_sha256": sha(args.frozen),
        "owner_policy_required": True,
        "owner_policy_question": "How should a frozen compatibility fixture that violates a tool's explicit N>K input contract count in the final scientific compatibility release gate?",
        "non_negotiable_controls": [
            "Do not rerun the original five-sample fixtures.", "Do not bypass N>K.",
            "Do not pad, clone, or fabricate samples.",
            "Do not substitute supplemental evidence for any frozen baseline row.",
        ],
        "frozen_input_contract_blocked_rows": exceptions,
        "supplemental_evidence": {
            "classification": "SUPPLEMENTARY_EXECUTION_EVIDENCE",
            "path": str(args.supplement), "sha256": sha(args.supplement),
            "excluded_from_frozen_execution_success_count": True,
            "summary": supplement.get("summary", {}),
            "provenance": "Owner-authorized simulated-data-50samples fixture; separate from the frozen five-sample fixture.",
        },
        "scientific_state_promotion": "PROHIBITED",
    })

    cpu_result = cpu4["results"][0]
    assert cpu_result["method"] == "MethylCIBERSORT"
    assert cpu_result["execution_status"] == "TIMED_OUT"
    target = [row for row in rows if row["method"] == "MethylCIBERSORT" and row["fixture_id"] == "breast.450k-native.not_applicable"]
    assert len(target) == 1
    target = target[0]
    target["execution_status"] = "TIMED_OUT"
    target["final_compatibility_status"] = "EXECUTION_TIMEOUT_NOT_ASSESSED"
    target["structural_validation"] = "NOT_ASSESSED_TIMEOUT"
    target["repeatability_status"] = "NOT_APPLICABLE_TIMEOUT"
    target["truth_metrics_disposition"] = "NOT_ASSESSED_TIMEOUT"
    target["blocker"] = {
        "class": "BOUNDED_RUNTIME_TIMEOUT_AFTER_CPU_REQUIREMENT_SATISFIED",
        "cpu_requirement_evidence": str(args.cpu_output),
        "execution_evidence": str(args.cpu4),
        "scientific_failure_inferred": False,
    }
    target["cpu4_rerun"] = {
        "allocated_cpu": cpu4["runner_controls"]["cpu_affinity_cores_per_run"],
        "wall_seconds": cpu_result["process"]["wall_seconds"],
        "exit_code": cpu_result["process"]["exit_code"],
        "peak_memory": "NOT_RELIABLY_MEASURED",
        "same_frozen_input_sha256": cpu_result["input_sha256"] == fixture_by_id[target["fixture_id"]]["input_files"][0]["sha256"],
        "same_reference_digest": cpu_result["reference_digest"] == target["reference_digest"],
        "same_truth_sha256": cpu_result["truth_sha256"] == fixture_by_id[target["fixture_id"]]["truth_files"][0]["sha256"],
        "same_wheel_sha256": cpu4["software"]["normalized_wheel_sha256"],
    }
    write(args.cpu_output, {
        "schema": "methunmix-methylcibersort-cpu-requirement-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": {"method": "MethylCIBERSORT", "fixture_id": target["fixture_id"], "selector": target["reference_selector"]},
        "requirement_evidence": {
            "nextflow_450k_profile": {"file": "src/methunmix_assets/deconvolution/450k.config", "line": 151, "declared_cpus": 4, "memory": "32 GB"},
            "nextflow_process": {"file": "src/methunmix_assets/deconvolution/450k_main.nf", "line": 307, "process": "RUN_MethylCIBERSORT"},
            "r_adapter": {"file": "src/methunmix_assets/deconvolution/bin/methylcibersort_decon.R", "lines": [16, 17, 66, 73], "finding": "CoreAlg uses three fixed workers; 450K profile has no outer sample worker argument."},
            "observed_batch02": "Process requirement exceeds available CPUs -- req: 4; avail: 2",
        },
        "conclusion": "MINIMUM_CPU_REQUIREMENT_CONFIRMED_4_FOR_450K",
        "rerun": {"path": str(args.cpu4), "sha256": sha(args.cpu4), "result": cpu_result},
        "outcome": "CPU_REQUIREMENT_SATISFIED_BUT_BOUNDED_180_SECOND_TIMEOUT",
        "scientific_state_promotion": "PROHIBITED",
    })

    timeout_cases = []
    for row in rows:
        if row["method"] == "MethylCIBERSORT" and row["fixture_id"] in {"brain.450k-native.not_applicable", "brain.epic-native.not_applicable"}:
            command = "methylcibersort_decon.R --perm 1000 --seed 20260916"
            if row["platform"] == "epic":
                command += " --sample_workers 2"
            timeout_cases.append({
                "fixture_id": row["fixture_id"], "platform": row["platform"], "selector": row["reference_selector"],
                "nextflow_process": "RUN_MethylCIBERSORT", "r_command_semantics": command,
                "allocated_cpu": 6, "timeout_seconds": 600,
                "terminal_observation": "R adapter emitted only argparse version warning before controller-enforced termination; no output result or progress log was emitted.",
                "measured_cpu_utilization": "NOT_CAPTURED", "measured_memory": "NOT_CAPTURED", "swap": "NOT_CAPTURED",
                "io_profile": "NOT_CAPTURED", "convergence_or_iteration_progress": "NOT_CAPTURED",
                "single_thread_bottleneck": "NOT_DETERMINABLE_FROM_STATIC_EVIDENCE",
                "classification": "UNKNOWN",
                "reason": "Both platforms entered the same RUN_MethylCIBERSORT R stage, but no time-series CPU/memory/I/O/progress telemetry survived operator termination; no safe diagnosis supports a timeout increase.",
                "recommend_bounded_1200_retry": False,
            })
    write(args.timeout_output, {
        "schema": "methunmix-methylcibersort-timeout-profile-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_evidence": "evidence/tier_a_remaining_batch_01_methylcibersort_retry_600s_rc.json",
        "source_evidence_sha256": sha(Path("evidence/tier_a_remaining_batch_01_methylcibersort_retry_600s_rc.json")),
        "cases": timeout_cases,
        "cross_platform_finding": "450K and EPIC both reached RUN_MethylCIBERSORT; EPIC additionally requested two outer sample workers. Neither run produced a final result before enforced termination.",
        "next_action": "No timeout expansion is justified from current telemetry. Obtain a separately authorized, instrumented profile before proposing a longer bound.",
    })

    primary = {}
    for row in rows:
        state = {
            "SUCCEEDED": "EXECUTION_SUCCESS",
            "SUCCEEDED_ORIGINAL_AND_REPEAT": "EXECUTION_SUCCESS",
            "BLOCKED_INPUT_CONTRACT": "INPUT_CONTRACT_BLOCKED",
            "BLOCKED_RESOURCE_REQUIREMENT": "RESOURCE_CONTRACT_BLOCKED",
            "TIMED_OUT": "TIMEOUT_NOT_ASSESSED",
            "NOT_RUN": "UNATTEMPTED",
        }.get(row.get("execution_status"))
        if state is None:
            raise SystemExit(f"unmapped row: {key(row)} -> {row.get('execution_status')}")
        primary[key(row)] = state
    counts = Counter(primary.values())
    assert sum(counts.values()) == 199
    write(args.reconciliation_output, {
        "schema": "methunmix-frozen-199-primary-state-reconciliation-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "frozen_baseline_sha256": sha(args.frozen), "row_count": len(rows), "unique_primary_key_count": len(primary),
        "primary_state_counts": {state: counts.get(state, 0) for state in [
            "EXECUTION_SUCCESS", "INPUT_CONTRACT_BLOCKED", "RESOURCE_CONTRACT_BLOCKED", "TIMEOUT_NOT_ASSESSED",
            "INFRASTRUCTURE_FAILURE", "SCIENTIFIC_FAILURE", "UNATTEMPTED",
        ]},
        "primary_state_by_combination": primary,
        "supplemental_evidence_excluded": {"path": str(args.supplement), "sha256": sha(args.supplement), "does_not_add_or_modify_frozen_rows": True},
        "owner_policy_required": "Frozen fixture input-contract failure counting needs owner policy; this does not block legal Tier A execution.",
        "scientific_state_promotion": "PROHIBITED",
    })
    matrix["batch_02_cpu4_methylcibersort_update"] = {
        "evidence": str(args.cpu4), "cpu_requirement_audit": str(args.cpu_output),
        "outcome": "TIMEOUT_NOT_ASSESSED_AFTER_CPU4", "scientific_status_promotion": False,
    }
    matrix["frozen_fixture_exception_evidence"] = str(args.exception_output)
    matrix["timeout_profile_evidence"] = str(args.timeout_output)
    matrix["reconciliation_evidence"] = str(args.reconciliation_output)
    write(args.output_matrix, matrix)


if __name__ == "__main__":
    main()
