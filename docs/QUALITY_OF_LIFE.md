# Raven interaction guide index

[README](../README.md) · [Controls](CONTROLS.md) · [Runbook](runbook.md)

This index covers the 50 interaction features introduced in release 4.0.
The identifiers connect each feature to its detailed operating guide.
Use the guide in each row for prerequisites, controls, procedures, and limits.
All displays use terminal characters.
Each graphic has an ASCII fallback.

## Table and job controls: improvements 1–15

Read the [table guide](guides/tables.md).

| ID | Improvement | Use it to |
| --- | --- | --- |
| 1 | [Sort priority editor](guides/tables.md#feature-01) | Change the order of cascading sort rules without removing every rule |
| 2 | [Keyboard-accessible headers](guides/tables.md#feature-02) | Focus a column heading and change its sort direction |
| 3 | [Column order and width controls](guides/tables.md#feature-03) | Reorder, resize, and hide optional table columns |
| 4 | [Numeric resource filters](guides/tables.md#feature-04) | Compare underlying CPU, GPU, memory, duration, and efficiency values |
| 5 | [Interactive filter builder](guides/tables.md#feature-05) | Add field conditions and inspect matching records |
| 6 | [Independent filters per table](guides/tables.md#feature-06) | Keep each table's text search and field conditions separate |
| 7 | [Saved view picker](guides/tables.md#feature-07) | Preview and restore filters, columns, and sort priorities |
| 8 | [Exact History date ranges](guides/tables.md#feature-08) | Select named or custom calendar intervals |
| 9 | [Adjustable Recents size](guides/tables.md#feature-09) | Change the completion count or time window |
| 10 | [Paging that matches the viewport](guides/tables.md#feature-10) | Move by the visible page size with a context row |
| 11 | [Marked jobs manager](guides/tables.md#feature-11) | Inspect marked jobs that are hidden by the current filter |
| 12 | [Freeze the inspection view](guides/tables.md#feature-12) | Hold displayed records while background sampling continues |
| 13 | [Contextual job action menu](guides/tables.md#feature-13) | Inspect the actions available for the selected job |
| 14 | [Interactive node details](guides/tables.md#feature-14) | Inspect the selected node's allocations, jobs, state, and freshness |
| 15 | [Partition and user drill-down](guides/tables.md#feature-15) | Open jobs for a selected partition or account user |

Click a heading to cycle ascending, descending, and off.
Use `:sortby` for the equivalent command operation.
JOBID uses numeric order, including array task IDs.
Sort priority follows activation order.
Unknown values remain last.

## Log search and display: improvements 16–28

Read the [log search guide](guides/log-search.md) for improvements 16–21.
Read the [log display guide](guides/log-view.md) for improvements 22–28.

| ID | Improvement | Use it to |
| --- | --- | --- |
| 16 | [Read older log content on demand](guides/log-search.md#feature-16) | Retrieve a bounded page before the retained tail |
| 17 | [Search the complete log file](guides/log-search.md#feature-17) | Search outside the displayed page through a background operation |
| 18 | [Search across every job log](guides/log-search.md#feature-18) | Search registered files and keep each result attached to its exact source |
| 19 | [Explicit search modes](guides/log-search.md#feature-19) | Select literal or regular-expression matching, case handling, and whole words |
| 20 | [Search results browser](guides/log-search.md#feature-20) | Inspect match context and open the corresponding source location |
| 21 | [Jump to a precise location](guides/log-search.md#feature-21) | Select a source line, byte offset, timestamp, or percentage |
| 22 | [Named log bookmarks](guides/log-search.md#feature-22) | Label important log positions and select them from a list |
| 23 | [Remember each file's reading position](guides/log-search.md#feature-23) | Restore the file's reading cursor and follow state when changing sources |
| 24 | [Timestamp-aligned split logs](guides/log-view.md#feature-24) | Compare source timing and inspect missing or ambiguous timestamps |
| 25 | [Compare actual log files](guides/log-view.md#feature-25) | Inspect additions and removals between two registered sources |
| 26 | [Interactive structured logs](guides/log-view.md#feature-26) | Expand JSON objects and filter by field values |
| 27 | [Reversible noise folding](guides/log-view.md#feature-27) | Collapse repeated messages and expand them when needed |
| 28 | [Unread log indicators](guides/log-view.md#feature-28) | Inspect appended content while following is paused |

A log display mode does not change the source file.
Selections and full-file copies use the original content.
A complete-file operation can reach files larger than the display buffer.
Read the operation's coverage and status before you interpret its result.

## Navigation and terminal controls: improvements 29–40

Read the [navigation guide](guides/navigation.md) for improvements 29–38.
Read the [operations guide](guides/operations.md) for improvements 39–40.

| ID | Improvement | Use it to |
| --- | --- | --- |
| 29 | [Universal jump search](guides/navigation.md#feature-29) | Find cached jobs, runs, log sources, views, workspaces, and commands |
| 30 | [Forward navigation](guides/navigation.md#feature-30) | Restore a location after using Back |
| 31 | [Saved location bookmarks](guides/navigation.md#feature-31) | Save a destination and its inspection context |
| 32 | [Interactive settings screen](guides/navigation.md#feature-32) | Inspect and change display, sampling, mouse, and clipboard settings |
| 33 | [Keybinding editor](guides/navigation.md#feature-33) | Check conflicts and change action keys |
| 34 | [Better command editing](guides/navigation.md#feature-34) | Move by words, remove words, and undo input changes |
| 35 | [Reliable pasted commands](guides/navigation.md#feature-35) | Edit bracketed-paste input before command execution |
| 36 | [Assistance while typing](guides/navigation.md#feature-36) | Inspect command syntax and input errors before execution |
| 37 | [Explain the selected field](guides/navigation.md#feature-37) | Inspect a metric's units, formula, scope, and unavailable-value meaning |
| 38 | [Expand clipped values](guides/navigation.md#feature-38) | Inspect the complete value and copy its underlying text |
| 39 | [Searchable Activity and export library](guides/operations.md#feature-39) | Find retained notices and a bounded persistent export index |
| 40 | [Terminal compatibility assistant](guides/operations.md#feature-40) | Inspect capabilities and run terminal input checks |

Use `?` for contextual help.
Use `:` for command entry.
Use `Ctrl-B` for Back.
An overlay's instructions take priority over page shortcuts while that overlay is open.

## Graph inspection: improvements 41–47

Read the [chart guide](guides/charts.md).

| ID | Improvement | Use it to |
| --- | --- | --- |
| 41 | [Quick chart time presets](guides/charts.md#feature-41) | Select five minutes, thirty minutes, two hours, or available history |
| 42 | [Interactive axis controls](guides/charts.md#feature-42) | Select automatic, fixed, or logarithmic bounds |
| 43 | [Preserve spikes in compressed graphs](guides/charts.md#feature-43) | Retain the minimum and maximum within compressed time bins |
| 44 | [Statistics for a selected interval](guides/charts.md#feature-44) | Inspect sample counts, distributions, and missing-data coverage |
| 45 | [Events overlaid on charts](guides/charts.md#feature-45) | Locate observed phases, alerts, checkpoints, and failures |
| 46 | [Shared scales for comparisons](guides/charts.md#feature-46) | Apply common bounds to comparable graph values |
| 47 | [Metric display preferences](guides/charts.md#feature-47) | Set display labels, declared units, and precision |

Graphs use measured samples.
Missing measurements remain gaps.
Display preferences do not modify recorded values.
Events identify observations available to Raven.
They do not establish events that Raven did not observe.

## Completion review and artifacts: improvements 48–50

Read the [operations guide](guides/operations.md) for improvements 48–49.
Read the [artifact guide](guides/artifacts.md) for improvement 50.

| ID | Improvement | Use it to |
| --- | --- | --- |
| 48 | [Completion inbox](guides/operations.md#feature-48) | Review unacknowledged outcomes and open their logs or evidence |
| 49 | [Alert snooze controls](guides/operations.md#feature-49) | Mute selected notifications temporarily and inspect active quiet periods |
| 50 | [Paged artifact previews](guides/artifacts.md#feature-50) | Inspect declared text, CSV, and JSON outputs with page controls |

Completion states come from scheduler accounting evidence.
Silencing an alert does not remove its underlying condition.
Artifact inspection uses declared outputs.
It does not recursively enumerate a project.

## Select a starting workflow

| Need | Start with | Then inspect |
| --- | --- | --- |
| Find failed jobs | History filters and date ranges | Completion inbox, inspector, and exact job logs |
| Compare resource use | Numeric table filters and saved views | Shared chart scales and interval statistics |
| Investigate a large log | Complete-file search | Results browser, precise jump, and named bookmarks |
| Compare execution attempts | Project run selection | Log diff, passports, charts, and declared outputs |
| Reduce terminal friction | Settings and key bindings | Compatibility assistant and command editing |
| Share an observation | Activity and export library | Plain-text report or the private full-log export |

## Read the complete operating documentation

The [Controls guide](CONTROLS.md) lists launch modes and command families.
The [reference](reference.md) defines measurements, sampling sources, expressions, and configuration.
The [runbook](runbook.md) provides installation and troubleshooting procedures.
The [project standard](PROJECT_STANDARD.md) defines portable reporting files.
The [documentation style](DOCUMENTATION_STYLE.md) defines the writing conventions used here.
