nextflow.enable.dsl=2

/*
 * BAM is intentionally materialised in a standalone workflow before the
 * normal WGBS doctor and deconvolution workflow.  Native tools then consume
 * the existing BED/PAT interfaces, and WGBS-derived array projection consumes
 * the same six-column BED interface.  Do not add tool-specific BAM branches
 * here.
 */

process PREPROCESS_WGBS_BAM {
    tag "$sample_id"
    cpus { params.preprocess_threads as int }
    errorStrategy 'finish'

    publishDir "${params.preprocess_outdir}/bed", mode: 'copy', pattern: '*.bed'
    publishDir "${params.preprocess_outdir}/pat", mode: 'copy', pattern: '*.pat.gz*'
    publishDir "${params.preprocess_outdir}/beta", mode: 'copy', pattern: '*.lbeta'
    publishDir "${params.preprocess_outdir}/logs", mode: 'copy', pattern: '*.log'
    publishDir "${params.preprocess_outdir}/qc", mode: 'copy', pattern: '*.json'

    input:
    tuple val(sample_id), path(source_bam)

    output:
    tuple val(sample_id), path("${sample_id}.bed"), emit: bed
    tuple val(sample_id), path("${sample_id}.pat.gz"), emit: pat
    tuple val(sample_id), path("${sample_id}.pat.gz.csi"), emit: pat_index
    tuple val(sample_id), path("${sample_id}.lbeta"), emit: lbeta
    tuple val(sample_id), path("${sample_id}.json"), emit: qc
    tuple val(sample_id), path("${sample_id}.bam_qc.json"), emit: bam_qc
    tuple val(sample_id), path("${sample_id}.staging.json"), emit: staging_qc
    path "${sample_id}.log", emit: log

    script:
    """
    exec > >(tee -a '${sample_id}.log') 2>&1
    # The WGBSTools vendor implementation internally builds shell strings
    # containing its absolute runtime/reference paths.  Execute that mutable
    # runtime from a controlled ASCII-only task scratch path so user output
    # directories containing spaces or Unicode remain fully supported.
    WGBS_TASK_SCRATCH="\$(mktemp -d /tmp/demethflow_wgbstools.XXXXXX)"
    trap 'rm -rf -- "\$WGBS_TASK_SCRATCH"' EXIT
    export DEMETHFLOW_WGBS_RUNTIME_PARENT="\$WGBS_TASK_SCRATCH"
    WGBS_TOOLS_RUNTIME="\$(bash '${params.core_root}/deconvolution/wgbs_scripts/prepare_wgbstools_runtime.sh' '${params.genome_build}')"
    unset DEMETHFLOW_WGBS_RUNTIME_PARENT
    echo "demethflow_wgbs_preprocess_sample=${sample_id}"
    echo "genome_build=${params.genome_build}"
    echo "threads=${task.cpus}"
    echo "samtools_sort_memory=${params.preprocess_sort_memory}"
    echo "bam_contig_policy=${params.bam_contig_policy}"
    echo "bam2pat_mapq=10"
    echo "bam2pat_exclude_flags=1796"
    echo "bam2pat_include_flags=auto_by_detected_layout"
    echo "bam2pat_min_cpg=1"
    echo "wgbstools_runtime_source_sha256=\$(sha256sum \"\$WGBS_TOOLS_RUNTIME/src/python/wgbs_tools.py\" | awk '{print \$1}')"
    echo "samtools=\$(samtools --version | head -1)"
    echo "bgzip=\$(bgzip --version 2>&1 | head -1 || true)"
    echo "tabix=\$(tabix --version 2>&1 | head -1 || true)"
    echo "cpg_dictionary_sha256=\$(sha256sum /opt/wgbs_tools/references/${params.genome_build}/CpG.bed.gz | awk '{print \$1}')"

    # The task receives a staged Nextflow input.  A symlink is sufficient:
    # samtools never mutates the source, and avoiding an initial full BAM copy
    # materially reduces local scratch pressure for production-scale inputs.
    ln -s '${source_bam}' input.bam
    PYTHONPATH='${params.core_root}' python3 '${params.core_root}/deconvolution/wgbs_scripts/bam_preprocess.py' validate-bam \\
        --bam input.bam \\
        --chrom-sizes /opt/wgbs_tools/references/${params.genome_build}/chrome.size \\
        --contig-policy '${params.bam_contig_policy}' \\
        --threads ${task.cpus} \\
        --output '${sample_id}.bam_qc.json'

    # Nextflow stages only the declared BAM path.  Preserve a valid source
    # sidecar as a task-local symlink under the exact name htslib resolves for
    # input.bam; never create or alter an index next to the user source.
    resolved_source="\$(readlink -f input.bam)"
    source_index_sidecar=absent
    for source_index in "\${resolved_source}.bai" "\${resolved_source%.bam}.bai" "\${resolved_source}.csi" "\${resolved_source%.bam}.csi"; do
        if [[ -s "\$source_index" ]]; then
            if [[ "\$source_index" == *.csi ]]; then
                ln -s "\$source_index" input.bam.csi
            else
                ln -s "\$source_index" input.bam.bai
            fi
            source_index_sidecar=reused
            break
        fi
    done
    echo "source_index_sidecar=\$source_index_sidecar"

    preprocess_bam=input.bam
    if [[ '${params.bam_contig_policy}' == 'primary-only' ]]; then
        PYTHONPATH='${params.core_root}' python3 '${params.core_root}/deconvolution/wgbs_scripts/bam_preprocess.py' stage-primary-bam \\
            --bam input.bam \\
            --chrom-sizes /opt/wgbs_tools/references/${params.genome_build}/chrome.size \\
            --output '${sample_id}.sorted.bam' \\
            --qc-output '${sample_id}.staging.json' \\
            --threads ${task.cpus} \\
            --sort-memory '${params.preprocess_sort_memory}' \\
            --preflight-qc '${sample_id}.bam_qc.json'
        preprocess_bam='${sample_id}.sorted.bam'
        echo "contig_staging=primary_only"
        echo "sort_strategy=primary_stage_selection_see_staging_qc"
    else
        echo "contig_staging=none"
        printf '{"schema":"demethflow-bam-primary-staging-v1","status":"NOT_APPLIED","contig_policy":"strict"}\n' > '${sample_id}.staging.json'
        if grep -q '"coordinate_sorted": true' '${sample_id}.bam_qc.json'; then
            echo "sort_strategy=reused_validated_coordinate_order"
            cp "\$preprocess_bam" '${sample_id}.sorted.bam'
        else
            echo "sort_strategy=canonical_samtools_sort"
            samtools sort -@ ${task.cpus} -m '${params.preprocess_sort_memory}' -o '${sample_id}.sorted.bam' "\$preprocess_bam"
        fi
    fi
    # CSI is required for valid coordinate-sorted BAMs whose virtual offsets
    # exceed BAI's representable range.  It is universally readable by the
    # pinned samtools/WGBSTools runtime, so use it deterministically rather
    # than attempting BAI and failing only for real-scale inputs.
    samtools index -c -@ ${task.cpus} '${sample_id}.sorted.bam'
    python3 "\$WGBS_TOOLS_RUNTIME/src/python/wgbs_tools.py" bam2pat -@ ${task.cpus} --genome '${params.genome_build}' --no_beta --out_dir . '${sample_id}.sorted.bam'
    test -s '${sample_id}.sorted.pat.gz' || { echo 'PAT_EMPTY: bam2pat produced no PAT' >&2; exit 2; }
    mv '${sample_id}.sorted.pat.gz' '${sample_id}.pat.gz'
    # bam2pat may have emitted a CSI beside its temporary sorted PAT name.
    # Re-index the final, canonical PAT path deterministically; -f is required
    # when a prior task attempt left a target CSI in its sandbox.
    python3 "\$WGBS_TOOLS_RUNTIME/src/python/wgbs_tools.py" index -f -@ ${task.cpus} '${sample_id}.pat.gz'
    test -s '${sample_id}.pat.gz.csi' || { echo 'PAT_INDEX_MISSING: wgbstools index produced no CSI' >&2; exit 2; }
    pat_pattern_count="\$(gzip -cd '${sample_id}.pat.gz' | awk 'END {print NR}')"
    [[ "\$pat_pattern_count" =~ ^[1-9][0-9]*\$ ]] || { echo 'PAT_EMPTY: bam2pat produced zero PAT patterns' >&2; exit 2; }
    echo "pat_pattern_count=\$pat_pattern_count"
    python3 "\$WGBS_TOOLS_RUNTIME/src/python/wgbs_tools.py" pat2beta -@ ${task.cpus} --genome '${params.genome_build}' --lbeta --out_dir . '${sample_id}.pat.gz'
    test -s '${sample_id}.lbeta' || { echo 'LBETA_SIZE_MISMATCH: pat2beta produced no lbeta' >&2; exit 2; }
    PYTHONPATH='${params.core_root}' python3 '${params.core_root}/deconvolution/wgbs_scripts/bam_preprocess.py' lbeta-to-bed \\
        --lbeta '${sample_id}.lbeta' \\
        --cpg-dictionary /opt/wgbs_tools/references/${params.genome_build}/CpG.bed.gz \\
        --output '${sample_id}.bed' \\
        --sample-id '${sample_id}' \\
        --genome-build '${params.genome_build}' \\
        --qc-output '${sample_id}.json'
    test -s '${sample_id}.bed' || { echo 'BED_CONTRACT_INVALID: lbeta conversion produced no BED' >&2; exit 2; }
    # Published PAT/CSI/lbeta/BED/log/QC artifacts are complete. Remove only
    # explicit task-local BAM intermediates; source_bam is owned by Nextflow.
    rm -f -- input.bam input.bam.bai input.bam.csi '${sample_id}.sorted.bam' '${sample_id}.sorted.bam.bai' '${sample_id}.sorted.bam.csi'
    """
}

workflow {
    if (!(params.genome_build?.toString()?.toLowerCase() in ['hg19', 'hg38'])) {
        error 'BAM preprocessing requires --genome_build hg19 or hg38'
    }
    if (!(params.preprocess_threads as int > 0)) {
        error '--preprocess_threads must be a positive integer'
    }
    if (!(params.preprocess_sort_memory?.toString() ==~ /^[1-9][0-9]*[KMG]$/)) {
        error '--preprocess_sort_memory must be a positive integer followed by K, M, or G'
    }
    if (!(params.bam_contig_policy?.toString() in ['strict', 'primary-only'])) {
        error '--bam_contig_policy must be strict or primary-only'
    }
    ch_bams = Channel
        .fromPath(params.bam_manifest, checkIfExists: true)
        .splitText()
        .filter { line -> !line.startsWith('sample_id') && line.trim() }
        .map { line ->
            def fields = line.trim().split('\\t', -1)
            if (fields.size() != 3 || !fields[0] || !fields[1]) {
                error "Invalid BAM manifest row: ${line}"
            }
            tuple(fields[0], file(fields[1], checkIfExists: true))
        }
    PREPROCESS_WGBS_BAM(ch_bams)
}
