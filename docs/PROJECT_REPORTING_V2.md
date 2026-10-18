# Project reporting V2

Connect an existing application to Raven with a small launcher and portable
report files. The same reporting code works on a Linux workstation and inside a
Slurm job. Python 3.10 or later is the only dependency of the portable launcher;
the application can use any language.

V2 is the revision of this integration guide and starter kit. The files retain
the supported `raven.run/v1`, `raven.summary/v1`, and other versioned contracts.
Existing V1 projects remain readable. Do not change their schema markers to
`/v2`.

For the installed CLI's simplest existing-script workflow, start with
[automated reporting and exports](guides/automated-reporting-exports.md).
`raven launch batch.sh --research` packages the runtime and creates exact run
records without copying a helper or writing a manifest. This guide remains the
portable, explicitly embedded integration for applications and site workflows
that need to own their reporting lifecycle.

## Choose the part you need

Inside Raven, open **Setup → Recordings and metrics** for sample validation and
reporting instructions. **Setup → Connections and accounts** configures Raven's
own Heavy worker account and resource defaults. See the
[internal Setup guide](guides/setup.md).

| Need | Start here |
| --- | --- |
| Add Python measurements with one decorator or call | [Python metrics client](guides/python-metrics.md) |
| Launch an unchanged shell script and export measurements | [Automated launch and export](guides/automated-reporting-exports.md) |
| Record a first application run | [Quick start](#quick-start) |
| Add reporting to an existing Bash or batch script | [Application and job hooks](#application-and-job-hooks) |
| Use a professor's allocation as a student | [Users, lab accounts, and runs](#users-lab-accounts-and-runs) |
| Configure a workstation, CARC, or UCI HPC3 | [Site recipes](guides/project-reporting-sites.md) |
| Write JSON, JSONL, or GPU CSV correctly | [File-format cookbook](reference/project-reporting-formats.md) |
| Populate a specific Research or Analytics panel | [Complete metric and source reference](reference/project-reporting-metrics.md) |
| Reuse the starter files | [Portable starter](../examples/reporting-v2/README.md) |
| Integrate with the installed Slurm reporting CLI | [Existing CLI workflow](guides/research-automation.md) |

The starter records execution identity, stdout, stderr, wall time, and exit
status. Add scientific metrics, GPU traces, output contracts, and planning
evidence as separate modules. A missing module leaves its corresponding view
unavailable; a launcher cannot infer scientific progress or results.

## Quick start

For Python jobs launched by Raven, start with this optional API:

```python
import raven
@raven.measure("residual", kind="residual", every=50)
def iterate(state): return solve_iteration(state)
```

Raven provides the client inside launched Python processes. Use `raven.phase`
for timed phases, `raven.metric` for explicit scalar values and `raven.progress`
for measured completion. The [client guide](guides/python-metrics.md) includes
the kind table, MPI/DDP collective requirements, helper-worker behavior,
coalescing, optional vendoring and local development. Reporting is inactive
outside a run unless an output is configured. The portable launcher below and
manual JSONL/Bash producers remain useful for other languages and lifecycles.

### 1. Copy the reporting module into your project

Run this from your own project root. Set `RAVEN_CHECKOUT` to your Raven checkout
and update that checkout before copying the V2 starter.

```bash
RAVEN_CHECKOUT="$HOME/Raven"

mkdir -p .raven &&
mkdir .raven/reporting-v2 &&
cp "$RAVEN_CHECKOUT/examples/project-template/reporting.py" \
   "$RAVEN_CHECKOUT/examples/project-template/LICENSE" \
   .raven/reporting-v2/ &&
cp "$RAVEN_CHECKOUT/examples/reporting-v2/portable_run.py" \
   "$RAVEN_CHECKOUT/examples/reporting-v2/run.sh" \
   "$RAVEN_CHECKOUT/examples/reporting-v2/batch.sh" \
   "$RAVEN_CHECKOUT/examples/reporting-v2/demo.py" \
   "$RAVEN_CHECKOUT/examples/reporting-v2/README.md" \
   .raven/reporting-v2/
```

The destination must be new. If it already exists, review an upgrade separately
instead of replacing your integration files. Keep the license with copied code.
Commit the module to the application repository so compute nodes and other lab
members receive the same reporting implementation.

Add generated `runs/`, `logs/`, `reports/`, and `.raven/passports/` directories to
your project's `.gitignore`. These files can contain private measurements and
application output. Share selected results deliberately.

### 2. Run the small example

On a Linux workstation where local computation is permitted:

```bash
export RAVEN_EXPERIMENT=reporting-demo
export RAVEN_SCRIPT=.raven/reporting-v2/demo.py
bash .raven/reporting-v2/run.sh python3 .raven/reporting-v2/demo.py
```

On a cluster, run this command after obtaining a compute shell using the
[site recipe](guides/project-reporting-sites.md), or submit it through the batch
hook below. A cluster login shell is not a local workstation.

The launcher prints the exact run directory. Expected files are:

```text
runs/<unique-run-id>/
  run.json
  metrics.jsonl
  summary.json
  logs.json
  logs/stdout.log
  logs/stderr.log
  outputs/
  reports/
  passports/
```

The demo performs a small numerical calculation and reports its actual values.
It is an integration check, not a workload benchmark. A standalone run has no
Slurm job ID or claimed scheduler allocation. A run inside Slurm records the
available scheduler identity and resource declarations.

### 3. Open the run in Raven

On a workstation without Slurm, start `ravenl --site desktop`. For local Slurm
or a cluster connection, start Raven with your configured Slurm site. The
portable reporter does not require the dashboard to be running.

Use **Connect → Connect and validate a research project** to select your
project and the exact run directory printed by the launcher. Alternatively,
use `:project` with the absolute project path, then `:run select` with that
directory's run ID. See [project connection](guides/project-connection.md).

Open **Research → Experiment** for application measurements. A job's native CPU
and memory curves are separate scheduler measurements. The
[panel reference](reference/project-reporting-metrics.md) explains which source
each view consumes.

## Application and job hooks

### Wrap an existing program

Set the scientific source entry point and keep the application's existing
arguments. For example, in a project that contains `train.py`:

```bash
export RAVEN_EXPERIMENT=model-fit
export RAVEN_SCRIPT=train.py
export RAVEN_PARAMETERS='{"seed":7,"dataset":"dataset-v3"}'
bash .raven/reporting-v2/run.sh python3 train.py --epochs 100
```

`RAVEN_PARAMETERS` records context; it does not set the application's arguments
or random seed. Keep it consistent with the configuration the program actually
uses.

For Julia, select the actual `.jl` source and launch `julia` normally. For a
compiled application, select its scientific source entry point and run the
existing executable. `RAVEN_SCRIPT` names a regular project-relative source
file, at most 8 MiB. Include code-tree, environment, and input fingerprints in
parameters when one file does not describe the workload.

The application receives these paths:

| Variable | Purpose |
| --- | --- |
| `RAVEN_RUN_DIR` | Absolute directory for this exact execution attempt |
| `RAVEN_REPORT_HELPER` | Copied portable helper for metric commands |
| `RAVEN_REPORT_PYTHON` | Python interpreter used by the reporting helper |

Write new outputs beneath `RAVEN_RUN_DIR/outputs`, or declare existing outputs
through the documented contract. The wrapper preserves the application's exit
status and writes its logs to this run. It does not interpret arbitrary stdout
as metrics.

### Add one line to an existing batch script

Keep your module loads, environment activation, and `#SBATCH` directives.
Replace the existing application launch with the reporting wrapper:

```bash
export RAVEN_EXPERIMENT=model-fit
export RAVEN_SCRIPT=train.py
bash .raven/reporting-v2/run.sh srun python3 train.py --epochs 100
```

Run the wrapper once per job or array task. For an MPI application, put the
wrapper around the existing `srun` or `mpirun` command; do not start a reporting
wrapper in every rank. One coordinator owns a run's JSONL file. Use rank zero
or an explicit aggregator for application measurements.

If you need a new batch entry point, the supplied `batch.sh` wraps `srun` once:

```bash
export RAVEN_EXPERIMENT=model-fit
export RAVEN_SCRIPT=train.py
sbatch --account="${RAVEN_REPORT_ACCOUNT:?Set your approved Slurm account}" \
  --partition="${RAVEN_REPORT_PARTITION:?Set your approved partition}" \
  --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=8G --time=00:30:00 \
  .raven/reporting-v2/batch.sh python3 train.py --epochs 100
```

Set the account and partition from your actual site settings before submitting.
Submit from the project root, or select it with `sbatch --chdir` and use the
batch script's absolute path. The script preserves Slurm's actual WorkDir.
Resource requests are examples, not requirements of Raven. Slurm does not
expand shell variables inside `#SBATCH` lines; pass variable values as command
arguments, as above. If your site clears exported environments, load the same
settings inside the batch script.

### Publish actual application metrics

Call the helper with values your application has computed. This Bash fragment
assumes `loss`, `step`, and `total_steps` are already set:

```bash
"$RAVEN_REPORT_PYTHON" "$RAVEN_REPORT_HELPER" metric \
  --value "loss=$loss" \
  --step "$step" --completed "$step" --total "$total_steps" --unit steps
```

The helper writes one newline-terminated JSON object. Its shape is:

```json
{"t":1791820800.0,"step":10,"metrics":{"loss":0.24},"progress":{"completed":10,"total":100,"unit":"steps"}}
```

Those numbers illustrate the format. A real application supplies its own
timestamp and measurements; the helper supplies the current timestamp by
default. Use JSON numbers, finite values, and consistent units. There is no
fixed list of scientific metric names. The [format cookbook](reference/project-reporting-formats.md)
contains Python, Bash, and direct JSONL examples, all optional fields, and
requirements for summaries, logs, outputs, and planning reports.

### Add GPU measurements when needed

GPU traces are an optional producer alongside the application. They use the
job's actual working directory and Slurm ID:
`logs/gpu-util-<job-id>.csv`. They are separate from `runs/<run-id>/metrics.jsonl`.
See the [GPU CSV recipe](reference/project-reporting-formats.md) and
[GPU collection guide](guides/gpu-detection.md#provide-an-optional-gpu-trace).

An application metric named `gpu_util` remains an application metric; that name
does not turn it into a native device measurement. The source reference covers
device identity, multi-node limits, retained samples, and the views that can
consume the CSV.

## Users, lab accounts, and runs

Suppose student **User A** is permitted to charge work to **LabX**. These are
different identities:

| Identity | Example | Meaning |
| --- | --- | --- |
| Login / UNIX user | `user_a` | Owns the submitted job and personal report files |
| Slurm account | `labx` | Allocation charged by `--account=labx` |
| Site / cluster | Site-reported cluster name | Distinguishes job numbers on different schedulers |
| Slurm job | Actual value returned by Slurm | Allocation or array-task identity |
| Experiment | `model-fit` | Stable scientific workload shared by comparable attempts |
| Run | A newly generated run ID | One invocation, with its own logs and outputs |

`user_a` and `labx` are fictional examples. Use your own login and an account
the site has assigned to you. Setting `resources.account` in a report records
metadata; only the Slurm submission or allocation request selects the billing
account. A lab account does not change job ownership.

Keep one writable project/report root per student, even when the storage is a
lab allocation. Share source code and read-only datasets as appropriate. The
starter creates private run directories; it does not configure a multiuser
database or shared-write ACLs. Share selected result bundles using the lab's
permission and retention procedure.

### Reuse an interactive allocation

Request the lab account through the site's allocation command. Enter a compute
shell if the site's `salloc` does not already place you there. Then change to
your project root and use the same `run.sh` command for each experiment. The
portable launcher creates a new run ID on every invocation, even when all runs
share one Slurm job ID.

Select the exact run in Raven when multiple runs refer to that job. The job
alone cannot identify which scientific attempt you intend to inspect. Do not
create a fake job ID for a standalone run or overwrite an earlier attempt.
See the [interactive site recipes](guides/project-reporting-sites.md).

Raven Heavy is a dashboard worker allocation. It is separate from the lab's
scientific job and does not automatically launch, instrument, or move the
application. Reporting itself needs no Heavy worker.

## Check the integration

| Check | Expected result | If it fails |
| --- | --- | --- |
| Run the supplied demo | A new run directory, real metric rows, terminal summary | Read that run's stderr and check Python 3.10+ |
| Open the project guide | The exact run and its declared files are recognized | Check project root, run ID, permissions, and exact relative paths |
| Watch Research → Experiment | New metric rows appear | Check final newlines, finite numeric values, selected run, and source age |
| Open a selected running job's resource view | Available scheduler samples appear | Check Slurm accounting, current source demand, and collection policy |
| Open a completed job | Retained curves or job-produced traces appear when present | Missing historical samples cannot be reconstructed from one summary |
| Open an advanced Research view | Its declared native inputs are recognized | Follow the specific panel's source contract; metrics JSONL is not a substitute |

SIGINT and SIGTERM can be reported by the portable launcher. SIGKILL, node
failure, and lost storage can prevent finalization; a leftover `RUNNING` manifest
is not proof that the application still runs. Use Slurm accounting and preserved
logs to reconcile the attempt.

For a complete implementation, work through the [metric and panel reference](reference/project-reporting-metrics.md)
and [file-format cookbook](reference/project-reporting-formats.md). Each module
states its producer, required files, and evidence limits. The original
[project standard](PROJECT_STANDARD.md) remains the detailed compatibility
contract.
