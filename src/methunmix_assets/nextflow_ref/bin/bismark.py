import os
import pandas as pd
from tqdm import tqdm
import sys
from pathlib import Path

# 输入和输出目录
input_dir = Path(sys.argv[1])
output_dir = Path(sys.argv[2])

output_dir.mkdir(parents=True, exist_ok=True)

# 递归寻找所有 .bed 文件
file_list = list(input_dir.rglob("*.bed"))

print(f"找到 {len(file_list)} 个 BED 文件")

if len(file_list) == 0:
    raise RuntimeError(f"No .bed files found under: {input_dir}")

for f_in_path in tqdm(file_list):

    # 保留相对于 input_dir 的目录结构
    relative_path = f_in_path.relative_to(input_dir)

    # 例如：
    # bcell/GSM5652316.hg38.bed
    #
    # 输出为：
    # bcell/GSM5652316.hg38.bismark.cov.gz

    out_subdir = output_dir / relative_path.parent
    out_subdir.mkdir(parents=True, exist_ok=True)

    f_out_path = out_subdir / (
        f_in_path.stem + ".bismark.cov.gz"
    )

    # 读取 WGBS BED
    df = pd.read_csv(
        f_in_path,
        sep="\t",
        header=None
    )

    # 输入格式：
    # 0 chr 1 start 2 end 3 methylated count 4 total coverage 5 methylation rate (0-1)

    df["unmeth"] = df[4] - df[3]

    # Bismark methylation percentage
    df["rate_100"] = df[5] * 100

    # Bismark coverage format：
    # chr start end percentage methylated unmethylated
    df_out = df[
        [0, 1, 2, "rate_100", 3, "unmeth"]
    ]

    df_out.to_csv(
        f_out_path,
        sep="\t",
        header=None,
        index=None
    )

print(f"处理完成，共生成 {len(file_list)} 个 bismark coverage 文件")