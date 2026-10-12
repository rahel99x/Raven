# Artifact inspection

[README](../../README.md) · [Project reporting](../PROJECT_STANDARD.md) · [Live workbench](live-workbench.md)

Inspect declared text, CSV, and JSON outputs without modifying their source files.
The browser preserves each artifact's run identity, source path, and inspected byte range.

**Prerequisites:** Select a project run with an output contract, or attach a standalone contract and root.
Use the local file adapter for local outputs or the supported SSH adapter for remote outputs.
The SSH reader requires `python3` on the selected host.

<a id="feature-50"></a>

## Open a declared output

1. Select a project with `:project PATH`.
2. Select a run with `:run select RUN_ID`.
3. Enter `:outputs`.
4. Select a declared file.
5. Press Enter.

For a standalone output contract, attach it with `:artifacts CONTRACT ROOT`, then enter `:outputs`. Press Enter on a directory to expand or fold it. Press Left to fold its parent. Use `/` to filter the declared output list.

**Expected result:** The preview identifies the declared file and the inspected byte range.
Raven anchors each read to the contract's root and rejects symlinks, special files, and traversal paths.
It verifies file identity before publishing a page.
For SSH connections, paths refer to the selected remote host; a remote read does not fall back to a local path.

## Read adjacent pages

Use Up, Down, Page Up, and Page Down to scroll the loaded page. Press `]` to read the next page in the background. Press `[` to read the previous page. These commands do the same operations:

```text
:artifact next
:artifact prev
:artifact refresh
:artifact text
:artifact structured
```

Refresh returns to the first page. `text` shows original text pages for a structured file. `structured` restores its declared format. These controls can inspect values inside a tree branch that reaches a display limit. Each page states its byte coverage. Pages retain exact byte positions. If a file changes between pages, Raven keeps the previous published page and asks you to reopen the file. Press Esc to return to the same output-list selection.

Text pages contain at most 128 display rows and approximately 256 KiB. An oversized line can continue on the next page. Byte boundaries preserve UTF-8 characters. Binary and invalid UTF-8 content have no text preview.

## Choose CSV columns

CSV pages preserve complete quoted records, including fields with newlines. The header stays visible when you scroll.

Use Left and Right to choose a header. The selected header has brackets. Press `c` to show or hide that column. At least one column remains visible. To choose columns directly, enter:

```text
:artifact columns id loss epoch
:artifact columns 1 3
:artifact columns all
```

Column names are exact. Column numbers start at one. Quote names that contain spaces. Raven retains the original record number when it sorts data.

## Sort CSV records

Choose a header with Left or Right. Press Enter to cycle ascending, descending, and off. The header shows `^` for ascending and `v` for descending.

```text
:artifact sort loss asc
:artifact sort loss desc
:artifact sort loss off
```

Raven sorts the complete inspected dataset before it divides that dataset into pages. Numeric values use numeric order. Text uses natural order. Missing values remain last in either direction. Off restores original file order.

Global sorting supports files of at most 8 MiB and 50,000 data records. Raven refuses a sort that exceeds these bounds. It does not apply a partial sort. Use unsorted pages to read a larger file. Each page contains at most 128 complete records. Oversized or invalid CSV records produce an inspection error.

## Expand JSON values

Structured JSON inspection supports files of at most 8 MiB. Use Up and Down to select a node. Press Enter or Space to fold or expand an object or array.

To change a node directly, use its JSON pointer:

```text
:artifact json /worker
:artifact json /results/0
:artifact json /
```

The last command changes the root node. JSON pointer escaping uses `~1` for `/` and `~0` for `~`. Each tree page displays at most 128 nodes. Use `[` and `]` to read adjacent node pages. A fold or expansion returns to the first page of the changed tree. Tree nesting is limited to 24 levels. A node path is limited to approximately 4096 characters. Raven labels branches that reach those bounds. Files above the structured-size limit use text pages, with a visible explanation.

## Preserve source evidence

Artifact inspection is read-only. Column visibility, sorting, and tree expansion change the presentation. They do not edit output files or contracts. File paths, run identity, byte coverage, and inspection errors remain visible.

## Integration methods

The `tower.artifact_pages.read_page(root, specification, ...)` method reads one stable declared output page. Its keyword controls are `files`, `offset`, `row`, `columns`, `sort`, and `collapsed`. Column indexes start at zero in this API. A sort is `(column_index, "asc"|"desc")`; `None` retains original order. The result includes source identity, byte coverage, original record indexes, next-page positions, and presentation rows. Run this method on the background worker.

The `tower.project_ui` methods connect inventories and pages to the terminal UI.

| Method | Function |
| --- | --- |
| `initialize(app)` | Create bounded project-picker and artifact state. |
| `restore(app, data)` / `save(app)` | Restore or save project and run identities. Revalidation is required before binding. |
| `selected_binding(app)` | Get the current validated run binding. |
| `log_entries(app)` | Get cached log declarations for the selected run. |
| `resolve_log_entry(app)` | Resolve the exact selected declared log source. |
| `cycle_log_entry(app)` | Select the next cached source and preserve its reading position. |
| `clear_binding(app)` | Leave a run and restore the previous research and log settings. |
| `command_names()` / `run_command(app, args)` | Register and execute project, run, output, and artifact controls. |
| `handle_key(app, key)` | Navigate run inventories, output lists, and artifact pages. |
| `overlay(views, snap, app, width, height)` | Render the active project or artifact overlay from published state. |
