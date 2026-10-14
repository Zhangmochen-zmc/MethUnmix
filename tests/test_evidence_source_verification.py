from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from demethflow_core.cli import main
from demethflow_core.errors import DeMethFlowError
from demethflow_core.scientific_evidence import audit, import_record, initialize


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EvidenceSourceVerificationTests(unittest.TestCase):
    def _record(self, source_artifacts: object = None, *, evidence_id: str = "source-fixture") -> dict[str, object]:
        record: dict[str, object] = {
            "evidence_id": evidence_id,
            "schema_version": "methunmix-scientific-evidence-v1",
            "evidence_type": "TECHNICAL_SMOKE",
            "validation_object": {
                "reference_selector": "fixture.array@1.0.0",
                "reference_content_digest": "a" * 64,
                "tool": "Houseman", "adapter": "fixture", "profile": "cpu",
                "scenario": "fixture", "platform": "450k",
                "genome_build": "not_applicable", "analysis_contract": "array_450k_cpg",
            },
            # Legacy source remains factual display data only.  It must never
            # be recursively interpreted as an artifact or a command.
            "source": {"path": "legacy-missing.json", "sha256": "f" * 64, "command": "do-not-run"},
        }
        if source_artifacts is not None:
            record["source_artifacts"] = source_artifacts
        return record

    @staticmethod
    def _descriptor(artifact_id: str, locator: str, data: bytes) -> dict[str, object]:
        return {"artifact_id": artifact_id, "kind": "RUN_MANIFEST", "locator": locator,
                "sha256": sha256(data), "size_bytes": len(data)}

    def _registry(self, root: Path, record: dict[str, object]) -> Path:
        registry = root / "registry.json"
        initialize(registry)
        import_record(registry, record)
        return registry

    def test_default_structural_audit_does_not_claim_legacy_source_verified(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-evidence-source-") as temporary:
            root = Path(temporary)
            registry = self._registry(root, self._record())
            structural = audit(registry)
            self.assertEqual(structural["status"], "PASS_STRUCTURAL_NO_STATUS_PROMOTION")
            self.assertEqual(structural["source_verification"]["status"], "NOT_REQUESTED")
            checked = audit(registry, verify_sources=True, evidence_root=root)
            self.assertEqual(checked["status"], "FAIL_SOURCE_VERIFICATION")
            self.assertEqual(checked["source_verification"]["problems"][0]["code"], "SOURCE_ARTIFACTS_NOT_DECLARED")

    def test_valid_sources_are_verified_and_tamper_missing_and_multiple_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-evidence-source-") as temporary:
            root = Path(temporary)
            one = b'{"run":"one"}\n'; two = b'{"run":"two"}\n'
            (root / "records").mkdir()
            (root / "records" / "one.json").write_bytes(one)
            (root / "records" / "two.json").write_bytes(two)
            descriptors = [self._descriptor("one", "records/one.json", one), self._descriptor("two", "records/two.json", two)]
            registry = self._registry(root, self._record(descriptors))
            passed = audit(registry, verify_sources=True, evidence_root=root)
            self.assertEqual(passed["status"], "PASS_STRUCTURAL_AND_SOURCES_VERIFIED_NO_STATUS_PROMOTION")
            self.assertEqual(passed["source_verification"]["artifact_count_checked"], 2)
            self.assertFalse(passed["source_verification"]["root_disclosed"])
            (root / "records" / "one.json").write_bytes(b"changed")
            (root / "records" / "two.json").unlink()
            failed = audit(registry, verify_sources=True, evidence_root=root)
            self.assertEqual(failed["status"], "FAIL_SOURCE_VERIFICATION")
            self.assertEqual({item["code"] for item in failed["source_verification"]["problems"]}, {"SOURCE_SIZE_MISMATCH", "SOURCE_MISSING"})

    def test_schema_rejects_unsafe_or_underspecified_declared_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-evidence-source-") as temporary:
            root = Path(temporary); registry = root / "registry.json"; initialize(registry)
            for locator in ("/etc/passwd", "../outside", "records//bad", "records\\bad"):
                with self.subTest(locator=locator):
                    with self.assertRaisesRegex(DeMethFlowError, "safe relative|portable relative"):
                        import_record(registry, self._record([self._descriptor("one", locator, b"x")], evidence_id="bad-" + sha256(locator.encode())[:8]))
            bad = self._descriptor("one", "records/a.json", b"x"); bad["size_bytes"] = -1
            with self.assertRaisesRegex(DeMethFlowError, "non-negative"):
                import_record(registry, self._record([bad], evidence_id="bad-size"))
            with self.assertRaisesRegex(DeMethFlowError, "non-empty list"):
                import_record(registry, self._record([], evidence_id="bad-empty"))

    def test_symlink_nonregular_and_bounded_read_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-evidence-source-") as temporary:
            root = Path(temporary); (root / "records").mkdir()
            outside = root / "outside.json"; outside.write_text("outside", encoding="utf-8")
            link = root / "records" / "link.json"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink fixture unavailable: {exc}")
            registry = self._registry(root, self._record([self._descriptor("link", "records/link.json", b"outside")]))
            report = audit(registry, verify_sources=True, evidence_root=root)
            self.assertEqual(report["source_verification"]["problems"][0]["code"], "SOURCE_LOCATOR_SYMLINK")
            link.unlink(); (root / "records" / "directory").mkdir()
            registry2 = self._registry(root, self._record([self._descriptor("dir", "records/directory", b"")], evidence_id="nonregular"))
            report2 = audit(registry2, verify_sources=True, evidence_root=root)
            self.assertIn("SOURCE_NOT_REGULAR_FILE", {item["code"] for item in report2["source_verification"]["problems"]})
            data = b"oversize"
            (root / "records" / "large.json").write_bytes(data)
            registry3 = self._registry(root, self._record([self._descriptor("large", "records/large.json", data)], evidence_id="bounded"))
            report3 = audit(registry3, verify_sources=True, evidence_root=root, maximum_source_bytes=3)
            self.assertIn("SOURCE_TOO_LARGE", {item["code"] for item in report3["source_verification"]["problems"]})

    def test_public_cli_requires_explicit_verify_and_never_executes_legacy_command(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-evidence-source-") as temporary:
            root = Path(temporary); marker = root / "marker"
            data = b"fixture\n"; (root / "item.json").write_bytes(data)
            record = self._record([self._descriptor("item", "item.json", data)])
            record["source"] = {"command": f"touch {marker}"}
            registry = self._registry(root, record)
            stream = StringIO()
            with redirect_stdout(stream):
                self.assertEqual(main(["evidence", "audit", "--registry", str(registry)]), 0)
            self.assertFalse(marker.exists())
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["evidence", "audit", "--registry", str(registry), "--verify-sources", "--evidence-root", str(root)]), 0)
            (root / "item.json").write_bytes(b"changed\n")
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["evidence", "audit", "--registry", str(registry), "--verify-sources", "--evidence-root", str(root)]), 2)
            self.assertFalse(marker.exists())

    def test_source_root_requirements_are_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-evidence-source-") as temporary:
            root = Path(temporary); registry = self._registry(root, self._record())
            with self.assertRaisesRegex(DeMethFlowError, "ROOT_REQUIRED"):
                audit(registry, verify_sources=True)
            with self.assertRaisesRegex(DeMethFlowError, "ROOT_UNUSED"):
                audit(registry, evidence_root=root)
            if hasattr(os, "symlink"):
                root_link = root.parent / (root.name + "-link")
                try:
                    root_link.symlink_to(root, target_is_directory=True)
                    with self.assertRaisesRegex(DeMethFlowError, "ROOT_INVALID"):
                        audit(registry, verify_sources=True, evidence_root=root_link)
                finally:
                    if root_link.is_symlink():
                        root_link.unlink()

    def test_explicit_source_gate_is_nonzero_when_structural_binding_fails(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-evidence-source-") as temporary:
            root = Path(temporary); data = b"fixture\n"; (root / "item.json").write_bytes(data)
            record = self._record([self._descriptor("item", "item.json", data)])
            record["relationships"] = [{"relation": "SUPPLEMENTS", "target_evidence_id": "missing"}]
            registry = self._registry(root, record)
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["evidence", "audit", "--registry", str(registry), "--verify-sources", "--evidence-root", str(root)]), 2)


if __name__ == "__main__":
    unittest.main()
