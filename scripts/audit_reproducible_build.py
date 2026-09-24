#!/usr/bin/env python3
"""Check deterministic source archives and normalized wheels in one builder."""

from __future__ import annotations

import hashlib
import json
import argparse
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from build_source_snapshot import ROOT
from normalize_wheel import normalize_wheel

DEFAULT_STAGED_SOURCE = ROOT / "dist_source_static_catalog_rc1_helper_fix" / f"methunmix-{(ROOT / 'VERSION').read_text().strip()}.tar.gz"
DEFAULT_STAGED_WHEEL = ROOT / "dist_conda_static_catalog_rc1_clean_helper_fix" / f"methunmix-{(ROOT / 'VERSION').read_text().strip()}-py3-none-any.whl"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(command: list[str], *, timeout: int = 90) -> dict[str, object]:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-1500:],
        "stderr_tail": result.stderr[-1500:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged-source", type=Path, default=DEFAULT_STAGED_SOURCE)
    parser.add_argument("--staged-wheel", type=Path, default=DEFAULT_STAGED_WHEEL)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/reproducible_build_audit_rc.json")
    args = parser.parse_args()
    staged_source = args.staged_source.expanduser().resolve()
    staged_wheel = args.staged_wheel.expanduser().resolve()
    output = args.output.expanduser().resolve()
    results: dict[str, object] = {}
    failure: str | None = None
    with tempfile.TemporaryDirectory(prefix="methunmix-reproducible-build-") as tmp:
        scratch = Path(tmp)
        source_hashes: list[str] = []
        wheel_raw_hashes: list[str] = []
        wheel_normalized_hashes: list[str] = []
        wheel_member_counts: list[int] = []
        try:
            for index in (1, 2):
                source_dir = scratch / f"source-{index}"
                result = run([sys.executable, "scripts/build_source_snapshot.py", "--output", str(source_dir)])
                results[f"source_build_{index}"] = result
                if result["returncode"] != 0:
                    failure = f"source build {index} failed"
                    break
                source_archive = source_dir / f"methunmix-{(ROOT / 'VERSION').read_text().strip()}.tar.gz"
                source_hashes.append(sha256(source_archive))

            if failure is None:
                for index in (1, 2):
                    wheel_dir = scratch / f"wheel-{index}"
                    wheel_dir.mkdir()
                    result = run([sys.executable, "-m", "build", "--quiet", "--wheel", "--no-isolation", "--outdir", str(wheel_dir)])
                    results[f"wheel_build_{index}"] = result
                    if result["returncode"] != 0:
                        failure = f"wheel build {index} failed"
                        break
                    wheel = wheel_dir / f"methunmix-{(ROOT / 'VERSION').read_text().strip()}-py3-none-any.whl"
                    wheel_raw_hashes.append(sha256(wheel))
                    with zipfile.ZipFile(wheel) as archive:
                        wheel_member_counts.append(len(archive.namelist()))
                    normalized = wheel_dir / f"methunmix-{(ROOT / 'VERSION').read_text().strip()}-normalized.whl"
                    wheel_normalized_hashes.append(normalize_wheel(wheel, normalized))
        except (OSError, subprocess.TimeoutExpired, ValueError, zipfile.BadZipFile) as exc:
            failure = f"{type(exc).__name__}: {exc}"

    source_reproducible = len(source_hashes) == 2 and source_hashes[0] == source_hashes[1]
    raw_wheel_reproducible = len(wheel_raw_hashes) == 2 and wheel_raw_hashes[0] == wheel_raw_hashes[1]
    normalized_wheel_reproducible = len(wheel_normalized_hashes) == 2 and wheel_normalized_hashes[0] == wheel_normalized_hashes[1]
    staged_source_digest = sha256(staged_source) if staged_source.is_file() else None
    staged_wheel_digest = sha256(staged_wheel) if staged_wheel.is_file() else None
    source_stage_matches = bool(source_hashes) and staged_source_digest == source_hashes[0]
    wheel_stage_matches = bool(wheel_normalized_hashes) and staged_wheel_digest == wheel_normalized_hashes[0]
    passed = (
        failure is None and source_reproducible and normalized_wheel_reproducible
        and source_stage_matches and wheel_stage_matches
    )
    report = {
        "schema": "methunmix-reproducible-build-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "PASS" if passed else "FAIL",
        "scope": "two source archives and two normalized wheels, same local Python/build environment; dependency isolation disabled, not an OS network sandbox",
        "builder": {
            "python": sys.version.split()[0],
            "zlib": __import__("zlib").ZLIB_VERSION,
            "build_frontend": subprocess.run([sys.executable, "-m", "build", "--version"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip(),
        },
        "source_archives": {
            "sha256": source_hashes,
            "reproducible": source_reproducible,
            "staged_sha256": staged_source_digest,
            "staged_path": str(staged_source),
            "staged_matches_reproducible_build": source_stage_matches,
        },
        "raw_wheels": {"sha256": wheel_raw_hashes, "reproducible": raw_wheel_reproducible, "note": "raw wheel ZIP timestamps vary with build time"},
        "normalized_wheels": {
            "sha256": wheel_normalized_hashes,
            "member_counts": wheel_member_counts,
            "reproducible": normalized_wheel_reproducible,
            "staged_sha256": staged_wheel_digest,
            "staged_path": str(staged_wheel),
            "staged_matches_reproducible_build": wheel_stage_matches,
        },
        "network_access_policy": "no dependency isolation or install was invoked; subprocesses were not run inside an OS network sandbox",
        "failure": failure,
        "build_logs": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "source_archives": report["source_archives"],
        "raw_wheels": report["raw_wheels"],
        "normalized_wheels": report["normalized_wheels"],
        "failure": failure,
        "output": str(output),
    }, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
