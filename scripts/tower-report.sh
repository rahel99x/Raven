#!/usr/bin/env bash
# Source this file from a launcher or batch script. No traps or shell settings
# are replaced. Tower must be on PATH (or set TOWER_BIN to its executable path).

tower_submit() {
    if [[ $# -lt 1 ]]; then
        printf '%s\n' 'tower_submit: expected SCRIPT [SBATCH_OPTION ...]' >&2
        return 2
    fi
    local tower_script=$1
    shift
    "${TOWER_BIN:-tower}" report submit --script "$tower_script" -- "$@"
}

tower_run() {
    if [[ $# -lt 1 ]]; then
        printf '%s\n' 'tower_run: expected COMMAND [ARG ...]' >&2
        return 2
    fi
    "${TOWER_BIN:-tower}" report exec -- "$@"
}

tower_metric() {
    "${TOWER_BIN:-tower}" report metric "$@"
}

tower_artifact() {
    "${TOWER_BIN:-tower}" report artifact "$@"
}
