# Slurm Tower: complete reference

[Quick start](../README.md) · [Runbook](runbook.md) · [Controls](CONTROLS.md) ·
[Tower 4.0 improvements](QUALITY_OF_LIFE.md) · [Terminal workbench](WORKBENCH.md)

This reference describes the terminal dashboard and its underlying metrics.
Unicode block visuals are the normal terminal presentation, with portable ASCII
and accessible themes available.
ASCII reports present the complete snapshot as portable plain text.
Run Tower directly on your CARC login node; the runbook provides a low-impact profile.

Tower 4.0 adds table inspection, complete-file log search, navigation tools,
graph controls, completion review, and paged artifact previews.
The [50-improvement index](QUALITY_OF_LIFE.md) links each feature to its operating guide.
The [Controls guide](CONTROLS.md) lists launch modes, key bindings, and command families.
Use those guides with the metric and architecture definitions in this reference.

Tower 4.14 adds a [sampling inspector, optional GPU adapters, shell checks, and
scientific array manifests](guides/phase-one.md). The [roadmap](ROADMAP.md)
tracks proposals that remain unimplemented.

```bash
tower                        # interactive (curses); q quits, ? lists the keys
tower --fake                 # a simulated cluster: try it anywhere, no Slurm needed
tower --unicode              # explicitly prefer Unicode block graphics and symbols
tower --ascii --no-color     # portable fallback for a limited terminal
tower --watch --bell         # the animated non-interactive screen (Ctrl-C exits)
tower --once --tab history   # one frame of text, e.g. to paste into a chat
tower --json                 # the whole snapshot as JSON, for scripts
tower --write-config         # ~/.config/tower/config.toml with the commented defaults
tower --profile carc         # a [profiles.carc] section of the config: a cluster (host, account, ...)
tower --host login.example.edu   # from a laptop: every Slurm command over ssh, logs read remotely
tower --record today.jsonl.gz    # keep every command and answer; --replay today.jsonl.gz --speed 10 plays it back
tower --ui-trace ui-trace.json   # record interactive UI timings; quit normally to save the new private report
tower run cancel 123 --yes   # one palette command without the screen (exit 0 ok, 1 failed, 3 needs --yes)
tower --eval '[j.id for j in running if j.cpu < 0.3]'     # an expression over the snapshot
tower --wait-for 'n_pending == 0' --timeout 3600          # block until it holds (exit 0) or time out (exit 2)
tower --report report.txt    # the whole dashboard as a plain ASCII text report
```

## Install and configure

Start with the [quick start](../README.md#1-install-on-carc) or the
[adaptive runbook](runbook.md). The commands below use `tower` after installing
the Bash alias or Python package; `./scripts/tower` from the checkout accepts
the same arguments.

## Terminal visuals and accessibility

The dashboard uses solid Unicode blocks for resource bars, distributions, and
timelines. Fractional blocks preserve values at one eighth of a character's width.
Filled area charts use gradient blocks; telemetry curves use continuous Braille strokes
with two horizontal and four vertical positions per character. Measured resource heatmaps use opaque cells,
an explicit scale, and numeric readings. Composition strips carry a legend with
the underlying counts or allocation values.
`--unicode` explicitly selects the Unicode preference; `--ascii` selects portable
characters. By default Tower checks the output encoding and terminal type and
falls back to ASCII when needed. `--unicode` overrides the default fallback for
`TERM=dumb`, but cannot override an output encoding that lacks the glyphs.
Existing configurations with `ascii = true`
retain that preference unless overridden on the command line.

Color adapts to terminal support. The interactive curses renderer uses native
truecolor when ncurses and the terminal description expose direct colors, with
256-color and basic fallbacks. ANSI `--once` and `--watch` output uses truecolor gradients
when `COLORTERM` is `truecolor` or `24bit`, or `$TERM` names a direct-color terminal;
otherwise it selects 256 or basic colors. Labels, axis values, and state names convey
the metrics without relying on color alone. `--no-color` and a nonempty
`NO_COLOR` environment variable suppress colors. `T` or `:theme NAME` selects
`default`, `dark`, `light`, `terminal`, `mono`, `high`, `cb`, `reader`,
`darcula`, `modnokai`, or `gruvbox-dark`:
`dark` and `light` provide matching surfaces and readable semantic colors;
`terminal` retains the terminal's background. `mono` retains the graphics without
colors; `cb` uses the color-blind palette; `reader` forces plain ASCII text.
`monokai` is an alias for `modnokai`; `gruvbox` is an alias for `gruvbox-dark`.
Quoted `"Gruvbox Dark"` and `gruvbox_dark` normalize to `gruvbox-dark`.
Runtime changes repaint the canvas, blank cells, text, menus, information strips,
chart colours, and all interface symbols with the active palette. Status symbols retain their semantic colors.
Terminal, mono, and reader keep the
terminal's default background. Colour capacity determines the available approximation.

Charts use collected measurements. Unavailable metrics and gaps remain unknown,
and time-series detail grows as samples arrive. ASCII `--report` exports remain
portable plain text regardless of the live dashboard's glyph preference.

Visible metric plots have a thin pointer crosshair in the active theme's accent color, with the graph's background.
Unicode selectors use two horizontal and four vertical Braille positions per cell.
The crosshair reaches the latest reported cell immediately.
Only its Braille dot phase animates, for at most 24 ms inside that cell.
Curve and annotation glyphs retain their original style at intersections.
Mouse coordinates still resolve to whole terminal cells.
ASCII and reader modes use static dots and `+` intersections.
Set `animations = false` to disable easing and keep the Unicode selector static.
Press and drag inside the plot, then release inside it, to select a time interval.
The time selection must span two columns; labels and buttons are outside capture.
Tower fits its vertical scale to the known curve in that interval.
Hold Shift before pressing to select explicit horizontal and vertical bounds instead.
That rectangle must also span one row.
Selected intervals use relative `s`, `ms`, or `us` labels with a start-time anchor.
Use `u` or `0` while pointing at the graph to undo or reset, or use
`:chartzoom undo|reset` for the last selected or zoomed graph.
Right-click inside a plot to restore its full view and turn off its Live window.
This keeps the selected job and marks in Jobs Details.
The inspector provides `:chart undo|reset` and corresponding buttons.
Zoom belongs to the exact metric/source/job/attempt and remains session display state.
An active drag has a three-cell margin beyond the axis labels and tick row, clipped to the actual visible pane.
Movement and release inside the margin use the nearest plot edge; the initial press must remain inside the plot.
The drag retains both painted axes while sampling continues. New samples do not cancel it.
Esc, movement beyond the margin, changed source or geometry, menu/dialog/startup, or a
15-second missing-release timeout cancels an unfinished preview.
See [Graph interaction](guides/charts.md#zoom-a-rectangular-area) for controls and bounds.

F8 navigation uses the current painted viewport. In Jobs and Recents, focused
real rows select their exact job for Details. Up and Down scroll or load another
row at the viewport edge; Right reaches visible Details controls, including
stacked layouts. Sorting, filtering, and scrolling publish fresh row geometry
before activation. Focus movement does not activate Quick Advisor or another
Details button. See [Button navigation](guides/pointer-navigation.md#select-controls-with-the-keyboard).

Roomier terminals reveal resource cards, node heatmaps, and additional charts;
compact terminals retain the essential tables and labels. These visuals reuse
the existing snapshot and sample history without increasing Slurm polling.

Interactive launches can show a short Unicode welcome animation.
ASCII mode uses a static welcome.
Use `:startup [on|off|toggle|preview]` or the View menu to control it.
The command without an argument reports the enabled preference.
Preview leaves that preference unchanged.
The reader theme and `animations = false` skip the welcome.
One-frame, JSON, scripted, and ANSI watch output also skip it.

Running metric plots can show a per-metric Live toggle and two adjacent sliders.
The first is a logarithmic display-window slider, from 30 seconds at the left to one second at the right.
The second requests polling from five seconds to 500 milliseconds for that exact job and source.
Its interval scale is logarithmic. Numeric commands use positions one to 100, rather than frequency multipliers.
It shows the effective interval in `s`, `ms`, or `µs`; ASCII mode uses `us`.
Live displays the interval ending at the graph's delayed display time, using retained observations.
Running timestamped graphs also use that buffered edge in the ordinary retained view.
The target delay is twice the greater of effective polling and observed sample cadence, up to 30 seconds.
Actual lag and wait status identify the displayed history; **Poll** and **Set** still identify read intervals.
Missing observations remain gaps. Alerts, current-value readouts, and stored measurements remain current.
Completed jobs, comparisons, and explicit time selections retain their existing bounds.
See [Buffered running graphs](guides/charts.md#read-buffered-running-graphs) for startup and recovery behavior.
Line plots label their local quadratic trend as `fit` and dense observed low/high bands as `range`.
The fit models the visible curve; the range represents observed extrema, not statistical confidence.
Both layers preserve missing intervals and original sample inspection.
See [Trend and range layers](guides/charts.md#read-the-trend-and-range-layers) for their data and display limits.
Changing the display window alone does not change Slurm sampling or create new measurements.
The separate sampling request applies even when Live is off.
Right-click the first slider to restore 30 seconds, or the second to restore its five-second polling request.
Each reset preserves the other slider, Live state, and graph selection.
The graph display clock requests at most ten scheduled refreshes per second; input and animations can request other refreshes.
A valid rectangular zoom turns Live off; cancellation resumes it.
Completed jobs have no active Live or sampling controls.
See [Live metric windows](guides/charts.md#follow-a-running-metric) for mouse, keyboard, and temporary-token commands.

## Workbench navigation and layout

The first terminal row contains File, Edit, View, Help, the Copy/Yank switch, the quit `x`, and the update slider.
F10 opens or closes a menu.
Left/Right changes menus; Up/Down selects a choice; Enter activates it; Esc dismisses it.
Parameterized choices open an editable command prompt.
Dismiss the dropdown before editing the opened prompt or completing an action review.
Menus retain the underlying screen and existing job-action reviews.
Hover highlights controls without activating them.
Direct menu actions retain the dropdown while the pointer stays inside it.
Pointer movement outside the dropdown dismisses it.
The toolbar label row remains a corridor for switching menus.
Esc and F10 close menus explicitly.
Use `:menu [File|Edit|View|Help]` or `:about` for command entry.
See [Toolbar menus](guides/live-workbench.md#use-the-terminal-toolbar) for every choice and its operating context.

Jobs and History Details provide Inspector, Logs, Investigate, Research, Analytics, Quick Advisor, and Off buttons.
Each button reads the exact selected active, recent, or historical job.
Click a button, then use arrow keys and Enter to activate another visible control.
Click content to restore its normal navigation.
Use `:jobpanel focus` for native mode selection with arrows.
Enter or Tab changes native focus between mode buttons, subview buttons, and content.
Esc returns input to the job rows.
Use `:jobpanel focus` for keyboard entry.
Inspector, logs, and investigation evidence update from background snapshots during the session.
The full Logs page retains complete-file operations.
Research includes all twelve workspaces; Analytics includes all five subviews.
Their content fits the Details width and scrolls vertically.
Each inline subview retains an independent scroll position.
Jobs and History save separate mode, Research subview, and Analytics subview preferences.
Inline Job series uses the exact selected job, including a job with no samples.
Aggregate Analytics views retain their stated accounting window.
The full Research, Analytics, and Logs pages retain their own view settings.
See [Inline inspection](guides/live-workbench.md#inspect-a-job-inside-jobs) for every panel control.

Quick Advisor runs only after explicit activation with its button or `:jobpanel quick`.
Restoring the mode leaves it idle until Analyze this job is selected.
It uses published job, inspection, accounting, compatible history, and resource-series evidence in a bounded background task.
A loading box leaves normal display input available.
Refresh analysis captures newer evidence explicitly.
Changing the job or leaving the mode discards a stale result.
Task-scoped MaxRSS does not establish aggregate memory usage or headroom.
The report identifies unavailable measurements, sampled coverage, and truncation.
See [Quick Advisor](guides/adaptive-workspaces.md#request-quick-advisor) for sources and operating limits.

F8 or `:focusbuttons` toggles directional focus for visible controls.
Arrows select a nearby control; Enter or Space activates it.
Tab and Shift-Tab traverse the current controls in order.
Esc restores ordinary content navigation.
The graph follows visible geometry and updates after layout and page changes.
Hover does not change the selected job or file.
Use [Mouse and button navigation](guides/pointer-navigation.md) for focus, dragging, menus, and terminal support.

Tower retains the published document for cosmetic pointer feedback and paints changed terminal rows.
Data, actions, resize, and animation deadlines still refresh the document.
Pointer traffic does not postpone scheduled publication.
Bounded Advisor, chart-data, and Details reflow caches update when their inputs change.
See [Display and input performance](guides/ui-performance.md) for checks and connection limits.

Mouse-wheel scrolling targets the latest requested viewport position.
The bounded PID controller combines wheel movement without replaying stale steps at an edge.
It clamps acceleration, avoids overshooting the target, and discards accumulated motion after a direction change.
Keyboard movement, selection, and copy positions remain immediate.
Use `:smoothscroll [on|off|toggle]` to change smoothing; no argument toggles it.
The reader theme and `animations = false` keep immediate wheel movement.
Scrolling changes display scheduling without changing scheduler polling or backoff.

Scrollable panes reserve four left header cells for top/bottom arrows and one right-edge cell for a scrollbar.
Drag its thumb, click above or below it, or use the header arrows to scroll the pane.
The controls preserve the selected source and do not activate rows beneath the scrollbar.
A changed row count keeps the gesture; source, page, geometry, layout, or modal changes cancel capture.
Advisor, History, Timeline, and comparison documents retain their complete published content for scrolling.
See [Pane navigation](guides/pane-navigation.md) for controls, reduced motion, text selection, and editor delivery.

`Ctrl-W` or F6 focuses Main/Details panels; arrows/page keys scroll a focused
Details panel and `z` maximizes it on non-Logs pages. `:maximize` supplies the
layout action on any page. `:density comfortable|compact|focused` changes
information density; `:layout split 20..80` controls Main's percentage on wide
screens. `:layout save NAME`, `load NAME`, `delete NAME`, and `list` manage up to
16 layouts. UI preferences persist when ordinary state is enabled.
Click a Jobs or Recents row to give its list arrow-key ownership after a Details interaction or mouse range selection.
Esc from Details control navigation returns to Main in one press. Focus changes preserve marked jobs.

Grey dividers support mouse capture from their line and one cell beside it.
Drag and release to change the split; Esc restores the starting size.
Page, dialog, and terminal-size changes also cancel capture.
Only full vertical dividers carry a centred blue diamond.
ASCII mode uses `|`, `-`, and `*`.
Click a divider to focus it, then use axis arrows or page keys to adjust its size.
Use `:pane-focus KEY` or `:pane-resize KEY smaller|larger` for command access.
Jobs Main and Details use `workspace:jobs`; History uses `workspace:history`.
The horizontal divider above Recents changes its data-row budget.
See [Adjustable workspaces](guides/adaptive-workspaces.md) for minimum sizes and keyboard steps.

Analytics, Dependencies, Logs, and Research include a virtualized job-history panel.
Its explicit selections preserve exact active, departed, and historical IDs.
Analytics opens the selected resource series; Logs uses that job's own sources; Research selects its project evidence; Dependencies selects its dependency context.
Missing historical data remains unavailable.
Drag the `⠿` handle (`::` in ASCII) to an edge to use a column or horizontal strip.
Use `:history-dock auto|left|right|top|bottom|next|off` and `:history-browser [on|off]` for command control.
`:history-focus` gives keys to the browser; Esc returns them to the data.
`:history-job JOBID` activates an exact available record.
`:advisor-job JOBID` selects an available running-job row in Analytics → Advisor and keeps that view open.
`:history-scroll up|down|page-up|page-down|home|end` changes its viewport without changing the data target.
Each page retains its dock and history share when normal UI state is enabled.
See [Dockable history](guides/adaptive-workspaces.md#keep-job-history-beside-the-data) for fallback dimensions and runtime updates.

`Ctrl-B`, Alt-Left, or `:back` returns to the prior location and restores its
selected identity, filters, table options, panel context, and retained log
position. Breadcrumbs make the current job/workspace/file visible. `Ctrl-P` or
`:workspaces` opens the searchable Research picker; `:workspace NAME` opens one
directly. `I` or `:inspect [JOBID]` opens the shared active/recent/historical job
inspector. Tab changes sections, `l` opens logs, and `e` opens Evidence.
The column editor provides clickable optional-column checkboxes with hover and directional focus.
Required identity and state columns remain visible.
Chart Events and Timeline provide clickable exact job or log citations.
Use `:timeline open EVENT_NUMBER` for the current event number, starting at one.
Use `:timeline seek EVENT_NUMBER` for replay seeking.

Help (`?` or `:help WORDS`) is contextual, searchable with `/`, and scrollable
with arrows/page keys. The command palette supports cursor editing, quoted
arguments, fuzzy selectable suggestions, and supported argument/path completion.
Up/Down chooses suggestions, Tab completes, and PgUp/PgDn recalls command history.
Ordinary action confirmations scroll through every target, with visible Cancel
and Confirm controls selected by Tab and activated by Enter; `y` confirms and
`n`/Esc cancels.

`Ctrl-A` or `:activity` opens retained session notices and available task status;
`y` copies a selected notice/export path. `:task cancel` or `c` there requests
cancellation of a running full-log copy, large-selection copy, or interactive
report export. Other background
readers display busy status when numerical progress is unavailable. Batch
execution uses its separate `c` cancellation control.

See the [workbench guide](WORKBENCH.md) for all twenty improvements, bounded
project discovery, graph inspection, log presentation, and execution recovery.

## Tabs

The **Research** tab (`0`) includes twelve workspaces: experiment telemetry, job arrays,
failure evidence, output contracts, run passports, batch submission preparation,
resource predictions, queue-start forecasts, scheduler blockers, submission
tradeoffs, controlled scaling and workflow critical paths.
Use Left/Right for subviews and PgUp/PgDn to scroll. Jobs and History carry the
selected job into Research. See [Research workflows](RESEARCH.md) for schemas,
instrumentation, commands, examples and inspection limits.
See [Wave two planning](WAVE_TWO.md) for evidence requirements, prediction and
calibration limits, reviewed choices, and complete scaling/workflow examples.

```bash
tower --fake --tab research --research-view predict
tower run blockers 123
tower run scaling plan examples/planning/scaling.json --workdir "$PWD"
tower run scaling analyze measurements.json --mode strong --baseline 1
tower run workflow analyze examples/planning/workflow.json
```

`--research-view` accepts `experiment`, `arrays`, `evidence`, `artifacts`,
`passport`, `submit`, `predict`, `forecast`, `blockers`, `tradeoffs`, `scaling`
and `workflow`. `--planning-file PATH` attaches explicit local observations or
a recipe. `predict`, `forecast` and `blockers` use the selected snapshot job, or
read an explicit bundle with `--file PATH`; the latter runs without contacting
Slurm. `tradeoffs`, `scaling` and `workflow` use explicit local files.
Standard project runs can declare these files under their own `reports/` directory.
The exact `run.json` job ID connects the selected job to its report paths.
Tower rechecks those inventories and declared sources in the background.
Manual selection resolves duplicate attempts; incomplete discovery does not choose an arbitrary run.
See the [Research source map](PROJECT_STANDARD.md#files-for-every-research-view) for all twelve workspaces.
Planning preparation is separate from confirmed submission. Prediction ranges
require matching evidence, queue forecasts require recorded calibration, and
scaling spread describes measured repeats rather than future-run confidence.

| tab | what it shows |
|---|---|
| **Jobs** | your running jobs (by start) then pending ones (by priority): partition, state, nodes, CPUs, GPUs, elapsed against the limit, time left or time waited, CPU rate, CPU efficiency so far, memory against the request, GPU utilisation, flags, and the reason / projected start / priority of pending jobs. Below the table: the selected job (bars and sparklines for time, CPU, memory and each GPU with its mean since the dashboard started, the node's load, a tail of its stdout; for a pending job its dependency with the names of the jobs it waits for), the recently finished jobs, the last events. |
| **Cluster** | the partitions (availability, limit, nodes and CPUs allocated / idle / other, your running and pending jobs there, GPUs free per type with drained ones subtracted), GPUs cluster-wide with each node counted once, fair share, your account's overall load (everyone's jobs); **queue weather**: the pending jobs, CPUs and GPUs (per type) waiting ahead of a new job in each partition, idle nodes and free GPUs, and what `sbatch --test-only` projects for a few typical jobs (one per GPU type of each GPU partition and one per partition with your jobs, or the `weather_probes` of the config): "a job of 8 cpus, 32G, gpu:a100:1, 01:00:00 would start in 1h09m (07:32) on a01-06"; **allocation**: the account's core-hours and gpu-hours this month against the association's limits (`sreport`, `sacctmgr GrpTRESMins`), the burn rate of the last 7 days, the month-end projection and the days left at that rate, the top users (also `allocation cpu 38% gpu 41%` in the header). |
| **History** | sacct over the last N days (default 2): state, elapsed, CPU and memory efficiency (what `seff` reports), peak memory, exit code, nodes; a summary line with the counts per state, core-hours and gpu-hours; sortable and filterable. |
| **Analytics** | five views (`←`/`→`): **job series**, full-width charts of a job's CPU rate, memory against the request and up to four observed session devices and four trace indices with utilisation and observed busy mean over time, from samples the dashboard records to `state/series/<job>.jsonl` (so they survive restarts and outlive the job; `↑`/`↓` and Home/End move between jobs; in the compact native layout, wheel and Page Up/Down scroll the metric document beneath its sticky heading; Enter on a history row opens its series); **history**, jobs per day stacked by outcome with core-hours and gpu-hours, core-hours per partition, the queue-wait and CPU-efficiency distributions; **timeline**, a Gantt chart of every job in the window with its queue wait and run. `=`/`_` widen or narrow the window (1, 2, 7, 14, 30 days; sacct refreshes); **advisor**, what every job name in the window should have asked for (see below); **compare**, the marked jobs (or `:compare 123 456`) side by side: a table (elapsed, cores, CPU mean and max, peak memory and its share of the request, GPU mean, core-hours) and, per metric, one chart per job aligned on its first sample. |
| **Nodes** | two views (`←`/`→`): **my nodes**, the nodes running your jobs: state, allocated CPUs, load, memory in use, GRES and GRES in use, GPU utilisation with a sparkline per GPU; **cluster map**, every node of every partition (or of the configured `partitions`) as a cell: a state glyph (idle, mixed, allocated, down or drained), cores allocated / total, GPUs in use / total, the nodes running your jobs marked, with per-partition totals. |
| **Deps** | the dependency chains among your jobs (the `Dependency` field of squeue: `afterok`, `afterany`, `afternotok`, `after`, `aftercorr`, `singleton`), as trees from each root with every job's state; prerequisites that already finished are named from the history. The cursor selects a job; `c` cancels it **and everything that waits for it** (the confirmation lists the chain), `h` holds or releases the chain, `Space` marks, `Enter` opens its details. The selected panel on the Jobs tab says what a pending job still waits for and how many jobs wait for it. |
| **Group** | everyone in your account: a bar per user (running jobs, CPUs, GPUs, nodes, pending) and the table of all their jobs (sortable by user, state, name, id, time, priority; filterable; `C` exports it; `i` opens a job's details). |
| **Log** | logs stay attached to the exact job selected from active Jobs, Recents, or History, including failed and other terminal states. `O` opens a grouped file list combining actual scheduler stdout/stderr, bounded job-ID-matching files in both output directories, and explicit `tower.logs/v1` index entries from other locations. Select with arrows/PgUp/PgDn/Home/End and Enter; Esc returns to the list, then closes it. The displayed tail is bounded by `log_max_mb` and follows appended bytes. Arrows move a logical line cursor, page keys move across pages, Home goes to the first retained line, and End or `f` follows unless extending a selection. `v` starts a line selection; `y` copies its original text. `V` then `y`, `Y`, or `:copy all` copies the entire selected file through a worker, independent of the tail limit. `/` searches, `N`/`P` move through matches, `L` opens `less`, `w` wraps, `e` switches stdout/stderr, and `o` cycles files. `m` bookmarks a line; `'` jumps to the next bookmark. Bookmarks and wrapping persist in `ui.json`. Missing historical paths stay missing instead of falling back to a running job. |
| **Sources** | every Slurm command the dashboard runs: cadence, last success, latency, calls, errors, backoff, the last error; `x` disables or enables one (say `sinfo` on a slow controller). |

## Observed job completion

When a job disappears from a successful live-queue observation, its last real
record remains selectable in Jobs' Recents as **awaiting accounting**. Queue
absence alone does not establish `COMPLETED`, failure, cancellation, or another
terminal state. Tower requests a bounded, expedited accounting refresh while
respecting source timeouts and backoff. A matching terminal accounting record
confirms the attempt and adds it to History; cached details and log paths remain
associated with that exact job. Delayed accounting remains visible as pending
evidence instead of a fabricated terminal record.
If the same job ID is requeued, Tower clears the old attempt's cached details
and requests fresh metadata subject to source backoff; old log paths do not
silently become evidence for the new attempt.

In the interactive terminal, departure produces a 1.6-second block-particle
motion toward Recents. Confirmed History arrivals trigger exactly two
0.35-second pulses, followed by a static `+N` unread badge. Entering History
acknowledges the badge. Existing history loaded at startup and unchanged polling
results do not replay a completion animation. Set `animations = false` in TOML,
`"animations": false` in JSON, or use the `reader` theme to retain static notices
without motion or pulses. One-frame reports are static as well.

## Keys (remappable in the config)

| keys | action |
|---|---|
| `↑` `↓` `j` `k` `PgUp` `PgDn` `Home` `End` `g` `G` | move; `g` instead groups two or more marked jobs in a focused job list. In Logs, move the logical line cursor before selection and extend it after `v` |
| `Tab` `S-Tab` `]` `[` `1`..`9` | switch tabs (7 Analytics, 8 Group, 9 Deps) |
| `Space` `a` `U` | mark the selected job, all visible jobs, none: actions apply to the marked jobs, else the selected one |
| `g` `u` | in a focused job list, `g` creates a closed manual group from two or more marked jobs. `u` dissolves the selected closed group or detaches selected expanded members; it clears marks when no grouped target applies |
| `p` | pin or unpin the marked or selected jobs: pinned jobs stay at the top of the Jobs table with a `⚲` mark; `:tag [ids] <tags>`, `:untag`, `:note [id] <text>` attach tags (a TAGS column, also on the History tab) and a note (shown in INFO and under the job); `/#tag` filters by tag; `j.tags` in expressions. All of it persists in `state/tags.json` across restarts. |
| `A` | clone and resubmit the selected job (also `:resubmit [id] ...`, `tower run resubmit 123 --mem 12G --yes`): the palette opens with `resubmit <id> `, where `--mem 12G`, `--time 03:00:00`, `-c 4`, `--gres gpu:a100:2`, `-p gpu`, `--dependency=`, `--script PATH` override the original flags and `--advised` applies the advisor's suggestions. The clone is the job's own submit line (sacct's `SubmitLine`, Slurm 23.02+, so `#SBATCH` lines in the script still apply and the overrides win over them) or, without one, an `sbatch` command rebuilt from `scontrol show job` and the record (name, partition, account, QOS, nodes, CPUs, memory, limit, GRES, an output pattern with `%x-%j` in place of the old name and id, the script). The confirmation box shows the exact command, the working directory it runs in, the resources, what `sbatch --test-only` projects for it and any caveat (an array task without `--array`, a kept dependency); `y` submits. The new job is tagged `from-<old id>` and the audit event carries the command. |
| `Enter` `d` `i` | the job's `scontrol show job` record with its **steps** (sstat per step: tasks, CPU time, peak memory and the task and node holding it, the slowest rank and how far behind the mean it is); `i` on the History or Group tab opens the same for that job (sacct -j for a finished one: steps, states, exit codes, peak memory). The selected panel shows the steps of a multi-task job, and the job's own GPU trace when `logs/gpu-util-<id>.csv` exists in its WorkDir (one nvidia-smi line per GPU per minute, see "The GPU trace" below): mean and current utilisation per GPU and the share of idle samples; the Analytics job view charts it. |
| `c` | cancel (a confirmation box lists the jobs; `y` confirms) |
| `h` | hold a pending job, release a held one (confirmation) |
| `R` | requeue a running job (confirmation) |
| `t` | `scontrol top`: put a pending job first among your own (confirmation) |
| `l` `L` `f` `+` `-` | the Log tab for the exact selected Jobs/Recents/History job; the current log in `less`; follow on/off; more / fewer log lines under the selected job |
| `O` `Enter` `Esc` | Logs: open the grouped file list; open its selected file; return from the file to its list, then close the list |
| `s` `S` | resume a single-column sort: cycle the tab's legacy sort keys (jobs: state, name, id, time, priority; history: end, name, state, elapsed, cpu eff, mem eff); reverse |
| `/` `Esc` | filter by name, id, partition or info; clear the filter (or the marks) |
| `n` `b` `r` `x` | GPU sampling on/off; bell on start on/off; sample every source now; Sources tab: enable / disable the selected source |
| `v` `V` `y` | Raw Logs: select original lines, mark the whole source, copy its bytes. Other panes: select rendered lines, select the painted pane, copy displayed text. Arrows/page keys or Shift-click extend a selection. Right-click clears selections, subject to the graph, History-list, and dialog controls below. |
| `Y` | Logs: copy the entire exact selected file, independent of displayed lines, search, scrolling, or wrapping |
| `I` | structured job inspector for the exact active, recent, or historical job; Tab changes sections, `l` opens logs, `e` opens Evidence |
| `Ctrl-W` / F6, `z` | focus Main/Details; maximize the focused panel (`:maximize` supplies this action on Logs) |
| `Ctrl-B` / Alt-Left | Back to the previous location, selected identity, filters, and retained scroll context |
| `Ctrl-P`, `Ctrl-A` | searchable Research workspace picker; retained activity/task results |
| F10 | File, Edit, View, and Help menus; Left/Right changes menus, Up/Down selects, Enter activates, Esc returns |
| F8 | Toggle directional focus for visible buttons and links; arrows select, Enter/Space activates, Esc returns to content |
| `E` `C` `J` | export the tab as text; its table as CSV; the marked or selected jobs with their recorded series as JSON |
| `:` | the command palette (Tab completes): `cancel 123 456`, `hold marked`, `filter rb2`, `sort name`, `days 7`, `tab history`, `view timeline`, `export csv`, `copy 5 12`, `gpu off`, `source sinfo off`, `theme mono`, `log 123`, `profile carc`, `eval n_pending`, and every plugin command |
| `T` | cycle the theme: default, mono, high contrast, cb (colour-blind safe: blue / orange / magenta for green / red / yellow), reader (plain text), dark, light, terminal, darcula, modnokai, gruvbox-dark |
| `?` `q` | help; quit (Esc closes an overlay, cancels a selection, clears the filter or the marks) |

The mouse works too: a click selects a row or switches tabs, a double-click opens the details (Jobs) or the series
(History), or opens the selected file in the Logs browser. The wheel scrolls,
and Shift-click extends a line selection from the last click.
Right-click inside a metric plot restores its full view and keeps selected and marked jobs.
Right-click a selected or marked job row for **Create Group**, **Ungroup**, and applicable **Add to GROUP NAME** choices.
Right-click elsewhere on a main page clears job selections, marks, and line selections without activating the surface beneath it, subject to History's export control.
In History, Shift-drag through rows to mark a range. Right-click a selected row and choose **Export logs** for the clipboard or directory export menu; right-click elsewhere inside the list to open export directly.
Right-click outside the History list clears its job selection.
In Logs, clearing keeps the same source open, including raw, alternate, and browser views.
Click a job or line, or use navigation keys, to select again.
In a Log Tools page, results list, or bookmarks list, right-click clears only the local selection.
The dialog and its source remain open. Cursor-line copies and result actions require a new explicit selection.
Help, Details, and analysis dialogs clear local text or row selections on right-click.
They preserve the selected job, marks, source, and dialog.
Analysis graphs retain their right-click reset control.
Select a cleared sample or event again before a row action.
Use `:historylogs [clipboard|directory|cancel]` for History export commands.
See [Export History logs](guides/log-view.md#export-logs-for-history-jobs) for full-source copying, expected missing-file alerts, folder creation, and receipts.
Plain drag through visible job rows marks a range in the current table order, including from selected or marked rows.
Shift-drag retains earlier marks. Click then Shift-click marks the inclusive range in either direction.
Alt-dragging a selected or marked row starts an explicit group membership move.
Esc during capture restores the earlier marks.
Release completes the marked range; supported bulk actions use their existing confirmation.
Drag the top-right slider to change the fetching interval.
Its labels show the effective Jobs interval, from five seconds down to 500 milliseconds.
Use `:rate N` to select a logarithmic position from one to 50.
Right-click the update control to restore five seconds.

Click any visible **column heading**, including **JOBID**, to cycle ascending,
descending, then off. Cascades follow the order in which you activate columns:
`NAME ^1` is the primary ascending order; `CPU v2` resolves equal names by
descending CPU count. Changing direction keeps that priority. Turning a column
off removes only its rule; activating it again adds it after the remaining rules.
Turning every column off restores the source sequence. Equal rows retain that
sequence, and sorting applies to all rows before paging.

The seven independent table scopes are `jobs`, `recent`, `history`, `group`,
`nodes` (My Nodes), `sources`, and `cluster` (partitions). Recents sorts its
retained candidate records independently from History. The default initial preview is five.
Use `:recents` to change the size or time window; see the [table guide](guides/tables.md#feature-09).
Larger panels and scrolling can reach older matching records beyond that initial preview.
The wheel inside Recents stays in its own viewport.
Its arrows, page keys, Home, and End use exact job identities.
The available records remain limited by the accounting window and filters.
Numeric JOBIDs and array task
IDs use natural order (`2` before `10`, `123_2` before `123_10`). Measurements use
their original numeric values rather than rounded labels; unknown values stay
last in either direction. Jobs pins remain first, and optional array grouping
arranges the sorted rows into their observed task groups.
If sorting would hide the selected task behind a different folded group representative, Tower expands that array to keep the same job selected.

Automatic launch grouping is enabled by default; new groups start open.
It recognizes arrays, heterogeneous components, scoped explicit launch tags or scheduler comments/details, bounded dependency-connected launches, and ordinary submission bursts.
Two-job bursts require adjacent IDs, at most ten seconds, and matching nonempty owner, account, WorkDir, exact Command, and name.
Three or more jobs can use a matching name family within 30 seconds, bounded numeric ID gaps and density, and nonconflicting known script paths.
Dependency-connected groups require matching ownership and WorkDir within 60 seconds and a numeric ID span of 256.
Missing provenance and names alone do not establish a group.
Click the right-pointing chevron on the first visible expanded group row to close it.
Click the down-pointing chevron on the closed summary to restore its members.
During ordinary job-row navigation, Left closes the selected group and Right opens it.
With directional button focus active, Right from a Jobs row enters Details instead.
`:jobgroups [on|off]` toggles grouping; `:jobgroup toggle|open|close GROUP_ID` controls one known group.
`:jobgroup create` and `:jobgroup ungroup` use the focused job list's current marked or selected records.
`:jobgroup reset` removes all saved and session manual groups and explicit exclusions, allowing normal automatic deduction to apply again when grouping display is enabled.
It preserves the current `:jobgroups on` or `:jobgroups off` setting.
Reset changes local display preferences only and also releases the bounded manual identity storage.
Fold preferences span the job views and their history browsers.
Press `g` with at least two jobs marked in the focused list to create a manual group.
The Jobs page includes marks in both Main and Recents; another workspace uses its focused list only.
It starts closed and uses the same state-count summary and disclosure controls.
With no applicable marks, select a closed summary and press `u` to dissolve that group.
In an open group, mark the members to remove and press `u`; without marks, only the selected member is removed.
Removed members stay outside automatic grouping for that execution attempt.
Press `U` to clear marks without changing groups.
Grouping keys do not act on stale job marks while an editor, menu, graph, log text, or another pane owns input.
In particular, graph `u` still undoes zoom without clearing retained job marks. `U` explicitly clears job marks.
Click and release a job, then Alt-drag it onto an existing group's closed summary, expanded member, or chevron to add it.
Pressing a chevron directly retains its fold action and does not start a move; right-clicking a selected row's chevron opens its job menu.
Alt-drag a marked row to add the applicable marked jobs.
Plain drag always selects a range. Shift-drag retains earlier marks.
A closed source summary contributes only its real representative, not hidden members.
Esc, right-click, an invalid drop, or a changed source/view cancels the move without changing membership.
The job menu exposes **Create Group** for two or more targets, **Ungroup** for grouped targets, and **Add to GROUP NAME** when the destination would gain a member.
A marked-row menu uses applicable marks. An active unmarked selected-row menu uses that exact job and preserves unrelated marks.
Arrow keys, Tab, Shift-Tab, page keys, Home, and End navigate the menu; Enter or Space activates a choice. The wheel scrolls; Esc, `q`, and Ctrl-C close it.
Manual membership uses natural job-ID order; displayed rows retain each list's active sort, including its default sort.
Adding to an automatic group retains its label and converts its known members to manual membership.
An existing manual destination retains its identity and saved absent members.
Tower revalidates the exact source attempts and destination membership before changing local preferences.
See [Manual groups](guides/batch-launches.md#create-a-manual-group) for selection, persistence, and identity rules.
A closed group keeps a real representative record and exact action ID.
Hidden members are not automatically marked or added to cancellation.
Closed INFO cells show themed, disjoint counts for running, pending, dependency wait, dependency never satisfied, completed, failed, cancelled, and other matching records.
Compressed array ranges count as observed records, not expanded task states.
Grouping applies to available old accounting records and new queue publications, subject to the history window and source evidence.
See [Automatic launch groups](guides/batch-launches.md) for exact deduction limits, status badges, scheduler-comment launch examples, and action targeting.

Jobs has a six-cell progress field before JOBID and the sort identifier `progress`.
Its marking and fold gutter remains separate to the left.
The `▸` marker and four-cell fractional block bar use the exact job's published application progress.
ASCII uses a `p` prefix.
Without application progress, `◷` in Unicode or `t` in ASCII distinguishes elapsed/time-limit usage from completed work.
The six-cell field contains a narrow symbol, a space, and four block-bar cells.
Running time symbols animate; pending jobs show an animated hourglass and `wait`.
ASCII uses static `p`, `t`, and `w` labels.
Unavailable progress and limits show `--`.
Sources include the standard metric-stream `progress.completed`/`progress.total`, or valid `progress_fraction`, `progress_pct`, and `completed_steps`/`total_steps` numeric metrics.
These values update from job-bound, current published results without renderer I/O.
See [Progress column](guides/adaptive-workspaces.md#read-the-six-cell-progress-column) for interpretation and project publication.

`:sortby [TABLE] COLUMN [asc|desc|off]` provides the same controls. Omitting the
direction cycles that column; omitting TABLE uses the current tab. Use the column
key, such as `id` for JOBID and `cpus` for CPU:

```text
sortby jobs name asc
sortby jobs cpus desc
sortby jobs cpus off
sortby recent id asc
sortby jobs clear
```

`:sortby [TABLE] clear` disables every rule for that table. `s`, `S`, and
`:sort NAME` resume the legacy single-key sort instead. Cascades persist across
restarts with ordinary UI state, restore with Back, and are included in saved
Jobs and History views.

The palette follows the same targets: `:find PATTERN` from Jobs or History opens
that selected job's logs. In the file browser, `:filter TEXT` filters file labels,
groups, and paths; inside a file, it sets the content search. These filters stay
separate from the job-table filter. Empty tables or file lists have no action target.

## Copying and exporting

In a log file, arrows move the logical line cursor before `v` starts selection.
The cursor has a right-edge `›` caret (`>` in ASCII); selected lines have an
orange right-edge `◆` (`*` in ASCII), including wrapped continuations. With a
selection active, arrows, PgUp/PgDn, Home, and End extend the range across pages;
Esc cancels it. `y` copies source text without row numbers, status lines, search
highlights, or wrapping. Tabs and CRLF line endings are preserved for valid
UTF-8 text. The display's bounded retained history still applies to line ranges.
Other panes support rendered-line selection through click-drag or `v` and navigation keys.
`V` selects the currently painted pane. `y` or Edit → Copy copies its selected lines.
Job-row dragging retains exact job marking unless explicit rendered mode starts with `v`.
The rendered cache retains at most 20,000 lines and 8 MiB of UTF-8 text.
Missing displayed coverage causes a stated copy failure; scroll through the range or export the source.
Copied text includes only the pane's displayed columns, without neighboring panes, borders, or scrollbar rails.

The top toolbar switches between **Copy** and **Yank**.
The Edit menu provides the same destination switch.
`clipboard.destination` accepts `copy` (default) or `yank`.
Yank targets a running local Vim or Neovim and sets registers `0` and unnamed.
It resolves executables on `PATH` and checks an inherited owned Neovim Unix socket or a reported Vim server.
`TOWER_VIM_SERVER` selects an exact name when Vim has several servers.
Discovery times out after 0.4 seconds; delivery times out after 0.8 seconds.
Editor delivery accepts complete UTF-8 text without NUL bytes, up to 8 MiB.
It does not start an editor or change an editor buffer.
An unavailable or rejected editor delivery keeps the export and uses enabled clipboard fallback.
Complete-log and History multi-job copy operations share these captured destination and transport preferences.
See [Editor yanking](guides/pane-navigation.md#send-text-to-vim-or-neovim) for setup and troubleshooting.

Ranges larger than 256 KiB or 4,096 lines run on the shared worker, as do ranges
containing invalid UTF-8. These copies pin the selected source bytes and publish
a private unique `state/exports/log-selected-<uuid>.log` file before attempting
clipboard delivery. Invalid UTF-8 skips text clipboard transport rather than
substituting replacement characters. Small valid UTF-8 ranges keep the immediate
private atomic `clipboard.txt` fallback. With `--no-state`, exports use
`./tower-exports/` instead.
An unfinished selected last line keeps its original bytes without an invented
final newline.

Rotation, truncation, or eviction of selected lines from the retained tail
invalidates their selection. Pressing `y` then reports that you need to reselect;
it does not turn an expired range into a whole-file copy. Stale mouse clicks are
also rejected instead of selecting whatever now occupies the old screen row.

In Logs, `V` marks the **whole file** and `y` then requests a full-file copy.
`Y`, `:copy all`, or `y` with no line selection requests the same operation.
Open an entry with Enter before copying from the file browser. The worker reads
the exact selected file from its selected local or SSH backend in 1 MiB chunks,
with bounded memory, independently of tail-buffer limits, filtering, cursor
position, wrapping, or line count. It saves exact source bytes to a unique
private file with mode `0600` at `state/exports/log-full-<uuid>.log`; the default
state base is `~/.local/state/tower`, honoring `XDG_STATE_HOME`. With `--no-state`,
the file goes under the launch directory's `tower-exports/` instead.

A growing log is copied through its initial byte count; later appends are
excluded and the status reports that snapshot. The writer is not locked, so
this is not an atomic application checkpoint. Detected replacement, rotation,
truncation, in-place mutation, or a short read fails without publishing a
partial export or requesting clipboard delivery; retry the current file.

Tower attempts an OSC 52 request when the **complete** UTF-8 text fits its
100,000-byte base64 limit, and a local clipboard tool when available. It never
sends only a prefix. Terminal clipboard permission and acceptance cannot be
confirmed; the status says when a request was sent. Oversize or non-UTF-8 files
keep their complete raw export, with the skipped transport explained. A local
tool can accept larger text files when a usable display is available.

Small valid UTF-8 selections and ordinary screen-text copies publish private, atomically replaced
`state/clipboard.txt` (or `tower-exports/clipboard.txt` with `--no-state`) as a
fallback. The clipboard tools are pbcopy, wl-copy, xclip, xsel, or clip.exe.
Exports of views use `text-<tab>-<stamp>.txt`, `csv-<tab>-<stamp>.csv`,
`json-<tab>-<stamp>.json`, or `report-<stamp>.txt` under `state/exports/`.
JSON carries the job record, live statistics, GPU samples, controller details,
and recorded series. From the command line, `tower --csv --tab history`,
`tower --json`, and `tower --report [PATH]` print the corresponding reports.

`:export report` (and `--report`) writes **the complete dashboard as an ASCII text
report**: summary, jobs, partitions, history, analytics, nodes, dependencies,
group usage, logs, and source health. Tables, resource bars, trend charts, and
timelines use terminal-renderable characters. Read it with `less report.txt` or
attach it as plain text. JSON and CSV exports remain available for scripts.

Interactive `:export report` captures a private frozen scheduler/UI snapshot
and renders on the existing background worker. Navigation continues while
project files are read for the report. The complete ASCII file is published
atomically with mode `0600`; Ctrl-A / `:activity` retains the result path, and
`c` there or `:task cancel` stops between pages and bounded chunks without
publishing an incomplete report. The Log page contains a selected 40-line
window; use `Y` in Logs to copy the whole original file. Scripted `--report`
continues to wait for its result.

## Remote mode and profiles

`--host login.example` (or `host = ...` in the config) runs every Slurm command on that machine over ssh with a
ControlMaster connection (`~/.ssh/tower-*`), so a command costs one round trip after the first; stdout and the GPU
traces are read there too (`stat` and offset reads, so following a log costs one stat every 1.5 s), and `L`
opens `less` over `ssh -t`.  Actions go the same way.  `ssh_user` and `ssh_opts` (`["-J", "bastion"]`) complete it.

Interactive selected-log reads publish immutable background snapshots; frames
and key actions consume the last published buffer, including visible loading or
source errors, without performing SSH reads. Explicit noninteractive reads can
wait for their requested data. Unicode command input preserves actual paths
independently of ASCII display rendering.

A profile is a named set of configuration keys, one per cluster or per way of looking at one:

```toml
[profiles.mycluster]
host = "login.example.edu"
ssh_user = "me"
account = "lab_01"
partitions = ["gpu", "main"]
[profiles.local]
history_days = 7
```

`tower --profile mycluster` merges it over the top level; `:profile local` inside the dashboard restarts on that
profile (the header shows `user@host [profile]`).

## Recording and replay

`--record FILE` (or `record = ...` in the config) appends every command, its answer or error, and every action to
a JSONL file (gzipped when the name ends in `.gz`).  `--replay FILE` runs the whole dashboard against it: the
clock is the recording's (`--speed 10` plays ten recorded seconds per second, `--paused` starts still), every
tab, chart and log position works, actions are refused, and nothing is written to the state directory.  The
header shows `REPLAY <time> x<speed>` and a scrub bar under it with the position in the recording, its span, the
speed and the state.  `|` pauses and plays, `<` and `>` move 60 s, `{` and `}` halve and double the speed;
`:replay seek 10:30`, `:replay seek 50%`, `:replay seek +600`, `:replay speed 20`, `:replay pause`.  A recording
made with `--fake --record` is a self-contained fixture: the tests replay one.

## Scripted mode and expressions

`tower run <palette command>` runs one command without a screen: `tower run cancel 123 --yes`, `tower run export
csv --tab history`, `tower run hold marked` (marks come from the saved state).  An action that would ask for
confirmation needs `--yes` (exit 3 otherwise); a failed or unknown command exits 1.

`--eval EXPR` prints the value of an expression over the snapshot; `--wait-for EXPR [--timeout S] [--poll S]`
polls until it is true (exit 0) or the timeout passes (exit 2), for scripts that wait on the queue:

```bash
tower --wait-for 'not any(j.name.startswith("train") for j in jobs)' --timeout 7200 && ./collect_results.sh
tower --eval 'sum(j.gpus for j in running)'
tower --eval '[j.id for j in running if j.cpu is not None and j.cpu < 0.2 and j.elapsed > 600]'
```

The language is Python syntax restricted to literals, names, attribute and item access, arithmetic, comparisons,
`and or not`, conditionals, comprehensions and the functions `len min max sum any all abs round int float str
sorted bool list set` plus the string methods `startswith endswith lower upper split strip replace count find`
and `dict.get`; nothing else parses.  A missing field reads as None, and a comparison with None is False rather
than an error.  Names: `jobs running pending` (lists of jobs), `finished` (the history), `n_running n_pending
n_jobs n_finished cpus_running gpus_running gpus_free`, `free_gpus` (per type), `partitions nodes account events
source_errors now user`, `by_id` and `by_name`.  A job has `id name state partition pending running held dep
reason priority nodes cpus gpus gpu_type nodelist hosts elapsed limit left waited age` (seconds), `cpu eff mem`
(fractions), `rss mem_req` (bytes), `gpu gpu_mean` (percent), `account qos submit start est_start` (epoch
seconds), `command marked tags`.  A finished job has `id name state partition elapsed cpus gpus nodes cpu_eff
mem_eff rss mem_req exit start end submit nodelist core_hours gpu_hours ok`.  `:eval EXPR` in the palette shows
the same inside the dashboard; the alert rules use the same language.

## Plugins

Python files in `~/.config/tower/plugins/` (or listed under `plugins` in the config; `--no-plugins` skips them)
define `setup(api)` and register palette commands, flags in the FLAGS column, extra tabs, event hooks and
sampled sources, each with the same health and backoff as the built-in sources:

```python
def setup(api):
    api.command("hello", lambda app, args: f"hello {' '.join(args) or app.user}", "hello [name]")
    api.flag(lambda job, snap, app: "big" if job.cpus >= 32 else None, style="magenta")
    api.tab("hello", "Hello", lambda snap, app, width, height: [[("   hi there", "bold")]])
    api.on_event(lambda ev: print(ev["kind"], ev["text"]))
    api.source("ticker", 60.0, lambda slurm, store: store.event("tick", "tick"))
```

A plugin that fails to load or whose tab raises is reported (an event, a line on the tab) and never stops the
dashboard.

## The advisor

The **advisor** view of the Analytics tab (and `:advise [id | name]`, `tower run advise rb3-identity`) turns what
jobs used into what they should ask for: peak memory × 1.25 rounded to a round `--mem` (doubled after an
out-of-memory), `--cpus-per-task` from the CPU efficiency when it is under 50% (cores = CPU time / (elapsed × 0.7)),
`--time` from the longest run × 1.3 rounded up to 15 minutes (doubled after a timeout) when the limit is more than
40% slack or too short.  Per job name over the window: the runs, peak against request, efficiency, longest run
against limit, the suggested flags, the core-hours spent on idle cores (1 - efficiency × core-hours), and what
failed.  For a running job the same so far, with the finished runs of the same name widening the memory and time
figures: the selected panel shows one `advice` line once the job is older than `warn_after_minutes` or has
history, and "already N past the longest completed run" when it is running long.

## Alert rules

```toml
[[alerts]]
name = "idle gpu"
when = "running and gpus and gpu is not None and gpu < 15 and elapsed > 900"   # over each job (scope = "job")
every = 1800                    # seconds before the same job fires the rule again (0: once per job)
actions = ["bell", "event"]     # bell | event | notify (the [notify] command, TOWER_EVENT=alert) | command
command = ""                    # with "command": a shell command with TOWER_ALERT TOWER_JOBID TOWER_JOBNAME TOWER_TEXT
[[alerts]]
name = "queue empty"
scope = "cluster"               # over the whole snapshot: n_pending, free_gpus, source_errors ...
when = "n_pending == 0 and n_running == 0"
every = 0
```

Rules use the expression language above; the sampler evaluates them after every jobs, live and GPU round.  A
firing rings (the curses screen beeps, `--watch` writes a bell), logs an `alert` event (red in the events list),
runs the notify command or the rule's own command, and the header shows the active alerts (`⚠ 2 alerts: idle gpu
12480001, queue empty`) until their conditions turn false.  A rule that does not parse or fails to evaluate is an
`alert_error` event, once.  `--alert EXPR` (repeatable) adds a rule for one run: `tower --watch --alert 'running
and cpu is not None and cpu < 0.1'`.

## Figures and flags

CPU% is the rate between two accounting observations. Slurm's configured collection interval can differ from Tower's read interval, so the value can move in steps. Use `:telemetry` to inspect the available configuration; unknown configuration stays unknown. EFF is the CPU time
over elapsed × cores so far; MEM% the peak resident set against the request; GPU% the mean utilisation of the job's
GPUs at the last sample.  FLAGS: `!cpu` `!mem` `!gpu` when a job older than `thresholds.warn_after_minutes` is below
the configured fractions, `ending` under ten minutes before the limit, `held`, `dep`.  Finished jobs: CPU EFF and
MEM EFF are what `seff` reports, computed from sacct.

## Sources and cadences

| source | command | every |
|---|---|---|
| jobs | `squeue -u $USER` | 5 s |
| starts | `squeue --start` (pending jobs) | 10 s, and after every queue change |
| live | `sstat` (CPU time, peak memory of the batch step) | 5 s |
| gpu | Selected NVIDIA, AMD, or Intel vendor command inside the allocation; see [provider limits](guides/phase-one.md#select-a-gpu-provider) | 5 s |
| nodes | `scontrol show node` | 15 s |
| partitions | `sinfo` (partitions, GPU inventory per node) | 60 s |
| finished | `sacct -S now-Nhours` | 60 s |
| share | `sshare -U` | 120 s |
| account | `squeue -A account` (everyone's jobs in the account: the Group tab and the header's load) | 30 s |
| details | `scontrol show job` of the selected job | 20 s |
| weather | `squeue -t PD` cluster-wide (pending work per partition) and `sbatch --test-only` per probe | 120 s |
| budget | `sreport cluster AccountUtilizationByUser` since the first of the month and over the last 7 days, `sacctmgr show assoc` for the limits | 600 s |
| trace | the GPU trace CSV of each running GPU job and of the job the analytics view shows, through the file reader (local or ssh) | 5 s |
| fin_details | `sacct -j` of the finished job whose details are open | on demand |

Every source runs in a small thread pool on its own cadence, never overlapping itself, with a timeout per command;
a failing source backs off (doubling up to five minutes) without touching the others, and its error shows in the
header and on the Sources tab.  Nothing in the screen thread ever waits on Slurm.  A job that leaves the queue shows
within one `jobs` round.

The intervals above are effective defaults without a faster metric request.
Profiles can set different bases for sources other than jobs, live, GPU, and trace.
Those four native sources follow the global polling request directly, from five seconds to 500 milliseconds.
The top-right update slider shows the effective Jobs interval.
The command `:rate N` selects a position from one to 50, using a logarithmic interval scale.
Position one requests five seconds; position 50 requests 500 milliseconds.
Other sources use their configured base divided by the global speed ratio, from one to ten.
Ordinary sources, GPU probes, and local file readers retain a 0.5-second minimum.
Weather and budget retain 30-second minimums. Remote file readers retain a 1.5-second minimum.
Use `:rate reset` or right-click the update control to restore five seconds.
Per-metric sampling commands select positions one to 100 across the same five-second to 500-millisecond range.
Each metric control shows its own requested interval as **Set** and the shared effective interval as **Poll**.
Compact rows place the effective value before the polling track and `S` plus the requested value after it.
Moving the slider changes the requested label even when a faster global or companion-metric request keeps the effective interval unchanged.
CPU and memory share a job probe. GPU curves share a GPU probe.
Each uses the fastest global or per-metric request for the exact job, without multiplying those requests.
Jobs without a faster metric request retain their global cadence.
Reported application metrics and CSV traces retain their job-produced measurement cadence; faster polling only reads their files more often.
Failure backoff, source timeouts, and worker limits remain in effect.
The Sources table's **EVERY** column shows the effective collector cadence, using adaptive units such as `5s` and `500ms`.
It reflects active global and shared metric requests rather than the legacy configured native bases.
See [Update rate](guides/live-workbench.md#set-the-update-rate) for controls and persistence.

## Configuration

`worker_mode` accepts `single` or `multi` (default). The toolbar and
`:workers single|multi|toggle|status` control background concurrency at runtime.
`--workers single|multi` overrides saved startup preferences. Single retains
the separate UI thread. A reduction drains running task groups and their
callbacks before queued work starts under the new limit. See
[Background workers](guides/background-workers.md) for the operating procedure.

`~/.config/tower/config.toml` (or `.json`; `--config PATH`; `$TOWER_CONFIG`).  `tower --write-config` writes the
commented defaults without replacing an existing config (JSON on Python 3.10, TOML on 3.11+).  Sections: top level (`user`, `account`, `ascii`, `color`, `history_days`, `log_lines`,
`gpu_sampling`, `bell`, `partitions`, `gpu_types`, `weather`, `weather_probes`, `budget`, `animations`, `startup_animation`, `smooth_scrolling`), `[research]`, `[logs]`, `[exports]`, `[clipboard]`, `[intervals]`, `[timeouts]`, `[thresholds]`, `[[alerts]]`, `[notify]`
(`command` runs through the shell on the configured `events` with `TOWER_EVENT`, `TOWER_JOBID`, `TOWER_JOBNAME`
and `TOWER_TEXT` in the environment: a Slack webhook, an e-mail, anything), `[keys]` (action = list of key names).

`clipboard.destination` selects `copy` or `yank`; its default is `copy`.
`clipboard.osc52` and `clipboard.tools` control the clipboard transports used in Copy mode and Yank fallback.
These preferences also apply to complete-file and History multi-job copy workers.
The editor server variables are `NVIM`, `NVIM_LISTEN_ADDRESS`, and `TOWER_VIM_SERVER`.
See [Editor delivery](guides/pane-navigation.md#send-text-to-vim-or-neovim) for exact server checks and limits.

Set `logs.manifest_file` to an exact log-index filename, such as `logs.json`.
Relative filenames use the explicitly selected research workdir (`--workdir`),
otherwise the selected job's actual WorkDir; `{job_id}` follows that exact job.
The index's relative file entries use its own directory and may explicitly name
sibling locations. Exact absolute entries may name other directories; they retain
their source-machine meaning after copying a run. See the
[project log-index standard](PROJECT_STANDARD.md#log-locations-logsjson) and
[schema](schemas/logs.v1.schema.json). Tower caches bounded catalog work on its
shared background worker and does not recursively scan directories.

Set `exports.projects_root` to the existing local directory used by the History log destination picker.
For example, use `"exports": {"projects_root": "/home/alex/projects"}` in JSON.
An empty value uses the registered native project root, then `~/projects`.
The picker confines traversal and folder creation to this root.
Sources can reside at other registered locations; exported files are saved on the machine running Tower.
See [History log exports](guides/log-view.md#export-logs-for-history-jobs) for controls, missing-output alerts, and bundle format.

State lives in `~/.local/state/tower/`: `ui.json` (tab, sort, log lines, toggles) and `events.jsonl`, the append-only
log of every transition and every action (an audit trail: what was cancelled, held or requeued, when, and whether
Slurm accepted it).  `--no-state` reads and writes neither.

Set `state_namespace` to isolate state for different scheduler environments.
The default is an empty string, which retains the normal state directory.
A namespace must contain 1–64 ASCII letters, digits, underscores, or hyphens.
Nonempty namespaces use `<state_base>/profiles/<namespace>/<connection_digest>/`.
The connection digest includes backend, host, scheduler user, local owner, profile, and SSH connection options.
It separates preferences, log bookmarks, tags, events, exports, and recorded resource samples.
The supplied `desktop` profile uses `state_namespace = "desktop"`.
Existing state is retained in its original directory.
The normal base follows `XDG_STATE_HOME`, or defaults to `~/.local/state/tower/`.
Fake sessions, replay, and `--no-state` do not enable persistent state.
See the [Fedora desktop guide](DESKTOP.md#keep-desktop-state-separate) for the local setup procedure.

Set `show_all_partitions = true` to include every reported partition in Cluster.
The default is `false`, which retains the existing relevant-partition display.
The desktop profile enables this option to show idle CPU-only partitions.
A nonempty `partitions` list still restricts the displayed partition names.

Set `polling_multiplier` to a whole number from 1 to 50.
The default is `1`.
The field name is retained for compatibility; its value selects a logarithmic polling position.
Position one requests five seconds and position 50 requests 500 milliseconds.
Use `:rate N` or the update slider to change the active preference.
The saved position uses the same scoped UI state as other preferences.
A valid saved position overrides the configured launch default.
An explicit `--rate N` startup argument overrides the saved position.

Positive values in `intervals.jobs`, `intervals.live`, `intervals.gpu`, and `intervals.trace` remain accepted for configuration compatibility.
They no longer set the actual native polling interval.
Use `polling_multiplier`, the top-right slider, or Settings → Polling interval to choose that interval.
`--interval SECONDS` selects the nearest position in the five-second to 500-millisecond domain; `--rate N` takes precedence.
Settings retains `intervals.jobs` as a compatibility key for its Polling interval row and omits separate editable live, GPU, and trace rows.
Other `intervals` values still set source bases, adjusted by the global speed ratio and source limits.
Settings previews take effect during editing. Esc restores the exact previous position, and Enter saves the accepted preference.
An unaccepted preview is not written to saved UI state.

Set `startup_animation = true` to enable the interactive welcome by default.
Set `smooth_scrolling = true` to enable mouse-wheel viewport interpolation by default.
Both defaults are enabled.
Their active preferences persist with the current profile and connection's UI state.
Use `:startup off` or `:smoothscroll off` to change them during operation.
The global `animations` switch and reader theme suppress motion without erasing these preferences.

## The twenty features at a glance

This table describes the original monitoring features. The twenty new UI/UX
improvements in Tower 3.0 have their own [workbench map](WORKBENCH.md#the-twenty-improvements).

| | feature | where |
|---|---|---|
| 1 | resource advisor: suggested `--mem` / `--cpus-per-task` / `--time`, idle core-hours, time against the longest run | Analytics › advisor, the selected panel, `:advise` |
| 2 | clone and resubmit with overrides, the advisor's flags and an `sbatch --test-only` preview | `A`, `:resubmit`, `tower run resubmit` |
| 3 | dependency chains as trees, chain cancel / hold / release | Deps tab, `:chain` |
| 4 | alert rules (per job or over the snapshot) with rate limits: bell, event, notify, command | `[[alerts]]`, `--alert` |
| 5 | queue weather: pending work ahead per partition, `sbatch --test-only` projections | Cluster tab |
| 6 | allocation budget: used this month, limits, burn rate, month-end projection, by user | Cluster tab, header |
| 7 | cluster map: every node as a cell with state, cores and GPUs | Nodes › cluster map |
| 8 | the account's jobs: per-user bars and the full table | Group tab |
| 9 | session recording and replay with a scrub bar | `--record`, `--replay` |
| 10 | a complete ASCII text report | `--report`, `:export report` |
| 11 | remote mode over ssh with remote file reads | `--host`, `host =` |
| 12 | per-cluster profiles with hot switching | `--profile`, `:profile` |
| 13 | job comparison: a table and aligned charts of the marked jobs | Analytics › compare |
| 14 | pins, tags and notes that persist; `#tag` filters | `p`, `:tag`, `:note` |
| 15 | steps per job: tasks, CPU time, peak memory and where, the slowest rank; sacct steps of finished jobs | the panel, `i` |
| 16 | the job's own GPU trace CSV in the panel and the charts | `logs/gpu-util-<id>.csv` |
| 17 | log extras: grouped multi-location files, wrap, stderr, bookmarks | Log tab `O` `w` `e` `o` `m` `'` |
| 18 | scripted mode: one command, an expression, a wait | `tower run`, `--eval`, `--wait-for` |
| 19 | plugins: commands, flags, tabs, hooks, sources | `~/.config/tower/plugins/` |
| 20 | accessibility: a colour-blind palette and a plain-text reader theme; recordings as test fixtures | `T`, `theme cb`, `theme reader` |

## The GPU trace

Job Series shows recorded GPU utilisation and its observed busy mean when telemetry is available.
The mean uses valid retained device observations and excludes missing or invalid readings.
It is a GPU activity proxy, not FLOP efficiency, throughput, or an allocation-use ratio.
Session devices retain node/vendor identity and stable device IDs when exposed;
an index-only job trace cannot identify an allocation's node or devices.
See [GPU graph interpretation](guides/charts.md#read-gpu-utilisation-and-busy-mean) for source scope and stable graph IDs.

The panel, the charts and the advisor read a CSV the job writes itself, `<WorkDir>/logs/gpu-util-<jobid>.csv`, with
one line per GPU per minute: `timestamp, index, utilization.gpu, memory.used`.  Add this to a GPU job script, before
the work starts:

```bash
mkdir -p logs
nvidia-smi --query-gpu=timestamp,index,utilization.gpu,memory.used --format=csv,noheader,nounits -l 60 \
    > logs/gpu-util-${SLURM_JOB_ID}.csv 2>/dev/null &
trap 'kill $! 2>/dev/null' EXIT
```

Without the file the dashboard can sample the selected NVIDIA, AMD, or Intel
provider live inside the allocation (`gpu_sampling`). The example above is a
NVIDIA trace producer; provider selection does not convert its format. A
recorded trace adds history from before the dashboard started and survives the
job. See [provider requirements](guides/phase-one.md#select-a-gpu-provider).

## Architecture

```
tower/
  layout.py      pure layout: columns fitted to a width (shrink flexible ones, drop the least useful, then a floor),
                 ellipsis clipping by display width, bars, sparklines, rules, boxes, painters to text / ANSI
  model.py       typed records (Job, Live, GpuSample, Node, Partition, Finished, Health) and the Store: histories,
                 transitions, events, per-source health, persistence
  slurm.py       the command layer: one function per Slurm call, parsers on the recorded formats, and FakeBackend,
                 a simulated cluster answering the same commands as text
  sampler.py     the scheduler: cadences, a thread pool, per-source timeouts, backoff, health, kick
  transitions.py short monotonic queue-departure motion, confirmed History pulses, and an unread count
  actions.py     cancel / hold / release / requeue / top with an audit event each; the notification hook
  views.py       the tabs and overlays as rows of (text, style) segments
  interaction.py bounded visible-control graph, exact hit regions, hover feedback, and directional focus
  control_rows.py wrapped semantic control rows and translation into visible modal coordinates
  job_panels.py  shared Jobs/History inline workspaces, independent mode preferences, and Main/Details focus handoffs
  job_groups.py  snapshot-based launch deduction, shared folds, exact-job projections, and disjoint state counts
  manual_job_groups.py  bounded attempt-specific manual membership and automatic-group exclusions
  job_group_actions.py  frozen source selection and attempt validation for explicit membership changes
  job_group_drag.py  published exact-job drag targets, cancelled-capture handling, and destination feedback
  job_group_menu.py  selection-scoped group choices and History log-export handoff
  job_group_ui.py themed, width-aware group badges and representative rows
  job_selection.py exact job-ID range capture for existing marked-job actions and confirmation
  scrolling.py   bounded PID viewport motion, immediate selection, and saved smooth-scroll preferences
  startup.py     short interruptible Unicode welcome and static ASCII fallback
  toolbar.py     persistent mouse menus, editable command entry, captured update-slider dragging, and interval-control reset routing
  controller.py  the application state and every key, mouse click and confirmation; no curses, so tests drive it;
                 the line selection, the command palette, exports and the clipboard
  charts.py      continuous Braille curves, eight-level area bars, axes, histograms, heatmaps, and Gantt rows
  chart_interaction.py final plot geometry, thin theme-colored crosshairs, buffered exact-source time or XY zoom, cancellation, undo, and reset
  metric_live.py per-running-metric display windows and sampling sliders with independent mouse, keyboard, and reset controls
  metric_playback.py bounded delayed display clocks for acquired running-metric history
  metric_envelope.py display-bounded local polynomial models and exact observed range geometry
  metric_sampling.py exact-source requests, shared probe intervals, source limits, and adaptive interval labels
  analytics_document.py bounded native Job Series document scrolling and visible metric cards
  logs.py        bounded incremental log buffers, logical keyboard cursors, raw line ranges, and selection
  log_catalog.py bounded grouped file discovery and explicit tower.logs/v1 manifests on the selected backend
  log_copy.py    background exact-byte full-file snapshots and selected ranges, private exports, clipboard handoff
  log_bundle.py  exact-job multi-source full-file bundles, source hashes, confined destination listings and folder creation
  history_log_export.py asynchronous History export menus, directory picker, missing-source alerts and receipts
  research.py    the shared bounded worker for research inspections, log catalogs, and full-log copies
  worker_scheduler.py  shared worker permits, bounded queues, cooperative GPU waits, and live mode drains
  worker_ui.py   toolbar worker state, palette commands, and saved mode preferences
  clipboard.py   complete OSC 52 requests, streamed local clipboard tools, and private atomic text fallbacks
  editor_yank.py bounded register delivery to running local Vim/Neovim; clipboard fallback
  text_selection.py pinned rendered-line ranges over published panes; complete-cache copy checks
  scrollbars.py pane rails and endpoint arrows over published geometry; bounded viewport targets
  modal_scrollbars.py shared reserved controls and viewport adapters for boxed dialogs
  export.py      text / CSV / JSON exports
  screen.py      the curses loop (colours, mouse, resize, less), the ANSI watch loop, one-frame text and JSON
  cli.py         the command line: profiles, remote mode, recording and replay, scripted mode, plugins
  doctor.py      read-only environment checks for demo, local Slurm, and remote SSH capabilities
  clock.py       the dashboard's clock (the wall clock, or the recording's time under --replay)
  expr.py        the expression language (alert rules, --eval, --wait-for) and the namespaces over the snapshot
  record.py      RecordingBackend, ReplayBackend and the replay clock (pause, seek, speed)
  remote.py      SshBackend (ControlMaster) and the file readers: LocalFiles, RemoteFiles (stat and offset reads over ssh)
  plugins.py     the plugin API and loader
  advisor.py     the resource advisor: suggested --mem / --cpus-per-task / --time from finished and running jobs
  alerts.py      the alert rules engine (per job or over the snapshot, rate limits, bell / event / notify / command)
  deps.py        dependency parsing, the chain graph (trees, upstream, downstream) behind the Deps tab and chain actions
  resubmit.py    clone and resubmit: sbatch flag parsing, overrides, the clone from a submit line or a record, the preview
  report.py      the ASCII report: complete terminal snapshots of every tab
```

`tests/test_tower.py` runs the parsers on recorded output, the layout, the configuration, the store's transitions,
the controller against the simulated cluster (marks, confirmations, every action, filter, sort, tabs, overlays,
mouse), the one-frame and JSON outputs, the watch mode, the curses screen through a pseudo-terminal, the
expression language (and what it refuses), a recording replayed through the whole dashboard, the ssh transport
and remote file reads through a local shell, profiles, plugins (commands, flags, tabs, hooks, sources, a broken
one), scripted mode and the themes; the advisor on finished and running jobs, alert rules (firing, rate limits, scopes,
errors, the header and events, `--alert`), the dependency graph and chain cancel through the Deps tab, tags, pins,
notes and the compare view; sbatch flag parsing and overrides, clones from a submit line and from a record, and a
resubmission through the screen and the scripted mode (the fake cluster lists the new job); the ASCII report from the
screen and the command line, the replay controls, scrub bar and seeks, and the log extras (wrap, stderr, the job's
other files, bookmarks that persist).
