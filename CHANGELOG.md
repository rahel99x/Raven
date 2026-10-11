# Changelog

## 6.1.0

- Fold the 56 tracked proposal entries into eight owning areas, with generated
  capability references and explicit links to shared operations and qualification gates.
- Use one registry for command metadata, static toolbar controls, task workspaces,
  completion and command help. Preserve aliases and contextual execution handlers.
- Share pane context and document geometry across Jobs/History Details, Research
  and Analytics. Keep each pane's zoom separate from metric collection identity.
- Unify capture ownership across pointer controls; preserve press/release ordering,
  cancellation, stale-release guards, live updates and modal interception.
- Publish immutable metric revisions with correction tracking and bounded retention.
  Reuse prepared source data, copy only visible cached table rows, and avoid hidden
  heatmap work. Reuse exact chart time buckets across presentation changes.
- Coalesce governed scheduler and cached Prometheus reads through one acquisition
  interface using existing workers. Retain invalidated source reservations until
  collectors drain; preserve fresh action validation and subscriber cancellation.
- Add a staged investigation workflow over existing evidence, analysis, diagnostic
  planning and reviewed trial execution. Reuse bounded derived results with exact
  source, attempt, algorithm and parameter identities.
- Share committed-prefix trace acquisition across causal and collective analyses.
  Treat explicit incomplete coverage and absent matching registrations as missing
  evidence; guard against newly registered streams before publication.
- Share trial admission, staging, execution, measurement and receipts. Revalidate
  captured action plans before dispatch. Fix cancellation after closed output pipes
  and prevent child launches after preparation exhausts the trial budget.
- Preserve existing project schemas, commands, scientific algorithms, trial drivers,
  ASCII/Unicode layouts and source limits. Real cluster and hardware qualification
  remains site-specific; no universal lossless tracing claim is added.

## 6.0.0

- Add Run, Investigate, Compare, Results and Connect workspaces, searchable and
  pinned tools, a project connection guide with sample validation and Bash hooks,
  generated operation reference, and contextual controls.
- Add Jobs and History Trace details with exact-attempt discovery, incremental
  journal following, bounded Python monitoring, native process/thread instrumentation,
  supported automatic PMPI collectives, and reviewed nonblocking Python snapshots.
  Record coverage, sequence gaps, storage exhaustion and uncommitted crash tails.
- Join scheduler transitions, bounded log tails and registered trace evidence into
  an automatic investigation timeline. Preserve clock domains and unknown times.
- Add causal delay and collective analysis, recommendation validity, investigation
  planning, scientific lineage impact, numerical stability, diagnostic campaigns,
  input minimization and measured regression sweeps over immutable Git snapshots.
- Add calibrated execution scenarios with held-out acceptance and model withdrawal,
  declared scientific-field queries,
  evidence-preserving summaries, cooperative live steering and I/O coordination.
  Include optional MPI and PyTorch application integration examples.
- Add evidence receipts, site policies, governed shared scheduler reads, a bounded
  adapter protocol, exact-job Prometheus queries, supported Perfetto/Caliper/OTLP
  JSON and Workflow Run RO-Crate conversion, and reviewed private support bundles.
- Fix stale pointer release ownership, keyboard activation of task controls,
  changed-job trace publication, isolated SSH reader dependencies, and operation
  cancellation/freshness through governed backends. Reap owned supervisor exits
  without stopping active persistent services. Preserve existing controls.
- Cache immutable scalar histories and unchanged trace documents; use short
  trace-reader batches, shared nested worker admission, and coalesced
  asynchronous preference writes with atomic recovery.
- Add offline CLI/PTY/wheel qualification, release checksums and contents inventory,
  SPDX metadata, and versioned schema/example checks.
  Document supported scopes and untested deployment gates. This release does not
  claim universal lossless tracing or production qualification on every cluster.

## 4.16.1

- Add Collapse All beside Expand All in Jobs Main, with the matching `:grouping collapse` command. Preserve membership, manual exclusions, and explicit fold ownership when Undo Auto is used.
- Keep the grouping controls fitted to narrow panes and retain the existing bounded fold storage.

## 4.16.0

- Add captured mouse column resizing with cancel/reset controls, shared Jobs and Recents geometry, and clipped headings that retain correct hit targets. Keep completion trails on the left of Main.
- Add inclusive Shift-click job ranges and allow plain drag selection from selected rows. Reserve Alt-drag for group membership and preserve native and raw-terminal mouse isolation.
- Add automatic-group, undo, clear, and expand controls. Preserve manual memberships and fold ownership. A dedicated group handle prepares reviewed, same-owner pending-job ordering through supported Slurm `scontrol top`.
- Add Bash Research reporting for actual submissions, dependencies, arrays, retries, lifecycle, metrics, artifacts, and declared native sources. Discover attempt-bound sources locally and through supported SSH readers during runtime.
- Guard stale gestures, scheduler identity changes, ambiguous submissions, concurrent report publication, and fast job startup. Add focused and sequential interaction regressions.

## 4.15.1

- Restore NVIDIA GPU measurements on mixed-graphics desktops by resolving
  numeric CUDA visibility to stable device UUIDs inside the job allocation.
- Resolve ambiguous device subsets with a bounded CUDA Driver API helper.
  It creates no CUDA context and requires no new Python package. Cache identity
  briefly per allocation and node; invalidate changed visibility or inventory.
- Suppress obsolete ambiguity warnings after a valid stable match, so successful
  collection does not trigger retry backoff. Preserve mixed-vendor, allocation,
  unknown-counter, and physical MIG-parent safeguards.
- Explain allocation identity failures in GPU diagnostics and document recovery.
  Add hybrid-desktop, driver-helper, cache, and sampler-to-chart regressions.

## 4.15.0

Implement the remaining 26 research proposals through a shared terminal
operations workbench. Open Research → Operations or enter `:ops`. Related
controls also appear in job Details, Research views, Analytics, and Sources.

- Inspect storage and quota evidence, edit pending jobs, adjust array limits,
  inspect reservations and licenses, diagnose Slurm, recover retained scripts,
  and open a reviewed allocation shell or step attachment.
- Validate software environments, measured placement, scientific acceptance,
  statistical comparisons, verified output reuse, and dependency-chain repair.
- Prepare checkpoint recovery, adaptive searches, packed tasks, optional Dask
  pools, and heterogeneous allocations. Use durable monitoring, verified data
  staging, and read-only Nextflow or Snakemake connectors.
- Inspect distributed bottlenecks, multiple clusters, historical incidents,
  and energy per useful result with explicit attribution and missing evidence.
- Keep collection off the renderer. Bind action reviews to their connection,
  job attempt, and input evidence; reject stale and repeated actions.
- Validate cross-feature sequences, worker-mode transitions, bounded reads,
  subprocess cleanup, and mouse isolation in ASCII and Unicode layouts.

See the [operations guide](docs/guides/operations.md) for controls, contracts,
optional dependencies, and site limits. The separate live stack tracer remains
a future 5.0 proposal.

## 4.14.0

Phase one adds measurement and submission checks while retaining the terminal
interface and optional-adapter architecture.

- Inspect Tower polling separately from Slurm collection capabilities.
- Select NVIDIA, AMD, or Intel GPU collection where the required vendor tool
  and allocation access are available.
- Run explicit local shell syntax checks and optional ShellCheck analysis
  without executing the batch script.
- Attach a validated scientific manifest to a Slurm array using actual task
  indices rather than submission order.
- Keep inspection-dialog mouse events isolated from hidden panes.
- Preserve fitted graph geometry during selection when retained samples expire.
- Keep the selected source visible in short panes and clear stale retry delays
  when the GPU provider changes.

See the [phase one guide](docs/guides/phase-one.md) for controls, data contracts,
and limits. The [roadmap](docs/ROADMAP.md) tracks the remaining proposals.

Earlier release notes continue below.

## 4.13.0

- Group membership supports drag and drop. In the current release, hold **Alt** while dragging a selected job or marked selection onto an existing group. Plain drag selects a range. A cancelled or stale drag does not change membership.
- Right-click a selected or marked job for **Create Group**, **Ungroup**, and applicable **Add to** choices. History also provides **Export logs** through this menu.
- Manual membership uses natural job-ID order, independent of insertion order. With JOBID sorted ascending, adding `7–9`, then `4–6`, to a group containing `1–3` displays `1–9`. Each list retains its active sorting, including default sorts.
- Adding to an automatically detected group creates a manual group with its existing label and known members. Existing manual destinations retain their identity and saved members. See [Add jobs to a group](docs/guides/batch-launches.md#add-jobs-to-a-group).


## 4.12.0

- Mark at least two jobs in a focused job list and press `g` to create a closed manual group. It uses the existing chevrons and state-count summary.
- Press `u` on a closed group to dissolve it. In an open group, `u` removes only the marked members, or the selected member when no jobs are marked. Press `U` to clear marks without changing groups.
- Manual membership and removals apply across job listings. Saved choices identify the execution attempt so that a reused job number does not inherit an earlier grouping.
- Menus, text selections, graphs, and graph/slider drags retain their input controls. See [Manual groups](docs/guides/batch-launches.md#create-a-manual-group) for selection and persistence rules.


## 4.11.0

- Running metric graphs use a short adaptive display delay. The visible edge advances through measurements already received, which reduces the new-segment jump at each source update.
- Full-history and Live views follow the delayed display time, including the full-screen chart while it follows its running source. Duration controls and source polling remain independent. Graph labels identify the display lag and any wait for more data.
- Completed jobs, comparison plots, and explicit zoom selections retain their existing time bounds. Missing measurements remain gaps. See [Buffered graph playback](docs/guides/charts.md#read-buffered-running-graphs).
- Line plots add a labeled local polynomial trend when enough continuous data is available. Dense oscillations use thin observed low/high borders and a faint interior. Original values, spikes, and missing intervals remain distinct. See [Trend and range layers](docs/guides/charts.md#read-the-trend-and-range-layers).
- Expanded job groups show a right-pointing chevron on their first visible row. Click it to close the group, then click the down-pointing chevron on its summary to reopen it. Existing batch counts and shared fold preferences remain available.


## 4.10.0

- Metric polling controls show both the requested **Set** interval and shared effective **Poll** interval. A faster shared request no longer makes a changed slider appear unchanged.
- Launch grouping uses available queue and accounting evidence for old and new jobs. Closed groups show state counts while retaining exact member identities. See [Automatic launch groups](docs/guides/batch-launches.md).
- History Details provides the same Inspector, Logs, Investigate, Research, Analytics, Quick Advisor, and Off modes as Jobs. Reports remain attached to the selected historical job.
- Grouping and Details use published snapshots and the existing background readers. Mouse movement does not fetch job evidence or activate another page.


## 4.9.0

- One byte decoder handles mouse reports, keyboard controls, Unicode, paste, and resize. Extended mouse coordinates cannot become keyboard shortcuts through partial ncurses decoding.
- Native CPU/GPU series and traces reuse bounded prepared data. Indexed Live windows retain continuous curves, sample gaps, and corrections to older measurements.
- Rendering reuses fitted text, measured widths, and theme styles. A bounded pointer check before paint prevents an old crosshair position from appearing after a rebuild.
- Queue observers release the UI data lock before conversion and callback work. Each observer retains an isolated job snapshot.
- The toolbar switches live between Single and Multi background workers. Single keeps one background worker and a separate UI thread. Accepted work and results survive mode changes. See [worker controls](docs/guides/background-workers.md).
- Use the [sustained-input benchmark](docs/guides/ui-performance.md#measure-sustained-terminal-input) to measure queued input, paint intervals, and display work together.


## 4.8.3

- Delayed mouse reports remain pointer input even when their first Escape byte arrives separately. Active drags allow a short prefix grace period.
- Metric archive reads and sampler file writes run outside the shared data lock. Interactive views use published samples while saved history loads in the background.
- Jobs Details renders the visible metric bands. Off-screen graphs retain their place in the scrollable document without repeating raster work.
- Pointer feedback validates visible Live controls. Background maintenance still checks all retained sampling requests.
- Completed background Research commands preserve the current page, selection, and source when the user leaves their workflow.
- Use `tower --ui-trace ui-trace.json` to record bounded UI timings and page transitions. Quit normally to save the report. See [the timing guide](docs/guides/ui-performance.md#record-a-desktop-latency-report).


## 4.8.2

- The decoder recognizes legacy X10, SGR, and urxvt mouse reports. Version 4.8.3 also covers a delay immediately after the initial Escape byte.
- Clicks use the current published job, source, and pane. Stale row positions and changed hit payloads cannot open another job or page.
- Graph crosshairs repaint changed cells. Unchanged native curves reuse bounded raster caches while axes, source age, and live controls continue to update.
- Drag owners cancel stale captures after context changes. A new gesture retains its own release after an earlier gesture was cancelled.
- Passive movement does not activate buttons, rebuild the page, or read measurement sources.

See [the input audit](docs/guides/input-audit.md) for reproduced faults, regression coverage, and recovery checks.
See [display performance](docs/guides/ui-performance.md) for reproducible measurements.


## 4.8.1

- Each running metric has two adjacent sliders: a display window from 30 seconds to one second, and a polling interval from five seconds to 500 milliseconds. Both scales use logarithmic steps.
- The top-right polling slider also spans five seconds to 500 milliseconds. Each native polling position changes the requested interval; requests cannot exceed two polls per second.
- Right-click either metric slider to restore its default: 30 seconds for the window, or five seconds for its polling request. The other control and graph selection stay unchanged.
- Sampling requests apply to the exact job and source. Shared probes use the fastest global or per-metric request, without multiplying the requests. Source limits and retry backoff remain active.
- The graph footer shows the effective sampling interval. Published application metrics and CSV traces remain limited by how often the job writes them.

See [chart controls](docs/guides/charts.md#follow-a-running-metric) for mouse, keyboard, commands, and sampling limits.


## 4.7.1

- The supplied desktop profile enables GPU sampling for detected running GPU allocations. Use `:gpu on` if an earlier session saved the toggle as off.
- GPU detection reads allocation TRES when the per-node queue field omits a `--gpus` request.
- Analytics finds GPU traces without opening Inspector first and shows sampling errors before the first sample.
- Unknown NVIDIA counters remain gaps. Trace paths preserve spaces, and missing trace files clear stale readings.
- Use `tower --gpu-check` to inspect current jobs. Add `--gpu-check-output DIRECTORY` to save allocation, command, trace, and cache evidence.

See [GPU detection checks](docs/guides/gpu-detection.md) for Fedora instructions and report interpretation.


## 4.7.0

- Scroll panes with a draggable right-edge scrollbar and header arrows. Advisor, History, Timeline, and comparison documents expose their complete published content.
- Curves use continuous two-by-four Braille strokes. The crosshair follows the latest reported cell immediately and preserves curve and annotation cells.
- Select rendered lines by dragging, or use `v`, navigation keys, and `y`. Raw log copying retains its original source bytes.
- Switch **Copy** to **Yank** in the top toolbar to send text to a running local Vim or Neovim.

See [pane navigation](docs/guides/pane-navigation.md) for controls, copy limits, editor discovery, and fallback results.
See [charts](docs/guides/charts.md) for curve rendering, pointer feedback, and measurement limits.


## 4.6.2

- Graph drags retain their painted axes while new samples arrive. The capture margin extends three terminal cells beyond the axis labels and ticks.
- Thin selectors use themed Braille positions and 80 ms visual easing. Mouse events still resolve to whole terminal cells.
- Right-click clears job and line selections on the main pages. It preserves the open log source and does not activate another control.

Graph right-click still resets that graph. History-list right-click still opens the log-export menu.
Log Tools right-click clears only its local selection and keeps its dialog open.
Help, Details, and analysis dialogs clear local text or row selections and retain job marks.
See [graph interaction](docs/guides/charts.md#point-at-a-metric-graph) and [selection clearing](docs/guides/pointer-navigation.md#clear-selections-with-right-click) for controls and limits.
