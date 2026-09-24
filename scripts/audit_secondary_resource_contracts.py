#!/usr/bin/env python3
"""Conservative static CPU-contract sweep for the frozen-199 matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)
ARRAY_WORKFLOWS = ("450k_main.nf", "epic_main.nf")
LEGACY_PUBLIC_WORKFLOW = "wgbs_epic_main.nf"
SECONDARY_METHODS = ("EpiDISH", "Houseman", "RefFreeEWAS")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def process_block(workflow: str, name: str) -> str:
    marker = f"process {name} {{"
    start = workflow.index(marker)
    next_process = workflow.find("\nprocess ", start + len(marker))
    return workflow[start:] if next_process < 0 else workflow[start:next_process]


def has_thread_budget(block: str) -> bool:
    return all(
        f"export {name}='${{task.cpus}}'" in block
        or f"export {name}=${{task.cpus}}" in block
        for name in THREAD_VARIABLES
    )


def source_auto_detection(source: str) -> list[str]:
    patterns = ("detectCores", "cpu_count", "Pool()", "Pool( )")
    return [pattern for pattern in patterns if pattern in source]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--matrix",
        type=Path,
        default=ROOT / "evidence/scientific_compatibility_coverage_matrix_tier_e_e1b_rc.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evidence/frozen_199_resource_contract_sweep_rc.json",
    )
    parser.add_argument("--phase", choices=("PRE_FIX", "POST_FIX"), required=True)
    args = parser.parse_args()

    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    rows = matrix["rows"]
    if len(rows) != 199:
        raise SystemExit(f"expected frozen-199 matrix, found {len(rows)} rows")

    deconv = ROOT / "src/methunmix_assets/deconvolution"
    workflow_text = {
        name: (deconv / name).read_text(encoding="utf-8")
        for name in (*ARRAY_WORKFLOWS, LEGACY_PUBLIC_WORKFLOW, "wgbs_main.nf")
    }

    secondary_controls: dict[str, dict[str, bool]] = {}
    for method in SECONDARY_METHODS:
        process = f"RUN_{method}"
        secondary_controls[method] = {
            name: has_thread_budget(process_block(workflow_text[name], process))
            for name in (*ARRAY_WORKFLOWS, LEGACY_PUBLIC_WORKFLOW)
        }

    controlled_array = {"ARIC", "EMeth", "EpiSCORE", "MethAtlas", "PRmeth", "MeDeCom"}
    potentially_uncontrolled_array = {"EDec", "MEnet", "MethylCIBERSORT", "Tsisal"}
    controlled_native = {"MethylBERT", "UXM"}
    potentially_uncontrolled_native = {"CelFEER", "CelFiE", "MEnet", "MetDecode"}

    findings = []
    for row in rows:
        method = row["method"]
        contract = row.get("analysis_contract", "")
        is_native_wgbs = contract.startswith("wgbs_native_")
        if method in SECONDARY_METHODS:
            all_frozen_routes_controlled = all(
                secondary_controls[method][name] for name in ARRAY_WORKFLOWS
            )
            classification = (
                "CONTROLLED" if all_frozen_routes_controlled else "CONFIRMED_OVERSUBSCRIPTION"
            )
            rationale = (
                "all six thread variables are propagated from task.cpus"
                if all_frozen_routes_controlled
                else "E1B telemetry confirmed oversubscription and the RUN process does not propagate task.cpus"
            )
        elif not is_native_wgbs and method in controlled_array:
            classification = "CONTROLLED"
            rationale = "explicit task.cpus propagation/worker bound established by Phase 3A or tool ncores contract"
        elif not is_native_wgbs and method in potentially_uncontrolled_array:
            classification = "POTENTIALLY_UNCONTROLLED"
            rationale = "no universal task-scoped numerical-library limit; no confirmed oversubscription in the audited case"
        elif is_native_wgbs and method in controlled_native:
            classification = "CONTROLLED"
            rationale = "explicit tool thread argument or deterministic one-thread runtime contract"
        elif is_native_wgbs and method in potentially_uncontrolled_native:
            classification = "POTENTIALLY_UNCONTROLLED"
            rationale = "Python numerical backend has no complete task.cpus-to-thread propagation contract"
        else:
            classification = "NOT_APPLICABLE"
            rationale = "no resource-sensitive execution backend identified for this frozen row"
        findings.append({
            "combination_id": row.get("combination_id"),
            "method": method,
            "scenario": row.get("scenario"),
            "platform": row.get("platform"),
            "genome_build": row.get("genome_build"),
            "analysis_contract": contract,
            "baseline_status": row.get("baseline_status"),
            "resource_contract_classification": classification,
            "rationale": rationale,
        })

    process_inventory = []
    for workflow_name, text in workflow_text.items():
        for match in re.finditer(r"(?m)^process\s+([A-Za-z0-9_]+)\s*\{", text):
            name = match.group(1)
            block = process_block(text, name)
            command_kind = "R" if re.search(r"(?:Rscript|\.R\b)", block) else (
                "PYTHON" if re.search(r"python(?:3)?\b", block) else "OTHER"
            )
            if command_kind == "OTHER":
                classification = "NOT_APPLICABLE"
                rationale = "no direct R/Python numerical command in this process block"
            elif has_thread_budget(block):
                classification = "CONTROLLED"
                rationale = "all six numerical thread variables use task.cpus"
            elif "--ncores '${task.cpus}'" in block or "-@ ${task.cpus}" in block:
                classification = "CONTROLLED"
                rationale = "explicit tool worker/thread argument uses task.cpus"
            elif name in {"RUN_METHYLBERT", "RUN_METHYLBERT_COMPACT"}:
                classification = "CONTROLLED"
                rationale = "runtime fixes CPU numerical threads to one and disables data-loader workers"
            elif name.startswith("POST_PROCESS_") or name.startswith("MERGE_"):
                classification = (
                    "CONTROLLED" if "python_thread_preamble(task.cpus)" in block
                    else "POTENTIALLY_UNCONTROLLED"
                )
                rationale = "common Python post-processing contract" if classification == "CONTROLLED" else "Python post-process lacks explicit thread budget"
            elif name in {f"RUN_{method}" for method in SECONDARY_METHODS}:
                classification = "CONFIRMED_OVERSUBSCRIPTION"
                rationale = "E1B telemetry confirmation and missing thread propagation"
            else:
                classification = "POTENTIALLY_UNCONTROLLED"
                rationale = "direct R/Python process without a complete static thread/worker contract"
            process_inventory.append({
                "workflow": workflow_name,
                "process": name,
                "command_kind": command_kind,
                "classification": classification,
                "rationale": rationale,
            })

    source_files = sorted((deconv / "bin").glob("*.py")) + sorted((deconv / "bin").glob("*.R"))
    auto_detection = []
    for path in source_files:
        hits = source_auto_detection(path.read_text(encoding="utf-8", errors="replace"))
        if hits:
            auto_detection.append({"path": str(path.relative_to(ROOT)), "patterns": hits})

    payload = {
        "schema": "methunmix-frozen-199-resource-contract-sweep-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_phase": args.phase,
        "status": "STATIC_SWEEP_COMPLETE",
        "matrix": str(args.matrix),
        "matrix_sha256": sha256(args.matrix),
        "row_count": len(findings),
        "classification_counts": dict(sorted(Counter(
            item["resource_contract_classification"] for item in findings
        ).items())),
        "secondary_method_controls": secondary_controls,
        "combination_findings": findings,
        "process_inventory": process_inventory,
        "process_classification_counts": dict(sorted(Counter(
            item["classification"] for item in process_inventory
        ).items())),
        "host_core_auto_detection_findings": auto_detection,
        "legacy_public_route_note": "wgbs_epic_main.nf is public but has no row in frozen-199; it is audited separately and must receive the same three-method fix.",
        "interpretation": {
            "confirmed_is_runtime_proven": True,
            "potential_is_not_a_failure_claim": True,
            "scope_expansion": "PROHIBITED_WITHOUT_OWNER_AUTHORIZATION",
            "scientific_status_change": "PROHIBITED",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
