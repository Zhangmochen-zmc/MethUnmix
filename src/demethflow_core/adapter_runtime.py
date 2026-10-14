"""Built-in execution adapters for the bounded 2.x migration pilot.

Descriptors in :mod:`adapter_contract` are the single source of adapter
metadata.  This module intentionally contains only core-owned translation and
validation code: it never discovers arbitrary plugins, imports third-party
algorithm code, downloads a runtime, or executes a shell command.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapter_contract import ADAPTER_API_VERSION, AdapterDescriptor, load_adapter_descriptors, validate_adapter_binding
from .errors import DeMethFlowError
from .manifest import ReferenceBundle, ToolCapability


# This remains a closed, package-built registry.  Additions are deliberate
# production migrations, not runtime plugin discovery.
MIGRATED_TOOLS = frozenset({
    "Houseman", "MeDeCom", "CelFiE", "EDec", "EMeth", "EpiDISH", "EpiSCORE",
})


@dataclass(frozen=True)
class AdapterContext:
    """Core-owned facts supplied to a built-in adapter; no executable payload."""

    bundle: ReferenceBundle
    capability: ToolCapability
    tool: str
    accelerator: str
    execution_parameters: dict[str, object]
    external_runtime: Path | None = None


class BuiltInAdapter:
    """Safe behavioural adapter whose output remains a Nextflow argument list."""

    def __init__(self, descriptor: AdapterDescriptor):
        self.descriptor = descriptor

    def _require_api(self, required_api_version: str = ADAPTER_API_VERSION) -> None:
        if self.descriptor.adapter_api_version != required_api_version:
            raise DeMethFlowError(
                "ADAPTER_API_INCOMPATIBLE: "
                f"{self.descriptor.adapter_id} declares API {self.descriptor.adapter_api_version}; "
                f"core requires {required_api_version}"
            )

    def validate_input(self, context: AdapterContext) -> dict[str, object]:
        self._require_api()
        binding = validate_adapter_binding(
            self.descriptor,
            analysis_contract=context.bundle.contract,
            capability=context.capability,
        )
        problems = list(binding.get("problems", []))
        if context.accelerator not in self.descriptor.supported_profiles:
            problems.append(
                f"accelerator {context.accelerator} is not declared by adapter; "
                f"supported={list(self.descriptor.supported_profiles)}"
            )
        if context.tool != self.descriptor.tool:
            problems.append(f"adapter tool mismatch context={context.tool} descriptor={self.descriptor.tool}")
        if problems:
            raise DeMethFlowError(
                "ADAPTER_CAPABILITY_CONFLICT: " + "; ".join(problems)
            )
        return {**binding, "status": "PASS", "operation": "validate_input"}

    def prepare_input(self, context: AdapterContext) -> dict[str, object]:
        self.validate_input(context)
        return {
            "operation": "prepare_input",
            "input_representations": list(self.descriptor.input_representations),
            "preprocess": "ESTABLISHED_WORKFLOW_AUTHORITY_NO_ADAPTER_TRANSFORM",
        }

    def execute_arguments(self, context: AdapterContext) -> list[str]:
        """Translate existing core parameters without changing workflow semantics."""
        self.prepare_input(context)
        artifacts = context.capability.artifacts
        if context.tool == "Houseman":
            required = {"cell_reference": "ref_houseman_cell", "beta_reference": "ref_houseman_beta"}
            return [item for name, flag in required.items() if name in artifacts for item in (f"--{flag}", str(context.bundle.artifact(context.tool, name)))]
        if context.tool == "MeDeCom":
            params = context.execution_parameters
            for name in ("k", "lambda"):
                if name not in params:
                    raise DeMethFlowError(f"ADAPTER_PARAMETER_CONTRACT_FAILED: MeDeCom requires {name}")
            return [
                "--medecom_k", str(params["k"]),
                "--medecom_lambda", str(params["lambda"]),
                "--medecom_nfolds", str(params.get("nfolds") or 0),
                "--medecom_output_contract", context.capability.output_contract,
            ]
        if context.tool == "CelFiE":
            if context.external_runtime is None:
                raise DeMethFlowError("EXTERNAL_RUNTIME_NOT_INSTALLED: CelFiE requires the external runtime contract")
            atlas = context.bundle.artifact(context.tool, "atlas") if "atlas" in artifacts else None
            if atlas is None:
                raise DeMethFlowError("ADAPTER_REFERENCE_CONTRACT_FAILED: CelFiE atlas is missing")
            return ["--ref_celfie_atlas", str(atlas), "--tool_celfie", str(context.external_runtime)]
        if context.tool == "EDec":
            # The established workflow accepts a materialized Tref directly.
            # If it is absent, PREP_EDec derives it from the source reference
            # data and metadata.  Markers are required by RUN_EDec in either
            # branch.  Preserve that conditional production contract instead
            # of requiring inputs which the selected branch does not read.
            if "markers" not in artifacts:
                raise DeMethFlowError("ADAPTER_REFERENCE_CONTRACT_FAILED: EDec requires markers")
            marker_arguments = ["--ref_edec_mark", str(context.bundle.artifact(context.tool, "markers"))]
            if "tref" in artifacts:
                return [
                    "--ref_edec_tref", str(context.bundle.artifact(context.tool, "tref")),
                    *marker_arguments,
                ]
            missing = [name for name in ("reference_data", "metadata") if name not in artifacts]
            if missing:
                raise DeMethFlowError(
                    "ADAPTER_REFERENCE_CONTRACT_FAILED: EDec requires tref or "
                    "reference_data and metadata; missing " + ", ".join(missing)
                )
            return [
                "--ref_edec_data", str(context.bundle.artifact(context.tool, "reference_data")),
                "--ref_edec_meta", str(context.bundle.artifact(context.tool, "metadata")),
                *marker_arguments,
            ]
        if context.tool == "EMeth":
            required = {"reference": "ref_emeth", "source": "src_emeth"}
            missing = [name for name in required if name not in artifacts]
            if missing:
                raise DeMethFlowError(
                    "ADAPTER_REFERENCE_CONTRACT_FAILED: EMeth requires " + ", ".join(missing)
                )
            return [
                item
                for name, flag in required.items()
                for item in (f"--{flag}", str(context.bundle.artifact(context.tool, name)))
            ]
        if context.tool == "EpiDISH":
            # ``package_builtin`` is an established workflow mode: its
            # immutable runtime supplies the reference, so there is no bundle
            # artifact flag.  Dynamic/bundle mode must remain fail-closed.
            if "reference" in artifacts:
                return ["--ref_epidish", str(context.bundle.artifact(context.tool, "reference"))]
            if context.capability.reference_mode == "package_builtin":
                return []
            raise DeMethFlowError(
                "ADAPTER_REFERENCE_CONTRACT_FAILED: EpiDISH requires reference "
                "for bundle_artifact mode"
            )
        if context.tool == "EpiSCORE":
            # The established 450K/EPIC workflows consume a precomputed gene
            # reference directly when one is present.  Only bundles without
            # that derivative need the raw CpG reference for PREP_EpiSCORE.
            # Do not require both branches: the raw input is not read after a
            # verified gene_reference has been supplied.
            if "gene_reference" in artifacts:
                return [
                    "--ref_episcore_gene",
                    str(context.bundle.artifact(context.tool, "gene_reference")),
                ]
            if "reference" in artifacts:
                return [
                    "--ref_episcore",
                    str(context.bundle.artifact(context.tool, "reference")),
                ]
            raise DeMethFlowError(
                "ADAPTER_REFERENCE_CONTRACT_FAILED: EpiSCORE requires "
                "gene_reference or reference for workflow preparation"
            )

        raise DeMethFlowError(f"ADAPTER_UNMIGRATED_TOOL: {context.tool}")

    def execution_preflight(self, context: AdapterContext) -> dict[str, object]:
        """Validate the pure adapter-to-workflow argument contract.

        This intentionally constructs no command through a shell and starts no
        workflow.  It is shared by plan/doctor and run so a missing required
        manifest artifact cannot appear execution-ready until ``run --dry-run``.
        """
        arguments = self.execute_arguments(context)
        flags = arguments[::2]
        return {
            "status": "PASS",
            "operation": "execution_preflight",
            "argument_flags": flags,
            "argument_count": len(arguments),
        }

    def collect_provenance(self, context: AdapterContext) -> dict[str, object]:
        return {
            "adapter_id": self.descriptor.adapter_id,
            "adapter_api_version": self.descriptor.adapter_api_version,
            "descriptor_sha256": self.descriptor.sha256,
            "execution_path": "PRODUCTION_BUILTIN_ADAPTER",
            "operations": list(self.descriptor.operations),
            "parameter_semantics": list(self.descriptor.parameter_semantics),
            "model_requirements": list(self.descriptor.model_requirements),
            "cacheable_steps": list(self.descriptor.cacheable_steps),
            "recovery_semantics": self.descriptor.recovery_semantics,
            "scientific_disclosure_source": self.descriptor.scientific_disclosure_source,
            "scientific_status_promotion": "PROHIBITED",
        }

    def assess_output(self, *, output_contract: str, output: dict[str, object]) -> dict[str, object]:
        """Assess already-canonicalized output; never parse third-party raw files."""
        status = str(output.get("output_contract_status", "NOT_ASSESSED"))
        outputs = output.get("outputs")
        available = isinstance(outputs, list) and bool(outputs)
        if output_contract.startswith("reference_free_components"):
            axis = "anonymous_component_axis"
            reason = "anonymous components are not implicitly mapped to named cell proportions"
        else:
            axis = "named_cell_axis"
            reason = "canonical named cell-proportion output required"
        assessment = {
            "operation": "parse_output_and_validate_output",
            "status": "CONFORMANT" if status == "CONFORMANT" and available else ("MISSING" if not available else status),
            "output_contract": output_contract,
            "axis_semantics": axis,
            "reason": reason,
            "canonicalizer_authority": "ESTABLISHED_RUNNER",
        }
        if self.descriptor.tool == "EMeth":
            variants = output.get("variants")
            if isinstance(variants, dict):
                assessment["variants"] = {
                    name: {
                        "execution_status": item.get("execution_status"),
                        "output_contract_status": item.get("output_contract_status"),
                    }
                    for name, item in variants.items()
                    if isinstance(item, dict)
                }
        return assessment


def built_in_registry() -> dict[str, BuiltInAdapter]:
    """Load only packaged static descriptors; no plugin discovery or import hooks."""
    adapters: dict[str, BuiltInAdapter] = {}
    for descriptor in load_adapter_descriptors().values():
        if descriptor.tool not in MIGRATED_TOOLS:
            raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: non-pilot tool {descriptor.tool}")
        if descriptor.tool in adapters:
            raise DeMethFlowError(f"ADAPTER_DESCRIPTOR_INVALID: duplicate built-in adapter for {descriptor.tool}")
        adapters[descriptor.tool] = BuiltInAdapter(descriptor)
    if set(adapters) != set(MIGRATED_TOOLS):
        raise DeMethFlowError("ADAPTER_DESCRIPTOR_INVALID: built-in pilot registry is incomplete")
    return adapters


def adapter_for_tool(tool: str) -> BuiltInAdapter | None:
    return built_in_registry().get(tool)


def adapter_plan_record(
    *,
    bundle: ReferenceBundle,
    capability: ToolCapability,
    tool: str,
    accelerator: str,
    execution_parameters: dict[str, object] | None = None,
    external_runtime: Path | None = None,
) -> dict[str, object]:
    adapter = adapter_for_tool(tool)
    if adapter is None:
        return {"status": "NOT_MIGRATED_COMPATIBILITY_PATH", "reason": "established workflow remains execution authority"}
    context = AdapterContext(
        bundle,
        capability,
        tool,
        accelerator,
        execution_parameters or {},
        external_runtime,
    )
    try:
        binding = adapter.execution_preflight(context)
    except DeMethFlowError as exc:
        return {
            "status": "FAIL",
            "reason": str(exc),
            "adapter_id": adapter.descriptor.adapter_id,
            "operation": "execution_preflight",
        }
    return {
        **binding,
        **adapter.collect_provenance(context),
        "status": "READY_FOR_PRODUCTION_ADAPTER_EXECUTION",
    }
