from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import PROJECT_ROOT
from .errors import ManifestError, ResolutionError
from .manifest import ReferenceBundle, load_reference
from .util import default_data_home, legacy_data_home


@dataclass(frozen=True)
class CatalogProblem:
    path: str
    error: str


class ReferenceCatalog:
    def __init__(self, roots: list[Path]):
        self.roots = _dedupe_paths(roots)
        self.bundles: list[ReferenceBundle] = []
        self.problems: list[CatalogProblem] = []
        self.conflicting_bundles: dict[str, ReferenceBundle] = {}

    @classmethod
    def discover(cls, explicit_root: Path | None = None) -> "ReferenceCatalog":
        roots: list[Path] = []
        if explicit_root:
            roots.append(explicit_root)
        elif os.environ.get("METHUNMIX_REFERENCE_ROOT"):
            roots.append(Path(os.environ["METHUNMIX_REFERENCE_ROOT"]))
        elif os.environ.get("DEMETHFLOW_REFERENCE_ROOT"):
            roots.append(Path(os.environ["DEMETHFLOW_REFERENCE_ROOT"]))
        roots.append(default_data_home() / "references")
        roots.append(legacy_data_home() / "references")
        roots.append(PROJECT_ROOT / "references")
        return cls(roots)

    def scan(self, verify_files: bool = False) -> "ReferenceCatalog":
        self.bundles = []
        self.problems = []
        self.conflicting_bundles = {}
        by_selector: dict[str, ReferenceBundle] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for manifest_path in sorted(root.rglob("manifest.json")):
                try:
                    bundle = load_reference(manifest_path, verify_files=verify_files)
                    previous = by_selector.get(bundle.selector)
                    if previous:
                        left = previous.content_digest()
                        right = bundle.content_digest()
                        if left != right:
                            self.conflicting_bundles[bundle.selector] = previous
                            self.problems.append(
                                CatalogProblem(
                                    str(manifest_path),
                                    f"Conflicting installed bundles {bundle.selector}: "
                                    f"{previous.manifest_path} and {manifest_path}",
                                )
                            )
                        continue
                    by_selector[bundle.selector] = bundle
                    self.bundles.append(bundle)
                except ManifestError as exc:
                    self.problems.append(CatalogProblem(str(manifest_path), str(exc)))
        if self.conflicting_bundles:
            conflicts = set(self.conflicting_bundles)
            self.bundles = [bundle for bundle in self.bundles if bundle.selector not in conflicts]
        return self

    def resolve(
        self,
        *,
        reference: str | None,
        scenario: str | None,
        platform: str | None,
        genome_build: str | None = None,
        allow_cross_platform: bool = False,
        analysis_contract: str | None = None,
    ) -> ReferenceBundle:
        if (
            analysis_contract == "array_epic_from_450k_common_cpg_v1"
            and (not reference or "@" not in reference)
        ):
            raise ResolutionError(
                "array_epic_from_450k_common_cpg_v1 requires an exact versioned reference selector"
            )
        if (
            analysis_contract in {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}
            and (not reference or "@" not in reference)
        ):
            raise ResolutionError(
                f"{analysis_contract} requires an exact versioned reference selector"
            )
        matching_conflicts = [
            bundle
            for bundle in self.conflicting_bundles.values()
            if _matches_filters(
                bundle,
                reference=reference,
                scenario=scenario,
                platform=platform,
                genome_build=genome_build,
                allow_cross_platform=allow_cross_platform,
                analysis_contract=analysis_contract,
            )
        ]
        if matching_conflicts:
            values = ", ".join(sorted(bundle.selector for bundle in matching_conflicts))
            raise ResolutionError(
                f"Conflicting installed reference content for {values}; remove the duplicate or select a clean root"
            )
        candidates = list(self.bundles)
        if reference:
            if "@" in reference:
                candidates = [bundle for bundle in candidates if bundle.selector == reference]
            else:
                candidates = [bundle for bundle in candidates if bundle.reference_id == reference]
        if scenario:
            candidates = [bundle for bundle in candidates if bundle.scenario_id == scenario]
        if analysis_contract:
            candidates = [bundle for bundle in candidates if bundle.contract == analysis_contract]
        if platform:
            if allow_cross_platform:
                candidates = [bundle for bundle in candidates if platform in bundle.compatible_routes]
            else:
                candidates = [
                    bundle for bundle in candidates
                    if platform in bundle.compatible_routes
                    and _native_platform(bundle.contract) == platform
                    and bundle.source_platform == platform
                ]
        if genome_build:
            candidates = [bundle for bundle in candidates if bundle.payload["genome_build"] == genome_build]
        explicit_immutable = bool(reference and "@" in reference)
        allowed_statuses = (
            {"READY", "PARTIAL", "RELEASED_UNVALIDATED", "QUARANTINED"}
            if explicit_immutable
            else {"READY", "PARTIAL"}
        )
        candidates = [bundle for bundle in candidates if bundle.status in allowed_statuses]
        if not explicit_immutable:
            candidates = [
                bundle for bundle in candidates
                if bundle.payload.get("release_validation", {}).get("publishable") is not False
            ]
        if not explicit_immutable:
            latest: dict[str, ReferenceBundle] = {}
            for bundle in candidates:
                current = latest.get(bundle.reference_id)
                if current is None or _default_bundle_key(bundle) > _default_bundle_key(current):
                    latest[bundle.reference_id] = bundle
            candidates = list(latest.values())
        if not candidates:
            filters = (
                f"reference={reference!r}, scenario={scenario!r}, platform={platform!r}, "
                f"genome_build={genome_build!r}"
                f", analysis_contract={analysis_contract!r}"
            )
            raise ResolutionError(f"No installed usable reference matches {filters}")
        if len(candidates) > 1:
            values = ", ".join(bundle.selector for bundle in candidates)
            raise ResolutionError(f"Multiple references match; select one with --reference: {values}")
        selected = candidates[0]
        if (
            selected.contract == "array_epic_from_450k_common_cpg_v1"
            and analysis_contract != "array_epic_from_450k_common_cpg_v1"
        ):
            raise ResolutionError(
                f"{selected.selector} requires explicit analysis contract "
                "array_epic_from_450k_common_cpg_v1"
            )
        if selected.contract in {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"} and analysis_contract != selected.contract:
            raise ResolutionError(
                f"{selected.selector} requires explicit analysis contract {selected.contract}"
            )
        if (
            selected.contract in {"wgbs_derived_epic_cpg_v1", "wgbs_derived_450k_cpg_v1"}
            and (
                not selected.payload.get("input_measurement_type")
                or (
                    selected.payload.get("genome_build") == "hg19"
                    and not selected.payload.get("wgbs_projection", {}).get("hg19_annotation_audit")
                )
            )
        ):
            raise ResolutionError(
                f"{selected.selector} is a historical derived-route candidate missing mandatory release provenance and is not runnable; "
                "select the current audited immutable reference documented for this route"
            )
        return selected


def _native_platform(contract: str) -> str:
    return {
        "array_450k_cpg": "450k",
        "array_epic_cpg": "epic",
        "array_epic_from_450k_common_cpg_v1": "epic",
        "wgbs_derived_epic_cpg_v1": "wgbs",
        "wgbs_derived_450k_cpg_v1": "wgbs",
        "wgbs_native_hg19": "wgbs",
        "wgbs_native_hg38": "wgbs",
    }[contract]


def _matches_filters(
    bundle: ReferenceBundle,
    *,
    reference: str | None,
    scenario: str | None,
    platform: str | None,
    genome_build: str | None,
    allow_cross_platform: bool,
    analysis_contract: str | None = None,
) -> bool:
    if reference:
        if "@" in reference and bundle.selector != reference:
            return False
        if "@" not in reference and bundle.reference_id != reference:
            return False
    if scenario and bundle.scenario_id != scenario:
        return False
    if analysis_contract and bundle.contract != analysis_contract:
        return False
    if platform:
        if platform not in bundle.compatible_routes:
            return False
        if not allow_cross_platform and (
            _native_platform(bundle.contract) != platform or bundle.source_platform != platform
        ):
            return False
    if genome_build and bundle.payload["genome_build"] != genome_build:
        return False
    return bundle.status in {"READY", "PARTIAL", "RELEASED_UNVALIDATED"}


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    output: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        value = str(path.expanduser().resolve())
        if value not in seen:
            output.append(Path(value))
            seen.add(value)
    return output


def _version_key(value: str) -> tuple:
    main, separator, suffix = value.partition("-")
    parts = []
    for item in main.split("."):
        parts.append((0, int(item)) if item.isdigit() else (1, item.lower()))
    while len(parts) < 3:
        parts.append((0, 0))
    return tuple(parts), (0 if not separator else -1), suffix.lower()


def _default_bundle_key(bundle: ReferenceBundle) -> tuple:
    """Prefer released/legacy bundles before an explicitly pending candidate."""
    release_validation = bundle.payload.get("release_validation")
    if (
        release_validation
        and release_validation.get("stage") in {"final", "formal_offline_release"}
        # Older final releases wrote ``publishable=true`` explicitly, while
        # current formal offline releases rely on the stage and omit it.  Only
        # an explicit false value may demote either form; otherwise the newer
        # immutable formal release must supersede an older rc/final bundle.
        and release_validation.get("publishable") is not False
    ):
        # A scientifically honest PARTIAL final release must supersede its
        # legacy predecessor for the tools it explicitly marks READY.  The
        # former ordering accidentally ranked every release-state manifest
        # below pre-state-machine bundles, including completed final releases.
        release_rank = 2
    elif bundle.status == "READY":
        release_rank = 2
    elif not release_validation:
        # Older manifests predate the release-validation state machine.  Keep
        # their established default position while a new version is pending.
        release_rank = 1
    else:
        release_rank = 0
    return release_rank, _version_key(bundle.version)
