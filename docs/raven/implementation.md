# Raven implementation and qualification

This record maps the supplied [implementation handout](../design/project-raven-implementation-handout.md)
to release 7.0.0. It separates implemented behavior, local evidence, and site
qualification. It does not treat a working menu or a passing mock as proof of
production readiness.

## Ownership

```text
Light interface
  +-- shared collection owner --> private host-local ledger/cache --> Slurm reads
  +-- allocation manager ------> isolated lifetime helper
  |                                +-- explicit desktop process
  |                                `-- owned salloc / exact srun step
  |                                      `-- verified Heavy worker --> bounded tasks
  `-- terminal service --------> PTY broker --> explicitly selected shell sessions
```

The Heavy worker receives bounded named operations and data. It does not own a
second dashboard sampler. The original interface continues to own navigation,
copy/selection, input generations, collection subscriptions, and terminal focus.
Each result binds its session, generation, policy, context, and worker identity.
Cancellation is idempotent; a lost acknowledgement does not justify repeating a
scheduler mutation. Uncertain allocation cleanup remains visible.

## Requirement traceability

| Requirement | Implemented owner | Evidence and boundary |
| --- | --- | --- |
| R01 Brand | Metadata, README, toolbar/views, launchers | Public Raven labels; distribution/import/schema compatibility retained |
| R02 Launchers | `raven_launch.py`, source launchers | Light starts without allocation; Heavy plan/help/version are non-mutating |
| R03 Useful bounded Light | `raven_collection.py`, `raven_limits.py`, existing views | Explicit retention and admission tests; measured resource report required for numerical CPU/RSS claims |
| R04 Allocated Heavy | `raven_allocation*`, `raven_resources.py`, `raven_worker*` | Real local tasks and synthetic Slurm faults; real Slurm placement remains untested here |
| R05 Allocation controls | `raven_ui.py`, toolbar, interaction graph | Request/review/cancel/release, stale actions, modal/focus preservation, reuse receipt tests |
| R06 Lightweight wait | `raven_launch.py`, isolated helper | Dashboard import follows ready admission; help/version/plan do not wait or allocate |
| R07 Configurable sites | `raven_config.py`, structured adapters | No CARC-specific defaults; local/SSH argv and policy tests; every real site needs qualification |
| R08 Standalone desktop | Explicit desktop profile and process adapter | No Slurm inference; real fresh-process task/cancel/drain tests |
| R09 Separate budgets | Collection ledger and execution router | Heavy readiness does not change collection floors or reset request records |
| R10 Honest cadence | `refresh_rate.py`, `metric_sampling.py`, Sampler | Slow configured bases survive neutral controls; collection floors remain authoritative |
| R11 No automatic amplification | Sampler, managed Slurm methods | GPU step/weather sources disabled; declared existing telemetry remains readable |
| R12 Sharing scope | Existing acquisition and private host-local cache | Same-user/host/connection tests; no cross-user or cluster-wide sharing claim |
| R13 Bounded recovery | Ledger, source scheduler | Rolling quotas, jitter, idle demand, burst limits, circuits, refresh floods, reserved validation lane |
| R14 Ownership/expiry | Allocation helper, private receipt/lock, reuse review | Cancel/grant race, duplicate launch, wrong owner/attempt, stale receipt, idle/time limit, uncertain cleanup tests |
| R15 Retention | Limits, Store, logs, archives, native/raster caches | Byte/count/queue bounds and explicit partial coverage; original evidence preserved |
| R16 Input safety | Existing input/session generations and shared header fix | Narrow panes, hover cleanup, stale release, allocation transitions, PTY tests |
| R17 Workflow placement | `raven_execution.py`, Research/Operations/planning/trace/log gates | Named handler ledger; bounded Light readers; no managed local Heavy fallback |
| R18 Migration | `raven_migration.py`, config compatibility, UI import | Plan/hash/journal/resume/rollback; only verified migration activates safe preferences; exact scope required for group identities |
| R19 Scale evidence | `raven_qualification.py`, load and process-tree harnesses | Actual ledger with synthetic distinct users; production RPCs and administrator-approved cohorts remain untested |
| R20 Distribution | One wheel, three commands, schemas/examples, CI and runbooks | Offline install and source/wheel checks; old evidence IDs unchanged |

## Advanced workflow contracts

The existing operation catalog remains the interface. The execution ledger maps
its scientific, planning, investigation, provenance, log, trace, and adapter
classes to an explicit target, side-effect class, input/output bounds, cancellation,
and result contract. The offline qualification report includes this ledger.

Retained views, small summaries, exact action review, and bounded file operations
can run in Light. Scientific and large read analyses use admitted named handlers.
Scheduler mutations keep their existing fresh-review and exact-attempt path;
they are never cached, replayed, or converted into opaque worker commands.

An external experiment, debugger, instrumentation transport, vendor GPU collector,
or site service still needs its declared adapter and authority. A prepared plan
or deferred result is not counted as a completed measurement. Work that requires
an unavailable target reports that reason instead of running on a login host.
The worker accepts JSON data, not arbitrary imported modules, Python pickles, or
user-supplied executable handlers.

## Explicit unsupported cases

- A production site-operated observation service is optional in the handout and
  is not shipped. Its configuration is rejected, not treated as direct-query
  permission. Standalone private host-local coordination is implemented.
- The portable adapter cannot claim hard aggregate cgroup enforcement. Policies
  that require it fail before starting a worker. Portable RLIMIT/process-group
  limits and their weaker aggregate guarantees are disclosed.
- Full-interface transfer to a compute node is not provided. The supported model
  keeps the Light interface and starts a verified Heavy worker.
- Generic reuse rejects ambiguous array/heterogeneous identities, GPU reuse, and
  unknown per-node grants. It never adds `--overlap` to force coexistence. Sites
  with different placement/accounting semantics need their own qualified adapter.
- Arbitrary in-process plugins are not loaded by managed launchers. A plugin needs
  declared execution and collection contracts before it can enter this profile.
- A configuration change is validated for the next launch. Runtime connection
  retargeting is not implicit. Close the session and select the new profile.

These are deliberate exposed boundaries, not evidence of a completed site-wide
rollout. R04, R07, R19, and the site part of R20 require external qualification.

## Validation scope

The [release validation record](validation.md) contains observed results and
links to the machine-readable evidence.

Focused tests exercise the shared header, configuration restrictions, legacy
aliases, native worker processes, allocation helpers, reuse, source admission,
retention, migration, and workflow routes. The existing full regression suite
continues to cover mouse framing, graph gestures, job selection/groups, paste,
copy mode, terminals, and scheduler action identity.

The offline cohort harness uses the actual ledger with separate synthetic user
identities and private databases. It includes two sessions per user, synchronized
startup, refresh floods, failed producers, and recovery. It does not contact a
controller. At 200 users, one queue query per minute is approximately 3.33 queries
per second before other query families and server-side fan-out. This arithmetic
and bounded CLI attempts do not prove an acceptable Slurm RPC load.

The resource harness measures a real curses PTY and its local descendants against
the retained 6.3.0 source baseline. It records CPU, summed RSS, PSS where available,
threads, processes, descriptors, terminal output, and emitted fake collection
commands. A five-minute fixture is a local comparison; it is not a multi-hour
soak, an SSH benchmark, or a site capacity test.

## Site rollout and stop criteria

Before a pilot, record the exact release, configuration/policy digest, Slurm
version, connection/placement adapter, compute Python, and scheduler authority.
Agree on numerical stop thresholds with the administrators for:

1. Incremental controller and accounting traffic, including actual RPCs.
2. Query, submission, and cancellation latency and error rates.
3. Login-node CPU/RSS headroom and filesystem metadata/write pressure.
4. Queue size, payload sizes, outstanding work, and cleanup uncertainty.

Test one disposable owned allocation, then approved cohorts of 1, 10, 50, and
200 or more as the site's thresholds allow. Never manufacture controller outages
or run an unsolicited load test on a shared login node. Preserve an independently
measured baseline, staged results, and rollback procedure.
