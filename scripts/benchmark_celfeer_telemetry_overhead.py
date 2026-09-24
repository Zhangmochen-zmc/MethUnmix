#!/usr/bin/env python3
"""Longer, non-scientific telemetry overhead benchmark."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

TELEMETRY = Path(__file__).resolve().parent / "celfeer_telemetry_v2.py"
CODE_TEMPLATE = "s=0\nfor i in range({iterations}): s=(s+i*i)&0xffffffff\nimport time; time.sleep(0.8)\nprint(s)"


def parse_time_v(text: str) -> dict:
    def num(label: str) -> float | None:
        m = re.search(rf"^\s*{re.escape(label)}:\s*([0-9.]+)", text, re.M)
        return float(m.group(1)) if m else None
    rss = re.search(r"^\s*Maximum resident set size \(kbytes\):\s*(\d+)", text, re.M)
    return {"user_seconds": num("User time (seconds)"), "system_seconds": num("System time (seconds)"), "max_rss_kib": int(rss.group(1)) if rss else None}


def baseline(code: str) -> dict:
    t = time.perf_counter()
    p = subprocess.run(["/usr/bin/time", "-v", sys.executable, "-c", code], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    rec = parse_time_v(p.stderr)
    rec.update({"return_code": p.returncode, "wall_seconds": time.perf_counter() - t})
    return rec


def monitored(code: str, interval: float, output: Path) -> dict:
    target = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target_start = time.perf_counter()
    sampler = subprocess.Popen(["/usr/bin/time", "-v", sys.executable, str(TELEMETRY), "--pid", str(target.pid), "--output", str(output), "--interval", str(interval), "--stage", "SYNTHETIC_OVERHEAD", "--task-id", f"interval-{interval}", "--stop-when-root-exits", "--max-seconds", "30"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    target_rc = target.wait(timeout=30)
    target_wall = time.perf_counter() - target_start
    stdout, stderr = sampler.communicate(timeout=30)
    try:
        sampler_result = json.loads(stdout)
    except Exception:
        sampler_result = {"status": "INVALID_JSON", "stdout": stdout}
    rows = [json.loads(line) for line in output.read_text().splitlines() if line.strip()] if output.exists() else []
    totals = [row["cpu_time_total_sec"] for row in rows if isinstance(row.get("cpu_time_total_sec"), (int, float))]
    return {"target_return_code": target_rc, "target_wall_seconds": target_wall, "target_cpu_total_seconds_observed": max(totals, default=None), "rows": len(rows), "sampler": sampler_result, "sampler_time_v": parse_time_v(stderr), "sampler_return_code": sampler.returncode}


def median(values: list[float]) -> float:
    values = sorted(values)
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--iterations", type=int, default=100_000_000)
    args = ap.parse_args()
    code = CODE_TEMPLATE.format(iterations=args.iterations)
    baseline_runs = [baseline(code) for _ in range(args.repeats)]
    interval_runs = {}
    for interval in (0.10, 0.25):
        runs = []
        for idx in range(args.repeats):
            out = args.output.parent / f"raw_interval_{interval}_{idx}.jsonl"
            runs.append(monitored(code, interval, out))
        interval_runs[str(interval)] = runs
    base_wall = median([x["wall_seconds"] for x in baseline_runs])
    base_cpu = median([(x.get("user_seconds") or 0) + (x.get("system_seconds") or 0) for x in baseline_runs])
    summaries = {}
    for interval, runs in interval_runs.items():
        wall = median([x["target_wall_seconds"] for x in runs])
        cpu = median([x["target_cpu_total_seconds_observed"] or 0 for x in runs])
        sampler_cpu = median([(x["sampler_time_v"].get("user_seconds") or 0) + (x["sampler_time_v"].get("system_seconds") or 0) for x in runs])
        sampler_rss = max((x["sampler_time_v"].get("max_rss_kib") or 0 for x in runs), default=0)
        summaries[interval] = {"median_target_wall_seconds": wall, "target_wall_overhead_percent": (wall / base_wall - 1) * 100, "median_target_cpu_seconds": cpu, "target_cpu_difference_percent": (cpu / base_cpu - 1) * 100 if base_cpu else None, "median_sampler_cpu_seconds": sampler_cpu, "peak_sampler_rss_kib": sampler_rss, "record_count_median": median([float(x["rows"]) for x in runs]), "runs": runs}
    result = {"schema": "methunmix-celfeer-telemetry-overhead-benchmark-v1", "scientific_execution": False, "workload": "single CPU-bound synthetic process with 0.8 s tail sleep", "iterations": args.iterations, "repeats": args.repeats, "baseline": {"median_wall_seconds": base_wall, "median_cpu_seconds": base_cpu, "runs": baseline_runs}, "intervals": summaries, "selection_rule": "prefer 0.10 s only if median target wall overhead <=5% and sampler resource use is not materially perturbing; otherwise evaluate 0.25 s", "status": "OBSERVED_NOT_RELEASE_GATE", "timestamp_epoch": time.time()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
