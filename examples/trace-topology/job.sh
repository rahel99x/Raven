#!/usr/bin/env bash
#SBATCH --job-name=raven-trace-topology
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=512M
#SBATCH --time=00:05:00

# run.sh makes the explicit per-example resource overrides. Load the site's
# Python/MPI environment before launching; all paths must be shared by nodes.
set -euo pipefail
cd -- "${RAVEN_PROJECT_ROOT:-$(dirname -- "${BASH_SOURCE[0]}")}"
raven_program="${RAVEN_EXAMPLE_PROGRAM:?Use run.sh to select an example}"
case "$raven_program" in
  single_thread.py|multi_thread.py|single_core.py|multi_core.py|single_node.py|multi_node.py|single_gpu.py|multi_gpu.py) ;;
  *) printf '%s\n' 'Invalid topology program selection.' >&2; exit 2 ;;
esac
raven_command=("${RAVEN_EXAMPLE_PYTHON:-python3}")
if [[ "${RAVEN_EXAMPLE_ACTIVITIES:-0}" == 1 ]]; then
  raven_command+=(profile_runner.py)
fi
raven_command+=("$raven_program" "$@")
case "$raven_program" in
  single_core.py|multi_core.py)
    if [[ "${RAVEN_EXAMPLE_EXECUTION:?Use run.sh to select local or Slurm}" == slurm ]]; then
      exec srun --nodes=1 --ntasks=1 --cpus-per-task="${RAVEN_EXAMPLE_CPUS:?}" \
        --cpu-bind=cores "${raven_command[@]}"
    fi
    exec "${raven_command[@]}"
    ;;
  single_node.py|multi_node.py)
    if [[ "${RAVEN_EXAMPLE_EXECUTION:?Use run.sh to select local or Slurm}" == slurm ]]; then
      raven_mpi_args=(--nodes="${RAVEN_EXAMPLE_NODES:?}" --ntasks="${RAVEN_EXAMPLE_RANKS:?}" --cpu-bind=cores)
      if [[ "$raven_program" == multi_node.py ]]; then
        raven_mpi_args+=(--ntasks-per-node=1)
      fi
      if [[ -n "${RAVEN_EXAMPLE_MPI:-}" ]]; then
        raven_mpi_args+=(--mpi="$RAVEN_EXAMPLE_MPI")
      fi
      exec srun "${raven_mpi_args[@]}" "${raven_command[@]}"
    fi
    exec "${RAVEN_EXAMPLE_MPIEXEC:-mpiexec}" -n "${RAVEN_EXAMPLE_RANKS:?}" "${raven_command[@]}"
    ;;
  *) exec "${raven_command[@]}" ;;
esac
