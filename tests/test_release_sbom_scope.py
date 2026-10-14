from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_release_sbom_scope.py"
SPEC = importlib.util.spec_from_file_location("release_sbom_scope", SCRIPT)
assert SPEC and SPEC.loader
scope_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope_audit)


class ReleaseSbomScopeTests(unittest.TestCase):
    @staticmethod
    def _context() -> dict[str, object]:
        return {
            "core_version": "2.0.0rc3", "source_commit": "a" * 40,
            "artifact_sha256": {"source": "b" * 64, "wheel": "c" * 64},
            "core_sbom": {"sha256": "d" * 64}, "core_sbom_partial_bound": True,
            "remote_audit": {"sha256": "e" * 64},
            "license_ledger_audit": {"sha256": "f" * 64}, "license_ledger_approved": True,
            "public_references": {"fixture@1.0": "1" * 64},
            "qualification_status": "LOCAL_RC_CANDIDATE_QUALIFIED",
        }

    def test_draft_template_is_not_a_complete_scope(self):
        result = scope_audit.audit_ledger(scope_audit.template(self._context()), self._context())
        self.assertEqual(result["status"], "SBOM_SCOPE_INCOMPLETE_OR_UNBOUND")

    def test_complete_scope_requires_every_identity(self):
        context = self._context()
        ledger = scope_audit.template(context)
        ledger["status"] = "COMPLETE_RELEASE_SCOPE"
        ledger["conda_environment"] = {"lock_sha256": "2" * 64, "sbom_sha256": "3" * 64, "sbom_url": "https://example.invalid/conda.json"}
        ledger["public_reference_sboms"][0].update({"sbom_sha256": "4" * 64, "sbom_url": "https://example.invalid/reference.json"})
        self.assertEqual(scope_audit.audit_ledger(ledger, context)["status"], "COMPLETE_RELEASE_SCOPE_CURRENT_CANDIDATE")
        ledger["public_reference_sboms"][0]["archive_sha256"] = "0" * 64
        self.assertEqual(scope_audit.audit_ledger(ledger, context)["status"], "SBOM_SCOPE_INCOMPLETE_OR_UNBOUND")

    def test_template_is_json_serializable(self):
        self.assertIn("DRAFT_SBOM_SCOPE_REQUIRED", json.dumps(scope_audit.template(self._context())))
