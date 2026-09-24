import argparse
import gzip
import sys

def load_cpg_reference(ref_file):
    """
    加载 CpG 坐标参考文件。
    返回一个列表，索引 i 对应全基因组第 i+1 个 CpG 的 (chrom, pos)。
    """
    print("Loading CpG reference...", file=sys.stderr)
    cpg_map = []
    # 如果参考文件很大，这里会消耗较多内存。
    # 每一行代表一个CpG： chr   pos
    with open(ref_file, 'r') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 2:
                # 存储元组 (chr1, 10484)
                cpg_map.append((parts[0], parts[1])) 
    print(f"Loaded {len(cpg_map)} CpG sites.", file=sys.stderr)
    return cpg_map

def convert_pat_to_readlevel(pat_file, cpg_map):
    """
    读取 .pat.gz 并输出为目标格式
    """
    # 自动识别是否为 gz 压缩文件
    opener = gzip.open if pat_file.endswith('.gz') else open
    
    with opener(pat_file, 'rt') as f:
        for line_idx, line in enumerate(f):
            parts = line.strip().split('\t')
            # .pat 格式: chrom, start_index, pattern, count
            if len(parts) < 4: continue
            
            chrom = parts[0]
            start_index = int(parts[1]) # 1-based index
            pattern = parts[2]
            count = int(parts[3]) # 这条 Read 出现的次数
            
            # 解析 Pattern
            # 目标格式: ReadID, MethState, Chrom, Pos
            
            # 1. 提取这条 Read 包含的所有有效位点
            current_read_sites = []
            
            for i, char in enumerate(pattern):
                if char == '.': 
                    continue # 忽略未知状态
                
                # 计算当前位点的全局 Index
                current_global_idx = start_index + i
                
                # 获取真实基因组坐标 (注意: Index 是 1-based, 列表是 0-based)
                # 需要确保 global_idx - 1 在 cpg_map 范围内
                if (current_global_idx - 1) < len(cpg_map):
                    ref_chrom, ref_pos = cpg_map[current_global_idx - 1]
                    
                    # 校验染色体是否匹配 (可选，防止参考文件对不上)
                    if ref_chrom != chrom:
                        continue 

                    meth_state = "+" if char == 'C' else "-"
                    current_read_sites.append((meth_state, chrom, ref_pos))
            
            # 如果这条 Read 没有有效位点，跳过
            if not current_read_sites:
                continue

            # 2. 展开 Count (还原 reads)
            # pat 文件把相同的 reads 合并了，但你的后续脚本是基于单条 read 计算的。
            # 我们需要把 count 展开，生成多个唯一的 Read ID。
            for k in range(count):
                # 生成唯一 ID: pat行号_第k个拷贝
                read_id = f"read_{line_idx}_{k}"
                
                # 输出该 Read 的所有位点
                for state, c, p in current_read_sites:
                    # 输出格式: ID \t State \t Chrom \t Pos
                    print(f"{read_id}\t{state}\t{c}\t{p}")


def methylation_bin(methylated, total, weight):
    """Return the five CelFEER read-bin counts for one compressed PAT row."""
    value = methylated / total
    index = 0
    if 0.125 <= value < 0.375:
        index = 1
    elif 0.375 <= value < 0.625:
        index = 2
    elif 0.625 <= value < 0.875:
        index = 3
    elif 0.875 <= value <= 1:
        index = 4
    counts = [0, 0, 0, 0, 0]
    counts[index] = weight
    return counts


def convert_pat_to_weighted_bed(pat_file, cpg_map, output_file):
    """Convert PAT rows directly to weighted CelFEER read-bin BED records.

    PAT stores repeated identical reads as one pattern plus a count.  Expanding
    that count before binning is mathematically identical to multiplying the
    resulting one-hot read bin by the count, but can create hundreds of GB of
    intermediate text.  This streaming representation preserves the exact
    sufficient statistics consumed by the downstream 500-bp aggregation.
    """
    opener = gzip.open if pat_file.endswith(".gz") else open
    with opener(pat_file, "rt") as source, open(output_file, "w") as output:
        for line in source:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            chrom, start_raw, pattern, weight_raw = parts[:4]
            start_index = int(start_raw)
            weight = int(weight_raw)
            if weight <= 0:
                continue
            positions = []
            methylated = 0
            for offset, state in enumerate(pattern):
                if state == ".":
                    continue
                global_index = start_index + offset
                if not (1 <= global_index <= len(cpg_map)):
                    continue
                ref_chrom, ref_pos = cpg_map[global_index - 1]
                if ref_chrom != chrom:
                    continue
                positions.append(int(ref_pos))
                if state == "C":
                    methylated += 1
            if len(positions) < 3:
                continue
            counts = methylation_bin(methylated, len(positions), weight)
            output.write(
                "\t".join(map(str, [chrom, min(positions), max(positions), *counts])) + "\n"
            )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cpg_reference")
    parser.add_argument("pat")
    parser.add_argument("--weighted-bed")
    args = parser.parse_args()

    cpg_ref = load_cpg_reference(args.cpg_reference)
    if args.weighted_bed:
        convert_pat_to_weighted_bed(args.pat, cpg_ref, args.weighted_bed)
    else:
        convert_pat_to_readlevel(args.pat, cpg_ref)
