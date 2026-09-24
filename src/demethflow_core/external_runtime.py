"""Fail-closed contract for third-party algorithm runtimes.

The core MethUnmix distribution contains orchestration and adapters only.  The
algorithm implementations listed here are supplied by a separately licensed,
user-installed runtime tree.  This module deliberately performs only bounded
metadata/path validation; it never downloads, clones, or executes a scientific
algorithm.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import DeMethFlowError


EXTERNAL_TOOLS = frozenset({"CelFEER", "CelFiE", "MetDecode", "PRmeth", "UXM"})
CONTRACT_SCHEMA = "methunmix-external-runtime-v1"
MANIFEST_NAME = "methunmix-external-runtime.json"


@dataclass(frozen=True)
class ExternalRuntime:
    tool: str
    path: Path
    identity: str
    version: str
    license: str
    required_files: tuple[str, ...]


def _safe_relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeMethFlowError(f"EXTERNAL_RUNTIME_CONTRACT_INVALID: {field} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or "\\" in value or any(part in {"", ".", ".."} for part in path.parts):
        raise DeMethFlowError(f"EXTERNAL_RUNTIME_CONTRACT_INVALID: {field} must be a safe relative path")
    return path.as_posix()


def discover_external_runtimes(root: Path | None, tools: set[str] | frozenset[str]) -> dict[str, ExternalRuntime]:
    """Validate an explicitly supplied runtime root for the selected tools.

    The root is intentionally not inferred from the source checkout.  A caller
    must pass ``--external-runtime-root`` (or ``METHUNMIX_EXTERNAL_RUNTIME_ROOT``)
    and provide a manifest with identity, version, license disclosure and file
    requirements for every selected third-party tool.
    """
    required = sorted(set(tools) & EXTERNAL_TOOLS)
    if not required:
        return {}
    if root is None:
        raise DeMethFlowError(
            "EXTERNAL_RUNTIME_NOT_INSTALLED: selected third-party tools require "
            "--external-runtime-root with methunmix-external-runtime.json"
        )
    root = root.expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise DeMethFlowError(f"EXTERNAL_RUNTIME_NOT_INSTALLED: runtime root is not a directory: {root}")
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise DeMethFlowError(f"EXTERNAL_RUNTIME_CONTRACT_INVALID: missing {MANIFEST_NAME} under {root}")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"EXTERNAL_RUNTIME_CONTRACT_INVALID: cannot read {manifest_path}: {exc}") from exc
    if document.get("schema") != CONTRACT_SCHEMA:
        raise DeMethFlowError(f"EXTERNAL_RUNTIME_CONTRACT_INVALID: expected schema {CONTRACT_SCHEMA}")
    entries = document.get("runtimes")
    if not isinstance(entries, dict):
        raise DeMethFlowError("EXTERNAL_RUNTIME_CONTRACT_INVALID: runtimes must be an object")
    found: dict[str, ExternalRuntime] = {}
    for tool in required:
        raw = entries.get(tool)
        if not isinstance(raw, dict):
            raise DeMethFlowError(f"EXTERNAL_RUNTIME_NOT_INSTALLED: manifest has no entry for {tool}")
        relative = _safe_relative(raw.get("path"), f"{tool}.path")
        path = root / relative
        try:
            path.relative_to(root)
        except ValueError as exc:  # defensive, PurePosixPath check above should catch this
            raise DeMethFlowError(f"EXTERNAL_RUNTIME_CONTRACT_INVALID: {tool}.path escapes root") from exc
        if not path.exists() or path.is_symlink():
            raise DeMethFlowError(f"EXTERNAL_RUNTIME_NOT_INSTALLED: {tool} path is missing or symlinked: {path}")
        for field in ("identity", "version", "license"):
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise DeMethFlowError(f"EXTERNAL_RUNTIME_CONTRACT_INVALID: {tool}.{field} is required")
        required_files = raw.get("required_files", [])
        if not isinstance(required_files, list) or any(not isinstance(item, str) for item in required_files):
            raise DeMethFlowError(f"EXTERNAL_RUNTIME_CONTRACT_INVALID: {tool}.required_files must be a list")
        checked_files: list[str] = []
        for item in required_files:
            rel = _safe_relative(item, f"{tool}.required_files")
            candidate = path / rel if path.is_dir() else path.parent / rel
            if not candidate.is_file() or candidate.is_symlink():
                raise DeMethFlowError(f"EXTERNAL_RUNTIME_NOT_INSTALLED: {tool} required file is missing: {candidate}")
            checked_files.append(rel)
        found[tool] = ExternalRuntime(
            tool=tool,
            path=path,
            identity=raw["identity"].strip(),
            version=raw["version"].strip(),
            license=raw["license"].strip(),
            required_files=tuple(checked_files),
        )
    return found


def external_runtime_root_from_env() -> Path | None:
    value = os.environ.get("METHUNMIX_EXTERNAL_RUNTIME_ROOT") or os.environ.get("DEMETHFLOW_EXTERNAL_RUNTIME_ROOT")
    return Path(value).expanduser() if value else None
