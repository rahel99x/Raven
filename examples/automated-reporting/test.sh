#!/usr/bin/env bash
#SBATCH --job-name=raven-report-demo
#SBATCH --time=00:02:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=256M
#SBATCH --output=demo-%j.out
#SBATCH --error=demo-%j.err

# An ordinary batch script: Raven supplies lifecycle records around this file.
# Run from this example directory; Raven preserves the submission working directory.
set -euo pipefail
python3 solve.py "$@"
