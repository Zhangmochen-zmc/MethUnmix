#!/usr/bin/env python3
"""Clean-wheel install and Nextflow/Apptainer smoke for packaged Python helpers.

The smoke is deliberately limited to ``--help`` invocations. It proves that
the helpers included in a wheel can be reached from the installed workflow
bridge and started through Python inside the real method runtimes; it does not
run any scientific workflow, modify input/reference assets, or require chmod.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HELPERS = {
    "ARIC": ("aric_decon.py", "aric_image"),
    "MEnet": ("menet.py", "menet_image"),
    "MethAtlas": ("methatlas_decon.py", "methatlas_image"),
    "MEnet_standardizer": ("menet_standardize.py", "menet_image"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def mode(path: Path) -> str:
    return f"{path.stat().st_mode & 0o777:04o}"


def run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=env, text=True, capture_output=True,
            check=False, timeout=timeout,
        )
        return {
            "command": command,
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "exit_code": None,
            "stdout": str(exc.stdout or "")[-4000:],
            "stderr": str(exc.stderr or "")[-4000:],
            "timed_out": True,
        }


def json_cli_identity(python: Path, cwd: Path, env: dict[str, str], bridge: Path) -> tuple[Path, dict[str, Any]]:
    probe = (
        "import json,sys; from pathlib import Path; "
        "from demethflow_core import PROJECT_ROOT; "
        "from demethflow_core.runner import _bridge_workflow_command; "
        "b=Path(sys.argv[1]); "
        "_bridge_workflow_command([str(PROJECT_ROOT/'deconvolution'/'450k_main.nf')],b); "
        "print(json.dumps({'project_root':str(PROJECT_ROOT),'sys_path':sys.path}))"
    )
    result = run([str(python), "-c", probe, str(bridge)], cwd=cwd, env=env, timeout=30)
    if result["exit_code"] != 0:
        raise RuntimeError(f"installed bridge materialization failed: {result['stderr']}")
    payload = json.loads(result["stdout"].strip().splitlines()[-1])
    return Path(payload["project_root"]), payload


def make_smoke_workflow(path: Path, images: dict[str, Path], output_dir: Path) -> None:
    image_literal = {key: str(value).replace("\\", "\\\\").replace("'", "\\'") for key, value in images.items()}
    output_literal = str(output_dir).replace("\\", "\\\\").replace("'", "\\'")
    text = f'''nextflow.enable.dsl=2
params.aric_image = '{image_literal["aric_image"]}'
params.menet_image = '{image_literal["menet_image"]}'
params.methatlas_image = '{image_literal["methatlas_image"]}'
params.output_dir = '{output_literal}'

process PACKAGED_ARIC_HELPER_SMOKE {{
    container params.aric_image
    publishDir params.output_dir, mode: 'copy'
    input: val trigger
    output: path 'aric_helper_help.txt'
    script:
    """
    python3 "${{projectDir}}/bin/aric_decon.py" --help > aric_helper_help.txt
    """
}}

process PACKAGED_MENET_HELPER_SMOKE {{
    container params.menet_image
    publishDir params.output_dir, mode: 'copy'
    input: path trigger
    output: path 'menet_helper_help.txt'
    script:
    """
    python3 "${{projectDir}}/bin/menet.py" --help > menet_helper_help.txt
    """
}}

process PACKAGED_METHATLAS_HELPER_SMOKE {{
    container params.methatlas_image
    publishDir params.output_dir, mode: 'copy'
    input: path trigger
    output: path 'methatlas_helper_help.txt'
    script:
    """
    python3 "${{projectDir}}/bin/methatlas_decon.py" --help > methatlas_helper_help.txt
    """
}}

process PACKAGED_MENET_STANDARDIZER_SMOKE {{
    container params.menet_image
    publishDir params.output_dir, mode: 'copy'
    input: path trigger
    output: path 'menet_standardizer_help.txt'
    script:
    """
    python3 "${{projectDir}}/bin/menet_standardize.py" --help > menet_standardizer_help.txt
    """
}}

workflow {{
    seed = channel.value('start')
    aric_done = PACKAGED_ARIC_HELPER_SMOKE(seed)
    menet_done = PACKAGED_MENET_HELPER_SMOKE(aric_done)
    methatlas_done = PACKAGED_METHATLAS_HELPER_SMOKE(menet_done)
    PACKAGED_MENET_STANDARDIZER_SMOKE(methatlas_done)
}}
'''
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--nextflow", type=Path, required=True)
    parser.add_argument("--apptainer", type=Path, required=True)
    parser.add_argument("--aric-image", type=Path, required=True)
    parser.add_argument("--menet-image", type=Path, required=True)
    parser.add_argument("--methatlas-image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error(f"refusing to overwrite existing smoke evidence: {args.output}")

    paths = {
        "wheel": args.wheel.resolve(), "sdist": args.sdist.resolve(),
        "nextflow": args.nextflow.resolve(), "apptainer": args.apptainer.resolve(),
        "aric_image": args.aric_image.resolve(), "menet_image": args.menet_image.resolve(),
        "methatlas_image": args.methatlas_image.resolve(),
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        parser.error(f"required local artifact/runtime is missing: {missing}")

    report: dict[str, Any] = {
        "schema": "methunmix-clean-wheel-packaged-helper-smoke-v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "PENDING",
        "scope": "isolated offline wheel installation; installed workflow bridge; helper --help through Nextflow/Apptainer; no scientific data inference",
        "artifacts": {
            "wheel": {"path": str(paths["wheel"]), "sha256": sha256(paths["wheel"])},
            "sdist": {"path": str(paths["sdist"]), "sha256": sha256(paths["sdist"])},
        },
        "runtimes": {
            key: {"path": str(path), "sha256": sha256(path)}
            for key, path in paths.items() if key.endswith("_image")
        },
        "network": "OFFLINE_NXF_OFFLINE; pip --no-index",
        "chmod_used": False,
        "source_tree_fallback_used": False,
        "cases": {},
    }

    with tempfile.TemporaryDirectory(prefix="methunmix-clean-helper-wheel-") as temporary:
        temp = Path(temporary).resolve()
        venv_dir = temp / "venv"
        bridge = temp / "installed-workflow-bridge"
        output_dir = temp / "published"
        work_dir = temp / "nextflow-work"
        for directory in (bridge, output_dir, work_dir, temp / "home", temp / "nxf-home", temp / "apptainer-cache"):
            directory.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        scripts_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
        cli = scripts_dir / ("methunmix.exe" if os.name == "nt" else "methunmix")
        base_path = ":".join([
            str(scripts_dir), str(paths["apptainer"].parent), str(paths["nextflow"].parent),
            "/usr/bin", "/bin",
        ])
        env = {
            key: value for key, value in os.environ.items()
            if key not in {
                "PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX", "JAVA_HOME",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy",
            }
        }
        env.update({
            "PATH": base_path,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NXF_OFFLINE": "true",
            "NXF_DISABLE_CHECK_LATEST": "true",
            "NXF_HOME": str(temp / "nxf-home"),
            "NXF_WORK": str(work_dir),
            "APPTAINER_CACHEDIR": str(temp / "apptainer-cache"),
            "SINGULARITY_CACHEDIR": str(temp / "apptainer-cache"),
            "METHUNMIX_HOME": str(temp / "home"),
        })

        install = run([
            str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--no-index", "--no-deps", str(paths["wheel"]),
        ], cwd=temp, env=env, timeout=120)
        report["install"] = install
        if install["exit_code"] != 0:
            report["status"] = "FAIL_CLEAN_INSTALL"
        else:
            version = run([str(cli), "--version"], cwd=temp, env=env, timeout=30)
            help_result = run([str(cli), "--help"], cwd=temp, env=env, timeout=30)
            report["cli"] = {"version": version, "help": help_result}
            if version["exit_code"] != 0 or help_result["exit_code"] != 0:
                report["status"] = "FAIL_CLI_SMOKE"
            else:
                project_root, identity = json_cli_identity(python, temp, env, bridge)
                try:
                    project_root.relative_to(venv_dir.resolve())
                    installed_only = True
                except ValueError:
                    installed_only = False
                report["installed_package"] = {
                    "project_root": str(project_root),
                    "inside_isolated_venv": installed_only,
                    "source_checkout_in_sys_path": any(str(ROOT) in item for item in identity["sys_path"]),
                    "bridge": str(bridge),
                }
                if not installed_only or report["installed_package"]["source_checkout_in_sys_path"]:
                    report["status"] = "FAIL_SOURCE_TREE_ISOLATION"
                else:
                    # The new package must work even though wheel installation
                    # leaves normal Python data files non-executable.
                    helper_checks = []
                    for tool, (name, image_key) in HELPERS.items():
                        helper = bridge / "bin" / name
                        if not helper.is_file():
                            helper_checks.append({"tool": tool, "status": "FAIL_HELPER_NOT_IN_BRIDGE", "path": str(helper)})
                            continue
                        helper_checks.append({
                            "tool": tool,
                            "helper": str(helper),
                            "installed_mode": mode(project_root / "deconvolution" / "bin" / name),
                            "bridge_mode": mode(helper),
                            "executable_bit_required": False,
                            "runtime": image_key,
                        })
                    report["helper_inventory"] = helper_checks
                    smoke_nf = bridge / "packaged_helper_smoke.nf"
                    make_smoke_workflow(smoke_nf, {
                        key: paths[key] for key in ("aric_image", "menet_image", "methatlas_image")
                    }, output_dir)
                    config = temp / "packaged_helper_smoke.config"
                    config.write_text("""profiles {
  local { process.executor = 'local' }
  apptainer {
    apptainer.enabled = true
    apptainer.autoMounts = true
  }
}
process {
  maxForks = 1
  shell = ['/bin/bash', '-euo', 'pipefail']
}
""", encoding="utf-8")
                    nextflow = run([
                        str(paths["nextflow"]), "run", str(smoke_nf), "-c", str(config),
                        "-profile", "local,apptainer", "-work-dir", str(work_dir),
                        "-ansi-log", "false",
                    ], cwd=temp, env=env, timeout=args.timeout_seconds)
                    expected_outputs = [
                        "aric_helper_help.txt", "menet_helper_help.txt",
                        "methatlas_helper_help.txt", "menet_standardizer_help.txt",
                    ]
                    published = {name: (output_dir / name).is_file() for name in expected_outputs}
                    report["nextflow_smoke"] = {
                        "result": nextflow,
                        "workflow_path": str(smoke_nf),
                        "project_dir": str(bridge),
                        "published_outputs": published,
                        "all_helpers_started_by_nextflow": all(published.values()),
                    }
                    if nextflow["exit_code"] == 0 and all(published.values()):
                        report["status"] = "PASS_CLEAN_INSTALL_NEXTFLOW_HELPER_SMOKE"
                    else:
                        report["status"] = "FAIL_NEXTFLOW_HELPER_SMOKE"

    report["success"] = report["status"] == "PASS_CLEAN_INSTALL_NEXTFLOW_HELPER_SMOKE"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "success": report["success"],
        "wheel_sha256": report["artifacts"]["wheel"]["sha256"],
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
