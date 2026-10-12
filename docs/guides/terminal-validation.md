# Terminal validation: version 6.3.0

[Terminal guide](terminal-fusion.md) · [Delivery matrix](../reference/terminal-fusion.md) ·
[Qualification policy](../QUALIFICATION.md)

This record documents validation on a shared Linux cloud host using local
pseudo-terminals (PTYs) and the installed optional engine. The results cover the
tested local configurations. Live clusters, physical terminals, remote
authentication, and container runtimes require separate qualification.

## Regression scope

The complete repository suite passed **15,103 tests**, with **8 skips**, on
Python 3.12 in **408.76 seconds**. Three warnings came from the existing
cross-process recovery test's multiprocessing fork fixture. The terminal
broker uses a fresh executable process.

After the complete run, shell-hook launch paths were corrected for aliases and
empty `PATH` values. The final terminal suite was checked separately on the
supported interpreter boundaries. These overlapping suites are not added to
the complete-suite total:

| Final terminal suite | Passed | Skipped | Duration |
| --- | ---: | ---: | ---: |
| Python 3.10 | 696 | 1 | 77.85 s |
| Python 3.12 | 696 | 1 | 75.34 s |
| Python 3.14 | 696 | 1 | 126.43 s |

The skipped terminal case requires an installed Fish shell. The tests cover:

- Normal/alternate screens, modes, fragmented protocols, Unicode, bounded
  control strings, clipboard/link mediation and actual Vim edit/save/restore.
- Real controlling terminals, foreground groups, Ctrl-C/Ctrl-Z, input queue
  boundaries, output fairness, resize epochs, final output, reaping and shutdown.
- All six supported pages, Details state, shared sessions, explicit focus,
  stale input, cross-pane drags, hidden resize geometry and modal ownership.
- Exact pinned selection, soft wraps, retained search, copy destination,
  multiline paste review, long reports and menus at narrow sizes.
- Exact reviewed allocation targets, shell quoting, recipe imports, explicit
  broadcast recipients, sequence cancellation and delayed launch receipts.
- Authenticated CLI bridge reports, revoked session capabilities, context
  provenance, command evidence, filesystem bounds, privacy and stale results.
- Recording timestamps and budgets, inert timed replay, framed player input,
  compatibility profiles and an actual finite PTY capability probe.

The independent page/input integration set passed **17 tests** in **31.53
seconds**, retaining its six-second per-operation deadlines. It found and
verified fixes for hidden terminal focus, report-end scrolling, hidden-session
notices, capability retirement and shutdown cleanup. Overlapping focused suites
are not added to the final complete-suite total.

An outer curses PTY test starts the complete application, opens the embedded
shell, sends fragmented mouse reports and Raven shortcut characters, verifies
the child's output and exits through the explicit focus escape. It passed and
is included in the final terminal suite. Real Bash hook tests also pass with an
empty `PATH`, helper paths containing spaces/apostrophes, and a working directory
outside the checkout; they verify command boundaries and preserved exit status.

## Release checks

- Demo setup with `--terminal` passed without changing the selected profile.
- Generated operation and capability references passed their freshness checks.
- All **49 schemas** and **35 published examples** validated.
- Documentation qualification checked **64 documents** and **708 local links**.
- Wheel and source distributions built successfully. All **215 runtime Python
  modules** in the wheel matched the checkout byte for byte.
- All **eight local qualification categories** passed, including a clean core
  wheel installation outside the checkout with no optional terminal packages.
- The optional engine installed from local wheels outside the checkout. Its
  six real-PTY self-tests passed. Private profile export and comparison against
  a fresh probe passed as well.

These checks used pyte 0.8.2 and wcwidth 0.9.2. They did not submit, cancel, or
attach to any live Slurm job. The release CI matrix installed the terminal extra
and ran an installed-wheel terminal self-test in addition to core qualification.

## Compatibility and measurement

Python 3.10, 3.12 and 3.14 are checked. Optional shell availability is reported
separately from tested support. The engine tests include the installed Vim;
that does not certify every Neovim plugin or interactive application.

On the development host, the initial isolated engine probe parsed approximately
0.67 MiB/s of plain output and 0.28 MiB/s of SGR-heavy output. Cached 40×120 screen
snapshots took approximately 0.05–0.1 ms. These are adapter observations, not
input-to-display guarantees. Backpressure is expected for faster producers.

The embedded-terminal benchmark uses real finite PTY children, 60 synthetic
Jobs records, a 150×45 viewport, 40 inputs per scenario and a varied output
stream bounded at 1 MiB. It runs with a 16 ms readiness interval. The final
instrumented run produced these results:

| Scenario | Input to paint, p95 | Input to paint, maximum | Cached feedback, p95 |
| --- | ---: | ---: | ---: |
| Idle | 44.7 ms | 60.2 ms | 2.52 ms |
| Hidden output flood | 47.4 ms | 62.9 ms | 2.78 ms |
| Visible output flood | 74.1 ms | 76.3 ms | 4.96 ms |

All 120 acknowledgments arrived. Cached feedback caused no Jobs document
composition, no document rebuild before its normal deadline, and no painting
outside the terminal pane. The measured endpoint is the in-memory differential
paint callback. It excludes curses conversion, the outer terminal, network
transport and monitor latency. Hidden-session startup is included; visible
startup completes before measurement. Published hidden-session byte counters
can lag actual parsing.

The attribution run recorded a 15.1 ms generation-2 garbage collection during
control publication and a 20 ms differential paint operation. UI CPU time
matched wall time and container counters reported no CPU throttling. Earlier
runs recorded 421 ms and 222 ms input outliers, including a 230 ms document
rebuild. Those outliers did not reproduce in the final instrumented run; their
causes remain unassigned. These results do not establish spike-free behavior.

Reproduce the bounded probe separately from test suites:

```bash
.venv/bin/python scripts/benchmark_embedded_terminal.py \
  --source-root "$PWD" --output /tmp/raven-terminal-benchmark.json
```

The existing sustained-pointer probe also passed at **50, 250 and 1,000 mouse
reports per second** with six visible live plots and toolbar worker-mode changes.
Every run exited cleanly with **zero unexpected tab changes**. The p95 age of
the painted pointer was 27.8, 4.7 and 2.4 ms respectively; the largest observed
age across the runs was 66.7 ms. Motion coalescing intentionally makes the
dispatched count smaller than the received count. Each rate ran for one second,
so this is a bounded regression probe, not a long-duration stability claim.

```bash
.venv/bin/python -S scripts/benchmark_sustained_pointer.py \
  --source-root "$PWD" --output /tmp/raven-pointer.json \
  --rates 50,250,1000 --seconds 1 --points 1000 --jobs 6 \
  --live --workers single --switch-workers --switch-control toolbar
```

The broker uses bounded parsing slices and separate execution from Raven's UI.
The UI's paint measurements exclude physical terminal transport. Shared-host
contention can increase wall time. In one deliberately overlapping development
run, real-PTY deadline tests timed out while several suites competed; the
serialized independent integration run passed with the original deadlines.

Live Slurm attachment, SSH authentication, containers, remote tmux persistence,
Termius, physical mouse devices and cluster policy require site-specific checks.
No result in this record substitutes for those checks.
