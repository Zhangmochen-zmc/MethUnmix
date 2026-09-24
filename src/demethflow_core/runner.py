from __future__ import annotations

import csv
import json
import math
import os
import signal
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import NEXTFLOW_VERSION, PROJECT_ROOT, version
from .errors import DeMethFlowError, ResolutionError
from .celfeer import labels_from_manifest
from .epic_projection import CONTRACT as EPIC_FROM_450K_CONTRACT, project_inputs
from .wgbs_projection import project_wgbs_inputs, target_platform_for_contract
from .manifest import ReferenceBundle, is_native_wgbs_contract, is_wgbs_derived_array_contract
from .runtime import (
    DoctorResult,
    doctor,
    find_data_module,
    find_runtime_file,
    release_disclosures_for_tools,
    release_warning_for_tools,
    result_as_dict,
)
from .util import (
    atomic_write_json,
    require_linux_x86_64_workflow,
    require_local_executor,
    sha256_file,
    stable_digest,
)
from .wgbs_bam import (
    PREPROCESS_SCHEMA,
    PREPROCESS_VERSION,
    discover_bam_samples,
    is_bam_input,
    load_wgbstools_assets,
    preprocess_signature,
    validate_sort_memory,
    validate_materialized_preprocess,
    write_preprocess_samples,
)


ARTIFACT_PARAMS = {
    "ARIC": {"reference": "ref_aric"},
    "MethylCIBERSORT": {"reference": "ref_cibersort"},
    "EDec": {
        "reference_data": "ref_edec_data",
        "metadata": "ref_edec_meta",
        "markers": "ref_edec_mark",
        "tref": "ref_edec_tref",
    },
    "EMeth": {"reference": "ref_emeth", "source": "src_emeth"},
    "EpiDISH": {"reference": "ref_epidish"},
    "EpiSCORE": {"reference": "ref_episcore", "gene_reference": "ref_episcore_gene"},
    "Houseman": {"cell_reference": "ref_houseman_cell", "beta_reference": "ref_houseman_beta"},
    "MEnet": {"model": "ref_menet", "native_model": "ref_menet_model"},
    "MethAtlas": {"reference": "ref_methatlas"},
    "MetDecode": {"atlas": "ref_metdecode_atlas"},
    "CelFiE": {"atlas": "ref_celfie_atlas"},
    "CelFEER": {
        "markers": "ref_celfeer_markers",
        "cell_types": "ref_celfeer_cell_types",
    },
    "UXM": {"atlas": "ref_uxm_atlas"},
    "MethylBERT": {
        "reference_root": "mb_reference_root",
        "markers": "mb_ref_markers",
        "model": "mb_model_dir",
        "training_data": "mb_train_data",
        "runtime_metadata": "mb_runtime_metadata",
    },
    "RefFreeEWAS": {"reference": "ref_reffree"},
    "PRmeth": {"reference": "ref_prmeth"},
    "Tsisal": {"reference": "ref_tsisal"},
}

# Owner-approved E3A CelFEER workflow bound.  This is deliberately scoped to
# native-WGBS CelFEER runs and is not a generic timeout for unrelated tools.
CELFEER_OVERALL_TIMEOUT_SECONDS = 5400


@dataclass
class DeconRequest:
    input_path: Path
    outdir: Path
    platform: str
    scenario: str | None = None
    reference: str | None = None
    tools: str = "all"
    runtime: str = "container"
    container_engine: str = "auto"
    executor: str = "local"
    accelerator: str = "cpu"
    gpu_count: int = 1
    reference_root: Path | None = None
    runtime_root: Path | None = None
    external_runtime_root: Path | None = None
    allow_cross_platform: bool = False
    resume: bool = False
    dry_run: bool = False
    verify_reference_checksums: bool = False
    random_seed: int = 20260826
    medecom_k: int | None = None
    medecom_lambda: str = "auto"
    medecom_nfolds: int | None = None
    methylcibersort_permutations: int = 1000
    genome_build: str | None = None
    requested_genome_build: str | None = None
    analysis_contract: str | None = None
    validation_candidate: bool = False
    preprocess_threads: int = 4
    preprocess_sort_memory: str = "768M"
    bam_contig_policy: str = "strict"


def _write_text_if_changed(path: Path, content: str) -> bool:
    """Write a generated input only when its bytes actually change.

    Nextflow's default cache key includes file metadata.  Replacing an
    identical generated file therefore invalidates an otherwise legitimate
    ``-resume`` task.  Returning False makes this behavior directly testable.
    """
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def prepare_decon(
    request: DeconRequest, *, input_path: Path | None = None,
) -> tuple[DoctorResult, list[str], dict[str, str]]:
    """Prepare the unchanged downstream flow against direct or materialized input."""
    require_local_executor(request.executor)
    effective_input = input_path or request.input_path
    result = doctor(
        mode="decon",
        reference=request.reference,
        scenario=request.scenario,
        platform_name=request.platform,
        genome_build=request.genome_build,
        tools=request.tools,
        input_path=effective_input,
        reference_root=request.reference_root,
        runtime_root=request.runtime_root,
        external_runtime_root=request.external_runtime_root,
        runtime=request.runtime,
        container_engine=request.container_engine,
        allow_cross_platform=request.allow_cross_platform,
        accelerator=request.accelerator,
        verify_reference_checksums=request.verify_reference_checksums,
        medecom_k=request.medecom_k,
        medecom_lambda=request.medecom_lambda,
        medecom_nfolds=request.medecom_nfolds,
        methylcibersort_permutations=request.methylcibersort_permutations,
        analysis_contract=request.analysis_contract,
        allow_validation_candidate=request.validation_candidate,
    )
    if not result.ok or not result.bundle or not result.nextflow_command:
        raise DeMethFlowError(_doctor_failure(result))
    bundle = result.bundle
    outdir = request.outdir.expanduser().resolve()
    state_dir = outdir / ".demethflow"
    state_dir.mkdir(parents=True, exist_ok=True)
    if "MEnet" in result.selected_tools:
        menet_cell_types = state_dir / "menet_cell_types.json"
        _write_text_if_changed(
            menet_cell_types,
            json.dumps(list(result.bundle.cell_types), indent=2, ensure_ascii=False) + "\n",
        )
    projection_report = None
    if bundle.contract == EPIC_FROM_450K_CONTRACT:
        projected_inputs, projection_report = project_inputs(
            bundle,
            effective_input,
            result.selected_tools,
            state_dir / "platform_projection",
            resume=request.resume,
        )
        result.projection_report = projection_report
        input_value = _input_glob(projected_inputs)
    elif is_wgbs_derived_array_contract(bundle.contract):
        projected_inputs, projection_report = project_wgbs_inputs(
            bundle,
            effective_input,
            result.selected_tools,
            state_dir / "wgbs_array_projection",
            resume=request.resume,
        )
        result.projection_report = projection_report
        input_value = _input_glob(projected_inputs)
    else:
        input_value = _input_glob(request.input_path)
    workflow, base_config = _workflow_for(bundle, request.platform)
    generated_config = state_dir / "runtime.config"
    generated_config.write_text(
        _runtime_config(result, request.runtime, request.executor, request.accelerator, request.gpu_count), encoding="utf-8"
    )
    command = list(result.nextflow_command)
    command.extend(["run", str(workflow), "-c", str(base_config), "-c", str(generated_config)])
    command.extend(["-profile", f"{request.executor},{_profile_engine(result, request.runtime)}"])
    if is_native_wgbs_contract(bundle.contract):
        bed_dir, pat_dir = _native_input_dirs(effective_input)
        command.extend(["--input_bed_dir", str(bed_dir), "--input_pat_dir", str(pat_dir)])
    else:
        command.extend(["--input_dir", input_value])
    command.extend(["--outdir", str(outdir), "--tools", ",".join(result.selected_tools)])
    command.extend(["--gpu_enabled", "true" if request.accelerator == "gpu" else "false"])
    # Keep the logical MEnet device explicit in the Nextflow command so a CPU
    # and GPU invocation cannot share a task hash or work directory.
    command.extend(["--menet_device", "cuda" if request.accelerator == "gpu" else "cpu"])
    # Keep MethylBERT's compact tissue device explicit as well.  This is
    # intentionally separate from MEnet so the two adapters can evolve
    # independently while sharing the user-facing accelerator choice.
    command.extend(["--methylbert_device", "cuda" if request.accelerator == "gpu" else "cpu"])
    command.extend(["--gpu_count", str(request.gpu_count)])
    command.extend(["--random_seed", str(request.random_seed)])
    if is_native_wgbs_contract(bundle.contract):
        command.extend(["--genome_build", str(bundle.payload["genome_build"])])
    if "MEnet" in result.selected_tools:
        menet = result.execution_parameters.get("MEnet", {})
        command.extend(["--menet_cell_types", str(menet_cell_types)])
        command.extend([
            "--menet_min_overlap_regions",
            str(int(menet.get("minimum_overlap_regions", 100))),
        ])
        command.extend([
            "--menet_min_overlap_fraction",
            str(float(menet.get("minimum_overlap_fraction", 0.05))),
        ])
    if "MethylCIBERSORT" in result.selected_tools:
        methylcibersort = result.execution_parameters.get("MethylCIBERSORT", {})
        permutations = methylcibersort.get(
            "permutations", request.methylcibersort_permutations
        )
        command.extend(["--methylcibersort_permutations", str(permutations)])
        if request.platform == "epic" or (
            is_wgbs_derived_array_contract(bundle.contract)
            and target_platform_for_contract(bundle.contract) == "epic"
        ):
            command.extend([
                "--methylcibersort_sample_workers",
                str(int(methylcibersort.get("sample_workers", 2))),
            ])
    if "MeDeCom" in result.selected_tools:
        medecom = result.execution_parameters.get("MeDeCom", {})
        command.extend(["--medecom_k", str(medecom["k"])])
        command.extend(["--medecom_lambda", str(medecom["lambda"])])
        command.extend(["--medecom_nfolds", str(medecom.get("nfolds") or 0)])
        command.extend([
            "--medecom_output_contract",
            result.bundle.tools["MeDeCom"].output_contract,
        ])
    if "ARIC" in result.selected_tools:
        policy = result.bundle.tools["ARIC"].validation_policy or {}
        command.extend(["--aric_min_overlap", str(int(policy.get("minimum_overlap_cpg", 500)))])
    for tool in result.selected_tools:
        capability = bundle.tools[tool]
        for artifact_name, parameter in ARTIFACT_PARAMS.get(tool, {}).items():
            if artifact_name in capability.artifacts:
                command.extend([f"--{parameter}", str(bundle.artifact(tool, artifact_name))])
    if "EpiDISH" in result.selected_tools:
        capability = bundle.tools["EpiDISH"]
        command.extend(["--epidish_reference_mode", capability.reference_mode])
        if capability.builtin_reference:
            command.extend(["--epidish_builtin_reference", capability.builtin_reference])
        if capability.postprocess_profile:
            command.extend(["--epidish_postprocess_profile", capability.postprocess_profile])
    if "CelFEER" in result.selected_tools:
        capability = bundle.tools["CelFEER"]
        if "cell_types" not in capability.artifacts:
            compatibility_sidecar = state_dir / "celfeer_cell_types.tsv"
            labels = labels_from_manifest(list(bundle.cell_types))
            compatibility_sidecar.write_text("cell_type\n" + "\n".join(labels) + "\n", encoding="utf-8")
            command.extend(["--ref_celfeer_cell_types", str(compatibility_sidecar)])
        cpg = bundle.artifact("CelFEER", "cpg_reference") if "cpg_reference" in capability.artifacts else None
        bins = bundle.artifact("CelFEER", "read_bins") if "read_bins" in capability.artifacts else None
        if cpg is None or bins is None:
            build = str(bundle.payload["genome_build"])
            module_name = f"CelFEER-{build}-data"
            exports = result.data_exports.get(module_name, {})
            cpg = cpg or exports.get(f"celfeer_{build}_cpg_reference")
            bins = bins or exports.get(f"celfeer_{build}_read_bins")
        if not cpg or not bins:
            raise DeMethFlowError(
                f"CelFEER requires either self-contained {bundle.payload['genome_build']} common artifacts "
                f"or the CelFEER-{bundle.payload['genome_build']}-data module"
            )
        command.extend(["--ref_celfeer_cpg", str(cpg), "--ref_celfeer_bins", str(bins)])
    if "MethylBERT" in result.selected_tools and "reference_root" not in bundle.tools["MethylBERT"].artifacts:
        build = str(bundle.payload["genome_build"])
        module_name = f"MethylBERT-{build}-data"
        exports = result.data_exports.get(module_name, {})
        reference_root = exports.get("methylbert_reference_root")
        if not reference_root:
            raise DeMethFlowError(f"{module_name} does not export methylbert_reference_root")
        command.extend(["--mb_reference_root", str(reference_root)])
    if "MethAtlas" in result.selected_tools:
        command.extend(["--script_atlas", str(PROJECT_ROOT / "deconvolution" / "bin" / "MethAtlas.py")])
    if "PRmeth" in result.selected_tools:
        external_prmeth = result.external_runtime_paths.get("PRmeth")
        if external_prmeth is None:
            raise DeMethFlowError(
                "EXTERNAL_RUNTIME_NOT_INSTALLED: PRmeth requires the external runtime contract"
            )
        command.extend(["--src_prmeth", str(external_prmeth)])
    external_params = {
        "MetDecode": "tool_metdecode",
        "CelFiE": "tool_celfie",
        "CelFEER": "tool_celfeer",
        "UXM": "tool_uxm",
    }
    for tool, parameter in external_params.items():
        if tool in result.selected_tools:
            path = result.external_runtime_paths.get(tool)
            if path is None:
                raise DeMethFlowError(
                    f"EXTERNAL_RUNTIME_NOT_INSTALLED: {tool} requires the external runtime contract"
                )
            command.extend([f"--{parameter}", str(path)])
    command.extend(["-work-dir", str(state_dir / "work")])
    if request.resume:
        command.append("-resume")
    env = _offline_env(outdir)
    if request.runtime == "container" and result.container_executable:
        engine_dir = str(Path(result.container_executable).parent)
        env["PATH"] = engine_dir + os.pathsep + env.get("PATH", "")
    return result, command, env


def _materialize_bam_input(request: DeconRequest) -> tuple[Path, dict | None]:
    """Run the pinned BAM materialisation workflow, if and only if input is BAM.

    It is intentionally called before the ordinary doctor.  Therefore every
    BAM must pass its build, modality and binary-output checks before any tool
    sees a BED/PAT path.  Existing direct BED/PAT runs return unchanged.
    """
    if not is_bam_input(request.input_path):
        return request.input_path, None
    if request.platform != "wgbs":
        raise DeMethFlowError("BAM_UNSUPPORTED_MODALITY: BAM input is supported only with --platform wgbs")
    if request.requested_genome_build is None:
        raise DeMethFlowError("BAM_BUILD_MISMATCH: BAM input requires explicit --genome-build hg19 or hg38")
    if request.genome_build not in {"hg19", "hg38"}:
        raise DeMethFlowError("BAM_BUILD_MISMATCH: BAM input requires genome build hg19 or hg38")
    if request.runtime != "container":
        raise DeMethFlowError("BAM_PREPROCESS_FAILED: BAM input requires the pinned wgbs-common container runtime")
    if request.preprocess_threads < 1:
        raise DeMethFlowError("BAM_PREPROCESS_FAILED: --preprocess-threads must be a positive integer")
    sort_memory = validate_sort_memory(request.preprocess_sort_memory)
    if request.bam_contig_policy not in {"strict", "primary-only"}:
        raise DeMethFlowError("BAM_BUILD_MISMATCH: --bam-contig-policy must be strict or primary-only")

    samples = discover_bam_samples(request.input_path)
    module_name = f"WGBSTools-{request.genome_build}-data"
    resolved_module = find_data_module(module_name, request.runtime_root)
    if not resolved_module:
        raise DeMethFlowError(
            f"WGBSTOOLS_REFERENCE_MISSING: install offline data module {module_name}"
        )
    _, exports = resolved_module
    asset_root = exports.get(f"wgbstools_{request.genome_build}_reference")
    if not asset_root:
        raise DeMethFlowError(
            f"WGBSTOOLS_REFERENCE_MISSING: {module_name} does not export the selected build"
        )
    assets = load_wgbstools_assets(asset_root, request.genome_build)
    common = find_runtime_file("wgbs-common", request.runtime_root)
    if not common:
        raise DeMethFlowError("BAM_PREPROCESS_FAILED: install runtime module wgbs-common")
    runtime_sha256 = sha256_file(common)
    signature = preprocess_signature(
        samples, assets, request.preprocess_threads, runtime_sha256=runtime_sha256,
        contig_policy=request.bam_contig_policy, sort_memory=sort_memory,
    )
    disk_preflight = _preprocess_disk_preflight(
        request.outdir, samples, assets, contig_policy=request.bam_contig_policy,
    )
    state_dir = request.outdir.expanduser().resolve() / ".methunmix"
    primary_root = state_dir / "preprocess"
    primary_manifest = primary_root / "manifest.json"
    root = primary_root
    if primary_manifest.is_file():
        try:
            previous = json.loads(primary_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeMethFlowError(f"BAM_PREPROCESS_FAILED: invalid existing preprocess manifest: {exc}") from exc
        if request.resume and previous.get("signature") == signature and previous.get("status") == "PASS":
            report = validate_materialized_preprocess(primary_root, samples, signature=signature, sort_memory=sort_memory)
            report["cache_reused"] = True
            report["assets"] = assets.evidence()
            report["preprocess_version"] = PREPROCESS_VERSION
            return primary_root, report
        # Never overwrite an existing materialisation: changed BAM bytes,
        # dictionary bytes or frozen preprocessing arguments receive a new
        # cache root.  The primary root remains the human-readable first run.
        root = state_dir / "preprocess-runs" / signature
        existing = root / "manifest.json"
        if request.resume and existing.is_file():
            try:
                previous = json.loads(existing.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DeMethFlowError(f"BAM_PREPROCESS_FAILED: invalid cached preprocess manifest: {exc}") from exc
            if previous.get("signature") == signature and previous.get("status") == "PASS":
                report = validate_materialized_preprocess(root, samples, signature=signature, sort_memory=sort_memory)
                report["cache_reused"] = True
                report["assets"] = assets.evidence()
                report["preprocess_version"] = PREPROCESS_VERSION
                return root, report
            raise DeMethFlowError("BAM_PREPROCESS_FAILED: existing BAM preprocess cache is incomplete or has incompatible provenance")
        if existing.exists():
            raise DeMethFlowError(f"BAM_PREPROCESS_FAILED: refusing to overwrite existing preprocess cache: {root}")
    if request.dry_run:
        return root, {
            "schema": PREPROCESS_SCHEMA,
            "preprocess_version": PREPROCESS_VERSION,
            "bam_contig_policy": request.bam_contig_policy,
            "preprocess_sort_memory": sort_memory,
            "status": "DRY_RUN",
            "signature": signature,
            "output_root": str(root),
            "sample_ids": [sample.sample_id for sample in samples],
            "assets": assets.evidence(),
            "data_module": {"module_id": module_name, "export": f"wgbstools_{request.genome_build}_reference"},
            "runtime": {"module_id": "wgbs-common", "path": str(common), "sha256": runtime_sha256},
            "disk_preflight": disk_preflight,
            "cache_reused": False,
        }

    root.mkdir(parents=True, exist_ok=False)
    samples_tsv = root / "samples.tsv"
    write_preprocess_samples(samples, samples_tsv)
    initial = {
        "schema": PREPROCESS_SCHEMA,
        "preprocess_version": PREPROCESS_VERSION,
        "status": "RUNNING",
        "signature": signature,
        "genome_build": request.genome_build,
        "preprocess_threads": request.preprocess_threads,
        "preprocess_sort_memory": sort_memory,
        "bam_contig_policy": request.bam_contig_policy,
        "started_at": _now(),
        "resume_requested": request.resume,
        "input": {"path": str(request.input_path.expanduser().resolve()), "samples": [sample.__dict__ | {"path": str(sample.path)} for sample in samples]},
        "assets": assets.evidence(),
        "data_module": {"module_id": module_name, "export": f"wgbstools_{request.genome_build}_reference"},
        "runtime": {"module_id": "wgbs-common", "path": str(common), "sha256": runtime_sha256},
        "disk_preflight": disk_preflight,
        "output_root": str(root),
        "samples_tsv": str(samples_tsv),
        "cache_reused": False,
    }
    atomic_write_json(root / "manifest.json", initial)
    from .runtime import bundled_nextflow, choose_container_engine
    nextflow = bundled_nextflow()
    engine, executable = choose_container_engine(request.container_engine)
    if not nextflow or not engine or not executable:
        initial["status"] = "FAILED"
        initial["error"] = "Nextflow or container engine is unavailable"
        atomic_write_json(root / "manifest.json", initial)
        raise DeMethFlowError("BAM_PREPROCESS_FAILED: Nextflow and an Apptainer/Singularity runtime are required")
    config = root / "preprocess.runtime.config"
    config.write_text(
        _preprocess_runtime_config(engine, common, assets.root, request.genome_build), encoding="utf-8"
    )
    workflow = PROJECT_ROOT / "deconvolution" / "wgbs_preprocess.nf"
    command = [
        *nextflow, "run", str(workflow), "-c", str(config), "-profile", f"{request.executor},{engine}",
        "--bam_manifest", str(samples_tsv), "--preprocess_outdir", str(root),
        "--preprocess_threads", str(request.preprocess_threads), "--preprocess_sort_memory", sort_memory,
        "--genome_build", request.genome_build,
        "--bam_contig_policy", request.bam_contig_policy,
        "--core_root", str(PROJECT_ROOT), "-work-dir", str(root / "work"),
    ]
    if request.resume:
        command.append("-resume")
    env = _offline_env(request.outdir.expanduser().resolve())
    launch = root / "launch"
    launch.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(command, cwd=launch, env=env, check=False)
    initial["command"] = command
    initial["exit_code"] = completed.returncode
    if completed.returncode != 0:
        failure = _preprocess_failure_detail(root)
        initial["status"] = "FAILED"
        initial["error"] = failure
        initial["finished_at"] = _now()
        atomic_write_json(root / "manifest.json", initial)
        raise DeMethFlowError(f"{failure}; see {root}")
    report = validate_materialized_preprocess(root, samples, signature=signature, sort_memory=sort_memory)
    report.update({
        "genome_build": request.genome_build,
        "preprocess_version": PREPROCESS_VERSION,
        "preprocess_threads": request.preprocess_threads,
        "preprocess_sort_memory": sort_memory,
        "bam_contig_policy": request.bam_contig_policy,
        "assets": assets.evidence(),
        "command": command,
        "cache_reused": False,
        "started_at": initial["started_at"],
        "finished_at": _now(),
        "resume_requested": request.resume,
        "data_module": initial["data_module"],
        "runtime": initial["runtime"],
        "disk_preflight": disk_preflight,
    })
    atomic_write_json(root / "manifest.json", report)
    return root, report


def _preprocess_disk_preflight(outdir: Path, samples, assets, *, contig_policy: str = "strict") -> dict[str, int | bool | str]:
    """Reject insufficient local scratch before a BAM task is launched.

    The estimate intentionally scales with the actual BAM bytes and the
    selected build's exact lbeta cardinality.  It is not a fixed free-space
    threshold: every job records the formula and observed free space.
    """
    cpg_count = 0
    try:
        with assets.cpg_chrom_sizes.open(encoding="utf-8") as handle:
            for line in handle:
                fields = line.rstrip("\n").split("\t")
                if len(fields) == 2:
                    cpg_count += int(fields[1])
    except (OSError, ValueError) as exc:
        raise DeMethFlowError(f"WGBSTOOLS_REFERENCE_MISSING: invalid CpG.chrome.size: {exc}") from exc
    if cpg_count <= 0:
        raise DeMethFlowError("WGBSTOOLS_REFERENCE_MISSING: selected CpG.chrome.size has no CpG records")
    input_bytes = sum(sample.bytes for sample in samples)
    if contig_policy not in {"strict", "primary-only"}:
        raise DeMethFlowError("BAM_BUILD_MISMATCH: invalid BAM contig policy for disk preflight")
    # Strict mode sorts the staged Nextflow input without copying the source.
    # Explicit primary-only staging additionally needs a task-local source
    # sort, region-selected BAM and reheadered primary BAM before WGBSTools
    # starts. Reserve five source-BAM
    # equivalents rather than risk a full disk half way through a large run.
    amplification = 5 if contig_policy == "primary-only" else 3
    required = input_bytes * amplification + cpg_count * 4 * len(samples) + 64 * 1024 * 1024 * len(samples)
    probe = outdir.expanduser().resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    available = shutil.disk_usage(probe).free
    payload: dict[str, int | bool] = {
        "input_bytes": input_bytes,
        "bam_contig_policy": contig_policy,
        "source_bam_amplification": amplification,
        "cpg_record_count": cpg_count,
        "estimated_lbeta_bytes": cpg_count * 4 * len(samples),
        "estimated_required_bytes": required,
        "available_bytes": available,
        "passed": available >= required,
    }
    if not payload["passed"]:
        raise DeMethFlowError(
            "BAM_PREPROCESS_FAILED: insufficient disk space for BAM preprocessing; "
            f"need at least {required} bytes, have {available} bytes"
        )
    return payload


def _preprocess_runtime_config(engine: str, image: Path, asset_root: Path, genome_build: str) -> str:
    """Generate a narrow read-only bind: no host PATH or network fallback."""
    option = (
        f"--bind {_groovy(str(asset_root))}:/opt/wgbs_tools/references/{genome_build}:ro "
        f"--bind {_groovy(str(PROJECT_ROOT))}:{_groovy(str(PROJECT_ROOT))}:ro"
    )
    return "\n".join([
        "profiles {",
        "  local { process.executor = 'local' }",
        f"  {engine} {{",
        "    conda.enabled = false",
        f"    {engine}.enabled = true",
        f"    {engine}.autoMounts = true",
        "  }",
        "}",
        "process {",
        "  shell = ['/bin/bash', '-euo', 'pipefail']",
        f"  withName: 'PREPROCESS_WGBS_BAM' {{ container = '{_groovy(str(image))}'; containerOptions = '{option}' }}",
        "}",
        "",
    ])


def _preprocess_failure_detail(root: Path) -> str:
    """Surface frozen worker failure codes through the top-level CLI."""
    codes = (
        "BAM_CORRUPT", "BAM_EMPTY", "BAM_BUILD_MISMATCH", "BAM_CONTIG_STYLE_MISMATCH",
        "BAM_UNSUPPORTED_MODALITY", "BAM_SAMPLE_ID_COLLISION", "BAM_PREPROCESS_FAILED", "PAT_EMPTY",
        "PAT_INDEX_MISSING", "LBETA_SIZE_MISMATCH", "LBETA_SATURATION",
        "BED_CONTRACT_INVALID", "BED_PAT_SAMPLE_MISMATCH",
        "WGBSTOOLS_REFERENCE_MISSING", "WGBSTOOLS_REFERENCE_DIGEST_MISMATCH",
    )
    candidates = [
        path for path in root.rglob("*")
        if path.is_file() and path.name in {".command.err", ".command.out", ".command.log"}
    ]
    for path in sorted(candidates):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for code in codes:
            marker = f"{code}:"
            if marker in content:
                detail = content[content.index(marker):].splitlines()[0]
                return detail
    return "BAM_PREPROCESS_FAILED: BAM preprocessing Nextflow task failed"


def run_decon(request: DeconRequest) -> dict:
    require_local_executor(request.executor)
    if not request.dry_run:
        require_linux_x86_64_workflow()
    materialized_input, preprocess_report = _materialize_bam_input(request)
    if request.dry_run and preprocess_report and preprocess_report.get("status") == "DRY_RUN":
        outdir = request.outdir.expanduser().resolve()
        outdir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": "demethflow-run-v2",
            "mode": "decon",
            "status": "DRY_RUN",
            "input": str(request.input_path.expanduser().resolve()),
            "input_platform": request.platform,
            "genome_build": request.genome_build,
            "scenario": request.scenario,
            "reference": request.reference,
            "requested_tools": request.tools,
            "wgbs_bam_preprocess": preprocess_report,
            "note": "BAM materialisation is planned but was not executed; downstream doctor/tool checks are intentionally deferred.",
        }
        atomic_write_json(outdir / "run_manifest.json", manifest)
        return manifest
    result, command, env = prepare_decon(request, input_path=materialized_input)
    outdir = request.outdir.expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    launch_dir = outdir / ".demethflow" / "launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    manifest = _initial_run_manifest(
        request, result, command, projection_report=result.projection_report,
        preprocess_report=preprocess_report,
    )
    atomic_write_json(outdir / "run_manifest.json", manifest)
    _write_tool_status(outdir, result)
    if request.dry_run:
        manifest["status"] = "DRY_RUN"
        atomic_write_json(outdir / "run_manifest.json", manifest)
        return manifest
    # Nextflow automatically prepends <projectDir>/bin to every task PATH.  Its
    # generated task wrapper does not safely quote that export when projectDir
    # contains whitespace. Execute a lightweight copy/symlink view from a
    # deterministic, user-owned bridge root. Validation jobs may pin that root
    # below their project-scoped output; ordinary installations retain /tmp as
    # the portability default. A random bridge defeats Nextflow -resume.
    bridge = _persistent_workflow_bridge(outdir)
    bridged_command = _bridge_workflow_command(command, bridge)
    manifest["executed_command"] = bridged_command
    manifest["execution_environment"] = {
        "NXF_OFFLINE": env["NXF_OFFLINE"],
        "NXF_DISABLE_CHECK_LATEST": env["NXF_DISABLE_CHECK_LATEST"],
        "NXF_HOME": env["NXF_HOME"],
        "NXF_WORK": env["NXF_WORK"],
        "APPTAINER_CACHEDIR": env["APPTAINER_CACHEDIR"],
        "workflow_bridge": str(bridge),
        "workflow_timeout_seconds": (
            CELFEER_OVERALL_TIMEOUT_SECONDS
            if is_native_wgbs_contract(result.bundle.contract)
            and "CelFEER" in result.selected_tools
            else None
        ),
        "proxy_variables_removed": not any(
            key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
            for key in env
        ),
    }
    atomic_write_json(outdir / "run_manifest.json", manifest)
    workflow_timeout = manifest["execution_environment"]["workflow_timeout_seconds"]
    if workflow_timeout is None:
        completed = subprocess.run(bridged_command, cwd=launch_dir, env=env, check=False)
    else:
        # Start a dedicated process group so an overall timeout cannot leave a
        # Nextflow/Apptainer descendant running after the CLI has returned.
        process = subprocess.Popen(
            bridged_command,
            cwd=launch_dir,
            env=env,
            start_new_session=True,
        )
        try:
            process.wait(timeout=workflow_timeout)
        except subprocess.TimeoutExpired:
            manifest["status"] = "TIMEOUT"
            manifest["execution_status"] = "TIMEOUT"
            manifest["timeout_seconds"] = workflow_timeout
            manifest["timeout_cleanup"] = "SIGTERM_PROCESS_GROUP_THEN_WAIT"
            atomic_write_json(outdir / "run_manifest.json", manifest)
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise DeMethFlowError(
                f"Nextflow exceeded the CelFEER overall timeout of {workflow_timeout}s; "
                f"see {outdir}"
            )
        completed = subprocess.CompletedProcess(bridged_command, process.returncode)
    manifest["finished_at"] = _now()
    manifest["exit_code"] = completed.returncode
    if completed.returncode != 0:
        manifest["status"] = "FAILED"
        manifest["execution_status"] = "FAILED"
        atomic_write_json(outdir / "run_manifest.json", manifest)
        raise DeMethFlowError(f"Nextflow failed with exit code {completed.returncode}; see {outdir}")
    reference_free = separate_reference_free_outputs(outdir, result)
    cell_tools = [
        tool for tool in result.selected_tools
        if result.bundle.tools[tool].output_contract == "cell_proportions_v1"
    ]
    canonical = canonicalize_outputs(outdir, result.bundle) if cell_tools else []
    manifest["canonical_outputs"] = [str(path) for path in canonical]
    manifest["reference_free_outputs"] = [str(path) for path in reference_free]
    manifest["status"] = "SUCCEEDED"
    manifest["execution_status"] = "SUCCEEDED"
    atomic_write_json(outdir / "run_manifest.json", manifest)
    return manifest


def canonicalize_outputs(outdir: Path, bundle: ReferenceBundle) -> list[Path]:
    results_dir = outdir / "results"
    if not results_dir.is_dir():
        raise DeMethFlowError(f"Nextflow succeeded but produced no results directory: {results_dir}")
    output_dir = outdir / "canonical"
    output_dir.mkdir(parents=True, exist_ok=True)
    cells = list(bundle.cell_types)
    display_to_id: dict[str, str] = {}
    for cell in cells:
        names = [cell["display_name"], cell["cell_type_id"], *cell.get("synonyms", [])]
        for name in names:
            key = _normalize_label(name)
            if key in display_to_id and display_to_id[key] != cell["cell_type_id"]:
                raise DeMethFlowError(f"Ambiguous cell label {name!r} in {bundle.selector}")
            display_to_id[key] = cell["cell_type_id"]
    created: list[Path] = []
    for source in sorted(results_dir.glob("*.csv")):
        with source.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fieldnames = reader.fieldnames or []
        if not rows or len(fieldnames) < 2:
            raise DeMethFlowError(f"Empty or malformed result: {source}")
        sample_field = fieldnames[0]
        mapping: dict[str, str] = {}
        unexpected: list[str] = []
        for field in fieldnames[1:]:
            cell_id = display_to_id.get(_normalize_label(field))
            if cell_id:
                mapping[field] = cell_id
            else:
                unexpected.append(field)
        if unexpected:
            raise DeMethFlowError(
                f"{source.name} contains cell labels absent from {bundle.selector}: {', '.join(unexpected)}"
            )
        expected_ids = [cell["cell_type_id"] for cell in cells]
        actual_ids = set(mapping.values())
        missing = [cell_id for cell_id in expected_ids if cell_id not in actual_ids]
        if missing:
            raise DeMethFlowError(
                f"{source.name} is missing fine-grained cell types from {bundle.selector}: {', '.join(missing)}"
            )
        destination = output_dir / source.name
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["SampleID", *expected_ids])
            writer.writeheader()
            for row in rows:
                output = {"SampleID": row[sample_field]}
                for original, cell_id in mapping.items():
                    output[cell_id] = row[original]
                if bundle.contract == EPIC_FROM_450K_CONTRACT or is_wgbs_derived_array_contract(bundle.contract):
                    try:
                        values = [float(output[cell_id]) for cell_id in expected_ids]
                    except (KeyError, TypeError, ValueError) as exc:
                        raise DeMethFlowError(
                            f"{source.name} contains a non-numeric canonical proportion"
                        ) from exc
                    if any(not math.isfinite(value) or value < -1e-10 for value in values):
                        raise DeMethFlowError(
                            f"{source.name} contains an invalid canonical proportion"
                        )
                    if not math.isclose(sum(values), 1.0, rel_tol=1e-4, abs_tol=1e-6):
                        raise DeMethFlowError(
                            f"{source.name} canonical proportions do not sum to one"
                        )
                writer.writerow(output)
        created.append(destination)
    if not created:
        raise DeMethFlowError(f"Nextflow produced no result CSV files under {results_dir}")
    with (outdir / "cell_types.tsv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["cell_type_id", "display_name"])
        for cell in cells:
            writer.writerow([cell["cell_type_id"], cell["display_name"]])
    return created


def separate_reference_free_outputs(outdir: Path, result: DoctorResult) -> list[Path]:
    if not result.bundle:
        return []
    legacy_tools = [
        tool for tool in result.selected_tools
        if result.bundle.tools[tool].output_contract == "reference_free_components_v1"
    ]
    v2_tools = [
        tool for tool in result.selected_tools
        if result.bundle.tools[tool].output_contract == "reference_free_components_v2"
    ]
    if not legacy_tools and not v2_tools:
        return []
    destination_dir = outdir / "reference_free"
    destination_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for tool in legacy_tools:
        source = outdir / "results" / f"{tool}_results.csv"
        if not source.is_file():
            raise DeMethFlowError(f"{tool} produced no reference-free result: {source}")
        with source.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fields = reader.fieldnames or []
        expected = ["SampleID", *[f"Component_{index}" for index in range(1, 7)]]
        if fields != expected or not rows:
            raise DeMethFlowError(f"{tool} result must have columns {', '.join(expected)}")
        for row in rows:
            try:
                values = [float(row[field]) for field in expected[1:]]
            except (TypeError, ValueError) as exc:
                raise DeMethFlowError(f"{tool} result contains a non-numeric component") from exc
            if any(not math.isfinite(value) or value < 0 for value in values):
                raise DeMethFlowError(f"{tool} result contains invalid component weights")
            if not math.isclose(sum(values), 1.0, rel_tol=1e-4, abs_tol=1e-6):
                raise DeMethFlowError(f"{tool} component weights do not sum to one")
        destination = destination_dir / source.name
        source.replace(destination)
        created.append(destination)
    for tool in v2_tools:
        tool_root = destination_dir / tool
        if not tool_root.is_dir():
            raise DeMethFlowError(f"{tool} produced no v2 reference-free output directory: {tool_root}")
        cohort_dirs = sorted(path for path in tool_root.iterdir() if path.is_dir())
        if not cohort_dirs:
            raise DeMethFlowError(f"{tool} produced no cohort outputs under {tool_root}")
        for cohort_dir in cohort_dirs:
            created.append(_validate_reference_free_v2(tool, cohort_dir))
    return created


def _validate_reference_free_v2(tool: str, cohort_dir: Path) -> Path:
    required = {
        "results": cohort_dir / "MeDeCom_results.csv",
        "latent": cohort_dir / "latent_components.csv",
        "parameters": cohort_dir / "selected_parameters.json",
        "cross_validation": cohort_dir / "cross_validation.csv",
        "reconstruction": cohort_dir / "reconstruction_qc.json",
        "diagnostic": cohort_dir / "diagnostic.pdf",
        "model": cohort_dir / "raw_model.rds",
        "samples": cohort_dir / "input_samples.tsv",
        "metadata": cohort_dir / "MeDeCom_metadata.json",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} is missing v2 assets: {', '.join(missing)}")
    try:
        parameters = json.loads(required["parameters"].read_text(encoding="utf-8"))
        k = int(parameters["selected_k"])
        selected_lambda = float(parameters["selected_lambda"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} has invalid selected_parameters.json") from exc
    if (
        k < 1 or not math.isfinite(selected_lambda) or selected_lambda < 0
        or parameters.get("fit_status") != "PASS"
        or parameters.get("cve_finite") is not True
        or parameters.get("authoritative_accessors_succeeded") is not True
    ):
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} has invalid selected K/lambda")
    try:
        metadata = json.loads(required["metadata"].read_text(encoding="utf-8"))
        reconstruction = json.loads(required["reconstruction"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} has unreadable v2 metadata/QC") from exc
    if (
        metadata.get("schema") != "reference_free_components_v2"
        or metadata.get("anonymous_components") is not True
    ):
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} must declare anonymous reference-free v2 components")
    if reconstruction.get("status") != "PASS" or reconstruction.get("improves_over_null") is not True:
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} reconstruction QC did not pass")
    with required["cross_validation"].open(newline="", encoding="utf-8-sig") as handle:
        cv_rows = list(csv.DictReader(handle))
    selected_rows = [row for row in cv_rows if str(row.get("selected", "")).strip().lower() == "true"]
    try:
        cv_matches = (
            len(selected_rows) == 1
            and int(selected_rows[0]["K"]) == k
            and math.isclose(float(selected_rows[0]["lambda"]), selected_lambda, rel_tol=1e-12, abs_tol=1e-15)
            and all(math.isfinite(float(row["mean_cve"])) for row in cv_rows)
        )
    except (KeyError, TypeError, ValueError):
        cv_matches = False
    if not cv_rows or not cv_matches:
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} cross-validation table disagrees with selected parameters")
    with required["samples"].open(newline="", encoding="utf-8-sig") as handle:
        sample_reader = csv.DictReader(handle, delimiter="\t")
        sample_rows = list(sample_reader)
        sample_fields = sample_reader.fieldnames or []
    if "SampleID" not in sample_fields or not sample_rows:
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} has invalid input_samples.tsv")
    expected_ids = [row["SampleID"] for row in sample_rows]
    if any(not value for value in expected_ids) or len(expected_ids) != len(set(expected_ids)):
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} input sample IDs are empty or duplicated")
    try:
        declared_samples = int(parameters["sample_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} parameters omit a valid sample_count") from exc
    if declared_samples != len(expected_ids):
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} selected parameters disagree with the sample axis")
    with required["results"].open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fields = reader.fieldnames or []
    expected_fields = ["SampleID", *[f"Component_{index}" for index in range(1, k + 1)]]
    if fields != expected_fields or [row.get("SampleID") for row in rows] != expected_ids:
        raise DeMethFlowError(
            f"{tool}/{cohort_dir.name} result rows/columns do not match input samples and selected K={k}"
        )
    for row in rows:
        try:
            values = [float(row[field]) for field in expected_fields[1:]]
        except (TypeError, ValueError) as exc:
            raise DeMethFlowError(f"{tool}/{cohort_dir.name} contains a non-numeric component") from exc
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise DeMethFlowError(f"{tool}/{cohort_dir.name} contains invalid component weights")
        if not math.isclose(sum(values), 1.0, rel_tol=1e-4, abs_tol=1e-6):
            raise DeMethFlowError(f"{tool}/{cohort_dir.name} component weights do not sum to one")
    with required["latent"].open(newline="", encoding="utf-8-sig") as handle:
        latent_reader = csv.reader(handle)
        latent_header = next(latent_reader, [])
        latent_rows = list(latent_reader)
    if latent_header != ["CpG", *[f"Component_{index}" for index in range(1, k + 1)]] or not latent_rows:
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} latent_components.csv is not CpG x K")
    latent_ids: set[str] = set()
    for row in latent_rows:
        if len(row) != k + 1 or not row[0] or row[0] in latent_ids:
            raise DeMethFlowError(f"{tool}/{cohort_dir.name} latent CpG axis is empty, duplicated, or ragged")
        latent_ids.add(row[0])
        try:
            values = [float(value) for value in row[1:]]
        except ValueError as exc:
            raise DeMethFlowError(f"{tool}/{cohort_dir.name} latent components contain non-numeric values") from exc
        if any(not math.isfinite(value) for value in values):
            raise DeMethFlowError(f"{tool}/{cohort_dir.name} latent components contain non-finite values")
    if required["diagnostic"].stat().st_size == 0 or required["model"].stat().st_size == 0:
        raise DeMethFlowError(f"{tool}/{cohort_dir.name} diagnostic/model asset is empty")
    return required["results"]


def _workflow_for(bundle: ReferenceBundle, input_platform: str) -> tuple[Path, Path]:
    root = PROJECT_ROOT / "deconvolution"
    if bundle.contract == "array_450k_cpg":
        if input_platform != "450k":
            raise ResolutionError("array_450k_cpg currently supports only 450k input")
        return root / "450k_main.nf", root / "450k.config"
    if bundle.contract == "array_epic_cpg":
        if input_platform == "epic":
            return root / "epic_main.nf", root / "epic.config"
        if input_platform == "wgbs":
            return root / "wgbs_epic_main.nf", root / "wgbs_epic.config"
        raise ResolutionError("array_epic_cpg supports EPIC or an explicitly declared WGBS-to-EPIC route")
    if bundle.contract == EPIC_FROM_450K_CONTRACT:
        if input_platform != "epic":
            raise ResolutionError(
                "array_epic_from_450k_common_cpg_v1 requires EPIC v1 input"
            )
        # Input remains an EPIC-v1 measurement even after common-CpG
        # projection. The EPIC adapters are required in particular for
        # EpiSCORE's EPIC CpG-to-gene mapping; all reference paths are still
        # supplied by the derived 450K-source bundle.
        return root / "epic_main.nf", root / "epic.config"
    if is_wgbs_derived_array_contract(bundle.contract):
        return (
            (root / "epic_main.nf", root / "epic.config")
            if target_platform_for_contract(bundle.contract) == "epic"
            else (root / "450k_main.nf", root / "450k.config")
        )
    if is_native_wgbs_contract(bundle.contract):
        return root / "wgbs_main.nf", root / "wgbs.config"
    raise ResolutionError(f"Unsupported analysis contract: {bundle.contract}")


def _runtime_config(
    result: DoctorResult, runtime: str, executor: str, accelerator: str, gpu_count: int = 1
) -> str:
    require_local_executor(executor)
    lines = ["params.executor_name = '%s'" % _groovy(executor), "profiles {"]
    lines.append("  local { process.executor = 'local' }")
    if runtime == "container":
        engine = result.container_engine
        lines.append(f"  {engine} {{")
        lines.append("    conda.enabled = false")
        lines.append(f"    {engine}.enabled = true")
        lines.append(f"    {engine}.autoMounts = true")
        if accelerator == "gpu":
            lines.append(f"    {engine}.runOptions = '--nv'")
        common = result.runtime_files.get("array-common") or result.runtime_files.get("wgbs-common")
        if common:
            lines.append(f"    process.container = '{_groovy(str(common))}'")
        lines.append("  }")
    else:
        lines.append("  conda { conda.enabled = true; conda.useMamba = false }")
    lines.append("}")
    lines.append("process {")
    process_names = {
        "ARIC": "RUN_ARIC",
        "MethylCIBERSORT": "RUN_MethylCIBERSORT",
        "EDec": "PREP_EDec|RUN_EDec",
        "EMeth": "RUN_EMeth",
        "EpiDISH": "RUN_EpiDISH",
        "EpiSCORE": "PREP_EpiSCORE|RUN_EpiSCORE",
        "Houseman": "RUN_Houseman",
        "MeDeCom": "RUN_MeDeCom|POST_PROCESS_MEDECOM_V1",
        "MEnet": "PRE_MENET|RUN_MEnet|RUN_MENET",
        "MethAtlas": "RUN_MethAtlas",
        "RefFreeEWAS": "RUN_RefFreeEWAS",
        "PRmeth": "RUN_PRmeth",
        "Tsisal": "RUN_Tsisal",
        "MetDecode": "RUN_METDECODE",
        "CelFiE": "RUN_CELFIE",
        "CelFEER": "RUN_CELFEER",
        "UXM": "RUN_UXM",
        "MethylBERT": "PRE_METHYLBERT_PREPARE|PRE_METHYLBERT_FIX|RUN_METHYLBERT|RUN_METHYLBERT_COMPACT|POST_PROCESS_METHYLBERT",
    }
    common = result.runtime_files.get("array-common")
    if common:
        lines.append(
            "  withName: 'POST_PROCESS_.*|MERGE_TOOL_RESULTS|MERGE_BENCHMARKS' "
            f"{{ container = '{_groovy(str(common))}' }}"
        )
    for tool in result.selected_tools:
        for module in result.bundle.tools[tool].effective_runtime_modules(accelerator) if result.bundle else ():
            if module in {"array-common", "wgbs-common"}:
                continue
            image = result.runtime_files.get(module)
            if image and tool in process_names:
                lines.append(f"  withName: '{process_names[tool]}' {{ container = '{_groovy(str(image))}' }}")
    if accelerator == "gpu" and "MEnet" in result.selected_tools:
        # Nextflow's accelerator directive exposes only logical GPU IDs to
        # local containerized tasks; no batch scheduler is configured here.
        requested = max(1, int(gpu_count))
        lines.append(
            f"  withName: 'RUN_MENET' {{ accelerator = {requested}; maxForks = 1 }}"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _initial_run_manifest(
    request: DeconRequest,
    result: DoctorResult,
    command: list[str],
    *,
    projection_report: dict | None = None,
    preprocess_report: dict | None = None,
) -> dict:
    bundle = result.bundle
    release_validation = bundle.payload.get("release_validation", {})
    simulation_validated_tools = [
        tool for tool in result.selected_tools
        if bundle.tools[tool].validation_scope
        == "fixed_seed_truth_labeled_simulated_native_wgbs_v1"
    ]
    if simulation_validated_tools:
        disclosure_status = "PERFORMED_ON_TRUTH_LABELED_SIMULATION"
    elif bundle.status == "RELEASED_UNVALIDATED":
        disclosure_status = "NOT_PERFORMED"
    else:
        disclosure_status = "SEE_REFERENCE_VALIDATION_RECORDS"
    reference_qc = {}
    for tool in result.selected_tools:
        capability = bundle.tools[tool]
        if "qc_report" in capability.artifacts:
            report = bundle.artifact(tool, "qc_report")
            reference_qc[tool] = {"path": str(report), "sha256": sha256_file(report)}
    disclosure_tools = list(result.selected_tools) + [
        item["tool"] for item in result.skipped_tools
        if item.get("tool") in bundle.tools
    ]
    disclosures = release_disclosures_for_tools(
        bundle, disclosure_tools, accelerator=request.accelerator
    )
    selected_disclosures = [
        disclosures[tool] for tool in result.selected_tools if tool in disclosures
    ]
    selected_evidence_states = {
        str(item.get("scientific_evidence_status")) for item in selected_disclosures
    }
    if "INCOMPLETE" in selected_evidence_states or "NOT_CONFIGURED" in selected_evidence_states:
        scientific_disclosure_status = "SEE_REFERENCE_VALIDATION_RECORDS"
    elif selected_disclosures and selected_evidence_states == {"BOUND_CURRENT"}:
        scientific_disclosure_status = "PERFORMED_ON_TRUTH_LABELED_SIMULATION"
    else:
        scientific_disclosure_status = disclosure_status
    claim_scopes = {
        str(item.get("scientific_claim_scope"))
        for item in selected_disclosures
        if item.get("scientific_claim_scope")
        and "metdecode" not in str(item.get("scientific_claim_scope")).lower()
    }
    scientific_claim_scope = next(iter(claim_scopes)) if len(claim_scopes) == 1 else None
    selected_warnings = [
        str(item["warning"]) for item in selected_disclosures if item.get("warning")
    ]
    if selected_warnings:
        scientific_warning = " ".join(selected_warnings)
    else:
        scientific_warning = None
    if selected_disclosures and all(
        item.get("scientific_claims_permitted") is True for item in selected_disclosures
    ) and scientific_claim_scope:
        scientific_claims_permitted = True
    else:
        scientific_claims_permitted = None
    selected_ru = [
        tool for tool, disclosure in disclosures.items()
        if disclosure.get("status") == "RELEASED_UNVALIDATED"
    ]
    explicit_only_skipped = [
        item["tool"] for item in result.skipped_tools
        if item.get("status") == "EXPLICIT_ONLY"
    ]
    blocked = [
        item["tool"] for item in result.skipped_tools
        if item.get("status") not in {"EXPLICIT_ONLY", "READY", "RELEASED_UNVALIDATED"}
    ]
    return {
        "schema": "demethflow-run-v2",
        "core_version": version(),
        "nextflow_version": NEXTFLOW_VERSION,
        "mode": "decon",
        "status": "PREPARED",
        "execution_status": "PREPARED",
        "started_at": _now(),
        "scenario_id": bundle.scenario_id,
        "reference_id": bundle.reference_id,
        "reference_version": bundle.version,
        "reference_digest": bundle.content_digest(),
        "release_class": release_validation.get("stage"),
        "scientific_validation_disclosure": {
            "status": scientific_disclosure_status,
            "scientific_status": "NOT_PROMOTED",
            "scientific_evidence_status": (
                "INCOMPLETE" if "INCOMPLETE" in selected_evidence_states else
                ("BOUND_CURRENT" if selected_evidence_states == {"BOUND_CURRENT"} else "NOT_CONFIGURED")
            ),
            "validation_type": release_validation.get("validation_type"),
            "simulation_validated_tools": simulation_validated_tools,
            "independent_real_world_validation": release_validation.get(
                "independent_real_world_validation"
            ),
            "independent_real_world_validation_required_for_release": release_validation.get(
                "independent_real_world_validation_required_for_release"
            ),
            "scientific_claim_scope": scientific_claim_scope,
            "independent_epic_v1_truth_available": release_validation.get(
                "independent_epic_v1_truth_available"
            ),
            "scientific_claims_permitted": scientific_claims_permitted,
            "warning": scientific_warning,
        },
        "reference_status_semantics": {
            "bundle_status": bundle.status,
            "execution_content_status": "COMPLETE" if result.ok else "INCOMPLETE",
            "scientific_evidence_status": "INCOMPLETE" if bundle.status == "PARTIAL" else "DECLARED",
            "interpretation": "PARTIAL distinguishes scientific-evidence incompleteness from execution-content completeness.",
        },
        "analysis_contract": bundle.contract,
        "requested_analysis_contract": request.analysis_contract,
        "cross_platform_authorized": request.allow_cross_platform,
        "validation_candidate_mode": request.validation_candidate,
        "platform_projection": projection_report,
        "wgbs_bam_preprocess": preprocess_report,
        "genome_build_requested": request.requested_genome_build,
        "genome_build_defaulted": (
            is_native_wgbs_contract(bundle.contract) and request.requested_genome_build is None
        ),
        "genome_build": bundle.payload["genome_build"],
        "input_platform": request.platform,
        "accelerator": request.accelerator,
        "random_seed": request.random_seed,
        "input": str(request.input_path.expanduser().resolve()),
        "input_digest": _input_digest(request.input_path),
        "input_sha256": sha256_file(request.input_path) if request.input_path.is_file() else None,
        "selected_tools": result.selected_tools,
        "selected_released_unvalidated_tools": selected_ru,
        "explicit_only_skipped_tools": explicit_only_skipped,
        "blocked_tools": blocked,
        "release_disclosures": disclosures,
        "tool_output_contracts": {
            tool: bundle.tools[tool].output_contract for tool in result.selected_tools
        },
        "tool_execution_metadata": {
            tool: {
                "reference_mode": bundle.tools[tool].reference_mode,
                "builtin_reference": bundle.tools[tool].builtin_reference,
                "postprocess_profile": bundle.tools[tool].postprocess_profile,
                "validation_policy": bundle.tools[tool].validation_policy,
                "execution_defaults": bundle.tools[tool].execution_defaults,
            }
            for tool in result.selected_tools
        },
        "skipped_tools": result.skipped_tools,
        "container_engine": result.container_engine,
        "runtime_images": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in result.runtime_files.items()
        },
        "external_runtime": {
            "paths": {tool: str(path) for tool, path in result.external_runtime_paths.items()},
            "metadata": result.external_runtime_metadata,
            "contract": "methunmix-external-runtime-v1",
        },
        "data_modules": {
            module: _data_module_evidence(exports)
            for module, exports in result.data_exports.items()
        },
        "reference_qc": reference_qc,
        "resolved_execution_parameters": result.execution_parameters,
        "command": command,
        "doctor": result_as_dict(result),
    }


def _path_evidence(path: Path) -> dict[str, object]:
    evidence: dict[str, object] = {"path": str(path)}
    if path.is_file():
        evidence.update(type="file", bytes=path.stat().st_size, sha256=sha256_file(path))
    elif path.is_dir():
        files = [child for child in sorted(path.rglob("*")) if child.is_file()]
        evidence.update(
            type="directory",
            file_count=len(files),
            bytes=sum(child.stat().st_size for child in files),
            tree_digest=stable_digest(
                (child.relative_to(path).as_posix(), sha256_file(child)) for child in files
            ),
        )
    else:
        evidence["type"] = "missing"
    return evidence


def _data_module_evidence(exports: dict[str, Path]) -> dict[str, object]:
    summarized = {name: _path_evidence(path) for name, path in sorted(exports.items())}
    return {
        "exports": summarized,
        "digest": stable_digest(
            (
                name,
                str(evidence.get("sha256") or evidence.get("tree_digest", "")),
            )
            for name, evidence in summarized.items()
        ),
    }


def _write_tool_status(outdir: Path, result: DoctorResult) -> None:
    with (outdir / "tool_status.tsv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["tool", "status", "reason"])
        for tool in result.selected_tools:
            capability = result.bundle.tools[tool] if result.bundle else None
            status, reason = (
                capability.effective_status(result.accelerator)
                if capability is not None
                else ("READY", "")
            )
            disclosure = result.release_disclosures.get(tool, {})
            if status == "RELEASED_UNVALIDATED" and disclosure.get("warning"):
                reason = str(disclosure["warning"])
            writer.writerow([tool, status, reason or ""])
        for item in result.skipped_tools:
            disclosure = result.release_disclosures.get(item["tool"], {})
            reason = item["reason"]
            if disclosure.get("warning"):
                reason = f"{reason}; {disclosure['warning']}" if reason else str(disclosure["warning"])
            writer.writerow([item["tool"], item["status"], reason])
    # Keep the historical three-column TSV stable for downstream consumers and
    # place the complete, machine-readable RU disclosure beside it.
    atomic_write_json(outdir / "release_disclosures.json", result.release_disclosures)


def _offline_env(outdir: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            env.pop(key, None)
    env.update(
        NXF_OFFLINE="true",
        NXF_DISABLE_CHECK_LATEST="true",
        NXF_HOME=str(outdir / ".demethflow" / "nextflow-home"),
        NXF_WORK=str(outdir / ".demethflow" / "work"),
        APPTAINER_CACHEDIR=str(outdir / ".demethflow" / "container-cache"),
        SINGULARITY_CACHEDIR=str(outdir / ".demethflow" / "container-cache"),
    )
    # The bundled launcher invokes its Java binary by absolute path.  Do not
    # leak a space-containing installation path into task PATH/JAVA_HOME;
    # Apptainer/Nextflow task wrappers may otherwise split the export.
    env.pop("JAVA_HOME", None)
    return env


def _bridge_workflow_command(command: list[str], bridge: Path) -> list[str]:
    source = PROJECT_ROOT / "deconvolution"
    bridge.mkdir(parents=True, exist_ok=True)
    source_digest = _workflow_source_digest(source)
    sentinel = bridge / ".demethflow-bridge.json"
    expected_sentinel = {
        "schema": "demethflow-workflow-bridge-v1",
        "source": str(source.resolve()),
        "source_digest": source_digest,
    }
    if sentinel.is_symlink():
        raise DeMethFlowError(f"Refusing symlinked workflow bridge sentinel: {sentinel}")
    if sentinel.is_file():
        try:
            observed_sentinel = json.loads(sentinel.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeMethFlowError(f"Invalid workflow bridge sentinel: {sentinel}") from exc
        if observed_sentinel != expected_sentinel:
            raise DeMethFlowError(f"Workflow bridge provenance mismatch: {sentinel}")
    else:
        # Materialize exactly once. Refreshing this tree immediately before a
        # resume can perturb Nextflow's project/bin fingerprint even when every
        # byte is unchanged, causing a one-run cache miss. The bridge path is
        # content-addressed by _persistent_workflow_bridge, so changed source
        # code receives a new bridge instead of mutating the active one.
        for child in source.iterdir():
            destination = bridge / child.name
            # Nextflow bind-mounts <projectDir>/bin into every task container
            # and prepends it to PATH. A symlinked bin directory is not
            # portable across Apptainer versions, so materialize it.
            if child.name == "bin" and child.is_dir():
                if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
                    raise DeMethFlowError(f"Unsafe persistent workflow bridge entry: {destination}")
                shutil.copytree(child, destination, copy_function=shutil.copy2, dirs_exist_ok=True)
            elif child.is_dir():
                if destination.is_symlink():
                    if destination.resolve(strict=False) != child.resolve():
                        destination.unlink()
                        destination.symlink_to(child, target_is_directory=True)
                elif destination.exists():
                    raise DeMethFlowError(f"Unsafe persistent workflow bridge entry: {destination}")
                else:
                    destination.symlink_to(child, target_is_directory=True)
            else:
                if destination.is_symlink() or destination.is_dir():
                    raise DeMethFlowError(f"Unsafe persistent workflow bridge entry: {destination}")
                shutil.copy2(child, destination)
        atomic_write_json(sentinel, expected_sentinel)
    output: list[str] = []
    source_resolved = source.resolve()
    for value in command:
        path = Path(value)
        try:
            relative = path.resolve().relative_to(source_resolved)
        except (OSError, ValueError):
            output.append(value)
            continue
        if len(relative.parts) == 1 and path.suffix in {".nf", ".config"}:
            output.append(str(bridge / relative))
        else:
            output.append(value)
    return output


def _persistent_workflow_bridge(outdir: Path) -> Path:
    source = (PROJECT_ROOT / "deconvolution").resolve()
    token = stable_digest([
        ("outdir", str(outdir.resolve()).encode("utf-8").hex()),
        ("source", str(source).encode("utf-8").hex()),
        ("source_digest", _workflow_source_digest(source)),
    ])[:24]
    configured_root = os.environ.get("METHUNMIX_WORKFLOW_BRIDGE_ROOT") or os.environ.get("DEMETHFLOW_WORKFLOW_BRIDGE_ROOT")
    if configured_root:
        bridge_root = Path(configured_root).expanduser()
    else:
        temporary_root = Path(os.environ.get("TMPDIR", "/tmp")).expanduser()
        bridge_root = temporary_root / f"demethflow-{os.getuid()}-workflows"
    if any(character.isspace() for character in str(bridge_root)):
        raise DeMethFlowError(
            "Workflow bridge root must not contain whitespace because Nextflow "
            f"prepends its bin directory to container PATH: {bridge_root}"
        )
    if bridge_root.is_symlink():
        raise DeMethFlowError(f"Refusing symlinked workflow bridge root: {bridge_root}")
    bridge_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    bridge_root = bridge_root.resolve()
    if bridge_root.stat().st_uid != os.getuid():
        raise DeMethFlowError(f"Workflow bridge root is not owned by the current user: {bridge_root}")
    bridge = bridge_root / f"demethflow-{os.getuid()}-workflow-{token}"
    if bridge.is_symlink():
        raise DeMethFlowError(f"Refusing symlinked persistent workflow bridge: {bridge}")
    if bridge.exists() and not bridge.is_dir():
        raise DeMethFlowError(f"Persistent workflow bridge is not a directory: {bridge}")
    bridge.mkdir(mode=0o700, parents=False, exist_ok=True)
    if bridge.stat().st_uid != os.getuid():
        raise DeMethFlowError(f"Persistent workflow bridge is not owned by the current user: {bridge}")
    bridge.chmod(0o700)
    return bridge


def _workflow_source_digest(source: Path) -> str:
    files = [
        child for child in sorted(source.rglob("*"))
        if child.is_file()
        and "__pycache__" not in child.parts
        and child.suffix != ".pyc"
    ]
    return stable_digest(
        (child.relative_to(source).as_posix(), sha256_file(child))
        for child in files
    )


def _input_glob(path: Path) -> str:
    resolved = path.expanduser().resolve()
    return str(resolved / "*.csv") if resolved.is_dir() else str(resolved)


def _native_input_dirs(path: Path) -> tuple[Path, Path]:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        return resolved.parent, resolved.parent
    bed = resolved / "bed" if (resolved / "bed").is_dir() else resolved
    pat = resolved / "pat" if (resolved / "pat").is_dir() else resolved
    return bed, pat


def _input_digest(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        return sha256_file(resolved)
    pairs = [
        (item.relative_to(resolved).as_posix(), sha256_file(item))
        for item in sorted(resolved.rglob("*"))
        if item.is_file()
    ]
    return stable_digest(pairs)


def _profile_engine(result: DoctorResult, runtime: str) -> str:
    return result.container_engine if runtime == "container" else "conda"


def _doctor_failure(result: DoctorResult) -> str:
    failures = [f"{check.name}: {check.detail}" for check in result.checks if check.required and not check.ok]
    return "Preflight failed:\n  - " + "\n  - ".join(failures)


def _normalize_label(value: str) -> str:
    return " ".join(value.strip().lower().replace("_", " ").split())


def _groovy(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
