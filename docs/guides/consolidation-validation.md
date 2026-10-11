# Tower 6.1.0 validation record

Recorded on 11 October 2026 on a shared Linux cloud host. The changes consolidate
the mechanisms described in the [6.1 guide](consolidation.md). The
[capability map](../reference/capabilities.md) keeps all 56 proposal entries and
their supported scope. Ownership consolidation does not close external site,
hardware, scientific-evaluation, or user-study gates.

## Release checks

The complete regression suite passed **13,980 tests on Python 3.12.14** in
354.39 seconds, with seven explicit skips: one optional Dask integration,
one unavailable ShellCheck executable, and five MPI checks that require missing
development headers/libraries. No tests failed. These skips are not counted as
passes or as target-system qualification.

The selected 19-module compatibility set passed **614 tests on Python 3.10.21**
and **614 tests on Python 3.14.7**, with no skips. These cover the new shared
mechanisms, remote readers, job panes and cached rows. A timeout-boundary fixture
was corrected to use `concurrent.futures.TimeoutError` on Python 3.10; the runtime
already handled that exception. All 13 derived-result tests then passed again on
Python 3.12.14. These overlapping sets are not added to the full-suite count.

All **49 schemas** and **35 published examples** passed validation. Generated
operation and capability references passed freshness checks. The built wheel
passed all **eight local qualification categories**, including documentation,
all ten terminal pages, the real-PTY check, command-line entry points and a clean
offline installation outside the checkout. Its **202 Python modules** matched
the source files byte for byte. The qualification report leaves target-system
and external-evaluation categories explicitly untested.

## Regression scope

The regression checks cover these contracts:

- An unchanged owned metric source reuses its publication. Interior corrections
  invalidate the changed record. Foreign mutable values retain exact checking
  or an uncached reader, without invoking extension copy or reducer hooks.
- Source and prepared caches admit up to 128 entries, each under its existing
  24 MiB retained-source accounting budget. Nine small Advisor histories no
  longer evict one another on each frame. Byte eviction still applies to larger
  inputs, and existing readers remain valid after eviction.
- Jobs and History Details retain isolated selection, zoom, job-attempt and
  control geometry. An offscreen heatmap does not materialize its metric vectors
  after new data arrives. Scrolling it back restores its rows and controls.
- Native table rendering copies visible cached rows. Existing instance, class,
  and subclass `job_rows` overrides retain their previous call signature.
- Pointer captures keep a single owner through source refresh, selection,
  resize, menus, scrolling, cancellation and delayed release. The raw terminal
  decoder remains in place.
- Concurrent governed reads share eligible work on existing workers. Fresh
  action validation cannot join an earlier observation. Invalidated producers
  retain their source reservation until they drain.
- Investigations preserve exact-attempt identity, source revisions, committed
  trace prefixes, coverage gaps and missing evidence. Pure analysis reuse does
  not replace source verification. Diagnostic execution still requires review.
- Trial budgets are checked after preparation and before child launch. Process
  cancellation remains effective after a child closes its output pipes.
  Accepted action outcomes retain receipts even if cancellation arrives later.
- Isolated SSH readers contain their complete standard-library-only module
  bundle. They retain remote identity, confinement and transfer limits.

These checks include real local subprocesses, isolated imports, local sockets,
temporary files, thread synchronization and terminal pseudo-devices. Scheduler,
GPU and remote-host fixtures do not constitute a production cluster test.

## Performance interpretation

The changing-UI fixture uses 1,000 active jobs, 3,000 history jobs, 4,000 points
per source kind, four native GPU devices and four trace devices. Each case has
100 measured frames at a 320-column by 52-row viewport. Observations publish
every 500 ms. Unicode and ASCII runs use the same data. Baseline source is
commit `dd97ec4cae5c061e10df3a579c3eaaf8776ac4cd`.

Times measure elapsed frame work with a counting paint sink. They exclude
terminal transport and external producer latency. The shared host can interrupt
any measured phase; these percentiles are observations, not deadlines.

An earlier run made alongside other checks showed a 1,522 ms one-second Live
frame. It included about 985 ms for only two paint calls. Two isolated pairs,
including a reverse-order pair, did not reproduce that slowdown. Their
one-second Live Unicode p95 was 44–56 ms after the change, compared with 72–104
ms before it. The exact cause of the earlier outlier is not proven by elapsed
timing alone. Do not infer that all rendering spikes have been eliminated.

The final sequential comparison ran after source changes were frozen and other
test workloads had stopped. All twelve cases improved median and p95 frame
time; p95 reductions ranged from 10.8% to 61.0%. Times below are milliseconds.

| Case | Glyphs | Frame median before → after | Frame p95 before → after | Rebuilding-frame median before → after |
| --- | --- | ---: | ---: | ---: |
| Full history | Unicode | 3.31 → 1.86 | 115.36 → 48.86 | 131.77 → 56.83 |
| Full history | ASCII | 2.92 → 1.53 | 83.90 → 50.28 | 85.56 → 50.43 |
| Live, 30 s | Unicode | 3.20 → 1.82 | 108.01 → 54.95 | 128.13 → 56.60 |
| Live, 30 s | ASCII | 1.79 → 1.64 | 65.00 → 44.72 | 59.69 → 43.64 |
| Live, 1 s | Unicode | 1.92 → 1.77 | 56.52 → 48.58 | 50.04 → 41.14 |
| Live, 1 s | ASCII | 1.75 → 1.69 | 70.72 → 40.22 | 48.83 → 36.59 |
| Switch jobs | Unicode | 3.30 → 2.01 | 139.20 → 54.23 | 139.20 → 54.23 |
| Switch jobs | ASCII | 1.93 → 1.57 | 120.55 → 60.60 | 82.09 → 44.00 |
| Resize | Unicode | 3.81 → 1.82 | 127.99 → 52.82 | 137.04 → 63.08 |
| Resize | ASCII | 1.78 → 1.70 | 83.49 → 48.31 | 70.75 → 47.05 |
| Jump to end | Unicode | 2.24 → 2.07 | 71.15 → 63.49 | 71.15 → 63.49 |
| Jump to end | ASCII | 1.94 → 1.80 | 65.30 → 47.82 | 51.14 → 43.64 |

The rebuilding-frame column measures the complete frames that rebuilt their
document. The other columns also include the cheaper pointer-feedback frames.
Per-frame work counts matched, with no unexpected I/O or geometry failures.
Job-switch frames still reached 301 ms in Unicode and 262 ms in ASCII. The
ASCII maximum exceeded its baseline maximum of 249 ms. A lower p95 does not
establish that each individual frame became faster.

The terminal stress harness uses a real curses process and 50, 250, or 1,000
mouse reports per second. It crosses live graphs and changes worker mode
through the toolbar from Single to Multi and back. Motion coalescing makes the
latest dispatched position younger than some decoded reports. The benchmark
therefore records both measures, together with painted pointer age.

All three final four-second stress runs retained the selected job, showed six
plots, completed both worker-mode changes and exited with **zero unexpected tab
changes**.

| Mouse reports/s | Latest dispatched input p99 | Painted pointer age p99 | Maximum painted age |
| --- | ---: | ---: | ---: |
| 50 | 16.36 ms | 28.74 ms | 90.36 ms |
| 250 | 3.34 ms | 7.02 ms | 50.57 ms |
| 1,000 | 1.01 ms | 5.74 ms | 53.86 ms |

The [benchmark record](../benchmarks/6.1.0-local.json) includes the comparison,
separate decoded-report latency, worker transitions, earlier Live observations,
source digest, and isolated chart-kernel measurements. The kernel measurements
are separate experiments, not whole-application latency claims.

## Reproduce the checks

From a development installation, run:

```bash
.venv/bin/python -m pytest -q -ra
python3 scripts/generate_operation_reference.py --check
python3 scripts/generate_capability_reference.py --check
.venv/bin/python scripts/validate_operation_schemas.py
.venv/bin/python -m build --no-isolation
.venv/bin/python scripts/qualify_flagship.py \
  --wheel dist/slurm_tower-6.1.0-py3-none-any.whl \
  --output /tmp/tower-6.1-qualification.json
```

The qualification output path must not already exist. `--no-isolation` requires
the declared build requirements to be installed in the development environment.
Read the [release inventory guide](release-inventory.md) to generate and verify
artifact and member checksums after the final build.

For the UI measurement, use an unchanged checkout as the comparison source:

```bash
python3 -S scripts/benchmark_changing_ui.py \
  --source-root /path/to/comparison-source \
  --output /tmp/tower-changing-ui.json \
  --jobs 1000 --history 3000 --points 4000 --repeats 100 \
  --cases static,live30,live1,switch,resize,scroll-end --glyphs both
python3 -S scripts/benchmark_sustained_pointer.py \
  --source-root "$PWD" --output /tmp/tower-pointer.json \
  --rates 50,250,1000 --seconds 4 --points 1000 --jobs 6 \
  --live --delta 1 --workers single --switch-workers --switch-control toolbar
```

Run the compared measurements sequentially without concurrent test suites.
Keep the complete results, including slower cases, source identity, I/O attempts,
geometry failures and unexpected tab changes.

Actual Slurm allocations, physical GPUs, multi-node transport, production SSH,
terminal emulator presentation, user studies and scientific outcomes still need
the target-system evidence described in [qualification](../QUALIFICATION.md).
