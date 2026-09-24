#!/usr/bin/env python3
"""Refresh only release evidence after the owner-authorized bounded Batch 04."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dump(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def update_evidence(matrix: Path, reconciliation: Path, report: Path) -> None:
    recon = load(reconciliation)
    phase = load(report)
    counts = recon["primary_state_counts"]
    tier = recon["tier_state_counts"]
    assert counts == {
        "EXECUTION_SUCCESS": 63,
        "INPUT_CONTRACT_BLOCKED": 14,
        "RESOURCE_CONTRACT_BLOCKED": 0,
        "TIMEOUT_NOT_ASSESSED": 3,
        "INFRASTRUCTURE_FAILURE": 0,
        "SCIENTIFIC_FAILURE": 0,
        "UNATTEMPTED": 119,
    }
    assert tier["A"] == {
        "EXECUTION_SUCCESS": 63,
        "INPUT_CONTRACT_BLOCKED": 14,
        "TIMEOUT_NOT_ASSESSED": 3,
        "UNATTEMPTED": 4,
    }
    assert phase["batch04"] == {
        "attempted": 8,
        "success": 8,
        "blocked": 0,
        "timeout": 0,
        "failed": 0,
        "evidence": "evidence/tier_a_batch04_execution_rc.json",
        "sha256": digest(ROOT / "evidence/tier_a_batch04_execution_rc.json"),
    }

    science_path = ROOT / "evidence/scientific_compatibility_audit_rc.json"
    science = load(science_path)
    science["updated_on"] = str(date.today())
    science["status"] = "PARTIAL_EXECUTION_COVERAGE_63_SUCCESSFUL_OF_199; BATCH04_COMPLETE_PAUSED; SCIENTIFIC_ACCEPTANCE_NOT_PASSED"
    science["claim_boundary"] = (
        "Sixty-three frozen baseline pairs have execution success evidence; fourteen frozen five-sample "
        "EDec/Tsisal rows are N>K input-contract blocked; three MethylCIBERSORT rows are bounded "
        "timeouts; and four Tier A MethylCIBERSORT rows remain unattempted pending explicit 1200-second "
        "owner authorization. The full scientific compatibility gate is not PASS. Truth metrics remain "
        "subject only to preapproved exact-scope policies; otherwise they are report-only. No scientific "
        "capability state was promoted."
    )
    coverage = science["current_execution_coverage"]
    coverage["composition"] = (
        "7 repeated Houseman pairs + 6 original pilot successes + 4 helper-repaired pilot pairs + "
        "12 Batch 01 pairs + 12 Batch 02 pairs + 14 Batch 03 pairs + 8 Batch 04 pairs. "
        "Fourteen original EDec/Tsisal rows remain N>K input-contract blocked; three MethylCIBERSORT "
        "rows remain bounded timeouts; four additional MethylCIBERSORT rows await an explicitly approved "
        "1200-second bound."
    )
    coverage["coverage_matrix"] = {"path": str(matrix.relative_to(ROOT)), "sha256": digest(matrix)}
    coverage["frozen_199_reconciliation"] = {"counts": counts, "path": str(reconciliation.relative_to(ROOT)), "sha256": digest(reconciliation)}
    coverage["not_run_pairs"] = 119
    coverage["successful_unique_pairs_with_execution_evidence"] = 63
    coverage["tier_a_remaining"] = 4
    coverage["tier_b_remaining"] = 7
    coverage["tier_e_remaining"] = 108
    coverage["unresolved_failed_or_blocked_pairs"] = 17
    linked = science["linked_execution_evidence"]
    linked.update({
        "path": "evidence/tier_a_batch04_execution_rc.json",
        "results_count": 63,
        "scope": (
            "Current cumulative evidence: 63 successful frozen pairs of 199, with fourteen N>K input-contract "
            "blocks, three bounded MethylCIBERSORT timeouts, and four remaining Tier A MethylCIBERSORT rows awaiting "
            "owner authorization for a 1200-second bound. Batch 04 added eight legal native-array CPU execution records."
        ),
        "status": "PARTIAL_PASS_EXECUTED_SCOPE",
    })
    science["not_performed"][0] = (
        "The remaining 119 of 199 frozen baseline pairs were not executed; Batch 04 is complete and paused. "
        "Four Tier A MethylCIBERSORT rows require separate explicit owner authorization before any 1200-second run."
    )
    dump(science_path, science)

    breakdown_path = ROOT / "evidence/release_gate_breakdown_rc.json"
    breakdown = load(breakdown_path)
    for gate in breakdown["gates"]:
        if gate["id"] == "execution_coverage_gate":
            gate["evidence"] = str(matrix.relative_to(ROOT))
            gate["reconciliation_evidence"] = str(reconciliation.relative_to(ROOT))
            gate["status"] = "PARTIAL_63_SUCCESSFUL_EXECUTION_PAIRS_119_NOT_RUN_17_CONTRACT_OR_TIMEOUT_CASES_4_TIER_A_PENDING_LONG_RUNTIME"
            gate["remaining"] = (
                "63 frozen combinations have successful execution evidence. Fourteen original five-sample EDec/Tsisal rows "
                "are INPUT_CONTRACT_BLOCKED; three MethylCIBERSORT rows are TIMEOUT_NOT_ASSESSED after bounded local CPU "
                "execution; four Tier A MethylCIBERSORT rows remain unattempted pending separate explicit 1200-second owner "
                "authorization. 119 pairs remain NOT_RUN (Tier A 4, B 7, E 108). No scientific status was promoted."
            )
        elif gate["id"] == "regression_compatibility_gate":
            gate["evidence"] = gate["evidence"] + "; evidence/tier_a_batch04_execution_rc.json"
            gate["remaining"] = (
                "Tested combinations lacking authoritative prior outputs remain explicitly marked NO_AUTHORITATIVE_PRIOR_OUTPUT; "
                "Batch 04 adds eight such bounded execution records. No temporary output was promoted to truth."
            )
    dump(breakdown_path, breakdown)


def update_rc_state(matrix: Path, reconciliation: Path, report: Path, release_audit: Path) -> None:
    state_path = ROOT / "evidence/rc_gate_state.json"
    state = load(state_path)
    audit = load(release_audit)
    latest = state["latest_local_audit"]
    latest.update({
        "path": str(release_audit.relative_to(ROOT)),
        "sha256": digest(release_audit),
        "status": audit["status"],
        "scientific_compatibility_coverage_matrix": str(matrix.relative_to(ROOT)),
        "scientific_compatibility_coverage_matrix_sha256": digest(matrix),
        "frozen_199_reconciliation": str(reconciliation.relative_to(ROOT)),
        "frozen_199_reconciliation_sha256": digest(reconciliation),
        "tier_a_batch04_execution": "evidence/tier_a_batch04_execution_rc.json",
        "tier_a_batch04_execution_sha256": digest(ROOT / "evidence/tier_a_batch04_execution_rc.json"),
        "tier_a_batch04_phase_report": str(report.relative_to(ROOT)),
        "tier_a_batch04_phase_report_sha256": digest(report),
        "scientific_compatibility_status": (
            "PARTIAL_EXECUTION_COVERAGE_63_SUCCESSFUL_OF_199; 14_INPUT_CONTRACT_BLOCKED; "
            "3_TIMEOUT_NOT_ASSESSED; 4_TIER_A_METHYLCIBERSORT_PENDING_OWNER_1200S; 119_UNATTEMPTED; BATCH04_COMPLETE_PAUSED"
        ),
        "scientific_compatibility_scope": (
            "199 frozen pairs: 63 successful execution records; fourteen original five-sample EDec/Tsisal rows remain N>K "
            "input-contract blocked; three MethylCIBERSORT rows timed out under bounded local execution; four Tier A "
            "MethylCIBERSORT rows are held pending explicit 1200-second authorization. No unapproved threshold or READY/RU change."
        ),
    })
    state["pending_gates"]["SCIENTIFIC_COMPATIBILITY_READY"] = (
        "The owner-frozen 199-pair baseline and acceptance-policy draft remain unchanged. Current cumulative execution evidence "
        "covers 63 successful pairs. Fourteen five-sample EDec/Tsisal rows are N>K input-contract blocked; three "
        "MethylCIBERSORT rows timed out under bounded local execution; four Tier A MethylCIBERSORT rows require explicit "
        "1200-second authorization before execution. The cumulative matrix records 119 NOT_RUN (Tier A 4, Tier B 7, Tier E 108). "
        "Truth metrics remain report-only unless an exact preapproved policy applies; no global threshold was invented, no "
        "external-generalization claim was made, and no READY/RU status changed. The full scientific compatibility gate remains PARTIAL, not PASS."
    )
    for completed_item in (
        "tier_a_batch04_eight_locked_legal_cpu_cases_passed_without_scope_expansion",
        "batch04_frozen_199_reconciliation_and_release_gate_evidence_refreshed_without_state_promotion",
    ):
        if completed_item not in state["completed_local_gates"]:
            state["completed_local_gates"].append(completed_item)
    dump(state_path, state)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("evidence", "rc-state"))
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--reconciliation", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--release-audit", type=Path)
    args = parser.parse_args()
    args.matrix = args.matrix.resolve()
    args.reconciliation = args.reconciliation.resolve()
    args.report = args.report.resolve()
    if args.release_audit is not None:
        args.release_audit = args.release_audit.resolve()
    if args.mode == "evidence":
        update_evidence(args.matrix, args.reconciliation, args.report)
    else:
        if args.release_audit is None:
            parser.error("--release-audit is required for rc-state")
        update_rc_state(args.matrix, args.reconciliation, args.report, args.release_audit)


if __name__ == "__main__":
    main()
