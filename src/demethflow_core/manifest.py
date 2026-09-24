from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import CORE_API, REFERENCE_SCHEMA, RUNTIME_API
from .errors import ManifestError
from .util import read_json, resolve_artifact, sha256_file, stable_digest, validate_id


VALID_BUNDLE_STATUSES = {
    "READY",
    "PARTIAL",
    "RELEASED_UNVALIDATED",
    "QUARANTINED",
    "INVALID",
}
VALID_TOOL_STATUSES = {
    "READY",
    "RELEASED_UNVALIDATED",
    "NOT_AVAILABLE",
    "NOT_ADAPTED",
    "QUARANTINED",
    "INVALID",
}
KNOWN_TOOL_NAMES = (
    "ARIC",
    "CelFEER",
    "CelFiE",
    "EDec",
    "EMeth",
    "EpiDISH",
    "EpiSCORE",
    "Houseman",
    "MEnet",
    "MeDeCom",
    "MetDecode",
    "MethAtlas",
    "MethylBERT",
    "MethylCIBERSORT",
    "PRmeth",
    "RefFreeEWAS",
    "Tsisal",
    "UXM",
)
# Candidate references are intentionally executable only through the explicit
# ``--validation-candidate`` route.  Keep this allow-list small and tied to an
# exact artifact family: a quarantined reference must never become runnable
# merely because a caller supplied a broad development flag.
VALIDATION_CANDIDATE_TOOLS: dict[str, frozenset[str]] = {
    "hg19_native_wgbs_menet_v1": frozenset({"MEnet"}),
    "hg19_native_wgbs_immune6_celfeer_celfie_metdecode_v1": frozenset(
        {"CelFEER", "CelFiE", "MetDecode"}
    ),
}
CONTRACT_TO_PLATFORM = {
    "array_450k_cpg": "450k",
    "array_epic_cpg": "epic",
    "array_epic_from_450k_common_cpg_v1": "epic",
    "wgbs_derived_epic_cpg_v1": "wgbs",
    "wgbs_derived_450k_cpg_v1": "wgbs",
    "wgbs_native_hg19": "wgbs",
    "wgbs_native_hg38": "wgbs",
}

NATIVE_WGBS_CONTRACTS = {"wgbs_native_hg19", "wgbs_native_hg38"}
WGBS_DERIVED_ARRAY_CONTRACTS = {
    "wgbs_derived_epic_cpg_v1",
    "wgbs_derived_450k_cpg_v1",
}


def is_native_wgbs_contract(contract: str) -> bool:
    return contract in NATIVE_WGBS_CONTRACTS


def is_wgbs_derived_array_contract(contract: str) -> bool:
    """Return whether a contract projects build-locked WGBS BED into an array axis."""
    return contract in WGBS_DERIVED_ARRAY_CONTRACTS


def genome_build_for_contract(contract: str) -> str:
    if is_native_wgbs_contract(contract):
        return contract.rsplit("_", 1)[-1]
    if is_wgbs_derived_array_contract(contract):
        # The build is supplied by the immutable derived-reference declaration.
        return "declared_in_projection"
    return "not_applicable"


def _compatible(actual: str, required: str) -> bool:
    return actual.split(".", 1)[0] == required.split(".", 1)[0]


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    status: str
    runtime_modules: tuple[str, ...]
    reason: str | None = None
    validation_policy: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolCapability:
    name: str
    status: str
    artifacts: dict[str, str]
    runtime_modules: tuple[str, ...]
    reason: str | None = None
    reference_requirement: str = "required"
    output_contract: str = "cell_proportions_v1"
    execution_policy: str = "normal"
    execution_profiles: dict[str, ExecutionProfile] | None = None
    data_modules: tuple[str, ...] = ()
    validation_scope: str | None = None
    reference_mode: str = "bundle_artifact"
    builtin_reference: str | None = None
    postprocess_profile: str | None = None
    validation_policy: dict[str, Any] | None = None
    execution_defaults: dict[str, Any] | None = None
    validation_candidate: str | None = None

    def effective_status(self, accelerator: str) -> tuple[str, str | None]:
        if not self.execution_profiles:
            return self.status, self.reason
        profile = self.execution_profiles.get(accelerator)
        if not profile:
            return "NOT_AVAILABLE", f"no {accelerator} execution profile is declared"
        return profile.status, profile.reason

    def effective_runtime_modules(self, accelerator: str) -> tuple[str, ...]:
        if not self.execution_profiles:
            return self.runtime_modules
        profile = self.execution_profiles.get(accelerator)
        return profile.runtime_modules if profile else ()

    def aggregate_profile_status(self) -> str:
        """Return the capability status implied by its execution profiles."""
        if not self.execution_profiles:
            return self.status
        statuses = {profile.status for profile in self.execution_profiles.values()}
        if statuses == {"READY"}:
            return "READY"
        if statuses == {"RELEASED_UNVALIDATED"}:
            return "RELEASED_UNVALIDATED"
        if "READY" in statuses or "RELEASED_UNVALIDATED" in statuses:
            return "PARTIAL"
        if statuses == {"NOT_AVAILABLE"}:
            return "NOT_AVAILABLE"
        if "INVALID" in statuses:
            return "INVALID"
        return "QUARANTINED"


@dataclass(frozen=True)
class ReferenceBundle:
    manifest_path: Path
    payload: dict[str, Any]
    tools: dict[str, ToolCapability]

    @property
    def root(self) -> Path:
        return self.manifest_path.parent

    @property
    def reference_id(self) -> str:
        return self.payload["reference_id"]

    @property
    def version(self) -> str:
        return self.payload["version"]

    @property
    def selector(self) -> str:
        return f"{self.reference_id}@{self.version}"

    @property
    def scenario_id(self) -> str:
        return self.payload["scenario_id"]

    @property
    def contract(self) -> str:
        return self.payload["analysis_contract"]

    @property
    def compatible_routes(self) -> tuple[str, ...]:
        return tuple(self.payload["compatible_input_routes"])

    @property
    def cell_types(self) -> tuple[dict[str, str], ...]:
        return tuple(self.payload["cell_types"])

    @property
    def status(self) -> str:
        return self.payload["status"]

    @property
    def source_platform(self) -> str:
        return self.payload["source_platform"]

    def artifact(self, tool: str, key: str) -> Path:
        capability = self.tools[tool]
        try:
            relative = capability.artifacts[key]
        except KeyError as exc:
            raise ManifestError(f"{self.selector}: {tool} has no artifact named {key}") from exc
        return resolve_artifact(self.root, relative, f"{tool}.{key}")

    def selected_tools(
        self,
        requested: str,
        accelerator: str = "cpu",
        allow_validation_candidate: bool = False,
    ) -> tuple[list[str], list[dict[str, str]]]:
        if requested.strip().lower() == "all":
            ready: list[str] = []
            skipped: list[dict[str, str]] = []
            for name, cap in self.tools.items():
                status, reason = cap.effective_status(accelerator)
                # A quarantined/unavailable profile is blocked regardless of
                # its policy metadata.  Do not relabel an engineering failure
                # as merely explicit-only, otherwise ``--tools all`` would
                # hide the real reason and make a blocked tool look runnable.
                if status in {"QUARANTINED", "NOT_AVAILABLE", "NOT_ADAPTED", "INVALID"}:
                    skipped.append({"tool": name, "status": status, "reason": reason or ""})
                elif cap.execution_policy == "explicit_only":
                    skipped.append({"tool": name, "status": "EXPLICIT_ONLY", "reason": reason or "explicit selection required"})
                elif status in {"READY", "RELEASED_UNVALIDATED"}:
                    ready.append(name)
                else:
                    skipped.append({"tool": name, "status": status, "reason": reason or ""})
            return ready, skipped
        aliases = {"cibersort": "MethylCIBERSORT", "tsisa": "Tsisal", "prmeth": "PRmeth"}
        normalized = {name.lower(): name for name in self.tools}
        known = {name.lower(): name for name in KNOWN_TOOL_NAMES}
        chosen: list[str] = []
        for raw in requested.split(","):
            value = raw.strip()
            if not value:
                continue
            name = aliases.get(value.lower()) or normalized.get(value.lower()) or known.get(value.lower())
            if not name:
                raise ManifestError(f"{self.selector}: unknown tool {value!r}")
            if name not in self.tools:
                raise ManifestError(
                    f"{self.selector}: {name} is NOT_AVAILABLE: selected reference does not declare this tool"
                )
            cap = self.tools[name]
            status, reason = cap.effective_status(accelerator)
            validation_candidate = (
                allow_validation_candidate
                and cap.status == "QUARANTINED"
                and cap.validation_candidate in VALIDATION_CANDIDATE_TOOLS
                and name in VALIDATION_CANDIDATE_TOOLS[cap.validation_candidate]
                and self.contract == "wgbs_native_hg19"
                and self.version.endswith(("-rc1", "-rc2"))
            )
            if status not in {"READY", "RELEASED_UNVALIDATED"} and not validation_candidate:
                suffix = f": {reason}" if reason else ""
                raise ManifestError(f"{self.selector}: {name} is {status}{suffix}")
            if name not in chosen:
                chosen.append(name)
        if not chosen:
            raise ManifestError("--tools selected no tools")
        return chosen, []

    def content_digest(self, verify_files: bool = False) -> str:
        pairs: list[tuple[str, str]] = []
        declared = self.payload.get("artifact_checksums", {})
        for tool, cap in self.tools.items():
            for key, relative in cap.artifacts.items():
                # Validation reports and device smoke records describe evidence
                # about a reference; they are not part of the immutable model /
                # marker payload being validated.  Excluding them prevents a
                # circular digest (report -> reference digest -> report) while
                # all report files remain checksum-verified by load_reference.
                if key in {"scientific_validation", "profile_validation", "cpu_runtime_validation"}:
                    continue
                label = f"{tool}.{key}:{relative}"
                expected = declared.get(relative)
                if verify_files:
                    path = self.artifact(tool, key)
                    if path.is_file():
                        actual = sha256_file(path)
                        if expected and expected != actual:
                            raise ManifestError(
                                f"{self.selector}: checksum mismatch for {relative}: "
                                f"expected {expected}, got {actual}"
                            )
                        expected = actual
                    elif path.is_dir():
                        nested = []
                        for child in sorted(path.rglob("*")):
                            if not child.is_file():
                                continue
                            child_relative = child.relative_to(self.root).as_posix()
                            actual = sha256_file(child)
                            nested_expected = declared.get(child_relative)
                            if nested_expected and nested_expected != actual:
                                raise ManifestError(
                                    f"{self.selector}: checksum mismatch for {child_relative}: "
                                    f"expected {nested_expected}, got {actual}"
                                )
                            nested.append((child_relative, actual))
                        expected = stable_digest(nested)
                if expected is None:
                    prefix = relative.rstrip("/") + "/"
                    nested_declared = [(name, digest) for name, digest in declared.items() if name.startswith(prefix)]
                    if nested_declared:
                        expected = stable_digest(nested_declared)
                pairs.append((label, expected or "unverified"))
        projection = self.payload.get("platform_projection")
        if isinstance(projection, dict):
            for name in ("epic_manifest", "hm450_manifest", "common_cpgs", "source_reference_manifest"):
                item = projection.get(name, {})
                if isinstance(item, dict) and isinstance(item.get("artifact"), str):
                    expected = str(item.get("sha256", "unverified"))
                    if verify_files:
                        path = resolve_artifact(self.root, item["artifact"], f"platform_projection.{name}")
                        actual = sha256_file(path)
                        if expected != "unverified" and actual != expected:
                            raise ManifestError(f"{self.selector}: checksum mismatch for {item['artifact']}")
                        expected = actual
                    pairs.append((f"platform_projection.{name}:{item['artifact']}", expected))
            for tool, item in projection.get("tool_features", {}).items():
                if isinstance(item, dict) and isinstance(item.get("artifact"), str):
                    expected = str(item.get("sha256", "unverified"))
                    if verify_files:
                        path = resolve_artifact(
                            self.root, item["artifact"], f"platform_projection.tool_features.{tool}"
                        )
                        actual = sha256_file(path)
                        if expected != "unverified" and actual != expected:
                            raise ManifestError(f"{self.selector}: checksum mismatch for {item['artifact']}")
                        expected = actual
                    pairs.append((f"platform_projection.tool_features.{tool}:{item['artifact']}", expected))
            source = projection.get("source_reference", {})
            if isinstance(source, dict):
                for key in ("selector", "manifest_sha256", "content_digest"):
                    if source.get(key) is not None:
                        pairs.append((f"platform_projection.source_reference.{key}", str(source[key])))
        wgbs_projection = self.payload.get("wgbs_projection")
        if isinstance(wgbs_projection, dict):
            for name in ("mapping", "source_reference_manifest"):
                item = wgbs_projection.get(name, {})
                if isinstance(item, dict) and isinstance(item.get("artifact"), str):
                    expected = str(item.get("sha256", "unverified"))
                    if verify_files:
                        path = resolve_artifact(self.root, item["artifact"], f"wgbs_projection.{name}")
                        actual = sha256_file(path)
                        if expected != "unverified" and actual != expected:
                            raise ManifestError(f"{self.selector}: checksum mismatch for {item['artifact']}")
                        expected = actual
                    pairs.append((f"wgbs_projection.{name}:{item['artifact']}", expected))
            for tool, item in wgbs_projection.get("tool_features", {}).items():
                if isinstance(item, dict) and isinstance(item.get("artifact"), str):
                    expected = str(item.get("sha256", "unverified"))
                    if verify_files:
                        path = resolve_artifact(self.root, item["artifact"], f"wgbs_projection.tool_features.{tool}")
                        actual = sha256_file(path)
                        if expected != "unverified" and actual != expected:
                            raise ManifestError(f"{self.selector}: checksum mismatch for {item['artifact']}")
                        expected = actual
                    pairs.append((f"wgbs_projection.tool_features.{tool}:{item['artifact']}", expected))
            source = wgbs_projection.get("source_reference", {})
            if isinstance(source, dict):
                for key in ("selector", "manifest_sha256", "content_digest"):
                    if source.get(key) is not None:
                        pairs.append((f"wgbs_projection.source_reference.{key}", str(source[key])))
        return stable_digest(pairs)


def load_reference(path: Path, verify_files: bool = False) -> ReferenceBundle:
    payload = read_json(path)
    if payload.get("schema") != "demethflow-reference-v1":
        raise ManifestError(f"Unsupported reference schema in {path}: {payload.get('schema')!r}")
    for key in (
        "reference_id",
        "version",
        "scenario_id",
        "source_platform",
        "analysis_contract",
        "compatible_input_routes",
        "genome_build",
        "status",
        "cell_types",
        "tool_capabilities",
        "compatibility",
    ):
        if key not in payload:
            raise ManifestError(f"Missing {key!r} in {path}")
    validate_id(payload["reference_id"], "reference_id")
    validate_id(payload["version"], "version")
    validate_id(payload["scenario_id"], "scenario_id")
    if payload["status"] not in VALID_BUNDLE_STATUSES:
        raise ManifestError(f"Invalid bundle status in {path}: {payload['status']!r}")
    if payload["analysis_contract"] not in CONTRACT_TO_PLATFORM:
        raise ManifestError(f"Unsupported analysis_contract in {path}: {payload['analysis_contract']!r}")
    expected_build = genome_build_for_contract(payload["analysis_contract"])
    if expected_build != "declared_in_projection" and payload["genome_build"] != expected_build:
        raise ManifestError(
            f"{path}: analysis_contract={payload['analysis_contract']!r} requires "
            f"genome_build={expected_build!r}, got {payload['genome_build']!r}"
        )
    routes = payload["compatible_input_routes"]
    if not isinstance(routes, list) or not routes or any(route not in {"450k", "epic", "wgbs"} for route in routes):
        raise ManifestError(f"compatible_input_routes must be a non-empty platform list in {path}")
    if payload["analysis_contract"] == "array_epic_from_450k_common_cpg_v1":
        if payload["source_platform"] != "450k" or routes != ["epic"]:
            raise ManifestError(
                f"{path}: EPIC-from-450K requires source_platform='450k' and routes=['epic']"
            )
    if is_wgbs_derived_array_contract(payload["analysis_contract"]):
        expected_target = "epic" if payload["analysis_contract"] == "wgbs_derived_epic_cpg_v1" else "450k"
        if payload["source_platform"] != expected_target or routes != ["wgbs"]:
            raise ManifestError(
                f"{path}: {payload['analysis_contract']} requires source_platform={expected_target!r} "
                "and routes=['wgbs']"
            )
        if payload["genome_build"] not in {"hg19", "hg38"}:
            raise ManifestError(
                f"{path}: WGBS-derived array references require genome_build='hg19' or 'hg38'"
            )
    cells = payload["cell_types"]
    if not isinstance(cells, list) or not cells:
        raise ManifestError(f"cell_types must be a non-empty list in {path}")
    seen_ids: set[str] = set()
    for cell in cells:
        if not isinstance(cell, dict):
            raise ManifestError(f"Invalid cell_types entry in {path}: {cell!r}")
        cell_id = validate_id(cell.get("cell_type_id"), "cell_type_id")
        if cell_id in seen_ids:
            raise ManifestError(f"Duplicate cell_type_id {cell_id!r} in {path}")
        seen_ids.add(cell_id)
        if not isinstance(cell.get("display_name"), str) or not cell["display_name"].strip():
            raise ManifestError(f"Missing display_name for {cell_id!r} in {path}")
    compatibility = payload["compatibility"]
    if not isinstance(compatibility, dict):
        raise ManifestError(f"compatibility must be an object in {path}")
    for key, required in (("core_api", CORE_API), ("reference_schema", REFERENCE_SCHEMA), ("runtime_api", RUNTIME_API)):
        actual = str(compatibility.get(key, ""))
        if not _compatible(actual, required):
            raise ManifestError(f"{path}: incompatible {key}={actual!r}; core requires {required}")
    raw_tools = payload["tool_capabilities"]
    if not isinstance(raw_tools, dict) or not raw_tools:
        raise ManifestError(f"tool_capabilities must be a non-empty object in {path}")
    tools: dict[str, ToolCapability] = {}
    for name, raw in raw_tools.items():
        validate_id(name, "tool name")
        if not isinstance(raw, dict) or raw.get("status") not in VALID_TOOL_STATUSES:
            raise ManifestError(f"Invalid capability for {name} in {path}")
        artifacts = raw.get("artifacts", {})
        if not isinstance(artifacts, dict):
            raise ManifestError(f"{name}.artifacts must be an object in {path}")
        for artifact_name, relative in artifacts.items():
            validate_id(artifact_name, f"{name} artifact name")
            resolved = resolve_artifact(path.parent, relative, f"{name}.{artifact_name}")
            if verify_files and resolved.is_file():
                expected = payload.get("artifact_checksums", {}).get(relative)
                if expected and sha256_file(resolved) != expected:
                    raise ManifestError(f"Checksum mismatch: {path.parent / relative}")
        runtime_modules = raw.get("runtime_modules", [])
        if not isinstance(runtime_modules, list) or any(not isinstance(item, str) for item in runtime_modules):
            raise ManifestError(f"{name}.runtime_modules must be a list in {path}")
        data_modules = raw.get("data_modules", [])
        if not isinstance(data_modules, list) or any(not isinstance(item, str) for item in data_modules):
            raise ManifestError(f"{name}.data_modules must be a list in {path}")
        for module_name in data_modules:
            validate_id(module_name, f"{name} data module")
        reference_requirement = raw.get("reference_requirement", "required")
        if reference_requirement not in {"required", "none"}:
            raise ManifestError(f"{name}.reference_requirement must be required or none in {path}")
        output_contract = raw.get("output_contract", "cell_proportions_v1")
        if output_contract not in {
            "cell_proportions_v1",
            "reference_free_components_v1",
            "reference_free_components_v2",
        }:
            raise ManifestError(f"Unsupported {name}.output_contract={output_contract!r} in {path}")
        execution_policy = raw.get("execution_policy", "normal")
        if execution_policy not in {"normal", "explicit_only"}:
            raise ManifestError(f"Unsupported {name}.execution_policy={execution_policy!r} in {path}")
        reference_mode = raw.get("reference_mode", "bundle_artifact")
        if reference_mode not in {"bundle_artifact", "package_builtin"}:
            raise ManifestError(f"Unsupported {name}.reference_mode={reference_mode!r} in {path}")
        builtin_reference = raw.get("builtin_reference")
        if reference_mode == "package_builtin":
            if name != "EpiDISH":
                raise ManifestError(f"Only EpiDISH may use package_builtin reference mode in {path}")
            if not isinstance(builtin_reference, str) or not builtin_reference.strip():
                raise ManifestError(f"{name}.builtin_reference is required for package_builtin mode in {path}")
        postprocess_profile = raw.get("postprocess_profile")
        if postprocess_profile is not None and (
            not isinstance(postprocess_profile, str) or not postprocess_profile.strip()
        ):
            raise ManifestError(f"{name}.postprocess_profile must be a non-empty string in {path}")
        validation_policy = raw.get("validation_policy")
        if validation_policy is not None and not isinstance(validation_policy, dict):
            raise ManifestError(f"{name}.validation_policy must be an object in {path}")
        execution_defaults = raw.get("execution_defaults")
        if execution_defaults is not None and not isinstance(execution_defaults, dict):
            raise ManifestError(f"{name}.execution_defaults must be an object in {path}")
        validation_candidate = raw.get("validation_candidate")
        if validation_candidate is not None and (
            validation_candidate not in VALIDATION_CANDIDATE_TOOLS
            or name not in VALIDATION_CANDIDATE_TOOLS[validation_candidate]
        ):
            raise ManifestError(f"Unsupported {name}.validation_candidate in {path}")
        if output_contract in {"reference_free_components_v1", "reference_free_components_v2"}:
            if reference_requirement != "none":
                raise ManifestError(f"{name} reference-free output requires reference_requirement=none in {path}")
            if execution_policy != "explicit_only":
                raise ManifestError(f"{name} reference-free output requires execution_policy=explicit_only in {path}")
        if output_contract == "reference_free_components_v2":
            try:
                recommended_k = int((execution_defaults or {})["recommended_k"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ManifestError(f"{name}.execution_defaults.recommended_k must be a positive integer in {path}") from exc
            if recommended_k < 1:
                raise ManifestError(f"{name}.execution_defaults.recommended_k must be a positive integer in {path}")
        profiles_payload = raw.get("execution_profiles")
        profiles: dict[str, ExecutionProfile] | None = None
        if profiles_payload is not None:
            if not isinstance(profiles_payload, dict) or not profiles_payload:
                raise ManifestError(f"{name}.execution_profiles must be a non-empty object in {path}")
            profiles = {}
            for profile_name, profile_raw in profiles_payload.items():
                if profile_name not in {"cpu", "gpu"} or not isinstance(profile_raw, dict):
                    raise ManifestError(f"Invalid {name} execution profile {profile_name!r} in {path}")
                profile_status = profile_raw.get("status")
                profile_modules = profile_raw.get("runtime_modules", [])
                if profile_status not in VALID_TOOL_STATUSES:
                    raise ManifestError(f"Invalid {name}.{profile_name} status in {path}")
                if not isinstance(profile_modules, list) or any(not isinstance(item, str) for item in profile_modules):
                    raise ManifestError(f"{name}.{profile_name}.runtime_modules must be a list in {path}")
                profile_validation = profile_raw.get("validation_policy")
                if profile_validation is not None and not isinstance(profile_validation, dict):
                    raise ManifestError(f"{name}.{profile_name}.validation_policy must be an object in {path}")
                profiles[profile_name] = ExecutionProfile(
                    name=profile_name,
                    status=profile_status,
                    runtime_modules=tuple(profile_modules),
                    reason=profile_raw.get("reason"),
                    validation_policy=profile_validation,
                )
        tools[name] = ToolCapability(
            name=name,
            status=raw["status"],
            artifacts=dict(artifacts),
            runtime_modules=tuple(runtime_modules),
            reason=raw.get("reason"),
            reference_requirement=reference_requirement,
            output_contract=output_contract,
            execution_policy=execution_policy,
            execution_profiles=profiles,
            data_modules=tuple(data_modules),
            validation_scope=raw.get("validation_scope"),
            reference_mode=reference_mode,
            builtin_reference=builtin_reference,
            postprocess_profile=postprocess_profile,
            validation_policy=validation_policy,
            execution_defaults=execution_defaults,
            validation_candidate=validation_candidate,
        )
    bundle = ReferenceBundle(path.resolve(), payload, tools)
    if bundle.contract == "array_epic_from_450k_common_cpg_v1":
        # Local import avoids a manifest/projection import cycle while keeping
        # the cross-platform declaration mandatory and self-verifying.
        from .epic_projection import validate_projection_declaration

        validate_projection_declaration(bundle)
    if is_wgbs_derived_array_contract(bundle.contract):
        # Keep this import local to avoid a manifest/projection import cycle.
        from .wgbs_projection import validate_wgbs_projection_declaration

        validate_wgbs_projection_declaration(bundle, verify_files=verify_files)
    if verify_files:
        bundle.content_digest(verify_files=True)
    return bundle
