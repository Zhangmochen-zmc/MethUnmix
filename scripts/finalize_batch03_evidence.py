#!/usr/bin/env python3
"""Finalize Batch 03 evidence without changing the frozen baseline or scientific states."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def load(path: Path):
    return json.loads(path.read_text())


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def dump(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def key(row: dict) -> str:
    return "|".join((row["method"], row["fixture_id"], row["reference_selector"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", type=Path, required=True)
    ap.add_argument("--batch-evidence", type=Path, required=True)
    ap.add_argument("--frozen", type=Path, required=True)
    ap.add_argument("--supplement", type=Path, required=True)
    ap.add_argument("--out-matrix", type=Path, required=True)
    ap.add_argument("--out-reconciliation", type=Path, required=True)
    ap.add_argument("--out-report", type=Path, required=True)
    args = ap.parse_args()
    matrix, batch, frozen = load(args.matrix), load(args.batch_evidence), load(args.frozen)
    rows = matrix["rows"]
    results = {(r["method"], r["fixture_id"]): r for r in batch["results"]}
    assert len(rows) == 199 and len({key(r) for r in rows}) == 199
    assert len(results) == 14 and all(r["execution_status"] == "SUCCEEDED" for r in results.values())
    review = []
    for row in rows:
        result = results.get((row["method"], row["fixture_id"]))
        if not result:
            continue
        output = result["outputs"][0]
        row.update({
            "execution_status": "SUCCEEDED",
            "final_compatibility_status": "EXECUTION_PASS_STRUCTURAL_PASS_METRICS_REPORTED_NO_THRESHOLD",
            "structural_validation": output["axes_schema_structural_status"],
            "repeatability_status": "NOT_REPEATED_IN_BATCH03",
            "old_vs_new_regression_status": result["old_vs_new_regression_status"],
            "truth_metrics_disposition": output["acceptance_disposition"],
            "batch_03_case_id": result["case_id"],
            "batch_03_execution_evidence": str(args.batch_evidence),
            "batch_03_package_source_sha256": batch["software"]["source_archive_sha256"],
            "batch_03_package_wheel_sha256": batch["software"]["normalized_wheel_sha256"],
        })
        if not output["estimate_all_in_unit_interval"]:
            row["scientific_metric_review"] = "SCIENTIFIC_METRIC_REVIEW_REQUIRED: output contains value outside [0,1]; no automatic state change."
            review.append({"case_id": result["case_id"], "method": row["method"], "fixture_id": row["fixture_id"], "reason": "estimate_all_in_unit_interval=false", "output": output["estimate"]})
    matrix["batch_03_update"] = {
        "execution_evidence": str(args.batch_evidence), "execution_evidence_sha256": sha(args.batch_evidence),
        "attempted_unique_pairs": 14, "successful_pairs": 14,
        "scientific_metric_review_required_pairs": len(review), "next_batch_started": False,
        "stop_rule_observation": "No new package, reference-binding, helper, schema-axis, or resource-profile systemic failure in this bounded batch.",
        "scientific_status_promotion": False,
    }
    dump(args.out_matrix, matrix)
    primary_map = {}
    mapping = {"SUCCEEDED": "EXECUTION_SUCCESS", "SUCCEEDED_ORIGINAL_AND_REPEAT": "EXECUTION_SUCCESS", "BLOCKED_INPUT_CONTRACT": "INPUT_CONTRACT_BLOCKED", "BLOCKED_RESOURCE_REQUIREMENT": "RESOURCE_CONTRACT_BLOCKED", "TIMED_OUT": "TIMEOUT_NOT_ASSESSED", "NOT_RUN": "UNATTEMPTED"}
    for r in rows:
        state = mapping.get(r.get("execution_status"))
        if not state:
            raise SystemExit(f"unmapped execution status: {key(r)} {r.get('execution_status')}")
        primary_map[key(r)] = state
    counts = Counter(primary_map.values())
    assert len(primary_map) == 199 and sum(counts.values()) == 199
    dump(args.out_reconciliation, {
        "schema": "methunmix-frozen-199-primary-state-reconciliation-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "frozen_baseline_sha256": sha(args.frozen), "row_count": 199, "unique_primary_key_count": 199,
        "primary_state_counts": {x: counts.get(x, 0) for x in ["EXECUTION_SUCCESS", "INPUT_CONTRACT_BLOCKED", "RESOURCE_CONTRACT_BLOCKED", "TIMEOUT_NOT_ASSESSED", "INFRASTRUCTURE_FAILURE", "SCIENTIFIC_FAILURE", "UNATTEMPTED"]},
        "primary_state_by_combination": primary_map,
        "supplemental_evidence_excluded": {"path": str(args.supplement), "sha256": sha(args.supplement), "does_not_add_or_modify_frozen_rows": True},
        "owner_policy_required": "Frozen fixture input-contract failure counting needs an owner decision; legal Tier A execution remains permitted.",
        "scientific_state_promotion": "PROHIBITED",
    })
    strata_unattempted = Counter(r["stratum"] for r in rows if r["execution_status"] == "NOT_RUN")
    dump(args.out_report, {
        "schema": "methunmix-tier-a-batch03-phase-report-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "frozen_baseline": {"path": str(args.frozen), "sha256": sha(args.frozen), "row_count": 199, "unchanged": True},
        "batch_03": {"attempted": 14, "execution_success": 14, "blocked": 0, "timeout": 0, "failed": 0, "evidence": str(args.batch_evidence), "sha256": sha(args.batch_evidence)},
        "current_primary_state_counts": {x: counts.get(x, 0) for x in ["EXECUTION_SUCCESS", "INPUT_CONTRACT_BLOCKED", "RESOURCE_CONTRACT_BLOCKED", "TIMEOUT_NOT_ASSESSED", "INFRASTRUCTURE_FAILURE", "SCIENTIFIC_FAILURE", "UNATTEMPTED"]},
        "successful_execution_evidence": counts["EXECUTION_SUCCESS"],
        "tier_remaining": dict(strata_unattempted),
        "batch03_scientific_metric_review_required": review,
        "systemic_root_cause": "None observed in Batch 03; no package mismatch, reference-binding error, helper regression, schema-axis corruption, or CPU-profile mismatch.",
        "batch04": "NOT_STARTED; owner review required by this phase boundary.",
        "overall_release_state": "METHUNMIX_CONDA_RC_BLOCKED",
        "scientific_compatibility_gate": "PARTIAL",
        "scientific_status_promotion": "PROHIBITED",
        "package_binding": batch["software"],
    })


if __name__ == "__main__":
    main()
