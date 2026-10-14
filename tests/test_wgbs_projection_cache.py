from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from demethflow_core.errors import DeMethFlowError
from demethflow_core.util import sha256_file
from demethflow_core.wgbs_projection import (
    COORDINATE_SYSTEM,
    PROJECTION_IMPLEMENTATION_VERSION,
    PROJECTION_SCHEMA,
    RUN_SCHEMA,
    _cached_projection,
    _projection_cache_identity,
)


class _Bundle:
    contract = "wgbs_derived_450k_cpg_v1"
    selector = "fixture.wgbs-derived@1.0.0"

    @staticmethod
    def content_digest() -> str:
        return "f" * 64


def _report() -> dict:
    return {
        "target_platform": "450k",
        "genome_build": "hg38",
        "coordinate_system": COORDINATE_SYSTEM,
        "automatic_coordinate_shift": False,
        "liftover": False,
        "missing_cpg_imputation": False,
        "mapping": {"sha256": "a" * 64},
        "inputs": [{"sample_id": "样本 one", "input_sha256": "b" * 64}],
    }


class WgbsProjectionCacheTests(unittest.TestCase):
    def _cached_payload(self, root: Path, report: dict) -> dict:
        matrix = root / "cohort" / "projected_matrix.csv"
        matrix.parent.mkdir(parents=True)
        matrix.write_text("CpG,sample\ncg00000029,0.5\n", encoding="utf-8")
        staged = root / "nextflow_inputs"
        staged.mkdir()
        (staged / "cohort.csv").write_bytes(matrix.read_bytes())
        return {
            "schema": RUN_SCHEMA,
            "reference": _Bundle.selector,
            "reference_digest": _Bundle.content_digest(),
            "projection_implementation_version": PROJECTION_IMPLEMENTATION_VERSION,
            "projection_cache_identity": report["projection_cache_identity"],
            "inputs": report["inputs"],
            "projected_matrix_sha256": sha256_file(matrix),
            "cache_reused": False,
        }

    def test_resume_requires_complete_projection_semantics_identity(self):
        report = _report()
        report["projection_cache_identity"] = _projection_cache_identity(_Bundle(), report, ("Houseman", "EDec"))
        with tempfile.TemporaryDirectory(prefix="methunmix-projection-cache-") as temporary:
            root = Path(temporary)
            (root / "projection_qc.json").write_text(
                json.dumps(self._cached_payload(root, report)), encoding="utf-8"
            )
            cached = _cached_projection(_Bundle(), report, root, True)
            assert cached is not None
            self.assertTrue(cached["cache_reused"])

            changed = _report()
            changed["projection_cache_identity"] = _projection_cache_identity(_Bundle(), changed, ("Houseman",))
            with self.assertRaisesRegex(DeMethFlowError, "provenance mismatch"):
                _cached_projection(_Bundle(), changed, root, True)

    def test_identity_binds_mapping_and_implementation_contract(self):
        report = _report()
        base = _projection_cache_identity(_Bundle(), report, ("Houseman",))
        report["mapping"]["sha256"] = "c" * 64
        self.assertNotEqual(base, _projection_cache_identity(_Bundle(), report, ("Houseman",)))
        self.assertEqual(PROJECTION_SCHEMA, "demethflow-wgbs-derived-array-projection-v1")


if __name__ == "__main__":
    unittest.main()
