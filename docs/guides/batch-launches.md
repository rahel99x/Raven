# Group jobs automatically or manually

[README](../../README.md) · [Controls](../CONTROLS.md) · [Project reporting standard](../PROJECT_STANDARD.md) · [Adjustable workspaces](adaptive-workspaces.md)

Use automatic launch detection or explicit membership to organize related jobs.
Raven can group jobs submitted before startup and jobs that arrive during the
session. Grouping changes the displayed rows; each member retains its actual
Slurm job ID, logs, measurements, and action target.

The procedures assume default keybindings and focus in the relevant job list.
You can create a manual group from marked jobs, move members between groups, or
remove members. Scheduler actions require a separate review for exact job IDs.

## Create a manual group

1. Click a job row in the required list to focus that pane.
2. Mark at least two jobs with `Space`, plain click-drag, or click followed by Shift-click on the other end of the range. Shift-click works in either direction. Hold Shift during a drag to retain earlier marks.
3. Check the marked job IDs.
4. Press `g`, or enter `:jobgroup create`.

**Expected result:** Raven creates a closed manual group and selects its real representative row.
The group uses the existing count and state-summary formatting.
Raven clears the marks consumed by this operation.
Creating a group enables grouping display if it was disabled.
Click the down-pointing chevron to show its members.

The selected jobs do not need to have matching names or submission times.
Manual membership takes precedence over automatic launch deduction.
You can select members from different existing groups to form a new group.
On Jobs, the marked real records can come from both Main and Recents.
In other workspaces, only the marked real records in the focused list are included.
A marked closed summary contributes its representative job, not its hidden members.
Open the old group and mark its members when you want to include those jobs.

Marks outside that scope or excluded by its current filters are not included.
They remain marked. After a membership change, Raven reports how many other job marks remain.
Offscreen rows in the same filtered scope remain eligible.
When fewer than two eligible jobs are marked, `g` retains its ordinary beginning-of-list behavior.
The explicit `:jobgroup create` command instead reports that at least two marked jobs are required.
Use Home when you need to move to the first row without grouping eligible marks.

The controls apply to the job lists in Jobs, Recents, History, Group, Dependencies, Advisor, and the job-history panels in Analytics, Logs, and Research.
The history browser and the underlying Advisor list are separate selection scopes even when both are visible.
Focus must remain in a job list.
An open menu, input editor, graph control, rendered text selection, or graph/slider drag keeps its own input behavior.
Marks retained from a different pane do not make a graph or log key change job groups.
For example, `u` still undoes a graph zoom while the graph owns input; it does not clear job marks retained in another pane.
Click the intended job list before using its grouping keys.
Release a job-row drag before grouping its selected range.
If a grouping key ends an unfinished job-row drag, its delayed mouse release cannot extend the old selection or activate a different control.

## Add jobs to a group

Use an existing group's closed summary or any of its visible expanded members as the destination.

1. Click a job to select it, or mark several jobs with `Space` or a range selection.
2. Hold **Alt**, then press the left button on that selected row or on one of the marked rows. Avoid its fold chevron and queue-order handle.
3. Drag to a row in the destination group.
4. Release the button on that group row.

**Expected result:** Raven adds the exact source jobs to the destination group.
A drag from a marked row uses the applicable marks in its focused list; Jobs can include marks from both Main and Recents.
A drag from an unmarked row that you previously clicked and released moves that one job.
Plain click-drag always selects a range, including when its first row is selected or marked.
Alt-drag explicitly requests a membership move. Shift takes precedence over Alt and keeps range selection.
A closed source summary contributes only its real representative. Expand it and mark its members to move those members together.
The destination can be closed or expanded.
Raven highlights an eligible destination and displays a move hint while you drag.
You can drop into another job pane on the same page when its destination row is visible.
You can release on the destination group's chevron as well as its row.
A left press on a chevron still folds or opens that group; it does not start a move.
Other buttons, scrollbars, and dividers retain their own controls and are not drop targets.

The destination keeps its label. Adding to an automatically detected group creates a manual group containing its known members and the added jobs.
An existing manual group retains its group identity and saved members that are absent from the current history window.
Moving a member removes it from its previous manual group without changing other members.
Adding a job already in the destination does not duplicate it.
Manual membership uses natural job-ID order: `1`, `2`, ..., `9`, `10`, rather than the order in which members were added.
Array task IDs also use natural order, such as `123_2` before `123_10`.
Displayed rows follow each list's active sort, including its default sort.
Sort JOBID ascending to display the group in increasing numeric order; adding `7–9`, then `4–6`, to members `1–3` then displays `1–9`.

Press Esc or right-click to cancel an unfinished move and keep the job marks.
Releasing outside an eligible group cancels it without changing membership.
A new keyboard action, changed page, layout, filter, sort, source identity, or destination membership invalidates the gesture instead of redirecting it to different jobs.
Raven checks the current execution attempts again before it applies a change.
Pointer movement uses published row data and does not issue scheduler queries.
The terminal must report press, movement, and release events; use the menu below if it does not.

## Use the job menu

Right-click a selected row or one of the marked rows to open its job menu.
Right-clicking a marked row targets the applicable marks in that list.
Right-clicking the active unmarked selected row targets that exact job and keeps unrelated marks.

You can also right-click an expanded or collapsed group header without selecting it first.
Its menu shows the group name and the number of targeted jobs.
The targets include matching members outside the viewport and members hidden by collapse.
The pane's filters still apply. Jobs that appear only in another pane are excluded.
Opening or cancelling the menu preserves existing marks and the group's expanded/collapsed state.

An expanded table can share its header row with its first job.
Right-click that row's chevron to target the group explicitly.
Right-click marked data cells on that expanded row to retain the existing multi-selection menu.
A collapsed header always targets its matching group members.

| Choice | Result |
| --- | --- |
| **Create Group** | Create a closed manual group when at least two exact jobs are targeted |
| **Ungroup** | From a group-header menu, detach the listed members only; from a member-selection menu, retain the existing closed-group or expanded-member behavior |
| **Add to GROUP NAME** | Add the exact targeted jobs to an existing group; a destination that already contains all targets is omitted |
| **More (N more groups)** | Expand the menu to show the remaining eligible groups |
| **Rerun cancelled job** | Inspect one exact cancelled job's retained submission, then review and confirm its restart |
| **Export logs** in History | Open the existing complete-log export procedure for the exact targeted jobs |
| **Cancel** | Close the menu without changing jobs or membership |

Use this menu when the destination group is not visible in the current viewport.
The menu first shows the three most recent eligible groups. Recency uses each
group's latest known job submission time; natural job ID order breaks ties and
provides the fallback when submission times are unavailable. Select **More**
to reveal the rest. Reopening the menu restores the compact list. **Cancel** is
always the last choice, and End selects it even in a small terminal.
If two destinations have the same label, their displayed ID ranges distinguish the choices.
Use the arrow keys, Tab, Shift-Tab, Page Up, Page Down, Home, or End to select a choice; press Enter or Space to activate it.
The mouse wheel scrolls a long menu. Esc, `q`, or Ctrl-C closes it.
F8 also enables the standard directional button navigation for its visible choices.
Creating and removing groups follows the same identity, focus, and persistence rules as `g` and `u`.
The group choices change Raven's membership only. **Rerun cancelled job** opens
a separate scheduler-operation review for one job in Recents or History. It
restarts that job under the same JobID and preserves its retained individual
submission; it does not launch the group again. Controller retention and
requeue permission are required. Read [Rerun one cancelled job](operations-cluster.md#rerun-one-cancelled-job)
for array-task limits, dependency handling, and log-path behavior.
The separate queue-order handle described below also opens a scheduler-operation review.
Right-click outside a selected job keeps the existing selection-clearing behavior, subject to History's log-export control and the graph-reset controls.

## Remove a group or some members

| Required change | Procedure |
| --- | --- |
| Remove an entire closed group | Clear unrelated marks with `U`, select the closed summary, and press `u` |
| Remove one member from an open group | Clear unrelated marks with `U`, select that member, and press `u` |
| Remove several members from one or more open groups | Mark those members and press `u` |
| Remove several closed groups | Mark their closed summary rows and press `u` |
| Clear marks without changing any groups | Press `U` |

You can use `:jobgroup ungroup` or the job menu's **Ungroup** choice for the same membership operation as `u`.
Applicable marks take precedence over the single selected row.
A targeted closed summary removes the whole group, including its known members that are outside the current pane or hidden by filters.
For a saved manual group, this includes saved members that are not in the currently fetched records.
A targeted expanded row removes only that exact member.
This rule applies to automatically identified groups and manual groups.
The remaining members retain their group when enough members remain.
A manual group with only one currently available member appears as an ordinary job row; that member does not merge into a different automatically detected group.

Removed jobs remain individual rows instead of immediately returning to an automatically deduced group.
You can include them in a later manual group with `g`.
Within a focused job list, `u` keeps its previous clear-marks behavior when no applicable grouped target exists.
Use `U` to clear job marks explicitly without changing membership.
These commands change Raven's local organization. They do not alter Slurm jobs, array membership, dependencies, allocation, or report files.

### Keep membership attached to the correct attempt

Manual choices are shared across Raven's job lists and their history browsers.
They apply to exact execution attempts rather than job numbers alone.
Raven uses the available cluster, job ID, and submit-time evidence to retain these choices in saved UI state.
A reused job number with a different submit time does not inherit an old manual group or removal.
Filtering, sorting, or temporarily losing a member from the fetched history does not change the saved attempt identity.
When the missing members return, the same manual membership applies.

When the submit time is unavailable, Raven keeps the choice in the current session only.
An ambiguous record replacement invalidates that temporary membership instead of assuming it is the same job.
If current and historical records disagree about the same job ID's attempt, Raven refuses the membership change and asks you to select refreshed records.
No scheduler query is issued merely to paint a group or move the pointer.
Saved preferences are local to the current Raven state directory; another installation does not receive them automatically.
The preference store accepts at most 256 manual groups and 8,192 saved or session identities across groups and explicit removals.
An operation that exceeds a limit reports the problem without applying a partial change.
Ungrouping retains an identity as an explicit removal, so it does not free an identity slot.
Use `:jobgroup reset` when you want to discard all manual choices and release these slots.

### Reset manual preferences

Enter `:jobgroup reset` to remove every manual group and every explicit exclusion from automatic grouping.
This includes saved preferences and temporary session choices across all job lists.
Automatic grouping can then identify those jobs again from its normal evidence when grouping display is enabled.
Reset preserves the current `:jobgroups on` or `:jobgroups off` setting.
Use `:jobgroups on` if grouping display is currently disabled.

Reset changes Raven's local display preferences only.
It does not cancel or modify Slurm jobs, remove reports, or change scheduler arrays and dependencies.
This command clears all manual grouping choices, not only the currently selected group.

## Fold and open a batch

1. Open a list of jobs.
2. Find the right-pointing chevron on the first visible row of an expanded group.
3. Click that chevron to close the group.
4. Read the representative job ID and the existing state summary.
5. Click the down-pointing chevron on the closed summary row to show the members again.

**Expected result:** One row represents the closed group.
Opening it restores the matching members under the current filters and sort order.
The fold preference applies across job lists and their history browsers.
New automatically detected groups start open. New manual groups start closed.
The chevron indicates the available action: `▸` closes an expanded group; `▾` opens a closed group.
ASCII mode uses `>` to close and `v` to open.
The first row follows the current grouping, filters, and sort order.
If that row scrolls above the pane, the first visible member receives the disclosure control.
You can therefore close the group without scrolling back to its original header.
A click applies the action shown on that frame. Duplicate events for the same painted control do not reverse its action.
The disclosure control does not select or mark every member.

The same controls apply to Jobs, Recents, History, account Group rows, and job-history browsers in Analytics, Dependencies, Logs, and Research.
Advisor's running-job list and independent dependency roots also support folding.
Dependency trees retain their relationship structure.
Research → Arrays also has cohort disclosure arrows with the same orientation.
They open or close that cohort's task page while keeping its summary and mosaic visible.
Array task-page state is separate from shared launch-group folds.
See [Array operations](../RESEARCH.md#array-operations) for full-page and inline controls.
Aggregate charts, timelines, explicit comparison traces, and node ownership displays retain their full measurement context; their job-history browser supplies grouped job selection.

During ordinary job-row navigation, Left closes the selected job's group and Right opens it.
With directional button focus active, arrows move between controls instead.
Use F8, arrows, and Enter to focus and activate a visible fold control.
Use `:jobgroups off` to show individual jobs, or `:jobgroups on` to restore grouping.
Use `:jobgroup open GROUP_ID`, `:jobgroup close GROUP_ID`, or `:jobgroup toggle GROUP_ID` to change a known group directly.
Use `:jobgroup create` and `:jobgroup ungroup` with the focused list's current selection to change membership.
Normal saved UI state retains grouping and closed-group preferences.

## Read the closed row

The group label identifies the launch and its matching record count (`records`, or `rec` in a compact browser).
The INFO cell shows compact state badges in the current theme.
Zero counts are omitted. Unicode separates badges with `·`; ASCII uses `|`.
For example:

```text
4 run · 2 pend · 3 dep · 1 never · 8 done
```

| Badge | Narrow form | Meaning |
| --- | --- | --- |
| `run` | `R` | Running jobs and scheduler transition states such as configuring or completing |
| `pend` | `P` | Pending jobs without a reported dependency wait |
| `dep` | `D` | Pending jobs with a dependency reason or dependency expression |
| `never` | `!` | Pending jobs with Slurm's `DependencyNeverSatisfied` reason |
| `done` | `C` | Completed jobs |
| `fail` | `F` | Failed jobs, including timeout, out of memory, node failure, preemption, and deadline failure |
| `cancel` | `X` | Cancelled jobs |
| `other` | `?` | Other reported states, including evidence that does not yet establish a terminal outcome |

A narrow cell puts the count after the short code, such as `R4 P2 D3 !1 C8`.
Point at the group control to read the fuller status description.

Each observed record contributes to one category only.
The pending count excludes `dep` and `never`.
These badges report scheduler evidence; `never` does not infer failure from a long wait.
Counts cover members that match the current list and its filters, not every job that might exist in Slurm.
Expanded group information can also show matching members against the total observed group membership.
Compressed Slurm array ranges count as observed records, not as an invented count of individual task states; the summary adds `(records)`.
Queue records take precedence over accounting records with the same exact job ID.

Recents and History add INFO when a closed group needs that summary.
Resource cells on a closed row are blank rather than showing one member's measurements as batch totals.
The narrowest layouts can clip badge text; enlarge the pane or expand the group to inspect its members.

## Mark a batch explicitly

Use an explicit marker when your project already knows which jobs form one launch.
Create one unique ID for that launch and pass it as the whole Slurm comment on each member.
Raven accepts `launch:ID` and `group:ID`.
The ID must start with a letter or digit and contain 1–128 letters, digits, `.`, `_`, or `-`.

For example, run these commands from one project working directory.
Replace the script paths with your project's actual batch entry points.

```bash
raven_batch_id="study-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
sbatch --comment="launch:${raven_batch_id}" jobs/first.sbatch
sbatch --comment="launch:${raven_batch_id}" jobs/second.sbatch
sbatch --comment="launch:${raven_batch_id}" jobs/third.sbatch
```

**Expected result:** Raven groups available members that share the marker and its scope.
It reads comments from queue and accounting publications when Slurm supplies them.
Slurm must retain and expose job comments for that marker to remain available in historical accounting.
The full comment must match the supported marker; free text around it is not a launch ID.
An explicit marker does not add scheduler dependencies or change job resources.

Markers are scoped by cluster, owner, account, and working directory.
Use the same project working directory for all marked members that you want to group.
Use a new marker for each independent launch, including a new batch retry.
Reusing a marker in the same scope can combine otherwise separate launches.

You can tag jobs that already exist from Raven's command palette:

```text
:tag 101 102 103 launch:study-17
```

Use the actual available job IDs.
Local tags persist in the current Raven state directory; they are not written to Slurm and are not shared automatically with another installation.
Remove a local marker with `:untag 101 102 103 launch:study-17`.
Conflicting local marker tags do not establish a reliable local marker.
When an integration already publishes `RavenLaunchId`, `LaunchId`, `LaunchGroup`, or `JobGroup` in scheduler details, Raven also accepts that explicit ID.
An explicit scheduler detail takes precedence over a comment; a recognized comment takes precedence over local marker tags.

## Understand automatic deduction

Raven uses these evidence rules in order:

| Evidence | Requirement |
| --- | --- |
| Slurm array or heterogeneous component | Actual member IDs share their scheduler parent |
| Explicit launch marker | Matching marker within the same cluster, owner, account, and working-directory scope |
| Dependency-connected launch | Direct reported dependency links, matching owner, account, and working directory, submission span of at most 60 seconds, and numeric job ID span of at most 256 |
| Launch with at least three jobs | Matching owner, account, working directory, and name family; submission span of at most 30 seconds; each numeric ID gap at most eight; total ID span at most four times the number of gaps; observed script paths cannot conflict |
| Strong two-job launch | Matching nonempty owner, account, working directory, exact command, and name; adjacent numeric IDs; submission span of at most ten seconds |

A numbered name family removes a final numeric suffix preceded by `.`, `_`, or `-`.
For example, `fit-1`, `fit-2`, and `fit-3` share a family.
Submission spans are measured from the first member, so a chain of nearby submissions cannot extend a batch indefinitely.
The wider launch rule tolerates a few other users' jobs between your submissions.
When accounting lacks command fields, the three-job rule can use the remaining evidence; a known conflicting script still prevents that grouping.
Missing owner, account, working directory, or submission evidence prevents ordinary launch deduction.
Array identities and available explicit markers remain usable under their own rules.

An inferred launch is **likely**, not proof that all jobs used the same input or belong to one experiment.
Two independent submissions can satisfy these rules, and a sparse or slow real batch can fail them.
Use a unique explicit marker for reliable project-defined membership.
Use `u` to remove a particular group or member when a deduction is unsuitable.
Use `:jobgroups off` to show individual rows throughout the interface.
Batch and extern steps do not become separate launch members.
A later submit time for a reused job ID identifies a new inferred attempt.

## Keep job actions exact

A closed row retains one real representative ID.
Opening Details, reading logs, and other single-job actions use that ID.
Closing a scheduler list group can move a hidden selected member to the representative.
A data page already opened for an exact job can retain that job while its browser group is closed.
Check the displayed job ID before acting.

Collapsing a group does not mark its hidden members for cancellation, export, comparison, or another bulk action.
Open the group and mark the actual jobs that you need, or explicitly open its group-header menu.
The header menu targets the matching members shown by its count, including collapsed and offscreen members.
In History, **Export logs** uses those exact targets. A group-header menu never offers an individual-job rerun.
Header-menu **Ungroup** detaches only the targeted members; filtered and other-pane members remain grouped.
Mouse range selection follows visible real rows and does not add hidden children.
The membership-removal command is explicit: `u` on a closed summary dissolves its whole group.
This rule does not change the target rules for cancellation or other scheduler actions.
Existing dependency-chain actions retain their own confirmation and target rules.
Review the target IDs in each confirmation.

## Load older batches

Grouping uses the queue, available account jobs, retained queue departures, and fetched accounting records.
It also uses their available metadata and local job tags.
It does not retrieve an unlimited scheduler archive.

1. Open History.
2. Use `:days 30`, or select the required History date range.
3. Wait for a successful accounting update.
4. Remove filters that exclude the required jobs.
5. Inspect the available groups.

**Expected result:** Eligible old jobs group as their records and evidence become available.
The configured `history_days` value sets the initial accounting window.
Source access, Slurm retention, permissions, and filters can limit the visible records.
Use Sources to check an accounting error or delayed update.
If Slurm rejects the optional accounting metadata fields, Raven falls back to its legacy history fields.
The job history remains available, but missing provenance can reduce automatic grouping.
Existing session recordings remain readable. An older recording without owner, account, command, or marker evidence cannot supply those missing grouping facts.

New queue and accounting publications update grouping without a restart.
A newly fetched record can complete a group that previously had only one known member.
Missing provenance remains unknown. Matching names alone do not establish a shared batch.

## Preserve each job's reports

A launch group is not a replacement for run identity.
Keep one run attempt and one exact `job_id` for each job or array task.
Continue to publish each task's `run.json`, `metrics.jsonl`, `logs.json`, and declared Research reports separately.
No new grouping field is required in these files.
See the [project reporting standard](../PROJECT_STANDARD.md#identity-and-path-rules) for the identity rules.

## Integration methods

The grouping layer reads supplied snapshots. It does not run Slurm commands or read files while it renders a group.

| Method | Purpose |
| --- | --- |
| `job_groups.Registry.ensure(snap)` | Refresh cached evidence from the supplied snapshot and return the current inferred group index |
| `job_groups.frame(app, snap)` | Share one inference check across a display composition, then release that frame scope |
| `job_groups.project_records(app, snap, records, tab, ...)` | Apply the current folds to a list and retain exact real records and representative metadata |
| `job_groups.project_rows(app, rows, snap=None, tab="jobs", ...)` | Apply group presentation to prepared queue rows |
| `job_groups.fold(app, group_id, collapsed=None)` | Change a known group's fold state and reanchor hidden list selection where required |
| `manual_job_groups.create(app, snap, job_ids)` | Create a manual membership override from exact supplied records; do not expand closed summaries implicitly |
| `manual_job_groups.detach(app, snap, job_ids=(), group_ids=())` | Remove exact member records or all members of explicitly targeted groups and prevent their automatic regrouping |
| `manual_job_groups.validate_state(value)` | Validate and bound saved membership and removal records |
| `manual_job_groups.reset(app)` | Clear all saved and session manual memberships and exclusions while preserving the grouping display setting |
| `job_groups.status_counts(records)` | Count distinct observed records in disjoint state categories |
| `job_groups.summary_segments(records, ...)` | Return themed status text without changing job identity or marks |

## Use the Main grouping controls

The five buttons above Jobs Main use the same group preferences as the other
job lists. Narrow panes wrap the controls without covering the table headings.

| Button | Result |
| --- | --- |
| **Auto Group** | Enable inferred launch groups and close the inferred groups currently found. Preserve manual membership and explicit exclusions. |
| **Undo Auto** | Restore the inference setting and fold changes owned by the preceding Auto Group action. Preserve later disclosure clicks and manual membership edits. |
| **Ungroup All** | Clear saved groups, folds, and exclusions, and disable automatic inference. Jobs remain individual until you group them manually or use Auto Group again. |
| **Expand All** | Open the shared job groups without changing their membership. |
| **Collapse All** | Close the shared job groups without changing their membership. |

The matching commands are `:grouping auto`, `:grouping undo`, `:grouping clear`,
`:grouping expand`, and `:grouping collapse`. After Ungroup All, `:grouping restore` restores its
session snapshot if no newer membership change would be overwritten.
These controls affect the shared group presentation; they do not change Slurm.
Collapse All uses the existing 256-group fold limit. If the known group count
exceeds that limit, it reports the limit and leaves all folds unchanged.

## Review a group's queue order

Use the **↕** handle beside a Main group header. ASCII mode uses `:`.
The fold chevron remains a separate control.

1. Press the left button on the order handle without Alt or Shift.
2. Drag upward to place the source before another group, or downward to place it after that group.
3. Release on the destination group's visible row.
4. Wait for the background queue inspection. Read the exact proposed order and excluded jobs.
5. Select **Review action**, then **Confirm apply** only if the displayed order is correct.

For an expanded source group, the visible members dim and only its header
label follows the pointer. A closed group uses the same header preview.
Esc, right-click, or an invalid drop cancels the gesture. A changed row order,
layout, execution attempt, or destination invalidates it. Hover and dragging
use the published frame and do not query the scheduler.

Raven uses Slurm's native `scontrol top` command. The site must enable
`SchedulerParameters=enable_user_top`. Source and destination need pending
jobs owned by your account in the same partition, Slurm account, and QOS.
Running, completed, and archived members stay unchanged. Held jobs,
heterogeneous identifiers, compressed array ranges, and ambiguous scope are
rejected. Exact array task IDs are supported.

The review includes all eligible pending peers in that scheduling scope,
including peers outside the two displayed groups. It preserves their relative
order and shows the complete resulting order. Inspection is bounded to 512
jobs in the scope. Unrelated jobs in other scopes do not consume that bound.
Before applying, Raven rechecks identities, restart counts, priorities,
configuration, and scope. A changed plan requires a fresh review.

This operation changes your eligible jobs' scheduler priority order. It does
not alter another user's job records. Slurm can adjust nice values as part of
its native operation. Dependencies, resources, reservations, and site policy
still determine actual start times. Raven cannot reserve the scheduler between
its final check and Slurm's command execution; the review states this limit.
The displayed table still follows its current sort; drag order does not
silently replace that sort.

See [Queue-order operation](operations.md#additional-operation-own-pending-queue-order)
for the command form and detailed restrictions.
