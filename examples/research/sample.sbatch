#!/bin/bash
#SBATCH --job-name=raven-science
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=256M
#SBATCH --time=00:05:00
#SBATCH --output=raven-science-%A_%a.out
#SBATCH --error=raven-science-%A_%a.err
set -euo pipefail

# Submit from the checkout, or export its absolute path before submitting.
# SLURM_SUBMIT_DIR remains valid when Slurm copies this script into its spool.
checkout=${RAVEN_ROOT:-${SLURM_SUBMIT_DIR:?}}
python="$checkout/.venv/bin/python"
if [ ! -x "$python" ]; then
    printf 'Run python3 scripts/setup.py --mode local in %s first.\n' "$checkout" >&2
    exit 127
fi
seed=$((7 + ${SLURM_ARRAY_TASK_ID:-0}))
suffix=${SLURM_ARRAY_TASK_ID:+_$SLURM_ARRAY_TASK_ID}
mkdir -p runs
"$python" "$checkout/examples/research/experiment.py" \
    --output "runs/${SLURM_JOB_ID:?}${suffix}" --seed "$seed" --delay 0
