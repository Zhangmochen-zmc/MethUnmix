import json
import tempfile
import unittest
from pathlib import Path

from demethflow_core.errors import DeMethFlowError
from demethflow_core.external_runtime import discover_external_runtimes


class ExternalRuntimeContractTests(unittest.TestCase):
    def _root(self, *, complete=True):
        root = Path(tempfile.mkdtemp(prefix="methunmix-external-runtime-"))
        (root / "CelFEER").mkdir()
        (root / "CelFEER" / "adapter.py").write_text("# synthetic only\n", encoding="utf-8")
        document = {
            "schema": "methunmix-external-runtime-v1",
            "runtimes": {
                "CelFEER": {
                    "path": "CelFEER",
                    "identity": "synthetic-celfeer",
                    "version": "0.0",
                    "license": "USER_SUPPLIED_SUPPORTED",
                    "required_files": ["adapter.py"] if complete else ["missing.py"],
                }
            },
        }
        (root / "methunmix-external-runtime.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
        return root

    def test_synthetic_contract_resolves_without_execution(self):
        root = self._root()
        found = discover_external_runtimes(root, {"CelFEER"})
        self.assertEqual(found["CelFEER"].identity, "synthetic-celfeer")
        self.assertEqual(found["CelFEER"].path, root / "CelFEER")

    def test_missing_required_file_fails_closed(self):
        with self.assertRaisesRegex(DeMethFlowError, "EXTERNAL_RUNTIME_NOT_INSTALLED"):
            discover_external_runtimes(self._root(complete=False), {"CelFEER"})

    def test_no_selected_external_tool_is_noop(self):
        self.assertEqual(discover_external_runtimes(None, {"MEnet"}), {})


if __name__ == "__main__":
    unittest.main()
