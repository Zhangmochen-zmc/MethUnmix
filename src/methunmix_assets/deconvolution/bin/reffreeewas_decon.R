#!/usr/bin/env Rscript
library(argparse)
library(peakRAM)
library(quadprog)
library(RefFreeEWAS)

# ==============================================================================
# 【全局补丁】劫持 solve.QP 防止正定性报错
# ==============================================================================
if (!exists("original_solve_QP_handle")) {
    original_solve_QP_handle <- quadprog::solve.QP
}

robust_solve_QP <- function(Dmat, dvec, Amat, bvec, meq=0, factorized=FALSE) {
    tryCatch({
        original_solve_QP_handle(Dmat, dvec, Amat, bvec, meq, factorized)
    }, error = function(e) {
        if (grepl("positive definite", e$message, ignore.case = TRUE)) {
            jitter_val <- 1e-8
            if (is.matrix(Dmat)) {
                new_Dmat <- Dmat + diag(ncol(Dmat)) * jitter_val
            } else {
                new_Dmat <- Dmat + jitter_val
            }
            original_solve_QP_handle(new_Dmat, dvec, Amat, bvec, meq, factorized)
        } else {
            stop(e)
        }
    })
}

qp_ns <- asNamespace("quadprog")
if (bindingIsLocked("solve.QP", qp_ns)) unlockBinding("solve.QP", qp_ns)
assign("solve.QP", robust_solve_QP, envir = qp_ns)

tryCatch({
    rf_ns <- asNamespace("RefFreeEWAS")
    rf_imp <- parent.env(rf_ns) 
    if (exists("solve.QP", envir = rf_imp)) {
        if (bindingIsLocked("solve.QP", rf_imp)) unlockBinding("solve.QP", rf_imp)
        assign("solve.QP", robust_solve_QP, envir = rf_imp)
    }
}, error = function(e) {})

message("Success: solve.QP patched globally.")
# ==============================================================================

parser <- ArgumentParser()
parser$add_argument("--mix", required=TRUE)
parser$add_argument("--ref", required=TRUE)
parser$add_argument("--sample_id", required=TRUE)
parser$add_argument("--iters", type="integer", default=10)
args <- parser$parse_args()

sample_id <- args$sample_id

monitor_res <- peakRAM({
    df_ref <- read.csv(args$ref, row.names = 1, check.names = FALSE)
    ref_850k <- as.matrix(df_ref)

    raw_data <- read.csv(args$mix, row.names = 1, check.names = FALSE)
    first_data_matrix <- as.matrix(raw_data)
    
    first_data_matrix_num <- apply(first_data_matrix, 2, as.numeric)
    if(is.null(dim(first_data_matrix_num))) {
        first_data_matrix_num <- as.matrix(first_data_matrix_num)
        colnames(first_data_matrix_num) <- colnames(first_data_matrix)
    }
    rownames(first_data_matrix_num) <- rownames(first_data_matrix)

    common_rows <- intersect(rownames(first_data_matrix_num), rownames(ref_850k))
    if (length(common_rows) < 100) stop("Common CpGs too few (<100).")

    mix_sub <- first_data_matrix_num[common_rows, , drop=FALSE]
    ref_sub <- ref_850k[common_rows, , drop=FALSE]
    
    if (ncol(mix_sub) < 2) {
        stop("RefFreeEWAS requires at least two bulk samples in one input matrix.")
    }
    message("Running RefFreeEWAS iterations...")
    cell_mix <- RefFreeEWAS::RefFreeCellMix(
            Y = mix_sub,
            mu0 = ref_sub,
            iters = args$iters,
            verbose = FALSE
        )
    final_prop <- cell_mix$Omega

    write.table(final_prop,
                file = paste0(sample_id, "_RefFreeEWAS_result.txt"),
                sep = "\t", col.names = NA, quote = FALSE)
})

# --- 改用最稳妥的写法生成 Benchmark 日志，防止粘贴断行报错 ---
log_df <- data.frame(Sample = sample_id)
log_df$Time_Seconds <- monitor_res$Elapsed_Time_sec
log_df$Peak_Memory_MB <- monitor_res$Peak_RAM_Used_MiB
log_df$Status <- "Success"

write.csv(log_df, paste0(sample_id, "_benchmark.csv"), row.names = FALSE)
