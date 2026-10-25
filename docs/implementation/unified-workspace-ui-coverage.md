# Unified workspace UI: implementation and acceptance

This revision builds on `91c6598`, which contains the pending trace-discovery
fixes. It changes the shared dashboard used by `raven`, `ravenl`, and `ravenh`;
it does not introduce another scheduler backend or application event loop.

## Coverage ledger

| Requirement | Implementation and acceptance evidence |
| --- | --- |
| Persistent left navigation | `sidebar.py` projects every registered main pane. `Views` reserves one content origin; body hits, charts, scrollbars, terminal bounds, alerts, and history docks use global coordinates. `test_sidebar_layout_integration.py` covers narrow through wide terminals and stream output. |
| Direct subview selection | Shared `ui/destinations.py` supplies pane and Research, Analytics, Nodes, and Trace Live/Fit identities to both sidebar and existing Favorites commands. `test_sidebar_navigation.py` verifies navigation and disclosure. |
| Right-click Favorites | A destination-bound popup adds/removes that exact identity. Favorites precede the full list and retain their original entries. Native and F8 paths are covered in `test_sidebar_native_pipeline.py`. |
| Saved custom/alphabetical order | Existing bounded favorite identities remain compatible. Sidebar preferences save order and expanded panes separately. Alphabetical display retains the custom sequence. Persistence round trips are covered in sidebar tests. |
| Drag reorder | A press captures a favorite, motion previews the move, and release verifies the current drop target. Outside drops cancel; resize, changed mode, and stale geometry invalidate capture. Edge scrolling and release without a final motion have regressions. |
| Mouse operation across menus | Legacy owners share `ui/modal_pointer.py`; modern forms retain their native gesture handlers. Layout, columns, tables, settings, project, execution, activity, exports, alerts, diagnostics, logs, analysis, and palette expose explicit actions and exits. Tests include `test_modal_pointer.py`, `test_modal_session_actions.py`, `test_table_mouse_menus.py`, `test_workspace_layout_mouse.py`, `test_project_mouse_workflows.py`, `test_execution_mouse_controls.py`, and `test_analysis_mouse_navigation.py`. |
| Mouse field completion | Setup, onboarding, Connect Data, telemetry settings, Launch job, and Export data share Keep field/Cancel edit controls. Existing validation owns the result; keeping a field does not save a configuration or launch a job. `test_form_edit_mouse.py` covers native/SGR, F8, invalid input, stale state, small screens, and duplicate events. |
| Job context actions | Right-clicking an unmarked row targets that job; a marked row targets the marked selection in its list. Applicable Hold/Release/Cancel choices retain frozen identity and disclose exclusions. Existing confirmation and mutation backends remain authoritative. `test_job_action_pointer.py` and existing grouping/mutation suites verify these paths. |
| Confirmation mouse correctness | Native press/release must match the same review and painted control. Opening gestures and subsequent aggregate clicks cannot confirm or activate an underlying pane. Known cluster, user, and account identities are rechecked before action. |
| Curated palettes and default | Gruvbox, Modnokai, Darcula, Catppuccin Mocha, Nord, Tokyo Night, Rosé Pine Dawn, and System appear in the dedicated Themes menu. Gruvbox is the configuration default. `test_curated_themes.py` verifies contrast, aliases, basic-color status hues, live updates, and persistence. |
| System inheritance | ANSI output uses native palette slots and default text/background. Curses uses native slots when available; direct-color curses retains terminal defaults with emphasis rather than inventing RGB values. No terminal/desktop queries or shell commands run during rendering. |
| Compatibility and accessibility | Old palette names migrate on read. Existing reader, monochrome, high-contrast, and color-vision modes remain accepted outside the curated menu. Keyboard/F8 access, ASCII, `NO_COLOR`, top-level Quit, embedded terminal ownership, and streamed navigation remain functional. `test_unified_workspace_controls.py` exercises the integrated native path. |

## Design decisions

A static rail plus the old Pin command would have been smaller, but would not
support right-click pinning before visiting a destination or saved drag order.
The implementation instead reuses the existing favorite identities and one
shared destination catalog. It stores at most 24 favorites. Projection uses
O(D + F) storage and O(D + F log F) work for alphabetical display, where D is
the destination count and F is the bounded favorite count. Custom display is
linear. No job-array copy or scheduler read is needed to paint navigation.

Native pointer normalization is confined to explicitly listed legacy owners.
New forms keep their own gesture handlers. This avoids rewriting each old
handler while preserving control identity, review state, cancellation, and
duplicate-event protection. A shared field-edit footer handles local edits;
backend operations remain in their established owners.

The 80-column dashboard reserves 17 columns for navigation. Narrower terminals
use a smaller rail with independent scrolling and full accessible labels.
Plain streamed output keeps its horizontal navigation rather than printing a
noninteractive sidebar. Modals retain foreground input ownership.

## Verification

The full integrated check is:

```bash
python -m pytest -q -n 2
python scripts/check_quality.py
python scripts/generate_operation_reference.py --check
python scripts/generate_capability_reference.py --check
python scripts/validate_operation_schemas.py
python -m build
```

Installed-wheel checks use `scripts/qualify_flagship.py`, which exercises a real
private PTY and the installed commands outside the checkout. The source archive
must contain the new UI modules, tests, and user guide. CI records the exact
revision associated with each final result; historical successful runs are not
evidence for a newer code tree.

Focused review found and corrected cross-dialog duplicate clicks, stale
favorite drop positions, inherited-theme selection contrast, duplicate F8 pane
stops, wrong global row hitboxes, and ASCII theme labels. Existing behavioral
assertions were retained when coordinate helpers were updated for the sidebar.
Integration checks also retain foreground right-click clearing, exact retained
log-line selection, raw terminal-probe reports, and notice identity including
the task and file path. Unpainted/streamed navigation cannot claim document
cells. Disabled controls explain their reason without dispatching an action.
Compact menus keep their selected content visible, and expanded metric sliders
reserve at least three track cells.
Legacy Details and footer-filter modes use contextual toolbar controls for
Close/scroll and Apply/Clear. These delegate to the existing edit semantics,
dismiss an overlaid menu first, and guard duplicate clicks from activating
Launch after the dialog closes.

## Performance evidence and limits

The existing `scripts/benchmark_ui.py` harness compared the base and candidate
with identical 80×30 fixtures: 100 queue jobs, 500 history records, Unicode,
40 render iterations and 150 hover iterations. Runs were sequential on the same
cloud host, outside competing heavy tests.

| Measurement | Base median | Candidate median |
| --- | ---: | ---: |
| Full-frame CPU work | 3.983 ms | 4.146 ms |
| Ordinary pointer hover | 0.253 ms | 0.276 ms |

Both runs reported zero source I/O attempts. Hover retained zero composition
and graph-publication work per measured frame. These measurements show small
local overhead, not a speedup. They exclude terminal transport and wait time;
they do not measure physical GPU behavior, live production scheduler latency,
SSH performance, process RSS, or Python allocation peaks. Theme inheritance
depends on the terminal's capabilities and user palette; System cannot certify
contrast for unknown user-selected colors.

## Installation

No config migration command is required. Existing favorite identities and user
configuration remain valid; new sidebar preferences are additive. After this
revision reaches the installation's selected branch, close Raven and run
`raven update`, then reopen it. A source checkout can use
`python3 scripts/install.py --no-setup` after updating that checkout.

See [Navigation and themes](../guides/navigation-and-themes.md) for user-facing
controls and [Installation](../INSTALLATION.md) for installation ownership and
branch-selection procedures. An unmerged branch must not be described as
available through the main update channel.
