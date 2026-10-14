"""Immutable reference lifecycle and evidence-registry validation.

The static asset catalog answers a deliberately narrow question: whether an
exact archive can be obtained safely.  It must not be overloaded with claims
about how a reference was constructed or scientifically evaluated.  This
module defines the complementary, offline lifecycle registry used to bind
those claims to an immutable selector and content digest.

Loading a registry never promotes a reference's READY/RU state.  It only
reports whether the supplied lifecycle assertions are structurally complete
and match the selected catalog snapshot.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .asset_manager import FULL_SELECTOR, load_catalog
from .construction_record import validate_construction_record
from .errors import DeMethFlowError
from .util import strict_json_loads


LIFECYCLE_REGISTRY_SCHEMA = "methunmix-reference-lifecycle-v1"
LIFECYCLE_AUDIT_SCHEMA = "methunmix-reference-lifecycle-audit-v1"
LIFECYCLE_DECLARATION_PLAN_SCHEMA = "methunmix-reference-lifecycle-declaration-plan-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LIFECYCLE_STATES = {"ACTIVE", "SUPERSEDED", "REVOKED"}
_LIFECYCLE_STAGES = {
    "CONSTRUCTION_DECLARED", "ENGINEERING_CHECKED", "LOCALLY_REGISTERED",
    "RELEASE_PREPARED", "PUBLIC_RELEASED", "SUPERSEDED", "REVOKED", "LEGACY_UNSPECIFIED",
}
_ENGINEERING_KINDS = {"TECHNICAL_SMOKE", "STRUCTURAL_OUTPUT"}
_QC_KINDS = {"STRUCTURAL_OUTPUT", "COORDINATE_QC", "CELL_TYPE_AXIS_QC", "MARKER_MODEL_QC"}
_SCIENTIFIC_KINDS = {
    "FIXED_SIMULATED_TRUTH",
    "INDEPENDENT_SOURCE",
    "REAL_INDEPENDENT_COHORT",
}


def reference_lifecycle_declaration_plan(catalog_path: Path | None = None) -> dict[str, Any]:
    """List the exact evidence a maintainer must bind before publishing a registry.

    This intentionally produces a *plan*, not a declaration or a lifecycle
    registry.  It must therefore never be mistaken for evidence that a
    reference was constructed, QCed, or scientifically validated.  The plan
    is useful when a catalog has public archives but no trustworthy local
    construction/evidence-path inventory from which a registry can be built.
    """
    catalog = load_catalog(catalog_path)
    references = [
        target for target in catalog.get("targets", [])
        if isinstance(target, dict) and target.get("module_type") == "reference"
    ]
    rows: list[dict[str, Any]] = []
    for target in sorted(references, key=lambda item: str(item.get("selector", ""))):
        selector = target.get("selector")
        if not isinstance(selector, str) or not FULL_SELECTOR.fullmatch(selector):
            # Catalog validation rejects this before normal use.  Keep this
            # function fail-closed if it is called with a malformed catalog.
            raise DeMethFlowError(f"Catalog reference selector is not exact: {selector!r}")
        scope = {
            key: target.get(key)
            for key in ("scenario", "source_platform", "analysis_contract", "genome_build")
        }
        rows.append({
            "selector": selector,
            "reference_id": target.get("reference_id"),
            "version": target.get("version"),
            "content_digest": target.get("content_digest"),
            "archive_sha256": target.get("sha256"),
            "scope": scope,
            "required_declaration_fields": {
                "construction": [
                    "input_paths[]", "pipeline.identifier", "pipeline.version", "pipeline.path",
                ],
                "quality_control": [
                    "evidence_id", "kind", "evidence_path", "scope including route",
                ],
                "engineering_evidence": [
                    "evidence_id", "kind", "evidence_path", "scope including route",
                ],
                "scientific_evidence_if_declared": [
                    "evidence_id", "kind", "evidence_path", "input_path", "truth_path",
                    "sample_axis_path", "cell_type_axis_path", "metric_definitions_path",
                    "metrics", "acceptance_policy", "validation_scope",
                    "scope including route, tool and execution_profile",
                ],
            },
            "scientific_evidence_policy": (
                "optional for registry construction; an empty list remains "
                "NOT_DECLARED_NO_PROMOTION and must not be filled with inferred values"
            ),
        })
    return {
        "schema": LIFECYCLE_DECLARATION_PLAN_SCHEMA,
        "product": "MethUnmix",
        "status": "DECLARATIONS_REQUIRED_NO_STATUS_PROMOTION",
        "scope": (
            "catalog-derived lifecycle declaration requirements only; this is not a lifecycle registry, "
            "construction/QC/engineering/scientific evidence, or a READY/RU promotion"
        ),
        "catalog_binding": {
            "catalog_release": catalog["catalog_release"],
            "catalog_sequence": catalog["catalog_sequence"],
        },
        "reference_count": len(rows),
        "references": rows,
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeMethFlowError(f"Cannot read reference lifecycle registry {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DeMethFlowError("Reference lifecycle registry must be a JSON object")
    return payload


def _required_text(value: Any, *, field: str, selector: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        suffix = f" for {selector}" if selector else ""
        raise DeMethFlowError(f"Reference lifecycle registry requires non-empty {field}{suffix}")
    return value.strip()


def _required_digest(value: Any, *, field: str, selector: str | None = None) -> str:
    digest = _required_text(value, field=field, selector=selector).lower()
    if not _SHA256.fullmatch(digest):
        suffix = f" for {selector}" if selector else ""
        raise DeMethFlowError(f"Reference lifecycle registry requires SHA256 {field}{suffix}")
    return digest


def _validate_evidence(
    entries: Any,
    *,
    selector: str,
    content_digest: str,
    field: str,
    allowed_kinds: set[str],
    require_nonempty: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise DeMethFlowError(f"Reference lifecycle {field} must be a list for {selector}")
    if require_nonempty and not entries:
        raise DeMethFlowError(f"Reference lifecycle {field} must be non-empty for {selector}")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise DeMethFlowError(f"Reference lifecycle {field} entry must be an object for {selector}")
        evidence_id = _required_text(entry.get("evidence_id"), field=f"{field}.evidence_id", selector=selector)
        if evidence_id in seen:
            raise DeMethFlowError(f"Duplicate {field} evidence_id for {selector}: {evidence_id}")
        seen.add(evidence_id)
        kind = _required_text(entry.get("kind"), field=f"{field}.kind", selector=selector)
        if kind not in allowed_kinds:
            raise DeMethFlowError(f"Unsupported {field} evidence kind for {selector}: {kind}")
        if _required_digest(entry.get("reference_content_digest"), field=f"{field}.reference_content_digest", selector=selector) != content_digest:
            raise DeMethFlowError(f"{field} evidence does not bind the declared reference content digest for {selector}")
        _required_digest(entry.get("evidence_digest"), field=f"{field}.evidence_digest", selector=selector)
        scope = entry.get("scope")
        if not isinstance(scope, dict):
            raise DeMethFlowError(f"Reference lifecycle {field} evidence requires object scope for {selector}")
        for scope_field in ("scenario", "source_platform", "analysis_contract", "genome_build", "route"):
            _required_text(scope.get(scope_field), field=f"{field}.scope.{scope_field}", selector=selector)
        if kind in _SCIENTIFIC_KINDS:
            _required_digest(entry.get("input_digest"), field=f"{field}.input_digest", selector=selector)
            _required_digest(entry.get("truth_digest"), field=f"{field}.truth_digest", selector=selector)
            _required_text(entry.get("acceptance_policy"), field=f"{field}.acceptance_policy", selector=selector)
            _required_text(entry.get("validation_scope"), field=f"{field}.validation_scope", selector=selector)
            _required_digest(entry.get("sample_axis_digest"), field=f"{field}.sample_axis_digest", selector=selector)
            _required_digest(entry.get("cell_type_axis_digest"), field=f"{field}.cell_type_axis_digest", selector=selector)
            _required_digest(entry.get("metric_definitions_digest"), field=f"{field}.metric_definitions_digest", selector=selector)
            if not isinstance(entry.get("metrics"), dict) or not entry["metrics"]:
                raise DeMethFlowError(f"Reference lifecycle {field} evidence requires non-empty metrics for {selector}")
            _required_text(scope.get("tool"), field=f"{field}.scope.tool", selector=selector)
            _required_text(scope.get("execution_profile"), field=f"{field}.scope.execution_profile", selector=selector)
        validated.append(entry)
    return validated


def load_reference_lifecycle_registry(path: Path) -> dict[str, Any]:
    """Load and schema-validate a lifecycle registry without catalog binding."""
    payload = _load_json(path)
    if payload.get("schema") != LIFECYCLE_REGISTRY_SCHEMA:
        raise DeMethFlowError(f"Unsupported reference lifecycle schema: {payload.get('schema')!r}")
    if payload.get("product") != "MethUnmix":
        raise DeMethFlowError("Reference lifecycle registry product must be MethUnmix")
    _required_text(payload.get("registry_release"), field="registry_release")
    catalog = payload.get("catalog_binding")
    if not isinstance(catalog, dict):
        raise DeMethFlowError("Reference lifecycle registry requires catalog_binding")
    _required_text(catalog.get("catalog_release"), field="catalog_binding.catalog_release")
    sequence = catalog.get("catalog_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise DeMethFlowError("Reference lifecycle catalog_binding.catalog_sequence must be a positive integer")
    if payload.get("coverage") not in {"COMPLETE_CATALOG", "DECLARED_SUBSET"}:
        raise DeMethFlowError("Reference lifecycle coverage must be COMPLETE_CATALOG or DECLARED_SUBSET")
    records = payload.get("references")
    if not isinstance(records, list):
        raise DeMethFlowError("Reference lifecycle registry references must be a list")

    seen_selectors: set[str] = set()
    replacements: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            raise DeMethFlowError("Reference lifecycle record must be an object")
        selector = _required_text(record.get("selector"), field="selector")
        if not FULL_SELECTOR.fullmatch(selector):
            raise DeMethFlowError(f"Reference lifecycle selector is not exact: {selector!r}")
        if selector in seen_selectors:
            raise DeMethFlowError(f"Duplicate reference lifecycle selector: {selector}")
        seen_selectors.add(selector)
        reference_id, version = selector.rsplit("@", 1)
        if _required_text(record.get("reference_id"), field="reference_id", selector=selector) != reference_id:
            raise DeMethFlowError(f"Reference lifecycle reference_id does not match selector for {selector}")
        if _required_text(record.get("version"), field="version", selector=selector) != version:
            raise DeMethFlowError(f"Reference lifecycle version does not match selector for {selector}")
        content_digest = _required_digest(record.get("content_digest"), field="content_digest", selector=selector)
        _required_digest(record.get("archive_sha256"), field="archive_sha256", selector=selector)
        state = _required_text(record.get("lifecycle_state"), field="lifecycle_state", selector=selector)
        if state not in _LIFECYCLE_STATES:
            raise DeMethFlowError(f"Unsupported lifecycle_state for {selector}: {state}")
        stage = record.get("lifecycle_stage", "LEGACY_UNSPECIFIED")
        if stage not in _LIFECYCLE_STAGES:
            raise DeMethFlowError(f"Unsupported lifecycle_stage for {selector}: {stage}")
        construction = record.get("construction")
        if not isinstance(construction, dict):
            raise DeMethFlowError(f"Reference lifecycle construction must be an object for {selector}")
        inputs = construction.get("input_digests")
        if not isinstance(inputs, list) or not inputs:
            raise DeMethFlowError(f"Reference lifecycle construction requires non-empty input_digests for {selector}")
        for index, digest in enumerate(inputs):
            _required_digest(digest, field=f"construction.input_digests[{index}]", selector=selector)
        pipeline = construction.get("pipeline")
        if not isinstance(pipeline, dict):
            raise DeMethFlowError(f"Reference lifecycle construction requires pipeline object for {selector}")
        for key in ("identifier", "version", "digest"):
            if key == "digest":
                _required_digest(pipeline.get(key), field=f"construction.pipeline.{key}", selector=selector)
            else:
                _required_text(pipeline.get(key), field=f"construction.pipeline.{key}", selector=selector)
        # Rich construction records are optional only for legacy historical
        # entries.  If supplied, they are strict and bind the exact immutable
        # content digest without creating a circular dependency on later QC or
        # scientific evidence.
        construction_record = construction.get("record")
        if construction_record is not None:
            validated_record = validate_construction_record(construction_record)
            if validated_record["reference_content_digest"] != content_digest:
                raise DeMethFlowError(
                    f"construction.record does not bind the declared reference content digest for {selector}"
                )
            construction["record"] = validated_record
        _validate_evidence(record.get("quality_control"), selector=selector, content_digest=content_digest,
                           field="quality_control", allowed_kinds=_QC_KINDS, require_nonempty=True)
        _validate_evidence(record.get("engineering_evidence"), selector=selector, content_digest=content_digest,
                           field="engineering_evidence", allowed_kinds=_ENGINEERING_KINDS, require_nonempty=True)
        _validate_evidence(record.get("scientific_evidence"), selector=selector, content_digest=content_digest,
                           field="scientific_evidence", allowed_kinds=_SCIENTIFIC_KINDS)
        replacement = record.get("replacement_selector")
        if replacement is not None:
            if not isinstance(replacement, str) or not FULL_SELECTOR.fullmatch(replacement) or replacement == selector:
                raise DeMethFlowError(f"Invalid replacement_selector for {selector}")
            replacements[selector] = replacement
        if state in {"REVOKED", "SUPERSEDED"} and replacement is None:
            raise DeMethFlowError(f"{state} reference requires replacement_selector for {selector}")
        if state == "REVOKED":
            _required_text(record.get("revocation_reason"), field="revocation_reason", selector=selector)
        elif record.get("revocation_reason") is not None:
            raise DeMethFlowError(f"Only REVOKED reference may declare revocation_reason for {selector}")
    # Replacement means a new immutable object, never an equivalence claim.
    # Catch loops here even when records are audited against a subset catalog.
    for origin in replacements:
        visited: set[str] = set()
        current = origin
        while current in replacements:
            if current in visited:
                raise DeMethFlowError(f"Reference lifecycle replacement cycle includes {current}")
            visited.add(current)
            current = replacements[current]
    return payload


def audit_reference_lifecycle_registry(registry_path: Path, *, catalog_path: Path | None = None) -> dict[str, Any]:
    """Audit immutable lifecycle records against one exact catalog snapshot."""
    registry = load_reference_lifecycle_registry(registry_path)
    catalog = load_catalog(catalog_path)
    binding = registry["catalog_binding"]
    problems: list[dict[str, str]] = []
    if binding["catalog_release"] != catalog["catalog_release"]:
        problems.append({"code": "CATALOG_RELEASE_MISMATCH", "message": "Registry catalog_release does not match selected catalog"})
    if binding["catalog_sequence"] != catalog["catalog_sequence"]:
        problems.append({"code": "CATALOG_SEQUENCE_MISMATCH", "message": "Registry catalog_sequence does not match selected catalog"})

    targets = {
        str(target.get("selector")): target
        for target in catalog.get("targets", [])
        if isinstance(target, dict) and target.get("module_type") == "reference" and isinstance(target.get("selector"), str)
    }
    records = {str(record["selector"]): record for record in registry["references"]}
    if registry["coverage"] == "COMPLETE_CATALOG":
        for selector in sorted(set(targets) - set(records)):
            problems.append({"code": "MISSING_LIFECYCLE_RECORD", "selector": selector,
                             "message": "Catalog reference has no lifecycle record"})
    for selector in sorted(set(records) - set(targets)):
        problems.append({"code": "UNKNOWN_SELECTOR", "selector": selector,
                         "message": "Lifecycle record selector is absent from selected catalog"})

    evidence_summary: dict[str, int] = {kind: 0 for kind in sorted(_SCIENTIFIC_KINDS)}
    rows: list[dict[str, Any]] = []
    for selector, record in sorted(records.items()):
        target = targets.get(selector)
        if target is None:
            continue
        for field in ("reference_id", "version", "content_digest", "archive_sha256"):
            catalog_field = "sha256" if field == "archive_sha256" else field
            if target.get(catalog_field) != record.get(field):
                problems.append({"code": "IMMUTABLE_IDENTITY_MISMATCH", "selector": selector,
                                 "message": f"Catalog {catalog_field} does not match lifecycle record {field}"})
        for evidence_field in ("quality_control", "engineering_evidence", "scientific_evidence"):
            for evidence in record[evidence_field]:
                scope = evidence["scope"]
                for scope_field, catalog_field in (
                    ("scenario", "scenario"),
                    ("source_platform", "source_platform"),
                    ("analysis_contract", "analysis_contract"),
                    ("genome_build", "genome_build"),
                ):
                    if scope[scope_field] != target.get(catalog_field):
                        problems.append({"code": "EVIDENCE_SCOPE_MISMATCH", "selector": selector,
                                         "message": f"{evidence_field} scope.{scope_field} differs from catalog"})
        state = record["lifecycle_state"]
        target_revoked = bool(target.get("revoked", False))
        if state == "REVOKED" and not target_revoked:
            problems.append({"code": "REVOCATION_NOT_PUBLISHED", "selector": selector,
                             "message": "Lifecycle marks reference REVOKED but catalog does not"})
        if target_revoked and state != "REVOKED":
            problems.append({"code": "CATALOG_REVOCATION_UNACKNOWLEDGED", "selector": selector,
                             "message": "Catalog revokes reference but lifecycle state is not REVOKED"})
        replacement = record.get("replacement_selector")
        if replacement:
            replacement_target = targets.get(replacement)
            if replacement_target is None:
                problems.append({"code": "REPLACEMENT_NOT_IN_CATALOG", "selector": selector,
                                 "message": "replacement_selector is absent from selected catalog"})
            elif replacement_target.get("revoked"):
                problems.append({"code": "REPLACEMENT_REVOKED", "selector": selector,
                                 "message": "replacement_selector is itself revoked"})
            else:
                for field in ("reference_id", "scenario", "source_platform", "analysis_contract", "genome_build"):
                    if replacement_target.get(field) != target.get(field):
                        problems.append({"code": "REPLACEMENT_SCOPE_MISMATCH", "selector": selector,
                                         "message": f"replacement_selector changes immutable {field}"})
        scientific = record["scientific_evidence"]
        for evidence in scientific:
            evidence_summary[evidence["kind"]] += 1
        rows.append({
            "selector": selector,
            "lifecycle_state": state,
            "lifecycle_stage": record.get("lifecycle_stage", "LEGACY_UNSPECIFIED"),
            "replacement_selector": replacement,
            "quality_control_evidence": len(record["quality_control"]),
            "engineering_evidence": len(record["engineering_evidence"]),
            "scientific_evidence": len(scientific),
            "scientific_evidence_status": "DECLARED" if scientific else "NOT_DECLARED_NO_PROMOTION",
            "construction_record_status": (
                "BOUND_EXPLICIT_RECORD" if isinstance(record.get("construction"), dict)
                and isinstance(record["construction"].get("record"), dict)
                else "LEGACY_INCOMPLETE_NO_INFERENCE"
            ),
        })
    return {
        "schema": LIFECYCLE_AUDIT_SCHEMA,
        "product": "MethUnmix",
        "registry_release": registry["registry_release"],
        "catalog_release": catalog["catalog_release"],
        "catalog_sequence": catalog["catalog_sequence"],
        "coverage": registry["coverage"],
        "reference_count": len(rows),
        "scientific_evidence_by_kind": evidence_summary,
        "references": rows,
        "problems": problems,
        "ok": not problems,
        "status": "PASS_LIFECYCLE_BINDING_NO_STATUS_PROMOTION" if not problems else "FAIL_LIFECYCLE_BINDING",
    }
