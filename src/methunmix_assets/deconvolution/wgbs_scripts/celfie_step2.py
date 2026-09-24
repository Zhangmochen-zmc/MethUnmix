#!/usr/bin/env python3
"""Build a CelFiE input table through an explicit coordinate-key join."""

from __future__ import annotations

import argparse
from pathlib import Path

from celfie_contract import ContractError, merge_atlas_and_mapped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mapped", type=Path)
    parser.add_argument("atlas", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--qc", type=Path)
    args = parser.parse_args()
    try:
        merge_atlas_and_mapped(args.atlas, args.mapped, args.output, args.qc)
    except ContractError as exc:
        raise SystemExit(f"CelFiE contract error: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
