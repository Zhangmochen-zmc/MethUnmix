#!/usr/bin/env python3
"""Validate and normalize user-supplied MEnet or CelFEER-ready references."""

import argparse
import csv
import gzip
import math
from pathlib import Path


def patterns(input_format):
    return ('*.bismark.cov', '*.bismark.cov.gz') if input_format == 'menet' else ('*.txt',)


def sample_id(path, input_format):
    name = path.name
    suffixes = ('.bismark.cov.gz', '.bismark.cov') if input_format == 'menet' else ('.txt',)
    for suffix in suffixes:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return path.stem


def find_files(root, input_format):
    found = []
    for pattern in patterns(input_format):
        found.extend(Path(root).rglob(pattern))
    return sorted({path for path in found if path.is_file()})


def inspect_menet(path):
    opener = gzip.open if path.name.endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            fields = raw.rstrip('\n').split('\t')
            if len(fields) != 6:
                raise SystemExit(f'{path}:{line_number}: MEnet/Bismark input requires 6 tab-separated columns')
            try:
                start, end = int(fields[1]), int(fields[2])
                percentage = float(fields[3]); methylated = float(fields[4]); unmethylated = float(fields[5])
            except ValueError:
                raise SystemExit(f'{path}:{line_number}: invalid numeric MEnet/Bismark field')
            if start < 0 or end < start or not 0 <= percentage <= 100 or methylated < 0 or unmethylated < 0:
                raise SystemExit(f'{path}:{line_number}: invalid MEnet/Bismark coordinates, percentage, or counts')
            total = methylated + unmethylated
            if total and not math.isclose(100 * methylated / total, percentage, abs_tol=0.11):
                raise SystemExit(f'{path}:{line_number}: percentage does not match methylated/(methylated+unmethylated)')
            return
    raise SystemExit(f'{path}: empty MEnet/Bismark input')


def inspect_celfeer(path, validation_lines):
    with path.open(encoding='utf-8') as handle:
        checked = 0
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            checked += 1
            fields = raw.split()
            if len(fields) < 4:
                raise SystemExit(f'{path}:{line_number}: CelFEER read-level input requires at least 4 columns')
            read_id, state, chrom, coordinate = fields[:4]
            if not read_id or state not in ('+', '-') or not chrom:
                raise SystemExit(f'{path}:{line_number}: invalid CelFEER read ID, methylation state, or chromosome')
            try:
                coordinate = int(coordinate)
            except ValueError:
                raise SystemExit(f'{path}:{line_number}: invalid CelFEER coordinate')
            if coordinate < 0:
                raise SystemExit(f'{path}:{line_number}: CelFEER coordinate must be non-negative')
            if checked >= validation_lines:
                break
        if not checked:
            raise SystemExit(f'{path}: empty CelFEER read-level input')


def read_metadata(path):
    with open(path, newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def validate_metadata(rows, sid_column, cell_column):
    if not rows:
        raise SystemExit('Metadata is empty')
    missing = [column for column in (sid_column, cell_column) if column not in rows[0]]
    if missing:
        raise SystemExit(f'Metadata missing columns: {missing}')
    ids = [row[sid_column].strip() for row in rows]
    cells = [row[cell_column].strip() for row in rows]
    if any(not value for value in ids + cells):
        raise SystemExit('Metadata contains an empty sample ID or cell type')
    if len(ids) != len(set(ids)):
        raise SystemExit('Metadata contains duplicate sample IDs')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('discover', 'validate'))
    parser.add_argument('--reference-root', required=True)
    parser.add_argument('--metadata')
    parser.add_argument('--format', required=True, choices=('menet', 'celfeer'))
    parser.add_argument('--sample-id-column', required=True)
    parser.add_argument('--cell-type-column', required=True)
    parser.add_argument('--output-root')
    parser.add_argument('--output-metadata', required=True)
    parser.add_argument('--manifest')
    parser.add_argument('--celfeer-validation-lines', type=int, default=1000)
    args = parser.parse_args()

    root = Path(args.reference_root)
    files = find_files(root, args.format)
    if not files:
        expected = 'bismark.cov[.gz]' if args.format == 'menet' else 'read-level .txt'
        raise SystemExit(f'No {expected} files found below {root}')
    indexed = {}
    for path in files:
        sid = sample_id(path, args.format)
        if sid in indexed:
            raise SystemExit(f'Duplicate preconverted sample ID {sid}: {indexed[sid]} and {path}')
        indexed[sid] = path

    if args.mode == 'discover':
        rows = [{args.sample_id_column: sid, args.cell_type_column: path.parent.name}
                for sid, path in indexed.items()]
    else:
        if not args.metadata:
            raise SystemExit('validate requires --metadata')
        rows = read_metadata(args.metadata)
        validate_metadata(rows, args.sample_id_column, args.cell_type_column)
        requested = {row[args.sample_id_column].strip() for row in rows}
        missing = sorted(requested - set(indexed)); extra = sorted(set(indexed) - requested)
        if missing:
            raise SystemExit(f'Metadata samples without preconverted files: {missing[:20]}')
        if extra:
            raise SystemExit(f'Preconverted files absent from metadata: {extra[:20]}')

    validate_metadata(rows, args.sample_id_column, args.cell_type_column)
    if args.mode == 'discover':
        with open(args.output_metadata, 'w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
        return

    if args.celfeer_validation_lines < 1:
        raise SystemExit('--celfeer-validation-lines must be at least 1')
    records = []
    normalized_rows = []
    for row in rows:
        sid = row[args.sample_id_column].strip(); cell = row[args.cell_type_column].strip(); source = indexed[sid]
        inspect_menet(source) if args.format == 'menet' else inspect_celfeer(source, args.celfeer_validation_lines)
        if args.format == 'celfeer' and source.parent.name != cell:
            raise SystemExit(
                f'CelFEER sample {sid} metadata cell type {cell!r} does not match '
                f'its parent directory {source.parent.name!r}; direct-input mode requires matching names'
            )
        normalized_rows.append(dict(row)); records.append((sid, cell, str(source), str(source), args.format))
    with open(args.output_metadata, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=normalized_rows[0].keys()); writer.writeheader(); writer.writerows(normalized_rows)
    with open(args.manifest, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle, delimiter='\t'); writer.writerow(('sample_id', 'cell_type', 'source', 'normalized', 'format')); writer.writerows(records)


if __name__ == '__main__':
    main()
