# Tower 6.0.0 validation record

Recorded on 11 October 2026 on a shared Linux cloud host. This record separates
executable checks from target-system qualification. It does not certify every
Slurm installation, terminal, runtime, or scientific workload.

The [implementation ledger](../FLAGSHIP_IMPLEMENTATION.md) maps the forty
investments, five research hypotheses, and ten earlier proposals to controls,
supported mechanisms, and remaining boundaries.

## Release checks

The full regression runs passed **13,584 tests on Python 3.12.14** and
**13,581 tests on Python 3.13**, with one optional Dask dependency skip each.
The 3.12 run includes the final attempt-association and breadcrumb-cache fixes;
237 focused context/workspace tests then checked those fixes on 3.13. The
fork regression runs in an isolated interpreter with a timeout.

Final test-harness checks passed 156 PTY/raw-input cases and 47 sustained-pointer
cases on 3.12. A 69-case PTY/benchmark set also passed on 3.13. They include
atomic publication of readiness and acknowledgement files, so a reader cannot
observe an empty file while a writer is publishing JSON or a counter. Input
timings and gesture assertions remain unchanged.

An exit warning identified a stopped supervisor child that had not yet been
reaped. All 71 final service tests passed with resource warnings treated as
errors. They check bounded reconciliation and concurrent PID replacement,
without signaling unrelated processes or stopping active persistent services.

Additional compatibility sets passed on Python 3.10 and 3.14. The broader
trace/platform/scientific-diagnostics set passed 476 cases on 3.10, with the
CPython 3.12 monitoring case skipped, and 477 cases on 3.14. The final runtime,
schema, pinning and inventory sets passed 228 cases on 3.10 and 222 on 3.14;
these sets overlap and are not added to the full-suite totals. The explicit
older-Python profile adapter is tested separately from monitoring.

The optional Dask integration passed with `dask-jobqueue 0.9.0` and
`distributed 2026.8.0` in an isolated environment. Submission and cancellation
were simulated; this validates the installed API contract, not live workers.

The validation exercises:

- Fragmented mouse protocols, passive hover, keyboard traversal, capture
  cancellation, stale releases, graph zoom, selection and sequential actions.
- Late results after job, attempt, tab or connection changes; no source reads
  in trace paint; correctly fitted first publication of inline graphs.
- Single/Multi worker transitions, nested admission, cancelled work that is
  still draining, coalesced preference writes and last-good state recovery.
- Real child processes, process-group cancellation, immutable Git trials,
  local HTTP/TLS, bounded adapter messages, source replacement and shared-cache
  admission across processes.
- Python event journals, native C instrumentation, two local Open MPI ranks,
  missing coverage, corrupted records, committed prefixes and partial outcomes.
- Reporting schemas, published examples, operation-reference freshness,
  documentation links, source/wheel builds and clean installed-wheel commands.

The schema audit passed all 48 schemas and 35 published examples. The final
dependency-free installed wheel passed all eight local qualification categories
and fourteen additional platform integration checks. These used real supervised
HTTP/TLS processes with synthetic credentials and endpoints, bounded adapter
execution, reviewed portable/support exports, and shared-cache cancellation.
Wheel Python modules were compared byte-for-byte with the checkout.

Separate installed-wheel checks generated and compiled the native SDK and PMPI
wrapper, recorded Python threads, and ran two local MPI ranks. Release
inventories include artifact/member hashes, the core license, build metadata
and explicit external-tool exclusions. The generated minimal SPDX document
passed the official SPDX 2.3 schema. Read the
[inventory guide](release-inventory.md) for verification and authentication limits.

## Pointer stress

The real PTY harness published CPU/GPU fixture observations while the pointer
crossed Jobs and six inline graphs. Each run lasted four seconds, used 1,000
retained observations, and switched workers through the toolbar from Single to
Multi and back to Single. All runs retained the selected job, rendered the
graphs, exited cleanly, and recorded **zero unintended tab changes**.

| Mouse reports/s | Latest dispatched input p99 | Painted pointer age p99 | Maximum painted age |
| --- | ---: | ---: | ---: |
| 50 | 18.77 ms | 29.79 ms | 46.67 ms |
| 250 | 2.81 ms | 5.50 ms | 45.39 ms |
| 1,000 | 0.97 ms | 2.82 ms | 47.49 ms |

Motion reports are coalesced. A higher input rate can make the most recent
position younger without making each report faster. For example, at 1,000
reports/s the p99 latency across *all decoded reports* was 38.92 ms, and the
maximum was 61.90 ms. These measures exclude the user's terminal presentation
and network. Do not infer a universal sub-millisecond interface from this table.

The compact [benchmark record](../benchmarks/6.0.0-local.json) includes these
separate measures and the changing-UI comparison.

## Optimization evidence

Revisioned scalar histories avoid repeated copying of unchanged CPU/GPU history
tuples. A 2,000-history, 600-value, 120-call stage benchmark reduced median
snapshot work from 12.436 ms to 0.333 ms. This measures one publication stage.

A separate changing-UI comparison used 1,000 jobs, 3,000 historical jobs, 4,000
points, and 100 frames per case. Results were mixed: the job-switch case's p95
frame work fell from 99.23 ms to 69.45 ms, while several other percentiles rose.
The integration build's largest frame was 245.81 ms. Large history preparation,
document composition and graph rasterization remain material costs. These
results do not establish that every lag spike is fixed or that every frame is
faster. The comparison predates the final first-publication viewport fix.

A proposed C-backed native-record copying shortcut was rejected. Once its
identity and correctness checks were included, it was slower on the measured
workload and increased memory use. It is not part of this release.

## Trace cost and coverage

A CPython 3.12 workload of 200 calls, each doing 15,000 arithmetic iterations,
took a median 0.211 s without recording and 0.415 s through the recorder across
three repetitions: approximately **1.97 times the wall time**. Each recorded
run produced 4,163 events and approximately 2.28 MB. This includes launcher and
publication costs; it is neither a universal overhead bound nor a zero-cost
trace claim.

Incremental reads targeted 15 ms slices. The largest observed slice was
18.32 ms; caught-up reads had medians from 0.19 to 0.23 ms. Budgets are checked
between bounded units of work, not enforced as hard real-time deadlines.

The actual local Open MPI 5.0.7 checks used two ranks. Supported world-communicator
collectives matched; unsupported operation/communicator/thread cases retained
application results and reported coverage gaps. This does not qualify physical
multi-node transport, arbitrary MPI implementations, or GPU device stacks.

## Reproduce the checks

Use the corresponding 6.0.0 source and a development installation to reproduce
this record. Run from that release's repository root. Later releases require
their own artifact version and validation record.

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q -ra
python3 scripts/generate_operation_reference.py --check
python3 scripts/validate_operation_schemas.py
python3 -m build
python3 scripts/qualify_flagship.py \
  --wheel dist/slurm_tower-6.0.0-py3-none-any.whl \
  --output /tmp/tower-qualification.json
python3 scripts/benchmark_sustained_pointer.py --help
python3 -S scripts/benchmark_changing_ui.py --help
```

Real MPI tests require a supported MPI compiler and launcher. If development
headers live outside the compiler's default search path, the optional
`TOWER_TEST_MPI_INCLUDE` setting supplies their directory. No test here submits
a production Slurm job.

## Remaining qualification

No production Slurm controller, physical GPU allocation, multi-node cluster,
production SSH endpoint, or external user study was available for this run.
These remain explicit untested entries in the qualification report. Optional
MPI/PyTorch collective controls and CUDA summaries need their documented
target-runtime checks. Scientific benefit and novelty require independent
evaluation. Use the [qualification guide](../QUALIFICATION.md) to record those
results against the exact release and site policy.
