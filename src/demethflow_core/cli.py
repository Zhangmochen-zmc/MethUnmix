from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from . import PROJECT_ROOT, version
from .asset_manager import (
    audit_online,
    fetch_asset,
    gc_assets,
    import_asset,
    list_assets,
    plan_assets,
    verify_assets,
)
from .builder import BuildRequest, run_build
from .catalog import ReferenceCatalog
from .errors import DeMethFlowError
from .modules import ModuleStore
from .packaging import export_reference
from .runner import DeconRequest, prepare_decon, run_decon
from .runtime import doctor, result_as_dict
from .release import build_release
from .util import atomic_write_json, data_home_override
from .wgbs_bam import is_bam_input, validate_sort_memory


LEGACY_PIPELINES = {
    "450k": ("450k", "builtin.immune6.450k"),
    "epic": ("epic", "builtin.immune6.epic"),
    "wgbs_epic": ("wgbs", "builtin.immune6.wgbs-epic"),
    "wgbs": ("wgbs", "builtin.immune6.wgbs-native"),
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "home", None) is not None:
            home = args.home.expanduser()
            if not home.is_absolute():
                raise DeMethFlowError("--home must be an absolute path")
            with data_home_override(home.resolve()):
                return args.handler(args)
        return args.handler(args)
    except DeMethFlowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="methunmix", description="Standalone offline methylation deconvolution")
    parser.add_argument(
        "--home", type=Path,
        help="absolute user asset/store directory (also available as METHUNMIX_HOME); place before the subcommand",
    )
    parser.add_argument("--version", action="version", version=f"MethUnmix {version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Build a reference, deconvolve, or run both")
    _add_run_arguments(run)
    run.set_defaults(handler=_cmd_run)

    doctor_cmd = sub.add_parser("doctor", help="Validate only the dependencies required by one task")
    _add_common_selection(doctor_cmd)
    doctor_cmd.add_argument("--mode", choices=("decon", "build", "build_and_decon"), default="decon")
    doctor_cmd.add_argument("--input", type=Path)
    doctor_cmd.add_argument("--verify-reference-checksums", action="store_true")
    doctor_cmd.add_argument("--json", action="store_true")
    doctor_cmd.set_defaults(handler=_cmd_doctor)

    reference = sub.add_parser("reference", help="Inspect and manage local reference bundles")
    _add_home_option(reference)
    reference_sub = reference.add_subparsers(dest="reference_command", required=True)
    ref_list = reference_sub.add_parser("list")
    _add_home_option(ref_list)
    ref_list.add_argument("--scenario")
    ref_list.add_argument("--platform", choices=("450k", "epic", "wgbs"))
    ref_list.add_argument("--genome-build", choices=("hg19", "hg38"))
    ref_list.add_argument("--reference-root", type=Path)
    ref_list.add_argument("--json", action="store_true")
    ref_list.set_defaults(handler=_cmd_reference_list)
    ref_show = reference_sub.add_parser("show")
    _add_home_option(ref_show)
    ref_show.add_argument("reference")
    ref_show.add_argument("--reference-root", type=Path)
    ref_show.set_defaults(handler=_cmd_reference_show)
    ref_validate = reference_sub.add_parser("validate")
    _add_home_option(ref_validate)
    ref_validate.add_argument("reference", nargs="?")
    ref_validate.add_argument("--reference-root", type=Path)
    ref_validate.add_argument("--checksums", action="store_true")
    ref_validate.set_defaults(handler=_cmd_reference_validate)
    ref_install = reference_sub.add_parser("install")
    _add_home_option(ref_install)
    ref_install.add_argument("archive", type=Path)
    ref_install.add_argument("--install-root", type=Path)
    ref_install.set_defaults(handler=_cmd_module_install)
    ref_export = reference_sub.add_parser("export")
    _add_home_option(ref_export)
    ref_export.add_argument("reference")
    ref_export.add_argument("destination", type=Path)
    ref_export.add_argument("--reference-root", type=Path)
    ref_export.set_defaults(handler=_cmd_reference_export)

    module = sub.add_parser("module", help="Install and verify local offline modules")
    _add_home_option(module)
    module_sub = module.add_subparsers(dest="module_command", required=True)
    module_list = module_sub.add_parser("list")
    _add_home_option(module_list)
    module_list.add_argument("--install-root", type=Path)
    module_list.add_argument("--json", action="store_true")
    module_list.set_defaults(handler=_cmd_module_list)
    module_install = module_sub.add_parser("install")
    _add_home_option(module_install)
    module_install.add_argument("archive", type=Path)
    module_install.add_argument("--install-root", type=Path)
    module_install.set_defaults(handler=_cmd_module_install)
    module_verify = module_sub.add_parser("verify")
    _add_home_option(module_verify)
    module_verify.add_argument("module", nargs="?")
    module_verify.add_argument("--install-root", type=Path)
    module_verify.set_defaults(handler=_cmd_module_verify)

    asset = sub.add_parser("asset", help="Explicitly manage external assets from the static catalog")
    _add_home_option(asset)
    asset_sub = asset.add_subparsers(dest="asset_command", required=True)
    asset_list = asset_sub.add_parser("list", help="List catalog targets and installed modules (offline)")
    _add_home_option(asset_list)
    asset_list.add_argument("--json", action="store_true")
    asset_list.set_defaults(handler=_cmd_asset_list)
    asset_plan = asset_sub.add_parser("plan", help="Show the external asset plan (offline)")
    _add_home_option(asset_plan)
    asset_plan.add_argument("--json", action="store_true")
    asset_plan.set_defaults(handler=_cmd_asset_plan)
    asset_verify = asset_sub.add_parser("verify", help="Verify installed module payloads (offline)")
    _add_home_option(asset_verify)
    asset_verify.add_argument("module", nargs="?")
    asset_verify.add_argument("--json", action="store_true")
    asset_verify.set_defaults(handler=_cmd_asset_verify)
    asset_import = asset_sub.add_parser("import", help="Import a user-owned offline module archive")
    _add_home_option(asset_import)
    asset_import.add_argument("archive", type=Path)
    asset_import.set_defaults(handler=_cmd_asset_import)
    asset_fetch = asset_sub.add_parser("fetch", help="Fetch one explicitly approved static-catalog target")
    _add_home_option(asset_fetch)
    asset_fetch.add_argument("asset_id")
    asset_fetch.add_argument("--metadata-dir", type=Path)
    asset_fetch.set_defaults(handler=_cmd_asset_fetch)
    asset_audit = asset_sub.add_parser("audit", help="Audit an immutable static catalog over HTTPS")
    _add_home_option(asset_audit)
    asset_audit.add_argument("--online", action="store_true", required=True)
    asset_audit.add_argument(
        "--catalog-url", "--base-url", dest="catalog_url", required=True,
        help="exact HTTPS URL of a versioned immutable catalog.json (TUF signatures are deferred)",
    )
    asset_audit.add_argument("--metadata-dir", type=Path)
    asset_audit.set_defaults(handler=_cmd_asset_audit)
    asset_gc = asset_sub.add_parser("gc", help="List or remove download cache entries")
    _add_home_option(asset_gc)
    asset_gc.add_argument("--dry-run", action="store_true")
    asset_gc.set_defaults(handler=_cmd_asset_gc)

    release = sub.add_parser("release", help="Build relocatable offline release archives")
    _add_home_option(release)
    release_sub = release.add_subparsers(dest="release_command", required=True)
    release_build = release_sub.add_parser("build")
    _add_home_option(release_build)
    release_build.add_argument("--dist", type=Path, required=True)
    release_build.add_argument("--reference", action="append", default=[])
    release_build.add_argument("--runtime-module", action="append", default=[])
    release_build.add_argument("--data-module", action="append", default=[])
    release_build.add_argument("--include-build", action="store_true")
    release_build.add_argument("--full-offline", action="store_true")
    release_build.add_argument("--reference-root", type=Path)
    release_build.set_defaults(handler=_cmd_release_build)
    return parser


def _add_common_selection(parser: argparse.ArgumentParser) -> None:
    _add_home_option(parser)
    parser.add_argument("--scenario")
    parser.add_argument("--platform", choices=("450k", "epic", "wgbs"))
    parser.add_argument(
        "--genome-build",
        choices=("hg19", "hg38"),
        help="native WGBS reference genome (default: hg38); invalid for array inputs",
    )
    parser.add_argument("--reference")
    parser.add_argument("--tools", default="all")
    parser.add_argument(
        "--validation-candidate",
        action="store_true",
        help="restricted: run only an explicitly marked quarantined validation candidate",
    )
    parser.add_argument("--reference-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument(
        "--external-runtime-root", type=Path,
        help="explicit user-installed third-party runtime tree (contains methunmix-external-runtime.json)",
    )
    parser.add_argument("--runtime", choices=("container", "conda", "singularity"), default="container")
    parser.add_argument(
        "--container-engine",
        metavar="ENGINE_OR_PATH",
        default="auto",
        help="auto, apptainer, singularity, or an absolute executable path ending in apptainer/singularity",
    )
    parser.add_argument("--executor", choices=("local",), default="local")
    parser.add_argument("--accelerator", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument(
        "--gpu-count", type=int, default=1,
        help="number of logical GPUs to request for GPU tasks (MEnet currently uses one)",
    )
    parser.add_argument("--allow-cross-platform", action="store_true")
    parser.add_argument(
        "--analysis-contract",
        choices=(
            "array_450k_cpg",
            "array_epic_cpg",
            "array_epic_from_450k_common_cpg_v1",
            "wgbs_derived_epic_cpg_v1",
            "wgbs_derived_450k_cpg_v1",
            "wgbs_native_hg19",
            "wgbs_native_hg38",
        ),
        help="explicit analysis contract; required for every cross-platform projection",
    )
    parser.add_argument("--medecom-k", type=int, help="MeDeCom component count; defaults to the scenario recommendation")
    parser.add_argument("--medecom-lambda", default="auto", help="MeDeCom lambda: auto or a non-negative number")
    parser.add_argument("--medecom-nfolds", type=int, help="MeDeCom CV folds; defaults to min(10, cohort sample count)")
    parser.add_argument(
        "--methylcibersort-permutations", type=int, default=1000,
        help="CIBERSORT empirical P-value permutations (default: 1000; 0 disables empirical P-values)",
    )
    parser.add_argument("--pipeline", choices=tuple(LEGACY_PIPELINES), help=argparse.SUPPRESS)


def _add_home_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home", type=Path,
        default=argparse.SUPPRESS,
        help="absolute user asset/store directory (also available as METHUNMIX_HOME)",
    )


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    _add_common_selection(parser)
    parser.add_argument("--mode", choices=("decon", "build", "build_and_decon"), default="decon")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--reference-input", type=Path)
    parser.add_argument("--uxm-pat-root", type=Path)
    parser.add_argument("--methylbert-reads-root", type=Path)
    parser.add_argument("--celfeer-pat-root", type=Path)
    parser.add_argument("--celfeer-input-root", type=Path)
    parser.add_argument("--celfeer-metadata", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--reference-id")
    parser.add_argument("--reference-version")
    parser.add_argument("--build-tools", default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-reference-checksums", action="store_true")
    parser.add_argument("--random-seed", type=int, default=20260826)
    parser.add_argument(
        "--preprocess-threads", type=int, default=4,
        help="threads for pinned BAM-to-PAT/BED materialisation (default: 4)",
    )
    parser.add_argument(
        "--preprocess-sort-memory", default="768M",
        help="per-thread samtools sort memory for BAM materialisation (positive integer plus K/M/G; default: 768M)",
    )
    parser.add_argument(
        "--bam-contig-policy", choices=("strict", "primary-only"), default="strict",
        help="BAM header policy: strict exact dictionary, or explicitly stage complete-GRCh38 extra contigs away",
    )


def _legacy(args: argparse.Namespace) -> None:
    if args.runtime == "singularity":
        args.runtime = "container"
        if args.container_engine == "auto":
            args.container_engine = "singularity"
    if not args.pipeline:
        return
    platform, reference = LEGACY_PIPELINES[args.pipeline]
    if args.platform and args.platform != platform:
        raise DeMethFlowError(f"--pipeline {args.pipeline} conflicts with --platform {args.platform}")
    if args.reference and args.reference != reference:
        raise DeMethFlowError(f"--pipeline {args.pipeline} conflicts with --reference {args.reference}")
    args.platform = platform
    args.reference = reference
    print(
        f"WARNING: --pipeline {args.pipeline} is a compatibility alias for {reference}; "
        "use --scenario/--platform or --reference for new runs.",
        file=sys.stderr,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    _legacy(args)
    if not args.platform:
        raise DeMethFlowError("--platform is required")
    if args.gpu_count < 1:
        raise DeMethFlowError("--gpu-count must be a positive integer")
    if args.accelerator == "cpu" and args.gpu_count != 1:
        raise DeMethFlowError("--gpu-count is only configurable in GPU mode")
    if (
        args.analysis_contract == "array_epic_from_450k_common_cpg_v1"
        and not args.allow_cross_platform
    ):
        raise DeMethFlowError(
            "--allow-cross-platform is required for array_epic_from_450k_common_cpg_v1"
        )
    if (
        args.mode == "decon"
        and args.analysis_contract == "array_epic_from_450k_common_cpg_v1"
        and (not args.reference or "@" not in args.reference)
    ):
        raise DeMethFlowError(
            "an exact --reference REFERENCE_ID@VERSION is required for "
            "array_epic_from_450k_common_cpg_v1"
        )
    requested_genome_build = args.genome_build
    if args.input and args.platform == "wgbs" and is_bam_input(args.input) and requested_genome_build is None:
        raise DeMethFlowError("BAM input requires explicit --genome-build hg19 or hg38")
    if args.preprocess_threads < 1:
        raise DeMethFlowError("--preprocess-threads must be a positive integer")
    validate_sort_memory(args.preprocess_sort_memory)
    if (
        args.analysis_contract in {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}
        and requested_genome_build is None
    ):
        raise DeMethFlowError("WGBS-derived array projection requires explicit --genome-build hg19 or hg38")
    if (
        args.analysis_contract in {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}
        and not args.scenario
    ):
        raise DeMethFlowError("WGBS-derived array projection requires explicit --scenario")
    _normalize_genome_build(args)
    outdir = args.outdir.expanduser().resolve()
    built_manifest: Path | None = None
    if args.mode in {"build", "build_and_decon"}:
        if args.mode == "build_and_decon" and not args.input:
            raise DeMethFlowError("--input is required for build_and_decon")
        for name in ("reference_id", "reference_version", "scenario"):
            if not getattr(args, name):
                raise DeMethFlowError(f"--{name.replace('_', '-')} is required for {args.mode}")
        build_tool_names = {value.strip().lower() for value in args.build_tools.split(",") if value.strip()}
        if not args.metadata and build_tool_names != {"celfeer"}:
            raise DeMethFlowError("--metadata is required unless CelFEER is the only construction tool")
        build_request = BuildRequest(
            reference_input=args.reference_input,
            uxm_pat_root=args.uxm_pat_root,
            methylbert_reads_root=args.methylbert_reads_root,
            celfeer_pat_root=args.celfeer_pat_root,
            celfeer_input_root=args.celfeer_input_root,
            celfeer_metadata=args.celfeer_metadata,
            metadata=args.metadata,
            outdir=outdir,
            scenario=args.scenario,
            platform=args.platform,
            reference_id=args.reference_id,
            reference_version=args.reference_version,
            build_tools=args.build_tools,
            analysis_contract=args.analysis_contract,
            genome_build=args.genome_build,
            requested_genome_build=requested_genome_build,
            reference_root=args.reference_root,
            runtime_root=args.runtime_root,
            container_engine=args.container_engine,
            executor=args.executor,
            accelerator=args.accelerator,
            dry_run=args.dry_run,
            resume=args.resume,
        )
        build_manifest, built_manifest = run_build(build_request)
        if args.dry_run and args.mode == "build_and_decon":
            build_manifest["planned_deconvolution"] = {
                "reference": f"{args.reference_id}@{args.reference_version}",
                "reference_root": str(
                    args.reference_root.expanduser().resolve()
                    if args.reference_root else "<METHUNMIX_HOME>/references"
                ),
                "input": str(args.input.expanduser().resolve()) if args.input else None,
                "platform": args.platform,
                "analysis_contract": build_manifest["analysis_contract"],
                "genome_build_requested": requested_genome_build,
                "genome_build_defaulted": build_manifest["genome_build_defaulted"],
                "genome_build": build_manifest["genome_build"],
                "same_newly_built_bundle_required": True,
            }
            atomic_write_json(outdir / "build_run_manifest.json", build_manifest)
        if args.mode == "build" or args.dry_run:
            print(json.dumps(build_manifest, ensure_ascii=False, indent=2))
            return 0
    if not args.input:
        raise DeMethFlowError(f"--input is required for {args.mode}")
    reference = args.reference
    reference_root = args.reference_root
    decon_outdir = outdir
    if built_manifest:
        reference = f"{args.reference_id}@{args.reference_version}"
        reference_root = built_manifest.parent.parent.parent
        decon_outdir = outdir / "deconvolution"
    request = DeconRequest(
        input_path=args.input,
        outdir=decon_outdir,
        platform=args.platform,
        scenario=args.scenario,
        reference=reference,
        tools=args.tools,
        runtime=args.runtime,
        container_engine=args.container_engine,
        executor=args.executor,
        accelerator=args.accelerator,
        reference_root=reference_root,
        runtime_root=args.runtime_root,
        external_runtime_root=args.external_runtime_root,
        allow_cross_platform=args.allow_cross_platform,
        resume=args.resume,
        dry_run=args.dry_run,
        verify_reference_checksums=args.verify_reference_checksums,
        random_seed=args.random_seed,
        medecom_k=args.medecom_k,
        medecom_lambda=args.medecom_lambda,
        medecom_nfolds=args.medecom_nfolds,
        methylcibersort_permutations=args.methylcibersort_permutations,
        genome_build=args.genome_build,
        requested_genome_build=requested_genome_build,
        analysis_contract=args.analysis_contract,
        validation_candidate=args.validation_candidate,
        preprocess_threads=args.preprocess_threads,
        preprocess_sort_memory=args.preprocess_sort_memory,
        bam_contig_policy=args.bam_contig_policy,
    )
    manifest = run_decon(request)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    _legacy(args)
    if (
        args.analysis_contract in {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}
        and args.genome_build is None
    ):
        raise DeMethFlowError("WGBS-derived array projection requires explicit --genome-build hg19 or hg38")
    if (
        args.analysis_contract in {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}
        and not args.scenario
    ):
        raise DeMethFlowError("WGBS-derived array projection requires explicit --scenario")
    if args.mode == "decon" and args.analysis_contract == "array_epic_from_450k_common_cpg_v1":
        if not args.allow_cross_platform:
            raise DeMethFlowError(
                "--allow-cross-platform is required for array_epic_from_450k_common_cpg_v1"
            )
        if not args.reference or "@" not in args.reference:
            raise DeMethFlowError(
                "an exact --reference REFERENCE_ID@VERSION is required for "
                "array_epic_from_450k_common_cpg_v1"
            )
    if args.platform:
        _normalize_genome_build(args)
    result = doctor(
        mode=args.mode,
        reference=args.reference,
        scenario=args.scenario,
        platform_name=args.platform,
        genome_build=args.genome_build,
        tools=args.tools,
        input_path=args.input,
        reference_root=args.reference_root,
        runtime_root=args.runtime_root,
        external_runtime_root=args.external_runtime_root,
        runtime=args.runtime,
        container_engine=args.container_engine,
        allow_cross_platform=args.allow_cross_platform,
        analysis_contract=args.analysis_contract,
        accelerator=args.accelerator,
        verify_reference_checksums=args.verify_reference_checksums,
        medecom_k=args.medecom_k,
        medecom_lambda=args.medecom_lambda,
        medecom_nfolds=args.medecom_nfolds,
        methylcibersort_permutations=args.methylcibersort_permutations,
        allow_validation_candidate=args.validation_candidate,
    )
    if args.json:
        print(json.dumps(result_as_dict(result), ensure_ascii=False, indent=2))
    else:
        for check in result.checks:
            print(f"{'OK' if check.ok else 'FAIL'}\t{check.name}\t{check.detail}")
    return 0 if result.ok else 2


def _normalize_genome_build(args: argparse.Namespace) -> None:
    """Apply the public build policy without changing cross-platform legacy routes."""
    if args.platform in {"450k", "epic"}:
        if args.genome_build is not None:
            raise DeMethFlowError("--genome-build is valid only for native WGBS input")
        return
    cross_platform = bool(
        args.allow_cross_platform
        or getattr(args, "pipeline", None) == "wgbs_epic"
        or (args.reference and "wgbs-epic" in args.reference)
        or getattr(args, "analysis_contract", None) in {
            "array_450k_cpg", "array_epic_cpg", "array_epic_from_450k_common_cpg_v1",
            "wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1",
        }
    )
    if cross_platform and getattr(args, "analysis_contract", None) not in {
        "wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1",
    }:
        if args.genome_build is not None:
            raise DeMethFlowError("--genome-build cannot be combined with a cross-platform array contract")
        return
    args.genome_build = args.genome_build or "hg38"


def _catalog(args: argparse.Namespace, verify: bool = False) -> ReferenceCatalog:
    return ReferenceCatalog.discover(args.reference_root).scan(verify_files=verify)


def _cmd_reference_list(args: argparse.Namespace) -> int:
    if args.platform in {"450k", "epic"} and args.genome_build is not None:
        raise DeMethFlowError("--genome-build is valid only for native WGBS references")
    catalog = _catalog(args)
    bundles = [
        bundle for bundle in catalog.bundles
        if (not args.scenario or bundle.scenario_id == args.scenario)
        and (not args.platform or args.platform in bundle.compatible_routes)
        and (not args.genome_build or bundle.payload["genome_build"] == args.genome_build)
    ]
    payload = [
        {
            "reference": bundle.selector,
            "scenario": bundle.scenario_id,
            "source_platform": bundle.payload["source_platform"],
            "analysis_contract": bundle.contract,
            "genome_build": bundle.payload["genome_build"],
            "routes": list(bundle.compatible_routes),
            "status": bundle.status,
            "cell_types": len(bundle.cell_types),
            "ready_tools": [name for name, cap in bundle.tools.items() if cap.status == "READY"],
            "released_unvalidated_tools": [
                name for name, cap in bundle.tools.items()
                if cap.status == "RELEASED_UNVALIDATED"
            ],
            "ru_default_tools": [
                name for name, cap in bundle.tools.items()
                if cap.status == "RELEASED_UNVALIDATED" and cap.execution_policy == "normal"
            ],
            "explicit_only_tools": [
                name for name, cap in bundle.tools.items()
                if cap.execution_policy == "explicit_only"
            ],
        }
        for bundle in bundles
    ]
    if args.json:
        print(json.dumps({"references": payload, "problems": [item.__dict__ for item in catalog.problems]}, ensure_ascii=False, indent=2))
    else:
        print(
            "REFERENCE\tSCENARIO\tSOURCE\tCONTRACT\tSTATUS\tCELLS\t"
            "READY_TOOLS\tRELEASED_UNVALIDATED_TOOLS\tRU_DEFAULT_TOOLS\tEXPLICIT_ONLY_TOOLS"
        )
        for item in payload:
            print(
                f"{item['reference']}\t{item['scenario']}\t{item['source_platform']}\t"
                f"{item['analysis_contract']}\t{item['status']}\t{item['cell_types']}\t"
                f"{','.join(item['ready_tools'])}\t"
                f"{','.join(item['released_unvalidated_tools'])}\t"
                f"{','.join(item['ru_default_tools'])}\t"
                f"{','.join(item['explicit_only_tools'])}"
            )
        for problem in catalog.problems:
            print(f"INVALID\t{problem.path}\t{problem.error}", file=sys.stderr)
    return 0 if not catalog.problems else 2


def _select_exact(catalog: ReferenceCatalog, selector: str):
    matches = [bundle for bundle in catalog.bundles if bundle.selector == selector or bundle.reference_id == selector]
    if len(matches) != 1:
        raise DeMethFlowError(f"Expected exactly one installed reference for {selector!r}; found {len(matches)}")
    return matches[0]


def _cmd_reference_show(args: argparse.Namespace) -> int:
    bundle = _select_exact(_catalog(args), args.reference)
    print(json.dumps(bundle.payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_reference_validate(args: argparse.Namespace) -> int:
    catalog = _catalog(args, verify=args.checksums)
    bundles = catalog.bundles
    if args.reference:
        bundles = [_select_exact(catalog, args.reference)]
    for bundle in bundles:
        print(f"OK\t{bundle.selector}\t{bundle.manifest_path}")
    for problem in catalog.problems:
        print(f"FAIL\t{problem.path}\t{problem.error}")
    return 0 if not catalog.problems else 2


def _cmd_reference_export(args: argparse.Namespace) -> int:
    bundle = _select_exact(_catalog(args), args.reference)
    archive, sidecar = export_reference(bundle, args.destination)
    print(archive)
    print(sidecar)
    return 0


def _cmd_module_list(args: argparse.Namespace) -> int:
    modules = ModuleStore(args.install_root).list()
    payload = [module.payload for module in modules]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("MODULE\tTYPE\tINSTALLED_PATH")
        for module in modules:
            print(f"{module.selector}\t{module.payload.get('module_type')}\t{module.install_root}")
    return 0


def _cmd_module_install(args: argparse.Namespace) -> int:
    module, changed = ModuleStore(args.install_root).install(args.archive)
    print(f"{'INSTALLED' if changed else 'ALREADY_INSTALLED'}\t{module.selector}\t{module.install_root}")
    return 0


def _cmd_module_verify(args: argparse.Namespace) -> int:
    store = ModuleStore(args.install_root)
    modules = store.list()
    if args.module:
        modules = [module for module in modules if module.selector == args.module or module.module_id == args.module]
        if not modules:
            raise DeMethFlowError(f"Installed module not found: {args.module}")
    failed = False
    for module in modules:
        errors = store.verify(module)
        if errors:
            failed = True
            for error in errors:
                print(f"FAIL\t{module.selector}\t{error}")
        else:
            print(f"OK\t{module.selector}")
    return 2 if failed else 0


def _cmd_asset_list(args: argparse.Namespace) -> int:
    print(list_assets(json_output=args.json))
    return 0


def _cmd_asset_plan(args: argparse.Namespace) -> int:
    print(plan_assets(json_output=args.json))
    return 0


def _cmd_asset_verify(args: argparse.Namespace) -> int:
    return verify_assets(module_id=args.module, json_output=args.json)


def _cmd_asset_import(args: argparse.Namespace) -> int:
    return import_asset(args.archive)


def _cmd_asset_fetch(args: argparse.Namespace) -> int:
    return fetch_asset(args.asset_id, metadata_dir=args.metadata_dir)


def _cmd_asset_audit(args: argparse.Namespace) -> int:
    return audit_online(args.catalog_url, metadata_dir=args.metadata_dir)


def _cmd_asset_gc(args: argparse.Namespace) -> int:
    return gc_assets(dry_run=args.dry_run)


def _cmd_release_build(args: argparse.Namespace) -> int:
    catalog = ReferenceCatalog.discover(args.reference_root).scan()
    bundles = [_select_exact(catalog, selector) for selector in args.reference]
    index = build_release(
        args.dist,
        bundles=bundles,
        runtimes=args.runtime_module,
        data_modules=args.data_module,
        include_build=args.include_build,
        include_full_bundle=args.full_offline,
    )
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0
