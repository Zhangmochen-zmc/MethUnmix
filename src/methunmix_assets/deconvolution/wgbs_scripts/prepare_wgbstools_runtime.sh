#!/usr/bin/env bash
# Materialise the fixed WGBSTools source shipped in wgbs-common inside one task.
# The current runtime image intentionally remains immutable; this script never
# writes under /opt and never consults the host PATH or a network source.
set -euo pipefail

build=${1:?genome build is required}
case "$build" in hg19|hg38) ;; *) echo "WGBSTOOLS_REFERENCE_MISSING: invalid build $build" >&2; exit 2;; esac

source_root=/opt/wgbs_tools
# WGBSTools' bam2pat vendor implementation builds shell command strings from
# the absolute path of its own genome files.  A Nextflow task may legitimately
# be below a user-selected output directory containing spaces or Unicode
# characters; materialising the mutable runtime there would therefore split
# those paths in the vendor shell commands.  The caller may provide a freshly
# created ASCII-only task scratch parent.  Keep the historical task-directory
# default for callers that do not provide it.
runtime_parent=${DEMETHFLOW_WGBS_RUNTIME_PARENT:-$PWD}
case "$runtime_parent" in
    /tmp/demethflow_wgbstools.*) ;;
    *)
        # Do not accept an arbitrary external location: the runtime is an
        # ephemeral task artifact and must remain in the task directory unless
        # it was created by the controlled /tmp scratch route below.
        runtime_parent=$PWD
        ;;
esac
runtime_root="$runtime_parent/wgbs_tools_runtime"
if [[ ! -d "$source_root/src/python" ]]; then
    echo "BAM_PREPROCESS_FAILED: wgbs-common lacks fixed WGBSTools source" >&2
    exit 2
fi
if [[ ! -d "/opt/wgbs_tools/references/$build" ]]; then
    echo "WGBSTOOLS_REFERENCE_MISSING: selected WGBSTools build is not mounted" >&2
    exit 2
fi
if [[ ! -d "$runtime_root" ]]; then
    cp -a "$source_root" "$runtime_root"
    rm -rf "$runtime_root/references"
    mkdir -p "$runtime_root/references"
    ln -s "/opt/wgbs_tools/references/$build" "$runtime_root/references/$build"
    # bam2pat's region helper resolves the package default before applying its
    # explicit --genome argument.  Keep that internal default build-locked.
    ln -s "$build" "$runtime_root/references/default"
fi

pipeline="$runtime_root/src/pipeline_wgbs"
compile() {
    local target=$1
    shift
    if [[ ! -x "$pipeline/$target" ]]; then
        g++ -O3 -std=c++11 "$@" -o "$pipeline/$target"
    fi
}
compile match_maker "$pipeline/match_maker.cpp" "$pipeline/patter_utils.cpp"
compile patter "$pipeline/main.cpp" "$pipeline/patter_utils.cpp" "$pipeline/patter.cpp" "$pipeline/ont.cpp"
compile add_cpg_counts "$pipeline/add_cpg_counts.cpp" "$pipeline/patter_utils.cpp"
for binary in "$pipeline/match_maker" "$pipeline/patter" "$pipeline/add_cpg_counts"; do
    [[ -x "$binary" ]] || { echo "BAM_PREPROCESS_FAILED: failed to compile ${binary##*/}" >&2; exit 2; }
done
printf '%s\n' "$runtime_root"
