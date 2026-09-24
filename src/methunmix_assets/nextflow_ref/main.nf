nextflow.enable.dsl = 2

include { DISCOVER_METADATA } from './modules/common'
include { VALIDATE_INPUT } from './modules/common'
include { DISCOVER_PRECONVERTED_METADATA as DISCOVER_MENET_METADATA } from './modules/common'
include { DISCOVER_PRECONVERTED_METADATA as DISCOVER_CELFEER_METADATA } from './modules/common'
include { VALIDATE_PRECONVERTED_INPUT as VALIDATE_MENET_INPUT } from './modules/common'
include { VALIDATE_PRECONVERTED_INPUT as VALIDATE_CELFEER_INPUT } from './modules/common'
include { PREPARE_ARRAY_REFERENCE } from './modules/common'
include { GENERATE_MENET_METADATA } from './modules/common'
include { MENET_BISMARK_PREPROCESS } from './modules/common'
include { WGBS_TO_850K } from './modules/common'
include { CELFEER_PREPROCESS } from './modules/common'
include { RUN_MARKER_TOOL } from './modules/tools'
include { CELFEER_READS_TO_BED; CELFEER_BIN_READS; CELFEER_BUILD_MARKERS; CELFIE_MERGE_REFERENCE; CELFIE_BUILD_TIMS; METDECODE_MERGE_REFERENCE; METDECODE_FIND_DMR; METDECODE_MAP_ATLAS; METDECODE_FINALIZE } from './modules/heavy_tools'
include { BUILD_UXM_REFERENCE; BUILD_METHYLBERT_REFERENCE } from './modules/specialized_tools'

def supportedTools(String dataType) {
    def common = ['edec','emeth','epidish','episcore','houseman','menet','methatlas','reffreeewas','prmeth','tsisal']
    dataType == 'wgbs' ? common + ['celfeer','celfie','metdecode','uxm','methylbert'] : common + ['aric','methylcibersort']
}

def defaultTools(String dataType) {
    supportedTools(dataType).findAll { !(it in ['uxm','methylbert']) }
}

def requiredAssets(String dataType, List selected, String assetsDir, String genomeBuild) {
    def required = [:]
    def wgbs850kTools = ['edec','emeth','epidish','episcore','houseman','methatlas','reffreeewas','prmeth','tsisal']
    def needsWgbs850k = dataType == 'wgbs' && selected.any { it in wgbs850kTools }
    if (selected.any { it in ['methatlas','menet'] } || needsWgbs850k) {
        required['platform_manifest'] = dataType == '450k' \
            ? "${assetsDir}/manifests/450k/HM450.hg38.manifest.tsv" \
            : (dataType in ['epic','wgbs']) ? "${assetsDir}/manifests/epic/EPIC.hg38.manifest.tsv" : null
    }
    if (needsWgbs850k) {
        required['wgbs_850k_annotation'] = genomeBuild == 'hg19' \
            ? "${assetsDir}/wgbs_to_850k/850k_cg_info_hg19.bed" \
            : "${assetsDir}/wgbs_to_850k/850k_cg_info_nona.bed"
    }
    if ('epidish' in selected && dataType in ['450k','epic','wgbs']) {
        required['dhs_cpg'] = dataType == '450k' \
            ? "${assetsDir}/dhs/450k/filtered_cg_info_450k.bed" \
            : "${assetsDir}/dhs/epic/filtered_cg_info_epic.bed"
    }
    if ('celfeer' in selected) {
        required['celfeer_main'] = "${assetsDir}/celfeer/CelFEER-main"
        if (genomeBuild in ['hg19','hg38']) {
            required['celfeer_cpg_ref'] = "${assetsDir}/celfeer/references/${genomeBuild}_cpg_ref.txt"
            required['celfeer_read_bins'] = genomeBuild == 'hg19' \
                ? "${assetsDir}/celfeer/CelFEER-main/data/hg19_read_bins.txt" \
                : "${assetsDir}/celfeer/CelFEER-main/data/read_bins.txt"
        }
    }
    required.findAll { key, value -> value != null }
}

workflow {
    def dataType = params.data_type.toString().toLowerCase()
    if (!(dataType in ['450k','epic','wgbs'])) {
        error "--data_type must be one of: 450k, epic, wgbs"
    }

    def allowed = supportedTools(dataType)
    def aliases = ['tsisa':'tsisal', 'medecode':'metdecode']
    def requested = params.tools.toString().toLowerCase().split(',')
        .collect { aliases.get(it.trim(), it.trim()) }.findAll { it }.unique()
    def selected = requested == ['all'] ? defaultTools(dataType) : requested
    def invalid = selected - allowed
    if (invalid) error "Unsupported tool(s) for ${dataType}: ${invalid.join(', ')}. Allowed: ${allowed.join(', ')}"

    def inputMode = params.input_mode?.toString()?.toLowerCase() ?: 'auto'
    if (!(inputMode in ['auto','raw','preconverted'])) {
        error "--input_mode must be one of: auto, raw, preconverted"
    }
    def heavyToolMode = params.heavy_tool_mode?.toString()?.toLowerCase() ?: 'split'
    if (!(heavyToolMode in ['split','legacy'])) {
        error "--heavy_tool_mode must be one of: split, legacy"
    }
    def menetPreconverted = 'menet' in selected && inputMode != 'raw' && params.menet_input_root
    def celfeerPreconverted = 'celfeer' in selected && inputMode != 'raw' && params.celfeer_input_root
    if (inputMode == 'preconverted' && 'menet' in selected && !menetPreconverted) {
        error "--input_mode preconverted with MEnet requires --menet_input_root"
    }
    if (inputMode == 'preconverted' && 'celfeer' in selected && !celfeerPreconverted) {
        error "--input_mode preconverted with CelFEER requires --celfeer_input_root"
    }
    def rawTools = selected.findAll { tool ->
        !(tool == 'menet' && menetPreconverted) && !(tool in ['celfeer','uxm','methylbert'])
    }
    if (rawTools && !params.reference_root) {
        error "Raw input is required by ${rawTools.join(', ')}; provide --reference_root"
    }

    log.info "Data type : ${dataType}"
    log.info "Tools     : ${selected.join(', ')}"
    log.info "Input mode: ${inputMode} (MEnet=${menetPreconverted ? 'preconverted' : 'raw'}, CelFEER=${celfeerPreconverted ? 'preconverted' : 'raw'})"
    log.info "Seed      : ${params.seed}"
    log.info "Heavy mode: ${heavyToolMode}"

    if ('uxm' in selected) {
        if (!params.uxm_pat_root) error "UXM construction requires --uxm_pat_root"
        if (!params.metadata) error "UXM construction requires --metadata"
        def build = params.genome_build?.toString()?.toLowerCase()
        if (!(build in ['hg19','hg38'])) error "UXM construction requires --genome_build hg19 or hg38"
        BUILD_UXM_REFERENCE(
            Channel.value(file(params.uxm_pat_root, checkIfExists: true)),
            Channel.value(file(params.metadata, checkIfExists: true)),
            build,
            Channel.value(file("${params.tool_source_dir}/uxm", checkIfExists: true))
        )
    }

    if ('methylbert' in selected) {
        if (!params.methylbert_reads_root) error "MethylBERT construction requires --methylbert_reads_root"
        if (!params.methylbert_pretrain) error "MethylBERT construction requires --methylbert_pretrain"
        if (!params.metadata) error "MethylBERT construction requires --metadata"
        def build = params.genome_build?.toString()?.toLowerCase()
        if (!(build in ['hg19','hg38'])) error "MethylBERT construction requires --genome_build hg19 or hg38"
        BUILD_METHYLBERT_REFERENCE(
            Channel.value(file(params.methylbert_reads_root, checkIfExists: true)),
            Channel.value(file(params.metadata, checkIfExists: true)),
            Channel.value(file(params.methylbert_pretrain, checkIfExists: true)),
            params.scenario_id?.toString() ?: 'user-scenario',
            build
        )
    }

    def genomeBuild = params.genome_build?.toString()?.toLowerCase()
    def wgbs850kTools = ['edec','emeth','epidish','episcore','houseman','methatlas','reffreeewas','prmeth','tsisal']
    def selectedWgbs850k = selected.findAll { it in wgbs850kTools }
    def assetTools = selected.findAll { !(it == 'menet' && menetPreconverted) }
    def assets = requiredAssets(dataType, assetTools, params.assets_dir.toString(), genomeBuild)
    if (params.celfeer_cpg_ref || celfeerPreconverted) assets.remove('celfeer_cpg_ref')
    assets.each { label, assetPath ->
        file(assetPath, checkIfExists: true)
        log.info "Asset     : ${label} = ${assetPath}"
    }

    if (rawTools) {
        reference_ch = Channel.value(file(params.reference_root, checkIfExists: true))
        if (params.metadata) {
            metadata_ch = Channel.value(file(params.metadata, checkIfExists: true))
        } else {
            def defaultMetadata = file("${params.reference_root}/id.txt")
            if (defaultMetadata.exists()) {
                log.info "Metadata  : using ${defaultMetadata}"
                metadata_ch = Channel.value(defaultMetadata)
            } else {
                log.info "Metadata  : id.txt not found; generating it from directory hierarchy"
                discovered = DISCOVER_METADATA(reference_ch, dataType)
                metadata_ch = discovered.metadata
            }
        }
        validated = VALIDATE_INPUT(reference_ch, metadata_ch, dataType, reference_ch)

        if (dataType in ['450k','epic']) {
            prepared = PREPARE_ARRAY_REFERENCE(validated.reference_root, validated.metadata)
        } else {
            prepared = validated
        }

        if ('menet' in rawTools) {
            GENERATE_MENET_METADATA(validated.reference_root, validated.metadata, dataType)
        }
    }

    jobs_ch = Channel.empty()
    if (dataType in ['450k','epic']) {
        def arrayTools = rawTools
        if (arrayTools) {
            jobs_ch = Channel.fromList(arrayTools).combine(Channel.value(dataType))
                .combine(prepared.reference_root).combine(prepared.metadata)
                .map { tool, dtype, refRoot, meta -> tuple(tool, dtype, dataType, refRoot, meta) }
        }
    } else {
        if (selectedWgbs850k) {
            if (!(genomeBuild in ['hg19','hg38'])) {
                error "WGBS-to-850K tools require --genome_build hg19 or hg38"
            }
            def converter = file(params.wgbs_to_850k_script, checkIfExists: true)
            def annotationPath = genomeBuild == 'hg19' \
                ? "${params.assets_dir}/wgbs_to_850k/850k_cg_info_hg19.bed" \
                : "${params.assets_dir}/wgbs_to_850k/850k_cg_info_nona.bed"
            def annotationBed = file(annotationPath, checkIfExists: true)
            wgbs850k = WGBS_TO_850K(validated.reference_root, Channel.value(converter), Channel.value(annotationBed))
            wgbs850kPrepared = PREPARE_ARRAY_REFERENCE(wgbs850k.reference_root, validated.metadata)
            convertedJobs = Channel.fromList(selectedWgbs850k).combine(Channel.value('epic'))
                .combine(wgbs850kPrepared.reference_root).combine(wgbs850kPrepared.metadata)
                .map { tool, dtype, refRoot, meta -> tuple(tool, dtype, 'wgbs', refRoot, meta) }
            jobs_ch = jobs_ch.mix(convertedJobs)
        }
        def nativeWgbsTools = heavyToolMode == 'legacy' ? rawTools.findAll { it in ['celfie','metdecode'] } : []
        if (nativeWgbsTools) {
            nativeJobs = Channel.fromList(nativeWgbsTools).combine(Channel.value('wgbs'))
                .combine(validated.reference_root).combine(validated.metadata)
                .map { tool, dtype, refRoot, meta -> tuple(tool, dtype, 'wgbs', refRoot, meta) }
            jobs_ch = jobs_ch.mix(nativeJobs)
        }
    }

    if ('celfeer' in selected) {
        if (dataType != 'wgbs') error "CelFEER is available only for --data_type wgbs"
        def build = genomeBuild
        if (!(build in ['hg19','hg38'])) error "CelFEER requires --genome_build hg19 or --genome_build hg38"
        if (celfeerPreconverted) {
            celfeer_input_ch = Channel.value(file(params.celfeer_input_root, checkIfExists: true))
            if (params.celfeer_metadata) {
                celfeer_metadata_ch = Channel.value(file(params.celfeer_metadata, checkIfExists: true))
            } else {
                celfeer_discovered = DISCOVER_CELFEER_METADATA(celfeer_input_ch, 'celfeer')
                celfeer_metadata_ch = celfeer_discovered.metadata
            }
            celfeer_pre = VALIDATE_CELFEER_INPUT(celfeer_input_ch, celfeer_metadata_ch, 'celfeer', celfeer_input_ch)
        } else {
            if (!params.celfeer_pat_root) error "Raw CelFEER input requires --celfeer_pat_root containing cell-type directories with *.pat.gz files"
            def defaultCpgRef = "${params.assets_dir}/celfeer/references/${build}_cpg_ref.txt"
            celfeer_ref = params.celfeer_cpg_ref ? file(params.celfeer_cpg_ref, checkIfExists: true) : file(defaultCpgRef, checkIfExists: true)
            celfeer_pre = CELFEER_PREPROCESS(
                Channel.value(file(params.celfeer_pat_root, checkIfExists: true)),
                Channel.value(celfeer_ref)
            )
        }
        if (heavyToolMode == 'legacy') {
            celfeer_job = Channel.value('celfeer').combine(Channel.value(dataType))
                .combine(celfeer_pre.reference_root).combine(celfeer_pre.metadata)
                .map { tool, dtype, refRoot, meta -> tuple(tool, dtype, 'wgbs', refRoot, meta) }
            jobs_ch = jobs_ch.mix(celfeer_job)
        } else {
            celfeer_main_ch = Channel.value(file("${params.assets_dir}/celfeer/CelFEER-main", checkIfExists: true))
            celfeer_beds = CELFEER_READS_TO_BED(celfeer_pre.reference_root)
            celfeer_bins = CELFEER_BIN_READS(celfeer_beds, celfeer_main_ch, build)
            CELFEER_BUILD_MARKERS(celfeer_bins, celfeer_main_ch)
        }
    }

    if (menetPreconverted) {
        menet_input_ch = Channel.value(file(params.menet_input_root, checkIfExists: true))
        if (params.menet_metadata) {
            menet_metadata_ch = Channel.value(file(params.menet_metadata, checkIfExists: true))
        } else {
            menet_discovered = DISCOVER_MENET_METADATA(menet_input_ch, 'menet')
            menet_metadata_ch = menet_discovered.metadata
        }
        menet_ready = VALIDATE_MENET_INPUT(menet_input_ch, menet_metadata_ch, 'menet', menet_input_ch)
        menet_job = Channel.value('menet').combine(Channel.value('wgbs'))
            .combine(menet_ready.reference_root)
            .combine(menet_ready.metadata)
            .map { tool, dtype, refRoot, meta -> tuple(tool, dtype, dataType, refRoot, meta) }
        jobs_ch = jobs_ch.mix(menet_job)
    } else if ('menet' in selected && dataType == 'wgbs') {
        if (!params.menet_bismark_script) error "WGBS MEnet requires --menet_bismark_script"
        menet_bismark = MENET_BISMARK_PREPROCESS(
            validated.reference_root,
            Channel.value(file(params.menet_bismark_script, checkIfExists: true))
        )
        menet_job = Channel.value('menet').combine(Channel.value(dataType))
            .combine(menet_bismark.reference_root)
            .combine(validated.metadata)
            .map { tool, dtype, refRoot, meta -> tuple(tool, dtype, 'wgbs', refRoot, meta) }
        jobs_ch = jobs_ch.mix(menet_job)
    }

    if (heavyToolMode == 'split' && dataType == 'wgbs' && 'celfie' in selected) {
        celfie_merged = CELFIE_MERGE_REFERENCE(validated.reference_root)
        CELFIE_BUILD_TIMS(celfie_merged[0], celfie_merged[1])
    }

    if (heavyToolMode == 'split' && dataType == 'wgbs' && 'metdecode' in selected) {
        metdecode_merged = METDECODE_MERGE_REFERENCE(validated.reference_root)
        metdecode_markers = METDECODE_FIND_DMR(metdecode_merged[0])
        metdecode_mapped = METDECODE_MAP_ATLAS(metdecode_merged[0], metdecode_markers)
        METDECODE_FINALIZE(metdecode_mapped, metdecode_markers, metdecode_merged[1])
    }

    build_assets_ch = Channel.value(file(params.assets_dir, checkIfExists: true))
    build_tools_ch = Channel.value(file(params.tool_source_dir, checkIfExists: true))
    RUN_MARKER_TOOL(jobs_ch, build_assets_ch, build_tools_ch)
}
