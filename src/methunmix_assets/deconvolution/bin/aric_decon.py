#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import os
import random
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from memory_profiler import memory_usage
except ImportError as exc:
    print("[Error] Cannot import memory_profiler. Install memory-profiler in the ARIC environment.")
    raise exc

try:
    from ARIC import ARIC
except ImportError as exc:
    print("[Error] Cannot import ARIC. Install the ARIC Python package in the ARIC environment.")
    raise exc


def _read_beta(path: str, label: str) -> pd.DataFrame:
    frame = pd.read_csv(path, index_col=0)
    if frame.empty or frame.shape[1] < 1:
        raise ValueError(f"{label} is empty")
    if frame.index.has_duplicates or frame.columns.has_duplicates:
        raise ValueError(f"{label} has duplicate CpG IDs or column labels")
    frame = frame.apply(pd.to_numeric, errors="raise")
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(f"{label} contains NA or non-finite values")
    if (values < 0).any() or (values > 1).any():
        raise ValueError(f"{label} beta values must be in [0,1]")
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description="ARIC methylation deconvolution adapter")
    parser.add_argument("--mix", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sample_id", required=True)
    parser.add_argument("--min_overlap", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260826)
    args = parser.parse_args()

    if args.min_overlap < 1:
        raise ValueError("--min_overlap must be positive")
    random.seed(args.seed)
    np.random.seed(args.seed)

    sample_id = args.sample_id
    out_prop = f"{sample_id}_prop.csv"
    out_bench = f"{sample_id}_benchmark.csv"
    out_qc = f"{sample_id}_aric_run_qc.json"
    tmp_mix = f"tmp_aligned_mix_{sample_id}.csv"
    tmp_ref = f"tmp_aligned_ref_{sample_id}.csv"
    start = time.perf_counter()
    peak_memory_mb = 0.0
    status = "Success"
    error_msg = "None"
    run_error: Exception | None = None
    qc: dict[str, object] = {
        "schema": "demethflow-aric-run-qc-v1",
        "sample_id": sample_id,
        "seed": args.seed,
        "minimum_overlap_cpg": args.min_overlap,
        "status": "FAILED",
    }

    try:
        reference = _read_beta(args.ref, "ARIC reference")
        mixture = _read_beta(args.mix, "ARIC mixture")
        common = mixture.index.intersection(reference.index, sort=False)
        qc.update(
            reference_cpg_count=int(reference.shape[0]),
            mixture_cpg_count=int(mixture.shape[0]),
            overlap_cpg_count=int(len(common)),
            reference_cell_type_count=int(reference.shape[1]),
            mixture_sample_count=int(mixture.shape[1]),
            reference_cell_types=[str(value) for value in reference.columns],
        )
        if len(common) < args.min_overlap:
            raise ValueError(
                f"ARIC CpG overlap {len(common)} is below the frozen minimum {args.min_overlap}"
            )
        aligned_reference = reference.loc[common]
        informative = aligned_reference.max(axis=1) - aligned_reference.min(axis=1) > 0
        aligned_reference = aligned_reference.loc[informative]
        aligned_mixture = mixture.loc[aligned_reference.index]
        qc["informative_overlap_cpg_count"] = int(aligned_reference.shape[0])
        if aligned_reference.shape[0] < args.min_overlap:
            raise ValueError(
                f"ARIC informative CpG overlap {aligned_reference.shape[0]} is below {args.min_overlap}"
            )
        aligned_mixture.to_csv(tmp_mix)
        aligned_reference.to_csv(tmp_ref)

        gc.collect()
        mem_max = memory_usage(
            (ARIC, [], {
                "mix_path": tmp_mix,
                "ref_path": tmp_ref,
                "save_path": out_prop,
                "is_methylation": True,
            }),
            max_usage=True,
            interval=0.1,
            retval=False,
        )
        peak_memory_mb = max(mem_max) if isinstance(mem_max, (list, tuple)) else float(mem_max)
        if not Path(out_prop).is_file():
            raise RuntimeError("ARIC returned without producing its proportion file")
        qc["status"] = "PASS"
    except Exception as exc:
        status = "Failed"
        error_msg = str(exc)
        qc["error"] = error_msg
        run_error = exc
        traceback.print_exc()
    finally:
        for temporary in (tmp_mix, tmp_ref):
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass

    elapsed = time.perf_counter() - start
    pd.DataFrame([{
        "Sample": sample_id,
        "Time_Seconds": round(elapsed, 4),
        "Peak_Memory_MB": round(peak_memory_mb, 4),
        "Status": status,
        "Error": error_msg,
    }]).to_csv(out_bench, index=False)
    Path(out_qc).write_text(json.dumps(qc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if run_error is not None:
        raise run_error


if __name__ == "__main__":
    main()
