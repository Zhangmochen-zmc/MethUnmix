from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import demethflow_core.execution_lifecycle as lifecycle
from demethflow_core.errors import DeMethFlowError
from demethflow_core.execution_lifecycle import (
    RunLease,
    StepCache,
    inspect_resources,
    make_step_key,
    prepare_recovery_plan,
    supervise_command,
)


class ExecutionLifecycleTests(unittest.TestCase):
    def test_resource_observation_is_explicitly_non_reserving(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-resource-") as temporary:
            root = Path(temporary)
            observed = inspect_resources(
                [("out", root / "out"), ("cache", root / "cache")],
                requested_cpus=2, requested_concurrency=1, known_required_bytes=1,
            )
            self.assertEqual(observed["schema"], "methunmix-resource-observation-v1")
            self.assertEqual(len(observed["filesystems"]), 2)
            self.assertIn("No disk space", observed["scope_limitations"][0])

    def test_cache_requires_complete_receipt_and_detects_corruption(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-cache-") as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("deterministic", encoding="utf-8")
            key = make_step_key(step="fixture", identities={"input": "a"}, parameters={"seed": 1})["sha256"]
            cache = StepCache(root / "cache")
            self.assertEqual(cache.lookup(key)["status"], "MISS")
            self.assertEqual(cache.publish(key, source)["status"], "PUBLISHED")
            self.assertEqual(cache.lookup(key)["status"], "HIT")
            (root / "cache" / key / "payload").write_text("corrupted", encoding="utf-8")
            self.assertEqual(cache.lookup(key)["status"], "CORRUPT")

    def test_cache_refuses_incomplete_or_conflicting_entry(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-cache-conflict-") as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("x", encoding="utf-8")
            key = make_step_key(step="fixture", identities={"input": "a"}, parameters={})["sha256"]
            conflict = root / "cache" / key
            conflict.mkdir(parents=True)
            with self.assertRaisesRegex(DeMethFlowError, "refusing overwrite"):
                StepCache(root / "cache").publish(key, source)

    def test_concurrent_cache_publish_has_one_winner_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-cache-race-") as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("x" * 200000, encoding="utf-8")
            key = make_step_key(step="fixture", identities={"input": "a"}, parameters={})["sha256"]
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            def publish() -> None:
                barrier.wait()
                try:
                    outcomes.append(str(StepCache(root / "cache").publish(key, source)["status"]))
                except DeMethFlowError:
                    outcomes.append("REFUSED")
            workers = [threading.Thread(target=publish), threading.Thread(target=publish)]
            [worker.start() for worker in workers]
            [worker.join() for worker in workers]
            self.assertEqual(outcomes.count("PUBLISHED"), 1)
            self.assertEqual(StepCache(root / "cache").lookup(key)["status"], "HIT")

    def test_supervisor_finishes_and_cleans_only_its_group_on_timeout(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-supervisor-") as temporary:
            root = Path(temporary)
            normal = supervise_command(
                [sys.executable, "-c", "print('ok')"], cwd=root, env=os.environ.copy(),
                timeout_seconds=5, receipt_path=root / "normal.json",
            )
            self.assertEqual(normal["reason"], "COMPLETED")
            self.assertTrue(normal["orphan_free"])
            timed = supervise_command(
                [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"],
                cwd=root, env=os.environ.copy(), timeout_seconds=0.2, receipt_path=root / "timeout.json",
                interrupt_grace_seconds=0.2,
            )
            self.assertEqual(timed["reason"], "TIMEOUT")
            self.assertTrue(timed["orphan_free"])
            self.assertIn("SIGTERM_PROCESS_GROUP", timed["cleanup_actions"])

    def test_supervisor_records_an_interruption_and_cleans_group(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-interrupt-") as temporary:
            root = Path(temporary)
            helper = root / "helper.py"
            receipt = root / "interrupt.json"
            ready = root / "ready"
            helper.write_text(
                "from demethflow_core.execution_lifecycle import supervise_command\n"
                "import os,sys\n"
                "from pathlib import Path\n"
                "Path(sys.argv[2]).write_text('ready')\n"
                "supervise_command([sys.executable,'-c','import time; time.sleep(30)'], cwd=Path('.'), env=os.environ.copy(), timeout_seconds=20, receipt_path=Path(sys.argv[1]))\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            process = __import__("subprocess").Popen([sys.executable, str(helper), str(receipt), str(ready)], cwd=root, env=environment)
            for _ in range(50):
                if ready.is_file():
                    break
                time.sleep(0.02)
            self.assertTrue(ready.is_file(), "helper did not enter the supervisor in time")
            # Synchronize on the durable pre-launch journal rather than a
            # timing sleep: a signal before that journal is atomically
            # committed must fail closed without launching anything.
            attempt = receipt.with_name(f"{receipt.name}.attempt.json")
            state = None
            for _ in range(250):
                if attempt.is_file():
                    try:
                        state = json.loads(attempt.read_text(encoding="utf-8")).get("state")
                    except json.JSONDecodeError:
                        state = None
                    if state == "PROCESS_STARTED":
                        break
                time.sleep(0.02)
            self.assertEqual(state, "PROCESS_STARTED", "supervisor did not confirm its owned child in time")
            os.kill(process.pid, __import__("signal").SIGINT)
            process.wait(timeout=5)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(payload["reason"], "INTERRUPTED")
            self.assertIn(payload["process_start_state"], {"CONFIRMED_STARTED", "UNKNOWN_CREATION_INTERRUPTED"})
            self.assertTrue((root / "interrupt.json.attempt.json").is_file())
            if payload["process_start_state"] == "CONFIRMED_STARTED":
                self.assertTrue(payload["orphan_free"])
            else:
                self.assertIsNone(payload["orphan_free"])

    def test_supervisor_pre_popen_interrupt_persists_unknown_attempt_without_launching(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-pre-popen-") as temporary:
            root = Path(temporary); receipt_path = root / "receipt.json"; launched = root / "launched"
            def hook(stage: str, _attempt: dict[str, object]) -> None:
                if stage == "BEFORE_POPEN":
                    raise KeyboardInterrupt()
            receipt = supervise_command(
                [sys.executable, "-c", f"from pathlib import Path; Path({str(launched)!r}).write_text('ran')"],
                cwd=root, env=os.environ.copy(), timeout_seconds=5, receipt_path=receipt_path,
                attempt_identity={"software": "fixture", "plan": "fixture"}, task_scope={"case": "pre-popen"},
                _test_hook=hook,
            )
            self.assertEqual(receipt["reason"], "INTERRUPTED")
            self.assertEqual(receipt["process_start_state"], "UNKNOWN_CREATION_INTERRUPTED")
            self.assertIsNone(receipt["pid"]); self.assertIsNone(receipt["orphan_free"])
            self.assertFalse(launched.exists())
            attempt = json.loads((root / "receipt.json.attempt.json").read_text(encoding="utf-8"))
            self.assertEqual(attempt["software_and_plan_identity"], {"software": "fixture", "plan": "fixture"})
            self.assertEqual(attempt["state"], "TERMINAL_RECEIPT_PERSISTED")

    def test_supervisor_does_not_claim_cleanup_when_creation_started_but_handle_was_lost(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-lost-handle-") as temporary:
            root = Path(temporary); receipt_path = root / "receipt.json"; created: list[object] = []
            real_popen = lifecycle.subprocess.Popen
            def create_then_interrupt(*args: object, **kwargs: object) -> object:
                child = real_popen(*args, **kwargs)
                created.append(child)
                raise KeyboardInterrupt()
            with patch("demethflow_core.execution_lifecycle.subprocess.Popen", side_effect=create_then_interrupt):
                receipt = supervise_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"], cwd=root, env=os.environ.copy(),
                    timeout_seconds=5, receipt_path=receipt_path,
                )
            self.assertEqual(receipt["process_start_state"], "UNKNOWN_CREATION_INTERRUPTED")
            self.assertEqual(receipt["cleanup_actions"], [])
            self.assertIsNone(receipt["orphan_free"])
            # This test owns the deliberately hidden child; the supervisor
            # correctly has no safe PID/PGID basis to do so itself.
            child = created[0]
            os.killpg(child.pid, __import__("signal").SIGTERM)
            child.wait(timeout=5)

    def test_supervisor_handles_known_group_interrupt_cleanup_and_receipt_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-races-") as temporary:
            root = Path(temporary)
            def after_popen(stage: str, _attempt: dict[str, object]) -> None:
                if stage == "AFTER_POPEN":
                    raise KeyboardInterrupt()
            known = supervise_command(
                [sys.executable, "-c", "import time; time.sleep(30)"], cwd=root, env=os.environ.copy(),
                timeout_seconds=5, interrupt_grace_seconds=0.1, receipt_path=root / "known.json", _test_hook=after_popen,
            )
            self.assertEqual(known["reason"], "INTERRUPTED")
            self.assertEqual(known["process_start_state"], "CONFIRMED_STARTED")
            self.assertTrue(known["orphan_free"])
            self.assertIn("SIGTERM_PROCESS_GROUP", known["cleanup_actions"])

            def cleanup_interrupt(stage: str, _attempt: dict[str, object]) -> None:
                if stage == "BEFORE_CLEANUP":
                    raise KeyboardInterrupt()
            nested = supervise_command(
                [sys.executable, "-c", "import time; time.sleep(30)"], cwd=root, env=os.environ.copy(),
                timeout_seconds=0.01, interrupt_grace_seconds=0.1, receipt_path=root / "nested.json", _test_hook=cleanup_interrupt,
            )
            self.assertEqual(nested["reason"], "TIMEOUT")
            self.assertTrue(nested["orphan_free"])
            self.assertIn("INTERRUPTED_DURING_CLEANUP_BEFORE_SIGNAL", nested["cleanup_actions"])

            def before_receipt(stage: str, _attempt: dict[str, object]) -> None:
                if stage == "BEFORE_RECEIPT":
                    raise KeyboardInterrupt()
            terminal_interrupt = supervise_command(
                [sys.executable, "-c", "pass"], cwd=root, env=os.environ.copy(), timeout_seconds=5,
                receipt_path=root / "terminal-interrupt.json", _test_hook=before_receipt,
            )
            self.assertEqual(terminal_interrupt["reason"], "INTERRUPTED_AFTER_PROCESS_EXIT")
            self.assertEqual(terminal_interrupt["exit_code"], 0)
            self.assertEqual(terminal_interrupt["receipt_write_status"], "FAILED_OR_INTERRUPTED")
            self.assertTrue((root / "terminal-interrupt.json.attempt.json").is_file())

    def test_supervisor_receipt_write_failure_preserves_actual_exit_and_unknown_creation_is_not_cleaned(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-write-failure-") as temporary:
            root = Path(temporary); receipt_path = root / "receipt.json"; actual_write = lifecycle.atomic_write_json
            def selective_write(path: Path, payload: object) -> None:
                if Path(path) == receipt_path:
                    raise OSError("fixture receipt device failure")
                actual_write(path, payload)
            with patch("demethflow_core.execution_lifecycle.atomic_write_json", side_effect=selective_write):
                receipt = supervise_command(
                    [sys.executable, "-c", "pass"], cwd=root, env=os.environ.copy(), timeout_seconds=5,
                    receipt_path=receipt_path,
                )
            self.assertEqual(receipt["reason"], "COMPLETED")
            self.assertEqual(receipt["exit_code"], 0)
            self.assertEqual(receipt["receipt_write_status"], "FAILED_OR_INTERRUPTED")
            self.assertFalse(receipt_path.exists())
            attempt = json.loads((root / "receipt.json.attempt.json").read_text(encoding="utf-8"))
            self.assertEqual(attempt["state"], "TERMINAL_RECEIPT_WRITE_FAILED")

    def test_recovery_never_maps_a_partial_manifest_to_resume(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-recovery-") as temporary:
            root = Path(temporary)
            manifest = root / "run_manifest.json"
            manifest.write_text(json.dumps({"reference_digest": "r"}), encoding="utf-8")
            plan = prepare_recovery_plan(manifest, new_outdir=root / "new")
            self.assertEqual(plan["decision"], "FRESH_RUN_REQUIRED")
            self.assertIn("does not pass -resume", plan["explicitly_not_done"])

    def test_lease_is_closed_and_never_claims_host_wide_idleness(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-lease-") as temporary:
            lease = RunLease(Path(temporary) / "leases", modules=[], identity={"fixture": "x"})
            self.assertEqual(lease.open()["state"], "ACTIVE")
            closed = lease.close("TEST")
            self.assertEqual(closed["state"], "IDLE_CONFIRMED")
            self.assertIn("managed local", closed["scope"])

    def test_production_workflow_source_receipt_cache_reuses_only_exact_source_identity(self):
        from demethflow_core.runner import _cache_workflow_identity, _persistent_workflow_bridge
        with tempfile.TemporaryDirectory(prefix="methunmix-wp06-production-cache-") as temporary:
            root = Path(temporary)
            out = root / "out"
            out.mkdir()
            original = os.environ.get("METHUNMIX_WORKFLOW_BRIDGE_ROOT")
            os.environ["METHUNMIX_WORKFLOW_BRIDGE_ROOT"] = str(root / "bridges")
            try:
                bridge = _persistent_workflow_bridge(out)
                first = _cache_workflow_identity(root / "cache", outdir=out, bridge=bridge)
                second = _cache_workflow_identity(root / "cache", outdir=out, bridge=bridge)
            finally:
                if original is None:
                    os.environ.pop("METHUNMIX_WORKFLOW_BRIDGE_ROOT", None)
                else:
                    os.environ["METHUNMIX_WORKFLOW_BRIDGE_ROOT"] = original
            self.assertEqual(first["kind"], "workflow_source_receipt")
            self.assertEqual(second["lookup"]["status"], "HIT")


if __name__ == "__main__":
    unittest.main()
