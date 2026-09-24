#!/usr/bin/env python3
"""Audit wheel/source payload boundaries and deterministic release hashes."""
from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
import argparse
import re
from pathlib import Path, PurePosixPath

try:
    from .build_source_snapshot import EXCLUDE_PARTS, INCLUDE_DIRS, INCLUDE_FILES, files as source_allowlist_files
except ImportError:  # executing this file directly from scripts/
    from build_source_snapshot import EXCLUDE_PARTS, INCLUDE_DIRS, INCLUDE_FILES, files as source_allowlist_files

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WHEEL = ROOT / "dist_conda_static_catalog_rc1_clean_helper_fix" / "methunmix-2.0.0rc1-py3-none-any.whl"
DEFAULT_SOURCE = ROOT / "dist_source_static_catalog_rc1_helper_fix" / "methunmix-2.0.0rc1.tar.gz"
DEFAULT_RECIPE = ROOT / "bioconda" / "recipes" / "methunmix" / "meta.yaml"
FORBIDDEN = (".sif", ".bam", ".cram", ".pat", ".bed", ".bai", ".csi", ".pyc", ".pyo", ".jar", ".class", ".so", ".dll", ".dylib")
PRIVATE_PATH = re.compile(rb"/(?:data|home|disk1)/(?:zhangmch|yuxy|weiyk)(?:/|$)", re.IGNORECASE)
PACKAGE_VERSION = "2.0.0rc1"
# RC1 size envelope is based on the measured allowlisted payload plus a fixed
# allowance. Updating it requires reviewing the source/wheel allowlists first.
SIZE_ALLOWANCE_BYTES = 256 * 1024
COMPRESSED_ALLOWANCE_BYTES = 64 * 1024
# Measured after the resource-contract source commit, its regression tests,
# and the portable release-audit tooling were added to the reviewed allowlist.
# Keep the fixed allowances unchanged; future growth still requires review.
SOURCE_UNCOMPRESSED_BASELINE = 1_796_440
SOURCE_ARCHIVE_BASELINE = 439_945
WHEEL_UNCOMPRESSED_BASELINE = 1_259_298
WHEEL_ARCHIVE_BASELINE = 398_963
DIST_INFO_MEMBERS = {
    f"methunmix-{PACKAGE_VERSION}.dist-info/METADATA",
    f"methunmix-{PACKAGE_VERSION}.dist-info/WHEEL",
    f"methunmix-{PACKAGE_VERSION}.dist-info/entry_points.txt",
    f"methunmix-{PACKAGE_VERSION}.dist-info/top_level.txt",
    f"methunmix-{PACKAGE_VERSION}.dist-info/RECORD",
    f"methunmix-{PACKAGE_VERSION}.dist-info/licenses/LICENSE",
    f"methunmix-{PACKAGE_VERSION}.dist-info/licenses/NOTICE",
}
WHEEL_ASSET_SUFFIXES = {".nf", ".config", ".py", ".R", ".sh", ".txt", ".yml", ".yaml", ".json"}
WHEEL_NEXTFLOW_SUFFIXES = WHEEL_ASSET_SUFFIXES | {".md"}
SOURCE_ASSET_SUFFIXES = WHEEL_ASSET_SUFFIXES | {".md", ".gitkeep"}
SOURCE_SPECIAL_FILES = {
    "src/methunmix_assets/deconvolution/wgbs_scripts/uxm",
    "src/methunmix_assets/deconvolution/wgbs_vendor/MetDecode/LICENSE",
    "src/methunmix_assets/nextflow_ref/condarc",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def check_names(names: list[str]) -> list[str]:
    return sorted({
        name for name in names
        if "__pycache__" in name.split("/")
        or any(part.endswith(".egg-info") for part in name.split("/"))
        or name.lower().endswith(FORBIDDEN)
    })


def scan_private_paths(members: list[tuple[str, bytes]]) -> list[str]:
    return sorted({name for name, data in members if PRIVATE_PATH.search(data)})


def safe_archive_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and "\\" not in name
        and path.as_posix() == name
        and bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def expected_source_members() -> set[str]:
    prefix = f"methunmix-{PACKAGE_VERSION}/"
    return {prefix + path.relative_to(ROOT).as_posix() for path in source_allowlist_files()}


def allowed_source_member(name: str) -> bool:
    if not safe_archive_path(name):
        return False
    parts = PurePosixPath(name).parts
    prefix = f"methunmix-{PACKAGE_VERSION}"
    if not parts or parts[0] != prefix:
        return False
    relative = parts[1:]
    if not relative:
        return False
    if any(part in EXCLUDE_PARTS or part.endswith(".egg-info") for part in relative):
        return False
    relative_text = PurePosixPath(*relative).as_posix()
    if "deconvolution/wgbs_vendor/" in relative_text or "deconvolution/vendor/prmeth_R/" in relative_text:
        return False
    if len(relative) == 1:
        return relative[0] in INCLUDE_FILES
    if relative[0] not in INCLUDE_DIRS:
        return False
    suffix = PurePosixPath(name).suffix
    if relative[0] == "docs":
        return suffix == ".md"
    if relative[0] in {"scripts", "tests"}:
        return suffix == ".py"
    if relative[0] == "src":
        relative_source_path = PurePosixPath(*relative).as_posix()
        return (
            suffix in SOURCE_ASSET_SUFFIXES
            or relative[-1] in {"VERSION", ".gitkeep"}
            or relative_source_path in SOURCE_SPECIAL_FILES
        )
    return False


def allowed_wheel_member(name: str) -> bool:
    if not safe_archive_path(name) or name.endswith("/"):
        return False
    parts = PurePosixPath(name).parts
    if len(parts) == 2 and parts[0] == "demethflow_core":
        return parts[1].endswith(".py")
    if parts[0] == "methunmix_assets":
        relative = parts[1:]
        if relative in (("__init__.py",), ("VERSION",), ("ASSET_BOUNDARY.txt",)):
            return True
        if len(relative) == 2 and relative[0] == "capabilities":
            return relative[1].endswith(".json")
        if len(relative) >= 2 and relative[0] == "assets":
            return relative[-1].endswith(".json")
        if len(relative) >= 2 and relative[0] == "deconvolution":
            relative_text = PurePosixPath(*relative).as_posix()
            if "deconvolution/wgbs_vendor/" in relative_text or "deconvolution/vendor/prmeth_R/" in relative_text:
                return False
            return Path(relative[-1]).suffix in WHEEL_ASSET_SUFFIXES or relative[-1] == "uxm"
        if len(relative) >= 2 and relative[0] == "nextflow_ref":
            return Path(relative[-1]).suffix in WHEEL_NEXTFLOW_SUFFIXES
        return False
    return name in DIST_INFO_MEMBERS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, default=DEFAULT_WHEEL)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--output", type=Path, help="also write the JSON audit evidence")
    args = parser.parse_args()
    wheel = args.wheel.expanduser().resolve()
    source = args.source.expanduser().resolve()
    recipe_path = args.recipe.expanduser().resolve()
    reasons: list[str] = []
    evidence: dict[str, object] = {}
    expected_source = expected_source_members()
    if not wheel.is_file():
        reasons.append(f"missing wheel: {wheel}")
    else:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            unsafe_paths = sorted(name for name in names if not safe_archive_path(name))
            unexpected = sorted(name for name in names if not allowed_wheel_member(name))
            duplicates = sorted({name for name in names if names.count(name) > 1})
            forbidden = check_names(names)
            uncompressed_bytes = sum(info.file_size for info in archive.infolist())
            if forbidden:
                reasons.append(f"forbidden payload in wheel: {forbidden}")
            if unsafe_paths:
                reasons.append(f"unsafe paths in wheel: {unsafe_paths}")
            if unexpected:
                reasons.append(f"wheel members outside reviewed allowlist: {unexpected[:20]}")
            if duplicates:
                reasons.append(f"duplicate wheel paths: {duplicates}")
            if uncompressed_bytes > WHEEL_UNCOMPRESSED_BASELINE + SIZE_ALLOWANCE_BYTES:
                reasons.append(f"wheel uncompressed size exceeds allowlist baseline plus fixed allowance: {uncompressed_bytes}")
            if wheel.stat().st_size > WHEEL_ARCHIVE_BASELINE + COMPRESSED_ALLOWANCE_BYTES:
                reasons.append(f"wheel archive size exceeds measured baseline plus fixed allowance: {wheel.stat().st_size}")
            path_hits = scan_private_paths([
                (name, archive.read(name)) for name in names if not name.endswith("/")
            ])
            if path_hits:
                reasons.append(f"machine-specific absolute paths in wheel: {path_hits}")
            evidence["wheel"] = {
                "path": str(wheel), "sha256": sha256(wheel), "members": len(names),
                "allowlist_status": "PASS" if not unexpected and not unsafe_paths and not duplicates else "FAIL",
                "uncompressed_bytes": uncompressed_bytes,
                "uncompressed_limit_bytes": WHEEL_UNCOMPRESSED_BASELINE + SIZE_ALLOWANCE_BYTES,
                "archive_bytes": wheel.stat().st_size,
                "archive_limit_bytes": WHEEL_ARCHIVE_BASELINE + COMPRESSED_ALLOWANCE_BYTES,
            }
    if not source.is_file():
        reasons.append(f"missing source archive: {source}")
    else:
        with tarfile.open(source, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            normalized_names = {name.removeprefix(f"methunmix-{PACKAGE_VERSION}/") for name in names}
            unsafe_paths = sorted(name for name in names if not safe_archive_path(name))
            outside_policy = sorted(name for name in names if not allowed_source_member(name))
            unexpected = sorted(normalized_names - {name.removeprefix(f"methunmix-{PACKAGE_VERSION}/") for name in expected_source})
            missing = sorted({name.removeprefix(f"methunmix-{PACKAGE_VERSION}/") for name in expected_source} - normalized_names)
            non_regular = sorted(member.name for member in members if not member.isfile())
            duplicates = sorted({name for name in names if names.count(name) > 1})
            forbidden = check_names(names)
            uncompressed_bytes = sum(member.size for member in members if member.isfile())
            if forbidden:
                reasons.append(f"forbidden payload in source archive: {forbidden}")
            if unsafe_paths:
                reasons.append(f"unsafe paths in source archive: {unsafe_paths}")
            if outside_policy:
                reasons.append(f"source archive members outside reviewed path/extension allowlist: {outside_policy[:20]}")
            if unexpected or missing:
                reasons.append(f"source archive differs from reviewed allowlist; unexpected={unexpected[:20]}, missing={missing[:20]}")
            if non_regular:
                reasons.append(f"non-regular members in source archive: {non_regular[:20]}")
            if duplicates:
                reasons.append(f"duplicate source archive paths: {duplicates}")
            if uncompressed_bytes > SOURCE_UNCOMPRESSED_BASELINE + SIZE_ALLOWANCE_BYTES:
                reasons.append(f"source uncompressed size exceeds allowlist baseline plus fixed allowance: {uncompressed_bytes}")
            if source.stat().st_size > SOURCE_ARCHIVE_BASELINE + COMPRESSED_ALLOWANCE_BYTES:
                reasons.append(f"source archive size exceeds measured baseline plus fixed allowance: {source.stat().st_size}")
            path_hits = scan_private_paths([
                (member.name, handle.read())
                for member in archive.getmembers()
                if member.isfile() and (handle := archive.extractfile(member)) is not None
            ])
            if path_hits:
                reasons.append(f"machine-specific absolute paths in source archive: {path_hits}")
            evidence["source"] = {
                "path": str(source), "sha256": sha256(source), "members": len(names),
                "allowlist_status": "PASS" if not unexpected and not missing and not unsafe_paths and not outside_policy and not non_regular and not duplicates and not forbidden else "FAIL",
                "uncompressed_bytes": uncompressed_bytes,
                "uncompressed_limit_bytes": SOURCE_UNCOMPRESSED_BASELINE + SIZE_ALLOWANCE_BYTES,
                "archive_bytes": source.stat().st_size,
                "archive_limit_bytes": SOURCE_ARCHIVE_BASELINE + COMPRESSED_ALLOWANCE_BYTES,
            }
    if recipe_path.is_file():
        recipe = recipe_path.read_text(encoding="utf-8")
        source_digest = evidence.get("source", {}).get("sha256") if isinstance(evidence.get("source"), dict) else None
        if source_digest and source_digest not in recipe:
            reasons.append("recipe SHA256 does not match the staged source archive")
        if "REPLACE_WITH_FINAL_SOURCE_SHA256" in recipe:
            reasons.append("recipe contains checksum placeholder")
    payload = {"schema": "methunmix-conda-payload-audit-v1", "status": "PASS" if not reasons else "FAIL", "reasons": reasons, "evidence": evidence, "read_only": True}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if not reasons else 2


if __name__ == "__main__":
    raise SystemExit(main())
