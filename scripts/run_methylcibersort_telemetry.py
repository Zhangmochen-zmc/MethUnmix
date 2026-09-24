#!/usr/bin/env python3
"""Run one bounded MethylCIBERSORT case with Linux telemetry; no package fallback."""
from __future__ import annotations
import argparse, hashlib, json, os, resource, signal, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

def sha(p: Path):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()
def mem():
 d={}
 for line in Path('/proc/meminfo').read_text().splitlines():
  k,v=line.split(':',1); d[k]=int(v.strip().split()[0])*1024
 return {k:d.get(k) for k in ('MemAvailable','MemFree','SwapTotal','SwapFree')}
def ps_rows():
 out=subprocess.check_output(['ps','-eo','pid=,ppid=,pcpu=,rss=,stat=,etime=,comm=,args='],text=True)
 rows=[]
 for ln in out.splitlines():
  p=ln.strip().split(None,7)
  if len(p)>=7: rows.append({'pid':int(p[0]),'ppid':int(p[1]),'pcpu':float(p[2]),'rss_kib':int(p[3]),'state':p[4],'elapsed':p[5],'comm':p[6],'args':p[7] if len(p)>7 else ''})
 return rows
def descendants(root, rows):
 ids={root}; changed=True
 while changed:
  changed=False
  for r in rows:
   if r['ppid'] in ids and r['pid'] not in ids: ids.add(r['pid']);changed=True
 return [r for r in rows if r['pid'] in ids]
def io_for(rows):
 total={k:0 for k in ('rchar','wchar','read_bytes','write_bytes')}
 for r in rows:
  try:
   for line in Path(f'/proc/{r["pid"]}/io').read_text().splitlines():
    k,v=line.split(':',1);k=k.strip()
    if k in total: total[k]+=int(v)
  except (FileNotFoundError,PermissionError): pass
 return total
def preexec(cpus,mem_gb,timeout):
 os.setsid(); allowed=sorted(os.sched_getaffinity(0)); os.sched_setaffinity(0,set(allowed[:cpus])); resource.setrlimit(resource.RLIMIT_AS,(mem_gb*1024**3,mem_gb*1024**3)); resource.setrlimit(resource.RLIMIT_CPU,(timeout+30,timeout+30))
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--cli',type=Path,required=True);ap.add_argument('--nextflow',type=Path,required=True);ap.add_argument('--engine',type=Path,required=True);ap.add_argument('--reference-root',type=Path,required=True);ap.add_argument('--runtime-root',type=Path,required=True);ap.add_argument('--input',type=Path,required=True);ap.add_argument('--truth',type=Path,required=True);ap.add_argument('--outdir',type=Path,required=True);ap.add_argument('--evidence',type=Path,required=True);ap.add_argument('--timeout',type=int,default=600);ap.add_argument('--cpus',type=int,default=4);ap.add_argument('--memory-gb',type=int,default=64);ap.add_argument('--seed',type=int,default=20260916);ap.add_argument('--wheel-sha',required=True);ap.add_argument('--source-sha',required=True);a=ap.parse_args()
 if a.outdir.exists(): raise SystemExit('refusing output collision')
 a.outdir.mkdir(parents=True); log=a.outdir/'telemetry.stdout.log'
 selector='builtin.brain.450k@1.5.0'; manifest=a.reference_root/'builtin.brain.450k/1.5.0/manifest.json'
 expected_input='9682cdcf77741e9ee36bffdafda9e671a94b3c06efee5ad66a748b7d2c60a4cb';expected_truth='e8eabb8f256101fb2686f58164a42e742664f757536e5d6092a5ebaaa9ad6241';expected_ref='a41d2cd3181a604f027ae8e3cf234cb3570651df8fdc05eb6cf548e2cda6e4d9'
 if sha(a.input)!=expected_input or sha(a.truth)!=expected_truth or sha(manifest)!=expected_ref: raise SystemExit('frozen input/truth/reference digest mismatch')
 cmd=[str(a.cli),'run','--scenario','brain','--platform','450k','--reference',selector,'--tools','MethylCIBERSORT','--input',str(a.input),'--reference-root',str(a.reference_root),'--runtime-root',str(a.runtime_root),'--runtime','singularity','--container-engine',str(a.engine),'--executor','local','--accelerator','cpu','--random-seed',str(a.seed),'--verify-reference-checksums','--outdir',str(a.outdir/'run')]
 env=dict(os.environ);env.update({'METHUNMIX_NEXTFLOW_CMD':str(a.nextflow),'NXF_OPTS':'-Xms128m -Xmx8g','NXF_OFFLINE':'true','NXF_DISABLE_CHECK_LATEST':'true','PATH':str(a.nextflow.parent)+os.pathsep+env.get('PATH','')})
 started=now(); t0=time.monotonic(); samples=[]
 with log.open('w') as h:
  p=subprocess.Popen(cmd,stdout=h,stderr=subprocess.STDOUT,env=env,preexec_fn=lambda:preexec(a.cpus,a.memory_gb,a.timeout))
  timed=False
  while p.poll() is None:
   elapsed=time.monotonic()-t0; rows=descendants(p.pid,ps_rows());
   samples.append({'elapsed_seconds':round(elapsed,3),'system_memory':mem(),'process_tree':rows,'aggregate_cpu_percent':round(sum(r['pcpu'] for r in rows),3),'aggregate_rss_kib':sum(r['rss_kib'] for r in rows),'io':io_for(rows),'log_tail':log.read_text(errors='replace').splitlines()[-5:]})
   if elapsed>=a.timeout:
    timed=True;os.killpg(p.pid,signal.SIGTERM)
    try:p.wait(timeout=10)
    except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
    break
   time.sleep(5)
 finished=now(); wall=round(time.monotonic()-t0,3); exit_code=p.returncode
 cpu_tail=[s['aggregate_cpu_percent'] for s in samples[-3:]];rss=[s['aggregate_rss_kib'] for s in samples]
 active=bool(cpu_tail and max(cpu_tail)>=5); growth=(max(rss)-min(rss)) if rss else None
 if timed and active and growth is not None and growth < 512*1024: classification='EXPECTED_LONG_RUNTIME'; recommendation='OWNER_APPROVAL_REQUIRED_FOR_A_SINGLE_1200_SECOND_BOUND'; reason='CPU remained active near bound and RSS did not continuously grow by 512 MiB.'
 elif timed and not active: classification='PROCESS_STALLED'; recommendation='NO_TIMEOUT_INCREASE';reason='No material CPU activity was observed near the bound.'
 elif timed: classification='UNKNOWN';recommendation='NO_TIMEOUT_INCREASE';reason='Telemetry did not support a safe runtime diagnosis.'
 else: classification='COMPLETED_WITHIN_BOUND';recommendation='NO_TIMEOUT_INCREASE';reason='The bounded telemetry run completed.'
 payload={'schema':'methunmix-methylcibersort-instrumented-timeout-profile-v1','generated_at':now(),'scope':'one frozen brain-450K MethylCIBERSORT local CPU run; no WGBS/GPU/Slurm','frozen_binding':{'selector':selector,'input':str(a.input),'input_sha256':sha(a.input),'truth':str(a.truth),'truth_sha256':sha(a.truth),'manifest':str(manifest),'manifest_sha256':sha(manifest),'wheel_sha256':a.wheel_sha,'source_archive_sha256':a.source_sha},'command':cmd,'resource_contract':{'allocated_cpus':a.cpus,'address_space_limit_gb':a.memory_gb,'timeout_seconds':a.timeout,'cpu_affinity_enforced':True},'process':{'started_at':started,'finished_at':finished,'wall_seconds':wall,'exit_code':exit_code,'timed_out':timed,'log':str(log),'log_sha256':sha(log)},'telemetry':{'sample_interval_seconds':5,'samples':samples,'peak_rss_kib':max(rss) if rss else None,'rss_growth_kib':growth,'cpu_active_near_timeout':active,'cpu_tail_percent':cpu_tail,'swap_observed':any((s['system_memory']['SwapFree'] or 0)<(s['system_memory']['SwapTotal'] or 0) for s in samples)},'classification':classification,'recommendation':recommendation,'classification_rationale':reason,'scientific_status_promotion':'PROHIBITED'}
 a.evidence.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n');print(json.dumps({'classification':classification,'exit_code':exit_code,'timed_out':timed,'wall_seconds':wall,'evidence':str(a.evidence)}))
if __name__=='__main__':main()
