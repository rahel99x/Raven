# Embedded terminal

[README](../../README.md) · [Controls](../CONTROLS.md) ·
[Terminal workflows](terminal-workflows.md) · [Delivery matrix](../reference/terminal-fusion.md)

Run local shells and explicit remote or allocation sessions in Raven's terminal pane.
The pane shares the application layout while retaining each session's host, directory, and process identity.
This guide covers installation, focus, copying, shell reporting, evidence export, and replay.

## Install and open

Use Python 3.10 or later on Linux. From the checkout, run:

```bash
python3 scripts/setup.py --mode demo --terminal
./scripts/tower terminal selftest
```

The setup installs `pyte` and `wcwidth` in `.venv`. It validates simulated data
and preserves your existing alias and connection profile. Start Raven with your
normal command. Select **Terminal** beside **Details** in Jobs, History,
Analytics, Dependencies, Logs or Research. The first explicit opening starts a
local shell. Drawing a page or restoring a saved layout does not start one.
On very short screens, some pages omit the pane buttons. Use **View → Open
embedded terminal** or `:terminal show` instead.

Raven's source remains MIT licensed. The separately installed pyte dependency
uses LGPLv3; wcwidth uses MIT. Their distributions retain their own license files.

To install from this checkout with another interpreter, use:

```bash
python -m pip install '.[terminal]'
```

For an offline host, transfer the Raven wheel and compatible `pyte` and
`wcwidth` wheels, then run
`python -m pip install --no-index --find-links DIR 'slurm-tower[terminal]'`.
The core application still works without these packages.

The shell runs on the machine that runs Raven. An SSH Slurm data connection does
not change this destination. The pane displays the session label, destination
and process state. A running SSH process does not prove successful authentication.

## Focus and sessions

Click the terminal body to assign keyboard input. Hover only highlights controls.
While the terminal owns input, ordinary keys and Ctrl-C go to its child program.
Press **Ctrl-]**, then **t**, to return input to Raven. Press **Ctrl-]** twice to
send one literal prefix. This release uses a fixed prefix.

Select **Details** to hide the terminal. Its directory, environment and command
remain intact while you use other pages. Selecting another job does not move the
shell, change its host or send it input. Each page remembers its pane choice;
the selected named session is shared across pages.

Use the arrows in the terminal toolbar to switch sessions, **+** to start one,
and **Tools → Sessions** to choose one by name. An unread marker identifies a
session with new output. Tools menus support mouse, arrows, Home/End and Enter.
Use the normal pane divider and **Maximize / restore** to change the viewport.
A resize that hides the terminal revokes its keyboard focus.

| Palette command | Purpose |
| --- | --- |
| `:terminal show`, `:terminal details` | Choose Terminal or Details |
| `:terminal focus`, `:terminal blur` | Assign or release terminal input |
| `:terminal new NAME` | Start a local session with an optional label |
| `:terminal sessions`, `:terminal session ID` | Choose a session |
| `:terminal next`, `:terminal previous` | Cycle sessions |
| `:terminal rename NAME` | Rename the selected session |
| `:terminal menu` | Open Tools |
| `:terminal maximize` | Maximize or restore the workspace pane |
| `:terminal close` | Close the owned PTY and terminate its session |
| `:terminal restart` | Restart an ordinary owned session; allocation sessions require a fresh review |
| `:terminal cancel-input` | Cancel remaining queued input and report that already delivered bytes cannot be recalled |
| `:terminal health` | Inspect current session state and limits |

Closing a normal terminal or exiting Raven ends its owned process group.
Hiding the pane does not. Durable work requires an explicit tmux session or a
scheduler-managed job. See [terminal workflows](terminal-workflows.md) for
tmux detach, SSH collections, containers, directory actions and allocation shells.

## Copy, paste and scroll

Select **Copy** to enter copy mode. Drag across lines, or press **v**, move with
arrows, and press **y**. Raven copies only the pinned selection. New output cannot
replace selected text. Soft wraps join without inserting false newlines.
The existing Copy/Yank toolbar destination also applies here.

In copy mode, **/** opens search and **n** finds the next retained match.
Use the wheel, scroll rail, Home/End, Page Up/Page Down and top/bottom controls.
Press **Esc** to leave copy mode. If **Follow** is off, enable it to return to
current output.
Outside copy mode, a child that requests mouse reporting receives pane-relative
mouse events; **y** is ordinary terminal input.

History defaults to 2,000 lines. The engine also enforces a byte budget. The
retained boundary appears in **Tools → Health**; search cannot recover evicted output. Selection
copies operate on frozen text. A full history read uses bounded, coherent pages.

Multiline paste opens a preview by default. Inspect its contents and destination
before Insert. Existing newlines can execute commands in an unbracketed shell;
the preview does not remove or rewrite them. Prepared diagnostics and recipes
do not add Enter. Input is bounded, queued and ordered. Oversized paste is
rejected before delivery. Cancelling queued input cannot retract bytes already
sent or undo a command.

Child OSC 52 clipboard writes and OSC 8 links are available through **Tools →
Child clipboard** and **Links**. Inspect the exact text or destination, then use
Copy. Child requests never copy automatically. Clipboard reads are denied.
Links do not launch a browser or execute a command automatically.

## Configure the shell

The following keys belong to the compatibility `tower` configuration.
Managed Raven profiles apply terminal budgets through `resources.light`.
TOML requires Python 3.11 or later; use JSON on Python 3.10.

```toml
[terminal]
shell = ""                    # use $SHELL, or /bin/bash when $SHELL is unset
login = false                 # interactive; enable the shell's login profile explicitly
history_lines = 2000
max_sessions = 8
paste_limit_bytes = 1048576
paste_preview = true
```

The equivalent JSON object is:

```json
{
  "terminal": {
    "shell": "",
    "login": false,
    "history_lines": 2000,
    "max_sessions": 8,
    "paste_limit_bytes": 1048576,
    "paste_preview": true
  }
}
```

Use an executable path for `shell`, not a command with arguments. Sessions inherit
the normal environment and shell startup policy. An invalid configured path or
`$SHELL` produces a launch error; it does not silently select another shell.
Raven does not modify dotfiles or source a batch launcher.
The hard session limit is eight and the maximum
paste payload is 1 MiB. Configuration cannot increase those hard limits.
The emulator viewport is bounded at 160 rows by 400 columns per session.
Managed launches also apply Raven's light-mode resource budget.
Its default terminal limit is two sessions, configured by `resources.light.max_terminal_sessions`.
Use `ravenl config explain` to inspect the effective budget.

## Connect shell activity to research

Hooks are optional. A plain terminal needs none. In an owned local shell, source
one of these generated scripts deliberately:

```bash
# Bash
eval "$(tower terminal hook --shell bash)"

# Zsh
eval "$(tower terminal hook --shell zsh)"
```

For Fish, use `tower terminal hook --shell fish | source`. The Tools hook action
prepares the appropriate command for a matching local shell. Generated hooks
call the current interpreter and reporting helper by absolute path, so a Bash
alias or a missing `tower` entry on `PATH` does not break reporting. Hooks publish prompt boundaries,
cwd, timing and exit status through a private same-user socket. They do not
dump the environment. Command-text capture is off by default. Zsh and Fish can
opt in with `TOWER_CAPTURE_COMMANDS=1`; the Bash integration records command
boundaries without capturing command text.

Report exact observations from the shell or launcher:

```bash
tower terminal report submission --job 1234
tower terminal report project --project /project/example
tower terminal report artifact --artifact /project/example/runs/1234/output.log
tower terminal report environment --label before
```

Use the exact `sbatch --parsable` result for submission reporting. These reports
add context to the existing [project standard](../PROJECT_STANDARD.md); they do
not replace manifests, create scheduler authority or silently navigate away.
Registered project paths and cached exact job identities are checked before
handoff. SSH and container sessions do not receive an invented remote bridge.
Hook setup is for an owned local shell. It does not inject local helper paths
into an SSH, allocation, container or persistent tmux session.

## Inspect and export evidence

All actions below are available in Tools, with prompts when arguments are needed.
The palette provides the same controls. Open `:terminal evidence-help` for the
in-application reference.

| Command | Result |
| --- | --- |
| `:terminal blocks`, `:terminal block ID` | List command blocks or inspect one |
| `:terminal block-copy ID` | Copy one selected command block |
| `:terminal evidence-search TEXT job=ID status=N` | Search retained blocks; filters are optional |
| `:terminal bookmark ID LABEL`, `:terminal pins` | Pin and inspect bounded evidence, including available cached metric samples |
| `:terminal timeline JOBID` | Inspect observed command times for a job |
| `:terminal failures`, `:terminal dismiss ID` | Inspect or dismiss nonzero command results |
| `:terminal compare LEFT RIGHT trailing-space` | Compare blocks; omit the last argument for exact comparison |
| `:terminal evidence-preview ID... exclude=TEXT` | Review the complete export, with optional explicit exclusions |
| `:terminal evidence-export NEW_FILE` | Export the reviewed snapshot to a new private file |
| `:terminal environment capture NAME` | Capture an allowlisted summary of Raven's process environment |
| `:terminal environment diff BEFORE AFTER` | Compare named captures |
| `:terminal handoffs` | Inspect declared project and artifact observations |
| `:terminal handoff project SESSION` | Open a validated declared project |
| `:terminal handoff artifact SESSION INDEX` | Open a validated declared artifact |
| `:terminal job-link ID` | Navigate to a validated job explicitly |
| `:terminal acceptance POLICY RESULTS` | Populate the existing scientific acceptance review |
| `:terminal evidence-result` | Reopen the last completed file-operation result |

Each completed block captures the latest rendered viewport available when its
completion report arrives. This can include an older command or prompt and omit
output that scrolled away. It is not accumulated output for the command. PTY
output merges stdout and stderr; the hook and output channels are independent.
Reports preserve unknown clock skew and missing metadata. A nonzero status alone
does not establish a scientific failure cause.

The palette's environment capture reads Raven's process environment. To capture
changes in a child shell, run `tower terminal report environment --label NAME`
from that shell. Both routes use an allowlist. Module/package versions are
unavailable unless an explicit report supplies them.

Evidence storage is bounded. Filesystem work and comparisons run outside the UI
thread. Exports create new private files and refuse overwrite. Automatic
redaction is limited; the preview and explicit exclusions define what you share.

## Record and replay

Recording is off by default. Use:

```text
:terminal record on /private/new-recording.jsonl
:terminal record off
:terminal replay /private/new-recording.jsonl
```

The recorder stores output, resize events and relative times. It does not store
the input stream. Echoed commands and program output can still contain secrets.
The limits are 16 MiB, 100,000 events and 24 hours; reaching a limit or a write
failure reports incomplete coverage. It does not silently claim a complete run.

Replay opens a separate read-only player in the terminal pane. It interprets
recorded output as terminal data and never executes recorded commands.

| Player key | Action |
| --- | --- |
| Space | Pause or resume |
| h / l | Seek backward or forward five seconds |
| g / G | Seek to start or end |
| - / + | Decrease or increase speed, from 0.125× to 16× |
| j / k | Move through a tall recorded screen |
| q | Close the player |

`tower terminal inspect FILE` reports recording metadata. `tower terminal replay
FILE` uses the player on a TTY and an inert text summary when redirected.
Seeking rebuilds terminal state in bounded slices; large recordings can take
time to catch up. Playback processing is separate from Raven's UI.

`:terminal share NEW_FILE` writes a private, redacted, read-only screen snapshot.
`:terminal share-read FILE` opens it. Snapshot sharing supports the same user.
Cross-user live streaming and remote control are outside the supported scope.

## Diagnose compatibility and performance

Run `tower terminal selftest --json`, or choose **Capability self-test** in Tools.
The finite local probe checks the controlling TTY, foreground process group,
input, resize, alternate-screen restoration, final output and exit. It does not
contact Slurm. Use `tower terminal selftest --help` for profile export and comparison.

`tower terminal selftest --save-profile NEW_FILE` writes a private compatibility
profile. `tower terminal selftest --profile FILE` compares that profile with a
fresh probe and current runtime metadata. Matching metadata does not certify an
application or physical terminal. The profile distinguishes available tools
from tools actually tested.

The engine supports normal and alternate screens, tested xterm key and mouse
modes, ANSI/256/RGB color, wide characters and bounded combining marks. Explicit
child colors are retained; default colors and pane chrome follow Raven's theme.
ASCII fallback applies to Raven chrome, not arbitrary child output.

Known limits: no image protocols, no complex emoji shaping, no extended keyboard
negotiation and no normal-buffer text reflow on resize. A requested bar cursor
uses a cell approximation. Unsupported control strings are contained. For an
allocation application outside this coverage, use `:terminal job JOBID
display=fullscreen` and review the original terminal handoff.

The broker processes PTYs independently of the sampler pool. It rotates ready
sessions, parses bounded slices, publishes visible screens at up to 30 Hz and
hidden screens at up to 1 Hz. Raven paints changed rows without rebuilding jobs
or graphs for each output update. Backpressure preserves protocol order and can
slow a high-volume producer. Terminal throughput is not unlimited.

The test suite includes real Bash job control, a real installed Vim edit/save
cycle, fragmented input, stale focus, resizing, floods, recording and replay.
Local adapter tests do not certify every SSH configuration, cluster policy,
container runtime or physical terminal. Validate those combinations on the host
where you use them; the [delivery matrix](../reference/terminal-fusion.md) records
the supported scope.
