# Detect changes and compare recorded phases

Raven provides experimental, read-only analyses of timestamped measurements.
A detected change is evidence to investigate, not a diagnosis. Statistical
segments need not correspond to scientific phases: the included numerical
reporting example has one declared solve phase but produces two statistical
segments as it converges. Deployment accuracy remains unqualified.

## Use existing recordings

1. Export the intended run attempts and [index their evidence](evidence-queries.md).
2. Query `source_id`, `attempt_key`, `metric` and `unit` from `measurements`.
   Select one source and one attempt. For multiple ranks, nodes or GPU devices,
   include the exact stream dimensions rather than combining them implicitly.
3. From the checkout, convert the selected stream on the machine holding the
   private index. This utility reads the index without launching a job:

   ```bash
   python scripts/prepare_signal_series.py /absolute/private/index \
     --source-id 1 --attempt EXACT_ATTEMPT_KEY --metric rss_bytes \
     --selector '{"node":"node01"}' > measurements.json
   ```

   Replace the example identifiers with the query results. Omit `--selector`
   for a source with one stream. The output preserves capture/source digests,
   original CSV rows, exact attempt identity and missing values. It refuses
   ambiguous streams, unknown units and more than 4,096 observations. It does
   not average ranks or downsample silently. `rss_bytes`/`bytes` becomes
   `rss`/`B`; the original metric and unit remain in source provenance.
4. In Raven Heavy, open **Telemetry changes (experimental)** from Research
   Operations (`:ops telemetry-anomalies`). Select the JSON file and inspect.
   The operation follows the existing admitted worker, cancellation and result
   receipt path. Files must be accessible to that worker. Light does not start
   an unmanaged analysis on a cluster login node.

Live CPU/memory and available GPU samples also feed a small monitor after
Raven verifies the current attempt. Experimental changes appear as dismissible
mild notifications with their evidence window. No new toolbar button is added.
Unknown attempts and callbacks from old attempts cannot create current alerts.
A restart or an evicted stream begins a new warmup; original recordings remain
available for repeatable offline analysis. Alerts and detector state are bounded
session observations, not a durable scientific finding store.

## Measurement format

Use one explicit series per metric and attempt. Times are increasing finite
seconds in one declared recording clock; values are finite numbers or `null`.
Do not turn missing measurements into zero or reuse a job number as an attempt.

```json
{
  "schema": "raven.measurement-series/v1",
  "series": [{
    "identity": {
      "scope": "cluster-and-project-or-capture-identity",
      "attempt": "immutable-submission-and-restart-identity",
      "job": "1170",
      "verified": true
    },
    "metric": "rss",
    "unit": "B",
    "limit": 17179869184,
    "samples": [
      {"t": 1.0, "value": 104857600, "phase": "solve"},
      {"t": 2.0, "value": null, "phase": "solve"}
    ]
  }]
}
```

`verified` in a supplied analysis file is the producer's identity assertion. It
never grants scheduler authority or establishes scientific ground truth. The
converter checks that identity against the indexed immutable export. The live
monitor uses Raven's independently verified scheduler attempt identity.

`limit` is optional and must refer to the same measured memory scope in bytes;
for example, do not compare one rank's RSS with an entire multi-node allocation
limit. The live monitor does not infer that equivalence from requested memory.
A memory crossing estimate requires metric `rss`, unit `B`, a positive limit,
and a stable positive slope. Results give an interval of remaining seconds,
bytes-per-second slope, the observation time, and a horizon no longer than the
observed trend or one hour. The residual sensitivity envelope is **not a
calibrated confidence interval**. Missing samples or phase changes invalidate
that extrapolation.

The robust detector uses 12 warmup samples, at most 32 retained observations,
median/MAD variation and three confirming deviations. The unit-dependent noise
floor avoids division by zero. RSS also uses a bounded positive-trend check.
Missing observations, declared phase changes, gaps over 120 seconds, uneven
sampling, noisy baselines and constant signals can suppress an alert or restart
warmup. Alerts say what changed and include the measured window; they do not
claim a leak, hardware fault or application cause.

## Compare two attempts

Create a comparison document containing the two measurement documents:

```json
{
  "schema": "raven.phase-comparison/v1",
  "reference": {"schema": "raven.measurement-series/v1", "series": []},
  "candidate": {"schema": "raven.measurement-series/v1", "series": []},
  "metrics": ["rss"],
  "max_gap": 120,
  "minimum": 8,
  "penalty": 4
}
```

The empty arrays above mark where to insert the actual series; they are not a
runnable fixture. A complete example is generated by
`python examples/signal-analysis/make_example.py > comparison.json`.
Open **Compare observed phases (experimental)** (`:ops phase-alignment`) and
select the comparison file. **Original** shows original-time segment ranges;
**Aligned** shows possible corresponding phases, gaps and uncertain matches.
Switching the view does not change timestamps, values or source files. Results
retain source digests, original sample indices and original times.

Both attempts must use the same explicit units for 1–8 selected metrics.
Normalization uses the reference median and scale for both attempts. Features
must have observations at the same exact timestamps within each attempt;
missing values split observation blocks. No interpolation fills those gaps.
Bounded binary segmentation precedes phase matching. Matching may leave gaps;
only compatible phase summaries enter windowed, downsampled DTW. Constant
signals do not establish temporal correspondence. Phase labels remain
scientifically unverified even when a numerical match is found.

## Limits and qualification

Offline inputs accept at most 16 series and 4,096 samples per series. Alignment
allows at most 8 features, 32 phases, two million segmentation work units and
200,000 DTW cells. Exceeding a bound produces a visible request to select a
smaller window. Live monitoring retains at most 128 streams and 64 episodes;
least recently used streams are evicted. The dashboard paints cached alerts
without rerunning detection, reading files or polling Slurm.

The reproducible synthetic evaluation records injected steps, increasing
memory, stalls, noisy/stationary controls, known phase boundaries, time scaling,
false correspondences and fixed-window/simple-time baselines. Real scientific
phase labels, site-specific false-positive rates and useful held-out deployment
performance still require an authorized representative corpus. These features
remain experimental until those gates are satisfied.
