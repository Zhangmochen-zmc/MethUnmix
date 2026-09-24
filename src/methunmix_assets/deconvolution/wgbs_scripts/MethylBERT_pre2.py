#!/usr/bin/env python3
import os
import sys
import subprocess
import shutil

# 参数: 
# 1. 原始脚本路径 (batch_fix.py)
# 2. 输入文件 (单样本)
# 3. 输出目录 (在这个脚本里定义为当前目录下的 output)

if len(sys.argv) < 3:
    print("Usage: python methylbert_step1_fix.py <fix_script> <input_file>")
    sys.exit(1)

FIX_SCRIPT = sys.argv[1]
INPUT_FILE = sys.argv[2]

# 定义临时的“输入文件夹”和“输出文件夹”
# 因为 batch_fix.py 也就是读取文件夹里的文件
TEMP_IN_DIR = "temp_fix_input"
TEMP_OUT_DIR = "temp_fix_output"

def main():
    # 1. 准备环境
    if os.path.exists(TEMP_IN_DIR): shutil.rmtree(TEMP_IN_DIR)
    if os.path.exists(TEMP_OUT_DIR): shutil.rmtree(TEMP_OUT_DIR)
    
    os.makedirs(TEMP_IN_DIR)
    os.makedirs(TEMP_OUT_DIR)

    # 2. 将 Nextflow 传入的文件链接到 临时输入文件夹
    # 获取文件名
    filename = os.path.basename(INPUT_FILE)
    dest_path = os.path.join(TEMP_IN_DIR, filename)
    os.symlink(os.path.abspath(INPUT_FILE), dest_path)

    print(f"Running fix script on {filename}...")
    
    # 3. 调用 batch_fix.py
    # 命令: python batch_fix.py input_folder output_folder
    cmd = ["python", FIX_SCRIPT, TEMP_IN_DIR, TEMP_OUT_DIR]
    
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running batch_fix: {e}")
        sys.exit(1)

    # 4. 整理输出
    # 假设输出在 TEMP_OUT_DIR 里，我们把它们移动到当前目录以便 Nextflow 捕获
    # 假设输出文件后缀有了变化，或者保持原样
    found_files = os.listdir(TEMP_OUT_DIR)
    if not found_files:
        print("Error: No output files generated.")
        sys.exit(1)
        
    for f in found_files:
        shutil.move(os.path.join(TEMP_OUT_DIR, f), f)
        print(f"Generated: {f}")

    # 清理
    shutil.rmtree(TEMP_IN_DIR)
    shutil.rmtree(TEMP_OUT_DIR)

if __name__ == "__main__":
    main()