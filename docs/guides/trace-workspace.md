# Trace Workspace

Trace Workspace opens the verified recordings for one exact job attempt in a terminal or browser. Use it to follow a running job, inspect a retained recording, compare recorded functions, or explore process, CPU and GPU activity lanes.

The viewer is portable. Recording support depends on the application's runtime, operating system, permissions and available profiler. Python calls, CPU placement observations, scheduling slices, GPU activities and sampled stacks remain distinct kinds of evidence.

## Start with a recorded job

Keep an existing submission script and enable Raven's Python tracing:

```bash
raven launch job.sh --project "$PWD" --trace-profile pilot --check
raven launch job.sh --project "$PWD" --trace-profile topology
```

On a lab-account cluster, select the scientific job's account and partition through the existing launch review or explicit launch options. Your login name and billing account are separate. Raven Heavy has its own resource settings; viewing a recording does not silently submit another scientific allocation.

CPython 3.12 or newer supports the monitoring adapter. Python 3.10/3.11 requires explicit `--trace-adapter python-legacy-profile` consent. The legacy adapter has reduced yield/unwind fidelity. See [recorded stacks](stack-tracing.md) for environment propagation, native hooks and capture limits.

The launch wizard exposes Python call tracing, presets and a byte/process budget preview beside its existing recording choices. Presets expand into existing launch settings; explicit flags override their corresponding preset value. They preserve the original script and do not reserve additional application CPUs or memory.

| Preset | Python scope | CPU placement | Per process / per run | Maximum processes |
|---|---|---|---|---|
| `project` | Project code | Off | 64 MiB / 256 MiB | 128 |
| `topology` | Project code | Observation points | 64 MiB / 256 MiB | 128 |
| `pilot` | Project code | Off | 4 MiB / 32 MiB | 16 |

These limits bound retained data, not application slowdown. Compare a representative short pilot with an unrecorded baseline before raising budgets or enabling dependency-wide tracing.

A complete example is available from the repository:

```bash
bash examples/trace-lab/run.sh --slurm --cpu-placement
```

The [trace topology examples](../../examples/trace-topology/README.md) provide
separate single/multi thread, core, node and GPU programs with matched launch
resources, two-minute pacing and exact-receipt viewing instructions.

For a workstation without Slurm, use `--local`. The helper prints an exact launch receipt. Open it with the existing `examples/trace-lab/view.sh` workflow, bind the printed project/run, and select that job in Jobs, Recents or History. Do not choose a recording solely because it has the same numeric job ID.

## Open the workspace

From a selected job's Trace panel, choose Workspace or enter:

```text
:trace workspace
```

The global Traces destination uses the same session. Choose a published Jobs/History record or open an exact retained manifest, including a standalone recording whose scheduler row is no longer available:

```bash
raven --tab traces
```

```text
:traces
:trace workspace open /absolute/project/.raven/traces/STREAM/manifest.json
```

Use a real manifest printed by the recorder; `STREAM` above illustrates the path structure. Explicit opening verifies the journal and declared attempt. It does not independently verify scheduler membership or grant actions on a running job. The bounded run picker is a navigation aid, not an exhaustive filesystem scan.

The run identity and source list identify the selected attempt, process/rank, node and recording adapter. Read/committed counts and warnings explain whether ingestion is caught up. CPU, memory and GPU sources have independent availability; an unavailable recording must not hide another source's valid data. Existing Research/Analytics recordings and trace-clock overlays retain their distinct association requirements described below.

### Live, Explore and Fit

- **Live** follows the latest verified data through the trailing Δ time window.
- **Explore** preserves the chosen range and selection while new recording data can continue arriving.
- **Fit** leaves follow mode and fits the current clock's whole verified retained range, or each displayed independent clock's range. Resume Live restores the previous Δ.

Δ changes the visible span, not recording frequency or Slurm polling. Panning, zooming or selecting an interval makes inspection stable. Pausing the view does not pause the application recorder.

Fit preserves run context when a CPU, device or event-type filter is active;
it does not recompute a separate fitted recording extent for every resource
predicate. The overview remains interval-start density for its declared
source/clock scope. **Focus** zooms the actual selected event. Resource filters
still apply to exact evidence before query limits.

The terminal command palette exposes the same actions:

```text
:trace workspace live 10
:trace workspace delta 30
:trace workspace pause
:trace workspace fit
:trace workspace find solve
:trace workspace next
:trace workspace focus
:trace workspace bookmark
:trace workspace functions
:trace workspace parent
:trace workspace child
```

Lane groups can be expanded, collapsed and paged. Full names and timing are available through selection even when a narrow terminal abbreviates the bar label. Completion retains the same run context in History; it does not create a different trace merely because the scheduler state changed. The transition to completed pauses following once and preserves the selected range/event. An explicit subsequent **Resume Live** remains honored, allowing inspection of later committed evidence without repeatedly forcing the view back to Explore.

The compact default row keeps Follow, Fit, View, Δ and Find together. **More** exposes filters, lane pinning, saved ranges, function statistics and analysis modes. For example, `:trace workspace bookmark solve phase` annotates the current range. Bookmarks and presentation preferences are separate from captured evidence. Browser **Copy view link** retains the exact range/event within the current private viewer session; it is not a permanent public URL.

In an 80-column terminal, the expandable hierarchy and recorded **Parent** /
**Child** navigation provide lineage without a second dense horizontal control
row. The browser also shows scope breadcrumbs that clear individual filters.
The global run picker discloses its 64 cached-run bound; use History, an exact
job selection or an explicit manifest for older evidence.

### Choose an execution or resource perspective

| Perspective | What the hierarchy means |
|---|---|
| Execution | Node → process/rank → thread → recorded stack/activity |
| CPU | Node → socket/physical core/logical CPU → recorded observations or execution slices |
| GPU | Node → device/partition → process context → stream/queue → device activity |

```text
:trace workspace projection execution
:trace workspace projection cpu
:trace workspace projection gpu
:trace workspace projection node
```

A software thread can migrate between CPU cores. Sparse CPU-placement points show where it was observed during a query bracket; they do not establish residency between those points. Continuous execution slices require scheduling evidence. Logical CPUs can be SMT threads on the same physical core.

**More → Filters** in the terminal and **Filter recorded execution** in the
browser select node, process, thread, MPI rank, declared logical task, logical
CPU, device and event kind. Predicates apply before indexed result limits;
they do not merely hide rows from an already truncated page. Facet choices are
bounded and disclose omitted values. Exact CLI/terminal values remain usable:

```text
:trace workspace filter kind gpu_kernel
:trace workspace filter rank 0
:trace workspace filter-clear
```

CLI equivalents include `--node`, `--process`, `--thread`, `--rank`, `--task`,
`--cpu`, `--device` and repeatable `--kind`. For example,
`raven trace workspace "$raven_trace_manifest" --projection gpu --kind gpu_kernel`
selects recorded kernels. Declared task IDs/generations stay separate from
physical threads; automatic complete asyncio task lifecycle capture is not
implied. CPU topology can include recorded die, NUMA and heterogeneous core
metadata; unavailable fields stay unknown.

GPU queues are asynchronous device resources. A CPU call that enqueues work is separate from the kernel's execution. Device identity includes the node and physical/partition identity, so GPU 0 on two nodes does not become one device. Unknown placement, device identity or symbols remain explicitly unknown.

### Inspect a function or device activity

Select a bar in the terminal, click it in the browser, or use the accessible event list. The inspector preserves the full label, recorded start/end, duration, source location where present, source identity and coverage. Browser hover provides a short tooltip; clicking pins the selected interval.

Available metrics have explicit scope:

| Value | Interpretation |
|---|---|
| Inclusive elapsed | Matched recorded boundaries; includes waiting and instrumentation effects |
| Self elapsed | Synchronous matched nesting with child elapsed intervals removed; not CPU utilization |
| On/off CPU | Requires appropriate scheduling or thread CPU-time evidence |
| Sample share | Observed weighted samples under the declared sampling method; not an exact call duration |
| GPU activity duration | Producer-reported device execution or transfer boundaries; not occupancy |
| Resource busy time | Union of compatible activity intervals over a stated covered range |
| Occupancy/throughput | Requires the relevant device-specific counter and definition |
| Parallel efficiency | `T1 / (P × TP)` with explicitly comparable workload, input, resource unit/model and single-resource baseline |

Concurrent thread durations do not add up to job makespan. An unavailable metric has a reason rather than a fabricated zero or generic efficiency percentage. Selected-event comparisons show their recorded quantities; they do not establish whole-program speedup without a comparable workload and resource baseline.

## Read a retained recording from the CLI

Use the exact manifest path printed by the recorder or displayed in the source list. This example asks for that path, avoiding a placeholder job ID:

```bash
read -r -p 'Exact trace manifest path: ' raven_trace_manifest
raven trace workspace "$raven_trace_manifest" --json
raven trace workspace "$raven_trace_manifest" --functions
raven trace workspace "$raven_trace_manifest" --overview
raven trace workspace "$raven_trace_manifest" --call-tree --bottom-up --counters --hints --metrics
raven trace workspace "$raven_trace_manifest" --samples
```

Multiple manifest arguments combine sources belonging to the same exact attempt. Other attempts or clusters are rejected. Select a source/clock/range with the options shown by `raven trace workspace --help`; use integer nanoseconds for `--start-ns` and `--end-ns`.

The supplied manifests form a **fixed source set** by default. Live viewing
continues reading newly committed data from those sources, but does not silently
add every sibling process or profiler window registered later. To discover new
sources for the same attempt, opt into following its local project:

```bash
read -r -p 'Local project root containing this run: ' raven_trace_project
raven trace workspace "$raven_trace_manifest" \
  --follow-project "$raven_trace_project" --serve --open-browser
```

Following binds to the initial source's verified committed job/attempt/cluster
identity; matching registration metadata alone is insufficient. A distinct run
ID remains an additional constraint. A redundant `run_id` equal to `attempt`
is normalized only in the common cohort identity, so an equivalent optional
alias does not prevent same-attempt sources joining. Original source identities
and raw journals remain unchanged; this does not merge different attempts.

Job-bound dashboard workspaces already subscribe to the associated run's
registrations. Explicit retained-manifest opening remains fixed unless an
owner registers project following. `--follow-project` is source discovery,
independent of the Live/Explore display mode; without `--serve`, the CLI still
produces a bounded one-shot result and exits.

The chronological graph and aggregate flamegraph answer different questions. A timeline bar occupies its recorded start/end range. A call-tree or bottom-up flamegraph aggregates recorded elapsed values over the returned query; its horizontal position is not chronological time. Large exact queries report their result limit. Narrow the range or source filters to inspect additional evidence. Fit's multiresolution overview counts interval starts in its declared source/clock scope, not utilization or complete activity coverage; resource facets do not turn that overview into a filtered busy-time graph.

To resolve a selected native sample against an explicitly chosen binary:

```bash
read -r -p 'Exact indexed event ID: ' raven_trace_event
read -r -p 'Matching recorded-build ELF path: ' raven_trace_binary
raven trace workspace "$raven_trace_manifest" --detail "$raven_trace_event" \
  --binary "$raven_trace_binary"
```

Add `--source-root /absolute/source/root` only for source you intend to inspect. Build-ID mismatch leaves a symbol unresolved. Displaying source contents additionally requires recorded digest or revision evidence; a filename and line number alone do not authorize substituting a current source file. The terminal equivalent is `:trace workspace symbol BINARY [SOURCE_ROOT]` after selecting the event. This path uses bounded local symbol tools and never executes the recorded binary or a command embedded in trace data.

For a sampled native stack, add `--symbol-frame N` to select one **original
recorded frame index**, together with `--detail` and the binary/source options.
The terminal translates a selected projected sample frame back to its raw
marker and frame index. Ordinary Python metadata often contains only a
function, filename and definition line; those labels remain useful, but do not
prove that a current file is the recorded source. Unproved excerpts remain
unresolved rather than silently opening an unrelated checkout.

### Resource denominators and sampled shares

`--samples` and terminal `:trace workspace analysis samples` expose observed
sample hits, weighted inclusive/leaf shares and their actual denominators.
Each source/process/event/weight-unit scope is separate, including samples
whose stack was unavailable. Recursion contributes one inclusive hit per
sample. Shares describe observed samples, not exact CPU utilization or the
fraction of all application execution. Frame/symbol work limits leave partial
counts explicit and disable incomplete shares.

Absolute observed on-CPU/device busy durations remain useful without a
percentage. A percentage additionally needs an effective allocation and
complete compatible coverage. Register a bounded, data-only context explicitly:

```bash
raven trace workspace "$raven_trace_manifest" --metrics --capacity resource-context.json
```

The terminal equivalent is `:trace workspace capacity /absolute/resource-context.json`;
`capacity clear` removes it. Choose the context before opening the browser so
the owner can register it; web requests cannot select filesystem paths. This
descriptor does not request or modify an allocation. It is an explicit owner
declaration, not independent proof of scheduler capacity or capture completeness.

Every epoch needs all fields shown below. Replace the illustrative IDs, times,
counts and coverage claims with actual evidence from the exact catalog and
covered allocation. The schema accepts 1–128 ordered, nonoverlapping epochs
and at most 64 KiB. Contiguous epochs can express changing effective capacity.

```json
{
  "schema": "raven.trace-resource-context/v1",
  "identity": {"cluster": "local", "job_id": "42", "attempt": "exact-run-attempt"},
  "provenance": "Effective allocation and capture coverage for this exact run",
  "epochs": [
    {
      "clock_id": "EXACT_CATALOG_CLOCK_ID",
      "start_ns": 1000000000,
      "end_ns": 2000000000,
      "logical_cpu_count": 4,
      "unit": "logical-cpu",
      "coverage_complete": true,
      "gpu_coverage_complete": false,
      "source_ids": ["EXACT_SOURCE_ID"],
      "provenance": "Recorded effective logical-CPU allocation over this clock range"
    }
  ]
}
```

Optional `identity.run_id` binds the corresponding exact run ID. CPU capacity
uses explicit logical-CPU units; physical cores, SMT siblings and affinity are
not inferred from a processor model. A matching source set, clock, selected
range and covered epochs are required. Filtering by thread/CPU/device/event
kind or search can remove part of the numerator, so a whole-source denominator
cannot silently supply a fraction for that filtered view. Recorded blocked or
runnable scheduler slices have their own observed-state durations; gaps
between samples do not become off-CPU time. GPU coverage is independently
declared, so missing GPU data does not erase valid CPU observations. Unavailable
metrics retain their actual missing-evidence or context error.

The workspace uses a private, rebuildable local SQLite index. Use `--index` only for a private local filesystem path; shared-filesystem index locking has not been qualified. Newly opened index owners revalidate source integrity from the beginning before exposing their rows. This preserves the source-verification boundary; it is not a trusted serialized-reader checkpoint or a guarantee of instant reopening for very large recordings. Another process cannot concurrently own the same index writer.

Cached rows, facet choices and source metadata are scoped to the sources
requested by the current owner. Reopening one manifest cannot silently expose
previously cached peers; add those paths explicitly or use project following.
The default cache has four independently owned index slots. Separate dashboards
can use separate slots for the same run; a fifth concurrent default owner is
refused until a slot is released, or the user selects another private `--index`
path. Reuse/cleanup affects only disposable indexes, never raw recordings.

Reader working state is shared across sources with a **16 MiB Light / 64 MiB
normal** admission budget, including retained metadata, partial blocks, event
windows, code dictionaries and stack state with conservative Python-container
charges. This is separate from the **128 MiB Light / 512 MiB normal** database
budget. Neither figure is a process RSS limit: temporary decoding, SQLite,
returned snapshots and the rest of Raven have other costs. The catalog reports
charged/peak reader state explicitly. A resource-limited source retains its
verified indexed prefix, reports the actual limit and releases its reader so
other sources can progress. Reopen fewer sources to reduce that working set;
changing a display limit cannot recover evidence that failed capture.

Only committed evidence is indexed. Discovery reads bounded registration and
manifest metadata; the index is the workspace's sole journal-integrity reader.
Owned project-follow discovery scans run on a two-second cadence, independently
of the half-second known-journal ingestion cadence and Slurm polling. New
processes/windows can therefore appear after the next metadata scan while
already known sources continue updating. Slow storage, work budgets and browser
refresh add their own latency; these are cadences, not universal deadlines.

An incomplete tail or temporary read error retains the last verified prefix and
reports its actual state. If I/O fails midway through a page, already verified
events are published once and normal-cadence retry continues from that prefix;
the error does not turn sound evidence into corruption or add failure backoff.
Corrupt sources are quarantined independently. “Caught up” means the current
committed prefix was read; it does not prove that uninstrumented work or a
stopped recorder captured the entire application.

## Open the browser viewer

The live browser viewer queries the same owned index:

```bash
raven trace workspace "$raven_trace_manifest" --serve --open-browser
```

Keep this process running while using the live viewer. Stop it with Ctrl-C when finished. Closing the viewer does not terminate the scientific job or a separately running recorder.

The viewer binds to the host's loopback interface and prints a private session URL. Keep that complete URL private. It provides read-only bounded queries for this workspace, not arbitrary filesystem or job-control access.

On an SSH/headless host, a browser-open request may have no visible desktop. The remote host's `127.0.0.1` is not your laptop's loopback. Use an approved SSH forwarding route to the host actually running the viewer, keeping the same local and remote port, then open the printed `127.0.0.1` URL with its session fragment. The viewer validates its exact host/origin, so forwarding to a different local port does not work transparently; restart the viewer for a new port if the selected port conflicts locally. A login-node loopback forward does not reach a compute node's separate loopback service. Follow CARC/RCIC or another site's access policy for the required jump/forward route.

For a viewer running directly on a host you can SSH to, the laptop-side forwarding pattern is:

```bash
read -r -p 'SSH destination running the viewer: ' raven_trace_host
read -r -p 'Port printed in its viewer URL: ' raven_trace_port
ssh -N -L "127.0.0.1:$raven_trace_port:127.0.0.1:$raven_trace_port" "$raven_trace_host"
```

This does not bind the service publicly. Keep the forwarding process and viewer process running, then open the complete printed URL locally. A compute-node route may instead require your site's approved jump-host configuration.

The existing private offline HTML export remains available when a live connection is unsuitable. It is a bounded snapshot, not a live connection or an unlimited whole-recording file:

```bash
raven trace export "$raven_trace_manifest" --format html --output "$PWD/trace-window.html"
```

Export creates a new file exclusively. Existing files are not overwritten. Large retained recordings use indexed range navigation rather than silently truncating a “full” HTML document.

## Import GPU or other activity reports

The standard activity importer accepts bounded Chrome Trace Event / compatible Perfetto JSON activity reports. It preserves producer timestamps, recorded kernels/copies, queue/context identifiers and correlation metadata where present. Native binary profiler files require an appropriate export/import adapter; changing their extension to `.json` does not convert them.

```bash
raven trace import-activity activity.json --project "$PWD" \
  --job-id local --attempt imported-profile-1 --cluster local --max-mib 32
```

Use your actual job/run identity when linking an import to an existing job. The command prints the imported manifest path and source report digest. Open that manifest with the workspace command above. Source process/device labels in a report are producer declarations; journal integrity and a chosen job label do not verify membership of an unrelated live process.

Optional PyTorch activity profiling is available through the profile command:

```bash
raven trace profile --python "$(command -v python3)" \
  --project "$PWD" --job-id local --attempt profile-1 \
  --window-steps 5 --max-windows 16 --gpu -- train.py
```

Activate the application's environment before selecting `python3`, or supply its absolute interpreter path with `--python`. Inspect `raven trace profile --help` for recording-budget options. The selected profiling interpreter needs the application dependencies and a compatible PyTorch profiler. Raven does not install a driver, grant GPU access, or obtain PyTorch from an unrelated environment automatically. A missing GPU path must remain distinct from successfully collected CPU activity. Use the explicit required policy only when capture failure should prevent or fail the profiling workflow.

A generic script profile exports at completion. For intermediate windows, add an explicit cooperative step after each application iteration:

```python
from raven.tracing.activity_capture import step

for batch in batches:
    train_step(batch)
    step()
```

`step()` is harmless outside an active wrapper. It advances the owning thread's scheduled window; it does not synchronize the GPU. The wrapper makes Raven's capture module available in the chosen application interpreter. Direct SDK use requires Raven importability there. `--window-steps` and `--max-windows` bound the number of explicit steps/windows. Byte limits apply to report import and retained artifacts; PyTorch's internal buffer and temporary export remain tool-dependent. Unsupported capture stops with a durable receipt while optional scientific work continues. `--required` instead prevents startup without capture, or fails a successfully completed application wrapper if required capture subsequently fails. An original application exception or nonzero exit remains the original failure.

Framework Python replay stacks are off by default. The optional `--framework-stacks` flag is experimental and separate from Raven's Python call recorder. In a real CPU-only pilot, PyTorch 2.6.0 with Python 3.12.14 produced a replay-stack assertion during scheduled windows with this option. Operator-only activity recording completed, produced two verified windows, and retained a shared producer-session clock. This qualifies that CPU adapter exercise, not CUDA/ROCm capture, GPU call stacks or every framework version.

Run the [two-minute activity example](../../examples/trace-lab/README.md#framework-activity-windows) for a complete cooperative loop. Scientific output can precede the wrapper's final JSON receipt on stdout; the durable receipt path is also printed on stderr.

### Sample an owned native program

On Linux with a usable, permitted `perf` tool, launch a new program under the optional sampler:

```bash
raven trace sample --project "$PWD" --job-id local --attempt native-pilot \
  --frequency 99 --duration 30 --max-mib 64 -- ./solver --iterations 100
```

This records sampled callchain observations of the newly owned child. It does not attach to an arbitrary PID or change system-wide kernel permissions. A sample's weight and CPU observation are distinct from matched call duration and continuous CPU residency. The collector's duration/storage budget stops collection while the scientific program continues. Optional unavailable capture runs the original application once; `--required` requests capture as a launch prerequisite. Live `perf` qualification requires the actual tool and permissions; the implementation environment did not provide them.

For managed Raven execution, both profiling launchers must run inside an independently verified owned Slurm job/step. An account name, `SLURM_*` environment hints or an available Heavy dashboard do not substitute for placement verification. Keep the application's existing account, allocation and site-approved launcher; these commands do not obtain a nested allocation or move scientific execution to a login node.

### Retain a difficult incident

Use the cooperative flight recorder when only a short incident matters:

```python
from raven.tracing.activity_trigger import TriggeredCapture

with TriggeredCapture(project, job_id=job_id, attempt=attempt,
                      pre_seconds=5, pre_events=4096, pre_bytes=1 << 20,
                      post_seconds=5, post_events=4096, max_bytes=16 << 20) as trace:
    for step_number in range(total_steps):
        with trace.span("solve step"):
            residual = solve_step()
        trace.emit("counter", {"name": "residual", "values": {"residual": residual},
                               "units": "dimensionless"})
        if residual > incident_threshold:
            trace.trigger("Residual exceeded the declared threshold")
```

Set `project`, `job_id` and `attempt` to the exact current run. Before the first trigger, a bounded in-memory ring intentionally evicts older metadata; the receipt counts evictions. The first trigger publishes the retained prefix and enables live post-event reading. Later trigger calls do not restart the budget. Post time/event/journal limits stop capture while application work continues. No trigger produces a `never-triggered` receipt and no persisted event journal. An evicted span entry leaves its later exit unmatched; the viewer does not reconstruct a fictitious full call. See the [incident example](../../examples/trace-lab/README.md#triggered-incident-capture) for a runnable program.

## Connect scientific measurements to the trace

Existing Raven Python measurements can supply the counter/phase lens without
another recording API. Launch the existing reporting script with `--trace` or
a trace profile, then keep its ordinary micro-API calls:

```python
import raven

with raven.phase("solve"):
    residual = solve_step()
    raven.metric("residual", residual, kind="residual")
    raven.progress(completed, total, unit="iterations")
```

When one owned Python capture is active and reporting is configured, admitted
measurements enqueue into **that same recorder**, using its exact attempt,
monotonic clock and thread lifetime. Existing scientific JSONL output remains
unchanged. `@raven.measure` uses the same path. No extra metric writer, scheduler
poll or second tracing owner is introduced by this connection. Without a
configured active capture, standalone/vendored reporting retains its ordinary
behavior.

The trace overlay admits at most 20 observations/s per metric group and 64
metric names across configured streams per capture. Coverage metadata
`scientific_metrics` and event `overlay` fields record coalesced/dropped or
unavailable observations. Counter plots show retained points; they do not
interpolate missed values. An ambiguous pair of active recorders does not
borrow either identity for measurements. Optional hook failure preserves
scientific output and application execution.

Historical metrics JSONL and existing CPU/memory recordings may contain only
epoch timestamps, without an exact-attempt/boot identity and a recorded
epoch-to-monotonic calibration bracket. Those files remain usable in their
existing Research/Analytics views, but are not guessed onto a trace clock.
Readiness explains the missing association/calibration evidence. Use the
same-recorder path for new scientific overlays, or import a source with
explicit compatible identity/clock evidence.

## Multiple nodes and independent clocks

Sources with proven common monotonic node/boot clocks can share a timeline. Independent node and device clocks require explicit clock transforms and uncertainty. Without them, inspect the declared clock domains separately; equal-looking timestamps are not proof of simultaneity.

An imported report can declare one common report clock. That declaration is scoped to the report digest and does not independently qualify its producer's host/device calibration. Causal links require explicit matching correlation or flow evidence. Missing links and clock uncertainty limit queue-delay, overlap and critical-path conclusions.

The owned framework collector can declare a producer-session clock across its own windows. That identity includes the session and process lifetime; changing report epochs is rejected. This provides continuity for one producer, not cross-node synchronization. Explicit calibrated reference axes remain opt-in and preserve raw timestamps, validity ranges and stated uncertainty. Choose a reference clock in the browser or use `:trace workspace clock CLOCK_ID` / CLI `--clock-id` with a clock ID from the catalog. The raw multi-node view keeps independent axes.

Place node recordings on accessible, correctly propagated storage or inspect them on their recording host. The viewer must not substitute a local file for an unavailable remote path. A generated rank/topology fixture qualifies software behavior, not actual multi-node launch, permissions, filesystem durability or GPU capture.

## Investigate causality and compare runs

In the browser, enable **Explicit flows** for recorded endpoint links. Select an event and choose **Analyze selected range** for its observed dependency slice, counter/phase lens and scoped investigation hints. The call-tree section provides chronological-independent aggregate flamegraphs and an accessible table. Terminal **More** offers corresponding analysis modes, for example:

```text
:trace workspace analysis call-tree
:trace workspace analysis bottom-up
:trace workspace analysis causal
:trace workspace analysis counters
:trace workspace analysis hints
```

CLI `--causal EVENT_ID` requests the same bounded observed dependency slice. Its chain follows supported explicit dependencies, protocol semantics, flows and scoped typed correlations. Recorded call nesting has its separate call-tree/parent-child view. Missing nodes, cycles, query limits and uncertain clocks remain visible. It is an observed evidence chain, not a proven global critical path. Investigation hints link to supporting event IDs and proposed checks; empty trace regions do not prove idle resources or blocking.

Supported recorded PMPI `MPI_COMM_WORLD` collective entry/return points also
participate after the existing collective auditor validates exact
communicator/generation/sequence, membership, signatures and call lifetimes.
A Barrier's observed entries precede successful returns; positive-count SUM
Allreduce links inputs to results. Positive-count Bcast links root input to
receiver return, without inventing a barrier. Zero-count/unsupported data
dependencies retain only matched local entry/return ordering. Missing/repeated
ranks, gaps or ambiguous phases produce a diagnostic rather than a deadlock
claim. Edge fanout is bounded and omitted edges are counted. Raw profiler
correlation numbers without versioned typed launch semantics stay unresolved
with their actual reason; explicit standard flow endpoints remain usable.

Register baseline manifests explicitly when starting a CLI/browser session:

```bash
read -r -p 'Exact baseline manifest path: ' raven_baseline_manifest
raven trace workspace "$raven_trace_manifest" \
  --baseline "$raven_baseline_manifest" \
  --comparison-context comparison-context.json --serve
```

`comparison-context.json` uses `raven.trace-comparison-context/v1`. It declares provenance, `workload_comparable`, `inputs_comparable`, `builds_comparable`, and both `current` and `baseline` descriptors. Each descriptor needs matching `workload`, `input`, and `scope` identifiers; a `build`; positive `elapsed_ns`; `elapsed_provenance`; and `resources` with `count`, `unit`, and `model`. Units are `logical-cpu`, `physical-core`, `gpu`, `rank`, `process` or `worker`. Different resource models/units or a multi-resource baseline make parallel efficiency unavailable. Recorded build disagreement also prevents a misleading comparison. Unknown cross-run symbol identity requires explicit one-to-one `source_pairs` from the two catalogs. Merely sharing a function name is insufficient. Browser-selected interval comparison remains a simpler recorded-duration comparison, distinct from this workload baseline.

The following illustrates the context file's exact shape. Replace every workload,
build, provenance, timing and resource value with evidence for your comparison;
these example numbers are not qualification results:

```json
{
  "schema": "raven.trace-comparison-context/v1",
  "provenance": "User-declared comparable pilot runs; timing source described below",
  "workload_comparable": true,
  "inputs_comparable": true,
  "builds_comparable": true,
  "current": {
    "workload": "solver-v1",
    "input": "input-sha256-or-stable-id",
    "build": "recorded-build-id",
    "scope": "whole-run",
    "elapsed_ns": 2000000000,
    "elapsed_provenance": "Measured application wall elapsed for this exact run",
    "resources": {"count": 4, "unit": "logical-cpu", "model": "declared CPU model"}
  },
  "baseline": {
    "workload": "solver-v1",
    "input": "input-sha256-or-stable-id",
    "build": "recorded-build-id",
    "scope": "whole-run",
    "elapsed_ns": 8000000000,
    "elapsed_provenance": "Measured single-resource application wall elapsed",
    "resources": {"count": 1, "unit": "logical-cpu", "model": "declared CPU model"}
  },
  "source_pairs": []
}
```

When source identity needs an explicit cross-run mapping, add entries such as
`{"current": "CURRENT_SOURCE_ID", "baseline": "BASELINE_SOURCE_ID"}` to
`source_pairs`, using exact IDs from each catalog. The mapping is one-to-one.
It declares intended matching; it does not independently validate scientific
equivalence. The browser accepts only baselines registered by the local owner
when starting the session, never arbitrary filesystem paths from a web request.

## Recording health and recovery

Check the source list for adapter, read/committed counts, fidelity, warnings and actual read errors. A gap, missing exit, stopped capture quota or unknown tail remains visible. Fit cannot reconstruct observations that were never recorded or retained.

### A launched job has no visible trace

An application exit code of zero does not prove recording availability. Check
the exact run's `run.json` warnings and `.raven/traces/*/manifest.json` files.
`committed_seq` is the producer's retained-event count; journal integrity is
verified separately when Raven reads the source. A closed manifest with events
can still be unavailable to a selected scheduler row when its run association
has not been established.

The Trace views distinguish missing recordings, unassociated recording metadata
and unreadable metadata. Automatic discovery uses the selected job's published
working directory and the project's exact attempt inventory. It does not attach
a recording by numeric job ID alone or ignore conflicting scheduler identities.

To select an existing run manually, open Raven and use its `:` command bar:

```text
:project /absolute/project
:run select EXACT_RUN_ID
```

Enter these inside Raven, one at a time after each read completes. For direct
inspection from a shell, use the exact manifest path:

```bash
raven trace workspace /absolute/project/runs/EXACT_RUN_ID/.raven/traces/STREAM/manifest.json \
  --serve --open-browser
```

Explicit retained-file inspection does not establish scheduler ownership or
prove that the file belongs to another currently selected job. Preserve the
original recordings while resolving project, cluster or attempt mismatches.

### Range limits, catch-up and unknown stack prefixes

- **Range query reached its event bound:** the current exact-event query is
  truncated. Zoom in or filter by function, thread, process or device. Fit can
  cover the full time axis while its bounded event query remains partial. The
  retained journal and independent full-index function statistics are not
  reduced by this display limit.
- **More committed records remain:** the reader is still ingesting a committed
  prefix within its time/event budget. Watch the read/committed counts; this
  message clears when ingestion catches up. A visible terminal Trace view uses
  its catch-up deadline independently of scheduler polling. If counts stop
  advancing, inspect the source's actual read error and index/storage status.
- **Return without a recorded entry:** the observed stream lacks an entry for
  that return, so the preceding stack cannot be reconstructed. Recording begun
  inside an existing frame or a capture gap can cause this. Automatic capture
  excludes the loaded Raven instrumentation runtime and its verified generated
  startup file; it preserves application code, including ordinary files named
  `raven.py` or `sitecustomize.py`. Existing recordings retain their original
  boundaries and warnings.

If an index is damaged, choose a fresh private index path or reopen through the normal workspace workflow and let it rebuild from retained journals. Preserve the raw journals while diagnosing source integrity. User-selected quotas and slow shared storage can limit ingestion independently of application recording.

Hardware capture availability is described by `raven trace capabilities` on the execution host. A detected tool or runtime is a prerequisite, not proof of a live-qualified capture. See [capture capabilities](trace-capabilities.md) and the [implementation/qualification ledger](../implementation/trace-workspace-coverage.md) for scope and evidence.
