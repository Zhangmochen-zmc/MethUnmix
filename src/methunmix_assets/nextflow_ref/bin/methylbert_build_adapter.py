#!/usr/bin/env python3
"""Prepare leakage-safe MethylBERT train/test files and compact runtime metadata."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
from collections import Counter
from pathlib import Path


REQUIRED = ("dna_seq", "methyl_seq", "dmr_label", "dmr_ctype", "dmr_coordinates", "ctype")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def metadata_rows(path: Path) -> list[tuple[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        except csv.Error:
            dialect = csv.excel_tab
        reader = csv.DictReader(handle, dialect=dialect)
        normalized = {name.strip().lower(): name for name in (reader.fieldnames or [])}
        sample_col = next((normalized[key] for key in ("gsm", "sample_id", "sample", "name") if key in normalized), None)
        cell_col = next((normalized[key] for key in ("cell_type", "celltype", "group") if key in normalized), None)
        if not sample_col or not cell_col:
            raise SystemExit("metadata requires GSM/sample_id and cell_type columns")
        rows = [(str(row.get(sample_col, "")).strip(), str(row.get(cell_col, "")).strip()) for row in reader]
    if not rows or any(not sample or not cell for sample, cell in rows):
        raise SystemExit("metadata contains an empty sample or cell type")
    return rows


def stable_order(value: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}\0{value}".encode()).hexdigest()


def source_stem(path: Path) -> str:
    name = path.name
    for suffix in ("_reads.csv", ".reads.csv", ".csv"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def prepare(args: argparse.Namespace) -> None:
    root = Path(args.reads_root).resolve()
    output = Path(args.output).resolve()
    rows = metadata_rows(Path(args.metadata))
    found = {source_stem(path): path for path in root.rglob("*_reads.csv")}
    grouped: dict[str, list[tuple[str, Path]]] = {}
    for sample, cell in rows:
        path = found.get(sample)
        if not path:
            raise SystemExit(f"metadata sample has no matching *_reads.csv file: {sample}")
        grouped.setdefault(cell, []).append((sample, path))
    train_samples: set[str] = set()
    test_samples: set[str] = set()
    for cell, records in grouped.items():
        if len(records) < 2:
            raise SystemExit(f"MethylBERT needs at least two independent source samples for cell type {cell!r}")
        ordered = sorted(records, key=lambda item: stable_order(item[0], args.seed))
        test_count = max(1, min(len(ordered) - 1, math.ceil(len(ordered) * args.test_fraction)))
        test_samples.update(sample for sample, _ in ordered[:test_count])
        train_samples.update(sample for sample, _ in ordered[test_count:])

    output.mkdir(parents=True, exist_ok=False)
    train_path = output / "train_seq.kmers_fixed.csv"
    test_path = output / "test_seq.kmers_fixed.csv"
    mappings: dict[int, tuple[str, str]] = {}
    counts: Counter[str] = Counter()
    split_rows = []
    handles = {
        "train": train_path.open("w", newline="", encoding="utf-8"),
        "test": test_path.open("w", newline="", encoding="utf-8"),
    }
    writers: dict[str, csv.DictWriter] = {}
    try:
        for sample, declared_cell in rows:
            source = found[sample]
            split = "test" if sample in test_samples else "train"
            with source.open(newline="", encoding="utf-8-sig") as source_handle:
                reader = csv.DictReader(source_handle, delimiter="\t")
                fields = reader.fieldnames or []
                missing = [name for name in REQUIRED if name not in fields]
                if missing:
                    raise SystemExit(f"{source} is missing columns: {', '.join(missing)}")
                if split not in writers:
                    writers[split] = csv.DictWriter(handles[split], fieldnames=fields, delimiter="\t", lineterminator="\n")
                    writers[split].writeheader()
                row_count = 0
                for row in reader:
                    if str(row["ctype"]).strip() != declared_cell:
                        raise SystemExit(f"{source}: ctype {row['ctype']!r} conflicts with metadata {declared_cell!r}")
                    kmers = str(row["dna_seq"]).strip().split()[: args.seq_len]
                    row["dna_seq"] = " ".join(kmers)
                    row["methyl_seq"] = str(row["methyl_seq"])[: len(kmers)]
                    label = int(row["dmr_label"])
                    mapping = (str(row["dmr_ctype"]), str(row["dmr_coordinates"]))
                    if label in mappings and mappings[label] != mapping:
                        raise SystemExit(f"DMR {label} has conflicting definitions")
                    mappings[label] = mapping
                    writers[split].writerow(row)
                    row_count += 1
                    if split == "train":
                        counts[declared_cell] += 1
            if row_count == 0:
                raise SystemExit(f"source sample contains no reads: {source}")
            split_rows.append({"sample": sample, "cell_type": declared_cell, "split": split, "reads": row_count, "source_sha256": sha256(source)})
    finally:
        for handle in handles.values():
            handle.close()
    labels = sorted(mappings)
    if labels != list(range(len(labels))):
        raise SystemExit("DMR labels must be the dense range 0..N-1")
    with (output / "markers_with_id.tsv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["chr", "start", "end", "target", "dmr_id"])
        for label in labels:
            target, coordinate = mappings[label]
            chrom, interval = coordinate.split(":", 1)
            start, end = interval.split("-", 1)
            writer.writerow([chrom, start, end, target, label])
    total = sum(counts.values())
    runtime = {
        "schema": "demethflow-methylbert-runtime-v1",
        "scenario_id": args.scenario,
        "genome_build": args.genome_build,
        "num_dmrs": len(labels),
        "n_mers": 3,
        "seq_len": args.seq_len,
        "cell_types": [
            {"name": name, "train_count": count, "prior": count / total}
            for name, count in sorted(counts.items())
        ],
        "source_checksums": {"train": sha256(train_path), "test": sha256(test_path)},
        "validation_scope": "independent_source_sample_split",
    }
    (output / "runtime_metadata.json").write_text(json.dumps(runtime, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output / "split_manifest.json").write_text(
        json.dumps({"schema": "demethflow-methylbert-split-v1", "seed": args.seed, "samples": split_rows}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def finalize(args: argparse.Namespace) -> None:
    prepared = Path(args.prepared).resolve()
    model = Path(args.model).resolve()
    output = Path(args.output).resolve()
    required_model = model / "bert.model" / "model.safetensors"
    if not required_model.is_file() or not (model / "train_param.txt").is_file():
        raise SystemExit("MethylBERT fine-tuning did not produce a complete model")
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(model, output / "model")
    for filename in ("dmr_encoder.pickle", "read_classification_model.pickle"):
        target = output / "model" / filename
        nested = output / "model" / "bert.model" / filename
        if not target.is_file() and nested.is_file():
            shutil.copy2(nested, target)
        if not target.is_file():
            raise SystemExit(f"MethylBERT fine-tuning output is missing {filename}")
    shutil.copy2(prepared / "markers_with_id.tsv", output / "markers_with_id.tsv")
    runtime = json.loads((prepared / "runtime_metadata.json").read_text(encoding="utf-8"))
    runtime["model_sha256"] = sha256(required_model)
    runtime["dmr_encoder_sha256"] = sha256(output / "model" / "dmr_encoder.pickle")
    runtime["read_classifier_sha256"] = sha256(output / "model" / "read_classification_model.pickle")
    runtime["markers_sha256"] = sha256(output / "markers_with_id.tsv")
    (output / "runtime_metadata.json").write_text(json.dumps(runtime, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    split = json.loads((prepared / "split_manifest.json").read_text(encoding="utf-8"))
    shutil.copy2(prepared / "split_manifest.json", output / "split_manifest.json")
    train = {record["sample"] for record in split["samples"] if record["split"] == "train"}
    test = {record["sample"] for record in split["samples"] if record["split"] == "test"}
    qc = {
        "schema": "demethflow-methylbert-reference-qc-v1",
        "validation_scope": "independent_source_sample_split",
        "source_sample_overlap": sorted(train & test),
        "train_samples": len(train),
        "test_samples": len(test),
        "num_dmrs": runtime["num_dmrs"],
        "model_sha256": runtime["model_sha256"],
        "split_manifest": split,
    }
    (output / "qc.json").write_text(json.dumps(qc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--reads-root", required=True)
    prep.add_argument("--metadata", required=True)
    prep.add_argument("--scenario", required=True)
    prep.add_argument("--genome-build", default="hg38", choices=("hg19", "hg38"))
    prep.add_argument("--output", required=True)
    prep.add_argument("--seed", type=int, default=123)
    prep.add_argument("--test-fraction", type=float, default=0.2)
    prep.add_argument("--seq-len", type=int, default=160)
    prep.set_defaults(handler=prepare)
    finish = sub.add_parser("finalize")
    finish.add_argument("--prepared", required=True)
    finish.add_argument("--model", required=True)
    finish.add_argument("--output", required=True)
    finish.set_defaults(handler=finalize)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
