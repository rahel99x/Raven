#!/bin/bash
#SBATCH --mem=512M
#SBATCH --time=00:05:00
set -euo pipefail

# Review each generated plan before explicitly submitting it. This is a small,
# single-node CPU demonstration, not a representative application benchmark.
checkout=${RAVEN_ROOT:-${SLURM_SUBMIT_DIR:?}}
python="$checkout/.venv/bin/python"
if [ ! -x "$python" ]; then
    printf 'Run python3 scripts/setup.py --mode local in %s first.\n' "$checkout" >&2
    exit 127
fi
if [ "${SLURM_JOB_NUM_NODES:-1}" != 1 ]; then
    printf 'This example requires exactly one allocated node.\n' >&2
    exit 2
fi
mkdir -p runs/scaling
# The job allocation contains workers CPU tasks. One bounded multiprocessing
# step consumes those CPUs; the Python workload partitions the fixed problem.
srun --ntasks=1 --cpus-per-task="${RAVEN_SCALING_WORKERS:?}" --cpu-bind=cores \
    "$python" "$checkout/examples/planning/scaling_workload.py" run \
    --script "$checkout/examples/planning/scaling.sbatch" \
    --output "runs/scaling/${SLURM_JOB_ID:?}.json"
