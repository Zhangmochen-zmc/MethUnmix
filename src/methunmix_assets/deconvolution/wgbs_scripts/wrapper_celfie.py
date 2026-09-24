#!/usr/bin/env python3
import sys
import subprocess
import os

# 参数: script_path, input_tsv, output_dir, k_value
if len(sys.argv) < 5:
    print("Usage: wrapper_celfie.py <script_path> <input_tsv> <output_dir> <k_value>")
    sys.exit(1)

script_path = sys.argv[1]
input_file = sys.argv[2]
output_dir = sys.argv[3]
k_value = sys.argv[4]

os.makedirs(output_dir, exist_ok=True)

# 调用 CelFiE 核心脚本 (原 celfie_new.py)
# 注意：你需要确认 celfie_new.py 的参数格式，这里假设是 wrapper 传递进去的格式
cmd = ["python", script_path, input_file, output_dir, k_value]
print(f"Running CelFiE: {' '.join(cmd)}")
subprocess.run(cmd, check=True)