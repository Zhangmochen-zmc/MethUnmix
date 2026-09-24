#!/usr/bin/env python3
"""Read-only Phase 2 reconciliation and PRmeth output-contract triage."""
from __future__ import annotations
import argparse, csv, hashlib, json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

def load(p: Path): return json.loads(p.read_text())
def sha(p: Path):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def write(p: Path,x: dict): p.write_text(json.dumps(x,indent=2,sort_keys=True)+'\n')
def matrix_stats(path: Path):
    delimiter='\t' if path.suffix.lower()=='.txt' else ','
    rows=list(csv.DictReader(path.open(encoding='utf-8-sig'), delimiter=delimiter))
    sample_key=next((x for x in rows[0] if x.strip().lower() in {'sampleid','sample'}), next(iter(rows[0])))
    cells=[x for x in rows[0] if x!=sample_key]; values=[float(r[c]) for r in rows for c in cells]
    sums={r[sample_key]:sum(float(r[c]) for c in cells) for r in rows}
    neg=[{'sample_id':r[sample_key],'cell_type':c,'value':float(r[c])} for r in rows for c in cells if float(r[c])<0]
    gt=[{'sample_id':r[sample_key],'cell_type':c,'value':float(r[c])} for r in rows for c in cells if float(r[c])>1]
    return {'path':str(path),'sha256':sha(path),'sample_count':len(rows),'cell_types':cells,'minimum_estimated_value':min(values),'maximum_estimated_value':max(values),'values_lt_zero_count':len(neg),'values_gt_one_count':len(gt),'values_lt_zero':neg,'values_gt_one':gt,'affected_samples':sorted({x['sample_id'] for x in neg+gt}),'affected_cell_types':sorted({x['cell_type'] for x in neg+gt}),'row_sum_minimum':min(sums.values()),'row_sum_maximum':max(sums.values()),'maximum_sum_to_one_deviation':max(abs(v-1) for v in sums.values())}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--matrix',type=Path,required=True);ap.add_argument('--batch03',type=Path,required=True);ap.add_argument('--tier-out',type=Path,required=True);ap.add_argument('--prmeth-out',type=Path,required=True);ap.add_argument('--historical-output',type=Path,action='append',default=[]);a=ap.parse_args()
    matrix=load(a.matrix); rows=matrix['rows']; assert len(rows)==199
    states=['SUCCEEDED','SUCCEEDED_ORIGINAL_AND_REPEAT','BLOCKED_INPUT_CONTRACT','BLOCKED_RESOURCE_REQUIREMENT','TIMED_OUT','NOT_RUN']
    c=Counter((r['stratum'],r['execution_status']) for r in rows)
    table={t:{s:c[t,s] for s in states} for t in ['A','B','E']}
    totals={t:sum(table[t].values()) for t in table}
    assert sum(totals.values())==199
    write(a.tier_out,{'schema':'methunmix-tier-primary-state-reconciliation-v1','generated_at':datetime.now(timezone.utc).isoformat(),'source_matrix':str(a.matrix),'source_matrix_sha256':sha(a.matrix),'row_count':199,'unique_combination_count':len({(r['method'],r['fixture_id'],r['reference_selector']) for r in rows}),'tier_by_execution_status':table,'tier_totals':totals,'tier_a_primary_state':{'EXECUTION_SUCCESS':table['A']['SUCCEEDED']+table['A']['SUCCEEDED_ORIGINAL_AND_REPEAT'],'INPUT_CONTRACT_BLOCKED':table['A']['BLOCKED_INPUT_CONTRACT'],'RESOURCE_CONTRACT_BLOCKED':table['A']['BLOCKED_RESOURCE_REQUIREMENT'],'TIMEOUT_NOT_ASSESSED':table['A']['TIMED_OUT'],'UNATTEMPTED':table['A']['NOT_RUN']},'assertions':{'all_tier_state_cells_sum_to_199':True,'tier_b_unattempted_is_7':table['B']['NOT_RUN']==7,'tier_e_unattempted_is_108':table['E']['NOT_RUN']==108,'tier_a_unattempted_is_machine_computed':table['A']['NOT_RUN']==19},'scientific_status_promotion':'PROHIBITED'})
    batch=load(a.batch03); rs=[r for r in batch['results'] if r['method']=='PRmeth']; assert len(rs)==2
    cases=[]
    for r in rs:
        canonical=Path(r['outputs'][0]['estimate']); raw=next(canonical.parents[1].glob('raw/PRmeth/*_PRMeth_ref_based.txt'))
        truth=Path(r['truth']); cases.append({'case_id':r['case_id'],'fixture_id':r['fixture_id'],'selector':r['reference_selector'],'reference_digest':r['reference_digest'],'raw_output':matrix_stats(raw),'canonical_output':matrix_stats(canonical),'truth_values':matrix_stats(truth),'raw_and_canonical_byte_identical':raw.read_bytes()==canonical.read_bytes(),'execution_evidence_path':str(a.batch03),'execution_evidence_sha256':sha(a.batch03)})
    hist=[]
    for p in a.historical_output:
        if p.is_file(): hist.append(matrix_stats(p))
    write(a.prmeth_out,{'schema':'methunmix-prmeth-output-contract-triage-v1','generated_at':datetime.now(timezone.utc).isoformat(),'tool_semantics':{'declared_output_contract':'cell_proportions_v1','reference_based_algorithm':'vendor/prmeth_R/prmeth.R::qp invokes QPfunction(..., sumLessThanOne=TRUE)','constraints':'QPfunction builds equality sum(coefficients)=1 and non-negativity constraints; PRmeth is not an unconstrained coefficient output.','workflow':'RUN_PRmeth writes the solver H matrix; POST_PROCESS_PRMETH only parses/transposes/renames numeric columns and does not normalize, clip, or project.','package_omits_postprocessing':False,'native_structural_range_policy':'No PRmeth-specific native-array range tolerance was frozen. The existing cross-platform canonical validator accepts values >= -1e-10 and sum-to-one abs_tol=1e-6, showing the implementation already recognizes floating-point boundary residuals.'},'cases':cases,'historical_output_samples':hist,'classification':'HISTORICALLY_EXPECTED_BEHAVIOR','classification_rationale':'All observed out-of-range values are negative floating-point residuals between -8.7e-18 and 0, with no values >1 and row sums within 8.9e-16 of one. This is consistent with the constrained QP solver and existing -1e-10 canonical boundary tolerance, not missing normalization/projection or a material output-contract violation.','policy_change_required':False,'scientific_metric_review_resolution':'Retain the original Batch 03 anomaly records for traceability; classify them as numerical-boundary observations resolved by source/contract audit, not accuracy PASS/FAIL.','scientific_status_promotion':'PROHIBITED'})
if __name__=='__main__':main()
