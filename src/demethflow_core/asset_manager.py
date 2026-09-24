"""Explicit, offline-first asset distribution for MethUnmix.

The 2.0 first-release contract is an immutable static catalog fetched only by
an explicit audit command. HTTPS protects the catalog in transit; each asset
is separately checked against its catalog SHA256 and byte length. This is not
TUF: it does not protect a fresh client from a compromised catalog host or
provide signed-metadata rollback protection. TUF is deliberately deferred.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import PROJECT_ROOT, version
from .errors import DeMethFlowError, InstallError
from .modules import ModuleStore, _store_lock
from .util import (
    assert_no_symlink_components,
    atomic_write_json,
    default_data_home,
    fsync_directory,
    prepare_data_home,
    sha256_file,
    strict_json_loads,
)


CATALOG_SCHEMA = "methunmix-static-catalog-v1"
CATALOG_FILE = PROJECT_ROOT / "assets" / "catalog.json"
MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 128 * 1024**3
MAX_DOWNLOAD_ATTEMPTS = 3
MAX_MIRRORS = 2
MIN_DOWNLOAD_FREE_SPACE = 16 * 1024**2
SAFE_ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _json(path: Path) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeMethFlowError(f"Cannot read asset metadata {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DeMethFlowError(f"Asset metadata must be a JSON object: {path}")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _validated_asset_url(value: Any, digest: str, asset_id: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(str(value or ""))
    if (
        parsed.scheme != "https" or not parsed.hostname or not parsed.netloc
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise DeMethFlowError(f"Public target requires an absolute HTTPS URL without credentials, query, or fragment: {asset_id}")
    if digest.lower() not in parsed.path.lower():
        raise DeMethFlowError(f"Public target URL must be content-addressed by its SHA256: {asset_id}")
    return parsed


def _hash_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _metadata_dir(path: Path | None = None) -> Path:
    if path is not None:
        return path.expanduser()
    return default_data_home() / ".methunmix" / "catalogs"


def _assert_no_symlink_components(path: Path) -> None:
    assert_no_symlink_components(path)


def _managed_directory(path: Path, *, create: bool) -> Path | None:
    _assert_no_symlink_components(path)
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DeMethFlowError(f"Cannot create MethUnmix managed directory {path}: {exc}") from exc
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_dir():
        raise DeMethFlowError(f"MethUnmix managed path is not a real directory: {path}")
    return path


def _store_state_dir(home: Path, *, create: bool) -> Path | None:
    return _managed_directory(home / ".methunmix", create=create)


def _download_dir(home: Path, *, create: bool) -> Path | None:
    state_dir = _store_state_dir(home, create=create)
    if state_dir is None:
        return None
    return _managed_directory(state_dir / "downloads", create=create)


def _catalog_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    configured = os.environ.get("METHUNMIX_CATALOG_PATH")
    return Path(configured).expanduser() if configured else CATALOG_FILE


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    selected = _catalog_path(path)
    if not selected.is_file():
        raise DeMethFlowError(f"Asset catalog is not installed: {selected}")
    try:
        catalog_bytes = selected.stat().st_size
    except OSError as exc:
        raise DeMethFlowError(f"Cannot inspect asset catalog {selected}: {exc}") from exc
    if catalog_bytes > MAX_CATALOG_BYTES:
        raise DeMethFlowError("Asset catalog exceeds the 8 MiB metadata size limit")
    payload = _json(selected)
    if payload.get("schema") != CATALOG_SCHEMA:
        raise DeMethFlowError(f"Unsupported asset catalog schema: {payload.get('schema')!r}")
    if payload.get("product") != "MethUnmix":
        raise DeMethFlowError("Asset catalog product must be MethUnmix")
    sequence = payload.get("catalog_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise DeMethFlowError("Static catalog requires a positive integer catalog_sequence")
    if not isinstance(payload.get("catalog_release"), str) or not payload["catalog_release"].strip():
        raise DeMethFlowError("Static catalog requires a non-empty catalog_release")
    if payload.get("trust_model") != "HTTPS_STATIC_CATALOG_SHA256":
        raise DeMethFlowError("Static catalog trust_model must be HTTPS_STATIC_CATALOG_SHA256")
    targets = payload.get("targets", [])
    if not isinstance(targets, list):
        raise DeMethFlowError("Asset catalog targets must be a list")
    if not targets:
        empty_policy = payload.get("empty_catalog_policy")
        if (
            not isinstance(empty_policy, dict)
            or empty_policy.get("allowed") is not True
            or empty_policy.get("fallback") != "USER_SUPPLIED_SUPPORTED_OR_BLOCKED"
            or not isinstance(empty_policy.get("reason"), str)
            or not empty_policy["reason"].strip()
        ):
            raise DeMethFlowError(
                "An empty asset catalog requires an explicit empty_catalog_policy "
                "allowing USER_SUPPLIED_SUPPORTED_OR_BLOCKED assets"
            )
    seen_ids: set[str] = set()
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("asset_id"), str):
            raise DeMethFlowError("Every asset catalog target requires asset_id")
        asset_id = target["asset_id"]
        if not SAFE_ASSET_ID.fullmatch(asset_id):
            raise DeMethFlowError(f"Invalid asset_id in static catalog: {asset_id!r}")
        if asset_id in seen_ids:
            raise DeMethFlowError(f"Duplicate asset catalog target: {asset_id}")
        seen_ids.add(asset_id)
        if target.get("distribution_status") not in {
            "PUBLIC_BUNDLED", "PUBLIC_DOWNLOADABLE", "USER_SUPPLIED_SUPPORTED",
            "INTERNAL_VALIDATED", "UNAVAILABLE", "QUARANTINED", "LOCAL_ONLY",
        }:
            raise DeMethFlowError(f"Invalid distribution_status for {target.get('asset_id')!r}")
        if target.get("distribution_status") == "PUBLIC_DOWNLOADABLE":
            if not isinstance(target.get("version"), str) or not target["version"].strip():
                raise DeMethFlowError(f"Public target requires an immutable asset version: {asset_id}")
            digest = str(target.get("sha256", ""))
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
                raise DeMethFlowError(f"Public target has invalid SHA256: {asset_id}")
            try:
                length = target["length"]
                if isinstance(length, bool) or not isinstance(length, int):
                    raise ValueError("byte length must be a JSON integer")
            except (KeyError, TypeError, ValueError) as exc:
                raise DeMethFlowError(f"Public target has no valid length: {asset_id}") from exc
            if length < 1 or length > MAX_DOWNLOAD_BYTES:
                raise DeMethFlowError(f"Public target length is outside allowed range: {asset_id}")
            _validated_asset_url(target.get("url"), digest, asset_id)
            mirrors = target.get("mirrors", [])
            if not isinstance(mirrors, list) or len(mirrors) > MAX_MIRRORS:
                raise DeMethFlowError(f"Public target mirrors must be a list of at most {MAX_MIRRORS} URLs: {asset_id}")
            mirror_urls = []
            for mirror in mirrors:
                if not isinstance(mirror, str):
                    raise DeMethFlowError(f"Public target mirror must be a URL string: {asset_id}")
                _validated_asset_url(mirror, digest, asset_id)
                mirror_urls.append(mirror)
            if len(set(mirror_urls)) != len(mirror_urls) or target.get("url") in mirror_urls:
                raise DeMethFlowError(f"Public target contains duplicate mirror URLs: {asset_id}")
            if target.get("immutable_object") is not True:
                raise DeMethFlowError(f"Public target must declare immutable_object=true: {asset_id}")
            evidence = target.get("license_evidence")
            if target.get("license_status") != "LICENSE_APPROVED" or not isinstance(evidence, (str, dict)) or not evidence:
                raise DeMethFlowError(f"Public target lacks explicit redistribution approval evidence: {asset_id}")
        if target.get("revoked") is True and not str(target.get("revocation_reason", "")).strip():
            raise DeMethFlowError(f"Revoked target has no revocation_reason: {asset_id}")
    return payload


def list_assets(*, json_output: bool = False) -> str:
    catalog, catalog_origin = _current_catalog()
    installed = ModuleStore().list()
    result = {
        "product": "MethUnmix",
        "core_version": version(),
        "catalog_schema": catalog["schema"],
        "catalog_trust_model": catalog["trust_model"],
        "catalog_origin": catalog_origin,
        "targets": catalog.get("targets", []),
        "installed_modules": [module.payload for module in installed],
        "network_accessed": False,
    }
    return json.dumps(result if json_output else result["targets"], ensure_ascii=False, indent=2, sort_keys=True)


def plan_assets(*, json_output: bool = False) -> str:
    catalog, _catalog_origin = _current_catalog()
    targets = [
        {
            "asset_id": item["asset_id"],
            "version": item.get("version"),
            "distribution_status": item.get("distribution_status"),
            "platform": item.get("platform"),
            "requires_user_asset": item.get("distribution_status") == "USER_SUPPLIED_SUPPORTED",
            "revoked": bool(item.get("revoked", False)),
        }
        for item in catalog.get("targets", [])
    ]
    return json.dumps(targets, ensure_ascii=False, indent=2, sort_keys=True) if json_output else json.dumps(targets, ensure_ascii=False, indent=2)


def verify_assets(*, module_id: str | None = None, json_output: bool = False) -> int:
    store = ModuleStore()
    rows = []
    for module in store.list():
        if module_id and module.module_id != module_id:
            continue
        errors = store.verify(module)
        rows.append({"selector": module.selector, "ok": not errors, "errors": errors})
    payload = {"ok": all(row["ok"] for row in rows), "modules": rows, "network_accessed": False}
    print(json.dumps(payload, ensure_ascii=False, indent=2) if json_output else json.dumps(payload["modules"], ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 2


def import_asset(archive: Path) -> int:
    module, created = ModuleStore().install(archive)
    print(json.dumps({"selector": module.selector, "installed": created, "path": str(module.install_root)}, ensure_ascii=False, indent=2))
    return 0


def _current_catalog(metadata_dir: Path | None = None) -> tuple[dict[str, Any], str]:
    directory = _metadata_dir(metadata_dir)
    _assert_no_symlink_components(directory)
    state_path = directory / "audit_state.json"
    if state_path.is_symlink():
        raise DeMethFlowError(f"Refusing symlinked catalog audit state: {state_path}")
    if not state_path.is_file():
        return load_catalog(), "CORE_BUNDLED"
    state = _json(state_path)
    sequence = state.get("catalog_sequence")
    digest = state.get("catalog_sha256")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise DeMethFlowError("Cached static catalog audit state is malformed")
    snapshot = directory / "snapshots" / f"{sequence}-{digest}.json"
    _assert_no_symlink_components(snapshot)
    if not snapshot.is_file() or sha256_file(snapshot) != digest:
        raise DeMethFlowError("Cached static catalog snapshot is missing or its SHA256 changed")
    catalog = load_catalog(snapshot)
    if catalog["catalog_sequence"] != sequence:
        raise DeMethFlowError("Cached static catalog sequence does not match audit state")
    if catalog["catalog_release"] != state.get("catalog_release"):
        raise DeMethFlowError("Cached static catalog release does not match audit state")
    return catalog, "HTTPS_CACHED_STATIC_CATALOG"


def audit_online(catalog_url: str, *, metadata_dir: Path | None = None, timeout: int = 30) -> int:
    parsed = urllib.parse.urlparse(catalog_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DeMethFlowError("Static catalog URL must be an absolute HTTPS URL without credentials or fragment")
    home = prepare_data_home()
    state_dir = _store_state_dir(home, create=True)
    assert state_dir is not None
    with _store_lock(state_dir / "store.lock", operation="catalog-audit"):
        return _audit_online_locked(catalog_url, parsed, target_dir=_metadata_dir(metadata_dir), timeout=timeout)


def _audit_online_locked(catalog_url: str, parsed: urllib.parse.ParseResult, *, target_dir: Path, timeout: int) -> int:
    _assert_no_symlink_components(target_dir)
    _managed_directory(target_dir, create=True)
    snapshots_dir = target_dir / "snapshots"
    _managed_directory(snapshots_dir, create=True)
    request = urllib.request.Request(catalog_url, headers={"User-Agent": "MethUnmix-static-catalog-audit"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.netloc.lower() != parsed.netloc.lower():
                raise DeMethFlowError("Static catalog endpoint redirected away from its original HTTPS host")
            data = response.read(MAX_CATALOG_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise DeMethFlowError(f"Cannot download static catalog over HTTPS: {exc}") from exc
    if len(data) > MAX_CATALOG_BYTES:
        raise DeMethFlowError("Static catalog exceeds the 8 MiB metadata size limit")
    staging = Path(tempfile.mkdtemp(prefix="catalog-audit-", dir=target_dir))
    try:
        staged_catalog = staging / "catalog.json"
        staged_catalog.write_bytes(data)
        catalog = load_catalog(staged_catalog)
        if catalog["catalog_release"] not in parsed.path:
            raise DeMethFlowError("Immutable catalog URL path must contain the catalog_release identifier")
        catalog_digest = hashlib.sha256(data).hexdigest()
        state_path = target_dir / "audit_state.json"
        if state_path.is_symlink():
            raise DeMethFlowError(f"Refusing symlinked catalog audit state: {state_path}")
        previous = _json(state_path) if state_path.is_file() else None
        if previous:
            previous_sequence = previous.get("catalog_sequence")
            previous_digest = previous.get("catalog_sha256")
            if isinstance(previous_sequence, int) and not isinstance(previous_sequence, bool) and catalog["catalog_sequence"] < previous_sequence:
                raise DeMethFlowError("Static catalog rollback detected: downloaded sequence is older than the last audited sequence")
            if catalog["catalog_sequence"] == previous_sequence and catalog_digest != previous_digest:
                raise DeMethFlowError("Immutable static catalog changed without increasing catalog_sequence")
        snapshot = snapshots_dir / f"{catalog['catalog_sequence']}-{catalog_digest}.json"
        if snapshot.is_symlink():
            raise DeMethFlowError(f"Refusing symlinked immutable catalog snapshot: {snapshot}")
        if snapshot.exists():
            if sha256_file(snapshot) != catalog_digest:
                raise DeMethFlowError("Existing immutable catalog snapshot is corrupted")
        else:
            os.replace(staged_catalog, snapshot)
            fsync_directory(snapshots_dir)
        state = {
            "schema": "methunmix-static-catalog-audit-v1",
            "catalog_release": catalog["catalog_release"],
            "catalog_sequence": catalog["catalog_sequence"],
            "catalog_sha256": catalog_digest,
            "catalog_url": catalog_url,
            "transport": "HTTPS",
            "metadata_signature": "NOT_USED_TUF_DEFERRED",
            "audited_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "rollback_protection": "local_monotonic_sequence_only; not a cryptographic freshness guarantee",
        }
        atomic_write_json(state_path, state)
        print(json.dumps({"status": "PASS_STATIC_CATALOG_VALIDATED", **state}, ensure_ascii=False, indent=2))
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def fetch_asset(asset_id: str, *, metadata_dir: Path | None = None, timeout: int = 60) -> int:
    home = prepare_data_home()
    state_dir = _store_state_dir(home, create=True)
    assert state_dir is not None
    with _store_lock(state_dir / "store.lock", operation=f"asset-fetch:{asset_id}"):
        return _fetch_asset_locked(asset_id, metadata_dir=metadata_dir, timeout=timeout, home=home)


def _fetch_asset_locked(asset_id: str, *, metadata_dir: Path | None, timeout: int, home: Path) -> int:
    catalog, catalog_origin = _current_catalog(metadata_dir)
    target = next((item for item in catalog.get("targets", []) if item.get("asset_id") == asset_id), None)
    if not isinstance(target, dict):
        raise DeMethFlowError(f"Asset is not present in the selected static catalog: {asset_id}")
    if target.get("distribution_status") != "PUBLIC_DOWNLOADABLE":
        raise DeMethFlowError(f"Asset is not approved for public download: {asset_id} ({target.get('distribution_status')})")
    if target.get("revoked"):
        raise DeMethFlowError(f"Asset is revoked: {asset_id}: {target.get('revocation_reason', 'unspecified')}")
    url = str(target["url"])
    download_urls = [url, *target.get("mirrors", [])]
    expected = str(target["sha256"]).lower()
    length = int(target["length"])
    download_dir = _download_dir(home, create=True)
    assert download_dir is not None
    # Content-addressed cache: a selector never causes duplicate bytes to be
    # downloaded under a mutable asset-id filename.
    final = download_dir / expected.lower()
    if final.is_symlink():
        raise DeMethFlowError(f"Refusing symlinked content-addressed cache entry: {final}")
    if final.exists():
        final_stat = final.lstat()
        if not stat.S_ISREG(final_stat.st_mode) or final_stat.st_nlink != 1:
            raise DeMethFlowError(f"Refusing non-regular or hard-linked content-addressed cache entry: {final}")
        if final_stat.st_size != length or sha256_file(final) != expected:
            raise DeMethFlowError(f"Cached content-addressed asset is corrupt; remove it with asset gc after review: {final}")
        print(json.dumps({"asset_id": asset_id, "path": str(final), "sha256": expected, "catalog_release": catalog["catalog_release"], "catalog_origin": catalog_origin, "network_accessed": False, "cached": True}, ensure_ascii=False, indent=2))
        return 0
    try:
        free_bytes = shutil.disk_usage(download_dir).free
    except OSError as exc:
        raise DeMethFlowError(f"Cannot inspect free space for asset download: {exc}") from exc
    required_bytes = length + MIN_DOWNLOAD_FREE_SPACE
    if free_bytes < required_bytes:
        raise DeMethFlowError(f"Insufficient free space for asset {asset_id}: need {required_bytes} bytes including reserve; have {free_bytes}")
    last_error: Exception | None = None
    for attempt in range(MAX_DOWNLOAD_ATTEMPTS):
        selected_url = download_urls[attempt % len(download_urls)]
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{expected.lower()}.", suffix=".part", dir=download_dir,
        )
        destination = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb+") as handle:
                request = urllib.request.Request(selected_url, headers={"User-Agent": "MethUnmix-asset-fetch"})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    original_url = urllib.parse.urlparse(selected_url)
                    final_url = urllib.parse.urlparse(response.geturl())
                    if final_url.scheme != "https" or final_url.netloc.lower() != original_url.netloc.lower():
                        raise DeMethFlowError("Asset endpoint redirected away from its original HTTPS host")
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None and int(content_length) != length:
                        raise DeMethFlowError(f"HTTP Content-Length does not match catalog length for {asset_id}")
                    copied = 0
                    while True:
                        chunk = response.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > MAX_DOWNLOAD_BYTES or copied > length:
                            raise DeMethFlowError("Asset exceeds the catalog download size limit")
                        handle.write(chunk)
                if copied != length:
                    raise OSError(f"Incomplete asset transfer: expected {length} bytes, received {copied}")
                handle.flush()
                os.fsync(handle.fileno())
                handle.seek(0)
                actual = _hash_stream(handle)
                descriptor_stat = os.fstat(handle.fileno())
                path_stat = destination.lstat()
                if (
                    not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1
                    or not os.path.samestat(descriptor_stat, path_stat)
                ):
                    raise DeMethFlowError(f"Temporary asset file changed identity while downloading: {destination}")
                if actual != expected:
                    raise DeMethFlowError(f"Asset SHA256 mismatch for {asset_id}")
            try:
                # Publish a complete file atomically, without overwriting an existing name.
                os.link(destination, final)
            except FileExistsError as exc:
                raise DeMethFlowError(f"Refusing to overwrite an existing content-addressed asset: {final}") from exc
            destination.unlink()
            fsync_directory(download_dir)
            print(json.dumps({"asset_id": asset_id, "path": str(final), "sha256": expected, "catalog_release": catalog["catalog_release"], "catalog_origin": catalog_origin, "network_accessed": True, "source_url": selected_url}, ensure_ascii=False, indent=2))
            return 0
        except DeMethFlowError:
            destination.unlink(missing_ok=True)
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            last_error = exc
            destination.unlink(missing_ok=True)
            if attempt + 1 == MAX_DOWNLOAD_ATTEMPTS:
                raise DeMethFlowError(f"Cannot download asset {asset_id} after {MAX_DOWNLOAD_ATTEMPTS} attempts: {exc}") from exc
    raise DeMethFlowError(f"Cannot download asset {asset_id}: {last_error or 'all download attempts failed'}")


def gc_assets(*, dry_run: bool = False) -> int:
    home = default_data_home()
    download_dir = _download_dir(home, create=False)
    if download_dir is None:
        print(json.dumps({"dry_run": dry_run, "candidates": [], "removed": [], "network_accessed": False}, ensure_ascii=False, indent=2))
        return 0
    state_dir = home / ".methunmix"
    if dry_run:
        return _gc_assets_locked(home, download_dir, dry_run=True)
    with _store_lock(state_dir / "store.lock", operation="asset-gc"):
        return _gc_assets_locked(home, download_dir, dry_run=False)


def _gc_assets_locked(home: Path, download_dir: Path, *, dry_run: bool) -> int:
    candidates = sorted(path for path in download_dir.glob("*") if path.is_file() or path.is_symlink())
    protected: dict[str, str] = {}
    for module in ModuleStore(home).list():
        digest = module.payload.get("archive_sha256")
        if isinstance(digest, str):
            protected[digest.lower()] = f"installed module {module.selector}"
    rows = []
    for path in candidates:
        reason = "symlink cache entry; left untouched" if path.is_symlink() else protected.get(path.name.lower())
        if path.name.endswith(".part"):
            reason = reason or "incomplete download"
        elif not re.fullmatch(r"[0-9a-f]{64}", path.name.lower()):
            reason = reason or "unrecognized cache entry; left untouched"
        rows.append({"path": str(path), "protected": bool(reason), "reason": reason})
    payload = {"dry_run": dry_run, "candidates": rows, "removed": [], "network_accessed": False}
    if not dry_run:
        for row in rows:
            if row["protected"]:
                continue
            path = Path(row["path"])
            path.unlink()
            payload["removed"].append(str(path))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
