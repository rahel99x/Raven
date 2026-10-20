# Recording storage and recovery

Raven keeps scheduler observations, application measurements and job-side
telemetry as distinct sources. A missing source does not invalidate measurements
from another source. Sampling gaps and storage limits remain visible in coverage
metadata; an unavailable measurement is never converted to zero.

## Attempt identity

Native scheduler recordings use the source scope, job ID, submission time,
start time and an explicit restart counter. Structured Slurm queue responses
provide these fields without an additional controller request. On sites that
only expose the legacy text interface, Raven verifies sampled jobs through a
bounded rotation of exact detail requests. Selecting a job is not required.

A cached detail record can retain the previous graph, but cannot authorize a
new verified append after another queue publication. Samples awaiting a fresh
restart proof stay in a separate session-only recording. The chart's storage
status explains this condition. Raven preserves these files rather than
guessing which side of a same-second requeue produced an observation.

When the first verified observation proves a zero restart count and the same
submission, start and source identity, Raven promotes the matching initial
session recording without clearing its graph. Ambiguous positive-counter
promotions remain separate. A scheduler ID alone never authorizes a merge.

## Committed native recordings

Compact `.rvb` files contain independently checksummed, committed frames.
Interactive source workers batch queued rows until a sample-count or time
deadline; explicit flushes and shutdown drain pending rows. A bounded write
delay does not delay the in-memory graph.

Readers walk frame footers backward to return the newest verified window.
Coverage identifies whether older bytes were left unread. If the final append
is damaged, the writer first durably quarantines its suffix in a
`.damaged-OFFSET-ID` file, retains the verified prefix, appends an explicit gap
record and continues. Failed appends roll back to their initial file length.
A small, attempt-bound recovery descriptor retains the pending gap if that
first append fails or the process restarts; successful durable publication
removes the descriptor.

JSONL remains supported. Writers separate an existing torn final line from the
next record and reject nonfinite JSON numbers. Migration and retention skip
invalid records with an explicit ignored-record count; they preserve their
source files.

Light mode enforces its storage quota by evicting the oldest inactive, unpinned
attempt files. Active and pinned raw recordings remain protected. Quarantined
suffixes count toward the budget and retire before valid inactive recordings.
Operational events
rotate independently. After exact verification, a redundant inactive JSONL
recovery copy may leave the working quota while its compact file retains every
raw value. Conversion itself remains a copy-and-verify operation.

## Segmented job-side telemetry

New recordings publish `raven.telemetry-index/v2`. Existing v1 indexes and
expanded v1 measurement rows remain readable. The v2 index records invariant
row fields once, alongside device descriptors and an ordered segment inventory.
The active files remain `telemetry/cpu-memory.jsonl` and
`telemetry/gpu.jsonl`; sealed files use names such as
`telemetry/gpu.segment-00000001.jsonl`.

A compact GPU row can look like this:

```json
{"sequence":12,"timestamp":1792454400.125,"elapsed_seconds":60.2,"device":0,"utilization_percent":72.0}
```

`device` indexes that source's `devices` array. `row_defaults` supplies fields
such as the measurement schema, run ID, node, clock, scope and sampling interval.
The reader reconstructs the expanded measurement before validation or CSV
export. The UTC timestamp is derived from the finite epoch timestamp. The
[compact row schema](../schemas/telemetry-record.v2.schema.json) and
[index schema](../schemas/telemetry-index.v2.schema.json) define the format.

Each family rotates bounded segments. When its budget is reached, it retires
the oldest sealed segment and continues collecting the newest observations.
Coverage distinguishes lifetime `samples` from `retained_samples` and
`evicted_samples`. The default budget scales with the declared time limit,
device count and sampling interval, with a 32 MiB floor and 256 MiB ceiling per
family. An explicit storage budget overrides that estimate.

Temporary `ENOSPC`, `EDQUOT` and `EIO` errors preserve a usable writer and retry
with bounded backoff. This also applies when creating the next segment.
Publication synchronizes data, then the new index, then its directory. CPU and
memory observations remain usable while GPU recording is unavailable.

## Retention and exports

The default gap threshold follows observed cadence: 2.5 times the median
positive interval, with a 15-second minimum. Large recordings use a disk-backed
ordering spool, one time-bin aggregate and a bounded encoder batch rather than
materializing every sample in Python. Rollups preserve counts, extrema, units,
source-row provenance and exact decimal sums. They do not infer interpolation,
event order, quantiles or unobserved values.

Automatic export budgets scale with captured source size. A deadline or user
cancellation leaves the request pending and does not consume a failure attempt.
Captured watermarks hash the complete source prefix, including interior bytes.

Table, chart, clipboard and full-log exports default to
`$XDG_DATA_HOME/raven/exports`, or `~/.local/share/raven/exports`. Set
`RAVEN_EXPORT_DIR` to an absolute durable directory to choose another location.
These exports remain separate from ephemeral scheduler state. New export files
are private and created exclusively; a planted symlink or existing destination
is refused. The clipboard uses its own private, atomically replaced cache.

Plain passport hashes detect corruption. They do not authenticate an author
who can rewrite a document and recompute its hashes. Use Raven's signed-evidence
workflow when authenticity is required.
