# Query retained trace summaries

Trace summaries are an experimental, bounded view of verified recorded intervals.
They preserve counts, sums, extrema and the largest 32 durations exactly within
the matched retained prefix. Quantiles report deterministic duration intervals;
they do not certify event order, causality or complete execution coverage.

```bash
raven trace summary create /path/to/manifest.json --output summary.json
raven trace summary query summary.json count
raven trace summary query summary.json quantile --quantile 0.95
raven trace summary query summary.json top-k --k 10 --phase train --rank 0
raven trace summary merge rank0.json rank1.json --output merged.json
raven trace summary delete merged.json --digest SOURCE_SHA256 --output reduced.json
```

Output files are private and published exclusively: choose a new name when
repeating a command. Originals and trace journals are unchanged. Source digests,
exact stream identities, retained sequence/time bounds, recording coverage and
unmatched events travel with each summary. Digests identify evidence; importing
a summary does not authenticate its producer or independently reread its journal.

In the existing Trace panel, select **Summaries** or use `:trace summary`.
`Events` or `:trace events` restores the retained event view. Computation uses
the same background subscription and selected attempt; rendering performs no
file reads. Phase labels come from recorded phase/name/function fields. Rank is
emitter-declared on opening events and remains unknown when absent.

Every query reports `exact`, `approximate`, or `unanswerable` before its result.
Exactness concerns matched observed intervals, never omitted prefixes or unknown
frame boundaries. Wall durations are inclusive and may overlap; their sum is
not CPU utilization. Empty/missing evidence is unanswerable, not a measured zero.

Quantiles use nearest-rank selection over deterministic integer bins retaining
seven significant bits. The actual nearest-rank duration lies in the returned
`[lower, upper]` nanosecond interval. Bin width is less than 1/64 of a positive
value; values below 128 ns are exact. This is a value-error contract, not an
empirical or universal rank-error bound. Merging identical bins preserves it.

Merge refuses overlapping ranges from the same exact stream, incompatible
versions or exceeded bounds. At most 16 retained source contributions, 64 total
phase/rank groups, 8192 bins and 4 MiB are accepted. Deletion removes complete
source contributions by digest and recomputes queries from the remainder.

`--window START_NS END_NS` selects complete retained source windows in one
node/boot monotonic clock domain. A window cutting a retained source is
unanswerable because the summary did not preserve individual event times.
Cross-node time windows are also unanswerable. Consult the original recording
when a query needs detail outside this contract.

Raw journals have their existing, independent retention policy. Summarization
never deletes them. Users may retain only a summary after explicitly archiving
or removing their original recordings, accepting that omitted event queries
then cannot be recovered. Summary files persist until explicitly removed.
