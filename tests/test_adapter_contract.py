from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from demethflow_core.adapter_contract import load_adapter_descriptors, registry_report, validate_adapter_binding
from demethflow_core.adapter_runtime import AdapterContext, BuiltInAdapter, adapter_for_tool, built_in_registry
from demethflow_core.cli import main
from demethflow_core.errors import DeMethFlowError
from demethflow_core.manifest import ToolCapability


class AdapterContractTests(unittest.TestCase):
    def test_emeth_descriptor_is_packaged_and_validate_manifest_binding(self):
        descriptors = load_adapter_descriptors()
        self.assertEqual(
            {item.tool for item in descriptors.values()},
            {"Houseman", "MeDeCom", "CelFiE", "EDec", "EMeth", "EpiDISH", "EpiSCORE"},
        )
        houseman = next(item for item in descriptors.values() if item.tool == "Houseman")
        binding = validate_adapter_binding(
            houseman,
            analysis_contract="array_450k_cpg",
            capability=ToolCapability(
                name="Houseman", status="READY", artifacts={}, runtime_modules=("array-common",),
            ),
        )
        self.assertEqual(binding["status"], "PASS")
        incompatible = validate_adapter_binding(
            houseman,
            analysis_contract="wgbs_native_hg38",
            capability=ToolCapability(
                name="Houseman", status="READY", artifacts={}, runtime_modules=("array-common",),
            ),
        )
        self.assertEqual(incompatible["status"], "FAIL")
        emeth = next(item for item in descriptors.values() if item.tool == "EMeth")
        self.assertEqual(
            validate_adapter_binding(
                emeth,
                analysis_contract="array_450k_cpg",
                capability=ToolCapability(
                    name="EMeth", status="READY", artifacts={"reference": "x", "source": "y"},
                    runtime_modules=("array-common", "EMeth"),
                ),
            )["status"],
            "PASS",
        )
        episcore = next(item for item in descriptors.values() if item.tool == "EpiSCORE")
        self.assertEqual(
            validate_adapter_binding(
                episcore,
                analysis_contract="array_450k_cpg",
                capability=ToolCapability(
                    name="EpiSCORE", status="READY",
                    artifacts={"reference": "x", "gene_reference": "y"},
                    runtime_modules=("array-common", "EpiSCORE"),
                ),
            )["status"],
            "PASS",
        )
        edec = next(item for item in descriptors.values() if item.tool == "EDec")
        self.assertEqual(
            validate_adapter_binding(
                edec,
                analysis_contract="array_450k_cpg",
                capability=ToolCapability(
                    name="EDec", status="READY", artifacts={"tref": "x", "markers": "y"},
                    runtime_modules=("array-common", "EDec"),
                ),
            )["status"],
            "PASS",
        )
        epidish = next(item for item in descriptors.values() if item.tool == "EpiDISH")
        self.assertEqual(
            validate_adapter_binding(
                epidish,
                analysis_contract="array_450k_cpg",
                capability=ToolCapability(
                    name="EpiDISH", status="READY", artifacts={"reference": "x"},
                    runtime_modules=("array-common", "EpiDISH"),
                ),
            )["status"],
            "PASS",
        )
        for descriptor, capability in (
            (edec, ToolCapability(name="EDec", status="READY", artifacts={"tref": "x", "markers": "y"}, runtime_modules=("array-common", "EDec"))),
            (epidish, ToolCapability(name="EpiDISH", status="READY", artifacts={"reference": "x"}, runtime_modules=("array-common", "EpiDISH"))),
        ):
            blocked = validate_adapter_binding(
                descriptor,
                analysis_contract="wgbs_native_hg38",
                capability=capability,
            )
            self.assertEqual(blocked["status"], "FAIL")
            self.assertTrue(any("analysis contract" in problem for problem in blocked["problems"]))

    def test_invalid_descriptor_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-adapter-") as temporary:
            root = Path(temporary)
            (root / "invalid.json").write_text(json.dumps({"schema": "methunmix-adapter-descriptor-v1"}), encoding="utf-8")
            with self.assertRaisesRegex(DeMethFlowError, "ADAPTER_DESCRIPTOR_INVALID"):
                load_adapter_descriptors(root)

    def test_cli_lists_and_validates_registry(self):
        report = registry_report()
        self.assertEqual(report["status"], "PASS")
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["adapter", "validate", "--json"]), 0)
        self.assertIn("houseman-array-v1", stdout.getvalue())

    def test_payload_allowlist_permits_only_reviewed_adapter_descriptor_depth(self):
        from scripts.audit_conda_payload import allowed_wheel_member

        self.assertTrue(allowed_wheel_member("methunmix_assets/capabilities/adapters/houseman-array-v1.json"))
        self.assertFalse(allowed_wheel_member("methunmix_assets/capabilities/adapters/nested/unreviewed.json"))
        self.assertFalse(allowed_wheel_member("methunmix_assets/capabilities/adapters/descriptor.py"))

    def test_contribution_template_keeps_runtime_and_science_boundaries_explicit(self):
        template = (Path(__file__).resolve().parents[1] / "docs" / "ADAPTER_SDK_TEMPLATE.md").read_text(encoding="utf-8")
        for required in ("validate_input", "collect_provenance", "USER_SUPPLIED_SUPPORTED", "never promotes scientific status"):
            self.assertIn(required, template)
        self.assertIn("does not enable dynamic plugin discovery", template)

    def test_contribution_template_descriptor_is_schema_valid_in_temporary_directory(self):
        """The documented skeleton is accepted as a descriptor, without registering a plugin."""
        template = (Path(__file__).resolve().parents[1] / "docs" / "ADAPTER_SDK_TEMPLATE.md").read_text(encoding="utf-8")
        skeleton = template.split("```json\n", 1)[1].split("\n```", 1)[0]
        with tempfile.TemporaryDirectory(prefix="methunmix-adapter-template-") as temporary:
            descriptor = Path(temporary) / "example.json"
            descriptor.write_text(skeleton, encoding="utf-8")
            loaded = load_adapter_descriptors(Path(temporary))
        self.assertEqual(set(loaded), {"example-tool-v1"})
        self.assertEqual(loaded["example-tool-v1"].tool, "ExampleTool")
        # Parsing a temporary descriptor is deliberately not dynamic registration.
        self.assertIsNone(adapter_for_tool("ExampleTool"))

    def test_built_in_registry_is_closed_and_exposes_required_runtime_semantics(self):
        registry = built_in_registry()
        self.assertEqual(
            set(registry), {"Houseman", "MeDeCom", "CelFiE", "EDec", "EMeth", "EpiDISH", "EpiSCORE"}
        )
        self.assertEqual(registry["Houseman"].descriptor.adapter_api_version, "1")
        self.assertIn("no_adapter_output_cache", registry["CelFiE"].descriptor.recovery_semantics)
        self.assertIsNone(adapter_for_tool("UnreviewedPlugin"))

    def test_adapter_rejects_profile_conflict_before_argument_construction(self):
        class Bundle:
            contract = "array_450k_cpg"
            def artifact(self, tool, name):  # pragma: no cover - must not be reached
                raise AssertionError("artifact construction must not begin")
        cap = ToolCapability(name="Houseman", status="READY", artifacts={}, runtime_modules=())
        context = AdapterContext(Bundle(), cap, "Houseman", "tpu", {})
        with self.assertRaisesRegex(DeMethFlowError, "ADAPTER_CAPABILITY_CONFLICT"):
            adapter_for_tool("Houseman").execute_arguments(context)

    def test_adapter_argument_and_output_semantics_are_tool_specific(self):
        class Bundle:
            contract = "array_450k_cpg"
            def artifact(self, tool, name):
                return Path(f"/immutable/{tool}/{name}")
        houseman = ToolCapability(name="Houseman", status="READY", artifacts={"cell_reference": "x", "beta_reference": "y"}, runtime_modules=())
        args = adapter_for_tool("Houseman").execute_arguments(AdapterContext(Bundle(), houseman, "Houseman", "cpu", {}))
        self.assertEqual(args, ["--ref_houseman_cell", "/immutable/Houseman/cell_reference", "--ref_houseman_beta", "/immutable/Houseman/beta_reference"])
        medecom = ToolCapability(name="MeDeCom", status="READY", artifacts={}, runtime_modules=(), reference_requirement="none", output_contract="reference_free_components_v1")
        args = adapter_for_tool("MeDeCom").execute_arguments(AdapterContext(Bundle(), medecom, "MeDeCom", "cpu", {"k": 2, "lambda": "0.01", "nfolds": 3}))
        self.assertEqual(args[-2:], ["--medecom_output_contract", "reference_free_components_v1"])
        assessed = adapter_for_tool("MeDeCom").assess_output(output_contract="reference_free_components_v1", output={"output_contract_status": "CONFORMANT", "outputs": [{"path": "x"}]})
        self.assertEqual(assessed["axis_semantics"], "anonymous_component_axis")
        self.assertEqual(assessed["status"], "CONFORMANT")
        self.assertEqual(
            adapter_for_tool("Houseman").assess_output(output_contract="cell_proportions_v1", output={})["status"],
            "MISSING",
        )
        self.assertEqual(
            adapter_for_tool("Houseman").assess_output(
                output_contract="cell_proportions_v1",
                output={"output_contract_status": "INVALID", "outputs": [{"path": "bad.csv"}]},
            )["status"],
            "INVALID",
        )
        emeth = ToolCapability(
            name="EMeth", status="READY", artifacts={"reference": "x", "source": "y"}, runtime_modules=(),
        )
        emeth_args = adapter_for_tool("EMeth").execute_arguments(
            AdapterContext(Bundle(), emeth, "EMeth", "cpu", {})
        )
        self.assertEqual(
            emeth_args,
            ["--ref_emeth", "/immutable/EMeth/reference", "--src_emeth", "/immutable/EMeth/source"],
        )
        with self.assertRaisesRegex(DeMethFlowError, "EMeth requires source"):
            adapter_for_tool("EMeth").execute_arguments(
                AdapterContext(
                    Bundle(), ToolCapability(name="EMeth", status="READY", artifacts={"reference": "x"}, runtime_modules=()),
                    "EMeth", "cpu", {},
                )
            )
        gene_only = ToolCapability(
            name="EpiSCORE", status="READY", artifacts={"gene_reference": "y"}, runtime_modules=(),
        )
        self.assertEqual(
            adapter_for_tool("EpiSCORE").execute_arguments(
                AdapterContext(Bundle(), gene_only, "EpiSCORE", "cpu", {})
            ),
            ["--ref_episcore_gene", "/immutable/EpiSCORE/gene_reference"],
        )
        raw_only = ToolCapability(
            name="EpiSCORE", status="READY", artifacts={"reference": "x"}, runtime_modules=(),
        )
        self.assertEqual(
            adapter_for_tool("EpiSCORE").execute_arguments(
                AdapterContext(Bundle(), raw_only, "EpiSCORE", "cpu", {})
            ),
            ["--ref_episcore", "/immutable/EpiSCORE/reference"],
        )
        with self.assertRaisesRegex(DeMethFlowError, "EpiSCORE requires gene_reference or reference"):
            adapter_for_tool("EpiSCORE").execute_arguments(
                AdapterContext(
                    Bundle(), ToolCapability(name="EpiSCORE", status="READY", artifacts={}, runtime_modules=()),
                    "EpiSCORE", "cpu", {},
                )
            )
        edec_tref = ToolCapability(
            name="EDec", status="READY", artifacts={"tref": "x", "markers": "y"}, runtime_modules=(),
        )
        self.assertEqual(
            adapter_for_tool("EDec").execute_arguments(AdapterContext(Bundle(), edec_tref, "EDec", "cpu", {})),
            ["--ref_edec_tref", "/immutable/EDec/tref", "--ref_edec_mark", "/immutable/EDec/markers"],
        )
        edec_prep = ToolCapability(
            name="EDec", status="READY",
            artifacts={"reference_data": "x", "metadata": "y", "markers": "z"}, runtime_modules=(),
        )
        self.assertEqual(
            adapter_for_tool("EDec").execute_arguments(AdapterContext(Bundle(), edec_prep, "EDec", "cpu", {})),
            [
                "--ref_edec_data", "/immutable/EDec/reference_data",
                "--ref_edec_meta", "/immutable/EDec/metadata",
                "--ref_edec_mark", "/immutable/EDec/markers",
            ],
        )
        with self.assertRaisesRegex(DeMethFlowError, "EDec requires markers"):
            adapter_for_tool("EDec").execute_arguments(
                AdapterContext(
                    Bundle(), ToolCapability(name="EDec", status="READY", artifacts={"tref": "x"}, runtime_modules=()),
                    "EDec", "cpu", {},
                )
            )
        epidish_dynamic = ToolCapability(
            name="EpiDISH", status="READY", artifacts={"reference": "x"}, runtime_modules=(),
        )
        self.assertEqual(
            adapter_for_tool("EpiDISH").execute_arguments(
                AdapterContext(Bundle(), epidish_dynamic, "EpiDISH", "cpu", {})
            ),
            ["--ref_epidish", "/immutable/EpiDISH/reference"],
        )
        epidish_builtin = ToolCapability(
            name="EpiDISH", status="READY", artifacts={}, runtime_modules=(),
            reference_mode="package_builtin", builtin_reference="centDHSbloodDMC.m",
        )
        self.assertEqual(
            adapter_for_tool("EpiDISH").execute_arguments(
                AdapterContext(Bundle(), epidish_builtin, "EpiDISH", "cpu", {})
            ),
            [],
        )

    def test_adapter_api_mismatch_fails_before_any_execution_argument(self):
        adapter = adapter_for_tool("Houseman")
        incompatible = BuiltInAdapter(replace(adapter.descriptor, adapter_api_version="999"))
        class Bundle:
            contract = "array_450k_cpg"
        cap = ToolCapability(name="Houseman", status="READY", artifacts={}, runtime_modules=())
        with self.assertRaisesRegex(DeMethFlowError, "ADAPTER_API_INCOMPATIBLE"):
            incompatible.execute_arguments(AdapterContext(Bundle(), cap, "Houseman", "cpu", {}))
