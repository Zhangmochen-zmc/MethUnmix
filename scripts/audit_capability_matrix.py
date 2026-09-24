#!/usr/bin/env python3
"""Strict static audit of the manifest-derived capability matrix."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "evidence" / "capability_matrix_rc.json"
REGISTRY = ROOT / "src" / "methunmix_assets" / "capabilities" / "capability_registry.json"
RUNNABLE = {"READY", "RELEASED_UNVALIDATED"}


def expected_public_reproducibility(distribution_status: str) -> str:
    if distribution_status in {"BLOCKED", "QUARANTINED", "UNAVAILABLE"}:
        return "BLOCKED_PENDING_LICENSE_OR_CAPABILITY_REVIEW"
    if distribution_status == "USER_SUPPLIED_SUPPORTED":
        return "USER_SUPPLIED_ASSETS_REQUIRED_PUBLIC_IMPORT_TEST_PENDING"
    if distribution_status == "INTERNAL_VALIDATED":
        return "INTERNAL_ONLY_NOT_PUBLICLY_REPRODUCIBLE"
    return "PUBLIC_END_TO_END_CI_PENDING"


def expected_evidence_id(row: dict) -> str:
    payload = {
        "selector": row.get("selector"),
        "manifest_sha256": row.get("manifest_sha256"),
        "artifact_checksum_map_digest": row.get("artifact_checksum_map_digest"),
        "tool": row.get("tool"),
        "manifest_tool": row.get("manifest_tool"),
        "validation_evidence": [
            {"kind": item.get("kind"), "sha256": item.get("current_sha256")}
            for item in row.get("validation_evidence", []) if isinstance(item, dict)
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "evidence:" + hashlib.sha256(raw).hexdigest()[:20]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="also write JSON audit evidence")
    args = parser.parse_args()
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    reasons: list[str] = []
    if matrix.get("schema") != "methunmix-capability-matrix-v2":
        reasons.append("matrix is not generated with the manifest-derived v2 schema")

    technical = matrix.get("technical_candidates", [])
    blocked = matrix.get("blocked_candidates", [])
    invalid_rules = matrix.get("invalid_rules", [])
    if not isinstance(technical, list) or not isinstance(blocked, list) or not isinstance(invalid_rules, list):
        raise SystemExit("matrix technical_candidates/blocked_candidates/invalid_rules must be arrays")

    tools = set(registry.get("logical_tools", []))
    routes = {route["route_id"]: route for route in registry.get("routes", [])}
    known_reasons = set(registry.get("declared_invalid_reason_codes", []))
    gpu_tools = set(registry.get("gpu_tools", []))
    distribution_states = {
        "PUBLIC_BUNDLED", "PUBLIC_DOWNLOADABLE", "USER_SUPPLIED_SUPPORTED",
        "INTERNAL_VALIDATED", "UNAVAILABLE", "BLOCKED", "QUARANTINED",
    }
    expected_platform_support = {
        "package_platform_support": registry.get("package_platform_support", []),
        "workflow_execution_platform_support": registry.get("workflow_execution_platform_support", []),
        "asset_architecture": registry.get("asset_architecture"),
        "container_architecture": registry.get("container_architecture"),
    }
    if matrix.get("platform_support") != expected_platform_support:
        reasons.append("matrix platform-support declaration does not match the capability registry")
    ids: list[str] = []
    seen_selectors: set[str] = set()

    def audit_release_fields(row: dict, label: str) -> None:
        capability_id = row.get("capability_id", label)
        if row.get("distribution_status") not in distribution_states:
            reasons.append(f"unknown distribution_status: {capability_id}")
        if row.get("cpu_conda_status") != "PENDING_EXTERNAL_CONDA_CI":
            reasons.append(f"CPU Conda state is promoted without a Conda E2E: {capability_id}")
        expected_gpu_conda = "PENDING_GPU_CONDA_CI" if row.get("tool") in gpu_tools else "NOT_APPLICABLE"
        if row.get("gpu_conda_status") != expected_gpu_conda:
            reasons.append(f"GPU Conda state is inconsistent with the registered GPU tool set: {capability_id}")
        if row.get("public_reproducibility") != expected_public_reproducibility(str(row.get("distribution_status"))):
            reasons.append(f"public reproducibility disclosure is inconsistent: {capability_id}")
        if row.get("evidence_id") != expected_evidence_id(row):
            reasons.append(f"evidence locator does not bind the selector/tool/evidence row: {capability_id}")
        required_assets = row.get("required_assets")
        if (
            not isinstance(required_assets, dict)
            or required_assets.get("reference_selector") != row.get("selector")
            or required_assets.get("device") != row.get("device")
            or not isinstance(required_assets.get("reference_artifacts"), dict)
            or not isinstance(required_assets.get("runtime_modules"), list)
            or not isinstance(required_assets.get("data_modules"), list)
        ):
            reasons.append(f"required assets are missing or malformed: {capability_id}")

    for row in technical:
        capability_id = row.get("capability_id")
        ids.append(capability_id)
        seen_selectors.add(row.get("selector"))
        if row.get("tool") not in tools:
            reasons.append(f"unknown logical tool in technical candidate: {row.get('tool')}")
        route = routes.get(row.get("route_id"))
        if not route:
            reasons.append(f"unknown route in technical candidate: {row.get('route_id')}")
            continue
        if row.get("analysis_contract") != route.get("analysis_contract") or row.get("source_platform") != route.get("source_platform"):
            reasons.append(f"contract/source mismatch: {capability_id}")
        if row.get("scenario") not in route.get("scenarios", []) or row.get("genome_build") not in route.get("genome_builds", []):
            reasons.append(f"candidate outside declared route scope: {capability_id}")
        allowed_inputs = route.get("input_types_by_tool", {})
        tool_inputs = allowed_inputs.get(row.get("tool"), allowed_inputs.get("default", []))
        if row.get("input_type") not in tool_inputs:
            reasons.append(f"unsupported input was marked technical: {capability_id}")
        if row.get("scientific_status") not in RUNNABLE:
            reasons.append(f"non-runnable scientific status marked technical: {capability_id}")
        if row.get("device_profile_status") not in RUNNABLE:
            reasons.append(f"unvalidated device profile marked technical: {capability_id}")
        if row.get("execution_policy") not in {"normal", "explicit_only"}:
            reasons.append(f"unknown execution policy: {capability_id}")
        if row.get("conda_compatibility_status") != "PENDING":
            reasons.append(f"matrix incorrectly promotes Conda compatibility: {capability_id}")
        if row.get("distribution_approval_status") != "PENDING_OWNER_REVIEW":
            reasons.append(f"matrix incorrectly promotes redistribution approval: {capability_id}")
        if row.get("requires_explicit_tool_selection") != (row.get("execution_policy") == "explicit_only"):
            reasons.append(f"explicit-only behavior not represented: {capability_id}")
        route_opt_in = bool(route.get("requires_explicit_opt_in", False))
        expected_tools_all = (
            "EXCLUDE_EXPLICIT_ONLY"
            if row.get("execution_policy") == "explicit_only"
            else "INCLUDE_AFTER_ROUTE_OPT_IN"
            if route_opt_in
            else "INCLUDE"
        )
        if row.get("tools_all_policy") != expected_tools_all:
            reasons.append(f"--tools all selection policy is inconsistent: {capability_id}")
        expected_warnings = []
        if row.get("scientific_status") == "RELEASED_UNVALIDATED":
            expected_warnings.append("SCIENTIFIC_STATUS_RU")
        if row.get("device_profile_status") == "RELEASED_UNVALIDATED":
            expected_warnings.append("DEVICE_PROFILE_RU")
        if row.get("release_disclosures") != expected_warnings:
            reasons.append(f"release disclosure list does not match capability status: {capability_id}")
        if row.get("warning_required") != bool(expected_warnings):
            reasons.append(f"warning_required does not match release disclosures: {capability_id}")
        expected_route_disclosures = ["EXPLICIT_ROUTE_OPT_IN_REQUIRED"] if route_opt_in else []
        if row.get("route_disclosures") != expected_route_disclosures:
            reasons.append(f"route disclosure list does not match route policy: {capability_id}")
        audit_release_fields(row, str(capability_id))

    for row in blocked:
        ids.append(row.get("capability_id"))
        seen_selectors.add(row.get("selector"))
        if row.get("tool") not in tools:
            reasons.append(f"unknown logical tool in blocked candidate: {row.get('tool')}")
        if row.get("reason_code") not in known_reasons:
            reasons.append(f"blocked candidate has undeclared reason code: {row.get('reason_code')}")
        audit_release_fields(row, str(row.get("capability_id")))

    if len(ids) != len(set(ids)):
        reasons.append("capability IDs are not unique across technical and blocked candidates")
    if len(registry.get("logical_tools", [])) != 21 or len(tools) != 21:
        reasons.append(f"expected exactly 21 unique logical tools, found {len(tools)}")
    unrepresented = matrix.get("summary", {}).get("unrepresented_logical_tools", [])
    if unrepresented:
        reasons.append(f"logical tools have no actual manifest candidate or explicit blocked row: {unrepresented}")
    if matrix.get("scope_mismatches"):
        reasons.append(f"reference manifests fall outside the declared route scope: {len(matrix['scope_mismatches'])}")

    rule_ids = [rule.get("rule_id") for rule in invalid_rules]
    if len(rule_ids) != len(set(rule_ids)):
        reasons.append("invalid rule IDs are not unique")
    for rule in invalid_rules:
        if rule.get("reason_code") not in known_reasons:
            reasons.append(f"invalid rule has undeclared reason: {rule.get('reason_code')}")
        if rule.get("must_fail_before_process_start") is not True and rule.get("must_fail_before_automatic_selection") is not True:
            reasons.append(f"negative rule lacks a pre-execution enforcement assertion: {rule.get('rule_id')}")

    legacy = matrix.get("legacy_quarantined_candidates", [])
    if any(row.get("release_status") != "QUARANTINED" or row.get("reason_code") != "LEGACY_ROUTE_UNRESOLVED" for row in legacy):
        reasons.append("legacy WGBS-sourced array_epic_cpg references are not quarantined")
    if any(row.get("source_platform") == "wgbs" and row.get("analysis_contract") == "array_epic_cpg" for row in technical):
        reasons.append("legacy WGBS-sourced array_epic_cpg was incorrectly counted as a current route")

    audits = matrix.get("source_manifest_audit", [])
    if not audits or any(row.get("status") != "MATCH" for row in audits):
        reasons.append("current reference manifest digests were not all verified")
    required_digests = matrix.get("source_digests", {})
    if any(not required_digests.get(key) for key in ("registry_sha256", "baseline_inventory_sha256", "license_inventory_sha256")):
        reasons.append("matrix input inventory digests are incomplete")

    payload = {
        "schema": "methunmix-capability-matrix-audit-v2",
        "status": "PASS" if not reasons else "FAIL",
        "technical_candidate_rows": len(technical),
        "blocked_candidate_rows": len(blocked),
        "legacy_quarantined_selectors": len(legacy),
        "invalid_rule_count": len(invalid_rules),
        "verified_source_manifest_count": len(audits),
        "unique_selector_count": len(seen_selectors),
        "route_gaps": len(matrix.get("missing_references", [])),
        "reasons": reasons,
        "read_only": True,
        "claim_boundary": "A PASS validates matrix construction and evidence binding only; it does not establish Conda compatibility, license approval, scientific performance, or public release readiness.",
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if not reasons else 2


if __name__ == "__main__":
    raise SystemExit(main())
