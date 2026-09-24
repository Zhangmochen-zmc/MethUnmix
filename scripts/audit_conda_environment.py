#!/usr/bin/env python3
"""Read-only probe for local Bioconda/mulled prerequisites."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evidence" / "bioconda_environment_audit.json"


def probe(command: list[str], timeout: int = 5) -> dict:
    try:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired:
        return {"available": True, "exit_code": None, "output": "TIMEOUT", "timed_out": True}
    except OSError as exc:
        return {"available": False, "exit_code": None, "output": str(exc), "timed_out": False}
    output = (result.stdout + result.stderr).strip()
    return {
        "available": result.returncode == 0,
        "exit_code": result.returncode,
        "output": output[:2000],
        "timed_out": False,
    }


def conda_prefixes(conda_path: str | None) -> tuple[list[Path], dict]:
    if not conda_path:
        return [], {"available": False, "reason": "conda executable not found"}
    try:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [conda_path, "info", "--envs", "--json"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return [], {"available": False, "reason": "conda info --envs --json timed out"}
    except OSError as exc:
        return [], {"available": False, "reason": str(exc)}
    if result.returncode != 0:
        return [], {
            "available": False,
            "exit_code": result.returncode,
            "reason": (result.stderr or result.stdout).strip()[:1000],
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [], {"available": False, "reason": f"invalid conda env JSON: {exc}"}
    prefixes = [Path(value) for value in payload.get("envs", []) if isinstance(value, str)]
    return prefixes, {"available": True, "prefix_count": len(prefixes)}


def locate_tool(name: str, path: str | None, prefixes: list[Path]) -> tuple[str | None, str, str | None]:
    if path:
        return path, "PATH", None
    executable_names = [name]
    if os.name == "nt":
        executable_names.extend([f"{name}.exe", f"{name}.bat"])
    for prefix in prefixes:
        for relative in ("bin", "Scripts"):
            for executable in executable_names:
                candidate = prefix / relative / executable
                if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                    return str(candidate), "CONDA_ENV", str(prefix)
    return None, "NOT_FOUND", None


def main() -> int:
    names = ("conda", "bioconda-utils", "conda-build", "docker", "podman")
    path_tools = {name: shutil.which(name) for name in names}
    prefixes, env_discovery = conda_prefixes(path_tools["conda"])
    tools = {}
    for name in names:
        path, discovery, prefix = locate_tool(name, path_tools[name], prefixes)
        tools[name] = {"path": path, "discovery": discovery}
        if prefix:
            tools[name]["environment_prefix"] = prefix
    tools["conda"]["probe"] = probe([tools["conda"]["path"], "--version"]) if tools["conda"]["path"] else {"available": False}
    tools["bioconda-utils"]["probe"] = probe([tools["bioconda-utils"]["path"], "--version"]) if tools["bioconda-utils"]["path"] else {"available": False}
    tools["conda-build"]["probe"] = probe([tools["conda-build"]["path"], "--version"]) if tools["conda-build"]["path"] else {"available": False}
    tools["docker"]["daemon_probe"] = (
        probe([tools["docker"]["path"], "info", "--format", "{{.ServerVersion}}"])
        if tools["docker"]["path"] else {"available": False, "output": "not installed"}
    )
    tools["podman"]["daemon_probe"] = (
        probe([tools["podman"]["path"], "info", "--format", "{{.host.version}}"])
        if tools["podman"]["path"] else {"available": False, "output": "not installed"}
    )
    local_ready = bool(
        tools["conda"]["path"]
        and tools["conda-build"]["path"]
        and tools["bioconda-utils"]["path"]
        and (tools["docker"]["daemon_probe"]["available"] or tools["podman"]["daemon_probe"]["available"])
    )
    payload = {
        "schema": "methunmix-bioconda-environment-audit-v1",
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "LOCAL_BIOCONDA_TESTS_AVAILABLE" if local_ready else "PENDING_EXTERNAL_CI",
        "local_ready": local_ready,
        "side_effects": "read_only; only bounded version/engine/environment-list probes; no packages installed; no network build or test started; Python bytecode writing disabled for probed tools",
        "conda_environment_discovery": env_discovery,
        "tools": tools,
        "policy": "A local Bioconda mulled test requires conda, conda-build, bioconda-utils, and a usable Docker or Podman daemon. Missing prerequisites keep the recipe at PENDING_EXTERNAL_CI; do not request user test files.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
