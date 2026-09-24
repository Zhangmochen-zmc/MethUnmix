#!/usr/bin/env python3
import argparse, json, os, shutil, subprocess
from pathlib import Path

ALIASES={'tsisa':'tsisal','medecode':'metdecode'}

def require(path, label):
    if not Path(path).exists(): raise SystemExit(f'Missing required {label}: {path}')

def main():
    p=argparse.ArgumentParser()
    for x in ['tool','data-type','input-data-type','reference-root','metadata','assets-dir','source-dir','output-dir','execution-record']: p.add_argument('--'+x,required=True)
    p.add_argument('--genome-build',default=''); p.add_argument('--seed',type=int,default=123)
    a=p.parse_args(); tool=ALIASES.get(a.tool,a.tool); source=(Path(a.source_dir)/tool).resolve()
    require(source,f'source directory for {tool}')
    assets=Path(a.assets_dir).resolve()
    manifest=''
    if a.data_type=='450k': manifest=str(assets/'manifests/450k/HM450.hg38.manifest.tsv')
    elif a.data_type=='epic': manifest=str(assets/'manifests/epic/EPIC.hg38.manifest.tsv')
    dhs=''
    if a.data_type=='450k': dhs=str(assets/'dhs/450k/filtered_cg_info_450k.bed')
    elif a.data_type=='epic': dhs=str(assets/'dhs/epic/filtered_cg_info_epic.bed')
    env=os.environ.copy(); env.update({
        'PYTHONHASHSEED':str(a.seed), 'DECONVOLUTION_SEED':str(a.seed),
        'REFERENCE_ROOT':str(Path(a.reference_root).resolve()),
        'REFERENCE_METADATA':str(Path(a.metadata).resolve()),
        'DATA_TYPE':a.data_type, 'INPUT_DATA_TYPE':a.input_data_type, 'GENOME_BUILD':a.genome_build,
        'ASSETS_DIR':str(assets), 'ASSET_MANIFEST':manifest, 'ASSET_DHS_CPG':dhs,
        'CELFEER_MAIN':str(assets/'celfeer/CelFEER-main'),
        'TOOL_COMMON_DIR':str((Path(a.source_dir)/'common').resolve()),
        'OUTPUT_DIR':str(Path(a.output_dir).resolve())
    })
    record={'tool':tool,'requested_tool':a.tool,'input_data_type':a.input_data_type,
            'effective_data_type':a.data_type,'seed':a.seed,'source':str(source.resolve()),'status':'not_started'}
    # V1 deliberately executes only parameter-aware entrypoints. Legacy scripts with
    # absolute paths or fixed cell lists must not be silently rewritten.
    candidates=[source/'run_marker.sh',source/'run_marker.py',source/'run_marker.R']
    entry=next((x for x in candidates if x.exists()),None)
    if entry is None:
        record['status']='blocked'; record['reason']='No parameter-aware run_marker entrypoint; legacy source retained unchanged'
        Path(a.execution_record).write_text(json.dumps(record,indent=2,ensure_ascii=False),encoding='utf-8')
        raise SystemExit(f'{tool}: missing parameter-aware run_marker.sh/.py/.R entrypoint')
    if entry.suffix=='.py': cmd=['python3',str(entry)]
    elif entry.suffix=='.R': cmd=['Rscript',str(entry)]
    else: cmd=['bash',str(entry)]
    # Rscript rewrites spaces in its --file= argument to "~+~" under some
    # Apptainer/Nextflow combinations.  Pass the canonical entrypoint through
    # the environment so provenance hashing remains relocatable.
    env['TOOL_ENTRYPOINT']=str(entry.resolve())
    record['entrypoint']=str(entry.resolve()); record['status']='running'
    Path(a.execution_record).write_text(json.dumps(record,indent=2,ensure_ascii=False),encoding='utf-8')
    subprocess.run(cmd,check=True,env=env,cwd=a.output_dir)
    record['status']='complete'; Path(a.execution_record).write_text(json.dumps(record,indent=2,ensure_ascii=False),encoding='utf-8')
if __name__=='__main__': main()
