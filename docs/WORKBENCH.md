# Raven terminal workbench

[Quick start](../README.md) · [Reference](reference.md) · [Runbook](runbook.md) ·
[Project reporting standard](PROJECT_STANDARD.md) · [Planning guide](WAVE_TWO.md)

This guide describes workbench workflows introduced in release 3.0.
The [interaction guide index](QUALITY_OF_LIFE.md) covers additional procedures
introduced in release 4.0.
Use [Controls](CONTROLS.md) for the complete control index.

The controls below describe native workbench contracts. Managed Raven applies
execution admission in addition to each contract's input validation and review.
Request Heavy in the same interface for supported scientific-analysis adapters.
Some native execution handoffs remain deferred; consult the
[execution contracts](raven/execution-contracts.md) before adopting them.

Raven remains a terminal-only application for CARC and other Slurm clusters.
Everything here renders as terminal characters, with Unicode block charts, an
ASCII fallback, and optional color and motion. The core has no third-party
runtime dependencies; the embedded terminal has separate optional dependencies.
Run it on the CARC login node so local project paths and scheduler commands refer
to the same machine. The simulated cluster demonstrates the interface without
submitting a real job.

## First use

Update your checkout, keep its existing configuration, and launch normally:

```bash
git pull --ff-only
python3 scripts/setup.py --mode local
raven
# Explore without accessing Slurm:
raven --fake --unicode
```

1. Press `?`, then `/` and a word such as `logs` to search help. Arrows and page
   keys scroll the results; Esc returns to the dashboard.
2. Open `:` and type `theme dark`, `theme light`, or `theme terminal`. Commands
   use shell-style quoting, so a path with spaces can be enclosed in quotes.
3. In Jobs, use `Ctrl-W` to move between Main and Details; scroll Details with
   arrows, and use `z` to maximize the focused panel. Esc restores it.
4. Select an active, recent, or historical job and press `I` for its inspector.
   Tab changes sections; `l` opens logs and `e` opens failure evidence.
5. Press `Ctrl-P` to search Research workspaces. `Ctrl-B` returns to the previous
   location, preserving its selected identity, filter, and scroll position.
6. Use `:activity` to revisit operation results and export paths. Notices remain
   available during the session even after the footer changes.

The command examples below are entered after `:` inside Raven. They are
interactive workbench commands. Overlay availability differs from the scripted
`raven run` interface. Use `?` on the current page
for its keys and the [reference](reference.md) for scripted commands.

## The twenty improvements

| Improvement | How to use it | Behavior and limits |
| --- | --- | --- |
| 1. Shared visual design | Open **Themes**; `T` cycles curated palettes; `:theme system` inherits terminal colours | Gruvbox is the default. Named palettes cover all surfaces and charts; compatibility accessibility modes remain available by command. |
| 2. Focusable, expandable panels | `Ctrl-W` / F6, `z`; `:focus details`, `:maximize on` | Main and Details have independent scroll positions where a page provides both. `:panel-scroll page-down` supplies a command alternative. |
| 3. Information density and saved layouts | `:density comfortable`, `compact`, or `focused`; `:layout split 55`; `:layout save work` | Split is Main's percentage, from 20 to 80. Up to 16 named layouts; `load`, `delete`, and `list` manage them. Narrow screens use a vertical layout. |
| 4. Configurable metric dashboards | `:dashboard`, `:dashboard pin loss`, `:dashboard hide debug_value` | Arrange up to 64 reported metrics, with saved pins, visibility, order, card expansion, and colors. |
| 5. Richer Jobs and History tables | Click headings; `:sortby cpus desc`, `:columns`, `:facet state=FAILED,TIMEOUT`, `:savedview save failures` | Cascading column sorts with direction/priority, selectable columns, saved table views, and optional array grouping. Identity and state columns remain visible. |
| 6. Breadcrumbs and Back | `Ctrl-B` / Alt-Left / `:back`; `Ctrl-P` / `:workspaces` | Restores the originating view and exact identity. Back history is bounded to 32 locations and stays session-local. |
| 7. Searchable, scrollable help | `?`, then `/`; `:help copy` | Help is contextual, wraps to the terminal width, and scrolls through all matching instructions. |
| 8. Editable command palette | `:`, arrows, Tab, PgUp/PgDn | Fuzzy results have descriptions; edit anywhere in the line, complete supported IDs/arguments/paths, and recall up to 50 commands. |
| 9. Unified job inspector | `I` / `:inspect JOBID` | Overview, Resources, Steps, Files, and Evidence use the same job identity across active, recent, and historical records. Missing evidence stays unavailable. |
| 10. Activity and background tasks | `Ctrl-A` / `:activity`; `:task cancel` | Retains up to 256 session notices. Full-log and large-selection copies report bytes when available. Copies and interactive report exports accept cancellation; readers without numerical progress show busy/elapsed status. |
| 11. Interactive graphs | `:chart loss`; arrows, `+`/`-`, `[`/`]`, Tab | Exact sample timestamps/values, zoom and pan, synchronized timestamp selection across metrics, and source/freshness/coverage labels. |
| 12. Event timeline | `:timeline`; arrows and Enter | Bounded observed transitions, application phase changes, resource observations, and cited log events. `s` seeks a selected event during replay. |
| 13. Log workbench | `:logpan +40`, `:logview split`, `:logview json`, `:logpreview on` | Horizontal scrolling, grouped file metadata/previews, stdout/stderr comparison, and structured JSON presentation. Original bytes remain the copy source. |
| 14. Multi-log failure evidence | `:workspace evidence` / `:investigate JOBID`; arrows and Enter | Evidence uses the job/run log catalog, reports inspected and omitted sources, and opens exact cited files. |
| 15. Project and run picker | `:project /absolute/project`, then Enter; `:runs` | Explicitly binds a standard execution attempt's metrics, log index, contract, and verified passport, including local runs without a scheduler job. |
| 16. Visual comparisons | `:diff JOBID JOBID`; `:diff passport LEFT RIGHT` | Structured resource/parameter changes and aligned job curves. In Diff, `u` reveals or hides unchanged fields. |
| 17. Artifact explorer | `:outputs` / `:artifact` | A tree of declared outputs, validation states, and bounded text/JSON/CSV previews. It does not recursively enumerate the project. |
| 18. Editable submission preflight | `:preflight SCRIPT --workdir DIR` | Edit script, workdir, CPUs/task, memory, time, partition, and GRES; `p` validates locally, then `s` opens submission review. |
| 19. Complete action reviews | Action confirmation: arrows/PgUp/PgDn, Tab, Enter | Every target is available in the scrollable review. Cancel has initial focus; `y` explicitly confirms and `n`/Esc cancels ordinary action reviews. |
| 20. Reviewed planning-to-execution handoff | `:orchestrate workflow FILE --workdir DIR` or `:orchestrate scaling FILE --workdir DIR` | Separate explicit batch review, actual scheduler IDs, durable receipts, observed results, and guarded partial-failure recovery. See below before execution. |

## Shape your workspace

Density and split proportions adjust the layout without changing sampled data or
Slurm polling. A focused panel has a visible focus label. The Main panel keeps
the page's table selection behavior; Details scrolls its own content. Pages with
one panel retain their normal controls. Logs keeps its existing `z` behavior;
use `:maximize` when you want the layout action there.

```text
density comfortable
layout split 50
focus details
maximize on
layout save inspection
layout load inspection
```

Named layouts store density, split, focus, and maximization. Preferences save in
ordinary Raven UI state when enabled; `--no-state` disables that persistence.
Roomier layouts reveal more content, while narrow terminals wrap the same
evidence into a vertical presentation. Panels inspect bounded source rows rather
than allowing a frame to allocate an unbounded document.

In Jobs or History, `:columns` opens the column picker: arrows choose and Space
toggles optional columns. You can also name a table explicitly:

```text
columns history hide nodes
facet history state=FAILED,TIMEOUT partition=gpu
sortby history name asc
sortby history cpus desc
savedview save "failed GPU jobs"
savedview load "failed GPU jobs"
facet clear
jobgroups on
```

Facets accept `state`, `partition`, `tag`, `name`, and `id`. Commas provide
alternatives within a field; fields combine with the ordinary text filter. An
empty value removes that field. Up to 32 named views retain their table,
columns, facets, text filter, complete sort cascade, and history window. Array grouping
applies to observed array task IDs in Jobs: Left folds the selected array and
Right expands it. It does not synthesize unobserved tasks.

Every visible table heading, including JOBID, accepts a mouse click to cycle
**ascending → descending → off**. The first activated column is primary; later
columns break its ties. `^1` means ascending at priority one, and `v2` descending
at priority two. Direction changes keep priority. Turning one column off leaves
the others; turning all off restores source order. `:sortby [TABLE] COLUMN
[asc|desc|off]` offers the same action, cycling when direction is omitted.
`:sortby cpus off` removes the CPU rule; `:sortby clear` removes them all.
`s`, `S`, and `:sort name` resume a legacy single-key sort.

Jobs, Recents, History, Group, My Nodes, Sources, and Cluster partitions keep
independent cascades (`jobs`, `recent`, `history`, `group`, `nodes`, `sources`,
`cluster`). Recents defaults to five retained records. Raven 4.0 adds count and
time-window controls through `:recents`; see the [table guide](guides/tables.md#feature-09).
JOBID uses natural
numeric and array-task order: `2` before `10`, `123_2` before `123_10`. Measured
values sort before rounding; unknowns remain last in both directions. Jobs pins
remain first, and grouping presents the sorted observed array tasks together.
If sorting would hide the selected task behind a different folded representative, Raven expands that array and preserves the selected job.
Cascades persist with ordinary UI state, restore with Back, and travel with
saved Jobs and History views.

## Navigate and issue commands

Breadcrumbs show the page, selected job, workspace, or current log source. Back
restores filters, table options, panel context, and retained log position; if a
file rotated or disappeared, normal identity validation still applies. Back does
not reissue job actions. Changing profiles clears context belonging to the old
connection.

The palette supports Left/Right/Home/End, Backspace and Delete, Up/Down to choose
a suggestion, Tab to insert a completion, and PgUp/PgDn to recall older/newer
commands. Enter runs the edited command; Esc closes it. Input is bounded to
4,096 characters, and command history to 50 entries. Paths and values containing
spaces use quotes, for example:

```text
project "/project/my study"
dashboard color "validation loss" magenta
```

Unicode text input is preserved in command arguments, including paths, even
when the dashboard uses ASCII rendering. Rendering a portable label does not
rewrite the underlying filename.

`Ctrl-P` opens the Research picker; type to filter, arrows/page keys choose, and
Enter opens the workspace. `:workspace evidence` opens one directly. The job
inspector uses Tab/Shift-Tab or Left/Right to change sections and arrows/page keys
to scroll. It preserves the selected job if that job's lifecycle changes.

In ordinary action confirmations, scrolling inspects targets without dismissing
the review. Tab or Left/Right chooses Cancel or Confirm, then Enter activates
that choice. The existing explicit `y` shortcut remains available. The batch
execution review below has its own visible confirmation controls.

Interactive `:export report` runs in the existing background worker, so navigation
and terminal redraws continue. It captures a private frozen scheduler/UI snapshot
when requested, then reads project files while building the report. A complete
ASCII report is published atomically with mode `0600`; the report includes its
capture context and a selected 40-line log window, rather than the whole log file.
Use `Y` in Logs for the full original file instead.

Open `Ctrl-A` / `:activity` to see report status and its eventual output path.
`c` there or `:task cancel` stops a report between pages and bounded read/write
chunks, without publishing an incomplete report. Copy cancellation uses the same
controls. Execution cancellation remains separate and stops future submissions.

## Inspect measurements and events

Scrollable documents have a right-edge scrollbar and top/bottom arrows in their header.
Use them to read Advisor, History, Timeline, and comparison content without changing the selected source.
Drag rendered lines, or use `v`, navigation keys, and `y`, to copy one pane's displayed text.
Use the toolbar's Copy/Yank switch for clipboard delivery or a running local Vim/Neovim server.
See [Pane navigation](guides/pane-navigation.md) for scope, coverage limits, and editor setup.

Load application metrics in Experiment, then arrange the dashboard:

```text
dashboard search loss
dashboard pin loss
dashboard move loss 1
dashboard expand loss
dashboard color loss magenta
dashboard show throughput
dashboard search
chart loss
```

Colors are `cyan`, `magenta`, `green`, `yellow`, `blue`, and `red`. `unpin`,
`collapse`, and `hide` reverse the corresponding actions; `dashboard reset`
restores the default arrangement. Pins sort before the custom order. Searching
and card preferences affect the display, not collected metrics.

In Chart, arrows move the crosshair between samples; Home/End jump to the first
or last visible sample. `+`/`-` zoom and `[`/`]` pan. Tab/Shift-Tab switches
metric while choosing the nearest available timestamp. Command alternatives:

```text
chart zoom 4
chart pan 1
chart cursor 10
chart window 300
```

Zoom ranges from 1 to 1,024; pan is a fraction from 0 to 1. Cursor numbering is
one based within the visible samples. A time window is positive seconds, up to
365 days. Analysis retains at most 10,000 points per series and 512 timeline
events. Unknown values and observed sampling outages remain gaps; graph inspection
does not fabricate readings between samples.

Timeline collects events Raven actually observed. Application phases appear as
their reports arrive; older phases absent from those observations stay unknown.
Enter follows an attached job or log citation; events without an attached source
explain that limitation. In a recording, `s` or `:timeline seek EVENT_NUMBER`
seeks the event's timestamp. Replay stays visibly distinct from live data.

`:diff` accepts two to six job IDs, or uses marked jobs when IDs are omitted.
Passport comparisons load actual passport files in the background and show
changed fields; `u` includes unchanged fields. Job comparisons align retained
resource samples on elapsed time. Neither comparison invents history from before
Raven observed it.

## Read logs and follow evidence

Select a job and press `l`, or open a bound run's logs. `O` shows the grouped
file list. Metadata includes regular-file size, modification time, and status;
`:logpreview on` adds a bounded preview. Space folds the selected group,
`:loggroup GROUP` toggles it, and `:loggroup all` expands every group. Metadata
inspects at most 32 files, selected first, and explicitly reports omissions.
Metadata is cached for 30 seconds; refresh updates it.

Interactive local and SSH log reads publish immutable snapshots through the shared worker.
Frames and key actions use the last published data while a read is pending, so a
cold file or a slow CARC shared filesystem/remote connection does not block scrolling.
Source errors and pending status remain visible; refresh invalidates the cached
observation. Local files are checked at most twice per second; SSH keeps its
longer polling interval. Job-pane previews use the same background worker.
Explicit noninteractive reads can still wait for their result.

Scrolling keeps cursor movement in memory, without writing preferences on each
arrow or wheel tick. Rapid scroll inputs share a bounded redraw: every input
still runs in order, and clicks or other commands wait for a fresh view. Long
Unicode lines are clipped using their visible prefix rather than scanning the
entire offscreen line.

Left/Right pans an unwrapped file by eight display columns; `:logpan +40`
moves farther and `:logpan 0` resets. `:logview json` formats a bounded JSON
document, while `:logview split` compares available stdout/stderr sources.
Alternate views read at most 64 KiB per source and retain up to 240 display rows,
with truncation visible. Arrows/page keys scroll their presentation. Esc or
`:logview plain` returns to original lines. In Split, `[` and `]` choose which
source full-file copy uses. Return to Plain for logical source-line selection.

In version 3.0.1, opening another job/run/file and restarting Raven starts at the
left edge. Horizontal position is kept only for the current source; `Ctrl-B`
restores a previous source's deliberate position. An active offset and the
`:logpan 0` reset command stay at the beginning of the status line. This keeps
the horizontal offset and its reset visible. ANSI color/cursor commands in logs
are removed from the display; embedded carriage returns, backspaces and NUL
appear as `^M`, `^H` and `^@` without overwriting text or moving terminal cursors.
These display changes preserve physical source lines and original copy bytes.

Selection and full-file copy retain the existing guarantees: arrows position the
cursor, `v` begins a logical range, and `y` copies its original bytes. `Y`,
`V` then `y`, or `:copy all` copies the complete selected file through bounded
background work, independent of formatting or the displayed tail. Large or
non-UTF-8 copies keep exact private exports; clipboard limits do not turn them
into shortened successful copies. See [copy details](reference.md#copying-and-exporting).

Failure Evidence shares the same job/run catalog, including explicitly registered
worker and external logs. Inspection is bounded to 32 sources, a 128 KiB tail per
source, and 1 MiB combined bytes. Coverage distinguishes inspected, omitted, and
unavailable files. Arrows select a citation and Enter opens its exact source;
replaced files or citations outside the retained window produce a visible notice.
Scheduler-only citations have no log file to open. `Ctrl-B` returns to Evidence.

## Bind a project execution attempt

Follow the [reporting standard](PROJECT_STANDARD.md) and create
`runs/<run_id>/run.json`. Then, with Raven running locally on CARC:

```text
project /absolute/project
run select fit-a1
runs
outputs
run clear
```

The picker discovers only direct `runs/<run_id>` directories beneath the root
you chose. It inspects at most 256 attempts, 4,096 directory entries, 64 KiB per
inventory, and 8 MiB of inventory bytes. It does not scan source trees or guess
an active run. `/` filters, arrows and Enter select, `r` refreshes, and `!` opens
binding/discovery notices. The inventory is revalidated when selected; its
`run_id` must match its directory, and `attempt` identifies that execution attempt.

A selected inventory binds its declared metrics and log index, the standard
`.raven/contracts/outputs.v1.json`, and a verified passport where unambiguous.
Runs without `job_id` remain project runs: metrics, logs, and outputs work without
attaching an unrelated running job. Missing sources stay missing. Multiple
passports require an explicit `:run passport passports/FILE.json`. Clearing the
binding restores the original research/log settings. The standard SSH backend
also supports confined project discovery through an isolated Python helper on
the selected host. That host needs Python 3.10 or later. See
[automatic reads and SSH](guides/research-automation.md#automatic-reads-and-ssh).
Failure Evidence and the job inspector require an actual visible scheduler
record; a project-only run still exposes its declared files in Logs and Outputs.

Run inventory paths and artifact declarations are exact, relative, and confined
to their declared roots, without symlink traversal. A `logs.json` index may
explicitly register sibling or absolute read-only log locations; those preserve
their source identity and appear as external declarations. Moving a run does not
rewrite an absolute source path.

The output explorer displays declared contract files as a collapsible tree,
with validation results and missing/invalid states. `/` filters, arrows select,
Enter opens a preview, and Esc returns. Text, JSON, and CSV previews read at most
64 KiB and expose at most 256 lines; at most 2,048 tree nodes are shown. Previewing
does not execute project code or open undeclared filesystem content. Expected
final outputs may legitimately be missing while a run is active.

## Edit and review a submission

```text
preflight jobs/run.sbatch --workdir /absolute/project
```

Arrows or Tab choose a field; Enter edits it. Left/Right/Home/End, Delete,
Backspace, and Ctrl-U edit the value; Enter accepts it and Esc abandons editing.
`p` validates the edited script and effective resource flags locally and refreshes
the command preview. Existing metadata and resource flags outside the form are
preserved. Editing invalidates the previous validation: press `p` again before
`s` opens ordinary explicit submission review. Preflight itself does not run the
script, contact Slurm, or submit a job. Local preparation is available without a
live scheduler; actual submission needs the local cluster backend.

## Execute a reviewed workflow or scaling experiment

The existing `workflow plan`, `scaling plan`, and analysis commands remain
offline preparation. Workflow plans keep sealed symbolic dependencies and
`submittable: false`; an individual `submit` cannot bypass that seal.
Use the separate, explicit interactive handoff:

```text
orchestrate workflow /absolute/project/workflow.json --workdir /absolute/project
orchestrate scaling /absolute/project/scaling.json --workdir /absolute/project
```

Preparation reads the recipe and preflights every script without submitting.
The review lists each node/repeat, its dependencies, effective resources, and
command. Arrows/page keys inspect every node; Enter or `d` expands its details.
Tab moves to the visible Cancel/Confirm controls, then Enter activates one.
Only explicit confirmation starts scheduler calls. Batch execution requires
Raven running locally on the cluster; SSH viewers and replay cannot submit.

Execution allows at most 64 reviewed jobs and 16 recorded attempts per node.
Workflow children receive actual accepted upstream IDs in `afterok` dependencies;
symbolic IDs are never sent as scheduler receipts. Scripts are revalidated before
new calls. Raven durably records each launching intent before contacting Slurm
and retains actual accepted IDs, rejected responses, and unknown outcomes in a
private execution receipt. A partial failure stops further submissions.

While execution is running, `c` requests cancellation before further nodes.
Already accepted jobs keep running; this does not cancel them on the scheduler.
Closing the execution overlay also does not undo accepted jobs. Receipts live
in the configured state's `executions/` directory, with private passports
alongside them. Raven's default state base is `~/.local/state/raven/`, honoring
`XDG_STATE_HOME`. Execution requires persistent private state and
refuses new submissions with `--no-state`; preserve receipts for recovery.

Receipts belong to their recorded connection, profile, and owner. Copied or
unscoped receipts remain inspectable, but collection, resume, retry, and recovery
require the original scope. Return to that connection or prepare a fresh reviewed
batch; numeric job IDs from another cluster cannot establish the receipt's outcome.

```text
execution /absolute/path/to/receipt.json
execution collect
execution resume
execution retry NODE
execution recover NODE REAL_JOB_ID
```

Collect records what the current queue/accounting snapshot actually shows.
Missing observations stay missing. Scaling collection derives measured repeats
from observed scheduler records, with failed attempts censored; it does not
invent runtimes or claim future performance. In a receipt, `c` collects current
observations and Enter opens node details.

Resume, Retry, and Recover each open a separate explicit review. Resume launches
only unattempted nodes and refuses to proceed while an outcome is unknown or a
submission needs a reviewed retry. Retry is limited to definitively rejected or
not-submitted nodes; it is not an automatic rerun of a failed accepted job.
Recovery binds an unknown launch to a supplied real scheduler ID only when
accounting verifies its exact reviewed submit line and working directory.
Insufficient accounting evidence refuses recovery instead of risking a duplicate
submission. Preserve receipts and inspect the scheduler directly when evidence
is unavailable.

## Verification and adoption

The interface and scheduler integration are covered with simulated jobs, local
files, malformed/stale data, bounded-work checks, and terminal rendering tests.
Development verification does not submit real jobs. Live CARC accounting,
permissions, submission receipts, and workload performance still require local
verification. Start with `raven --fake`, then the runbook's low-impact CARC
checks, and review one small real submission before adopting batch execution.
