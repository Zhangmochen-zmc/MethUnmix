from __future__ import annotations

import hashlib
import json
import errno
import os
import platform
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from .errors import DeMethFlowError, ManifestError


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DATA_HOME_OVERRIDE: Path | None = None


def strict_json_loads(document: str) -> Any:
    """Parse JSON while rejecting duplicate object keys instead of last-wins."""
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    return json.loads(document, object_pairs_hook=unique_pairs)


def require_linux_x86_64_workflow() -> None:
    """Fail early when a workflow is launched outside its supported host."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux" and machine in {"x86_64", "amd64"}:
        return
    raise DeMethFlowError(
        "WORKFLOW_PLATFORM_UNSUPPORTED: reference construction and deconvolution "
        f"workflows require Linux x86_64; detected {system or 'unknown'}-{machine or 'unknown'}. "
        "Core installation and offline asset/reference management may still be used on this host."
    )


def require_local_executor(executor: str) -> None:
    """Enforce MethUnmix 2.0's local-only scheduler support boundary."""
    if executor != "local":
        raise DeMethFlowError(
            "EXECUTOR_UNSUPPORTED: MethUnmix 2.0 supports only the local Nextflow executor; "
            "Slurm is out of scope and is neither supported nor tested by this release."
        )


def fsync_directory(path: Path) -> None:
    """Persist a directory entry update where the platform supports it."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if exc.errno not in unsupported:
            raise
    finally:
        os.close(descriptor)


@contextmanager
def data_home_override(path: Path) -> Iterator[None]:
    """Temporarily apply an explicit CLI --home without mutating environment."""
    global _DATA_HOME_OVERRIDE
    previous = _DATA_HOME_OVERRIDE
    _DATA_HOME_OVERRIDE = path
    try:
        yield
    finally:
        _DATA_HOME_OVERRIDE = previous


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"Cannot read JSON manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"Manifest must contain a JSON object: {path}")
    return payload


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        fsync_directory(path.parent)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_digest(items: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(items):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ManifestError(f"{label} must match {SAFE_ID.pattern}: {value!r}")
    return value


def validate_relative_posix(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must be a non-empty relative POSIX path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise ManifestError(f"{label} must not be absolute or contain '.'/'..': {value}")
    return candidate


def resolve_artifact(manifest_dir: Path, relative: object, label: str) -> Path:
    rel = validate_relative_posix(relative, label)
    root = manifest_dir.resolve()
    path = manifest_dir.joinpath(*rel.parts)
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ManifestError(f"Missing {label}: {path}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"{label} escapes its reference bundle: {relative}") from exc
    if not resolved.is_file() and not resolved.is_dir():
        raise ManifestError(f"{label} is not a regular file or directory: {path}")
    return resolved


def default_data_home() -> Path:
    # New name wins.  The old variable remains a read-compatible alias for
    # DeMethFlow 1.x users. Blank values are treated as unset; conflicting
    # non-empty values fail rather than silently selecting the wrong store.
    if _DATA_HOME_OVERRIDE is not None:
        return _DATA_HOME_OVERRIDE
    new_value = os.environ.get("METHUNMIX_HOME") or None
    old_value = os.environ.get("DEMETHFLOW_HOME") or None
    new_home = _absolute_configured_path(new_value, "METHUNMIX_HOME") if new_value else None
    old_home = _absolute_configured_path(old_value, "DEMETHFLOW_HOME") if old_value else None
    if new_home is not None and old_home is not None and new_home != old_home:
        raise ManifestError(
            "METHUNMIX_HOME and DEMETHFLOW_HOME point to different stores; "
            "set only one, or set both to the same absolute path"
        )
    if new_home is not None or old_home is not None:
        return new_home or old_home  # type: ignore[return-value]
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return _absolute_configured_path(xdg, "XDG_DATA_HOME") / "methunmix"
    return Path.home() / ".local" / "share" / "methunmix"


def prepare_data_home() -> Path:
    """Create the selected store; make implicit per-user stores private.

    An explicit METHUNMIX_HOME/DEMETHFLOW_HOME may intentionally be shared,
    so this helper never chmods it. XDG/default locations are treated as
    personal stores and kept at 0700. Symlinked final store paths are not
    chmodded through to their targets.
    """
    explicit_store = _DATA_HOME_OVERRIDE is not None or bool(
        os.environ.get("METHUNMIX_HOME") or os.environ.get("DEMETHFLOW_HOME")
    )
    path = default_data_home()
    try:
        assert_no_symlink_components(path)
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not explicit_store:
            path.chmod(0o700)
    except OSError as exc:
        raise ManifestError(f"Cannot prepare MethUnmix data directory {path}: {exc}") from exc
    return path


def legacy_data_home() -> Path:
    """Return the pre-2.x DeMethFlow data location for read-only discovery."""
    old = os.environ.get("DEMETHFLOW_HOME")
    if old and not os.environ.get("METHUNMIX_HOME"):
        return _absolute_configured_path(old, "DEMETHFLOW_HOME")
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return _absolute_configured_path(xdg, "XDG_DATA_HOME") / "demethflow"
    return Path(os.path.abspath(Path.home() / ".local" / "share" / "demethflow"))


def _absolute_configured_path(value: str, name: str) -> Path:
    return absolute_lexical_path(Path(value), name=name, require_absolute=True)


def absolute_lexical_path(
    path: Path, *, name: str = "managed path", require_absolute: bool = False
) -> Path:
    expanded = path.expanduser()
    if require_absolute and not expanded.is_absolute():
        raise ManifestError(f"{name} must be an absolute path")
    if ".." in expanded.parts:
        raise ManifestError(f"{name} must not contain '..' path components")
    # Normalize `.` without resolving symlinks. Managed-store callers must
    # see and reject symlink components instead of canonicalizing through them.
    return Path(os.path.abspath(expanded))


def assert_no_symlink_components(path: Path) -> None:
    """Reject symlinks in a path while preserving lexical path components."""
    absolute = absolute_lexical_path(path)
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ManifestError(f"Refusing a symlink in MethUnmix managed path: {candidate}")
