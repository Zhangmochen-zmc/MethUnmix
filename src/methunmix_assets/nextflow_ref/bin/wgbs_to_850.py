#!/usr/bin/env python3
"""Python adapter for the supplied wgbs_850k/anno.sh conversion."""
import argparse
import gzip
from pathlib import Path


def open_text(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.name.endswith(".gz") else path.open("rt", encoding="utf-8")


def load_annotation(path):
    annotation = {}
    with open_text(path) as handle:
        for line_number, line in enumerate(handle, 1):
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 4:
                raise SystemExit(f"{path}:{line_number}: annotation requires at least four tab-separated columns")
            annotation[(fields[0], fields[1], fields[2])] = fields[3]
    if not annotation:
        raise SystemExit(f"empty 850K annotation: {path}")
    return annotation


def convert(source, destination, annotation):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    matched = 0
    try:
        with open_text(source) as src, temporary.open("wt", encoding="utf-8", newline="") as dst:
            for line_number, line in enumerate(src, 1):
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 6:
                    raise SystemExit(f"{source}:{line_number}: WGBS input requires at least six columns")
                probe = annotation.get((fields[0], fields[1], fields[2]))
                if probe is not None:
                    dst.write(f"{probe}\t{fields[5]}\n")
                    matched += 1
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print(f"{source} -> {destination}: {matched} matched 850K probes")


def main():
    parser = argparse.ArgumentParser(description="Convert WGBS BED files to 850K probe/beta TXT files")
    parser.add_argument("input_folder", type=Path)
    parser.add_argument("output_folder", type=Path)
    parser.add_argument("annotation_bed", type=Path)
    args = parser.parse_args()
    if not args.input_folder.is_dir():
        raise SystemExit(f"input folder does not exist: {args.input_folder}")
    if not args.annotation_bed.is_file():
        raise SystemExit(f"850K annotation does not exist: {args.annotation_bed}")

    files = sorted(set(args.input_folder.rglob("*.bed")) | set(args.input_folder.rglob("*.bed.gz")))
    if not files:
        raise SystemExit(f"no .bed or .bed.gz files below {args.input_folder}")
    annotation = load_annotation(args.annotation_bed)
    for source in files:
        relative = source.relative_to(args.input_folder)
        name = source.name[:-len(".bed.gz")] if source.name.endswith(".bed.gz") else source.stem
        convert(source, args.output_folder / relative.parent / f"{name}.txt", annotation)


if __name__ == "__main__":
    main()
