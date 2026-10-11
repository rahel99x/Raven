# Automate Research reporting from Bash

Use `tower report` in the launcher and the batch script. Tower records the actual
Slurm job ID, experiment, dependencies, array identity, and each restart attempt.
Research refreshes these records while Tower remains open. Reporting has no
Python package dependencies beyond Tower and Python 3.10 or later.

The reporting commands run only when your script calls them. Reading Research
never submits a job or executes a project file.

## A complete minimal project

Keep the source and the batch script in a project directory. Use an absolute
project path on storage that the submission host and compute nodes can access.
Tower must be on `PATH` in both environments.

Create `launch.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
export TOWER_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export TOWER_EXPERIMENT="solver-comparison"
tower report init --name "Solver comparison"
job_id=$(tower report submit --script batch.sh -- --job-name=solver --cpus-per-task=4)
printf 'Submitted %s\n' "$job_id"
```

Create `batch.sh`:

```bash
#!/usr/bin/env bash
#SBATCH --time=00:10:00
#SBATCH --output=slurm-%j.out

# Inherit TOWER_PROJECT_ROOT, TOWER_EXPERIMENT, and TOWER_SCRIPT from submit.
# This command records start, stdout, stderr, wall time, and the process result.
tower report exec -- python3 solve.py
```

The application receives `TOWER_RUN_DIR`. Write scientific files there:

```python
import json
import os
from pathlib import Path

run = Path(os.environ["TOWER_RUN_DIR"])
(run / "outputs" / "result.json").write_text(json.dumps({"residual": 0.002}))
```

The application remains responsible for actual progress and scientific metrics.
Tower does not substitute wall-time usage for scientific progress or fabricate
missing CPU, memory, GPU, or result measurements.

`exec` returns the command's exit status. It records nonzero exits and forwards
SIGINT and SIGTERM to the command. SIGKILL, machine failure, or an inaccessible
filesystem can prevent a final report; Slurm accounting remains the authority
for those terminal states. Do not place the same whole-job `exec` wrapper in
every MPI rank. Use one coordinator per job or array task.

## Use Bash functions

Optionally source `scripts/tower-report.sh` from your Tower checkout:

```bash
source "$HOME/Slurm-Tower/scripts/tower-report.sh"
# If only the interactive alias exists, use the checkout executable explicitly:
export TOWER_BIN="$HOME/Slurm-Tower/scripts/tower"
first=$(tower_submit preprocess.sbatch --job-name=prepare)
second=$(tower_submit train.sbatch --dependency="afterok:${first%%;*}")
last=$(tower_submit summarize.sbatch --dependency="afterany:${second%%;*}")
```

The helper provides `tower_submit SCRIPT [SBATCH_OPTIONS]`, `tower_run COMMAND
[ARGS]`, `tower_metric`, and `tower_artifact`. It changes no shell options or
traps. Set `TOWER_BIN` to an exact Tower executable path if needed. An interactive
shell alias alone is not available inside batch scripts.

Submission options use `--option=value` syntax. Use the root and script options
before `--`; put Slurm options after it. The tracked launcher fixes the workdir
to the project root. It refuses a second script, `--wrap`, `--wait`, workdir
overrides, and multi-cluster submissions. Submit separate tracked calls for
different clusters or heterogeneous components. Slurm's resource options and
site policy still apply.

## Arrays, dependency graphs, and retries

Each array task uses `ARRAY_ID_TASK_INDEX`, not its incidental allocation ID.
Requeues use `SLURM_RESTART_COUNT + 1`. A run never overwrites a finished attempt.
Array tasks write separate directories and can report concurrently.

To map task indices to scientific inputs, supply a
[`tower.array-manifest/v1`](../schemas/array-manifest.schema.json) document:

```bash
array_id=$(tower report submit --script train.sbatch \
  --array-manifest array-manifest.json -- --array=2,19,44%3)
```

Use the real sparse indices in `entries`. The manifest's initial `array_id` is
a placeholder; the wrapper replaces it with the actual submitted parent ID and
cluster. Each task binds its own immutable copy in `reports/array-manifest.json`.
Do not infer sample order from the order in which Slurm starts tasks.

Arbitrary Slurm dependency expressions are recorded verbatim, including
`afterany`, `afternotok`, array dependencies, and OR expressions. Native Workflow
forecast reports have a narrower, explicit `afterok` model with user-supplied
runtime estimates. Reporting does not silently reinterpret other dependency
types as successful-completion dependencies. Use the Deps view for the actual
scheduler graph.

If `sbatch` times out or returns ambiguous output, the command writes an
`unknown` intent under `.tower/launches/` and stops. Check Slurm and the intent
before submitting again. There is no automatic submission retry. If receipt
publication fails after acceptance, the error includes the accepted job ID.

## Report every Research source

| Research page | Data to declare or write |
| --- | --- |
| Experiment | `run.json`, `metrics.jsonl`, `summary.json`, experiment parameters, and actual results. `start`/`exec`/`finish` manage lifecycle files. |
| Arrays | `--array-manifest` on `submit`; actual task IDs and indices are attached automatically. |
| Evidence | Actual per-run metrics and scheduler measurements. Missing samples remain unknown. |
| Artifacts | `tower report artifact outputs/result.json --format=json` declares exact outputs and required checks. |
| Passport | `tower report passport --input=data.csv --environment=OMP_NUM_THREADS` captures selected evidence. Secrets are excluded. |
| Submit | `tower report research submit prepared-plan.json` attaches an intact `tower.submission-plan/v1` preflight. It remains read-only until the user explicitly reviews a submission action. |
| Predict, Forecast, Blockers, Tradeoffs | `tower report aggregate` builds `tower.planning` history from actual terminal summaries. Use `research VIEW FILE` for an explicit native per-view source with extra required inputs. |
| Scaling | Publish real scaling observations with `research scaling FILE`; worker counts, problem size, repeats, and measured runtime are required for meaningful scaling comparisons. |
| Workflow | `research workflow FILE` attaches an explicit native workflow recipe with estimates. Actual scheduler dependency text is also retained in launch metadata. |
| Operations | The existing operation-specific manifests remain explicit, reviewed inputs. Place them in the project and use the contextual Operations controls; reporting never auto-applies cluster or workflow changes. |

Within a coordinated shell application:

```bash
tower report metric --value loss=0.24 --value throughput=480 \
  --step 10 --completed 10 --total 100 --unit epochs
tower report artifact outputs/result.json --format=json
tower report log worker.stderr /scratch/my-project/logs/worker-17.err
tower report passport --input=data.csv
```

These commands use `TOWER_RUN_DIR`, or accept `--run /exact/attempt/path`.
Logs may have explicit external paths. Artifact and Research source bindings
remain inside the run directory. Unknown node/rank substitutions such as `%N`
and `%t` require explicit log declarations; Tower does not guess the files.

`--parameters '{"seed":7}'` and `--resources '{"cpus":4}'` on `submit` record
declared scientific parameters and known resource requests. They do not alter
Slurm's requests; the `#SBATCH` directives and actual `sbatch` options control
the allocation. No complete environment is stored in a report.

For an existing JSONL producer, use `start --metrics relative/path.jsonl` to bind
its native metric file instead. `tower report metric` refuses that custom binding
so it cannot append to an unrelated default stream. Use the
[project standard](../PROJECT_STANDARD.md) for the exact JSONL schema.

## Existing batch scripts and launchers

You can keep an existing launcher and call `start` from the batch script:

```bash
export TOWER_PROJECT_ROOT=/shared/project
export TOWER_EXPERIMENT=solver-comparison
export TOWER_SCRIPT=batch.sh
export TOWER_RUN_DIR
TOWER_RUN_DIR=$(tower report start)

# Keep your existing application and EXIT trap. Record its actual result there:
# tower report finish --exit-code "$status" --results '{"residual":0.002}'
```

Use `exec` for automatic start/finish handling when it can own the command.
Use `start`/`finish` when your workflow already controls traps, signals, logging,
or distributed execution. `finish` is idempotent for the same result and refuses
a conflicting second result. `exec` refreshes the bounded comparison history
after completion; manual coordinators call `aggregate` when appropriate.

Tracked submissions require Slurm to export the six `TOWER_*` handshake variables
so even a job that starts before `sbatch` returns keeps its correct identity.
The default `--export=ALL` works. An isolated export list must contain the names
`TOWER_PROJECT_ROOT,TOWER_EXPERIMENT,TOWER_SCRIPT,TOWER_PARAMETERS,TOWER_RESOURCES,TOWER_SUBMISSION_TOKEN`
without assigning different values. Incompatible `NONE`, `NIL`, and export-file
policies are refused before submission. For an existing, untracked launcher with
`--export=NONE`, set the three project/experiment/script declarations explicitly
in the batch script and use `start`/`finish` or `exec`. Load the Python/Tower module in the script if your site requires it.
All declared paths must be visible on the machine that runs Tower.

## Files and limits

```text
PROJECT/
  .tower/project.json                 optional descriptive metadata
  .tower/report.lock                  short metadata publication lock
  .tower/launches/TOKEN.json           durable single-submit intent/result
  .tower/submissions/CLUSTER-JOB.json  exact scheduler receipt
  reports/planning.json               bounded comparison bundle
  runs/JOB-ATTEMPT/
    run.json                         exact job/array task/attempt identity
    metrics.jsonl                    actual application measurements
    summary.json                     actual terminal result
    logs.json                        exact, grouped log locations
    logs/stdout.log
    logs/stderr.log
    outputs/
    passports/
    .tower/contracts/outputs.v1.json  optional per-attempt artifact checks
    reports/                         declared native Research sources
```

Metadata uses atomic replacement and bounded, duplicate-key-safe JSON readers.
Paths reject traversal, globs, and symlinks. Locks have a five-second wait budget.
Run-local writes use separate locks, so an array task's metric writer does not
wait for unrelated tasks or comparison aggregation. The project lock covers
shared indexes and submission receipts. No lock is held while the scientific
command runs or while `sbatch` waits. Different tasks do not share metric streams.

Exact job indexes let selected-job bindings bypass directory scans even in large
arrays. A restart remains a separate attempt; cached scheduler restart evidence
or explicit attempt selection disambiguates multiple attempts. Cluster-scoped
indexes prevent colliding numeric job IDs from selecting another cluster's run.
The project inventory and comparison reader inspect at most 256 attempts and
4096 directory entries. Split larger comparisons into explicit project roots
or archive old attempts into a separate root. Aggregation writes one shared
planning bundle rather than copying it into every run. Explicit per-run native
reports take priority over this project default. Aggregation is bounded to 1 MiB and
retains failed attempts. It does not fit models when the required scientific
data is absent. The standalone `examples/project-template/reporting.py` remains
available for Python projects that need to emit the format without installing
Tower.

## Automatic reads and SSH

Select a job in Jobs or History. Tower checks its registered project root and
the scheduler WorkDir in the background. It reads the exact job index when one
exists, then validates the selected attempt before it attaches reports. A
cluster collision, incomplete index, or changed identity produces a notice;
Tower does not guess a run. A known scheduler restart count selects the matching
restart. Otherwise use `:runs` to select the exact attempt explicitly.

The same automatic reads work with `tower --host HOST`. The selected host needs
Python 3.10 or later. Tower sends its read-only stdlib helper through the existing SSH
connection; a remote Tower installation is not required. The helper installs
nothing and does not execute project code. It uses the same confined file
checks as local reads. A missing remote helper or connection error does not
fall back to files on the local computer. Use absolute remote project paths.

Metrics append incrementally. Project inventories, array input maps, logs,
passports, output contracts, and declared planning reports refresh during the
session. Automatic array maps follow the current declaration; an explicitly
attached manual map keeps its reviewed revision until you reload it. Detach an
automatic map with `:arraymap clear` to suppress it for that selected attempt.
An explicitly declared per-run planning report takes precedence over shared
project history. Source changes preserve unaffected cached Research views.

An array parent row marked **ARRAY RECEIPT** records the submission. Its state
does not establish whether every task completed. Open Arrays for scheduler task
states. Each executing array task has its own run, logs, metrics, and terminal
summary. Failed attempts stay in the history. Missing measurements stay missing.
