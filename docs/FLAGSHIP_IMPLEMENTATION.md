# Raven flagship implementation and qualification ledger

This ledger maps the [archived research strategy](research/flagship-strategy.md)
to mechanisms, controls, evaluation procedures, and remaining qualification work
in Raven 7.0.0. The strategy proposed changes against version 4.16.1. Its future
release language describes that historical baseline.

The [Raven implementation map](raven/implementation.md) and
[execution contracts](raven/execution-contracts.md) define current Light/Heavy
placement, admission, and deferred operations. The
[7.0 validation record](raven/validation.md) links release evidence. Use the
[6.0 validation record](guides/flagship-validation.md) for the earlier foundation
measurements.

| Status | Meaning |
| --- | --- |
| Supported mechanism | An implementation exists within the stated scope. |
| Partial | The mechanism delivers only the stated subset of the proposal. |
| External gate | Qualification requires a real site, device, terminal chain, or independent users. |
| Research gate | The claimed scientific or decision benefit still requires an evaluated comparison. |

Rows 39 and 40 provide evaluation procedures. They do not report completed user
studies or research results. Mechanism delivery and qualification remain separate
claims throughout the tables.

The [6.1 consolidation](guides/consolidation.md) assigns these overlapping
requirements to [eight owning areas](reference/capabilities.md) and shares
publication, collection, presentation, investigation, trial, and action
mechanisms. The 56 entries comprise forty investments, five research hypotheses,
ten advanced proposals, and one tracing proposal. Each has one primary owner;
related entries identify overlap. This ownership map preserves the supported
subsets and qualification gates below.

## Managed Raven execution

The tables retain the delivery scope of inherited mechanisms and project-owned
standalone tools. In a managed Raven session, the current execution contracts
determine which path can run:

| Work | Current execution requirement |
| --- | --- |
| Scientific analysis and planning | An admitted named Heavy adapter with bounded inputs and results |
| Scheduler mutations | The existing exact-attempt review and governed scheduler target |
| External diagnostic, reproducer, and regression drivers; campaign submissions; transfers; plugin execution | A declared owned execution adapter and reviewed target handoff; the managed path remains deferred when that handoff is absent |
| Live multi-cluster collection, site telemetry queries, or detached collectors | A governed source adapter or collection owner |
| Python/native program recording | Verified owned Slurm job/step placement for managed launch; bounded viewing and SDK generation remain available without compute |

The [deferred-path record](raven/execution-contracts.md#explicit-deferred-paths)
states the remaining handoffs. Preparing a plan or inspecting an artifact does
not mean that its external action or collection ran.

## Use the task controls

1. Open a task control in Research and cluster operations, or enter
   `:workbench investigate`. Other workspace choices are `run`, `compare`,
   `results`, `connect`, `pinned`, and `all`.
2. Select a feature and fill its declared fields.
3. Select **Inspect / prepare** to produce a report.
4. If the operation changes a job, file, service, or application state, use
   **Review action → Confirm apply**. The operation preserves a receipt or an
   explicit failure.

Jobs and History Details retain their existing inspectors, logs, analytics and
research views. The Trace control inspects prepared recordings and supported
capture paths; it does not silently attach an arbitrary debugger.

## Forty investments

| ID | Mechanism or procedure delivered | Remaining boundary or validation |
| --- | --- | --- |
| 01 Task workspaces | Shared task descriptors filter the searchable operation catalog; durable pinned tools have keyboard/mouse controls and reopen with current job context. | **External gate:** newcomer task study; task completion and usability effects remain unevaluated. |
| 02 Context/navigation | Back/Forward and scoped saved locations remain available. Cached breadcrumbs show known cluster, matched project/run attempt and declared source; changed scheduler attempts suppress stale bindings. | Session back stacks remain transient; narrow screens omit optional context before hiding the current page/job. |
| 03 Command model | Shared command descriptors supply command names, usage, palette search/completion, toolbar action metadata, task controls and generated command help; installed operation specifications supply forms and field choices. | **Partial:** contextual input handlers, raw-key dispatch and specialist controls remain module-owned with their own validation; trusted plugins keep their separate dispatch contract. |
| 04 Project guide | `:projectguide ROOT` / `project-guide` checks declaration coverage and one exact sample's metrics, summary, output contents and bounded logs through confined local/SSH readers; executable Bash hooks and reviewed descriptor initialization. | Sample coverage is bounded and source-specific; native Research analyses still establish scientific applicability. |
| 05 Investigation timeline | `investigation-timeline` verifies the selected attempt and joins supported scheduler events, bounded logs and registered trace evidence; explicit timeline manifests remain supported. | Original text, source order and unknown clock domains remain distinct; no automatic universal log parser or cross-clock causal order. |
| 06 Semantic inspection | Evidence records retain units, source and coverage; existing raw/trend/range chart distinctions; `scientific-field` provides bounded field queries. | Full arbitrary-volume scientific visualization remains outside the terminal core. |
| 07 Accessibility | Existing keyboard traversal, ASCII, reader mode, reduced motion, focus, resizable panes and selection remain available. | **External gate:** real emulator, multiplexer, font and assistive-technology qualification. |
| 08 Gesture state | One pointer lease owns motion/release across eleven control families in native and direct dispatch, with shared cancellation and fresh-gesture reset. Pane-scoped zoom retains the shared exact metric acquisition identity. | **Partial:** geometry, attempt checks, previews, rollback and validation remain in the existing per-control state machines; terminal byte decoding retains its established protocol. |
| 09 Immutable publication | Store-owned revisioned immutable metric publications and CPU/GPU histories feed shared native-series, resource-column, comparison and Advisor preparations. Supported nested corrections invalidate source revisions; publication and preparation retention have bounded shared limits. | **Partial:** unmanaged extension writes retain exact content checking or uncached fallback; other Store fields and full document composition are not universally incremental. |
| 10 Virtual documents | Explicit pane contexts replace whole-App copies. Research and Analytics Details and native Job Series share document transforms for rows, controls, charts and scrollbars, with bounded visible-band rendering and first-publication, growth, resize and attempt-change handling. | Bounded documents may recompose to establish valid geometry after a first publication or scrolled resize. Other panes retain specialized renderers; no unbounded or universally incremental document promise. |
| 11 Work admission | Nested cluster readers use the shared scheduler and cooperative child gathering; settings use one coalesced background writer. | Adapter/service processes retain their explicit resource contracts; not every external tool obeys Python worker counts. |
| 12 Latency budgets | Bounded UI timings, p95/p99, sustained-input and changing-UI harnesses, snapshot comparison and qualification controls. | **External gate:** physical-terminal and SSH response qualification; local PTY results are linked below. |
| 13 State/reconnect | Atomic versioned/checksummed UI state, last-good recovery, older-state compatibility, future-schema preservation and final background-save drain. | Filesystem/node failure can still lose uncommitted state; external transports have separate replay contracts. |
| 14 Incident reproduction | Existing replay, input traces, source fixtures and sequential input regressions; bounded support evidence and qualification reports. | **Partial:** no deterministic recording of every OS scheduling event or arbitrary external application. |
| 15 Evidence records | `evidence` reads versioned identity, provenance, clock, unit, coverage and integrity records. | A valid schema checks supplied evidence; it cannot prove a producer measured correctly. |
| 16 Capability manifests | Adapter/source capabilities and site policy admission distinguish unsupported and unavailable observations. | Claims must be qualified against the installed runtime, build and site. |
| 17 Instrumentation federation | `diagnostic-recipe` checks explicit tool capabilities and conflicts. `raven trace mpi --source` emits a scoped C PMPI wrapper for supported world-communicator collectives. | **Partial:** no universal Caliper/OMPT/Kokkos/GPU injector. PMPI supports its declared C signatures/rank limit; unsupported operations produce gaps. |
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
| 28 Cooperative controls | `runtime-steering`, `io-coordination`, `scientific-field`, and `raven runtime` expose safe points and bounded summaries. `CollectiveRuntime` uses application-owned gather/broadcast for shared validation, commit epochs and outcomes. | Private leader-local mailbox; existing MPI/PyTorch process group is required. Partial commits become uncertain, not rolled back or automatically retried. |
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
| 40 Outcome evaluation | Research hypotheses retain explicit baselines, assumptions, negative-result and evaluation requirements in the dossier. | **Research gate:** independent outcome studies and comparisons are still required. |

## Five research hypotheses

| ID | Implemented mechanism and control | Qualification still required |
| --- | --- | --- |
| R1 Checkable, expiring recommendations | `recommendation` / `raven.recommendation-review/v1` checks supplied identity, evidence, applicability, expiration and acceptance. | Compare incorrect endorsement and resource cost with expert/rule baselines; a predicate checker is not a proof of universal validity. |
| R2 Choose the next information action | `investigation-plan` / `raven.investigation-plan/v1` ranks declared actions within budgets. | Evaluate information utility, calibration, noise and regret. Current planner is deterministic greedy, not optimal learning. |
| R3 Conservative revalidation | `scientific-impact` / `raven.scientific-lineage/v1` traverses declared dependencies and proposes checks. | Compare against full recomputation with seeded parser/input/policy changes; no unjustified minimality claim. |
| R4 Compile diagnostic intent | `diagnostic-recipe` checks `raven.diagnostic-intent/v1` against supplied site capabilities. | Execute reviewed recipes on independently configured sites; portability of JSON alone is not diagnostic equivalence. |
| R5 Query-preserving summaries | `information-query` checks supported declared summaries and answers against their contract. | Validate query families against complete reference data and rare events; cannot answer arbitrary discarded-history queries. |

## Ten advanced proposals

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

Each control implements the scope stated in its row. Scientific benefit and
target-system support remain subject to the corresponding evaluation gates.

## Prepared tracing contract

The recording pipeline uses **prepared, scoped instrumentation**. Preferred
Python recording uses CPython 3.12+ monitoring events.
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

## Foundation evidence

Foundation checks completed during development: 822 combined existing/new tests;
476 focused pointer, fragmented-input and sequential-control tests; and 88
asynchronous persistence/worker-switch checks. These sets overlap and must not
be added as a unique test count. The
[6.0 validation record](guides/flagship-validation.md) retains the release-wide
results and measured limitations of this foundation.

A snapshot-only microbenchmark used 2,000 histories with 600 values each and 120
calls on the same shared host. Mutable history copying took a 12.436 ms median;
cached immutable render histories took 0.333 ms. Maxima were 25.197 ms and 1.370
ms. This measures one data-publication stage. End-to-end terminal latency and
other sources of lag require separate measurements.

## Current release evidence and remaining gates

The [Raven 7.0 validation record](raven/validation.md) retains regression and
compatibility results, installed-package checks, offline collection qualification,
and local resource and pointer measurements. It also records the corrected PTY
test failure and the unmet 1% idle CPU goal. The linked reports define the
measured workload, host, and coverage; local success does not complete a site
pilot or a research evaluation.

The recorded cloud validation did not exercise a production Slurm controller,
hardware GPU allocation, multi-node workload, or production SSH endpoint.
Offline tests use controlled responses and real local files/processes/PTYs where stated. Use
[qualification](QUALIFICATION.md) to retain the untested target-system and
external evaluation gates. Follow the
[site rollout criteria](raven/implementation.md#site-rollout-and-stop-criteria)
before a production pilot.
