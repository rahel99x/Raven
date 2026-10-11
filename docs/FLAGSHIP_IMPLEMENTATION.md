# Flagship implementation and qualification ledger

This file maps the [research dossier](research/flagship-strategy.md) to executable
mechanisms, controls and remaining qualification work. The dossier is an archived
proposal against version 4.16.1; its statements about future work describe that
baseline. This ledger describes the current implementation.

**Implemented** means a supported mechanism exists. **Partial** means the
mechanism covers only the stated subset. **External gate** means it requires
real site, user or research evidence that cannot be produced by an offline test.
An implementation does not establish production certification or novelty.

## Open the relevant task

Use `:workbench run|investigate|compare|results|connect|all`, or the task controls
in Research and cluster operations. Select a feature and fill its declared
fields. **Inspect / prepare** produces a report. Mutations require
**Review action → Confirm apply** and preserve a receipt or explicit failure.

Jobs and History Details retain their existing inspectors, logs, analytics and
research views. The Trace control inspects prepared recordings and supported
capture paths; it does not silently attach an arbitrary debugger.

## Forty investments

| ID | Scope delivered | Remaining boundary or validation |
| --- | --- | --- |
| 01 Task workspaces | Shared task descriptors filter the searchable operation catalog; durable pinned tools have keyboard/mouse controls and reopen with current job context. | **External gate:** newcomer task study; no measured usability improvement claimed. |
| 02 Context/navigation | Back/Forward and scoped saved locations remain available. Cached breadcrumbs show known cluster, matched project/run attempt and declared source; changed scheduler attempts suppress stale bindings. | Session back stacks remain transient; narrow screens omit optional context before hiding the current page/job. |
| 03 Command model | Task descriptors and operation specifications drive task controls and forms; existing commands remain available. | **Partial:** legacy keyboard/menu definitions are not all generated from one registry. |
| 04 Project guide | `:projectguide ROOT` / `project-guide` checks declaration coverage and one exact sample's metrics, summary, output contents and bounded logs through confined local/SSH readers; executable Bash hooks and reviewed descriptor initialization. | Sample coverage is bounded and source-specific; native Research analyses still establish scientific applicability. |
| 05 Investigation timeline | `investigation-timeline` verifies the selected attempt and joins supported scheduler events, bounded logs and registered trace evidence; explicit timeline manifests remain supported. | Original text, source order and unknown clock domains remain distinct; no automatic universal log parser or cross-clock causal order. |
| 06 Semantic inspection | Evidence records retain units, source and coverage; existing raw/trend/range chart distinctions; `scientific-field` provides bounded field queries. | Full arbitrary-volume scientific visualization remains outside the terminal core. |
| 07 Accessibility | Existing keyboard traversal, ASCII, reader mode, reduced motion, focus, resizable panes and selection remain available. | **External gate:** real emulator, multiplexer, font and assistive-technology qualification. |
| 08 Gesture state | Shared typed high-priority capture routing and fresh-gesture reset in native/direct paths; regressions cover stale releases into the toolbar. | **Partial:** retain established per-control state machines; no risky replacement of the byte decoder. |
| 09 Immutable publication | Render snapshots reuse revisioned immutable CPU/GPU history tuples. Corrections to interior samples invalidate publication. | **Partial:** unrelated full document composition and other Store fields are not universally incremental. |
| 10 Virtual documents | Existing visible-row/plot rendering, bounded indexed selection and source caches remain in use. | More pane-level invalidation is a future measured optimization; no unbounded document promise. |
| 11 Work admission | Nested cluster readers use the shared scheduler and cooperative child gathering; settings use one coalesced background writer. | Adapter/service processes retain their explicit resource contracts; not every external tool obeys Python worker counts. |
| 12 Latency budgets | Existing bounded UI timings, p95/p99, sustained-input and changing-UI harnesses; new snapshot comparison and qualification controls. | **External gate:** release-target performance on actual terminals and SSH; targets are not results. |
| 13 State/reconnect | Atomic versioned/checksummed UI state, last-good recovery, older-state compatibility, future-schema preservation and final background-save drain. | Filesystem/node failure can still lose uncommitted state; external transports have separate replay contracts. |
| 14 Incident reproduction | Existing replay, input traces, source fixtures and sequential input regressions; bounded support evidence and qualification reports. | **Partial:** no deterministic recording of every OS scheduling event or arbitrary external application. |
| 15 Evidence records | `evidence` reads versioned identity, provenance, clock, unit, coverage and integrity records. | A valid schema checks supplied evidence; it cannot prove a producer measured correctly. |
| 16 Capability manifests | Adapter/source capabilities and site policy admission distinguish unsupported and unavailable observations. | Claims must be qualified against the installed runtime, build and site. |
| 17 Instrumentation federation | `diagnostic-recipe` checks explicit tool capabilities and conflicts. `tower trace mpi --source` emits a scoped C PMPI wrapper for supported world-communicator collectives. | **Partial:** no universal Caliper/OMPT/Kokkos/GPU injector. PMPI supports its declared C signatures/rank limit; unsupported operations produce gaps. |
| 18 Adapter SDK | `adapter` inspects pinned external manifests; reviewed describe/validate/run use bounded NDJSON, deadlines and process-group cleanup. | Installed code remains trusted; isolation is not a security sandbox. |
| 19 Site telemetry | `site-telemetry` inspects or explicitly queries Prometheus range data with exact cluster/job/attempt labels, bounded HTTP responses and optional site policy/shared cache. | **External gate:** target-site authentication, deployment and label mappings; no blanket Jobstats, ClusterCockpit, LLview or slurmrestd client certification. |
| 20 Portable records | `research-portable` imports supported Perfetto JSON, Caliper JSON, OTLP JSON spans and literal-key Workflow Run RO-Crate; reviewed bundle export. | Binary Perfetto/protobuf, arbitrary Caliper formats and general JSON-LD expansion are not implied. |
| 21 Shared queries/API | Versioned structured commands and a bounded subscription/admission broker with exact request identity and freshness. | A user-scoped broker is not an institution-wide authenticated monitoring service. |
| 22 Causal/protocol diagnosis | `causal-delay` and `collective-audit` analyze declared trace relations and communicator evidence. | **Partial:** abstain on loss, missing ranks or unsupported callbacks; no whole-program proof of causality. |
| 23 Reproducers/regressions | Reviewed `diagnostic-campaign`, `minimize-reproducer` and `regression-sweep`; immutable Git snapshots run measured repeats with scientific checks; `regression-investigation` also evaluates supplied measurements. | Line-deletion minimization needs an explicit repeated failure oracle; no universal input/rank/language minimizer or automatic attribution to one changed line. |
| 24 Scientific stability | `numerical-stability`, declared scientific checks, existing acceptance and measured numerical demo. | No invented scientific threshold; no universal numerical-error bound or automatic perturbation of arbitrary binaries. |
| 25 Counterfactual experiments | `execution-simulation` checks declared calibration and held-out acceptance, withdraws failed predictions and can publish reviewed immutable model/scenario/prediction records. | **Research gate:** limited fixed-workload model; missing policy/evidence stays unqualified. No arbitrary-program or scheduler prediction. |
| 26 Investigation planning | `investigation-plan` chooses declared information actions under explicit cost/risk budgets. | Deterministic greedy planning over supplied models; not a learned optimal Bayesian investigator. |
| 27 Claim impact | `scientific-impact` propagates declared lineage changes and prepares conservative revalidation checks. | Missing lineage remains uncertain; proposed checks do not prove a scientific claim. |
| 28 Cooperative controls | `runtime-steering`, `io-coordination`, `scientific-field`, and `tower runtime` expose safe points and bounded summaries. `CollectiveRuntime` uses application-owned gather/broadcast for shared validation, commit epochs and outcomes. | Private leader-local mailbox; existing MPI/PyTorch process group is required. Partial commits become uncertain, not rolled back or automatically retried. |
| 29 Site policy | `site-policy` evaluates explicit allowlists and collection limits; deployment guide states responsibilities. | Cooperative policy is not OS-enforced privilege isolation. |
| 30 Observability accounting | Request admission exposes source limits, delivery capability, duration/storage/resource budgets and refusal reasons. | Estimated cost requires target-system measurement; strict completeness is never inferred from interval alone. |
| 31 Action lifecycle | Existing reviewed plans plus new adapters use exact scope, content/identity revalidation and explicit outcomes. | Multi-step scheduler/filesystem effects are not a distributed transaction; uncertain outcomes require reconciliation. |
| 32 Support records | `support-bundle` prepares selected structured, redacted reports in a new reviewed directory. | **Partial:** no unauthenticated shared session, universal redactor or automatic upload. Review before sharing. |
| 33 Distribution | Offline setup, clean-install checks, per-artifact/member SHA256 inventory, core license/dependency/build metadata and minimal SPDX 2.3 record with verification. | Hashes detect changes, not publisher authenticity. Signing and separately installed external runtime/tool inventories remain release/site responsibilities. |
| 34 Qualification kit | `scripts/qualify_flagship.py` checks docs, CLI, ten pages, real PTY and optional wheel; explicit existing-allocation manifest adds one read-only identity query. | **External gate:** real GPU, multi-node, production SSH and site policy tests remain explicit untested entries. |
| 35 Task docs | Short task-based README, runbook, specialized guides, administrator/support/qualification documentation, this ledger and generated operation fields/defaults. | Schema and control changes require reference regeneration and compatibility review. |
| 36 Starter kits | Bash/Python reporting, numerical/Git-regression demos, Python/native and scoped PMPI trace launch, optional MPI reduction and PyTorch training controls. Two local Open MPI ranks were exercised for the supported trace wrapper. | **External gate:** multi-node/PyTorch/CUDA/NCCL and site qualification; CUDA metric tests use fake events and do not certify hardware sampling. |
| 37 Reliability corpus | Existing public source/input/performance fixtures plus persistence, nested-budget, partial trace, adapter and diagnostic-process regressions. | **External gate:** publish target-system tail latency, memory/RPC cost and real workload outcomes. |
| 38 Support policy | Documented schema/API boundaries, deprecation/fixture expectations and separate trusted-plugin contract. | Maintainer capacity and long-term compatibility depend on actual maintained releases. |
| 39 External adoption | Qualification procedure and five-user-journey evaluation protocol are provided. | **External gate:** independent labs/operators have not been recruited or evaluated by this implementation. |
| 40 Outcome evaluation | Research hypotheses retain explicit baselines, assumptions, negative-result and evaluation requirements in the dossier. | **External gate:** no scientific study, superiority result or world-first claim has been established. |

## Five research hypotheses

| ID | Implemented mechanism and control | Qualification still required |
| --- | --- | --- |
| R1 Checkable, expiring recommendations | `recommendation` / `tower.recommendation-review/v1` checks supplied identity, evidence, applicability, expiration and acceptance. | Compare incorrect endorsement and resource cost with expert/rule baselines; a predicate checker is not a proof of universal validity. |
| R2 Choose the next information action | `investigation-plan` / `tower.investigation-plan/v1` ranks declared actions within budgets. | Evaluate information utility, calibration, noise and regret. Current planner is deterministic greedy, not optimal learning. |
| R3 Conservative revalidation | `scientific-impact` / `tower.scientific-lineage/v1` traverses declared dependencies and proposes checks. | Compare against full recomputation with seeded parser/input/policy changes; no unjustified minimality claim. |
| R4 Compile diagnostic intent | `diagnostic-recipe` checks `tower.diagnostic-intent/v1` against supplied site capabilities. | Execute reviewed recipes on independently configured sites; portability of JSON alone is not diagnostic equivalence. |
| R5 Query-preserving summaries | `information-query` checks supported declared summaries and answers against their contract. | Validate query families against complete reference data and rare events; cannot answer arbitrary discarded-history queries. |

## Trace delivery contract

The earlier ten proposals map to these delivered operation families:

| Earlier proposal | Control | Supported scope |
| --- | --- | --- |
| Causal explorer | `causal-delay` | Declared trace dependency/phase relations, with evidence gaps preserved |
| Collective auditor | `collective-audit` | Supported declared communicator/rank/sequence records; incomplete evidence remains incomplete |
| Execution simulator | `execution-simulation` | Declared fixed-workload DAG/resource model with calibration and held-out checks |
| Numerical robustness | `numerical-stability` | Supplied repeated results and declared scientific tolerances |
| Minimal reproducer | `minimize-reproducer` | Reviewed line-deletion trials with an explicit repeated failure oracle |
| Performance regression | `regression-investigation`, `regression-sweep` | Imported measurements or reviewed immutable Git executions with repeated trials, uncertainty and scientific checks |
| I/O coordination | `io-coordination` | Cooperative bounded permits at declared application safe points |
| Live steering | `runtime-steering` | Application-declared parameters, safe points, epochs, deadlines and outcomes |
| Lineage impact | `scientific-impact` | Conservative impact propagation and revalidation over declared lineage |
| Observability planner | `investigation-plan` | Budgeted selection among declared configuration, repeat, trace and validation actions |

These are bounded implementations of the proposals. Their existence does not
establish unrestricted language/hardware support, optimum planning, or measured
scientific benefit across real clusters.

The recording pipeline is **prepared, scoped instrumentation**, not a universal
lossless tracer. Preferred Python recording uses CPython 3.12+ monitoring events.
An explicit older-Python profile fallback has weaker event semantics. Native
recording uses explicit instrumentation and private inherited local channels.

Journals include sequence/integrity and exact process/attempt metadata.
Incremental readers remain outside rendering. Budget exhaustion ends recording
and marks it partial. A crash can leave an uncommitted tail. There is no
per-event durable acknowledgement guarantee, automatic global MPI launch,
GPU-device stack recording, or retrospective history from an unprepared program.
The reviewed Python snapshot path verifies supported local allocation/process
identity and is a non-atomic snapshot, not complete execution history.

Read [stack tracing](guides/stack-tracing.md),
[scientific investigation](guides/scientific-investigation.md), and
[runtime research](guides/runtime-research.md) for exact setup and controls.

## Validation evidence and limits

Foundation checks completed during development: 822 combined existing/new tests;
476 focused pointer, fragmented-input and sequential-control tests; and 88
asynchronous persistence/worker-switch checks. These sets overlap and must not
be added as a unique test count. Release-wide results and measured limitations
are in the [6.0 validation record](guides/flagship-validation.md).

A snapshot-only microbenchmark used 2,000 histories with 600 values each and 120
calls on the same shared host. Mutable history copying took a 12.436 ms median;
cached immutable render histories took 0.333 ms. Maxima were 25.197 ms and 1.370
ms. This isolates one data-publication stage; it is not an end-to-end terminal
latency claim, a measured reduction of every lag source, or a universal speedup.

The cloud workspace has no production Slurm controller, hardware GPU allocation,
multi-node workload or production SSH endpoint. Offline tests use controlled
responses and real local files/processes/PTYs where stated. Use
[qualification](QUALIFICATION.md) to retain the untested target-system and
external evaluation gates.
