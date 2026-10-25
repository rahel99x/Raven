# Native scheduler connections

Raven's scheduler registry separates transport, native scheduler semantics, and
the interface. The established Slurm dashboard remains available. New native
connections use explicit scheduler operations and a reviewed mutation boundary.
Importing an adapter never contacts a cluster or imports code from a project.

| Adapter | Native source | Identity | Important limits |
| --- | --- | --- | --- |
| Slurm | `squeue`, `sacct` JSON | Connection, native cluster, exact task/job ID, submission time, restart counter | Modular adapter requires structured fields; missing restart counters disable actions. Existing Slurm readers retain their compatibility fallbacks. |
| Kubernetes | `kubectl`, `batch/v1` Jobs | Context/server/namespace scope, Job name and UID | Raven-labelled Jobs only; retained completed Jobs are history, not durable deleted-job accounting. |
| IBM Spectrum LSF | `bjobs` JSON | Connection, job/array index, authoritative submission time | Imprecise submission times disable actions. LSF memory units remain site dependent. |
| OpenPBS | `qstat -F json` | Connection, server-qualified job ID, creation/run attempt | Completed-job retention depends on server history settings. |
| PBS Professional | `qstat -F json` | Same native identity boundary as OpenPBS | Native version/site qualification is separate from OpenPBS. |
| HTCondor | ClassAd JSON | Schedd/pool scope, cluster/process ID, GlobalJobId and start count | Submission files are Condor `.sub` documents, not shell scripts. |
| Flux | Flux JSON | Explicit instance URI, job ID, submission time | Nested Flux instances must be selected explicitly. |

These are implemented adapters with native-format fixtures. Fixture validation is
not live production qualification. Availability and native limitations are
reported by each connection's capabilities operation. Oracle is not included.

## Connection and review

The Python boundary is intentionally small:

```python
from raven.schedulers import create
from raven.slurm import Backend

connection = create(
    "slurm", Backend(),
    {"cluster": "research", "user": "alice", "account": "lab-x"},
    journal_dir="/node-local/alice/raven/scheduler-actions",
)
jobs = connection.jobs()                    # Current login only by default
history = connection.jobs(history=True)
capabilities = connection.capabilities()
# Resolve a native ID without trusting a caller-provided attempt token:
# current = connection.observe("12345")

plan = connection.review("submit", submission={
    "script": "/shared/experiment/job.sh",
    "account": "lab-x", "partition": "cpu",
    "cpus": 2, "memory_mib": 1024, "wall_seconds": 120,
})
# Present plan, native argv, script digest, billing and resource settings to the
# user. Only after that explicit review:
receipt = connection.apply(plan, review_digest=plan["digest"])
```

Use `host` for the SSH transport endpoint and `schedd`/`pool` for HTCondor's
native target. Billing-account defaults do not split a native job's identity:
two lab profiles on the same connection can identify the same job. Different
SSH aliases still produce separate connection scopes; grouping those requires a
verified canonical site association rather than guessing that two hosts match.
Changing a connection scope creates a new connection; mutating an existing
client's scope in place is rejected before native I/O.

The CLI and Operations workbench expose the same review/apply boundary. A plan
expires after five minutes. Before changing a job, Raven reads its native record
again and checks the attempt and selected login. Before submission, Raven checks
the script's digest again and streams those verified bytes to the native command.
Script directives are preserved. Set `name` explicitly when a descriptive name
is required: a native scheduler may call an unnamed stdin submission `STDIN`.
For modular Slurm mutations, `scope.cluster` must be the actual configured
`ClusterName`; the ambient default is available for inspection only. This leaves
the established Slurm launch workflow unchanged. Native submission replies must
identify the reviewed Kubernetes Job name, namespace, owner and an actual UID;
an unrelated or malformed reply becomes an unknown outcome with a consumed token.

Native script paths refer to the scheduler host, including through Raven's
existing SSH transport. Scripts must be regular, non-symlink, unambiguous UTF-8
files without NUL bytes, at most 96 KiB. The small input bound keeps payloads below
common per-argument process limits. A heavily quoted SSH command can reach a
platform's argument limit sooner; that error is reported rather than retried.

The private durable journal records a claim **before** attempting a mutation.
Keep it on suitable node-local storage and use the same directory after
reconnecting. A claimed plan cannot run again, including from another process.
An interrupted or timed-out command has an **unknown outcome**, because the
scheduler may already have accepted it. Inspect the native scheduler and receipts
before preparing another plan. Deleting a claim does not prove an action failed.
Successful and unknown outcomes also receive an atomic private receipt beside the
claim. A claim without a final receipt means the result was not observed. Large
native command output is retained as an explicitly truncated preview with its
original byte count; parsing uses the complete bounded native response.

## Kubernetes

Always select an explicit context and namespace. Pin the API server for mutations
so a changed kubeconfig context cannot redirect a reviewed submission:

```python
connection = create("kubernetes", Backend(), {
    "cluster": "campus", "context": "campus-research",
    "namespace": "lab-x", "uri": "https://api.cluster.example:6443",
    "user": "alice",
}, journal_dir="/node-local/alice/raven/scheduler-actions")
```

Submission accepts `{"manifest": <batch/v1 Job JSON>}`. The manifest must have an
exact name and native pod template, named container images, and an explicit
`Never` or `OnFailure` restart policy. Its namespace must match the connection.
Raven adds discovery labels and a login annotation. Those are navigation hints;
Kubernetes RBAC remains the authority for every request.

Requests, limits, millicores, init containers, GPU extended resource names,
parallelism, completion counts and deadlines retain their native values. Raven
does not convert `500m` into a physical-core allocation or invent GPU utilization
from a device request. Volumes, images, service accounts and site admission
requirements remain explicit in the manifest.

Cancellation sends a native `DeleteOptions` body with a UID precondition. A Job
recreated under the same name cannot receive the old Job's cancellation. The
adapter does not advertise Kubernetes suspension as equivalent to Slurm hold.
Deleted Jobs require Raven's retained evidence or an external accounting source;
the API server's current Job list cannot supply their historical counters.

## Budgets and unsupported capabilities

The common transport bounds wall time, combined output bytes, cancellation and
record count. Each connection serializes native reads, reuses identical results
for a short minimum interval, and reports request/cache/error/byte counts plus
the last successful read time. Rendering must consume retained snapshots, not
issue scheduler commands. Mutations invalidate cached observations.
Counters count native commands, not inferred scheduler RPCs or authentication
requests. Kubernetes uses raw namespaced REST reads, avoiding implicit discovery
and pagination. A server continuation requires a narrower query or an explicitly
larger record budget instead of hidden additional requests.

These limits protect one connection. Site-wide quotas across multiple dashboards
need a shared site admission policy; a per-process cache is not a global rate
limiter. Native parsers reject ambiguous identities rather than invent missing
timestamps or silently discard incompatible rows.

Native schedulers differ in atomic action support. Kubernetes provides an atomic
UID delete precondition. HTCondor can use a native ClassAd constraint containing
the verified GlobalJobId and start count. Slurm, PBS, LSF and Flux actions
revalidate first and then issue a native command; a scheduler-side race can
remain between those operations. Capability notes preserve that distinction.

Fair share, billing history, node topology, GPU counters, Heavy workers and
checkpoint support are not implied by a successful job-list request. Each needs
a native implementation and site qualification before the interface enables it.
SSH credentials, kubeconfig credentials and scientific data remain in their
existing stores; connection manifests do not need credential values.
