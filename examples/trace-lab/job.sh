#!/usr/bin/env bash
#SBATCH --job-name=raven-trace-lab
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=3
#SBATCH --mem=512M
#SBATCH --time=00:05:00

# Submit with the trace-lab directory as Raven's project root. Raven freezes
# this script elsewhere, so use its project identity rather than the copy's path.
# Load your site's Python module or activate its environment here if needed.
# RAVEN_EXAMPLE_PYTHON is an executable path/name, not a shell command.
set -euo pipefail
if [[ -n "${RAVEN_PROJECT_ROOT:-}" ]]; then
  cd -- "$RAVEN_PROJECT_ROOT"
else
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
fi
exec "${RAVEN_EXAMPLE_PYTHON:-python3}" heat_lab.py "$@"
