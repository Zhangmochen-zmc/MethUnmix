process RUN_MARKER_TOOL {
    tag "${data_type}:${tool}"
    conda { "${projectDir}/envs/${tool}.yml" }
    cpus {
        tool == 'menet' ? params.menet_workers as int :
        tool == 'celfeer' ? Math.max(params.celfeer_workers as int, params.celfeer_bin_workers as int) : 1
    }

    input:
    tuple val(tool), val(data_type), val(input_data_type), path(reference_root), path(metadata)
    path assets_dir
    path tool_source_dir

    output:
    path 'output/**'
    path 'execution.json'

    script:
    """
    mkdir -p output
    export MENET_WORKERS='${task.cpus}'
    export MENET_CHECK_SORTED='${params.menet_check_sorted}'
    export CELFEER_WORKERS='${params.celfeer_workers}'
    export CELFEER_BIN_WORKERS='${params.celfeer_bin_workers}'
    python3 ${projectDir}/bin/run_tool.py \
      --tool '${tool}' \
      --data-type '${data_type}' \
      --input-data-type '${input_data_type}' \
      --reference-root ${reference_root} \
      --metadata ${metadata} \
      --genome-build '${params.genome_build ?: ''}' \
      --assets-dir ${assets_dir} \
      --source-dir ${tool_source_dir} \
      --seed '${params.seed}' \
      --output-dir output \
      --execution-record execution.json
    """
}
