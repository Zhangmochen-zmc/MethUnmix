#!/usr/bin/env python3
"""Bind the static scientific gate summary to current bounded execution evidence."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

def load(p): return json.loads(p.read_text())
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for x in iter(lambda:f.read(1024*1024),b''):h.update(x)
 return h.hexdigest()
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--audit',type=Path,required=True);ap.add_argument('--matrix',type=Path,required=True);ap.add_argument('--phase-report',type=Path,required=True);ap.add_argument('--reconciliation',type=Path,required=True);a=ap.parse_args()
 x=load(a.audit);report=load(a.phase_report);c=report['current_primary_state_counts']
 x['status']='PARTIAL_EXECUTION_COVERAGE_55_SUCCESSFUL_OF_199; BATCH03_COMPLETE_PAUSED; SCIENTIFIC_ACCEPTANCE_NOT_PASSED'
 x['updated_on']='2026-09-16'
 x['linked_execution_evidence']={'path':'evidence/tier_a_batch03_execution_rc.json','status':'PARTIAL_PASS_EXECUTED_SCOPE','scope':'Current cumulative evidence: 55 successful frozen pairs of 199, with seven N>K input-contract blocks and three bounded MethylCIBERSORT timeouts. Batch 03 added 14 legal native-array CPU execution records. The four-case 50-sample EDec/Tsisal supplement remains outside frozen baseline counts.','results_count':55,'all_executed_routes_succeeded':True,'metrics_disposition':'REPORT_ONLY_UNLESS_EXACT_PREAPPROVED_POLICY_APPLIES','scientific_status_promotions':0}
 x['current_execution_coverage'].update({'successful_unique_pairs_with_execution_evidence':55,'unresolved_failed_or_blocked_pairs':10,'not_run_pairs':134,'tier_a_remaining':19,'tier_b_remaining':7,'tier_e_remaining':108,'composition':'7 repeated Houseman pairs + 6 original pilot successes + 4 helper-repaired pilot pairs + 12 Batch 01 pairs + 12 Batch 02 pairs + 14 Batch 03 pairs. Seven original EDec/Tsisal rows remain N>K input-contract blocked; three MethylCIBERSORT rows remain bounded timeouts.','coverage_matrix':{'path':str(a.matrix),'sha256':sha(a.matrix)},'frozen_199_reconciliation':{'path':str(a.reconciliation),'sha256':sha(a.reconciliation),'counts':c}})
 x['not_performed'][0]='The remaining 134 of 199 frozen baseline pairs were not executed; Batch 03 is complete and the authorized phase boundary prohibits Batch 04 until owner review.'
 x['claim_boundary']='Fifty-five frozen baseline pairs have execution success evidence, seven original N>K input-contract blocks and three bounded MethylCIBERSORT timeouts remain, and 134 are NOT_RUN. The four helper-launch failures are resolved operationally, but the full scientific compatibility gate is not PASS. Truth metrics remain subject only to preapproved exact-scope policies; otherwise they are report-only. No scientific capability state was promoted.'
 a.audit.write_text(json.dumps(x,indent=2,sort_keys=True)+'\n')
if __name__=='__main__':main()
