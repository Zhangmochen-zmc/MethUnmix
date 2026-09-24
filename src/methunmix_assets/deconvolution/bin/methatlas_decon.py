#!/usr/bin/env python3
import os
import subprocess
import argparse
import time
import gc
import pandas as pd
from memory_profiler import memory_usage

def run_script(cmd):
    """
    运行外部 deconvolve.py 脚本
    """
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def main():
    parser = argparse.ArgumentParser(description="Wrapper for deconvolve.py with benchmarking")
    parser.add_argument('--mix', required=True, help="Input mix CSV file")
    parser.add_argument('--atlas', required=True, help="Reference Atlas CSV")
    parser.add_argument('--script', required=True, help="Path to deconvolve.py")
    parser.add_argument('--sample_id', required=True, help="Sample ID")
    parser.add_argument('--workers', required=True, type=int,
                        help="Maximum MethAtlas sample workers; supplied from Nextflow task.cpus")
    parser.add_argument('--plot', action='store_true')
    parser.add_argument('--slim', action='store_true')
    parser.add_argument('--residuals', action='store_true')
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be a positive integer")

    # 构造命令
    cmd = [
        "python", args.script,
        "-a", args.atlas,
        args.mix,
        "-o", ".", # 直接输出到当前工作目录
        "--workers", str(args.workers)
    ]
    if args.plot: cmd.append("--plot")
    if args.slim: cmd.append("--slim")
    if args.residuals: cmd.append("--residuals")

    gc.collect()
    start_time = time.perf_counter()
    peak_memory = 0
    status = "Success"
    error_msg = "None"
    run_error = None

    try:
        # 监控子进程内存
        peak = memory_usage(
            (run_script, (cmd,)),
            max_usage=True,
            interval=0.1,
            retval=False,
            include_children=True 
        )
        peak_memory = max(peak) if isinstance(peak, (list, tuple)) else peak

    except subprocess.CalledProcessError as e:
        status = "Failed"
        error_msg = e.stderr.decode() if e.stderr else str(e)
        run_error = e
    except Exception as e:
        status = "Failed"
        error_msg = str(e)
        run_error = e

    end_time = time.perf_counter()
    elapsed = end_time - start_time

    # 保存性能指标
    bench_df = pd.DataFrame([{
        "Sample": args.sample_id,
        "Time_Seconds": round(elapsed, 4),
        "Peak_Memory_MB": round(peak_memory, 4),
        "Status": status,
        "Error": error_msg
    }])
    bench_df.to_csv(f"{args.sample_id}_benchmark.csv", index=False)
    if run_error is not None:
        raise run_error

if __name__ == "__main__":
    main()
