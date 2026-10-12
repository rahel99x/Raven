# Evidence and experiment planning

This guide covers six terminal Research views: resource predictions, queue forecasts,
scheduler blockers, submission tradeoffs, controlled scaling, and workflow
critical paths. They use Raven's existing scheduler snapshot or an explicit JSON
file. Analysis runs in bounded background work so terminal redraws can continue.
There are no extra scheduler queries per chart redraw.

The [project reporting standard](PROJECT_STANDARD.md) defines how another
application should organize its runs and export compatible planning evidence.
The [terminal workbench](WORKBENCH.md) provides interactive graph inspection,
visual comparisons, a run picker, and a separately reviewed execution handoff.

Analysis and planning commands never submit jobs, change allocations, release
holds, or execute
batch scripts. A tradeoff choice can prepare one ordinary submission plan; the
existing Submit view then requires a separate, explicit confirmation. Scaling
and workflow planning commands produce review documents rather than bulk submission.
The separate interactive `:orchestrate` command opens a complete execution
review; only explicit confirmation of that review submits a batch.

These examples preserve native planning formats and compatibility syntax.
In managed Raven, bare `tower` starts Light. Scientific planning requires a
supported admitted worker, and some execution handoffs remain deferred even
after admission. Use the [execution contracts](raven/execution-contracts.md)
to distinguish supported analysis, verified job-local work, and deferred actions.
The standalone Python APIs at the end of this guide retain their behavior
outside a managed session.

## Try the terminal views

Install Raven or complete the source setup before running these commands.
The examples use the `tower` compatibility entry point; `ravenl` also opens the
Light interface. Installed entry points do not require a shell alias.

```bash
tower --fake --tab research --research-view predict
tower --fake --tab research --research-view forecast
tower --fake --tab research --research-view blockers
tower --fake --tab research --research-view tradeoffs
tower --fake --tab research --research-view scaling
tower --fake --tab research --research-view workflow
```

The demo is explicitly simulated. Solid block ranges, comparison bars, scaling
curves, and workflow timelines render with terminal characters. Use `--ascii`
for a terminal that cannot display Unicode, or `--unicode` to select block
glyphs. In Research, Left/Right changes the view. Up/Down selects jobs in
Resources, Forecast, and Blockers; PgUp/PgDn scrolls long results.

Attach an explicit planning file with:

```bash
tower --tab research --research-view predict \
  --planning-file examples/planning/observations.json
tower --fake --tab research --research-view workflow \
  --planning-file examples/planning/workflow.json
```

The first command uses the real local backend unless you add `--fake`. On CARC,
run Raven on the login node where your Slurm commands and project paths work.
File preparation uses the local filesystem; run it on the cluster rather than
asking an SSH viewer to inspect a different machine's files.

## Commands and result files

`tower run` prints a JSON result suitable for inspection or redirection. The
file-backed analyses below use explicit observations or recipes rather than
collecting scheduler data. Managed execution admission still applies. A result
can legitimately contain `null`, `insufficient`, `partial`, or
`blocked`: read its evidence and limitations before treating a number as usable.
An invalid or blocked result can return exit status 1 while still printing its
JSON evidence; a blocked job report does not mean Raven attempted a job action.

| Command | What it does |
| --- | --- |
| `tower run predict [JOBID]` | Predicts measurements from the current history snapshot. |
| `tower run forecast [JOBID]` | Examines the selected queued job and recorded forecast outcomes. |
| `tower run blockers [JOBID]` | Explains observed scheduler reasons and dependency evidence. |
| `tower run predict --file FILE` | Predicts from an explicit history or planning bundle. |
| `tower run forecast --file FILE` | Forecasts from a bundle containing the queued job and observations. |
| `tower run blockers --file FILE` | Explains the bundle's job, details, dependencies, and placement evidence. |
| `tower run tradeoffs FILE` | Compares explicitly declared requests for the same scientific work. |
| `tower run tradeoffs FILE --choose 0 --script SCRIPT --workdir DIR` | Prepares candidate index 0 for review; does not submit. |
| `tower run scaling analyze FILE --mode strong --baseline 1` | Summarizes comparable measured repeats. |
| `tower run scaling plan RECIPE --workdir DIR` | Prepares bounded repeated-job plans and their forwarded controls. |
| `tower run workflow analyze RECIPE` | Computes conditional times, critical paths, slack, and resource envelopes. |
| `tower run workflow plan RECIPE --workdir DIR` | Adds individual script preflights with symbolic dependencies. |

Candidate indices are **zero based** and retain their original positions even
when another candidate is invalid. In the interactive command palette, type:

```text
tradeoffs examples/planning/observations.json
choose 0 examples/research/sample.sbatch --workdir /absolute/checkout
```

The choice opens the Submit view. Inspect its exact `sbatch` arguments and
preflight issues, then use the existing `submit` command and confirmation if you
intend to submit that one job. The prediction and comparison do not themselves
authorize or trigger submission.

## Planning bundle

A `tower.planning` bundle can supply several views from one frozen snapshot:

```json
{
  "version": 1,
  "kind": "tower.planning",
  "simulated": false,
  "coverage": 0.8,
  "query": {
    "name": "my-analysis",
    "partition": "main",
    "cpus": 4,
    "nodes": 1,
    "gpus": 0
  },
  "jobs": [],
  "history": [],
  "observations": [],
  "candidates": [],
  "scaling": [],
  "workflow": {
    "version": 1,
    "kind": "tower.workflow",
    "nodes": [{"id": "analysis", "runtime_seconds": 120}]
  }
}
```

Supply the relevant fields rather than empty arrays for actual analysis.
`job_id` selects a job in `jobs`; otherwise pending-job views prefer a pending
job. `details` can map job IDs to captured `scontrol` fields. `partitions`,
`nodes`, `share`, and `health` supply optional blocker evidence. `queue_history`
is separate historical start evidence for tradeoffs. `now` can freeze the
forecast clock for a reproducible historical example. A bare history array is
also accepted by `predict`, and a bare candidate array by `tradeoffs`.

The supplied [observations example](../examples/planning/observations.json) is a
**simulated frozen snapshot**, not a measurement of CARC. Its values demonstrate
the presentation and schema. Replace them with identified runs and timestamps
from your own workload before drawing operational conclusions.

## Resource predictions

Predictions compare exact workload/allocation cohorts. The core identity is
`name`, `partition`, total `cpus`, `nodes`, and total `gpus`. Optional identity
fields include `gpu_type`, `account`, `qos`, requested `mem_bytes` and
`time_seconds`, `script_sha256`, `input_size`, and scientific `parameters`.
Different optional identities are not pooled when the query cannot resolve
them unambiguously.

A measured history row can look like:

```json
{
  "id": "12345",
  "name": "my-analysis",
  "partition": "main",
  "state": "COMPLETED",
  "cpus": 4,
  "nodes": 1,
  "gpus": 0,
  "mem_bytes": 8589934592,
  "time_seconds": 3600,
  "runtime_seconds": 912.4,
  "cpu_seconds": 2780.1,
  "memory_bytes": 2684354560,
  "memory_scope": "job_peak",
  "start": 1700000000,
  "end": 1700000912.4,
  "parameters": {"dataset": "v3", "iterations": 100}
}
```

`mem_bytes` describes the request; `memory_bytes` describes the observation.
They are different quantities. A `memory_bytes` observation requires one of:

| Memory scope | Meaning |
| --- | --- |
| `job_peak` | An explicitly measured peak across the job allocation. |
| `per_node_peak` | A node-level peak; retain this scope when interpreting it. |
| `max_task_rss` | Maximum task RSS, including ordinary `sacct` MaxRSS evidence. |

Maximum task RSS is not total job memory. Multiplying individual peaks by a
rank or node count does not establish a simultaneous allocation-wide peak.
Raven withholds a memory interval when scopes are unknown or mixed and does
not turn task RSS into a safe `--mem` recommendation.

```bash
tower run predict --file examples/planning/observations.json --coverage 0.8
tower run predict --file history.json --name my-analysis --partition main \
  --cpus 4 --nodes 1 --gpus 0 --account my_account --qos normal
```

Only specify account/QoS or other identity fields when the history records prove
those values. A changed name, script, input size, allocation, or parameter set
needs its own comparable evidence.

The predictor uses a training median and split-conformal absolute residuals.
The requested coverage must be strictly between zero and one. By default it
needs at least 10 compatible samples; a high requested coverage can require
more calibration samples. With at least 20 samples, the newest fifth is held
out separately for observed coverage. Verified end times determine chronological
order; missing times leave the input order explicit in the result.

An interval describes a future observation **under exchangeability within that
cohort**, not a confidence interval for a population mean. It is not a guarantee
that a resource limit will suffice. Each metric has its own sample count,
validation status, and observed coverage. Three 80% intervals do not establish
80% joint coverage across runtime, CPU consumption, and memory.

TIMEOUT and OOM observations are censored evidence. Their measured quantities
provide lower bounds rather than completed runtimes. Raven retains them and
withholds predictive intervals instead of fitting an optimistic completed-only
cohort. Conflicting duplicate run IDs, ambiguous workloads, failed recent
validation, and insufficient calibration are surfaced in the result.

## Queue forecasts

The scheduler's current `squeue --start` projection is useful evidence. Raven
labels it `scheduler_uncalibrated` until comparable, recorded **pre-start**
predictions have actual outcomes. It does not manufacture a calibrated interval
from the current point alone.

A pending job uses ordinary job fields plus `submit` and optional `est_start`:

```json
{
  "id": "23456",
  "name": "my-analysis",
  "state": "PENDING",
  "reason": "Resources",
  "partition": "main",
  "cpus": 4,
  "nodes": 1,
  "gpus": 0,
  "mem_bytes": 8589934592,
  "time_seconds": 3600,
  "submit": 1700000000,
  "est_start": 1700000600
}
```

Recorded forecast outcomes use `job_id`, the same cohort fields, `submit`,
`issued_at`, `predicted_start`, and `actual_start`. Optional
`first_issued_at`/`first_predicted_start` and revision fields retain the first
and latest issued projection. Predictions issued after the actual start do not
calibrate a forecast. Repeated snapshots and job steps do not become independent
outcomes. A job ID plus submission timestamp identifies a scheduling episode.

```bash
tower run forecast --file examples/planning/observations.json --coverage 0.8
tower run forecast 23456
```

Scheduler calibration requires at least **20 independent submitted-job
outcomes** with comparable known CPU/node/GPU counts, requested memory and
walltime, partition, and any known account/QoS/workload fields. GPU requests also
need a known GPU type; an explicit empty type means a recorded generic request.
Missing allocation evidence keeps the scheduler point uncalibrated.

Calibration also matches the forecast lead band:

| Lead between issue and predicted start | Band |
| --- | --- |
| 0 through 60 seconds | Immediate |
| More than 60 through 300 seconds | Up to 5 minutes |
| More than 300 through 900 seconds | Up to 15 minutes |
| More than 900 through 3,600 seconds | Up to 1 hour |
| More than 3,600 through 21,600 seconds | Up to 6 hours |
| More than 21,600 through 86,400 seconds | Up to 1 day |
| More than 86,400 seconds | Longer lead |

Each submitted job contributes one latest comparable pre-start point in the
current band; the first point can remain useful when a later revision moves
into a different band. Empirical coverage evaluates chronological outcomes
using calibration evidence available earlier. A `null` empirical coverage means
there are not yet held-out coverage trials.

When no scheduler estimate exists, the historical fallback conditions on how
long the current job has already waited. It requires at least **40 comparable
observed waits longer than that queued age**, with separate chronological halves
for training and calibration. Exhausted history yields insufficient evidence,
not an invented imminent start.

A passed scheduler projection is `stale`. Holds, unresolved dependencies, and
policy blockers suppress a queue estimate. Mixed naive and timezone-aware
timestamps are rejected; consistent naive Slurm timestamps assume the process's
local timezone and carry a warning. Use epoch seconds or timezone-aware ISO
timestamps when exporting observations.

Queue demand, scheduler policy, reservations, and selective history can change.
The statistical assumptions and revision history remain visible; a displayed
start range is not a scheduler promise.

## Scheduler blockers

Blockers explains the reported scheduler reason and links supporting observations
to evidence IDs. It can show dependency clauses and predecessor states, partial
placement screening, source freshness, and matched fairshare information.

```bash
tower run blockers 23456
tower run blockers --file examples/planning/observations.json
```

Dependencies support documented AND/OR forms and common Slurm kinds including
`afterok`, `afterany`, `afternotok`, `after`, `aftercorr`, and `singleton`.
Missing predecessor records remain external/unknown; they may belong to another
user or fall outside the available history. Cycles and unsupported or mixed
syntax are shown rather than silently evaluated.

`Resources` alone does not establish that CPUs, GPUs, memory, or topology caused
the delay. Placement checks need concrete per-node evidence: aggregate free
resources can be fragmented. Missing features, reservations, exclusivity,
sharing policy, or stale data keep the conclusion conditional. Fairshare is one
priority component and does not establish queue position or a start time.

Suggested checks explain what to inspect; the view never clears a dependency,
releases a hold, or changes an allocation.

## Submission tradeoffs

Declare alternatives for the same scientific work. `cpus`, `gpus`, and
`mem_bytes` are allocation totals; `time_seconds` is requested walltime.

```json
{
  "candidates": [
    {"label": "4 CPUs", "name": "my-analysis", "partition": "main", "cpus": 4, "nodes": 1, "gpus": 0, "mem_bytes": 8589934592, "time_seconds": 3600},
    {"label": "8 CPUs", "name": "my-analysis", "partition": "main", "cpus": 8, "nodes": 1, "gpus": 0, "mem_bytes": 8589934592, "time_seconds": 3600}
  ],
  "history": [],
  "queue_history": []
}
```

History must establish each allocation's own compatible runtime cohort. More
CPUs or GPUs do not imply an inverse-linear speedup. Optional
`estimated_runtime_seconds` is a caller-supplied assumption, explicitly labeled;
its point bounds have no empirical coverage.

```bash
tower run tradeoffs examples/planning/observations.json
tower run tradeoffs examples/planning/observations.json --choose 0 \
  --script examples/research/sample.sbatch --workdir "$PWD"
```

The comparison considers runtime, requested core/GPU reservation hours, and
completion time when both queue and runtime evidence exist. Reservation hours
are requested counts multiplied by requested walltime, not consumed resources
or CARC billing. Queue and runtime bounds may be summed conservatively; no
joint probability coverage is claimed for their sum.

Pareto relations compare matching work definitions and known objective scopes.
Different script/input/parameter identities are separate comparison groups.
Unknown dimensions cannot establish dominance. A separated interval relation
is conditional on its supplied bounds and assumptions, not a guarantee of
which real run will finish first. Poorly validated intervals cannot establish
dominance.

Preparing a choice uses **one task per node**, with total CPUs divided evenly
across the nodes and total memory rounded up to MiB per node. CPU totals must
divide evenly by node count. Conflicting inherited task/GPU/memory directives
need reconciliation in the script. This topology is explicit; choose a request
that matches your workload rather than treating it as an MPI launcher.

## Controlled scaling

[scaling.json](../examples/planning/scaling.json) prepares a small CPU-only
strong-scaling experiment using the supplied synthetic workload. It fixes the
problem size and scientific parameters, varies workers, and repeats each
configuration. The plan contains exact resource flags, unique log paths, and
the controls the workload must consume.

```bash
tower run scaling plan examples/planning/scaling.json --workdir "$PWD"
tower run scaling analyze examples/planning/observations.json --mode strong --baseline 1
```

A recipe has `version: 1`, `kind: "tower.scaling"`, `script`, `mode`,
`configurations`, and a positive `problem_size`. Optional fields include
`workdir`, `name`, `baseline`, `repeats`, `seed`, `max_runs`, and scalar
scientific `parameters`. Each configuration declares `workers` and can declare
`label`, `cpus_per_task`, `nodes`, total `gpus`, `parameters`, and `problem_size`.
Worker counts and labels must be unique. Scientific parameters remain identical
across configurations. Strong scaling fixes problem size; weak scaling requires
exact proportional problem size per worker.

The script receives:

```text
TOWER_SCALING_WORKERS
TOWER_SCALING_REPEAT
TOWER_SCALING_SEED
TOWER_SCALING_CONFIGURATION
TOWER_SCALING_PROBLEM_SIZE
TOWER_SCALING_EXPERIMENT_ID
TOWER_PARAM_<UPPERCASE_PARAMETER_NAME>
```

Forwarding controls does not prove that your application uses them. The supplied
[workload](../examples/planning/scaling_workload.py) reads these controls; adapt
your real workload deliberately. Parameters are bounded scalar values with
environment-safe names, no secret-like names, reserved scheduler controls, or
commas. Seeds are paired across configurations by repeat number.

Measure the small toy workload on a development machine without Slurm. Use Slurm
compute allocations for your real scaling experiments on CARC. Use fresh output
paths for the local demonstration:

```bash
.venv/bin/python examples/planning/scaling_workload.py local \
  --workers 1 --problem-size 10000 --repeat 1 --output one.json
.venv/bin/python examples/planning/scaling_workload.py local \
  --workers 2 --problem-size 10000 --repeat 1 --output two.json
.venv/bin/python examples/planning/scaling_workload.py combine \
  one.json two.json --output measurements.json
tower run scaling analyze measurements.json --mode strong --baseline 1
```

Run at least three distinct repeats per worker count to describe observed spread.
Existing outputs are refused; collection uses explicit paths instead of a
recursive filesystem walk. Local runs leave unmeasured CPU/GPU allocations
unknown. Startup overhead is included. The synthetic workload is useful for
testing the flow, not evidence about your application's scaling on CARC.

An analysis record needs positive `workers`, `runtime_seconds`, `problem_size`,
verified `script_sha256`, scientific `parameters`, `state: "COMPLETED"`, and
an identifying `job_id` or `repeat`. Recorded total `cpus`/`gpus` support resource
hours; they are not inferred from worker count. Optional work fingerprints can
further distinguish experiments. Duplicate or conflicting run identities are
not independent repeats.

Strong-scaling speedup is baseline median time divided by the configuration
median; efficiency divides that speedup by the worker ratio. Weak-scaling
efficiency compares medians for proportional problem sizes; no strong-scaling
speedup is reported. Runtime bands are **empirical repeat quantiles**, not
confidence intervals or future-run prediction intervals. Changing scientific
work, missing comparability metadata, truncated observations, or a failed
baseline suppresses relative claims. Failed/censored observations suppress
relative claims for affected worker groups instead of disappearing from the
analysis. An observed timing ratio does not prove a causal effect of resources.

## Workflow critical paths

The workflow planner models a bounded DAG whose dependencies all mean
**AND-success**, equivalent to an `afterok` planning relationship. It assumes
immediate capacity and successful predecessors. Queue delays, scheduler
priority, retries, data staging, and real resource contention are not inferred.

```bash
tower run workflow analyze examples/planning/workflow.json
tower run workflow plan examples/planning/workflow.json --workdir "$PWD"
```

The example is an illustrative fork/join graph that reuses the independent toy
script. Replace its scripts and real data dependencies for your own workflow.
Its declared makespan is 85/145/265 seconds across lower/nominal/upper duration
scenarios; those are illustrative estimates, not measurements.

```json
{
  "version": 1,
  "kind": "tower.workflow",
  "name": "My workflow",
  "deadline_seconds": 900,
  "nodes": [
    {"id": "prepare", "script": "prepare.sbatch", "runtime_seconds": 30, "resources": {"cpus_per_task": 1}},
    {"id": "analysis", "script": "analysis.sbatch", "depends_on": ["prepare"], "duration": {"lower": 120, "estimate": 180, "upper": 300}, "resources": {"cpus_per_task": 4, "ntasks": 2, "nodes": 1, "gpus": 0}}
  ]
}
```

Use one positive `runtime_seconds` estimate or an ordered positive `duration`
object with exactly `lower`, `estimate`, and `upper`. An omitted duration is
unknown, including downstream finish times. Unknown durations withhold global
critical/slack claims instead of being replaced with zero-duration jobs.
Node IDs are unique printable ASCII identifiers; dependencies must refer to
declared nodes. Cycles, unknown references, unsupported fields, and mutually
exclusive declarations fail before any script inspection.

`resources` accepts positive integer `cpus_per_task`, `ntasks` or
`ntasks_per_node`, `nodes`, nonnegative total integer `gpus`, and bounded Slurm
`mem`, `time`, `partition`, `account`, `qos`, and `constraint` strings. CPU
envelopes need explicit CPU allocation evidence. A single-node task count
defaults to one; multiple nodes need explicit task counts. Missing GPUs are
modeled as zero in this recipe, while script directives can differ and are
checked during preparation. Script requests that contradict the modeled
node/GPU/known-CPU envelope must be declared or reconciled. Inherited arrays and
alternative task/GPU allocation modes outside this model are rejected.

The planner reports earliest starts/finishes, nominal critical nodes and edges,
one deterministic critical path, latest starts, and slack. Dependency-depth
layers describe graph structure, not batches that should be launched together.
The resource envelope sums declared core/GPU/node hours and sweeps the nominal
earliest-start schedule for peak concurrent requests. Simultaneous finishes and
starts share a half-open boundary, so a predecessor does not overlap its
successor artificially.

`lower`/`estimate`/`upper` on node **latest-start and slack** dictionaries name
the duration scenarios, not ordered uncertainty bounds: switching the critical
branch can make these values nonmonotonic. Deadline `slack_interval` instead
provides ordered bounds. A deadline can be an absolute numeric epoch (`deadline`)
or relative seconds (`deadline_seconds`), never both. Its feasibility remains
conditional on immediate capacity, the declared durations, and successful
dependencies. Extreme combinations whose positive scheduling windows collapse
at floating-point precision are rejected; unrepresentable absolute clock
timestamps are unavailable while usable relative times remain explicit.

`workflow plan` validates the entire graph and requires every node's script.
Any script preflight error prevents returning partial plans. Repeated identical
script preflights reuse one detached snapshot. Individual plans retain
`symbolic_dependencies`; they do **not** insert fake scheduler job IDs. These
plans are sealed, marked `submittable: false`, and refused by the submission
layer. The separate orchestration workbench creates concrete plans
only after real upstream scheduler IDs exist, while leaving the offline
planner's sealed documents unchanged.

## Explicit execution handoff and receipts

Run Raven locally on the cluster with persistent state, then enter these
commands after `:`:

```text
orchestrate workflow /absolute/project/workflow.json --workdir /absolute/project
orchestrate scaling /absolute/project/scaling.json --workdir /absolute/project
```

Preparation remains local and offline. The execution review lists every
node/repeat, effective resources, command, and dependency. Arrows/page keys
navigate, Enter or `d` opens details, and Tab focuses visible Cancel/Confirm
controls. Enter on Confirm explicitly starts submissions. SSH viewers, replay,
and `--no-state` cannot start batch execution.

Execution is bounded to 64 reviewed jobs and 16 recorded attempts per node,
with 16 MiB metadata receipts. Workflow dependencies use actual accepted IDs in
`afterok`; scaling repeats keep their original scientific identity. Changed
scripts are revalidated before any new submission. Private receipts under
the configured state's `executions/` directory record durable launching intent before each scheduler
call, actual accepted IDs, definitive rejection, and unknown outcomes. A
failure or uncertain response stops later calls. While running, `c` stops
further submissions; already accepted jobs keep running.

```text
execution /absolute/path/to/receipt.json
execution collect
execution resume
execution retry NODE
execution recover NODE REAL_JOB_ID
```

Collect uses the currently observed queue/accounting records. Missing data stays
missing, and failed measured attempts remain censored in scaling analysis.
Resume, Retry, and Recover each require a new explicit review. Resume submits
only unattempted nodes; an unknown outcome blocks it. Retry permits only a
definitively rejected/not-submitted node, with dependency protection for already
accepted descendants. Recovery checks a supplied real scheduler job against the
exact reviewed submit command and working directory in accounting. If the
required evidence is unavailable, it refuses to infer acceptance or retry.

This handoff does not execute batch scripts on the login node, automatically
rerun failed accepted jobs, or turn duration estimates into measurements. Keep
the receipt for interrupted-session recovery. See the
[workbench execution walkthrough](WORKBENCH.md#execute-a-reviewed-workflow-or-scaling-experiment).

## Bounds, persistence, and direct APIs

| Component | Default or hard bound |
| --- | --- |
| CLI planning JSON | 1 MiB, 100,000 values, depth 32; stable regular UTF-8 file, no duplicate keys/nonfinite numbers/final symlink. |
| Resource prediction | First 10,000 records by default; direct `max_records` accepts up to 100,000. |
| Queue forecasting | First 10,000 history and observation records each by default; direct ceiling 100,000 each. |
| Forecast tracker | 256 pending identities and 4,096 observations by default; bounded retained first/latest revisions. |
| Blockers | 20,000 job/history records, 4,096 nodes, 128 tree entries, depth 12, 96 evidence items. |
| Tradeoffs | 64 candidates and 10,000 records per historical source; caches repeated cohort models. |
| Scaling | 10,000 analysis records, 8 MiB aggregate metadata; at most 128 configurations/runs, 20 repeats, and recipe `max_runs` defaults to 64. |
| Workflow | 512 nodes, 4,096 edges, 2,048 parameter values per node, depth 8, 128 KiB parameter text per node. |
| Explicit execution | 64 reviewed jobs, 16 attempts per node, 16 MiB receipts; separate interactive confirmation and persistent private state. |

The direct `scaling.load` API allows an 8 MiB file with depth 16; CLI recipe
loading keeps the smaller shared file bound. Reaching a bound is reported as a
limit, truncation, incomplete result, or validation error rather than silently
pretending the entire input was analyzed.

Forecast state can persist with Raven's ordinary UI state. The tracker stores
bounded first/latest issued projections and outcomes, not every redraw.
`--no-state` disables state persistence. Replaying backwards clears future
tracker evidence so it cannot calibrate an earlier projection.

For integrations, the pure analysis APIs return JSON-compatible dictionaries:

```python
from tower.predict import predict
from tower.forecast import forecast, ForecastTracker
from tower.blockers import explain
from tower.tradeoffs import compare, prepare_choice
from tower.scaling import analyze as analyze_scaling, plan as plan_scaling
from tower.workflow import analyze as analyze_workflow, load, plans

resources = predict(history, query, coverage=0.8, max_records=10000)
queue = forecast(job, history, observations=issued_outcomes, now=epoch,
                 coverage=0.8, max_records=10000)
blockers = explain(job, jobs=jobs, finished=history, details=details)
choices = compare(candidates, history=history, queue_history=queue_history)
scaling = analyze_scaling(records, mode="strong", baseline=1)
workflow = analyze_workflow(recipe, now=epoch)
```

Increasing record ceilings does not improve cohort comparability or satisfy a
missing measurement scope. Read the result's evidence, validation, and
limitations. The automated suite exercises malformed and censored observations,
deduplication, timestamp leakage, queue lead bands, dependency complexity,
resource semantics, controlled repeats, workflow graph properties, terminal
rendering, and submission guards. Live scheduler behavior and real workload
performance still need validation on CARC.
