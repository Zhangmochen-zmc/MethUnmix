"""Non-public compatibility shim for historical Python callers.

The 1.0.0 distribution deliberately does not install a ``demethflow`` console
script. New callers must use the public ``methunmix`` command.
"""

from __future__ import annotations

import os
import sys

from .cli import main as _main

_WARNING_EMITTED = False


def main(argv: list[str] | None = None) -> int:
    global _WARNING_EMITTED
    if not _WARNING_EMITTED and os.environ.get("METHUNMIX_SILENCE_LEGACY_WARNING") != "1":
        print(
            "WARNING: demethflow is not a public MethUnMix command; use methunmix.",
            file=sys.stderr,
        )
        _WARNING_EMITTED = True
    return _main(argv)
