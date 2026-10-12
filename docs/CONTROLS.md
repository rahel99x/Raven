# Tower controls

## Raven execution controls

`ravenl` opens Light; `ravenh` requests an admitted worker before loading the UI.
`tower` remains a compatibility route to Light. Open the Light/Heavy toolbar
control or enter `:raven inspect` for mode, target, collection rates, queues, and
retention coverage. Request, Cancel, and Release use a current identity-bound
review. `:raven reuse JOBID` starts a fresh ownership/resource review; it does not
trust a job number alone. Single/Multi controls background concurrency separately.

Details and Terminal share one focus indicator. Switching execution modes does
not change the selected job, active terminal, or destination of queued paste.
See the [Raven runbook](raven/README.md) for profiles, limits, migration, and site
qualification boundaries.


Press Ctrl-C to exit Tower when Tower owns keyboard input. A configured Ctrl-C binding takes precedence.
When an embedded terminal owns input, Ctrl-C goes to its foreground program.
Press Ctrl-], then t, to return input to Tower.
The terminal test and keybinding tester record this key without exiting.
Press Esc to leave either test.

[README](../README.md) · [Feature guides](QUALITY_OF_LIFE.md) · [Reference](reference.md)

## Embedded terminal

Select **Details** or **Terminal** in Jobs, History, Analytics, Dependencies,
Logs or Research. The optional engine must be installed. Hover does not assign
keyboard focus. Use **Tools** for session, workflow and evidence actions.
If a short screen hides the pane buttons, use **View → Open embedded terminal**
or `:terminal show`.

| Control | Action |
| --- | --- |
| Ctrl-], then t | Return keyboard control to Tower |
| Ctrl-], then Ctrl-] | Send a literal Ctrl-] to the child |
| `:terminal show` / `:terminal details` | Select the secondary-pane view |
| `:terminal new` / `:terminal sessions` | Create or choose a named shell |
| `:terminal menu` | Open Tools; arrows and Enter select an action |
| Copy | Enter or leave terminal copy mode |
| v, then arrows, then y in copy mode | Select and copy retained lines |
| Mouse drag in copy mode | Select terminal text without sending a child drag |
| /, then n in copy mode | Search and move to the next result |
| Home / End in copy mode | Move to the first or last retained line |
| Esc in a terminal dialog or copy mode | Close that mode; do not exit the shell |
| `:terminal close` | Terminate the selected owned session; Details only hides it |

Normal shell `y`, `q`, `g`, `:`, arrows and Escape go to the child. Terminal copy
uses the existing Copy/Yank destination setting. Read the
[terminal guide](guides/terminal-fusion.md) and
[workflow guide](guides/terminal-workflows.md) for all commands and limits.

This guide lists launch methods, default keys, mouse actions, and command families.
Use `?` inside Tower for contextual help.
Use the feature guides for complete procedures and operating limits.
Use [Mouse and button navigation](guides/pointer-navigation.md) for hover feedback, directional focus, dragging, and motion preferences.
Use [Adjustable workspaces and job advice](guides/adaptive-workspaces.md) for dividers, Recents, launch groups, history panels, and Quick Advisor.

In compact native **Analytics → Job series**, Up, Down, Home, and End change the job.
The mouse wheel, Page Up, and Page Down scroll its metric document.
Use `:series-scroll home` or `:series-scroll end` to reach its first or last rows.
The job heading stays visible. A changed job returns the document to the top.
Point at Job history to scroll that browser instead.
Slider, browser, and directional button focus use their own keys while active.

## Control notation

A leading `:` means: open the command palette, then type the command.
Do not type the leading `:` after the palette opens.
`JOBID`, `FILE`, `DIR`, `NAME`, and `VALUE` are values that you supply.
Square brackets in command syntax identify optional arguments.
They are not characters to type unless the guide explicitly describes a key.
`A` and `a` identify different keys.

An overlay uses its own controls while it is open.
Its visible instructions take priority over page shortcuts.
In Help, Details, and analysis dialogs, right-click clears local text or row selections.
It preserves the selected job, marks, source, and dialog.
Right-click on an analysis graph retains its graph-reset priority.
Cleared sample and event rows require deliberate navigation before a row action.
Use `Esc` to return from an overlay.
Key bindings in configuration can replace the defaults below.

## Launch methods

| Method | Command | Purpose |
| --- | --- | --- |
| Checkout launcher | `./scripts/tower` | Use the checkout's virtual environment when available |
| Python module | `python3 -m tower` | Run directly from the checkout |
| Bash alias | `tower` | Use the launcher installed with `scripts/install_shell.py` |
| Installed entry point | `tower` | Use the Python package installed in the active environment |
| Demonstration | `tower --fake` | Run the simulated scheduler |
| One frame | `tower --once --tab history` | Print a page and exit |
| Updating text screen | `tower --watch` | Refresh an ANSI screen; Ctrl-C exits |
| Scripted command | `tower run COMMAND ARGUMENTS` | Execute one palette command |
| Bash Research reporting | `tower report COMMAND ARGUMENTS` | Publish job-linked reports; explicit `submit` launches a tracked Slurm job |

Run `type tower` to identify which shell command will run.
Use the [runbook](runbook.md#use-one-tower-shell-command) to install or inspect the managed alias.
Use [Research reporting from Bash](guides/research-automation.md) for `init`,
`submit`, `start`, `exec`, `finish`, `metric`, `research`, `log`, `passport`,
`artifact`, and `aggregate`. These are command-line subcommands, not palette commands.

### Background worker mode

Click the toolbar worker switch, or use `:workers single`, `:workers multi`,
`:workers toggle`, and `:workers status`. F8 includes the switch in directional
button navigation. Single keeps one background worker and a separate UI thread.
Start with `tower --workers single` or `tower --workers multi` to override the
saved preference. See [Background workers](guides/background-workers.md) for
pending transitions, result collection, and limits.

### Select a local setup profile

| Command | Function |
| --- | --- |
| `python3 scripts/setup.py --mode local` | Set up the default CARC profile |
| `python3 scripts/setup.py --mode local --profile desktop` | Set up local desktop Slurm settings |
| `python3 scripts/install_shell.py --profile desktop` | Preview the desktop alias |
| `python3 scripts/install_shell.py --profile desktop --apply` | Back up `.bashrc` and install the desktop alias |

Source the updated `.bashrc` to load the alias.
Use `tower --version`, `tower --doctor`, and `tower --once --tab sources` to inspect the installation.
Use the [Fedora desktop guide](DESKTOP.md) when Slurm is installed directly on your desktop.
Use the [runbook](runbook.md#3-adapt-setup-to-your-environment) for other setup modes.

## Global navigation keys

| Key | Function |
| --- | --- |
| `Tab`, `]` | Open the next page |
| `Shift-Tab`, `[` | Open the previous page |
| Up, `k` | Move one row or line up |
| Down, `j` | Move one row or line down |
| Page Up, Page Down | Move by the current view's page size |
| Home, `g` | Move to the beginning; `g` groups two or more marked jobs when a job list has focus |
| End, `G` | Move to the end |
| Left, `,`; Right, `.` | Change subviews in Analytics, Nodes, and Research |
| `Ctrl-W`, F6 | Focus Main or Details |
| `z` | Maximize the focused panel outside Logs |
| `Ctrl-B`, Alt-Left | Restore the previous location |
| Alt-Right | Restore the next location after Back |
| `Ctrl-G` | Open universal jump search |
| `Ctrl-P` | Open the Research workspace picker |
| `Ctrl-A` | Open Activity |
| F10 | Open or close the terminal toolbar menu |
| F8 | Toggle directional focus for visible buttons and links |
| `:` | Open the command palette |
| `/` | Filter the current list or search the current log |
| `?` | Open contextual searchable help |
| `Esc` | Close an overlay, end a selection, or clear the current filter or marks |
| `q` | Exit Tower |

Use `:maximize` for the panel layout action on Logs.
The [navigation guide](guides/navigation.md) describes Forward, saved locations, jump search, settings, and command editing.

## Page shortcuts

| Key | Page | Command identifier |
| --- | --- | --- |
| `1` | Jobs | `jobs` |
| `2` | Cluster | `cluster` |
| `3` | History | `history` |
| `4` | Nodes | `nodes` |
| `5` | Logs | `log` |
| `6` | Sources | `sources` |
| `7` | Analytics | `analytics` |
| `8` | Group | `group` |
| `9` | Dependencies | `deps` |
| `0` | Research | `research` |

Use `:tab IDENTIFIER` to open a page by command.
Use `:workspace NAME` to open a Research workspace.
Use `:view NAME` to select an Analytics or Research subview.

## Terminal toolbar and update slider

The first display row contains `x`, File, Edit, View, Help, the copy destination, and the update-rate control.
The toolbar remains available above pages and overlays.
Menu actions use the same commands and reviews as their keyboard equivalents.

| Control | Function |
| --- | --- |
| Click a menu label, F10, or `:menu [File\|Edit\|View\|Help]` | Open a menu |
| Left / Right, Tab / Shift-Tab in a menu | Select the previous or next menu |
| `f`, `e`, `v`, `h` in a menu | Select File, Edit, View, or Help |
| Up / Down, page keys, Home / End in a menu | Select a menu choice |
| Enter / Space, or click a menu choice | Activate the choice |
| Esc, `q`, F10, or move the pointer outside the dropdown | Dismiss the menu |
| Click `x` | Exit Tower |
| Click Copy / Yank | Switch between clipboard copying and a running local Vim/Neovim target |
| Edit → Switch to Vim/Neovim yanking / Switch to clipboard copying | Change the same copy destination |
| Click or drag the update track | Request a fetching interval; the label shows the effective Jobs interval |
| Right-click the update track, interval, or step buttons | Restore five-second polling and preserve selections |
| Click `[-]` / `[+]`, or use the wheel over the update control | Move one polling position toward five seconds or 500 milliseconds |
| Click the interval, or View → Focus update-rate slider | Focus slider keyboard controls |
| Arrows, `-` / `+` with slider focus | Move one polling position |
| Home / End with slider focus | Request five seconds or 500 milliseconds |
| Esc / Enter / Tab with slider focus | Return input to the page |
| `:rate [N\|reset]` | Inspect the effective Jobs interval, select position 1 to 50, or restore five seconds |
| `:about` | Open version and toolbar instructions |

A menu choice ending in `...` opens an editable command prompt.
Unavailable choices report their required context.
Direct choices keep the dropdown available while the pointer stays inside it.
An input prompt or action review remains beneath the dropdown.
Dismiss the menu before editing that prompt or completing the review.
Hover highlights a control without activating it.
See [Live workbench](guides/live-workbench.md#use-the-terminal-toolbar) for every menu choice.
See [Update rate](guides/live-workbench.md#set-the-update-rate) for source limits and saved preferences.

### Mouse and directional button focus

| Control | Function |
| --- | --- |
| Move the pointer over a button, menu choice, or link | Highlight that visible control |
| F8, `:focusbuttons`, or `:focusbuttons on` | Enter directional button focus |
| Arrow keys with button focus | Select the nearest visible control in that direction |
| Up / Down with Jobs or Recents row focus | Select real jobs and scroll or load the next row at the viewport edge |
| Right from a Jobs or Recents row with button focus | Move into visible Details controls |
| Tab / Shift-Tab with button focus | Select the next or previous visible control |
| Home / End with button focus | Select the first or last visible control |
| Enter / Space with button focus | Activate the focused control |
| Esc or `:focusbuttons off` | Restore normal content controls |
| Click content or scroll | Restore content navigation |
| `:smoothscroll [on\|off\|toggle]` | Change mouse-wheel viewport smoothing; no argument toggles it |
| `:startup [on\|off\|toggle\|preview]` | Change or preview the welcome display; no argument reports the setting |

Mouse hover and drag require movement reports from the terminal.
Use `:terminaltest` to inspect reported keys and mouse events.
Keyboard navigation remains available when those reports are missing.
Jobs row focus selects that exact job for Details without activating a Details button.
Viewport changes publish fresh row controls before another action can use their positions.
The reader theme and `animations = false` use immediate scrolling and skip the welcome.
See [Mouse and button navigation](guides/pointer-navigation.md) for procedures, saved preferences, and troubleshooting.

### Terminal palette

| Control | Function |
| --- | --- |
| `T` | Cycle the available themes |
| `:theme NAME` | Select an exact palette during the session |
| `:theme darcula` | Select Darcula |
| `:theme modnokai`, `:theme monokai` | Select Modnokai; Monokai is an alias |
| `:theme gruvbox-dark`, `:theme gruvbox` | Select Gruvbox Dark |
| `:theme terminal` | Retain the terminal's own background |
| `:theme mono`, `:theme reader` | Use colour-free output; reader also uses plain ASCII |

The active theme updates the canvas, blank cells, text, menus, information strips, and chart colours.
Theme changes preserve job selection and measured values.
All interface symbols use the active palette, including sliders, clocks, hourglasses, arrows, diamonds, and selection markers.
Status symbols retain their warning, error, and success meanings.
Use `dark`, `light`, `high`, or `cb` for the other explicit display palettes.
Use `--no-color` or `NO_COLOR` to suppress colour.
See [Terminal palettes](guides/pointer-navigation.md#change-the-terminal-palette) for controls and terminal limits.

### Adjustable panel dividers

| Control | Function |
| --- | --- |
| Press a divider or one cell beside it, drag, and release | Change the two panel sizes and retain the completed size |
| Esc during a divider drag | Restore the previous size |
| Click a divider, or activate it through F8 focus | Focus its keyboard size controls |
| Arrows along the movement axis with divider focus | Change the split by two percentage points |
| Page Up / Page Down with divider focus | Change the split by ten percentage points |
| Enter / Esc with divider focus | Return input to the page |
| `:pane-focus KEY` | Focus the named divider |
| `:pane-resize KEY smaller\|larger` | Adjust the named divider |
| `:layout split N` | Set Main's split percentage from 20 to 80 |

Jobs Main and Details use the divider key `workspace:jobs`; History uses `workspace:history`.
Queue and Recents use `recent:jobs`; paired-source Logs uses `log:sources`.
Dockable history uses `history:analytics`, `history:deps`, `history:log`, or `history:research`.
A full vertical divider has a blue diamond at its centre.
Horizontal and shorter vertical dividers use a plain line.
Page, dialog, and terminal-size changes stop a divider drag and restore its starting size.
See [Panel size](guides/adaptive-workspaces.md#change-panel-size) for layout bounds and ASCII characters.

## Navigation, settings, and input

| Command | Function |
| --- | --- |
| `:jump [QUERY]` | Search cached jobs, runs, logs, views, workspaces, and commands |
| `:forward` | Restore the next location after Back |
| `:location [list]` | Browse saved destinations |
| `:location save NAME` | Save the current destination |
| `:location open NAME` | Open a saved destination |
| `:location delete NAME` | Remove a saved destination |
| `:settings [reset]` | Open settings or preview defaults |
| `:keybindings [reset]` | Open key bindings or preview defaults |
| `:explain [TABLE] COLUMN` | Inspect a field's units, meaning, scope, and unavailable state |
| `:peek [TABLE] COLUMN` | Inspect an unclipped underlying field value |

In settings, Up/Down selects a setting.
Left/Right changes its value.
Space changes a Boolean value.
Use `D` to preview defaults.
Enter applies the draft.
Esc cancels the draft.
The Polling interval row controls the native global request from five seconds to 500 milliseconds.
Separate live, GPU, and trace interval rows are not shown.
Esc restores the exact previous polling position; unaccepted previews are not saved.
See [Settings](guides/navigation.md#32-change-settings-with-a-preview) for source bases and launch overrides.

In key bindings, `e` or Enter edits the selected action.
Use `t` to test a key.
Use `a` to apply the draft.
Use `D` to preview defaults.
Esc cancels the draft.

In Peek, `y` copies the complete underlying value.
Explain and Peek use the focused header when one is available.
Otherwise, the default field is `name`.
See [Navigation](guides/navigation.md) for supported fields and exact value semantics.

### Command palette editing

| Key | Function |
| --- | --- |
| Left / Right | Move the input cursor |
| Home / End, Ctrl-A / Ctrl-E | Move to the input start or end |
| Ctrl-Left / Ctrl-Right, Alt-B / Alt-F | Move by words |
| Backspace / Delete | Remove a character |
| Ctrl-W, Alt-Backspace | Remove the previous word |
| Alt-D | Remove the next word |
| Ctrl-U / Ctrl-K | Remove input before or after the cursor |
| Alt-U / Ctrl-Z | Undo an input change |
| Alt-R / Ctrl-Y | Redo an input change |
| Up / Down | Select a suggestion |
| Tab | Insert a completion |
| Page Up / Page Down | Recall command history |
| Enter | Run the edited command |
| Esc | Cancel input |

Bracketed paste inserts editable input.
It does not execute pasted commands automatically.
Input is limited to 4096 characters.
See [Navigation](guides/navigation.md#feature-35) for multiline input and quoting.

## Job controls

| Key | Function |
| --- | --- |
| `Space` | Mark or unmark the selected job |
| `a` | Mark all visible jobs |
| `g` | Create a closed manual group from two or more marked jobs in the focused job list |
| `u` | In the focused job list, dissolve the selected closed group or detach marked or selected expanded members; clear marks when no grouped target applies |
| `U` | Clear job marks without changing groups |
| `p` | Pin or unpin selected or marked jobs |
| `Enter`, `d` | Open the selected job's details |
| `i` | Open scheduler details and steps |
| `I` | Open the structured job inspector |
| `l` | Open the exact selected job's logs |
| `A` | Start a clone-and-resubmit command |
| `c` | Review cancellation |
| `h` | Review hold or release |
| `R` | Review requeue |
| `t` | Review priority within your pending jobs |
| `s` | Resume the current page's legacy single-column sort |
| `S` | Reverse the legacy sort |
| `=` / `_` | Increase or decrease the accounting and Analytics day window |

Marked jobs are the target of supported group actions.
When no jobs are marked, the selected job is the target.
Click one job, then Shift-click another to mark the inclusive range in either direction.
Repeated Shift-clicks adjust the range from the original anchor and preserve unrelated marks.
Press, drag, and release through job rows to mark a range in the current table order.
This works from unselected, selected, and already marked rows.
Hold Shift during a range drag to retain earlier marks.
Hold Alt when pressing a job row to drag that job into an existing group. Alt-drag a marked row to add the applicable marked jobs.
Plain dragging always selects rows; it does not start a membership move.
The destination can be a closed summary, an expanded member, or that group's chevron. A closed source summary contributes only its representative job.
Pressing a chevron directly retains its fold action and does not start a move.
Release outside an eligible group, press Esc, or right-click to cancel a group move.
Press Esc during capture to cancel the drag and restore earlier marks.
Release the mouse button to complete the range before opening a job action.
Press `g` to group the marked jobs in the focused list.
On Jobs, this includes marked rows from both Main and Recents. Other workspaces use the focused list only.
Open the group, mark specific members, and press `u` to detach those members.
Select its closed summary and press `u` to dissolve the group.
The membership keys and Alt-drag change Tower's display organization only.
If the terminal intercepts Alt-drag, use the equivalent right-click group menu.
See [Manual groups](guides/batch-launches.md#create-a-manual-group) for focus, identity, and persistence rules.
Right-click a selected or marked job, including its group chevron, for **Create Group** (at least two targets), **Ungroup** (grouped targets), and applicable **Add to GROUP NAME** choices.
Right-clicking a marked row targets applicable marks; an unmarked selected row targets that exact job and retains unrelated marks.
The menu shows the three most recent eligible **Add to** groups. **More** expands
the list; **Cancel** remains last. Recency uses the latest member submission
time, with natural job ID order as the fallback.
History's job menu also provides **Export logs**.
For a single cancelled job in Recents or History, **Rerun cancelled job** opens
background inspection, followed by **Review action** and **Confirm apply**.
`:rerun [JOBID]` opens the same procedure. It requeues the retained individual
submission under the same JobID; it does not restart the launch group.
Read [Rerun one cancelled job](guides/operations-cluster.md#rerun-one-cancelled-job)
for retention, array-task, dependency, and output requirements.
In this menu, arrows, Tab, Shift-Tab, page keys, Home, and End select a choice; Enter or Space activates it. The wheel scrolls, and Esc, `q`, or Ctrl-C closes it.
Manual membership uses natural job-ID order; displayed rows retain each list's active sort, including its default sort.
Right-click a metric graph to restore its full view and keep the selected job and marks.
Right-click outside selected job rows on a main page to clear job selections, marks, and line selections, subject to History's export control below.
It keeps the viewed log source open and does not activate the surface beneath the pointer.
This also applies to raw Logs, alternate log views, and browser panes.
Click a job or line, or use its navigation keys, to select again.
Click a Jobs or Recents row to return arrow navigation from Details to that list.
When Details controls have focus, one Esc returns to Main; F6 or Ctrl-W switches panes.
Focus changes preserve marks. Right-click clearing cancels unfinished drags and consumes their later releases.
In History, right-click a selected or marked row and choose **Export logs** to open the log-export menu.
Right-click elsewhere inside that job list retains the direct log-export control.
Right-click outside that list to clear the selection without activating another surface.
Inspect the complete target list in the action review.
Use Tab to choose Cancel or Confirm.
Press Enter to activate the selected review control.
For ordinary action reviews, `y` confirms and `n` or `Esc` cancels.
Use `:inspect section Overview\|Resources\|Steps\|Files\|Evidence` to select a section in an open structured inspector.
The inspector's section buttons also accept mouse clicks.

Click column headings for cascading sorts.
Drag a heading's thin separator to resize that column. Right-click the separator to restore automatic width.
Press Esc during the resize to restore the previous width. A completed drag saves once.
The separator buffer stays outside adjacent heading text; the last fitted column can use an edge handle.
Jobs Main and Recents share widths and column order. Their sorts and filters remain independent.
Use `:sortby [TABLE] COLUMN [asc|desc|off]` for the equivalent command.
Use `:sortby [TABLE] clear` to remove every rule in that table.
See [Tables](guides/tables.md) for sort editing, column controls, filters, marks, and drill-down.
The fixed six-cell Progress column precedes JOBID and supports `:sortby jobs progress asc\|desc\|off`.
The marking and fold gutter remains separate to its left.
The field contains one narrow symbol, a space, and four fractional block cells.
`▸` identifies a published application completion fraction; an animated clock identifies time-limit usage.
Pending jobs show an animated narrow hourglass and `wait`.
ASCII uses static `p`, `t`, and `w` symbols.
See [Job progress](guides/adaptive-workspaces.md#read-the-six-cell-progress-column) for exact source fields and unknown states.
Recents leaves live-only progress and measurement slots empty. The completion cue travels through Main's left gutter.

### Jobs Main grouping and queue controls

| Control | Function |
| --- | --- |
| **Auto Group**, `:grouping auto` | Enable automatic detection and close its groups; keep manual groups and exclusions |
| **Undo Auto**, `:grouping undo` | Undo the last Auto Group change without replacing later manual changes |
| **Ungroup All**, `:grouping clear` | Remove manual groups and disable automatic grouping; keep all jobs |
| **Expand All**, `:grouping expand` | Open all groups without changing membership |
| **Collapse All**, `:grouping collapse` | Close groups without changing membership |
| `:grouping restore` | Restore the last Ungroup All change if later grouping edits would not be overwritten |
| Drag a group header's `↕` (`:` in ASCII) onto another Main group | Request placement before an earlier group or after a later group; release opens inspection and review |
| Esc or right-click during a queue-order drag | Cancel without changing scheduler order |
| `:ops queue-order` | Open the queue-order form without a mouse |

The five grouping buttons change shared display organization only. Their undo records last for the current session.
The queue-order handle is separate from the fold chevron and Alt-drag membership control.
Only its header follows the pointer; expanded members are dimmed during the drag.
After dropping, inspect **Review action**, then select **Confirm apply** to request a scheduler change.
Both groups need pending jobs owned by the connected user in the same partition, account, and QOS.
The site must enable Slurm's `enable_user_top` capability. Running and completed members are excluded.
A held or priority-one job in either selected group blocks the request.
The review checks up to 512 exact pending jobs, including affected peers owned by that same user.
It does not change another user's jobs, resources, or dependencies. Actual starts still depend on scheduler policy and available resources.
A changed queue invalidates the review. See [Queue-order procedure](guides/tables.md#review-a-groups-queue-order).

### Jobs Details buttons

Jobs and History provide Inspector, Logs, Investigate, Research, Analytics, Quick Advisor, and Off buttons in Details.
The buttons inspect the selected active, recent, or historical job without leaving its page.

| Control | Function |
| --- | --- |
| Click a Details button | Select its mode and enter visible-control graph focus |
| Arrows, then Enter / Space in graph focus | Focus a nearby control, then activate it |
| Esc / F8 in graph focus | Restore native panel navigation |
| Arrow keys after `:jobpanel focus` | Select and activate the previous or next Details mode |
| Home / End after `:jobpanel focus` | Select Inspector or Off |
| Enter / Tab in native panel focus | Change focus between buttons and content; Research and Analytics include their subview buttons |
| Up / Down, page keys with content focus | Scroll inspection content or retained log lines |
| Left / Right with Logs content focus | Select the previous or next exact log file |
| Home / End with Logs content focus | Reach the oldest retained line or follow the tail |
| Click a log source button | Read that file; click its content or leave graph focus to use native scroll keys |
| Click Research or Analytics, then a subview | Show that workspace inside Details |
| Wheel over Details with content visible | Scroll that view vertically |
| Esc | Return input to the job rows |
| `:jobpanel [inspector\|logs\|investigate\|research\|analytics\|quick\|off\|focus]` | Select a mode or focus the buttons without a mouse |
| `:jobpanel research VIEW` | Select one of the twelve inline Research workspaces |
| `:jobpanel analytics VIEW` | Select Job series, History, Timeline, Advisor, or Compare |

Use the full Logs page for complete-file search and copying.
Off hides inspection content and starts no new log or evidence reads.
Quick Advisor starts a background calculation only after explicit activation.
Use Analyze this job in an idle restored panel, Refresh analysis for newer evidence, or Cancel for an unfinished request.
Changing the selected job or leaving the mode discards its later result.
Inline views fit the panel width and retain an independent vertical position for each subview.
Jobs and History save separate mode, Research subview, and Analytics subview preferences.
History-list right-click retains log export, through **Export logs** on a selected row's job menu; Details right-click clears selections, except that a graph keeps its zoom-reset action.
The full Research, Analytics, and Logs pages keep their own selection and position.
See [Live workbench](guides/live-workbench.md#inspect-a-job-inside-jobs) for procedures and layout behavior on Jobs and History.

## Table inspection

### Dockable job history

Analytics, Dependencies, Logs, and Research include a job-history panel.
Its choices target the exact available active, recent, or completed job.

| Control | Function |
| --- | --- |
| Click a Job history choice | Open that exact job's data in the page |
| Drag `⠿` (`::` in ASCII) to a page edge, then release | Dock history as a column or horizontal strip |
| Esc during a history-handle drag | Retain its previous dock |
| Click Dock | Cycle the available dock positions |
| Click the history `x` or its remaining show control | Hide or restore the history panel |
| `:history-dock auto\|left\|right\|top\|bottom\|next\|off` | Choose, cycle, or hide its dock |
| `:history-browser [on\|off]` | Toggle, show, or hide the browser |
| `:history-focus` | Give the browser keyboard focus |
| Arrows, page keys, Home / End with browser focus | Select and activate a history job |
| Esc with browser focus | Return input to the page data |
| `:history-job JOBID` | Activate an exact available job |
| `:advisor-job JOBID` | Select a published running-job row in Analytics → Advisor without changing the view |
| `:history-scroll up\|down\|page-up\|page-down\|home\|end` | Scroll history without activating another job |

Use the panel's divider to change its share of the page.
Each page retains its own dock preference when normal UI state is enabled.
See [Job history panels](guides/adaptive-workspaces.md#keep-job-history-beside-the-data) for compact layouts and source identity.

### History log exports

| Control | Function |
| --- | --- |
| Press, drag, and release through History rows | Mark exact jobs in the current table order, including from selected rows |
| Click one History row, then Shift-click another | Mark the inclusive range in either direction; retain unrelated marks |
| Right-click a selected or marked History row, then **Export logs** | Open the clipboard, directory, and Cancel menu for the targeted jobs |
| Right-click elsewhere inside the History job list | Open the existing export menu directly for marked or selected jobs |
| `:historylogs` | Open that export menu without a mouse |
| `:historylogs clipboard` | Discover and copy complete selected jobs' log outputs to the clipboard |
| `:historylogs directory` | Open the confined project-directory picker |
| `:historylogs cancel` | Cancel the open log-export procedure |
| Picker Up / Down, page keys, Home / End | Select a listed directory or control |
| Picker Tab / Shift-Tab, then Enter | Select and activate a picker control |
| Picker Backspace / Left, or **Parent folder** | Return toward the configured projects root |
| **New folder...**, type a name, Enter | Create one child folder in the current directory |
| **Open named folder...**, type a name, Enter | Open an existing child by its exact name, including one omitted by a listing limit |
| **Save here** | Export all discovered complete raw sources and their manifest to a unique bundle |
| **Cancel** / Esc | Cancel the dialog or active export; Esc in the folder-name prompt returns to the picker |

The picker root is `exports.projects_root`, then the registered native project root, then `~/projects`.
Missing expected sources produce a per-job alert before publication.
**Export available logs** explicitly permits the listed omissions and records them in the manifest.
Clipboard transport limits preserve the raw bundle and produce a stated result.
See [History log exports](guides/log-view.md#export-logs-for-history-jobs) for source scope, directory procedures, and limits.

### Table commands

Use a table identifier when you need a table other than the current one.
Identifiers include `jobs`, `recent`, `history`, `group`, `nodes`, `sources`, and `cluster`.

| Command | Function |
| --- | --- |
| `:sorteditor [TABLE]` | Open sort priority editing |
| `:headers [TABLE]` | Open keyboard header controls |
| `:columns [TABLE]` | Open column order, width, and visibility controls |
| `:columns [TABLE] order KEYS` | Set column order |
| `:columns [TABLE] width KEY WIDTH` | Set a width from two to one hundred twenty terminal cells, or `auto`; Progress stays at six |
| `:columns [TABLE] show\|hide KEYS` | Change optional column visibility |
| `:columns [TABLE] reset` | Restore default column preferences |
| `:where [TABLE] CONDITION...` | Apply numeric resource conditions |
| `:where [TABLE] clear` | Clear numeric conditions |
| `:filters [TABLE]` | Open the interactive field filter builder |
| `:viewpicker [TABLE]` | Preview and select a saved table view |
| `:historyrange today\|yesterday\|week\|all` | Select a named History date interval |
| `:historyrange START END` | Select inclusive local calendar dates |
| `:recents 5\|10\|25\|auto` | Select the initial Recents preview size |
| `:recents expand\|collapse` | Change the Recents presentation |
| `:jobgroups [on\|off]` | Toggle shared group visibility; explicit `on` also enables automatic detection |
| `:grouping auto\|undo\|clear\|expand\|collapse\|restore` | Use the Jobs Main grouping buttons and their guarded session restore |
| `:jobgroup toggle\|open\|close GROUP_ID` | Fold or expand one known launch group |
| `:jobgroup create` | Group at least two marked real jobs in the focused list |
| `:jobgroup ungroup` | Dissolve targeted closed groups or detach targeted expanded members |
| `:jobgroup reset` | Clear all saved and session manual groups and automatic-group exclusions; retain the current grouping on/off setting |
| `:recents window DURATION\|all` | Select the completion time window |
| `:marked [all\|hidden\|visible\|active\|finished]` | Inspect a subset of marked jobs |
| `:freeze [on\|off]` | Hold or resume the inspection view |
| `:jobactions [JOBID]` | Open actions for the exact job |
| `:node NODE_NAME` | Open cached node details |
| `:drill partition NAME` | Inspect jobs in a partition |
| `:drill user NAME` | Inspect an account user's jobs |

Numeric conditions combine with AND.
For example, `:where jobs cpus>=8 memory>16GiB cpu_eff<30%` requires every condition to match.
Use the [table guide](guides/tables.md) for supported fields, units, and unknown values.

Drag the divider above Recents to change its visible size.
The wheel inside Recents scrolls that list.
Click a recent job, then use arrows, page keys, Home, or End to reach older matching history.
The initial preview size does not cap that navigation.

Click the right-pointing chevron on the first visible row of an expanded launch group to close it.
Click the down-pointing chevron on its closed summary row to open it again.
When the original first member is above the viewport, the first visible member carries the disclosure control.
The existing summary formatting and state counts stay available while the group is closed.
Fold preferences remain shared across the job listings and their history browsers.
With a grouped job selected and ordinary page focus, Left closes its group and Right opens it.
Closed groups retain one real representative ID.
Manual groups use the same chevrons and state summaries.
`g` creates one from at least two marked jobs; `u` removes the selected group or its selected expanded members.
Alt-drag a job row or marked selection onto a group row to add those exact jobs.
Right-click the selected row for equivalent group creation, removal, and destination choices.
Manual membership uses natural job-ID order. Displayed rows retain each list's active sort, including its default sort.
Use `U` when you only want to clear marks.
These membership keys belong to the focused job list. Graph `u` still undoes zoom; text and menu controls retain their own input behavior.
Their INFO badges use `run`, `pend`, `dep`, `never`, `done`, `fail`, `cancel`, and `other` for nonzero counts.
The counts are disjoint and cover matching observed records; compressed array ranges remain records.
See [Automatic launch groups](guides/batch-launches.md) for the badge meanings, historical coverage, and an explicit batch-launch procedure.
Grouping does not add hidden members to a marked range or a job-action target list.
See [Launch groups](guides/adaptive-workspaces.md#fold-related-launches) for deduction evidence and exact-action rules.

In the sort editor, Up/Down selects a rule.
Left/Right changes its priority.
Space changes its direction.
Use `d` to remove a rule.
Use `a` to add a rule.

In header controls, Enter or Space cycles ascending, descending, and off.
Left selects ascending order.
Right selects descending order.

In column controls, Left/Right changes column order.
Use `+` and `-` to change width.
Use `a` for automatic width.
Use `r` to reset column order, widths, and visibility.
Space changes optional visibility.
Click a visible column checkbox to show or hide that optional column.
Hover feedback and F8 navigation use the same checkbox control.
Required identity and state columns remain visible.
Adjust their widths or order when you need more space.
Enter closes the overlay.
You can also drag the thin table-header separator or its adjacent gap to preview a width.
Right-click that separator to reset its width. Esc cancels an unfinished drag.
Page, modal, terminal-size, and incompatible layout changes also cancel the preview.
`:columns recent` opens the Jobs editor because Queue and Recents share their column layout.
Explicit Recents column commands use the equivalent Jobs field. Use History for completed-only columns.

In the filter builder, Enter edits a field.
An empty field removes its condition.
Use `c` to clear conditions.
Use Ctrl-U to clear the current edit.
Click a filter chip to remove its condition.

In saved views, Enter loads the selected view.
Use `d` to delete a view.
In marked jobs, `s` changes the subset, `d` removes one mark, and `c` clears the displayed subset.
Enter opens details.
Use `l` to open the selected marked job's exact logs.

## Log controls

| Key | Function |
| --- | --- |
| `O` | Open the grouped file list |
| Up, Down, page keys | Select a file or move the logical line cursor |
| `Enter` | Open the selected file |
| `Esc` | Return to the file list, then close it |
| `o` | Cycle the registered files |
| `e` | Select stdout or stderr |
| `f` | Change following state |
| End | Follow the end unless extending a selection |
| `w` | Change line wrapping |
| `L` | Open the current log in `less` |
| `/` | Search retained content |
| `N` / `P` | Move to the next or previous retained-content match |
| `v` | Start selecting original log lines at the cursor |
| `V` | Select the entire file |
| `y` | Copy the original selection |
| `Y` | Copy the entire exact selected source |
| `m` | Add a basic bookmark at the cursor |
| `'` | Visit the next basic bookmark |
| `+` / `-` | Increase or decrease the inline job log preview |

Shift-click extends a line selection.
Right-click clears job and line selections without changing the open source or log view.
In a Log Tools page, results list, or bookmarks list, right-click clears only the local selection.
That dialog stays open. Click a line or use navigation keys to select again.
After clearing, copying a cursor line or opening a result requires an explicit new selection.
The far-right marker identifies selected lines.
Display transformations do not replace original copy content.
Use `:copy all` for a full-file copy through a background worker.

### Complete-file search and source paging

| Command | Function |
| --- | --- |
| `:logolder [BYTE_POSITION]` | Read a bounded source page before the retained window |
| `:logsearch [--all] [--literal\|--regex] [--case\|--nocase] [--word] [--] QUERY` | Search a complete file or the registered source set |
| `:logsearchmode literal\|regex [case\|nocase] [word\|partial]` | Change retained-content search behavior |
| `:logresults` | Open the latest complete-file results |
| `:loggoto line N` | Open an absolute source line; line numbers start at one |
| `:loggoto byte N` | Open a source byte offset |
| `:loggoto percent PERCENT` | Open a source percentage between zero and one hundred |
| `:loggoto time ISO_TIMESTAMP` | Find a timestamped source location |
| `:logmark NAME` | Name the current source position |
| `:logmarks` | Open named source bookmarks |

In a source page, `[` and `]` load the previous or next file page.
In results and bookmark lists, Enter opens the exact selected source.
In a bookmark list, Delete or `d` removes the selected bookmark.
Use `Esc` to restore the originating results or retained-tail view.
See [Log search](guides/log-search.md) for coverage, cancellation, regex limits, and remote behavior.

### Structured, folded, and paired log views

| Command | Function |
| --- | --- |
| `:logview plain\|split\|json\|diff\|fold` | Select a presentation |
| `:logalign on\|off` | Change timestamp alignment in split view |
| `:logdiff [LEFT_ID RIGHT_ID] [exact\|ignore-time]` | Compare two registered sources |
| `:logjson FIELD VALUE` | Filter a dot-separated JSON field by text |
| `:logjson clear` | Remove the JSON field condition |
| `:logfold on\|off` | Fold or expand repeated messages |
| `:logunread` | Visit the first unread retained line |
| `:logpan OFFSET` | Change horizontal display offset |
| `:logpan 0` | Restore the left edge |
| `:loggroup GROUP` | Fold or expand a file-list group |
| `:loggroup all` | Expand every file-list group |
| `:logpreview on\|off` | Change file-list previews |

In JSON and folded views, Enter or Space expands the selected item.
In paired views, `[` and `]` choose the original copy source.
Use `o` to open the original row from a structured or folded view.
Use `Esc` to return to original lines.
Use `v` to return to original lines before selecting raw text.
Use `Y` for the complete exact source.
See [Log display](guides/log-view.md) for inspected-source coverage and timestamp limitations.

## Pane scrolling and rendered selection

Scrollable panes reserve four left header cells for top/bottom arrows and one right-edge cell for the scrollbar.
Drag its thumb to scroll that pane. Click above or below the thumb for a viewport-sized step.
Click the header arrows for the pane's endpoints, or reach them with F8 and Enter.
These controls preserve the selected source and do not activate rows under the rail.
Count growth keeps the current gesture. Changed source, page, geometry, layout, or modal context cancels it.
A cancelled gesture consumes its delayed release.
Advisor, History, Timeline, and comparison documents expose their complete published content through their viewport.
The reader theme, disabled animations, and `:smoothscroll off` use immediate movement.

Drag through ordinary rendered text to select lines in one pane.
Use `v` to start explicit text selection, then arrows, page keys, Home, or End to extend it.
Shift-click extends the range. `V` selects the currently painted pane.
Press `y` or use Edit → Copy to copy the selected text.
For a native job selection, `y` copies only the selected or marked jobs in the focused list.
The copy uses tab-separated record fields. Use `v` first to copy the rendered table text instead.
Selections do not include neighboring panes, and a lost selection never becomes a whole-screen copy.
Esc or right-click clears it.
Job dragging retains its exact-ID marking behavior unless explicit rendered selection starts with `v`.
Right-click a group header or its chevron to open the group menu in either expanded or collapsed state.
The menu lists the group name and target count, includes offscreen matching members, and respects the pane's filters.
On an expanded first-job row, marked data cells retain the marked-job menu; the chevron selects the group context explicitly.
Raw Logs and source-page dialogs retain their original-byte selection controls.
The rendered cache is limited to 20,000 lines and 8 MiB.
Missing displayed coverage causes a stated failure; no partial range is copied.
See [Pane navigation](guides/pane-navigation.md) for procedures, source scope, and editor delivery.

## Copy and export controls

| Key or command | Function |
| --- | --- |
| `v`, `V`, `y` outside raw Logs | Select rendered lines, select the painted pane, and copy the selected text or native rows |
| `Y` outside raw Logs | Copy the focused painted pane; Operations and Telemetry copy their complete report |
| `E` | Export the current page as text |
| `C` | Export the current table as CSV |
| `J` | Export selected or marked jobs and their recorded series as JSON |
| `:export text\|csv\|json\|report` | Export a page, table, jobs, or complete dashboard |
| `:copy` | Copy the current selection |
| `:copy FIRST LAST` | Copy a range of loaded log lines or screen rows; indices start at one |
| `:copy all` | Copy the entire log source, or the focused painted pane outside Logs |
| `:activity` | Inspect retained notices and background tasks |
| `:task cancel` | Request cancellation of supported background tasks |

The [operations guide](guides/operations.md) describes notice search and the export library.
A terminal can reject or limit clipboard requests.
Inspect the result notice and private export path after a large copy.
Copy is the default destination. Yank uses a running local Vim or Neovim server.
The toolbar uses compact `C` / `Y` labels when space is limited.
Editor delivery sets registers `0` and unnamed without opening an editor or changing a buffer.
`clipboard.destination` accepts `copy` or `yank`.
An inherited owned Unix socket identifies Neovim; one reported server or `TOWER_VIM_SERVER` identifies Vim.
Discovery has a 0.4-second timeout; delivery has a 0.8-second timeout.
Editor payloads must be complete UTF-8 without NUL bytes and no larger than 8 MiB.
Failures keep the private export and attempt enabled clipboard fallback.
Complete-log and History multi-job exports use the captured copy destination and transport preferences.

## Activity, diagnostics, completions, and alerts

| Command | Function |
| --- | --- |
| `:activity filter TEXT` | Search notice text and supported field terms |
| `:activity level VALUE` | Select notice severity |
| `:activity job JOBID` | Search job identity |
| `:activity task TEXT` | Search task labels |
| `:activity persistent on\|off` | Change optional restart retention |
| `:activity clear` | Remove retained notices |
| `:exports [show]` | Open the export library |
| `:exports filter TEXT` | Search export labels and paths |
| `:exports preview ID_OR_INDEX` | Read a bounded preview |
| `:exports copy ID_OR_INDEX` | Copy a path |
| `:exports label ID_OR_INDEX NAME` | Label a record |
| `:exports forget ID_OR_INDEX` | Remove metadata for one export |
| `:exports clear` | Remove export metadata |
| `:terminaldoctor` | Inspect terminal and path metadata |
| `:terminaltest` | Test glyphs, colors, keys, and mouse events |
| `:terminaltest clipboard` | Request delivery of explicit test text |
| `:inbox [unread\|all\|failed]` | Select completion records |
| `:inbox filter TEXT` | Search completion names, IDs, states, and partitions |
| `:inbox ack JOBID\|all` | Review records in the current filtered list |
| `:alerts [show]` | Inspect configured alert conditions and delivery controls |
| `:alerts snooze RULE SECONDS [JOBID]` | Snooze a rule or one exact job |
| `:alerts unsnooze [RULE [JOBID]]` | Remove snoozes |
| `:alerts quiet START END [ZONE]` | Set daily quiet hours |
| `:alerts quiet off` | Disable quiet hours |

In Activity, `/` edits the filter, `f` clears it, and `e` opens exports.
Enter expands a notice.
Use `y` to copy its path or text.
Use `c` to request background task cancellation.

In exports, Enter opens a preview, `y` copies a path, and `d` forgets a record.
Forgetting metadata preserves the exported file.
Export indices start at one in the current filtered list.

In terminal diagnostics, `t` opens input tests and `r` refreshes evidence.
In input tests, only Esc exits.
Other keys and mouse events are recorded without running job actions.

In the inbox, `u` selects unreviewed, `f` selects failed, and `a` selects all records.
Enter opens the exact job in History.
Use `l` for its logs, `r` to review one record, and `A` to review the filtered list.

In alert controls, `s` snoozes the selected rule for thirty minutes.
Use `u` to remove its general snooze.
Use `Q` to disable quiet hours.
Quiet times use `HH:MM` in the selected IANA zone.
An omitted zone uses `America/Los_Angeles`.
Muted delivery preserves active conditions and events.
See [Operations](guides/operations.md) for precise persistence, limits, and troubleshooting.

## Chart inspection

| Command | Function |
| --- | --- |
| `:chart METRIC` | Open exact sample inspection |
| `:chart preset 5m\|30m\|2h\|all` | Select a time window |
| `:chart window SECONDS` | Select a custom time window |
| `:chart zoom FACTOR` | Select a zoom factor from one to 1024 |
| `:chart pan FRACTION` | Select a position from zero to one |
| `:chart undo`, `:chart reset` | Undo or reset rectangular zoom for the inspector's current metric |
| `:chartzoom undo`, `:chartzoom reset` | Undo or reset rectangular zoom for the last selected or zoomed metric graph |
| `:metric-live TOKEN on\|off\|toggle` | Control Live for a currently visible running metric; TOKEN is its session control token |
| `:metric-window TOKEN SECONDS` | Set that metric's display window from 1 to 30 seconds |
| `:metric-window TOKEN focus` | Enter its window-slider keyboard controls |
| `:metric-window TOKEN reset` | Restore that metric's 30-second window |
| `:metric-sampling TOKEN N` | Select polling position 1 to 100 for that exact running metric; endpoints request five seconds and 500 milliseconds |
| `:metric-sampling TOKEN focus` | Enter its sampling-slider keyboard controls |
| `:metric-sampling TOKEN reset` | Restore that metric's five-second polling request |
| `:series-scroll up\|down\|page-up\|page-down\|home\|end` | Scroll the compact native Analytics Job Series metric document |
| `:chart axis auto` | Fit the axis to available measurements |
| `:chart axis fixed LOW HIGH` | Apply fixed numeric bounds |
| `:chart axis log [POSITIVE_LOW POSITIVE_HIGH]` | Apply a logarithmic scale |
| `:chart range FIRST LAST` | Select visible samples by number; indices start at one |
| `:chart range clear` | Clear the selected sample interval |
| `:chart events` | Open the observed-event list |
| `:chart events on\|off` | Show or hide event markers |
| `:chart event NUMBER` | Open the numbered event's citation or exact job |
| `:timeline [events]` | Open the observed-event timeline |
| `:timeline open EVENT_NUMBER` | Open the exact job or cited log for the numbered timeline event; numbers start at one |
| `:timeline seek EVENT_NUMBER` | Seek a numbered event in a replay when supported |
| `:chart shared on\|off` | Change the scale lock for comparisons |
| `:metricdisplay METRIC label TEXT` | Set a display label |
| `:metricdisplay METRIC unit DECLARED_UNIT` | Set a declared display unit |
| `:metricdisplay METRIC precision INTEGER` | Set decimal precision between zero and twelve |
| `:metricdisplay METRIC reset` | Remove display overrides |

In the chart inspector, arrows and Home/End move between samples.
Use `+` and `-` to change zoom.
Use `[` and `]` to pan.
Use Tab or Shift-Tab to change metrics.
Use `t` to cycle time presets.
Use `a` for an automatic axis.
Use `g` for a logarithmic axis.
Use `r` to set the start and end of a sample interval.
Use `e` to open observed events.
Use `s` to change shared-scale mode.
Click a visible time-preset or axis control for the corresponding chart operation.
Move the pointer inside a metric plot for its thin crosshair in the active theme's accent color.
Unicode selectors use two horizontal and four vertical Braille positions per cell.
The crosshair reaches the latest reported cell immediately; its Braille dot phase animates for at most 24 ms.
Mouse coordinates remain whole terminal cells.
Original curve and annotation glyphs keep their styles where the selector intersects them.
ASCII and reader modes use static dots and `+` intersections.
Set `animations` to `false` to disable easing while keeping the Unicode selector.
Press, drag, and release inside the same plot to select a time interval and fit its visible curve to both axes.
The interval must span at least two columns.
Hold Shift before pressing to retain an explicit two-axis rectangle instead; this also requires at least one row.
Selected time intervals show relative offsets in adaptive `s`, `ms`, or `us` units, with a start timestamp and span note.
Press `u` or `0` while pointing at that graph to undo or reset rectangular zoom.
Right-click inside the plot to restore its full view. This also turns off that metric's Live window.
The reset applies only to that graph's exact metric and source.
An active drag has a three-cell margin beyond the axis labels and tick row.
The margin is clipped to the actual visible pane. The initial press must be inside the plot.
Movement and release inside that margin use the nearest plot edge.
The painted horizontal and vertical axes stay fixed while new samples arrive during the drag.
Sampling continues. Automatic sample updates do not cancel the gesture.
Esc, movement beyond the margin, or changed job/layout cancels the preview. A later release cannot commit it.
Menus, dialogs, and startup previews prevent capture of hidden graphs.

In Diff, `s` changes the comparison scale lock.
In the event picker, Enter opens the selected citation or job.
Right-click outside an analysis plot clears its sample or event selection and keeps the dialog open.
Use arrows or click a row to select again before opening an event or starting a sample interval.
Click an event's visible text or source row to open that exact painted citation.
Chart Events and Timeline links also support F8 navigation and Enter activation.
Display labels and precision do not change recorded values.
A running metric can show **Live off/ON** and two adjacent sliders.
Compact rows use empty/filled Live symbols, or `o`/`+` in ASCII mode.
The first slider controls the display window. Drag left for 30 seconds or right for one second.
The second slider controls polling. Drag left for five seconds or right for 500 milliseconds.
Both scales use logarithmic steps. Native polling requests do not have a flat region caused by a lower-limit clamp.
**Set** shows that slider's requested interval; **Poll** shows the shared effective interval.
Compact rows show the effective interval before the polling track and `S` plus the request after it.
Labels use `s`, `ms`, or `µs`; ASCII mode uses `us`.
The request updates even when a faster global or companion-metric request keeps the effective interval unchanged.
Numeric polling commands use positions one to 100; they do not multiply the polling frequency.
Remote file reads retain a 1.5-second minimum, which can limit their faster positions.
Right-click the first slider to restore 30 seconds. Right-click the second to restore its five-second request.
Each reset preserves the other control, Live state, job selection, and graph zoom.
While the slider has focus, use Left/Right, Page Up/Page Down, Home, and End.
Home and End select that slider's minimum and maximum endpoint positions.
Enter or Esc leaves slider focus. Esc during a drag restores that slider's previous value.
Live displays `[display time - duration, display time]`.
Running timestamped graphs use an adaptive display delay in both Live and the ordinary retained view.
The visible edge follows acquired samples; a status label reports the actual display lag or a wait for data.
Changing the window alone does not change source polling.
See [Buffered running graphs](guides/charts.md#read-buffered-running-graphs) for delay, startup, and outage behavior.
Line-plot title labels `fit` and `range` identify the optional data-dependent display layers.
The local polynomial fit is a visual model; the faint range band shows observed low and high values.
Sample inspection still returns the original measurements.
See [Trend and range layers](guides/charts.md#read-the-trend-and-range-layers) for eligibility and limits.
The sampling slider requests more frequent source reads, even while Live is off.
The effective interval below the graph accounts for source limits and the fastest global or per-metric request.
Shared probes use the fastest requested rate: CPU and memory share a resource probe, and GPU curves share a GPU probe.
The requests do not multiply. Another job keeps its global cadence and its own sampling request.
Job-produced files retain their own writing cadence.
A valid rectangular zoom turns Live off. A cancelled rectangle resumes it.
Completed jobs have no active Live or sampling controls. Metric preferences reset when Tower restarts.

See [Charts](guides/charts.md) for coverage, axis restrictions, and declared units.

## Array task pages

| Control | Function |
| --- | --- |
| Click `▾` / ASCII `v` on an array cohort | Select that cohort and open its task page |
| Click `▸` / ASCII `>` on an open cohort | Close its task page and retain the summary |
| Enter in full Research → Arrays | Toggle the selected cohort's task page |
| Page Up / Page Down in full Research → Arrays | Move through an open task page in groups of 24 |
| `:array open\|close ARRAYID [CLUSTER]` | Apply an explicit action to an exact published cohort; specify the cluster when the ID is ambiguous |

Jobs and History Details use the cohort arrows and their normal pane scrolling.
Array task pages retain separate state from shared job-group folds.
Opening tasks does not prepare a retry or submit a job.
See [Array operations](RESEARCH.md#array-operations) for summary coverage and reviewed retries.

## Artifact inspection

| Command | Function |
| --- | --- |
| `:outputs` | Browse declared artifacts for the selected project run |
| `:artifact` | Open the selected artifact |
| `:artifact open RELATIVE_PATH` | Expand or open the exact visible declared output in the artifact browser |
| `:artifact next\|prev` | Load the next or previous artifact page |
| `:artifact refresh` | Reload the preview |
| `:artifact columns NAME_OR_NUMBER...` | Select CSV columns by name or number; indices start at one |
| `:artifact column NAME_OR_NUMBER` | Focus one column in an open CSV preview |
| `:artifact sort COLUMN asc\|desc\|off` | Change the global CSV record order before paging |
| `:artifact json /POINTER` | Inspect a JSON Pointer |
| `:artifact text` | Open raw text pages from the first page |
| `:artifact structured` | Restore declared-format pages from the first page |

Use `[` and `]` for background page loading.
Use arrows and page keys to scroll the loaded content.
In CSV, Left/Right focuses a column.
Click a column button to focus that column.
Enter cycles ascending, descending, and off.
Use `c` to change the focused column's visibility.
In JSON, Enter or Space expands the selected node.
Click a run row to bind its exact attempt.
Click a declared output row to expand its directory or open its bounded preview.
See [Artifacts](guides/artifacts.md) for size limits, source identity, and declared path rules.

## Command families

### Research and cluster operations

The [generated operation reference](reference/operations.md) lists all current
forms, fields, defaults and choices. The earlier twenty-six research operations
retain their [feature guides](guides/operations.md#all-twenty-six-operation-keys).
Open **File → Research and cluster operations**, or use a related Sources,
Analytics, or Research button. Embedded Research views expose the same controls.
Use `:workbench run|investigate|compare|results|connect` to select a task, or
`:workbench all` to open the complete catalog.

Pin frequently used tools with **Pin tool [p]** or `p`. Open **Pinned** or
`:workbench pinned` to see these tools. A star marks a pinned tool in the catalog.
Pins survive restart when preference storage is enabled. They store tool names
only. Opening a pinned tool uses the current job and project, with a fresh form.
Search also works within Pinned. While you edit a field or search text, `p`
inserts text. Pin changes are disabled during inspection and action review.

| Control | Function |
| --- | --- |
| `:ops`, `:ops catalog` | Open the complete catalog |
| **Pinned**, `:workbench pinned` | Show saved tool shortcuts |
| `p`, **Pin tool / Unpin tool** | Toggle the selected catalog tool or open form/report tool |
| `:ops pin FEATURE`, `:ops unpin FEATURE` | Add / remove one tool shortcut without changing its inputs |
| `:ops FEATURE`, `:ops open FEATURE` | Open a feature's editable form |
| `:ops run FEATURE field=value` | Inspect explicit inputs; quote values that contain spaces |
| Click or Enter on a field | Edit text or cycle fixed choices |
| Enter / Esc while editing | Accept / discard the field edit |
| Up / Down, page keys, Home / End | Move through catalog entries, fields, or report lines |
| Tab / Shift-Tab | Switch content and button focus |
| Left / Right | Move action-button focus |
| **Inspect / prepare**, `:ops inspect` | Collect evidence or prepare a reviewable plan |
| **Review action**, `:ops review` | Display the exact current action plan |
| **Confirm apply**, `:ops apply` | Apply only the displayed, current, unused review |
| **Edit inputs**, `:ops form` | Return to the input form |
| `r`, **Refresh** | Inspect the current inputs again |
| `v`, then movement; Shift-click | Select report or review lines |
| `y`, **Copy report**, `:ops copy` | Copy selected lines, or the complete structured report when no selection exists |
| Right-click | Clear selected report lines |
| Wheel / scrollbar | Scroll the operation pane |
| **Cancel inspection**, `:ops cancel` | Cancel a read-only inspection at a bounded checkpoint |
| Esc / `q`, **Close**, `:ops close` | Return to the prior view; a running action still has its result collected |

A review expires after five minutes and is invalid after a scope, attempt, or
source change. Inspection never confirms a change. The interface consumes the
plan before it starts Apply. An unknown submission result needs reconciliation,
not a repeated click. Read the [operation review rules](guides/operations.md#review-background-work-and-recovery-rules).

### Phase one inspection controls

These controls are available in release 4.14. Inspectors do not submit jobs.
See the [phase one guide](guides/phase-one.md) for source scope and limits.

| Control | Function |
| --- | --- |
| **Metric sampling** on Sources, Analytics, or Research Experiment | Inspect read cadence and upstream collection evidence |
| `:telemetry [JOBID]` | Inspect the selected or specified job; show cluster capabilities when no job is selected |
| `:telemetry refresh` | Refresh the report and bypass the configuration cache |
| `:telemetry copy`, `:telemetry close` | Copy or close the inspector report |
| **GPU source**, or `:gpuprovider` | Open the provider choices; arrows choose, Enter applies, Esc returns |
| `:gpuprovider auto\|nvidia\|amd\|intel` | Set the live GPU adapter; GPU sampling must also be enabled |
| **Shell checks** in Research Submit or the resource submission form | Check the prepared local script; `h` is the form shortcut |
| `:shellcheck [SCRIPT]` | Check an explicit local script, or the prepared plan's script when omitted |
| `:shellcheck refresh` | Rerun the previous check |
| **Load input map**, or `:arraymap [PATH]` | Load an explicit array manifest; omitted path opens a prompt |
| **Browse inputs**, or `:arraymap inspect` | Inspect the attached scientific mapping |
| `:arraymap search TEXT` | Search attached scientific entries |
| **Reload map**, or `:arraymap reload` | Accept a reviewed current source revision |
| **Detach map**, or `:arraymap clear` | Remove the session attachment |

Research controls also apply to their inline Jobs and History Details views.
The toolbar's Help menu exposes sampling capabilities and GPU provider
selection. View also exposes GPU provider selection. Edit exposes shell checks
and array mapping. Use Esc to leave an inspection. Shell checks reject remote
and replay sessions, even when a path is explicit.

### Command index

These commands are available through the palette.
The same command families are available through `tower run` when they do not require an interactive overlay.
Quoted arguments preserve paths that contain spaces.
Job-changing commands open a review; scripted changes require `--yes`.

| Family | Commands | Reference |
| --- | --- | --- |
| Help and command discovery | `menu`, `about`, `help`, `commands`, `quit` | [Live workbench](guides/live-workbench.md#use-the-terminal-toolbar) |
| Main navigation | `tab`, `view`, `workspace`, `workspaces`, `back`, `forward`, `jump`, `location` | [Navigation](guides/navigation.md) |
| Settings and field inspection | `settings`, `keybindings`, `explain`, `peek` | [Navigation](guides/navigation.md) |
| Panel layout | `density`, `focus`, `maximize`, `layout`, `panel-scroll` | [Workbench](WORKBENCH.md#shape-your-workspace) |
| Table inspection | `sort`, `sortby`, `sorteditor`, `headers`, `columns`, `facet`, `filters`, `where`, `savedview`, `viewpicker`, `jobgroups`, `jobgroup`, `grouping`, `queue-drag`, `filter`, `days`, `historyrange`, `recents`, `marked`, `freeze`, `jobactions`, `node`, `drill` | [Tables](guides/tables.md) |
| Job selection and annotations | `mark`, `unmark`, `pin`, `tag`, `untag`, `note`, `compare` | [Reference](reference.md#keys-remappable-in-the-config) |
| Job changes | `cancel`, `hold`, `release`, `requeue`, `rerun`, `top`, `resubmit`, `chain` | [Reference](reference.md#keys-remappable-in-the-config), [retained rerun](guides/operations-cluster.md#rerun-one-cancelled-job) |
| Selected job | `jobpanel`, `inspect`, `log`, `investigate`, `advise` | [Live workbench](guides/live-workbench.md#inspect-a-job-inside-jobs) |
| Log files and presentation | `find`, `wrap`, `bookmark`, `logview`, `logpan`, `loggroup`, `logpreview`, `logalign`, `logdiff`, `logjson`, `logfold`, `logunread` | [Log display](guides/log-view.md) |
| Complete-file log inspection | `logolder`, `logsearch`, `logsearchmode`, `logresults`, `loggoto`, `logmark`, `logmarks` | [Log search](guides/log-search.md) |
| Sampling and profiles | `rate`, `refresh`, `source`, `gpu`, `bell`, `profile`, `theme` | [Live workbench](guides/live-workbench.md#set-the-update-rate) |
| Sampling evidence and GPU provider | `telemetry`, `gpuprovider` | [Phase one](guides/phase-one.md) |
| Pointer navigation and motion | `focusbuttons`, `smoothscroll`, `startup` | [Mouse and button navigation](guides/pointer-navigation.md) |
| Activity and output | `activity`, `notifications`, `task`, `export`, `exports`, `copy` | [Operations](guides/operations.md) |
| Completion and alert delivery | `inbox`, `alerts` | [Operations](guides/operations.md) |
| Terminal diagnostics | `terminaldoctor`, `terminaltest` | [Operations](guides/operations.md) |
| Project and run selection | `project`, `runs`, `run`, `outputs`, `artifact` | [Project standard](PROJECT_STANDARD.md) |
| Application measurements | `metrics`, `metric`, `metricdisplay`, `metric-live`, `metric-window`, `metric-sampling`, `dashboard`, `chart`, `timeline`, `diff` | [Charts](guides/charts.md) |
| Output contracts | `artifacts`, `validate` | [Research](RESEARCH.md) |
| Provenance | `passport` | [Research](RESEARCH.md) |
| Submission preparation | `prepare`, `preflight`, `submit`, `array` | [Research](RESEARCH.md) |
| Shell checks and scientific array mapping | `shellcheck`, `arraymap` | [Phase one](guides/phase-one.md) |
| Research and cluster operation forms | `ops` | [Operation catalog and controls](guides/operations.md) |
| Resource and queue evidence | `predict`, `forecast`, `blockers`, `tradeoffs`, `choose` | [Planning](WAVE_TWO.md) |
| Experiment and workflow execution | `scaling`, `workflow`, `orchestrate`, `execution` | [Planning](WAVE_TWO.md) |
| Recorded sessions | `replay` | [Reference](reference.md#recording-and-replay) |
| Snapshot expressions | `eval` | [Reference](reference.md#scripted-mode-and-expressions) |

Plugins can register additional commands.
Use `:commands` to inspect the commands loaded in your session.
The [plugin reference](reference.md#plugins) defines the extension interface.

## Command-line options

Run `tower --help` for the parser's complete current syntax.

| Option | Purpose |
| --- | --- |
| `-h`, `--help` | Print command-line help |
| `--version` | Print the application version |
| `--config FILE` | Select TOML or JSON configuration |
| `--write-config` | Create commented defaults without replacing an existing file |
| `--doctor` | Inspect demo, local, or remote prerequisites |
| `--gpu-check [JOBID]` | Inspect GPU detection; omit JOBID to check current individual jobs |
| `--gpu-check-output DIRECTORY` | Save a GPU check to a new private directory |
| `--ui-trace FILE` | Record bounded interactive UI timings and page transitions; save to a new private JSON file on exit |
| `--profile NAME` | Apply a configured cluster profile |
| `--host HOST` | Run scheduler commands and log reads through SSH |
| `--ssh-user USER` | Select the remote SSH login |
| `--user USER` | Select the scheduler user |
| `--account ACCOUNT` | Select the account used for aggregate views |
| `--interval SECONDS` | Select the nearest polling position within five seconds to 500 milliseconds; `--rate` takes precedence |
| `--rate N` | Select startup polling position 1 to 50; override the saved preference |
| `--days DAYS` | Set the accounting lookback window |
| `--no-gpu` | Disable live GPU sampling |
| `--bell` | Ring on observed job starts |
| `--ascii` | Select portable character graphics |
| `--unicode` | Select Unicode block graphics and symbols when encoding permits them |
| `--no-color` | Disable color |
| `--no-plugins` | Disable plugin loading |
| `--once` | Print one frame and exit |
| `--tab IDENTIFIER` | Select the initial page or export table |
| `--research-view NAME` | Select the initial Research workspace |
| `--metrics-file FILE` | Attach an application JSONL measurement stream |
| `--contract FILE` | Attach an output contract |
| `--workdir DIR` | Set the root for attached project files |
| `--passport FILE` | Attach an immutable run passport |
| `--planning-file FILE` | Attach planning observations or a recipe |
| `--json` | Print the complete snapshot as JSON |
| `--report [FILE]` | Write a complete ASCII report; omitted FILE means standard output |
| `--csv` | Print the current export table as CSV |
| `--watch` | Display an updating ANSI screen |
| `--fake` | Use simulated scheduler data |
| `--record FILE` | Record scheduler commands and responses |
| `--replay FILE` | Use recorded scheduler data |
| `--speed FACTOR` | Set replay speed |
| `--paused` | Start replay paused |
| `--run COMMAND` | Execute one palette command and exit |
| `--yes` | Confirm a scripted job-changing operation |
| `--eval EXPRESSION` | Print a snapshot expression result |
| `--wait-for EXPRESSION` | Wait until a snapshot condition is true |
| `--timeout SECONDS` | Limit a wait; zero means no timeout |
| `--poll SECONDS` | Set the interval for condition polling |
| `--alert EXPRESSION` | Add a session alert; this option can be repeated |
| `--no-state` | Disable normal UI state reads and writes |
| `--width COLUMNS` | Set report or one-frame output width |

Use `tower run COMMAND ARGUMENTS` as an alternative to `--run`.
Command-specific flags are passed to that palette command.
See the [scripted-mode reference](reference.md#scripted-mode-and-expressions) for exit codes.

## Configuration and environment variables

Use `--config`, `TOWER_CONFIG`, or the default file under `~/.config/tower/`.
Use `NO_COLOR=1` to disable color.
Use `--account ACCOUNT` for a single launch override.

| Variable | Managed Bash alias behavior |
| --- | --- |
| `SLURM_TOWER_PROFILE` | A nonempty value overrides the installed profile |
| `SLURM_TOWER_ACCOUNT` | Sets an account for any profile; an empty value suppresses legacy account injection |
| `CARC_ACCOUNT` | Provides a fallback only for the CARC profile when the generic account variable is unset |
| `TOWER_CONFIG` | Selects a custom configuration instead of the supplied example |
| `SLURM_TOWER_ROOT` | Selects the checkout used by the managed launcher |

The desktop alias ignores `CARC_ACCOUNT`.
See [Desktop account overrides](DESKTOP.md#account-and-profile-overrides) for examples.

Editor destination variables are separate from the managed alias settings:

| Variable | Editor delivery behavior |
| --- | --- |
| `NVIM` | Preferred absolute Unix socket for an already-running local Neovim owned by the current user |
| `NVIM_LISTEN_ADDRESS` | Alternative owned local Neovim Unix socket |
| `TOWER_VIM_SERVER` | Exact reported Vim server name; required to choose among multiple servers |

See [Pane navigation](guides/pane-navigation.md#send-text-to-vim-or-neovim) for discovery, timeouts, and clipboard fallback.

The normal persistent state base is `~/.local/state/tower/`.
The desktop profile uses `state_namespace = "desktop"` to separate connection state.
See [Desktop state](DESKTOP.md#keep-desktop-state-separate) for the default directory layout.
The [configuration reference](reference.md#configuration) defines profiles, intervals, timeouts, thresholds, alerts, clipboard options, and plugins.
The [documentation style](DOCUMENTATION_STYLE.md) defines the terminology used in these guides.
