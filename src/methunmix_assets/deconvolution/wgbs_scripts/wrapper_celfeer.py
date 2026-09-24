#!/usr/bin/env python3
import sys
import subprocess
import os

# 参数: script_path, input_file, output_dir, num_samples
if len(sys.argv) < 5:
    print("Usage: wrapper_celfeer.py <script_path> <input_txt> <output_dir> <num_samples>")
    sys.exit(1)

script_path = sys.argv[1]
input_path = sys.argv[2]
output_dir = sys.argv[3]
num_samples = sys.argv[4]

os.makedirs(output_dir, exist_ok=True)

cmd = ["python", script_path, input_path, output_dir, num_samples]
print(f"Running CelFEER: {' '.join(cmd)}")
subprocess.run(cmd, check=True)