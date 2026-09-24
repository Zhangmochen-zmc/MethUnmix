#!/usr/bin/env Rscript
suppressPackageStartupMessages(library(argparse))
suppressPackageStartupMessages(library(EDec))
suppressPackageStartupMessages(library(peakRAM))
suppressPackageStartupMessages(library(clue))

parser <- ArgumentParser()
parser$add_argument("--mix", required = TRUE)
parser$add_argument("--markers", required = TRUE)
parser$add_argument("--tref", required = TRUE)
parser$add_argument("--sample_id", required = TRUE)
parser$add_argument("--k", type = "integer")
parser$add_argument("--min_common_markers", type = "integer", default = 50L)
parser$add_argument("--seed", type = "integer", default = 20260826L)
args <- parser$parse_args()

set.seed(args$seed)
tref <- readRDS(args$tref)
markers <- unique(as.character(unlist(readRDS(args$markers), use.names = FALSE)))
bulk <- as.matrix(read.csv(args$mix, row.names = 1, check.names = FALSE))
storage.mode(bulk) <- "numeric"
if (!is.matrix(tref) || !is.numeric(tref) || !nrow(tref) || !ncol(tref)) stop("EDec Tref must be a non-empty numeric matrix")
if (is.null(rownames(tref)) || is.null(colnames(tref))) stop("EDec Tref requires probe and cell-type names")
if (anyDuplicated(rownames(tref))) stop("EDec Tref contains duplicate probe IDs")
if (anyDuplicated(colnames(tref))) stop("EDec Tref contains duplicate cell-type labels")
if (anyNA(tref) || any(!is.finite(tref))) stop("EDec Tref contains NA/Inf")
k <- ncol(tref)
if (!is.null(args$k) && args$k != k) stop(sprintf("--k=%d conflicts with Tref cell count %d", args$k, k))
common <- Reduce(intersect, list(markers, rownames(tref), rownames(bulk)))
if (length(common) < args$min_common_markers) {
  stop(sprintf("EDec common marker count %d is below minimum %d", length(common), args$min_common_markers))
}
bulk <- bulk[common, , drop = FALSE]
if (anyNA(bulk) || any(!is.finite(bulk))) stop("EDec bulk input contains NA/Inf at common markers")

perf_log <- peakRAM({
  result <- run_edec_stage_1(
    meth_bulk_samples = bulk,
    informative_loci = common,
    num_cell_types = k,
    max_its = 2000,
    rss_diff_stop = 1e-10
  )
  profiles <- as.matrix(result$methylation)
  proportions <- as.matrix(result$proportions)
  if (ncol(profiles) != k || ncol(proportions) != k) stop("EDec returned an unexpected component count")
  match_markers <- Reduce(intersect, list(common, rownames(profiles), rownames(tref)))
  if (length(match_markers) < args$min_common_markers) stop("EDec produced too few profiles for label assignment")
  correlations <- cor(profiles[match_markers, , drop = FALSE], tref[match_markers, , drop = FALSE], use = "pairwise.complete.obs")
  if (!identical(dim(correlations), c(k, k)) || anyNA(correlations) || any(!is.finite(correlations))) {
    stop("EDec component/reference correlation matrix is invalid")
  }
  assignment <- as.integer(solve_LSAP(1 - correlations, maximum = FALSE))
  if (length(unique(assignment)) != k) stop("EDec label assignment is not one-to-one")
  labels <- colnames(correlations)[assignment]
  colnames(proportions) <- labels
  colnames(profiles) <- labels
  if (any(proportions < -1e-8)) stop("EDec returned materially negative proportions")
  proportions[proportions < 0] <- 0
  totals <- rowSums(proportions)
  if (any(!is.finite(totals)) || any(totals <= 0)) stop("EDec returned zero-sum/invalid proportions")
  proportions <- proportions / totals
  write.csv(proportions, paste0(args$sample_id, "_Proportions_Labeled.csv"))
  write.csv(profiles, paste0(args$sample_id, "_Methylation_Labeled.csv"))
  matching_qc <- do.call(rbind, lapply(seq_len(k), function(index) {
    ordered <- order(correlations[index, ], decreasing = TRUE)
    selected <- assignment[index]
    alternatives <- ordered[ordered != selected]
    second <- if (length(alternatives)) alternatives[1] else NA_integer_
    component_names <- rownames(correlations)
    component_id <- if (is.null(component_names) || !nzchar(component_names[index])) paste0("Component_", index) else component_names[index]
    data.frame(
      component_id = component_id,
      assigned_cell_type = colnames(correlations)[selected],
      correlation = correlations[index, selected],
      second_best_cell_type = if (is.na(second)) NA_character_ else colnames(correlations)[second],
      second_best_correlation = if (is.na(second)) NA_real_ else correlations[index, second],
      correlation_margin = if (is.na(second)) NA_real_ else correlations[index, selected] - correlations[index, second],
      common_marker_count = length(match_markers),
      seed = args$seed,
      stringsAsFactors = FALSE
    )
  }))
  write.csv(matching_qc, paste0(args$sample_id, "_EDec_matching_qc.csv"), row.names = FALSE)
})

perf_log$Sample <- args$sample_id
perf_log$Common_Markers <- length(common)
perf_log$Cell_Type_Count <- k
perf_log$Seed <- args$seed
perf_log$Status <- "Success"
write.csv(perf_log, paste0(args$sample_id, "_benchmark.csv"), row.names = FALSE)
