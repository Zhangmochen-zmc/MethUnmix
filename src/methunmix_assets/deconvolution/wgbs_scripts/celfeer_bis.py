import sys
import os


def add_to_list(out, chrom, start, end, meth_value):
    # 将结果追加到传入的 out 列表中
    if 0.125 <= meth_value < 0.375:
        out.append(chrom+"\t" + str(start) + "\t" + str(end) + "\t0\t1\t0\t0\t0\n")
    elif 0.375 <= meth_value < 0.625:
        out.append(chrom+"\t" + str(start) + "\t" + str(end) + "\t0\t0\t1\t0\t0\n")
    elif 0.625 <= meth_value < 0.875:
        out.append(chrom+"\t" + str(start) + "\t" + str(end) + "\t0\t0\t0\t1\t0\n")
    elif 0.875 <= meth_value <= 1:
        out.append(chrom+"\t" + str(start) + "\t" + str(end) + "\t0\t0\t0\t0\t1\n")
    else:
        out.append(chrom+"\t" + str(start) + "\t" + str(end) + "\t1\t0\t0\t0\t0\n")


if __name__ == "__main__":

    if len(sys.argv) < 3:
        print("Usage: python script.py <out_dir> <input_file>")
        sys.exit(1)

    out_dir = sys.argv[1]
    readfile = sys.argv[2]

    # 【修复1】自动创建输出目录
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    reads = []
    
    # 【修复2】文件名提取逻辑增强（防止因下划线不足而报错）
    base_name = os.path.basename(readfile).split('.')[0]
    try:
        name = base_name.split('_')[2]
    except IndexError:
        name = base_name
        
    outfile = os.path.join(out_dir, name + '.bed')
    
    with open(readfile, 'r') as f:
        meth_count = 0
        total_count = 0
        prev = None
        chrom = ""
        start = -1
        end = -1
        
        for line in f:
            line = line.strip().split()
            if not line: continue
            
            id = line[0]
            meth = line[1]
            curr_chrom = line[2]
            # 【修复3】坐标转为整数 (关键修复，否则比较大小会出错)
            pos = int(line[3])
            
            # 遇到新 Read ID
            if prev is not None and prev != id:
                # 【阈值设定】只保留覆盖位点数 >= 3 的 Reads
                if total_count >= 3:
                    add_to_list(reads, chrom, start, end, meth_count / total_count)
                
                meth_count = 0
                total_count = 0
                start = pos
                end = pos
            
            # 第一行初始化
            if prev is None:
                start = pos
                end = pos

            prev = id
            chrom = curr_chrom
            total_count += 1
            
            if pos > end:
                end = pos
            if pos < start:
                start = pos
                
            if meth == "+":
                meth_count += 1
        
        # 【修复4】循环结束后处理最后一行 (防止遗漏)
        # 同样应用 >= 3 的阈值
        if prev is not None and total_count >= 3:
            add_to_list(reads, chrom, start, end, meth_count / total_count)
            
    with open(outfile, 'w') as out:
        out.writelines(reads)
        print(f"Processed {readfile} -> {outfile}")