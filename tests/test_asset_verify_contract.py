from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from demethflow_core.cli import main
from demethflow_core.errors import InstallError
from demethflow_core.modules import ModuleStore
from demethflow_core.release import _build_module_archive
from demethflow_core.util import data_home_override


class AssetVerifyContractTests(unittest.TestCase):
    def _archive(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        (source / "entrypoint.txt").write_text("fixture\n", encoding="utf-8")
        archive = root / "fixture.tar.gz"
        _build_module_archive(
            archive, module_id="runtime.fixture", module_type="runtime",
            module_version="1.0.0", sources=[(source / "entrypoint.txt", "entrypoint.txt")],
        )
        return archive

    def _call(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_public_cli_rejects_unknown_target_and_empty_store(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-asset-verify-") as temporary:
            home = Path(temporary) / "home"
            code, stdout, stderr = self._call(["--home", str(home), "asset", "verify", "missing@9.9.9", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "TARGET_NOT_FOUND")
            self.assertEqual(payload["target"], "missing@9.9.9")
            self.assertEqual(payload["modules"], [])

            code, stdout, _ = self._call(["--home", str(home), "asset", "verify", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout)["status"], "NO_VERIFIABLE_MODULES")

    def test_public_cli_distinguishes_exact_selector_valid_and_damaged_target(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-asset-verify-") as temporary:
            root = Path(temporary)
            home = root / "home"
            archive = self._archive(root)
            with data_home_override(home):
                self.assertEqual(main(["module", "install", str(archive)]), 0)

            code, stdout, _ = self._call(["--home", str(home), "asset", "verify", "runtime.fixture@1.0.0", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout)["status"], "VERIFIED")

            code, stdout, _ = self._call(["--home", str(home), "asset", "verify", "runtime.fixture@2.0.0", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout)["status"], "TARGET_NOT_FOUND")

            payload = json.loads((home / ".methunmix" / "modules" / "runtime.fixture@1.0.0.json").read_text(encoding="utf-8"))
            Path(payload["installed_path"], "entrypoint.txt").write_text("damaged\n", encoding="utf-8")
            code, stdout, _ = self._call(["--home", str(home), "asset", "verify", "runtime.fixture", "--json"])
            self.assertEqual(code, 2)
            damaged = json.loads(stdout)
            self.assertEqual(damaged["status"], "INTEGRITY_FAILURE")
            self.assertTrue(damaged["modules"][0]["errors"])

            code, human, _ = self._call(["--home", str(home), "asset", "verify", "runtime.fixture"])
            self.assertEqual(code, 2)
            self.assertIn("INTEGRITY_FAILURE", human)
            self.assertIn("runtime.fixture@1.0.0", human)

            with data_home_override(home):
                with self.assertRaisesRegex(InstallError, "failed current integrity verification"):
                    ModuleStore().install(archive)
