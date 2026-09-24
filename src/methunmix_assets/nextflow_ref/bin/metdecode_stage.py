#!/usr/bin/env python3
import argparse
import glob
import os
import shutil
import subprocess
from pathlib import Path

def link(source, target):
    source = Path(source).resolve(); target = Path(target)
    if target.exists() or target.is_symlink():
        return
    target.symlink_to(source, target_is_directory=source.is_dir())


def merge(args):
    root, source = Path(args.input).resolve(), Path(args.source).resolve()
    cells = sorted(p for p in root.iterdir() if p.is_dir() and list(p.glob("*.bed")))
    if len(cells) < 2:
        raise SystemExit(f"MetDecode requires at least 2 cell types containing BED files; found {len(cells)}: {[p.name for p in cells]}")
    cell_names = [p.name for p in cells]
    Path("cell_types.tsv").write_text("cell_type\n" + "\n".join(p.name for p in cells) + "\n")
    text = (source / "1_merged.py").read_text()
    start, end = text.index("INPUT_MAP = {"), text.index("\n\n\nOUTPUT_DIR", text.index("INPUT_MAP = {"))
    mapping = "INPUT_MAP = {\n" + "".join(
        f"    {p.name!r}: sorted(glob.glob({str(p / '*.bed')!r})),\n" for p in cells) + "}"
    Path("1_merged_runtime.py").write_text(text[:start] + mapping + text[end:])
    subprocess.run(["python3", "1_merged_runtime.py"], check=True)
    missing = [name for name in cell_names
               if not (Path("1_merged_data") / f"{name}.merged.bed").is_file()
               or (Path("1_merged_data") / f"{name}.merged.bed").stat().st_size == 0]
    if missing:
        raise SystemExit(f"MetDecode merge did not produce complete cell types: {missing}")
    print(f"MetDecode merge complete: expected={len(cell_names)}", flush=True)


def run_stage(args, script):
    source = Path(args.source).resolve()
    required = ["2_250_markers.bed"] if script == "2_find_dmr.py" else ["3_mapped_atlas_data"]
    if args.input:
        link(args.input, Path(args.input).name)
    if args.markers:
        link(args.markers, "2_250_markers.bed")
    subprocess.run(["python3", str(source / script)], check=True)
    if script == "3_map.py":
        cells = sorted(path.name.removesuffix(".merged.bed") for path in Path(args.input).glob("*.merged.bed"))
        incomplete = [cell for cell in cells
                      if not (Path("3_mapped_atlas_data") / f"{cell}_mapped.txt").is_file()
                      or (Path("3_mapped_atlas_data") / f"{cell}_mapped.txt").stat().st_size == 0]
        if incomplete:
            raise SystemExit(f"MetDecode map incomplete cell types: {incomplete}")
    for name in required:
        output = Path(name)
        if not output.exists() or (output.is_file() and output.stat().st_size == 0):
            raise SystemExit(f"MetDecode {args.stage} produced no usable {name}; check cell-type count and biological differences")


def finish(args):
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    final_names = ["atlas.npz", "Annotation_final.txt", "mask-balanced.npy", "mask-significant.npy", "atlas.tsv"]
    link(args.input, "3_mapped_atlas_data"); link(args.markers, "2_250_markers.bed")
    source = Path(args.source).resolve()
    for script in ("4_atlas.py", "5_filter_dmr.py", "6_tsv.py"):
        subprocess.run(["python3", str(source / script)], check=True)
    shutil.copy2(args.cell_types, output / "cell_types.tsv")
    for name in final_names:
        path = Path(name)
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(f"MetDecode missing or empty final output: {name}")
        shutil.move(str(path), output / name)


def main():
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="stage", required=True)
    for name in ("merge", "find", "map", "finish"):
        p = sub.add_parser(name); p.add_argument("--input", required=True); p.add_argument("--source", required=True)
        p.add_argument("--markers"); p.add_argument("--cell-types"); p.add_argument("--output")
    args = parser.parse_args()
    if args.stage == "merge": merge(args)
    elif args.stage == "find": run_stage(args, "2_find_dmr.py")
    elif args.stage == "map": run_stage(args, "3_map.py")
    else: finish(args)


if __name__ == "__main__":
    main()
