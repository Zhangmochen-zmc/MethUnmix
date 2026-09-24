#!/usr/bin/env python3
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ('edec','emeth','epidish','episcore','houseman','menet','methatlas','prmeth','reffreeewas','tsisal','celfeer','celfie','metdecode')
REQUIRED = (
    'main.nf','nextflow.config','nextflow_schema.json','run.sh','README.md',
    'bin/prepare_inputs.py','bin/run_tool.py','bin/celfeer_pre.py','bin/bismark.py','bin/wgbs_to_850.py',
    'containers/deconvolution.sif'
)

def main():
    failures=[]
    for relative in REQUIRED:
        if not (ROOT/relative).is_file(): failures.append(f'missing release file: {relative}')
    for tool in TOOLS:
        folder=ROOT/'tools'/tool
        if not folder.is_dir():
            failures.append(f'missing bundled tool directory: tools/{tool}')
            continue
        if not any((folder/name).is_file() for name in ('run_marker.sh','run_marker.py','run_marker.R')):
            failures.append(f'missing parameter-aware entrypoint: tools/{tool}/run_marker.sh|py|R')
    manifest=[]
    for path in sorted(p for p in ROOT.rglob('*') if p.is_file() and '.conda' not in p.parts):
        if path.name == 'release_manifest.json': continue
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.append({'path':path.relative_to(ROOT).as_posix(),'bytes':path.stat().st_size,'sha256':digest})
    (ROOT/'release_manifest.json').write_text(json.dumps({'version':'1.0.0','files':manifest},indent=2),encoding='utf-8')
    if failures:
        print('RELEASE CHECK FAILED',file=sys.stderr)
        for failure in failures: print(f' - {failure}',file=sys.stderr)
        return 1
    print(f'RELEASE CHECK PASSED: {len(manifest)} files')
    return 0
if __name__=='__main__': raise SystemExit(main())
