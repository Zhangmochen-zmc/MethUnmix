#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CelFEER using the sample count recorded by preprocessing")
    parser.add_argument("matrix", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("tool_root", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.metadata.read_text(encoding="utf-8"))
    sample_count = payload.get("sample_count")
    sample_ids = payload.get("sample_ids")
    if not isinstance(sample_count, int) or sample_count < 1:
        raise ValueError("CelFEER input metadata has no positive integer sample_count")
    if not isinstance(sample_ids, list) or len(sample_ids) != sample_count or len(set(sample_ids)) != sample_count:
        raise ValueError("CelFEER input metadata sample_ids do not match sample_count")
    main_script = args.tool_root / "scripts" / "celfeer.py"
    if not main_script.is_file():
        raise FileNotFoundError(f"CelFEER executable is missing: {main_script}")
    subprocess.run(
        ["python3", str(main_script), str(args.matrix), str(args.output_dir), str(sample_count)],
        check=True,
    )


if __name__ == "__main__":
    main()
