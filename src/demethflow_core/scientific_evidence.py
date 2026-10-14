"""Local, immutable-by-append scientific evidence registry.

The registry stores factual evidence and later review decisions separately.  It
is intentionally a file-backed local mechanism, not an authority that changes
READY/RU, publishes a reference, or authenticates a reviewer identity.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from .errors import DeMethFlowError
from .asset_manager import load_catalog
from .util import atomic_write_json, strict_json_loads

SCHEMA = "methunmix-scientific-evidence-registry-v1"
RECORD_SCHEMA_V1 = "methunmix-scientific-evidence-v1"
EVIDENCE_TYPES = {
    "TECHNICAL_SMOKE", "OUTPUT_STRUCTURE", "FIXED_SIMULATED_TRUTH",
    "INDEPENDENT_SOURCE", "REAL_INDEPENDENT_COHORT", "REPEATABILITY", "VERSION_REGRESSION",
}
REVIEW_DECISIONS = {"ACCEPTED", "REJECTED", "NEEDS_INFORMATION", "REVOKED", "SUPERSEDED"}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SOURCE_ARTIFACT_KIND = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
DEFAULT_MAX_SOURCE_BYTES = 64 * 1024 * 1024


def _identity(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: {field} must be a non-empty string")
    return value.strip()


def _optional_digest(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA.fullmatch(value.lower()):
        raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: {field} must be a SHA256 or null")
    return value.lower()


def _safe_source_locator(value: object, field: str) -> str:
    """Return a portable evidence-root-relative locator or reject it.

    A source artifact is deliberately a file under an operator-selected root,
    not an arbitrary host pathname and never an executable command.  Do not
    reuse this parser for the legacy free-form ``source`` object below: that
    object predates source verification and is intentionally not interpreted.
    """
    if not isinstance(value, str) or not value.strip():
        raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: {field} must be a non-empty relative locator")
    if value != value.strip() or "\\" in value:
        raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: {field} must be a portable relative locator")
    locator = PurePosixPath(value)
    parts = value.split("/")
    if locator.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: {field} must be a safe relative locator")
    return locator.as_posix()


def _validate_source_artifacts(value: object) -> list[dict[str, object]] | None:
    """Validate the opt-in, typed source-artifact contract.

    ``None`` means that an older factual record has no portable artifact
    declaration.  It remains queryable and structurally auditable, but an
    explicit source verification reports it as unverified rather than passing
    it by implication.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise DeMethFlowError("EVIDENCE_SCHEMA_INVALID: source_artifacts must be a non-empty list when declared")
    artifacts: list[dict[str, object]] = []
    artifact_ids: set[str] = set()
    for index, raw in enumerate(value):
        prefix = f"source_artifacts[{index}]"
        if not isinstance(raw, dict):
            raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: {prefix} must be an object")
        artifact_id = raw.get("artifact_id")
        kind = raw.get("kind")
        if not isinstance(artifact_id, str) or not _SOURCE_ARTIFACT_ID.fullmatch(artifact_id):
            raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: {prefix}.artifact_id is invalid")
        if artifact_id in artifact_ids:
            raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: duplicate source artifact_id {artifact_id!r}")
        if not isinstance(kind, str) or not _SOURCE_ARTIFACT_KIND.fullmatch(kind):
            raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: {prefix}.kind is invalid")
        digest = _optional_digest(raw.get("sha256"), f"{prefix}.sha256")
        size = raw.get("size_bytes")
        if digest is None or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: {prefix} requires SHA256 and non-negative size_bytes")
        artifact_ids.add(artifact_id)
        artifacts.append({
            "artifact_id": artifact_id,
            "kind": kind,
            "locator": _safe_source_locator(raw.get("locator"), f"{prefix}.locator"),
            "sha256": digest,
            "size_bytes": size,
        })
    return artifacts


def empty_registry(registry_id: str = "local-development") -> dict[str, object]:
    return {"schema": SCHEMA, "registry_id": registry_id, "records": [], "reviews": [],
            "scope": "local evidence registry; imports and reviews never promote scientific status"}


def _validate_record(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise DeMethFlowError("EVIDENCE_SCHEMA_INVALID: record must be an object")
    record = dict(raw)
    record["evidence_id"] = _text(record.get("evidence_id"), "evidence_id")
    record["schema_version"] = _text(record.get("schema_version"), "schema_version")
    # ``1`` is retained solely for a small pre-WP-08 local fixture format.
    # New records must use the named contract so an arbitrary future schema is
    # never silently interpreted as this one.
    if record["schema_version"] not in {"1", RECORD_SCHEMA_V1}:
        raise DeMethFlowError(
            f"EVIDENCE_SCHEMA_INVALID: unsupported record schema {record['schema_version']!r}"
        )
    record["evidence_type"] = _text(record.get("evidence_type"), "evidence_type")
    if record["evidence_type"] not in EVIDENCE_TYPES:
        raise DeMethFlowError(f"EVIDENCE_SCHEMA_INVALID: unsupported evidence_type {record['evidence_type']}")
    validation = record.get("validation_object")
    if not isinstance(validation, dict):
        raise DeMethFlowError("EVIDENCE_SCHEMA_INVALID: validation_object must be an object")
    record["validation_object"] = {
        "reference_selector": validation.get("reference_selector"),
        "reference_content_digest": _optional_digest(validation.get("reference_content_digest"), "validation_object.reference_content_digest"),
        "tool": validation.get("tool"), "adapter": validation.get("adapter"), "profile": validation.get("profile"),
        "scenario": validation.get("scenario"), "platform": validation.get("platform"),
        "genome_build": validation.get("genome_build"), "analysis_contract": validation.get("analysis_contract"),
    }
    for field in ("input_identity", "truth_identity", "software_identity", "runtime_identity", "model_identity", "parameters", "seed", "sample_axis", "output_axis", "metrics", "acceptance_policy", "limitations", "source"):
        record.setdefault(field, None)
    relationships = record.get("relationships", [])
    if not isinstance(relationships, list):
        raise DeMethFlowError("EVIDENCE_SCHEMA_INVALID: relationships must be a list")
    normalized_relationships = []
    for item in relationships:
        if not isinstance(item, dict) or item.get("relation") not in {"SUPPLEMENTS", "REPLACES", "WITHDRAWS"}:
            raise DeMethFlowError("EVIDENCE_SCHEMA_INVALID: invalid evidence relationship")
        normalized_relationships.append({"relation": item["relation"], "target_evidence_id": _text(item.get("target_evidence_id"), "relationships.target_evidence_id")})
    record["relationships"] = normalized_relationships
    record["source"] = record["source"] if isinstance(record["source"], dict) else {"status": "MISSING_OR_UNSTRUCTURED"}
    record["source_artifacts"] = _validate_source_artifacts(record.get("source_artifacts"))
    record["raw_record_identity"] = _identity({key: value for key, value in record.items() if key != "raw_record_identity"})
    record["review_status"] = "IMPORTED_UNREVIEWED"
    record["scientific_status_promotion"] = "PROHIBITED"
    return record


def _load(path: Path) -> dict[str, object]:
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeMethFlowError(f"EVIDENCE_REGISTRY_INVALID: cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise DeMethFlowError("EVIDENCE_REGISTRY_INVALID: unsupported registry schema")
    if not isinstance(payload.get("records"), list) or not isinstance(payload.get("reviews"), list):
        raise DeMethFlowError("EVIDENCE_REGISTRY_INVALID: records/reviews must be lists")
    return payload


def initialize(path: Path, *, registry_id: str = "local-development") -> dict[str, object]:
    path = path.expanduser().resolve()
    if path.exists():
        return _load(path)
    payload = empty_registry(registry_id)
    atomic_write_json(path, payload)
    return payload


def import_record(path: Path, raw: object) -> dict[str, object]:
    registry = _load(path)
    record = _validate_record(raw)
    existing = {str(item.get("evidence_id")): item for item in registry["records"] if isinstance(item, dict)}
    prior = existing.get(str(record["evidence_id"]))
    if prior is not None:
        if prior.get("raw_record_identity") == record["raw_record_identity"]:
            return {"status": "IDEMPOTENT", "evidence_id": record["evidence_id"], "identity": record["raw_record_identity"]}
        raise DeMethFlowError(f"EVIDENCE_ID_CONFLICT: {record['evidence_id']} has different content")
    registry["records"].append(record)
    atomic_write_json(path, registry)
    return {"status": "IMPORTED_UNREVIEWED", "evidence_id": record["evidence_id"], "identity": record["raw_record_identity"]}


def add_review(path: Path, raw: object) -> dict[str, object]:
    registry = _load(path)
    if not isinstance(raw, dict):
        raise DeMethFlowError("EVIDENCE_REVIEW_INVALID: review must be an object")
    review_id = _text(raw.get("review_id"), "review_id")
    evidence_id = _text(raw.get("evidence_id"), "evidence_id")
    decision = _text(raw.get("decision"), "decision")
    if decision not in REVIEW_DECISIONS:
        raise DeMethFlowError("EVIDENCE_REVIEW_INVALID: unsupported decision")
    records = {str(item.get("evidence_id")): item for item in registry["records"] if isinstance(item, dict)}
    if evidence_id not in records:
        raise DeMethFlowError(f"EVIDENCE_REVIEW_INVALID: unknown evidence_id {evidence_id}")
    reason = raw.get("reason")
    supersedes = raw.get("supersedes_review_id")
    if decision in {"REJECTED", "REVOKED", "SUPERSEDED"} and (not isinstance(reason, str) or not reason.strip()):
        raise DeMethFlowError("EVIDENCE_REVIEW_INVALID: rejection/revocation/supersede requires non-empty reason")
    if decision == "SUPERSEDED":
        if not isinstance(supersedes, str) or not supersedes.strip():
            raise DeMethFlowError("EVIDENCE_REVIEW_INVALID: supersede requires supersedes_review_id")
        target_review = next((item for item in registry["reviews"] if isinstance(item, dict) and item.get("review_id") == supersedes), None)
        if target_review is None:
            raise DeMethFlowError("EVIDENCE_REVIEW_INVALID: supersedes_review_id is unknown")
        if target_review.get("evidence_id") != evidence_id:
            raise DeMethFlowError("EVIDENCE_REVIEW_INVALID: supersede target must belong to the same evidence")
    elif supersedes is not None:
        raise DeMethFlowError("EVIDENCE_REVIEW_INVALID: supersedes_review_id is only valid for SUPERSEDED")
    review = {"review_id": review_id, "evidence_id": evidence_id, "decision": decision,
              "policy_identity": raw.get("policy_identity"), "reviewer_claim": raw.get("reviewer_claim"),
              "source": raw.get("source"), "reason": reason.strip() if isinstance(reason, str) else None,
              "supersedes_review_id": supersedes, "authentication": "UNVERIFIED_DECLARATION", "scientific_status_promotion": "PROHIBITED"}
    review["review_identity"] = _identity(review)
    for prior in registry["reviews"]:
        if isinstance(prior, dict) and prior.get("review_id") == review_id:
            if prior.get("review_identity") == review["review_identity"]:
                return {"status": "IDEMPOTENT", "review_id": review_id}
            raise DeMethFlowError(f"EVIDENCE_REVIEW_CONFLICT: {review_id} has different content")
    registry["reviews"].append(review)
    atomic_write_json(path, registry)
    return {"status": "RECORDED_UNAUTHENTICATED_REVIEW", "review_id": review_id, "evidence_id": evidence_id}


def query(path: Path, *, selector: str | None = None, tool: str | None = None) -> dict[str, object]:
    registry = _load(path)
    records = []
    for record in registry["records"]:
        if not isinstance(record, dict):
            continue
        obj = record.get("validation_object") if isinstance(record.get("validation_object"), dict) else {}
        if selector and obj.get("reference_selector") != selector:
            continue
        if tool and obj.get("tool") != tool:
            continue
        reviews = [item for item in registry["reviews"] if isinstance(item, dict) and item.get("evidence_id") == record.get("evidence_id")]
        records.append({"record": record, "reviews": reviews})
    return {"schema": "methunmix-scientific-evidence-query-v1", "record_count": len(records), "records": records,
            "scope": "factual evidence and unverified review declarations; no scientific-state conclusion"}


def _source_problem(code: str, evidence_id: object, artifact_id: object | None = None) -> dict[str, str]:
    result = {"code": code, "evidence_id": str(evidence_id)}
    if artifact_id is not None:
        result["artifact_id"] = str(artifact_id)
    return result


def _controlled_evidence_root(value: Path) -> Path:
    expanded = value.expanduser()
    if expanded.is_symlink() or not expanded.is_dir():
        raise DeMethFlowError("EVIDENCE_SOURCE_ROOT_INVALID: evidence root must be an existing non-symlink directory")
    return expanded.resolve()


def _verify_artifact(root: Path, descriptor: dict[str, object], *, maximum_bytes: int) -> str | None:
    """Return a source-problem code, or ``None`` after bounded hashing.

    Every path component is lstat'ed so a symlink located *inside* the chosen
    root cannot redirect the verifier outside it.  The method reads only a
    regular file and never evaluates a record field as a command.
    """
    locator = str(descriptor["locator"])
    candidate = root
    try:
        for part in locator.split("/"):
            candidate = candidate / part
            details = candidate.lstat()
            if stat.S_ISLNK(details.st_mode):
                return "SOURCE_LOCATOR_SYMLINK"
    except FileNotFoundError:
        return "SOURCE_MISSING"
    except OSError:
        return "SOURCE_UNREADABLE"
    try:
        candidate.relative_to(root)
    except ValueError:
        return "SOURCE_LOCATOR_UNSAFE"
    if not stat.S_ISREG(details.st_mode):
        return "SOURCE_NOT_REGULAR_FILE"
    if details.st_size != descriptor["size_bytes"]:
        return "SOURCE_SIZE_MISMATCH"
    if details.st_size > maximum_bytes:
        return "SOURCE_TOO_LARGE"
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return "SOURCE_UNREADABLE"
    return None if digest.hexdigest() == descriptor["sha256"] else "SOURCE_DIGEST_MISMATCH"


def audit(
    path: Path,
    *,
    catalog_path: Path | None = None,
    verify_sources: bool = False,
    evidence_root: Path | None = None,
    maximum_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
) -> dict[str, object]:
    if maximum_source_bytes < 1:
        raise DeMethFlowError("EVIDENCE_SOURCE_LIMIT_INVALID: maximum source bytes must be positive")
    if verify_sources and evidence_root is None:
        raise DeMethFlowError("EVIDENCE_SOURCE_ROOT_REQUIRED: --verify-sources requires --evidence-root")
    if not verify_sources and evidence_root is not None:
        raise DeMethFlowError("EVIDENCE_SOURCE_ROOT_UNUSED: --evidence-root requires --verify-sources")
    root = _controlled_evidence_root(evidence_root) if verify_sources and evidence_root is not None else None
    registry = _load(path)
    missing_identity = 0
    problems: list[dict[str, str]] = []
    source_problems: list[dict[str, str]] = []
    source_checked = 0
    types: dict[str, int] = {}
    known_ids = {str(item.get("evidence_id")) for item in registry["records"] if isinstance(item, dict)}
    targets: dict[str, object] = {}
    if catalog_path is not None:
        catalog = load_catalog(catalog_path)
        targets = {str(item.get("selector")): item for item in catalog.get("targets", []) if isinstance(item, dict)}
    for record in registry["records"]:
        if not isinstance(record, dict):
            continue
        kind = str(record.get("evidence_type")); types[kind] = types.get(kind, 0) + 1
        obj = record.get("validation_object") if isinstance(record.get("validation_object"), dict) else {}
        if not obj.get("reference_selector") or not obj.get("reference_content_digest"):
            missing_identity += 1
        elif targets:
            target = targets.get(str(obj["reference_selector"]))
            if not isinstance(target, dict):
                problems.append({"code": "UNKNOWN_REFERENCE_SELECTOR", "evidence_id": str(record.get("evidence_id"))})
            elif target.get("content_digest") != obj["reference_content_digest"]:
                problems.append({"code": "REFERENCE_DIGEST_MISMATCH", "evidence_id": str(record.get("evidence_id"))})
        for relation in record.get("relationships", []):
            if isinstance(relation, dict) and relation.get("target_evidence_id") not in known_ids:
                problems.append({"code": "MISSING_RELATED_EVIDENCE", "evidence_id": str(record.get("evidence_id"))})
        if verify_sources:
            descriptors = record.get("source_artifacts")
            if not isinstance(descriptors, list) or not descriptors:
                source_problems.append(_source_problem("SOURCE_ARTIFACTS_NOT_DECLARED", record.get("evidence_id")))
            else:
                for descriptor in descriptors:
                    if not isinstance(descriptor, dict):
                        source_problems.append(_source_problem("SOURCE_ARTIFACTS_INVALID", record.get("evidence_id")))
                        continue
                    source_checked += 1
                    code = _verify_artifact(root, descriptor, maximum_bytes=maximum_source_bytes)  # type: ignore[arg-type]
                    if code is not None:
                        source_problems.append(_source_problem(code, record.get("evidence_id"), descriptor.get("artifact_id")))
    for review in registry["reviews"]:
        if not isinstance(review, dict):
            problems.append({"code": "MALFORMED_REVIEW"})
            continue
        if review.get("evidence_id") not in known_ids:
            problems.append({"code": "REVIEW_UNKNOWN_EVIDENCE", "review_id": str(review.get("review_id"))})
        if review.get("decision") == "SUPERSEDED" and not any(
            isinstance(item, dict) and item.get("review_id") == review.get("supersedes_review_id")
            for item in registry["reviews"]
        ):
            problems.append({"code": "REVIEW_SUPERSEDE_TARGET_MISSING", "review_id": str(review.get("review_id"))})
    superseded_review_ids = {
        str(review.get("supersedes_review_id")) for review in registry["reviews"]
        if isinstance(review, dict) and review.get("decision") == "SUPERSEDED"
    }
    for evidence_id in known_ids:
        decisions = {
            str(review.get("decision")) for review in registry["reviews"]
            if isinstance(review, dict) and review.get("evidence_id") == evidence_id
            and review.get("decision") != "SUPERSEDED" and review.get("review_id") not in superseded_review_ids
        }
        if "ACCEPTED" in decisions and ("REJECTED" in decisions or "REVOKED" in decisions):
            problems.append({"code": "CONTRADICTORY_REVIEW_DECISIONS", "evidence_id": evidence_id})
    structural_status = "PASS_STRUCTURAL_NO_STATUS_PROMOTION" if not problems else "FAIL_STRUCTURAL_BINDING"
    source_status = (
        "NOT_REQUESTED" if not verify_sources else
        ("PASS_SOURCES_VERIFIED" if not source_problems else "FAIL_SOURCE_VERIFICATION")
    )
    status = structural_status
    if verify_sources and not problems and not source_problems:
        status = "PASS_STRUCTURAL_AND_SOURCES_VERIFIED_NO_STATUS_PROMOTION"
    elif verify_sources and source_problems:
        status = "FAIL_SOURCE_VERIFICATION" if not problems else "FAIL_STRUCTURAL_AND_SOURCE_VERIFICATION"
    return {"schema": "methunmix-scientific-evidence-audit-v1", "record_count": len(registry["records"]),
            "review_count": len(registry["reviews"]), "evidence_type_counts": types,
            "records_missing_reference_identity": missing_identity,
            "problems": problems,
            "source_verification": {
                "requested": verify_sources,
                "root_disclosed": False,
                "artifact_count_checked": source_checked,
                "maximum_source_bytes": maximum_source_bytes if verify_sources else None,
                "problems": source_problems,
                "status": source_status,
            },
            "status": status, "scientific_status_promotion": "PROHIBITED"}
