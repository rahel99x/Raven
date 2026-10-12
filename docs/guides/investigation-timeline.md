# Join evidence for a job

The investigation timeline joins scheduler transitions, log lines, and registered
trace events for one job attempt. Use a connection with a declared cluster name
and readable job evidence. Collection runs on a background worker; pointer
movement and rendering use the retained report.

## Collect the selected job

1. Select an exact job in Jobs or History.
2. Open `:ops investigation-timeline`, or choose **Investigate → Join
   investigation events** from `:workbench`.
3. Leave **Optional timeline manifest** empty. The selected job ID is filled in.
4. If the project uses a separate reporting root, set **Optional registered
   project root**. Raven also checks the root associated with the scheduler's
   working directory. It does not search arbitrary ancestor directories.
5. Select **Inspect / prepare**. Use **Refresh** to collect a new observation.

**Expected result:** The report shows the collected events, their source identity,
and any unavailable or bounded sources.

Raven verifies the selected submit and
start identity against a fresh controller response. If the controller no longer
retains the job, Raven tries accounting. A changed job ID, attempt, owner or
cluster cannot silently select another execution.

The collector joins these sources:

| Source | Read limit | Interpretation |
| --- | --- | --- |
| Scheduler | One verified controller or accounting record | Submit and start transitions; terminal end time when retained; current observed state |
| Standard output and standard error | 64 KiB and 256 lines per distinct file | Stable tails from the exact paths in the verified scheduler record |
| Registered traces | Eight streams, 3,000 events and a 2 MiB journal budget | Checked committed prefixes for the exact cluster, job and submit/start attempt |

Limits can stop a read after its final complete trace record. The report states
which bounds were reached. Full journals and logs remain unchanged.

A running job's `EndTime` is a forecast and is not an observed completion. A
pending job's predicted `StartTime` is not an observed start. An accounting end
time from the published selected record is identified separately from the fresh
identity check.

If Slurm has discarded a historical log path, the report lists that missing
source. It does not substitute another running job's files. Local automatic log
reads reject symlink parents and file replacement. SSH reads use the configured
remote file connection and compare file identity before and after collection.
Registered trace journals currently require Raven on the journal host; remote
views never substitute local files.

## Read clocks and identity correctly

An offset-bearing ISO timestamp, such as `2026-10-10T10:01:01Z`, supplies epoch
seconds. A Slurm or log timestamp without an offset remains textual. A log line
without a timestamp keeps a source ordinal. Tail ordinals start at the first
line in the displayed window; they are not absolute line numbers in the file.

Trace monotonic clocks retain their node and boot identity. Different clock
domains remain separate. Display order does not prove causality or establish
clock synchronization.

The scheduler verifies log **paths**, not the producing attempt of every line.
Append-mode or reused files can contain older output. The report marks these
line identities as unverified instead of asserting that all content came from
the current execution.

## Import a declared timeline

Set **Optional timeline manifest** to a JSON file and select **Inspect / prepare**
to inspect supplied evidence. A manifest uses `raven.timeline/v1` and contains
at most 20,000 events. Each event declares:

- `id`, `source`, `cluster`, `job_id` and `attempt` for its namespace.
- `clock`, `kind` and `message` for its interpretation.
- `timestamp` in seconds and optional nonnegative `uncertainty_s`; or
  `timestamp: null`, a positive `ordinal`, and optional `timestamp_text`.

Use [the example](../../examples/runtime/investigation-timeline.json) and
[JSON Schema](../schemas/investigation-timeline.schema.json) as a
starting point. Timestamp-less events retain source order. A selected job
cannot combine several declared clusters or attempts. Legacy manifests without
`cluster` remain readable but are marked as unassociated evidence.

The operation is read-only. Copy the report with its source metadata when an
investigation must be reviewed or reproduced.
