# Research operations and application diagnostics

[README](../../README.md) · [Controls](../CONTROLS.md) · [Rollout status](../ROADMAP.md) · [Validation](operations-validation.md)

Raven provides forms, bounded background inspections, and structured reports
for research and cluster operations. Actions that change a job, start a service,
submit work, or write results require a separate review and confirmation.
This guide explains the shared controls and the twenty-six research operations
introduced in the 4.15 release. The catalog also includes the `queue-order` and
`rerun-job` job-management operations.

Read the [generated catalog](../reference/operations.md) for the current
operations and field definitions. Use
`:workbench run|investigate|compare|results|connect|all` to open a task workspace.
The [implementation ledger](../FLAGSHIP_IMPLEMENTATION.md) records the supported
scope of tracing, scientific investigation, site integration, and cooperative
runtime control. Earlier measurement and submission tools are documented in the
[phase one guide](phase-one.md).

## Open and use an operation

1. Open **File → Research and cluster operations**, or enter `:ops catalog`.
2. Select an operation with the mouse, or use Up/Down and Enter.
3. Select a field. Type a value and press Enter, or cycle its fixed choices.
4. Select **Inspect / prepare**. The shared background worker collects evidence.
5. Read the result and its warnings. Use **Edit inputs** or **Refresh** as needed.
6. If the result offers **Review action**, select it and inspect the exact plan.
7. Select **Confirm apply** only when the displayed scope and action are correct.

Inspection never silently confirms a plan. Some explicit inspections create
short, read-only Slurm steps to measure allocation placement or compute-host
runtime. The operation guide identifies those probes. A generated environment
bundle is written only after review; Raven does not execute its recreation
script.

Use `:ops FEATURE` to open a form directly. Use
`:ops run FEATURE field=value` to inspect explicit inputs. Quote values that
contain spaces. Examples:

```text
:ops storage
:ops run storage path=/scratch/project min_free_gib=100
:ops run pending-edit job_id=1234 field=TimeLimit value=02:00:00
:ops run workflow-engine engine=nextflow source=/project/run/trace.tsv
:ops run energy path=examples/scale-energy.json
```

Direct `run` commands inspect or prepare; they do not bypass action review.
The `ops apply` command also requires a displayed current review. An old
release's generic `--yes` flag is not a replacement for that review.

### Keyboard, mouse, and report controls

| Control | Result |
| --- | --- |
| Up / Down, Page Up / Page Down, Home / End | Select and scroll catalog entries, fields, or report lines |
| Enter / Space | Open the selected operation, edit its field, cycle a choice, or activate the focused button |
| Tab / Shift-Tab | Switch between content and action-button focus |
| Left / Right | Move action-button focus |
| Click a field or button | Activate that visible control |
| Wheel or scrollbar | Scroll the visible operation pane |
| `v`, then movement keys | Select a range of report or review lines |
| Shift-click | Extend a line selection |
| `y` | Copy the clicked or keyboard-selected line, a selected range, or the complete structured result when no lines are selected |
| `Y`, **Copy report**, or `:ops copy` | Copy the complete structured result |
| Right-click | Clear the report line selection |
| `r`, **Refresh**, or `:ops inspect` | Repeat inspection of the current inputs |
| **Edit inputs**, or `:ops form` | Return to the form without applying its plan |
| **All tools**, or `:ops catalog` | Return to the operation catalog |
| **Cancel inspection**, or `:ops cancel` | Request cancellation at the next bounded inspection checkpoint |
| Esc / `q`, **Close**, or `:ops close` | Return to the prior view |

While editing text, Left/Right, Home/End, Backspace, and Delete edit that field.
The highlighted caret stays visible as you move through a long path. Paste
inserts text at the caret. Use one line with no tabs or control characters;
each paste can contain at most 4096 characters, and a field can contain at most
8192 characters. An invalid or oversized paste leaves the field unchanged.
Pasting never runs an operation. Enter accepts the edit; Esc discards it.
Field choices cycle without opening an editor.

The report display has an 8192-line limit. A notice identifies truncation.
**Copy report** without a selection copies the complete structured
evidence that the adapter retained, including its own source-limit warnings.
Copying cannot recover records that an adapter omitted at its collection limit.

### Controls in the relevant views

The global catalog always includes every operation. Related views also have
buttons that open the same forms. Research buttons also appear in Research
views embedded in Jobs or History Details.

| View | Related operations |
| --- | --- |
| Sources | Slurm doctor, cluster workspace, storage readiness |
| Jobs Main group order handle | Review own pending queue order |
| Recents or History, one cancelled job's right-click menu | Inspect and review one retained submission's rerun |
| Analytics | Bottlenecks, statistical comparison, energy |
| Research Experiment | Acceptance, statistics, energy |
| Research Arrays | Existing array concurrency, search, packing |
| Research Evidence | Bottlenecks, placement, incidents |
| Research Artifacts | Storage, staging, verified reuse |
| Research Passport | Environment comparison and recreation bundle |
| Research Submit | Pending-job edit, retained batch script, heterogeneous allocation |
| Research Workflow | Dependency repair, native workflow observer, checkpoint restart, persistent monitor, Dask |
| Research Forecast | Reservations and licenses |
| Research Blockers | Slurm doctor, pending-job edit, licenses |
| Research Scaling | Bottlenecks, placement, Dask |

A job-ID field starts with the selected job when available. Check the field
before inspection. A highlighted row is not sufficient evidence for changing
a job; the adapter also rechecks the scheduler identity and required state.
Use `:rerun [JOBID]` or `:ops run rerun-job job_id=1234` to open the cancelled-job
procedure directly. It requires separate **Review action** and **Confirm apply**
steps. Read [Rerun one cancelled job](operations-cluster.md#rerun-one-cancelled-job).

<a id="all-twenty-six-operation-keys"></a>

## The twenty-six research operation keys

The scope below describes the implemented adapters. A supplied manifest or
export is required where shown. An operation name does not imply automatic
support for every scheduler, application, profiler, or workflow format.

| Proposal | Key | Available operation and boundary | Guide |
| --- | --- | --- | --- |
| P01 | `storage` | Free bytes/inodes and explicit user, group, or project quota evidence; no file walk | [Cluster](operations-cluster.md#storage-space-inodes-and-quotas--p01) |
| P02 | `pending-edit` | Review one supported field on an unchanged pending job; re-read its result | [Cluster](operations-cluster.md#edit-one-pending-job-field--p02) |
| P03 | `array-throttle` | Change a real existing array's concurrency; zero means unlimited | [Cluster](operations-cluster.md#change-live-array-concurrency--p03) |
| P04 | `reservations` | Read available reservation windows, resources, and access restrictions | [Cluster](operations-cluster.md#reservation-timeline--p04) |
| P05 | `licenses` | Read Slurm's license inventory; no claim about unreported vendor-server use | [Cluster](operations-cluster.md#license-inventory--p05) |
| A05 | `slurm-doctor` | Read controller, accounting, partition, and selected configuration evidence | [Cluster](operations-cluster.md#slurm-service-doctor--a05) |
| A20 | `batch-script` | Retrieve Slurm's retained script; optionally create a new local copy | [Cluster](operations-cluster.md#retrieve-a-retained-batch-script--a20) |
| A19 | `allocation-shell` | Review a shell step or exact numeric-step attachment, then return to Raven | [Cluster](operations-cluster.md#allocation-shell-and-step-attachment--a19) |
| A01 | `environment` | Compare pinned runtime evidence; write a hashed Python/optional Apptainer recreation bundle | [Science](operations-science.md) |
| A02 | `placement` | Inspect existing allocation-process CPU, NUMA, and GPU visibility; no affinity changes | [Science](operations-science.md) |
| A04 | `acceptance` | Check all required measurements against project-defined tolerances | [Science](operations-science.md) |
| A07 | `statistics` | Independent-unit or exact-pair comparisons with Student t uncertainty | [Science](operations-science.md) |
| A08 | `reuse` | Verify identity, dependencies, accepted science, and output hashes before copying to a new directory | [Science](operations-science.md) |
| A15 | `dependency-repair` | Review exact pending dependency changes and report partial outcomes | [Science](operations-science.md) |
| S01 | `checkpoint` | Verify a declared complete checkpoint and compatible recipe before an exact restart | [Campaigns](operations-campaigns.md#checkpoint-recovery) |
| A06 | `search` | Bounded discrete parameter search with persistent trial identity and explicit launches | [Campaigns](operations-campaigns.md#adaptive-parameter-search) |
| A09 | `packing` | Submit one resource-bounded allocation that runs declared tasks in exclusive steps | [Campaigns](operations-campaigns.md#short-task-packing) |
| A10 | `dask` | Start, inspect, scale, or stop one owned optional dask-jobqueue controller | [Campaigns](operations-campaigns.md#elastic-dask-pool) |
| A12 | `heterogeneous` | Compose distinct Slurm components and their coupled `srun` launch | [Campaigns](operations-campaigns.md#heterogeneous-allocation) |
| S03 | `supervisor` | Reconnectable Linux read-only queue/accounting monitor; no automatic recovery | [Services](operations-services.md#persistent-monitor--s03) |
| S04 | `staging` | Stream declared input/output files between visible mounts with checksum verification and receipts | [Services](operations-services.md#verified-staging-and-return--s04) |
| A11 | `workflow-engine` | Observe Nextflow TSV or Snakemake DAG/runtime evidence; native engine owns execution | [Services](operations-services.md#native-workflow-observer--a11) |
| S02 | `bottlenecks` | Inspect bounded rank/phase JSON or Darshan POSIX text; no automatic profiler attachment | [Scale](operations-scale.md#distributed-bottleneck-explorer) |
| S05 | `clusters` | Concurrent, isolated multi-cluster snapshot with exact attempt identity | [Scale](operations-scale.md#concurrent-cluster-workspace) |
| A17 | `incidents` | Correlate exact job nodes and time with permitted event evidence | [Scale](operations-scale.md#historical-incident-correlation) |
| A18 | `energy` | Account for all declared attempts per accepted unit, with explicit attribution and coverage | [Scale](operations-scale.md#energy-per-useful-result) |

## Additional operation: own pending queue order

In **Jobs → Main**, drag a group's `↕` handle (ASCII: `:`) onto another group.
An upward drop requests a position before that group. A downward drop requests
a position after it. The drag moves the group header preview; expanded members
are dimmed. Releasing the mouse prepares a report. It does not change Slurm.
Use **Review action**, then **Confirm apply** to request the displayed order.

The same operation is available through `:ops queue-order`. Its fields are:

| Field | Required value |
| --- | --- |
| `source_jobs` | Comma-separated exact IDs for the jobs to move; for example, `120,121` |
| `target_jobs` | Comma-separated exact IDs for the destination group; for example, `100,101` |
| `placement` | `before` (default) or `after` |

```text
:ops run queue-order source_jobs=120,121 target_jobs=100,101 placement=before
```

Raven reads both sets of jobs, then discovers the current pending queue for
their owner, partition, account, and QOS. The report lists every affected job's
old and proposed position, including other jobs in that scheduling scope.
Members of the moved group retain their relative priority order. Unrelated
jobs in the same scope retain their relative order. Other users and other
scheduling scopes are excluded.

The operation uses the native `scontrol top` command with one ordered,
comma-separated ID list. It requires the site's
`SchedulerParameters=enable_user_top` setting. Raven does not change that
setting or substitute direct priority edits. Slurm adjusts nice values to
express the preference. Resources, dependencies, reservations, and site policy
still determine eligibility and start order; **the requested order is not a
start-time guarantee**. Dependencies remain unchanged.

These limits apply:

- Each set must contain at least one pending job. Every pending source or
  destination job must have a priority greater than one. A held or priority-one
  target blocks the operation and keeps its hold state. Running jobs are not moved.
- Pending source and destination members must have the same owner, single
  partition, account, and QOS. Cross-scope moves are rejected.
- A review accepts at most 512 exact jobs in the affected scheduling scope.
  Scheduler output also has a 1 MiB read limit. Oversized evidence is rejected,
  not silently truncated. Jobs in other scheduling scopes do not consume the
  pending-queue record limit.
- Exact numeric array task IDs are supported. Compressed array ranges and
  heterogeneous job IDs are rejected. No array is silently expanded into
  additional action targets.
- A group drag omits archived or departed members that are no longer in the
  current queue and reports their count. A missing current job or a changed
  submission identity requires a new inspection.
- Apply rechecks the site setting, connection, owners, submissions, states,
  priorities, nice values, dependencies, and affected peer membership. A
  changed review is rejected. A job can still start or arrive between that
  final check and Slurm's controller action; the controller owns that decision.

The operation uses the shared background worker and the common one-use review
and receipt rules below. Pointer motion makes no scheduler calls. A timeout or
lost response requires a fresh inspection; Raven does not retry an uncertain
queue mutation automatically.

Upstream reference: [SchedMD `scontrol top` documentation](https://github.com/SchedMD/slurm/blob/master/doc/man/man1/scontrol.1).

## Review, background work, and recovery rules

An action plan records its inputs, exact connection scope, evidence, digest,
creation time, and relevant job attempt. Reviews expire after five minutes.
Changing the connection, attempt, source, or action requires another inspection.
The interface consumes a plan before starting its action. Repeated clicks
cannot submit the same plan again. Adapter receipts provide additional durable
protection for campaign launches and transfers.

With normal state persistence, the common action layer writes
`operations-receipts/<plan-digest>.intent.json` before execution and a matching
`.result.json` when it collects the outcome. These files use
`raven.operation-receipt/v1`. A durable intent prevents the same plan from
being attempted again after a restart. An error record can describe a partial
or unknown outcome; read the result and inspect current state before preparing
another review. With `--no-state`, this common persistence is unavailable and
Raven reports that limit. Adapter-specific requirements can still refuse an
action that needs a durable state directory.

Scheduler updates are not transactional. A dependency repair or campaign launch
can partially succeed. Read every outcome. A command timeout or lost response
can leave its result unknown; it does not prove that Slurm rejected the action.
Inspect Slurm and the durable receipt before deciding how to continue. Raven
does not blindly repeat uncertain submissions.

Inspection uses the existing research worker lane and keeps I/O outside
rendering and pointer handling. **Single** means one background worker with a
separate responsive UI thread. **Multi** permits the existing worker scheduler
to distribute work. Switching modes drains in-flight work under that scheduler;
it does not abandon an operation result. The cluster workspace bounds its own
source readers and joins them before the parent operation ends.

Closing a read-only inspection requests cancellation. Closing a running action
does not detach its result or erase its receipt. The result is collected without
opening a different tab or replacing the selected job. A standalone monitor or
Dask controller is a separate persistent process; use that operation's Stop
action when it must end. Closing the terminal or switching worker modes does
not stop those services.

Source reads, command output, records, graph edges, and file sizes have adapter
limits. Large collections report omissions or refuse oversized evidence. No
operation recursively scans an undeclared project tree. A missing counter,
unsupported command, absent permission, or stale observation remains distinct
from a measured zero and a successful action.

## Project contracts and optional dependencies

Keep reusable input policies in `.raven/definitions/operations/`. Keep generated
per-attempt evidence in `runs/<run_id>/reports/operations/`, and campaign evidence
in `reports/operations/`. These are recommended locations, not automatic
attachment rules. Enter explicit paths in operation forms. The existing
`run.json`, `metrics.jsonl`, and `logs.json` contracts remain unchanged.

Use the [project standard](../PROJECT_STANDARD.md#research-operation-contracts)
for the file mapping, the [schema index](../schemas/README.md) for machine-readable
contracts, and each group guide for examples and adapter-specific fields.
Publish producer snapshots atomically, retain exact cluster/job/attempt identity,
and provide scientific acceptance criteria rather than equating an exit code
with correct science.

| Capability | Additional requirement |
| --- | --- |
| Core operations and parsing | Standard-library Raven installation and the relevant readable files |
| Slurm commands or mutations | Matching Slurm clients, account permissions, and supported site configuration |
| Lustre quota | Optional `lfs`; generic quota uses the site's `quota` command |
| Allocation/runtime probes | Compute-node Python 3 and permission for bounded `srun` steps |
| Environment recreation | Compatible Python/venv and hash-locked packages; optional Apptainer for a declared container |
| Dask pool | Optional `dask-jobqueue` on the controller, compatible workers, and site network/queue configuration |
| Persistent monitor and Dask identity | Linux `/proc`; site-approved service hosting |
| Native workflow observation | A supported Nextflow trace or Snakemake DAG/runtime export |
| Darshan inspection | An exported `darshan-parser` POSIX text file; Raven does not parse native binary traces |

Use a local Raven session on the target host for local file mutations,
submission composers, and persistent-service controls. SSH-capable inspections
read through their captured connection. A remote path never authorizes a
silent local substitute. Recorded sessions cannot prepare or apply live changes.

Read the [validation record](operations-validation.md). The automated suite
covers parser errors, stale evidence, identity reuse, worker transitions,
pointer routing, interrupted work, and sequential module interactions.
Real site permissions, hardware, filesystem semantics, and optional engine
installations require the target-system checks described in the group guides.

## Activity, diagnostics, completions, and alerts

[README](../../README.md) · [Controls](../CONTROLS.md) · [Feature index](../QUALITY_OF_LIFE.md)

Use these controls to inspect application results and manage notifications.
The procedures change local display or notification preferences. The clipboard
test sends a test copy request to the configured clipboard transport.

<a id="feature-39"></a>

<a id="39-search-activity-and-exported-files"></a>

## Search Activity and exported files

### Find a notice

1. Press `Ctrl-A`, or enter `:activity`.
2. Press `/` to edit the notice filter.
3. Enter the required text.
4. Press Enter to finish the filter.
5. Select a notice with Up or Down.
6. Press Enter to expand its text.
7. Press `y` to copy its path or text.
8. Press Esc to return.

**Expected result:** Activity shows notices that match every filter term.
Plain terms match notice text, paths, job identity, and task labels.
Use `level=`, `job=`, or `task=` for field conditions.
These field conditions use case-insensitive text containment.

```text
:activity filter level=error job=123
:activity level warning
:activity job 12345
:activity task copy
:activity filter
```

Press `f` in Activity to clear the filter.
Press `e` to open the export library.
Press `c` to request cancellation of a supported background task.
Use `:task cancel` for the same cancellation request.
Cancellation takes effect at the next bounded operation checkpoint.

Activity retains at most 256 notices during a session.
Notice persistence is disabled by default.
Use `:activity persistent on` to retain the latest 128 notices across restarts.
Use `:activity persistent off` to disable that preference.
Normal state persistence must also be enabled.
Use `:activity clear` to remove retained notices.

A task shows bytes when it reports numerical progress.
Other background readers show a busy state.
Do not interpret a busy state as a measured completion percentage.

### Find an exported file

1. Enter `:exports`.
2. Press `/` to filter its labels or paths.
3. Select a record with Up or Down.
4. Press Enter to read a bounded preview.
5. Press `y` to copy the exact path.
6. Press Esc to return to the export list.

**Expected result:** Raven opens the selected file through the background reader.
A preview reads at most 64 KiB and 256 lines.
A visible notice identifies content outside that preview.
The original file remains available through the copied path.

The export library stores at most 128 path records.
Normal state persistence retains their metadata across restarts.
The library does not store a second copy of each export.
A missing or replaced file produces a preview error.

| Command | Result |
| --- | --- |
| `:exports [show]` | Open the export library |
| `:exports filter TEXT` | Filter labels, paths, jobs, and tasks |
| `:exports preview ID_OR_INDEX` | Open a bounded file preview |
| `:exports copy ID_OR_INDEX` | Copy the exact path |
| `:exports label ID_OR_INDEX NAME` | Set a readable record label |
| `:exports forget ID_OR_INDEX` | Remove one metadata record |
| `:exports clear` | Remove all metadata records |

Indices refer to the current filtered list and start at one.
The `d` key removes the selected record.
Forgetting or clearing metadata does not delete exported files.
File previews require a regular local file.
A symbolic link is not opened as an export preview.

### Troubleshoot Activity and exports

| Symptom | Action |
| --- | --- |
| No notice matches | Press `f` to clear the Activity filter |
| No export matches | Run `:exports filter` to clear the export filter |
| Preview reports a busy reader | Wait for the current background operation to finish |
| Preview reaches its limit | Copy the path with `y` and inspect the original file |
| Notices disappear after restart | Enable `:activity persistent on` and normal state persistence |
| An export path is unavailable | Check that the original file still exists on this machine |

<a id="feature-40"></a>

<a id="40-inspect-terminal-compatibility"></a>

## Inspect terminal compatibility

### Read capability evidence

1. Enter `:terminaldoctor`.
2. Wait for the background inspection to finish.
3. Use arrows and page keys to read its evidence.
4. Press `t` to open the interactive input test.
5. Press Esc to return.

**Expected result:** Raven reports terminal and path metadata.
The inspection checks output encoding, terminal type, color hints, and clipboard transports.
It also reports relevant SSH and tmux context.
Configuration and state checks inspect path types and access permissions.
The inspection does not create a file or open another SSH connection.

These results are capability evidence.
They do not prove that the local terminal accepted a clipboard request.
They do not automatically verify glyph width, mouse input, or key translation.
Use the interactive test for those observations.
Press `r` in diagnostics to refresh the evidence.

### Test glyphs, colors, keys, and mouse input

1. Enter `:terminaltest`.
2. Compare the block row with its numbered cell row.
3. Inspect the labeled color samples.
4. Press the keys that you want to test.
5. Click or scroll with the mouse.
6. Read the recorded key and mouse events.
7. Press Esc to close the test.

**Expected result:** The test records input without executing page or job actions.
The `q`, `c`, `h`, and other action keys are inert in this test.
Esc is the exit key.
The event list retains the latest 16 observations.

If blocks are misaligned, select a compatible monospace font.
Use a UTF-8 locale for Unicode output.
Use `raven --ascii --no-color` when those capabilities are unavailable.

### Test clipboard transport

1. Close the input test with Esc.
2. Enter `:terminaltest clipboard`.
3. Paste into a suitable local text field.
4. Check for `Raven clipboard test`.
5. Press Esc to close the input test.

The command requests delivery through the configured clipboard transports.
It can also write the normal private clipboard fallback.
The result notice identifies the requested transport or available fallback.
A sent OSC 52 request does not prove terminal acceptance.

For SSH, check the local terminal's clipboard settings.
For tmux, also check `set-clipboard` and OSC 52 forwarding.
Raven does not change those terminal settings automatically.
See the [runbook](../runbook.md#6-troubleshoot-the-specific-failure) for launch failures.

<a id="feature-48"></a>

<a id="48-review-the-completion-inbox"></a>

## Review the completion inbox

1. Enter `:inbox`.
2. Press `f` to show failed outcomes, or `a` to show all retained completions.
3. Select a record with Up or Down.
4. Press Enter to open that job in History.
5. Press `I` to inspect its files and evidence.

To open logs directly, select a record in the inbox and press `l`.
Use `r` to mark the selected record as reviewed without opening it.
Use `A` to mark the current filtered list as reviewed.
Press `/` to filter names, IDs, states, or partitions.
Press `u` to return to unreviewed records.

**Expected result:** Raven preserves the exact accounting job identity.
Successfully opening History or logs marks that record as reviewed.
A failed open leaves the record unreviewed.

| Command | Result |
| --- | --- |
| `:inbox [unread]` | Show unreviewed completions |
| `:inbox all` | Show retained completions |
| `:inbox failed` | Show terminal outcomes other than `COMPLETED` |
| `:inbox filter TEXT` | Filter the current completion view |
| `:inbox ack JOBID` | Review matching records in the current filtered list |
| `:inbox ack all` | Review every record in the current filtered list |

The inbox retains at most 256 confirmed terminal accounting records.
Unreviewed outcomes appear before reviewed outcomes.
Within those groups, failed outcomes appear before successful outcomes.
Records within each outcome group use the latest completion time first.

Queue disappearance alone does not establish completion.
Records loaded from the accounting window can appear unreviewed at startup.
Review state uses the job ID, timestamps, and terminal state.
It does not identify a retry by job ID alone.

Raven retains at most 512 reviewed record identities.
Normal state persistence saves these identities for the current user, host, and profile.
Switching scope does not acknowledge another scope's records.
Older records can return as unreviewed when the bounded review history expires.

If History cannot expose a record, expand its date window.
Inspect active filters if the record remains hidden.
The inbox does not select another job as a substitute.

### Troubleshoot completion review

| Symptom | Action |
| --- | --- |
| No unreviewed records appear | Use `:inbox all` and inspect the accounting source |
| A record is outside History | Expand the History date window and inspect active filters |
| The job ID identifies an active attempt again | Select the required History record before opening its evidence |
| Historical log paths are unavailable | Select the exact project run and use its declared log index |

An active attempt can reuse a historical job ID.
Raven refuses an ambiguous direct log action instead of opening the active attempt's logs.
Preserve separate project run identities and exact log declarations for reliable attempt inspection.

<a id="feature-49"></a>

<a id="49-snooze-alerts-and-set-quiet-hours"></a>

## Snooze alerts and set quiet hours

**Prerequisite:** Configure an alert rule.
See the [alert reference](../reference.md#alert-rules) for expressions and delivery actions.

### Snooze a rule

1. Enter `:alerts`.
2. Select a rule with Up or Down.
3. Press `s` to snooze its delivery for thirty minutes.
4. Inspect the active conditions and remaining snooze time.
5. Press `u` to remove that rule's general snooze.
6. Press Esc to return.

Use the palette for a custom duration or one exact job:

```text
:alerts snooze "idle gpu" 900
:alerts snooze "idle gpu" 900 12345
:alerts unsnooze "idle gpu" 12345
:alerts unsnooze "idle gpu"
:alerts unsnooze
```

Durations use seconds.
A duration must be greater than zero and no more than seven days.
An omitted job ID snoozes the complete rule.
An omitted rule in `unsnooze` removes all snoozes.
Raven retains at most 256 active snooze entries.

### Set daily quiet hours

```text
:alerts quiet 22:00 07:00 America/Los_Angeles
:alerts quiet 22:00 07:00
:alerts quiet off
```

Times use the 24-hour `HH:MM` format.
An omitted zone uses `America/Los_Angeles`.
Use an installed IANA time zone when you need another zone.
The start is inclusive and the end is exclusive.
An end earlier than the start defines an interval across midnight.
The start and end must differ.

Press `Q` in alert controls to disable quiet hours.
Normal state persistence saves snoozes and quiet hours for the current user, host, and profile.
The active configured time zone controls daylight-saving transitions.

**Expected result:** Quiet hours and snoozes suppress bell, notification, and command delivery.
Raven continues to evaluate conditions and record alert events.
Active conditions remain visible.
Suppressed deliveries are not queued for replay when the quiet period ends.
The existing rule interval still controls future delivery.

<a id="module-methods"></a>

## Integration reference

These methods integrate the procedures with the existing controller and worker.

| Method | Function |
| --- | --- |
| `Activity.post(text, level, path, job, task)` | Register a bounded notice and export metadata when a path is supplied |
| `Activity.start(label, source)` | Register a cancellable background task |
| `Activity.progress(task, done, total)` | Publish reported task progress |
| `Activity.finish(task, status)` | Publish the final task state |
| `activity_ui.export_items(app)` | Return the current filtered export records |
| `doctor.terminal_evidence(cfg, state_dir=...)` | Read terminal hints and path metadata |
| `session_tools.observe(app, snapshot)` | Register confirmed accounting completions |
| `session_tools.inbox_items(app)` | Return completion records in the active filter |
| `session_tools.unread_count(app)` | Count retained unreviewed records |
| `AlertEngine.snooze(rule, seconds, job)` | Suppress selected alert delivery temporarily |
| `AlertEngine.unsnooze(rule, job)` | Remove snooze controls |
| `AlertEngine.set_quiet(start, end, zone)` | Validate and store a daily quiet interval |
| `AlertEngine.notification_muted(rule, job, now)` | Check active delivery suppression |
| `AlertEngine.controls_snapshot()` | Export bounded alert preferences |
| `AlertEngine.restore_controls(data)` | Validate restored preferences |

`activity_ui` and `session_tools` also expose initialization, restore, save,
command dispatch, key handling, and overlay rendering methods.
Their display paths use retained data.
Their file previews and inspections use the shared background worker.
