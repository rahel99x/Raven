# Metric charts

[README](../../README.md) · [Controls](../CONTROLS.md) · [Live workbench](live-workbench.md)

Use Raven's charts to inspect recorded measurements, select time intervals, and compare jobs.
Raven draws charts with terminal characters.
Unicode curves use continuous Braille strokes with two horizontal and four vertical positions per cell.
ASCII curves use directional strokes. Filled area graphs retain their block presentation.
Both modes use the same samples, time windows, and scale limits.
Chart display controls do not change source data.
The separate polling control requests background measurements for an existing running job.
Requests remain subject to Raven's collection policy and the producer's measurement cadence.

**Prerequisites:** Select a job with recorded resource samples or attach its reported application metrics.
Missing measurements remain gaps; chart controls do not supply unavailable data.

## Open a chart

1. Select a job in Jobs or History.
2. Open Experiment to load its application metrics, or use session resource data.
3. Enter `:chart METRIC`. Use the exact metric ID. Quote an ID that has spaces.
4. Use the arrow keys to select a sample. Use Home or End to select an endpoint.
5. Press Tab or Shift-Tab to change the metric. Raven selects the nearest sample
   time in the new metric.
6. Press Esc to return.

Use Page Up and Page Down to read rows that do not fit in a small terminal.

The selected sample shows its original timestamp, value, and reported step. A
display preference does not change these values. The chart stays attached to the
job that was selected when you opened it.

## Read a long Job series page

Open **Analytics → Job series** in the compact native layout and select the required job.
Use the mouse wheel, Page Up, and Page Down to scroll the metric document.
Use `:series-scroll home` or `:series-scroll end` to reach its first or last rows.
The job heading and navigation controls remain visible.
Up, Down, Home, and End continue to change the job.
Changing the job returns the document to its first row.

Point at the metric content when scrolling with the wheel.
A wheel event over Job history scrolls that browser instead.
Inside Jobs Details, use the Details column's normal scroll controls.
When a slider or directional button focus owns the keys, its controls take priority.

## Point at a metric graph

Move the pointer inside a visible metric plot with recorded data.
Raven draws a thin crosshair at the pointer.
The crosshair and selection outline use the active theme's accent color.
They update when you change the theme.
Unicode selectors use a Braille lattice with two horizontal positions and four vertical positions per terminal cell.
Crossing strokes combine their Braille dots in the same cell.
Their exact appearance depends on the terminal font.
The crosshair preserves the graph's background and has no black outline.
At an intersection, original curve, range-border, fitted-line, and annotation glyphs retain their styles.
The selector can replace faint range-fill cells, which keeps it visible inside a dense band.
It otherwise draws in unoccupied cells and does not punch holes in the curve.
The crosshair stays inside the measured plot, outside its labels and controls.
It marks the pointer's coordinates and does not turn a missing sample into a measurement.

The crosshair moves to the latest reported terminal cell immediately.
Only its Braille dot phase moves within that cell, over at most 24 ms.
This transition is visual only. Mouse events and selected bounds still use whole terminal cells.
The lattice does not increase mouse accuracy or measurement precision.
ASCII and reader modes use static `.` strokes with `+` intersections.
Set `ui.animations` to `false` in a managed Raven profile to disable easing.
The compatibility configuration uses `"animations": false` in JSON or `animations = false` in TOML.
The Unicode selector remains available without motion.
You can also open `:settings`, disable **Interface animations**, and apply the settings.
That setting controls selector easing as well as completion motion.

Hover uses the published graph geometry.
It does not reload the source, change the selected job, or rasterize the curve again.
Easing repaints the cached overlay instead of replaying queued mouse events.
The terminal must report mouse movement.
Use `:terminaltest` when movement reports are missing.

## Read buffered running graphs

Timestamped metric graphs for a running job use a short display buffer automatically.
This applies to native CPU, memory, and GPU series, GPU traces, and reported application metrics.
The same behavior applies in Analytics, Research, and the corresponding Jobs or History Details views when the exact job is still running.
A graph's ordinary full-history view and its Live window both use the delayed display time.
This includes the full-screen chart opened with `:chart METRIC`, while it follows the running source automatically.
An explicit zoom, pan, or time preset keeps its requested bounds.

The buffer keeps the visible edge behind the newest measurements.
Raven advances that edge between observations it has already received, reducing jumps when a source publishes a batch.
The graph remains a view of measured history. Raven does not predict the next value or fill a measurement outage.

The target delay is twice the greater of the effective polling interval and the observed sample interval, with a maximum target of 30 seconds.
For a source measured and polled every five seconds, the target is ten seconds.
For a source measured and polled every 500 milliseconds, the target is one second.
Faster file polling does not reduce a slower application's measurement interval.
The displayed lag reports the actual distance from current time to the graph's display time; it can exceed the target when a source stops reporting.
**Poll** remains the effective read interval. **Set** remains the requested read interval.
Neither value describes display lag.

| Status | Meaning |
| --- | --- |
| `buffered 10s behind` | The display advances through received history, ten seconds behind current time in this example. |
| `buffering` or `buffering 1s behind` | The source has not supplied enough distinct history for normal playback. Any shown duration is the actual lag. |
| `held 15s behind` | The display holds while the source catches up or a larger buffer forms. The lag shown is actual. |

During startup, the graph waits for enough observed history to support playback.
If the source falls behind, the display holds at the last supported time and identifies that state.
When data resumes, the display catches up at a bounded rate of at most 1.25 times normal time.
A long outage can therefore take time to recover. Its missing measurements remain visible as a gap.
The buffer does not delay source collection, alerts, current-value readouts, or saved measurements.
It has no separate slider or configuration setting.

Automatic full-history graphs match their display updates to the available drawing resolution.
Between updates, the plot deliberately retains its previous end time.
A visible edge movement, a changed data boundary, or a changed view can advance the presentation.
The reported lag includes this extra display hold.
This also applies to the full-screen chart while it follows full history.
Live windows and explicit zoom or drag selections keep their own update behavior.
This presentation policy can omit intermediate rasters that would differ from the retained display.
Original timestamps, measurements, and source corrections remain unchanged.

Completed and historical jobs use their retained measured history without this delay.
Comparison plots and explicit time selections keep their selected bounds.
A graph with no valid timestamps cannot use timed playback.
The display clock does not advance past the latest usable measurement, including during startup or an outage.
A full measured curve is therefore possible only where the source provides enough valid data.

## Follow a running metric

A running job can show a Live control and two adjacent sliders above each metric.
The first slider selects the display window. The second selects the sampling interval.
Each metric has its own request. A curve and its filled companion share the same controls.

1. Open the exact running job's metric view.
2. Click **Live off** to enable Live.
3. Drag the first slider toward the left for 30 seconds, or toward the right for one second.
4. Read the duration beside the toggle. Wider rows label the first slider **Delta**.
5. Drag the second slider toward the left for a five-second request, or toward the right for a 500-millisecond request.
6. Read **Set** for this slider's request and **Poll** for the shared effective interval.
7. Click **Live ON** to return to the ordinary retained view.

**Expected result:** Live displays `[display time - duration, display time]`.
The display time follows the [buffered running graph](#read-buffered-running-graphs), behind current time.
The window slider uses logarithmic steps between `30s` and `1s`.
The polling slider uses logarithmic steps between `5s` and `500ms`.
Subsecond axes show fractional seconds in their timestamp labels.
Selected time intervals use relative offsets with adaptive `s`, `ms`, or `us` units and an exact start-time anchor.
Labels use available terminal space and do not imply a finer source cadence.
Each metric retains its own duration and sampling request. A completed job has no active Live or polling controls.
Control rows adapt to the available terminal width.
Compact rows use `○` and `●`, or `o` and `+` in ASCII mode.
The empty symbol is off; the filled symbol is on.
Polling labels use `s`, `ms`, or `µs`; ASCII mode uses `us` for microseconds.
**Set** changes with the slider and identifies its requested interval.
**Poll** identifies the effective interval after global requests, other metrics on the same probe, and collection limits are applied.
Managed launches apply collection floors, site policy, demand gating, and command budgets.
Light and Heavy use different reader settings; see [telemetry updates](telemetry-updates.md).
In a compact row, the effective value precedes the polling track and `S` precedes the requested value after it.
On a connection whose policy permits 500-millisecond reads, `Poll 500ms Set 2s` means this metric requests two seconds while a faster shared request keeps its probe at 500 milliseconds.
Changing **Set** in that case is still accepted even though **Poll** stays unchanged.
The footer also reports the effective interval.
Slider values and tracks update immediately during a drag.
Graph previews are limited to one rebuild per 50 milliseconds while the slider moves; release applies the final position immediately.
With Live off, changing Delta stores the next Live duration and updates the control without rerasterizing the retained graph.
These intervals do not establish the spacing of measurements produced by the job or guarantee that every read completes on time.

Select the slider with the mouse or directional button focus.
Use Left and Right for one slider step, Page Up and Page Down for larger steps,
Home for the left endpoint, and End for the right endpoint.
The window endpoints are 30 seconds and one second.
The polling endpoints request five seconds and 500 milliseconds.
Press Enter or Esc to leave slider focus.
Press Esc during a drag to restore that slider's previous value.
A page, source, geometry, or terminal-size change also discards an unfinished drag.
Right-click the window slider to restore 30 seconds.
Right-click the polling slider to restore its five-second request.
Each reset preserves the other slider, Live state, selected job, and graph zoom.
Right-click inside the graph retains its separate full-view reset behavior.

The control graph assigns a temporary token such as `m1` to each visible source.
Use these commands with that visible token:

| Command | Function |
| --- | --- |
| `:metric-live TOKEN on\|off\|toggle` | Change the moving display window state |
| `:metric-window TOKEN SECONDS` | Set a duration from 1 to 30 seconds |
| `:metric-window TOKEN focus` | Focus the window slider |
| `:metric-window TOKEN reset` | Restore a 30-second window |
| `:metric-sampling TOKEN N` | Select polling position 1 to 100; position 1 requests five seconds and position 100 requests 500 milliseconds |
| `:metric-sampling TOKEN focus` | Focus the polling slider |
| `:metric-sampling TOKEN reset` | Restore the five-second polling request |

Tokens are session controls, not project metric IDs. Commands reject a hidden or nonrunning source.
Previously set sampling requests remain active when their graphs move offscreen.
They belong to the exact job, source, and run attempt; completion, departure, or a changed attempt invalidates them.
Raven retains at most 128 metric identities. An evicted request returns to its default.
Live and sampling preferences do not survive a restart.

Live and the first slider change only the displayed interval.
The polling slider changes background read requests even when Live is off.
The graph display clock requests at most ten scheduled refreshes per second while an uncaptured moving plot is visible.
Input and scrolling animations can request other display refreshes.
A one-second window can fall between recorded observations when its source reports less often.
The graph can show the line between its acquired edge observations, but its sample count does not include an invented measurement.
Valid empty axes retain their Live controls and time labels, but have no crosshair or rectangular selection until recorded data is visible.
Read the source-age and sampling-cadence note below the graph before interpreting a sparse window.
Missing observations and sampling outages remain gaps.
Current resource captures retain their timestamp precision.
Older saved captures can retain the earlier 0.1-second rounding.
A precise capture timestamp does not establish equally frequent source measurements.

During a rectangular selection, Raven holds both painted axis mappings fixed.
The sampler continues to publish new observations.
Automatic sample updates and changing data limits do not cancel the drag.
Cancelling the selection resumes Live.
Committing a valid time selection turns Live off and keeps its selected interval.
Enabling Live again clears that metric's completed rectangular zoom.

### Read the sampling limits

Raven Light and Heavy request CPU and memory measurements for the selected job
when its Inspector or resource charts are visible. The first observation is
eligible immediately; subsequent reads follow the configured collection policy.
Light starts with a 60-second live-statistics interval. Default Heavy requests
selected live statistics every two seconds; its other presets and Custom values
apply without a restart. Slurm's own accounting interval controls when new
measurements exist, independently of Raven's read interval. CPU rate needs two observations; cumulative CPU efficiency and
memory can appear after the first. Memory observations remain usable when CPU
accounting is unavailable.

Completed-job curves use measurements retained during execution or a saved trace.
An accounting summary supplies totals and peaks, but cannot reconstruct a missing
timeline. GPU graphs in managed sessions use existing job telemetry; see
[GPU collection and diagnostics](gpu-detection.md).

Selecting or attaching a reported run requests its retained telemetry immediately.
The local reader checks accessible recordings every second by default; remote
reads use at least 1.5 seconds. This reader runs independently of Slurm queue
and accounting polling. Job series reports **Retained samples loaded: N
CPU/memory, M GPU** and the actual read error. When a later read is deferred or
fails, cached counts identify the last successful read. Temporary “retry later”
notices preserve existing samples and retry without adding failure backoff.
Available CPU and memory curves remain visible if GPU recording is unavailable.

The per-metric polling slider uses positions one to 100 internally.
For position `N`, its requested interval is `5 × 10^(-(N - 1) / 99)` seconds.
**Set** shows that request; **Poll** shows the shared effective interval after
source limits. CPU and memory share one probe, and GPU curves share another;
the fastest request for a shared source applies to that exact job. Requests do
not multiply. Managed collection settings and site policy still apply.

The top-right queue slider is separate. Managed `raven`, `ravenl`, and `ravenh`
sessions use the active queue base and site-policy endpoints and leave metric
readers at their own cadence. Use **Setup → Heavy telemetry update settings**
for live changes to Heavy readers and request budgets; see
[telemetry updates](telemetry-updates.md). Native compatibility sessions retain
the existing global five-second to 500-millisecond request.

| Source | Reader minimum before site policy |
| --- | --- |
| Live CPU and memory probe | 0.5 seconds |
| Live GPU probe | 0.5 seconds |
| Local application metrics or CSV traces | 0.5 seconds |
| Remote application metrics or CSV traces | 1.5 seconds |

Reading more often cannot make an application or recorder write measurements
more often. Shared budgets, real source errors, command duration, and disabled
sources can delay observations. Raven does not overlap reads to meet a shorter
interval. The graph footer reports the effective interval, source age, and
producer limits. Missing observations stay gaps; a short display window can be
empty even when reading is enabled.

<a id="zoom-a-rectangular-area"></a>

## Zoom a time interval

1. Press the left mouse button inside the required metric plot.
2. Keep the button pressed and move horizontally across the required time interval.
3. Check the thin selection preview.
4. Release inside the same plot or its capture margin.

**Expected result:** The selected interval fills the graph's horizontal extent.
Raven calculates the vertical limits from the complete known curve inside that interval.
Both axes update to the new view.
The selection must span at least two columns.
A click without that width leaves the view unchanged.
After the drag starts inside the plot, its capture margin includes the axis labels and tick row.
It extends three terminal cells beyond that axis rectangle.
The margin is clipped to the actual visible pane.
Moving or releasing inside the margin uses the nearest plot edge.
The margin does not extend hover, initial presses, or right-click targets.
It cannot switch the captured source to a neighboring graph.
The operation changes the display bounds and preserves the original measurements.
Between adjacent known samples, a zoom can show their connected line even when
the selected interval contains no original sample. The sample count still
reports zero for that interval. Missing values and sampling outages remain gaps.

Time labels show offsets from the selected start, using seconds, milliseconds, or microseconds.
Raven selects the unit from the displayed span and the available space.
The note below the plot identifies the start timestamp and interval length.
Very small finite spans can use scientific notation.
Display precision does not imply a finer measurement cadence.

Hold Shift before pressing the mouse button to select an explicit two-axis rectangle.
Move to the opposite corner and release inside the same plot or its capture margin.
This mode requires at least two columns and one row.
It keeps the selected vertical limits instead of fitting them to the visible data.

Press `u` while pointing at the graph to undo the last rectangular zoom.
Press `0` while pointing at it to restore its original rectangular view.
Right-click inside the plot to restore the full view, including an empty zoomed interval.
This also turns off that metric's Live window. Other metric graphs keep their current views.
In Jobs Details, the selected job and marks stay selected.
Use `:chartzoom undo` or `:chartzoom reset` for the last selected or zoomed graph.
In the chart inspector, use the Undo zoom and Reset zoom buttons, or
`:chart undo` and `:chart reset`, for the current metric.
The existing `+`, `-`, time presets, and axis commands remain available.

In an analysis dialog, right-click outside the plot to clear its selected sample or event row.
This also clears the sample interval when the chart inspector is open.
The job, marks, metric, source, and dialog remain open.
Use the arrows or click a row to select again.
An event action or sample-interval anchor requires that explicit selection.
Right-click inside the plot still resets the graph.

Each zoom belongs to an exact metric, source, job, and run attempt.
Another job cannot inherit it.
Zooms are session display state; they do not change files or sampler intervals.
Raven retains at most 128 zoom entries and 16 undo steps per entry.

Press Esc before release to discard the preview.
Moving beyond the margin discards the preview. A later release cannot commit it.
Changing the page, job, source, axis mode, panel geometry, or terminal size cancels capture.
Opening a menu, dialog, or startup preview cancels it too.
A missing release times out after 15 seconds.
Cancellation keeps the previous completed zoom.
On a logarithmic graph, Shift-drag uses that graph's logarithmic coordinates.

<a id="feature-41"></a>

## Time window

Press `t` to change the time window. The control row shows these presets:

| Preset | Result |
| --- | --- |
| `5m` | Show the last five minutes of retained samples. |
| `30m` | Show the last thirty minutes of retained samples. |
| `2h` | Show the last two hours of retained samples. |
| `all` | Show all retained samples. |

Use `:chart preset 5m`, `:chart preset 30m`, `:chart preset 2h`, or
`:chart preset all` to select a preset directly. Use `:chart window SECONDS` for
a custom window. Raven accepts a positive duration of up to 365 days. A preset
does not load samples that are outside the retained history.

Use `+` or `-` to change the zoom. Use `[` or `]` to move the time window. Use
`:chart zoom FACTOR` for a factor from 1 to 1024. Use `:chart pan FRACTION` for a
position from 0 to 1. Zero selects the beginning; one selects the end.

<a id="feature-42"></a>

## Axis scale

| Control | Result |
| --- | --- |
| `a` or `:chart axis auto` | Calculate a linear scale from the visible data. |
| `:chart axis fixed LOW HIGH` | Use the specified linear limits. |
| `g` or `:chart axis log` | Calculate a base-10 logarithmic scale. |
| `:chart axis log LOW HIGH` | Use positive logarithmic limits. |

The lower limit must be less than the upper limit. Both limits must be finite.
For a logarithmic axis, both limits must be positive.

Raven labels the active scale. It shows how many visible values are outside
fixed limits. On a logarithmic axis, zero and negative samples are undefined.
Raven draws a gap for these samples and shows their count. The original values
remain available in the sample inspector and interval statistics.

<a id="feature-43"></a>

## Interpret compressed samples

When samples share a terminal column, Raven retains the first, minimum, maximum,
and last known values in source order. This envelope preserves short spikes.
Unicode curves draw the range with continuous two-by-four Braille strokes. ASCII curves use
`/`, `\`, `-`, `:`, and `+`; an isolated sample uses `.`. If a bucket contains an unknown sample, the bucket stays
unknown. Raven does not draw a bridge through that bucket or a detected outage.
It connects known samples without smoothing their values. Fixed or zoomed
vertical bounds clip a crossing line. A segment wholly outside those bounds stays
blank instead of appearing as a flat measurement on the scale edge.
Straight segments join adjacent known observations; they do not create new measurements.
The same curve renderer serves dashboard, resource, and inspector graphs.

## Read the trend and range layers

Line plots can show `[fit]`, `[range]`, or `[fit + range]` beside their title.
These layers update with the visible measurements and use the active theme.
They do not change original values, summary statistics, selected samples, or axis limits.
Bars, histograms, and heatmaps retain their own display methods.

**Fit** is a local quadratic polynomial model of the displayed curve.
It uses the centre of each visible drawing column's observed range, with at most seven neighboring columns for each fit.
A continuous run needs at least five columns before it receives a fit.
The fit stays within the acquired domain and local observed range.
At each acquired drawing column, its fitted anchor remains inside that column's actual low and high values.
A column with one observed value keeps that value as its anchor; modeled curvature joins the acquired anchors.
It stops at missing data, detected sampling outages, and equal-time jumps.
A short or unsuitable run retains its measured curve.
The fitted line uses a separate theme color; original curve and range-border cells keep priority where the lines meet.

The fit provides a visual summary. It does not supply measurements or forecasts.
It is recalculated for the visible window and drawing resolution, so zooming can change it.
On a logarithmic axis, it operates on the displayed logarithmic values.
Inspect an original sample or its interval statistics when an exact value is required.

**Range** replaces an unreadable dense zigzag with its observed low and high borders.
A drawing column must contain at least four observations and a range of at least two raster pixels.
It also needs two direction reversals, either within that column or shared with an adjacent qualifying column.
An ordinary slope or an equal-time step does not meet this rule.
Adjacent eligible columns join their lower and upper borders continuously.
Unknown or broken columns leave a gap.
The low and high values come from the observed range; the band is not a confidence interval.
Sparse stipple and a dim theme color make its interior faint.
The crosshair can draw over that faint fill while preserving the range borders, measured curve, and fitted line.
Terminals do not provide alpha transparency for these characters.
Unicode uses subcell Braille strokes; ASCII uses its line and dot characters.

Hover and crosshair movement reuse the published plot.
They do not refit the model or read the source.
Fit preparation is bounded by display width, with at most 4,096 drawing columns.
No additional sampling request or dependency is needed.

<a id="feature-44"></a>

## Sample interval

1. Move to the first sample of interest.
2. Press `r` to set the start of the interval.
3. Use the arrow keys to move to its end. Raven highlights the selected chart
   interval. The statistics change as you move.
4. Press `r` again to finish the selection.

Use `:chart range FIRST LAST` to select two visible sample numbers. Numbering
starts at one. Endpoints are included. Use `:chart range clear` to remove the
selection. The interval is attached to the metric ID and source timestamps.

Raven reports the known sample count, missing sample count, minimum, median,
mean, maximum, and the 5th, 95th, and 99th percentiles. Percentiles use linear
interpolation between sorted known samples. The mean uses finite arithmetic
that avoids overflow for extreme values.

Sample coverage is the fraction of selected samples with a known value. Time
coverage is the fraction of the interval covered by adjacent known samples
within 2.5 median sampling intervals. Raven calculates the median cadence from
the complete retained series. It does not fill gaps. A single sample has no
measurable time interval, so its time coverage is unavailable.

<a id="feature-45"></a>

## Observed events

The `Events e` strip marks observed events at their actual timestamps. A diamond
in Unicode mode, or an asterisk in ASCII mode, indicates more than one event in
the same terminal column. Only events for the chart's job and global observed
events are eligible. Resource samples are not repeated as event markers.

Press `e` or enter `:chart events` to open the event picker. Use the arrow keys,
Home, End, Page Up, and Page Down to select an event. Press Enter to open its
exact job or cited source file. Press Esc to return to the chart. Use
`:chart event NUMBER` to open an event directly. The picker shows the full event
numbers, source paths, and original line numbers when they are available.

Use `:chart events off` to hide the strip. Use `:chart events on` to show it.

Phase changes, alerts, checkpoints, and log events appear only when a published
observation includes a timestamp. Raven does not infer earlier phase changes or
checkpoint times. If an event has a source path but no original line number,
Raven opens the exact file and reports that its exact line is unavailable.

<a id="feature-46"></a>

## Comparison scales

Enter `:diff JOBID JOBID` to compare two jobs. You can supply up to six IDs.
Resource curves align on each job's first observed sample.

Shared scales are on by default. Each metric uses the same vertical limits for
every compared job. Press `s` to toggle between shared and independent limits.
You can also use `:chart shared on` or `:chart shared off`. The comparison shows
the active setting. A fixed scale for a metric applies to all its curves.

Raven compares measurements of the same metric. It does not combine CPU,
memory, and GPU values into one scale. If a job has no measured samples, Raven
shows that condition instead of drawing a curve.

<a id="read-gpu-utilisation-and-busy-mean"></a>

## Read GPU utilization and busy mean

Job series can show a GPU utilization curve and an observed busy-mean curve for each available device.
Utilization is the recorded busy percentage.
The busy mean is the cumulative mean of valid utilization observations in the retained series.
It indicates activity. It does not establish FLOP efficiency, throughput, or the fraction of allocated devices used.
Missing and invalid device readings remain gaps and do not enter the mean.

Session telemetry identifies devices by `node:index` and retains utilization, used memory, and total memory.
An application's job trace uses `<WorkDir>/logs/gpu-util-<jobid>.csv` with rows
`timestamp,index,utilisation_percent,memory_used_MiB`.
Raven keeps that trace attached to the exact job.
An index-only trace cannot establish which node or allocated device produced a reading.
See [GPU trace](../reference.md#the-gpu-trace) for the producer procedure.

The view shows at most four session devices and four trace indices.
The corresponding stable graph IDs are `gpu:<node:index>:rate`,
`gpu:<node:index>:busy-mean`, `gpu-trace:<index>:rate`, and `gpu-trace:<index>:busy-mean`.
These graph identities remain stable as observations arrive or the glyph mode changes.
Unavailable telemetry leaves utilization and busy-mean measurements unknown.

<a id="feature-47"></a>

## Metric display

Use the exact metric ID with these commands:

```text
:metricdisplay loss label Validation loss
:metricdisplay loss precision 4
:metricdisplay samples_per_second unit samples/s
:metricdisplay loss reset
```

| Preference | Accepted value |
| --- | --- |
| `label` | Printable text of up to 96 characters. |
| `unit` | An explicitly declared unit of up to 96 characters. |
| `precision` | An integer from 0 to 12 decimal places. |
| `reset` | Remove the metric's display preferences. |

These preferences change presentation only. They do not convert units or rewrite
measurements. Declare a unit that matches the reported data. Raven does not infer
an application metric's unit from its name. Session CPU and GPU percentages use
percent units. Session memory samples use GiB because their source bytes are
divided by 1024 cubed; their existing metric ID remains `Memory (GB)` for saved
view compatibility. The chart shows the original metric ID and the declared
unit. Exact sample inspection always retains the original numeric precision.

Raven saves labels, units, precision, axis limits, event visibility, and the
comparison scale setting. It does not restore a live chart, selected job, or
temporary sample interval after a restart.

## Data limits

- Raven uses published metric snapshots and in-memory resource samples.
- A metric chart retains at most 10,000 points. A dashboard has at most 64 metrics.
- An event picker has at most 512 observed events.
- Plot dimensions are limited to 2,048 terminal columns and 128 rows.
- The interaction registry holds at most 96 plots in one published frame.
- Rectangular zoom retains at most 128 source keys and 16 undo steps per key.
- Live and sampling requests retain at most 128 exact metric identities. Controls act on visible running sources; accepted sampling requests can remain active offscreen.
- Unknown and nonfinite values remain gaps. A measured zero remains a value.
- Sampling outages remain visible after zooming or compression.
- No rendering path loads a metric file or queries the scheduler.

## Python methods

These methods are internal application interfaces. A project integration should
publish the reporting schema. It does not need to call these methods.

### `raven.chart_tools`

| Method | Purpose and result |
| --- | --- |
| `finite(value)` | Check that a value is a finite number. Reject Boolean values. |
| `text(value, limit=96)` | Check printable, nonempty preference text. |
| `restore(saved)` | Return bounded, validated display and axis preferences. |
| `valid_axis(axis)` | Check the scale mode and finite limits. |
| `display(state, name)` | Return the display label, declared unit, and precision. |
| `preference(state, name)` | Return the source unit and explicit metric preferences. |
| `format_value(value, preference)` | Format a value without changing its numeric data. |
| `axis_values(values, axis)` | Return plot coordinates, limits, and the nonpositive log sample count. |
| `percentile(sorted_values, fraction)` | Calculate an interpolated percentile from sorted known values. |
| `statistics_for(points, full_points=None)` | Calculate sample statistics and observed-interval coverage. Supply normalized points in timestamp order. |
| `interval(points, state, name)` | Select source timestamps between the stored interval endpoints. |

### `raven.charts`

All drawing methods return rows of `(text, style)` segments. They do not perform
file or process I/O. Use `Glyphs(True)` for ASCII output.

| Method | Purpose and result |
| --- | --- |
| `resample(values, width, how="mean")` | Compress samples to mean or maximum buckets. Keep unknown buckets. |
| `envelope_points(values, width)` | Retain first, minimum, maximum, and last samples in each equally spaced bucket. |
| `fmt_num(value, unit="")` | Format a compact numeric label. |
| `time_axis(t0, t1, width, ...)` | Draw timestamp or elapsed-time labels. |
| `time_selection_note(t0, t1, width, ...)` | Identify the exact selected start timestamp and the displayed span. |
| `vbar_chart(g, values, width, height, ...)` | Draw a filled area graph with scale labels and optional timestamps. |
| `hbar_rows(g, items, width, ...)` | Draw horizontal bars with measured values. |
| `braille_chart(g, values, width, height, ..., curve_style="fine", trend=True, bands=True)` | Draw continuous Braille or directional ASCII curves. Add eligible labeled fit and range layers when enabled. |
| `heatmap(g, matrix, width, ...)` | Draw a shared-scale measured matrix. Mark unknown cells. |
| `stacked_bar(g, items, width, ...)` | Draw a composition bar with a numeric legend. |
| `histogram(g, values, bins, width, ...)` | Count values in specified bins and draw horizontal bars. |
| `gantt(g, jobs, t0, t1, width, ...)` | Draw observed queue and run intervals. |
| `summary_line(g, parts, indent=" ")` | Join styled summary values into a row. |

`vbar_chart` and `braille_chart` accept `envelope=True` to preserve extremes.
Use `envelope=False` only when a mean representation is intended. Their optional
`axis_formatter` formats plotted coordinates; their `sample_times` and
`sample_interval` preserve actual time spacing and known sampling outages.
The internal `curve_style="blocks"` argument retains the older two-by-two raster for callers that require it.
It is not a user-facing chart setting.

### `raven.chart_interaction`

The interaction layer reads published plot geometry. It performs no file or scheduler I/O.
Renderers begin one outer frame, stage local plots, map them through the final layout,
then publish the visible geometry. A `Rect` uses exclusive `(top, left, bottom, right)` edges.
Zoom bounds use plotted coordinates; logarithmic vertical bounds remain base-10 logarithms.

| Method | Purpose and result |
| --- | --- |
| `initialize(app)` | Create bounded transient pointer, capture, and zoom state. |
| `key(app, metric, source, jid=None, ...)` | Identify the exact source, job, and run attempt without truncating paths. |
| `begin_frame(app, width=None, height=None)` | Start the outer frame and clear pending geometry. |
| `mark(app)` | Record the first pending plot for a nested renderer. |
| `record(app, identity, metadata, ...)` | Stage the plot rectangle and axis bounds in local cells. |
| `place_since(app, first, dy=0, dx=0, clip=None)` | Translate and clip the nested plots after layout. |
| `take_since(app, first)` | Detach a layout candidate so discarded probes publish no plots. |
| `put_records(app, records)` | Restore the selected layout candidate's geometry. |
| `map_records(records, mapping, ...)` | Map plots through wrapping and sticky headers; reject disordered plot rows. |
| `publish(app, width=None, height=None)` | Freeze the final visible geometry and validate capture. |
| `hover(app, y, x)` | Update cosmetic pointer feedback from the published plots. |
| `active(app)` | Report whether a rectangular drag is active. |
| `capture_bounds(plot)` | Extend the axis rectangle by three cells, then clip it to the actual visible pane. |
| `tick(app, now=None)` | Cancel stale, blocked, or timed-out capture. |
| `cancel(app)` | Discard an unfinished preview without changing completed zoom. |
| `handle_mouse(app, y, x, button="left", shift=False)` | Apply plot press, motion, valid-release zoom, and right-click full-view reset. |
| `bounds(app, identity, scale=None)` | Return completed source-specific display bounds. |
| `captured_bounds(app, identity, scale=None)` | Return both painted axis mappings while the exact source has an active drag. |
| `autofit(app, identity, scale=None)` | Identify a time-only zoom that requires an observed-data vertical fit. |
| `undo(app, identity=None)` | Restore the previous rectangular view. |
| `reset(app, identity=None)` | Remove rectangular zoom for the selected source. |
| `handle_key(app, key)` | Apply cancellation, undo, and reset controls in plot context. |
| `command_names()` | Return the interaction command names. |
| `run_command(app, args)` | Apply `chartzoom undo` or `chartzoom reset`. |
| `feedback(app, ascii_=None, rows=None, overlays=())` | Return themed Braille selector strokes or ASCII fallback, preserving each painted cell's background. |
| `next_deadline(app, now=None)` | Schedule cached selector frames during the bounded 24 ms dot-phase transition. |
| `overlay(views, snap, app, width, height)` | Return no full modal overlay; feedback belongs to the current plot. |

### `raven.selector_glyphs`

These pure helpers place visual strokes on the two-by-four Braille lattice.
They do not change event coordinates, measured values, or source sampling.

| Method | Purpose and result |
| --- | --- |
| `locate(y, x)` | Map finite visual coordinates to a terminal cell and its Braille positions. |
| `glyph(y_slot, x_slot, horizontal=False, vertical=False, ascii_=False, fine=True)` | Return a thin stroke, combined intersection, or portable fallback glyph. |
| `interpolate(start, target, elapsed, duration=0.08)` | Ease the visual position without overshoot; use at most 200 ms for a custom duration. |

The selector passes a 0.024-second duration for its dot-phase transition inside the latest reported cell.
The helper's general default does not delay whole-cell pointer movement.

### `raven.metric_live`

This layer uses published exact-job status and chart geometry.
It changes display state and publishes bounded polling requests without performing source reads or scheduler I/O.
A filled companion uses the same canonical identity as its resource curve.

| Method | Purpose and result |
| --- | --- |
| `initialize(app)` | Create bounded, transient per-metric duration, polling, and control state. |
| `canonical(identity)` | Share a resource area's Live identity with its source curve. |
| `fraction(delta)`, `delta_at(value)` | Convert between durations and the logarithmic slider position. |
| `format_delta(value)` | Format the duration in seconds or milliseconds. |
| `rate_fraction(value)`, `rate_at(value)` | Convert between integer polling positions and the logarithmic interval scale. |
| `set_running(app, identity, running)` | Publish exact source eligibility and stop a finished source. |
| `window(app, identity, now=None, *, newest=..., previous=None, oldest=None, poll_interval=None)` | Return the buffered Live interval or the captured plot's fixed mapping. Supplying observations advances its display clock. |
| `display_end(app, identity, now=None, *, newest=None, previous=None, oldest=None, poll_interval=None)` | Advance an eligible source's display clock from supplied timestamps and return its end time. |
| `buffered(app, identity)` | Check that the exact running source has entered buffered playback. |
| `playback_status(app, identity)` | Return the last published source clock without advancing it. |
| `playback_label(app, identity, *, ascii_=False, displayed_end=None)` | Format playback status and actual lag; supply the presented endpoint when full-history drawing holds an older end time. |
| `display_resolution(app, identity, span, width, *, following=True)` | Set the movement deadline from the visible span and drawing width. Keep source publication independent. |
| `enabled(app, identity)` | Check that Live is enabled for the current running source. |
| `set_enabled(app, identity, value)` | Toggle Live and clear its rectangular zoom when enabling it. |
| `stop_for_zoom(app, identity)` | Stop Live after a valid rectangular zoom. |
| `set_delta(app, identity, value)` | Set a finite duration from 1 to 30 seconds. |
| `set_rate(app, identity, value)` | Set a polling position from one to 100 and publish the changed background sampling demand. |
| `controls(g, app, identity, width, ...)` | Render and stage the running metric's control row. |
| `publish(app, records)` | Freeze controls after final layout transformation. |
| `descriptors(app)` | Return visible toggle and both slider actions for the control graph. |
| `active(app)`, `cancel(app)`, `tick(app, now=None)` | Check capture, restore the captured slider's cancelled value, and reject stale capture. |
| `handle_mouse(app, y, x, button="left", shift=False)` | Handle the toggle, both slider gestures, and each slider's right-click reset. |
| `handle_key(app, key)` | Apply keyboard slider steps and cancellation. |
| `command_names()`, `run_command(app, args)` | Expose and apply temporary-token Live, duration, and sampling commands. |
| `document_revision(app)`, `document_interval(app)` | Report changed display state and the active display-refresh deadline. |
| `feedback(app, g)` | Return the small control-row overlay between document refreshes. |
| `overlay(views, snap, app, width, height)` | Return no full modal overlay. |

### `raven.metric_envelope`

This module derives display-only trend and range geometry from bounded drawing columns.
It does not alter or retain the original measurement history.
Local fits use normalized coordinates and values. Geometry-only projection weights have a 128-entry cache.

| Method or record | Purpose and result |
| --- | --- |
| `columns(points, width, details=None)` | Summarize drawing vertices as ordered observed ranges. Preserve continuity, original counts, turns, and equal-time jumps when supplied. |
| `local_trend(source, width)` | Fit eligible continuous runs with local quadratic polynomials. Return bounded drawing vertices and their continuity flags. |
| `band_columns(source, low, high, pixels_high)` | Return the columns that contain sufficiently dense observed oscillations for a range band. |
| `Column` | Hold one drawing column's observed bounds, first and last values, count, and continuity metadata. Its `centre` is the midpoint of its range. |
| `Polynomial.at(x)` | Evaluate one normalized local model within its stored value limits. Rendering clips and joins it only within the acquired domain. |

### `raven.metric_playback`

This module owns the transient display time for each exact source and job attempt.
It reads supplied timestamp metadata only. It does not read a source, scan its history, create samples, or schedule collection.
At most 128 source clocks, 128 presentation gates, and seven observed intervals per source clock are retained.
The observed cadence uses the median of those intervals.
A backward clock, older newest timestamp, changed source generation, or return after more than 60 seconds resets the clock.
If retained history starts after the old display time, the clock resumes at its earliest available observation and buffers again.

| Method or record | Purpose and result |
| --- | --- |
| `advance(app, identity, *, now, newest, previous=None, poll_interval=None, generation=None, oldest=None)` | Advance the buffered display time within acquired history. Return `Playback`, or `None` for invalid or future latest timestamps. |
| `reset(app, identity=None)` | Forget the matching source clock and presentation, or all clocks and presentations when no identity is supplied. |
| `present(app, identity, playback, *, now, geometry, x_interval, edge_signature, force_token=None, oldest=None, reveal_changed=False)` | Publish or retain an automatic full-history endpoint using bounded edge and view guards. Return its actual displayed lag. |
| `reset_presentation(app, identity=None)` | Discard one or all presentation gates without restarting the source clock. |
| `Playback(end, delay, lag, interval, status)` | Report display time, target delay, actual lag, estimated source interval, and playback status. |

The UI owns these clocks. Captured and explicitly selected bounds do not advance with them.
The delay target is bounded at 30 seconds; actual lag can be longer after an outage.
Catch-up is bounded at 1.25 times elapsed display-clock time.

### `raven.metric_sampling`

This layer maps exact metric identities to shared background collectors.
Its validation and interval calculations perform no scheduler or file I/O.

| Method | Purpose and result |
| --- | --- |
| `validate_rate(value)` | Validate an integer polling position from one to 100. Reject Boolean, nonfinite, and fractional values. |
| `interval(base, global_rate=1, metric_rate=1, ...)` | Calculate the fastest global or metric request while retaining reader bounds and configured slow bases. Actual collectors also apply managed policy. |
| `format_interval(value, ascii_=False)` | Format seconds, milliseconds, or microseconds; return `?` for an invalid interval. |
| `source(identity)` | Map a metric to its live resource, GPU, trace, or application-file collector. |
| `attempt(job)`, `matches(identity, job)` | Identify and validate the exact running scheduler attempt. |
| `research_matches(identity, context)` | Validate a reported metric's exact job, project, run, and published generation. |
| `sync(app)` | Publish a bounded replacement request snapshot to the background collectors when demands change. |
| `cadence(app, identity, rate=None)` | Return the effective shared collector interval, or an endpoint interval for a supplied polling position. |

`Sampler.set_metric_sampling(requests)` validates and replaces at most 128 exact metric requests.
`Sampler.sampling_interval(name, jid, attempt=None)` returns the safe interval for one job's shared source.
The sampler schedules those probes separately from jobs that have no faster request.

`ResearchHub.set_metric_sampling(requests)` replaces bounded application-file requests.
`ResearchHub.refresh_interval(context=None)` returns the interval for the current exact run context.
`ResearchHub.sampling_interval(identity)` reports that run's shared file-reader interval.
These methods change requests without reading files immediately.

### `raven.refresh_rate`

This layer stores the polling position. Managed sessions map it to queue
intervals through `telemetry_settings`; native compatibility sessions retain
global source scaling. The stored `polling_multiplier` field keeps its name
for compatibility.

| Method | Purpose and result |
| --- | --- |
| `poll_interval(value, maximum=50)` | Convert a whole-number position to a logarithmic interval from five seconds to 500 milliseconds. |
| `poll_position(seconds, maximum=50)` | Return the nearest bounded position for a finite positive interval. |
| `source_interval(base, value, *, source="")` | Calculate a compatibility source request. Native bases up to five seconds use the slider interval; slower bases and other sources use the global speed ratio within reader bounds. |
| `file_interval(base, value, *, remote=False)` | Adjust a file-reader base by the global speed ratio while retaining local or remote limits. |
| `cadence(app, source, base=None)` | Return the source's effective interval without reading the source. |
| `candidate_cadence(app, source, value)` | Return an endpoint or candidate interval using the same mode and policy calculation as the active reader. |

### `raven.analysis_ui`

| Method | Purpose and result |
| --- | --- |
| `initialize(app)` | Create bounded analysis state. |
| `rows_selected(app, kind)` | Report whether the current sample, Timeline, Chart Events, or Evidence cursor is explicitly selected. |
| `resume_rows(app, kind)` | Restore row selection after deliberate navigation. |
| `context_click(app, y, x, button="left")` | Preserve graph-reset priority; clear local analysis row selection and keep the exact dialog source. |
| `restore(app, ui)` | Restore preferences and reject malformed saved values. |
| `save(app)` | Return persistent preferences without a live modal or job selection. |
| `command_names()` | Return the analysis command names for the command palette. |
| `dashboard_names(app, series)` | Return visible metric IDs in dashboard order. |
| `observe_metrics(app, result, jid)` | Record an observed, timestamped phase change. |
| `memory_series(app, jid)` | Read bounded in-memory resource samples. No file is loaded. |
| `chart_data(app, snap)` | Select published application metrics or session resource samples. |
| `viewport(points, state, normalized=False)` | Select the current time window. Set `normalized=True` only for validated, timestamp-sorted points. |
| `chart_rows(g, app, points, width, height, ...)` | Draw the chart, exact sample, scale, selected interval, and source status. Pass `snapshot` to reuse the published frame. |
| `chart_events(app, snap, times=None)` | Return observed events for the pinned chart job, with original citations. |
| `event_markers(g, events, times, width)` | Draw timestamp-aligned event marks. |
| `open_inspector(app, jid=None)` | Open an active, recent, or historical job by its exact ID. |
| `timeline_events(app, snap)` | Collect bounded, deduplicated published observations. |
| `passport_rows(g, passport, differences=None, ...)` | Draw passport data and substantive differences. |
| `run_command(app, args)` | Validate and execute an analysis command. Return whether it was handled. |
| `handle_key(app, key)` | Apply context-specific chart or analysis controls. |
| `overlay(views, snap, app, width, height)` | Draw the active analysis overlay from published data. |
