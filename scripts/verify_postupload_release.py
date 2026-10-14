#!/usr/bin/env python3
"""Prepare or execute a post-upload artifact verification without publishing.

Without ``--execute`` this program only validates a fully bound verification
plan and writes ``PREPARED_NOT_EXECUTED``.  It never guesses a project URL,
contacts a network service, creates a tag, or changes package state.  The
explicit mode is for an owner-approved public artifact and verifies the exact
downloaded bytes before making a clean, no-dependency wheel installation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import venv
from pathlib import Path
from urllib.parse import urlparse


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())


def valid_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def plan(args: argparse.Namespace) -> dict[str, object]:
    problems: list[str] = []
    for label, url in (("sdist", args.sdist_url), ("wheel", args.wheel_url)):
        if not valid_https_url(url):
            problems.append(f"{label}_url must be an absolute credential-free https URL")
    for label, digest in (("sdist", args.sdist_sha256), ("wheel", args.wheel_sha256)):
        if not valid_sha256(digest):
            problems.append(f"{label}_sha256 must be a 64-character hexadecimal digest")
    if not args.version or "+" in args.version or ".dev" in args.version:
        problems.append("version must be a public non-development version")
    if args.sdist_url == args.wheel_url:
        problems.append("sdist_url and wheel_url must be distinct")
    return {
        "schema": "methunmix-postupload-verification-plan-v1",
        "scope": "artifact identity and clean wheel installation only; no scientific or workflow claim",
        "version": args.version,
        "artifacts": {
            "sdist": {"url": args.sdist_url, "sha256": args.sdist_sha256},
            "wheel": {"url": args.wheel_url, "sha256": args.wheel_sha256},
        },
        "problems": problems,
        "status": "INVALID_PLAN" if problems else ("EXECUTION_REQUESTED" if args.execute else "PREPARED_NOT_EXECUTED"),
    }


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "MethUnmix-postupload-verifier/1"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)


def execute(payload: dict[str, object]) -> dict[str, object]:
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    with tempfile.TemporaryDirectory(prefix="methunmix-postupload-") as temporary:
        root = Path(temporary)
        downloaded: dict[str, dict[str, str]] = {}
        for label in ("sdist", "wheel"):
            item = artifacts[label]
            assert isinstance(item, dict)
            target = root / f"{label}.artifact"
            download(str(item["url"]), target)
            observed = sha256(target)
            if observed != item["sha256"]:
                raise ValueError(f"{label} SHA256 mismatch: {observed}")
            downloaded[label] = {"sha256": observed, "bytes": str(target.stat().st_size)}
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        venv_dir = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        pip = bin_dir / ("pip.exe" if os.name == "nt" else "pip")
        cli = bin_dir / ("methunmix.exe" if os.name == "nt" else "methunmix")
        install = subprocess.run([str(pip), "install", "--no-deps", "--no-index", str(root / "wheel.artifact")], text=True, capture_output=True, env=environment, check=False)
        version = subprocess.run([str(cli), "--version"], text=True, capture_output=True, env=environment, check=False)
        return {
            "downloaded": downloaded,
            "clean_install_exit_code": install.returncode,
            "version_exit_code": version.returncode,
            "version_stdout": version.stdout.strip(),
            "status": "PASS" if install.returncode == 0 and version.returncode == 0 else "FAIL",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sdist-url", required=True)
    parser.add_argument("--sdist-sha256", required=True)
    parser.add_argument("--wheel-url", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = plan(args)
    if payload["status"] == "INVALID_PLAN":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 2
    if args.execute:
        try:
            payload["execution"] = execute(payload)
            payload["status"] = str(payload["execution"]["status"])
        except (OSError, ValueError, urllib.error.URLError, subprocess.SubprocessError) as exc:
            payload["execution"] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
            payload["status"] = "FAIL"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if payload["status"] in {"PREPARED_NOT_EXECUTED", "PASS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
