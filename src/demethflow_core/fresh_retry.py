"""Explicit fresh retry of one terminally failed tool.

This module deliberately accepts structured request fields, never a command
stored in a historical manifest.  It does not expose Nextflow ``-resume`` and
does not reuse an old output/work directory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .asset_lifecycle import create_catalog_plan
from .errors import DeMethFlowError
from .execution_lifecycle import _now
from .runner import DeconRequest, run_decon
from .software_identity import EXECUTION_CONTENT_SCHEMA, current_software_identity
from .util import atomic_write_json, sha256_file


FRESH_RETRY_SCHEMA = "methunmix-fresh-failed-tool-retry-v1"


def _record(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"FRESH_RETRY_{label}_UNREADABLE: {exc}") from exc
    if not isinstance(value, dict):
        raise DeMethFlowError(f"FRESH_RETRY_{label}_INVALID: expected object")
    return value


def _current_software() -> dict[str, object]:
    return current_software_identity()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DeMethFlowError(f"FRESH_RETRY_BLOCKED: {message}")


def _old_effective(manifest: dict[str, Any]) -> dict[str, Any]:
    plan = manifest.get("execution_plan")
    effective = plan.get("effective_request", {}).get("effective_values") if isinstance(plan, dict) else None
    _require(isinstance(effective, dict), "source manifest has no complete effective execution request")
    return effective


def _revalidate_local_dependencies(
    manifest: dict[str, Any], old: dict[str, Any], request: dict[str, Any], *,
    metadata_dir: Path | None, binding_lock: Path | None, locator_overlay: Path | None,
) -> dict[str, Any] | None:
    """Resolve a fresh retry's local bindings instead of copying history."""
    historical = manifest.get("installation_dependency_plan")
    if historical is None:
        _require(binding_lock is None and locator_overlay is None, "source has no local dependency plan; do not supply an unrelated binding document")
        return None
    _require(isinstance(historical, dict), "source local dependency plan is invalid")
    bindings = historical.get("local_dependencies", [])
    _require(isinstance(bindings, list), "source local dependency list is invalid")
    if not bindings:
        _require(binding_lock is None and locator_overlay is None, "source has no local bindings; do not supply an unrelated binding document")
        return None
    _require(binding_lock is not None and locator_overlay is not None, "source used local bindings; provide both current binding lock and locator overlay")
    expected = historical.get("plan_identity")
    _require(isinstance(expected, dict) and isinstance(expected.get("sha256"), str), "source local dependency plan lacks a stable identity")
    contract = old.get("analysis_contract")
    _require(isinstance(contract, str), "source effective request lacks analysis_contract for local binding revalidation")
    genome_build = old.get("genome_build")
    _require(genome_build is None or isinstance(genome_build, str), "source effective request has invalid genome_build")
    current, code = create_catalog_plan(
        scenario=str(request["scenario"]), platform=str(request["platform"]), genome_build=genome_build,
        analysis_contract=contract, tools=",".join(request["tools"]), accelerator=str(request["accelerator"]),
        reference=str(request["reference"]), metadata_dir=metadata_dir,
        local_dependency_lock=binding_lock, local_dependency_locators=locator_overlay,
    )
    _require(code == 0 and current.get("installable") is True, "current local dependency plan is blocked")
    current_identity = current.get("plan_identity")
    _require(isinstance(current_identity, dict) and current_identity.get("sha256") == expected.get("sha256"), "current local dependency plan identity differs from source")
    return current


def _require_software_compatibility(old: object, current: object) -> dict[str, object]:
    """Fail closed unless independently recorded execution content matches.

    A commit is unavailable after normal wheel installation, so it is retained
    as provenance rather than used as a surrogate artifact identity.  Legacy
    records without execution content cannot prove migration compatibility.
    """
    _require(isinstance(old, dict) and isinstance(current, dict), "source/current software identity is invalid")
    _require(old.get("core_version") == current.get("core_version"), "core version is incompatible with source")
    old_content = old.get("execution_content_identity")
    current_content = current.get("execution_content_identity")
    _require(isinstance(old_content, dict), "source software has no execution-content identity; migration compatibility is not established")
    _require(isinstance(current_content, dict), "current software has no execution-content identity")
    _require(old_content.get("schema") == EXECUTION_CONTENT_SCHEMA, "source execution-content identity schema is unsupported")
    _require(current_content.get("schema") == EXECUTION_CONTENT_SCHEMA, "current execution-content identity schema is unsupported")
    old_sha, current_sha = old_content.get("sha256"), current_content.get("sha256")
    _require(isinstance(old_sha, str) and len(old_sha) == 64, "source execution-content digest is invalid")
    _require(isinstance(current_sha, str) and len(current_sha) == 64, "current execution-content digest is invalid")
    _require(old_sha == current_sha, "execution content differs; migration compatibility is not established")
    return {
        "core_version": current.get("core_version"),
        "execution_content_identity": current_content,
        "source_commit_provenance": {
            "source": old.get("source_commit"),
            "current": current.get("source_commit"),
            "source_status": old.get("source_commit_status"),
            "current_status": current.get("source_commit_status"),
        },
    }


def validate_fresh_retry(
    source_run_dir: Path, tool: str, new_outdir: Path, request: dict[str, Any], *,
    catalog_metadata_dir: Path | None = None, local_dependency_lock: Path | None = None,
    local_dependency_locators: Path | None = None,
    current_software: Callable[[], dict[str, object]] = _current_software,
) -> dict[str, Any]:
    """Validate a new, standalone retry without creating files or processes."""
    source = source_run_dir.expanduser().resolve()
    target = new_outdir.expanduser().resolve(strict=False)
    manifest = _record(source / "run_manifest.json", "SOURCE_MANIFEST")
    receipt = _record(source / ".demethflow" / "task_receipt.json", "SOURCE_RECEIPT")
    lease = manifest.get("run_lease")
    _require(manifest.get("status") in {"FAILED", "TIMEOUT"}, "source run is not terminally failed or timed out")
    _require(manifest.get("selected_tools") == [tool], "source run must contain exactly the explicitly failed tool")
    _require(receipt.get("process_start_state") == "CONFIRMED_STARTED", "source process creation is not confirmed")
    _require(receipt.get("orphan_free") is True and receipt.get("remaining_group_members") == [], "source receipt does not confirm cleanup")
    _require(isinstance(lease, dict) and lease.get("state") == "IDLE_CONFIRMED", "source managed lease is not confirmed idle")
    _require(not target.exists(), "new output directory already exists")
    _require(not target.parent.is_symlink(), "new output parent may not be symlinked")
    _require(request.get("tools") == [tool], "retry request must select only the failed tool")
    _require(request.get("resume") in (None, False), "retry request may not enable resume")
    _require(request.get("step_cache_root") in (None, ""), "retry request may not reuse a step cache")
    for key in (
        "scenario", "platform", "reference", "input", "runtime", "container_engine", "executor",
        "accelerator", "random_seed", "reference_root", "runtime_root", "workflow_timeout_seconds",
    ):
        _require(key in request, f"retry request is missing {key}")
    old = _old_effective(manifest)
    old_keys = {"scenario": "scenario", "platform": "input_platform", "reference": "reference", "runtime": "runtime", "container_engine": "container_engine", "executor": "executor", "accelerator": "accelerator"}
    for key, old_key in old_keys.items():
        _require(request[key] == old.get(old_key), f"request {key} differs from source effective request")
    _require(request["random_seed"] == old.get("parameters", {}).get("random_seed"), "random_seed differs from source effective request")
    old_timeout = manifest.get("execution_environment", {}).get("workflow_timeout_seconds")
    _require(
        request["workflow_timeout_seconds"] == old_timeout,
        "workflow_timeout_seconds differs from source supervised execution",
    )
    if tool == "MeDeCom":
        old_params = manifest.get("resolved_execution_parameters", {}).get("MeDeCom", {})
        for key, old_key in (("medecom_k", "k"), ("medecom_lambda", "lambda"), ("medecom_nfolds", "nfolds")):
            _require(request.get(key) == old_params.get(old_key), f"{key} differs from source effective parameters")
    input_path = Path(str(request["input"])).expanduser().resolve()
    _require(input_path.is_file() and not input_path.is_symlink(), "retry input must be an existing regular file")
    old_input = manifest.get("input_identity", {})
    _require(sha256_file(input_path) == old_input.get("sha256"), "retry input digest differs from source")
    software = _require_software_compatibility(manifest.get("software_identity"), current_software())
    local_dependency_plan = _revalidate_local_dependencies(
        manifest, old, request, metadata_dir=catalog_metadata_dir,
        binding_lock=local_dependency_lock, locator_overlay=local_dependency_locators,
    )
    return {
        "schema": FRESH_RETRY_SCHEMA,
        "mode": "PREVIEW_ONLY" if not request.get("execute") else "EXECUTE_FRESH_RUN",
        "decision": "FRESH_RUN_ALLOWED" if request.get("execute") else "FRESH_RUN_REQUIRES_EXPLICIT_EXECUTE",
        "source_run_dir": str(source),
        "source_terminal_status": manifest.get("status"),
        "failed_tool": tool,
        "new_outdir": str(target),
        "identity_checks": {"input_sha256": old_input.get("sha256"), "software": software, "reference_digest": manifest.get("reference_digest"), "local_dependency_plan": local_dependency_plan.get("plan_identity") if local_dependency_plan else None},
        "current_installation_dependency_plan": local_dependency_plan,
        "explicitly_not_done": ["does not execute a manifest command", "does not pass -resume", "does not reuse partial output or old work directories", "does not rerun successful tools"],
    }


def execute_fresh_retry(
    source_run_dir: Path, tool: str, new_outdir: Path, request: dict[str, Any], *,
    catalog_metadata_dir: Path | None = None, local_dependency_lock: Path | None = None,
    local_dependency_locators: Path | None = None,
    current_software: Callable[[], dict[str, object]] = _current_software,
) -> dict[str, Any]:
    """Create one fresh directory and invoke the public runner after validation."""
    request = dict(request)
    request["execute"] = True
    preview = validate_fresh_retry(
        source_run_dir, tool, new_outdir, request, catalog_metadata_dir=catalog_metadata_dir,
        local_dependency_lock=local_dependency_lock, local_dependency_locators=local_dependency_locators,
        current_software=current_software,
    )
    target = new_outdir.expanduser().resolve(strict=False)
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise DeMethFlowError("FRESH_RETRY_BLOCKED: new output directory was claimed concurrently") from exc
    linkage = {
        **preview,
        "started_at": _now(),
        "prior_run_readonly": True,
        "prior_result_reference": {"source_run_dir": str(source_run_dir.expanduser().resolve()), "mode": "READ_ONLY"},
    }
    atomic_write_json(target / "fresh-retry-attempt.json", linkage)
    run_request = DeconRequest(
        input_path=Path(str(request["input"])), outdir=target, platform=str(request["platform"]),
        scenario=str(request["scenario"]), reference=str(request["reference"]), tools=tool,
        runtime=str(request["runtime"]), container_engine=str(request["container_engine"]),
        executor=str(request["executor"]), accelerator=str(request["accelerator"]),
        reference_root=Path(str(request["reference_root"])), runtime_root=Path(str(request["runtime_root"])),
        verify_reference_checksums=bool(request.get("verify_reference_checksums", True)),
        random_seed=int(request["random_seed"]), medecom_k=request.get("medecom_k"),
        medecom_lambda=request.get("medecom_lambda", "auto"), medecom_nfolds=request.get("medecom_nfolds"),
        workflow_timeout_seconds=request["workflow_timeout_seconds"], resume=False, step_cache_root=None,
        installation_dependency_plan=preview.get("current_installation_dependency_plan"),
    )
    try:
        manifest = run_decon(run_request)
        linkage.update(terminal_state="RUNNER_RETURNED", new_manifest=str(target / "run_manifest.json"))
        atomic_write_json(target / "fresh-retry-attempt.json", linkage)
        return manifest
    except BaseException as exc:
        terminal = "RUNNER_INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "RUNNER_RAISED"
        linkage.update(terminal_state=terminal, error=f"{type(exc).__name__}: {exc}")
        atomic_write_json(target / "fresh-retry-attempt.json", linkage)
        raise
