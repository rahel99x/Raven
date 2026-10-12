# Experiment and research workflows

Raven's Research workspace connects application progress, run evidence, batch
submission, arrays, failure investigation, and declared outputs. Everything runs
in a terminal. The scientific result comes from your application; Raven reads
job-bound reports and scheduler evidence rather than guessing convergence from
CPU or GPU utilization.

Use Light to inspect bounded job and run evidence. Request Heavy in the same
interface before using supported scientific-analysis adapters. The shell
examples below retain native compatibility syntax; bare `raven` starts Light,
so computational commands can return a deferred result. Provenance capture
requires a verified job-local target in managed Raven. See the
[execution contracts](raven/execution-contracts.md) for supported and deferred
paths. Standalone Python APIs retain their behavior outside a managed session.

For a common layout and reporting contract across your own projects, use the
[project reporting standard](PROJECT_STANDARD.md), its
[copyable template](../examples/project-template/README.md), and
[machine-readable schemas](schemas/README.md).

| View | What it answers | Source |
| --- | --- | --- |
| Experiment | Is the application progressing, and how are its metrics changing? | The selected run's JSONL metrics |
| Passport | Which script, code revision, inputs, and requests describe this run? | Verified immutable native capture |
| Submit | What does the preflight show, and what issues need attention? | Current prepared plan or read-only captured native report |
| Arrays | Which observed tasks succeeded, failed, or remain active? | Current queue and accounting |
| Evidence | Which observations support a possible failure explanation? | Job records and selected log excerpts |
| Artifacts | Do the declared outputs meet their checks? | An explicit JSON contract |
| Resources | Which measured resource requests fit comparable work? | Selected summaries and an explicit prediction query |
| Forecast | What evidence supports a queued job's possible start? | Actual scheduler predictions and observed outcomes |
| Blockers | Which observed scheduler constraints explain pending work? | Matching job, controller, node, partition, and health facts |
| Tradeoffs | How do comparable resource choices affect runtime and wait? | Explicit candidates, comparable history, and actual queue observations |
| Scaling | How does controlled work change across worker configurations? | Measured repeat records or a reviewable scaling recipe |
| Workflow | How do real dependencies and timing evidence shape a pipeline? | Native dependency recipe and measured timing intervals |

Start at a particular view with `--tab research --research-view VIEW`, where
`VIEW` is `experiment`, `passport`, `submit`, `arrays`, `evidence`, `artifacts`,
`predict`, `forecast`, `blockers`, `tradeoffs`, `scaling`, or `workflow`.
The regular Jobs and History views remain the place to select a job.

## Attach every Research view to its job

Place each attempt under `PROJECT/runs/<run_id>` and publish its actual Slurm
`job_id` in `run.json`. Make the job's scheduler WorkDir the project root or a
directory beneath that standard attempt. A known project root registered with
`:project PROJECT` also enables bounded discovery. Raven attaches an
unambiguous exact match and refreshes declared sources in the background.
Array task IDs and retry attempts remain distinct. Missing identity, ambiguous
attempts, or incomplete discovery coverage require explicit selection.

The [per-view source table](PROJECT_STANDARD.md#files-for-every-research-view)
specifies concrete file locations and fields for all twelve views. Publish
metrics in `runs/<run_id>/metrics.jsonl`, logs in the declared `logs.json`, and
planning sources under that attempt's `reports/`. The copied reporter publishes
and binds native sources without importing Raven:

```bash
python3 reporting.py report workflow .raven/definitions/workflow.json --run runs/my-run
python3 reporting.py export runs/prior-run runs/my-run --reference runs/my-run \
  --output runs/my-run/reports/planning.json
```

Selecting another job updates the report attachment without opening another
page. Appends and atomic replacements appear on subsequent polling cycles.
Discovery does not generate metrics, finalize outcomes, or execute a captured
submission plan. Scientific fits and failure explanations retain their evidence
limits. Use the [planning guide](WAVE_TWO.md) for the six analysis views.

## Try the complete workflow without a cluster

From an installed checkout, create a fresh output location and run the included
regression experiment:

```bash
demo_dir=$(mktemp -d /tmp/raven-research.XXXXXX)
.venv/bin/python examples/research/experiment.py \
  --output "$demo_dir/run" --steps 120 --delay 0.1 &
experiment_pid=$!
scripts/raven --fake --tab research --research-view experiment \
  --metrics-file "$demo_dir/run/metrics.jsonl" --workdir "$demo_dir/run"
wait "$experiment_pid"
```

Quit the dashboard with `q`. The output directory contains the generated dataset,
the loss/RMSE curve, a result summary, and the append-only metric stream. The
experiment is deterministic for the same seed and parameters, and computes
full-batch regression gradients using sufficient statistics in
`O(samples + steps)` work. `--delay` only makes its progress easier to watch.
It refuses an existing output directory, protecting previous runs.

The following native commands express artifact validation, provenance capture,
and submission preparation. Managed Light can defer validation, and capture
requires the verified job-local target described above. File-view and preparation
commands do not themselves authorize submission:

```bash
scripts/raven run validate examples/research/contract.json "$demo_dir/run"
scripts/raven --fake --tab research --research-view artifacts \
  --contract examples/research/contract.json --workdir "$demo_dir/run" --once
scripts/raven run passport capture examples/research/sample.sbatch \
  --workdir "$PWD" --output-dir "$demo_dir/passports"
scripts/raven --fake run prepare examples/research/sample.sbatch --workdir "$PWD"
```

`--fake` explicitly identifies simulated scheduler data. Attached metric files
are still your actual files. The simulated application curves used when no file
is attached are demonstration data, not measurements of a CARC job. Passports,
contracts, and native local preparation do not inherently require a Slurm
installation. Managed execution admission remains a separate requirement.

## Application metrics

Report only the values you want Raven to inspect. A stream is UTF-8 JSONL: one
object per newline, with a finite epoch timestamp `t`, a `metrics` object, and
optional `step`, `phase`, and `progress`:

```json
{"t":1700000000,"step":1,"phase":"fit","metrics":{"loss":0.4,"rmse":0.632456},"progress":{"completed":1,"total":100,"unit":"steps"}}
```

Numeric values must be finite; progress must satisfy
`0 <= completed <= total` with a positive total. Metric names are printable
strings. The reader rejects malformed records atomically, preserves incomplete
lines until their newline arrives, and displays bounded parsing errors. It
follows appended bytes and handles truncation/rotation rather than rereading an
ever-growing file on every terminal frame.

Python applications can use the optional reporter:

```python
from raven.metrics import MetricReader, write_metric

write_metric("metrics.jsonl", {"loss": 0.4}, step=1, phase="fit",
             completed=1, total=100, unit="steps")
snapshot = MetricReader().read("metrics.jsonl")
print(snapshot["latest"], snapshot["progress"])
```

Other applications can write the same format, or append through the CLI:

```bash
raven run metric metrics.jsonl --value loss=0.4 --step 1 \
  --completed 1 --total 100 --unit steps
raven --metrics-file metrics.jsonl --workdir "$PWD" \
  --tab research --research-view experiment
```

In the dashboard, `:metrics FILE` attaches a stream. `{job_id}` is the supported
token in a configured metric path, for example `runs/{job_id}/metrics.jsonl`.
Create the parent directory before reporting. The writer creates private files,
serializes cooperating writers with a POSIX advisory lock, and appends without
truncation. Separate streams are preferable for independent array tasks.

By default the reader retains up to 600 points for each of at most 64 metric
names, reads at most 1 MiB at a time, and keeps at most 32 streams. Limits and
discarded data are reported. ETA comes from several reported progress samples,
resets when progress restarts or changes phases, and is an estimate of
application progress rather than a scheduler finish guarantee.

## Reproducible run passports

```bash
raven run passport capture examples/research/sample.sbatch \
  --workdir "$PWD" --output-dir ./passports
raven run passport show /absolute/path/to/printed-passport.json
raven run passport compare /absolute/path/to/first-passport.json \
  /absolute/path/to/second-passport.json
```

Use the content-addressed file path printed by capture. Passports contain a
script SHA256, an observed Git revision and tracked dirty status, selected
resource requests and parameters, and only explicitly declared input and
environment metadata. Unknown information remains `null`. They do not capture
Git patches, remote URLs, arbitrary directory contents, or the entire process
environment. The actual batch script is never executed by capture.

The Python API supports more explicit declarations:

```python
from raven.provenance import capture, diff, load, save

passport = capture(
    "/absolute/path/to/workdir", script="run.sbatch",
    resources={"cpus_per_task": 8, "mem": "32G"},
    parameters={"seed": 7, "samples": 512},
    inputs=["dataset.csv", {"path": "experiment.py", "hash": True,
                           "max_bytes": 1048576}],
    environment_names=["LOADEDMODULES", "OMP_NUM_THREADS",
                       "APPTAINER_CONTAINER"],
)
path = save(passport, "/absolute/path/to/passports")
assert diff(passport, load(path)) == []
```

Input strings record size and modification time, not a content hash. Hashing
requires explicit opt-in and is bounded to 64 MiB across inputs; scripts are
bounded to 8 MiB. Capture accepts explicit paths outside the working directory,
resolves symlinks, and records both the declared and resolved paths. Missing,
nonregular, oversized, or concurrently changed files prevent capture. Explicitly
declare application source and data files when the batch script alone does not
identify them; untracked files do not make the Git snapshot dirty.

Environment selection is an allowlist, with secret-like names rejected. Module
and container metadata are captured only when their corresponding variables are
selected. Parameters and resource objects also reject secret-like field names.
Do not put credential values under other field names. A passport describes
observed local metadata, not an exhaustive or verified compute-node environment.

Records are immutable through Raven's API, published atomically with mode
`0600`, and limited to 1 MiB. Equivalent evidence shares an ID despite different
capture timestamps; saving it again reuses the original record. A separate
checksum covers the entire record, including its timestamp, and is verified on
load. These hashes detect corruption or modification; they are not signatures
proving who created a record. Comparisons report JSON Pointer paths and distinguish
missing fields from explicit `null` and different JSON types.

## Submission workbench

Prepare and review a batch file before submitting:

```bash
raven run prepare examples/research/sample.sbatch --workdir "$PWD" \
  --account "$CARC_ACCOUNT"
```

The plan includes the exact `sbatch` argv and working directory, the script
fingerprint, parsed resource requests, and issues. Command-line resource options
override directives for the same option; conflicting memory or GPU request
styles still need correction. Only directives before the first executable
command are active. Slurm does not expand shell variables inside `#SBATCH`
directives, so account/partition choices should be passed explicitly or written
as concrete values. Supported options are deliberately bounded; this workbench
does not claim to parse arbitrary shell behavior or every Slurm extension.

On a CARC login node, set the checkout path for the supplied example and confirm
a reviewed submission:

```bash
export RAVEN_ROOT="$PWD"
raven run submit examples/research/sample.sbatch --workdir "$PWD" \
  --account "$CARC_ACCOUNT" --yes
```

Choose a partition or other resources appropriate to your site if necessary.
The example uses a single CPU, 256 MiB, and five minutes; its output directory is
`runs/JOBID` below the submitted working directory. It executes the checkout's
own `.venv/bin/python` and imports Raven's reporter. Array submissions use
`runs/JOBID_TASKID` and a distinct seed per task.

Interactive `:prepare SCRIPT --workdir DIR [flags]` reviews a plan in the Submit
view; `:submit` starts its confirmation flow. Preparation never submits a job.
Submitting rechecks the script and plan, captures a passport before contacting
Slurm, and links the returned job ID without changing that immutable passport.
Confirmed CLI and interactive submissions save private passports under the batch
workdir's `.raven/passports` by default. Use `--passport-dir DIR` on `submit` to
choose another directory. Passports are explicit run artifacts, independent of
the dashboard's `--no-state` option. Heavy palette preparation, capture and
validation run on the single Research worker; results appear on the next UI tick.
An accepted response without an unambiguous ID, or a transport timeout, requires
queue inspection before retrying; Raven does not automatically repeat it.

Programmatic callers use the same separation:

```python
from raven.submission import prepare, preview, submit

plan = prepare("run.sbatch", workdir="/absolute/workdir",
               overrides=["--cpus-per-task=8", "--mem=32G"],
               parameters={"seed": 7}, inputs=["dataset.csv"],
               outputs=["result.json"])
# `slurm` is an existing raven.slurm.Slurm instance for the selected cluster.
# preview(plan, slurm) runs the scheduler's test-only probe when requested.
# Only call submit(plan, slurm, passport_directory="/absolute/passports")
# after the user has reviewed and explicitly confirmed this exact plan.
```

Declared inputs and outputs are explicit paths. The workbench detects obvious
collisions and missing parents; it cannot infer files opened by arbitrary
application code. Site account eligibility and scheduler behavior ultimately
come from Slurm, not an offline plan.

CLI declarations use `--declared-input FILE` and `--declared-output FILE`, each
repeatable. These describe experiment files; ordinary Slurm `--input FILE` and
`--output PATTERN` retain their stdin and log-file meanings. Passport capture's
`--input FILE` declares a fingerprinted input, since capture is not an `sbatch`
command.

## Array operations

```bash
raven --tab research --research-view arrays
```

The Arrays view merges current and historical task records, gives individual
records precedence over compressed ranges, and displays bounded state mosaics
and runtime summaries. Compressed ranges are counted arithmetically instead of
expanding millions of indices into Python objects. An observed subset is not
reported as the complete array; missing size information stays unknown.

Click a cohort's right-pointing `▸` chevron to select it and open its task page.
Click its down-pointing `▾` chevron to close that page. ASCII mode uses `>` and `v` respectively.
The cohort's summary, state counts, and mosaic remain visible.
This task-page state is separate from the shared automatic job-group folds.
Jobs and History Details retain their own Arrays task-page state.

In the full Research Arrays view, Enter toggles the selected task page.
Page Up and Page Down move through its tasks in pages of 24 while it is open.
Inside Jobs or History Details, use the cohort arrows and the pane's existing scroll controls.
Use `:array open ARRAYID [CLUSTER]` or `:array close ARRAYID [CLUSTER]` for an explicit action on a published cohort.
Supply its cluster when the same array ID occurs in more than one cluster.
These actions read the published document; they do not request a retry or submit a job.
Repeated open requests retain the current task offset.
A refreshed cohort order retains the exact cluster and array ID; removal closes its task page.

Select an array and prepare a failed-task retry:

```text
:array retry ARRAYID /absolute/path/to/run.sbatch --workdir /absolute/workdir
```

This produces a review plan for observed failed tasks. It does not submit it.
Review the generated indices, resources, and output paths, then use `:submit`
to confirm. A retry is a new submission, so check how the application's output
layout distinguishes original attempts from retries.

The API also supports a bounded subset and concurrency limit:

```python
from raven.arrays import summarize, tasks, select_failed, retry_plan

groups = summarize(current_job_records, accounting_records)
group = groups[0]
page = tasks(group, offset=0, limit=100)
failed_spec = select_failed(group)
plan = retry_plan(group, "/absolute/run.sbatch", limit=4,
                  workdir="/absolute/workdir")
```

Only observed failures are eligible. Ambiguous, malformed, or excessively
complex range evidence is reported rather than silently converted into a broad
retry. Bounded grouping and paging limit UI work as campaigns grow.

Scientific array manifests add labels, parameters, and declared paths to actual
task indices. Use `:arraymap` to inspect the attachment. See
[Scientific array manifests](guides/phase-one.md#attach-a-scientific-array-manifest)
for the schema, controls, and mapping limits. A manifest does not change
scheduler identity, infer task order, or submit a retry.

Submission review also offers an explicit local shell check. See
[Check a batch script](guides/phase-one.md#check-a-batch-script) for syntax
checks, optional ShellCheck, and handling of remote paths.

## Evidence-based failure investigation

Select a job from Jobs or History, then open Evidence, or use
`:investigate JOBID`. Raven ranks candidate explanations from scheduler state,
exit code/signal, available resource observations, selected stdout/stderr tails,
and job-scoped events. Each hypothesis links to supporting evidence,
contradictory observations, and useful next checks. A recovered warning in a
completed job does not change its recorded scheduler state.

The pure analysis API does not open files or run commands:

```python
from raven.investigate import investigate

case = investigate(
    {"id": "12345", "state": "OUT_OF_MEMORY", "exitcode": "0:125"},
    stderr={"text": "slurmstepd: oom-kill event detected\n",
            "path": "/absolute/job.err", "first_line": 42},
)
print(case["summary"])
for candidate in case["hypotheses"]:
    print(candidate["name"], candidate["support"], candidate["next_checks"])
```

Log excerpts are bounded to 128 KiB and 1,200 lines per supplied source.
Original line numbers are shown when supplied; otherwise locations explicitly
refer to the tail excerpt. Missing or unreadable logs remain limitations, and
candidate explanations are not proven causes. Terminal escape sequences are
removed from displayed evidence. Investigation never automatically cancels,
changes resources, or reruns a job.

## Output contracts

The example contract names four exact files. A smaller contract might require
a CSV with specified columns and at least ten data rows, plus a JSON summary:

```json
{
  "version": 1,
  "outputs": [
    {"path": "results.csv", "format": "csv", "columns": ["id", "score"], "min_rows": 10},
    {"path": "summary.json", "format": "json", "required_keys": ["count", "mean"], "min_bytes": 2}
  ]
}
```

```bash
raven run validate contract.json /absolute/output-root
raven --contract contract.json --workdir /absolute/output-root \
  --tab research --research-view artifacts
```

Interactive `:artifacts CONTRACT ROOT` attaches the same checks. The API is:

```python
from raven.artifacts import load_contract, validate_contract

result = validate_contract(load_contract("contract.json"), "/absolute/output-root")
print(result["status"], result["summary"])
```

Contract version 1 accepts size bounds, strict JSON and top-level required keys,
CSV required columns and row bounds, text decoding, optional SHA256, and optional
files (`required: false`). CSV row counts exclude the header and blank rows.
Checks establish only the properties declared in the contract; successful
parsing does not prove scientific correctness or completion of all intended
steps. Require an exact row count or application completion summary when that
distinction matters.

Paths are exact relative paths under the output root. Absolute paths, traversal,
globs, duplicates, and symlinks in output paths are refused. No recursive scan,
shell command, or validator plugin runs. The default inspection budget is
8 MiB shared across at most 256 entries; exhausted budgets and concurrent file
changes yield incomplete checks rather than a false success. Contracts are
bounded to 256 KiB. The result distinguishes `valid`, `invalid`, `incomplete`,
and `error` and records per-check observations independently of Slurm's state.

## CARC and remote operation

Install and run Raven on a CARC login node for local submission and confined
output validation. Keep application work inside allocations; the example's
offline experiment is deliberately small. The CARC profile slows scheduler
polling and avoids enabling GPU job steps or scheduling probes automatically.

`--host` can monitor a selected remote cluster and read explicit metric/log
paths through SSH; these paths belong to that host. A local passport is still a
local capture, and its optional `host` field labels metadata rather than
performing remote collection. Submission plans require local files on the
cluster where they will be submitted. The standard SSH reader now supports
confined project discovery, declared output checks, and artifact browsing through
an isolated Python helper on that host. See [Bash Research automation](guides/research-automation.md#automatic-reads-and-ssh).
Other remote adapters must prove regular-file type and confinement before they
can certify a contract. No feature turns an SSH
connection into permission to recursively inspect files or execute application
code outside a reviewed submission.
