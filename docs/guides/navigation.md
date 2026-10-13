# Navigation and terminal controls

[README](../../README.md) · [Controls](../CONTROLS.md) · [Pane navigation](pane-navigation.md)

Find cached data, restore inspection locations, and configure Raven's terminal preferences.
Unicode and ASCII modes use the same commands and key bindings.
Open the command palette with `:`; type the command text without the leading colon.

<a id="feature-29"></a>

## Find a destination

1. Press `Ctrl-G`, or enter `:jump`.
2. Enter part of a name, job ID, path, or destination type.
3. Use Up and Down to select a result.
4. Press Enter to open the result.
5. Press Esc to close the search.

The search contains these cached records:

| Result | Action |
| --- | --- |
| Job | Open the inspector for the exact job ID. |
| Project run | Select the registered run. |
| Log file | Open the declared source path and its job identity. |
| Saved table view | Apply the saved view. |
| Research workspace | Open the workspace. |
| Command | Put the command in the palette for editing. |
| Saved location | Restore the complete destination. |

Use `:jump 123_2` to start with a query.
Search does not request scheduler data or read directories.
The index contains at most 4,096 entries.
It refreshes at most once each second during search.
The result list contains at most 512 matches.

<a id="feature-30"></a>

## Use Back and Forward

Enter `:back`, or press `Ctrl-B` or Alt-Left, to restore the previous location.
Enter `:forward`, or press Alt-Right, to return to the location you left with Back.

The restored location includes the selected identity, filters, column sorts, and scroll position.
It also includes the selected log source and Research workspace.
Chart sample positions and event pickers return to their saved positions.

A new destination clears the Forward stack.
Raven retains at most 32 locations in each session stack.
Back and Forward do not repeat scheduler actions.
These stacks remain local to the current session.

<a id="feature-31"></a>

## Save a complete location

1. Open the required page or log source.
2. Set the required filters and column sorts.
3. Move to the required reading position.
4. Enter `:location save NAME`.

Use quotes for a name with spaces:

```text
:location save "worker failure"
:location open "worker failure"
:location delete "worker failure"
```

Enter `:location` or `:location list` to open the location picker.
Select a location with Up and Down.
Press Enter to open it.

Saved locations survive a restart when state persistence is enabled.
Each location retains page context, exact source identity, column layout, filters, and reading position.
Raven stores at most 50 named locations.
A location can open only in the cluster profile that saved it.
Log locations must also match the original source connection.
Raven revalidates an opened log buffer through its normal source controls.
Opaque comparison results and file buffers are not stored in the location file.
Clearing a reopened project run restores the original manual metric, contract, passport, and log-index paths.
Loaded passport objects remain local to the session.

<a id="feature-32"></a>

## Change settings with a preview

1. Enter `:settings`.
2. Use Up and Down to select a setting.
3. Use Left and Right to change its value.
4. Use Space to change a Boolean setting.
5. Press Enter to apply and save the settings.

Press Esc to cancel the preview and restore the previous settings.
Press `D` to preview the default settings.
Use `:settings reset` to open a preview of the defaults.
Press Enter to accept them.

The editor provides these settings:

| Setting | Effect |
| --- | --- |
| Theme | Change colors and contrast. The reader theme uses ASCII and static feedback. |
| Density | Select comfortable, compact, or focused panel spacing. |
| Terminal colors | Enable or disable color. |
| Interface animations | Enable or disable completion motion and graph-selector easing. |
| Terminal bell | Enable or disable the configured start bell. |
| Mouse input | Enable or disable clicks and wheel input. |
| GPU sampling | Enable or disable the existing GPU sampler. |
| Terminal clipboard | Enable or disable OSC 52 clipboard output. |
| Local clipboard tools | Enable or disable installed clipboard utilities. |
| Polling interval | Change the queue slider position using the active mode and site-policy range. |
| Other source intervals | Change the configured base for sources such as accounting, nodes, and forecast probes. |

The **Polling interval** row selects the nearest position on the top-right
control's logarithmic scale. For managed `raven`, `ravenl`, and `ravenh`, it changes
queue cadence while the other readers keep their own settings. The endpoints
follow the active queue base and mandatory site limits. Native compatibility
sessions retain the existing global five-second to 500-millisecond request,
with slower configured bases scaled by at most ten.

Use **Setup → Heavy telemetry update settings** to select Default, Medium, High,
Ultra, or Custom, and its gear button to edit individual reader intervals and
budgets. Heavy changes apply live; **Save settings** persists them. See
[telemetry updates](telemetry-updates.md) for values, mode transitions, and limits.
Separate live, GPU, and trace interval rows are not shown in the general editor.
Other source rows range from 1 to 3,600 seconds; Left decreases by 20 percent
and Right increases by 25 percent. Managed readers follow their collection
configuration and selected telemetry preset.

The general settings editor previews changes in the active sampler.
Cancel restores its previous values and exact queue-slider position.
Unaccepted previews are not saved; Enter accepts the draft and saves the polling
preference. GPU sampling remains subject to managed collection policy and does
not enable automatic allocation probes.
Explicit launch options and environment overrides retain priority over saved settings.
A locked row shows `[launch option]`.
Restart Raven with different launch options to change that value.
Other settings can still be previewed and applied.
Later theme, density, GPU, or bell commands become the current saved preferences.

<a id="feature-33"></a>

## Edit and test keybindings

1. Enter `:keybindings`.
2. Select an action with Up and Down.
3. Press `e` or Enter to edit its keys.
4. Enter up to four key names, separated by spaces.
5. Press Enter to retain the edit in the draft.
6. Press `a` to apply and save the complete draft.

Use names such as `j`, `up`, `pgdn`, `ctrl-q`, or `alt-f`.
Named keys include arrows, Home, End, Page Up, Page Down, Enter, Esc, Tab, Shift-Tab (`btab`), Space, Backspace, and Delete.
Function keys use `f1` through `f24`.
Ctrl letter names use lowercase letters except `h`, `i`, `j`, and `m`.
Those four combinations arrive as Backspace, Tab, or Enter.
Alt letter names use lowercase letters.
Ctrl and Alt also support arrows, Home, End, Page Up, Page Down, Delete, and Backspace through their terminal protocols.
An empty list disables the selected action.
Raven rejects duplicate assignments and reserved navigation keys.
Reserved workspace controls include Ctrl-W, F6, and `z`.
Resolve each conflict before you apply the draft.

Press `t`, then press a key, to test its action without executing it.
Press `D` to restore the default bindings in the draft.
Use `:keybindings reset` to open the default draft.
Press Esc to discard draft changes and close the editor.
During a key edit, Esc cancels that edit first.
Press Esc again to close the editor.

The bindings apply where a page uses that action.
Workspace controls and overlays retain their contextual editing, scrolling, and review controls.
For example, `Ctrl-A` opens Activity on a main page and moves to the start in the command palette.

<a id="feature-34"></a>

## Edit a command

Press `:` to open the command palette.
Type a command and use these editing controls:

| Control | Action |
| --- | --- |
| Left / Right | Move one character. |
| Home / End or Ctrl-A / Ctrl-E | Move to the start or end. |
| Ctrl-Left / Ctrl-Right or Alt-B / Alt-F | Move one word. |
| Backspace / Delete | Delete the preceding or current character. |
| Ctrl-W, Ctrl-Backspace, or Alt-Backspace | Delete the preceding word. |
| Alt-D | Delete the next word. |
| Ctrl-U / Ctrl-K | Delete to the start or end. |
| Alt-U / Alt-R or Ctrl-Z / Ctrl-Y | Undo or redo an edit. |
| Up / Down | Select a completion. |
| Tab | Insert the selected completion. |
| PgUp / PgDn | Recall an older or newer command. |
| Enter | Run the command. |
| Esc | Cancel editing and return to the originating page or overlay. |

Raven retains at most 100 undo entries for the current edit session.
Quote file arguments that contain spaces.
Tab completion supplies quotes when required.
Use Alt controls if the terminal intercepts a Ctrl combination.

<a id="feature-35"></a>

## Paste a command safely

Use a terminal that supports bracketed paste.
Paste the command into Raven.
Raven opens the palette or inserts the text at its editing cursor.
It does not execute the paste.

Check the text before you press Enter.
Pasted Unicode and quoted paths retain their characters.
Pasted line breaks appear as visible markers in the palette.
Enter submits one command with whitespace-separated arguments.
It does not execute a batch of separate lines.

A paste is one undo operation.
The input limit is 4,096 characters.
Raven reports a truncated paste.
It rejects terminal control characters in pasted command text.
Close a review dialog before you paste a command.

<a id="feature-36"></a>

## Read command assistance

Read the status line below the command completions.
It shows examples, invalid choices, and missing or incomplete command text.
It also reports an unmatched quote and job IDs absent from the cached inventory.

An unmatched quote keeps the command open for editing.
An unknown cached job can still be queried through the command's normal scheduler path.
Scheduler changes retain their existing review dialogs.
Command assistance performs no scheduler requests.

<a id="feature-37"></a>

## Explain a field

Enter `:explain COLUMN` to explain a column on the current table.
Enter `:explain TABLE COLUMN` to specify its table.
If header focus is active, `:explain` uses the focused column.
Otherwise, it uses the name column.
On an active chart, `:explain` uses the chart's metric.
Enter `:explain metric NAME` to explain a published metric explicitly.

Examples:

```text
:explain jobs "CPU%"
:explain jobs eff
:explain history ce
:explain nodes loadpct
:explain sources latency
:explain metric loss
```

The explanation shows the field's meaning, units, formula, and source.
It also shows the configured sample interval and the last successful source sample when available.
Missing-data text explains when a value is unknown.
Use Up, Down, Page Up, and Page Down to read the explanation.
Press Esc to return.
An application metric explanation shows its declared unit, job scope, source, available sample interval, and missing-value count.
Raven does not infer an application formula or unit.

<a id="feature-38"></a>

## Inspect and copy a complete value

1. Select the required job, node, partition, or source.
2. Enter `:peek COLUMN` or `:peek TABLE COLUMN`.
3. Scroll the complete value with Up, Down, Page Up, and Page Down.
4. Press `y` to copy its underlying value.
5. Press Esc to return.

Use `:peek name` for a complete job name.
Use `:peek where` for the complete allocated node list.
Use `:peek sources error` for a complete source diagnostic.
If header focus is active, `:peek` uses the focused column.
On an active chart, `:peek` copies the selected sample's original numeric value.
Use `:peek metric NAME` to inspect an explicitly named metric.
Outside its active chart, this command uses the latest available sample.

Raven reads the value from the selected record's exact identity.
The copied value does not contain table clipping marks.
The open preview keeps its source identity if incoming data changes the selected row.
The display limit is 65,536 characters.
Copy uses the complete value held by the preview.
An unknown field displays an explicit missing-value message.

## Integration methods

The workbench uses these public module entry points:

| Method | Purpose |
| --- | --- |
| `navigation_tools.initialize(app)` | Create bounded session state. |
| `navigation_tools.restore(app, data)` | Validate saved locations and preferences. |
| `navigation_tools.save(app)` | Return portable state for Raven persistence. |
| `navigation_tools.run_command(app, args)` | Dispatch navigation and preference commands. |
| `navigation_tools.handle_key(app, key)` | Handle keyboard input for its overlays. |
| `navigation_tools.handle_mouse(app, y, x, button, shift)` | Select visible overlay rows. |
| `navigation_tools.overlay(views, snap, app, width, height)` | Render a clipped terminal overlay. |
| `navigation_tools.jump_matches(app)` | Search the bounded cached index. |
| `navigation_tools.raw_field(app, table, key)` | Read a field by selected source identity. |
| `navigation_ui.record(app, target_tab, force=False)` | Capture a session location before navigation. |
| `navigation_ui.back(app)` / `forward(app)` | Restore a session location. |
| `command_ui.open_palette(app, text, origin)` | Open editing and retain its originating mode. |
| `command_ui.paste(app, text)` | Insert a paste without command execution. |
| `command_ui.validation(app, text)` | Return local command assistance. |

These methods use the existing Raven controller and event loop.
They add no runtime dependencies.
