#!/usr/bin/env python3
"""Prepare a downstream-install verification; execute only with owner approval.

The default mode is deliberately non-mutating and does not contact Conda,
Bioconda, Docker, or any external channel. It refuses local paths and channel
guesses so a local package can never be represented as a published downstream
artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def safe_spec(value: str) -> bool:
    return bool(value) and not any(token in value for token in ("/", "\\", "file:", ".."))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-spec", required=True, help="e.g. methunmix=2.1.0rc1")
    parser.add_argument("--channel", required=True, help="owner-approved downstream channel name")
    parser.add_argument("--conda-executable", default="conda")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    problems: list[str] = []
    if not safe_spec(args.package_spec):
        problems.append("package_spec must be a package specification, not a path or local file URI")
    if not safe_spec(args.channel):
        problems.append("channel must be an owner-approved channel name, not a path or local file URI")
    payload: dict[str, object] = {
        "schema": "methunmix-downstream-verification-plan-v1",
        "scope": "downstream package install and entry-point verification only; no scientific or workflow claim",
        "package_spec": args.package_spec,
        "channel": args.channel,
        "problems": problems,
        "status": "INVALID_PLAN" if problems else ("EXECUTION_REQUESTED" if args.execute else "PREPARED_NOT_EXECUTED"),
    }
    if args.execute and not problems:
        with tempfile.TemporaryDirectory(prefix="methunmix-downstream-") as temporary:
            prefix = Path(temporary) / "prefix"
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            command = [args.conda_executable, "create", "--yes", "--prefix", str(prefix), "--channel", args.channel, args.package_spec]
            completed = subprocess.run(command, text=True, capture_output=True, env=environment, check=False)
            payload["execution"] = {
                "conda_create_exit_code": completed.returncode,
                "command": command,
                "status": "PASS" if completed.returncode == 0 else "FAIL",
            }
            payload["status"] = payload["execution"]["status"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if payload["status"] in {"PREPARED_NOT_EXECUTED", "PASS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
