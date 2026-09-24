#!/usr/bin/env python3
"""Pinned-worker helpers for DeMethFlow BAM WGBS preprocessing."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from demethflow_core.errors import DeMethFlowError
from demethflow_core.wgbs_bam import lbeta_to_bed, stage_primary_bam, validate_bam_header


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-bam")
    validate.add_argument("--bam", type=Path, required=True)
    validate.add_argument("--chrom-sizes", type=Path, required=True)
    validate.add_argument("--output", type=Path, required=True)
    validate.add_argument("--contig-policy", choices=("strict", "primary-only"), default="strict")
    validate.add_argument("--threads", type=int, default=1)
    stage = sub.add_parser("stage-primary-bam")
    stage.add_argument("--bam", type=Path, required=True)
    stage.add_argument("--chrom-sizes", type=Path, required=True)
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument("--qc-output", type=Path, required=True)
    stage.add_argument("--threads", type=int, required=True)
    stage.add_argument("--sort-memory", required=True)
    stage.add_argument("--preflight-qc", type=Path, required=True)
    convert = sub.add_parser("lbeta-to-bed")
    convert.add_argument("--lbeta", type=Path, required=True)
    convert.add_argument("--cpg-dictionary", type=Path, required=True)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--sample-id", required=True)
    convert.add_argument("--genome-build", choices=("hg19", "hg38"), required=True)
    convert.add_argument("--qc-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate-bam":
            payload = validate_bam_header(
                args.bam, args.chrom_sizes, contig_policy=args.contig_policy, threads=args.threads,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif args.command == "stage-primary-bam":
            try:
                preflight = json.loads(args.preflight_qc.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DeMethFlowError(f"BAM_PREPROCESS_FAILED: unreadable primary-only preflight: {exc}") from exc
            if not isinstance(preflight, dict):
                raise DeMethFlowError("BAM_PREPROCESS_FAILED: primary-only preflight must be a JSON object")
            stage_primary_bam(
                args.bam, args.chrom_sizes, args.output, args.qc_output, threads=args.threads,
                sort_memory=args.sort_memory, source_preflight=preflight,
            )
        else:
            lbeta_to_bed(
                lbeta=args.lbeta,
                cpg_dictionary=args.cpg_dictionary,
                output=args.output,
                sample_id=args.sample_id,
                genome_build=args.genome_build,
                qc_output=args.qc_output,
            )
    except DeMethFlowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
