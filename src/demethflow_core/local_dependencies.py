"""Fail-closed local bindings for non-distributable execution dependencies.

The public reference catalog deliberately does not contain server-local SIFs,
models or caches.  A logical dependency in a catalog therefore remains a
blocker until an operator supplies two local documents:

* a portable, reviewable *binding lock* containing scope and content identity;
* a non-portable *locator overlay* mapping a lock key to a local path.

The split prevents a server path from being mistaken for a distributable
artifact.  Neither document downloads, executes, imports, nor repackages a
runtime.  Directory identities are explicit complete member manifests: every
regular file must be declared, in sorted POSIX-path order, and symlinks are
rejected.  SIFs are opaque regular files; this contract does not claim an
interior SBOM for them.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import DeMethFlowError
from .util import sha256_file


BINDING_SCHEMA = "methunmix-local-dependency-bindings-v1"
LOCATOR_SCHEMA = "methunmix-local-dependency-locators-v1"
RUNTIME_MODULE_KINDS = frozenset({"runtime_module", "logical_runtime_module"})
SUPPORTED_LOCAL_DEPENDENCY_KINDS = frozenset({*RUNTIME_MODULE_KINDS, "logical_cache"})


def canonical_local_dependency_kind(value: object) -> str:
    """Validate a dependency kind and normalize the historical runtime alias.

    Catalog v1 writes ``runtime_module`` while early local-binding fixtures and
    locks used ``logical_runtime_module``.  They name the same user-supplied,
    content-bound runtime object.  Keep both inputs compatible, but use one
    matching key and reject unrecognized kinds rather than letting a plan look
    resolved without an execution consumer.
    """
    if not isinstance(value, str) or value not in SUPPORTED_LOCAL_DEPENDENCY_KINDS:
        raise DeMethFlowError(
            "LOCAL_DEPENDENCY_KIND_UNSUPPORTED: expected runtime_module, "
            "logical_runtime_module, or logical_cache"
        )
    return "runtime_module" if value in RUNTIME_MODULE_KINDS else value


def is_runtime_module_kind(value: object) -> bool:
    return isinstance(value, str) and value in RUNTIME_MODULE_KINDS


def _load(path: Path, *, label: str) -> dict[str, Any]:
    try:
        if path.is_symlink():
            raise OSError("symlink is not allowed")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"LOCAL_DEPENDENCY_{label}_INVALID: {exc}") from exc
    if not isinstance(value, dict):
        raise DeMethFlowError(f"LOCAL_DEPENDENCY_{label}_INVALID: expected a JSON object")
    return value


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower())


def _safe_key(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise DeMethFlowError(f"LOCAL_DEPENDENCY_BINDING_INVALID: {field} must be a non-empty simple key")
    return value


def _safe_member(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DeMethFlowError("LOCAL_DEPENDENCY_BINDING_INVALID: directory member path is required")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or "\\" in value:
        raise DeMethFlowError("LOCAL_DEPENDENCY_BINDING_INVALID: unsafe directory member path")
    return path.as_posix()


def _regular_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DeMethFlowError(f"LOCAL_DEPENDENCY_NOT_FOUND: {path}: {exc}") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise DeMethFlowError(f"LOCAL_DEPENDENCY_LOCATOR_INVALID: expected a regular non-symlink file: {path}")


def _safe_locator_path(value: str) -> Path:
    """Return a host-local locator only when every lexical component is safe.

    Locator overlays are deliberately non-portable, but they must still not
    make a plan depend on the caller's current working directory or traverse a
    symlinked parent.  Resolving first would hide such a traversal, so inspect
    the lexical absolute path instead.
    """
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise DeMethFlowError("LOCAL_DEPENDENCY_LOCATOR_INVALID: locator path must be absolute and must not contain '..'")
    path = Path(os.path.abspath(path))
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise DeMethFlowError(f"LOCAL_DEPENDENCY_LOCATOR_INVALID: symlink in locator path: {candidate}")
    return path


def _directory_identity(path: Path, expected: dict[str, Any]) -> tuple[str, int, list[dict[str, Any]]]:
    if path.is_symlink() or not path.is_dir():
        raise DeMethFlowError(f"LOCAL_DEPENDENCY_LOCATOR_INVALID: expected a non-symlink directory: {path}")
    members = expected.get("members")
    if not isinstance(members, list) or not members:
        raise DeMethFlowError("LOCAL_DEPENDENCY_BINDING_INVALID: directory identity requires non-empty members")
    declared: dict[str, dict[str, Any]] = {}
    for raw in members:
        if not isinstance(raw, dict):
            raise DeMethFlowError("LOCAL_DEPENDENCY_BINDING_INVALID: directory member must be an object")
        relative = _safe_member(raw.get("path"))
        if relative in declared or not _digest(raw.get("sha256")) or not isinstance(raw.get("bytes"), int) or raw["bytes"] < 0:
            raise DeMethFlowError("LOCAL_DEPENDENCY_BINDING_INVALID: invalid or duplicate directory member")
        declared[relative] = {"path": relative, "sha256": raw["sha256"].lower(), "bytes": raw["bytes"]}
    actual: dict[str, Path] = {}
    for candidate in path.rglob("*"):
        rel = candidate.relative_to(path).as_posix()
        if candidate.is_symlink():
            raise DeMethFlowError(f"LOCAL_DEPENDENCY_LOCATOR_INVALID: symlink in directory dependency: {rel}")
        if candidate.is_file():
            actual[rel] = candidate
        elif not candidate.is_dir():
            raise DeMethFlowError(f"LOCAL_DEPENDENCY_LOCATOR_INVALID: special file in directory dependency: {rel}")
    if set(actual) != set(declared):
        missing, extra = sorted(set(declared) - set(actual)), sorted(set(actual) - set(declared))
        raise DeMethFlowError(f"LOCAL_DEPENDENCY_MEMBER_SET_MISMATCH: missing={missing[:3]} extra={extra[:3]}")
    canonical: list[dict[str, Any]] = []
    for rel in sorted(declared):
        item = declared[rel]
        candidate = actual[rel]
        if candidate.stat().st_size != item["bytes"] or sha256_file(candidate) != item["sha256"]:
            raise DeMethFlowError(f"LOCAL_DEPENDENCY_IDENTITY_MISMATCH: directory member {rel}")
        canonical.append(item)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), sum(item["bytes"] for item in canonical), canonical


def _scope_matches(binding: dict[str, Any], requirement: dict[str, Any], selection: dict[str, Any]) -> bool:
    scope = binding.get("scope", {})
    if not isinstance(scope, dict):
        raise DeMethFlowError("LOCAL_DEPENDENCY_BINDING_INVALID: scope must be an object")
    for key in ("scenario", "platform", "analysis_contract", "accelerator"):
        value = scope.get(key)
        if value is not None and value != selection.get(key):
            return False
    tools = scope.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
            raise DeMethFlowError("LOCAL_DEPENDENCY_BINDING_INVALID: scope.tools must be a string list")
        if not set(requirement.get("required_by", [])) <= set(tools):
            return False
    profile = binding.get("profile")
    return profile is None or profile == requirement.get("profile")


def resolve_local_dependencies(
    requirements: list[dict[str, Any]], selection: dict[str, Any],
    binding_lock: Path | None, locator_overlay: Path | None,
    *, runtime_files: dict[str, Path] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Resolve selected logical requirements against local exact identities.

    Returns observations and blockers; callers retain unresolved requirements
    as blockers.  No locator path is included in the canonical binding lock.

    ``runtime_files`` is an intentionally ephemeral, caller-owned mapping.
    When supplied, it receives only successfully verified
    ``logical_runtime_module`` files.  It is not part of the observations,
    plan identity, or any portable lock: its sole purpose is to carry the
    already-checked local object through the *same command invocation* to
    doctor and launch-preflight.  The runner still rechecks the selected file
    immediately before a workflow can start.
    """
    if not requirements:
        return [], []
    if binding_lock is None or locator_overlay is None:
        return [], [{"code": "LOCAL_DEPENDENCY_BINDING_REQUIRED", "detail": "logical dependencies require both --local-dependency-lock and --local-dependency-locators"}]
    document = _load(binding_lock, label="BINDING_LOCK")
    locators = _load(locator_overlay, label="LOCATOR_OVERLAY")
    if document.get("schema") != BINDING_SCHEMA or locators.get("schema") != LOCATOR_SCHEMA:
        return [], [{"code": "LOCAL_DEPENDENCY_BINDING_SCHEMA_UNSUPPORTED", "detail": "expected local dependency binding and locator schemas v1"}]
    raw_bindings = document.get("bindings")
    raw_locators = locators.get("locators")
    if not isinstance(raw_bindings, list) or not isinstance(raw_locators, dict):
        return [], [{"code": "LOCAL_DEPENDENCY_BINDING_INVALID", "detail": "bindings must be a list and locators an object"}]
    by_requirement: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    observations: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    for raw in raw_bindings:
        if not isinstance(raw, dict):
            blockers.append({"code": "LOCAL_DEPENDENCY_BINDING_INVALID", "detail": "binding must be an object"})
            continue
        name, kind = raw.get("name"), raw.get("kind")
        if not isinstance(name, str) or not name or not isinstance(kind, str) or not kind:
            blockers.append({"code": "LOCAL_DEPENDENCY_BINDING_INVALID", "detail": "binding name and kind are required"})
            continue
        try:
            matching_kind = canonical_local_dependency_kind(kind)
        except DeMethFlowError as exc:
            blockers.append({"code": "LOCAL_DEPENDENCY_KIND_UNSUPPORTED", "detail": str(exc)})
            continue
        key = (name, matching_kind, raw.get("profile") if isinstance(raw.get("profile"), str) else None)
        identity = raw.get("identity")
        locator_key = raw.get("locator_key")
        if not isinstance(identity, dict) or not _digest(identity.get("sha256")) or not isinstance(identity.get("bytes"), int) or identity["bytes"] < 0:
            blockers.append({"code": "LOCAL_DEPENDENCY_BINDING_INVALID", "detail": f"{name}: exact SHA256 and bytes are required"})
            continue
        try:
            locator_key = _safe_key(locator_key, field=f"{name}.locator_key")
        except DeMethFlowError as exc:
            blockers.append({"code": "LOCAL_DEPENDENCY_BINDING_INVALID", "detail": str(exc)})
            continue
        previous = by_requirement.get(key)
        if previous is not None and previous["identity"] != identity:
            blockers.append({"code": "LOCAL_DEPENDENCY_IDENTITY_CONFLICT", "detail": f"{name}: same logical binding has different content"})
            continue
        by_requirement[key] = {**raw, "identity": {**identity, "sha256": identity["sha256"].lower()}, "locator_key": locator_key}
    for requirement in requirements:
        # A separately verified external-runtime contract is consumed by the
        # external-runtime resolver, not by the local file-binding schema.
        if requirement.get("kind") == "external_runtime_contract":
            continue
        try:
            matching_kind = canonical_local_dependency_kind(requirement.get("kind"))
        except DeMethFlowError as exc:
            blockers.append({"code": "LOCAL_DEPENDENCY_KIND_UNSUPPORTED", "detail": str(exc)})
            continue
        key = (str(requirement.get("name")), matching_kind, requirement.get("profile") if isinstance(requirement.get("profile"), str) else None)
        binding = by_requirement.get(key)
        if binding is None or not _scope_matches(binding, requirement, selection):
            continue
        locator = raw_locators.get(binding["locator_key"])
        if not isinstance(locator, dict) or not isinstance(locator.get("path"), str):
            blockers.append({"code": "LOCAL_DEPENDENCY_LOCATOR_MISSING", "detail": f"{binding['name']}: locator {binding['locator_key']} is absent"})
            continue
        try:
            path = _safe_locator_path(locator["path"])
        except DeMethFlowError as exc:
            blockers.append({"code": str(exc).split(":", 1)[0], "detail": str(exc)})
            continue
        identity = binding["identity"]
        try:
            identity_type = identity.get("type", "file")
            if identity_type not in {"file", "directory"}:
                raise DeMethFlowError(f"LOCAL_DEPENDENCY_BINDING_INVALID: {binding['name']} identity type must be file or directory")
            if identity_type == "directory":
                actual_sha, actual_bytes, members = _directory_identity(path, identity)
            else:
                _regular_file(path)
                actual_sha, actual_bytes, members = sha256_file(path), path.stat().st_size, None
            if actual_sha != identity["sha256"] or actual_bytes != identity["bytes"]:
                raise DeMethFlowError(f"LOCAL_DEPENDENCY_IDENTITY_MISMATCH: {binding['name']} locator content differs from lock")
        except DeMethFlowError as exc:
            blockers.append({"code": str(exc).split(":", 1)[0], "detail": str(exc)})
            continue
        observation = {
            "name": binding["name"], "kind": requirement["kind"], "profile": binding.get("profile"),
            "required_by": requirement.get("required_by", []), "identity": identity,
            "locator_key": binding["locator_key"], "locator_state": "LOCAL_VERIFIED",
            "distribution_status": "USER_SUPPLIED_LOCAL_ONLY", "license_status": binding.get("license_status", "NOT_DISTRIBUTION_APPROVED"),
            "registration_state": binding.get("registration_state", "LOCAL_REGISTERED"),
            "members": members,
        }
        if runtime_files is not None and is_runtime_module_kind(requirement["kind"]):
            if identity.get("type", "file") != "file":
                blockers.append({
                    "code": "LOCAL_DEPENDENCY_RUNTIME_INVALID",
                    "detail": f"{binding['name']}: runtime modules must bind a regular file",
                })
                continue
            prior = runtime_files.get(binding["name"])
            if prior is not None and prior != path:
                blockers.append({
                    "code": "LOCAL_DEPENDENCY_IDENTITY_CONFLICT",
                    "detail": f"{binding['name']}: selected logical runtime resolves to more than one local file",
                })
                continue
            runtime_files[binding["name"]] = path
        observations.append(observation)
    return sorted(observations, key=lambda item: (item["name"], str(item.get("profile") or ""))), blockers


def verify_runtime_binding_observations(
    observations: list[dict[str, Any]], runtime_files: dict[str, Path],
) -> list[tuple[str, bool, str]]:
    """Compare the runtime selected by doctor/runner with the locked content."""
    checks: list[tuple[str, bool, str]] = []
    for row in observations:
        name = str(row["name"])
        selected = runtime_files.get(name)
        identity = row.get("identity", {})
        if selected is None:
            checks.append((f"local-dependency:{name}", False, "selected runtime was not resolved by doctor"))
            continue
        try:
            _regular_file(selected)
            actual_sha, actual_bytes = sha256_file(selected), selected.stat().st_size
            ok = actual_sha == identity.get("sha256") and actual_bytes == identity.get("bytes")
            checks.append((f"local-dependency:{name}", ok, str(selected) if ok else "selected runtime content differs from local dependency lock"))
        except DeMethFlowError as exc:
            checks.append((f"local-dependency:{name}", False, str(exc)))
    return checks
