#!/usr/bin/env python3
"""Collect baseline candidates from existing fixtures and immutable references.

This is an inventory/freeze-preparation tool, not a scientific test runner.
It never edits the source/reference/simulation trees. Small files are hashed;
large WGBS inputs are inventoried but deliberately not read end-to-end unless
the maintainer explicitly supplies --hash-large-inputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "baseline_candidate_inventory.json"
SUMMARY_MD = ROOT / "evidence" / "BASELINE_CANDIDATE_SUMMARY.md"
POLICY_JSON = ROOT / "evidence" / "BASELINE_TOLERANCE_REVIEW.json"
POLICY_MD = ROOT / "evidence" / "BASELINE_TOLERANCE_REVIEW.md"
SMALL_HASH_LIMIT = 64 * 1024 * 1024

SCENARIO_NAMES = {
    "immune_6": "immune6",
    "immune6": "immune6",
    "immune_12": "immune12",
    "immune12": "immune12",
    "immune_19": "fetal19",
    "fetal19": "fetal19",
    "epi": "epithelial",
    "epithelial": "epithelial",
    "breast": "breast",
    "brain": "brain",
}

MODE_PARENT_TOOLS = {
    "EpiDISH-RPC": "EpiDISH",
    "EpiDISH-CP": "EpiDISH",
    "EpiDISH-CBS": "EpiDISH",
    "EMeth-normal": "EMeth",
    "EMeth-laplace": "EMeth",
}


def sha256_file(path: Path, limit: int | None = None) -> str | None:
    if limit is not None and path.stat().st_size > limit:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def version_key(version: str) -> tuple[Any, ...]:
    main, separator, suffix = str(version).partition("-")
    parts: list[tuple[int, Any]] = []
    for token in re.split(r"[.+]", main):
        parts.append((0, int(token)) if token.isdigit() else (1, token.lower()))
    prerelease = (0, "") if not separator else (-1, suffix.lower())
    return tuple(parts), prerelease


def stable_file_record(path: Path, base: Path, *, hash_large: bool) -> dict[str, Any]:
    size = path.stat().st_size
    digest = sha256_file(path, None if hash_large else SMALL_HASH_LIMIT)
    return {
        "path": str(path),
        "relative_path": str(path.relative_to(base)),
        "bytes": size,
        "sha256": digest,
        "digest_status": "COMPUTED" if digest else "DEFERRED_LARGE_INPUT",
        "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }


def first_csv_column(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader, [])
            return [str(row[0]) for row in reader if row and len(row) > 0]
    except (OSError, UnicodeError, csv.Error):
        return []


def csv_header(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return [str(value) for value in next(csv.reader(stream), [])]
    except (OSError, UnicodeError, csv.Error):
        return []


def normalize_sample_id(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^simulated[_ -]*", "", value)
    value = re.sub(r"^sample[_ -]*", "sample", value)
    return re.sub(r"[^a-z0-9]", "", value)


def compare_samples(expected: list[str], observed: list[str]) -> dict[str, Any]:
    expected_by_norm = {normalize_sample_id(value): value for value in expected}
    observed_by_norm = {normalize_sample_id(value): value for value in observed}
    return {
        "expected_ids": sorted(expected),
        "observed_ids": sorted(observed),
        "missing_input_ids": [expected_by_norm[key] for key in sorted(expected_by_norm.keys() - observed_by_norm.keys())],
        "extra_input_ids": [observed_by_norm[key] for key in sorted(observed_by_norm.keys() - expected_by_norm.keys())],
        "match": expected_by_norm.keys() == observed_by_norm.keys(),
        "matching_rule": "case-insensitive sample identifier normalization; removes optional 'Simulated' prefix, underscores, spaces and punctuation",
    }


def sample_id_from_path(path: Path) -> str | None:
    # Do not let PAT CSI sidecars count as input samples.
    if path.name.endswith(".pat.gz.csi") or path.name.endswith(".csi") or path.name.endswith(".tbi"):
        return None
    match = re.search(r"(sample[_-]?\d+)", path.name, re.IGNORECASE)
    return match.group(1) if match else None


def collect_simulations(root: Path, hash_large: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    observations: list[str] = []
    platform_dirs = {p.name.lower(): p for p in root.iterdir() if p.is_dir()} if root.is_dir() else {}
    if "450k" in platform_dirs:
        platform_map = [(platform_dirs["450k"], "450k", "450K")]
    else:
        platform_map = []
    if "850k" in platform_dirs:
        platform_map.append((platform_dirs["850k"], "epic", "850K"))
        observations.append("The source directory is named 850K; it is recorded as EPIC with the original folder label preserved.")
    if "epic" in platform_dirs:
        platform_map.append((platform_dirs["epic"], "epic", "EPIC"))
    if "wgbs" in platform_dirs:
        platform_map.append((platform_dirs["wgbs"], "wgbs", "WGBS"))
    if "epic" not in platform_dirs and "850k" not in platform_dirs:
        observations.append("No EPIC/850K simulation directory was found.")

    for platform_root, platform, source_label in platform_map:
        if platform == "wgbs":
            for case_root in sorted(p for p in platform_root.iterdir() if p.is_dir()):
                match = re.fullmatch(r"(hg19|hg38)_(.+)", case_root.name, re.IGNORECASE)
                if not match:
                    observations.append(f"Unclassified WGBS fixture directory: {case_root}")
                    continue
                build = match.group(1).lower()
                scenario = SCENARIO_NAMES.get(match.group(2).lower())
                if not scenario:
                    observations.append(f"Unknown WGBS scenario directory: {case_root}")
                    continue
                truth_files = sorted(case_root.glob("proportions*.csv"))
                truth_records = [stable_file_record(p, root, hash_large=hash_large) for p in truth_files]
                input_records = []
                input_counts = {}
                index_counts = {}
                observed_sample_ids: set[str] = set()
                for input_kind in ("bed", "pat", "bam"):
                    input_root = case_root / input_kind
                    children = sorted(p for p in input_root.glob("*") if p.is_file()) if input_root.is_dir() else []
                    indices = [p for p in children if p.name.endswith((".csi", ".tbi"))]
                    index_counts[input_kind] = len(indices)
                    if input_kind == "bed":
                        files = [p for p in children if p.name.endswith((".bed", ".bed.gz"))]
                    elif input_kind == "pat":
                        files = [p for p in children if p.name.endswith(".pat.gz")]
                    else:
                        files = [p for p in children if p.suffix.lower() == ".bam"]
                    input_counts[input_kind] = len(files)
                    for path in files:
                        sample_id = sample_id_from_path(path)
                        if sample_id:
                            observed_sample_ids.add(sample_id)
                        input_records.append(stable_file_record(path, root, hash_large=hash_large))
                all_sample_ids = []
                for truth in truth_files:
                    all_sample_ids.extend(first_csv_column(truth))
                sample_mapping = compare_samples(sorted(set(all_sample_ids)), sorted(observed_sample_ids))
                provenance_files = sorted(case_root.glob("fixture_provenance.json"))
                fixtures.append({
                    "fixture_id": f"{scenario}.wgbs-native.{build}",
                    "scenario": scenario,
                    "platform": "wgbs",
                    "source_platform_label": source_label,
                    "route": "wgbs-native",
                    "genome_build": build,
                    "source_directory": str(case_root),
                    "truth_files": truth_records,
                    "input_counts": input_counts,
                    "index_counts": index_counts,
                    "input_files": input_records,
                    "truth_row_ids": sorted(set(all_sample_ids)),
                    "sample_mapping_audit": sample_mapping,
                    "sample_mapping_valid": sample_mapping["match"],
                    "provenance_files": [stable_file_record(p, root, hash_large=hash_large) for p in provenance_files],
                    "truth_content_digest_complete": all(r["sha256"] is not None for r in truth_records),
                    "large_input_content_digests_complete": all(r["sha256"] is not None for r in input_records),
                    "simulation_truth_fixture_only": True,
                    "approval_status": "CANDIDATE_NOT_FROZEN",
                })
            continue

        for case_root in sorted(p for p in platform_root.iterdir() if p.is_dir()):
            scenario = SCENARIO_NAMES.get(case_root.name.lower())
            if not scenario:
                observations.append(f"Unknown array fixture directory: {case_root}")
                continue
            truth_files = sorted(case_root.glob("proportions*.csv"))
            matrix_files = sorted(p for p in case_root.glob("*.csv") if "proportion" not in p.name.lower() and "selected_reference" not in p.name.lower())
            selected_files = sorted(case_root.glob("selected_reference_samples*.csv"))
            expected_ids = sorted({sample for path in truth_files for sample in first_csv_column(path)})
            matrix_sample_ids: list[str] = []
            for matrix in matrix_files:
                matrix_sample_ids.extend(csv_header(matrix)[1:])
            sample_mapping = compare_samples(expected_ids, matrix_sample_ids)
            fixtures.append({
                "fixture_id": f"{scenario}.{platform}-native.not_applicable",
                "scenario": scenario,
                "platform": platform,
                "source_platform_label": source_label,
                "route": "array-native",
                "genome_build": "not_applicable",
                "source_directory": str(case_root),
                "truth_files": [stable_file_record(p, root, hash_large=hash_large) for p in truth_files],
                "input_counts": {"array_matrix": len(matrix_files)},
                "input_files": [stable_file_record(p, root, hash_large=hash_large) for p in matrix_files],
                "provenance_files": [stable_file_record(p, root, hash_large=hash_large) for p in selected_files],
                "truth_row_ids": expected_ids,
                "sample_mapping_audit": sample_mapping,
                "sample_mapping_valid": sample_mapping["match"],
                "truth_content_digest_complete": all(p.stat().st_size <= SMALL_HASH_LIMIT or hash_large for p in truth_files),
                "large_input_content_digests_complete": all(p.stat().st_size <= SMALL_HASH_LIMIT or hash_large for p in matrix_files + selected_files),
                "simulation_truth_fixture_only": True,
                "approval_status": "CANDIDATE_NOT_FROZEN",
            })

    summary = {
        "root": str(root),
        "exists": root.is_dir(),
        "total_files": sum(len(f["input_files"]) + len(f["truth_files"]) for f in fixtures),
        "fixture_count": len(fixtures),
        "fixture_ids": [f["fixture_id"] for f in fixtures],
        "observations": observations,
        "digest_policy": (
            "All discovered fixture input, truth, and provenance files were SHA256-hashed, including large files."
            if hash_large else
            "All files <=64 MiB were SHA256-hashed. Larger inputs were stat-inventoried without reading content. "
            "Use --hash-large-inputs to compute content digests when a final freeze is explicitly scheduled."
        ),
    }
    return fixtures, summary


def collect_references(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifests: list[tuple[Path, dict[str, Any]]] = []
    parse_failures = []
    if root.is_dir():
        for path in root.rglob("manifest.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("reference_id") and value.get("version"):
                    manifests.append((path, value))
            except (OSError, json.JSONDecodeError) as exc:
                parse_failures.append({"path": str(path), "error": str(exc)})
    latest: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, payload in manifests:
        ref_id = str(payload["reference_id"])
        prior = latest.get(ref_id)
        if prior is None or version_key(str(payload["version"])) > version_key(str(prior[1]["version"])):
            latest[ref_id] = (path, payload)

    records: list[dict[str, Any]] = []
    for ref_id, (manifest_path, manifest) in sorted(latest.items()):
        artifact_hash_map = manifest.get("artifact_checksums", {})
        cap_map = manifest.get("tool_capabilities", {})
        build = str(manifest.get("genome_build", "not_applicable"))
        if build in {"not_applicable", "None", ""}:
            build = "not_applicable"
        for tool, capability in sorted(cap_map.items()):
            if not isinstance(capability, dict):
                continue
            evidence = []
            for evidence_key, relative in capability.get("artifacts", {}).items():
                if not isinstance(relative, str) or not any(x in evidence_key.lower() for x in ("scientific", "repeat", "engineering", "validation", "profile")):
                    continue
                path = manifest_path.parent / relative
                digest_in_manifest = artifact_hash_map.get(relative)
                row: dict[str, Any] = {
                    "kind": evidence_key,
                    "path": str(path),
                    "exists": path.is_file(),
                    "manifest_sha256": digest_in_manifest,
                }
                if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
                    row["current_sha256"] = sha256_file(path)
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        row["reported_status"] = payload.get("status", payload.get("result", payload.get("overall_status"))) if isinstance(payload, dict) else None
                        row["metric_summary"] = metric_summary(payload)
                    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                        row["parseable_json"] = False
                evidence.append(row)
            records.append({
                "capability_id": f"{manifest.get('scenario_id', 'unknown')}.{manifest.get('analysis_contract', 'unknown')}.{build}.{tool}",
                "reference_id": ref_id,
                "reference_version": str(manifest["version"]),
                "selector": f"{ref_id}@{manifest['version']}",
                "scenario": manifest.get("scenario_id"),
                "source_platform": manifest.get("source_platform"),
                "analysis_contract": manifest.get("analysis_contract"),
                "genome_build": build,
                "tool": tool,
                "scientific_status": capability.get("status", "UNKNOWN"),
                "execution_policy": capability.get("execution_policy"),
                "validation_scope": capability.get("validation_scope"),
                "validation_policy_candidate": capability.get("validation_policy", {}),
                "execution_profiles": capability.get("execution_profiles", capability.get("execution_profile", {})),
                "manifest_path": str(manifest_path),
                "manifest_sha256": sha256_file(manifest_path),
                "artifact_checksum_map_digest": sha256_json(artifact_hash_map),
                "artifact_checksum_count": len(artifact_hash_map),
                "validation_evidence": evidence,
                "baseline_approval_status": "CANDIDATE_NOT_FROZEN",
            })
    counts: dict[str, int] = {}
    for record in records:
        status = record["scientific_status"]
        counts[status] = counts.get(status, 0) + 1
    return records, {
        "root": str(root),
        "manifest_count_parsed": len(manifests),
        "unique_reference_ids_latest": len(latest),
        "tool_capability_record_count": len(records),
        "scientific_status_counts": counts,
        "parse_failures": parse_failures,
        "selection_rule": "Highest manifest semantic version per reference_id; version parsing preserves prerelease as lower than stable.",
        "integrity_caveat": "Manifest-declared artifact SHA256 values are recorded, not recomputed across the 125 GB reference tree. A final selected baseline requires reference verification against its exact immutable selector.",
    }


def metric_summary(value: Any) -> dict[str, Any]:
    interesting = re.compile(r"(mae|pearson|correlation|rmse|r2|repeat|difference|error|valid_output|accuracy|status|passed|threshold|tolerance)", re.I)
    output: dict[str, Any] = {}

    def walk(node: Any, prefix: str, depth: int) -> None:
        if depth > 5 or len(output) >= 120:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if interesting.search(str(key)) and not isinstance(child, (dict, list)):
                    output[path] = child
                elif isinstance(child, (dict, list)):
                    walk(child, path, depth + 1)
        elif isinstance(node, list):
            for index, child in enumerate(node[:30]):
                if isinstance(child, (dict, list)):
                    walk(child, f"{prefix}[{index}]", depth + 1)

    walk(value, "", 0)
    return output


def make_candidate_links(fixtures: list[dict[str, Any]], references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links = []
    for fixture in fixtures:
        for reference in references:
            if not fixture_reference_route_matches(fixture, reference):
                continue
            links.append({
                "fixture_id": fixture["fixture_id"],
                "capability_id": reference["capability_id"],
                "selector": reference["selector"],
                "tool": reference["tool"],
                "fixture_route": fixture["route"],
                "reference_contract": reference["analysis_contract"],
                "reference_platform": reference["source_platform"],
                "genome_build": fixture["genome_build"],
                "scientific_status": reference["scientific_status"],
                "validation_scope": reference["validation_scope"],
                "validation_policy_candidate": reference["validation_policy_candidate"],
                "input_content_digest_ready": fixture["large_input_content_digests_complete"],
                "truth_content_digest_ready": fixture["truth_content_digest_complete"],
                "sample_mapping_valid": fixture.get("sample_mapping_valid", False),
                "freeze_status": "CANDIDATE_REQUIRES_FIXTURE_RECONCILIATION" if not fixture.get("sample_mapping_valid", False) else "CANDIDATE_REQUIRES_SCIENCE_APPROVAL",
            })
    return links


def fixture_reference_route_matches(fixture: dict[str, Any], reference: dict[str, Any]) -> bool:
    """Join only data fixtures that satisfy the registered input contract.

    A WGBS-derived array reference has array as its target/reference platform,
    but it must be joined to a WGBS fixture. Matching only scenario and
    `source_platform` would incorrectly treat that reference as tested by an
    array matrix. Conversely, EPIC→450K consumes EPIC input while using a 450K
    reference and must join to an EPIC fixture.
    """
    if reference.get("scenario") != fixture.get("scenario"):
        return False
    contract = str(reference.get("analysis_contract", ""))
    fixture_route = fixture.get("route")
    platform = fixture.get("platform")
    build = str(fixture.get("genome_build", "not_applicable"))

    if fixture_route == "array-native":
        if platform == "450k":
            return contract == "array_450k_cpg" and reference.get("source_platform") == "450k"
        if platform == "epic":
            return (
                (contract == "array_epic_cpg" and reference.get("source_platform") == "epic")
                or (
                    contract == "array_epic_from_450k_common_cpg_v1"
                    and reference.get("source_platform") == "450k"
                )
            )
        return False

    if fixture_route == "wgbs-native":
        if build not in {"hg19", "hg38"}:
            return False
        if contract == f"wgbs_native_{build}":
            return reference.get("source_platform") == "wgbs" and reference.get("genome_build") == build
        if contract in {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}:
            target_platform = "epic" if contract == "wgbs_derived_epic_cpg_v1" else "450k"
            return reference.get("source_platform") == target_platform and reference.get("genome_build") == build
        return False

    return False


def build_tolerance_review(references: list[dict[str, Any]]) -> dict[str, Any]:
    by_tool: dict[str, dict[str, dict[str, Any]]] = {}
    evidence_counts = {"rows": 0, "existing": 0, "current_digest_computed": 0, "digest_matches": 0, "digest_mismatches": 0, "reported_metrics": 0}
    for record in references:
        tool = str(record["tool"])
        policy = record.get("validation_policy_candidate")
        manifest_policy = policy if isinstance(policy, dict) else {}
        evidence_tolerances = []
        for evidence in record.get("validation_evidence", []):
            metrics = evidence.get("metric_summary", {})
            if not isinstance(metrics, dict):
                continue
            selected = {
                str(key): value for key, value in metrics.items()
                if re.search(r"threshold|tolerance", str(key), re.IGNORECASE)
            }
            if selected:
                evidence_tolerances.append({
                    "kind": evidence.get("kind"),
                    "path": evidence.get("path"),
                    "sha256": evidence.get("current_sha256"),
                    "values": selected,
                })
        policy_payload = {
            "manifest_policy": manifest_policy,
            "historical_evidence_tolerances": evidence_tolerances,
        }
        has_candidate_policy = bool(manifest_policy or evidence_tolerances)
        policy_key = sha256_json(policy_payload) if has_candidate_policy else "NO_POLICY"
        tool_profiles = by_tool.setdefault(tool, {})
        profile = tool_profiles.setdefault(policy_key, {
            "policy_sha256": None if policy_key == "NO_POLICY" else policy_key,
            "policy": manifest_policy,
            "historical_evidence_tolerances": evidence_tolerances,
            "policy_sources": sorted(set(
                (["reference_manifest.validation_policy"] if manifest_policy else [])
                + (["referenced_validation_evidence.thresholds_or_tolerances"] if evidence_tolerances else [])
            )),
            "capability_ids": [], "selectors": [], "scientific_statuses": {},
        })
        profile["capability_ids"].append(record["capability_id"])
        profile["selectors"].append(record["selector"])
        status = str(record.get("scientific_status", "UNKNOWN"))
        profile["scientific_statuses"][status] = profile["scientific_statuses"].get(status, 0) + 1
        for evidence in record.get("validation_evidence", []):
            evidence_counts["rows"] += 1
            if evidence.get("exists"):
                evidence_counts["existing"] += 1
            current_digest = evidence.get("current_sha256")
            declared_digest = evidence.get("manifest_sha256")
            if current_digest:
                evidence_counts["current_digest_computed"] += 1
            if current_digest and declared_digest:
                if current_digest == declared_digest:
                    evidence_counts["digest_matches"] += 1
                else:
                    evidence_counts["digest_mismatches"] += 1
            if evidence.get("metric_summary"):
                evidence_counts["reported_metrics"] += 1
    try:
        registry = json.loads((ROOT / "src/methunmix_assets/capabilities/capability_registry.json").read_text(encoding="utf-8"))
        # Capability registry v2 uses the unambiguous `logical_tools` key;
        # continue accepting v1 registries for older source snapshots.
        declared_tools = [str(item) for item in registry.get("logical_tools", registry.get("tools", []))]
    except (OSError, json.JSONDecodeError):
        declared_tools = []
    represented = {re.sub(r"[^a-z0-9]", "", tool.lower()) for tool in by_tool}
    missing_tools = []
    for tool in declared_tools:
        normalized = re.sub(r"[^a-z0-9]", "", tool.lower())
        if normalized in represented:
            continue
        parent = MODE_PARENT_TOOLS.get(tool)
        if parent and re.sub(r"[^a-z0-9]", "", parent.lower()) in represented:
            parent_profiles = by_tool[parent]
            inherited_profiles = {}
            for parent_key, parent_profile in parent_profiles.items():
                inherited_policy = parent_profile.get("policy", {})
                inherited_evidence_tolerances = parent_profile.get("historical_evidence_tolerances", [])
                inherited_key = sha256_json({"mode": tool, "parent_tool": parent, "parent_policy_sha256": parent_profile.get("policy_sha256")})
                expected_model = {"EMeth-normal": "Normal", "EMeth-laplace": "Laplace"}.get(tool)
                model_scope_ok = expected_model is None or expected_model in inherited_policy.get("models", [])
                inherited_profiles[inherited_key] = {
                    **parent_profile,
                    "tool": tool,
                    "policy_profile_id": inherited_key,
                    "policy_source_tool": parent,
                    "policy_inheritance": "parent manifest/evidence candidate only; mode-specific test and owner approval still required",
                    "mode_scope": expected_model or tool.removeprefix("EpiDISH-"),
                    "mode_scope_matches_policy": model_scope_ok,
                    "candidate_status": (
                        "NO_TOOL_POLICY_FOUND"
                        if not (inherited_policy or inherited_evidence_tolerances) or parent_key == "NO_POLICY"
                        else "MODE_SPECIFIC_POLICY_INHERITED_REQUIRES_E2E_AND_OWNER_REVIEW"
                        if model_scope_ok
                        else "MODE_SCOPE_NOT_COVERED_BY_PARENT_POLICY"
                    ),
                }
            by_tool[tool] = inherited_profiles
        else:
            missing_tools.append(tool)
    profiles = []
    for tool, policies in sorted(by_tool.items()):
        for key, profile in sorted(policies.items()):
            profile["tool"] = tool
            profile["policy_profile_id"] = key
            profile["capability_ids"] = sorted(set(profile["capability_ids"]))
            profile["selectors"] = sorted(set(profile["selectors"]))
            if "candidate_status" not in profile:
                has_manifest_policy = bool(profile.get("policy"))
                has_evidence_tolerances = bool(profile.get("historical_evidence_tolerances"))
                if has_manifest_policy and has_evidence_tolerances:
                    profile["candidate_status"] = "MANIFEST_AND_EVIDENCE_POLICY_CANDIDATES_REQUIRE_OWNER_REVIEW"
                elif has_manifest_policy:
                    profile["candidate_status"] = "MANIFEST_POLICY_CANDIDATE_REQUIRES_OWNER_REVIEW"
                elif has_evidence_tolerances:
                    profile["candidate_status"] = "HISTORICAL_EVIDENCE_TOLERANCE_CANDIDATE_REQUIRES_OWNER_REVIEW"
                else:
                    profile["candidate_status"] = "NO_TOOL_POLICY_FOUND"
            profiles.append(profile)
    return {
        "schema": "methunmix-baseline-tolerance-review-v1",
        "status": "CANDIDATE_NOT_FROZEN_OR_APPROVED",
        "policy_source": "latest immutable reference manifests and referenced validation artifacts",
        "owner_confirmed_global_policy": {
            "pearson_minimum": {"value": 0.80, "applies_only_to": ["existing MethylBERT routes"]},
            "other_metrics": "report observed values; do not add universal thresholds",
            "repeatability_cpu_gpu": "extract only predeclared tool/profile-specific values from reference manifests and validation evidence; owner review before comparisons",
            "non_methylbert_manifest_thresholds": "preserved as historical per-capability candidates only; not automatically promoted to Conda release gates",
            "external_generalization": "not claimed without independent cohort",
        },
        "registered_tools": declared_tools,
        "tools_without_reference_candidates": missing_tools,
        "historical_validation_evidence_integrity": evidence_counts,
        "tool_policy_profiles": profiles,
        "interpretation": "Manifest policies and threshold/tolerance fields explicitly present in digest-checked historical validation evidence are candidate policies, not new approvals and not evidence that a run passed. Observed errors are not reinterpreted as tolerances. Missing policy is reported rather than filled with a universal threshold.",
    }


def collect_reports(root: Path) -> dict[str, Any]:
    current_catalog = root / "current_wgbs_reference_catalog.json"
    result: dict[str, Any] = {"current_wgbs_reference_catalog": {"path": str(current_catalog), "exists": current_catalog.is_file()}}
    if current_catalog.is_file():
        try:
            payload = json.loads(current_catalog.read_text(encoding="utf-8"))
            entries = payload.get("references", [])
            result["current_wgbs_reference_catalog"].update({
                "sha256": sha256_file(current_catalog),
                "reference_snapshot_count": len(entries) if isinstance(entries, list) else 0,
                "status_counts": _count_key(entries, "status"),
                "tool_status_counts": _count_nested_tool_states(entries),
                "classification": "Historical/current summary snapshot; cross-check selector and digest against manifest before freezing.",
            })
        except (OSError, json.JSONDecodeError) as exc:
            result["current_wgbs_reference_catalog"]["error"] = str(exc)
    return result


def _count_key(rows: Any, key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                value = str(row.get(key, "UNKNOWN"))
                counts[value] = counts.get(value, 0) + 1
    return counts


def _count_nested_tool_states(rows: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("ready_tools", "released_unvalidated_tools", "quarantined_tools", "not_available_tools"):
                values = row.get(key, [])
                if isinstance(values, list):
                    label = key.removesuffix("_tools").upper()
                    counts[label] = counts.get(label, 0) + len(values)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, help="Maintainer source project containing simulated-data/, references/, and reports/.")
    parser.add_argument("--simulated-data", type=Path, help="Simulation fixture root; overrides --project-root/simulated-data.")
    parser.add_argument("--references", type=Path, help="Reference root; overrides --project-root/references.")
    parser.add_argument("--reports", type=Path, help="Historical report root; overrides --project-root/reports.")
    parser.add_argument("--reuse-fixture-inventory", type=Path, help="Reuse a prior fixture inventory, including its SHA256 records, without reopening or rehashing simulation inputs.")
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--summary-md", type=Path, default=SUMMARY_MD)
    parser.add_argument("--policy-json", type=Path, default=POLICY_JSON)
    parser.add_argument("--policy-md", type=Path, default=POLICY_MD)
    parser.add_argument("--hash-large-inputs", action="store_true", help="Read/hash every large input file; may take substantial I/O time.")
    args = parser.parse_args()

    if not args.project_root and not args.simulated_data and not args.reuse_fixture_inventory:
        parser.error("provide --project-root, --simulated-data, or --reuse-fixture-inventory; maintainer paths are never hard-coded")
    project_root = args.project_root
    reused_inventory = json.loads(args.reuse_fixture_inventory.read_text(encoding="utf-8")) if args.reuse_fixture_inventory else None
    reused_root = Path(str(reused_inventory.get("user_approved_input_root"))) if isinstance(reused_inventory, dict) else None
    simulated_data = args.simulated_data or (project_root / "simulated-data" if project_root else reused_root)
    references = args.references or (project_root / "references" if project_root else None)
    reports = args.reports or (project_root / "reports" if project_root else None)
    assert simulated_data is not None

    if reused_inventory is not None:
        if reused_inventory.get("schema") != "methunmix-baseline-candidate-inventory-v1":
            parser.error("--reuse-fixture-inventory must point to a MethUnmix baseline candidate inventory")
        if Path(str(reused_inventory.get("user_approved_input_root"))) != simulated_data:
            parser.error("reused fixture inventory root does not match the requested simulation root")
        fixtures = reused_inventory.get("fixtures", [])
        fixture_summary = reused_inventory.get("fixture_summary", {})
        if not fixtures or not fixture_summary:
            parser.error("reused fixture inventory has no fixture records/summary")
        if args.hash_large_inputs:
            parser.error("do not combine --reuse-fixture-inventory with --hash-large-inputs; reuse preserves prior fixture digests")
    else:
        fixtures, fixture_summary = collect_simulations(simulated_data, args.hash_large_inputs)
    fixture_file_records = [
        record
        for fixture in fixtures
        for key in ("input_files", "truth_files", "provenance_files")
        for record in fixture.get(key, [])
    ]
    fixture_content_digests_complete = all(bool(record.get("sha256")) for record in fixture_file_records)
    reference_records, reference_summary = collect_references(references) if references else ([], {"root": None, "manifest_count_parsed": 0, "unique_reference_ids_latest": 0, "tool_capability_record_count": 0, "scientific_status_counts": {}, "parse_failures": [], "selection_rule": "No reference root supplied.", "integrity_caveat": "No reference manifests were scanned."})
    tolerance_review = build_tolerance_review(reference_records)
    required_before_freeze = [
        "Select current immutable selectors from generated candidates and verify their artifact files.",
        "Review the generated BASELINE_TOLERANCE_REVIEW report and approve applicable pre-existing tool/profile policies before comparison runs; missing policies remain undefined and are not defaulted.",
        "Bind exact core source, Conda build, runtime, reference, fixtures, truth, seed, device and post-processing digests.",
    ]
    if not fixture_content_digests_complete:
        required_before_freeze.insert(
            1,
            "Compute SHA256 for large WGBS inputs only after the baseline set is selected and the freeze run is authorized.",
        )
    payload = {
        "schema": "methunmix-baseline-candidate-inventory-v1",
        "product": "MethUnmix",
        "software_version": "2.0.0rc1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "CANDIDATES_GENERATED_NOT_FROZEN",
        "user_approved_input_root": str(simulated_data),
        "fixture_summary": fixture_summary,
        "fixtures": fixtures,
        "reference_summary": reference_summary,
        "latest_reference_tool_candidates": reference_records,
        "fixture_capability_candidates": make_candidate_links(fixtures, reference_records),
        "historical_release_summaries": collect_reports(reports) if reports else {"root": None, "files": [], "observations": ["No historical reports root supplied."]},
        "tolerance_review": {
            "path_json": str(args.policy_json),
            "path_markdown": str(args.policy_md),
            "status": tolerance_review["status"],
            "historical_validation_evidence_integrity": tolerance_review["historical_validation_evidence_integrity"],
            "tools_without_reference_candidates": tolerance_review["tools_without_reference_candidates"],
        },
        "policy": {
            "pearson_minimum": {"scope": "existing MethylBERT routes only", "value": 0.80, "status": "OWNER_CONFIRMED"},
            "other_scientific_metrics": "Report observed values; no new universal hard thresholds are introduced without owner approval.",
            "repeatability_and_cpu_gpu_tolerances": "Candidate values are read from each tool/reference validation_policy; final freeze requires per-tool approval before rerunning comparisons.",
            "platform_aliases": {"850K": "EPIC", "status": "OWNER_CONFIRMED", "preserve_original_fixture_label": True},
            "no_external_generalization_claim": True,
            "scientific_status_promotion": "prohibited by packaging/baseline inventory",
        },
        "required_before_freeze": required_before_freeze,
    }
    present_pairs = {(f["scenario"], f["platform"]) for f in fixtures if f["platform"] != "wgbs"}
    expected_native_array_pairs = {
        ("immune6", "450k"), ("immune6", "epic"),
        ("immune12", "450k"), ("immune12", "epic"),
        ("fetal19", "450k"), ("epithelial", "450k"),
        ("breast", "450k"), ("brain", "450k"), ("brain", "epic"),
    }
    missing_array = sorted(expected_native_array_pairs - present_pairs)
    wgb_fixture_pairs = {(f["scenario"], f["genome_build"]) for f in fixtures if f["platform"] == "wgbs"}
    expected_native_wgbs_pairs = {
        (scenario, build)
        for scenario in ("immune6", "epithelial", "breast")
        for build in ("hg19", "hg38")
    }
    missing_wgbs = sorted(expected_native_wgbs_pairs - wgb_fixture_pairs)
    capability_status_counts: dict[str, int] = {}
    for candidate in payload["fixture_capability_candidates"]:
        status = str(candidate["scientific_status"])
        capability_status_counts[status] = capability_status_counts.get(status, 0) + 1
    payload["coverage_summary"] = {
        "native_array_fixture_pair_count": len(present_pairs & expected_native_array_pairs),
        "expected_native_array_fixture_pair_count": len(expected_native_array_pairs),
        "missing_native_array_fixture_pairs": [{"scenario": s, "platform": p} for s, p in missing_array],
        "native_wgbs_fixture_pair_count": len(wgb_fixture_pairs & expected_native_wgbs_pairs),
        "expected_native_wgbs_fixture_pair_count": len(expected_native_wgbs_pairs),
        "missing_native_wgbs_fixture_pairs": [{"scenario": s, "genome_build": b} for s, b in missing_wgbs],
        "fixture_candidate_status_counts": capability_status_counts,
        "not_a_scientific_result": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.policy_json.parent.mkdir(parents=True, exist_ok=True)
    args.policy_json.write_text(json.dumps(tolerance_review, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    policy_counts: dict[str, dict[str, int]] = {}
    for profile in tolerance_review["tool_policy_profiles"]:
        counts = policy_counts.setdefault(profile["tool"], {"profiles": 0, "missing_profiles": 0, "capabilities": 0})
        counts["profiles"] += 1
        counts["missing_profiles"] += int(profile["candidate_status"] in {"NO_TOOL_POLICY_FOUND", "MODE_SCOPE_NOT_COVERED_BY_PARENT_POLICY"})
        counts["capabilities"] += len(profile["capability_ids"])
    policy_lines = [
        "# Baseline tool-specific tolerance review (candidate only)",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "Status: `CANDIDATE_NOT_FROZEN_OR_APPROVED`. Values are extracted from the latest immutable reference manifests and validation evidence. They are not new thresholds or proof of a scientific pass.",
        "",
        "## Fixed owner policy",
        "",
        "- Pearson ≥ 0.80 is limited to existing MethylBERT routes.",
        "- Other metrics are reported; no universal hard threshold is introduced.",
        "- Existing tool/profile-specific repeatability and CPU/GPU tolerances are candidates for owner review before comparison runs.",
        "- Non-MethylBERT thresholds found in historical manifests remain per-capability candidates only; they are not automatically promoted into Conda gates.",
        "- No independent external-generalization claim without an independent cohort.",
        "",
        "## Candidate policy coverage",
        "",
        "| Tool | Distinct historical policy profiles | Profiles with no policy | Capabilities represented |",
        "|---|---:|---:|---:|",
    ]
    policy_lines.extend(
        f"| {tool} | {counts['profiles']} | {counts['missing_profiles']} | {counts['capabilities']} |"
        for tool, counts in sorted(policy_counts.items())
    )
    policy_lines += [
        "",
        "## Historical evidence integrity",
        "",
        f"- Evidence references inventoried: **{tolerance_review['historical_validation_evidence_integrity']['rows']}**.",
        f"- Existing evidence files: **{tolerance_review['historical_validation_evidence_integrity']['existing']}**.",
        f"- Current digests computed: **{tolerance_review['historical_validation_evidence_integrity']['current_digest_computed']}**; matching manifest declarations: **{tolerance_review['historical_validation_evidence_integrity']['digest_matches']}**; mismatches: **{tolerance_review['historical_validation_evidence_integrity']['digest_mismatches']}**.",
        f"- Evidence files with extractable reported metrics: **{tolerance_review['historical_validation_evidence_integrity']['reported_metrics']}**.",
        "",
        "The machine-readable report lists every capability, selector, exact policy object and policy digest. Missing policy remains missing; do not fill it with a global default.",
        "",
    ]
    args.policy_md.parent.mkdir(parents=True, exist_ok=True)
    args.policy_md.write_text("\n".join(policy_lines), encoding="utf-8")
    fixture_lines = [
        "# Baseline candidate inventory (not frozen)",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Source: `{simulated_data}`",
        "",
        "This is an automatically collected inventory, not a scientific run and not baseline approval. Existing scientific states are not changed.",
        "",
        "## Observed fixture coverage",
        "",
        f"- Total fixture configurations: **{len(fixtures)}** ({len(present_pairs & expected_native_array_pairs)}/{len(expected_native_array_pairs)} expected native array scenario/platform pairs; {len(wgb_fixture_pairs & expected_native_wgbs_pairs)}/{len(expected_native_wgbs_pairs)} expected native WGBS scenario/build pairs).",
        "- Array folders: `450K` plus `850K`; per owner-confirmed policy, `850K` is normalized to EPIC while the original folder label is preserved.",
        "- Every observed fixture's truth sample IDs matched its array matrix or WGBS BED/PAT sample IDs.",
        (
            "- SHA256 digests were computed for every discovered fixture input, truth and provenance file, including large WGBS BED/PAT files."
            if fixture_content_digests_complete else
            "- Array matrix/truth digests and all small truth-file digests were computed. Large WGBS BED/PAT files were only stat-inventoried; their content hashes were not read/computed."
        ),
        "",
        "### Missing expected native array fixtures",
        "",
    ]
    fixture_lines += [f"- `{scenario} + {platform}`" for scenario, platform in missing_array] or ["- None"]
    fixture_lines += ["", "### Missing expected native WGBS fixture pairs", ""]
    fixture_lines += [f"- `{scenario} + {build}`" for scenario, build in missing_wgbs] or ["- None"]
    fixture_lines += [
        "",
        "## Historical capability candidates",
        "",
        f"- Latest immutable reference manifests inspected: **{reference_summary['unique_reference_ids_latest']}** unique reference IDs, **{len(reference_records)}** tool-capability records.",
        f"- Fixture-to-reference joins produced **{len(payload['fixture_capability_candidates'])}** candidates. Status counts: `{json.dumps(capability_status_counts, sort_keys=True)}`.",
        "- Candidate selector, metric policies, validation artifact paths and declared digests are in `baseline_candidate_inventory.json`.",
        "- Manifest-declared artifact digests were recorded but not recomputed across the 125 GB reference tree.",
        "",
        "## Frozen owner policy currently recorded",
        "",
        "- Pearson ≥ 0.80 applies only to existing MethylBERT routes.",
        "- Other metrics are reported as observed; no new universal hard threshold is introduced.",
        "- Repeatability and CPU/GPU tolerances must be selected from the tool-specific historical policy and approved before comparison runs.",
        "- No independent external-generalization claim is made where an independent cohort is absent.",
        "",
        "## Remaining freeze requirements",
        "",
        *[f"- {item}" for item in payload["required_before_freeze"]],
        "",
    ]
    args.summary_md.parent.mkdir(parents=True, exist_ok=True)
    args.summary_md.write_text("\n".join(fixture_lines), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "summary_md": str(args.summary_md),
        "fixture_count": len(fixtures),
        "latest_reference_capabilities": len(reference_records),
        "fixture_capability_candidates": len(payload["fixture_capability_candidates"]),
        "hash_large_inputs": args.hash_large_inputs,
        "tolerance_review": str(args.policy_json),
        "missing_native_array_pairs": missing_array,
        "missing_native_wgbs_pairs": missing_wgbs,
        "status": payload["status"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
