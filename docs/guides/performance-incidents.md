# Performance investigations and incident evidence

Raven can explain what a recording measured, what remains unknown, and which
measurement might help next. These tools use the retained recordings already
shown in Trace. Opening an investigation does not attach a debugger, request an
allocation, start a hardware profiler, or change a scientific job.

## Start from the selected run

Keep the run's cluster, scheduler job identifier, and attempt together. An
identifier such as `42` alone is insufficient: a scheduler may reuse it after
retention expires, and a restarted job can contain several attempts.

The public Python API reads the same verified journals and cached index as the
terminal and browser viewers:

```python
from raven.engineering import performance, diagnosis

identity = {"cluster": "my-site", "job_id": "42", "attempt": "launch-example-a1"}
query = performance.read_recordings(
    ["/path/to/run/.raven/traces/STREAM/manifest.json"],
    identity=identity,
    max_seconds=1.0,
)
report = performance.analyze(query, identity=identity)
advice = diagnosis.propose(report, budget={
    "max_overhead_percent": 2.0,
    "max_bytes": 2 * 1024 * 1024,
})
```

`read_recordings` accepts one to 512 explicit retained source paths. The initial
indexing loop has a configurable progress budget, at most five seconds; an
individual bounded index query or filesystem operation can finish after that
progress budget. It can return a partial result. Call again to continue reading
the same cached index, or let the normal Trace workspace continue refreshing.
Inspect `caught_up`, `truncated`, discovery errors, and source read status before
treating a query as a complete range. There is no Slurm polling dependency.

When supplying `start_ns` or `end_ns` filters, also choose an explicit `clock_id`.
An unqualified time range across independent clocks is rejected with a clear
diagnostic. A smaller `limit` bounds returned events as well as the existing
shared query budget; reaching it marks the result partial.

The installed Engineering command and Operations form expose these same public
functions through the service catalog. Use `raven engineering --help` for the
current command arguments. A read request is a JSON object containing the
function arguments above; a retained query JSON is passed as `query`, not as an
executable plugin or command.

## Read timing correctly

| Evidence | Raven can report | Raven cannot derive from this alone |
| --- | --- | --- |
| Python, host, or cooperative call boundaries | Inclusive elapsed duration and validated self elapsed duration | Consumed CPU time, blocking cause, or hardware efficiency |
| Explicit scheduling running slices | Recorded CPU execution unions per logical CPU | Throughput efficiency or unobserved execution |
| Explicit blocked, runnable, waiting, or off-CPU states | Recorded thread/task state durations | A partition of the whole job or physical-core idle time |
| GPU kernel/copy activity | Device/engine activity unions with original identity | Occupancy, achieved throughput, or another device's activity |
| CPU/memory counters | Original values, units, source, and recorded timestamps | Missing GPU values or a complete interval between samples |
| Samples | Statistical observations with their sampling scope | Exact entry/exit time, unsampled calls, or absence of waiting |
| Explicit flows/dependencies | Recorded links and calibrated endpoint timing bounds | A whole-program critical path from nearby timestamps |

CPU/memory counters remain available when GPU recording is missing. Each source
has independent availability and a missing-evidence explanation. Function
duration totals can overlap across recursion, threads, and processes; their sum
is neither job makespan nor consumed CPU time.

`clock_envelopes` contains one range per returned clock domain. Raven does not
sum independent node clocks. The optional `selected_id` argument adds the
existing causal-slice analysis, which preserves declared clock calibration,
validity, uncertainty, missing endpoints, and inconsistent ordering. A causal
link without a shared valid clock does not establish an elapsed delay.

Queries are limited by the existing workspace's event, byte, depth, and item
budgets. A bound is a maximum, not a promise that every combination of 20,000
large records fits. Narrow the clock, source, rank, thread, or time range when a
budget is reached. Original recordings remain unchanged.

## Save an incident bundle

An incident is an observed or explicitly supplied trigger plus related evidence.
It is not an automatic root-cause verdict. Keep OOM, node failure, preemption,
timeout, and application error distinct; their recovery choices differ.

```python
from raven.engineering import incidents

result = incidents.capture(
    "/new/private/incident-directory",  # must not already exist
    identity=identity,
    trigger={
        "kind": "oom",
        "clock_id": "EXACT_RECORDED_CLOCK_ID",
        "time_ns": 1234567890,
        "evidence": "Scheduler reports OUT_OF_MEMORY for this exact attempt",
    },
    query=query,
    scheduler={"identity": identity, "state": "OUT_OF_MEMORY"},
    logs=[{"path": "/path/to/explicitly-selected.log", "identity": identity}],
    samples=[{
        "identity": identity,
        "clock_id": "EXACT_RECORDED_CLOCK_ID",
        "time_ns": 1234567890,
        "rss_bytes": 1024,
    }],
    budget={"pre_seconds": 5.0, "post_seconds": 5.0,
            "max_bytes": 4 * 1024 * 1024, "max_events": 4096,
            "max_log_bytes": 256 * 1024},
)
print(result["path"], result["coverage"])
```

Choose a timestamp in the declared recording clock. Do not label a wall-clock
timestamp as monotonic or invent alignment with a different node. The export
retains full original intervals overlapping the selected window; it does not
clip function durations to manufacture shorter calls. Other clock domains are
counted as omitted, since this export does not assume a calibration.

Supported trigger kinds are `oom`, `node-failure`, `preemption`, `timeout`,
`application-error`, `rank-stall`, `gpu-error`, `slowdown`, and `manual`. The
description records the supplied observation. Raven does not infer a stall or a
GPU failure from absent samples.

The export writes `incident.json` and a completion receipt in a new mode-0700
directory with mode-0600 files. It never overwrites an earlier bundle. Input
logs must be bounded regular files; final symlinks, changes during reading, and
changes before publication are rejected or disclosed. Log read errors remain
visible. Explicit untimed logs have unverified chronology. No ambient
environment, directory scan, or unrequested file is collected. Common credential
forms are redacted; review a bundle before sharing because no general redactor
can prove all sensitive material absent.

Coverage reports retained and omitted events, other clock domains, sample/log
counts, source query errors, and input-query digest. Trace, scheduler, samples,
and logs have separate readiness states. Event and byte limits are hard bounds
for the saved evidence; the byte count includes the completion receipt.

Reopen a saved bundle with `incidents.load(result["path"], identity=identity)`.
This checks the completion receipt, content digest, byte budget, private owner
and permissions, exact attempt, and anchored no-follow file identity. An
interrupted publication or altered bundle is rejected. A matching digest proves
the saved bytes are unchanged relative to their receipt; it does not attest the
source's truth or establish a cause.

## Arm bounded pre/post retention in an application

Use the existing cooperative flight recorder when the triggering event occurs
during a run. This retains explicitly emitted metadata; it is not a hardware
sampler and does not automatically attach to other processes.

```python
from raven.engineering import incidents

with incidents.recorder(project_directory, identity=identity, budget={
    "pre_seconds": 5.0, "post_seconds": 5.0,
    "max_events": 4096, "max_bytes": 4 * 1024 * 1024,
}) as capture:
    with capture.span("training step"):
        capture.emit("phase", {"name": "forward"})
        # Run scientific work here.
    if application_detected_anomaly:
        capture.trigger("Application's declared anomaly threshold crossed")
```

Before the trigger, a bounded in-memory ring evicts older metadata. Triggering
publishes the retained prefix into Raven's ordinary journal. The first post-time,
event, or storage bound stops recording; scientific work continues. The receipt
reports evictions, actual retained pre/post counts, capture errors, stop reason,
and qualification status. Without a trigger, buffered metadata is discarded and
a receipt explains that outcome.

Bounded retention is not a measured runtime CPU-overhead guarantee. Qualify
event rate, storage latency, and instrumentation overhead with a representative
workload at the intended site before choosing a deployment budget. No GPU or
multi-node capture qualification is implied by the portable metadata tests.

## Experimental next-measurement advice

`diagnosis.propose` separates observed facts, missing evidence, hypotheses, and
proposed next measurements. Its deterministic greedy heuristic selects declared
measurements under additive overhead/byte estimates. It never launches a
collector, submits a job, alters settings, or claims that the chosen plan is
optimal. Every proposed measurement requires review.

The default candidates inspect recording coverage, explicit CPU scheduling
states, device activity, and clock calibration. Costs are estimates, not measured
savings. The actual overhead of combined collectors, information gained, and
site-specific availability remain qualification questions. A zero budget can
still suggest inspecting already retained coverage without new collection.

## Implementation and validation evidence

The smaller alternative was another independent trace summarizer and incident
format. The implementation instead composes the existing verified workspace
reader, resource unions, function/counter/flow analysis, causal slices,
`TriggeredCapture`, redactor, and no-clobber evidence publisher. This preserves
clock, thread/process lifetime, device identity, coverage, and failure semantics
already exercised by the trace viewers. Investigation cost is bounded linear
traversal plus the existing interval-union/statistics sorts; incident window
selection is O(n log n), with each source descriptor prepared once.

Focused checks live in `tests/test_engineering_performance_incidents.py`.
`scripts/benchmark_engineering_performance.py` compares the same five existing
analysis blocks with the composed service on equivalent fixtures. It reports
wall/CPU time, process RSS, and separate Python allocations. It does not simulate
live scheduler, remote I/O, GPU, or multi-node qualification.

Run the reproducible comparisons in separate fresh processes without competing
heavy tests:

```bash
python scripts/benchmark_engineering_performance.py baseline --iterations 30 --calls 1000
python scripts/benchmark_engineering_performance.py duplicate-validation --iterations 30 --calls 1000
python scripts/benchmark_engineering_performance.py service --iterations 30 --calls 1000
```

The duplicate-validation mode reconstructs an initial implementation's avoidable
second full input serialization. The delivered service computes its digest from
one canonical serialization. Compare it with both that initial composition and
the smaller existing-analysis-only baseline: the latter intentionally performs
less validation and reporting work, so an improvement over it is not claimed.
