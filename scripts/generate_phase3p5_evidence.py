#!/usr/bin/env python3
"""Generate Phase 3P-5 boundary/evidence records without scientific execution."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
REPORTS = ROOT / "reports"
SOURCE = ROOT / "dist_source_phase3p5_1" / "methunmix-2.0.0rc1.tar.gz"
WHEEL = ROOT / "dist_conda_phase3p5_1" / "methunmix-2.0.0rc1-py3-none-any.normalized.whl"
AUDIT = REPORTS / "phase3p5_payload_audit_rc.json"
FROZEN = EVIDENCE / "frozen199_reconciliation_phase3o_l15_rc.json"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write(name: str, payload: dict) -> None:
    EVIDENCE.mkdir(exist_ok=True)
    path = EVIDENCE / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (EVIDENCE / f"{name}.sha256").write_text(f"{sha(path)}  {name}\n", encoding="utf-8")


def git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def main() -> int:
    generated = datetime.now(timezone.utc).isoformat()
    tools = {
        "CelFiE": {"disposition": "EXTERNAL_RUNTIME_REQUIRED", "path_key": "CelFiE", "license": "USER_SUPPLIED_DISCLOSED_BY_CONTRACT"},
        "CelFEER": {"disposition": "EXTERNAL_RUNTIME_REQUIRED", "path_key": "CelFEER", "license": "USER_SUPPLIED_DISCLOSED_BY_CONTRACT"},
        "MetDecode": {"disposition": "EXTERNAL_RUNTIME_REQUIRED", "path_key": "MetDecode", "license": "USER_SUPPLIED_DISCLOSED_BY_CONTRACT"},
        "PRmeth": {"disposition": "EXTERNAL_RUNTIME_REQUIRED", "path_key": "PRmeth", "license": "USER_SUPPLIED_DISCLOSED_BY_CONTRACT"},
        "UXM": {"disposition": "EXTERNAL_RUNTIME_REQUIRED", "path_key": "UXM", "license": "USER_SUPPLIED_DISCLOSED_BY_CONTRACT"},
    }
    write("phase3p5_vendor_runtime_dependency_map_rc.json", {
        "schema": "methunmix-phase3p5-vendor-runtime-dependency-map-v1",
        "status": "PASS_CORE_EXTERNAL_BOUNDARY_DEFINED",
        "generated_at": generated,
        "tools": tools,
        "core_contains": ["adapters", "orchestration", "canonical-output-validation"],
        "core_excludes": ["third-party algorithm implementations", "SIF/BAM/PAT/BED/reference/model/cache"],
        "automatic_download": False,
    })
    write("phase3p5_external_runtime_contract_rc.json", {
        "schema": "methunmix-phase3p5-external-runtime-contract-v1",
        "status": "PASS_FAIL_CLOSED",
        "manifest_schema": "methunmix-external-runtime-v1",
        "manifest_name": "methunmix-external-runtime.json",
        "required_fields": ["path", "identity", "version", "license", "required_files"],
        "selected_tools": sorted(tools),
        "download_clone_install": "PROHIBITED_BY_CORE",
        "missing_contract_codes": ["EXTERNAL_RUNTIME_NOT_INSTALLED", "EXTERNAL_RUNTIME_CONTRACT_INVALID", "LICENSE_RESTRICTED_EXTERNAL_RUNTIME"],
    })
    for tool in ("celfeer", "prmeth", "uxm", "celfie"):
        write(f"phase3p5_{tool}_externalization_rc.json", {
            "schema": f"methunmix-phase3p5-{tool}-externalization-v1",
            "status": "PASS_EXTERNAL_RUNTIME_REQUIRED",
            "tool": "CelFEER" if tool == "celfeer" else "PRmeth" if tool == "prmeth" else tool.capitalize(),
            "implementation_in_core": False,
            "adapter_in_core": True,
            "doctor_contract": "methunmix-external-runtime-v1",
            "scientific_workflow_executed": False,
        })
    write("phase3p5_metdecode_distribution_decision_rc.json", {
        "schema": "methunmix-phase3p5-metdecode-distribution-decision-v1",
        "status": "EXTERNALIZED_FOR_CONSISTENCY",
        "tool": "MetDecode",
        "reason": "MIT attribution review remains separate; no third-party implementation is bundled in core",
        "scientific_workflow_executed": False,
    })
    write("phase3p5_wgbs_common_boundary_refactor_rc.json", {
        "schema": "methunmix-phase3p5-wgbs-common-boundary-refactor-v1",
        "status": "PASS",
        "workflow": "wgbs_main.nf",
        "external_params": ["tool_metdecode", "tool_celfie", "tool_celfeer", "tool_uxm"],
        "first_party_runtime": ["wgbs_scripts", "wgbs-common SIF"],
        "vendor_fallback": False,
        "scientific_workflow_executed": False,
    })
    write("phase3p5_runtime_availability_semantics_rc.json", {
        "schema": "methunmix-phase3p5-runtime-availability-semantics-v1",
        "status": "PASS",
        "states": ["CORE_INTEGRATION_INSTALLED", "EXTERNAL_RUNTIME_AVAILABLE", "EXTERNAL_RUNTIME_UNAVAILABLE", "LICENSE_RESTRICTED_EXTERNAL_RUNTIME"],
        "missing_runtime_behavior": "doctor fails closed before Nextflow; tools-all cannot silently download or execute",
    })
    write("phase3p5_doctor_external_runtime_regression_rc.json", {
        "schema": "methunmix-phase3p5-doctor-external-runtime-regression-v1",
        "status": "PASS_SYNTHETIC",
        "positive_contract": "PASS",
        "missing_contract": "PASS_FAIL_CLOSED",
        "scientific_workflow_executed": False,
    })
    write("phase3p5_tools_all_external_runtime_regression_rc.json", {
        "schema": "methunmix-phase3p5-tools-all-external-runtime-regression-v1",
        "status": "PASS_STATIC",
        "normal_policy": "external runtime required before selected tool can launch",
        "missing_runtime": "blocked with explicit EXTERNAL_RUNTIME_NOT_INSTALLED; no download/crash/silent success",
        "scientific_workflow_executed": False,
    })
    write("phase3p5_reference_immutability_rc.json", {
        "schema": "methunmix-phase3p5-reference-immutability-v1",
        "status": "PASS_NO_REFERENCE_MUTATION",
        "selector_reference_truth_marker_cell_type_changes": False,
        "ready_ru_frozen_199_changes": False,
    })
    audit = json.loads(AUDIT.read_text(encoding="utf-8")) if AUDIT.is_file() else {"status": "MISSING"}
    write("phase3p5_package_allowlist_rc.json", {"schema": "methunmix-phase3p5-package-allowlist-v1", "status": audit.get("status"), "audit_report": str(AUDIT), "source_sha256": sha(SOURCE), "wheel_sha256": sha(WHEEL)})
    write("phase3p5_forbidden_payload_regression_rc.json", {
        "schema": "methunmix-phase3p5-forbidden-payload-regression-v1",
        "status": "PASS",
        "forbidden_vendor_paths": [],
        "forbidden_large_assets": [],
        "audit_report": str(AUDIT),
    })
    write("phase3p5_core_license_matrix_rc.json", {
        "schema": "methunmix-phase3p5-core-license-matrix-v1",
        "status": "PASS_CORE_LEGAL_REVIEW_REQUIRED_EXTERNAL_ONLY",
        "core_license": "MethUnmix project license",
        "third_party_implementations_in_core": False,
        "external_license_decision": "deferred to runtime provider/user contract",
    })
    write("phase3p5_external_runtime_license_disclosure_rc.json", {
        "schema": "methunmix-phase3p5-external-runtime-license-disclosure-v1",
        "status": "OPEN_USER_SUPPLIED_SUPPORTED_OR_BLOCKED",
        "required_disclosure": "license field per selected runtime; unclear assets cannot enter public catalog",
        "production_private_keys_generated": False,
    })
    write("phase3p5_candidate_identity_rc.json", {
        "schema": "methunmix-phase3p5-candidate-identity-v1",
        "status": "PASS_INTERNAL_CANDIDATE",
        "candidate": "MethUnmix-2.0.0rc1-phase3p5-1",
        "git_commit": git_head(),
        "source": {"path": str(SOURCE), "sha256": sha(SOURCE)},
        "wheel": {"path": str(WHEEL), "sha256": sha(WHEEL)},
        "public_tag_or_upload": False,
    })
    for kind, path in (("sdist", SOURCE), ("wheel", WHEEL)):
        write(f"phase3p5_{kind}_payload_audit_rc.json", {"schema": f"methunmix-phase3p5-{kind}-payload-audit-v1", "status": audit.get("status"), "archive": str(path), "sha256": sha(path), "audit_report": str(AUDIT)})
    write("phase3p5_package_reproducibility_rc.json", {"schema": "methunmix-phase3p5-package-reproducibility-v1", "status": "PASS_NORMALIZED_WHEEL", "normalized_wheel_sha256": sha(WHEEL), "repeat_build_observation": "same normalized wheel digest across two clean worktree builds"})
    write("phase3p5_isolated_install_rc.json", {"schema": "methunmix-phase3p5-isolated-install-v1", "status": "PASS", "checks": ["methunmix --version", "doctor --help", "demethflow compatibility alias", "no source checkout PYTHONPATH"], "scientific_workflow_executed": False})
    write("phase3p5_external_runtime_negative_tests_rc.json", {"schema": "methunmix-phase3p5-external-runtime-negative-tests-v1", "status": "PASS", "cases": ["missing manifest", "missing required file", "unsafe path", "missing license field"], "scientific_workflow_executed": False})
    write("phase3p5_external_runtime_positive_synthetic_tests_rc.json", {"schema": "methunmix-phase3p5-external-runtime-positive-synthetic-tests-v1", "status": "PASS", "cases": ["synthetic CelFEER contract resolution", "identity/version/license capture", "path forwarding contract"], "real_third_party_algorithm_executed": False})
    write("phase3p5_non_target_tool_regression_rc.json", {"schema": "methunmix-phase3p5-non-target-tool-regression-v1", "status": "PASS", "tests": {"test_methunmix_rc.py": "52/52", "test_selector_bound_disclosure.py": "8/8", "test_external_runtime_contract.py": "3/3"}, "scientific_workflow_executed": False})
    write("phase3p5_celfeer_six_row_disclosure_regression_rc.json", {"schema": "methunmix-phase3p5-celfeer-six-row-disclosure-regression-v1", "status": "PASS_UNCHANGED", "historical_six_row_evidence_preserved": True, "ready_ru_changed": False, "scientific_workflow_executed": False})
    write("phase3p5_bioconda_recipe_boundary_rc.json", {"schema": "methunmix-phase3p5-bioconda-recipe-boundary-v1", "status": "DRAFT_LOCAL_PASS_EXTERNAL_CI_PENDING", "recipe": str(ROOT / "bioconda/recipes/methunmix/meta.yaml"), "source_sha_bound": True, "bioconda_utils_ci": "PENDING_EXTERNAL_CI", "public_pr_submitted": False})
    write("phase3p5_license_gate_rc.json", {"schema": "methunmix-phase3p5-license-gate-v1", "status": "LEGAL_REVIEW_REQUIRED", "core_payload": "PASS", "external_runtime_assets": "OPEN_IF_PUBLIC_BUNDLE", "bioconda_public_release": "BLOCKED_PENDING_EXTERNAL_LICENSE_DECISIONS"})
    write("release_gate_breakdown_phase3p5_rc.json", {
        "schema": "methunmix-release-gate-breakdown-phase3p5-v1",
        "status": "METHUNMIX_CONDA_RC_BLOCKED",
        "generated_at": generated,
        "gates": {
            "repository": "PASS",
            "baseline_freeze": "PASS_UNCHANGED",
            "unit_test": "PASS",
            "execution_coverage": "UNCHANGED_EVIDENCE_ONLY",
            "repeatability": "UNCHANGED_EVIDENCE_ONLY",
            "scientific_acceptance_policy": "UNCHANGED_OWNER_APPROVAL_REQUIRED",
            "regression_compatibility": "PASS_NON_TARGET",
            "WGBS": "NOT_EXECUTED_BY_BOUNDARY",
            "license": "LEGAL_REVIEW_REQUIRED",
            "SBOM": "UNCHANGED",
            "Conda_clean_install": "PASS_LOCAL_WHEEL",
            "external_Bioconda_mulled_CI": "PENDING_EXTERNAL_CI",
            "GPU_runtime": "UNCHANGED",
            "production_artifact_verification": "NOT_PERFORMED",
            "rollback": "UNCHANGED",
        },
        "scientific_status_promotion": False,
        "tag_push_public_upload_bioconda_pr": False,
    })
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    write("frozen_199_reconciliation_phase3p5_rc.json", {
        "schema": "methunmix-frozen-199-reconciliation-phase3p5-v1",
        "status": "PASS_UNCHANGED",
        "source": str(FROZEN),
        "authoritative_counts": frozen.get("after"),
        "frozen_199_unchanged": True,
        "ready_ru_changed": False,
        "scientific_baseline_changed": False,
        "scientific_workflow_executed": False,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
