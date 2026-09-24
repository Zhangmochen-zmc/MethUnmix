#!/usr/bin/env python3
import os
import sys
import subprocess
import shutil

# 参数:
# 1. main.py 脚本路径
# 2. 输入文件 (上一步生成的文件)
# 3. markers 文件路径
# 4. 工作目录 (wd)
# 5. 基因组版本 (hg38)
# 6. CPU 核心数 (-c)

if len(sys.argv) < 7:
    print("Usage: wrapper <main_script> <input_file> <markers> <wd> <genome> <cores>")
    sys.exit(1)

SCRIPT_MAIN = sys.argv[1]
INPUT_FILE = sys.argv[2]
MARKERS = sys.argv[3]
WORK_DIR = sys.argv[4]
GENOME = sys.argv[5]
CORES = sys.argv[6]

TEMP_IN_DIR = "temp_main_input"
TEMP_OUT_DIR = "temp_main_output"

def main():
    # 1. 准备目录
    if os.path.exists(TEMP_IN_DIR): shutil.rmtree(TEMP_IN_DIR)
    if os.path.exists(TEMP_OUT_DIR): shutil.rmtree(TEMP_OUT_DIR)
    os.makedirs(TEMP_IN_DIR)
    # output_folder 由脚本创建，不需要提前 mkdir，或者看脚本行为
    # 这里我们不用makedirs，让工具自己建

    # 2. 链接输入文件
    filename = os.path.basename(INPUT_FILE)
    os.symlink(os.path.abspath(INPUT_FILE), os.path.join(TEMP_IN_DIR, filename))

    # 3. 构建命令
    # python src/main.py -f input_folder -r regions/markers.tsv -o output_folder -c 10 -g hg38 -wd ...
    seed = os.environ.get("DEMETHFLOW_RANDOM_SEED", "20260826")
    serial_pool = (
        "class _DeMethFlowDeterministicPool:\n"
        "    def __init__(self, *args, **kwargs): pass\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, exc_type, exc, traceback): return False\n"
        "    def starmap(self, function, iterable): return [function(*args) for args in iterable]\n"
    )
    seeded_launcher = (
        "import random,runpy,sys,numpy as np,multiprocessing as mp; "
        f"exec({serial_pool!r},globals()); mp.Pool=_DeMethFlowDeterministicPool; "
        "script=sys.argv[1]; sys.argv=sys.argv[1:]; "
        f"random.seed({int(seed)}); np.random.seed({int(seed)}); "
        "runpy.run_path(script,run_name='__main__')"
    )
    cmd = [
        "python", "-c", seeded_launcher, SCRIPT_MAIN,
        "-f", TEMP_IN_DIR,
        "-r", MARKERS,
        "-o", TEMP_OUT_DIR,
        "-c", str(CORES),
        "-g", GENOME,
        "-wd", WORK_DIR
    ]

    print(f"CMD: {' '.join(cmd)}")

    try:
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["DEMETHFLOW_RANDOM_SEED"] = seed
        env["OMP_NUM_THREADS"] = "1"
        env["MKL_NUM_THREADS"] = "1"
        env["OPENBLAS_NUM_THREADS"] = "1"
        subprocess.run(cmd, check=True, env=env)
    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # 4. 移动结果
    # 将 output_folder 里的内容移出来
    if os.path.exists(TEMP_OUT_DIR):
        for f in os.listdir(TEMP_OUT_DIR):
            shutil.move(os.path.join(TEMP_OUT_DIR, f), f)
            print(f"Output: {f}")
        shutil.rmtree(TEMP_OUT_DIR)
    else:
        print("Warning: Output directory not found.")
    
    shutil.rmtree(TEMP_IN_DIR)

if __name__ == "__main__":
    main()
