"""Core library for the standalone, offline MethUnmix distribution.

The historical ``demethflow_core`` import path is intentionally retained for
2.x compatibility.  Runtime resources are kept in the separate
``methunmix_assets`` package so an installed wheel/Conda package never relies
on a source checkout layout.
"""

from importlib.resources import files
from pathlib import Path

PACKAGE_NAME = "MethUnmix"
CORE_API = "2.0"
REFERENCE_SCHEMA = "1.0"
RUNTIME_API = "1.0"
NEXTFLOW_VERSION = "26.04.4"

try:
    PROJECT_ROOT = Path(files("methunmix_assets"))
except (ModuleNotFoundError, TypeError):
    # Source-tree fallback used by development checkouts.
    PROJECT_ROOT = Path(__file__).resolve().parent.parent


def version() -> str:
    return (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
