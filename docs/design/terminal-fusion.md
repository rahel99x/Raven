# CLI fusion: embedded terminal design

[README](../../README.md) · [Workspaces](../guides/adaptive-workspaces.md) · [Allocation operations](../guides/operations-cluster.md) · [Selection](../guides/pane-navigation.md)

**Status: implemented in 6.3.0 with the explicit scope recorded in the
[delivery matrix](../reference/terminal-fusion.md).**
Use the [terminal guide](../guides/terminal-fusion.md) for current installation
and controls. This document retains the original design catalog and acceptance
criteria; a proposed variant is not a promise of universal application support.

## Intended experience

Add a shared **Details | Terminal** control above the secondary pane.
Details retains the existing job inspector, Logs, Research, Analytics, and other contextual views.
Terminal shows a real interactive shell while Tower continues to display jobs and metrics.

```text
File  Edit  View  Help                              Updates: 1s
Jobs  History  Analytics  Dependencies  Logs  Research
+----------------------------+--------------------------------+
| Jobs / Recents             | [ Details ] [ Terminal ]       |
|                            | local:project  [+]             |
| selected jobs              | user@host ~/project $          |
|                            |                                |
|                            |                                |
+----------------------------+--------------------------------+
 Terminal focused | local host | bash | Ctrl-] then t: Tower
```

The drawing illustrates the pane arrangement. It is not a screenshot.
The implemented escape prefix is Ctrl-], then t.

Start with one lazily created local session shared across pages.
Keep the Details/Terminal preference per page.
Add named sessions when the user requests them.
Do not create one shell for every selected job.
Selecting another row must not change an existing shell's host, working directory, environment, or input destination.
Offer an explicit **Open for selected job** action instead.

The terminal runs on the machine that runs Tower.
For example, Tower inside an SSH connection to a login node starts its local shell on that login node.
A remote Slurm data connection does not silently turn this into a shell on the remote cluster.
Always display the actual terminal destination separately from the scheduler connection.

## Architecture and ownership

```text
Outer terminal input
  -> one framing decoder
  -> explicit focus and gesture owner
       -> existing Tower controls
       -> session-bound semantic input -> broker input encoder

Owned terminal broker process
  -> nonblocking PTY read/write selector
  -> one VT emulator state per session
  -> mode-aware key/paste encoding -> PTY write queue
  -> bounded, ordered screen changes and terminal-mode updates
  -> immutable IPC messages

Tower UI thread
  -> verify session, geometry, screen epoch and update sequence
  -> clip changed cells to the secondary pane
  -> existing differential painter
```

Use a real pseudo-terminal, or PTY. Pipes alone do not provide interactive shell job control.
Use an established VT terminal emulator to interpret the child output.
Only Tower's UI thread can write to curses or change the outer terminal's modes.
Child output never goes directly to Tower's physical terminal.

An isolated broker is the preferred starting design.
It separates terminal parsing from Python UI execution and from Slurm collection workers.
One selector can service several PTYs; a shell does not require a permanent sampler worker or a polling thread.
Benchmark IPC costs before selecting the final transport and snapshot format.
Do not serialize the complete terminal grid for every output byte.

Launch the broker with a fresh executable process.
Use a clean child launcher to establish the controlling TTY and execute the selected shell.
Avoid Python `preexec_fn` and direct `pty.fork()` in Tower after its background threads have started.
Set the child session, controlling TTY, descriptors and process groups correctly.
`start_new_session=True` alone does not establish every controlling-TTY requirement.

Keep these states separate:

| State | Required identity and ownership |
| --- | --- |
| Pane presentation | Page, Details/Terminal mode, bounds, focus generation, active session |
| Terminal session | Stable session ID, transport, child PID, owned PTY, emulator, size epoch, exit status |
| Job attachment | Cluster/connection, owner, allocation, exact job attempt, optional step/node/task; independently revalidated |
| Input transaction | Originating session, focus generation, byte order, paste or mouse capture, progress |
| Terminal selection | Session, normal/alternate buffer, immutable line IDs, retained selected text |
| Screen publication | Session, screen epoch, geometry, sequence number, cursor, terminal modes, changed cells |

If an update is missing or its geometry is obsolete, request a fresh complete screen snapshot.
Do not combine cells from different sessions or resize epochs.
Encode keys and paste in the broker using its latest parsed child-mode state.
The UI must not encode input from an obsolete copy of application-cursor or paste modes.
Coalesce complete rendered states only when the receiver can recover their contents.
PTY bytes and dependent screen deltas cannot be dropped arbitrarily.

### Emulator choice

| Candidate | Advantage | Required proof before selection |
| --- | --- | --- |
| Maintained libvterm adapter | Native terminal state, keyboard/mouse encoding, cursor and damage callbacks | Supported upstream release, maintained binding, Unicode and protocol coverage, wheel/offline installation, license review, crash containment |
| pyte adapter | Python implementation; easier platform deployment and experimentation | Actual alternate-screen, input-mode, query, Unicode and application compatibility; output-flood CPU and memory measurements |
| tmux-backed transport | Sessions can survive Tower disconnection or exit | Explicit session/socket ownership and attach lifecycle; an embedded terminal still needs an emulator |

Prefer the native engine if its conformance and packaging checks pass.
Use a small adapter contract so that the pane host does not depend on one engine's private API.
Keep the core Tower installation usable without the optional terminal engine.
A fallback engine must report its supported capabilities rather than silently changing behavior.
Do not assume that an ANSI color parser can support Vim or interactive job control.

The `neovim/libvterm` repository currently directs development to Neovim's bundled implementation.
Do not select that older fork solely because of its repository name.
Select and pin an actively maintained, embeddable implementation after the feasibility work.

## Capability catalog

The following 100 entries are the original design mechanisms. See the
[delivery matrix](../reference/terminal-fusion.md) for their implemented form,
deliberate variants, limits and validation sources.
Stages mean: **A** = local terminal foundation; **B** = interaction and compatibility; **C** = HPC and workflow integration; **D** = advanced optional capabilities.

### Pane and session controls

| ID | Stage | Capability and mechanism |
| --- | --- | --- |
| TF01 | A | **Details / Terminal buttons.** Place the control above the existing Details implementation so every supported page uses the same host. |
| TF02 | A | **Explicit focus.** Mouse activation or keyboard navigation assigns input; hover only changes the hover style. |
| TF03 | A | **Visible destination.** Show focused pane, host, user, session name and attachment type even when shell prompts omit them. |
| TF04 | A | **Preserved Details state.** Switching to Terminal retains the selected job, graph zoom, log selection, scroll and Details subview. |
| TF05 | A | **Preserved shell state.** Hide and reopen a shell without losing its directory, environment, aliases or running command. |
| TF06 | A | **Shared session across pages.** Bind pages to a session ID; page navigation does not start another shell. |
| TF07 | B | **Named session picker.** Add, rename, switch and close sessions with bounded counts and unread-output indicators. |
| TF08 | B | **Responsive layout.** Reuse divider drag, maximize and restore; use a full-pane toggle or stacked layout on narrow displays. |

### Shell and process correctness

| ID | Stage | Capability and mechanism |
| --- | --- | --- |
| TF09 | A | **Real interactive shell.** Support a configured executable and tested Bash, Zsh and Fish launch profiles. |
| TF10 | A | **Normal user environment.** Preserve shell completion, history, functions, modules, virtual environments and Conda without copying an entire environment into saved Tower state. |
| TF11 | A | **Startup-file policy.** Define interactive versus login-shell behavior. Do not edit user dotfiles or source a batch submission script automatically. |
| TF12 | A | **Controlling terminal.** Give the child a real controlling PTY with correct descriptors, session and foreground process groups. |
| TF13 | A | **Job control.** Test foreground/background jobs, pipelines, `Ctrl-Z`, `fg`, `bg`, terminal echo and raw/canonical modes. |
| TF14 | A | **Signal semantics.** Deliver `Ctrl-C`, `Ctrl-D`, `Ctrl-Z` and flow-control keys as terminal input; let the child line discipline interpret them. |
| TF15 | A | **Exit lifecycle.** Drain final output, handle Linux PTY EOF/EIO, collect exit status, reap the owned child and retain an exited-session screen. |
| TF16 | B | **Explicit close policy.** Distinguish hide, detach, terminate and restart. Act only on owned processes; closing a pane is not a scheduler cancellation. |

### Keyboard, mouse and focus

| ID | Stage | Capability and mechanism |
| --- | --- | --- |
| TF17 | A | **Exclusive keyboard owner.** Route terminal input before Tower's global quit, tab, grouping, copy, palette and graph bindings. |
| TF18 | A | **Escape prefix.** Provide a configurable prefix for Tower focus and a way to send that prefix literally. Avoid capturing every `Esc` or `Ctrl-W`. |
| TF19 | B | **Application key modes.** Encode arrows, Home/End, function keys and keypad input according to the child's requested modes. |
| TF20 | B | **Modifier compatibility.** Test Alt/Escape ambiguity, Control keys and supported extended keyboard protocols; advertise only tested modes. |
| TF21 | A | **Session-bound input.** Tag queued input with its original session and focus generation. A page change must never redirect queued bytes. |
| TF22 | B | **One framing decoder.** Preserve complete key, mouse, paste and terminal-reply frames before choosing the recipient. |
| TF23 | B | **Child mouse modes.** Honor requested button, drag and motion reporting; convert outer coordinates to pane-local coordinates. |
| TF24 | B | **Selection override.** Provide an explicit copy mode or modifier so users can select text when Vim or another child owns the mouse. |
| TF25 | B | **Gesture capture.** A terminal drag cannot activate graph zoom, job grouping, a divider or a different tab. Handle release outside the pane. |
| TF26 | B | **Application-aware batching.** Preserve meaningful child drag/button events; coalesce only events whose semantics allow it. |
| TF27 | B | **Focus negotiation.** Send supported focus-in/out notifications to the child; keep the physical terminal reporting modes owned by Tower. |
| TF28 | B | **Modal arbitration.** Tower menus and dialogs get an explicit focus lease. Restore the previous terminal only after consuming stale releases and pending prefix state. |

### Paste, selection and scrollback

| ID | Stage | Capability and mechanism |
| --- | --- | --- |
| TF29 | B | **Exact paste framing.** Use the child's bracketed-paste mode and retain all approved payload bytes. Do not reuse the palette's short input limit. |
| TF30 | B | **Bounded large-paste queue.** Handle short writes, EAGAIN, progress and cancellation without blocking the UI or silently truncating a command. Report bytes already delivered; cancellation cannot retract them or undo an executed command. |
| TF31 | B | **Multiline preview.** Offer a configurable preview before transmitting multiline content. Inserting a suggested command does not append Enter. |
| TF32 | B | **Terminal copy mode.** Live-shell `y` types `y`; copy mode exposes explicit selection-only copy and editor-yank actions. |
| TF33 | B | **Stable line identities.** Pin selected text across new output and refuse expired selections instead of copying another line or the whole screen. |
| TF34 | B | **Wrap-aware extraction.** Distinguish soft wrapping from original newlines; copy Unicode without splitting wide or combining characters. |
| TF35 | B | **Bounded scrollback.** Limit both lines and bytes, provide a rail, follow toggle, top/bottom controls and a visible eviction boundary. |
| TF36 | B | **Search and navigation.** Search retained text, step through results, preserve selection while scrolling and distinguish clear-screen from clear-history. |

### Emulator and output containment

| ID | Stage | Capability and mechanism |
| --- | --- | --- |
| TF37 | A | **Normal and alternate buffers.** Keep shell history separate from full-screen applications such as Vim, less and htop. |
| TF38 | A | **Cursor and screen operations.** Implement tested cursor addressing, erase, insert/delete, scroll regions, tab stops and saved cursor state. |
| TF39 | A | **Incremental decoding.** Preserve fragmented UTF-8 and escape sequences across arbitrary PTY read boundaries. |
| TF40 | B | **Unicode cell policy.** Define behavior for wide characters, combining marks, ambiguous width, malformed bytes and complex emoji; expose tested limits. |
| TF41 | A | **Terminal capabilities.** Set a matching `TERM` and provide terminfo where needed. Terminal queries receive supported replies from the emulator. |
| TF42 | B | **Terminal color policy.** Theme pane chrome and default colors; preserve explicit application colors. Approximate RGB only when the outer renderer requires it. |
| TF43 | B | **Cursor rendering.** Display the child cursor at the clipped cell; support tested shapes and blinking without disturbing Tower's own cursor. |
| TF44 | A | **Resize propagation.** Send the actual PTY viewport size through `TIOCSWINSZ`; deliver the resulting resize notification and verify application redraw. |
| TF45 | B | **Resize stability.** Coalesce redundant sizes, preserve screen epochs and define normal-buffer reflow separately from alternate-screen resize. |
| TF46 | A | **Control-string containment.** Bound OSC/DCS payloads and consume unsupported sequences as framed data; never let them change outer input modes or window geometry. |
| TF47 | B | **Clipboard and links.** Mediate child OSC 52 requests, disable unsolicited clipboard reads, show link destinations and restrict explicit opening actions. |
| TF48 | B | **Character-only compatibility.** Keep graphics protocols outside the initial terminal scope. Preserve an explicit full-screen handoff for applications beyond the embedded compatibility matrix. |

### Responsiveness and resource limits

| ID | Stage | Capability and mechanism |
| --- | --- | --- |
| TF49 | A | **Readiness-driven I/O.** Use selectors and nonblocking descriptors. The Slurm update slider does not slow terminal keystrokes or PTY reads. |
| TF50 | A | **Separate execution owner.** Service PTYs independently of the single/multi collection worker pool; no worker waits for a shell to exit. |
| TF51 | A | **Bounded processing slices.** Limit read, parse and publication work per session so a noisy command does not monopolize input handling. |
| TF52 | A | **Changed-cell publication.** Send bounded damage updates with sequence numbers instead of rebuilding all graphs and job tables. |
| TF53 | A | **Single painter.** Only the UI applies screen changes and writes curses output; broker tasks cannot paint. |
| TF54 | B | **Fair session scheduling.** Use per-session quotas and rotate ready sessions so hidden output cannot starve the visible terminal. |
| TF55 | B | **Hidden-session servicing.** Continue parsing hidden output, avoid hidden-pane painting and summarize unread activity. |
| TF56 | A | **Honest backpressure.** Preserve ordered terminal bytes. When bounded queues fill, slow the producer and show the condition; do not silently discard stream fragments. |
| TF57 | B | **Independent memory budgets.** Bound parser strings, write queues, scrollback, selections and total session memory. Discard old parsed history with explicit coverage. |
| TF58 | B | **Measured latency.** Record input-to-paint latency, parser duration, backlog and output bytes. Smooth viewport motion only; never smooth or interpolate protocol bytes. |

### HPC and remote sessions

| ID | Stage | Capability and mechanism |
| --- | --- | --- |
| TF59 | C | **Explicit local/remote choice.** Label the host that will receive input; do not infer a remote shell from a Slurm data-source setting. |
| TF60 | C | **Normal SSH configuration.** Reuse user host aliases, ProxyJump and host verification. Support interactive authentication through the child terminal. |
| TF61 | C | **Connection lifecycle.** Report connecting, connected, disconnected and uncertain-delivery states. Do not automatically replay commands after reconnect. |
| TF62 | C | **Reviewed allocation shell.** Reuse the existing owned, running-allocation review and revalidate the exact attempt immediately before launch. |
| TF63 | C | **Precise step attachment.** Distinguish a new `srun` shell step from `sattach` input/output on an existing application. Show the effect before attaching. |
| TF64 | C | **Task and node precision.** Select exact array task, heterogeneous component, step, node or supported rank. Reject unsupported or ambiguous targets. |
| TF65 | C | **No hidden allocation.** Opening Terminal starts the chosen shell only. New jobs, allocations and compute-node steps require explicit actions. |
| TF66 | C | **Allocation status.** Show time remaining, attachment validity and expiry. Requeue or reused IDs must not transfer an existing attachment to a new attempt. |
| TF67 | C | **Resource awareness.** A diagnostic shell shares allocation resources. Default helpers must not reserve every CPU/GPU or launch expensive login-node work. |
| TF68 | C | **Working-directory action.** Open a declared, accessible job directory in a new session or insert an explicit directory-change command into a known shell prompt. |
| TF69 | C | **Shell-aware insertion.** Quote selected job IDs, paths and artifacts for the actual shell. Do not assume Bash quoting is valid in every REPL or editor. |
| TF70 | C | **Preserved user authority.** Commands typed by the user behave like ordinary shell commands. Tower-generated scheduler changes retain Tower's existing review controls. |

### CLI and research fusion

| ID | Stage | Capability and mechanism |
| --- | --- | --- |
| TF71 | C | **Optional shell hooks.** Capture prompt boundaries, command start/end, exit status and cwd with supported Bash/Zsh/Fish integration. The terminal also works without hooks. |
| TF72 | C | **Private context bridge.** Use a versioned, bounded local socket/pipe API with session ownership checks. Avoid exposing an unauthenticated network control endpoint. |
| TF73 | C | **Command blocks.** Navigate one completed command and its output, copy that block, and show duration and exit status when hooks establish them. |
| TF74 | C | **Submission linking.** A reporting wrapper can publish the exact `sbatch --parsable` result and run identity. Validate the job before linking it to Tower. |
| TF75 | C | **Project handoff.** Launch reporting helpers from the terminal and update Research from their declared manifests using the existing project standard. |
| TF76 | C | **Artifact handoff.** Explicitly open recognized output files in Logs or Research; resolve paths against the captured host and cwd. |
| TF77 | C | **Job-reference links.** Turn validated job references into explicit navigation actions without changing the user's current page as output arrives. |
| TF78 | C | **Prepared diagnostic commands.** Insert trace, environment, GPU or allocation checks with visible target and arguments; execute only on deliberate action. |
| TF79 | C | **Command result notices.** Report completion in a hidden session without stealing focus or displaying private output in a global notification. |
| TF80 | C | **Evidence bookmarks.** Attach selected command/output blocks to an investigation with timestamp, host, session and exact job-attempt context. |
| TF81 | C | **Context provenance.** Treat shell-reported cwd and command metadata as observations. Text that resembles a prompt or job ID is not scheduler authority. |
| TF82 | C | **Versioned recipes.** Save explicit parameterized command templates with required context, environment assumptions and an editable preview. |

### Advanced optional capabilities

| ID | Stage | Capability and mechanism |
| --- | --- | --- |
| TF83 | D | **Command/metric correlation.** Place observed command start/end markers on the job timeline with clock-source and clock-skew information. |
| TF84 | D | **Failure investigation cards.** Combine a nonzero command result with linked logs and existing advice. Preserve unknown causes and allow dismissal. |
| TF85 | D | **Pinned evidence snapshots.** Capture selected output and corresponding metric ranges as a bounded review artifact; keep live data separate. |
| TF86 | D | **Command-output comparison.** Compare chosen blocks from two sessions or attempts with explicit normalization rules and source links. |
| TF87 | D | **Search across sessions.** Query retained command blocks by project, host, job, exit status or text without scanning remote files on each keystroke. |
| TF88 | D | **Redacted reproduction bundle.** Export selected commands, environment summaries and declared artifacts with exclusions. Do not claim this reproduces every shell side effect. |
| TF89 | D | **Opt-in recording and replay.** Record bounded terminal output with resize/time events; replay never re-executes recorded commands. |
| TF90 | D | **Durable tmux sessions.** Offer explicit create/attach/detach for owned sessions so work can survive Tower exit. Show persistence capability separately from normal PTY sessions. |
| TF91 | D | **Session health view.** Show PTY state, foreground process group, parser modes, queue lengths, memory and unsupported protocol counters without logging secret input. |
| TF92 | D | **Compatibility profiles.** Store tested application and outer-terminal combinations; give a direct handoff option for unsupported behavior. |
| TF93 | D | **Multi-node session collections.** Organize explicit node/rank sessions under one allocation, with connection and concurrency limits. |
| TF94 | D | **Armed broadcast mode.** If added, require deliberate arming, display every destination, limit recipients and disable it on focus/context changes. Default to one recipient. |
| TF95 | D | **Reviewed maintenance sequences.** Prepare a bounded series of known diagnostics with per-step target, cancellation and result collection. Arbitrary typed shell commands remain ordinary shell commands. |
| TF96 | D | **Scientific acceptance handoff.** Register declared results after a command and offer the existing acceptance checks without inferring the user's scientific thresholds. |
| TF97 | D | **Container-aware context.** Explicitly distinguish host, container and allocation paths; support a tested container exec adapter without assuming shared mount layouts. |
| TF98 | D | **Environment-difference views.** Compare explicitly captured module/package summaries across runs; exclude secrets and distinguish unavailable metadata. |
| TF99 | D | **Read-only session sharing.** Consider explicit transport/authentication and output-redaction controls before exposing another user's view; remote write access is a separate feature. |
| TF100 | D | **Capability self-test.** Run bounded local shell, resize, input and rendering checks; report measured support instead of claiming every terminal application works. |

## Limits that the implementation must state

PTY output is an ordered byte stream. It normally merges stdout and stderr and does not expose shell ASTs, command provenance or original file lines.
Command blocks require optional shell integration; a prompt regex cannot reliably establish them.
A program can print false shell-integration markers. Such metadata cannot authorize scheduler actions.

Bounded memory, an arbitrarily fast producer and unlimited lossless retained output cannot all be guaranteed.
Preserve the parser stream, evict old parsed scrollback with a visible boundary, and apply backpressure when required.
Backpressure can slow a writing program. Optional recording has its own disk budget and failure policy.

A normal PTY session can continue while the user changes Tower pages.
It does not guarantee survival after Tower crashes or its SSH connection ends.
Durable remote work requires an explicit persistence backend or a scheduler-managed job.
An `srun` or `sattach` connection also has Slurm-specific exit and signal behavior; test and describe each mode separately.

Terminal text can contain credentials and private output.
Do not record input or transcripts by default.
User shell-history behavior remains controlled by the shell.
Redaction cannot promise to discover every secret, so export must permit explicit exclusion and preview.

The feature remains character-rendered. It does not require a web server, browser, GUI toolkit or image protocol.
No emulator choice establishes universal application compatibility.

## Repository integration

| Existing area | Planned change |
| --- | --- |
| `workspace_layout.py` and page-specific layout paths | Add the shared secondary-pane host, geometry and page preference; preserve browser and Details layout behavior |
| `job_panels.py` | Remain the existing Details implementation under the new host |
| `screen.py` input decoder and `_apply_input` | Preserve complete framed terminal events and route them to the explicit owner before global Tower shortcuts |
| `pointer_routing.py` and `interaction.py` | Register terminal focus, selection and application-mouse capture; preserve stale-release guards |
| `_FrameCache` and `_DifferentialPainter` | Apply terminal damage without recomputing graphs or job documents |
| `controller.py` | Add explicit terminal actions and focus routing; protect Ctrl-C, paste and normal child keys from Tower command interpretation |
| `cli.Session.close()` | Shut down the owned broker and collect session results without abandoned descriptors, children or waits |
| `worker_scheduler.py` | Keep scheduler worker mode independent of terminal lifetime; do not submit permanent read loops |
| `ops_cluster.py` / `ops_ui.py` | Reuse reviewed allocation and exact-attempt validation through a terminal-session launch result |
| `text_selection.py` and clipboard adapters | Give terminal selection its own source identity and copy mode; preserve the 6.2.1 selection-only guarantees |
| New terminal backend modules | Own transport, emulator adapter, protocol policy, immutable publication and bounded session state |

## Delivery and acceptance

Implement the foundation before the advanced catalog.
Each stage must have a working end-to-end path and measured limits.

1. **Feasibility gate.** Compare candidate emulator engines with a real PTY and Tower-sized viewport. Test shell job control, Vim, less, output floods, resize, Unicode and installation on the supported Python/Linux targets. Choose the engine from that evidence.
2. **Stage A.** Deliver one persistent local session, Details/Terminal switching, explicit focus, correct resize, exit cleanup, input isolation and bounded output handling.
3. **Stage B.** Complete scrollback, paste, selection, application mouse mode, Unicode/color behavior, named sessions and compatibility diagnostics.
4. **Stage C.** Add explicit SSH/allocation sessions and the project/reporting bridge using the existing review and identity mechanisms.
5. **Stage D.** Add advanced features independently. Recording, sharing, persistence and broadcasting stay optional; none is a prerequisite for an ordinary shell.

Use the following release gates:

| Gate | Required validation |
| --- | --- |
| Input isolation | `q`, `:`, `g`, `u`, `y`, Ctrl-C, Escape, arrows and function keys never activate Tower while the child owns input |
| Focus races | Change job, page, session, layout and worker mode during queued writes, fragmented UTF-8, paste and mouse drag; bytes retain their original recipient |
| Protocol framing | Split each representative escape/paste/mouse sequence at every byte boundary; test malformed, oversized and unterminated control strings |
| PTY semantics | Foreground process groups, Ctrl-Z/fg, echo changes, exec failure, EOF/EIO, stopped jobs, exit during writes and descriptor/child leak tests |
| Rendering | Normal/alternate screens, wrap margins, Unicode boundaries, cursor, color fallback, resize storms and stale screen generations |
| Selection | Single/multiple lines, output during selection, history eviction, hidden sessions, neighboring panes and exact selection-only copy |
| Performance | Continuous output with live metric graphs and 50/250/1000 Hz synthetic pointer traffic; compare input latency, CPU, memory and backlog with the same baseline workload |
| Worker interaction | Single/multi collection switches never wait on a terminal's lifetime, lose output or leave an abandoned broker |
| Remote/allocation | Authentication, disconnect, uncertain command delivery, no replay, exact array/step identity, job completion/requeue and allocation expiry |
| Applications | Document tested Bash/Zsh/Fish, Vim/Neovim, less, top/htop, Python/IPython, SSH and tmux behavior; distinguish absent optional tools from passing tests |
| Outer terminals | Test a regular terminal, Termius, tmux/screen and SSH combinations; account for protocols that those layers cannot transmit |
| Lifecycle | Repeated open/hide/show/close, Tower shutdown with running sessions, explicit detach and broker failure recovery |

Set numerical latency and memory budgets from the feasibility measurements.
Publish percentile latency and workload details rather than a blanket claim of zero lag.
Keep existing copy, group, graph and raw-mouse regression suites in every stage.

## Reference material checked for this design

- [Python PTY documentation](https://docs.python.org/3.12/library/pty.html): controlling-terminal behavior and the limits of `pty.spawn`.
- [Python termios documentation](https://docs.python.org/3.12/library/termios.html): terminal modes and window sizing.
- [Python process documentation](https://docs.python.org/3.12/library/os.html#os.fork): constraints on forking after threads exist.
- [pyte documentation and source](https://github.com/selectel/pyte): an in-memory terminal emulator candidate, with protocol coverage to verify.
- [libvterm API declaration](https://github.com/neovim/libvterm/blob/master/include/vterm.h): screen-damage, cursor, keyboard, mouse and selection interfaces; this older fork is a reference, not a selected dependency.
- [Neovim libvterm repository notice](https://github.com/neovim/libvterm): current implementation location and maintenance warning.
- Tower's current `screen._operation_terminal`, `ops_cluster._allocation_shell`, input decoder, pane geometry and collection-worker code.
