#!/usr/bin/env python3
"""Audit a proposed public-core file set without publishing anything.

``preparation`` mode reports unresolved owner decisions but succeeds when the
review material is structurally complete.  ``release-ready`` mode is deliberately
stricter and exits nonzero for unresolved decisions, placeholders, development
or local versions, or missing public URLs.  It never builds, uploads, invokes
a workflow/container, or reads any external asset directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {".sif", ".bam", ".cram", ".pat", ".bed", ".jar", ".so", ".dll", ".dylib"}
PRIVATE_PATH = re.compile(r"/(?:data|home|disk1)/(?:zhangmch|yuxy|weiyk)(?:/|$)", re.I)
PLACEHOLDER = re.compile(r"<(?:PUBLISHED_[A-Z_]+|[A-Z_]+_URL)>|https?://(?:example\.invalid|TODO|TBD|REPLACE[._-])", re.I)
PUBLIC_RELEASE_MARKER = re.compile(r"^\s*(?:status|release[ _-]?status)\s*[:=]\s*(?:PUBLIC_RELEASE_READY|STABLE)\b", re.I | re.M)
VERSION_LINE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']\s*$', re.M)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(path.parts) and not path.is_absolute() and "\\" not in name and all(part not in {"", ".", ".."} for part in path.parts)


def archive_members(path: Path, kind: str) -> tuple[list[str], list[str]]:
    if kind == "wheel":
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"wheel CRC failure: {bad}")
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            links = [entry.filename for entry in entries if (entry.external_attr >> 16) & 0o170000 == 0o120000]
    else:
        with tarfile.open(path, "r:gz") as archive:
            entries = archive.getmembers()
            names = [entry.name for entry in entries]
            links = [entry.name for entry in entries if entry.issym() or entry.islnk()]
    if links:
        raise ValueError(f"{kind} contains links: {links[:5]}")
    if len(names) != len(set(names)):
        raise ValueError(f"{kind} contains duplicate members")
    if any(not safe_name(name) for name in names):
        raise ValueError(f"{kind} contains unsafe member path")
    return names, [name for name in names if not name.endswith("/")]


def source_versions(root: Path) -> dict[str, str | None]:
    def text(relative: str) -> str | None:
        path = root / relative
        return path.read_text(encoding="utf-8").strip() if path.is_file() else None

    pyproject = text("pyproject.toml") or ""
    match = VERSION_LINE.search(pyproject)
    return {
        "VERSION": text("VERSION"),
        "pyproject": match.group(1) if match else None,
        "packaged_VERSION": text("src/methunmix_assets/VERSION"),
    }


def file_text(root: Path, relative: str) -> str:
    path = root / relative
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def inspect_artifact(path: Path, kind: str) -> dict[str, Any]:
    names, files = archive_members(path, kind)
    forbidden = sorted(name for name in files if Path(name).suffix.lower() in FORBIDDEN_SUFFIXES)
    locators = sorted(name for name in files if "locator" in name.lower() or "binding" in name.lower())
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "members": len(names),
        "files": len(files),
        "forbidden_binary_or_data_members": forbidden,
        "possible_locator_or_binding_members": locators,
    }


def check(root: Path, wheel: Path, sdist: Path, mode: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    versions = source_versions(root)
    expected = versions["VERSION"]
    if not expected or len(set(versions.values())) != 1:
        errors.append(f"version sources disagree or are missing: {versions}")
    for relative in ("LICENSE", "NOTICE", "README.md", "CITATION.cff", "docs/PUBLIC_USER_GUIDE.md", "docs/PUBLIC_USER_GUIDE_zh-CN.md"):
        if not (root / relative).is_file():
            errors.append(f"required public member missing: {relative}")
    pending_path = root / "docs/publication/PENDING_OWNER_DECISIONS.json"
    pending: dict[str, Any] = {}
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        errors.append("missing or invalid machine-readable owner decision list")
    pending_status = pending.get("status") if isinstance(pending, dict) else None
    accepted_pending_statuses = {
        "DRAFT_PENDING_OWNER_APPROVAL",
        "OWNER_METADATA_RECORDED_REFERENCE_LICENSE_AND_PLATFORM_PENDING",
        "REFERENCE_LICENSE_AND_SOFTWARE_TARGETS_RECORDED_PUBLIC_ACTIONS_NOT_AUTHORIZED",
    }
    if pending_status not in accepted_pending_statuses:
        errors.append(f"unexpected owner-decision status: {pending_status!r}")
    public_text_paths = ["README.md", "docs/PUBLIC_USER_GUIDE.md", "docs/PUBLIC_USER_GUIDE_zh-CN.md"]
    public_text_paths += [str(path.relative_to(root)) for path in sorted((root / "docs/publication").glob("*")) if path.suffix == ".md"]
    placeholders = [relative for relative in public_text_paths if PLACEHOLDER.search(file_text(root, relative))]
    private_paths = [relative for relative in public_text_paths if PRIVATE_PATH.search(file_text(root, relative))]
    false_release_claims = [relative for relative in public_text_paths if PUBLIC_RELEASE_MARKER.search(file_text(root, relative))]
    if private_paths:
        errors.append(f"public documentation contains host-private paths: {private_paths}")
    if false_release_claims:
        errors.append(f"public documentation makes stable/release-ready claim: {false_release_claims}")
    try:
        wheel_info = inspect_artifact(wheel, "wheel")
        sdist_info = inspect_artifact(sdist, "sdist")
        for name, info in (("wheel", wheel_info), ("sdist", sdist_info)):
            if info["forbidden_binary_or_data_members"]:
                errors.append(f"{name} includes forbidden external payload: {info['forbidden_binary_or_data_members'][:5]}")
            if info["possible_locator_or_binding_members"]:
                errors.append(f"{name} includes locator/binding-like payload: {info['possible_locator_or_binding_members'][:5]}")
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        wheel_info = {"path": str(wheel), "error": str(exc)}
        sdist_info = {"path": str(sdist), "error": str(exc)}
        errors.append(f"artifact inspection failed: {exc}")
    public_version = bool(expected) and "+" not in expected and ".dev" not in expected
    if mode == "preparation":
        if placeholders:
            warnings.append(f"publication placeholders intentionally remain: {placeholders}")
        if not public_version:
            warnings.append(f"current version remains internal/development: {expected}")
        if pending_status == "OWNER_METADATA_RECORDED_REFERENCE_LICENSE_AND_PLATFORM_PENDING":
            warnings.append("author/core-license decisions are recorded; reference license instrument and platform actions remain pending")
        elif pending_status == "REFERENCE_LICENSE_AND_SOFTWARE_TARGETS_RECORDED_PUBLIC_ACTIONS_NOT_AUTHORIZED":
            warnings.append(
                "CC BY-NC 4.0 and GitHub/BioCode software targets are recorded; "
                "reference rights/hosting and every public action remain pending"
            )
        else:
            warnings.append("owner decisions remain pending; preparation is not release-ready")
    else:
        if placeholders:
            errors.append(f"release-ready mode rejects unresolved publication placeholders: {placeholders}")
        if not public_version:
            errors.append(f"release-ready mode rejects development/local version: {expected}")
        errors.append("release-ready mode requires separately recorded owner approvals and public artifact URL verification")
    return {
        "schema": "methunmix-prepublication-check-v1",
        "mode": mode,
        "root": str(root.resolve()),
        "version_sources": versions,
        "artifacts": {"wheel": wheel_info, "sdist": sdist_info},
        "pending_owner_decision_status": pending_status,
        "placeholders": placeholders,
        "errors": errors,
        "warnings": warnings,
        "status": (
            "PASS_PREPARATION_PENDING_REFERENCE_RIGHTS_HOST_AND_PUBLIC_ACTIONS"
            if not errors and mode == "preparation" and pending_status == "REFERENCE_LICENSE_AND_SOFTWARE_TARGETS_RECORDED_PUBLIC_ACTIONS_NOT_AUTHORIZED"
            else "PASS_PREPARATION_PENDING_REFERENCE_LICENSE_AND_PLATFORM"
            if not errors and mode == "preparation" and pending_status == "OWNER_METADATA_RECORDED_REFERENCE_LICENSE_AND_PLATFORM_PENDING"
            else "PASS_PREPARATION_PENDING_OWNER_APPROVAL"
            if not errors and mode == "preparation"
            else "RELEASE_READY"
            if not errors
            else "FAIL"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--sdist", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--mode", choices=("preparation", "release-ready"), default="preparation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = check(args.source_root.resolve(), args.wheel.resolve(), args.sdist.resolve(), args.mode)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["status"] in {
        "PASS_PREPARATION_PENDING_OWNER_APPROVAL",
        "PASS_PREPARATION_PENDING_REFERENCE_LICENSE_AND_PLATFORM",
        "PASS_PREPARATION_PENDING_REFERENCE_RIGHTS_HOST_AND_PUBLIC_ACTIONS",
        "RELEASE_READY",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
