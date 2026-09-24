process CELFEER_READS_TO_BED {
    tag 'celfeer:reads-to-bed'
    conda { "${projectDir}/envs/celfeer.yml" }
    cpus { params.celfeer_workers as int }

    input:
    path reference_root

    output:
    path '1_ref_bed'

    script:
    """
    python3 ${projectDir}/bin/celfeer_stage.py reads \
      --input ${reference_root} --output 1_ref_bed \
      --script ${projectDir}/tools/celfeer/1_reads_to_bed.py \
      --workers ${task.cpus}
    """
}

process CELFEER_BIN_READS {
    tag 'celfeer:500bp-bins'
    conda { "${projectDir}/envs/celfeer.yml" }
    cpus { params.celfeer_bin_workers as int }

    input:
    path bed_root
    path celfeer_main
    val genome_build

    output:
    path '2_ref_bin'

    script:
    """
    python3 ${projectDir}/bin/celfeer_stage.py bins \
      --input ${bed_root} --output 2_ref_bin \
      --celfeer-main ${celfeer_main} --genome-build '${genome_build}' \
      --workers ${task.cpus}
    """
}

process CELFEER_BUILD_MARKERS {
    tag 'celfeer:markers'
    conda { "${projectDir}/envs/celfeer.yml" }

    input:
    path bin_root
    path celfeer_main

    output:
    path 'output/**'

    script:
    """
    mkdir output
    python3 ${projectDir}/bin/celfeer_stage.py markers \
      --input ${bin_root} --output output \
      --celfeer-main ${celfeer_main} \
      --match-script ${projectDir}/tools/celfeer/6_match.py
    """
}

process CELFIE_MERGE_REFERENCE {
    tag 'celfie:merge-reference'
    conda { "${projectDir}/envs/celfie.yml" }

    input:
    path reference_root

    output:
    path '1_merged.txt'
    path 'cell_types.tsv'

    script:
    """
    python3 ${projectDir}/bin/celfie_stage.py merge \
      --input ${reference_root} --source ${projectDir}/tools/celfie
    """
}

process CELFIE_BUILD_TIMS {
    tag 'celfie:TIMs'
    conda { "${projectDir}/envs/celfie.yml" }

    input:
    path merged
    path cell_types

    output:
    path 'output/**'

    script:
    """
    python3 ${projectDir}/bin/celfie_stage.py tims \
      --input ${merged} --cell-types ${cell_types} --output output \
      --source ${projectDir}/tools/celfie
    """
}

process METDECODE_MERGE_REFERENCE {
    tag 'metdecode:merge-reference'
    conda { "${projectDir}/envs/metdecode.yml" }

    input:
    path reference_root

    output:
    path '1_merged_data'
    path 'cell_types.tsv'

    script:
    """
    python3 ${projectDir}/bin/metdecode_stage.py merge \
      --input ${reference_root} --source ${projectDir}/tools/metdecode
    """
}

process METDECODE_FIND_DMR {
    tag 'metdecode:find-dmr'
    conda { "${projectDir}/envs/metdecode.yml" }

    input:
    path merged_root

    output:
    path '2_250_markers.bed'

    script:
    """
    python3 ${projectDir}/bin/metdecode_stage.py find \
      --input ${merged_root} --source ${projectDir}/tools/metdecode
    """
}

process METDECODE_MAP_ATLAS {
    tag 'metdecode:map-atlas'
    conda { "${projectDir}/envs/metdecode.yml" }

    input:
    path merged_root
    path markers

    output:
    path '3_mapped_atlas_data'

    script:
    """
    python3 ${projectDir}/bin/metdecode_stage.py map \
      --input ${merged_root} --markers ${markers} --source ${projectDir}/tools/metdecode
    """
}

process METDECODE_FINALIZE {
    tag 'metdecode:finalize'
    conda { "${projectDir}/envs/metdecode.yml" }

    input:
    path mapped_root
    path markers
    path cell_types

    output:
    path 'output/**'

    script:
    """
    mkdir output
    python3 ${projectDir}/bin/metdecode_stage.py finish \
      --input ${mapped_root} --markers ${markers} \
      --cell-types ${cell_types} --source ${projectDir}/tools/metdecode \
      --output output
    """
}
