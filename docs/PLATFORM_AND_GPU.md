# Platform and GPU declaration

The Python control plane and offline asset/reference-management commands may be
used on platforms supported by Python. Starting reference-construction or
deconvolution workflows is explicitly limited to Linux x86_64; other hosts get
an early `WORKFLOW_PLATFORM_UNSUPPORTED` error. Dry-run remains available for
inspection and planning.

Reference bundles and data-only tool/build modules are exported with the
`noarch` module platform and can be imported on non-Linux hosts. Runtime/SIF
and build-runtime modules are tagged `linux-x86_64` and are rejected on other
hosts. A module's architecture tag controls installation eligibility; it does
not broaden the workflow execution platform or imply that a noarch data module
has been scientifically validated on another host.

H100 validation proves only the recorded H100 driver/CUDA/VRAM/runtime
combination. Other NVIDIA GPUs are `UNVALIDATED` unless separately tested. A
GPU profile cannot be marked READY from a CPU run or from
`apptainer --version` alone.
