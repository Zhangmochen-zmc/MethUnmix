"""Strict, non-promoting construction-provenance records.

Construction provenance is intentionally a separate immutable object.  Its
identity excludes only its self-declared digest, so attaching later QC or
scientific evidence never requires rewriting the construction fact or the
reference content digest it describes.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .errors import DeMethFlowError


SCHEMA = "methunmix-construction-record-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeMethFlowError(f"CONSTRUCTION_RECORD_INVALID: {field} must be a non-empty string")
    return value.strip()


def _sha(value: object, field: str) -> str:
    value = _text(value, field).lower()
    if not _SHA256.fullmatch(value):
        raise DeMethFlowError(f"CONSTRUCTION_RECORD_INVALID: {field} must be a SHA256")
    return value


def construction_record_identity(record: dict[str, Any]) -> str:
    """Return the semantic construction identity, excluding self digest only."""
    return _digest({key: value for key, value in record.items() if key != "construction_record_sha256"})


def validate_construction_record(raw: object) -> dict[str, Any]:
    """Validate one explicit construction fact without reading or executing data.

    The consumer deliberately validates declared identities and axes only.  It
    never unpickles a model, executes a pipeline, or infers absent historical
    information from an archive.
    """
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: unsupported schema")
    record = dict(raw)
    record["record_id"] = _text(record.get("record_id"), "record_id")
    source = record.get("record_source")
    if not isinstance(source, dict):
        raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: record_source must be an object")
    record["record_source"] = {
        "kind": _text(source.get("kind"), "record_source.kind"),
        "schema_version": _text(source.get("schema_version"), "record_source.schema_version"),
    }
    record["reference_content_digest"] = _sha(record.get("reference_content_digest"), "reference_content_digest")
    inputs = record.get("construction_inputs")
    if not isinstance(inputs, list) or not inputs:
        raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: construction_inputs must be a non-empty list")
    normalized_inputs = []
    seen = set()
    for index, item in enumerate(inputs):
        if not isinstance(item, dict):
            raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: construction_inputs entries must be objects")
        identifier = _text(item.get("identifier"), f"construction_inputs[{index}].identifier")
        if identifier in seen:
            raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: duplicate construction input identifier")
        seen.add(identifier)
        normalized_inputs.append({"identifier": identifier, "sha256": _sha(item.get("sha256"), f"construction_inputs[{index}].sha256")})
    record["construction_inputs"] = normalized_inputs
    pipeline = record.get("pipeline")
    if not isinstance(pipeline, dict):
        raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: pipeline must be an object")
    record["pipeline"] = {
        "identifier": _text(pipeline.get("identifier"), "pipeline.identifier"),
        "version": _text(pipeline.get("version"), "pipeline.version"),
        "sha256": _sha(pipeline.get("sha256"), "pipeline.sha256"),
    }
    if not isinstance(record.get("parameters"), dict):
        raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: parameters must be an object")
    semantic = record.get("semantic_scope")
    if not isinstance(semantic, dict):
        raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: semantic_scope must be an object")
    record["semantic_scope"] = {
        "genome_build": _text(semantic.get("genome_build"), "semantic_scope.genome_build"),
        "coordinate_system": _text(semantic.get("coordinate_system"), "semantic_scope.coordinate_system"),
        "sample_axis_sha256": _sha(semantic.get("sample_axis_sha256"), "semantic_scope.sample_axis_sha256"),
        "cell_type_axis_sha256": _sha(semantic.get("cell_type_axis_sha256"), "semantic_scope.cell_type_axis_sha256"),
    }
    for field in ("marker_sources", "model_or_cache_sources"):
        values = record.get(field, [])
        if not isinstance(values, list):
            raise DeMethFlowError(f"CONSTRUCTION_RECORD_INVALID: {field} must be a list")
        normalized = []
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise DeMethFlowError(f"CONSTRUCTION_RECORD_INVALID: {field}[{index}] must be an object")
            normalized.append({"identifier": _text(item.get("identifier"), f"{field}[{index}].identifier"), "sha256": _sha(item.get("sha256"), f"{field}[{index}].sha256")})
        record[field] = normalized
    limitations = record.get("known_limitations", [])
    if not isinstance(limitations, list) or not all(isinstance(item, str) and item.strip() for item in limitations):
        raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: known_limitations must be a list of non-empty strings")
    record["known_limitations"] = [item.strip() for item in limitations]
    expected = construction_record_identity(record)
    claimed = record.get("construction_record_sha256")
    if claimed is not None and _sha(claimed, "construction_record_sha256") != expected:
        raise DeMethFlowError("CONSTRUCTION_RECORD_INVALID: construction_record_sha256 does not match record content")
    record["construction_record_sha256"] = expected
    return record
