#!/usr/bin/env bash
#SBATCH --job-name=raven-cpu-ddp
#SBATCH --time=00:03:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=2G

# Activate a Python environment with PyTorch before torchrun.
set -euo pipefail
torchrun --standalone --nproc-per-node=2 train_ddp.py
