#!/usr/bin/env bash
# Aggregate per-CpG WGBS counts into the explicit CelFiE atlas intervals.
# Input and atlas coordinates are 0-based half-open BED intervals.  A WGBS
# record must represent one CpG ([start,start+1)); overlapping atlas markers
# intentionally receive the CpG independently.
set -euo pipefail

INPUT_BED=$1
ATLAS_BED=$2
OUTPUT_MAPPED=$3
SORTED_INPUT="${OUTPUT_MAPPED}.sorted_input.bed"

if [[ ! -s "$INPUT_BED" ]]; then
  echo "CelFiE input BED is empty: $INPUT_BED" >&2
  exit 2
fi
if [[ ! -s "$ATLAS_BED" ]]; then
  echo "CelFiE prepared atlas is empty: $ATLAS_BED" >&2
  exit 2
fi

# Validate the direct native-WGBS contract before bedtools aggregates.  The
# input has at least chrom,start,end,methylated_count,total_count; a sixth beta
# column may be present and is deliberately ignored by CelFiE.
awk -F'\t' '
  NF < 5 { printf("CelFiE input row %d has fewer than five columns\n", NR) > "/dev/stderr"; exit 2 }
  $2 !~ /^[0-9]+$/ || $3 !~ /^[0-9]+$/ || $4 !~ /^[0-9]+(\.[0-9]+)?$/ || $5 !~ /^[0-9]+(\.[0-9]+)?$/ {
    printf("CelFiE input row %d has invalid coordinate/count fields\n", NR) > "/dev/stderr"; exit 2
  }
  ($2 < 0 || $3 != $2 + 1) { printf("CelFiE input row %d is not one 0-based CpG interval [start,start+1)\n", NR) > "/dev/stderr"; exit 2 }
  ($4 > $5) { printf("CelFiE input row %d has methylated count greater than total depth\n", NR) > "/dev/stderr"; exit 2 }
' "$INPUT_BED"

# Keep chrom/start/end in the mapped output.  Step 2 joins on the full key and
# rejects any lost or shifted interval; it never relies on row count/order.
LC_ALL=C sort -k1,1 -k2,2n -k3,3n "$INPUT_BED" > "$SORTED_INPUT"
bedtools map -a "$ATLAS_BED" -b "$SORTED_INPUT" -c 4,5 -o sum,sum -null 0 \
  > "$OUTPUT_MAPPED"
