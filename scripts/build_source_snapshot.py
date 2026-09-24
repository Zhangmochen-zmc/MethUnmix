#!/usr/bin/env python3
"""Create a deterministic source artifact from the MethUnmix allowlist."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INCLUDE_FILES = {
    "pyproject.toml",
    "MANIFEST.in",
    "VERSION",
    "LICENSE",
    "NOTICE",
    "CITATION.cff",
    "README.md",
    "VERSIONING_POLICY.md",
    "COMPATIBILITY_RULES.json",
}
# The Bioconda recipe is maintained by the packaging channel and deliberately
# is not embedded in the upstream source tarball.  Including it would create a
# self-referential SHA256 because the recipe records this archive's digest.
INCLUDE_DIRS = {"src", "docs", "scripts", "tests"}
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", "dist_source", "dist_conda_rc", ".git", "build"}
# Repository-local host launchers and phase-specific evidence generators are
# retained in Git for release operations, but are not part of the portable
# user source distribution.  They contain machine-specific paths and are not
# imported by the installed package or required by the CLI/runtime.
EXCLUDE_SOURCE_PATHS = {
    "scripts/audit_e2b_g01_launcher_final_phase3h.py",
    "scripts/capture_native_host_context.sh",
    "scripts/generate_e2a_g03_phase3g_evidence.py",
    "scripts/preflight_e2a_g03_host_context.py",
    "scripts/prepare_e2b_hg19_scope_phase3h.py",
    "scripts/reconcile_e2b_phase_boundary_phase3h.py",
    "scripts/run_e2a_g03_host.sh",
    "scripts/run_e2b_hg19_standard_cpu_host.sh",
}

# Phase-specific CelFEER host launchers, forensic helpers and evidence
# generators are repository tooling, not portable MethUnmix source.  They
# contain machine-specific paths and are intentionally excluded from the
# public sdist just like the earlier host-context launchers above.
EXCLUDE_SOURCE_PREFIXES = (
    "src/methunmix_assets/deconvolution/wgbs_vendor",
    "src/methunmix_assets/deconvolution/vendor/prmeth_R",
    "scripts/e3a_celfeer_",
    "scripts/analyze_e3a_celfeer_",
    "scripts/audit_e3a_celfeer_",
    "scripts/generate_e3a_celfeer_",
    "scripts/implement_e3a_celfeer_",
    "scripts/prepare_e3a_celfeer_",
    "scripts/run_e3a_celfeer_",
    "scripts/celfeer_targeted_replay",
    "scripts/generate_phase3n_evidence.py",
    "scripts/generate_celfeer_release_contract_phase3p1_evidence.py",
    "scripts/generate_phase3p3_evidence.py",
    "scripts/generate_phase3p4_evidence.py",
)


def tracked_paths() -> set[str] | None:
    if not (ROOT / ".git").exists():
        return None
    status = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if status.strip():
        raise RuntimeError("source snapshot requires a clean tracked Git checkout; commit tracked changes first")
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout
    return {entry.decode("utf-8") for entry in output.split(b"\0") if entry}


def files() -> list[Path]:
    tracked = tracked_paths()
    selected: list[Path] = []
    for name in sorted(INCLUDE_FILES):
        path = ROOT / name
        if not path.is_file():
            raise FileNotFoundError(f"required source allowlist file is missing: {path}")
        if tracked is not None and name not in tracked:
            raise ValueError(f"required source file is not tracked in Git: {name}")
        selected.append(path)
    for directory in sorted(INCLUDE_DIRS):
        base = ROOT / directory
        if not base.is_dir():
            raise FileNotFoundError(f"required source allowlist directory is missing: {base}")
        if base.is_symlink():
            raise ValueError(f"refusing symlinked source allowlist directory: {base}")
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"refusing symlinked source allowlist member: {path}")
            parts = path.relative_to(ROOT).parts
            generated_metadata = any(part.endswith(".egg-info") for part in parts)
            if path.is_file() and not generated_metadata and not any(part in EXCLUDE_PARTS for part in parts):
                relative_name = path.relative_to(ROOT).as_posix()
                if relative_name in EXCLUDE_SOURCE_PATHS or relative_name.startswith(EXCLUDE_SOURCE_PREFIXES):
                    continue
                if tracked is None or relative_name in tracked:
                    selected.append(path)
    unique: set[Path] = set()
    for path in selected:
        if path.is_symlink():
            raise ValueError(f"refusing symlinked source allowlist member: {path}")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(ROOT)
        except ValueError as exc:
            raise ValueError(f"source allowlist member resolves outside the project: {path}") from exc
        if resolved in unique:
            raise ValueError(f"duplicate source allowlist member: {path}")
        unique.add(resolved)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist_source")
    args = parser.parse_args()
    output_dir = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    artifact = output_dir / f"methunmix-{version}.tar.gz"
    prefix = f"methunmix-{version}"
    with artifact.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in files():
                    relative = path.relative_to(ROOT).as_posix()
                    info = archive.gettarinfo(str(path), arcname=f"{prefix}/{relative}")
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    if info.isfile():
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (artifact.with_name(artifact.name + ".sha256")).write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
    print(f"artifact={artifact}")
    print(f"sha256={digest}")
    print(f"files={len(files())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
