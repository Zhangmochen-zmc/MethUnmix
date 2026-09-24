from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import CORE_API, PROJECT_ROOT, REFERENCE_SCHEMA, RUNTIME_API, version
from .errors import DeMethFlowError
from .manifest import ReferenceBundle
from .packaging import export_reference
from .runtime import RUNTIME_FILES
from .util import atomic_write_json, sha256_file


RUNTIME_TOOL_SOURCES = {
    "array-common": [
        (PROJECT_ROOT / "deconvolution" / "array_pro", "tools/array_pro"),
        (PROJECT_ROOT / "deconvolution" / "bin", "tools/bin"),
    ],
    "wgbs-common": [
        (PROJECT_ROOT / "deconvolution" / "wgbs_scripts", "tools/wgbs_scripts"),
    ],
    "MethylBERT-cpu": [
        (PROJECT_ROOT / "deconvolution" / "wgbs_scripts" / "methylbert_pre1.py", "tools/methylbert/methylbert_pre1.py"),
        (PROJECT_ROOT / "deconvolution" / "wgbs_scripts" / "methylbert_fix.py", "tools/methylbert/methylbert_fix.py"),
        (PROJECT_ROOT / "deconvolution" / "wgbs_scripts" / "methylbert_compact_deconvolute.py", "tools/methylbert/methylbert_compact_deconvolute.py"),
    ],
    "MethylBERT-gpu": [
        (PROJECT_ROOT / "deconvolution" / "wgbs_scripts" / "methylbert_pre1.py", "tools/methylbert/methylbert_pre1.py"),
        (PROJECT_ROOT / "deconvolution" / "wgbs_scripts" / "methylbert_fix.py", "tools/methylbert/methylbert_fix.py"),
        (PROJECT_ROOT / "deconvolution" / "wgbs_scripts" / "methylbert_compact_deconvolute.py", "tools/methylbert/methylbert_compact_deconvolute.py"),
    ],
}

DATA_MODULE_SOURCES = {
    "WGBSTools-hg19-data": {
        "sources": [
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "reference_manifest.json", "wgbstools/hg19/reference_manifest.json"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "CpG.bed.gz", "wgbstools/hg19/CpG.bed.gz"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "CpG.bed.gz.csi", "wgbstools/hg19/CpG.bed.gz.csi"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "rev.CpG.bed.gz", "wgbstools/hg19/rev.CpG.bed.gz"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "rev.CpG.bed.gz.tbi", "wgbstools/hg19/rev.CpG.bed.gz.tbi"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "CpG.chrome.size", "wgbstools/hg19/CpG.chrome.size"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "chrome.size", "wgbstools/hg19/chrome.size"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "genome.fa.gz", "wgbstools/hg19/genome.fa.gz"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "genome.fa.gz.fai", "wgbstools/hg19/genome.fa.gz.fai"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg19" / "genome.fa.gz.gzi", "wgbstools/hg19/genome.fa.gz.gzi"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "LICENSE.md", "wgbstools/LICENSE.md"),
        ],
        "exports": {"wgbstools_hg19_reference": "wgbstools/hg19"},
        "provenance": {
            "genome_build": "hg19", "asset_scope": "WGBSTools CpG dictionary and GRCh37 FASTA",
            "source": "bundled release-tree asset; original upstream filesystem path is intentionally not recorded",
            "license": "wgbstools/LICENSE.md",
            "coordinate_system": "CpG dictionary positions are frozen 0-based starts; DeMethFlow emits 0-based half-open BED without coordinate shifts",
            "cross_build_fallback": False,
        },
    },
    "WGBSTools-hg38-data": {
        "sources": [
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg38" / "reference_manifest.json", "wgbstools/hg38/reference_manifest.json"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg38" / "CpG.bed.gz", "wgbstools/hg38/CpG.bed.gz"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg38" / "CpG.bed.gz.csi", "wgbstools/hg38/CpG.bed.gz.csi"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg38" / "rev.CpG.bed.gz", "wgbstools/hg38/rev.CpG.bed.gz"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg38" / "rev.CpG.bed.gz.tbi", "wgbstools/hg38/rev.CpG.bed.gz.tbi"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg38" / "CpG.chrome.size", "wgbstools/hg38/CpG.chrome.size"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg38" / "chrome.size", "wgbstools/hg38/chrome.size"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg38" / "genome.fa", "wgbstools/hg38/genome.fa"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "hg38" / "genome.fa.fai", "wgbstools/hg38/genome.fa.fai"),
            (PROJECT_ROOT / "nextflow_ref" / "assets" / "wgbstools" / "LICENSE.md", "wgbstools/LICENSE.md"),
        ],
        "exports": {"wgbstools_hg38_reference": "wgbstools/hg38"},
        "provenance": {
            "genome_build": "hg38", "asset_scope": "WGBSTools CpG dictionary and GRCh38 FASTA",
            "source": "bundled release-tree asset; original upstream filesystem path is intentionally not recorded",
            "license": "wgbstools/LICENSE.md",
            "coordinate_system": "CpG dictionary positions are frozen 0-based starts; DeMethFlow emits 0-based half-open BED without coordinate shifts",
            "cross_build_fallback": False,
        },
    },
    "CelFEER-hg19-data": {
        "sources": [
            (
                PROJECT_ROOT / "nextflow_ref" / "assets" / "celfeer" / "references" / "hg19_cpg_ref.txt",
                "celfeer/hg19_cpg_ref.txt",
            ),
            (
                PROJECT_ROOT / "nextflow_ref" / "assets" / "celfeer" / "CelFEER-main" / "data" / "hg19_read_bins.txt",
                "celfeer/hg19_read_bins.txt",
            ),
        ],
        "exports": {
            "celfeer_hg19_cpg_reference": "celfeer/hg19_cpg_ref.txt",
            "celfeer_hg19_read_bins": "celfeer/hg19_read_bins.txt",
        },
        "provenance": {
            "genome_build": "hg19",
            "asset_scope": "CelFEER CpG reference and read bins",
            "cross_build_fallback": False,
        },
    },
    "CelFEER-hg38-data": {
        "sources": [
            (
                PROJECT_ROOT / "nextflow_ref" / "assets" / "celfeer" / "references" / "hg38_cpg_ref.txt",
                "celfeer/hg38_cpg_ref.txt",
            ),
            (
                PROJECT_ROOT / "nextflow_ref" / "assets" / "celfeer" / "CelFEER-main" / "data" / "read_bins.txt",
                "celfeer/read_bins.txt",
            ),
        ],
        "exports": {
            "celfeer_hg38_cpg_reference": "celfeer/hg38_cpg_ref.txt",
            "celfeer_hg38_read_bins": "celfeer/read_bins.txt",
        },
        "provenance": {
            "genome_build": "hg38",
            "asset_scope": "CelFEER CpG reference and read bins",
            "cross_build_fallback": False,
        },
    },
    "MethylBERT-hg19-data": {
        "sources": [
            (
                PROJECT_ROOT / "nextflow_ref" / "assets" / "methylbert" / "hg19" / "src",
                "methylbert/src",
            ),
            (
                PROJECT_ROOT / "nextflow_ref" / "assets" / "methylbert" / "hg19" / "data" / "hg19",
                "methylbert/data/hg19",
            ),
            (
                PROJECT_ROOT / "nextflow_ref" / "assets" / "methylbert" / "hg19" / "data" / "cell_type_match.json",
                "methylbert/data/cell_type_match.json",
            ),
        ],
        "exports": {
            "methylbert_reference_root": "methylbert",
        },
        "provenance": {
            "genome_build": "hg19",
            "asset_scope": "MethylBERT preprocessing source and GRCh37 genome cache",
            "required_cache_files": [
                "data/cell_type_match.json",
                "data/hg19/hg19_cpgs.csv",
                "data/hg19/hg19_genome.pk"
            ],
            "cross_build_fallback": False,
        },
    },
    "MethylBERT-hg38-data": {
        "sources": [
            (
                PROJECT_ROOT / "references" / "builtin.immune6.wgbs-native" / "1.1.0" / "artifacts" / "methylbert" / "src",
                "methylbert/src",
            ),
            (
                PROJECT_ROOT / "references" / "builtin.immune6.wgbs-native" / "1.1.0" / "artifacts" / "methylbert" / "data",
                "methylbert/data",
            ),
        ],
        "exports": {
            "methylbert_reference_root": "methylbert",
        },
        "provenance": {
            "genome_build": "hg38",
            "asset_scope": "MethylBERT preprocessing source and GRCh38 genome cache",
            "cross_build_fallback": False,
        },
    },
}


CORE_PATHS = [
    "VERSION",
    "demethflow",
    "demethflow.py",
    "demethflow_core",
    "scripts",
    "deconvolution",
    "runtime",
    "LICENSE",
    "CITATION.cff",
    "THIRD_PARTY_LICENSES.md",
    "licenses",
    "reports",
    "validation",
    "README.md",
    "OFFLINE_INSTALL.md",
    "RELEASE_READINESS.md",
    "WORK_PLAN.md",
    "HG19_NATIVE_WGBS.md",
    "NATIVE_WGBS_SIMULATION_VALIDATION.md",
    "CELFIE_REGION_CONTRACT.md",
    "EPIC_FROM_450K.md",
    "WGBS_DERIVED_ARRAY.md",
    "BAM_WGBS_INPUT.md",
]

REFERENCE_WORKFLOW_PATHS = [
    "README.md",
    "RELEASE_STATUS.md",
    "main.nf",
    "nextflow.config",
    "nextflow_schema.json",
    "run.sh",
    "modules",
    "bin",
    "params",
    "envs",
]


def build_release(
    destination: Path,
    *,
    bundles: list[ReferenceBundle],
    runtimes: list[str],
    include_build: bool,
    include_full_bundle: bool,
    data_modules: list[str] | None = None,
    resume: bool = False,
) -> dict:
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    index = {
        "schema": "demethflow-release-index-v2",
        "core_version": version(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": "linux-x86_64",
        "packages": [],
        "reference_disclosures": {},
    }
    core = _reuse_or_build_core(destination, resume=resume)
    index["packages"].append(core)
    for bundle in bundles:
        archive = destination / f"{bundle.reference_id}-{bundle.version}.tar.gz"
        if resume and archive.is_file():
            _verify_existing_archive(archive)
            sidecar = Path(f"{archive}.sha256")
        else:
            archive, sidecar = export_reference(bundle, destination)
        index["packages"].append(_package_record(archive, "reference", bundle.selector, _tree_bytes(bundle.root)))
        release = bundle.payload.get("release_validation", {})
        if bundle.status == "RELEASED_UNVALIDATED" or release.get("user_warning"):
            index["reference_disclosures"][bundle.selector] = {
                "status": bundle.status,
                "validation_type": release.get("validation_type"),
                "scientific_validation_status": release.get("scientific_validation_status"),
                "independent_real_world_validation": release.get(
                    "independent_real_world_validation"
                ),
                "independent_real_world_validation_required_for_release": release.get(
                    "independent_real_world_validation_required_for_release"
                ),
                "scientific_claim_scope": release.get("scientific_claim_scope"),
                "independent_epic_v1_truth_available": release.get(
                    "independent_epic_v1_truth_available"
                ),
                "scientific_claims_permitted": release.get("scientific_claims_permitted"),
                "warning": release.get("user_warning"),
                "tool_warnings": release.get("tool_warnings"),
                "tool_validation_evidence": release.get("tool_validation_evidence"),
                "ru_policy": release.get("ru_policy"),
                "ru_default_tools": [
                    name for name, capability in bundle.tools.items()
                    if capability.status == "RELEASED_UNVALIDATED"
                    and capability.execution_policy == "normal"
                ],
                "explicit_only_tools": [
                    name for name, capability in bundle.tools.items()
                    if capability.execution_policy == "explicit_only"
                ],
            }
    required_data_modules = list(data_modules or [])
    # BAM is a core WGBS input contract rather than a reference capability.
    # A full offline release must therefore carry both build-locked dictionaries
    # and FASTAs even when its selected reference subset happens to use one
    # build. Minimal releases can request the exact module explicitly.
    if include_full_bundle:
        for module_name in ("WGBSTools-hg19-data", "WGBSTools-hg38-data"):
            if module_name not in required_data_modules:
                required_data_modules.append(module_name)
    for bundle in bundles:
        for capability in bundle.tools.values():
            # A PARTIAL bundle may intentionally contain quarantined assets
            # whose external data module does not exist yet.  Those candidates
            # must not block a minimal offline package for independent READY
            # tools. Explicit --data-module requests remain authoritative.
            if capability.status not in {"READY", "RELEASED_UNVALIDATED"}:
                continue
            for module_name in capability.data_modules:
                if module_name not in required_data_modules:
                    required_data_modules.append(module_name)
    for module_name in required_data_modules:
        definition = DATA_MODULE_SOURCES.get(module_name)
        if not definition:
            raise DeMethFlowError(f"Unknown data module: {module_name}")
        sources = definition["sources"]
        for source, _ in sources:
            if not source.exists():
                raise DeMethFlowError(f"Data module payload source is missing: {source}")
        archive = destination / f"DeMethFlow-data-{module_name}-{version()}.tar.gz"
        if resume and archive.is_file():
            _verify_existing_archive(archive)
        else:
            _build_module_archive(
                archive,
                module_id=module_name,
                module_type="tool-data",
                module_version=version(),
                sources=sources,
                exports=definition["exports"],
                provenance=definition.get("provenance"),
            )
        index["packages"].append(
            _package_record(archive, "tool-data", module_name, sum(_tree_bytes(path) for path, _ in sources))
        )
    for runtime_name in runtimes:
        filename = RUNTIME_FILES.get(runtime_name)
        if not filename:
            raise DeMethFlowError(f"Unknown runtime module: {runtime_name}")
        source = PROJECT_ROOT / "runtimes" / "sifs" / filename
        if not source.is_file():
            raise DeMethFlowError(f"Runtime image is not installed in the release tree: {source}")
        archive = destination / f"DeMethFlow-runtime-{runtime_name}-{version()}.tar.gz"
        runtime_sources = [(source, f"sifs/{filename}"), *RUNTIME_TOOL_SOURCES.get(runtime_name, [])]
        for runtime_source, _ in runtime_sources:
            if not runtime_source.exists():
                raise DeMethFlowError(f"Runtime payload source is missing: {runtime_source}")
        if resume and archive.is_file():
            _verify_existing_archive(archive)
        else:
            _build_module_archive(
                archive,
                module_id=runtime_name,
                module_type="runtime",
                module_version=version(),
                sources=runtime_sources,
            )
        index["packages"].append(_package_record(archive, "runtime", runtime_name, source.stat().st_size))
    if include_build:
        assets_sources = [
            (PROJECT_ROOT / "nextflow_ref" / "assets", "assets"),
            (PROJECT_ROOT / "nextflow_ref" / "tools", "tools"),
        ]
        for source, _ in assets_sources:
            if not source.is_dir():
                raise DeMethFlowError(f"Build asset source is missing: {source}")
        assets_archive = destination / f"DeMethFlow-build-assets-{version()}.tar.gz"
        if resume and assets_archive.is_file():
            _verify_existing_archive(assets_archive)
        else:
            _build_module_archive(
                assets_archive,
                module_id="build-assets",
                module_type="build-assets",
                module_version=version(),
                sources=assets_sources,
            )
        index["packages"].append(
            _package_record(assets_archive, "build-assets", "build-assets", sum(_tree_bytes(path) for path, _ in assets_sources))
        )
        build_sif = PROJECT_ROOT / "nextflow_ref" / "containers" / "deconvolution.sif"
        if not build_sif.is_file():
            raise DeMethFlowError(f"Build runtime is missing: {build_sif}")
        runtime_archive = destination / f"DeMethFlow-build-runtime-{version()}.tar.gz"
        if resume and runtime_archive.is_file():
            _verify_existing_archive(runtime_archive)
        else:
            _build_module_archive(
                runtime_archive,
                module_id="build-runtime",
                module_type="build-runtime",
                module_version=version(),
                sources=[(build_sif, "containers/deconvolution.sif")],
            )
        index["packages"].append(
            _package_record(runtime_archive, "build-runtime", "build-runtime", build_sif.stat().st_size)
        )
    atomic_write_json(destination / "release-index.json", index)
    release_digest = sha256_file(destination / "release-index.json")
    (destination / "release-index.json.sha256").write_text(
        f"{release_digest}  release-index.json\n", encoding="utf-8"
    )
    if include_full_bundle:
        full = destination / f"DeMethFlow-full-offline-{version()}.tar"
        if resume and full.is_file():
            _verify_existing_archive(full)
        else:
            with tarfile.open(full, "w") as handle:
                for package in index["packages"]:
                    archive = destination / package["filename"]
                    handle.add(archive, arcname=archive.name, recursive=False)
                    sidecar = Path(f"{archive}.sha256")
                    if sidecar.is_file():
                        handle.add(sidecar, arcname=sidecar.name, recursive=False)
                handle.add(destination / "release-index.json", arcname="release-index.json", recursive=False)
                handle.add(destination / "release-index.json.sha256", arcname="release-index.json.sha256", recursive=False)
            full_sha = sha256_file(full)
            Path(f"{full}.sha256").write_text(f"{full_sha}  {full.name}\n", encoding="utf-8")
        index["full_offline"] = _package_record(full, "aggregate", "full-offline", full.stat().st_size)
        atomic_write_json(destination / "release-index.json", index)
        release_digest = sha256_file(destination / "release-index.json")
        (destination / "release-index.json.sha256").write_text(
            f"{release_digest}  release-index.json\n", encoding="utf-8"
        )
    return index


def _build_core(destination: Path) -> dict:
    archive = destination / f"DeMethFlow-core-{version()}.tar.gz"
    if archive.exists():
        raise DeMethFlowError(f"Refusing to overwrite existing archive: {archive}")
    core_sources = [PROJECT_ROOT / relative for relative in CORE_PATHS]
    core_sources.extend(PROJECT_ROOT / "nextflow_ref" / relative for relative in REFERENCE_WORKFLOW_PATHS)
    _validate_relocatable_symlinks(core_sources, PROJECT_ROOT)
    top = f"DeMethFlow-{version()}"
    with tarfile.open(archive, "w:gz", compresslevel=1) as handle:
        for relative in CORE_PATHS:
            source = PROJECT_ROOT / relative
            if source.exists():
                handle.add(source, arcname=f"{top}/{relative}", filter=_core_filter)
        for relative in REFERENCE_WORKFLOW_PATHS:
            source = PROJECT_ROOT / "nextflow_ref" / relative
            if source.exists():
                handle.add(source, arcname=f"{top}/nextflow_ref/{relative}", filter=_core_filter)
    digest = sha256_file(archive)
    Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    installed = sum(_tree_bytes(PROJECT_ROOT / path) for path in CORE_PATHS if (PROJECT_ROOT / path).exists())
    installed += sum(
        _tree_bytes(PROJECT_ROOT / "nextflow_ref" / path)
        for path in REFERENCE_WORKFLOW_PATHS
        if (PROJECT_ROOT / "nextflow_ref" / path).exists()
    )
    return _package_record(archive, "core", "core", installed)


def _reuse_or_build_core(destination: Path, *, resume: bool) -> dict:
    archive = destination / f"DeMethFlow-core-{version()}.tar.gz"
    if resume and archive.is_file():
        _verify_existing_archive(archive)
        installed = sum(_tree_bytes(PROJECT_ROOT / path) for path in CORE_PATHS if (PROJECT_ROOT / path).exists())
        installed += sum(
            _tree_bytes(PROJECT_ROOT / "nextflow_ref" / path)
            for path in REFERENCE_WORKFLOW_PATHS
            if (PROJECT_ROOT / "nextflow_ref" / path).exists()
        )
        return _package_record(archive, "core", "core", installed)
    return _build_core(destination)


def _verify_existing_archive(archive: Path) -> None:
    sidecar = Path(f"{archive}.sha256")
    if not sidecar.is_file():
        raise DeMethFlowError(f"Cannot resume without checksum sidecar: {sidecar}")
    expected = sidecar.read_text(encoding="utf-8").split()[0] if sidecar.read_text(encoding="utf-8").split() else ""
    actual = sha256_file(archive)
    if expected != actual:
        raise DeMethFlowError(f"Cannot resume with corrupted archive: {archive}")


def _validate_relocatable_symlinks(sources: list[Path], root: Path) -> None:
    root = root.resolve()
    for source in sources:
        if not source.exists() and not source.is_symlink():
            continue
        items = [source]
        if source.is_dir():
            items.extend(source.rglob("*"))
        for item in items:
            # Keep validation aligned with _core_filter.  Interval-repair
            # workspaces are deliberately excluded from the distributable
            # core and contain Nextflow-managed absolute work symlinks.
            # Their compact, immutable evidence is bundled with references.
            try:
                relative_item = item.relative_to(root)
            except ValueError:
                relative_item = None
            if relative_item is not None and relative_item.parts[:2] == (
                "reports",
                "metdecode_interval_repair",
            ):
                continue
            if not item.is_symlink():
                continue
            target = Path(os.readlink(item))
            if target.is_absolute():
                raise DeMethFlowError(f"Core archive contains an absolute symlink: {item} -> {target}")
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise DeMethFlowError(f"Core archive symlink escapes the product tree: {item} -> {target}") from exc
            if not resolved.exists():
                raise DeMethFlowError(f"Core archive contains a broken symlink: {item} -> {target}")


def _core_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    forbidden = {"__pycache__", ".nextflow", ".git", ".codex", ".agents"}
    if any(part in forbidden for part in parts) or info.name.endswith((".pyc", ".pyo")):
        return None
    # Ship the signed/condensed immune6 reports, not the ~1 GB validation
    # workspaces containing regenerated bulk matrices and raw intermediate
    # outputs.  Detailed evidence is also copied into each reference archive.
    try:
        reports_index = parts.index("reports")
    except ValueError:
        reports_index = -1
    # The release-specific hg19 audit, regression and capability snapshots are
    # detached companion evidence. Embedding them in the core would always make
    # them describe an older archive: the exact-archive audit can only be
    # produced after that archive has been built. Keep the scenario-level
    # scientific reports below epithelial/ and breast/, but omit every volatile
    # top-level file under reports/hg19_native from the downloadable core.
    if (
        reports_index >= 0
        and parts[reports_index + 1:reports_index + 2] == ("hg19_native",)
        and len(parts) == reports_index + 3
        and not info.isdir()
    ):
        return None
    # Release-wide native-WGBS audits and matrices are post-build companion
    # evidence. Keep immutable per-scenario reports under hg19/ and hg38/, but
    # detach top-level summaries and the candidate-audit subtree so a core
    # archive never embeds evidence describing older core bytes.
    if reports_index >= 0 and parts[reports_index + 1:reports_index + 2] == ("native_wgbs",):
        relative = parts[reports_index + 2:]
        if (len(relative) == 1 and not info.isdir()) or relative[:1] == ("release_candidate",):
            return None
    # MetDecode interval-repair workspaces contain Nextflow work symlinks and
    # are post-build audit evidence, not distributable core payload. The bound
    # compact reports live inside the finalized reference archives instead.
    if reports_index >= 0 and parts[reports_index + 1:reports_index + 2] == ("metdecode_interval_repair",):
        return None
    if (
        reports_index >= 0
        and parts[reports_index + 1:reports_index + 2] == ("immune6_v1_2",)
        and len(parts) > reports_index + 2
        and info.isdir()
    ):
        return None
    return info


def _build_module_archive(
    archive: Path,
    *,
    module_id: str,
    module_type: str,
    module_version: str,
    sources: list[tuple[Path, str]],
    exports: dict[str, str] | None = None,
    provenance: dict | None = None,
) -> None:
    if archive.exists():
        raise DeMethFlowError(f"Refusing to overwrite existing archive: {archive}")
    stage = Path(tempfile.mkdtemp(prefix="demethflow-module-", dir=archive.parent))
    try:
        payload = stage / "payload"
        payload.mkdir()
        for source, relative in sources:
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(
                    source,
                    target,
                    symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                )
            else:
                shutil.copy2(source, target)
        files = [
            {
                "path": path.relative_to(payload).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(payload.rglob("*"))
            if path.is_file()
        ]
        manifest = {
            "schema": "demethflow-module-v2",
            "platform": _module_platform(module_type),
            "module_id": module_id,
            "module_type": module_type,
            "version": module_version,
            "installed_bytes": sum(item["bytes"] for item in files),
            "compatibility": {
                "core_api": CORE_API,
                "reference_schema": REFERENCE_SCHEMA,
                "runtime_api": RUNTIME_API,
            },
            "files": files,
        }
        if exports:
            manifest["exports"] = exports
        if provenance:
            manifest["provenance"] = provenance
        atomic_write_json(stage / "module.json", manifest)
        temporary = archive.with_name(f".{archive.name}.partial")
        with tarfile.open(temporary, "w:gz", compresslevel=1) as handle:
            handle.add(stage / "module.json", arcname="module.json", recursive=False)
            handle.add(payload, arcname="payload", recursive=True)
        os.replace(temporary, archive)
        digest = sha256_file(archive)
        Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _module_platform(module_type: str) -> str:
    """Return the install architecture for an exported module type."""
    if module_type in {"reference", "tool-data", "build-assets"}:
        return "noarch"
    if module_type in {"runtime", "build-runtime", "launcher"}:
        return "linux-x86_64"
    raise DeMethFlowError(f"Unknown module type for platform declaration: {module_type}")


def _package_record(archive: Path, kind: str, module: str, installed_bytes: int) -> dict:
    return {
        "filename": archive.name,
        "kind": kind,
        "module": module,
        "compressed_bytes": archive.stat().st_size,
        "installed_bytes": installed_bytes,
        "sha256": sha256_file(archive),
    }


def _tree_bytes(path: Path) -> int:
    if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix not in {".pyc", ".pyo"}
    )
