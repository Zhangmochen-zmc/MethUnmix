process DISCOVER_METADATA {
    tag "${data_type}"
    conda "${projectDir}/envs/common.yml"

    input:
    path reference_root
    val data_type

    output:
    path 'generated_metadata.csv', emit: metadata

    script:
    """
    python3 ${projectDir}/bin/prepare_inputs.py discover \
      --reference-root ${reference_root} \
      --data-type '${data_type}' \
      --sample-id-column '${params.sample_id_column}' \
      --cell-type-column '${params.cell_type_column}' \
      --output-metadata generated_metadata.csv
    """
}

process VALIDATE_INPUT {
    tag "${data_type}"
    conda "${projectDir}/envs/common.yml"

    input:
    path reference_root
    path metadata
    val data_type
    val source_reference_root

    output:
    val source_reference_root, emit: reference_root
    path 'validated_metadata.csv', emit: metadata
    path 'input_manifest.tsv', emit: manifest

    script:
    """
    python3 ${projectDir}/bin/prepare_inputs.py validate \
      --reference-root ${reference_root} \
      --metadata ${metadata} \
      --data-type '${data_type}' \
      --sample-id-column '${params.sample_id_column}' \
      --cell-type-column '${params.cell_type_column}' \
      --wgbs-validation-lines '${params.wgbs_validation_lines}' \
      --output-metadata validated_metadata.csv \
      --manifest input_manifest.tsv
    """
}

process DISCOVER_PRECONVERTED_METADATA {
    tag "${format}"
    conda "${projectDir}/envs/common.yml"

    input:
    path reference_root
    val format

    output:
    path 'generated_preconverted_metadata.csv', emit: metadata

    script:
    """
    python3 ${projectDir}/bin/validate_preconverted.py discover \
      --reference-root ${reference_root} \
      --format '${format}' \
      --sample-id-column '${params.sample_id_column}' \
      --cell-type-column '${params.cell_type_column}' \
      --output-metadata generated_preconverted_metadata.csv
    """
}

process VALIDATE_PRECONVERTED_INPUT {
    tag "${format}"
    conda "${projectDir}/envs/common.yml"

    input:
    path reference_root
    path metadata
    val format
    val source_reference_root

    output:
    val source_reference_root, emit: reference_root
    path 'validated_preconverted_metadata.csv', emit: metadata
    path 'preconverted_input_manifest.tsv', emit: manifest

    script:
    """
    python3 ${projectDir}/bin/validate_preconverted.py validate \
      --reference-root ${reference_root} \
      --metadata ${metadata} \
      --format '${format}' \
      --sample-id-column '${params.sample_id_column}' \
      --cell-type-column '${params.cell_type_column}' \
      --celfeer-validation-lines '${params.celfeer_validation_lines}' \
      --output-metadata validated_preconverted_metadata.csv \
      --manifest preconverted_input_manifest.tsv
    """
}

process CELFEER_PREPROCESS {
    tag "pat-to-readlevel"
    conda "${projectDir}/envs/celfeer.yml"

    input:
    path pat_root
    path cpg_reference

    output:
    path 'celfeer_readlevel', emit: reference_root
    path 'celfeer_pre_metadata.csv', emit: metadata
    path 'celfeer_pre_manifest.tsv', emit: manifest

    script:
    """
    python3 ${projectDir}/bin/celfeer_pre.py \
      --input-root ${pat_root} \
      --cpg-reference ${cpg_reference} \
      --converter '${params.tool_source_dir}/celfeer/pre/batch_change.py' \
      --output-root celfeer_readlevel \
      --output-metadata celfeer_pre_metadata.csv \
      --manifest celfeer_pre_manifest.tsv
    """
}

process MENET_BISMARK_PREPROCESS {
    tag "bed-to-bismark"
    conda "${projectDir}/envs/menet.yml"

    input:
    path input_folder
    path bismark_script

    output:
    path 'menet_bismark', emit: reference_root

    script:
    """
    mkdir -p menet_bismark
    python ${bismark_script} ${input_folder} menet_bismark
    """
}

process WGBS_TO_850K {
    tag "wgbs-to-850k"
    conda "${projectDir}/envs/common.yml"

    input:
    path input_folder
    path converter_script
    path manifest

    output:
    path 'wgbs_850k_reference', emit: reference_root

    script:
    """
    mkdir -p wgbs_850k_reference
    python ${converter_script} ${input_folder} wgbs_850k_reference ${manifest}
    """
}

process PREPARE_ARRAY_REFERENCE {
    tag "array-reference"
    conda "${projectDir}/envs/common.yml"

    input:
    path reference_root
    path metadata

    output:
    path 'array_reference', emit: reference_root
    path 'reference_metadata.csv', emit: metadata
    path 'reference_matrix.tsv', emit: matrix

    script:
    """
    python3 ${projectDir}/bin/prepare_inputs.py array \
      --reference-root ${reference_root} \
      --metadata ${metadata} \
      --sample-id-column '${params.sample_id_column}' \
      --cell-type-column '${params.cell_type_column}' \
      --output-root array_reference \
      --output-metadata reference_metadata.csv \
      --matrix reference_matrix.tsv
    """
}

process GENERATE_MENET_METADATA {
    tag "menet-metadata"
    conda "${projectDir}/envs/common.yml"

    input:
    path reference_root
    path metadata
    val data_type

    output:
    path 'menet_reference_metadata.csv'

    script:
    """
    python3 ${projectDir}/bin/prepare_inputs.py menet \
      --reference-root ${reference_root} \
      --metadata ${metadata} \
      --data-type '${data_type}' \
      --sample-id-column '${params.sample_id_column}' \
      --cell-type-column '${params.cell_type_column}' \
      --output-metadata menet_reference_metadata.csv
    """
}
