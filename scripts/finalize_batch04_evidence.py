#!/usr/bin/env python3
"""Finalize bounded Batch 04 and classify known N>K exclusions without rerunning them."""
from __future__ import annotations
import argparse,hashlib,json
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
def load(p):return json.loads(p.read_text())
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def dump(p,x):p.write_text(json.dumps(x,indent=2,sort_keys=True)+'\n')
def key(r):return '|'.join((r['method'],r['fixture_id'],r['reference_selector']))
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--matrix',type=Path,required=True);ap.add_argument('--batch',type=Path,required=True);ap.add_argument('--candidate',type=Path,required=True);ap.add_argument('--out-matrix',type=Path,required=True);ap.add_argument('--out-reconciliation',type=Path,required=True);ap.add_argument('--out-report',type=Path,required=True);a=ap.parse_args()
 m,b,c=load(a.matrix),load(a.batch),load(a.candidate);rows=m['rows'];fixtures={x['fixture_id']:x for x in c['fixtures']};assert len(rows)==199==len({key(x) for x in rows});assert b['summary']['succeeded']==8 and b['summary']['failed_or_blocked']==0
 # These frozen five-sample fixtures cannot legally invoke the N>K methods.
 kval={'builtin.epithelial.450k@1.6.0':8,'builtin.fetal19.450k@1.5.0':19,'builtin.immune12.450k@1.5.0':12,'builtin.immune12.epic@1.5.0':12}; newly=[]
 for r in rows:
  if r['execution_status']=='NOT_RUN' and r['method'] in {'EDec','Tsisal'} and r['reference_selector'] in kval:
   n=5;k=kval[r['reference_selector']];r.update({'execution_status':'BLOCKED_INPUT_CONTRACT','final_compatibility_status':'EXECUTION_BLOCKED_INPUT_CONTRACT','structural_validation':'NOT_RUN_INPUT_CONTRACT_BLOCK','repeatability_status':'NOT_APPLICABLE_INPUT_CONTRACT','truth_metrics_disposition':'NOT_AVAILABLE_INPUT_CONTRACT_BLOCK','blocker':{'class':'N_GREATER_THAN_K_INPUT_CONTRACT','sample_count_n':n,'cell_type_count_k':k,'minimum_required_n':k+1,'frozen_fixture_digest':fixtures[r['fixture_id']]['input_files'][0]['sha256'],'static_preflight_evidence':'evidence/frozen_edec_tsisal_fixture_exception_rc.json','scientific_failure_inferred':False}});newly.append({'method':r['method'],'fixture_id':r['fixture_id'],'selector':r['reference_selector'],'n':n,'k':k})
 # MethylCIBERSORT has no extended-bound authorization, so retain primary UNATTEMPTED.
 held=[]
 for r in rows:
  if r['execution_status']=='NOT_RUN' and r['method']=='MethylCIBERSORT' and r['stratum']=='A':
   r['execution_hold']='OWNER_APPROVAL_REQUIRED: representative brain telemetry supports expected long runtime; no scenario-specific 1200-second run authorized.';held.append({'fixture_id':r['fixture_id'],'selector':r['reference_selector']})
 states={'SUCCEEDED':'EXECUTION_SUCCESS','SUCCEEDED_ORIGINAL_AND_REPEAT':'EXECUTION_SUCCESS','BLOCKED_INPUT_CONTRACT':'INPUT_CONTRACT_BLOCKED','TIMED_OUT':'TIMEOUT_NOT_ASSESSED','NOT_RUN':'UNATTEMPTED'};primary={key(r):states[r['execution_status']] for r in rows};counts=Counter(primary.values());assert len(primary)==199 and sum(counts.values())==199
 m['batch_04_update']={'execution_evidence':str(a.batch),'execution_evidence_sha256':sha(a.batch),'attempted_unique_pairs':8,'successful_pairs':8,'blocked_pairs':0,'timeout_pairs':0,'failed_pairs':0,'static_n_greater_k_classifications_added':len(newly),'no_batch05_started':True,'scientific_status_promotion':False};m['phase2_tier_reconciliation']='evidence/tier_primary_state_reconciliation_phase2_rc.json';dump(a.out_matrix,m)
 dump(a.out_reconciliation,{'schema':'methunmix-frozen-199-primary-state-reconciliation-v1','generated_at':datetime.now(timezone.utc).isoformat(),'row_count':199,'unique_primary_key_count':199,'primary_state_counts':{x:counts.get(x,0) for x in ['EXECUTION_SUCCESS','INPUT_CONTRACT_BLOCKED','RESOURCE_CONTRACT_BLOCKED','TIMEOUT_NOT_ASSESSED','INFRASTRUCTURE_FAILURE','SCIENTIFIC_FAILURE','UNATTEMPTED']},'tier_state_counts':{t:{s:sum(1 for r in rows if r['stratum']==t and primary[key(r)]==s) for s in set(primary.values())} for t in 'ABE'},'new_static_input_contract_classifications':newly,'unattempted_pending_owner_timeout_decision':held,'scientific_status_promotion':'PROHIBITED'})
 tier_a={s:sum(1 for r in rows if r['stratum']=='A' and primary[key(r)]==s) for s in set(primary.values())}
 dump(a.out_report,{'schema':'methunmix-tier-a-batch04-phase-report-v1','generated_at':datetime.now(timezone.utc).isoformat(),'batch04':{'attempted':8,'success':8,'blocked':0,'timeout':0,'failed':0,'evidence':str(a.batch),'sha256':sha(a.batch)},'current_primary_state_counts':{x:counts.get(x,0) for x in ['EXECUTION_SUCCESS','INPUT_CONTRACT_BLOCKED','RESOURCE_CONTRACT_BLOCKED','TIMEOUT_NOT_ASSESSED','INFRASTRUCTURE_FAILURE','SCIENTIFIC_FAILURE','UNATTEMPTED']},'tier_a_primary_state_counts':tier_a,'tier_a_remaining_unattempted':sum(1 for r in rows if r['stratum']=='A' and primary[key(r)]=='UNATTEMPTED'),'new_systemic_problem':False,'systemic_problem_detail':'No package, helper, reference-binding, schema-axis, resource-allocation, or repeated runtime failure occurred in the eight legal cases.','pending_owner_decision':held,'final_tier_a_closure_batch_ready':False,'closure_readiness_rationale':'No further legal short-bound Tier A combinations remain. Four MethylCIBERSORT combinations require explicit owner approval of a 1200-second bounded execution before they can be resolved; Batch 05 is not started.','overall_status':'METHUNMIX_CONDA_RC_BLOCKED','scientific_status_promotion':'PROHIBITED','package_binding':b['software']})
if __name__=='__main__':main()
