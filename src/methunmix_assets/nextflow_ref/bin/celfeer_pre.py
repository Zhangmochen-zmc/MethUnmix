#!/usr/bin/env python3
import argparse, csv, subprocess, sys
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description='Run the supplied CelFEER PAT-to-read-level conversion for every sample')
    p.add_argument('--input-root',required=True); p.add_argument('--cpg-reference',required=True)
    p.add_argument('--converter',required=True); p.add_argument('--output-root',required=True); p.add_argument('--output-metadata',required=True); p.add_argument('--manifest',required=True)
    a=p.parse_args(); root=Path(a.input_root); ref=Path(a.cpg_reference); converter=Path(a.converter); out=Path(a.output_root)
    for path,label in ((root,'PAT input root'),(ref,'CpG reference'),(converter,'batch_change.py')):
        if not path.exists(): raise SystemExit(f'Missing {label}: {path}')
    files=sorted(root.rglob('*.pat.gz'))
    if not files: raise SystemExit(f'No *.pat.gz files found below {root}')
    records=[]
    parents=sorted({source.parent for source in files})
    for input_dir in parents:
        relative_parent=input_dir.relative_to(root)
        destination_dir=out/relative_parent
        subprocess.run([sys.executable,str(converter),str(input_dir),str(destination_dir),
                        '--reference',str(ref)],check=True)
        for source in sorted(input_dir.glob('*.pat.gz')):
            sample=source.name[:-len('.pat.gz')]; destination=destination_dir/f'{sample}.txt'
            if not destination.is_file(): raise SystemExit(f'batch_change.py did not create: {destination}')
            records.append((sample,relative_parent.as_posix(),str(source),str(destination)))
    with open(a.manifest,'w',newline='',encoding='utf-8') as handle:
        writer=csv.writer(handle,delimiter='\t'); writer.writerow(['sample_id','cell_type_directory','source_pat','readlevel_txt']); writer.writerows(records)
    with open(a.output_metadata,'w',newline='',encoding='utf-8') as handle:
        writer=csv.writer(handle); writer.writerow(['GSM','cell_type']); writer.writerows((sample,cell) for sample,cell,_,_ in records)
if __name__=='__main__': main()
