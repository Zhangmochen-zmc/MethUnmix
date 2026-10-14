from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


class PublicationVerificationPlanTests(unittest.TestCase):
    def test_postupload_plan_is_nonexecuting_and_requires_https_hashes(self):
        from scripts import verify_postupload_release as checker

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "postupload.json"
            arguments = [
                "--version", "2.1.0rc1",
                "--sdist-url", "https://releases.example.invalid/methunmix-2.1.0rc1.tar.gz",
                "--sdist-sha256", "a" * 64,
                "--wheel-url", "https://releases.example.invalid/methunmix-2.1.0rc1-py3-none-any.whl",
                "--wheel-sha256", "b" * 64,
                "--output", str(output),
            ]
            original_argv = sys.argv
            try:
                sys.argv = ["verify_postupload_release.py", *arguments]
                self.assertEqual(checker.main(), 0)
            finally:
                sys.argv = original_argv
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "PREPARED_NOT_EXECUTED")

    def test_postupload_plan_function_refuses_local_url(self):
        from argparse import Namespace
        from scripts import verify_postupload_release as checker

        result = checker.plan(Namespace(
            version="2.1.0rc1", sdist_url="file:///tmp/source.tar.gz", sdist_sha256="a" * 64,
            wheel_url="https://example.invalid/wheel.whl", wheel_sha256="b" * 64, execute=False,
        ))
        self.assertEqual(result["status"], "INVALID_PLAN")
        self.assertTrue(any("https" in item for item in result["problems"]))

    def test_downstream_plan_refuses_local_paths_and_does_not_execute_by_default(self):
        from scripts import verify_downstream_release as checker

        self.assertFalse(checker.safe_spec("file:///tmp/methunmix.conda"))
        self.assertTrue(checker.safe_spec("methunmix=2.1.0rc1"))


if __name__ == "__main__":
    unittest.main()
