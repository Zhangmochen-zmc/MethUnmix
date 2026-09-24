#!/usr/bin/env python3
"""Small dependency-free preflight for the MethUnmix Bioconda recipe.

This is not a replacement for bioconda-utils/conda-build; it catches local
identity, checksum, noarch and dependency mistakes before those tools run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = ROOT / "bioconda/recipes/methunmix/meta.yaml"
DEFAULT_SOURCE = ROOT / "dist_source_static_catalog_rc1_helper_fix/methunmix-2.0.0rc1.tar.gz"
CATALOG = ROOT / "src/methunmix_assets/assets/catalog.json"
PYPROJECT = ROOT / "pyproject.toml"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    recipe_path = args.recipe.expanduser().resolve()
    source_path = args.source.expanduser().resolve()
    reasons: list[str] = []
    text = recipe_path.read_text(encoding="utf-8") if recipe_path.is_file() else ""
    checks = {
        "name": bool(re.search(r"\{\s*%\s*set\s+name\s*=\s*\"methunmix\"", text)) and "name: {{ name|lower }}" in text,
        "version": bool(re.search(r"\{\s*%\s*set\s+version\s*=\s*\"2\.0\.0rc1\"", text)),
        "noarch_python": "noarch: python" in text,
        "stable_release_url": "github.com/zhangmch/MethUnmix/releases/download/v{{ version }}" in text,
        "legacy_entrypoint": "demethflow = demethflow_core.legacy:main" in text,
        "new_entrypoint": "methunmix = demethflow_core.cli:main" in text,
        "python_bound": "python >=3.10,<3.14" in text,
        "nextflow_bound": "nextflow >=26.04.4,<26.05" in text,
        "java_bound": "openjdk >=17,<18" in text,
        "license": "license: MIT" in text and "license_file: LICENSE" in text,
        "recipe_omits_tuf_only_dependency": "cryptography" not in text,
        "core_dependencies_do_not_include_crypto_only_requirement": "cryptography" not in PYPROJECT.read_text(encoding="utf-8"),
    }
    try:
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        catalog = {}
    checks["static_catalog_schema"] = (
        catalog.get("schema") == "methunmix-static-catalog-v1"
        and catalog.get("trust_model") == "HTTPS_STATIC_CATALOG_SHA256"
    )
    targets = catalog.get("targets", [])
    empty_policy = catalog.get("empty_catalog_policy", {})
    checks["empty_catalog_has_explicit_user_supplied_fallback"] = bool(targets) or (
        isinstance(empty_policy, dict)
        and empty_policy.get("allowed") is True
        and empty_policy.get("fallback") == "USER_SUPPLIED_SUPPORTED_OR_BLOCKED"
        and bool(str(empty_policy.get("reason", "")).strip())
    )
    checks["no_unapproved_public_assets"] = all(
        item.get("distribution_status") != "PUBLIC_DOWNLOADABLE"
        or (item.get("license_status") == "LICENSE_APPROVED" and bool(item.get("license_evidence")))
        for item in catalog.get("targets", []) if isinstance(item, dict)
    )
    reasons.extend(f"recipe check failed: {name}" for name, ok in checks.items() if not ok)
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path.is_file() else None
    match = re.search(r"^\s*sha256:\s*([0-9a-fA-F]{64})\s*$", text, re.MULTILINE)
    if not digest:
        reasons.append(f"missing staged source archive: {source_path}")
    elif not match or match.group(1).lower() != digest:
        reasons.append("recipe source checksum does not match staged source archive")
    payload = {
        "schema": "methunmix-conda-recipe-audit-v1",
        "status": "PASS" if not reasons else "FAIL",
        "recipe": str(recipe_path),
        "source_sha256": digest,
        "checks": checks,
        "reasons": reasons,
        "bioconda_utils_required": True,
        "read_only": True,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if not reasons else 2


if __name__ == "__main__":
    raise SystemExit(main())
