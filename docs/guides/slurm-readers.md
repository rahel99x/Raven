# Scheduler reads and historical identity

Raven uses Slurm's command-line tools. Each of `squeue`, `scontrol` and `sacct`
is checked separately for JSON support. A qualified data-parser schema enables
structured reads; an unsupported capability uses the conservative text reader.
**Sources** shows the selected reader, the fallback reason, parse coverage and
accounting windows. Malformed JSON and temporary scheduler errors preserve the
last trustworthy snapshot and show their actual error.

The recorded Slurm 26.05.5 `data_parser/v0.0.45` schema is qualified for these
reads. Other schemas use a visible text fallback until their exact fields have
been verified. Version numbers alone do not enable a parser. Unknown descriptive
JSON extensions are tolerated; unavailable required identity fields are not
invented. A text field with ambiguous separators stays unknown.

Capability checks are cached for an hour. A manual refresh invalidates that cache.
For diagnosis, select a reader for a single launch:

```bash
RAVEN_SLURM_FORMAT=legacy ravenl
RAVEN_SLURM_FORMAT=json ravenl
```

`auto` is the default. `json` requires a qualified structured reader and reports
unsupported capabilities as errors. `legacy` is a compatibility option; it
retains parser validation and historical identity checks. Recordings replay
only captured capabilities and commands, including older text-only sessions.

## Completed jobs and logs

A retained submission timestamp selects a four-day accounting window around
that date, even for a job completed years ago. Without a timestamp, the fallback
window is the last 14 days. Job number, submission, owner, start and cluster
are checked against the selected record where available. Reused IDs, duplicate
attempts and conflicting timestamps cannot select another job's logs.

For an older record without a retained submission timestamp, use the command
bar to request a specific interval:

```text
historyrange 2026-01-01 2026-02-01
```

This setting is persisted. The range must be ordered and span at most 3,650 days.
`historyrange all` clears the explicit override; missing-identity lookups then
return to the 14-day fallback. It does not scan from epoch zero. Sources displays
the effective lookup bounds and command count.

An explicit range can resolve a unique record for the selected owner and ID.
If multiple attempts match, Raven reports ambiguity. Missing or delayed Slurm
accounting remains unknown and can be retried. Mixed timezone-aware and naive
timestamps are not silently treated as equal. These changes do not delete or
rewrite retained history.

Structured responses contain many more fields than Raven's selected legacy
columns. In the recorded 500-job benchmark, the JSON response was 2.90 MB versus
0.11 MB, and local parsing took about 87–90 ms versus 3 ms. These figures are
host/fixture specific. Large queues or constrained connections can select
`RAVEN_SLURM_FORMAT=legacy` while retaining the checked parser and identity
validation. Both paths remain bounded; missing or ambiguous evidence is never
silently treated as a verified empty queue.
