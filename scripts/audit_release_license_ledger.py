#!/usr/bin/env python3
"""Fail-closed audit for owner-approved MethUnmix release-license scope.

The existing reference approval record proves only the approved reference
selectors.  It must not be mistaken for a decision about the core payload,
external runtime/model/cache distribution, or the exact candidate artifacts.
This auditor validates an owner-provided release ledger against those current
identities.  It never makes a legal decision and a missing ledger is an
expected, non-zero-claim result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "evidence" / "license_approval_decisions.json"
DEFAULT_INVENTORY = ROOT / "evidence" / "LICENSE_GAP_INVENTORY.json"
DEFAULT_REFERENCE_APPROVAL = ROOT / "evidence" / "reference_license_approvals.rc3-static.2.json"
DEFAULT_REFERENCE_TRANSPORT = ROOT / "evidence" / "reference_public_distribution_gate_rc3.json"
DEFAULT_REFERENCE_REMOTE_AUDIT = ROOT / "evidence" / "reference-public-remote-audit-rc3-static.2.json"
DEFAULT_RC_EVIDENCE = ROOT / "evidence" / "rc3_local_release_gate.json"

REQUIRED_SCOPE_DECISIONS = {
    "core_payload": "APPROVED_FOR_PUBLIC_REDISTRIBUTION",
    "public_references": "APPROVED_FOR_PUBLIC_REDISTRIBUTION",
    "external_runtimes": "NOT_DISTRIBUTED_USER_SUPPLIED_ONLY",
    "models_and_caches": "NOT_DISTRIBUTED_USER_SUPPLIED_ONLY",
}


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def record(path: Path) -> dict[str, Any]:
    return {"path": str(path), "exists": path.is_file(), "sha256": sha256(path)}


def reference_approval_is_complete(payload: dict[str, Any], transport: dict[str, Any], remote_audit: dict[str, Any]) -> bool:
    approvals = payload.get("approvals")
    counts = transport.get("counts") if isinstance(transport.get("counts"), dict) else {}
    targets = remote_audit.get("targets")
    if not isinstance(approvals, list) or not isinstance(targets, list) or counts.get("catalog_targets") != 33:
        return False
    approved_selectors = {
        item.get("selector") for item in approvals
        if isinstance(item, dict) and item.get("redistribution_approved") is True and isinstance(item.get("selector"), str)
    }
    published_selectors = {
        item.get("selector") for item in targets
        if isinstance(item, dict)
        and item.get("distribution_status") == "PUBLIC_DOWNLOADABLE"
        and item.get("status") == "PASS"
        and isinstance(item.get("selector"), str)
    }
    return len(approved_selectors) == 33 and len(approvals) == 33 and approved_selectors == published_selectors


def current_context(
    *,
    inventory_path: Path = DEFAULT_INVENTORY,
    reference_approval_path: Path = DEFAULT_REFERENCE_APPROVAL,
    reference_transport_path: Path = DEFAULT_REFERENCE_TRANSPORT,
    reference_remote_audit_path: Path = DEFAULT_REFERENCE_REMOTE_AUDIT,
    rc_evidence_path: Path = DEFAULT_RC_EVIDENCE,
) -> dict[str, Any]:
    inventory = load_json(inventory_path)
    approval = load_json(reference_approval_path)
    transport = load_json(reference_transport_path)
    remote_audit = load_json(reference_remote_audit_path)
    rc = load_json(rc_evidence_path)
    artifacts = rc.get("artifact_sha256") if isinstance(rc.get("artifact_sha256"), dict) else {}
    catalog = transport.get("catalog") if isinstance(transport.get("catalog"), dict) else {}
    return {
        "core_version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "source_commit": rc.get("source_commit"),
        "artifact_sha256": {"source": artifacts.get("source"), "wheel": artifacts.get("wheel")},
        "inventory": record(inventory_path),
        "reference_approval": record(reference_approval_path),
        "reference_transport": record(reference_transport_path),
        "reference_remote_audit": record(reference_remote_audit_path),
        "reference_catalog": {
            "release": catalog.get("release"),
            "sha256": catalog.get("sha256"),
            "target_count": (transport.get("counts") or {}).get("catalog_targets") if isinstance(transport.get("counts"), dict) else None,
        },
        "reference_approval_complete": reference_approval_is_complete(approval, transport, remote_audit),
        "reference_transport_technical_ready": transport.get("overall_status") == "TECHNICAL_REFERENCE_AUTODOWNLOAD_READY",
        "inventory_schema": inventory.get("schema"),
        "qualification_status": rc.get("local_qualification_status"),
    }


def template(context: dict[str, Any]) -> dict[str, Any]:
    """Return an intentionally non-approving owner decision template."""
    return {
        "schema": "methunmix-release-license-approvals-v1",
        "status": "DRAFT_OWNER_DECISION_REQUIRED",
        "product": "MethUnmix",
        "core_version": context["core_version"],
        "candidate": {
            "source_commit": context["source_commit"],
            # Keep the template isolated from the observed current context.
            # An owner editing a template must never mutate the candidate
            # identity against which that template will later be audited.
            "artifact_sha256": dict(context["artifact_sha256"]),
        },
        "bound_evidence": {
            "license_inventory_sha256": context["inventory"]["sha256"],
            "reference_approval_sha256": context["reference_approval"]["sha256"],
            "reference_catalog_release": context["reference_catalog"]["release"],
            "reference_catalog_sha256": context["reference_catalog"]["sha256"],
            "reference_remote_audit_sha256": context["reference_remote_audit"]["sha256"],
        },
        "scope_decisions": dict(REQUIRED_SCOPE_DECISIONS),
        "approved_by": "",
        "approved_at": "",
        "decision_basis": "",
        "owner_note": "Fill only after reviewing the exact candidate and all scope decisions.",
    }


def audit_ledger(ledger: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    if ledger.get("schema") != "methunmix-release-license-approvals-v1":
        problems.append("schema must be methunmix-release-license-approvals-v1")
    if ledger.get("status") != "APPROVED_FULL_RELEASE_SCOPE":
        problems.append("status must be APPROVED_FULL_RELEASE_SCOPE")
    if ledger.get("product") != "MethUnmix":
        problems.append("product must be MethUnmix")
    if ledger.get("core_version") != context["core_version"]:
        problems.append("core_version does not bind the current candidate")
    candidate = ledger.get("candidate") if isinstance(ledger.get("candidate"), dict) else {}
    if candidate.get("source_commit") != context["source_commit"]:
        problems.append("candidate.source_commit does not bind the current candidate")
    if candidate.get("artifact_sha256") != context["artifact_sha256"]:
        problems.append("candidate.artifact_sha256 does not bind current source/wheel")
    bound = ledger.get("bound_evidence") if isinstance(ledger.get("bound_evidence"), dict) else {}
    expected_bound = {
        "license_inventory_sha256": context["inventory"]["sha256"],
        "reference_approval_sha256": context["reference_approval"]["sha256"],
        "reference_catalog_release": context["reference_catalog"]["release"],
        "reference_catalog_sha256": context["reference_catalog"]["sha256"],
        "reference_remote_audit_sha256": context["reference_remote_audit"]["sha256"],
    }
    for key, expected in expected_bound.items():
        if not isinstance(expected, str) or bound.get(key) != expected:
            problems.append(f"bound_evidence.{key} is missing or does not match current evidence")
    decisions = ledger.get("scope_decisions") if isinstance(ledger.get("scope_decisions"), dict) else {}
    for key, expected in REQUIRED_SCOPE_DECISIONS.items():
        if decisions.get(key) != expected:
            problems.append(f"scope_decisions.{key} must be {expected}")
    for key in ("approved_by", "approved_at", "decision_basis"):
        if not isinstance(ledger.get(key), str) or not ledger[key].strip():
            problems.append(f"{key} must be a non-empty owner decision record")
    if context["qualification_status"] != "LOCAL_RC_CANDIDATE_QUALIFIED":
        problems.append("current local candidate qualification is not PASS")
    if not context["reference_transport_technical_ready"]:
        problems.append("reference transport is not technically ready")
    if not context["reference_approval_complete"]:
        problems.append("33 public reference approvals are not complete")
    return {
        "status": "APPROVED_FULL_RELEASE_SCOPE_CURRENT_CANDIDATE" if not problems else "LICENSE_LEDGER_INCOMPLETE_OR_UNBOUND",
        "problems": problems,
        "required_scope_decisions": REQUIRED_SCOPE_DECISIONS,
    }


def build_audit(
    *,
    ledger_path: Path = DEFAULT_LEDGER,
    inventory_path: Path = DEFAULT_INVENTORY,
    reference_approval_path: Path = DEFAULT_REFERENCE_APPROVAL,
    reference_transport_path: Path = DEFAULT_REFERENCE_TRANSPORT,
    reference_remote_audit_path: Path = DEFAULT_REFERENCE_REMOTE_AUDIT,
    rc_evidence_path: Path = DEFAULT_RC_EVIDENCE,
) -> dict[str, Any]:
    context = current_context(
        inventory_path=inventory_path,
        reference_approval_path=reference_approval_path,
        reference_transport_path=reference_transport_path,
        reference_remote_audit_path=reference_remote_audit_path,
        rc_evidence_path=rc_evidence_path,
    )
    ledger_record = record(ledger_path)
    if not ledger_path.is_file():
        result = {
            "status": "MISSING_OWNER_APPROVAL_LEDGER",
            "problems": ["owner-approved release-license ledger has not been supplied"],
            "required_scope_decisions": REQUIRED_SCOPE_DECISIONS,
        }
    else:
        result = audit_ledger(load_json(ledger_path), context)
    return {
        "schema": "methunmix-release-license-ledger-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "license-decision binding audit only; no legal conclusion, asset publication, status promotion, tag, upload, or external fetch",
        "ledger": ledger_record,
        "context": context,
        "result": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--reference-approval", type=Path, default=DEFAULT_REFERENCE_APPROVAL)
    parser.add_argument("--reference-transport", type=Path, default=DEFAULT_REFERENCE_TRANSPORT)
    parser.add_argument("--reference-remote-audit", type=Path, default=DEFAULT_REFERENCE_REMOTE_AUDIT)
    parser.add_argument("--rc-evidence", type=Path, default=DEFAULT_RC_EVIDENCE)
    parser.add_argument("--write-template", type=Path, help="write a non-approving template and exit")
    parser.add_argument("--output", type=Path, help="write the audit JSON")
    args = parser.parse_args()
    context = current_context(
        inventory_path=args.inventory,
        reference_approval_path=args.reference_approval,
        reference_transport_path=args.reference_transport,
        reference_remote_audit_path=args.reference_remote_audit,
        rc_evidence_path=args.rc_evidence,
    )
    if args.write_template:
        args.write_template.parent.mkdir(parents=True, exist_ok=True)
        args.write_template.write_text(json.dumps(template(context), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "TEMPLATE_WRITTEN_NON_APPROVING", "output": str(args.write_template)}, ensure_ascii=False))
        return 0
    payload = build_audit(
        ledger_path=args.ledger,
        inventory_path=args.inventory,
        reference_approval_path=args.reference_approval,
        reference_transport_path=args.reference_transport,
        reference_remote_audit_path=args.reference_remote_audit,
        rc_evidence_path=args.rc_evidence,
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
