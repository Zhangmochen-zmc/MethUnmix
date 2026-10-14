#!/usr/bin/env python3
"""Fail-closed validation of a complete MethUnmix release-SBOM scope ledger.

``SBOM_CORE_RC.json`` deliberately proves only the staged core candidate.  A
complete-release assertion additionally requires a resolved Conda environment
and one immutable SBOM record for every public reference object.  This helper
does not download those records or make legal decisions; it checks whether an
owner-supplied scope ledger is complete and bound to the current identities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "evidence" / "release_sbom_scope.json"
DEFAULT_CORE_SBOM = ROOT / "evidence" / "SBOM_CORE_RC.json"
DEFAULT_RC = ROOT / "evidence" / "rc3_local_release_gate.json"
DEFAULT_REMOTE_AUDIT = ROOT / "evidence" / "reference-public-remote-audit-rc3-static.2.json"
DEFAULT_LICENSE_LEDGER_AUDIT = ROOT / "evidence" / "release_license_ledger_audit_rc3.json"


def digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def record(path: Path) -> dict[str, Any]:
    return {"path": str(path), "exists": path.is_file(), "sha256": digest(path)}


def _property(payload: dict[str, Any], name: str) -> str | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    for item in metadata.get("properties", []):
        if isinstance(item, dict) and item.get("name") == name and isinstance(item.get("value"), str):
            return item["value"]
    return None


def current_context(
    *,
    core_sbom_path: Path = DEFAULT_CORE_SBOM,
    rc_path: Path = DEFAULT_RC,
    remote_audit_path: Path = DEFAULT_REMOTE_AUDIT,
    license_ledger_audit_path: Path = DEFAULT_LICENSE_LEDGER_AUDIT,
) -> dict[str, Any]:
    rc = load(rc_path)
    core = load(core_sbom_path)
    remote = load(remote_audit_path)
    license_audit = load(license_ledger_audit_path)
    artifacts = rc.get("artifact_sha256") if isinstance(rc.get("artifact_sha256"), dict) else {}
    public_references = {
        item.get("selector"): item.get("expected_sha256")
        for item in remote.get("targets", [])
        if isinstance(item, dict)
        and item.get("distribution_status") == "PUBLIC_DOWNLOADABLE"
        and item.get("status") == "PASS"
        and isinstance(item.get("selector"), str)
        and isinstance(item.get("expected_sha256"), str)
    }
    return {
        "core_version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "source_commit": rc.get("source_commit"),
        "artifact_sha256": {"source": artifacts.get("source"), "wheel": artifacts.get("wheel")},
        "core_sbom": record(core_sbom_path),
        "core_sbom_partial_bound": (
            core.get("bomFormat") == "CycloneDX"
            and _property(core, "methunmix:sbomCompleteness") == "PARTIAL_RC_CORE_ONLY"
        ),
        "remote_audit": record(remote_audit_path),
        "public_references": public_references,
        "license_ledger_audit": record(license_ledger_audit_path),
        "license_ledger_approved": license_audit.get("result", {}).get("status") == "APPROVED_FULL_RELEASE_SCOPE_CURRENT_CANDIDATE",
        "qualification_status": rc.get("local_qualification_status"),
    }


def template(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "methunmix-release-sbom-scope-v1",
        "status": "DRAFT_SBOM_SCOPE_REQUIRED",
        "product": "MethUnmix",
        "core_version": context["core_version"],
        "candidate": {"source_commit": context["source_commit"], "artifact_sha256": dict(context["artifact_sha256"])},
        "bound_evidence": {
            "core_sbom_sha256": context["core_sbom"]["sha256"],
            "reference_remote_audit_sha256": context["remote_audit"]["sha256"],
            "license_ledger_audit_sha256": context["license_ledger_audit"]["sha256"],
        },
        "conda_environment": {"lock_sha256": "", "sbom_sha256": "", "sbom_url": ""},
        "public_reference_sboms": [
            {"selector": selector, "archive_sha256": archive, "sbom_sha256": "", "sbom_url": ""}
            for selector, archive in sorted(context["public_references"].items())
        ],
        "scope_note": "Complete only after resolved Conda and every public reference SBOM have immutable evidence.",
    }


def audit_ledger(ledger: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    if ledger.get("schema") != "methunmix-release-sbom-scope-v1":
        problems.append("schema must be methunmix-release-sbom-scope-v1")
    if ledger.get("status") != "COMPLETE_RELEASE_SCOPE":
        problems.append("status must be COMPLETE_RELEASE_SCOPE")
    if ledger.get("product") != "MethUnmix" or ledger.get("core_version") != context["core_version"]:
        problems.append("product/core_version does not bind current candidate")
    candidate = ledger.get("candidate") if isinstance(ledger.get("candidate"), dict) else {}
    if candidate.get("source_commit") != context["source_commit"] or candidate.get("artifact_sha256") != context["artifact_sha256"]:
        problems.append("candidate identity does not bind current source/wheel")
    bound = ledger.get("bound_evidence") if isinstance(ledger.get("bound_evidence"), dict) else {}
    for key, expected in {
        "core_sbom_sha256": context["core_sbom"]["sha256"],
        "reference_remote_audit_sha256": context["remote_audit"]["sha256"],
        "license_ledger_audit_sha256": context["license_ledger_audit"]["sha256"],
    }.items():
        if not isinstance(expected, str) or bound.get(key) != expected:
            problems.append(f"bound_evidence.{key} is missing or stale")
    conda = ledger.get("conda_environment") if isinstance(ledger.get("conda_environment"), dict) else {}
    if not all(isinstance(conda.get(key), str) and conda[key].strip() for key in ("lock_sha256", "sbom_sha256", "sbom_url")):
        problems.append("conda_environment requires immutable lock, SBOM digest and SBOM URL")
    records = ledger.get("public_reference_sboms")
    seen: dict[str, dict[str, Any]] = {}
    if isinstance(records, list):
        seen = {item.get("selector"): item for item in records if isinstance(item, dict) and isinstance(item.get("selector"), str)}
    if set(seen) != set(context["public_references"]):
        problems.append("public_reference_sboms selector set does not exactly match published references")
    for selector, archive_digest in context["public_references"].items():
        item = seen.get(selector, {})
        if item.get("archive_sha256") != archive_digest:
            problems.append(f"public_reference_sboms.{selector} archive digest mismatch")
        if not all(isinstance(item.get(key), str) and item[key].strip() for key in ("sbom_sha256", "sbom_url")):
            problems.append(f"public_reference_sboms.{selector} lacks immutable SBOM evidence")
    if context["qualification_status"] != "LOCAL_RC_CANDIDATE_QUALIFIED":
        problems.append("current local candidate qualification is not PASS")
    if not context["core_sbom_partial_bound"]:
        problems.append("partial core SBOM is not a valid current candidate baseline")
    if not context["license_ledger_approved"]:
        problems.append("release-license ledger is not approved for this candidate")
    return {"status": "COMPLETE_RELEASE_SCOPE_CURRENT_CANDIDATE" if not problems else "SBOM_SCOPE_INCOMPLETE_OR_UNBOUND", "problems": problems}


def build_audit(*, ledger_path: Path = DEFAULT_LEDGER, **paths: Path) -> dict[str, Any]:
    context = current_context(**paths)
    result = (
        audit_ledger(load(ledger_path), context)
        if ledger_path.is_file()
        else {"status": "MISSING_COMPLETE_RELEASE_SBOM_SCOPE", "problems": ["complete release-SBOM scope ledger has not been supplied"]}
    )
    return {
        "schema": "methunmix-release-sbom-scope-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "SBOM-scope binding audit only; no external fetch, publication, license decision or release-state promotion",
        "ledger": record(ledger_path),
        "context": context,
        "result": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--write-template", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    context = current_context()
    if args.write_template:
        args.write_template.parent.mkdir(parents=True, exist_ok=True)
        args.write_template.write_text(json.dumps(template(context), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "TEMPLATE_WRITTEN_NON_APPROVING", "output": str(args.write_template)}))
        return 0
    payload = build_audit(ledger_path=args.ledger)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
