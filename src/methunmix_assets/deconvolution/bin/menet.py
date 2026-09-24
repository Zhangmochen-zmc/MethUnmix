#!/usr/bin/env python3
"""Strict MEnet model inspector and runtime adapter for DeMethFlow.

MEnet's upstream predictor imputes every absent 1 kb region.  Without an
explicit pre-imputation overlap gate, an unrelated or malformed input can
therefore produce a plausible-looking result.  This adapter validates the
trusted bundled model, measures real overlap, refuses incompatible inputs and
only then performs prediction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import re
import resource
import sys
import time
import traceback
import warnings
import random
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
# The SIF home directory is read-only.  Configure these before importing the
# upstream package, which may import Matplotlib during predictor startup.
# Paths are relative to the task work directory and are shared by every MEnet
# route (native WGBS, native array, and WGBS-derived array).
os.environ["MPLCONFIGDIR"] = str(Path.cwd() / ".demethflow-matplotlib")
os.environ["XDG_CONFIG_HOME"] = str(Path.cwd() / ".demethflow-xdg-config")
os.environ["XDG_CACHE_HOME"] = str(Path.cwd() / ".demethflow-xdg-cache")
from MEnet import _version, models, utils
from menet_native_tiling import (
    NATIVE_WGBS_TILING_STRATEGY,
    aggregate_model_region_values,
)


REGION_RE = re.compile(r"^([^:]+):(\d+)-(\d+)$")


class OverlapError(RuntimeError):
    def __init__(self, message: str, qc: dict):
        super().__init__(message)
        self.qc = qc


def configure_determinism(seed: int, device: torch.device) -> None:
    """Configure reproducible inference without enabling mixed precision."""
    os.environ.setdefault("PYTHONHASHSEED", str(seed))
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(True, warn_only=True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def configure_cpu_budget(device: torch.device) -> dict:
    """Bind PyTorch CPU pools to the Nextflow task.cpus budget when provided."""
    raw = os.environ.get("METHUNMIX_TASK_CPUS")
    if device.type != "cpu" or raw is None:
        return {
            "cpu_budget_source": "NOT_APPLICABLE" if device.type != "cpu" else "UNDECLARED",
            "torch_num_threads": int(torch.get_num_threads()),
            "torch_num_interop_threads": int(torch.get_num_interop_threads()),
        }
    try:
        budget = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"METHUNMIX_TASK_CPUS must be a positive integer, found {raw!r}") from exc
    if budget < 1:
        raise RuntimeError(f"METHUNMIX_TASK_CPUS must be positive, found {budget}")
    torch.set_num_threads(budget)
    # Keep inter-op scheduling serial so it cannot multiply the intra-op pool.
    torch.set_num_interop_threads(1)
    return {
        "cpu_budget_source": "METHUNMIX_TASK_CPUS",
        "declared_task_cpus": budget,
        "torch_num_threads": int(torch.get_num_threads()),
        "torch_num_interop_threads": int(torch.get_num_interop_threads()),
    }


def resolve_device(requested: str) -> tuple[torch.device, dict]:
    """Resolve a logical device and return auditable runtime metadata.

    The public workflow passes only ``cpu`` or ``cuda``.  ``cuda:N`` remains
    useful for diagnosis and obeys CUDA_VISIBLE_DEVICES inside the container.
    """
    value = str(requested or "cpu").strip().lower()
    if value == "cpu":
        return torch.device("cpu"), {
            "accelerator_requested": "cpu", "device_requested": "cpu",
            "device_resolved": "cpu", "dtype": "float32", "mixed_precision": False,
        }
    if value == "cuda":
        device = torch.device("cuda")
    elif re.fullmatch(r"cuda:[0-9]+", value):
        device = torch.device(value)
    else:
        raise RuntimeError(f"unsupported MEnet device {requested!r}; use cpu or cuda")
    if not torch.cuda.is_available():
        raise RuntimeError(
            "MEnet GPU requested but CUDA is unavailable in the selected runtime; "
            "GPU execution will not fall back to CPU"
        )
    if device.index is not None and device.index >= torch.cuda.device_count():
        raise RuntimeError(
            f"requested logical GPU {device.index} is unavailable; "
            f"CUDA device count is {torch.cuda.device_count()}"
        )
    index = torch.cuda.current_device() if device.index is None else device.index
    resolved = torch.device(f"cuda:{index}")
    return resolved, {
        "accelerator_requested": "gpu", "device_requested": value,
        "device_resolved": str(resolved), "cuda_available": True,
        "cuda_device_count": int(torch.cuda.device_count()),
        "cuda_device_name": torch.cuda.get_device_name(index),
        "torch_version": str(torch.__version__),
        "cuda_runtime": str(torch.version.cuda), "dtype": "float32",
        "mixed_precision": False,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def string_sequence_sha256(values: list[str], *, sort_values: bool = False) -> str:
    """Hash a string sequence with an unambiguous canonical JSON encoding."""
    normalized = list(map(str, values))
    if sort_values:
        normalized.sort()
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def json_compatible(value: object) -> object:
    """Convert nested NumPy scalars while preserving architecture semantics."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (list, tuple)):
        return [json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_compatible(item) for key, item in value.items()}
    return value


def normalize(value: object) -> str:
    return " ".join(str(value).strip().lower().replace("_", " ").split())


def load_expected(path: Path | None) -> list[dict]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("expected cell-type sidecar must be a non-empty JSON list")
    return payload


def expected_label_mapping(observed: list[str], expected: list[dict]) -> dict[str, str]:
    if not expected:
        return {label: label for label in observed}
    aliases: dict[str, str] = {}
    for cell in expected:
        cell_id = str(cell["cell_type_id"])
        for label in (cell_id, cell["display_name"], *cell.get("synonyms", [])):
            key = normalize(label)
            if key in aliases and aliases[key] != cell_id:
                raise RuntimeError(f"ambiguous manifest cell-type alias: {label!r}")
            aliases[key] = cell_id
    # Historical MEnet assets use these two spellings interchangeably.
    if "epithelial" in {str(cell["cell_type_id"]) for cell in expected}:
        aliases.setdefault("epithelium", "epithelial")
    mapping: dict[str, str] = {}
    for label in observed:
        key = normalize(label)
        if key not in aliases:
            raise RuntimeError(f"MEnet label is absent from the manifest: {label!r}")
        mapping[label] = aliases[key]
    mapped = list(mapping.values())
    expected_ids = [str(cell["cell_type_id"]) for cell in expected]
    if len(mapped) != len(set(mapped)):
        raise RuntimeError(f"MEnet labels do not map one-to-one: {mapping}")
    if set(mapped) != set(expected_ids):
        raise RuntimeError(
            f"MEnet labels do not cover the manifest: observed={mapped}, expected={expected_ids}"
        )
    return mapping


def load_and_inspect(model_path: Path, expected: list[dict], genome_build: str) -> tuple[list, dict]:
    captured: list[str] = []
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        with model_path.open("rb") as handle:
            params = pickle.load(handle)
        captured = [str(item.message) for item in emitted]
    if not isinstance(params, (list, tuple)) or len(params) != 6:
        raise RuntimeError("MEnet model must be the documented six-element sequence")
    architecture, states, regions, minor_labels, imputers, category = params
    if not isinstance(architecture, (list, tuple)) or len(architecture) < 2:
        raise RuntimeError("invalid MEnet architecture declaration")
    if not isinstance(states, (list, tuple)) or not states:
        raise RuntimeError("MEnet model contains no fitted network states")
    if not isinstance(imputers, (list, tuple)) or len(imputers) != len(states):
        raise RuntimeError("MEnet network-state and imputer counts differ")
    if not isinstance(regions, (list, tuple)) or not regions or len(regions) != len(set(regions)):
        raise RuntimeError("MEnet regions must be a non-empty unique sequence")
    widths: set[int] = set()
    for region in regions:
        matched = REGION_RE.fullmatch(str(region))
        if not matched:
            raise RuntimeError(f"invalid MEnet region: {region!r}")
        start, end = int(matched.group(2)), int(matched.group(3))
        if start < 0 or end <= start:
            raise RuntimeError(f"invalid MEnet region interval: {region!r}")
        widths.add(end - start)
    if widths != {1000}:
        raise RuntimeError(f"MEnet release models must use 1000 bp regions; observed {sorted(widths)}")
    if int(architecture[0]) != len(regions):
        raise RuntimeError(
            f"architecture input width {architecture[0]} differs from {len(regions)} regions"
        )
    if not isinstance(minor_labels, (list, tuple)) or not minor_labels:
        raise RuntimeError("MEnet model contains no cell labels")
    if len(minor_labels) != len(set(map(str, minor_labels))):
        raise RuntimeError("MEnet minor labels are not unique")
    if not isinstance(category, pd.DataFrame) or not {"MinorGroup", "Tissue"} <= set(category.columns):
        raise RuntimeError("MEnet category table must contain MinorGroup and Tissue")
    category = category.loc[:, ["Tissue", "MinorGroup"]].copy()
    if category.isna().any().any() or category["MinorGroup"].duplicated().any():
        raise RuntimeError("MEnet category table contains missing or duplicate minor labels")
    if set(map(str, category["MinorGroup"])) != set(map(str, minor_labels)):
        raise RuntimeError("MEnet category table does not cover its minor labels")
    major_labels = list(dict.fromkeys(map(str, category["Tissue"])))
    mapping = expected_label_mapping(major_labels, expected)
    model = models.MEnet(*architecture)
    for index, state in enumerate(states):
        try:
            model.load_state_dict(state, strict=True)
        except Exception as exc:
            raise RuntimeError(f"network state {index + 1} cannot be loaded: {exc}") from exc
    for index, imputer in enumerate(imputers):
        statistics = np.asarray(getattr(imputer, "statistics_", []), dtype=float)
        if statistics.shape != (len(regions),):
            raise RuntimeError(
                f"imputer {index + 1} width {statistics.shape} differs from {len(regions)} regions"
            )
        if not np.isfinite(statistics).all():
            raise RuntimeError(f"imputer {index + 1} contains non-finite statistics")
    qc = {
        "schema": "demethflow-menet-model-qc-v1",
        "status": "PASS",
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "model_sha256": sha256(model_path),
        "model_size_bytes": model_path.stat().st_size,
        "menet_version": str(_version.__version__),
        "python_version": sys.version.split()[0],
        "torch_version": str(torch.__version__),
        "genome_build": genome_build,
        "region_count": len(regions),
        "region_width_bp": 1000,
        "region_order_sha256": string_sequence_sha256(list(map(str, regions))),
        "region_set_sha256": string_sequence_sha256(list(map(str, regions)), sort_values=True),
        "architecture": json_compatible(architecture),
        "fold_count": len(states),
        "minor_group_count": len(minor_labels),
        "major_group_count": len(major_labels),
        "minor_group_labels": list(map(str, minor_labels)),
        "major_group_labels": major_labels,
        "manifest_label_mapping": mapping,
        "load_warnings": captured,
    }
    return list(params), qc


def load_tiled_input(input_path: Path, input_type: str, regions: list[str], output_dir: Path) -> pd.DataFrame:
    tile_bp = int(REGION_RE.fullmatch(str(regions[0])).group(3)) - int(
        REGION_RE.fullmatch(str(regions[0])).group(2)
    )
    delimiter = utils.detect_delim(str(input_path))
    if input_type == "array":
        frame = utils.tile_array(str(input_path), delimiter, tile_bp)
    elif input_type == "bismark":
        frame, preprocessing_qc = aggregate_model_region_values(
            input_path, regions, value_column=3, value_name="methylated_frequency"
        )
        frame.attrs["menet_preprocessing_qc"] = preprocessing_qc
    else:
        raise RuntimeError(f"unsupported MEnet input type: {input_type}")
    if isinstance(frame, pd.Series):
        frame = frame.to_frame()
    if not isinstance(frame, pd.DataFrame) or frame.shape[1] == 0:
        raise RuntimeError("MEnet preprocessing produced no samples")
    frame.index = frame.index.astype(str)
    frame.columns = frame.columns.astype(str)
    if frame.index.duplicated().any() or frame.columns.duplicated().any():
        raise RuntimeError("MEnet preprocessing produced duplicate regions or sample IDs")
    # pandas transformations are not required to preserve ``attrs`` across
    # versions.  Keep the audited native-WGBS preprocessing record explicitly.
    preprocessing_qc = dict(frame.attrs.get("menet_preprocessing_qc", {}))
    frame = frame.apply(pd.to_numeric, errors="coerce")
    if preprocessing_qc:
        frame.attrs["menet_preprocessing_qc"] = preprocessing_qc
    return frame


def device_smoke(params: list, device: torch.device) -> dict:
    """Move a validated model to *device* and execute one FP32 inference."""
    architecture, states, _, _, _, _ = params
    model = models.MEnet(*architecture)
    model.load_state_dict(states[0], strict=True)
    model = model.to(device)
    model.eval()
    dummy = torch.zeros((1, int(architecture[0])), dtype=torch.float32, device=device)
    with torch.inference_mode():
        values = F.softmax(model(dummy), dim=1)
    if values.device != device or not torch.isfinite(values).all():
        raise RuntimeError("MEnet device smoke inference returned an invalid tensor")
    return {
        "model_on_device": all(parameter.device == device for parameter in model.parameters()),
        "input_tensor_device": str(dummy.device),
        "output_tensor_device": str(values.device),
        "output_shape": list(values.shape),
    }


def overlap_qc(frame: pd.DataFrame, regions: list[str], minimum_regions: int, minimum_fraction: float) -> dict:
    model_regions = set(map(str, regions))
    present = frame.index.isin(model_regions)
    candidate = frame.loc[present]
    finite = np.isfinite(candidate.to_numpy(float)).any(axis=1) if not candidate.empty else np.array([], dtype=bool)
    observed_regions = int(finite.sum())
    fraction = observed_regions / len(regions)
    required = max(int(minimum_regions), int(math.ceil(len(regions) * minimum_fraction)))
    status = "PASS" if observed_regions >= required else "FAIL"
    return {
        "status": status,
        "observed_overlap_regions": observed_regions,
        "model_region_count": len(regions),
        "overlap_fraction": fraction,
        "minimum_overlap_regions": int(minimum_regions),
        "minimum_overlap_fraction": float(minimum_fraction),
        "effective_minimum_overlap_regions": required,
        "sample_count": int(frame.shape[1]),
        "sample_ids": list(map(str, frame.columns)),
    }


def native_bed_overlap_qc(
    path: Path, regions: list[str], minimum_regions: int, minimum_fraction: float,
    genome_build: str,
) -> dict:
    frame, preprocessing_qc = aggregate_model_region_values(
        path, regions, value_column=5, value_name="beta"
    )
    count = int(frame.shape[0])
    fraction = count / len(regions)
    required = max(int(minimum_regions), int(math.ceil(len(regions) * minimum_fraction)))
    return {
        "status": "PASS" if count >= required else "FAIL",
        "observed_overlap_regions": count,
        "model_region_count": len(regions),
        "overlap_fraction": fraction,
        "minimum_overlap_regions": int(minimum_regions),
        "minimum_overlap_fraction": float(minimum_fraction),
        "effective_minimum_overlap_regions": required,
        "sample_count": 1,
        "sample_ids": [path.stem],
        "genome_build": genome_build,
        "tiling_strategy": NATIVE_WGBS_TILING_STRATEGY,
        "raw_input_overlap_regions": preprocessing_qc["raw_input_overlap_regions"],
        "actual_model_input_overlap_regions": preprocessing_qc["actual_model_input_overlap_regions"],
        "imputed_region_count": preprocessing_qc["imputed_region_count"],
        "imputed_region_fraction": preprocessing_qc["imputed_region_fraction"],
        "preprocessing_qc": preprocessing_qc,
    }


def predict(
    params: list, frame: pd.DataFrame, model_qc: dict, output_dir: Path, prefix: str,
    minimum_regions: int, minimum_fraction: float, device: torch.device,
    device_metadata: dict, seed: int,
) -> dict:
    architecture, states, regions, minor_labels, imputers, category = params
    run_qc = overlap_qc(frame, list(map(str, regions)), minimum_regions, minimum_fraction)
    run_qc.update({
        "schema": "demethflow-menet-run-qc-v1",
        "model_sha256": model_qc["model_sha256"],
        "input_type": None,
    })
    run_qc.update(device_metadata)
    run_qc["random_seed"] = int(seed)
    run_qc["deterministic_algorithms"] = True
    preprocessing_qc = frame.attrs.get("menet_preprocessing_qc", {})
    run_qc["genome_build"] = model_qc["genome_build"]
    run_qc["tiling_strategy"] = preprocessing_qc.get("tiling_strategy")
    run_qc["raw_input_overlap_regions"] = int(
        preprocessing_qc.get("raw_input_overlap_regions", run_qc["observed_overlap_regions"])
    )
    run_qc["actual_model_input_overlap_regions"] = int(run_qc["observed_overlap_regions"])
    run_qc["imputed_region_count"] = len(regions) - int(run_qc["observed_overlap_regions"])
    run_qc["imputed_region_fraction"] = run_qc["imputed_region_count"] / len(regions)
    run_qc["preprocessing_qc"] = preprocessing_qc
    if run_qc["status"] != "PASS":
        raise OverlapError(
            "MEnet input/model overlap is below the frozen safety threshold: "
            f"{run_qc['observed_overlap_regions']} < {run_qc['effective_minimum_overlap_regions']} "
            f"({run_qc['overlap_fraction']:.4f})", run_qc,
        )
    aligned = frame.reindex(list(map(str, regions)))
    matrix = aligned.to_numpy(float).T
    accumulated = np.zeros((len(minor_labels), frame.shape[1]), dtype=float)
    for state, imputer in zip(states, imputers):
        transformed = imputer.transform(matrix)
        if not np.isfinite(transformed).all():
            raise RuntimeError("MEnet imputation produced non-finite values")
        model = models.MEnet(*architecture)
        model.load_state_dict(state, strict=True)
        model = model.to(device)
        model.eval()
        tensor = torch.as_tensor(transformed, dtype=torch.float32, device=device)
        with torch.inference_mode():
            values = F.softmax(model(tensor), dim=1)
        if values.device != device:
            raise RuntimeError(
                f"MEnet output tensor is on {values.device}, expected {device}"
            )
        run_qc["model_on_device"] = True
        run_qc["input_tensor_device"] = str(tensor.device)
        run_qc["output_tensor_device"] = str(values.device)
        accumulated += values.detach().cpu().numpy().T
    minor = pd.DataFrame(
        accumulated / len(states), index=list(map(str, minor_labels)), columns=frame.columns
    )
    minor = minor.div(minor.sum(axis=0), axis=1)
    category = category.loc[:, ["Tissue", "MinorGroup"]].copy()
    category["MinorGroup"] = category["MinorGroup"].astype(str)
    merged = minor.copy()
    merged["MinorGroup"] = merged.index
    major = pd.merge(merged, category, on="MinorGroup", how="left", validate="one_to_one")
    if major["Tissue"].isna().any():
        raise RuntimeError("MEnet category mapping left minor labels without a major group")
    order = list(dict.fromkeys(map(str, category["Tissue"])))
    major = major.groupby("Tissue", sort=False)[list(frame.columns)].sum().loc[order]
    values = major.to_numpy(float)
    if not np.isfinite(values).all() or (values < -1e-10).any():
        raise RuntimeError("MEnet output contains non-finite or negative values")
    sums = values.sum(axis=0)
    if not np.allclose(sums, 1.0, rtol=1e-5, atol=1e-7):
        raise RuntimeError(f"MEnet output columns are not normalized: {sums.tolist()}")
    output_dir.mkdir(parents=True, exist_ok=True)
    minor.to_csv(output_dir / f"{prefix}cell_proportion_MinorGroup.csv")
    major.to_csv(output_dir / f"{prefix}cell_proportion_MajorGroup.csv")
    return run_qc


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def inspect_command(args: argparse.Namespace) -> int:
    expected = load_expected(args.cell_types)
    params, qc = load_and_inspect(args.model, expected, args.genome_build)
    if args.device:
        device, device_metadata = resolve_device(args.device)
        configure_determinism(args.seed, device)
        qc.update(device_metadata)
        qc.update(device_smoke(params, device))
    if args.output:
        atomic_json(args.output, qc)
    print(json.dumps(qc, ensure_ascii=False))
    return 0


def inspect_input_command(args: argparse.Namespace) -> int:
    expected = load_expected(args.cell_types)
    params, model_qc = load_and_inspect(args.model, expected, args.genome_build)
    records = []
    for path in args.input:
        if args.input_type == "native-bed":
            qc = native_bed_overlap_qc(
                path, list(map(str, params[2])), args.min_overlap_regions,
                args.min_overlap_fraction, args.genome_build,
            )
        else:
            frame = load_tiled_input(path, "array", list(map(str, params[2])), Path("."))
            qc = overlap_qc(
                frame, list(map(str, params[2])), args.min_overlap_regions, args.min_overlap_fraction
            )
        qc["input_path"] = str(path)
        qc["input_type"] = args.input_type
        records.append(qc)
    report = {
        "schema": "demethflow-menet-input-preflight-v1",
        "status": "PASS" if records and all(item["status"] == "PASS" for item in records) else "FAIL",
        "model_qc": model_qc,
        "inputs": records,
    }
    if args.output:
        atomic_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


def predict_command(args: argparse.Namespace) -> int:
    output_dir = Path(f"results_{args.sample_id}") if args.output_dir is None else args.output_dir
    benchmark_path = Path(f"{args.sample_id}_benchmark.csv")
    run_qc_path = Path(f"{args.sample_id}_menet_run_qc.json")
    started = time.perf_counter()
    status, error = "Success", "None"
    try:
        device, device_metadata = resolve_device(args.device)
        device_metadata.update(configure_cpu_budget(device))
        configure_determinism(args.seed, device)
        expected = load_expected(args.cell_types)
        params, model_qc = load_and_inspect(args.model, expected, args.genome_build)
        frame = load_tiled_input(args.mix, args.input_type, list(map(str, params[2])), output_dir)
        run_qc = predict(
            params, frame, model_qc, output_dir, args.output_prefix,
            args.min_overlap_regions, args.min_overlap_fraction,
            device, device_metadata, args.seed,
        )
        run_qc["input_type"] = args.input_type
        run_qc["input_path"] = str(args.mix)
        run_qc["model_qc"] = model_qc
        atomic_json(run_qc_path, run_qc)
    except Exception as exc:
        status = "Failed"
        error = str(exc)
        failure = {
            "schema": "demethflow-menet-run-qc-v1", "status": "FAIL",
            "input_path": str(args.mix), "input_type": args.input_type,
            "error": error, "traceback": traceback.format_exc(),
        }
        if getattr(args, "device", "cpu"):
            failure["device_requested"] = args.device
            failure["accelerator_requested"] = "gpu" if str(args.device).startswith("cuda") else "cpu"
        if isinstance(exc, OverlapError):
            failure.update(exc.qc)
            failure["status"] = "FAIL"
        atomic_json(run_qc_path, failure)
    elapsed = time.perf_counter() - started
    peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    pd.DataFrame([{
        "Sample": args.sample_id,
        "Time_Seconds": round(elapsed, 4),
        "Peak_Memory_MB": round(float(peak_kib) / 1024.0, 4),
        "Status": status,
        "Error": error,
    }]).to_csv(benchmark_path, index=False)
    if status != "Success":
        print(f"[MEnet] {error}", file=sys.stderr)
        return 1
    target = output_dir / f"{args.output_prefix}cell_proportion_MajorGroup.csv"
    if not target.is_file() or target.stat().st_size == 0:
        print(f"[MEnet] required output is missing: {target}", file=sys.stderr)
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Strict DeMethFlow MEnet adapter")
    sub = root.add_subparsers(dest="command")
    inspect = sub.add_parser("inspect-model")
    inspect.add_argument("--model", type=Path, required=True)
    inspect.add_argument("--cell-types", type=Path)
    inspect.add_argument("--genome-build", default="hg38")
    inspect.add_argument("--device", default=None)
    inspect.add_argument("--seed", type=int, default=20260826)
    inspect.add_argument("--output", type=Path)
    inspect.set_defaults(handler=inspect_command)
    inspect_input = sub.add_parser("inspect-input")
    inspect_input.add_argument("--model", type=Path, required=True)
    inspect_input.add_argument("--cell-types", type=Path, required=True)
    inspect_input.add_argument("--genome-build", default="hg38")
    inspect_input.add_argument("--input", type=Path, action="append", required=True)
    inspect_input.add_argument("--input-type", choices=("array", "native-bed"), required=True)
    inspect_input.add_argument("--min-overlap-regions", type=int, default=100)
    inspect_input.add_argument("--min-overlap-fraction", type=float, default=0.05)
    inspect_input.add_argument("--output", type=Path)
    inspect_input.set_defaults(handler=inspect_input_command)
    run = sub.add_parser("predict")
    run.add_argument("--mix", type=Path, required=True)
    run.add_argument("--model", type=Path, required=True)
    run.add_argument("--cell-types", type=Path, required=True)
    run.add_argument("--sample-id", "--sample_id", dest="sample_id", required=True)
    run.add_argument("--input-type", choices=("array", "bismark"), default="array")
    run.add_argument("--genome-build", default="hg38")
    run.add_argument("--device", default="cpu")
    run.add_argument("--seed", type=int, default=20260826)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--output-prefix", default="")
    run.add_argument("--min-overlap-regions", type=int, default=100)
    run.add_argument("--min-overlap-fraction", type=float, default=0.05)
    run.set_defaults(handler=predict_command)
    return root


def main() -> int:
    args = parser().parse_args()
    if not getattr(args, "handler", None):
        # Backward compatible with the old array-only CLI.
        print("A subcommand is required (predict or inspect-model)", file=sys.stderr)
        return 2
    if getattr(args, "min_overlap_regions", 1) < 1:
        raise SystemExit("--min-overlap-regions must be positive")
    fraction = getattr(args, "min_overlap_fraction", 0.05)
    if not 0 < fraction <= 1:
        raise SystemExit("--min-overlap-fraction must be in (0,1]")
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
