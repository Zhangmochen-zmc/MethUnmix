from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_release_license_ledger.py"
SPEC = importlib.util.spec_from_file_location("release_license_ledger", SCRIPT)
assert SPEC and SPEC.loader
ledger_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger_audit)


class ReleaseLicenseLedgerTests(unittest.TestCase):
    @staticmethod
    def _complete_context() -> dict[str, object]:
        """A portable fixture: extracted source intentionally has no evidence/."""
        return {
            "core_version": "2.0.0rc3",
            "source_commit": "a" * 40,
            "artifact_sha256": {"source": "b" * 64, "wheel": "c" * 64},
            "inventory": {"sha256": "d" * 64},
            "reference_approval": {"sha256": "e" * 64},
            "reference_remote_audit": {"sha256": "f" * 64},
            "reference_catalog": {"release": "fixture-static.2", "sha256": "g" * 64, "target_count": 33},
            "reference_approval_complete": True,
            "reference_transport_technical_ready": True,
            "qualification_status": "LOCAL_RC_CANDIDATE_QUALIFIED",
        }

    def test_missing_ledger_is_explicitly_non_approving(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-license-ledger-") as tmp:
            payload = ledger_audit.build_audit(ledger_path=Path(tmp) / "missing.json")
        self.assertEqual(payload["result"]["status"], "MISSING_OWNER_APPROVAL_LEDGER")

    def test_template_cannot_be_mistaken_for_owner_approval(self):
        context = ledger_audit.current_context()
        result = ledger_audit.audit_ledger(ledger_audit.template(context), context)
        self.assertEqual(result["status"], "LICENSE_LEDGER_INCOMPLETE_OR_UNBOUND")
        self.assertIn("status must be APPROVED_FULL_RELEASE_SCOPE", result["problems"])

    def test_exact_owner_ledger_requires_current_binding(self):
        context = self._complete_context()
        ledger = ledger_audit.template(context)
        ledger.update({
            "status": "APPROVED_FULL_RELEASE_SCOPE",
            "approved_by": "fixture owner",
            "approved_at": "2026-09-29",
            "decision_basis": "fixture only",
        })
        result = ledger_audit.audit_ledger(ledger, context)
        self.assertEqual(result["status"], "APPROVED_FULL_RELEASE_SCOPE_CURRENT_CANDIDATE")
        ledger["candidate"]["artifact_sha256"]["wheel"] = "0" * 64
        self.assertEqual(ledger_audit.audit_ledger(ledger, context)["status"], "LICENSE_LEDGER_INCOMPLETE_OR_UNBOUND")

    def test_template_is_machine_readable(self):
        context = self._complete_context()
        rendered = json.dumps(ledger_audit.template(context))
        self.assertIn("DRAFT_OWNER_DECISION_REQUIRED", rendered)
