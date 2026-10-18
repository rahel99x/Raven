# Raven execution contracts and compatibility

Raven assigns each operation to the interface, a reviewed scheduler target, or
an admitted Heavy worker. Light does not run a Heavy algorithm when a worker is
missing, expires, disconnects, changes identity, or rejects the request. It
returns a deferred result and a nonzero command exit code.

The [generated capability ledger](capability-ledger.json) covers 57 operation
keys, 56 original proposal entries, and 44 named worker adapters. It specifies
dispatch and resource contracts; the [validation record](validation.md) identifies
the exercised paths and remaining site checks. Actual Light read limits depend
on the reader and background admission. The enforced limits below take precedence
over the ledger's generic baseline allowance.

## Boundaries

| Work | Execution and limits |
| --- | --- |
| Queue, selected details, scheduler actions | Existing governed collection owner. Exact-attempt review remains mandatory. Heavy does not increase scheduler quotas. |
| Small log search and selected copy/export | Interface background lane; at most 2 MiB of captured sources and 128 files per task. Complete bytes are copied or failure is reported. |
| Large local log search/export | Named Heavy tasks; at most 8 MiB aggregate source bytes, or the worker's lower admitted limit. Search uses at most 32 sources; export uses at most 128. |
| Trace viewing in Light | At most 512 events and 256 KiB per refresh. A streaming reader advances through committed windows; incomplete coverage remains visible. |
| Basic Quick Advisor | At most 512 series samples, a 256 KiB persisted tail, and 512 historical jobs. |
| Scientific analysis and planning | Fixed named worker adapters. No arbitrary Python function dispatch. |
| Ordinary terminal typing/paste | Existing shell destination and normal user authority. Light/Heavy never redirects that input. |
| Generated terminal helpers | Require a declared adapter. Arbitrary generated recipes/broadcasts defer; bounded local shell hooks remain available. |

Each worker adapter has version 1, explicit target, CPU/memory/scratch/time and
record/byte limits, cancellation semantics, result coverage, and immutable input
and context digests. Results contain finite JSON and are capped at 512 KiB.
Inline request framing has its own limit; the 8 MiB file allowance does not permit
an 8 MiB inline message. Existing stable-file checks and no-clobber publication
rules remain active.

The router intersects each task budget with the worker's admitted per-task share.
It acknowledges validated committed results. Mutation cancellation, lost replies,
and process failures are uncertain until their receipts are reconciled. Raven
does not replay them automatically.

## Supported worker adapters

Pure adapters cover statistical comparisons, acceptance, numerical stability,
regression investigation, recommendations, investigation planning, scientific
lineage, causal and collective analysis, execution simulation, diagnostic recipes,
information queries, fields, and measurement planning. File adapters use the
existing scientific, campaign, service, platform, energy, and investigation
readers. They receive explicit artifact paths and captured identity, not a new
scheduler transport. An adapter that needs live evidence fails visibly when that
evidence is unavailable.

Reviewed artifact publication supports `reuse`, `research-portable`,
`support-bundle`, `execution-simulation`, and complete log bundles. Log clipboard
delivery occurs on the original interface after the worker publishes a complete
bundle. Missing clipboard tools or an inaccessible worker output path preserve
the exported files and return a delivery warning.

## Existing project commands

Bounded `report init/start/finish/metric/research/log/artifact/vendor-client` commands retain their
argument and output contracts. `report submit` does not require Heavy. It uses
the same governed scheduler ledger, original exact `sbatch` arguments and
environment, and persistent submission intent. An uncertain submission is never
silently retried. The current reporting submission bridge requires a local Slurm
connection; remote filesystem and environment mapping is not inferred.

`report exec`, `report passport`, `report aggregate`, `passport capture`, and
Python/native trace launch may run inside an already owned Slurm job. The bridge
requires fresh governed job and step records, matching user and host, and actual
local process membership in the job/step cgroup. Batch and multinode membership
is supported for this instrumentation purpose. This does not grant a Heavy worker
budget or certify capture of every rank/thread/GPU. Environment variables alone
do not authorize a launch. If proof is unavailable, Raven returns the prepared
invocation and required job-local target without starting the program.

`trace inspect` remains a bounded Light viewer. `trace register`,
`trace native --sdk-header`, and `trace mpi --source` remain available without
compute. Child arguments such as `--help` after a program name or `--` cannot
bypass execution admission.

Standalone reporting/scientific library APIs outside a configured managed Raven
runtime retain their existing behavior. This boundary supports project-owned
instrumentation libraries. Raven's execution policy is not an operating-system
sandbox for arbitrary Python programs or user shell commands.

`raven launch --trace` attaches Python recording inside the launched workload,
using its existing attempt and execution authority. It does not allocate another
worker or authorize attaching to unrelated processes. CPython 3.12+ monitoring
is preferred; Python 3.10/3.11 profile semantics require explicit consent.
Project scope is the default, helpers have separate process coverage, and an
explicit `raven_exec` override remains available. Environment propagation and
startup exclusions are declared gaps, not an inferred global capture guarantee.
Native instructions, GPU-device stacks and retrospective history remain outside
this automatic Python path. See [stack tracing](../guides/stack-tracing.md).

The optional Python metrics client writes bounded, coalesced numeric observations
for the current run. Its sidecar describes metric kinds, sampling and rank policy;
it cannot grant scheduler authority. Explicit reductions reuse an initialized
application communicator and are collectives at the application call site.
`report vendor-client` copies only the standalone client, with an API version and
content hash, preserving unrelated project configuration and refusing modified
copies unless `--force` is explicitly selected.

## Explicit deferred paths

The ledger's `deferred_paths` field is the authoritative list. The following
paths require an additional transport, adapter, or verified execution target:

- Live multi-cluster collection, starting a detached supervisor, or querying a
  site telemetry service without a governed source adapter.
- Campaign submissions, external diagnostic/reproducer/regression drivers,
  plugin execution, environment bundle publication, or staging transfers without
  their reviewed worker-to-target handoff. Read-only artifact analysis remains
  available through named adapters.
- Remote artifacts without an explicit worker-visible path mapping.
- Job-local program launch on a desktop/interface host without verified Slurm
  placement. Desktop Heavy scientific analysis is supported separately.
- Absolute log line/time seeks above 2 MiB. Use bounded byte/percent windows or
  Heavy search. Direct whole-file copy above 2 MiB reports its limit; explicitly
  select Heavy bundle export to copy a larger supported source.

## Validation scope

Tests execute actual local worker subprocesses for scientific comparisons,
artifact validation, simulation review/publication, and complete large log
bundles with interface clipboard handoff. Regression tests cover missing workers,
identity changes, cancellation, finite JSON limits, source budgets, mutation
replay, managed programmatic entry points, generated helper gates, copied file
identity round trips, and selected-copy preservation. Controlled placement
fixtures test batch/multinode proof and environment-only rejection. These tests
do not qualify a real Slurm site or a live cluster filesystem.
