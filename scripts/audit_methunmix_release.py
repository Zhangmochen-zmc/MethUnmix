#!/usr/bin/env python3
"""Fast, read-only release audit for the MethUnmix Conda workspace."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "src/methunmix_assets/capabilities/capability_registry.json"
CATALOG = ROOT / "src/methunmix_assets/assets/catalog.json"
RECIPE = ROOT / "bioconda/recipes/methunmix/meta.yaml"
SOURCE_ARCHIVE = ROOT / "dist_source_static_catalog_rc1_helper_fix" / "methunmix-2.0.0rc1.tar.gz"
STAGED_WHEEL = ROOT / "dist_conda_static_catalog_rc1_clean_helper_fix" / "methunmix-2.0.0rc1-py3-none-any.whl"
NAME_AUDIT = ROOT / "evidence/name_identity_audit.json"
BASELINE = ROOT / "evidence/baseline_frozen.json"
PRODUCTION = ROOT / "evidence/production_artifact_verification.json"
LICENSE_MATRIX = ROOT / "evidence/LICENSE_MATRIX.template.csv"
LICENSE_INVENTORY = ROOT / "evidence/LICENSE_GAP_INVENTORY.json"
SBOM = ROOT / "evidence/SBOM_CORE_RC.json"
RECIPE_AUDIT = ROOT / "evidence/conda_recipe_audit_rc.json"
PAYLOAD_AUDIT = ROOT / "evidence/conda_payload_audit_rc.json"
INSTALL_AUDIT = ROOT / "evidence/clean_conda_install_audit_rc.json"
SCIENCE_AUDIT = ROOT / "evidence/scientific_compatibility_audit_rc.json"
GPU_AUDIT = ROOT / "evidence/gpu_claims_audit_rc.json"
ROLLBACK_AUDIT = ROOT / "evidence/catalog_rollback_audit_rc.json"
MATRIX = ROOT / "evidence/capability_matrix_rc.json"
MATRIX_AUDIT = ROOT / "evidence/capability_matrix_audit_rc.json"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_file(name: str, path: Path, reasons: list[str], evidence: dict) -> bool:
    ok = path.is_file()
    evidence[name] = {"path": str(path), "exists": ok, "sha256": digest(path) if ok else None}
    if not ok:
        reasons.append(f"missing required file: {path}")
    return ok


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def empty_catalog_policy_valid(catalog: dict) -> bool:
    """Require an explicit, safe fallback when no public assets are listed."""
    if catalog.get("targets"):
        return True
    policy = catalog.get("empty_catalog_policy")
    return bool(
        isinstance(policy, dict)
        and policy.get("allowed") is True
        and policy.get("fallback") == "USER_SUPPLIED_SUPPORTED_OR_BLOCKED"
        and isinstance(policy.get("reason"), str)
        and policy["reason"].strip()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="also write the JSON audit evidence")
    args = parser.parse_args()
    reasons: list[str] = []
    evidence: dict = {}
    check_file("registry", REGISTRY, reasons, evidence)
    check_file("catalog", CATALOG, reasons, evidence)
    check_file("recipe", RECIPE, reasons, evidence)
    check_file("capability matrix", MATRIX, reasons, evidence)
    check_file("capability matrix audit", MATRIX_AUDIT, reasons, evidence)
    reproducible_build_path = ROOT / "evidence/reproducible_build_audit_rc.json"
    check_file("reproducible build audit", reproducible_build_path, reasons, evidence)
    check_file("core RC SBOM", SBOM, reasons, evidence)
    registry = read_json(REGISTRY)
    catalog = read_json(CATALOG)
    # Capability registry v2 names this list `logical_tools`; accept the legacy
    # key only for older staged registries so release audit cannot report a
    # false zero-tool failure after matrix schema migration.
    tools = registry.get("logical_tools", registry.get("tools", []))
    if len(tools) != 21 or len(set(tools)) != 21:
        reasons.append(f"expected 21 unique logical tools, found {len(tools)}")
    catalog_schema_ok = (
        catalog.get("schema") == "methunmix-static-catalog-v1"
        and catalog.get("trust_model") == "HTTPS_STATIC_CATALOG_SHA256"
        and isinstance(catalog.get("catalog_sequence"), int)
        and bool(catalog.get("catalog_release"))
        and isinstance(catalog.get("targets"), list)
        and empty_catalog_policy_valid(catalog)
    )
    if not catalog_schema_ok:
        reasons.append("catalog does not satisfy the MethUnmix 2.0 static-catalog schema")
    public_targets = [item for item in catalog.get("targets", []) if isinstance(item, dict) and item.get("distribution_status") == "PUBLIC_DOWNLOADABLE"]
    invalid_public_targets = []
    for item in public_targets:
        url = str(item.get("url", ""))
        if (
            item.get("license_status") != "LICENSE_APPROVED"
            or not item.get("license_evidence")
            or item.get("immutable_object") is not True
            or not re.fullmatch(r"[0-9a-fA-F]{64}", str(item.get("sha256", "")))
            or str(item.get("sha256", "")).lower() not in url.lower()
            or not url.startswith("https://")
            or not isinstance(item.get("length"), int)
            or item.get("length", 0) < 1
        ):
            invalid_public_targets.append(item.get("asset_id", "<unknown>"))
    if invalid_public_targets:
        reasons.append(f"public catalog targets fail immutable HTTPS/SHA256/license checks: {invalid_public_targets}")
    if RECIPE.is_file():
        text = RECIPE.read_text(encoding="utf-8")
        if "REPLACE_WITH_FINAL_SOURCE_SHA256" in text:
            reasons.append("Conda recipe still contains the final source checksum placeholder")
        if "https://github.com/zhangmch/MethUnmix/releases/" not in text:
            reasons.append("recipe does not use the stable MethUnmix release artifact URL")
        if SOURCE_ARCHIVE.is_file():
            match = re.search(r"^\s*sha256:\s*([0-9a-fA-F]{64})\s*$", text, re.MULTILINE)
            staged_sha = digest(SOURCE_ARCHIVE)
            if not match:
                reasons.append("recipe has no parseable source SHA256")
            elif match.group(1).lower() != staged_sha:
                reasons.append("recipe source SHA256 does not match staged source archive")
    for label, path in (("name identity audit", NAME_AUDIT), ("baseline evidence", BASELINE), ("production artifact evidence", PRODUCTION)):
        if not path.is_file():
            reasons.append(f"missing {label}: {path}")
    name_status = read_json(NAME_AUDIT).get("status")
    if name_status != "OWNER_AND_LEGAL_APPROVED":
        reasons.append("name/identity gate is not approved")
    if BASELINE.is_file():
        baseline = read_json(BASELINE)
        if not (baseline.get("baseline_frozen") is True and baseline.get("tolerance_preapproved") is True):
            reasons.append("baseline is not frozen with pre-approved tolerances")
    if PRODUCTION.is_file() and read_json(PRODUCTION).get("status") != "PASS":
        reasons.append("production artifact has not been reverified")
    license_items = []
    if LICENSE_INVENTORY.is_file():
        license_items = read_json(LICENSE_INVENTORY).get("items", [])
    if any(item.get("proposed_distribution_status") == "BLOCKED" for item in license_items):
        reasons.append("license inventory contains blocked bundled third-party assets")
    if LICENSE_MATRIX.is_file() and "PENDING" in LICENSE_MATRIX.read_text(encoding="utf-8"):
        reasons.append("license approval template is not an approved license matrix")
    sbom_report = read_json(SBOM)
    sbom_properties = {
        item.get("name"): item.get("value")
        for item in sbom_report.get("metadata", {}).get("component", {}).get("properties", [])
        if isinstance(item, dict)
    }
    source_bound = bool(
        SOURCE_ARCHIVE.is_file()
        and sbom_properties.get("methunmix:sourceArchiveSha256") == digest(SOURCE_ARCHIVE)
    )
    wheel_bound = bool(
        STAGED_WHEEL.is_file()
        and sbom_properties.get("methunmix:wheelSha256") == digest(STAGED_WHEEL)
    )
    sbom_completeness = next((
        item.get("value") for item in sbom_report.get("metadata", {}).get("properties", [])
        if isinstance(item, dict) and item.get("name") == "methunmix:sbomCompleteness"
    ), "MISSING")
    sbom_bound_to_staged = source_bound and wheel_bound
    sbom_ready = (
        sbom_report.get("bomFormat") == "CycloneDX"
        and sbom_report.get("specVersion") == "1.6"
        and sbom_bound_to_staged
        and sbom_completeness == "COMPLETE_RELEASE_SCOPE"
    )
    evidence["sbom_binding"] = {
        "status": "PASS" if sbom_bound_to_staged else "FAIL_OR_STALE",
        "source_sha256_matches": source_bound,
        "wheel_sha256_matches": wheel_bound,
        "completeness": sbom_completeness,
    }
    # Generated release archives are expected in dist_source/dist_conda_rc and
    # are not runtime payloads.  Do not treat their .tar.gz suffix as bundled
    # reference/input data.  The audit must nevertheless reject those files if
    # they are placed under src/package directories.
    generated_dirs = {"build", "*.egg-info"}
    generated_prefixes = ("dist_source", "dist_conda")
    forbidden_suffixes = {".sif", ".bam", ".cram", ".pat", ".bed"}
    oversized = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(ROOT).parts
        if relative_parts and (relative_parts[0] in generated_dirs or relative_parts[0].startswith(generated_prefixes) or any(part.endswith(".egg-info") for part in relative_parts)):
            continue
        if any(part in {"work", "reports", "references", "runtimes"} for part in relative_parts):
            reasons.append(f"development/runtime artifact is inside package workspace: {path.relative_to(ROOT)}")
        if path.stat().st_size > 256 * 1024 * 1024:
            oversized.append({"path": str(path.relative_to(ROOT)), "bytes": path.stat().st_size})
        if path.suffix.lower() in forbidden_suffixes:
            reasons.append(f"large/runtime input file is inside Conda workspace: {path.relative_to(ROOT)}")
    evidence["oversized_files"] = oversized
    evidence["tool_count"] = len(tools)
    evidence["source_version"] = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").is_file() else None
    recipe_report = read_json(RECIPE_AUDIT)
    payload_report = read_json(PAYLOAD_AUDIT)
    install_report = read_json(INSTALL_AUDIT)
    science_report = read_json(SCIENCE_AUDIT)
    gpu_report = read_json(GPU_AUDIT)
    rollback_report = read_json(ROLLBACK_AUDIT)
    matrix_report = read_json(MATRIX_AUDIT)
    reproducible_build_report = read_json(reproducible_build_path)
    reproducible_build_ready = reproducible_build_report.get("status") == "PASS" and (
        reproducible_build_report.get("source_archives", {}).get("reproducible") is True
        and reproducible_build_report.get("normalized_wheels", {}).get("reproducible") is True
        and reproducible_build_report.get("source_archives", {}).get("staged_matches_reproducible_build") is True
        and reproducible_build_report.get("normalized_wheels", {}).get("staged_matches_reproducible_build") is True
    )
    package_ready = (
        SOURCE_ARCHIVE.is_file()
        and recipe_report.get("status") == "PASS"
        and payload_report.get("status") == "PASS"
        and reproducible_build_ready
    )
    asset_catalog_implementation_ready = catalog_schema_ok and not invalid_public_targets
    baseline = read_json(BASELINE)
    baseline_ready = baseline.get("baseline_frozen") is True and baseline.get("tolerance_preapproved") is True
    name_ready = name_status == "OWNER_AND_LEGAL_APPROVED"
    license_approved = (
        bool(license_items)
        and not any(item.get("proposed_distribution_status") == "BLOCKED" for item in license_items)
        and read_json(ROOT / "evidence/license_approval_decisions.json").get("status") == "APPROVED"
    )
    import_regression = install_report.get("asset_manager_regression", {})
    safe_import_path_ready = (
        import_regression.get("status") == "PASS"
        and any("module import" in str(item).lower() for item in import_regression.get("checks", []))
    )
    asset_distribution_ready = (
        asset_catalog_implementation_ready
        and not invalid_public_targets
        and (bool(public_targets) or (empty_catalog_policy_valid(catalog) and safe_import_path_ready))
    )
    installation_e2e_ready = install_report.get("scope") == "conda_install" and install_report.get("status") == "PASS"
    scientific_compatibility_ready = science_report.get("status") == "PASS"
    gpu_claims_ready = (
        gpu_report.get("status") == "PASS"
        and gpu_report.get("scope") == "conda_gpu_runtime_e2e"
        and gpu_report.get("runtime_conda_e2e") == "PASS"
    )
    # Passing mocked local unit tests is necessary, but it must never be
    # mistaken for a real rollback/revocation drill against the release object
    # store.  Only an explicitly scoped production drill can satisfy this gate.
    rollback_ready = (
        rollback_report.get("status") == "PASS"
        and rollback_report.get("scope") == "production_object_store"
        and rollback_report.get("production_rollback_claim") is True
    )
    capability_matrix_ready = matrix_report.get("status") == "PASS"
    production_ready = read_json(PRODUCTION).get("status") == "PASS"
    if not sbom_ready:
        reasons.append("SBOM is missing, stale, or incomplete for the full release scope; the current core-only SBOM does not cover resolved Conda dependencies and external SIF/reference/model/cache assets")
    if not installation_e2e_ready:
        reasons.append(
            "clean wheel smoke is not a Conda installation test; recipe build and clean Linux x86_64 Conda validation remain pending external CI"
        )
    if not scientific_compatibility_ready:
        reasons.append(
            f"scientific compatibility gate is not PASS (status={science_report.get('status', 'MISSING')}); no READY/RU state promotion is inferred"
        )
    if not gpu_claims_ready:
        reasons.append(
            f"GPU Conda runtime E2E is not PASS (static audit status={gpu_report.get('status', 'MISSING')}, runtime={gpu_report.get('runtime_conda_e2e', 'NOT_RECORDED')}); only runtime-tested device claims may ship"
        )
    if not reproducible_build_ready:
        reasons.append("deterministic source and normalized wheel double-build evidence is missing or failed")
    if not rollback_ready:
        reasons.append(
            "production object-store rollback/revocation drill is not complete; local mocked tests do not satisfy this release gate"
        )
    public_release_ready = all((
        name_ready, baseline_ready, package_ready, asset_distribution_ready,
        installation_e2e_ready, scientific_compatibility_ready, gpu_claims_ready,
        rollback_ready, production_ready, license_approved, capability_matrix_ready, sbom_ready,
    ))
    payload = {
        "schema": "methunmix-release-audit-v1",
        "product": "MethUnmix",
        "status": "PUBLIC_RELEASE_READY" if public_release_ready and not reasons else "METHUNMIX_CONDA_RC_BLOCKED",
        "gates": {
            "NAME_AND_IDENTITY_READY": name_ready,
            "BASELINE_READY": baseline_ready,
            "PACKAGE_READY": package_ready,
            "CAPABILITY_MATRIX_STATIC_READY": capability_matrix_ready,
            "ASSET_CATALOG_IMPLEMENTATION_READY": asset_catalog_implementation_ready,
            "ASSET_DISTRIBUTION_READY": asset_distribution_ready,
            "LICENSE_APPROVED": license_approved,
            "SBOM_READY": sbom_ready,
            "INSTALLATION_E2E_READY": installation_e2e_ready,
            "SCIENTIFIC_COMPATIBILITY_READY": scientific_compatibility_ready,
            "GPU_CLAIMS_MATCH_EVIDENCE": gpu_claims_ready,
            "ROLLBACK_DRILL_PASSED": rollback_ready,
            "PRODUCTION_ARTIFACT_VERIFIED": production_ready,
            "PUBLIC_RELEASE_READY": public_release_ready,
        },
        "asset_distribution_mode": (
            "PUBLIC_DOWNLOADABLE_TARGETS"
            if public_targets
            else "EMPTY_PUBLIC_CATALOG_WITH_USER_SUPPLIED_IMPORT_FALLBACK"
        ),
        "reasons": reasons,
        "evidence": evidence,
        "rollback_evidence": {
            "status": rollback_report.get("status", "MISSING"),
            "scope": rollback_report.get("scope"),
            "production_rollback_claim": rollback_report.get("production_rollback_claim", False),
            "external_object_store_rollback_revocation_drill": rollback_report.get(
                "external_object_store_rollback_revocation_drill", "NOT_RECORDED"
            ),
        },
        "gpu_evidence": {
            "status": gpu_report.get("status", "MISSING"),
            "scope": gpu_report.get("scope"),
            "runtime_conda_e2e": gpu_report.get("runtime_conda_e2e", "NOT_RECORDED"),
            "summary": gpu_report.get("summary", {}),
            "current_host_gpu_probe": gpu_report.get("current_host_gpu_probe", {}),
        },
        "reproducible_build_evidence": {
            "status": reproducible_build_report.get("status", "MISSING"),
            "source_archives_reproducible": reproducible_build_report.get("source_archives", {}).get("reproducible"),
            "normalized_wheels_reproducible": reproducible_build_report.get("normalized_wheels", {}).get("reproducible"),
            "raw_wheels_reproducible": reproducible_build_report.get("raw_wheels", {}).get("reproducible"),
        },
        "read_only": True,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload["gates"]["PUBLIC_RELEASE_READY"] and not reasons else 2


if __name__ == "__main__":
    raise SystemExit(main())
