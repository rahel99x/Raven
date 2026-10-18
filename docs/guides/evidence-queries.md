# Query retained evidence

Raven can build a local SQLite index of completed, immutable data exports. The
original export remains authoritative. The index is a rebuildable convenience
for repeated queries; it does not replace recordings or infer missing samples.

## Create an index

1. Export the selected run attempts with the existing **Export data** workflow.
   Include application measurements, CPU/memory, GPU, summaries or logs as needed.
2. In Raven Heavy, open the command palette and select **Query retained
   measurements** (`:ops evidence-query`). Choose **index**.
3. Select the completed export directory, its exact project path, and a new
   private index directory on the worker's local filesystem. Review the project,
   manifest digest and source byte count, then Apply.
4. Choose **query** and a named view: sources, attempts, application, CPU/memory,
   GPU, logs or transitions. The optional SELECT field permits bounded SQL.

Each complete capture commits in one transaction. Reapplying the same manifest
is a no-op. Interrupted ingestion rolls back; reopening the writer and retrying
the immutable capture recovers a hot rollback journal. A source checksum failure
leaves earlier commits intact. Rebuild by indexing the original exports into a
new directory. Keep the old index until its replacement has been verified.

Indexes are project-specific and local to the admitted worker. Raven does not
map a desktop path onto a remote filesystem or fall back to running an operation
on a login node. Light reports that analysis requires Heavy admission. The
standalone workstation Heavy profile remains available through the ordinary
explicit setup workflow.

## Read and interpret results

The installed read-only command uses the same admitted operation:

```bash
ravenh query --index /absolute/private/index --view sources
ravenh query --index /absolute/private/index \
  'SELECT source_id,attempt_key,metric,timestamp,value,value_text,unit FROM measurements ORDER BY timestamp LIMIT 50'
```

Join `measurements.source_id` to `sources.id`, and `sources.capture` to
`captures.digest`, to recover the original path, SHA-256, manifest and source
row. An attempt key binds project, host, run, attempt, cluster, job identifier,
submission token/time and script digest. Job numbers alone never join attempts.

`value_text` preserves the exact exported number. `value` is a binary64 SQL
number, or NULL when unavailable, outside binary64's range, or an integer cannot
be represented exactly (including decimal and exponent spellings of integers);
`numeric_status` explains that distinction. Units, scope, quality, status,
missing-data reasons and the complete CSV attributes remain available. NULL is
not zero. Two different exports may contain the same observation; they remain
separate captures and are not silently deduplicated.

Log rows retain the original source, line number and byte offset. A final state
in a summary is labelled `state-snapshot`, with no invented transition time.
Only explicit producer-declared transition events have a source timestamp.

For **rate**, **delta** or **window**, select an exact source ID, attempt key and
metric from a query. If the file contains several ranks, nodes or devices, supply
an explicit stream selector, for example `{"rank":"0"}`. Ambiguous streams and
unknown or mixed units are refused. A partial selection cannot become a complete
chart.

Rates use actual elapsed seconds. Counter resets, missing observations,
non-increasing timestamps, excessive gaps and nonfinite arithmetic produce an
unavailable value with a reason. Deltas use the same counter rules. Windows are
sample averages over `(t − window, t]`, with explicit gap barriers; they are not
time-weighted averages or interpolated observations. The result records source
rows, dimensions, units and the chosen semantics. Raven's existing expression
language is unchanged; expressions are not translated into SQL.

## Bounds, storage and export

Queries use read-only SQLite connections, disabled extension loading and an
authorizer that rejects writes, ATTACH, PRAGMAs and unapproved functions. Default
limits are 1,000 rows, 1 MiB output, one second and 200,000 VM steps. Hard limits
are 10,000 rows, 4 MiB output, five seconds and two million VM steps. SQL length,
column count, expression depth and individual values also have limits. A
truncated result says **partial** and identifies the exhausted budget. On Python
3.10, which lacks SQLite's per-connection limit API, queries run in a disposable
process with explicit memory, CPU and wall-time limits.

An index is limited to 64 MiB and 256 captures. Ingestion accepts at most 8 MiB
per source, 64 MiB of source data and 200,000 measurements per capture. Logs have
bounded UTF-8 lines. The admitted worker's smaller configured input/output,
memory and time limits also apply; these index ceilings do not grant extra
resources. A refusal should be addressed by exporting a smaller selection or
reviewing the normal worker resource settings, not by bypassing admission.

One kernel-backed owner writes the index. SQLite uses a rollback journal and
FULL synchronization; Raven does not enable WAL on shared filesystems or infer
safe locking from an NFS mount. Use a verified local filesystem. Choose **export**
to review a consistent SQLite backup into a new directory. The supported backup
API copies committed state, even when another reader is active, and a checksum
receipt accompanies the database. The backup retains references to the original
evidence; copy those exports too when independent source verification is needed.

## Performance and qualification

The existing export harness compares equivalent repeated CSV and indexed
time-window queries, verifies every returned value, and records build time,
query plans, disk growth, Python allocation peaks and separate process RSS:

```bash
python scripts/benchmark_data_export.py --index-queries --rows 1000 5000
```

Results describe the measured host, filesystem and warm-cache fixtures. Index
construction and duplicated provenance have costs; a one-off CSV read can be the
simpler choice. See the [implementation evidence](../implementation/review-2026-10/evidence/W23.md)
for measurements, exact source hashes, review findings and qualification limits.
