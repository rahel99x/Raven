# Mouse and button navigation

[README](../../README.md) · [Controls](../CONTROLS.md) · [Live workbench](live-workbench.md)

Use the mouse or directional keyboard focus to select Raven's visible controls.
This guide covers pointer feedback, graph gestures, selection, scrolling, themes, and the startup display.

**Prerequisites:** Run Raven in an interactive terminal.
Mouse gestures require the terminal to report their press, movement, and release events.
Use the keyboard alternatives when those reports are unavailable.

## Check terminal mouse support

1. Start Raven in an interactive terminal.
2. Move the pointer over a page label or button.
3. Check that the control highlights.
4. Click the control to activate it.
5. Open `:terminaltest` if the terminal does not report the expected input.
6. Press Esc to leave the test.

**Expected result:** The pointer highlights the control beneath it without changing the selected job or file.
Hover feedback requires a terminal that reports pointer movement.
Clicks and drag reports also depend on the terminal's mouse support.
The `mouse` configuration field must be `true`.

Raven updates pointer feedback from the last published page.
It paints changed cells while data, actions, and resize still refresh the page.
Continuous movement does not postpone live results.
See [Display and input performance](ui-performance.md) for validation and connection limits.

SGR, legacy X10, and urxvt mouse reports remain pointer input.
Their coordinate bytes cannot activate keyboard shortcuts.
Clicks require the current published pane and job identity.
After a pane changes, allow its next display update before clicking its content.
See [the input audit](input-audit.md) for tested failure cases.

Run `:terminaldoctor` to inspect terminal settings and connection evidence.
Check the terminal emulator and any intervening SSH or tmux session when events are missing.
Use keyboard controls when the terminal cannot report pointer movement.
An ordinary SSH connection can carry supported terminal mouse events.

## Select controls with the keyboard

Raven builds directional connections between the visible controls in the current display.
The connections follow the controls' positions and groups.
They update when the page, layout, menu, or terminal size changes.

1. Press F8 to focus the visible buttons and links.
2. Use arrow keys to select a nearby control in that direction.
3. Press Enter or Space to activate the focused control.
4. Press Esc to return arrow keys to the page content.

**Expected result:** A visible focus indicator identifies the control that Enter will activate.
Arrow movement does not activate a button or link.
In Jobs and Recents, focusing a real job row also selects that exact job for Details.
Home and End select the first and last visible controls.
Tab and Shift-Tab traverse the visible controls in order while button focus is active.

Use `:focusbuttons on` or `:focusbuttons off` to select the same focus mode.
Use `:focusbuttons` to toggle it.
Click an ordinary button to keep button focus available for arrow navigation.
Click content or scroll to restore the content's normal controls.
Menus retain their F10 navigation and the slider retains its own keyboard controls.

Focus reaches controls that are visible in the current frame.
Scroll the content or use its page controls to reach further items.
The same activation rules apply to mouse clicks and keyboard activation.
Job-changing controls still require their normal review.

In Jobs, hover a Queue or Recents row and press F8 to begin at that real job.
Up and Down follow rows in the current table.
At a viewport edge, they scroll or load the next available row and refresh the visible controls.
Right moves from a job row into visible Details controls; Left returns toward Main.
This pane crossing also works when Details is stacked below Main.
Click a Jobs or Recents row to return arrow navigation to that list, including after a range selection or Details click.
When Details controls have focus, press Esc once to return arrows to Main.
Use F6 or Ctrl-W to switch pane focus. Marks stay selected during these focus changes.
Scrolling, sorting, filtering, and resizing update the graph before another action can use the old row positions.
Moving focus into Quick Advisor does not start its calculation; activate that button to request it.

Click a column-editor checkbox to show or hide its optional column.
Required identity and state columns stay visible.
Use the editor's width and order controls to adjust those required fields.

Click a Chart Events or Timeline row to open its exact painted job or log citation.
Use `:timeline open EVENT_NUMBER` for the numbered current timeline event.
Event numbers start at one.
The existing `:timeline seek EVENT_NUMBER` command remains the replay-seeking operation.

## Point at and zoom a graph

Move the pointer inside a metric plot to show its thin crosshair in the active theme's accent color.
Press the left button, drag across a time interval, and release inside the plot.
Raven fits the selected interval across the plot and calculates the vertical scale from its visible curve.
The crosshair preserves the graph background.
Unicode strokes use two horizontal and four vertical Braille positions per cell.
The crosshair reaches the latest reported cell immediately. Only its dot phase animates, for at most 24 ms.
Curve and annotation glyphs keep their original styles where the selector intersects them.
Mouse events and selected bounds still use whole terminal cells.
ASCII and reader modes use static dots and `+` intersections.
Disable **Interface animations** in `:settings` to keep the Unicode selector static.
Hold Shift before pressing to select explicit horizontal and vertical bounds instead.
Time labels use `s`, `ms`, or `us` as required and identify the selected start timestamp.
Use `u` or `0` while pointing at the same graph to undo or reset rectangular zoom.
Right-click inside the plot to restore its full view and turn off that metric's Live window.
An active drag includes the axis labels and tick row, then extends three cells beyond them.
This capture margin is clipped to the actual visible pane.
Movement and release in the margin use the nearest plot edge.
The initial press must be inside the plot.
Both painted axis mappings remain fixed while the sampler publishes new data.
Automatic sample updates do not cancel the gesture.
Moving beyond it cancels the preview, and a later release has no effect.
Esc before release discards the preview.
A changed job, source, page, axis mode, layout, menu, or terminal size also cancels it.
The operation changes display bounds and preserves measured values.
See [Graph interaction](charts.md#zoom-a-time-interval) for keyboard commands, source scope, logarithmic axes, and limits.

## Open refresh controls

1. Click the toolbar's **Queue · Recordings** summary.
2. Choose the queue's Faster or Slower control, or select a recording-read interval.
3. Inspect the effective value and source freshness before closing the panel.

**Expected result:** The chosen reader interval changes without changing the
selected job, marks, or viewport. The summary is a button: dragging over it
does not change a rate or activate a control beneath the pointer after it opens.
The underlying queue range follows the active base and site policy.
Managed Light starts at 60–6 seconds; Default Heavy starts at 5–0.5 seconds.
Request budgets, demand, command duration, and retry backoff can defer a read.

The compatibility command `:rate N` accepts positions from one to 50.
Managed sessions change queue cadence only; native compatibility sessions
retain their existing rate-command behavior.
See [Update rate](live-workbench.md#set-the-update-rate) for source limits and saved preferences.

## Resize a panel by dragging

Press the grey divider or one cell beside it.
Keep the left mouse button pressed, move it to the required position, then release it.
The full vertical divider has a centred blue diamond.
The horizontal divider above Recents changes its share of Jobs Main.
Press Esc during the drag to restore the starting size.
Page, dialog, and terminal-size changes also end capture and restore that size.

Click a divider to focus keyboard adjustment.
Use arrows along its movement axis, or Page Up and Page Down, then Enter or Esc to return to the page.
F8 navigation and `:pane-focus KEY` can focus the same control.
See [Panel dividers](adaptive-workspaces.md#change-panel-size) for complete commands, layout bounds, and persistence.

## Use a menu for several changes

1. Click File, Edit, View, Setup, or Help.
2. Move the pointer over a menu choice.
3. Click a choice to activate it.
4. Keep the pointer inside the dropdown to select another available choice.
5. Move the pointer outside the dropdown to dismiss it.

**Expected result:** A direct setting or navigation choice leaves the menu available for the next change.
An editable prompt or action review remains beneath the dropdown until you dismiss the menu.
Then use that dialog's controls to complete or cancel the operation.
Esc and F10 also dismiss a menu.

The toolbar label row also permits switching between menus without dismissal.
Clicks or releases outside the dropdown do not activate a hidden page control.
The menu's hover highlight identifies the choice beneath the pointer.
Hover does not execute that choice.
Use the menu's Up and Down keys to select choices without a mouse.
See [Toolbar menus](live-workbench.md#use-the-terminal-toolbar) for every menu choice.

## Mark several jobs by dragging

1. Press the left mouse button on a visible job row. Hold Shift if you need to retain earlier marks.
2. Keep the button pressed and move through the required rows.
3. Release the button to complete the marked range.
4. Press `c` when you need to review cancellation of the marked jobs.
5. Check every target ID before confirming the review.

**Expected result:** The marked range follows the table's current displayed order.
Sorting and filtering define that order.
A simple click continues to select one row without starting a range.
Shift-drag adds the range to existing marks, including when the starting row is selected or marked.
Plain drag always marks a range, including from selected or marked rows.
You can also click one row and Shift-click another to mark the inclusive range in either direction. The anchor stays in that pane.
Hold Alt while dragging selected jobs to add them to a group.
Press Esc before release to cancel capture and restore the previous marks.
Runtime header changes do not add a job beneath a stationary pointer.
Resizing the terminal stops the drag and keeps its marked job IDs.

Marking jobs does not cancel them.
Supported group actions use the existing marked IDs and their normal review.
Range dragging is available for job rows in Jobs, Recents, History, Group, and Dependencies.
The terminal must report mouse press, movement, and release for a drag.
Use Space to mark individual jobs when those events are unavailable.

In History, Shift-drag through visible job rows to mark a range.
Right-click a selected or marked row and choose **Export logs** to open the log-export menu for those exact jobs.
Right-click elsewhere inside its job list retains the existing direct export control.
Right-click outside that list to clear the selection without activating another control.
See [Export History logs](log-view.md#export-logs-for-history-jobs) for clipboard and directory procedures.

## Drag jobs into a group

Click and release a job row, then **Alt-drag** it onto an existing group's closed summary or an expanded member.
For several jobs, mark them first, then Alt-drag one of the marked rows. Plain drag retains range selection.
The destination highlight and footer identify the move while the button is held.
Release over the group to apply the membership change.
Drop targets can be in another visible job pane on the same page.
Releasing over the destination group's chevron also adds the jobs.
Pressing that chevron directly still folds or opens its group and does not start a move.
Other buttons, dividers, and scrollbar rails retain their normal controls.

A closed source summary supplies its exact representative only.
Expand a source group and mark its members to move them together.
Press Esc, right-click, or release outside an eligible group to cancel the move and retain marks.
A changed view or execution attempt cancels the gesture; a delayed release cannot activate another control.
Right-click a selected or marked row, including its group chevron, for **Create Group**, **Ungroup**, and applicable **Add to** choices when dragging is inconvenient.
See [Add jobs to a group](batch-launches.md#add-jobs-to-a-group) for ordering, identity, and menu controls.

## Clear selections with right-click

Right-click outside a selected or marked job row on a main page to clear job selections, all marks, and line selections, subject to the controls below.
This works in raw Logs, alternate log views, file browsers, and docked history browsers.
The viewed log source and its display mode remain open.
The click does not activate a page label, button, link, or other control beneath it.
Clearing cancels unfinished drags and prevents their delayed releases from committing an action.
The cleared state remains through ordinary display and sampler updates.
Click a job or line, or use its navigation keys, to select again.

These main-page targets retain their specific right-click controls:

| Target | Right-click result |
| --- | --- |
| Metric plot | Reset that graph to its full view and turn off its Live window. Keep the selected job and marks. |
| Selected or marked job row | Open its job menu. A marked row uses applicable marks; an unmarked selected row uses that exact job. History adds **Export logs**. One cancelled job in Recents or History adds **Rerun cancelled job**, which opens inspection and a separate action review. |
| Elsewhere inside the History job list | Open the log-export menu for the exact marked or selected jobs. |
| An unfinished group move | Cancel the move and retain marks. |

Right-click elsewhere in History clears selections without opening that menu.
In a Log Tools page, results list, or bookmarks list, right-click clears only the local selection.
That dialog and its exact source stay open.
Cursor-line copies, result opening, and bookmark deletion require a new explicit selection.
Click a row or use navigation keys to select again.
Use `Y` for an explicit complete-file copy from a source page.

Help, Details, and analysis dialogs also keep their own right-click clearing scope.
They clear carried text or row selections and retain the selected job, marks, source, and dialog.
Analysis graphs still use right-click for full-view reset.
Outside those plots, analysis clears sample, Timeline, and Chart Events row selections.
Select a row again before opening an event or starting a sample interval.
Use its arrow keys or click the row.
Research Evidence uses the shared line-selection state on its main page.

## Scroll with pane controls

Scrollable panes reserve four header cells for top/bottom arrows and one right-edge cell for the scrollbar.
Drag the scrollbar thumb to move within that pane.
Click anywhere on its track to seek directly to that position, then drag to adjust it.
The pane moves only while the pointer is on its visible track. Releasing outside the track ends the drag without scrolling.
Use the header arrows to reach the document endpoints.
These gestures preserve the selected source and do not activate rows under the rail.
A changed content count keeps the drag active; changed source or geometry cancels it.
Advisor, History, Timeline, and comparison documents expose their complete published content through their pane viewport.
See [Pane navigation](pane-navigation.md) for keyboard access, cancellation, rendered text selection, and editor yanking.

## Scroll without delayed input

In compact native Analytics Job series, point at the metric content to scroll its document.
Point at Job history to scroll the history browser.
Page Up/Down scroll the metric document; ordinary Up/Down and Home/End change the job.
Use `:series-scroll home` or `:series-scroll end` for the document endpoints.
A slider or directional button focus uses its own keys while active.

Mouse-wheel scrolling moves the viewport toward the latest requested position.
Successive wheel events update that target.
Raven does not replay a long queue of old scroll movements after reaching an edge.
Changing direction updates the target immediately.

Keyboard arrows, page keys, Home, End, clicks, and copy operations retain immediate, exact positioning.
Scrolling does not increase the scheduler fetching rate.
Use the update slider separately when you need more frequent data sampling.

Use `:smoothscroll off` to disable viewport interpolation.
Use `:smoothscroll on` to restore it.
Use `:smoothscroll` or `:smoothscroll toggle` to change the current preference.
The View menu provides Enable smooth scrolling or Disable smooth scrolling.
The compatibility configuration's top-level `smooth_scrolling` field sets its launch default.
Normal UI state retains the preference for the current profile and connection.

The reader theme and disabled **Interface animations** use immediate viewport movement.
These settings preserve mouse-wheel scrolling.
Static operation is useful when a terminal connection has limited display throughput.

## Change the terminal palette

Press `T` to cycle the available themes, or enter `:theme NAME` for an exact choice.
The active palette updates the canvas, text, menus, information strips, and charts during the session.
Blank areas use the same canvas as the page.
Theme changes preserve the selected job and measured values.

| Theme | Palette |
| --- | --- |
| `darcula` | Grey canvas with blue and warm accents |
| `modnokai` | Dark olive canvas with vivid accents; `monokai` is an alias |
| `gruvbox-dark` | Dark neutral canvas with warm accents; `gruvbox` is an alias |
| `dark`, `light` | Explicit dark or light canvas and matching text |
| `terminal` | The terminal's own background |
| `mono`, `reader` | Colour-free output on the terminal's background |

Use `high` for high contrast or `cb` for the colour-blind palette.
The reader theme also uses plain ASCII text and static notices.
`--no-color` and `NO_COLOR` suppress colour even when a named theme is selected.
The terminal's colour capacity determines the available approximation.

## Control the startup display

Raven can show a short Unicode welcome animation when an interactive curses session starts.
The ASCII mode uses a compact static welcome.
One-frame output, JSON, scripts, and ANSI watch output do not show this display.

Use `:startup off` to disable the startup display.
Use `:startup on` to enable it.
Use `:startup toggle` to change the preference.
Use `:startup` to inspect the preference without changing it.
Use `:startup preview` to view it without changing the saved preference.
The View menu provides Enable startup animation, Disable startup animation, and Preview startup animation.

The compatibility configuration's top-level `startup_animation` field sets the launch default.
Managed Raven launches disable the startup display by default.
Normal UI state retains the enabled preference for the current profile and connection.
The reader theme and disabled **Interface animations** suppress the startup display.
Very small terminals also skip it.
The Unicode welcome lasts less than one second.
ASCII mode shows its static welcome for about half a second.

Press a key, click, or use the wheel to dismiss the display immediately.
Raven then performs that input's normal action.
Pointer movement alone leaves the display running.
The welcome does not change sampler intervals or delay job actions behind an animation queue.

## Selection integration methods

The `raven.job_selection` methods operate on published job identities and terminal display state.
They do not read log files or send scheduler actions.

| Method | Purpose and result |
| --- | --- |
| `initialize(app)` | Create bounded drag and explicit-clearing state. |
| `selected(app, tab, identifier)` | Keep an explicitly cleared job table deselected during maintenance frames. |
| `resume(app, tab=None)` | Restore job selection after a deliberate row gesture. |
| `cleared(app, tab=None)` | Report whether job selection is explicitly cleared for that page. |
| `lines_cleared(app)` | Report whether line selection is explicitly cleared. |
| `resume_lines(app)` | Restore a line cursor without implicitly selecting a job. |
| `clear_lines(app)` | Clear text and log cursors while preserving job marks, source, and dialog ownership. |
| `clear(app)` | Clear job marks and line selections, cancel captures, and preserve the viewed source. |
| `context_click(app, y, x, button="left")` | Preserve graph, selected-job menu, and History-export priority; apply main-page clearing or supported dialog-local clearing. |
| `publish(app, rows, hits, width, height)` | Publish the clipped History job-list rectangle for its export priority. |
| `active(app)`, `tick(app)`, `handle_key(app, key)` | Check, validate, or cancel an unfinished job-range drag. |
| `handle_mouse(app, y, x, button="left", shift=False)` | Select exact visible job IDs during a range drag. |

## Resolve an input problem

| Symptom | Action |
| --- | --- |
| Clicks work, but controls do not highlight under the pointer | Check movement reports with `:terminaltest`; the terminal must support pointer tracking |
| Slider clicks work, but dragging does not | Check drag reports; use `:rate N` or keyboard slider controls |
| Dragging does not mark job rows | Check press, movement, and release reports; use Space to mark individual jobs |
| Arrow keys move between buttons instead of content | Press Esc or `:focusbuttons off` |
| Arrow keys move content instead of buttons | Press F8 or `:focusbuttons on` |
| A menu hides before another toggle | Keep the pointer inside the visible dropdown; dismiss it before editing a prompt or completing a review |
| Scrolling produces too many intermediate frames | Use `:smoothscroll off` or the reader theme |
| The welcome does not appear | Check `:startup`, the theme, `animations`, terminal size, and launch mode |

Use [Controls](../CONTROLS.md) for complete key and command syntax.
Use [Live workbench](live-workbench.md) for inline Jobs views and persistent menu procedures.
