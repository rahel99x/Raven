# Trace capture across processors and platforms

Raven uses the same verified timeline model in its terminal and offline browser
views. A recording from an AMD, Intel, Arm, Marvell, Apple, or other processor
does not need a different viewer. What can be **recorded** depends on the runtime,
operating system, instrumentation, and hardware available where the job runs.

Check the local prerequisites without recording or changing a job:

```bash
raven trace capabilities
raven trace capabilities --json
```

Run this command in the job's execution environment when checking a compute
node. A report from a laptop or login node describes that host, not a remote
worker. The command reads local platform metadata, checks a few known tool names
and Linux performance-monitoring paths, and exits. It does not start `perf`,
attach to a process, request elevated access, reserve a Python profiling slot,
or change kernel settings.

For optional CPU placement, the report also checks the calling thread's
`sched_getcpu` interface and reads a bounded Linux topology snapshot. It does
not start continuous sampling or inspect another process.

## Choose a capture method

| Workload | Available Raven path | Evidence and limits |
| --- | --- | --- |
| Python on a POSIX host, CPython 3.12+ | `raven launch --trace` using `python-monitoring` | Python calls, returns, yields, resumptions, and unwinds in included code. Native library internals and accelerator execution remain unknown. |
| Python 3.10 or 3.11 on a POSIX host | Explicit `--trace-adapter python-legacy-profile` with `--trace` | Python call/return observations with reduced fidelity: the runtime does not reliably distinguish yield, return, and exception unwind. Existing profilers are not replaced. |
| Application-defined phases in Python | `raven.trace_recorder.Recorder.span(...)` | Exact boundaries for spans the application explicitly emits. Uninstrumented work remains unknown. |
| Instrumented C/C++ on Linux | `raven trace native --sdk-header`, then compile and launch with `raven trace native` | Explicit SDK events. The current implementation uses Linux process identity, thread IDs, and an inherited Unix socket. It does not automatically reconstruct native stacks. |
| Selected C MPI collectives on Linux | `raven trace mpi --source`, compiled against the application's MPI runtime and native SDK | `MPI_COMM_WORLD` Barrier, supported Bcast types, and scalar Allreduce with SUM, up to 256 ranks. Unsupported communicators and thread modes produce explicit gaps. |
| An explicitly owned Linux process on a verified Heavy host | [Process observations](process-observations.md) | Sampled process/thread counters and observed states. This is not stack capture or a reconstruction of every scheduling event. |
| CPU placement of an instrumented Python process/thread on Linux | Add `--trace-cpu-placement` to `raven launch --trace`, or use `raven trace run --cpu-placement` | Logical CPU observations bracketed in time, mapped to socket and physical-core IDs where Linux exposes them. This is not continuous residency. |
| CUDA, ROCm, Metal, or other accelerator kernels | Host-side Python or cooperative spans only | A host call may enqueue asynchronous work. Its duration is not kernel execution time. Raven has no universal device-instruction capture adapter. |

For a practical Python, MPI, or CUDA workload, use the
[trace laboratory examples](../../examples/trace-lab/README.md). The capability
report's `runtime-present` status means an API is present, not that a capture
has passed qualification. Launch still checks permissions, monitoring-slot
availability, attempt identity, and recording limits. `requires-build` means
the application needs explicit instrumentation and a matching toolchain.

## Inspect a process by CPU socket and physical core

CPU placement is a software observation independent of instruction tracing.
On Linux, Raven asks which logical CPU the emitting thread occupies at an
event-time observation, then looks up the CPU's socket and physical-core IDs
in a topology snapshot captured before recording. This path uses the operating
system interface on AMD, Intel, Arm, Marvell, or another processor; it does not
select capture behavior by vendor name.

Enable it for a retained launch:

```bash
raven launch job.sh --trace --trace-cpu-placement
```

The same opt-in exists for an explicit Python recorder:

```bash
raven trace run --cpu-placement --project "$PWD" -- program.py
```

In **Trace → Timeline**, select **CPU cores** (`:trace cores`) to inspect rows
by socket, physical core, and logical CPU for the selected process recording.
**Calls** (`:trace calls`) returns to nested functions. Filter by thread, then
select an observation to inspect its recorded CPU IDs, sequence number, and
query bracket. See [the tracing guide](stack-tracing.md) for timeline navigation
and opening the optional browser view.

On Python 3.10/3.11, also choose the explicit legacy adapter described above
(`--legacy-profile` for `raven trace run`). CPU placement is disabled by default
and does not change the job's affinity, bind threads, or reserve CPUs. It adds
work to recorded events, so measure its overhead with the intended workload.

A process can occupy several CPUs through its threads. A logical CPU is a
hardware thread exposed by the operating system; it is not necessarily a whole
physical core. A physical core is identified by **socket ID and core ID
together**, because the same core ID can recur on different sockets. In a
virtual machine, these are the topology identifiers exposed by the guest; they
do not prove the layout of the underlying physical machine.

Each observation includes the logical CPU and a monotonic time bracket around
the query. The recording can show where a thread was observed within that
bracket. Two observations on the same CPU do not establish that the thread
remained there between them, and observations on different CPUs do not identify
the exact migration time. A call interval can cross CPUs; its wall duration must
not be charged to a single core as measured CPU time.

The topology snapshot is bounded to 8,192 logical CPUs. Missing socket/core
identifiers, inaccessible sysfs, and CPUs introduced after that snapshot remain
unknown. macOS, including Apple silicon, currently has no Raven CPU-placement
adapter; the viewer must show unknown placement when no observations exist.
The capability report exposes topology completeness separately from the ability
to observe a logical CPU.

## Hardware capture is a separate capability

[Magic Trace](https://blog.janestreet.com/magic-trace/) demonstrates the detail
possible when hardware control-flow tracing and a suitable decoder are
available. Raven's timeline can make deeply nested recorded calls easy to
inspect without claiming that every CPU exposes equivalent hardware evidence.

| Technology | What it can provide | Current Raven status |
| --- | --- | --- |
| Intel Processor Trace | Compressed hardware control-flow evidence on supported Intel systems, with decoding dependent on binaries and execution context. | Capture/decoder adapter not implemented. A visible `intel_pt` PMU is only a prerequisite hint. |
| Arm CoreSight ETM | Hardware instruction/control-flow evidence on systems with a usable SoC trace path, kernel support, and access. | Capture/decoder adapter not implemented. `cs_etm` visibility is only a hint. Arm ISA support, a Marvell part, or another derivative does not guarantee access. |
| AMD Instruction-Based Sampling | Samples describing selected instructions and operations on supported AMD processors. | Adapter not implemented. IBS samples cannot establish a continuous history of every call. They are not an Intel PT substitute. |
| Apple Instruments and platform tools | Platform-specific profiling whose fidelity depends on the chosen instrument and permissions. | Adapter not implemented. Apple silicon does not imply a publicly accessible CoreSight trace path; finding `xcrun` does not prove Instruments readiness. |

Future capture adapters must declare their fidelity: explicit events, sampled
stacks, or decoded control flow. Sampling intervals, dropped records, unknown
boundaries, unavailable symbols, and clock domains must remain visible. A
sampled stack must never be drawn as proof that a function ran continuously
between two observations. Data from different hosts must not be aligned by
unverified monotonic timestamps.

## Platform and qualification boundaries

The viewer interprets portable Raven event records; it does not inspect machine
instructions. Python capture uses runtime APIs, with POSIX file locking and
private journals. Linux provides additional boot/process-start identity through
`/proc`. On other systems those identities may be `unknown`; the report does
not invent replacements or treat them as verified ownership.

The C/C++ SDK, PMPI transport, and process-counter sampler currently require
Linux. The native SDK is not a macOS native-capture implementation. Raven's
installed runtime targets POSIX; an exported offline HTML timeline can be
opened in a browser on another platform.

Linux and WSL2 support Raven's persistent local state. On macOS, dashboard use
currently requires `ravenl --no-state`; the presence of Python monitoring APIs
does not qualify managed launch or capture there. See the
[installation platform limits](../INSTALLATION.md).

Neither CPU brand nor a successful capability report is live qualification.
Tests that inject alternate architecture names check reporting policy only.
Live Apple silicon, Arm/Marvell systems, hardware tracing, remote permissions,
GPU execution, and production clusters require their own measured qualification.
The report deliberately keeps these unknowns separate from working portable
viewing and from already-recorded evidence.
