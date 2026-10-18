#!/usr/bin/env bash
#SBATCH --job-name=raven-trace-cuda
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --gres=gpu:1
#SBATCH --time=00:05:00

# Use a CUDA-enabled PyTorch environment suitable for your site's driver.
# RAVEN_EXAMPLE_PYTHON is an executable path/name, not a shell command.
# Raven freezes this script outside the project, so use the project identity.
set -euo pipefail
if [[ -n "${RAVEN_PROJECT_ROOT:-}" ]]; then
  cd -- "$RAVEN_PROJECT_ROOT"
else
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
fi
exec "${RAVEN_EXAMPLE_PYTHON:-python3}" cuda_heat_lab.py "$@"
