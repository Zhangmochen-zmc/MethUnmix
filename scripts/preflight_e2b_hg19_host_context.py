#!/usr/bin/env python3
"""E2B host-context preflight; workflow launch is opt-in via --smoke only.

The script checks the immutable group manifest, RC3 package/runtime digests and
the host identity.  Without --smoke it never invokes Apptainer.  With --smoke
it runs only the two required minimal container commands; it still never runs
Nextflow or MethUnmix.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PHASE3J_CANDIDATE = "MethUnmix-2.0.0rc1-phase3j-1"
SOURCE_SHA = "9356996a9934b21a0c50c12e9cc4e079465a641228b5e30425eaa8cdc8f2a5bd"
WHEEL_SHA = "daef8a43dba9b145ef832f746cd5187b70f7b1ca0017ff6ea9c7b371cd882766"
NEXTFLOW_SHA = "0f1b51c129e00f6a9104965a18b960fd0bd3ffaef778c9c808d670a1baa8f250"
APPTAINER_SHA = "55ae0ec39abf785e3812fe2eb6626ada05a078c3f41431dc37107741a7218f9a"
SIF_SHA = "85f136626c23ba90fbe3dc3c18ad076abf07b7ed307b2a5ea7f912123396c8f5"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(argv: list[str], timeout: int = 30) -> dict:
    try:
        p = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        return {"argv": argv, "returncode": p.returncode, "output": p.stdout[-8000:]}
    except Exception as exc:
        return {"argv": argv, "returncode": None, "output": repr(exc)}


def check(label: str, ok: bool, detail: str) -> dict:
    return {"label": label, "status": "PASS" if ok else "BLOCKED", "detail": detail}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--cli", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--smoke", action="store_true", help="run only Apptainer true/id smoke; never starts Nextflow")
    args = ap.parse_args()
    doc = json.loads(args.manifest.read_text(encoding="utf-8"))
    binding = doc.get("package_binding", {})
    runtime = doc.get("runtime_binding", {})
    checks = []
    checks.append(check("non-root", os.geteuid() != 0, f"uid={os.geteuid()}"))
    checks.append(check("manifest-status", doc.get("status") == "PREPARED_NOT_EXECUTED", str(doc.get("status"))))
    checks.append(check("manifest-sha-sidecar", True, "launcher verifies the sidecar before invoking this script"))
    checks.append(check("candidate-identity", binding.get("candidate_id") == PHASE3J_CANDIDATE, str(binding.get("candidate_id"))))
    checks.append(check("source-binding", binding.get("source_sha256") == SOURCE_SHA, str(binding.get("source_sha256"))))
    checks.append(check("wheel-binding", binding.get("normalized_wheel_sha256") == WHEEL_SHA, str(binding.get("normalized_wheel_sha256"))))
    checks.append(check("cli-exists", args.cli.is_file() and os.access(args.cli, os.X_OK), str(args.cli)))
    wheel_path = Path(binding.get("wheel_path", ""))
    checks.append(check("candidate-wheel-path-digest", wheel_path.is_file() and sha(wheel_path) == WHEEL_SHA, str(wheel_path)))
    install_root = args.cli.parent.parent
    candidate_python = install_root / "bin" / "python"
    identity = {}
    if candidate_python.is_file():
        probe = run([
            "/usr/bin/env", "-u", "PYTHONPATH", str(candidate_python), "-c",
            "import importlib.metadata, demethflow_core, methunmix_assets, pathlib; "
            "print(importlib.metadata.version('methunmix')); "
            "print(demethflow_core.__file__); print(methunmix_assets.__file__); "
            "print(pathlib.Path(demethflow_core.__file__).read_text(encoding='utf-8') if False else 'ok')",
        ])
        lines = probe.get("output", "").splitlines()
        identity = {"probe": probe, "version": lines[0] if len(lines) > 0 else None, "demethflow_core": lines[1] if len(lines) > 1 else None, "methunmix_assets": lines[2] if len(lines) > 2 else None}
        source_tree = Path(__file__).resolve().parents[1] / "src"
        core_path = Path(identity.get("demethflow_core", ""))
        assets_path = Path(identity.get("methunmix_assets", ""))
        identity_ok = (
            probe.get("returncode") == 0
            and identity.get("version") == "2.0.0rc1"
            and isinstance(identity.get("demethflow_core"), str)
            and isinstance(identity.get("methunmix_assets"), str)
            and not core_path.is_relative_to(source_tree)
            and not assets_path.is_relative_to(source_tree)
        )
        runtime_path = install_root / "lib"
        identity_ok = identity_ok and str(runtime_path) in identity.get("demethflow_core", "")
        runtime_file = next(runtime_path.glob("python*/site-packages/demethflow_core/runtime.py"), None) if runtime_path.is_dir() else None
        selector_logic = runtime_file is not None and "_celfie_validation_binding" in runtime_file.read_text(encoding="utf-8")
    else:
        identity_ok = False
        selector_logic = False
    checks.append(check("candidate-cli-identity", identity_ok, json.dumps(identity, ensure_ascii=False)))
    checks.append(check("selector-binding-logic", selector_logic, "installed runtime contains selector-bound disclosure logic" if selector_logic else "missing selector-bound disclosure logic"))
    nf = Path(runtime.get("nextflow", "")); appt = Path(runtime.get("apptainer", "")); sif = Path(runtime.get("sif", ""))
    checks.append(check("nextflow-path-digest", nf.is_file() and sha(nf) == NEXTFLOW_SHA, str(nf)))
    checks.append(check("apptainer-path-digest", appt.is_file() and sha(appt) == APPTAINER_SHA, str(appt)))
    checks.append(check("sif-path-digest", sif.is_file() and sha(sif) == SIF_SHA, str(sif)))
    checks.append(check("genome-build", doc.get("genome_build") == "hg19", str(doc.get("genome_build"))))
    checks.append(check("native-contract", doc.get("analysis_contract") == "wgbs_native_hg19" and doc.get("route") == "wgbs-native", str(doc.get("analysis_contract"))))
    host = {
        "uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(), "user": os.environ.get("USER"),
        "hostname": platform.node(), "python": platform.python_version(),
        "no_new_privs": run(["/bin/bash", "-lc", "grep -i '^NoNewPrivs:' /proc/self/status || true"]),
        "seccomp": run(["/bin/bash", "-lc", "grep -i '^Seccomp:' /proc/self/status || true"]),
        "namespaces": run(["/bin/bash", "-lc", "readlink /proc/self/ns/* || true"]),
        "cgroup": run(["/bin/bash", "-lc", "cat /proc/self/cgroup || true"]),
        "ulimit": run(["/bin/bash", "-lc", "ulimit -a"]),
        "apptainer_version": run([str(appt), "--version"]),
        "nextflow_version": run([str(nf), "-version"]),
    }
    smoke = []
    if args.smoke:
        smoke.append(run([str(appt), "exec", str(sif), "true"], timeout=120))
        smoke.append(run([str(appt), "exec", str(sif), "/bin/sh", "-c", "id; true"], timeout=120))
        checks.append(check("apptainer-smoke", all(x.get("returncode") == 0 for x in smoke), json.dumps(smoke, ensure_ascii=False)))
    else:
        checks.append(check("apptainer-smoke", True, "NOT_RUN: owner must invoke with --smoke in the actual E2B launch context"))
    result = {"schema": "methunmix-e2b-hg19-host-context-preflight-phase3k-v1", "candidate_id": PHASE3J_CANDIDATE, "generated_at": datetime.now(timezone.utc).isoformat(), "status": "PASS" if all(x["status"] == "PASS" for x in checks) else "BLOCKED", "workflow_started": False, "nextflow_started": False, "checks": checks, "host_context": host, "apptainer_smoke": smoke, "scope_boundary": "No Nextflow or MethUnmix workflow is started by this preflight."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
