# Raven project reporting standard, version 1

This convention defines portable files for execution identity, application
measurements, log locations, outputs, and planning evidence. Applications can
produce these files without importing or installing Raven.

For a first integration, start with [Project reporting V2](PROJECT_REPORTING_V2.md).
It provides a portable launcher, workstation and cluster recipes, an exact
[file-format cookbook](reference/project-reporting-formats.md), and a complete
[metric and panel reference](reference/project-reporting-metrics.md).
V2 revises the guide and starter; this version 1 file standard remains current.

For Bash launchers, use the
[automated Research reporting guide](guides/research-automation.md).
`raven report` creates the standard files and links them to actual Slurm job
attempts. The copyable Python reporter supports projects without a Raven
installation.

When a launcher runs in Raven's optional embedded terminal, it can also publish
explicit command, submission, project and artifact observations through the
[private terminal bridge](guides/terminal-fusion.md#connect-shell-activity-to-research).
These observations supplement the files below. They do not replace the run
manifest or authorize scheduler actions. Use the exact `sbatch --parsable` result
and declared paths; Raven validates them before an explicit handoff.

Follow this convention when building a project you want to inspect with Raven.
It works with the current terminal application and does not require your
application to import Raven. Keep your existing source and build tools; add the
reporting files alongside them.

The [copyable project template](../examples/project-template/README.md) includes
a dependency-free reporter, a small working application, a batch script, and
configuration. [JSON Schemas](schemas/README.md) describe the interchange files.
Raven's native readers perform the final compatibility and evidence checks.

## Standard directory layout

```text
my-project/
├── README.md
├── src/                         application code; use your own source layout
├── configs/                     scientific parameters and input declarations
├── jobs/
│   └── run.sbatch                batch entry point
├── .raven/                      reusable, versioned Raven integration
│   ├── config.json
│   ├── contracts/
│   │   └── outputs.v1.json
│   └── definitions/             optional experiment and workflow recipes
│       ├── scaling.json
│       ├── workflow.json
│       └── operations/          optional explicit policies and campaign recipes
├── runs/
│   └── <run_id>/                one execution attempt, created exclusively
│       ├── run.json             identity, lifecycle, and declared file paths
│       ├── logs.json            grouped index of exact log locations
│       ├── metrics.jsonl        live numeric measurements and progress
│       ├── summary.json         final measurements and scientific results
│       ├── reports/             optional native Research sources for this job
│       │   ├── planning.json    shared source for the six planning views
│       │   ├── predict.json     Resources source, if different from planning
│       │   ├── forecast.json    actual scheduler prediction observations
│       │   ├── blockers.json    actual scheduler snapshot and details
│       │   ├── tradeoffs.json   explicit comparable resource candidates
│       │   ├── scaling.json    controlled measurements or a scaling recipe
│       │   ├── workflow.json   actual dependency recipe and timing evidence
│       │   ├── submit.json     captured read-only native submission preflight
│       │   └── operations/    declared science, profiler, or workflow evidence
│       ├── outputs/             tables, models, checkpoints, other results
│       ├── logs/
│       │   ├── stdout.log
│       │   └── stderr.log
│       └── passports/           immutable Raven provenance records
├── reports/
│   ├── planning.json            bounded aggregate of explicitly selected runs
│   ├── operations/              explicit campaign/cohort operation evidence
│   └── arrays/
│       └── <cluster>-<array_id>.json  optional exact-index scientific mapping
└── logs/                        optional Slurm output opened before job startup
```

`logs/`, `outputs/`, and `passports/` can stay empty until they have real data.
Recipes are optional: add them when the project actually has a controlled
scaling experiment or a dependency graph.

Track integration configuration, contracts, recipes, schemas, and reporting
code in Git. Keep generated run directories, reports, scheduler logs, and
private passports out of Git by default. Share a selected, reviewed run bundle
when someone needs to reproduce or inspect its results.

Raven's normal submission receipt may store passports under `.raven/passports/`
in the batch workdir. Ignore that generated subdirectory. An explicit
`--passport-dir` can associate the passport with a particular run directory.

## Identity and path rules

Use these identities consistently:

| Field | Meaning | Example |
| --- | --- | --- |
| `run_id` / summary `id` | Unique execution attempt; never reuse it | `fit-20261004T120000Z-a1` |
| `experiment_id` | Logical experiment shared by its attempts | `regression-v3` |
| `attempt` | Positive retry number within that experiment | `1` |
| `name` | Stable workload name used for comparisons | `my-project/regression-v3` |
| `job_id` | Actual Slurm scheduling identity, when known | `12345` or `12345_7` |

A project-scoped prefix or a UUID keeps run IDs distinct when you combine
projects. Use 1–128 characters from letters, digits, `.`, `_`, and `-`, starting
with a letter or digit. Keep a new directory for each retry, restart, or array
task. A changing job ID or run ID belongs in its identity field; putting it into
the stable workload name would prevent comparison of repeated work.

For an array task, use the array's parent job ID plus task index as its Slurm
identity. Keep every task's reports separate. Batch/extern steps and repeated
snapshots are not independent experiments.

### Scientific array identities

For a submitted array, publish an optional
`reports/arrays/<cluster>-<array_id>.json` using
[`raven.array-manifest/v1`](schemas/array-manifest.schema.json). The
[example](../examples/array-manifest.json) shows the required structure.
This file maps actual array indices to scientific IDs, labels, scalar
parameters, and declared input/output paths.

Use the exact cluster and parent array ID from the scheduler records. An empty
cluster is an exact empty identity, not a wildcard. Each entry requires a
unique integer `index` and a unique scientific `id`; `label` defaults to that
ID. Sparse indices are valid. Preserve one complete revision per launch and
use a new parent binding for retries submitted as a new array.

Attach it with `:arraymap /absolute/project/reports/arrays/CLUSTER-ARRAYID.json`
or **Load input map** in Research Arrays. `:arraymap inspect` opens the mapping;
`:arraymap search TEXT` filters scientific entries. The same controls are
available in Jobs and History Details. A job row keeps its actual Slurm ID.

For automatic attachment, declare a confined run-relative `paths.array_manifest`
in `run.json`. The selected run's exact cluster, array parent, and task identity
must match the map. A map outside that declaration needs explicit attachment;
placing it in the project directory alone does not load it.

Automatic maps follow validated run declarations. A manually attached map retains
its reviewed revision until `:arraymap reload` is requested. Raven checks the
attached file's metadata in the background and reports a changed source.
Retry plans that use the mapping verify its revision before proceeding.
Only one map is attached per session. Its connection and exact cluster/parent
scope must match. An available submission timestamp also guards the observed
attempt; conflicting or reused-parent evidence requires reattachment. If Slurm
exposes no submission timestamp, Raven displays that limitation and does not
claim to distinguish reused parent IDs by attempt.

Paths in an entry are descriptive declarations. Use project-relative paths for
portability, but do not expect this reader to resolve, open, validate, or stage
them. The array mapping does not replace per-task `run.json`, `logs.json`,
metrics, or output contracts.

Limits are 4 MiB per manifest, 10,000 explicit entries, 64 scalar parameters per
entry, and 32 input and 32 output paths per entry. Raven rejects duplicate
keys/indices/IDs, nonfinite numbers, invalid fields, and concurrent writes.
An unmapped task stays unmapped. See the
[phase one guide](guides/phase-one.md#attach-a-scientific-array-manifest) for all
controls and source-change behavior.

### Group identity and shared paths

Use a shared launch marker when several independent jobs belong to one submitted batch.
See [Group jobs from one launch](guides/batch-launches.md) for scheduler markers, local tags, automatic deduction, and historical coverage.
Each grouped job must still publish its own exact `job_id` and run attempt.
A display group does not merge reports or make one job's logs valid for another job.

Use relative, forward-slash paths in the shared project files. Their bases are:

| Path | Resolved relative to |
| --- | --- |
| `run.json.paths` entries | That run's directory |
| `run.json.provenance.script` | The project root |
| Output contract entries | The run root passed as `--workdir` or to `validate` |
| Relative `research.metrics_file` | `research.workdir`, including a CLI override |
| Relative `logs.manifest_file` | Selected `research.workdir` (`--workdir`), otherwise the selected job's actual WorkDir |
| Relative `logs.json` entries | The directory containing that log index |
| Contract/config/planning filenames supplied to Raven | The shell's current directory |
| A recipe's relative batch script | The explicit planning workdir |

Launch the documented commands from the project root. Use an absolute run root
when passing `--workdir`. An artifact contract names exact files, with no globs,
`..`, absolute paths, or symlink traversal.

`run_id` must match the attempt directory. `attempt` identifies its retry number.
The actual `job_id` links that attempt to its selected scheduler job. Job names,
file modification times, and the newest run directory do not establish identity.
The next section describes bounded runtime attachment. Explicit
`:project /absolute/project` and `:run select RUN_ID` remain available for
local runs and manual selection. Discovery works locally on the Slurm host,
including CARC or a native Fedora desktop, and through Raven's standard SSH
reader. The selected SSH host needs Python 3.10 or later; paths refer to that
host. Custom file adapters must support confined reads. See
[automatic reads and SSH](guides/research-automation.md#automatic-reads-and-ssh).

The existing explicit configuration workflow also works. Raven supports
`{job_id}` substitution in configured metric and log-index paths; it does not
expand that token in an artifact workdir. For a project whose directory names are actual
job IDs, `runs/{job_id}/metrics.jsonl` can follow the selected job with the
project root as the metric workdir. To validate that job's outputs, select its
concrete run directory as the artifact workdir.

## Automatic attachment and runtime updates

Make the scheduler's actual WorkDir equal the project root, or a directory
inside `PROJECT/runs/<run_id>`. Raven uses the selected job's cached controller
or accounting WorkDir to identify that standard location. It also considers a
project explicitly registered through `:project PROJECT`. It does not walk
arbitrary ancestors or search home directories, scratch mounts, or other projects.

Under a candidate root, publish this exact identity before emitting metrics:

```json
{
  "schema": "raven.run/v1",
  "run_id": "fit-a1-task7",
  "experiment_id": "fit",
  "attempt": 1,
  "state": "RUNNING",
  "job_id": "12345_7",
  "paths": {
    "metrics": "metrics.jsonl",
    "summary": "summary.json",
    "log_index": "logs.json",
    "outputs": "outputs",
    "passports": "passports",
    "planning": "reports/planning.json",
    "predict": "reports/predict.json",
    "forecast": "reports/forecast.json",
    "blockers": "reports/blockers.json",
    "tradeoffs": "reports/tradeoffs.json",
    "scaling": "reports/scaling.json",
    "workflow": "reports/workflow.json",
    "submit": "reports/submit.json"
  }
}
```

Each path is optional. Declare the sources your project actually publishes.
A file can appear later; Raven reports its absence until it exists. The template
declares standard shared-planning and preflight locations without creating fake reports.

Raven attaches only an unambiguous exact `job_id` match. `12345_7` does not match
the array parent `12345` or task `12345_8`. Missing job identity leaves a local
project run available for manual selection. Multiple attempts with the same
requeued job ID require explicit attempt selection; the newest directory is not
assumed correct. Invalid or capped inventory coverage also prevents a guessed binding.

Inventory discovery, report reads, and binding refresh use background work.
Selecting another job updates its attachment without opening another page.
Reports appended or atomically replaced during the session appear on subsequent
Research polls. Newly published logs, report paths, and passports are rechecked.
The current tab and line selection remain under the user's control.
When job details have not supplied WorkDir, register the root explicitly or
select the attempt; an unavailable scheduler path does not justify a filesystem scan.

Discovery retains the project picker's bounds: 256 attempts, 4,096 directory
entries, 64 KiB per inventory, and 8 MiB combined inventory bytes. Symlinks,
devices, traversal, and unstable reads are rejected. Runtime refresh does not
run project code, create aggregates, finalize application reports, or submit jobs.

All six planning views prefer their corresponding `paths.<view>` declaration.
Otherwise they use `paths.planning`, then an explicitly configured planning file.
These are exact run-relative declarations; an explicitly declared missing source
is reported missing rather than silently replaced by another job's report.
Old version-1 inventories without these optional keys remain supported.

## Files for every Research view

Use `RUN = PROJECT/runs/<run_id>`. Paths below are recommended concrete locations,
not recursive search rules. The inventory binds them to the job's exact identity.

| Research view / CLI ID | Place the producer's source here | Declare or supply | Required evidence and update behavior |
| --- | --- | --- | --- |
| Experiment / `experiment` | `RUN/metrics.jsonl` | `paths.metrics` | Complete JSONL `t` and finite `metrics`; optional `step`, `phase`, `progress`. Appends update graphs, values, progress, and conditional ETA. |
| Arrays / `arrays` | Each task's separate `RUN/run.json`, `metrics.jsonl`, and `summary.json` | Exact `job_id` such as `12345_7`; actual scheduler array records | The grid uses live/accounting task identities and states. No `arrays.json` reader exists; task reports attach when that exact task is selected. |
| Evidence / `evidence` | `RUN/logs.json`, its exact log files, and `RUN/metrics.jsonl` | `paths.log_index`, optional `paths.stdout` / `stderr`, `paths.metrics` | Actual job/details observations and bounded cited log excerpts. Updated indexes and logs are reread; no separate diagnosis JSON is consumed. |
| Artifacts / `artifacts` | `PROJECT/.raven/contracts/outputs.v1.json`, files under `RUN/outputs/` | Standard project contract and the selected run root | Required/optional exact output paths, formats, bounds, keys, rows, columns, or hashes. Atomic result publication updates validation; final outputs may be missing while running. |
| Passport / `passport` | `RUN/passports/<native-content-id>.json` | `paths.passports`, or an explicitly selected passport | Genuine immutable Raven provenance records. One verified record can bind automatically; choose explicitly when multiple records exist. Do not synthesize a passport from `run.json`. |
| Submit / `submit` | `RUN/reports/submit.json` | `paths.submit` | Intact `raven.submission-plan/v1` output from `raven run prepare`, including `plan_id`. Replacement updates read-only preflight display; it does not arm `:submit`. |
| Resources / `predict` | `RUN/reports/predict.json` | `paths.predict`, otherwise `paths.planning` | Native `raven.planning` object with `history` and an explicit comparable `query`. Measured summaries, including failures, support resource intervals only when evidence permits. |
| Forecast / `forecast` | `RUN/reports/forecast.json` | `paths.forecast`, otherwise `paths.planning` | Actual pending `jobs`, selected `job_id`, and optional real pre-start `observations` / historical outcomes. Updated source changes the analysis; recorded `now` keeps a historical snapshot frozen. |
| Blockers / `blockers` | `RUN/reports/blockers.json` | `paths.blockers`, otherwise `paths.planning` | Actual `jobs`, `job_id`, and matching `details`, `partitions`, `nodes`, `share`, `health` when known. Omitted snapshot facts remain unknown. |
| Tradeoffs / `tradeoffs` | `RUN/reports/tradeoffs.json` | `paths.tradeoffs`, otherwise `paths.planning` | Explicit `candidates`, comparable observed `history`, optional actual `queue_history`, and `coverage`. Unknown runtime or queue behavior stays unestimated. |
| Scaling / `scaling` | `RUN/reports/scaling.json` | `paths.scaling`, otherwise `paths.planning` | `raven.planning` `scaling` records with actual worker/repeat controls, or a native `raven.scaling` recipe. Report replacement updates analysis or reviewable plans without execution. |
| Workflow / `workflow` | `RUN/reports/workflow.json` | `paths.workflow`, otherwise `paths.planning` | Native `raven.workflow` recipe, or a planning bundle's `workflow`: node IDs, real dependencies, and measured timing intervals when known. Missing durations stay unknown; no automatic orchestration. |

The following sections specify measurements, final summaries, contracts, and
planning fields. [The planning guide](WAVE_TWO.md) covers native interpretation,
confidence, limits, and offline commands for the last six views.

<a id="publish-a-per-view-source-without-a-raven-dependency"></a>

### Publish a per-view source without a Raven dependency

The copied reporter supplies one coordinator API:

```python
from reporting import publish_research

# selected_bundle contains actual selected summaries and an explicit query.
publish_research(run, "predict", selected_bundle)
# actual_recipe contains your real dependency graph, not invented timings.
publish_research(run, "workflow", actual_recipe)
```

`publish_research` writes `RUN/reports/<view>.json` atomically, then declares that
exact location in `run.json.paths`. It supports `planning`, the six planning
view IDs, and `submit`. Documents must be finite JSON objects below 1 MiB.
It checks the native version/header, actual job identity when supplied, and an
intact preflight's review digest. Native Raven readers perform the remaining
scientific, chronology, topology, and coverage checks. This producer is not a
replacement for schema validation in the project's CI.

Publish an existing actual source through the same interface:

```bash
python3 reporting.py report forecast captured-forecast.json --run runs/my-run
python3 reporting.py report workflow .raven/definitions/workflow.json --run runs/my-run
```

Forecast and blocker files contain native input observations, not the analyzed
JSON output printed by `raven run forecast` or `raven run blockers`. Never label
those analysis results as fresh scheduler observations.

Create a per-run aggregate with explicit attempts:

```bash
python3 reporting.py export runs/trial-001 runs/trial-002 \
  --reference runs/trial-002 --output runs/trial-002/reports/planning.json
```

The default `reports/planning.json` remains a project-level aggregate for
explicit file commands. A confined inventory cannot point to `../../reports`.
Publish the selected aggregate into the target attempt's `reports/` to bind it
automatically. Repeat the export when the intended cohort changes; Raven reads
the new document but does not choose or aggregate scientific cohorts for you.

### Capture Submit evidence without submitting

Use the actual local preparation command and publish its result through the reporter:

```bash
preflight_file=$(mktemp)
raven run prepare jobs/run.sbatch --workdir "$PWD" > "$preflight_file"
python3 reporting.py report submit "$preflight_file" --run runs/my-run
rm "$preflight_file"
```

Review the command's exit status and output: a blocked native preflight is still
useful evidence, while a non-JSON command error is refused by the reporter.
The native marker is `raven.submission-plan/v1`; `plan_id` protects its exact
review fields. Retain captured paths and arguments intact. Source relocation
does not make a historical plan executable on the new machine. Use `:prepare`
again for a current interactive submission review; discovered files never
populate an executable plan or authorize a scheduler action.

## The files Raven consumes

| File | Producer | How Raven uses it |
| --- | --- | --- |
| `.raven/config.json` | Project author | `--config` attaches explicit report paths and preferences |
| `metrics.jsonl` | Application | Experiment graphs, latest values, progress, and conditional ETA |
| `.raven/contracts/outputs.v1.json` | Project author | Artifacts checks exact declared result files |
| `summary.json` | Application, then optionally scheduler reconciliation | Exported records become prediction/scaling evidence |
| `reports/planning.json` | Explicit aggregation step | Project-level offline analysis or a per-run shared planning source |
| `runs/<run_id>/reports/<view>.json` | Project coordinator | Exact job-bound source for one planning view or captured read-only Submit evidence |
| Recipe JSON | Project author | Scaling/workflow analysis and script preflight |
| Passport JSON | Raven's provenance API | Passport view and immutable evidence comparisons |
| `logs.json` | Application's run coordinator | Grouped Logs/Evidence catalog; bound through the selected inventory or explicit `logs.manifest_file` |
| Job stdout/stderr | Application and batch launcher | Logs uses paths reported by Slurm or retained from actual controller evidence |
| `run.json` | Application | Runtime discovery and project picker validate identity and bind declared reports; contracts can also check its presence/keys |

Raven validates `run.json` during bounded discovery or explicit selection,
reads metrics directly, and checks declared files through contracts. It analyzes
`summary.json` records after explicit placement in a supported planning bundle.
The template supplies that aggregation step; discovery does not manufacture it.

## Research operation contracts

Release 4.15 introduced explicit forms for twenty-six operations. Open `:ops catalog`,
or use a related view button. These sources are separate from automatic
`run.json` binding: adding a file to the recommended directory does not load it,
arm an action, or start a service. Set its exact path in the operation form and
select **Inspect / prepare**. See the [complete operation catalog](guides/operations.md).

Keep reusable policies and recipes in
`PROJECT/.raven/definitions/operations/`. Keep measurements for one attempt in
`PROJECT/runs/<run_id>/reports/operations/`. Keep an explicitly selected cohort
or campaign in `PROJECT/reports/operations/`. The names below are conventions;
the schema and exact path supplied to a form determine the reader.

| Source | Suggested filename | Form / contract |
| --- | --- | --- |
| Python, native-library, and optional container requirements | `.raven/definitions/operations/environment.json` | `environment`; [raven.environment/v1](schemas/science-environment.schema.json) |
| Scientific acceptance rules | `.raven/definitions/operations/acceptance.json` | `acceptance`; [raven.acceptance/v1](schemas/science-acceptance.schema.json) |
| Actual scientific measurements | `RUN/reports/operations/results.json` | `acceptance`; [result contract](schemas/science-results.schema.json) |
| Declared independent units or exact pairs | `reports/operations/statistics.json` | `statistics`; [comparison contract](schemas/science-statistics.schema.json) |
| Exact requested code, environment, and parameter identity | `.raven/definitions/operations/identity.json` | `reuse`; [identity contract](schemas/science-identity.schema.json) |
| Verified inputs, outputs, identity, and acceptance evidence | `RUN/reports/operations/reuse.json` | `reuse`; [reuse contract](schemas/science-reuse.schema.json) |
| Exact pending-job dependency repair recipe | `reports/operations/dependency-repair.json` | `dependency-repair`; [repair contract](schemas/science-dependency-repair.schema.json) |
| Checkpoint path/hash, completed source attempt, compatible restart recipe | `RUN/reports/operations/checkpoint.json` | `checkpoint`; `raven.checkpoint-restart/v1`, [example and rules](guides/operations-campaigns.md#checkpoint-recovery) |
| Discrete search space, budget, trial script, and observations | `reports/operations/search.json` | `search`; `raven.parameter-search/v1`, [example and rules](guides/operations-campaigns.md#adaptive-parameter-search) |
| Logical tasks and resource budgets | `.raven/definitions/operations/packing.json` | `packing`; `raven.packed-tasks/v1`, [example and rules](guides/operations-campaigns.md#short-task-packing) |
| Owned pool and scaling bounds | `.raven/definitions/operations/dask.json` | `dask`; `raven.dask-pool/v1`, [example and rules](guides/operations-campaigns.md#elastic-dask-pool) |
| Distinct Slurm components and coupled launch | `.raven/definitions/operations/heterogeneous.json` | `heterogeneous`; `raven.heterogeneous-allocation/v1`, [example and rules](guides/operations-campaigns.md#heterogeneous-allocation) |
| Declared input/output transfer paths, sizes, and hashes | `reports/operations/staging.json` | `staging`; [raven.staging/v1](schemas/staging.schema.json), [example](examples/staging-manifest.json) |
| Native Nextflow trace | `RUN/reports/operations/trace.tsv` | `workflow-engine`; [native TSV fields](guides/operations-services.md#nextflow) |
| Native Snakemake DAG or runtime plugin snapshot | `RUN/reports/operations/workflow-engine.json` | `workflow-engine`; [snapshot contract](schemas/workflow-engine.schema.json), [example](examples/workflow-engine.json) |
| Exclusive profiler rank/phase observations | `RUN/reports/operations/profiler.json` | `bottlenecks`; [raven.profiler/v1](schemas/profiler.schema.json), [example](../examples/scale-profiler.json) |
| Explicit cluster endpoints or profile references | `.raven/definitions/operations/clusters.json` | `clusters`; [workspace contract](schemas/cluster-workspace.schema.json), [example](../examples/scale-clusters.json) |
| Permitted node-event intervals and reasons | `reports/operations/incidents.json` | `incidents`; [raven.incidents/v1](schemas/incidents.schema.json), [example](../examples/scale-incidents.json) |
| Attempt energy, attribution, coverage, and accepted work | `reports/operations/energy.json` | `energy`; [raven.energy/v1](schemas/energy.schema.json), [example](../examples/scale-energy.json) |

`RUN` means `PROJECT/runs/<run_id>`. Read the
[science guide](guides/operations-science.md) before adapting the
[science examples](../examples/operations-science). Keep missing or unknown
measurements distinct from zero. A successful Slurm exit does not establish
scientific acceptance. Energy totals include failed attempts, and statistical
units must identify actual independent observations or explicit pairs.

Publish producer snapshots atomically. Do not rewrite the file while a reader
is validating its identity. Preserve exact cluster, job ID, submit/start
attempt, logical task, and workflow-run identities where the contract requires
them. Append only complete newline-terminated trace rows. A bounded tail is
not a complete workflow history.

Operation plans and service state are Raven-owned outputs. Do not synthesize
an action plan or copy one between connections. Common action receipts use
`raven.operation-receipt/v1` under Raven's state directory in
`operations-receipts/`; transfer, campaign, and service adapters also retain
their own durable records. Keep this private operational state out of project
Git history. Share a reviewed structured report when another researcher needs
the evidence.

The [operation guide](guides/operations.md#review-background-work-and-recovery-rules)
defines review lifetime, cancellation, worker behavior, and unknown-action
reconciliation. A schema validator checks document shape; the native reader
also checks identity, bounds, file contents, and graph semantics. A passing
schema check alone never authorizes a scheduler change or proves valid science.

## Log locations: `logs.json`

Keep a portable index beside each run's `run.json`. The index describes log
locations rather than copying or merging their contents. For example:

```json
{
  "schema": "raven.logs/v1",
  "run_id": "fit-20261004T120000Z-a1",
  "job_id": "12345",
  "logs": [
    {"id": "application.stdout", "path": "logs/stdout.log", "label": "Application stdout", "group": "Application"},
    {"id": "application.stderr", "path": "logs/stderr.log", "label": "Application stderr", "group": "Application"},
    {"id": "worker.rank-0", "path": "logs/rank-0.log", "label": "Rank 0", "group": "Workers"},
    {"id": "batch.stderr", "path": "/scratch/my-project/batch/12345.err", "label": "Batch stderr", "group": "Scheduler"}
  ]
}
```

Use the actual `run_id` and scheduler `job_id`, or omit `job_id` for a local run.
When `job_id` is present, Raven checks it against the selected job; a mismatch
does not attach another job's logs. The project picker also checks a supplied
`run_id` against the explicitly selected inventory.
Declare `"log_index": "logs.json"` under `run.json.paths`; the template does so
automatically. Selecting the run through `:project PATH` / `:run select RUN_ID`
binds that declared index. Without the picker, use the explicit configuration
binding:

```json
{"logs":{"manifest_file":"logs.json"}}
```

With `--workdir "$RUN_DIR"`, that index is read from the selected run. Otherwise
a relative configured index uses the selected scheduler job's actual WorkDir.
Use `runs/{job_id}/logs.json` from a known project workdir when run directories
use real scheduler IDs. If no real WorkDir is available, bind the concrete run
directory rather than relying on the shell's current directory.

| Index field | Rule |
| --- | --- |
| `schema` | Exactly `raven.logs/v1` |
| `logs` | Required array, at most 256 exact file entries |
| Entry `id` | Unique stable ASCII ID, 1–128 letters/digits/`.`/`_`/`-`, starting with a letter or digit |
| Entry `path` | Required nonempty printable path, at most 4,096 characters; no globs or backslashes |
| Entry `label` / `group` | Optional printable text, at most 160 characters each |
| Entry `description` | Optional printable text, at most 512 characters |
| `run_id` / `job_id` | Optional actual identities; use the selected job's exact scheduler ID |

Relative entry paths resolve from the **index directory**. Explicit `../`
locations can name sibling logs; unlike artifact-contract outputs, read-only
log attachments are not confined to the run root. Prefer relative paths within
the shared bundle. An absolute path may refer to an actual external file, but
keeps its source-machine meaning when the project moves. Copy that file into
the bundle and update its entry, or rebind the external path explicitly.

Publish UTF-8 JSON atomically, with finite values and unique keys, within the
256 KiB native read budget. Use one coordinator to register entries. The template's
`register_log(run_dir, id, path, label=..., group=..., description=...)` adds one
location atomically and refuses duplicate IDs or normalized paths. It never
reads log contents or scans directories. A declared log may not exist yet;
Raven reports missing files. Existing local files must be regular files with
real directory ancestors. Do not point entries at symlinks, pipes, or devices.

In **Jobs**, move to an active row or a **Recents** row and press `l`. In
**History**, select a job in any state and press `l`. Logs stays attached to
that exact job rather than switching to a currently running one. Press `O` to
open the grouped file list, move with arrows/PgUp/PgDn/Home/End, and press Enter
to open a file. Esc returns from the file to its list, then closes the list;
select another entry without changing jobs. Lowercase `o` remains quick file
cycling, and `e` switches scheduler stdout/stderr.

History also exports complete log bundles for marked or selected job IDs.
Shift-drag through History rows, then right-click a marked row and choose **Export logs** to select clipboard or directory export.
The background task combines the exact scheduler outputs with every declared source in the matching run inventory.
Keep the inventory's `job_id`, `run_id`, and `attempt` correct so that another job or execution attempt cannot supply its logs.
Register all worker logs and external locations explicitly.
A declared missing file appears in the per-job alert; an undeclared file cannot be identified as an expected missing output.
See [History log exports](guides/log-view.md#export-logs-for-history-jobs) for the destination picker, raw-file manifest, and clipboard limits.

Inside a file, arrows move the logical line cursor before `v` starts selection.
Arrows/page keys/Home/End then extend the selected source-line range across
pages. The right edge shows `›` for the cursor and orange `◆` for selected lines
(`>` and `*` in ASCII). `y` copies original UTF-8 text, preserving tabs and CRLF
endings without display headers, line numbers, wrapping, or search decorations.
Ranges above 256 KiB or 4,096 lines, and any non-UTF-8 range, run on the shared
worker. Those copies pin the selected exact bytes and save a private
`log-selected-<uuid>.log` file in the durable export directory before clipboard delivery. Invalid
UTF-8 skips text clipboard transport instead of replacing bytes.
Small valid UTF-8 ranges keep the immediate atomic `clipboard.txt` fallback.
Exports use `$XDG_DATA_HOME/raven/exports` (default `~/.local/share/raven/exports`),
including with `--no-state`; an absolute `RAVEN_EXPORT_DIR` overrides the destination.
If rotation, truncation, or
retained-tail eviction invalidates a selection, `y` asks for reselection rather
than silently copying a whole file; stale mouse targets are rejected too.

`V` then `y`, `Y`, `:copy all`, or `y` without a line selection reads the whole
exact selected log through the shared worker rather than copying the retained
tail. Raven streams its initial byte range in 1 MiB chunks to a unique private
`log-full-<uuid>.log` file (mode `0600`) in that same durable export directory. Later appends are excluded and reported. Detected rotation,
replacement, truncation, in-place change, or short reads fail without publishing
a partial copy. This does not lock the producer or establish an application
checkpoint. Treat exports as generated private evidence, outside public Git.
The export directory must be owned by the user and not writable by others;
individual exports are created exclusively and never follow planted symlinks.

The complete raw export survives clipboard limitations. OSC 52 is requested
only for complete UTF-8 text that fits its limit; no prefix is silently copied.
Terminal acceptance cannot be confirmed. Large or non-UTF-8 files keep their
exact export with skipped transport reported. A local clipboard tool can accept
larger text when available; small valid source-line selections additionally keep
the private atomic `clipboard.txt` fallback.

The list combines scheduler stdout/stderr, bounded job-ID-matching files in
those output directories, and explicit index entries. Different directories
and groups stay visible. Duplicate resolved locations produce one catalog
entry. There is no recursive filesystem scan. Catalog work is cached and
performed by the shared background worker to keep terminal input responsive.

Older Slurm accounting installations may omit stdout/stderr or WorkDir for
finished jobs. Raven uses actual retained controller paths when available and
reports missing evidence otherwise. A concrete run workdir and its index allow
project-owned logs to remain accessible even after scheduler metadata expires.
Preserve indexes and failure logs when retaining a failed, cancelled, timed-out,
or otherwise incomplete attempt.

If a job leaves the live queue before accounting confirms its outcome, Jobs'
Recents preserves it as **awaiting accounting**. Raven expedites a bounded
accounting refresh and adds its real terminal record to History when available.
Queue absence does not relabel the project attempt as `COMPLETED` and does not
finalize `run.json` or `summary.json` for your application. Keep the reporting
lifecycle truthful while reconciliation is pending. Completion motion and two
brief History pulses are optional; `animations = false` or the `reader` theme
retains static notices and the unread badge.
Requeued IDs start a new scheduler attempt: Raven discards old cached details
and requests fresh metadata while respecting source backoff. Keep your own
unique attempt directory and reports separate even when Slurm reuses its job ID.

## Live metrics: `metrics.jsonl`

Python applications may use the optional
[`import raven` client](guides/python-metrics.md) to supply the run path and
timestamp automatically:

```python
import raven
@raven.measure("loss", kind="objective")
def train_step(batch): return compute_loss(batch)
```

The client writes the same timestamped JSONL records described below. It adds
meaning and coverage in a separate optional metadata file; existing producers
and readers need no JSONL format change.

Write UTF-8 JSON Lines, one complete object followed by a newline:

```json
{"t":1791115200.25,"step":12,"phase":"fit","metrics":{"loss":0.032,"throughput_samples_per_second":142.5},"progress":{"completed":12,"total":100,"unit":"steps"}}
```

| Field | Rule |
| --- | --- |
| `t` | Finite, nonnegative Unix epoch seconds, UTC; fractions are allowed |
| `metrics` | Object of numeric observations; at most 64 names |
| Metric name | Printable, nonempty string, at most 96 characters |
| Metric value | Finite JSON number; negative values are valid |
| `step` | Optional integer from 0 through 2^63−1 |
| `phase` | Optional printable string, at most 160 characters |
| `progress.completed` | Finite number, at least zero and no greater than total |
| `progress.total` | Finite number strictly greater than zero |
| `progress.unit` | Optional printable string, at most 64 characters |

Use stable metric names, with units in names when useful: `runtime_seconds`,
`throughput_samples_per_second`, `max_task_rss_bytes`, `accuracy_fraction`.
Report a fraction as 0–1 consistently, or name an explicit percentage metric.
Put experiment labels and parameters in run metadata rather than generating a
new metric name for each sample or worker.

Omit an unavailable metric. `null`, strings such as `"4 GiB"`, Boolean values,
NaN, and Infinity are not numeric metric observations. Zero is a real observed
value. Keep each line below 65,536 bytes.

Append complete lines and flush at a useful cadence, such as every few seconds
or every meaningful iteration. Use one coordinator to write the run's stream,
or Raven's locked writer for cooperating processes. Give independent ranks and
array tasks their own streams. Each record should describe measurements from
that timestamp; avoid combining unsynchronized peaks into an allocation total.

Report increasing timestamps and steps within a phase. Reset progress when a
phase starts. Raven computes a progress rate and ETA from several observations;
the writer supplies `completed`, `total`, and `unit`. A measurement's wall-clock
timestamp and its elapsed duration are separate quantities: use a monotonic
clock to measure runtime, and an epoch clock for `t`.

Jobs reads application progress from this linked run's `paths.metrics` file.
Publish the top-level `progress` object shown above. Existing integrations can
also publish the numeric metric `progress_fraction` from 0 to 1,
`progress_pct` from 0 to 100, or `completed_steps` with `total_steps` under the
same completed/total rules. The six-cell PROG column uses the exact job and run
attempt. Another job's file cannot supply its progress. Missing application
progress uses `t` for elapsed time divided by the requested time limit, or `--`
when that limit is unavailable. The `t` value is not completion or ETA.

Visible Main jobs refresh from discovered standard project inventories during
runtime. The existing background reader reads at most four linked reports per
cycle and rotates through the visible jobs. Its base interval is eight seconds,
subject to the global update speed ratio and reader minimum intervals. Each report read
uses a confined tail of at most 64 KiB. Inventory lookup examines at most 256
runs, and the scalar cache holds at most 128 entries. Ambiguous run attempts and
stale results cannot update another job. Explicit Quick Advisor and project
tasks take priority. See [Adaptive workspaces](guides/adaptive-workspaces.md)
for the display and sorting controls.

If Raven is installed in the application's Python environment:

```python
from raven.metrics import write_metric

write_metric("metrics.jsonl", {"loss": 0.032}, step=12, phase="fit",
             completed=12, total=100, unit="steps")
```

Other languages can append the same JSONL. The template's `reporting.py` writes
the format using only Python's standard library.

### Optional metric metadata: `metrics.meta.json`

The `raven.metrics-meta/v1` sidecar describes a metric stream. It is never a
header record in `metrics.jsonl`; every line in that stream still requires `t`.
For a separately named stream, use the same stem, such as
`metrics.rank-1.jsonl` and `metrics.rank-1.meta.json`.

```json
{"schema":"raven.metrics-meta/v1","client":{"api_version":1},"kinds":{"residual":"residual","energy":"conserved"},"coalesced":{"residual":12},"units":{"energy":"J"},"rank_policy":{"mode":"coordinator","rank":0}}
```

| Field | Meaning |
| --- | --- |
| `client.api_version` | Client API version, currently `1`. |
| `kinds` | Stable metric-name to kind mapping, at most 64 names. |
| `coalesced` | Counts of intermediate observations omitted by coalescing. |
| `units` | Optional unit labels keyed by metric name. |
| `rank_policy` | Declared coordinator or separate-rank coverage; not proof of a global reduction. |

| Kind | Meaning and default presentation |
| --- | --- |
| `objective` | A minimized or maximized objective; linear values and trend. |
| `residual` | A convergence residual; logarithmic positive values. |
| `throughput` | Work per unit time; linear values and rolling median. |
| `progress` | Completed work against a positive total; progress and conditional ETA. |
| `conserved` | A nominal invariant; drift relative to the first reported value. |
| `error` | Distance from a reference; logarithmic positive values. |
| `gauge` | Another numeric measurement; linear values. |

Kinds affect defaults, not the stored numbers or scientific acceptance.
Nonpositive log samples remain available as raw observations. The versioned
[schema](schemas/metrics-meta.v1.schema.json) defines exact fields and bounds;
invalid metadata does not erase valid JSONL samples. Existing synchronous
`write_metric` callers can continue without a sidecar.

## Run inventory: `run.json`

The inventory's minimum fields are:

```json
{
  "schema": "raven.run/v1",
  "run_id": "fit-20261004T120000Z-a1",
  "experiment_id": "regression-v3",
  "attempt": 1,
  "state": "RUNNING"
}
```

Add declared relative paths, actual job identity, stable parameters, code/input
identity, and request metadata as needed by your application. The schema gives
the supported vocabulary. Create the directory exclusively before work starts;
an existing directory belongs to an earlier attempt.

Write a new complete JSON document to a temporary file in the same directory,
then atomically replace the inventory. Use the same publication pattern for
the final summary and aggregate report. Readers should see a complete previous
or next version rather than a half-written JSON document.

The application can report its observed success or failure. A lost process may
leave a run marked `RUNNING`; a downstream reconciliation step should use actual
scheduler evidence to finalize it. A termination signal alone does not establish
an OOM or a timeout. Preserve interrupted measurements and record the verified
terminal state when it becomes known.

## Final report: `summary.json`

Use flat allocation and measurement fields so the same record can feed the
existing prediction and scaling readers. A completed Slurm-backed report might
look like this:

```json
{
  "schema": "raven.summary/v1",
  "id": "fit-20261004T120000Z-a1",
  "job_id": "12345",
  "name": "my-project/regression-v3",
  "state": "COMPLETED",
  "partition": "main",
  "account": "research",
  "qos": "normal",
  "cpus": 4,
  "nodes": 1,
  "gpus": 0,
  "gpu_type": "",
  "mem_bytes": 8589934592,
  "time_seconds": 3600,
  "runtime_seconds": 91.2,
  "cpu_seconds": 310.4,
  "memory_bytes": 2684354560,
  "memory_scope": "max_task_rss",
  "script_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "input_size": 100000,
  "parameters": {"dataset": "v3", "iterations": 100, "code_revision": "release-17"},
  "start": 1791115200,
  "end": 1791115291.2,
  "results": {"loss": 0.018, "accuracy_fraction": 0.94}
}
```

These values illustrate the shape; the producer must report its actual
measurements and fingerprint. `schema`, `id`, `name`, and `state` form the minimum
summary. Optional unknown measurements can be omitted or `null`. The minimum
summary provides an inventory even when there is insufficient evidence for an
analysis.

| Field | Meaning |
| --- | --- |
| `cpus`, `nodes`, `gpus` | Known total allocation counts; `gpus: 0` means a known CPU-only allocation |
| `mem_bytes` | Total allocation memory request, in bytes |
| `time_seconds` | Requested walltime, in seconds |
| `runtime_seconds` | Measured execution duration; exclude time waiting for allocation |
| `cpu_seconds` | Observed CPU time summed across the declared scope |
| `memory_bytes` | Observed memory measurement, paired with `memory_scope` |
| `script_sha256` | Actual 64-hex fingerprint of the declared executable/batch entry point |
| `input_size` | Explicit workload-size measure with a stable definition |
| `parameters` | Small stable JSON object describing comparable scientific work |
| `results` | Project-specific final scientific results |
| `start`, `end`, `submit` | Optional epoch timestamps for the corresponding actual events |

The observed memory scopes are `job_peak`, `per_node_peak`, and `max_task_rss`.
They describe different measurements. Ordinary `sacct` MaxRSS belongs to
`max_task_rss`; it is not total job memory. Record a job peak only when you
actually measured a simultaneous allocation-wide peak.

Slurm's `--mem` is normally a per-node request. A `--mem-per-cpu` request needs
the known allocation CPU count. Normalize these into a total `mem_bytes` only
when the counts and request semantics are known. Submission recipes retain
Slurm's own per-task/per-node option vocabulary. Do not put a per-task CPU count
into the total `cpus` field.

Use omission or `null` for an unknown allocation. Application worker count and
`os.cpu_count()` do not prove allocated CPU count. An unknown GPU request is
different from a verified zero-GPU allocation; an empty GPU type means a known
generic GPU request where that distinction matters.

### Preserve comparability and failure evidence

Resource predictions compare `name`, `partition`, total CPU/node/GPU counts,
and any supplied account, QoS, request, script, input-size, or parameter
identities. Keep code and input identities in small, stable `parameters` fields
as well as their detailed provenance records. A batch-script hash alone may not
identify the application it launches or the dataset it reads.

Use explicit data hashes or versions, a code revision/fingerprint, and the
parameters that change scientific work. Keep parameters shallow: the predictor
accepts depth at most four and a bounded 128-value tree. Record a repetition's
identity separately. If different random seeds belong to the same experimental
protocol, document that protocol; if the seed defines different work, include
it in the cohort parameters.

Preserve `FAILED`, `TIMEOUT`, `OUT_OF_MEMORY`, `CANCELLED`, and other observed
terminal states. A timed-out duration and OOM memory observation are incomplete
measurements. Include those reports in aggregates instead of discarding them
or relabeling them as completed runs. Unavailable measurements remain unknown.
Raven can then withhold unsupported intervals and scaling claims.

For controlled scaling, add explicit `workers`, `problem_size`, and a positive
`repeat` number. Use three or more comparable repeats per worker configuration
to obtain observed repeat spread. Keep fixed work for strong scaling or a fixed
work-per-worker ratio for weak scaling. Retain the same code and parameter
identity across the comparison. These are empirical measurements, not assumed
speedups from requesting more CPUs.

For resource comparisons across clusters, include the relevant platform or
hardware identity in cohort parameters and select comparable runs explicitly.
Keep a worker's CPU time separate from a measured sum across an allocation;
record its source and scope in report metadata.

## Output declaration: `.raven/contracts/outputs.v1.json`

Example contract:

```json
{
  "version": 1,
  "outputs": [
    {"path": "run.json", "required": true, "format": "json", "max_bytes": 65536,
     "required_keys": ["schema", "run_id", "experiment_id", "attempt", "state"]},
    {"path": "logs.json", "required": true, "format": "json", "max_bytes": 262144,
     "required_keys": ["schema", "logs"]},
    {"path": "summary.json", "required": true, "format": "json", "max_bytes": 262144,
     "required_keys": ["schema", "id", "name", "state"]},
    {"path": "metrics.jsonl", "required": true, "format": "text", "min_bytes": 1,
     "max_bytes": 4194304},
    {"path": "outputs/results.csv", "required": false, "format": "csv",
     "columns": ["step", "value"], "min_rows": 1, "max_bytes": 4194304}
  ]
}
```

Declare your project's actual result names and required columns. Mark a result
required when successful completion depends on it. Use bounded byte limits and
appropriate row/key/hash checks. Raven validates JSON top-level keys and CSV
structure; it does not execute custom validation commands or recursively parse
a scientific model.

An artifact check passing establishes the declared file/structure checks.
Read the run's state and scientific results to assess its outcome. During a
running job, a required final file may legitimately be absent. Full JSON Schema
validation can be added to the producing project's own CI; Raven's artifact
`required_keys` check is intentionally narrower.

## Aggregate report: `reports/planning.json`

Export explicitly selected run summaries to the native planning format:

```json
{
  "version": 1,
  "kind": "raven.planning",
  "coverage": 0.8,
  "query": {"name": "my-project/regression-v3", "partition": "main",
            "cpus": 4, "nodes": 1, "gpus": 0},
  "history": [],
  "scaling": []
}
```

`history` contains the selected summary objects, retaining successes and
failures. `query` describes the particular comparable workload/allocation you
want to predict. Include the known scientific identity fields used by those
summaries and an explicit memory scope when predicting a memory measurement.
`scaling` contains the subset with declared scaling metadata, including failed
attempts. A single completed run is useful evidence but cannot establish a
calibrated prediction interval or a repeat spread.

The template collector accepts explicit run directories and an optional
reference run, validates its inputs, and publishes a bounded aggregate. It does
not recursively search storage or infer missing metadata. Keep normal planning
bundles at most 1 MiB and at most 10,000 input records. Archive older reports
and select a coherent analysis cohort when the dataset grows. Scaling-only
inputs can use Raven's 8 MiB reader; shared bundles should fit the smaller limit.

Optional `candidates`, `jobs`, `observations`, `queue_history`, `workflow`, and
captured blocker metadata follow the native formats in the
[planning guide](WAVE_TWO.md). Publish captured scheduler evidence with its
actual timestamp, connection identity, and known resource fields. An explicit
`jobs` snapshot must carry its own evidence; Raven will not fill it from an
unrelated live snapshot. Let Raven collect connection-scoped pre-start forecast
observations for live jobs rather than generating queue outcomes from an
application's runtime.

Keep forecast observations separate from experiment measurements. Never derive
a queue start from CPU utilization, a synthetic date, or a completed-only
runtime estimate. Use actual submission/start events and issued predictions.

## Open and inspect a project

For the integrated workbench, launch locally on the Slurm host, then enter these
commands after `:`:

```text
project /absolute/project
run select my-run
workspace experiment
outputs
```

The project picker reads only direct attempt directories beneath the chosen
`runs/`: at most 256 attempts, 4,096 entries, 64 KiB per inventory, and 8 MiB
combined inventory bytes. It reports invalid, omitted, or unsafe entries and
revalidates an inventory on selection. `/` filters, Enter selects, `r` refreshes,
and `!` opens notices. `:runs` revisits the picker, and `:run clear` restores the
original report bindings. A missing or omitted `job_id` remains a local project
run, without attaching reports to an unrelated scheduler job.

Selection binds declared metrics/log paths relative to the selected run, the
standard project output contract, and a verified passport when only one actual
record is available. For multiple passports, use
`:run passport passports/actual-record.json`. Artifact/inventory paths stay
confined, exact, and relative without symlink traversal. Explicit sibling or
absolute files inside `logs.json` remain valid read-only log declarations and
are identified as external; copying a run does not relocate those absolute paths.

`:outputs` browses only declared contract results and previews stable text,
JSON, or CSV prefixes (64 KiB, up to 256 lines), with missing/invalid states and
visible limits. Evidence consumes the same selected log catalog, with source
coverage and cited-file drilldown. See the [workbench guide](WORKBENCH.md) for
navigation, metrics, and bounded inspection details.

For the explicit configuration workflow, from the project root after creating a run:

```bash
RUN_DIR="$PWD/runs/my-run"
raven --config .raven/config.json --workdir "$RUN_DIR" \
  --tab research --research-view experiment
raven run validate .raven/contracts/outputs.v1.json "$RUN_DIR"
```

On a development machine without Slurm, add `--fake --no-state --no-plugins`
to the dashboard command. Its scheduler data is simulated; the attached project
files are actual reports. On CARC, use the site's Python/Slurm environment and
your account, partition, and polling preferences from the [runbook](runbook.md).

After exporting a planning bundle:

```bash
raven run predict --file reports/planning.json
raven run scaling analyze reports/planning.json --mode strong --baseline 1
raven --config .raven/config.json --workdir "$RUN_DIR" \
  --tab research --research-view predict
```

The two `raven run` analyses above are offline. Missing allocation or repetition
metadata produces an explicit insufficient/partial result. Supply real metadata
when it is known; keep local development reports honest when it is unavailable.

For provenance, use Raven's API or command to create a genuine passport:

```bash
raven run passport capture jobs/run.sbatch --workdir "$PWD" \
  --input experiment.py --input reporting.py \
  --output-dir "$RUN_DIR/passports"
```

Pass the printed file path to `--passport` or the Passport command. Keep
submission receipts and actual returned job IDs in separate metadata; saved
passports remain immutable. A `run.json` inventory or hand-written hash record
does not have the checksum/identity contract of a Raven passport.

Copy passports intact when moving a run. Their captured host and absolute paths
describe the original observation. Use the relocated run's relative report
paths for viewing; preserve the passport's contents and checksum.

Batch stdout/stderr directories must exist before submission because Slurm
opens them before application startup. Use precreated run log directories when
the run ID is known before submission, or a precreated project `logs/` directory
with Slurm job/task tokens. Raven follows the selected job's actual scheduler
`StdOut`/`StdErr` paths and the explicitly bound `logs.json` index. Declare
application logs in that index for browsing, and optionally in the artifact
contract as exact text files for checks. Preserve failure output in either case.

## Adoption checklist for every new project

1. Copy the integration template and keep its source/parameter layout suited to
   your application.
2. Create a unique run directory and inventory before execution; preserve old
   attempts.
3. Emit complete, finite JSONL metric records with stable names and units.
4. Publish actual final measurements/results atomically and retain failures.
5. Declare exact output files and meaningful bounds/checks in the contract.
   Index application, worker, and external logs in `logs.json` with stable groups
   and actual job identity, then verify browsing a retained failed attempt.
6. Record real allocation counts, request semantics, memory scope, and
   scientifically comparable code/input/parameter identities when known.
7. Export selected run summaries into a bounded planning bundle for analysis.
8. Verify an actual metric stream and contract with Raven before submitting
   a production job; keep scripts, local report production, and schemas in CI.

Version new reporting contracts when their meaning changes. Preserve the
version-1 native field names and units when extending reports; put additional
scientific results under `results` and keep raw large artifacts in `outputs/`.
The producing project owns measurement definitions and scientific validation.
Raven presents the declared evidence and its limits.
