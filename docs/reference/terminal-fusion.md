# Terminal fusion delivery matrix

[User guide](../guides/terminal-fusion.md) · [Workflows](../guides/terminal-workflows.md) ·
[Original 100-entry design](../design/terminal-fusion.md)

This record describes version 6.3.0. All four delivery stages have implementation
paths. The original entries overlap; they are not 100 independent products or
100 claims of universal compatibility. Each row below identifies the delivered
mechanism and any narrower form than the design proposed.

## Pane, process and interaction

| Design IDs | Delivered mechanism and scope | Primary regression source |
| --- | --- | --- |
| TF01, TF04, TF06 | Shared Details/Terminal host on Jobs, History, Analytics, Dependencies, Logs and Research; per-page mode, preserved Details and shared named sessions | `test_terminal_fusion_integration.py`, `test_terminal_ui.py` |
| TF02, TF03 | Explicit click/key focus and visible host, label, kind/state; hover does not redirect input. The child prompt controls its displayed username | `test_terminal_ui.py` |
| TF05, TF07 | Hidden shells retain state; bounded named picker, rename, next/previous, unread markers and explicit close | `test_terminal_ui.py`, `test_terminal_service.py` |
| TF08 | Existing split resize/maximize/restore and terminal viewport propagation; invisible panes revoke focus | `test_terminal_fusion_integration.py` |
| TF09, TF10, TF11 | Configured interactive or login shell, inherited environment, no dotfile edits. Bash/Zsh/Sh launch paths tested where installed; Fish availability is separate | `test_terminal_workflows.py`, `test_terminal_service.py` |
| TF12, TF13, TF14 | Real controlling PTY, foreground groups, shell job control, line-discipline signals and mode-aware keys | `test_terminal_service.py`, local self-test |
| TF15, TF16 | Final-output drain, EOF/EIO, retained exit screen, owned process cleanup, hide versus close/restart; durable detach through explicit tmux | `test_terminal_service.py` |
| TF17, TF18 | Terminal receives keys before global commands; fixed Ctrl-] prefix, t escape and doubled literal. Prefix remapping is not exposed in this release | `test_terminal_input.py`, `test_terminal_ui.py` |
| TF19, TF20 | Tested application cursor/keypad, function, Control and Alt encodings. Unsupported extended keyboard negotiations are not advertised | `test_terminal_emulator.py`, `test_terminal_input.py` |
| TF21, TF22 | Framed decoder, original session/focus/geometry identity, bounded paste and incomplete-protocol containment | `test_terminal_input.py`, `test_terminal_fusion_integration.py`, `test_terminal_curses.py` |
| TF23, TF24, TF25, TF26 | Pane-local mouse protocols, explicit copy-mode override, original gesture capture and child-motion preservation | `test_terminal_input.py`, `test_terminal_ui.py` |
| TF27, TF28 | Child focus events and Tower modal ownership; stale releases and hidden input leases rejected | `test_terminal_input.py`, `test_terminal_fusion_integration.py` |
| TF29, TF30, TF31 | Exact bounded paste, queued short writes, visible rejection/cancellation and configurable multiline preview; cancellation cannot undo delivered bytes | `test_terminal_service.py`, `test_terminal_ui.py` |
| TF32, TF33, TF34 | Explicit selection-only copy, frozen text identity and actual soft-wrap metadata; ordinary shell y stays input | `test_terminal_ui.py`, `test_terminal_emulator.py` |
| TF35, TF36 | Line and byte bounds, coherent paged scrollback, rail/follow/top/bottom, retained search and visible eviction boundary | `test_terminal_service.py`, `test_terminal_ui.py` |

## Emulator and performance

The selected adapter uses pyte 0.8.x and wcwidth. Both are optional dependencies.
The isolated broker contains parser work and avoids forking Python's threaded
UI. Full immutable screen snapshots are coalesced after damage detection. The
UI paints changed rows. This trades some IPC bandwidth for recovery without
dependent-delta loss; it does not serialize a screen per output byte.

| Design IDs | Delivered mechanism and scope | Primary regression source |
| --- | --- | --- |
| TF37, TF38, TF39 | Separate normal/alternate screens, tested VT editing/scrolling, fragmented UTF-8 and controls | `test_terminal_emulator.py`; real Vim PTY test |
| TF40 | Wide and combining cells, malformed-byte replacement, explicit combining bounds; no complex emoji shaping | `test_terminal_emulator.py` |
| TF41 | xterm-256color profile and tested primary/status/cursor replies; unsupported queries counted/contained | `test_terminal_emulator.py` |
| TF42, TF43 | Theme-owned defaults/chrome, preserved explicit RGB, clipped cursor and blink. Bar cursor uses a cell approximation | `test_terminal_installation.py`, `test_terminal_ui.py` |
| TF44, TF45 | TIOCSWINSZ, SIGWINCH, coalesced geometry and epoch guards. Normal screen clips on resize; no text reflow | `test_terminal_service.py`, `test_terminal_input.py` |
| TF46, TF47, TF48 | Bounded OSC/DCS, explicit clipboard/link review, denied clipboard reads, character-only rendering and allocation fullscreen fallback. URI copy does not auto-open | `test_terminal_emulator.py`, `test_terminal_ui.py`, `test_terminal_workflows.py` |
| TF49, TF50, TF51 | Nonblocking selectors, independent broker, 4 KiB fair parse slices and 12 ms processing budget; collection workers do not own PTYs | `test_terminal_service.py` |
| TF52, TF53 | Damage-triggered immutable publication, sequence/epoch validation and one UI painter; changed-row feedback bypasses graph/table rebuild | `test_terminal_input.py`, `test_terminal_fusion_integration.py` |
| TF54, TF55 | Rotating ready sessions, visible/hidden publication limits and unread notices | `test_terminal_service.py`, `test_terminal_fusion.py` |
| TF56, TF57 | Ordered bytes, bounded input/IPC/history/parser/record queues, explicit rejection and eviction; producer backpressure is possible | `test_terminal_service.py`, `test_terminal_emulator.py`, `test_terminal_recording.py` |
| TF58 | Parser duration, queue/output diagnostics, cached feedback and sustained-pointer measurements. These do not measure physical end-to-end Termius latency | `test_terminal_input.py`, existing pointer benchmark |

## HPC, shell context and evidence

| Design IDs | Delivered mechanism and scope | Primary regression source |
| --- | --- | --- |
| TF59, TF60, TF61 | Explicit SSH arguments using normal host configuration, visible process lifecycle, no command replay/reconnect. Authentication state remains the SSH client's output | `test_terminal_workflows.py` |
| TF62, TF63, TF64 | Existing reviewed operation extended for embedded/fullscreen display, exact allocation/array/component, node, numeric step and supported task rank | `test_terminal_workflows.py`, existing operation tests |
| TF65, TF66, TF67 | No hidden allocation, exact-attempt revalidation, cached attachment status, bounded one-task/CPU srun shell and expiry checks | `test_terminal_workflows.py` |
| TF68, TF69, TF70 | Explicit accessible local/job directory, shell-profile quoting and insertion preview, existing review for generated scheduler operations. User confirms a known shell is currently at its prompt | `test_terminal_workflows.py` |
| TF71, TF72 | Optional Bash/Zsh/Fish hooks, private same-user token-authenticated Unix socket, bounded versioned reports and revoked closed-session credentials | `test_terminal_fusion.py`, `test_terminal_fusion_integration.py` |
| TF73, TF79 | Bounded observed command blocks, duration/status, copy and hidden completion notices. A block captures the latest viewport at observed completion; it can include older text and omit scrolled output | `test_terminal_fusion.py` |
| TF74, TF75, TF76, TF77 | Exact reported job references and declared project/artifact handoffs validated against cached identity and registered paths; explicit navigation | `test_terminal_fusion.py`, `test_terminal_fusion_integration.py` |
| TF78, TF82 | Prepared diagnostics, parameterized shell-aware recipes, private versioned import/export and no automatic Enter | `test_terminal_workflows.py` |
| TF80, TF81 | Evidence bookmarks with source/clock/attempt context; hook text is observation, never authorization | `test_terminal_fusion.py` |
| TF83, TF84, TF85 | Command timeline, nonzero-result cards, bounded pinned evidence and already-cached metric samples; missing coverage/skew remain explicit | `test_terminal_fusion.py` |
| TF86, TF87 | Bounded asynchronous block comparison with declared normalization and retained cross-session filtered search | `test_terminal_fusion.py` |
| TF88 | Complete export preview, exclusions, private exclusive file creation and frozen export identity. No promise to reproduce arbitrary shell side effects | `test_terminal_fusion.py` |
| TF89 | Opt-in bounded output/resize/time recording and separate inert timed player; pause/seek/speed controls, never command re-execution | `test_terminal_recording.py`, `test_terminal_playback.py` |
| TF90 | Explicit local tmux create/attach on a dedicated server socket; tmux detach preserves its session | `test_terminal_workflows.py` |
| TF91, TF92 | Health view and bounded exported compatibility profiles comparing fresh local probes/runtime metadata; installed tools are not marked tested merely because present | `test_terminal_installation.py`, `test_terminal_service.py` |
| TF93, TF94 | Up to four explicit SSH destinations, allocation identity in reviewed sessions and deliberately armed one-shot broadcast insertion. No automatic all-rank fanout or persistent broadcast typing | `test_terminal_workflows.py` |
| TF95 | Manually advanced bounded diagnostic sequence, per-step preview and cancellation. Results remain terminal/evidence observations; no autonomous maintenance agent | `test_terminal_workflows.py` |
| TF96, TF97, TF98 | Existing acceptance review handoff, explicit container exec adapters, allowlisted environment and supplied package/module differences | `test_terminal_workflows.py`, `test_terminal_fusion.py` |
| TF99 | Private same-user redacted read-only file snapshots. Authenticated cross-user live sharing and remote write access are outside this release | `test_terminal_fusion.py` |
| TF100 | Real local controlling-PTY/input/resize/render/exit self-test with structured results and optional profile export | `test_terminal_installation.py` |

## Validation boundary

The release checks include real local PTYs and subprocesses, six-page integrated
interaction tests, Python 3.10/3.12/3.14 checks, the complete repository suite,
package qualification and sustained-pointer regression measurements. See the
[validation record](../guides/terminal-validation.md) for measured results.

This environment has no live Slurm allocation or authenticated remote cluster.
Live SSH authentication, site-specific srun/sattach policy, multi-node execution,
container deployments, physical Termius/tmux/screen chains, and unavailable
applications require installation-specific testing. Test fixtures and adapter
argument checks are not presented as live-cluster certification.
