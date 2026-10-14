from __future__ import annotations

import unittest
from pathlib import Path

from demethflow_core.cli import build_parser


class UserGuideExampleTests(unittest.TestCase):
    """Keep documented command forms within their actual parser scopes.

    This is deliberately a parser test: it does not claim that the placeholder
    paths, assets or host runtime make a real workflow executable.
    """

    def test_primary_english_and_chinese_examples_parse(self):
        parser = build_parser()
        examples = [
            ["adapter", "validate", "--json"],
            ["adapter", "list", "--json"],
            ["asset", "audit", "--online", "--catalog-url", "https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json", "--metadata-dir", "/data/catalog-metadata"],
            ["reference", "available", "--metadata-dir", "/data/catalog-metadata"],
            ["reference", "fetch", "--install", "--metadata-dir", "/data/catalog-metadata", "builtin.brain.450k@1.0.0"],
            ["reference", "validate", "--checksums", "builtin.brain.450k@1.0.0"],
            ["reference", "install", "/data/archives/reference.tar.gz"],
            ["asset", "verify", "reference.builtin.brain.450k@1.0.0", "--json"],
            ["plan", "--scenario", "brain", "--platform", "450k", "--reference", "builtin.brain.450k@1.0.0", "--tools", "Houseman", "--input", "/data/input/brain-450k.csv", "--reference-root", "/data/refs", "--runtime-root", "/data/runtimes", "--container-engine", "/opt/apptainer/bin/apptainer", "--json"],
            ["doctor", "--scenario", "brain", "--platform", "450k", "--reference", "builtin.brain.450k@1.0.0", "--tools", "Houseman", "--input", "/data/input/brain-450k.csv", "--reference-root", "/data/refs", "--runtime-root", "/data/runtimes", "--container-engine", "/opt/apptainer/bin/apptainer", "--json"],
            ["asset", "plan", "--scenario", "brain", "--platform", "450k", "--analysis-contract", "array_450k_cpg", "--reference", "builtin.brain.450k@1.0.0", "--tools", "Houseman", "--accelerator", "cpu", "--local-dependency-lock", "/secure/bindings/houseman.json", "--local-dependency-locators", "/secure/host/houseman-locators.json", "--summary"],
            ["run", "--mode", "decon", "--scenario", "brain", "--platform", "450k", "--reference", "builtin.brain.450k@1.0.0", "--tools", "Houseman", "--input", "/data/input/brain-450k.csv", "--outdir", "/data/results/houseman-source", "--reference-root", "/data/refs", "--runtime-root", "/data/runtimes", "--container-engine", "/opt/apptainer/bin/apptainer", "--random-seed", "20260826", "--workflow-timeout-seconds", "600"],
            ["report", "--run-dir", "/data/results/houseman-source", "--output-dir", "/data/results/houseman-source-report", "--json"],
            ["replay-plan", "--manifest", "/data/results/houseman-source/run_manifest.json", "--outdir", "/data/results/houseman-replay", "--input", "/data/input/brain-450k.csv", "--verify-input-identity", "--output", "/data/results/houseman-replay-plan.json"],
            ["replay-execute", "--replay-plan", "/data/results/houseman-replay-plan.json", "--source-manifest", "/data/results/houseman-source/run_manifest.json", "--input", "/data/input/brain-450k.csv", "--outdir", "/data/results/houseman-replay", "--reference-root", "/data/refs", "--runtime-root", "/data/runtimes", "--workflow-timeout-seconds", "600", "--output", "/data/results/houseman-replay-preview.json"],
            ["replay-execute", "--replay-plan", "/data/results/houseman-replay-plan.json", "--source-manifest", "/data/results/houseman-source/run_manifest.json", "--input", "/data/input/brain-450k.csv", "--outdir", "/data/results/houseman-replay", "--reference-root", "/data/refs", "--runtime-root", "/data/runtimes", "--workflow-timeout-seconds", "600", "--execute", "--report-output-dir", "/data/results/houseman-replay-report"],
            ["retry-failed-tool", "--source-run-dir", "/data/results/failed-houseman", "--tool", "Houseman", "--outdir", "/data/results/houseman-fresh-retry", "--request-json", "/data/requests/houseman-retry.json", "--execute"],
        ]
        for argv in examples:
            with self.subTest(command=argv[0]):
                parser.parse_args(argv)

    def test_run_only_options_are_not_accepted_by_plan_or_doctor(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["plan", "--outdir", "/x", "--random-seed", "7"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["doctor", "--workflow-timeout-seconds", "600"])

    def test_packaged_guides_use_current_candidate6_catalog_and_no_retired_claims(self):
        root = Path(__file__).resolve().parents[1]
        current_url = (
            "https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/"
            "methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json"
        )
        for relative in (
            "README.md",
            "docs/PUBLIC_USER_GUIDE.md",
            "docs/PUBLIC_USER_GUIDE_zh-CN.md",
            "docs/REFERENCE_INSTALLATION_GUIDE.md",
            "docs/REFERENCE_INSTALLATION_GUIDE_zh-CN.md",
        ):
            text = (root / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn(current_url, text)
                self.assertNotIn("candidate.4", text)
                self.assertNotIn("no current catalog URL is active", text)
                self.assertNotIn("当前 catalog URL 仍为空", text)

    def test_same_version_artifact_identity_is_not_misrepresented(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn("artifact identity", readme.lower())
        self.assertIn("owner-reviewed release manifest", readme)
        self.assertNotIn("unpublished local fixed candidate", readme.lower())
        self.assertIn("1.0.0", readme)
        self.assertIn("cf15a70e53fa3ecae2d940b90b919a0643724d38b48098d2a1532dabdda16133", readme)


if __name__ == "__main__":
    unittest.main()
