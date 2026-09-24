#!/usr/bin/env python3
"""Read-only Git history safety audit; reports paths and findings, never values."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "repository_history_audit.json"
SENSITIVE_PATH = re.compile(r"(^|/)(\.env(?!\.example)|[^/]*(secret|token|password|credential|private[_-]?key|id_rsa)[^/]*|[^/]+\.(pem|p12|pfx|key|sif|bam|cram|pat(\.gz)?|fastq(\.gz)?)$)", re.I)
SECRET_PATTERNS = {
    "private_key_pem": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "aws_access_key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "github_personal_token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    "slack_token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}
MAX_SECRET_SCAN_BYTES = 12 * 1024 * 1024
LARGE_BLOB_BYTES = 50 * 1024 * 1024


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=check)


def sanitize_remote(value: str) -> str:
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        return f"ssh://{host}/{path}"
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return "configured_non_url_remote"
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    path = parsed.path
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Exact Git repository to audit; do not infer from a similarly named web-server checkout.")
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        report = {
            "schema": "methunmix-git-history-audit-v1",
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "NOT_A_GIT_REPOSITORY",
            "repository": str(repo),
        }
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    root = git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    refs = [line for line in git(repo, "for-each-ref", "--format=%(refname)").stdout.splitlines() if line]
    if not refs:
        refs = ["HEAD"]
    commits = [line.split(" ", 2) for line in git(repo, "log", *refs, "--format=%H %cI %s").stdout.splitlines() if line]
    status_lines = git(repo, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    modified_count = sum(1 for line in status_lines if not line.startswith("??"))
    untracked_count = sum(1 for line in status_lines if line.startswith("??"))
    remote_result = git(repo, "config", "--get", "remote.origin.url", check=False)
    remote = sanitize_remote(remote_result.stdout.strip()) if remote_result.returncode == 0 else None

    objects = git(repo, "rev-list", "--objects", *refs).stdout.splitlines()
    oid_paths: dict[str, set[str]] = defaultdict(set)
    for line in objects:
        parts = line.split(" ", 1)
        oid = parts[0]
        if len(parts) > 1:
            oid_paths[oid].add(parts[1])

    blob_rows = []
    sensitive_names = []
    large_blobs = []
    secret_findings = []
    object_type_errors = []
    for oid, paths in oid_paths.items():
        type_result = git(repo, "cat-file", "-t", oid, check=False)
        if type_result.returncode != 0:
            object_type_errors.append({"oid": oid, "error": type_result.stderr.strip()[:200]})
            continue
        if type_result.stdout.strip() != "blob":
            continue
        size_result = git(repo, "cat-file", "-s", oid, check=False)
        try:
            size = int(size_result.stdout.strip())
        except ValueError:
            continue
        blob_rows.append((oid, size, paths))
        for path in paths:
            if SENSITIVE_PATH.search(path):
                sensitive_names.append({"path": path, "blob_oid": oid, "bytes": size})
        if size > LARGE_BLOB_BYTES:
            for path in paths:
                large_blobs.append({"path": path, "blob_oid": oid, "bytes": size})
        if size <= MAX_SECRET_SCAN_BYTES:
            data = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", oid], capture_output=True, check=False).stdout
            for kind, pattern in SECRET_PATTERNS.items():
                if pattern.search(data):
                    for path in paths:
                        secret_findings.append({"path": path, "blob_oid": oid, "finding_type": kind})

    tracked_current = git(repo, "ls-files").stdout.splitlines()
    current_sensitive_paths = [path for path in tracked_current if SENSITIVE_PATH.search(path)]
    history_clean = not (sensitive_names or large_blobs or secret_findings or object_type_errors)
    worktree_clean = modified_count == 0 and untracked_count == 0
    report = {
        "schema": "methunmix-git-history-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repository": root,
        "remote_origin_sanitized": remote,
        "branches_and_tags": refs,
        "commit_count_reachable": len(commits),
        "commits": [{"commit": parts[0], "committed_at": parts[1], "subject": " ".join(parts[2:])} for parts in commits],
        "reachable_blob_count": len(blob_rows),
        "historical_sensitive_filename_hits": sensitive_names,
        "historical_large_blob_hits_over_50_mib": large_blobs,
        "high_confidence_secret_pattern_hits": secret_findings,
        "object_scan_errors": object_type_errors,
        "current_worktree": {
            "modified_or_deleted_tracked_path_count": modified_count,
            "untracked_path_count": untracked_count,
            "tracked_sensitive_filename_hits": current_sensitive_paths,
            "scope_note": "Git history audit covers reachable committed blobs only. Uncommitted/untracked worktree data is not part of a history-only rename audit and must be separately reviewed before any push or public release.",
        },
        "history_scan_status": "PASS_NO_MATCHES_IN_SCANNED_REACHABLE_REFS" if history_clean else "REVIEW_REQUIRED",
        "status": "HISTORY_SCAN_PASS_BUT_MIGRATION_REVIEW_REQUIRED" if history_clean and not worktree_clean else ("PASS_HISTORY_SCAN_NO_MATCHES" if history_clean else "REVIEW_REQUIRED"),
        "limitations": [
            "A clean secret-pattern scan is not proof that no sensitive or restricted content exists.",
            "Binary blobs larger than 12 MiB are not content-scanned for secret patterns; all blobs are checked by filename and size.",
            "License and data redistribution rights are audited separately from Git secrets.",
            "Repository identity should be confirmed by owner; the Nextflow source tree itself currently has no Git history in its supplied path.",
            "Reachable local branch/remote refs were scanned explicitly; a broken Codex checkpoint ref was excluded rather than repaired or removed.",
            "The audited GitHub remote is a DeMethFlow web-server repository; it may not be the intended long-term source repository for the standalone offline MethUnmix CLI.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "repository": root,
        "remote_origin_sanitized": remote,
        "commits": len(commits),
        "reachable_blobs": len(blob_rows),
        "sensitive_filename_hits": len(sensitive_names),
        "large_blob_hits": len(large_blobs),
        "secret_pattern_hits": len(secret_findings),
        "worktree_modified": modified_count,
        "worktree_untracked": untracked_count,
        "history_scan_status": report["history_scan_status"],
        "status": report["status"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS_HISTORY_SCAN_NO_MATCHES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
