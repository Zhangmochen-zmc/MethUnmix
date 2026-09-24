#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DEF="$PROJECT_DIR/containers/deconvolution.def"
IMAGE="$PROJECT_DIR/containers/deconvolution.sif"

command -v apptainer >/dev/null || { echo "ERROR: apptainer is required" >&2; exit 2; }
[[ -f "$DEF" ]] || { echo "ERROR: missing definition: $DEF" >&2; exit 2; }

cd "$PROJECT_DIR/containers"
apptainer build --fakeroot "$IMAGE" "$DEF"
apptainer test "$IMAGE"
apptainer exec "$IMAGE" python3 --version
apptainer exec "$IMAGE" Rscript --version
apptainer exec "$IMAGE" bedtools --version
echo "Built and tested: $IMAGE"
