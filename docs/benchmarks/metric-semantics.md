# Metric semantics: reader and render qualification

The baseline is `b52cd93e2b7581eeff6e9f601ad13765f4618a0c`. The
[measurement receipt](metric-semantics.json) identifies the measured implementation
files by SHA-256 and records the focused regression command: 551 tests passed in
15.45 seconds. These hashes identify working-tree bytes, not a later integrated
commit. Performance results are diagnostic observations, not acceptance thresholds.

## Reader method and results

Each variant ran sequentially in a separate process on the same Linux host,
without competing heavy tests. The local temporary JSONL fixture contains 64
series with 600 observations each. One complete read warms the reader before 60
unchanged-source snapshots are timed. The metadata variant declares all 64 series
as residuals and includes bounded scientific insight preparation. Every variant
retains all 38,400 observations.

The [portable harness](../../scripts/benchmark_metric_semantics.py) accepts an
explicit source checkout and mode. Recorded results used an equivalent fixed
fixture precursor; its hash and the harness differences are recorded in the
receipt. The published CLI adds argument parsing, fidelity checks, and provenance
fields without changing the fixture or timed read loop.

| Variant | Median read (ms) | p95 read (ms) | Total process CPU, 60 reads (ms) | Peak process RSS (KiB) |
| --- | ---: | ---: | ---: | ---: |
| Baseline, legacy stream | 5.866 | 7.662 | 372.452 | 37,444 |
| Implementation, legacy stream | 5.943 | 7.260 | 365.632 | 37,496 |
| Implementation, residual metadata | 8.565 | 10.622 | 522.653 | 38,464 |

The legacy median increases by 0.076 ms, approximately 1.3%. Preparing metadata
and residual insights adds 2.622 ms to the implementation's legacy median on this
fixture. These measurements establish no speedup or universal overhead bound.
The p95 is the nearest-rank percentile. Process RSS is the whole-process high-water
mark, including imports, fixture construction, and native memory; Python allocation
peaks were not measured.

The read path remains outside rendering. Confined reads carry the selected stream,
index, and metadata through one existing SSH request; focused tests verify that
boundary. This benchmark measures local filesystem work and does not measure SSH
latency. Rank selection reads one stream; it does not aggregate or read every rank.

## Supplemental rendering result

The existing UI harness measures cached Research frames at 80 columns by 24 rows,
with 64 series, 600 points per series, and 30 repetitions. See the raw
[baseline](metric-semantics-render-before.json) and
[implementation](metric-semantics-render-after.json) reports.

| Variant | Median frame (ms) | p95 frame (ms) |
| --- | ---: | ---: |
| Baseline | 3.459 | 4.357 |
| Implementation | 3.543 | 4.324 |

Both runs attempt zero I/O during measured frames and request one visible chart
card without preparing a new raster per frame. The counting paint sink excludes
terminal transport and waiting. These supplemental reports were collected before
the final correction for rank fallback when a canonical stream is empty. Their
fixture contains no index or metadata, but the reports do not identify the final
integrated revision.

## Reproduce

From the implementation checkout, choose an unused directory for the baseline.
Run commands sequentially while heavy tests and other benchmarks are idle:

```bash
git worktree add --detach /tmp/raven-metric-baseline b52cd93e2b7581eeff6e9f601ad13765f4618a0c
.venv/bin/python scripts/benchmark_metric_semantics.py /tmp/raven-metric-baseline legacy --output /tmp/metric-before.json
.venv/bin/python scripts/benchmark_metric_semantics.py . legacy --output /tmp/metric-after.json
.venv/bin/python scripts/benchmark_metric_semantics.py . sidecar --output /tmp/metric-kinds.json

.venv/bin/python scripts/benchmark_ui.py --source-root /tmp/raven-metric-baseline \
  --scenario research --gesture render --metrics 64 --points 600 \
  --repeats 30 --width 80 --height 24 --label before --output /tmp/render-before.json
.venv/bin/python scripts/benchmark_ui.py --source-root . \
  --scenario research --gesture render --metrics 64 --points 600 \
  --repeats 30 --width 80 --height 24 --label after --output /tmp/render-after.json
```

Process scheduling and Python garbage collection affect small same-host samples;
repeat measurements before drawing deployment conclusions. These synthetic local
fixtures do not qualify live Slurm, a remote cluster, GPU behavior, scheduler load,
or network filesystem performance. Isolated SSH-helper regression tests use local
subprocesses. Correctness checks cover rank isolation, ownership, malformed or
unsafe metadata, explicit selection, and the actual local launch path separately
from these timing measurements.
