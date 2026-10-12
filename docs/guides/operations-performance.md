# Operations release: terminal performance checks

[Performance guide](ui-performance.md) · [Scale operations](operations-scale.md)

Tower 4.15.0 uses an immutable operation-definition cache for contextual
buttons, forms, and the catalog. The cache is built at application startup.
Rendering no longer copies all field definitions for each visible button.
This also fixes a regression in frozen Jobs views, where the new buttons
previously caused a deep copy during every rendered frame.

The inline Jobs Analytics document also skips operation buttons that its
Details adapter would immediately discard. Standalone Analytics retains all
three operation buttons. The visible inline document, controls, and plot count
are unchanged.

The action-review size check is cached by plan identity, digest, and terminal
width. Moving the pointer over a large review no longer scans up to 8,192 lines
each frame. A resize or replacement plan recalculates the bound. Apply remains
disabled when the review exceeds its display limit.

## Comparison method

The comparison used the existing `scripts/benchmark_changing_ui.py` harness:

- Baseline: 4.14.0 commit `324f6f9`, extracted with `git archive`.
- Candidate: the 4.15.0 operations implementation, including the cache fixes.
- Python 3.12.14 with `-S`; Linux x86-64, glibc 2.41.
- 1,000 jobs, 3,000 historical jobs, 4,000 points per source kind, four native
  GPU devices, four trace devices, and a 320 by 52 terminal.
- Six scenarios, both character sets, and 100 frames per scenario: 1,200
  measured frames for each revision. The revisions ran sequentially.
- Deterministic pointer events at 50 Hz, live redraws at 10 Hz, and source
  publication every 500 ms. Elapsed time uses `perf_counter`.

## Results

All twelve candidate scenarios recorded **zero file, scheduler, or worker I/O
attempts during measured frames** and **zero graph geometry failures**. The
baseline and candidate had identical per-frame work counts, including snapshots,
document rebuilds, graph publications, chart rasters, and painted rows. The
number of visible plots also matched in every scenario.

Times below are milliseconds. Each pair is **baseline / candidate**.

| Scenario | Characters | Median frame | p95 frame |
| --- | --- | --- | --- |
| Static plots | Unicode | 1.88 / 1.78 | 76.93 / 102.55 |
| Static plots | ASCII | 2.77 / 1.56 | 123.22 / 46.80 |
| Live 30-second window | Unicode | 3.29 / 2.15 | 145.63 / 74.13 |
| Live 30-second window | ASCII | 1.96 / 1.69 | 107.13 / 78.80 |
| Live 1-second window | Unicode | 1.92 / 1.81 | 81.00 / 50.03 |
| Live 1-second window | ASCII | 1.76 / 1.65 | 47.02 / 70.01 |
| Job selection changes | Unicode | 2.14 / 1.86 | 90.07 / 91.72 |
| Job selection changes | ASCII | 1.80 / 1.76 | 92.79 / 90.83 |
| Terminal resize | Unicode | 2.33 / 1.66 | 100.69 / 62.07 |
| Terminal resize | ASCII | 2.57 / 1.67 | 99.12 / 66.82 |
| Scroll to either end | Unicode | 2.65 / 1.85 | 101.17 / 77.51 |
| Scroll to either end | ASCII | 1.77 / 1.78 | 72.60 / 72.95 |

Candidate median frames ranged from 1.56 to 2.15 ms. In this initial comparison,
the largest recorded frame was 286.11 ms, compared with 472.85 ms in the baseline
run. Full document rebuilds still have substantially higher latency than
pointer-feedback frames.
Two candidate p95 values increased substantially despite identical work
counts. A targeted repeat below checks these two differences. These measurements
include shared-host scheduling variation. They establish neither a uniform
speedup nor a hard latency ceiling.

The two scenarios were then repeated sequentially, with other validation work
paused and **300 frames per revision and scenario**:

| Targeted repeat | Median baseline / candidate | p95 baseline / candidate | Maximum baseline / candidate |
| --- | --- | --- | --- |
| Static Unicode | 1.99 / 1.77 | 75.07 / 76.32 | 263.96 / 204.58 |
| Live 1-second ASCII | 1.63 / 1.94 | 58.10 / 71.01 | 166.92 / 163.80 |

The static p95 difference decreased to about 2%. The live 1-second ASCII case
remained slower: about 22% at p95 and 19% at the median. Its work counts stayed
identical and its maximum frame time was similar. The repeat did not identify
additional rendering work that explains this timing difference; it remains a
measured limitation, not a claimed optimization. All repeated scenarios again
recorded zero I/O attempts and zero geometry failures. The two runs together
measured 1,800 frames per revision.

## CPU and call-profile investigation

After removing the hidden inline buttons, a separate investigation measured
the live 1-second ASCII scenario in **baseline, candidate, candidate, baseline**
order. Each process ran 300 frames. The harness recorded `process_time` beside
wall time, and `cProfile` recorded frame 4 in the first two processes. The table
excludes frame 4 from all four processes so that the profiler's cost does not
affect the comparison. These instrumented results are separate from the tables
above.

| Process order | Median CPU ms | p95 CPU ms | Total CPU ms, 299 frames | Total wall ms |
| --- | --- | --- | --- | --- |
| Baseline A | 2.02 | 65.12 | 4,244.93 | 4,247.00 |
| Candidate A | 2.04 | 69.71 | 3,981.94 | 3,982.88 |
| Candidate B | 1.98 | 87.70 | 5,276.75 | 5,277.76 |
| Baseline B | 2.19 | 72.49 | 4,161.56 | 4,163.01 |

The first candidate consumed less total CPU than its baseline; the second
consumed more and contained a 668 ms outlier. CPU time closely followed wall
time. This excludes scheduler waiting as the main explanation, but it does
not distinguish CPU frequency, allocator behavior, and automatic garbage
collection. The I/O guard remained active, and there was no worker I/O. Process
CPU time includes threads, although this fixture did not admit background work.

The sampled frame contained **600,028 baseline calls and 599,392 candidate
calls**. Both made two navigation-button rendering calls. The candidate made no
hidden operation-button calls or operation-definition copies. Its additional
Operations tick and inactive-overlay checks took approximately **4.3 µs total**
under profiling. Thus the new operation controls do not account for a
multi-millisecond frame increase in the sampled path.

The main sampled cost remained in pre-existing code:

- `native_series_cache.owned_snapshot`: 28,008 calls, about 112 ms baseline and
  120 ms candidate cumulative time under the profiler.
- Native metric normalization, GPU value extraction, and chart rasterization.
- Job-group input processing across 4,000 queue and history rows.

Profiled absolute times include instrumentation overhead. The call counts and
paths identify work; they are not normal terminal-frame timings. This review
removed the two introduced sources of unnecessary work. Remaining tail latency
is concentrated in existing full-snapshot and rendering work, with variable
CPU cost. The release does not claim that this older workload is now free of
lag, or that the observed timing difference has a single proven machine cause.

The harness uses a counting paint sink. It does not measure terminal transport,
SSH latency, real Slurm responses, external profiler processing, or hardware
GPU collection. Repeat the same test on the deployment host before treating
these numbers as a target for that system.

## Reproduce the comparison

Use the recorded 4.15.0 implementation as the candidate when reproducing these
results. For a new release comparison, record that candidate's revision
separately. Extract the baseline into a separate directory and run the same
harness and arguments once for each source directory:

```sh
python3 -S scripts/benchmark_changing_ui.py \
  --source-root /path/to/source \
  --output /tmp/tower-benchmark.json \
  --cases static,live30,live1,switch,resize,scroll-end \
  --glyphs both --repeats 100
```

`tests/test_ops_render_budget.py` checks immutable metadata, repeated render
reuse, review-size invalidation, and preservation of standalone controls while
skipping invisible inline controls. `tests/test_qol_performance_review.py`
checks frozen-frame snapshot reuse and prohibits file or process reads during
rendering. These structural checks do not depend on benchmark timing.
