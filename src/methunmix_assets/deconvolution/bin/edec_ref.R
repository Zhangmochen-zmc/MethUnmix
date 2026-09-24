#!/usr/bin/env Rscript
library(argparse)

parser <- ArgumentParser()
parser$add_argument("--ref_data", required=TRUE, help="refdata.txt")
parser$add_argument("--ref_meta", required=TRUE, help="refmeta.csv")
parser$add_argument("--output", required=TRUE, help="Tref.rds")
args <- parser$parse_args()

cat(">>> 正在从原始数据构建 Tref...\n")
meth_data <- read.table(args$ref_data, row.names = 1, header = TRUE, sep = "\t", check.names = FALSE)
meth_data <- as.matrix(meth_data)
meta_data <- read.csv(args$ref_meta)

unique_cell_types <- unique(meta_data$Tissue)
Tref <- sapply(unique_cell_types, function(ct) {
  samples_for_ct <- meta_data$FileID[meta_data$Tissue == ct]
  subset_data <- meth_data[, samples_for_ct, drop = FALSE]
  rowMeans(subset_data, na.rm = TRUE)
})
Tref <- as.matrix(Tref)

saveRDS(Tref, args$output)
cat("Tref 构建完成并保存至:", args$output, "\n")
