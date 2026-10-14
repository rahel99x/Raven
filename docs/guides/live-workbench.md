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
Light reads Research files every five seconds by default. Active Heavy uses
its telemetry preset: Default requests one-second reads. The managed queue
control leaves this reader unchanged.
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

The toolbar's queue/recording summary opens **Refresh settings**. Queue polling
and reading recorded samples have independent controls in the same panel.
**Setup → Refresh and performance → Refresh settings** opens it too, alongside
Heavy presets and the Custom editor. See [telemetry updates](telemetry-updates.md).

1. Open the summary or enter `:refreshsettings`.
2. Use the queue's **Faster**, **Slower**, or **Preset rate** action.
3. Choose Automatic, one-second, or two-second recording reads.
4. Inspect actual sample/read age and any source or policy limits.
5. Use Esc to return to the previous view.

The displayed queue interval uses the same calculation as the
collector. The range starts at the active queue base and ends at a tenfold
faster request, subject to site policy. With starter settings, Light spans
60–6 seconds and Default Heavy spans 5–0.5 seconds. If a mandatory minimum
fixes the whole range, the label reports that minimum. Selecting a Heavy preset
resets the queue control to the preset's base. The recording-read choice remains
independent. Existing `:rate N` commands retain their one-to-50 scale.

| Command | Function |
| --- | --- |
| `:rate` | Show the effective Jobs interval |
| `:rate N` | Select a position from 1 (base) to 50 (fastest permitted endpoint) |
| `:rate reset` | Restore the active queue base |

Request budgets, demand, idle slowdown, errors, and command duration can defer
an individual read. Changing queue cadence does not reset rolling usage, overlap
reads, or enable a disabled source. Read the effective interval and source age
in Sources to distinguish the requested cadence from actual observations.

The queue preference persists with normal UI state and its selected namespace.
The compatibility field `polling_multiplier` stores this position; it is not a
frequency multiplier. A saved position overrides that configured default.
`raven --rate N` overrides the saved position at launch. `--interval SECONDS`
remains a compatibility launch option; an explicit `--rate` takes precedence.
Settings → Polling interval changes the same position. Cancelling its preview
restores the previous position; accepting it saves the preference.

Native compatibility sessions retain their existing global behavior: the scale
requests five seconds through 500 milliseconds for ordinary native sources.
Slower configured bases are accelerated by at most ten, subject to source
limits. Remote file readers retain a 1.5-second minimum; local readers and live
probes retain a half-second minimum. Forecast and budget sources retain their
30-second minimums. Managed sessions use versioned `collection` settings and
Heavy telemetry preferences instead of accelerating every reader with the queue
control.

### Set one metric's interval

A running metric has a display-window slider and a separate polling slider.
Use them on Analytics → Job series or Jobs → Details → Analytics → Job series.
The first sets a window from 30 seconds to one second.
The second requests reads from five seconds to 500 milliseconds for that exact job and source.
Both use logarithmic scales. Numeric metric polling commands use positions one to 100.
**Set** shows that slider's requested interval; **Poll** shows the shared effective interval.
Compact rows put the effective value before the polling track and `S` plus the requested value after it.
Moving the slider updates its requested value even when another request for the same source keeps the effective interval unchanged.

Right-click the first slider to restore 30 seconds.
Right-click the second to restore its five-second polling request.
These resets preserve the other slider, Live state, graph zoom, and job selection.
Sampling requests apply while Live is off and can remain active after the graph moves offscreen.
They reset when Raven restarts or the exact job attempt ends.

The fastest reader or per-metric request sets the shared interval, subject to
collection policy. Requests do not multiply. The managed queue control does not
change these metric requests.
CPU and memory share one job probe. GPU curves share another.
Other jobs retain their reader cadence and their own requests.
CPU, memory, GPU, and local application-file readers retain a half-second lower bound before managed collection policy.
Remote application-file reads retain a 1.5-second minimum.
Managed collection floors, demand gating, command budgets, command duration, and retry backoff still apply.

The sampling control changes how often Raven reads an application metrics file or CSV trace.
It cannot increase how often the producing job writes that file.
Read the effective polling interval and source age below the graph before interpreting an empty window.
See [Charts](charts.md#follow-a-running-metric) for commands, keyboard controls, and limits.

## Use the terminal toolbar

The first row contains File, Edit, View, Setup, Help, **Launch**, a quit control,
the managed Light/Heavy status, and the queue/recording summary. The next rows
identify the site, login, and attached project, then provide the main page tabs
and any notifications. The default layout fits an 80-column terminal.

**Setup** has five categories: Connections and accounts, Launch defaults,
Recordings and metrics, Refresh and performance, and Installation and diagnostics.
Research and Analytics provide **Set up this view** beside their own content.
See [Everyday workflow](everyday-workflow.md) for the launch-to-results path.

Change Single/Multi background concurrency through **Setup → Refresh and
performance → Advanced preferences**, or `:preferences`. It is independent of
Light/Heavy and applies without restarting Raven. See
[Background workers](background-workers.md) for pending transitions and limits.

### Open and traverse a menu

1. Click a menu label or press F10.
2. Use Left or Right to select another top-level menu; Right opens a selected submenu.
3. Use Up, Down, or page keys to select a choice.
4. Press Enter or Space to activate the selected choice.
5. Press Left or Esc to return from a submenu; Esc at the top level closes the menu.

**Expected result:** Opening a menu preserves the underlying screen.
Browsing menu choices does not execute an action.
A choice ending in `...` opens an editable command prompt.
Dismiss the dropdown, then complete the command's arguments before running it.

Tab and Shift-Tab change menus while a menu is open.
Home and End select the first or last choice.
The letters `f`, `e`, `v`, `s`, and `h` choose File, Edit, View, Setup, and Help while a menu is open.
F10 also closes an open menu.
Use `:menu File`, `:menu Edit`, `:menu View`, `:menu Setup`, or `:menu Help` for command entry.
Use `:menu` to open File.

Unavailable choices appear dimmed.
Their activation reports the required job, project, file, or page context.
Close an unfinished job-action review before starting another reviewed action.
The toolbar remains above overlays.
On narrow terminals, menu labels shorten and the queue/recording summary becomes **Refresh** or **R**.
F10 keeps all five menus accessible.
Move the pointer over a menu choice to highlight it without activating it.
Local presentation choices can leave the dropdown open for repeated changes.
Guided setup, Launch, Connect data, Run home, and Refresh settings close it when opened.
Move the pointer outside the dropdown to dismiss it.
Prompts and reviews remain beneath the dropdown while it keeps keyboard focus.
Dismiss the menu before using those dialogs' editing or review controls.
About replaces the dropdown with its own panel.

Click the top-left `x`, or choose **File → Quit Raven**, to exit.
Esc returns from a submenu or closes the top-level menu; it does not exit Raven.
Use **Edit → Switch to Vim/Neovim yanking** or **Switch to clipboard copying**
to change text delivery.
Yank targets a running local Vim or Neovim server; unavailable delivery retains the export and uses clipboard fallback.
See [Pane navigation](pane-navigation.md#send-text-to-vim-or-neovim) for server discovery and complete-payload limits.

### File menu

| Choice | Result or editable command |
| --- | --- |
| Launch job... | Choose a project and script, review resources and recordings, then submit |
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
| Jobs, History, Analytics, Log, Research | Open a primary page |
| Run home | Follow the selected run from submission through outputs |
| Research workspaces | Choose any Research destination |
| Analytics views | Choose job series, history, timeline, advisor, or compare |
| Cluster and diagnostics | Open Cluster, Nodes, Group, Deps, Sources, or a plugin page |
| Favorites | Pin the current page or subview and reopen a saved destination |
| Pinned tools | Open the existing operation favorites |
| Display and navigation | Back/Forward, jump, panel density and focus, terminal tools, layouts, freeze, and log controls |

The default tab row shows Jobs, History, Analytics, Research, and Log. A secondary
page remains visible while active; pinned pages can remain in the row. Research
shows common workspaces, the current workspace, and saved favorites; **All views**
opens its searchable picker. Favorites store destination identities without
retaining a selected job, path, or account.

**Display and navigation → Advanced preferences** and **Setup → Refresh and
performance → Advanced preferences** open the same preferences menu. It contains
background worker controls, terminal and clipboard preferences, smooth scrolling,
startup controls, and themes. `:preferences` opens it directly.

Themes include `default`, `darcula`, `modnokai`, `gruvbox-dark`, `dark`, `light`,
`terminal`, `mono`, `high`, and `cb`. Plain ASCII reader mode uses static feedback,
immediate scrolling, and no welcome animation. See
[Navigation](navigation.md) for saved destinations and settings, and
[Mouse and button navigation](pointer-navigation.md) for directional focus.

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
