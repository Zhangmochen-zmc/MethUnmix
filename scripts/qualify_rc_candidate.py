#!/usr/bin/env python3
"""Build and locally qualify a MethUnmix RC without publication side effects.

The script deliberately emits ``LOCAL_RC_CANDIDATE_QUALIFIED`` rather than a
public-release status.  It does not create a tag, contact an asset server,
modify scientific states, or substitute for external Bioconda/production
gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import venv
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 180) -> dict[str, object]:
    completed = subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-12000:],
        "stderr": completed.stderr[-12000:],
        "status": "PASS" if completed.returncode == 0 else "FAIL",
    }


def clean_install_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment that cannot satisfy a wheel import from source.

    Qualification intentionally uses ``PYTHONPATH`` while testing the source
    tree and the extracted source artifact.  A clean-wheel installation is a
    different gate: inheriting a caller's ``PYTHONPATH`` can make pip see a
    same-version ``*.egg-info`` in a checkout, decide the wheel is already
    installed, and leave the new virtual environment without its console
    scripts.  Remove it before both pip install and every installed-wheel
    smoke command.
    """
    environment = dict(os.environ if base is None else base)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def extract_source_archive(archive_path: Path, destination: Path, *, expected_root: str) -> Path:
    """Extract the produced source archive without accepting archive path tricks.

    Qualification must exercise the exact artifact users receive, rather than
    another view of the live checkout.  The archive is produced locally, but
    this still validates it before extraction so a future packaging regression
    cannot turn the release gate into an unsafe extractor.
    """
    if destination.exists():
        raise ValueError(f"extraction destination already exists: {destination}")
    destination.mkdir(parents=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        seen: set[str] = set()
        for member in members:
            name = member.name
            path = PurePosixPath(name)
            if (
                not member.isfile()
                or path.is_absolute()
                or "\\" in name
                or not path.parts
                or path.parts[0] != expected_root
                or any(part in {"", ".", ".."} for part in path.parts)
                or name in seen
            ):
                raise ValueError(f"unsafe source archive member: {name}")
            seen.add(name)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"source archive member has no payload: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            # Source snapshot identity includes executable mode for portable
            # helper scripts.  Preserve it so rebuilding the extracted source
            # validates byte-for-byte archive reproducibility rather than only
            # member content equality.
            os.chmod(target, member.mode & 0o777)
    root = destination / expected_root
    if not root.is_dir():
        raise ValueError(f"source archive has no expected root: {expected_root}")
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="new or empty qualification directory outside the source tree")
    parser.add_argument(
        "--release-evidence-output", type=Path,
        help="optional compact, durable gate record; full command logs remain only in --output-dir",
    )
    parser.add_argument(
        "--reference-transport-evidence", type=Path,
        help=(
            "optional durable public-reference transport record to bind by digest; "
            "use this when qualifying an isolated source snapshot"
        ),
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    output = args.output_dir.expanduser().resolve()
    try:
        output.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise SystemExit("--output-dir must be outside the source tree")
    if output.exists() and any(output.iterdir()):
        raise SystemExit("--output-dir must be empty to prevent mixing qualification evidence")
    output.mkdir(parents=True, exist_ok=True)
    source_dir = output / "source"
    wheel_dir = output / "wheel"
    venv_dir = output / "venv"
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src")

    version_audit = run(
        [sys.executable, "scripts/audit_version_identity.py", "--expect-version", (ROOT / "VERSION").read_text(encoding="utf-8").strip(), "--output", str(output / "version_identity.json")],
        cwd=ROOT, env=env, timeout=args.timeout,
    )
    tests = run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT, env=env, timeout=args.timeout,
    )
    source_build = run(
        [sys.executable, "scripts/build_source_snapshot.py", "--output", str(source_dir)],
        cwd=ROOT, env=env, timeout=args.timeout,
    )
    wheel_build = run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(wheel_dir)],
        cwd=ROOT, env=env, timeout=args.timeout,
    )

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    source = source_dir / f"methunmix-{version}.tar.gz"
    wheel = wheel_dir / f"methunmix-{version}-py3-none-any.whl"
    wheel_normalization = run(
        [sys.executable, "scripts/normalize_wheel.py", str(wheel)],
        cwd=ROOT, env=env, timeout=args.timeout,
    ) if wheel.is_file() else {
        "status": "NOT_PERFORMED", "reason": "wheel build did not produce expected artifact"
    }
    extracted_source: dict[str, object] = {"status": "NOT_PERFORMED"}
    if source.is_file():
        try:
            extracted_root = extract_source_archive(
                source,
                output / "extracted-source",
                expected_root=f"methunmix-{version}",
            )
            extracted_env = os.environ.copy()
            extracted_env["PYTHONDONTWRITEBYTECODE"] = "1"
            # This must refer to the extracted artifact, never the live source
            # checkout that initiated qualification.
            extracted_env["PYTHONPATH"] = str(extracted_root / "src")
            extracted_tests = run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                cwd=extracted_root, env=extracted_env, timeout=args.timeout,
            )
            extracted_rebuild_dir = extracted_root / "rebuild"
            extracted_source_rebuild = run(
                [sys.executable, "scripts/build_source_snapshot.py", "--output", str(extracted_rebuild_dir)],
                cwd=extracted_root, env=extracted_env, timeout=args.timeout,
            )
            rebuilt_source = extracted_rebuild_dir / source.name
            extracted_wheel_dir = extracted_root / "wheel"
            extracted_wheel_build = run(
                [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(extracted_wheel_dir)],
                cwd=extracted_root, env=extracted_env, timeout=args.timeout,
            )
            extracted_wheel = extracted_wheel_dir / wheel.name
            extracted_payload_audit = run(
                [
                    sys.executable, "scripts/audit_conda_payload.py",
                    "--wheel", str(extracted_wheel), "--source", str(source),
                    "--skip-recipe-binding", "--output", str(output / "extracted_source_payload_audit.json"),
                ],
                cwd=extracted_root, env=extracted_env, timeout=args.timeout,
            ) if extracted_wheel.is_file() else {
                "status": "NOT_PERFORMED", "reason": "extracted source wheel build did not produce expected artifact"
            }
            source_rebuild_matches = rebuilt_source.is_file() and digest(rebuilt_source) == digest(source)
            extracted_source = {
                "status": "PASS" if (
                    extracted_tests.get("status") == "PASS"
                    and extracted_source_rebuild.get("status") == "PASS"
                    and source_rebuild_matches
                    and extracted_wheel_build.get("status") == "PASS"
                    and extracted_payload_audit.get("status") == "PASS"
                ) else "FAIL",
                "source_archive_sha256": digest(source),
                "rebuild_source_sha256": digest(rebuilt_source) if rebuilt_source.is_file() else None,
                "rebuild_matches_source": source_rebuild_matches,
                "extracted_wheel_sha256": digest(extracted_wheel) if extracted_wheel.is_file() else None,
                "tests": extracted_tests,
                "source_rebuild": extracted_source_rebuild,
                "wheel_build": extracted_wheel_build,
                "payload_audit": extracted_payload_audit,
            }
        except (OSError, tarfile.TarError, ValueError, subprocess.TimeoutExpired) as exc:
            extracted_source = {
                "status": "FAIL",
                "source_archive_sha256": digest(source),
                "failure": f"{type(exc).__name__}: {exc}",
            }
    payload_audit = run(
        [sys.executable, "scripts/audit_conda_payload.py", "--wheel", str(wheel), "--source", str(source), "--skip-recipe-binding", "--output", str(output / "payload_audit.json")],
        cwd=ROOT, env=env, timeout=args.timeout,
    ) if source.is_file() and wheel.is_file() else {
        "status": "NOT_PERFORMED", "reason": "source or wheel build did not produce expected artifact"
    }
    license_payload_boundary = run(
        [
            sys.executable, "scripts/audit_license_inventory.py",
            "--source-archive", str(source), "--wheel", str(wheel),
            "--require-complete-payload-boundary",
            "--report", str(output / "license_inventory.json"),
            "--csv", str(output / "license_inventory.csv"),
            "--summary-md", str(output / "license_inventory.md"),
        ],
        cwd=ROOT, env=env, timeout=args.timeout,
    ) if source.is_file() and wheel.is_file() else {
        "status": "NOT_PERFORMED", "reason": "source or wheel build did not produce expected artifact"
    }

    clean_install: dict[str, object] = {"status": "NOT_PERFORMED"}
    if wheel.is_file():
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        pip = bin_dir / ("pip.exe" if os.name == "nt" else "pip")
        install_env = clean_install_environment()
        install = run(
            [str(pip), "install", "--no-deps", "--no-index", str(wheel)],
            cwd=output,
            env=install_env,
            timeout=args.timeout,
        )
        smoke_env = install_env
        commands = {
            "install": install,
            "methunmix_version": run([str(bin_dir / "methunmix"), "--version"], cwd=output, env=smoke_env, timeout=args.timeout),
            "demethflow_version": run([str(bin_dir / "demethflow"), "--version"], cwd=output, env=smoke_env, timeout=args.timeout),
            "methunmix_help": run([str(bin_dir / "methunmix"), "--help"], cwd=output, env=smoke_env, timeout=args.timeout),
            "adapter_registry": run(
                [str(bin_dir / "methunmix"), "adapter", "validate", "--json"],
                cwd=output, env=smoke_env, timeout=args.timeout,
            ),
            "benchmark_help": run(
                [str(bin_dir / "methunmix"), "benchmark", "--help"],
                cwd=output, env=smoke_env, timeout=args.timeout,
            ),
            "reference_lifecycle_help": run(
                [str(bin_dir / "methunmix"), "reference", "lifecycle", "--help"],
                cwd=output, env=smoke_env, timeout=args.timeout,
            ),
            "installed_identity": run(
                [str(python), "-c", "import json; from demethflow_core import version; from demethflow_core.asset_manager import load_catalog; from importlib.resources import files; r=files('methunmix_assets'); reg=json.loads((r/'capabilities/capability_registry.json').read_text()); print(json.dumps({'core_version':version(),'registry_version':reg['software_version'],'catalog_core_version':load_catalog()['core_version']}))"],
                cwd=output, env=smoke_env, timeout=args.timeout,
            ),
        }
        clean_install = {
            "status": "PASS" if all(item.get("status") == "PASS" for item in commands.values()) else "FAIL",
            "source_tree_on_pythonpath": False,
            "commands": commands,
        }
        write_json(output / "clean_install.json", clean_install)

    reference_gate = (
        args.reference_transport_evidence
        if args.reference_transport_evidence is not None
        else ROOT / "evidence" / "reference_public_distribution_gate_rc3.json"
    ).expanduser().resolve()
    reference_gate_value: dict[str, object] = {"status": "MISSING", "path": str(reference_gate)}
    if reference_gate.is_file():
        payload = json.loads(reference_gate.read_text(encoding="utf-8"))
        reference_gate_value = {
            "status": payload.get("overall_status"),
            "path": str(reference_gate),
            "sha256": digest(reference_gate),
            "scope": payload.get("scope"),
        }

    license_inventory_path = output / "license_inventory.json"
    license_boundary_evidence: dict[str, object] = {"status": "MISSING", "path": str(license_inventory_path)}
    if license_inventory_path.is_file():
        license_inventory = json.loads(license_inventory_path.read_text(encoding="utf-8"))
        raw_boundary = license_inventory.get("inputs", {}).get("public_payload_boundary", {})
        raw_artifacts = raw_boundary.get("artifacts", []) if isinstance(raw_boundary, dict) else []
        artifact_summary = [
            {
                key: item.get(key)
                for key in ("kind", "sha256", "member_count", "vendor_member_count", "status")
            }
            for item in raw_artifacts
            if isinstance(item, dict)
        ]
        license_boundary_evidence = {
            "status": license_inventory.get("payload_boundary_gate", {}).get("status"),
            "payload_boundary_status": raw_boundary.get("status") if isinstance(raw_boundary, dict) else None,
            "source_and_wheel": artifact_summary,
            "scope": "core staged source/wheel payload boundary only; no external runtime or asset redistribution approval",
        }

    local_checks = {
        "VERSION_IDENTITY": version_audit.get("status") == "PASS",
        "SOURCE_TESTS": tests.get("status") == "PASS",
        "SOURCE_SNAPSHOT": source_build.get("status") == "PASS" and source.is_file(),
        "EXTRACTED_SOURCE_ARCHIVE": extracted_source.get("status") == "PASS",
        "WHEEL_BUILD_NO_ISOLATION": wheel_build.get("status") == "PASS" and wheel.is_file(),
        "WHEEL_NORMALIZATION": wheel_normalization.get("status") == "PASS",
        "PAYLOAD_AUDIT": payload_audit.get("status") == "PASS",
        "LICENSE_CORE_PAYLOAD_BOUNDARY": license_payload_boundary.get("status") == "PASS",
        "CLEAN_WHEEL_INSTALL": clean_install.get("status") == "PASS",
        "PUBLIC_REFERENCE_TRANSPORT": reference_gate_value.get("status") == "TECHNICAL_REFERENCE_AUTODOWNLOAD_READY",
    }
    payload = {
        "schema": "methunmix-rc-local-qualification-v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "core_version": version,
        "source_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
        "artifacts": {
            "source": {"path": str(source), "sha256": digest(source) if source.is_file() else None},
            "wheel": {"path": str(wheel), "sha256": digest(wheel) if wheel.is_file() else None},
        },
        "checks": local_checks,
        "evidence": {
            "version_identity": version_audit,
            "source_tests": tests,
            "source_snapshot": source_build,
            "extracted_source_archive": extracted_source,
            "wheel_build": wheel_build,
            "wheel_normalization": wheel_normalization,
            "payload_audit": payload_audit,
            "license_payload_boundary": license_payload_boundary,
            "clean_install": clean_install,
            "reference_transport": reference_gate_value,
            "license_core_payload_boundary": license_boundary_evidence,
        },
        "build_isolation": {
            "status": "NOT_PERFORMED",
            "reason": "isolated build backend dependency bootstrap requires network access in this environment; no-isolation build used already-installed setuptools/wheel",
        },
        "publication": {
            "git_tag_created": False,
            "remote_upload": False,
            "bioconda_pr": False,
            "scientific_status_changed": False,
        },
        "overall_status": "LOCAL_RC_CANDIDATE_QUALIFIED" if all(local_checks.values()) else "LOCAL_RC_CANDIDATE_BLOCKED",
        "remaining_external_gates": [
            "owner release approval", "public tagged core artifact verification", "full license/SBOM release scope",
            "external Bioconda/mulled CI", "production rollback/revocation drill", "GPU and scientific gates per declared scope",
        ],
    }
    write_json(output / "rc_local_qualification.json", payload)
    if args.release_evidence_output:
        durable = args.release_evidence_output.expanduser().resolve()
        extracted_source_evidence = {
            key: extracted_source.get(key)
            for key in (
                "status",
                "source_archive_sha256",
                "rebuild_source_sha256",
                "rebuild_matches_source",
                "extracted_wheel_sha256",
            )
        }
        extracted_source_evidence["checks"] = {
            name: (extracted_source.get(name) or {}).get("status")
            for name in ("tests", "source_rebuild", "wheel_build", "payload_audit")
        }
        extracted_source_evidence["scope"] = (
            "safe extraction of the exact staged source archive, portable source tests, "
            "deterministic source rebuild, wheel build, and payload audit; no workflow execution"
        )
        compact = {
            "schema": "methunmix-rc-local-release-gate-v1",
            "generated_at": payload["generated_at"],
            "core_version": payload["core_version"],
            "source_commit": payload["source_commit"],
            "artifact_sha256": {
                "source": payload["artifacts"]["source"]["sha256"],
                "wheel": payload["artifacts"]["wheel"]["sha256"],
            },
            "local_gates": payload["checks"],
            "local_qualification_status": payload["overall_status"],
            "build_isolation": payload["build_isolation"],
            "extracted_source_archive_evidence": extracted_source_evidence,
            "reference_transport_evidence": reference_gate_value,
            "license_core_payload_boundary_evidence": license_boundary_evidence,
            "publication": payload["publication"],
            "remaining_external_gates": payload["remaining_external_gates"],
            "scope": (
                "local RC3 qualification only; source and wheel were built in an isolated temporary "
                "qualification directory and are not public release artifacts"
            ),
        }
        write_json(durable, compact)
    print(json.dumps({"overall_status": payload["overall_status"], "evidence": str(output / "rc_local_qualification.json")}, ensure_ascii=False))
    return 0 if payload["overall_status"] == "LOCAL_RC_CANDIDATE_QUALIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
