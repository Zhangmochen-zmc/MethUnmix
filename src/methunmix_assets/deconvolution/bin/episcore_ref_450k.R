#!/usr/bin/env Rscript
library(argparse)
library(EpiSCORE)

parser <- ArgumentParser()
parser$add_argument("--input", required=TRUE, help="Input CpG CSV file")
parser$add_argument("--output", required=TRUE, help="Output Gene RDS file")
parser$add_argument("--type", default="450k", help="450k or 850k")
args <- parser$parse_args()

# 加载探针信息
if(args$type == "450k") { data("probeInfo450k") } else { data("probeInfo850k") }

# 转换函数 (保持原逻辑)
constAvBetaTSS <- function(beta.m, type){
  if(type=="450k"){ probeInfoALL.lv <- probeInfo450k.lv } 
  else { probeInfoALL.lv <- probeInfo850k.lv } 
  
  map.idx <- match(rownames(beta.m), probeInfoALL.lv$probeID)
  probeInfo.lv <- lapply(probeInfoALL.lv, function(tmp.v,ext.idx){return(tmp.v[ext.idx]);}, map.idx);
  beta.lm <- list();
  
  for (g in 1:6) {
    group.idx <- which(probeInfo.lv$GeneGroup == g)
    if(length(group.idx) == 0) next;
    tmp.m <- beta.m[group.idx, , drop=FALSE]
    rownames(tmp.m) <- probeInfo.lv$EID[group.idx];
    sel.idx <- which(!is.na(rownames(tmp.m)));
    tmp.m <- tmp.m[sel.idx, , drop=FALSE];
    if(nrow(tmp.m) == 0) next;
    nL <- length(factor(rownames(tmp.m)));
    nspg.v <- summary(factor(rownames(tmp.m)),maxsum=nL);
    beta.lm[[g]] <- rowsum(tmp.m, group=rownames(tmp.m))/nspg.v;
  }
  genes2 <- if(!is.null(beta.lm[[2]])) rownames(beta.lm[[2]]) else c()
  genes4 <- if(!is.null(beta.lm[[4]])) rownames(beta.lm[[4]]) else c()
  unqEID.v <- unique(c(genes2, genes4));
  avbeta.m <- matrix(nrow = length(unqEID.v), ncol = ncol(beta.m))
  colnames(avbeta.m) <- colnames(beta.m); rownames(avbeta.m) <- unqEID.v
  if(!is.null(beta.lm[[4]])) avbeta.m[match(rownames(beta.lm[[4]]), rownames(avbeta.m)), ] <- beta.lm[[4]]
  if(!is.null(beta.lm[[2]])) avbeta.m[match(rownames(beta.lm[[2]]), rownames(avbeta.m)), ] <- beta.lm[[2]]
  return(avbeta.m)
}

# 执行转换
raw_data <- read.csv(args$input, row.names=1, check.names=FALSE)
gene_mat <- constAvBetaTSS(as.matrix(raw_data), type=args$type)

# 保存为 RDS 格式，方便 Nextflow 传输
saveRDS(gene_mat, args$output)