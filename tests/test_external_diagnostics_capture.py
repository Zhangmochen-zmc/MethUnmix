from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ExternalDiagnosticsCaptureTests(unittest.TestCase):
    """Exercise the core-owned wrapper with base-R fixtures, never MeDeCom."""

    @classmethod
    def setUpClass(cls):
        cls.rscript = shutil.which("Rscript")
        if cls.rscript is None:
            raise unittest.SkipTest("Rscript is not available for the lightweight fixture")
        cls.wrapper = (
            Path(__file__).resolve().parents[1]
            / "src/methunmix_assets/deconvolution/bin/capture_external_diagnostics.sh"
        )

    def _run(self, source: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        temporary = tempfile.TemporaryDirectory(prefix="methunmix-r-diagnostics-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        fixture = root / "fixture.R"
        fixture.write_text(source, encoding="utf-8")
        output = root / "diagnostics"
        completed = subprocess.run(
            [str(self.wrapper), "--stage", "fixture_r", "--output", str(output), "--", self.rscript, str(fixture)],
            text=True,
            capture_output=True,
            check=False,
        )
        return completed, output

    def test_normal_r_exit_is_preserved_and_recorded(self):
        completed, output = self._run('cat("normal fixture stdout\\n")\n')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "normal fixture stdout\n")
        metadata = json.loads((output / "capture-metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["terminal_state"], "COMPLETED")
        self.assertEqual(metadata["process_exit_code"], 0)
        self.assertIn("COMPLETED", (output / "events.tsv").read_text(encoding="utf-8"))

    def test_warning_text_is_retained_without_changing_success(self):
        completed, output = self._run('warning("fixture warning text")\ncat("continues after warning\\n")\n')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("fixture warning text", (output / "stderr.raw.log").read_text(encoding="utf-8"))
        self.assertIn("fixture warning text", (output / "warnings.raw.log").read_text(encoding="utf-8"))
        self.assertIn("WARNING", (output / "events.tsv").read_text(encoding="utf-8"))

    def test_error_after_warning_remains_nonzero_and_is_separate(self):
        completed, output = self._run('warning("before fixture failure")\nstop("fixture failure")\n')
        self.assertNotEqual(completed.returncode, 0)
        metadata = json.loads((output / "capture-metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["terminal_state"], "ERROR")
        self.assertEqual(metadata["process_exit_code"], completed.returncode)
        self.assertIn("before fixture failure", (output / "warnings.raw.log").read_text(encoding="utf-8"))
        self.assertIn("fixture failure", (output / "stderr.raw.log").read_text(encoding="utf-8"))
        self.assertIn("ERROR", (output / "events.tsv").read_text(encoding="utf-8"))

    def test_diagnostic_setup_failure_does_not_launch_the_wrapped_process(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-r-diagnostics-failure-") as temporary:
            root = Path(temporary)
            marker = root / "wrapped-process-was-started"
            # A regular file cannot be made into the required diagnostic
            # directory.  The fixture must consequently never be launched.
            output = root / "not-a-directory"
            output.write_text("fixture", encoding="utf-8")
            fixture = root / "fixture.R"
            fixture.write_text(f'file.create("{marker}")\n', encoding="utf-8")
            completed = subprocess.run(
                [str(self.wrapper), "--stage", "fixture_r", "--output", str(output), "--", self.rscript, str(fixture)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 73)
            self.assertIn("setup failed before wrapped process launch", completed.stderr)
            self.assertFalse(marker.exists())

    def test_medecom_workflow_declares_the_core_owned_capture_wrapper(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / "src/methunmix_assets/deconvolution/450k_main.nf"
        ).read_text(encoding="utf-8")
        self.assertIn('path "medecom_external_diagnostics"', workflow)
        self.assertIn("capture_external_diagnostics.sh", workflow)
        self.assertIn("--stage medecom_external_r", workflow)
        self.assertIn('medecom_decon.R --mix', workflow)

    def test_nextflow_process_uses_packaged_wrapper_for_normal_warning_and_error(self):
        """Exercise the same ``${projectDir}/bin`` invocation shape as RUN_MeDeCom.

        This is a synthetic Nextflow task, not the MeDeCom algorithm, adapter,
        product CLI, or an Apptainer/SIF test.  It proves only that a packaged
        workflow project can run the wrapper and retain its three terminal
        outcomes without treating a warning as an error.
        """
        nextflow = os.environ.get("METHUNMIX_TEST_NEXTFLOW")
        if not nextflow or not Path(nextflow).is_file():
            self.skipTest("set METHUNMIX_TEST_NEXTFLOW to run the synthetic Nextflow process fixture")
        with tempfile.TemporaryDirectory(prefix="methunmix-nextflow-diagnostics-") as temporary:
            root = Path(temporary)
            (root / "bin").mkdir()
            wrapper = root / "bin" / "capture_external_diagnostics.sh"
            shutil.copy2(self.wrapper, wrapper)
            fixtures = {
                "normal": ('cat("normal stdout\\n")\n', 0),
                "warning": ('warning("synthetic warning text")\ncat("warning stdout\\n")\n', 0),
                "error": ('warning("before synthetic error")\nstop("synthetic error")\n', 1),
            }
            for label, (source, _) in fixtures.items():
                (root / f"{label}.R").write_text(source, encoding="utf-8")
            channels = ",\n        ".join(
                f"tuple('{label}', file(params.{label}), {expected})"
                for label, (_, expected) in fixtures.items()
            )
            (root / "main.nf").write_text(
                f"""nextflow.enable.dsl=2
process DIAGNOSTIC_FIXTURE {{
  tag "$label"
  input:
  tuple val(label), path(fixture), val(expected)
  output:
  path "result-*.txt"
  script:
  \"\"\"
  set +e
  bash \"${{projectDir}}/bin/capture_external_diagnostics.sh\" --stage synthetic_r --output diagnostics -- Rscript \"$fixture\" > \"${{label}}.stdout\" 2> \"${{label}}.stderr\"
  code=\\$?
  set -e
  test \"\\$code\" -eq \"$expected\"
  printf 'label=%s\\nwrapped_exit=%s\\n' \"$label\" \"\\$code\" > \"result-${{label}}.txt\"
  \"\"\"
}}
workflow {{
  DIAGNOSTIC_FIXTURE(Channel.of(
        {channels}
  ))
}}
""",
                encoding="utf-8",
            )
            command = [nextflow, "run", "-offline", "main.nf"]
            for label in fixtures:
                command.extend([f"--{label}", str(root / f"{label}.R")])
            environment = {**os.environ, "NXF_HOME": str(root / "nxf-home"), "NXF_ANSI_LOG": "false"}
            completed = subprocess.run(command, cwd=root, text=True, capture_output=True, env=environment, check=False, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            work = root / "work"
            result_files = sorted(work.rglob("result-*.txt"))
            self.assertEqual(len(result_files), 3)
            records = {path.name.removeprefix("result-").removesuffix(".txt"): path.read_text(encoding="utf-8") for path in result_files}
            self.assertIn("wrapped_exit=0", records["normal"])
            self.assertIn("wrapped_exit=0", records["warning"])
            self.assertIn("wrapped_exit=1", records["error"])
            warning_log = next(path.parent / "diagnostics" / "warnings.raw.log" for path in result_files if path.name == "result-warning.txt")
            error_raw = next(path.parent / "diagnostics" / "stderr.raw.log" for path in result_files if path.name == "result-error.txt")
            self.assertIn("synthetic warning text", warning_log.read_text(encoding="utf-8"))
            self.assertIn("synthetic error", error_raw.read_text(encoding="utf-8"))
