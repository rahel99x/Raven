# Recovery and campaign operations

Use these operations to restart from a declared checkpoint, run discrete
parameter searches, pack short tasks, manage a Dask pool, or prepare a
heterogeneous allocation. Each operation separates preparation from submission.

1. Open **File → Research and cluster operations**.
2. Select a **Campaigns** operation and enter its manifest path.
3. Select the required action, then select **Inspect / prepare**.
4. Read the report and select **Review action** when a plan is available.
5. Check the exact inputs and allocation, then select **Confirm apply**.

Use `:ops checkpoint`, `:ops search`, `:ops packing`, `:ops dask`, or
`:ops heterogeneous` to open a form directly. For example,
`:ops run search manifest=/project/search.json action=propose count=2` inspects
two proposals. The command does not bypass the review and confirmation controls.

See the [example manifests](../examples/campaigns/README.md) and the
[JSON Schema](../schemas/campaign-operations.schema.json).

| Operation | Inspect or plan | Apply |
| --- | --- | --- |
| Checkpoint recovery | Verify the checkpoint, application contract, and restart script | Submit the exact restart script |
| Adaptive parameter search | Show distinct trial proposals and the remaining budget | Submit only the reviewed trials |
| Short-task packing | Show task resources and the complete runner command | Submit one allocation and its logical tasks |
| Elastic Dask pool | Show the controller and connected and queued jobs | Start, scale, or stop the owned controller |
| Heterogeneous allocation | Show every component, the `sbatch` command, and the `srun` command | Submit the reviewed coupled allocation |

These operations run on the host that runs Raven. Use Raven on a cluster login
node, or on the local Slurm workstation. An SSH view or a recorded session cannot
submit local substitute files. The background worker does file checks and
scheduler queries. Rendering does not run these checks.

## Common rules

Use UTF-8 JSON. Relative input paths are relative to the manifest directory.
JSON inputs have a 4 MiB limit. Duplicate keys and non-finite numbers are errors.
Campaign shell scripts have a 256 KiB limit.
Use a shared filesystem for generated scripts and files that compute nodes need.
Raven stores generated artifacts and receipts under its state directory, grouped
by connection and operation. The report includes each exact path.

An action is bound to its connection and evidence. A review expires after five
minutes. Raven verifies the manifest and script again before Apply. If a source
changes, prepare a new review. A changed profile or reused job attempt also
requires a new review.

Raven writes a durable launch intent **before** it submits work. A timeout or an
unreadable scheduler receipt is an **unknown** result. Raven does not repeat that
launch automatically. Inspect the queue and the receipt before you decide how to
continue. Each accepted submission records its exact job ID. Search trials also
retain their parameters and submission passport.

For checkpoint recovery, packed runs, and heterogeneous allocations, an optional
manifest `id` names an intentional run. Keep the same ID while inspecting and
applying that run. Choose a new ID only when you intend another submission after
you have reconciled the previous receipt. An omitted ID uses a deterministic
identity and therefore prevents repeated identical submissions.

## Checkpoint recovery

Raven does not infer restart flags, repair an incomplete checkpoint, or claim that
a filename proves compatibility. The application must publish a complete
checkpoint and this explicit contract:

```json
{
  "schema": "raven.checkpoint-restart/v1",
  "id": "restart-001",
  "script": "restart.sbatch",
  "script_sha256": "REPLACE_WITH_THE_RESTART_SCRIPT_SHA256",
  "workdir": ".",
  "checkpoint": {
    "path": "checkpoints/step-1000.bin",
    "sha256": "REPLACE_WITH_THE_CHECKPOINT_SHA256",
    "complete": true
  },
  "compatibility": {
    "expected": {"application": "solver-2.1", "format": "checkpoint-v3", "runtime": "gcc14-openmpi5"},
    "actual": {"application": "solver-2.1", "format": "checkpoint-v3", "runtime": "gcc14-openmpi5"}
  },
  "source_job": {"id": "1234_7", "cluster": "my-cluster", "start": "2026-01-02T10:00:00"}
}
```

Calculate digests with `sha256sum`. Write the checkpoint to a temporary file,
close it, then rename it into place. Publish `complete: true` only after that
operation succeeds. The verification limit is 1 GiB per checkpoint. Larger or
multi-file checkpoint formats need an application adapter before this operation
can verify them; Raven does not silently skip their data checks.

`expected` describes the checkpoint format and environment. `actual` describes
the restart environment that the application has checked. These maps must match
exactly. The application remains responsible for the meaning of its compatibility
identifiers. Pin the exact restart script as well as the checkpoint.

The generated shell script exports `RAVEN_CHECKPOINT` and
`RAVEN_CHECKPOINT_SHA256`. A Python 3 guard rechecks the hash when the queued job
starts, before the application runs. Keep the checkpoint immutable for the full
restart. Write `restart.sbatch` to consume the path explicitly:

```sh
#!/bin/bash
#SBATCH --time=01:00:00
#SBATCH --cpus-per-task=4
exec ./solver --restart "$RAVEN_CHECKPOINT"
```

Use a bash, sh, or dash script. Raven inserts literal environment values after
the leading Slurm directives. The generated script has its own filename; do not
use `$0` to locate project inputs. Use the declared work directory instead.

Before submission, Raven queries both the live queue and accounting for the
source ID. It rejects an active source, a missing or ambiguous attempt, a changed
start time, or a different cluster. Array task IDs and heterogeneous component
IDs are supported. A missing accounting service blocks the restart; it does not
cause Raven to guess the source identity.

## Adaptive parameter search

The search uses a bounded, deterministic discrete method. It
explores distinct configurations. When successful results exist, it usually
selects an unused configuration near the best result. Every fourth proposal
remains an exploration step. Numeric distances are normalized per dimension;
categorical differences have unit distance. This method has no external optimizer
dependency and does not claim Bayesian uncertainty estimates.

```json
{
  "schema": "raven.parameter-search/v1",
  "id": "mesh-study-01",
  "script": "trial.sbatch",
  "workdir": ".",
  "budget": 6,
  "max_parallel": 2,
  "direction": "minimize",
  "parameters": {"mesh": [64, 128, 256], "method": ["a", "b"]},
  "observations": []
}
```

Select **Propose** to inspect candidates without a submission plan. Select
**Launch**, set **Trial count**, and inspect the plan before Apply. The limits are
16 dimensions, 128 values per dimension, 4,096 total configurations, and 64
concurrent trials. Values must be finite JSON scalars. Existing Slurm arrays in
the trial script are not supported: each logical trial has one scheduler receipt.

The trial script reads `RAVEN_TRIAL_ID` and JSON `RAVEN_TRIAL_PARAMETERS` from its
environment. For example, a Python application can use
`json.loads(os.environ["RAVEN_TRIAL_PARAMETERS"])`. Arguments are not interpolated
into shell commands. An optional `sbatch` list contains normal submission
workbench overrides.

Record results under `observations` after the trial finishes:

```json
{"trial": "COPY_THE_REVIEWED_TRIAL_ID", "status": "success", "metric": 0.014}
```

The other statuses are `scientific_failed` and `infrastructure_failed`. Only a
successful finite metric influences the best result. Infrastructure failures do
not become bad scientific objective values. A scientific outcome for a job that
is still in the current queue does not free its concurrency slot. Unresolved
launches continue to use a slot. Explicit scheduler rejections free the slot but
still consume a distinct trial attempt from the budget.

Raven persists trial intents and receipts across restarts. It never launches the
same configuration twice within one campaign. Parameters, script, resource
overrides, budget, direction, and concurrency are pinned to the campaign ID.
Parameter strings have a 1,024-character limit. Raven checks the total journal
size and the 1 MiB script budget for a proposal batch before submission; reduce
the trial budget or batch size if those checks fail.
Change the campaign ID for a different experiment. An uncertain launch must be
reconciled before it can receive a scientific outcome; do not delete the journal
to force a retry.

## Short-task packing

```json
{
  "schema": "raven.packed-tasks/v1",
  "id": "packed-analysis-01",
  "workdir": ".",
  "cpus": 8,
  "memory_mb": 16384,
  "parallel": 4,
  "walltime": "00:30:00",
  "sbatch": ["--partition=compute"],
  "tasks": [
    {"id": "sample-a", "argv": ["python3", "analyze.py", "data/a.csv"], "cpus": 2, "memory_mb": 4096},
    {"id": "sample-b", "argv": ["python3", "analyze.py", "data/b.csv"], "cpus": 2, "memory_mb": 4096}
  ]
}
```

Raven submits one single-node allocation. The portable standard-library Python
runner admits a task only when its declared CPU and memory requests fit in the
remaining budget and its concurrency limit. Each task runs through
`srun --exclusive --exact` with its own CPU and memory request. Slurm provides
actual isolation according to the site's configured task and memory enforcement.
The admission calculation does not replace a site's cgroup configuration.

Each task has an argument list, a work directory, and a unique ID. The task limit
is 4,096. `timeout_s` defaults to 86,400 and cannot exceed seven days. A task cannot
request more resources than the allocation. Standalone colon arguments are not
permitted because Slurm interprets them as component separators.
Optional packing `sbatch` flags are limited to account, partition, QoS,
reservation, and constraint; they cannot
override the runner's resource limits.

The result directory contains `manifest.json`, `receipts/ID.json`, and separate
`logs/ID.out` and `logs/ID.err` files. The special ID `manifest` is safe because
receipts are in their own directory. Completed, failed, and uncertain intents
block automatic replay. A changed manifest cannot reuse an existing result
directory. Output directories must be owned by the current user, must not be
writable by other users, and cannot traverse symbolic links.

Cancellation terminates each active process group and marks tasks that did not
start. A task timeout is distinct from an application failure. Tasks must not
daemonize: remaining children are terminated when their task leader exits. The
copied runner needs Python 3 on the compute node, but does not need Raven there.
The runner's `--local` option is an explicit test mode without Slurm isolation.
Normal prepared submissions always use Slurm steps.

## Elastic Dask pool

Install `dask-jobqueue` in the Python environment that runs Raven. It remains an
optional dependency. For example, use that environment's Python executable with
`-m pip install dask-jobqueue`. Compute nodes also need compatible Dask and
distributed installations and network access to the scheduler. Site firewall,
interface, and queue settings remain site configuration.

```json
{
  "schema": "raven.dask-pool/v1",
  "id": "analysis-pool-01",
  "cores": 8,
  "memory_mb": 16384,
  "walltime": "01:00:00",
  "queue": "compute",
  "max_jobs": 10,
  "min_jobs": 0,
  "adaptive": true
}
```

Optional fields are `account`, `interface`, `local_directory`, and `job_name`.
Use **Start** with target zero for adaptive mode. Dask's adapter owns adaptive
scaling between `min_jobs` and `max_jobs`. In fixed mode, **Target Slurm jobs** sets
the requested pool size. **Scale** stops the adaptive controller before it applies
the fixed target. **Stop** closes the cluster and its owned jobs.

One persistent process owns each pool. It remains active when Raven exits. Its
status includes the scheduler address, PID identity, desired jobs, requested
jobs, connected jobs, and queued or starting jobs. Each Slurm job starts one Dask
worker process with the configured cores, so these counts use the same units.
The queued estimate is requested jobs minus connected jobs. It includes workers
that are still starting and must not be interpreted as an exact Slurm pending
state count.

The controller reads owned requests and publishes status every half second.
Starting it does not block rendering. A start or scale response reports that a
request was queued; inspect again for its acknowledgement. A stale or reused PID
does not prove ownership. A failed or unacknowledged controller blocks another
start until its scheduler jobs are reconciled. Raven does not adopt or cancel
unrelated Dask jobs. Linux `/proc` process identity is required for live controls.

## Heterogeneous allocation

```json
{
  "schema": "raven.heterogeneous-allocation/v1",
  "id": "coupled-run-01",
  "workdir": ".",
  "walltime": "01:00:00",
  "components": [
    {"resources": {"nodes": 1, "ntasks": 8, "cpus-per-task": 1, "mem": "16G"}, "argv": ["./cpu-solver"]},
    {"resources": {"nodes": 1, "ntasks": 1, "gpus": 1, "mem": "32G"}, "argv": ["./gpu-solver"]}
  ]
}
```

Raven generates real colon-separated `sbatch` component requests and a coupled
`srun --het-group=0 ... : --het-group=1 ...` launch. It validates each component's
resources and preserves their order. The component limit is 32. Every component
must explicitly request nodes and tasks. Use the report to inspect both commands.
Standalone colon arguments and executable names that start with a dash are not
permitted in component commands.

Components support CPU, memory, GPU, partition, account, QoS, constraint,
reservation, and exclusive requests. Invalid or conflicting resource options
block preparation. Inherited `SBATCH_*` environment options also block the plan,
so unreviewed environment flags cannot alter its allocation. Site support for
heterogeneous jobs, the selected MPI configuration, and compatible component
queues are required. Raven reports an exact submission receipt and does not
convert a site rejection into a successful launch.

## Validation limits

The test suite executes real local packed tasks, cancellation, timeouts, and
resource admission. It verifies immutable evidence, duplicate-launch prevention,
scope changes, malformed inputs, and controller ownership with scheduler and
Dask adapters. A local controller smoke test can validate the optional Dask API
without starting Slurm jobs. A live site must still validate its scheduler,
shared filesystem, task isolation, network routes, and application restart
contract before production use.
