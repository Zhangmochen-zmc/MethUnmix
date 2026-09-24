#!/usr/bin/env python3
"""Rewrite wheel ZIP metadata with fixed timestamps for reproducible bytes."""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import zipfile
from pathlib import Path


FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def normalize_wheel(source: Path, output: Path) -> str:
    if source.suffix != ".whl" or output.suffix != ".whl":
        raise ValueError("both paths must end in .whl")
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"wheel source must be a regular non-symlink file: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".methunmix-wheel-", suffix=".tmp", dir=output.parent, delete=False) as temporary:
            temporary_name = temporary.name
        with zipfile.ZipFile(source, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("wheel contains duplicate member paths")
            comment = archive.comment
            members = [(archive.getinfo(name), archive.read(name)) for name in sorted(names)]
        with zipfile.ZipFile(temporary_name, "w", allowZip64=True, compression=zipfile.ZIP_DEFLATED, compresslevel=9) as normalized:
            normalized.comment = comment
            for old_info, content in members:
                info = zipfile.ZipInfo(old_info.filename, date_time=FIXED_TIMESTAMP)
                info.compress_type = old_info.compress_type
                info.comment = old_info.comment
                info.extra = b""
                info.create_system = old_info.create_system
                info.external_attr = old_info.external_attr
                info.internal_attr = old_info.internal_attr
                info.flag_bits = old_info.flag_bits
                normalized.writestr(info, content, compress_type=info.compress_type, compresslevel=9)
        os.replace(temporary_name, output)
        temporary_name = None
        return sha256(output)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--output", type=Path, help="write a separate normalized wheel; default is in-place")
    args = parser.parse_args()
    source = args.wheel
    output = args.output or source
    before = sha256(source)
    after = normalize_wheel(source, output)
    print(f"source_sha256={before}")
    print(f"normalized_sha256={after}")
    print(f"output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
