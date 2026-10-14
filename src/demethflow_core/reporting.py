"""Offline result reporting and safe replay preparation.

This module only reads a completed (or partial) run directory and writes
derived report material elsewhere.  It never executes a command contained in a
manifest, never mutates the source run, and never turns engineering evidence
into a scientific status decision.
"""

from __future__ import annotations

import csv
import html
import json
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import version
from .benchmark import benchmark
from .errors import DeMethFlowError
from .util import atomic_write_json, atomic_write_text, sha256_file


REPORT_SCHEMA = "methunmix-run-report-v1"
REPLAY_SCHEMA = "methunmix-replay-plan-v1"
_SENSITIVE_HTML_KEY = re.compile(r"(?:token|secret|password|passphrase|credential|authorization|api[_-]?key)", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT = re.compile(r"(?i)\b([a-z][a-z0-9_-]*(?:token|secret|password|passphrase|credential|api[_-]?key))=([^\s,;]+)")
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\s<>\"']+)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report_generator_identity() -> dict[str, object]:
    """Use a commit only when the active source checkout can prove one."""
    root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=2,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=False, capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
        status = None
    commit = completed.stdout.strip() if completed is not None and completed.returncode == 0 else None
    dirty = status is not None and status.returncode == 0 and bool(status.stdout.strip())
    return {
        "core_version": version(),
        "source_commit": commit,
        "source_commit_status": (
            "VERIFIED_SOURCE_CHECKOUT_DIRTY" if commit and dirty else
            ("VERIFIED_SOURCE_CHECKOUT" if commit else "UNKNOWN_NOT_EMBEDDED_IN_INSTALLED_ARTIFACT")
        ),
        "scope": "derived report generator; does not alter the original run record",
    }


def _read_object(path: Path, *, required: bool) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            raise DeMethFlowError(f"REPORT_RECORD_MISSING: required record is absent: {path}")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"REPORT_RECORD_INVALID: cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DeMethFlowError(f"REPORT_RECORD_INVALID: JSON root must be an object: {path}")
    return value


def _evidence(path: Path) -> dict[str, object]:
    result: dict[str, object] = {"path": str(path)}
    if path.is_file():
        result.update(type="file", bytes=path.stat().st_size, sha256=sha256_file(path))
    else:
        result["type"] = "missing"
    return result


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _output_availability(outputs: list[object]) -> list[dict[str, object]]:
    """Record current file availability without reclassifying conformance."""
    result: list[dict[str, object]] = []
    for raw in outputs:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            result.append({"status": "DECLARED_OUTPUT_RECORD_INVALID", "record": raw})
            continue
        path = Path(raw["path"])
        result.append({
            "path": str(path),
            "status": "PRESENT_AT_REPORT_TIME" if path.is_file() else "MISSING_AT_REPORT_TIME",
            "declared_sha256": raw.get("sha256"),
        })
    return result


def _normalized_variants(tool: str, raw: dict[str, Any]) -> dict[str, dict[str, object]]:
    source = raw.get("variants") if isinstance(raw.get("variants"), dict) else {}
    variants: dict[str, dict[str, object]] = {}
    for name, item in source.items():
        if isinstance(item, dict):
            outputs = _as_list(item.get("outputs"))
            variants[str(name)] = {
                **item,
                "outputs": outputs,
                "declared_output_availability": _output_availability(outputs),
            }
        else:
            variants[str(name)] = {
                "outputs": [],
                "output_contract_status": "UNKNOWN_RECORD_INCOMPLETE",
                "reason": "variant entry is not a JSON object",
                "declared_output_availability": [],
            }
    if tool == "EMeth":
        for required in ("Normal", "Laplace"):
            if required not in variants:
                variants[required] = {
                    "outputs": [],
                    "output_contract_status": "MISSING",
                    "reason": f"required EMeth {required} variant is absent from result_summary.json",
                    "declared_required": True,
                    "declared_output_availability": [],
                }
            else:
                variants[required]["declared_required"] = True
    return variants


def _read_tool_status(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
    except (OSError, UnicodeError, csv.Error):
        return {}
    records: dict[str, dict[str, str]] = {}
    for row in rows:
        tool = str(row.get("tool") or "").strip()
        if tool:
            records[tool] = {"status": str(row.get("status") or ""), "reason": str(row.get("reason") or "")}
    return records


def _tool_records(
    summary: dict[str, Any] | None, manifest: dict[str, Any], tool_status: dict[str, dict[str, str]]
) -> dict[str, dict[str, object]]:
    """Normalize existing summary semantics without inventing a result."""
    source = summary.get("tools") if isinstance(summary, dict) else None
    records: dict[str, dict[str, object]] = {}
    if isinstance(source, dict):
        for tool, raw in sorted(source.items()):
            if not isinstance(raw, dict):
                records[str(tool)] = {
                    "requested_or_selected": "UNKNOWN_RECORD_INCOMPLETE",
                    "execution_status": "UNKNOWN_RECORD_INCOMPLETE",
                    "output_contract_status": "UNKNOWN_RECORD_INCOMPLETE",
                    "reason": "tool entry is not a JSON object",
                }
                continue
            records[str(tool)] = {
                "requested_or_selected": (
                    "SELECTED" if raw.get("selected") is True else
                    ("NOT_SELECTED" if raw.get("selected") is False else "UNKNOWN_RECORD_INCOMPLETE")
                ),
                "selection_status": raw.get("selection_status"),
                "execution_status": raw.get("execution_status", "UNKNOWN_RECORD_INCOMPLETE"),
                "actual_start_status": raw.get("actual_start_status", "NOT_RECORDED"),
                "exit_code": raw.get("exit_code"),
                "output_contract": raw.get("output_contract"),
                "output_contract_status": raw.get("output_contract_status", "NOT_ASSESSED"),
                "outputs": _as_list(raw.get("outputs")),
                "declared_output_availability": _output_availability(_as_list(raw.get("outputs"))),
                "variants": _normalized_variants(str(tool), raw),
                "declared_scientific_status": raw.get("declared_status"),
                "scientific_disclosure": raw.get("release_disclosure"),
                "adapter": raw.get("adapter"),
                "reason": raw.get("reason") or raw.get("declared_reason"),
            }
            if not records[str(tool)]["declared_scientific_status"] and str(tool) in tool_status:
                records[str(tool)]["declared_scientific_status"] = tool_status[str(tool)]["status"]
        return records
    # A legacy/partial run still receives an honest report.  The selected-tool
    # list establishes only intent; it is not evidence of launch or success.
    for tool in sorted(str(item) for item in _as_list(manifest.get("selected_tools"))):
        records[tool] = {
            "requested_or_selected": "SELECTED",
            "execution_status": "UNKNOWN_RECORD_INCOMPLETE",
            "actual_start_status": "NOT_RECORDED",
            "output_contract_status": "NOT_ASSESSED",
            "outputs": [],
            "variants": {},
            "declared_scientific_status": None,
            "scientific_disclosure": None,
            "reason": "result_summary.json is absent; selected_tools records intent only",
        }
        if tool in tool_status:
            records[tool]["declared_scientific_status"] = tool_status[tool]["status"]
            records[tool]["declared_status_reason"] = tool_status[tool]["reason"]
    return records


def _status_counts(tools: dict[str, dict[str, object]]) -> dict[str, object]:
    execution: dict[str, int] = {}
    contracts: dict[str, int] = {}
    variants = 0
    for item in tools.values():
        execution[str(item.get("execution_status"))] = execution.get(str(item.get("execution_status")), 0) + 1
        contracts[str(item.get("output_contract_status"))] = contracts.get(str(item.get("output_contract_status")), 0) + 1
        variants += len(item.get("variants", {})) if isinstance(item.get("variants"), dict) else 0
    return {
        "unit": "tool executions; output variants counted separately",
        "tool_execution_count": len(tools),
        "output_variant_count": variants,
        "execution_status_counts": execution,
        "output_contract_status_counts": contracts,
    }


def _select_output_for_truth(
    tools: dict[str, dict[str, object]], *, tool: str | None, variant: str | None
) -> tuple[Path | None, dict[str, object]]:
    if tool is None:
        return None, {"status": "NOT_COMPUTED_NO_TOOL_SELECTED", "reason": "--tool is required with --truth"}
    item = tools.get(tool)
    if item is None:
        return None, {"status": "NOT_COMPUTED_UNKNOWN_TOOL", "reason": f"tool is absent from report: {tool}"}
    if item.get("output_contract") != "cell_proportions_v1":
        return None, {
            "status": "NOT_COMPUTED_OUTPUT_CONTRACT_UNSUPPORTED",
            "reason": "truth metrics require named cell-proportion output; anonymous components are not mapped implicitly",
        }
    variants = item.get("variants")
    if isinstance(variants, dict) and variants:
        if variant is None:
            return None, {"status": "NOT_COMPUTED_VARIANT_REQUIRED", "reason": f"{tool} has multiple output variants; specify --variant"}
        item = variants.get(variant)
        if not isinstance(item, dict):
            return None, {"status": "NOT_COMPUTED_UNKNOWN_VARIANT", "reason": f"unknown {tool} variant: {variant}"}
    elif variant is not None:
        return None, {"status": "NOT_COMPUTED_VARIANT_NOT_APPLICABLE", "reason": f"{tool} has no declared output variants"}
    outputs = _as_list(item.get("outputs"))
    if len(outputs) != 1 or not isinstance(outputs[0], dict) or not isinstance(outputs[0].get("path"), str):
        return None, {"status": "NOT_COMPUTED_OUTPUT_UNAVAILABLE", "reason": "exactly one conformant declared output is required"}
    path = Path(str(outputs[0]["path"])).expanduser()
    if not path.is_file():
        return None, {"status": "NOT_COMPUTED_OUTPUT_MISSING", "reason": f"declared output is unavailable: {path}"}
    return path, {"status": "READY_FOR_DESCRIPTIVE_METRICS", "tool": tool, "variant": variant}


def _truth_metrics(
    tools: dict[str, dict[str, object]], *, truth: Path | None, tool: str | None, variant: str | None
) -> dict[str, object]:
    if truth is None:
        return {"status": "NOT_COMPUTED_NO_RUNTIME_TRUTH_INPUT", "metrics": None}
    estimate, context = _select_output_for_truth(tools, tool=tool, variant=variant)
    if estimate is None:
        return {**context, "metrics": None, "truth": _evidence(truth)}
    try:
        metrics = benchmark(estimate, truth)
    except DeMethFlowError as exc:
        return {
            "status": "NOT_COMPUTED_INPUT_OR_AXIS_CONTRACT_FAILED",
            "reason": str(exc),
            "estimate": _evidence(estimate),
            "truth": _evidence(truth),
            "metrics": None,
        }
    return {
        "status": "METRICS_COMPUTED_REPORT_ONLY",
        "estimate": _evidence(estimate),
        "truth": _evidence(truth),
        "metric_definition_source": "demethflow_core.benchmark.benchmark",
        "metrics": metrics.get("metrics"),
        "axis": metrics.get("axis"),
        "policy_conclusion": "REPORT_ONLY_NO_NEW_ACCEPTANCE_POLICY",
    }


def _manifest_summary_consistency(manifest: dict[str, Any], summary: dict[str, Any] | None) -> dict[str, object]:
    if summary is None:
        return {"status": "NOT_CHECKED_RESULT_SUMMARY_ABSENT"}
    manifest_status = manifest.get("execution_status", manifest.get("status"))
    summary_status = summary.get("execution_status")
    return {
        "status": "MATCH" if manifest_status == summary_status else "MISMATCH",
        "manifest_execution_status": manifest_status,
        "summary_execution_status": summary_status,
        "note": "workflow and report statuses remain separate records; a mismatch is disclosed rather than overwritten",
    }


def _fresh_retry_lineage(run_dir: Path) -> dict[str, object]:
    """Disclose a fresh retry's historical pointer without merging outputs."""
    path = run_dir / "fresh-retry-attempt.json"
    record = _read_object(path, required=False)
    if record is None:
        return {"status": "NOT_A_FRESH_RETRY"}
    if record.get("schema") != "methunmix-fresh-failed-tool-retry-v1":
        return {
            "status": "RETRY_LINEAGE_RECORD_INVALID",
            "record": _evidence(path),
            "reason": "fresh-retry-attempt.json has an unexpected schema",
        }
    return {
        "status": "FRESH_RETRY_LINEAGE_RECORDED",
        "record": _evidence(path),
        "historical_run_reference": record.get("prior_result_reference"),
        "historical_source_run_directory": record.get("source_run_dir"),
        "new_results_scope": "this report only evaluates outputs recorded in original_run_directory",
        "not_supported": ["step-level recovery", "Nextflow resume", "reuse of partial outputs"],
    }


def _html_display(value: object, *, key: str | None = None) -> object:
    """Return a safe display-only projection; JSON provenance is unchanged."""
    if key and _SENSITIVE_HTML_KEY.search(key):
        return "REDACTED"
    if isinstance(value, dict):
        return {str(name): _html_display(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_html_display(item) for item in value]
    if isinstance(value, str):
        text = _SENSITIVE_ASSIGNMENT.sub(r"\1=REDACTED", value)
        return _ABSOLUTE_PATH.sub("[absolute-path]", text)
    return value


def _html_report(report: dict[str, object]) -> str:
    title = "MethUnMix offline run report"
    display = _html_display(report)
    assert isinstance(display, dict)
    overview = display.get("overview", {}) if isinstance(display.get("overview"), dict) else {}
    rows = []
    tools = display.get("tools", {}) if isinstance(display.get("tools"), dict) else {}
    for tool, item in tools.items():
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(tool))}</td>"
            f"<td>{html.escape(str(item.get('requested_or_selected')))}</td>"
            f"<td>{html.escape(str(item.get('execution_status')))}</td>"
            f"<td>{html.escape(str(item.get('output_contract_status')))}</td>"
            f"<td>{html.escape(str(item.get('declared_scientific_status')))}</td>"
            f"<td>{html.escape(str(item.get('reason') or ''))}</td>"
            "</tr>"
        )
    report_json = html.escape(json.dumps(display, ensure_ascii=False, indent=2))
    return "\n".join((
        "<!doctype html>",
        "<html lang=\"en\"><head><meta charset=\"utf-8\">",
        f"<title>{html.escape(title)}</title>",
        "<style>body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse;width:100%}th,td{border:1px solid #bbb;padding:.4rem;text-align:left;vertical-align:top}th{background:#eee}code,pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>",
        "</head><body>",
        f"<h1>{html.escape(title)}</h1>",
        f"<p>Run execution status: <strong>{html.escape(str(overview.get('execution_status')))}</strong></p>",
        "<h2>Tool outcomes</h2><table><thead><tr><th>Tool</th><th>Selection</th><th>Execution</th><th>Output contract</th><th>Declared science status</th><th>Reason</th></tr></thead><tbody>",
        *rows,
        "</tbody></table>",
        "<h2>Machine report</h2><pre>", report_json, "</pre>",
        "</body></html>",
    )) + "\n"


def build_report(
    run_dir: Path, output_dir: Path, *, truth: Path | None = None, tool: str | None = None, variant: str | None = None
) -> dict[str, object]:
    """Create derived JSON and offline HTML without modifying ``run_dir``."""
    run_dir = run_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if output_dir == run_dir or run_dir in output_dir.parents:
        raise DeMethFlowError("REPORT_OUTPUT_DIRECTORY_CONFLICT: --output-dir must be outside the source run directory")
    manifest_path = run_dir / "run_manifest.json"
    summary_path = run_dir / "result_summary.json"
    tool_status_path = run_dir / "tool_status.tsv"
    manifest = _read_object(manifest_path, required=True)
    summary = _read_object(summary_path, required=False)
    tools = _tool_records(summary, manifest, _read_tool_status(tool_status_path))
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "generated_at": _now(),
        "report_generator": _report_generator_identity(),
        "source_records": {
            "run_manifest": _evidence(manifest_path),
            "result_summary": _evidence(summary_path),
            "tool_status": _evidence(tool_status_path),
            "original_run_directory": str(run_dir),
        },
        "fresh_retry_lineage": _fresh_retry_lineage(run_dir),
        "overview": {
            "execution_status": manifest.get("execution_status", manifest.get("status", "UNKNOWN_RECORD_INCOMPLETE")),
            "exit_code": manifest.get("exit_code"),
            "failure_classification": manifest.get("failure_classification"),
            "run_software_identity": manifest.get("software_identity") or {"core_version": manifest.get("core_version"), "source_commit": None, "source_commit_status": "UNKNOWN_LEGACY_RECORD"},
            "scientific_status": "OBSERVATIONAL_ONLY_NO_PROMOTION",
            "analysis_contract": manifest.get("analysis_contract"),
            "reference_selector": (
                f"{manifest.get('reference_id')}@{manifest.get('reference_version')}"
                if manifest.get("reference_id") and manifest.get("reference_version") else None
            ),
            "reference_digest": manifest.get("reference_digest"),
            "input_identity": manifest.get("input_identity"),
            "runtime_identity": {
                "runtime_images": manifest.get("runtime_images"),
                "external_runtime": manifest.get("external_runtime"),
                "data_modules": manifest.get("data_modules"),
            },
            "execution_plan_semantic_identity": manifest.get("execution_plan_semantic_identity"),
            "actual_execution_plan_identity": manifest.get("execution_plan_identity"),
            "resolved_analysis_request": manifest.get("resolved_analysis_request") or (manifest.get("execution_plan") or {}).get("effective_request"),
            "random_seed": manifest.get("random_seed"),
            "resolved_execution_parameters": manifest.get("resolved_execution_parameters"),
            "tool_execution_metadata": manifest.get("tool_execution_metadata"),
            "adapter_execution": manifest.get("adapter_execution"),
            "preprocess_or_projection": {
                "platform_projection": manifest.get("platform_projection"),
                "wgbs_bam_preprocess": manifest.get("wgbs_bam_preprocess"),
            },
            "installation_dependency_plan": manifest.get("installation_dependency_plan"),
            "execution_lifecycle": {
                "resource_preflight": manifest.get("resource_preflight"),
                "step_cache": manifest.get("step_cache"),
                "task_supervisor": manifest.get("task_supervisor"),
                "run_lease": manifest.get("run_lease"),
            },
            "scientific_disclosure": manifest.get("scientific_validation_disclosure"),
        },
        "manifest_summary_consistency": _manifest_summary_consistency(manifest, summary),
        "tools": tools,
        "counts": _status_counts(tools),
        "truth_metrics": _truth_metrics(tools, truth=truth, tool=tool, variant=variant),
        "limitations": [
            "Execution success, output conformance, descriptive metrics, and scientific-policy conclusions are distinct.",
            "This report never changes READY/RU, reference evidence, or a frozen baseline.",
            "A missing or partial source record is reported as incomplete rather than inferred as success.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "report.json", report)
    atomic_write_text(output_dir / "report.html", _html_report(report))
    return report


def _replay_input_check(path: Path | None, expected: object, *, verify: bool) -> dict[str, object]:
    if path is None:
        return {"status": "NOT_CHECKED_NO_CURRENT_INPUT_PATH", "expected_identity": expected}
    candidate = path.expanduser().resolve()
    if not candidate.exists():
        return {"status": "MISSING", "path": str(candidate), "expected_identity": expected}
    if not verify:
        return {
            "status": "PRESENT_NOT_REVERIFIED",
            "path": str(candidate),
            "expected_identity": expected,
            "reason": "full input hashing is opt-in for replay preparation",
        }
    if not candidate.is_file():
        return {"status": "NOT_VERIFIED_UNSUPPORTED_INPUT_TYPE", "path": str(candidate), "expected_identity": expected}
    actual = sha256_file(candidate)
    expected_sha = expected.get("sha256") if isinstance(expected, dict) else None
    return {
        "status": "MATCH" if expected_sha and actual == expected_sha else "MISMATCH_OR_IDENTITY_INSUFFICIENT",
        "path": str(candidate), "expected_identity": expected, "actual_sha256": actual,
    }


def prepare_replay(
    manifest_path: Path, output_dir: Path, *, input_path: Path | None = None, verify_input_identity: bool = False
) -> dict[str, object]:
    """Recover a controlled execution intent, never an executable manifest shell string."""
    manifest_path = manifest_path.expanduser().resolve()
    manifest = _read_object(manifest_path, required=True)
    output_dir = output_dir.expanduser().resolve()
    source_output = (manifest.get("execution_plan") or {}).get("output_directory")
    if source_output and Path(str(source_output)).expanduser().resolve() == output_dir:
        raise DeMethFlowError("REPLAY_OUTPUT_DIRECTORY_CONFLICT: --outdir must be a new directory, not the source run directory")
    plan = manifest.get("execution_plan") if isinstance(manifest.get("execution_plan"), dict) else {}
    effective = plan.get("effective_request") if isinstance(plan.get("effective_request"), dict) else {}
    values = effective.get("effective_values") if isinstance(effective.get("effective_values"), dict) else {}
    requested_input = input_path or (Path(str(manifest["input"])) if isinstance(manifest.get("input"), str) else None)
    input_check = _replay_input_check(requested_input, manifest.get("input_identity"), verify=verify_input_identity)
    # An effective request is authoritative even when it deliberately records
    # JSON null/false/zero.  Manifest fields are presentation or legacy
    # fallbacks only when a resolved field is genuinely absent.
    def resolved(key: str, legacy_key: str) -> object:
        return values[key] if key in values else manifest.get(legacy_key)

    reference = values["reference"] if "reference" in values else (
            f"{manifest.get('reference_id')}@{manifest.get('reference_version')}"
            if manifest.get("reference_id") and manifest.get("reference_version") else None
        )
    parameters = values.get("parameters")
    known = {
        "scenario": resolved("scenario", "scenario_id"),
        "platform": resolved("input_platform", "input_platform"),
        "genome_build": resolved("genome_build", "genome_build"),
        "analysis_contract": resolved("analysis_contract", "analysis_contract"),
        "reference": reference,
        "tools": values["tools_requested"] if "tools_requested" in values else ",".join(str(item) for item in _as_list(manifest.get("selected_tools"))),
        "accelerator": resolved("accelerator", "accelerator"),
        "random_seed": parameters["random_seed"] if isinstance(parameters, dict) and "random_seed" in parameters else manifest.get("random_seed"),
    }
    command = ["methunmix", "run"]
    argument_map = (("scenario", "--scenario"), ("platform", "--platform"), ("genome_build", "--genome-build"),
                    ("analysis_contract", "--analysis-contract"), ("reference", "--reference"),
                    ("tools", "--tools"), ("accelerator", "--accelerator"), ("random_seed", "--random-seed"))
    for key, flag in argument_map:
        value = known.get(key)
        if key == "genome_build" and value not in {"hg19", "hg38"}:
            continue
        if value is not None and value != "":
            command.extend((flag, str(value)))
    if requested_input is not None:
        command.extend(("--input", str(requested_input.expanduser().resolve())))
    command.extend(("--outdir", str(output_dir)))
    prerequisites = {
        "input": input_check,
        "reference": {"selector": known["reference"], "content_digest": manifest.get("reference_digest"), "status": "NOT_REVERIFIED_BY_REPLAY_PREPARATION"},
        "runtime": {"expected": manifest.get("runtime_images"), "external": manifest.get("external_runtime"), "status": "NOT_REVERIFIED_BY_REPLAY_PREPARATION"},
        "software": {"core_version": manifest.get("core_version"), "status": "IDENTITY_INSUFFICIENT_UNLESS_ARTIFACT_BINDING_IS_RECORDED"},
    }
    blockers = []
    if input_check["status"] in {"MISSING", "MISMATCH_OR_IDENTITY_INSUFFICIENT"}:
        blockers.append({"code": "INPUT_IDENTITY_NOT_READY", "detail": input_check["status"]})
    if not known["reference"] or not known["analysis_contract"] or not known["platform"]:
        blockers.append({"code": "MANIFEST_CONFIGURATION_INCOMPLETE", "detail": "reference, contract, or input platform is absent"})
    payload: dict[str, object] = {
        "schema": REPLAY_SCHEMA,
        "status": "BLOCKED" if blockers else "PREPARED_NOT_EXECUTED",
        "source_manifest": _evidence(manifest_path),
        "new_output_directory": str(output_dir),
        "resume": False,
        "controlled_configuration": known,
        "input_check": input_check,
        "prerequisites": prerequisites,
        "blockers": blockers,
        "suggested_command_argv": command,
        "suggested_command_display": " ".join(shlex.quote(part) for part in command),
        "safety": "manifest command strings are not interpreted or executed; this is a new-run plan only",
    }
    return payload
