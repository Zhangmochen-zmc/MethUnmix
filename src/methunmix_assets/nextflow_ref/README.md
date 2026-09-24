# deconvolution marker selection — Nextflow V1

DSL2 workflow for selecting one, several, or all marker-selection tools for `450k`, `epic`, and `wgbs` inputs.

## End-user command

Copy one template, edit its paths, and run one command:

```bash
cp params/template_epic.yml my_run.yml
./run.sh my_run.yml
```

The MethUnmix 2.0 release launcher defaults to `standard,conda` and runs locally. Slurm scheduling is outside this release's supported and tested scope. The core package does not bundle a SIF; runtime assets must be supplied through the documented offline module mechanism.

Maintainers build the image once on a networked Linux host with `bash bin/build_container.sh`, then run `python3 bin/release_check.py`. A bundle must not be published unless the check passes.

## Usage

```bash
nextflow run main.nf \
  --data_type epic \
  --tools edec,emeth,menet \
  --reference_root /path/to/reference \
  --metadata /path/to/id.txt \
  --genome_build hg38 \
  --outdir results
```

Use `--tools all` for every tool allowed for the selected platform. The workflow seed defaults to `123`.

`uxm` and `methylbert` are explicit native-WGBS construction tools and are not
included by `--tools all`, because each has a distinct input contract. UXM uses
`--uxm_pat_root` plus metadata with sample and cell-type columns. MethylBERT
uses `--methylbert_reads_root`, the build-matched pretrain/data module and a GPU; its
adapter performs a deterministic source-sample-level split and rejects cell
types with fewer than two independent source samples.

```bash
nextflow run main.nf -profile standard,apptainer \
  --data_type wgbs --tools uxm --uxm_pat_root /data/pats \
  --metadata /data/id.csv --genome_build hg38 --outdir results_uxm

nextflow run main.nf -profile standard,apptainer \
  --data_type wgbs --tools methylbert \
  --methylbert_reads_root /data/reads --methylbert_pretrain /data/pretrain_hg38 \
  --metadata /data/id.csv --genome_build hg38 --scenario_id my-tissue \
  --outdir results_methylbert
```

The `methunmix run --mode build` wrapper exposes the same two
CelFEER routes as `--celfeer-pat-root` (raw PAT hierarchy) and
`--celfeer-input-root` plus optional `--celfeer-metadata` (preconverted
read-level hierarchy). CelFEER construction supports native hg19 and hg38 WGBS
and selects both its CpG reference and read bins by `--genome_build`; it never
substitutes one build for the other. MethylBERT hg19 construction additionally
requires `methylbert/pretrain_hg19` in the construction-only `build-assets`
module. `MethylBERT-hg19-data` is currently a reserved module contract rather
than a publishable payload: the trusted hg19 preprocessing source/genome cache
has not been supplied. When available, that separately installed module must
contain the cache used by trained-model deconvolution; a pretrain directory is
not a runtime substitute for it.
Its output is registered with markers, ordered cell types, marker-selection and
unique-region provenance, and strict QC. User-built CelFEER capabilities are
always quarantined until a separate scientific validation promotes a new
immutable reference version.

## Raw and preconverted input modes

`input_mode` controls the MEnet and CelFEER preprocessing branches:

- `auto` (default) uses `menet_input_root` and/or `celfeer_input_root` when supplied and otherwise keeps the original raw-input behavior.
- `raw` always runs the original converters and ignores the preconverted roots.
- `preconverted` requires a preconverted root for every selected MEnet/CelFEER tool. Other selected tools still use `reference_root`, so raw and preconverted tools can be run together.

MEnet-ready input is a directory hierarchy of headerless Bismark coverage files named `*.bismark.cov` or `*.bismark.cov.gz`, with six tab-separated columns: chromosome, start, end, methylation percentage (0–100), methylated count, and unmethylated count. CelFEER-ready input is a hierarchy of read-level `*.txt` files whose first four whitespace-separated columns are read ID, `+`/`-` methylation state, chromosome, and coordinate. Put samples below cell-type directories.

Each root can have its own metadata (`menet_metadata` or `celfeer_metadata`). Metadata uses the configured `sample_id_column` and `cell_type_column` (defaults: `GSM`, `cell_type`). If omitted, it is generated from filenames and immediate parent directories. The workflow checks extensions, non-empty files, columns and numeric ranges, unique sample IDs, metadata/file correspondence, and format-specific consistency before starting the tool. CelFEER validation samples the first 1,000 non-empty rows of every read-level file by default; configure `celfeer_validation_lines` to change that positive limit. Its metadata cell type must match the file's immediate parent directory because the validated original directory is passed directly to CelFEER without a duplicate copy.

Preconverted-only YAML example:

```yaml
data_type: wgbs
tools: menet,celfeer
input_mode: preconverted
reference_root: null
metadata: null
genome_build: hg38
menet_input_root: /data/reference/menet_bismark
menet_metadata: /data/reference/menet_id.csv
celfeer_input_root: /data/reference/celfeer_readlevel
celfeer_metadata: null
outdir: results_preconverted
```

Equivalent command-line example:

```bash
nextflow run main.nf -profile standard,apptainer \
  --data_type wgbs --tools menet,celfeer --input_mode preconverted \
  --menet_input_root /data/reference/menet_bismark \
  --menet_metadata /data/reference/menet_id.csv \
  --celfeer_input_root /data/reference/celfeer_readlevel \
  --genome_build hg38 --outdir results_preconverted
```

For a mixed run, leave `input_mode: auto`, set (for example) `menet_input_root`, and also set `reference_root`; MEnet skips conversion while all other selected tools retain the existing raw path. Existing YAML files with no `input_mode` remain equivalent to `auto` with no preconverted roots and therefore follow the original workflow.

## Metadata

Defaults expect CSV columns `GSM` and `cell_type`; override with `--sample_id_column` and `--cell_type_column`.

`--metadata` is optional. When it is omitted, the workflow recursively reads sample files below `--reference_root`, uses the immediate parent directory as `cell_type`, and uses the filename without `.txt`, `.bed`, or `.bed.gz` as the sample ID. The generated file is published as `results/run_manifest/generated_metadata.csv`.

Array inputs (`450k` and `epic`) are headerless two-column, tab-separated `probe_id, beta` files. WGBS inputs are headerless six-column BED files: chromosome, start, end, two non-negative numeric count fields, and a ratio in `[0,1]`. For scalability, the validator checks that every file is non-empty and validates the first 1,000 non-empty rows by default, including confirmation that column 6 equals column 4 divided by column 5 within 0.001. Set `wgbs_validation_lines` to another positive value to change the sample size. Metadata/file correspondence is always checked across the complete input tree. After validation, downstream processes consume the original reference directory directly rather than copying the complete WGBS dataset into `validated_reference`.

For WGBS runs, EDec, EMeth, EpiDISH, EpiSCORE, Houseman, PRMeth, Tsisal, RefFreeEWAS, and MethAtlas share one conversion step. Nextflow calls `python bin/wgbs_to_850.py input_folder output_folder assets/wgbs_to_850k/850k_cg_info_nona.bed`. This is the packaged form of `wgbs_850k/anno.sh`: it matches chromosome/start/end against the annotation BED and writes the matched probe ID plus input column 6. It preserves the cell-type directory hierarchy. These tools then run as the EPIC/850K platform. MEnet, CelFEER, Celfie, and MetDecode continue to use native WGBS inputs.

## Adapter boundary

The workflow validates and normalizes inputs and generates MEnet metadata. It runs a tool only when its source directory contains a parameter-aware `run_marker.sh`, `run_marker.py`, or `run_marker.R`. Adapters replace fixed paths and fixed input lists while retaining the supplied thresholds and marker parameters. Any generalized cell-count behavior is recorded by the adapter and still requires end-to-end validation before release.

Each tool entrypoint receives `REFERENCE_ROOT`, `REFERENCE_METADATA`, `DATA_TYPE`, `GENOME_BUILD`, `ASSETS_DIR`, `OUTPUT_DIR`, and `DECONVOLUTION_SEED` as environment variables.

## Install fixed resources

```bash
bash bin/install_assets.sh
```

First edit the `USER CONFIGURATION` section at the top of `bin/install_assets.sh` and place the absolute server paths there. Empty paths are skipped. Existing files are not overwritten unless `--force` is supplied.

MEnet window and probe-mapping files are generated by the MEnet workflow and must not be configured as fixed input assets.

For WGBS MEnet runs, the workflow first executes the supplied conversion exactly as `python bismark.py input_folder output_folder`. Package the original script as `bin/bismark.py`; Nextflow locates it through `${projectDir}` and users do not provide a server-specific script path. Converted files are published under `results/menet/pre/menet_bismark/` and become MEnet's reference input.

MEnet then follows `tools/menet/menet_pipline.txt`: array inputs are converted with `array2bismark.py`, Bismark coverage is tiled at 500 and 1000 bp, and `createref_multi.py 1000 12` creates the marker reference. Generated metadata uses `Tissue = MinorGroup = cell_type`.

The packaged WGBS adapter is optimized for the marker workflow: it creates only the required 1,000 bp windows, links staged Bismark coverage files into MEnet's working layout instead of copying them, and runs per-sample tiling concurrently. `menet_workers` defaults to 12 and is used both as the Nextflow CPU request and as the MEnet tiling/reference worker count. With the default `menet_check_sorted: true`, each Bismark file is checked in a streaming pass against the hg38 window chromosome order. Already sorted inputs go directly to `bedtools map`; only unsorted inputs run `bedtools sort`, and their temporary sorted BED is removed after mapping.

At startup, the workflow validates only the assets needed by the selected platform and tools. It exports the resolved paths to tool entrypoints as `ASSET_MANIFEST`, `ASSET_DHS_CPG`, and `CELFEER_MAIN`. Missing resources stop the run before marker selection begins.

## Managed software environments

Use `-profile conda` to let Nextflow create per-process environments with Mamba. WGBS environments include bedtools, so no global bedtools installation is required. Environments are cached under `.conda/` and reused with `-resume`.

On the target server, export the project-local Conda configuration before creating environments or starting Nextflow. This prevents the system and user `.condarc` files from mixing official and mirror channels:

```bash
export CONDARC="$PWD/condarc"
conda config --show-sources
```

```bash
nextflow run main.nf -profile standard,conda [parameters]
```

Apptainer is available as `-profile apptainer`, but tool-specific container images are intentionally not assigned until their exact images are confirmed. Do not combine the current `apptainer` profile with `conda`.

## CelFEER PAT preprocessing

CelFEER additionally accepts cell-type directories containing `*.pat.gz`. Supply them separately:

```bash
nextflow run main.nf \
  --data_type wgbs \
  --tools celfeer \
  --reference_root /path/to/wgbs_bed_reference \
  --celfeer_pat_root /path/to/celfeer_pat_reference \
  --celfeer_cpg_ref /path/to/hg19_cpg_ref.txt
```

Configure `CELFEER_CPG_REF_HG19` and `CELFEER_CPG_REF_HG38` in `bin/install_assets.sh`. They are installed as `assets/celfeer/references/hg19_cpg_ref.txt` and `hg38_cpg_ref.txt`. At runtime, `--genome_build hg19` or `hg38` selects the matching reference automatically. `--celfeer_cpg_ref /path/file` can override that selection. Configure `CELFEER_MAIN` with the author's complete original `CelFEER-main` directory. The workflow calls the supplied `celfeer/pre/batch_change.py input_dir output_dir --reference reference_file` once per PAT-containing cell-type directory and preserves the hierarchy.

CelFEER uses `CelFEER-main/data/hg19_read_bins.txt` for hg19 and `CelFEER-main/data/read_bins.txt` for hg38. A missing file stops the workflow; it never substitutes bins from the other genome build.

For preconverted read-level input, CelFEER processes samples concurrently. `celfeer_workers` defaults to 12 for the streaming read-level-to-BED conversion. The more memory- and I/O-intensive 500 bp binning stage has an independent `celfeer_bin_workers` limit, defaulting to 4. Nextflow requests the larger of these two CPU counts for the CelFEER task. Per-sample intermediates are written atomically through temporary `.partial` paths: a final output name appears only after successful completion, so an existing final output can be reused without persistent marker files.

The downstream native-WGBS deconvolution route uses a separate PAT adapter.
`celfeer_change.py --weighted-bed` streams PAT multiplicities into a weighted
five-bin BED instead of materializing one BED row per expanded read. The result
is numerically equivalent to the legacy expansion but bounds temporary disk and
memory use. `PRE_CELFEER_STEP1` runs at most two samples concurrently, requests
64 GB, removes weighted intermediates on both success and failure, and passes
the build-specific CpG reference and read-bin file explicitly. Mixture sample
count and output cell labels are derived dynamically; neither is fixed to the
historical immune6 layout.
