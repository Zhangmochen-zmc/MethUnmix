#!/bin/bash
set -euo pipefail

# Arguments: input per-CpG BED, full atlas BED, mapped output, contract,
# contract QC output, and the original complete METH/DEPTH atlas TSV.
INPUT_BED=$1
ATLAS_BED=$2
OUTPUT_MAPPED=$3
CONTRACT=$4
QC_FILE=$5
ATLAS_TSV=$6
RAW_MAPPED="${OUTPUT_MAPPED}.raw"
SORTED_INPUT="${OUTPUT_MAPPED}.sorted_input.bed"
BEDTOOLS_MAPPED="${OUTPUT_MAPPED}.bedtools"

# bedtools performs the production aggregation.  Keep all three atlas
# coordinates so the next stage can reject order, missing-row and 2 bp errors.
LC_ALL=C sort --parallel=1 -k1,1 -k2,2n "$INPUT_BED" > "$SORTED_INPUT"
bedtools map -a "$ATLAS_BED" -b "$SORTED_INPUT" -c 4,5 -o sum,sum -null 0 \
  > "$BEDTOOLS_MAPPED"
cut -f 1,2,3,4,5 "$BEDTOOLS_MAPPED" > "$RAW_MAPPED"

python3 "$CONTRACT" finalise-mapped \
  --atlas "$ATLAS_TSV" \
  --mapped "$RAW_MAPPED" \
  --output "$OUTPUT_MAPPED" \
  --qc "$QC_FILE"
