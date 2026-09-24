from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import stat
import tarfile
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import CORE_API, REFERENCE_SCHEMA, RUNTIME_API
from .errors import InstallError, ManifestError
from .util import (
    assert_no_symlink_components,
    absolute_lexical_path,
    atomic_write_json,
    default_data_home,
    fsync_directory,
    legacy_data_home,
    prepare_data_home,
    read_json,
    sha256_file,
    validate_id,
)


MODULE_SCHEMA = "demethflow-module-v2"
MODULE_TYPES = {"reference", "runtime", "tool-data", "build-runtime", "build-assets", "launcher"}
MAX_ARCHIVE_MEMBERS = 250_000
MAX_UNPACKED_BYTES = 128 * 1024**3
LOCK_TIMEOUT_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class InstalledModule:
    manifest_path: Path
    payload: dict

    @property
    def module_id(self) -> str:
        return self.payload["module_id"]

    @property
    def version(self) -> str:
        return self.payload["version"]

    @property
    def selector(self) -> str:
        return f"{self.module_id}@{self.version}"

    @property
    def install_root(self) -> Path:
        return Path(self.payload["installed_path"])


class ModuleStore:
    def __init__(self, root: Path | None = None):
        self.uses_default_root = root is None
        try:
            self.root = absolute_lexical_path(root if root is not None else default_data_home())
        except ManifestError as exc:
            raise InstallError(str(exc)) from exc
        self.meta_root = self.root / ".methunmix" / "modules"
        self.legacy_meta_root = self.root / ".demethflow" / "modules"
        self.legacy_root = legacy_data_home()

    def list(self) -> list[InstalledModule]:
        try:
            assert_no_symlink_components(self.root)
        except ManifestError as exc:
            raise InstallError(str(exc)) from exc
        output: list[InstalledModule] = []
        roots = [root for root in (self.meta_root, self.legacy_meta_root, self.legacy_root / ".demethflow" / "modules") if root.is_dir()]
        seen: set[Path] = set()
        for meta_root in roots:
            if meta_root.is_symlink():
                continue
            for path in sorted(meta_root.glob("*.json")):
                if path in seen:
                    continue
                seen.add(path)
                if path.is_symlink():
                    continue
                try:
                    output.append(InstalledModule(path, read_json(path)))
                except ManifestError:
                    continue
        return output

    def find(self, module_id: str) -> InstalledModule | None:
        matches = [module for module in self.list() if module.module_id == module_id]
        if not matches:
            return None
        if len(matches) > 1:
            matches.sort(key=lambda item: item.version)
        return matches[-1]

    def install(self, archive: Path, verify_sidecar: bool = True) -> tuple[InstalledModule, bool]:
        try:
            archive = absolute_lexical_path(archive.expanduser(), name="module archive")
            assert_no_symlink_components(archive)
        except ManifestError as exc:
            raise InstallError(str(exc)) from exc
        if not archive.is_file():
            raise InstallError(f"Module archive does not exist: {archive}")
        sidecar = Path(f"{archive}.sha256")
        try:
            descriptor = os.open(archive, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise InstallError(f"Cannot safely open module archive {archive}: {exc}") from exc
        with os.fdopen(descriptor, "rb") as archive_handle:
            try:
                archive_before = os.fstat(archive_handle.fileno())
                if not stat.S_ISREG(archive_before.st_mode):
                    raise InstallError(f"Module archive is not a regular file: {archive}")
                archive_sha = _sha256_handle(archive_handle)
                if verify_sidecar:
                    try:
                        sidecar_descriptor = os.open(sidecar, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    except OSError as exc:
                        raise InstallError(
                            f"Module archive checksum sidecar is missing or unsafe: {sidecar}: {exc}"
                        ) from exc
                    with os.fdopen(sidecar_descriptor, "rb") as sidecar_handle:
                        sidecar_stat = os.fstat(sidecar_handle.fileno())
                        if not stat.S_ISREG(sidecar_stat.st_mode):
                            raise InstallError(f"Module archive checksum sidecar is not a regular file: {sidecar}")
                        if sidecar_stat.st_size > 4096:
                            raise InstallError(f"Module archive checksum sidecar is too large: {sidecar}")
                        checksum_document = sidecar_handle.read(4097)
                    try:
                        checksum_parts = checksum_document.decode("utf-8").split()
                    except UnicodeDecodeError as exc:
                        raise InstallError(f"Module archive checksum sidecar is not UTF-8: {sidecar}") from exc
                    expected = checksum_parts[0].lower() if checksum_parts else ""
                    if not re.fullmatch(r"[0-9a-f]{64}", expected):
                        raise InstallError(f"Module archive checksum sidecar is malformed: {sidecar}")
                    if expected != archive_sha:
                        raise InstallError(f"Archive checksum mismatch for {archive}")

                if self.uses_default_root:
                    self.root = prepare_data_home()
                else:
                    try:
                        assert_no_symlink_components(self.root)
                    except ManifestError as exc:
                        raise InstallError(str(exc)) from exc
                    self.root.mkdir(parents=True, exist_ok=True)
                staging_parent = self.root / ".methunmix" / "staging"
                self._ensure_store_directory(staging_parent)
                self._ensure_store_directory(self.meta_root)
                with _store_lock(self.root / ".methunmix" / "store.lock", operation=f"install:{archive.name}"):
                    staging = Path(tempfile.mkdtemp(prefix="install-", dir=staging_parent))
                    try:
                        archive_handle.seek(0)
                        with tarfile.open(fileobj=archive_handle, mode="r:*") as handle:
                            members = handle.getmembers()
                            required = sum(member.size for member in members if member.isfile())
                            free = shutil.disk_usage(self.root).free
                            reserve = 64 * 1024 * 1024
                            if free < required + reserve:
                                raise InstallError(
                                    f"Insufficient disk space under {self.root}: need at least "
                                    f"{required + reserve} bytes, have {free}"
                                )
                            _safe_extract(handle, staging, archive_before.st_size)
                        archive_after = os.fstat(archive_handle.fileno())
                        if _archive_stat_signature(archive_before) != _archive_stat_signature(archive_after):
                            raise InstallError(f"Module archive changed while it was being installed: {archive}")
                        module_manifest = staging / "module.json"
                        if not module_manifest.is_file():
                            raise InstallError("Archive is missing module.json at its root")
                        payload = _validate_module_payload(read_json(module_manifest))
                        payload_root = staging / "payload"
                        if not payload_root.is_dir():
                            raise InstallError("Archive is missing payload/")
                        _verify_payload(payload_root, payload.get("files", []))
                        _fsync_tree(staging)
                        destination = self._destination(payload)
                        metadata_path = self.meta_root / f"{payload['module_id']}@{payload['version']}.json"
                        self._ensure_store_directory(destination.parent)
                        if metadata_path.is_symlink():
                            raise InstallError(f"Refusing a symlinked module registration file: {metadata_path}")
                        if metadata_path.is_file():
                            installed = read_json(metadata_path)
                            if installed.get("archive_sha256") == archive_sha and destination.is_dir():
                                return InstalledModule(metadata_path, installed), False
                            raise InstallError(
                                f"Refusing to overwrite {payload['module_id']}@{payload['version']} with different content"
                            )
                        if destination.exists() or destination.is_symlink():
                            raise InstallError(f"Destination already exists without registration: {destination}")
                        os.replace(payload_root, destination)
                        fsync_directory(destination.parent)
                        fsync_directory(staging)
                        installed_payload = dict(payload)
                        installed_payload.update(
                            archive_sha256=archive_sha,
                            installed_path=str(destination),
                            source_archive=str(archive),
                        )
                        atomic_write_json(metadata_path, installed_payload)
                        return InstalledModule(metadata_path, installed_payload), True
                    except (tarfile.TarError, OSError, ManifestError) as exc:
                        if isinstance(exc, InstallError):
                            raise
                        raise InstallError(f"Cannot install {archive}: {exc}") from exc
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)
            except OSError as exc:
                raise InstallError(f"Cannot inspect module archive {archive}: {exc}") from exc

    def verify(self, module: InstalledModule) -> list[str]:
        errors: list[str] = []
        if module.install_root.is_symlink():
            return [f"symlink present at installed root: {module.install_root}"]
        if not module.install_root.is_dir():
            return [f"missing install root: {module.install_root}"]
        declared = {entry["path"]: entry for entry in module.payload.get("files", [])}
        actual: dict[str, Path] = {}
        for candidate in module.install_root.rglob("*"):
            relative = candidate.relative_to(module.install_root).as_posix()
            if candidate.is_symlink():
                errors.append(f"symlink present in installed payload: {relative}")
            elif candidate.is_file():
                actual[relative] = candidate
            elif not candidate.is_dir():
                errors.append(f"special file present in installed payload: {relative}")
        for relative in sorted(set(declared) - set(actual)):
            errors.append(f"missing: {relative}")
        for relative in sorted(set(actual) - set(declared)):
            errors.append(f"undeclared: {relative}")
        for relative, entry in declared.items():
            path = actual.get(relative)
            if path is None:
                continue
            if not path.is_file():
                errors.append(f"missing: {entry['path']}")
                continue
            actual_hash = sha256_file(path)
            if actual_hash != entry["sha256"]:
                errors.append(f"checksum mismatch: {entry['path']}")
            if "bytes" in entry and path.stat().st_size != entry["bytes"]:
                errors.append(f"size mismatch: {entry['path']}")
        return errors

    def _destination(self, payload: dict) -> Path:
        module_id = payload["module_id"]
        version = payload["version"]
        kind = payload["module_type"]
        if kind == "reference":
            reference_id = validate_id(payload.get("reference_id"), "reference_id")
            return self.root / "references" / reference_id / version
        if kind == "runtime":
            return self.root / "runtimes" / module_id / version
        if kind == "tool-data":
            return self.root / "tool-data" / module_id / version
        return self.root / "modules" / module_id / version

    def _ensure_store_directory(self, path: Path) -> None:
        """Create a directory below the store without traversing symlinks."""
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise InstallError(f"Managed module-store path escapes store root: {path}") from exc
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise InstallError(f"Refusing a symlink in module-store path: {current}")
            try:
                current.mkdir()
            except FileExistsError:
                if current.is_symlink() or not current.is_dir():
                    raise InstallError(f"Module-store path is not a real directory: {current}")
            except OSError as exc:
                raise InstallError(f"Cannot create module-store directory {current}: {exc}") from exc


def _validate_module_payload(payload: dict) -> dict:
    if payload.get("schema") != MODULE_SCHEMA:
        raise InstallError(f"Unsupported module schema: {payload.get('schema')!r}")
    validate_id(payload.get("module_id"), "module_id")
    validate_id(payload.get("version"), "version")
    if payload.get("module_type") not in MODULE_TYPES:
        raise InstallError(f"Invalid module_type: {payload.get('module_type')!r}")
    target = payload.get("platform")
    if not isinstance(target, str) or not target.strip():
        raise InstallError("Module platform declaration is required")
    current = f"{platform.system().lower()}-{platform.machine().lower()}"
    aliases = {"linux-amd64": "linux-x86_64"}
    normalized_target = aliases.get(target.lower(), target.lower())
    normalized_current = aliases.get(current, current)
    if normalized_target not in {"noarch", "linux-x86_64"}:
        raise InstallError(f"Unsupported module platform: {target!r}")
    executable_module = payload.get("module_type") in {"runtime", "build-runtime", "launcher"}
    if executable_module and normalized_target == "noarch":
        raise InstallError(f"Executable module {payload.get('module_type')!r} cannot be declared noarch")
    if normalized_target not in {"noarch", normalized_current}:
        raise InstallError(f"Module platform {target!r} is incompatible with host {current!r}")
    compatibility = payload.get("compatibility")
    if not isinstance(compatibility, dict):
        raise InstallError("module compatibility must be an object")
    required = {"core_api": CORE_API, "reference_schema": REFERENCE_SCHEMA, "runtime_api": RUNTIME_API}
    for key, current in required.items():
        actual = str(compatibility.get(key, ""))
        if actual.split(".", 1)[0] != current.split(".", 1)[0]:
            raise InstallError(f"Incompatible {key}={actual!r}; this core requires {current}")
    files = payload.get("files")
    if not isinstance(files, list):
        raise InstallError("module files must be a list")
    declared_paths: set[str] = set()
    declared_files: set[str] = set()
    declared_directories: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise InstallError("Every module file entry requires path and sha256")
        path = _safe_member_path(entry["path"])
        path_key = "/".join(part.casefold() for part in path.parts)
        if path_key in declared_paths:
            raise InstallError(f"Duplicate or case-colliding module file path: {entry['path']}")
        if any("/".join(path_key.split("/")[:index]) in declared_files for index in range(1, len(path.parts))):
            raise InstallError(f"Module file path is nested below another file: {entry['path']}")
        if path_key in declared_directories:
            raise InstallError(f"Module file path conflicts with a directory: {entry['path']}")
        declared_paths.add(path_key)
        declared_files.add(path_key)
        for index in range(1, len(path.parts)):
            declared_directories.add("/".join(path_key.split("/")[:index]))
        digest = entry.get("sha256", "")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise InstallError(f"Invalid sha256 for {entry['path']}")
        if "bytes" in entry and (
            isinstance(entry["bytes"], bool)
            or not isinstance(entry["bytes"], int)
            or entry["bytes"] < 0
        ):
            raise InstallError(f"Invalid byte length for {entry['path']}")
    exports = payload.get("exports", {})
    if not isinstance(exports, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in exports.items()):
        raise InstallError("module exports must be a string-to-string object")
    for name, relative in exports.items():
        validate_id(name, "module export")
        _safe_member_path(relative)
    return payload


def _safe_member_path(name: str) -> PurePosixPath:
    if not isinstance(name, str) or "\x00" in name:
        raise InstallError(f"Unsafe archive path: {name!r}")
    if name != unicodedata.normalize("NFC", name):
        raise InstallError(f"Archive path is not NFC-normalized: {name!r}")
    windows_path = PureWindowsPath(name)
    if (
        "\\" in name
        or windows_path.is_absolute()
        or windows_path.drive
        or ":" in name
        or any(part.endswith((".", " ")) for part in name.split("/"))
        or any(_is_windows_reserved_component(part) for part in name.split("/"))
    ):
        raise InstallError(f"Archive path is not portable across supported platforms: {name!r}")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != name
        or ".." in path.parts
        or "." in path.parts
    ):
        raise InstallError(f"Unsafe archive path: {name!r}")
    return path


def _is_windows_reserved_component(part: str) -> bool:
    stem = part.split(".", 1)[0].upper()
    return stem in {"CON", "PRN", "AUX", "NUL"} or bool(
        len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "123456789"
    )


def _safe_extract(handle: tarfile.TarFile, destination: Path, archive_bytes: int) -> None:
    members = handle.getmembers()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise InstallError(f"Archive contains too many members: {len(members)}")
    if any(member.size < 0 for member in members):
        raise InstallError("Archive contains a negative member size")
    total = sum(member.size for member in members if member.isfile())
    if total > MAX_UNPACKED_BYTES:
        raise InstallError(f"Archive expands beyond the {MAX_UNPACKED_BYTES} byte limit")
    if archive_bytes > 0 and total > archive_bytes * 10_000:
        raise InstallError("Archive compression ratio is unsafe")
    normalized: set[str] = set()
    files_seen: set[str] = set()
    directories_seen: set[str] = set()
    for member in members:
        raw_name = member.name
        name = raw_name[:-1] if member.isdir() and raw_name.endswith("/") else raw_name
        path = _safe_member_path(name)
        if name == "module.json":
            if not member.isfile():
                raise InstallError("module.json must be a regular file at the archive root")
        elif name == "payload":
            if not member.isdir():
                raise InstallError("payload at the archive root must be a directory")
        elif not name.startswith("payload/"):
            raise InstallError(f"Unexpected path outside module.json and payload/: {member.name}")
        key = unicodedata.normalize("NFC", name).casefold()
        if key in normalized:
            raise InstallError(f"Duplicate archive path: {member.name}")
        normalized.add(key)
        if not member.isfile() and not member.isdir():
            raise InstallError(f"Only regular files and directories are allowed in module archives: {member.name}")
        if member.isdir() and member.size != 0:
            raise InstallError(f"Directory member has unexpected data: {member.name}")
        parts = [part.casefold() for part in path.parts]
        prefixes = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
        if any(prefix in files_seen for prefix in prefixes[:-1]):
            raise InstallError(f"Archive path is nested below a file: {member.name}")
        if member.isfile():
            if key in directories_seen:
                raise InstallError(f"Archive file conflicts with a directory: {member.name}")
            files_seen.add(key)
        else:
            if key in files_seen:
                raise InstallError(f"Archive directory conflicts with a file: {member.name}")
            directories_seen.add(key)
        directories_seen.update(prefixes[:-1])
        target = destination.joinpath(*path.parts)
        current = destination
        for part in path.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise InstallError(f"Archive path traverses a symlink: {member.name}")
        try:
            target.resolve(strict=False).relative_to(destination.resolve())
        except ValueError as exc:
            raise InstallError(f"Archive path escapes destination: {member.name}") from exc
    # The preflight above intentionally avoids tarfile's newer extraction
    # filter API so Python 3.10--3.13 behave identically.
    handle.extractall(destination)


def _pid_is_alive(pid: int) -> bool:
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown platform/permission failures must not be interpreted as
        # permission to steal a lock.
        return True
    return True


@contextmanager
def _store_lock(path: Path, *, operation: str = "module-store-operation"):
    """Acquire a module-store lock with serialized, conservative stale recovery.

    A persistent sibling guard file serializes the short inspect/recover/create
    section. Without it, two installers can both observe a stale directory and
    one can remove the other's newly-created live lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
        lock_backend = "fcntl"
    except ImportError:  # pragma: no cover - exercised on Windows
        try:
            import msvcrt
            lock_backend = "msvcrt"
        except ImportError as exc:
            raise InstallError("Safe module-store locking requires POSIX flock or Windows msvcrt locking") from exc

    guard_path = path.with_name(f"{path.name}.guard")
    if guard_path.is_symlink():
        raise InstallError(f"Refusing a symlinked module-store lock guard: {guard_path}")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        guard_fd = os.open(guard_path, flags, 0o600)
    except OSError as exc:
        raise InstallError(f"Cannot safely open module-store lock guard {guard_path}: {exc}") from exc

    lock_created = False
    try:
        opened_stat = os.fstat(guard_fd)
        path_stat = guard_path.lstat()
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or (opened_stat.st_dev, opened_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise InstallError(f"Module-store lock guard is not a regular file: {guard_path}")
        with os.fdopen(guard_fd, "a+b", closefd=True) as guard:
            guard_fd = -1
            try:
                if lock_backend == "fcntl":
                    fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
                else:
                    guard.seek(0, os.SEEK_END)
                    if guard.tell() == 0:
                        guard.write(b"\0")
                        guard.flush()
                    guard.seek(0)
                    msvcrt.locking(guard.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise InstallError(f"Cannot acquire module-store lock guard {guard_path}: {exc}") from exc
            try:
                locked_stat = guard_path.lstat()
                opened_stat = os.fstat(guard.fileno())
                if (
                    guard_path.is_symlink()
                    or not stat.S_ISREG(locked_stat.st_mode)
                    or (opened_stat.st_dev, opened_stat.st_ino) != (locked_stat.st_dev, locked_stat.st_ino)
                ):
                    raise InstallError(f"Module-store lock guard changed while acquiring it: {guard_path}")
                if path.is_symlink():
                    raise InstallError(f"Refusing a symlinked module-store lock path: {path}")
                if path.exists():
                    if not path.is_dir():
                        raise InstallError(f"Module-store lock path is not a directory: {path}")
                    owner = path / "owner.json"
                    try:
                        payload = read_json(owner)
                    except ManifestError as exc:
                        raise InstallError(
                            f"Cannot safely recover lock without valid owner metadata: {path}; "
                            "inspect it manually on the same local filesystem"
                        ) from exc
                    host = str(payload.get("host", ""))
                    if host != socket.gethostname():
                        raise InstallError(
                            f"Lock belongs to host {host or '<unknown>'}: {path}; remote/NFS stale-lock "
                            "recovery is not supported automatically"
                        )
                    try:
                        pid = int(payload.get("pid", 0))
                        created_at = float(payload.get("created_at"))
                        recorded_timeout = int(payload.get("stale_timeout_seconds"))
                    except (TypeError, ValueError):
                        pid, created_at, recorded_timeout = 0, 0.0, 0
                    if (
                        pid < 1 or created_at <= 0 or created_at > time.time()
                        or recorded_timeout < 1 or not str(payload.get("operation", "")).strip()
                    ):
                        raise InstallError(
                            f"Lock owner metadata is incomplete or invalid: {path}; inspect it manually"
                        )
                    if _pid_is_alive(pid):
                        raise InstallError(
                            f"Another {payload.get('operation', 'module-store operation')} is active "
                            f"(pid {pid}) at {path}"
                        )
                    # Only this serialized critical section may remove a stale lock.
                    shutil.rmtree(path)
                try:
                    path.mkdir()
                except FileExistsError as exc:
                    raise InstallError(f"Another module-store operation acquired the lock: {path}") from exc
                lock_created = True
                try:
                    atomic_write_json(path / "owner.json", {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "created_at": time.time(),
                        "operation": operation,
                        "stale_timeout_seconds": LOCK_TIMEOUT_SECONDS,
                    })
                except OSError as exc:
                    shutil.rmtree(path, ignore_errors=True)
                    lock_created = False
                    raise InstallError(f"Cannot write module-store lock metadata for {path}: {exc}") from exc
            finally:
                if lock_backend == "fcntl":
                    fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
                else:
                    guard.seek(0)
                    msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        if guard_fd >= 0:
            os.close(guard_fd)
    try:
        yield
    finally:
        if lock_created:
            shutil.rmtree(path, ignore_errors=True)


def _verify_payload(payload_root: Path, files: list[dict]) -> None:
    declared = {entry["path"]: entry for entry in files}
    actual_files = {
        path.relative_to(payload_root).as_posix(): path
        for path in payload_root.rglob("*")
        if path.is_file()
    }
    missing = sorted(set(declared) - set(actual_files))
    extra = sorted(set(actual_files) - set(declared))
    if missing:
        raise InstallError(f"Module payload is missing declared files: {', '.join(missing)}")
    if extra:
        raise InstallError(f"Module payload contains undeclared files: {', '.join(extra)}")
    for relative, path in actual_files.items():
        declared_entry = declared[relative]
        if "bytes" in declared_entry and path.stat().st_size != declared_entry["bytes"]:
            raise InstallError(f"Payload size mismatch: {relative}")
        if sha256_file(path) != declared_entry["sha256"]:
            raise InstallError(f"Payload checksum mismatch: {relative}")


def _sha256_handle(handle) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


def _archive_stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _fsync_tree(root: Path) -> None:
    """Flush staged module contents before atomically publishing the payload."""
    if os.name == "nt":
        # Windows supports fsync on regular files but not portable directory
        # handles. Flush file data; atomic rename remains the publication step.
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        return
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise InstallError(f"Refusing symlink before flushing staged module: {path}")
        if path.is_file():
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif path.is_dir():
            directories.append(path)
        else:
            raise InstallError(f"Refusing special file before publishing module: {path}")
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fsync_directory(directory)
