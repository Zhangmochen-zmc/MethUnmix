#!/usr/bin/env python3
import sys
import subprocess
import os

# 参数: script_path, atlas_path, input_file, output_csv
if len(sys.argv) < 5:
    print("Usage: wrapper_metdecode.py <script_path> <atlas_path> <input_tsv> <output_csv>")
    sys.exit(1)

script_path = sys.argv[1]
atlas_path = sys.argv[2]
input_file = sys.argv[3]
output_csv = sys.argv[4]

# 确保输出目录存在
out_dir = os.path.dirname(output_csv)
if out_dir:
    os.makedirs(out_dir, exist_ok=True)

cmd = ["python3", script_path, atlas_path, input_file, output_csv]
print(f"Running MetDecode: {' '.join(cmd)}")
subprocess.run(cmd, check=True)