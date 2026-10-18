# Raven: implementation handoff for both external reviews

Prepared 2026-10-17. Status: **planning complete; implementation not started**. The source reviews are preserved in `04-source-reviews.md`. This handoff covers all **30 numbered proposals, the raster-plot suggestion, and the opening assessment**, divided into **44 work packages** so immediate fixes do not wait for architectural redesigns.

The recommended first release is a reliability and polish release: atomic state, attempt-safe recordings, trustworthy parsing, bounded accounting queries, secure configuration/plugin loading, efficient SSH reuse, diagnostic traces, and credible release/validation records. Larger capabilities follow explicit evidence and dependency gates.

Repository inspected: `/workspace/Raven`, commit `33316a08bd583a242356c7722a21c10e00cff21b`. Its working tree was clean. Remote freshness was not checked. Selected code, tests, CI, guides, and benchmark records were read; no full audit, runtime reproduction, or test run is claimed. Revalidate these findings against the implementation branch before editing. All paths below are repository-relative unless stated otherwise.

## 1. Scope and source accounting

Use A-01…A-20 for Review A, B-01…B-10 for Review B, and A-H1 for its honourable mention. Reviewer metrics, estimates, novelty claims, and claims of missing features remain claims until verified. The complete text is available in the companion source file; filenames/previews are no longer a blocker.

Disposition **accept** means implement the stated outcome; **adjust** means preserve the outcome with the design correction specified here; **gated** means deliver a bounded implementation/prototype and evidence, with production qualification conditional on its stated gate. Gated work is still in the backlog, not silently dropped. A failed research hypothesis is a reportable result, not permission to market an ineffective model.

| Source | Proposal and disposition | Packages | Verification/design position |
| --- | --- | --- | --- |
| A-01 | Real Slurm CI — accept | W14 | Current workflow has no Slurm service; one container cannot clear unrelated GPU, SSH, or multi-node gates |
| A-02 | JSON reads, corpus, parser diagnostics, bounded accounting — adjust | W02, W03, W15 | Legacy parsing and epoch-wide lookup confirmed; probe individual JSON capabilities and data-parser shapes |
| A-03 | Properties, faults, faster tests — accept | W11 | Extend existing fragmentation tests; three-minute suite target is a hypothesis |
| A-04 | Wakeable loop and synchronized output — adjust | W18, W19 | UI timeout confirmed; preserve real timers and use terminal capability fallback |
| A-05 | Attempt identity, compact series, rollups, revisions — adjust | W06, W24, W25 | Job-only path confirmed; land identity fix independently; compact storage needs measured benefit |
| A-06 | Shared framing, validators, fewer serializations — adjust | W12, W13 | Three codecs confirmed; limits/semantics differ intentionally in places; schemas already run in CI |
| A-07 | Package boundaries, quality gates, command extraction — adjust | W22 | Incremental boundaries and packaging compatibility, not a whole-tree rewrite |
| A-08 | Atomic state, scoped ownership, file trust, NFS policy — adjust | W01, W04, W05 | Tag defect and plugin checks supported; other subsystems already have locks/security checks |
| A-09 | Per-user daemon and thin clients — gated | W38 | Design around retained sessions and collection budgets; client compatibility and site policy matter |
| A-10 | Shared host collector — gated | W39 | Requires administrator deployment and isolation qualification; build local harness first |
| A-11 | Automatic worker recovery and wait cleanup — adjust | W16, W17, W18 | Reconcile verified ownership; never unblock by ignoring an uncertain allocation |
| A-12 | WASI adapter sandbox — gated | W40 | Optional compatible adapters; runtime/host-call limits, not universal adapter conversion or guaranteed safety |
| A-13 | SQLite queries, time-series operators, SQLite export — adjust | W23 | Add a rebuildable index and optional bundle; preserve raw evidence, expression semantics, and CSV/JSON |
| A-14 | Self-trace and tracer overhead reduction — adjust | W08, W20 | UI spans and per-event hashing confirmed; overhead factor and proposed optimization require measurement |
| A-15 | Backfill-aware counterfactual forecasts — gated | W34 | Partial observability and causal uncertainty require abstention and calibration |
| A-16 | Rank-divergence logs — accept | W27 | New analysis must account for legitimate role differences and missing logs |
| A-17 | Streaming anomaly detection — accept | W26 | Start with one interpretable detector and explicit coverage/false-positive evaluation |
| A-18 | Signed evidence — adjust | W21 | Verifier trust must come from outside the bundle; signing-tool availability is not universal |
| A-19 | Read-only MCP — accept | W41 | Bounded project-scoped queries, no arbitrary filesystem/shell access; mutations remain outside this server |
| A-20 | Core/lab split, signed releases, user study — adjust/gated | W09, W10, W42, W44 | Release evidence and study precede distribution split; optional extras alone do not split installed source |
| A-H1 | Kitty/Sixel plots — gated | W43 | Optional rendering experiment after baseline responsiveness; Braille/ASCII remain complete |
| B-01 | Slurm digital twin — gated | W33 | Limited observable simulator, not a faithful hidden-state reconstruction or CI replacement |
| B-02 | Fairshare-aware submission shaper — gated | W35 | Advisory and shadow modes first; actual submission requires owned campaign scope and reviewed budgets |
| B-03 | Time-travel views and diff — accept | W28 | Distinguish what Raven observed then from later knowledge; missing history stays unknown |
| B-04 | Unprepared-process profiler — adjust | W29 | Owned local processes only; `/proc` permissions, rank identity, locality, and attribution limits apply |
| B-05 | Phase segmentation/alignment — gated | W32 | Bounded algorithms, known-phase evaluation, gaps and uncertainty made explicit |
| B-06 | Allocation health fingerprint — adjust/gated | W31 | Opt-in workload with a budget; no automatic startup tax or cancellation by default |
| B-07 | Checkpoint advisor — adjust | W30 | Separate failure causes, measure exposure/checkpoint cost, state model assumptions |
| B-08 | Ownership/protocol model checking — accept | W16 | Stateful tests against real classes first; bounded formal model where it adds coverage |
| B-09 | Trace sketches — gated | W36 | Define answerable query families and justified error contracts before replacing evidence |
| B-10 | Nondeterminism campaigns — gated | W37 | Randomization, repetitions, interactions, scientific equivalence, and uncertainty precede attribution |

The opening assessment is also covered: validation breadth and deferred drivers → W10/W14/W44; timed polling → W08/W18; SSH reuse → W07; state defects → W01/W04/W05/W06; framing/schema/package drift → W12/W13/W22; release provenance → W09/W10. Numerical claims about LOC, schema count, test count, annotations, and deferred drivers are not treated as current measurements. W10 verifies any such number retained in documentation.

## 2. Verified anchors and corrections to the reviews

| Observation | Evidence inspected | Interpretation |
| --- | --- | --- |
| Tag writes truncate the destination and suppress `OSError` | `raven/model.py:Store._save_tags` | Strong source evidence for atomic publication and visible failure handling |
| Persistent series path contains only sanitized job ID | `raven/model.py:Store._series_path`; `raven/series_archive.py:SeriesArchive._identity/_merge` | In-memory attempt guard exists; it does not add attempt identity to the disk path. Reproduce cross-attempt restoration before fixing |
| Key/value parser splits on whitespace | `raven/slurm.py:parse_kv`; allocation helper/reuse callers | Spaces and embedded assignments need corpus-backed handling; do not infer a particular lease failure without reproduction |
| Short job rows are skipped; an accounting log lookup starts at the epoch | `raven/slurm.py:parse_jobs`, `SACCT_LOG_FIELDS` lookup | Report degraded parsing and use bounded identity-aware accounting windows |
| Placement collection constructs SSH with `control=False` | `raven/raven_launch.py:configure_placement`; `raven/remote.py:SshBackend` | Verify all call paths; the broader backend defaults to multiplexing, so “every poll” is not established |
| UI wait is clamped to 1–200 ms; terminal visibility shortens it | `raven/screen.py:wait_ms`, `run_curses` | Wakeable waiting is justified; polling alone is not yet proven to explain all idle CPU |
| Existing idle report records mean CPU about 5.68% of one core | `docs/benchmarks/7.0.0-light-idle.json` | Historical evidence only; establish a current comparable baseline |
| Fragmentation tests already exist | `tests/test_raven_allocation.py:test_generation_protocol_fragmentation`; `tests/test_terminal_service.py:test_ipc_framing_handles_every_header_and_payload_boundary` | The blanket “currently untested” statement is wrong; add missing properties/state sequences instead of duplicating tests |
| PTY broker and worker components already use selectors | `raven/terminal_broker.py`, `raven/raven_worker_client.py`, `raven/raven_allocation_helper.py` | Integrate readiness across owners; do not rebuild an already event-driven broker |
| Protocol limits differ | `raven/raven_protocol.py`, `raven/raven_allocation.py`, `raven/terminal_service.py` | Share framing machinery with per-protocol policy; uniform large limits would weaken existing bounds |
| Schemas are audited in CI | `scripts/validate_operation_schemas.py`, `.github/workflows/tests.yml` | They are not merely unused documentation. Runtime single-source validation is a separate design decision |
| Config/state hardening exists in some paths | `raven/setup_config.py`, `raven/raven_config.py`, `raven/runtime_control.py`; several modules use `flock` | Close specific gaps; a blanket permission or locking rewrite would duplicate working code |
| Trusted Python plugins execute discovered files | `raven/plugins.py:discover/load` | Add a clear trust policy and pre-execution checks; trusted plugins remain code execution |
| Tracer hashes each event and registers `sys.monitoring` callbacks | `raven/trace_recorder.py:Recorder.emit`, `PythonCapture.start`; `raven/trace_protocol.py` | Optimize only with integrity/coverage compatibility tests; no measured 10–100× claim yet |
| Refresh documentation disagrees | `docs/guides/ui-performance.md` still references the old top-right slider; `docs/guides/everyday-workflow.md` documents Refresh settings | LOCAL-01, an independently found polish item; confirm runtime and reconcile in W10 |

Additional design corrections are mandatory:

- **Recovery:** responsiveness may recover while resource ownership remains uncertain. A job number alone never authorizes cancellation; verify site, user, allocation/request identity, generation, and submit/ownership evidence. Unknown scheduler results must not trigger a duplicate allocation or scientific submission.
- **Scientific inference:** `TIMEOUT` is not node failure; preemption is a separate mechanism. A checkpoint file's size or mtime does not measure checkpoint duration. The square-root checkpoint rule is an approximation under assumptions, not a universally optimal schedule.
- **Sampling:** D-state observations do not prove `/scratch` caused a stall. Sampling does not reconstruct exact wall-time states between observations. A collector on one node cannot read another node's `/proc`.
- **Trust:** an `allowed_signers` file supplied by the signer cannot establish the reviewer's trust. A digest proves content integrity, not scientific truth. SO_PEERCRED proves local peer credentials, not project entitlement.
- **Research claims:** one user's traces omit other users' demand and site policies. Simulation and self-calibration are not equivalent to a real scheduler. “Nobody does this,” fixed speedups, three-minute CI, and guaranteed cost savings are not accepted claims without evidence.
- **Compatibility:** accept unknown descriptive fields only where specified; reject unsupported protocol versions or unknown action/security semantics. Do not globally relax exact validation.

## 3. Effort ranking: shortest to longest

Ranges are provisional **engineer-days**, including implementation, focused tests, integration, documentation, and review, excluding waiting for users/sites/CI. They are not predictions of agent wall time. Overlapping ranges are intentionally approximate; do not sum them into a delivery promise. Confidence is medium for narrow verified paths and low for migrations, research, and infrastructure. Rank indicates effort; dependencies determine executable order.

| Rank | Package | Outcome | Effort | Main prerequisites / gate |
| --- | --- | --- | --- | --- |
| 1 | W01 | Atomic tags and visible save failures | 0.5–2 | Reproduce persistence failure |
| 2 | W10 | Honest capability ledger and current guidance | 0.5–2 | Source inventory; retain external gates |
| 3 | W02 | Correct legacy parsing and parse diagnostics | 1–3 | Recorded cases; ambiguous fields stay unknown |
| 4 | W03 | Bounded accounting queries | 1–3 | W02; exact historical identity |
| 5 | W07 | Scoped SSH connection reuse | 1–3 | W04 trust policy; connection-path audit |
| 6 | W09 | Versioned releases and build attestations | 1–3 | W10; actual release requires publishing authorization |
| 7 | W04 | Configuration and plugin trust checks | 2–4 | Audit all loading paths and trust classes |
| 8 | W08 | Bounded Perfetto self-trace export | 2–4 | Existing timing-span baseline |
| 9 | W19 | Capability-gated synchronized painting | 2–4 | Terminal probes; independent of W18 |
| 10 | W05 | Scoped state writer ownership and NFS policy | 3–6 | W01/W04; multi-client behavior defined |
| 11 | W06 | Attempt-safe archive paths and migration | 3–6 | Identity contract; coordinate with W05 |
| 12 | W12 | Shared bounded framing and serialization | 3–6 | W11 characterization subset |
| 13 | W11 | Properties, faults, and bounded test parallelism | 3–7 | Baseline timings; isolated fault targets |
| 14 | W21 | Verifiable signed evidence bundles | 3–7 | Signature/trust format and tool capability |
| 15 | W44 | Five-journey usability study and fixes | 3–7 | W10; participant/site access for live phase |
| 16 | W13 | Versioned validation and schema consistency | 4–8 | W12 contracts; dependency decision |
| 17 | W17 | Bounded ownership-preserving recovery | 4–8 | W03/W05/W16 |
| 18 | W15 | Capability-gated Slurm JSON adapters | 4–10 | W02/W14 corpus and real qualification |
| 19 | W25 | Explicit retention tiers and fidelity metadata | 5–10 | W06/W24; no silent loss of raw evidence |
| 20 | W30 | Statistically qualified checkpoint advice | 5–10 | W03; measured exposure and checkpoint costs |
| 21 | W26 | Online anomaly detection with abstention | 5–12 | W06; labelled synthetic fixtures |
| 22 | W20 | Lower-overhead tracing and interoperable exports | 5–12 | W08/W11; versioned trace integrity |
| 23 | W43 | Optional raster plotting experiment | 5–12 | W19 terminal capability/fallback approach |
| 24 | W14 | Real Slurm CI and qualified version matrix | 5–15 | Reproducible images; accounting configuration |
| 25 | W16 | Executable ownership/protocol models | 5–15 | W11; precedes automatic recovery changes |
| 26 | W18 | Wakeable UI and deadline-based worker waits | 7–15 | W08/W11/W16 |
| 27 | W23 | Bounded SQLite evidence queries and export | 10–20 | W05/W06/W13 |
| 28 | W24 | Versioned compact series and indexed summaries | 10–20 | W06/W11; benchmark payoff gate |
| 29 | W27 | Cross-rank log divergence analysis | 10–20 | W06/W11; verified rank/attempt mapping |
| 30 | W28 | Historical replay and instant comparison | 10–20 | W06; defined observation/snapshot coverage |
| 31 | W29 | Owned-process state profiler | 10–20 | W16/W17; Linux access and node locality |
| 32 | W41 | Read-only bounded MCP tools | 10–20 | W13/W23; supported MCP protocol/transport |
| 33 | W22 | Incremental package and command boundaries | 10–25 | W12/W13; keep import/package compatibility |
| 34 | W31 | Opt-in allocation health receipts | 10–25 | W17; owned compute and hardware-dependent tests |
| 35 | W32 | Phase segmentation and cross-run alignment | 15–30 | W06/W26; algorithm/evaluation budgets |
| 36 | W37 | Nondeterminism experiment campaigns | 15–30 | W16/W17; valid repeatable workloads |
| 37 | W42 | Independently installable core/lab distributions | 15–30 | W10/W22; dependency audit and usability evidence |
| 38 | W34 | Backfill-aware calibrated counterfactuals | 20–40 | W15/W33; held-out outcomes and abstention |
| 39 | W35 | Fairshare-aware campaign shaping | 20–40 | W16/W17/W33; shadow-mode evaluation |
| 40 | W36 | Query-preserving trace summaries | 20–40 | W06/W20; explicit approximation contracts |
| 41 | W38 | Optional per-user daemon and clients | 20–40 | W05/W12/W13/W17/W18 |
| 42 | W39 | Admin-managed multi-user collection | 20–40 | W15/W38; isolation and deployment gate |
| 43 | W40 | Optional WASI execution boundary | 20–40 | W12/W13; compatible adapters and threat model |
| 44 | W33 | Calibrated, limited Slurm digital twin | 30–60 | W14/W15; observable configuration and held-out traces |

No source proposal is represented by only its easiest fragment: A-05 spans W06/W24/W25, A-14 spans W08/W20, and A-20 spans W09/W10/W42/W44. The expensive portions remain visible.

Planning assessments apply to every package as follows. W01/W02/W04/W05/W06 address potentially high-consequence integrity or trust failures; severity remains provisional until reproduction. W03/W07/W08/W09/W10/W11/W12/W13/W14/W15/W16/W17/W18/W19/W20/W21/W22 improve existing correctness, operability, or assurance; the remaining packages are capability enhancements or research rather than established defects. Immediate value is highest for the first two groups; enhancement value must be established by its user/evaluation gate. Change risk is high for W05/W06/W12/W13/W16/W17/W18/W20/W22/W23/W24/W25/W29/W33/W34/W35/W36/W37/W38/W39/W40/W42, low for W08/W10, and medium for the remainder. Effort confidence is medium for W01/W02/W03/W07/W08/W10/W19 and low for the remainder. Update these assessments with baseline evidence rather than treating them as objective scores.

## 4. Dependency-aware delivery sequence

**Gate 0 — establish evidence.** Record current revision/status, verify repository instructions, reproduce W01/W02/W06, map every source subrequirement, and capture targeted UI/resource/test baselines. Start W08 and the relevant W11 characterization tests early. Do not run the entire suite once per proposed feature.

**Release tranche 1 — correct and polish.** Complete W01/W02/W03/W04/W05/W06/W07/W08/W09/W10, ordered by actual dependencies. W06 moves ahead of cosmetic work because it protects scientific evidence. Prepare the demo portion of W44 now. Start the container setup for W14 independently if it can unblock real parser evidence.

**Release tranche 2 — prove contracts and responsiveness.** Complete W11/W12/W13/W14/W15/W16/W17/W18/W19/W20/W21. W16 precedes W17 even though it may cost more. W08 supplies profiling evidence before W18. Implement W22 incrementally after contract changes settle; do not let directory moves obscure functional diffs. Repeat W44 after these changes.

**Release tranche 3 — extend existing evidence workflows.** Complete W23/W24/W25/W26/W27/W28/W29/W30/W31/W41, in dependency order. W28 need not wait for SQLite if bounded replay over retained records is sufficient. W43 is a late optional display slice and must not delay correctness or accessibility work. Finish the live W44 study when access is available.

**Research tranche — test hypotheses explicitly.** W32/W33/W34/W35/W36/W37 have a vertical prototype, benchmark baseline, held-out evaluation, uncertainty report, and product readiness decision. W33 precedes W34/W35, not every analytics feature. Failed efficacy gates leave honest experimental functionality or a documented negative result; they do not justify invented success or indefinite tuning.

**Platform tranche — expand deployment only when justified.** W38/W39/W40/W42 reuse tested contracts. Deliver local integration harnesses and deployable artifacts; production multi-user operation, new publishing, and live compute use remain subject to active authorization and site access. Do not add PBS/Kubernetes or unrelated scheduler replacement work: those were not proposals in these reviews.

At every tranche, smaller independent work can proceed while a larger dependency is blocked. A release gate limits what can be claimed or enabled; it does not erase the remaining backlog. Keep a checkpoint ledger so a resumed implementation continues at the next unresolved package.

## 5. Work-package specifications

Each card inherits the shared validation, review, measurement, and completion rules in sections 6–8. Its rank/effort/dependencies are in section 3. Before coding, add the precise affected symbols, reproduction, proposed diff boundary, and test commands discovered in the current checkout. Paths here are entry points, not an instruction to put all new logic in those files. Each package should produce a focused reviewable change; split larger packages into the listed milestones.

### W01 — Atomic tag persistence

Source A-08. Replace truncating writes in `raven/model.py` with bounded snapshot publication using established atomic-file utilities from `ui_state.py`/`setup_config.py` where suitable. Serialize writes without holding the Store lock during slow I/O; prevent an older snapshot overwriting a newer one. Expose save failure while retaining usable in-memory data.

Acceptance: interruption leaves either the previous or new complete file; permission/disk-full errors are visible; retry persists the latest tags, pins, and notes. Test concurrent updates, replace failure, restart, and old JSON compatibility. Keep the file format unchanged. Do not introduce a database for this repair.

### W02 — Legacy parser correctness and diagnostics

Source A-02/opening assessment. Characterize `slurm.py:parse_kv`, `parse_jobs`, and their allocation/log callers with captured and adversarial records: spaces, embedded `=`, delimiters, arrays, heterogeneous jobs, missing/extra fields, nulls, and invalid numbers. Parse known boundaries conservatively; some legacy text is intrinsically ambiguous and must remain unknown.

Acceptance: recognized values survive round trips; malformed rows increment source-specific diagnostics and do not masquerade as an empty successful queue. Valid rows can publish with explicit partial coverage. Invalid ownership/lease fields prevent unsafe actions. Preserve the last trustworthy snapshot when a new response is unusable. Keep bounded input sizes and linear-time parsing; do not invent quoting rules absent from real output.

### W03 — Bounded accounting lookups

Source A-02. Replace the epoch-wide `sacct` log lookup with an identity-aware window based on retained submission time, a documented fallback horizon, and explicit historical lookup when needed. Inspect actual command semantics; bounds must not exclude known legitimate old attempts.

Acceptance: routine refresh never performs an unbounded history scan; reused job numbers cannot select another attempt's output; missing accounting remains visible as unknown. Test absent submit time, time zones, old runs, arrays/steps, delayed accounting, and an explicit user-requested wider lookup. Record command count/time window in source diagnostics. Rollback must not discard historical records.

### W04 — Configuration and plugin trust policy

Source A-08. Audit `config.py`, `raven_config.py`, `setup_config.py`, `plugins.py`, and existing `runtime_control.py` helpers. Define separate policies for private user config/secrets, user-executable plugins, and approved root-owned site config. Check ownership, writable ancestors, regular-file type, and symlink/race behavior before executing trusted code. Reuse safe loading paths; do not blindly require 0600 for every harmless shared file.

Acceptance: untrusted writable code cannot execute before the diagnostic appears; legitimate approved site config and user plugins continue working; users receive an actionable path/reason. Test loader variants and unsafe replacements. Do not silently chmod/chown existing files, disable all plugins, or claim trusted Python is sandboxed.

### W05 — State ownership and network-filesystem policy

Source A-08. Inventory all writers and existing locks. Define writer ownership per state scope/host/profile, not one machine-wide Raven singleton. Choose an open lock-file descriptor with supported locking semantics for local state; specify second-client read-only/refusal/isolated-state behavior and how headless exports coexist. Lock acquisition errors differ from active ownership.

Acceptance: two processes cannot lose mutable state; crashes release or safely reconcile ownership; another host's PID is never assumed local. Test two processes, stale metadata, read-only state, independent profiles, and unsupported/network filesystem behavior. Local `flock` is not proof of distributed safety. Keep retained project evidence readable where permitted, document local mutable-state placement, and show any unavailable durability guarantee.

### W06 — Attempt-safe series identity

Source A-05. Reproduce job-ID reuse/requeue contamination across `model.py`, `series_archive.py`, and persistence queues. Define a durable identity including site/cluster, project or source scope, job/array/step identity as applicable, and a verified attempt token. Do not rely on a process-local counter or mutable start time alone. Capture identity at sample acquisition/enqueue, not when delayed writes drain.

Acceptance: two attempts sharing a job number never read or append each other's samples; late callbacks/writes cannot cross generations; reopen and exports select the same attempt. Exercise same-state requeues, restarts, sanitized-name collisions, partial files, and unknown identity. Preserve old JSONL as legacy/ambiguous when attribution cannot be proved; migrate only verifiable data and retain a recovery path. This fix ships before binary storage.

### W07 — SSH connection reuse

Source opening assessment. Trace where `configure_placement` disables multiplexing and how collection/file readers share transports. Reuse scoped ControlMaster connections when allowed, with bounded startup/cleanup and a control-path identity incorporating relevant connection options. Respect existing SSH configuration and intentional opt-outs.

Acceptance: repeated qualified queries avoid new handshakes in a loopback-SSH test; different targets/users/proxy/security settings do not accidentally share a connection; a lost socket reconnects within policy without replaying mutations. Preserve host-key checks and keep control sockets private. Measure connection count, wall latency, and CPU; a fake argv assertion alone cannot prove network reuse.

### W08 — Perfetto self-trace

Source A-14. Extend `ui_trace.py` and existing `_timed_ui` spans with an opt-in bounded Chrome trace-event export, including correlation for background sampler/worker work where available. Use consistent monotonic timing, explicit process/thread IDs, dropped-event counters, and redacted categories. Export on an explicit action or background writer, never during paint.

Acceptance: a known injected stall appears in a valid trace with correct nesting/correlation; tracing off has negligible measured overhead; tracing on has a measured cap. A ring-buffer overwrite is labelled incomplete. Verify in an independent trace reader and document that timeline traces are not automatically CPU call-stack profiles. This is the measurement foundation for W18/W20.

### W09 — Release provenance and signing

Source A-20. Extend `.github/workflows/tests.yml` and `scripts/release_inventory.py` with a coherent version/tag policy, source-to-wheel inventory, and Sigstore build/signing provenance where supported. Build and verify release artifacts from the tested revision; distinguish tag signing, artifact signatures, checksums, and attestations.

Acceptance: a verifier checks artifact digest and expected repository/workflow identity; modified artifacts fail; installed version agrees with release metadata. Dry-run and workflow tests precede publishing. Do not retroactively imply old binaries were signed or verified. Record missing network/OIDC capability explicitly; actual tag/release publication follows active-chat authorization.

### W10 — Evidence ledger and documentation polish

Source A-20/opening assessment plus LOCAL-01. Audit `docs/raven/capability-ledger.json`, `docs/research/flagship-strategy.md`, current guides, and reported counts. Distinguish implemented analysis, executable integration, simulated validation, and real-site qualification. Reconcile stale refresh guidance against the current interface. Inventory deferred drivers without claiming this review authorizes implementing all 18.

Acceptance: every changed capability row links to revision-specific evidence; external gaps remain explicit; commands and menu labels match current behavior. Replace unsupported novelty/performance claims with bounded statements. Documentation-only edits need proportionate checks, not new mirror tests. Keep this small: it is a trustworthy baseline and polish pass, not a rewrite of every guide.

### W11 — Properties, fault injection, and test efficiency

Source A-03. Add Hypothesis as a development dependency where it improves coverage. Extend existing protocol tests with arbitrary fragmentation/coalescing, invalid UTF-8/JSON, depth/length boundaries, version mismatch, expression budgets, canonical/digest round trips, and attempt-aware archive merge properties. Fuzz legacy parsers from the recorded corpus with defined invariants and metamorphic changes, keeping ambiguous inputs explicitly unknown. Use isolated child processes for worker death, SIGSTOP, socket loss, and truncated-write scenarios; always clean them up.

Acceptance: invariants, not implementation copies, determine success; minimized counterexamples become fixed regression fixtures. Profile suite duration and worker RSS, then try bounded xdist with isolated ports/temp/state. Mark truly serial tests and compare clean serial/parallel outcomes. Record any time reduction with the same test selection and host; under three minutes is a target to test, not a guarantee or reason to drop checks.

### W12 — Shared framing, explicit protocol policies

Source A-06. Extract the repeated length-prefix/buffer/partial-I/O machinery from `raven_protocol.py`, `raven_allocation.py`, and `terminal_service.py`. Keep policy objects for frame/queue/depth/string/record limits, message validation, and protocol versions. Preserve each channel's intended wire semantics and public imports during migration.

Acceptance: existing and property tests cover each policy, mixed old/new peers where supported, partial writes, empty/oversize frames, nonfinite values, and bounded queues. Keep evidence canonicalization distinct from transport encoding; never change receipt hashes incidentally. Reuse immutable canonical bytes where valid to reduce repeated serialization, and measure allocations/throughput. A single codec must not silently expand all channels to the largest limit.

### W13 — Versioned schema and validation strategy

Source A-06. Inventory runtime hand validators against published schemas. Evaluate generated lightweight validators, shared typed contracts with schema generation, and an optional/full validator dependency. Preserve the dependency-free base unless measured requirements justify a change; do not write an incomplete general JSON Schema engine.

Acceptance: representative valid/invalid corpora agree between runtime and schema validation; new minor descriptive fields follow an explicit extension policy; unsupported major versions and unknown action semantics are rejected. Migrations are explicit, bounded, and round-trip tested. Schema refs are local, input depth/size is bounded, and startup/hot-path overhead is measured. Expand coverage incrementally rather than loading every schema on every message.

### W14 — Real Slurm qualification in CI

Source A-01. Start with one pinned reproducible Slurm controller/compute/accounting setup and a vertical lifecycle: submit → pending → running → accounting completion → logs → cancel/requeue → reconnect. Then expand a nightly matrix. Preserve the requested 23.02/23.11/24.05/24.11 as compatibility candidates; check their availability/support and add a maintained version appropriate at implementation time rather than pretending the historical list is current.

Acceptance: use real commands and versioned outputs, including accounting delay, arrays, resource/account fields, spaces, and job reuse cases. Save redacted stdout/stderr/config/version fixtures. A PR smoke job stays bounded; larger matrices run nightly with actionable artifacts. Restrict privileged service/secret use for untrusted PRs. Only ledger gates exercised by this environment can close; physical GPU, production SSH, site policy, and true multi-node performance remain separate.

### W15 — Capability-gated structured Slurm readers

Source A-02. Add per-command capability discovery and normalizers in/around `slurm.py`. Probe supported JSON mode and data-parser/schema variants with cached results and explicit invalidation; version strings alone are insufficient. Maintain a redacted recorded corpus from W14 and field-by-field legacy/JSON equivalence tests.

Acceptance: JSON and fallback produce the same stable domain facts on shared fixtures; unavailable fields stay unavailable; transient command failures or malformed JSON are not silently recast as successful legacy reads. Fallback occurs for an established unsupported capability and is visible. Preserve bounds and query budgets. slurmrestd is not required to deliver this proposal; add it only if a separately verified need emerges within scope.

### W16 — Ownership and protocol state models

Source B-08/A-11. Specify identity, ownership, pending/granted/cancelled, lost-message, and committed/acknowledged transitions for allocation and worker classes; separately model pointer capture leases. Drive real classes with Hypothesis stateful actions. Add a bounded TLA+/PlusCal model where exhaustive interleavings improve assurance; document abstraction and fairness assumptions.

Acceptance: no cancellation of another owner's resources, no duplicate accepted external effect after a lost acknowledgment, stale generations cannot revive ownership, and recovery progresses when required evidence becomes available. Unknown external effects remain unknown where the backend lacks idempotency. Include grant/cancel races, reused job IDs, delayed messages, crashes/restarts, and committed-result retry. Check counterexamples against implementation fixtures. Bounded model checking is not a proof for every deployment.

### W17 — Ownership-preserving automatic recovery

Source A-11. Extend `raven_allocation.py`, helper/reuse code, and collection policy with a persistent bounded reconciliation procedure. Distinguish transport loss, terminal allocation state, unverifiable ownership, and retryable scheduler outage. Reuse W16 transitions and W03's bounded identity queries.

Acceptance: verified owned orphan allocations are reconciled/cancelled according to existing reviewed intent; already-completed resources unblock correctly; a recycled job number is never cancelled; repeated errors back off with jitter/budget and expose the next action. No automatic scientific resubmission. Keep Light usable while uncertainty remains. Test disconnect-before-grant, cancel/grant crossing, missing accounting, revoked permissions, and restart. Release the allocation block only on adequate evidence, not elapsed time alone.

### W18 — Wakeable UI and deadline-driven waits

Source A-04/A-11. Integrate stdin readiness, a nonblocking coalesced self-pipe/socketpair, sampler publication, PTY/client readiness, and allocation events with the UI owner in `screen.py`. Derive selector timeout from actual animation, escape-decoder, maintenance, and sampling deadlines. Existing brokers retain their ownership. Remove busy sleep loops incrementally in worker/helper paths.

Acceptance: no lost wakeup, starvation, stuck buffered curses input, blocked exit, or dropped click/release during bursts; signals, resize, EOF, escape-sequence deadlines, subprocess exit, and fallback terminals work. Producers never block on a full wakeup pipe; shutdown closes descriptors safely. Preserve finite fairness budgets and required periodic work. Measure idle CPU, wakeups, input p95, and publication latency with/without live PTY. Sub-1% of one core is a stated target on a documented idle fixture, not a universal promise.

### W19 — Synchronized terminal output

Source A-04. Add capability detection/configuration for DEC private mode 2026 around the complete painted frame where supported. Use bounded sync regions and `finally` cleanup; preserve behavior through tmux/SSH and unsupported terminals. Keep byte emission owned by the renderer.

Acceptance: frame boundary sequences are balanced on success, resize, exception, and exit; unsupported terminals use the current painter; terminal input remains intact. Test PTY byte sequences and representative terminal/manual behavior. Measure added bytes/latency and report subjective tearing observations separately from timing. Do not claim the escape sequence is supported everywhere or removes network latency.

### W20 — Tracer efficiency and exports

Source A-14. Benchmark representative call-heavy, async/generator, multithreaded, and trace-disabled workloads before changing `trace_recorder.py`/`trace_protocol.py`. Cache immutable code metadata; use `sys.monitoring.DISABLE` only for events/code objects where its actual semantics permit correct filtering and re-enabling. Evaluate block-integrity records as a versioned format, preserving committed cutoffs and reader compatibility.

Acceptance: event order/coverage, fork handling, exceptions, dropped-event counts, and crash-prefix verification remain correct. Export valid Perfetto and, for supported semantics, speedscope views without pretending a flat timeline is a sampled stack profile. Test Python 3.10/3.11 legacy behavior separately from 3.12+ monitoring. Measure application slowdown, bytes/event, CPU, RSS, and durability; adopt block hashing only with a demonstrated benefit and migration path.

### W21 — Signed evidence bundles

Source A-18. Define a signature namespace and canonical signed manifest binding receipt, artifact digests, schema, and identity. Integrate signatures and descriptive signer metadata into the existing export/RO-Crate workflow. Use detected OpenSSH signing support or report unavailable; preserve unsigned export. Supply a standalone verification recipe and optional tooling without requiring Raven.

Acceptance: modification/substitution fails verification; wrong signer/namespace fails; the reviewer selects trusted keys through an independent allowlist/fingerprint. A bundled signer file is descriptive, not automatically trusted. Never copy private keys into exports or invoke signing without user intent. Test paths with spaces, unavailable tooling, detached signatures, and key/trust changes. Distinguish authenticity from scientific validity and signing time from independently trusted timestamps.

### W22 — Incremental architecture and quality gates

Source A-07. Inventory dependencies and extract stable boundaries toward `ui`, `model`, `scheduler`, `heavy`, `evidence`, `ops`, and `terminal` only where the graph supports them. Move one seam at a time; retain import shims/versioned plugin APIs as needed. Consolidate command dispatch through the existing `command_registry.py` before growing another registry.

Acceptance: public commands/imports, configuration, optional terminal support, portable helpers, and installed-wheel contents remain correct. Add import-linter/ruff/formatting gates for changed boundaries, with a documented baseline for legacy debt; ramp mypy strictness on typed modules and use `py.typed` only with an honest typing policy. `pyproject.toml` currently lists only package `raven`: update package discovery/data inclusion before introducing subpackages. Separate mechanical moves from behavior changes and avoid one enormous formatting diff.

### W23 — SQLite evidence query layer

Source A-13. Build a versioned, rebuildable per-project index over immutable retained sources using stdlib SQLite. Define ingestion checkpoints, transactional cutoffs, row provenance, and attempt keys. Add a bounded read-only query entry point, named contextual query views in the existing evidence/log interface, and optional SQLite export alongside existing formats. Keep `expr` semantics and budgets stable; translate only a verified equivalent subset.

Acceptance: crash/replay cannot duplicate or misattribute rows; index rebuild recovers from authoritative evidence; queries have row/byte/time/VM-step budgets, cancellation, and clear partial-result status. Enforce read-only connections and an authorizer that blocks mutations, ATTACH, dangerous PRAGMAs/functions, and extension loading. Define WAL/network-filesystem handling and consistent backup/export via supported SQLite APIs. Time-series rates specify counter reset, irregular sampling, gaps, and zero intervals. Benchmark ingestion, query plans, disk growth, and memory against JSON/CSV workflows.

### W24 — Compact series and indexed rendering

Source A-05. After W06, introduce an opt-in versioned block format with checksum, explicit endianness/float/time semantics, append commit markers, and bounded decoders. Compare a simple array/zlib design with more complex timestamp/float coding before selecting it. Add min/max summaries preserving discontinuities and provenance, and reliable mutation revisions before removing any cache signature fallback.

Acceptance: export/reopen round trips preserve raw values within the declared representation; corrupted/truncated blocks yield verified prefixes and coverage errors; corrected interior samples invalidate views. Existing JSONL remains readable and migrates through copy/verify, not destructive in-place conversion. Measure build/append/query costs separately: indexed queries may approach O(log N + width), while building summaries still processes input. Compressed blocks require decompression; mmap alone does not make them zero-copy. Enable by default only after a documented storage/CPU/RSS payoff.

### W25 — Retention tiers and explicit fidelity

Source A-05. Add configurable raw/10-second/1-minute retention with counts, min/max, sums or other declared statistics, time bounds, and aggregation semantics. Keep raw retention for protected scientific/export use and record which questions a tier cannot answer. Compaction is interruptible and uses a staged publication protocol.

Acceptance: restart during compaction loses neither verified data nor tier identity; disk-full/quota events are visible; rollups never pose as raw samples. Queries spanning tiers neither duplicate intervals nor hide gaps. Preview retention impact and preserve source references needed by receipts. Test numeric extremes, irregular sampling, late corrections, pinned evidence, and unknown units. Benchmark retained bytes and query cost without comparing lossy output to raw output as if equivalent.

### W26 — Streaming anomaly detection

Source A-17. Implement one bounded baseline detector first, such as robust rolling deviation or EWMA, then add CUSUM where a specified use case justifies it. Consume attempt-scoped timestamped measurements and attach evidence windows to deduplicated alerts. Separate detecting a change from diagnosing its cause.

Acceptance: known injected steps/leaks/stalls are detected with recorded delay and false-positive rate; warmup, missing GPUs, uneven sampling, phase changes, constant signals, and noisy baselines can yield abstention. Memory/time stay bounded per series and total active streams. A forecast of reaching memory limits has units, uncertainty, and validity horizon, not a precise unsupported timestamp. Do not create another permanent toolbar control.

### W27 — Rank-divergence logs

Source A-16. Extend `log_match_index.py`/`log_workbench.py` with bounded template extraction and ordered per-rank comparison, retaining original line references. Require evidence for rank/attempt association; allow role cohorts instead of assuming every rank should match a global majority.

Acceptance: known divergent and legitimate-role fixtures produce an interpretable first divergence and last common anchor; reordered timestamps, missing/rotated/truncated logs, repeated templates, and ties remain explicit. Do not use timestamp equality as proof of causal ordering. Bound bytes, templates, ranks, and comparison window; report truncation/approximation. Compare against a simple normalized diff baseline and preserve a path to raw evidence.

### W28 — Historical replay and comparison

Source B-03. Reuse retained transitions/history snapshots/series to build an immutable as-of view for Jobs/History/Analytics with explicit observation time and source time. Add timeline navigation and instant diff within the existing run context; commands such as `[`/`]` require keyboard-conflict checks. Record snapshots only as needed to make replay bounded.

Acceptance: later observations do not leak into “known at the time” replay; unknown intervals are shown, not interpolated as fact. A separate reconstructed-state mode may use later evidence only when labelled. All mutations are disabled in historical mode; returning live restores identity/selection. Test requeues, timezone changes, partial retention, clocks, and deterministic replay fixtures. Measure seek latency and index/snapshot disk overhead.

### W29 — Unprepared-process profiler

Source B-04. Add an opt-in Heavy collector for verified owned local process trees/cgroups, using PID plus start time to prevent PID-reuse mistakes. Read available `/proc` counters for observed states, CPU/runtime and run-delay, context switches, I/O, and migrations where exposed; mark unavailable metrics explicitly. Rank mapping requires evidence and may be unknown; multi-node capture requires a separately verified collector on each covered node.

Acceptance: process exits, thread churn, hidepid/permissions, missing counters, PID reuse, and cancellation cannot escape ownership or break workloads. Report observed state proportions and counter deltas with their limits; do not infer exact scheduling history or a specific filesystem from D-state/wchan alone. Start with adaptive bounded sampling; 100 ms is a requested mode subject to overhead/task-count budgets. Compare collector-on/off workload slowdown and publish coverage per node/rank.

### W30 — Checkpoint advisor

Source B-07. Implement an explanatory advisor over measured checkpoint duration, restart cost, useful-work duration, and a failure model with exposure and censoring. Treat node failure, preemption, and wall-time expiry separately. Support explicit user-provided estimates when local observations are insufficient.

Acceptance: known analytical cases and simulations validate the chosen Young/Daly variant and units; sparse/biased history, zero observed failures, uncertain durations, and incompatible checkpoint support produce ranges or abstention. File size/mtime alone cannot establish write cost. Show sensitivity and assumptions in launch/recovery review; recommendation does not alter jobs automatically. Evaluate expected lost work and overhead against fixed-interval/no-checkpoint baselines, including the advisor's own uncertainty.

### W31 — Allocation health fingerprint

Source B-06. Add an optional reviewed preflight suite bound to an owned attempt/allocation, with selected tests, time/byte/resource caps, cleanup, and cancellation. Begin with local memory/scratch tests; MPI/GPU tests are conditional on compatible tooling and an appropriate allocation. Keep workload execution and the diagnostic's own interference distinguishable.

Acceptance: disabled means no added startup work; temporary storage is bounded and cleaned; a failed or slow probe cannot silently cancel a scientific job. Receipts include hardware/topology, library versions, placement, warmup, raw results, and uncertainty. Compare within meaningful hardware cohorts, with repeatability checks. Ten seconds is a configurable budget, not a promise that every MPI/GPU benchmark fits. Flag unusual performance without claiming a causal “sick node” diagnosis.

### W32 — Phase segmentation and alignment

Source B-05. Define metric normalization, feature selection, missing-data handling, and bounded change-point detection before DTW alignment. Start with a simple segmented synthetic producer with known phase boundaries, then the reporting example and held-out attempts. Use constraints/windows or downsampling to bound DTW; PELT does not guarantee linear work for every input.

Acceptance: measure boundary accuracy, alignment error, stability under noise/time scaling, and false correspondences. Distinct scientific phases are not forced into a match; gaps and uncertain alignment remain visible. Preserve original timestamps/values and make aligned views reversible. Compare with fixed-window/simple alignment baselines and record runtime/memory as samples/features increase. Product release requires useful held-out performance, not just attractive plots.

### W33 — Observable Slurm digital twin

Source B-01. Milestones: explicit simplified discrete-event model; deterministic priority/resource/dependency scheduling; backfill/fairshare/reservation/QOS subsets; recorded-trace calibration; held-out evaluation. Discover configuration through permitted commands and describe missing fields/hidden demand; user-provided or synthetic configurations remain supported.

Acceptance: seeded reproducibility, resource conservation, dependency/limit invariants, and hand-computable schedules first. Compare supported semantics with real W14 Slurm fixtures, then temporal holdouts. Report wait/start error, coverage, ranking accuracy, and sensitivity to hidden state. Keep model/version/parameter receipts and compute budgets. Calibration and testing must use different traces; do not validate forecasts solely against the same simulator that generated training data. The twin supplements real Slurm CI and cannot prove fidelity to an unobserved site.

### W34 — Backfill-aware forecasts

Source A-15. Extend `forecast.py` with explicit request alternatives and observational/context features supported by W15/W33. Begin as advisory scenario analysis. Model runtime-overrun risk when suggesting shorter wall time; do not recommend a smaller request that makes the workload likely to be killed merely to improve queue metrics.

Acceptance: chronological held-out evaluation compares simple cohort forecasts, current predictor, and new model with interval calibration, Brier/log score for probabilities, tail errors, and abstention coverage. Handle drift, out-of-distribution requests, reservations not visible to the user, and missing policies. A counterfactual estimate is not a causal guarantee; present assumptions and tradeoffs. Keep simulation and real-outcome evidence separate, with bounded inference cost.

### W35 — Fairshare-aware submission shaping

Source B-02. Implement a policy interface over already-declared campaigns, initially producing reviewed plans and shadow decisions. Compare FIFO, submit-all, shortest-job/backfill-friendly heuristics, and fairshare-aware timing under the same workloads and constraints. Reuse existing campaign identity, submission receipts, and W16/W17 recovery.

Acceptance: dependencies, concurrency, MaxSubmitJobs/GrpTRES and other known limits are obeyed; restart/lost acknowledgment cannot duplicate submissions; users can pause/cancel/override a campaign plan. Evaluate makespan, throughput, wait tails, starvation, wasted compute, scheduler command rate, and robustness to forecast error. Actual execution is opt-in and scoped to authorized owned work. No evasion of site policy, claim to replace Slurm, or claim of global optimality. A policy that helps one metric but worsens another must disclose that result.

### W36 — Trace summaries with query contracts

Source B-09. Enumerate supported query families: per-phase/rank counts, sums, extrema, quantiles, and top-k duration summaries, including merge/delete/time-window semantics. Distinguish exact aggregates from approximations. Select algorithms with justified guarantees; t-digest's empirical behavior is not a universal deterministic error bound.

Acceptance: an answerability check returns exact/approximate/unanswerable before answering; merging preserves the stated error contract; adversarial and heavy-tailed distributions are tested against full journals. Record sketch version, parameters, coverage, time bounds, units, and source digests. Keep raw evidence optional but governed by explicit retention, and never allow a sketch to certify event order or causality it did not preserve. Measure ingest cost, bounded memory, export size, and actual query errors.

### W37 — Nondeterminism attribution campaigns

Source B-10. Extend `ops_investigation.py`, existing regression/diagnostic runners, and `science_diagnostics.py` with a declared experiment design: compatible factor levels, randomized/block-randomized order, repeated controls, resource budget, and output-equivalence checks. Start with a tiny deterministic/controlled reduction fixture, then an MPI example when tooling exists.

Acceptance: cancellation/restart retain exact trial identities and do not repeat committed work; unsupported GPU/BLAS/MPI controls are reported, not presumed effective. Distinguish bitwise changes, tolerance failures, and semantic changes. Estimate main effects/interactions/residual variation with uncertainty and rank/conditioning checks; do not assign causal percentages from a confounded or underpowered design. Compare against known injected factors and label unexplained variance. Live experiments consume only explicitly authorized allocation budgets.

### W38 — Optional per-user session service

Source A-09. Milestones: stable session/query/event contract; one owner of Store/Sampler; local Unix-socket daemon; reconnecting TUI and JSON/watch client; remote transport bootstrap/version negotiation; minimal notebook/editor/portal integration examples. Reuse session identifiers and sequence/cursor-based replay. Do not make every install require a daemon.

Acceptance: disconnect/reconnect preserves authorized session and observation context; restart reconciles external ownership; slow clients have bounded queues and explicit gaps; clients cannot multiply collection load or submit duplicate actions. Authenticate local peers, restrict sockets, preserve site limits on login-node work, and keep Heavy computation in verified allocations. Replace repeated source-shipping only after profiling and a compatible installed/bootstrap path exists. Test daemon/client version skew, remote disconnects, stale sockets, crash recovery, and uninstall/rollback.

### W39 — Shared host collector

Source A-10. Build an optional service over read-only scheduler observations, reusing W38 contracts and `shared_collection.py` lessons. Authenticate peers using supported local credentials, then separately authorize user/project/site visibility. Per-user response shaping must happen before data leaves the service; cache keys include entitlement scope and policy generation.

Acceptance: multi-user tests prove no cross-user job/log/config leakage, bounded fan-out, quotas, revocation, and slow-client isolation. Do not centralize privileged arbitrary file reads or scheduler mutations. Measure query reduction and service CPU/RSS with 1/10/200 clients; preserve scheduler policy and freshness indicators. Ship service configuration, audit/health diagnostics, upgrade/rollback instructions, and an unprivileged test harness. Production installation and real multi-user qualification require administrator participation; mock peers alone cannot establish site security.

### W40 — Optional WASI adapter boundary

Source A-12. Define a threat model and supported adapter ABI, then choose a maintained runtime/version as an optional dependency. Start with a compatible pure-analysis adapter; arbitrary Python/native/MPI/CUDA plugins are not automatically portable to WASI. Explicitly configure preopened directories, host calls, environment, network access, memory, output, fuel, and wall time.

Acceptance: hostile fixtures cannot read outside grants, escape via symlinks/path traversal, exceed output/memory/CPU budgets, or stall cancellation indefinitely. Fuel alone does not bound blocking host I/O, and clocks/randomness/network affect determinism. Keep trusted Python plugins clearly distinct. Compare output fidelity and overhead with the trusted adapter; document runtime vulnerabilities/update process and the limits of the boundary. Avoid absolute claims of safety or determinism.

### W41 — Read-only MCP evidence tools

Source A-19. Reuse bounded evidence/query APIs and expose a small versioned MCP surface: list attempts, read receipts, bounded logs, query evidence, inspect timeline. Prefer a standards-compliant SDK as an optional extra if needed; document protocol/transport compatibility and validate it with an independent client. Stdio is a simple first transport; remote service adds separate authentication requirements.

Acceptance: tool schemas, pagination, byte/row/time budgets, cancellation, provenance, and errors are consistent; project scope and path canonicalization prevent access outside authorized evidence. No arbitrary shell, scheduler actions, write SQL, or filesystem path tool. Treat logs/plugin text as untrusted data, never instructions to the tool server. Test malformed requests, repeated/cancelled requests, large logs, and attempts to escape scope. Integrate through existing commands, without new permanent dashboard clutter.

### W42 — Core/lab distribution split

Source A-20. Use W22's import graph and W44's workflow evidence to define a small auditable core and optional science/ops extensions. Decide whether separate distributions, plugins, or optional dependencies best achieve the actual audit/install-size objective; an extra alone does not remove shipped modules. Preserve existing `raven`, `ravenl`, `ravenh` entry points through a compatible umbrella package if needed.

Acceptance: clean core installation handles monitor/launch/logs/run home without importing lab modules; lab installation restores compatible capabilities; upgrade/downgrade and missing-extension states preserve user data/config. Test wheel and sdist contents, helper runtime shipping, plugin API compatibility, and optional dependency absence. Publish measured installed footprint/import/startup changes and a migration guide. No breaking distribution rename until these gates pass; the audit and extraction milestones remain useful independently.

### W43 — Optional terminal raster plots

Source A-H1. Prototype one bounded renderer behind a capability/configuration switch, keeping existing Braille/ASCII/text views complete. Investigate Kitty and Sixel support separately, including tmux/SSH behavior, image lifecycle, resize, partial repaint, and mouse-coordinate mapping. Do not assume both protocols have identical semantics.

Acceptance: unsupported terminals fall back cleanly; overlays/selection remain correct; images are removed on view change/exit; escape output cannot corrupt input handling. Bound resolution, image memory, encoded bytes, and draw frequency; measure transport cost as well as rendering CPU. Keep accessibility and exports independent of terminal images. Enable by default only with clear UX evidence; no novelty claim is needed.

### W44 — Five-journey user study

Source A-20. Locate the existing G5 journey definitions rather than inventing different success criteria. Recruit five participants when authorized; run demo-first journeys, then real-account sessions when site access permits. A reproducible scripted walkthrough can prepare fixtures but cannot replace human evidence. Use clear consent for any recorded observations and avoid collecting credentials or private research content.

Acceptance: record task completion, time, unnecessary decisions, documentation lookups, error recovery, and participant understanding of account/resources. Separate observer assistance from unaided success. Rank discovered friction and ship focused fixes before unrelated feature expansion; rerun affected journeys. Five users reveal usability issues but do not establish statistical population claims. Demo and CARC/live-account evidence remain separate; user access is a real external gate.

## 6. Engineering workflow and self-review

Use the same loop for every package: **verify → baseline → design/alternative → implement → focused validation → diff review → measure → resolve → record**.

Before implementation, write the problem and expected outcome in one paragraph; identify existing mechanisms and compare at least one simpler alternative. Estimate CPU/memory/I/O complexity and identify state/compatibility risks only where relevant. Avoid speculative infrastructure, gratuitous caches, or new defaults that run work in the background.

After implementation, review the actual diff independently of the initial design. Check lifecycle ownership, stale generations, partial failure, cancellation, backpressure, atomicity/durability, data fidelity, privacy of evidence, migration compatibility, UI discoverability, keyboard interaction, and resource limits where touched. Link every substantive finding to its resolution. Another agent may review if the main environment permits; a separate self-review pass is required otherwise.

Parallelize independent modules/tests only with explicit ownership and contracts. Serialize shared-file integration and controlled performance comparisons. Do not run competing full suites or benchmarks on a resource-constrained host. Continue routine authorized work without repeated permission questions; record actual access/product blockers and proceed with independent packages.

Keep a status ledger with: package/subrequirement ID, source reference, verification status, implementation state, dependencies, tested revision, evidence paths, reviewer findings, performance result, and next action. Work states are pending/in progress/validated/blocked/rejected-with-reason; a source claim being unverified is a separate field. Preserve this checkpoint across compaction or handoff.

## 7. Measurement and validation strategy

### Reuse existing harnesses

`scripts/benchmark_ui.py`, `benchmark_changing_ui.py`, `benchmark_sustained_pointer.py`, `benchmark_raven_resources.py`, and `benchmark_data_export.py` already provide useful foundations. Inspect their current arguments and process scopes before extending them. Use `ui_trace.py` and W08 to locate work; instrumentation timings are separate from normal execution results.

| Dimension | Measures | Decision rule |
| --- | --- | --- |
| Correctness | Identity/data fidelity; duplicate/lost effects; coverage/partial status; numerical error | No silent regressions or fabricated evidence; exactness changes need a declared contract |
| UI latency | Usable startup, input-to-render p50/p95, longest stalls, background publication delay | Compare equivalent fixtures; p99 only with adequate sample count |
| CPU | Idle and active process-tree CPU, wakeups, calls/work per action | W18 targets under 1% of one core on a specified idle fixture; verify freshness and child cost too |
| Memory | RSS/peak RSS, Python allocations separately, cache/queue bounds, growth after warmup | Persistent services/collectors must plateau within declared bounds; no RSS claim from tracemalloc alone |
| Shared infrastructure | Queries/minute, retries, handshake count, network bytes, filesystem ops/bytes, disk growth | Stay within site budgets; improvements cannot come from hidden stale data |
| Telemetry | Requested/effective recording and read cadence; sample age, gaps, drops, timestamp integrity | Faster UI cannot be credited for measurements not actually collected |
| Reliability | Recovery time, lost-ack outcomes, cancellation, crash consistency, version skew | Observe actual end state and ownership; deterministic structural invariants first |
| UX | Steps/decisions to first job, unaided completion, keyboard/80-column behavior, non-color cues | Preserve simple default workflow and contextual controls; use W44 evidence |
| Research | Holdout error/calibration, false positives, approximation error, simulation validity | Compare simple baselines; define abstention and stop criteria before tuning |
| Delivery | Install/update/rollback success, package/import footprint, CI duration/RSS/flakiness | Every supported installation mode remains verifiable; no weakened tests to hit timing targets |

Select relevant metrics per package; a prose change does not need a benchmark battery. Record revisions, environment, fixtures, units, warmup, duration, repetitions, sample count, and raw results. Use same-host equivalent workloads and an interleaved baseline/candidate order when host drift matters. Establish tolerance from baseline variation plus a meaningful absolute delta before evaluating candidate results. Report improvements, regressions, and inconclusive measurements; optimize a measured bottleneck rather than every code path.

For size-sensitive work, scale samples/jobs/ranks/clients independently through normal and stress cases. For persistent workers, add a justified soak and descriptor/process/memory-growth check. Energy and cost require suitable instrumentation/rates; CPU time alone is not a measured energy bill. Query/retention speedups must preserve the requested fidelity or label the tradeoff.

### Regression and integration gates

Use behavioral tests for substantive defects/contracts; do not add mirror tests for trivial prose changes. Extend existing tests instead of assuming their absence. During UI redesign preserve assertions for selection, input ordering, focus, pointer ownership, exact run identity, and accessibility even if the old control layout changes.

Run focused suites during development and integration checks at dependency boundaries. At each reviewable tranche, run the repository-required integrated checks against the delivered revision; after later changes rerun affected checks and align required CI with the new revision. Do not sum historical passes and targeted reruns into a claim that the final complete suite ran cleanly.

At the inspected revision, `.github/workflows/tests.yml` runs Python **3.10–3.14**, regression tests, generated operation reference checks, schema/example validation, offline setup, source/wheel builds, installed commands outside the checkout, terminal self-test, flagship qualification, and release inventory/checksums. Use its current form as the implementation-time authority for exact commands. Package discovery/helper installation checks become especially important for W22/W42.

End-to-end validation follows the changed path: setup/configure → launch or query → observe → error/recovery → reopen → retain/export. Use an isolated state/config directory. Cover clean install and upgrade with existing user data for persistence/packaging changes. For every report label the environment: unit fake, demo, local process, loopback SSH, real Slurm container, target cluster, physical GPU, or multi-node. A fixture cannot satisfy an unrelated external gate.

## 8. Completion, rollout, and final handback

For each package, completion requires mapped source subrequirements, observable acceptance evidence, resolved review findings, compatible configuration/state behavior, relevant measured results, updated user/developer docs, and current checks. Record remaining qualification limits without calling them complete. A button or API skeleton without its working backend path does not qualify.

Use additive formats and read-old/write-new transitions for storage/protocol changes; stage migrations with verification and recoverable originals. Defaults remain conservative until their measured/qualification gates pass. Keep dangerous or experimental operations behind explicit reviewed intent and preserve the existing ownership rules. Avoid a single PR combining all 44 packages; use cohesive tranches or stacked reviewable changes following active-chat repository instructions. Preparing this handoff does not authorize publishing or merging from this side conversation.

For research packages, define a finite experiment and product-readiness criterion in advance. A completed prototype with a negative/inconclusive result is documented as experimental, not a production feature or a fabricated improvement. Do not spend unbounded iterations seeking a flattering result. Continue unrelated work while external qualification is unavailable.

Final handback must include: coverage ledger for A/B/H1 and opening concerns; completed versus blocked scope; links to reviewable changes; exact tested revisions and commands; before/after performance with limitations; migration/update/rollback instructions where affected; and remaining site/hardware/user-study gates. The main agent should start with Gate 0 and tranche 1, then continue through the accepted, unblocked backlog using the companion kickoff prompt.
