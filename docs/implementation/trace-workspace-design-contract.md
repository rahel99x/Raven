# Raven Trace Workspace: implementation brief

Status: accepted implementation contract, retained from the source-readiness audit. The starting-point revision and baseline below are historical audit evidence; this contract itself does not certify implementation. Current delivery status, adjustments, commands and qualification limits are recorded in the [coverage ledger](trace-workspace-coverage.md).

## Verified starting point

The fetched `origin/main` revision is `2bfe5795290575de0e1c4390f2166e89897902f6`. The inspected checkout at `288a680` has an identical tracked tree. Recheck the repository and its instructions before implementation; this snapshot is not a promise about a future revision. Reconcile existing approved or pending fixes, including the verified Heavy-startup change at `8b317a0`, when choosing the implementation base; do not silently drop that behavior or merge unrelated work without authorization.

Raven already has **Jobs/History → Details → Trace → Timeline**, a terminal timeline, an offline browser viewer, process/thread selection, nested recorded intervals, search, zoom, pan, Fit, CPU-placement observations, exact-attempt association, and retained export/summary paths. Extend these owners and tests.

| Requested capability | Existing basis | Required expansion |
|---|---|---|
| Magic Trace-style Live/Fit graph | Nested time-versus-stack bars, live terminal reads, zoom/pan/Fit | Explicit trailing Live window, run-wide overview, whole-recording range queries |
| Multi-thread | Browser thread lanes; terminal thread selection | Simultaneous expandable thread lanes in both interfaces |
| Multi-core | Socket/core/logical-CPU placement observations | Scheduling-backed execution slices and migration-aware stack projection when capture supports them |
| Multi-node | Node/rank/process identities | Combined run catalog, clock-domain-aware federation, node grouping |
| Multi-GPU | Python host calls and cooperative spans | Device activities, stream/queue lanes, visibility mapping and host/device correlation |
| Timing tooltips | Selected-call duration/source details | Browser hover, pinned inspector, equivalent keyboard/click interaction |
| Completed full-job exploration | History association and full-prefix CLI traversal | Persistent index and navigable full retained recording, function statistics and evidence-based resource metrics |

Baseline verification: 77 existing timeline/workflow/browser/CPU-timeline tests passed in 2.46 seconds. Command:

```bash
.venv/bin/python -m pytest -q \
  tests/test_trace_timeline_workflow.py \
  tests/test_trace_timeline_ui.py \
  tests/test_trace_browser.py \
  tests/test_trace_cpu_timeline.py
```

This checks existing behavior, not the proposed features, physical GPU capture, or production multi-node qualification.

## Optimized implementation prompt

Act as Raven's senior systems engineer and implementation lead. Evolve the existing Trace pane into a unified, portable **Trace Workspace** for live and retained job recordings. Deliver complete, independently reviewable milestones, with a working path from launch/capture or import through indexing, terminal/browser viewing, persistence, diagnostics, and tests.

The experience should make deeply nested execution understandable in the style of Magic Trace and GPU timelines in profiling tools. Keep the initial interface simple, while allowing detailed topology, timing, causality, counters and comparisons through progressive disclosure.

### 1. Product contract and terminology

Use one shared viewer/model with two navigation states:

- **Live:** follow the latest verified data through a trailing window of duration Δ. Δ controls the displayed time span, independently of polling and recording frequency. Include useful presets and a custom duration.
- **Explore:** preserve the chosen range and selection for inspection of running or finished jobs.
- **Fit:** a viewport action available from either state. Fit pauses following and fits the selected scope's verified retained time range; Resume Live restores the previous Δ. Completed jobs initially open in Explore with a whole-recording overview.

Panning, zooming or pinning a selection pauses following predictably. Pausing the view does not stop application recording. Capture and bounded index ingestion may continue independently while the displayed snapshot remains fixed. Closing a dashboard stops its owned viewing/index work without terminating an independently running recorder or scientific job.

“Full trace” means all retained, verified committed evidence for the chosen run attempt. Capture limits, incomplete streams and unknown tails remain visible. Do not equate a successfully read prefix with complete observation of all program execution.

### 2. Portable viewing and explicit capture capabilities

Build a vendor-neutral event model and viewer. Report capture support separately for each actual execution environment, runtime and adapter.

Distinguish:

1. Instrumented call/span boundaries.
2. Statistical stack samples.
3. Scheduler execution/state observations.
4. GPU activities and counters.
5. Decoded hardware control flow.

Every source declares adapter/version, fidelity, scope, permissions, symbols, clock domain, coverage, loss and qualification status. Distinguish implemented, prerequisites present, usable, and live-qualified. A processor name or detected profiler binary does not qualify a collector.

Preserve existing Python monitoring, explicitly selected legacy profiling, native cooperative SDK, selected MPI support and CPU-placement capture. Add adapters through an explicit registry and trust boundary. Optional tools are dependencies of their adapter, not mandatory installation requirements for all Raven users.

Plan GPU capture/import separately for NVIDIA, AMD, Intel and Apple ecosystems. Candidate integrations include standard Perfetto/Chrome trace exports and framework profilers first, followed by qualified vendor report importers or collectors such as NVIDIA activity profiling, AMD ROCprofiler, Intel Level Zero, and Apple platform tools. Validate actual versions and export schemas during implementation. These names are integration candidates, not statements that Raven currently implements them.

A device timeline may show kernels, transfers, synchronization and launch-site CPU stacks before device call stacks are available. Device stack unwinding, PC sampling, GPU hardware counters and CPU instruction tracing require their own adapters and fidelity labels. A host CUDA/ROCm/Metal call's elapsed time is not kernel execution time. Do not insert application-wide synchronization merely to make UI timing easier.

Unsupported domains must not suppress available CPU, memory, Python or other trace data. Preserve normal scientific-job behavior when optional capture fails; offer an explicit capture-required policy for workflows that need stricter guarantees.

### 3. One run session and stable identities

Create a run-level session/catalog over the existing verified source journals. Keep the journals authoritative and the index disposable/rebuildable.

Bind every source and result to the exact cluster, run, scheduler job attempt, node/boot, process lifetime, stream and recording generation. Preserve rank/local-rank and process-role metadata. Extend thread identities to handle TID reuse; distinguish physical threads from logical async tasks. Do not merge requeues or recycled job IDs without explicit comparison.

Use stable event/interval IDs to preserve selection across refresh, pagination, node reconnects and job completion. Reject stale background results after run/source/selection changes. Persist view preferences separately from captured evidence.

Legacy recordings with missing clock, thread-lifetime, residency or device metadata remain viewable with those fields explicitly unknown. Evolve bounded schemas compatibly. Descriptive extensions may be tolerated according to the existing contract; action, ownership, security and accounting fields require explicit validation. Reuse the current background-work, cancellation, verified ownership, transport and notification services.

### 4. Composable topology perspectives

Use a hierarchical navigator and shared time axis with alternative projections:

- **By execution:** Node → Process/MPI rank → Thread/logical task → Recorded call stack.
- **By CPU resource:** Node → Socket/die/NUMA metadata where available → Physical core → Logical CPU/SMT thread → Observed execution slices.
- **By GPU resource:** Node → Device/partition → Process context → Stream/queue → Kernel, transfer and synchronization activity.

Threads migrate. Cores are a projection of execution evidence, not permanent parents of software threads. When scheduling and call evidence permit it, intersect verified on-CPU slices with the recorded stack to show migration across resource lanes. Sparse `sched_getcpu` observations remain points with query brackets; they do not prove residency between observations.

Identify a physical core using node/boot plus socket/die/core identifiers, not a bare core number. Distinguish logical CPUs, physical cores, SMT siblings, allocation limits, affinity and heterogeneous core types. Unknown or changing topology must remain explicit.

GPU identity includes node, physical UUID or equivalent identity, partition/virtual-device identity and lifetime. Preserve mappings from process-visible ordinals to physical devices. GPU 0 on two nodes is two devices; two process-local ordinal values can refer to the same device. Scope context and stream IDs to their owning process/device lifetimes.

Support one GPU on one node, multiple GPUs on one node, one GPU per node, unequal GPU counts per node, nodes without GPUs, mixed vendors, partitions, visibility remapping, device loss and reconnects. GPU-native units such as SMs/CUs may be optional adapter-specific lanes; do not invent a universal GPU-core/warp model.

### 5. Time, ordering and causality

Preserve integer nanosecond timestamps, their original clock domain and source sequence. Continue exact timestamp handling across the Python/JavaScript boundary; convert relative values only for drawing.

Align streams directly only when they have a proven common clock domain. Cross-node and host/device alignment require calibrated transforms with validity ranges, resolution, drift and uncertainty. Retain raw timestamps and calibration provenance.

When alignment is unavailable, show separate/local axes or explicitly marked relative views. Manual phase alignment is a visual aid, not evidence of simultaneity. Do not infer precise cross-host ordering from wall-clock proximity or align every process's first event to zero without disclosure.

Represent explicit causal links for async task creation, supported MPI operations, GPU launches and synchronization. Correlation IDs must be scoped and lifetime-safe. Show host enqueue time, queue delay and device execution separately when their prerequisites are satisfied. Missing links and uncertain clocks must limit critical-path and overlap claims.

### 6. Timeline interaction and accessibility

Reuse the current terminal/browser renderers where practical. Provide:

- Run identity/status, selected time range, Follow/Pause, Δ, Fit, search and recording health as the compact default toolbar.
- A virtualized, collapsible lane navigator with filters for node, rank/process, thread, CPU, GPU, function and event type; useful presets instead of a large row of grouping buttons.
- Time-aligned lanes, nested call bars, range selection, zoom-to-selection, pan, breadcrumbs and an overview minimap.
- Browser hover tooltips showing full symbol, source, start/end, duration, lane, provenance and coverage. Click pins a stable inspector. Preserve a tooltip's event identity as live data arrives.
- Equivalent keyboard focus, next/previous event, parent/child stack navigation and click-to-inspect behavior in the terminal. Mouse hover cannot be the only route to information.
- Source navigation tied to the recorded binary/build/revision. Missing or mismatched symbols remain unresolved, not silently resolved against unrelated current source.
- Explicit rendering for gaps, corrupt sources, unpaired boundaries, incomplete capture and unknown placement. Never close a missing call at a viewport edge to manufacture a duration.
- Color-independent labels/patterns, readable contrast, keyboard discoverability and an accessible event/details list alongside graphics.

Design the terminal experience for 80 columns first. Reveal the hierarchy/inspector contextually in small terminals; allow richer simultaneous panes in the browser. Persist lane expansion, filters, pinned lanes, zoom and selection per view/run as appropriate. Live → completed transitions preserve context rather than reopening or scrolling the user elsewhere.

Provide a global Traces destination that opens the same run session used by a job's Trace action. History, notifications, saved links and exports should deep-link to an exact run attempt and selected range/event.

### 7. Define metrics instead of a generic efficiency score

Every metric must declare units, numerator, denominator, source, scope, coverage and availability conditions.

| Metric | Required evidence and meaning |
|---|---|
| Inclusive elapsed time | Matched recorded boundaries in a valid clock domain; includes waits and instrumentation effects |
| Self elapsed time | Valid same-activation nesting, subtracting the union of child intervals; async device work is not a synchronous child subtraction |
| On-CPU/off-CPU time | Appropriate scheduler or thread CPU-time evidence; absence from samples is not proof of blocking |
| Sample share | Observed weighted samples and stated sampling methodology; not an exact per-call duration |
| CPU allocation utilization | Consumed CPU time divided by effective allocated logical-CPU time over the same covered range; explain SMT/affinity and allocation changes |
| Physical-core busy time | Union of valid execution slices across a core's SMT siblings; not a throughput/IPC efficiency claim |
| GPU activity busy time | Union of relevant device/engine intervals over a covered range; concurrent kernels must not be double-counted |
| Occupancy/throughput/memory metrics | Adapter-specific hardware counters and definitions; kernel duration alone cannot establish them |
| Parallel efficiency | Comparable serial/baseline workload and declared resource count; otherwise unavailable |

Show call count, inclusive/self elapsed totals, min/max and appropriate distribution statistics. Label approximate histograms/quantiles with their actual bounds. Concurrent thread durations do not sum to job makespan. Occupancy is not utilization, and either can be high while application throughput is poor.

Overlay CPU/memory/device counters and scientific phase markers from existing Raven recordings when identities and clocks match. Make bottleneck hints evidence-linked and clearly scoped. Report unavailable values as unavailable with a reason, rather than zero or an invented percentage.

### 8. Full retained-recording storage and efficient queries

Extend the verified reader and existing disk-backed traversal into a persistent, versioned, rebuildable run index. Support interval overlap queries, source/stream cursors, stack checkpoints, symbol dictionaries and multiresolution overview summaries.

Live ingestion reads only newly committed blocks in bounded batches. A bounded display ring must not silently become the archive's retention policy. Retention/capture quotas, unknown tails and stopped capture remain visible. Requeue/truncation/replacement must not append unrelated data into an old session.

Use page/range queries and lane virtualization. Full Fit draws honest overview/density summaries and drills into exact evidence; it must not load every event into RAM. Query cost should scale with requested ranges, visible lanes and returned results. Target indexed lookup behavior near O(log N + K), with separately bounded live buffers and caches; measure actual behavior rather than claiming an asymptotic result from an interface alone.

Start with the existing standard-library/disk-backed approach. Avoid introducing a heavyweight trace engine or new binary format until benchmarks justify it. Preserve Perfetto/other existing exports. If SQLite is used, keep a single owned writer and local materialized index; do not assume shared NFS WAL locking is safe. Shared-storage publication should use validated immutable chunks/manifests or another explicitly qualified scheme.

Separate capture/checkpoint cadence, file/index ingestion cadence, viewer refresh, Live Δ and Slurm polling. Avoid scheduler RPCs per frame or per lane. Batch node/source discovery and reuse shared connection/collection budgets. Read retained data promptly on selection; temporary uncommitted-tail/read-later states keep the previous verified display and retry on normal cadence without being promoted to recorder corruption.

### 9. Browser, remote and developer workflows

Keep the private offline HTML export working, with explicit retained-range/size limits. Whole-recording navigation beyond a portable snapshot's bound needs a range-query backend, not a silently truncated “full” HTML file.

For live browser viewing, evaluate a user-scoped read-only loopback service using the same query model. Start it only through the explicit viewer action. Use session-bound access, strict origins/host handling, bounded queries, no public default binding and no arbitrary file or job-control endpoints. Keep offline export CSP protections separate from live-service policy.

On headless/SSH hosts, explain the difference between remote and local loopback and provide a reviewed forwarding workflow or private offline transfer. Do not claim that asking the remote host to open a browser opened a tab on the user's laptop.

Reuse existing site/lab/project profiles. Keep login identity, billing account, scientific-job resources and Raven Heavy resources distinct. Node collectors must stay within verified owned allocations and declared budgets; avoid surprise nested allocations.

Integrate capture configuration into the existing launch/setup workflow: capability preview, profile choice, resource/overhead budget, recording scope and a small pilot. Preserve original job scripts and show reproducible generated commands. Existing `raven launch ... --trace` remains supported. Label proposed new flags as proposed until implemented and documented.

Support custom phase spans, metrics and tracing through the existing Python/native hooks, with documented schemas and runnable examples. Make raw symbol/stack text safe in terminals and HTML. Preserve privacy, path redaction and private artifact permissions. Importers must treat reports as untrusted bounded data and must not execute plugins, SQL extensions, paths or commands supplied by a trace.

### 10. Additional capabilities worth including

- Call-tree, bottom-up and aggregate flamegraph views, clearly distinguished from the chronological timeline.
- Bookmarked ranges, annotations, exact-event links and baseline/run comparison with explicit alignment and workload comparability.
- CPU ↔ GPU launch/copy/synchronization flows and available MPI/async links.
- Symbol resolution with build-ID caching, demangling and source revision checks.
- Independent readiness/health indicators per capture domain, freshness/watermarks per source, and deduplicated notifications linked to the affected run/settings.
- Recording profiles, helper/rank filtering and a capture budget preview; optional bounded triggered capture for difficult incidents, with explicit retention/fidelity semantics.
- Recoverable indexes, reconnect support and cancellation of obsolete analysis/query work.

### 11. Delivery sequence and gates

Use the following dependency order. Each milestone must work from user action to persisted evidence to rendered result; do not deliver placeholder controls.

1. **Session and query foundation:** run catalog, stable IDs, compatible schema extensions, capability/fidelity manifests and shared query contracts. Keep current terminal/browser behavior working.
2. **Indexed Live/Explore viewing:** persistent local index, full retained-run range navigation, multiresolution Fit, Live Δ, multiple thread lanes, tooltips/keyboard inspector and completion continuity. Deliver terminal and browser range access against the same model.
3. **CPU and node federation:** resource projection, migration-aware scheduling evidence where supported, independent process/node clocks, calibration/uncertainty and exact-attempt aggregation. Unknown placement remains usable as unknown.
4. **GPU activity:** implement a portable activity import path and one complete, qualified GPU adapter end to end; add device identity, context/stream lanes and host correlation. Imported fixtures are useful before hardware qualification but cannot be labeled live-qualified.
5. **Additional adapters and multi-node GPU:** qualify vendor/OS/device paths separately, mixed topology, partitions, counters and clock mappings. Keep each supported path functional while unsupported domains remain explicit.
6. **Analysis and workflow polish:** validated function statistics, evidence-based utilization, comparisons, flow/critical-path tools where justified, documentation and onboarding examples. Basic duration inspection must already ship in milestone 2.

Do not postpone an essential dependency behind a feature that requires it. Split milestones further when needed for reviewability; retain the accepted end state and a coverage ledger rather than discarding difficult requirements.

### 12. Acceptance and qualification

Preserve existing behavioral assertions, including exact-attempt association, background reading, pause/follow, selection continuity, bounded output and safe browser export.

Add deterministic/property/integration coverage for:

- Deep recursion, re-entrancy, yields/resumes, concurrent/nested spans, unmatched boundaries, loss, corruption and events crossing index pages or view windows.
- PID/TID reuse, fork/exec, requeue, recycled job IDs, stale results, node disconnect/reconnect, source replacement and schema migration.
- CPU SMT, repeated core IDs across sockets/nodes, migration, affinity changes, missing topology, heterogeneous cores and hotplug.
- One/no/multiple/uneven/mixed/partitioned GPUs, ordinal remapping, overlapping streams, async launch delays, copies, missing symbols and device loss.
- Unsynchronized clocks, skew/drift, uncertain calibration and invalid cross-domain duration/critical-path claims.
- Live interaction during arrivals, stable hover/selection, range cancellation, completed History navigation, readable narrow terminals and headless browser fallback.
- Hostile names, enormous/deep inputs, path traversal, import isolation, malformed optional metadata, disk-full, quota exhaustion and slow shared storage.

Use the existing trace benchmark harnesses and add focused ingestion/index/range/render measurements. Measure application slowdown, event rate, committed-data freshness, UI latency, scheduler RPC count, CPU use, storage I/O, Python allocations and process RSS separately. Track stopped/lost/unknown capture explicitly. Use identical before/after workloads on documented hardware; do not improve results by removing fidelity or validation.

Initial UX targets for qualification are p95 local input/selection latency below 100 ms and p95 committed-data-to-visible latency below 2 seconds on a documented healthy local fixture. These are goals to measure and revise transparently, not hardware-independent guarantees. Test increasing fixture sizes and lane counts; reserve large stress suites for an explicit resource budget rather than making every small CI job ingest gigabytes.

Qualify actual local capture, local Slurm, real multi-node execution and physical GPU paths independently. A simulated topology or architecture string checks software behavior only. If hardware, permissions, tools or credentials are unavailable, complete independent work and record the exact blocked qualification and unblock condition. Never mark those adapters fully supported from imported fixtures alone.

For every milestone: establish the baseline; compare a simpler alternative; implement the complete path; run focused regressions/integration checks; inspect the actual diff; measure affected costs; update requirement coverage, commands, source revision, results, limitations and installation guidance. Run required full regression, packaging, installed-command, schema and compatibility checks on the final integrated revision. Publish or merge only with authorization for this implementation.

## Source references for the implementation lead

- `docs/guides/stack-tracing.md:226`: current terminal/browser workflow and bounds.
- `raven/tracing/ui.py:461`: selected-source reading/model preparation.
- `raven/tracing/ui.py:637`: current read rescheduling.
- `raven/tracing/timeline_ui.py:211`: controls; `:295`: current selected-thread rendering.
- `raven/tracing/timeline.py:22`: single-stream, 20,000-event visual projection.
- `raven/tracing/document.py:46`: static single-process browser coverage; `:162`: pointer interaction.
- `raven/tracing/protocol.py:19`: existing schemas and identity contracts.
- `raven/tracing/cpu_timeline.py:1`: CPU-placement fidelity contract.
- `raven/tracing/stream.py:20`: existing bounded disk-backed complete-prefix traversal.
- `raven/tracing/summary.py:153`: current aggregate limitations.
- `docs/guides/trace-capabilities.md`: implemented capture paths and qualification boundaries.
- `tests/test_trace_timeline_workflow.py:32` and `:88`: existing capture/view/export and live/completion assertions.
- `tests/test_trace_browser.py`: privacy, browser actions, integer precision, bounds and attempt isolation.
- `scripts/benchmark_trace.py`, `scripts/benchmark_trace_summary.py`, `scripts/benchmark_trace_integrity.py`: existing benchmark owners.
