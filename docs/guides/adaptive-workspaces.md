# Adjustable workspaces and job advice

[README](../../README.md) · [Controls](../CONTROLS.md) · [Tables](tables.md) · [Mouse navigation](pointer-navigation.md)

Configure Raven's panel layout, recent-job browser, launch groups, and job inspection tools.
These controls use the terminal's mouse and keyboard input.
Layout and navigation changes preserve the selected job and do not change scheduler polling.

**Prerequisites:** Run Raven in an interactive terminal.
Divider and graph dragging require mouse press, movement, and release reports.
Each section also describes keyboard alternatives.

## Change panel size

1. Open a page with two visible panels.
2. Move the pointer onto the grey divider or one cell beside it.
3. Press the left mouse button and keep it pressed.
4. Move the pointer toward the panel that needs less space.
5. Release the button at the required size.

**Expected result:** Both panels fit the new space.
Their contents and clickable controls use the new panel dimensions.
A full vertical divider has a blue diamond at its centre.
Short vertical dividers and horizontal dividers use a plain grey line.
ASCII mode uses `|`, `-`, and `*` for these characters.
The colour-free display retains their shapes.

The one-cell capture buffer helps you grab the divider.
An existing control, including a job row or column heading, keeps its own click action inside that buffer.
Use the divider line or a free margin when those controls overlap.
Pressing inside that buffer does not move the divider until you drag.
The divider keeps enough space for both panels.
Columns retain at least 24 cells when the available width permits that layout.
Stacked panels retain at least three rows when the available height permits it.
The existing Main split range remains 20–80 percent.
Very small terminals use the available compact layout.

Press Esc before releasing to restore the previous size.
Changing the page, opening a dialog, or resizing the terminal also ends capture and restores that size.
Release stores a changed size when normal UI state is enabled.
An unchanged click focuses the divider for keyboard adjustment.

### Adjust a divider without dragging

1. Click the divider, or press F8 and activate its visible control with Enter.
2. Use arrows along the divider's movement axis to change its size by two percentage points.
3. Use Page Up or Page Down for ten percentage points.
4. Press Enter or Esc to restore page input.

**Expected result:** The divider moves within its permitted range.
Its focus indicator identifies the control receiving the keys.

Use `:pane-focus KEY` to focus a known divider.
Use `:pane-resize KEY smaller` or `:pane-resize KEY larger` for a command adjustment.
For Jobs Main and Details, the key is `workspace:jobs`; History uses `workspace:history`.
Queue and Recents use `recent:jobs`.
The paired-source Logs view uses `log:sources`.
The history browser on each page uses `history:analytics`, `history:deps`, `history:log`, or `history:research`.
Use `:layout split 60` when you need an exact Main percentage.
Existing `Ctrl-W`, F6, `:focus`, `:maximize`, and saved-layout commands remain available.
With button focus, Right from a Queue or Recents row reaches visible Details controls.
Up and Down keep exact job selection synchronized and admit older rows at viewport edges.
See [Button navigation](pointer-navigation.md#select-controls-with-the-keyboard) for the refreshed focus graph.

In `:logview split`, drag the grey separator to change the two original-source widths.
The default split is 50 percent, with a 20–80 percent range.
Its short divider has no diamond.
The source controls, copy operations, and original bytes retain their existing meaning.

## Show more recent jobs

Jobs Main has independently usable active-job and Recents sections.
The grey horizontal divider controls their relative height.
Its default gives Recents about 35 percent of the available data rows.
Unused active-queue space flows into Recents until you choose a split.
After a divider drag, Raven retains the selected queue height even when that area has few jobs.

1. Open Jobs.
2. Drag the divider between the active jobs and Recents upward.
3. Check that Recents shows more records.
4. Move the pointer into Recents and turn the mouse wheel to reach older jobs.
5. Click an older job to inspect its exact ID in Details.

**Expected result:** A larger Recents section displays more history.
Scrolling reaches older matching records without restarting Raven.
The active queue retains at least one data row when both sections contain jobs.
The Recents split is limited to 10–90 percent of the available data space.

Click a Recents row to give its list keyboard focus.
Clicking a Jobs row also returns keyboard focus from Details to the active list.
When Details controls have focus, press Esc once to return to Main, or use F6 or Ctrl-W to switch panes.
These focus changes preserve marked jobs.
Use Up, Down, Page Up, Page Down, Home, and End to navigate.
Up at the first recent job returns to the last active job.
The wheel stays within Recents instead of switching to the active queue.
Home selects the first matching recent job.
End loads the matching history needed to reach its last job.
The available history remains limited to Raven's current accounting window and filters.

The `:recents 5`, `:recents 10`, and `:recents 25` choices set the initial preview size.
They do not prevent a larger panel or scrolling from showing older matching records.
`:recents auto`, `:recents expand`, and `:recents collapse` retain their presentation controls.
Use `:recents window DURATION` or `:recents window all` to change the completion-time filter.
Use `:days N` or the History range controls when the accounting source must fetch an older interval.
See [Recents controls](tables.md#feature-09) for filtering and sorting.

Raven grows the loaded candidate prefix in bounded pages as you navigate.
It does not scan all accounting history for the default preview.
Newly departed jobs appear as awaiting accounting until Slurm reports their terminal state.
Jobs and Recents retain separate filters and cascading sorts.

## Read metric graphs inside Details

Jobs and History share the Inspector, Logs, Investigate, Research, Analytics, Quick Advisor, and Off modes.
Select a historical row to use these modes with that exact job.
Open **Analytics → Job series** or an available **Research** metric view for the selected job.
Point inside a plot for a thin Braille crosshair in the active theme's accent color.
ASCII and reader modes use static dots and `+` intersections.
Drag across a time interval and release inside that plot to fit its horizontal extent and visible curve.
Hold Shift before pressing to set explicit horizontal and vertical bounds instead.
Selected intervals use adaptive `s`, `ms`, or `us` axis labels and identify their start timestamp.
After pressing inside the plot, the capture margin includes the axis labels and tick row.
It extends three terminal cells beyond them, clipped to the actual visible pane.
Movement and release in that margin use the nearest plot edge. Moving beyond it cancels the preview.
Both painted axes stay fixed during the drag while the sampler continues to publish new data.
Use `u` while pointing at the plot to undo, or `0` to reset rectangular zoom.
Right-click inside the plot to restore its full view and turn off that metric's Live window.
The graph keeps the exact selected job and source.

A running metric can show its own Live toggle and a window slider from 30 seconds to one second.
Its adjacent polling slider requests intervals from five seconds to 500 milliseconds.
**Set** identifies that slider's request; **Poll** identifies the shared effective interval.
Compact rows put the effective value before the polling track and `S` plus the requested value after it.
The requested label changes even if shared requests or collection floors keep the effective interval unchanged.
Managed launches apply the [collection policy](../raven/README.md#collection-and-retention) before reading sources.
Live and the window slider change the displayed interval. The polling slider changes source-read requests.
A valid rectangular zoom turns Live off.
GPU utilisation and observed busy mean use recorded device observations and retain missing-data gaps.
The busy mean indicates activity; it does not measure computation efficiency.
See [Charts](charts.md) for scale controls, Live procedures, safe cancellation, and GPU source limits.

## Fold related launches

Raven groups jobs when the published evidence supports a shared launch.
Grouping is enabled by default, and new groups start open.
The fold setting is shared by Jobs, Recents, History, Group, Dependencies, and the job-history browsers.

1. Find the right-pointing chevron on the first visible row of an expanded launch group.
2. Click it to close the group. Click the down-pointing chevron on its summary row to open it again.
3. Select a member and press Left to close the group or Right to open it when ordinary job-row navigation owns the keys.
4. Open another job page and check the same group's shared fold setting.

**Expected result:** Closing a group leaves one real representative job row.
Opening it restores its matching jobs in the current sort and filter order.
The representative ID remains a real scheduler allocation.
In scheduler job lists, closing a group moves a hidden selected child to the visible representative.
A data view opened for an exact historical job can stay on that job while its browser group is folded.
Check that displayed ID before inspecting or acting on a job.

Use `:jobgroups off` to show individual jobs without grouping.
Use `:jobgroups on` to restore grouping, or `:jobgroups` to toggle it.
Use `:jobgroup open GROUP_ID`, `:jobgroup close GROUP_ID`, or `:jobgroup toggle GROUP_ID` for an exact known group.
A simple unscoped array also accepts its numeric parent ID in this command.
Normal UI state retains grouping and closed-group preferences.

### Interpret grouping evidence

| Evidence | Group basis |
| --- | --- |
| Slurm array task IDs | Shared parent allocation; the task suffix identifies each real child |
| Slurm heterogeneous component IDs | Shared parent allocation; the component suffix identifies each real child |
| One explicit launch marker | Matching `launch:ID` or `group:ID` scheduler comments or local job tags, or a published `RavenLaunchId`, `LaunchId`, `LaunchGroup`, or `JobGroup` scheduler detail |
| Dependency-connected launch | Matching owner, account, and WorkDir with direct dependency links; at most 60 seconds and a numeric ID span of 256 |
| At least three ordinary jobs | Matching owner, account, WorkDir, and name family within 30 seconds; bounded gaps and density in numeric job IDs; no conflicting known script paths |
| Two ordinary jobs | Adjacent numeric IDs within ten seconds; matching nonempty owner, account, WorkDir, exact Command, and name |

Explicit markers are scoped by cluster, owner, account, and work directory.
Use a unique marker for each real launch.
For example, `:tag 101 102 launch:study-17` can mark two available jobs from one launch.
Conflicting local marker tags do not establish one reliable local marker.
Missing provenance and names alone do not establish an ordinary launch.
Step IDs do not establish independent launch members.
Likely groups describe matching evidence; they do not establish equal experimental inputs.
See [Automatic launch groups](batch-launches.md) for exact deduction limits, scheduler-comment examples, metadata precedence, and historical coverage.

Closed groups show themed INFO badges for `run`, `pend`, `dep`, `never`, `done`, `fail`, `cancel`, and `other`.
Only nonzero categories appear, and each matching observed record belongs to one category.
`pend` excludes dependency waits and `DependencyNeverSatisfied`; those appear as `dep` and `never`.
Compressed array rows count as records rather than expanded task states.
The summary updates with published state changes even when launch membership stays the same.

No new `run.json` grouping field is required.
Projects should continue to publish each task's exact job ID and separate run attempt.
Use the existing [project reporting standard](../PROJECT_STANDARD.md) for those files.
Automatic groups update when queue, departed-job, accounting, or metadata publications change.
Older metadata can add evidence without requiring a restart.
A later submit time for a reused job ID establishes a different inferred attempt.

### Keep actions exact

Grouping changes presentation.
It does not mark every hidden member for cancellation or another job action.
Select and mark the actual job IDs you need.
Drag ranges follow the displayed real rows and do not add hidden children.
Review every target ID before confirming a bulk action.
Dependencies retains its existing chain-action rules and reviews.
Filtering does not reintroduce excluded jobs when a group opens.
Expanded group information distinguishes matching members from all observed members.
Closed INFO badges count the matching members in that list, subject to filters and the accounting window.

## Keep job history beside the data

Analytics, Dependencies, Logs, and Research provide a second panel of available jobs.
It includes active, awaiting-accounting, completed, and failed records from the current snapshot.
Choose a job there to inspect its data in the larger content panel.

1. Open Analytics, Dependencies, Logs, or Research.
2. Click a job in Job history.
3. Check the job ID in the content heading or source path.
4. Scroll the history panel to select an older record.
5. Use Dock to cycle its position, or drag its handle to a page edge.

**Expected result:** The content uses the exact selected job.
Analytics opens that job's resource series.
Logs opens that job's own registered or scheduler log paths.
Research selects that job's project evidence.
Dependencies selects that job for the dependency context.
Unavailable historical data remains unavailable.
Raven does not substitute a currently running job.

### Move the history panel

1. Press the `⠿` handle at the start of the Job history heading.
2. Keep the left mouse button pressed.
3. Move the pointer near the left, right, top, or bottom edge of the content area.
4. Check the highlighted docking preview.
5. Release to place the panel at that edge.

**Expected result:** Left and right create columns.
Top and bottom create a horizontal strip of job choices.
ASCII mode uses `::` for the handle.
Dropping in the centre or outside the page leaves the original position.
Esc, a page or dialog change, and terminal resize end capture without applying a new dock.

Drag the divider between history and content to adjust their sizes.
Click Dock to cycle right, bottom, left, and top.
In automatic mode, the first click advances from its current visible position.
Use `:history-dock left`, `:history-dock right`, `:history-dock top`, or `:history-dock bottom` for an exact position.
Use `:history-dock auto` to select a column on wide terminals and a strip on narrower terminals.
Automatic columns require at least 110 columns and 12 content rows.
A requested column falls back to a strip below 56 columns or eight content rows.
The history share is limited to 15–55 percent.
Normal UI state retains each page's chosen position, size, and selected identity.

Click its `x`, use `:history-browser off`, or enter `:history-dock off` to hide the panel.
A small Job history control remains available to show it again.
Use `:history-browser on` for the command equivalent.
Hiding the browser retains the page's selected job and content.

### Select history without a mouse

Use `:history-focus` to give the list keyboard focus.
This command also restores a hidden browser.
Up and Down select and activate a job.
Page Up, Page Down, Home, and End move by the available history choices.
Press Esc to return keys to the data view.
Use `:history-job JOBID` to select an exact available job directly.
Use `:history-scroll up`, `down`, `page-up`, `page-down`, `home`, or `end` to change the list viewport without activating another job.

The browser deduplicates exact job IDs and uses the active record when it is still in the queue.
It orders available records by their reported submission, start, or end time, then by numeric job ID.
Runtime publications update its records without a restart.
Available accounting history still depends on the fetched accounting window.
The shared launch-group controls also apply inside each history browser.

## Request Quick Advisor

Quick Advisor summarizes the selected job's allocation and measured resource behaviour.
It uses the available observations across the job, rather than treating its latest sample as the whole run.
It does not apply recommendations to Slurm.

1. Open Jobs and select an active or recent job.
2. Click Quick Advisor in Details, or enter `:jobpanel quick`.
3. Wait for the loading box to finish.
4. Read the allocation, CPU, memory, GPU, and timing findings.
5. Check the evidence coverage and unavailable measurements.
6. Select Refresh analysis when newer observations need a new report.

**Expected result:** The report stays attached to the requested job ID.
The loading box leaves pointer feedback, navigation, and background publication available.
Analysis begins only when you request it.
Opening Jobs or restoring a saved Quick Advisor mode does not start analysis.
Use Analyze this job in an idle restored panel to request it.

Use Cancel to stop displaying an unfinished request.
Selecting another job or leaving Quick Advisor also discards that request's later result.
An old result cannot replace the panel for a different job or run attempt.
Refresh analysis captures the newer published evidence explicitly.
It does not start repeated analysis on every mouse move or scheduled display refresh.

### Interpret the report

| Evidence | Use |
| --- | --- |
| Exact job record and scheduler fields | Identify allocated CPUs, memory, GPUs, limits, state, and reported constraints |
| Live values and retained CPU, RSS, and GPU observations | Summarize measured levels, peaks, variation, and covered intervals |
| Scheduler steps and final accounting | Compare available task evidence with the job record |
| Compatible same-name history | Add observed context for related runs; it is not proof that a different workload will behave the same |
| Job tags and available inspection evidence | Preserve contextual limitations in the findings |

Slurm MaxRSS is a task-scoped peak.
Quick Advisor does not present that value as total job memory or infer aggregate memory headroom from it.
Missing samples, unavailable steps, gaps, and unreported limits remain unknown.
A partial run can produce a partial recommendation.
The report identifies the observed interval and source coverage.
Live batch or first-step CPU accounting can omit other steps, tasks, or nodes.
Quick Advisor identifies that scope and withholds CPU-count reductions when aggregate use is not established.
Review those limits before you change a job script.

Analysis reads at most 10,000 resource observations, an 8 MiB persistent-series tail, 256 compatible historical runs, and 4,096 GPU-trace entries.
Persistent data reads and the calculation run outside the display thread.
The report identifies truncation when these bounds exclude evidence.
Source files and Slurm records continue to use their normal fetching intervals.
Use the update slider separately when you need a different source rate.

## Read the six-cell progress column

Jobs places its six-cell Progress column before JOBID.
The fixed marking and group-fold gutter remains separate to its left.
It uses published data and starts no file read or scheduler command while rendering.

| Display | Meaning |
| --- | --- |
| `▸`, a space, and four fractional block cells | Application-reported completion fraction |
| `p` and four ASCII bar cells | The same application-reported fraction |
| A clock, a space, and four fractional block cells | Elapsed time as a fraction of the Slurm time limit |
| `t` and four ASCII bar cells | The same time-limit usage fraction |
| An hourglass and `wait`, or `w wait` in ASCII | The job is pending |
| `--` | Neither application progress nor a usable time limit is available |

For example, `▸ █▋░░` and `◷ █▋░░` each use six cells for about 42 percent.
The space after the symbol separates it from the bar.
ASCII uses `p =>--` and `t =>--` respectively.
The underlying fraction remains available for numeric sorting.
When animation is enabled, a running time symbol cycles through `◴`, `◷`, `◶`, and `◵` every 0.4 seconds.
A pending symbol alternates between `⧗` and `⧖` every 0.8 seconds.
These are single-cell characters; Raven does not use double-width emoji in this column.
ASCII, reader mode, disabled animations, and noninteractive output use static symbols.
The clock or `t` value measures used allocation time.
It does not measure completed work or predict when the application will finish.
A completed job still needs valid application progress to show application completion.
Missing progress remains unknown rather than being assumed to be 100 percent.

Publish the standard top-level progress object in a linked job's `metrics.jsonl`:

```json
{"t":1791115200.25,"step":12,"metrics":{"loss":0.032},"progress":{"completed":12,"total":100,"unit":"steps"}}
```

Use finite numbers, `0 <= completed <= total`, and `total > 0`.
For integrations that already use numeric metric names, Raven also accepts `progress_fraction` from zero to one, `progress_pct` from zero to one hundred, or the pair `completed_steps` and `total_steps` with the same completed/total rules.
Keep those names stable across records.
The [metric-stream standard](../PROJECT_STANDARD.md) describes the file identity, fields, and bounds.

The progress source must match the exact job, run, and published generation.
Another job's linked file cannot supply its progress.
New valid publications update the column at runtime.
The Jobs tab refreshes linked streams for visible Main rows through the existing idle background reader.
The reader uses discovered standard project inventories and rejects ambiguous job attempts.
It reads at most four reports in a cycle and rotates through the remaining visible jobs.
Each report read uses a confined tail of at most 64 KiB.
The inventory lookup examines at most 256 runs, and the scalar progress cache holds at most 128 entries.
Its base interval is eight seconds, subject to the global update speed ratio and file-reader minimum intervals.
An explicit Quick Advisor or project task takes priority.
Use `:columns jobs` to hide other optional fields, or enlarge Main when the terminal has insufficient width.
Click the column heading for its cascading sort, or use `:sortby jobs progress asc`, `desc`, or `off`.
Sorting uses the underlying numeric fraction.

## Troubleshoot workspace controls

| Symptom | Action |
| --- | --- |
| A divider click works, but dragging does not | Check mouse movement and release reports with `:terminaltest`; use divider focus or `:layout split` |
| The divider stops before the requested edge | Keep enough space for both panels; use maximize for a single-panel view |
| Older jobs do not appear in Recents | Check the Recents time filter and accounting window; inspect Sources for unavailable accounting |
| The loaded Quick Advisor panel is idle | Select Analyze this job; restoring a mode does not start analysis |
| A history column becomes a strip | Increase the terminal size or keep the compact fallback |
| Job history is hidden | Select its remaining show control or use `:history-browser on` |
| A historical job has no chart, report, or log | Check its exact source and source coverage; missing data remains unavailable |
| Quick Advisor omits a resource recommendation | Check evidence coverage; unavailable samples remain unknown |
| A newer sample does not change a finished Quick Advisor report | Select Refresh analysis to request a new calculation |
| Progress shows `◷` or `t` | Publish valid application progress for the exact job; the clock and `t` report time-limit usage |

Use [Display and input performance](ui-performance.md) for checks and connection limits.
Use [Controls](../CONTROLS.md) for the complete command syntax.

## Implementation history

Version 4.4.0 introduced adjustable dividers, scrollable Recents, launch groups,
history panels, Quick Advisor, and the compact job-progress column.
Version 4.5.0 added refreshed job-row focus, source-specific graph zoom,
runtime palettes, and the leftmost progress field.
