#!/usr/bin/env python3
"""Stage UXM construction inputs and create a lossless atlas QC report."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_metadata(path: Path) -> list[tuple[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        except csv.Error:
            dialect = csv.excel_tab
        reader = csv.DictReader(handle, dialect=dialect)
        fields = reader.fieldnames or []
        normalized = {name.strip().lower(): name for name in fields}
        sample_col = next((normalized[key] for key in ("name", "gsm", "sample_id", "sample") if key in normalized), None)
        group_col = next((normalized[key] for key in ("group", "cell_type", "celltype") if key in normalized), None)
        if not sample_col or not group_col:
            raise SystemExit("metadata requires name/GSM/sample_id and group/cell_type columns")
        rows = [(str(row.get(sample_col, "")).strip(), str(row.get(group_col, "")).strip()) for row in reader]
    if not rows or any(not name or not group for name, group in rows):
        raise SystemExit("metadata contains an empty sample or cell type")
    if len({name for name, _ in rows}) != len(rows):
        raise SystemExit("metadata sample identifiers must be unique")
    return rows


def pat_stem(path: Path) -> str:
    name = path.name
    return name[:-7] if name.endswith(".pat.gz") else path.stem


def stage(args: argparse.Namespace) -> None:
    source = Path(args.pat_root).resolve()
    output = Path(args.output).resolve()
    rows = read_metadata(Path(args.metadata))
    pats = {pat_stem(path): path for path in source.rglob("*.pat.gz")}
    if not pats:
        raise SystemExit(f"no *.pat.gz files found under {source}")
    output.mkdir(parents=True, exist_ok=False)
    pat_dir = output / "pat"
    pat_dir.mkdir()
    manifest = []
    for sample, group in rows:
        candidate = pats.get(sample) or pats.get(sample.removesuffix(".pat"))
        if candidate is None:
            raise SystemExit(f"metadata sample has no matching PAT: {sample}")
        target = pat_dir / f"{sample}.pat.gz"
        try:
            os.link(candidate, target)
        except OSError:
            shutil.copy2(candidate, target)
        csi = Path(f"{candidate}.csi")
        if csi.is_file():
            shutil.copy2(csi, Path(f"{target}.csi"))
        manifest.append({"sample": sample, "group": group, "source": str(candidate), "sha256": digest(candidate)})
    with (output / "uxm_wgbs_meta.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "group"])
        writer.writerows(rows)
    (output / "input_manifest.json").write_text(
        json.dumps({"schema": "demethflow-uxm-build-input-v1", "samples": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )


def finalize(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    output = Path(args.output).resolve()
    atlases = list((work / "atlas").glob("*_marker_atlas.csv"))
    if len(atlases) != 1:
        raise SystemExit(f"expected one UXM atlas, found {len(atlases)}")
    output.mkdir(parents=True, exist_ok=False)
    atlas = output / "marker_atlas.tsv"
    shutil.copy2(atlases[0], atlas)
    shutil.copy2(work / "input_manifest.json", output / "input_manifest.json")
    with atlas.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows:
        raise SystemExit("UXM atlas is empty")
    cells = list(rows[0])[8:]
    any_na = all_na = 0
    per_target: dict[str, dict[str, int]] = {}
    for row in rows:
        values = [str(row.get(cell, "")).strip().upper() for cell in cells]
        flags = [value in {"", "NA", "NAN"} for value in values]
        any_na += int(any(flags))
        all_na += int(all(flags))
        target = str(row.get("target", ""))
        record = per_target.setdefault(target, {"total": 0, "fully_numeric": 0})
        record["total"] += 1
        record["fully_numeric"] += int(not any(flags))
    qc = {
        "schema": "demethflow-uxm-reference-qc-v1",
        "total_markers": len(rows),
        "markers_with_any_na": any_na,
        "markers_all_na": all_na,
        "fully_numeric_markers": len(rows) - any_na,
        "cell_types": cells,
        "per_target": per_target,
        "na_policy": "NA markers are preserved in the immutable atlas and reported; they do not prevent UXM result generation.",
        "atlas_sha256": digest(atlas),
    }
    (output / "qc.json").write_text(json.dumps(qc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--pat-root", required=True)
    prepare.add_argument("--metadata", required=True)
    prepare.add_argument("--output", required=True)
    prepare.set_defaults(handler=stage)
    finish = sub.add_parser("finalize")
    finish.add_argument("--work", required=True)
    finish.add_argument("--output", required=True)
    finish.set_defaults(handler=finalize)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
