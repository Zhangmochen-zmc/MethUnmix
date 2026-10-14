"""Non-executing production-adapter tests for the bounded SDK migration pair.

They exercise the established command construction path and never invoke
Nextflow, a container, or an algorithm.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from demethflow_core.adapter_runtime import MIGRATED_TOOLS
from demethflow_core.manifest import ToolCapability
from demethflow_core.runner import DeconRequest, _selected_tool_output, prepare_decon
from demethflow_core.runtime import DoctorResult


class _Bundle:
    contract = "array_450k_cpg"
    payload: dict[str, object] = {}
    cell_types: tuple[str, ...] = ()

    def __init__(self, tools: dict[str, ToolCapability]) -> None:
        self.tools = tools

    def artifact(self, tool: str, name: str) -> Path:
        return Path("/immutable-reference") / tool / name


def _result(tool: str, artifacts: dict[str, str]) -> DoctorResult:
    bundle = _Bundle({tool: ToolCapability(name=tool, status="RELEASED_UNVALIDATED", artifacts=artifacts, runtime_modules=())})
    return DoctorResult(
        bundle=bundle, selected_tools=[tool], nextflow_command=["nextflow"],
        container_engine="apptainer", accelerator="cpu",
    )


class AdapterMigrationCharacterizationTests(unittest.TestCase):
    def _prepared(self, tool: str, artifacts: dict[str, str]):
        with tempfile.TemporaryDirectory(prefix="methunmix-adapter-characterization-") as temporary:
            root = Path(temporary)
            input_path = root / "input.csv"; input_path.write_text("CpG,S1\ncg1,0.5\n", encoding="utf-8")
            request = DeconRequest(
                input_path=input_path, outdir=root / "out", platform="450k", scenario="brain",
                reference="fixture@1", tools=tool, runtime="container", container_engine="apptainer",
                accelerator="cpu", analysis_contract="array_450k_cpg", random_seed=20260826,
            )
            with patch("demethflow_core.runner.doctor", return_value=_result(tool, artifacts)):
                result, argv, _ = prepare_decon(request)
        return result, argv

    def _argv(self, tool: str, artifacts: dict[str, str]) -> list[str]:
        _result_value, argv = self._prepared(tool, artifacts)
        return argv

    def test_stage7_bounded_registry_includes_the_two_new_array_adapters(self):
        self.assertEqual(
            MIGRATED_TOOLS,
            frozenset({"Houseman", "MeDeCom", "CelFiE", "EDec", "EMeth", "EpiDISH", "EpiSCORE"}),
        )
        self.assertIn("EDec", MIGRATED_TOOLS)
        self.assertIn("EpiDISH", MIGRATED_TOOLS)
        self.assertIn("EMeth", MIGRATED_TOOLS)
        self.assertIn("EpiSCORE", MIGRATED_TOOLS)

    def test_edec_prefers_materialized_tref_and_keeps_markers(self):
        result, argv = self._prepared("EDec", {"tref": "ignored", "markers": "ignored"})
        self.assertIn("--ref_edec_tref", argv)
        self.assertIn("/immutable-reference/EDec/tref", argv)
        self.assertIn("--ref_edec_mark", argv)
        self.assertIn("/immutable-reference/EDec/markers", argv)
        self.assertNotIn("--ref_edec_data", argv)
        self.assertNotIn("--ref_edec_meta", argv)
        self.assertEqual(result.adapter_execution["EDec"]["adapter_id"], "edec-array-v1")

    def test_edec_uses_source_and_metadata_only_when_tref_is_absent(self):
        _result, argv = self._prepared(
            "EDec", {"reference_data": "ignored", "metadata": "ignored", "markers": "ignored"}
        )
        expected = [
            "--ref_edec_data", "/immutable-reference/EDec/reference_data",
            "--ref_edec_meta", "/immutable-reference/EDec/metadata",
            "--ref_edec_mark", "/immutable-reference/EDec/markers",
        ]
        start = argv.index("--ref_edec_data")
        self.assertEqual(argv[start:start + len(expected)], expected)

    def test_edec_missing_required_conditional_reference_branch_fails_closed(self):
        with self.assertRaisesRegex(Exception, "EDec requires tref or reference_data and metadata"):
            self._argv("EDec", {"markers": "ignored", "reference_data": "ignored"})

    def test_epidish_keeps_existing_reference_and_mode_parameters(self):
        result, argv = self._prepared("EpiDISH", {"reference": "ignored"})
        self.assertIn("--ref_epidish", argv)
        self.assertIn("/immutable-reference/EpiDISH/reference", argv)
        self.assertIn("--epidish_reference_mode", argv)
        self.assertEqual(argv[argv.index("--epidish_reference_mode") + 1], "bundle_artifact")
        self.assertEqual(result.adapter_execution["EpiDISH"]["adapter_id"], "epidish-array-v1")

    def test_epidish_is_declared_as_a_three_variant_output_contract(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-epidish-variants-") as temporary:
            root = Path(temporary); canonical = root / "canonical"; canonical.mkdir()
            rpc = canonical / "EpiDISH_RPC_results.csv"; rpc.write_text("sample,a\nS1,1\n", encoding="utf-8")
            partial = _selected_tool_output(
                outdir=root, tool="EpiDISH", output_contract="cell_proportions_v1",
                canonical=[rpc], reference_free=[], run_status="SUCCEEDED",
            )
            self.assertEqual(set(partial["missing_variants"]), {"CBS", "CP"})
            for variant in ("CBS", "CP"):
                (canonical / f"EpiDISH_{variant}_results.csv").write_text("sample,a\nS1,1\n", encoding="utf-8")
            complete = _selected_tool_output(
                outdir=root, tool="EpiDISH", output_contract="cell_proportions_v1",
                canonical=list(canonical.glob("*.csv")), reference_free=[], run_status="SUCCEEDED",
            )
            self.assertEqual(complete["output_contract_status"], "CONFORMANT")
            self.assertEqual(set(complete["variants"]), {"RPC", "CBS", "CP"})

    def test_emeth_uses_established_reference_and_source_arguments(self):
        result, argv = self._prepared("EMeth", {"reference": "ignored", "source": "ignored"})
        self.assertIn("--ref_emeth", argv)
        self.assertIn("/immutable-reference/EMeth/reference", argv)
        self.assertIn("--src_emeth", argv)
        self.assertIn("/immutable-reference/EMeth/source", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "EMeth")
        self.assertEqual(argv[argv.index("--random_seed") + 1], "20260826")
        self.assertNotIn("-resume", argv)
        provenance = result.adapter_execution["EMeth"]
        self.assertEqual(provenance["adapter_id"], "emeth-array-v1")
        self.assertEqual(provenance["execution_path"], "PRODUCTION_BUILTIN_ADAPTER")
        self.assertRegex(str(provenance["descriptor_sha256"]), r"^[0-9a-f]{64}$")

    def test_episcore_prefers_the_declared_precomputed_gene_reference(self):
        result, argv = self._prepared("EpiSCORE", {"gene_reference": "ignored"})
        self.assertNotIn("--ref_episcore", argv)
        self.assertIn("--ref_episcore_gene", argv)
        self.assertIn("/immutable-reference/EpiSCORE/gene_reference", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "EpiSCORE")
        self.assertEqual(argv[argv.index("--random_seed") + 1], "20260826")
        provenance = result.adapter_execution["EpiSCORE"]
        self.assertEqual(provenance["adapter_id"], "episcore-array-v1")
        self.assertEqual(provenance["execution_path"], "PRODUCTION_BUILTIN_ADAPTER")
        self.assertRegex(str(provenance["descriptor_sha256"]), r"^[0-9a-f]{64}$")

    def test_episcore_uses_raw_reference_only_as_workflow_preparation_fallback(self):
        _result, argv = self._prepared("EpiSCORE", {"reference": "ignored"})
        self.assertIn("--ref_episcore", argv)
        self.assertIn("/immutable-reference/EpiSCORE/reference", argv)
        self.assertNotIn("--ref_episcore_gene", argv)

    def test_emeth_variants_are_a_single_tool_contract_not_duplicate_tools(self):
        with tempfile.TemporaryDirectory(prefix="methunmix-emeth-variants-") as temporary:
            root = Path(temporary); canonical = root / "canonical"; canonical.mkdir()
            normal = canonical / "EMeth_Normal_results.csv"; normal.write_text("sample,a\nS1,1\n", encoding="utf-8")
            partial = _selected_tool_output(
                outdir=root, tool="EMeth", output_contract="cell_proportions_v1",
                canonical=[normal], reference_free=[], run_status="SUCCEEDED",
            )
            self.assertEqual(partial["output_contract_status"], "MISSING")
            self.assertEqual(set(partial["missing_variants"]), {"Laplace"})
            laplace = canonical / "EMeth_Laplace_results.csv"; laplace.write_text("sample,a\nS1,1\n", encoding="utf-8")
            complete = _selected_tool_output(
                outdir=root, tool="EMeth", output_contract="cell_proportions_v1",
                canonical=[normal, laplace], reference_free=[], run_status="SUCCEEDED",
            )
            self.assertEqual(complete["output_contract_status"], "CONFORMANT")
            self.assertEqual(set(complete["variants"]), {"Normal", "Laplace"})
