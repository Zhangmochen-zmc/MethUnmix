from __future__ import annotations

import ast
import unittest

from demethflow_core import PROJECT_ROOT


THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

ARRAY_PYTHON_POST_PROCESSES = (
    "POST_PROCESS_ARIC",
    "POST_PROCESS_MENET",
    "POST_PROCESS_METHATLAS",
    "POST_PROCESS_CIBERSORT",
    "POST_PROCESS_EDEC",
    "POST_PROCESS_EMETH",
    "POST_PROCESS_EPIDISH",
    "POST_PROCESS_EPISCORE",
    "POST_PROCESS_HOUSEMAN",
    "POST_PROCESS_REFFREE",
    "POST_PROCESS_PRMETH",
    "POST_PROCESS_TSISAL",
    "MERGE_TOOL_RESULTS",
)

SECONDARY_RUN_PROCESSES = (
    "RUN_EpiDISH",
    "RUN_Houseman",
    "RUN_RefFreeEWAS",
)

NATIVE_NUMERICAL_RUN_PROCESSES = (
    "RUN_CELFIE",
    "RUN_MENET",
    "RUN_METDECODE",
)

NATIVE_STANDARD_PYTHON_PROCESSES = (
    "PRE_MENET",
    "RUN_MENET",
    "POST_PROCESS_MENET",
    "PRE_METDECODE_ATLAS",
    "PRE_METDECODE_STEP2",
    "RUN_METDECODE",
    "POST_PROCESS_METDECODE",
    "PRE_CELFIE_ATLAS",
    "PRE_CELFIE_STEP1",
    "PRE_CELFIE_STEP2",
    "RUN_CELFIE",
    "POST_PROCESS_CELFIE",
    "POST_PROCESS_UXM",
    "MERGE_TOOL_RESULTS",
)


def process_block(workflow: str, name: str) -> str:
    start = workflow.index(f"process {name} {{")
    next_process = workflow.find("\nprocess ", start + 1)
    return workflow[start:] if next_process < 0 else workflow[start:next_process]


class ResourceContractTests(unittest.TestCase):
    def test_native_wgbs_numerical_backends_propagate_task_cpus_inside_container(self):
        workflow = (PROJECT_ROOT / "deconvolution" / "wgbs_main.nf").read_text(encoding="utf-8")
        preamble = workflow[workflow.index("def native_python_thread_preamble"):]
        for variable in THREAD_VARIABLES:
            self.assertIn(f"export {variable}='${{cpus}}'", preamble)
        self.assertIn("export METHUNMIX_TASK_CPUS='${cpus}'", preamble)
        for process_name in NATIVE_STANDARD_PYTHON_PROCESSES:
            block = process_block(workflow, process_name)
            with self.subTest(process=process_name):
                self.assertIn("native_python_thread_preamble(task.cpus)", block)

    def test_native_wgbs_single_cpu_mapping_steps_do_not_use_concurrent_pipelines(self):
        for script_name in ("celfie_step1.sh", "metdecode_step1.sh"):
            source = (
                PROJECT_ROOT / "deconvolution" / "wgbs_scripts" / script_name
            ).read_text(encoding="utf-8")
            with self.subTest(script=script_name):
                self.assertNotRegex(source, r"(?m)^\s*\|\s*(?:bedtools|cut)\b")
                self.assertIn("sorted_input.bed", source)
        metdecode = (
            PROJECT_ROOT / "deconvolution" / "wgbs_scripts" / "metdecode_step1.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("sort --parallel=1", metdecode)

    def test_native_wgbs_uxm_contract_remains_task_scoped(self):
        workflow = (PROJECT_ROOT / "deconvolution" / "wgbs_main.nf").read_text(encoding="utf-8")
        block = process_block(workflow, "RUN_UXM")
        self.assertIn("-@ ${task.cpus}", block)
        self.assertNotIn("native_python_thread_preamble", block)

    def test_secondary_run_backends_propagate_task_cpus_on_every_public_array_route(self):
        for workflow_name in ("450k_main.nf", "epic_main.nf", "wgbs_epic_main.nf"):
            workflow = (PROJECT_ROOT / "deconvolution" / workflow_name).read_text(encoding="utf-8")
            for process_name in SECONDARY_RUN_PROCESSES:
                block = process_block(workflow, process_name)
                with self.subTest(workflow=workflow_name, process=process_name):
                    for variable in THREAD_VARIABLES:
                        self.assertIn(f"export {variable}='${{task.cpus}}'", block)

    def test_secondary_run_backends_do_not_auto_detect_host_cores(self):
        forbidden = ("detectCores", "cpu_count", "Pool()", "Pool( )")
        for workflow_name in ("450k_main.nf", "epic_main.nf", "wgbs_epic_main.nf"):
            workflow = (PROJECT_ROOT / "deconvolution" / workflow_name).read_text(encoding="utf-8")
            for process_name in SECONDARY_RUN_PROCESSES:
                block = process_block(workflow, process_name)
                with self.subTest(workflow=workflow_name, process=process_name):
                    for token in forbidden:
                        self.assertNotIn(token, block)

    def test_resource_sensitive_array_processes_propagate_task_cpus(self):
        for workflow_name in ("450k_main.nf", "epic_main.nf"):
            workflow = (PROJECT_ROOT / "deconvolution" / workflow_name).read_text(encoding="utf-8")
            for process_name in ("RUN_ARIC", "RUN_EMeth", "RUN_EpiSCORE", "RUN_PRmeth"):
                block = process_block(workflow, process_name)
                with self.subTest(workflow=workflow_name, process=process_name):
                    for variable in THREAD_VARIABLES:
                        self.assertIn(f"export {variable}='${{task.cpus}}'", block)

            methatlas = process_block(workflow, "RUN_MethAtlas")
            with self.subTest(workflow=workflow_name, process="RUN_MethAtlas"):
                for variable in THREAD_VARIABLES:
                    self.assertIn(f"export {variable}=1", methatlas)
                self.assertIn("--workers '${task.cpus}'", methatlas)

    def test_methatlas_pool_is_explicitly_bounded(self):
        source_path = PROJECT_ROOT / "deconvolution" / "bin" / "MethAtlas.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        pool_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Pool"
        ]
        self.assertEqual(len(pool_calls), 1)
        self.assertTrue(pool_calls[0].keywords)
        self.assertEqual(pool_calls[0].keywords[0].arg, "processes")
        self.assertNotIn("detectCores", source)
        self.assertNotIn("os.cpu_count", source)

    def test_methatlas_wrapper_requires_and_forwards_workers(self):
        wrapper = (PROJECT_ROOT / "deconvolution" / "bin" / "methatlas_decon.py").read_text(encoding="utf-8")
        self.assertIn("parser.add_argument('--workers', required=True, type=int", wrapper)
        self.assertIn('"--workers", str(args.workers)', wrapper)

    def test_array_python_postprocessing_propagates_task_cpus(self):
        for workflow_name in ("450k_main.nf", "epic_main.nf"):
            workflow = (PROJECT_ROOT / "deconvolution" / workflow_name).read_text(encoding="utf-8")
            preamble = workflow[workflow.index("def python_thread_preamble"):]
            for variable in THREAD_VARIABLES:
                self.assertIn(f"export {variable}='${{cpus}}'", preamble)
            for process_name in ARRAY_PYTHON_POST_PROCESSES:
                block = process_block(workflow, process_name)
                with self.subTest(workflow=workflow_name, process=process_name):
                    self.assertIn("${python_thread_preamble(task.cpus)}", block)


if __name__ == "__main__":
    unittest.main()
