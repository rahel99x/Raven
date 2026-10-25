# Navigation, mouse menus, and themes

Raven uses the same workspace navigation in Light and Heavy. The left sidebar
lists every main pane. Click a pane to open it; click its arrow to reveal its
views. Research, Analytics, Nodes, and Traces provide direct view choices.
Navigation changes the view without changing which account pays for a job.

## Favorites

Right-click a pane or view and choose **Add to Favorites**. Favorites appear
above the complete workspace list; adding one never removes its original entry.
Right-click a favorite to remove it.

The **Own / A–Z** button switches between your custom sequence and alphabetical
order. Alphabetical display preserves the custom sequence. Drag a favorite onto
another favorite to move it. Dropping outside the favorites cancels the move.
Dragging while alphabetically sorted switches to the resulting custom order.
Raven saves completed changes through its existing preference writer.

Scroll the sidebar independently with the mouse wheel or its scroll arrows.
The rail narrows on small terminals; full destination labels remain available
through keyboard focus. Plain, unbounded text output retains its horizontal
navigation because it has no interactive sidebar.

## Keyboard and mouse access

Press **F8** to navigate visible controls with arrows or Tab and activate one
with Enter. Enter `:sidebar focus` to navigate the sidebar directly. Up/Down
select an entry; Right/Left expand or collapse its children; Enter opens it.
Use `f` for the selected entry's Favorites menu, `o` to change favorite order,
and Esc to return to the document. Menu text explains additional local actions.

Press **F10** for the menu bar. Menus and confirmation screens accept mouse
clicks as well as keyboard input. Editable values still accept typed text;
their visible keep/cancel actions avoid requiring an editing shortcut.

The legacy Details dialog exposes **Close**, **Up**, and **Down** in the primary
toolbar position. The footer filter editor exposes **Apply** and **Clear**;
Clear uses the existing Esc behavior and removes the current filter. Closing
either view preserves the selected job and leaves scientific jobs running.

Right-click a job to open actions for that exact job. Right-click a marked job
to act on the marked selection in that list. **Hold**, **Release**, and
**Cancel jobs** appear when applicable, alongside grouping actions. Completed
or otherwise ineligible jobs are identified before any action. Every scheduler
change passes through the existing confirmation review and identity checks.
**Close menu** dismisses the menu without changing jobs.

## Themes

Open **Themes** in the top menu bar. A selection applies immediately and is
saved for later launches. **Gruvbox** is the default.

| Theme | Appearance |
| --- | --- |
| Gruvbox | Warm charcoal with muted green, amber, and rose accents |
| Modnokai | Olive charcoal with bright, distinct accents |
| Darcula | Neutral charcoal with blue and violet accents |
| Catppuccin Mocha | Soft dark surfaces with pastel accents |
| Nord | Cool slate with restrained blue and mint accents |
| Tokyo Night | Deep indigo with blue, green, and violet accents |
| Rosé Pine Dawn | Warm light surfaces with dark rose and blue ink |
| System | Your terminal's default text, background, and ANSI palette |

System inherits the terminal used to run Raven, including an SSH terminal. It
does not inspect a remote desktop, query terminal colors, or guess an operating
system palette. Direct-color curses terminals that cannot expose native palette
slots retain default foreground/background with emphasis and reverse selection.
Explicit colors supplied by embedded applications remain their own colors.

Older `default`/`dark`, `light`, and `terminal` selections migrate to Gruvbox,
Rosé Pine Dawn, and System respectively. Existing `monokai` and `gruvbox` aliases
remain accepted. Monochrome, reader, high-contrast, and color-vision modes remain
available through their existing commands for compatibility and accessibility;
they are excluded from the curated palette list. `--no-color` and `NO_COLOR`
continue to take precedence over a selected theme.

For installation and existing configurations, see [Updates](../INSTALLATION.md).
For pane resizing and job selection, see
[Adjustable workspaces](adaptive-workspaces.md).
