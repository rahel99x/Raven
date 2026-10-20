# UI, log and terminal review outcomes

The per-finding record is [ui-log-terminal-results.json](ui-log-terminal-results.json).
It covers UI-1–14, LOG-1–11 and the terminal portions of HV-11, HV-17 and HV-18.
Cross-owner Store and mutation evidence is identified explicitly.

The delivered paths preserve selected job and recording identity, reject stale
callbacks, keep irreversible actions behind the existing review flow, and retain
raw log bytes independently of terminal-safe presentation. Test changes preserve
behavioral assertions. Export tests now choose an explicit durable directory;
legitimate symlinks at that chosen root are supported, while internal planted
leaves remain protected by exclusive/no-follow writes.

## Design choices

- A shared pager/allocation handoff restores input ownership, signal handlers,
  terminal modes and resize handling on every child outcome.
- Native publication revisions reuse idle documents on every workspace page;
  visible clocks, source ages and pending waits refresh separately. Unknown extensions retain
  conservative invalidation. Managed native publications use one-second idle
  housekeeping; mutable/unmanaged sources and visible active-file readers retain
  the 200 ms fallback. Selection and marks do not rebuild native source rows.
- Clipboard delivery and retained-log indexes use existing shared workers.
  Immutable selections, bounded queues, source guards and cancellation retain
  accepted work without blocking terminal input.
- The notification lane keeps at most 32 queued hook invocations, drops the oldest
  queued hook visibly, and batches events as JSON on stdin. Timeouts clean up the
  complete owned process group even when its shell exits first.
- The terminal runtime is a coherent package with no upward controller, view or
  curses dependencies. Flat module names remain identity-preserving aliases.
  Its bulk VT path accepts only compatible complete printable CRLF lines and
  retains exact history, wrapping, style, cursor and eviction behavior; other
  output keeps the ordinary VT parser.

## Validation

Focused suites passed: terminal/package 255; UI/package 114; real ncurses PTY,
alerts and copying 3; log/export/cache/wakeup 697; history/grouping/persistence/data
249. After the injected clipboard worker port and final source-age freshness check,
274 focused tests passed in 3.16 seconds. These suites overlap deliberately and their counts are not a unique-test total.
Integration preflight then exposed nine analysis context-click regressions: the
foreground owner accepted only left clicks. Its existing right-click handler now
clears local text/row selection after graph-reset precedence. All 26 original
selection tests and 1,214 related focus/chart/metric/operations tests passed;
the no-I/O, hidden Enter, source identity, marks and zoom assertions remain intact.
Further pointer qualification preserved stale-release consumption across modal,
toolbar and geometry changes and registered column resizing as an input-only
focus owner. The final 191-test cache/input run verifies immediate publication
invalidation, captured-snapshot provenance, independent clock patches, the actual
one-second native idle interval, conservative fallback and active-file freshness.
Commands, source hashes and qualification limits are in the JSON record. Full
repository regression, packaging and compatibility checks belong to the final
integrated revision and its CI run.

## Measurements

Equivalent fixtures load the reviewed `b98742f` implementation and the current
implementation in fresh processes. The reproducible harness is
`scripts/benchmark_ui_review.py`; raw values are in
[measurements/ui-log-terminal.json](measurements/ui-log-terminal.json).

| Fixture | Before | Current | Process maximum RSS |
| --- | ---: | ---: | ---: |
| 8 MiB printable CRLF, 24×80, 2,000 retained lines | 40.26 s; 40.24 CPU s | 4.39 s; 4.38 CPU s | 23,532 → 24,896 KiB |
| 10,000 jobs, 30 selection/mark changes, prepared shared rows | 227.9 ms/input | 52.8 ms/input | 94,360 → 68,444 KiB |
| Real local PTY, 8 MiB LF input expanded to 10 MiB CRLF | Current qualification only | 1.97 s; broker parsing 1.63 s; UI CPU 0.069 s | UI process 13,568 KiB |

The terminal fixture retains exactly the same screen, history count and eviction
boundary. Its observed speedup is about 9.2×, with approximately 5.8% more maximum
RSS. Prepared-row processing is about 4.3× faster; group projection remains part
of its measured cost. RSS is a whole-process Linux measurement, not Python
allocation tracing. Runs are observational measurements on a shared cloud host,
without affinity control or statistical confidence intervals. They establish no
live Slurm, GPU, remote-host or vendor-specific hardware qualification.
