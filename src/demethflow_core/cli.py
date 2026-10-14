from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from . import PROJECT_ROOT, version
from .asset_manager import (
    audit_online,
    fetch_asset,
    fetch_reference,
    gc_assets,
    import_asset,
    list_reference_catalog,
    list_assets,
    plan_assets,
    verify_assets,
)
from .asset_lifecycle import (
    compare_catalog_plans,
    create_catalog_plan,
    import_catalog_plan,
    install_catalog_plan,
    preflight_catalog_plan,
    repair_catalog_plan,
    render_catalog_plan_summary,
    verify_catalog_plan,
)
from .builder import BuildRequest, run_build
from .benchmark import benchmark
from .adapter_contract import registry_report
from .catalog import ReferenceCatalog
from .errors import DeMethFlowError
from .modules import ModuleStore
from .offline_assets import (
    apply_lock_to_selection,
    compare_upgrade_locks,
    create_bundle,
    create_plan,
    install_lock,
    load_lock,
    remove_module,
    repair_lock,
    verify_lock,
    verify_locked_external_runtimes,
    verify_locked_reference,
)
from .packaging import export_reference
from .runner import DeconRequest, prepare_decon, run_decon
from .runtime import doctor, result_as_dict
from .release import build_release
from .reporting import build_report, prepare_replay
from .execution_lifecycle import inspect_resources, prepare_recovery_plan
from .fresh_retry import execute_fresh_retry, validate_fresh_retry
from .safe_replay import execute_safe_replay, validate_safe_replay
from .scientific_evidence import add_review, audit as audit_scientific_evidence, import_record, initialize as initialize_scientific_evidence, query as query_scientific_evidence
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
    parser.add_argument("--version", action="version", version=f"MethUnMix {version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Build a reference, deconvolve, or run both")
    _add_run_arguments(run)
    run.set_defaults(handler=_cmd_run)

    doctor_cmd = sub.add_parser("doctor", help="Validate only the dependencies required by one task")
    _add_common_selection(doctor_cmd)
    doctor_cmd.add_argument("--mode", choices=("decon", "build", "build_and_decon"), default="decon")
    doctor_cmd.add_argument("--input", type=Path)
    doctor_cmd.add_argument("--verify-reference-checksums", action="store_true")
    doctor_cmd.add_argument("--check-mode", choices=("quick", "deep"), default="quick")
    doctor_cmd.add_argument("--json", action="store_true")
    doctor_cmd.set_defaults(handler=_cmd_doctor)

    execution_plan_cmd = sub.add_parser("plan", help="Explain one analysis selection and its execution readiness without running it")
    _add_common_selection(execution_plan_cmd)
    execution_plan_cmd.add_argument("--mode", choices=("decon",), default="decon")
    execution_plan_cmd.add_argument("--input", type=Path)
    execution_plan_cmd.add_argument("--outdir", type=Path, help="optional intended output directory; observational only")
    execution_plan_cmd.add_argument("--verify-reference-checksums", action="store_true")
    execution_plan_cmd.add_argument("--check-mode", choices=("quick", "deep"), default="quick")
    execution_plan_cmd.add_argument("--json", action="store_true")
    execution_plan_cmd.set_defaults(handler=_cmd_execution_plan)

    report_cmd = sub.add_parser("report", help="Create a derived machine-readable and offline HTML report from an existing run directory")
    report_cmd.add_argument("--run-dir", type=Path, required=True, help="existing run directory; source records are read-only")
    report_cmd.add_argument("--output-dir", type=Path, required=True, help="new directory for derived report.json and report.html")
    report_cmd.add_argument("--truth", type=Path, help="optional named-cell truth matrix for descriptive metrics only")
    report_cmd.add_argument("--tool", help="tool whose declared named-cell output is compared to --truth")
    report_cmd.add_argument("--variant", help="required for a multi-variant tool such as EMeth")
    report_cmd.add_argument("--json", action="store_true", help="print the derived machine report after writing it")
    report_cmd.set_defaults(handler=_cmd_report)

    replay_cmd = sub.add_parser("replay-plan", help="Prepare, but never execute, a controlled rerun plan from a prior manifest")
    replay_cmd.add_argument("--manifest", type=Path, required=True, help="existing run_manifest.json; untrusted command strings are ignored")
    replay_cmd.add_argument("--outdir", type=Path, required=True, help="new output directory required for any future run")
    replay_cmd.add_argument("--input", type=Path, help="current input location; may differ from historical path")
    replay_cmd.add_argument("--verify-input-identity", action="store_true", help="opt in to full SHA256 verification for a file input")
    replay_cmd.add_argument("--output", type=Path, help="optional JSON output path; defaults to stdout")
    replay_cmd.set_defaults(handler=_cmd_replay_plan)

    replay_execute_cmd = sub.add_parser(
        "replay-execute",
        help="Preview or explicitly execute one strict, new-directory replay from a prepared replay plan",
    )
    replay_execute_cmd.add_argument("--replay-plan", type=Path, required=True, help="prepared replay-plan JSON; command strings inside it are ignored")
    replay_execute_cmd.add_argument("--source-manifest", type=Path, required=True, help="source run_manifest.json bound by --replay-plan")
    replay_execute_cmd.add_argument("--input", type=Path, required=True, help="current input location; full content identity is rechecked")
    replay_execute_cmd.add_argument("--outdir", type=Path, required=True, help="new, currently absent output directory")
    replay_execute_cmd.add_argument("--reference-root", type=Path, required=True, help="current reference root; content is revalidated")
    replay_execute_cmd.add_argument("--runtime-root", type=Path, required=True, help="current runtime root; image identities are revalidated")
    replay_execute_cmd.add_argument("--external-runtime-root", type=Path, help="current explicit user-supplied runtime tree, when required")
    replay_execute_cmd.add_argument("--catalog-metadata-dir", type=Path, help="current audited catalog metadata required when the source used local dependency bindings")
    replay_execute_cmd.add_argument("--local-dependency-lock", type=Path, help="current exact local dependency lock required when the source used local bindings")
    replay_execute_cmd.add_argument("--local-dependency-locators", type=Path, help="current non-portable locator overlay required when the source used local bindings")
    replay_execute_cmd.add_argument("--workflow-timeout-seconds", type=int, required=True, help="explicit complete-workflow hard limit; never inherited from the plan")
    replay_execute_cmd.add_argument("--report-output-dir", type=Path, help="optional new derived-report directory; failure never rewrites workflow exit facts")
    replay_execute_cmd.add_argument("--execute", action="store_true", help="required to claim the new directory and invoke the public runner")
    replay_execute_cmd.add_argument("--output", type=Path, help="optional preview/result JSON output path")
    replay_execute_cmd.set_defaults(handler=_cmd_replay_execute)

    resource_cmd = sub.add_parser("resource-preflight", help="Record bounded local resource observations without reserving or executing work")
    resource_cmd.add_argument("--outdir", type=Path, required=True)
    resource_cmd.add_argument("--tmpdir", type=Path, required=True)
    resource_cmd.add_argument("--cache-dir", type=Path, required=True)
    resource_cmd.add_argument("--known-required-bytes", type=int)
    resource_cmd.add_argument("--cpus", type=int, default=1)
    resource_cmd.add_argument("--concurrency", type=int, default=1)
    resource_cmd.add_argument("--output", type=Path)
    resource_cmd.set_defaults(handler=_cmd_resource_preflight)

    recovery_cmd = sub.add_parser("recovery-plan", help="Prepare an identity-aware recovery decision; never executes or passes -resume")
    recovery_cmd.add_argument("--manifest", type=Path, required=True)
    recovery_cmd.add_argument("--outdir", type=Path, required=True)
    recovery_cmd.add_argument("--output", type=Path)
    recovery_cmd.set_defaults(handler=_cmd_recovery_plan)

    fresh_retry_cmd = sub.add_parser(
        "retry-failed-tool",
        help="Preview or explicitly execute one new-directory retry of one terminally failed tool",
    )
    fresh_retry_cmd.add_argument("--source-run-dir", type=Path, required=True)
    fresh_retry_cmd.add_argument("--tool", required=True)
    fresh_retry_cmd.add_argument("--outdir", type=Path, required=True)
    fresh_retry_cmd.add_argument("--request-json", type=Path, required=True, help="structured current request; historical command strings are ignored")
    fresh_retry_cmd.add_argument("--catalog-metadata-dir", type=Path, help="current audited catalog metadata required when the source used local dependency bindings")
    fresh_retry_cmd.add_argument("--local-dependency-lock", type=Path, help="current exact local dependency lock required when the source used local bindings")
    fresh_retry_cmd.add_argument("--local-dependency-locators", type=Path, help="current non-portable locator overlay required when the source used local bindings")
    fresh_retry_cmd.add_argument("--execute", action="store_true", help="required to create the new output and call the public runner")
    fresh_retry_cmd.add_argument("--output", type=Path, help="optional preview/result record path")
    fresh_retry_cmd.set_defaults(handler=_cmd_retry_failed_tool)

    benchmark_cmd = sub.add_parser(
        "benchmark",
        help="Calculate descriptive metrics for user-supplied named-cell truth",
    )
    benchmark_cmd.add_argument("--estimate", type=Path, required=True, help="canonical named cell-proportion CSV/TSV")
    benchmark_cmd.add_argument("--truth", type=Path, required=True, help="truth-proportion CSV/TSV with the same cell types")
    benchmark_cmd.add_argument("--output", type=Path, help="optional JSON report path")
    benchmark_cmd.add_argument("--tool")
    benchmark_cmd.add_argument("--adapter")
    benchmark_cmd.add_argument("--profile")
    benchmark_cmd.add_argument("--scenario")
    benchmark_cmd.add_argument("--platform")
    benchmark_cmd.add_argument("--genome-build")
    benchmark_cmd.add_argument("--analysis-contract")
    benchmark_cmd.add_argument("--variant", help="record an explicitly selected output variant; does not create a repeat")
    benchmark_cmd.add_argument("--policy", type=Path, help="optional exact-scope policy JSON; unmatched policies remain report-only")
    benchmark_cmd.add_argument(
        "--output-contract",
        choices=("cell_proportions_v1", "reference_free_components_v1", "reference_free_components_v2"),
        default="cell_proportions_v1",
        help="reference-free components are intentionally rejected without a separately validated mapping",
    )
    benchmark_cmd.set_defaults(handler=_cmd_benchmark)

    evidence_cmd = sub.add_parser("evidence", help="Manage local scientific evidence records without status promotion")
    evidence_sub = evidence_cmd.add_subparsers(dest="evidence_command", required=True)
    evidence_init = evidence_sub.add_parser("init")
    evidence_init.add_argument("--registry", type=Path, required=True); evidence_init.add_argument("--registry-id", default="local-development")
    evidence_init.set_defaults(handler=_cmd_evidence_init)
    evidence_import = evidence_sub.add_parser("import")
    evidence_import.add_argument("--registry", type=Path, required=True); evidence_import.add_argument("--record", type=Path, required=True)
    evidence_import.set_defaults(handler=_cmd_evidence_import)
    evidence_review = evidence_sub.add_parser("review")
    evidence_review.add_argument("--registry", type=Path, required=True); evidence_review.add_argument("--review", type=Path, required=True)
    evidence_review.set_defaults(handler=_cmd_evidence_review)
    evidence_query = evidence_sub.add_parser("query")
    evidence_query.add_argument("--registry", type=Path, required=True); evidence_query.add_argument("--selector"); evidence_query.add_argument("--tool")
    evidence_query.set_defaults(handler=_cmd_evidence_query)
    evidence_audit = evidence_sub.add_parser("audit")
    evidence_audit.add_argument("--registry", type=Path, required=True); evidence_audit.add_argument("--catalog", type=Path)
    evidence_audit.add_argument("--verify-sources", action="store_true", help="verify only typed source_artifacts beneath --evidence-root")
    evidence_audit.add_argument("--evidence-root", type=Path, help="controlled root for portable source_artifacts locators; required with --verify-sources")
    evidence_audit.add_argument("--max-source-bytes", type=int, help="maximum bytes to read from one declared source artifact")
    evidence_audit.set_defaults(handler=_cmd_evidence_audit)

    adapter = sub.add_parser("adapter", help="Inspect declarative adapter SDK contracts")
    adapter_sub = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_list = adapter_sub.add_parser("list", help="List bundled adapter descriptors")
    adapter_list.add_argument("--json", action="store_true")
    adapter_list.set_defaults(handler=_cmd_adapter_list)
    adapter_validate = adapter_sub.add_parser("validate", help="Validate bundled adapter descriptor schema")
    adapter_validate.add_argument("--json", action="store_true")
    adapter_validate.set_defaults(handler=_cmd_adapter_validate)

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
    ref_available = reference_sub.add_parser(
        "available",
        help="List reference records from the local bundled or audited catalog without networking",
    )
    _add_home_option(ref_available)
    ref_available.add_argument("--metadata-dir", type=Path)
    ref_available.add_argument("--json", action="store_true")
    ref_available.set_defaults(handler=_cmd_reference_available)
    ref_fetch = reference_sub.add_parser(
        "fetch",
        help="Fetch one exact approved reference from the audited static catalog",
    )
    _add_home_option(ref_fetch)
    ref_fetch.add_argument(
        "selector",
        help="exact reference selector: reference_id@version (unversioned selectors are refused)",
    )
    ref_fetch.add_argument(
        "--install",
        action="store_true",
        help="install the verified archive after download and validate its reference manifest",
    )
    ref_fetch.add_argument(
        "--metadata-dir",
        type=Path,
        help="catalog metadata directory created by `asset audit --online`",
    )
    ref_fetch.set_defaults(handler=_cmd_reference_fetch)
    ref_lifecycle = reference_sub.add_parser(
        "lifecycle",
        help="Audit immutable reference construction/QC/evidence records against one catalog snapshot",
    )
    ref_lifecycle.add_argument("registry", type=Path, help="reference lifecycle registry JSON")
    ref_lifecycle.add_argument("--catalog", type=Path, help="exact static catalog JSON to bind")
    ref_lifecycle.add_argument("--json", action="store_true")
    ref_lifecycle.set_defaults(handler=_cmd_reference_lifecycle)
    ref_lifecycle_plan = reference_sub.add_parser(
        "lifecycle-plan",
        help="List catalog-bound construction/QC/evidence requirements without creating a lifecycle registry",
    )
    ref_lifecycle_plan.add_argument("--catalog", type=Path, help="exact static catalog JSON to inspect")
    ref_lifecycle_plan.add_argument("--json", action="store_true")
    ref_lifecycle_plan.set_defaults(handler=_cmd_reference_lifecycle_plan)
    ref_qc = reference_sub.add_parser(
        "qc",
        help="Validate a declared supported-format QC record without executing a model or workflow",
    )
    ref_qc.add_argument("--record", type=Path, required=True, help="asset QC JSON record")
    ref_qc.set_defaults(handler=_cmd_reference_qc)
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
    asset_plan.add_argument("--bundle", type=Path, help="offline-kit bundle-index.json; enables exact dependency planning")
    asset_plan.add_argument("--metadata-dir", type=Path, help="audited static-catalog metadata directory; defaults to the local catalog snapshot")
    asset_plan.add_argument("--scenario")
    asset_plan.add_argument("--platform", choices=("450k", "epic", "wgbs"))
    asset_plan.add_argument("--genome-build", choices=("hg19", "hg38"))
    asset_plan.add_argument("--analysis-contract", choices=(
        "array_450k_cpg", "array_epic_cpg", "array_epic_from_450k_common_cpg_v1",
        "wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1", "wgbs_native_hg19", "wgbs_native_hg38",
    ))
    asset_plan.add_argument(
        "--input-type", choices=("array", "bam", "bed", "pat"),
        help="array for native 450K/EPIC matrices; bam/bed/pat for WGBS routes",
    )
    asset_plan.add_argument("--tools", default="all")
    asset_plan.add_argument("--accelerator", choices=("cpu", "gpu"), default="cpu")
    asset_plan.add_argument("--mode", choices=("decon", "build", "build_and_decon"), default="decon")
    asset_plan.add_argument("--reference")
    asset_plan.add_argument("--external-runtime-root", type=Path)
    asset_plan.add_argument("--local-dependency-lock", type=Path, help="local exact dependency binding lock")
    asset_plan.add_argument("--local-dependency-locators", type=Path, help="non-portable local locator overlay for the binding lock")
    asset_plan.add_argument("--output", type=Path, help="path for the immutable asset lock JSON")
    asset_plan.add_argument(
        "--previous-lock", type=Path,
        help="compare a prior exact lock to this newly generated lock; never performs an upgrade or removal",
    )
    asset_plan.add_argument("--json", action="store_true")
    asset_plan.add_argument(
        "--summary", action="store_true",
        help="print a concise human-readable selection, state, space, warning and blocker summary",
    )
    asset_plan.set_defaults(handler=_cmd_asset_plan)
    asset_verify = asset_sub.add_parser("verify", help="Verify installed module payloads (offline)")
    _add_home_option(asset_verify)
    asset_verify.add_argument("module", nargs="?")
    asset_verify.add_argument("--lock", type=Path, help="verify one complete exact installation set")
    asset_verify.add_argument("--plan", type=Path, help="verify one selection-bound static-catalog installation plan")
    asset_verify.add_argument("--metadata-dir", type=Path)
    asset_verify.add_argument("--local-dependency-lock", type=Path)
    asset_verify.add_argument("--local-dependency-locators", type=Path)
    asset_verify.add_argument("--external-runtime-root", type=Path)
    asset_verify.add_argument("--json", action="store_true")
    asset_verify.set_defaults(handler=_cmd_asset_verify)
    asset_import = asset_sub.add_parser("import", help="Import a user-owned offline module archive")
    _add_home_option(asset_import)
    asset_import.add_argument("archive", type=Path, nargs="+")
    asset_import.add_argument("--plan", type=Path, help="selection-bound plan required for exact offline import")
    asset_import.add_argument("--metadata-dir", type=Path)
    asset_import.add_argument("--local-dependency-lock", type=Path)
    asset_import.add_argument("--local-dependency-locators", type=Path)
    asset_import.add_argument("--external-runtime-root", type=Path)
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
    asset_install = asset_sub.add_parser("install", help="Install every module locked by an offline asset plan")
    _add_home_option(asset_install)
    asset_install.add_argument("--lock", type=Path)
    asset_install.add_argument("--bundle-root", type=Path)
    asset_install.add_argument("--plan", type=Path, help="selection-bound static-catalog plan for explicit public installation")
    asset_install.add_argument("--metadata-dir", type=Path)
    asset_install.add_argument("--external-runtime-root", type=Path)
    asset_install.set_defaults(handler=_cmd_asset_install)
    asset_repair = asset_sub.add_parser("repair", help="Restore damaged/missing modules from one exact offline asset lock")
    _add_home_option(asset_repair)
    asset_repair.add_argument("--lock", type=Path)
    asset_repair.add_argument("--bundle-root", type=Path)
    asset_repair.add_argument("--plan", type=Path, help="selection-bound static-catalog plan for exact repair")
    asset_repair.add_argument("--archive", type=Path, action="append", default=[], help="verified caller-provided archive used for exact repair")
    asset_repair.add_argument("--metadata-dir", type=Path)
    asset_repair.add_argument("--external-runtime-root", type=Path)
    asset_repair.set_defaults(handler=_cmd_asset_repair)
    asset_remove = asset_sub.add_parser("remove", help="Preview the impact of retiring one exact managed module")
    _add_home_option(asset_remove)
    asset_remove.add_argument("module", help="exact MODULE_ID@VERSION; unversioned removal is refused")
    remove_mode = asset_remove.add_mutually_exclusive_group()
    remove_mode.add_argument("--dry-run", action="store_true")
    remove_mode.add_argument("--yes", action="store_true", help="deprecated compatibility flag; daily remove remains preview-only")
    asset_remove.add_argument("--force", action="store_true", help="deprecated compatibility flag; use maintenance-retire for an offline maintenance operation")
    asset_remove.set_defaults(handler=_cmd_asset_remove)
    asset_maintenance = asset_sub.add_parser("maintenance-retire", help="Recoverably retire one exact module during an approved offline maintenance window")
    _add_home_option(asset_maintenance)
    asset_maintenance.add_argument("module", help="exact MODULE_ID@VERSION; unversioned retirement is refused")
    asset_maintenance.add_argument("--yes", action="store_true", required=True, help="perform the recoverable maintenance retirement")
    asset_maintenance.add_argument("--maintenance-window-confirmed", action="store_true", required=True, help="confirm external workflows have been stopped for this maintenance window")
    asset_maintenance.add_argument("--force", action="store_true", required=True, help="acknowledge that host-wide process use is otherwise not provable")
    asset_maintenance.set_defaults(handler=_cmd_asset_maintenance_retire)
    asset_upgrade = asset_sub.add_parser("upgrade", help="Compare two exact plans; never installs or removes assets")
    _add_home_option(asset_upgrade)
    asset_upgrade.add_argument("--from-plan", type=Path, required=True)
    asset_upgrade.add_argument("--to-plan", type=Path, required=True)
    asset_upgrade.set_defaults(handler=_cmd_asset_upgrade)
    asset_preflight = asset_sub.add_parser("preflight", help="Check managed download/install filesystems for one exact plan")
    _add_home_option(asset_preflight)
    asset_preflight.add_argument("--plan", type=Path, required=True)
    asset_preflight.add_argument("--metadata-dir", type=Path)
    asset_preflight.set_defaults(handler=_cmd_asset_preflight)
    asset_bundle = asset_sub.add_parser("bundle", help="Create a portable offline-kit directory from immutable module archives")
    _add_home_option(asset_bundle)
    asset_bundle_sub = asset_bundle.add_subparsers(dest="asset_bundle_command", required=True)
    asset_bundle_create = asset_bundle_sub.add_parser("create")
    _add_home_option(asset_bundle_create)
    asset_bundle_create.add_argument("--output", type=Path, required=True)
    asset_bundle_create.add_argument("--archive", type=Path, action="append", required=True)
    asset_bundle_create.add_argument(
        "--distribution-status", choices=("LICENSE_APPROVED", "USER_SUPPLIED_SUPPORTED", "BLOCKED"),
        default="USER_SUPPLIED_SUPPORTED",
    )
    asset_bundle_create.set_defaults(handler=_cmd_asset_bundle_create)

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
    parser.add_argument("--asset-lock", type=Path, help="exact installed asset-set lock; binds reference, modules and execution profile")
    parser.add_argument("--tools", default="all")
    parser.add_argument(
        "--validation-candidate",
        action="store_true",
        help="restricted: run only an explicitly marked quarantined validation candidate",
    )
    parser.add_argument("--reference-root", type=Path)
    parser.add_argument(
        "--catalog-metadata-dir", type=Path,
        help="optional audited catalog metadata directory used to resolve the WP-03 installation dependency plan",
    )
    parser.add_argument(
        "--local-dependency-lock", type=Path,
        help="local exact dependency binding lock; requires --local-dependency-locators and grants no distribution rights",
    )
    parser.add_argument(
        "--local-dependency-locators", type=Path,
        help="non-portable local path overlay for --local-dependency-lock",
    )
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
    parser.add_argument("--step-cache-root", type=Path, help="opt-in local cache for deterministic lifecycle artifacts; never enables network retrieval")
    parser.add_argument("--workflow-timeout-seconds", type=int, help="optional complete-workflow hard limit; timeout cleans only this workflow process group")


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


def _resolve_analysis_request(args: argparse.Namespace, *, entrypoint: str, require_platform: bool) -> dict[str, object]:
    """Apply the established CLI policy once for plan, doctor and run.

    This is deliberately an adapter over existing arguments, not a new config
    language.  Values retain their existing CLI/environment/default behavior;
    the record makes the resulting effective selection and known origin class
    visible to every entry point.
    """
    _legacy(args)
    _apply_asset_lock(args)
    if require_platform and not args.platform:
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
        getattr(args, "mode", "decon") == "decon"
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
    if args.platform:
        _normalize_genome_build(args)
    source = "ASSET_LOCK" if getattr(args, "_asset_lock", None) else "CLI_OR_DEFAULT"
    target_platform = {
        "array_epic_from_450k_common_cpg_v1": "epic",
        "wgbs_derived_epic_cpg_v1": "epic",
        "wgbs_derived_450k_cpg_v1": "450k",
    }.get(args.analysis_contract, args.platform)
    effective = {
        "scenario": args.scenario,
        "input_platform": args.platform,
        "target_platform": target_platform,
        "genome_build_requested": requested_genome_build,
        "genome_build": args.genome_build,
        "analysis_contract": args.analysis_contract,
        "reference": args.reference,
        "tools_requested": args.tools,
        "accelerator": args.accelerator,
        "runtime": args.runtime,
        "container_engine": args.container_engine,
        "executor": args.executor,
        "allow_cross_platform": args.allow_cross_platform,
        "input": str(args.input.expanduser().resolve()) if getattr(args, "input", None) else None,
        "output": str(args.outdir.expanduser().resolve()) if getattr(args, "outdir", None) else None,
        "catalog_metadata_dir": str(args.catalog_metadata_dir.expanduser().resolve()) if args.catalog_metadata_dir else None,
        "parameters": {
            "random_seed": getattr(args, "random_seed", None),
            "medecom_k": args.medecom_k,
            "medecom_lambda": args.medecom_lambda,
            "medecom_nfolds": args.medecom_nfolds,
            "methylcibersort_permutations": args.methylcibersort_permutations,
            "preprocess_threads": args.preprocess_threads,
            "preprocess_sort_memory": args.preprocess_sort_memory,
            "bam_contig_policy": args.bam_contig_policy,
        },
    }
    record: dict[str, object] = {
        "schema": "methunmix-resolved-analysis-request-v1",
        "entrypoint": entrypoint,
        "raw_user_input": {
            "platform": args.platform, "scenario": args.scenario, "reference": args.reference,
            "tools": args.tools, "analysis_contract": args.analysis_contract,
        },
        "effective_values": effective,
        "field_sources": {key: source for key in effective},
        "configuration_policy": "existing CLI, asset-lock, environment and default resolution; no new configuration precedence is introduced by WP-04",
    }
    args._analysis_request = record
    return record


def _resolve_catalog_dependency_plan(args: argparse.Namespace, request_record: dict[str, object]) -> dict[str, object]:
    """Optionally attach WP-03 exact-asset planning without inventing metadata.

    Existing direct-reference workflows are deliberately not forced through an
    incomplete catalog.  Once a caller explicitly provides audited metadata
    (or the explicit catalog override environment), its identity and blockers
    become part of the execution-preflight contract for all three entry points.
    """
    explicitly_requested = (
        args.catalog_metadata_dir is not None or bool(os.environ.get("METHUNMIX_CATALOG_PATH"))
        or args.local_dependency_lock is not None or args.local_dependency_locators is not None
    )
    if not explicitly_requested:
        plan: dict[str, object] = {
            "state": "NOT_CHECKED_NO_CATALOG_METADATA_REQUESTED",
            "detail": "existing direct reference/runtime resolution remains active; no complete installation dependency claim is made",
        }
    elif not args.scenario or not args.platform or not args.analysis_contract:
        plan = {
            "state": "NOT_CHECKED_INCOMPLETE_ANALYSIS_SELECTION",
            "detail": "scenario, platform and analysis contract are required before catalog dependency planning",
        }
    else:
        try:
            resolved, code = create_catalog_plan(
                scenario=args.scenario,
                platform=args.platform,
                genome_build=args.genome_build,
                analysis_contract=args.analysis_contract,
                tools=args.tools,
                accelerator=args.accelerator,
                reference=args.reference,
                external_runtime_root=args.external_runtime_root,
                metadata_dir=args.catalog_metadata_dir,
                local_dependency_lock=args.local_dependency_lock,
                local_dependency_locators=args.local_dependency_locators,
                capture_local_runtime_files=True,
            )
            ephemeral_runtime_files = resolved.pop("_ephemeral_local_runtime_files", {})
            if not isinstance(ephemeral_runtime_files, dict) or not all(
                isinstance(name, str) and isinstance(path, Path)
                for name, path in ephemeral_runtime_files.items()
            ):
                raise DeMethFlowError("CATALOG_PLAN_ERROR: local runtime resolver returned an invalid ephemeral context")
            args._local_runtime_files = ephemeral_runtime_files
            plan = {
                "state": "RESOLVED" if code == 0 else "BLOCKED",
                "return_code": code,
                "plan_identity": resolved.get("plan_identity"),
                "catalog": resolved.get("catalog"),
                "reference": resolved.get("reference"),
                "assets": resolved.get("assets"),
                "dependency_requirements": resolved.get("dependency_requirements"),
                "local_dependencies": resolved.get("local_dependencies"),
                "external_runtimes": resolved.get("external_runtimes"),
                "blockers": resolved.get("blockers"),
                "warnings": resolved.get("warnings"),
            }
        except DeMethFlowError as exc:
            args._local_runtime_files = {}
            plan = {"state": "BLOCKED", "detail": str(exc), "blockers": [{"code": "CATALOG_PLAN_ERROR", "detail": str(exc)}]}
    request_record["installation_dependency_plan"] = plan
    args._catalog_dependency_plan = plan
    return plan


def _cmd_run(args: argparse.Namespace) -> int:
    request_record = _resolve_analysis_request(args, entrypoint="run", require_platform=True)
    dependency_plan = _resolve_catalog_dependency_plan(args, request_record)
    if dependency_plan["state"] == "BLOCKED":
        raise DeMethFlowError(f"Execution preflight blocked by catalog dependency plan: {json.dumps(dependency_plan.get('blockers', []), ensure_ascii=False)}")
    requested_genome_build = request_record["effective_values"]["genome_build_requested"]
    assert requested_genome_build is None or isinstance(requested_genome_build, str)
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
        asset_lock=getattr(args, "_asset_lock", None),
        request_provenance=request_record,
        installation_dependency_plan=dependency_plan,
        local_runtime_files=getattr(args, "_local_runtime_files", {}),
        step_cache_root=args.step_cache_root,
        workflow_timeout_seconds=args.workflow_timeout_seconds,
    )
    manifest = run_decon(request)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    request_record = _resolve_analysis_request(args, entrypoint="doctor", require_platform=False)
    dependency_plan = _resolve_catalog_dependency_plan(args, request_record)
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
        verify_reference_checksums=(args.verify_reference_checksums or args.check_mode == "deep"),
        medecom_k=args.medecom_k,
        medecom_lambda=args.medecom_lambda,
        medecom_nfolds=args.medecom_nfolds,
        methylcibersort_permutations=args.methylcibersort_permutations,
        preprocess_threads=args.preprocess_threads,
        preprocess_sort_memory=args.preprocess_sort_memory,
        bam_contig_policy=args.bam_contig_policy,
        allow_validation_candidate=args.validation_candidate,
        isolated_reference_root=getattr(args, "_asset_lock", None) is not None,
        request_provenance=request_record,
        check_mode=args.check_mode,
        installation_dependency_plan=dependency_plan,
        local_runtime_files=getattr(args, "_local_runtime_files", {}),
    )
    if getattr(args, "_asset_lock", None) and result.bundle:
        verify_locked_reference(args._asset_lock, result.bundle)
        verify_locked_external_runtimes(args._asset_lock, args.external_runtime_root)
    if args.json:
        print(json.dumps(result_as_dict(result), ensure_ascii=False, indent=2))
    else:
        for check in result.checks:
            print(f"{'OK' if check.ok else 'FAIL'}\t{check.name}\t{check.detail}")
    return 0 if result.ok else 2


def _cmd_execution_plan(args: argparse.Namespace) -> int:
    request_record = _resolve_analysis_request(args, entrypoint="plan", require_platform=True)
    dependency_plan = _resolve_catalog_dependency_plan(args, request_record)
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
        verify_reference_checksums=(args.verify_reference_checksums or args.check_mode == "deep"),
        medecom_k=args.medecom_k,
        medecom_lambda=args.medecom_lambda,
        medecom_nfolds=args.medecom_nfolds,
        methylcibersort_permutations=args.methylcibersort_permutations,
        preprocess_threads=args.preprocess_threads,
        preprocess_sort_memory=args.preprocess_sort_memory,
        bam_contig_policy=args.bam_contig_policy,
        allow_validation_candidate=args.validation_candidate,
        isolated_reference_root=getattr(args, "_asset_lock", None) is not None,
        request_provenance=request_record,
        check_mode=args.check_mode,
        installation_dependency_plan=dependency_plan,
        local_runtime_files=getattr(args, "_local_runtime_files", {}),
    )
    plan = result.execution_plan
    if args.outdir is not None:
        plan = {**plan, "observations": {"intended_output_directory": str(args.outdir.expanduser().resolve())}}
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(f"STATUS\t{plan['execution_readiness']}")
        print(f"REFERENCE\t{plan['selection']['reference_selector']}")
        print(f"ROUTE\t{plan['selection']['analysis_contract']}")
        print(f"TOOLS\t{','.join(item['tool'] for item in plan['tools']['selected'])}")
        for item in plan["constraints"]["blocking_checks"]:
            print(f"BLOCKED\t{item['name']}\t{item['detail']}")
    return 0 if result.ok else 2


def _cmd_report(args: argparse.Namespace) -> int:
    if args.truth is not None and not args.tool:
        raise DeMethFlowError("REPORT_TRUTH_TOOL_REQUIRED: --tool is required when --truth is supplied")
    report = build_report(
        args.run_dir,
        args.output_dir,
        truth=args.truth,
        tool=args.tool,
        variant=args.variant,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "status": "REPORT_WRITTEN",
            "json": str(args.output_dir.expanduser().resolve() / "report.json"),
            "html": str(args.output_dir.expanduser().resolve() / "report.html"),
            "source_run": str(args.run_dir.expanduser().resolve()),
        }, ensure_ascii=False, indent=2))
    return 0


def _cmd_replay_plan(args: argparse.Namespace) -> int:
    plan = prepare_replay(
        args.manifest,
        args.outdir,
        input_path=args.input,
        verify_input_identity=args.verify_input_identity,
    )
    if args.output is not None:
        atomic_write_json(args.output.expanduser().resolve(), plan)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan["status"] == "PREPARED_NOT_EXECUTED" else 2


def _cmd_replay_execute(args: argparse.Namespace) -> int:
    kwargs = {
        "replay_plan_path": args.replay_plan,
        "source_manifest_path": args.source_manifest,
        "input_path": args.input,
        "outdir": args.outdir,
        "reference_root": args.reference_root,
        "runtime_root": args.runtime_root,
        "external_runtime_root": args.external_runtime_root,
        "timeout_seconds": args.workflow_timeout_seconds,
        "catalog_metadata_dir": args.catalog_metadata_dir,
        "local_dependency_lock": args.local_dependency_lock,
        "local_dependency_locators": args.local_dependency_locators,
    }
    if args.execute:
        payload = execute_safe_replay(**kwargs, report_output_dir=args.report_output_dir)
    else:
        payload = validate_safe_replay(**kwargs)
    if args.output is not None:
        atomic_write_json(args.output.expanduser().resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_resource_preflight(args: argparse.Namespace) -> int:
    if args.cpus < 1 or args.concurrency < 1:
        raise DeMethFlowError("--cpus and --concurrency must be positive")
    if args.known_required_bytes is not None and args.known_required_bytes < 0:
        raise DeMethFlowError("--known-required-bytes must be non-negative")
    payload = inspect_resources(
        [("outdir", args.outdir), ("tmpdir", args.tmpdir), ("cache_dir", args.cache_dir)],
        requested_cpus=args.cpus, requested_concurrency=args.concurrency,
        known_required_bytes=args.known_required_bytes,
    )
    if args.output is not None:
        atomic_write_json(args.output.expanduser().resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_recovery_plan(args: argparse.Namespace) -> int:
    payload = prepare_recovery_plan(args.manifest, new_outdir=args.outdir)
    if args.output is not None:
        atomic_write_json(args.output.expanduser().resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_retry_failed_tool(args: argparse.Namespace) -> int:
    try:
        request = json.loads(args.request_json.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"FRESH_RETRY_REQUEST_INVALID: {exc}") from exc
    if not isinstance(request, dict):
        raise DeMethFlowError("FRESH_RETRY_REQUEST_INVALID: request JSON must be an object")
    request["execute"] = bool(args.execute)
    dependency_kwargs = {
        "catalog_metadata_dir": args.catalog_metadata_dir,
        "local_dependency_lock": args.local_dependency_lock,
        "local_dependency_locators": args.local_dependency_locators,
    }
    if args.execute:
        payload = execute_fresh_retry(args.source_run_dir, args.tool, args.outdir, request, **dependency_kwargs)
    else:
        payload = validate_fresh_retry(args.source_run_dir, args.tool, args.outdir, request, **dependency_kwargs)
    if args.output is not None:
        atomic_write_json(args.output.expanduser().resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    policy = None
    if args.policy is not None:
        try:
            policy = json.loads(args.policy.expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeMethFlowError(f"BENCHMARK_POLICY_INVALID: cannot read --policy: {exc}") from exc
    result = benchmark(
        args.estimate,
        args.truth,
        output=args.output,
        output_contract=args.output_contract,
        context={key: getattr(args, key) for key in ("tool", "adapter", "profile", "scenario", "platform", "genome_build", "analysis_contract", "variant") if getattr(args, key) is not None},
        policy=policy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _read_json_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"{label}_INVALID: cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DeMethFlowError(f"{label}_INVALID: expected a JSON object")
    return value


def _cmd_evidence_init(args: argparse.Namespace) -> int:
    print(json.dumps(initialize_scientific_evidence(args.registry, registry_id=args.registry_id), ensure_ascii=False, indent=2)); return 0


def _cmd_evidence_import(args: argparse.Namespace) -> int:
    print(json.dumps(import_record(args.registry, _read_json_object(args.record, label="EVIDENCE_RECORD")), ensure_ascii=False, indent=2)); return 0


def _cmd_evidence_review(args: argparse.Namespace) -> int:
    print(json.dumps(add_review(args.registry, _read_json_object(args.review, label="EVIDENCE_REVIEW")), ensure_ascii=False, indent=2)); return 0


def _cmd_evidence_query(args: argparse.Namespace) -> int:
    print(json.dumps(query_scientific_evidence(args.registry, selector=args.selector, tool=args.tool), ensure_ascii=False, indent=2)); return 0


def _cmd_evidence_audit(args: argparse.Namespace) -> int:
    result = audit_scientific_evidence(
        args.registry,
        catalog_path=args.catalog,
        verify_sources=args.verify_sources,
        evidence_root=args.evidence_root,
        maximum_source_bytes=(args.max_source_bytes if args.max_source_bytes is not None else 64 * 1024 * 1024),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Existing structural-only audit remains an informational compatibility
    # report.  An explicitly requested source verification is a gate: missing,
    # changed or unsafe declared artifacts must be observable to automation.
    return 0 if not args.verify_sources or result["status"] == "PASS_STRUCTURAL_AND_SOURCES_VERIFIED_NO_STATUS_PROMOTION" else 2


def _cmd_adapter_list(args: argparse.Namespace) -> int:
    report = registry_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("ADAPTER\tTOOL\tWORKFLOW_FAMILY\tCONTRACTS")
        for item in report["descriptors"]:
            print(f"{item['adapter_id']}\t{item['tool']}\t{item['workflow_family']}\t{','.join(item['contracts'])}")
    return 0


def _cmd_adapter_validate(args: argparse.Namespace) -> int:
    report = registry_report()
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else f"PASS\t{report['descriptor_count']} adapter descriptors")
    return 0


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


def _cmd_reference_available(args: argparse.Namespace) -> int:
    result = list_reference_catalog(metadata_dir=args.metadata_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("SELECTOR\tACCESS\tINSTALLED\tSCENARIO\tSOURCE\tCONTRACT\tBUILD\tDISTRIBUTION")
        for row in result["references"]:
            print(
                f"{row['selector']}\t{row['access']}\t{row['installed']}\t{row['scenario']}\t"
                f"{row['source_platform']}\t{row['analysis_contract']}\t{row['genome_build']}\t"
                f"{row['distribution_status']}"
            )
    return 0


def _cmd_reference_fetch(args: argparse.Namespace) -> int:
    result = fetch_reference(
        args.selector,
        install=args.install,
        metadata_dir=args.metadata_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_reference_lifecycle(args: argparse.Namespace) -> int:
    from .reference_lifecycle import audit_reference_lifecycle_registry

    result = audit_reference_lifecycle_registry(args.registry, catalog_path=args.catalog)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


def _cmd_reference_lifecycle_plan(args: argparse.Namespace) -> int:
    from .reference_lifecycle import reference_lifecycle_declaration_plan

    result = reference_lifecycle_declaration_plan(catalog_path=args.catalog)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("SELECTOR\tSCENARIO\tPLATFORM\tCONTRACT\tBUILD\tLIFECYCLE_DECLARATIONS")
        for row in result["references"]:
            scope = row["scope"]
            print(
                f"{row['selector']}\t{scope['scenario']}\t{scope['source_platform']}\t"
                f"{scope['analysis_contract']}\t{scope['genome_build']}\tREQUIRED"
            )
        print(
            "No construction/QC/engineering/scientific status is inferred from this plan; "
            "submit only digestable existing records to the lifecycle builder."
        )
    return 0


def _cmd_reference_qc(args: argparse.Namespace) -> int:
    from .asset_qc import validate_asset_qc_record
    from .util import strict_json_loads

    try:
        result = validate_asset_qc_record(strict_json_loads(args.record.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise DeMethFlowError(f"ASSET_QC_INVALID: cannot read record: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
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
    if args.bundle is not None:
        required = ("scenario", "platform", "analysis_contract", "input_type", "output")
        missing = [name for name in required if getattr(args, name) is None]
        if missing:
            raise DeMethFlowError("--bundle planning requires " + ", ".join(f"--{name.replace('_', '-')}" for name in missing))
        lock, code = create_plan(
            args.bundle,
            scenario=args.scenario,
            platform=args.platform,
            genome_build=args.genome_build,
            analysis_contract=args.analysis_contract,
            input_type=args.input_type,
            tools=args.tools,
            accelerator=args.accelerator,
            mode=args.mode,
            output=args.output,
            reference=args.reference,
            external_runtime_root=args.external_runtime_root,
        )
        payload = lock
        if args.previous_lock is not None:
            payload = {
                "asset_lock": lock,
                "upgrade_comparison": compare_upgrade_locks(args.previous_lock, lock),
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return code
    required = ("scenario", "platform", "analysis_contract")
    missing = [name for name in required if getattr(args, name) is None]
    if missing:
        # Preserve the historical zero-argument inventory behavior while
        # refusing an ambiguous partial analysis selection.
        supplied = any(getattr(args, name) is not None for name in (*required, "reference", "output"))
        if supplied:
            raise DeMethFlowError("static-catalog planning requires " + ", ".join(f"--{name.replace('_', '-')}" for name in missing))
        print(plan_assets(json_output=args.json))
        return 0
    plan, code = create_catalog_plan(
        scenario=args.scenario, platform=args.platform, genome_build=args.genome_build,
        analysis_contract=args.analysis_contract, tools=args.tools, accelerator=args.accelerator,
        reference=args.reference, external_runtime_root=args.external_runtime_root,
        metadata_dir=args.metadata_dir, local_dependency_lock=args.local_dependency_lock,
        local_dependency_locators=args.local_dependency_locators, output=args.output,
    )
    if args.summary:
        print(render_catalog_plan_summary(plan))
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    return code


def _cmd_asset_verify(args: argparse.Namespace) -> int:
    if args.lock is not None or args.plan is not None:
        if args.module or (args.lock is not None and args.plan is not None):
            raise DeMethFlowError("asset verify accepts exactly one of MODULE, --lock, or --plan")
        if args.plan is not None:
            report, code = verify_catalog_plan(
                args.plan, metadata_dir=args.metadata_dir, external_runtime_root=args.external_runtime_root,
                local_dependency_lock=args.local_dependency_lock,
                local_dependency_locators=args.local_dependency_locators,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return code
        report, code = verify_lock(args.lock)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else json.dumps(report, ensure_ascii=False, indent=2))
        return code
    return verify_assets(module_id=args.module, json_output=args.json)


def _cmd_asset_import(args: argparse.Namespace) -> int:
    if args.plan is None:
        if len(args.archive) != 1:
            raise DeMethFlowError("legacy asset import accepts exactly one archive; use --plan for exact multi-archive import")
        return import_asset(args.archive[0])
    report, code = import_catalog_plan(
        args.plan, args.archive, metadata_dir=args.metadata_dir,
        external_runtime_root=args.external_runtime_root,
        local_dependency_lock=args.local_dependency_lock,
        local_dependency_locators=args.local_dependency_locators,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


def _cmd_asset_fetch(args: argparse.Namespace) -> int:
    return fetch_asset(args.asset_id, metadata_dir=args.metadata_dir)


def _cmd_asset_audit(args: argparse.Namespace) -> int:
    return audit_online(args.catalog_url, metadata_dir=args.metadata_dir)


def _cmd_asset_gc(args: argparse.Namespace) -> int:
    return gc_assets(dry_run=args.dry_run)


def _cmd_asset_install(args: argparse.Namespace) -> int:
    if args.plan is not None:
        if args.lock is not None or args.bundle_root is not None:
            raise DeMethFlowError("asset install accepts either --plan or --lock with --bundle-root")
        report, code = install_catalog_plan(
            args.plan, metadata_dir=args.metadata_dir,
            external_runtime_root=args.external_runtime_root,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return code
    if args.lock is None or args.bundle_root is None:
        raise DeMethFlowError("asset install requires --plan or both --lock and --bundle-root")
    report, code = install_lock(args.lock, args.bundle_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


def _cmd_asset_repair(args: argparse.Namespace) -> int:
    if args.plan is not None:
        if args.lock is not None or args.bundle_root is not None:
            raise DeMethFlowError("asset repair accepts either --plan or --lock with --bundle-root")
        report, code = repair_catalog_plan(
            args.plan, args.archive, metadata_dir=args.metadata_dir, external_runtime_root=args.external_runtime_root,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return code
    if args.lock is None or args.bundle_root is None:
        raise DeMethFlowError("asset repair requires --plan or both --lock and --bundle-root")
    report, code = repair_lock(args.lock, args.bundle_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


def _cmd_asset_remove(args: argparse.Namespace) -> int:
    report, code = remove_module(args.module, dry_run=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


def _cmd_asset_maintenance_retire(args: argparse.Namespace) -> int:
    report, code = remove_module(
        args.module,
        dry_run=False,
        force=args.force,
        maintenance=True,
        maintenance_window_confirmed=args.maintenance_window_confirmed,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


def _cmd_asset_upgrade(args: argparse.Namespace) -> int:
    print(json.dumps(compare_catalog_plans(args.from_plan, args.to_plan), ensure_ascii=False, indent=2))
    return 0


def _cmd_asset_preflight(args: argparse.Namespace) -> int:
    report, code = preflight_catalog_plan(args.plan, metadata_dir=args.metadata_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


def _cmd_asset_bundle_create(args: argparse.Namespace) -> int:
    index = create_bundle(args.output, args.archive, distribution_status=args.distribution_status)
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0


def _apply_asset_lock(args: argparse.Namespace) -> None:
    lock_path = getattr(args, "asset_lock", None)
    if lock_path is None:
        return
    values = vars(args)
    apply_lock_to_selection(lock_path, values)
    args._asset_lock = values["asset_lock"]
    if args.analysis_contract in {"array_epic_from_450k_common_cpg_v1", "wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}:
        args.allow_cross_platform = True


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
