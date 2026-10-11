# Slurm Tower

**Inspect Slurm jobs, measurements, logs, and results in one terminal.**

Slurm Tower is a terminal application for CARC and other Slurm clusters.
It uses solid block bars, continuous terminal curves, resource maps, and labeled tables.
It runs through an ordinary SSH session.
It also provides an ASCII display mode and plain-text reports.

| Requirement | Specification |
| --- | --- |
| Interface | Terminal only; curses for the interactive display |
| Python | Python 3.10 or later, with curses and venv support |
| Operating system | Linux, macOS, or Windows through WSL |
| Runtime packages | Core: Python standard library only; optional adapters have separate requirements |
| Live data | Slurm commands available to your cluster account |
| Demonstration | Simulated scheduler; no cluster account required |
| Release | Tower 4.16.1 |
| License | [MIT](LICENSE) |

[Installation and operation](docs/runbook.md) ·
[Fedora desktop setup](docs/DESKTOP.md) ·
[Live workbench](docs/guides/live-workbench.md) ·
[Mouse and button navigation](docs/guides/pointer-navigation.md) ·
[Adjustable workspaces and job advice](docs/guides/adaptive-workspaces.md) ·
[Automatic launch groups](docs/guides/batch-launches.md) ·
[Display and input performance](docs/guides/ui-performance.md) ·
[Background workers](docs/guides/background-workers.md) ·
[Pane scrolling and text selection](docs/guides/pane-navigation.md) ·
[Controls](docs/CONTROLS.md) ·
[Phase one: measurement and submission checks](docs/guides/phase-one.md) ·
[Research and cluster operations](docs/guides/operations.md) ·
[Implementation roadmap](docs/ROADMAP.md) ·
[All 50 improvements](docs/QUALITY_OF_LIFE.md) ·
[Complete reference](docs/reference.md) ·
[Project reporting standard](docs/PROJECT_STANDARD.md) ·
[Bash Research automation](docs/guides/research-automation.md)

## Changes in Tower 4.16.1

Use **Collapse All** beside **Expand All** in Jobs Main, or enter `:grouping collapse`, to close shared job groups while keeping their membership.

## Changes in Tower 4.16.0

- Drag a table heading separator to resize its column. Right-click the separator to reset it. Jobs and Recents share column positions and widths; Progress retains its six-cell width.
- Click one job, then Shift-click another to mark the inclusive range in either direction. Plain dragging selects a range even when its first row is already selected. Use **Alt-drag** to add selected jobs to a group, or use the job menu.
- Use **Auto Group**, **Undo Auto**, **Ungroup All**, **Expand All**, and **Collapse All** above Jobs Main. Automatic grouping preserves manual membership. The completion trail now stays on Main's left edge.
- Drag a group's **↕** handle before or after another group to review a change to your pending-job order. Apply uses Slurm's native `scontrol top` where the site enables it. Dependencies, resources, reservations, and site policy still determine start times.
- Use `tower report` from Bash to record actual submissions, array tasks, retries, metrics, outputs, and Research sources. Runtime discovery links each report to its job attempt. Local and supported SSH readers refresh declared sources without restarting Tower.

Read the [validation report](docs/guides/qol-416-validation.md), [table controls](docs/guides/tables.md), [group and queue controls](docs/guides/batch-launches.md), and the [complete Bash reporting guide](docs/guides/research-automation.md). The reporting helper records supplied scientific evidence; it does not invent missing measurements or apply workflow operations automatically.

## Changes in Tower 4.15.1

GPU sampling can resolve numeric CUDA device IDs to stable UUIDs inside a job
allocation. This fixes missing NVIDIA graphs on desktops that also have AMD or
Intel graphics, while preserving allocation and MIG checks. The identity cache
avoids repeated driver initialization during rapid polling. Successful matches
also clear the ambiguity warning that caused unnecessary sampling backoff.

Use `tower --gpu-check JOBID` to check a running job after updating. See the
[GPU diagnosis guide](docs/guides/gpu-detection.md) for identity evidence,
driver requirements, and recovery steps.

## Changes in Tower 4.15.0

The twenty-six remaining research proposals now have explicit operation forms
and supported adapters. Open **File → Research and cluster operations**, or
enter `:ops catalog`. Related buttons also appear in Sources, Analytics, and
Research views, including embedded Research views in job Details.

- Inspect storage, quotas, reservations, licenses, service health, retained scripts, and allocation placement. Review pending-job edits, array concurrency, and allocation attachment.
- Compare environments, evaluate scientific acceptance and statistics, verify result reuse, and repair declared dependencies.
- Prepare checkpoint restarts, bounded parameter searches, packed tasks, Dask pools, heterogeneous allocations, and verified file transfers. Reconnect to a persistent read-only monitor and observe native workflow evidence.
- Inspect profiler exports, concurrent cluster snapshots, historical incidents, and energy per accepted result.

Set the fields and select **Inspect / prepare**. Changes require **Review
action → Confirm apply**. The work runs outside rendering and preserves the
current page when a result arrives. Source limits, unavailable evidence,
partial actions, and uncertain submissions remain explicit.

Read the [operation catalog and controls](docs/guides/operations.md) for every
feature, its [validation record](docs/guides/operations-validation.md), and
the [rollout status](docs/ROADMAP.md) for the exact implementation scope.
Optional engines and site features require their documented dependencies.
The proposed 5.0 stack tracer remains a separate design.

## Changes in Tower 4.14.0

- Inspect the difference between Tower's read interval and Slurm's collection capabilities. Unknown or unavailable configuration remains explicit.
- Select an optional NVIDIA, AMD, or Intel GPU adapter. Unsupported counters remain unknown, and device observations retain their allocation context.
- Check a local batch script with a supported shell's syntax checker and optional ShellCheck. Checks do not execute the script or submit a job.
- Attach scientific labels, parameters, and paths to actual Slurm array indices through a validated manifest. Task order does not determine scientific identity.

Use the [phase one guide](docs/guides/phase-one.md) for interface controls,
setup, and limits. The [roadmap](docs/ROADMAP.md) records the later 4.15 operation
rollout and the separate planned stack tracer.
See the [validation report](docs/guides/phase-one-validation.md) for test results,
render measurements, and hardware validation limits.

## Changes in Tower 4.13.0

- Group membership supports drag and drop. In the current release, hold **Alt** while dragging a selected job or marked selection onto an existing group. Plain drag selects a range. A cancelled or stale drag does not change membership.
- Right-click a selected or marked job for **Create Group**, **Ungroup**, and applicable **Add to** choices. History also provides **Export logs** through this menu.
- Manual membership uses natural job-ID order, independent of insertion order. With JOBID sorted ascending, adding `7–9`, then `4–6`, to a group containing `1–3` displays `1–9`. Each list retains its active sorting, including default sorts.
- Adding to an automatically detected group creates a manual group with its existing label and known members. Existing manual destinations retain their identity and saved members. See [Add jobs to a group](docs/guides/batch-launches.md#add-jobs-to-a-group).

## Changes in Tower 4.12.0

- Mark at least two jobs in a focused job list and press `g` to create a closed manual group. It uses the existing chevrons and state-count summary.
- Press `u` on a closed group to dissolve it. In an open group, `u` removes only the marked members, or the selected member when no jobs are marked. Press `U` to clear marks without changing groups.
- Manual membership and removals apply across job listings. Saved choices identify the execution attempt so that a reused job number does not inherit an earlier grouping.
- Menus, text selections, graphs, and graph/slider drags retain their input controls. See [Manual groups](docs/guides/batch-launches.md#create-a-manual-group) for selection and persistence rules.

## Changes in Tower 4.11.0

- Running metric graphs use a short adaptive display delay. The visible edge advances through measurements already received, which reduces the new-segment jump at each source update.
- Full-history and Live views follow the delayed display time, including the full-screen chart while it follows its running source. Duration controls and source polling remain independent. Graph labels identify the display lag and any wait for more data.
- Completed jobs, comparison plots, and explicit zoom selections retain their existing time bounds. Missing measurements remain gaps. See [Buffered graph playback](docs/guides/charts.md#read-buffered-running-graphs).
- Line plots add a labeled local polynomial trend when enough continuous data is available. Dense oscillations use thin observed low/high borders and a faint interior. Original values, spikes, and missing intervals remain distinct. See [Trend and range layers](docs/guides/charts.md#read-the-trend-and-range-layers).
- Expanded job groups show a right-pointing chevron on their first visible row. Click it to close the group, then click the down-pointing chevron on its summary to reopen it. Existing batch counts and shared fold preferences remain available.

## Changes in Tower 4.10.0

- Metric polling controls show both the requested **Set** interval and shared effective **Poll** interval. A faster shared request no longer makes a changed slider appear unchanged.
- Launch grouping uses available queue and accounting evidence for old and new jobs. Closed groups show state counts while retaining exact member identities. See [Automatic launch groups](docs/guides/batch-launches.md).
- History Details provides the same Inspector, Logs, Investigate, Research, Analytics, Quick Advisor, and Off modes as Jobs. Reports remain attached to the selected historical job.
- Grouping and Details use published snapshots and the existing background readers. Mouse movement does not fetch job evidence or activate another page.

## Changes in Tower 4.9.0

- One byte decoder handles mouse reports, keyboard controls, Unicode, paste, and resize. Extended mouse coordinates cannot become keyboard shortcuts through partial ncurses decoding.
- Native CPU/GPU series and traces reuse bounded prepared data. Indexed Live windows retain continuous curves, sample gaps, and corrections to older measurements.
- Rendering reuses fitted text, measured widths, and theme styles. A bounded pointer check before paint prevents an old crosshair position from appearing after a rebuild.
- Queue observers release the UI data lock before conversion and callback work. Each observer retains an isolated job snapshot.
- The toolbar switches live between Single and Multi background workers. Single keeps one background worker and a separate UI thread. Accepted work and results survive mode changes. See [worker controls](docs/guides/background-workers.md).
- Use the [sustained-input benchmark](docs/guides/ui-performance.md#measure-sustained-terminal-input) to measure queued input, paint intervals, and display work together.

## Changes in Tower 4.8.3

- Delayed mouse reports remain pointer input even when their first Escape byte arrives separately. Active drags allow a short prefix grace period.
- Metric archive reads and sampler file writes run outside the shared data lock. Interactive views use published samples while saved history loads in the background.
- Jobs Details renders the visible metric bands. Off-screen graphs retain their place in the scrollable document without repeating raster work.
- Pointer feedback validates visible Live controls. Background maintenance still checks all retained sampling requests.
- Completed background Research commands preserve the current page, selection, and source when the user leaves their workflow.
- Use `tower --ui-trace ui-trace.json` to record bounded UI timings and page transitions. Quit normally to save the report. See [the timing guide](docs/guides/ui-performance.md#record-a-desktop-latency-report).

## Changes in Tower 4.8.2

- The decoder recognizes legacy X10, SGR, and urxvt mouse reports. Version 4.8.3 also covers a delay immediately after the initial Escape byte.
- Clicks use the current published job, source, and pane. Stale row positions and changed hit payloads cannot open another job or page.
- Graph crosshairs repaint changed cells. Unchanged native curves reuse bounded raster caches while axes, source age, and live controls continue to update.
- Drag owners cancel stale captures after context changes. A new gesture retains its own release after an earlier gesture was cancelled.
- Passive movement does not activate buttons, rebuild the page, or read measurement sources.

See [the input audit](docs/guides/input-audit.md) for reproduced faults, regression coverage, and recovery checks.
See [display performance](docs/guides/ui-performance.md) for reproducible measurements.

## Changes in Tower 4.8.1

- Each running metric has two adjacent sliders: a display window from 30 seconds to one second, and a polling interval from five seconds to 500 milliseconds. Both scales use logarithmic steps.
- The top-right polling slider also spans five seconds to 500 milliseconds. Each native polling position changes the requested interval; requests cannot exceed two polls per second.
- Right-click either metric slider to restore its default: 30 seconds for the window, or five seconds for its polling request. The other control and graph selection stay unchanged.
- Sampling requests apply to the exact job and source. Shared probes use the fastest global or per-metric request, without multiplying the requests. Source limits and retry backoff remain active.
- The graph footer shows the effective sampling interval. Published application metrics and CSV traces remain limited by how often the job writes them.

See [chart controls](docs/guides/charts.md#follow-a-running-metric) for mouse, keyboard, commands, and sampling limits.

## Changes in Tower 4.7.1

- The supplied desktop profile enables GPU sampling for detected running GPU allocations. Use `:gpu on` if an earlier session saved the toggle as off.
- GPU detection reads allocation TRES when the per-node queue field omits a `--gpus` request.
- Analytics finds GPU traces without opening Inspector first and shows sampling errors before the first sample.
- Unknown NVIDIA counters remain gaps. Trace paths preserve spaces, and missing trace files clear stale readings.
- Use `tower --gpu-check` to inspect current jobs. Add `--gpu-check-output DIRECTORY` to save allocation, command, trace, and cache evidence.

See [GPU detection checks](docs/guides/gpu-detection.md) for Fedora instructions and report interpretation.

## Changes in Tower 4.7.0

- Scroll panes with a draggable right-edge scrollbar and header arrows. Advisor, History, Timeline, and comparison documents expose their complete published content.
- Curves use continuous two-by-four Braille strokes. The crosshair follows the latest reported cell immediately and preserves curve and annotation cells.
- Select rendered lines by dragging, or use `v`, navigation keys, and `y`. Raw log copying retains its original source bytes.
- Switch **Copy** to **Yank** in the top toolbar to send text to a running local Vim or Neovim.

See [pane navigation](docs/guides/pane-navigation.md) for controls, copy limits, editor discovery, and fallback results.
See [charts](docs/guides/charts.md) for curve rendering, pointer feedback, and measurement limits.

## Changes in Tower 4.6.2

- Graph drags retain their painted axes while new samples arrive. The capture margin extends three terminal cells beyond the axis labels and ticks.
- Thin selectors use themed Braille positions and 80 ms visual easing. Mouse events still resolve to whole terminal cells.
- Right-click clears job and line selections on the main pages. It preserves the open log source and does not activate another control.

Graph right-click still resets that graph. History-list right-click still opens the log-export menu.
Log Tools right-click clears only its local selection and keeps its dialog open.
Help, Details, and analysis dialogs clear local text or row selections and retain job marks.
See [graph interaction](docs/guides/charts.md#point-at-a-metric-graph) and [selection clearing](docs/guides/pointer-navigation.md#clear-selections-with-right-click) for controls and limits.

## 1. Install on CARC

**Prerequisites:** Connect to your CARC login node.
Use the SSH hostname and account supplied by your institution.
Select a site Python module if `python3` is older than version 3.10.

1. Get the source.

   ```bash
   git clone https://github.com/rahel99x/Slurm-Tower.git
   cd Slurm-Tower
   ```

2. Set up the local launcher.

   ```bash
   python3 scripts/setup.py --mode local
   ```

3. Start Tower with the CARC profile.

   ```bash
   ./scripts/tower --config docs/config.example.json --profile carc
   ```

**Expected result:** Tower opens the Jobs page.
The CARC profile uses slower sampling intervals.
It disables live GPU sampling, queue forecast probes, and allocation queries.
Adapt those settings to your site's operating rules.

Setup creates `.venv` and checks the application with simulated data.
It writes a demonstration report to `.tower/demo.txt`.
Normal setup requires no package downloads or administrator access.
It preserves existing configuration, state, and reports.
See the [runbook](docs/runbook.md) for Python environments and SSH transport.

### Install the `tower` command

Run these commands from the checkout:

```bash
python3 scripts/install_shell.py --apply
source ~/.bashrc
type tower
tower
```

The helper backs up `.bashrc` before it changes the file.
It replaces the old `tower`, `dash`, and `dash2` definitions with one managed alias.
The alias selects this checkout's launcher, the CARC profile, and Unicode graphics.
The launcher uses this checkout's `.venv` when it exists.
An active virtual environment for another project does not change that interpreter.

Use `python3 scripts/install_shell.py` to preview the alias without changing `.bashrc`.
Rerun the helper if you move the checkout.
See [shell setup](docs/runbook.md#use-one-tower-shell-command) for account overrides and backups.

### Update an existing installation

Run these commands from the checkout:

```bash
git pull --ff-only
python3 scripts/setup.py --mode local
tower
```

Resolve local Git changes if the first command fails.
Keep the existing configuration and state directories.
See [update and removal](docs/runbook.md#7-update-or-remove) for the complete procedure.

### Run Tower on a Fedora desktop

Use the desktop profile when Slurm is already installed directly on Fedora.
Run these commands from your existing checkout as your normal desktop user:

```bash
git pull --ff-only
python3 scripts/setup.py --mode local --profile desktop
python3 scripts/install_shell.py --profile desktop --apply
source ~/.bashrc
tower --version
tower --doctor
tower --once --tab sources
tower
```

The desktop profile uses local Slurm commands and normal user detection.
It polls Jobs every five seconds at the default update setting and accounting every ten seconds.
Cluster shows reported local partitions, including idle CPU-only partitions.
GPU sampling is enabled for detected running GPU allocations.
Forecast probes and allocation-budget queries remain disabled.
The desktop alias ignores `CARC_ACCOUNT`.
Use `SLURM_TOWER_ACCOUNT` when you need an explicit desktop account override.
Desktop preferences, bookmarks, tags, and recorded resource samples use separate state from CARC.

An empty queue is normal when you have no jobs.
History requires working Slurm accounting.
Sources reports unavailable accounting; Tower keeps missing results unknown.
See the [Fedora desktop guide](docs/DESKTOP.md) for preflight checks, first installation, and troubleshooting.

## 2. Try Tower without a cluster

```bash
python3 scripts/setup.py --mode demo
./scripts/tower --fake
```

**Expected result:** Tower displays simulated jobs and resource measurements.
Leave it open briefly to collect graph samples.
Press `Tab` to change pages.
Press `?` to open help.
Press `q` to exit.

You can also run `python3 -m tower --fake` directly from the checkout.

## 3. Select a display mode

```bash
tower --unicode
tower --ascii --no-color
tower --fake --unicode --once --tab analytics
```

Unicode graphics are the default on compatible terminals.
Use a UTF-8 locale and a monospace font with block and braille characters.
Tower selects an ASCII fallback when the output encoding cannot display those characters.

Use `--ascii` for portable character graphics.
Use `--no-color` or `NO_COLOR=1` to disable color.
Metric labels and state names remain visible without color.

Press `T` to change the theme.
Use `:theme dark`, `:theme light`, or `:theme terminal` to select a background style.
Use `:theme darcula`, `:theme modnokai`, or `:theme gruvbox-dark` for an editor palette.
`monokai` is an alias for `modnokai`; `gruvbox` is an alias for `gruvbox-dark`.
Theme changes update the canvas, text, menus, information strips, and chart colours during the session.
The `mono`, `high`, `cb`, and `reader` themes provide additional display options.
The `reader` theme uses plain ASCII text and static notices.
Set `animations` to `false` for immediate scrolling and static completion notices.
This setting also disables selector phase animation and suppresses the startup display.
Use View → Disable startup animation or `:startup off` to disable only the welcome.
Use `:startup preview` to inspect it without changing the preference.

## 4. Learn the controls

A leading `:` opens the command palette.
Type the command without that leading character inside the palette.
Uppercase and lowercase keys have different functions.

| Control | Function |
| --- | --- |
| `Tab` / `Shift-Tab` | Open the next or previous page |
| `1`–`9`, `0` | Open a page directly; see the page table below |
| Up / Down | Move the cursor |
| Page Up / Page Down | Move through the visible table or document |
| Home / End | Move to the start or end of the current list |
| `/` | Filter the current list or search the current log |
| `Enter` | Open the selected item |
| `I` | Open the selected job's inspector |
| `l` | Open logs for the selected job |
| `Ctrl-W` / F6 | Change panel focus |
| `Ctrl-B` / Alt-Left | Return to the previous location |
| Alt-Right | Restore the next location after Back |
| `Ctrl-G` | Search jobs, runs, logs, views, workspaces, and commands |
| `Ctrl-P` | Open the Research workspace picker |
| `Ctrl-A` | Open Activity and background task results |
| F10 | Open or close the File, Edit, View, and Help menus |
| F8 | Focus visible buttons and links for directional navigation |
| `:` | Open the editable command palette |
| `?` | Open searchable help |
| `Esc` | Close the current overlay or clear the current selection |
| `q` | Exit Tower |

Click a page label to open that page.
Click a row to select it.
Move the pointer over a control to highlight it without activating it.
Press F8 to traverse visible controls with arrow keys.
Press Enter or Space to activate the focused control, or Esc to return to content.
Use the mouse wheel to scroll.
Use `:smoothscroll off` for immediate wheel movement.
Click a column heading to cycle **ascending → descending → off**.
The first active column has the highest sort priority.
Later columns resolve ties.
`^1` and `v2` show the direction and priority.

JOBID uses numeric order, including array task IDs.
For example, `9` precedes `10`, and `123_2` precedes `123_10`.
Unknown measurements remain last in either direction.
Removing one sort rule preserves the other rules.

The toolbar occupies the first display row.
Click its `x` control to quit.
The top-right update control remains visible above pages and overlays.
Press and drag its track to adjust the fetching interval.
Menu choices remain available while the pointer stays inside the dropdown.
See [Live workbench](docs/guides/live-workbench.md) for every menu choice and slider control.
See [Mouse and button navigation](docs/guides/pointer-navigation.md) for input procedures and terminal support.

```text
:sortby jobs name asc
:sortby jobs cpus desc
:sortby jobs cpus off
:sortby jobs clear
```

See [Controls](docs/CONTROLS.md) for every key, mouse action, command family, and CLI option.
See the [50-improvement guide](docs/QUALITY_OF_LIFE.md) for the new workflows.

### Tower 4.0 operating tools

| Task | Entry controls | Procedure |
| --- | --- | --- |
| Arrange and inspect tables | `:sorteditor`, `:headers`, `:columns`, `:filters`, `:marked`, `:freeze` | [Tables](docs/guides/tables.md) |
| Search complete log sources | `:logsearch`, `:logresults`, `:logolder`, `:loggoto`, `:logmarks` | [Log search](docs/guides/log-search.md) |
| Compare and organize log content | `:logdiff`, `:logalign`, `:logjson`, `:logfold`, `:logunread` | [Log display](docs/guides/log-view.md) |
| Find destinations and change preferences | `:jump`, `:forward`, `:location`, `:settings`, `:keybindings`, `:peek` | [Navigation](docs/guides/navigation.md) |
| Inspect measured intervals and adjust graph scales | `:chart`, `:metricdisplay` | [Charts](docs/guides/charts.md) |
| Review completions and application results | `:inbox`, `:activity`, `:exports`, `:alerts`, `:terminaldoctor` | [Operations](docs/guides/operations.md) |
| Read declared result files | `:outputs`, `:artifact` | [Artifacts](docs/guides/artifacts.md) |

## 5. Select a page

| Key | Page | Purpose |
| --- | --- | --- |
| `1` | Jobs | Inspect active jobs, pending reasons, and recent completions |
| `2` | Cluster | Inspect partition capacity, queue estimates, and account allocation |
| `3` | History | Inspect completed jobs, failures, and resource efficiency |
| `4` | Nodes | Inspect allocated nodes and the cluster resource map |
| `5` | Logs | Read the selected job's exact log sources |
| `6` | Sources | Inspect sampler freshness, latency, errors, and backoff |
| `7` | Analytics | Inspect resource series, outcomes, timelines, advice, and comparisons |
| `8` | Group | Inspect the account's jobs and resource use by user |
| `9` | Dependencies | Inspect prerequisites and dependent jobs |
| `0` | Research | Inspect project measurements, outputs, provenance, and execution plans |

Use Left and Right to change subviews in Analytics, Nodes, and Research.
Research has twelve workspaces:
Experiment, Arrays, Evidence, Artifacts, Passport, Submit, Resources, Forecast,
Blockers, Tradeoffs, Scaling, and Workflow.
Resources uses the `predict` command-line identifier.

The [complete reference](docs/reference.md#tabs) defines each page and metric.
The [Research guide](docs/RESEARCH.md) explains application reports and submission preparation.
The [planning guide](docs/WAVE_TWO.md) explains predictions, uncertainty, scaling, and workflows.

## 6. Work with jobs and logs

### Inspect a completed or failed job

1. Open Jobs or History.
2. Select the job in the active table, Recents, or History.
3. Click Inspector, Logs, Investigate, Research, Analytics, or Quick Advisor in Details.
4. Use arrow keys and Enter to select and activate another visible button.
5. Click its content to scroll, or use `:jobpanel focus` for native panel keyboard controls.
6. Press Esc as needed to return to the job rows.

**Expected result:** The inspector and logs remain attached to the selected job ID.
A missing historical path remains unavailable.
Use a run's `logs.json` index to retain project-owned log locations.

Select Off to hide inspection content.
Use `:jobpanel focus` to reach the buttons without a mouse.
Research provides all twelve workspaces inside Details.
Analytics provides Job series, History, Timeline, Advisor, and Compare.
Choose a subview and scroll its content vertically.
Each inline view fits the available width without horizontal panning.
See [Live workbench](docs/guides/live-workbench.md) for panel navigation and live updates.

For a running job, each metric has a Live button and two adjacent sliders.
Use these controls on Analytics → Job series or Jobs → Details → Analytics → Job series.
Set the first slider's window from 30 seconds to one second.
Live follows a buffered display time and shows measured history within that window.
Running graphs also use that delayed edge when Live is off.
The status reports actual display lag; polling and current resource readouts remain current.
See [Buffered running graphs](docs/guides/charts.md#read-buffered-running-graphs) for startup and outage behavior.
Drag the second slider from five seconds to 500 milliseconds to request reads for that exact job and source, subject to source limits.
**Set** shows that slider's requested interval; **Poll** shows the shared effective interval.
Compact rows show the effective value before the polling track and `S` plus the request after it.
A faster global or companion-metric request can keep **Poll** unchanged while **Set** changes.
Labels use `s`, `ms`, or `µs`; ASCII mode uses `us` for microseconds.
Right-click the window slider to restore 30 seconds, or the sampling slider to restore its five-second request.
Sampling requests also apply while Live is off.
Read the source age and sampling interval when the window is empty.

GPU charts show each observed device's utilization rate and its sampled-mean efficiency proxy.
The proxy is the mean of valid retained busy percentages.
Missing measurements stay unknown.
It does not measure application throughput or FLOP efficiency.
Use the [chart guide](docs/guides/charts.md) for live controls, graph zoom, and GPU evidence.

Drag a grey divider to give either panel more space.
The full vertical divider has a centred blue diamond.
Drag the horizontal divider above Recents to show more recent jobs.
Use its wheel or arrow controls to reach older matching history.
The initial Recents preview size does not prevent further scrolling.

Quick Advisor calculates only after you request it.
Its loading box keeps the display available while a background task examines the selected job's resource history and published evidence.
Read its evidence limits, then use Refresh analysis when newer measurements need a new report.
See [Adjustable workspaces and job advice](docs/guides/adaptive-workspaces.md) for procedures and operating limits.

Analytics, Dependencies, Logs, and Research provide a job-history panel beside their data.
Click a job to inspect its exact charts, sources, or context.
Drag the `⠿` handle to a page edge to use a column or horizontal strip.
Use `:history-dock left|right|top|bottom|auto` when you need keyboard access.
The history choices update from published job records during the session.

Tower recognizes scheduler arrays, explicit launch markers, and ordinary launch bursts with matching provenance.
The same detection applies to available historical records and new queue publications.
Click the right-pointing chevron on the first visible expanded group row to close it.
Click the down-pointing chevron on the summary row to show its jobs again.
The fold preference remains shared across the job pages and their history browsers.
Mark two or more real jobs in the focused list and press `g` to create a closed manual group.
Press `u` on a closed summary to dissolve its group, or on marked expanded members to remove only those members.
Removed members stay outside automatic grouping for that execution attempt. `U` clears marks without changing groups.
Use `:jobgroups off` when you need individual rows.
Collapsed groups retain a real representative job ID; hidden members do not become action targets automatically.
See [Automatic launch groups](docs/guides/batch-launches.md) to identify a batch, read its state counts, and load older records.

The six-cell Progress column appears before JOBID and uses the job's published application progress when available.
It contains a narrow symbol, a space, and four fractional block cells.
Unicode uses `▸` for reported progress and a rotating clock for elapsed time against the Slurm limit.
Pending jobs show an animated hourglass and `wait`.
ASCII uses `p`, `t`, and `w`. Time usage does not report completed work.
Projects can publish `progress.completed` and `progress.total` in their linked `metrics.jsonl`.
See [Job progress](docs/guides/adaptive-workspaces.md#read-the-six-cell-progress-column) for display states, alternate numeric keys, and sorting.

To mark several jobs, click one row, then Shift-click the other end of the range.
Plain click-drag also marks a range, including when it starts on a selected or marked row. Hold Shift during a drag to keep earlier marks.
To add jobs to a group, hold Alt and drag the selected row or a marked row onto that group. The right-click job menu provides the same membership action.
Press Esc during a drag to restore the earlier marks.
Press `c` after the drag to review cancellation of the marked jobs.
Check every job ID before you confirm the action.
Right-click a metric graph to restore its full view and keep the selected job and marks.
Right-click a selected or marked job for group creation, removal, and **Add to** choices.
Right-click elsewhere on a main page to clear job selections, marks, and line selections, subject to History's export control.
That click does not activate the control beneath the pointer.
An open log source remains open, including alternate views and file browsers.
Click a job or line, or use its navigation keys, to select again.
Click a Jobs or Recents row to return arrow navigation from Details to that list.
Press Esc from Details control navigation to return to Main, or use F6 to switch panes.

In History, select the completed or failed job.
Use its Details buttons for the same inspection modes as Jobs.
Press `I` for the separate inspector or `l` for its full Logs page.
In Logs, press `O` for the grouped file list.
Select a file with arrow keys and press Enter.
Press Esc to return to that list.

To collect several completed jobs' outputs, Shift-drag through their History rows.
Right-click a marked row, choose **Export logs**, then choose **Copy all logs to clipboard** or **Copy logs to directory**.
The directory picker can open folders and create a new folder beneath the configured projects root.
Select **Save here** to export, or **Cancel** to return.
Tower checks all registered job outputs and reports each missing source before it publishes an export.
See [Export History logs](docs/guides/log-view.md#export-logs-for-history-jobs) for the complete procedure.

When a job leaves the active queue, Recents shows it as awaiting accounting.
Tower requests a bounded accounting refresh.
History receives the terminal state when Slurm confirms it.
Completion notices do not require an application restart.

### Select and copy log text

1. Move the line cursor with Up or Down.
2. Press `v` to start a selection.
3. Extend the selection with arrows or page keys.
4. Press `y` to copy the original text.

Selected lines show an orange marker on the right.
The ASCII display uses `*` for that marker.
Shift-click extends the line selection. Right-click clears it and keeps the same file open.
In a Log Tools dialog, right-click clears only the dialog's local selection.

Press `Y`, or run `:copy all`, to copy the entire selected log file.
This operation runs in a background worker.
The displayed page, wrapping, and retained tail do not limit the full-file copy.
Tower also writes a private complete export when the clipboard cannot accept the content.
Open Activity to inspect the result or cancel an active copy.

### Select other pane text

Drag across rendered text in Advisor or another document pane.
Alternatively, point at the pane and press `v`.
Extend the selection with navigation keys.
Press `y` to copy it.
`V` selects the currently painted pane. Esc or right-click clears the selection.
Rendered copies contain displayed columns. They exclude neighboring panes, borders, and scrollbar rails.
Job-row dragging keeps its job-marking function unless you start explicit rendered selection with `v`.
Raw Logs retain their original-byte selection controls.

Click **Copy** in the top toolbar to switch to **Yank**.
Tower uses a running local Vim or Neovim server and sets registers `0` and unnamed.
It preserves the complete private export and falls back to clipboard copying when editor delivery is unavailable.
See [pane selection and editor yanking](docs/guides/pane-navigation.md#send-text-to-vim-or-neovim) for prerequisites and limits.

See [log search](docs/guides/log-search.md) and [log display](docs/guides/log-view.md) for paging, search, bookmarks, structured logs, comparisons, and unread lines.

## 7. Add your project

Use the [project reporting standard](docs/PROJECT_STANDARD.md) for portable integration.
Copy the [project template](examples/project-template/README.md) when you start an integration.
The reporter uses the Python standard library.
The standard defines source locations for the Research workspaces and operation manifests.

```text
my-project/
  .tower/                   configuration, contracts, and recipes
  jobs/                     batch scripts
  runs/
    <run_id>/
      run.json              execution identity and declared paths
      logs.json             grouped index of exact log files
      metrics.jsonl         application measurements and progress
      summary.json          final measurements and results
      logs/                 project-owned log files
      outputs/              declared result files
      passports/            immutable provenance records
      reports/              reports for this execution attempt
  reports/                  selected planning observations
```

1. Create a separate run directory for each execution attempt.
2. Record the actual Slurm job ID when it is known.
3. Write measurements to `metrics.jsonl`.
4. Register all required log locations in `logs.json`.
5. Declare result files in an output contract.
6. Run `:project /absolute/project/path` in Tower.
7. Select the execution attempt.
8. Run `:outputs` to inspect its declared results.

**Expected result:** Tower binds that attempt's measurements, logs, outputs, and verified provenance.
Project discovery is bounded and explicit.
It does not recursively scan your source tree.
Local project runs can be inspected without a Slurm job.

Keep each report attached to its execution attempt.
Retain the exact job ID in `run.json` and declare each required source path.
Use the [Research source map](docs/PROJECT_STANDARD.md#files-for-every-research-view) when you instrument a new project.
An unambiguous exact job ID links the selected job to its run's declared reports.
Tower rechecks inventories and source files in the background during operation.
The [live project procedure](docs/guides/live-workbench.md#connect-a-project-to-its-jobs) explains runtime attachment and refresh.

Use the [JSON Schema guide](docs/schemas/README.md) to check interchange files.
Keep generated run directories and private reports out of Git.

## 8. Prepare and review execution

```bash
tower run prepare examples/research/sample.sbatch --workdir "$PWD"
tower --fake --tab research --research-view submit
tower run scaling plan examples/planning/scaling.json --workdir "$PWD"
```

Preparation validates a script without submitting it.
Resource plans report their evidence and uncertainty.
Missing measurements remain unknown.

Use `:preflight SCRIPT --workdir DIR` to edit and check a submission.
Use `:orchestrate workflow FILE --workdir DIR` for a batch workflow review.
Use `:orchestrate scaling FILE --workdir DIR` for a scaling experiment review.

Inspect the exact command, working directory, resource request, and complete target list.
Confirm the review to submit or change jobs.
Tower records actual scheduler IDs and partial or unknown outcomes.
Batch execution requires local cluster operation and writable persistent state.
See [execution and recovery](docs/WORKBENCH.md#execute-a-reviewed-workflow-or-scaling-experiment) before submitting a batch.

## 9. Export and automate

| Method | Command | Result |
| --- | --- | --- |
| Inspect prerequisites | `tower --doctor` | Local environment checks |
| Diagnose missing GPU graphs | `tower --gpu-check` | Current job allocations, selected GPU adapter, traces, and retained samples |
| Inspect a simulated environment | `tower --doctor --fake` | Checks without Slurm |
| Export one page | `tower --once --tab history` | One terminal frame |
| Export a snapshot | `tower --json` | Machine-readable JSON |
| Export a table | `tower --csv --tab history` | CSV table |
| Export the complete dashboard | `tower --report report.txt` | Portable ASCII report |
| Record a session | `tower --record session.jsonl.gz` | Scheduler observations for replay |
| Replay a session | `tower --replay session.jsonl.gz` | Recorded scheduler data |
| Evaluate an expression | `tower --eval 'n_running'` | Expression result |
| Wait for a condition | `tower --wait-for 'n_pending == 0' --timeout 3600` | Status when the condition holds or times out |
| Run a palette command | `tower run COMMAND ARGUMENTS` | One command without the interactive screen |

Use `E`, `C`, or `J` inside Tower to export text, CSV, or job JSON.
Use `:export report` for a complete interactive report export.
Recordings, exports, and logs can contain private job names and paths.
Review a file before you share it.
Use `--fake` when you need public examples.

Scripted job changes require `--yes`.
The [scripted-mode reference](docs/reference.md#scripted-mode-and-expressions) defines expressions and exit codes.
The [operations guide](docs/guides/operations.md) covers export history, completion review, diagnostics, and alert controls.

## 10. Configure Tower

Run `tower --write-config` to create a commented default configuration.
The command preserves an existing file.

Tower searches for configuration in this order:

1. The path supplied with `--config`.
2. The path in `TOWER_CONFIG`.
3. `~/.config/tower/config.toml` or `config.json`.

Python 3.11 and later read TOML.
Use JSON with Python 3.10.
Start with [config.example.json](docs/config.example.json) for cluster profiles.

The normal UI state base is `~/.local/state/tower/`.
The desktop profile selects a separate connection directory with `state_namespace = "desktop"`.
Use `--no-state` to disable normal state reads and writes.
The [configuration reference](docs/reference.md#configuration) describes every section.
The [navigation guide](docs/guides/navigation.md) describes interactive settings and key bindings.

Use the always-visible update control to request a fetching interval.
Its value and endpoints show time units rather than multiplier labels.
The endpoints are five seconds and 500 milliseconds.
Use `:rate N` to select a position from one to 50, or `:rate reset` to restore five seconds.
Position one requests five seconds; position 50 requests 500 milliseconds.
Source minimum intervals and retry backoff remain in effect.
Sources shows the effective intervals.

Live GPU sampling creates short `srun` steps inside an existing allocation.
The compute-node helper needs Python 3 and a supported vendor tool.
It requires allocation evidence before attributing a device to the selected job.
Enable this feature only where cluster policy permits it.
Use `--no-gpu` to disable it for one launch.

## 11. Find a guide

These guides use controlled technical English guided by ASD-STE100 principles.
See [Documentation style](docs/DOCUMENTATION_STYLE.md) for terminology and procedure conventions.

| Task | Guide |
| --- | --- |
| Install, update, or diagnose a CARC launch | [Adaptive runbook](docs/runbook.md) |
| Use an existing Slurm installation on Fedora | [Desktop setup](docs/DESKTOP.md) |
| Find a key, command, CLI option, or mouse action | [Controls](docs/CONTROLS.md) |
| Use the toolbar, inline job panels, and update-rate slider | [Live workbench](docs/guides/live-workbench.md) |
| Use hover feedback, mouse dragging, button focus, and smooth scrolling | [Mouse and button navigation](docs/guides/pointer-navigation.md) |
| Diagnose delayed highlights or long-view scrolling | [Display and input performance](docs/guides/ui-performance.md) |
| Find any of the 50 quality-of-life changes | [Improvement index](docs/QUALITY_OF_LIFE.md) |
| Sort, filter, mark, and inspect table records | [Table guide](docs/guides/tables.md) |
| Group new and historical batches and mark a launch explicitly | [Automatic launch groups](docs/guides/batch-launches.md) |
| Read older log content or search complete files | [Log search guide](docs/guides/log-search.md) |
| Bookmark, compare, and organize log content | [Log display guide](docs/guides/log-view.md) |
| Change navigation, settings, and command input | [Navigation guide](docs/guides/navigation.md) |
| Inspect graph windows, ranges, events, and units | [Chart guide](docs/guides/charts.md) |
| Page through declared output files | [Artifact guide](docs/guides/artifacts.md) |
| Review completions, exports, diagnostics, and alerts | [Operations guide](docs/guides/operations.md) |
| Use all twenty-six research and cluster operations | [Operation catalog, controls, and review rules](docs/guides/operations.md#all-twenty-six-operation-keys) |
| Check storage, pending jobs, arrays, reservations, licenses, and allocation access | [Cluster operations](docs/guides/operations-cluster.md) |
| Verify environments, scientific results, statistics, reuse, and dependencies | [Science operations](docs/guides/operations-science.md) |
| Run checkpoint recovery, searches, packing, Dask, or heterogeneous allocations | [Campaign operations](docs/guides/operations-campaigns.md) |
| Monitor persistently, stage verified files, or inspect native workflows | [Service operations](docs/guides/operations-services.md) |
| Inspect rank bottlenecks, clusters, incidents, and energy | [Scale diagnostics](docs/guides/operations-scale.md) |
| Diagnose missing GPU measurements | [GPU detection checks](docs/guides/gpu-detection.md) |
| Use the original terminal workbench | [Workbench guide](docs/WORKBENCH.md) |
| Instrument projects and define reports | [Project reporting standard](docs/PROJECT_STANDARD.md) ·
[Bash Research automation](docs/guides/research-automation.md) |
| Prepare research and output contracts | [Research workflows](docs/RESEARCH.md) |
| Analyse predictions, scaling, and dependencies | [Planning workflows](docs/WAVE_TWO.md) |
| Adapt the source or add tests | [Contribution guide](CONTRIBUTING.md) |
| Apply the documentation conventions | [Documentation style](docs/DOCUMENTATION_STYLE.md) |

## 12. Develop and adapt

```bash
python3 scripts/setup.py --mode demo --dev --test
.venv/bin/python -m build
```

Development tools are optional dependencies.
Normal operation requires no third-party Python packages.
Tests use simulated Slurm output and temporary files.
They check parsing, job identity, actions, reports, remote transport, and terminal interaction.
GitHub Actions tests supported Python versions and builds installable packages.

To install the Python entry point, create a virtual environment.
Activate that environment.
Run `python -m pip install .` from the checkout.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before you change the source.
Read [SECURITY.md](SECURITY.md) to report a security issue.
You can fork, adapt, redistribute, and use Tower commercially under the [MIT license](LICENSE).
