# Portable reporting starter

Use this starter to wrap an existing application on a Linux workstation or
inside a Slurm compute allocation. It records each execution in a separate
directory using Raven's current `v1` file formats. The [V2 implementation
guide](https://github.com/rahel99x/Raven/blob/main/docs/PROJECT_REPORTING_V2.md) covers installation, lab accounts,
CARC, UCI RCIC, optional GPU traces, and viewing the resulting files.

Python 3.10 or later and Bash are required. Neither Raven nor additional Python
packages are needed on the machine running the application. The wrapper runs
the command you supply on that machine; obtain compute resources before using
it on a cluster.

## Copy into an existing project

From the project root, point `raven_checkout` at your Raven checkout. The
destination must be new: these commands refuse to replace existing integration
files. Retain the canonical reporter's MIT license when redistributing it.

```bash
raven_checkout="$HOME/Raven"
(
  set -eu
  mkdir -p .raven
  mkdir .raven/reporting-v2
  for raven_file in portable_run.py run.sh batch.sh demo.py README.md; do
    cp "$raven_checkout/examples/reporting-v2/$raven_file" .raven/reporting-v2/
  done
  cp "$raven_checkout/examples/project-template/reporting.py" .raven/reporting-v2/
  cp "$raven_checkout/examples/project-template/LICENSE" .raven/reporting-v2/LICENSE
)
```

Commit the integration files. Keep generated `runs/`, `reports/`, and
`slurm-*.out` / `slurm-*.err` files out of Git. Give each student their own
writable project/report root; sharing a Slurm billing account does not make the
private run directories a multiuser collaboration store.

## Run the working example

Run this on your workstation or from a compute shell obtained through your
site's interactive allocation procedure:

```bash
export RAVEN_EXPERIMENT=first-run
export RAVEN_SCRIPT=.raven/reporting-v2/demo.py
export RAVEN_PARAMETERS='{"steps":5,"terms_per_step":1000}'
bash .raven/reporting-v2/run.sh python3 .raven/reporting-v2/demo.py
```

The example calculates an approximation of pi. Each iteration writes its
actual estimate, error, and progress. The wrapper prints the new run directory
and saves the application's output in `logs/stdout.log` and `logs/stderr.log`
within that directory. The final scientific result is `outputs/result.json`.

To wrap your own program, change the experiment name, scientific source path,
parameters, and command:

```bash
export RAVEN_EXPERIMENT=solver-baseline
export RAVEN_SCRIPT=src/solver.cpp
export RAVEN_PARAMETERS='{"mesh":"coarse","seed":7}'
bash .raven/reporting-v2/run.sh ./build/solver --mesh coarse --seed 7
```

`RAVEN_SCRIPT` is an existing project-relative scientific source file that the
reporter fingerprints. It does not choose or build the executable. For a
third-party program, use the actual scientific configuration/input declaration
and record the program version in parameters. That fingerprints the declaration;
it does not capture the external program's source. Add dataset, build, and
environment digests when those are needed for meaningful comparisons.

`RAVEN_PARAMETERS` records the configuration; it does not pass arguments or set
the application's seed. Keep its values consistent with the program's actual
inputs.

The current directory is the project root unless `RAVEN_PROJECT_ROOT` names an
absolute root. The wrapper changes the application's working directory to that
root. `RAVEN_REPORT_PYTHON` optionally selects a Python executable containing
only the standard library; the application command can use a different Python,
Conda environment, compiled binary, or language runtime.

## Submit through Slurm

Put your site's Python and application module loads inside your copy of
`batch.sh`, before its final launcher command. For interactive work, load them
inside the allocated compute shell. Follow the site's login-node policy;
HPC3 requires application environments to be loaded in the compute allocation.
Set `RAVEN_REPORT_ACCOUNT` and `RAVEN_REPORT_PARTITION` to names authorized for
your user. For the demo, set the scientific declarations explicitly before
submitting from the project root:

```bash
export RAVEN_EXPERIMENT=first-run
export RAVEN_SCRIPT=.raven/reporting-v2/demo.py
export RAVEN_PARAMETERS='{"steps":5,"terms_per_step":1000}'
sbatch --account="${RAVEN_REPORT_ACCOUNT:?Set your authorized Slurm account}" \
  --partition="${RAVEN_REPORT_PARTITION:?Set your authorized Slurm partition}" \
  --cpus-per-task=4 --mem=8G --time=00:30:00 \
  .raven/reporting-v2/batch.sh python3 .raven/reporting-v2/demo.py
```

An account such as `LabX` is the allocation charged by Slurm; the submitting
student remains the job owner. This file does not assume that CARC and RCIC use the same account or
partition names. If the site disables environment export, set the `RAVEN_*`
declarations and load Python/application modules explicitly inside your copy of
`batch.sh`.

The batch script uses Slurm's actual job working directory as its project root.
When submitting elsewhere, supply `sbatch --chdir=/absolute/project` and the
absolute batch script path. `SLURM_SUBMIT_DIR` may differ from that directory;
the script preserves the job's working directory for report and trace discovery.

The batch script creates one reporting coordinator around `srun APPLICATION`.
For MPI, launch the application ranks inside that one `srun`; do not put
`run.sh` inside every rank. Only rank zero should publish the shared run's
metrics, or aggregate measurements through a single coordinator.

Each call creates a new attempt directory, including repeated commands within
one interactive allocation. Slurm arrays retain `ARRAY_ID_TASK_ID`; requeues
retain the restart count. When multiple reports match the same allocation,
select the specific run explicitly in Raven.

## Add measurements from any language

The application receives `RAVEN_RUN_DIR`, `RAVEN_PROJECT_ROOT`,
`RAVEN_REPORT_HELPER`, and `RAVEN_REPORT_PYTHON`. Invoke the following from its
coordinator with values that the application actually measured:

```bash
"$RAVEN_REPORT_PYTHON" "$RAVEN_REPORT_HELPER" metric \
  --value loss=0.24 --step 10 --phase training \
  --completed 10 --total 100 --unit epochs
```

Replace the example numbers with application variables. The command validates
finite numbers and progress before appending one native JSONL row. Repeated
metric names within one command are refused. Python applications can instead
import `write_metric` from the copied canonical `reporting.py`; the wrapper
already owns `begin_run` and `finish_run`.

Write scientific outputs beneath `$RAVEN_RUN_DIR/outputs/`. Wrapping alone
provides lifecycle, logs, source fingerprint, and elapsed time. CPU, memory,
GPU usage, scientific progress, and research predictions require their actual
measurements; the wrapper does not invent them. Optional GPU trace instructions
are in the V2 guide.

## Inspect the result

Open Raven and enter:

```text
:project /absolute/path/to/your/project
:runs
```

Select the run printed by the wrapper, or use `:run select RUN_ID`. Local runs
have no scheduler job ID. For Slurm runs, the original job work directory must
be this project root for automatic attachment; explicit project/run selection
also works. The `run.json` and `summary.json` records contain separate user,
host, Slurm account/resource, cluster, array, and retry information when known.

The wrapper forwards SIGINT and SIGTERM to the application process group and
retains its actual exit status. Uncatchable termination, machine failure, or a
lost filesystem can leave a run unfinished. A failure to save the final report
is printed to stderr and does not replace the application's exit status.
