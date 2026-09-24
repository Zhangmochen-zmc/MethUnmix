"""Compatibility entry point for the historical ``demethflow`` command."""

from __future__ import annotations

import os
import sys

from .cli import main as _main

_WARNING_EMITTED = False


def main(argv: list[str] | None = None) -> int:
    global _WARNING_EMITTED
    if not _WARNING_EMITTED and os.environ.get("METHUNMIX_SILENCE_LEGACY_WARNING") != "1":
        print(
            "WARNING: demethflow is a compatibility alias for methunmix and "
            "will be removed no earlier than MethUnmix 3.0.0.",
            file=sys.stderr,
        )
        _WARNING_EMITTED = True
    return _main(argv)
