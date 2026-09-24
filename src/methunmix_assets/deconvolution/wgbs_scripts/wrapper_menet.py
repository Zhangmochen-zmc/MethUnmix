#!/usr/bin/env python3
import os
import sys
import subprocess
import argparse

def run_menet(input_file, model_path, output_dir, input_type, menet_path, bedtools_path):
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    cmd = [
        menet_path, "predict",
        "--input", input_file,
        "--model", model_path,
        "-o", output_dir,
        "--input_type", input_type
    ]
    if bedtools_path:
        cmd.extend(["--bedtools", bedtools_path])

    print(f"Running command: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input', required=True, help="Single .cov.gz file")
    parser.add_argument('-m', '--model', required=True)
    parser.add_argument('-o', '--output_dir', required=True)
    parser.add_argument('--input_type', default='bismark')
    parser.add_argument('--menet_path', required=True)
    parser.add_argument('--bedtools_path', required=True)
    
    args = parser.parse_args()
    
    run_menet(args.input, args.model, args.output_dir, args.input_type, args.menet_path, args.bedtools_path)