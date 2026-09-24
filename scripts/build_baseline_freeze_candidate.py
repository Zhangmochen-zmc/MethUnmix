#!/usr/bin/env python3
"""Join owner-approved policy with fixture, reference, runtime and history candidates.

This creates a review artifact only. It does not freeze a baseline, run a tool,
change scientific status, or infer missing tolerances.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "evidence/baseline_candidate_inventory.json"
DEFAULT_PACKAGE_MANIFEST = ROOT / "evidence/package_manifest_rc.json"
DEFAULT_OUTPUT = ROOT / "evidence/BASELINE_FREEZE_CANDIDATE.json"
DEFAULT_SUMMARY = ROOT / "evidence/BASELINE_FREEZE_CANDIDATE.md"
ELIGIBLE_STATUSES = {"READY", "RELEASED_UNVALIDATED"}


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def extract_seed_values(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if any(term in str(key).lower() for term in ("seed", "random_state")) and not isinstance(child, (dict, list)):
                result[path] = child
            elif isinstance(child, (dict, list)):
                result.update(extract_seed_values(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, (dict, list)):
                result.update(extract_seed_values(child, f"{prefix}[{index}]"))
    return result


def has_explicit_seed(values: dict[str, Any]) -> bool:
    for value in values.values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if isinstance(value, str) and (value.isdigit() or value.startswith("0x")):
            return True
    return False


def evidence_record(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        "kind": row.get("kind"),
        "path": row.get("path"),
        "exists": bool(row.get("exists")),
        "declared_sha256": row.get("manifest_sha256"),
        "observed_sha256": row.get("current_sha256"),
        "reported_status": row.get("reported_status"),
        "observed_metrics_and_thresholds": row.get("metric_summary", {}),
        "seed_values": {},
    }
    path = row.get("path")
    if row.get("exists") and isinstance(path, str):
        evidence_path = Path(path)
        try:
            if evidence_path.stat().st_size <= 2 * 1024 * 1024:
                result["seed_values"] = extract_seed_values(read_json(evidence_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            result["seed_values"] = {"status": "UNREADABLE_OR_NON_JSON"}
    if not result["seed_values"]:
        result["seed_values"] = {"status": "NOT_DECLARED_IN_LINKED_EVIDENCE"}
    return result


def fixture_seed_sources(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    sources = []
    for row in fixture.get("provenance_files", []):
        path = row.get("path")
        if not isinstance(path, str) or not path.lower().endswith(".json"):
            continue
        try:
            values = extract_seed_values(read_json(Path(path)))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            values = {"status": "UNREADABLE_OR_NON_JSON"}
        sources.append({
            "path": path,
            "sha256": row.get("sha256"),
            "seed_values": values or {"status": "NOT_DECLARED_IN_FIXTURE_PROVENANCE"},
        })
    return sources


def build(inventory: dict[str, Any], package: dict[str, Any], inventory_path: Path) -> dict[str, Any]:
    fixtures = {row["fixture_id"]: row for row in inventory.get("fixtures", [])}
    references = {row["capability_id"]: row for row in inventory.get("latest_reference_tool_candidates", [])}
    all_links = inventory.get("fixture_capability_candidates", [])
    linked_capability_ids = {str(row.get("capability_id")) for row in all_links}
    counts = Counter(str(row.get("scientific_status", "UNKNOWN")) for row in all_links)
    records: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for link in all_links:
        status = str(link.get("scientific_status", "UNKNOWN"))
        if status not in ELIGIBLE_STATUSES:
            excluded[status] += 1
            continue
        fixture = fixtures.get(str(link.get("fixture_id")))
        reference = references.get(str(link.get("capability_id")))
        if fixture is None or reference is None:
            excluded["MISSING_FIXTURE_OR_REFERENCE_JOIN"] += 1
            continue
        if not link.get("sample_mapping_valid"):
            excluded["SAMPLE_MAPPING_INVALID"] += 1
            continue
        if not link.get("input_content_digest_ready") or not link.get("truth_content_digest_ready"):
            excluded["FIXTURE_DIGEST_INCOMPLETE"] += 1
            continue

        profiles = reference.get("execution_profiles", {})
        profile_digest = canonical_sha256(profiles)
        policy = reference.get("validation_policy_candidate") or {}
        evidence = [evidence_record(row) for row in reference.get("validation_evidence", [])]
        fixture_seeds = fixture_seed_sources(fixture)
        historical_tolerances = []
        for item in evidence:
            metrics = item.get("observed_metrics_and_thresholds", {})
            selected = {
                str(key): value for key, value in metrics.items()
                if re.search(r"threshold|tolerance", str(key), re.IGNORECASE)
            }
            if selected:
                historical_tolerances.append({
                    "kind": item.get("kind"),
                    "path": item.get("path"),
                    "sha256": item.get("observed_sha256"),
                    "values": selected,
                })
        item = {
            "capability_id": link["capability_id"],
            "tool": link["tool"],
            "scientific_status_current": status,
            "execution_policy_current": reference.get("execution_policy"),
            "selector": reference["selector"],
            "validation_scope_current": reference.get("validation_scope"),
            "fixture": {
                "fixture_id": fixture["fixture_id"],
                "scenario": fixture["scenario"],
                "platform": fixture["platform"],
                "source_platform_label": fixture.get("source_platform_label"),
                "route": fixture["route"],
                "genome_build": fixture["genome_build"],
                "sample_mapping_audit": fixture.get("sample_mapping_audit"),
                "input_files": fixture.get("input_files", []),
                "input_file_interpretation": (
                    "For a WGBS-derived array projection, only BED/BED.GZ or BAM is an eligible source; PAT files are recorded as fixture assets but are not selected for this route."
                    if str(reference.get("analysis_contract", "")).startswith("wgbs_derived_")
                    else "All source fixture modalities and their digests are listed; final E2E evidence must bind the exact tool-specific input modality."
                ),
                "projection_compatible_inputs_present": (
                    [row for row in fixture.get("input_files", []) if row.get("relative_path", "").lower().endswith((".bed", ".bed.gz", ".bam"))]
                    if str(reference.get("analysis_contract", "")).startswith("wgbs_derived_")
                    else fixture.get("input_files", [])
                ),
                "truth_files": fixture.get("truth_files", []),
                "provenance_files": fixture.get("provenance_files", []),
                "seed_candidates_from_fixture_provenance": fixture_seeds,
                "input_content_digest_ready": fixture.get("large_input_content_digests_complete", False),
                "truth_content_digest_ready": fixture.get("truth_content_digest_complete", False),
                "scope_note": "Synthetic fixed-truth fixture only; not independent-cohort evidence.",
            },
            "reference": {
                "reference_id": reference["reference_id"],
                "version": reference["reference_version"],
                "scenario": reference.get("scenario"),
                "analysis_contract": reference.get("analysis_contract"),
                "reference_platform": reference.get("source_platform"),
                "genome_build": reference.get("genome_build"),
                "manifest_path": reference["manifest_path"],
                "manifest_sha256": reference["manifest_sha256"],
                "artifact_checksum_count": reference["artifact_checksum_count"],
                "artifact_checksum_map_sha256": reference["artifact_checksum_map_digest"],
                "artifact_verification_scope": "Manifest and declared checksum map are digest-bound; full referenced payload rehash was not performed by this candidate generator.",
            },
            "execution": {
                "profiles": profiles,
                "profile_contract_sha256": profile_digest,
                "runtime_modules_named_in_profiles": sorted({
                    module
                    for profile in (profiles.values() if isinstance(profiles, dict) else [])
                    if isinstance(profile, dict)
                    for module in profile.get("runtime_modules", [])
                    if isinstance(module, str)
                }),
                "runtime_binary_sha256": None,
                "runtime_binary_digest_status": "NOT_REHASHED_OR_NOT_IN_PROFILE; use manifest-declared artifact map for selected release verification",
            },
            "historical_validation": evidence,
            "policy_candidates": {
                "existing_tool_profile_policy": policy,
                "historical_evidence_tolerances": historical_tolerances,
                "policy_status": (
                    "MANIFEST_AND_EVIDENCE_CANDIDATES_REQUIRE_FINAL_OWNER_APPROVAL" if policy and historical_tolerances
                    else "MANIFEST_CANDIDATE_REQUIRES_FINAL_OWNER_APPROVAL" if policy
                    else "HISTORICAL_EVIDENCE_CANDIDATE_REQUIRES_FINAL_OWNER_APPROVAL" if historical_tolerances
                    else "NO_PREDECLARED_POLICY_FOUND; DO_NOT_INVENT_ONE"
                ),
                "methylbert_pearson_minimum": 0.80 if link["tool"] == "MethylBERT" else None,
                "other_metric_thresholds": "Report observed results; do not add a new universal gate.",
            },
            "approval": {"status": "PENDING_OWNER_FINAL_APPROVAL", "approver": None, "approved_at": None},
        }
        item["candidate_record_sha256"] = canonical_sha256(item)
        records.append(item)

    records.sort(key=lambda row: (row["capability_id"], row["fixture"]["fixture_id"]))
    package_ref = {
        "version": package.get("version"),
        "source_archive_sha256": package.get("source_archive", {}).get("sha256"),
        "wheel_sha256": package.get("staged_python_wheel", {}).get("sha256"),
        "conda_build": "NOT_BUILT_PENDING_EXTERNAL_BIOCONDA_CI",
    }
    inventory_sha = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    route_contract_counts = Counter(
        (row["fixture"]["route"], row["reference"]["analysis_contract"])
        for row in records
    )
    unmatched_ready_ru: Counter[tuple[str, str, str, str]] = Counter()
    for reference in inventory.get("latest_reference_tool_candidates", []):
        status = str(reference.get("scientific_status", "UNKNOWN"))
        if status in ELIGIBLE_STATUSES and str(reference.get("capability_id")) not in linked_capability_ids:
            unmatched_ready_ru[(
                str(reference.get("scenario")),
                str(reference.get("analysis_contract")),
                str(reference.get("genome_build")),
                status,
            )] += 1
    history_seed_counts = Counter()
    fixture_seed_pair_counts = Counter()
    for row in records:
        for item in row["historical_validation"]:
            values = item.get("seed_values", {})
            if not values or "status" in values:
                history_seed_counts["not_declared"] += 1
            elif has_explicit_seed(values):
                history_seed_counts["explicit_value"] += 1
            else:
                history_seed_counts["marker_without_reproducible_value"] += 1
        fixture_sources = row["fixture"].get("seed_candidates_from_fixture_provenance", [])
        values = [source.get("seed_values", {}) for source in fixture_sources]
        if any(has_explicit_seed(item) for item in values):
            fixture_seed_pair_counts["explicit_value"] += 1
        elif any(item and "status" not in item for item in values):
            fixture_seed_pair_counts["marker_without_reproducible_value"] += 1
        else:
            fixture_seed_pair_counts["not_declared_or_no_json_provenance"] += 1
    payload = {
        "schema": "methunmix-baseline-freeze-candidate-v1",
        "product": "MethUnmix",
        "version": package_ref["version"],
        "status": "CANDIDATE_PENDING_OWNER_APPROVAL",
        "baseline_frozen": False,
        "scientific_runs_performed": False,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_inventory": {"path": str(inventory_path), "sha256": inventory_sha},
        "confirmed_owner_policy": {
            "simulation_root": inventory.get("user_approved_input_root"),
            "pearson_minimum": {"value": 0.80, "scope": "existing MethylBERT routes only"},
            "other_metrics": "Report all observed values; no new universal hard thresholds.",
            "repeatability_cpu_gpu": "Use only pre-frozen tool/profile-specific tolerances; missing tolerances remain undefined.",
            "external_generalization": "Not claimed without an independent cohort.",
            "scientific_state_promotion": "Prohibited by this packaging baseline process.",
            "850K": "Normalize to EPIC and preserve original fixture label.",
        },
        "package_binding": package_ref,
        "scope": {
            "fixture_configurations": len(inventory.get("fixtures", [])),
            "ready_ru_candidate_pairs": len(records),
            "ready_ru_unmatched_reference_capabilities": [
                {"scenario": key[0], "analysis_contract": key[1], "genome_build": key[2], "scientific_status": key[3], "count": value}
                for key, value in sorted(unmatched_ready_ru.items())
            ],
            "route_contract_counts": [
                {"fixture_route": key[0], "analysis_contract": key[1], "record_count": value}
                for key, value in sorted(route_contract_counts.items())
            ],
            "seed_metadata_extraction": {
                "historical_validation_evidence": dict(sorted(history_seed_counts.items())),
                "fixture_pair_seed_candidates": dict(sorted(fixture_seed_pair_counts.items())),
                "policy": "Only seed metadata explicitly present in the digest-bound evidence/provenance is recorded; missing seed is not inferred.",
            },
            "status_counts_all_fixture_reference_joins": dict(sorted(counts.items())),
            "excluded_candidate_counts": dict(sorted(excluded.items())),
            "missing_array_fixture_pairs": inventory.get("coverage_summary", {}).get("missing_native_array_fixture_pairs", []),
            "missing_wgbs_fixture_pairs": inventory.get("coverage_summary", {}).get("missing_native_wgbs_fixture_pairs", []),
            "explicit_scope_boundary": "Only existing READY/RU selectors joined to available fixtures are represented. Unavailable/Q/N-adapted rows, missing fixtures, and unsupported input modes are not treated as tested or passed. This is not a hand-authored truth set per static capability combination.",
        },
        "records": records,
        "approval_required": "Review the generated selectors, fixture mappings and policy candidates as a whole; this artifact is not frozen until the owner approves it. No fixture SHA256 or per-combination truth file needs to be supplied manually.",
    }
    payload["candidate_manifest_sha256"] = canonical_sha256(payload)
    return payload


def render_summary(payload: dict[str, Any]) -> str:
    scope = payload["scope"]
    lines = [
        "# Baseline freeze candidate (not frozen)",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Status: `{payload['status']}`",
        "",
        "This is a digest-bound review candidate assembled from the confirmed simulation directory, current READY/RU reference selectors, execution profiles and existing validation evidence. It does not run deconvolution or promote scientific status.",
        "",
        "## Scope",
        "",
        f"- Available fixture configurations: **{scope['fixture_configurations']}**.",
        f"- READY/RU fixture-selector pairs with valid sample mapping and complete input/truth SHA256: **{scope['ready_ru_candidate_pairs']}**.",
        f"- Candidate route/contract coverage: `{json.dumps(scope['route_contract_counts'], sort_keys=True)}`.",
        f"- READY/RU reference capabilities without a compatible fixture: `{json.dumps(scope['ready_ru_unmatched_reference_capabilities'], sort_keys=True)}`; these remain untested, not failed or passed.",
        f"- Seed metadata extracted from historical evidence/provenance: `{json.dumps(scope['seed_metadata_extraction'], sort_keys=True)}`; missing values are left unfilled.",
        f"- Other joined statuses excluded from baseline records: `{json.dumps(scope['excluded_candidate_counts'], sort_keys=True)}`.",
        f"- Missing native array fixture pairs: `{json.dumps(scope['missing_array_fixture_pairs'])}`; these are uncovered, not failures.",
        f"- Missing native WGBS fixture pairs: `{json.dumps(scope['missing_wgbs_fixture_pairs'])}`.",
        "- WGBS input and truth digests are included per file; reference manifest and declared artifact-map digests are bound, but the full 125 GB reference payload was not rehashed.",
        "- Conda build digest remains pending Bioconda CI; current source/wheel digests are attached for candidate review.",
        "",
        "## Policy boundary",
        "",
        "- Pearson ≥ 0.80 applies only to existing MethylBERT routes.",
        "- All other metrics are recorded as observed; no new universal threshold is introduced.",
        "- Existing tool/profile repeatability and CPU/GPU tolerances are included only as candidates; absent policies remain undefined.",
        "- No independent-cohort generalization claim is made.",
        "- READY/RU/QUARANTINED/NOT_AVAILABLE state is not changed by this candidate.",
        "",
        "## Owner action",
        "",
        "Review the generated manifest once as a whole and provide the final approval before treating it as the release baseline. No manual SHA256 values or 3,276 hand-built truth records are requested.",
        "",
        f"Candidate manifest digest: `{payload['candidate_manifest_sha256']}`",
        "",
        "Machine-readable records: `BASELINE_FREEZE_CANDIDATE.json`.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--package-manifest", type=Path, default=DEFAULT_PACKAGE_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()
    payload = build(read_json(args.inventory), read_json(args.package_manifest), args.inventory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    args.summary.write_text(render_summary(payload), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "summary": str(args.summary),
        "status": payload["status"],
        "record_count": len(payload["records"]),
        "candidate_manifest_sha256": payload["candidate_manifest_sha256"],
        "scientific_runs_performed": payload["scientific_runs_performed"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
