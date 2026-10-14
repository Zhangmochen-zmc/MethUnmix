#!/usr/bin/env python3
"""Build a digest-bound lifecycle registry from reviewed local declarations.

The declaration deliberately contains local evidence paths; the emitted
registry contains only immutable digests and human-readable scope metadata.
This keeps host paths out of a release record while avoiding hand-calculated
SHA256 values.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from demethflow_core.asset_manager import load_catalog
from demethflow_core.errors import DeMethFlowError
from demethflow_core.reference_lifecycle import audit_reference_lifecycle_registry
from demethflow_core.util import atomic_write_json, sha256_file, strict_json_loads


DECLARATION_SCHEMA = "methunmix-reference-lifecycle-declarations-v1"


def _read(path: Path) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeMethFlowError(f"Cannot read lifecycle declarations {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DeMethFlowError("Lifecycle declarations must be a JSON object")
    if value.get("schema") != DECLARATION_SCHEMA or value.get("product") != "MethUnmix":
        raise DeMethFlowError("Unsupported lifecycle declaration schema or product")
    return value


def _path_digest(value: Any, *, root: Path, field: str, selector: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeMethFlowError(f"Lifecycle declaration requires non-empty {field} path for {selector}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise DeMethFlowError(f"Lifecycle declaration path is not a regular file for {selector}: {field}={path}")
    return sha256_file(path)


def _evidence(
    entries: Any,
    *,
    root: Path,
    selector: str,
    content_digest: str,
    scientific: bool,
) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise DeMethFlowError(f"Lifecycle declaration evidence must be a list for {selector}")
    output: list[dict[str, Any]] = []
    for source in entries:
        if not isinstance(source, dict):
            raise DeMethFlowError(f"Lifecycle declaration evidence entry must be an object for {selector}")
        item = copy.deepcopy(source)
        item["evidence_digest"] = _path_digest(
            item.pop("evidence_path", None), root=root, field="evidence_path", selector=selector
        )
        item["reference_content_digest"] = content_digest
        if scientific:
            for path_key, digest_key in (
                ("input_path", "input_digest"),
                ("truth_path", "truth_digest"),
                ("sample_axis_path", "sample_axis_digest"),
                ("cell_type_axis_path", "cell_type_axis_digest"),
                ("metric_definitions_path", "metric_definitions_digest"),
            ):
                item[digest_key] = _path_digest(
                    item.pop(path_key, None), root=root, field=path_key, selector=selector
                )
        output.append(item)
    return output


def build_registry(declarations_path: Path, catalog_path: Path) -> dict[str, Any]:
    declarations_path = declarations_path.expanduser().resolve()
    declarations = _read(declarations_path)
    catalog = load_catalog(catalog_path)
    if declarations.get("catalog_binding") != {
        "catalog_release": catalog["catalog_release"],
        "catalog_sequence": catalog["catalog_sequence"],
    }:
        raise DeMethFlowError("Lifecycle declaration catalog_binding must exactly match selected catalog")
    references = declarations.get("references")
    if not isinstance(references, list):
        raise DeMethFlowError("Lifecycle declarations references must be a list")
    targets = {
        item.get("selector"): item
        for item in catalog.get("targets", [])
        if isinstance(item, dict) and item.get("module_type") == "reference"
    }
    output: list[dict[str, Any]] = []
    for source in references:
        if not isinstance(source, dict) or not isinstance(source.get("selector"), str):
            raise DeMethFlowError("Lifecycle declaration reference requires selector")
        selector = source["selector"]
        target = targets.get(selector)
        if not isinstance(target, dict):
            raise DeMethFlowError(f"Lifecycle declaration selector is absent from catalog: {selector}")
        construction = source.get("construction")
        if not isinstance(construction, dict):
            raise DeMethFlowError(f"Lifecycle declaration requires construction object for {selector}")
        input_paths = construction.get("input_paths")
        if not isinstance(input_paths, list):
            raise DeMethFlowError(f"Lifecycle declaration construction.input_paths must be a list for {selector}")
        pipeline = construction.get("pipeline")
        if not isinstance(pipeline, dict):
            raise DeMethFlowError(f"Lifecycle declaration construction.pipeline must be an object for {selector}")
        pipeline_out = {"identifier": pipeline.get("identifier"), "version": pipeline.get("version")}
        pipeline_out["digest"] = _path_digest(
            pipeline.get("path"), root=declarations_path.parent,
            field="construction.pipeline.path", selector=selector,
        )
        record = {
            "selector": selector,
            "reference_id": target.get("reference_id"),
            "version": target.get("version"),
            "archive_sha256": target.get("sha256"),
            "content_digest": target.get("content_digest"),
            "lifecycle_state": source.get("lifecycle_state", "ACTIVE"),
            "construction": {
                "input_digests": [
                    _path_digest(path, root=declarations_path.parent,
                                 field="construction.input_paths", selector=selector)
                    for path in input_paths
                ],
                "pipeline": pipeline_out,
            },
            "quality_control": _evidence(source.get("quality_control"), root=declarations_path.parent, selector=selector,
                                            content_digest=str(target.get("content_digest")), scientific=False),
            "engineering_evidence": _evidence(source.get("engineering_evidence"), root=declarations_path.parent, selector=selector,
                                                 content_digest=str(target.get("content_digest")), scientific=False),
            "scientific_evidence": _evidence(source.get("scientific_evidence", []), root=declarations_path.parent, selector=selector,
                                                content_digest=str(target.get("content_digest")), scientific=True),
        }
        for key in ("replacement_selector", "revocation_reason"):
            if key in source:
                record[key] = source[key]
        output.append(record)
    return {
        "schema": "methunmix-reference-lifecycle-v1",
        "product": "MethUnmix",
        "registry_release": declarations.get("registry_release"),
        "catalog_binding": declarations["catalog_binding"],
        "coverage": declarations.get("coverage"),
        "references": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declarations", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        registry = build_registry(args.declarations, args.catalog)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        staging = args.output.with_name(f".{args.output.name}.staging.json")
        atomic_write_json(staging, registry)
        audit = audit_reference_lifecycle_registry(staging, catalog_path=args.catalog)
        if not audit["ok"]:
            raise DeMethFlowError("Generated lifecycle registry failed binding audit: " + json.dumps(audit["problems"], ensure_ascii=False))
        staging.replace(args.output)
        print(json.dumps({"status": "PASS_LIFECYCLE_REGISTRY_GENERATED", "output": str(args.output), "audit": audit}, ensure_ascii=False, indent=2))
        return 0
    except DeMethFlowError as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
