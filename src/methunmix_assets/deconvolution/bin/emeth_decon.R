#!/usr/bin/env Rscript
suppressPackageStartupMessages(library(argparse))
suppressPackageStartupMessages(library(peakRAM))
suppressPackageStartupMessages(library(EMeth))

parser <- ArgumentParser()
parser$add_argument("--mix", required = TRUE)
parser$add_argument("--ref_rdata", required = TRUE)
parser$add_argument("--emeth_src", default = "")
parser$add_argument("--sample_id", required = TRUE)
parser$add_argument("--min_common_cpgs", type = "integer", default = 20L)
parser$add_argument("--seed", type = "integer", default = 20260826L)
args <- parser$parse_args()
set.seed(args$seed)

objects <- load(args$ref_rdata)
if (!("avg_data_matrix" %in% objects)) stop("EMeth RData must contain avg_data_matrix")
mu <- as.matrix(get("avg_data_matrix"))
storage.mode(mu) <- "numeric"
if (!nrow(mu) || !ncol(mu) || is.null(rownames(mu)) || is.null(colnames(mu))) stop("EMeth reference must be a named numeric matrix")
if (anyDuplicated(rownames(mu)) || anyDuplicated(colnames(mu))) stop("EMeth reference contains duplicate row/column names")
if (anyNA(mu) || any(!is.finite(mu)) || min(mu) < 0 || max(mu) > 1) stop("EMeth reference contains invalid beta values")

bulk <- as.matrix(read.csv(args$mix, row.names = 1, check.names = FALSE))
storage.mode(bulk) <- "numeric"
common <- intersect(rownames(bulk), rownames(mu))
if (length(common) < args$min_common_cpgs) stop(sprintf("EMeth common CpG count %d is below minimum %d", length(common), args$min_common_cpgs))
bulk <- bulk[common, , drop = FALSE]
mu <- mu[common, , drop = FALSE]
all_na <- apply(bulk, 1, function(values) all(is.na(values)))
if (any(all_na)) {
  bulk <- bulk[!all_na, , drop = FALSE]
  mu <- mu[!all_na, , drop = FALSE]
}
# Preserve the CpG-by-sample orientation.  `t(apply(..., 1, ...))` happens to
# preserve the shape for multi-sample matrices, but turns a CpG-by-one-sample
# matrix into one-by-CpG.  EMeth then receives a one-element response vector
# and fails inside its initial linear model.  Impute each sample column in
# place so the adapter has identical semantics for one and many samples.
for (sample_index in seq_len(ncol(bulk))) {
  values <- bulk[, sample_index]
  if (anyNA(values)) {
    replacement <- median(values, na.rm = TRUE)
    if (!is.finite(replacement)) stop("EMeth bulk input has an all-NA sample column")
    values[is.na(values)] <- replacement
    bulk[, sample_index] <- values
  }
}
if (anyNA(bulk) || any(!is.finite(bulk))) stop("EMeth bulk input contains unresolved NA/Inf")

# The vendor EMeth implementation additionally treats a one-element eta
# vector as the requested dimension for diag(eta), rather than as a 1x1
# diagonal matrix.  Keep the user's one-sample result, but give that library
# an internal duplicate solely to satisfy its matrix contract.  The duplicate
# is never emitted and the fixed penalty below makes this path deterministic.
single_sample_adapter <- ncol(bulk) == 1L
emeth_bulk <- bulk
if (single_sample_adapter) {
  emeth_bulk <- cbind(bulk, bulk[, 1])
  colnames(emeth_bulk) <- c(colnames(bulk), paste0(colnames(bulk), "__emeth_internal_duplicate"))
}

run_family <- function(family) {
  purity <- rep(1e-10, ncol(emeth_bulk))
  penalty_grid <- nrow(mu) * (10^seq(-2, 1, 0.5))
  # EMeth's packaged CV routine drops a one-column training matrix to a
  # vector before entering its EM implementation.  The library consequently
  # cannot cross-validate a one-sample input even though the supervised
  # deconvolution itself is well-defined.  Use the deterministic middle grid
  # penalty for that documented library limitation; multi-sample cohorts keep
  # the original CV grid unchanged.
  penalty <- if (single_sample_adapter) penalty_grid[ceiling(length(penalty_grid) / 2)] else penalty_grid
  result <- cv.emeth(emeth_bulk, purity, mu, aber = FALSE, V = "c", init = "default",
                     family = family, nu = penalty, folds = min(5L, nrow(mu)),
                     maxiter = 50, verbose = FALSE)
  rho <- as.matrix(result[[1]]$rho)
  if (!identical(dim(rho), c(ncol(emeth_bulk), ncol(mu)))) stop(sprintf("EMeth %s returned unexpected dimensions", family))
  rho <- rho[seq_len(ncol(bulk)), , drop = FALSE]
  colnames(rho) <- colnames(mu)
  rownames(rho) <- colnames(bulk)
  rho[abs(rho) < .Machine$double.eps] <- 0
  if (anyNA(rho) || any(!is.finite(rho)) || any(rho < -1e-8)) stop(sprintf("EMeth %s returned invalid proportions", family))
  rho[rho < 0] <- 0
  totals <- rowSums(rho)
  if (any(totals <= 0)) stop(sprintf("EMeth %s returned a zero-sum sample", family))
  rho <- rho / totals
  rho
}

bench <- list()
perf_log <- peakRAM({
  started <- proc.time()[[3]]
  laplace <- run_family("laplace")
  bench[[1]] <- data.frame(Sample = args$sample_id, Model = "Laplace", Time_Seconds = proc.time()[[3]] - started)
  started <- proc.time()[[3]]
  normal <- run_family("normal")
  bench[[2]] <- data.frame(Sample = args$sample_id, Model = "Normal", Time_Seconds = proc.time()[[3]] - started)
  write.table(laplace, paste0(args$sample_id, "_EMeth_rho_laplace.txt"), sep = "\t", quote = FALSE)
  write.table(normal, paste0(args$sample_id, "_EMeth_rho_normal.txt"), sep = "\t", quote = FALSE)
})
benchmark <- do.call(rbind, bench)
benchmark$Peak_Memory_MB <- perf_log$Peak_RAM_Used_MiB[1]
benchmark$Common_CpGs <- nrow(mu)
benchmark$Cell_Type_Count <- ncol(mu)
benchmark$Seed <- args$seed
benchmark$Penalty_Selection <- if (single_sample_adapter) "single_sample_fixed_middle_grid" else "cross_validated_grid"
benchmark$Single_Sample_Adapter <- if (single_sample_adapter) "internal_duplicate_not_emitted" else "not_used"
benchmark$Status <- "Success"
write.csv(benchmark, paste0(args$sample_id, "_benchmark.csv"), row.names = FALSE)
