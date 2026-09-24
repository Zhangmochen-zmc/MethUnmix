#!/usr/bin/env python3
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd


def parallel(jobs, workers, command):
    def run(source, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(str(output) + ".partial")
        try:
            subprocess.run(command(source, partial), check=True)
            if not partial.is_file() or partial.stat().st_size == 0:
                raise RuntimeError(f"missing or empty output: {partial}")
            partial.replace(output)
        finally:
            partial.unlink(missing_ok=True)
        return source.name

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        futures = [pool.submit(run, *job) for job in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            print(f"[{index}/{len(jobs)}] completed {future.result()}", flush=True)


def reads(args):
    root, output = Path(args.input), Path(args.output)
    cells = sorted(p for p in root.iterdir() if p.is_dir())
    expected = [(p, output / cell.name / f"{p.stem}.bed")
                for cell in cells for p in sorted(cell.glob("*.txt"))]
    if not expected:
        raise SystemExit("CelFEER found no read-level TXT files")
    parallel(expected, args.workers,
             lambda source, target: ["python3", args.script, str(source), str(target)])


def bins(args):
    root, output, main = Path(args.input), Path(args.output), Path(args.celfeer_main).resolve()
    bin_file = main / "data" / ("hg19_read_bins.txt" if args.genome_build == "hg19" else "read_bins.txt")
    script = main / "scripts" / "data_processing" / "sum_reads_in_500_bins.py"
    if args.genome_build not in ("hg19", "hg38") or not script.is_file() or not bin_file.is_file():
        raise SystemExit("CelFEER genome build/resources are invalid")
    expected = [(p, output / cell.name / f"{p.stem}.window.bed")
                for cell in sorted(x for x in root.iterdir() if x.is_dir())
                for p in sorted(cell.glob("*.bed"))]
    if not expected:
        raise SystemExit("CelFEER found no converted BED files")
    parallel(expected, args.workers,
             lambda source, target: ["python3", str(script), str(bin_file), str(source), str(target)])


def markers(args):
    root, output, main = Path(args.input), Path(args.output), Path(args.celfeer_main).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cells = sorted(p for p in root.iterdir() if p.is_dir())
    if not cells:
        raise SystemExit("CelFEER found no binned cell directories")
    (output / "cell_types.tsv").write_text("cell_type\n" + "\n".join(p.name for p in cells) + "\n")
    merged_cells = []
    (output / "3_ref_merge").mkdir()
    for cell in cells:
        files = sorted(cell.glob("*.window.bed"))
        if not files:
            raise SystemExit(f"CelFEER has no binned replicates for {cell.name}")
        merged = pd.read_csv(files[0], sep="\t", header=None)
        for path in files[1:]:
            current = pd.read_csv(path, sep="\t", header=None)
            if current.shape != merged.shape or not np.array_equal(merged.iloc[:, :3], current.iloc[:, :3]):
                raise SystemExit(f"CelFEER coordinate mismatch: {path}")
            merged.iloc[:, 3:] = merged.iloc[:, 3:].to_numpy() + current.iloc[:, 3:].to_numpy()
        merged.to_csv(output / "3_ref_merge" / f"{cell.name}_merged.txt", sep="\t", header=False, index=False)
        merged_cells.append(merged)
    coordinates = merged_cells[0].iloc[:, :3].to_numpy()
    if any(not np.array_equal(coordinates, frame.iloc[:, :3].to_numpy()) for frame in merged_cells[1:]):
        raise SystemExit("CelFEER coordinates differ between cell types")
    pd.concat([merged_cells[0]] + [x.iloc[:, 3:] for x in merged_cells[1:]], axis=1).to_csv(
        output / "4_ref.txt", sep="\t", header=False, index=False)
    marker_script = main / "scripts" / "markers.py"
    subprocess.run(["python3", str(marker_script), "4_ref.txt", "markers.tsv", "100",
                    str(len(cells)), "20", "0", "True", "hypomin"], check=True, cwd=output)
    subprocess.run(["python3", str(Path(args.match_script).resolve())], check=True, cwd=output)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="stage", required=True)
    for name in ("reads", "bins", "markers"):
        p = sub.add_parser(name)
        p.add_argument("--input", required=True); p.add_argument("--output", required=True)
        p.add_argument("--workers", type=int, default=1)
        p.add_argument("--celfeer-main"); p.add_argument("--genome-build")
        p.add_argument("--script"); p.add_argument("--match-script")
    args = parser.parse_args()
    {"reads": reads, "bins": bins, "markers": markers}[args.stage](args)


if __name__ == "__main__":
    main()
