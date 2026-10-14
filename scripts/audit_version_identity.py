#!/usr/bin/env python3
"""Fail-closed audit of the version identities shipped by MethUnmix.

This check covers only current core release inputs. Historical evidence,
reference selectors, runtime API versions, and catalog releases use separate
namespaces and must not be globally rewritten when the core version changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:(?:[a-z]+\d+)|(?:\.(?:dev|post)\d+))?(?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?$")
CATALOG_SCHEMA = "methunmix-static-catalog-v1"
CATALOG_TRUST_MODEL = "HTTPS_STATIC_CATALOG_SHA256"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_text_version(path: Path, label: str, issues: list[str]) -> tuple[str | None, dict[str, object]]:
    record: dict[str, object] = {"path": str(path), "exists": path.is_file(), "value": None, "sha256": None}
    if not path.is_file():
        issues.append(f"missing {label}: {path}")
        return None, record
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        issues.append(f"cannot read {label}: {path}: {exc}")
        return None, record
    record.update({"value": value, "sha256": sha256(path)})
    if not VERSION_PATTERN.fullmatch(value):
        issues.append(f"invalid {label} version syntax: {value!r}")
    return value, record


def audit(root: Path = ROOT, expected_version: str | None = None) -> dict[str, object]:
    """Return machine-readable release identity evidence without mutations."""
    root = root.resolve()
    issues: list[str] = []
    sources: dict[str, dict[str, object]] = {}
    root_version, sources["root_version"] = read_text_version(root / "VERSION", "root VERSION", issues)
    asset_version, sources["asset_version"] = read_text_version(root / "src" / "methunmix_assets" / "VERSION", "packaged asset VERSION", issues)

    pyproject = root / "pyproject.toml"
    sources["pyproject"] = {"path": str(pyproject), "exists": pyproject.is_file(), "value": None, "sha256": None}
    pyproject_version: str | None = None
    public_scripts: dict[str, object] = {}
    if not pyproject.is_file():
        issues.append(f"missing pyproject metadata: {pyproject}")
    else:
        try:
            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
            pyproject_version = project.get("version")
            scripts = project.get("scripts", {})
            public_scripts = scripts if isinstance(scripts, dict) else {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            issues.append(f"cannot parse pyproject metadata: {exc}")
        sources["pyproject"].update({
            "value": pyproject_version,
            "sha256": sha256(pyproject),
            "public_scripts": public_scripts,
        })
        if not isinstance(pyproject_version, str) or not VERSION_PATTERN.fullmatch(pyproject_version):
            issues.append(f"invalid pyproject project.version: {pyproject_version!r}")
        if public_scripts.get("methunmix") != "demethflow_core.cli:main":
            issues.append("public methunmix console entry point is missing or unexpected")
        if "demethflow" in public_scripts:
            issues.append("removed public demethflow console entry point is still declared")

    registry = root / "src" / "methunmix_assets" / "capabilities" / "capability_registry.json"
    sources["capability_registry"] = {"path": str(registry), "exists": registry.is_file(), "value": None, "sha256": None}
    registry_version: str | None = None
    if not registry.is_file():
        issues.append(f"missing capability registry: {registry}")
    else:
        try:
            registry_version = json.loads(registry.read_text(encoding="utf-8")).get("software_version")
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"cannot parse capability registry: {exc}")
        sources["capability_registry"].update({"value": registry_version, "sha256": sha256(registry)})
        if not isinstance(registry_version, str) or not VERSION_PATTERN.fullmatch(registry_version):
            issues.append(f"invalid capability registry software_version: {registry_version!r}")

    catalog = root / "src" / "methunmix_assets" / "assets" / "catalog.json"
    sources["embedded_catalog"] = {
        "path": str(catalog), "exists": catalog.is_file(), "value": None, "sha256": None,
        "version_role": "CATALOG_SNAPSHOT_PROVENANCE",
        "core_version_match_required": False,
        "compatibility_scope": "per-target compatibility declarations; empty catalog has no targets",
    }
    catalog_version: str | None = None
    if not catalog.is_file():
        issues.append(f"missing embedded catalog: {catalog}")
    else:
        try:
            catalog_payload = json.loads(catalog.read_text(encoding="utf-8"))
            if not isinstance(catalog_payload, dict):
                raise ValueError("catalog root is not an object")
            catalog_version = catalog_payload.get("core_version")
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"cannot parse embedded catalog: {exc}")
            catalog_payload = None
        except ValueError as exc:
            issues.append(f"invalid embedded catalog: {exc}")
            catalog_payload = None
        sources["embedded_catalog"].update({"value": catalog_version, "sha256": sha256(catalog)})
        if not isinstance(catalog_version, str) or not VERSION_PATTERN.fullmatch(catalog_version):
            issues.append(f"invalid embedded catalog core_version: {catalog_version!r}")
        if isinstance(catalog_payload, dict):
            sources["embedded_catalog"].update({
                "schema": catalog_payload.get("schema"),
                "product": catalog_payload.get("product"),
                "catalog_release": catalog_payload.get("catalog_release"),
                "catalog_sequence": catalog_payload.get("catalog_sequence"),
                "trust_model": catalog_payload.get("trust_model"),
                "targets_count": len(catalog_payload.get("targets", [])) if isinstance(catalog_payload.get("targets"), list) else None,
            })
            if catalog_payload.get("schema") != CATALOG_SCHEMA:
                issues.append(f"unsupported embedded catalog schema: {catalog_payload.get('schema')!r}")
            if catalog_payload.get("product") != "MethUnmix":
                issues.append(f"embedded catalog product is not MethUnmix: {catalog_payload.get('product')!r}")
            if not isinstance(catalog_payload.get("catalog_release"), str) or not catalog_payload["catalog_release"].strip():
                issues.append("embedded catalog has no non-empty catalog_release")
            sequence = catalog_payload.get("catalog_sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
                issues.append(f"embedded catalog has invalid catalog_sequence: {sequence!r}")
            if catalog_payload.get("trust_model") != CATALOG_TRUST_MODEL:
                issues.append(f"embedded catalog has unsupported trust_model: {catalog_payload.get('trust_model')!r}")
            if not isinstance(catalog_payload.get("targets", []), list):
                issues.append("embedded catalog targets is not a list")

    # The bundled static catalog has a separate immutable lifecycle. Its
    # ``core_version`` is provenance for the catalog snapshot, not a claim
    # that every current-core build is compatible with every catalog target.
    # Current core identities therefore exclude it. Asset compatibility stays
    # fail-closed at selection/import time through each target's declared
    # core/runtime/reference API constraints. An unknown catalog schema is
    # rejected above rather than accepted as compatible by omission.
    observed = {value for value in (root_version, asset_version, pyproject_version, registry_version) if isinstance(value, str)}
    if len(observed) != 1:
        issues.append("current core version identities disagree: " + ", ".join(sorted(observed)))
    if expected_version is not None and observed != {expected_version}:
        issues.append(f"current core version is not the required value {expected_version!r}")

    return {
        "schema": "methunmix-version-identity-audit-v1",
        "status": "PASS" if not issues else "FAIL",
        "core_version": next(iter(observed)) if len(observed) == 1 else None,
        "expected_version": expected_version,
        "sources": sources,
        "public_cli_contract": {
            "primary_entry": public_scripts.get("methunmix"),
            "demethflow_entry_present": "demethflow" in public_scripts,
            "internal_import_path_retained": (root / "src" / "demethflow_core").is_dir(),
        },
        "issues": issues,
        "scope": "current core release inputs only; reference/runtime/catalog/evidence identities remain independent",
        "read_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--expect-version")
    parser.add_argument("--output", type=Path, help="write audit JSON after computing it")
    args = parser.parse_args()
    result = audit(args.root, args.expect_version)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
