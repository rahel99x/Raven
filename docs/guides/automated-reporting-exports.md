# Launch, inspect, and export an existing job

[README](../../README.md) · [Reporting formats](../reference/project-reporting-formats.md) · [Metric catalog](../reference/project-reporting-metrics.md) · [Runnable example](../../examples/automated-reporting/README.md)

`raven launch` runs an existing Bash or POSIX-shell script with an exact run
record and optional retained measurements. `raven export-data` exports those
records as CSV, JSON, and native evidence. Neither workflow requires preparing
a Raven manifest. The dashboard can be closed while the job runs.

Use **Run** to start work, **Results** to inspect retained evidence, and
**Export data** to choose what to save. A feature selection requests an
integration; its readiness explains whether that integration has data.

## Guided launch in the dashboard

Open **File → Launch job**, use the Launch toolbar button when visible, or
enter `:launchjob`. Choose the project and script, review the workload account
and resources, select recordings, then inspect the final request before
submitting. The review includes an equivalent command for repeated or scripted
use. The [first-job guide](getting-started.md) includes installation and site
selection.

The launcher reads supported `#SBATCH` directives without rewriting the script.
It explains effective values and their sources, including inherited scheduler
environment settings. Blank resource fields inherit those choices; values
entered in the form become explicit launch overrides. Saving a named workload
profile is separate from submission, and its defaults fill missing settings on
later launches without replacing explicit script or environment choices.
Selecting GPU recording does not itself request a GPU allocation;
scientific measurements still need application output.

Run this workflow on the host that owns the project and can submit its job.
An SSH monitoring connection cannot launch a local project on that remote
cluster. Use Raven on the permitted cluster submission host instead, with the
project on storage visible to the execution nodes. Select standalone local
execution only when you intend to run the workload on that machine.

The advanced **Setup → Launch defaults** editor remains
available for measurement bindings, scientific context, recipes, and completion
exports. It is separate from both the launch review and the Export data screen.

Standalone launches from the dashboard run under a detached supervisor and
retain stdout/stderr files. Closing the dashboard does not stop the accepted
workload. The receipt provides its PID, log paths, and stop instructions.
Command-line local launches retain their foreground behavior described below.

## 1. Preview an existing script

From the directory where you normally submit the script:

```bash
raven launch test.sh --research experiment scaling forecast --gpu --trace --check
```

The preview reports the original script, its project, the effective working
directory, the workload account and its source, execution mode, and feature
readiness. `--check` does not create project state, submit a job, allocate Heavy,
or run the script. It is a static check, not a promise that Slurm will accept
the request or that a compute node has a particular interpreter or GPU tool.

Slurm is the default. A missing `sbatch` produces an error rather than local
execution. On a Linux workstation where local computation is appropriate,
select `--local` explicitly:

```bash
raven launch test.sh --local --research experiment --check
raven launch test.sh --local --research experiment
```

A local run has a Raven run ID, without an invented Slurm job ID. Do not use
local mode to run a batch workload on a cluster login node.

These two commands request the same research integrations:

```bash
raven launch test.sh --research scaling forecast --trace --gpu
raven test.sh --r sc f --trace --gpu
```

`--research`, `--r`, and `--re` are exact option aliases. With no values,
`--research` selects basic Experiment/Evidence wiring. It does not start every
collector. An unknown feature is an error. Existing `raven run`, `raven report`,
and `raven trace` commands retain their meanings; use `raven launch ./run` for
a script whose name resembles a subcommand.

Put application arguments after `--`. Their boundaries, empty strings, and
leading dashes are preserved; Raven does not evaluate them as shell text:

```bash
raven launch test.sh --research experiment -- --epochs 100 --label 'run A' ''
```

## 2. Choose the workload account and resources

On CARC, UCI HPC3, or another Slurm cluster, keep the site's module loads,
environment activation, partition, QoS, and resource directives in the script.
Choose an account granted to your own username:

```bash
raven launch test.sh --account LabX \
  --sbatch=--partition=gpu --sbatch=--time=01:00:00 \
  --research experiment evidence --gpu -- --epochs 100
```

`LabX` illustrates a professor's allocation; it is not a universal account
name. The submitting user remains the student. `--account` after `launch`
controls this workload's billing and does not change Raven Heavy's saved
account. **Setup → Connections and accounts → Sites and lab profiles** saves the connection and Heavy
preferences; workload defaults belong to the project and are saved separately
through the guided launch workflow. Desktop Slurm users can omit the workload
account when their scheduler supplies the
appropriate default. See [site recipes](project-reporting-sites.md) for the
distinction between a username, a Slurm account, a partition, and an allocation.

Use one repeated `--sbatch=TOKEN` per scheduler argument, preferably a complete
`--option=value` token. Slurm's precedence is preserved: command-line options,
then `SBATCH_*` environment settings, then effective `#SBATCH` directives.
Project or selected-recipe resource defaults fill unspecified settings; they
do not override existing directives or `SBATCH_*` environment choices. Only directives before
the script's first executable line are part of the Slurm header.

The project root is a reporting and export anchor. It does not replace the
submission working directory. An effective `--chdir` remains the job's working
directory; relative log paths keep that meaning. This differs from the older
`raven report submit` workflow, which deliberately selects the project root as
WorkDir.

Raven snapshots the submitted bytes and packages a private reporting runtime
before submitting once. The receipt includes the known job ID, launch locator,
project, and readiness. `--json` produces a machine-readable receipt while
diagnostics go to stderr. Local execution requires `--json --receipt-file PATH`
to write the receipt to a new file while preserving workload stdout/stderr.
Local human-readable receipts go to stderr. An ambiguous response is not retried:
inspect the retained launch intent and scheduler state before submitting again.

Local execution inherits stdout/stderr and does not automatically tee them
into run-local logs. Explicitly register application log files when retention
is needed. Slurm launches register the original known stdout/stderr paths.

The wrapper supports the validated Bash/POSIX-shell interpreter forms shown by
the preview. It does not translate Python, zsh, csh, heterogeneous submissions,
multi-cluster requests, or conflicting `--wrap` commands. It executes the
snapshot as a child rather than sourcing it. Scripts that derive data paths
from `$0` or `BASH_SOURCE` must account for the relocated snapshot; use the
preserved working directory, an explicit project path, or an application hook.
Ordinary relative paths are relative to WorkDir, as in the original job.
Supported shebangs are absolute `bash`/`sh` paths with an optional combined
`-[efuvx]+` flag, or exactly `/usr/bin/env bash` or `/usr/bin/env sh`.
`env -S` and login-shell flags are rejected rather than reinterpreted.

Isolated environment policies are preserved explicitly. The generated wrapper
embeds its required reporting context without converting `--export=NONE`,
`NIL`, or an allowlist into `ALL`. Unsupported export-file policies fail with
guidance. The older tracked `report submit` route still requires its documented
handshake. For a site-owned launcher, use the explicit
[start/finish integration](research-automation.md#existing-batch-scripts-and-launchers).

## 3. Associate the right project

The script is resolved from the caller's original directory. Project discovery
then considers the explicit `--project`, existing unambiguous associations,
nearest Raven project, Git worktree root, and script directory. The preview
names the selected source. Discovery uses bounded ancestors, not a recursive
project scan. A different project selected in the dashboard does not redirect
the job's output. An explicit root must contain the script.

```bash
raven launch scripts/train.sh --project /shared/alice/model --check
```

The project and generated runtime must be writable and visible on the execution
node. An interpreter path visible on a login node is not proof of compute-node
availability. Keep site module/environment setup in the workload; configure a
reporting interpreter available independently of the application virtualenv.
Raven does not automatically install packages or execute project discovery code.

Launch choices resolve from explicit options, a selected `--recipe`, project
settings, then defaults. Explicit disabled options remain disabled. The
resolved settings are captured for that launch; later preference changes do
not alter an active job or its completion export.

For a one-time integration, connect the project and open **Setup → Project
launch and recording defaults**, or `:launchsetup`. Bind an existing metric
file, declared artifacts, an array manifest, scientific context, a reporting
Python, or an explicit trace adapter. Save named recipes and choose a captured
completion-export preset there. Opening and saving the editor do not execute
the application. The editor maintains `.raven/automation.json`; no manual
manifest editing is required.

The machine contracts are
[`raven.launch-plan/v1`](../schemas/launch-plan.v1.schema.json) and
[`raven.automation-settings/v1`](../schemas/automation-settings.v1.schema.json).

## 4. Add scientific measurements when needed

Lifecycle, identity, and process outcome need no application changes. Scientific
loss, accuracy, residual, throughput, and progress need actual observations.
The optional producer in the [example](../../examples/automated-reporting/solve.py)
uses the supplied helper without importing Raven into the application environment:

```bash
"$RAVEN_REPORT_PYTHON" -I "$RAVEN_REPORT_HELPER" metric \
  --value loss=0.24 --value throughput=480 \
  --step 10 --phase train --completed 10 --total 100 --unit epochs

"$RAVEN_REPORT_PYTHON" -I "$RAVEN_REPORT_HELPER" artifact \
  outputs/result.json --format json
```

Use the real computed values. One coordinator writes a run's metric stream;
independent ranks must not append concurrently to the same ordinary JSONL file.
Array tasks have separate runs. Artifacts are explicit declarations rather than
a recursive copy of checkpoints, datasets, or the whole project.

An existing compatible JSONL producer can be bound with `--metrics-file PATH`.
Use `--artifact PATH` for declared outputs and `--research-source VIEW=PATH`
for an existing native Research report. These are declarations of actual files,
not instructions to parse arbitrary stdout or manufacture measurements. For
concurrent attempts, a binding can include `{run_id}` and the producer can use
`RAVEN_RUN_ID` to write its own file. A shared static metric path leaves attempt
ownership with the producer; Raven does not infer which run wrote each row.
Declared native reports remain exportable, while display requires a supported
reader for that report format. Attaching arbitrary JSON does not populate every
Research view. The
[format reference](../reference/project-reporting-formats.md) defines the native
contracts, including one complete JSON object per newline:

```json
{"t":1791801000.125,"step":10,"phase":"train","metrics":{"loss":0.24,"throughput":480},"progress":{"completed":10,"total":100,"unit":"epochs"}}
```

`t` is an observed Unix epoch time; `step` is an integer. Metrics are finite
numbers. Omit missing measurements. Zero means an observed zero. The native
helper's `--unit` describes progress; it does not assign a unit to every metric.
Exported metric units remain unspecified when the source declares none.

## 5. Understand readiness and recording scope

Readiness distinguishes **Ready**, **Will verify at job start**, **Needs input**,
and **Unsupported**. Runtime coverage can be **Recording**, **Partial**, or
**Failed**. A successfully submitted job may still have unavailable measurements.

| Research selection | Alias | Required evidence |
| --- | --- | --- |
| Experiment | `ex` | Lifecycle is automatic; application measurements need a producer. |
| Arrays | `arr` | Task identity is automatic; scientific task labels need an array manifest. |
| Evidence | `ev` | Actual retained application and scheduler observations. |
| Artifacts | `ar` | Explicit output declarations and any required checks. |
| Passport | `pa` | Bounded source identity and explicitly selected inputs/environment. |
| Submit | `su` | The prepared launch and actual submission receipt; never a second submission. |
| Resources | `resource`, `re` | A native `predict` query and comparable completed observations. |
| Forecast | `f` | A permitted observer while pending; later execution cannot recover missed forecasts. |
| Blockers | `b` | Observed pending reasons and visible scheduler context. |
| Tradeoffs | `t` | Explicit candidates and comparable measured outcomes. |
| Scaling | `sc` | Worker counts, problem sizes, repeats, and comparable measured runtimes. |
| Workflow | `w` | Declared workflow metadata; scheduler dependencies retain their actual expressions. |
| Operations | `op` | Explicit supported manifests/receipts; selecting it applies no operation. |

Full selector names are the lowercase display names. `--re` is a Research
option, while the value `re` means Resources. `--workers`, `--problem-size`,
`--repeat`, and repeated `--parameter KEY=JSON` provide declared scientific
context; they do not prove runs are comparable or alter scheduler resources.
See the [metric catalog](../reference/project-reporting-metrics.md) for every
Research and Analytics source.

`--gpu` requests job-owned retained sampling inside the allocation. It does not
reserve a GPU, run Heavy, or enable dashboard vendor probes. Device identity
must be verified from assigned visibility; device 0 is not assumed. Counters
are device observations, not exclusive per-process usage. Missing counters
remain missing. Provider/tool availability and shared-device ambiguity appear
in coverage. Legacy four-column NVIDIA traces remain readable with their
original index, whole-second, and local-time limitations.

Retained CPU/memory process observations cover the supported local child process
tree. CPU time sums currently observed processes and can decrease when a process
exits; it is not a final job CPU total. Summed RSS can count shared pages more
than once. Neither series includes unobserved remote MPI ranks or reconstructs
a Slurm timeline from final accounting. Allocation counts, sampled RSS, process
CPU time, and scheduler summary peaks remain distinct. Multi-node jobs report
partial node coverage; Raven does not silently add an `srun` sampler.

GPU timestamps use collector UTC time, with available microsecond precision at
probe completion, and monotonic elapsed time. Provider source time stays empty
when the vendor supplies none. Clock precision does not establish synchronized
nodes or a simultaneous measurement across separate tools.

Recording uses bounded streams, timeouts, cadence, and storage. Recorder errors
and storage exhaustion leave the workload's actual exit status intact and
mark coverage incomplete. Closing the TUI does not stop the job's recorder.
SIGKILL, node loss, or inaccessible storage can prevent a final summary; absence
of a summary is never success. Pending observations need the existing governed
observer; launch does not create a detached per-job scheduler daemon.

## 6. Trace a supported application launch

`--trace` requests application tracing, not shell `set -x` or arbitrary native
stack capture. Place the explicit hook after module loads and environment
activation:

```bash
source "$RAVEN_SHELL_HELPER"
raven_exec python3 train.py --epochs 100
```

Use `raven launch batch.sh --trace` for the CPython monitoring adapter on a
supported CPython 3.12+ application runtime. The explicit
`--trace-adapter python-legacy-profile` option supports the documented reduced
Python 3.10+ profile semantics. Raven does not silently replace a profiler or
rewrite hidden `python`, `srun`, or `mpirun` commands. The hook preserves argv
and application status; the outer wrapper owns the single reporting attempt.

The automatic hook supports a simple `python script.py [args]` invocation.
Python `-m`, interpreter flags, shell pipelines, and hidden distributed launches
run unchanged with an unavailable-trace explanation; they are not rewritten.

Native cooperative events, supported MPI instrumentation, and reviewed
`py-spy` snapshots are separate integrations with their own coverage and
permissions. See [stack tracing](stack-tracing.md). Without a supported hook,
the run remains usable and its trace readiness explains the missing input.

## Inspect the exact retained attempt

Open Raven on the intended project host and connect the project. For a local
run, use explicit project/run selection because there is no Slurm job row:

```text
:project /shared/alice/model
:run select RUN_ID
```

Use the receipt's actual run ID. For Slurm jobs, the exact binding follows the
job into Recents and History. Research Experiment shows application observations;
Research Submit shows **Run / Results · Captured launch**, including effective
options, their sources, original readiness, and the recorded application exit.
Viewing this report submits nothing. Analytics displays retained observations
under their measured scope. Read coverage before treating a short or partial
series as a full-run history. Logs remain their declared files.

Reopening after the dashboard was closed selects retained evidence, including
the independent recording/export outcomes. Launch readiness describes the
request; runtime coverage describes what the producer actually recorded.

Selecting or attaching a run requests its accessible retained telemetry
immediately. It refreshes independently of Slurm polling, every second locally
by default and at least every 1.5 seconds over a remote connection. Analytics
Job series and the corresponding Jobs inspector show **Retained samples loaded**
for CPU/memory and GPU, plus the actual read error. A deferred or failed read
retains the last successful samples and identifies those counts as cached.
Temporary “retry later” notices do not increase failure backoff. CPU and memory
curves remain available when GPU recording is missing or failed. See
[telemetry updates](telemetry-updates.md) to adjust Heavy readers live.

## 7. Export retained data from the command line

Use the exact run ID printed by the launch receipt or selected in Results.
Repeat `--run` for specific attempts from the same project. An unqualified job
number is insufficient across clusters, requeues, and recycled Slurm IDs.

```bash
raven export-data --project /shared/alice/model --run RUN_ID \
  --include application,gpu,summary --metric loss --check

raven export-data --project /shared/alice/model --run RUN_ID \
  --include application,gpu,summary --metric loss

raven export-data --project /shared/alice/model --run RUN_ID \
  --include application --metric loss --metric accuracy \
  --relative-dir figures/experiment-a/data/

raven export-data --project /shared/alice/model --run RUN_ID \
  --include gpu --absolute-dir /scratch/alice/figure-data --json
```

Replace `RUN_ID` with the actual receipt value. Omission of a destination uses
the **Job's project folder**, under `exports/`. The entered custom directory is
a parent; Raven creates a uniquely named bundle beneath it. Missing components
are created only by the export, not by `--check`. Existing bundles are never
overwritten.

`--relative-dir` stays inside the job's project, including symlink checks.
Intentional external destinations require `--absolute-dir`. Spaces and Unicode
are allowed; there is no shell expansion, globbing, or implicit transfer. The
plan and receipt identify the filesystem host. This implementation supports
local filesystem sources/destinations only: an SSH project does not imply a
remote writer. Run the command on the project host when remote access is
unsupported. Raven does not silently redirect a remote path to this computer.
With a configured SSH connection, choose `--host local` explicitly only when
both source and destination are intentionally on this computer.

A shared project mounted at its recorded path on login and compute nodes can
retain one filesystem identity. Raven checks its `.raven/filesystem.json`
binding against the run's captured identity before allowing another access
host to use project-relative storage. The manifest distinguishes the original
filesystem host from the host performing the export. Identical path spelling
alone is insufficient. This is verified shared-file access, not a remote
transfer adapter; an arbitrary absolute directory outside the bound project
must still belong to the explicitly selected local host.

Available families are `application`, `gpu`, `cpu-memory`, `summary`, `research`,
`trace`, `logs`, and `artifacts`. `--include all` selects available measurement
and report families; logs and artifacts require explicit inclusion. An explicit
missing family remains partial even when combined with `all`. `--source` selects
an exact registered file ID (or `family:id`) for native evidence. Repeated
`--metric` and `--device` select individual identities. `--layout metric` splits
application metrics into separate files. `--start` and `--end` select epoch or
UTC ISO times; the default is the full retained range. Missing requested data
produces an explained partial result, never an invented zero-valued series.

CLI exits are stable: **0** complete, **3** partial output, **2** invalid request,
**4** failure, and **130** cancellation. A partial result may contain useful
files; inspect its receipt before treating it as a complete dataset. Export
does not query Slurm, sample a device, rerun a workload, or allocate compute.

### Export through the dashboard

Select a recorded attempt in Research, Analytics, Jobs, Recents, or History,
then choose **Export data** in the toolbar or **File → Export recorded data**.
The `:exportdata` command opens the same panel. Attempts are frozen when it
opens; completion, list refresh, or grouping changes cannot redirect the
request. For a group, review the admitted member count and identities.

1. Select measurement families and individual metric/device identities.
2. Choose the full retained range or explicit time bounds. **Export this chart**
   (`:exportdata chart`, or Shift-right-click on a recorded chart) preselects
   supported identities and the chart's epoch time range.
3. Choose category CSVs or a separate file per application metric.
4. Choose project `exports/`, a project-relative parent, or an absolute parent.
5. Activate **Review**, inspect actual host, paths, source availability and
   cutoffs, then **Export**. Progress and cancellation run in the background.

Use Tab/arrows to navigate, Enter/Space to activate, Page Up/Down to scroll,
and Escape to close. The File entry remains available in narrow terminals.
Per-project preferences and named presets retain useful choices; unavailable
preset sources remain visible. Completion choices apply only when explicitly
saved or captured. Existing table/screen and History log exports retain their
separate commands and purposes.

## 8. Read an exported bundle

Each bundle contains `manifest.json` and `README.txt`, plus selected evidence:

| File or directory | Meaning |
| --- | --- |
| `application-metrics.csv` | Long-form application observations: one row per metric value. |
| `gpu.csv` | Retained node/device counters with explicit units and scope. |
| `cpu-memory.csv` | Supported process CPU/memory observations and their scope. |
| `summary.json` | Native structured outcome and summary observations. |
| `research/` | Declared native scientific/planning reports; predictions remain labeled. |
| `trace/` | Native trace evidence and coverage, not rendered pixels. |
| `logs/`, `artifacts/` | Explicitly selected declared files. |

Multiple attempts have separate subfolders. The manifest lists actual paths,
source versions, per-source byte cutoffs, identities, schema/column definitions,
row counts, byte sizes, checksums, and any skipped or incomplete data. Consult
it instead of assuming a filename exists for an unavailable family.

CSV is UTF-8 with standard quoting. Empty numeric cells mean unavailable, not
zero. Values retain source precision rather than chart display rounding. Units
and measurement scope are explicit; an unspecified unit is not automatically
dimensionless. UTC/epoch times retain the available source precision. Original
clock information remains visible when timezone or cross-node alignment is
uncertain. Rows retain source order; there is no smoothing, interpolation,
averaging, or forced cross-node time merge.

The tabular contract has these exact column groups:

| Group | Columns |
| --- | --- |
| Identity, every table | `project`, `project_id`, `host`, `run_id`, `attempt`, `cluster`, `job_id`, `array_parent`, `array_task`, `submission_token`, `submit_time`, `script_sha256` |
| Clock, every table | `timestamp`, `timestamp_utc`, `source_time`, `clock`, `elapsed_seconds`, `sequence` |
| Application values | `metric`, `value`, `unit`, `phase`, `step`, `rank`, `node` |
| GPU values | `node`, `provider`, `device_uuid`, `device_index`, `device_bdf`, `partition`, `utilization_percent`, `memory_used_mib`, `memory_total_mib` |
| CPU/memory values | `node`, `cpu_seconds`, `rss_bytes`, `process_count` |
| Interpretation, every table | `source`, `scope`, `quality`, `status`, `reason` |

Each table uses identity, clock, its own value group, and interpretation in
that order. GPU memory is MiB; RSS is bytes; CPU and elapsed time are seconds.
`partition` in the GPU table denotes a device partition, not the Slurm queue.
Unknown identities stay empty. The manifest schema is `raven.export-manifest/v1`;
each payload entry includes `path`, `bytes`, `records`, and `sha256`. CSV
`records` counts data rows without its header.

Export snapshots source identities and byte watermarks, then streams complete
records up to those cutoffs. Concurrent append does not extend an in-progress
export indefinitely. An incomplete final line or malformed record is reported;
replacement, rotation, or truncation cannot silently become complete output.
Independent files do not represent a perfectly simultaneous global snapshot.

The writer stages a private bundle on the destination filesystem and publishes
without clobbering another export. Cancellation removes only owned staging
files; it preserves source evidence and user-created parents. Linux publication
uses exclusive `renameat2`; unsupported platform publication fails explicitly.
Crash durability and remote/NFS semantics require site qualification; no remote
publication guarantee is implied.

## 9. Save completion exports

Request a completion export at launch:

```bash
raven launch test.sh --research experiment --export-on-finish
```

Without a named preset, completion export selects available evidence and
excludes logs/artifacts unless explicitly requested. Use
`--export-on-finish PRESET` for a saved named export selection or
`--no-export-on-finish` to disable a saved default for this launch. Raven
captures the request and exact attempt policy before execution. A later preset
edit does not redirect a running job's export.

After bounded collector cleanup, completed, failed, or interrupted attempts
export their available evidence. The workload exit code and export result are
separate. A slow or failed export does not turn a successful application into
a failed application or keep an allocation alive indefinitely.

Pending requests are durable and keyed by exact attempt and captured request.
Repeated reconciliation cannot intentionally publish duplicate bundles. A
killed supervisor or unavailable filesystem leaves work pending for the next
governed reconciliation session. If no worker is running, there is no immediate
completion guarantee. Pre-start cancellation can contain launch/scheduler
evidence without application measurements. Requeues retain independent
attempts; published bundles are immutable.

Inspect pending/finished requests without running exports, or reconcile them:

```bash
raven export-data --project /shared/alice/model --reconcile --check --json
raven export-data --project /shared/alice/model --reconcile --json
raven export-data --project /shared/alice/model --reconcile --retry --json
```

Retry only after addressing a recorded failure; retries are bounded. Recovery
uses retained exact terminal evidence. A local process killed before it wrote
an outcome can remain pending rather than being mislabeled complete.

The durable contracts are
[`raven.auto-export/v1`](../schemas/auto-export.v1.schema.json) and
[`raven.scheduler-outcome/v1`](../schemas/scheduler-outcome.v1.schema.json).
The latter keeps an observed scheduler outcome separate from application exit.

## Resource bounds

| Resource | Bound |
| --- | --- |
| Selected attempts | 256 per request |
| Selected source files | 4,096 per request |
| Export metadata or JSONL record | 1 MiB each |
| Distinct selected metric names | 1,024 |
| Concurrent CSV outputs | 16, using a bounded reopen cache |
| Streaming chunk | 64 KiB |
| Recording cadence | Default 5 seconds; supported settings 1–300 seconds |
| Provider timeout | Default 2 seconds; supported settings 0.2–10 seconds |
| Retained stream storage | Default 32 MiB per family; configurable 4 KiB–256 MiB |

Recording stops at its storage cap and preserves the retained prefix with an
explicit limit reason. It does not silently overwrite early measurements.
Bounds apply separately from the governed dashboard scheduler collection policy.

## Verification and operational limits

The [example project](../../examples/automated-reporting/README.md) demonstrates
an ordinary script, actual application metrics, an explicit Python hook,
controlled failure, and the three destination modes. Its local acceptance
tests use the public CLI and ordinary CSV/JSON readers; installed-wheel
qualification separately checks packaging outside the source checkout.

Run `python3 scripts/benchmark_data_export.py --help` for reproducible streaming
benchmarks. The benchmark reports elapsed time, traced Python peak memory,
rows/bytes, independent checksum verification, and cancellation latency. These
are measurements on the machine running it, not claims of negligible cluster
overhead. Export work is proportional to selected input bytes and emitted rows;
buffers and record sizes are bounded, while selected source metadata scales
with selected attempts and files.

A baseline on a shared Linux development host, using Python 3.12.14 and
synthetic application records, measured:

| Input records | Metrics per record | Layout | Verified CSV rows | Planning + export | Traced Python peak |
| --- | --- | --- | --- | --- | --- |
| 1,000 | 2 | Category | 2,000 | 0.182 s | 1,212,488 bytes |
| 100,000 | 2 | Category | 200,000 | 14.077 s | 1,211,263 bytes |
| 1,000 | 32 | Per metric | 32,000 | 6.151 s | 2,272,052 bytes |

The 100-fold row increase kept traced memory near 1.21 MB for the same selected
metadata. The 32-metric case exercised more outputs than the 16-file cache.
Independent CSV parsing verified every value and identity, plus payload byte
counts and SHA-256 checksums. Cooperative cancellation completed in
0.124–0.836 ms after the benchmark's cancellation callback first returned true;
this excludes UI event/worker queue delay and filesystem stalls.

These are single trials with `tracemalloc` enabled, excluding fixture creation
and subsequent verification. Shared host activity and filesystem caching affect
timing. They are bounded-memory evidence, not cluster throughput guarantees.
The [category record](../benchmarks/automated-reporting-export.json) and
[per-metric record](../benchmarks/automated-reporting-export-per-metric.json)
include the exporter source hash and machine/runtime details. Reproduce with:

```bash
python3 scripts/benchmark_data_export.py --rows 1000 100000
python3 scripts/benchmark_data_export.py --rows 1000 --metrics 32 --layout metric
```

Local and fake-scheduler tests do not establish real Slurm signal delivery,
GPU/MIG mapping, multi-node MPI coverage, site module availability, or shared
filesystem durability. Validate those on the intended partition with a small
job before using a large allocation. Unsupported adapters and missing evidence
remain visible in readiness, retained coverage, and export receipts.
