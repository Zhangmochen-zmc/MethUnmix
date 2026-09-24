#!/usr/bin/env Rscript

suppressPackageStartupMessages({
  library(argparse)
  library(MeDeCom)
  library(peakRAM)
  library(jsonlite)
})

parser <- ArgumentParser(description = "Cohort-level MeDeCom deconvolution")
parser$add_argument("--mix", required = TRUE, help = "CpG x N bulk beta matrix CSV")
parser$add_argument("--cohort_id", required = TRUE)
parser$add_argument("--k", type = "integer", required = TRUE)
parser$add_argument("--lambda", dest = "lambda_mode", default = "auto", help = "auto or a non-negative value")
parser$add_argument("--lambdas", default = "0,1e-5,1e-4,1e-3,1e-2,1e-1")
parser$add_argument("--nfolds", type = "integer", default = 0, help = "0 selects min(10,N)")
parser$add_argument("--ncores", type = "integer", default = 1)
parser$add_argument("--seed", type = "integer", default = 20260826)
args <- parser$parse_args()

fail <- function(message) stop(message, call. = FALSE)
cohort_id <- args$cohort_id
if (args$k < 1) fail("MeDeCom K must be a positive integer")
if (args$ncores < 1) fail("MeDeCom ncores must be a positive integer")

bulk <- as.matrix(read.csv(args$mix, row.names = 1, check.names = FALSE))
storage.mode(bulk) <- "double"
if (nrow(bulk) < 1 || ncol(bulk) < 1) fail("MeDeCom input matrix is empty")
if (is.null(rownames(bulk)) || any(!nzchar(rownames(bulk))) || anyDuplicated(rownames(bulk))) {
  fail("MeDeCom input CpG IDs must be non-empty and unique")
}
if (is.null(colnames(bulk)) || any(!nzchar(colnames(bulk))) || anyDuplicated(colnames(bulk))) {
  fail("MeDeCom input SampleIDs must be non-empty and unique")
}
if (any(!is.finite(bulk))) fail("MeDeCom input contains NA, Inf, or non-numeric values")
if (any(bulk < 0 | bulk > 1)) fail("MeDeCom input beta values must be in [0,1]")

sample_ids <- colnames(bulk)
n_samples <- ncol(bulk)
if (n_samples <= args$k) fail(sprintf("MeDeCom requires N > K; observed N=%d, K=%d", n_samples, args$k))
nfolds <- if (args$nfolds == 0) min(10L, n_samples) else args$nfolds
if (nfolds < 2 || nfolds > n_samples) {
  fail(sprintf("MeDeCom nfolds must satisfy 2 <= nfolds <= N; observed nfolds=%d, N=%d", nfolds, n_samples))
}

variable <- apply(bulk, 1, function(x) max(x) - min(x) > 1e-12)
minimum_variable <- max(100L, 10L * args$k)
if (sum(variable) < minimum_variable) {
  fail(sprintf("MeDeCom requires at least %d non-constant CpGs; observed %d", minimum_variable, sum(variable)))
}
bulk_model <- bulk[variable, , drop = FALSE]

fixed_grid <- as.numeric(strsplit(args$lambdas, ",", fixed = TRUE)[[1]])
if (!length(fixed_grid) || any(!is.finite(fixed_grid)) || any(fixed_grid < 0) || anyDuplicated(fixed_grid)) {
  fail("MeDeCom lambda grid must contain unique non-negative finite values")
}
lambda_mode <- tolower(trimws(args$lambda_mode))
if (identical(lambda_mode, "auto")) {
  run_lambdas <- fixed_grid
} else {
  selected_requested <- suppressWarnings(as.numeric(lambda_mode))
  if (length(selected_requested) != 1 || !is.finite(selected_requested) || selected_requested < 0) {
    fail("--lambda must be auto or a non-negative finite number")
  }
  run_lambdas <- selected_requested
}

set.seed(args$seed)
started <- proc.time()[["elapsed"]]
monitor <- peakRAM({
  model <- runMeDeCom(
    bulk_model,
    Ks = args$k,
    lambdas = run_lambdas,
    NINIT = 10,
    NFOLDS = nfolds,
    ITERMAX = 300,
    NCORES = args$ncores,
    random.seed = args$seed
  )
})
elapsed <- proc.time()[["elapsed"]] - started

k_index <- match(args$k, model@parameters$Ks)
cve <- as.numeric(model@outputs[[1]]$cve[k_index, , drop = TRUE])
if (length(cve) != length(run_lambdas) || any(!is.finite(cve))) fail("MeDeCom returned invalid CVE values")
if (identical(lambda_mode, "auto")) {
  minimum <- min(cve)
  tolerance <- sqrt(.Machine$double.eps) * max(1, abs(minimum))
  tied <- which(cve <= minimum + tolerance)
  selected_lambda <- max(run_lambdas[tied])
} else {
  selected_lambda <- run_lambdas[[1]]
}

A <- as.matrix(MeDeCom::getProportions(model, K = args$k, lambda = selected_lambda))
Tmat <- as.matrix(MeDeCom::getLMCs(model, K = args$k, lambda = selected_lambda))
if (!identical(dim(A), c(args$k, n_samples))) {
  fail(sprintf("MeDeCom proportions have shape %s; expected %dx%d", paste(dim(A), collapse = "x"), args$k, n_samples))
}
if (!identical(dim(Tmat), c(nrow(bulk_model), args$k))) {
  fail(sprintf("MeDeCom LMCs have shape %s; expected %dx%d", paste(dim(Tmat), collapse = "x"), nrow(bulk_model), args$k))
}
if (any(!is.finite(A)) || any(A < -1e-10)) fail("MeDeCom returned invalid component weights")
A[A < 0] <- 0
column_sums <- colSums(A)
if (any(!is.finite(column_sums)) || any(abs(column_sums - 1) > 1e-4)) {
  fail("MeDeCom component weights do not sum to one")
}
A <- sweep(A, 2, column_sums, "/")
if (any(!is.finite(Tmat))) fail("MeDeCom returned non-finite latent components")

component_names <- paste0("Component_", seq_len(args$k))
results <- data.frame(SampleID = sample_ids, t(A), check.names = FALSE)
colnames(results) <- c("SampleID", component_names)
write.csv(results, "MeDeCom_results.csv", row.names = FALSE, quote = FALSE)

latent <- data.frame(CpG = rownames(bulk_model), Tmat, check.names = FALSE)
colnames(latent) <- c("CpG", component_names)
write.csv(latent, "latent_components.csv", row.names = FALSE, quote = FALSE)
write.table(data.frame(SampleID = sample_ids), "input_samples.tsv", sep = "\t", row.names = FALSE, quote = FALSE)

cv <- data.frame(K = args$k, lambda = run_lambdas, mean_cve = cve, selected = run_lambdas == selected_lambda)
write.csv(cv, "cross_validation.csv", row.names = FALSE, quote = FALSE)

reconstruction <- Tmat %*% A
relative_error <- sqrt(sum((bulk_model - reconstruction)^2)) / sqrt(sum(bulk_model^2))
null_reconstruction <- matrix(rowMeans(bulk_model), nrow = nrow(bulk_model), ncol = n_samples)
null_error <- sqrt(sum((bulk_model - null_reconstruction)^2)) / sqrt(sum(bulk_model^2))
reconstruction_qc <- list(
  status = if (is.finite(relative_error) && relative_error < null_error) "PASS" else "FAIL",
  input_cpg_count = nrow(bulk),
  modeled_cpg_count = nrow(bulk_model),
  discarded_constant_cpg_count = sum(!variable),
  relative_frobenius_error = unname(relative_error),
  null_relative_frobenius_error = unname(null_error),
  improves_over_null = isTRUE(relative_error < null_error)
)
write_json(reconstruction_qc, "reconstruction_qc.json", auto_unbox = TRUE, pretty = TRUE)

parameters <- list(
  schema = "demethflow-medecom-parameters-v2",
  cohort_id = cohort_id,
  selected_k = args$k,
  selected_lambda = selected_lambda,
  lambda_mode = lambda_mode,
  lambda_grid = unname(run_lambdas),
  mean_cve = unname(cve),
  nfolds = nfolds,
  ninit = 10,
  itermax = 300,
  seed = args$seed,
  ncores = args$ncores,
  sample_count = n_samples,
  cpg_count = nrow(bulk_model),
  fit_status = "PASS",
  cve_finite = all(is.finite(cve)),
  authoritative_accessors_succeeded = TRUE,
  medecom_version = as.character(packageVersion("MeDeCom")),
  r_version = R.version.string
)
write_json(parameters, "selected_parameters.json", auto_unbox = TRUE, pretty = TRUE)
write_json(
  list(
    schema = "reference_free_components_v2",
    tool = "MeDeCom",
    cohort_id = cohort_id,
    anonymous_components = TRUE,
    sample_axis = "rows",
    component_axis = "columns",
    selected_parameters = "selected_parameters.json"
  ),
  "MeDeCom_metadata.json", auto_unbox = TRUE, pretty = TRUE
)

saveRDS(model, "raw_model.rds")
pdf("diagnostic.pdf", width = 10, height = 7)
try(plotParameters(model), silent = TRUE)
try(plotParameters(model, K = args$k, lambdaScale = "log"), silent = TRUE)
dev.off()

benchmark <- data.frame(
  Sample = cohort_id,
  Time_Seconds = if (is.finite(monitor$Elapsed_Time_sec[[1]])) monitor$Elapsed_Time_sec[[1]] else elapsed,
  Peak_Memory_MB = monitor$Peak_RAM_Used_MiB[[1]],
  Status = "Success"
)
write.csv(benchmark, paste0(cohort_id, "_benchmark.csv"), row.names = FALSE, quote = FALSE)
