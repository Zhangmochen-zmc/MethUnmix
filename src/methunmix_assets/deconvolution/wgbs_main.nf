nextflow.enable.dsl=2

// ==============================================================================
// 1. 初始化与辅助逻辑
// ==============================================================================
// def selected_tools = params.tools.split(',').collect { it.trim().toLowerCase() }
// def is_active = { tool_name -> 
//     return selected_tools.contains('all') || selected_tools.contains(tool_name.toLowerCase()) 
// }

def is_active(String tool_name) {
    def selected_tools = (params.tools ?: 'all')
        .toString()
        .split(',')
        .collect { it.trim().toLowerCase() }

    return selected_tools.contains('all') || selected_tools.contains(tool_name.toLowerCase())
}

// Native WGBS tasks run inside an `env -` Apptainer launch.  Thread limits
// therefore have to be declared in the task script itself so task.cpus remains
// the real total CPU budget inside the container.
def native_python_thread_preamble(cpus) {
    return """export OMP_NUM_THREADS='${cpus}'
export OPENBLAS_NUM_THREADS='${cpus}'
export MKL_NUM_THREADS='${cpus}'
export BLIS_NUM_THREADS='${cpus}'
export VECLIB_MAXIMUM_THREADS='${cpus}'
export NUMEXPR_NUM_THREADS='${cpus}'
export METHUNMIX_TASK_CPUS='${cpus}'"""
}

// log.info """\
def print_banner() {
    log.info """
================================================================
      W G B S   D E C O N V O L U T I O N   P I P E L I N E
================================================================
Output Dir : ${params.outdir}
Tools      : ${params.tools}
Genome     : ${params.genome_build}
================================================================
"""
}

workflow {
    def genomeBuild = params.genome_build?.toString()?.toLowerCase()
    if (!(genomeBuild in ['hg19', 'hg38'])) {
        error "Native WGBS deconvolution requires --genome_build hg19 or hg38"
    }
    // ---------------------------------------------------------
    // 1. 数据通道初始化
    // ---------------------------------------------------------
    ch_bed_raw = Channel.fromPath("${params.input_bed_dir}/*.{bed,txt,tsv}")
                        .map { file -> tuple(file.simpleName, file) }
    
    ch_pat_raw = Channel.fromPath("${params.input_pat_dir}/*.{pat,pat.gz}")
                        .map { pat -> tuple(pat.name.replaceAll(/\.pat(\.gz)?$/, ''), pat) }
                        .toSortedList { left, right -> left[0] <=> right[0] }
                        .flatMap { rows -> rows }

    // 初始化合并通道 (用于收集所有工具的最终输出)
    ch_merge_inputs = Channel.empty()

    // =========================================================
    // Group A: BED 输入工具
    // =========================================================

    // --- 1. MEnet ---
    if ( is_active('menet') ) {
        PRE_MENET(ch_bed_raw, "${params.scripts_dir}/menet_pre.py")
        RUN_MENET(PRE_MENET.out, file(params.ref_menet_model), file(params.menet_cell_types))
        POST_PROCESS_MENET(RUN_MENET.out.res)
        
        // 收集结果并混合到主通道
        ch_merge_inputs = ch_merge_inputs.mix(
            POST_PROCESS_MENET.out.collect().map { [it, "MEnet"] }
        )
    }

    // --- 2. MetDecode ---
    if ( is_active('metdecode') ) {
        ch_tool_metdecode = Channel.value(file(params.tool_metdecode, checkIfExists: true))
        ch_met_atlas = Channel.value(file(params.ref_metdecode_atlas))
        ch_met_contract = Channel.value(file("${params.scripts_dir}/metdecode_contract.py", checkIfExists: true))
        PRE_METDECODE_ATLAS(ch_met_atlas, ch_met_contract)
        PRE_METDECODE_STEP1(ch_bed_raw, PRE_METDECODE_ATLAS.out.bed, "${params.scripts_dir}/metdecode_step1.sh", ch_met_contract, ch_met_atlas)
        PRE_METDECODE_STEP2(PRE_METDECODE_STEP1.out.mapped, ch_met_atlas, "${params.scripts_dir}/metdecode_step2.py")
        RUN_METDECODE(PRE_METDECODE_STEP2.out.input, ch_tool_metdecode, ch_met_atlas)
        POST_PROCESS_METDECODE(RUN_METDECODE.out)
        
        ch_merge_inputs = ch_merge_inputs.mix(
            POST_PROCESS_METDECODE.out.collect().map { [it, "MetDecode"] }
        )
    }

    // --- 3. CelFiE ---
    if ( is_active('celfie') ) {
        ch_tool_celfie = Channel.value(file(params.tool_celfie, checkIfExists: true))
        ch_celfie_atlas = Channel.value(file(params.ref_celfie_atlas))
        ch_celfie_contract = Channel.value(file("${params.scripts_dir}/celfie_contract.py", checkIfExists: true))
        PRE_CELFIE_ATLAS(ch_celfie_atlas, ch_celfie_contract)
        PRE_CELFIE_STEP1(ch_bed_raw, PRE_CELFIE_ATLAS.out.bed, "${params.scripts_dir}/celfie_step1.sh")
        PRE_CELFIE_STEP2(PRE_CELFIE_STEP1.out.mapped, ch_celfie_atlas, "${params.scripts_dir}/celfie_step2.py", ch_celfie_contract)
        RUN_CELFIE(PRE_CELFIE_STEP2.out, ch_tool_celfie, params.celfie_k)
        POST_PROCESS_CELFIE(RUN_CELFIE.out)
        
        ch_merge_inputs = ch_merge_inputs.mix(
            POST_PROCESS_CELFIE.out.collect().map { [it, "CelFiE"] }
        )
    }

    // =========================================================
    // Group B: PAT 输入工具
    // =========================================================

    // --- 4. CelFEER ---
    if ( is_active('celfeer') ) {
        ch_tool_celfeer = Channel.value(file(params.tool_celfeer, checkIfExists: true))
        PRE_CELFEER_STEP1(ch_pat_raw, params.ref_celfeer_cpg, params.ref_celfeer_bins, "${params.scripts_dir}/celfeer_step1.sh")
        // PRE_CELFEER_STEP1 completes in scheduler order.  A plain collect()
        // therefore hashes a different path-list order when the same tasks
        // are replayed from cache, forcing the merge and every downstream
        // CelFEER process to run again on the first -resume invocation.
        // Do not sort Path objects directly: their natural order includes the
        // content-addressed work directory, whose hash differs between live
        // and cached emissions.  Keep the sample id until after collection,
        // sort tuples by that stable id, then project the ordered path list.
        // This makes the aggregate task hash deterministic on the very first
        // -resume invocation, not merely after the merge has run twice.
        ch_celfeer_all = PRE_CELFEER_STEP1.out
            .collect(flat: false, sort: { left, right -> left[0] <=> right[0] })
            .map { rows -> rows.collect { row -> row[1] } }
        PRE_CELFEER_MERGE(
            ch_celfeer_all,
            file(params.ref_celfeer_markers, checkIfExists: true),
            file(params.ref_celfeer_cell_types, checkIfExists: true),
            file("${params.scripts_dir}/celfeer_step2.py", checkIfExists: true)
        )
        RUN_CELFEER(
            PRE_CELFEER_MERGE.out.matrix,
            PRE_CELFEER_MERGE.out.metadata,
            ch_tool_celfeer,
            file("${params.scripts_dir}/celfeer_run_adapter.py", checkIfExists: true)
        )
        POST_PROCESS_CELFEER(
            RUN_CELFEER.out,
            file(params.ref_celfeer_cell_types, checkIfExists: true),
            file("${params.scripts_dir}/celfeer_postprocess.py", checkIfExists: true)
        )
        
        ch_merge_inputs = ch_merge_inputs.mix(
            POST_PROCESS_CELFEER.out.collect().map { [it, "CelFEER"] }
        )
    }

    // --- 5. UXM ---
    if ( is_active('uxm') ) {
        ch_pat_for_uxm = Channel
            .fromPath("${params.input_pat_dir}/*.pat.gz", checkIfExists: true)
            .map { pat -> tuple(pat.name.replaceAll(/\.pat\.gz$/, ''), pat) }

        RUN_UXM(
            ch_pat_for_uxm, 
            file(params.ref_uxm_atlas), 
            file(params.tool_uxm)
        )
        POST_PROCESS_UXM(RUN_UXM.out.csv)
        
        ch_merge_inputs = ch_merge_inputs.mix(
            POST_PROCESS_UXM.out.collect().map { [it, "UXM"] }
        )
    }

    // --- 6. MethylBERT ---
    if ( is_active('methylbert') ) {
            PRE_METHYLBERT_PREPARE(
                ch_pat_raw,
                file("${params.scripts_dir}/methylbert_pre1.py"), 
                file(params.mb_reference_root),
                file(params.mb_ref_markers)
            )
            PRE_METHYLBERT_FIX(
                PRE_METHYLBERT_PREPARE.out,
                file("${params.scripts_dir}/methylbert_fix.py")
            )
            if ( params.mb_runtime_metadata ) {
                RUN_METHYLBERT_COMPACT(
                    PRE_METHYLBERT_FIX.out,
                    params.mb_model_dir,
                    file(params.mb_runtime_metadata),
                    file("${params.scripts_dir}/methylbert_compact_deconvolute.py"),
                    params.gpu_enabled ? params.mb_batch_size : (params.mb_cpu_batch_size ?: 32)
                )
                POST_PROCESS_METHYLBERT(RUN_METHYLBERT_COMPACT.out)
            } else {
                RUN_METHYLBERT(
                    PRE_METHYLBERT_FIX.out,
                    params.mb_model_dir,
                    file(params.mb_train_data),
                    params.gpu_enabled ? params.mb_batch_size : (params.mb_cpu_batch_size ?: 32)
                )
                POST_PROCESS_METHYLBERT(RUN_METHYLBERT.out)
            }
            
            ch_merge_inputs = ch_merge_inputs.mix(
                POST_PROCESS_METHYLBERT.out.collect().map { [it, "MethylBERT"] }
            )
    }

    // ---------------------------------------------------------
    // 3. 最终统一合并执行 (只调用一次 Process)
    // ---------------------------------------------------------
    // ch_merge_inputs 里的数据结构是: [ [fileA1, fileA2...], "ToolName" ]
    MERGE_TOOL_RESULTS(ch_merge_inputs)
}

// ==============================================================================
// 独立 POST PROCESS 进程定义
// ==============================================================================

// 1. MEnet Post Process
process POST_PROCESS_MENET {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized", mode: 'copy'
    
    input:
    tuple val(sample_id), path(raw_major)
    
    output:
    path "MEnet_${sample_id}.csv"
    
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python3 "${projectDir}/bin/menet_standardize.py" --input '${raw_major}' --output 'MEnet_${sample_id}.csv' --sample-id '${sample_id}'
    """
}

// 2. MetDecode Post Process
process POST_PROCESS_METDECODE {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized", mode: 'copy'
    
    input:
    tuple val(sample_id), path(raw_csv)
    
    output:
    path "MetDecode_${sample_id}.csv"
    
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python3 -c "
import pandas as pd
df = pd.read_csv('${raw_csv}')
if df.empty or len(df.columns) < 2:
    raise RuntimeError('MetDecode produced an empty or malformed raw result')
raw_sample = str(df.iloc[0, 0])
if raw_sample != '${sample_id}':
    raise RuntimeError(f'MetDecode sample/profile order changed: expected ${sample_id}, got {raw_sample}')
if df.iloc[:, 0].duplicated().any():
    raise RuntimeError('MetDecode raw result contains duplicate profile labels')
df = df.iloc[[0]].copy().rename(columns={df.columns[0]: 'SampleID'})
df['SampleID'] = '${sample_id}'
cols = [c for c in df.columns if c != 'SampleID']
df[cols] = df[cols].div(df[cols].sum(axis=1), axis=0).fillna(0)
if not df[cols].apply(lambda column: column.map(lambda value: pd.notna(value) and value >= 0).all()).all():
    raise RuntimeError('MetDecode standardized result contains invalid proportions')
df.to_csv('MetDecode_${sample_id}.csv', index=False)
"
    """
}

// 3. CelFiE Post Process
process POST_PROCESS_CELFIE {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized", mode: 'copy'
    
    input:
    tuple val(sample_id), path(raw_txt)
    
    output:
    path "CelFiE_${sample_id}.csv"
    
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python3 -c "
import pandas as pd
try:
    df = pd.read_csv('${raw_txt}', sep='\\t')
    df = df.rename(columns={df.columns[0]: 'SampleID'})
    df = df.rename(columns={c: c[:-5] if c.lower().endswith('_meth') else c for c in df.columns})
    df['SampleID'] = '${sample_id}'
    
    cols = [c for c in df.columns if c != 'SampleID']
    df[cols] = df[cols].div(df[cols].sum(axis=1), axis=0).fillna(0)
    df.to_csv('CelFiE_${sample_id}.csv', index=False)
except Exception as e:
    print(e)
    open('CelFiE_${sample_id}.csv', 'w').close()
    "
    """
}

// 4. CelFEER Post Process
process POST_PROCESS_CELFEER {
    publishDir "${params.outdir}/standardized", mode: 'copy'
    
    input:
    tuple path(raw_txt), path(input_metadata)
    path cell_types
    path postprocess_script
    
    output:
    path "CelFEER_all_samples.csv"
    
    script:
    """
    python3 ${postprocess_script} \
      '${raw_txt}' '${cell_types}' '${input_metadata}' CelFEER_all_samples.csv
    """
}

// 5. UXM Post Process
process POST_PROCESS_UXM {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized", mode: 'copy'
    
    input:
    tuple val(sample_id), path(raw_csv)
    
    output:
    path "UXM_${sample_id}.csv"
    
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python3 -c "
import pandas as pd
try:
    df = pd.read_csv('${raw_csv}')
    mapping = {
        'B-cell': 'B cells',
        'CD4+T-cell': 'CD4+ T cells',
        'CD8+T-cell': 'CD8+ T cells',
        'Granulocyte': 'Neutrophils',
        'Monocyte': 'Monocytes',
        'NK-cell': 'Natural Killer cells'
    }
    
    if 'CellType' in df.columns:
        df['CellType'] = df['CellType'].replace(mapping)
        df = df.set_index('CellType').T
        df = df.reset_index(drop=True)
        df.insert(0, 'SampleID', '${sample_id}')
        df.columns.name = None
        
        cols = [c for c in df.columns if c != 'SampleID']
        df[cols] = df[cols].apply(pd.to_numeric, errors='coerce').fillna(0)
        row_sums = df[cols].sum(axis=1)
        if row_sums.iloc[0] > 0:
            df[cols] = df[cols].div(row_sums, axis=0)
        
        df.to_csv('UXM_${sample_id}.csv', index=False)
    else:
        print(f'Error: Column CellType not found in {df.columns}')
        open('UXM_${sample_id}.csv', 'w').close()

except Exception as e:
    print(f'Error processing UXM output: {e}')
    open('UXM_${sample_id}.csv', 'w').close()
    "
    """
}

// 6. MethylBERT Post Process
process POST_PROCESS_METHYLBERT {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized", mode: 'copy'
    
    input:
    tuple val(sample_id), path(raw_txt)
    
    output:
    path "MethylBERT_${sample_id}.csv"
    
    script:
    """
    python3 -c "
import pandas as pd
import sys

try:
    df_raw = pd.read_csv('${raw_txt}', sep='\\t')
    if df_raw.shape[1] < 2:
        df_raw = pd.read_csv('${raw_txt}', sep=',')
except:
    df_raw = pd.read_csv('${raw_txt}')

mapping = {
    'Granulocyte': 'Neutrophils', 
    'CD4+T-cell': 'CD4+ T cells', 
    'CD8+T-cell': 'CD8+ T cells', 
    'B-cell': 'B cells', 
    'NK-cell': 'Natural Killer cells', 
    'Monocyte': 'Monocytes'
}

if 'cell_type' in df_raw.columns and 'pred' in df_raw.columns:
    df_raw['cell_type'] = df_raw['cell_type'].replace(mapping)
    df = df_raw.set_index('cell_type').T
    df = df.reset_index(drop=True)
else:
    print('Warning: MethylBERT output format unexpected, trying standard processing')
    df = df_raw.copy()

df.insert(0, 'SampleID', '${sample_id}')
df.columns.name = None

cols =[c for c in df.columns if c != 'SampleID']
df[cols] = df[cols].apply(pd.to_numeric, errors='coerce').fillna(0)
row_sums = df[cols].sum(axis=1)
if row_sums.iloc[0] > 0:
    df[cols] = df[cols].div(row_sums, axis=0)

df.to_csv('MethylBERT_${sample_id}.csv', index=False)
    "
    """
}

// ==============================================================================
// 辅助运行进程
// ==============================================================================

process PRE_MENET {
    tag "$sample_id"
    input: tuple val(sample_id), path(bed); path script
    output: tuple val(sample_id), path("${sample_id}.cov.gz")
    // Nextflow already shell-escapes staged path values. Wrapping `${bed}`
    // again turns escaped whitespace into a literal backslash in the task.
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python ${script} ${bed} '${sample_id}.cov.gz'
    """
}

process RUN_MENET {
    tag "$sample_id"
    publishDir "${params.outdir}/raw/MEnet", mode: 'copy'
    
    input: 
        tuple val(sample_id), path(cov)   
        path model
        path cell_types
        
    output: 
        tuple val(sample_id), path("${sample_id}_out/cell_proportion_MajorGroup.csv"), emit: res
        path "${sample_id}_menet_run_qc.json", emit: qc
        path "${sample_id}_benchmark.csv", emit: bench
        
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python3 "${projectDir}/bin/menet.py" predict --mix '${cov}' --model '${model}' --cell-types '${cell_types}' \
      --sample-id '${sample_id}' --input-type bismark --output-dir '${sample_id}_out' \
      --genome-build '${params.genome_build}' \
      --device '${params.menet_device ?: 'cpu'}' --seed '${params.random_seed ?: 20260826}' \
      --min-overlap-regions '${params.menet_min_overlap_regions}' \
      --min-overlap-fraction '${params.menet_min_overlap_fraction}'
    """
}

process PRE_METDECODE_ATLAS {
    publishDir "${params.outdir}/qc/MetDecode", mode: 'copy', pattern: "metdecode_atlas_contract.json"
    input:
    path atlas
    path contract
    output:
    path "atlas.bed", emit: bed
    path "metdecode_atlas_contract.json", emit: qc
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python3 ${contract} prepare-atlas --atlas '${atlas}' --output atlas.bed --qc metdecode_atlas_contract.json
    """
}

process PRE_METDECODE_STEP1 {
    tag "$sample_id"
    publishDir "${params.outdir}/qc/MetDecode", mode: 'copy'
    input:
    tuple val(sample_id), path(bed)
    path atlas_bed
    path script
    path contract
    path atlas_tsv
    output:
    tuple val(sample_id), path("${sample_id}.mapped"), emit: mapped
    tuple val(sample_id), path("${sample_id}.metdecode_coverage.json"), emit: qc
    script:
    """
    bash ${script} '${bed}' '${atlas_bed}' '${sample_id}.mapped' '${contract}' '${sample_id}.metdecode_coverage.json' '${atlas_tsv}'
    """
}

process PRE_METDECODE_STEP2 {
    tag "$sample_id"
    publishDir "${params.outdir}/qc/MetDecode", mode: 'copy'
    input:
    tuple val(sample_id), path(mapped)
    path atlas
    path script
    output:
    tuple val(sample_id), path("${sample_id}_input.tsv"), emit: input
    tuple val(sample_id), path("${sample_id}.metdecode_cfdna.json"), emit: qc
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python3 ${script} '${mapped}' '${atlas}' '${sample_id}_input.tsv' '${sample_id}' '${sample_id}.metdecode_cfdna.json'
    """
}

process RUN_METDECODE {
    tag "$sample_id"
    publishDir "${params.outdir}/raw/MetDecode", mode: 'copy'
    input: tuple val(sample_id), path(input_tsv); path tool_dir; path atlas
    output: tuple val(sample_id), path("raw_res.csv")
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    export PYTHONPATH="\${PYTHONPATH:-}:${tool_dir}"
    python3 "${tool_dir}/run.py" "${atlas}" "${input_tsv}" raw_res.csv
    """
}

process PRE_CELFIE_ATLAS {
    input:
    path atlas
    path contract
    output:
    path "atlas.bed", emit: bed
    path "celfie_atlas_contract.json", emit: qc
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python3 ${contract} prepare-atlas --atlas '${atlas}' --output atlas.bed --qc celfie_atlas_contract.json
    """
}

process PRE_CELFIE_STEP1 {
    tag "$sample_id"
    publishDir "${params.outdir}/qc/CelFiE", mode: 'copy', pattern: "*.celfie_coverage.json"
    input:
    tuple val(sample_id), path(bed)
    path atlas_bed
    path script
    output:
    tuple val(sample_id), path("${sample_id}.mapped"), emit: mapped
    tuple val(sample_id), path("${sample_id}.celfie_coverage.json"), emit: qc
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    bash ${script} '${bed}' '${atlas_bed}' '${sample_id}.mapped'
    python3 - '${sample_id}.mapped' '${sample_id}.celfie_coverage.json' <<'PY'
import csv, json, sys
mapped, output = sys.argv[1:]
rows = 0; nonzero = 0; meth = 0.0; depth = 0.0
with open(mapped, newline='', encoding='utf-8') as handle:
    for number, row in enumerate(csv.reader(handle, delimiter='\\t'), start=1):
        if len(row) != 5:
            raise SystemExit(f'CelFiE mapped row {number} expected 5 columns, found {len(row)}')
        value_meth, value_depth = float(row[3]), float(row[4])
        if value_meth < 0 or value_depth < 0 or value_meth > value_depth:
            raise SystemExit(f'CelFiE mapped row {number} violates methylated/depth contract')
        rows += 1; nonzero += int(value_depth > 0); meth += value_meth; depth += value_depth
with open(output, 'w', encoding='utf-8') as handle:
    json.dump({'schema':'demethflow-celfie-marker-coverage-v1', 'status':'PASS',
               'coverage':{'mapped_row_count':rows, 'nonzero_window_count':nonzero,
                           'zero_coverage_window_count':rows-nonzero,
                           'methylated_sum_total':meth, 'depth_sum_total':depth}}, handle, indent=2)
    handle.write('\\n')
PY
    """
}

process PRE_CELFIE_STEP2 {
    tag "$sample_id"
    input:
    tuple val(sample_id), path(mapped)
    path atlas
    path script
    path contract
    output:
    tuple val(sample_id), path("${sample_id}.tsv")
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    PYTHONPATH=\$(dirname ${contract}):\${PYTHONPATH:-} python3 ${script} '${mapped}' '${atlas}' '${sample_id}.tsv'
    """
}

process RUN_CELFIE {
    tag "$sample_id"
    publishDir "${params.outdir}/raw/CelFiE", mode: 'copy'
    input: tuple val(sample_id), path(input_tsv); path tool_dir; val k
    output: tuple val(sample_id), path("output/1_tissue_proportions.txt")
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    export DEMETHFLOW_RANDOM_SEED=${params.random_seed}
    export PYTHONHASHSEED=${params.random_seed}
    export PYTHONPATH="\${PYTHONPATH:-}:${tool_dir}"
    python3 "${tool_dir}/scripts/celfie_new.py" "${input_tsv}" output ${k}
    """
}

process PRE_CELFEER_STEP1 {
    tag "$sample_id"
    // Preserve input-channel emission order even when large per-sample jobs
    // finish (or are restored from cache) in a different scheduler order.
    // Nextflow may otherwise retain completion-order metadata in a collected
    // Path input and change the downstream aggregate task hash despite an
    // identical staged command and identical symlink targets.
    fair true
    input: tuple val(sample_id), path(pat); path cpg; path bins; path script_sh
    output: tuple val(sample_id), path("${sample_id}_binned.txt")
    script:
    def change_py = "${params.scripts_dir}/celfeer_change.py"
    def bis_py    = "${params.scripts_dir}/celfeer_bis.py"
    def sum_py    = "${params.scripts_dir}/celfeer_sum_reads_in_500_bins.py"
    """
    bash ${script_sh} '${pat}' '${sample_id}_binned.txt' '${cpg}' '${bins}' '${change_py}' '${bis_py}' '${sum_py}' ${task.cpus}
    """
}

process PRE_CELFEER_MERGE {
    input:
    path binned_files
    path markers
    path cell_types
    path script
    output:
    path "celfeer_input.txt", emit: matrix
    path "celfeer_input_metadata.json", emit: metadata
    script:
    """
    mkdir inputs
    cp ${binned_files} inputs/
    python3 ${script} inputs celfeer_input.txt '${markers}' '${cell_types}'
    """
}

process RUN_CELFEER {
    publishDir "${params.outdir}/raw/CelFEER", mode: 'copy'
    input:
    path matrix
    path input_metadata
    path tool_dir
    path adapter
    output:
    tuple path("output/1_tissue_proportions.txt"), path("celfeer_input_metadata.json")
    script:
    """
    export DEMETHFLOW_RANDOM_SEED=${params.random_seed}
    export PYTHONHASHSEED=${params.random_seed}
    export PYTHONPATH="\${PYTHONPATH:-}:${tool_dir}"
    python3 ${adapter} '${matrix}' '${input_metadata}' '${tool_dir}' output
    """
}

process RUN_UXM {
    tag "$sample_id"
    publishDir "${params.outdir}/raw/UXM", mode: 'copy'
    cpus 12
    input: 
        tuple val(sample_id), path(pat_files) 
        path atlas
        path tool_uxm_dir
    output: 
        tuple val(sample_id), path("${sample_id}_uxm.csv"), emit: csv
    script: 
    def clean_id = sample_id.toString().trim()
    def input_pat = pat_files instanceof List ? pat_files.find { it.name.endsWith('.pat.gz') } : pat_files
    """
    set -e
    export WGBS_HOME=/opt/wgbs_tools
    export UXM_HOME=\$(realpath ${tool_uxm_dir})
    export PYTHONPATH="\$WGBS_HOME/src/python:\$UXM_HOME/src:\${PYTHONPATH:-}"
    export PATH="\$WGBS_HOME:\$UXM_HOME:\${PATH:-}"
    sed 's/\\r//g' "${atlas}" > atlas_cleaned.txt
    if [ ! -f "${input_pat}.csi" ]; then
        FOUND_CSI=\$(find . -maxdepth 1 -name '*.csi' -print -quit)
        if [ -n "\$FOUND_CSI" ]; then
            ln -sf "\$FOUND_CSI" "${input_pat}.csi"
        else
            wgbstools index "${input_pat}"
        fi
    fi
    echo "Running UXM deconv for ${clean_id}..."
    python3 "\$UXM_HOME/src/uxm.py" deconv -a atlas_cleaned.txt -@ ${task.cpus} -l 4 -T . -d -v "${input_pat}" -o "${clean_id}_uxm.csv"
    """
}

process PRE_METHYLBERT_PREPARE {
    tag "$sample_id"
    input:
        tuple val(sample_id), path(pat)
        path wrapper_script
        path mb_tool_root
        path markers
    output:
        tuple val(sample_id), path("*_reads.csv"), path("selected_cpgs.csv")
    script:
    def main_py = "${mb_tool_root}/src/main.py"
    // 【修改点】消除硬编码，根据 projectDir 动态获取环境目录下的 bin 文件夹
    def env_bin = (params.runtime_mode == 'singularity') ? "/opt/env/bin" : "${projectDir}/conda/methylbert_env/bin"
    """
    set -e
    export DEMETHFLOW_RANDOM_SEED=${params.random_seed}
    export PYTHONHASHSEED=${params.random_seed}
    export PYTHONDONTWRITEBYTECODE=1
    export PATH="${env_bin}:\${PATH:-}"
    if [ -d "${mb_tool_root}/data" ]; then
        ln -snf "\$(realpath ${mb_tool_root}/data)" ./data
    fi
    export PYTHONPATH="${mb_tool_root}/src:\${PYTHONPATH:-}"
    echo "Running MethylBERT Prepare for ${sample_id}..."
    python ${wrapper_script} ${main_py} ${pat} ${markers} ${mb_tool_root} ${params.genome_build} ${params.mb_prepare_cores}
    """
}

process PRE_METHYLBERT_FIX {
    tag "$sample_id"
    input:
        tuple val(sample_id), path(reads_csv), path(cpgs_csv)
        path fix_script
    output:
        tuple val(sample_id), path("fixed_${reads_csv}"), path(cpgs_csv)
    script:
    """
    python ${fix_script} '${reads_csv}' 'fixed_${reads_csv}'
    """
}

process RUN_METHYLBERT {
    tag "$sample_id"
    publishDir "${params.outdir}/raw/MethylBERT", mode: 'copy'
    label 'big_mem'
    
    input:
        tuple val(sample_id), path(fixed_csv), path(cpgs_csv)
        path model_dir
        path train_data_file
        val batch_size

    output:
        tuple val(sample_id), path("output_dir/deconvolution.csv")

    script:
    """
    export DEMETHFLOW_RANDOM_SEED=${params.random_seed}
    export PYTHONHASHSEED=${params.random_seed}
    export CUBLAS_WORKSPACE_CONFIG=:4096:8
    export OMP_NUM_THREADS=1
    export MKL_NUM_THREADS=1
    export OPENBLAS_NUM_THREADS=1
    export HF_HOME=\$PWD/.hf-cache
    export TRANSFORMERS_CACHE=\$PWD/.hf-cache
    mkdir -p input/training_data
    ln -sf "\$(realpath ${train_data_file})" input/training_data/train_seq.kmers_fixed.csv
    rm -rf local_model
    cp -RL ${model_dir} local_model
    chmod -R u+w local_model
    if [[ "${params.gpu_enabled}" == "true" ]]; then
        if [[ -z "\${CUDA_VISIBLE_DEVICES+x}" ]]; then
            unset CUDA_VISIBLE_DEVICES
        fi
    else
        export CUDA_VISIBLE_DEVICES=""
    fi
    export METHYLBERT_NUM_WORKERS=\${METHYLBERT_NUM_WORKERS:-0}
    
    mkdir -p output_dir
    python -c 'import random,numpy as np,torch; seed=${params.random_seed}; random.seed(seed); np.random.seed(seed); torch.manual_seed(seed); torch.cuda.manual_seed_all(seed); torch.set_num_threads(1); torch.set_num_interop_threads(1); torch.use_deterministic_algorithms(True,warn_only=True); from methylbert.cli import main; raise SystemExit(main())' deconvolute -i "${fixed_csv}" -m local_model/ -o output_dir -b ${batch_size}
    """
}

process RUN_METHYLBERT_COMPACT {
    tag "$sample_id"
    publishDir "${params.outdir}/raw/MethylBERT", mode: 'copy'
    label 'big_mem'

    input:
        tuple val(sample_id), path(fixed_csv), path(cpgs_csv)
        path model_dir
        path runtime_metadata
        path compact_runner
        val batch_size

    output:
        tuple val(sample_id), path("output_dir/deconvolution.csv")

    script:
    """
    export DEMETHFLOW_RANDOM_SEED=${params.random_seed}
    export PYTHONHASHSEED=${params.random_seed}
    export PYTHONDONTWRITEBYTECODE=1
    export CUBLAS_WORKSPACE_CONFIG=:4096:8
    export HF_HOME=\$PWD/.hf-cache
    export TRANSFORMERS_CACHE=\$PWD/.hf-cache
    export METHYLBERT_NUM_WORKERS=\${METHYLBERT_NUM_WORKERS:-0}
    rm -rf local_model
    cp -RL ${model_dir} local_model
    chmod -R u+w local_model
    if [[ "${params.methylbert_device ?: 'cpu'}" == "cuda" ]]; then
        if [[ -z "\${CUDA_VISIBLE_DEVICES+x}" ]]; then
            unset CUDA_VISIBLE_DEVICES
        fi
    else
        export CUDA_VISIBLE_DEVICES=""
    fi
    mkdir -p output_dir
    python ${compact_runner} \
      --input "${fixed_csv}" \
      --model-dir local_model \
      --runtime-metadata "${runtime_metadata}" \
      --output-dir output_dir \
      --batch-size ${batch_size} \
      --seed ${params.random_seed} \
      --device "${params.methylbert_device ?: 'cpu'}"
    """
}

// ==============================================================================
// 7. 通用合并进程 (Merge Process)
// ==============================================================================
process MERGE_TOOL_RESULTS {
    tag "Merge_$tool_name"
    publishDir "${params.outdir}/results", mode: 'copy'
    
    input:
    tuple path(csv_files), val(tool_name)
    
    output:
    path "${tool_name}_results.csv"
    
    script:
    """
    ${native_python_thread_preamble(task.cpus)}
    python3 -c "
import pandas as pd
import glob
import os
import re

files = glob.glob('*.csv')
dfs =[]

for f in files:
    try:
        df = pd.read_csv(f)
        dfs.append(df)
    except Exception as e:
        print(f'Warning: Could not read {f}: {e}')

if dfs:
    final_df = pd.concat(dfs, ignore_index=True)
    if 'SampleID' in final_df.columns:
        try:
            final_df['sort_key'] = final_df['SampleID'].astype(str).str.extract('(\\\\d+)')[0].astype(float)
            final_df = final_df.sort_values('sort_key').drop(columns=['sort_key'])
        except:
            final_df = final_df.sort_values('SampleID')
            
    final_df = final_df.fillna(0)
    final_df.to_csv('${tool_name}_results.csv', index=False)
else:
    print('No valid CSV files found to merge.')
    open('${tool_name}_results.csv', 'w').close()
    "
    """
}
