"""Strict, offline-safe BAM input support for DeMethFlow WGBS workflows.

This module deliberately owns only the *input materialisation* contract.  It
does not select a reference or a tool: after preprocessing, the existing
native-WGBS and WGBS-derived-array paths consume the same BED/PAT contracts as
before.  Keeping that boundary explicit prevents BAM input from becoming a
second, subtly different deconvolution implementation.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from .errors import DeMethFlowError
from .util import atomic_write_json, sha256_file, stable_digest


PREPROCESS_SCHEMA = "demethflow-wgbs-bam-preprocess-v1"
BED_CONTRACT = "demethflow-native-wgbs-six-column-bed-v1"
# 1.4.4 locks the corrected dictionary-native 0-based coordinate semantics,
# complete runtime provenance, explicit strict/primary-only header policy,
# post-publication temporary-BAM cleanup, and read-only reuse of a valid
# coordinate-sorted source BAM index during primary-only staging. It
# intentionally invalidates every cache produced by older preprocessing
# contracts, including the obsolete 1.0.0 position-minus-one converter.
# It also requires CSI for all task-local BAM indexes, avoiding BAI's
# representable-range failure on valid real-scale alignments. It additionally
# records the strictly validated per-thread samtools sort memory allocation.
# It additionally re-encodes primary-only records against the reduced primary
# header before the final coordinate sort.  ``samtools reheader`` alone cannot
# remap BAM binary reference IDs when _alt/_random records are interleaved with
# primary contigs in a complete GRCh38 header.
# It also declares the CSI-validated primary-only output as ``SO:coordinate``;
# retaining an unsorted source header made WGBSTools reject a correctly sorted
# staged BAM and was therefore a cache-invalidating functional defect.
PREPROCESS_VERSION = "1.4.4"
_BAM_SUFFIX = ".bam"
_SIDECAR_SUFFIXES = (".bai", ".csi", ".bam.bai", ".bam.csi")
_UNSUPPORTED_SUFFIXES = (".cram", ".bed", ".bed.gz", ".pat", ".pat.gz", ".csv", ".tsv", ".txt")
_SAFE_SAMPLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SORT_MEMORY = re.compile(r"^[1-9][0-9]*[KMG]$")


def validate_sort_memory(value: str) -> str:
    """Return a safe, explicit ``samtools sort -m`` value.

    This intentionally supports only a positive integer followed by K, M, or
    G because the value crosses the CLI, provenance, and Nextflow shell
    boundary.  The constrained form is portable and cannot alter a command.
    """
    if not isinstance(value, str) or not _SORT_MEMORY.fullmatch(value):
        raise fail(
            "BAM_PREPROCESS_FAILED",
            "--preprocess-sort-memory must be a positive integer followed by K, M, or G (for example 768M or 16G)",
        )
    return value


def fail(code: str, detail: str) -> DeMethFlowError:
    return DeMethFlowError(f"{code}: {detail}")


@dataclass(frozen=True)
class BamSample:
    sample_id: str
    path: Path
    sha256: str
    bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class WgbsToolsAssets:
    genome_build: str
    root: Path
    cpg_dictionary: Path
    cpg_dictionary_index: Path
    reverse_dictionary: Path
    reverse_dictionary_index: Path
    cpg_chrom_sizes: Path
    chrom_sizes: Path
    genome_fasta: Path
    genome_fai: Path
    reference_manifest: Path

    def evidence(self) -> dict[str, object]:
        paths = {
            "cpg_dictionary": self.cpg_dictionary,
            "cpg_dictionary_index": self.cpg_dictionary_index,
            "reverse_dictionary": self.reverse_dictionary,
            "reverse_dictionary_index": self.reverse_dictionary_index,
            "cpg_chrom_sizes": self.cpg_chrom_sizes,
            "chrom_sizes": self.chrom_sizes,
            "genome_fasta": self.genome_fasta,
            "genome_fai": self.genome_fai,
            "reference_manifest": self.reference_manifest,
        }
        return {
            "genome_build": self.genome_build,
            "root": str(self.root),
            "files": {name: {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
                      for name, path in paths.items()},
        }


def is_bam_input(path: Path | None) -> bool:
    if path is None:
        return False
    expanded = path.expanduser()
    if expanded.is_file():
        return expanded.name.lower().endswith((_BAM_SUFFIX, ".cram"))
    if expanded.is_dir():
        return any(child.is_file() and child.name.lower().endswith((_BAM_SUFFIX, ".cram")) for child in expanded.iterdir())
    return expanded.name.lower().endswith((_BAM_SUFFIX, ".cram"))


def discover_bam_samples(input_path: Path) -> list[BamSample]:
    """Accept exactly one BAM or a flat directory that contains only BAMs.

    Index sidecars are deliberately not samples.  We reject mixed directories
    before any container is started so a user cannot accidentally send a
    previously generated BED/PAT cohort through an unrelated BAM route.
    """
    path = input_path.expanduser().resolve()
    if path.is_file():
        if path.suffix.lower() == ".cram":
            raise fail("BAM_UNSUPPORTED_MODALITY", "CRAM is not accepted; provide regular bisulfite BAM")
        if not path.name.lower().endswith(_BAM_SUFFIX):
            raise fail("BAM_UNSUPPORTED_MODALITY", f"BAM input must end in .bam: {path}")
        candidates = [path]
    elif path.is_dir():
        directories = sorted(child.name for child in path.iterdir() if child.is_dir())
        if directories:
            raise fail("BAM_UNSUPPORTED_MODALITY", "BAM input directory must be flat; nested directories are not accepted: " + ", ".join(directories))
        files = sorted(child for child in path.iterdir() if child.is_file())
        if not files:
            raise fail("BAM_EMPTY", f"BAM input directory is empty: {path}")
        crams = [child.name for child in files if child.name.lower().endswith(".cram")]
        if crams:
            raise fail("BAM_UNSUPPORTED_MODALITY", "CRAM is not accepted; provide regular bisulfite BAM: " + ", ".join(crams))
        unexpected = [
            child.name for child in files
            if not child.name.lower().endswith(_BAM_SUFFIX)
            and not child.name.lower().endswith(_SIDECAR_SUFFIXES)
        ]
        if unexpected:
            if any(name.lower().endswith(_UNSUPPORTED_SUFFIXES) for name in unexpected):
                raise fail("BAM_UNSUPPORTED_MODALITY", "mixed BAM and non-BAM input is forbidden: " + ", ".join(unexpected))
            raise fail("BAM_UNSUPPORTED_MODALITY", "BAM input directory may contain only BAMs and BAI/CSI sidecars: " + ", ".join(unexpected))
        candidates = [child for child in files if child.name.lower().endswith(_BAM_SUFFIX)]
        if not candidates:
            raise fail("BAM_EMPTY", f"no .bam files found in {path}")
    else:
        raise fail("BAM_CORRUPT", f"BAM input does not exist: {path}")

    samples: list[BamSample] = []
    safe_seen: dict[str, Path] = {}
    exact_seen: set[str] = set()
    for source in candidates:
        sample_id = source.name[: -len(_BAM_SUFFIX)]
        if not sample_id or not _SAFE_SAMPLE.fullmatch(sample_id):
            raise fail("BAM_SAMPLE_ID_COLLISION", f"invalid BAM sample id after stripping .bam: {source.name!r}")
        safe = sample_id.lower()
        if sample_id in exact_seen or safe in safe_seen:
            other = safe_seen.get(safe, source)
            raise fail("BAM_SAMPLE_ID_COLLISION", f"BAM sample ids collide after normalization: {other.name}, {source.name}")
        exact_seen.add(sample_id)
        safe_seen[safe] = source
        if source.stat().st_size == 0:
            raise fail("BAM_EMPTY", f"empty BAM: {source}")
        stat = source.stat()
        samples.append(BamSample(
            sample_id=sample_id, path=source, sha256=sha256_file(source),
            bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
        ))
    return samples


def write_preprocess_samples(samples: list[BamSample], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sample_id", "bam_path", "bam_sha256"], delimiter="\t")
        writer.writeheader()
        for sample in samples:
            writer.writerow({"sample_id": sample.sample_id, "bam_path": str(sample.path), "bam_sha256": sample.sha256})
    os.replace(temporary, destination)


def load_wgbstools_assets(root: Path, genome_build: str) -> WgbsToolsAssets:
    if genome_build not in {"hg19", "hg38"}:
        raise fail("WGBSTOOLS_REFERENCE_MISSING", f"unsupported genome build: {genome_build!r}")
    resolved = root.expanduser().resolve()
    candidates = {
        "cpg_dictionary": resolved / "CpG.bed.gz",
        "cpg_dictionary_index": resolved / "CpG.bed.gz.csi",
        "reverse_dictionary": resolved / "rev.CpG.bed.gz",
        "reverse_dictionary_index": resolved / "rev.CpG.bed.gz.tbi",
        "cpg_chrom_sizes": resolved / "CpG.chrome.size",
        "chrom_sizes": resolved / "chrome.size",
        "reference_manifest": resolved / "reference_manifest.json",
    }
    # WGBSTools accepts genome.fa.gz through GenomeRefPaths.join('genome.fa').
    fasta = resolved / "genome.fa"
    if not fasta.is_file():
        fasta = resolved / "genome.fa.gz"
    fai = Path(f"{fasta}.fai")
    candidates["genome_fasta"] = fasta
    candidates["genome_fai"] = fai
    missing = [name for name, path in candidates.items() if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise fail("WGBSTOOLS_REFERENCE_MISSING", f"{genome_build} WGBSTools asset is incomplete under {resolved}: " + ", ".join(missing))
    if sha256_file(candidates["cpg_dictionary"]) != sha256_file(candidates["reverse_dictionary"]):
        raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", "CpG.bed.gz and rev.CpG.bed.gz are not the same immutable dictionary")
    _validate_reference_manifest(candidates["reference_manifest"], genome_build, candidates)
    return WgbsToolsAssets(genome_build=genome_build, root=resolved, **candidates)  # type: ignore[arg-type]


def _validate_reference_manifest(
    manifest_path: Path, genome_build: str, candidates: dict[str, Path],
) -> None:
    """Prove that the installed WGBSTools data module is immutable and complete.

    The manifest is shipped inside each offline data module.  It deliberately
    validates the exact build-specific files used by preprocessing instead of
    trusting a directory name or allowing a sibling build to substitute them.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise fail("WGBSTOOLS_REFERENCE_MISSING", f"unreadable WGBSTools reference manifest {manifest_path}: {exc}") from exc
    if manifest.get("schema") != "demethflow-wgbstools-reference-v1" or manifest.get("genome_build") != genome_build:
        raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", f"reference manifest does not declare selected build {genome_build}")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", "reference manifest has no files mapping")
    for name, path in candidates.items():
        if name == "reference_manifest":
            continue
        expected = files.get(path.name)
        if not isinstance(expected, dict):
            raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", f"reference manifest does not describe {path.name}")
        if expected.get("bytes") != path.stat().st_size:
            raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", f"reference size mismatch for {path.name}")
        recorded = expected.get("sha256")
        if not isinstance(recorded, str) or sha256_file(path) != recorded:
            raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", f"reference digest mismatch for {path.name}")


def preprocess_signature(
    samples: list[BamSample], assets: WgbsToolsAssets, threads: int, *, runtime_sha256: str | None = None,
    contig_policy: str = "strict", sort_memory: str = "768M",
) -> str:
    if not isinstance(threads, int) or isinstance(threads, bool) or threads < 1:
        raise fail("BAM_PREPROCESS_FAILED", "--preprocess-threads must be a positive integer")
    if contig_policy not in {"strict", "primary-only"}:
        raise fail("BAM_BUILD_MISMATCH", f"unsupported BAM contig policy: {contig_policy!r}")
    sort_memory = validate_sort_memory(sort_memory)
    evidence = assets.evidence()
    values = [(f"bam:{sample.sample_id}", sample.sha256) for sample in samples]
    values.extend((f"asset:{name}", str(item["sha256"])) for name, item in evidence["files"].items())
    values.extend([
        ("schema", PREPROCESS_SCHEMA),
        ("version", PREPROCESS_VERSION),
        ("genome_build", assets.genome_build),
        ("threads", str(threads)),
        ("sort_memory", sort_memory),
        ("contig_policy", contig_policy),
        ("bed_contract", BED_CONTRACT),
        ("bam2pat", "wgbstools bam2pat --no_beta"),
        ("beta", "wgbstools pat2beta --lbeta"),
        ("runtime_sha256", runtime_sha256 or "unavailable"),
    ])
    return stable_digest(values)


def read_chrom_sizes(path: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                fields = line.rstrip("\n").split("\t")
                if len(fields) != 2 or not fields[0]:
                    raise ValueError(f"line {number} is not two-column")
                size = int(fields[1])
                if size <= 0 or fields[0] in sizes:
                    raise ValueError(f"line {number} has invalid/duplicated chromosome")
                sizes[fields[0]] = size
    except (OSError, ValueError) as exc:
        raise fail("WGBSTOOLS_REFERENCE_MISSING", f"invalid chromosome sizes {path}: {exc}") from exc
    if not sizes:
        raise fail("WGBSTOOLS_REFERENCE_MISSING", f"empty chromosome sizes: {path}")
    return sizes


def validate_bam_header(
    bam: Path, chrom_sizes: Path, *, samtools: str = "samtools", contig_policy: str = "strict",
    scan_alignments: bool = True, threads: int = 1,
) -> dict[str, object]:
    """Validate a normal bisulfite BAM against an immutable primary dictionary.

    ``primary-only`` is deliberately narrow: every expected primary contig must
    occur with its exact immutable length.  It permits only *additional* header
    contigs, which are counted and later removed by :func:`stage_primary_bam`;
    it never renames contigs or performs a coordinate conversion.
    """
    if contig_policy not in {"strict", "primary-only"}:
        raise fail("BAM_BUILD_MISMATCH", f"unsupported BAM contig policy: {contig_policy!r}")
    if not isinstance(threads, int) or isinstance(threads, bool) or threads < 1:
        raise fail("BAM_PREPROCESS_FAILED", "BAM preflight threads must be a positive integer")
    try:
        quickcheck = subprocess.run([samtools, "quickcheck", "-v", str(bam)], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise fail("BAM_PREPROCESS_FAILED", f"samtools is unavailable: {exc}") from exc
    if quickcheck.returncode != 0:
        raise fail("BAM_CORRUPT", quickcheck.stderr.strip() or quickcheck.stdout.strip() or str(bam))
    header = subprocess.run(
        [samtools, "view", "-@", str(threads), "-H", str(bam)], capture_output=True, text=True, check=False,
    )
    if header.returncode != 0:
        raise fail("BAM_CORRUPT", header.stderr.strip() or f"could not read BAM header: {bam}")
    expected = read_chrom_sizes(chrom_sizes)
    observed: dict[str, int] = {}
    platforms: set[str] = set()
    for line in header.stdout.splitlines():
        fields = line.split("\t")
        if not fields:
            continue
        if fields[0] == "@SQ":
            values = dict(field.split(":", 1) for field in fields[1:] if ":" in field)
            if "SN" not in values or "LN" not in values:
                raise fail("BAM_BUILD_MISMATCH", f"malformed @SQ header record in {bam}")
            try:
                length = int(values["LN"])
            except ValueError as exc:
                raise fail("BAM_BUILD_MISMATCH", f"non-integer @SQ length for {values['SN']}") from exc
            if values["SN"] in observed or length <= 0:
                raise fail("BAM_BUILD_MISMATCH", f"duplicated or invalid @SQ record: {values['SN']}")
            observed[values["SN"]] = length
        elif fields[0] == "@RG":
            values = dict(field.split(":", 1) for field in fields[1:] if ":" in field)
            if "PL" in values:
                platforms.add(values["PL"].strip().upper())
    if not observed:
        raise fail("BAM_CORRUPT", f"BAM header has no @SQ records: {bam}")
    unexpected = sorted(set(observed).difference(expected))
    missing = sorted(set(expected).difference(observed))
    length_mismatch = sorted(name for name in set(observed).intersection(expected) if observed[name] != expected[name])
    # The default remains exact header equality.  A complete GRCh38 BAM often
    # contains alternate/unlocalized contigs in addition to the primary 25;
    # those may be explicitly staged away only after the primary dictionary is
    # proved byte-for-byte compatible by name and length.
    if missing or length_mismatch or (unexpected and contig_policy == "strict"):
        detail = []
        if unexpected:
            detail.append("unexpected contigs=" + ",".join(unexpected[:10]))
        if missing:
            detail.append("missing contigs=" + ",".join(missing[:10]))
        if length_mismatch:
            detail.append("length mismatch=" + ",".join(length_mismatch[:10]))
        code = "BAM_CONTIG_STYLE_MISMATCH" if missing and any(not name.startswith("chr") for name in observed) else "BAM_BUILD_MISMATCH"
        raise fail(code, "; ".join(detail))
    if platforms.intersection({"ONT", "OXFORDNANOPORE", "PACBIO", "PACBIO_HIFI"}):
        raise fail("BAM_UNSUPPORTED_MODALITY", "ONT/PacBio BAM is not supported; provide regular bisulfite BAM")
    base = {
        "contig_policy": contig_policy,
        "header_contig_count": len(observed),
        "primary_header_contig_count": len(expected),
        "extra_header_contig_count": len(unexpected),
        "extra_header_contigs_preview": unexpected[:20],
        "read_group_platforms": sorted(platforms),
        "alignment_scan_performed": scan_alignments,
    }
    if not scan_alignments:
        return {
            **base,
            "alignment_count": None,
            "mapped_alignment_count": None,
            "mapped_primary_alignment_count": None,
            "mapped_non_primary_alignment_count": None,
            "paired_alignment_count": None,
            "input_layout": "not_scanned",
            "coordinate_sorted": None,
        }
    scan = subprocess.Popen(
        [samtools, "view", "-@", str(threads), str(bam)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert scan.stdout is not None
    reads = mapped_reads = primary_mapped_reads = non_primary_mapped_reads = 0
    paired_alignments = 0
    previous_coordinate: tuple[int, int] | None = None
    coordinate_sorted = True
    expected_order = {name: index for index, name in enumerate(expected)}
    try:
        for line in scan.stdout:
            reads += 1
            fields = line.rstrip("\n").split("\t")
            try:
                flag = int(fields[1])
                paired_alignments += bool(flag & 1)
                if not (flag & 4):
                    mapped_reads += 1
                    if fields[2] in expected_order:
                        primary_mapped_reads += 1
                        coordinate = (expected_order[fields[2]], int(fields[3]))
                        if previous_coordinate is not None and coordinate < previous_coordinate:
                            coordinate_sorted = False
                        previous_coordinate = coordinate
                    else:
                        non_primary_mapped_reads += 1
            except (IndexError, ValueError) as exc:
                raise fail("BAM_CORRUPT", f"malformed alignment record in {bam}") from exc
            tags = {field[:2] for field in fields[11:] if len(field) >= 3 and field[2] == ":"}
            if "MM" in tags or "ML" in tags:
                raise fail("BAM_UNSUPPORTED_MODALITY", "MM/ML-tagged BAM is not supported; provide regular bisulfite BAM")
    finally:
        scan.stdout.close()
        stderr = scan.stderr.read() if scan.stderr is not None else ""
        if scan.stderr is not None:
            scan.stderr.close()
        code = scan.wait()
    if code != 0:
        raise fail("BAM_CORRUPT", stderr.strip() or f"samtools view failed: {bam}")
    if reads == 0:
        raise fail("BAM_EMPTY", f"BAM has no alignments: {bam}")
    if contig_policy == "primary-only" and primary_mapped_reads == 0:
        raise fail("BAM_EMPTY", f"BAM has no mapped alignments on the selected primary contigs: {bam}")
    return {
        **base,
        "alignment_count": reads,
        "mapped_alignment_count": mapped_reads,
        "mapped_primary_alignment_count": primary_mapped_reads,
        "mapped_non_primary_alignment_count": non_primary_mapped_reads,
        "paired_alignment_count": paired_alignments,
        "input_layout": "paired_or_mixed" if paired_alignments else "single_end",
        "coordinate_sorted": coordinate_sorted,
    }


def stage_primary_bam(
    bam: Path, chrom_sizes: Path, output: Path, qc_output: Path, *, threads: int = 1,
    sort_memory: str = "768M", samtools: str = "samtools",
    source_preflight: dict[str, object] | None = None,
) -> dict[str, object]:
    """Copy only immutable primary-contig alignments into a task-local BAM.

    This staging route exists solely for an explicitly selected
    ``primary-only`` policy.  It leaves the source untouched, keeps coordinates
    unchanged and writes a header that contains exactly the selected primary
    dictionary.  The caller must validate the source first.
    """
    if threads < 1:
        raise fail("BAM_PREPROCESS_FAILED", "staging threads must be positive")
    sort_memory = validate_sort_memory(sort_memory)
    expected = read_chrom_sizes(chrom_sizes)
    source_qc = source_preflight or validate_bam_header(
        bam, chrom_sizes, samtools=samtools, contig_policy="primary-only",
    )
    required_preflight = {
        "contig_policy": "primary-only",
        "primary_header_contig_count": len(expected),
    }
    for key, value in required_preflight.items():
        if source_qc.get(key) != value:
            raise fail("BAM_BUILD_MISMATCH", f"primary-only staging requires a matching source preflight: {key}")
    for key in ("mapped_non_primary_alignment_count", "mapped_primary_alignment_count", "extra_header_contig_count"):
        if not isinstance(source_qc.get(key), int):
            raise fail("BAM_PREPROCESS_FAILED", f"primary-only staging preflight is incomplete: {key}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_selection = output.with_name(f".{output.name}.primary-selection.tmp.bam")
    temporary_sorted = output.with_name(f".{output.name}.source-sort.tmp.bam")
    temporary_header = output.with_name(f".{output.name}.primary-header.tmp.sam")
    temporary_output = output.with_name(f".{output.name}.tmp")
    try:
        header = subprocess.run([samtools, "view", "-H", str(bam)], capture_output=True, text=True, check=False)
        if header.returncode != 0:
            raise fail("BAM_CORRUPT", header.stderr.strip() or f"could not read BAM header: {bam}")
        retained = []
        for line in header.stdout.splitlines():
            if line.startswith("@HD") or (line.startswith("@") and not line.startswith("@SQ")):
                retained.append(line)
        # A deterministic header avoids carrying alternate-contig records into
        # the selected BAM and locks the WGBSTools dictionary order.
        # The selected alignment stream is emitted from a coordinate-sorted
        # source in immutable primary-dictionary order.  Do not retain an
        # original ``SO:unsorted`` declaration: WGBSTools treats @HD as the
        # authoritative sort contract and will skip an otherwise indexable
        # BAM.  Preserve other @HD fields while declaring the order that is
        # subsequently proved by successful CSI construction (or enforced by
        # the canonical-sort fallback below).
        source_hd = retained[0] if retained and retained[0].startswith("@HD") else "@HD\tVN:1.6"
        hd_fields = [field for field in source_hd.split("\t") if not field.startswith("SO:")]
        primary_header = ["\t".join([*hd_fields, "SO:coordinate"])]
        primary_header.extend(f"@SQ\tSN:{name}\tLN:{length}" for name, length in expected.items())
        primary_header.extend(line for line in retained if not line.startswith("@HD"))
        temporary_header.write_text("\n".join(primary_header) + "\n", encoding="utf-8")
        # Region selection is only reliable on a coordinate-sorted, indexed
        # BAM.  Reuse a valid user index when it is already present: this is
        # read-only and avoids a needless full-size temporary copy.  If either
        # condition is absent, materialise and index a task-local canonical
        # sort; the source BAM and any source sidecar remain untouched.
        selection_source = bam
        sort_strategy = "reused_validated_source_coordinate_order_and_index"
        # ``idxstats`` may return success even when an absent sidecar only
        # becomes visible during random retrieval.  Probe the exact region API
        # used below, so an unreadable/missing index deterministically takes
        # the task-local sort/index fallback instead of failing mid-stage.
        primary_probe = next(iter(expected))
        source_index = subprocess.run(
            [samtools, "view", "-c", str(bam), primary_probe], capture_output=True, text=True, check=False,
        )
        if not source_qc.get("coordinate_sorted") or source_index.returncode != 0:
            sorted_source = subprocess.run(
                [samtools, "sort", "-@", str(threads), "-m", sort_memory, "-o", str(temporary_sorted), str(bam)],
                capture_output=True, text=True, check=False,
            )
            if sorted_source.returncode != 0:
                raise fail("BAM_PREPROCESS_FAILED", sorted_source.stderr.strip() or "samtools source staging sort failed")
            # A primary-only GRCh38 BAM can still have BGZF virtual offsets
            # beyond the BAI limit.  Use CSI for every task-local staging
            # index, rather than trying BAI first and failing only on large
            # production inputs (samtools: "Numerical result out of range").
            indexed = subprocess.run(
                [samtools, "index", "-c", "-@", str(threads), str(temporary_sorted)], capture_output=True, text=True, check=False,
            )
            if indexed.returncode != 0:
                raise fail("BAM_PREPROCESS_FAILED", indexed.stderr.strip() or "samtools source staging index failed")
            selection_source = temporary_sorted
            sort_strategy = "task_local_canonical_sort_and_index"
        # Do not use ``samtools reheader`` here.  It changes the BAM header but
        # intentionally leaves binary reference IDs untouched.  In a complete
        # GRCh38 header, primary and alt/random records are commonly
        # interleaved; after dropping those records, ``chr2`` and later IDs no
        # longer refer to the same header entry and CSI creation can fail with
        # ``Numerical result out of range``.  Stream selected SAM alignment
        # records through the new primary header instead, so samtools resolves
        # RNAMEs into the reduced dictionary's IDs anew.
        selected = subprocess.Popen(
            [samtools, "view", "-@", str(threads), str(selection_source), *expected],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        reencoded = subprocess.Popen(
            [samtools, "view", "-@", str(threads), "-b", "-o", str(temporary_selection), "-"],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            if selected.stdout is None or reencoded.stdin is None:
                raise fail("BAM_PREPROCESS_FAILED", "could not create primary-only SAM re-encoding pipeline")
            with temporary_header.open("rb") as header_handle:
                shutil.copyfileobj(header_handle, reencoded.stdin)
            shutil.copyfileobj(selected.stdout, reencoded.stdin)
            reencoded.stdin.close()
            selected.stdout.close()
            selected_error = selected.stderr.read().decode("utf-8", errors="replace") if selected.stderr is not None else ""
            reencoded_error = reencoded.stderr.read().decode("utf-8", errors="replace") if reencoded.stderr is not None else ""
            selected_code = selected.wait()
            reencoded_code = reencoded.wait()
        finally:
            if selected.stdout is not None and not selected.stdout.closed:
                selected.stdout.close()
            if selected.stderr is not None and not selected.stderr.closed:
                selected.stderr.close()
            if reencoded.stdin is not None and not reencoded.stdin.closed:
                reencoded.stdin.close()
            if reencoded.stderr is not None and not reencoded.stderr.closed:
                reencoded.stderr.close()
        if selected_code != 0:
            raise fail("BAM_PREPROCESS_FAILED", selected_error.strip() or "samtools primary-contig selection failed")
        if reencoded_code != 0:
            raise fail("BAM_PREPROCESS_FAILED", reencoded_error.strip() or "samtools primary-only re-encoding failed")
        # ``samtools view sorted.bam chr1 chr2 ...`` emits one non-overlapping
        # region at a time, in the explicit immutable primary-dictionary
        # order.  Re-encoding that stream should therefore remain coordinate
        # ordered.  Do not merely assume this on a production-scale BAM:
        # build a temporary CSI, whose construction rejects out-of-order
        # coordinates.  This removes a second full 70+ GB sort for the common
        # valid case while retaining the canonical-sort fallback for every
        # unexpected ordering condition.
        selection_index = subprocess.run(
            [samtools, "index", "-c", "-@", str(threads), str(temporary_selection)],
            capture_output=True,
            text=True,
            check=False,
        )
        selection_csi = Path(f"{temporary_selection}.csi")
        if selection_index.returncode == 0:
            selection_sort_strategy = "validated_region_order_reencode_and_csi"
            os.replace(temporary_selection, output)
            # The public preprocessing worker builds the final canonical CSI
            # after staging.  Remove this validation-only sidecar so that the
            # worker never mistakes it for a published output index.
            try:
                selection_csi.unlink()
            except FileNotFoundError:
                pass
        else:
            try:
                selection_csi.unlink()
            except FileNotFoundError:
                pass
            staged_sort = subprocess.run(
                [samtools, "sort", "-@", str(threads), "-m", sort_memory, "-o", str(temporary_output), str(temporary_selection)],
                capture_output=True,
                text=True,
                check=False,
            )
            if staged_sort.returncode != 0:
                raise fail("BAM_PREPROCESS_FAILED", staged_sort.stderr.strip() or "samtools primary-only final sort failed")
            selection_sort_strategy = "canonical_samtools_sort_after_reencode"
            os.replace(temporary_output, output)
        # The source has already received the full MM/ML and alignment scan.
        # This output is produced by a new-header SAM re-encoding plus a final
        # canonical coordinate sort, so quickcheck/header verification is
        # sufficient here and avoids a second full scan of a production-scale
        # BAM.
        staged_qc = validate_bam_header(
            output, chrom_sizes, samtools=samtools, contig_policy="strict", scan_alignments=False,
        )
        payload = {
            "schema": "demethflow-bam-primary-staging-v1",
            "status": "PASS",
            "contig_policy": "primary-only",
            "source_bam": str(bam),
            "source_bam_sha256": sha256_file(bam),
            "source_preflight": source_qc,
            "staged_bam": str(output),
            "staged_bam_sha256": sha256_file(output),
            "staged_bam_bytes": output.stat().st_size,
            "source_sort_strategy": sort_strategy,
            "source_sort_memory": sort_memory,
            "source_index_reused": sort_strategy == "reused_validated_source_coordinate_order_and_index",
            "selection_sort_strategy": selection_sort_strategy,
            "selection_coordinate_order_validation": (
                "samtools_index_csi_passed" if selection_index.returncode == 0
                else "samtools_index_csi_rejected_then_canonical_sort"
            ),
            "discarded_extra_header_contig_count": source_qc["extra_header_contig_count"],
            "discarded_mapped_non_primary_alignment_count": source_qc["mapped_non_primary_alignment_count"],
            "nonprimary_exclusion_verification": "samtools region selection over exact immutable primary contigs",
            "staged_preflight": staged_qc,
        }
        atomic_write_json(qc_output, payload)
        return payload
    finally:
        for temporary in (
            temporary_selection,
            selection_csi,
            temporary_header,
            temporary_output,
            temporary_sorted,
            Path(f"{temporary_sorted}.bai"),
            Path(f"{temporary_sorted}.csi"),
        ):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def lbeta_to_bed(*, lbeta: Path, cpg_dictionary: Path, output: Path, sample_id: str, genome_build: str, qc_output: Path) -> dict[str, object]:
    """Convert uint16 methylated/depth pairs to the frozen six-column BED.

    The pinned CpG dictionary's second field is the frozen 0-based CpG start.
    This converter carries it directly into the public 0-based half-open BED
    contract. No coordinate shift is ever inferred from user data.
    """
    size = lbeta.stat().st_size
    if size == 0 or size % 4:
        raise fail("LBETA_SIZE_MISMATCH", f"{lbeta} is empty or not uint16 methylated/depth pairs")
    expected_records = size // 4
    output.parent.mkdir(parents=True, exist_ok=True)
    count = nonzero = saturated = 0
    total_depth = total_methylated = 0
    maximum_depth = 0
    minimum_beta = 1.0
    maximum_beta = 0.0
    chrom_order: dict[str, int] = {}
    previous: tuple[int, int] | None = None
    digest = hashlib.sha256()
    temporary = output.with_name(f".{output.name}.tmp")
    completed = False
    try:
        with lbeta.open("rb") as beta_handle, gzip.open(cpg_dictionary, "rt", encoding="utf-8") as dict_handle, temporary.open("w", encoding="utf-8") as bed_handle:
            for number, line in enumerate(dict_handle, start=1):
                if not line.strip():
                    continue
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 3:
                    raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", f"invalid CpG dictionary row {number}")
                chrom, position, index = fields[:3]
                try:
                    pos = int(position)
                    cpg_index = int(index)
                except ValueError as exc:
                    raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", f"invalid CpG dictionary row {number}") from exc
                if pos < 0 or cpg_index != count + 1:
                    raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", f"CpG dictionary index/order mismatch at row {number}")
                pair = beta_handle.read(4)
                if len(pair) != 4:
                    raise fail("LBETA_SIZE_MISMATCH", f"lbeta is shorter than CpG dictionary at row {number}")
                methylated, depth = struct.unpack("<HH", pair)
                count += 1
                if depth == 65535:
                    saturated += 1
                if depth == 0:
                    continue
                if methylated > depth:
                    raise fail("BED_CONTRACT_INVALID", f"methylated count exceeds depth at CpG index {cpg_index}")
                start = pos
                order = chrom_order.setdefault(chrom, len(chrom_order))
                current = (order, start)
                if previous is not None and current < previous:
                    raise fail("WGBSTOOLS_REFERENCE_DIGEST_MISMATCH", "CpG dictionary is not in reference order")
                previous = current
                beta = methylated / depth
                if not math.isfinite(beta) or beta < 0 or beta > 1:
                    raise fail("BED_CONTRACT_INVALID", f"invalid beta at CpG index {cpg_index}")
                row = f"{chrom}\t{start}\t{start + 1}\t{methylated}\t{depth}\t{beta:.12g}\n"
                bed_handle.write(row)
                digest.update(row.encode("utf-8"))
                nonzero += 1
                total_methylated += methylated
                total_depth += depth
                maximum_depth = max(maximum_depth, depth)
                minimum_beta = min(minimum_beta, beta)
                maximum_beta = max(maximum_beta, beta)
            trailing = beta_handle.read(1)
            if trailing:
                raise fail("LBETA_SIZE_MISMATCH", f"lbeta is longer than CpG dictionary: {lbeta}")
        completed = True
    except OSError as exc:
        raise fail("BAM_PREPROCESS_FAILED", f"could not materialize BED from lbeta: {exc}") from exc
    finally:
        if temporary.exists() and not completed:
            temporary.unlink(missing_ok=True)
    if count != expected_records:
        raise fail("LBETA_SIZE_MISMATCH", f"lbeta has {expected_records} records but dictionary has {count}")
    if saturated:
        temporary.unlink(missing_ok=True)
        raise fail("LBETA_SATURATION", f"{saturated} CpG depth values reached uint16 saturation (65535)")
    if nonzero == 0:
        temporary.unlink(missing_ok=True)
        raise fail("BAM_PREPROCESS_FAILED", "no covered CpGs after lbeta conversion")
    os.replace(temporary, output)
    qc = {
        "schema": PREPROCESS_SCHEMA,
        "status": "PASS",
        "sample_id": sample_id,
        "genome_build": genome_build,
        "bed_contract": BED_CONTRACT,
        "coordinate_system": "0_based_half_open",
        "cpg_dictionary": str(cpg_dictionary),
        "cpg_dictionary_sha256": sha256_file(cpg_dictionary),
        "lbeta": str(lbeta),
        "lbeta_sha256": sha256_file(lbeta),
        "lbeta_record_count": expected_records,
        "covered_cpg_count": nonzero,
        "bed_row_count": nonzero,
        "total_methylated_count": total_methylated,
        "total_depth": total_depth,
        "mean_depth": total_depth / nonzero,
        "maximum_depth": maximum_depth,
        "minimum_beta": minimum_beta,
        "maximum_beta": maximum_beta,
        "saturated_depth_count": saturated,
        "bed_sha256": digest.hexdigest(),
    }
    atomic_write_json(qc_output, qc)
    return qc


def validate_materialized_preprocess(
    root: Path, samples: list[BamSample], *, signature: str | None = None, sort_memory: str | None = None,
) -> dict[str, object]:
    """Validate published outputs before any native or projection doctor runs."""
    root = root.resolve()
    expected_ids = {sample.sample_id for sample in samples}
    observed_sets = {
        "bed": {path.name[:-4] for path in (root / "bed").glob("*.bed")},
        "pat": {path.name[:-7] for path in (root / "pat").glob("*.pat.gz")},
        "pat_index": {path.name[:-11] for path in (root / "pat").glob("*.pat.gz.csi")},
        "lbeta": {path.name[:-6] for path in (root / "beta").glob("*.lbeta")},
    }
    mismatched = {name: sorted(values) for name, values in observed_sets.items() if values != expected_ids}
    if mismatched:
        raise fail(
            "BED_PAT_SAMPLE_MISMATCH",
            "published BED/PAT/lbeta sample sets differ from input samples: " + json.dumps(mismatched, sort_keys=True),
        )
    records: list[dict[str, object]] = []
    for sample in samples:
        paths = {
            "bed": root / "bed" / f"{sample.sample_id}.bed",
            "pat": root / "pat" / f"{sample.sample_id}.pat.gz",
            "pat_index": root / "pat" / f"{sample.sample_id}.pat.gz.csi",
            "lbeta": root / "beta" / f"{sample.sample_id}.lbeta",
            "log": root / "logs" / f"{sample.sample_id}.log",
            "qc": root / "qc" / f"{sample.sample_id}.json",
            "staging": root / "qc" / f"{sample.sample_id}.staging.json",
        }
        missing = [name for name, path in paths.items() if not path.is_file() or path.stat().st_size == 0]
        if missing:
            code = "PAT_INDEX_MISSING" if "pat_index" in missing else "BAM_PREPROCESS_FAILED"
            raise fail(code, f"{sample.sample_id}: missing materialized artifacts: " + ", ".join(missing))
        try:
            qc = json.loads(paths["qc"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise fail("BAM_PREPROCESS_FAILED", f"{sample.sample_id}: unreadable QC record: {exc}") from exc
        if qc.get("status") != "PASS" or qc.get("sample_id") != sample.sample_id or qc.get("bed_contract") != BED_CONTRACT:
            raise fail("BED_CONTRACT_INVALID", f"{sample.sample_id}: QC does not prove the frozen BED contract")
        bam_qc_path = root / "qc" / f"{sample.sample_id}.bam_qc.json"
        try:
            bam_qc = json.loads(bam_qc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise fail("BAM_PREPROCESS_FAILED", f"{sample.sample_id}: BAM preflight QC is missing or unreadable: {exc}") from exc
        try:
            staging_qc = json.loads(paths["staging"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise fail("BAM_PREPROCESS_FAILED", f"{sample.sample_id}: BAM staging QC is missing or unreadable: {exc}") from exc
        if staging_qc.get("status") not in {"PASS", "NOT_APPLIED"}:
            raise fail("BAM_PREPROCESS_FAILED", f"{sample.sample_id}: BAM staging QC has invalid status")
        try:
            log_text = paths["log"].read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise fail("BAM_PREPROCESS_FAILED", f"{sample.sample_id}: preprocessing log is unreadable: {exc}") from exc
        logged = {
            key: next((line.split("=", 1)[1] for line in log_text.splitlines() if line.startswith(key + "=")), "unknown")
            for key in (
                "samtools", "bgzip", "tabix", "wgbstools_runtime_source_sha256",
                "cpg_dictionary_sha256", "bam2pat_mapq", "bam2pat_exclude_flags",
                "bam2pat_include_flags", "bam2pat_min_cpg", "source_index_sidecar",
                "sort_strategy", "samtools_sort_memory", "pat_pattern_count",
            )
        }
        if not re.fullmatch(r"[1-9][0-9]*", logged["pat_pattern_count"]):
            raise fail(
                "PAT_EMPTY",
                f"{sample.sample_id}: worker did not record a positive PAT pattern count",
            )
        if sort_memory is not None and logged["samtools_sort_memory"] != validate_sort_memory(sort_memory):
            raise fail(
                "BAM_PREPROCESS_FAILED",
                f"{sample.sample_id}: worker sort-memory provenance does not match the materialisation signature",
            )
        if sort_memory is not None and staging_qc.get("status") == "PASS" and staging_qc.get("source_sort_memory") != sort_memory:
            raise fail(
                "BAM_PREPROCESS_FAILED",
                f"{sample.sample_id}: staging QC sort-memory provenance does not match the materialisation signature",
            )
        records.append({
            "sample_id": sample.sample_id,
            "bam_path": str(sample.path),
            "bam_sha256": sample.sha256,
            "bam_bytes": sample.bytes,
            "bam_mtime_ns": sample.mtime_ns,
            "bam_mtime_utc": datetime.fromtimestamp(
                sample.mtime_ns / 1_000_000_000, tz=timezone.utc
            ).isoformat(),
            **{name: {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}
               for name, path in paths.items()},
            "qc_summary": qc,
            "bam_preflight": bam_qc,
            "bam_staging": staging_qc,
            "runtime_versions": {
                key: logged[key]
                for key in ("samtools", "bgzip", "tabix", "wgbstools_runtime_source_sha256")
            },
            "preprocess_parameters": {
                key: logged[key]
                for key in logged
                if key not in {"samtools", "bgzip", "tabix", "wgbstools_runtime_source_sha256"}
            },
        })
    return {
        "schema": PREPROCESS_SCHEMA,
        "status": "PASS",
        "signature": signature,
        "output_root": str(root),
        "samples": records,
    }
