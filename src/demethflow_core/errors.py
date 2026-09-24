class DeMethFlowError(RuntimeError):
    """User-facing error with no traceback required."""


class ManifestError(DeMethFlowError):
    """A reference or module manifest is invalid."""


class ResolutionError(DeMethFlowError):
    """A requested reference/tool combination cannot be resolved."""


class InstallError(DeMethFlowError):
    """A local module archive cannot be installed safely."""
