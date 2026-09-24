#!/usr/bin/env Rscript
suppressPackageStartupMessages(library(argparse))
suppressPackageStartupMessages(library(EpiDISH))
suppressPackageStartupMessages(library(peakRAM))

parser <- ArgumentParser(description = "DeMethFlow EpiDISH 450K adapter")
parser$add_argument("--mix", required = TRUE)
parser$add_argument("--own_ref", default = "")
parser$add_argument("--sample_id", required = TRUE)
parser$add_argument("--reference_mode", choices = c("bundle_artifact", "package_builtin"),
                    default = "bundle_artifact")
parser$add_argument("--builtin_reference", default = "")
args <- parser$parse_args()

beta_matrix <- as.matrix(read.csv(args$mix, row.names = 1, check.names = FALSE))
storage.mode(beta_matrix) <- "numeric"
if (!nrow(beta_matrix) || !ncol(beta_matrix) || anyNA(beta_matrix) || any(!is.finite(beta_matrix))) {
  stop("EpiDISH bulk matrix must be a non-empty finite numeric matrix")
}

reference_name <- args$own_ref
if (args$reference_mode == "package_builtin") {
  if (!identical(args$builtin_reference, "centDHSbloodDMC.m")) {
    stop("450K package_builtin mode requires centDHSbloodDMC.m")
  }
  reference_env <- new.env(parent = emptyenv())
  data(list = args$builtin_reference, package = "EpiDISH", envir = reference_env)
  if (!exists(args$builtin_reference, envir = reference_env, inherits = FALSE)) {
    stop(paste("EpiDISH runtime does not contain", args$builtin_reference))
  }
  reference_matrix <- get(args$builtin_reference, envir = reference_env, inherits = FALSE)
  reference_name <- args$builtin_reference
} else {
  if (!nzchar(args$own_ref) || !file.exists(args$own_ref)) stop("bundle_artifact reference is missing")
  reference_matrix <- as.matrix(read.csv(args$own_ref, row.names = 1, check.names = FALSE))
}
reference_matrix <- as.matrix(reference_matrix)
storage.mode(reference_matrix) <- "numeric"
if (!nrow(reference_matrix) || !ncol(reference_matrix) || anyNA(reference_matrix) ||
    any(!is.finite(reference_matrix)) || is.null(rownames(reference_matrix)) || is.null(colnames(reference_matrix))) {
  stop("EpiDISH reference must be a named, non-empty finite numeric matrix")
}

monitor_res <- peakRAM({
  for (method in c("RPC", "CBS", "CP")) {
    estimate <- epidish(beta.m = beta_matrix, ref.m = reference_matrix, method = method)$estF
    if (is.null(estimate) || !nrow(as.matrix(estimate)) || !ncol(as.matrix(estimate))) {
      stop(paste("EpiDISH", method, "returned an empty result"))
    }
    write.table(
      estimate,
      paste0(args$sample_id, "_result_selected_", method, ".txt"),
      sep = "\t", row.names = TRUE, col.names = TRUE, quote = FALSE
    )
  }
})

write.csv(data.frame(
  Sample = args$sample_id,
  Time_Seconds = monitor_res$Elapsed_Time_sec,
  Peak_Memory_MB = monitor_res$Peak_RAM_Used_MiB,
  Status = "Success"
), paste0(args$sample_id, "_benchmark.csv"), row.names = FALSE)
