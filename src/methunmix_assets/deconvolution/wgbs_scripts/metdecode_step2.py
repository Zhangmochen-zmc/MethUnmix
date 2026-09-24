#!/usr/bin/env python3
"""Build a strict MetDecode cfDNA table by exact genomic-coordinate key."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metdecode_contract import ContractError, merge_cfdna


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mapped")
    parser.add_argument("atlas")
    parser.add_argument("output")
    parser.add_argument("sample_id")
    parser.add_argument("qc")
    args = parser.parse_args()
    try:
        merge_cfdna(args.atlas, args.mapped, args.output, args.sample_id, args.qc)
    except ContractError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
