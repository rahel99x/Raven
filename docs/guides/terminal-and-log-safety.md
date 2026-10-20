# Terminal and log handling

Raven displays scheduler fields, logs, notifications and child output as untrusted
text. Nonterminal panes never execute their escape sequences: complete terminal
commands are removed, remaining C0/C1 controls become visible text, and Unicode
formatting controls are shown as `<U+XXXX>` labels. The embedded terminal
interprets its own bounded VT protocol; a child clipboard request requires an
explicit user action.

## Copy and export

Interactive copies retain the accepted selection and run on the shared background
worker. Raven reports that the request was queued, then reports its destination
or delivery error. A slow desktop clipboard process cannot block keyboard input.
Clipboard tools have a shared two-second delivery budget. At most eight accepted
ordinary copies wait at once; a full queue produces an actionable message.

Exports use `$XDG_DATA_HOME/raven/exports`, or `~/.local/share/raven/exports` when
`XDG_DATA_HOME` is unset. Set an absolute `RAVEN_EXPORT_DIR` to choose a durable
location. This also applies to `--no-state`. The directory must be owned by the
current user and must not be writable by others. Export files use mode `0600`;
ordinary screen copies atomically replace the private `clipboard.txt` cache.
Logs discovered in scheduler directories must be regular files owned by the
current user. Symlinks at individual log paths are refused. A deliberately chosen
project or export root may itself be a symlink; Raven resolves that root once and
opens internal paths without following newly substituted symlinks.

The redactor masks common credential assignments, authorization headers, password
flags, URL credentials and well-known token forms. Review evidence before sharing:
pattern-based redaction cannot establish that an arbitrary document has no secrets.

## Search coverage

Full-source search has byte, time and result limits. For a file larger than the
byte budget, Raven searches its newest bounded window and lists the unsearched
absolute byte ranges. A byte offset from that window is exact; its global line
number is unknown. Regex searches exclude fragmented or overlong lines and report
that limitation. Literal searches can cross bounded fragments. Retained-log full
indexes run in the background; small literal append/trim updates reuse their index.

## Notification hooks

Scheduler transitions from one publication are delivered in one hook invocation.
Matching alert commands are similarly grouped per evaluation. Hooks receive a JSON
document on standard input:

```json
{"schema":"raven.hook-events/v1","events":[{"kind":"started","job":"73","name":"training","text":"Job started"}]}
```

Existing `RAVEN_EVENT`, `RAVEN_JOBID`, `RAVEN_JOBNAME` and `RAVEN_TEXT` environment
variables describe the first event. `RAVEN_EVENT=batch` identifies mixed transition
kinds. Read `events` to process every member of a batch. For example:

```bash
#!/usr/bin/env bash
python3 -c 'import json,sys; [print(e.get("job"), e.get("text")) for e in json.load(sys.stdin)["events"]]'
```

The notification lane retains at most 32 queued invocations. A newer invocation
replaces the oldest queued one when full; Raven shows the cumulative skipped
count. Source collection keeps its own capacity. Timed-out hooks receive a process
group termination followed by forced cleanup, including descendants that outlive
their shell. Quiet hours use the machine's local timezone unless a zone is chosen.

Closing an embedded terminal sends a terminal hangup to the shell and foreground
job. Explicit `nohup` or disowned background jobs retain normal shell behavior.
Returning from a pager or allocation shell restores input ownership and resize
handling before Raven accepts further terminal input.

## Runtime boundaries

`raven.terminal` contains the VT emulator, bounded IPC client, PTY broker and
recording format. These modules do not import the controller, views, curses or
terminal UI. The UI supplies focus and permission decisions; the broker owns
child process and PTY lifecycle. Existing `raven.terminal_emulator`,
`raven.terminal_service`, `raven.terminal_broker` and `raven.terminal_recording`
imports remain compatibility aliases to the same implementation modules.
