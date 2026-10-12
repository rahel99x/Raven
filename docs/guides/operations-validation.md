# Operations validation

[Operations guide](operations.md) · [Roadmap](../ROADMAP.md)

This report defines the validation gates for research operations and records
the 4.15 rollout checks. Historical test totals apply to that artifact.
Target installations still need their own permission, service, and hardware
validation; an offline pass cannot establish those prerequisites.

## Required interaction checks

| Sequence | Required result |
| --- | --- |
| Prepare an action, switch connection, then apply | Refuse the old action before a scheduler or filesystem change |
| Prepare against a job, replace that job with a new attempt, then apply | Refuse an action for a reused job ID or previous attempt |
| Prepare from a file, change that file, then apply | Refuse stale source content and request a fresh review |
| Apply an action, then click Apply again | Preserve the receipt and prevent duplicate execution |
| Cancel during a multi-step operation | Retain completed steps and report work that did not run |
| Pass scientific acceptance, then request result reuse | Verify the complete declared identity, acceptance evidence, and output content |
| Change an accepted result before reuse | Refuse reuse; scheduler completion alone does not establish validity |
| Stage data into an existing destination | Refuse a collision rather than replace an unrelated file |
| Switch between Single and Multi while a command runs | Keep one command owner and publish its completion on the UI thread |
| Leave a tool while its worker completes | Keep the user's current page and selection |
| Move, drag, release, or scroll over an operation overlay | Keep input inside the visible overlay; do not activate hidden jobs, graphs, or tabs |
| Render in ASCII and Unicode at narrow and normal widths | Keep controls within the viewport and perform no collection from the renderer |

## Site validation

Cluster administrators can restrict reservations, licenses, accounting, node
events, job updates, allocation attachment, and service hosting. Check those
permissions with a small test allocation before use on a production campaign.
An unavailable permission or optional adapter must appear as unavailable
evidence, not a measured zero or a successful action.

Transfer, checkpoint, environment, and reuse manifests describe application
requirements. Test them with disposable data first. Include an intentional
missing file, changed checksum, interrupted action, and output collision.

Statistical results require the experimental design declared in the input.
The software can reject missing pairs and duplicate unit identities. It cannot
establish that a physical experiment is independent or that a declared
checkpoint is scientifically suitable for restart.

## Validation record

Validation was run on 2026-10-09 (America/Los_Angeles). The complete Python
3.12 regression run passed **11,920 tests**, with **one optional Dask test
skipped**. ShellCheck was available for this run. The new operation suites also
passed **834 tests on Python 3.13**, with the same optional test skipped.

The optional test passed separately with `dask-jobqueue 0.9.0` and
`distributed 2026.8.0`. It used a real local Dask scheduler and adapter, with
Slurm submission and cancellation replaced by controlled responses. It did
not submit a job to a real cluster.

Additional release checks passed:

- All 22 JSON Schemas and 19 published JSON examples passed structural
  validation. Native readers and headless CLI checks verified the documented
  examples separately.
- Source and wheel distributions built. The wheel installed into a fresh
  environment outside the checkout, passed the simulated doctor, produced a
  complete ASCII report, and read the energy example through its CLI.
- The offline setup checked all ten main tabs, thirteen Research workspaces,
  ASCII and Unicode output, and report generation.
- A real curses session in a pseudo-terminal accepted field paste, inspected
  the energy example, processed a burst of 250 mouse reports, switched worker
  modes, and exited with code zero. No mouse event changed tabs. Motion
  coalescing reduced the burst to three dispatched mouse events.
- All 146 application modules parsed with Python 3.10 grammar. This syntax
  check is separate from the runtime tests on Python 3.12 and 3.13.

Read the [performance comparison](operations-performance.md) for measured
latency, rendering work, and the limitations of the counting-painter harness.

The focused interaction and sequence checks include:

```sh
.venv/bin/python -m pytest -q \
  tests/test_ops_ui.py \
  tests/test_operations_sequences.py \
  tests/test_pointer_integration.py
```

These checks include all 26 operation forms in ASCII and Unicode at three
terminal sizes; raw mouse press, release, motion, drag, and wheel events;
keyboard field editing; and both directions of the Single/Multi transition.
They also check the following complete sequences:

- Scientific acceptance, verified result reuse, and verified output return.
- Pending-job editing followed by dependency repair, and the reverse order.
- Changed source files, changed connections, and reused job identities between
  preparation and application.
- A reviewed action followed by closing its pane, changing pages, requesting
  quit, or shutting down the session.
- A subprocess failure followed by a successful command, and a timed-out
  command whose child remains alive after the command's parent exits.

An incomplete action review disables Apply. Long JSON values remain exact
when the complete review fits the display budget. The copy command retains
the full structured document.

The subsystem tests cover optional adapters and lifecycle failures separately.
Live hardware and site-specific checks remain distinct from these offline
results. CARC allocations, a Fedora Slurm installation, physical GPU telemetry,
and an Apptainer runtime were not exercised in this environment.
