#!/usr/bin/env python3
"""Audit reviewed lifecycle declarations before constructing a registry.

This is deliberately a read-only readiness check.  It establishes whether a
declaration names real, regular construction/QC/engineering/scientific files
and has the scope required to bind them to an exact catalog reference.  It
does not calculate a lifecycle registry, does not modify a catalog, and never
changes READY/RU/QUARANTINED state.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from demethflow_core.asset_manager import load_catalog
from demethflow_core.errors import DeMethFlowError
from demethflow_core.util import strict_json_loads


DECLARATION_SCHEMA = "methunmix-reference-lifecycle-declarations-v1"
READINESS_SCHEMA = "methunmix-reference-lifecycle-declaration-readiness-v1"
SCOPE_FIELDS = ("scenario", "source_platform", "analysis_contract", "genome_build")
SCIENTIFIC_PATH_FIELDS = (
    "evidence_path",
    "input_path",
    "truth_path",
    "sample_axis_path",
    "cell_type_axis_path",
    "metric_definitions_path",
)


def _load_declarations(path: Path) -> dict[str, Any]:
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeMethFlowError(f"Cannot read lifecycle declarations {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DeMethFlowError("Lifecycle declarations must be a JSON object")
    if payload.get("schema") != DECLARATION_SCHEMA or payload.get("product") != "MethUnmix":
        raise DeMethFlowError("Unsupported lifecycle declaration schema or product")
    return payload


def _regular_file(value: Any, *, root: Path, field: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return f"{field} must name a non-empty existing regular file"
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file() or path.is_symlink():
        return f"{field} is missing, not a regular file, or symlinked: {path}"
    return None


def _text(value: Any, field: str) -> str | None:
    return None if isinstance(value, str) and value.strip() else f"{field} must be a non-empty string"


def _scope_problems(scope: Any, target: dict[str, Any], *, scientific: bool) -> list[str]:
    if not isinstance(scope, dict):
        return ["scope must be an object"]
    problems: list[str] = []
    for field in SCOPE_FIELDS:
        value = scope.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"scope.{field} must be a non-empty string")
        elif value != target.get(field):
            problems.append(f"scope.{field}={value!r} does not match catalog {target.get(field)!r}")
    for field in ("route",) + (("tool", "execution_profile") if scientific else ()):
        issue = _text(scope.get(field), f"scope.{field}")
        if issue:
            problems.append(issue)
    return problems


def _evidence_problems(
    entries: Any,
    *,
    root: Path,
    target: dict[str, Any],
    field: str,
    required: bool,
    scientific: bool,
) -> tuple[list[str], int]:
    if not isinstance(entries, list):
        return [f"{field} must be a list"], 0
    if required and not entries:
        return [f"{field} must contain at least one reviewed record"], 0
    problems: list[str] = []
    for index, entry in enumerate(entries):
        prefix = f"{field}[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{prefix} must be an object")
            continue
        for key in ("evidence_id", "kind"):
            issue = _text(entry.get(key), f"{prefix}.{key}")
            if issue:
                problems.append(issue)
        path_fields = SCIENTIFIC_PATH_FIELDS if scientific else ("evidence_path",)
        for key in path_fields:
            issue = _regular_file(entry.get(key), root=root, field=f"{prefix}.{key}")
            if issue:
                problems.append(issue)
        problems.extend(f"{prefix}.{problem}" for problem in _scope_problems(
            entry.get("scope"), target, scientific=scientific
        ))
        if scientific:
            for key in ("acceptance_policy", "validation_scope"):
                issue = _text(entry.get(key), f"{prefix}.{key}")
                if issue:
                    problems.append(issue)
            if not isinstance(entry.get("metrics"), dict) or not entry["metrics"]:
                problems.append(f"{prefix}.metrics must be a non-empty object")
    return problems, len(entries)


def _record_readiness(record: dict[str, Any] | None, target: dict[str, Any], root: Path) -> dict[str, Any]:
    selector = str(target["selector"])
    if record is None:
        return {
            "selector": selector,
            "status": "MISSING_DECLARATION",
            "blockers": ["no reviewed declaration exists for this catalog selector"],
            "scientific_evidence": "NOT_DECLARED_NO_PROMOTION",
        }
    blockers: list[str] = []
    construction = record.get("construction")
    if not isinstance(construction, dict):
        blockers.append("construction must be an object")
    else:
        paths = construction.get("input_paths")
        if not isinstance(paths, list) or not paths:
            blockers.append("construction.input_paths must be a non-empty list")
        else:
            for index, value in enumerate(paths):
                issue = _regular_file(value, root=root, field=f"construction.input_paths[{index}]")
                if issue:
                    blockers.append(issue)
        pipeline = construction.get("pipeline")
        if not isinstance(pipeline, dict):
            blockers.append("construction.pipeline must be an object")
        else:
            for key in ("identifier", "version"):
                issue = _text(pipeline.get(key), f"construction.pipeline.{key}")
                if issue:
                    blockers.append(issue)
            issue = _regular_file(pipeline.get("path"), root=root, field="construction.pipeline.path")
            if issue:
                blockers.append(issue)
    qc_problems, _qc_count = _evidence_problems(
        record.get("quality_control"), root=root, target=target,
        field="quality_control", required=True, scientific=False,
    )
    engineering_problems, _engineering_count = _evidence_problems(
        record.get("engineering_evidence"), root=root, target=target,
        field="engineering_evidence", required=True, scientific=False,
    )
    science = record.get("scientific_evidence", [])
    scientific_problems, scientific_count = _evidence_problems(
        science, root=root, target=target,
        field="scientific_evidence", required=False, scientific=True,
    )
    blockers.extend(qc_problems)
    blockers.extend(engineering_problems)
    blockers.extend(scientific_problems)
    scientific_status = "NOT_DECLARED_NO_PROMOTION" if scientific_count == 0 else "DECLARED_STRUCTURALLY_READY"
    return {
        "selector": selector,
        "status": "READY_TO_BUILD" if not blockers else "INCOMPLETE",
        "blockers": blockers,
        "scientific_evidence": scientific_status,
        "scientific_evidence_record_count": scientific_count,
    }


def audit_declarations(declarations_path: Path, catalog_path: Path) -> dict[str, Any]:
    declarations_path = declarations_path.expanduser().resolve()
    catalog_path = catalog_path.expanduser().resolve()
    catalog = load_catalog(catalog_path)
    try:
        declarations = _load_declarations(declarations_path)
    except DeMethFlowError as exc:
        return {
            "schema": READINESS_SCHEMA,
            "status": "INVALID_DECLARATIONS_NO_REGISTRY",
            "registry_build_permitted": False,
            "scientific_status": "NOT_EVALUATED_NO_PROMOTION",
            "error": str(exc),
        }
    targets = {
        str(item.get("selector")): item
        for item in catalog.get("targets", [])
        if isinstance(item, dict) and item.get("module_type") == "reference" and isinstance(item.get("selector"), str)
    }
    raw_records = declarations.get("references")
    top_level_problems: list[str] = []
    if declarations.get("catalog_binding") != {
        "catalog_release": catalog.get("catalog_release"),
        "catalog_sequence": catalog.get("catalog_sequence"),
    }:
        top_level_problems.append("catalog_binding does not exactly match the selected catalog")
    coverage = declarations.get("coverage")
    if coverage not in {"COMPLETE_CATALOG", "DECLARED_SUBSET"}:
        top_level_problems.append("coverage must be COMPLETE_CATALOG or DECLARED_SUBSET")
    records: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_records, list):
        top_level_problems.append("references must be a list")
    else:
        for index, record in enumerate(raw_records):
            if not isinstance(record, dict) or not isinstance(record.get("selector"), str):
                top_level_problems.append(f"references[{index}] must be an object with selector")
                continue
            selector = record["selector"]
            if selector in records:
                top_level_problems.append(f"duplicate declaration selector: {selector}")
            else:
                records[selector] = record
    unknown = sorted(set(records) - set(targets))
    if unknown:
        top_level_problems.extend(f"declaration selector is absent from catalog: {selector}" for selector in unknown)
    rows = [_record_readiness(records.get(selector), target, declarations_path.parent)
            for selector, target in sorted(targets.items())]
    if coverage == "DECLARED_SUBSET":
        rows = [row for row in rows if row["selector"] in records]
    complete_coverage = coverage == "COMPLETE_CATALOG" and set(records) == set(targets)
    all_ready = bool(rows) and all(row["status"] == "READY_TO_BUILD" for row in rows)
    permitted = not top_level_problems and all_ready and (coverage == "DECLARED_SUBSET" or complete_coverage)
    if permitted and complete_coverage:
        status = "READY_TO_BUILD_COMPLETE_CATALOG_NO_PROMOTION"
    elif permitted:
        status = "READY_TO_BUILD_DECLARED_SUBSET_NO_PROMOTION"
    else:
        status = "INCOMPLETE_DECLARATIONS_NO_REGISTRY"
    return {
        "schema": READINESS_SCHEMA,
        "status": status,
        "scope": "declaration path/readiness audit only; no registry, catalog, execution, or scientific status mutation",
        "scientific_status": "NOT_EVALUATED_NO_PROMOTION",
        "catalog_binding": {
            "catalog_release": catalog.get("catalog_release"),
            "catalog_sequence": catalog.get("catalog_sequence"),
        },
        "coverage": coverage,
        "catalog_reference_count": len(targets),
        "declared_reference_count": len(records),
        "registry_build_permitted": permitted,
        "complete_catalog_registry_build_permitted": permitted and complete_coverage,
        "top_level_problems": top_level_problems,
        "references": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declarations", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_declarations(args.declarations, args.catalog)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["registry_build_permitted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
