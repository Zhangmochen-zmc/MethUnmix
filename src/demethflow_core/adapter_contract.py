"""Declarative adapter contracts for gradual MethUnMix tool modularisation.

The established Nextflow workflows remain the execution authority in 2.x.
This registry is a fail-closed descriptor layer for new/gradually migrated
adapters: it describes what a tool needs without embedding third-party code in
the core package or altering a tool's scientific status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PROJECT_ROOT
from .errors import DeMethFlowError
from .manifest import CONTRACT_TO_PLATFORM, ToolCapability
from .util import sha256_file


SCHEMA = "methunmix-adapter-descriptor-v1"
REQUIRED_OPERATIONS = (
    "validate_input",
    "prepare_input",
    "execute",
    "parse_output",
    "validate_output",
    "collect_provenance",
)
VALID_REFERENCE_REQUIREMENTS = {"required", "none"}
VALID_OUTPUT_CONTRACTS = {
    "cell_proportions_v1",
    "reference_free_components_v1",
    "reference_free_components_v2",
}
ADAPTER_API_VERSION = "1"


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    tool: str
    workflow_family: str
    supported_analysis_contracts: tuple[str, ...]
    input_representations: tuple[str, ...]
    reference_requirement: str
    runtime_requirement: str
    sample_constraint: str
    output_contracts: tuple[str, ...]
    operations: tuple[str, ...]
    provenance_fields: tuple[str, ...]
    adapter_api_version: str
    supported_profiles: tuple[str, ...]
    parameter_semantics: tuple[str, ...]
    model_requirements: tuple[str, ...]
    cacheable_steps: tuple[str, ...]
    recovery_semantics: str
    error_classes: tuple[str, ...]
    scientific_disclosure_source: str
    path: Path
    sha256: str


def _require_string(raw: dict[str, Any], field: str, path: Path) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: {path}: {field} must be a non-empty string")
    return value.strip()


def _require_string_list(raw: dict[str, Any], field: str, path: Path) -> tuple[str, ...]:
    value = raw.get(field)
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: {path}: {field} must be a non-empty string list")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: {path}: {field} contains duplicates")
    return normalized


def _require_optional_string_list(raw: dict[str, Any], field: str, path: Path) -> tuple[str, ...]:
    """Accept an explicit empty list for a capability that is deliberately absent."""
    value = raw.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: {path}: {field} must be a string list")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: {path}: {field} contains duplicates")
    return normalized


def _parse_descriptor(path: Path) -> AdapterDescriptor:
    if not path.is_file() or path.is_symlink():
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: descriptor is missing or symlinked: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: cannot parse {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: {path}: expected schema {SCHEMA}")
    contracts = _require_string_list(raw, "supported_analysis_contracts", path)
    unknown = sorted(set(contracts) - set(CONTRACT_TO_PLATFORM))
    if unknown:
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: {path}: unsupported analysis contracts {unknown}")
    output_contracts = _require_string_list(raw, "output_contracts", path)
    if unknown_outputs := sorted(set(output_contracts) - VALID_OUTPUT_CONTRACTS):
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: {path}: unsupported output contracts {unknown_outputs}")
    reference_requirement = _require_string(raw, "reference_requirement", path)
    if reference_requirement not in VALID_REFERENCE_REQUIREMENTS:
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: {path}: unsupported reference_requirement")
    operations = _require_string_list(raw, "operations", path)
    if set(operations) != set(REQUIRED_OPERATIONS) or len(operations) != len(REQUIRED_OPERATIONS):
        raise DeMethFlowError(
            f"ADAPTER_DESCRIPTOR_INVALID: {path}: operations must be exactly {list(REQUIRED_OPERATIONS)}"
        )
    return AdapterDescriptor(
        adapter_id=_require_string(raw, "adapter_id", path),
        tool=_require_string(raw, "tool", path),
        workflow_family=_require_string(raw, "workflow_family", path),
        supported_analysis_contracts=contracts,
        input_representations=_require_string_list(raw, "input_representations", path),
        reference_requirement=reference_requirement,
        runtime_requirement=_require_string(raw, "runtime_requirement", path),
        sample_constraint=_require_string(raw, "sample_constraint", path),
        output_contracts=output_contracts,
        operations=operations,
        provenance_fields=_require_string_list(raw, "provenance_fields", path),
        adapter_api_version=_require_string(raw, "adapter_api_version", path),
        supported_profiles=_require_string_list(raw, "supported_profiles", path),
        parameter_semantics=_require_string_list(raw, "parameter_semantics", path),
        model_requirements=_require_optional_string_list(raw, "model_requirements", path),
        cacheable_steps=_require_optional_string_list(raw, "cacheable_steps", path),
        recovery_semantics=_require_string(raw, "recovery_semantics", path),
        error_classes=_require_string_list(raw, "error_classes", path),
        scientific_disclosure_source=_require_string(raw, "scientific_disclosure_source", path),
        path=path.resolve(),
        sha256=sha256_file(path),
    )


def default_descriptor_root() -> Path:
    return PROJECT_ROOT / "capabilities" / "adapters"


def load_adapter_descriptors(root: Path | None = None) -> dict[str, AdapterDescriptor]:
    directory = (root or default_descriptor_root()).expanduser().resolve()
    if not directory.is_dir() or directory.is_symlink():
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: adapter descriptor directory is missing: {directory}")
    descriptors: dict[str, AdapterDescriptor] = {}
    tools: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        descriptor = _parse_descriptor(path)
        if descriptor.adapter_id in descriptors:
            raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: duplicate adapter_id {descriptor.adapter_id}")
        if descriptor.tool in tools:
            raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: duplicate tool descriptor {descriptor.tool}")
        descriptors[descriptor.adapter_id] = descriptor
        tools.add(descriptor.tool)
    if not descriptors:
        raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: no descriptors under {directory}")
    return descriptors


def validate_adapter_binding(
    descriptor: AdapterDescriptor,
    *,
    analysis_contract: str,
    capability: ToolCapability,
) -> dict[str, object]:
    """Validate a descriptor against an existing manifest capability only."""
    problems: list[str] = []
    if analysis_contract not in descriptor.supported_analysis_contracts:
        problems.append(f"analysis contract {analysis_contract} is not declared by adapter")
    if capability.output_contract not in descriptor.output_contracts:
        problems.append(f"output contract {capability.output_contract} is not declared by adapter")
    if capability.reference_requirement != descriptor.reference_requirement:
        problems.append(
            f"reference requirement manifest={capability.reference_requirement} adapter={descriptor.reference_requirement}"
        )
    return {
        "adapter_id": descriptor.adapter_id,
        "tool": descriptor.tool,
        "descriptor_sha256": descriptor.sha256,
        "status": "PASS" if not problems else "FAIL",
        "problems": problems,
    }


def registry_report(root: Path | None = None) -> dict[str, object]:
    descriptors = load_adapter_descriptors(root)
    return {
        "schema": "methunmix-adapter-registry-report-v1",
        "status": "PASS",
        "descriptor_count": len(descriptors),
        "descriptors": [
            {
                "adapter_id": item.adapter_id,
                "tool": item.tool,
                "workflow_family": item.workflow_family,
                "contracts": list(item.supported_analysis_contracts),
                "output_contracts": list(item.output_contracts),
                "adapter_api_version": item.adapter_api_version,
                "supported_profiles": list(item.supported_profiles),
                "sha256": item.sha256,
            }
            for item in descriptors.values()
        ],
    }
