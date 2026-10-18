# Recording storage and retention

Raven keeps raw native CPU, memory and GPU recordings under the selected local state directory. Every new recording filename includes the exact job identifier and a connection/attempt identity digest. Verified submit/start/restart changes start a separate recording. Until sufficient scheduler identity is available, observations remain isolated to their current session and are labelled unverified; conversion cannot promote them to verified evidence. Project scientific metrics and external trace files remain separate authoritative inputs.

After controller purge, reopening a completed native recording requires qualified
accounting evidence for the same submit/start/restart identity. A legacy response
without the restart counter remains explicitly unverified; Raven preserves the
archive and reports why it cannot attach it. A scientific run's attempt number is
not a Slurm restart counter. It must not be substituted to force an attachment.

JSONL is the default. **Setup → Installation and diagnostics → Recording storage** offers conversion preview, format selection and retention inspection. These actions use the existing background task lane and a named local maintenance child. The child has 256 MiB address space, 15 seconds CPU, 20 seconds wall time and bounded input/output. It inherits only the exact state-lock descriptor; shutdown cancels and reaps it before state ownership is released. A remote Heavy worker is unnecessary for this local state operation. Stop other Raven writers before using the equivalent CLI mutation commands.

## Lossless compact blocks

Set `storage.series_format` to `"blocks-v1"` in a managed configuration, or use the storage page. The change applies to new recording streams immediately. Existing JSONL streams keep their format until an explicit conversion: JSONL is copied, decoded and verified before a new `.rvb` file is published. The original remains unchanged. Once a compact counterpart exists it stays canonical, including after selecting JSONL again; an older preserved original must not replace newer samples.

```bash
raven series inspect /path/to/state/series/v2-...jsonl
raven series migrate /path/to/state/series/v2-...jsonl
raven series migrate /path/to/state/series/v2-...jsonl --apply
raven series export /path/to/state/series/v2-...rvb --output /new/path/raw.jsonl
```

Use the actual filename from your state directory. Inspection and preview are read-only; apply requires exclusive ownership of that state directory. Export requires a new destination. Legacy files require `--allow-unattributed` for conversion and remain explicitly unattributed. Conversion cannot establish a missing execution identity.

The version-1 block header uses little-endian integers, a filename identity digest, decoded/encoded lengths, row count and SHA-256. Its payload is a zlib-compressed JSON array of object samples. A checksummed commit footer closes each append. Finite binary64 values round-trip exactly through JSON's round-trip representation, including signed zero; integers retain their exact integer representation. Epoch-second timestamps, key order, missing fields and null fields are preserved. There is no executable deserialization. Limits apply to block size, nesting, structural slots, row counts, encoded input and cumulative decoded bytes. Decompression allocates memory: this format is not zero-copy.

A damaged append exposes its verified prefix with a coverage error. Readers stop at the first invalid block; they never skip corruption. Further append is refused if the final commit is incomplete. Preserve the damaged file and its JSONL original. Use `raven series recover DAMAGED.rvb --output NEW.jsonl` for an explicit verified-prefix copy. Its result reports `complete=false` and the actual read error when coverage is partial. Ordinary export intentionally fails on incomplete input. Keep the damaged original and the recovery status as evidence. Never relabel a recovery file as another attempt.

Compact storage is opt-in because small synchronous appends can cost more CPU and bytes than JSONL; batching and compression benefits depend on the recording workload. In the controlled 8,192-record fixture, bulk conversion reduced bytes by 71.9% but increased encoding time by 2.66 times. Individual committed blocks were larger and slower than matched JSONL appends, so JSONL remains the recommended default for new live recordings. See the [measured storage evidence](../implementation/review-2026-10/evidence/W24.md) for the fixture, costs and limits. Raw export and reopen retain the same values. Indexed chart envelopes preserve original extrema and ordering; gaps, unknown observations, duplicate timestamps and interpolated window edges continue to use the established renderer. Corrected interior values invalidate cached results. No cache signature fallback has been removed.

## Explicit retention working copies

Retention creates a separate `.rvt` working archive. It never deletes, truncates or rewrites the original native or scientific recording. Raw protection is enabled by default. This is intentional: Raven cannot prove that an external scientific receipt or exported figure no longer references an original file.

The managed `storage.retention` settings are:

```json
{
  "raw_seconds": 86400,
  "ten_second_seconds": 604800,
  "minute_seconds": 2592000,
  "gap_seconds": 2.0,
  "protect_raw": true
}
```

Ages are relative to the captured `as_of` epoch time and must satisfy `raw_seconds ≤ ten_second_seconds ≤ minute_seconds`. With raw protection enabled, the working archive retains all admitted raw samples. With protection explicitly disabled, recent observations remain raw, older observations use 10-second bins, then 60-second bins; older observations expire from the working copy. The original still retains every observation. Tier boundaries are disjoint, and edge bins are clipped to their own tier interval.

```bash
# Preview the proposed working copy, preserving originals.
raven series retention /path/to/state/series/v2-...jsonl

# Explicitly permit tiering in a new working copy.
raven series retention /path/to/state/series/v2-...jsonl \
  --raw-seconds 86400 --ten-second-seconds 604800 \
  --minute-seconds 2592000 --unprotect-working-copy --apply

raven series query /path/to/state/series/v2-...rvt --limit 100
raven series pin /path/to/state/series/v2-...rvt

# Required before reducing a previously pinned working copy.
raven series pin /path/to/state/series/v2-...rvt --unpin
```

The Setup page previews and publishes the configured policy and opens the tagged tier results. CLI `--as-of`, `--start` and `--end` accept epoch seconds. Query intervals are half-open. A query crossing a rollup boundary returns the entire intersecting rollup with `boundary_partial=true`; it cannot claim an exact answer for a smaller interval.

Each rollup declares its sample count, time interval, first/last observed times, source rows, metric counts, missing counts, minima/maxima and their source rows, exact decimal sums and units. CPU ratios remain fractions; RSS is bytes; GPU utilization is percent and GPU memory is MiB. Unknown units remain null. Aggregates are sample-weighted, not time-weighted; explicit observation gaps exceed the configured gap threshold. No interval is filled by interpolation. The archive preserves the source path, attempt digest and canonical raw-value digest.

Rollups cannot reconstruct sample ordering within a bin, quantiles, covariance, causal relationships, rates from counters, or values during missing intervals. They never enter native raw graphs or raw export paths. Queries display their fidelity explicitly. Late corrections rebuild a complete bounded generation from the authoritative raw source; aggregates are not patched by guessing missing subtraction information.

Compaction checks cancellation, input and output budgets, checksums, source stability and quotas. A staged file is verified before one atomic replacement publishes the complete generation. Restart before replacement retains the previous generation; restart afterward sees the complete new generation. Disk-full, unsupported identity, ambiguous fields and corruption produce visible errors. The initial working budget is 50,000 source records and 32 MiB encoded input/output, with a separate cumulative decompression bound. Narrow a larger source explicitly rather than assuming partial compaction is complete.

A smaller working archive does not imply lower total disk use while protected originals remain. Raven reports original bytes and additional archive bytes separately. Raw garbage collection requires a future reference-aware policy; this implementation does not infer permission to erase evidence from missing references.

## Updating and downgrading

Stop existing Raven sessions, update the checkout and reinstall the package in its existing environment. No state-directory deletion is needed. Keep JSONL originals, compact recordings and retention files when changing versions. Older Raven builds do not understand compact/tier formats; export raw compact data to a new JSONL destination before using it with an older tool. Never run old and new writers concurrently on the same state directory.
