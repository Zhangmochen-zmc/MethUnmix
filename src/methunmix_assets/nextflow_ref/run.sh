#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PARAMS_FILE="${1:-}"

if [[ -z "$PARAMS_FILE" || "$PARAMS_FILE" == '-h' || "$PARAMS_FILE" == '--help' ]]; then
    echo "Usage: ./run.sh <params.yml> [additional Nextflow arguments]" >&2
    exit $([[ -n "$PARAMS_FILE" ]] && echo 0 || echo 2)
fi
shift

[[ -f "$PARAMS_FILE" ]] || { echo "ERROR: params file not found: $PARAMS_FILE" >&2; exit 2; }
command -v nextflow >/dev/null || { echo "ERROR: nextflow is not available" >&2; exit 2; }

PROFILE="${NEXTFLOW_PROFILE:-standard,conda}"
if [[ ",${PROFILE}," == *",slurm,"* ]]; then
    echo "ERROR: Slurm execution is outside the MethUnmix 2.0 support scope; use a local Nextflow profile." >&2
    exit 2
fi
if [[ "$PROFILE" == *apptainer* ]]; then
    command -v apptainer >/dev/null || { echo "ERROR: apptainer is not available" >&2; exit 2; }
    [[ -f "$SCRIPT_DIR/containers/deconvolution.sif" ]] || {
        echo "ERROR: missing release container: $SCRIPT_DIR/containers/deconvolution.sif" >&2
        exit 2
    }
fi

exec nextflow run "$SCRIPT_DIR/main.nf" \
    -profile "$PROFILE" \
    -params-file "$PARAMS_FILE" \
    "$@"
