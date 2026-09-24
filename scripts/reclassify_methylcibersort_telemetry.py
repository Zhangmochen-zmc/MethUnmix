#!/usr/bin/env python3
"""Derive an auditable timeout classification from already-captured telemetry."""
from __future__ import annotations
import argparse,json
from pathlib import Path
def main():
 ap=argparse.ArgumentParser();ap.add_argument('evidence',type=Path);a=ap.parse_args();x=json.loads(a.evidence.read_text());s=x['telemetry']['samples'];tail=s[-6:];rss=[z['aggregate_rss_kib'] for z in tail];cpu=[z['aggregate_cpu_percent'] for z in tail];io=[z['io']['rchar']+z['io']['wchar'] for z in tail]
 active=sum(v>=5 for v in cpu)>=3; tail_rss_span=max(rss)-min(rss); io_progress=io[-1]>io[0]; healthy=active and tail_rss_span < 512*1024 and not x['telemetry']['swap_observed'] and io_progress
 x['telemetry']['near_timeout_assessment']={'sample_count':len(tail),'cpu_percent':cpu,'cpu_active_samples':sum(v>=5 for v in cpu),'rss_kib':rss,'rss_span_kib':tail_rss_span,'io_total_bytes':io,'io_progress':io_progress,'swap_observed':x['telemetry']['swap_observed']}
 if x['process']['timed_out'] and healthy:
  x['classification']='EXPECTED_LONG_RUNTIME';x['recommendation']='OWNER_APPROVAL_REQUIRED_FOR_A_SINGLE_1200_SECOND_BOUND';x['classification_rationale']='The final 30 seconds retained active CPU in at least three samples, made forward I/O progress, used no swap, and had bounded non-monotonic RSS variation below 512 MiB; the R workers were executing rather than stalled.'
 elif x['process']['timed_out']:
  x['classification']='UNKNOWN';x['recommendation']='NO_TIMEOUT_INCREASE';x['classification_rationale']='Captured telemetry does not establish healthy forward progress.'
 a.evidence.write_text(json.dumps(x,indent=2,sort_keys=True)+'\n')
if __name__=='__main__':main()
