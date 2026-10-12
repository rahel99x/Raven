# Project reporting: metrics and data sources

This reference explains what supplies each Raven display, what the numbers
mean, and what a project must publish. Start with the
[reporting guide](../PROJECT_REPORTING_V2.md) for setup and copyable workflows.
Use this page when adding a measurement or explaining a missing graph.
The [file-format cookbook](project-reporting-formats.md) provides complete
producer examples and field-by-field contracts.

The coverage is the current implementation: **10 workspace tabs, 13 Research
views, 5 Analytics views, and 57 operations**. The tables cover every built-in
measurement family and integration route. Application metric names are open
ended; each project defines its own vocabulary. Linked schemas specify the
complete field sets, bounds, and required properties for structured inputs.

## Choose the source that matches the question

| Question | Producer | Integration | Where to look |
| --- | --- | --- | --- |
| Is my job queued, running, or finished? | Slurm controller and accounting | No application hook | Jobs, Recents, History, Timeline |
| How much CPU and memory has this allocation used? | Slurm job accounting | Working `sstat` / `sacct`, with permitted reads | Jobs Inspector; Analytics Job series, History, Advisor, Compare |
| How busy was my GPU? | A recorder inside the GPU job | Exact NVIDIA CSV trace described below | Analytics Job series and the corresponding Jobs/History Series view |
| How far has my computation progressed? | Your application | `metrics.jsonl` plus the matching run inventory | Research Experiment; Jobs progress |
| Is the scientific answer acceptable? | Your application and a declared acceptance policy | Results and acceptance manifests | Research Operations → Acceptance |
| Which rank, phase, or function caused a delay? | Explicit instrumentation or a profiler export | Profiler, trace, or investigation contract | Bottlenecks, Trace, causal-delay and investigation operations |
| What resources should the next comparable run request? | Recorded attempts and an explicit comparison query | Summaries in a planning bundle | Research Resources, Tradeoffs, Scaling |
| What did the lab spend? | Permitted account accounting; optionally measured energy | Account source or an energy manifest | Group/account views; Energy operation |

Publishing `{"metrics":{"gpu_utilization_pct":80}}` creates an **application
metric** in Experiment. It does not feed the native GPU curve. Likewise, a
metric named `cpu_seconds` does not replace Slurm's CPU observations. Each route
retains its identity, units, and measurement scope.

## Identity, publication, and units

Create one `runs/<run_id>/` directory for each execution attempt. Its
[`run.json`](../schemas/run.v1.schema.json) identifies the `run_id`,
`experiment_id`, positive `attempt`, lifecycle `state`, and actual Slurm
`job_id` when present. Array task `12345_7` is distinct from its parent `12345`.
A requeue can reuse the scheduler ID, so preserve a new run ID and attempt.
Local programs without Slurm omit `job_id` and use explicit project/run
selection. Do not invent a scheduler ID to make discovery work.

The account is a separate field. For a student submitting with `--account=LabX`,
the job owner remains the student's user and `resources.account` records
`LabX`. Neither field establishes permission to inspect other lab members'
files. Keep cluster, partition, QoS, allocation, and code/input identity with
the records used in comparisons.

`run.json.paths` binds exact files relative to that attempt directory. Automatic
attachment requires an unambiguous matching job attempt and a known project
root or scheduler WorkDir. File names alone do not establish ownership. See
[attachment and path rules](../PROJECT_STANDARD.md#automatic-attachment-and-runtime-updates).

| Quantity | Reporting convention |
| --- | --- |
| Timestamp | Unix epoch seconds for application metrics and planning timestamps; include fractional seconds when available |
| Duration | Seconds; measure elapsed application work with a monotonic clock |
| CPU time | CPU-seconds over an explicitly stated process/task/allocation scope |
| Memory or I/O amount | Bytes in project summaries and profiler byte fields; native GPU CSV memory is MiB |
| Fraction | 0–1; name percentages explicitly when using 0–100 |
| Counts | Actual counts with a stable definition, such as accepted samples or solver iterations |
| Unknown observation | Omit the application metric; use omission or `null` only where the structured schema permits it |
| Observed zero | Write numeric `0`; it means a measurement was made |

Write complete newline-terminated JSONL or CSV records. Publish JSON manifests
with a temporary file and atomic replacement in the same directory. Use one
coordinator per run; separate independent ranks/tasks into distinct streams
unless a producer explicitly aggregates them. Preserve failed and interrupted
attempts. An exit code, a scientific acceptance result, and Slurm's final state
answer different questions.

## Application metrics and progress

The [`metrics.jsonl` contract](../schemas/metrics.v1.schema.json) accepts this
shape, without a `schema` marker:

```json
{"t":1791820800.25,"step":12,"phase":"solve","metrics":{"residual_l2":0.00032,"throughput_cells_per_second":142500},"progress":{"completed":12,"total":100,"unit":"iterations"}}
```

These are illustrative measurements. Report actual values from the application.

| Field | Meaning and limit | Display |
| --- | --- | --- |
| `t` | Finite nonnegative epoch seconds | Sample time, source age, time axis |
| `metrics` | Object containing at most 64 numeric metric names | Named Experiment curves and latest values |
| Metric name | 1–96 printable characters; keep the same meaning and units across samples | Curve label |
| Metric value | Finite JSON number; negatives are allowed | Observed value; `null`, Boolean, string, NaN, and Infinity are invalid |
| `step` | Optional integer, 0 through 2^63−1 | Application iteration/index |
| `phase` | Optional printable text, at most 160 characters | Current phase; changing it resets progress history |
| `progress.completed` | Nonnegative finite count, no greater than `total` | Completed work |
| `progress.total` | Strictly positive finite count | Total work and completed fraction |
| `progress.unit` | Optional printable text, at most 64 characters | Work label and rate unit |

Raven retains at most 64 distinct names per stream. Keep each line at most
65,536 bytes and use stable names instead of creating a new name for every
rank or iteration. A read can be bounded or truncated; inspect the reader's
status and retained coverage before interpreting a whole-run statistic.

Choose names that describe the measurement:

| Measurement type | Example name | Producer responsibility |
| --- | --- | --- |
| Gauge | `temperature_celsius`, `residual_l2`, `allocated_memory_bytes` | Report the value observed at `t` |
| Cumulative counter | `examples_processed_total`, `io_bytes_written_total` | Keep its scope and reset behavior explicit; a new attempt gets a new stream |
| Rate | `examples_per_second`, `io_bytes_per_second` | Compute a delta over a measured duration; document the interval |
| Duration | `iteration_seconds`, `checkpoint_seconds` | Measure the named operation, with clear inclusive/exclusive scope |
| Fraction or percentage | `accuracy_fraction`, `gpu_utilization_pct` | Use the named scale consistently |
| Peak | `max_task_rss_bytes`, `device_peak_memory_bytes` | State whose peak, over what interval, and how it was measured |

The generic metric reader plots supplied numbers. It does not infer counter
types, differentiate counters, convert units, or infer a GPU/CPU allocation
from a name. Compute those transformations in the producer. Never sum
independently observed peaks and label the result a simultaneous job peak.

With Raven installed in the application's Python environment:

```python
import os
from pathlib import Path
from raven.metrics import write_metric

# The launcher supplies the real attempt directory.
metrics_path = Path(os.environ["RAVEN_RUN_DIR"]) / "metrics.jsonl"
write_metric(metrics_path, {"residual_l2": residual},
             step=iteration, phase="solve",
             completed=iteration, total=iterations, unit="iterations")
```

Here `residual`, `iteration`, and `iterations` are values from your existing
application. The parent directory must already exist. The writer validates and
appends one record, flushes it, and uses POSIX advisory locking for cooperating
writers. The [copyable reporter](../../examples/project-template/reporting.py)
supports the same format without installing Raven in the application.
Other languages can write the JSONL directly.

Bash jobs can report measured shell variables:

```bash
raven report metric --run "$RAVEN_RUN_DIR" \
  --value "residual_l2=$residual" --step "$iteration" --phase solve \
  --completed "$iteration" --total "$iterations" --unit iterations
```

`raven report start` or the reporting launcher establishes the attempt before
these calls; metric writing does not create a Slurm allocation. See the
[Bash integration](../guides/research-automation.md).

Progress ETA needs at least three advancing observations with increasing
timestamps and a consistent total/unit in the same phase. Raven computes
`advance / elapsed` and `remaining / rate`. A phase change, a decreasing count,
a changed total/unit, or a non-increasing timestamp restarts that history.
Missing ETA means the current evidence cannot support the estimate.

Jobs also recognizes numeric `progress_fraction` (0–1), `progress_pct` (0–100),
or the pair `completed_steps` / `total_steps` from the linked application stream.
Prefer the explicit `progress` object. A progress cell prefixed with `t` is the
scheduler elapsed/time-limit ratio, not application completion.

## Native CPU, memory, GPU, and accounting measurements

These observations come from the selected scheduler connection and retained
samples. They require no generic application-metric hook.

| Metric | Source and computation | Unit and interpretation | First useful data / missing meaning |
| --- | --- | --- | --- |
| Live CPU time | `sstat` `AveCPU × NTasks`, summed once per distinct application step; `.extern` is excluded | CPU-seconds; includes observed batch and numeric application steps | One valid observation with task counts; incomplete CPU evidence stays unknown |
| CPU rate / CPU per core | Difference in observed CPU time divided by elapsed sample time and allocated CPUs | Fraction internally, percent in the UI; 100% means full average use of each allocated CPU | Two usable observations at least five seconds apart, with a nondecreasing CPU counter; the chart can use cumulative efficiency before a rate exists |
| CPU efficiency / EFF | CPU time divided by job elapsed seconds × allocated CPUs | Fraction internally, percent displayed | CPU total, elapsed time, and allocation count |
| Peak RSS | Largest observed `MaxRSS` across returned steps/tasks | Bytes; an individual task's resident-memory high-water mark | A memory observation can appear even when CPU data is unavailable |
| Memory percentage / MEM EFF | Peak RSS divided by normalized requested allocation memory | Percent; compares a task peak with the request, not measured simultaneous allocation-wide occupancy | Needs a known request; Series otherwise displays RSS divided by 2^30, in GiB despite the current `GB`/`G` label |
| Step memory detail | `MaxRSS`, `MaxRSSTask`, `MaxRSSNode`, `AveRSS` | Bytes, rank/task identity, node; average and maximum have different scopes | Available step fields; unavailable fields cannot establish a job total |
| Step CPU detail | `AveCPU`, `NTasks`, `MinCPU`, `MinCPUTask`, `MinCPUNode`, or finished-step `TotalCPU` | Durations, task count, and the least-CPU task location | Live `sstat` or finished `sacct -j`; low CPU time alone does not prove a slow rank |
| Finished CPU time | `sacct` `TotalCPU` | CPU-seconds | Accounting may lag the job leaving the queue |
| Runtime and time remaining | Scheduler `Elapsed`, `Start`, and requested time limit | Seconds or formatted duration | Pending jobs have no measured execution duration |
| Queue wait | Actual `Start − Submit` | Seconds; Analytics history histogram uses minutes | Both timestamps are required; an estimated start is a different field |
| Core-hours | Elapsed seconds × allocated CPUs / 3600 | Allocated CPU-hours | Resource-time consumption, including idle allocated CPUs |
| GPU-hours | Elapsed seconds × allocated GPUs / 3600 | Allocated GPU-hours | Resource-time consumption, not observed GPU activity or energy |
| GPU rate / utilization | Valid device utilization from the job's trace or verified native samples | 0–100% per device | At least one recorded observation; allocation count is insufficient |
| GPU observed busy mean | Arithmetic cumulative mean of valid retained utilization samples for that device | Percent; an activity proxy | Missing samples are excluded; this is not time-weighted full-job efficiency, FLOP utilization, or scientific throughput |
| GPU memory | Native device `used`/`total`, or trace `mem` | MiB; native percentage needs both used and total | The minimal trace has used memory only; its memory column does not add a separate native GPU-memory rate chart |
| GPU inventory | Allocation TRES/GRES and node inventory | Device count/type; optional verified vendor, UUID/BDF and partition | Describes allocation/identity, not a utilization measurement |

Slurm job accounting must be configured at the site. Its sample cadence is
independent of Raven's read cadence. `JobAcctGatherType` and
`JobAcctGatherFrequency` explain CPU/RSS availability; cgroup and runtime
support determine which fields exist. No package upgrade can recreate CPU or
memory curves that were never sampled. Completed-job accounting can still
supply final totals and task peaks without a historical time series.

### NVIDIA GPU CSV trace

Managed `raven`, `ravenl`, and `ravenh` use existing telemetry and do not start
automatic GPU sampling steps. A job-side recorder must write:

```text
<Slurm WorkDir>/logs/gpu-util-<exact job_id>.csv
```

For an array task, use `gpu-util-12345_7.csv`. This location is relative to the
scheduler's actual WorkDir, **not** implicitly to the selected run directory.
Keep the file after the job ends if completed-job inspection matters.

```csv
2026/10/12 14:30:00.000, 0, 83, 6120
2026/10/12 14:30:05.000, 0, 91, 6400
```

| Column | Required content |
| --- | --- |
| 1 | NVIDIA timestamp, `YYYY/MM/DD HH:MM:SS[.fraction]`; the current reader uses whole seconds in its local timezone |
| 2 | Nonnegative integer device index |
| 3 | Utilization percentage, 0–100; unavailable/nonfinite readings remain unmeasured |
| 4 | Optional nonnegative used GPU memory in MiB |

Use `nvidia-smi --query-gpu=timestamp,index,utilization.gpu,memory.used
--format=csv,noheader,nounits` inside the allocated job, with a producer cadence
suited to the workload. See the complete
[recorder and cleanup example](../guides/gpu-detection.md#provide-an-optional-gpu-trace).
Only sample devices assigned to the job. This index-based format suits a single
node; duplicate indices from multiple nodes cannot be merged into an unlabeled
file. A different login/compute-node timezone also requires a consistent
producer/reader clock convention.

Native Job series displays up to four device identities, with a rate and busy
mean curve for each. A slower Raven read does not increase or decrease the
writer's sampling resolution in the retained file. GPU CSV curves appear in
Job series; Analytics Compare currently uses native retained GPU series and
does not import the CSV as a comparison series. An application metric or a site
telemetry operation also does not automatically populate that comparison.

Slurm `gres/gpumem` and `gres/gpuutil` TRES depend on working site GPU accounting
such as NVIDIA NVML. The `TRESUsageIn*` values aggregate high-water marks across
ranks; they cannot replace instantaneous per-device curves. An RTX driver can
support this CSV producer even when Slurm has no NVML plugin. See
[GPU diagnosis](../guides/gpu-detection.md#managed-raven-collection).

## Every Analytics view

The same Series content can appear inside Jobs or History Details.

| View / ID | What it contains | Required inputs and interpretation |
| --- | --- | --- |
| Job series / `job` | CPU per core, memory/request or RSS, per-device GPU rate and observed busy mean; sample count/span and chart coverage | Retained live samples and/or the selected job's GPU trace. A blank CPU rate can coexist with a measured memory sample. Completed jobs need retained observations. |
| History / `history` | Daily completed/failed/cancelled counts, daily core-hours and GPU-hours, core-hours by partition, queue-wait distribution and median, CPU-efficiency distribution and mean | Accounting rows in the selected time window. Counts describe scheduler outcomes; GPU-hours are allocation time. Missing timestamps/CPU totals exclude those rows from the corresponding statistic. |
| Timeline / `timeline` | Queued and execution intervals for active and accounting jobs | Actual submit/start/end times; pending jobs do not acquire a fabricated execution interval. |
| Advisor / `advisor` | Runs grouped by job name; observed peak/request memory, CPU efficiency/count, longest elapsed/requested walltime, suggested requests, idle core-hours, flags | History and current observations. Suggestions are heuristics with headroom; grouping by name does not establish controlled scientific comparability. Use Research Resources for an explicit cohort. |
| Compare / `compare` | Up to six selected jobs: state, elapsed, CPUs, CPU mean/max, peak memory and memory percentage, GPU mean, core-hours, sample count; CPU/RSS/GPU curves | Retained native series; memory can fall back to accounting RSS. Curves align on each job's **first recorded sample**, not necessarily its real start. Mean/max summarize observed samples, with missing coverage retained. |

The sampling controls display requested and effective polling intervals. Moving
a chart's display window or zoom does not recollect past measurements.
[Chart controls](../guides/charts.md) explain retained windows, gaps, and source
age; [collection limits](../raven/README.md#collection-and-retention) explain
managed polling floors and demand gating.

### Chart statistics and presentation

The full-screen metric chart's selected interval can show valid/sample counts,
missing count, minimum, maximum, arithmetic mean, median, P05, P95 and P99.
Percentiles interpolate between sorted observed values. These statistics keep
the metric's units and describe the retained selected observations; they are
not independent-run confidence intervals.

Sample coverage is valid observations divided by supplied observations. Time
coverage counts adjacent known samples separated by at most 2.5 times the
median positive cadence of the retained source, divided by the selected span.
A single observation has no measurable interval coverage. Long gaps stay gaps.
The source footer reports source age, visible/retained point counts, gaps and
effective polling. A logarithmic display leaves nonpositive values undefined;
changing axis units or precision is presentation, not a producer conversion.

Heatmaps, filled companion curves, sparklines and resource gauges reuse the
same observations. They need no additional hook. Running charts can display a
buffered history edge, independently of collection and current-value readouts;
the reported display lag is not scheduler response time. See
[buffered graphs](../guides/charts.md#read-buffered-running-graphs).

## Every Research view

`RUN` below is the selected `PROJECT/runs/<run_id>`. Declare only files that the
project actually produces. Per-view planning paths take precedence over
`paths.planning`; a declared missing file is reported missing.

| View / ID | Hook or producer | Measurements/evidence shown | Minimum useful evidence |
| --- | --- | --- | --- |
| Experiment / `experiment` | `RUN/metrics.jsonl`, bound by `paths.metrics` | All application metric names, latest values, phase, progress fraction, rate, conditional ETA | One valid complete record; several advancing progress samples for ETA |
| Arrays / `arrays` | Actual scheduler array rows; optional [`array_manifest`](../schemas/array-manifest.schema.json) | Task identity/state and scientific labels/parameters; exact selected task's run evidence | Exact cluster and array parent/task identity; each task has its own run |
| Evidence / `evidence` | Scheduler job/details, linked metric file, declared logs | Cited observations and log excerpts used to explain an outcome | Relevant recorded facts; no `diagnosis.json` or inferred scientific success |
| Artifacts / `artifacts` | [`outputs.v1.json`](../schemas/output-contract.v1.schema.json), or run `paths.contract`, and exact output files | Required/optional existence, byte bounds, format, JSON keys, CSV rows/columns, declared hashes | Contract plus actual files; missing final output can be expected while running |
| Passport / `passport` | Genuine Raven passport in declared `paths.passports` | Recorded code/input/environment identity and provenance comparisons | Native immutable record; `run.json` is not a passport |
| Submit / `submit` | `paths.submit`: intact [`raven run prepare` output](../schemas/submission-plan.v1.schema.json) | Parsed requests, preflight issues, identity and review digest | Real captured plan; discovering a plan never submits it |
| Resources / `predict` | `paths.predict` or shared planning source | Comparable observed runtime, memory, CPU distributions/intervals; coverage and abstention | Selected history plus concrete workload/allocation query and sufficient comparable observations |
| Forecast / `forecast` | `paths.forecast` or shared planning source | Scheduler start estimate, issued forecast evidence, historical outcomes and calibration when supported | Actual pending job and timestamps; pre-start observations for validation |
| Blockers / `blockers` | `paths.blockers` or matching scheduler snapshot | Reasons, dependencies, limits, capacity and source-health evidence | Exact selected job and observed scheduler facts; omitted facts stay unknown |
| Tradeoffs / `tradeoffs` | `paths.tradeoffs` or shared planning source | Explicit candidate resource choices, measured runtime/queue comparisons, uncertainty and unsupported candidates | Comparable history, candidates, and real queue outcomes where needed |
| Scaling / `scaling` | `paths.scaling`: measured records or native scaling recipe | Strong/weak scaling, runtime/repeat spread, speedup/efficiency and cost quantities supported by the selected records | Controlled workload/worker counts and actual independent repeats; a recipe alone is only a plan |
| Workflow / `workflow` | `paths.workflow`: native workflow recipe or planning bundle | Dependency DAG, declared duration scenarios, supported critical-path analysis and proposed launch order | Explicit dependencies and duration estimates; missing durations stay unknown |
| Operations / `operations` | Explicit operation form and its native contract | Scientific results, diagnostics, services, provenance and reviewed actions listed below | The selected operation's required evidence; a directory of arbitrary JSON is not auto-loaded |

Publish an existing valid per-view document with
`raven report research VIEW SOURCE --run "$RAVEN_RUN_DIR"`, or use the copied
reporter's `publish_research(run, view, document)`. Supported publication IDs
are `planning`, `predict`, `forecast`, `blockers`, `tradeoffs`, `scaling`,
`workflow`, and `submit`. Other Research views use their own contracts above;
there is no generic `raven report research operations` hook.

See the [planning schema](../schemas/planning.v1.schema.json) and
[planning interpretation guide](../WAVE_TWO.md) for every query, candidate,
observation, recipe, and coverage field.

### Derived planning measurements

| View | Output quantities and units | Interpretation |
| --- | --- | --- |
| Resources | `runtime_seconds`, `memory_bytes`, `cpu_seconds`: estimate/lower/upper, sample and calibration/validation counts, observed coverage; censored lower bounds | Seconds, bytes, CPU-seconds. Exact workload/allocation identity and memory scope determine the cohort; failure evidence can prevent an interval. |
| Forecast | Predicted/lower/upper start epochs, wait and queued seconds, sample counts, issued-estimate revisions and held-out calibration coverage | A scheduler estimate and calibrated uncertainty require their own evidence. An application runtime estimate cannot establish a queue start. |
| Blockers | Observed reason/dependency/access constraints; available node/CPU/GPU capacity and relevant limit quantities | Screening against the same snapshot, not a guaranteed scheduling decision or a simulated exact start. |
| Tradeoffs | Runtime/wait/completion seconds; reserved core-hours, GPU-hours, and memory GiB-hours; certain/possible domination and frontier membership | Boundaries and assumptions accompany every candidate. Unsupported dimensions remain unknown; frontier status is conditional on supplied comparable bounds. |
| Scaling | Worker/sample counts, median and empirical runtime bounds, speedup, efficiency, problem size, median/total core-hours and GPU-hours | Ratios depend on strong/weak scaling and the explicit baseline; spread comes from observed independent repeats, not assumed speedup. |
| Workflow | Earliest/latest start/finish and node slack, critical nodes/edges/path, makespan/completion scenarios, deadline slack/feasibility, peak concurrent jobs/CPUs/GPUs/requested nodes, total core/GPU/node-hours | Durations are declared lower/estimate/upper scenarios. Resource totals come from the recipe; immediate unlimited scheduler capacity and successful predecessors are assumed. Queue delays are excluded. |

Workflow scenario bounds are not calibrated confidence intervals. A recipe may
use measured durations as its basis, but Raven does not certify that provenance
or turn a plan into an observed execution. Predicted resource costs likewise
remain separate from accounting charges.

## Final summaries and reusable analysis evidence

The complete [`raven.summary/v1` schema](../schemas/summary.v1.schema.json)
contains these field families. `schema`, `id`, `name`, and `state` are required;
unknown optional quantities may be omitted or `null` where permitted. A summary
is final per-attempt evidence, not a sampled time series.

| Fields | Meaning and units | Consumer |
| --- | --- | --- |
| `id`, `job_id`, `experiment_id`, `attempt`, `project_id` | Immutable run ID; actual scheduler ID; logical experiment; retry number; project identity | Attachment, deduplication, selected cohorts |
| `name`, `state`, `exit_code` | Stable workload name, observed lifecycle state, application exit outcome | Inventory and comparable-run analysis; retain failures |
| `partition`, `account`, `qos`, `gpu_type` | Recorded scheduling scope | Cohort/query matching; account is the charged lab allocation |
| `cpus`, `nodes`, `gpus` | Known allocation totals | Resource matching and cost; `gpus: 0` means verified CPU-only |
| `mem_bytes`, `time_seconds` | Total requested allocation memory in bytes; requested walltime in seconds | Request/observation comparisons |
| `runtime_seconds`, `cpu_seconds` | Measured execution duration and scoped CPU-seconds | Resource predictions and scaling |
| `memory_bytes`, `memory_scope` | Measured memory in bytes; `job_peak`, `per_node_peak`, or `max_task_rss` | Memory predictions within compatible scopes |
| `submit`, `start`, `end` | Actual epoch timestamps | Queue/execution chronology |
| `script_sha256`, `fingerprint`, `parameters`, `input_size` | Declared code/workload/input identity; project-defined input-size unit | Comparable cohort selection; do not pool changed science implicitly |
| `workers`, `problem_size`, `repeat` | Controlled worker count, problem-size definition and independent repeat number | Explicit scaling analysis |
| `work_units` | Optional declared useful-work quantity with a project-defined unit | Retained and validated metadata; the current scaling reader does not derive throughput from this field |
| `results` | Project-defined final scientific values with documented units | Retained project result metadata; native prediction/scaling do not interpret this object |
| `metadata` | Measurement sources, scope descriptions and other bounded descriptive provenance | Explanation and export; no implicit numeric interpretation |

Normalize Slurm memory requests carefully: `--mem` is normally per node;
`--mem-per-cpu` requires the allocated CPU count. `os.cpu_count()` and MPI rank
count do not establish total allocated CPUs. A process's `process_time()` does
not include all child processes or all ranks.

Aggregate selected summaries into `raven.planning` `history`; include an
explicit query and actual coverage. `raven report aggregate` creates the
standard project's aggregate from its eligible terminal attempt summaries;
the copied reporter supports explicit run selection for controlled cohorts.
Automatic discovery reads reports and does not construct a comparison cohort.
Use actual independent repeats, retaining failures, timeouts, and OOMs. Three
or more comparable repeats per worker configuration permit observed repeat
spread; this does not by itself establish broader statistical validity.

For scientific acceptance, explicitly publish
[`raven.results/v1`](../schemas/science-results.schema.json):
`runs` with unique run `id` and numeric `metrics`. Supply the separate
[acceptance policy](../schemas/science-acceptance.schema.json) naming required
measurements and tolerances. Copying a value into summary `results` alone does
not run acceptance or create an acceptance manifest.

## Every operation and its data contract

Open `:ops KEY`, supply the listed source, and select **Inspect / prepare**.
The [generated operation reference](operations.md) lists all current input
fields, choices, and defaults. An operation may also need a running verified
allocation, a supported Heavy adapter, site permission, or separately reviewed
execution. Supplying data does not grant those capabilities.

### Scientific measurements, profiling, and infrastructure

| Operation key | What to supply and what Raven derives | Complete contract or implementation guide |
| --- | --- | --- |
| `acceptance` | Required metric names, reference/tolerances, and actual per-run numeric results; pass/fail/missing evidence | [Policy](../schemas/science-acceptance.schema.json), [results](../schemas/science-results.schema.json), [science guide](../guides/operations-science.md) |
| `statistics` | Independent units or exact pairs in a stated measurement unit; differences and Student t uncertainty | [Statistics](../schemas/science-statistics.schema.json) |
| `environment` | Pinned Python/native/container requirements and observed runtime; matching/missing/incompatible evidence | [Environment](../schemas/science-environment.schema.json) |
| `placement` | Exact running allocation and existing process placement; CPU affinity, NUMA, GPU visibility | [Placement](../guides/operations-science.md#verify-cpu-numa-and-gpu-placement-ops-placement) |
| `reuse` | Requested identity, input/output hashes and independently checked acceptance; verified reusable outputs | [Identity](../schemas/science-identity.schema.json), [reuse](../schemas/science-reuse.schema.json) |
| `dependency-repair` | Exact pending attempts and desired dependency expressions; dependency closure and cycle checks | [Repair](../schemas/science-dependency-repair.schema.json) |
| `bottlenecks` | Rank/phase profiler JSON or supported Darshan POSIX text; slowest rank, skew, MPI/I/O fractions, byte rates | [Profiler](../schemas/profiler.schema.json), [export guide](../guides/operations-scale.md#distributed-bottleneck-explorer) |
| `energy` | Every attempt's energy boundary, attribution, coverage and accepted work; joules and joules/accepted unit | [Energy](../schemas/energy.schema.json), [interpretation](../guides/operations-scale.md#energy-per-useful-result) |
| `incidents` | Exact nodes/time window and permitted node events; overlapping nodes and duration | [Incidents](../schemas/incidents.schema.json) |
| `clusters` | Explicit connection/profile endpoints; isolated queue/accounting snapshots | [Cluster workspace](../schemas/cluster-workspace.schema.json) |
| `workflow-engine` | Native Nextflow TSV or supported Snakemake DAG/runtime snapshot; task states/attempts and explicit available dependencies | [Workflow snapshot](../schemas/workflow-engine.schema.json), [native fields](../guides/operations-services.md#native-workflow-observer--a11) |

Profiler records are unique `(rank, phase)` observations with optional `node`,
`elapsed_s`, `cpu_s`, `mpi_s`, `io_s`, `read_bytes`, and `write_bytes`. Durations
are seconds and byte counts are bytes. MPI and I/O wall durations must be
exclusive measurements bounded by elapsed time; their fractions can overlap
and must not be added. CPU time can exceed elapsed time for threaded work.
Provide `expected_ranks` to expose missing coverage. Only complete expected
rank/phase coverage plus `ordered_barrier_phases=true` permits a sum of phase
maxima; this is a declared barrier envelope, not an asynchronous critical path.

The Nextflow reader uses `task_id`, `status`, and optional `native_id`, `name`,
`process`, `hash`, `attempt`, and `exit`; it does not import additional CPU,
memory, or duration columns as Analytics metrics. Nextflow trace row order
does not establish dependency edges. The supported Snakemake snapshot declares
`id`, `rule`, `native_id`, `state`, `attempt`, and `dependencies`; a static DAG
alone does not establish runtime state.

Energy records distinguish `scope` (`job`, `node`, `gpu`), `attribution`
(`exclusive`, `shared`, `allocated_fraction`), and `kind` (`total`, `counter`,
`power`). Totals/counters use J, kJ, Wh, or kWh; power uses W. Power integration
uses valid intervals and the trapezoidal rule. Full attempt-window boundaries,
monotonic timestamps, `max_gap_s`, and total `coverage_complete` determine
completeness. Include failed/retried attempts once, avoid overlapping node/GPU
totals, and report `accepted_work` independently of Slurm success. Missing,
shared, reset, incomplete, or mixed-boundary evidence can make energy per useful
result unavailable.

### Scientific investigation and runtime data

| Operation key | What to supply and what Raven derives | Complete contract or implementation guide |
| --- | --- | --- |
| `investigation-workflow` | Ordered question, evidence and diagnostic stages; measured findings and unresolved questions | [Workflow](../schemas/investigation-workflow.schema.json) |
| `recommendation` | Hypothesis, applicable identity/domain and measured experiment; supported/refuted/stale/inconclusive status | [Recommendation](../schemas/investigation-recommendation.schema.json) |
| `investigation-plan` | Available information actions, missing observations and resource budgets | [Planner](../schemas/investigation-planner.schema.json) |
| `scientific-impact` | Explicit code/data/result/claim lineage and proposed changes; affected claims and revalidation needs | [Lineage](../schemas/investigation-lineage.schema.json) |
| `numerical-stability` | Named reference metrics with absolute/relative tolerances; per-run configurations and measurements | [Numerical stability](../schemas/investigation-numerical.schema.json) |
| `regression-investigation` | Ordered versions, baseline, units, independent/paired samples, minimum change, confidence and scientific validity | [Regression](../schemas/investigation-regression.schema.json) |
| `regression-sweep` | Reviewed immutable Git revisions, real repeated measurement command and declared result contract | [Diagnostic campaign](../schemas/investigation-diagnostic-campaign.schema.json), [execution guide](../guides/scientific-investigation.md#measure-an-actual-git-regression) |
| `diagnostic-campaign` | Bounded actual configuration trials and persistent measurements | [Diagnostic campaign](../schemas/investigation-diagnostic-campaign.schema.json) |
| `minimize-reproducer` | Original input, reduction candidates and an observed failure signature | [Diagnostic campaign](../schemas/investigation-diagnostic-campaign.schema.json), [minimization](../guides/scientific-investigation.md#minimize-a-reproducer) |
| `execution-simulation` | Fixed workload/resource DAG, work/rate units, latency, placement/routes, measured calibration and held-out observations | [Execution model](../schemas/execution-model.schema.json), [model limits](../guides/runtime-research.md#execution-scenario-laboratory) |
| `diagnostic-recipe` | Required observations, runtime identity, budgets and declared site adapter capabilities | [Intent](../schemas/diagnostic-intent.schema.json), [site capabilities](../schemas/site-capabilities.schema.json) |
| `information-query` | Named numeric columns with unique raw references and a count/sum/min/max query | [Source](../schemas/query-source.schema.json), [query](../schemas/summary-query.schema.json) |
| `scientific-field` | Declared units, identity, 1D/2D/3D shape, optional axes and row-major flattened values | [Field](../schemas/scientific-field.schema.json), [slices](../guides/runtime-research.md#scientific-fields-and-slices) |
| `runtime-steering` | Application-opted-in commands, current epoch and safe-point acknowledgments | [Control](../schemas/runtime-control.schema.json), [request](../schemas/runtime-request.schema.json), [SDK integration](../guides/runtime-research.md#cooperative-application-control) |
| `io-coordination` | Cooperative I/O permit requests, declared work and participant acknowledgments | [I/O request](../schemas/io-request.schema.json), [integration](../guides/runtime-research.md#cooperative-io-permits) |
| `investigation-timeline` | Explicit events and source clock/attempt identity; joined but provenance-preserving timeline | [Timeline](../schemas/investigation-timeline.schema.json), [clock rules](../guides/investigation-timeline.md) |

Numerical metric values use the same units as their declared reference;
absolute tolerance is in those units and relative tolerance is a fraction.
Regression sample units belong to the experiment, such as seconds per solve;
pair identities must represent actual matched units. Scientific fields retain
valid/missing counts and extrema when tiled; they do not imply native HDF5,
netCDF or arbitrary-volume loading. Summary queries retain missing counts and
raw references; they do not support arbitrary statistical queries.

Simulation produces model quantities, with measured calibration and held-out
residuals kept distinct from actual observations. A failed qualification
withdraws the prediction. Runtime steering and I/O messages are cooperative
protocol evidence, not numeric metric-stream aliases. They require application
safe points and acknowledgments; copying their status into `metrics.jsonl`
does not implement the protocol.

### Recorded stacks and distributed events

| Operation key | What to supply and what Raven derives | Integration |
| --- | --- | --- |
| `trace-capture` | Explicit Python or cooperative native launch, project/job/attempt identity and storage budget | [Record from startup](../guides/stack-tracing.md#record-a-python-job-from-its-start) |
| `trace-inspect` | Recorder manifest and journals; stack/call events, sequence integrity and recorded coverage | [Trace files](../guides/stack-tracing.md#project-files-and-durability) |
| `trace-snapshot` | Verified running job/PID on the process host; observed current Python stacks | [Snapshot](../guides/stack-tracing.md#capture-a-running-python-process-without-restarting-it) |
| `causal-delay` | Recorded explicit causal links and clocks; supported delay paths and missing links | [Causal delay](../guides/stack-tracing.md#inspect-causal-delays) |
| `collective-audit` | Supported cooperative collective signatures; sequence/participant mismatches | [Collective signatures](../guides/stack-tracing.md#audit-collective-signatures) |

The Python recorder captures supported function events, not GPU kernels or
native-extension internals. Cooperative native events require SDK calls; the
optional PMPI adapter covers its documented C-binding subset. GPU CSV rows,
Python stack events, profiler phase summaries, and collective signatures are
different trace contracts. Follow each producer's clock, sequence and attempt
rules; generic progress or JSONL metrics cannot reconstruct missing events.

### Scheduling, campaign, and file operations

These operations use declared recipes or scheduler/file observations rather
than a new numeric application hook.

| Operation key | Evidence or measurement | Contract / guide |
| --- | --- | --- |
| `storage` | Free bytes/inodes, explicit user/group/project quota, requested minimum free GiB/inodes | [Storage](../guides/operations-cluster.md#storage-space-inodes-and-quotas--p01) |
| `pending-edit` | Current and proposed field of an exact unchanged pending job | [Cluster operations](../guides/operations-cluster.md) |
| `array-throttle` | Existing array and concurrency limit; zero means unlimited | [Cluster operations](../guides/operations-cluster.md) |
| `reservations` | Observed reservation interval, resources and access restrictions | [Cluster operations](../guides/operations-cluster.md) |
| `licenses` | Slurm-reported total/used/free/reserved licenses | [Cluster operations](../guides/operations-cluster.md) |
| `slurm-doctor` | Controller/accounting/configuration readiness, query outcome and latency | [Cluster operations](../guides/operations-cluster.md) |
| `batch-script` | Exact retained batch-script bytes | [Cluster operations](../guides/operations-cluster.md) |
| `allocation-shell` | Verified job/step/node/task ownership and requested attachment | [Cluster operations](../guides/operations-cluster.md) |
| `queue-order` | Own pending jobs, common scheduling scope, original/proposed priority order | [Queue order](../guides/operations.md#additional-operation-own-pending-queue-order) |
| `rerun-job` | Retained cancelled attempt and its actual submission settings | [Rerun](../guides/operations-cluster.md#rerun-one-cancelled-job) |
| `checkpoint` | Completed source attempt, checkpoint path/hash and compatible restart recipe | [Checkpoint recovery](../guides/operations-campaigns.md#checkpoint-recovery) |
| `search` | Discrete parameters, objective observations, trial identities and budget | [Parameter search](../guides/operations-campaigns.md#adaptive-parameter-search) |
| `packing` | Logical task resource/time requests and exclusive-step packing plan | [Task packing](../guides/operations-campaigns.md#short-task-packing) |
| `dask` | Owned pool identity, current/target worker jobs and scaling bounds | [Dask pool](../guides/operations-campaigns.md#elastic-dask-pool) |
| `heterogeneous` | Distinct component resource requests and coupled launch recipe | [Heterogeneous allocation](../guides/operations-campaigns.md#heterogeneous-allocation) |
| `supervisor` | Persistent queue/accounting snapshots, source status and record limits | [Monitor](../guides/operations-services.md#persistent-monitor--s03) |
| `staging` | Exact source/destination paths, byte counts, hashes and durable transfer results | [Staging](../schemas/staging.schema.json) |

### Platform, site telemetry, and project connection

| Operation key | Evidence or measurement | Contract / guide |
| --- | --- | --- |
| `adapter` | Pinned external adapter capabilities and bounded describe/validate/run results | [Adapter](../schemas/adapter.schema.json), [administration](../ADMINISTRATION.md) |
| `evidence` | Source/identity/clock/coverage receipts for recorded observations | [Evidence](../schemas/evidence.schema.json) |
| `qualification` | Explicit compatibility checks and tested/untested environment evidence | [Qualification](../schemas/qualification.schema.json), [procedure](../QUALIFICATION.md) |
| `research-portable` | Supported Perfetto JSON, Caliper JSON, OTLP JSON spans or literal-key Workflow Run RO-Crate | [Supported import boundaries](../FLAGSHIP_IMPLEMENTATION.md#forty-investments), [form fields](operations.md#portable-research-evidence) |
| `site-policy` | Permitted source/cost/identity scope and collection request | [Collection policy](../schemas/site-policy.schema.json), [request](../schemas/collection-request.schema.json) |
| `site-telemetry` | Bounded explicit Prometheus range query with exact cluster/job/attempt labels; returned timestamps/values and coverage | [Prometheus query](../schemas/prometheus-query.schema.json), [source capabilities](../schemas/source-capabilities.schema.json) |
| `support-bundle` | Reviewed bounded diagnostic sources and redaction/export evidence | [Support bundle](../schemas/support-bundle.schema.json) |
| `project-guide` | Standard project/run identity, file contracts and sample attempt; missing-evidence report and integration preparation | [Project connection](../guides/project-connection.md) |

A site's Prometheus metric names and units belong to its exporter. Preserve its
labels and counter/gauge semantics; an account label alone cannot attribute
node-wide data to one job. Site telemetry is an explicit evidence operation,
not an automatic replacement for the native Analytics series.

## Jobs, nodes, accounts, logs, and source health

The remaining workspace displays derive from these sources. No extra
application-reporting file is required unless explicitly listed.

| Workspace | Fields and derived figures | Source / limitation |
| --- | --- | --- |
| Jobs, Inspector, Recents | Exact ID/name/state; owner/account/partition/QoS; CPU/node/GPU requests, memory and walltime; priority/reason/dependency; submit/start/end estimates; WorkDir and log paths; CPU/RSS/GPU values and progress described above | `squeue`, selected `scontrol` details, `sstat`, linked progress. Queue disappearance means awaiting accounting until a terminal outcome is observed. |
| History | Terminal state/exit, actual submit/start/end, elapsed, allocated resources, total CPU, task peak RSS, CPU/memory efficiency, core/GPU-hours, step details and retained logs | `sacct` plus retained exact-attempt evidence. Accounting retention or permissions can omit fields. |
| Cluster | Partition availability/time limit, allocated/idle/other/total node and CPU counts, GPU type totals/used/down/free, pending counts/resources and available estimates | `sinfo`, permitted queue observations; automatic `sbatch --test-only` probes are disabled in managed Raven. |
| Nodes: my nodes | Node state, CPU count/allocation/load, total/free memory, GRES/GRESUsed, partition membership and verified job GPU samples | `scontrol show node`; memory fields are MiB. CPU load is node-level, not this job's CPU efficiency. Free memory zero is a real observation. |
| Nodes: cluster map | Per-node partitions/state, allocated/idle/other/total CPUs, total/allocated memory, GPU type/count/used and down state | `sinfo -N`; allocation is distinct from measured utilization. |
| Group | Authorized account jobs and counts/resource totals, fair-share/usage fields; permitted allocation-budget/usage evidence | Account-filtered `squeue`, `sshare`; optional `sreport`/association limits. Managed automatic budget queries are disabled. Lab allocation visibility depends on site permissions. |
| Deps | Actual dependency edges, predecessor state, hold/blocker evidence and supported graph analysis | Scheduler dependency expressions; unavailable/purged predecessors stay unresolved. |
| Log | Scheduler stdout/stderr; declared application/rank/service logs; exact excerpts and search matches | [`logs.json`](../schemas/logs.v1.schema.json) entries, `paths.log_index` and actual scheduler paths; logs do not become numeric metrics automatically. |
| Sources | Source enabled state, calls/errors, last attempt/success, age, latency in ms, backoff, effective interval and inflight state; telemetry availability | Collector health and policy, not an application science metric. A deferred read does not mean a measured zero. |

Where authorized account-budget evidence is available, Raven also displays
current-month core/GPU-hours, association limits, fraction used, the previous
seven days' daily usage rate, a month-end projection, estimated days to exhaust
the remaining limit, and per-user usage. The projection extends the observed
seven-day average; it is not an allocation guarantee or a currency charge.
Fair share and effective usage come from `sshare`, not from a student's
`summary.json`.

Flags such as `!cpu`, `!mem`, `!gpu`, `ending`, `held`, and `dep` are derived from
observed values, configured thresholds, age, time limit, and scheduler state.
Alerts evaluate the declared expression over available observations. Neither
flags nor alerts constitute a new measurement or a scientific acceptance rule.

Installed [dashboard plugins](../reference.md#plugins) can define their own
columns, flags or panes. Their author owns the extension's source and unit
contract; no project file automatically installs or executes one. Managed
execution policy still applies. Prefer the numeric stream and native manifests
above for portable project reporting.

## Diagnose a missing value

| Symptom | Check first | Expected resolution |
| --- | --- | --- |
| Nothing attaches to a job | Exact `job_id`, cluster/attempt, real WorkDir, `run.json.paths`, file permissions | Select the correct run or fix its real identity/path; do not choose a different attempt merely because it is newer |
| Experiment curve missing | Complete newline, finite numeric value, stable name, declared metric path, reader status | Correct the producer and let the reader observe the next valid record |
| Progress visible but no ETA | Three advancing samples with unchanged phase/total/unit | More valid progress evidence; walltime fraction alone provides no application ETA |
| CPU rate missing at startup | `sstat` CPU/task fields and upstream accounting cadence | Two valid observations; memory may already be available independently |
| Completed CPU/memory graph empty | Whether this exact attempt was sampled and its series retained | Use final accounting totals; the missing historical curve cannot be reconstructed from one peak |
| GPU allocated but graph empty | Exact WorkDir CSV, valid device rows, managed existing-telemetry setting | Start an authorized recorder inside a future/current GPU job and keep the trace |
| Native Compare GPU empty while Job series works | Whether the data exists only in the CSV | Current Compare needs native retained GPU samples; CSV supports Job series |
| Prediction or energy ratio unavailable | Comparable independent records, complete attribution/coverage and required fields | Supply the actual missing evidence; leave the result unavailable otherwise |
| Operation file exists but view is empty | Operation input form and native schema; supported execution adapter | Attach the explicit source and inspect it; arbitrary files in `reports/` are not automatically imported |

Open Sources and `:telemetry` to distinguish a producer problem, missing
permission, source backoff, and an effective read interval. Faster UI updates
cannot create observations that the application, scheduler, or driver did not
record.

## Coverage and maintenance

This reference follows [`RESEARCH_VIEWS`](../../raven/research.py),
[`PLANNING_VIEWS`](../../raven/planning.py),
[`TABS` and `ANALYTICS_VIEWS`](../../raven/views.py), the
[generated operation catalog](operations.md), the
[native data model](../../raven/model.py), the
[Slurm readers](../../raven/slurm.py), and the
[application metric reader/writer](../../raven/metrics.py).
When a display or hook changes, update its row and the linked schema/reader
documentation together. Native readers also validate chronology, identity,
measurement scope, graph semantics and file evidence beyond JSON shape.
