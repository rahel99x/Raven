# Synchronized terminal output

Raven's interactive dashboard asks the terminal whether it supports DEC private
mode 2026. When the terminal reports switchable support, Raven brackets each
changed frame's terminal flush with begin/end synchronization sequences. It does
not hold a synchronized region open while collecting data or waiting for input.

Set `synchronized_output` in the dashboard configuration to one of:

| Value | Behavior |
| --- | --- |
| `auto` (default) | Send one capability query; use synchronization only after a supported response within one second. |
| `off` | Keep ordinary painting and send no capability query. |
| `on` | Explicitly enable synchronization; support has not been verified. |

The terminal diagnostic view shows the capability decision. Unsupported terminals,
noninteractive output and unanswered probes retain ordinary painting. Delayed or
duplicate replies to Raven's probe are consumed, including while an embedded shell
owns keyboard input. They do not become shell commands or keystrokes.

Support through SSH or tmux depends on the actual terminal and intermediary.
Raven does not infer it from a terminal brand or force passthrough. The `--watch`
text interface does not use this curses frame protocol. ASCII and Unicode content
are unchanged. Each synchronized changed frame adds 16 control bytes; unchanged
frames add none. Synchronization may reduce visible partial redraws on supporting
terminals, but does not remove network latency. No human tearing study has been
completed for this revision.

Implementation and qualification evidence: [W19](../implementation/review-2026-10/evidence/W19.md).
