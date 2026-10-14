#!/usr/bin/env bash
# Capture diagnostics emitted by an external process without interpreting or
# changing its arguments, stdout, stderr, or exit status.  This wrapper is
# intentionally outside algorithm/runtime assets: it can only observe text
# that the invoked process actually writes to stderr.
set -uo pipefail

usage() {
    echo "usage: $0 --stage STAGE --output DIRECTORY -- COMMAND [ARG ...]" >&2
    exit 64
}

stage=""
output=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stage) [[ $# -ge 2 ]] || usage; stage="$2"; shift 2 ;;
        --output) [[ $# -ge 2 ]] || usage; output="$2"; shift 2 ;;
        --) shift; break ;;
        *) usage ;;
    esac
done
[[ -n "$stage" && -n "$output" && $# -gt 0 ]] || usage
[[ "$stage" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "invalid diagnostic stage" >&2; exit 64; }

diagnostic_setup_failed() {
    # Do not start the algorithm if its diagnostics destination cannot be
    # established.  Otherwise a successful algorithm exit could be reported
    # without the promised terminal diagnostic record.
    printf 'external diagnostics setup failed before wrapped process launch: %s\n' "$1" >&2
    exit 73
}

if [[ -L "$output" ]]; then
    diagnostic_setup_failed "output path is a symlink"
fi
mkdir -p "$output" || diagnostic_setup_failed "cannot create output directory"
raw_stderr="$output/stderr.raw.log"
warning_stderr="$output/warnings.raw.log"
events="$output/events.tsv"
metadata="$output/capture-metadata.json"
fifo="${output}/.stderr-capture.$$"

# Prove all mandatory files are writable before the wrapped process is allowed
# to run.  The preflight files deliberately remain empty until their events are
# recorded below.
for diagnostic_file in "$raw_stderr" "$warning_stderr" "$events"; do
    : > "$diagnostic_file" || diagnostic_setup_failed "cannot initialize $(basename "$diagnostic_file")"
done

timestamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
event() { printf '%s\t%s\t%s\t%s\n' "$(timestamp)" "$stage" "$1" "$2" >> "$events"; }

write_metadata() {
    local code="$1" terminal="$2"
    cat > "$metadata" <<EOF
{
  "schema": "methunmix-external-diagnostics-v1",
  "stage": "$stage",
  "terminal_state": "$terminal",
  "process_exit_code": $code,
  "stderr_raw": "stderr.raw.log",
  "warning_lines": "warnings.raw.log",
  "events": "events.tsv",
  "coverage": "captures only text emitted by the wrapped process; it cannot recover warnings withheld by that process or by an external runtime"
}
EOF
}

cleanup() { rm -f "$fifo"; }
on_signal() {
    event "INTERRUPTED" "wrapper received signal"
    write_metadata 143 "INTERRUPTED"
    exit 143
}
trap cleanup EXIT
trap on_signal HUP INT TERM

capture_stderr() {
    local line=""
    local warning_block=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        printf '%s\n' "$line" >> "$raw_stderr"
        printf '%s\n' "$line" >&2
        # Base R commonly writes a "Warning message:" header followed by the
        # actual warning text on subsequent lines. Preserve that block, but do
        # not absorb a later fatal error into the warning record.
        if [[ "$line" =~ [Ww]arning[[:space:]]message ]]; then
            warning_block=1
            printf '%s\n' "$line" >> "$warning_stderr"
            event "WARNING" "stderr line captured"
        elif [[ $warning_block -eq 1 && ! "$line" =~ ^Error: && ! "$line" =~ ^Execution[[:space:]]halted ]]; then
            printf '%s\n' "$line" >> "$warning_stderr"
            event "WARNING" "stderr continuation captured"
        elif [[ "$line" =~ [Ww]arning ]]; then
            warning_block=0
            printf '%s\n' "$line" >> "$warning_stderr"
            event "WARNING" "stderr line captured"
        else
            warning_block=0
            event "STDERR" "stderr line captured"
        fi
    done
}

rm -f "$fifo"
mkfifo "$fifo" || diagnostic_setup_failed "cannot create stderr capture fifo"
event "STARTED" "external process launched"
capture_stderr < "$fifo" &
capture_pid=$!

set +e
"$@" 2> "$fifo"
code=$?
wait "$capture_pid"
capture_code=$?
set -e

if [[ $capture_code -ne 0 ]]; then
    # A post-launch capture failure is explicit and fail-closed.  Preserve the
    # child status in stderr; exit 74 means no complete diagnostics record can
    # be trusted, rather than silently treating the model output as complete.
    printf 'external diagnostics capture failed after wrapped process exit=%s; refusing to report a complete diagnostic record\n' "$code" >&2
    exit 74
fi

if [[ $code -eq 0 ]]; then
    event "COMPLETED" "external process exited zero"
    write_metadata "$code" "COMPLETED"
else
    event "ERROR" "external process exited nonzero"
    write_metadata "$code" "ERROR"
fi
exit "$code"
