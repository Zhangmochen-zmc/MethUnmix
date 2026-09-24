#!/usr/bin/env python3
import pandas as pd
import sys
import os

# Nextflow 调用方式: python menet_pre.py <输入文件> <输出文件>
if len(sys.argv) < 3:
    print("Usage: python menet_pre.py <input.bed> <output.cov.gz>")
    sys.exit(1)

input_file = sys.argv[1]
output_file = sys.argv[2]

try:
    # 1. 读取数据 (无表头)
    # 你的 bed 文件是 tab 分隔
    df = pd.read_csv(input_file, sep='\t', header=None)
    
    # 你的文件列对应关系:
    # 0: chr, 1: start, 2: end, 3: meth_count, 4: coverage, 5: rate(0-1)

    # 2. 计算需要的列
    # Unmethylated = Total Coverage (Col 4) - Methylated (Col 3)
    df['unmeth'] = df[4] - df[3]
    
    # Rate 需要从 0-1 转换为 0-100
    df['rate_100'] = df[5] * 100
    
    # 3. 重组列顺序以符合 Bismark 格式
    # Bismark: [chr, start, end, rate(0-100), meth, unmeth]
    df_out = df[[0, 1, 2, 'rate_100', 3, 'unmeth']]
    
    # 4. 保存文件
    # Pandas 检测到 .gz 后缀会自动压缩
    df_out.to_csv(output_file, sep='\t', header=None, index=None)

    print(f"转换成功: {output_file}")

except Exception as e:
    print(f"转换失败 {input_file}: {e}")
    sys.exit(1)