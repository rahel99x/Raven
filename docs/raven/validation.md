# Raven 7.0.0 validation record

This record describes local checks, corrected failures, and measured limits.
It is not a production Slurm qualification. The [implementation map](implementation.md) and
[execution contracts](execution-contracts.md) state the supported scope.

## Regression and compatibility

| Check | Result |
| --- | --- |
| Initial application suite, Python 3.12.14 | 15,639 passed; 8 skipped; 587.47 seconds |
| Complete sweep after the quiet-Jobs optimization, Python 3.12.14 | 15,663 passed; 8 skipped; one timed PTY journey failed; 598.31 seconds |
| Complete terminal integration module after test correction, Python 3.12.14 | 35 passed; 41.78 seconds; two additional PTY repetitions passed |
| Changed components, Python 3.10 | 1,884 initially passed; 1 skipped; 3 setup failures corrected as described below |
| Complete setup suite after correction, Python 3.10 | 10 passed; 141.57 seconds |
| Complete setup suite after correction, Python 3.12.14 | 10 passed; 142.14 seconds |
| Changed components, Python 3.14 | 1,888 passed; 1 skipped; 330.98 seconds |
| Final UI, cache, completion and terminal checks, Python 3.10 | 481 passed; 56.76 seconds |
| Final UI, cache, completion and terminal checks, Python 3.14 | 481 passed; 52.43 seconds |
| Reporting contracts | 51 schemas and 38 examples passed |
| Generated references | Operation reference and Raven capability ledger match source |

The Python 3.10 failures identified a setup portability defect. Copying a
relocatable Python executable into a new virtual environment prevented it from
finding its standard library. Setup now follows the POSIX `python -m venv`
symlink behavior. A real nested-environment regression checks isolated startup,
standard-library imports, and reuse without rewriting the environment.
All three failures passed after the correction. The optimized full application
sweep also includes this portability fix.

The eight full-suite skips cover optional Dask, ShellCheck, an unavailable shell,
and five MPI development-library cases. Three warnings come from an existing
test that deliberately forks to exercise cross-process crash recovery. They are
Python deprecation warnings, not Raven's application worker launch mechanism.
The [regression report](../benchmarks/7.0.0-regression.json) records commands,
the 32 selected compatibility modules, initial failures, and follow-up results.

The final sweep exposed an unreliable fixed-sleep PTY journey: its expected
two-line clipboard export was absent. The original test passed in isolation,
and its exact missed intermediate state could not be reconstructed. No
production copy defect was reproduced. The corrected test uses bounded waits
for actual rendered pages, confirmations, and selection feedback; reconstructs
the terminal screen; isolates host clipboard tools; and still requires the
exact two-line exported payload. The complete 35-test module and two additional
journey repetitions passed. This was a test-only correction after the sweep,
not a claim that the failing full run itself passed.

The 22 new quiet-frame regressions cover publication identity, unknown source
state, callbacks, clocks, completions, and conservative cache invalidation.

Coverage includes Details/Terminal focus markers and narrow layouts, mouse-frame
containment, graph gestures, selected copy, terminal ownership, stale actions,
allocation cancellation, worker loss, protocol limits, task placement, cache
pressure, source budgets, retention, migration, and exact-job advice.

## Distribution and terminal checks

The wheel and source archive build successfully. All 233 Python modules in the
wheel match the checkout. The source archive includes the source launchers,
schemas, and TOML examples.

A fresh environment outside the checkout passed all three installed launchers,
a dependency-free core run, standalone desktop diagnostics, a Heavy plan, and
an actual finite desktop Heavy session. The optional terminal dependencies were
then installed from local wheels; the real PTY self-test passed.
The [installed-package report](../benchmarks/7.0.0-installed-smoke.json) records
these final-wheel checks, including the six PTY protocol checks.

The [local qualification report](../benchmarks/7.0.0-local-qualification.json)
also records documentation links, ten plain-ASCII pages, CLI help, a real curses
PTY with fragmented pointer input, and an offline wheel installation.
This PTY check is a functional smoke test. It does not measure the time at which
a physical terminal or SSH client presents pixels.

## Offline collection qualification

The [offline report](../benchmarks/7.0.0-offline-qualification.json) exercises the
actual admission ledger with 1, 10, 50, 200, and 256 synthetic users, two sessions
per user, synchronized startup, refresh floods, failed producers, and recovery.
All emitted-attempt ceilings passed. Protocol-fragmentation and explicit-desktop
plan checks also passed.

The cohorts use independent private databases and synthetic identities. They do
not test authentication, cross-user sharing, actual scheduler RPC cost, or a
shared filesystem under production load. At 200 users, one queue request per
minute is already about 3.33 requests per second before other query families.
An administrator-approved pilot must measure the site's actual cost.

## Resource measurement method

The comparison uses the retained 6.3.0 source at
`3ed910affe02c06510f990ebadb8944be9ccdf85` and the Raven application source.
Each run uses a real 160-column, 48-row curses PTY, a ten-second warmup, and a
300-second measured window. The fixture contains 100 jobs, 80 running jobs,
25 history records, no GPU jobs, no ordinary shells, and no Heavy worker.
Monitoring receives one Up/Down key every 15 seconds. Idle tests pre-age the
actual idle timer at the first complete frame and then send no input; they
measure steady idle behavior, not the natural transition into idle.

Tests and builds do not run alongside these measurements. CPU is normalized to
one logical core and includes owned processes and waited descendants. RSS is
summed across the observed process tree; PSS is reported separately where Linux
provides it. Process/thread limits and incomplete accounting flags are retained
in the reports. Measurements can miss extremely short orphaned helpers.

The host runs Linux 6.18.44, glibc 2.41, and Python 3.12.14 with five affinity
CPUs, a four-core-equivalent cgroup quota, and a 32 GiB memory ceiling. This is a
cloud development host, not a login node. Simulated backend call counts cover
startup, warmup, measurement, and shutdown; they are full-run totals, not exact
five-minute RPC rates. The fixture makes no real scheduler calls.

The initial Light monitoring run still rebuilt quiet Jobs documents about five
times per second. The final implementation retains an unchanged document until
a source/UI publication or the next whole-second label boundary. It still runs
200 ms housekeeping and paints progress animation and pointer feedback from
cached rows. Unknown source state, pending readers, graphs, terminals, and active
gestures retain their existing refresh paths. A publication token is captured
under the same lock as the painted snapshot, so a later result cannot mark an
older frame as current.

Idle backoff reduces collection traffic; it does not stop the visible clock,
progress animation, feature housekeeping, or background self-observation.
Source inspection and aggregate measurements cannot assign a precise CPU cost
to those individual activities. The provisional 1% idle and 5% monitoring goals
must be assessed against the final five-minute reports; they are not hard
process limits or claims about arbitrary sites and terminals.

## Measured resource results

| Variant | Mode | Mean CPU, one core | Peak summed RSS | Peak PSS |
| --- | --- | ---: | ---: | ---: |
| [6.3.0 baseline](../benchmarks/7.0.0-baseline-monitor.json) | Monitoring | 9.43% | 50.95 MiB | 48.50 MiB |
| [Raven Light](../benchmarks/7.0.0-light-monitor.json) | Monitoring | 4.47% | 36.70 MiB | 34.23 MiB |
| [6.3.0 baseline](../benchmarks/7.0.0-baseline-idle.json) | Idle | 14.07% | 50.97 MiB | 48.53 MiB |
| [Raven Light](../benchmarks/7.0.0-light-idle.json) | Idle | 5.68% | 36.44 MiB | 33.97 MiB |

Monitoring CPU is 52.6% lower and peak RSS is 28.0% lower than the baseline in
this fixture. The provisional 5% monitoring and 256 MiB RSS goals pass. **The
1% idle CPU goal remains unmet.** Idle CPU is 59.6% below the measured baseline;
it is still higher than the monitoring result. These are separate runs on a
shared development host, not a guarantee about relative idle/active cost.
The [pre-cache-change Light run](../benchmarks/7.0.0-light-monitor-before-cache-fix.json)
is retained: it used 7.89% mean CPU and did not meet the monitoring goal.

Final Light monitoring emitted nine queue, two accounting, and six details
fixture calls over startup, warmup, measurement, and shutdown. Idle emitted
three queue, one accounting, and two details calls. Neither run emitted live
statistics calls because the fixture used the Jobs summary and inspector,
without visible metric demand. This does not compare all-metric sampling at
equal cadence. Light used four threads; the baseline used twelve.

The CPU headline uses cumulative owned-process CPU divided by the full measured
elapsed time. CPU percentile samples have varying intervals: sampling delays
can be followed by short catch-up intervals, where Linux CPU-tick resolution
exaggerates apparent peaks. Do not interpret those percentiles as uniformly
spaced one-second utilization or evidence of UI stalls. The whole-window mean
is unaffected by that interpretation. All four final resource reports mark
their process and PSS accounting complete within the stated harness bounds.

A separate [30-second profiling diagnostic](../benchmarks/7.0.0-idle-profile.json)
found document composition, character-width/layout work, rasterization, and
differential painting prominent in the remaining periodic work. See the
[call-context summary](../benchmarks/7.0.0-idle-profile.txt). Instrumentation raised
mean CPU to 18.43%; its timings are not qualification results. The process CPU
timer can charge overlapping background work to an active profiled call.
Earlier concurrent-profiler and thread-clock attempts were invalid and excluded.
This diagnostic identifies optimization candidates; it does not establish the
cause of individual samples or resolve the unmet idle target.

## Local pointer response

The sustained-pointer fixture has six visible live plots with 1,000 seeded
points and a one-second live window. Each rate runs for four seconds in a real
local curses PTY. The harness verifies that the requested six or 100 jobs remain
present. It records input decoding, dispatch, rendering and `curses.doupdate`.

| Jobs | Reports/s | Latest-pointer flush p95 | Latest-pointer flush p99 |
| ---: | ---: | ---: | ---: |
| 6 | 50 | 18.65 ms | 27.47 ms |
| 6 | 250 | 3.66 ms | 6.89 ms |
| 6 | 1,000 | 2.28 ms | 3.40 ms |
| 100 | 50 | 18.48 ms | 26.65 ms |
| 100 | 250 | 4.29 ms | 8.91 ms |
| 100 | 1,000 | 2.83 ms | 6.15 ms |

These [six-job](../benchmarks/7.0.0-pointer-multi-6.json) and
[100-job](../benchmarks/7.0.0-pointer-multi-100.json) results use Multi mode.
No unexpected tab changes or timing-event truncation occurred, and all runs
exited normally. Across these runs, decoded-report p99 latency was at most
42.60 ms; the longest single decoded-report delay was 73.37 ms.

Pointer motion can be coalesced. Flush timing measures the latest dispatched
motion, not an acknowledgement for every generated report. Higher offered
rates can therefore show a younger latest pointer without doing less work.
These results meet the provisional local p95/p99 response goals. They do not
measure physical terminal presentation, SSH, or a multi-hour interaction soak.

The same fixtures were then checked in Single mode, followed by a 100-job run
that used actual toolbar press/release events to switch Multi → Single → Multi.

| Mode | Jobs | Reports/s | Latest-pointer flush p95 | Latest-pointer flush p99 |
| --- | ---: | ---: | ---: | ---: |
| Single | 6 | 50 | 17.71 ms | 21.11 ms |
| Single | 6 | 250 | 4.65 ms | 10.10 ms |
| Single | 6 | 1,000 | 2.73 ms | 4.69 ms |
| Single | 100 | 50 | 20.14 ms | 32.29 ms |
| Single | 100 | 250 | 4.04 ms | 8.70 ms |
| Single | 100 | 1,000 | 3.11 ms | 6.37 ms |
| Mode switching | 100 | 50 | 21.76 ms | 55.00 ms |
| Mode switching | 100 | 250 | 4.80 ms | 9.40 ms |
| Mode switching | 100 | 1,000 | 2.66 ms | 6.02 ms |

The [Single six-job](../benchmarks/7.0.0-pointer-single-6.json),
[Single 100-job](../benchmarks/7.0.0-pointer-single-100.json), and
[mode-switch](../benchmarks/7.0.0-pointer-switch-100.json) reports verify the actual
worker states, job counts, clean exits, and absence of unexpected tabs. Every
switch run observed both modes and finished in settled Multi mode with no
queued or running work.

All 15 rate fixtures met the local p95 < 100 ms and p99 < 250 ms goals for
decoded reports and latest-pointer flushes. **This is not spike-free latency:**
Single mode with 100 jobs at 1,000 reports/s had a 235.29 ms worst flush age and
197.13 ms decoded-report p99 (234.47 ms maximum). The raw distributions and the
coalescing/physical-terminal limitations are part of the result.

The [benchmark manifest](../benchmarks/7.0.0-benchmark-manifest.json) records
harness, source and retained-artifact hashes. Resource and pointer harnesses
use different filename delimiters for source hashing; their digest strings
are therefore not directly comparable. Both measured the same frozen modules.

## Remaining target-system qualification

- Real Slurm allocations, execution-host and step placement, site cancellation
  behavior, accounting, and cgroup enforcement.
- Authenticated production SSH, compute-node software and path mapping, physical
  GPUs, and multiple nodes.
- Multi-hour retention and recovery soaks, physical terminal chains and clipboard
  delivery, external-user studies, and scientific outcome validation.
- Administrator-approved deployment cohorts and controller/accounting stop
  thresholds. The cloud workspace did not submit a cluster job or run a cluster
  load test.

Use the [site rollout and stop criteria](implementation.md#site-rollout-and-stop-criteria)
before a production pilot. Optional site services, full-interface transfer, and
deferred external mutation adapters remain exposed limitations; Heavy admission
does not silently enable them.
