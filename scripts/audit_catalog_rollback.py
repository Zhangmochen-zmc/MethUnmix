#!/usr/bin/env python3
"""Run local mocked static-catalog rollback/revocation checks.

This is intentionally not an object-store or production rollback drill.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = ROOT / "tests/test_methunmix_rc.py"
TESTS = [
    "test_static_catalog_blocks_rewrite_at_same_sequence_and_rollback",
    "test_static_catalog_fetch_rejects_revoked_target_before_network",
    "test_static_catalog_fetch_rejects_content_hash_mismatch",
]


def main() -> int:
    output = ROOT / "evidence/catalog_rollback_audit_rc.json"
    command = [
        sys.executable,
        "-m",
        "unittest",
        *[f"test_methunmix_rc.MethUnmixControlPlaneTests.{name}" for name in TESTS],
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), str(ROOT / "tests"), env.get("PYTHONPATH", "")]))
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    passed = result.returncode == 0
    report = {
        "schema": "methunmix-static-catalog-rollback-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "LOCAL_TESTS_PASS_EXTERNAL_DRILL_PENDING" if passed else "LOCAL_TESTS_FAILED",
        "scope": "local_mocked_static_catalog_tests",
        "test_source": "tests/test_methunmix_rc.py",
        "test_source_sha256": hashlib.sha256(TEST_FILE.read_bytes()).hexdigest(),
        "tests": TESTS,
        "local_test_exit_code": result.returncode,
        "local_test_output": (result.stdout + result.stderr)[-12000:],
        "external_object_store_rollback_revocation_drill": "PENDING_EXTERNAL_ENVIRONMENT",
        "production_rollback_claim": False,
        "slurm_used": False,
        "network_used_by_audit": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "local_test_output"}, ensure_ascii=False, indent=2))
    if not passed:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
