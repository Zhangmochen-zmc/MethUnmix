#!/usr/bin/env python3
"""Inventory GPU capability claims against immutable reference evidence.

This static audit hashes only manifests and small JSON validation reports. It
does not load models, execute an inference, or promote scientific status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "evidence/capability_matrix_rc.json"
DEFAULT_REFERENCES = ROOT / "references"
DEFAULT_OUTPUT = ROOT / "evidence/gpu_claims_audit_rc.json"
MAX_REPORT_BYTES = 16 * 1024 * 1024
EVIDENCE_KEY = re.compile(r"gpu|profile|cpu.?gpu|engineering.?validation|scientific.?validation|repeatability|nextflow.?e2e", re.I)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def safe_bundle_file(bundle: Path, relative: str) -> Path | None:
    rel = Path(relative)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        return None
    candidate = bundle / rel
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
        resolved_bundle = bundle.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        if not resolved_candidate.is_relative_to(resolved_bundle):
            return None
    except OSError:
        return None
    return candidate


def evidence_paths(capability: dict[str, Any]) -> list[tuple[str, str]]:
    paths: set[tuple[str, str]] = set()
    artifacts = capability.get("artifacts", {})
    if isinstance(artifacts, dict):
        for key, value in artifacts.items():
            if EVIDENCE_KEY.search(str(key)) and isinstance(value, str) and value.lower().endswith(".json"):
                paths.add((str(key), value))
    declared = capability.get("validation_evidence", [])
    if isinstance(declared, str):
        declared = [declared]
    if isinstance(declared, list):
        for item in declared:
            if isinstance(item, str):
                paths.add(("validation_evidence", item))
            elif isinstance(item, dict):
                path = item.get("path")
                if isinstance(path, str):
                    paths.add((str(item.get("kind", "validation_evidence")), path))
    return sorted(paths)


def expected_file_digest(checksums: dict[str, Any], relative: str) -> str | None:
    value = checksums.get(relative)
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, dict):
        digest = value.get("sha256")
        return digest.lower() if isinstance(digest, str) else None
    return None


def referenced_selectors(value: Any, key: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for child_key, child in value.items():
            found.update(referenced_selectors(child, str(child_key)))
    elif isinstance(value, list):
        for child in value:
            found.update(referenced_selectors(child, key))
    elif isinstance(value, str) and re.search(r"reference|selector", key, re.I) and value.startswith("builtin.") and "@" in value:
        found.add(value)
    return found


def probe_gpu() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"status": "NOT_AVAILABLE", "reason": "nvidia-smi not found", "hardware_inference_run": False}
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "NOT_AVAILABLE", "reason": type(exc).__name__, "hardware_inference_run": False}
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:400]
        return {"status": "NOT_AVAILABLE", "reason": detail or f"nvidia-smi exit {result.returncode}", "hardware_inference_run": False}
    devices = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return {"status": "AVAILABLE" if devices else "NO_DEVICE", "devices": devices, "hardware_inference_run": False}


def audit(matrix_path: Path, references_root: Path) -> dict[str, Any]:
    matrix = read_json(matrix_path)
    rows = matrix.get("technical_candidates", [])
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("device") == "gpu":
            unique.setdefault((str(row.get("selector", "")), str(row.get("tool", ""))), row)

    cases: list[dict[str, Any]] = []
    for (selector, tool), row in sorted(unique.items()):
        item: dict[str, Any] = {
            "selector": selector,
            "tool": tool,
            "capability_ids": sorted(
                str(candidate.get("capability_id")) for candidate in rows
                if isinstance(candidate, dict) and candidate.get("device") == "gpu"
                and candidate.get("selector") == selector and candidate.get("tool") == tool
            ),
            "matrix_scientific_status": row.get("scientific_status"),
            "matrix_device_profile_status": row.get("device_profile_status"),
            "manifest_status": "PENDING",
            "gpu_profile_status": "MISSING",
            "manifest_digest_match": False,
            "evidence": [],
            "warnings": [],
        }
        try:
            ref_id, version = selector.rsplit("@", 1)
        except ValueError:
            item["warnings"].append("selector is not versioned")
            cases.append(item)
            continue
        bundle = references_root / ref_id / version
        manifest_path = bundle / "manifest.json"
        manifest = read_json(manifest_path)
        if not manifest:
            item["warnings"].append("manifest missing or invalid JSON")
            cases.append(item)
            continue
        item["manifest_status"] = manifest.get("status")
        actual_manifest_sha = sha256(manifest_path)
        item["manifest_sha256"] = actual_manifest_sha
        item["manifest_digest_match"] = actual_manifest_sha == row.get("manifest_sha256")
        if not item["manifest_digest_match"]:
            item["warnings"].append("manifest digest differs from capability matrix")
        cap = manifest.get("tool_capabilities", {}).get(tool, {})
        profile = cap.get("execution_profiles", {}).get("gpu", {}) if isinstance(cap, dict) else {}
        release_validation = manifest.get("release_validation", {})
        tool_binding = release_validation.get("tool_validation_evidence", {}).get(tool, {}) if isinstance(release_validation, dict) else {}
        if not isinstance(tool_binding, dict):
            tool_binding = {}
        item["evidence_binding"] = {
            "mode": tool_binding.get("binding"),
            "scientific_inference_reexecuted": tool_binding.get("scientific_inference_reexecuted"),
            "release_record_status": tool_binding.get("status"),
        }
        item["tool_scientific_status"] = cap.get("status") if isinstance(cap, dict) else None
        item["gpu_profile_status"] = profile.get("status", "MISSING") if isinstance(profile, dict) else "MISSING"
        item["gpu_runtime_modules"] = profile.get("runtime_modules", []) if isinstance(profile, dict) else []
        item["gpu_profile_reason"] = profile.get("reason") if isinstance(profile, dict) else None
        checksums = manifest.get("artifact_checksums", {})
        declared_evidence = evidence_paths(cap) if isinstance(cap, dict) else []
        if not declared_evidence:
            item["warnings"].append("no profile/GPU validation artifact path declared")
        for key, relative in declared_evidence:
            record: dict[str, Any] = {"key": key, "path": relative}
            path = safe_bundle_file(bundle, relative)
            if path is None:
                record["status"] = "MISSING_OR_UNSAFE_PATH"
                item["warnings"].append(f"evidence path missing/unsafe: {relative}")
                item["evidence"].append(record)
                continue
            size = path.stat().st_size
            record["bytes"] = size
            if size > MAX_REPORT_BYTES:
                record["status"] = "TOO_LARGE_TO_PARSE"
                item["warnings"].append(f"evidence JSON exceeds static-audit size cap: {relative}")
                item["evidence"].append(record)
                continue
            actual = sha256(path)
            declared = expected_file_digest(checksums, relative) if isinstance(checksums, dict) else None
            record["sha256"] = actual
            record["declared_sha256"] = declared
            record["checksum_match"] = declared == actual if declared else None
            payload = read_json(path)
            record["reported_status"] = payload.get("status")
            record["referenced_selectors"] = sorted(referenced_selectors(payload))
            other_selectors = [s for s in record["referenced_selectors"] if s != selector]
            if other_selectors:
                record["selector_binding_warning"] = other_selectors
                if tool_binding.get("binding") == "byte_identity_reuse":
                    record["lineage_classification"] = "DECLARED_BYTE_IDENTITY_REUSE_NOT_REEXECUTED"
                else:
                    record["lineage_classification"] = "HISTORICAL_SELECTOR_REVIEW_REQUIRED"
                    item["warnings"].append(f"report references non-current selector(s) without an explicit byte-identity binding: {', '.join(other_selectors)}")
            if not declared:
                record["status"] = "PRESENT_UNCHECKSUMMED"
                item["warnings"].append(f"no manifest checksum for evidence: {relative}")
            elif declared != actual:
                record["status"] = "CHECKSUM_MISMATCH"
                item["warnings"].append(f"evidence checksum mismatch: {relative}")
            else:
                record["status"] = "CHECKSUM_MATCH"
            item["evidence"].append(record)
        cases.append(item)

    mismatch_count = sum(1 for case in cases if not case["manifest_digest_match"])
    missing_profile_count = sum(1 for case in cases if case["gpu_profile_status"] == "MISSING")
    evidence_hash_failures = sum(
        1 for case in cases for evidence in case["evidence"]
        if evidence.get("status") in {"CHECKSUM_MISMATCH", "MISSING_OR_UNSAFE_PATH", "TOO_LARGE_TO_PARSE"}
    )
    status_counts = Counter(case["gpu_profile_status"] for case in cases)
    matrix_valid = matrix.get("schema") in {"methunmix-capability-matrix-v1", "methunmix-capability-matrix-v2"} and bool(cases)
    structural_pass = matrix_valid and mismatch_count == 0 and missing_profile_count == 0 and evidence_hash_failures == 0
    selector_binding_warnings = sum(
        1 for case in cases for evidence in case["evidence"]
        if evidence.get("lineage_classification") == "HISTORICAL_SELECTOR_REVIEW_REQUIRED"
    )
    historical_selector_files = sum(
        1 for case in cases for evidence in case["evidence"] if evidence.get("selector_binding_warning")
    )
    return {
        "schema": "methunmix-gpu-claims-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": (
            "STATIC_HASHES_PASS_WITH_SELECTOR_REVIEW_RUNTIME_E2E_PENDING"
            if structural_pass and selector_binding_warnings
            else "STATIC_EVIDENCE_PASS_RUNTIME_E2E_PENDING"
            if structural_pass
            else "STATIC_EVIDENCE_REVIEW_REQUIRED"
        ),
        "scope": "static_manifest_and_small_validation_report_audit_only",
        "scope_limits": [
            "does not execute GPU inference or Nextflow",
            "does not promote READY/RU/QUARANTINED status",
            "does not prove the current Conda environment contains a working GPU runtime",
        ],
        "matrix": str(matrix_path),
        "references_root": str(references_root),
        "summary": {
            "gpu_capability_rows": sum(1 for row in rows if isinstance(row, dict) and row.get("device") == "gpu") if isinstance(rows, list) else 0,
            "unique_selector_tool_profiles": len(cases),
            "profile_status_counts": dict(sorted(status_counts.items())),
            "manifest_digest_mismatches": mismatch_count,
            "missing_gpu_profiles": missing_profile_count,
            "evidence_hash_or_path_failures": evidence_hash_failures,
            "profile_cases_with_warnings": sum(bool(case["warnings"]) for case in cases),
            "evidence_reports_referencing_noncurrent_selectors": historical_selector_files,
            "historical_selector_reports_without_explicit_byte_identity_binding": selector_binding_warnings,
            "profiles_with_explicit_byte_identity_reuse": sum(case["evidence_binding"].get("mode") == "byte_identity_reuse" for case in cases),
        },
        "current_host_gpu_probe": probe_gpu(),
        "runtime_conda_e2e": "PENDING",
        "tolerances": "No new global tolerance applied; use pre-frozen tool/profile policy only.",
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--references", type=Path, default=DEFAULT_REFERENCES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = audit(args.matrix, args.references)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "summary": report["summary"], "current_host_gpu_probe": report["current_host_gpu_probe"], "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0 if report["status"] != "STATIC_EVIDENCE_REVIEW_REQUIRED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
