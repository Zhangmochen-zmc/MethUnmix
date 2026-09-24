#!/usr/bin/env python3
"""Refresh release-gate pointers after bounded Batch 03; never promote release state."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

def load(p): return json.loads(p.read_text())
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()
def save(p,v): p.write_text(json.dumps(v,indent=2,sort_keys=True)+'\n')

def main():
 a=argparse.ArgumentParser()
 a.add_argument('--breakdown',type=Path,required=True);a.add_argument('--gate-state',type=Path,required=True)
 a.add_argument('--matrix',type=Path,required=True);a.add_argument('--reconciliation',type=Path,required=True)
 a.add_argument('--phase-report',type=Path,required=True);a.add_argument('--batch-evidence',type=Path,required=True)
 a.add_argument('--release-audit',type=Path,required=True)
 x=a.parse_args(); report=load(x.phase_report); counts=report['current_primary_state_counts']
 b=load(x.breakdown); p=b['policy']
 p.update({'tier_a_batch_03_invocations':14,'tier_a_batch_03_successful':14,'tier_a_batch_03_failed_or_blocked':0,'tier_a_batch_03_stopped_at_phase_boundary':True,'tier_a_next_batch_started':False,'tier_a_next_batch_completed':False,'not_yet_attempted_pairs_after_pilot':counts['UNATTEMPTED']})
 for gate in b['gates']:
  if gate['id']=='execution_coverage_gate':
   gate.update({'status':'PARTIAL_55_SUCCESSFUL_EXECUTION_PAIRS_134_NOT_RUN_10_CONTRACT_OR_TIMEOUT_CASES','evidence':str(x.matrix),'reconciliation_evidence':str(x.reconciliation),'remaining':'55 frozen combinations have successful execution evidence. Seven original five-sample EDec/Tsisal rows remain INPUT_CONTRACT_BLOCKED; three MethylCIBERSORT rows are TIMEOUT_NOT_ASSESSED after bounded local CPU execution. The 50-sample EDec/Tsisal supplement is isolated and excluded from frozen counts. 134 pairs remain NOT_RUN (Tier A 19, B 7, E 108). No scientific status was promoted.'})
  elif gate['id']=='repeatability_gate':
   gate['remaining']='Existing representative repeatability evidence remains unchanged. Batch 03 recorded one run per new combination; no new repeatability threshold was inferred.'
  elif gate['id']=='regression_compatibility_gate':
   gate['evidence'] += '; '+str(x.batch_evidence)
   gate['remaining']='Tested combinations lacking authoritative prior outputs remain explicitly marked NO_AUTHORITATIVE_PRIOR_OUTPUT; Batch 03 adds 14 such bounded execution records. No temporary output was promoted to truth.'
 for finding in b['supplemental_findings']:
  if finding['id']=='tier_a_pilot':
   finding['detail']='Cumulative frozen matrix: 55 successful execution pairs, seven original N>K input-contract blocks, three bounded MethylCIBERSORT timeouts, and 134 NOT_RUN. Batch 03 added 14/14 successful legal Tier A runs. Four 50-sample EDec/Tsisal runs remain supplemental only and do not alter frozen rows. No unapproved threshold or READY/RU state changed.'
 b['overall_status']='METHUNMIX_CONDA_RC_BLOCKED';save(x.breakdown,b)
 s=load(x.gate_state); latest=s['latest_local_audit']
 latest.update({'scientific_compatibility_status':'PARTIAL_EXECUTION_COVERAGE_55_SUCCESSFUL_OF_199; 7_INPUT_CONTRACT_BLOCKED; 3_TIMEOUT_NOT_ASSESSED; 134_UNATTEMPTED; BATCH03_COMPLETE_PAUSED','scientific_compatibility_scope':'199 frozen pairs: 55 successful execution records; seven original five-sample EDec/Tsisal rows remain N>K input-contract blocked; three MethylCIBERSORT rows timed out under bounded local execution after resource triage. The separate four-case 50-sample EDec/Tsisal supplement is isolated from frozen counts. No unapproved threshold or READY/RU change.','scientific_compatibility_coverage_matrix':str(x.matrix),'scientific_compatibility_coverage_matrix_sha256':digest(x.matrix),'frozen_199_reconciliation':str(x.reconciliation),'frozen_199_reconciliation_sha256':digest(x.reconciliation),'tier_a_batch03_execution':str(x.batch_evidence),'tier_a_batch03_execution_sha256':digest(x.batch_evidence),'tier_a_batch03_phase_report':str(x.phase_report),'tier_a_batch03_phase_report_sha256':digest(x.phase_report),'methylcibersort_cpu_requirement_audit':'evidence/methylcibersort_cpu_requirement_audit_rc.json','methylcibersort_timeout_profile':'evidence/methylcibersort_timeout_profile_rc.json','frozen_fixture_exception':'evidence/frozen_edec_tsisal_fixture_exception_rc.json','public_release_ready':False})
 audit=load(x.release_audit); latest.update({'path':str(x.release_audit),'sha256':digest(x.release_audit),'status':audit['status']})
 s['state']='METHUNMIX_CONDA_RC_BLOCKED';save(x.gate_state,s)

if __name__=='__main__': main()
