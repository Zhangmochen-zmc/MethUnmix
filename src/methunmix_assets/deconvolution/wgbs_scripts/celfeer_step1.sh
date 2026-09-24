#!/bin/bash
set -euo pipefail

# 参数
INPUT_PAT=$1
OUTPUT_TXT=$2
REF_CPG=$3
REF_BINS=$4
SCRIPT_CHANGE=$5
SCRIPT_BIS=$6  # retained in the public process interface for compatibility
SCRIPT_SUM=$7
THREADS=${8:-1}  # 获取第 8 个参数作为 CPU 线程数，默认为 1

TMP_BED="temp_step2_${$}.bed"
TMP_WINDOW="temp_step3_${$}.window.bed"
cleanup() {
    rm -f "$TMP_BED" "$TMP_WINDOW"
}
trap cleanup EXIT

echo "=== [Step 1] PAT -> weighted read-bin BED ==="
# PAT count is retained as a weight instead of expanding identical reads.
# This produces the exact same five-bin sufficient statistics without the
# unbounded read-level intermediate and external sort.
python "$SCRIPT_CHANGE" "$REF_CPG" "$INPUT_PAT" --weighted-bed "$TMP_BED"

echo "=== [Step 2] Bed -> Window Bed (sum_reads.py) ==="
# sum_reads 通常涉及区间计算，如果该脚本不支持 --threads，则依然单核
python "$SCRIPT_SUM" "$REF_BINS" "$TMP_BED" "$TMP_WINDOW"

echo "=== [Step 3] Finalizing ==="
cat "$TMP_WINDOW" > "$OUTPUT_TXT"

echo "单样本处理完成: $OUTPUT_TXT"
