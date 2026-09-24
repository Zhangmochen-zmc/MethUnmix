import pandas as pd
import os
import sys

# --- 配置区域 ---
TARGET_KMER_COUNT = 160
FILE_EXTENSION = '.gz'  # 修改为匹配 .pat.gz

def fix_kmer_file(input_filepath, output_filepath):
    """ 处理单个文件的核心逻辑 """
    print(f"\n>>> Processing {input_filepath} ...")
    
    try:
        # 如果是 .gz 文件，pandas 会自动处理压缩
        df = pd.read_csv(input_filepath, sep='\t')
    except Exception as e:
        print(f"   [Error] Could not read file: {e}")
        return False
    
    # 如果列名不对，打印出来方便调试
    if 'dna_seq' not in df.columns:
        print(f"   [Error] Columns not found. Available: {list(df.columns)}")
        return False

    fixed_dna = []
    fixed_methyl = []

    for index, row in df.iterrows():
        dna_kmers = str(row['dna_seq']).strip().split(' ')
        if len(dna_kmers) > TARGET_KMER_COUNT:
            dna_kmers = dna_kmers[:TARGET_KMER_COUNT]
        
        fixed_dna.append(" ".join(dna_kmers))
        methyl_seq = str(row['methyl_seq'])
        fixed_methyl.append(methyl_seq[:len(dna_kmers)])

    df['dna_seq'] = fixed_dna
    df['methyl_seq'] = fixed_methyl
    
    df.to_csv(output_filepath, sep='\t', index=False)
    print(f"   - Saved to: {output_filepath}")
    return True

def main():
    if len(sys.argv) < 3:
        print("Usage: python methylbert_fix.py <input_path> <output_path> [extra_args...]")
        return

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    # 情况 A: Nextflow 模式 - 输入是一个具体的文件
    if os.path.isfile(input_path):
        # 如果 Nextflow 传了第三个参数作为输出文件名，则使用第三个参数
        final_output = sys.argv[3] if len(sys.argv) > 3 else output_path
        
        # 如果 output_path 是个文件夹，就拼凑文件名；如果是具体文件路径，就直接用
        if os.path.isdir(final_output):
            final_output = os.path.join(final_output, os.path.basename(input_path))
            
        fix_kmer_file(input_path, final_output)

    # 情况 B: 批量模式 - 输入是一个文件夹
    elif os.path.isdir(input_path):
        if not os.path.exists(output_path):
            os.makedirs(output_path)
        
        count = 0
        for filename in os.listdir(input_path):
            if filename.endswith(('.csv', '.gz', '.pat')) and not filename.startswith('.'):
                in_f = os.path.join(input_path, filename)
                out_f = os.path.join(output_path, filename)
                if fix_kmer_file(in_f, out_f):
                    count += 1
        print(f"\n批量处理完成，处理了 {count} 个文件。")

    else:
        print(f"错误: 找不到输入路径 '{input_path}'")

if __name__ == "__main__":
    main()