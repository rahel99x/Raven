# Pointer input audit

[README](../../README.md) · [Pointer controls](pointer-navigation.md) · [Display performance](ui-performance.md)

Version 4.9.0 introduced a shared byte decoder for terminal mouse reports and keyboard input.
This record documents reproduced faults, their corrections, and a manual validation procedure.
The audit covers the shared input path and page-specific pointer handlers.
Its results apply to the tested protocols and sequences; they do not qualify every terminal.

## Reproduced faults and corrections

| Fault | Cause | Correction |
| --- | --- | --- |
| Movement opens Logs at wider coordinates | Ncurses consumed a UTF-8 X10 mouse header and three bytes. At column 120 and row 75, the remaining coordinate byte was `l`, which opened Logs. | Disable ncurses keypad decoding. Read raw bytes and decode the complete report in Raven. Decode UTF-8 incrementally and retain legacy 8-bit coordinate bytes. |
| Movement opens Research | A timeout after the first Escape byte released the delayed report body as keyboard input. `[` selects the previous tab, which takes Jobs to Research; a coordinate `r` forces a refresh. | Recover a delayed mouse prefix after Escape. Decode SGR, X10, and urxvt reports without replaying their contents as shortcuts. |
| A delayed pointer prefix cancels a drag | The first Escape byte can arrive separately from the rest of a report. | Allow a bounded prefix grace period during recent pointer activity or an active capture. A genuine Escape still cancels the gesture after the timeout. |
| An inherited mouse encoding leaks a coordinate | Extended X10 or pixel reporting can remain enabled after another application exits. | Clear modes 1005, 1015, and 1016 before enabling cell-based SGR 1006; clear them again on exit. Keep report decoding in Raven when a terminal still sends a legacy format. |
| Research opens after leaving a command | A background project command completed after the user changed workspace. | Bind completion to its original workflow. Retain detached results without changing the current page, source, or selection. |
| Hover stalls while samples are saved | Sampler file operations held the same lock used by foreground job checks. | Complete the atomic data update under the lock, then perform persistence outside it. |
| Opening or refreshing Analytics blocks | Saved series and their directory inventory were read in the foreground. | Restore saved samples and inventory through the existing background executor. Show restore status in the view. |
| Fresh samples cause repeated graph spikes | Inline Job Series rasterized off-screen metric bands. | Render the viewport while retaining the complete scrollable document and source-bound controls. |
| A delayed click opens old content | A job, source, viewport, or pane changed after the hit map was published. | Bind activation to immutable hit contents, current geometry, and the exact job or source. |
| A double-click bypasses row validation | Follow-up activation used an old row without checking its horizontal location and current frame. | Validate the row before applying double-click activation. |
| Hover continues an old drag | A release report was lost before a known no-button report arrived. | End captures when the decoded protocol proves that no button is held. Preserve compatibility when native curses reports do not provide that evidence. |
| A new graph drag loses its release | A cancelled owner retained a release marker after another owner claimed the next press. | Clear old release markers before routing a new gesture. |
| Details activates another job's control | The action checked an old proxy identity without checking the current selected job. | Validate the current job and the visible Details rectangle. |
| Hover rebuilds the document | Uncaptured held-button reports were classified as content changes. | Treat those reports as cosmetic until a content capture requires an update. |
| Crosshair movement repeats large writes | Changed rows restored complete chart rows and many separate segments. | Compose the final cells and repaint changed runs. Restore wide and combining glyphs as complete characters. |
| Unchanged native charts rasterize repeatedly | Maintenance updates rebuilt the same measured curves. | Reuse bounded rasters keyed by complete data and rendering settings. Continue publishing current controls and source age. |
| Menus or drag handles use old geometry | Capture validation waited for the next paint. | Check context and geometry before handling the next event. |

The tests also cover invalid coordinates, changed nested hit payloads, sparse tall controls,
same-position drag reports, theme changes, overlays, and interrupted capture handoffs.
Pointer feedback performs no measurement-source I/O in the tested memory fixtures.
Scheduler actions retain their existing review step.

The raw decoder reads the terminal's keyboard capabilities and retains arrow keys, function keys F1–F24,
Ctrl and Alt navigation, Unicode input, bracketed paste, and resize events.
Paste completion keeps ncurses keypad decoding disabled.
Resize events preserve a partial report or Unicode character.
Only the first byte read can use the frame's idle timeout; continuation bytes do not block a frame.
Ctrl-C remains available through a partial or malformed report. Inside bracketed paste, it remains inert payload.
Recognized SS3 and terminal-specific key prefixes survive delayed fragments.
Unknown parameterized SS3 controls remain one bounded control sequence.
An invalid suffix does not replay the prefix as a command. Keyboard-only Escape keeps its separate timeout.

Raven requests cell-based SGR reports, whose numeric coordinates have explicit boundaries.
Legacy 8-bit X10 and UTF-8 X10 can assign different meanings to the same byte sequence.
The decoder handles the tested legacy formats without leaking coordinate bytes into shortcuts;
it cannot infer every ambiguous legacy coordinate with certainty.
A partial UTF-8 coordinate retains at most three bytes until more input arrives.
It does not expire into a shortcut. An isolated legacy high byte can also remain pending until the next input byte.
The wider-coordinate fault also reproduces in the pre-4.8 code; it was a latent input-path fault.

During an active capture or recent pointer activity, a bare Escape prefix has a 200 ms grace period.
Keyboard-only Escape retains its 30 ms decoder timeout. Frame and process scheduling can add to these times.
An indefinitely delayed report cannot be distinguished from a genuine Escape key without waiting indefinitely.
If its prefix exceeds the grace period, Escape can cancel the gesture; its later mouse body remains quarantined.
Recognized partial SGR reports retain their bounded prefix so a delayed packet can still update the pointer.

## Check the update in a terminal

**Prerequisites:** Install Raven 4.9.0 or later.
Use a terminal that reports pointer movement.

1. Open Jobs and select a running job.
2. Open Analytics → Job Series in Details.
3. Move the pointer repeatedly between job rows, the divider, and each graph.
4. Drag a graph interval and release inside its capture margin.
5. Right-click the graph to reset it.
6. Start another drag, press Esc, then start a new graph drag.
7. Resize the terminal and select another job.
8. Repeat movement in Analytics, Research, History, and Logs.
9. Enter a command after a burst of movement reports.

**Expected result:** Movement leaves the page and selected job unchanged.
The crosshair follows the latest reported cell.
Only a deliberate valid click activates a control.
Each new drag receives its own release.
Resizing or changing the source cancels a stale capture.
The command remains available after the movement burst.

Use `:terminaltest` to inspect missing movement, drag, or release reports.
Use `:terminaldoctor` to inspect terminal and connection settings.
Press Esc to leave the test.
See [display performance](ui-performance.md) for the synthetic benchmark and its limits.
Use `tower --ui-trace ui-trace.json` when movement still stalls. Reproduce the issue, quit normally,
and inspect the saved phase timings and page transitions.

## Run the regressions

Run the development tests from the source directory:

```bash
python3 -m pytest -q
```

Focused regression files include `test_raw_terminal_input.py`, `test_mouse_protocol_pty.py`, `test_mouse_protocol_safety.py`,
`test_hover_navigation_guards.py`, `test_mouse_capture_adversarial.py`,
`test_pane_hover_capture_safety.py`, `test_screen_cell_painter.py`,
and `test_native_metric_raster_cache.py` under `tests/`.
These tests exercise both ASCII and Unicode paths where applicable.
Installed-package checks exercise all main pages and Research views with synthetic Slurm data.
The tests do not require or modify a user's live scheduler jobs.

The 4.9.0 raw-input checks exercise extended UTF-8 and 8-bit X10 coordinates, every byte boundary in the
tested reports, and interrupted graph gestures through the real App input path.
Four pseudo-terminal profiles (`screen`, `screen-256color`, `tmux-256color`, and `xterm-256color`)
also exercise their actual keyboard capability sequences, fragmented Unicode text, paste, and SIGWINCH resize.
SS3 arrow and function-key fragments are separated by 120 ms and 300 ms in those checks.
They also test a 300 ms delay at every byte boundary in the exact UTF-8 report that previously leaked `l`.
The exact coordinate corpus that previously leaked `l` now retains the full pointer report and leaves Jobs selected.

The 4.8.3 delayed-input matrix covered 220 cases across all report byte boundaries under four TERM profiles.
It retained all 440 pointer reports, with no leaked refresh commands or page changes.
Two actual curses-loop checks each accepted 301 fragmented mixed-protocol reports and preserved Jobs and its selection.
A separate real CLI run handled 600 rapid SGR reports and saved a valid timing report on quit.
These checks exercise curses in a pseudo-terminal; they do not emulate the Termius client itself.

The earlier 4.8.2 release check passed 8,949 tests on Python 3.12.
The installed wheel passed 44 ASCII and Unicode display checks and three native Slurm command-fixture checks.
Real curses input checks passed SGR, X10, and urxvt reports under `screen`, `screen-256color`,
`tmux-256color`, and `xterm-256color` terminal descriptions.
Runtime and launcher sources also passed a Python 3.10 grammar check.
These checks used a Linux cloud runner. They did not measure a live CARC or Fedora GPU allocation.
