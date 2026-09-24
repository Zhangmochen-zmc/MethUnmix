from __future__ import annotations

import csv
import json
import math
import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import PROJECT_ROOT
from .catalog import ReferenceCatalog
from .celfeer import (
    discover_pat_samples,
    inspect_marker_reference,
    labels_from_manifest,
    markers_missing_from_read_bins,
    read_cell_types,
)
from .errors import DeMethFlowError
from .manifest import (
    VALIDATION_CANDIDATE_TOOLS,
    ReferenceBundle,
    is_native_wgbs_contract,
    is_wgbs_derived_array_contract,
)
from .epic_projection import CONTRACT as EPIC_FROM_450K_CONTRACT, inspect_projection_inputs
from .external_runtime import EXTERNAL_TOOLS, discover_external_runtimes, external_runtime_root_from_env
from .wgbs_projection import inspect_wgbs_projection_inputs, project_wgbs_inputs, target_platform_for_contract
from .wgbs_bam import discover_bam_samples, is_bam_input
from .modules import ModuleStore
from .util import default_data_home, legacy_data_home, sha256_file


RUNTIME_FILES = {
    "array-common": "array_env.sif",
    "ARIC": "aric_env.sif",
    "MethylCIBERSORT": "methylcibersort_env.sif",
    "EDec": "edec_env.sif",
    "EMeth": "emeth_env.sif",
    "EpiDISH": "epidish_env.sif",
    "EpiSCORE": "episcore_env.sif",
    "Houseman": "houseman_env.sif",
    "MeDeCom": "medecom_env.sif",
    "MEnet": "menet_env.sif",
    "MethAtlas": "methatlas_env.sif",
    "RefFreeEWAS": "reffreeewas_env.sif",
    "PRmeth": "prmeth_env.sif",
    "Tsisal": "tsisal_env.sif",
    "wgbs-common": "wgbs_env.sif",
    "MethylBERT-cpu": "methylbert_env_cpu.sif",
    "MethylBERT-gpu": "methylbert_env_gpu.sif",
}

NATIVE_WGBS_BED_TOOLS = {"MEnet", "MetDecode", "CelFiE"}
NATIVE_WGBS_PAT_TOOLS = {"CelFEER", "UXM", "MethylBERT"}


def release_warning_for_tools(release: dict, tools: list[str] | tuple[str, ...]) -> str | None:
    """Return disclosures for the selected tools, preserving legacy fallback.

    A native-WGBS bundle can contain several tools with different scientific
    limitations.  Prefer the per-tool disclosure when present so selecting
    MethylBERT cannot accidentally display an MEnet/MetDecode-only warning.
    """
    warnings = release.get("tool_warnings")
    if isinstance(warnings, dict):
        selected = [str(warnings[tool]) for tool in tools if tool in warnings]
        if selected:
            return " ".join(selected)
    value = release.get("user_warning")
    return str(value) if value else None


def _flatten_validation_metrics(value: object, prefix: str = "") -> dict[str, object]:
    """Collect scalar scientific metrics from a validation JSON object.

    Validation reports have evolved from a flat ``metrics`` object to nested
    CPU/GPU/profile objects.  This intentionally keeps only well-known metric
    names and never infers a scientific pass/fail state from them.
    """
    names = {
        "overall_pearson", "overall_mae", "maximum_cell_type_mae",
        "max_cell_mae", "valid_output_rate", "repeat_max_absolute_difference",
        "cpu_gpu_max_absolute_difference",
    }
    output: dict[str, object] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in names and isinstance(item, (int, float)) and not isinstance(item, bool):
                output[normalized] = item
            if isinstance(item, (dict, list)):
                output.update(_flatten_validation_metrics(item, f"{prefix}{normalized}."))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                output.update(_flatten_validation_metrics(item, prefix))
    return output


SELECTOR_BOUND_VALIDATION_TOOLS = frozenset({"CelFiE", "CelFEER"})


def _selector_bound_validation_binding(
    report: object,
    bundle: ReferenceBundle,
    tool: str,
    report_path: str | None = None,
    report_sha256: str | None = None,
) -> tuple[str, dict[str, object]]:
    """Require an exact, minimally complete selector-bound validation record.

    Validation reports are release evidence, not execution inputs.  A report
    from an older reference must never be exposed as the current bundle's
    scientific disclosure.  For the two tools whose native-WGBS disclosure
    must be selector-bound, the report must also identify a truth/input
    collection (at least one digest or explicit identity) and its own report
    identity.  Missing evidence fails closed; it does not alter execution
    capability or the frozen scientific baseline.
    """
    if tool not in SELECTOR_BOUND_VALIDATION_TOOLS:
        return "NOT_APPLICABLE", {}
    if not isinstance(report, dict):
        return "NO_SELECTOR_BOUND_VALIDATION_EVIDENCE", {"reason": "validation record is not a JSON object"}

    declared_selector = report.get("selector")
    if declared_selector is None:
        # Historical native-WGBS reports used ``reference``.  It is accepted
        # only as an identity alias; the value still has to match exactly.
        declared_selector = report.get("reference")
    declared_digest = report.get("reference_digest")
    if declared_digest is None:
        declared_digest = report.get("validation_reference_digest")
    expected = {
        "tool": tool,
        "scenario": bundle.scenario_id,
        "platform": bundle.source_platform,
        "genome_build": bundle.payload.get("genome_build"),
        "selector": bundle.selector,
        "reference_digest": bundle.content_digest(),
    }
    observed = {
        "tool": report.get("tool"),
        "scenario": report.get("scenario"),
        "platform": report.get("platform"),
        "genome_build": report.get("genome_build"),
        "selector": declared_selector,
        "reference_digest": declared_digest,
    }
    mismatches = [
        key for key, value in expected.items()
        if value is None or observed.get(key) != value
    ]
    truth = report.get("truth") if isinstance(report.get("truth"), dict) else {}
    input_obj = report.get("input") if isinstance(report.get("input"), dict) else {}
    truth_identity = truth.get("sha256") or truth.get("digest") or report.get("truth_digest")
    input_identity = (
        report.get("input_digest") or report.get("input_sha256")
        or input_obj.get("sha256") or input_obj.get("digest")
    )
    if not truth_identity and not input_identity:
        mismatches.append("input_or_truth_identity")
    if not report_path or not report_sha256:
        mismatches.append("validation_report_identity")
    if mismatches:
        return "NO_SELECTOR_BOUND_VALIDATION_EVIDENCE", {
            "reason": "validation evidence identity does not exactly match the selected bundle",
            "mismatched_fields": mismatches,
            "expected": expected,
            "observed": observed,
            "truth_identity_present": bool(truth_identity),
            "input_identity_present": bool(input_identity),
            "validation_report_identity_present": bool(report_path and report_sha256),
        }
    return "BOUND_CURRENT", {
        "expected": expected,
        "observed": observed,
        "truth_identity": truth_identity,
        "input_identity": input_identity,
        "validation_report": report_path,
        "validation_report_sha256": report_sha256,
        "random_seed": report.get("random_seed", report.get("seed")),
        "metric_computation_version": report.get("metric_computation_version"),
    }


def _celfie_validation_binding(
    report: object,
    bundle: ReferenceBundle,
    tool: str,
) -> tuple[str, dict[str, object]]:
    """Backward-compatible wrapper retained for downstream imports."""
    return _selector_bound_validation_binding(report, bundle, tool)


def release_disclosures_for_tools(
    bundle: ReferenceBundle,
    tools: list[str] | tuple[str, ...],
    accelerator: str = "cpu",
) -> dict[str, dict[str, object]]:
    """Build tool-level release disclosures for manifests, doctor and reports.

    The disclosure is deliberately observational.  It never upgrades a tool,
    lowers a gate, or treats an RU metric as scientifically passing.
    """
    release = bundle.payload.get("release_validation", {})
    raw_capabilities = bundle.payload.get("tool_capabilities", {})
    warnings = release.get("tool_warnings", {}) if isinstance(release, dict) else {}
    evidence = release.get("tool_validation_evidence", {}) if isinstance(release, dict) else {}
    output: dict[str, dict[str, object]] = {}
    for tool in tools:
        cap = bundle.tools.get(tool)
        if cap is None:
            continue
        status, reason = cap.effective_status(accelerator)
        raw = raw_capabilities.get(tool, {}) if isinstance(raw_capabilities, dict) else {}
        raw = raw if isinstance(raw, dict) else {}
        profile = raw.get("execution_profiles", {}).get(accelerator, {}) if isinstance(raw.get("execution_profiles"), dict) else {}
        failed = raw.get("failed_validation_gates", [])
        if not isinstance(failed, list):
            failed = []
        tool_evidence = evidence.get(tool, {}) if isinstance(evidence, dict) else {}
        if not isinstance(tool_evidence, dict):
            tool_evidence = {}
        # CelFEER reports metrics for disclosure, but the historical generic
        # MAE/Pearson policy is not a CelFEER acceptance contract.  Do not
        # project that policy into manifests or derive failed gates from it.
        declared_validation_policy = None if tool == "CelFEER" else cap.validation_policy
        metrics: dict[str, object] = {}
        report_path: str | None = None
        validation_binding_status = "NOT_CHECKED"
        validation_binding: dict[str, object] = {}
        for key in ("scientific_validation", "profile_validation", "engineering_validation"):
            relative = cap.artifacts.get(key)
            if not relative:
                continue
            path = bundle.root / relative
            if path.is_file() and key != "engineering_validation":
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                    validation_binding_status, validation_binding = _selector_bound_validation_binding(
                        report,
                        bundle,
                        tool,
                        report_path=relative,
                        report_sha256=sha256_file(path),
                    )
                    if validation_binding_status != "NO_SELECTOR_BOUND_VALIDATION_EVIDENCE":
                        metrics.update(_flatten_validation_metrics(report))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    if tool == "CelFiE":
                        validation_binding_status = "NO_SELECTOR_BOUND_VALIDATION_EVIDENCE"
                        validation_binding = {"reason": "validation record could not be parsed"}
            if key == "scientific_validation":
                report_path = relative
        # Some older MEnet manifests recorded the scientific result and
        # metrics but omitted ``failed_validation_gates``. Derive only the
        # transparent gate names from the declared thresholds; this does not
        # promote the status and keeps run evidence complete.
        if not failed and metrics:
            policy = declared_validation_policy or {}
            comparisons = (
                ("overall_mae", "overall_mae_max", lambda value, threshold: value > threshold),
                ("overall_pearson", "overall_pearson_min", lambda value, threshold: value < threshold),
                ("maximum_cell_type_mae", "maximum_cell_type_mae_max", lambda value, threshold: value > threshold),
                ("valid_output_rate", "valid_output_rate_min", lambda value, threshold: value < threshold),
            )
            for metric_name, threshold_name, failed_test in comparisons:
                value = metrics.get(metric_name)
                threshold = policy.get(threshold_name)
                if isinstance(value, (int, float)) and isinstance(threshold, (int, float)):
                    if failed_test(float(value), float(threshold)):
                        failed.append(metric_name)
        warning = warnings.get(tool) if isinstance(warnings, dict) else None
        if not isinstance(warning, str):
            warning = (
                f"{tool} is RELEASED_UNVALIDATED: it is runnable, but its configured "
                "scientific accuracy gates are not a READY claim. Interpret results cautiously."
                if status == "RELEASED_UNVALIDATED"
                else None
            )
        if validation_binding_status in {
            "NO_SELECTOR_BOUND_VALIDATION_EVIDENCE",
            "NO_VALIDATION_EVIDENCE",
        }:
            warning = (
                f"{tool} disclosure blocked: {validation_binding_status}; "
                "execution output remains independent of scientific disclosure metrics."
            )
        binding_required = tool in SELECTOR_BOUND_VALIDATION_TOOLS
        if binding_required and validation_binding_status == "NOT_CHECKED":
            validation_binding_status = "NO_VALIDATION_EVIDENCE"
            validation_binding = {"reason": "no selector-bound validation report is registered"}
            warning = (
                f"{tool} disclosure blocked: NO_VALIDATION_EVIDENCE; execution output remains "
                "independent of scientific disclosure metrics."
            )
        if binding_required:
            scientific_evidence_status = (
                "BOUND_CURRENT" if validation_binding_status == "BOUND_CURRENT" else "INCOMPLETE"
            )
        elif status == "RELEASED_UNVALIDATED":
            scientific_evidence_status = "RELEASED_UNVALIDATED"
        elif metrics:
            scientific_evidence_status = "REPORTED"
        else:
            scientific_evidence_status = "NOT_CONFIGURED"
        scientific_status = (
            status
            if not binding_required or validation_binding_status == "BOUND_CURRENT"
            else "NOT_PROMOTED"
        )
        # Operational readiness and scientific disclosure are separate gates.  A
        # profile with no configured/selector-bound evidence must never inherit
        # a scientific READY label merely because the executable path ran.
        if scientific_evidence_status in {"NOT_CONFIGURED", "INCOMPLETE"}:
            scientific_status = "NOT_PROMOTED"
        report_disclosure = {}
        if report_path and validation_binding_status == "BOUND_CURRENT":
            try:
                report_disclosure = json.loads(
                    (bundle.root / report_path).read_text(encoding="utf-8")
                ).get("validation_disclosure", {})
            except (OSError, UnicodeError, json.JSONDecodeError):
                report_disclosure = {}
        if isinstance(warning, str) and failed:
            missing_gate_names = [name for name in failed if name not in warning]
            if missing_gate_names:
                warning = warning.rstrip(".") + ". Failed gates: " + ", ".join(missing_gate_names) + "."
        operational = "PASS"
        if status in {"QUARANTINED", "NOT_AVAILABLE", "INVALID", "NOT_ADAPTED"}:
            operational = "FAIL"
        elif any(str(item).lower() in {"engineering", "runtime", "e2e", "output_schema", "coordinate"} for item in failed):
            operational = "FAIL"
        output[tool] = {
            "tool": tool,
            "status": status,
            "execution_status": operational,
            "execution_policy": cap.execution_policy,
            "validation_policy": declared_validation_policy,
            "profile_status": status,
            "operational_status": operational,
            "scientific_status": "NOT_PROMOTED" if status == "RELEASED_UNVALIDATED" else scientific_status,
            "scientific_evidence_status": scientific_evidence_status,
            "failed_gates": list(failed),
            "observed_metrics": metrics,
            "disclosure_status": (
                validation_binding_status
                if tool in SELECTOR_BOUND_VALIDATION_TOOLS
                else ("VALIDATION_METRICS_AVAILABLE" if metrics else "NO_VALIDATION_METRICS")
            ),
            "validation_binding": validation_binding,
            "validation_scope": cap.validation_scope or release.get("validation_type"),
            "validation_report": report_path or tool_evidence.get("report"),
            "scientific_claim_scope": report_disclosure.get("scientific_claim_scope"),
            "scientific_claims_permitted": report_disclosure.get("scientific_claims_permitted"),
            "reference_selector": bundle.selector,
            "accelerator": accelerator,
            "warning": warning,
            "allow_tools_all": bool(cap.execution_policy == "normal" and status in {"READY", "RELEASED_UNVALIDATED"}),
            "evidence": tool_evidence,
            "reason": reason or cap.reason,
            "profile_metadata": profile if isinstance(profile, dict) else {},
        }
    return output


def _normal_label(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def inspect_celfie_atlas_contract(path: Path, bundle: ReferenceBundle) -> tuple[bool, str, dict[str, object]]:
    """Validate the immutable explicit-interval CelFiE atlas contract.

    This is deliberately stdlib-only because ``doctor`` runs before the WGBS
    runtime container is selected.  The production formatter repeats the same
    checks and then performs a full coordinate-key join with bedtools output.
    """
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fields = list(reader.fieldnames or [])
            aliases = {
                "chrom": {"chrom", "chr", "chromosome"},
                "start": {"start", "pos"},
                "end": {"end", "stop"},
            }
            coordinates: dict[str, str] = {}
            for field in fields:
                normalized = field.strip().lower().replace("-", "_").replace(" ", "_")
                for name, options in aliases.items():
                    if normalized in options:
                        if name in coordinates:
                            return False, f"atlas has multiple {name} columns", {}
                        coordinates[name] = field
            missing = [name for name in ("chrom", "start", "end") if name not in coordinates]
            if missing:
                detail = "atlas lacks " + ", ".join(missing)
                if "end" in missing:
                    detail += "; legacy start+2 inference is forbidden"
                return False, detail, {}
            coordinate_fields = set(coordinates.values())
            meth: dict[str, str] = {}
            depth: dict[str, str] = {}
            unexpected: list[str] = []
            for field in fields:
                if field in coordinate_fields:
                    continue
                normalized = field.strip().lower().replace("-", "_").replace(" ", "_")
                if normalized.endswith("_meth"):
                    meth[normalized[:-5]] = field
                elif normalized.endswith("_depth"):
                    depth[normalized[:-6]] = field
                else:
                    unexpected.append(field)
            if unexpected or not meth or set(meth) != set(depth):
                return False, "atlas has unpaired or unexpected CelFiE reference columns", {}
            expected = {
                _normal_label(name)
                for cell in bundle.cell_types
                for name in (cell["cell_type_id"], cell["display_name"], *cell.get("synonyms", []))
            }
            atlas_cells = set(meth)
            unmatched = sorted(cell for cell in atlas_cells if _normal_label(cell) not in expected)
            if unmatched or len(atlas_cells) != len(bundle.cell_types):
                return False, (
                    "atlas cell-type axis does not match reference canonical cell types; "
                    f"unmatched={unmatched} atlas_count={len(atlas_cells)} expected_count={len(bundle.cell_types)}"
                ), {}
            seen: set[tuple[str, int, int]] = set()
            widths: dict[int, int] = {}
            count = 0
            overlap_events = 0
            previous_by_chrom: dict[str, int] = {}
            for row_number, row in enumerate(reader, start=2):
                if not row or all(value in (None, "") for value in row.values()):
                    continue
                chrom = str(row.get(coordinates["chrom"], "")).strip()
                try:
                    start = int(str(row.get(coordinates["start"], "")))
                    end = int(str(row.get(coordinates["end"], "")))
                except ValueError:
                    return False, f"atlas row {row_number} has non-integer coordinates", {}
                if not chrom or start < 0 or end <= start:
                    return False, f"atlas row {row_number} violates 0-based half-open interval semantics", {}
                key = (chrom, start, end)
                if key in seen:
                    return False, f"atlas row {row_number} duplicates interval {key}", {}
                seen.add(key)
                widths[end - start] = widths.get(end - start, 0) + 1
                if chrom in previous_by_chrom and start < previous_by_chrom[chrom]:
                    overlap_events += 1
                previous_by_chrom[chrom] = max(previous_by_chrom.get(chrom, end), end)
                count += 1
            if not count:
                return False, "atlas has no marker rows", {}
    except (OSError, csv.Error, UnicodeError) as exc:
        return False, f"could not read CelFiE atlas: {exc}", {}
    details = {
        "marker_rows": count,
        "width_distribution": {str(width): value for width, value in sorted(widths.items())},
        "overlapping_marker_events": overlap_events,
        "coordinate_system": "bed_0_based_half_open",
        "interval_semantics": "explicit_atlas_start_end",
        "overlapping_intervals": "allowed_per_marker",
        "exact_duplicate_intervals": "forbidden",
        "legacy_start_plus_2": "forbidden",
    }
    return True, f"{count} explicit interval markers; widths={details['width_distribution']}", details


def inspect_celfie_bed_cpg_contract(paths: list[Path], *, max_records_per_file: int = 100000) -> tuple[bool, str]:
    """Fast bounded preflight for native per-CpG BED inputs.

    The full input is checked again in the Nextflow aggregation process.  The
    bounded doctor check gives users a prompt error before a large workflow is
    started while avoiding a multi-gigabyte scan on every ``doctor`` command.
    """
    if not paths:
        return False, "CelFiE requires at least one BED-like input"
    inspected = 0
    try:
        for path in paths:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip() or line.startswith("#"):
                        continue
                    fields = line.rstrip("\n").split("\t")
                    if len(fields) < 5:
                        return False, f"{path.name}:{line_number} has fewer than five BED/count columns"
                    try:
                        start, end = int(fields[1]), int(fields[2])
                        methylated, depth = float(fields[3]), float(fields[4])
                    except ValueError:
                        return False, f"{path.name}:{line_number} has invalid coordinate/count fields"
                    if start < 0 or end != start + 1:
                        return False, f"{path.name}:{line_number} is not one CpG [start,start+1)"
                    if not all(math.isfinite(value) and value >= 0 for value in (methylated, depth)) or methylated > depth:
                        return False, f"{path.name}:{line_number} violates methylated/depth count constraints"
                    inspected += 1
                    if inspected >= max_records_per_file:
                        break
    except OSError as exc:
        return False, f"could not inspect native WGBS BED: {exc}"
    return True, f"validated {inspected} per-CpG BED records (full input is rechecked during aggregation)"


def _native_wgbs_input_inventory(path: Path) -> dict[str, list[Path]]:
    """Return supported native-WGBS inputs without accepting arbitrary files."""
    resolved = path.expanduser().resolve()
    # A materialised BAM run has an explicit public input boundary.  Its
    # ``work/`` directory necessarily contains staged source BAMs, which must
    # never be reclassified as a direct user BAM input during downstream
    # doctor.  The same structured layout is also safe for callers that
    # deliberately supply ``bed/`` and/or ``pat/`` directories.
    if (
        resolved.is_dir()
        and (resolved / "manifest.json").is_file()
        and ((resolved / "bed").is_dir() or (resolved / "pat").is_dir())
    ):
        candidates = []
        for child in (resolved / "bed", resolved / "pat"):
            if child.is_dir():
                candidates.extend(child.rglob("*"))
    else:
        candidates = [resolved] if resolved.is_file() else list(resolved.rglob("*")) if resolved.is_dir() else []
    files = [item for item in candidates if item.is_file()]
    return {
        "bed": sorted(
            item for item in files
            if item.name.endswith((".bed", ".txt", ".tsv"))
            and not item.name.endswith((".pat", ".pat.gz"))
        ),
        "pat": sorted(item for item in files if item.name.endswith((".pat", ".pat.gz"))),
        "bam": sorted(item for item in files if item.name.endswith((".bam", ".cram"))),
    }


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass
class DoctorResult:
    checks: list[Check] = field(default_factory=list)
    bundle: ReferenceBundle | None = None
    selected_tools: list[str] = field(default_factory=list)
    skipped_tools: list[dict[str, str]] = field(default_factory=list)
    runtime_files: dict[str, Path] = field(default_factory=dict)
    external_runtime_paths: dict[str, Path] = field(default_factory=dict)
    external_runtime_metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    data_exports: dict[str, dict[str, Path]] = field(default_factory=dict)
    execution_parameters: dict[str, dict[str, object]] = field(default_factory=dict)
    container_engine: str | None = None
    container_executable: str | None = None
    container_engine_version: str | None = None
    container_engine_source: str | None = None
    container_sif_smoke: dict[str, str] = field(default_factory=dict)
    nextflow_command: list[str] | None = None
    accelerator: str = "cpu"
    projection_preflight: dict | None = None
    projection_report: dict | None = None
    release_disclosures: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(check.ok or not check.required for check in self.checks)

    def add(self, name: str, ok: bool, detail: str, required: bool = True) -> None:
        self.checks.append(Check(name, ok, detail, required))


def choose_container_engine(requested: str) -> tuple[str | None, str | None]:
    requested_path = Path(requested).expanduser()
    if requested_path.is_absolute():
        engine = requested_path.name.lower()
        if engine not in {"apptainer", "singularity"}:
            return None, None
        if not requested_path.is_file() or not os.access(requested_path, os.X_OK):
            return engine, None
        return engine, str(requested_path)
    if requested == "apptainer":
        return ("apptainer", shutil.which("apptainer"))
    if requested == "singularity":
        return ("singularity", shutil.which("singularity"))
    if requested != "auto":
        return None, None
    apptainer = shutil.which("apptainer")
    if apptainer:
        return "apptainer", apptainer
    singularity = shutil.which("singularity")
    if singularity:
        return "singularity", singularity
    return None, None


def inspect_container_engine(requested: str, executable: str) -> tuple[bool, str, str, str]:
    """Return a bounded version probe and the executable's discovery source."""
    path = Path(executable)
    source = "explicit" if Path(requested).expanduser().is_absolute() else "system"
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if source != "explicit" and conda_prefix:
        try:
            path.resolve().relative_to(Path(conda_prefix).resolve())
            source = "conda"
        except (OSError, ValueError):
            pass
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", source, f"version probe failed: {exc}"
    version_text = (completed.stdout.strip() or completed.stderr.strip()).splitlines()
    version = version_text[0] if version_text else ""
    detail = f"{path}; source={source}; version={version or 'not reported'}"
    if completed.returncode != 0:
        detail += f"; exit_code={completed.returncode}"
    return completed.returncode == 0, version, source, detail


def check_container_sif_smoke(
    *, executable: str, engine: str, runtime_file: Path, timeout_seconds: int = 30
) -> tuple[bool, str]:
    """Run a bounded, no-input `true` command inside one local SIF image."""
    if not runtime_file.is_file() or runtime_file.suffix.lower() != ".sif":
        return False, f"container runtime is not a local SIF file: {runtime_file}"
    try:
        completed = subprocess.run(
            [executable, "exec", str(runtime_file), "true"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"SIF smoke exceeded {timeout_seconds}s: {runtime_file}"
    except OSError as exc:
        return False, f"could not start {engine} for SIF smoke: {exc}"
    detail = f"{engine} exec {runtime_file.name} true exit_code={completed.returncode}"
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout).strip()[-500:]
        if tail:
            detail += f"; {tail}"
    return completed.returncode == 0, detail


def check_epidish_builtin_reference(
    *, runtime_file: Path | None, container_engine: str | None, dataset: str
) -> tuple[bool, str]:
    """Verify that the immutable EpiDISH runtime exports the requested dataset."""
    if not runtime_file:
        return False, "EpiDISH runtime is unavailable"
    executable = shutil.which(container_engine or "")
    if not executable:
        return False, "container engine is unavailable for EpiDISH built-in reference check"
    if not dataset or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_." for character in dataset):
        return False, f"unsafe EpiDISH built-in reference name: {dataset!r}"
    expression = (
        "e <- new.env(parent=emptyenv()); "
        f"data(list='{dataset}', package='EpiDISH', envir=e); "
        f"stopifnot(exists('{dataset}', envir=e, inherits=FALSE)); "
        f"x <- get('{dataset}', envir=e, inherits=FALSE); "
        "stopifnot(is.matrix(x), nrow(x)>0, ncol(x)>0); "
        f"cat(as.character(packageVersion('EpiDISH')), '{dataset}', nrow(x), ncol(x), sep='\\t')"
    )
    try:
        completed = subprocess.run(
            [executable, "exec", str(runtime_file), "Rscript", "-e", expression],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not inspect EpiDISH runtime: {exc}"
    detail = completed.stdout.strip() or completed.stderr.strip()
    return completed.returncode == 0, detail or f"EpiDISH dataset {dataset} check failed"


def check_array3_runtime_versions(
    *, tool: str, runtime_file: Path | None, container_engine: str | None
) -> tuple[bool, str]:
    """Read required package versions from the selected immutable local SIF."""
    if runtime_file is None:
        return False, f"{tool} runtime is unavailable"
    executable = shutil.which(container_engine or "")
    if not executable:
        return False, "container engine is unavailable for runtime version inspection"
    if tool == "ARIC":
        command = [
            "python3", "-c",
            "import importlib.metadata as m,platform; "
            "print('Python='+platform.python_version()); "
            "print('ARIC='+m.version('ARIC')); "
            "print('numpy='+m.version('numpy')); print('pandas='+m.version('pandas'))",
        ]
    else:
        packages = {
            "MethylCIBERSORT": ("MethylCIBERSORT", "CIBERSORT", "limma"),
            "MeDeCom": ("MeDeCom",),
        }.get(tool)
        if packages is None:
            return False, f"no version probe is registered for {tool}"
        literal = "c(" + ",".join(json.dumps(name) for name in packages) + ")"
        expression = (
            f"p <- {literal}; missing <- p[!vapply(p, requireNamespace, logical(1), quietly=TRUE)]; "
            "if(length(missing)) stop(paste('missing',paste(missing,collapse=','))); "
            "cat(paste0('R=',R.version$major,'.',R.version$minor,';')); "
            "for(x in p) cat(paste0(x,'=',as.character(packageVersion(x)),';'))"
        )
        command = ["Rscript", "-e", expression]
    try:
        completed = subprocess.run(
            [executable, "exec", "--cleanenv", str(runtime_file), *command],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not inspect {tool} runtime: {exc}"
    detail = completed.stdout.strip() or completed.stderr.strip()
    return completed.returncode == 0, detail or f"{tool} version probe failed"


def check_menet_contract_and_input(
    *, bundle: ReferenceBundle, input_path: Path | None, runtime_file: Path | None,
    container_engine: str | None,
) -> tuple[bool, str, dict]:
    """Inspect a trusted bundled MEnet pickle and reject low-overlap inputs."""
    if runtime_file is None:
        return False, "MEnet runtime is unavailable", {}
    executable = shutil.which(container_engine or "")
    if not executable:
        return False, "container engine is unavailable for MEnet preflight", {}
    capability = bundle.tools["MEnet"]
    artifact_key = "native_model" if is_native_wgbs_contract(bundle.contract) else "model"
    if artifact_key not in capability.artifacts:
        return False, f"MEnet capability has no {artifact_key} artifact", {}
    if input_path is None:
        return False, "MEnet preflight requires an input path", {}
    resolved = input_path.expanduser().resolve()
    if is_native_wgbs_contract(bundle.contract):
        bed_root = resolved / "bed" if resolved.is_dir() and (resolved / "bed").is_dir() else resolved
        inputs = [bed_root] if bed_root.is_file() else sorted(bed_root.glob("*.bed")) if bed_root.is_dir() else []
        input_type = "native-bed"
    else:
        inputs = [resolved] if resolved.is_file() else sorted(resolved.glob("*.csv")) if resolved.is_dir() else []
        input_type = "array"
    if not inputs:
        return False, f"no MEnet-compatible {input_type} inputs found under {resolved}", {}
    policy = capability.validation_policy or {}
    try:
        minimum_regions = int(policy.get("minimum_overlap_regions", 100))
        minimum_fraction = float(policy.get("minimum_overlap_fraction", 0.05))
    except (TypeError, ValueError) as exc:
        return False, f"invalid MEnet overlap policy: {exc}", {}
    script = PROJECT_ROOT / "deconvolution/bin/menet.py"
    model = bundle.artifact("MEnet", artifact_key)
    with tempfile.TemporaryDirectory(prefix="demethflow-menet-doctor-") as temporary:
        cells = Path(temporary) / "cell_types.json"
        cells.write_text(json.dumps(list(bundle.cell_types), ensure_ascii=False), encoding="utf-8")
        bind_roots = {
            PROJECT_ROOT.parent.resolve(), model.parent.resolve(), cells.parent.resolve(),
            *(path.parent.resolve() for path in inputs),
        }
        command = [executable, "exec", "--cleanenv"]
        for root in sorted(bind_roots, key=str):
            command.extend(["--bind", f"{root}:{root}"])
        command.extend([
            str(runtime_file), "python3", str(script), "inspect-input",
            "--model", str(model), "--cell-types", str(cells),
            "--genome-build", str(bundle.payload.get("genome_build", "unknown")),
            "--input-type", input_type,
            "--min-overlap-regions", str(minimum_regions),
            "--min-overlap-fraction", str(minimum_fraction),
        ])
        for path in inputs:
            command.extend(["--input", str(path)])
        try:
            completed = subprocess.run(
                command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=900,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"could not execute MEnet preflight: {exc}", {}
    report: dict = {}
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("schema") == "demethflow-menet-input-preflight-v1":
            report = candidate
            break
    if not report:
        detail = completed.stderr.strip() or completed.stdout.strip() or "MEnet preflight emitted no JSON report"
        return False, detail, {}
    summaries = [
        f"{Path(item['input_path']).name}:{item['observed_overlap_regions']}/"
        f"{item['model_region_count']} (min {item['effective_minimum_overlap_regions']})"
        for item in report.get("inputs", [])
    ]
    warning_count = len(report.get("model_qc", {}).get("load_warnings", []))
    cross_platform_ok = True
    if bundle.contract == EPIC_FROM_450K_CONTRACT:
        try:
            baseline = int(policy["cross_platform_baseline_observed_regions"])
            minimum_ratio = float(policy.get("minimum_cross_platform_baseline_ratio", 0.90))
            maximum_imputed = float(policy.get("maximum_imputed_region_fraction", 0.10))
        except (KeyError, TypeError, ValueError) as exc:
            return False, f"invalid cross-platform MEnet policy: {exc}", report
        for item in report.get("inputs", []):
            ratio = item["observed_overlap_regions"] / baseline if baseline else 0.0
            imputed = 1.0 - item["observed_overlap_regions"] / item["model_region_count"]
            item["cross_platform_450k_baseline_observed_regions"] = baseline
            item["cross_platform_baseline_ratio"] = ratio
            item["imputed_region_fraction"] = imputed
            item["minimum_cross_platform_baseline_ratio"] = minimum_ratio
            item["maximum_imputed_region_fraction"] = maximum_imputed
            item["cross_platform_gate_status"] = (
                "PASS" if ratio >= minimum_ratio and imputed <= maximum_imputed else "FAIL"
            )
            cross_platform_ok &= item["cross_platform_gate_status"] == "PASS"
        summaries.append(
            "cross-platform 450K-baseline gate=" + ("PASS" if cross_platform_ok else "FAIL")
        )
    detail = "; ".join(summaries) + f"; model warnings={warning_count}"
    return (
        completed.returncode == 0 and report.get("status") == "PASS" and cross_platform_ok,
        detail,
        report,
    )


def check_menet_device_runtime(
    *, bundle: ReferenceBundle, runtime_file: Path | None,
    container_engine: str | None, accelerator: str,
) -> tuple[bool, str, dict]:
    """Verify the selected MEnet runtime can execute one real device forward."""
    if accelerator != "gpu":
        return True, "MEnet CPU mode does not require a CUDA runtime check", {}
    if runtime_file is None:
        return False, "MEnet GPU runtime is unavailable", {}
    executable = shutil.which(container_engine or "")
    if not executable:
        return False, "container engine is unavailable for MEnet GPU check", {}
    capability = bundle.tools["MEnet"]
    artifact_key = "native_model" if is_native_wgbs_contract(bundle.contract) else "model"
    try:
        model = bundle.artifact("MEnet", artifact_key)
    except DeMethFlowError as exc:
        return False, str(exc), {}
    script = PROJECT_ROOT / "deconvolution/bin/menet.py"
    with tempfile.TemporaryDirectory(prefix="demethflow-menet-gpu-doctor-") as temporary:
        cells = Path(temporary) / "cell_types.json"
        report_path = Path(temporary) / "device_qc.json"
        cells.write_text(json.dumps(list(bundle.cell_types), ensure_ascii=False), encoding="utf-8")
        bind_roots = {PROJECT_ROOT.parent.resolve(), model.parent.resolve(), cells.parent.resolve()}
        command = [executable, "exec", "--nv", "--cleanenv"]
        for root in sorted(bind_roots, key=str):
            command.extend(["--bind", f"{root}:{root}"])
        command.extend([
            str(runtime_file), "python3", str(script), "inspect-model",
            "--model", str(model), "--cell-types", str(cells),
            "--genome-build", str(bundle.payload.get("genome_build", "unknown")),
            "--device", "cuda", "--output", str(report_path),
        ])
        try:
            completed = subprocess.run(
                command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"could not execute MEnet GPU doctor: {exc}", {}
        report = {}
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = {}
        if completed.returncode != 0 or report.get("status") != "PASS":
            detail = completed.stderr.strip() or completed.stdout.strip() or "MEnet GPU device smoke check failed"
            return False, detail, report
        if not report.get("model_on_device") or not str(report.get("output_tensor_device", "")).startswith("cuda"):
            return False, "MEnet GPU doctor did not prove model and output tensor were on CUDA", report
        return True, (
            f"CUDA forward PASS on {report.get('cuda_device_name')} "
            f"({report.get('device_resolved')}); FP32; no mixed precision"
        ), report


def check_methylbert_runtime(
    *, runtime_file: Path | None, container_engine: str | None, accelerator: str,
) -> tuple[bool, str]:
    """Check that the selected MethylBERT runtime imports offline.

    This is intentionally an operational check only.  Model accuracy is not
    evaluated here; device/model/output checks are recorded by the real E2E
    runner and its runtime summary.
    """
    if runtime_file is None:
        return False, f"MethylBERT-{accelerator} runtime is unavailable"
    executable = shutil.which(container_engine or "")
    if not executable:
        return False, "container engine is unavailable for MethylBERT runtime check"
    command = [executable, "exec", "--cleanenv"]
    if accelerator == "gpu":
        command.insert(2, "--nv")
    command.extend([
        str(runtime_file), "python", "-c",
        "import methylbert, torch; print('methylbert=' + getattr(methylbert, '__version__', 'unknown')); "
        "print('torch=' + torch.__version__); print('cuda=' + str(torch.cuda.is_available()))",
    ])
    environment = dict(os.environ)
    if accelerator == "cpu":
        environment["CUDA_VISIBLE_DEVICES"] = ""
    try:
        completed = subprocess.run(
            command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=120, env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not execute MethylBERT runtime check: {exc}"
    if completed.returncode != 0:
        return False, completed.stderr.strip() or completed.stdout.strip() or "MethylBERT runtime import failed"
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    version = next((line.split("=", 1)[1] for line in lines if line.startswith("methylbert=")), "unknown")
    cuda = next((line.split("=", 1)[1] for line in lines if line.startswith("cuda=")), "unknown")
    if accelerator == "cpu" and cuda.lower() == "true":
        return False, "MethylBERT CPU runtime unexpectedly reports CUDA available"
    return True, f"MethylBERT {version} import PASS ({accelerator}, cuda={cuda})"


def check_methylbert_reference_contract(
    *, bundle: ReferenceBundle, data_exports: dict[str, dict[str, Path]],
) -> tuple[bool, str]:
    """Verify the reference-side MethylBERT contract before an E2E run.

    This deliberately validates only identity and input contracts.  Accuracy is
    assessed by fixed-seed E2E evidence, never inferred from doctor output.
    """
    capability = bundle.tools["MethylBERT"]
    try:
        metadata_path = bundle.artifact("MethylBERT", "runtime_metadata")
        markers_path = bundle.artifact("MethylBERT", "markers")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("scenario_id") != bundle.scenario_id:
            raise DeMethFlowError("runtime metadata scenario does not match reference")
        if metadata.get("genome_build") != bundle.payload.get("genome_build"):
            raise DeMethFlowError("runtime metadata genome build does not match reference")
        records = metadata.get("cell_types")
        if not isinstance(records, list) or not records:
            raise DeMethFlowError("runtime metadata has no MethylBERT cell types")
        def norm(value: object) -> str:
            return "".join(character.lower() for character in str(value) if character.isalnum())
        declared: set[str] = set()
        for cell in bundle.cell_types:
            declared.update(norm(value) for value in (cell["cell_type_id"], cell["display_name"], *cell.get("synonyms", [])))
        # A MethylBERT training label can be a historical label (for example
        # ``Granulocyte``) while the release schema deliberately uses a
        # canonical label (``neutrophils``).  The explicit per-record mapping
        # is part of the immutable runtime metadata; it avoids a hidden,
        # tool-specific alias table in the execution layer.
        model_labels = [norm(record.get("name", "")) for record in records]
        canonical_labels = [
            norm(record.get("reference_cell_type_id", record.get("name", "")))
            for record in records
        ]
        if (
            any(not label for label in model_labels)
            or len(canonical_labels) != len(set(canonical_labels))
            or any(label not in declared for label in canonical_labels)
        ):
            raise DeMethFlowError("MethylBERT runtime cell types do not map one-to-one to reference cell types")
        with markers_path.open(newline="", encoding="utf-8") as handle:
            markers = list(csv.DictReader(handle, delimiter="\t"))
        dmr_ids = [str(row.get("dmr_id", "")).strip() for row in markers]
        if not markers or len(dmr_ids) != len(set(dmr_ids)) or any(not item for item in dmr_ids):
            raise DeMethFlowError("MethylBERT marker DMR IDs are missing or duplicated")
        if len(markers) != int(metadata.get("num_dmrs", -1)):
            raise DeMethFlowError("MethylBERT marker count does not match runtime metadata")
        module_name = f"MethylBERT-{bundle.payload['genome_build']}-data"
        exports = data_exports.get(module_name, {})
        root = exports.get("methylbert_reference_root")
        if root is None:
            raise DeMethFlowError(f"{module_name} does not export methylbert_reference_root")
        build = str(bundle.payload["genome_build"])
        required_cache = [root / "data" / "cell_type_match.json", root / "data" / build / f"{build}_cpgs.csv", root / "data" / build / f"{build}_genome.pk"]
        missing = [str(path) for path in required_cache if not path.is_file()]
        if missing:
            raise DeMethFlowError("MethylBERT genome cache is incomplete: " + ", ".join(missing))
        if capability.output_contract != "cell_proportions_v1":
            raise DeMethFlowError(f"unexpected MethylBERT output contract {capability.output_contract!r}")
    except (OSError, ValueError, TypeError, json.JSONDecodeError, DeMethFlowError) as exc:
        return False, str(exc)
    return True, (
        f"scenario={bundle.scenario_id}; build={build}; K={len(model_labels)}; "
        f"DMRs={len(markers)}; cache={module_name}; output=cell_proportions_v1"
    )


def bundled_nextflow() -> list[str] | None:
    launcher = PROJECT_ROOT / "runtime" / "nextflow" / "bin" / "nextflow"
    java = PROJECT_ROOT / "runtime" / "jre" / "bin" / "java"
    if launcher.is_file() and os.access(launcher, os.X_OK) and java.is_file():
        return [str(launcher)]
    override = os.environ.get("METHUNMIX_NEXTFLOW_CMD") or os.environ.get("DEMETHFLOW_NEXTFLOW_CMD")
    if override:
        return override.split()
    executable = shutil.which("nextflow")
    return [executable] if executable else None


def find_runtime_file(module_name: str, runtime_root: Path | None = None) -> Path | None:
    filename = RUNTIME_FILES.get(module_name)
    if not filename:
        return None
    roots: list[Path] = []
    if runtime_root:
        roots.append(runtime_root)
    elif os.environ.get("METHUNMIX_RUNTIME_ROOT"):
        roots.append(Path(os.environ["METHUNMIX_RUNTIME_ROOT"]))
    elif os.environ.get("DEMETHFLOW_RUNTIME_ROOT"):
        roots.append(Path(os.environ["DEMETHFLOW_RUNTIME_ROOT"]))
    roots.extend([default_data_home() / "runtimes", legacy_data_home() / "runtimes", PROJECT_ROOT / "runtimes"])
    for root in roots:
        direct = root / "sifs" / filename
        if direct.is_file():
            return direct.resolve()
        for path in root.glob(f"*/**/{filename}") if root.is_dir() else ():
            if path.is_file():
                return path.resolve()
    return None


def find_data_module(module_name: str, runtime_root: Path | None = None) -> tuple[Path, dict[str, Path]] | None:
    roots: list[Path | None] = [runtime_root, default_data_home(), legacy_data_home(), PROJECT_ROOT]
    seen: set[Path] = set()
    for root in roots:
        if root is None:
            continue
        resolved = root.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        module = ModuleStore(resolved).find(module_name)
        if module and module.payload.get("module_type") == "tool-data":
            exports = {
                name: (module.install_root / relative).resolve()
                for name, relative in module.payload.get("exports", {}).items()
            }
            if all(path.exists() for path in exports.values()):
                return module.install_root, exports

    # Developer-tree fallbacks. Release installations receive the same files in
    # explicit tool-data modules and never depend on another reference.
    if module_name in {"WGBSTools-hg19-data", "WGBSTools-hg38-data"}:
        build = "hg19" if "hg19" in module_name else "hg38"
        reference_root = PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / build
        required = [
            reference_root / "reference_manifest.json",
            reference_root / "CpG.bed.gz",
            reference_root / "CpG.bed.gz.csi",
            reference_root / "rev.CpG.bed.gz",
            reference_root / "rev.CpG.bed.gz.tbi",
            reference_root / "CpG.chrome.size",
            reference_root / "chrome.size",
            reference_root / ("genome.fa.gz" if build == "hg19" else "genome.fa"),
            reference_root / ("genome.fa.gz.fai" if build == "hg19" else "genome.fa.fai"),
        ]
        if all(path.is_file() and path.stat().st_size > 0 for path in required):
            return reference_root, {f"wgbstools_{build}_reference": reference_root}
    if module_name == "MethylBERT-hg38-data":
        reference_root = (
            PROJECT_ROOT
            / "references"
            / "builtin.immune6.wgbs-native"
            / "1.1.0"
            / "artifacts"
            / "methylbert"
        )
        exports = {"methylbert_reference_root": reference_root}
        if reference_root.is_dir():
            return reference_root, exports
    if module_name == "CelFEER-hg38-data":
        cpg = PROJECT_ROOT / "nextflow_ref" / "assets" / "celfeer" / "references" / "hg38_cpg_ref.txt"
        bins = PROJECT_ROOT / "nextflow_ref" / "assets" / "celfeer" / "CelFEER-main" / "data" / "read_bins.txt"
        if cpg.is_file() and bins.is_file():
            return cpg.parent, {
                "celfeer_hg38_cpg_reference": cpg,
                "celfeer_hg38_read_bins": bins,
            }
    if module_name == "CelFEER-hg19-data":
        cpg = PROJECT_ROOT / "nextflow_ref" / "assets" / "celfeer" / "references" / "hg19_cpg_ref.txt"
        bins = PROJECT_ROOT / "nextflow_ref" / "assets" / "celfeer" / "CelFEER-main" / "data" / "hg19_read_bins.txt"
        if cpg.is_file() and bins.is_file():
            return cpg.parent, {
                "celfeer_hg19_cpg_reference": cpg,
                "celfeer_hg19_read_bins": bins,
            }
    if module_name == "MethylBERT-hg19-data":
        reference_root = PROJECT_ROOT / "nextflow_ref" / "assets" / "methylbert" / "hg19"
        required = [
            reference_root / "src",
            reference_root / "data" / "cell_type_match.json",
            reference_root / "data" / "hg19" / "hg19_cpgs.csv",
            reference_root / "data" / "hg19" / "hg19_genome.pk",
        ]
        if all(path.exists() for path in required):
            return reference_root, {"methylbert_reference_root": reference_root}
    return None


def doctor(
    *,
    mode: str,
    reference: str | None,
    scenario: str | None,
    platform_name: str | None,
    genome_build: str | None = None,
    tools: str,
    input_path: Path | None,
    reference_root: Path | None,
    runtime_root: Path | None,
    external_runtime_root: Path | None = None,
    runtime: str,
    container_engine: str,
    allow_cross_platform: bool,
    accelerator: str = "cpu",
    verify_reference_checksums: bool = False,
    medecom_k: int | None = None,
    medecom_lambda: str = "auto",
    medecom_nfolds: int | None = None,
    methylcibersort_permutations: int = 1000,
    analysis_contract: str | None = None,
    allow_validation_candidate: bool = False,
) -> DoctorResult:
    result = DoctorResult(accelerator=accelerator)
    input_is_bam = bool(input_path and is_bam_input(input_path))
    result.add("host-os", platform.system() == "Linux", f"{platform.system()} {platform.machine()}")
    result.add("host-arch", platform.machine() in {"x86_64", "amd64"}, platform.machine())
    result.add("python", tuple(map(int, platform.python_version_tuple()[:2])) >= (3, 9), platform.python_version())
    nf = bundled_nextflow()
    result.nextflow_command = nf
    result.add("nextflow", bool(nf), " ".join(nf) if nf else "bundled/system Nextflow not found")
    if runtime == "container":
        engine, executable = choose_container_engine(container_engine)
        result.container_engine = engine
        result.container_executable = executable
        if executable:
            engine_ok, version, source, detail = inspect_container_engine(container_engine, executable)
            result.container_engine_version = version or None
            result.container_engine_source = source
            result.add("container-engine", engine_ok, detail)
        else:
            result.add(
                "container-engine",
                False,
                f"{container_engine} not found or is not an executable Apptainer/Singularity path",
            )
    else:
        result.add("conda-backend", bool(shutil.which("conda")), shutil.which("conda") or "conda not found")
    effective_genome_build = genome_build
    if analysis_contract == EPIC_FROM_450K_CONTRACT:
        result.add(
            "cross-platform-opt-in",
            allow_cross_platform,
            (
                "explicit --allow-cross-platform opt-in confirmed"
                if allow_cross_platform
                else "--allow-cross-platform is required for array_epic_from_450k_common_cpg_v1"
            ),
        )
        result.add(
            "projection-input-platform",
            platform_name == "epic",
            f"requested platform={platform_name!r}; only EPIC v1 is supported",
        )
        if mode == "decon":
            result.add(
                "explicit-derived-reference",
                bool(reference and "@" in reference),
                (
                    f"exact derived reference selected: {reference}"
                    if reference and "@" in reference
                    else "an exact --reference REFERENCE_ID@VERSION is required for the derived route"
                ),
            )
    if analysis_contract in {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}:
        result.add(
            "cross-platform-opt-in",
            allow_cross_platform,
            (
                "explicit --allow-cross-platform opt-in confirmed"
                if allow_cross_platform
                else f"--allow-cross-platform is required for {analysis_contract}"
            ),
        )
        result.add(
            "projection-input-platform",
            platform_name == "wgbs",
            (
                f"requested platform={platform_name!r}; build-matched 0-based WGBS BED "
                "or regular bisulfite BAM is supported"
            ),
        )
        result.add(
            "projection-genome-build",
            genome_build in {"hg19", "hg38"},
            f"requested genome build={genome_build!r}; explicit hg19/hg38 selection is required",
        )
        result.add(
            "projection-scenario",
            bool(scenario),
            (
                f"explicit scenario={scenario!r}"
                if scenario else "--scenario is required for WGBS-derived array projection"
            ),
        )
        if mode == "decon":
            result.add(
                "explicit-derived-reference",
                bool(reference and "@" in reference),
                (
                    f"exact derived reference selected: {reference}"
                    if reference and "@" in reference
                    else "an exact --reference REFERENCE_ID@VERSION is required for the WGBS-derived route"
                ),
            )
    if mode in {"decon", "build_and_decon"}:
        if platform_name in {"450k", "epic"} and genome_build is not None:
            result.add("genome-build-selection", False, "--genome-build is valid only for native WGBS input")
        if (
            platform_name == "wgbs"
            and not allow_cross_platform
            and not (reference and "wgbs-epic" in reference)
        ):
            effective_genome_build = genome_build or "hg38"
        if not input_path:
            result.add("input", False, "--input is required")
        else:
            resolved_input = input_path.expanduser().resolve()
            if input_is_bam:
                try:
                    samples = discover_bam_samples(resolved_input)
                    result.add(
                        "input", True,
                        f"{resolved_input}; {len(samples)} regular bisulfite BAM sample(s) will be materialized before deconvolution",
                    )
                except DeMethFlowError as exc:
                    result.add("input", False, str(exc))
            else:
                supported = ("*.csv", "*.bed", "*.txt", "*.tsv", "*.pat", "*.pat.gz")
                has_input = (
                    resolved_input.is_file()
                    and resolved_input.name.endswith((".csv", ".bed", ".txt", ".tsv", ".pat", ".pat.gz"))
                ) or (
                    resolved_input.is_dir()
                    and any(any(resolved_input.rglob(pattern)) for pattern in supported)
                )
                result.add("input", has_input, str(resolved_input))
        catalog = ReferenceCatalog.discover(reference_root).scan(verify_files=False)
        result.add(
            "reference-catalog",
            not catalog.problems,
            f"{len(catalog.bundles)} bundles; {len(catalog.problems)} invalid manifests",
            required=False,
        )
        try:
            bundle = catalog.resolve(
                reference=reference,
                scenario=scenario,
                platform=platform_name,
                genome_build=effective_genome_build,
                allow_cross_platform=allow_cross_platform,
                analysis_contract=analysis_contract,
            )
            result.bundle = bundle
            result.add("reference-resolution", True, bundle.selector)
            if bundle.contract == EPIC_FROM_450K_CONTRACT:
                result.add(
                    "explicit-projection-contract",
                    analysis_contract == EPIC_FROM_450K_CONTRACT,
                    (
                        "explicit array_epic_from_450k_common_cpg_v1 selection confirmed"
                        if analysis_contract == EPIC_FROM_450K_CONTRACT
                        else "derived EPIC reference requires --analysis-contract array_epic_from_450k_common_cpg_v1"
                    ),
                )
                result.add(
                    "cross-platform-reference-source",
                    allow_cross_platform and bundle.source_platform == "450k" and platform_name == "epic",
                    f"input={platform_name}; reference_source={bundle.source_platform}",
                )
            if is_wgbs_derived_array_contract(bundle.contract):
                target = target_platform_for_contract(bundle.contract)
                result.add(
                    "explicit-projection-contract",
                    analysis_contract == bundle.contract,
                    f"requested={analysis_contract!r}; required={bundle.contract!r}",
                )
                result.add(
                    "cross-platform-reference-source",
                    allow_cross_platform and platform_name == "wgbs" and bundle.source_platform == target,
                    f"input={platform_name}; target_array={target}; reference_source={bundle.source_platform}",
                )
                result.add(
                    "projection-build-lock",
                    genome_build == bundle.payload.get("genome_build"),
                    f"input={genome_build!r}; mapping/reference={bundle.payload.get('genome_build')!r}",
                )
                result.add(
                    "projection-scenario-lock",
                    scenario == bundle.scenario_id,
                    f"requested={scenario!r}; reference={bundle.scenario_id!r}",
                )
            result.add("genome-build", True, str(bundle.payload["genome_build"]))
            aliases = {"cibersort": "MethylCIBERSORT", "tsisa": "Tsisal", "prmeth": "PRmeth"}
            normalized = {name.lower(): name for name in bundle.tools}
            requested_candidate_tools = {
                aliases.get(item.strip().lower()) or normalized.get(item.strip().lower())
                for item in tools.split(",")
                if item.strip()
            }
            requested_candidate_tools.discard(None)
            candidate_tokens = {
                bundle.tools[name].validation_candidate
                for name in requested_candidate_tools
                if name in bundle.tools and bundle.tools[name].validation_candidate
            }
            candidate_token = next(iter(candidate_tokens)) if len(candidate_tokens) == 1 else None
            expected_tools = VALIDATION_CANDIDATE_TOOLS.get(candidate_token or "", frozenset())
            exact_immune6_candidate = (
                candidate_token == "hg19_native_wgbs_immune6_celfeer_celfie_metdecode_v1"
                and bundle.reference_id == "builtin.immune6.wgbs-native-hg19"
                and bundle.version.endswith("-rc1")
            )
            exact_immune6_menet_candidate = (
                candidate_token == "hg19_native_wgbs_menet_v1"
                and bundle.reference_id == "builtin.immune6.wgbs-native-hg19"
                and bundle.version == "1.2.3-menet-rc1"
            )
            validation_candidate_ok = (
                allow_validation_candidate
                and bundle.contract == "wgbs_native_hg19"
                and bool(requested_candidate_tools)
                and requested_candidate_tools.issubset(expected_tools)
                and all(
                    bundle.tools[name].status == "QUARANTINED"
                    and bundle.tools[name].validation_candidate == candidate_token
                    for name in requested_candidate_tools
                )
                and (
                    exact_immune6_candidate
                    or exact_immune6_menet_candidate
                    or (
                        candidate_token == "hg19_native_wgbs_menet_v1"
                        and bundle.version.endswith("-rc2")
                    )
                )
            )
            result.add(
                "validation-candidate-gate",
                validation_candidate_ok if allow_validation_candidate else True,
                (
                    "isolated hg19 native-WGBS candidate validation explicitly authorized"
                    if validation_candidate_ok
                    else "not requested"
                ),
                required=allow_validation_candidate,
            )
            selected, skipped = bundle.selected_tools(
                tools,
                accelerator=accelerator,
                allow_validation_candidate=validation_candidate_ok,
            )
            # Third-party implementations are deliberately external to the
            # core wheel/sdist.  Resolve them once at doctor time so every
            # downstream workflow receives an explicit, auditable path and a
            # missing/restricted runtime fails closed before Nextflow starts.
            requested_external = set(selected)
            external_root = external_runtime_root or external_runtime_root_from_env()
            try:
                external = discover_external_runtimes(external_root, requested_external)
            except DeMethFlowError as exc:
                affected = sorted(requested_external & set(EXTERNAL_TOOLS))
                if affected:
                    for tool in affected:
                        if tool in selected:
                            selected.remove(tool)
                            skipped.append({"tool": tool, "status": "EXTERNAL_RUNTIME_UNAVAILABLE", "reason": str(exc)})
                        result.add(f"external-runtime:{tool}", False, str(exc))
                external = {}
            for tool, runtime in external.items():
                result.external_runtime_paths[tool] = runtime.path
                result.external_runtime_metadata[tool] = {
                    "identity": runtime.identity,
                    "version": runtime.version,
                    "license": runtime.license,
                    "required_files": ",".join(runtime.required_files),
                }
                result.add(f"external-runtime:{tool}", True, f"{runtime.path}; {runtime.identity} {runtime.version}")
            if input_is_bam:
                if platform_name != "wgbs":
                    result.add("input-route:bam", False, "BAM is supported only with --platform wgbs")
                else:
                    build = str(genome_build or bundle.payload.get("genome_build", ""))
                    common_runtime = find_runtime_file("wgbs-common", runtime_root)
                    result.add(
                        "input-route:wgbs-bam-materialization", True,
                        "regular bisulfite BAM will be validated and materialized to strict BED/PAT/lbeta before downstream execution",
                    )
                    result.add(
                        "runtime:wgbs-common-materialization", common_runtime is not None,
                        str(common_runtime) if common_runtime else "install runtime module wgbs-common",
                    )
                    if common_runtime:
                        result.runtime_files["wgbs-common"] = common_runtime
                    module_name = f"WGBSTools-{build}-data"
                    resolved_data = find_data_module(module_name, runtime_root)
                    result.add(
                        f"data-module:{module_name}", resolved_data is not None,
                        str(resolved_data[0]) if resolved_data else f"install data module {module_name}",
                    )
                    if resolved_data:
                        result.data_exports[module_name] = resolved_data[1]
            if input_path and is_native_wgbs_contract(bundle.contract) and not input_is_bam:
                inventory = _native_wgbs_input_inventory(input_path)
                if inventory["bam"]:
                    result.add(
                        "input-route:native-wgbs-bam",
                        False,
                        "BAM/CRAM is not a declared direct deconvolution input route in this release; "
                        "convert it to build-matched BED and/or indexed PAT before running",
                    )
                bed_tools = sorted(set(selected) & NATIVE_WGBS_BED_TOOLS)
                pat_tools = sorted(set(selected) & NATIVE_WGBS_PAT_TOOLS)
                if bed_tools:
                    result.add(
                        "input-route:native-wgbs-bed",
                        bool(inventory["bed"]),
                        (
                            f"{len(inventory['bed'])} BED-like input file(s) for {','.join(bed_tools)}"
                            if inventory["bed"]
                            else f"BED-like input is required by {','.join(bed_tools)}"
                        ),
                    )
                if pat_tools:
                    result.add(
                        "input-route:native-wgbs-pat",
                        bool(inventory["pat"]),
                        (
                            f"{len(inventory['pat'])} PAT input file(s) for {','.join(pat_tools)}"
                            if inventory["pat"]
                            else f"PAT input is required by {','.join(pat_tools)}"
                        ),
                    )
                if "CelFiE" in selected:
                    capability = bundle.tools["CelFiE"]
                    contract_ok = False
                    atlas_path = (
                        bundle.artifact("CelFiE", "atlas")
                        if "atlas" in capability.artifacts else None
                    )
                    if atlas_path is None:
                        result.add(
                            "celfie:VALID_COORDINATE_CONTRACT",
                            False,
                            "CelFiE capability has no atlas artifact",
                        )
                    else:
                        contract_ok, contract_detail, contract_metadata = inspect_celfie_atlas_contract(
                            atlas_path, bundle
                        )
                        result.add("celfie:VALID_COORDINATE_CONTRACT", contract_ok, contract_detail)
                        if contract_metadata:
                            result.execution_parameters["CelFiE"] = {
                                "coordinate_contract": contract_metadata,
                                "marker_coverage_report": "qc/CelFiE/<sample>.celfie_coverage.json",
                            }
                    bed_ok, bed_detail = inspect_celfie_bed_cpg_contract(inventory["bed"])
                    result.add("celfie:INPUT_PER_CPG_BED_CONTRACT", bed_ok, bed_detail)
                    operational = bool(atlas_path) and contract_ok and bed_ok
                    result.add(
                        "celfie:OPERATIONALLY_RUNNABLE",
                        operational,
                        (
                            "explicit atlas contract and native per-CpG BED preflight passed"
                            if operational else "coordinate/input contract must pass before CelFiE can run"
                        ),
                    )
                    scientific = capability.validation_scope == "fixed_seed_truth_labeled_simulated_native_wgbs_v1"
                    result.add(
                        "celfie:SCIENTIFICALLY_VALIDATED" if scientific else "celfie:SCIENTIFIC_VALIDATION_NOT_RUN",
                        True,
                        (
                            "current CelFiE release is bound to fixed-seed simulated-truth evidence"
                            if scientific else "no current CelFiE simulated-truth scientific promotion evidence"
                        ),
                        required=False,
                    )
            released_unvalidated = [
                tool for tool in selected
                if bundle.tools[tool].effective_status(accelerator)[0] == "RELEASED_UNVALIDATED"
            ]
            if released_unvalidated:
                result.add(
                    "scientific-limitation:released-unvalidated",
                    True,
                    (
                        "FORMAL OFFLINE RELEASE WITH A FAILED OR NON-PROMOTING SCIENTIFIC "
                        "VALIDATION RECORD; outputs are runnable but do not meet the configured "
                        "scientific accuracy promotion gate; tools=" + ",".join(released_unvalidated)
                    ),
                    required=False,
                )
            simulation_validated = [
                tool for tool in selected
                if bundle.tools[tool].validation_scope
                == "fixed_seed_truth_labeled_simulated_native_wgbs_v1"
            ]
            if simulation_validated:
                release = bundle.payload.get("release_validation", {})
                result.add(
                    "scientific-limitation:native-wgbs-simulation",
                    True,
                    release_warning_for_tools(release, simulation_validated) or (
                        "This native WGBS reference was validated using truth-labeled "
                        "simulated mixtures. Performance on independent real biological "
                        "cohorts with experimentally established cell proportions has not "
                        "been established; tools=" + ",".join(simulation_validated)
                    ),
                    required=False,
                )
            if "EpiSCORE" in selected:
                policy = bundle.tools["EpiSCORE"].validation_policy or {}
                warning = policy.get("observed_warning")
                if policy.get("policy_id") == "episcore-array-cross-platform-v2" and isinstance(warning, dict):
                    result.add(
                        "scientific-limitation:EpiSCORE",
                        True,
                        (
                            f"{warning.get('cell_type', 'unknown')} MAE={warning.get('mae')} lies in the "
                            "relaxed (0.20,0.23] band; limitation: reduced CD8 precision"
                        ),
                        required=False,
                    )
            if input_path and bundle.contract == EPIC_FROM_450K_CONTRACT:
                try:
                    projection = inspect_projection_inputs(bundle, input_path, selected)
                    result.projection_preflight = projection
                    details = "; ".join(
                        f"{Path(item['input_path']).name}: {item['hm450_common_probe_count']} common CpGs"
                        for item in projection["inputs"]
                    )
                    if projection.get("global_duplicate_sample_ids"):
                        details += "; duplicate sample IDs across matrices: " + ",".join(
                            projection["global_duplicate_sample_ids"]
                        )
                    result.add("platform-projection", projection["status"] == "PASS", details)
                except DeMethFlowError as exc:
                    result.add("platform-projection", False, str(exc))
            if input_path and is_wgbs_derived_array_contract(bundle.contract):
                try:
                    if input_is_bam:
                        samples = discover_bam_samples(input_path)
                        result.add(
                            "platform-projection", True,
                            f"{len(samples)} regular bisulfite BAM sample(s) will be materialized to six-column BED, then projected to "
                            f"{target_platform_for_contract(bundle.contract)}; header/coordinate/marker checks run during materialization",
                        )
                        result.projection_preflight = None
                    else:
                        projection = inspect_wgbs_projection_inputs(bundle, input_path, selected)
                    # The projection itself is valid only when its coordinate
                    # contract and shared CpG backbone pass.  Feature overlap
                    # is tool-specific: an explicit tool must fail, while
                    # ``--tools all`` uses the normal input-incompatible skip
                    # policy and records the exact frozen-overlap reason.
                    failed_tools = dict(projection.get("tool_errors", {})) if not input_is_bam else {}
                    select_all_requested = tools.strip().lower() == "all"
                    if not input_is_bam and failed_tools and select_all_requested:
                        for tool, reason in failed_tools.items():
                            if tool in selected:
                                selected.remove(tool)
                                skipped.append({
                                    "tool": tool,
                                    "status": "INPUT_INCOMPATIBLE",
                                    "reason": reason,
                                })
                                result.add(
                                    f"input-contract:{tool}", True,
                                    f"skipped by --tools all: {reason}", required=False,
                                )
                        # Persist the first audit, including skipped tools,
                        # but evaluate the runnable subset for the required
                        # platform-projection check used by execution.
                        projection["initial_tool_errors"] = failed_tools
                        runnable_projection = inspect_wgbs_projection_inputs(bundle, input_path, selected)
                        runnable_projection["initial_tool_errors"] = failed_tools
                        runnable_projection["skipped_tools"] = [
                            item for item in skipped if item["tool"] in failed_tools
                        ]
                        projection = runnable_projection
                    if not input_is_bam:
                        result.projection_preflight = projection
                        details = (
                            f"{projection['sample_count']} WGBS BED sample(s); "
                            f"{projection['shared_cg_probe_count']} shared exact CpGs; "
                            f"target={projection['target_platform']}; build={projection['genome_build']}"
                        )
                        result.add("platform-projection", projection["status"] == "PASS", details)
                except DeMethFlowError as exc:
                    result.add("platform-projection", False, str(exc))
            if "MethylCIBERSORT" in selected:
                permutations_ok = (
                    isinstance(methylcibersort_permutations, int)
                    and not isinstance(methylcibersort_permutations, bool)
                    and methylcibersort_permutations >= 0
                )
                result.add(
                    "parameter:MethylCIBERSORT:permutations",
                    permutations_ok,
                    str(methylcibersort_permutations),
                )
                if permutations_ok:
                    target_is_epic = platform_name == "epic" or (
                        is_wgbs_derived_array_contract(bundle.contract)
                        and target_platform_for_contract(bundle.contract) == "epic"
                    )
                    result.execution_parameters["MethylCIBERSORT"] = {
                        "permutations": methylcibersort_permutations,
                        # EPIC uses the sample-scoped adapter path. Each outer
                        # worker runs the package's unchanged three-nu CoreAlg,
                        # so the workflow declares 2 x 3 CPUs explicitly.
                        "sample_workers": 2 if target_is_epic else 1,
                    }
                    if target_is_epic:
                        result.execution_parameters["MethylCIBERSORT"].update({
                            "declared_cpus": 6,
                            "time_limit": "8h",
                            "parallelism_scope": "independent_mixture_samples",
                        })
            select_all = tools.strip().lower() == "all"
            if input_path and not (input_is_bam and is_wgbs_derived_array_contract(bundle.contract)) and (
                bundle.contract in {"array_450k_cpg", "array_epic_cpg", EPIC_FROM_450K_CONTRACT}
                or is_wgbs_derived_array_contract(bundle.contract)
            ):
                if is_wgbs_derived_array_contract(bundle.contract):
                    projection = result.projection_preflight
                    counts = [("WGBS-derived cohort", int(projection["sample_count"]))] if projection else []
                    count_error = None if projection else "WGBS-derived projection preflight did not complete"
                else:
                    counts, count_error = _array_input_sample_counts(input_path)
                if count_error:
                    result.add("input-matrix", False, count_error)
                else:
                    if "EDec" in selected:
                        invalid = [f"{path}: N={count}" for path, count in counts if count <= len(bundle.cell_types)]
                        detail = (
                            f"all matrices have N > K={len(bundle.cell_types)}"
                            if not invalid
                            else "EDec stage 1 requires N > K for stable unsupervised decomposition; " + "; ".join(invalid)
                        )
                        if invalid and select_all:
                            selected.remove("EDec")
                            skipped.append({"tool": "EDec", "status": "INPUT_INCOMPATIBLE", "reason": detail})
                            result.add("input-contract:EDec", True, f"skipped by --tools all: {detail}", required=False)
                        else:
                            result.add("input-contract:EDec", not invalid, detail)
                    if "Tsisal" in selected:
                        invalid = [f"{path}: N={count}" for path, count in counts if count <= len(bundle.cell_types)]
                        detail = (
                            f"all matrices have N > K={len(bundle.cell_types)}"
                            if not invalid
                            else "Tsisal requires N > K for every matrix; " + "; ".join(invalid)
                        )
                        if invalid and select_all:
                            selected.remove("Tsisal")
                            skipped.append({"tool": "Tsisal", "status": "INPUT_INCOMPATIBLE", "reason": detail})
                            result.add("input-contract:Tsisal", True, f"skipped by --tools all: {detail}", required=False)
                        else:
                            result.add("input-contract:Tsisal", not invalid, detail)
                    if "RefFreeEWAS" in selected:
                        invalid = [f"{path}: N={count}" for path, count in counts if count < 2]
                        detail = (
                            "all matrices contain at least two bulk samples"
                            if not invalid
                            else "RefFreeEWAS requires at least two samples per matrix; " + "; ".join(invalid)
                        )
                        if invalid and select_all:
                            selected.remove("RefFreeEWAS")
                            skipped.append(
                                {"tool": "RefFreeEWAS", "status": "INPUT_INCOMPATIBLE", "reason": detail}
                            )
                            result.add(
                                "input-contract:RefFreeEWAS",
                                True,
                                f"skipped by --tools all: {detail}",
                                required=False,
                            )
                        else:
                            result.add("input-contract:RefFreeEWAS", not invalid, detail)
                    if "MeDeCom" in selected:
                        capability = bundle.tools["MeDeCom"]
                        result.add(
                            "semantics:MeDeCom",
                            True,
                            "reference-free cohort decomposition; Component_1..K are anonymous and must not be interpreted as manifest cell-type labels",
                            required=False,
                        )
                        defaults = capability.execution_defaults or {}
                        try:
                            k = int(
                                medecom_k
                                if medecom_k is not None
                                else defaults.get("recommended_k", len(bundle.cell_types))
                            )
                        except (KeyError, TypeError, ValueError):
                            k = 0
                        invalid_k = k < 1
                        invalid_counts = [f"{path}: N={count}" for path, count in counts if count <= k]
                        if invalid_k:
                            result.add("input-contract:MeDeCom", False, "MeDeCom K must be a positive integer")
                        else:
                            result.add(
                                "input-contract:MeDeCom",
                                not invalid_counts,
                                f"all cohorts have N > K={k}" if not invalid_counts else "MeDeCom requires N > K for every cohort; " + "; ".join(invalid_counts),
                            )
                            if is_wgbs_derived_array_contract(bundle.contract):
                                projection = result.projection_preflight or {}
                                variable_count = int(projection.get("nonconstant_shared_cg_probe_count", 0))
                                minimum_variable = max(100, 10 * k)
                                inspection_error = (
                                    None if variable_count >= minimum_variable else
                                    f"WGBS-derived cohort has {variable_count} non-constant shared CpGs; "
                                    f"MeDeCom requires at least {minimum_variable}"
                                )
                                inspections = [{
                                    "path": "WGBS-derived cohort",
                                    "cpg_count": int(projection.get("shared_cg_probe_count", 0)),
                                    "variable_cpg_count": variable_count,
                                    "sample_count": int(projection.get("sample_count", 0)),
                                }]
                            else:
                                inspections, inspection_error = _inspect_medecom_cohorts(input_path, k)
                            result.add(
                                "input-content:MeDeCom",
                                inspection_error is None,
                                inspection_error or "; ".join(
                                    f"{item['path']}: {item['cpg_count']} CpGs, "
                                    f"{item['variable_cpg_count']} non-constant, N={item['sample_count']}"
                                    for item in inspections
                                ),
                            )
                        lambda_value = str(medecom_lambda).strip().lower()
                        if lambda_value != "auto":
                            try:
                                parsed_lambda = float(lambda_value)
                                lambda_ok = math.isfinite(parsed_lambda) and parsed_lambda >= 0
                            except ValueError:
                                lambda_ok = False
                            result.add("parameter:MeDeCom:lambda", lambda_ok, lambda_value)
                        if medecom_nfolds is not None:
                            folds_ok = medecom_nfolds >= 2 and all(medecom_nfolds <= count for _, count in counts)
                            result.add(
                                "parameter:MeDeCom:nfolds",
                                folds_ok,
                                str(medecom_nfolds) if folds_ok else "nfolds must be >=2 and <= N for every cohort",
                            )
                        result.execution_parameters["MeDeCom"] = {
                            "k": k,
                            "lambda": lambda_value,
                            "nfolds": medecom_nfolds,
                            "lambda_grid": [0, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1],
                        }
            elif input_path and input_is_bam and is_wgbs_derived_array_contract(bundle.contract):
                result.add(
                    "input-matrix", True,
                    "array matrix cardinality, nonconstant-CpG and tool-overlap gates are deferred until the declared BAM materialization completes",
                    required=False,
                )
            result.selected_tools = selected
            result.skipped_tools = skipped
            disclosure_tools = list(selected) + [
                item["tool"] for item in skipped
                if item.get("tool") in bundle.tools
            ]
            result.release_disclosures = release_disclosures_for_tools(
                bundle, disclosure_tools, accelerator=accelerator
            )
            # Keep the selection diagnostics explicit: a RU tool with a normal
            # policy is included in ``--tools all``; an explicit-only RU is
            # intentionally skipped and remains available only when named.
            for tool, disclosure in result.release_disclosures.items():
                if disclosure.get("status") == "RELEASED_UNVALIDATED":
                    result.add(
                        f"release-disclosure:{tool}",
                        True,
                        str(disclosure.get("warning") or "RELEASED_UNVALIDATED: runnable with scientific warning"),
                        required=False,
                    )
            result.add(
                "tool-selection",
                bool(selected),
                ",".join(selected) or "no runnable/input-compatible tools",
            )
            # Native WGBS MEnet always has Python preprocessing and strict
            # postprocessing tasks in addition to the MEnet inference task.
            # Older immune6 manifests predate explicit workflow-runtime
            # dependencies, so preserve their immutable content and resolve
            # the common image here for backward-compatible offline execution.
            if (
                is_native_wgbs_contract(bundle.contract) and "MEnet" in selected
                and "wgbs-common" not in bundle.tools["MEnet"].effective_runtime_modules(accelerator)
            ):
                common_runtime = find_runtime_file("wgbs-common", runtime_root)
                result.add(
                    "runtime:wgbs-common",
                    common_runtime is not None,
                    str(common_runtime) if common_runtime else "install runtime module wgbs-common",
                )
                if common_runtime:
                    result.runtime_files["wgbs-common"] = common_runtime
            for tool in selected:
                capability = bundle.tools[tool]
                for key in capability.artifacts:
                    try:
                        path = bundle.artifact(tool, key)
                        if verify_reference_checksums and path.is_file():
                            expected = bundle.payload.get("artifact_checksums", {}).get(capability.artifacts[key])
                            actual = sha256_file(path)
                            if expected and actual != expected:
                                raise DeMethFlowError(f"checksum mismatch: {path}")
                        result.add(f"artifact:{tool}:{key}", True, str(path))
                    except DeMethFlowError as exc:
                        result.add(f"artifact:{tool}:{key}", False, str(exc))
                for module_name in capability.effective_runtime_modules(accelerator):
                    runtime_file = find_runtime_file(module_name, runtime_root)
                    result.add(
                        f"runtime:{module_name}",
                        runtime_file is not None,
                        str(runtime_file) if runtime_file else f"install runtime module {module_name}",
                    )
                    if runtime_file:
                        result.runtime_files[module_name] = runtime_file
                for module_name in capability.data_modules:
                    resolved_data = find_data_module(module_name, runtime_root)
                    result.add(
                        f"data-module:{module_name}",
                        resolved_data is not None,
                        str(resolved_data[0]) if resolved_data else f"install data module {module_name}",
                    )
                    if resolved_data:
                        result.data_exports[module_name] = resolved_data[1]
            if "MethylBERT" in selected:
                ok, detail = check_methylbert_reference_contract(
                    bundle=bundle, data_exports=result.data_exports,
                )
                result.add("contract:MethylBERT", ok, detail)
            for tool in ("ARIC", "MethylCIBERSORT"):
                if tool in selected and "reference" in bundle.tools[tool].artifacts:
                    try:
                        labels = _reference_column_labels(bundle.artifact(tool, "reference"))
                        result.add(
                            f"cell-types:{tool}",
                            _reference_labels_cover_bundle(labels, bundle),
                            f"K={len(labels)}: {','.join(labels)}",
                        )
                    except DeMethFlowError as exc:
                        result.add(f"cell-types:{tool}", False, str(exc))
            if runtime == "container":
                for tool in ("ARIC", "MethylCIBERSORT", "MeDeCom"):
                    if tool in selected:
                        ok, detail = check_array3_runtime_versions(
                            tool=tool,
                            runtime_file=result.runtime_files.get(tool),
                            container_engine=result.container_executable,
                        )
                        result.add(f"runtime-version:{tool}", ok, detail)
                if "MethylBERT" in selected:
                    ok, detail = check_methylbert_runtime(
                        runtime_file=result.runtime_files.get(
                            "MethylBERT-gpu" if accelerator == "gpu" else "MethylBERT-cpu"
                        ),
                        container_engine=result.container_executable,
                        accelerator=accelerator,
                    )
                    result.add("runtime-version:MethylBERT", ok, detail)
                if "MEnet" in selected:
                    if input_is_bam:
                        ok, detail, report = (
                            True,
                            "MEnet feature-overlap gate is deferred until BAM materialization produces the exact strict BED/array matrix consumed by execution",
                            None,
                        )
                    elif is_wgbs_derived_array_contract(bundle.contract) and input_path:
                        # MEnet's trusted checker reads the final array matrix,
                        # not raw BED.  Materialize it in a private temporary
                        # directory so doctor proves the same model/feature
                        # gate that the production run will use.
                        with tempfile.TemporaryDirectory(prefix="demethflow-wgbs-array-menet-") as temporary:
                            projected_inputs, _ = project_wgbs_inputs(
                                bundle, input_path, selected, Path(temporary) / "projection"
                            )
                            ok, detail, report = check_menet_contract_and_input(
                                bundle=bundle,
                                input_path=projected_inputs,
                                runtime_file=result.runtime_files.get("MEnet"),
                                container_engine=result.container_executable,
                            )
                    else:
                        ok, detail, report = check_menet_contract_and_input(
                            bundle=bundle,
                            input_path=input_path,
                            runtime_file=result.runtime_files.get("MEnet"),
                            container_engine=result.container_executable,
                        )
                    result.add("contract-and-overlap:MEnet", ok, detail)
                    if report:
                        policy = bundle.tools["MEnet"].validation_policy or {}
                        result.execution_parameters["MEnet"] = {
                            "minimum_overlap_regions": int(policy.get("minimum_overlap_regions", 100)),
                            "minimum_overlap_fraction": float(policy.get("minimum_overlap_fraction", 0.05)),
                            "preflight": report,
                        }
                    device_ok, device_detail, device_report = check_menet_device_runtime(
                        bundle=bundle,
                        runtime_file=result.runtime_files.get("MEnet"),
                        container_engine=result.container_executable,
                        accelerator=accelerator,
                    )
                    result.add("device-forward:MEnet", device_ok, device_detail)
                    if device_report:
                        result.execution_parameters.setdefault("MEnet", {})[
                            "device_preflight"
                        ] = device_report
            if "EpiDISH" in selected:
                capability = bundle.tools["EpiDISH"]
                if capability.reference_mode == "package_builtin":
                    ok, detail = check_epidish_builtin_reference(
                        runtime_file=result.runtime_files.get("EpiDISH"),
                        container_engine=result.container_executable,
                        dataset=capability.builtin_reference or "",
                    )
                    result.add("builtin-reference:EpiDISH", ok, detail)
            if "CelFEER" in selected:
                capability = bundle.tools["CelFEER"]
                native_ok = is_native_wgbs_contract(bundle.contract)
                result.add("contract:CelFEER", native_ok, bundle.contract)
                result.add(
                    "genome-build:CelFEER",
                    bundle.payload.get("genome_build") in {"hg19", "hg38"},
                    str(bundle.payload.get("genome_build")),
                )
                try:
                    if input_is_bam:
                        result.add(
                            "input-contract:CelFEER", True,
                            "CelFEER PAT input contract is deferred until the declared BAM materialization emits indexed PAT",
                        )
                    else:
                        samples = discover_pat_samples(input_path) if input_path else []
                        result.add(
                            "input-contract:CelFEER",
                            bool(samples),
                            f"{len(samples)} PAT sample{'s' if len(samples) != 1 else ''}" if samples else "no .pat or .pat.gz input found",
                        )
                except DeMethFlowError as exc:
                    result.add("input-contract:CelFEER", False, str(exc))
                try:
                    if "cell_types" in capability.artifacts:
                        labels = read_cell_types(bundle.artifact("CelFEER", "cell_types"))
                    else:
                        labels = labels_from_manifest(list(bundle.cell_types))
                    marker_qc = inspect_marker_reference(bundle.artifact("CelFEER", "markers"), labels)
                    result.add("cell-types:CelFEER", True, f"K={len(labels)}: {','.join(labels)}")
                    result.add(
                        "marker-width:CelFEER",
                        True,
                        f"{marker_qc['observed_reference_width']}=3+5*{len(labels)}",
                    )
                except DeMethFlowError as exc:
                    result.add("marker-contract:CelFEER", False, str(exc))
                try:
                    build = str(bundle.payload["genome_build"])
                    if "read_bins" in capability.artifacts:
                        read_bins = bundle.artifact("CelFEER", "read_bins")
                    else:
                        exports = result.data_exports.get(f"CelFEER-{build}-data", {})
                        read_bins = exports.get(f"celfeer_{build}_read_bins")
                    if read_bins is None:
                        raise DeMethFlowError(
                            f"CelFEER-{build}-data does not export celfeer_{build}_read_bins"
                        )
                    missing_bins = markers_missing_from_read_bins(
                        bundle.artifact("CelFEER", "markers"), read_bins
                    )
                    result.add(
                        "marker-read-bins:CelFEER",
                        not missing_bins,
                        (
                            f"all markers are present in {build} read bins"
                            if not missing_bins
                            else f"{len(missing_bins)} markers are absent from {build} read bins"
                        ),
                    )
                except DeMethFlowError as exc:
                    result.add("marker-read-bins:CelFEER", False, str(exc))
                try:
                    free_bytes = shutil.disk_usage((input_path or PROJECT_ROOT).expanduser().resolve()).free
                    result.add("work-space:CelFEER", True, f"{free_bytes / (1024 ** 3):.1f} GiB free", required=False)
                except OSError as exc:
                    result.add("work-space:CelFEER", False, str(exc), required=False)
            if ({"MethylBERT", "MEnet"} & set(selected)) and accelerator == "gpu":
                gpu_ready = bool(shutil.which("nvidia-smi") or Path("/dev/nvidiactl").exists())
                result.add(
                    "accelerator:gpu",
                    gpu_ready,
                    "NVIDIA GPU detected for " + ",".join(sorted({"MethylBERT", "MEnet"} & set(selected)))
                    if gpu_ready else "GPU profile requested but no NVIDIA device was detected",
                )
        except DeMethFlowError as exc:
            result.add("reference-resolution", False, str(exc))
    if mode in {"build", "build_and_decon"}:
        build_workflow = PROJECT_ROOT / "nextflow_ref" / "main.nf"
        store = ModuleStore()
        build_runtime_module = store.find("build-runtime")
        build_assets_module = store.find("build-assets")
        bundled_sif = PROJECT_ROOT / "nextflow_ref" / "containers" / "deconvolution.sif"
        build_sif = (
            bundled_sif
            if bundled_sif.is_file()
            else (build_runtime_module.install_root / "containers" / "deconvolution.sif")
            if build_runtime_module
            else bundled_sif
        )
        bundled_assets = PROJECT_ROOT / "nextflow_ref" / "assets"
        bundled_tools = PROJECT_ROOT / "nextflow_ref" / "tools"
        assets_root = bundled_assets if bundled_assets.is_dir() else (build_assets_module.install_root / "assets") if build_assets_module else bundled_assets
        tools_root = bundled_tools if bundled_tools.is_dir() else (build_assets_module.install_root / "tools") if build_assets_module else bundled_tools
        result.add("build-workflow", build_workflow.is_file(), str(build_workflow))
        result.add("build-runtime", build_sif.is_file(), str(build_sif))
        if build_sif.is_file():
            result.runtime_files["build-runtime"] = build_sif
        result.add("build-assets", assets_root.is_dir(), str(assets_root))
        result.add("build-tool-sources", tools_root.is_dir(), str(tools_root))
        requested_build_tools = {
            value.strip().lower() for value in tools.split(",") if value.strip()
        }
        if "methylbert" in requested_build_tools and platform_name == "wgbs":
            build = effective_genome_build or "hg38"
            pretrain = assets_root / "methylbert" / f"pretrain_{build}"
            result.add(
                "build-asset:MethylBERT:pretrain",
                pretrain.is_dir(),
                str(pretrain) if pretrain.is_dir() else (
                    f"build-assets/methylbert/pretrain_{build} is missing; "
                    "another genome build will not be substituted"
                ),
            )
            methylbert_runtime = find_runtime_file("MethylBERT-gpu", runtime_root)
            result.add(
                "build-runtime:MethylBERT-gpu",
                methylbert_runtime is not None,
                str(methylbert_runtime) if methylbert_runtime else "install runtime module MethylBERT-gpu",
            )
        if "uxm" in requested_build_tools and platform_name == "wgbs":
            uxm_runtime = find_runtime_file("wgbs-common", runtime_root)
            result.add(
                "build-runtime:UXM",
                uxm_runtime is not None,
                str(uxm_runtime) if uxm_runtime else "install runtime module wgbs-common",
            )
        if "celfeer" in requested_build_tools and platform_name == "wgbs":
            build = effective_genome_build or "hg38"
            cpg_name = f"{build}_cpg_ref.txt"
            bins_name = "hg19_read_bins.txt" if build == "hg19" else "read_bins.txt"
            cpg = assets_root / "celfeer" / "references" / cpg_name
            bins = assets_root / "celfeer" / "CelFEER-main" / "data" / bins_name
            result.add("build-asset:CelFEER:cpg-reference", cpg.is_file(), str(cpg))
            result.add("build-asset:CelFEER:read-bins", bins.is_file(), str(bins))
    if runtime == "container" and result.container_executable:
        smoke_runtime = next(
            (path for path in result.runtime_files.values() if path.is_file() and path.suffix.lower() == ".sif"),
            None,
        )
        if smoke_runtime is not None:
            ok, detail = check_container_sif_smoke(
                executable=result.container_executable,
                engine=result.container_engine or "container",
                runtime_file=smoke_runtime,
            )
            result.container_sif_smoke = {
                "status": "PASS" if ok else "FAIL",
                "path": str(smoke_runtime),
                "detail": detail,
            }
            result.add("container-sif-smoke", ok, detail)
        else:
            result.container_sif_smoke = {
                "status": "NOT_PERFORMED",
                "detail": "no installed local SIF was available for the bounded container-engine smoke",
            }
            result.add(
                "container-sif-smoke",
                False,
                result.container_sif_smoke["detail"],
                required=False,
            )
    return result


def _array_input_sample_counts(input_path: Path) -> tuple[list[tuple[str, int]], str | None]:
    resolved = input_path.expanduser().resolve()
    files = [resolved] if resolved.is_file() else sorted(resolved.glob("*.csv")) if resolved.is_dir() else []
    if not files:
        return [], f"no CSV input matrices found under {resolved}"
    counts: list[tuple[str, int]] = []
    for path in files:
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                header = next(csv.reader(handle))
        except (OSError, StopIteration, UnicodeError, csv.Error) as exc:
            return [], f"cannot read CSV header from {path}: {exc}"
        if len(header) < 2:
            return [], f"input matrix has no bulk sample columns: {path}"
        counts.append((path.name, len(header) - 1))
    return counts, None


def _inspect_medecom_cohorts(input_path: Path, k: int) -> tuple[list[dict[str, int | str]], str | None]:
    """Stream-validate every array CSV using the same strict rules as MeDeCom."""
    resolved = input_path.expanduser().resolve()
    files = [resolved] if resolved.is_file() else sorted(resolved.glob("*.csv")) if resolved.is_dir() else []
    if not files:
        return [], f"no CSV input cohorts found under {resolved}"
    inspections: list[dict[str, int | str]] = []
    minimum_variable = max(100, 10 * k)
    for path in files:
        seen_cpgs: set[str] = set()
        variable_count = 0
        row_count = 0
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.reader(handle)
                header = next(reader)
                samples = [value.strip().strip('"') for value in header[1:]]
                if not samples or any(not value for value in samples) or len(samples) != len(set(samples)):
                    return [], f"MeDeCom sample IDs must be non-empty and unique: {path}"
                if len(samples) <= k:
                    return [], f"MeDeCom requires N > K; observed N={len(samples)}, K={k}: {path}"
                for line_number, row in enumerate(reader, start=2):
                    if len(row) != len(header):
                        return [], f"ragged CSV row {line_number}: {path}"
                    cpg = row[0].strip().strip('"')
                    if not cpg or cpg in seen_cpgs:
                        return [], f"MeDeCom CpG IDs must be non-empty and unique; row {line_number}: {path}"
                    seen_cpgs.add(cpg)
                    try:
                        values = [float(value) for value in row[1:]]
                    except ValueError:
                        return [], f"non-numeric MeDeCom beta value at row {line_number}: {path}"
                    if any(not math.isfinite(value) for value in values):
                        return [], f"NA/Inf MeDeCom beta value at row {line_number}: {path}"
                    if any(value < 0 or value > 1 for value in values):
                        return [], f"MeDeCom beta value outside [0,1] at row {line_number}: {path}"
                    if max(values) - min(values) > 1e-12:
                        variable_count += 1
                    row_count += 1
        except (OSError, StopIteration, UnicodeError, csv.Error) as exc:
            return [], f"cannot inspect MeDeCom cohort {path}: {exc}"
        if variable_count < minimum_variable:
            return [], (
                f"MeDeCom requires at least {minimum_variable} non-constant CpGs; "
                f"observed {variable_count}: {path}"
            )
        inspections.append({
            "path": path.name,
            "sample_count": len(samples),
            "cpg_count": row_count,
            "variable_cpg_count": variable_count,
        })
    return inspections, None


def _reference_column_labels(path: Path) -> list[str]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            first = handle.readline()
    except (OSError, UnicodeError) as exc:
        raise DeMethFlowError(f"cannot read reference header from {path}: {exc}") from exc
    delimiter = "\t" if first.count("\t") > first.count(",") else ","
    header = next(csv.reader([first], delimiter=delimiter), [])
    labels = [value.strip().strip('"') for value in header[1:]]
    if not labels or any(not value for value in labels) or len(labels) != len(set(labels)):
        raise DeMethFlowError(f"reference has empty or duplicate cell-type columns: {path}")
    return labels


def _map_reference_labels(labels: list[str], bundle: ReferenceBundle) -> list[str]:
    aliases: dict[str, str] = {}
    for cell in bundle.cell_types:
        for value in (cell["cell_type_id"], cell["display_name"], *cell.get("synonyms", [])):
            key = " ".join(str(value).strip().lower().replace("_", " ").split())
            if key in aliases and aliases[key] != cell["cell_type_id"]:
                raise DeMethFlowError(f"ambiguous cell-type alias {value!r} in {bundle.selector}")
            aliases[key] = cell["cell_type_id"]
    mapped: list[str] = []
    for label in labels:
        key = " ".join(label.strip().lower().replace("_", " ").split())
        if key not in aliases:
            raise DeMethFlowError(f"reference label {label!r} is absent from {bundle.selector}")
        mapped.append(aliases[key])
    if len(mapped) != len(set(mapped)):
        raise DeMethFlowError(f"reference labels do not map one-to-one in {bundle.selector}")
    return mapped


def _reference_labels_cover_bundle(labels: list[str], bundle: ReferenceBundle) -> bool:
    """Return whether a reference labels every manifest cell exactly once.

    Reference matrix column order is an implementation detail of the upstream
    method.  Canonical output order is enforced later by ``canonicalize_outputs``
    using cell-type IDs, so doctor must require one-to-one coverage rather than
    incorrectly requiring the artifact to use manifest order.
    """
    mapped = _map_reference_labels(labels, bundle)
    expected = [cell["cell_type_id"] for cell in bundle.cell_types]
    return len(mapped) == len(expected) and set(mapped) == set(expected)


def result_as_dict(result: DoctorResult) -> dict:
    return {
        "ok": result.ok,
        "reference": result.bundle.selector if result.bundle else None,
        "selected_tools": result.selected_tools,
        "skipped_tools": result.skipped_tools,
        "container_engine": result.container_engine,
        "container_executable": result.container_executable,
        "container_engine_version": result.container_engine_version,
        "container_engine_source": result.container_engine_source,
        "container_sif_smoke": result.container_sif_smoke,
        "accelerator": result.accelerator,
        "data_exports": {
            module: {name: str(path) for name, path in exports.items()}
            for module, exports in result.data_exports.items()
        },
        "external_runtime_paths": {tool: str(path) for tool, path in result.external_runtime_paths.items()},
        "external_runtime_metadata": result.external_runtime_metadata,
        "execution_parameters": result.execution_parameters,
        "projection_preflight": result.projection_preflight,
        "projection_report": result.projection_report,
        "release_disclosures": result.release_disclosures,
        "checks": [check.__dict__ for check in result.checks],
    }
