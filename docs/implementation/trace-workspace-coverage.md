# Trace Workspace coverage and qualification ledger

This ledger reconciles the accepted twelve-section [design contract](trace-workspace-design-contract.md) with the current implementation. The [Trace Workspace guide](../guides/trace-workspace.md) documents the user workflow. The brief's twelve sections are mapped below without substituting portable rendering for hardware capture. Journals remain authoritative; an index, generated topology or imported fixture cannot prove complete execution, process membership or hardware qualification.

## Source and evidence boundary

- Worktree: `/workspace/Raven-trace-workspace`, branch `codex/trace-workspace`.
- Base: `8b317a085ed95cf041df318ac45370a7479a84b4`, retaining the independently verified Heavy-startup fix. The original checkout and other tasks' worktrees were preserved.
- Planning audit: fetched `origin/main` at `2bfe5795290575de0e1c4390f2166e89897902f6`; the audited `288a680` checkout had an identical tracked tree. Implementation uses current source, rather than assuming the planning snapshot contains subsequent changes.
- Existing baseline: 77 timeline/workflow/browser/CPU-timeline tests passed in 2.46 seconds before implementation. This is regression evidence, not qualification of new features.
- Frozen delivered runtime: `824a83423f7e1069c61b698764fb3e808e6b1284`. The remaining changes are documentation only. The preceding `3b8558d` runtime passed the full local suite; the final ASCII heading and qualification-runner corrections received the additional focused and installed checks below. Final PR CI checks cover the integrated documentation commit.
- Frozen-runtime full regression: **20,066 passed, 24 skipped, 4 warnings in 413.70 seconds**, `python -m pytest -q -n 2`, log `/workspace/trace-workspace-full-final.log`. Packaging, installed commands, controlled measurements and hosted CI are recorded separately below.
- Review dependency: [Heavy startup PR #6](https://github.com/rahel99x/Raven/pull/6). The trace draft is stacked on that fix; merging it into main is not part of this implementation authorization. [Branch CI runs](https://github.com/rahel99x/Raven/actions?query=branch%3Acodex%2Ftrace-workspace) identify the exact delivered HEAD.
- Raw logs/JSON remain outside `docs/implementation`, which intentionally contains Markdown only. Final reports should retain their artifacts and checksums with the reviewed revision.

Status meanings:

| Status | Meaning |
|---|---|
| Implemented / focused | Complete software path exists and focused acceptance evidence is identified; final integrated revision remains subject to Q06. |
| Reused / focused | Existing capability is retained, with behavioral tests preserved; it is not presented as a newly invented subsystem. |
| Adjusted | The requirement is implemented through an explicitly stated, evidence-preserving alternative. Its limitation is part of the contract. |
| Software pending | A required software path or integration is still missing or under review. This is not a hardware blocker. |
| Blocked qualification | Software evidence exists, but the named external environment/tool/device/permission must be supplied for the stated qualification. |
| Pending final gate | Final revision, integrated testing, measurement or publication evidence has not yet been supplied. |

## Evidence packages

References in the tables identify these concrete source/test owners. Test presence alone is not the final test result; exact integrated commands/results are recorded under Q06 when available.

| Code | Source and acceptance evidence |
|---|---|
| E0 | Existing `protocol.py`, `capture.py`, `timeline.py`, `cpu_timeline.py`, `stream.py`, `export.py`, `summary.py`; unchanged behavioral assertions in `test_trace_protocol.py`, `test_trace_auto_recording.py`, `test_trace_timeline_workflow.py`, `test_trace_timeline_ui.py`, `test_trace_cpu_timeline.py`, `test_trace_browser.py`, `test_trace_summary.py`. |
| E1 | `workspace_index.py`, `workspace_index_storage.py`, `workspace_index_statistics.py`, `workspace_index_alignment.py`, `workspace_index_filters.py`; `test_trace_workspace_index.py`, `test_trace_workspace_alignment.py` and `test_trace_workspace_filters.py`. Tests cover full retained history, cross-page pairing, exact integer times, private ownership, invalid sources, fair source registration, payload/query limits, incremental summaries, reuse and calibrated indexed queries. |
| E2 | `workspace_model*.py`; `test_trace_workspace_model.py`, `test_trace_workspace_model_flows.py`. Includes property checks against interval-union and affine-rounding oracles, producer/process/device scoping, independent clocks, scheduling-backed stack intersections, sample points, SMT unions and recursion. |
| E3 | `workspace_session.py`, `workspace_cache.py`, `workspace_cleanup.py`, `workspace_browser_effect.py`, `tracing/ui.py` and global navigation; `test_trace_workspace_integration.py`, `test_trace_workspace_review.py`. Actual journal → background index → terminal/global retained opening, completion, cache recovery and stale effects. |
| E4 | `workspace_ui*.py`, additive legacy `timeline_ui.py` controls; `test_trace_workspace_ui.py`. Published-state rendering, 80-column/ASCII/Unicode controls, Live/Explore/Fit, simultaneous lanes, source/thread filters, parent/child navigation, exact-event/bookmark restoration, per-attempt preferences and current-selection reports. |
| E5 | `workspace_browser.py`, `workspace_document.py`; `test_trace_workspace_browser.py`. Real HTTP/auth/origin/bounds and actual Chromium interaction, including journal arrivals, imported GPU/flow points, counter plot, calibrated clock selection, event links and causal-graph navigation. |
| E6 | `activity_import.py`, `activity_capture.py`, `activity_trigger.py`; `test_trace_activity_import.py`, `test_trace_activity_capture.py`, `test_trace_activity_trigger.py`. Real integrity journals plus bounded hostile input, physical/ordinal identity, optional/required failures, application exits, scheduled windows, rolling pre/post incidents, thread/fork ownership and never-triggered receipts. |
| E7 | `perf_capture.py`, `perf_capture_process.py`, `perf_import.py`; `test_trace_perf_capture.py`, `test_trace_perf_import.py`. Actual unavailable-tool execution/exit recovery, owned gated children, collector-only quota/duration/cancellation, strict report import, sample weights/unknown stacks and no rerun. Collector fixtures do not qualify live `perf`. |
| E8 | `workspace_analysis*.py`, `workspace_correlations.py`; `test_trace_workspace_analysis.py`, `test_trace_workspace_correlations.py`. Explicit dependency graph, cycles/missing endpoints, bounded causal slice, calibrated uncertainty, comparability/source-pair checks, exact efficiency formula, counters and evidence-linked investigation hints. |
| E9 | `workspace_symbols.py`; `test_trace_workspace_symbols.py`. Explicit binary/build-ID matching, bounded tools/private cache, unsafe addresses, unresolved symbols, source-root containment and content/Git proof; actual small ELF/demangling/source qualification where the local toolchain is present. |
| E10 | `profiles.py`, `workspace_capabilities.py`, launch options/planning/wizard; `test_trace_workspace_launch.py`, `test_trace_capabilities.py`. Reproducible project/topology/pilot settings, script preservation, separate scientific account/resources, capability/fidelity registry and bounded budget preview. |
| E11 | `scripts/benchmark_trace_workspace.py`, `test_trace_workspace_benchmark.py`; `scripts/measure_trace_workspace_ux.py`. Equivalent real-journal fixtures, correctness checks, process-isolated cost samples and actual browser freshness/input measurements. |
| E13 | `workspace_metrics_context.py`; `test_trace_workspace_metrics_context.py`. Owner-registered exact-attempt/clock/source/range capacity epochs, independent CPU/GPU readiness, weighted observed sample shares, effective allocation integration and filtered-denominator refusal. |
| E14 | `metrics_overlay.py`, narrow existing metrics-client/PythonCapture hooks; `test_trace_metrics_overlay.py`. Real application micro-API → same owned recorder → index/counter lens, unchanged JSONL output, no hook-time filesystem encoding, per-name/rate bounds, explicit coalescing and fork/thread/optional-failure isolation. |
| E15 | `reader_budget.py`, `workspace_index_memory.py` and normal `protocol.py` reader admission; `test_trace_reader_budget.py`. Shared retained object-state charges before growth, partial blocks/codebooks/checkpoints, resource-limited source release, exact once-only mid-page I/O prefix publication and descriptor ownership. |
| E16 | `workspace_discovery.py`, `workspace_follow.py`, Session/CLI/cache cohort/identity integration; `test_trace_workspace_follow.py`, `test_trace_workspace_cohort.py`, `test_trace_workspace_identity.py`, `test_trace_workspace_discovery.py`, and existing `test_research_reporting.py`. Metadata-only discovery with the index as sole journal verifier, explicit project following, late source windows, independent cadences, requested-source-only warm caches and redundant run-ID alias normalization. |
| E17 | Existing `capture.py` ordering/teardown corrections; `test_trace_workspace_capture_order.py`. Real CPU observer interleaving, detached batches/direct spans, no encoding/fsync under the callback queue lock, original timestamps preserved, bounded writer failure containment and a final real Slurm two-thread workload. |
| E18 | Baseline `evidence_index.py` cancellation publication guard; `test_evidence_index.py` / `test_mcp_evidence.py`. Cancellation is rechecked after child communication and reply decoding, with actual active-VM barrier, nonempty original child result and reaped-child assertions. The race was reproduced on base `8b317a0`, not excused as a flaky test. |
| E19 | Session `_functions_many`; `test_trace_workspace_function_batch.py`. All clock-domain function tables share aggregate 1-second SQL work and 8 MiB encoded-row budgets; cancellation, actual retained-byte counts, truncation and queried/unqueried ranges remain explicit. |
| E12 | README links, `docs/guides/trace-workspace.md`, `stack-tracing.md`, `trace-capabilities.md`, `examples/trace-lab/README.md`, `activity_window_lab.py`, `triggered_incident_lab.py`; exact commands, formats, headless workflow, qualification limits and runnable examples. |

Focused results already recorded:

- Final identity/requeue focused review: **43 passed**. Existing reporting run IDs are scoped by validated restart count (`research_reporting.py` identity/start paths); an actual filesystem start/restart exercise and workspace identity/discovery tests confirm separation. `_matches` rejects contradictory supplied Slurm submit/start anchors before considering a matching run alias.
- Baseline SQL cancellation review: **39 passed in 2.66 seconds**, covering the active-VM and reply-publication race without weakening nonempty-child/result and child-reaping assertions. The final full-suite result is recorded separately below.
- Frozen-runtime strict browser checks: **84 passed in 29.99 seconds** with `RAVEN_REQUIRE_BROWSER=1`; Python 3.11 focused compatibility: **185 passed, 2 expected monitoring skips in 19.26 seconds**. Final integrated release gates remain below.

- E1 + E2 + perf import: **146 passed in 3.06 seconds**, command: `pytest -q tests/test_trace_workspace_index.py tests/test_trace_workspace_alignment.py tests/test_trace_workspace_filters.py tests/test_trace_workspace_model.py tests/test_trace_workspace_model_flows.py tests/test_trace_perf_import.py`.
- E4 + E9 + E13 and preserved timeline/workflow: **112 passed in 1.97 seconds** using `test_trace_workspace_ui.py`, `test_trace_workspace_metrics_context.py`, `test_trace_workspace_symbols.py`, `test_trace_timeline_ui.py`, `test_trace_timeline_workflow.py`. A later truthful MPI-point-inspector addition passed **54 workspace UI cases in 0.26 seconds**.
- E7 collector: **20 passed** under Python 3.12 (3.20 seconds) and Python 3.11 (3.16 seconds), `pytest -q tests/test_trace_perf_capture.py`. Actual unavailable-tool behavior is retained in `/workspace/trace-perf-capture-qualification.log`; no live perf was present.
- E14 final isolated bridge cases: **11 passed** under Python 3.12 (0.76 seconds) and Python 3.11 (0.80 seconds), `pytest -q tests/test_trace_metrics_overlay.py`. An earlier combined client/vendor/protocol/automatic-capture/timeline/CPU run had **189 passed in 69.86 seconds**; its later private-runtime namespace correction is covered by the full regression pass below.

- E8 MPI bridge plus existing analysis: **44 passed in 0.23 seconds**, command `pytest -q tests/test_trace_workspace_correlations.py tests/test_trace_workspace_analysis.py`; actual integrity journals → index → collective point projection → authenticated browser analysis were exercised. These are simulated MPI observations in real journals, not live MPI qualification.
- E5 + E8: **69 passed in 8.76 seconds** using `python -m pytest -q tests/test_trace_workspace_analysis.py tests/test_trace_workspace_browser.py`, including actual Chromium; the later strict 84-test browser run revalidated the integrated browser paths.
- E6 + launch/capability checks: **94 passed in 0.46 seconds** using the five activity/capability/launch test files; the three activity files alone had **72 passed**. Ruff F checks and formatting passed for those capture owners.
- E12: five edited user documents had balanced fenced blocks, parseable JSON examples and **78 existing relative file targets**. This checks documentation consistency, not execution of every command on every site.
- Actual CPU framework capture and local-browser latency results are recorded separately below. They are not GPU, live Slurm or universal performance evidence.

## Requirements and acceptance gates

### Session, identity and compatibility — brief sections 1, 3 and 8

| ID | Requirement and acceptance disposition | Evidence | Status |
|---|---|---|---|
| S01 | Source/instructions/pending fixes were reconciled on an isolated branch and the independently verified Heavy fix retained. Runtime is frozen at 824a83423f7e1069c61b698764fb3e808e6b1284; integrated release and PR evidence is recorded under Q06/Q08. | Source boundary above; E3; final review below | Implemented; release evidence below |
| S02 | Reuse authoritative v1 journals/hash-chain verification, bounded readers, existing timeline/export/summary paths and application background/cancellation brokers. No second unverified event reader. | E0, E1, E3; legacy assertions retained | Reused / focused |
| S03 | Exact cluster/job-attempt/run/node/boot/process-start/stream scope is preserved. Validated Slurm restart count creates distinct reporting attempts; contradictory supplied submit/start anchors veto run-alias association. Redundant run_id==attempt normalization changes only the common cohort key, never raw identities or explicit expected constraints. Distinct run IDs and unrequested cached peers cannot join silently. | E1/E2 identity; E6 reports; E16 actual start/restart and workspace cohort cases; 43 focused review checks | Implemented / focused |
| S04 | Physical thread reuse has existing per-thread UUIDs and explicit index lifetime/reset handling; fork children cannot append to parent streams. Declared logical-task identity/generation has separate model lanes and pre-limit filters. Automatic asyncio task lifecycle/causal capture is not implemented or advertised; missing legacy metadata stays unknown. | E0 thread/fork tests; E1 explicit generation/boundary/facet tests; E2 logical-task/process reuse | Implemented declared metadata; automatic async adapter not implied |
| S05 | IDs derive from source identity and original event sequence; range paging/live arrivals keep selection. Generation/session checks reject obsolete callbacks and cancelled browser effects. Owned browser ingestion continues after leaving the terminal Trace pane; metadata following and verified source subscriptions survive temporary discovery failure. | E1 stable detail; E3 stale-effect review; E4 completion/preferences; E16 late-source/cancellation tests | Implemented / locally validated; hosted checks under Q06 |
| S06 | Existing bounded v1 schemas remain compatible. Descriptive data is retained without executing it; identity, time, calibration, correlation, capacity/action semantics need explicit validators. Malformed optional text cannot inject terminal/HTML controls. | E0 protocol/summary contracts; E1/E2/E6/E8 validators; E5 hostile labels | Implemented / focused |
| S07 | View preferences/bookmarks are bounded and stored separately from raw journals; run/attempt changes cannot carry source selection across identities. Returning to a saved exact attempt restores its context. | E4 preferences roundtrip/history/malformed-entry tests; E3 | Implemented / focused |

### Live, Explore, Fit and interaction — brief sections 1 and 6

| ID | Requirement and acceptance disposition | Evidence | Status |
|---|---|---|---|
| V01 | One Session/Index query model supplies both terminal and live browser; detail retains the original ID, clock and integer times, including underlying records for CPU projections/samples. | E1, E3 real journal integration; E4 raw detail; E5 real HTTP | Implemented / focused |
| V02 | Live follows fresh committed bounds over Δ. Display span, checkpoint, known-journal ingestion, metadata discovery, browser polling and Slurm polling remain distinct. Fixed CLI paths never silently become project-wide subscriptions; --follow-project explicitly adds same-attempt late processes/windows. | E3 fresh-tail/all-clock tests; E4 modes; E5 Chromium arrivals; E16 late sources | Implemented / focused |
| V03 | Explore holds its explicit viewport; pan/zoom/selection pauses following. Fit uses the whole verified current-clock run extent (each raw domain separately in multi-clock mode), preserving cross-resource context instead of recomputing resource-facet-specific bounds/density. Focus zooms the selected event; Resume restores stored Δ. | E3 explicit CLI range; E4 clock/reference Fit; E5 range/Live/Fit | Adjusted run-context Fit versus event Focus / focused |
| V04 | Pausing display does not stop recorder/index ingestion or metadata following. Scientific jobs and independent recorders outlive viewer shutdown. Browser closure cancels owned discovery/read work; resource or I/O failure does not terminate scientific execution. | E0 workflow; E3 owned cleanup; E5/E16 real arrivals/cancellation; E7 collector isolation | Implemented / locally validated; hosted checks under Q06 |
| V05 | Multiple thread/activity lanes appear simultaneously; terminal pages bounded visible rows, browser virtualizes visible lanes and navigator pages. Independent overlapping X activities are not invented nesting. | E2 overlap lanes; E4 multiple threads; E5 actual lanes | Implemented / focused |
| V06 | Zoom/pan, overview range selection, zoom-to-selection and exact-event restore work; Fit uses bounded density then exact drill-down. Browser scope breadcrumbs clear filters. Terminal exposes lineage through its expandable hierarchy and recorded parent/child navigation, deliberately avoiding an additional dense horizontal row at 80 columns. | E1 LOD; E4 overview/parent-child/hierarchy; E5 real range/flamegraph/scope breadcrumb | Adjusted responsive breadcrumb/lineage presentation; focused |
| V07 | Source/node/process/thread/rank/task/CPU/GPU/event-kind/search filters apply typed indexed predicates before result limits. Bounded facets disclose omitted choices; terminal More/Filters and browser choices use the same query scope. Stable identity-based hierarchy collapse/pin controls avoid truncated-label collisions. | E1 facet/filter tests; E4 real indexed filter/navigation; E5 controls | Implemented / locally validated; hosted checks under Q06 |
| V08 | Browser hover shows full safe details; click pins stable event identity. Inspector keeps original times/provenance, uncertainty and raw detail while arrivals continue; stale detail/tree responses are rejected. | E5 actual Chromium selection/hostile names/clock tests | Implemented / focused |
| V09 | Terminal click/key selection, next/previous/focus/parent/child and accessible browser event/detail tables provide a route without hover. Ambiguous nesting is refused. | E4 navigation/selection; E5 accessible event list, causal graph | Implemented / focused |
| V10 | Compact controls reveal More progressively; 80-column and narrower ASCII/Unicode layouts have bounded painted hitboxes, safe wrapped text and explicit fidelity labels rather than color-only meaning. Browser canvas has accessible table/detail alternatives. | E4 width/control tests; E5 real Chromium | Implemented / focused; general accessibility audit not claimed |
| V11 | Open/unmatched/lost/corrupt boundaries remain unknown. Missing calls are not closed at viewport edges; quarantined sources disappear from intervals/statistics/density. Placement/sample points do not become continuous slices. | E0/E1 pairing, loss/quarantine; E2 samples; E6 evicted incident span | Implemented / focused |
| V12 | Job Trace, global :traces, History and explicit retained opening use the same run session. Event/bookmark restore, private range links and exact-run notification navigation preserve scope. No permanent cross-session/public URL is implied. Navigation regression asserts the new Traces destination in complete forward/back cycles. | E3 global/notification paths; E4 exact events/preferences; E5 links; preserved navigation assertions | Implemented / focused |
| V13 | Completion pauses following once, preserving selected ID, viewport, expansion and run association instead of fitting or scrolling. A later explicit Resume is honored even after completion, so later committed evidence can be followed. Older standalone/aged-out recordings remain openable. | E0 workflow; E3 completion; E4 state tests; E5 browser completion; E16 late windows | Implemented / focused |

### CPU/node/GPU topology and time — brief sections 2, 4 and 5

| ID | Requirement and acceptance disposition | Evidence | Status |
|---|---|---|---|
| T01 | Execution projection scopes node/process/rank/thread/declared logical task/stack by lifetime, including reported process metadata. Missing async metadata is unknown; no complete automatic logical async stack is inferred from a physical thread. | E2 lane/process/task reuse; E1 predicates; E4/E5 | Implemented / focused |
| T02 | CPU lanes identify node/boot/socket/available die/NUMA/core-type/core/logical CPU. Same numeric IDs across sockets/nodes/boots do not merge; identity-based path IDs remain distinct even when readable labels truncate. Unknown topology is not zero. | E0 CPU observations; E2 topology/boot/path-ID tests | Implemented / focused |
| T03 | Valid same-owner/thread/clock recorded running slices intersect call boundaries and split at migrations. Sparse `sched_getcpu` points remain bracketed observations. No live kernel scheduling collector is implied. | E2 migration/owner/slice tests; E1 typed scheduling tests | Implemented projection/import; live scheduling capture not offered |
| T04 | SMT union math and matching effective capacity/coverage are validated. Owner-registered resource epochs integrate changing effective logical-CPU counts over the selected clock/range/source scope. Incomplete affinity/allocation coverage or facet-filtered numerators refuse fractions; CPU brand/host count/call elapsed cannot supply denominators. | E2 SMT/boot tests; E13 capacity epochs/filter refusals; CLI/Session/browser registration | Implemented / locally validated; hosted checks under Q06 |
| T05 | Node/boot/device UUID/partition and process/context/queue generations scope GPU lanes. Report-local ordinals remain local; physical merging needs sufficient identity. Host calls stay separate. | E2 device/context tests; E6 remapping/partition fixtures; E5 imported canvas | Implemented / focused |
| T06 | No/one/multiple/unequal/mixed/partitioned device metadata and unknown physical identities stay usable; missing GPU data does not hide CPU streams. Device/source loss has visible health; physical loss/reconnect still needs live qualification. | E6 topology fixtures; E2 identities; E5 two-node activity view | Software implemented; physical qualification blocked |
| T07 | Original clock and sequence are retained. Integer nanoseconds survive SQLite and JSON/JavaScript without float conversion; raw unrelated domains default to separate axes. Same owned producer-session windows can share a validated process-lifetime domain. | E1 integer/filter tests; E2 clock tests; E5 uint64; E6 real CPU windows | Implemented / focused |
| T08 | Affine transforms validate anchors/rates/validity/drift/uncertainty; reference rows are indexed before range/limit filtering, raw values remain, out-of-range rows stay raw. Reference selection is explicit. Actual calibration measurement is not performed by the viewer. | E1 actual aligned journals; E2 property tests; E4/E5 reference UI | Implemented; calibration efficacy requires external evidence |
| T09 | Scoped explicit flow/link/dependency records and supported PMPI markers reach the causal graph. The existing collective auditor validates WORLD generation/members/signatures and unique ordered process/thread phases. Barrier and positive SUM Allreduce justify input/entry → return edges; positive Bcast justifies root-input → receiver-return plus local pairs, never an implicit barrier. Zero-count/unsupported data dependencies stay local. Bare profiler IDs retain an explicit unresolved reason; only validated typed mappings or explicit flows justify host/device links. | E2 flow tests; E8 actual journal/HTTP bridge and ambiguity/zero-count/fanout tests | Implemented supported semantics; generic raw correlation remains unqualified |
| T10 | Host and device intervals are distinct; explicit endpoint-delay bounds retain clock uncertainty. True GPU queue delay needs proven enqueue/end/correlation semantics; generic launch-entry delay is labeled as such. | E6 activity kinds; E8 typed links/delay tests | Adjusted: observed endpoint delays; true queue timing unavailable without evidence |
| T11 | Unknown clocks stay local; no automatic first-event-zero or wall-clock alignment. Manual visual alignment is not introduced as an alternative source of precision. Observed-chain timing never asserts an unknown global critical path. | E2/E8 invalid/unaligned tests; E4/E5 raw default | Implemented / focused |

### Capture, import and developer workflows — brief sections 2, 9 and 10

| ID | Requirement and acceptance disposition | Evidence | Status |
|---|---|---|---|
| C01 | Fixed built-in capability registry distinguishes implementation/prerequisites/usable/qualified and fidelity/scope. Metadata probing neither starts a collector nor certifies a remote host. Trace content cannot select executable plugins. | E10 registry/capability tests; E12 vendor matrix | Implemented / focused |
| C02 | Existing automatic monitoring, explicit legacy profile consent, native SDK, selected C MPI instrumentation, helper/rank metadata and CPU observations are retained. These are not universal native/GPU instruction capture. | E0 existing regression; E10 launch settings | Reused / focused |
| C03 | Bounded TraceEvent JSON import handles X/B/E, markers, counters and explicit flows; exact decimal timestamps/source SHA are retained. Duplicate keys, huge/deep/malformed/symlink inputs and quota failures reject without publishing a successful complete source. | E6 importer tests; E5 actual import → index → canvas | Implemented / focused |
| C04 | Optional PyTorch CPU/CUDA/HIP activity collector reaches private reports → registered verified journals → indexed terminal/browser views. Cooperative steps export live windows; generic wrappers export at completion. Actual CPU pilot is qualified below; no physical GPU was available. | E6; actual CPU logs; E3/E5 | Implemented; physical GPU qualification blocked |
| C05 | Import-first shared standard JSON path supports declared NVIDIA/AMD/Intel/Apple activities; Torch offers CUDA/HIP where its runtime supplies them. Dedicated vendor binary decoders/device-stack collectors are explicitly not implemented, not hidden as hardware-only blockers. Candidates are not advertised as working adapters. | E6 multi-vendor fixtures; E10 registry; E12 matrix | Adjusted shared-import scope; vendor-specific software/qualification not claimed |
| C06 | Owned Linux `perf` collector/importer records statistical frame-pointer callchains, periods, observed CPU and unknown/lost stacks. Preflight/gated ownership, duration/quota and once-only launch are tested. This is sampling, not continuous scheduling, off-CPU reconstruction or hardware decoding. | E7 actual missing-tool behavior and controlled collector fixtures | Implemented; live `perf` qualification blocked |
| C07 | Optional failures preserve application output/environment/exit/exception and never rerun science. Capture-required is explicit and stops before launch if prerequisites fail. After launch, recorder failure cannot silently kill/restart the job. | E6 application/required/error tests; E7 once-only/signal/quota tests | Implemented / focused |
| C08 | Capture adds no artificial device-wide synchronize/barrier; device events remain provider timings. The example's energy scalar check may itself wait for its scientific result and is disclosed. Existing supported MPI semantics are retained. | E6 fake-provider synchronize trap and code review; E12 example | Implemented / focused; physical overhead unqualified |
| C09 | Launch wizard/CLI project/topology/pilot presets, per-process/run/process-count budget preview and reproducible command preserve scripts. Presets bound retention/attachments, not scientific memory/CPU or slowdown. | E10 launch tests; E12 commands | Implemented / focused |
| C10 | Login vs billing account and scientific-job vs Heavy resources remain separate. Managed instrumentation requires independently verified owned local Slurm placement; no extra allocation, scheduler replacement or login-host execution fallback. | E10 account test; existing admission owner; actual top-level refusal below | Reused / focused; real cluster launch qualification blocked |
| C11 | Existing Python/native spans and metrics APIs remain. Configured Python micro-API measurements/progress/phases automatically enqueue bounded counter/phase evidence into the same active owned recorder, exact attempt/thread/clock, while preserving JSONL/scientific output. No new mandatory dependency or second capture owner. | E0 hooks; E6 examples; E14 real-app bridge and vendored runtime tests | Implemented / focused |
| C12 | Cooperative TriggeredCapture retains bounded immutable pre-event ring, original times, explicit first trigger, bounded post time/events/journal, durable never-triggered/eviction/error receipts. Deadline stops capture only; fork/thread ownership and missing span halves stay explicit. | E6 trigger tests; actual runnable incident smoke | Implemented / focused |

### Full retained data, statistics and analysis — brief sections 7, 8 and 10

| ID | Requirement and acceptance disposition | Evidence | Status |
|---|---|---|---|
| D01 | Full committed history beyond the old 20,000-event display window is indexed on disk; early/middle/late and cross-page intervals remain queryable. Selection uses original stable IDs. | E1 full-run/page tests; E3 fresh-tail archive | Implemented / focused |
| D02 | Disposable versioned SQLite index has a private single writer and safe replay/rebuild. Current owners expose only requested/explicitly followed source cohorts, not cached peers. The default has four independent owner slots; multiple dashboards may use separate copies, while a fifth active default owner is refused with close-viewer/explicit-index guidance. Only owned disposable databases can be repaired/pruned. | E1 revalidation/foreign DB; E3 cache tests; E16 cohort tests | Adjusted safe replay/rebuild and bounded independent ownership / focused |
| D03 | Committed-page ingestion has event/time/cancel limits and fair registration. Shared reader-state admission precedes metadata/partial-block/event/codebook/stack growth. Resource limits preserve the indexed verified prefix, release that reader and allow other sources to progress. Mid-page I/O publishes already verified events exactly once and retries normally without corruption classification/backoff. | E1 cancellation/retry; E15 memory/partial-I/O/descriptor tests; E16 discovery recovery | Implemented / focused |
| D04 | Range overlap uses coarse candidates plus exact integer predicates; 17 incremental base-16 density levels avoid full-run scans for Fit. The whole-run overview has at most max(requested bins, domain count) buckets and means interval-start counts in its declared source/clock scope. Resource-facet filtering does not make it a filtered busy-time graph or recompute its extent. | E1 range/LOD/total-bin tests; E11 | Implemented; controlled costs recorded below |
| D05 | Display/query limits never delete raw capture. Retained reader working-state is shared across sources: 16 MiB Light / 64 MiB normal; database budgets are separate 128/512 MiB. Neither is an RSS cap. Interval+marker payload, source catalog and function rows each have separate 8 MiB bounds, not a single overall-response guarantee. Partial/resource/quota states remain visible. | E1 payload/detail tests; E6 capture budgets; E15 shared admission and raw-hash preservation | Implemented / focused |
| D06 | Paint performs no file/scheduler I/O. Workspace discovery reads registration/manifest metadata only; Index is its sole authoritative journal verifier. Owned project following checks metadata every 2 seconds independently of 0.5-second known-journal browser ingestion and Slurm polling. Up to 512 sources/64 warnings and shared all-clock result/work bounds remain explicit. All-clock function requests share one aggregate 1-second SQL deadline and 8 MiB encoded-row budget, check cancellation between bounded calls and report actual retained bytes plus queried/unqueried ranges and truncation. | E3/E4 no-paint-I/O; E11 direct zero RPC; E16 sole-reader/cadence/late-source tests | Implemented / focused |
| M01 | Matched inclusive/self elapsed uses synchronous pairing and child-interval union; independent async/GPU overlap is not subtracted. Count/min/max and existing bounded summary/quantile contracts remain available, with uncertainty/partial scope stated. Concurrent totals are not makespan. | E1/E2 statistics and recursion; E0 summary quantile tests | Implemented / focused |
| M02 | Point-only stacks preserve weights/methodology. CLI/terminal/browser expose inclusive/leaf weighted shares per observed source/process/event/unit, including unknown-stack denominator; bounded work exhaustion disables incomplete shares. Recorded blocked/runnable scheduler states have explicit union durations; sampled absence never establishes off-CPU time. | E2 state/sample tests; E7 importer; E13 shares and user paths | Implemented / locally validated; hosted checks under Q06 |
| M03 | Scheduler running unions avoid duplicate SMT charging and remain node/boot scoped under reference clocks. Explicit --capacity / terminal owner-context registration validates attempt, source, clock, range and effective epochs. Time-varying allocation denominators integrate covered capacity; execution filters cannot reuse a whole-scope denominator. | E2 capacity/SMT/boot tests; E13 calculations/rejections; Session/browser context | Implemented / locally validated; hosted checks under Q06 |
| M04 | GPU engine/device busy duration uses interval union, excluding host launch time. Explicit per-domain coverage context can supply fractions independently of CPU readiness. Occupancy/throughput/memory-counter definitions remain adapter-specific and unavailable when absent; kernel duration cannot supply them. | E2 GPU union/capacity; E6 counters; E13 independent readiness/context | Implemented; physical counters unqualified |
| M05 | Baseline comparison requires explicit workload/input/build/scope/resource provenance. `T1/(P*TP)` needs a single equivalent-resource baseline; missing or inconsistent declarations produce unavailable rather than fabricated efficiency. Source pairing/build validation prevents same-name binary merges. | E8 comparison tests; E3 baseline Session; E5 registered baseline route | Implemented / focused |
| M06 | Validated call-tree/bottom-up aggregation preserves recursion and activation domains; browser aggregate flamegraph is labeled nonchronological and has an accessible table. Terminal analysis reads published background results. | E2 recursion/activation tests; E4 reports; E5 Chromium flamegraph | Implemented / focused |
| M07 | Imported/cooperative and same-recorder micro-API counters/phases retain exact value/unit/source/clock/gap scope and draw points without interpolation. Metrics hook coalescing/drops are explicit (20 observations/s per group, 64 metric names). Legacy epoch-only scientific or CPU/memory recordings lack proven attempt/boot/calibration anchors and remain unalignable rather than guessed into a trace; their existing Analytics views remain usable. | E8 lens; E5 actual plot; E14 real-app bridge; limitation documented | Implemented supported clocked path; legacy cross-clock overlay explicitly unavailable |
| M08 | Bounded investigation hints cite evidence IDs, assumptions and proposed checks. They diagnose missing evidence/observed patterns, not unproved idle time, GPU occupancy, optimization cause or experimental efficacy. | E8 evidence/hint tests; E4/E5 analysis UI | Implemented / focused |
| M09 | Explicit selected binary is build-ID checked; bounded readelf/addr2line can demangle and cache. Source contents require contained root plus digest/exact Git proof. No trace-supplied DSO path is executed or silently resolved against unrelated current source. | E9 actual ELF/source and adversarial tests; E3 CLI/TUI paths | Implemented / focused |
| M10 | Exact event/range links, saved bookmarks/named annotations and per-attempt preferences are separate evidence. Registered baseline context is explicit. Browser URL token/session expires with viewer; links are not permanent public artifacts. | E4 preferences/history/bookmarks; E5 URL restore/comparison | Implemented / focused |
| M11 | Causal slice diagnoses missing endpoints, cycles, bounds and unaligned clocks. Supported MPI/explicit producer declarations contribute only validated dependencies. The observed chain maximizes supported link count through selection; it is not a proven global critical path or simulated ground truth. | E8 causal/MPI bridge; E5 actual dependency graph | Adjusted evidence-chain analysis / focused |

### Browser, security, delivery and qualification — brief sections 9, 11 and 12

| ID | Requirement and acceptance disposition | Evidence | Status |
|---|---|---|---|
| B01 | Original private offline HTML export/CSP/exclusive-file/integer protections remain. Snapshot coverage is bounded and labeled, separate from the live service's whole-retained-run range access. | E0 export/browser/workflow; E5 strict live CSP; E12 | Reused / focused |
| B02 | Browser action explicitly starts owned readonly loopback service with Bearer session access, exact Host/Origin and bounded registered-source routes. No public bind, arbitrary paths/job controls or HTTP-provided baseline/project path. Private owner project following keeps late streams discoverable while terminal view is hidden; shutdown cancels only owned viewer work. | E5 HTTP/security; E3 lifecycle; E16 independent follow/cancellation | Implemented / focused |
| B03 | Exact URL token fragment stays private; headless/SSH does not pretend remote browser launch opens the laptop. Same-port loopback SSH forwarding and private offline transfer are documented; compute-node route is site-specific. | E5 headless test; E12 reviewed commands | Implemented / focused; actual remote route unqualified |
| B04 | Reports/strings/paths/clock/correlation semantics are bounded untrusted data. No code/plugins/SQL extensions or report-supplied command execution; private files reject symlinks/special files. | E0/E1/E5/E6/E9 adversarial tests | Implemented / focused |
| B05 | Source CPU/memory/GPU availability and metric readiness remain independent; an unavailable domain cannot hide valid others. Metadata/partial-read errors preserve the verified prefix. Actual complete source errors reach deduplicated exact-run notifications and verified recovery resolves them. Discovery error and recording failure remain distinct from integrity corruption. | E1 read/quarantine; E3 notification/lifecycle review; E5 recovery; E15/E16 partial source tests | Implemented / locally validated; hosted checks under Q06 |
| Q01 | Process-isolated equivalent-fixture benchmark separates CPU/wall/I/O/RSS from Python allocations and refuses false legacy whole-run comparisons. Controlled integrated runs and regressions are recorded below. Application slowdown/event rate/storage/scheduler/device qualification require separate measured fixtures. | E11 and interpretation below | Measured locally; external limits explicit |
| Q02 | Actual local browser p95 input/selection and committed-to-visible goals measured on the documented small healthy fixture; terminal/large/remote/GPU UX are not inferred. | E11 actual Chromium; numbers below | Local targets met on the frozen runtime fixture |
| Q03 | Actual local CPU framework report → integrity journal → index/projection exercised independently from generated fixtures; original Python recorder has existing real-interpreter tests. | Actual CPU evidence below; E0/E6/E7 | CPU framework path qualified for recorded versions only |
| Q04 | Actual disposable one-node Slurm 26.05.5 / Python 3.11.2 qualification passed 16/16 recorded checks on the runtime fingerprint below. It covers lab billing vs login, two-thread live/completed tracing, CPU points, selection continuity and owned Heavy placement startup. Multi-node/MPI, production SSH/site policy, cgroup enforcement and full Heavy readiness/analysis remain unqualified. | r10 manifest/execution context below; E17 deterministic ordering regressions | Single-node recorded checks qualified; remaining external gates explicit |
| Q05 | Actual NVIDIA/AMD/Intel/Apple device capture/counters need physical device/tool/version and permitted representative workload. Import fixtures and CPU Torch do not qualify these. | E6 software fixtures; external matrix | Blocked qualification: physical devices, producers and permissions |
| Q06 | Required full regression, quality/architecture, schema, packaging, installed commands, compatibility and CI must cover final integrated revision; current focused tests cannot substitute. | Final gate section below | Local gates passed; final hosted checks tracked on PR |
| Q07 | Every identified software integration/review finding is resolved in the frozen runtime: filter/range/baseline/stale effects, helper-only metrics, resource admission, cohorts/follow/completion, original timestamp ordering, requeue association and baseline SQL cancellation publication. Behavioral assertions are preserved with complete fixture context. Final full-suite/diff/release binding is Q06. | E3/E13/E15–E19; real Slurm; final skeptical review below | Software review closed; local gates passed; hosted checks under Q06 |
| Q08 | Reviewed-branch installation below uses the existing no-sudo/no-pipx installer and a separate clean checkout to preserve local edits/config/state. Final PR/publication is tracked by Q06; installed-package evidence is recorded below. Historical main approvals do not authorize merging this implementation. | E12; installation guidance below; integration lead | Guidance and local package gates passed; publication tracked by PR |

## Accepted-contract traceability and milestone sequence

| Brief section | Requirements retained | Current dependency/qualification disposition |
|---|---|---|
| 1. Product/Live/Explore/Fit/lifecycle | V01–V04, V13, S05, D06 | Shared Session and state semantics; lifecycle regressions receive explicit final review. |
| 2. Portable viewing/capture/fidelity/failure policy | C01–C08, T05–T08, B04 | Built-in import/optional collector paths; capture support is independent from viewer portability. |
| 3. Run identity, stale results, schema and preferences | S02–S07 | Exact-attempt sources, physical thread/declared logical-task lifetimes and bounded separate preferences; no automatic complete async adapter claim. |
| 4. Execution/CPU/GPU topology | T01–T06, V05, V07 | Lifetime-based lane/path identities, scheduling-backed slices, explicit points and unknown metadata preserved. |
| 5. Clocks/order/causality | T07–T11, M11 | Raw default axes, opt-in calibrated index and explicit supported protocol links, never guessed global ordering. |
| 6. Interaction/accessibility/global navigation | V05–V13, M09–M10 | Compact terminal lineage and browser breadcrumbs are a responsive presentation choice; links remain exact-attempt/session scoped. |
| 7. Defined timing/resource/scientific metrics | M01–M08, C11 | Epoch-scoped capacity, actual observed sample shares, independent readiness and supported same-clock overlays; unavailable evidence remains unavailable. |
| 8. Retained/index/query/retention/cadence | D01–D06, S02, S05 | Verified journal foundation, local single writer, bounded ranges/LOD/cache and safe replay/rebuild instead of trusting unchecked persistent cursors. |
| 9. Browser/remote/profiles/DevX/trust | B01–B05, C09–C11, M09 | Read-only private service plus offline export, explicit headless forwarding, existing account/profile/launch owners reused. |
| 10. Analysis/counters/comparison/symbols/incidents/recovery | M06–M11, C12, B05, S07, D02 | Actual helper-to-user paths were audited; difficult requirements remain represented with explicit evidence/limits. |
| 11. Ordered delivery milestones | S/D/V foundation → T CPU/clocks → C import/GPU → M analysis | Software foundations precede dependent renderers and analyses. Independent actual CPU producer qualification ran while topology/viewer work continued; physical GPU/site gates are separate. |
| 12. Adversarial/property/integration/performance/release gates | E0–E19, Q01–Q08 | Focused evidence and actual local results are distinct from final integrated revision/CI and external qualification. |

Milestone 1 (session/query) and milestone 2 (indexed Live/Explore) supply the
shared foundation for both viewers. Milestone 3 implements CPU/node projections,
metadata and calibrated reference queries; live scheduler-residency capture is
not fabricated from samples. Milestone 4 implements the complete optional
framework/import software path and actual CPU producer exercise; its physical
GPU qualification remains Q05. Milestone 5 provides common-format mixed-device
federation and explicit vendor capability boundaries. Dedicated binary vendor
and hardware-control-flow decoders are outside the accepted portable-viewer
priority, remain **unimplemented**, and require both software work and actual
platform qualification before being offered. Milestone 6 adds analysis,
comparisons, source proof, recorded metrics, incident capture and documentation.
These distinctions do not turn missing software into a credentials-only blocker.

## Actual local qualification evidence

### CPU framework adapter

The actual application-interpreter wrapper ran a diffusion workload using isolated **PyTorch 2.6.0+cpu / Python 3.12.14**. The final pilot used 3 explicit steps over 2 seconds, 64 field points and 2 sweeps, exporting scheduled 2-step windows. It completed numerically with no capture errors. Two registered, verified journals indexed on one owned producer-session clock produced **223 matched intervals, 8 markers, 18 projected lanes and 46 function aggregates**. The retained reports/journals/manifest accounting was **284,084 bytes**; this is not a measurement of profiler peak memory.

Evidence files:

- `/workspace/trace-workspace-torch-cpu-session.log`: actual wrapper/application/receipt.
- `/workspace/trace-workspace-torch-cpu-index.log`: actual integrity/index/projection assertions.
- `/workspace/trace-workspace-torch-cpu-pilot.log`: preserved initial failure from legitimate empty global-marker PID/TID values; importer now retains unknown identities.
- `/workspace/trace-workspace-torch-cpu-adapter.log`: preserved later PyTorch replay-stack assertion with scheduled `with_stack=True`; not hidden as a successful pilot.
- `/workspace/trace-workspace-torch-cpu-rerun.log`: top-level managed CLI correctly deferred on the interface host because verified owned Slurm placement was absent. Adapter qualification then exercised the explicit application-interpreter wrapper directly as a local test; admission was not weakened.

Framework replay stacks are now separate explicit `--framework-stacks` opt-in; default operator/activity recording succeeded. This change avoids claiming fragile provider Python replay as universal native/device stack capture. Optional recording errors preserve application behavior; capture-required remains explicit. No physical GPU activity was recorded by this CPU-only qualification.

Reproduction command for the successful local adapter exercise (not a managed-site admission bypass):

```bash
PYTHONPATH=/workspace/Raven-trace-workspace /workspace/Raven/.venv/bin/python \
  -c 'import sys; from raven.tracing.workspace_cli import profile; raise SystemExit(profile(sys.argv[1:]))' \
  --project /workspace/trace-torch-cpu-session-CekA1W \
  --job-id torch-cpu-pilot --attempt cpu-actual-tool \
  --python /workspace/trace-workspace-torch-app-env/bin/python \
  --window-steps 2 --max-windows 3 -- \
  examples/trace-lab/activity_window_lab.py --backend torch \
  --seconds 2 --steps 3 --size 64 --sweeps 2
```

Use a new project/attempt when reproducing; imported source IDs are exclusive and intentionally refuse duplicate overwrite. The ordinary end-user command is `raven trace profile` in its admitted execution environment, as documented in the guide.

### Actual one-node Slurm qualification

The final recorded disposable-scheduler exercise used **Slurm 26.05.5 / Python
3.11.2**, login `raven`, billing account `labx`, one node and a two-CPU scientific
allocation. **16/16 checks passed**. The original scientific Bash script was
preserved; its exact numeric job receipt matched durable metadata. Verified
intervals and an 80-column terminal workspace were visible while the real job
ran. Two scientific threads remained independently inspectable; actual
`sched_getcpu` observations remained points. Completion verified the retained
recording and preserved the selected event/range while entering Explore.

The owned Heavy WorkerClient probe reached **placement** with no startup error
and empty stderr. This is deliberately not full verified Heavy readiness or
analysis qualification. The scheduler used `task/none`; cgroup enforcement was
not qualified. Physical GPU, multi-node, MPI and production SSH/site policy
also remain unqualified.

The real multithread exercise exposed an existing capture-ordering fault:
taking a timestamp before a CPU observer released the GIL could let another
thread enqueue a later timestamp first. The correction serializes timestamp,
CPU observation and queue admission, preserves original times, coordinates
batch/direct-span writer ordering and contains queued metadata failure. It does
not clamp timestamps, weaken the reader or move encoding/fsync under the
callback queue lock. E17 retains deterministic reproductions and failure-path
checks; controlled instrumentation measurements below retain equal event fidelity. CPU-placement-specific application overhead still needs a separate representative workload.

Evidence:

- `/workspace/trace-workspace-slurm-qualification-r10/run-artifacts/results/manifest.json`:
  status `passed-for-recorded-checks`; actual commands, stdout/stderr hashes,
  live/completed query artifacts and copied runtime source hashes.
- Runtime aggregate SHA-256:
  `56aa47d1c8ad754b137fdaad37ab60d9d9e93ca50e60890960f9c5e1d0bcf365`.
- Qualified `capture.py` SHA-256:
  `740ac3faefe6383093d6d38c94a92bd628deb6bef7228d694adf3a9a82d98c2f`.
- `/workspace/trace-workspace-slurm-qualification-r10/execution-context.json`:
  the copied runtime matches frozen commit
  `824a83423f7e1069c61b698764fb3e808e6b1284` by its recorded per-file hashes.
  Documentation-only working-tree edits are outside that runtime qualification. Package checks ran concurrently: **14.538 seconds**
  qualification elapsed and **8.035 seconds** scientific elapsed (two threads,
  100 iterations each) are correctness timings, not controlled benchmarks.

This qualifies the recorded one-node path on that exact runtime commit/fingerprint,
not every cluster configuration. Q06 records the additional package, full-suite
and final integrated PR checks; documentation does not change these runtime hashes.

### Browser input and committed-data freshness

`/workspace/trace-workspace-ux-release.json` records actual headless Chromium on Linux x86_64/glibc 2.41, Python 3.12.14, five reported logical CPUs, Node and `/usr/bin/chromium`. Fixture: 16 lanes, 64 warm intervals and 20 newly committed intervals; index ingestion 0.5 seconds and browser catalog polling 1 second. Twenty observations were taken for each action.

| Quantity | p95 | Scope |
|---|---|---|
| Exact event selection to two animation frames | 32.2 ms | Local accessible event-list selection; exact ID asserted |
| Pan input to two animation frames | 32.5 ms | Local browser input/drawing |
| Keyboard pan to two animation frames | 32.6 ms | Focused timeline keyboard input |
| Committed journal to visible new interval | 1,000.5933 ms | Actual local journal → Session/index → loopback browser |

The report records the same before/after SHA-256 `145273b47ff7c4b082cb9102ca0ec7b8f78f870f8c3b97be1d3f9fd661970a5d` over its 44 listed source files during that measurement window. The listed runtime sources match frozen revision `824a834`; no concurrent tests, builds or Slurm qualification ran in this controlled window. Earlier pilot JSON remains preserved.

This fixture met the initial local p95 targets of 100 ms input and 2 seconds freshness. These are observed results, not hardware-independent guarantees. The direct fixture made **zero scheduler RPCs**; that does not measure normal dashboard discovery or real Slurm load. This run does not qualify large captures, remote filesystems, GPU overhead or terminal input latency.

## External qualification matrix

| Path | Software evidence / limitation | Actual qualification | Unblock condition |
|---|---|---|---|
| CPython call capture and portable viewer | Existing monitoring/legacy/real-interpreter tests; indexed retained and browser paths | Recorded local regression, installed-command and browser evidence | Consult exact PR CI checks; qualify each supported runtime/site workload separately |
| PyTorch CPU operator activities | Actual versioned producer reports, verified journals and common session-clock index | Qualified for the recorded CPU pilot only | Repeat representative versions/workloads and overhead measurements before generalizing |
| Standard reported GPU/CPU activities | Strict common TraceEvent JSON parser; fixture GPU/copy/counter/flow → both views | Parser/model/view behavior only for device fixtures | Actual versioned producer export plus representative device workload; retain raw report and receipt |
| Linux native stack samples | Owned `perf` collector and parser implemented; actual missing-tool recovery runs original once | Live sampling unqualified because `perf` is absent | Compatible `perf` with control-fd acknowledgement, permitted `cpu-clock:u`, frame pointers and owned workload |
| Continuous CPU scheduling capture | Typed reported slices/import and migration-aware projection implemented | No automatic kernel scheduling collector offered | Implement/qualify a bounded owned scheduling adapter before advertising automatic residency/off-CPU capture |
| NVIDIA activities/counters | Shared JSON importer and optional Torch CUDA collector; no dedicated instruction/device-stack decoder | Physical NVIDIA capture unqualified | Physical device, compatible driver/profiler/runtime, permission and versioned pilot; counters need their own definitions |
| AMD activities/counters | Shared JSON importer and optional Torch HIP collector; no dedicated decoder | Physical AMD capture unqualified | Physical device, supported ROCm profiler/runtime/export and permitted pilot |
| Intel GPU activities/counters | Shared standard JSON only; no Level Zero binary decoder/native device-stack collector | Physical Intel capture unqualified | Supported actual producer/schema, device, permissions; implement a dedicated adapter if nonstandard export is required |
| Apple CPU/GPU platform capture | Portable imported viewing; no Instruments binary decoder, no automatic Metal/device-stack collector | Apple host/tool capture unqualified | Supported host, real export/schema and permissions; implement missing native adapter before claiming it |
| Real Slurm/MPI federation | One-node real Slurm r10 passed recorded checks; exact-attempt clocks/topology and selected-MPI links have software coverage | One-node 26.05.5/Python 3.11.2 qualified only for recorded checks; no live multi-node/MPI/production-site qualification | At least two actual nodes, site account/credentials, permitted MPI launch/transport/shared storage and propagated runtime; cgroup/Heavy readiness need independent enforcement/analysis checks |
| Hardware instruction/device-stack tracing | No Intel PT/CoreSight/IBS/device-stack decoder implied by portable viewer | Unimplemented adapters, not merely missing hardware | Implement bounded decoder/capture adapter, then qualify actual tools, access, symbols and representative hardware |

## Design decisions and rejected simpler alternatives

| Choice | Simpler alternative and why it was insufficient | Correctness/resource/UX consequence |
|---|---|---|
| Rebuildable standard-library SQLite range index over existing journals | Increase the old in-memory 20,000-event window | A bigger ring still loses early retained history and scales memory with capture size. Indexed ranges preserve authoritative raw evidence and bounded result payloads. Ingestion/storage cost must be measured; no free speedup is assumed. |
| Shared reader-state admission independent from database quota | Count only raw event bytes or cap each source separately | Aggregate metadata/codebooks/partial blocks/stacks can otherwise multiply with source count. Conservative object-state admission preserves a verified partial prefix and frees limited readers; transient decoding, SQLite and total RSS still require separate measurement. |
| Metadata-only discovery with explicit project following | Read journals twice during discovery, or silently subscribe every manifest argument to its whole project | The index remains the sole workspace verifier. Known sources continue on the journal cadence while bounded metadata scans discover late same-attempt sources; fixed-source CLI inspection stays predictable. |
| Revalidate raw history on a new owner | Deserialize a reader/hash cursor and trust the old database immediately | Revalidation preserves continuity and handles replacement/corruption safely; reopening large sources can cost O(retained history) work. The index is a cache, not a trust root. |
| One writer per local index with RLock + lifetime flock; four default independent owner slots | Multiple viewers write one shared NFS WAL index, or warm caches expose every previously indexed peer | Independent dashboards may own separate copies within four slots; a fifth default owner is refused until a slot is released or an explicit private index is chosen. Requested-source cohorts remain isolated; raw/unrelated files are never pruned. Shared-filesystem locking is unqualified. |
| Separate raw clock axes; optional indexed calibrated reference | Align first events to zero or compare wall timestamps | Avoids fabricated simultaneity; retains source times/uncertainty. Selecting clocks adds an intentional decision when sources cannot be proven aligned. |
| Scheduling-backed intersections plus point-only samples/placement | Charge a whole call to its entry CPU, or extend samples until the next one | Prevents false residency, utilization and exact duration. The viewer can display less dense data honestly; richer capture requires actual evidence. |
| Standard activity import plus optional application-interpreter framework capture | Require a proprietary profiler engine/GPU dependency for every installation | Default runtime stays standard-library-only. Supported reports are reproducible and bounded at import; provider buffers remain tool-dependent and device/runtime qualification stays explicit. |
| Operator recording by default, framework replay stacks explicit | Enable all provider stack instrumentation by default | Real PyTorch replay failed in the pilot. Separating it preserves usable activity timing and reduces unqualified provider behavior; separate Raven Python tracing remains available. This is not a universal overhead claim. |
| Explicit dependencies and observed longest-link chain | Infer links from timestamp proximity or call the graph a global critical path | Missing/cyclic/uncertain evidence remains visible. The analysis is useful for navigation without fabricated global causality, machine efficiency or optimization efficacy. |
| Owner-registered readonly loopback service plus retained offline export | Embed the full archive in every HTML snapshot or expose arbitrary file paths over HTTP | Whole retained navigation uses bounded queries; offline snapshots keep existing privacy/CSP limits. Session tokens, exact origin/host and lifecycle ownership constrain access without adding job controls. |
| Triggered explicit metadata ring | Unbounded always-on recording, hidden sampling or debugger attachment | Bounded memory/storage and deliberate pre-window eviction are visible. No incident saves no event journal; missing boundaries remain missing and scientific work continues. |
| Preserve original multithread timestamps through ordered queue admission | Clamp backward timestamps or weaken integrity checks | The real Slurm workload exposed an observer/GIL interleaving. Serializing timestamp/brief CPU observation/admission and coordinating writer/direct spans preserves evidence; no encoding/fsync runs under the callback queue lock. Actual observer overhead must be measured separately. |
| Build-ID and source-content proof | Open the trace's filename/DSO against whatever current checkout exists | Prevents wrong-build source and trace-supplied commands/paths. Explicit trusted binary/root selection adds user effort only when source resolution is requested. |

## Final skeptical review of the frozen runtime

Runtime changes are committed at
`824a83423f7e1069c61b698764fb3e808e6b1284`; the final evidence commit changes documentation only.
No accepted software requirement remains an unimplemented hidden helper or an
unresolved **Software pending** row. Explicitly bounded alternatives and
unimplemented optional hardware/vendor adapters remain stated in their rows;
external qualification and final release gates are not converted into software
completion claims.

The final source review closed these additional findings:

- **Aggregate statistics bounds:** all-clock function statistics share one
  1-second SQL deadline and one 8 MiB encoded-row budget. Results report actual
  retained byte counts, truncation, queried ranges and unqueried ranges;
  cancellation between domain calls stops subsequent work. Checking each table
  separately would otherwise multiply both cost and memory by source count.
  `test_trace_workspace_function_batch.py` preserves the boundary behavior,
  including large Unicode rows and a first-domain failure.
- **Requeue and supplied scheduler anchors:** existing reporting startup derives
  attempt-specific IDs from validated `SLURM_RESTART_COUNT`, retaining array task
  and cluster identity (`research_reporting.py` identity/start paths). An actual
  filesystem start/restart exercise supports that contract. Trace association
  now rejects conflicting supplied submit/start anchors before accepting a run
  alias. A matching run label alone cannot override contradictory scheduler
  evidence. Missing optional anchors retain the established exact-run or
  explicitly unverified retained-recording path; no new allocation authority
  is inferred. The focused identity/source/reporting review passed 43 checks.
- **Baseline SQL cancellation publication race:** the original race was
  reproduced on `8b317a0`. An owned query child could finish inside a
  communication poll after cancellation, and its reply could be published.
  `evidence_index.py` now checks cancellation immediately after communication
  and after decoding. The smaller fix preserves the established child/IPC/SQL
  budget architecture. Active-VM barrier, nonempty original child result and
  reaped-child assertions remain; cancellation is not tested only before work
  starts. The focused package passed 39 tests in 2.66 seconds.
- **Behavior-preserving UI regressions:** navigation tests now traverse the full
  forward/back cycle including the new Traces destination. Existing selected-ID
  assertions remain intact; source-association fixtures supply complete
  scheduler context rather than weakening the new contradictory-anchor veto.

The first full regression attempt recorded **20,051 passed, 3 failed, 24
skipped in 426.12 seconds**. It remains a failed pre-final run, not a release
pass. After the reviewed corrections, the frozen-runtime suite passed **20,066
tests, with 24 expected skips and four existing fork deprecation warnings in
413.70 seconds**. No behavioral assertions were removed to obtain this pass.
Strict real-browser and compatibility results are recorded above. Packaging,
installed-command and remote CI outcomes remain separate Q06 gates.

## Install the reviewed branch without discarding local edits

When installing the published review branch, keep an existing checkout such as
`/home/rahel/Raven` in place, including its local edits. A separate clean review
checkout avoids a forced reset, stash conflicts or overwriting that work:

```bash
(
  set -e
  raven_review_dir=$(mktemp -d "$HOME/Raven-trace-review.XXXXXX")
  git clone --single-branch --branch codex/trace-workspace \
    https://github.com/rahel99x/Raven.git "$raven_review_dir"
  cd "$raven_review_dir"
  git rev-parse HEAD
  bash install.sh --no-setup
  hash -r
  "$HOME/.local/bin/raven" --version
  "$HOME/.local/bin/raven" trace workspace --help
)
```

This uses the existing repository installer: no sudo, pipx or shell startup-file
changes. It preserves private configuration/state and previous releases. The
original checkout and project recordings are untouched. Close other Raven
dashboards/owned viewing work before maintenance; installation does not cancel
scientific jobs. Python 3.10+, venv support and access to the configured package
index are required, as described in [installation](../INSTALLATION.md).

The command paths above assume the normal `~/.local/bin` installation. If the
existing managed installation uses custom `--prefix` or `--bin-dir` locations,
pass those same installer options and use its printed executable paths. The
installer refuses unrelated command ownership instead of taking it over; an
existing alternative installation should follow its documented migration path.
`--no-setup` keeps existing setup rather than starting the wizard again.

Check the printed revision against the reviewed PR. The installer retains this
review checkout as its source; subsequent updates follow its tracking branch,
not an assumed main branch. Keep the review checkout available. After main is
actually merged, reinstall from the desired main checkout using the existing
[update guidance](../INSTALLATION.md#update-the-installed-package). No branch
publication, successful packaging gate or main merge is implied by these
instructions.

## Benchmark interpretation and final gates

`scripts/benchmark_trace_workspace.py` uses identical deterministic real-journal fixtures for the legacy retained reader and indexed workspace. Fresh child processes separate process RSS high-water from separately tracked Python allocations. Required reports include wall/CPU time, exact query equivalence, fixture/event/lane counts, index/raw bytes and available process I/O counters with units.

The legacy viewer cannot represent a whole source once its retained event window has evicted the fixture's early part. That result is **not whole-recording comparable**; do not report speedup against a smaller prefix or treat an unsupported query as zero cost. The harness alone does not measure scientific-job slowdown, device utilization, live Slurm load or production distributed freshness.

### Controlled measurements on the delivered runtime

All five commands ran **sequentially after tests, builds and owned Slurm
containers stopped**, on Linux x86_64/glibc 2.41, Python 3.12.14, five visible
logical CPUs (cgroup CPU quota four CPUs) and a 32 GiB memory limit. The scripts
record source hashes, raw observations, units and environment limits. Three
timing repetitions use fresh processes; allocation passes are separate from
untraced timing passes. Filesystem caches and the host are not physically
isolated. These are local measurements, not production guarantees.

| Equivalent 5,001-interval fixture | Legacy retained reader | Indexed workspace |
|---|---:|---:|
| Median initial ingestion, wall seconds | 0.07959 | 0.76950 |
| Median initial ingestion, CPU seconds | 0.07957 | 0.76921 |
| p95 exact range query, milliseconds | 0.3781 | 1.2787 |
| Peak process RSS, bytes | 32,460,800 | 20,185,088 |
| Separate Python allocation peak, bytes | 16,355,192 | 2,356,536 |
| Derived database, bytes | 0 | 22,142,976 |

Every query answer matches on the complete fixture. The index reduces this
fixture's process RSS by about 38% and Python allocations by about 86%, while
adding about 0.69 seconds of initial ingestion and 0.90 ms at p95 range lookup.
This is a memory/history tradeoff, **not a query speedup**. The median indexed
whole-range density overview takes 0.219 ms; the legacy reader has no equivalent
whole-recording overview.

At **20,001 intervals**, the index retains the entire source: median ingestion
2.8756 wall / 2.8754 CPU seconds, p95 range query 1.9115 ms, RSS 20,185,088 bytes,
Python allocation peak 2,357,868 bytes, database 86,712,320 bytes and median
overview 0.245 ms. The legacy reader evicts the early recording at this size;
its suffix timings are preserved in the report but are **not a comparable
whole-run baseline**.

The first timed 5,001-interval processes report logical `rchar/wchar` counts of
2,058,252 / 4 bytes for legacy and 8,611,651 / 86,035,552 bytes for the index;
the 20,001-interval index reports 87,511,979 / 367,355,636 bytes. SQLite journaling
and derived summaries therefore add substantial logical I/O. Physical
`read_bytes/write_bytes` counters are zero in this cached/container fixture;
that does not mean no files were read or written, or predict shared-storage
latency. The JSON retains all repetitions rather than presenting these first
samples as I/O medians.

Actual instrumentation keeps **4,007 events, 2,000 calls/returns, the same
numerical result and no drops** in every comparable full-scope run. Legacy/new
median wall times are 0.04964 / 0.02938 seconds; CPU times are 0.04891 / 0.02797
seconds. New project scope takes 0.02913 seconds; the disabled control takes
0.000107 seconds. Prior pilots varied materially on this short workload, so the
observed reduction is not a universal acceleration claim. This does not measure
CPU-placement observer cost. The durable metrics-client fixture costs 0.5196
seconds for its single-thread case and 1.4182 seconds for its threaded case;
those are explicit recording costs, not a faster-than-disabled claim. The
no-op fixture is 0.01102 versus 0.00533 seconds disabled. All requested benchmark
cases completed without failures or skipped fidelity checks.

The actual **Torch 2.6.0+cpu** profiler benchmark keeps identical numerical
checksums and complete recordings. Median scientific work plus checkpoints is
0.19106 seconds disabled versus 0.38416 seconds profiled (about **2.01 times**);
whole process wall time is 1.01576 versus 1.26604 seconds (about **24.6% more**).
RSS is 241,426,432 versus 246,685,696 bytes. Profiler setup/checkpoint/teardown
costs are 0.00865 / 0.20396 / 0.05199 seconds. The tiny scientific-loop-only
variation is noise, not evidence that profiling accelerates the application.
Provider buffer memory and physical GPU overhead remain separately unqualified.

Reproduction commands (choose new output paths to preserve earlier evidence):

```bash
python scripts/benchmark_trace_workspace.py --baseline-root /workspace/Raven --intervals 5000 --repeats 3 --output /workspace/trace-workspace-index-5000-delivered.json
python scripts/benchmark_trace_workspace.py --baseline-root /workspace/Raven --intervals 20000 --repeats 3 --output /workspace/trace-workspace-index-20000-delivered.json
python scripts/benchmark_auto_instrumentation.py --source-root /workspace/Raven-trace-workspace --baseline-root /workspace/Raven --baseline-revision 288a680 --cases calls,noop,metrics,metric-threads --repeats 3 --memory-pass --iterations 2000 --timeout 60 --output /workspace/trace-workspace-instrumentation-release.json
python scripts/benchmark_activity_capture.py --python /workspace/trace-workspace-torch-app-env/bin/python --repeats 3 --allocations --artifacts /workspace/trace-workspace-activity-release-artifacts --output /workspace/trace-workspace-activity-release.json
python scripts/measure_trace_workspace_ux.py --output /workspace/trace-workspace-ux-release.json
```

The original checkout is clean at `288a680dd85dde9766fce3c4d8d962b42bd12203`,
whose tracked tree matches audited main. It provides the old reader and recorder;
these measurements do not compare against an invented old GPU adapter.

| Retained report under `/workspace` | SHA-256 |
|---|---|
| `trace-workspace-index-5000-delivered.json` | `d8f321daa3a6866c284078a74cf767fe8cb5fc34d1edcff5f192719c335f8653` |
| `trace-workspace-index-20000-delivered.json` | `34c4e36a464b5b4af9fb93e79850ac794798db8eb3bbbb20d371928ff74662aa` |
| `trace-workspace-instrumentation-release.json` | `00302d2cfdbdabffd156bd71d58c0a50f1e7b15a0e71c12a3c929ea5b06bf717` |
| `trace-workspace-activity-release.json` | `1dc52ceb11e132c88b52ad21377d7b21bb361e39ac20c71bb28db36c3b02bee9` |
| `trace-workspace-ux-release.json` | `72473a03db27b309814f27043c5e920ad17aa74638bb6f99ace8dd5890defe24` |
| `trace-workspace-installed-release-qualification.json` | `6a7aa26418056f7c6b5cd6bbacbac43b0f47dae67674cf9c68f57684fb894aeb` |

Initial failed runs remain retained: the first index comparison used different
range-boundary conventions in its harness, now corrected with an equivalence
regression; a full-scope capture pilot exposed internal guard events, corrected
without dropping application events; installed-command probes exposed merged
stderr/JSON and a real ASCII heading defect, now corrected with assertions
preserved. They are not included as successful samples.

### Final integrated gates

| Gate | Command / evidence | Recorded result |
|---|---|---|
| Local full regression | `python -m pytest -q -n 2`; `/workspace/trace-workspace-full-final.log`, runtime `3b8558d` | 20,066 passed, 24 expected skips, four existing fork deprecation warnings; 413.70 seconds |
| Final heading/runner follow-up | `pytest -q tests/test_trace_workspace_qualification.py tests/test_flagship_qualification.py tests/test_trace_workspace_integration.py`; `/workspace/trace-workspace-release-installed-regressions.log` | 42 passed, 1.54 seconds on final `824a834` |
| Required actual browser | `RAVEN_REQUIRE_BROWSER=1 RAVEN_BROWSER_EXECUTABLE=/usr/bin/chromium python -m pytest -q tests/test_trace_workspace_browser.py tests/test_trace_workspace_analysis.py tests/test_trace_workspace_follow.py` | 84 passed, 29.99 seconds; no silent browser skips |
| Python 3.11 compatibility | `/workspace/raven-compat/py3.11/bin/python -m pytest -q` with reader/cohort/identity/index/functions/follow/discovery/import/integration/guard/evidence packages | 185 passed, two expected monitoring skips; 19.26 seconds |
| Quality and dependency contracts | `python scripts/check_quality.py`; `/workspace/trace-workspace-release-quality.log` | Passed; 676 modules, zero import cycles, seven contracts kept, Ruff, formatting and typed boundaries passed |
| Generated documentation/schemas | `generate_operation_reference.py --check`, `generate_capability_reference.py --check`, `validate_operation_schemas.py` | All passed |
| Offline onboarding | `python scripts/setup.py --mode demo --venv /workspace/trace-workspace-demo-venv --report /workspace/trace-workspace-demo.txt` | Passed; no live-cluster qualification implied |
| Distribution build | `python -m build`; `/workspace/trace-workspace-gate-build-release.log` | Wheel and SDist built successfully |
| Extracted source distribution | Existing workflow's eight-file regression set, member/fixture checks and source trace qualifier | 143 passed, seven expected skips; 9.64 seconds; new source modules/examples/guide present; historical implementation evidence excluded |
| Installed package outside checkout | `/workspace/trace-workspace-package-gates-release.json`, `/workspace/trace-workspace-installed-release-qualification.json` | All 19 general command smokes, 12 trace helps, three retained CLI queries, activity import, global ASCII rendering, real readonly HTTP/index/terminal probes passed |
| Heavy command semantics/cleanup | Actual `ravenh --site desktop trace workspace ...`, private receipt plus exact helper-lifetime check | Owned standalone worker completes and cleans up; no surviving claim/helper, `cleanup_uncertain=false`; zero scheduler attempts. Default Slurm Heavy admission remains unchanged |
| Flagship/terminal/prototype | `qualify_flagship.py --wheel ...`, installed `raven terminal selftest --json`, `distribution_prototype.py` | All passed; extraction remains experimental |
| Release inventory | `release_inventory.py create`, `verify`, `policy` on the built artifacts | All passed; no artifact-attestation claim |
| Actual one-node Slurm | Final r10 receipt and source hashes above | 16/16 passed on exact `824a834` runtime; owned cleanup verified |
| Controlled performance | Five sequential commands above | All passed, exact fidelity/equivalence checks retained; gains and regressions disclosed |
| Final hosted CI | Review PR checks for the delivered HEAD: Python 3.10–3.14, strict Chromium and actual Slurm workflow | Check the exact HEAD/run links in the PR; local results are not substituted for hosted CI |

The qualification runner preserves stdout and stderr separately while enforcing
the shared selector broker's original aggregate byte/deadline/reaping limits.
JSON is parsed from stdout, and bounded diagnostics are retained in the report;
no prefix-stripping heuristic hides command failures. The default `ravenh`
allocation path is not relabeled as allocation-free. The global plain-text
Traces heading now honors `--ascii`; the real installed-output assertion stays.

Q06's local software/release gates are complete. Hosted CI must cover the final
PR HEAD, including documentation; its live check links provide the authoritative
result without creating a circular documentation-only commit after every run.
Q04/Q05 and the external matrix retain the actual hardware/site blockers.
Main has not been merged. The PR remains a review artifact stacked on the
independently verified Heavy-startup fix until that dependency is reconciled.
