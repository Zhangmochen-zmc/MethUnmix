"""Explicit, offline-first asset distribution for MethUnMix.

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
FULL_SELECTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}@[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


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
            raise DeMethFlowError(f"Cannot create MethUnMix managed directory {path}: {exc}") from exc
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_dir():
        raise DeMethFlowError(f"MethUnMix managed path is not a real directory: {path}")
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
        # WP-03 consumes these optional fields to derive an exact installation
        # plan.  They are deliberately optional so frozen legacy catalogs stay
        # readable; absence is reported by the planner rather than guessed.
        for field in (
            "dependencies", "tool_dependencies", "profile_dependencies",
            "dependency_requirements", "compatibility", "tool_capabilities",
        ):
            if field not in target:
                continue
            value = target[field]
            if field == "dependencies" and not isinstance(value, list):
                raise DeMethFlowError(f"Catalog {field} must be a list for {asset_id}")
            if field in {
                "tool_dependencies", "profile_dependencies", "dependency_requirements",
                "compatibility", "tool_capabilities",
            } and not isinstance(value, dict):
                raise DeMethFlowError(f"Catalog {field} must be an object for {asset_id}")
        installed_bytes = target.get("installed_bytes")
        if installed_bytes is not None and (isinstance(installed_bytes, bool) or not isinstance(installed_bytes, int) or installed_bytes < 0):
            raise DeMethFlowError(f"Catalog installed_bytes must be a non-negative integer when present: {asset_id}")
        routes = target.get("compatible_input_routes")
        if routes is not None and (
            not isinstance(routes, list)
            or not routes
            or any(item not in {"450k", "epic", "wgbs"} for item in routes)
            or len(set(routes)) != len(routes)
        ):
            raise DeMethFlowError(f"Catalog compatible_input_routes must be a non-empty unique platform list: {asset_id}")
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


def list_reference_catalog(*, metadata_dir: Path | None = None) -> dict[str, Any]:
    """Describe locally known downloadable and user-supplied references.

    This is deliberately offline.  A user obtains a newer record only through
    the explicit ``asset audit --online`` command, then this query and
    ``reference fetch`` read the same audited snapshot.
    """
    catalog, catalog_origin = _current_catalog(metadata_dir)
    # Reference catalog selectors are ``reference_id@version`` while generic
    # module selectors are ``module_id@version`` (where a reference module id
    # has the ``reference.`` prefix).  Compare the catalog selector against
    # the reference manifest identity recorded in the installed module rather
    # than the generic module selector, otherwise a successful fetch/install
    # is misleadingly displayed as not installed.
    installed = {
        f"{module.payload.get('reference_id')}@{module.version}"
        for module in ModuleStore().list()
        if module.payload.get("module_type") == "reference"
        and isinstance(module.payload.get("reference_id"), str)
    }
    rows: list[dict[str, Any]] = []
    for target in catalog.get("targets", []):
        if not isinstance(target, dict) or target.get("module_type") != "reference":
            continue
        selector = target.get("selector")
        distribution = target.get("distribution_status")
        revoked = bool(target.get("revoked", False))
        if revoked:
            access = "REVOKED"
        elif distribution == "PUBLIC_DOWNLOADABLE":
            access = "AUTO_DOWNLOAD_AVAILABLE"
        elif distribution == "USER_SUPPLIED_SUPPORTED":
            access = "USER_SUPPLIED_ARCHIVE_REQUIRED"
        else:
            access = "NOT_DOWNLOADABLE"
        rows.append({
            "selector": selector,
            "asset_id": target.get("asset_id"),
            "version": target.get("version"),
            "scenario": target.get("scenario"),
            "source_platform": target.get("source_platform"),
            "analysis_contract": target.get("analysis_contract"),
            "genome_build": target.get("genome_build"),
            "distribution_status": distribution,
            "license_status": target.get("license_status"),
            "revoked": revoked,
            "access": access,
            "installed": selector in installed,
        })
    return {
        "catalog_release": catalog["catalog_release"],
        "catalog_sequence": catalog["catalog_sequence"],
        "catalog_origin": catalog_origin,
        "network_accessed": False,
        "references": sorted(rows, key=lambda item: str(item["selector"])),
    }


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
    installed = store.list()
    rows_to_verify = [module for module in installed if module.module_id == module_id or module.selector == module_id] if module_id else installed
    rows = []
    for module in rows_to_verify:
        errors = store.verify(module)
        rows.append({"selector": module.selector, "ok": not errors, "errors": errors})
    if module_id and not rows:
        payload = {"ok": False, "status": "TARGET_NOT_FOUND", "target": module_id,
                   "errors": [f"installed module target not found: {module_id}"], "modules": [], "network_accessed": False}
    elif not module_id and not rows:
        payload = {"ok": False, "status": "NO_VERIFIABLE_MODULES",
                   "errors": ["no installed modules are available to verify"], "modules": [], "network_accessed": False}
    else:
        ok = all(row["ok"] for row in rows)
        payload = {"ok": ok, "status": "VERIFIED" if ok else "INTEGRITY_FAILURE", "modules": rows, "network_accessed": False}
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload["ok"]:
        print(json.dumps(payload["modules"], ensure_ascii=False, indent=2))
    else:
        print(f"ERROR\t{payload['status']}\t" + "; ".join(payload.get("errors", [])))
        for row in payload["modules"]:
            print(f"FAIL\t{row['selector']}\t" + "; ".join(row["errors"]))
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


def fetch_asset_record(asset_id: str, *, metadata_dir: Path | None = None, timeout: int = 60) -> dict[str, Any]:
    """Fetch one approved catalog target and return its verified cache record."""
    home = prepare_data_home()
    state_dir = _store_state_dir(home, create=True)
    assert state_dir is not None
    with _store_lock(state_dir / "store.lock", operation=f"asset-fetch:{asset_id}"):
        return _fetch_asset_locked(asset_id, metadata_dir=metadata_dir, timeout=timeout, home=home)


def fetch_asset(asset_id: str, *, metadata_dir: Path | None = None, timeout: int = 60) -> int:
    print(json.dumps(fetch_asset_record(asset_id, metadata_dir=metadata_dir, timeout=timeout), ensure_ascii=False, indent=2))
    return 0


def _fetch_asset_locked(asset_id: str, *, metadata_dir: Path | None, timeout: int, home: Path) -> dict[str, Any]:
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
        return {"asset_id": asset_id, "path": str(final), "sha256": expected, "catalog_release": catalog["catalog_release"], "catalog_origin": catalog_origin, "network_accessed": False, "cached": True}
    try:
        free_bytes = shutil.disk_usage(download_dir).free
    except OSError as exc:
        raise DeMethFlowError(f"Cannot inspect free space for asset download: {exc}") from exc
    required_bytes = length + MIN_DOWNLOAD_FREE_SPACE
    if free_bytes < required_bytes:
        raise DeMethFlowError(f"Insufficient free space for asset {asset_id}: need {required_bytes} bytes including reserve; have {free_bytes}")
    # Keep an incomplete transfer under its immutable content digest.  A large
    # reference archive can legitimately outlive one HTTPS connection; keeping
    # this file lets a later request resume it, while the final size and digest
    # checks remain the only publication step.  It is intentionally not named
    # after an asset_id, which could otherwise be reassigned by future catalog
    # versions.
    partial = download_dir / f".{expected}.part"
    if partial.exists() or partial.is_symlink():
        if partial.is_symlink():
            raise DeMethFlowError(f"Refusing symlinked incomplete asset cache entry: {partial}")
        partial_stat = partial.lstat()
        if not stat.S_ISREG(partial_stat.st_mode) or partial_stat.st_nlink != 1:
            raise DeMethFlowError(f"Refusing non-regular or hard-linked incomplete asset cache entry: {partial}")
        if partial_stat.st_size > length:
            # This is a managed, incomplete content-addressed cache entry.  It
            # cannot be a valid prefix for this catalog target, so discard it
            # rather than ever appending to untrusted bytes.
            partial.unlink()
        elif partial_stat.st_size == length:
            # A prior process may have completed the transfer but exited before
            # the atomic content-addressed publish.  Verify it locally instead
            # of issuing an invalid ``Range: bytes=<length>-`` request.
            if sha256_file(partial) != expected:
                partial.unlink()
            else:
                try:
                    os.link(partial, final)
                except FileExistsError as exc:
                    raise DeMethFlowError(f"Refusing to overwrite an existing content-addressed asset: {final}") from exc
                partial.unlink()
                fsync_directory(download_dir)
                return {
                    "asset_id": asset_id,
                    "path": str(final),
                    "sha256": expected,
                    "catalog_release": catalog["catalog_release"],
                    "catalog_origin": catalog_origin,
                    "network_accessed": False,
                    "cached": True,
                    "recovered_complete_partial": True,
                    "resumed_bytes": length,
                }
    last_error: Exception | None = None
    resumed_bytes = 0
    restarted_from_zero = False
    for attempt in range(MAX_DOWNLOAD_ATTEMPTS):
        selected_url = download_urls[attempt % len(download_urls)]
        try:
            with partial.open("a+b") as handle:
                partial_stat = partial.lstat()
                descriptor_stat = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(partial_stat.st_mode) or partial_stat.st_nlink != 1
                    or not os.path.samestat(descriptor_stat, partial_stat)
                ):
                    raise DeMethFlowError(f"Incomplete asset file changed identity while downloading: {partial}")
                handle.seek(0, os.SEEK_END)
                offset = handle.tell()
                if offset > length:
                    raise DeMethFlowError("Incomplete asset cache exceeds the catalog download size limit")
                headers = {"User-Agent": "MethUnmix-asset-fetch"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                    resumed_bytes = max(resumed_bytes, offset)
                request = urllib.request.Request(selected_url, headers=headers)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    original_url = urllib.parse.urlparse(selected_url)
                    final_url = urllib.parse.urlparse(response.geturl())
                    if final_url.scheme != "https" or final_url.netloc.lower() != original_url.netloc.lower():
                        raise DeMethFlowError("Asset endpoint redirected away from its original HTTPS host")
                    status = getattr(response, "status", 200)
                    if offset and status == 206:
                        content_range = response.headers.get("Content-Range")
                        expected_range = f"bytes {offset}-{length - 1}/{length}"
                        if content_range != expected_range:
                            raise DeMethFlowError("HTTP Content-Range does not match the cached asset prefix")
                    elif offset and status == 200:
                        # Some HTTPS origins do not implement byte ranges.  A
                        # clean full retry is safe; blindly appending its body
                        # would not be.
                        handle.seek(0)
                        handle.truncate(0)
                        offset = 0
                        # The prior incomplete prefix was deliberately not
                        # reused.  Do not report it as a resumed transfer.
                        resumed_bytes = 0
                        restarted_from_zero = True
                    elif status != 200:
                        raise DeMethFlowError(f"Unexpected HTTP status while downloading asset: {status}")
                    content_length = response.headers.get("Content-Length")
                    expected_response_length = length - offset
                    if content_length is not None and int(content_length) != expected_response_length:
                        raise DeMethFlowError(f"HTTP Content-Length does not match catalog length for {asset_id}")
                    copied = offset
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
                path_stat = partial.lstat()
                if (
                    not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1
                    or not os.path.samestat(descriptor_stat, path_stat)
                ):
                    raise DeMethFlowError(f"Incomplete asset file changed identity while downloading: {partial}")
                if actual != expected:
                    raise DeMethFlowError(f"Asset SHA256 mismatch for {asset_id}")
            try:
                # Publish a complete file atomically, without overwriting an existing name.
                os.link(partial, final)
            except FileExistsError as exc:
                raise DeMethFlowError(f"Refusing to overwrite an existing content-addressed asset: {final}") from exc
            partial.unlink()
            fsync_directory(download_dir)
            return {
                "asset_id": asset_id,
                "path": str(final),
                "sha256": expected,
                "catalog_release": catalog["catalog_release"],
                "catalog_origin": catalog_origin,
                "network_accessed": True,
                "source_url": selected_url,
                "resumed_bytes": resumed_bytes,
                "restarted_from_zero": restarted_from_zero,
            }
        except DeMethFlowError:
            partial.unlink(missing_ok=True)
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            last_error = exc
            if attempt + 1 == MAX_DOWNLOAD_ATTEMPTS:
                raise DeMethFlowError(f"Cannot download asset {asset_id} after {MAX_DOWNLOAD_ATTEMPTS} attempts: {exc}") from exc
    raise DeMethFlowError(f"Cannot download asset {asset_id}: {last_error or 'all download attempts failed'}")


def _reference_target(selector: str, *, metadata_dir: Path | None = None) -> tuple[dict[str, Any], dict[str, Any], str]:
    if not FULL_SELECTOR.fullmatch(selector):
        raise DeMethFlowError("reference fetch requires the exact selector in reference_id@version form")
    catalog, catalog_origin = _current_catalog(metadata_dir)
    matches = [
        item for item in catalog.get("targets", [])
        if isinstance(item, dict) and item.get("module_type") == "reference" and item.get("selector") == selector
    ]
    if len(matches) != 1:
        raise DeMethFlowError(f"Expected exactly one catalog reference for {selector!r}; found {len(matches)}")
    target = matches[0]
    if target.get("asset_id") != f"reference.{target.get('reference_id')}":
        raise DeMethFlowError(f"Catalog reference has inconsistent asset_id: {target.get('asset_id')!r}")
    if target.get("reference_id") != selector.rsplit("@", 1)[0] or target.get("version") != selector.rsplit("@", 1)[1]:
        raise DeMethFlowError(f"Catalog reference has inconsistent selector fields: {selector!r}")
    return target, catalog, catalog_origin


def _reference_archive_identity(archive: Path, target: dict[str, Any]) -> dict[str, Any]:
    # This is a read-only preflight. ModuleStore.install performs the full
    # archive/member/payload checksum validation before it publishes anything.
    from .offline_assets import _declared_reference_digest, _read_archive

    module, reference = _read_archive(archive)
    if reference is None:
        raise DeMethFlowError(f"Downloaded archive does not contain a reference manifest: {archive}")
    selector = f"{reference.get('reference_id')}@{reference.get('version')}"
    actual = {
        "selector": selector,
        "reference_id": reference.get("reference_id"),
        "version": reference.get("version"),
        "module_id": module.get("module_id"),
        "module_type": module.get("module_type"),
        "content_digest": _declared_reference_digest(reference),
    }
    expected = {
        "selector": target.get("selector"),
        "reference_id": target.get("reference_id"),
        "version": target.get("version"),
        "module_id": target.get("asset_id"),
        "module_type": "reference",
        "content_digest": target.get("content_digest"),
    }
    mismatches = [key for key in expected if expected[key] != actual[key]]
    if mismatches:
        raise DeMethFlowError(
            "Downloaded reference archive does not match its catalog record for "
            f"{target.get('selector')}: {', '.join(mismatches)}"
        )
    return actual


def _ensure_cached_archive_sidecar(archive: Path, digest: str) -> Path:
    """Materialise the install-required sidecar beside a verified cache object.

    The content-addressed cache filename is the archive digest rather than the
    original tarball name.  ModuleStore intentionally requires a same-path
    sidecar, so reference fetch creates that small derived record only after
    hashing and identity preflight have succeeded.
    """
    sidecar = Path(f"{archive}.sha256")
    expected = f"{digest}  {archive.name}\n".encode("utf-8")
    if sidecar.exists() or sidecar.is_symlink():
        try:
            state = sidecar.lstat()
        except OSError as exc:
            raise DeMethFlowError(f"Cannot validate cached archive checksum sidecar: {sidecar}: {exc}") from exc
        if not stat.S_ISREG(state.st_mode) or state.st_nlink != 1:
            raise DeMethFlowError(f"Cached archive checksum sidecar is unsafe or inconsistent: {sidecar}")
        try:
            content = sidecar.read_bytes()
        except OSError as exc:
            raise DeMethFlowError(f"Cannot read cached archive checksum sidecar: {sidecar}: {exc}") from exc
        if content != expected:
            raise DeMethFlowError(f"Cached archive checksum sidecar is unsafe or inconsistent: {sidecar}")
        return sidecar
    descriptor = None
    try:
        descriptor = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, expected)
        os.fsync(descriptor)
    except FileExistsError:
        return _ensure_cached_archive_sidecar(archive, digest)
    except OSError as exc:
        raise DeMethFlowError(f"Cannot create cached archive checksum sidecar: {sidecar}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    fsync_directory(archive.parent)
    return sidecar


def _reference_dependencies(reference: dict[str, Any]) -> list[str]:
    tools = reference.get("tool_capabilities", {})
    if not isinstance(tools, dict):
        return []
    required: set[str] = set()
    for capability in tools.values():
        if not isinstance(capability, dict):
            continue
        for key in ("runtime_modules", "data_modules"):
            values = capability.get(key, [])
            if isinstance(values, list):
                required.update(str(value) for value in values if isinstance(value, str) and value)
        profiles = capability.get("execution_profiles", {})
        if isinstance(profiles, dict):
            for profile in profiles.values():
                if not isinstance(profile, dict):
                    continue
                values = profile.get("runtime_modules", [])
                if isinstance(values, list):
                    required.update(str(value) for value in values if isinstance(value, str) and value)
    return sorted(required)


def fetch_reference(selector: str, *, install: bool = False, metadata_dir: Path | None = None, timeout: int = 60) -> dict[str, Any]:
    """Fetch one exact public reference and optionally install it into METHUNMIX_HOME."""
    target, _catalog, _catalog_origin = _reference_target(selector, metadata_dir=metadata_dir)
    downloaded = fetch_asset_record(str(target["asset_id"]), metadata_dir=metadata_dir, timeout=timeout)
    archive = Path(str(downloaded["path"]))
    identity = _reference_archive_identity(archive, target)
    result: dict[str, Any] = {
        "selector": selector,
        "asset_id": target["asset_id"],
        "download": downloaded,
        "archive_identity": identity,
        "installed": False,
        "network_accessed": downloaded["network_accessed"],
    }
    if not install:
        return result
    _ensure_cached_archive_sidecar(archive, str(target["sha256"]).lower())
    module, created = ModuleStore().install(archive)
    if module.module_id != target["asset_id"] or module.payload.get("module_type") != "reference":
        raise DeMethFlowError(f"Installed module does not match catalog reference {selector!r}")
    from .manifest import load_reference

    bundle = load_reference(module.install_root / "manifest.json", verify_files=True)
    if bundle.selector != selector or bundle.content_digest(verify_files=True) != target.get("content_digest"):
        raise DeMethFlowError(f"Installed reference content does not match catalog record for {selector!r}")
    dependencies = _reference_dependencies(bundle.payload)
    store = ModuleStore()
    installed_modules = {item.module_id for item in store.list()}
    result.update({
        "installed": created,
        "install_state": "INSTALLED" if created else "ALREADY_INSTALLED",
        "installed_path": str(module.install_root),
        "reference_validation": "PASS",
        "status": bundle.status,
        "required_modules": dependencies,
        "missing_modules": [name for name in dependencies if name not in installed_modules],
    })
    return result


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
