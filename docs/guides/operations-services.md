# Persistent monitoring, data transfers, and workflow evidence

[README](../../README.md) · [Research](../RESEARCH.md) · [Project standard](../PROJECT_STANDARD.md)

Use these operations to manage a persistent scheduler monitor, transfer declared
files with checksum verification, and inspect native workflow evidence.
Service control and file transfer require explicit review. Workflow observation
is read-only.

1. Open **File → Research and cluster operations** and select an operation.
2. Set its fields and select **Inspect / prepare**.
3. Read the result. If a plan is available, select **Review action**.
4. Check the exact plan, then select **Confirm apply**.

A plan expires after five minutes. Prepare a new plan if the connection or
source changes. Inspection runs outside the terminal renderer.

The commands `:ops supervisor`, `:ops staging`, and `:ops workflow-engine` open
the corresponding forms. `:ops catalog` opens the catalog. For an inspection
with explicit arguments, use `:ops run FEATURE key=value`, for example
`:ops run workflow-engine engine=nextflow source=/project/run/trace.tsv`.

## Persistent monitor — S03

Select **Persistent monitor**. Set **Action** to `status`, `start`, or `stop`.
The default directory is private and specific to the connection. To reconnect,
select **Status** with the same connection and directory. The result includes
the service state, owner, source errors, queue and accounting observations, and
recent state transitions. A stopped service's saved observations are marked
stale.

Set **Action** to `start` and inspect the user, cluster, interval, and record limit.
Select **Review action**, check the plan, then select **Confirm apply**.
Raven launches a separate process. Closing the terminal does not stop it.
No monitor starts during installation or a normal Raven launch. Action `stop`
requests a graceful stop of the exact reviewed instance. Refresh to confirm
that the service has stopped. A command that is already in progress can take
up to both eight-second source timeouts before a stop request is processed.

The service reads `squeue` and `sacct`; it does not submit, requeue, cancel, or
recover jobs. Hosting must follow your site's rules for long-running processes
on login or service nodes. It requires Python, Slurm clients, and the installed
Raven package on that host. Use a local Raven session on the target host. An
SSH profile cannot start a substitute service on your workstation.

Command-line access uses the same service:

```bash
python -m raven.supervisor start "$HOME/.local/state/raven-monitor" \
  --user "$USER" --interval 5 --max-records 1000
python -m raven.supervisor status "$HOME/.local/state/raven-monitor"
python -m raven.supervisor stop "$HOME/.local/state/raven-monitor"
```

Add `--cluster CLUSTER` to Start for an explicit Slurm cluster. The interval
range is 1–300 seconds. The record limit is 1–10000 per source. Each source has
an eight-second command limit and a two-MiB output limit. Accounting covers
the last 24 hours. If a response exceeds the output limit, the source is
unavailable; Raven does not parse a partial command response. Record-limit
truncation is reported. This is a monitor, not a complete accounting archive.

The state directory has mode `0700`. It contains:

| File | Purpose |
| --- | --- |
| `writer.lock` | Stable advisory lock; only one service owns the directory |
| `owner.json` | PID, process start identity, and random service token |
| `config.json` | User, cluster, interval, and record limit |
| `snapshot.json` | Atomic observations and a bounded transition history |
| `stop.json` | Stop request bound to a specific service token |

Do not remove the lock file while a service runs. After a host or process
failure, Start takes the released lock and reconciles the saved observations
with current Slurm data. An unavailable source retains its previous data with
an error; it does not turn all jobs into completed jobs. Requeued attempts use
their start identity. A delayed accounting response does not override queue
evidence for the same attempt. A reused PID cannot accept an old stop request.

The service uses Linux process start identity. CARC and Fedora meet this
requirement. A different operating system requires a supported identity
adapter before persistent monitoring can be used reliably.

## Verified staging and return — S04

Select **Verified staging and return**, choose a manifest, and set **Transfer
direction** to `input`, `output`, or `all`. Inspect the exact paths, byte count,
and manifest revision. Select **Review action**, check the plan, then select
**Confirm apply** to perform the declared transfers.

Use [`raven.staging/v1`](../schemas/staging.schema.json). The
[example](../examples/staging-manifest.json) has input and output entries. Set
each file's absolute source and destination path, byte size, SHA-256 checksum,
and direction. Create destination parent directories before review. Raven does
not walk directories or infer extra files.

```json
{
  "schema": "raven.staging/v1",
  "id": "experiment-42-input",
  "retention": "keep-source",
  "entries": [
    {
      "id": "empty-example",
      "direction": "input",
      "source": "/project/data/empty.txt",
      "destination": "/scratch/experiment-42/empty.txt",
      "size": 0,
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
  ]
}
```

The manifest supports 1–1000 regular files. Each transfer streams in one-MiB
chunks, checks the reviewed source identity, computes SHA-256, and publishes a
complete file atomically. The destination receives mode `0600`. Source files
remain in place. Source and destination symlinks, same-file aliases, duplicate
destinations, and chains that use another entry's destination as an input are
refused. Hard-link publication must be supported by the destination filesystem.

An existing destination with the expected bytes is verified and retained. An
existing destination with different bytes is refused. Raven never overwrites
that file. On a checksum error, source change, disk error, or interruption, an
unfinished temporary file is removed. Verified earlier transfers remain valid.
The result names failed, cancelled, and unattempted entries.

Receipts are written atomically under Raven's state directory in `staging/`.
A receipt records the manifest revision and each outcome. To resume, inspect
the same manifest and select Apply again. Raven verifies existing destinations
before skipping them; a receipt alone is not proof that the file still exists
or contains the expected data. One writer can execute a revision at a time.

Supported paths are local or mounted POSIX filesystems. No SSH, cloud object
store, Globus, or automatic scheduler staging adapter is included. Use a local
session on a host that can see both mounts. Output return uses the same
verification contract with `direction: "output"`. Source deletion and
automatic retention expiry are intentionally unavailable.

## Native workflow observer — A11

Select **Native workflow observer**, select the engine, and enter its source
file. **Workflow identity** can name a specific engine run; without it the
source path identifies the observation. Refresh rereads the source through the
current connection's file adapter. SSH profiles read the remote file and never
substitute a local file with the same name.

Raven keeps native task, attempt, hash, scheduler ID, and state evidence when
the engine supplies it. It does not infer missing scheduler IDs, dependency
edges, or execution state. The native engine owns scheduling, retry, resume,
and cancellation. This view has no Apply action and never resubmits work.

### Nextflow

Enable Nextflow's native trace output when starting a workflow:

```bash
nextflow run main.nf -with-trace trace.tsv
```

Select engine `nextflow` and source `trace.tsv`. The TSV header must contain
unique `task_id` and `status` columns. Optional `native_id`, `name`, `process`,
`hash`, `attempt`, and `exit` columns enrich the result. Repeated updates for
the same task/hash/attempt retain the last complete row. An unfinished final
line is omitted until the writer completes it.

Raven reads the original header and at most the last two MiB of trace records.
It reports truncation and does not claim that this tail contains the entire
workflow. A trace can contain at most 10000 retained task attempts. A file that
changes during the read is retried on Refresh. Trace order is not a dependency
DAG, so this adapter does not create edges from row order.

### Snakemake

Export the native DAG:

```bash
snakemake --d3dag > dag.json
```

Select engine `snakemake` and source `dag.json`. Raven accepts the native
`nodes`/`links` format. This format has rule and graph identity but no runtime
state or scheduler IDs. Those fields remain unknown.

For live state, an executor or project adapter can atomically publish
[`raven.workflow-engine/v1`](../schemas/workflow-engine.schema.json). Use the
[example](../examples/workflow-engine.json). Each node declares its native
logical ID, rule, optional scheduler ID, state, attempt, and dependency IDs.
Keep the engine's run identity in `workflow_id`. Do not mix different runs in
one snapshot. This contract does not install or replace a Snakemake executor.

Both Snakemake formats are bounded to two MiB, 10000 nodes, and 50000 edges.
Raven rejects duplicate IDs, missing dependency targets, self-dependencies,
duplicate edges, and cycles. Graph validation is linear in the node and edge
count and does not run on pointer movement or during rendering.

## Validation and operational limits

Automated tests cover real monitor process launch, reconnect, lock contention,
graceful stop, restart, stale tokens, recycled PID evidence, bounded child
output, and cleanup of child process groups. Transfer tests cover review
changes, checksum failure, no-clobber publication, resume, partial success,
cancellation, symlinks, hard-link aliases, FIFOs, and directory replacement.
Workflow tests cover bounded tails, partial lines, changing files, remote
reads, duplicate identities, malformed DAGs, and missing runtime data.

The tests use deterministic Slurm command fixtures. Site service permissions,
shared-filesystem behavior, and native engine plugins need validation on the
target installation. A fixture result does not establish live-cluster or
native-workflow compatibility for every version.
