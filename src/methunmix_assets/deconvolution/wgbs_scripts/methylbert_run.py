#!/usr/bin/env python3
import os
import subprocess
import argparse
import sys

def run_single_methylbert(input_file, model_path, output_dir, batch_size):
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)

    # 获取绝对路径，防止目录切换导致找不到文件
    abs_input = os.path.abspath(input_file)
    abs_model = os.path.abspath(model_path)
    abs_output = os.path.abspath(output_dir)

    print(f"Processing: {abs_input}")
    print(f"Model: {abs_model}")
    print(f"Output: {abs_output}")

    # 构建命令
    cmd = [
        "methylbert", "deconvolute",
        "-i", abs_input,
        "-m", abs_model,
        "-o", abs_output,
        "-b", str(batch_size)
    ]

    print(f"CMD: {' '.join(cmd)}")
    
    # 执行
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error processing {input_file}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input', required=True, help="Single .kmers_fixed.csv file")
    parser.add_argument('-m', '--model', required=True, help="Path to model folder")
    parser.add_argument('-o', '--output_dir', required=True, help="Output directory")
    parser.add_argument('-b', '--batch_size', default=128, help="Batch size")
    
    args = parser.parse_args()
    
    run_single_methylbert(args.input, args.model, args.output_dir, args.batch_size)