#!/usr/bin/env python3
"""Bounded, non-scientific A-J regression suite for telemetry-v3."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TELEMETRY = HERE / "celfeer_telemetry_v2.py"


def module():
    spec = importlib.util.spec_from_file_location("celfeer_telemetry_v2", TELEMETRY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def run_case(name: str, code: str, *, interval: float = 0.05, cwd: Path | None = None, expect_sampler: str = "PASS") -> dict:
    with tempfile.TemporaryDirectory(prefix="methunmix-telemetry-case-") as td:
        root = Path(td)
        output = root / "runtime.jsonl"
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=cwd or root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sampler = subprocess.Popen([sys.executable, str(TELEMETRY), "--pid", str(proc.pid), "--output", str(output), "--interval", str(interval), "--stage", "SYNTHETIC_REGRESSION", "--task-id", name, "--stop-when-root-exits", "--max-seconds", "8"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            workload_rc = proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            workload_rc = 124
        sampler_stdout, sampler_stderr = sampler.communicate(timeout=8)
        try:
            sampler_report = json.loads(sampler_stdout)
        except Exception:
            sampler_report = {"status": "INVALID_SAMPLER_JSON", "stdout": sampler_stdout, "stderr": sampler_stderr}
        valid = workload_rc == 0 and sampler_report.get("status") == expect_sampler
        if expect_sampler == "PASS":
            valid = valid and sampler_report.get("validation", {}).get("valid", False)
        return {"name": name, "workload_return_code": workload_rc, "sampler": sampler_report, "stderr": sampler_stderr, "pass": valid}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    cases = []
    cases.append(run_case("A_root_only", "import time; time.sleep(0.7)"))
    cases.append(run_case("B_parent_child", "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(0.7)']); time.sleep(0.9); p.wait()"))
    cases.append(run_case("C_multi_thread", "import threading,time; ts=[threading.Thread(target=lambda: sum(i*i for i in range(2500000))) for _ in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]; time.sleep(0.3)"))
    cases.append(run_case("D_child_birth_death", "import subprocess,sys,time; [subprocess.run([sys.executable,'-c','import time; time.sleep(0.18)']) for _ in range(3)]; time.sleep(0.3)"))
    cases.append(run_case("E_root_exits_after_descendant", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(1.0)']); time.sleep(0.35)"))
    cases.append(run_case("F_short_task", "import time; time.sleep(0.25)", interval=0.05))
    cases.append(run_case("G_invalid_pid", "import time; time.sleep(0.25)", expect_sampler="ROOT_PID_UNAVAILABLE"))
    # Re-run G with an invalid root PID instead of a live workload.
    g = cases[-1]
    with tempfile.TemporaryDirectory(prefix="methunmix-telemetry-invalid-") as td:
        out = Path(td) / "invalid.jsonl"
        r = subprocess.run([sys.executable, str(TELEMETRY), "--pid", "9999999", "--output", str(out), "--stage", "SYNTHETIC_REGRESSION", "--task-id", "G_invalid_pid", "--max-seconds", "1"], text=True, capture_output=True)
        try:
            rep = json.loads(r.stdout)
        except Exception:
            rep = {"status": "INVALID_SAMPLER_JSON", "stdout": r.stdout, "stderr": r.stderr}
        g = {"name": "G_invalid_pid", "workload_return_code": 0, "sampler": rep, "stderr": r.stderr, "pass": rep.get("status") == "ROOT_PID_UNAVAILABLE" and r.returncode != 0}
        cases[-1] = g
    with tempfile.TemporaryDirectory(prefix="methunmix-telemetry-output-") as td:
        out_dir = Path(td) / "output-directory"
        out_dir.mkdir()
        r = subprocess.run([sys.executable, str(TELEMETRY), "--pid", str(os.getpid()), "--output", str(out_dir), "--stage", "SYNTHETIC_REGRESSION", "--task-id", "H_output_failure", "--max-seconds", "0.1"], text=True, capture_output=True)
        try:
            rep = json.loads(r.stdout)
        except Exception:
            rep = {"status": "INVALID_SAMPLER_JSON", "stdout": r.stdout, "stderr": r.stderr}
        cases.append({"name": "H_output_failure", "sampler": rep, "pass": rep.get("status") == "OUTPUT_WRITE_FAILURE" and r.returncode != 0})
    mod = module()
    fake = {"pid": 123, "process_start_time": 1}
    accounting_rows = [
        {"timestamp_monotonic": 0.0, "sample_seq": 0, "pid": 123, "process_start_time": 1, "cpu_time_total_sec": 1.0, "nlwp": 1, "direct_child_count": 1, "descendant_count": 1},
        {"timestamp_monotonic": 1.0, "sample_seq": 1, "pid": 123, "process_start_time": 1, "cpu_time_total_sec": 2.0, "nlwp": 1, "direct_child_count": 1, "descendant_count": 1},
        {"timestamp_monotonic": 1.0, "sample_seq": 1, "pid": 456, "process_start_time": 2, "cpu_time_total_sec": 0.5, "nlwp": 2, "direct_child_count": 0, "descendant_count": 0},
    ]
    summary = mod.summarize_runtime_rows(accounting_rows)
    pid_reuse = {"name": "I_pid_reuse_identity_cpu_delta", "pass": mod.process_identity(fake) == (123, 1) and mod.process_identity({"pid": 123, "process_start_time": 2}) != (123, 1) and summary["process_tree_cpu_seconds"] == 1.0 and summary["process_tree_cpu_equivalents"] == 1.0, "status": "PASS_STATIC_IDENTITY_AND_BASELINE_EXCLUDED_DELTA_TEST", "summary": summary}
    cases.append(pid_reuse)
    two_worker_rows = [
        {"timestamp_monotonic": 0.0, "sample_seq": 0, "pid": 700, "process_start_time": 10, "cpu_time_total_sec": 0.0, "nlwp": 1, "direct_child_count": 0, "descendant_count": 0},
        {"timestamp_monotonic": 0.0, "sample_seq": 0, "pid": 701, "process_start_time": 11, "cpu_time_total_sec": 0.0, "nlwp": 1, "direct_child_count": 0, "descendant_count": 0},
        {"timestamp_monotonic": 1.0, "sample_seq": 1, "pid": 700, "process_start_time": 10, "cpu_time_total_sec": 1.0, "nlwp": 1, "direct_child_count": 0, "descendant_count": 0},
        {"timestamp_monotonic": 1.0, "sample_seq": 1, "pid": 701, "process_start_time": 11, "cpu_time_total_sec": 1.0, "nlwp": 1, "direct_child_count": 0, "descendant_count": 0},
    ]
    two_worker_summary = mod.summarize_runtime_rows(two_worker_rows)
    cases.append({"name": "K_two_concurrent_cpu_worker_equivalents", "pass": two_worker_summary["process_tree_cpu_seconds"] == 2.0 and two_worker_summary["process_tree_cpu_equivalents"] == 2.0, "status": "PASS_STATIC_TWO_WORKER_ACCOUNTING_TEST", "summary": two_worker_summary})
    with tempfile.TemporaryDirectory(prefix="methunmix telemetry 空格-") as td:
        cases.append(run_case("J_unicode_space_cwd", "import time; time.sleep(0.6)", cwd=Path(td)))
    result = {"schema": "methunmix-celfeer-runtime-sampler-regression-v1", "scientific_execution": False, "tests": cases, "status": "PASS" if all(c.get("pass") for c in cases) else "FAIL", "test_count": len(cases), "timestamp_epoch": time.time()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
