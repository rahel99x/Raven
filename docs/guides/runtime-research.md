# Runtime research operations

[README](../../README.md) · [Project standard](../PROJECT_STANDARD.md)

These operations model execution scenarios, compile diagnostic intents,
summarize supplied evidence, display scientific fields, and coordinate
application control or I/O. Each operation requires its declared input contract.
Cooperative control also requires an application that uses the runtime SDK and
a supported local mailbox filesystem.

Open the research operations workbench and select the **Runtime** group.
Each operation also has an `:ops` key shown below. JSON examples are in
[`examples/runtime`](../../examples/runtime). Model inspection and compilation
do not execute the proposed workload or change scheduler policy.

## Execution scenario laboratory

Use `execution-simulation` with `execution-model.json`. Enter scenario JSON such
as `{"rates":{"fabric":10}}`. The model declares resources, units of work,
dependencies, rank placement, routing, and the permitted rate domain.

The simulator executes a deterministic nonpreemptive resource DAG. Each resource
is one serial server. Independent resources overlap. A resource has a work rate
and an optional latency. A rank placement change is accepted only if every
required route exists. The input workload, dependency graph, and rank count stay
fixed. The model covers only the declared resource DAG. It excludes scheduler
waiting time, application branches, cache effects, packet contention, and changes
to problem size; it does not provide general MPI or GPU simulation.

Calibration observations fit a scalar ratio between modeled and measured run
durations. Separate validation observations report residuals and relative errors.
Declare `domain.max_validation_relative_error` between 0 and 1 to state the
acceptance threshold; for example, 0.05 permits at most 5% relative error in
**each** held-out observation. Set this policy before evaluating the observations.
Raven checks the declared policy but cannot certify when the project chose it or
whether the declared held-out cases are independent of fitting.

Merely supplying an observation does not validate a model. The validation state
is `unqualified` if either the policy or held-out data is absent. This also applies
to older manifests that omit the optional policy. It is `passed` only if every
held-out case meets the threshold. Any failed case sets validation to `failed`,
withdraws the prediction, and sets `predicted_seconds` to null. A separate
`diagnostic_model_seconds` retains the computed value for explaining the failed
model; it is not an accepted forecast. Changing a fit, model, or acceptance policy
requires a new evidence record. Failures must not be hidden by deleting them.

The view identifies whether the candidate rates and placement were directly
observed in validation. Passing other scenarios does not independently verify
this candidate, nor establish a confidence interval. Existing fixed-workload and
declared-domain restrictions still apply. A model permits at most 20,000 events, 256
resources, 4,096 ranks, and 64 observations. Timeline rows are bounded in the
report; its structured data retains the modeled event records.

To retain a prediction, enter a new prediction-record directory. Inspect the
inputs, select **Review action**, check the plan, then select **Confirm apply**.
Raven verifies the reviewed source and writes `model.json`, `scenario.json`,
`prediction.json`, and its completion receipt without overwriting existing files.
The record includes the model digest, fit digest, prediction ID, held-out evidence
revision, applicability, residuals, acceptance policy, and qualification outcome.
Additional held-out observations revise the status of the same prediction ID when
the model, fit, policy, and scenario remain unchanged. A later failed revision is
recorded as withdrawn; historical records remain immutable and must not be read
as the current qualification. Recording does not launch a validation job.

## Diagnostic intent compiler

Use `diagnostic-recipe` with `diagnostic-intent.json` and
`site-capabilities.json`. Declare the exact observations the question requires,
the runtime fingerprints, and the CPU, memory, storage, and duration budgets.
Each adapter declares provided observations, runtime requirements, conflicts,
site permission, estimated cost, and a proposed argument vector.

Raven chooses a compatible set of up to 16 declared adapters. It prefers exact
observations to explicitly permitted approximations, then minimizes relative
resource costs. Missing mandatory evidence, conflicting adapters, or exhausted
budgets cause a clear failure. Costs and capabilities are declared evidence,
not a live certification. The example commands are illustrative and require
applications built for their instrumentation.

**The compiler does not execute commands from capability files.** Inspect the
result, check it against the site's tool instructions, and run approved recipes
through the site's normal application launch procedure.

## Summaries with provenance

Use `information-query` with `query-source.json` and `summary-query.json`.
The supported query family is count, sum, minimum, and maximum over named numeric
columns. Every row retains a unique raw reference. Null or absent values increase
the missing counter. Source coverage propagates unchanged.

Counts and rational sums are exact over supplied stored numbers. `exact_sums`
contains numerator and denominator strings. Display sums use stable floating-point
accumulation. JSON numbers already parsed as binary floats do not gain arbitrary
decimal precision. Unsupported questions, such as quantiles or correlations,
require raw data and are rejected. Sources allow at most 50,000 rows and queries
at most 32 fields. Existing operation file limits apply before computation.

## Scientific fields and slices

Use `scientific-field` with `scientific-field.json`. Supply a row-major flattened
array, a 1D, 2D, or 3D shape, units, and optional axis labels. For 3D data, set
the zero-based slice axis and index. Raven shows a terminal field, distribution,
full-data extrema, slice extrema, and original flat indices.

The reader permits at most 262,144 cells. Extract larger or native-format datasets
using their own readers before passing them to Raven. Each display tile preserves
minimum, maximum, mean, valid count, and gap count. Mixed missing tiles use a
distinct glyph; no observation is invented. These JSON views do not imply native
HDF5, netCDF, mesh, or arbitrary-volume support.

## Cooperative application control

The application opts in. It declares commands and calls the SDK at a safe point.
Raven queues a typed request. The application performs its own synchronization,
applies the change, and returns an acknowledgment. Raven never injects code,
sends a stop signal, or changes application memory externally.

Initialize a new mailbox on node-local storage:

```bash
tower runtime --mailbox /tmp/tower-runtime-demo init examples/runtime/control-config.json
python examples/runtime/cooperative_app.py /tmp/tower-runtime-demo
```

In another terminal on the same host:

```bash
tower runtime --mailbox /tmp/tower-runtime-demo --attempt job123:attempt1 submit learning-rate-change-1 learning-rate '{"value":0.025}' --epoch 0
tower runtime --mailbox /tmp/tower-runtime-demo --attempt job123:attempt1 status
```

The same interfaces are available as `python -m tower.runtime_control`. Replace
the example identity with an exact cluster/job/array/requeue attempt identity.
The configuration lists every authorized attempt. A job ID alone is insufficient
when a job can restart. The mailbox belongs to one host and boot. Use a new
mailbox after a reboot and reconcile old receipts.

For an interface review, select `runtime-steering` and open
`runtime-request.json`. Apply queues the reviewed request after source-file,
connection, mailbox identity, configuration, and parameter-epoch checks. The
application acknowledgment, not successful queueing, confirms application.

The Python SDK is:

```python
from tower.runtime_control import RuntimeControl
control = RuntimeControl(mailbox_path, exact_attempt)
control.safe_point("epoch", apply_declared_command)
# handler(command, parameters, request_id) returns a bounded JSON result.
```

Commands support bounded numeric/integer parameters, booleans, and explicit
enums. Every command has an immutable request ID, expected epoch, expiry, claim
token, and durable receipt. Retrying the same request does not execute it again.
A successful acknowledgment advances the epoch. An old-epoch request becomes
stale. An unclaimed expired request cannot apply.

If an application crashes after claiming a command, its side effects can be
unknown. Raven marks it uncertain and prevents another command for that attempt
from overtaking it. The application must reconcile the claim using `ack` or
`acknowledge`. It must not claim that a failed callback had no effects without
checking its state. This is not an exactly-once guarantee across arbitrary
application crashes.

The mailbox directory must be owned by the user and mode 0700; the database must
be a private mode-0600 regular file. Symlink paths, a changed host/boot, distributed
filesystems, and unknown filesystem types are rejected. Supported local types
are ext2/3/4, XFS, Btrfs, tmpfs, ramfs, ZFS, and overlay. This is a same-user boundary, not isolation
between mutually untrusted processes of one account. SQLite transactions have a
bounded lock wait. Each API call returns; no UI worker waits on a permanent stream.
The status view shows the latest 64 command and permit receipts. Use
`get REQUEST_ID` or `permit-status REQUEST_ID` to retrieve an older exact receipt.
Mailboxes retain at most 10,000 command receipts and 10,000 permit receipts.
Archive completed receipts and create a new mailbox when the bound is reached.

## Cooperative I/O permits

The same mailbox coordinates discretionary I/O across its authorized attempts.
Declare permit capacity and maximum lease duration in the configuration. Select
`io-coordination` with `io-request.json` to review a permit request. Raven queues
it with **no asserted arrivals**. Only the application reports its own arrivals.

The application calls `permit(request_id, participants=[...], arrived=[...],
lease_seconds=30, wait_seconds=10, fallback="skip")`. Calls return immediately.
After every declared participant has arrived, the coordinator grants capacity
in request order among ready requests. It returns a token and lease deadline.
Use `renew(request_id, token, seconds)` before expiry, and call
`release(request_id, token)` only after the I/O operation has completed.

For a multi-node or collective application, use its existing control rank to
gather arrivals, request a permit on that rank's host, and broadcast **one**
decision before collective I/O. Raven does not implement an MPI barrier, detect
failed ranks, or allow nodes to share this SQLite database. Each rank must follow
the application's existing collective failure protocol. A group that misses
participants is marked incomplete; it never gets permission to start half of a
collective.

At the deadline, `fallback="skip"` skips optional output. Explicit
`fallback="proceed"` permits a deadline-critical checkpoint to bypass this
advisory capacity limit only after all participants arrived. That outcome is
reported as `bypass`, not a normal grant. This policy protects application
checkpoints; it does not promise a strict throughput cap during emergency output.

An expired active lease becomes uncertain and continues to occupy capacity.
The writer may still be performing I/O. Raven does not free that capacity until
the application releases the token after reconciliation. This prevents expired
leases from silently admitting overlapping writers. The service does not control
other users or filesystem-wide fairness.

CLI helpers are `permit`, `renew`, and `release`. `poll` and `ack` provide the
same command protocol for applications that do not import the Python SDK. No
arbitrary manifest command or shell string is executed by these helpers.

## MPI, multi-process and multi-GPU applications

`tower.collective_runtime.CollectiveRuntime` is a working application-side
coordinator. Pass an existing mpi4py-compatible communicator. Only communicator
rank zero accesses the local mailbox. It sends requests and outcomes through the
application's own gather and broadcast operations; no extra Slurm queries,
listeners, ranks, or background threads are created.

```python
from mpi4py import MPI
from tower.collective_runtime import CollectiveRuntime

control = CollectiveRuntime(MPI.COMM_WORLD, node_local_mailbox, exact_attempt,
                            initial_epoch=restored_parameter_epoch)
control.safe_point("epoch", prepare_update, commit_update)
```

All communicator members call control methods in the same order from the
application's control thread. The coordinator checks the operation sequence,
attempt, parameters, and shared epoch. Every rank first runs a side-effect-free
`prepare_update(command, parameters, request_id)`. If any rank refuses, the
request is rejected everywhere and no commit callback runs. If preparation
succeeds everywhere, `commit_update(staged_value)` applies each local update.
Successful outcomes produce one durable receipt and a shared incremented epoch.

If a commit fails after some ranks change state, all ranks receive an uncertain
outcome and `CollectiveControlError`. Raven cannot roll back arbitrary application
state. The application must stop that algorithm phase and use its own recovery
procedure. The request is never automatically replayed. MPI or process-group
transport failures follow the application's existing failure policy; this API
does not promise recovery from a dead communicator.

The collective `permit`, `renew`, and `release` methods use the same transport.
`release(..., completed=False)` on any rank quarantines the permit instead of
freeing capacity while other ranks might still be writing. Communication itself
has overhead: call controls at suitable existing phase boundaries, not inside
every kernel or tight inner loop.

The optional [`mpi_cooperative.py`](../../examples/runtime/mpi_cooperative.py)
starter uses real mpi4py collectives and a small reduction workload. Its
`--initialize CONFIG` option creates the mailbox on rank zero's host and reports
initialization failure to every rank. Launch it with the site's normal MPI
command inside the user's allocation. The example configuration and attempt
are demonstration identities; replace them with the actual run identity.

The optional [`torch_cooperative.py`](../../examples/runtime/torch_cooperative.py)
starter trains a small actual PyTorch model on CPU or explicitly selected CUDA
devices. It works with an existing `torchrun` launch and DistributedDataParallel.
It respects visible CUDA devices, uses one CPU thread by default, and performs
control exchange at a configurable iteration interval. `TorchDistributedCommunicator`
adapts an initialized PyTorch process group; it never starts rendezvous services.
Set the CUDA device before NCCL object collectives. An existing Gloo control group
can avoid GPU object-collective costs when the application has one.

`TorchMetrics` records a bounded number of CUDA event pairs and calls `query()`
before reading elapsed time. It never calls a device-wide synchronization. It
reports completed GPU stream intervals, allocated/reserved/peak allocator bytes,
pending samples, and skipped samples when its queue is full. These measurements
are not GPU utilization, device-wide memory use, kernel instruction traces, or
a lossless record. CPU mode reports CUDA unavailable and invents no GPU values.
Pass `--run-dir` to publish bounded numeric samples into an existing Raven
Research run with the standard reporting API. Only rank zero publishes that
example's measurements; it does not imply all-rank aggregation.

PyTorch and mpi4py remain optional. Automated validation exercises the collective
algorithm with actual spawned local processes and tests the CUDA adapter with
explicit fake events. Real MPI transports, multiple physical nodes, real CUDA
devices, and NCCL behavior require target-system qualification before production
use. No successful fixture check is presented as a successful GPU test.
