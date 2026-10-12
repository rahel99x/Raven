# Validate the 4.16 workspace and reporting update

This record describes the 4.16 release checks for table resizing, shared
Jobs/Recents layout, explicit grouping, reviewed queue ordering, job selection,
and Bash Research reporting. The release retained the terminal event loop and
background workers. These results qualify the recorded artifact only.

## Regression coverage

The final Python 3.12 suite completed with **12,516 passed and 2 skipped**.
The optional ShellCheck suite also passed all 41 tests when its executable was
added to the test path.

- Column tests cover header geometry, wide characters, narrow panes, both width limits, drag cancellation, one save per completed gesture, right-click reset, and shared Recents alignment.
- Input tests cover App dispatch, native curses events, raw SGR reports, ASCII and Unicode, Shift-click in both directions, plain drag from marked rows, Main/Recents crossing, and explicit text-selection ownership.
- Combined sequences use grouping and undo, manual membership, column resizing, Details graph zoom/reset, live row replacement, cancelled queue drags, and delayed releases in one session.
- Queue tests verify owner and scheduling scope, site capability, exact array tasks, archived members, holds, refreshed priorities, changed attempts, duplicate actions, and bounded peer discovery. Pointer movement never applies a scheduler action.
- Reporting tests use real temporary files and child processes for fast pre-acknowledgment startup, sparse arrays, dependency strings, retries, signal handling, exclusive execution, interrupted publication, concurrent writes, and uncertain submission outcomes.
- Reader tests cover every Research source, late files, incremental metrics, shared planning, per-attempt contracts, replaced directories, ambiguous identities, changed connections, stale callbacks, and SSH artifact browsing. SSH-helper tests execute the real isolated Python helper through a local transport fixture.

The affected table/layout matrix passed 615 tests on Python 3.13. The final
Research reader matrix passed 721 tests on each of Python 3.12 and 3.13. The
reporting and portable-project matrix passed 144 tests on Python 3.13.
The combined selection regression set passed 842 tests.

## Package and schema checks

A clean wheel installation outside the checkout completed the Bash helper
workflow with dependency-linked jobs, sparse array tasks, a failure, a restart,
and a job that completed before the submission command returned. All eleven
`raven report` subcommands exposed their help. The source archive contains the
Bash helper; the wheel contains the reporting and remote reader modules.

All 23 JSON Schemas and 19 declared examples passed the schema audit. The Bash
helper passed syntax and ShellCheck validation. Updated documentation links and
command examples were checked against the implementation.

## Render measurements

The changing-UI benchmark used 1,000 queue jobs, 3,000 history jobs, 4,000
retained points, a 320-by-52 terminal, and 100 frames for each of six cases in
both Unicode and ASCII. Both releases recorded zero forbidden render I/O and
zero graph-geometry failures.

| Unicode case | 4.15.1 median frame work | 4.16.0 median frame work |
| --- | ---: | ---: |
| Static graph | 2.17 ms | 1.70 ms |
| 30-second Live window | 2.05 ms | 1.93 ms |
| 1-second Live window | 1.83 ms | 1.96 ms |
| Selected-job changes | 3.80 ms | 2.00 ms |
| Terminal resizing | 4.23 ms | 1.84 ms |
| Scroll to document ends | 2.23 ms | 1.79 ms |

These short measurements ran on a shared cloud host. They exclude terminal
transport and source latency. Full document rebuilds still cost more than
cached pointer feedback; the largest 4.16 frame in this matrix took 294 ms.
Use the sustained-input benchmark on the deployment terminal when assessing
perceived smoothness. The measurements do not establish a universal speedup.

## Repeat the checks

Use the corresponding 4.16.0 source and a development installation. Run from
that release's repository root:

```bash
python -m pytest -q
python scripts/validate_operation_schemas.py
bash -n scripts/raven-report.sh
shellcheck scripts/raven-report.sh
python -m build
python -S scripts/benchmark_changing_ui.py \
  --source-root "$PWD" --output /tmp/raven-changing-ui.json \
  --cases static,live30,live1,switch,resize,scroll-end \
  --glyphs both --repeats 100
```

The schema audit requires the optional developer package `jsonschema`; it is
not a Raven runtime dependency.

## Deployment limits

No live Slurm controller or production SSH host was available in this
workspace. Scheduler changes were tested through command fixtures and reviewed
against Slurm's documented native operation. The queue-order feature requires
site-enabled `enable_user_top`; scheduler policy and resources still determine
actual start times. See [queue controls](batch-launches.md#review-a-groups-queue-order).

The reporting helper records supplied evidence. It does not infer missing
scientific values or turn arbitrary dependency expressions into `afterok`
workflow estimates. See [Research automation](research-automation.md) for
source contracts, attempt identity, array receipts, and reader limits.
