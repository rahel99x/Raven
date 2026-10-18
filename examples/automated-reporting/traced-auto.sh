#!/usr/bin/env bash
#SBATCH --job-name=raven-auto-trace
#SBATCH --time=00:02:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=256M

# No helper, source rewriting or command prefix is needed for automatic tracing.
# Retain site module loads and application-environment activation here.
set -euo pipefail
python3 solve_client.py "$@"
