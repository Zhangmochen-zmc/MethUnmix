process BUILD_UXM_REFERENCE {
    tag 'uxm:atlas'
    cpus 8

    input:
    path pat_root
    path metadata
    val genome_build
    path uxm_source

    output:
    path 'output/**'

    script:
    """
    work_name=uxm_reference_${genome_build}
    python3 ${projectDir}/bin/uxm_build_adapter.py prepare \
      --pat-root ${pat_root} --metadata ${metadata} --output "\$work_name"
    mkdir .uxm-bin
    ln -s ${uxm_source}/src/uxm.py .uxm-bin/uxm
    export PATH="\$PWD/.uxm-bin:\${PATH}"
    export PYTHONPATH=${uxm_source}/src:"\${PYTHONPATH:-}"
    UXM_PIPELINE_SKIP_CONDA=1 bash ${uxm_source}/build_uxm_atlas.sh \
      "\$work_name" '${genome_build}' ${task.cpus}
    python3 ${projectDir}/bin/uxm_build_adapter.py finalize --work "\$work_name" --output output
    """
}

process BUILD_METHYLBERT_REFERENCE {
    tag 'methylbert:sample-split-and-finetune'
    cpus 8

    input:
    path reads_root
    path metadata
    path pretrain
    val scenario
    val genome_build

    output:
    path 'output/**'

    script:
    """
    python ${projectDir}/bin/methylbert_build_adapter.py prepare \
      --reads-root ${reads_root} --metadata ${metadata} --scenario '${scenario}' \
      --genome-build '${genome_build}' --seed ${params.seed} --output prepared
    mkdir trained
    python -c 'import random,numpy as np,torch; seed=${params.seed}; random.seed(seed); np.random.seed(seed); torch.manual_seed(seed); torch.cuda.manual_seed_all(seed); torch.use_deterministic_algorithms(True,warn_only=True); from methylbert.cli import main; raise SystemExit(main())' \
      finetune -c prepared/train_seq.kmers_fixed.csv -t prepared/test_seq.kmers_fixed.csv \
      -o trained -p ${pretrain} -s 160 --loss focal_bce -b 256 -e 1000 --lr 4e-4 --with_cuda
    python ${projectDir}/bin/methylbert_build_adapter.py finalize \
      --prepared prepared --model trained --output output
    """
}
