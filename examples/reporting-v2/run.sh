#!/usr/bin/env bash
# One reporting coordinator around the command, on a workstation or compute node.
set -euo pipefail
if [[ $# -eq 0 ]]; then
    printf '%s\n' 'Usage: run.sh COMMAND [ARG ...]; set RAVEN_EXPERIMENT and RAVEN_SCRIPT.' >&2
    exit 2
fi
raven_helper_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
raven_parameters="${RAVEN_PARAMETERS:-}"
if [[ -z "$raven_parameters" ]]; then
    raven_parameters='{}'
fi
exec "${RAVEN_REPORT_PYTHON:-python3}" "$raven_helper_dir/portable_run.py" run \
    --root "${RAVEN_PROJECT_ROOT:-$PWD}" \
    --experiment "${RAVEN_EXPERIMENT:?Set a stable scientific experiment name}" \
    --script "${RAVEN_SCRIPT:?Set the scientific source path relative to the project root}" \
    --parameters "$raven_parameters" -- "$@"
