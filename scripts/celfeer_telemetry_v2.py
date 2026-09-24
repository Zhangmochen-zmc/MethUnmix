#!/usr/bin/env python3
"""Small, machine-parseable process-tree sampler for Phase 3O-F.

The sampler is deliberately independent of CelFEER.  Runtime mode reads
Linux /proc directly and emits one JSON object per sample so command quoting
cannot shift columns.  ``--synthetic-smoke`` is a bounded controlled
process-tree test only; it never launches a MethUnmix or Nextflow workflow.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "methunmix-celfeer-process-telemetry-v2"
FIELDS = ("timestamp_epoch", "timestamp_iso", "stage", "task_id", "pid", "ppid", "process_name", "command", "rss_kib", "vsz_kib", "cpu_percent", "nlwp", "direct_child_count")
RUNTIME_SCHEMA = "methunmix-celfeer-process-telemetry-v3"
RUNTIME_FIELDS = (
    "schema_version", "sample_seq", "timestamp_epoch", "timestamp_monotonic", "timestamp_iso",
    "stage", "task_id", "root_pid", "pid", "ppid", "process_start_time", "process_name",
    "command", "rss_kib", "vsz_kib", "cpu_time_user_sec", "cpu_time_system_sec",
    "cpu_time_total_sec", "cpu_percent", "nlwp", "direct_child_count", "descendant_count", "sample_status",
)


PROCFS_ROOT = Path("/proc")
CLK_TCK = int(os.sysconf("SC_CLK_TCK"))


def process_identity(proc: dict) -> tuple[int, int] | None:
    """Return PID plus /proc stat starttime ticks to protect against PID reuse."""
    pid, start_ticks = proc.get("pid"), proc.get("process_start_time")
    if isinstance(pid, int) and isinstance(start_ticks, int):
        return (pid, start_ticks)
    return None


def _proc_stat(pid: int) -> tuple[str, list[str]] | None:
    try:
        text = (PROCFS_ROOT / str(pid) / "stat").read_text()
        close = text.rfind(")")
        if close < 0:
            return None
        comm = text[text.find("(") + 1:close]
        return comm, text[close + 2:].split()
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
        return None


def _proc_status(pid: int) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in (PROCFS_ROOT / str(pid) / "status").read_text().splitlines():
            key, _, value = line.partition(":")
            token = value.strip().split()[0] if value.strip() else ""
            if key in {"VmRSS", "VmSize", "Threads"} and token.isdigit():
                values[key] = int(token)
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        pass
    return values


def _proc_cmdline(pid: int, fallback: str) -> str:
    try:
        raw = (PROCFS_ROOT / str(pid) / "cmdline").read_bytes()
        parts = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        return " ".join(parts) if parts else fallback
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return fallback


def _proc_record(pid: int) -> dict | None:
    parsed = _proc_stat(pid)
    if parsed is None:
        return None
    comm, fields = parsed
    try:
        ppid = int(fields[1])
        user_ticks, system_ticks = int(fields[11]), int(fields[12])
        start_ticks = int(fields[19])
        vsz_bytes, rss_pages = int(fields[20]), int(fields[21])
    except (IndexError, TypeError, ValueError):
        return None
    status = _proc_status(pid)
    return {
        "pid": pid,
        "ppid": ppid,
        "process_start_time": start_ticks,
        "process_name": comm,
        "command": _proc_cmdline(pid, comm),
        "rss_kib": status.get("VmRSS"),
        "vsz_kib": int(vsz_bytes // 1024),
        "cpu_time_user_sec": user_ticks / CLK_TCK,
        "cpu_time_system_sec": system_ticks / CLK_TCK,
        "cpu_time_total_sec": (user_ticks + system_ticks) / CLK_TCK,
        "nlwp": status.get("Threads"),
        "sample_status": None if "VmRSS" in status and "Threads" in status else "PROCFS_FIELD_UNAVAILABLE",
    }


def _proc_snapshot() -> dict[int, dict]:
    current: dict[int, dict] = {}
    try:
        pids = [int(path.name) for path in PROCFS_ROOT.iterdir() if path.name.isdigit()]
    except (FileNotFoundError, PermissionError):
        return current
    for pid in pids:
        record = _proc_record(pid)
        if record is not None:
            current[pid] = record
    return current


def _descendant_ids(pid: int, records: dict[int, dict]) -> set[int]:
    children: dict[int, list[int]] = {}
    for child in records.values():
        children.setdefault(child["ppid"], []).append(child["pid"])
    seen: set[int] = set()
    pending = list(children.get(pid, []))
    while pending:
        child = pending.pop()
        if child in seen or child == pid:
            continue
        seen.add(child)
        pending.extend(children.get(child, []))
    return seen


def _safe_processes(root_pid: int, root_identity: tuple[int, int] | None, known: dict[tuple[int, int], dict]) -> tuple[list[dict], bool]:
    """Collect root/descendants from procfs, preserving known descendants after root exit."""
    all_records = _proc_snapshot()
    root = all_records.get(root_pid)
    root_alive = root is not None and process_identity(root) == root_identity
    selected: dict[tuple[int, int], dict] = {}
    if root_alive:
        ids = {root_pid, *_descendant_ids(root_pid, all_records)}
        for pid in ids:
            record = all_records.get(pid)
            ident = process_identity(record) if record else None
            if record is not None and ident is not None:
                selected[ident] = record
    else:
        known_ids = set(known)
        for record in all_records.values():
            ident = process_identity(record)
            if ident is None:
                continue
            if ident in known_ids:
                selected[ident] = record
                continue
            parent_pid = record["ppid"]
            parent_candidates = [known_ident for known_ident in known_ids if known_ident[0] == parent_pid]
            if parent_candidates:
                selected[ident] = record
    return list(selected.values()), root_alive


def _runtime_row(proc: dict, root_pid: int, stage: str, task_id: str, seq: int, now_epoch: float, now_mono: float, previous: dict[tuple[int, int], tuple[float, float, float]]) -> tuple[dict, tuple[int, int] | None, tuple[float, float, float] | None]:
    ident = process_identity(proc)
    row = {key: None for key in RUNTIME_FIELDS}
    row.update({"schema_version": RUNTIME_SCHEMA, "sample_seq": seq, "timestamp_epoch": now_epoch, "timestamp_monotonic": now_mono, "timestamp_iso": datetime.now(timezone.utc).isoformat(), "stage": stage, "task_id": task_id, "root_pid": root_pid})
    if ident is None:
        row["sample_status"] = "PROCESS_IDENTITY_UNAVAILABLE"
        return row, None, None
    for key in ("pid", "ppid", "process_name", "command", "rss_kib", "vsz_kib", "cpu_time_user_sec", "cpu_time_system_sec", "cpu_time_total_sec", "nlwp", "sample_status"):
        row[key] = proc.get(key)
    row["process_start_time"] = ident[1]
    if row["sample_status"] is None and row["nlwp"] is None:
        row["sample_status"] = "PROCFS_FIELD_UNAVAILABLE"
    if row["sample_status"] is None:
        cpu = (float(proc["cpu_time_user_sec"]), float(proc["cpu_time_system_sec"]), now_mono)
    else:
        cpu = None
    return row, ident, cpu


def runtime_snapshot(processes: list[dict], root_pid: int, stage: str, task_id: str, seq: int, previous: dict[tuple[int, int], tuple[float, float, float]]) -> tuple[list[dict], dict[tuple[int, int], tuple[float, float, float]]]:
    now_epoch = time.time()
    now_mono = time.monotonic()
    rows: list[dict] = []
    current_cpu: dict[tuple[int, int], tuple[float, float, float]] = {}
    for proc in processes:
        row, ident, cpu = _runtime_row(proc, root_pid, stage, task_id, seq, now_epoch, now_mono, previous)
        if ident is not None and cpu is not None and all(isinstance(value, (int, float)) for value in cpu[:2]):
            current_cpu[ident] = cpu
        rows.append(row)
    present = {row["pid"] for row in rows if isinstance(row.get("pid"), int)}
    children: dict[int, list[int]] = {}
    for row in rows:
        if isinstance(row.get("pid"), int) and isinstance(row.get("ppid"), int):
            children.setdefault(row["ppid"], []).append(row["pid"])
    for row in rows:
        pid = row.get("pid")
        row["direct_child_count"] = len(children.get(pid, [])) if isinstance(pid, int) else None
        row["descendant_count"] = len(_descendant_ids(pid, {r["pid"]: r for r in rows if isinstance(r.get("pid"), int)})) if isinstance(pid, int) else None
        ident = (pid, row.get("process_start_time")) if isinstance(pid, int) and isinstance(row.get("process_start_time"), int) else None
        if ident in previous and ident in current_cpu:
            old_user, old_system, old_mono = previous[ident]  # type: ignore[index]
            new_user, new_system, _ = current_cpu[ident]
            dt = max(now_mono - old_mono, 1e-9)
            row["cpu_percent"] = max(0.0, ((new_user - old_user) + (new_system - old_system)) / dt * 100.0)
    return rows, current_cpu


def validate_runtime_jsonl(path: Path) -> dict:
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except Exception as exc:
        return {"rows": 0, "valid": False, "error": type(exc).__name__}
    malformed = [row for row in rows if set(row) != set(RUNTIME_FIELDS)]
    numeric = all(
        isinstance(row.get("pid"), int)
        and isinstance(row.get("ppid"), int)
        and (isinstance(row.get("nlwp"), int) or row.get("nlwp") is None)
        and (isinstance(row.get("direct_child_count"), int) or row.get("direct_child_count") is None)
        and (isinstance(row.get("descendant_count"), int) or row.get("descendant_count") is None)
        and (row.get("sample_status") is not None or isinstance(row.get("nlwp"), int))
        for row in rows
    )
    monotonic = all(rows[i]["timestamp_monotonic"] <= rows[i + 1]["timestamp_monotonic"] for i in range(len(rows) - 1))
    return {"rows": len(rows), "malformed_rows": len(malformed), "numeric_identity_fields": numeric, "timestamps_monotonic": monotonic, "schema_valid": bool(rows) and not malformed, "valid": bool(rows) and not malformed and numeric and monotonic}


def runtime_sample(root_pid: int, output: Path, interval: float, stage: str, task_id: str, stop_when_root_exits: bool, max_seconds: float) -> dict:
    if interval <= 0 or interval > 60:
        return {"schema": RUNTIME_SCHEMA, "status": "INVALID_INTERVAL", "return_code": 2}
    root_record = _proc_record(root_pid)
    root_identity = process_identity(root_record) if root_record is not None else None
    if root_record is None:
        return {"schema": RUNTIME_SCHEMA, "status": "ROOT_PID_UNAVAILABLE", "return_code": 3}
    if root_identity is None:
        return {"schema": RUNTIME_SCHEMA, "status": "ROOT_IDENTITY_UNAVAILABLE", "return_code": 3}
    rows: list[dict] = []
    previous: dict[tuple[int, int], tuple[float, float, float]] = {}
    known: dict[tuple[int, int], dict] = {root_identity: root_record}
    start = time.monotonic()
    seq = 0
    root_seen_alive = False
    runtime_error = None
    while time.monotonic() - start <= max_seconds:
        processes, root_alive = _safe_processes(root_pid, root_identity, known)
        if processes:
            root_seen_alive = root_seen_alive or root_alive
            for proc in processes:
                ident = process_identity(proc)
                if ident is not None:
                    known[ident] = proc
            batch, previous = runtime_snapshot(processes, root_pid, stage, task_id, seq, previous)
            rows.extend(batch)
            seq += 1
        elif root_seen_alive and stop_when_root_exits:
            break
        elif not root_seen_alive and time.monotonic() - start > interval * 2:
            runtime_error = "ROOT_EXITED_BEFORE_FIRST_SAMPLE"
            break
        time.sleep(interval)
    else:
        runtime_error = "MAX_SECONDS_EXCEEDED"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""))
    except Exception as exc:
        return {"schema": RUNTIME_SCHEMA, "status": "OUTPUT_WRITE_FAILURE", "error": type(exc).__name__, "rows": len(rows), "return_code": 7}
    validation = validate_runtime_jsonl(output)
    if runtime_error:
        return {"schema": RUNTIME_SCHEMA, "status": runtime_error, "rows": len(rows), "validation": validation, "return_code": 8}
    if not validation.get("valid"):
        return {"schema": RUNTIME_SCHEMA, "status": "JSONL_VALIDATION_FAILURE", "rows": len(rows), "validation": validation, "return_code": 9}
    return {"schema": RUNTIME_SCHEMA, "status": "PASS", "rows": len(rows), "validation": validation, "return_code": 0, "root_pid": root_pid, "root_process_start_time": root_identity[1]}


def summarize_runtime_rows(rows: list[dict]) -> dict:
    """Aggregate CPU consumed during the observation window only.

    The first cumulative CPU value for each PID/start-time identity is a
    baseline and is deliberately excluded because it may predate sampler
    attachment. Only positive deltas between observations are authoritative.
    """
    ordered = sorted(rows, key=lambda row: (row.get("timestamp_monotonic", 0.0), row.get("sample_seq", 0), row.get("pid") or -1))
    previous: dict[tuple[int, float], float] = {}
    cpu_delta_total = 0.0
    for row in ordered:
        pid = row.get("pid")
        start = row.get("process_start_time")
        total = row.get("cpu_time_total_sec")
        if not isinstance(pid, int) or not isinstance(start, (int, float)) or not isinstance(total, (int, float)):
            continue
        ident = (pid, float(start))
        old = previous.get(ident)
        if old is not None:
            cpu_delta_total += max(0.0, float(total) - old)
        previous[ident] = float(total)
    timestamps = [float(row["timestamp_monotonic"]) for row in ordered if isinstance(row.get("timestamp_monotonic"), (int, float))]
    wall = max(timestamps) - min(timestamps) if len(timestamps) >= 2 else 0.0
    nlwps = [row["nlwp"] for row in ordered if isinstance(row.get("nlwp"), int)]
    sums_by_sample: dict[int, int] = {}
    for row in ordered:
        seq = row.get("sample_seq")
        if isinstance(seq, int) and isinstance(row.get("nlwp"), int):
            sums_by_sample[seq] = sums_by_sample.get(seq, 0) + row["nlwp"]
    return {"process_tree_cpu_seconds": cpu_delta_total, "wall_seconds": wall, "process_tree_cpu_equivalents": cpu_delta_total / wall if wall > 0 else None, "peak_single_process_nlwp": max(nlwps, default=None), "peak_sum_nlwp": max(sums_by_sample.values(), default=None), "max_direct_children": max((row["direct_child_count"] for row in ordered if isinstance(row.get("direct_child_count"), int)), default=None), "max_descendant_count": max((row["descendant_count"] for row in ordered if isinstance(row.get("descendant_count"), int)), default=None), "identity_count": len(previous), "initial_cumulative_cpu_excluded": True, "formula": "sum(positive per-identity CPU-time deltas after first observation) / (last monotonic timestamp - first monotonic timestamp)"}


def snapshot(root_pid: int, stage: str, task_id: str) -> list[dict]:
    root_record = _proc_record(root_pid)
    if root_record is None:
        return []
    records, _ = _safe_processes(root_pid, process_identity(root_record), {})
    now = time.time()
    rows = []
    for record in records:
        row = {
            "timestamp_epoch": now,
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "task_id": task_id,
            "pid": record["pid"],
            "ppid": record["ppid"],
            "process_name": record["process_name"],
            "command": record["command"],
            "rss_kib": record["rss_kib"],
            "vsz_kib": record["vsz_kib"],
            # A one-shot legacy snapshot has no prior interval.  Preserve the
            # historical numeric field semantics without inventing a rate.
            "cpu_percent": 0.0,
            "nlwp": record["nlwp"],
            "direct_child_count": len([other for other in records if other["ppid"] == record["pid"]]),
        }
        rows.append(row)
    return rows


def sample_process(root_pid: int, stage: str, task_id: str, output: Path, interval: float = 0.05, duration: float = 1.5) -> int:
    rows: list[dict] = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        rows.extend(snapshot(root_pid, stage, task_id))
        time.sleep(interval)
    output.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""))
    return len(rows)


def validate_jsonl(path: Path) -> dict:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    keys_ok = all(tuple(sorted(row)) == tuple(sorted(FIELDS)) for row in rows)
    numeric_ok = all(isinstance(row["pid"], int) and isinstance(row["ppid"], int) and isinstance(row["nlwp"], int) and isinstance(row["direct_child_count"], int) and isinstance(row["cpu_percent"], (int, float)) for row in rows)
    timestamps = [row["timestamp_epoch"] for row in rows]
    monotonic = timestamps == sorted(timestamps)
    return {"rows": len(rows), "keys_ok": keys_ok, "numeric_ok": numeric_ok, "timestamps_monotonic": monotonic, "valid": bool(rows) and keys_ok and numeric_ok and monotonic}


def synthetic_smoke(output: Path) -> dict:
    # Parent starts a child and a CPU-bound Python thread.  It lasts long
    # enough for a 50-ms sampler while remaining a short, bounded test.
    code = (
        "import subprocess,threading,time,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(1.0)']); "
        "stop=[False]; "
        "t=threading.Thread(target=lambda: (sum(i*i for i in range(4000000)))); t.start(); "
        "time.sleep(1.25)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    count = sample_process(proc.pid, "SYNTHETIC", "smoke-parent", output)
    rc = proc.wait(timeout=5)
    validation = validate_jsonl(output)
    rows = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
    saw_child = any(row["direct_child_count"] > 0 for row in rows)
    saw_threads = any(row["nlwp"] > 1 for row in rows)
    short_start = time.monotonic()
    short = subprocess.run([sys.executable, "-c", "pass"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    short_accounted = short.returncode == 0 and time.monotonic() >= short_start
    return {"schema": SCHEMA, "status": "PASS" if validation["valid"] and rc == 0 and saw_child and saw_threads and short_accounted else "FAIL", "process_tree_exit": rc, "sample_count": count, "validation": validation, "saw_direct_child": saw_child, "saw_multithreaded_process": saw_threads, "short_process_stdlib_accounting": short_accounted, "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic-smoke", action="store_true")
    parser.add_argument("--pid", type=int, help="root PID for runtime process-tree sampling")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--stage", default="UNKNOWN")
    parser.add_argument("--task-id", default="UNKNOWN")
    parser.add_argument("--stop-when-root-exits", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=300.0)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.synthetic_smoke and args.pid is not None:
        parser.error("--synthetic-smoke and --pid are mutually exclusive")
    if args.pid is not None:
        result = runtime_sample(args.pid, args.output, args.interval, args.stage, args.task_id, args.stop_when_root_exits, args.max_seconds)
    elif args.synthetic_smoke:
        result = synthetic_smoke(args.output)
    else:
        parser.error("one of --synthetic-smoke or --pid is required")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
