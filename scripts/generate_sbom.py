#!/usr/bin/env python3
"""Generate a digest-bound, explicitly partial CycloneDX SBOM for core RC artifacts.

This inventory never claims to cover external SIFs, references, models, genome
caches, or unresolved transitive Conda dependencies. Those require separate
asset/container SBOMs and the exact resolved build environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "dist_source_static_catalog_rc1_helper_fix" / "methunmix-2.0.0rc1.tar.gz"
DEFAULT_WHEEL = ROOT / "dist_conda_static_catalog_rc1_clean_helper_fix" / "methunmix-2.0.0rc1-py3-none-any.whl"
DEFAULT_INVENTORY = ROOT / "evidence/LICENSE_GAP_INVENTORY.json"
DEFAULT_OUTPUT = ROOT / "evidence/SBOM_CORE_RC.json"
PRODUCT_REF = "pkg:pypi/methunmix@2.0.0rc1"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def property_value(name: str, value: object) -> dict[str, str]:
    return {"name": name, "value": str(value)}


def _artifact_component(path: Path, artifact_type: str) -> dict[str, Any]:
    digest = sha256_file(path)
    return {
        "type": "file",
        "bom-ref": f"artifact:{artifact_type}:{digest}",
        "name": path.name,
        "version": "2.0.0rc1",
        "hashes": [{"alg": "SHA-256", "content": digest}],
        "properties": [
            property_value("methunmix:artifactType", artifact_type),
            property_value("methunmix:stagingStatus", "STAGED_NOT_PUBLIC"),
            property_value("methunmix:bytes", path.stat().st_size),
        ],
    }


def _vendor_component(
    item: dict[str, Any], wheel_members: dict[str, bytes], core_root: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    name = str(item.get("asset_id", "vendor:unknown")).removeprefix("tool-code:")
    file_components: list[dict[str, Any]] = []
    member_refs: list[str] = []
    packaged_prefixes: list[str] = []
    for source_path in item.get("bundled_vendor_paths", []):
        try:
            relative = Path(source_path).relative_to(core_root).as_posix()
        except (TypeError, ValueError):
            continue
        if relative.startswith("src/"):
            relative = relative[len("src/"):]
        packaged_prefixes.append(relative.rstrip("/") + "/")
    matching = sorted(
        (path, content)
        for path, content in wheel_members.items()
        if any(path.startswith(prefix) for prefix in packaged_prefixes)
    )
    for path, content in matching:
        component_digest = sha256_bytes(path.encode("utf-8") + bytes([0]) + content)
        ref = f"file:{component_digest}"
        member_refs.append(ref)
        file_components.append({
            "type": "file",
            "bom-ref": ref,
            "name": path,
            "hashes": [{"alg": "SHA-256", "content": sha256_bytes(content)}],
            "properties": [property_value("methunmix:packagedPath", path)],
        })

    license_families = item.get("detected_license_families") or []
    license_evidence = item.get("license_evidence") or []
    licenses: list[dict[str, Any]] = []
    family = license_families[0] if len(license_families) == 1 else None
    if family == "MIT":
        licenses = [{"license": {"id": "MIT"}}]
    elif family == "AGPL-3.0":
        licenses = [{"license": {"id": "AGPL-3.0-only"}}]
    elif family == "CUSTOM_SOFTWARE_RESEARCH_LICENSE":
        licenses = [{"license": {"name": "Yissum Software Research License"}}]

    component_ref = f"vendor:{name.lower()}"
    component = {
        "type": "library",
        "bom-ref": component_ref,
        "name": name,
        "properties": [
            property_value("methunmix:versionStatus", "UPSTREAM_VERSION_NOT_RESOLVED"),
            property_value("methunmix:licenseStatus", item.get("license_status", "UNKNOWN")),
            property_value("methunmix:distributionStatus", item.get("proposed_distribution_status", "BLOCKED")),
            property_value("methunmix:wheelFileCount", len(matching)),
            *[
                property_value(f"methunmix:licenseEvidence:{index}:sha256", evidence.get("sha256", ""))
                for index, evidence in enumerate(license_evidence, start=1)
                if isinstance(evidence, dict)
            ],
        ],
    }
    if licenses:
        component["licenses"] = licenses
    return component, file_components, member_refs


def build_sbom(
    source_archive: Path,
    wheel: Path,
    inventory_path: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if not isinstance(inventory, dict) or not isinstance(inventory.get("items"), list):
        raise ValueError("license inventory must contain an items array")
    core_root = Path(inventory.get("inputs", {}).get("core_root", ROOT))
    with zipfile.ZipFile(wheel, "r") as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise ValueError(f"wheel CRC check failed for {bad_member}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("wheel contains duplicate member paths")
        wheel_members = {name: archive.read(name) for name in names if not name.endswith("/")}

    source_component = _artifact_component(source_archive, "source-distribution")
    wheel_component = _artifact_component(wheel, "python-wheel")
    component_rows = [
        item for item in inventory["items"]
        if isinstance(item, dict)
        and item.get("asset_type") == "tool_source_or_adapter"
        and item.get("bundled_vendor_paths")
    ]
    vendor_components: list[dict[str, Any]] = []
    vendor_files: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    root_dependencies = [source_component["bom-ref"], wheel_component["bom-ref"]]
    for item in component_rows:
        parent, files, member_refs = _vendor_component(item, wheel_members, core_root)
        vendor_components.append(parent)
        vendor_files.extend(files)
        dependencies.append({"ref": parent["bom-ref"], "dependsOn": member_refs})
        root_dependencies.append(parent["bom-ref"])

    components = [source_component, wheel_component, *vendor_components, *vendor_files]
    stamp = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    identity = f"{source_component['bom-ref']}|{wheel_component['bom-ref']}"
    serial = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
    requirements = [
        "python >=3.10,<3.14",
        "nextflow >=26.04.4,<26.05",
        "openjdk >=17,<18",
    ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial,
        "version": 1,
        "metadata": {
            "timestamp": stamp,
            "component": {
                "type": "application",
                "bom-ref": PRODUCT_REF,
                "name": "MethUnmix",
                "version": "2.0.0rc1",
                "purl": PRODUCT_REF,
                "properties": [
                    property_value("methunmix:firstPartyLicense", "MIT; does not grant bundled third-party rights"),
                    property_value("methunmix:licenseAssessment", "PENDING_MIXED_VENDOR_LICENSE_REVIEW"),
                    property_value("methunmix:sourceArchiveSha256", source_component["hashes"][0]["content"]),
                    property_value("methunmix:wheelSha256", wheel_component["hashes"][0]["content"]),
                ],
            },
            "properties": [
                property_value("methunmix:sbomScope", "exact staged source archive, wheel, and detected bundled vendor files"),
                property_value("methunmix:sbomCompleteness", "PARTIAL_RC_CORE_ONLY"),
                property_value("methunmix:sourceArtifactStatus", "STAGED_NOT_PUBLIC"),
                *[property_value("methunmix:declaredCondaRuntimeRequirement", value) for value in requirements],
                property_value("methunmix:externalAssetInventory", "LICENSE_GAP_INVENTORY.json; external SIF/reference/model/cache SBOMs remain separate"),
            ],
        },
        "components": components,
        "dependencies": [{"ref": PRODUCT_REF, "dependsOn": root_dependencies}, *dependencies],
        "properties": [
            property_value("methunmix:licenseInventoryStatus", inventory.get("status", "UNKNOWN")),
            property_value("methunmix:licenseInventoryGeneratedAt", inventory.get("generated_at", "UNKNOWN")),
            property_value("methunmix:externalSifSbom", "NOT_INCLUDED; per-image SBOM and redistribution review required"),
            property_value("methunmix:resolvedCondaEnvironment", "NOT_AVAILABLE; local Bioconda build/solve pending"),
            property_value("methunmix:publicRelease", "NOT_AUTHORIZED"),
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--wheel", type=Path, default=DEFAULT_WHEEL)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    bom = build_sbom(args.source, args.wheel, args.inventory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bom, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "PARTIAL_RC_CORE_ONLY",
        "output": str(args.output),
        "components": len(bom["components"]),
        "source_sha256": next(item["value"] for item in bom["metadata"]["component"]["properties"] if item["name"] == "methunmix:sourceArchiveSha256"),
        "wheel_sha256": next(item["value"] for item in bom["metadata"]["component"]["properties"] if item["name"] == "methunmix:wheelSha256"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
