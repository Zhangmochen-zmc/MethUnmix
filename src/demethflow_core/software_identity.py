"""Portable identity for the code and bundled resources that execute a run.

Git commits are valuable provenance for a source checkout, but an installed
wheel has no checkout from which to recover one.  The retry contract therefore
uses an execution-content digest as its compatibility authority and retains a
commit only as independently recorded provenance.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

from . import PROJECT_ROOT, version
from .util import sha256_file, stable_digest


EXECUTION_CONTENT_SCHEMA = "methunmix-execution-content-v1"
_IGNORED_PARTS = {"__pycache__"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _included_asset_path(relative: Path) -> bool:
    """Mirror the explicit ``methunmix_assets`` wheel payload boundary."""
    parts = relative.parts
    if relative.name in {"VERSION", "ASSET_BOUNDARY.txt", "__init__.py"}:
        return len(parts) == 1
    if parts and parts[0] == "capabilities":
        return relative.suffix == ".json"
    if parts and parts[0] == "assets":
        return relative.suffix == ".json"
    if len(parts) >= 2 and parts[0] == "deconvolution":
        if len(parts) == 2:
            return relative.suffix in {".nf", ".config"}
        if parts[1] == "array_pro":
            return relative.suffix == ".py"
        if parts[1] in {"bin", "wgbs_scripts"}:
            return relative.suffix in {".py", ".R", ".sh"}
        return False
    if len(parts) >= 2 and parts[0] == "nextflow_ref":
        return relative.name == "README.md" or relative.suffix in {".nf", ".config", ".py", ".sh", ".txt", ".yml", ".yaml", ".json"}
    return False


def _content_entries(root: Path, namespace: str, *, assets: bool = False) -> Iterable[tuple[str, str]]:
    """Yield path-independent hashes for files that can affect execution."""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts) or path.suffix in _IGNORED_SUFFIXES:
            continue
        if assets and not _included_asset_path(relative):
            continue
        yield (f"{namespace}/{relative.as_posix()}", sha256_file(path))


def execution_content_identity(
    *, package_root: Path | None = None, asset_root: Path | None = None
) -> dict[str, object]:
    """Return a location-independent digest of executable core/resources.

    It deliberately does not claim to be the original wheel archive digest:
    an installed wheel does not retain that archive.  It instead identifies
    exactly the shipped Python package and resource payload used at runtime.
    """
    core = package_root or Path(__file__).resolve().parent
    assets = asset_root or PROJECT_ROOT
    entries = [*_content_entries(core, "demethflow_core"), *_content_entries(assets, "methunmix_assets", assets=True)]
    return {
        "schema": EXECUTION_CONTENT_SCHEMA,
        "sha256": stable_digest(entries),
        "file_count": len(entries),
        "scope": "installed-core-and-bundled-resources",
    }


def _source_commit() -> tuple[str | None, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=2,
        )
        status = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"],
            check=False, capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "UNKNOWN_NOT_EMBEDDED_IN_INSTALLED_ARTIFACT"
    commit = completed.stdout.strip() if completed.returncode == 0 else None
    if not commit:
        return None, "UNKNOWN_NOT_EMBEDDED_IN_INSTALLED_ARTIFACT"
    return commit, "VERIFIED_SOURCE_CHECKOUT_DIRTY" if status.returncode == 0 and status.stdout.strip() else "VERIFIED_SOURCE_CHECKOUT"


def current_software_identity() -> dict[str, object]:
    """Record independent provenance and the retry-compatible content identity."""
    commit, commit_status = _source_commit()
    return {
        "core_version": version(),
        "source_commit": commit,
        "source_commit_status": commit_status,
        "package_artifact_identity": "UNKNOWN_NOT_RECORDED_BY_CURRENT_RUNNER",
        "execution_content_identity": execution_content_identity(),
    }
