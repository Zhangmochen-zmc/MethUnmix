suppressPackageStartupMessages(library(argparse))
suppressPackageStartupMessages(library(EpiSCORE))
suppressPackageStartupMessages(library(MASS))
suppressPackageStartupMessages(library(peakRAM))

parser <- ArgumentParser()
parser$add_argument("--mix_cpg", required = TRUE)
parser$add_argument("--ref_gene", required = TRUE)
parser$add_argument("--sample_id", required = TRUE)
parser$add_argument("--min_common_genes", type = "integer", default = 50L)
args <- parser$parse_args()
platform <- Sys.getenv("DEMETHFLOW_EPISCORE_PLATFORM")
if (!(platform %in% c("450k", "epic"))) stop("invalid EpiSCORE platform")

cpg_to_gene <- function(beta_matrix) {
  if (platform == "450k") data("probeInfo450k") else data("probeInfo850k")
  probe_info <- if (platform == "450k") probeInfo450k.lv else probeInfo850k.lv
  map_index <- match(rownames(beta_matrix), probe_info$probeID)
  mapped <- lapply(probe_info, function(values) values[map_index])
  grouped <- list()
  for (group in 1:6) {
    index <- which(mapped$GeneGroup == group & !is.na(mapped$EID))
    if (!length(index)) next
    current <- beta_matrix[index, , drop = FALSE]
    rownames(current) <- mapped$EID[index]
    aggregated <- rowsum(current, group = rownames(current), na.rm = TRUE)
    counts <- table(rownames(current))
    grouped[[group]] <- aggregated / as.numeric(counts[rownames(aggregated)])
  }
  genes <- unique(c(if (!is.null(grouped[[2]])) rownames(grouped[[2]]) else character(),
                    if (!is.null(grouped[[4]])) rownames(grouped[[4]]) else character()))
  result <- matrix(NA_real_, nrow = length(genes), ncol = ncol(beta_matrix),
                   dimnames = list(genes, colnames(beta_matrix)))
  if (!is.null(grouped[[4]])) result[rownames(grouped[[4]]), ] <- grouped[[4]]
  if (!is.null(grouped[[2]])) result[rownames(grouped[[2]]), ] <- grouped[[2]]
  result
}

bulk_cpg <- as.matrix(read.csv(args$mix_cpg, row.names = 1, check.names = FALSE))
storage.mode(bulk_cpg) <- "numeric"
reference <- readRDS(args$ref_gene)
if (!is.matrix(reference) || !is.numeric(reference) || !nrow(reference) || !ncol(reference)) stop("EpiSCORE reference must be a non-empty numeric matrix")
if (is.null(rownames(reference)) || is.null(colnames(reference))) stop("EpiSCORE reference requires gene and cell-type names")
if (anyDuplicated(rownames(reference)) || anyDuplicated(colnames(reference))) stop("EpiSCORE reference has duplicate names")
if (anyNA(reference) || any(!is.finite(reference))) stop("EpiSCORE reference contains NA/Inf")
if (any(apply(reference, 2, function(values) all(values == 0)))) stop("EpiSCORE reference contains an all-zero cell-type column")

perf_log <- peakRAM({
  bulk_gene <- cpg_to_gene(bulk_cpg)
  common <- intersect(rownames(bulk_gene), rownames(reference))
  if (length(common) < args$min_common_genes) stop(sprintf("EpiSCORE common gene count %d is below minimum %d", length(common), args$min_common_genes))
  y <- bulk_gene[common, , drop = FALSE]
  w <- reference[common, , drop = FALSE]
  complete <- apply(cbind(y, w), 1, function(values) all(is.finite(values)))
  y <- y[complete, , drop = FALSE]
  w <- w[complete, , drop = FALSE]
  if (nrow(y) < args$min_common_genes) stop("EpiSCORE has too few complete common genes")
  condition <- kappa(w)
  estimates <- matrix(NA_real_, nrow = ncol(y), ncol = ncol(w), dimnames = list(colnames(y), colnames(w)))
  qc_rows <- vector("list", ncol(y))
  for (sample_index in seq_len(ncol(y))) {
    warnings <- character()
    fit <- withCallingHandlers(
      tryCatch(rlm(y[, sample_index] ~ w, maxit = 100), error = function(error) error),
      warning = function(warning) { warnings <<- c(warnings, conditionMessage(warning)); invokeRestart("muffleWarning") }
    )
    if (inherits(fit, "error")) stop(sprintf("EpiSCORE rlm failed for %s: %s", colnames(y)[sample_index], conditionMessage(fit)))
    coefficients <- coef(fit)[-1]
    if (length(coefficients) != ncol(w) || anyNA(coefficients) || any(!is.finite(coefficients))) stop("EpiSCORE returned invalid coefficients")
    negative_count <- sum(coefficients < 0)
    sum_before <- sum(coefficients)
    coefficients[coefficients < 0] <- 0
    total <- sum(coefficients)
    if (!is.finite(total) || total <= 0) stop("EpiSCORE coefficients sum to zero after clipping")
    estimates[sample_index, ] <- coefficients / total
    qc_rows[[sample_index]] <- data.frame(
      sample_id = colnames(y)[sample_index], common_gene_count = nrow(y),
      converged = isTRUE(fit$converged), warning_count = length(warnings),
      negative_coefficients_before_clipping = negative_count,
      coefficient_sum_before_normalization = sum_before,
      reference_condition_number = condition, stringsAsFactors = FALSE
    )
  }
  write.csv(estimates, paste0("Result_", args$sample_id, ".csv"))
  write.csv(do.call(rbind, qc_rows), paste0(args$sample_id, "_EpiSCORE_fitting_qc.csv"), row.names = FALSE)
})

write.csv(data.frame(Sample = args$sample_id, Time_Seconds = perf_log$Elapsed_Time_sec,
                     Peak_Memory_MB = perf_log$Peak_RAM_Used_MiB, Status = "Success"),
          paste0(args$sample_id, "_benchmark.csv"), row.names = FALSE)
