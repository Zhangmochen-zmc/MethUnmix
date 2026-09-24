#!/usr/bin/env Rscript

suppressPackageStartupMessages({
  library(argparse)
  library(CIBERSORT)
  library(peakRAM)
})

parser <- ArgumentParser(description = "Methylation-specific CIBERSORT adapter")
parser$add_argument("--mix", required = TRUE, help = "CpG x sample beta CSV in [0,1]")
parser$add_argument("--ref", required = TRUE, help = "MethylCIBERSORT signature TSV in [0,100]")
parser$add_argument("--sample_id", required = TRUE)
parser$add_argument("--perm", type = "integer", default = 1000)
parser$add_argument("--seed", type = "integer", default = 20260826)
parser$add_argument(
  "--sample_workers", type = "integer", default = 1,
  help = "Number of mixture samples evaluated concurrently (each CoreAlg uses three workers)"
)
args <- parser$parse_args()

fail <- function(message) stop(message, call. = FALSE)
validate_matrix <- function(x, label, lower, upper) {
  if (!is.matrix(x) || nrow(x) < 1 || ncol(x) < 1) fail(paste(label, "is empty"))
  if (is.null(rownames(x)) || any(!nzchar(rownames(x))) || anyDuplicated(rownames(x))) {
    fail(paste(label, "CpG IDs must be non-empty and unique"))
  }
  if (is.null(colnames(x)) || any(!nzchar(colnames(x))) || anyDuplicated(colnames(x))) {
    fail(paste(label, "column names must be non-empty and unique"))
  }
  if (any(!is.finite(x))) fail(paste(label, "contains NA, Inf, or non-numeric values"))
  if (any(x < lower | x > upper)) fail(sprintf("%s values must be in [%g,%g]", label, lower, upper))
}

# This is the CIBERSORT 0.1.0 execution path with the expression-only
# max(Y)<50 -> 2^Y branch deliberately removed.  All remaining alignment,
# standardisation, SVR, permutation, correlation and RMSE logic is preserved.
cibersort_methylation <- function(X, Y, maxSize = 500, perm = 0, sample_workers = 1) {
  X <- X[order(rownames(X)), , drop = FALSE]
  Y <- Y[order(rownames(Y)), , drop = FALSE]
  Y <- Y[rownames(Y) %in% rownames(X), , drop = FALSE]
  X <- X[rownames(X) %in% rownames(Y), , drop = FALSE]
  if (nrow(X) < 2 || !identical(rownames(X), rownames(Y))) fail("signature/mixture CpG alignment failed")
  if (sd(as.vector(X)) == 0) fail("signature has zero variance")
  X <- (X - mean(X)) / sd(as.vector(X))
  do_perm <- getFromNamespace("doPerm", "CIBERSORT")
  core_alg <- getFromNamespace("CoreAlg", "CIBERSORT")
  if (perm > 0) {
    nulldist <- sort(do_perm(perm, X, Y)$dist)
  }
  if (!is.numeric(sample_workers) || length(sample_workers) != 1 ||
      !is.finite(sample_workers) || sample_workers < 1 || sample_workers != as.integer(sample_workers)) {
    fail("sample_workers must be a positive integer")
  }
  fit_sample <- function(index) {
    y <- Y[, index]
    if (sd(y) == 0) fail(sprintf("mixture sample %s has zero variance", colnames(Y)[index]))
    y <- (y - mean(y)) / sd(y)
    result <- core_alg(X, y, maxSize)
    pvalue <- if (perm > 0) 1 - (which.min(abs(nulldist - result$mix_r)) / length(nulldist)) else 9999
    c(result$w, pvalue, result$mix_r, result$mix_rmse)
  }
  indices <- seq_len(ncol(Y))
  if (sample_workers == 1 || length(indices) == 1) {
    output <- lapply(indices, fit_sample)
  } else {
    # CoreAlg evaluates its three fixed nu values with three workers.  Two
    # outer sample workers therefore consume the six CPUs declared by the
    # Nextflow process while preserving each sample's exact algorithm and
    # output order.  Permutations, when enabled, are generated once above
    # under the fixed seed and are not repeated in child workers.
    output <- parallel::mclapply(
      indices, fit_sample,
      mc.cores = min(as.integer(sample_workers), length(indices)),
      mc.preschedule = TRUE,
      mc.set.seed = FALSE
    )
  }
  failed <- vapply(output, inherits, logical(1), what = "try-error")
  if (any(failed)) {
    failed_samples <- paste(colnames(Y)[indices[failed]], collapse = ", ")
    details <- paste(vapply(output[failed], as.character, character(1)), collapse = "; ")
    fail(sprintf("MethylCIBERSORT failed for sample(s) %s: %s", failed_samples, details))
  }
  result <- do.call(rbind, output)
  rownames(result) <- colnames(Y)
  colnames(result) <- c(colnames(X), "P-value", "Correlation", "RMSE")
  result
}

set.seed(args$seed)
sample_id <- args$sample_id
out_res <- paste0(sample_id, "_res.txt")
out_bench <- paste0(sample_id, "_benchmark.csv")

monitor <- peakRAM({
  signature <- as.matrix(read.table(args$ref, sep = "\t", header = TRUE, row.names = 1, check.names = FALSE))
  storage.mode(signature) <- "double"
  validate_matrix(signature, "MethylCIBERSORT signature", 0, 100)
  if (max(signature) <= 1) fail("MethylCIBERSORT signature appears to be unscaled beta; required scale is [0,100]")

  mixture <- as.matrix(read.csv(args$mix, header = TRUE, row.names = 1, check.names = FALSE))
  storage.mode(mixture) <- "double"
  validate_matrix(mixture, "MethylCIBERSORT mixture beta matrix", 0, 1)
  mixture_100 <- 100 * mixture

  result <- cibersort_methylation(
    signature, mixture_100,
    perm = args$perm,
    sample_workers = args$sample_workers
  )
  write.table(result, out_res, sep = "\t", row.names = TRUE, col.names = NA, quote = FALSE)
})

benchmark <- data.frame(
  Sample = sample_id,
  Time_Seconds = monitor$Elapsed_Time_sec[[1]],
  Peak_Memory_MB = monitor$Peak_RAM_Used_MiB[[1]],
  Status = "Success"
)
write.csv(benchmark, out_bench, row.names = FALSE, quote = FALSE)
