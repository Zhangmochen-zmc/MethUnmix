from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from demethflow_core.asset_manager import CATALOG_SCHEMA, fetch_asset_record, fetch_reference, list_reference_catalog
from demethflow_core.errors import DeMethFlowError
from demethflow_core.manifest import load_reference
from demethflow_core.packaging import export_reference


class _Response:
    def __init__(self, payload: bytes, url: str):
        self._payload = payload
        self._url = url
        self._sent = False
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self, _size: int = -1):
        if self._sent:
            return b""
        self._sent = True
        return self._payload


class _PartialResponse:
    def __init__(self, payload: bytes, url: str, *, status: int, headers: dict[str, str]):
        self._payload = payload
        self._url = url
        self.status = status
        self.headers = headers
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self, size: int = -1):
        if size < 0:
            size = len(self._payload) - self._offset
        chunk = self._payload[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


def _fixture_archive(root: Path) -> tuple[Path, str, str]:
    reference_root = root / "fixture-reference"
    reference_root.mkdir()
    manifest = {
        "schema": "demethflow-reference-v1",
        "reference_id": "fixture-reference",
        "version": "1.0.0",
        "scenario_id": "fixture",
        "source_platform": "450k",
        "analysis_contract": "array_450k_cpg",
        "compatible_input_routes": ["450k"],
        "genome_build": "not_applicable",
        "status": "READY",
        "cell_types": [{"cell_type_id": "cell", "display_name": "Cell"}],
        "tool_capabilities": {
            "EDec": {
                "status": "READY",
                "artifacts": {},
                "runtime_modules": ["fixture-runtime"],
            }
        },
        "compatibility": {"core_api": "2.0", "reference_schema": "1.0", "runtime_api": "1.0"},
    }
    manifest_path = reference_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    bundle = load_reference(manifest_path, verify_files=True)
    archive, _ = export_reference(bundle, root / "exports")
    return archive, bundle.selector, bundle.content_digest(verify_files=True)


class ReferenceFetchTests(unittest.TestCase):
    def test_asset_fetch_resumes_a_short_https_transfer_with_valid_range(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-reference-resume-") as tmp:
            root = Path(tmp)
            payload = b"0123456789"
            digest = hashlib.sha256(payload).hexdigest()
            url = f"https://assets.example/objects/sha256/{digest}/fixture.tar.gz"
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schema": CATALOG_SCHEMA,
                "product": "MethUnmix",
                "catalog_release": "fixture-static.2",
                "catalog_sequence": 2,
                "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [{
                    "asset_id": "reference.fixture-reference",
                    "version": "1.0.0",
                    "module_type": "reference",
                    "distribution_status": "PUBLIC_DOWNLOADABLE",
                    "license_status": "LICENSE_APPROVED",
                    "license_evidence": {"status": "LICENSE_APPROVED", "evidence": "fixture"},
                    "selector": "fixture-reference@1.0.0",
                    "reference_id": "fixture-reference",
                    "content_digest": "a" * 64,
                    "url": url,
                    "length": len(payload),
                    "sha256": digest,
                    "immutable_object": True,
                    "revoked": False,
                }],
            }), encoding="utf-8")
            seen_ranges: list[str | None] = []

            def short_then_range(request, timeout):
                seen_ranges.append(request.get_header("Range"))
                if len(seen_ranges) == 1:
                    return _PartialResponse(payload[:5], url, status=200, headers={"Content-Length": str(len(payload))})
                return _PartialResponse(
                    payload[5:], url, status=206,
                    headers={"Content-Length": str(len(payload) - 5), "Content-Range": "bytes 5-9/10"},
                )

            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(root / "home"),
                "METHUNMIX_CATALOG_PATH": str(catalog_path),
            }, clear=False), patch(
                "demethflow_core.asset_manager.urllib.request.urlopen", side_effect=short_then_range,
            ):
                result = fetch_asset_record("reference.fixture-reference")
            self.assertEqual(seen_ranges, [None, "bytes=5-"])
            self.assertEqual(result["resumed_bytes"], 5)
            self.assertEqual(Path(result["path"]).read_bytes(), payload)

    def test_asset_fetch_publishes_a_complete_interrupted_partial_without_network(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-reference-recover-") as tmp:
            root = Path(tmp)
            payload = b"already complete"
            digest = hashlib.sha256(payload).hexdigest()
            url = f"https://assets.example/objects/sha256/{digest}/fixture.tar.gz"
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schema": CATALOG_SCHEMA,
                "product": "MethUnmix",
                "catalog_release": "fixture-static.2",
                "catalog_sequence": 2,
                "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [{
                    "asset_id": "reference.fixture-reference",
                    "version": "1.0.0",
                    "module_type": "reference",
                    "distribution_status": "PUBLIC_DOWNLOADABLE",
                    "license_status": "LICENSE_APPROVED",
                    "license_evidence": {"status": "LICENSE_APPROVED", "evidence": "fixture"},
                    "selector": "fixture-reference@1.0.0",
                    "reference_id": "fixture-reference",
                    "content_digest": "a" * 64,
                    "url": url,
                    "length": len(payload),
                    "sha256": digest,
                    "immutable_object": True,
                    "revoked": False,
                }],
            }), encoding="utf-8")
            downloads = root / "home" / ".methunmix" / "downloads"
            downloads.mkdir(parents=True)
            (downloads / f".{digest}.part").write_bytes(payload)
            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(root / "home"),
                "METHUNMIX_CATALOG_PATH": str(catalog_path),
            }, clear=False), patch("demethflow_core.asset_manager.urllib.request.urlopen") as urlopen:
                result = fetch_asset_record("reference.fixture-reference")
            urlopen.assert_not_called()
            self.assertTrue(result["recovered_complete_partial"])
            self.assertEqual(result["resumed_bytes"], len(payload))
            self.assertEqual(Path(result["path"]).read_bytes(), payload)

    def test_asset_fetch_restarts_safely_when_https_origin_ignores_range(self):
        """A 200 response to Range must replace, not append to, partial bytes."""
        with tempfile.TemporaryDirectory(prefix="methunmix-reference-no-range-") as tmp:
            root = Path(tmp)
            payload = b"0123456789"
            digest = hashlib.sha256(payload).hexdigest()
            url = f"https://assets.example/objects/sha256/{digest}/fixture.tar.gz"
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schema": CATALOG_SCHEMA, "product": "MethUnmix", "catalog_release": "fixture-static.2",
                "catalog_sequence": 2, "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [{
                    "asset_id": "reference.fixture-reference", "version": "1.0.0", "module_type": "reference",
                    "distribution_status": "PUBLIC_DOWNLOADABLE", "license_status": "LICENSE_APPROVED",
                    "license_evidence": {"status": "LICENSE_APPROVED", "evidence": "fixture"},
                    "selector": "fixture-reference@1.0.0", "reference_id": "fixture-reference",
                    "content_digest": "a" * 64, "url": url, "length": len(payload), "sha256": digest,
                    "immutable_object": True, "revoked": False,
                }],
            }), encoding="utf-8")
            downloads = root / "home" / ".methunmix" / "downloads"
            downloads.mkdir(parents=True)
            (downloads / f".{digest}.part").write_bytes(payload[:5])
            seen_ranges: list[str | None] = []

            def no_range(request, timeout):
                seen_ranges.append(request.get_header("Range"))
                return _PartialResponse(payload, url, status=200, headers={"Content-Length": str(len(payload))})

            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(root / "home"), "METHUNMIX_CATALOG_PATH": str(catalog_path),
            }, clear=False), patch("demethflow_core.asset_manager.urllib.request.urlopen", side_effect=no_range):
                result = fetch_asset_record("reference.fixture-reference")
            self.assertEqual(seen_ranges, ["bytes=5-"])
            self.assertEqual(result["resumed_bytes"], 0)
            self.assertTrue(result["restarted_from_zero"])
            self.assertEqual(Path(result["path"]).read_bytes(), payload)

    def test_asset_fetch_rejects_changed_remote_object_and_never_publishes_it(self):
        """A matching-length but different remote suffix cannot complete a cached prefix."""
        with tempfile.TemporaryDirectory(prefix="methunmix-reference-remote-change-") as tmp:
            root = Path(tmp)
            expected = b"0123456789"
            changed = b"01234abcde"
            digest = hashlib.sha256(expected).hexdigest()
            url = f"https://assets.example/objects/sha256/{digest}/fixture.tar.gz"
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps({
                "schema": CATALOG_SCHEMA, "product": "MethUnmix", "catalog_release": "fixture-static.2",
                "catalog_sequence": 2, "trust_model": "HTTPS_STATIC_CATALOG_SHA256", "targets": [{
                    "asset_id": "reference.fixture-reference", "version": "1.0.0", "module_type": "reference",
                    "distribution_status": "PUBLIC_DOWNLOADABLE", "license_status": "LICENSE_APPROVED",
                    "license_evidence": {"status": "LICENSE_APPROVED", "evidence": "fixture"},
                    "selector": "fixture-reference@1.0.0", "reference_id": "fixture-reference",
                    "content_digest": "a" * 64, "url": url, "length": len(expected), "sha256": digest,
                    "immutable_object": True, "revoked": False,
                }],
            }), encoding="utf-8")
            downloads = root / "home" / ".methunmix" / "downloads"
            downloads.mkdir(parents=True)
            (downloads / f".{digest}.part").write_bytes(expected[:5])
            response = _PartialResponse(
                changed[5:], url, status=206,
                headers={"Content-Length": "5", "Content-Range": "bytes 5-9/10"},
            )
            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(root / "home"), "METHUNMIX_CATALOG_PATH": str(catalog_path),
            }, clear=False), patch("demethflow_core.asset_manager.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(DeMethFlowError, "SHA256 mismatch"):
                    fetch_asset_record("reference.fixture-reference")
            self.assertFalse((downloads / digest).exists())
            self.assertFalse((downloads / f".{digest}.part").exists())

    def test_fetch_and_install_validates_exact_identity_and_reports_missing_runtime(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-reference-fetch-") as tmp:
            root = Path(tmp)
            archive, selector, content_digest = _fixture_archive(root)
            payload = archive.read_bytes()
            archive_digest = hashlib.sha256(payload).hexdigest()
            url = f"https://assets.example/objects/sha256/{archive_digest}/{archive.name}"
            catalog = {
                "schema": CATALOG_SCHEMA,
                "product": "MethUnmix",
                "catalog_release": "fixture-static.2",
                "catalog_sequence": 2,
                "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [{
                    "asset_id": "reference.fixture-reference",
                    "version": "1.0.0",
                    "module_type": "reference",
                    "distribution_status": "PUBLIC_DOWNLOADABLE",
                    "license_status": "LICENSE_APPROVED",
                    "license_evidence": {"status": "LICENSE_APPROVED", "evidence": "fixture"},
                    "selector": selector,
                    "reference_id": "fixture-reference",
                    "content_digest": content_digest,
                    "url": url,
                    "length": len(payload),
                    "sha256": archive_digest,
                    "immutable_object": True,
                    "revoked": False,
                }],
            }
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(root / "home"),
                "METHUNMIX_CATALOG_PATH": str(catalog_path),
            }, clear=False), patch(
                "demethflow_core.asset_manager.urllib.request.urlopen",
                return_value=_Response(payload, url),
            ):
                result = fetch_reference(selector, install=True)
            self.assertTrue(result["installed"])
            self.assertEqual(result["reference_validation"], "PASS")
            self.assertEqual(result["missing_modules"], ["fixture-runtime"])
            self.assertTrue(Path(result["installed_path"]).is_dir())
            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(root / "home"),
                "METHUNMIX_CATALOG_PATH": str(catalog_path),
            }, clear=False):
                available = list_reference_catalog()
            self.assertTrue(available["references"][0]["installed"])

    def test_unapproved_reference_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-reference-fetch-") as tmp:
            root = Path(tmp)
            selector = "fixture-reference@1.0.0"
            digest = "a" * 64
            catalog = {
                "schema": CATALOG_SCHEMA,
                "product": "MethUnmix",
                "catalog_release": "fixture-static.1",
                "catalog_sequence": 1,
                "trust_model": "HTTPS_STATIC_CATALOG_SHA256",
                "targets": [{
                    "asset_id": "reference.fixture-reference",
                    "version": "1.0.0",
                    "module_type": "reference",
                    "distribution_status": "USER_SUPPLIED_SUPPORTED",
                    "license_status": "USER_SUPPLIED_SUPPORTED",
                    "license_evidence": {"status": "OWNER_DECISION_PENDING"},
                    "selector": selector,
                    "reference_id": "fixture-reference",
                    "content_digest": "b" * 64,
                    "url": f"https://assets.example/objects/sha256/{digest}/fixture.tar.gz",
                    "length": 1,
                    "sha256": digest,
                    "immutable_object": True,
                    "revoked": False,
                }],
            }
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with patch.dict(os.environ, {
                "METHUNMIX_HOME": str(root / "home"),
                "METHUNMIX_CATALOG_PATH": str(catalog_path),
            }, clear=False), patch("demethflow_core.asset_manager.urllib.request.urlopen") as urlopen:
                with self.assertRaisesRegex(DeMethFlowError, "not approved for public download"):
                    fetch_reference(selector, install=True)
                urlopen.assert_not_called()
                available = list_reference_catalog()
            self.assertEqual(available["references"][0]["access"], "USER_SUPPLIED_ARCHIVE_REQUIRED")
            self.assertFalse(available["network_accessed"])

    def test_unversioned_selector_is_rejected_before_catalog_or_network(self):
        with patch("demethflow_core.asset_manager.urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(DeMethFlowError, "exact selector"):
                fetch_reference("fixture-reference")
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
