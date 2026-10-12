#!/usr/bin/env bash
#SBATCH --job-name=raven-report
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:10:00
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err
# Supply the account, partition, and actual resources with sbatch options.
# Load your site's Python/application modules here when required.
set -euo pipefail
: "${SLURM_JOB_ID:?Run this batch script through sbatch}"
# Slurm starts this script in the actual job WorkDir, including --chdir.
# SLURM_SUBMIT_DIR can be a different directory and must not override it.
export RAVEN_PROJECT_ROOT="$PWD"
if [[ ! -f "$RAVEN_PROJECT_ROOT/.raven/reporting-v2/run.sh" ]]; then
    printf '%s\n' 'The Slurm job WorkDir must be the project root containing .raven/reporting-v2/run.sh.' >&2
    exit 2
fi
if [[ $# -eq 0 ]]; then
    printf '%s\n' 'Usage: sbatch [OPTIONS] .raven/reporting-v2/batch.sh COMMAND [ARG ...]' >&2
    exit 2
fi
# The reporter runs once in the batch allocation. Only the application is srun.
exec bash "$RAVEN_PROJECT_ROOT/.raven/reporting-v2/run.sh" srun "$@"
