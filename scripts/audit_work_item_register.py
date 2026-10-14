#!/usr/bin/env python3
"""Validate MethUnmix's machine-readable future-work item register.

The register is a planning and change-control artifact.  It intentionally
cannot promote a scientific state, authorize publication, or turn an external
release gate into a local PASS.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IDS = [f"WP-{index:02d}" for index in range(9)]
REQUIRED_FIELDS = {
    "id", "priority", "target", "current_basis", "allowed_scope", "out_of_scope",
    "dependencies", "acceptance", "evidence", "rollback", "current_status", "next_action",
}


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def audit_register(
    register_path: Path,
    future_audit_path: Path | None = None,
    *,
    require_current_binding: bool = False,
    verify_evidence_paths: bool = False,
) -> dict[str, Any]:
    register_path = register_path.expanduser().resolve()
    register = load_object(register_path)
    problems: list[str] = []
    if register.get("schema") != "methunmix-work-item-register-v1":
        problems.append("unsupported register schema")
    if register.get("product") != "MethUnmix":
        problems.append("register product must be MethUnmix")
    policy = register.get("policy")
    if not isinstance(policy, dict) or policy.get("scientific_status_promotion") != "PROHIBITED_BY_REGISTER":
        problems.append("register must prohibit scientific status promotion")
    items = register.get("work_items")
    if not isinstance(items, list):
        problems.append("work_items must be an array")
        items = []

    ids: list[str] = []
    for row in items:
        if not isinstance(row, dict):
            problems.append("work item must be an object")
            continue
        identifier = row.get("id")
        ids.append(identifier)
        missing = sorted(REQUIRED_FIELDS - set(row))
        if missing:
            problems.append(f"{identifier}: missing fields {', '.join(missing)}")
            continue
        if row.get("priority") not in {"P0", "P1", "P2"}:
            problems.append(f"{identifier}: invalid priority")
        for field in ("target", "rollback", "current_status", "next_action"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                problems.append(f"{identifier}: {field} must be a non-empty string")
        for field in ("current_basis", "allowed_scope", "out_of_scope", "dependencies", "acceptance", "evidence"):
            value = row.get(field)
            if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
                problems.append(f"{identifier}: {field} must be a non-empty list of strings")
        if verify_evidence_paths:
            for evidence in row.get("evidence", []):
                candidate = ROOT / evidence
                if not candidate.is_file():
                    problems.append(f"{identifier}: evidence path does not exist: {evidence}")
    if ids != EXPECTED_IDS:
        problems.append(f"work-item IDs must exactly be {EXPECTED_IDS}")

    binding: dict[str, Any] = {"status": "NOT_REQUESTED"}
    if future_audit_path is not None:
        future = load_object(future_audit_path.expanduser().resolve())
        statuses = {
            row.get("id"): row.get("status")
            for row in future.get("work_packages", []) if isinstance(row, dict)
        }
        mismatches = [
            {"id": row.get("id"), "register_status": row.get("current_status"), "future_audit_status": statuses.get(row.get("id"))}
            for row in items if isinstance(row, dict) and statuses.get(row.get("id")) != row.get("current_status")
        ]
        binding = {
            "status": "CURRENT_BINDING_MATCH" if not mismatches else "CURRENT_BINDING_MISMATCH",
            "future_audit": str(future_audit_path),
            "candidate_source_commit": future.get("current_candidate_source_commit"),
            "qualification_status": (
                future.get("implementation_qualification", {}).get("status")
                if isinstance(future.get("implementation_qualification"), dict) else None
            ),
            "mismatches": mismatches,
        }
        if require_current_binding and mismatches:
            problems.append("current future-work audit status does not match the register")
        if require_current_binding and binding["qualification_status"] != "CURRENT_SOURCE_LOCALLY_QUALIFIED":
            problems.append("future-work audit is not bound to a locally qualified current candidate")

    return {
        "schema": "methunmix-work-item-register-audit-v1",
        "status": "PASS_PLANNING_REGISTER_NO_PROMOTION" if not problems else "FAIL",
        "register": str(register_path),
        "work_item_count": len(items),
        "binding": binding,
        "problems": problems,
        "scope": "planning/change-control validation only; no scientific or release gate promotion",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", type=Path, default=ROOT / "docs/WORK_ITEM_REGISTER.json")
    parser.add_argument("--future-audit", type=Path)
    parser.add_argument("--require-current-binding", action="store_true")
    parser.add_argument(
        "--verify-evidence-paths", action="store_true",
        help="require repository-local evidence paths; use only in a release checkout, not an extracted public source archive",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_register(
        args.register,
        args.future_audit,
        require_current_binding=args.require_current_binding,
        verify_evidence_paths=args.verify_evidence_paths,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["status"].startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
