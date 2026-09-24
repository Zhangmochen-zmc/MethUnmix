#!/usr/bin/env Rscript
library(argparse)
library(matrixStats)
library(quadprog)
library(peakRAM)

parser <- ArgumentParser()
parser$add_argument("--mix", required=TRUE, help="Input bulk CSV file")
parser$add_argument("--cell_ref", required=TRUE, help="Reference cell types RDS")
parser$add_argument("--beta_ref", required=TRUE, help="Reference beta matrix RDS")
parser$add_argument("--sample_id", required=TRUE)
args <- parser$parse_args()

# ------------------------------------------------------------------------------
# 1. 核心函数 (保留原逻辑)
# ------------------------------------------------------------------------------

select_signature <- function(beta_ref, cell_ref, cellTypes, p_cutoff = 1e-8, n_any = 100) {
  N <- length(cell_ref); K <- length(cellTypes)
  overall <- rowMeans(beta_ref, na.rm = TRUE)
  means <- sapply(cellTypes, function(ct) rowMeans(beta_ref[, cell_ref == ct, drop = FALSE], na.rm = TRUE))
  ns    <- sapply(cellTypes, function(ct) sum(cell_ref == ct))
  vars  <- sapply(cellTypes, function(ct) rowVars(beta_ref[, cell_ref == ct, drop = FALSE], na.rm = TRUE))
  SSW   <- rowSums(t(t(vars) * (ns - 1)), na.rm = TRUE)
  SSB   <- rowSums(t(t((means - overall)^2) * ns), na.rm = TRUE)
  
  Fstat <- (SSB/(K-1)) / (SSW/(N-K))
  pval  <- pf(Fstat, K-1, N-K, lower.tail = FALSE)
  pval[is.na(pval) | !is.finite(pval)] <- 1
  
  rng <- apply(means, 1, function(x) max(x, na.rm = TRUE) - min(x, na.rm = TRUE))
  ok <- which(pval < p_cutoff & is.finite(rng) & rng > 0)
  if (length(ok) == 0) return(NULL)
  
  ix <- ok[order(rng[ok], decreasing = TRUE)]
  return(rownames(beta_ref)[ix[seq_len(min(n_any, length(ix)))]])
}

houseman_qp <- function(Y, W) {
  common <- intersect(rownames(Y), rownames(W))
  Y <- Y[common, , drop=FALSE]; W <- W[common, , drop=FALSE]
  K <- ncol(W); nS <- ncol(Y)
  P <- matrix(NA_real_, nrow = nS, ncol = K, dimnames = list(colnames(Y), colnames(W)))
  
  Amat <- cbind(rep(1, K), diag(K))
  bvec <- c(1, rep(0, K))
  
  for (j in seq_len(nS)) {
    y <- Y[, j]
    ok <- is.finite(y)
    if(sum(ok) < K) next
    Wj <- W[ok, , drop = FALSE]; yj <- y[ok]
    D <- crossprod(Wj) + diag(1e-8, K)
    d <- crossprod(Wj, yj)
    try({
      sol <- solve.QP(Dmat = D, dvec = as.vector(d), Amat = Amat, bvec = bvec, meq = 1)$solution
      P[j, ] <- pmax(sol, 0)
    }, silent = TRUE)
  }
  return(P)
}

# ------------------------------------------------------------------------------
# 2. 执行逻辑
# ------------------------------------------------------------------------------
sample_id <- args$sample_id

monitor_res <- peakRAM({
  # A. 加载数据
  sample_df <- read.csv(args$mix, row.names = 1, check.names = FALSE)
  beta_matrix <- as.matrix(sample_df)
  
  cell_ref_global <- readRDS(args$cell_ref)
  beta_ref_global <- readRDS(args$beta_ref)
  cellTypes <- unique(cell_ref_global)

  # B. 对齐与筛选
  common_cpg <- intersect(rownames(beta_matrix), rownames(beta_ref_global))
  if (length(common_cpg) < 200) stop("Common CpGs too few (<200)")
  
  beta_use <- beta_matrix[common_cpg, , drop = FALSE]
  beta_ref_sub <- beta_ref_global[common_cpg, , drop = FALSE]
  
  # C. 计算 Centroids 和 Signature
  ref_centroids <- sapply(cellTypes, function(ct) {
    rowMeans(beta_ref_sub[, cell_ref_global == ct, drop = FALSE], na.rm = TRUE)
  })
  colnames(ref_centroids) <- cellTypes
  
  feat <- select_signature(beta_ref_sub, cell_ref_global, cellTypes)
  if (is.null(feat)) stop("No signature probes found")

  # D. 解卷积
  W <- ref_centroids[feat, , drop = FALSE]
  Y <- beta_use[feat, , drop = FALSE]
  P <- houseman_qp(Y, W)
  
  # E. 保存
  write.table(P, paste0(sample_id, "_Houseman_QP_result.txt"), sep = "\t", col.names = NA, quote = FALSE)
})

# 记录性能
benchmark_log <- data.frame(
    Sample = sample_id,
    Time_Seconds = monitor_res$Elapsed_Time_sec,
    Peak_Memory_MB = monitor_res$Peak_RAM_Used_MiB,
    Status = "Success"
)
write.csv(benchmark_log, paste0(sample_id, "_benchmark.csv"), row.names = FALSE)
