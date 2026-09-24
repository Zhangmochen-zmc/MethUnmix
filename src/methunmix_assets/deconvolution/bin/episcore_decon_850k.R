#!/usr/bin/env Rscript
Sys.setenv(DEMETHFLOW_EPISCORE_PLATFORM = "epic")
bin_dir <- Sys.getenv("DEMETHFLOW_EPISCORE_BIN_DIR")
if (!nzchar(bin_dir)) {
  script_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  bin_dir <- dirname(sub("^--file=", "", script_arg[[1]]))
}
source(file.path(normalizePath(bin_dir, mustWork = TRUE), "episcore_decon_common.R"))
