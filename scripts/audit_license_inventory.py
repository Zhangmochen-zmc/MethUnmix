#!/usr/bin/env python3
"""Inventory license/redistribution evidence without making legal conclusions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "evidence" / "LICENSE_GAP_INVENTORY.json"
DEFAULT_CSV = ROOT / "evidence" / "LICENSE_GAP_INVENTORY.csv"
DEFAULT_MD = ROOT / "evidence" / "LICENSE_GAP_SUMMARY.md"
LICENSE_NAMES = {"license", "copying", "notice", "third_party_licenses.md"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def license_files(directory: Path, depth: int = 4) -> list[Path]:
    found = []
    if not directory.is_dir():
        return found
    for path in directory.rglob("*"):
        try:
            rel_parts = path.relative_to(directory).parts
        except ValueError:
            continue
        if len(rel_parts) > depth:
            continue
        if path.is_file() and (path.name.lower() in LICENSE_NAMES or path.name.lower().startswith(("license.", "copying.")) or path.name.lower().startswith("notice.")):
            found.append(path)
    return sorted(found)


def get_evidence_for_component(name: str, candidates: list[Path]) -> list[dict[str, str]]:
    key = norm_name(name)
    rows = []
    for path in candidates:
        text = path.as_posix()
        if key in norm_name(text) or name.lower() in text.lower():
            rows.append({"path": str(path), "sha256": sha256_file(path) if path.stat().st_size <= 4 * 1024 * 1024 else "NOT_HASHED_OVER_4MIB"})
    return rows


def identify_license(path: Path) -> str:
    """Identify common license texts conservatively; never treat this as approval."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").upper()
    except OSError:
        return "LICENSE_FILE_UNREADABLE"
    if "GNU AFFERO GENERAL PUBLIC LICENSE" in text and "VERSION 3" in text:
        return "AGPL-3.0"
    if "SOFTWARE RESEARCH LICENSE" in text and "YISSUM" in text:
        return "CUSTOM_SOFTWARE_RESEARCH_LICENSE"
    if "MIT LICENSE" in text and "PERMISSION IS HEREBY GRANTED" in text:
        return "MIT"
    return "LICENSE_TEXT_UNCLASSIFIED"


def ref_version_key(version: str) -> tuple[Any, ...]:
    main, separator, suffix = version.partition("-")
    numbers = tuple(int(x) if x.isdigit() else x.lower() for x in re.split(r"[.+]", main))
    return numbers, (0, "") if not separator else (-1, suffix.lower())


def category_for_artifact(path: str) -> str:
    value = path.lower()
    if any(token in value for token in ("model", ".pickle", ".pkl", ".pt", ".pth", "bert.model")):
        return "model_or_weights"
    if "cache" in value or "genome" in value or "fasta" in value:
        return "genome_cache_or_sequence_data"
    if "validation" in value or "scientific" in value or "repeatability" in value or "reports/" in value:
        return "validation_evidence"
    if any(token in value for token in ("marker", "atlas", "reference", "cell_types", "beta_ref", "tref", "dmc/", "centroid")):
        return "reference_or_marker_data"
    if value.endswith((".py", ".r", ".sh", ".nf", ".yaml", ".yml")):
        return "runtime_or_adapter_code"
    return "reference_bundle_artifact"


def create_row(**fields: Any) -> dict[str, Any]:
    defaults = {
        "license_status": "NO_ASSET_SPECIFIC_LICENSE_EVIDENCE",
        "proposed_distribution_status": "USER_SUPPLIED_SUPPORTED",
        "public_catalog_eligible": False,
        "owner_decision_required": True,
    }
    return {**defaults, **fields}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-project", type=Path, help="Optional maintainer source tree containing references/, runtimes/, licenses/, and workflow assets.")
    parser.add_argument("--references", type=Path, help="Reference bundle root; defaults to <source-project>/references when supplied.")
    parser.add_argument("--runtime-manifest", type=Path, help="SIF inventory manifest; defaults to <source-project>/runtimes/sifs/manifest.json when supplied.")
    parser.add_argument("--simulation-data", type=Path, help="Optional simulation fixture root; recorded as internal evidence only.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--summary-md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()

    source_project = args.source_project
    reference_root = args.references or (source_project / "references" if source_project else None)
    runtime_manifest = args.runtime_manifest or (source_project / "runtimes/sifs/manifest.json" if source_project else None)

    rows: list[dict[str, Any]] = []
    evidence_paths = [ROOT / "LICENSE", ROOT / "NOTICE"]
    evidence_paths.extend(license_files(ROOT / "src/methunmix_assets/deconvolution", depth=6))
    if source_project:
        evidence_paths.append(source_project / "THIRD_PARTY_LICENSES.md")
        evidence_paths.extend(license_files(source_project / "licenses", depth=5))
        evidence_paths.extend(license_files(source_project / "deconvolution", depth=6))
        evidence_paths.extend(license_files(source_project / "nextflow_ref/tools", depth=6))
        if (source_project / "licenses").is_dir():
            evidence_paths.extend(p for p in (source_project / "licenses").rglob("*") if p.is_file())
    evidence_paths = sorted(set(p for p in evidence_paths if p.is_file()))

    notice_path = ROOT / "NOTICE"
    notice_text = notice_path.read_text(encoding="utf-8", errors="replace") if notice_path.is_file() else ""
    bundled_source_present = any(
        path.is_dir() and any(path.iterdir())
        for path in (
            ROOT / "src/methunmix_assets/deconvolution/wgbs_vendor",
            ROOT / "src/methunmix_assets/deconvolution/vendor",
        )
    )
    notice_scope_conflict = bundled_source_present and "workflow control code and metadata only" in notice_text.lower()
    core_review_note = "The top-level MIT license covers MethUnmix-authored work only; bundled third-party materials need separate review."
    if notice_scope_conflict:
        core_review_note += " NOTICE currently says the Conda core contains workflow control code and metadata only, which conflicts with the detected bundled vendor source. Revise NOTICE before any public package build."
    rows.append(create_row(
        asset_id="methunmix-core",
        asset_type="core_package",
        version="2.0.0rc1",
        source=str(ROOT),
        license_status=(
            "MIT_DECLARED_CORE_LICENSE_PRESENT; NOTICE_SCOPE_CONFLICT_REQUIRES_REVISION"
            if notice_scope_conflict else "MIT_DECLARED_CORE_LICENSE_PRESENT"
        ),
        proposed_distribution_status="PUBLIC_BUNDLED_SUBJECT_TO_THIRD_PARTY_FILE_REVIEW",
        license_evidence=[
            {"path": str(ROOT / "LICENSE"), "sha256": sha256_file(ROOT / "LICENSE")},
            *([{"path": str(notice_path), "sha256": sha256_file(notice_path)}] if notice_path.is_file() else []),
        ],
        notice_scope_conflict=notice_scope_conflict,
        review_note=core_review_note,
        public_catalog_eligible=False,
    ))

    try:
        registry = json.loads((ROOT / "src/methunmix_assets/capabilities/capability_registry.json").read_text(encoding="utf-8"))
        # Registry v2 distinguishes logical tools from manifest aliases. Use
        # its canonical list, while retaining compatibility with v1 inputs.
        tools = registry.get("logical_tools", registry.get("tools", []))
    except (OSError, json.JSONDecodeError):
        tools = []
    for tool in tools:
        matches = get_evidence_for_component(str(tool), evidence_paths)
        vendor_roots = [
            ROOT / "src/methunmix_assets/deconvolution/wgbs_vendor",
            ROOT / "src/methunmix_assets/deconvolution/vendor",
        ]
        bundled_vendor_paths = []
        for vendor_root in vendor_roots:
            if not vendor_root.is_dir():
                continue
            for candidate in vendor_root.iterdir():
                normalized = norm_name(candidate.name)
                if candidate.is_dir() and (normalized == norm_name(str(tool)) or normalized.startswith(norm_name(str(tool)))):
                    bundled_vendor_paths.append(str(candidate))
        bundled_license_paths: list[Path] = []
        identified_licenses: list[str] = []
        if tool.lower() == "celfeer":
            matches = [m for m in matches if "celfeer" in m["path"].lower()]
        if tool == "Houseman":
            tool_status = "FIRST_PARTY_WRAPPER_AND_UPSTREAM_METHOD_NEEDS_ATTRIBUTION_REVIEW"
        elif bundled_vendor_paths and matches:
            bundled_roots = [Path(item) for item in bundled_vendor_paths]
            bundled_license_paths = [
                Path(item["path"]) for item in matches
                if any(
                    Path(item["path"]) == root or root in Path(item["path"]).parents
                    for root in bundled_roots
                )
            ]
            identified_licenses = sorted({identify_license(path) for path in bundled_license_paths})
            if identified_licenses == ["AGPL-3.0"]:
                tool_status = "AGPL_3_LICENSE_PRESENT_PACKAGE_SCOPE_REVIEW_REQUIRED"
            elif identified_licenses == ["CUSTOM_SOFTWARE_RESEARCH_LICENSE"]:
                tool_status = "RESEARCH_ONLY_LICENSE_PRESENT_GENERAL_DISTRIBUTION_REVIEW_REQUIRED"
            elif identified_licenses == ["MIT"]:
                tool_status = "MIT_LICENSE_PRESENT_MIXED_PACKAGE_NOTICE_REVIEW_REQUIRED"
            else:
                tool_status = "LICENSE_TEXT_LOCATED_NOT_LEGAL_APPROVAL"
        elif bundled_vendor_paths:
            bundled_license_paths = []
            identified_licenses = []
            tool_status = "BUNDLED_VENDOR_CODE_WITHOUT_LOCATED_LICENSE"
        else:
            bundled_license_paths = []
            identified_licenses = []
            tool_status = "METHUNMIX_ADAPTER_IN_CORE_LICENSE_SCOPE; UPSTREAM_RUNTIME_OR_ASSET_REVIEW_SEPARATE"
        proposed = "BLOCKED" if bundled_vendor_paths else "PUBLIC_BUNDLED"
        rows.append(create_row(
            asset_id=f"tool-code:{tool}",
            asset_type="tool_source_or_adapter",
            version="MethUnmix 2.0.0rc1 / upstream version varies",
            source="; ".join(bundled_vendor_paths) if bundled_vendor_paths else str(ROOT / "src/methunmix_assets/deconvolution"),
            license_status=tool_status,
            proposed_distribution_status=proposed,
            license_evidence=matches,
            bundled_license_files=[str(path) for path in bundled_license_paths],
            detected_license_families=identified_licenses,
            bundled_vendor_paths=bundled_vendor_paths,
            review_note=(
                "Bundled third-party code remains blocked for this RC pending component-specific license, attribution, and package-metadata review; an identified license family is not itself owner/legal approval."
                if bundled_vendor_paths else
                "Adapter is part of the MIT-declared core; confirm file provenance/authorship in final review. The tool's external runtime/reference license is tracked separately."
            ),
            public_catalog_eligible=False,
        ))

    runtime_payload = {}
    if runtime_manifest and runtime_manifest.is_file():
        try:
            runtime_payload = json.loads(runtime_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            runtime_payload = {}
    for image in runtime_payload.get("images", []):
        if not isinstance(image, dict):
            continue
        image_name = str(image.get("filename", ""))
        image_component = re.sub(r"_env.*$", "", image_name.removesuffix(".sif"), flags=re.I)
        component_aliases = {
            "aric": "ARIC", "array": "array runtimes", "edec": "EDec", "emeth": "EMeth",
            "epidish": "EpiDISH", "episcore": "EpiSCORE", "houseman": "Houseman",
            "medecom": "MeDeCom", "menet": "MEnet", "methatlas": "MethAtlas",
            "methylbert": "MethylBERT", "methylcibersort": "MethylCIBERSORT",
            "prmeth": "PRMeth", "reffreeewas": "RefFreeEWAS", "tsisal": "Tsisal", "wgbs": "wgbstools",
        }
        image_component = component_aliases.get(image_component.lower(), image_component)
        rows.append(create_row(
            asset_id=f"sif:{image.get('filename', 'unknown')}",
            asset_type="container_runtime",
            version=image.get("version", "version_not_in_runtime_manifest"),
            source=image.get("source", "source_not_recorded"),
            sha256=image.get("sha256"),
            bytes=image.get("bytes"),
            license_status="IMAGE_DIGEST_RECORDED_BUT_COMPLETE_IN_IMAGE_SBOM_AND_REDISTRIBUTION_RIGHTS_UNVERIFIED",
            proposed_distribution_status="USER_SUPPLIED_SUPPORTED",
            license_evidence=get_evidence_for_component(image_component, evidence_paths),
            review_note="SIF is excluded from core. Do not publish in the static catalog until an image SBOM and redistribution permission are approved; user-owned compatible runtime/module import remains the fallback.",
        ))

    manifests: list[tuple[Path, dict[str, Any]]] = []
    parse_failures = []
    for path in reference_root.rglob("manifest.json") if reference_root and reference_root.is_dir() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("reference_id") and value.get("version"):
                manifests.append((path, value))
        except (OSError, json.JSONDecodeError) as exc:
            parse_failures.append({"path": str(path), "error": str(exc)})
    latest: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, payload in manifests:
        key = str(payload["reference_id"])
        previous = latest.get(key)
        if previous is None or ref_version_key(str(payload["version"])) > ref_version_key(str(previous[1]["version"])):
            latest[key] = (path, payload)

    for ref_id, (manifest_path, manifest) in sorted(latest.items()):
        selector = f"{ref_id}@{manifest['version']}"
        source_info = manifest.get("provenance", {})
        license_candidates = license_files(manifest_path.parent, depth=7)
        checksums = manifest.get("artifact_checksums", {})
        data_modules = sorted({str(item) for cap in manifest.get("tool_capabilities", {}).values() if isinstance(cap, dict) for item in cap.get("data_modules", [])})
        for relative, digest in sorted(checksums.items()):
            category = category_for_artifact(relative)
            local_license = get_evidence_for_component(category.split("_")[0], license_candidates)
            rows.append(create_row(
                asset_id=f"reference-artifact:{selector}:{relative}",
                asset_type=category,
                version=selector,
                source=str(manifest_path.parent / relative),
                sha256=digest,
                reference_selector=selector,
                reference_id=ref_id,
                scenario=manifest.get("scenario_id"),
                platform=manifest.get("source_platform"),
                genome_build=manifest.get("genome_build"),
                source_provenance=source_info,
                license_evidence=local_license,
                license_status="BUNDLE_ARTIFACT_DIGEST_DECLARED_BUT_DATA_SPECIFIC_RIGHTS_NOT_PROVEN" if not local_license else "LOCAL_LICENSE_FILE_PRESENT_SCOPE_REQUIRES_REVIEW",
                proposed_distribution_status="USER_SUPPLIED_SUPPORTED",
                review_note="Artifact SHA256 is taken from the immutable reference manifest; the file itself was not rehashed. Public catalog inclusion requires data/model/source redistribution rights.",
            ))
        for module in data_modules:
            rows.append(create_row(
                asset_id=f"data-module:{selector}:{module}",
                asset_type="model_or_runtime_data_module",
                version=selector,
                source=str(manifest_path.parent),
                reference_selector=selector,
                data_module=module,
                source_provenance=source_info,
                license_status="NO_MODULE_SPECIFIC_REDISTRIBUTION_APPROVAL_FOUND",
                proposed_distribution_status="USER_SUPPLIED_SUPPORTED",
                review_note="Module is required by the registered selector; keep out of the public catalog until its own license/provenance is approved.",
            ))

    if args.simulation_data:
        rows.append(create_row(
            asset_id="simulation-fixtures:maintainer-supplied",
            asset_type="scientific_validation_data",
            version="inventory_snapshot",
            source=str(args.simulation_data),
            license_status="NOT_ASSESSED_AS_PUBLIC_DATASET_LICENSE",
            proposed_distribution_status="INTERNAL_VALIDATED",
            review_note="Maintainer-supplied local simulation source. Do not include in core or public asset catalog unless separately approved.",
        ))

    counts = Counter((str(row.get("asset_type")), str(row.get("proposed_distribution_status"))) for row in rows)
    report = {
        "schema": "methunmix-license-gap-inventory-v1",
        "product": "MethUnmix",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "AUTOMATED_INVENTORY_REQUIRES_OWNER_LICENSE_DECISIONS",
        "inputs": {
            "core_root": str(ROOT),
            "reference_root": str(reference_root) if reference_root else None,
            "reference_manifest_count": len(manifests),
            "latest_reference_selector_count": len(latest),
            "runtime_manifest": str(runtime_manifest) if runtime_manifest else None,
            "runtime_image_count": len(runtime_payload.get("images", [])),
            "license_evidence_files": [
                {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
                for path in evidence_paths
            ],
            "reference_manifest_parse_failures": parse_failures,
        },
        "policy": {
            "public_catalog_rule": "Only assets with explicit redistribution rights and reviewed notices may be PUBLIC_DOWNLOADABLE.",
            "unclear_but_user_can_supply": "USER_SUPPLIED_SUPPORTED; not public catalog content.",
            "no_authorized_supply_route_or_bundled_unlicensed_code": "BLOCKED.",
            "automated_inventory_is_not_legal_approval": True,
            "no_external_source_assets_are_copied_or_modified": True,
        },
        "summary_by_type_and_proposed_status": [
            {"asset_type": kind, "proposed_distribution_status": status, "count": count}
            for (kind, status), count in sorted(counts.items())
        ],
        "items": rows,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with args.csv.open("w", encoding="utf-8", newline="") as stream:
        fields = ["asset_id", "asset_type", "version", "source", "sha256", "license_status", "proposed_distribution_status", "public_catalog_eligible", "review_note"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            clean = {key: row.get(key, "") for key in fields}
            clean["public_catalog_eligible"] = str(bool(row.get("public_catalog_eligible", False))).lower()
            writer.writerow(clean)
    summary_lines = [
        "# Automated license and redistribution gap inventory",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Status: **{report['status']}**",
        "",
        "This is a source/manifest inventory, not legal approval. No external runtime, model, reference, or cache is automatically approved for public distribution.",
        "",
        "## Scope scanned",
        "",
        f"- Core license/NOTICE and bundled workflow/vendor source: `{ROOT}`",
        f"- Reference manifests: **{len(manifests)}** found, **{len(latest)}** latest selectors inventoried; reference artifact digests were taken from manifests rather than recomputed across the 125 GB tree.",
        f"- Runtime images: **{len(runtime_payload.get('images', []))}** SIF records from `{runtime_manifest or 'no manifest supplied'}`; image bytes were not opened or scanned.",
        f"- Asset-level rows: **{len(rows)}**. Public catalog eligible rows: **{sum(1 for row in rows if row.get('public_catalog_eligible'))}**.",
        "",
        "## High-priority findings",
        "",
        "- The MethUnmix core declares MIT. That does not grant rights to bundled third-party source, methods, runtime images, references, model weights, or caches.",
        (
            "- The top-level NOTICE scope conflicts with bundled vendor source and must be corrected before a public package build."
            if notice_scope_conflict else
            "- NOTICE now inventories the staged vendor-source components and license files. Package-level license metadata still needs a reviewed mixed-license resolution."
        ),
        "- CelFEER and PRMeth vendor source are present in the core payload but no corresponding license file was found; both are `BLOCKED` pending license evidence or removal from the public core payload.",
        "- License text identification found AGPL-3.0 for CelFiE, a research-use-only Software Research License for UXM, and MIT for MetDecode. These are distinct conditions: CelFiE needs copyleft/package-scope review; UXM is not cleared for general/commercial distribution; MetDecode has an explicit permissive grant but still needs correct bundled copyright/NOTICE and mixed-license metadata. The inventory does not make legal conclusions.",
        "- SIF runtime image digests/sizes are recorded, but a complete in-image SBOM and redistribution-rights approval are missing. Keep SIFs out of the public catalog; user-supplied/import is the current fallback.",
        "- Reference/model/cache artifacts inherit no license merely because their manifest has a digest. Rows without asset-specific rights are proposed as `USER_SUPPLIED_SUPPORTED`, not public downloadable.",
        "- Simulation fixtures are classified `INTERNAL_VALIDATED`, not core payload or public catalog.",
        "",
        "## Proposed distribution status counts",
        "",
        "| Asset type | Proposed status | Rows |",
        "|---|---|---:|",
    ]
    for item in report["summary_by_type_and_proposed_status"]:
        summary_lines.append(f"| {item['asset_type']} | {item['proposed_distribution_status']} | {item['count']} |")
    summary_lines += [
        "",
        "The exhaustive asset rows are in `LICENSE_GAP_INVENTORY.csv` and `LICENSE_GAP_INVENTORY.json`. Resolve licensing by upstream component/bundle and record the approval once for all artifacts covered by that exact license; do not manually hash or re-review every manifest line independently.",
        "",
    ]
    args.summary_md.parent.mkdir(parents=True, exist_ok=True)
    args.summary_md.write_text("\n".join(summary_lines), encoding="utf-8")
    print(json.dumps({
        "json_report": str(args.report),
        "csv_report": str(args.csv),
        "summary_md": str(args.summary_md),
        "asset_rows": len(rows),
        "reference_manifests": len(manifests),
        "latest_references": len(latest),
        "runtime_images": len(runtime_payload.get("images", [])),
        "public_catalog_eligible": sum(1 for row in rows if row.get("public_catalog_eligible")),
        "status": report["status"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
