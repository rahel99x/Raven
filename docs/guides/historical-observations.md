# Historical observations

Raven can retain bounded, immutable observations of Jobs, History, and the
selected job's published telemetry. Use this when you need to distinguish what
Raven knew at a particular time from information that arrived later.

## Record and inspect

In an open Raven session, enter these commands in the command palette:

```text
:asof record on
:asof
```

Recording is opt-in and uses already-published data. It does not issue additional
Slurm commands or read telemetry archives. A changed publication can produce one
snapshot every two seconds. If you need a particular job's telemetry, select it
before recording. A source that has not yet loaded remains unavailable in earlier
snapshots, even if its eventual samples carry older timestamps.

Historical inspection has seven keyboard-accessible controls: Live, Previous,
Next, Jobs, History, Analytics, and Diff. Use the left/right arrows to move between
observations, `1`/`2`/`3` to change views, `d` to compare the preceding retained
observation, and Escape to return live. Tab and Shift-Tab move between controls.
The mode consumes other shortcuts and disables action commands, confirmations,
shell input, and live table clicks. Live collection continues independently.

The existing Run home also opens historical inspection. Its retained run binding
is restored on return. Run navigation and a snapshot's selected telemetry are
labelled separately: a job number alone does not prove that they describe the
same scientific attempt. The snapshot shows the actual recording identity.
Returning live restores the original selection and scroll positions if that
exact identity still exists. A requeued or replaced attempt clears the selection
with an explanation instead of selecting a different job silently.

## Choose an observation time

```text
:asof 2026-10-17T10:30:00+00:00
:asof 1792233000
```

Use Unix seconds or an ISO timestamp with an explicit timezone. Raven selects the
last observation at or before that time. The interval after that observation is
unknown; Raven does not invent changes between snapshots. Before the first
retained observation, the result is unknown.

A source-scope change, observer restart, or backward clock movement starts a new
clock epoch. If several epochs are present, choose the displayed epoch explicitly
or navigate by sequence:

```text
:asof 2026-10-17T10:30:00+00:00 2
```

Each observation reports its completion time, scheduler source time, source
errors, omitted record counts, and telemetry attempt identity. Chart bars show
observations; blank intervals remain unknown. Missing GPU data does not suppress
CPU or memory. Unknown, future, or unrepresentable source clocks are retained in
the archive but excluded from chart placement. Eight GPU devices can be charted;
additional device values remain in the exact records.

Diff compares recorded fields, not unobserved transitions. Verified attempt tokens
include restart evidence, so same-second requeues are distinct. Duplicate
scheduler identities are marked ambiguous. Unverified rows from different
observers or source scopes are not joined as one attempt.

## Save and reopen

Return live before changing recording or publishing an archive:

```text
:asof live
:asof record off
:asof save /absolute/path/new-observation-directory
:asof load /absolute/path/new-observation-directory/timeline.json
```

Save publishes a new directory atomically and refuses to overwrite an existing
one. Load validates the version, bounds, identity correlations, clock metadata,
and every snapshot checksum. These checks establish internal consistency, not
an independent signature or proof that an archive's producer was truthful.
Loading replaces only this inspection timeline and stops snapshot recording.
It does not change scheduler state or the original source files.

Memory retains at most 64 observations and 7 MiB of encoded snapshots. Each
snapshot is capped at 2 MiB, 2,000 Jobs rows, 2,000 History rows, 128 transitions,
and 10,000 selected-job samples. A snapshot that exceeds its document budget is
refused visibly; previous snapshots remain available. Expired observations and
omitted rows are disclosed. Recording is not a complete scheduler audit log.
Snapshots are saved only on an explicit save; the ordinary state directory does
not grow automatically. Decoding, validation, comparison, and chart preparation
run in the existing bounded worker lane; paint uses prepared visible rows.

## Existing command recordings

The existing `--record`/replay workflow remains available. New command recordings
store request time separately from **observation publication time**, stamped
under the recorder's write lock. Each recorder has an observer ID, an increasing
sequence, and a monotonic clock. Replay cannot return an answer before its
recorded publication.

Only a consistent recording from one observer and one clock segment is labelled
**observed replay**. A backward wall clock, appended recorder sessions, missing or
inconsistent provenance, or omitted invalid records makes it **reconstructed
replay**. The replay summary explains the reason. Reconstructed recordings remain
readable in timestamp order, but overlapping clock segments cannot establish
which observation was known during an earlier occurrence of that wall-clock
time. Older recordings are also reconstructed: their request-era timestamps do
not establish when an answer became available. Provenance checks establish
internal consistency, not an independent signature or proof of producer truth.

Replay reads at most 64 MiB of decoded JSONL, 8 MiB per line, and 200,000 command
records. Malformed JSON and invalid command durations are omitted with a visible
count; incomplete coverage is never labelled known-at-time. Invalid UTF-8 or a
truncated compressed stream produces a readable input error. Recorded command
execution remains read-only in replay.
