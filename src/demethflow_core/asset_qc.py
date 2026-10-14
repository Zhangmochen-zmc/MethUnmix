"""Safe structural QC for the formats MethUnMix currently declares.

This is deliberately metadata/record QC: it does not execute a model, import
pickle objects, or treat a matching digest as scientific validation.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .errors import DeMethFlowError


SCHEMA = "methunmix-asset-qc-v1"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_CATEGORIES = {"array_cpg", "wgbs_interval", "read_level", "marker", "model_cache", "metadata_cell_axis"}


def _fail(message: str) -> None:
    raise DeMethFlowError(f"ASSET_QC_INVALID: {message}")


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value.lower()):
        _fail(f"{field} must be a SHA256")
    return value.lower()


def _axis(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x.strip() for x in value):
        _fail(f"{field} must be a non-empty list of names")
    values = [x.strip() for x in value]
    if len(values) != len(set(values)):
        _fail(f"{field} contains duplicate names")
    return values


def _finite_matrix(value: object, rows: int, columns: int) -> None:
    if not isinstance(value, list) or len(value) != rows:
        _fail("values row count does not match declared axis")
    for row in value:
        if not isinstance(row, list) or len(row) != columns:
            _fail("values matrix is ragged")
        for item in row:
            if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(float(item)):
                _fail("values matrix contains non-finite data")


def validate_asset_qc_record(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        _fail("unsupported schema")
    category = raw.get("asset_type")
    if category not in _CATEGORIES:
        _fail("unsupported asset_type")
    content_digest = _sha(raw.get("content_digest"), "content_digest")
    data = raw.get("data")
    if not isinstance(data, dict):
        _fail("data must be an object")
    result: dict[str, Any] = {"schema": "methunmix-asset-qc-result-v1", "asset_type": category, "content_digest": content_digest,
                              "structural_qc": "PASS", "engineering_validation": "NOT_EXECUTED", "scientific_validation": "NOT_EXECUTED"}
    if category == "array_cpg":
        probes = _axis(data.get("cpg_ids"), "data.cpg_ids")
        samples = _axis(data.get("sample_axis"), "data.sample_axis")
        _finite_matrix(data.get("values"), len(probes), len(samples))
        result["observed"] = {"cpg_count": len(probes), "sample_count": len(samples), "contract": "array_cpg_matrix"}
    elif category == "wgbs_interval":
        if data.get("genome_build") not in {"hg19", "hg38"} or data.get("coordinate_system") != "bed_0_based_half_open":
            _fail("WGBS intervals require hg19/hg38 and bed_0_based_half_open")
        intervals = data.get("intervals")
        if not isinstance(intervals, list) or not intervals:
            _fail("data.intervals must be a non-empty list")
        seen = set()
        for row in intervals:
            if not isinstance(row, dict) or not isinstance(row.get("chrom"), str): _fail("invalid interval chromosome")
            start, end = row.get("start"), row.get("end")
            if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool) or start < 0 or end <= start:
                _fail("invalid half-open interval")
            key = (row["chrom"], start, end)
            if key in seen: _fail("duplicate interval")
            seen.add(key)
        result["observed"] = {"interval_count": len(intervals), "coordinate_system": data["coordinate_system"], "genome_build": data["genome_build"]}
    elif category == "read_level":
        if data.get("format") != "native_cpg_bed": _fail("unsupported read-level format")
        records = data.get("records")
        if not isinstance(records, list) or not records: _fail("data.records must be a non-empty list")
        for row in records:
            if not isinstance(row, dict) or not isinstance(row.get("chrom"), str): _fail("invalid read-level chromosome")
            if not isinstance(row.get("start"), int) or row["start"] < 0 or row.get("end") != row["start"] + 1: _fail("read-level record is not one CpG interval")
            methylated, depth = row.get("methylated"), row.get("depth")
            if not isinstance(methylated, int) or not isinstance(depth, int) or methylated < 0 or depth < methylated: _fail("invalid read-level counts")
        result["observed"] = {"record_count": len(records), "format": "native_cpg_bed"}
    elif category == "marker":
        source = _sha(data.get("source_sha256"), "data.source_sha256")
        markers = _axis(data.get("marker_ids"), "data.marker_ids")
        target = _sha(data.get("target_content_digest"), "data.target_content_digest")
        result["observed"] = {"marker_count": len(markers), "source_sha256": source, "target_content_digest": target}
    elif category == "model_cache":
        if data.get("format") not in {"declared_text_table", "declared_json", "opaque_binary_no_execution"}: _fail("unsupported model/cache format declaration")
        members = data.get("members")
        if not isinstance(members, list) or not members: _fail("model/cache members must be non-empty")
        paths = set()
        for member in members:
            if not isinstance(member, dict) or not isinstance(member.get("path"), str) or not member["path"] or member["path"].startswith("/") or ".." in member["path"].split("/"):
                _fail("unsafe model/cache member path")
            if member["path"] in paths: _fail("duplicate model/cache member")
            paths.add(member["path"]); _sha(member.get("sha256"), "model/cache member sha256")
            if not isinstance(member.get("bytes"), int) or member["bytes"] < 0: _fail("invalid model/cache member bytes")
        result["observed"] = {"member_count": len(members), "format": data["format"], "model_code_executed": False}
    else:
        samples = _axis(data.get("sample_axis"), "data.sample_axis")
        cells = _axis(data.get("cell_type_axis"), "data.cell_type_axis")
        mapping = data.get("mapping", {})
        if not isinstance(mapping, dict) or set(mapping) - set(cells) or not all(isinstance(v, str) and v.strip() for v in mapping.values()):
            _fail("metadata mapping has unknown or invalid cell categories")
        result["observed"] = {"sample_count": len(samples), "cell_type_count": len(cells), "mapping_count": len(mapping)}
    return result
