nextflow.enable.dsl=2

// ==============================================================================
// 1. 参数配置 (Params)
// ==============================================================================
params.input_dir       = "${projectDir}/wgbs_epic_data/*.csv"
params.outdir          = "${projectDir}/results_wgbs_epic"
params.tools           = "all"  // 选项: "all" 或 "ARIC,RefFreeEWAS" (逗号分隔)
params.local_conda_env = "${projectDir}/conda/array_env"
params.random_seed     = 20260826
params.methylcibersort_permutations = 1000

// --- 参考文件路径 (Reference) ---
params.ref_dir         = "${projectDir}/wgbs_epic_ref"
params.ref_aric        = "${params.ref_dir}/ARIC.csv"
params.ref_cibersort   = "${params.ref_dir}/MethylCIBERSORT.txt"
params.ref_edec_data   = "${params.ref_dir}/EDEC.txt"
params.ref_edec_meta   = "${params.ref_dir}/EDEC_meta.csv"
params.ref_edec_mark   = "${params.ref_dir}/EDEC_markers.rds"
params.ref_emeth       = "${params.ref_dir}/EMeth.RData"
params.src_emeth       = "${params.ref_dir}/EMeth-master"
params.ref_epidish     = "${params.ref_dir}/EpiDISH.csv"
params.ref_episcore    = "${params.ref_dir}/EpiSCORE.csv"
params.ref_houseman_cell = "${params.ref_dir}/Houseman_cell.rds"
params.ref_houseman_beta = "${params.ref_dir}/Houseman_beta.rds"
params.ref_methatlas   = "${params.ref_dir}/MethAtlas.csv"
params.script_atlas    = "${params.ref_dir}/MethAtlas.py"
params.ref_reffree     = "${params.ref_dir}/RefFreeEWAS.csv"
params.ref_prmeth      = "${params.ref_dir}/PRMeth.csv"
params.src_prmeth      = null // supplied by the external PRmeth runtime contract
params.ref_tsisal      = "${params.ref_dir}/Tsisal.csv"

// --- 后处理脚本路径 (Pro Scripts) ---
params.pro_dir       = "${projectDir}/array_pro"
params.pro_aric      = "${params.pro_dir}/ARIC_pro.py"
params.pro_methatlas = "${params.pro_dir}/MethAtlas_pro.py"
params.pro_cibersort = "${params.pro_dir}/MethylCIBERSORT_pro.py"
params.pro_edec      = "${params.pro_dir}/EDec_pro.py"
params.pro_emeth     = "${params.pro_dir}/EMeth_pro.py"
params.pro_epidish   = "${params.pro_dir}/EpiDISH_850k_pro.py"
params.pro_episcore  = "${params.pro_dir}/EpiSCORE_pro.py"
params.pro_houseman  = "${params.pro_dir}/Houseman_pro.py"
params.pro_medecom   = "${params.pro_dir}/MeDeCom_pro.py"
params.pro_reffree   = "${params.pro_dir}/RefFreeEWAS_pro.py"
params.pro_prmeth    = "${params.pro_dir}/PRmeth_pro.py"
params.pro_tsisal    = "${params.pro_dir}/Tsisal_pro.py"


// ==============================================================================
// 2. 初始化逻辑
// ==============================================================================
def is_active(String tool_name) {
    def selected_tools = (params.tools ?: 'all')
        .toString()
        .split(',')
        .collect { tool -> tool.trim().toLowerCase() }

    return selected_tools.contains('all') || selected_tools.contains(tool_name.toLowerCase())
}

def print_banner() {
    log.info """
================================================================
  M E T H Y L A T I O N   A R R A Y   B E N C H M A R K
================================================================
Output Dir : ${params.outdir}
Tools      : ${params.tools}
Conda Env  : ${params.local_conda_env}
PRO Script : ${params.pro_dir}
================================================================
"""
}

// ==============================================================================
// 3. Workflow 主流程
// ==============================================================================
workflow {
    print_banner()

    // 读取输入文件
    ch_samples = channel.fromPath(params.input_dir)
                        .map { file -> tuple(file.baseName, file) }

    // 初始化收集通道
    ch_all_benchmarks = channel.empty() // 性能指标
    ch_merge_inputs   = channel.empty() // 标准化后的结果

    // ---------------------------------------------------------
    // Tool 1: ARIC (Python Pro)
    // ---------------------------------------------------------
    if ( is_active('aric') ) {
        RUN_ARIC(ch_samples, file(params.ref_aric))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_ARIC.out.bench.collect().map { benchmark -> ["ARIC", benchmark] })
        
        POST_PROCESS_ARIC(RUN_ARIC.out.res, file(params.pro_aric))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_ARIC.out.map { result -> ["ARIC", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // Tool 2: MethylCIBERSORT (R Pro)
    // ---------------------------------------------------------
    if ( is_active('methylcibersort') || is_active('cibersort') ) {
        RUN_MethylCIBERSORT(ch_samples, file(params.ref_cibersort))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_MethylCIBERSORT.out.bench.collect().map { benchmark -> ["MethylCIBERSORT", benchmark] })
        
        POST_PROCESS_CIBERSORT(RUN_MethylCIBERSORT.out.res, file(params.pro_cibersort))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_CIBERSORT.out.map { result -> ["MethylCIBERSORT", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // Tool 3: EDec (R Pro) - 需要 PREP
    // ---------------------------------------------------------
    if ( is_active('edec') ) {
        PREP_EDec(file(params.ref_edec_data), file(params.ref_edec_meta))
        
        RUN_EDec(ch_samples, PREP_EDec.out, file(params.ref_edec_mark))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_EDec.out.bench.collect().map { benchmark -> ["EDec", benchmark] })

        ch_edec_filtered = RUN_EDec.out.res.transpose()
        .filter { result -> result[1].name.contains("Proportions") } 
        
        POST_PROCESS_EDEC(ch_edec_filtered, file(params.pro_edec))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_EDEC.out.map { result -> ["EDec", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // Tool 4: EMeth (R Pro)
    // ---------------------------------------------------------
    if ( is_active('emeth') ) {
        RUN_EMeth(ch_samples, file(params.ref_emeth), file(params.src_emeth))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_EMeth.out.bench.collect().map { benchmark -> ["EMeth", benchmark] })

        ch_emeth_split = RUN_EMeth.out.res.transpose()
        POST_PROCESS_EMETH(ch_emeth_split, file(params.pro_emeth))

        ch_merge_inputs = ch_merge_inputs.mix(
            POST_PROCESS_EMETH.out
                .map { file ->
                    def method = file.name.contains("laplace") ? "EMeth_Laplace" : "EMeth_Normal"
                    return [ method, file ]
                }
                .groupTuple()
        )
    }


    // ---------------------------------------------------------
    // Tool 5: EpiDISH (R Pro)
    // ---------------------------------------------------------
    if ( is_active('epidish') ) {
        RUN_EpiDISH(ch_samples, file(params.ref_epidish))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_EpiDISH.out.bench.collect().map { benchmark -> ["EpiDISH", benchmark] })
        
        ch_epidish_filtered = RUN_EpiDISH.out.res.transpose().filter { result -> result[1].name.contains("result_own") }
        
        POST_PROCESS_EPIDISH(ch_epidish_filtered, file(params.pro_epidish))

        ch_merge_inputs = ch_merge_inputs.mix(
            POST_PROCESS_EPIDISH.out
                .map { file -> 
                    def method = file.name.contains("RPC") ? "EpiDISH_RPC" :
                                 file.name.contains("CBS") ? "EpiDISH_CBS" :
                                 file.name.contains("CP")  ? "EpiDISH_CP" : "EpiDISH_Other"
                    return [ method, file ] 
                }
                .groupTuple()
        )
    }

    // ---------------------------------------------------------
    // Tool 6: EpiScore (R Pro) - 需要 PREP
    // ---------------------------------------------------------
    if ( is_active('episcore') ) {
        PREP_EpiSCORE(file(params.ref_episcore))
        
        RUN_EpiSCORE(ch_samples, PREP_EpiSCORE.out)
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_EpiSCORE.out.bench.collect().map { benchmark -> ["EpiSCORE", benchmark] })
        
        POST_PROCESS_EPISCORE(RUN_EpiSCORE.out.res, file(params.pro_episcore))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_EPISCORE.out.map { result -> ["EpiSCORE", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // Tool 7: Houseman (R Pro)
    // ---------------------------------------------------------
    if ( is_active('houseman') ) {
        RUN_Houseman(ch_samples, file(params.ref_houseman_cell), file(params.ref_houseman_beta))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_Houseman.out.bench.collect().map { benchmark -> ["Houseman", benchmark] })
        
        POST_PROCESS_HOUSEMAN(RUN_Houseman.out.res, file(params.pro_houseman))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_HOUSEMAN.out.map { result -> ["Houseman", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // Tool 8: MeDeCom (R Pro)
    // ---------------------------------------------------------
    if ( is_active('medecom') ) {
        RUN_MeDeCom(ch_samples)
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_MeDeCom.out.bench.collect().map { benchmark -> ["MeDeCom", benchmark] })
        
        POST_PROCESS_MEDECOM(RUN_MeDeCom.out.res, file(params.pro_medecom))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_MEDECOM.out.map { result -> ["MeDeCom", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // Tool 9: MethAtlas (Python Pro)
    // ---------------------------------------------------------
    if ( is_active('methatlas') ) {
        RUN_MethAtlas(ch_samples, file(params.ref_methatlas), file(params.script_atlas))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_MethAtlas.out.bench.collect().map { benchmark -> ["MethAtlas", benchmark] })
        
        POST_PROCESS_METHATLAS(RUN_MethAtlas.out.res, file(params.pro_methatlas))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_METHATLAS.out.map { result -> ["MethAtlas", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // Tool 10: RefFreeEWAS (R Pro)
    // ---------------------------------------------------------
    if ( is_active('reffreeewas') ) {
        RUN_RefFreeEWAS(ch_samples, file(params.ref_reffree))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_RefFreeEWAS.out.bench.collect().map { benchmark -> ["RefFreeEWAS", benchmark] })
        
        POST_PROCESS_REFFREE(RUN_RefFreeEWAS.out.res, file(params.pro_reffree))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_REFFREE.out.map { result -> ["RefFreeEWAS", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // Tool 11: PRmeth (R Pro)
    // ---------------------------------------------------------
    if ( is_active('prmeth') ) {
        RUN_PRmeth(ch_samples, file(params.ref_prmeth), file(params.src_prmeth))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_PRmeth.out.bench.collect().map { benchmark -> ["PRmeth", benchmark] })
        
        POST_PROCESS_PRMETH(RUN_PRmeth.out.res, file(params.pro_prmeth))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_PRMETH.out.map { result -> ["PRmeth", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // Tool 12: Tsisal (R Pro)
    // ---------------------------------------------------------
    if ( is_active('tsisal') ) {
        RUN_Tsisal(ch_samples, file(params.ref_tsisal))
        ch_all_benchmarks = ch_all_benchmarks.mix(RUN_Tsisal.out.bench.collect().map { benchmark -> ["Tsisal", benchmark] })
        
        POST_PROCESS_TSISAL(RUN_Tsisal.out.res, file(params.pro_tsisal))
        ch_merge_inputs = ch_merge_inputs.mix(POST_PROCESS_TSISAL.out.map { result -> ["Tsisal", result] }.groupTuple())
    }

    // ---------------------------------------------------------
    // 4. 汇总
    // ---------------------------------------------------------
    MERGE_BENCHMARKS(ch_all_benchmarks)
    
    // 按工具合并所有样本的 standardized 结果
    MERGE_TOOL_RESULTS(ch_merge_inputs)
}


// ==============================================================================
// 4. PROCESS - RUN (工具运行)
// ==============================================================================

process RUN_ARIC {
    tag "$sample_id"; publishDir "${params.outdir}/raw/ARIC", mode: 'copy'
    input: tuple val(sample_id), path(mix); path ref
    // 注意：输出 tuple 包含 sample_id，以便 post process 使用
    output: tuple val(sample_id), path("*_prop.csv"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script: "python3 \"${projectDir}/bin/aric_decon.py\" --mix $mix --ref $ref --sample_id $sample_id"
}

process RUN_MethylCIBERSORT {
    tag "$sample_id"; publishDir "${params.outdir}/raw/MethylCIBERSORT", mode: 'copy'
    input: tuple val(sample_id), path(mix); path ref
    output: tuple val(sample_id), path("*_res.txt"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script: "methylcibersort_decon.R --mix $mix --ref $ref --sample_id $sample_id --perm ${params.methylcibersort_permutations} --seed ${params.random_seed}"
}

process PREP_EDec {
    input: path d; path m; output: path "Tref.rds"
    script: "edec_ref.R --ref_data $d --ref_meta $m --output Tref.rds"
}
process RUN_EDec {
    tag "$sample_id"; publishDir "${params.outdir}/raw/EDec", mode: 'copy'
    input: tuple val(sample_id), path(mix); path tref; path markers
    output: tuple val(sample_id), path("*.csv"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script: "edec_decon.R --mix $mix --tref $tref --markers $markers --sample_id $sample_id"
}

process RUN_EMeth {
    tag "$sample_id"; publishDir "${params.outdir}/raw/EMeth", mode: 'copy'
    input: tuple val(sample_id), path(mix); path ref; path src
    output: tuple val(sample_id), path("*.txt"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script: "emeth_decon.R --mix $mix --ref_rdata $ref --emeth_src $src --sample_id $sample_id"
}

process RUN_EpiDISH {
    tag "$sample_id"; publishDir "${params.outdir}/raw/EpiDISH", mode: 'copy'
    input: tuple val(sample_id), path(mix); path ref
    output: tuple val(sample_id), path("*.txt"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script:
    """
    export OMP_NUM_THREADS='${task.cpus}'
    export OPENBLAS_NUM_THREADS='${task.cpus}'
    export MKL_NUM_THREADS='${task.cpus}'
    export BLIS_NUM_THREADS='${task.cpus}'
    export VECLIB_MAXIMUM_THREADS='${task.cpus}'
    export NUMEXPR_NUM_THREADS='${task.cpus}'
    epidish_decon_850k.R --mix $mix --own_ref $ref --sample_id $sample_id
    """
}

process PREP_EpiSCORE {
    input: path ref_raw; output: path "ref_gene.rds"
    script: "episcore_ref_850k.R --input $ref_raw --output ref_gene.rds"
}
process RUN_EpiSCORE {
    tag "$sample_id"; publishDir "${params.outdir}/raw/EpiSCORE", mode: 'copy'
    input: tuple val(sample_id), path(mix_cpg); path ref_gene_rds
    output: tuple val(sample_id), path("Result_*.csv"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script: "episcore_decon_850k.R --mix_cpg $mix_cpg --ref_gene $ref_gene_rds --sample_id $sample_id"
}

process RUN_Houseman {
    tag "$sample_id"; publishDir "${params.outdir}/raw/Houseman", mode: 'copy'
    input: tuple val(sample_id), path(mix); path cell; path beta
    output: tuple val(sample_id), path("*.txt"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script:
    """
    export OMP_NUM_THREADS='${task.cpus}'
    export OPENBLAS_NUM_THREADS='${task.cpus}'
    export MKL_NUM_THREADS='${task.cpus}'
    export BLIS_NUM_THREADS='${task.cpus}'
    export VECLIB_MAXIMUM_THREADS='${task.cpus}'
    export NUMEXPR_NUM_THREADS='${task.cpus}'
    houseman_decon.R --mix $mix --cell_ref $cell --beta_ref $beta --sample_id $sample_id
    """
}

process RUN_MeDeCom {
    tag "$sample_id"; publishDir "${params.outdir}/raw/MeDeCom", mode: 'copy'
    input: tuple val(sample_id), path(mix)
    output: tuple val(sample_id), path("*.rds"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
            path "*.pdf"
    script: "medecom_decon.R --mix $mix --sample_id $sample_id --ncores ${task.cpus}"
}


process RUN_MethAtlas {
    tag "$sample_id"; publishDir "${params.outdir}/raw/MethAtlas", mode: 'copy'
    input: tuple val(sample_id), path(mix); path atlas; path script_file
    output: tuple val(sample_id), path("*_deconv_output.csv"), emit: res 
            path "${sample_id}_benchmark.csv", emit: bench
    script: "python3 \"${projectDir}/bin/methatlas_decon.py\" --mix $mix --atlas $atlas --script ${script_file} --sample_id $sample_id"
}

process RUN_RefFreeEWAS {
    tag "$sample_id"; publishDir "${params.outdir}/raw/RefFreeEWAS", mode: 'copy'
    input: tuple val(sample_id), path(mix); path ref
    output: tuple val(sample_id), path("*.txt"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script:
    """
    export OMP_NUM_THREADS='${task.cpus}'
    export OPENBLAS_NUM_THREADS='${task.cpus}'
    export MKL_NUM_THREADS='${task.cpus}'
    export BLIS_NUM_THREADS='${task.cpus}'
    export VECLIB_MAXIMUM_THREADS='${task.cpus}'
    export NUMEXPR_NUM_THREADS='${task.cpus}'
    reffreeewas_decon.R --mix $mix --ref $ref --sample_id $sample_id
    """
}

process RUN_PRmeth {
    tag "$sample_id"; publishDir "${params.outdir}/raw/PRmeth", mode: 'copy'
    input: tuple val(sample_id), path(mix); path ref; path src
    output: tuple val(sample_id), path("*.txt"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script: "prmeth_decon.R --mix $mix --ref $ref --src_dir $src --sample_id $sample_id"
}

process RUN_Tsisal {
    tag "$sample_id"; publishDir "${params.outdir}/raw/Tsisal", mode: 'copy'
    input: tuple val(sample_id), path(mix); path ref
    output: tuple val(sample_id), path("*.txt"), emit: res
            path "${sample_id}_benchmark.csv", emit: bench
    script: "tsisal_decon.R --mix $mix --ref $ref --sample_id $sample_id"
}


// ==============================================================================
// 5. PROCESS - POST PROCESS (结果标准化)
// ==============================================================================

// --- Python 组 ---

process POST_PROCESS_ARIC {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/ARIC", mode: 'copy'
    
    input: tuple val(sample_id), path(raw); path script
    // 输出文件名必须是 ${sample_id}_aric_std.csv，否则 Python 脚本提取 ID 会出错
    output: path "${sample_id}_aric_std.csv"
    
    script:
    """
    python3 ${script} "${raw}" "${sample_id}_aric_std.csv"
    """
}


process POST_PROCESS_METHATLAS {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/MethAtlas", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${sample_id}_methatlas_std.csv"
    script: "python3 ${script} '${raw}' '${sample_id}_methatlas_std.csv'"
}

// --- R 组 ---

process POST_PROCESS_CIBERSORT {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/MethylCIBERSORT", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${sample_id}_cibersort_std.csv"
    script: "python3 ${script} '${raw}' '${sample_id}_cibersort_std.csv'"
}

process POST_PROCESS_EDEC {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/EDec", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${sample_id}_edec_std.csv"
    script: "python3 ${script} '${raw}' '${sample_id}_edec_std.csv'"
}

process POST_PROCESS_EMETH {
    tag "${raw.baseName}"
    publishDir "${params.outdir}/standardized/EMeth", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${raw.baseName}_std.csv"
    script: "python3 ${script} '${raw}' '${raw.baseName}_std.csv'"
}

process POST_PROCESS_EPIDISH {
    tag "${raw.baseName}" 
    publishDir "${params.outdir}/standardized/EpiDISH", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${raw.baseName}_std.csv"
    script: "python3 ${script} '${raw}' '${raw.baseName}_std.csv'"
}

process POST_PROCESS_EPISCORE {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/EpiSCORE", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${sample_id}_episcore_std.csv"
    script: "python3 ${script} '${raw}' '${sample_id}_episcore_std.csv'"
}

process POST_PROCESS_HOUSEMAN {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/Houseman", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${sample_id}_houseman_std.csv"
    script: "python3 ${script} '${raw}' '${sample_id}_houseman_std.csv'"
}

process POST_PROCESS_MEDECOM {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/MeDeCom", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${sample_id}_medecom_std.csv"
    script: "python3 ${script} '${raw}' '${sample_id}_medecom_std.csv'"
}

process POST_PROCESS_REFFREE {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/RefFreeEWAS", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${sample_id}_reffree_std.csv"
    script: "python3 ${script} '${raw}' '${sample_id}_reffree_std.csv'"
}

process POST_PROCESS_PRMETH {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/PRmeth", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${sample_id}_prmeth_std.csv"
    script: "python3 ${script} '${raw}' '${sample_id}_prmeth_std.csv'"
}

process POST_PROCESS_TSISAL {
    tag "$sample_id"
    publishDir "${params.outdir}/standardized/Tsisal", mode: 'copy'
    input: tuple val(sample_id), path(raw); path script
    output: path "${sample_id}_tsisal_std.csv"
    script: "python3 ${script} '${raw}' '${sample_id}_tsisal_std.csv'"
}

// ==============================================================================
// 6. PROCESS - MERGE (汇总)
// ==============================================================================

process MERGE_BENCHMARKS {
    tag "$tool_name"
    publishDir "${params.outdir}/_Performance_Summaries", mode: 'copy'
    
    input:
        tuple val(tool_name), path(csvs)
    output:
        path "${tool_name}_benchmark_summary.csv"
    
    script:
        """
        echo "Sample,Time_Seconds,Peak_Memory_MB,Status" > ${tool_name}_benchmark_summary.csv
        tail -q -n +2 ${csvs} >> ${tool_name}_benchmark_summary.csv
        """
}

process MERGE_TOOL_RESULTS {
    tag "Merge_$method_tag"
    publishDir "${params.outdir}/results", mode: 'copy'
    
    input:
    // 修改：第一个是标签名（如 EpiDISH_RPC），第二个是该标签下的所有文件
    tuple val(method_tag), path(csv_files) 
    
    output:
    path "${method_tag}_results.csv"
    
    script:
    """
    python3 -c "
import pandas as pd
import sys

files = '${csv_files}'.split()
dfs = []
for f in files:
    try:
        df = pd.read_csv(f)
        if 'SampleID' not in df.columns:
            df.insert(0, 'SampleID', f.split('_')[0])
        dfs.append(df)
    except Exception as e:
        print(f'Error reading {f}: {e}')

if dfs:
    final_df = pd.concat(dfs, ignore_index=True)
    if 'SampleID' in final_df.columns:
        try:
             final_df['sort_key'] = final_df['SampleID'].astype(str).str.extract('(\\d+)')[0].astype(float)
             final_df = final_df.sort_values('sort_key').drop(columns=['sort_key'])
        except:
             final_df = final_df.sort_values('SampleID')
    final_df.to_csv('${method_tag}_results.csv', index=False)
else:
    open('${method_tag}_results.csv', 'w').close()
"
    """
}
