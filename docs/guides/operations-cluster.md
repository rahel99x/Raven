# Cluster operations

Use these operations to inspect cluster services, storage, reservations, and
licenses, or to review a specific job change. Slurm clients, credentials, and
permissions must match the selected cluster connection.

Open the Operations workbench with `:ops`. Select a tool, set its fields, and
select **Inspect / prepare** to read current evidence. For a tool that changes
a job, starts a shell, or writes an export, select **Review action**. Check the
exact plan, then select **Confirm apply**.

Use `:ops KEY` to open one tool. You can also start an inspection with
`:ops run KEY key=value`. Quote values that contain spaces. Leave **Job ID**
empty to use the selected job. An explicit ID is preferable when you compare
several jobs.

Commands run on the selected cluster connection in a background worker. They
have time and output limits. Reading or scrolling a result does not run the
commands again. Select **Inspect / prepare** again to get new evidence.

## Storage space, inodes, and quotas — P01

Open `:ops storage`.

| Field | Use |
| --- | --- |
| Path | A path on the selected cluster. No recursive directory scan is made. |
| Quota tool | `quota` for conventional Linux quota; `lfs` for Lustre quota. |
| Quota kind | `user`, `group`, or `project`. |
| Quota user/group/project ID | User name or numeric ID. Group and project checks require an explicit identity. A blank user identity uses the configured Slurm user. |
| Minimum free GiB | The minimum free storage for this check. The default is 1 GiB. |
| Minimum free inodes | The minimum free inode count. The default is 1,000. |

Examples:

```text
:ops run storage path=/scratch/myproject min_free_gib=100 min_free_inodes=100000
:ops run storage path=/scratch/myproject quota_tool=lfs quota_kind=project quota_user=12345
:ops run storage path=/home/myproject quota_kind=group quota_user=mygroup
```

The result combines `df -Pk`, `df -Pi`, and the selected quota command. It shows
both byte and file limits. A hard quota or failed free-space threshold produces
a blocked result. A soft quota produces a warning because its grace period can
expire before the hard limit.

An absent quota command, unsupported output, or missing filesystem match means
**unknown quota**, not unlimited quota. The raw bounded quota evidence remains
available for site-specific formats. Select each quota kind that applies to your
project. This check does not create a test file. Directory permissions, ACLs,
other quota kinds, and later writes can still prevent a job from writing.

## Edit one pending-job field — P02

Open `:ops pending-edit`. Set the exact job or array-task ID, select a field, and
enter its new value. Supported fields are `TimeLimit`, `Partition`, `QOS`,
`Account`, `Nice`, `Dependency`, and `BeginTime`.

```text
:ops run pending-edit job_id=1234 field=TimeLimit value=02:00:00
:ops run pending-edit job_id=1234 field=Dependency value=afterok:1200:1201
:ops run pending-edit job_id=1234 field=Dependency value=0
:ops run pending-edit job_id=1234 field=BeginTime value=now+1hour
```

The review shows the owner, submit time, old value, and new value. Apply requires
the same owner, submit time, restart attempt, pending state, and original field.
A conflict stops the update. Slurm determines whether the user may change the
field. Raven does not change CPU or memory allocation through this editor.

After Slurm accepts the command, Raven reads the job again. It distinguishes a
confirmed value from an accepted request whose readback is unavailable or
different. Time-limit readback accounts for Slurm's minutes and formatted-time
representations. Relative begin times can require inspection of the resulting
absolute timestamp.

Slurm does not provide a compare-and-swap operation for this update. A job can
start between the final state check and the scheduler's update. The result shows
the state observed during readback. Raven does not retry a failed or uncertain
mutation automatically.

## Change live array concurrency — P03

Open `:ops array-throttle`. Select an array or one of its tasks. Enter the maximum
number of concurrent tasks. **Zero means unlimited.**

```text
:ops run array-throttle job_id=2000 limit=8
:ops run array-throttle job_id=2000_7 limit=0
```

Raven verifies the actual `ArrayJobId` and targets the array parent. It sends
`scontrol update JobId=... ArrayTaskThrottle=...`; it does not resubmit the array
or edit only a local plan. A lower throttle limits new starts. It does not cancel
tasks that already run.

Apply rechecks the parent identity and previous throttle. A changing remaining
task range does not invalidate the array identity. After the update, Raven reads
the throttle again. If the active parent cannot be identified or the Slurm
version does not expose enough evidence, Raven stops or reports the missing
confirmation.

## Reservation timeline — P04

Open `:ops reservations`. Set the number of hours to show, from 1 to 8,784.

```text
:ops run reservations hours=72
```

Each row shows a reservation window, state, node/core counts, user/account
restrictions, partition, license allocation, and flags when Slurm exposes them.
The timeline uses the scheduler host's current time and UTC offset. If that
clock query is unavailable, the result identifies the local-time fallback.
Unknown boundaries have an explicit unknown band.

A visible reservation is not proof that the selected user can use it. The
reservation's access rules and site policy determine eligibility. This view does
not create or change reservations.

## License inventory — P05

Open `:ops licenses`. Leave the name filter empty to inspect all visible Slurm
licenses, or enter part of a license name.

```text
:ops run licenses name=matlab
```

Raven shows total, used, free, and reserved counts, plus remote-server evidence
when present. Unknown counters stay unknown. These are Slurm's counters; a
separate vendor license server can have additional users or limits. The tool
does not acquire or reserve licenses.

## Slurm service doctor — A05

Open `:ops slurm-doctor` and select **Inspect / prepare**. The doctor checks:

1. The Slurm client version.
2. Primary and backup controller responses.
3. A public allowlist of configuration fields.
4. Partition availability.
5. The accounting database response.

The result separates available evidence from failed queries. It supplies short
diagnostic guidance for authentication, name resolution, connection failures,
permissions, and missing commands. A failed probe does not hide successful
probes. Disabled accounting and job-accounting collection have explicit
warnings.

The doctor does not read secret configuration files, restart services, or
change settings. On managed clusters, send the evidence to the administrator
through your normal support process. Raven does not send it automatically.

## Rerun one cancelled job

Use this action to restart one cancelled job with its retained Slurm submission.
Slurm restarts the batch script from the beginning under the **same JobID** and
a new execution attempt. Its retained script, arguments, exported environment,
and job settings stay with that individual job. Raven does not run the batch
launcher again.

1. Open Recents in Jobs, or open History.
2. Select one **CANCELLED** job. Expand a group first if necessary, and clear
   other marks so that the menu targets that job alone.
3. Right-click its row and select **Rerun cancelled job**.
4. Wait for the background inspection of the retained job and batch script.
5. Select **Review action**. Check the exact job ID, settings, dependencies,
   and output paths.
6. Select **Confirm apply** to request `scontrol requeue` for that job.

You can open the same inspection from the command palette:

```text
:rerun 1234
:ops run rerun-job job_id=1234
:ops run rerun-job job_id=1234_7
```

`:rerun` without an ID uses the exact selected job. These commands prepare a
review; they do not apply it automatically. After confirmation, normal Slurm
scheduling and dependency checks determine when the new attempt starts.

The job must belong to you, remain cancelled, have requeue enabled, and still
have its submission retained by the controller. An array target must identify
one exact task, such as `1234_7`. Whole-array targets, task ranges,
heterogeneous allocations, and multiple jobs are not accepted. Raven checks
the job identity and retained data again before apply. Changed or unavailable
evidence stops the action and requires a new inspection.

Slurm does not provide an atomic "requeue only this cancelled attempt" command.
Raven checks again immediately before dispatch and prevents repeated use of a
review. A separate client can still change the same job between that final
check and Slurm's command processing. Avoid concurrent rerun actions on the
same job from other clients. Raven records acceptance separately from readback
confirmation and does not retry an uncertain result automatically.

The current retained dependencies remain in effect. Slurm may already have
removed satisfied dependencies; this action cannot reconstruct them. It does
not restart upstream jobs, downstream jobs, or other jobs in the launch group.
The same output paths remain in use. The site's append or truncate settings
therefore apply to existing logs. External source files, executables, inputs,
and runtime environments are not frozen by requeue and can have changed since
the previous attempt.

If the controller has purged the submission, Raven reports why an exact rerun
is unavailable. It does not substitute the current local script or infer lost
environment values. An accounting record or an archived batch script alone
does not restore all submission settings.

| Required result | Control |
| --- | --- |
| Restart one retained cancelled submission under the same JobID | **Rerun cancelled job**, or `:rerun JOBID` |
| Prepare a new submission with explicit resource or script changes | `A`, or `:resubmit JOBID ...` |
| Inspect or save the retained original batch script | `:ops batch-script` |

The existing `resubmit` command creates a **new JobID**. It uses the recorded
submit line when available, or reconstructs an `sbatch` command from available
metadata. The current script and submission environment can differ from the
original. Treat it as an approximate clone and review its caveats and overrides.
Read the [resubmit reference](../reference.md#keys-remappable-in-the-config).

## Retrieve a retained batch script — A20

Open `:ops batch-script`. Enter a job ID. Leave **Save path** empty for a preview.

```text
:ops run batch-script job_id=1234
:ops run batch-script job_id=1234 output=/home/alice/exports/job-1234.sh
```

For a controller-retained job, Raven reads `scontrol write batch_script JOB -`.
For an accounting-only job, it verifies the exact accounting row and requests
`sacct --batch-script`. The cluster must retain that data. Older Slurm versions
and sites without batch-script accounting can report unavailable data.
An archived script is useful evidence for preparing a new submission. It does
not make a purged job eligible for the exact rerun procedure above.

The preview includes its source, submit time, SHA-256 digest, and the first 200
lines. It never executes the script. A save review pins the script digest and
destination directory identity. Apply reads the script again and creates one
new file with mode `0600`. Existing files and symlinks are never overwritten.
A replaced directory, changed script, reused job ID, or ambiguous accounting
record stops the export.

Saving requires Raven to run on the target host. An SSH profile can preview the
retained script; it cannot silently substitute a local filesystem for the
selected remote filesystem.

## Allocation shell and step attachment — A19

Open `:ops allocation-shell` for a running job that belongs to the configured
Slurm user. Select `shell` to start one interactive shell step, or `attach` to
join an existing running numeric step.

```text
:ops run allocation-shell job_id=1234 mode=shell shell=/bin/bash
:ops run allocation-shell job_id=1234 mode=attach step_id=1234.0
```

The shell action reviews an explicit `srun --jobid=... --overlap --exact`
command with one node, one task, one CPU, a short immediate-start deadline, and
a pseudo-terminal. It uses the existing allocation. It does not call `salloc`,
request a new job, or alter the batch script. The shell path must be an absolute
path that exists on the compute node. Site policy or an older Slurm version can
refuse the step.

Attach accepts only a currently running numeric step in the selected allocation.
It rejects `batch`, `extern`, another job's step, and a completed step. `sattach`
joins the application's input/output; use it only when that application can
accept interactive input.

For an array task or heterogeneous component, Raven resolves the logical ID to
the scheduler's numeric allocation ID before it builds the terminal command.
The review retains both identities. Step selection uses the numeric ID that
Slurm reports. An unavailable existing-step inventory blocks attachment, but
does not prevent review of a new shell step.

Apply checks the job's owner, submit time, start time, and restart count. The
terminal handoff checks them again before launch. Raven suspends its terminal
view while the command owns the terminal and restores the view when the command
ends. Exit the shell to return. A terminal handoff requires Raven to run on the
target host; it is unavailable through a remote SSH profile or recorded session.

## Validation and limits

The cluster test suite covers malformed/oversized evidence, exact array IDs,
job-ID reuse, state transitions, changed fields, script-digest changes, export
directory replacement, symlinks, cancellation, unsupported quota formats,
controller failures, and sequential operations. The storage tests also run real
local `df` commands. Terminal tests validate the command and revalidation path;
they do not start a real production job.

The published JSON examples also run through their native readers and the real
headless command interface. For an optional structural audit, use a developer
Python environment with `jsonschema >= 4` and run
`python scripts/validate_operation_schemas.py`. This checks all schema documents
and their published examples without contacting Slurm. Raven itself does not
require `jsonschema`.

Live Slurm policies, Lustre/project-quota variants, accounting retention, and
interactive allocation steps still need a site smoke test. A missing facility
is reported explicitly instead of being simulated as successful.

## Slurm references

- [scontrol: job updates, reservations, licenses, and retained scripts](https://slurm.schedmd.com/scontrol.html)
- [Job arrays: task limits and array identifiers](https://slurm.schedmd.com/job_array.html)
- [sacct: accounting records and batch scripts](https://slurm.schedmd.com/sacct.html)
- [srun: allocation steps](https://slurm.schedmd.com/srun.html)
- [sattach: attach to a job step](https://slurm.schedmd.com/sattach.html)
