from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


class PrepublicationCheckTests(unittest.TestCase):
    def test_bioconda_draft_has_no_local_source_or_fabricated_artifact_identity(self):
        draft_path = (
            Path(__file__).resolve().parents[1]
            / "packaging"
            / "bioconda-public-core-draft"
            / "meta.yaml.pending"
        )
        # The recipe is intentionally a private owner-review aid, not a
        # member of the portable upstream source archive.
        if not draft_path.is_file():
            self.skipTest("non-submittable downstream recipe draft is excluded from portable source archive")
        draft = draft_path.read_text(encoding="utf-8")
        self.assertIn("NON-SUBMITTABLE OWNER-REVIEW DRAFT", draft)
        self.assertIn('{% set version = "1.0.0" %}', draft)
        self.assertIn("releases/download/v{{ version }}/methunmix-{{ version }}.tar.gz", draft)
        self.assertIn("LOCAL_1_0_0_SDIST_SHA256_PENDING_BUILD", draft)
        self.assertIn("number: 0", draft)
        self.assertIn("methunmix = demethflow_core.cli:main", draft)
        self.assertNotIn("demethflow = demethflow_core.legacy:main", draft)
        self.assertNotIn("file://", draft)
        self.assertNotIn("/data/", draft)

    def _root(self, base: Path, version: str = "1.0.0") -> Path:
        root = base / "source"
        (root / "src/methunmix_assets").mkdir(parents=True)
        (root / "docs/publication").mkdir(parents=True)
        (root / "VERSION").write_text(version + "\n", encoding="utf-8")
        (root / "src/methunmix_assets/VERSION").write_text(version + "\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            f'[project]\nversion = "{version}"\n[project.scripts]\nmethunmix = "demethflow_core.cli:main"\n',
            encoding="utf-8",
        )
        for name in ("LICENSE", "NOTICE", "README.md", "CITATION.cff"):
            (root / name).write_text("core text\n", encoding="utf-8")
        (root / "docs/PUBLIC_USER_GUIDE.md").write_text("<PUBLISHED_WHEEL_URL>\n", encoding="utf-8")
        (root / "docs/PUBLIC_USER_GUIDE_zh-CN.md").write_text("draft\n", encoding="utf-8")
        (root / "docs/publication/PENDING_OWNER_DECISIONS.json").write_text(json.dumps({"status": "DRAFT_PENDING_OWNER_APPROVAL"}), encoding="utf-8")
        (root / "docs/publication/PUBLICATION_SCOPE_DRAFT.md").write_text("draft\n", encoding="utf-8")
        return root

    def _artifacts(self, base: Path, extra: str | None = None) -> tuple[Path, Path]:
        wheel = base / "core.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("demethflow_core/cli.py", "pass\n")
        sdist = base / "core.tar.gz"
        with tarfile.open(sdist, "w:gz") as archive:
            info = tarfile.TarInfo("methunmix-2.1.0/README.md")
            data = b"readme\n"
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            if extra:
                info = tarfile.TarInfo(extra)
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
        return wheel, sdist

    def test_preparation_accepts_pending_owner_decisions_but_release_ready_rejects_them(self):
        from scripts import check_prepublication as checker
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._root(base)
            wheel, sdist = self._artifacts(base)
            preparation = checker.check(root, wheel, sdist, "preparation")
            self.assertEqual(preparation["status"], "PASS_PREPARATION_PENDING_OWNER_APPROVAL")
            release = checker.check(root, wheel, sdist, "release-ready")
            self.assertEqual(release["status"], "FAIL")
            self.assertFalse(any("development/local version" in item for item in release["errors"]))
            self.assertTrue(any("owner approvals" in item for item in release["errors"]))

    def test_forbidden_external_payload_and_version_drift_fail(self):
        from scripts import check_prepublication as checker
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._root(base)
            (root / "src/methunmix_assets/VERSION").write_text("2.1.0.dev15+other\n", encoding="utf-8")
            wheel, sdist = self._artifacts(base, "methunmix-2.1.0/assets/hidden.sif")
            result = checker.check(root, wheel, sdist, "preparation")
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(any("version sources disagree" in item for item in result["errors"]))
            self.assertTrue(any("forbidden external payload" in item for item in result["errors"]))

    def test_public_console_script_is_methunmix_only(self):
        import tomllib

        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["scripts"], {"methunmix": "demethflow_core.cli:main"})

    def test_owner_metadata_order_contact_and_license_boundary(self):
        import tomllib

        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(
            [item["name"] for item in project["authors"]],
            ["Mochen Zhang", "Xiangyu Yu", "Yaoke Wei"],
        )
        self.assertEqual(
            project["maintainers"],
            [{"name": "Mochen Zhang", "email": "zhangmochen@big.ac.cn"}],
        )
        citation = (root / "CITATION.cff").read_text(encoding="utf-8")
        self.assertLess(citation.index('given-names: "Mochen"'), citation.index('given-names: "Xiangyu"'))
        self.assertLess(citation.index('given-names: "Xiangyu"'), citation.index('given-names: "Yaoke"'))
        self.assertNotIn("orcid:", citation.lower())
        self.assertIn('license: "MIT"', citation)
        license_text = (root / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Mochen Zhang", license_text)
        self.assertIn("and/or sell", license_text)
        notice = (root / "NOTICE").read_text(encoding="utf-8")
        self.assertIn("selected CC BY-NC 4.0", notice)
        self.assertIn("not\nan additional restriction on the MIT-licensed core", notice)
        self.assertIn("Contact is not authorization", notice)

        self.assertEqual(
            project["urls"],
            {
                "Homepage": "https://github.com/Zhangmochen-zmc/MethUnmix",
                "Repository": "https://github.com/Zhangmochen-zmc/MethUnmix",
                "Issues": "https://github.com/Zhangmochen-zmc/MethUnmix/issues",
            },
        )

    def test_recorded_owner_metadata_preparation_status(self):
        from scripts import check_prepublication as checker

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._root(base)
            (root / "docs/publication/PENDING_OWNER_DECISIONS.json").write_text(
                json.dumps({"status": "OWNER_METADATA_RECORDED_REFERENCE_LICENSE_AND_PLATFORM_PENDING"}),
                encoding="utf-8",
            )
            wheel, sdist = self._artifacts(base)
            result = checker.check(root, wheel, sdist, "preparation")
            self.assertEqual(result["status"], "PASS_PREPARATION_PENDING_REFERENCE_LICENSE_AND_PLATFORM")

    def test_owner_decision_binding_and_unresolved_boundaries(self):
        root = Path(__file__).resolve().parents[1]
        decision = json.loads(
            (root / "docs/publication/OWNER_DECISIONS_2026-10-12.json").read_text(encoding="utf-8")
        )
        references = decision["references"]
        self.assertEqual(references["distribution_status"], "OWNER_APPROVED_DISTRIBUTION")
        self.assertEqual(references["derived_object_count"], 30)
        self.assertEqual(
            references["approval_binding_sha256"],
            "8f641feee00962f9605eb92ede71e6dafda3413f6c814c2c66707a1d19d336bc",
        )
        self.assertEqual(references["excluded_rows"], [])
        self.assertEqual(references["use_policy_status"], "LICENSE_POLICY_NONCOMMERCIAL_CONFIRMED")
        self.assertEqual(references["license_instrument_status"], "LICENSE_INSTRUMENT_PENDING")
        self.assertEqual(references["rights_holder_status"], "PENDING_OWNER_CONFIRMATION")
        self.assertTrue(all(value is None for key, value in decision["platform"].items() if key != "status"))
        self.assertEqual(decision["public_actions"], {"status": "PUBLIC_ACTION_NOT_AUTHORIZED", "executed": []})

    def test_license_and_software_target_decision_is_recorded_without_public_action(self):
        root = Path(__file__).resolve().parents[1]
        decision = json.loads(
            (root / "docs/publication/OWNER_DECISIONS_2026-10-12_LICENSE_TARGETS.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(decision["software"]["license"], "MIT")
        self.assertEqual(
            decision["software"]["github_repository"],
            "https://github.com/Zhangmochen-zmc/MethUnmix",
        )
        self.assertIsNone(decision["software"]["biocode_entry_id"])
        self.assertEqual(decision["references"]["license_identifier"], "CC-BY-NC-4.0")
        self.assertEqual(
            decision["references"]["license_selection_status"],
            "OWNER_SELECTED_CC_BY_NC_4_0",
        )
        self.assertFalse(
            decision["references"]["commercial_authorization_contact"]["contact_is_authorization"]
        )
        self.assertEqual(decision["public_actions"]["executed"], [])

        pending = json.loads(
            (root / "docs/publication/PENDING_OWNER_DECISIONS.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            pending["status"],
            "REFERENCE_LICENSE_AND_SOFTWARE_TARGETS_RECORDED_PUBLIC_ACTIONS_NOT_AUTHORIZED",
        )

    def test_new_preparation_status_is_accepted(self):
        from scripts import check_prepublication as checker

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = self._root(base)
            (root / "docs/publication/PENDING_OWNER_DECISIONS.json").write_text(
                json.dumps(
                    {
                        "status": "REFERENCE_LICENSE_AND_SOFTWARE_TARGETS_RECORDED_PUBLIC_ACTIONS_NOT_AUTHORIZED"
                    }
                ),
                encoding="utf-8",
            )
            wheel, sdist = self._artifacts(base)
            result = checker.check(root, wheel, sdist, "preparation")
            self.assertEqual(
                result["status"],
                "PASS_PREPARATION_PENDING_REFERENCE_RIGHTS_HOST_AND_PUBLIC_ACTIONS",
            )

    def test_reference_catalog_records_selected_license_without_enabling_download(self):
        root = Path(__file__).resolve().parents[1]
        catalog_path = root / "catalog/methunmix-reference-catalog-1.0.0.candidate.json"
        # This owner-review catalog is intentionally kept outside the portable
        # core sdist/wheel.  Source-tree validation covers it; extracted-sdist
        # validation must preserve that packaging boundary.
        if not catalog_path.is_file():
            self.skipTest("reference publication-review catalog is excluded from portable core artifacts")
        catalog = json.loads(
            catalog_path.read_text(encoding="utf-8")
        )
        self.assertEqual(len(catalog["targets"]), 33)
        for target in catalog["targets"]:
            self.assertEqual(target["distribution_status"], "USER_SUPPLIED_SUPPORTED")
            self.assertIsNone(target["source"])
            self.assertEqual(
                target["license_status"],
                "CC_BY_NC_4_0_SELECTED_PROJECT_CONTRIBUTIONS_WHOLE_ARCHIVE_RIGHTS_REVIEW_PENDING",
            )
            evidence = target["license_evidence"]
            self.assertEqual(evidence["standard_license"]["identifier"], "CC-BY-NC-4.0")
            self.assertEqual(
                evidence["whole_archive_status"],
                "NOT_PUBLIC_DOWNLOADABLE_PENDING_RIGHTS_AND_UPSTREAM_REVIEW",
            )
            self.assertFalse(evidence["commercial_authorization_contact"]["contact_is_authorization"])


if __name__ == "__main__":
    unittest.main()
