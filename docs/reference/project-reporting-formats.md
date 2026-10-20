# Project reporting V2: file formats and application hooks

[Implementation guide](../PROJECT_REPORTING_V2.md) · [Metric and panel catalog](project-reporting-metrics.md) · [Site setup](../guides/project-reporting-sites.md)

Use this reference when adding measurements to an application or adapting an
existing reporting system. Start with `run.json` and `metrics.jsonl`; add the
other sources when the project needs them. The
[portable integration kit](../../examples/reporting-v2/) creates the basic
files for a wrapped command.

**V2 is the integration guide revision. The interchange formats remain version
1.** Keep the literal `raven.run/v1`, `raven.summary/v1`, and other version
markers shown here. An invented `/v2` marker will not select a new reader.

The [automated launch and export guide](../guides/automated-reporting-exports.md)
adds job-owned retained telemetry and versioned plotting-ready exports. These
are additive formats; the application reporting contracts below remain valid.

## Choose the file for the measurement

In this reference, `PROJECT` is the project root and
`RUN` is `PROJECT/runs/<run_id>`.

| Information to publish | File | Consumer |
| --- | --- | --- |
| Attempt identity and exact report locations | `RUN/run.json` | Job attachment and project/run selection |
| Loss, residual, throughput, accuracy, iteration progress, other application values | `RUN/metrics.jsonl` | Research Experiment, application progress in Jobs |
| Final duration, CPU time, memory, allocation, scientific results | `RUN/summary.json` | Explicit planning export; Resources and Scaling consume its exported fields |
| Application and rank log locations | `RUN/logs.json` | Logs and Research Evidence |
| Expected outputs and their structural checks | `PROJECT/.raven/contracts/outputs.v1.json` | Research Artifacts and output browser |
| Existing NVIDIA device utilization and memory trace | `<Slurm WorkDir>/logs/gpu-util-<job_id>.csv` | Selected job's GPU Series graph |
| Job-owned retained GPU and process CPU/memory observations | `RUN/telemetry/index.json` and declared versioned JSONL streams | Exact-attempt Analytics and plotting-ready export |
| Selected historical attempts and planning evidence | `RUN/reports/planning.json`, or a declared per-view source | Six Research planning views |
| An advanced scientific, profiler, energy, or workflow measurement | An explicitly selected operation source | Corresponding Research operation |

`metrics.jsonl` is an application stream. Calling a value `cpu_pct`,
`memory_bytes`, or `gpu_utilization_pct` creates an Experiment metric with that
name; it does not replace the scheduler telemetry used by Jobs or Analytics.
There is no general `analytics.json` input. See the
[panel catalog](project-reporting-metrics.md) for the source and availability of
every displayed metric.

## Rules shared by the files

- Encode JSON and JSONL as UTF-8. Use unique JSON keys, finite numbers, and a
  JSON encoder rather than shell string concatenation.
- Publish JSON snapshots through a temporary file in the same directory and
  an atomic replacement. The reader sees a complete old or new document.
- Append one complete JSON object and a newline per metric record. Do not
  pretty-print JSONL across lines or wrap the file in a JSON array. A partial
  final line is held until its newline arrives.
- Use one reporting coordinator per attempt, usually MPI rank 0. An append
  flag does not make arbitrary concurrent writes safe on every filesystem.
  Give independent tasks separate runs; register individual rank logs in the
  shared run's log index.
- Measure durations with a monotonic clock. Record event timestamps as Unix
  epoch seconds; `time.time()` supplies the current epoch timestamp in Python.
  Record actual observation times, not a counter starting at zero.
- Omit unavailable observations. Zero means a measured zero. Metric values
  cannot be `null`, strings, Booleans, NaN, or Infinity. Some optional summary
  and resource fields explicitly permit `null`; the tables below identify
  that distinction.
- Preserve a separate directory for each run, array task, retry, and restart.
  Keep generated data outside version control unless sharing a reviewed
  example or run bundle.

The JSON and CSV values below illustrate valid serialization. They are not
measurements of a workstation, a cluster, or a lab allocation. Replace their
identities and observations with the producing job's actual values.

## 1. Attempt inventory: `run.json`

This is a complete example for a running job owned by `userA`, charged to the
Slurm account `LabX`:

```json
{
  "schema": "raven.run/v1",
  "run_id": "fit-a1",
  "experiment_id": "dataset-v3-fit",
  "attempt": 1,
  "name": "my-project/fit",
  "state": "RUNNING",
  "job_id": "12345",
  "start": 1791801000.0,
  "resources": {
    "account": "LabX",
    "partition": "compute",
    "cpus": 4,
    "nodes": 1,
    "gpus": 0,
    "mem_bytes": 8589934592,
    "time_seconds": 3600
  },
  "parameters": {"dataset": "v3", "iterations": 100},
  "metadata": {"owner": "userA", "lab": "LabX"},
  "paths": {
    "metrics": "metrics.jsonl",
    "summary": "summary.json",
    "log_index": "logs.json",
    "stdout": "logs/stdout.log",
    "stderr": "logs/stderr.log",
    "outputs": "outputs",
    "passports": "passports",
    "planning": "reports/planning.json"
  }
}
```

`LabX` is an example Slurm account, not a username or a portable account name.
Use the account association granted by the site. `metadata.owner` is descriptive;
Raven's scheduler ownership and access checks come from the actual connection
and scheduler records. Adding an account to a report does not grant access to it.

| Field | Required? | Format and meaning |
| --- | --- | --- |
| `schema` | Yes | Literal `raven.run/v1`. |
| `run_id` | Yes | Attempt directory name: 1–128 ASCII letters, digits, `.`, `_`, `-`; first character must be a letter or digit. |
| `experiment_id` | Yes | Printable logical experiment ID, 1–128 characters; shared by its attempts. |
| `attempt` | Yes | Integer 1–2,147,483,647; a retry gets a new attempt and directory. |
| `state` | Yes | Uppercase lifecycle state, such as `RUNNING`, `COMPLETED`, `FAILED`, `TIMEOUT`, or `OUT_OF_MEMORY`; complete vocabulary in the schema. |
| `job_id` | No | Actual scheduler identity as a string, including array task (`12345_7`) or heterogeneous component where applicable. Omit or use `null` for a local run. |
| `name` | No | Stable printable workload name, 1–128 characters, e.g. `my-project/fit`. Do not put the changing job ID here. |
| `project_id` | No | Stable project ID using the same character rules as `run_id`. |
| `start`, `end`, `submit` | No | Nonnegative epoch seconds, at most 253,402,300,799; omission or `null` means unknown. `end` must not precede `start`. |
| `parameters` | No | Small object of stable scientific parameters; at most 64 keys, four nesting levels, and 128 total JSON values. Keep run IDs, timestamps, and resource requests elsewhere. |
| `input_size` | No | Nonnegative finite workload size, or `null`. Define its unit in the project, e.g. number of examples. |
| `resources` | No | Known allocation/request fields from the next table. |
| `paths` | No | Exact relative paths from this run directory; supported keys follow below. |
| `provenance` | No | `script` relative to the project root; actual 64-hex `script_sha256`; optional actual 40–64-hex `git_commit` or `null`; optional printable `description` up to 4,096 characters. |
| `results`, `metadata` | No | Objects for project-specific results and descriptive provenance. Prefer `summary.json` for final results. |
| `description` | No | Printable description, at most 4,096 characters. |

The native inventory budget is **64 KiB**. The schema rejects unknown top-level
keys; place project-specific extensions inside `metadata` or `results`.

### Allocation fields

These fields live inside `run.json.resources`. In `summary.json`, the same
fields are **top-level** fields. Each may be omitted or `null` when unknown.

| Field | Format | Meaning |
| --- | --- | --- |
| `account`, `partition`, `qos` | Nonempty printable string, up to 128 characters | Actual Slurm charging account, partition, and QoS. These are separate from the submitting user. |
| `cpus`, `nodes` | Integer 1–1,000,000,000 | Total allocation counts, not CPUs per task. |
| `gpus` | Integer 0–1,000,000,000 | Total GPU count. `0` means a positively known CPU-only allocation. |
| `gpu_type` | Printable string, up to 128 characters | Known GPU type; `""` denotes a known generic GPU allocation. |
| `mem_bytes` | Integer 1–2⁶³−1 | Total requested allocation memory in bytes. |
| `time_seconds` | Positive finite number, at most 3,162,240,000 | Requested walltime limit in seconds. |

Convert Slurm requests using their actual scope: `--mem` is normally per node,
and `--mem-per-cpu` is per allocated CPU. For a known two-node allocation with
8 GiB per node, total `mem_bytes` is `17179869184`. Do not use
`os.cpu_count()`, a node's physical memory, or an MPI worker count as proof of
the job's allocation.

### Paths and discovery

Supported `paths` keys are `metrics`, `summary`, `outputs`, `logs`, `stdout`,
`stderr`, `log_index`, `passports`, `planning`, `predict`, `forecast`, `blockers`,
`tradeoffs`, `scaling`, `workflow`, `submit`, `array_manifest`, and `contract`.
Declare only sources the project intends to provide. Missing declared files
remain visibly missing until published.

Each value is an exact run-relative POSIX path, at most 4,096 characters:
no absolute paths, `.` or `..` segments, empty segments, globs, backslashes,
or symlink traversal. A per-run `contract` overrides the standard project
contract. Each planning view prefers its own path, then `planning`; see
[planning sources](#7-planning-sources).

Keep Slurm's WorkDir at the project root or within `PROJECT/runs/<run_id>`.
Otherwise explicitly register the project with `:project /absolute/project`.
The manifest's exact job ID establishes attachment. Similar names or the most
recent directory do not. A local run can be opened with `:run select RUN_ID`
after registering its project.

Schema: [run.v1.schema.json](../schemas/run.v1.schema.json).

## 2. Application measurements: `metrics.jsonl`

This is the complete content of a two-record file. Each displayed line ends
with a newline:

```jsonl
{"t":1791801005.0,"step":1,"phase":"fit","metrics":{"loss":0.42,"throughput_samples_per_second":120.5},"progress":{"completed":1,"total":100,"unit":"iterations"}}
{"t":1791801010.0,"step":2,"phase":"fit","metrics":{"loss":0.31,"throughput_samples_per_second":123.0},"progress":{"completed":2,"total":100,"unit":"iterations"}}
```

| Field | Required? | Exact rule |
| --- | --- | --- |
| `t` | Yes | Finite nonnegative epoch seconds; fractions are supported. |
| `metrics` | No | Object with at most 64 names. Include it for numeric measurements; a progress-only record can omit it or use `{}`. |
| Metric name | For each metric | Printable, nonempty string, at most 96 characters. |
| Metric value | For each metric | Finite JSON number; negative measurements are permitted. |
| `step` | No | Integer 0–2⁶³−1. |
| `phase` | No | Printable string, at most 160 characters, e.g. `warmup`, `fit`, `validate`. |
| `pid` | No | Producing process ID, integer 1–2⁶³−1. The Python client supplies it; existing files can omit it. |
| `progress` | No | Object containing `completed` and `total`, optionally `unit`. |
| `progress.completed` | If `progress` exists | Finite number, `0 <= completed <= total`. |
| `progress.total` | If `progress` exists | Finite number strictly greater than zero. |
| `progress.unit` | No | Printable string, at most 64 characters, e.g. `iterations`, `frames`, `samples`. |

Each encoded record must fit **65,536 bytes**. There is no `schema` field in
individual records. Keep timestamps and steps increasing within a phase; start
a new phase when resetting progress. Raven derives rate and ETA from multiple
observations. The record supplies the measured counts, not a promised finish
time. The reader retains bounded recent samples rather than an unlimited history.

Choose stable names and document units. These are examples of application
measurements, not reserved keywords:

| Example metric | Unit and interpretation |
| --- | --- |
| `loss`, `residual_l2` | Your explicitly documented objective or residual definition. |
| `accuracy_fraction` | Fraction from 0 to 1; use a different name for percent. |
| `throughput_samples_per_second` | Samples completed per second over a stated interval. |
| `iteration_seconds` | Duration of the completed iteration, not an epoch timestamp. |
| `bytes_read` | Bytes over the documented scope; state whether cumulative or per interval. |
| `rank0_rss_bytes` | Observed resident memory for rank 0 only. |
| `gpu0_utilization_pct` | Device utilization percent; remains an application graph if written here. |

The preferred progress shape is the top-level `progress` object. Existing
integrations may use `progress_fraction` (0–1), `progress_pct` (0–100), or
`completed_steps` and `total_steps` within `metrics`. A Jobs progress indicator
derived from elapsed/requested walltime is a time-budget fraction, not evidence
of application completion.

### Python: add one call inside an existing loop

When launched by the V2 wrapper, use its existing `RAVEN_RUN_DIR`; the wrapper
owns the attempt's start and finish. Import the copied standard-library
reporter once, then call it from the process that owns progress reporting:

```python
import importlib.util
import os
from pathlib import Path

project = Path(os.environ["RAVEN_PROJECT_ROOT"])
spec = importlib.util.spec_from_file_location(
    "project_reporting", project / ".raven/reporting-v2/reporting.py"
)
reporting = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporting)
run = Path(os.environ["RAVEN_RUN_DIR"])

# Inside your existing loop, after obtaining these actual values:
reporting.write_metric(
    run,
    {"loss": float(loss), "throughput_samples_per_second": float(throughput)},
    step=iteration,
    phase="fit",
    progress={"completed": iteration, "total": iterations, "unit": "iterations"},
)
```

For MPI, call this from rank 0 after the application's existing reduction when
the value is an aggregate. A rank-0 observation alone does not become an
allocation-wide observation. Do not have every rank initialize or finalize the
same attempt.

When your application directly owns the lifecycle, the same copied module
provides `begin_run(...)`, `write_metric(...)`, `register_log(...)`, and
`finish_run(...)`. See the complete
[Python producer](../../examples/project-template/experiment.py). Use one
lifecycle owner: a wrapper or your application.

### Bash and other languages

Use the kit's metric command with actual values produced by the application.
It performs JSON encoding and validation, so a Bash launcher need not construct
JSON by interpolation:

```bash
"$RAVEN_REPORT_PYTHON" "$RAVEN_REPORT_HELPER" metric \
  --value "loss=$loss" \
  --value "throughput_samples_per_second=$throughput" \
  --step "$iteration" --phase fit \
  --completed "$iteration" --total "$iterations" --unit iterations
```

C++, Julia, R, Rust, and other languages can instead encode the exact JSONL
format directly. Flush after a meaningful iteration or every few seconds.
Use one writer and preserve complete records when the application exits.

Schema: [metrics.v1.schema.json](../schemas/metrics.v1.schema.json), applied
to each complete line. Native validation additionally checks finite numbers,
duplicate keys, printable text, and `completed <= total`.

## 3. Final report: `summary.json`

Publish this once the application has a terminal outcome. The following
complete example uses the same attempt identity as the manifest:

```json
{
  "schema": "raven.summary/v1",
  "id": "fit-a1",
  "experiment_id": "dataset-v3-fit",
  "attempt": 1,
  "job_id": "12345",
  "name": "my-project/fit",
  "state": "COMPLETED",
  "exit_code": 0,
  "account": "LabX",
  "partition": "compute",
  "cpus": 4,
  "nodes": 1,
  "gpus": 0,
  "mem_bytes": 8589934592,
  "time_seconds": 3600,
  "start": 1791801000.0,
  "end": 1791801091.2,
  "runtime_seconds": 91.2,
  "cpu_seconds": 310.4,
  "memory_bytes": 2684354560,
  "memory_scope": "max_task_rss",
  "parameters": {"dataset": "v3", "iterations": 100},
  "results": {"loss": 0.018, "accuracy_fraction": 0.94},
  "metadata": {
    "cpu_source": "Slurm TotalCPU across the recorded application scope",
    "memory_source": "Slurm MaxRSS"
  }
}
```

Only `schema`, `id`, `name`, and `state` are required by the summary schema.
Use `id` equal to the manifest's `run_id` and directory name. Keep its terminal
state consistent with the finalized manifest. The exporter needs both files.

| Fields beyond the allocation and identity fields above | Format and meaning |
| --- | --- |
| `schema` | Literal `raven.summary/v1`. |
| `id` | Unique attempt identity using the `run_id` character rules. |
| `runtime_seconds` | Nonnegative measured wall duration of the workload, excluding queue wait. |
| `cpu_seconds` | Nonnegative CPU time summed across an explicitly documented scope. `time.process_time()` covers only the current Python process. |
| `memory_bytes` | Nonnegative observed memory measurement in bytes; requires a meaningful `memory_scope`. |
| `memory_scope` | Exactly `job_peak`, `per_node_peak`, or `max_task_rss`. |
| `script_sha256` | Actual SHA-256 of the declared source/entry point, 64 hexadecimal characters; copied reporter computes it. |
| `exit_code` | Integer −2³¹ through 2³¹−1; if supplied with `COMPLETED`, must be zero. |
| `workers` | Integer 1–1,000,000: actual workers used in a controlled scaling experiment. |
| `problem_size` | Positive finite workload size, at most 10³⁰⁰, with a fixed documented definition. |
| `repeat` | Integer 1–10,000: independent repeat within that scaling configuration. |
| `work_units` | Positive finite declared useful-work quantity, at most 10³⁰⁰. |
| `fingerprint` | Explicit printable workload fingerprint, 1–256 characters. |
| `results` | Object of final scientific results. Prediction/scaling readers do not automatically interpret these arbitrary values. |
| `metadata` | Object describing sources, scope, protocol, or other project context. |

The summary also accepts `project_id`, `experiment_id`, `attempt`, `job_id`,
`parameters`, `input_size`, `submit`, `start`, `end`, and all allocation fields
already specified. Optional measurements, counts, fingerprint, and exit code
may be omitted or `null`; `parameters`, `results`, and `metadata`, when present,
must be objects. No additional top-level vocabulary is accepted.

Slurm `MaxRSS` describes maximum task RSS. It is not a simultaneous job-wide
memory peak. Preserve `max_task_rss`; do not multiply task peaks by the rank
count. Report `job_peak` only when the entire job's simultaneous peak was
actually measured.

Keep failed, cancelled, timed-out, and OOM attempts in exported history.
An application's exit code does not identify an OOM or timeout by itself.
An interrupted coordinator may leave `RUNNING`; reconcile it using actual
scheduler evidence. A completed process also does not establish scientific
acceptance: use the optional acceptance contract when that matters.

For scaling, record real `workers`, `problem_size`, and `repeat` values in
addition to stable code and parameter identity. Choose fixed work for strong
scaling or a documented fixed work-per-worker ratio for weak scaling. Three
or more comparable repeats per configuration can establish observed repeat
spread; a single run cannot.

Schema: [summary.v1.schema.json](../schemas/summary.v1.schema.json).

## 4. Log catalog: `logs.json`

```json
{
  "schema": "raven.logs/v1",
  "run_id": "fit-a1",
  "job_id": "12345",
  "logs": [
    {"id": "application.stdout", "path": "logs/stdout.log", "label": "Application stdout", "group": "Application"},
    {"id": "application.stderr", "path": "logs/stderr.log", "label": "Application stderr", "group": "Application"},
    {"id": "rank.0", "path": "logs/rank-0.log", "label": "Rank 0", "group": "Workers", "description": "Coordinator diagnostics"}
  ]
}
```

| Field | Rule |
| --- | --- |
| `schema` | Required literal `raven.logs/v1`. |
| `logs` | Required array of at most 256 entries; empty is valid. |
| `run_id`, `job_id` | Optional actual attempt and selected-job identities; include them to preserve binding. |
| Entry `id` | Required unique stable ID, using the `run_id` character rules. |
| Entry `path` | Required exact printable path, 1–4,096 characters. No tilde expansion, globs, backslashes, or control characters. |
| Entry `label`, `group` | Optional printable strings, 1–160 characters. |
| Entry `description` | Optional printable string, at most 512 characters. |

Relative paths resolve from the **directory containing `logs.json`**. Read-only
log declarations may name explicit sibling or absolute files; these remain
external locations when copying the run. Prefer files within the run for a
portable bundle. Files must be regular files, with no symlink traversal.

The index describes locations; it does not contain log text or discover
undeclared worker outputs. Keep it under **256 KiB**. Publish it atomically
from one coordinator. Add a worker file using the copied SDK:

```python
reporting.register_log(
    run, "rank.0", "logs/rank-0.log", label="Rank 0", group="Workers"
)
```

Schema: [logs.v1.schema.json](../schemas/logs.v1.schema.json).

## 5. Expected outputs: `outputs.v1.json`

Place this project contract at `.raven/contracts/outputs.v1.json`. All paths
below resolve from the **selected run directory**, not the contract directory:

```json
{
  "version": 1,
  "outputs": [
    {"path": "run.json", "required": true, "format": "json", "max_bytes": 65536, "required_keys": ["schema", "run_id", "experiment_id", "attempt", "state"]},
    {"path": "logs.json", "required": true, "format": "json", "max_bytes": 262144, "required_keys": ["schema", "logs"]},
    {"path": "metrics.jsonl", "required": true, "format": "text", "min_bytes": 1, "max_bytes": 4194304},
    {"path": "summary.json", "required": true, "format": "json", "max_bytes": 262144, "required_keys": ["schema", "id", "name", "state"]},
    {"path": "outputs/results.csv", "required": true, "format": "csv", "columns": ["step", "loss"], "min_rows": 1, "max_bytes": 4194304}
  ]
}
```

Corresponding application result, `RUN/outputs/results.csv`:

```csv
step,loss
100,0.018
```

| Entry field | Meaning |
| --- | --- |
| `path` | Required exact confined run-relative path; no globs or traversal. |
| `required` | Whether absence fails the contract; set it explicitly. |
| `format` | `json`, `csv`, or `text`; omit for existence/byte/hash checks only. |
| `min_bytes`, `max_bytes` | Nonnegative integer byte limits. |
| `columns` | Required CSV column names; at most 256 unique names. |
| `rows`, `min_rows`, `max_rows` | Exact or bounded CSV data-row counts, excluding the header. Require `format: "csv"`. |
| `required_keys` | Required top-level JSON keys; at most 256 unique names. Requires `format: "json"`. |
| `sha256` | Expected actual file hash, 64 hexadecimal characters. |

The contract has required `version: 1` and `outputs`, with at most 4,096
entries. Required final outputs can be missing while a job runs. Declare the
files that actually determine success; adapt or remove the illustrative
`results.csv` check for your program. JSON key checks do not run scientific
validation or full JSON Schema validation. A CSV output is an artifact, not an
application graph source; publish its measurements separately as JSONL if
they should appear in Experiment.

Schema: [output-contract.v1.schema.json](../schemas/output-contract.v1.schema.json).

## 6. NVIDIA trace: `gpu-util-<job_id>.csv`

Raven's current managed collection reads an existing file at:

```text
<scheduler-recorded WorkDir>/logs/gpu-util-<exact-job-id>.csv
```

For an array task, use `12345_7`, not just `12345`. The trace path is independent
of `RUN/metrics.jsonl`: a run directory nested under the project does not move
Slurm's WorkDir. The [GPU guide](../guides/gpu-detection.md#provide-an-optional-gpu-trace)
supplies the optional job-local recorder and cleanup.

Write the native `nvidia-smi` CSV with **no header and no units**:

```csv
2026/10/12 14:30:00.000, 0, 83, 6120
2026/10/12 14:30:05.000, 0, 91, 6400
```

| Column, in exact order | Format |
| --- | --- |
| `timestamp` | `YYYY/MM/DD HH:MM:SS[.fraction]`, as emitted by NVIDIA. |
| `index` | Nonnegative decimal GPU index. |
| `utilization.gpu` | Utilization percentage from 0 to 100; unsupported counters remain unknown. |
| `memory.used` | Device memory used in MiB; multiply by 1,048,576 to express bytes elsewhere. |

Use query
`timestamp,index,utilization.gpu,memory.used` with
`--format=csv,noheader,nounits`. The native reader interprets the NVIDIA
timestamp in its local timezone and ignores its fractional seconds. Preserve
the producer timezone when reading a transferred trace; UTC-offset strings
are not this CSV format. This differs from the epoch timestamps in JSONL.

These are **device** counters. A desktop display or another permitted process
may contribute to them; they are not exclusive process accounting. Record
only GPUs assigned to the job. A physical `--id=0` is appropriate for the
documented single-GPU workstation, but is not a portable cluster selector.
Use the allocation's actual device identifiers on a shared system.

This index-only format suits a single node. Do not merge traces from several
nodes that each have GPU `0` into one file. Preserve separate node/rank logs
or publish explicitly named per-node application metrics in Experiment.
Retain the trace to inspect the completed job. It populates selected-job
Series; it does not populate Analytics Compare's retained native GPU series.

Native format and operating details: [GPU trace guide](../guides/gpu-detection.md#provide-an-optional-gpu-trace).

## 7. Planning sources

Use the copied reporter to export **explicitly selected** finalized attempts:

```bash
python3 .raven/reporting-v2/reporting.py export \
  runs/fit-a1 runs/fit-a2 \
  --reference runs/fit-a2 \
  --output runs/fit-a2/reports/planning.json
```

Run this from the project root. Each named directory must already have its
matching terminal `run.json` and `summary.json`. The reference supplies the
query identity; the exporter preserves failed attempts. Repeat the export when
the intended cohort changes. Raven does not recursively aggregate runs or
choose a scientific cohort automatically.

The smallest shared bundle header is:

```json
{
  "version": 1,
  "kind": "raven.planning",
  "coverage": 0.8,
  "query": {"name": "my-project/fit", "partition": "compute", "cpus": 4, "nodes": 1, "gpus": 0, "account": "LabX"},
  "history": []
}
```

An empty history is valid but insufficient for prediction. Populate it with
actual summary objects via export. Requested interval `coverage` is strictly
between 0 and 1. Keep shared bundles at most **1 MiB** and 10,000 records;
the copied exporter bounds its explicit selection to 256 attempts.

| View and optional per-run filename | Input fields to provide | Complete example and field definition |
| --- | --- | --- |
| Resources: `reports/predict.json` | `history` of comparable measured attempts and explicit `query`. Core query identity: stable `name`, `partition`, total `cpus`, `nodes`, `gpus`; retain known account, QoS, code, parameters, and memory scope. | [Planning fixture](../../examples/planning/observations.json), [query and history schema](../schemas/planning.v1.schema.json), [interpretation](../WAVE_TWO.md#resource-predictions). |
| Forecast: `reports/forecast.json` | Actual `jobs` snapshot and `job_id`; optional real `observations` and historical outcomes. An observation requires `job_id`, `submit`, `issued_at`, `predicted_start`; record `actual_start` only after it occurs. Optional `now` freezes a historical snapshot. | [Planning fixture](../../examples/planning/observations.json), schema `$defs.forecastObservation` and `$defs.jobRecord` in [planning.v1](../schemas/planning.v1.schema.json). |
| Blockers: `reports/blockers.json` | Actual `jobs`, `job_id`, and matching `details`; add observed `partitions`, `nodes`, `share`, and `health` when available. All must describe the same snapshot. | [Planning fixture](../../examples/planning/observations.json), [planning schema](../schemas/planning.v1.schema.json), [blocker interpretation](../WAVE_TWO.md). |
| Tradeoffs: `reports/tradeoffs.json` | Explicit `candidates`, measured comparable `history`, optional actual `queue_history`. Each candidate requires `name`, `partition`, total `cpus`, `nodes`, `gpus`, `mem_bytes`, and `time_seconds`; retain shared scientific identity. | [Planning fixture](../../examples/planning/observations.json), schema `$defs.candidate` in [planning.v1](../schemas/planning.v1.schema.json). |
| Scaling: `reports/scaling.json` | Bundle `scaling` records containing real workers, problem size, parameters, script hash, state, measured runtime, and repeat identity; alternatively a native `raven.scaling` recipe describing proposed configurations. | [Scaling recipe](../../examples/planning/scaling.json), schema `$defs.scalingRecord` / `$defs.scalingRecipe` in [planning.v1](../schemas/planning.v1.schema.json). |
| Workflow: `reports/workflow.json` | Native `raven.workflow` recipe with `version: 1`, `kind`, and `nodes`, or bundle `workflow`. Each node names its ID and real dependencies; optional `runtime_seconds` or `duration` bounds declare a planning scenario, whose source should be documented. | [Complete workflow](../../examples/planning/workflow.json), schema `$defs.workflowRecipe` in [planning.v1](../schemas/planning.v1.schema.json). |

The fixtures are explicitly illustrative, including their durations and
predictions. They demonstrate every field; replace their evidence before
operational use. A recipe proposes work. It does not establish an observed
speedup, queue wait, or critical-path duration. Workflow duration bounds remain
declared scenarios rather than calibrated confidence intervals, even when
measurements informed them.

Publish an existing valid source and declare its exact run-relative path:

```bash
python3 .raven/reporting-v2/reporting.py report \
  workflow .raven/definitions/workflow.json --run runs/fit-a2
```

Supported view arguments are `planning`, `predict`, `forecast`, `blockers`,
`tradeoffs`, `scaling`, `workflow`, and `submit`. The publisher atomically
writes `RUN/reports/<view>.json`, then updates the manifest. A specific view
path takes precedence over `paths.planning`; an explicitly declared missing
source stays missing.

`submit` is a separate read-only source: preserve intact JSON emitted by
`raven run prepare`, with schema `raven.submission-plan/v1` and its `plan_id`.
Use the [capture recipe](../PROJECT_STANDARD.md#capture-submit-evidence-without-submitting)
and [submission-plan schema](../schemas/submission-plan.v1.schema.json).
Do not relabel an analyzed forecast, a shell error, or a report summary as
native preparation output. Publishing the file does not submit the job.

## 8. Exact job indexes and array maps

`raven report` maintains optional `.raven/jobs/` indexes to accelerate exact
job-to-attempt discovery. A project using only the portable SDK can use the
standard `runs/<run_id>/run.json` layout without generating these indexes.

```json
{
  "schema": "raven.job-index/v1",
  "job_id": "12345_7",
  "cluster": "example-cluster",
  "run_ids": ["fit-a1-task7"]
}
```

All four fields are required. `job_id` is the exact numeric Slurm identity;
`cluster` is its exact name, or `""` when unknown, never a wildcard.
`run_ids` has 1–128 unique valid attempt IDs. Native publishing uses a
12-hex SHA-256 cluster prefix in the filename and keeps an unscoped alias
only where unambiguous. A collision across clusters uses a separate sentinel:

```json
{
  "schema": "raven.job-index-ambiguous/v1",
  "job_id": "12345_7",
  "clusters": ["example-cluster", "other-cluster"]
}
```

Do not resolve ambiguity by choosing the newest attempt. Use the standard
publisher or explicit run selection. The
[job-index schema](../schemas/job-index.v1.schema.json) documents both forms.

An optional scientific array map is a different file: it maps actual task
indices to scientific identities and parameters. Use the complete
[array example](../../examples/array-manifest.json),
[array-manifest schema](../schemas/array-manifest.schema.json), and
[attachment instructions](../PROJECT_STANDARD.md#scientific-array-identities).
Each task still writes its own manifest and measurements. There is no generic
`arrays.json` replacement for scheduler task records.

## 9. Advanced scientific and operational sources

Add these modules only when you have the corresponding evidence. Every row
links a complete machine-readable example and its exact contract. Adapt that
example as a unit, preserving its schema and identities. Open `:ops catalog`,
choose the operation, supply the exact source path, and select
**Inspect / prepare**. Merely placing a file under `reports/operations/` does
not attach it automatically.

| Module | Source and complete format |
| --- | --- |
| Environment compatibility | [Environment example](../../examples/operations-science/environment.json), [schema](../schemas/science-environment.schema.json). Declared Python, native-library, and optional container requirements. |
| Scientific acceptance | [Policy](../../examples/operations-science/acceptance.json) and [measured results](../../examples/operations-science/results.json); [policy schema](../schemas/science-acceptance.schema.json), [result schema](../schemas/science-results.schema.json). A successful process exit is not an acceptance result. |
| Statistical comparison | [Statistics example](../../examples/operations-science/statistics.json), [schema](../schemas/science-statistics.schema.json). Declare actual independent units or exact pairs and the measured outcomes. |
| Verified reuse | [Requested identity](../../examples/operations-science/identity.json), [reuse evidence](../../examples/operations-science/reuse.json); [identity schema](../schemas/science-identity.schema.json), [reuse schema](../schemas/science-reuse.schema.json). Preserve real input/output hashes and acceptance evidence. |
| Dependency repair | [Recipe](../../examples/operations-science/dependency-repair.json), [schema](../schemas/science-dependency-repair.schema.json). Exact pending-job and dependency identities. |
| Checkpoint restart | [Checkpoint example](../examples/campaigns/checkpoint.json), [campaign schema](../schemas/campaign-operations.schema.json). Actual checkpoint path/hash, source attempt, and compatible restart recipe. |
| Parameter search | [Search example](../examples/campaigns/search.json), [campaign schema](../schemas/campaign-operations.schema.json). Declared discrete search space, budget, and actual trial observations. |
| Short-task packing | [Packing example](../examples/campaigns/packing.json), [campaign schema](../schemas/campaign-operations.schema.json). Logical tasks and explicit resource budgets. |
| Dask pool | [Dask example](../examples/campaigns/dask.json), [campaign schema](../schemas/campaign-operations.schema.json). Owned pool and scaling limits. |
| Heterogeneous allocation | [Component recipe](../examples/campaigns/heterogeneous.json), [campaign schema](../schemas/campaign-operations.schema.json). Distinct Slurm components and their coupled launch. |
| Staging | [Transfer manifest](../examples/staging-manifest.json), [schema](../schemas/staging.schema.json). Exact paths, byte sizes, and hashes. |
| Nextflow | Native `trace.tsv`: tab-separated header with unique required `task_id` and `status`; optional `native_id`, `name`, `process`, `hash`, `attempt`, `exit`. Enable with `nextflow run main.nf -with-trace trace.tsv`; [format and limits](../guides/operations-services.md#nextflow). |
| Snakemake | Native `snakemake --d3dag` JSON for structure, or [runtime snapshot](../examples/workflow-engine.json) with [schema](../schemas/workflow-engine.schema.json). Preserve workflow ID, task attempts, state, scheduler IDs, and dependency edges when known. |
| Distributed profiler | [Rank/phase example](../../examples/scale-profiler.json), [schema](../schemas/profiler.schema.json). Exclusive phase wall durations, rank/node identities, CPU/MPI/I/O counters, and expected-rank coverage. |
| Darshan | Native text exported by `darshan-parser`, selected as `darshan`; [accepted POSIX counters and identity fields](../guides/operations-scale.md#distributed-bottleneck-explorer). Binary files and aggregate rank `-1` are not per-rank evidence. |
| Cross-cluster observations | [Workspace example](../../examples/scale-clusters.json), [schema](../schemas/cluster-workspace.schema.json). Explicit endpoints or profiles and Slurm owner; this selects actual scheduler observations. |
| Incident correlation | [Incident example](../../examples/scale-incidents.json), [schema](../schemas/incidents.schema.json). Permitted node-event intervals and reasons with timestamps and attempt identity. |
| Energy per useful result | [Energy example](../../examples/scale-energy.json), [schema](../schemas/energy.schema.json). Measured energy, attribution, interval/coverage, failed attempts, and accepted useful work. |

### Scientific results are a separate acceptance input

To test application values against an acceptance policy, publish
`RUN/reports/operations/results.json` in this form:

```json
{
  "schema": "raven.results/v1",
  "runs": [
    {"id": "fit-a1", "metrics": {"loss": 0.018, "accuracy_fraction": 0.94}}
  ]
}
```

Each run has its own unique `id` and a `metrics` object. The policy defines
which metric names matter, reference values, and tolerances in the same units.
This document is a results collection, not a JSONL stream. A final value under
`summary.json.results` alone does not create it or run scientific acceptance.

### Investigation, fields, queries, and cooperative runtime modules

These modules have more specific schemas than scalar application metrics.
Use their complete examples or example generators; their identities, clocks,
coverage, and protocol messages cannot be reconstructed from ordinary progress
records.

| Module | Exact input and implementation source |
| --- | --- |
| Numerical stability | [Numerical schema](../schemas/investigation-numerical.schema.json) and [collector example](../../examples/scientific-investigation/collect_numerical.py). Named reference metrics, absolute tolerances in metric units, relative tolerances as fractions, and actual per-configuration values. |
| Regression and diagnostic trials | [Regression schema](../schemas/investigation-regression.schema.json), [campaign schema](../schemas/investigation-diagnostic-campaign.schema.json), [regression generator](../../examples/scientific-investigation/prepare_regression_demo.py), [campaign generator](../../examples/scientific-investigation/prepare_demo.py). Real repeated observations, declared units/pairs, and scientific validity; [implementation guide](../guides/scientific-investigation.md). |
| Staged investigations | [Workflow](../schemas/investigation-workflow.schema.json), [recommendation](../schemas/investigation-recommendation.schema.json), [lineage](../schemas/investigation-lineage.schema.json), and [planner](../schemas/investigation-planner.schema.json). The [generator](../../examples/scientific-investigation/prepare_demo.py) writes a complete illustrative workflow and sources; replace its explicitly synthetic identity before job attachment. |
| Fixed-DAG execution scenarios | [Model example](../../examples/runtime/execution-model.json), [schema](../schemas/execution-model.schema.json). Explicit work/rate units, resources, routes, placement, and measured calibration/held-out evidence; [model boundaries](../guides/runtime-research.md#execution-scenario-laboratory). |
| Diagnostic intent | [Intent](../../examples/runtime/diagnostic-intent.json), [capabilities](../../examples/runtime/site-capabilities.json); [intent schema](../schemas/diagnostic-intent.schema.json), [capability schema](../schemas/site-capabilities.schema.json). Observation requirements and actual site adapter capabilities. |
| Information query | [Numeric source](../../examples/runtime/query-source.json) plus [query](../../examples/runtime/summary-query.json); [source schema](../schemas/query-source.schema.json), [query schema](../schemas/summary-query.schema.json). Each source row has a unique raw `ref` and numeric `values`; query operations are `count`, `sum`, `min`, `max`. |
| Scientific field or slice | [Field example](../../examples/runtime/scientific-field.json), [schema](../schemas/scientific-field.schema.json). Exact `identity`, `units`, 1D/2D/3D `shape`, optional `axes`, and row-major flattened `values`. This format supports explicit missing values; it is not scalar `metrics.jsonl`. |
| Cooperative runtime steering | [Control configuration](../../examples/runtime/control-config.json), [request](../../examples/runtime/runtime-request.json), [application](../../examples/runtime/cooperative_app.py); [control schema](../schemas/runtime-control.schema.json), [request schema](../schemas/runtime-request.schema.json). Application opt-in, safe points, epochs, and acknowledgments are required. |
| Cooperative I/O permits | [Request](../../examples/runtime/io-request.json), [schema](../schemas/io-request.schema.json), [integration](../guides/runtime-research.md#cooperative-io-permits). Real participant identities, declared work, and acknowledgments. |
| Investigation timeline | [Timeline example](../../examples/runtime/investigation-timeline.json), [schema](../schemas/investigation-timeline.schema.json), [clock rules](../guides/investigation-timeline.md). Explicit source clocks and attempt identities; cross-node timestamp order alone does not prove causality. |
| Python stacks and cooperative native events | [Recorder and SDK guide](../guides/stack-tracing.md). Use the recorder's manifests and journals, sequence/checksum chains, declared coverage, and optional supported PMPI adapter. Do not hand-write a journal from metric rows. |
| Site Prometheus telemetry | [Query example](../../examples/platform/prometheus-query.json), [schema](../schemas/prometheus-query.schema.json), [site integration](../../examples/platform/README.md#read-an-existing-prometheus-site-service). Exact cluster/job/attempt labels and exporter-defined names/units are required. Managed Query currently awaits a governed network transport. |
| Portable research evidence | [Perfetto](../../examples/platform/perfetto.json), [Caliper](../../examples/platform/caliper.json), [RO-Crate](../../examples/platform/ro-crate-metadata.json), [evidence receipt](../../examples/platform/evidence.json), [receipt schema](../schemas/evidence.schema.json). Use the [supported format boundaries](../../examples/platform/README.md#inspect-and-exchange-evidence) for these formats and OTLP JSON spans. |

Nextflow's supported columns describe task identity and state. Additional
native trace CPU, memory, or duration columns are not imported as Analytics
metrics. Prometheus labels likewise need exact job-attempt attribution; a lab
account label alone cannot assign a node-wide measurement to one student.

The [operation catalog](../guides/operations.md) covers additional controls
that use Raven's current scheduler observations or native action records.
Those records are not additional application metric files to fabricate.
Native passports must be created by Raven's provenance API or preparation
workflow; copying `run.json` into a passport directory does not create one.

## 10. Retained launch telemetry and export bundles

`raven launch` manages these additive files; applications normally do not need
to write them. Its [workflow guide](../guides/automated-reporting-exports.md)
explains collection choices and the plotting-ready output columns.

| Contract | Required interpretation |
| --- | --- |
| [`raven.telemetry-index/v1`](../schemas/telemetry-index.v1.schema.json) | `run_id` binds the exact attempt. Each source declares its `family`, relative `path`, row `schema`, `status`, `reason`, `coverage`, known `metrics`, and `devices`. |
| [`raven.telemetry-index/v2`](../schemas/telemetry-index.v2.schema.json) | New recordings add ordered `segments`, invariant `row_defaults`, and `storage_format`. Coverage distinguishes lifetime, retained and evicted sample counts. Existing v1 indexes remain readable. |
| [`raven.telemetry-compact/v1`](../schemas/telemetry-record.v2.schema.json) | A compact stored row supplies `sequence`, finite epoch `timestamp`, `elapsed_seconds`, changing observations and an optional `device` index. Readers merge `row_defaults` and the selected device descriptor, then validate the expanded measurement against its declared v1 schema. |
| [`raven.gpu-sample/v1`](../schemas/gpu-sample.v1.schema.json) | Complete JSONL records with sequence, run/node/device identity, collector UTC timestamp, monotonic elapsed time, supported utilization/memory counters, units in field names, scope, and quality. |
| [`raven.cpu-memory-sample/v1`](../schemas/cpu-memory-sample.v1.schema.json) | Complete JSONL records with the same clock/identity conventions and local process observations: `cpu_seconds`, `rss_bytes`, `process_count`, `cpu_scope`, and `memory_scope`. |
| `raven.export-manifest/v1` | Immutable export selection, attempt identities, per-source byte cutoffs, coverage/outcomes, transformations, and output paths/counts/checksums. |

The index lives at `RUN/telemetry/index.json`; its streams are
`telemetry/gpu.jsonl` and `telemetry/cpu-memory.jsonl`. `timestamp` is Unix epoch
seconds and `timestamp_utc` is its explicit UTC spelling in expanded records.
V2 also lists sealed segments and stores invariant fields once in the index;
readers reconstruct the expanded records before validation and export. See
[recording storage and recovery](../guides/recording-storage.md) for segment
retention, storage budgets and interrupted-write recovery. `source_time` remains
`null` when the vendor or procfs supplies no clock. Collector timestamps do not
establish simultaneous cross-node measurements. `elapsed_seconds` is monotonic
duration from recording start. A sequence is local to its producer stream.

GPU identity uses node/provider/UUID and the known device index or partition.
Never merge unrelated physical devices merely because both have index zero.
`utilization_percent` is 0–100; `memory_used_mib` and `memory_total_mib` use MiB.
Unsupported counters remain `null` in retained JSONL and empty in exported CSV.
Device-wide utilization does not establish exclusive per-process accounting.

CPU time is the sum of currently observed local child-process counters; exited
processes can disappear between samples. It is not a cumulative whole-job
accounting total. Summed RSS can double-count shared pages and excludes unseen
remote ranks. Read `scope`, `cpu_scope`, `memory_scope`, and coverage before
comparing it with Slurm allocation or peak-RSS summaries.

The exporter emits long-form application metrics, fixed-schema GPU/CPU tables,
native summaries/reports/traces, and explicitly requested declared logs or
artifacts. Its schemas are [request](../schemas/export-request.v1.schema.json),
[plan](../schemas/export-plan.v1.schema.json),
[manifest](../schemas/export-manifest.v1.schema.json), and
[receipt](../schemas/export-receipt.v1.schema.json). None replaces the run or
application-metric contracts above. Exporting preserves missing observations,
source order, scientific units, and available timestamp precision.

## Validate an integration

1. Run the [kit smoke test](../../examples/reporting-v2/README.md) on a tiny
   local command. Inspect the generated manifest, complete metric lines, log
   index, and terminal summary before adding scheduler options.
2. Open the exact attempt through `:project` and `:run select`. Confirm that
   its job ID, attempt, paths, and measured units agree with the application.
3. Validate the declared outputs with
   `raven run validate .raven/contracts/outputs.v1.json /absolute/project/runs/RUN_ID`.
   This checks declared artifacts; it is not a replacement for the schemas.
4. In project CI, validate JSON documents against their linked schema and
   JSONL one line at a time. Use the native reader as well for duplicate keys,
   finite values, identity, confinement, and scientific consistency checks.
5. For a completed-job graph, retain its recorded measurements. A summary
   containing one peak or total cannot reconstruct an unrecorded time series.

The [metric catalog](project-reporting-metrics.md) explains what should become
visible in each panel and why an unavailable measurement stays unknown.
