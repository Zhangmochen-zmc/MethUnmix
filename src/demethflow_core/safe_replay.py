"""Explicit, identity-bound execution of a prepared replay plan.

``replay-plan`` remains a display-only producer.  This module is its narrow
consumer: it accepts structured JSON records, reconstructs a ``DeconRequest``
from allow-listed fields, and invokes the normal runner.  It deliberately
never interprets a historical command, environment, executable path, or shell
fragment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .asset_lifecycle import create_catalog_plan
from .errors import DeMethFlowError
from .execution_lifecycle import _now
from .reporting import REPLAY_SCHEMA, build_report
from .runner import DeconRequest, _input_identity, prepare_decon, run_decon
from .software_identity import EXECUTION_CONTENT_SCHEMA, current_software_identity
from .util import atomic_write_json, sha256_file


SAFE_REPLAY_SCHEMA = "methunmix-safe-replay-execution-v1"
RESOLVED_REQUEST_SCHEMA = "methunmix-resolved-analysis-request-v1"
_ARRAY_PLATFORMS = {"450k", "epic"}
_WGBS_DERIVED_CONTRACTS = {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}
_VALID_GENOME_BUILDS = {"hg19", "hg38"}


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"SAFE_REPLAY_{label}_INVALID: {exc}") from exc
    if not isinstance(value, dict):
        raise DeMethFlowError(f"SAFE_REPLAY_{label}_INVALID: expected JSON object")
    return value


def _blocked(condition: bool, detail: str) -> None:
    if not condition:
        raise DeMethFlowError(f"SAFE_REPLAY_BLOCKED: {detail}")


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_target(source: Path, target: Path) -> Path:
    raw = target.expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    _blocked(not raw.exists(), "new output directory already exists")
    # Inspect existing ancestors before resolve() would hide a symlink.
    ancestor = raw.parent
    while ancestor != ancestor.parent:
        if ancestor.exists() and ancestor.is_symlink():
            raise DeMethFlowError("SAFE_REPLAY_BLOCKED: output parent may not be symlinked")
        ancestor = ancestor.parent
    resolved = raw.resolve(strict=False)
    _blocked(resolved != Path(resolved.anchor), "output directory may not be a filesystem root")
    _blocked(not _is_under(resolved, source) and not _is_under(source, resolved), "output directory may not overlap the source run")
    return resolved


def _source_effective(manifest: dict[str, Any]) -> dict[str, Any]:
    plan = manifest.get("execution_plan")
    request = plan.get("effective_request") if isinstance(plan, dict) else None
    effective = request.get("effective_values") if isinstance(request, dict) else None
    _blocked(isinstance(effective, dict), "source manifest lacks an effective execution request")
    schema = request.get("schema") if isinstance(request, dict) else None
    _blocked(schema in {None, RESOLVED_REQUEST_SCHEMA}, "source effective execution request schema is unsupported")
    return effective


def _field(values: dict[str, Any], key: str, manifest: dict[str, Any], fallback_key: str | None = None) -> Any:
    """Read an allow-listed execution field without collapsing falsey values.

    A resolved request is authoritative whenever it records a key, including
    JSON ``null``, ``false``, zero, and an empty value.  The manifest fallback
    exists only for legacy records that genuinely omitted that key.
    """
    if key in values:
        return values[key]
    return manifest.get(fallback_key or key)


def _string_field(values: dict[str, Any], key: str, manifest: dict[str, Any], fallback_key: str | None = None) -> str | None:
    value = _field(values, key, manifest, fallback_key)
    _blocked(value is None or isinstance(value, str), f"source {key} has an invalid type")
    return value


def _source_genome_build(values: dict[str, Any], manifest: dict[str, Any], platform: str | None, contract: str | None) -> tuple[str | None, str | None]:
    """Restore the effective build with route-aware legacy compatibility.

    ``manifest.genome_build`` is a presentation value for array records (often
    ``not_applicable``), never an executable CLI value.  It can only be
    interpreted for an old array record that omitted the resolved field.
    """
    _blocked(platform in _ARRAY_PLATFORMS | {"wgbs"}, "source input platform is unsupported")
    _blocked(contract is None or isinstance(contract, str), "source analysis contract has an invalid type")
    has_effective = "genome_build" in values
    effective = values.get("genome_build") if has_effective else None
    requested = values.get("genome_build_requested") if "genome_build_requested" in values else None
    if platform in _ARRAY_PLATFORMS:
        # Old array manifests used only the display field.  Its sole accepted
        # value is the documented presentation marker; it becomes no CLI arg.
        if not has_effective:
            display = manifest.get("genome_build")
            _blocked(display in {None, "not_applicable"}, "legacy array source genome build is ambiguous")
            effective = None
        _blocked(effective is None, "array source must not carry an executable genome build")
        _blocked(requested is None, "array source must not carry a requested genome build")
        return None, None
    _blocked(isinstance(effective, str) and effective in _VALID_GENOME_BUILDS, "WGBS source genome build must be hg19 or hg38")
    if requested is not None:
        _blocked(isinstance(requested, str) and requested in _VALID_GENOME_BUILDS, "source requested genome build must be hg19 or hg38")
    if contract in _WGBS_DERIVED_CONTRACTS:
        _blocked(requested in _VALID_GENOME_BUILDS, "WGBS-derived source lacks an explicit genome build")
        _blocked(requested == effective, "WGBS-derived source genome build conflicts with its effective build")
    return effective, requested


def _source_configuration(manifest: dict[str, Any]) -> dict[str, Any]:
    values = _source_effective(manifest)
    if "parameters" in values:
        parameters_value = values["parameters"]
        _blocked(isinstance(parameters_value, dict), "source parameters have an invalid type")
        parameters = parameters_value
    else:
        parameters = {}
    reference = _string_field(values, "reference", manifest)
    if reference is None:
        reference_id, reference_version = manifest.get("reference_id"), manifest.get("reference_version")
        _blocked(isinstance(reference_id, str) and isinstance(reference_version, str), "source reference is missing")
        reference = f"{reference_id}@{reference_version}"
    tools = _string_field(values, "tools_requested", manifest)
    if tools is None:
        selected = manifest.get("selected_tools")
        _blocked(isinstance(selected, list) and all(isinstance(item, str) for item in selected) and bool(selected), "source tools are missing")
        tools = ",".join(selected)
    platform = _string_field(values, "input_platform", manifest)
    contract = _string_field(values, "analysis_contract", manifest)
    genome_build, requested_genome_build = _source_genome_build(values, manifest, platform, contract)
    allow_cross_platform = _field(values, "allow_cross_platform", manifest)
    _blocked(isinstance(allow_cross_platform, bool), "source allow_cross_platform has an invalid type")
    random_seed = parameters["random_seed"] if "random_seed" in parameters else manifest.get("random_seed")
    _blocked(isinstance(random_seed, int) and not isinstance(random_seed, bool), "source random seed is missing or invalid")
    return {
        "scenario": _string_field(values, "scenario", manifest, "scenario_id"),
        "platform": platform,
        "genome_build": genome_build,
        "requested_genome_build": requested_genome_build,
        "analysis_contract": contract,
        "reference": reference,
        "tools": tools,
        "accelerator": _string_field(values, "accelerator", manifest),
        "random_seed": random_seed,
        "runtime": _string_field(values, "runtime", manifest),
        "container_engine": _string_field(values, "container_engine", manifest),
        "executor": _string_field(values, "executor", manifest),
        "allow_cross_platform": allow_cross_platform,
        "parameters": parameters,
    }


def _require_software(source: object, current: object) -> dict[str, object]:
    _blocked(isinstance(source, dict) and isinstance(current, dict), "software identity is invalid")
    _blocked(source.get("core_version") == current.get("core_version"), "core version differs from source")
    old = source.get("execution_content_identity")
    new = current.get("execution_content_identity")
    _blocked(isinstance(old, dict), "source software has no execution-content identity; cross-version replay is unsupported")
    _blocked(isinstance(new, dict), "current software has no execution-content identity")
    _blocked(old.get("schema") == EXECUTION_CONTENT_SCHEMA and new.get("schema") == EXECUTION_CONTENT_SCHEMA, "execution-content identity schema is unsupported")
    old_sha, new_sha = old.get("sha256"), new.get("sha256")
    _blocked(isinstance(old_sha, str) and len(old_sha) == 64, "source execution-content digest is invalid")
    _blocked(isinstance(new_sha, str) and len(new_sha) == 64, "current execution-content digest is invalid")
    _blocked(old_sha == new_sha, "execution content differs despite matching version")
    return {"source": source, "current": current, "status": "MATCH"}


def _expected_plan(plan: dict[str, Any], manifest_path: Path, target: Path, source: dict[str, Any]) -> dict[str, Any]:
    _blocked(plan.get("schema") == REPLAY_SCHEMA, "replay plan schema is unsupported")
    _blocked(plan.get("status") == "PREPARED_NOT_EXECUTED", "replay plan is not prepared for execution")
    evidence = plan.get("source_manifest")
    _blocked(isinstance(evidence, dict) and evidence.get("sha256") == sha256_file(manifest_path), "replay plan is not bound to this source manifest")
    _blocked(plan.get("resume") is False, "replay plan must explicitly disable resume")
    _blocked(Path(str(plan.get("new_output_directory", ""))).expanduser().resolve(strict=False) == target, "replay plan target differs from --outdir")
    controlled = plan.get("controlled_configuration")
    _blocked(isinstance(controlled, dict), "replay plan lacks controlled configuration")
    required = ("scenario", "platform", "analysis_contract", "reference", "tools", "accelerator", "random_seed")
    for key in required:
        _blocked(controlled.get(key) == source.get(key), f"replay plan {key} differs from source manifest")
    return controlled


def _revalidate_source_local_dependencies(
    manifest: dict[str, Any], source: dict[str, Any], *, metadata_dir: Path | None,
    binding_lock: Path | None, locator_overlay: Path | None,
) -> dict[str, Any] | None:
    """Re-resolve a source's exact local bindings without trusting its record.

    Old successful records which did not request catalog binding retain their
    established replay behavior.  A record that did request local bindings is
    replayable only when the caller supplies a fresh lock and overlay whose
    resolved stable plan identity exactly matches the source record.
    """
    historical = manifest.get("installation_dependency_plan")
    if historical is None:
        _blocked(binding_lock is None and locator_overlay is None, "source has no local dependency plan; do not supply an unrelated binding document")
        return None
    _blocked(isinstance(historical, dict), "source local dependency plan is invalid")
    bindings = historical.get("local_dependencies", [])
    _blocked(isinstance(bindings, list), "source local dependency list is invalid")
    if not bindings:
        _blocked(binding_lock is None and locator_overlay is None, "source has no local bindings; do not supply an unrelated binding document")
        return None
    _blocked(binding_lock is not None and locator_overlay is not None, "source used local bindings; provide both current binding lock and locator overlay")
    expected_identity = historical.get("plan_identity")
    _blocked(isinstance(expected_identity, dict) and isinstance(expected_identity.get("sha256"), str), "source local dependency plan lacks a stable identity")
    current, code = create_catalog_plan(
        scenario=str(source["scenario"]), platform=str(source["platform"]), genome_build=source.get("genome_build"),
        analysis_contract=str(source["analysis_contract"]), tools=str(source["tools"]),
        accelerator=str(source["accelerator"]), reference=str(source["reference"]),
        metadata_dir=metadata_dir, local_dependency_lock=binding_lock, local_dependency_locators=locator_overlay,
    )
    _blocked(code == 0 and current.get("installable") is True, "current local dependency plan is blocked")
    current_identity = current.get("plan_identity")
    _blocked(isinstance(current_identity, dict) and current_identity.get("sha256") == expected_identity.get("sha256"), "current local dependency plan identity differs from source")
    return {
        "state": "REVALIDATED_MATCH",
        "source_plan_identity": expected_identity,
        "current_plan_identity": current_identity,
        "current_plan": current,
        "scope": "current lock/overlay and catalog resolution match the source local dependency plan; locators are intentionally non-portable and not copied from history",
    }


def _request_from_source(source: dict[str, Any], *, input_path: Path, target: Path, reference_root: Path, runtime_root: Path, external_runtime_root: Path | None, timeout_seconds: int, installation_dependency_plan: dict[str, Any] | None = None) -> DeconRequest:
    parameters = source["parameters"]
    _blocked(isinstance(source.get("platform"), str) and isinstance(source.get("reference"), str), "source configuration is incomplete")
    _blocked(isinstance(source.get("runtime"), str) and isinstance(source.get("container_engine"), str) and isinstance(source.get("executor"), str), "source runtime configuration is incomplete")
    _blocked(isinstance(source.get("random_seed"), int), "source random seed is missing or invalid")
    _blocked(timeout_seconds > 0, "--workflow-timeout-seconds must be a positive explicit bound")
    return DeconRequest(
        input_path=input_path, outdir=target, platform=source["platform"], scenario=source["scenario"],
        reference=source["reference"], tools=source["tools"], runtime=source["runtime"],
        container_engine=source["container_engine"], executor=source["executor"], accelerator=source["accelerator"],
        reference_root=reference_root, runtime_root=runtime_root, external_runtime_root=external_runtime_root,
        allow_cross_platform=bool(source["allow_cross_platform"]), resume=False, step_cache_root=None,
        verify_reference_checksums=True, random_seed=source["random_seed"],
        medecom_k=parameters.get("medecom_k"), medecom_lambda=parameters.get("medecom_lambda", "auto"),
        medecom_nfolds=parameters.get("medecom_nfolds"), methylcibersort_permutations=parameters.get("methylcibersort_permutations", 1000),
        genome_build=source.get("genome_build"), requested_genome_build=source.get("requested_genome_build"),
        analysis_contract=source.get("analysis_contract"), preprocess_threads=parameters.get("preprocess_threads", 4),
        preprocess_sort_memory=parameters.get("preprocess_sort_memory", "768M"),
        bam_contig_policy=parameters.get("bam_contig_policy", "strict"), workflow_timeout_seconds=timeout_seconds,
        installation_dependency_plan=installation_dependency_plan,
    )


def _verify_current_assets(manifest: dict[str, Any], result: object) -> dict[str, Any]:
    bundle = getattr(result, "bundle", None)
    _blocked(bundle is not None, "current preflight did not resolve a reference")
    _blocked(
        list(getattr(result, "selected_tools", [])) == list(manifest.get("selected_tools", [])),
        "current selection differs from the source selected tools",
    )
    _blocked(bundle.content_digest() == manifest.get("reference_digest"), "reference content digest differs from source")
    expected = manifest.get("runtime_images")
    _blocked(isinstance(expected, dict), "source manifest lacks runtime identities")
    runtime_files = getattr(result, "runtime_files", {})
    current = {name: sha256_file(path) for name, path in runtime_files.items()}
    expected_hashes = {str(name): item.get("sha256") for name, item in expected.items() if isinstance(item, dict)}
    _blocked(expected_hashes == current, "runtime image identity differs from source")
    expected_external = manifest.get("external_runtime")
    current_external = {
        "metadata": getattr(result, "external_runtime_metadata", {}),
        "tools": sorted(getattr(result, "external_runtime_paths", {}).keys()),
    }
    if isinstance(expected_external, dict) and (expected_external.get("paths") or expected_external.get("metadata")):
        _blocked(expected_external.get("metadata") == current_external["metadata"] and sorted(expected_external.get("paths", {}).keys()) == current_external["tools"], "external runtime identity differs or is insufficient")
    return {"reference_digest": bundle.content_digest(), "runtime_images": current, "external_runtime": current_external}


def validate_safe_replay(
    *, replay_plan_path: Path, source_manifest_path: Path, input_path: Path, outdir: Path,
    reference_root: Path, runtime_root: Path, external_runtime_root: Path | None, timeout_seconds: int,
    catalog_metadata_dir: Path | None = None, local_dependency_lock: Path | None = None,
    local_dependency_locators: Path | None = None,
    execute: bool = False, current_software: Callable[[], dict[str, object]] = current_software_identity,
) -> dict[str, Any]:
    """Perform structural, content and path checks without creating an output."""
    manifest_path = source_manifest_path.expanduser().resolve()
    source_dir = manifest_path.parent
    manifest = _read_object(manifest_path, "SOURCE_MANIFEST")
    plan = _read_object(replay_plan_path, "PLAN")
    target = _safe_target(source_dir, outdir)
    _blocked(manifest.get("status") == "SUCCEEDED" and manifest.get("execution_status") == "SUCCEEDED", "source run is not a successful terminal run")
    receipt = _read_object(source_dir / ".demethflow" / "task_receipt.json", "SOURCE_RECEIPT")
    _blocked(receipt.get("process_start_state") == "CONFIRMED_STARTED", "source workflow start is not confirmed")
    _blocked(receipt.get("orphan_free") is True and receipt.get("remaining_group_members") == [], "source cleanup is not confirmed")
    lease = manifest.get("run_lease")
    _blocked(isinstance(lease, dict) and lease.get("state") == "IDLE_CONFIRMED", "source lease is not confirmed idle")
    source = _source_configuration(manifest)
    _expected_plan(plan, manifest_path, target, source)
    current_input = input_path.expanduser().resolve()
    _blocked(current_input.exists() and not current_input.is_symlink(), "current input must be an existing non-symlink path")
    _blocked(_input_identity(current_input) == manifest.get("input_identity"), "current input content identity differs from source")
    software = _require_software(manifest.get("software_identity"), current_software())
    local_dependency_revalidation = _revalidate_source_local_dependencies(
        manifest, source, metadata_dir=catalog_metadata_dir, binding_lock=local_dependency_lock,
        locator_overlay=local_dependency_locators,
    )
    return {
        "schema": SAFE_REPLAY_SCHEMA,
        "mode": "EXECUTE_SAFE_REPLAY" if execute else "PREVIEW_ONLY",
        "decision": "SAFE_REPLAY_ALLOWED" if execute else "SAFE_REPLAY_REQUIRES_EXPLICIT_EXECUTE",
        "source_run_dir": str(source_dir), "source_manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
        "replay_plan": {"path": str(replay_plan_path.expanduser().resolve()), "sha256": sha256_file(replay_plan_path.expanduser())},
        "new_outdir": str(target), "software": software, "input_identity": manifest.get("input_identity"),
        "controlled_configuration": source, "workflow_timeout_seconds": timeout_seconds,
        "local_dependency_revalidation": local_dependency_revalidation,
        "explicitly_not_done": ["does not execute any manifest or plan command", "does not use Nextflow resume", "does not reuse partial output or work directories"],
    }


def execute_safe_replay(
    *, replay_plan_path: Path, source_manifest_path: Path, input_path: Path, outdir: Path,
    reference_root: Path, runtime_root: Path, external_runtime_root: Path | None, timeout_seconds: int,
    catalog_metadata_dir: Path | None = None, local_dependency_lock: Path | None = None,
    local_dependency_locators: Path | None = None,
    report_output_dir: Path | None = None, current_software: Callable[[], dict[str, object]] = current_software_identity,
    preflight: Callable[[DeconRequest], object] = prepare_decon, runner: Callable[[DeconRequest], dict[str, Any]] = run_decon,
    reporter: Callable[..., dict[str, Any]] = build_report,
) -> dict[str, Any]:
    """Claim a new directory, revalidate current assets, then call ``run_decon``."""
    preview = validate_safe_replay(
        replay_plan_path=replay_plan_path, source_manifest_path=source_manifest_path, input_path=input_path,
        outdir=outdir, reference_root=reference_root, runtime_root=runtime_root, external_runtime_root=external_runtime_root,
        timeout_seconds=timeout_seconds, catalog_metadata_dir=catalog_metadata_dir,
        local_dependency_lock=local_dependency_lock, local_dependency_locators=local_dependency_locators,
        execute=True, current_software=current_software,
    )
    target = Path(str(preview["new_outdir"]))
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise DeMethFlowError("SAFE_REPLAY_BLOCKED: new output directory was claimed concurrently") from exc
    attempt = {**preview, "started_at": _now(), "source_records_read_only": True, "workflow_started": False, "state": "CLAIMED"}
    attempt_path = target / "safe-replay-attempt.json"
    atomic_write_json(attempt_path, attempt)
    request = _request_from_source(
        preview["controlled_configuration"], input_path=input_path.expanduser().resolve(), target=target,
        reference_root=reference_root.expanduser().resolve(), runtime_root=runtime_root.expanduser().resolve(),
        external_runtime_root=external_runtime_root.expanduser().resolve() if external_runtime_root else None,
        timeout_seconds=timeout_seconds,
        installation_dependency_plan=(preview.get("local_dependency_revalidation") or {}).get("current_plan"),
    )
    try:
        prepared = preflight(request)
        result = prepared[0] if isinstance(prepared, tuple) else prepared
        attempt["current_asset_verification"] = _verify_current_assets(_read_object(source_manifest_path, "SOURCE_MANIFEST"), result)
        attempt["state"] = "PREFLIGHT_PASSED"
        atomic_write_json(attempt_path, attempt)
    except BaseException as exc:
        attempt.update(state="PREFLIGHT_FAILED", error=f"{type(exc).__name__}: {exc}")
        atomic_write_json(attempt_path, attempt)
        raise
    try:
        attempt.update(state="RUNNER_INVOKED", runner_invoked_at=_now())
        atomic_write_json(attempt_path, attempt)
        manifest = runner(request)
        receipt_path = target / ".demethflow" / "task_receipt.json"
        receipt = _read_object(receipt_path, "NEW_RECEIPT") if receipt_path.is_file() else None
        attempt.update(
            state="RUNNER_RETURNED", workflow_status=manifest.get("status"), workflow_execution_status=manifest.get("execution_status"),
            workflow_started=bool(receipt and receipt.get("process_start_state") == "CONFIRMED_STARTED"),
            new_manifest=str(target / "run_manifest.json"), receipt=str(receipt_path) if receipt else None,
        )
        if report_output_dir is not None:
            try:
                reporter(target, report_output_dir.expanduser().resolve())
                attempt["report_status"] = "WRITTEN"
            except BaseException as exc:
                attempt["report_status"] = "FAILED"
                attempt["report_error"] = f"{type(exc).__name__}: {exc}"
                atomic_write_json(attempt_path, attempt)
                raise
        else:
            attempt["report_status"] = "NOT_REQUESTED"
        atomic_write_json(attempt_path, attempt)
        return manifest
    except BaseException as exc:
        if attempt.get("state") != "RUNNER_RETURNED":
            attempt.update(state="RUNNER_INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "RUNNER_RAISED", error=f"{type(exc).__name__}: {exc}")
            atomic_write_json(attempt_path, attempt)
        raise
