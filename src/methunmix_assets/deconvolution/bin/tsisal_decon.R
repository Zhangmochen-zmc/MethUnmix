#!/usr/bin/env Rscript
library(argparse)
library(TOAST)
library(peakRAM)

parser <- ArgumentParser()
parser$add_argument("--mix", required=TRUE, help="Input bulk CSV file")
parser$add_argument("--ref", required=TRUE, help="Reference Matrix CSV")
parser$add_argument("--sample_id", required=TRUE)
parser$add_argument("--k", type="integer", default=NULL,
                    help="Optional consistency check; K is derived from the reference")
parser$add_argument("--seed", type="integer", default=20260826)
args <- parser$parse_args()

set.seed(args$seed)

sample_id <- args$sample_id

# --------------------------------------------------------------------
# 核心运行逻辑 (peakRAM 监控)
# --------------------------------------------------------------------
monitor_res <- peakRAM({

    # 1. 加载并预清洗 Reference
    df_ref <- read.csv(args$ref, row.names = 1, check.names = FALSE)
    ref_mat <- as.matrix(df_ref)
    ref_mat <- ref_mat[rowSums(!is.finite(ref_mat)) == 0, , drop=FALSE]

    # 2. 读取并预处理 Bulk 数据
    raw_data <- read.csv(args$mix, row.names = 1, check.names = FALSE)
    bulk_mat <- as.matrix(raw_data)

    # 强制转换为数值型并恢复行名
    bulk_mat_num <- apply(bulk_mat, 2, as.numeric)
    if (is.vector(bulk_mat_num)) {
        bulk_mat_num <- as.matrix(bulk_mat_num)
        colnames(bulk_mat_num) <- colnames(bulk_mat)
    }
    rownames(bulk_mat_num) <- rownames(bulk_mat)

    # 3. 严格数据清洗
    # 剔除 Bulk 中的非有限值
    bulk_mat_num <- na.omit(bulk_mat_num)
    bulk_mat_num <- bulk_mat_num[rowSums(!is.finite(bulk_mat_num)) == 0, , drop=FALSE]

    # 4. 取位点交集
    common_probes <- intersect(rownames(bulk_mat_num), rownames(ref_mat))
    if (length(common_probes) < 100) {
        stop("Valid CpGs too few (<100) after cleaning.")
    }

    data_input <- bulk_mat_num[common_probes, , drop=FALSE]
    ref_input <- ref_mat[common_probes, , drop=FALSE]

    # 5. 方差过滤 (仅针对多样本 CSV)
    if (ncol(data_input) > 1) {
        row_vars <- apply(data_input, 1, var)
        keep_idx <- which(row_vars > 1e-8)
        data_input <- data_input[keep_idx, , drop=FALSE]
        ref_input <- ref_input[rownames(data_input), , drop=FALSE]
    }

    # 6. Run Tsisal.  Never substitute another method while retaining the
    # Tsisal label: N <= K is an input-contract error, not a fallback case.
    # Tsisal 强制要求: 样本数(N) > 细胞类型数(K)
    N_samples <- ncol(data_input)
    K_types <- ncol(ref_input)
    if (!is.null(args$k) && args$k != K_types) {
        stop(paste0("Requested K=", args$k, " does not match reference columns K=", K_types))
    }
    
    if (N_samples <= K_types) {
        stop(paste0(
            "Tsisal requires the number of bulk samples N to exceed the reference cell count K; ",
            "received N=", N_samples, ", K=", K_types, "."
        ))
    }
    message(paste0("Running Tsisal (N=", N_samples, ", K=", K_types, ")..."))
    out <- Tsisal(data_input, K = K_types, knowRef = ref_input)
    estProp <- as.matrix(out$estProp)

    if (is.null(estProp) || length(estProp) == 0 ||
        nrow(estProp) == 0 || ncol(estProp) == 0) {
        stop("Tsisal did not produce a non-empty proportion matrix.")
    }
    # TOAST may return the correct N x K values with generic "Unassigned"
    # column names.  The component order is anchored by knowRef, so recover
    # labels only from the exact frozen reference column order.  Never infer
    # labels from the mixture values or from an immune6 constant.
    if (identical(dim(estProp), c(K_types, N_samples))) {
        estProp <- t(estProp)
    }
    if (!identical(dim(estProp), c(N_samples, K_types))) {
        stop(paste0(
            "Tsisal returned dimensions ", paste(dim(estProp), collapse="x"),
            "; expected N x K = ", N_samples, "x", K_types, "."
        ))
    }
    rownames(estProp) <- colnames(data_input)
    colnames(estProp) <- colnames(ref_input)

    # 7. 保存结果
    write.table(estProp,
                file = paste0(sample_id, "_Tsisal_result.txt"),
                sep = "\t", col.names = NA, quote = FALSE)
})

# --------------------------------------------------------------------
# 性能记录
# --------------------------------------------------------------------
benchmark_log <- data.frame(
    Sample = sample_id,
    Time_Seconds = monitor_res$Elapsed_Time_sec,
    Peak_Memory_MB = monitor_res$Peak_RAM_Used_MiB,
    Status = "Success"
)
write.csv(benchmark_log, paste0(sample_id, "_benchmark.csv"), row.names = FALSE)
