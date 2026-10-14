"""Selection-bound asset planning and lifecycle operations.

This module deliberately reuses the static catalog reader, :class:`ModuleStore`
and the external-runtime contract.  It does not introduce a second asset
store, downloader, or selector matrix.  A plan is observational until an
explicit install/import/repair command receives it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import CORE_API, REFERENCE_SCHEMA, RUNTIME_API, version
from .asset_manager import _current_catalog, _ensure_cached_archive_sidecar, fetch_asset_record
from .errors import DeMethFlowError
from .external_runtime import (
    CONTRACT_SCHEMA,
    EXTERNAL_TOOLS,
    MANIFEST_NAME,
    discover_external_runtimes,
)
from .local_dependencies import canonical_local_dependency_kind, resolve_local_dependencies
from .manifest import resolve_tool_selection
from .modules import ModuleStore
from .util import atomic_write_json, default_data_home, sha256_file, stable_digest
from .offline_assets import _offline_tool_capabilities, installation_receipt_path


PLAN_SCHEMA = "methunmix-installation-plan-v1"
CATALOG_INSTALLATION_RECEIPT_SCHEMA = "methunmix-catalog-installation-receipt-v1"
PLAN_RESOLVER_VERSION = "wp03-3-local-binding"
_DISTRIBUTION = {"PUBLIC_DOWNLOADABLE", "USER_SUPPLIED_SUPPORTED", "UNAVAILABLE", "QUARANTINED", "LOCAL_ONLY", "BLOCKED", "PUBLIC_BUNDLED", "INTERNAL_VALIDATED"}


def _is_verified_external_contract_requirement(
    requirement: dict[str, Any], verified_tools: set[str],
) -> bool:
    """Return whether one precise logical requirement has a checked contract.

    Third-party runtime contracts are not catalog assets and must never be
    treated as a generic, identity-free runtime module.  The only satisfiable
    form is the explicit contract emitted by the catalog consumer for selected
    external tools; every tool in its scope must have been independently
    checked by :func:`discover_external_runtimes`.
    """
    required_by = requirement.get("required_by")
    return (
        requirement.get("kind") == "external_runtime_contract"
        and requirement.get("name") == MANIFEST_NAME
        and requirement.get("identity_state") == "RUNTIME_CONTRACT_REQUIRED"
        and requirement.get("acquisition") == "USER_SUPPLIED_SUPPORTED"
        and requirement.get("profile") is None
        and isinstance(required_by, list)
        and bool(required_by)
        and all(isinstance(tool, str) and tool in verified_tools for tool in required_by)
    )


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower())


def _catalog_snapshot(metadata_dir: Path | None) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog, origin = _current_catalog(metadata_dir)
    return catalog, {
        "origin": origin,
        "schema": catalog.get("schema"),
        "release": catalog.get("catalog_release"),
        "sequence": catalog.get("catalog_sequence"),
        "canonical_sha256": _canonical_digest(catalog),
    }


def _safe_selection(
    *, scenario: str, platform: str, genome_build: str | None, analysis_contract: str,
    tools: str, accelerator: str, reference: str | None,
) -> dict[str, Any]:
    if not all(isinstance(item, str) and item.strip() for item in (scenario, platform, analysis_contract, tools, accelerator)):
        raise DeMethFlowError("asset plan requires non-empty scenario, platform, analysis contract, tools and accelerator")
    if platform not in {"450k", "epic", "wgbs"}:
        raise DeMethFlowError("asset plan platform must be 450k, epic, or wgbs")
    if accelerator not in {"cpu", "gpu"}:
        raise DeMethFlowError("asset plan accelerator must be cpu or gpu")
    if platform == "wgbs" and genome_build not in {"hg19", "hg38"}:
        raise DeMethFlowError("native or derived WGBS asset planning requires --genome-build hg19 or hg38")
    if platform != "wgbs" and genome_build is not None:
        raise DeMethFlowError("array asset planning does not accept a genome build")
    requested_tools = [part.strip() for part in tools.split(",") if part.strip()]
    if not requested_tools:
        raise DeMethFlowError("asset plan tools cannot be empty")
    return {
        "scenario": scenario, "platform": platform, "genome_build": genome_build,
        "analysis_contract": analysis_contract, "tools": sorted(set(requested_tools)),
        "accelerator": accelerator, "reference": reference,
    }


def _reference_candidates(targets: list[dict[str, Any]], selection: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for target in targets:
        if target.get("module_type") != "reference":
            continue
        selector = target.get("selector")
        if selection["reference"] and selector != selection["reference"]:
            continue
        if target.get("scenario") != selection["scenario"]:
            continue
        # New catalog records preserve the manifest's input-route semantics.
        # In particular, WGBS-derived references target an array CpG space but
        # accept WGBS input.  Legacy catalogs lack this optional field, so their
        # established source-platform matching remains unchanged.
        routes = target.get("compatible_input_routes")
        if isinstance(routes, list):
            platform_matches = selection["platform"] in routes
        else:
            platform_matches = target.get("source_platform") == selection["platform"]
        if not platform_matches:
            continue
        if target.get("analysis_contract") != selection["analysis_contract"]:
            continue
        if target.get("genome_build") != (selection["genome_build"] or "not_applicable"):
            continue
        output.append(target)
    return sorted(output, key=lambda row: str(row.get("asset_id")))


def _dependency_items(
    target: dict[str, Any], selection: dict[str, Any], selected_tools: Iterable[str],
) -> list[dict[str, Any]]:
    """Read optional, backward-compatible catalog dependency declarations.

    Old reference catalogs have no dependency graph.  They remain readable but
    yield a precise ``DEPENDENCY_METADATA_MISSING`` warning rather than an
    inferred runtime/model/cache set.
    """
    raw: list[Any] = []
    if "dependencies" in target:
        if not isinstance(target["dependencies"], list):
            raise DeMethFlowError(f"Catalog dependencies must be a list for {target.get('asset_id')}")
        raw.extend(target["dependencies"])
    tool_dependencies = target.get("tool_dependencies", {})
    if tool_dependencies is not None:
        if not isinstance(tool_dependencies, dict):
            raise DeMethFlowError(f"Catalog tool_dependencies must be an object for {target.get('asset_id')}")
        # ``--tools all`` is expanded by the shared resolver before we read
        # tool-specific dependencies.  Looking at the literal request here
        # would silently omit every dependency keyed by an actual tool name.
        for tool in selected_tools:
            values = tool_dependencies.get(tool, [])
            if not isinstance(values, list):
                raise DeMethFlowError(f"Catalog tool_dependencies.{tool} must be a list")
            raw.extend(values)
    profile_dependencies = target.get("profile_dependencies", {})
    if profile_dependencies is not None:
        if not isinstance(profile_dependencies, dict):
            raise DeMethFlowError(f"Catalog profile_dependencies must be an object for {target.get('asset_id')}")
        values = profile_dependencies.get(selection["accelerator"], [])
        if not isinstance(values, list):
            raise DeMethFlowError(f"Catalog profile_dependencies.{selection['accelerator']} must be a list")
        raw.extend(values)
    output: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            output.append({"asset_id": item, "required": True})
        elif isinstance(item, dict) and isinstance(item.get("asset_id"), str):
            normalized = dict(item)
            normalized.setdefault("required", True)
            output.append(normalized)
        else:
            raise DeMethFlowError(f"Invalid dependency entry for {target.get('asset_id')}")
    return output


def _dependency_requirements(
    target: dict[str, Any], selected_tools: Iterable[str], accelerator: str,
) -> list[dict[str, Any]]:
    """Read unresolved logical dependencies without pretending they are assets.

    A runtime name found in a reference manifest is not an immutable downloadable
    asset identity.  It is surfaced as a requirement and blocks a complete plan
    until a later catalog release binds it to an exact public or user-supplied
    contract.  This deliberately differs from ``tool_dependencies``, whose
    entries must resolve to catalog targets.
    """
    raw = target.get("dependency_requirements", {})
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise DeMethFlowError(f"Catalog dependency_requirements must be an object for {target.get('asset_id')}")
    rows: list[dict[str, Any]] = []
    for tool in selected_tools:
        values = raw.get(tool, [])
        if not isinstance(values, list):
            raise DeMethFlowError(f"Catalog dependency_requirements.{tool} must be a list")
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise DeMethFlowError(f"Catalog dependency_requirements.{tool} has an invalid requirement")
            profile = item.get("profile")
            if profile is not None and profile != accelerator:
                continue
            normalized = dict(item)
            normalized["tool"] = tool
            normalized.setdefault("kind", "runtime_module")
            # Validate here, before a plan can present an unsupported kind as
            # an identity-insufficient but otherwise recognized dependency.
            if normalized["kind"] != "external_runtime_contract":
                canonical_local_dependency_kind(normalized["kind"])
            normalized.setdefault("acquisition", "IDENTITY_INSUFFICIENT")
            normalized.setdefault("identity_state", "IDENTITY_INSUFFICIENT")
            rows.append(normalized)
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _canonical_digest({key: value for key, value in row.items() if key not in {"source", "tool"}})
        existing = unique.get(key)
        if existing is None:
            normalized = dict(row)
            normalized["required_by"] = [str(row["tool"])]
            normalized.pop("tool", None)
            unique[key] = normalized
        else:
            existing["required_by"] = sorted(set([*existing["required_by"], str(row["tool"])]))
    return sorted(unique.values(), key=lambda row: (str(row.get("kind")), str(row.get("name")), str(row.get("profile") or ""), ",".join(row["required_by"])))


def _target_identity(target: dict[str, Any]) -> dict[str, Any]:
    status = target.get("distribution_status")
    row = {
        "asset_id": target.get("asset_id"), "selector": target.get("selector"), "reference_id": target.get("reference_id"),
        "asset_type": target.get("asset_type", target.get("module_type", "unknown")),
        "module_type": target.get("module_type"), "version": target.get("version"),
        "archive_sha256": target.get("sha256"), "archive_bytes": target.get("length"),
        "installed_bytes": target.get("installed_bytes"), "content_digest": target.get("content_digest"),
        "distribution_status": status, "source": target.get("source"),
        "compatibility": target.get("compatibility"), "revoked": bool(target.get("revoked")),
        "replaces": target.get("replaces"),
    }
    return row


def _resolve_catalog_tools(reference: dict[str, Any], requested: list[str], accelerator: str) -> tuple[list[str], list[dict[str, str]]]:
    """Use the shared manifest selection policy when catalog metadata permits.

    A catalog that predates capability metadata can still be inspected, but it
    cannot honestly promise a tool-specific installation set before the
    reference has been installed and read.  This is deliberately a blocker,
    rather than a second support matrix maintained by the asset planner.
    """
    raw = reference.get("tool_capabilities")
    if not isinstance(raw, dict):
        raise DeMethFlowError("TOOL_SELECTION_METADATA_MISSING: reference catalog target lacks tool_capabilities")
    try:
        return resolve_tool_selection(
            _offline_tool_capabilities(reference),
            ",".join(requested),
            accelerator=accelerator,
            allow_validation_candidate=False,
            contract=str(reference.get("analysis_contract", "")),
            version=str(reference.get("version", "")),
        )
    except Exception as exc:
        if isinstance(exc, DeMethFlowError):
            raise
        raise DeMethFlowError(f"TOOL_SELECTION_FAILED: {exc}") from exc


def _state_for_target(target: dict[str, Any], store: ModuleStore) -> tuple[str, str | None]:
    status = target.get("distribution_status")
    if target.get("revoked"):
        return "REVOKED", str(target.get("revocation_reason") or "catalog target revoked")
    if status == "USER_SUPPLIED_SUPPORTED":
        return "WAITING_FOR_USER_SUPPLIED", "user-supplied asset has no public acquisition route"
    if status not in _DISTRIBUTION:
        return "IDENTITY_INSUFFICIENT", "unknown distribution status"
    if status != "PUBLIC_DOWNLOADABLE":
        return "NOT_INSTALLABLE", f"distribution status is {status}"
    asset_id, target_version, digest = target.get("asset_id"), target.get("version"), target.get("sha256")
    if not isinstance(asset_id, str) or not isinstance(target_version, str) or not _is_digest(digest):
        return "IDENTITY_INSUFFICIENT", "public asset lacks exact module identity or archive SHA256"
    matches = [m for m in store.list() if m.module_id == asset_id and m.version == target_version]
    if not matches:
        return "NOT_INSTALLED", None
    if len(matches) != 1:
        return "IDENTITY_INSUFFICIENT", "multiple installed modules have the same selector"
    module = matches[0]
    if module.payload.get("archive_sha256") != digest:
        return "INCOMPATIBLE", "installed archive SHA256 differs from catalog target"
    errors = store.verify(module)
    if errors:
        return "CORRUPT", "; ".join(errors[:4])
    return "INSTALLED_VERIFIED", str(module.install_root)


def create_catalog_plan(
    *, scenario: str, platform: str, genome_build: str | None, analysis_contract: str,
    tools: str, accelerator: str = "cpu", reference: str | None = None,
    external_runtime_root: Path | None = None, metadata_dir: Path | None = None,
    local_dependency_lock: Path | None = None, local_dependency_locators: Path | None = None,
    capture_local_runtime_files: bool = False,
    output: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Resolve a deterministic, non-mutating installation plan from a catalog."""
    selection = _safe_selection(
        scenario=scenario, platform=platform, genome_build=genome_build,
        analysis_contract=analysis_contract, tools=tools, accelerator=accelerator, reference=reference,
    )
    catalog, snapshot = _catalog_snapshot(metadata_dir)
    raw_targets = catalog.get("targets", [])
    targets = [item for item in raw_targets if isinstance(item, dict)]
    index = {str(item.get("asset_id")): item for item in targets if isinstance(item.get("asset_id"), str)}
    store = ModuleStore()
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    candidates = _reference_candidates(targets, selection)
    root: dict[str, Any] | None = candidates[0] if len(candidates) == 1 else None
    selected_tools: list[str] = []
    skipped_tools: list[dict[str, str]] = []
    if root is None:
        blockers.append({
            "code": "REFERENCE_RESOLUTION_UNIQUE_REQUIRED",
            "detail": f"expected exactly one reference target, found {len(candidates)}",
        })
    elif (
        not root.get("dependencies")
        and not root.get("tool_dependencies")
        and not root.get("profile_dependencies")
        and not root.get("dependency_requirements")
    ):
        warnings.append({
            "code": "DEPENDENCY_METADATA_MISSING",
            "detail": "catalog is readable but does not declare runtime/model/cache dependency metadata for this reference",
        })
    if root is not None:
        try:
            selected_tools, skipped_tools = _resolve_catalog_tools(root, selection["tools"], selection["accelerator"])
            # The shared selector preserves its registry order for execution;
            # plans additionally need a canonical order so their lock identity
            # is stable across equivalent catalog serializations.
            selected_tools = sorted(selected_tools)
            skipped_tools = sorted(skipped_tools, key=lambda item: (item.get("tool", ""), item.get("reason", "")))
        except DeMethFlowError as exc:
            blockers.append({"code": str(exc).split(":", 1)[0], "detail": str(exc)})

    rows: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    visiting: list[str] = []

    def visit(asset_id: str, *, required_by: str, expected: dict[str, Any] | None = None) -> None:
        if asset_id in visiting:
            cycle = " -> ".join([*visiting, asset_id])
            blockers.append({"code": "DEPENDENCY_CYCLE", "detail": cycle})
            return
        target = index.get(asset_id)
        if target is None:
            blockers.append({"code": "DEPENDENCY_MISSING_FROM_CATALOG", "detail": f"{asset_id} required by {required_by}"})
            return
        if expected:
            for field in ("version", "sha256", "content_digest"):
                expected_value = expected.get(field)
                if expected_value is not None and target.get(field) != expected_value:
                    blockers.append({"code": "DEPENDENCY_IDENTITY_CONFLICT", "detail": f"{asset_id} {field} differs from dependency declaration"})
        if asset_id in rows:
            rows[asset_id]["required_by"] = sorted(set([*rows[asset_id]["required_by"], required_by]))
            return
        visiting.append(asset_id)
        state, detail = _state_for_target(target, store)
        row = _target_identity(target)
        row.update({"required_by": [required_by], "local_state": state, "local_detail": detail})
        rows[asset_id] = row
        for dependency in _dependency_items(target, selection, selected_tools):
            child = str(dependency["asset_id"])
            edges.append({"from": asset_id, "to": child, "required": str(bool(dependency.get("required", True))).lower()})
            if dependency.get("required", True):
                visit(child, required_by=asset_id, expected=dependency)
        visiting.pop()

    if root is not None:
        visit(str(root["asset_id"]), required_by="selected analysis")

    requirements: list[dict[str, Any]] = []
    local_dependencies: list[dict[str, Any]] = []
    # This mapping is request-scoped only.  It deliberately never enters the
    # stable payload, serialised plan, plan digest, receipt, or run manifest.
    # A locator is host-local location information, not portable identity.
    local_runtime_files: dict[str, Path] = {}
    external: list[dict[str, Any]] = []
    verified_external_tools: set[str] = set()
    external_tools = sorted(set(selected_tools) & set(EXTERNAL_TOOLS))
    if external_tools:
        try:
            found = discover_external_runtimes(external_runtime_root, set(external_tools))
        except DeMethFlowError as exc:
            for tool in external_tools:
                external.append({
                    "tool": tool, "acquisition": "USER_SUPPLIED_SUPPORTED",
                    "local_state": "WAITING_FOR_USER_SUPPLIED", "detail": str(exc),
                    "contract_schema": CONTRACT_SCHEMA, "contract_manifest": MANIFEST_NAME,
                })
            blockers.append({"code": "USER_SUPPLIED_RUNTIME_REQUIRED", "detail": str(exc)})
        else:
            for tool in external_tools:
                runtime = found[tool]
                verified_external_tools.add(tool)
                external.append({
                    "tool": tool, "acquisition": "USER_SUPPLIED_SUPPORTED",
                    "distribution_status": "USER_SUPPLIED_LOCAL_ONLY",
                    "local_state": "INSTALLED_VERIFIED", "contract_schema": CONTRACT_SCHEMA,
                    "contract_manifest": MANIFEST_NAME, "identity": runtime.identity,
                    "version": runtime.version, "license": runtime.license,
                    "content_sha256": runtime.content_sha256,
                    "required_files": list(runtime.required_files),
                })
    if root is not None and selected_tools:
        requirements = _dependency_requirements(root, selected_tools, selection["accelerator"])
        if local_dependency_lock is not None or local_dependency_locators is not None:
            local_dependencies, local_blockers = resolve_local_dependencies(
                requirements, selection, local_dependency_lock, local_dependency_locators,
                runtime_files=local_runtime_files if capture_local_runtime_files else None,
            )
            blockers.extend(local_blockers)
        resolved_local = {
            (str(item.get("name")), canonical_local_dependency_kind(item.get("kind")), item.get("profile") if isinstance(item.get("profile"), str) else None)
            for item in local_dependencies
        }
        for requirement in requirements:
            if _is_verified_external_contract_requirement(requirement, verified_external_tools):
                continue
            key = (
                str(requirement.get("name")), canonical_local_dependency_kind(requirement.get("kind")),
                requirement.get("profile") if isinstance(requirement.get("profile"), str) else None,
            )
            if key in resolved_local:
                continue
            state = str(requirement.get("identity_state", "IDENTITY_INSUFFICIENT"))
            if state != "RESOLVED":
                blockers.append({
                    "code": "DEPENDENCY_IDENTITY_INSUFFICIENT",
                    "detail": (
                        f"{','.join(requirement['required_by'])}: {requirement['kind']} {requirement['name']} "
                        f"is {state}; no exact catalog asset is declared"
                    ),
                })

    for row in rows.values():
        if row["local_state"] in {"REVOKED", "NOT_INSTALLABLE", "IDENTITY_INSUFFICIENT", "INCOMPATIBLE", "CORRUPT"}:
            blockers.append({"code": row["local_state"], "detail": f"{row['asset_id']}: {row.get('local_detail') or ''}"})
    needs_download = [r for r in rows.values() if r["local_state"] == "NOT_INSTALLED" and r["distribution_status"] == "PUBLIC_DOWNLOADABLE"]
    known_download = sum(int(r["archive_bytes"]) for r in needs_download if isinstance(r.get("archive_bytes"), int))
    unknown_download = [r["asset_id"] for r in needs_download if not isinstance(r.get("archive_bytes"), int)]
    known_install = sum(int(r["installed_bytes"]) for r in needs_download if isinstance(r.get("installed_bytes"), int))
    unknown_install = [r["asset_id"] for r in needs_download if not isinstance(r.get("installed_bytes"), int)]
    estimate: dict[str, Any] = {
        "download_bytes_known": known_download,
        "download_bytes_unknown_assets": sorted(unknown_download),
        "installed_bytes_known": known_install,
        "installed_bytes_unknown_assets": sorted(unknown_install),
        "temporary_extract_bytes_known": known_install,
        "peak_additional_bytes_known": None if unknown_install else known_download + (2 * known_install) + 64 * 1024 * 1024,
        "scope": "known archive and installed-byte fields only; unknown values are not treated as zero",
    }
    selection_record = dict(selection)
    selection_record["tools_requested"] = selection_record.pop("tools")
    selection_record["tools_selected"] = selected_tools
    selection_record["tools_skipped"] = skipped_tools
    stable_payload = {
        "schema": PLAN_SCHEMA, "resolver_version": PLAN_RESOLVER_VERSION,
        "selection": selection_record, "catalog": snapshot,
        "reference": _target_identity(root) if root else None,
        "assets": sorted(rows.values(), key=lambda row: str(row["asset_id"])),
        "edges": sorted(edges, key=lambda row: (row["from"], row["to"], row["required"])),
        "dependency_requirements": requirements,
        "local_dependencies": local_dependencies,
        "external_runtimes": sorted(external, key=lambda row: row["tool"]),
        "blockers": sorted(blockers, key=lambda row: (row["code"], row["detail"])),
        "warnings": sorted(warnings, key=lambda row: (row["code"], row["detail"])),
        "space": estimate,
        "compatibility": {"core_api": CORE_API, "reference_schema": REFERENCE_SCHEMA, "runtime_api": RUNTIME_API},
    }
    plan = dict(stable_payload)
    plan["core_version"] = version()
    plan["created_at"] = datetime.now(timezone.utc).isoformat()
    plan["plan_identity"] = {"algorithm": "sha256-canonical-json-v1", "sha256": _canonical_digest(stable_payload)}
    plan["installable"] = not blockers
    plan["suggested_actions"] = [
        "asset install --plan PLAN only installs PUBLIC_DOWNLOADABLE modules already locked by this plan",
        "asset import --plan PLAN --archive ARCHIVE uses the same identity and install validation offline",
        "USER_SUPPLIED_SUPPORTED runtimes require an explicit external runtime root and are never downloaded",
    ]
    if capture_local_runtime_files:
        # Keep the key visibly private and non-serialisable.  CLI consumers
        # remove it before retaining the public installation dependency plan.
        plan["_ephemeral_local_runtime_files"] = local_runtime_files
    if output is not None:
        atomic_write_json(
            output.expanduser().resolve(),
            {key: value for key, value in plan.items() if key != "_ephemeral_local_runtime_files"},
        )
    return plan, 0 if plan["installable"] else 2


def render_catalog_plan_summary(plan: dict[str, Any]) -> str:
    """Return a concise, non-mutating human view of a machine-readable plan."""
    selection = plan.get("selection", {}) if isinstance(plan.get("selection"), dict) else {}
    space = plan.get("space", {}) if isinstance(plan.get("space"), dict) else {}
    lines = [
        f"PLAN\t{plan.get('plan_identity', {}).get('sha256', 'unknown')}",
        "SELECTION\t" + "\t".join(
            f"{name}={selection.get(name)!s}"
            for name in ("scenario", "platform", "genome_build", "analysis_contract", "accelerator")
        ),
        "TOOLS\trequested=" + ",".join(selection.get("tools_requested", []))
        + "\tselected=" + ",".join(selection.get("tools_selected", [])),
        "SPACE\tdownload_known=" + str(space.get("download_bytes_known"))
        + "\tinstalled_known=" + str(space.get("installed_bytes_known"))
        + "\ttemporary_known=" + str(space.get("temporary_extract_bytes_known")),
        "ASSET\tSTATE\tDISTRIBUTION\tREQUIRED_BY",
    ]
    for asset in plan.get("assets", []):
        if isinstance(asset, dict):
            lines.append(
                f"{asset.get('asset_id')}\t{asset.get('local_state')}\t"
                f"{asset.get('distribution_status')}\t{','.join(asset.get('required_by', []))}"
            )
    for requirement in plan.get("dependency_requirements", []):
        if isinstance(requirement, dict):
            lines.append(
                f"REQUIREMENT\t{','.join(requirement.get('required_by', []))}:{requirement.get('name')}\t"
                f"{requirement.get('identity_state')}\t{requirement.get('acquisition')}"
            )
    for dependency in plan.get("local_dependencies", []):
        if isinstance(dependency, dict):
            lines.append(
                f"LOCAL_DEPENDENCY\t{','.join(dependency.get('required_by', []))}:{dependency.get('name')}\t"
                f"{dependency.get('locator_state')}\t{dependency.get('distribution_status')}"
            )
    for warning in plan.get("warnings", []):
        lines.append(f"WARNING\t{warning.get('code')}\t{warning.get('detail')}")
    for blocker in plan.get("blockers", []):
        lines.append(f"BLOCKER\t{blocker.get('code')}\t{blocker.get('detail')}")
    lines.append("STATUS\t" + ("INSTALLABLE" if plan.get("installable") else "BLOCKED"))
    return "\n".join(lines)


def load_catalog_plan(path: Path) -> tuple[Path, dict[str, Any], str]:
    resolved = path.expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"Cannot read installation plan {resolved}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != PLAN_SCHEMA or not isinstance(payload.get("assets"), list):
        raise DeMethFlowError(f"Unsupported installation plan: {resolved}")
    return resolved, payload, sha256_file(resolved)


def _assert_plan_catalog(plan: dict[str, Any], metadata_dir: Path | None) -> None:
    _catalog, current = _catalog_snapshot(metadata_dir)
    locked = plan.get("catalog", {})
    if not isinstance(locked, dict) or locked.get("canonical_sha256") != current["canonical_sha256"]:
        raise DeMethFlowError("PLAN_CATALOG_CHANGED: locked plan catalog identity differs; generate a new plan")
    if locked.get("sequence") != current["sequence"]:
        raise DeMethFlowError("PLAN_CATALOG_CHANGED: locked plan catalog sequence differs; generate a new plan")


def preflight_catalog_plan(plan_path: Path, *, metadata_dir: Path | None = None) -> tuple[dict[str, Any], int]:
    """Check the real managed filesystems before a plan is allowed to mutate.

    Unknown catalog unpack sizes remain an explicit warning.  They are never
    converted to a fictitious zero-byte requirement.
    """
    _path, plan, digest = load_catalog_plan(plan_path)
    _assert_plan_catalog(plan, metadata_dir)
    home = default_data_home()
    download_root = home / ".methunmix" / "downloads"
    install_root = ModuleStore().root
    download_root.mkdir(parents=True, exist_ok=True)
    install_root.mkdir(parents=True, exist_ok=True)
    download_usage = shutil.disk_usage(download_root)
    install_usage = shutil.disk_usage(install_root)
    space = plan.get("space", {}) if isinstance(plan.get("space"), dict) else {}
    download_required = int(space.get("download_bytes_known") or 0)
    installed_required = int(space.get("installed_bytes_known") or 0)
    reserve = 64 * 1024 * 1024
    same_filesystem = os.stat(download_root).st_dev == os.stat(install_root).st_dev
    if same_filesystem:
        required = download_required + 2 * installed_required + reserve
        sufficient = download_usage.free >= required
    else:
        required = None
        sufficient = download_usage.free >= download_required and install_usage.free >= 2 * installed_required + reserve
    unknown = sorted(set(space.get("download_bytes_unknown_assets", [])) | set(space.get("installed_bytes_unknown_assets", [])))
    report = {
        "schema": "methunmix-installation-space-preflight-v1", "plan_sha256": digest,
        "download_filesystem": {"path": str(download_root), "free_bytes": download_usage.free, "required_known_bytes": download_required},
        "install_filesystem": {"path": str(install_root), "free_bytes": install_usage.free, "required_known_bytes": 2 * installed_required + reserve},
        "same_filesystem": same_filesystem, "combined_required_known_bytes": required,
        "unknown_size_assets": unknown,
        "status": "PASS" if sufficient else "INSUFFICIENT_SPACE",
        "warning": "unknown size entries require runtime ENOSPC handling" if unknown else None,
    }
    return report, 0 if sufficient else 2


def _archive_cache_root() -> Path:
    root = default_data_home() / ".methunmix" / "offline-imports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _install_verified_archive(archive: Path, expected: dict[str, Any]) -> tuple[str, bool]:
    digest = sha256_file(archive)
    if digest != expected.get("archive_sha256"):
        raise DeMethFlowError(f"ARCHIVE_IDENTITY_MISMATCH: archive SHA256 differs for {expected.get('asset_id')}")
    cache = _archive_cache_root() / digest
    if not cache.exists():
        staging = Path(tempfile.mkdtemp(prefix="import-", dir=cache.parent))
        try:
            staged = staging / digest
            shutil.copy2(archive, staged)
            if sha256_file(staged) != digest:
                raise DeMethFlowError("ARCHIVE_IDENTITY_MISMATCH: copied offline archive changed during import")
            os.replace(staged, cache)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    _ensure_cached_archive_sidecar(cache, digest)
    store = ModuleStore()
    existing = [m for m in store.list() if m.module_id == expected.get("asset_id") and m.version == expected.get("version")]
    # A repair is exact only when the existing registration has the same
    # archive identity.  Move the damaged managed payload to recovery before
    # publishing the verified replacement; never overwrite user source paths.
    if len(existing) == 1 and existing[0].payload.get("archive_sha256") == digest and store.verify(existing[0]):
        store.retire_exact(str(expected["asset_id"]), str(expected["version"]))
    module, created = store.install(cache)
    if module.module_id != expected.get("asset_id") or module.version != expected.get("version"):
        raise DeMethFlowError(f"ARCHIVE_MODULE_IDENTITY_MISMATCH: imported archive is not {expected.get('asset_id')}@{expected.get('version')}")
    return module.selector, created


def _write_catalog_installation_receipt(plan: dict[str, Any], plan_digest: str) -> Path:
    """Register a complete exact catalog plan for safe shared-module removal.

    The receipt lives beside legacy offline-lock receipts, but has its own
    schema.  It records only exact catalog assets that were verified, never
    logical requirements or user-supplied runtime paths.  This lets a later
    ``asset remove`` preview find affected catalog plans without treating an
    incomplete plan as protection evidence.
    """
    receipt = installation_receipt_path(plan_digest)
    modules = [
        {
            "module_id": asset.get("asset_id"),
            "version": asset.get("version"),
            "archive_sha256": asset.get("archive_sha256"),
            "content_digest": asset.get("content_digest"),
        }
        for asset in plan.get("assets", [])
        if isinstance(asset, dict)
    ]
    if any(not isinstance(row["module_id"], str) or not isinstance(row["version"], str) for row in modules):
        raise DeMethFlowError("PLAN_INVALID: cannot write receipt for an asset without exact module identity")
    atomic_write_json(receipt, {
        "schema": CATALOG_INSTALLATION_RECEIPT_SCHEMA,
        "plan_sha256": plan_digest,
        "catalog": plan.get("catalog"),
        "selection": plan.get("selection"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "complete": True,
        "modules": modules,
        "scope": "exact catalog asset installation only; logical and user-supplied requirements remain separately declared",
    })
    return receipt


def install_catalog_plan(
    plan_path: Path, *, metadata_dir: Path | None = None,
    external_runtime_root: Path | None = None,
) -> tuple[dict[str, Any], int]:
    path, plan, digest = load_catalog_plan(plan_path)
    if not plan.get("installable"):
        raise DeMethFlowError("PLAN_BLOCKED: inspect plan blockers before installation")
    _assert_plan_catalog(plan, metadata_dir)
    space, space_code = preflight_catalog_plan(path, metadata_dir=metadata_dir)
    if space_code:
        raise DeMethFlowError(
            "INSUFFICIENT_SPACE: plan preflight did not pass; no download or installation was started"
        )
    installed = []
    for asset in plan["assets"]:
        if asset.get("local_state") == "INSTALLED_VERIFIED":
            installed.append({"asset": asset["asset_id"], "status": "ALREADY_INSTALLED"})
            continue
        if asset.get("distribution_status") != "PUBLIC_DOWNLOADABLE":
            raise DeMethFlowError(f"PLAN_BLOCKED: {asset.get('asset_id')} is not publicly installable")
        downloaded = fetch_asset_record(str(asset["asset_id"]), metadata_dir=metadata_dir)
        selector, created = _install_verified_archive(Path(downloaded["path"]), asset)
        installed.append({"asset": asset["asset_id"], "module": selector, "status": "INSTALLED" if created else "ALREADY_INSTALLED"})
    report, code = verify_catalog_plan(
        path, metadata_dir=metadata_dir, external_runtime_root=external_runtime_root,
    )
    report.update({"operation": "INSTALL", "plan_sha256": digest, "installed": installed, "space_preflight": space})
    if code == 0:
        report["receipt"] = str(_write_catalog_installation_receipt(plan, digest))
    return report, code


def import_catalog_plan(plan_path: Path, archives: Iterable[Path], *, metadata_dir: Path | None = None,
                        external_runtime_root: Path | None = None,
                        local_dependency_lock: Path | None = None,
                        local_dependency_locators: Path | None = None) -> tuple[dict[str, Any], int]:
    path, plan, digest = load_catalog_plan(plan_path)
    _assert_plan_catalog(plan, metadata_dir)
    expected = {str(asset.get("archive_sha256")): asset for asset in plan["assets"] if _is_digest(asset.get("archive_sha256"))}
    supplied = list(archives)
    installed = []
    for archive in supplied:
        actual = sha256_file(archive)
        asset = expected.get(actual)
        if asset is None:
            raise DeMethFlowError(f"ARCHIVE_NOT_LOCKED_BY_PLAN: {archive}")
        selector, created = _install_verified_archive(archive, asset)
        installed.append({"asset": asset["asset_id"], "module": selector, "status": "IMPORTED" if created else "ALREADY_INSTALLED"})
    report, code = verify_catalog_plan(
        path, metadata_dir=metadata_dir, external_runtime_root=external_runtime_root,
        local_dependency_lock=local_dependency_lock,
        local_dependency_locators=local_dependency_locators,
    )
    report.update({"operation": "OFFLINE_IMPORT", "plan_sha256": digest, "imported": installed})
    if code == 0:
        report["receipt"] = str(_write_catalog_installation_receipt(plan, digest))
    return report, code


def verify_catalog_plan(plan_path: Path, *, metadata_dir: Path | None = None, external_runtime_root: Path | None = None,
                        local_dependency_lock: Path | None = None,
                        local_dependency_locators: Path | None = None) -> tuple[dict[str, Any], int]:
    _path, plan, digest = load_catalog_plan(plan_path)
    _assert_plan_catalog(plan, metadata_dir)
    store = ModuleStore()
    rows = []
    for asset in plan["assets"]:
        current_target = {
            "asset_id": asset.get("asset_id"), "version": asset.get("version"), "sha256": asset.get("archive_sha256"),
            "distribution_status": asset.get("distribution_status"), "revoked": asset.get("revoked"),
            "revocation_reason": asset.get("revocation_reason"),
        }
        state, detail = _state_for_target(current_target, store)
        rows.append({"asset_id": asset.get("asset_id"), "state": state, "detail": detail, "ok": state == "INSTALLED_VERIFIED"})
    runtime_rows = []
    verified_external_tools: set[str] = set()
    for item in plan.get("external_runtimes", []):
        tool = item.get("tool")
        try:
            found = discover_external_runtimes(external_runtime_root, {str(tool)})
            runtime = found[str(tool)]
            expected = {
                "contract_schema": CONTRACT_SCHEMA,
                "contract_manifest": MANIFEST_NAME,
                "identity": runtime.identity,
                "version": runtime.version,
                "license": runtime.license,
                "content_sha256": runtime.content_sha256,
                "required_files": list(runtime.required_files),
            }
            mismatches = [
                key for key, value in expected.items()
                if item.get(key) != value
            ]
            ok = not mismatches
            if ok:
                verified_external_tools.add(str(tool))
            runtime_rows.append({
                "tool": tool,
                "state": "INSTALLED_VERIFIED" if ok else "IDENTITY_MISMATCH",
                "mismatches": mismatches,
                "ok": ok,
            })
        except DeMethFlowError as exc:
            runtime_rows.append({"tool": tool, "state": "WAITING_FOR_USER_SUPPLIED", "detail": str(exc), "ok": False})
    requirements = [item for item in plan.get("dependency_requirements", []) if isinstance(item, dict)]
    local_rows: list[dict[str, Any]] = []
    local_blockers: list[dict[str, str]] = []
    if plan.get("local_dependencies"):
        selection = plan.get("selection") if isinstance(plan.get("selection"), dict) else {}
        current_selection = dict(selection)
        current_selection["tools"] = list(selection.get("tools_selected", []))
        local_rows, local_blockers = resolve_local_dependencies(
            requirements, current_selection, local_dependency_lock, local_dependency_locators,
        )
    resolved_local = {
        (str(item.get("name")), str(item.get("kind")), item.get("profile") if isinstance(item.get("profile"), str) else None)
        for item in local_rows
    }
    requirement_rows = []
    for item in requirements:
        state = str(item.get("identity_state", "IDENTITY_INSUFFICIENT"))
        key = (str(item.get("name")), str(item.get("kind")), item.get("profile") if isinstance(item.get("profile"), str) else None)
        if key in resolved_local:
            state = "LOCAL_VERIFIED"
        elif _is_verified_external_contract_requirement(item, verified_external_tools):
            state = "EXTERNAL_RUNTIME_CONTRACT_VERIFIED"
        requirement_rows.append({
            "required_by": item.get("required_by"), "name": item.get("name"), "kind": item.get("kind"),
            "state": state, "acquisition": item.get("acquisition"),
            "ok": state in {"RESOLVED", "LOCAL_VERIFIED", "EXTERNAL_RUNTIME_CONTRACT_VERIFIED"},
        })
    report = {
        "schema": "methunmix-installation-plan-verification-v1", "plan_sha256": digest,
        "assets": rows, "external_runtimes": runtime_rows, "dependency_requirements": requirement_rows,
        "local_dependencies": local_rows, "local_dependency_blockers": local_blockers,
        "verification_scope": "module/archive content and user-supplied runtime contract only; no scientific status evaluation or promotion",
    }
    receipt = installation_receipt_path(digest)
    report["receipt"] = str(receipt)
    report["receipt_state"] = "NOT_CREATED"
    if receipt.is_file() and not receipt.is_symlink():
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            report["receipt_state"] = (
                "COMPLETE" if payload.get("schema") == CATALOG_INSTALLATION_RECEIPT_SCHEMA
                and payload.get("complete") is True and payload.get("plan_sha256") == digest
                else "INVALID"
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            report["receipt_state"] = "INVALID"
    report["status"] = "COMPLETE" if not local_blockers and all(row["ok"] for row in [*rows, *runtime_rows, *requirement_rows]) else "INCOMPLETE"
    return report, 0 if report["status"] == "COMPLETE" else 2


def repair_catalog_plan(plan_path: Path, archives: Iterable[Path], *, metadata_dir: Path | None = None, external_runtime_root: Path | None = None) -> tuple[dict[str, Any], int]:
    """Repair exact plan assets from caller-supplied archives only; never upgrade."""
    before, _ = verify_catalog_plan(plan_path, metadata_dir=metadata_dir, external_runtime_root=external_runtime_root)
    imported, _ = import_catalog_plan(
        plan_path, archives, metadata_dir=metadata_dir,
        external_runtime_root=external_runtime_root,
    )
    after, code = verify_catalog_plan(plan_path, metadata_dir=metadata_dir, external_runtime_root=external_runtime_root)
    return {"operation": "REPAIR", "before": before, "import": imported, "after": after, "version_change": "PROHIBITED_EXACT_PLAN_ONLY"}, code


def compare_catalog_plans(previous_plan: Path, next_plan: Path) -> dict[str, Any]:
    old_path, old, old_digest = load_catalog_plan(previous_plan)
    new_path, new, new_digest = load_catalog_plan(next_plan)
    def records(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(row.get("asset_id")): row for row in plan["assets"] if isinstance(row, dict)}
    left, right = records(old), records(new)
    changed = []
    for asset_id in sorted(set(left) & set(right)):
        if (left[asset_id].get("version"), left[asset_id].get("archive_sha256"), left[asset_id].get("content_digest")) != (right[asset_id].get("version"), right[asset_id].get("archive_sha256"), right[asset_id].get("content_digest")):
            changed.append(asset_id)
    return {
        "schema": "methunmix-explicit-upgrade-plan-v1", "status": "UPGRADE_REQUIRES_EXPLICIT_INSTALL_AND_RETIREMENT_REVIEW",
        "previous": {"path": str(old_path), "sha256": old_digest}, "next": {"path": str(new_path), "sha256": new_digest},
        "added": sorted(set(right) - set(left)), "removed_from_next_plan": sorted(set(left) - set(right)), "changed": changed,
        "actions": ["install next exact plan first", "inspect asset remove --dry-run for each old exact module", "no asset is removed or upgraded by this comparison"],
    }
