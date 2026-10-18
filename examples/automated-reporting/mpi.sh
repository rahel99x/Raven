#!/usr/bin/env bash
#SBATCH --job-name=raven-mpi-metrics
#SBATCH --time=00:03:00
#SBATCH --nodes=1
#SBATCH --ntasks=2
#SBATCH --mem=512M

# Load site-compatible MPI and mpi4py modules before this command.
# A site may require an explicit srun --mpi setting; follow its launch policy.
set -euo pipefail
srun -n 2 python3 rank_mpi.py
