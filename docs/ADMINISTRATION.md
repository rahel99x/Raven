# Raven site administration

Raven is a user-space terminal client. The default installation does not install
Slurm, change services, request elevated privileges, or expose a network server.
Optional collectors and reviewed operations require separate site permission.
Use this guide to define deployment policy, qualify the installation, and manage
private operational records. Complete the [installation procedure](INSTALLATION.md)
before starting a site pilot.

## Start with a read-only pilot

1. Select a supported Python and Slurm environment through your normal modules.
2. Install a reviewed source release or a locally built wheel. Keep its hash.
3. Start with the conservative example profile and disabled optional collectors.
4. Run `raven --doctor`, then the [qualification script](QUALIFICATION.md).
5. Check a small existing allocation, accounting delay, logs, and terminal behavior.
6. Enable one optional collector or operation at a time and record its cost.

Keep site hostnames, account/QOS choices, partitions, and credentials out of the
public example configuration. Distribute your profile through the site's normal
configuration mechanism. Raven honors existing SSH host-key verification and
authentication; it does not bypass them.

## Define policy and collection budgets

The [platform starter guide](../examples/platform/README.md) provides exact
site policy, Prometheus, adapter, portable evidence and support-bundle examples.
The [generated operation reference](reference/operations.md) lists their form
fields and defaults.

The `site-policy` operation evaluates a `raven.collection-policy/v1` document against
declared source capabilities and a collection request. Policy covers source and
operation allowlists, collection/resource limits, allocation requirements, and
intrusive-tool permission. It produces a reason for admission or refusal.

Open `:ops site-policy` to inspect a policy. Read the schema and operation's
fields before preparing it; a policy file is a contract for cooperating Raven
components. It is not an operating-system authorization mechanism. Use Slurm,
filesystem permissions, process ownership, cgroups and your institution's
controls for enforcement outside Raven.

Core source polling has independent collection limits and retry backoff. GPU
probes, a trace recorder, a live runtime mailbox, and a scheduler query have
different costs. A short display window does not authorize more polling.
Use exact allocation and device identity and check effective intervals in the
interface. Do not infer ownership from a GPU ordinal or a shared node alone.

The shared worker scheduler admits native nested cluster collectors and drains
accepted work when changing Single/Multi mode. Long-lived instrumentation and
application safe points have their own declared transport and storage contracts;
they must not be presented as cost-free because the UI is idle.

## Optional tools and private records

External adapters are explicit installed programs. Pin their executable/assets
and supported version, environment, declared capabilities, request deadline,
output budget and cancellation behavior. Keep trusted in-process Python plugins
separate from external adapters. Neither a Python environment nor a subprocess
is a sandbox for hostile code.

Recording application events can expose function names, paths and runtime
structure. Support bundles include only selected reports and require review.
Treat retained traces, logs and provenance as project data. Apply the site's
retention and access rules; do not publish a recorder or mailbox endpoint by
default.

Runtime-control mailboxes use private, same-host node-local SQLite. Do not place
them on shared network storage. A multi-node application must use its existing
coordinator and collective protocol to distribute a decision safely. Raven does
not manufacture a distributed barrier or inject a parameter update into an
unprepared process.

## Reviewed changes

Use the sequence **Inspect / prepare → Review action → Confirm apply → verify the result**.
Check job attempt, cluster, owner, files and exact command. Reopening a stale
review must require fresh evidence. A partial or uncertain result is not success
and is not permission to repeat a submission.

Do not assume local table order is scheduler order. Queue-order changes use
Slurm's native user-top capability only where enabled. Dependencies, resource
availability, reservations, QOS and scheduling policy still determine starts.

## Publish a site qualification record

Record the release and source hash, Slurm/Python/runtime versions, terminal,
device model/driver, adapter versions, effective policy, test allocation,
measurement limits, failed checks and untested capabilities. An offline suite
does not qualify a site. Establish incident reproduction and rollback procedures
before recommending the installation broadly.

The [support policy](SUPPORT_POLICY.md) defines format boundaries and bug-report
requirements. The [implementation ledger](FLAGSHIP_IMPLEMENTATION.md) identifies
experimental capabilities and external validation still required.
