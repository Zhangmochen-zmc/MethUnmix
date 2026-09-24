from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import CORE_API, NEXTFLOW_VERSION, PROJECT_ROOT, REFERENCE_SCHEMA, RUNTIME_API, version
from .errors import DeMethFlowError
from .celfeer import read_cell_types as read_celfeer_cell_types, write_reference_qc
from .manifest import genome_build_for_contract, is_native_wgbs_contract
from .epic_projection import CONTRACT as EPIC_FROM_450K_CONTRACT
from .runtime import bundled_nextflow, choose_container_engine, find_runtime_file
from .science import approved_policy_result, validate_user_array_build
from .modules import ModuleStore
from .util import (
    atomic_write_json,
    default_data_home,
    prepare_data_home,
    require_linux_x86_64_workflow,
    require_local_executor,
    sha256_file,
    validate_id,
)


READY_ARTIFACTS = {
    "ARIC": {
        "reference": "aric/output/cell_type_centroids.csv",
        "qc_report": "aric/output/aric_qc.json",
        "provenance": "aric/output/provenance.json",
        "scientific_validation": "aric/output/scientific_validation.json",
    },
    "MethylCIBERSORT": {
        "reference": "methylcibersort/output/MethylCIBERSORT.txt",
        "marker_selection": "methylcibersort/output/marker_selection.tsv",
        "qc_report": "methylcibersort/output/construction_qc.json",
        "provenance": "methylcibersort/output/construction_parameters.json",
        "scientific_validation": "methylcibersort/output/scientific_validation.json",
    },
    "EDec": {
        "markers": "edec/output/edec_stage0_markers.rds",
        "tref": "edec/output/edec_tref.rds",
        "qc_report": "edec/output/edec_qc.json",
        "scientific_validation": "edec/output/scientific_validation.json",
    },
    "EMeth": {
        "reference": "emeth/output/ref/EMeth.RData",
        "source": "emeth/output/EMeth-master",
        "qc_report": "emeth/output/emeth_qc.json",
        "marker_provenance": "emeth/output/marker_provenance.json",
        "dmc_results": "emeth/output/ref/dmc",
        "scientific_validation": "emeth/output/scientific_validation.json",
    },
    "EpiSCORE": {
        "gene_reference": "episcore/output/episcore_gene_reference.rds",
        "qc_report": "episcore/output/episcore_qc.json",
        "scientific_validation": "episcore/output/scientific_validation.json",
    },
    "EpiDISH": {"reference": "epidish/output/ref/EpiDISH_dynamic_reference.csv"},
    "Houseman": {
        "cell_reference": "houseman/output/cell_ref.rds",
        "beta_reference": "houseman/output/beta_ref.rds",
    },
    "MethAtlas": {"reference": "methatlas/output/methatlas_reference.csv"},
    "RefFreeEWAS": {"reference": "reffreeewas/output/reffreeewas_reference.csv"},
    "PRmeth": {"reference": "prmeth/output/prmeth_reference.csv"},
    "Tsisal": {"reference": "tsisal/output/tsisal_reference.csv"},
    "UXM": {
        "atlas": "uxm/output/marker_atlas.tsv",
        "qc_report": "uxm/output/qc.json",
        "input_manifest": "uxm/output/input_manifest.json",
    },
    "MethylBERT": {
        "model": "methylbert/output/model",
        "markers": "methylbert/output/markers_with_id.tsv",
        "runtime_metadata": "methylbert/output/runtime_metadata.json",
        "qc_report": "methylbert/output/qc.json",
        "split_manifest": "methylbert/output/split_manifest.json",
    },
    "CelFEER": {
        "markers": "celfeer/output/6_markers.txt",
        "cell_types": "celfeer/output/cell_types.tsv",
        "marker_selection": "celfeer/output/markers.tsv",
        "unique_regions": "celfeer/output/unique_markers.tsv",
        "qc_report": "celfeer/output/qc.json",
    },
}

TOOL_RUNTIME_MODULES = {
    "ARIC": ["array-common", "ARIC"],
    "MethylCIBERSORT": ["array-common", "MethylCIBERSORT"],
    "MeDeCom": ["array-common", "MeDeCom"],
    "EDec": ["array-common", "EDec"],
    "EMeth": ["array-common", "EMeth"],
    "EpiSCORE": ["array-common", "EpiSCORE"],
    "EpiDISH": ["array-common", "EpiDISH"],
    "Houseman": ["array-common", "Houseman"],
    "MethAtlas": ["array-common", "MethAtlas"],
    "RefFreeEWAS": ["array-common", "RefFreeEWAS"],
    "PRmeth": ["array-common", "PRmeth"],
    "Tsisal": ["array-common", "Tsisal"],
    "UXM": ["wgbs-common"],
    "MethylBERT": ["MethylBERT-gpu"],
    "CelFEER": ["wgbs-common"],
}

KNOWN_NOT_ADAPTED = {
    "MEnet": "construction output is a marker reference, not the trained deconvolution model",
}
KNOWN_QUARANTINED = {
}

SCIENTIFICALLY_GATED_TOOLS = {"EDec", "EMeth", "EpiSCORE", "ARIC", "MethylCIBERSORT"}
AUTOMATED_BUILD_VALIDATION_TOOLS = {"ARIC", "MethylCIBERSORT", "EDec", "EMeth", "EpiSCORE"}
CELFEER_ARTIFACT_FILENAMES = {
    "markers": "markers.txt",
    "cell_types": "cell_types.tsv",
    "marker_selection": "marker_selection.tsv",
    "unique_regions": "unique_regions.tsv",
    "qc_report": "qc.json",
}


@dataclass
class BuildRequest:
    reference_input: Path | None
    metadata: Path | None
    outdir: Path
    scenario: str
    platform: str
    reference_id: str
    reference_version: str
    build_tools: str = "all"
    analysis_contract: str | None = None
    genome_build: str | None = None
    requested_genome_build: str | None = None
    reference_root: Path | None = None
    uxm_pat_root: Path | None = None
    methylbert_reads_root: Path | None = None
    celfeer_pat_root: Path | None = None
    celfeer_input_root: Path | None = None
    celfeer_metadata: Path | None = None
    runtime_root: Path | None = None
    container_engine: str = "auto"
    executor: str = "local"
    accelerator: str = "cpu"
    dry_run: bool = False
    resume: bool = False


def run_build(request: BuildRequest) -> tuple[dict, Path | None]:
    require_local_executor(request.executor)
    if not request.dry_run:
        require_linux_x86_64_workflow()
    validate_id(request.reference_id, "reference_id")
    validate_id(request.reference_version, "reference_version")
    validate_id(request.scenario, "scenario")
    selected = _normalize_tools(request.build_tools)
    generic_selected = [tool for tool in selected if tool not in {"UXM", "MethylBERT", "CelFEER"}]
    reference_input = request.reference_input.expanduser().resolve() if request.reference_input else None
    uxm_pat_root = request.uxm_pat_root.expanduser().resolve() if request.uxm_pat_root else None
    methylbert_reads_root = request.methylbert_reads_root.expanduser().resolve() if request.methylbert_reads_root else None
    celfeer_pat_root = request.celfeer_pat_root.expanduser().resolve() if request.celfeer_pat_root else None
    celfeer_input_root = request.celfeer_input_root.expanduser().resolve() if request.celfeer_input_root else None
    celfeer_metadata = request.celfeer_metadata.expanduser().resolve() if request.celfeer_metadata else None
    metadata = request.metadata.expanduser().resolve() if request.metadata else None
    if generic_selected and (not reference_input or not reference_input.is_dir()):
        raise DeMethFlowError(f"Reference input directory does not exist: {reference_input}")
    if "UXM" in selected and (not uxm_pat_root or not uxm_pat_root.is_dir()):
        raise DeMethFlowError("UXM construction requires --uxm-pat-root containing *.pat.gz files")
    if "MethylBERT" in selected and (not methylbert_reads_root or not methylbert_reads_root.is_dir()):
        raise DeMethFlowError("MethylBERT construction requires --methylbert-reads-root containing *_reads.csv files")
    if "CelFEER" in selected:
        supplied = [path for path in (celfeer_pat_root, celfeer_input_root) if path is not None]
        if len(supplied) != 1 or not supplied[0].is_dir():
            raise DeMethFlowError(
                "CelFEER construction requires exactly one existing --celfeer-pat-root or --celfeer-input-root"
            )
        if celfeer_metadata and not celfeer_metadata.is_file():
            raise DeMethFlowError(f"CelFEER metadata does not exist: {celfeer_metadata}")
    if {"UXM", "MethylBERT", "CelFEER"}.intersection(selected) and request.platform != "wgbs":
        raise DeMethFlowError("UXM, MethylBERT, and CelFEER construction are available only for --platform wgbs")
    if "MethylBERT" in selected and request.accelerator != "gpu":
        raise DeMethFlowError("MethylBERT construction requires --accelerator gpu")
    if selected != ["CelFEER"] and (not metadata or not metadata.is_file()):
        raise DeMethFlowError(f"Reference metadata does not exist: {metadata}")
    if metadata and not metadata.is_file():
        raise DeMethFlowError(f"Reference metadata does not exist: {metadata}")
    contract = _contract(request.platform, request.analysis_contract, request.genome_build)
    build_platform = "450k" if contract == EPIC_FROM_450K_CONTRACT else request.platform
    expected_build = genome_build_for_contract(contract)
    if is_native_wgbs_contract(contract):
        request.genome_build = expected_build
    if {"UXM", "MethylBERT", "CelFEER"}.intersection(selected) and not is_native_wgbs_contract(contract):
        raise DeMethFlowError("UXM, MethylBERT, and CelFEER require a native WGBS analysis contract")
    nextflow = bundled_nextflow()
    if not nextflow:
        raise DeMethFlowError("Bundled/system Nextflow is not available")
    engine, executable = choose_container_engine(request.container_engine)
    if not executable:
        raise DeMethFlowError(f"Container engine not found: {request.container_engine}")
    workflow = PROJECT_ROOT / "nextflow_ref" / "main.nf"
    module_store = ModuleStore()
    build_runtime_module = module_store.find("build-runtime")
    build_assets_module = module_store.find("build-assets")
    bundled_sif = PROJECT_ROOT / "nextflow_ref" / "containers" / "deconvolution.sif"
    build_sif = (
        bundled_sif
        if bundled_sif.is_file()
        else (build_runtime_module.install_root / "containers" / "deconvolution.sif")
        if build_runtime_module
        else bundled_sif
    )
    if not build_sif.is_file():
        raise DeMethFlowError(f"Offline reference construction SIF is missing: {build_sif}")
    bundled_assets = PROJECT_ROOT / "nextflow_ref" / "assets"
    bundled_tools = PROJECT_ROOT / "nextflow_ref" / "tools"
    assets_root = bundled_assets if bundled_assets.is_dir() else (build_assets_module.install_root / "assets") if build_assets_module else bundled_assets
    tools_root = bundled_tools if bundled_tools.is_dir() else (build_assets_module.install_root / "tools") if build_assets_module else bundled_tools
    if not assets_root.is_dir() or not tools_root.is_dir():
        raise DeMethFlowError("Offline build-assets module is missing assets/ or tools/")
    build_outdir = request.outdir.expanduser().resolve() / "reference_build"
    build_outdir.mkdir(parents=True, exist_ok=True)
    runtime_config = build_outdir / ".runtime.config"
    escaped_sif = str(build_sif).replace("\\", "\\\\").replace("'", "\\'")
    runtime_lines = [
        "profiles {\n"
        f"  {engine} {{\n"
        "    conda.enabled = false\n"
        f"    {engine}.enabled = true\n"
        f"    {engine}.autoMounts = true\n"
        f"    process.container = '{escaped_sif}'\n"
        "  }\n"
        "}\n"
    ]
    if "UXM" in selected:
        uxm_sif = find_runtime_file("wgbs-common", request.runtime_root)
        if not uxm_sif:
            raise DeMethFlowError("UXM construction requires the wgbs-common runtime module")
        runtime_lines.append(
            "process {\n"
            f"  withName: BUILD_UXM_REFERENCE {{ container = '{str(uxm_sif).replace(chr(39), chr(92) + chr(39))}' }}\n"
            "}\n"
        )
    methylbert_pretrain: Path | None = None
    if "MethylBERT" in selected:
        mb_sif = find_runtime_file("MethylBERT-gpu", request.runtime_root)
        if not mb_sif:
            raise DeMethFlowError("MethylBERT construction requires the MethylBERT-gpu runtime module")
        # Pretraining weights are construction-only assets.  They must not be
        # coupled to the build-specific genome cache required later by a
        # trained model's deconvolution preprocessing.
        methylbert_pretrain = (
            assets_root / "methylbert" / f"pretrain_{request.genome_build}"
        )
        if not methylbert_pretrain.is_dir():
            raise DeMethFlowError(
                "MethylBERT construction requires the build-assets directory "
                f"methylbert/pretrain_{request.genome_build}; no other genome build is substituted"
            )
        runtime_lines.append(
            "process {\n"
            f"  withName: BUILD_METHYLBERT_REFERENCE {{ container = '{str(mb_sif).replace(chr(39), chr(92) + chr(39))}'; containerOptions = '--nv' }}\n"
            "}\n"
        )
    if "MethylCIBERSORT" in selected:
        methylcibersort_sif = find_runtime_file("MethylCIBERSORT", request.runtime_root)
        if not methylcibersort_sif:
            raise DeMethFlowError(
                "MethylCIBERSORT construction requires the offline MethylCIBERSORT runtime module"
            )
        build_default = str(build_sif).replace("'", "\\'")
        methyl_runtime = str(methylcibersort_sif).replace("'", "\\'")
        runtime_lines.append(
            "process {\n"
            "  withName: RUN_MARKER_TOOL {\n"
            f"    container = {{ tool == 'methylcibersort' ? '{methyl_runtime}' : '{build_default}' }}\n"
            "  }\n"
            "}\n"
        )
    runtime_config.write_text("".join(runtime_lines), encoding="utf-8")
    command = list(nextflow)
    command.extend(
        [
            "run",
            str(workflow),
            "-c",
            str(runtime_config),
            "-profile",
            f"{'standard' if request.executor == 'local' else request.executor},{engine}",
            "--data_type",
            build_platform,
            "--tools",
            request.build_tools,
            "--outdir",
            str(build_outdir),
            "--assets_dir",
            str(assets_root),
            "--tool_source_dir",
            str(tools_root),
            "--scenario_id",
            request.scenario,
            "-work-dir",
            str(build_outdir / ".work"),
        ]
    )
    if metadata:
        command.extend(["--metadata", str(metadata)])
    if reference_input:
        command.extend(["--reference_root", str(reference_input)])
    if uxm_pat_root:
        command.extend(["--uxm_pat_root", str(uxm_pat_root)])
    if methylbert_reads_root:
        command.extend(["--methylbert_reads_root", str(methylbert_reads_root)])
    if celfeer_pat_root:
        command.extend(["--celfeer_pat_root", str(celfeer_pat_root)])
    if celfeer_input_root:
        command.extend(["--celfeer_input_root", str(celfeer_input_root)])
    if celfeer_metadata:
        command.extend(["--celfeer_metadata", str(celfeer_metadata)])
    if methylbert_pretrain:
        command.extend(["--methylbert_pretrain", str(methylbert_pretrain)])
    if request.genome_build:
        command.extend(["--genome_build", request.genome_build])
    if request.resume:
        command.append("-resume")
    manifest = {
        "schema": "demethflow-build-run-v1",
        "core_version": version(),
        "nextflow_version": NEXTFLOW_VERSION,
        "status": "DRY_RUN" if request.dry_run else "RUNNING",
        "started_at": _now(),
        "scenario_id": request.scenario,
        "reference_id": request.reference_id,
        "reference_version": request.reference_version,
        "source_platform": build_platform,
        "analysis_contract": contract,
        "genome_build_requested": request.requested_genome_build,
        "genome_build_defaulted": (
            is_native_wgbs_contract(contract) and request.requested_genome_build is None
        ),
        "genome_build": expected_build,
        "build_runtime": {"path": str(build_sif), "sha256": sha256_file(build_sif)},
        "metadata_sha256": sha256_file(metadata) if metadata else None,
        "selected_tools": selected,
        "source_inputs": {
            "reference_input": str(reference_input) if reference_input else None,
            "uxm_pat_root": str(uxm_pat_root) if uxm_pat_root else None,
            "methylbert_reads_root": str(methylbert_reads_root) if methylbert_reads_root else None,
            "celfeer_pat_root": str(celfeer_pat_root) if celfeer_pat_root else None,
            "celfeer_input_root": str(celfeer_input_root) if celfeer_input_root else None,
            "celfeer_metadata": str(celfeer_metadata) if celfeer_metadata else None,
        },
        "command": command,
    }
    atomic_write_json(request.outdir / "build_run_manifest.json", manifest)
    if request.dry_run:
        return manifest, None
    env = dict(os.environ)
    for key in tuple(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            env.pop(key, None)
    env.update(
        NXF_OFFLINE="true",
        NXF_DISABLE_CHECK_LATEST="true",
        NXF_HOME=str(build_outdir / ".nextflow-home"),
        APPTAINER_CACHEDIR=str(build_outdir / ".container-cache"),
        SINGULARITY_CACHEDIR=str(build_outdir / ".container-cache"),
    )
    engine_dir = str(Path(executable).parent)
    env["PATH"] = engine_dir + os.pathsep + env.get("PATH", "")
    env.pop("JAVA_HOME", None)
    launch_dir = build_outdir / ".launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="demethflow-build-workflow-") as temporary:
        bridged_command = _bridge_build_command(command, Path(temporary))
        manifest["executed_command"] = bridged_command
        atomic_write_json(request.outdir / "build_run_manifest.json", manifest)
        completed = subprocess.run(bridged_command, cwd=launch_dir, env=env, check=False)
    manifest["finished_at"] = _now()
    manifest["exit_code"] = completed.returncode
    if completed.returncode != 0:
        manifest["status"] = "FAILED"
        atomic_write_json(request.outdir / "build_run_manifest.json", manifest)
        raise DeMethFlowError(f"Reference construction failed with exit code {completed.returncode}")
    gated_tools = [tool for tool in selected if tool in AUTOMATED_BUILD_VALIDATION_TOOLS]
    if gated_tools and reference_input and metadata:
        validate_user_array_build(
            build_output=build_outdir,
            reference_root=reference_input,
            metadata=metadata,
            platform=build_platform,
            selected_tools=gated_tools,
            tools_root=tools_root,
            build_sif=build_sif,
            runtime_files={tool: find_runtime_file(tool, request.runtime_root) for tool in gated_tools},
            container_executable=executable,
        )
    if "CelFEER" in selected:
        output = build_outdir / "celfeer" / "output"
        write_reference_qc(
            output / "qc.json",
            scenario_id=request.scenario,
            markers=output / "6_markers.txt",
            cell_types_file=output / "cell_types.tsv",
            marker_selection=output / "markers.tsv",
            unique_regions=output / "unique_markers.tsv",
            read_bins=(
                assets_root / "celfeer" / "CelFEER-main" / "data" /
                ("hg19_read_bins.txt" if request.genome_build == "hg19" else "read_bins.txt")
            ),
            genome_build=request.genome_build or "hg38",
        )
    bundle_path = register_build_outputs(
        request,
        build_outdir,
        contract,
        r_runtime_prefix=[executable, "exec", "--cleanenv", str(build_sif), "Rscript"],
    )
    manifest["status"] = "SUCCEEDED"
    manifest["registered_manifest"] = str(bundle_path)
    atomic_write_json(request.outdir / "build_run_manifest.json", manifest)
    return manifest, bundle_path


def register_build_outputs(
    request: BuildRequest,
    build_outdir: Path,
    contract: str,
    r_runtime_prefix: list[str] | None = None,
) -> Path:
    target_root = (
        request.reference_root.expanduser().resolve()
        if request.reference_root
        else prepare_data_home() / "references"
    )
    destination = target_root / request.reference_id / request.reference_version
    if destination.exists():
        raise DeMethFlowError(f"Immutable reference version already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{request.reference_id}-", dir=destination.parent))
    try:
        artifacts_root = staging / "artifacts"
        artifacts_root.mkdir()
        capabilities: dict[str, dict] = {}
        selected = _normalize_tools(request.build_tools)
        all_tools = sorted(set(selected) | set(READY_ARTIFACTS) | set(KNOWN_NOT_ADAPTED) | set(KNOWN_QUARANTINED))
        checksums: dict[str, str] = {}
        for tool in all_tools:
            expected = READY_ARTIFACTS.get(tool)
            if expected and tool in selected:
                missing = [relative for relative in expected.values() if not (build_outdir / relative).exists()]
                if missing:
                    capabilities[tool] = {
                        "status": "INVALID",
                        "reason": f"construction completed without expected artifacts: {', '.join(missing)}",
                        "artifacts": {},
                        "runtime_modules": TOOL_RUNTIME_MODULES.get(tool, []),
                    }
                    continue
                artifact_map: dict[str, str] = {}
                for name, relative in expected.items():
                    source = build_outdir / relative
                    target_name = CELFEER_ARTIFACT_FILENAMES.get(name, source.name) if tool == "CelFEER" else source.name
                    if tool == "ARIC" and name == "reference":
                        target = artifacts_root / "common" / "cell_type_centroids.csv"
                    else:
                        target = artifacts_root / tool.lower() / target_name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_dir():
                        shutil.copytree(source, target)
                    else:
                        shutil.copy2(source, target)
                    manifest_rel = target.relative_to(staging).as_posix()
                    artifact_map[name] = manifest_rel
                    if target.is_file():
                        checksums[manifest_rel] = sha256_file(target)
                    else:
                        for child in sorted(target.rglob("*")):
                            if child.is_file():
                                checksums[child.relative_to(staging).as_posix()] = sha256_file(child)
                capability = {
                    "status": "READY",
                    "artifacts": artifact_map,
                    "runtime_modules": TOOL_RUNTIME_MODULES[tool],
                }
                if tool == "CelFEER":
                    qc = _load_qc(build_outdir / expected["qc_report"], tool)
                    capability.update(
                        status="QUARANTINED",
                        reason=(
                            "technical integration and structural validation completed; "
                            "scenario-specific simulation validation is pending"
                        ),
                        data_modules=[f"CelFEER-{request.genome_build}-data"],
                        validation_scope="structural_only_pending_scenario_simulation",
                        output_contract="cell_proportions_v1",
                    )
                    if qc.get("structural_status") != "PASS":
                        capability["status"] = "INVALID"
                        capability["reason"] = "CelFEER construction QC did not pass the structural gate"
                elif tool in SCIENTIFICALLY_GATED_TOOLS:
                    qc = _load_qc(build_outdir / expected["qc_report"], tool)
                    validation_disposition = _scientific_validation_disposition(qc, tool)
                    capability.update(_user_build_science_state(qc, tool, validation_disposition))
                    if tool == "ARIC" and validation_disposition in {"PASS", "NOT_ASSESSED"}:
                        metrics = qc["scientific_validation"].get("metrics", {})
                        capability["validation_policy"] = {
                            "minimum_overlap_cpg": int(metrics.get("frozen_minimum_overlap_cpg", 500))
                        }
                elif tool == "MethylBERT":
                    capability.update(
                        status="QUARANTINED",
                        reason=(
                            "construction passed structural QC; independent scenario simulation, "
                            "production E2E and offline validation are required before READY"
                        ),
                        data_modules=[f"MethylBERT-{request.genome_build}-data"],
                        validation_scope="structural_user_built_pending_scientific_validation",
                        execution_profiles={
                            "cpu": {
                                "status": "NOT_AVAILABLE",
                                "runtime_modules": ["MethylBERT-cpu"],
                                "reason": "user-built tissue models require the GPU execution profile",
                            },
                            "gpu": {
                                "status": "QUARANTINED",
                                "runtime_modules": ["MethylBERT-gpu"],
                                "reason": "user-built model has not passed scenario scientific and offline gates",
                            },
                        },
                    )
                elif tool == "UXM":
                    capability.update(
                        status="QUARANTINED",
                        reason=(
                            "construction passed structural QC; independent scenario simulation, "
                            "production E2E and offline validation are required before READY"
                        ),
                        validation_scope="structural_user_built_pending_scientific_validation",
                    )
                capabilities[tool] = capability
            elif tool in KNOWN_QUARANTINED:
                capabilities[tool] = {
                    "status": "QUARANTINED",
                    "reason": KNOWN_QUARANTINED[tool],
                    "artifacts": {},
                    "runtime_modules": [],
                }
            elif tool in KNOWN_NOT_ADAPTED or tool in selected:
                capabilities[tool] = {
                    "status": "NOT_ADAPTED",
                    "reason": KNOWN_NOT_ADAPTED.get(tool, "construction adapter is not yet registered"),
                    "artifacts": {},
                    "runtime_modules": [],
                }
            else:
                capabilities[tool] = {
                    "status": "NOT_AVAILABLE",
                    "reason": "not requested or not supplied in this construction bundle",
                    "artifacts": {},
                    "runtime_modules": [],
                }
        celfeer_cells = build_outdir / READY_ARTIFACTS["CelFEER"]["cell_types"]
        if "CelFEER" in selected and celfeer_cells.is_file():
            cell_types = _cell_type_records(read_celfeer_cell_types(celfeer_cells))
        else:
            cell_types = _read_cell_types(request.metadata)
        if contract in {"array_450k_cpg", "array_epic_cpg", EPIC_FROM_450K_CONTRACT}:
            capabilities["MeDeCom"] = {
                "status": "READY",
                "artifacts": {},
                "runtime_modules": TOOL_RUNTIME_MODULES["MeDeCom"],
                "reference_requirement": "none",
                "output_contract": "reference_free_components_v2",
                "execution_policy": "explicit_only",
                "execution_defaults": {
                    "recommended_k": len(cell_types),
                    "lambda_mode": "minimum_mean_cve",
                },
                "validation_scope": "global_dynamic_k_reference_free_v2",
            }
        ready_count = sum(item["status"] == "READY" for item in capabilities.values())
        requested_ready = all(capabilities.get(tool, {}).get("status") == "READY" for tool in selected)
        selected_statuses = [capabilities.get(tool, {}).get("status") for tool in selected]
        if requested_ready and ready_count:
            bundle_status = "READY"
        elif any(status == "INVALID" for status in selected_statuses):
            bundle_status = "INVALID"
        elif ready_count:
            bundle_status = "PARTIAL"
        elif selected_statuses and all(status == "QUARANTINED" for status in selected_statuses):
            bundle_status = "QUARANTINED"
        else:
            bundle_status = "INVALID"
        payload = {
            "schema": "demethflow-reference-v1",
            "reference_id": request.reference_id,
            "version": request.reference_version,
            "scenario_id": request.scenario,
            "source_platform": "450k" if contract == EPIC_FROM_450K_CONTRACT else request.platform,
            "analysis_contract": contract,
            "compatible_input_routes": _routes(request.platform, contract),
            "genome_build": genome_build_for_contract(contract),
            "status": bundle_status,
            "cell_types": cell_types,
            "tool_capabilities": capabilities,
            "artifact_checksums": checksums,
            "compatibility": {
                "core_api": CORE_API,
                "reference_schema": REFERENCE_SCHEMA,
                "runtime_api": RUNTIME_API,
            },
            "provenance": {
                "kind": "user_built",
                "created_at": _now(),
                "metadata_sha256": sha256_file(request.metadata) if request.metadata else None,
                "construction_output": str(build_outdir),
            },
        }
        if contract == EPIC_FROM_450K_CONTRACT:
            _attach_epic_from_450k_projection(
                staging, payload, checksums, r_runtime_prefix=r_runtime_prefix
            )
        atomic_write_json(staging / "manifest.json", payload)
        os.replace(staging, destination)
        return destination / "manifest.json"
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _read_cell_types(metadata: Path) -> list[dict[str, str]]:
    with metadata.open(newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        except csv.Error:
            dialect = csv.excel_tab
        reader = csv.DictReader(handle, dialect=dialect)
        fields = reader.fieldnames or []
        column = next((name for name in fields if name.strip().lower() == "cell_type"), None)
        if not column:
            raise DeMethFlowError(f"Metadata needs a cell_type column: {metadata}")
        names: list[str] = []
        for row in reader:
            value = (row.get(column) or "").strip()
            if value.lower() == "atrocyte":
                value = "Astrocyte"
            if value and value not in names:
                names.append(value)
    if not names:
        raise DeMethFlowError(f"Metadata contains no cell types: {metadata}")
    return _cell_type_records(names)


def _cell_type_records(names: list[str]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    used: set[str] = set()
    for name in names:
        base = _cell_id(name)
        candidate = base
        index = 2
        while candidate in used:
            candidate = f"{base}-{index}"
            index += 1
        used.add(candidate)
        cell = {"cell_type_id": candidate, "display_name": name}
        if name == "Astrocyte":
            cell["synonyms"] = ["atrocyte"]
        output.append(cell)
    return output


def _cell_id(name: str) -> str:
    if name.strip().lower() == "epithelial":
        return "epithelium"
    value = "".join(char.lower() if char.isalnum() else "-" for char in name)
    value = "-".join(part for part in value.split("-") if part)
    return value or "cell"


def _load_qc(path: Path, tool: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"{tool} produced an unreadable QC report: {path}") from exc
    if not isinstance(payload, dict):
        raise DeMethFlowError(f"{tool} QC report must be a JSON object: {path}")
    return payload


def _scientific_validation_disposition(qc: dict, tool: str) -> str:
    """Return PASS, FAIL, or NOT_ASSESSED without inventing global gates."""
    validation = qc.get("scientific_validation")
    if not isinstance(validation, dict):
        return "FAIL"
    if validation.get("engineering_status") != "PASS":
        return "FAIL"
    if validation.get("tool_specific_gate_status") == "FAIL":
        return "FAIL"
    if tool == "MethylCIBERSORT":
        marker_stability = qc.get("marker_stability")
        if not isinstance(marker_stability, dict) or marker_stability.get("status") != "PASS":
            return "FAIL"
    metrics = validation.get("metrics", {})
    policy = validation.get("approved_policy")
    if policy is None:
        return "NOT_ASSESSED"
    try:
        return approved_policy_result(metrics, tool, policy)
    except (AttributeError, TypeError, ValueError):
        return "FAIL"


def _user_build_science_state(qc: dict, tool: str, disposition: str | None = None) -> dict:
    """Map engineering and approved-policy evidence to a non-inferred status."""
    if qc.get("structural_status") != "PASS":
        return {
            "status": "INVALID",
            "reason": "construction QC did not pass the structural gate",
            "validation_scope": "structural_user_built",
            "output_contract": "cell_proportions_v1",
        }
    if qc.get("marker_gate_status", "PASS") != "PASS":
        return {
            "status": "QUARANTINED",
            "reason": "construction completed, but the fixed marker-count gate did not pass",
            "validation_scope": "structural_user_built",
            "output_contract": "cell_proportions_v1",
        }
    disposition = disposition or _scientific_validation_disposition(qc, tool)
    if disposition == "PASS":
        status = "READY"
        reason = "technical output and explicitly approved tool-specific validation policy passed"
        scope = "heldout_synthetic_truth_v1"
    elif disposition == "NOT_ASSESSED":
        status = "RELEASED_UNVALIDATED"
        reason = (
            "technical output checks passed; scientific metrics are reported, but no "
            "owner-approved tool-specific policy is available to make a READY claim"
        )
        scope = "heldout_synthetic_metrics_report_only"
    else:
        validation = qc.get("scientific_validation")
        validation = validation if isinstance(validation, dict) else {}
        status = "QUARANTINED"
        reason = validation.get("reason") or "technical validation or an explicitly approved tool-specific scientific policy failed"
        scope = "structural_user_built"
    return {
        "status": status,
        "reason": reason,
        "validation_scope": scope,
        "output_contract": "cell_proportions_v1",
    }


def _normalize_tools(raw: str) -> list[str]:
    canonical = {
        "edec": "EDec", "emeth": "EMeth", "epidish": "EpiDISH", "episcore": "EpiSCORE",
        "houseman": "Houseman", "menet": "MEnet", "methatlas": "MethAtlas",
        "reffreeewas": "RefFreeEWAS", "prmeth": "PRmeth", "tsisal": "Tsisal",
        "aric": "ARIC", "methylcibersort": "MethylCIBERSORT",
        "uxm": "UXM", "methylbert": "MethylBERT", "celfeer": "CelFEER",
    }
    if raw.strip().lower() == "all":
        return ["ARIC", "MethylCIBERSORT", "EDec", "EMeth", "EpiDISH", "EpiSCORE", "Houseman", "MEnet", "MethAtlas", "RefFreeEWAS", "PRmeth", "Tsisal"]
    output: list[str] = []
    for item in raw.split(","):
        key = item.strip().lower()
        if key and key not in canonical:
            raise DeMethFlowError(f"Unknown construction tool: {item.strip()}")
        if key and canonical[key] not in output:
            output.append(canonical[key])
    return output


def _contract(platform: str, explicit: str | None, genome_build: str | None = None) -> str:
    if explicit:
        if explicit not in {
            "array_450k_cpg", "array_epic_cpg", EPIC_FROM_450K_CONTRACT,
            "wgbs_native_hg19", "wgbs_native_hg38"
        }:
            raise DeMethFlowError(f"Invalid --analysis-contract: {explicit}")
        expected = genome_build_for_contract(explicit)
        if expected == "not_applicable" and genome_build is not None:
            raise DeMethFlowError("--genome-build cannot be used with an array analysis contract")
        if expected != "not_applicable" and genome_build and genome_build != expected:
            raise DeMethFlowError(
                f"--analysis-contract {explicit} conflicts with --genome-build {genome_build}"
            )
        if explicit == EPIC_FROM_450K_CONTRACT and platform != "epic":
            raise DeMethFlowError(
                "array_epic_from_450k_common_cpg_v1 requires --platform epic; reference construction uses 450K inputs"
            )
        return explicit
    if platform == "450k":
        if genome_build is not None:
            raise DeMethFlowError("--genome-build is valid only for native WGBS construction")
        return "array_450k_cpg"
    if platform == "epic":
        if genome_build is not None:
            raise DeMethFlowError("--genome-build is valid only for native WGBS construction")
        return "array_epic_cpg"
    build = genome_build or "hg38"
    return f"wgbs_native_{build}"


def _routes(platform: str, contract: str) -> list[str]:
    if contract == EPIC_FROM_450K_CONTRACT:
        return ["epic"]
    if platform == "wgbs" and contract == "array_epic_cpg":
        return ["epic", "wgbs"]
    return [{
        "array_450k_cpg": "450k",
        "array_epic_cpg": "epic",
        EPIC_FROM_450K_CONTRACT: "epic",
        "wgbs_native_hg19": "wgbs",
        "wgbs_native_hg38": "wgbs",
    }[contract]]


def _attach_epic_from_450k_projection(
    staging: Path,
    payload: dict,
    checksums: dict[str, str],
    *,
    r_runtime_prefix: list[str] | None = None,
) -> None:
    """Attach a frozen projection contract to a user-built 450K reference.

    The construction itself is performed in 450K feature space.  Every
    capability that was technically usable in 450K space is explicitly
    released for the derived route with a permanent unvalidated-science
    disclosure. This never creates or claims an independent EPIC-v1 result.
    """
    from .epic_projection import PROJECTION_SCHEMA

    epic_manifest = PROJECT_ROOT / "nextflow_ref/assets/manifests/epic/EPIC.hg38.manifest.tsv"
    hm450_manifest = PROJECT_ROOT / "nextflow_ref/assets/manifests/450k/HM450.hg38.manifest.tsv"
    if not epic_manifest.is_file() or not hm450_manifest.is_file():
        raise DeMethFlowError("frozen EPIC v1/HM450 manifests are required for the derived contract")

    def manifest_ids(path: Path) -> list[str]:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if not reader.fieldnames or "probeID" not in reader.fieldnames:
                raise DeMethFlowError(f"platform manifest has no probeID column: {path}")
            values = [row["probeID"].strip() for row in reader if row.get("probeID", "").strip()]
        if len(values) != len(set(values)):
            raise DeMethFlowError(f"duplicate probe IDs in platform manifest: {path}")
        return values

    projection = staging / "artifacts/platform_projection"
    projection.mkdir(parents=True, exist_ok=True)
    epic_copy = projection / "EPIC.v1.manifest.tsv"
    hm450_copy = projection / "HM450.manifest.tsv"
    shutil.copy2(epic_manifest, epic_copy)
    shutil.copy2(hm450_manifest, hm450_copy)
    epic_ids = set(manifest_ids(epic_manifest))
    common = [probe for probe in manifest_ids(hm450_manifest) if probe in epic_ids]
    if len(common) != 452999:
        raise DeMethFlowError(f"frozen EPIC-v1/HM450 common backbone changed: {len(common)}")
    common_path = projection / "common_cpgs.tsv"
    with common_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["probe_id"])
        writer.writerows((probe,) for probe in common)

    # Preserve a byte-verifiable 450K source snapshot before changing public
    # route/status fields. This prevents a derived bundle from silently
    # drifting away from the source used to construct it.
    source_snapshot = json.loads(json.dumps(payload))
    source_snapshot.update(
        reference_id=f"{payload['reference_id']}.source-450k-snapshot",
        source_platform="450k",
        analysis_contract="array_450k_cpg",
        compatible_input_routes=["450k"],
    )
    source_manifest = projection / "source_reference_manifest.json"
    atomic_write_json(source_manifest, source_snapshot)

    def attach_pending_science(tool: str, capability: dict) -> None:
        """Keep source-450K science as provenance, never as EPIC validation."""
        artifacts = capability.setdefault("artifacts", {})
        source_path = artifacts.pop("scientific_validation", None)
        if source_path:
            artifacts["source_450k_scientific_validation"] = source_path
        source_validation_path = None
        if tool == "MeDeCom" and capability.get("validation_evidence"):
            source_validation_path = capability.pop("validation_evidence")
            capability["source_450k_validation_evidence"] = source_validation_path
        relative = f"validation/epic_from_450k/{tool}.json"
        record = {
            "schema": "demethflow-epic-from-450k-scientific-validation-v1",
            "status": "NOT_AVAILABLE",
            "scenario_id": payload["scenario_id"],
            "tool": tool,
            "analysis_contract": EPIC_FROM_450K_CONTRACT,
            "input_array_version": "EPIC_v1",
            "source_reference_platform": "450k",
            "source_reference": (
                f"{source_snapshot['reference_id']}@{source_snapshot['version']}"
            ),
            "source_450k_result_is_not_inherited": True,
            "independent_epic_v1_truth_available": False,
            "required_repeats": 2,
            "required_gates": [
                "fixed_seed_repeatability",
                "independent_truth_accuracy",
                "canonical_cell_or_anonymous_component_axis",
                "sample_axis",
                "finite_nonnegative_normalized_output",
                "reference_and_projection_provenance",
                "tool_specific_input_overlap",
                "installed_archive_offline_execution",
                "zero_submission_resume",
            ],
            "promotion_eligible": False,
            "release_disposition": "RELEASED_UNVALIDATED",
            "blocker": "independent EPIC v1 truth validation was not performed by owner decision",
        }
        evidence_path = source_path or source_validation_path
        if evidence_path:
            source_file = staging / evidence_path
            record["source_450k_scientific_validation"] = {
                "artifact": evidence_path,
                "sha256": sha256_file(source_file),
            }
        destination = staging / relative
        atomic_write_json(destination, record)
        artifacts["scientific_validation"] = relative
        if tool == "MeDeCom":
            capability["validation_evidence"] = relative
        checksums[relative] = sha256_file(destination)

    common_set = set(common)
    fallback = common
    aric = payload["tool_capabilities"].get("ARIC", {}).get("artifacts", {}).get("reference")
    if aric:
        fallback_path = staging / aric
        if fallback_path.is_file():
            fallback = _array_reference_features(fallback_path) or common
    tool_features = {}
    for tool, capability in payload["tool_capabilities"].items():
        mapped_key = {
            "ARIC": "reference", "MethylCIBERSORT": "reference", "EpiDISH": "reference",
            "MethAtlas": "reference", "RefFreeEWAS": "reference", "PRmeth": "reference",
            "Tsisal": "reference",
        }.get(tool)
        relative = capability.get("artifacts", {}).get(mapped_key) if mapped_key else None
        if tool == "EDec" and capability.get("artifacts", {}).get("markers"):
            original = _r_array_reference_features(
                staging / capability["artifacts"]["markers"],
                "x<-readRDS(commandArgs(TRUE)[1]); cat(as.character(x),sep='\\n')",
                r_runtime_prefix,
            )
        elif tool == "EMeth" and capability.get("artifacts", {}).get("reference"):
            original = _r_array_reference_features(
                staging / capability["artifacts"]["reference"],
                "e<-new.env(); load(commandArgs(TRUE)[1],envir=e); cat(rownames(e$avg_data_matrix),sep='\\n')",
                r_runtime_prefix,
            )
        elif tool == "Houseman" and capability.get("artifacts", {}).get("beta_reference"):
            original = _r_array_reference_features(
                staging / capability["artifacts"]["beta_reference"],
                "x<-readRDS(commandArgs(TRUE)[1]); cat(rownames(x),sep='\\n')",
                r_runtime_prefix,
            )
        else:
            original = _array_reference_features(staging / relative) if relative else fallback
        original = original or fallback
        retained = [probe for probe in original if probe in common_set]
        feature_path = projection / "tool_features" / f"{tool}.tsv"
        feature_path.parent.mkdir(parents=True, exist_ok=True)
        with feature_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["probe_id", "cell_type_id"])
            writer.writerows((probe, "*") for probe in retained)
        minimum_fraction = 0.90 if tool in {"EpiDISH", "MethylCIBERSORT", "MEnet"} else 0.80
        minimum_count = 100 if tool == "MeDeCom" else 45000 if tool == "ARIC" else max(1, int(len(retained) * minimum_fraction))
        if tool == "MeDeCom":
            minimum_fraction = 0.0
        condition_change, correlation_change = _builder_projection_metrics(
            staging, payload, tool, common_path, r_runtime_prefix
        )
        qc = {
            "schema": "demethflow-reference-platform-projection-qc-v1",
            "status": "PASS" if retained else "FAIL",
            "tool": tool,
            "original_reference_feature_count": len(original),
            "common_reference_feature_count": len(retained),
            "lost_reference_feature_count": len(original) - len(retained),
            "reference_feature_retention_fraction": len(retained) / len(original) if original else 0.0,
            "per_cell_type_marker_retention": {
                cell["cell_type_id"]: {
                    "original_marker_count": len(original), "common_marker_count": len(retained),
                    "retention_fraction": len(retained) / len(original) if original else 0.0,
                }
                for cell in payload["cell_types"]
            },
            "reference_condition_number_change": condition_change,
            "cell_type_correlation_change": correlation_change,
            "scientific_promotion": "PROHIBITED_BY_THIS_STRUCTURAL_QC",
        }
        qc_path = projection / "tool_qc" / f"{tool}.json"
        atomic_write_json(qc_path, qc)
        capability.setdefault("artifacts", {})["platform_projection_qc"] = qc_path.relative_to(staging).as_posix()
        if capability.get("status") == "READY":
            capability["status"] = "RELEASED_UNVALIDATED"
            capability["reason"] = (
                "explicitly runnable without independent EPIC-v1 truth validation; "
                "no scientific accuracy claim is made"
            )
            capability["validation_scope"] = "released_without_independent_epic_v1_truth_validation"
        attach_pending_science(tool, capability)
        if tool == "EpiSCORE":
            capability["validation_policy"] = {
                "policy_id": "episcore-array-cross-platform-v2",
                "overall_mae_max": 0.10, "overall_pearson_min": 0.80,
                "maximum_cell_type_mae_max": 0.23, "valid_output_rate_min": 1.0,
                "missing_cell_types_max": 0, "repeat_max_absolute_difference_max": 1e-6,
                "independent_epic_validation_required": True,
            }
        tool_features[tool] = {
            "artifact": feature_path.relative_to(staging).as_posix(),
            "sha256": sha256_file(feature_path),
            "original_feature_count": len(original),
            "common_feature_count": len(retained),
            "minimum_input_overlap": minimum_count,
            "minimum_input_overlap_fraction": minimum_fraction,
            "minimum_per_cell_type_retention_fraction": minimum_fraction,
            "projection_qc": qc_path.relative_to(staging).as_posix(),
        }

    authorized_tools = sorted(
        tool for tool, item in payload["tool_capabilities"].items()
        if item.get("status") == "RELEASED_UNVALIDATED"
    )
    payload["status"] = "RELEASED_UNVALIDATED" if (
        authorized_tools and len(authorized_tools) == len(payload["tool_capabilities"])
    ) else "PARTIAL"
    payload["source_platform"] = "450k"
    payload["compatible_input_routes"] = ["epic"]
    payload["platform_projection"] = {
        "schema": PROJECTION_SCHEMA,
        "input_platform": "epic", "input_array_version": "EPIC_v1",
        "unsupported_input_array_versions": ["EPIC_v2"],
        "source_reference_platform": "450k",
        "marker_selection_from_user_data": False, "missing_cpg_imputation": False,
        "epic_manifest": {"artifact": epic_copy.relative_to(staging).as_posix(), "version": "EPIC_v1_hg38_frozen", "sha256": sha256_file(epic_copy)},
        "hm450_manifest": {"artifact": hm450_copy.relative_to(staging).as_posix(), "version": "HM450_hg38_frozen", "sha256": sha256_file(hm450_copy)},
        "common_cpgs": {"artifact": common_path.relative_to(staging).as_posix(), "sha256": sha256_file(common_path)},
        "common_cpg_count": 452999,
        "minimum_epic_v1_probe_count": 400000,
        "source_reference_manifest": {"artifact": source_manifest.relative_to(staging).as_posix(), "sha256": sha256_file(source_manifest)},
        "source_reference": {
            "reference_id": source_snapshot["reference_id"],
            "version": payload["version"],
            "selector": f"{source_snapshot['reference_id']}@{payload['version']}",
            "manifest_sha256": sha256_file(source_manifest),
        },
        "tool_features": tool_features,
    }
    payload["release_validation"] = {
        "stage": "formal_offline_release_without_independent_truth",
        "publishable": True,
        "execution_authorized_without_independent_truth": True,
        "independent_epic_v1_truth_available": False,
        "scientific_claims_permitted": False,
        "mandatory_runtime_warning": True,
        "exact_reference_selector_required": True,
        "explicit_analysis_contract_required": True,
        "cross_platform_opt_in_required": True,
        "authorized_tools": authorized_tools,
        "scientific_validation_status": "NOT_PERFORMED",
        "user_warning": (
            "This user-built EPIC-v1 input plus 450K common-CpG reference route was "
            "released without independent EPIC-v1 truth validation."
        ),
    }
    payload.setdefault("provenance", {}).update({
        "kind": "user_built_450k_reference_with_epic_v1_common_cpg_derivation",
        "cross_platform_route_is_not_native_epic_reference": True,
    })
    for path in projection.rglob("*"):
        if path.is_file():
            checksums[path.relative_to(staging).as_posix()] = sha256_file(path)


def _array_reference_features(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(handle, dialect=dialect)
            next(reader, None)
            return list(dict.fromkeys(
                row[0].strip().strip('"') for row in reader
                if row and row[0].strip().strip('"').startswith("cg")
            ))
    except (OSError, UnicodeError, csv.Error):
        return []


def _builder_projection_metrics(
    staging: Path,
    payload: dict,
    tool: str,
    common_path: Path,
    runtime_prefix: list[str] | None,
) -> tuple[dict, dict]:
    """Measure the user-built reference before/after frozen platform projection."""
    if tool == "MEnet":
        return (
            {
                "status": "NOT_APPLICABLE",
                "reason": "MEnet construction does not produce a trained regional prediction model",
            },
            {
                "status": "NOT_APPLICABLE",
                "reason": "no trained MEnet cell-prediction matrix exists in this user-built bundle",
            },
        )
    if tool == "MeDeCom":
        return (
            {"status": "NOT_APPLICABLE", "reason": "MeDeCom is reference-free"},
            {
                "status": "NOT_APPLICABLE",
                "reason": "MeDeCom components are anonymous and have no cell-reference columns",
            },
        )
    capability = payload["tool_capabilities"][tool]
    artifacts = capability.get("artifacts", {})
    if tool == "EpiSCORE":
        qc_path = staging / artifacts.get("qc_report", "")
        try:
            qc = json.loads(qc_path.read_text(encoding="utf-8"))
            condition = float(qc["condition_number"])
            matrix = qc["cell_correlation_matrix"]
            upper = [
                abs(float(matrix[row][column]))
                for row in range(len(matrix))
                for column in range(row + 1, len(matrix[row]))
                if math.isfinite(float(matrix[row][column]))
            ]
            maximum = max(upper) if upper else None
            rows = int(qc["gene_count"])
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DeMethFlowError(f"cannot measure user-built EpiSCORE projection QC: {exc}") from exc
        cells = [cell["cell_type_id"] for cell in payload["cell_types"]]
        return (
            {
                "status": "UNCHANGED_GENE_LEVEL_REFERENCE",
                "definition": "gene-level EpiSCORE reference is unchanged; EPIC input uses its EPIC CpG-to-gene map",
                "original_row_count": rows,
                "projected_row_count": rows,
                "before": condition,
                "after": condition,
                "after_to_before_ratio": 1.0,
            },
            {
                "status": "UNCHANGED_GENE_LEVEL_REFERENCE",
                "definition": "gene-level reference cell correlations are unchanged",
                "cell_type_order": cells,
                "maximum_absolute_pairwise_before": maximum,
                "maximum_absolute_pairwise_after": maximum,
                "maximum_absolute_pairwise_change": 0.0,
            },
        )
    if not runtime_prefix:
        raise DeMethFlowError(
            f"cross-platform derived construction requires a frozen R runtime to measure {tool} reference projection"
        )
    if tool == "EDec":
        path = staging / artifacts["tref"]
        loader = "x<-readRDS(args[1])"
        extra: list[str] = []
    elif tool == "EMeth":
        path = staging / artifacts["reference"]
        loader = "e<-new.env(); load(args[1],envir=e); x<-e$avg_data_matrix"
        extra = []
    elif tool == "Houseman":
        path = staging / artifacts["beta_reference"]
        cell_path = staging / artifacts["cell_reference"]
        loader = (
            "x0<-readRDS(args[1]); labels<-readRDS(args[3]); ids<-unique(as.character(labels)); "
            "x<-sapply(ids,function(id) rowMeans(x0[,labels==id,drop=FALSE])); colnames(x)<-ids"
        )
        extra = [str(cell_path)]
    else:
        artifact_key = {
            "ARIC": "reference",
            "MethylCIBERSORT": "reference",
            "EpiDISH": "reference",
            "MethAtlas": "reference",
            "RefFreeEWAS": "reference",
            "PRmeth": "reference",
            "Tsisal": "reference",
        }.get(tool)
        if not artifact_key or artifact_key not in artifacts:
            raise DeMethFlowError(f"no measurable projection reference is declared for {tool}")
        path = staging / artifacts[artifact_key]
        delimiter = _detect_delimiter(path)
        loader = (
            "x<-as.matrix(read.table(args[1],sep=args[3],header=TRUE,row.names=1,"
            "check.names=FALSE,quote='\\\"',comment.char='',stringsAsFactors=FALSE))"
        )
        extra = [delimiter]
    expression = r'''args<-commandArgs(TRUE)
%s
storage.mode(x)<-"double"
common<-scan(args[2],what="",skip=1,quiet=TRUE)
if(is.null(rownames(x)) || is.null(colnames(x))) stop("reference matrix needs row and column names")
summarize<-function(m,excluded){
  if(nrow(m)<2 || ncol(m)<2) stop("reference matrix needs at least two finite rows and two columns")
  d<-svd(m,nu=0,nv=0)$d
  cond<-if(length(d)==0 || min(d)<=0) NA_real_ else max(d)/min(d)
  cm<-cor(m,use="pairwise.complete.obs")
  upper<-abs(cm[upper.tri(cm)])
  c(rows=nrow(m),excluded=excluded,condition=cond,
    maxcorr=if(length(upper) && any(is.finite(upper))) max(upper[is.finite(upper)]) else NA_real_)
}
finite<-rowSums(!is.finite(x))==0
excluded.before<-sum(!finite)
x<-x[finite,,drop=FALSE]
before<-summarize(x,excluded.before)
projected<-x[rownames(x) %%in%% common,,drop=FALSE]
after<-summarize(projected,0)
delta<-abs(cor(projected,use="pairwise.complete.obs")-cor(x,use="pairwise.complete.obs"))
upperdelta<-delta[upper.tri(delta)]
maxdelta<-if(length(upperdelta) && any(is.finite(upperdelta))) max(upperdelta[is.finite(upperdelta)]) else 0
cat(paste(colnames(x),collapse="\t"),"\n",sep="")
cat(paste(format(before,digits=17,scientific=FALSE),collapse="\t"),"\n",sep="")
cat(paste(format(after,digits=17,scientific=FALSE),collapse="\t"),"\n",sep="")
cat(format(maxdelta,digits=17,scientific=FALSE),"\n",sep="")
''' % loader
    try:
        completed = subprocess.run(
            [*runtime_prefix, "-e", expression, str(path), str(common_path), *extra],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) != 4:
            raise ValueError(f"unexpected metric output: {completed.stdout!r}")
        cells = lines[0].split("\t")
        before = [float(value) for value in lines[1].split("\t")]
        after = [float(value) for value in lines[2].split("\t")]
        maximum_change = float(lines[3])
    except (subprocess.CalledProcessError, ValueError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise DeMethFlowError(f"cannot measure user-built {tool} projection QC: {detail}") from exc
    ratio = (
        after[2] / before[2]
        if before[2] > 0 and math.isfinite(before[2]) and math.isfinite(after[2])
        else None
    )
    return (
        {
            "status": "MEASURED",
            "definition": "spectral condition number of the finite cell-reference matrix",
            "original_row_count": int(before[0]),
            "projected_row_count": int(after[0]),
            "original_excluded_nonfinite_row_count": int(before[1]),
            "projected_excluded_nonfinite_row_count": int(after[1]),
            "before": before[2] if math.isfinite(before[2]) else None,
            "after": after[2] if math.isfinite(after[2]) else None,
            "after_to_before_ratio": ratio,
        },
        {
            "status": "MEASURED",
            "definition": "Pearson correlation between reference cell-type columns",
            "cell_type_order": cells,
            "maximum_absolute_pairwise_before": before[3] if math.isfinite(before[3]) else None,
            "maximum_absolute_pairwise_after": after[3] if math.isfinite(after[3]) else None,
            "maximum_absolute_pairwise_change": maximum_change,
        },
    )


def _detect_delimiter(path: Path) -> str:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t").delimiter
    except csv.Error as exc:
        raise DeMethFlowError(f"cannot determine reference delimiter: {path}") from exc


def _r_array_reference_features(
    path: Path, expression: str, runtime_prefix: list[str] | None
) -> list[str]:
    if not runtime_prefix:
        raise DeMethFlowError(
            f"cross-platform derived construction requires a frozen R runtime to inspect {path.name}"
        )
    try:
        completed = subprocess.run(
            [*runtime_prefix, "-e", expression, str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeMethFlowError(f"could not extract reference feature IDs from {path}: {exc}") from exc
    values = [value.strip() for value in completed.stdout.splitlines() if value.strip().startswith("cg")]
    if not values:
        raise DeMethFlowError(f"no CpG feature IDs were extracted from {path}")
    return list(dict.fromkeys(values))


def _bridge_build_command(command: list[str], bridge: Path) -> list[str]:
    source = PROJECT_ROOT / "nextflow_ref"
    bridge.mkdir(parents=True, exist_ok=True)
    for name in ("bin", "modules", "envs", "params"):
        child = source / name
        if child.is_dir():
            # These paths are referenced from inside the construction
            # container.  Copy the small workflow support tree so Apptainer
            # can see it without needing to discover a symlink target mount.
            shutil.copytree(child, bridge / name)
    for name in ("main.nf", "nextflow.config", "nextflow_schema.json"):
        child = source / name
        if child.is_file():
            shutil.copy2(child, bridge / name)
    source_workflow = (source / "main.nf").resolve()
    return [str(bridge / "main.nf") if Path(value).resolve() == source_workflow else value for value in command]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
