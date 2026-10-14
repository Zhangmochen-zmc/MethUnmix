"""Bounded execution lifecycle primitives.

This module intentionally does not decide scientific validity and does not
turn Nextflow's ``-resume`` switch into a recovery claim.  It provides small,
content-addressed building blocks which callers must bind to all identities
that can affect a result.
"""

from __future__ import annotations

import hashlib
import json
import os
import errno
import shutil
import resource
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import DeMethFlowError
from .util import atomic_write_json, sha256_file


RESOURCE_SCHEMA = "methunmix-resource-observation-v1"
CACHE_SCHEMA = "methunmix-step-cache-v1"
LEASE_SCHEMA = "methunmix-run-lease-v1"
SUPERVISOR_SCHEMA = "methunmix-task-supervisor-v1"
RECOVERY_SCHEMA = "methunmix-recovery-plan-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_existing_parent(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def inspect_resources(
    paths: Iterable[tuple[str, Path]], *, requested_cpus: int = 1,
    requested_concurrency: int = 1, known_required_bytes: int | None = None,
) -> dict[str, object]:
    """Capture a conservative preflight observation without reserving space.

    ``os.access`` and ``statvfs`` are observations, not a promise that a
    downstream container or a concurrent task can write successfully.
    """
    entries: list[dict[str, object]] = []
    for label, raw in paths:
        parent = _safe_existing_parent(raw)
        stat = os.statvfs(parent)
        free = stat.f_bavail * stat.f_frsize
        entries.append({
            "label": label,
            "requested_path": str(raw.expanduser()),
            "observed_parent": str(parent),
            "observed_parent_is_symlink": parent.is_symlink(),
            "access_writable_check": os.access(parent, os.W_OK | os.X_OK),
            "free_bytes": free,
            "total_bytes": stat.f_blocks * stat.f_frsize,
            "known_required_bytes": known_required_bytes,
            "space_status": (
                "SUFFICIENT_BY_CURRENT_OBSERVATION" if known_required_bytes is not None and free >= known_required_bytes else
                ("INSUFFICIENT_BY_CURRENT_OBSERVATION" if known_required_bytes is not None else "UNKNOWN_NO_SIZE_MODEL")
            ),
        })
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return {
        "schema": RESOURCE_SCHEMA,
        "observed_at": _now(),
        "requested_cpus": requested_cpus,
        "requested_concurrency": requested_concurrency,
        "nofile_limit": {"soft": soft, "hard": hard},
        "filesystems": entries,
        "scope_limitations": [
            "No disk space or inode capacity is reserved.",
            "Write access is checked from the current process only; container mount policy is not proven.",
            "No process-tree memory or CPU capacity claim is made by this preflight.",
        ],
    }


class RunLease:
    """A short-lived, schema-bound managed lease for local maintenance guards."""

    def __init__(self, root: Path, *, modules: list[dict[str, str]], identity: dict[str, object]):
        self.root = root.expanduser().resolve()
        self.modules = modules
        self.identity = identity
        self.path = self.root / f"run-{uuid.uuid4().hex}.json"

    def open(self) -> dict[str, object]:
        if self.root.is_symlink():
            raise DeMethFlowError(f"Refusing symlinked active-use root: {self.root}")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "schema": LEASE_SCHEMA,
            "state": "ACTIVE",
            "created_at": _now(),
            "modules": self.modules,
            "run_identity": self.identity,
            "scope": "managed local lifecycle record only; does not establish host-wide idleness",
        }
        atomic_write_json(self.path, payload)
        return payload

    def close(self, outcome: str) -> dict[str, object]:
        if not self.path.is_file() or self.path.is_symlink():
            return {"status": "LEASE_NOT_WRITTEN_OR_UNSAFE", "path": str(self.path)}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        value.update(state="IDLE_CONFIRMED", closed_at=_now(), outcome=outcome)
        atomic_write_json(self.path, value)
        return value

    def mark_unknown(self, outcome: str, *, reason: str) -> dict[str, object]:
        """Preserve a lease when launch ownership could not be established.

        This is intentionally not a cleanup operation.  A launcher interrupted
        during process creation cannot safely assert that no process exists,
        so downstream maintenance must reconcile the run rather than treating
        its assets as confirmed idle.
        """
        if not self.path.is_file() or self.path.is_symlink():
            return {"status": "LEASE_NOT_WRITTEN_OR_UNSAFE", "path": str(self.path)}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        value.update(
            state="UNKNOWN_REQUIRES_RECONCILIATION", observed_at=_now(), outcome=outcome,
            reason=reason,
            scope="managed local lifecycle record only; launch ownership was not confirmed",
        )
        atomic_write_json(self.path, value)
        return value


def make_step_key(*, step: str, identities: dict[str, object], parameters: dict[str, object]) -> dict[str, object]:
    """Return a stable cache key; callers supply all result-affecting identity."""
    if not step or not isinstance(identities, dict) or not isinstance(parameters, dict):
        raise DeMethFlowError("step cache key requires non-empty step plus identity and parameter objects")
    semantic = {"schema": CACHE_SCHEMA, "step": step, "identities": identities, "parameters": parameters}
    return {"semantic": semantic, "sha256": hashlib.sha256(_canonical(semantic)).hexdigest()}


class StepCache:
    """Atomic local cache for a small deterministic file artifact.

    It never deletes an unknown lock, never reuses partial output, and never
    overwrites an existing complete entry with a different receipt.
    """

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        if self.root.is_symlink():
            raise DeMethFlowError(f"Refusing symlinked cache root: {self.root}")

    def _entry(self, key: str) -> Path:
        if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise DeMethFlowError("invalid step-cache key")
        return self.root / key

    def lookup(self, key: str) -> dict[str, object]:
        entry = self._entry(key)
        receipt = entry / "receipt.json"
        payload = entry / "payload"
        if not receipt.is_file() or receipt.is_symlink() or not payload.is_file() or payload.is_symlink():
            return {"status": "MISS", "key": key, "reason": "complete receipt and regular payload are both required"}
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"status": "CORRUPT", "key": key, "reason": "receipt unreadable"}
        if value.get("schema") != CACHE_SCHEMA or value.get("status") != "COMPLETE" or value.get("key") != key:
            return {"status": "CORRUPT", "key": key, "reason": "receipt identity/status mismatch"}
        observed = sha256_file(payload)
        if observed != value.get("payload_sha256"):
            return {"status": "CORRUPT", "key": key, "reason": "payload digest mismatch"}
        return {"status": "HIT", "key": key, "payload": str(payload), "payload_sha256": observed, "receipt": value}

    def publish(self, key: str, source: Path) -> dict[str, object]:
        """Copy a verified regular source file atomically into an empty key."""
        if not source.is_file() or source.is_symlink():
            raise DeMethFlowError(f"cache source must be a regular non-symlink file: {source}")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        entry = self._entry(key)
        existing = self.lookup(key)
        if existing["status"] == "HIT":
            return {**existing, "published": False}
        if entry.exists():
            raise DeMethFlowError(f"cache entry is incomplete/corrupt or locked; refusing overwrite: {entry}")
        partial = self.root / f".{key}.partial-{uuid.uuid4().hex}"
        partial.mkdir(mode=0o700)
        destination = partial / "payload"
        with source.open("rb") as inp, destination.open("xb") as out:
            while chunk := inp.read(1024 * 1024):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        digest = sha256_file(destination)
        atomic_write_json(partial / "receipt.json", {
            "schema": CACHE_SCHEMA, "status": "COMPLETE", "key": key,
            "payload_sha256": digest, "payload_bytes": destination.stat().st_size,
            "published_at": _now(),
        })
        try:
            os.replace(partial, entry)
        except OSError as exc:
            # A concurrent publisher may have completed the exact key between
            # our initial lookup and this replacement.  Never overwrite it;
            # only remove this process's uniquely named incomplete directory.
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                shutil.rmtree(partial)
                raise DeMethFlowError(
                    f"concurrent cache publish completed or conflicted; retry lookup instead: {entry}"
                ) from exc
            raise
        return {"status": "PUBLISHED", "key": key, "payload": str(entry / "payload"), "payload_sha256": digest, "published": True}


def _group_members(pgid: int) -> list[int]:
    try:
        output = subprocess.run(["ps", "-eo", "pid=,pgid="], capture_output=True, text=True, check=False, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    members: list[int] = []
    for row in output.splitlines():
        fields = row.split()
        if len(fields) == 2 and fields[1] == str(pgid):
            try:
                members.append(int(fields[0]))
            except ValueError:
                pass
    return sorted(members)


def _direct_child_sample(pid: int) -> dict[str, int] | None:
    """Best-effort direct-child observation; intentionally not a tree total."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return None
    rss = None
    for row in status.splitlines():
        if row.startswith("VmRSS:"):
            try:
                rss = int(row.split()[1])
            except (IndexError, ValueError):
                pass
    try:
        ticks = int(stat[13]) + int(stat[14])
    except (IndexError, ValueError):
        ticks = None
    return {"rss_kib": rss, "cpu_ticks": ticks} if rss is not None or ticks is not None else None


def _attempt_path(receipt_path: Path) -> Path:
    return receipt_path.with_name(f"{receipt_path.name}.attempt.json")


def _run_hook(
    hook: Callable[[str, dict[str, object]], None] | None,
    stage: str,
    attempt: dict[str, object],
) -> None:
    """Deterministic test-only fault injection without changing production flow."""
    if hook is not None:
        hook(stage, dict(attempt))


def _write_attempt(path: Path, attempt: dict[str, object]) -> None:
    """Publish an attempt fact; callers decide whether a write failure permits launch."""
    atomic_write_json(path, attempt)


def _record_terminal(
    receipt_path: Path,
    attempt_path: Path,
    attempt: dict[str, object],
    receipt: dict[str, object],
    *,
    hook: Callable[[str, dict[str, object]], None] | None,
) -> dict[str, object]:
    """Try to publish the terminal receipt, retaining a failed-write fact."""
    try:
        _run_hook(hook, "BEFORE_RECEIPT", attempt)
        receipt["receipt_write_status"] = "PERSISTED"
        atomic_write_json(receipt_path, receipt)
        attempt.update(state="TERMINAL_RECEIPT_PERSISTED", terminal_receipt=str(receipt_path), finished_at=receipt["finished_at"])
        try:
            _write_attempt(attempt_path, attempt)
        except (OSError, KeyboardInterrupt):
            # A terminal receipt failure must not be hidden by a secondary
            # attempt-journal failure.
            pass
    except (OSError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt) and receipt.get("reason") == "COMPLETED":
            receipt["reason"] = "INTERRUPTED_AFTER_PROCESS_EXIT"
        receipt["receipt_write_status"] = "FAILED_OR_INTERRUPTED"
        receipt["receipt_write_error"] = f"{type(exc).__name__}: {exc}"
        attempt.update(
            state="TERMINAL_RECEIPT_WRITE_FAILED", terminal_receipt=str(receipt_path),
            receipt_write_error=receipt["receipt_write_error"], finished_at=_now(),
        )
        try:
            _write_attempt(attempt_path, attempt)
        except (OSError, KeyboardInterrupt):
            pass
    return receipt


def _cleanup_owned_group(
    process: subprocess.Popen[object],
    pgid: int,
    *,
    interrupt_grace_seconds: float,
    cleanup: list[str],
    hook: Callable[[str, dict[str, object]], None] | None,
    attempt: dict[str, object],
) -> None:
    """Clean only a confirmed dedicated group, while recording nested interrupts."""
    try:
        _run_hook(hook, "BEFORE_CLEANUP", attempt)
    except KeyboardInterrupt:
        cleanup.append("INTERRUPTED_DURING_CLEANUP_BEFORE_SIGNAL")
    try:
        os.killpg(pgid, signal.SIGTERM)
        cleanup.append("SIGTERM_PROCESS_GROUP")
    except ProcessLookupError:
        cleanup.append("PROCESS_GROUP_ALREADY_EXITED")
    while True:
        try:
            process.wait(timeout=interrupt_grace_seconds)
            break
        except KeyboardInterrupt:
            cleanup.append("INTERRUPTED_DURING_CLEANUP_WAIT")
            # A second signal does not broaden cleanup scope. Continue the
            # bounded wait/kill sequence for this already-owned group only.
            continue
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
                cleanup.append("SIGKILL_PROCESS_GROUP")
            except ProcessLookupError:
                cleanup.append("PROCESS_GROUP_ALREADY_EXITED_BEFORE_SIGKILL")
            while True:
                try:
                    process.wait()
                    return
                except KeyboardInterrupt:
                    cleanup.append("INTERRUPTED_DURING_CLEANUP_FINAL_WAIT")


def supervise_command(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout_seconds: float | None,
    receipt_path: Path, interrupt_grace_seconds: float = 5.0,
    attempt_identity: dict[str, object] | None = None,
    task_scope: dict[str, object] | None = None,
    _test_hook: Callable[[str, dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Run one command in an owned session with conservative interruption facts.

    A pre-launch attempt journal is written before `Popen`. If an interrupt
    arrives during creation and Python cannot expose a child handle, the
    receipt says `UNKNOWN_CREATION_INTERRUPTED`; it never claims no child or
    performs an unsafe name/PID scan. Once `Popen` returned, cleanup targets
    only its known dedicated process group.
    """
    if not command:
        raise DeMethFlowError("supervisor command cannot be empty")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise DeMethFlowError("supervisor timeout must be positive")
    receipt_path = receipt_path.expanduser().resolve()
    attempt_path = _attempt_path(receipt_path)
    started = time.monotonic()
    attempt: dict[str, object] = {
        "schema": SUPERVISOR_SCHEMA, "attempt_id": uuid.uuid4().hex,
        "state": "PREPARING_PROCESS_CREATION", "created_at": _now(),
        "command": command, "cwd": str(cwd), "timeout_seconds": timeout_seconds,
        "expected_receipt": str(receipt_path), "software_and_plan_identity": attempt_identity,
        "task_scope": task_scope, "process_start_state": "NOT_YET_ATTEMPTED",
        "pid": None, "pgid": None,
        "scope": "pre-launch journal; no process ownership is claimed until Popen returns",
    }
    try:
        _write_attempt(attempt_path, attempt)
    except (OSError, KeyboardInterrupt) as exc:
        raise DeMethFlowError(f"TASK_ATTEMPT_RECORD_FAILED: refusing to launch without {attempt_path}")

    process: subprocess.Popen[object] | None = None
    cleanup: list[str] = []
    samples: list[dict[str, int]] = []
    reason = "COMPLETED"
    launch_error: str | None = None
    try:
        attempt.update(state="ENTERING_PROCESS_CREATION", process_start_state="CREATION_IN_PROGRESS")
        _write_attempt(attempt_path, attempt)
        _run_hook(_test_hook, "BEFORE_POPEN", attempt)
        process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
    except KeyboardInterrupt:
        reason = "INTERRUPTED"
        launch_error = "KeyboardInterrupt while process creation was not confirmable"
    except OSError as exc:
        reason = "LAUNCH_FAILED"
        launch_error = f"{type(exc).__name__}: {exc}"

    if process is None:
        attempt.update(
            state="TERMINAL_PROCESS_CREATION_UNCONFIRMED",
            process_start_state="UNKNOWN_CREATION_INTERRUPTED" if reason == "INTERRUPTED" else "UNKNOWN_CREATION_FAILED",
            terminal_reason=reason, launch_error=launch_error, finished_at=_now(),
        )
        receipt: dict[str, object] = {
            "schema": SUPERVISOR_SCHEMA, "attempt_id": attempt["attempt_id"], "command": command, "cwd": str(cwd),
            "pid": None, "pgid": None, "timeout_seconds": timeout_seconds, "reason": reason,
            "process_start_state": attempt["process_start_state"], "exit_code": None,
            "elapsed_seconds": round(time.monotonic() - started, 6), "cleanup_actions": cleanup,
            "remaining_group_members": None, "orphan_free": None, "finished_at": _now(),
            "launch_error": launch_error, "unknown_facts": ["whether Popen created a child before interruption/failure"],
            "attempt_record": str(attempt_path), "scope": "no group was signalled because ownership was not confirmed",
        }
        return _record_terminal(receipt_path, attempt_path, attempt, receipt, hook=_test_hook)

    pgid = process.pid
    attempt.update(
        state="PROCESS_STARTED", process_start_state="CONFIRMED_STARTED", pid=process.pid, pgid=pgid,
        process_group_ownership="DEDICATED_SESSION_CREATED_BY_THIS_ATTEMPT",
    )
    try:
        _write_attempt(attempt_path, attempt)
    except KeyboardInterrupt:
        reason = "INTERRUPTED"
        launch_error = "KeyboardInterrupt while recording a confirmed process handle"
    except OSError as exc:
        reason = "SUPERVISION_RECORD_FAILED"
        launch_error = f"{type(exc).__name__}: {exc}"
    try:
        if reason == "COMPLETED":
            _run_hook(_test_hook, "AFTER_POPEN", attempt)
            deadline = None if timeout_seconds is None else started + timeout_seconds
            while process.poll() is None:
                sample = _direct_child_sample(process.pid)
                if sample is not None:
                    samples.append(sample)
                if deadline is not None and time.monotonic() >= deadline:
                    reason = "TIMEOUT"
                    break
                time.sleep(0.05)
    except KeyboardInterrupt:
        reason = "INTERRUPTED"

    if reason != "COMPLETED":
        _cleanup_owned_group(
            process, pgid, interrupt_grace_seconds=interrupt_grace_seconds, cleanup=cleanup,
            hook=_test_hook, attempt=attempt,
        )
    remaining = _group_members(pgid)
    receipt = {
        "schema": SUPERVISOR_SCHEMA, "attempt_id": attempt["attempt_id"], "command": command, "cwd": str(cwd),
        "pid": process.pid, "pgid": pgid, "timeout_seconds": timeout_seconds, "reason": reason,
        "process_start_state": "CONFIRMED_STARTED", "exit_code": process.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 6), "cleanup_actions": cleanup,
        "remaining_group_members": remaining, "orphan_free": not remaining, "finished_at": _now(),
        "attempt_record": str(attempt_path), "launch_error": launch_error,
        "resource_observation": {
            "sampling_scope": "direct supervisor child only; not a process-tree total",
            "sample_count": len(samples),
            "peak_direct_child_rss_kib": max((item.get("rss_kib") or 0 for item in samples), default=None),
            "last_direct_child_cpu_ticks": samples[-1].get("cpu_ticks") if samples else None,
        },
        "scope": "only the dedicated process group confirmed as created by this attempt was signalled",
    }
    attempt.update(state="TERMINAL_PROCESS_CONFIRMED", terminal_reason=reason, exit_code=process.returncode, finished_at=receipt["finished_at"])
    return _record_terminal(receipt_path, attempt_path, attempt, receipt, hook=_test_hook)


def prepare_recovery_plan(manifest: Path, *, new_outdir: Path) -> dict[str, object]:
    """Prepare an identity-aware recovery decision; never invokes a workflow."""
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeMethFlowError(f"cannot read recovery manifest: {exc}") from exc
    required = {name: record.get(name) for name in ("software_identity", "reference_digest", "input_identity", "resolved_execution_parameters")}
    missing = [name for name, value in required.items() if value in (None, {}, "")]
    return {
        "schema": RECOVERY_SCHEMA, "mode": "PREPARE_ONLY_NO_EXECUTION", "source_manifest": str(manifest.resolve()),
        "new_outdir_required": str(new_outdir.expanduser().resolve()), "identity_fields": required,
        "decision": "FRESH_RUN_REQUIRED" if missing else "IDENTITY_REVIEW_REQUIRED_BEFORE_STAGE_REUSE",
        "missing_identity_fields": missing,
        "explicitly_not_done": ["does not pass -resume", "does not reuse partial outputs", "does not mutate prior run"],
        "recovery_granularity": "only a future stage receipt with matching full identity and complete output digest may be reused",
    }
