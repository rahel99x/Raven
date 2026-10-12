#!/usr/bin/env bash
#SBATCH --job-name=raven-trace-demo
#SBATCH --time=00:02:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=256M
#SBATCH --output=trace-%j.out
#SBATCH --error=trace-%j.err

set -euo pipefail
# Keep the site's module loads and application-environment activation above this hook.
if [[ -n ${RAVEN_SHELL_HELPER:-} ]]; then
    source "$RAVEN_SHELL_HELPER"
    raven_exec python3 solve.py "$@"
else
    python3 solve.py "$@"
fi
