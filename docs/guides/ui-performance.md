# Display and input performance

[README](../../README.md) · [Mouse and button navigation](pointer-navigation.md) · [Reference](../reference.md)

Tower 4.3.1 introduced the cached pointer display and bounded long inline views.
Tower 4.4.0 retains that path for draggable workspaces, shared launch groups, history browsers, and Quick Advisor.
Tower 4.5.0 extends it to current-viewport directional focus and metric crosshair feedback.
Tower 4.7.0 adds pane scrollbars, pinned rendered-line selections, and finer continuous Braille curves.
Tower 4.8.2 separates protocol-safe pointer input from keyboard shortcuts and repaints changed cells.
Tower 4.8.3 restores saved metrics in the background, releases the data lock before persistence,
and limits inline Job Series raster work to the visible metric bands.
Tower 4.9.0 adds one byte decoder, reusable native metric preparations, indexed
Live windows, shorter observer critical sections, and a bounded pointer check before paint.
Tower 4.11.0 buffers the display time of running metric graphs.
This reduces visible edge jumps when measurements arrive in batches; it does not increase source sampling or make a delayed measurement current.
See [Buffered running graphs](charts.md#read-buffered-running-graphs) for delay and outage behavior.
Theme changes repaint the document with the selected canvas and surfaces.
Live metric windows use display deadlines while source sampling retains its own limits.
Hover, smooth scrolling, drag selection, menus, and live job updates remain available.
The display changes do not increase Slurm sampling rates.

Tower 6.1 shares revisioned metric publications across source consumers, copies
only visible cached job rows, and defers hidden telemetry heatmaps. An additional
bounded cache reuses exact time buckets when chart presentation changes. The
Advisor working set can retain more small sources under the same byte budget.
Pane geometry and pointer capture ownership use shared contracts. Read the
[consolidation guide](consolidation.md) and the
[6.1 measurements](consolidation-validation.md) for the implemented scope,
regression checks, and remaining latency limits.

## Manual group identity checks in 4.12.0

Job marks retain their submission and cluster identity. This prevents a refreshed or reused job ID from moving a manual group to another execution.
Pointer movement uses published rows and does not read scheduler sources or rebuild group inference.
Unchanged frames reuse selection tokens. Frame preparation still checks the underlying identity fields, including conflicting records from different sources.
Cluster, Nodes, and Sources pages skip job-selection identity publication.

Local CPU measurements of the unchanged inference-plus-selection stage were 14.81 ms for 10,000 known jobs, compared with 12.07 ms in 4.11.0.
Stress cases with 10,000 missing-submission records or duplicate source records took 33.34 ms and 40.49 ms, respectively.
These measurements exclude graph rendering and terminal paint; they are not input latency promises.
With grouping off, the added selection-provenance stage took 3.41 ms for 10,000 records and 22.77 ms for 50,000 records after optimization.
This validation has a real cost on large lists. The cache does not skip in-place corrections or weaken attempt checks to remove that cost.
See [Manual groups](batch-launches.md) for identity rules, limits, and reset controls.

## Job drag and menu checks in 4.13.0

Job moves use the last painted row geometry and exact source and destination
identities. A pointer report does not query the Store or infer groups.
Each document preparation publishes only visible destination spans and reuses
unchanged destination evidence, including members hidden by collapsed groups.
Opening a menu and committing a move still validate the current identities.
Ungroup validates selected collapsed destinations in one batch.
Menu feedback reuses its action list. A changed hover updates the bounded menu
controls; an unchanged hover reuses that overlay.

The local comparison used 1,000 or 10,000 jobs with known submission times,
a 180-column by 52-row Jobs page, and six collapsed groups of 20 members each.
After three warm-up calls, 25 calls measured median process CPU time for each
publication stage. The 4.12.0 baseline was commit
`fd57016423f0d3ca7e45eb9d3c63a808be16630a`. Times below are milliseconds.

| Stage | 1,000 jobs | 10,000 jobs |
| --- | ---: | ---: |
| 4.12 selection publication | 0.20 | 3.45 |
| 4.13 selection and destination publication | 0.37 | 4.12 |
| 4.13 destination publication alone | 0.10 | 0.26 |
| Explicit menu opening, five-call median | 1.25 | 15.76 |

The input check used the actual `_FrameCache`, terminal input dispatcher, and
cached feedback. At each source size it dispatched 360 held drag reports and
360 menu hover reports. Hooks rejected any Store snapshot, document composition,
group inference, token rescan, or job-record lookup during these reports.
All checks passed, with no pointer-induced document invalidation.
The scheduler clock was fixed during each input check to exclude independently
due maintenance; process CPU timing remained active. Scheduled data updates
continue to require document work in normal operation.

For three measured 60-report bursts after warm-up, median input-plus-feedback
CPU time was 20.82/21.15 ms for dragging and 51.53/50.40 ms for menu hover,
at 1,000/10,000 jobs respectively. These checks omit terminal transport,
terminal painting, fresh data, and large destination menus. The shared test
host adds measurement variation. They establish neither zero overhead nor
a maximum interactive delay. Run the focused input regressions with:

```bash
python3 -m pytest -q tests/test_job_group_drag.py \
  tests/test_job_group_menu.py tests/test_job_group_mouse_integration.py
```

The measured 4.13 runtime digest was
`3a7bdadcdf7d0320a648fbc0dfd9c74e28534b52c4d29147cf2deb56038709d5`.
It is the SHA-256 of the concatenated `tower/*.py` contents in filename order.

## Check pointer feedback

1. Open Jobs.
2. Move the pointer across job rows and Details buttons.
3. Check that the highlight follows the pointer.
4. Click a job and open Analytics → Advisor in Details.
5. Scroll down, reverse direction, and scroll to the first row.
6. Enter a command immediately after scrolling.

**Expected result:** Pointer feedback follows the latest reported position.
Scrolling responds to the new direction without replaying old movements.
The next command remains available after a scroll burst.
Hover does not select a job or execute a button.

Use `:terminaltest` when the terminal does not report movement, wheel, drag, or release events.
Press Esc to leave the test.
See [Mouse and button navigation](pointer-navigation.md) for the complete controls.

## Keep the display responsive

Tower retains the last published page while pointer feedback changes.
It updates the affected terminal cells instead of rebuilding the whole page for each movement report.
The page still refreshes for changed data, deliberate input, resize, and active animations.
Continuous pointer movement does not postpone background-result publication.

Metric crosshairs read the frozen, final plot geometry.
Moving a pointer does not recompute a curve or read its source.
Crosshairs reach the latest reported terminal cell immediately.
Only the Braille dot phase uses a bounded 24 ms transition inside that cell.
Curve, range-border, fitted-line, and annotation glyphs retain their original styles at selector intersections.
The faint decorative interior of a range band allows selector strokes, so the crosshair remains visible inside the band.
Mouse coordinates remain whole terminal cells despite the finer Braille stroke positions.
ASCII and reader modes keep static feedback. Disabling `animations` also disables selector easing.
During a graph drag, both painted axis mappings stay fixed while new sampler results remain available.
Automatic sample updates do not cancel capture.
A valid rectangular release changes display bounds and requests a fresh document.
Each zoom belongs to the exact metric, source, job, and attempt.
Running graphs advance their buffered edge at a bounded display rate.
Live moves a rolling window with that edge; the ordinary view retains the older history.
Its window spans 30 seconds to one second; the separate polling control spans five seconds to 500 milliseconds.
Changing the window alone does not change scheduler polling.
A moving metric slider updates its control immediately while graph previews are limited to 20 rebuilds per second.
Release applies the final slider position immediately.
Changing Delta with Live off updates only its saved session preference and control feedback; it does not rerasterize the retained graph.

Automatic full-history plots retain a stable displayed endpoint between meaningful edge changes.
The presentation advances when the edge crosses a drawing column or vertical pixel, or its acquired bracket or continuity changes.
Changed scale, geometry, active graph controls, replay position, or job attempt force an updated view.
This gate also applies to the full-screen following chart; Live, capture, and explicit zoom keep their own bounds.
It reduces repeated maintenance raster work without changing source timestamps or samples.
The displayed lag includes the retained endpoint's extra age.
Interior rounding or a fitted neighborhood can change before an edge threshold is met, so this is an adaptive presentation policy, not a proof of identical skipped pixels.
Source corrections still invalidate the exact prepared data and raster inputs.

The control graph indexes visible rows and control identities.
Advisor results and wrapped Details documents reuse their current published inputs.
These results update when their measurements, settings, selected job, or available width change.
The inline Advisor indexes the complete retained report and renders only visible cards.
Running-job metric series are read only for cards in that window.
Long notes and suggested SBATCH flags remain available at narrow widths.
Metric charts retain bounded prepared data for the current view.
Caches have fixed bounds and do not retain unlimited jobs, views, or terminal sizes.
Native curve rasters compare the complete values, timestamps, scale, style, and sampling settings.
Interior sample corrections invalidate the raster without relying on a changed point count.
Source age, axes, live controls, and exact job identity still publish on each document update.
The raster cache retains at most 64 entries, 512,000 combined values and timestamps, and 65,536 display cells.
These bounds accommodate native and trace curves for four GPUs in the tested display sizes.
Native Jobs rows reuse one bounded prepared table while grouping, selection, and progress animation remain current.
Custom plugin and action callbacks retain their original calculation path.

Clicks, keyboard commands, and mouse releases retain their input order.
Drag selection keeps exact job IDs.
Cancellation still requires its normal review.
Log selections and copies retain their exact source positions and bytes.
Pane scrollbars use the same bounded viewport controller and preserve source selection.
Count growth keeps an active scrollbar gesture attached to its pane.
Source, geometry, layout, or modal changes cancel capture and consume its delayed release.
Rendered text selections cache at most 20,000 lines and 8 MiB.
Incomplete cache coverage refuses copying instead of publishing a partial range.
See [Pane navigation](pane-navigation.md) for controls and editor-delivery bounds.

Use the top-right update slider or `:rate N` to change source sampling.
Use `:smoothscroll off` to request immediate wheel movement.
These settings control different operations.
Disabling smoothing does not make Slurm commands complete sooner.

## Use a limited-throughput connection

1. Use `:smoothscroll off` if intermediate scrolling frames exceed the connection's display throughput.
2. Use `:theme reader` for plain ASCII text and static notices.
3. Keep the source rate appropriate for the cluster and connection.
4. Check Sources for slow commands, unavailable data, or retry backoff.

**Expected result:** Tower uses immediate viewport movement and a simpler display.
The requested source rate remains subject to source minimum intervals and backoff.

Terminal rendering, SSH transport, tmux, and Slurm command duration can each add delay.
The display optimizations reduce Tower's repeated work.
They do not impose a universal response-time guarantee.

## Tower 4.3.1 release measurements

The release comparison used Tower 4.3.0 at `98b2d86` and the final 4.3.1 source.
Both revisions used Python 3.12 on the same Linux cloud runner.
Each pair ran sequentially with the same fixtures and options.

The public benchmark measured UI computation with a counting paint sink.
It used a 160-column by 48-row Unicode display and eight measured frames per case.
The following values are median frame times.
They exclude terminal transport and scheduled waiting.

| Workload | 4.3.0 | 4.3.1 |
| --- | ---: | ---: |
| Hover with 1,000 jobs | 54.29 ms | 0.13 ms |
| Hover with Advisor and 10,000 accounting records | 1,224.11 ms | 0.17 ms |
| Advisor wheel frame with 10,000 accounting records | 1,904.62 ms | 37.21 ms |
| Research refresh with 64 metrics and 10,000 points per metric | 152.34 ms | 28.10 ms |

A separate local pseudo-terminal test measured input processing through `curses.doupdate()`.
It used `screen-256color`, a 120-column by 36-row Unicode display, 500 jobs, and 2,000 accounting records across 128 names.
Eight jobs were running.
The linked project contained 64 metrics and 256 records.
A compact observation record followed each display update in both revisions.

| Input workload | 4.3.0 | 4.3.1 |
| --- | ---: | ---: |
| Final hover after 1,000 movement reports | 1,928.77 ms | 51.94 ms |
| Command after 1,000 movement reports | 7,226.90 ms | 635.95 ms |
| Accept the target from 700 wheel reports in Advisor | 2,958.29 ms | 563.95 ms |
| Reverse through 820 wheel reports, then complete a focus command | 9,142.47 ms | 793.37 ms |

The hover row is the median of three bursts.
The other terminal rows describe one observed burst each.
The forward-wheel row measures target acceptance; optional smooth motion settles afterward.
The keyboard rows require the command's visible state change after the queued reports.
These local pseudo-terminal times exclude SSH transport and terminal-emulator presentation.

Additional terminal checks used 80, 120, and 180 columns in Unicode and ASCII.
They checked drag release, resize, reversed scrolling, and keyboard input.
Actual fixture job completion and new metric publication remained visible during continuous pointer movement.
No scheduler-changing command ran in the tests.

## Tower 4.4.0 workspace measurements

The workspace comparison used Tower 4.3.1 at `f873717` and the final 4.4.0 source.
Each pair ran sequentially on the same runner with Python 3.12.
The public benchmark used a 120-column by 36-row Unicode display, 500 jobs, 2,000 accounting records, and 20 measured frames.
The Research fixture contained 64 metrics and 256 records.

| UI workload, median | 4.3.1 | 4.4.0 |
| --- | ---: | ---: |
| Cached Advisor hover | 0.09 ms | 0.14 ms |
| Advisor wheel frame | 20.32 ms | 24.55 ms |
| Research document refresh | 4.09 ms | 11.65 ms |

The new history browser adds work to a full document refresh.
Cached hover still paints two rows without page composition, job-row formatting, Advisor aggregation, chart rasterization, or control-graph publication.
All three measured workloads attempted no file or scheduler I/O on the display path.
ASCII checks retained the same work bounds.

A paired local pseudo-terminal run used the same data and `screen-256color`.
It measured the visible state after actual input and `curses.doupdate()`.

| Input workload | 4.3.1 | 4.4.0 |
| --- | ---: | ---: |
| Final hover after 1,000 movement reports, median of three bursts | 52.01 ms | 42.25 ms |
| Command after 1,000 movement reports | 417.61 ms | 587.65 ms |
| Target acceptance after 700 Advisor wheel reports | 552.48 ms | 462.46 ms |
| Reverse through 820 wheel reports, then complete a focus command | 1,054.80 ms | 1,107.71 ms |

These are local observations, not universal response-time guarantees.
The terminal measurements include queued input and exclude SSH transport and terminal-emulator presentation.
Both runs used the same compact observer after each terminal update.

Adaptive controls also passed terminal checks at 40, 80, 120, and 180 columns in Unicode and ASCII.
Those checks covered buffered divider capture, drag cancellation, resize, older Recents, shared folds, exact historical job selection, four history docking orientations, asynchronous Quick Advisor, and six-cell progress sorting.
The scheduler fixtures allowed only read commands.

## Tower 4.5.0 metric and input measurements

The metric comparison used Tower 4.4.0 at `de94c7b` and the final 4.5.0 display and input paths.
Both revisions used Python 3.12 on the same runner and ran sequentially.
The counting-paint benchmark used 120 columns, 36 rows, 500 jobs, 2,000 accounting records, and 20 measured frames.
The Research fixture contained 64 metrics and 256 records.

| UI workload, median | 4.4.0 | 4.5.0 |
| --- | ---: | ---: |
| Cached Advisor hover | 0.14 ms | 0.23 ms |
| Advisor wheel frame | 25.73 ms | 33.35 ms |
| Research document refresh | 12.20 ms | 16.78 ms |

Cached Advisor hover painted two rows without page composition, job-row formatting, aggregation, chart rasterization, control-graph publication, or source I/O.
A separate 100-frame metric-crosshair check measured a 0.77 ms median and a 1.88 ms 95th percentile.
It used the published plot geometry and performed no page composition, chart rasterization, control-graph publication, or source I/O.
Full document refreshes do more work to publish current metric controls and exact plot geometry.

The paired local terminal check used `screen-256color`, 120 columns, 36 rows, the same job counts, eight running jobs, and 128 job names.
Both revisions used the same compact observer after actual `curses.doupdate()`.
The wheel settings included smooth scrolling and the same Details Advisor document.

| Input workload | 4.4.0 | 4.5.0 | Terminal updates, 4.4.0 / 4.5.0 |
| --- | ---: | ---: | ---: |
| Final hover after 1,000 movement reports, median of three bursts | 43.54 ms | 81.67 ms | 5 / 6 |
| Command after 1,000 movement reports | 521.78 ms | 530.47 ms | 18 / 22 |
| Target acceptance after 700 Advisor wheel reports | 1,284.03 ms | 486.20 ms | 18 / 9 |
| Reverse through 820 wheel reports, then complete a focus command | 1,202.54 ms | 814.02 ms | 25 / 22 |

The direct Details wheel route removes repeated input dispatch before the next viewport publication.
The forward-wheel measurement stops when Tower accepts the target; smooth motion settles afterward.
The reverse-wheel measurement requires the visible focus change after the queued command.
The hover result is a three-burst median; each other row describes one observed burst.
These observations include queued input and exclude SSH transport and terminal-emulator presentation.
They show bounded cached feedback and fewer wheel updates in this fixture, not a universal improvement for every workload.

## Tower 4.8.2 pointer measurements

The comparison used Tower 4.8.1 at `f5c2a3e` and the final 4.8.2 source on the same Python 3.12 Linux runner.
Each revision processed 300 movements across the selected job row and its Details graphs.
The fixture contained 1,000 jobs, 3,000 history records, and 4,000 native samples in a 200-column by 60-row display.
Both revisions used the same benchmark and a counting paint sink.

| Workload or operation | 4.8.1 | 4.8.2 |
| --- | ---: | ---: |
| Unicode hover frame, median | 2.42 ms | 1.30 ms |
| ASCII hover frame, median | 2.32 ms | 1.28 ms |
| Unicode paint calls per frame, median | 1,363 | 12.5 |
| Unicode painted cells per frame, median | 2,713 | 98 |
| ASCII painted cells per frame, median | 2,714 | 141 |
| Prepared job rows on unchanged maintenance | 1,000 | 0 |
| Native rasters on unchanged maintenance | 2 | 0 |

The Unicode 95th percentile changed from 3.84 ms to 4.57 ms in this pair.
The ASCII 95th percentile changed from 4.71 ms to 2.25 ms.
The median and write counts improved; this run does not establish an improvement for every frame.
No scheduler, file, or worker I/O occurred during the measured frames.
The times exclude terminal transport and terminal-emulator presentation.

A separate four-GPU fixture tested native and trace sources together.
It contained 36 curve and area rasters in Jobs Details.
Changing only the old cache bounds to the release bounds reduced unchanged raster calls from 36 to zero.
Observed maintenance composition decreased from 138.73 ms to 46.70 ms in that fixture.
The source values, chart cards, live controls, and graph geometry remained available.

## Validate a source change

Use the reproducible benchmark to measure UI work on your computer:

```bash
python3 scripts/benchmark_ui.py \
  --scenario jobs --gesture chart-hover \
  --jobs 1000 --history 3000 --points 4000 --width 200 --height 60 \
  --glyphs both --repeats 300 --output jobs-graph-hover.json

python3 scripts/benchmark_ui.py \
  --scenario advisor --gesture hover \
  --jobs 500 --history 2000 --width 120 --height 36 \
  --glyphs both --repeats 20 --output advisor-hover.json

python3 scripts/benchmark_ui.py \
  --scenario advisor --gesture wheel \
  --jobs 500 --history 2000 --width 120 --height 36 \
  --glyphs both --repeats 20 --output advisor-wheel.json
```

The benchmark uses synthetic data in memory.
It does not connect to Slurm or read personal configuration.
Its times exclude terminal and network transport.
The report includes median and 95th-percentile frame times, work counts, and attempted I/O.
Use identical options on the same computer when comparing revisions.
Use `--scenario research --metrics 64 --points 10000` to exercise metric charts.
Use `--scenario analytics --gesture chart-hover` to exercise native Analytics curves.
Jobs `chart-hover` alternates between the selected job row and its published Details graphs.
The benchmark rejects a changed page or selected job during that crossing.
Use `--gesture render` to measure a full document refresh.
Use `--source-root PATH --label REVISION` to benchmark a separate source checkout.

Run the focused regression checks from the repository:

```bash
python3 scripts/setup.py --mode demo --dev
.venv/bin/python -m pytest -q \
  tests/test_hover_render_budget.py \
  tests/test_responsive_input.py \
  tests/test_ui_state_budget.py \
  tests/test_workspace_render_budget.py \
  tests/test_advisor_render_budget.py \
  tests/test_advisor_document.py \
  tests/test_chart_render_budget.py \
  tests/test_fine_curves.py \
  tests/test_selector_responsiveness.py \
  tests/test_scrollbar_core.py \
  tests/test_scrollbar_tables.py \
  tests/test_scrollbar_documents.py \
  tests/test_modal_scrollbars.py \
  tests/test_text_selection.py \
  tests/test_editor_yank.py \
  tests/test_chart_interaction.py \
  tests/test_readable_curve.py \
  tests/test_metric_live.py \
  tests/test_reported_metric_live.py \
  tests/test_theme_coherence.py \
  tests/test_table_filter_budget.py \
  tests/test_pane_dividers.py \
  tests/test_adaptive_recents.py \
  tests/test_job_launch_groups.py \
  tests/test_history_browser.py \
  tests/test_quick_advisor.py \
  tests/test_job_progress.py
```

The checks cover bounded work, cache invalidation, input ordering, and continued publication.
Use [Contribution guidance](../../CONTRIBUTING.md) for the complete test suite.
Validate mouse behavior in an actual interactive terminal after changing input or painting code.
Include narrow and wide terminals, Unicode and ASCII, and tmux-compatible terminal types.

## Record a desktop latency report

Use this procedure when a normal terminal and an SSH client both show pauses.
The recorder measures Tower's foreground phases. It does not increase source polling.

1. Start `tower --ui-trace ui-trace.json`. Use a new file name for each run.
2. Open the affected job and graph view.
3. Move and drag the pointer across the job list and plots for 20–30 seconds.
4. Include a few source updates and a scroll through the Details graphs.
5. Quit Tower normally. The report is written after the interactive session exits.
6. Inspect `phases`, `slowest`, and `transitions` in the JSON file.

The file is created with owner-only permissions. An existing file is not overwritten.
The report contains timing statistics, terminal type, display size, event categories,
known shortcut action names, and page/mode transitions. It excludes typed text, raw input bytes,
job IDs, source paths, and log contents. Recording stays in bounded memory during the session;
there are no report-file writes in the input or paint loop. A forced process kill can leave an empty report.
Profile changes remain in the same report.

| Phase | What a long duration identifies |
| --- | --- |
| `input_dispatch` | Event handling or the action triggered by an event |
| `maintenance` | Foreground state maintenance and background-result publication |
| `snapshot` | Shared state access and snapshot preparation |
| `compose` | Page and graph construction |
| `feedback` | Pointer, selection, and control feedback |
| `paint` | Changed-cell preparation and curses drawing calls |
| `terminal_flush` | Terminal output through `curses.doupdate()` |
| `input_wait` | Waiting for input, including the normal idle timeout |

Each phase reports mean, maximum, and p50/p95/p99 wall duration, plus total UI-thread CPU time.
A high wall duration with little UI CPU time indicates waiting or scheduling; it does not prove
which external component caused the wait. CPU time excludes worker threads.
Parent phases include child phases. Do not add their totals.
`input_wait` is excluded from the slow-processing list because idle waits are expected.
Event context records the most recent input and its age; it does not establish causation.
Percentiles retain the latest 2,048 samples per phase. Totals and maxima cover the full run.
The recorder retains at most 32 phases, 64 slow events, and 128 page transitions.

## Tower 4.8.3 spike audit

The earlier warm-cache hover benchmark did not exercise all periodic update work.
The follow-up audit also tests cold saved histories, delayed file I/O, fresh samples,
Live windows, and mouse reports split immediately after their initial Escape byte.
A real curses pseudo-terminal reproduced delayed report bytes becoming keyboard events in 4.8.2.
The `[` byte selected the previous tab; an `r` coordinate could also request a refresh.

A controlled 250 ms file delay made CPU and GPU sampler persistence block foreground job checks
for about 253 ms and 251 ms. First Analytics composition took 289–308 ms while restoring saved data.
With the same injected delay after the fix, foreground checks took 0.002–0.003 ms, and first interactive
Analytics composition returned in 3.4–7.9 ms with a loading notice. The worker completed the read later.
These are injected-delay reproductions on a Linux cloud runner, not measurements of Fedora hardware.
The corrected paths keep file operations outside the data lock and restore interactive series
through one bounded worker on the existing sampler executor.
Headless exports retain synchronous access to saved history.

Interactive archive reads retain the newest configured samples. Each restore has a 64 MiB byte budget
and an 8 MiB line limit. The view identifies loading, errors, and limited history.
A limited restore does not invent missing measurements. Use a headless export when complete retained
archive coverage is required. Saved-series discovery is cached and refreshed in the background.

Pointer feedback checks visible Live controls, while scheduled maintenance checks every retained request.
A 128-request fixture with two visible controls reduced attempt checks across 20 feedback frames
from 2,560 to 40. Job completion and requeue validation remain active.

Use the [changing-data benchmark](../../scripts/benchmark_changing_ui.py) to reproduce this class of workload:

```bash
python3 -S scripts/benchmark_changing_ui.py --output /tmp/tower-changing-ui.json
```

The default fixture has 1,000 queue records, 3,000 accounting records, and 4,000 observations per source.
It includes four native GPUs and four static GPU traces. Native samples publish every 500 ms;
Live display windows advance at 10 Hz while pointer positions arrive at 50 Hz.
Cases cover ordinary refresh, 30-second and one-second Live windows, job switches, and resize.
Add `--cases scroll-end` to exercise the Details scrollbar endpoints.
Use `--glyphs both` to compare Unicode and ASCII. Use `--source-root PATH` to measure an isolated checkout.
`-S` prevents an installed editable-package finder from selecting a different source tree.

The report includes frame and phase p50/p95/p99/max durations, raster counts, and unexpected page changes.
Frames use a counting paint sink with published memory fixtures; the benchmark forbids scheduler and file reads.
It excludes terminal output, network latency, and external data producers. Use the real `--ui-trace` procedure
for those conditions. Compare identical options on the same machine, without competing benchmark processes.


The final quiet comparison ran 4.8.2 and 4.8.3 sequentially on the same Python 3.12 Linux runner,
using the default fixture above at 320 columns by 52 rows and 200 frames per case.
These observed frame durations include UI computation and a counting paint sink; they exclude terminal transport.

| Changing-data workload | 4.8.2 frame p95 | 4.8.3 frame p95 |
| --- | ---: | ---: |
| Native updates, Live off | 148.0 ms | 80.6 ms |
| Live, 30-second window | 432.9 ms | 103.9 ms |
| Live, one-second window | 397.5 ms | 106.4 ms |
| Selected job changes | 373.0 ms | 133.1 ms |
| Viewport resize | 360.5 ms | 126.9 ms |

For the 30-second Live case, frame p99 changed from 621.6 to 127.8 ms, and the observed maximum
changed from 876.8 to 227.8 ms. Each version performed 40 Live redraws. Raster calls fell from
1,440 to 320: 36 to eight per redraw, while six plots remained visible in the measured viewport.
No measured frame attempted source I/O. Timing varies with host scheduling, retained observations,
visible controls, and terminal size. These results do not establish a universal frame-time limit.
A separate 120-frame End/Home case retained visible graph controls in every frame, including the lower GPU trace cards.

## Tower 4.9.0 latency audit

The audit reproduced a mouse report that opened Logs. Ncurses consumed a legacy
mouse header and part of a UTF-8 coordinate. The remaining coordinate byte was `l`.
Tower now owns the complete input byte stream and uses terminal keyboard capabilities
without delegating mouse framing to ncurses. See [the input audit](input-audit.md)
for delayed reports, Unicode, paste, resize, and legacy-format limits.

The same report also failed in the tested pre-4.8.0 revision. The comparison did
not establish that 4.8.0 introduced this fault. Separate timing tests identified
work that caused repeated pauses in the current display:

| Cause | Change |
| --- | --- |
| Live redraws repeated native and trace preparation | Retain bounded preparations, with exact content checks that detect corrections to old samples. |
| Moving or selected time windows scanned the whole history | Use timestamp indexes and retain the edge observations needed to draw continuous lines and gaps. |
| Layout, dividers, and text selection repeatedly walked chart characters | Combine adjacent graph segments with the same style. Reuse measured widths, bounded style results, and complete fitted text segments. |
| Queue observers converted large job tables under the UI data lock | Capture fields briefly; convert and isolate observer inputs after releasing that lock. |
| A completed rebuild painted an old pointer position | Consume a bounded batch of passive reports before painting; preserve deliberate event order and active captures. |

The observer fixture with 10,000 jobs reduced median Store lock time from 148.46 ms
to 7.26 ms. Total observer processing changed from 545.35 ms to 132.09 ms.
These are five-run medians with the default forecast observer on the test runner.
The observer still receives each sampled publication. Calibration and replay rules stay active.

### Measure sustained terminal input

Use the pseudo-terminal benchmark to exercise the actual curses loop with fresh
CPU/GPU data, Live graphs, and continuous pointer reports:

```bash
python3 -S scripts/benchmark_sustained_pointer.py \
  --source-root . --output /tmp/tower-pointer.json \
  --live --delta 5 --points 4000 --jobs 3 \
  --rates 50,250,1000 --seconds 4
```

The report records source hashes, UI wall and CPU duration, queued-report age,
displayed-pointer age, and intervals between paints. It checks the final page and
visible graph controls. The fixture publishes new measurements every 500 ms and
does not query a real scheduler. Use the same window, source counts, and geometry
for both revisions. A five-second window is supported by the tested older revision.

Read these measurements together. Motion coalescing can keep the newest dispatched
endpoint fresh while older reports wait in a queue. Displayed-pointer age alone
does not measure pauses between paints. Idle intervals also reflect the input rate.
This local pseudo-terminal test excludes a physical display, Termius rendering,
SSH transport, and the latency of a live Slurm or GPU command.

### Sustained-input benchmark correction

The 4.11 audit found that the earlier sustained-input fixture seeded its requested
4,000-point history into a different job from the row selected in Jobs.
The displayed graphs therefore did not carry the stated history load.
The previously published comparison table for pre-4.8, 4.8.3, and 4.9 has been removed.
Its timings do not establish the claimed loaded-graph performance comparison.

The same fixture also supplied the earlier worker-switch timing runs.
Those timings are withdrawn as evidence of responsiveness with the stated graph load.
Successful mode transitions and input routing checks remain observations of the
workload that actually rendered, but they do not establish a large-history latency bound.

The corrected fixture must verify that the selected job owns the seeded measurements
and that the displayed graph identities and sample counts match that job.
Use a report from the corrected fixture for new timing comparisons.
The independent changing-data, source-only rendering, and observer-lock benchmarks
in this guide used different fixtures and are unaffected by this correction.

### Corrected 4.11 sustained-input measurements

The corrected comparison used a 320-column by 52-row pseudo-terminal, three jobs,
and the actual selected job's history: at least 4,000 CPU records, 4,000 GPU records
with four devices, and four 4,000-point GPU traces. Six plots belonged to that job.
New CPU/GPU publications arrived every 500 ms. Each case sent 1,000 passive SGR
motion reports per second for four seconds, with Multi selected.
The fixture verified the selected job, populated records, and visible plots.
All six cases retained Jobs and decoded and dispatched the quit key once.

The baseline was 4.10.0, commit `03da70b1ec3af4a047f769a1693dbd59c4db85ff`.
The 4.11 runtime source digest was `90d3d47fa6f89a9046c786d39b2999626cc8a3c20392e895fb05790ddee8d266`;
the baseline digest was `d9311a063c7268a6b1821948a9047a11cd90b00b0667150dfcb544a7863b5979`.
These are the benchmark's SHA-256 hashes of sorted runtime filenames and contents.

All times in this table are milliseconds. Document totals cover the four-second
measurement timeline; they exclude startup and other UI phases.

| Display | Build | Documents | Total document CPU | Document CPU p50 / p95 | Pointer age p99 | Largest paint gap |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Full history | 4.10 | 14 | 1,084.47 | 103.37 / 134.19 | 2.94 | 140.69 |
| Full history | 4.11 | 14 | 1,099.08 | 93.10 / 160.92 | 3.61 | 175.15 |
| Live, 1 second | 4.10 | 25 | 1,369.77 | 29.74 / 116.35 | 5.80 | 432.68 |
| Live, 1 second | 4.11 | 29 | 1,009.91 | 20.57 / 80.07 | 3.22 | 93.76 |
| Live, 30 seconds | 4.10 | 30 | 961.59 | 20.74 / 77.61 | 4.75 | 83.95 |
| Live, 30 seconds | 4.11 | 16 | 755.52 | 43.09 / 94.47 | 2.90 | 101.29 |

The one-second and 30-second cases reduced total measured document CPU by 26.3%
and 21.4%, respectively. The 30-second case prepared fewer documents, but each
was more expensive at the median and p95. Full-history total document CPU rose
by 1.3%, with a larger observed p95 and maximum paint gap. These results do not
establish that every mode is faster or that large-history rendering is seamless.
They are short runs on a shared cloud host, not terminal-independent latency limits.

A separate unchanged-source check used the same populated history, two initial
compositions, and 20 maintenance rebuilds spaced 201 ms apart. Before the
presentation gate, the 4.11 draft rasterized 80 curves with an 87.41 ms median.
The final gate reduced that to 36 curves and a 28.81 ms median; its maximum was
138.22 ms. The 4.10 baseline rasterized four curves with a 33.61 ms median and
115.28 ms maximum. The buffered display still advances through acquired data
when its edge criteria require it; the older baseline had no such playback.
The gate reduces repeated work while preserving the exact published domain,
not every hypothetical intermediate raster. Fresh preparations remain a cost.

### Remaining large-history costs

The independent 4.9 source-only fixture used four native GPUs, four traces, 4,000
observations per source, and a wider set of visible charts. Its final Live
frame p95 values ranged from 49.57 to 62.16 ms. Fresh publications can still
cost more than ordinary pointer feedback: observed full-history frame maxima
were 153.97 and 334.79 ms in two runs. This fixture calls rendering directly;
it omits terminal transport and the interactive loop's input admission and
cadence. It recorded no source I/O or unexpected navigation.

These runs had no queued background work. Their timing differences do not
establish a Single-versus-Multi speed advantage. Exact snapshot ownership
preserves historical corrections, but large fresh preparations remain a cost.
The measurements do not identify garbage collection as the cause of the
largest outlier. Use `--ui-trace` with the affected data to distinguish fresh
document work from terminal or input delay.

### Live worker-switch checks

Real terminal press and release reports exercise the toolbar during continuous
motion input. Check that both Multi → Single → Multi and Single → Multi → Single
complete, retain the selected job and page, and exit normally.
Also verify the selected job's populated graph workload before interpreting timings;
the [fixture correction](#sustained-input-benchmark-correction) applies to earlier results.

Use the [worker benchmark procedure](background-workers.md#diagnose-a-pending-change)
to repeat either control path. These tests improve coverage; they do not prove
that every terminal, history size, or input burst has a fixed maximum delay.
