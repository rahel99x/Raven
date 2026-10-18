#!/usr/bin/env bash
#SBATCH --job-name=raven-trace-mpi
#SBATCH --ntasks=2
#SBATCH --cpus-per-task=1
#SBATCH --mem=512M
#SBATCH --time=00:05:00

# Load the site's matching MPI/Python environment before submitting. Raven's
# helper overrides --ntasks when --ranks changes. MPI launcher/Slurm integration
# is site-specific; this script makes no generic srun or MPI ABI assumption.
set -euo pipefail
if [[ -n "${RAVEN_PROJECT_ROOT:-}" ]]; then
  cd -- "$RAVEN_PROJECT_ROOT"
else
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
fi
raven_mpi_ranks="${RAVEN_EXAMPLE_MPI_RANKS:-2}"
if ! [[ "$raven_mpi_ranks" =~ ^[1-9][0-9]{0,2}$ ]] || ((raven_mpi_ranks > 128)); then
  printf '%s\n' 'RAVEN_EXAMPLE_MPI_RANKS must be an integer from 1 to 128.' >&2
  exit 2
fi
exec "${RAVEN_EXAMPLE_MPIEXEC:-mpiexec}" -n "$raven_mpi_ranks" \
  "${RAVEN_EXAMPLE_PYTHON:-python3}" mpi_heat_lab.py "$@"
