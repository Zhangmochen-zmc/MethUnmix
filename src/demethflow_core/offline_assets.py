"""Offline-kit planning, exact asset locks, and transactional module sets.

This module intentionally contains no network client.  A bundle is a portable
directory of immutable module archives; online catalog fetch remains a separate
explicit command in :mod:`asset_manager`.
"""

from __future__ import annotations

import json
import hashlib
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import CORE_API, REFERENCE_SCHEMA, RUNTIME_API, version
from .errors import DeMethFlowError
from .external_runtime import EXTERNAL_TOOLS, discover_external_runtimes
from .manifest import (
    CONTRACT_TO_PLATFORM,
    ExecutionProfile,
    ToolCapability,
    resolve_tool_selection,
)
from .modules import ModuleStore
from .util import atomic_write_json, default_data_home, sha256_file, stable_digest


BUNDLE_SCHEMA = "methunmix-offline-bundle-v1"
LOCK_SCHEMA = "methunmix-asset-lock-v1"
INSTALLATION_SCHEMA = "methunmix-installation-set-v1"
CATALOG_INSTALLATION_RECEIPT_SCHEMA = "methunmix-catalog-installation-receipt-v1"
ACTIVE_USE_SCHEMA = "methunmix-module-active-use-v1"
RESOLVER_VERSION = "1"
_ALLOWED_DISTRIBUTION = {"LICENSE_APPROVED", "USER_SUPPLIED_SUPPORTED", "BLOCKED"}
_NATIVE_WGBS_BED_TOOLS = {"CelFiE", "MetDecode", "MEnet"}
_NATIVE_WGBS_PAT_TOOLS = {"CelFEER", "UXM", "MethylBERT"}


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"Cannot read JSON document {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DeMethFlowError(f"JSON document must contain an object: {path}")
    return payload


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeMethFlowError(f"{label} must be a non-empty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or "\\" in value:
        raise DeMethFlowError(f"{label} is not a safe relative POSIX path: {value!r}")
    return path.as_posix()


def _read_archive(archive: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read the module record and, for references, its payload manifest.

    This is intentionally metadata-only.  Archive members are still fully
    validated by ModuleStore before any payload is installed.
    """
    if not archive.is_file() or archive.is_symlink():
        raise DeMethFlowError(f"Offline archive is missing or symlinked: {archive}")
    try:
        with tarfile.open(archive, "r:gz") as handle:
            members = {member.name: member for member in handle.getmembers()}
            module_member = members.get("module.json")
            if module_member is None or not module_member.isfile():
                raise DeMethFlowError(f"Offline archive has no regular module.json: {archive}")
            raw = handle.extractfile(module_member)
            if raw is None:
                raise DeMethFlowError(f"Cannot read module.json from {archive}")
            module = json.loads(raw.read().decode("utf-8"))
            if not isinstance(module, dict) or module.get("schema") != "demethflow-module-v2":
                raise DeMethFlowError(f"Unsupported module archive schema: {archive}")
            reference = None
            if module.get("module_type") == "reference":
                manifest_member = members.get("payload/manifest.json")
                if manifest_member is None or not manifest_member.isfile():
                    raise DeMethFlowError(f"Reference archive has no payload/manifest.json: {archive}")
                raw = handle.extractfile(manifest_member)
                if raw is None:
                    raise DeMethFlowError(f"Cannot read reference manifest from {archive}")
                manifest_bytes = raw.read()
                reference = json.loads(manifest_bytes.decode("utf-8"))
                if not isinstance(reference, dict) or reference.get("schema") != "demethflow-reference-v1":
                    raise DeMethFlowError(f"Unsupported reference manifest in {archive}")
                # Retain the raw-file digest for the offline index.  This is
                # intentionally distinct from ReferenceBundle.content_digest,
                # which is a semantic digest of execution-relevant artifacts.
                reference["_offline_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    except (OSError, tarfile.TarError, UnicodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, DeMethFlowError):
            raise
        raise DeMethFlowError(f"Cannot inspect offline archive {archive}: {exc}") from exc
    return module, reference


def _declared_reference_digest(payload: dict[str, Any]) -> str:
    """Match ReferenceBundle.content_digest without reading payload data files."""
    checksums = payload.get("artifact_checksums", {})
    if not isinstance(checksums, dict):
        checksums = {}
    pairs: list[tuple[str, str]] = []
    capabilities = payload.get("tool_capabilities", {})
    if not isinstance(capabilities, dict):
        raise DeMethFlowError("Reference manifest has no tool_capabilities object")
    for tool, capability in capabilities.items():
        if not isinstance(capability, dict):
            continue
        artifacts = capability.get("artifacts", {})
        if not isinstance(artifacts, dict):
            continue
        for key, relative in artifacts.items():
            if key in {"scientific_validation", "profile_validation", "cpu_runtime_validation"}:
                continue
            if not isinstance(relative, str):
                continue
            expected = checksums.get(relative)
            if expected is None:
                prefix = relative.rstrip("/") + "/"
                nested = [(name, digest) for name, digest in checksums.items() if isinstance(name, str) and name.startswith(prefix)]
                if nested:
                    expected = stable_digest(nested)
            pairs.append((f"{tool}.{key}:{relative}", str(expected or "unverified")))
    for projection_name, keys in (
        ("platform_projection", ("epic_manifest", "hm450_manifest", "common_cpgs", "source_reference_manifest")),
        ("wgbs_projection", ("mapping", "source_reference_manifest")),
    ):
        projection = payload.get(projection_name, {})
        if not isinstance(projection, dict):
            continue
        for name in keys:
            item = projection.get(name, {})
            if isinstance(item, dict) and isinstance(item.get("artifact"), str):
                pairs.append((f"{projection_name}.{name}:{item['artifact']}", str(item.get("sha256", "unverified"))))
        features = projection.get("tool_features", {})
        if isinstance(features, dict):
            for tool, item in features.items():
                if isinstance(item, dict) and isinstance(item.get("artifact"), str):
                    pairs.append((f"{projection_name}.tool_features.{tool}:{item['artifact']}", str(item.get("sha256", "unverified"))))
        source = projection.get("source_reference", {})
        if isinstance(source, dict):
            for key in ("selector", "manifest_sha256", "content_digest"):
                if source.get(key) is not None:
                    pairs.append((f"{projection_name}.source_reference.{key}", str(source[key])))
    return stable_digest(pairs)


def _reference_summary(reference: dict[str, Any]) -> dict[str, Any]:
    required = ("reference_id", "version", "scenario_id", "source_platform", "analysis_contract", "genome_build", "compatible_input_routes", "tool_capabilities")
    missing = [key for key in required if key not in reference]
    if missing:
        raise DeMethFlowError(f"Reference manifest is missing required fields: {','.join(missing)}")
    return {
        "selector": f"{reference['reference_id']}@{reference['version']}",
        "reference_id": reference["reference_id"],
        "version": reference["version"],
        "scenario": reference["scenario_id"],
        "source_platform": reference["source_platform"],
        "analysis_contract": reference["analysis_contract"],
        "genome_build": reference["genome_build"],
        "compatible_input_routes": reference["compatible_input_routes"],
        "content_digest": _declared_reference_digest(reference),
        "manifest_sha256": reference.get("_offline_manifest_sha256"),
        "tool_capabilities": reference["tool_capabilities"],
        "build_modules": reference.get("build_modules", []),
        "build_runtime_modules": reference.get("build_runtime_modules", []),
    }


def create_bundle(destination: Path, archives: list[Path], *, distribution_status: str = "USER_SUPPLIED_SUPPORTED") -> dict[str, Any]:
    if distribution_status not in _ALLOWED_DISTRIBUTION:
        raise DeMethFlowError(f"Unsupported distribution status: {distribution_status}")
    destination = destination.expanduser().resolve()
    if destination.exists():
        if any(destination.iterdir()):
            raise DeMethFlowError(f"Offline bundle destination must be new or empty: {destination}")
    else:
        destination.mkdir(parents=True)
    archive_dir = destination / "archives"
    archive_dir.mkdir(exist_ok=True)
    modules: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw_archive in archives:
        archive = raw_archive.expanduser().resolve()
        module, reference = _read_archive(archive)
        key = (str(module.get("module_id")), str(module.get("version")))
        if key in seen:
            raise DeMethFlowError(f"Duplicate module in offline bundle: {key[0]}@{key[1]}")
        seen.add(key)
        sidecar = Path(f"{archive}.sha256")
        if not sidecar.is_file() or sidecar.is_symlink():
            raise DeMethFlowError(f"Offline archive checksum sidecar is missing or unsafe: {sidecar}")
        digest = sha256_file(archive)
        if digest not in sidecar.read_text(encoding="utf-8").split():
            raise DeMethFlowError(f"Offline archive sidecar does not declare its SHA256: {archive}")
        target = archive_dir / archive.name
        if target.exists():
            raise DeMethFlowError(f"Duplicate offline archive filename: {target.name}")
        shutil.copy2(archive, target)
        shutil.copy2(sidecar, Path(f"{target}.sha256"))
        record: dict[str, Any] = {
            "module_id": module["module_id"],
            "version": module["version"],
            "module_type": module["module_type"],
            "platform": module["platform"],
            "archive": f"archives/{target.name}",
            "sha256": digest,
            "compressed_bytes": target.stat().st_size,
            "installed_bytes": module.get("installed_bytes", 0),
            "compatibility": module.get("compatibility", {}),
            "dependencies": module.get("dependencies", []),
            "exports": module.get("exports", {}),
            "distribution_status": distribution_status,
            "provenance": module.get("provenance", {}),
        }
        if reference is not None:
            record["reference"] = _reference_summary(reference)
        modules.append(record)
    index = {
        "schema": BUNDLE_SCHEMA,
        "core_api": CORE_API,
        "reference_schema": REFERENCE_SCHEMA,
        "runtime_api": RUNTIME_API,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "modules": sorted(modules, key=lambda item: (item["module_id"], item["version"])),
    }
    atomic_write_json(destination / "bundle-index.json", index)
    (destination / "README.md").write_text(
        "# MethUnMix offline asset bundle\n\nInstall only through `methunmix asset install --lock ... --bundle-root ...`.\n",
        encoding="utf-8",
    )
    (destination / "licenses").mkdir(exist_ok=True)
    (destination / "licenses" / "README.md").write_text(
        "Place license evidence and required attribution notices for every LICENSE_APPROVED archive here.\n"
        "USER_SUPPLIED_SUPPORTED and BLOCKED assets must not be represented as public downloads.\n",
        encoding="utf-8",
    )
    return index


def load_bundle(path: Path) -> tuple[Path, dict[str, Any], str]:
    path = path.expanduser().resolve()
    index = _json(path)
    if index.get("schema") != BUNDLE_SCHEMA or not isinstance(index.get("modules"), list):
        raise DeMethFlowError(f"Unsupported offline bundle index: {path}")
    return path.parent, index, sha256_file(path)


def _module_lookup(index: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for item in index["modules"]:
        if not isinstance(item, dict) or not isinstance(item.get("module_id"), str) or not isinstance(item.get("version"), str):
            raise DeMethFlowError("Offline bundle contains an invalid module record")
        key = (item["module_id"], item["version"])
        if key in rows:
            raise DeMethFlowError(f"Offline bundle contains duplicate module record: {key[0]}@{key[1]}")
        rows[key] = item
    return rows


def _select_reference(index: dict[str, Any], *, scenario: str, platform: str, genome_build: str | None, analysis_contract: str, reference: str | None) -> dict[str, Any]:
    candidates = []
    for module in index["modules"]:
        ref = module.get("reference") if isinstance(module, dict) else None
        if not isinstance(ref, dict):
            continue
        if reference and ref.get("selector") != reference:
            continue
        if ref.get("scenario") != scenario or ref.get("analysis_contract") != analysis_contract:
            continue
        if platform not in ref.get("compatible_input_routes", []):
            continue
        if genome_build and ref.get("genome_build") != genome_build:
            continue
        candidates.append(module)
    if len(candidates) != 1:
        selectors = [item.get("reference", {}).get("selector") for item in candidates]
        raise DeMethFlowError(
            f"Offline bundle requires exactly one matching reference; found {len(candidates)}: {selectors}"
        )
    return candidates[0]


def _offline_tool_capabilities(reference: dict[str, Any]) -> dict[str, ToolCapability]:
    """Adapt immutable offline-kit manifest metadata to the shared selector.

    Archive metadata is read before extraction, so planning cannot construct a
    ``ReferenceBundle`` rooted in a local reference store.  The adapter is
    intentionally limited to selection fields; archive/manifest validation is
    still performed by the normal bundle and module checks.
    """
    raw_tools = reference.get("tool_capabilities")
    if not isinstance(raw_tools, dict):
        raise DeMethFlowError("Offline reference has invalid tool capabilities")
    capabilities: dict[str, ToolCapability] = {}
    for name, raw in raw_tools.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise DeMethFlowError("Offline reference has invalid tool capability metadata")
        raw_profiles = raw.get("execution_profiles")
        profiles: dict[str, ExecutionProfile] | None = None
        if raw_profiles is not None:
            if not isinstance(raw_profiles, dict):
                raise DeMethFlowError(f"Offline reference has invalid execution profiles for {name}")
            profiles = {}
            for profile_name, profile in raw_profiles.items():
                if not isinstance(profile_name, str) or not isinstance(profile, dict):
                    raise DeMethFlowError(f"Offline reference has invalid execution profile for {name}")
                profiles[profile_name] = ExecutionProfile(
                    name=profile_name,
                    status=str(profile.get("status", "INVALID")),
                    runtime_modules=tuple(str(item) for item in profile.get("runtime_modules", [])),
                    reason=str(profile["reason"]) if profile.get("reason") is not None else None,
                    validation_policy=profile.get("validation_policy") if isinstance(profile.get("validation_policy"), dict) else None,
                )
        capabilities[name] = ToolCapability(
            name=name,
            status=str(raw.get("status", "INVALID")),
            artifacts=dict(raw.get("artifacts", {})) if isinstance(raw.get("artifacts", {}), dict) else {},
            runtime_modules=tuple(str(item) for item in raw.get("runtime_modules", [])),
            reason=str(raw["reason"]) if raw.get("reason") is not None else None,
            execution_policy=str(raw.get("execution_policy", "normal")),
            execution_profiles=profiles,
            data_modules=tuple(str(item) for item in raw.get("data_modules", [])),
            validation_scope=str(raw["validation_scope"]) if raw.get("validation_scope") is not None else None,
            validation_candidate=str(raw["validation_candidate"]) if raw.get("validation_candidate") is not None else None,
        )
    return capabilities


def _selected_tools(reference: dict[str, Any], requested: str, accelerator: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        return resolve_tool_selection(
            _offline_tool_capabilities(reference),
            requested,
            accelerator=accelerator,
            # Offline installation locks must not provision quarantined
            # candidate validation assets through a normal user plan.
            allow_validation_candidate=False,
            contract=str(reference.get("analysis_contract", "")),
            version=str(reference.get("version", "")),
        )
    except Exception as exc:
        if isinstance(exc, DeMethFlowError):
            raise
        raise DeMethFlowError(f"Offline reference tool selection failed: {exc}") from exc


def _required_module_names(reference: dict[str, Any], selected: list[str], *, accelerator: str, platform: str, genome_build: str | None, input_type: str, mode: str) -> list[tuple[str, str]]:
    requirements: list[tuple[str, str]] = []
    tools = reference["tool_capabilities"]
    for name in selected:
        capability = tools[name]
        profiles = capability.get("execution_profiles")
        profile = profiles.get(accelerator) if isinstance(profiles, dict) and profiles else capability
        runtime_modules = profile.get("runtime_modules", []) if isinstance(profile, dict) else []
        for module_name in runtime_modules:
            requirements.append((str(module_name), f"runtime required by {name} ({accelerator})"))
        for module_name in capability.get("data_modules", []):
            requirements.append((str(module_name), f"tool-data required by {name}"))
    if platform == "wgbs" and input_type == "bam":
        if genome_build not in {"hg19", "hg38"}:
            raise DeMethFlowError("BAM offline planning requires --genome-build hg19 or hg38")
        requirements.extend([
            ("wgbs-common", "BAM materialisation runtime"),
            (f"WGBSTools-{genome_build}-data", f"BAM materialisation data for {genome_build}"),
        ])
    if mode in {"build", "build_and_decon"}:
        for module_name in reference.get("build_runtime_modules", []):
            requirements.append((str(module_name), "reference construction runtime"))
        for module_name in reference.get("build_modules", []):
            requirements.append((str(module_name), "reference construction data"))
    seen: set[str] = set()
    return [(name, reason) for name, reason in requirements if not (name in seen or seen.add(name))]


def _installed_module_states(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """Annotate a plan with exact local installation state without mutation.

    A module must have the exact ID/version/archive digest and pass its own
    declared payload verification before it is treated as reusable. A merely
    present directory is never enough to reduce the predicted requirement.
    """
    try:
        installed = ModuleStore().list()
    except DeMethFlowError:
        installed = []
    by_selector: dict[tuple[str, str], list[Any]] = {}
    for module in installed:
        by_selector.setdefault((module.module_id, module.version), []).append(module)
    download_bytes = 0
    install_bytes = 0
    store = ModuleStore()
    for row in rows:
        key = (str(row["module_id"]), str(row["version"]))
        matches = by_selector.get(key, [])
        healthy = []
        invalid = []
        for module in matches:
            if module.payload.get("archive_sha256") != row.get("sha256"):
                invalid.append("installed archive SHA256 differs from plan")
                continue
            errors = store.verify(module)
            if errors:
                invalid.extend(errors)
            else:
                healthy.append(module)
        if len(healthy) == 1 and not invalid:
            row["installation_state"] = "INSTALLED_HEALTHY"
            row["installation_detail"] = str(healthy[0].install_root)
        elif len(healthy) > 1:
            row["installation_state"] = "AMBIGUOUS_INSTALLED"
            row["installation_detail"] = "multiple installed registrations match the same module selector"
            download_bytes += int(row.get("compressed_bytes") or 0)
            install_bytes += int(row.get("installed_bytes") or 0)
        elif invalid:
            row["installation_state"] = "INSTALLED_INVALID"
            row["installation_detail"] = "; ".join(sorted(set(invalid))[:8])
            download_bytes += int(row.get("compressed_bytes") or 0)
            install_bytes += int(row.get("installed_bytes") or 0)
        else:
            row["installation_state"] = "NOT_INSTALLED"
            row["installation_detail"] = None
            download_bytes += int(row.get("compressed_bytes") or 0)
            install_bytes += int(row.get("installed_bytes") or 0)
    return rows, download_bytes, install_bytes


def _input_contract_errors(*, platform: str, input_type: str, selected: list[str]) -> list[str]:
    """Reject a lock that could never use its chosen WGBS input representation."""
    if platform in {"450k", "epic"}:
        return [] if input_type == "array" else [f"{platform} input requires --input-type array"]
    if input_type == "array":
        return ["WGBS input cannot use --input-type array"]
    errors: list[str] = []
    if input_type == "bed":
        incompatible = sorted(set(selected) & _NATIVE_WGBS_PAT_TOOLS)
        if incompatible:
            errors.append("BED cannot directly run PAT-only tools: " + ",".join(incompatible))
    if input_type == "pat":
        incompatible = sorted(set(selected) & _NATIVE_WGBS_BED_TOOLS)
        if incompatible:
            errors.append("PAT cannot directly run BED-only tools: " + ",".join(incompatible))
    return errors


def create_plan(
    bundle_index: Path,
    *, scenario: str, platform: str, genome_build: str | None, analysis_contract: str,
    input_type: str, tools: str, accelerator: str, mode: str, output: Path,
    reference: str | None = None, external_runtime_root: Path | None = None,
) -> tuple[dict[str, Any], int]:
    bundle_root, index, index_digest = load_bundle(bundle_index)
    if analysis_contract not in CONTRACT_TO_PLATFORM or platform not in {"450k", "epic", "wgbs"}:
        raise DeMethFlowError("Unsupported platform or analysis contract for offline planning")
    if input_type not in {"array", "bam", "bed", "pat"}:
        raise DeMethFlowError("--input-type must be array, bam, bed, or pat")
    module = _select_reference(index, scenario=scenario, platform=platform, genome_build=genome_build, analysis_contract=analysis_contract, reference=reference)
    ref = module["reference"]
    selected, skipped = _selected_tools(ref, tools, accelerator)
    input_errors = _input_contract_errors(platform=platform, input_type=input_type, selected=selected)
    required = [(module["module_id"], "selected reference")]
    required.extend(_required_module_names(ref, selected, accelerator=accelerator, platform=platform, genome_build=genome_build, input_type=input_type, mode=mode))
    records = _module_lookup(index)
    module_rows: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for module_name, reason in required:
        matches = [item for (name, _version), item in records.items() if name == module_name]
        if len(matches) != 1:
            missing.append({"module_id": module_name, "reason": reason, "detail": "missing or ambiguous in offline bundle"})
            continue
        item = matches[0]
        row = {key: item.get(key) for key in ("module_id", "version", "module_type", "platform", "archive", "sha256", "compressed_bytes", "installed_bytes", "compatibility", "distribution_status")}
        row["reason"] = reason
        module_rows.append(row)
    module_rows, download_bytes, install_bytes = _installed_module_states(module_rows)
    external_records: dict[str, dict[str, Any]] = {}
    external_tools = sorted(set(selected) & set(EXTERNAL_TOOLS))
    if external_tools:
        try:
            found = discover_external_runtimes(external_runtime_root, set(external_tools))
        except DeMethFlowError as exc:
            missing.extend({"module_id": f"external-runtime:{tool}", "reason": "external algorithm runtime", "detail": str(exc)} for tool in external_tools)
        else:
            for tool, runtime in found.items():
                external_records[tool] = {
                    "identity": runtime.identity, "version": runtime.version,
                    "license": runtime.license, "required_files": list(runtime.required_files),
                    "content_sha256": runtime.content_sha256,
                }
    blocked = [row for row in module_rows if row.get("distribution_status") == "BLOCKED"]
    installable = not missing and not blocked and not input_errors
    disclosures: dict[str, dict[str, Any]] = {}
    release = ref.get("release_validation", {})
    warnings = release.get("tool_warnings", {}) if isinstance(release, dict) else {}
    for name in selected:
        capability = ref["tool_capabilities"][name]
        profiles = capability.get("execution_profiles")
        profile = profiles.get(accelerator) if isinstance(profiles, dict) and profiles else capability
        disclosures[name] = {
            "status": profile.get("status", capability.get("status")),
            "execution_policy": capability.get("execution_policy", "normal"),
            "validation_scope": capability.get("validation_scope") or (release.get("validation_scope") if isinstance(release, dict) else None),
            "warning": warnings.get(name) if isinstance(warnings, dict) else None,
        }
    selection = {
        "scenario": scenario, "platform": platform, "genome_build": genome_build,
        "analysis_contract": analysis_contract, "input_type": input_type, "tools": selected,
        "accelerator": accelerator, "mode": mode,
    }
    # A fresh installation can retain a compressed archive while extracting
    # its payload, then needs the payload plus a fixed store reserve before an
    # atomic publication. This is deliberately conservative, not a promise
    # about filesystem compression or deduplicated stores.
    reserve_bytes = 64 * 1024 * 1024
    lock = {
        "schema": LOCK_SCHEMA,
        "resolver_version": RESOLVER_VERSION,
        "core_version": version(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bundle_index": {"sha256": index_digest, "schema": index["schema"]},
        "selection": selection,
        "reference": {
            "selector": ref["selector"], "reference_id": ref["reference_id"], "version": ref["version"],
            "content_digest": ref["content_digest"], "manifest_sha256": ref["manifest_sha256"],
            "module_id": module["module_id"], "module_sha256": module["sha256"],
        },
        "modules": module_rows,
        "external_runtimes": external_records,
        "tool_disclosures": disclosures,
        "skipped_tools": skipped,
        "missing": missing,
        "input_contract_errors": input_errors,
        "blocked": [{"module_id": row["module_id"], "reason": row["reason"]} for row in blocked],
        "installable": installable,
        "total_compressed_bytes": sum(int(row.get("compressed_bytes") or 0) for row in module_rows),
        "total_installed_bytes": sum(int(row.get("installed_bytes") or 0) for row in module_rows),
        "installation_estimate": {
            "download_bytes_required": download_bytes,
            "installed_bytes_required": install_bytes,
            "temporary_extract_bytes_required": install_bytes,
            "store_reserve_bytes": reserve_bytes,
            "estimated_peak_additional_bytes": download_bytes + (2 * install_bytes) + reserve_bytes,
            "scope": "conservative fresh-install estimate; excludes user input, Nextflow work directories, and external user-supplied runtimes",
        },
        "bundle_root_hint": bundle_root.name,
    }
    atomic_write_json(output.expanduser().resolve(), lock)
    return lock, 0 if installable else 2


def compare_upgrade_locks(previous_lock_path: Path, new_lock: dict[str, Any]) -> dict[str, Any]:
    """Describe an intentional lock transition without mutating either set.

    Asset upgrades are deliberately *not* in-place operations.  A caller first
    creates a new immutable lock, then may install it beside the old set and
    only later retire an old exact module after receipt-impact review.  This
    comparison makes that boundary visible without treating a new lock as an
    instruction to remove or overwrite anything.
    """
    previous_path, previous, previous_digest = load_lock(previous_lock_path)
    if not isinstance(new_lock.get("reference"), dict) or not isinstance(new_lock.get("modules"), list):
        raise DeMethFlowError("new asset lock is missing reference or module declarations")

    def module_records(lock: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        records: dict[tuple[str, str], dict[str, Any]] = {}
        for item in lock["modules"]:
            if not isinstance(item, dict):
                raise DeMethFlowError("asset lock has an invalid module declaration")
            module_id, module_version = item.get("module_id"), item.get("version")
            if not isinstance(module_id, str) or not isinstance(module_version, str):
                raise DeMethFlowError("asset lock module declaration lacks exact MODULE_ID@VERSION")
            key = (module_id, module_version)
            if key in records:
                raise DeMethFlowError(f"asset lock repeats module {module_id}@{module_version}")
            records[key] = item
        return records

    old_modules = module_records(previous)
    new_modules = module_records(new_lock)
    old_keys, new_keys = set(old_modules), set(new_modules)
    retained = sorted(old_keys & new_keys)
    changed_payload = [
        {
            "selector": f"{module_id}@{module_version}",
            "previous_archive_sha256": old_modules[(module_id, module_version)].get("sha256"),
            "new_archive_sha256": new_modules[(module_id, module_version)].get("sha256"),
        }
        for module_id, module_version in retained
        if old_modules[(module_id, module_version)].get("sha256") != new_modules[(module_id, module_version)].get("sha256")
    ]
    unchanged = [
        f"{module_id}@{module_version}" for module_id, module_version in retained
        if old_modules[(module_id, module_version)].get("sha256") == new_modules[(module_id, module_version)].get("sha256")
    ]
    old_reference, new_reference = previous.get("reference", {}), new_lock.get("reference", {})
    reference_changed = (
        old_reference.get("selector") != new_reference.get("selector")
        or old_reference.get("content_digest") != new_reference.get("content_digest")
        or old_reference.get("module_sha256") != new_reference.get("module_sha256")
    )
    selection_changed = {
        key: {"previous": previous.get("selection", {}).get(key), "new": new_lock.get("selection", {}).get(key)}
        for key in sorted(set(previous.get("selection", {})) | set(new_lock.get("selection", {})))
        if previous.get("selection", {}).get(key) != new_lock.get("selection", {}).get(key)
    }
    return {
        "schema": "methunmix-asset-upgrade-comparison-v1",
        "status": "UPGRADE_REQUIRES_EXPLICIT_NEW_LOCK",
        "previous_lock": {"path": str(previous_path), "sha256": previous_digest},
        "new_lock": {
            "reference_selector": new_reference.get("selector"),
            "reference_content_digest": new_reference.get("content_digest"),
            "installable": new_lock.get("installable"),
        },
        "reference": {
            "changed": reference_changed,
            "previous_selector": old_reference.get("selector"),
            "new_selector": new_reference.get("selector"),
            "previous_content_digest": old_reference.get("content_digest"),
            "new_content_digest": new_reference.get("content_digest"),
        },
        "modules": {
            "added": [f"{module_id}@{module_version}" for module_id, module_version in sorted(new_keys - old_keys)],
            "removed_from_new_lock": [f"{module_id}@{module_version}" for module_id, module_version in sorted(old_keys - new_keys)],
            "changed_archive_payload": changed_payload,
            "unchanged": unchanged,
        },
        "selection_changes": selection_changed,
        "actions": [
            "install the new exact lock before considering any retirement",
            "use asset remove --dry-run to inspect receipt impacts for old modules",
            "asset remove never runs automatically as part of upgrade planning",
        ],
        "scientific_status": "NOT_EVALUATED_NO_PROMOTION",
    }


def load_lock(path: Path) -> tuple[Path, dict[str, Any], str]:
    path = path.expanduser().resolve()
    lock = _json(path)
    if lock.get("schema") != LOCK_SCHEMA or not isinstance(lock.get("modules"), list) or not isinstance(lock.get("selection"), dict):
        raise DeMethFlowError(f"Unsupported asset lock: {path}")
    return path, lock, sha256_file(path)


def _verify_bundle_against_lock(lock: dict[str, Any], bundle_root: Path) -> None:
    index_path = bundle_root / "bundle-index.json"
    _, index, digest = load_bundle(index_path)
    expected = lock.get("bundle_index", {}).get("sha256")
    if digest != expected:
        raise DeMethFlowError("Offline bundle index SHA256 does not match the asset lock")
    rows = _module_lookup(index)
    for locked in lock["modules"]:
        key = (locked["module_id"], locked["version"])
        indexed = rows.get(key)
        if indexed is None or indexed.get("sha256") != locked.get("sha256"):
            raise DeMethFlowError(f"Offline bundle does not contain locked module {key[0]}@{key[1]}")
        relative = _safe_relative(locked.get("archive"), f"archive for {key[0]}")
        archive = bundle_root / relative
        if not archive.is_file() or archive.is_symlink() or sha256_file(archive) != locked["sha256"]:
            raise DeMethFlowError(f"Locked archive is missing or has wrong SHA256: {archive}")


def _locked_modules_installed(lock: dict[str, Any], store: ModuleStore) -> list[dict[str, Any]]:
    installed = {(module.module_id, module.version): module for module in store.list()}
    rows = []
    for locked in lock["modules"]:
        key = (locked["module_id"], locked["version"])
        module = installed.get(key)
        errors = [] if module is not None else ["not installed"]
        if module is not None:
            if module.payload.get("archive_sha256") != locked.get("sha256"):
                errors.append("installed archive SHA256 differs from lock")
            errors.extend(store.verify(module))
        rows.append({"module_id": key[0], "version": key[1], "ok": not errors, "errors": errors})
    return rows


def installation_receipt_path(lock_digest: str) -> Path:
    return default_data_home() / ".methunmix" / "installations" / f"{lock_digest}.json"


def _report_scope(lock: dict[str, Any]) -> dict[str, Any]:
    """Keep installation and scientific declarations visibly non-interchangeable."""
    return {
        "installation_status_scope": "archive/module integrity and exact lock completeness only",
        "scientific_status_scope": "read-only disclosure copied from the locked reference; not evaluated or promoted by asset installation",
        "tool_disclosures": lock.get("tool_disclosures", {}),
    }


def install_lock(lock_path: Path, bundle_root: Path) -> tuple[dict[str, Any], int]:
    _path, lock, lock_digest = load_lock(lock_path)
    if not lock.get("installable"):
        raise DeMethFlowError("Asset lock is incomplete or blocked; inspect its missing/blocked records before installation")
    bundle_root = bundle_root.expanduser().resolve()
    _verify_bundle_against_lock(lock, bundle_root)
    store = ModuleStore()
    # Fail before changing any shared module state when the complete locked
    # payload cannot fit.  ModuleStore repeats its own per-module free-space
    # check immediately before each atomic publication.
    required_bytes = sum(int(item.get("installed_bytes") or 0) for item in lock["modules"])
    available_bytes = shutil.disk_usage(store.root.parent if store.root.exists() else store.root.parent).free
    reserve = 64 * 1024 * 1024
    if available_bytes < required_bytes + reserve:
        raise DeMethFlowError(
            f"Insufficient disk space for complete asset lock: need at least {required_bytes + reserve} bytes, "
            f"have {available_bytes} bytes"
        )
    receipt = installation_receipt_path(lock_digest)
    existing = _locked_modules_installed(lock, store)
    if receipt.is_file() and all(row["ok"] for row in existing):
        return {
            "status": "ALREADY_COMPLETE", "lock_sha256": lock_digest, "modules": existing,
            "receipt": str(receipt), **_report_scope(lock),
        }, 0
    installed_rows = []
    for locked in lock["modules"]:
        archive = bundle_root / _safe_relative(locked["archive"], f"archive for {locked['module_id']}")
        module, changed = store.install(archive)
        installed_rows.append({"module": module.selector, "installed": changed, "path": str(module.install_root)})
    verified = _locked_modules_installed(lock, store)
    if not all(row["ok"] for row in verified):
        return {
            "status": "INCOMPLETE", "lock_sha256": lock_digest, "installed": installed_rows,
            "modules": verified, **_report_scope(lock),
        }, 2
    receipt_payload = {
        "schema": INSTALLATION_SCHEMA, "lock_sha256": lock_digest,
        "completed_at": datetime.now(timezone.utc).isoformat(), "complete": True,
        "modules": verified,
    }
    atomic_write_json(receipt, receipt_payload)
    return {
        "status": "COMPLETE", "lock_sha256": lock_digest, "installed": installed_rows,
        "modules": verified, "receipt": str(receipt), **_report_scope(lock),
    }, 0


def verify_lock(lock_path: Path) -> tuple[dict[str, Any], int]:
    _path, lock, lock_digest = load_lock(lock_path)
    rows = _locked_modules_installed(lock, ModuleStore())
    receipt = installation_receipt_path(lock_digest)
    receipt_ok = False
    if receipt.is_file() and not receipt.is_symlink():
        try:
            receipt_ok = _json(receipt).get("complete") is True
        except DeMethFlowError:
            receipt_ok = False
    reference_ok = False
    reference_error = None
    try:
        from .catalog import ReferenceCatalog

        catalog = ReferenceCatalog.discover(default_data_home() / "references", isolated=True).scan(verify_files=True)
        matching = [bundle for bundle in catalog.bundles if bundle.selector == lock["reference"]["selector"]]
        if len(matching) != 1:
            raise DeMethFlowError(
                f"expected exactly one installed locked reference {lock['reference']['selector']}, found {len(matching)}"
            )
        verify_locked_reference(lock, matching[0])
        reference_ok = True
    except DeMethFlowError as exc:
        reference_error = str(exc)
    result = {
        "lock_sha256": lock_digest, "installable": bool(lock.get("installable")),
        "receipt": str(receipt), "receipt_ok": receipt_ok, "modules": rows,
        "reference_ok": reference_ok, "reference_error": reference_error,
        **_report_scope(lock),
    }
    return result, 0 if result["installable"] and receipt_ok and reference_ok and all(row["ok"] for row in rows) else 2


def _exact_selector(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or value.count("@") != 1:
        raise DeMethFlowError("module selector must be exact MODULE_ID@VERSION")
    module_id, module_version = value.split("@", 1)
    if not module_id or not module_version:
        raise DeMethFlowError("module selector must be exact MODULE_ID@VERSION")
    return module_id, module_version


def _installation_impacts(module_id: str, module_version: str) -> list[dict[str, str]]:
    """Find only managed receipts known to this home; unknown locks remain unknown."""
    receipts = default_data_home() / ".methunmix" / "installations"
    if not receipts.is_dir() or receipts.is_symlink():
        return []
    impacts: list[dict[str, str]] = []
    for receipt in sorted(receipts.glob("*.json")):
        if receipt.is_symlink() or not receipt.is_file():
            continue
        try:
            payload = _json(receipt)
        except DeMethFlowError:
            continue
        if payload.get("schema") not in {INSTALLATION_SCHEMA, CATALOG_INSTALLATION_RECEIPT_SCHEMA}:
            continue
        if payload.get("complete") is not True or not isinstance(payload.get("modules"), list):
            continue
        if any(
            isinstance(row, dict) and row.get("module_id") == module_id and row.get("version") == module_version
            for row in payload["modules"]
        ):
            impacts.append({"receipt": str(receipt), "scope": "known complete managed installation receipt"})
    return impacts


def _active_module_uses(module_id: str, module_version: str) -> tuple[str, list[dict[str, str]]]:
    """Report managed-scope use without making a host-wide idle claim.

    WP-04 does not claim to solve host-wide process tracking.  A future runner
    may create short-lived, schema-bound lease records in this directory; any
    matching active record blocks maintenance retirement even when ``force`` is
    supplied.  An explicit ``IDLE_CONFIRMED`` record is limited to the managed
    lease scope; missing, unsafe, stale, or foreign records never prove that
    an arbitrary workflow is idle.
    """
    root = default_data_home() / ".methunmix" / "active-uses"
    if not root.exists():
        return "UNKNOWN_NO_MANAGED_LEASE_REGISTRY", []
    if root.is_symlink() or not root.is_dir():
        return "UNKNOWN_UNSAFE_LEASE_REGISTRY", []
    active: list[dict[str, str]] = []
    confirmed_idle: list[dict[str, str]] = []
    for path in sorted(root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = _json(path)
        except DeMethFlowError:
            continue
        if payload.get("schema") != ACTIVE_USE_SCHEMA:
            continue
        modules = payload.get("modules")
        if not isinstance(modules, list):
            continue
        matches = any(
            isinstance(row, dict) and row.get("module_id") == module_id and row.get("version") == module_version
            for row in modules
        )
        if not matches:
            continue
        if payload.get("state") == "ACTIVE":
            active.append({"lease": str(path), "scope": "managed active-use record"})
        elif payload.get("state") == "IDLE_CONFIRMED":
            confirmed_idle.append({"lease": str(path), "scope": "managed lease scope only"})
    if active:
        return "KNOWN_IN_USE", active
    if confirmed_idle:
        return "MANAGED_SCOPE_CONFIRMED_IDLE", confirmed_idle
    return "UNKNOWN_NO_CONFIRMED_IDLE_STATE", []


def remove_module(
    selector: str,
    *,
    dry_run: bool = True,
    force: bool = False,
    maintenance: bool = False,
    maintenance_window_confirmed: bool = False,
) -> tuple[dict[str, Any], int]:
    """Preview removal, or retire only during an explicit offline maintenance window.

    Moving a payload into recovery quarantine can still break an active process.
    Daily ``asset remove`` is therefore preview-only.  A separate maintenance
    path requires an explicit maintenance-window acknowledgement and still may
    not override a known active-use lease.
    """
    module_id, module_version = _exact_selector(selector)
    impacts = _installation_impacts(module_id, module_version)
    use_state, active_uses = _active_module_uses(module_id, module_version)
    if dry_run:
        return {
            "status": "DRY_RUN",
            "selector": selector,
            "impacts": impacts,
            "active_use_state": use_state,
            "known_active_uses": active_uses,
            "action": "daily removal is preview-only; use the separate maintenance-retire command only inside an approved offline maintenance window",
        }, 0
    if not maintenance:
        return {
            "status": "BLOCKED_MAINTENANCE_OPERATION_REQUIRED",
            "selector": selector,
            "impacts": impacts,
            "active_use_state": use_state,
            "known_active_uses": active_uses,
            "action": "asset remove never moves payloads; use asset maintenance-retire with an explicit maintenance-window acknowledgement",
        }, 2
    if use_state == "KNOWN_IN_USE":
        return {
            "status": "BLOCKED_KNOWN_ACTIVE_USE",
            "selector": selector,
            "impacts": impacts,
            "active_use_state": use_state,
            "known_active_uses": active_uses,
            "action": "wait until every managed active-use record is closed; --force cannot override known use",
        }, 2
    if not maintenance_window_confirmed or not force:
        return {
            "status": "BLOCKED_MAINTENANCE_CONDITIONS_REQUIRED",
            "selector": selector,
            "impacts": impacts,
            "active_use_state": use_state,
            "known_active_uses": active_uses,
            "action": "unknown host-wide use remains unresolved; explicitly confirm an offline maintenance window and pass --force only after stopping external workflows",
        }, 2
    retired = ModuleStore().retire_exact(module_id, module_version)
    return {
        **retired,
        "impacts": impacts,
        "forced": force,
        "maintenance": True,
        "maintenance_window_confirmed": True,
        "active_use_state": use_state,
        "known_active_uses": active_uses,
    }, 0


def repair_lock(lock_path: Path, bundle_root: Path) -> tuple[dict[str, Any], int]:
    """Restore missing or corrupt modules using only archives bound by a lock.

    Healthy modules are left untouched. Corrupt registrations are retired to
    local recovery quarantine before their exact locked archive is reinstalled.
    The operation never selects a newer reference or module version.
    """
    _path, lock, lock_digest = load_lock(lock_path)
    if not lock.get("installable"):
        raise DeMethFlowError("Asset lock is incomplete or blocked; repair refuses an un-installable lock")
    bundle_root = bundle_root.expanduser().resolve()
    _verify_bundle_against_lock(lock, bundle_root)
    store = ModuleStore()
    installed = {(module.module_id, module.version): module for module in store.list()}
    retired: list[dict[str, Any]] = []
    for row in lock["modules"]:
        key = (str(row["module_id"]), str(row["version"]))
        module = installed.get(key)
        if module is None:
            continue
        bad = module.payload.get("archive_sha256") != row.get("sha256") or bool(store.verify(module))
        if bad:
            retired.append(store.retire_exact(*key))
    report, code = install_lock(lock_path, bundle_root)
    return {
        "status": "REPAIRED" if code == 0 else "REPAIR_INCOMPLETE",
        "lock_sha256": lock_digest,
        "retired_invalid_modules": retired,
        "installation": report,
        "scope": "exact locked archives only; no version selection or scientific status change",
    }, code


def apply_lock_to_selection(lock_path: Path, values: dict[str, Any]) -> dict[str, Any]:
    """Bind CLI execution to a complete, exact installed asset set."""
    _path, lock, _digest = load_lock(lock_path)
    report, code = verify_lock(lock_path)
    if code != 0:
        raise DeMethFlowError(f"Asset lock is not completely installed: {json.dumps(report, ensure_ascii=False)}")
    selection = lock["selection"]
    exact_fields = ("scenario", "platform", "genome_build", "analysis_contract", "accelerator", "mode")
    for field in exact_fields:
        supplied = values.get(field)
        expected = selection.get(field)
        if supplied is not None and supplied != expected:
            raise DeMethFlowError(f"--asset-lock conflicts with --{field.replace('_', '-')}: expected {expected!r}, got {supplied!r}")
        values[field] = expected
    supplied_tools = values.get("tools")
    expected_tools = ",".join(selection["tools"])
    if supplied_tools not in {None, "all", expected_tools}:
        raise DeMethFlowError("--asset-lock conflicts with --tools")
    values["tools"] = expected_tools
    supplied_reference = values.get("reference")
    if supplied_reference not in {None, lock["reference"]["selector"]}:
        raise DeMethFlowError("--asset-lock conflicts with --reference")
    values["reference"] = lock["reference"]["selector"]
    home = default_data_home()
    for field, expected in (("reference_root", home / "references"), ("runtime_root", home)):
        supplied = values.get(field)
        if supplied is not None and supplied.expanduser().resolve() != expected.resolve():
            raise DeMethFlowError(f"--asset-lock conflicts with --{field.replace('_', '-')}")
        values[field] = expected
    values["asset_lock"] = lock
    return values


def verify_locked_reference(lock: dict[str, Any], bundle: Any) -> None:
    reference = lock["reference"]
    if bundle.selector != reference["selector"] or bundle.content_digest() != reference["content_digest"]:
        raise DeMethFlowError("Installed reference selector/content digest does not match the asset lock")


def verify_locked_external_runtimes(lock: dict[str, Any], root: Path | None) -> None:
    expected = lock.get("external_runtimes", {})
    if not expected:
        return
    resolved = discover_external_runtimes(root, set(expected))
    for tool, item in expected.items():
        runtime = resolved.get(tool)
        metadata_keys = ("identity", "version", "license")
        content_matches = (
            "content_sha256" not in item
            or runtime is not None and runtime.content_sha256 == item.get("content_sha256")
        )
        if runtime is None or any(getattr(runtime, key) != item.get(key) for key in metadata_keys) or list(runtime.required_files) != item.get("required_files", []) or not content_matches:
            raise DeMethFlowError(f"External runtime for {tool} does not match the asset lock")
