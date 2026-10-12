#!/usr/bin/env bash
# Source this file from a launcher or batch script. No traps or shell settings
# are replaced. Raven must be on PATH (or set RAVEN_BIN to its executable path).

raven_submit() {
    if [[ $# -lt 1 ]]; then
        printf '%s\n' 'raven_submit: expected SCRIPT [SBATCH_OPTION ...]' >&2
        return 2
    fi
    local raven_script=$1
    shift
    "${RAVEN_BIN:-raven}" report submit --script "$raven_script" -- "$@"
}

raven_run() {
    if [[ $# -lt 1 ]]; then
        printf '%s\n' 'raven_run: expected COMMAND [ARG ...]' >&2
        return 2
    fi
    "${RAVEN_BIN:-raven}" report exec -- "$@"
}

raven_metric() {
    "${RAVEN_BIN:-raven}" report metric "$@"
}

raven_artifact() {
    "${RAVEN_BIN:-raven}" report artifact "$@"
}
