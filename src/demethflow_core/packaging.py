from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from . import CORE_API, REFERENCE_SCHEMA, RUNTIME_API
from .errors import DeMethFlowError
from .manifest import ReferenceBundle
from .util import atomic_write_json, sha256_file


def export_reference(bundle: ReferenceBundle, destination: Path) -> tuple[Path, Path]:
    destination = destination.expanduser().resolve()
    if destination.is_dir() or destination.suffix not in {".gz", ".tgz"}:
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination / f"{bundle.reference_id}-{bundle.version}.tar.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise DeMethFlowError(f"Refusing to overwrite existing archive: {destination}")
    stage = Path(tempfile.mkdtemp(prefix="demethflow-export-", dir=destination.parent))
    try:
        payload_root = stage / "payload"
        shutil.copytree(bundle.root, payload_root, symlinks=False)
        files = []
        for path in sorted(payload_root.rglob("*")):
            if path.is_file():
                files.append(
                    {
                        "path": path.relative_to(payload_root).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
        module = {
            "schema": "demethflow-module-v2",
            # Reference bundles are data; workflow execution has a separate
            # Linux x86_64 constraint in the capability registry.
            "platform": "noarch",
            "module_id": f"reference.{bundle.reference_id}",
            "module_type": "reference",
            "reference_id": bundle.reference_id,
            "version": bundle.version,
            "installed_bytes": sum(item["bytes"] for item in files),
            "compatibility": {
                "core_api": CORE_API,
                "reference_schema": REFERENCE_SCHEMA,
                "runtime_api": RUNTIME_API,
            },
            "files": files,
        }
        atomic_write_json(stage / "module.json", module)
        temporary_archive = destination.with_name(f".{destination.name}.partial")
        with tarfile.open(temporary_archive, "w:gz", compresslevel=1) as handle:
            handle.add(stage / "module.json", arcname="module.json", recursive=False)
            handle.add(payload_root, arcname="payload", recursive=True)
        os.replace(temporary_archive, destination)
        digest = sha256_file(destination)
        sidecar = Path(f"{destination}.sha256")
        sidecar.write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
        return destination, sidecar
    finally:
        shutil.rmtree(stage, ignore_errors=True)
