#!/usr/bin/env python3
"""Build the manifest-derived CelFEER cell-types sidecar without mutation.

The output is an explicit runtime artifact.  This tool never writes into the
immutable reference bundle and performs no CelFEER inference.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from demethflow_core.celfeer import labels_from_manifest


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("manifest", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    labels = labels_from_manifest(manifest.get("cell_types", []))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(f"{label}\n" for label in labels), encoding="utf-8")


if __name__ == "__main__":
    main()
