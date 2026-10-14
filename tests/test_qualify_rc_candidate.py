from __future__ import annotations

import importlib.util
import io
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qualify_rc_candidate.py"
SPEC = importlib.util.spec_from_file_location("qualify_rc_candidate", SCRIPT)
assert SPEC and SPEC.loader
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


class ExtractSourceArchiveTests(unittest.TestCase):
    def test_clean_install_environment_removes_source_import_leak(self):
        environment = qualification.clean_install_environment({
            "PYTHONPATH": "/untrusted/source/src",
            "OTHER_SETTING": "retained",
        })
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["OTHER_SETTING"], "retained")
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")

    def _archive(self, root: Path, members: list[tuple[str, bytes]]) -> Path:
        path = root / "fixture.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, payload in members:
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o755 if name.endswith("pkg.py") else 0o644
                archive.addfile(info, io.BytesIO(payload))
        return path

    def test_extracts_only_regular_members_under_exact_root(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-qualification-") as temporary:
            root = Path(temporary)
            archive = self._archive(root, [
                ("methunmix-1.0.0/README.md", b"readme"),
                ("methunmix-1.0.0/src/pkg.py", b"value = 1\n"),
            ])
            extracted = qualification.extract_source_archive(
                archive, root / "destination", expected_root="methunmix-1.0.0"
            )
            self.assertEqual(extracted, root / "destination" / "methunmix-1.0.0")
            self.assertEqual((extracted / "src/pkg.py").read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual(stat.S_IMODE((extracted / "src/pkg.py").stat().st_mode), 0o755)

    def test_rejects_archive_path_escape_before_writing(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-qualification-") as temporary:
            root = Path(temporary)
            archive = self._archive(root, [
                ("methunmix-1.0.0/README.md", b"readme"),
                ("methunmix-1.0.0/../outside.txt", b"escape"),
            ])
            with self.assertRaisesRegex(ValueError, "unsafe source archive member"):
                qualification.extract_source_archive(
                    archive, root / "destination", expected_root="methunmix-1.0.0"
                )
            self.assertFalse((root / "destination" / "methunmix-1.0.0" / "README.md").exists())


if __name__ == "__main__":
    unittest.main()
