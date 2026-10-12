# Raven implementation roadmap

[README](../README.md) · [Implementation ledger](FLAGSHIP_IMPLEMENTATION.md) · [Project standard](PROJECT_STANDARD.md)

This roadmap records the delivered scope of the thirty original measurement,
submission, and research operation proposals. The phase tables retain the
original proposal IDs and groupings. Each row names a supported mechanism and
its dependencies or limits.

Raven 7.0.0 carries this catalog forward with governed collection, bounded Light
monitoring, and explicit Heavy execution. The [Raven implementation map](raven/implementation.md)
and [execution contracts](raven/execution-contracts.md) define current placement,
admission, and deferred operations. A catalog entry does not establish support
for every application, site, device, or workflow engine.
The phase tables describe each mechanism's original delivery. Managed Raven
sessions require the current execution target and governed handoff; external
campaign drivers, transfers, and live service connectors can remain deferred.

## Release scope and evidence

| Release | Delivered scope | Record |
| --- | --- | --- |
| 4.14.0 | Four measurement and submission additions | [Phase one guide](guides/phase-one.md) |
| 4.15.0 | Operation forms and supported adapters for the other twenty-six proposals | [Operations validation](guides/operations-validation.md) |
| 6.0 | Scoped recording, scientific investigation, cooperative runtime controls, task workspaces, and evidence integration | [6.0 validation](guides/flagship-validation.md) |
| 6.1 | Shared mechanisms and eight primary owning areas for the overlapping proposals | [Consolidation guide](guides/consolidation.md) and [capability map](reference/capabilities.md) |
| Raven 7.0.0 | Light/Heavy launchers, governed collection, execution placement, bounded recovery, migration, and packaged distribution | [7.0 validation](raven/validation.md) |

The [flagship ledger](FLAGSHIP_IMPLEMENTATION.md) tracks forty later investments,
five research hypotheses, ten advanced proposals, and the earlier tracing
proposal. It separates delivered mechanisms from site qualification and research
evaluation. The [archived strategy](research/flagship-strategy.md) describes the
4.16.1 baseline rather than the current release.

## Use the operation catalog

Open **File → Research and cluster operations**, or enter `:ops catalog`.
Read the [complete operation guide](guides/operations.md) for controls, every
feature key, project contracts, and action review. Read the
[validation record](guides/operations-validation.md) for automated checks and
target-system verification requirements.

Raven remains a terminal application with optional adapters. Scheduler actions
require an exact job identity and a reviewable scope. Missing evidence remains
unknown.

## Phase one: measurement and submission confidence

Release 4.14.0 implements four focused additions:

| Proposal | Addition | Release scope |
| --- | --- | --- |
| A03 | Shell-body correctness checks | Explicit local syntax and optional ShellCheck analysis; no script execution |
| A13 | AMD and Intel GPU telemetry | Optional command adapters alongside NVIDIA; preserve unsupported counters as unknown |
| A14 | Scientific array manifests | Explicit array-index mappings with validation and bounded reads |
| A16 | Sampling capability inspector | Separate application reads from upstream collection and show missing configuration evidence |

Read the [phase one guide](guides/phase-one.md) for supported controls and limits.
These four additions remain independent of the 4.15 operation workbench.

## Phase two: prevent avoidable failures

Implemented in 4.15 through the [cluster operations](guides/operations-cluster.md):

| Proposal | Addition | Dependency or important limit |
| --- | --- | --- |
| P01 | Storage readiness cards | Free bytes/inodes and generic/Lustre user, group, or project quota evidence; no directory walks |
| P02 | Edit pending jobs | One supported field per review; live identity/state checks and result readback |
| P03 | Existing array concurrency controls | Real array parents only; preserve Slurm's zero-means-unlimited semantics |
| P04 | Reservation and maintenance timeline | Available Slurm reservation intervals; private reservations and external notices can remain unavailable |
| P05 | Software-license inventory | Distinguish Slurm's inventory from the vendor license server |
| A05 | Slurm installation health checks | Read-only service and connectivity evidence; no automatic service restarts |
| A20 | Recover retained batch scripts | Respect scheduler retention and permissions; never substitute the current local script |
| A19 | Allocation shell and step attachment | Explicit terminal handoff, exact allocation, minimal requested resources, and clean return |

## Phase three: reproducible and scientifically valid runs

Implemented in 4.15 through the [science operations](guides/operations-science.md):

| Proposal | Addition | Dependency or important limit |
| --- | --- | --- |
| A01 | Recreate software environments | Hash-locked Python recreation bundle, optional immutable Apptainer identity, explicit compute-host evidence; no installation by the application |
| A02 | CPU, NUMA, and GPU placement verification | Allocation-process evidence through bounded probes; requested and observed placement remain separate |
| A04 | Scientific acceptance tests | Project-defined tolerances; missing results cannot pass |
| A07 | Statistical comparisons | Independent experimental units, explicit pairing, uncertainty, and small-sample limits |
| A08 | Verified intermediate-result reuse | Verify declared identity, dependency/output hashes, and scientific acceptance before copying to a new directory |
| A15 | Dependency-chain repair | Preserve successful branches; report partial application of non-transactional scheduler changes |

## Phase four: durable campaigns and data movement

Implemented in 4.15 through the [campaign operations](guides/operations-campaigns.md)
and [service operations](guides/operations-services.md):

| Proposal | Addition | Dependency or important limit |
| --- | --- | --- |
| S01 | Checkpoint-aware recovery | Declared complete checkpoint, verified hash and compatibility, explicit reviewed restart; no automatic application checkpoint discovery |
| S03 | Persistent monitoring service | Linux read-only queue/accounting service, single writer, atomic snapshots, reconnect and restart reconciliation |
| S04 | Input staging and verified output return | Declared mounted-file transfers, streamed hashes, no-clobber publication, resumable receipts, keep-source retention |
| A06 | Adaptive parameter searches | Bounded discrete exploration and local improvement with durable trial budgets; infrastructure failures remain distinct |
| A09 | Short-task packing | Declared logical tasks in exclusive Slurm steps, resource budgets, separate logs, durable task receipts |
| A10 | Elastic Dask worker pools | Optional dask-jobqueue controller with owned scaling, queued/connected evidence, persistent lifecycle |
| A11 | Workflow-engine connectors | Native Nextflow trace and Snakemake DAG/runtime observation; the engine retains retry, resume, and cancellation ownership |
| A12 | Heterogeneous allocations | Component resource requests, coupled `srun` launch, reviewed submission; site support required |

Raven retains reviewed workflow execution and durable submission receipts.
These proposals extend their scope. They do not describe those existing
capabilities as missing.

## Phase five: scale and infrastructure diagnosis

Implemented in 4.15 through the [scale diagnostics](guides/operations-scale.md):

| Proposal | Addition | Dependency or important limit |
| --- | --- | --- |
| S02 | Distributed bottleneck explorer | Rank/phase JSON and Darshan POSIX text exports; no native binary profiler attachment |
| S05 | Concurrent multi-cluster workspace | On-demand queue/history snapshots, exact source/attempt identity, bounded readers and isolated failure |
| A17 | Historical infrastructure-incident correlation | Permission-aware node events; an overlap is evidence, not proof of cause |
| A18 | Energy per useful result | Distinguish job, node, and device attribution; include failed attempts in campaign totals |

## Stack tracing: delivered scope in 6.0

The earlier 5.0 proposal has a scoped implementation through the Trace control
in Jobs and History Details. Supported paths include prepared CPython events,
explicit native instrumentation, scoped PMPI collectives and reviewed local Python
snapshots. Results bind to the execution identity and state their coverage.
Incremental collection runs outside the UI thread. An unprepared arbitrary
program cannot supply complete retrospective history; GPU-device stacks and
universal lossless tracing are not implied. Read the
[stack tracing guide](guides/stack-tracing.md) before selecting a capture path.
Current Raven launch and admission requirements are in the
[execution contracts](raven/execution-contracts.md#existing-project-commands).

## Remaining qualification

The released mechanisms have local regression, packaging, and PTY evidence.
The [Raven 7.0 validation record](raven/validation.md) links the retained reports
and states their workload and measurement limits. Its local resource comparison
meets the provisional monitoring and RSS goals; the 1% idle CPU goal remains
unmet.

Real Slurm placement, production SSH, hardware GPUs, multiple nodes, long-running
recovery, and physical terminal chains need target-system qualification.
Independent user studies and scientific outcome comparisons remain research
gates. Use the [qualification procedure](QUALIFICATION.md) and
[site rollout criteria](raven/implementation.md#site-rollout-and-stop-criteria)
to record those results.

## Compatibility and validation gates

Every implementation phase must account for interactions with existing controls:

- Publish immutable or isolated collector results. UI movement must not start
  filesystem reads, scheduler commands, or profiler work.
- Preserve the selected job and execution attempt across source refreshes.
  Results from an earlier selection must not replace the current view.
- Keep collection intervals, display windows, and source production rates
  separate. Faster reads cannot promise faster upstream measurements.
- Reuse shared readers and bounded caches. Invalidate them when identity,
  content, configuration, or adapter selection changes.
- Keep missing, unsupported, inaccessible, stale, and measured-zero values
  distinct. Remote and simulated backends must not silently read local data.
- Check the new controls with mouse capture, keyboard focus, overlays,
  selection, grouping, and Single/Multi worker transitions.
- Test malformed and oversized inputs, empty data, partial writes, source
  disappearance, command failure, stale results, and repeated actions.
- Run focused unit and integration tests, terminal rendering checks, and the
  existing regression suite. Document limits that require real hardware or a
  site Slurm installation.

Proposal IDs `S01`–`S05` and `P01`–`P05` identify the earlier substantial and
practical lists. `A01`–`A20` identify the twenty additional proposals. They are
tracking labels, not new commands or configuration keys.
