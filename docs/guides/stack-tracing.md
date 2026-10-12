# Recorded stacks and distributed trace evidence

Use Raven to record a prepared Python program, receive cooperative native
events, reconstruct recorded stacks, inspect explicit causal links, and audit
supported collective signatures. A running local Python process can provide a
reviewed, nonblocking snapshot when `py-spy` is installed and job membership can
be verified. The table below lists each method's runtime prerequisites.

Coverage is limited to the selected instrumentation. Checksums and sequence
numbers verify journal integrity; they do not establish that an uninstrumented
function, device, process, or runtime emitted an event.
Storage exhaustion stops recording and marks the trace incomplete. A crash can
leave an unknown tail after the last durable checkpoint. Native socket writes
acknowledge transport acceptance, not per-event durable storage.

## Choose a capture method

| Method | Preparation | Recorded scope | Important limit |
| --- | --- | --- | --- |
| `tower trace run` | Launch a Python script with Raven under CPython 3.12 or later | Python call, resume, return, yield, throw-resume and unwind events, including observed threads | No native-extension internals, GPU execution or automatic fork-child capture |
| `tower trace run --legacy-profile` | Explicitly select the legacy adapter | Python profile call/return events on supported Python 3.10 or later | Cannot reliably distinguish generator yields, returns and exception unwinds |
| `tower trace native` | Compile explicit calls to the cooperative SDK | Events supplied by local C/C++ processes and threads | No automatic native unwinding; private inherited sockets do not cross SSH or `srun` automatically |
| PMPI source adapter | Compile the optional adapter against the application's MPI runtime | Automatic C-binding WORLD Barrier, scalar Bcast and scalar Allreduce/SUM metadata | At most 256 ranks; no thread-multiple, Fortran, nonblocking or arbitrary communicator coverage |
| `Trace → Capture current Python stacks` | Install `py-spy`, run Raven on the target process node, and review a verified job/PID | One non-atomic set of observed Python stacks | No previous history; permission or runtime support may prevent capture |

Rust, OCaml, MPI and GPU applications can report the same cooperative events
through an application adapter or C FFI. This is a protocol integration point,
not a claim that automatic adapters for those runtimes are installed. There is
no debugger, kernel permission change, extra MPI barrier or public TCP listener.

## Record a Python job from its start

Use the launch command inside the job's existing allocation:

```bash
tower trace run \
  --project "$PROJECT_DIR" \
  --job-id "$SLURM_JOB_ID" \
  --attempt "$TOWER_RUN_ID" \
  --max-mib 64 \
  -- train.py --epochs 20
```

Set `PROJECT_DIR` to an existing project directory. Use the exact Raven run ID
from project reporting, or an explicit scheduler `SubmitTime/StartTime` string
as `--attempt`. Optional `--slurm-submit` and `--slurm-start` fields let Raven
verify the scheduler attempt independently. Array tasks should use their exact
`array_job_id_array_task_id` identity, not an array parent. Specify `--job-id`
when the launch environment does not already identify the exact task.

The recorder prints its manifest path. Open Raven, select the matching job, and
select **Trace** in Details. Raven discovers registered traces under that
project. Automatic association requires a matching attempt or project run ID.
An ambiguous old job ID must not attach automatically.

```text
:trace
:trace attach /project/.tower/traces/STREAM/manifest.json
:trace refresh
:trace pause
:trace follow
:trace select 0
:trace clear
```

Explicit attachment displays identity verification status. **Pause** pauses the
view, not recording. Closing Raven does not stop a separately running recorder.
Do not change the sampling sliders to control this recorder: its input is
event-driven. Display refresh and event capture are separate.

Python monitoring needs an unused `sys.monitoring` tool ID. The legacy profile
adapter refuses to replace an existing profiler. Recording excludes its own
implementation. Forked Python children do not append to the parent's journal;
each child must start its own recorder. Native instructions, existing frames at
attachment, signal-handler completeness and unprepared children are outside the
declared Python coverage.

The event recorder displays each Python function's definition line. It does
not enable per-line instruction events, so that number is not a live program
counter. Use a current-stack snapshot when the presently observed source line
is needed. Physical thread stacks do not establish a complete logical async
task or distributed GPU stack.

## Capture a running Python process without restarting it

Open the **Capture current Python stacks** operation, or run:

```text
:ops trace-snapshot
```

Enter the running job ID, its local process PID, and the project directory.
Inspect the review, then apply it. Raven checks the live scheduler owner and
submit/start identity, node-local `scontrol listpids` membership, Unix ownership,
and `/proc` process start identity. It repeats these checks before capture and
rejects a process that changes during capture.

The adapter runs only:

```text
py-spy dump --pid PID --json --nonblocking
```

The command has a five-second timeout and a 1 MiB output bound. Raven neither
requests native unwinding nor changes `ptrace` policy. Nonblocking reads can
observe inconsistent frames. The saved journal is labeled **snapshot**, with
incomplete execution coverage. If `py-spy` is unavailable or the node cannot
verify job membership, Raven reports that limitation instead of attaching to
an arbitrary process belonging to the same user.

## Add cooperative application events

The Python SDK uses only the standard library:

```python
from tower.trace_recorder import Recorder

with Recorder(project_dir, job_id=job_id, attempt=run_id) as trace:
    with trace.span("load input", span_id="load") as load:
        load_input()
    with trace.span("calculate", span_id="calculate") as calculation:
        calculate()
    trace.link(load, calculation, relation="precedes")
```

`span()` returns the globally qualified `stream:span_id`. Supply unique span
IDs within a stream. Use `parent_id` to declare nesting. A `precedes` link means
the source finished before the target started. Do not use it merely because
two operations are related or overlap.

`Recorder.emit(kind, data, thread=...)` accepts bounded JSON metadata. It rejects
terminal control characters, oversized values, nonfinite numbers and excessive
nesting. It does not capture local variables or user data automatically. The
return value is false if recording has stopped. `flush()` establishes a durable
journal checkpoint; it can add filesystem latency. `close()` publishes a clean
end when recording has not failed.

Generate the standalone native header:

```bash
tower trace native --sdk-header > tower_trace.h
cc -D_GNU_SOURCE -pthread program.c -o program
tower trace native --project "$PROJECT_DIR" --job-id "$SLURM_JOB_ID" \
  --attempt "$TOWER_RUN_ID" -- ./program
```

```c
#include "tower_trace.h"

int main(void) {
    tower_trace_emit("span_start",
        "{\"span_id\":\"solve\",\"name\":\"solve\"}");
    /* application work */
    tower_trace_emit("span_end", "{\"span_id\":\"solve\"}");
    return 0;
}
```

The header works with C and C++ on Linux. It uses a private inherited Unix
`SOCK_SEQPACKET` descriptor. Packets are limited to 4096 bytes. Each PID/start
time/thread has a separate stream. The native collector uses one event loop,
not a background thread per application thread. The default stream limit is
64; `--max-streams` can select 1–256.

The SDK returns `0` for a successful transport write, `1` when recording is not
enabled, and `-1` for failure. Writes can block under backpressure. Do not call
it from signal handlers, allocation hooks, or sensitive internal locks. A
collector failure must not be interpreted as proof that all events were saved.
No device memory, MPI payload or tensor data is copied automatically.

For multiple nodes, launch the adapter beside each participating application
process through the site's ordinary allocation launch method. Use a shared,
authorized project path if Raven must discover all streams there. Node-local
storage is preferable for heavy recording, but must be staged explicitly before
another host can inspect it. There is no automatic remote transport service in
this release.

## Capture supported MPI collectives automatically

The optional PMPI adapter records supported calls without adding explicit SDK
calls to the application. Compile it with the **same MPI compiler and runtime**
used by the application. Existing profilers can also use PMPI or `LD_PRELOAD`;
check compatibility before combining them.

```bash
tower trace native --sdk-header > tower_trace.h
tower trace mpi --source > tower_mpi.c
mpicc -std=c11 -D_GNU_SOURCE -fPIC -shared tower_mpi.c -o libtower_mpi.so
```

Launch the native collector **inside each rank launch**. For example, adapt the
site's normal `srun` invocation as follows:

```bash
srun tower trace native \
  --project "$PROJECT_DIR" --job-id "$SLURM_JOB_ID" --attempt "$TOWER_RUN_ID" \
  -- env LD_PRELOAD="$PROJECT_DIR/libtower_mpi.so" \
  TOWER_MPI_COMM_GENERATION="$TOWER_RUN_ID" ./mpi_program
```

`TOWER_MPI_COMM_GENERATION` must be the same nonempty identifier on all ranks.
Use only letters, digits, `.`, `_` and `-`, with a maximum of 128 characters.
Do not reuse a generation for distinct MPI worlds in the same job attempt.
Multiple independent worlds require distinct identifiers.

The **Prepare trace launch** operation also accepts `adapter=mpi`, the compiled
library path, and the shared generation. Its command is a rank-local wrapper;
the user retains the existing scheduler allocation and MPI launcher.

The current automatic scope is:

- C bindings for `MPI_Init`, `MPI_Init_thread`, and `MPI_Finalize`.
- `MPI_COMM_WORLD` with at most 256 ranks.
- `MPI_Barrier`, scalar `MPI_Bcast`, and scalar `MPI_Allreduce` with `MPI_SUM`.
- MPI thread levels SINGLE, FUNNELED and SERIALIZED.
- Compatible built-in float, double, integer and byte datatypes listed in the
  generated source. No derived datatype interpretation.

The adapter records entry and observed completion. It does not add a barrier,
extra collective, payload copy, kernel counter, or device read. Unsupported
communicators, datatypes, operators, oversized worlds, or THREAD_MULTIPLE
produce an explicit gap. MPI functions outside this wrapper set are outside
its declared scope. A gap prevents an overall complete result.

This implementation has real two-rank Open MPI 5.0.7 tests for successful
Barrier/Bcast/Allreduce, unsupported MAX reduction, COMM_SELF, thread-multiple,
and missing generations. These tests validate actual wrapper execution and
unchanged program results on one host. They do not qualify all MPI ABIs,
multi-node transports, Fortran bindings, or production launch policies.

## Inspect causal delays

Open **Causal delay explorer**, or use `:ops causal-delay`. Select a trace
manifest. To combine registered process/rank streams, select the project
directory and provide the exact job ID and attempt.

The analyzer validates explicit span IDs, durations, nesting and dependency
edges. It rejects cycles and impossible same-stream finish-before-start edges.
It calculates a longest chain of recorded leaf-span durations. Nested parents
remain visible and are not counted twice.

This duration is not global wall-clock makespan or a predicted speedup. Raven
does not subtract timestamps from different nodes or infer clock alignment.
Missing edges, unsupported relationship semantics, gaps and incomplete spans
remain visible. The default operation budget is 20,000 events and 8 MiB across
at most 256 registered streams. The live reader continues incrementally; an
offline operation reports when its bounded input is partial.

## Audit collective signatures

Emit `collective` events with the following fields:

```python
trace.emit("collective", {
    "communicator": "training-world",
    "generation": "run-1-world-0",
    "sequence": 42,
    "operation": "allreduce",
    "members": [0, 1],
    "rank": 0,
    "dtype": "float32",
    "count": 1024,
    "reduction": "sum",
    "phase": "enter",
})
# Emit the same signature with phase="complete" after observed completion.
```

The communicator and generation must be agreed across ranks. A local MPI
communicator pointer is not a portable identity. Do not add collectives or
barriers only to feed this monitor. Route metadata through the cooperative
side channel.

Open **Collective protocol auditor**, or use `:ops collective-audit`. The
initial supported signatures are scalar AllReduce, Bcast/Broadcast and Barrier.
The auditor distinguishes disagreement, incomplete participation, unfinished
operations and unsupported semantics. Derived datatypes and variable-count
collectives require a future adapter. Missing ranks are not proof of deadlock.
Results never cancel a job or alter a collective.

## Project files and durability

```text
PROJECT/
  .tower/
    traces/
      index.json
      STREAM/
        manifest.json
        events.jsonl
```

`index.json` uses `tower.trace-index/v1` and explicitly binds relative manifest
paths to job, attempt and stream identity. The `tower.trace/v1` manifest contains
cluster, job ID, attempt, node, boot ID, PID, process start, stream, coverage,
state, committed sequence and committed byte count. It can also include the
project run ID and scheduler submit/start values. Identity changes require a new
stream; readers reject replacement or regressed checkpoints.

Each `tower.trace-event/v1` JSONL envelope contains `stream`, `seq`, `prev`,
`event`, and `sha256`. The event contains `kind`, `mono_ns`, `thread` and `data`.
The digest is SHA256 over the canonical sorted-key, compact, ASCII JSON envelope
without `sha256`. `prev` links to the previous digest. The first sequence is 1
and the first `prev` is 64 zeroes. Duplicate records in the stored journal,
sequence gaps, corruption and clock regression invalidate the committed prefix.
The pure analysis API can deduplicate matching transport replay; the writer
does not silently accept duplicates into its journal.

The recorder fsyncs event bytes before publishing the commit checkpoint through
an atomic manifest replacement. The default checkpoint interval is 50 ms or 64
events. UI reads use only this committed prefix. A crash between checkpoints
can lose later events. Filesystem durability also depends on the storage system.
The manifest state and `complete` flag describe only the declared recording
scope. A `COMPLETE` recording does not establish universal execution coverage.

## Resource and validation limits

Recording every Python frame adds work and data. Import-heavy applications can
produce tens of thousands of events before useful computation begins. Measure
overhead on a representative allocation and reserve explicit CPU, memory and
storage. Raven does not promise a fixed overhead percentage. Cooperative spans
let an application declare a smaller scope; this is a separate capture choice,
not a silent fallback from complete frame recording.

A local qualification workload used 200 Python calls, each with 15,000 integer
arithmetic iterations. With CPython 3.12 and three repetitions, median complete
process time was **0.211 seconds without recording and 0.415 seconds with
recording**. Each recorded run wrote 4,163 events and approximately 2.28 MB,
including Python import and launcher activity. This is about 1.97 times the
baseline time for that specific small workload. It is not a cluster performance
claim or a fixed overhead bound.

Reading that backlog with a 15 ms worker budget used 10–11 slices. Observed
slices reached 18.4 ms on the shared test host; a caught-up refresh took about
0.19–0.23 ms. The time budget yields between records and cannot bound filesystem
latency or OS scheduling. Reads and analysis stay outside rendering. A busy
recording can accumulate backlog, which the view reports instead of skipping
unvalidated history.

Automated checks exercise real CPython capture with threads, recursion,
exceptions, generators and async functions; compiled C/C++ programs with
threads and fork; durable journals; corruption; process identity; cancellation;
storage limits; multi-stream causal paths; and collective signatures. Snapshot
tests exercise real journals and `/proc`, with Slurm and `py-spy` execution
fixtures. This environment does not certify production Slurm, MPI, GPU devices,
all compilers/runtimes, distributed filesystems or site permission policies.

For installations with development headers, run the actual MPI adapter tests
with `python -m pytest -q tests/test_trace_mpi.py`. If only the Open MPI runtime
is installed, a matching development package can be extracted into a user-owned
directory for validation. Set `TOWER_TEST_MPI_INCLUDE` to its Open MPI include
directory and, if required, `TOWER_TEST_MPI_LINK` to the matching linker flags.
These test variables do not change application launch configuration.
