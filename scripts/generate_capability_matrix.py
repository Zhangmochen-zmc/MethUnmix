#!/usr/bin/env python3
"""Generate a manifest-derived, non-overclaiming deconvolution matrix.

This generator consumes the already inventoried reference manifests rather
than multiplying every tool by every route. A row is an execution candidate
only when an exact selector has an eligible tool status and the route/input/
device contract agrees with the registry. Candidate status is not a Conda or
redistribution approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "evidence/baseline_candidate_inventory.json"
DEFAULT_REGISTRY = ROOT / "src/methunmix_assets/capabilities/capability_registry.json"
DEFAULT_LICENSES = ROOT / "evidence/LICENSE_GAP_INVENTORY.json"
DEFAULT_OUTPUT = ROOT / "evidence/capability_matrix_rc.json"
RUNNABLE = {"READY", "RELEASED_UNVALIDATED"}
RUNNABLE_DEVICE = {"READY", "RELEASED_UNVALIDATED"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def map_license_status(license_path: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(license_path)
    tool_status: dict[str, dict[str, Any]] = {}
    for row in payload.get("items", []):
        asset_id = str(row.get("asset_id", ""))
        if not asset_id.startswith("tool-code:"):
            continue
        tool = asset_id.split(":", 1)[1]
        tool_status[tool] = {
            "proposed_code_distribution_status": row.get("proposed_distribution_status", "PENDING"),
            "license_status": row.get("license_status", "PENDING"),
            "owner_decision_required": bool(row.get("owner_decision_required", True)),
            "license_evidence_count": len(row.get("license_evidence", [])),
        }
    return tool_status


def _logical_children(manifest_tool: str, aliases: dict[str, list[str]]) -> list[str]:
    return aliases.get(manifest_tool, [manifest_tool])


def _contract_key(contract: str, source_platform: str) -> tuple[str, str]:
    return contract, source_platform


def _route_lookup(registry: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for route in registry.get("routes", []):
        key = _contract_key(route["analysis_contract"], route["source_platform"])
        if key in result:
            raise ValueError(f"Duplicate contract/source route definition: {key}")
        result[key] = route
    return result


def verify_manifest_sources(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hash only the small manifest files, never referenced large artifacts."""
    paths: dict[str, str] = {}
    for row in candidates:
        path = str(row.get("manifest_path", ""))
        declared = str(row.get("manifest_sha256", ""))
        if not path or not declared:
            raise ValueError(f"Candidate lacks manifest path/digest: {row.get('capability_id')}")
        previous = paths.setdefault(path, declared)
        if previous != declared:
            raise ValueError(f"One manifest path has conflicting declared digests: {path}")
    audits = []
    for raw_path, declared in sorted(paths.items()):
        path = Path(raw_path)
        if not path.is_file():
            raise ValueError(f"Inventoried reference manifest is missing: {path}")
        observed = sha256_file(path)
        if observed != declared:
            raise ValueError(f"Reference manifest digest changed since inventory: {path}")
        audits.append({"path": raw_path, "sha256": observed, "status": "MATCH"})
    return audits


def _status_reason(status: str | None) -> str:
    return {
        "QUARANTINED": "QUARANTINED",
        "NOT_AVAILABLE": "NOT_AVAILABLE",
        "NOT_ADAPTED": "NOT_ADAPTED",
        "PARTIAL": "REFERENCE_NOT_AVAILABLE",
        "UNAVAILABLE": "NOT_AVAILABLE",
    }.get(str(status), "NOT_AVAILABLE")


def _distribution(tool: str, license_status: dict[str, dict[str, Any]]) -> dict[str, Any]:
    aliases = {
        "EpiDISH-RPC": "EpiDISH", "EpiDISH-CP": "EpiDISH", "EpiDISH-CBS": "EpiDISH",
        "EMeth-normal": "EMeth", "EMeth-laplace": "EMeth",
    }
    code = license_status.get(tool) or license_status.get(aliases.get(tool, tool), {})
    proposed = code.get("proposed_code_distribution_status", "PENDING")
    if proposed == "BLOCKED":
        overall = "BLOCKED"
    else:
        # The public catalog currently has no downloadable targets. References
        # and runtimes therefore remain user-supplied regardless of code license.
        overall = "USER_SUPPLIED_SUPPORTED"
    return {
        "distribution_status": overall,
        "code_distribution_status": proposed,
        "external_reference_runtime_status": "USER_SUPPLIED_SUPPORTED",
        "distribution_approval_status": "PENDING_OWNER_REVIEW",
        "owner_decision_required": code.get("owner_decision_required", True),
        "license_status": code.get("license_status", "PENDING_TOOL_CODE_LICENSE_REVIEW"),
    }


def _required_assets(manifest_capability: dict[str, Any], selector: str, device: str) -> dict[str, Any]:
    artifacts = manifest_capability.get("artifacts", {})
    runtime_modules = manifest_capability.get("runtime_modules", [])
    profiles = manifest_capability.get("execution_profiles", {})
    device_profile = profiles.get(device, {}) if isinstance(profiles, dict) else {}
    profile_modules = device_profile.get("runtime_modules", []) if isinstance(device_profile, dict) else []
    data_modules = manifest_capability.get("data_modules", [])
    runtime_values = (
        (runtime_modules if isinstance(runtime_modules, list) else [])
        + (profile_modules if isinstance(profile_modules, list) else [])
    )
    return {
        "reference_selector": selector,
        "device": device,
        "reference_artifacts": {
            str(key): str(value)
            for key, value in sorted(artifacts.items())
            if isinstance(value, (str, int, float))
        } if isinstance(artifacts, dict) else {},
        "runtime_modules": sorted({str(value) for value in runtime_values}),
        "data_modules": sorted({str(value) for value in data_modules}) if isinstance(data_modules, list) else [],
    }


def _evidence_id(
    *, selector: str, manifest_sha256: str, artifact_map_digest: str | None,
    tool: str, manifest_tool: str, evidence: list[dict[str, Any]],
) -> str:
    # Stable locator over the exact immutable selector and referenced evidence;
    # this is an identifier, not a signature or a claim that the evidence passed.
    payload = {
        "selector": selector,
        "manifest_sha256": manifest_sha256,
        "artifact_checksum_map_digest": artifact_map_digest,
        "tool": tool,
        "manifest_tool": manifest_tool,
        "validation_evidence": [
            {"kind": item.get("kind"), "sha256": item.get("current_sha256")}
            for item in evidence if isinstance(item, dict)
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "evidence:" + hashlib.sha256(raw).hexdigest()[:20]


def _public_reproducibility_status(distribution_status: str) -> str:
    if distribution_status in {"BLOCKED", "QUARANTINED", "UNAVAILABLE"}:
        return "BLOCKED_PENDING_LICENSE_OR_CAPABILITY_REVIEW"
    if distribution_status == "USER_SUPPLIED_SUPPORTED":
        return "USER_SUPPLIED_ASSETS_REQUIRED_PUBLIC_IMPORT_TEST_PENDING"
    if distribution_status == "INTERNAL_VALIDATED":
        return "INTERNAL_ONLY_NOT_PUBLICLY_REPRODUCIBLE"
    return "PUBLIC_END_TO_END_CI_PENDING"


def _candidate_row(
    *, route: dict[str, Any], ref: dict[str, Any], tool: str,
    manifest_tool: str, input_type: str, device: str, science_status: str,
    device_status: str, execution_policy: str, license_status: dict[str, dict[str, Any]],
    mode_inherited: bool, required_assets: dict[str, Any],
) -> dict[str, Any]:
    build = ref["genome_build"]
    capability_id = ".".join((
        ref["scenario"], route["route_id"], route["input_platform"], build,
        input_type, tool, device,
    ))
    warnings = []
    if science_status == "RELEASED_UNVALIDATED":
        warnings.append("SCIENTIFIC_STATUS_RU")
    if device_status == "RELEASED_UNVALIDATED":
        warnings.append("DEVICE_PROFILE_RU")
    route_disclosures = (
        ["EXPLICIT_ROUTE_OPT_IN_REQUIRED"]
        if route.get("requires_explicit_opt_in") else []
    )
    tools_all_policy = (
        "INCLUDE_AFTER_ROUTE_OPT_IN"
        if execution_policy == "normal" and route.get("requires_explicit_opt_in")
        else "INCLUDE"
        if execution_policy == "normal"
        else "EXCLUDE_EXPLICIT_ONLY"
    )
    distribution = _distribution(tool, license_status)
    evidence = ref.get("validation_evidence", [])
    return {
        "capability_id": capability_id,
        "scenario": ref["scenario"],
        "route_id": route["route_id"],
        "route_family": route["route_family"],
        "input_platform": route["input_platform"],
        "target_platform": route["target_platform"],
        "source_platform": ref["source_platform"],
        "analysis_contract": ref["analysis_contract"],
        "genome_build": build,
        "input_type": input_type,
        "tool": tool,
        "manifest_tool": manifest_tool,
        "selector": ref["selector"],
        "manifest_sha256": ref["manifest_sha256"],
        "artifact_checksum_map_digest": ref.get("artifact_checksum_map_digest"),
        "evidence_id": _evidence_id(
            selector=ref["selector"],
            manifest_sha256=ref["manifest_sha256"],
            artifact_map_digest=ref.get("artifact_checksum_map_digest"),
            tool=tool,
            manifest_tool=manifest_tool,
            evidence=evidence,
        ),
        "required_assets": required_assets,
        "scientific_status": science_status,
        "device": device,
        "device_profile_status": device_status,
        "execution_policy": execution_policy,
        "requires_explicit_tool_selection": execution_policy == "explicit_only",
        "requires_explicit_route_opt_in": bool(route.get("requires_explicit_opt_in", False)),
        "tools_all_policy": tools_all_policy,
        "warning_required": bool(warnings),
        "release_disclosures": warnings,
        "route_disclosures": route_disclosures,
        "mode_status_inherited_from_parent": mode_inherited,
        "validation_scope": ref.get("validation_scope"),
        "validation_evidence": evidence,
        "conda_compatibility_status": "PENDING",
        "cpu_conda_status": "PENDING_EXTERNAL_CONDA_CI",
        "gpu_conda_status": "PENDING_GPU_CONDA_CI" if tool in {"MEnet", "MethylBERT"} else "NOT_APPLICABLE",
        "public_reproducibility": _public_reproducibility_status(distribution["distribution_status"]),
        **distribution,
    }


def generate(inventory: dict[str, Any], registry: dict[str, Any], licenses: dict[str, Any], manifest_audits: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = inventory.get("latest_reference_tool_candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("inventory.latest_reference_tool_candidates must be a list")
    aliases = registry.get("manifest_tool_aliases", {})
    routes_by_key = _route_lookup(registry)
    license_status = map_license_status_payload(licenses)
    gpu_tools = set(registry.get("gpu_tools", []))
    routes: dict[str, dict[str, Any]] = {route["route_id"]: route for route in registry.get("routes", [])}

    # Group the flattened inventory into immutable reference selectors.
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in candidates:
        key = (row["selector"], row["analysis_contract"], row["source_platform"], row["genome_build"])
        current = grouped.setdefault(key, {"reference": row, "tools": {}})
        if current["reference"]["manifest_sha256"] != row["manifest_sha256"]:
            raise ValueError(f"Selector has inconsistent manifest digests: {row['selector']}")
        current["tools"][row["tool"]] = row

    technical: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    legacy: list[dict[str, Any]] = []
    scope_mismatches: list[dict[str, Any]] = []
    seen_coverage: dict[str, set[tuple[str, str]]] = defaultdict(set)
    manifest_capability_cache: dict[str, dict[str, dict[str, Any]]] = {}

    for (_selector, contract, source_platform, build), group in sorted(grouped.items()):
        ref = group["reference"]
        if (contract, source_platform) in {(item["analysis_contract"], item["source_platform"]) for item in registry.get("legacy_contracts", [])}:
            legacy.append({
                "selector": ref["selector"], "scenario": ref["scenario"],
                "analysis_contract": contract, "source_platform": source_platform,
                "genome_build": build, "tool_statuses": {
                    tool: value.get("scientific_status") for tool, value in sorted(group["tools"].items())
                },
                "release_status": "QUARANTINED",
                "reason_code": "LEGACY_ROUTE_UNRESOLVED",
                "reason": "Not a native EPIC or explicit genome-build WGBS-projection route.",
            })
            continue

        route = routes_by_key.get((contract, source_platform))
        if route is None:
            scope_mismatches.append({"selector": ref["selector"], "contract": contract, "source_platform": source_platform, "reason_code": "ROUTE_NOT_REGISTERED"})
            continue
        if ref["scenario"] not in route["scenarios"] or build not in route["genome_builds"]:
            scope_mismatches.append({"selector": ref["selector"], "scenario": ref["scenario"], "contract": contract, "genome_build": build, "route_id": route["route_id"], "reason_code": "ROUTE_SCOPE_MISMATCH"})
            continue
        seen_coverage[route["route_id"]].add((ref["scenario"], build))

        # Routes specify either a common input family or a per-tool WGBS contract.
        if "default" in route.get("input_types_by_tool", {}):
            manifest_tools = route["manifest_tools"]
            logical_scope = [child for base in manifest_tools for child in _logical_children(base, aliases)]
            input_map = {tool: route["input_types_by_tool"]["default"] for tool in logical_scope}
        else:
            input_map = route.get("input_types_by_tool", {})
            logical_scope = list(input_map)

        for tool in logical_scope:
            manifest_tool = next((base for base in route["manifest_tools"] if tool in _logical_children(base, aliases)), tool)
            cap = group["tools"].get(manifest_tool)
            input_types = input_map[tool]
            manifest_path = str(ref.get("manifest_path", ""))
            if manifest_path not in manifest_capability_cache:
                manifest_payload = read_json(Path(manifest_path)) if manifest_path else {}
                manifest_capability_cache[manifest_path] = {
                    str(key): value
                    for key, value in manifest_payload.get("tool_capabilities", {}).items()
                    if isinstance(value, dict)
                }
            manifest_capability = manifest_capability_cache[manifest_path].get(manifest_tool, {})
            if cap is None:
                required_assets = _required_assets(manifest_capability, ref["selector"], "cpu")
                for input_type in input_types:
                    row = _candidate_row(
                        route=route, ref=ref, tool=tool, manifest_tool=manifest_tool,
                        input_type=input_type, device="cpu", science_status="NOT_AVAILABLE",
                        device_status="NOT_AVAILABLE", execution_policy="normal",
                        license_status=license_status, mode_inherited=tool != manifest_tool,
                        required_assets=required_assets,
                    )
                    row["reason_code"] = "REFERENCE_NOT_AVAILABLE"
                    blocked.append(row)
                continue

            science_status = str(cap.get("scientific_status", "NOT_AVAILABLE"))
            policy = str(cap.get("execution_policy") or "normal")
            profiles = cap.get("execution_profiles") or {}
            mode_inherited = tool != manifest_tool
            devices = ["cpu"]
            if tool in gpu_tools:
                devices.append("gpu")
            for device in devices:
                profile = profiles.get(device)
                device_required_assets = _required_assets(manifest_capability, ref["selector"], device)
                if device == "cpu":
                    device_status = str(profile.get("status")) if profile else science_status
                else:
                    device_status = str(profile.get("status")) if profile else "UNVALIDATED"
                for input_type in input_types:
                    row = _candidate_row(
                        route=route, ref=ref, tool=tool, manifest_tool=manifest_tool,
                        input_type=input_type, device=device, science_status=science_status,
                        device_status=device_status, execution_policy=policy,
                        license_status=license_status, mode_inherited=mode_inherited,
                        required_assets=device_required_assets,
                    )
                    if science_status not in RUNNABLE:
                        row["reason_code"] = _status_reason(science_status)
                        blocked.append(row)
                    elif policy not in {"normal", "explicit_only"}:
                        row["reason_code"] = "EXECUTION_POLICY_UNSUPPORTED"
                        blocked.append(row)
                    elif device_status not in RUNNABLE_DEVICE:
                        row["reason_code"] = "DEVICE_PROFILE_UNVALIDATED" if device_status == "UNVALIDATED" else _status_reason(device_status)
                        blocked.append(row)
                    else:
                        technical.append(row)

    # Explicitly enumerate declared route gaps; absent selectors never become valid rows.
    missing_references = []
    for route in registry.get("routes", []):
        observed = seen_coverage.get(route["route_id"], set())
        expected = {(scenario, build) for scenario in route["scenarios"] for build in route["genome_builds"]}
        for scenario, build in sorted(expected - observed):
            missing_references.append({
                "route_id": route["route_id"], "scenario": scenario,
                "genome_build": build, "reason_code": "REFERENCE_NOT_AVAILABLE",
            })

    # Contract failures are rules, not fake per-tool successes.
    invalid_rules = [
        {"rule_id": "wgbs-cram-out-of-scope", "reason_code": "CRAM_OUT_OF_SCOPE", "when": {"input_platform": "wgbs", "input_type": "cram"}, "must_fail_before_process_start": True},
        {"rule_id": "all-slurm-out-of-scope", "reason_code": "SLURM_OUT_OF_SCOPE", "when": {"executor": "slurm"}, "must_fail_before_process_start": True},
        {"rule_id": "derived-route-pat-input", "reason_code": "INPUT_NOT_SUPPORTED", "when": {"route_family": ["wgbs-to-epic", "wgbs-to-450k"], "input_type": "pat"}, "must_fail_before_process_start": True},
        {"rule_id": "native-wgbs-celfie-pat-input", "reason_code": "INPUT_NOT_SUPPORTED", "when": {"route_family": "wgbs-native", "tool": "CelFiE", "input_type": "pat"}, "must_fail_before_process_start": True},
        {"rule_id": "native-wgbs-bed-pat-tools", "reason_code": "INPUT_NOT_SUPPORTED", "when": {"route_family": "wgbs-native", "tool": ["CelFEER", "UXM", "MethylBERT"], "input_type": "bed"}, "must_fail_before_process_start": True},
        {"rule_id": "native-wgbs-pat-bed-tools", "reason_code": "INPUT_NOT_SUPPORTED", "when": {"route_family": "wgbs-native", "tool": ["CelFiE", "MetDecode", "MEnet"], "input_type": "pat"}, "must_fail_before_process_start": True},
        {"rule_id": "medecom-explicit-only", "reason_code": "EXPLICIT_ONLY", "when": {"tool": "MeDeCom", "tools_argument": "all"}, "must_fail_before_automatic_selection": True},
        {"rule_id": "unregistered-legacy-wgbs-epic", "reason_code": "LEGACY_ROUTE_UNRESOLVED", "when": {"source_platform": "wgbs", "analysis_contract": "array_epic_cpg"}, "must_fail_before_process_start": True},
    ]

    supported_names = set(registry.get("logical_tools", []))
    if not supported_names:
        raise ValueError("registry.logical_tools is empty")
    observed_names = {row["tool"] for row in technical} | {row.get("tool") for row in blocked if row.get("tool")}
    unrepresented = sorted(supported_names - observed_names)
    summary = {
        "logical_tool_count": len(supported_names),
        "technical_candidate_rows": len(technical),
        "blocked_candidate_rows": len(blocked),
        "legacy_quarantined_selectors": len(legacy),
        "missing_route_reference_combinations": len(missing_references),
        "scope_mismatches": len(scope_mismatches),
        "unrepresented_logical_tools": unrepresented,
        "technical_candidates_by_route": dict(sorted(Counter(row["route_id"] for row in technical).items())),
        "technical_candidates_by_scientific_status": dict(sorted(Counter(row["scientific_status"] for row in technical).items())),
        "blocked_by_reason": dict(sorted(Counter(row.get("reason_code", "UNKNOWN") for row in blocked).items())),
    }
    return {
        "schema": "methunmix-capability-matrix-v2",
        "product": "MethUnmix",
        "software_version": registry.get("software_version"),
        "platform_support": {
            "package_platform_support": registry.get("package_platform_support", []),
            "workflow_execution_platform_support": registry.get("workflow_execution_platform_support", []),
            "asset_architecture": registry.get("asset_architecture"),
            "container_architecture": registry.get("container_architecture"),
        },
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "manifest-derived deconvolution execution candidates; not Conda compatibility or public redistribution approval",
        "source_digests": {
            "registry_sha256": None,
            "baseline_inventory_sha256": None,
            "license_inventory_sha256": None,
        },
        "source_manifest_audit": manifest_audits,
        "route_contracts": registry.get("routes", []),
        "technical_candidates": sorted(technical, key=lambda row: row["capability_id"]),
        "blocked_candidates": sorted(blocked, key=lambda row: row["capability_id"]),
        "legacy_quarantined_candidates": sorted(legacy, key=lambda row: row["selector"]),
        "missing_references": missing_references,
        "scope_mismatches": scope_mismatches,
        "invalid_rules": invalid_rules,
        "summary": summary,
        "release_gates": {
            "conda_compatibility": "PENDING",
            "public_distribution": "PENDING_LICENSE_APPROVAL; catalog has zero public targets",
            "scientific_status_promotion": "PROHIBITED_BY_THIS_GENERATOR",
        },
    }


def map_license_status_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in payload.get("items", []):
        asset_id = str(row.get("asset_id", ""))
        if asset_id.startswith("tool-code:"):
            result[asset_id.split(":", 1)[1]] = {
                "proposed_code_distribution_status": row.get("proposed_distribution_status", "PENDING"),
                "license_status": row.get("license_status", "PENDING"),
                "owner_decision_required": bool(row.get("owner_decision_required", True)),
            }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--licenses", type=Path, default=DEFAULT_LICENSES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-live-manifest-check", action="store_true", help="use only inventoried manifest digests; not for release qualification")
    args = parser.parse_args()

    inventory = read_json(args.inventory)
    registry = read_json(args.registry)
    licenses = read_json(args.licenses)
    candidates = inventory.get("latest_reference_tool_candidates", [])
    manifest_audits = [] if args.skip_live_manifest_check else verify_manifest_sources(candidates)
    matrix = generate(inventory, registry, licenses, manifest_audits)
    matrix["source_digests"] = {
        "registry_sha256": sha256_file(args.registry),
        "baseline_inventory_sha256": sha256_file(args.inventory),
        "license_inventory_sha256": sha256_file(args.licenses),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(matrix, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"schema": matrix["schema"], "output": str(args.output), "summary": matrix["summary"], "status": "GENERATED_CANDIDATES_NOT_RELEASE_APPROVAL"}, ensure_ascii=False, indent=2))
    return 0 if not matrix["scope_mismatches"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
