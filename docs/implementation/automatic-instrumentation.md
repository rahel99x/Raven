# Automatic Python tracing and metrics client: coverage ledger

This ledger reconciles the two supplied implementation handoffs with the current
repository and records their acceptance obligations. It is separate from the
frozen `review-2026-10` qualification bundle. **Implementation and local qualification are complete.** The full regression,
compatibility, artifact and real local MPI checks below passed. The tight-call
overhead target remains unmet; PyTorch and target-site qualification remain
explicit external gates, not simulated passes.

## Sources and revision

| Source | Scope |
| --- | --- |
| `Handoff Automatic Python tracing for raven launch --trace.md` | Automatic attachment, process coverage, recorder/reader budgets, UI, examples and qualification. |
| `Handoff the @raven metrics micro-API.md` | Optional application API, bounded writer, rank policy, semantic presentation, vendoring and qualification. |
| Handoff snapshot | `33316a0`, Raven 7.0.0; descriptive line numbers and existing-behavior claims require revalidation. |
| Implementation base | `b52cd93e2b7581eeff6e9f601ad13765f4618a0c`; branch `codex/automatic-tracing-and-metrics-api`. |
| Delivered sources | Exact runtime, test, schema and harness hashes in [integration evidence](automatic-instrumentation-evidence.json); local feature branch, not published. |

The full attached documents were available under `/workspace/attachments` and
read before implementation. Their program-type matrices, test checklists,
documentation changes, risks and open questions are covered below. No absent
review text was replaced with earlier conversation. The source path named in
the metrics handoff was stale: `raven/report.py` renders dashboard exports;
`raven report` dispatches to `research_reporting.main`. Vendoring therefore
belongs to the existing reporting CLI, preserving the report renderer.

## Design decisions and dependency order

1. Reuse the recorder, attempt identity, trace index/locks, streaming readers,
   launch bundle, reporting path checks and existing UI ownership boundaries.
   Recorder scope/batching and bounded readers precede automatic attachment.
2. Develop the independent standard-library metrics client and semantic readers
   in parallel with tracing. Publish metadata alongside unchanged JSONL records.
3. Connect the supervisor environment, readiness, process UI, snippets, client
   vendoring and examples; then run integrated launch/UI regression.
4. Run controlled benchmarks without competing heavy tests, optional real
   runtimes when installed, final regression, schemas, package and installed
   command checks. Record unavailable dependencies explicitly.

The handoffs suggested several sequential pull requests. One isolated feature
branch with separate file ownership avoids publishing automatic tracing before
its recorder/budget dependencies while allowing independent implementation.
No publication or merge is authorized by this implementation request.

| Decision | Simpler alternative and reason |
| --- | --- |
| Environment-based automatic attachment | Rewriting Python commands is narrower and breaks source/argument fidelity. A private site hook preserves scripts but requires explicit startup/environment coverage limits. |
| Block journals with existing event compatibility | Per-event writes are simpler but multiply CPU, hashing and I/O cost across ranks. Bounded blocks require explicit commit/tail verification and retain compatibility with old journals. |
| Standalone client plus lazy package exports | Importing reporting/UI modules would reuse more code but adds application dependencies and runtime startup work. The small validation boundary is duplicated and tested for equality. |
| Explicit reductions | Automatically aggregating every reported value could silently add collectives and deadlock. Only a requested reduction reuses an already initialized application communicator. |
| Private tracing package | Loading the frozen runtime as `_raven_launch_runtime` leaves the public `raven` name available for a vendored client. Changing global application import precedence would break site/user imports. Nested launches remove only verified prior Raven injection paths. |
| Hash-recorded vendoring | Blindly copying `raven.py` is simpler but overwrites user edits. Existing locks, atomic publication, bounded reads and content identity preserve configuration and make upgrades reviewable. |
| Preserve the manual `solve.py` producer | Switching this existing exact-observation fixture to coalescing would discard fast intermediate steps. New `solve_client.py` demonstrates the API; the original regression retains every submitted scientific observation. |

## Tracing contract coverage

`T` identifiers refer to the automatic-tracing source. These rows consolidate
repeated proposals from Goals, Design, Changes by file, UI and Risks without
discarding their subrequirements.

| ID | Required contract | Implementation and acceptance evidence |
| --- | --- | --- |
| T01 | `raven launch --trace` works with an unchanged job script, no source rewriting and no Raven installation in the application environment. | `launch_runtime.py`, `launch_trace.py`; integrated local-launch checks passed. |
| T02 | A private run-local site hook is prepended to existing `PYTHONPATH`; bundle/site contents belong to the reviewed launch artifact and project owner. | Runtime bundle builder and supervisor; path/permission/hash and original-environment checks passed. |
| T03 | Chain an existing site customization before attachment and record whether it was found; remain inert without a valid run/plan. | Site bootstrap and auto-attach; chain-import and inactive-startup regressions passed. |
| T04 | Gate on the exact plan, trace selection and run identity; expose the runtime bundle without changing application source. | Supervisor/attachment identity validation; no-trace/no-hook regression passed. |
| T05 | `python script.py`, `python -m`, `python -c`, shebang and Bash-function invocation attach. | Automatic-launch integration matrix passed. |
| T06 | Real subprocess, fork, spawn, ProcessPoolExecutor and local framework helpers get independent journals; forks never append/reflush the parent's journal or retain unsafe locks. | `test_trace_auto_recording.py`; integrated child lifecycle checks passed. |
| T07 | Record process role, PID, parent PID, interpreter, launcher, rank, local rank and world size with exact attempt binding. | Additive manifest/index identity fields and typed identity tests; rank-launch integration passed. |
| T08 | `torchrun`-shaped ranks and Slurm/MPI-shaped environments are classified; real CPU DDP and mpi4py tests remain separate optional qualification. | Rank fixtures passed; two real local MPI cases passed. PyTorch was skipped because it is absent; shaped environments are not live scheduler evidence. |
| T09 | DataLoader, pool and other helper processes are recorded, counted and filtered from the default view; `--trace-helpers` reveals them. | Process classification, manifest coverage, trace UI; helper/default/override checks passed. |
| T10 | Threads are observed with event thread IDs; physical thread events do not invent logical asyncio or distributed GPU stacks. | Existing monitoring/profile event path plus scope updates; threaded regression and coverage text. |
| T11 | Default scope is project root plus script directory, including first-party `src/`; exclude stdlib, installed dependencies and the runtime bundle. | `PythonCapture` filter and bounded code cache; source-filter tests in `test_trace_auto_recording.py`. |
| T12 | `--trace-scope all` broadens Python scope; monitoring returns `DISABLE` only for permanently excluded code; legacy profile uses the same scope decision without a LINE hook. | Scope/all/disable/cache tests; CLI/UI propagation passed. |
| T13 | Python 3.12+ monitoring is preferred; 3.10/3.11 legacy profile remains explicit consent, preserves profiler ownership and declares weaker semantics. | Existing adapter contracts plus automatic readiness; compatibility checks passed. |
| T14 | `raven_exec` remains supported, preserves arguments/status and prevents duplicate attachment. Explicit-only tracing is selectable. | Hook guard plus `RAVEN_TRACE_AUTO=0`; hook/automatic interaction checks passed. |
| T15 | Unsupported interpreter, isolated startup, replaced path, unsupported children and no attachment leave observable coverage gaps without failing the application. | Attachment diagnostics and observed subprocess audit; failure-isolation checks passed. Scope correction below applies. |
| T16 | Capture observed/pre-script environment evidence without claiming an unobserved module command caused a change. `PYTHONNOUSERSITE`/safe-path behavior is distinguished from `-I`/`-S`. | Startup/environment regressions and precise gap wording verified. |
| T17 | Hash complete bounded blocks rather than individual events, append with bounded writes, preserve sequence and chain integrity; manifest declares block format/size. | `trace_recorder.py`, `trace_protocol.py`; block-hash/corruption/partial-write tests in `test_trace_auto_recording.py`. |
| T18 | Default 64-event/50-ms flush targets and byte budgets prevent unbounded buffering; storage/aggregate exhaustion stops recording with partial coverage. | Recorder budget/background deadline, aggregate reservation and reservation-failure tests. |
| T19 | Stream journals beyond the old 20k-event/8-MiB prefix; retain bounded windows and allow UI progress through the committed journal. | `TraceReader.iter_pages` and refresh window; 200-MB reader and retained-byte/event-bound tests. |
| T20 | Old event journals remain readable; corrupted committed blocks fail and a truncated tail remains uncommitted, never certified as lossless. | Existing `test_trace_protocol.py` plus block/torn-tail regressions. |
| T21 | Multi-process analyses remain per process; cross-rank order requires explicit links and never inferred causal edges or shared clocks. | `trace_analysis.py` and trace UI integration; final cross-process regression passed. |
| T22 | Trace controls list roles, ranks, PIDs, events, coverage and gap reasons; helper toggle works through native UI controls. | `trace_ui.py`, `test_trace_ui_integration.py`; final UI checks passed. |
| T23 | Wizard/readiness accepts automatic tracing without a `raven_exec` requirement and explains runtime consent/coverage; generated descriptors stay current. | `launch_features.py`, `launch_setup_ui.py`, operation-reference regeneration; covered by the integrated checks below. |
| T24 | Native SDK, PMPI and reviewed stack snapshots retain their authority and coverage boundaries; no unrelated scheduler backend, ptrace/eBPF injector or GPU capture is introduced. | Existing native/snapshot suites and updated execution contracts. |
| T25 | Trace package examples cover unchanged script, explicit hook, CPU DDP, MPI and process pools; docs and investment 17 describe automatic attachment with gaps. | `examples/automated-reporting`, stack-tracing guide, execution contracts, README, changelog and current flagship ledger. |
| T26 | Benchmark baseline and revised 10M-call loop, CPU PyTorch and two-rank MPI; show filtered/all-scope cost, CPU/I/O/storage/memory and qualification limits. | Measured receipts in [tracing/client qualification](../benchmarks/automatic-instrumentation.md). The <=2x full-fidelity target failed; PyTorch is unavailable. Both are explicit limits below. |
| T27 | Existing recorder/protocol/runtime/failure-isolation suites continue passing; randomized fork/spawn/flush/exit sequences preserve identity and sequence. | `test_trace_auto_recording.py` property cases plus legacy focused suites; final integrated results passed. |

### Tracing acceptance checklist crosswalk

| Source checkbox | Ledger / check |
| --- | --- |
| Local unchanged script traces; `--no-trace` creates no trace hook | T01–T04; launch integration. Client-only bundle visibility remains independent of tracing. |
| Site customization chaining | T03; chain-import startup test. |
| Module, command, shebang and Bash function | T05; invocation matrix. |
| Fork, spawn, pool, subprocess parent identity | T06–T07. |
| Shaped and real CPU DDP | T08; optional runtime qualification. |
| Slurm-shaped and real local MPI | T08; optional runtime qualification. |
| `-I` / `-S` diagnostics | T15–T16; only observed exclusions can be attributed. |
| Hook plus site hook attaches once | T14. |
| Replaced `PYTHONPATH` diagnostic | T15–T16; no fabricated module name. |
| Default/all scope, DISABLE and legacy cache | T11–T13. |
| Block integrity and incomplete tails | T17–T20. |
| 200-MB bounded reader and per-process UI | T19, T22. |
| Hypothesis process lifecycle interleavings | T27. |
| Three before/after workloads | T26. |
| Existing suites unchanged in behavior | T27; behavioral assertions remain required. |

## Metrics client coverage

`M` identifiers refer to the metrics micro-API source. All rows retain the
unchanged `metrics.jsonl` line format and manual `write_metric`/Bash alternatives.

| ID | Required contract | Implementation and acceptance evidence |
| --- | --- | --- |
| M01 | `import raven` lazily exposes `measure`, `phase`, `watch`, `metric`, `progress`, `configure`; no curses, scheduler or non-stdlib client imports. | `client.py`, lazy `__init__.py`; lazy/static/import-isolation tests in `test_client.py`. |
| M02 | No run/output means no filesystem writes, no worker thread and original wrapped return values. Explicit environment/path configuration enables local development without creating directories. | No-op and local-environment/missing-directory tests. |
| M03 | `measure` accepts scalar or mapping returns, auto/keyword/attribute steps, `every`/`seconds` sampling, preserving return values and application exceptions. | Measurement/steps/mapping/tensor-sampling/async tests. |
| M04 | `phase` records elapsed seconds and innermost phase tags; nested, exceptional and async contexts do not leak phase state. | Nested/async/reentry phase tests. |
| M05 | `watch` reads named locals on completed returns without LINE events; unassigned locals and failed/yielded calls do not reuse stale values; existing profiler ownership is preserved. | Watch stale-local, profile chaining/replacement/nested-removal/completed-return tests. Scope correction below applies. |
| M06 | `metric` reports a finite scalar with kind, step, unit and explicit reduction; `progress` retains existing completed/total/unit semantics and conditional ETA. | All-entry-points and existing `MetricReader` validation checks. |
| M07 | Seven kinds: objective, residual, throughput, progress, conserved, error, gauge; names printable, 1–96 characters, no `=`, at most 64 stream names. | Client name/kind bounds and metadata schema; legacy JSONL compatibility remains unchanged. |
| M08 | Run target uses exact `RAVEN_RUN_DIR`; local env/config target is explicit; source paths are anchored, regular and private, with symlink/hardlink/device refusal. | Client local, symlink/hardlink/device/parent-replacement tests. |
| M09 | Writer is stdlib-only, asynchronous and the sole per-process file writer; complete O_APPEND/flock records and 0600 files; application calls do not wait on filesystem I/O. | Slow-filesystem, file-safety, thread and writer tests. |
| M10 | Flush targets 1 second/64 records; bounded latest-value coalescing, requested sampling, per-name counts, <=20 retained observations/s/name and bounded queue/step/diagnostic retention. | Coalescing, cross-name-pressure, queue/name bounds and thread count tests. |
| M11 | Normal shutdown attempts to drain; forks reset inherited queues and locks; abrupt termination never invents unwritten observations. | Atexit, fork-no-reflush, abrupt-exit and helper-child tests. Durability correction below applies. |
| M12 | Already initialized framework identity takes precedence; otherwise `RANK`, Open MPI, PMI, then Slurm environment identity. Only the coordinator writes by default; supported helpers are suppressed/count-only. | Conflicting nested-launch/framework/noncoordinator/helper/pool checks. This corrects stale outer Slurm rank variables, as explained below. |
| M13 | Explicit all-rank mode creates separate indexed streams; helper processes with no unique rank use PID-qualified names; index and metadata remain bounded. | All-rank/index/subprocess-metadata tests. |
| M14 | MPI/torch reductions support sum/mean/min/max using an initialized communicator; no unsolicited collective, no implicit runtime initialization, no communicator means disclosed coordinator-only value. | Explicit loaded-collective/fallback/failure tests and optional real-runtime qualification. |
| M15 | Reduction is a synchronous application collective: all ranks must call in matching order; sampling schedules must agree; a missing rank can block. | Client guide, DDP/MPI examples and explicit-call tests. No “nonblocking reporting” claim covers collectives. |
| M16 | Finite numeric values, detached scalar tensors/one-element values accepted; strings, bools, nonfinite and multi-element values rejected once without failing the computation. | Unsupported-value and sampled tensor-conversion checks; optional real tensor test. |
| M17 | Writes fail into counting no-op, one bounded diagnostic and existing run-warning location; original application outcomes remain intact. | Run-diagnostic and slow/failing storage tests. |
| M18 | API version 1 and kinds/coalesced/rank policy live in `metrics.meta.json`, never a JSONL header; sidecar parsing is optional, bounded and cannot authorize actions. | `metrics.py`, `metrics-meta.v1.schema.json`, schema fixtures, metadata tests. |
| M19 | Client record validation agrees with established `metrics._record` on arbitrary bounded names, values, steps and phases. | Hypothesis arbitrary-object/field parity tests; no weakening of manual JSONL compatibility. |
| M20 | Client works inside the launch bundle with/without tracing, installed outside launch and vendored outside any installation; `RAVEN_CLIENT=1` records activation. | Root launch environment integration passed; isolated vendored subprocess tests pass in focused suite. |
| M21 | `report vendor-client` copies exact standalone source to default `raven.py` or named project path, records API/hash and refuses changed/untracked copies unless forced. | `research_reporting.vendor_client`; `test_vendor_client.py`. |
| M22 | Vendoring preserves unrelated config, refuses links/nonregular/unbounded files, validates managed admission before mutation and rolls back source after failed metadata publication. | Vendor configuration/symlink/path/rollback/admission tests. Separate files are not a crash-proof distributed transaction. |
| M23 | Vendored copy marks itself, has the same public API and reveals shadowing through readiness/version/hash. | Client marker and isolated copied-source tests; readiness inspection passed. |
| M24 | Connect data leads with decorator, phase and explicit-reduction snippets; Bash remains manual; real preview understands metadata and residual log semantics. | `connect_data.py`, `connect_data_ui.py`, UI/preview tests and API-first guides. |
| M25 | Experiment/Analytics show kind badge and coalescing count; objective trend, residual/error log, throughput median, conserved drift and progress use actual retained observations. | `metric_envelope.py`, charts/analysis UI, `test_metric_semantics.py`; final integration passed. |
| M26 | Raw/trend/range and per-series overrides remain selectable and persistent; nonpositive log points retain raw values; missing/invalid metadata never hides valid data. | Semantic/chart/live-state regression suite; final results passed. |
| M27 | Advisor uses residual growth/conserved drift descriptively with existing abstention rules; no fabricated threshold, scientific proof or unobserved ground truth. | `quick_advisor.py`, semantic/advisor checks; final results passed. |
| M28 | Launch readiness scans declared immediate Python targets boundedly without executing them; detects the API and warns on vendored shadowing. | `launch_features.py`, readiness regression passed. |
| M29 | Plain/module/shebang, PyTorch/DDP, MPI, pool/DataLoader, threads/asyncio and local-development patterns are documented; framework callbacks are explicit, interception remains deferred. | Python metrics program-type table, callbacks and executable examples. |
| M30 | Examples include API scientific calculation, real optional DDP/MPI, standard-library pool, keeping the manual producer for exact-step compatibility. | `solve_client.py`, `train_ddp.py`, `rank_mpi.py`, `pool.py`; manual/client/pool example tests and real MPI passed; PyTorch remains an external gate. |
| M31 | API-first project/Connect data/V2 guides, kind+sidecar standard, README three-line example, changelog and manual alternatives are complete. | Updated current docs; link/schema/example checks passed in the final documentation/schema gate. |

### Metrics acceptance checklist crosswalk

| Source checkbox | Ledger / check |
| --- | --- |
| All API calls parse like manual records; property parity | M03–M07, M19. |
| No-op outside Raven | M02. |
| Local metrics output | M02, M08. |
| Import graph, lazy package | M01. |
| Vendored file has the same API | M20–M23. |
| 100k calls: <=20 points/name/s and coalesced counts | M10. |
| Crash/shutdown/fork semantics | M11; bounded best-effort flush, not hard durability. |
| Rank environment precedence and explicit separate streams | M12–M13. |
| Multiprocessing and pool helpers suppressed | M12–M13. |
| Eight threads × 10k calls, no partial records, accounted inputs | M09–M10. |
| Nonfinite/multi-value/string rejection once | M16–M17. |
| Watch absent local is absent | M05. |
| Nested phase timing/tags | M04. |
| Optional torch scalar and real two-rank gloo reduction | M14, M16; optional runtime gate. |
| Optional real mpi4py two-rank sum and coordinator policy | M12, M14; optional runtime gate. |
| Local API launch and readiness | M20, M28, M30; adjusted example preserves manual fixture. |
| Connect data semantic preview | M24–M26. |
| Sidecar schema and examples | M18, M31. |

## Explicit adjustments and open-question resolutions

| Source clause | Resolution and evidence required |
| --- | --- |
| Every Python interpreter or excluded child must be diagnosed | **Adjusted for observability:** inherited startup attachment covers interpreters it reaches; an attached parent can observe supported child launches. A site hook cannot see arbitrary children of an untraced shell or name a module command it never observed. Coverage says unknown/missing instead of claiming universal enumeration. |
| `python -I` and `python -S` both “skip site” | **Corrected:** `-I` ignores the injected environment path; `-S` skips site initialization. Test the respective outcome, not the inaccurate shared explanation. |
| Crash loses at most one flush interval | **Adjusted for durability:** queued memory and best-effort flush targets are bounded, but slow/unavailable storage and forced exit do not provide a hard wall-clock durability guarantee. Atexit attempts to drain; only committed evidence is certified. |
| `--no-trace` has no `PYTHONPATH` entry | **Adjusted for independent metrics:** no trace hook directory is injected. The runtime bundle can still be exposed to support `import raven` without tracing. |
| A `raven_exec` command makes the whole job explicit-only | **Corrected:** it controls that command/subtree and prevents double attachment; `RAVEN_TRACE_AUTO=0` selects explicit-only behavior before launch. A later hook cannot undo earlier attachment. |
| `watch` shares automatic monitoring scope universally | **Declared scope:** the standalone watch return hook chains existing profilers and is installed per calling thread; use a context/close handle. `measure`/`metric` remain the preferred thread/async reporting path. No LINE hook or universal local-variable introspection is claimed. |
| All framework processes are helpers | **Adjusted to observed evidence:** multiprocessing parent identity and fork state identify supported helpers. Dask/Ray/joblib behavior depends on the actual launcher; their names alone do not prove helper identity. |
| Rewrite the existing `solve.py` API fixture | **Adjusted for data fidelity:** keep its exact submitted observations and existing tests; add `solve_client.py` as the primary API example, explicitly documenting coalescing. |
| Claim <=2x tracing or “no measurable native overhead” | **Qualification target:** publish measured ratios, units, baseline and environmental limits; never turn a failed target or unavailable optional runtime into a pass. |
| Metrics import name and first-release watch | **Resolved:** lazy `import raven`; watch is included with its explicit thread/profile scope. |
| Vendored default location | **Resolved:** project-root `raven.py` for ordinary import resolution; optional `--path` changes placement, recorded with a content hash. |
| Default trace filter and helper behavior | **Resolved:** project root/script directory include first-party source; helpers are traced with separate identity and hidden by default. |
| Rank precedence under nested launchers | **Corrected:** an initialized framework is authoritative for metrics; Open MPI/PMI task variables precede inherited outer Slurm variables. Traces record startup environment evidence; framework ranks established later are not invented. |
| Explicit hook and automatic recorder parity | New frozen launches share block recording, scope and run/process budgets; old frozen plans retain their historical compatible hook. The explicit hook still owns only its command/subtree. |
| Legacy adapter consent | **Resolved:** retain explicit `python-legacy-profile` selection on Python 3.10/3.11. |

## Qualification record

The delivered runtime and tests are identified by SHA-256 in
[integration evidence](automatic-instrumentation-evidence.json). The initial
checkout was clean. Changes are isolated to
`codex/automatic-tracing-and-metrics-api`; no remote publication or merge was
performed. Git revision alone does not identify the earlier uncommitted benchmark
samples; their receipts retain the exact measured input hashes.

| Check | Command or scope | Result |
| --- | --- | --- |
| Full regression | `.venv/bin/python -m pytest -q -n 4 --dist=loadfile` | **18,746 passed, 15 skipped**, 223.86 s; four expected fork deprecation warnings. |
| Cross-version launch/recording | `tests/test_launch_trace_auto.py`, `test_launch_trace_review.py`, `test_launch_trace_settings.py`, `test_trace_auto_recording.py`, excluding the already-tested 200-MB fixture | Python 3.10: **93 passed, 4 skipped**; 3.11: **93 passed, 4 skipped**; 3.13 and 3.14: **97 passed** each. One large fixture deselected in each compatibility run. |
| Client compatibility | `tests/test_client.py tests/test_vendor_client.py` | Python 3.12: **94 passed**; Python 3.10 and 3.14: 93 passed before the final fork guard, then both affected fork tests passed on each. |
| Actual MPI | Optional qualification interpreter with mpi4py 4.1.2 and Open MPI 5.0.7 | **2 passed, 1 PyTorch skip**; verifies actual ranks, journals, collective values, invalid-rank input, failed writer and recovery ordering. |
| Schemas/examples | `scripts/validate_operation_schemas.py` | **80 schemas, 43 examples**, no errors. |
| Architecture/reference | `scripts/check_quality.py`; both reference generators with `--check`; `git diff --check` | Passed. |
| Distribution build | `.venv/bin/python -m build` | Source archive and wheel built. |
| Extracted source distribution | Recorded Slurm/parser/attempt identity, automatic launch and vendoring tests from extracted archive | **92 passed**, 15.37 s. |
| Installed wheel | `scripts/qualify_flagship.py --wheel dist/raven-7.0.0-py3-none-any.whl` | All **8 applicable local checks qualified**; 7 external/site checks explicitly untested. |
| Installed feature path | Two real local launches from outside the checkout with an application venv that has no Raven installation | Bundled client and vendored client both produced the expected metric and a verified process trace; `raven`, `ravenl`, `ravenh` version commands passed. |

The full suite includes the 200-MB incremental trace-reader fixture, bounded
allocation checks, corrupted blocks and interrupted writes, real fork/spawn
interleavings, profiler coexistence, thread contention, unsafe-path refusal,
stale attempts, UI persistence and isolated SSH-helper execution. The latter
uses a local subprocess and is not live SSH qualification. Existing behavioral
assertions were retained. The metrics-reader stat-count assertion was updated
for the optional bounded sidecar/index reads; source incrementality remains
asserted.

### Review findings resolved

- Private runtime imports prevent an installed or vendored public `raven` module
  from disabling attachment; ordinary application import precedence is preserved.
- New explicit hooks share the automatic scope/budgets/block journal and retain
  their existing report attachment contract; old frozen plans remain compatible.
- Shared reservation locks have a finite timeout. Partial startup, contention,
  missing metadata and shutdown failures preserve application outcomes.
- Complete frozen-plan ownership is verified before attachment writes. An
  unverified environment path receives stderr diagnostics, not writes into a
  different run. Nested launches remove only verified prior injection paths.
- Gap deduplication preserves known PID/rank/parent identity; unobserved processes
  do not acquire an invented PID or module command.
- Forked processes reset inherited recorder/cache/client state and never query
  an inherited MPI/PyTorch runtime before helper suppression.
- Explicit reductions agree on input validity before reducing values. Writer
  failure never removes a participant; wall-clock sampling with reductions is
  refused because it could desynchronize ranks.
- An initially empty canonical metrics file no longer hides indexed rank data.
  Rank selection remains within the same verified run and reads one stream.

### Measurements and qualification limits

[Automatic instrumentation measurements](../benchmarks/automatic-instrumentation.md)
include the preserved baseline and exact measured-source fingerprints. Fully
retained 2,000-call tracing is approximately **42% faster** and uses **30% fewer
journal bytes** than the baseline. It still takes approximately **292 times**
the unrecorded tight loop; **the requested <=2x target is not met**. Meeting that
target requires a separately evaluated capture architecture or an explicitly
reduced-fidelity product mode, not silently dropping required events. The
10-million-call resource-limit test retains under 1% of calls and cannot qualify
full-fidelity overhead. NumPy/MPI results do not establish zero native overhead.

The inactive decorator adds approximately **69 ns/call** in the recorded fixture.
A 100,000-call burst retains one value and reports 99,999 coalesced observations.
[Metric-reader measurements](../benchmarks/metric-semantics.md) report a **0.076 ms**
legacy median regression and **2.622 ms** added semantic preparation for 64 series
by 600 points. Rendering performs zero I/O. Process RSS and Python allocations
are distinguished; no local result establishes multi-node/NFS latency or
scientific efficacy. The benchmark receipt identifies the three-line fork-only
safety guard applied after client timing; it does not relabel earlier samples.

| Unqualified gate | Evidence / unblock condition |
| --- | --- |
| Real CPU PyTorch/DDP, DataLoader, GPU/NCCL | PyTorch is absent. Install a compatible CPU PyTorch and rerun `tests/test_instrumentation_qualification.py` for gloo; separately exercise DataLoader and CUDA/NCCL on the intended hardware. Rank-shaped fixtures are unit evidence only. |
| Slurm, multi-node, shared filesystem and module-system behavior | No live scheduler/allocation or selected target cluster was available. Run the documented unedited-script, MPI/DDP and environment-preservation examples inside an authorized site allocation and retain receipts. |
| Python interpreters below 3.10 | The startup source has a fail-open unsupported-version path and a reporting-interpreter gap receipt; no older interpreter was available for execution qualification. |
| Arbitrary startup bypass and unsupported launchers | `-I`/`-E`/`-S`, embedded interpreters and replaced environments may bypass attachment. Observed parent requests and aggregate unknown coverage are reported; complete shell-child enumeration needs a separate observer. |
| Deployed UI/user/scientific outcomes | Local terminal/PTY and regression evidence passed; actual emulator, external-user task completion and scientific outcomes require separate evaluation. |
| Hosted CI on the delivery branch | CI now collects bounded instrumentation fidelity/performance receipts and retains the existing Python 3.10–3.14, packaging and schema matrix. It has not run remotely because this work has not been published. |

### Installation and use

After this branch is published or merged, update the checkout and rerun
`bash install.sh --source-dir .` using the existing installer workflow. No metrics
migration is needed: JSONL records and legacy event journals remain readable.
Use `raven launch job.sh --trace` for inherited Python startup attachment, or
add `--local` on a standalone workstation. Python 3.10/3.11 requires
`--trace-adapter python-legacy-profile`. Use `raven report vendor-client --root
PROJECT` when the application needs a standalone import outside Raven launches.
Do not use a forced client overwrite to discard unreviewed local changes.
