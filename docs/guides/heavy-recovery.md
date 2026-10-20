# Recover a Heavy allocation request

Raven opens in Light while its owned Heavy worker starts. Before requesting a
Slurm allocation, it reads the actual controller's `ClusterName` through the
configured connection. An empty profile cluster is filled from that observation;
an explicit conflicting cluster or an unavailable controller stops the request
before any allocation is submitted.

Raven removes inherited `SALLOC_*`, `SBATCH_*`, and step hints from its launch
environment. It preserves `SLURM_CONF` so launch, placement and recovery address
the same controller. Choose the billing account, partition and QoS in the
allocation settings. Blank values use the scheduler defaults.

## Cancel or release

Use the compute status control and review **Cancel request** or **Release
compute**. A pending `salloc` receives a catchable signal, allowing it to revoke
the request. A granted worker first receives its shutdown message, then a
hang-up if needed. Raven waits at least the observed Slurm `MessageTimeout` plus
a margin before a final hard stop. Closing the interface can leave its isolated
cleanup helper draining during this interval.

An SSH supervisor translates input EOF and hang-up into cleanup of its own
remote launch. No cancellation targets a scientific job selected in the Jobs
pane. Reusing an existing allocation gives Raven ownership of its named worker
step; the parent allocation is preserved. When the profile leaves the cluster
blank, reuse review also reads the selected controller's identity. Its typed
receipt and the launch plan retain that identity even when the job record omits
the cluster. Launch checks the controller again before creating the worker step.

## Durable receipts and local coordination

`storage.allocation_receipts_dir` names a private durable directory for receipts.
Its default is `${XDG_STATE_HOME:-~/.local/state}/raven/allocations`, independently
of a node-local `RAVEN_STATE_DIR` override. It can live in a shared home so the
receipt is visible from another login host. Each receipt records its original
login host, connection, request token, actual cluster and observed job/step.

Kernel ownership locks remain on reliable host-local storage. An exclusively
created shared claim prevents two login hosts from starting the same context
simultaneously. Atomic, fsynced receipt publication preserves that claim across
interruption. Changing only the configured cluster does not hide an older
receipt; compatible old receipt names are migrated under ownership checks.

## Reconciliation

When cleanup is uncertain, Light remains available and Raven performs bounded
background checks with persistent backoff. Recovery uses connection identity
without requesting Heavy permission. It accepts exact terminal controller or
accounting evidence, a recognized submission rejection before allocation, or
the foreground `salloc` relinquish message followed by process exit. A worker's
nonzero exit alone does not erase a successful relinquish.

For a unique one-shot request whose original owner is gone, two successful
name-and-user controller reads separated by `MessageTimeout` and a margin can
establish absence even when accounting has no record. Empty accounting alone
does not prove cleanup. An active receipt from another login host requires
inspection of that original owner rather than automatic cancellation there.

Choose **Retry cleanup** after correcting a connection or scheduler problem.
If independent inspection has established what happened, choose **Forget this
request**, read the warning and confirm. This archives the original receipt and
clears Raven's bookkeeping. It does not cancel compute or certify that an old
allocation stopped; check the exact scheduler request and original login host
before confirming. No scientific submission is retried by recovery.

On an explicit desktop profile, a changed boot or vanished original helper
process identity can resolve an orphan under the local lock. Raven never sends a
signal to a PID merely because it appears in an old receipt.

## Worker liveness and limits

Deferred placement reads retry within the worker's verification window.
If the worker has no step cgroup, Raven requests fresh process membership from
`scontrol listpids JOB.STEP` on the existing connection. Its exact header, job
and step must match, and the worker PID must be present. A login host that
cannot observe the compute step leaves placement unverified; Raven does not
start an extra inspection step or assume that environment variables prove it.
Rejected or finished tasks release admission; acknowledged results are retired
from bounded state rather than consuming a lifetime task quota. Recent retired
task identities remain in a bounded replay-detection window.

Only an unsent latest status summary is retained while the interface is paused.
Committed task results retain their separate acknowledgement contract. A
malformed or oversized task result fails that task and leaves other accepted
work and the allocation available.

On a single-threaded Linux worker, fixed Raven operations run in fresh forked
task processes with private pipes, sessions and resource limits. The owner can
reuse imported implementation modules; application task state is never reused.
Other supported execution contexts retain fresh-interpreter task startup.

Local process and fake-Slurm tests validate these contracts. They do not qualify
a particular cluster's cancellation latency, shared filesystem or SSH policy.

The lifecycle implementation lives in `raven.heavy`. Historical Python module
imports and the worker/helper command entry points remain compatible. Worker
handshakes include the moved implementation files in the build digest.
