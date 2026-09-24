#!/usr/bin/env python3
import argparse
import subprocess
from pathlib import Path

def merge(args):
    root = Path(args.input)
    cells = sorted(p.name for p in root.iterdir() if p.is_dir())
    if not cells:
        raise SystemExit("Celfie found no cell-type directories")
    Path("cell_types.tsv").write_text("cell_type\n" + "\n".join(cells) + "\n")
    subprocess.run(["python3", str(Path(args.source) / "1_merged.py"),
                    "-i", str(root), "-o", "1_merged.txt"], check=True)


def tims(args):
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    tissues = sum(1 for line in Path(args.cell_types).read_text().splitlines()[1:] if line.strip())
    source = Path(args.source).resolve(); merged = Path(args.input).resolve()
    subprocess.run(["python3", str(source / "tim.py"), str(merged), str(output / "sample_tims.txt"),
                    "100", str(tissues), "15", "2"], check=True)
    lines = (output / "sample_tims.txt").read_text().splitlines()[1:]
    sites = []
    for line in lines:
        fields = line.split("\t")[:3]
        if len(fields) == 3:
            sites.append(f"{fields[0]}\t{int(fields[1])-250}\t{int(fields[2])+250}\n")
    windows = output / "sample_tims.txt_250"; windows.write_text("".join(sites))
    sorted_windows = output / "sample_tims.txt_sorted"
    with sorted_windows.open("w") as handle:
        subprocess.run(["bedtools", "sort", "-i", str(windows)], check=True, stdout=handle)
    selected = output / "1_merged.txt_tims"
    with selected.open("w") as handle:
        subprocess.run(["bedtools", "intersect", "-a", str(merged), "-b", str(sorted_windows)],
                       check=True, stdout=handle)
    subprocess.run(["python3", str(source / "sum_by_list.py"), str(sorted_windows), str(selected),
                    str(output / "sample_tims_summed.txt"), str(tissues)], check=True)


def main():
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="stage", required=True)
    for name in ("merge", "tims"):
        p = sub.add_parser(name)
        p.add_argument("--input", required=True); p.add_argument("--source", required=True)
        p.add_argument("--output", default="output")
        p.add_argument("--cell-types")
    args = parser.parse_args()
    (merge if args.stage == "merge" else tims)(args)


if __name__ == "__main__":
    main()
