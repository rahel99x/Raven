# Log presentation and export

[README](../../README.md) · [Controls](../CONTROLS.md) · [Log source search](log-search.md)

Inspect registered log sources as original text, aligned streams, differences, or structured records.
Display modes preserve the underlying file identity and bytes.
History exports collect complete initial source ranges, subject to the reported availability and discovery limits.

**Prerequisites:** Select a job or project run with declared log sources and a working local or SSH file connection.

## Open an exact source

1. Select a job in Jobs, Recents, or History.
2. Press `l`.
3. Use the log file catalog to select a registered source.
4. Press Enter to read that source.

**Expected result:** Logs opens the selected source and displays its path.
A source can be stdout, stderr, a worker log, or a declared external log.
Raven reads files in the background. A file adapter error does not cause a local-file fallback.

For full-file search, older pages, named bookmarks, and saved reading positions, use the [log search guide](log-search.md).

## Clear a selection without changing files

Right-click in the main Logs page to clear job selections, marks, and line selections.
Right-clicking a selected or marked job in its docked history pane opens that job's group menu instead.
The same rule applies to raw text, split streams, diffs, structured records, folded messages, and the file browser.
The exact source, display mode, and visible location remain open.
The click does not open a file or activate another control.
Click a line or file row, or use navigation keys, to select again.
Shift-click still extends a line selection.

In a Log Tools source page, results list, or bookmarks list, right-click clears only that dialog's local selection.
It leaves the dialog and its source open.
Select a row again before copying a cursor line, opening a result, or deleting a bookmark.
`Y` remains the explicit complete-file copy control in a source page.
See [Right-click priorities](pointer-navigation.md#clear-selections-with-right-click) for graph reset and History export.

## Export logs for History jobs

Use History to collect the complete outputs for one or more exact job IDs.
The export includes registered stdout, stderr, additional job log files, and declared project logs in other locations.
It does not use the displayed log page as the export content.

1. Open History.
2. Click a job row, or hold Shift and drag through the required rows.
3. Release the button to finish the marked range.
4. Right-click the selected row or one of the marked rows, then choose **Export logs**.
5. Select **Copy all logs to clipboard**, **Copy logs to directory**, or **Cancel**.

**Expected result:** A marked-row menu uses the exact applicable marked IDs.
An unmarked selected-row menu uses that exact job and retains unrelated marks.
The target list stays fixed while the menu and background task are open.
A right-click outside the History list clears the selection without activating another control.
Right-click elsewhere inside the list to open the export menu directly for marked jobs, or the selected job when there are no marks.

Use Space to mark individual rows when the terminal cannot report a drag.
Hold Shift during a drag to add its range to the existing marks.
Press Esc during the drag to restore the previous marks.
Use `:historylogs` to open the same menu without a mouse.
Use `:historylogs clipboard` or `:historylogs directory` to start the corresponding procedure.
Use `:historylogs cancel` to cancel the open procedure.

### Copy to the clipboard

Select **Copy all logs to clipboard**.
Raven discovers the exact sources and copies their complete initial byte ranges in a background task.
The clipboard text has job and source headings so that separate files remain identifiable.
Raven also retains a private raw-file bundle and its manifest.

The clipboard requires valid UTF-8 and an available local clipboard tool or terminal OSC 52 support.
Raven does not replace invalid bytes or send a truncated clipboard prefix.
If a transport cannot accept the complete text, the result identifies the retained export and the reason.
The directory bundle preserves the original bytes regardless of clipboard support.

### Copy to a project directory

1. Select **Copy logs to directory**.
2. Select a listed folder and press Enter, or click it, to open it.
3. Use **Parent folder** to return to its parent within the projects root.
4. To create a child folder, select **New folder...**.
5. Type one folder name and press Enter to create it. Press Esc to return without creating it.
6. Open the new folder if required.
7. Select **Save here** to copy the logs, or **Cancel** to return without an export.

The picker starts at `exports.projects_root` in the Raven configuration.
If that field is empty, Raven uses the registered native project root when available, then `~/projects`.
Set an explicit existing root when your projects use another location:

```json
{
  "exports": {
    "projects_root": "/home/alex/projects"
  }
}
```

Add the field to your existing configuration.
The selected destination is on the machine that runs Raven, including a CARC login node when Raven runs there.
The picker remains inside its root and rejects symlink traversal.
New folder names must be printable, contain no path separator, and fit within 255 UTF-8 bytes.
An existing folder is not overwritten by **New folder...**.

Use **Open named folder...** to enter the name of an existing child that is absent from a bounded directory listing.
It uses the same single-name rules and does not create a folder.
Use Up, Down, Page Up, Page Down, Home, and End to select picker entries.
Use Tab or Shift-Tab to reach its controls, then Enter to activate one.
Backspace or Left opens the parent directory.
Esc cancels the current dialog, except in the folder-name prompt where it returns to the picker.

### Check missing outputs and the result

Before publishing, Raven checks the expected scheduler paths and each registered project source.
If an expected file is absent or unavailable, an alert lists its job ID, label, path, and reason.
Scroll the alert to inspect all affected jobs.
Correct the source locations or restore the files, then use **Retry**.
Use **Export available logs** only when you accept the listed omissions.
That explicit partial export retains the missing-output list in its manifest.
Use **Back to export menu** to choose another destination, or **Cancel** to return to History.
Files that were never declared cannot be identified as missing expected outputs.
Declare every worker or external log in the [project log inventory](../PROJECT_STANDARD.md#log-locations-logsjson).

Raven gives each export a unique `tower-logs-...` directory.
It stores each raw source beneath a job and source directory, with the original basename.
`manifest.json` records job IDs, source paths, byte counts, SHA-256 digests, aliases, missing outputs, and warnings.
Identical source files shared by several selected jobs are copied once and keep all associated job IDs.
Directories use mode `0700`; files use mode `0600`.
The export never overwrites an existing bundle.

The receipt gives the exact destination and clipboard delivery methods.
Check warnings before using the output as a complete record.
A source that grows during copying contributes its complete initial byte range and an explicit warning.
A replaced, truncated, or changed source does not become a published partial file.
If a source becomes unavailable after the initial check, the alert identifies the affected job and source and any bundle already published.
Use **View export receipt** to inspect that bundle's destination and clipboard methods.
Use **Cancel** while a task is active to stop it; Raven removes its unfinished bundle before publication.
If publication finished before Raven receives the cancel request, the receipt identifies the completed bundle.

Discovery and copying run outside the terminal input loop.
An export accepts at most 1,024 exact job IDs.
Per-job catalogs and directory listings have bounded discovery limits; the result reports when a limit prevents complete discovery.
Directory browsing inspects at most 4,096 entries and displays at most 512 directories per listing.
Register exact log paths when a source is outside those bounded catalogs.

<a id="feature-24"></a>

## Align two streams by time

Enter `:logview split`. Raven chooses distinct registered stdout and stderr files. If those roles are absent, Raven uses the selected file and another registered file.

Use `:logalign on` to align reported ISO 8601 timestamps. Equal, unique instants occupy the same row. Events at different instants occupy separate rows. Zoned timestamps use UTC for comparison. Unzoned timestamps retain their wall-clock values; confirm that the sources use the same time zone.

Raven states when timing is missing, mixed, or backwards. In these cases, it shows positional rows. Repeated equal timestamps stay separate. An aligned row does not prove that two events have the same cause.

Use `:logalign off` for positional rows. Press `[` to select the left original source for copying. Press `]` to select the right original source.

Drag the grey separator between the two sources to change their widths.
Its one-cell capture buffer and keyboard focus use the shared divider controls.
Use `:pane-focus log:sources`, then Left or Right, for keyboard adjustment.
The split starts at 50 percent and stays between 20 and 80 percent.
This shorter divider has no diamond.
Changing its size preserves the exact original copy source and bytes.
See [Adjustable dividers](adaptive-workspaces.md#change-panel-size) for cancellation and saved preferences.

<a id="feature-25"></a>

## Compare two log files

Enter `:logdiff` to compare the registered pair. To choose a pair, enter:

```text
:logdiff LEFT_ID RIGHT_ID exact
:logdiff LEFT_ID RIGHT_ID ignore-time
```

Press `O` to open the file catalog. Source IDs appear in brackets after each file label. Raven refuses unknown IDs. Register files from different execution attempts in the same declared log inventory to compare them. Raven does not discover unrelated attempt files.

Removed lines have a minus sign. Added lines have a plus sign. Exact comparison preserves timestamp fields. `ignore-time` replaces the first reported ISO timestamp for comparison only. The original text remains available.

Press `[` or `]` to choose the exact original copy source. Press `o` on a diff row to open its underlying source line. Press Esc to return to the selected source. `Y` copies its complete file, subject to the normal clipboard or export result. It does not copy the diff panel.

<a id="feature-26"></a>

## Inspect structured log records

Enter `:logview json`. Use the arrows to select a node. Press Enter or Space to fold or expand the selected object or array.

To filter records by a field, enter:

```text
:logjson severity error
:logjson worker.rank 2
:logjson clear
```

Fields use dot-separated names. Numeric path elements select array indexes. Values use case-insensitive text containment. The header gives the count of filtered source lines. Plain lines remain visible when no field filter is active.

Press `o` on a structured row to return to its original source location. If the file changed or the location left the retained window, Raven reports that condition. Press `v` in the original view to select source lines. Press `y` to copy them.

<a id="feature-27"></a>

## Fold repeated messages

Enter `:logfold on`. Raven folds consecutive groups of at least three repeated messages. It ignores the first ISO timestamp when it checks repetition. Each summary states the original line range, repetition count, and hidden-line count.

Use the arrows to select a group. Press Enter or Space to expand it. Press the same key on an expanded group to fold it again. Enter `:logfold off` or press Esc to return to the original view.

Press `o` to inspect the original group. Selection and copying use original source bytes. A folded summary is never substituted for the source text.

<a id="feature-28"></a>

## Read new lines while following is paused

Scroll away from the end to pause following. The status line shows the number of unread retained lines as new data arrives. It counts a partial line once when that line becomes complete.

Enter `:logunread` to visit the first unread retained source line. Press End to resume following and clear the indicator. Replacement or truncation of the source resets the indicator. If the first unread line left the retained window, Raven reports that it cannot locate that line.

## Controls and limits

| Control | Function |
| --- | --- |
| Up / Down | Move the presentation cursor |
| Page Up / Page Down | Move by a viewport-sized step |
| Home / End | Select the first or last presentation row |
| Left / Right | Pan horizontally |
| `:logpan 0` | Return to the first display column |
| Enter / Space | Expand or fold a JSON node or repeated-message group |
| `[` / `]` | Select the left or right original source in paired views |
| `o` | Open the selected structured, folded, or diff row in its original source |
| Esc | Return to original source lines |
| `v` / `y` in a presentation | Return to the original view for source selection |
| `Y` | Copy the complete selected original file |
| Right-click | Clear selections and keep the exact source and presentation open |

Each presentation inspects at most 64 KiB and 240 original lines per source. The header states whether that inspection covers the complete source or a bounded tail. Line labels are relative when earlier data is omitted. Use full-file search and older pages for other content.

Structured records have bounded nesting and collection sizes. Invalid or oversized records stay as original text. Presentation preferences do not change file bytes. The same controls work with ASCII glyphs.

## Integration methods

The `tower.log_workbench` methods use the existing terminal event loop and the shared background reader.

| Method | Function |
| --- | --- |
| `initialize(app)` | Create bounded presentation state. |
| `restore(app, state)` / `save(app)` | Restore or save display preferences. Runtime readers and source paths are excluded. |
| `sync_source(app, path)` | Reset horizontal position when the exact source changes. |
| `command_names()` / `run_command(app, args)` | Register and execute presentation controls. |
| `visible_entries(app, entries)` | Apply file-group folds without changing source identities. |
| `handle_key(app, key)` / `handle_mouse(app, y, x, button, shift)` | Move the presentation cursor and prevent clicks through to hidden source rows. |
| `display_line(app, line)` | Apply horizontal panning in terminal display columns. |
| `status_label(app)` | Give visible horizontal-offset and unread indicators. |
| `observe_buffer(app, buf)` | Observe a published source buffer without file reads. Call this after publication. |
| `first_unread(app, buf)` | Move to the first unread location in the retained original source. |
| `render_browser(views, snap, app, width, height, rows, hits)` | Render the registered file catalog with cached metadata and source IDs. |
| `overlay(views, snap, app, width, height)` | Render the active alternate presentation. |
| `open_citation(app, citation)` | Choose a cited exact source. |
| `apply_citation(app, buf)` | Check source identity and locate the citation in a published buffer. |

The `tower.log_presentation` methods perform no file reads.

| Method | Function |
| --- | --- |
| `timestamp(line)` | Parse a reported ISO timestamp and its timing basis. Preserve up to nine fractional digits. |
| `aligned(left, right)` | Return source-index pairs and an explicit timing-status message. |
| `diff_rows(left, right, ignore_time=False)` | Return changed rows with their original left and right indexes. |
| `parse_json(line)` | Parse a bounded structured record, or return no structured value. |
| `field_value(value, path)` | Read a declared dot-separated field or array index. |
| `json_rows(value, collapsed, prefix, base_depth)` | Produce a bounded JSON tree for a log presentation. |
| `json_page(value, collapsed, start, limit)` | Produce a node page and a next-page indicator. |
| `structured_rows(lines, collapsed, field, query)` | Produce source-indexed structured rows and a filtered-line count. |
| `folded_rows(lines, expanded)` | Produce reversible repeated-message groups with exact source ranges. |

The `tower.log_bundle` methods implement full-source History exports.
Run discovery, directory operations, and copying on a background worker.

| Method | Function |
| --- | --- |
| `capture_jobs(job_ids, snap, ...)` | Validate and freeze exact job targets without file reads. |
| `discover_logs(request, ...)` | Resolve scheduler and registered project sources; report expected missing files and discovery limits. |
| `list_directories(root, current=None, ...)` | List bounded child directories beneath a pinned projects root. |
| `mkdir_directory(root, current, name, ...)` | Create one private child without overwrite or symlink traversal. |
| `export_logs(report, destination=None, ...)` | Stream complete raw sources into a unique manifest bundle and optionally deliver the labelled clipboard text. |

The `tower.history_log_export` methods connect these tasks to the terminal UI.

| Method | Function |
| --- | --- |
| `initialize(app)` / `active(app)` | Create bounded modal state and report whether an export dialog owns input. |
| `selected_jobs(app)` | Resolve marked or selected exact published History IDs. |
| `open_menu(app, job_ids=None)` | Freeze export targets and open the menu. |
| `command_names()` / `run_command(app, args)` | Register and execute `historylogs` commands. |
| `tick(app)` | Start queued work when the shared worker is available; reject stale UI contexts. |
| `cancel(app, close=False)` | Signal cancellation and retain published output receipts. |
| `paste(app, text)` | Insert literal folder-name text without interpreting commands. |
| `handle_key(app, key)` / `handle_mouse(app, y, x, ...)` | Navigate current modal entries and consume input before hidden page controls. |
| `overlay(views, snap, app, width, height)` | Paint a clipped dialog and publish its exact current hit geometry. |
| `controls(app)` | Publish visible modal buttons to the directional focus graph. |
