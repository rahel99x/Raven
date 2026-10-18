# Raven research strategy: archived Raven 4.16.1 proposal

Research snapshot: 10 October 2026, America/Los_Angeles. Repository baseline: **4.16.1**, commit `fd1c68304e3adbf5c11d715bb9851d0d2b187510`.

For the current source inventory and its qualification limits, use the
[October review audit](../implementation/review-2026-10/evidence/W10.md).
Its runtime contracts, executable handoffs, recorded checks and external gates
are separate evidence classes; this archived proposal is not a release result.

This archived strategy proposes product and research work from the 4.16.1
baseline. At that baseline, the 5.0 tracer and the ten previously discussed 6.0
capabilities were proposals. The findings, priorities, and acceptance targets
below retain that historical scope. In this document, **Raven** refers to the
application under review, then named Raven.

Supported subsets have since been delivered. Read the
[implementation ledger](../FLAGSHIP_IMPLEMENTATION.md) for their scope and
remaining gates, and the [Raven 7.0 validation record](../raven/validation.md) for
current release evidence.

The original study reviewed selected primary documentation and source. It did
not install competing products, run a comparative benchmark, or perform an
exhaustive literature or patent search. No application code was changed and no
tests were rerun for that study.

## 1. The product thesis

**Raven should help a researcher determine what happened, what evidence supports an explanation, what to measure or change next, and which scientific results remain trustworthy.**

The terminal supports ordinary SSH, keyboard operation, and access from a
workstation or cluster with limited deployment requirements. The proposed
product value depends on dependable investigations, interoperability,
administrator trust, and demonstrated user outcomes.

A representative complete journey is:

```text
Unexpected scientific result
  -> exact run attempt and input/build identities
  -> relevant logs, measurements, runtime events, and provenance
  -> competing explanations and evidence gaps
  -> bounded, reviewed diagnostic experiment
  -> measured result and scientific acceptance
  -> reproducible explanation or explicit unresolved question
```

Each stage produces a useful result. The complete journey connects the
scheduler, workflow engine, profiler, provenance system, and scientific
visualization stack through a common investigation context.

Define success through real users: shorter time to a correct diagnosis, fewer invalid recommendations, lower effort to connect a project, less wasted allocation time, and predictable monitoring cost. Feature count and terminal animation complexity are secondary measures.

## 2. What established tools already do

The cited primary sources confirm the capabilities below. A feature omitted
from this review may still exist in a tool; the table is a selected source
review rather than a comparative test.

| Ecosystem | Established capabilities | Implication for Raven |
| --- | --- | --- |
| LLview | Per-job CPU/GPU/power/I/O reports, node timelines, array and dependency-chain recognition, site collectors and reporting server. [S01] | Integrate permitted reports. Do not claim invention of workflow-aware job monitoring. |
| Princeton Jobstats | Terminal efficiency reports, allocation advice, GPU measurements, Prometheus/Grafana and Open OnDemand integration. [S02] | A terminal interface and efficiency advice alone do not distinguish Raven. |
| Rackslab Slurm-web | Scheduler UI, multiple clusters, topology, accounts/QOS/reservations, authentication, authorization and caching. [S03] | Complement institutional services; preserve user-space operation and authorized API access. |
| Open OnDemand | Browser shell, files, batch jobs, interactive applications and cluster access. [S04] | Raven can operate inside an existing access platform rather than trying to replace it. |
| ClusterCockpit | Job performance archives, comparisons, roofline views, aggregate statistics, APIs and support workflows. [S05] | Existing site telemetry can be a stronger starting point than another collector. |
| HPCToolkit, Caliper, Score-P, STAT | Source-correlated profiles, runtime instrumentation, scalable event collection and stack aggregation, with backend-specific limits. [S06–S09] | Build supported adapters and analysis workflows; do not promise a universal replacement. |
| Thicket and Hatchet | Cross-run and hierarchical performance analysis. [S10] | Multi-run comparison is established; connected scientific interpretation is a possible product opportunity. |
| AiiDA and Workflow Run RO-Crate | Scientific provenance, graph queries, caching/invalidation, and portable execution records. [S11–S12] | A provenance graph is not a new research contribution by itself. |
| GPTune, ReFrame, Coz | Constrained and multi-fidelity tuning; HPC testing; causal profiling and optimization assistance. [S13–S15] | Generic tuning, correctness checks, or AI profiler explanations are weak novelty claims. |
| Verificarlo and FLiT | Numerical sensitivity and floating-point variability analysis. [S16] | Integrate this expertise; do not rebrand numerical robustness as a new invention. |
| lnav | Merged chronological logs, structured parsing, error navigation, histograms and SQL queries in a terminal. [S17] | Raven can learn from mature log investigation workflows and connect their results to exact jobs. |
| SENSEI and openPMD | In-situ analysis infrastructure and scientific data access conventions. [S18] | Rich scientific field inspection should consume bounded derived data rather than pull entire datasets into the UI. |

The proposed opportunity is a connected, portable research workflow that retains
evidence identity and coverage. Its advantage over an existing combination of
these tools requires evaluation.

## 3. Implementation findings at the 4.16.1 baseline

The inspected release provides terminal input decoding, differential painting,
caches, background workers, reviewed actions, job-attempt identity, project
contracts, reporting automation, and offline regression coverage. The proposed
revisions preserve these foundations while improving their boundaries.

| Observation at the inspected revision | Proposed revision | Evidence scope |
| --- | --- | --- |
| `Store.snapshot()` copies retained CPU/GPU histories while holding the store lock; `_FrameCache.rebuild()` requests that snapshot before composition. | Publish immutable, revisioned source slices and update only dependent panes. | A concrete repeated-work opportunity; it is not proof that this explains every latency spike. |
| Raw input decoding is centralized, but gesture precedence and mouse routing are distributed across `screen.py` and `controller.py`. | One typed event router with explicit focus, capture ownership, geometry generation and cancellation. | A structural way to reduce recurring interaction bugs while retaining the existing decoder. |
| The shared worker scheduler exists, but multi-cluster collection constructs an additional executor from a one-time mode lookup. | Include nested collectors in shared admission and concurrency accounting. | A budget-consistency issue to investigate; no race was demonstrated by this review. |
| Trusted Python plugins receive app/store objects and render callbacks in-process. | Add a separate versioned adapter process protocol and conformance tests. | Better dependency and failure isolation; a subprocess or virtual environment alone is not a security sandbox. |
| UI settings are written directly to `ui.json`, with write errors suppressed. | Atomic, schema-versioned persistence, recovery and nonintrusive diagnostics. | A specific resilience improvement. |
| Ten main tabs, multiple Analytics/Research workspaces and seven Details modes expose considerable breadth. | Task-oriented defaults, consistent context and progressive disclosure. | This needs user testing; fewer visible choices alone do not prove better usability. |

Code anchors at the inspected revision:

- [Store snapshot and settings](https://github.com/rahel99x/Raven/blob/fd1c68304e3adbf5c11d715bb9851d0d2b187510/raven/model.py#L951).
- [Frame rebuild](https://github.com/rahel99x/Raven/blob/fd1c68304e3adbf5c11d715bb9851d0d2b187510/raven/screen.py#L1321).
- [Screen event routing](https://github.com/rahel99x/Raven/blob/fd1c68304e3adbf5c11d715bb9851d0d2b187510/raven/screen.py#L838) and [controller routing](https://github.com/rahel99x/Raven/blob/fd1c68304e3adbf5c11d715bb9851d0d2b187510/raven/controller.py#L1950).
- [Multi-cluster collection](https://github.com/rahel99x/Raven/blob/fd1c68304e3adbf5c11d715bb9851d0d2b187510/raven/scale_clusters.py#L121) and [plugin API](https://github.com/rahel99x/Raven/blob/fd1c68304e3adbf5c11d715bb9851d0d2b187510/raven/plugins.py#L21).

The recorded 4.16 validation reports 12,516 passing tests and two skips. It
does not exercise a live Slurm controller or production SSH host. Its changing-UI
benchmark records low median work alongside a 294 ms maximum frame, excluding
transport. These historical results identify validation coverage and remaining
production gates.

## 4. Forty investments, organized by outcome

Priority **A** means foundation or immediate product value; **B** means build after the foundation or with a pilot partner; **R** means an experimental capability requiring research validation. Some entries revise existing features rather than add another screen.

### A. Make the product understandable

| ID | Investment | Proposed behavior and acceptance evidence | Priority |
| --- | --- | --- | --- |
| 01 | Task-oriented home and workspaces | Offer Run, Investigate, Compare and Results entry points. Keep expert tools searchable and pinnable. Validate five core tasks with unfamiliar users. | A |
| 02 | Persistent context and navigation | Show cluster → project → run attempt → job/task → source. Back/forward navigation restores the intended view, not a stale job. Context changes invalidate gestures and actions. | A |
| 03 | Unified command model | Define commands once for menus, palette, buttons and keyboard activation. Generate control help from these definitions. Separate selection, focus and activation. | A |
| 04 | In-terminal project connection guide | Detect existing reports, show the minimum Bash hook, validate a sample run, and explain exactly which missing files/fields would populate an empty Research view. Extend `raven report`. | A |
| 05 | One investigation timeline | Join permitted logs, scheduler transitions, scientific phases, GPU events and user annotations with visible clock uncertainty. Preserve original bytes and source links. | B |
| 06 | Semantic chart and data inspection | Expose raw value, unit, aggregation, source age, display lag, interpolation and uncertainty. Display fitted trends as derived layers. Add bounded matrix, field-slice and distribution views where useful. | B |
| 07 | Accessibility and terminal consistency | Complete keyboard parity, color-independent state markers, ASCII and reduced-motion modes, explicit focus, scalable density and readable help. Qualify real terminals and multiplexers. | A |

### B. Make responsiveness and correctness architectural properties

| ID | Investment | Proposed behavior and acceptance evidence | Priority |
| --- | --- | --- | --- |
| 08 | Explicit gesture state machines | One capture owner and lifecycle for graph zoom, table resizing, selection, group drag and menus. Test arbitrary fragmented mouse streams and stale releases. | A |
| 09 | Incremental immutable data publication | Publish source revisions and changed identities. Unrelated data must not rebuild every pane or copy every history. Preserve corrections to older measurements. | A |
| 10 | Virtualized documents and queries | Compose visible rows/plots plus bounded overscan. Use indexes for selection, grouping, sorting and source lookup. Report truncation instead of allocating unbounded views. | A |
| 11 | Shared work admission | Account for all helpers and nested adapters; prioritize interaction-relevant results, isolate slow sources and bound queues. Trace transport must not monopolize the sole worker. | A |
| 12 | Measured latency budgets | Track input receipt, decode, dispatch, composition, paint/flush and source delay separately. Release decisions consider p95/p99 and stalls, not just average frame time. | A |
| 13 | Resilient reconnect and state | Atomic settings, version migrations, exact-attempt reconciliation, bounded offline retention and explicit stale state. Never publish an old session's reply into a new selection. | A |
| 14 | Deterministic incident reproduction | Expand replay into sanitized event/data/timing fixtures that reproduce input and lifecycle faults offline. Inject disconnects, reordered replies, source replacement and crashes. | A |

### C. Make data and integrations durable

| ID | Investment | Proposed behavior and acceptance evidence | Priority |
| --- | --- | --- | --- |
| 15 | Versioned evidence records | Common identity, units, time domains, coverage, loss, aggregation, provenance and source-version semantics. Preserve backend-specific evidence rather than flattening it away. | A |
| 16 | Capability and coverage manifests | Each adapter states what it can observe, under which runtime/build/site conditions, and with what missingness. UI distinguishes absent, unsupported, stale and zero. | A |
| 17 | Runtime instrumentation federation | Supported Caliper, PMPI, OMPT, Kokkos and GPU recipes; negotiate wrapper/tool conflicts and supported callbacks. Reuse runtime interfaces instead of inventing universal hooks. | B |
| 18 | Isolated adapter SDK | Versioned describe/validate/run/cancel/result protocol, bounded messages, explicit activation, pinned optional environments and compatibility fixtures. Keep core installation small. | A |
| 19 | Site telemetry connectors | Read authorized Jobstats/Prometheus, ClusterCockpit, LLview or Slurm REST data where available. Preserve exact attempt/device attribution and permission boundaries. | B |
| 20 | Portable research records | Bidirectional Workflow Run RO-Crate integration and preserved native workflow evidence. Raven exports remain useful to people who do not install Raven. | B |
| 21 | Shared queries and stable automation API | Extend current CLI/query capabilities with stable structured outputs and a user-scoped collection broker. Deduplicate subscriptions across windows, cache by identity and expose freshness. | A |

### D. Turn observation into scientific investigation

| ID | Investment | Proposed behavior and acceptance evidence | Priority |
| --- | --- | --- | --- |
| 22 | Causal delay and protocol diagnosis | Implement the earlier causal explorer and collective auditor with certified runtime scopes. Explain supported edges and abstain when missing evidence prevents attribution. | B |
| 23 | Reproducers and regression experiments | Combine exact failed-attempt evidence with bounded input reduction or controlled revision/configuration comparisons. Require a failure oracle and retain inconclusive results. | B |
| 24 | Scientific invariants and stability | Extend final acceptance checks with declared phase-level conservation, residual and domain checks, plus optional numerical perturbation campaigns. No invented scientific thresholds. | B |
| 25 | Validated counterfactual experiments | Extend the proposed simulator with applicability checks, recorded predictions, held-out validation and model withdrawal when predictions fail. | R |
| 26 | Active investigation planning | Choose among a new configuration, repeated run, targeted trace and stronger scientific validation under one explicit budget. Measure decision quality and cost. | R |
| 27 | Claim-level impact and revalidation | Extend artifact lineage with acceptance policies, assumptions and claims. Prepare a conservative selective revalidation plan after evidence changes. | R |
| 28 | Cooperative application controls | Supported live steering, checkpoint/I/O coordination and in-situ summaries through declared application safe points. Record parameter epochs and deadline/failure semantics. | B |

### E. Earn administrator and operator trust

| ID | Investment | Proposed behavior and acceptance evidence | Priority |
| --- | --- | --- | --- |
| 29 | Negotiated site policy | Document allowed collectors/actions, resource ownership, rate ceilings, retention and approved tools. Show effective limits. A user profile alone is not an OS-enforced security boundary. | A |
| 30 | Observability admission and accounting | Preview scope, supported delivery behavior, storage and resource reservations. Attribute observer cost. Reject incompatible requests; never silently weaken strict trace fidelity. | A |
| 31 | Consistent action lifecycle | Extend existing reviews to a uniform prepare → exact diff → confirm → receipt → readback/reconcile flow. Distinguish applied, partial, rejected and uncertain outcomes. | A |
| 32 | Private support and collaboration records | Explicitly selected, redacted evidence bundles with notes and reproducible queries. Optional authorized live read-only sessions require real access controls; no default public endpoint. | B |
| 33 | Offline and reproducible distribution | Pin optional tools, publish checksums/licenses/build metadata, support offline install and tested upgrades/recovery. No surprise dependency downloads during a capture. | A |
| 34 | Multi-site qualification kit | Portable small-allocation tests and support matrices tied to actual releases, terminals, Slurm versions, devices and policies. Integrate ReFrame where appropriate. | A |

### F. Make the project sustainable and useful beyond its authors

| ID | Investment | Proposed behavior and acceptance evidence | Priority |
| --- | --- | --- | --- |
| 35 | Task-based documentation | Short README, first-success tutorial, five core journeys, administrator guide, stable reference and separate release history. Generate repetitive control/schema reference. | A |
| 36 | Integration starter kits | Small Python, native MPI, GPU-training and Bash examples that publish real metrics and artifacts. Specify tested combinations and optional dependencies precisely. | B |
| 37 | Public performance and reliability corpus | Versioned realistic workloads, malformed inputs, clocks, arrays/retries, terminal events and scale cases. Publish tail latency, memory growth and collector/RPC cost. | A |
| 38 | Adapter and API support policy | Maintainer ownership, compatibility windows, deprecation rules, conformance tests and reproducible bug reports. Trusted Python plugins remain distinct from isolated adapters. | A |
| 39 | External user and site program | Recruit independent labs and operators, observe unassisted tasks, record missing capabilities and quantify diagnosis time. Treat installation failures as product failures. | A |
| 40 | Research and outcome evaluation | Benchmark flagship investigations against documented expert/tool baselines. Publish failed hypotheses, uncertainty and negative results alongside successes. | R |

The portfolio groups related work rather than prescribing forty simultaneous
implementations. Shared mechanisms should reduce duplicated collectors,
workflows, and controls as features arrive.

## 5. Proposed shared architecture

```text
Application hooks / scheduler / site telemetry / native profiler records
              |
       capability and policy negotiation
              |
    isolated collectors + shared admission/broker
              |
      raw evidence + versioned identity/time indexes
              |
       bounded queries and derived evidence
              |
   claims -> hypotheses -> reviewed experiments -> validation
              |
        immutable pane models and command descriptors
              |
         one terminal input router and diff painter
```

The UI subscribes to relevant revisions. It never starts filesystem scans, scheduler requests or analysis because the mouse crossed a graph. One command descriptor drives keyboard and mouse activation. Any external mutation revalidates exact identity and produces an outcome receipt.

Optional analysis engines can run in separate processes or allocated jobs.
Move a hot path to another language only after profiling shows that an algorithmic
fix, indexing, or cache invalidation cannot meet its budget.

Two interoperability details matter:

- Slurm REST is synchronous with the controller; its documentation recommends a caching proxy. Changing from CLI to HTTP is not a polling-load solution. [S19]
- OTLP does not provide an end-to-end no-loss guarantee across intermediaries. Its profiles specification is still marked Development in the reviewed source. Strict trace durability needs its own source coverage, journal, acknowledgment and recovery contract. [S20]

### Strict traces and low-cost observation are different contracts

Keep three claims separate:

1. Every event durably committed by the producer was transported and accounted for.
2. Every event in the declared, supported instrumentation scope was recorded.
3. Every behavior of an arbitrary program, runtime and device was captured.

The first is an engineering target. The second needs backend-specific qualification and conditions. The third is not a realistic universal guarantee. Score-P documents runtime/compiler limitations and the measurement disturbance of trace flushes; Caliper documents sampling/overhead tradeoffs. [S07–S08]

The proposal's strict-recording requirement needs an explicit result if its
declared scope cannot be admitted or maintained. Separately selected diagnostic
modes may use targeted instrumentation and summaries with their own coverage
contract. A fallback must disclose any change in collection semantics.

## 6. Five research hypotheses and evaluation plans

The hypotheses below combine established building blocks. A research
contribution requires a precise mechanism, a demonstrated advantage, and a
guarantee scoped to explicit assumptions.

### R1. A recommendation whose validity can be checked and expires

**Question:** Can a performance recommendation remain tied to the evidence and scientific conditions that make it applicable?

Represent a recommendation as a structured record containing exact attempts/builds/inputs, source queries, coverage and loss, competing explanations, the proposed intervention, predicted effect, scientific acceptance rules, refuting observations and expiry conditions. Present a short **Claim → Evidence → Test → Result** chain.

Example: fewer MPI ranks may reduce communication overhead but also alter numerical convergence. Raven prepares a bounded matched experiment and withholds endorsement until the declared scientific checks and performance evidence support it. A changed input, compiler or missing trace invalidates the old conclusion where relevant.

**Closest prior art:** Coz, GPTune, ReFrame, Thicket, provenance systems, and Raven's existing reviewed action identities. A portable lifecycle connecting all of these is a possible contribution; a generic AI recommendation is not.

**Evaluation:** Compare with fixed advisor rules and expert profiler workflows. Measure incorrect recommendations, accepted scientifically invalid changes, calibration, diagnosis time and total node-hours. Ablate intervention verification and scientific checks. A structured record verifies stated predicates and evidence, not universal correctness or optimality.

### R2. Choose the next information action, not only the next configuration

**Question:** Under a fixed budget, should the next action be a new configuration, a repeat, a short trace, or a higher-quality scientific check?

Maintain distinct models of performance, scientific admissibility and observation quality. Choose an action by expected improvement in the final decision, accounting for cost, queue uncertainty, perturbation and failure. Record censored and missing results rather than treating them as zero or success.

Example: two configurations differ slightly in runtime, but the faster one's scientific error is uncertain near the acceptance boundary. A stronger scientific check may be more valuable than exploring a third configuration. Later, variance may make another matched timing run the best use of resources.

**Closest prior art:** GPTune's constrained, multi-fidelity and transfer learning; active learning and value-of-information research; Score-P capture planning. The candidate novelty is the joint choice among instrumentation, replication, validation and configuration exploration with exact execution provenance.

**Evaluation:** Compare with constrained tuning, fixed measurement schedules and expert plans. Measure time/node-hours to a verified improvement, decision regret, false acceptance, budget adherence and robustness to bad queue estimates.

### R3. Compute a conservative scientific revalidation plan

**Question:** When evidence changes, how much work is actually necessary to support the affected scientific claims again?

Extend provenance entities with claims, policies, measured checks, assumptions and domain-specific equivalence/error bounds. Find a low-cost set of reparsing, validation, analysis and recomputation actions that restores the required evidence under stated rules.

Example: a parser reported incorrect units. Retained raw simulation outputs may allow corrected parsing and downstream analysis without repeating the numerical solve. A branch that used the wrong value as a new simulation input requires further review or recomputation.

**Closest prior art:** AiiDA graph queries and cache invalidation, incremental computation/build systems, RO-Crate and Raven's verified reuse. The proposed increment is claim-level scientific validity and conservative evidence propagation, not another DAG.

**Evaluation:** Seed input, parser, environment and policy changes. Compare decisions with full recomputation. Measure false-unaffected conclusions, validation agreement, saved compute and planning cost. Missing lineage must force uncertainty. Approximate equivalence is not automatically transitive, and minimality should be claimed only for an algorithm/model that establishes it.

### R4. Compile a diagnostic intent against a site's actual capabilities

**Question:** Can the same diagnostic question produce meaningful, permitted evidence on different clusters without researchers rebuilding the toolchain manually?

A declarative recipe names required observations, permitted approximations, intervals, storage/resource limits, allowed interventions and data exclusions. A planner resolves runtime/compiler/MPI compatibility, available tools, conflicting wrappers, permissions and capture semantics. It produces a reviewable plan or explains why the request cannot be satisfied.

Example: diagnose MPI imbalance and GPU launch starvation for a selected iteration range without stopping ranks or capturing application payloads. The adapter choices may differ by site, but the evidence requirements remain visible.

**Closest prior art:** ReFrame portability, Caliper recipes, environment/module systems, capability negotiation and profiler planning. The possible contribution is semantic portability of a diagnosis rather than portability of a shell command.

**Evaluation:** Execute the same diagnosis intent on independent sites with different MPI/compiler/GPU policies. Compare setup effort, evidence comparability, refusal accuracy and resource cost with manual instructions. Do not substitute weaker collection silently when a required observable is unavailable.

### R5. Preserve answers to declared questions with smaller evidence summaries

**Question:** Can a collector emit a substantially smaller representation while preserving answers to an explicitly declared family of diagnostic queries?

For suitable queries, retain exact counts, sums, dependency summaries or bounded-error distributions and include the transformation and error contract. The UI shows whether a requested question remains answerable from that summary or requires retained raw evidence/new collection.

This is not a substitute for a strict full-event recording. It is a separate mode, or a derived index over raw events that remain retained. A summary cannot guarantee support for arbitrary future questions or recover discarded history.

**Closest prior art:** Streaming algorithms, sketches, trace filtering/compression, database materialized views and STAT rank aggregation. A specific query language, preservation algorithm and coverage semantics would need a dedicated literature review; novelty confidence here is lower than for the product integration.

**Evaluation:** Compare query answers with complete reference traces across normal runs and seeded anomalies. Measure bytes, CPU, answer error and false assertions of completeness. Include adversarial rare events, missing ranks and questions outside the declared family.

## 7. Three proposed end-to-end demonstrations

### Demonstration A: a distributed stall explained

A supported MPI/GPU application has a seeded delayed rank or protocol mismatch. Raven opens the exact attempt, shows the supported causal chain or first confirmed mismatch, identifies missing evidence, and exports a reproducible investigation. Compare correctness and user diagnosis time with the existing expert workflow. Do not present a timeout alone as deadlock proof.

### Demonstration B: an expensive failure made reproducible

A failed campaign becomes a smaller input/configuration witness through bounded diagnostic jobs and an explicit failure oracle. Export the retained environment identity, script, evidence and verification test. Report the smallest case found within the model/budget and the observed reproduction rate.

### Demonstration C: a result change investigated and revalidated

A scientific output changes after a software or parser revision. Raven connects the result to its exact evidence, compares candidates, proposes a test, and prepares only the justified revalidation work. Show cases where the correct answer is to retain uncertainty.

Each demonstration joins existing and proposed mechanisms into an observable
user outcome. Evaluate the complete investigation, including unresolved cases,
rather than the number of available panels.

## 8. Validation gates and rollout

These are **proposed acceptance criteria**, not current measurements or universal performance promises.

| Gate | Evidence required before advancing |
| --- | --- |
| G0: predictable interaction | On a fixed disclosed workload/host, target p95 input-to-application feedback under 50 ms and p99 under 100 ms. Record full stalls and terminal/SSH contributions separately. No mouse-only sequence activates a keyboard command or wrong page. |
| G1: bounded operation | No collection from render/hover paths; enforced queue/storage/process bounds; no unbounded post-warmup growth in a 24-hour soak. Worker transitions and disconnected sources preserve accepted work and explicit outcomes. |
| G2: evidence integrity | Attempt/PID/device/clock identity tests; gaps, corrupt/reordered/duplicate records, runtime exceptions and requeues; immutable evidence transformations and round-trip format tests. Strict capture has explicit source and crash-tail limits. |
| G3: ecosystem compatibility | At least two external evidence adapters and versioned conformance tests. Faulty adapters cannot freeze the terminal. Offline installation and supported upgrade/recovery paths work. |
| G4: real deployment | Three independent site pilots spanning at least two Slurm versions and differing accounting/GPU policies. Include a workstation case and a genuine multi-node allocation. Publish untested/unsupported combinations. |
| G5: usable investigations | Unfamiliar users attempt the five core journeys without coaching. A proposed target is at least 80% task completion; publish failure reasons and time distributions. |
| G6: research contribution | Baselines, held-out workloads, missing-evidence cases, ablations, perturbation measurement and scientific-error outcomes. Results must justify complexity and resource cost. |

Recommended sequence:

1. Establish G0/G1 and simplify the core journeys. Fix structural interaction and snapshot costs before increasing trace volume.
2. Establish versioned evidence, adapter and site contracts; pilot two useful external integrations.
3. Qualify the planned trace backends within narrow declared scopes. Complete one end-to-end demonstration with external users.
4. Add the other demonstrations and portable support/qualification tooling.
5. Develop R1–R4 as focused prototypes and publish evaluations. Treat R5 as exploratory until its query family and prior art are well defined.

Release numbers should follow these gates; they should not imply maturity merely because the feature inventory grew.

## 9. Design risks and exclusions

- Another wave of independent tabs without common context, commands and evidence semantics.
- An unmeasured rewrite of the renderer or a premature requirement to rewrite everything in Rust/C++.
- Claims of universal, zero-overhead, complete tracing across arbitrary runtimes and GPUs.
- Smoothing or interpolation that visually hides missing scientific measurements.
- An AI assistant with unrestricted shell/scheduler access. If language assistance is added, it should propose structured, evidence-linked queries/plans through the same bounded APIs; external model use and data sharing remain explicit choices.
- Duplicating established profiler, workflow or provenance engines solely to claim feature breadth.
- Treating a read-only query as free, or treating a process boundary as a security sandbox.
- Claiming novelty from a terminal presentation, a feature combination, or absence of a feature in competitors' README files.
- Expanding the feature inventory without evaluating whether external users can complete the five core journeys.

## 10. Research claim evaluation

For each research candidate, write a one-sentence claim naming the mechanism, assumptions and measurable outcome. Search scientific literature and tool manuals for that exact problem, including adaptive profiling, active experimental design, scientific workflow validity, trace compression and measurement perturbation. Request review from relevant maintainers and HPC researchers. Compare against the strongest reasonable baseline, not an artificially weak one.

Keep three labels in the roadmap: **established technique**, **new Raven integration**, and **research hypothesis**. Promote a hypothesis only after evaluation; a useful negative result is still progress.

The primary-source review identifies relevant building blocks and evaluation
baselines. It does not establish priority of invention. Evaluate whether the
proposed mechanisms make HPC investigations faster, more reproducible, and more
scientifically reliable at a disclosed cluster cost.

## Primary-source ledger

Sources were inspected through public upstream documentation/source. Most links track development branches; implementations must pin and qualify supported releases. The links establish the stated building blocks, not superiority of Raven or novelty of the proposed combinations.

- **S01 — LLview:** [overview](https://github.com/FZJ-JSC/LLview/blob/master/README.md), [workflow recognition](https://github.com/FZJ-JSC/LLview/blob/master/docs/docs/workflow/index.md), [metrics](https://github.com/FZJ-JSC/LLview/blob/master/docs/docs/jobreport/metrics_list.md).
- **S02 — Jobstats:** [overview and CLI examples](https://github.com/PrincetonUniversity/jobstats/blob/master/README.md), [architecture](https://github.com/PrincetonUniversity/jobstats/blob/master/docs/index.md), [deployment](https://github.com/PrincetonUniversity/jobstats/blob/master/docs/setup/overview.md).
- **S03 — Slurm-web:** [overview](https://github.com/rackslab/Slurm-web/blob/main/README.md), [architecture](https://github.com/rackslab/Slurm-web/blob/main/docs/modules/overview/pages/architecture.adoc).
- **S04 — Open OnDemand:** [overview](https://github.com/OSC/ondemand/blob/master/README.md), [requirements](https://github.com/OSC/ood-documentation/blob/master/source/requirements.rst).
- **S05 — ClusterCockpit:** [overview](https://github.com/ClusterCockpit/cc-doc/blob/main/content/en/docs/overview/_index.md), [job views](https://github.com/ClusterCockpit/cc-doc/blob/main/content/en/docs/webinterface/views/job.md), [comparisons](https://github.com/ClusterCockpit/cc-doc/blob/main/content/en/docs/webinterface/views/jobcompare.md), [backend](https://github.com/ClusterCockpit/cc-backend/blob/master/README.md).
- **S06 — HPCToolkit:** [overview and supported features](https://github.com/HPCToolkit/hpctoolkit/blob/develop/README.md).
- **S07 — Caliper:** [overview and integrations](https://github.com/LLNL/Caliper/blob/master/README.md), [sampling tradeoffs](https://github.com/LLNL/Caliper/blob/master/doc/sphinx/SampleProfiling.rst).
- **S08 — Score-P:** [overview](https://gitlab.com/score-p/scorep/-/blob/master/README.md), [known limitations](https://gitlab.com/score-p/scorep/-/blob/master/OPEN_ISSUES), [trace planning and overhead](https://gitlab.com/score-p/scorep/-/blob/master/doc/doxygen-user/06_score.dox).
- **S09 — STAT:** [parallel stack analysis](https://github.com/LLNL/STAT/blob/master/README.md).
- **S10 — Ensemble analysis:** [Thicket](https://github.com/LLNL/thicket/blob/develop/README.md), [Hatchet](https://github.com/hatchet/hatchet/blob/develop/README.md).
- **S11 — AiiDA:** [overview](https://github.com/aiidateam/aiida-core/blob/main/README.md), [queries](https://github.com/aiidateam/aiida-core/blob/main/docs/source/howto/query.rst), [caching and invalidation](https://github.com/aiidateam/aiida-core/blob/main/docs/source/topics/provenance/caching.rst).
- **S12 — Workflow Run RO-Crate:** [provenance profiles](https://github.com/ResearchObject/workflow-run-crate/blob/main/docs/index.md).
- **S13 — GPTune:** [tuning methods, objectives and constraints](https://github.com/gptune/GPTune/blob/master/README.md).
- **S14 — ReFrame:** [HPC testing and benchmarking](https://github.com/reframe-hpc/reframe/blob/master/README.md).
- **S15 — Coz:** [causal profiling and optimization assistance](https://github.com/plasma-umass/coz/blob/master/README.md).
- **S16 — Numerical analysis:** [Verificarlo](https://github.com/verificarlo/verificarlo/blob/master/README.md), [numerical delta debugging](https://github.com/verificarlo/verificarlo/blob/master/doc/04-DeltaDebug.md), [FLiT](https://github.com/PRUNERS/FLiT/blob/master/README.md).
- **S17 — lnav:** [terminal log analysis](https://github.com/tstack/lnav/blob/master/README.md).
- **S18 — Scientific data and in-situ analysis:** [SENSEI](https://github.com/SENSEI-insitu/SENSEI/blob/develop/README.md), [openPMD API](https://github.com/openPMD/openPMD-api/blob/dev/README.md).
- **S19 — Slurm:** [REST architecture and caching](https://github.com/SchedMD/slurm/blob/master/doc/html/rest.shtml), [slurmrestd](https://github.com/SchedMD/slurm/blob/master/doc/man/man8/slurmrestd.8), [squeue performance guidance](https://github.com/SchedMD/slurm/blob/master/doc/man/man1/squeue.1).
- **S20 — OpenTelemetry:** [OTLP specification and delivery limits](https://github.com/open-telemetry/opentelemetry-proto/blob/main/docs/specification.md), [memory admission and retry behavior](https://github.com/open-telemetry/opentelemetry-collector/blob/main/processor/memorylimiterprocessor/README.md).
- **S21 — Perfetto:** [flow events](https://github.com/google/perfetto/blob/main/docs/instrumentation/track-events.md), [trace queries](https://github.com/google/perfetto/blob/main/docs/analysis/trace-processor.md), [preinstalled engine selection](https://github.com/google/perfetto/blob/main/docs/analysis/trace-processor-python.md).
- **S22 — Runtime interfaces:** [PMPI](https://github.com/open-mpi/ompi/blob/main/docs/features/profiling.rst), [OMPT callback definitions](https://github.com/llvm/llvm-project/blob/main/openmp/runtime/src/include/omp-tools.h.var), [Kokkos Tools](https://github.com/kokkos/kokkos-tools/blob/develop/README.md).
- **S23 — Collective diagnosis:** [PyTorch collective matcher](https://github.com/pytorch/pytorch/blob/main/torch/distributed/flight_recorder/components/types.py), [Flight Recorder](https://github.com/pytorch/pytorch/blob/main/torch/csrc/distributed/c10d/FlightRecorder.hpp).
- **S24 — Earlier advanced-feature foundations:** [SimGrid SMPI](https://github.com/simgrid/simgrid/blob/master/docs/source/app_smpi.rst), [Picire](https://github.com/renatahodovan/picire/blob/master/README.rst), [VisIt simulation controls](https://github.com/visit-dav/visit/blob/develop/src/sim/V2/lib/VisItControlInterface_V2.h), [ADIOS2 BP5](https://github.com/ornladios/ADIOS2/blob/master/docs/user_guide/source/engines/bp5.rst).
