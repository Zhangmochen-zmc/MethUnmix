#!/usr/bin/env python3
import argparse, csv, math, os, shutil
from pathlib import Path

def read_meta(path):
    with open(path, newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))

def extensions_for(data_type):
    return ('*.txt',) if data_type in ('450k', 'epic') else ('*.bed', '*.bed.gz')

def strip_sample_suffix(name):
    for suffix in ('.bed.gz', '.txt.gz', '.bed', '.txt'):
        if name.endswith(suffix): return name[:-len(suffix)]
    return name

def discover_rows(root, data_type, sid_column, cell_column):
    paths=[]
    for pattern in extensions_for(data_type): paths.extend(Path(root).rglob(pattern))
    rows=[]; seen=set()
    for path in sorted(set(paths)):
        if not path.is_file() or path.parent == Path(root):
            continue
        sample_id=strip_sample_suffix(path.name)
        if sample_id in seen: raise SystemExit(f'Duplicate discovered sample ID: {sample_id}')
        seen.add(sample_id)
        rows.append({sid_column: sample_id, cell_column: path.parent.name})
    if not rows: raise SystemExit(f'No sample files found below cell-type directories in {root}')
    return rows

def locate_samples(root, data_type, metadata):
    found = {}
    patterns = extensions_for(data_type)
    metadata_path = Path(metadata).resolve()
    paths = []
    for pattern in patterns:
        paths.extend(Path(root).rglob(pattern))
    for path in paths:
        if path.is_file():
            if path.resolve() == metadata_path:
                continue
            # id.txt is the workflow's conventional metadata file, never a sample.
            if path.parent.resolve() == Path(root).resolve() and path.name.lower() == 'id.txt':
                continue
            sample = strip_sample_suffix(path.name)
            if sample in found:
                raise SystemExit(f'Duplicate sample ID: {sample}: {found[sample]} and {path}')
            found[sample] = path
    return found

def validate_wgbs(path, validation_lines):
    opener = __import__('gzip').open if str(path).endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as handle:
        checked = 0
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            checked += 1
            fields = line.rstrip('\n').split('\t')
            if len(fields) != 6:
                raise SystemExit(f'{path}:{line_number}: expected 6 tab-separated WGBS columns, found {len(fields)}')
            try:
                start, end = int(fields[1]), int(fields[2])
                value4, value5, ratio = float(fields[3]), float(fields[4]), float(fields[5])
            except ValueError:
                raise SystemExit(f'{path}:{line_number}: invalid numeric WGBS field')
            if start < 0 or end <= start:
                raise SystemExit(f'{path}:{line_number}: invalid genomic interval {start}-{end}')
            if value4 < 0 or value5 < 0 or not 0 <= ratio <= 1:
                raise SystemExit(f'{path}:{line_number}: counts must be non-negative and ratio must be in [0,1]')
            if value5 > 0 and not math.isclose(value4 / value5, ratio, abs_tol=0.001):
                raise SystemExit(f'{path}:{line_number}: column 6 does not match column 4 / column 5')
            if checked >= validation_lines:
                break
        if not checked:
            raise SystemExit(f'{path}: empty WGBS sample file')

def validate_rows(rows, sid, cell):
    if not rows: raise SystemExit('Metadata is empty')
    missing = [x for x in (sid, cell) if x not in rows[0]]
    if missing: raise SystemExit(f'Metadata missing columns: {missing}')
    ids = [r[sid].strip() for r in rows]
    if any(not x for x in ids): raise SystemExit('Metadata contains an empty sample ID')
    if len(ids) != len(set(ids)): raise SystemExit('Metadata contains duplicate sample IDs')

def match_metadata_samples(rows, sid_column, discovered):
    matched = {}
    used = set()
    for row in rows:
        sample_id = row[sid_column].strip()
        candidates = []
        if sample_id in discovered:
            candidates = [discovered[sample_id]]
        else:
            candidates = [path for stem, path in discovered.items() if stem.startswith(sample_id + '_')]
        if not candidates:
            raise SystemExit(f'Metadata sample has no matching file: {sample_id}')
        if len(candidates) > 1:
            raise SystemExit(f'Metadata sample matches multiple files: {sample_id}: {candidates[:10]}')
        path = candidates[0]
        if path in used:
            raise SystemExit(f'Sample file matched more than once: {path}')
        matched[sample_id] = path
        used.add(path)
    extras = sorted(str(path) for path in set(discovered.values()) - used)
    if extras:
        raise SystemExit(f'Reference files absent from metadata ({len(extras)}): {extras[:20]}')
    return matched

def write_meta(rows, out):
    with open(out, 'w', newline='', encoding='utf-8') as h:
        w=csv.DictWriter(h, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)

def main():
    p=argparse.ArgumentParser(); p.add_argument('mode', choices=['discover','validate','array','menet'])
    p.add_argument('--reference-root', required=True); p.add_argument('--metadata')
    p.add_argument('--data-type'); p.add_argument('--sample-id-column', required=True); p.add_argument('--cell-type-column', required=True)
    p.add_argument('--output-root'); p.add_argument('--output-metadata', required=True); p.add_argument('--manifest'); p.add_argument('--matrix')
    p.add_argument('--wgbs-validation-lines', type=int, default=1000)
    a=p.parse_args()
    effective_type = a.data_type or '450k'
    if a.mode == 'discover':
        rows=discover_rows(a.reference_root,effective_type,a.sample_id_column,a.cell_type_column)
        write_meta(rows,a.output_metadata)
        return
    if not a.metadata: raise SystemExit(f'{a.mode} requires --metadata')
    rows=read_meta(a.metadata); validate_rows(rows,a.sample_id_column,a.cell_type_column)
    discovered=locate_samples(a.reference_root, effective_type, a.metadata)
    samples=match_metadata_samples(rows, a.sample_id_column, discovered)
    wanted={r[a.sample_id_column].strip() for r in rows}
    if a.mode == 'validate' and effective_type == 'wgbs':
        if a.wgbs_validation_lines < 1: raise SystemExit('--wgbs-validation-lines must be at least 1')
        for sample_id in sorted(wanted):
            validate_wgbs(samples[sample_id], a.wgbs_validation_lines)

    if a.mode=='menet':
        out=[]
        for r in rows:
            sid=r[a.sample_id_column].strip(); rel=samples[sid].parent.relative_to(Path(a.reference_root))
            tissue=r[a.cell_type_column].strip()
            out.append({'FileID':sid,'Directory':str(rel).replace(os.sep,'/'),'Tissue':tissue,'MinorGroup':tissue})
        with open(a.output_metadata,'w',newline='',encoding='utf-8') as h:
            w=csv.DictWriter(h,fieldnames=['FileID','Directory','Tissue','MinorGroup']); w.writeheader(); w.writerows(out)
        return

    if a.mode == 'validate':
        normalized=[]; manifest=[]
        for r in rows:
            sid=r[a.sample_id_column].strip(); cell=r[a.cell_type_column].strip(); src=samples[sid]
            normalized.append(dict(r)); manifest.append((sid,cell,str(src),str(src)))
        write_meta(normalized,a.output_metadata)
        if a.manifest:
            with open(a.manifest,'w',newline='',encoding='utf-8') as h:
                w=csv.writer(h,delimiter='\t'); w.writerow(['sample_id','cell_type','source','normalized']); w.writerows(manifest)
        return

    if not a.output_root: raise SystemExit(f'{a.mode} requires --output-root')
    Path(a.output_root).mkdir(parents=True,exist_ok=True)
    normalized=[]
    manifest=[]
    for r in rows:
        sid=r[a.sample_id_column].strip(); cell=r[a.cell_type_column].strip(); src=samples[sid]
        dest_dir=Path(a.output_root)/src.parent.name; dest_dir.mkdir(parents=True,exist_ok=True)
        dest=dest_dir/src.name; shutil.copy2(src,dest)
        normalized.append(dict(r)); manifest.append((sid,cell,str(src),str(dest)))
    write_meta(normalized,a.output_metadata)
    if a.manifest:
        with open(a.manifest,'w',newline='',encoding='utf-8') as h:
            w=csv.writer(h,delimiter='\t'); w.writerow(['sample_id','cell_type','source','normalized']); w.writerows(manifest)
    if a.mode=='array':
        values={}; order=[]
        for sid,_,_,dest in manifest:
            sample={}
            with open(dest,encoding='utf-8') as h:
                for n,line in enumerate(h,1):
                    f=line.rstrip('\n').split('\t')
                    if len(f)!=2: raise SystemExit(f'{dest}:{n}: expected 2 tab-separated columns')
                    try: val=float(f[1])
                    except ValueError: raise SystemExit(f'{dest}:{n}: non-numeric beta value')
                    if f[0] in sample: raise SystemExit(f'{dest}:{n}: duplicate probe {f[0]}')
                    sample[f[0]]=val
            if not order: order=list(sample)
            values[sid]=sample
        common=[probe for probe in order if all(probe in values[s] for s in values)]
        with open(a.matrix,'w',newline='',encoding='utf-8') as h:
            w=csv.writer(h,delimiter='\t'); w.writerow(['probe_id']+list(values))
            for probe in common: w.writerow([probe]+[values[s][probe] for s in values])
        shutil.copy2(a.matrix, Path(a.output_root)/'reference_matrix.tsv')
        shutil.copy2(a.output_metadata, Path(a.output_root)/'reference_metadata.csv')
if __name__=='__main__': main()
