# Live terminal workbench

[README](../../README.md) · [Controls](../CONTROLS.md) · [Project standard](../PROJECT_STANDARD.md)

Inspect job evidence and project reports through Raven's toolbar and Details panel.
This guide covers project attachment, inline inspection, menus, and polling controls.
Check the displayed job ID and source path before interpreting evidence or starting an action.

## Connect a project to its jobs

Use the [project reporting standard](../PROJECT_STANDARD.md) when you create a new project.
It defines the directory layout, job identity, and source location for every Research workspace.
Use the [copyable reporter](../../examples/project-template/README.md) to write compatible inventories and measurements.

Automatic attachment uses the selected job's cached scheduler WorkDir.
Set that directory to the project root or a directory beneath `PROJECT/runs/<run_id>/`.
Register a root with `:project PROJECT` when the scheduler cannot supply that path.
Raven performs bounded project discovery through the selected file connection.
Local connections read local project paths. Supported SSH connections read paths on the selected host and require remote `python3`.

1. Create one directory at `runs/<run_id>/` for each execution attempt.
2. Write `run.json` with the run ID and actual scheduler job ID.
3. Append application measurements to `metrics.jsonl`.
4. Register required log files in `logs.json`.
5. Write final results to `summary.json`.
6. Declare any additional Research reports in that run's source paths.
7. Start Raven with the connection that provides access to the project files.
8. Open the project with `:project /absolute/project/path`.
9. Select the job in Jobs, Recents, or History.
10. Open Research and select the required workspace.

**Expected result:** An unambiguous exact job ID links the attempt's reports to the selected job.
The source paths identify that execution attempt.
Changing jobs updates the attachment without restarting Raven.

Use the same steps for local and supported SSH connections.
On CARC, use an SSH profile or run Raven on the login node that can access the project paths.
For a local Slurm scheduler, use a Slurm site with local transport.
An absolute path identifies a file on the host associated with the selected file connection.

The twelve source routes cover Experiment, Arrays, Evidence, Artifacts, Passport, Submit,
Resources, Forecast, Blockers, Tradeoffs, Scaling, and Workflow.
Resources uses the `predict` command-line identifier.
The [per-view source table](../PROJECT_STANDARD.md#files-for-every-research-view) gives every exact location and required field.
See the [Research guide](../RESEARCH.md) for measurements, provenance, contracts, and submission preparation.
See the [planning guide](../WAVE_TWO.md) for evidence requirements and interpretation limits.

Keep measurements factual.
An unavailable report remains unavailable.
Resource utilization does not establish an application's scientific result.

### Publish reports while the job runs

Use one metrics stream per execution attempt.
Append only complete JSONL records.
Publish replacement JSON reports atomically after the complete file is ready.
The copied reporter can publish and declare a native per-view source:

```bash
python3 reporting.py report workflow .raven/definitions/workflow.json --run runs/my-run
```

Run that command from the copied project's root.
Replace the view, source file, and run directory with the actual values.
The reporter validates the source and records the destination in the run inventory.
See [Report publication](../PROJECT_STANDARD.md#publish-a-per-view-source-without-a-raven-dependency) for all supported views.

Raven rechecks inventories and bound source files in its background worker.
The normal Research interval is five seconds before the global speed ratio is applied.
Late-created metrics, log indexes, contracts, planning reports, and verified passports can appear during the session.
Atomic replacements update subsequent reads.
The reader keeps the current page and selection under your control.
It does not run project code, generate an aggregate, or finalize an application's outcome.

### Resolve an attachment conflict

An array task ID matches only that task.
For example, `12345_7` does not match `12345` or `12345_8`.
Multiple attempts with one requeued job ID require explicit selection.
Invalid inventories or capped discovery coverage prevent a guessed attachment.

1. Open `:runs`.
2. Inspect the inventory notices with `!`.
3. Select the correct attempt, or use `:run select RUN_ID`.
4. Check the job ID and source paths before reading its reports.

**Expected result:** Your selected attempt remains attached until you select a different job.
Clicking a run row also binds that exact attempt.
Use `:run clear` to restore the earlier report settings.
The `:metrics` and `:artifacts` commands also leave the run binding.
These actions suppress automatic attachment for the current job until the job selection changes.
See [Automatic attachment](../PROJECT_STANDARD.md#automatic-attachment-and-runtime-updates) for discovery and path limits.

A captured Submit report is read-only evidence.
Viewing it does not prepare or submit a scheduler job.
Use the existing submission procedure and review when you want to run a script.

## Inspect a job inside Jobs

Select an active job or a job in Recents.
History provides the same Details controls for its selected accounting record.
The Details panel provides seven buttons:

| Button | Purpose |
| --- | --- |
| Inspector | Inspect job identity, requests, resources, steps, paths, and source status |
| Logs | Read the exact job's available log sources |
| Investigate | Inspect bounded failure evidence and its source coverage |
| Research | Inspect all thirteen Research workspaces inside the current job page |
| Analytics | Inspect Job series, History, Timeline, Advisor, and Compare inside the current job page |
| Quick Advisor | Request a summary of allocation and measured resource behaviour for the exact job |
| Off | Hide inspection content |

The selected job ID remains the evidence target.
Selecting another job updates the panel for that job.
Off retains the button row so that you can restore an inspection mode.

### Use the buttons

1. Click a Details button.
2. Use arrow keys to focus a nearby visible control.
3. Press Enter or Space to activate that control.
4. Click the content area to restore content navigation.
5. Use Up, Down, or page keys to scroll the content.
6. Press Esc to return keyboard input to the job rows.

**Expected result:** The selected inspection stays inside Jobs or History.
The selected active, recent, or historical job remains the target.
Mouse clicks use the same visible-control graph as F8 navigation.
Its arrows change focus without activating the destination.
Press Esc or F8 to leave graph focus before using native panel keys.

Use `:jobpanel focus` to reach the buttons without a mouse.
In native panel focus, arrows select and activate the previous or next Details mode.
Home selects Inspector; End selects Off.
Enter or Tab moves from the mode buttons to content.
Research and Analytics include a subview-button stop before content.
In that stop, arrows select a subview; Home and End select its first or last choice.
Enter or Tab returns from content to the mode buttons.
Use `:jobpanel inspector`, `:jobpanel logs`, `:jobpanel investigate`, or `:jobpanel off` to select a mode directly.
Use `:jobpanel research VIEW` or `:jobpanel analytics VIEW` to select an inline workspace directly.
Use `:jobpanel quick` to request Quick Advisor.
Restoring that saved mode leaves it idle until you select Analyze this job.
The inspection mode persists when normal state is enabled.
Jobs and History retain separate mode, Research subview, and Analytics subview preferences.
Button focus and the panel's selected log file last for the current session.

### Inspect a historical job

1. Open History and select an available job record.
2. Select Inspector, Logs, Investigate, Research, Analytics, or Quick Advisor in Details.
3. Check the job ID in the panel heading.
4. Use the wheel or focused navigation keys to scroll its content.
5. Select Off to hide the content, or another mode to change the evidence view.

**Expected result:** History keeps the selected job beside the same inspection tools as Jobs.
The mode and subview rows remain available while long content scrolls.
Drag its divider to adjust the split, or maximize the focused pane.
Narrow terminals stack Main and Details vertically.
Missing historical files or measurements remain unavailable; an active job does not supply replacement data.
Completed jobs do not receive running-metric Live or polling controls.
If the exact selected job is still running, timestamped metric graphs use the same buffered display as Jobs and Analytics.
The graph status identifies its display lag; the current job values and source polling remain current.
See [Buffered running graphs](charts.md#read-buffered-running-graphs) for startup and source-outage behavior.
Quick Advisor still requires explicit activation and reports its evidence limits.

Right-click a selected or marked History row and choose **Export logs** to open export for those exact job IDs.
Right-click elsewhere inside that list retains the direct export control.
Right-click in Details to clear local selections; right-click on a graph retains its graph-reset action.
Use the [History export procedure](log-view.md#export-logs-for-history-jobs) for complete log bundles.

### Read multiple log sources

1. Select Logs in Details.
2. Click log content, or use native panel focus and press Enter.
3. Use Left or Right to select a source.
4. Use Up, Down, or page keys to scroll the retained content.
5. Press End to follow newly appended lines.

**Expected result:** The source path identifies the exact selected file.
The source buttons show the selected file and nearby choices.
Left and Right can traverse every available file.
Home reaches the oldest retained line; it does not load the complete file.
Clicking a source button selects that file.
Click the log content or leave graph focus before using its native scroll keys.
Inline Logs retains a bounded tail of up to 256 KiB per loaded file.

Use `l` after returning focus to the job rows to open the full Logs page.
That page provides complete-file search, copying, older pages, bookmarks, and the grouped file picker.
See [Log search](log-search.md) and [Log display](log-view.md) for those operations.

### Interpret inspection evidence

Inspector shows Overview, Resources, Steps, Files, and Evidence from the current snapshots.
The structured inspector opened with `I` provides clickable section buttons.
Use `:inspect section SECTION` in an open structured inspector for the equivalent command.
In inline Inspector, click StdOut or StdErr to open that exact log source.
Its log-browser link opens the complete source catalog.
Its Evidence link opens Research Evidence inside Details.
Investigate shows bounded multi-file findings, hypotheses, and inspected-source coverage.
Click a cited log path to open its exact source on the full Logs page.
A hypothesis is an explanation supported by cited evidence, not proof of the application's cause.
Readers update in the background as job records and files change.

The wide Jobs and History layouts place Details beside their tables.
Narrow terminals stack the panels.
Use `Ctrl-W` or F6 for panel focus and `z` to maximize the focused panel.
Off suppresses selected-job content and starts no new log or evidence reads.
Drag the divider to change the panel sizes.
Use its keyboard focus when mouse drag reports are unavailable.
The horizontal divider above Recents controls how much matching history is visible.
See [Adjustable workspaces and job advice](adaptive-workspaces.md) for divider, Recents, grouping, docking, and Quick Advisor procedures.

### Inspect Research and Analytics without changing pages

1. Select an active or recent job in Jobs, or a record in History.
2. Click Research or Analytics in Details.
3. Click the required subview button.
4. Move the pointer into the content and use the wheel to scroll.
5. Use arrow or page keys when the content has keyboard focus.
6. Select another subview to inspect its reports.
7. Select Off to hide inline content.

**Expected result:** The selected job remains visible beside its reports.
The view fits the Details width without horizontal panning.
Long content scrolls vertically beneath the button rows.
Each subview retains its own vertical position.
Each rendered inline document remains bounded to 2,048 source rows and 2,048 fitted display rows.
Metric dashboards rasterize visible cards as you scroll.

| Group | Inline subviews |
| --- | --- |
| Research | Experiment, Arrays, Evidence, Artifacts, Passport, Submit, Resources, Forecast, Blockers, Tradeoffs, Scaling, Workflow, Operations |
| Analytics | Job series, History, Timeline, Advisor, Compare |

Use `predict` as the Resources command identifier.
Use `job` as the Job series command identifier.
For example, `:jobpanel research workflow` opens Workflow inside Details.
Use `:jobpanel analytics job` for the selected job's recorded series.

Research uses the exact selected job and its bound project reports.
Its twelve report-backed workspaces share the project source routes; Operations provides the operation catalog.
Job series keeps that exact job ID even when no recorded samples are available.
Compare includes the selected job and the available explicit comparison or marked jobs.
History, Timeline, and Advisor keep their accounting-window context.
Check the selected-job header and the reported window when reading aggregate Analytics results.

The full Research and Analytics pages keep their own selections and view preferences.
Opening an inline view does not replace those pages' positions.
Click an explicit chart or cited-source control when you need its detailed inspection.
Click a Chart Events or Timeline row to open the exact displayed job or log citation.
Use `:timeline open EVENT_NUMBER` for a numbered current timeline event.
Use the opened dialog's Back or Esc control to return.
Research reports remain evidence; a captured Submit report does not submit a job.
Click a declared output row to expand its directory or open its bounded preview.
In a CSV preview, click a column button to focus it, then press Enter to cycle its sort direction.
Use `:artifact column NAME_OR_NUMBER` for column focus by command.

### Mark a job range with the mouse

1. Press the left mouse button on a visible job row. Hold Shift if you need to retain earlier marks.
2. Keep the button pressed and drag through the required rows.
3. Release the button to complete the marked range.
4. Press `c` to open cancellation review when cancellation is the required action.
5. Inspect every target ID and confirm only the intended jobs.

**Expected result:** The marked range follows the table's current visible order.
Hold Shift during the drag to retain existing marks and add the new range.
Plain drag selects a range even when the starting row is already selected or marked.
Click one row, then Shift-click another for an inclusive range in either direction.
Hold Alt during a drag to add the selected job or applicable marked jobs to an existing group.
See [Add jobs to a group](batch-launches.md#add-jobs-to-a-group) for destination controls.
Press Esc before release to cancel capture and restore the earlier marks.
A simple click retains ordinary row selection.
Marking jobs does not change their scheduler state.
Supported actions retain their existing review and confirmation.
See [Mouse and button navigation](pointer-navigation.md) for mouse support and directional focus.

## Set the update rate

The top-right update control shows the effective Jobs polling interval.
Its value and endpoints use `s`, `ms`, or `µs`; ASCII mode uses `us` for microseconds.
Move right toward 500 milliseconds. Move left toward five seconds.
The scale is logarithmic and uses positions one to 50 internally.
These positions select intervals; they do not act as frequency multipliers.
It changes fetching intervals without changing recorded timestamps or metric values.

1. Click the slider track to choose a polling request.
2. Keep the left mouse button pressed and drag horizontally to adjust the rate.
3. Click `[-]` or `[+]` to move one position.
4. Use the mouse wheel over the control for one-step changes.
5. Click the interval label to focus keyboard controls.
6. Use arrows or `-` / `+` to adjust the request.
7. Press Home for five seconds or End for 500 milliseconds.
8. Press Esc to return input to the current page.

**Expected result:** The requested position updates during the session.
The interval label shows the effective Jobs interval after collection floors and configured intervals are applied.
View → Focus update-rate slider provides keyboard entry.
Enter or Tab also releases slider focus.
Release the mouse button to finish dragging.
The pointer position stays inside the track.
Use a track click or `:rate N` if the terminal cannot report drag events.

| Command | Function |
| --- | --- |
| `:rate` | Show the effective Jobs polling interval |
| `:rate N` | Select a polling position from 1 to 50; position 1 requests five seconds and position 50 requests 500 milliseconds |
| `:rate reset` | Restore the five-second slider request; collection policy still applies |

For example, `:rate 50` selects the 500-millisecond slider request.
Raven applies configured intervals, collection floors, site policy, demand gating, and command budgets before collection.
The default queue and live-resource floors are 60 seconds, so selecting this position does not establish 500-millisecond collection.
See [collection policy](../raven/README.md#collection-and-retention) for the defaults and scope.
Other sources and supported file readers use the resulting speed ratio: five seconds divided by the requested interval.
That ratio spans one to ten and adjusts their configured bases, subject to source limits.
Raven keeps each configured base unchanged, so repeated adjustments do not compound.

### Read the limits

| Source | Reader minimum before managed collection policy |
| --- | --- |
| Ordinary Slurm and plugin sources | 0.5 seconds |
| GPU sampling inside an allocation | 0.5 seconds |
| Queue forecast probes and allocation-budget reads | 30 seconds |
| Local file readers | 0.5 seconds |
| Remote file readers | 1.5 seconds |

The slider's default position requests five seconds.
Managed launches retain configured collection intervals, including values slower than that request.
The table describes underlying reader bounds; managed collection floors can be longer.
Source errors retain their existing retry backoff.
A shorter requested interval does not start overlapping reads or increase the worker count.
Disabled sources remain disabled.

Sources shows effective fetching intervals.
Use that page to verify the actual cadence.
Every polling position selects a distinct request between five seconds and 500 milliseconds.
Several positions can produce the same effective interval when a collection floor applies.
Remote file limits, expensive-source limits, and retry backoff can delay those sources further.
The preference persists with normal UI state and follows the selected state namespace.
Use the top-level `polling_multiplier` configuration field to set a launch default.
A valid saved position overrides that default.
The configuration field keeps its existing name for compatibility and now stores a position from one to 50.
Use `raven --rate N` to override the saved position at launch.
Use `raven --interval SECONDS` to set a compatibility launch interval.
Intervals slower than five seconds retain their configured cadence instead of becoming a faster slider request.
An explicit `--rate N` takes precedence over `--interval`.
Use `:rate reset` to restore the default request in the active session.
Settings → Polling interval changes the same native request.
Cancelling its preview restores the exact previous position; accepting the draft saves it.
Compatibility `intervals.jobs`, `intervals.live`, `intervals.gpu`, and `intervals.trace` values remain inputs to the requested cadence.
Managed Raven configuration uses the `collection` settings and applies policy before collection.
See [Settings](navigation.md#change-settings-with-a-preview) for compatibility and preview controls.

### Set one metric's interval

A running metric has a display-window slider and a separate polling slider.
Use them on Analytics → Job series or Jobs → Details → Analytics → Job series.
The first sets a window from 30 seconds to one second.
The second requests reads from five seconds to 500 milliseconds for that exact job and source.
Both use logarithmic scales. Numeric metric polling commands use positions one to 100.
**Set** shows that slider's requested interval; **Poll** shows the shared effective interval.
Compact rows put the effective value before the polling track and `S` plus the requested value after it.
Moving the slider updates its requested value even when a faster global or companion request keeps the effective interval unchanged.

Right-click the first slider to restore 30 seconds.
Right-click the second to restore its five-second polling request.
These resets preserve the other slider, Live state, graph zoom, and job selection.
Sampling requests apply while Live is off and can remain active after the graph moves offscreen.
They reset when Raven restarts or the exact job attempt ends.

The fastest global or per-metric request sets the shared interval. Requests do not multiply.
CPU and memory share one job probe. GPU curves share another.
Other jobs retain their global cadence and their own requests.
CPU, memory, GPU, and local application-file readers retain a half-second lower bound before managed collection policy.
Remote application-file reads retain a 1.5-second minimum.
Managed collection floors, demand gating, command budgets, command duration, and retry backoff still apply.

The sampling control changes how often Raven reads an application metrics file or CSV trace.
It cannot increase how often the producing job writes that file.
Read the effective polling interval and source age below the graph before interpreting an empty window.
See [Charts](charts.md#follow-a-running-metric) for commands, keyboard controls, and limits.

## Use the terminal toolbar

The first display row contains File, Edit, View, Help, copy and worker switches, and a quit control.
Use the menus for file workflows, preferences, navigation, and diagnostics.
Job-changing actions retain their existing review and confirmation.
The worker switch changes background concurrency without restarting Raven.
See [Background workers](background-workers.md) for Single, Multi, and pending changes.

### Open and traverse a menu

1. Click a menu label or press F10.
2. Use Left or Right to select another menu.
3. Use Up, Down, or page keys to select a choice.
4. Press Enter or Space to activate the selected choice.
5. Press Esc to close the menu without activating a choice.

**Expected result:** Opening a menu preserves the underlying screen.
Browsing menu choices does not execute an action.
A choice ending in `...` opens an editable command prompt.
Dismiss the dropdown, then complete the command's arguments before running it.

Tab and Shift-Tab change menus while a menu is open.
Home and End select the first or last choice.
The letters `f`, `e`, `v`, and `h` choose File, Edit, View, and Help while a menu is open.
F10 also closes an open menu.
Use `:menu File`, `:menu Edit`, `:menu View`, or `:menu Help` for command entry.
Use `:menu` to open File.

Unavailable choices appear dimmed.
Their activation reports the required job, project, file, or page context.
Close an unfinished job-action review before starting another reviewed action.
The toolbar remains above overlays.
On narrow terminals, it abbreviates menu labels and compresses the slider to its interval label.
F10 keeps all four menus accessible.
Move the pointer over a menu choice to highlight it without activating it.
Direct choices leave the dropdown open while the pointer remains inside it.
This permits several setting changes without reopening the menu.
Move the pointer outside the dropdown to dismiss it.
Prompts and reviews remain beneath the dropdown while it keeps keyboard focus.
Dismiss the menu before using those dialogs' editing or review controls.
About replaces the dropdown with its own panel.
Focus update-rate slider leaves the menu and focuses the slider.

Click the top-left `x`, or choose **File → Quit Raven**, to exit.
Esc closes a menu; it does not exit Raven.
Click **Copy** to switch text delivery to **Yank**, or click **Yank** to restore clipboard copying.
Narrow terminals use `C` and `Y` for the same switch.
Yank targets a running local Vim or Neovim server; unavailable delivery retains the export and uses clipboard fallback.
See [Pane navigation](pane-navigation.md#send-text-to-vim-or-neovim) for server discovery and complete-payload limits.

### File menu

| Choice | Result or editable command |
| --- | --- |
| Open project... | Enter a project path with `project PATH` |
| Project runs and attempts | Open the selected project's run inventory |
| Run output artifacts | Browse declared artifacts for the selected run |
| Attach metrics stream... | Enter `metrics FILE` |
| Attach output contract... | Enter `artifacts CONTRACT ROOT` |
| Review batch script... | Enter `preflight SCRIPT [--workdir DIR]` |
| Capture run passport... | Complete `passport capture SCRIPT` and required path options |
| Export current page as text | Export the current page's text |
| Export current table as CSV | Export the current table |
| Export selected jobs as JSON | Export selected or marked jobs and their recorded series |
| Export complete terminal report | Start a background ASCII report export |
| Export library | Browse available exports |
| Execution receipts... | Complete an `execution` command to inspect or recover a receipt |
| Switch connection profile... | Enter `profile NAME` |
| Quit Raven | Exit the application |

The project inventory requires an open project.
Run artifacts require a selected project run.
Preparation and passport capture retain their existing file and path validation.
See [Research](../RESEARCH.md) and [execution recovery](../WORKBENCH.md#execute-a-reviewed-workflow-or-scaling-experiment) for those commands.

### Edit menu

| Choice | Result or editable command |
| --- | --- |
| Copy current selection | Copy the current text selection |
| Switch to Vim/Neovim yanking / Switch to clipboard copying | Change the destination for text and complete-log copies |
| Copy entire log / current page | Copy the complete selected log file, or the current page outside Logs |
| Start line selection | Start selection at the current cursor |
| Select entire log / current page | Mark the complete original log source, or select the currently painted pane elsewhere |
| Clear line selection | Clear the active text selection |
| Filter this page... | Enter `filter TEXT` |
| Clear this page's filter | Remove its text filter |
| Structured table filters | Open the field filter editor in Jobs or History |
| Choose table columns | Open the column editor in Jobs or History |
| Cascading sort editor | Open the sort editor in Jobs or History |
| Bookmark current log line | Bookmark the current line in an open Logs file |
| Pin / unpin selected jobs | Change pin status for the selected or marked jobs |
| Tag selected jobs... | Complete `tag` with tags and optional job IDs |
| Annotate selected job... | Complete `note` with a note and optional job ID |
| Save current location... | Enter `location save NAME` |
| Saved locations | Browse saved destinations |
| Terminal and sampler settings | Open the draft settings editor |
| Edit and test keybindings | Open keybinding editing and testing |

The log-specific choices require an open file on the full Logs page.
Table choices in this menu require Jobs or History.
The underlying table commands also support their documented explicit table identifiers.
See [Tables](tables.md), [Log display](log-view.md), and [Navigation](navigation.md) for complete procedures.

### View menu

| Choice | Result |
| --- | --- |
| Each of the ten page names | Open that page |
| Research: each workspace name | Open one of the thirteen Research workspaces |
| Search Research workspaces | Open the searchable workspace picker |
| Jump to job, run, file, or view | Open universal jump search |
| Back to previous location | Restore the previous destination |
| Forward to next location | Restore the next destination after Back |
| Comfortable panel density | Select the comfortable panel layout |
| Compact panel density | Select the compact layout |
| Focused panel density | Select the focused layout |
| Focus job / main panel | Focus Main |
| Focus Details panel | Focus Details |
| Maximize / restore focused panel | Toggle the focused panel's maximized layout |
| Saved workspace layouts | List saved layouts |
| Pause inspection / Resume inspection | Freeze or resume the inspected snapshot; sampling continues |
| Pause / follow current log | Change follow mode on the full Logs page |
| Wrap / unwrap current log | Change wrapping on the full Logs page |
| Refresh all sources now | Request an immediate source refresh |
| Focus update-rate slider | Focus the polling control's keyboard controls |
| Switch background workers to Single / Multi | Toggle the requested background concurrency mode |
| Use one background worker | Request Single after running task groups finish |
| Use concurrent background workers | Request Multi within shared limits |
| Background worker status | Show applied mode, pending change, and work counts |
| Reset queue polling to INTERVAL | Restore the default global fetching request; the label shows the resulting Jobs interval |
| Navigate buttons with arrow keys (F8) | Focus visible buttons and links for directional navigation |
| Enable / Disable smooth scrolling | Toggle mouse-wheel viewport smoothing |
| Enable / Disable startup animation | Toggle the interactive welcome display |
| Preview startup animation | Preview the welcome without changing its saved preference |
| Plain ASCII reader mode | Select the reader theme |
| Theme: each available theme | Select that theme |

See [workflows](../../README.md#workflows) for the main pages and their purposes.
The theme choices include `default`, `darcula`, `modnokai`, `gruvbox-dark`, `dark`, `light`, `terminal`, `mono`, `high`, and `cb`.
Reader provides plain ASCII text and static notices.
Reader also uses immediate scrolling and skips the welcome.
Refreshing or speeding up fetching does not enable disabled sources.
See [Mouse and button navigation](pointer-navigation.md) for focus, startup, and scrolling preferences.

### Help menu

| Choice | Result |
| --- | --- |
| Page controls and searchable help | Open contextual help |
| Search all commands | Open command discovery |
| Terminal diagnostics | Inspect terminal and path evidence |
| Test keys, mouse, and glyphs | Open the terminal input and glyph test |
| Source health and freshness | Open Sources |
| Activity and background tasks | Open Activity |
| Completed-job review inbox | Open the completion inbox, including reviewed items |
| Alert and quiet-hour controls | Open alert preferences |
| About Raven and update controls | Show the version and toolbar operating instructions |

Use `:about` to open the About panel directly.
Esc, Enter, or `q` closes that panel and restores the underlying screen.

## Check runtime updates

Use Sources to check sampler freshness, errors, and retry backoff.
Use Activity to check background results and file errors.
Account for accounting delay when a job leaves the active queue.
Queue absence alone does not establish completion or failure.

Update controls keep source timeouts, worker limits, and source identity checks.
Readers publish bounded snapshots for the terminal display.
An error must remain visible rather than producing an invented result.
