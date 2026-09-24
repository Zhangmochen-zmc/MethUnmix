#!/usr/bin/env Rscript
library(argparse)
library(matrixStats)
library(quadprog)
library(peakRAM)

# ==============================================================================
# PRMeth Strict Reference Mode (Fixed K)
# Skips unstable K estimation logic. Forces usage of Reference cell types.
# ==============================================================================

parser <- ArgumentParser()
parser$add_argument("--mix", required=TRUE, help="Input bulk CSV file")
parser$add_argument("--ref", required=TRUE, help="Reference Matrix CSV")
parser$add_argument("--src_dir", required=TRUE, help="Directory containing PRMeth R source files")
parser$add_argument("--sample_id", required=TRUE)
args <- parser$parse_args()

sample_id <- args$sample_id

# 1. Load PRMeth source functions
cat("Loading PRMeth functions from:", args$src_dir, "\n")
rfiles <- list.files(args$src_dir, pattern="\\.R$", full.names=TRUE)
rfiles <- rfiles[basename(rfiles) != "test.R"]
invisible(lapply(rfiles, source))

monitor_res <- peakRAM({

    # --- Load Data ---
    # Reference
    df_ref <- read.csv(args$ref, row.names = 1, check.names = FALSE)
    ref_mat <- as.matrix(df_ref)
    
    # Bulk Data
    raw_data <- read.csv(args$mix, row.names = 1, check.names = FALSE)
    mix_mat <- as.matrix(raw_data)
    mix_mat <- apply(mix_mat, 2, as.numeric)
    
    # Fix dimension loss for single-column matrix
    if(is.null(dim(mix_mat))) {
        mix_mat <- as.matrix(mix_mat)
        colnames(mix_mat) <- colnames(raw_data)
    }
    rownames(mix_mat) <- rownames(raw_data)
    
    # --- Alignment with Reference ---
    # Force alignment with common CpGs
    common_cpg <- intersect(rownames(mix_mat), rownames(ref_mat))
    if(length(common_cpg) < 50) stop("Too few common CpGs found!")
    
    bulk_common <- mix_mat[common_cpg, , drop = FALSE]
    ref_common <- ref_mat[common_cpg, , drop = FALSE]
    
    # Force K to be the number of cell types in Reference
    fixed_K <- ncol(ref_common)
    N_samples <- ncol(bulk_common)
    
    message(paste0("Running RPMeth in Strict Reference Mode. Fixed K=", fixed_K, ", N=", N_samples))

    # --- Run PRMeth ---
    # A PRMeth failure must remain a PRMeth failure.  Substituting EpiDISH here
    # would make the method label and provenance scientifically incorrect.
    out_result <- prmeth(Y = bulk_common,
                         W1 = ref_common,
                         K = fixed_K,
                         iters = 1000,
                         rssDiffStop = 1e-10)
    final_prop <- out_result$H

    colnames(final_prop) <- colnames(bulk_common)
    rownames(final_prop) <- colnames(ref_common)

    # --- Output Standardization ---
    # Ensure output is Samples (Rows) x Cells (Columns)
    if (nrow(final_prop) == fixed_K && ncol(final_prop) == N_samples) {
        final_prop <- t(final_prop)
    }

    # Write output files (triplicate to satisfy Nextflow expectations)
    write.table(final_prop, paste0(sample_id, "_PRMeth_ref_based.txt"), sep = "\t", col.names = NA, quote = FALSE)

    
    message("Success: Fixed-K deconvolution completed.")
})

# 3. Logging (Safe version)
log_df <- data.frame(Sample = sample_id)
log_df$Time_Seconds <- monitor_res$Elapsed_Time_sec
log_df$Peak_Memory_MB <- monitor_res$Peak_RAM_Used_MiB
log_df$Status <- "Success"
write.csv(log_df, paste0(sample_id, "_benchmark.csv"), row.names = FALSE)
