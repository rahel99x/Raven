# Scroll panes, select text, and choose a copy destination

[README](../../README.md) · [Controls](../CONTROLS.md) · [Charts](charts.md) · [Log sources](log-search.md)

Scroll pane content, select rendered text, and send copies to the clipboard or an existing editor.
These controls use Raven's published terminal content.
Pointer movement does not request extra scheduler samples or read source files.

## Scroll a pane

Scrollable panes have a right-edge rail and top/bottom arrows when the content exceeds the viewport.
The header reserves four cells at its left for those arrows.
The body reserves one cell at its right for the rail.
The controls do not add table columns or overlap the pane's text.

1. Point at the required pane's right-edge rail.
2. Press the left mouse button on its thumb.
3. Drag the thumb to the required position.
4. Release the button.

**Expected result:** That pane scrolls toward the requested position.
Dragging the scrollbar preserves the selected job, source, and line selection.
Click above or below the thumb to move by one viewport.
Use the wheel over the rail for smaller movements.
Click the header's up arrow to reach the top.
Click its down arrow to reach the bottom.
F8 navigation can focus those arrows. Press Enter to activate one.

Use the wheel inside a document pane to scroll that document.
Job tables and original log text retain their existing wheel and row-navigation controls.
Moving the scrollbar does not select the row beneath it.
Advisor, History, Timeline, and comparison documents can scroll through their complete published content.
Source adapters can still impose their documented data limits.

## Keep a drag stable during updates

New rows can arrive while a scrollbar drag is active.
A changed row count keeps the gesture attached to the same pane.
The thumb and scroll limits adapt to the new count.

A changed page, source, layout, pane geometry, or modal context cancels capture.
Its delayed release is consumed and cannot activate another control.
Esc also cancels an unfinished capture.

The viewport uses the existing bounded PID scroll controller.
Successive movements update its target instead of replaying old wheel positions.
Disable **Interface animations**, select the reader theme, or use `:smoothscroll off` for immediate movement.
These settings do not increase scheduler polling.

## Group selected jobs

Job marks identify scheduler jobs. Rendered line selections identify displayed text.
Use job marks when you want to organize a job list.

Press `y` to copy the selected job or marked jobs in the focused list.
The copy contains one tab-separated record per job: JOBID, NAME, STATE, PARTITION, ELAPSED, CPUS, GPUS, and INFO.
It includes marked rows outside the viewport, in list order, and excludes other panes.
A closed group identifies its representative job; hidden members are not selected automatically.
Raven checks job identity before copying. If that identity changed, select the current rows again.
Use `v` when you need the rendered table text instead of record fields.

1. Click a row in the required job list to focus that pane.
2. Mark at least two jobs with `Space`, plain click-drag, or click followed by Shift-click on the other end of the range.
3. Press `g`.
4. Open the resulting closed group with its down-pointing chevron when you need individual members.

**Expected result:** The marked jobs appear in one manual group with the existing state-count summary.
The group preference applies across job lists and their history browsers.
On Jobs, grouping can combine marked rows from Main and Recents. In another workspace it uses the focused list only.
Hidden members do not become selected action targets merely because their group is closed.

Select a closed group and press `u` to dissolve it.
To remove some members, open the group, mark those members, and press `u`.
With no marks, `u` detaches only the selected expanded member.
Use `U` to clear marks without changing groups.
`g` retains its ordinary beginning-of-list behavior when no eligible multi-job selection exists.
Menus, text selection, graph controls, and graph/slider drags retain their own input rules.
Graph `u` still undoes zoom and leaves job marks in another pane unchanged.
Click back into the job list before grouping a selection from that pane.
See [Manual groups](batch-launches.md#create-a-manual-group) for saved state and exact-job rules.

To add one job to an existing group, click and release its row, then Alt-drag it onto the group's closed summary, an expanded member, or its chevron.
Pressing a chevron directly retains its fold action and does not start a move.
To add marked jobs, hold Alt and start the drag on one of their marked rows.
Plain dragging always selects a range. Shift-drag retains earlier marks; Shift-click extends the range from the clicked anchor in either direction.
The source selection includes exact jobs only. A closed source summary does not include its hidden members.
Release on a group to commit, or press Esc, right-click, or release elsewhere to cancel.
Right-click a selected or marked row for **Create Group**, **Ungroup**, and applicable **Add to** choices.
History's menu also provides **Export logs**.
Manual membership uses natural job-ID order regardless of when members were added. Displayed rows follow each list's active sort, including its default sort.
See [Add jobs to a group](batch-launches.md#add-jobs-to-a-group) for destination identity and stale-input checks.

## Select rendered lines

Rendered selection copies the text displayed inside one pane.
It includes displayed columns and labels.
It excludes neighboring panes, box borders, and scrollbar rails.
Use source exports when you need content beyond the rendered view.

1. Point at text in Advisor or another document pane.
2. Press the left mouse button on the first required line.
3. Drag through the required lines.
4. Release the button.
5. Press `y`, or select **Edit → Copy**.

**Expected result:** Raven copies the selected rendered lines to the current copy destination.
Selected text remains pinned while new metric values arrive.
A source or view-context change invalidates the selection.
After invalidation, `y` asks you to select again. It does not copy another row or the entire screen.

| Control | Function |
| --- | --- |
| `v` | Start explicit rendered selection in the pointed or focused pane. |
| Up / Down | Extend the selection by one rendered line. |
| Page Up / Page Down | Extend by one pane viewport. |
| Home / End | Extend toward the published document's first or last line. |
| Shift-click | Extend the current selection to the clicked line. |
| `V` | Select the currently painted pane. |
| `y` or Edit → Copy | Copy the complete selected rendered range. |
| `Y` | Copy the focused painted pane, including when a smaller range is selected. |
| Esc / right-click | Clear the rendered selection. |

Job-row range selection marks exact job IDs. Alt-dragging a selected row or marked selection moves those exact jobs to an existing group.
Press `v` before selecting job-row text when you need rendered selection instead.
Raw Logs retain their original-byte `v`, `V`, `y`, and `Y` controls.
The existing source-page selection also retains its own controls.
See [log copying](log-view.md#clear-a-selection-without-changing-files) for source and dialog ownership.

## Check selection coverage

The rendered cache retains at most 20,000 lines and 8 MiB of UTF-8 text.
Cross-page keyboard selection can scroll the same published pane.
It cannot fabricate lines that have never been displayed.
If the range has missing cached lines, Raven refuses the copy and explains how to continue.
Scroll through the required range, narrow the selection, or export the source.
A failed coverage check sends no partial selection.
`V` selects the painted pane, not its undisplayed document or source file.

## Send text to Vim or Neovim

**Prerequisites:** A local editor is already running on the machine that runs Raven.
Its executable is available on `PATH`.
The editor exposes a supported local server.
Raven does not start an editor or modify an editor buffer.

1. Click **Copy** in the top toolbar to switch to **Yank**.
2. Select the required rendered text or original log content.
3. Press `y`, or use the existing complete-file copy control.
4. Check Raven's delivery notice.
5. Inspect register `0` in the editor with `:echo getreg('0')`.

**Expected result:** Successful delivery sets the editor's `0` and unnamed registers.
Paste with the editor's normal controls when required.
The toolbar uses compact `C` and `Y` labels in narrow terminals.
The Edit menu also provides **Switch to Vim/Neovim yanking** and **Switch to clipboard copying**.

For the compatibility `tower` launcher, set the launch preference in its existing configuration:

```json
{
  "clipboard": {
    "destination": "yank",
    "osc52": true,
    "tools": true
  }
}
```

The default destination is `copy`.
Copy operations capture destination and clipboard transport preferences before background work starts.
Complete-log copies and History multi-job log exports use the same destination setting.

### Connect to an existing Neovim server

Raven resolves `nvim` on `PATH`.
It checks `NVIM`, then `NVIM_LISTEN_ADDRESS`, for an absolute Unix socket owned by the current user.
An inherited editor terminal normally supplies the socket through `NVIM`.
Use `:echo v:servername` in the existing editor to inspect its server address.
Supply that existing socket to Raven's launch shell when it is not inherited.
Raven does not discover arbitrary sockets or infer a remote TCP connection.

### Select an existing Vim server

Raven resolves `vim` on `PATH` and requests `vim --serverlist`.
It selects one unambiguous running server.
When several servers exist, set `TOWER_VIM_SERVER` to the required exact server name before starting Raven.
That name must appear in the reported server list.
A Vim build without usable server support remains eligible for clipboard fallback.

### Check delivery limits and fallback

Server discovery has a 0.4-second timeout. Register delivery has a 0.8-second timeout.
Editor delivery accepts complete valid UTF-8 text of at most 8 MiB, without NUL bytes.
It preserves Unicode, CRLF, and trailing newlines.
The payload travels through a private file and a fixed register-setting expression.
The text is not interpreted as editor commands.

Unavailable servers, invalid text, excessive size, or delivery errors produce a stated fallback result.
Raven keeps the complete private export and attempts the enabled clipboard transports when applicable.
Invalid UTF-8 retains its raw export instead of replacement text.
An SSH session does not make a workstation editor a local editor on the cluster.
Use the terminal clipboard or exported file when the editor is on another machine.

## Resolve a navigation or copy problem

| Symptom | Action |
| --- | --- |
| No rail appears | Check whether the content exceeds the viewport and whether the pane has sufficient space. |
| A scrollbar selects a job | Check mouse press and release reports with `:terminaltest`. |
| Scrolling animation makes reading difficult | Use `:smoothscroll off` or disable Interface animations in Settings. |
| Dragging job rows marks jobs | Press `v` first when you need rendered job-row text. |
| Copy reports missing rendered lines | Scroll through the range, narrow it, or export the source. |
| Yank cannot find Neovim | Check `PATH` and the inherited owned Unix socket address. |
| Yank reports multiple Vim servers | Set `TOWER_VIM_SERVER` to one exact reported server name. |
| Editor delivery is rejected | Check the notice for the UTF-8, NUL, size, timeout, or server condition. Use the retained export. |

## Integration methods

These methods operate on published geometry and bounded display state.
`tower.editor_yank` performs the explicit local editor delivery.

| Method | Purpose and result |
| --- | --- |
| `scrollbars.begin_frame(app, ...)` | Start staging pane geometry for the outer frame. |
| `scrollbars.register(app, key, rect, count, page, target, painted, setter, ...)` | Stage one scrollable pane and its reserved header controls. |
| `scrollbars.mark(app)`, `take_since(app, first)`, `put_records(app, records)` | Preserve nested layout candidates without publishing discarded panes. |
| `scrollbars.map_records(records, ...)`, `place_since(app, first, ...)` | Transform and clip pane geometry through final layout. |
| `scrollbars.publish(app, width, height, overlays=())` | Publish visible panes and modal coverage. |
| `scrollbars.manual(app, key, context=None)`, `set_manual(app, key, target, context=())` | Read or set a source-scoped manual viewport. |
| `scrollbars.resume(app, key=None)` | Return a pane to its ordinary selection-following behavior. |
| `scrollbars.activate(app, key, direction)`, `keyboard_scroll(app, key, action)` | Apply endpoint or keyboard movement to a published pane. |
| `scrollbars.handle_mouse(app, y, x, button, shift)` | Apply rail, thumb, header-arrow, and eligible body-wheel gestures. |
| `scrollbars.cancel(app)`, `handle_key(app, key)` | Cancel capture and consume its delayed release. |
| `scrollbars.descriptors(app)`, `feedback(app, ascii_=False)` | Publish arrow controls and cached rail feedback. |
| `modal_scrollbars.window(app, key, target, count, page, ...)` | Compute a bounded dialog viewport without changing its selected item. |
| `modal_scrollbars.boxed(app, key, rendered, ...)` | Reserve and register controls inside the final dialog box. |
| `text_selection.publish(app, rows, width, height, overlays=())` | Cache painted pane text without reading source files. |
| `text_selection.handle_mouse(app, y, x, button, shift)`, `handle_key(app, key)` | Apply pane-local gestures and keyboard range selection. |
| `text_selection.active(app)`, `selected(app)`, `clear(app)` | Inspect or clear capture and selection state. |
| `text_selection.selection_text(app)`, `copy_selection(app)` | Validate complete cached coverage and copy its pinned rendered text. |
| `text_selection.feedback(app, ascii_=False)` | Paint the selected rows without rebuilding a document. |
| `editor_yank.discover(environ=None)` | Resolve an existing local editor target or return a stated reason. |
| `editor_yank.send(text, ...)`, `send_file(path, ...)` | Deliver complete validated text to registers `0` and unnamed. |
| `editor_yank.mode(app)`, `options(app)`, `toggle(app)` | Inspect or change the current copy destination. |
| `clipboard.options(app)` | Capture destination and transport preferences for one operation. |
| `clipboard.copy(text, ..., destination="copy", editor_context=None)` | Copy text or attempt editor delivery with clipboard fallback. |
| `clipboard.copy_file(path, ..., destination="copy", editor_context=None)` | Deliver a complete export without sending a truncated prefix. |
