# Automatic instrumentation: controlled qualification

These measurements compare the preserved `b52cd93e2b7581eeff6e9f601ad13765f4618a0c`
source with the implementation bytes identified in the accompanying JSON files.
The working branch has that baseline commit as its ancestor; a Git revision
alone does not identify these uncommitted benchmark inputs. Each subprocess
records SHA-256 hashes and refuses a sample if its measured source changes.

## Method and boundaries

The harness is [`benchmark_auto_instrumentation.py`](../../scripts/benchmark_auto_instrumentation.py).
Each observation runs in a new process, with fixed numerical inputs and one
BLAS/OpenMP thread. Three timing repetitions alternate the variant order.
Imports and framework setup occur before timing; recorder creation, durable
flush, and shutdown occur inside timing. The same fixture bytes execute against
both source trees. Checks verify numerical results, every retained call/return
pair, journal integrity, and metric counts and final values.

Timing and allocation measurements use separate subprocesses. `peak_process_rss_bytes`
is the process high-water mark, including imports and native memory;
`peak_python_allocated_bytes` is the separate `tracemalloc` peak. These quantities
are sampled before the subsequent bounded journal-verification pass and are not
interchangeable. Filesystem block counters and output byte counts are
also retained. The temporary filesystem reports zero physical output blocks in
these runs; that does not imply zero serialization, writes, or flushes.

This environment has CPython 3.12.14, NumPy 2.3.5, mpi4py 4.1.2, and Open MPI
5.0.7. MPI uses two actual processes on one machine. PyTorch is absent and is
explicitly skipped. There is no Slurm, GPU/NCCL, remote filesystem, or multi-node
qualification in this report. These results do not establish a universal
overhead bound or deployment efficacy.

## Fully retained recording

The controlled results are in
[`automatic-instrumentation-small.json`](automatic-instrumentation-small.json).
Times are medians in milliseconds. MPI reports the slower rank's wall time per
launch; its CPU statistic sums both ranks. Its six rank observations represent
three launches, not six independent experiments.

| Fixture | Recording off | Baseline recording | Block recording, project scope | Block recording, all scope |
| --- | ---: | ---: | ---: | ---: |
| 2,000 Python calls | 0.170 | 85.032 | 49.684 | 49.653 |
| 200 NumPy matrix multiplications | 143.266 | 145.071 | 146.980 | 153.159 |
| 20,000 MPI Allreduces, two ranks | 80.206 | 83.449 | 80.342 | 87.741 |

All 2,000 Python calls and returns are retained, with 4,005 total journal events
and no drops. Block recording reduces this journal from 1,949,060 to approximately
1,365,500 bytes, or 30%. Both project-scoped and all-scope recording are
approximately 42% faster than the baseline on this fixture. Replacing
per-character control-character checks with equivalent regular-expression
validation removes a measured hot spot without removing validation or events.

**The requested ≤2× tight-call overhead target is not met.** The project-scoped
recorder takes roughly 292× the unrecorded fixture time. Scope exclusion avoids
recording unrelated library frames, but it cannot remove the cost of recording
each admitted application call. The implementation retains the required event
fidelity and durability instead of dropping events to meet a timing target.

The NumPy project measurement is about 2.6% slower than recording off (the
all-scope measurement is 6.9% slower). The MPI project median is about 0.2%
slower, with overlapping trial ranges: 76.734–85.852 ms recorded and
73.772–91.708 ms unrecorded. Three local trials cannot establish
negligible overhead. Neither measurement supports a claim of “no measurable
native impact.” Python control
loops are included; native instruction stacks and device kernels are not.

For the fully retained Python fixture, peak process RSS is 15.75 MiB after the
change and 15.62 MiB at baseline. Separate Python allocation peaks are 1.011 MiB
and 1.013 MiB respectively. These are local measurements, not memory budgets.

## Ten-million-call budget exhaustion

The separately bounded diagnostic is recorded in
[`automatic-instrumentation-10m.json`](automatic-instrumentation-10m.json).
It uses one repetition, the same numerical answer, and the unchanged default
64 MiB process budget. It is a resource-limit check, not a statistical timing
study or a full-fidelity recording benchmark.

| Mode | Wall time | Retained call entries | Call coverage | Final status |
| --- | ---: | ---: | ---: | --- |
| Recording off | 0.897 s | — | — | Completed |
| Baseline | 28.349 s | 68,731 | 0.687% | Partial: budget exhausted |
| Project scope | 3.194 s | 97,904 | 0.979% | Partial: budget exhausted |
| All scope | 3.434 s | 97,902 | 0.979% | Partial: budget exhausted |

The block format retains approximately 42% more events within the same budget.
Once that budget is exhausted, terminal-state guards disable eligible monitoring
callbacks instead of repeatedly constructing metadata for rejected events.
Peak process RSS is 16.125 MiB for all three recording variants.

**More than 99% of this fixture's calls are not retained.** Every recorded
variant remains explicitly partial and ineligible for the full-fidelity overhead
target. The producer's `dropped: 1` records the first rejected event; it is not an
exact count of all subsequent unobserved execution. The fixture supplies a known
call count so the report can expose that distinction. No synthetic missing events
are inserted, and no unavailable execution history is claimed.

## Client costs and coalescing

Client-only measurements are repeated after collective failure handling is
finalized, independently of the recorder measurements above. Their source
hashes and complete samples are in
[`automatic-instrumentation-client.json`](automatic-instrumentation-client.json).
After these timings, one identity guard was added so a raw-fork child never
queries an inherited MPI/PyTorch runtime. It adds a false branch during the
healthy benchmark's one-time client construction and leaves its repeated-call
and writer paths unchanged. The qualification receipt maps the measured hash to
that later tested hash; these timings are not relabeled as measurements of the
later guard.

| Fixture | Median wall time | Persisted output | Fidelity check |
| --- | ---: | --- | --- |
| 100,000 calls without a decorator | 10.101 ms | None | Numerical answer unchanged |
| 100,000 calls with the inactive decorator | 17.044 ms | No files or writer thread | Numerical answer unchanged |
| 100,000 `metric()` calls, one name | 377.159 ms | One record, 99,999 coalesced | Last value 99,999; counts conserved |
| Eight threads × 10,000 calls, distinct names | 1,736.619 ms | 16 records, 79,984 coalesced | Every last value 9,999; counts conserved |

The inactive decorator adds approximately 69 nanoseconds per call in this
measurement. Its cached activation decision is invalidated by `configure()`.
“No-op” describes its reporting behavior; it does not mean
zero execution cost. The single-name burst finishes within one second and
produces one valid JSONL record. Threaded bursts take longer and still remain
within the documented rate ceiling. Each line parses independently, all accepted
calls are accounted for, and no final per-thread value is lost.

The active single-name fixture peaks at 15.5 MiB process RSS and 0.456 MiB of
separately measured Python allocations. The eight-thread fixture peaks at
15.75 MiB process RSS and 0.493 MiB of Python allocations. Thread contention and
metric validation remain visible costs even when writes coalesce well.

## Reproduction

Preserve the baseline outside the working checkout, then run the same harness
against both trees during an otherwise quiet test interval:

```bash
mkdir -p /tmp/raven-instrumentation-baseline
git archive b52cd93e2b7581eeff6e9f601ad13765f4618a0c |
  tar -x -C /tmp/raven-instrumentation-baseline
python scripts/benchmark_auto_instrumentation.py \
  --baseline-root /tmp/raven-instrumentation-baseline \
  --baseline-revision b52cd93e2b7581eeff6e9f601ad13765f4618a0c \
  --cases calls,numpy,torch,mpi,noop,metrics,metric-threads \
  --mpi-iterations 20000 --repeats 3 --memory-pass \
  --output /tmp/raven-instrumentation.json
```

Use an interpreter with the optional libraries already installed to qualify
those paths. A missing library produces a skip, never simulated framework
evidence. The ten-million-call case is separately selected with
`--cases calls-10m`; with the default 64 MiB recording budget, any exhausted
journal is partial and cannot qualify a full-fidelity overhead target.

Actual framework integration tests live in
[`test_instrumentation_qualification.py`](../../tests/test_instrumentation_qualification.py).
They launch unmodified shell entry points through Raven, inspect both rank
journals, and check coordinator-only collective metrics. Real MPI communication
also verifies that an invalid scalar on one rank and an injected disabled-writer
state on the coordinator preserve the subsequent collective sequence and barrier.
The failure is deliberately injected; this is not a live filesystem-outage test.
The source hashes, test results, library versions, and explicit skip are recorded
in [`automatic-instrumentation-qualification.json`](automatic-instrumentation-qualification.json).
Install PyTorch in the
test interpreter to exercise the two-rank CPU gloo test; GPU/NCCL and cluster
qualification require their own target environments.
