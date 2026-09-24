#!/usr/bin/env python3
"""Run MethylBERT deconvolution without loading the multi-gigabyte train table."""

from __future__ import annotations

import argparse
import json
import os
import random
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader

from methylbert.data.dataset import MethylBertFinetuneDataset
from methylbert.data.vocab import MethylVocab
from methylbert.deconvolute import optimise_nll_deconvolute, purity_estimation
from methylbert.trainer import MethylBertFinetuneTrainer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--runtime-metadata", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--seed", type=int, default=20260826)
    parser.add_argument(
        "--device",
        choices=("cpu", "cuda"),
        default="cpu",
        help="Explicit inference device; CUDA never falls back to CPU.",
    )
    return parser.parse_args()


def resolve_device(requested: str) -> tuple[torch.device, bool]:
    """Resolve the requested device without implicit accelerator fallback."""
    if requested == "cpu":
        return torch.device("cpu"), False
    if not torch.cuda.is_available():
        raise RuntimeError("MethylBERT GPU requested but CUDA is unavailable")
    return torch.device("cuda:0"), True


def model_device(model: torch.nn.Module) -> str:
    try:
        return str(next(model.parameters()).device)
    except StopIteration as exc:
        raise RuntimeError("MethylBERT model has no parameters") from exc


def device_smoke(trainer: MethylBertFinetuneTrainer, loader: DataLoader) -> dict[str, object]:
    """Run one forward pass and record devices before output transfer to CPU."""
    batch = next(iter(loader), None)
    if batch is None:
        raise RuntimeError("MethylBERT input produced no batches")
    data = {key: value.to(trainer.device) for key, value in batch.items() if isinstance(value, torch.Tensor)}
    with torch.inference_mode():
        output = trainer.model.forward(
            step=0,
            input_ids=data["dna_seq"],
            token_type_ids=data["methyl_seq"],
            labels=data["dmr_label"],
            ctype_label=data["ctype_label"],
        )
    logits = output["classification_logits"]
    if not torch.isfinite(logits).all().item():
        raise RuntimeError("MethylBERT device smoke produced non-finite logits")
    return {
        "model_device": model_device(trainer.model),
        "input_tensor_device": str(data["dna_seq"].device),
        "output_tensor_device": str(logits.device),
        "output_shape": list(logits.shape),
        "dtype": str(logits.dtype).replace("torch.", ""),
    }


def load_train_params(model_dir: Path) -> dict[str, str]:
    path = model_dir / "train_param.txt"
    if not path.is_file():
        raise RuntimeError(f"MethylBERT model is missing train_param.txt: {path}")
    params: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        key, value = line.split("\t", 1)
        params[key] = value
    for key in ("n_mers", "seq_len"):
        if key not in params:
            raise RuntimeError(f"MethylBERT train_param.txt is missing {key}")
    return params


def load_runtime_metadata(path: Path) -> tuple[int, pd.Series]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "demethflow-methylbert-runtime-v1":
        raise RuntimeError(f"Unsupported MethylBERT runtime metadata: {payload.get('schema')!r}")
    num_dmrs = int(payload["num_dmrs"])
    records = payload.get("cell_types", [])
    if num_dmrs < 1 or len(records) < 2:
        raise RuntimeError("MethylBERT runtime metadata needs DMRs and at least two cell types")
    names = [record["name"] for record in records]
    counts = np.asarray([int(record["train_count"]) for record in records], dtype=float)
    if np.any(counts <= 0):
        raise RuntimeError("MethylBERT cell-type counts must be positive")
    margins = pd.Series(counts / counts.sum(), index=names, dtype=float)
    declared = np.asarray([float(record["prior"]) for record in records], dtype=float)
    if not np.allclose(margins.to_numpy(), declared, rtol=0, atol=1e-15):
        raise RuntimeError("MethylBERT declared priors do not match train counts")
    return num_dmrs, margins


def main() -> None:
    args = parse_args()
    device, with_cuda = resolve_device(args.device)
    model_dir = Path(args.model_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("METHYLBERT_NUM_WORKERS", "0")
    os.environ.setdefault("PYTHONHASHSEED", str(args.seed))
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if with_cuda:
        torch.cuda.manual_seed_all(args.seed)
    torch.use_deterministic_algorithms(True, warn_only=True)

    params = load_train_params(model_dir)
    num_dmrs, margins = load_runtime_metadata(Path(args.runtime_metadata))
    tokenizer = MethylVocab(k=int(params["n_mers"]))
    dataset = MethylBertFinetuneDataset(args.input, tokenizer, seq_len=int(params["seq_len"]))
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        num_workers=int(os.environ["METHYLBERT_NUM_WORKERS"]),
    )
    trainer = MethylBertFinetuneTrainer(
        len(tokenizer), save_path=str(output_dir / "trainer"),
        train_dataloader=loader, test_dataloader=loader,
        with_cuda=with_cuda,
        amp=False,
    )
    trainer.load(str(model_dir / "bert.model"), load_fine_tune=True, n_dmrs=num_dmrs)
    if trainer.device != device:
        raise RuntimeError(f"MethylBERT resolved device {trainer.device}, expected {device}")
    device_qc = device_smoke(trainer, loader)

    reads, logits = trainer.read_classification(data_loader=loader, tokenizer=tokenizer, logit=True)
    reads = reads.drop(columns=["ctype_label"], errors="ignore")
    reads["n_cpg"] = reads["methyl_seq"].apply(lambda value: str(value).count("0") + str(value).count("1"))
    reads["P_ctype"] = logits[:, 1]
    reads.to_csv(output_dir / "res.csv", sep="\t", header=True, index=False)
    reads["P_N"] = logits[:, 0]
    reads = reads[reads["n_cpg"] > 0]
    if reads.empty:
        raise RuntimeError("No CpG-containing reads were available for MethylBERT deconvolution")

    if len(margins) == 2:
        deconv, fisher = purity_estimation(reads, margins, n_grid=10000, adjustment=False)
        fisher.to_csv(output_dir / "FI.csv", sep="\t", header=True, index=False)
    else:
        deconv = optimise_nll_deconvolute(reads, margins)
    deconv.to_csv(output_dir / "deconvolution.csv", sep="\t", header=True, index=False)
    (output_dir / "runtime_summary.json").write_text(
        json.dumps(
            {
                "schema": "demethflow-methylbert-run-v1",
                "num_dmrs": num_dmrs,
                "cell_types": list(margins.index),
                "priors": [float(value) for value in margins],
                "seed": args.seed,
                "device_requested": args.device,
                "device_resolved": str(device),
                "cuda_available": torch.cuda.is_available(),
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
                "mixed_precision": False,
                "device_qc": device_qc,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
