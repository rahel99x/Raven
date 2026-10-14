# Work from launch to results

[Getting started](getting-started.md) · [Setup](setup.md) · [Refresh settings](telemetry-updates.md) · [Job grouping](batch-launches.md)

Raven keeps the common workflow close at hand: choose a site and project,
launch a script, inspect its exact run, and retain the results. The default
layout is designed for an 80-column terminal. Less common tools remain
available through menus and the searchable command palette.

## Open the dashboard while Heavy starts

For a configured site, run `ravenh`. The interactive dashboard opens in Light
while the allocation is prepared, queued, and verified. Jobs and logs remain
available during this interval. The Heavy status control opens progress,
cancellation, and recovery actions. Heavy analysis is enabled only after the
worker is verified.

A failed request leaves the dashboard usable. Read its complete notification,
correct the reported configuration or connection problem, and choose Retry
when offered. A retry reviews a new request; it does not silently reuse a job
number or resubmit a scientific workload. An unresolved cleanup must finish
before another owned allocation can start.

A fresh bare launch without configuration still opens guided setup before
requesting compute. One-shot commands retain their required readiness checks.
`ravenl` opens Light without requesting a worker.

## Navigate without crowding the screen

The main navigation emphasizes Jobs, History, Analytics, Research, and Logs.
Use **View** for other workspaces, cluster pages, and diagnostics. The current
destination remains visible when opening a secondary page. Pin useful
destinations through **View → Favorites → Pin this view**; favorites are saved
with the connection's normal UI preferences. The command `:favorite` toggles
the current destination.

Launch and Heavy status remain accessible in the toolbar. Export actions live
in File and Run home. Background-worker concurrency is in **Setup → Refresh
and performance → Advanced preferences**; it is independent of Light/Heavy.
Press F10 to navigate menus without a mouse, or use the command palette to
find an action by name.

Setup has five task categories:

| Category | Typical task |
| --- | --- |
| Connections and accounts | Choose a site or lab profile; configure Raven Heavy's billing and resources. |
| Launch defaults | Save workload resource and recording recipes. |
| Recordings and metrics | Connect observations and validate a real sample. |
| Refresh and performance | Set reader intervals, Heavy presets, and advanced preferences. |
| Installation and diagnostics | Open guided setup or inspect installation and source health. |

Research and Analytics provide **Set up this view** beside their own content.
Scientific and tracing inputs appear in launch defaults when their associated
features are selected.

## Control a job list with three menus

| Control | Meaning |
| --- | --- |
| View | Grouped keeps each batch contiguous; Sequential shows individual jobs. |
| Order | Fewest running first or Most running first; both break ties with fewer total jobs. Secondary lists can retain their column/completion order. |
| Groups | Expand or collapse groups; manage assignments through a separate menu. |

Changing View preserves group assignments, fold choices, and the saved batch
order. Changing Order through its menu preserves the current view. Removing
assignments is an explicit reviewed action because assignments are shared
across job lists. The current Raven Heavy allocation remains pinned by its
verified identity.

Recents continues to receive completed, failed, and cancelled jobs. Sequential
shows completion order; Grouped keeps related jobs together. A selected job's
completion preserves its identity and the Jobs scroll position.

## Distinguish recording, reading, and freshness

Open the toolbar's queue/recording summary, or enter `:refreshsettings`.

- **Queue interval:** how often Raven requests scheduler observations.
- **Recording-read interval:** how often Raven reads accessible saved samples.
- **Recording frequency:** how often the application or job recorder writes samples.
- **Observed freshness:** the age of the latest read or actual sample, when known.

The panel contains queue controls, recording-read controls, Heavy presets, and
advanced overrides. Custom fields show explicit units, effective values, and
Default/Ultra references alongside the color gradient. Site policy, shared
budgets, and remote-reader limits still apply. Faster reading cannot create
missing historical observations or speed up the application's recorder.

## Connect real data and verify the result

Open **Setup → Recordings and metrics → Connect data and validate a sample**,
or enter `:connectdata`. Selecting an exact run supplies its declared sources.
You can also enter an existing metrics file on the connected filesystem.

1. Choose **Validate sample**. Raven reads a bounded sample and reports actual
   parsing errors and missing fields.
2. Inspect the independent CPU/memory, GPU, scientific-metrics, and tracing
   readiness indicators. An unavailable GPU source does not hide valid CPU or
   application measurements.
3. Preview an actual metric and its recorded values. A single observation is
   a value; it does not establish a rate or trend.
4. Choose **Open chart in Research** to attach the verified data. A manually chosen file is
   identified as manual data; Raven does not assign it to a run by its name.

If the application does not yet write measurements, choose the snippet page
and select Bash or Python. Copy the generated helper into the application or
submission workflow and pass real measured values. Generating or copying a
snippet does not execute it or alter a submission script. The generated helpers
use the per-attempt environment supplied by `raven launch` or the matching V2
reporting kit. See [Connect data](connect-data.md) for copyable integration
steps and source-specific checks.

Scientific metrics use UTF-8 JSONL: one complete JSON object per line, epoch
seconds in `t`, and finite numeric values in `metrics`. For example:

```json
{"t":1792000000.0,"step":1,"metrics":{"loss":0.42}}
```

The timestamp and value above illustrate the format; record the application's
actual values. Optional fields and all other recording formats are documented
in the [format cookbook](../reference/project-reporting-formats.md). Validation
and preview are read-only. They neither request compute nor generate missing
CPU, GPU, or trace observations.

## Keep an exact run from submission through completion

Choose **Open run home** after a launch, **View → Run home**, or `:runhome`.
The project run picker also offers **Open selected run home** and the `h` key.
The page keeps the same run and attempt while scheduler membership changes.
It connects:

- Original script, retained launch settings, workload account, and resources.
- Recorded state and available scheduler observations.
- Logs, independent recordings, research outputs, and export actions.
- Read errors, recovery actions, and a reviewed path to another attempt.

**Run again** copies the retained request into Launch job. Review resource and
recording overrides before submitting. Advanced settings remain those captured
for the old attempt; a changed original script requires a fresh launch review.
Opening this workflow does not submit anything. Starting a new attempt leaves
the old run's evidence intact.

After reconnecting, Run home can recover its saved location by re-reading and
verifying the exact attempt. Saved locations are hints, not proof that a job is
still running or that a new submission is needed. A read failure retains the
existing context and offers recovery rather than automatic resubmission.

Notifications retain complete messages and group repeated occurrences of the
same issue. Relevant notices link to their affected run or settings. An issue
is marked resolved only after an observed successful recovery; dismissing it
is a separate presentation choice.

For an SSH monitoring connection, files remain scoped to that remote host.
Submitting a workload still requires running Raven on the permitted submission
host with access to the project and Slurm commands.
