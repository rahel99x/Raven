# Tower 4.14.0 validation

[Phase one guide](phase-one.md) · [Performance guide](ui-performance.md) ·
[Roadmap](../ROADMAP.md)

Validation date: 2026-10-10. The comparison baseline is Tower 4.13.0,
commit `b636f23d31af8ee1e18e52fb263104c15a521299`.

This is a historical release record. Test totals and performance measurements
describe the 4.14.0 artifact; they do not qualify later Raven releases.

## Release checks

| Check | Result |
| --- | --- |
| Complete regression suite, Python 3.12.14 | 11,081 passed; no failures or skips |
| Optional ShellCheck | Tests used the actual ShellCheck 0.11.0 executable |
| Offline setup | Demo environment and report created through `scripts/setup.py` |
| Distribution build | Source archive and wheel built successfully |
| Installed wheel outside the checkout | Version, doctor, full report, telemetry, and shell analysis passed |
| AMD and Intel demo collection through installed wheel | Correct provider, stable device identity, healthy source |
| Python 3.13 smoke | Actual telemetry CLI command returned a valid report |
| Python 3.10 syntax compatibility | Changed Python files parsed with the 3.10 grammar |
| Whitespace and documentation | Diff checks, local links, and manifest examples checked |

The wheel shell-analysis check included a script that would create a marker
file if executed. Analysis completed and the marker was absent.

The local full test run used Python 3.12. The repository CI matrix also targets
Python 3.10 through 3.14; the local syntax and smoke checks do not replace full
tests on those interpreters.

## Interaction and failure coverage

- Dialogs receive mouse motion, press, release, drag, and wheel events without
  passing them to the job list, graph, or page behind the dialog.
- Unicode and ASCII rendering is checked at narrow and normal terminal sizes.
  Source selection remains visible in short panes. Graph selection survives
  sample retention changes at three widths in Jobs and Analytics.
- Provider changes discard old observations, retry delays, and completion
  timestamps. In-flight samples cannot replace data from the new provider.
  Tests cover Single and Multi worker modes.
- GPU tests cover structured vendor replies, missing tools, command deadlines,
  output bounds, unsupported counters, partial nodes, stable device identity,
  ambiguous allocation mapping, concurrent inventory updates, and isolated
  Python startup on compute nodes.
- Telemetry tests distinguish unknown fields from conflicting identity evidence.
  Reused job IDs, connection changes, delayed callbacks, and actual scripted
  commands are covered. Polling does not imply fresh producer measurements.
- Shell tests cover unsupported interpreters, non-regular files, oversized
  input, startup hooks, external-source directives, malformed analyzer output,
  cancellation, process cleanup, and content-based cache invalidation.
- Array tests cover sparse indices, duplicate identities, malformed documents,
  bounded remote reads, source replacement, explicit revision reloads,
  connection and submission identity changes, resource edits, and retained retry
  metadata. Source changes are checked again before submission.
- Existing mouse protocol, grouping, scrolling, copying, worker, report, and
  submission tests remain part of the complete suite.

## Render benchmark

The existing changing-UI benchmark ran against an isolated 4.13.0 source archive
and the 4.14.0 implementation on the same machine. Each run used 100 measured
frames for each of five cases in both Unicode and ASCII: static display, a
30-second Live window, a one-second Live window, job switching, and resizing.

The workload contained 1,000 current jobs, 3,000 history entries, and 4,000
points per source kind. Each selected job had four native GPU devices and four
trace devices. The viewport was 320 by 52 characters. Pointer events advanced
at 50 Hz, Live redraws at 10 Hz, and native publications every 500 ms.

| Observed frame statistic across the ten cases | 4.13.0 | 4.14.0 |
| --- | --- | --- |
| Median range | 1.63–2.19 ms | 1.59–2.12 ms |
| 95th percentile range | 52.93–112.00 ms | 46.60–101.65 ms |
| 99th percentile range | 106.12–190.69 ms | 107.66–193.26 ms |
| Render-time I/O attempts | 0 | 0 |
| Geometry validation failures | 0 | 0 |

Typical frame cost remained similar in this comparison. Large redraw spikes
remain an optimization target. These are single-run measurements with operating
system scheduling variation, not a claim of a statistically significant speedup.
The benchmark measures application work with a counting paint sink. It excludes
terminal transport and scheduler or hardware latency.

To repeat the 4.14.0 candidate measurement, run from that release's source:

```bash
python3 -S scripts/benchmark_changing_ui.py \
  --glyphs both --repeats 100 --output /tmp/tower-4.14-ui.json
```

## Site checks

This environment had no live Slurm cluster or physical NVIDIA, AMD, or Intel
GPU. Vendor tests use documented-format fixtures and executable utility
simulations. Confirm live behavior using a permitted allocation at the target
site, and inspect **Sources → GPU source** and **Metric sampling**.

Shared GPU allocations with opaque numeric subsets remain unavailable when
ownership cannot be established. Site permissions, installed vendor versions,
GPU partitions, and Slurm accounting configuration can affect available data.
Use the [GPU diagnostic guide](gpu-detection.md) for those checks.
