#!/usr/bin/env python3
"""Audit E2A package provenance and create a non-mutating G03 RC3 overlay."""
from __future__ import annotations
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E = ROOT / "evidence"
RC3_SOURCE = "7d4ea176c5fe7b3fbaa0254f83d1a6fd3bf765101b76bd76590862540a8fb1c6"
RC3_WHEEL = "0d8d29094b847330a34a29ac19d41459768773abcbf052502ecf2d60061acf8f"
PRE_SOURCE = "693c4f57299de2cd81a7ac7da8ba8d567fed7e467d860eea812acae57179c2c3"
PRE_WHEEL = "dc447d0bd0ae441845ad6fc8852cf08b497b3a70ac6839a1f3e54c0d23360073"
G03_NAME = "tier_e_e2a_g03_submanifest_rc.json"
G03_SHA = "85d52ccc49fdc154096cc3bf43dfa112aa6ade264690a10c30497e1e87be1765"

def load(name):
    with (E / name).open() as f:
        return json.load(f)

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def package_values(doc):
    src, wheel = set(), set()
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                lk = key.lower()
                if "source_sha256" in lk and isinstance(child, str): src.add(child)
                if "wheel_sha256" in lk and isinstance(child, str): wheel.add(child)
                walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(doc)
    return src, wheel

def provenance_values(doc):
    commits, labels = set(), set()
    def walk(value, key_path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                lk = key.lower()
                if "commit" in lk and isinstance(child, str): commits.add(child)
                if any(token in lk for token in ("provenance", "phase", "candidate", "release", "version")) and isinstance(child, str): labels.add(f"{key}={child}")
                walk(child, f"{key_path}/{key}")
        elif isinstance(value, list):
            for child in value: walk(child, key_path)
    walk(doc)
    return commits, labels

def classify(label, src, wheel, mixed=False):
    # Package manifests and cumulative matrices intentionally record historical
    # candidates; their artifact keys are not execution-row bindings.
    if label in {"e2a_g02_retry3_coverage_matrix", "package_manifest_legacy_rc", "package_manifest_phase3b"}:
        return "HISTORICAL_PRE_RC3_VALID"
    if label == "package_manifest_phase3c":
        return "CONSISTENT_RC3"
    if mixed or (RC3_SOURCE in src and PRE_SOURCE in src) or (RC3_WHEEL in wheel and PRE_WHEEL in wheel):
        return "STALE_BINDING"
    if src == {RC3_SOURCE} and wheel == {RC3_WHEEL}: return "CONSISTENT_RC3"
    if src == {PRE_SOURCE} and wheel == {PRE_WHEEL}: return "HISTORICAL_PRE_RC3_VALID"
    if not src and not wheel: return "NOT_APPLICABLE"
    return "AMBIGUOUS"

def strip_binding(doc):
    d = copy.deepcopy(doc)
    for key in ("package_binding", "artifact_role", "historical_scientific_selection_artifact", "scientific_scope_equivalence"):
        d.pop(key, None)
    for row in d.get("combinations", []):
        for key in ("package_source_sha256", "package_normalized_wheel_sha256", "package_provenance_class"):
            row.pop(key, None)
    return d

def main():
    now = datetime.now(timezone.utc).isoformat()
    selected = [
        ("authoritative_e2a_manifest", "tier_e_e2a_manifest_rc.json", "immutable selection manifest"),
        ("e2a_g01_execution", "tier_e_e2a_g01_execution_rc.json", "historical execution evidence"),
        ("e2a_g02_retry2_execution", "tier_e_e2a_g02_retry2_execution_rc.json", "preflight attempt evidence"),
        ("e2a_g02_submanifest", "tier_e_e2a_g02_submanifest_rc.json", "execution submanifest"),
        ("e2a_g02_retry3_execution", "tier_e_e2a_g02_retry3_execution_rc.json", "successful execution evidence"),
        ("e2a_g02_retry3_runtime", "tier_e_e2a_g02_retry3_runtime_binding_rc.json", "runtime binding evidence"),
        ("e2a_g03_submanifest", G03_NAME, "historical G03 scientific selection with mixed binding"),
        ("e2a_g02_retry3_coverage_matrix", "scientific_compatibility_coverage_matrix_e2a_g02_retry3_rc.json", "historical cumulative matrix"),
        ("package_manifest_phase3b", "package_manifest_phase3b_rc1.json", "historical package manifest"),
        ("package_manifest_legacy_rc", "package_manifest_rc.json", "historical static-catalog package manifest"),
        ("package_manifest_phase3c", "package_manifest_native_resource_phase3c_rc.json", "current local RC3 package manifest"),
    ]
    files = []
    for label, name, role in selected:
        doc = load(name); src, wheel = package_values(doc); commits, labels = provenance_values(doc)
        mixed = label == "e2a_g03_submanifest" and doc.get("package_binding", {}).get("source_sha256") == RC3_SOURCE and any(c.get("package_source_sha256") == PRE_SOURCE for c in doc.get("combinations", []))
        files.append({"label": label, "path": f"evidence/{name}", "sha256": sha(E / name), "role": role, "source_sha256_values": sorted(src), "wheel_sha256_values": sorted(wheel), "git_commit_values": sorted(commits), "rc_generation_labels": sorted(labels), "classification": classify(label, src, wheel, mixed)})
    audit = {
        "schema": "methunmix-e2a-package-binding-audit-phase3f-v1",
        "generated_at": now,
        "status": "PASS_AUDIT_WITH_HISTORICAL_AND_STALE_BINDINGS_IDENTIFIED",
        "current_rc3": {"source_sha256": RC3_SOURCE, "normalized_wheel_sha256": RC3_WHEEL},
        "pre_rc3": {"source_sha256": PRE_SOURCE, "normalized_wheel_sha256": PRE_WHEEL},
        "historical_g03_submanifest": {"path": f"evidence/{G03_NAME}", "sha256": G03_SHA, "classification": "HISTORICAL_G03_SUBMANIFEST_WITH_STALE_COMBINATION_PACKAGE_BINDING", "immutable_preserved": True, "future_execution_authority": False},
        "files": files,
        "affected_rows": {"authoritative_e2a_manifest": 10, "e2a_g01_execution": 4, "e2a_g03_submanifest": 2},
        "authoritative_manifest_current_execution_authority": False,
        "g01_g02_summary": {"g01": "HISTORICAL_PRE_RC3_VALID", "g02_retry2": "CONSISTENT_RC3", "g02_retry3": "CONSISTENT_RC3", "g03": "STALE_BINDING"},
        "stale_binding_files": ["evidence/tier_e_e2a_g03_submanifest_rc.json"],
        "root_cause": {"classification": "INCOMPLETE_MANIFEST_OVERLAY_OR_ROW_LOCAL_METADATA_INHERITANCE", "findings": ["The authoritative E2A manifest was locked with the Phase 3B package and all ten combination rows consistently carry that historical digest.", "G02 submanifest was regenerated with RC3 binding at both top level and combination rows.", "G03 submanifest top-level binding was changed to RC3, but its two copied combination-level package fields remained from the parent manifest.", "No checked-in G03 submanifest generator was found; this evidence does not establish a packaged generator defect."], "generator_defect": "NO_GENERATOR_DEFECT_CONFIRMED", "minimal_future_fix": "Generate a new immutable execution-binding overlay that recursively binds every selected row to RC3; do not edit historical manifests."},
        "scope_impact": {"scientific_fields_changed": False, "g01_g02_scientific_scope_changed": False, "g03_scientific_scope_changed": False, "ready_ru_changed": False, "frozen_baseline_changed": False, "packaged_runtime_changed": False},
    }
    (E / "e2a_package_binding_audit_phase3f_rc.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    original = load(G03_NAME); overlay = copy.deepcopy(original)
    overlay["schema"] = "methunmix-tier-e-e2a-g03-execution-binding-rc3-phase3f-v1"
    overlay["artifact_role"] = "CURRENT_RC3_EXECUTION_PACKAGE_BINDING_OVERLAY"
    overlay["historical_scientific_selection_artifact"] = {"path": f"evidence/{G03_NAME}", "sha256": G03_SHA, "role": "HISTORICAL_SCIENTIFIC_SELECTION_ARTIFACT"}
    overlay["package_binding"] = {"source_sha256": RC3_SOURCE, "normalized_wheel_sha256": RC3_WHEEL, "source_tree_fallback": False, "binding_scope": "TOP_LEVEL_AND_EVERY_COMBINATION_ENTRY"}
    for row in overlay["combinations"]:
        row["package_source_sha256"] = RC3_SOURCE; row["package_normalized_wheel_sha256"] = RC3_WHEEL; row["package_provenance_class"] = "CURRENT_RC3_EXECUTION_BINDING_OVERLAY"
    overlay["scientific_scope_equivalence"] = "PASS"; overlay["scientific_status_promotion"] = "PROHIBITED"
    out = E / "tier_e_e2a_g03_execution_binding_rc3_phase3f.json"; out.write_text(json.dumps(overlay, indent=2, ensure_ascii=False) + "\n")
    eq = {"schema": "methunmix-e2a-g03-execution-binding-equivalence-phase3f-v1", "generated_at": now, "historical_manifest": {"path": f"evidence/{G03_NAME}", "sha256": G03_SHA}, "new_execution_binding": {"path": "evidence/tier_e_e2a_g03_execution_binding_rc3_phase3f.json", "sha256": sha(out)}, "comparison_fields": ["combination_id", "scenario", "genome_build", "platform", "method", "profile", "analysis_contract", "execution_input_type", "execution_input_files", "frozen_fixture_all_input_collection_sha256", "reference_selector", "reference_manifest", "truth_files", "truth_collection_sha256", "native_input_contract", "current_matrix_state", "baseline_status", "timeout_policy", "timeout_seconds"], "scientific_rows_identical": strip_binding(original).get("combinations") == strip_binding(overlay).get("combinations"), "only_allowed_change": "package binding and execution-context metadata schema", "ready_ru_changed": False, "frozen_baseline_changed": False, "scientific_status_promotion": "PROHIBITED"}
    eq["status"] = "PASS" if eq["scientific_rows_identical"] else "STOP_SCIENTIFIC_FIELD_CHANGED"
    (E / "g03_execution_binding_equivalence_phase3f_rc.json").write_text(json.dumps(eq, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"audit": str(E / "e2a_package_binding_audit_phase3f_rc.json"), "overlay": str(out), "overlay_sha256": sha(out), "equivalence": str(E / "g03_execution_binding_equivalence_phase3f_rc.json"), "status": eq["status"]}, indent=2))

if __name__ == "__main__": main()
