#!/bin/bash
set -e

# 参数接收
INPUT_FILE="$1"
OUTPUT_DIR="$2"
ATLAS_PATH="$3"
THREADS="$4"

# 确保输出目录存在
mkdir -p "$OUTPUT_DIR"
BASENAME=$(basename "$INPUT_FILE" .pat.gz)
OUTPUT_CSV="${OUTPUT_DIR}/${BASENAME}.csv"

# 直接调用 uxm (它会找到 bin/uxm 这个包装器)
uxm deconv -a "$ATLAS_PATH" -@ "$THREADS" -l 4 "$INPUT_FILE" -o "$OUTPUT_CSV"

if [ -f "$OUTPUT_CSV" ]; then
    echo "UXM Success: $BASENAME"
else
    echo "UXM Failed"
    exit 1
fi