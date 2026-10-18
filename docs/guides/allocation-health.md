# Check an allocation's local performance

Open Raven Heavy, then enter `:ops allocation-health`. This optional operation
checks Raven's verified Heavy worker allocation. It does not inspect or change
a selected scientific job, request another allocation, or run during startup.
Standalone Heavy uses its explicitly approved local worker and is identified
separately in the receipt.

Choose memory, scratch, MPI, or a listed combination. For scratch, create or select an existing
private directory on the filesystem you want to measure. Set the buffer size,
repeat count, excluded warmup count and time budget. Select a new directory for
the receipt, then inspect the review and confirm Apply. The review displays the
execution identity and peak/aggregate resource limits before any probe runs.
The default ten seconds is a budget, not a promised execution time.

Memory probes copy and verify a Python bytearray. Scratch probes write, flush,
fsync and read back a small anonymous temporary file. The largest buffer is
16 MiB, at most two buffers are retained, and each file is bounded by the
reviewed size. Reads may hit the page cache; these fingerprints do not measure
peak memory bandwidth or distributed-storage durability. They consume resources
alongside any other work sharing that node, so schedule them deliberately.

The existing workbench Cancel action terminates only the diagnostic task. It
cannot cancel the scientific job. Anonymous scratch files are removed when the
process closes or exits, including termination. Kernel I/O stalls can delay
termination. No automatic retry runs after a timeout or lost response: inspect
the destination's `health.json` and completion receipt before preparing another
review. An incomplete directory is not proof of success; choose a new
receipt directory for a reviewed retry.

## Read and compare the result

`health.json` records the verified worker/allocation identity, host, CPU model,
available CPU affinity, kernel/Python/libc versions, selected settings, warmup
and measured repetitions, raw seconds and bytes, data-verification results,
median/range and elapsed time. It labels uncertainty and diagnostic interference
explicitly. A completed receipt uses `raven.allocation-health/v1`.

Optionally supply a previous `health.json` as the baseline. Comparisons require
matching hardware, placement, library versions and probe settings. Memory-only
comparisons can span matching nodes. Scratch comparisons additionally require
the same host and scratch device: local device numbers cannot establish
cross-node filesystem identity. At least two repeated measurements and a relative range of at most 25%
are required before a duration above 1.5 times baseline is labelled unusual.
These thresholds are descriptive heuristics. A changed cohort, sparse result
or unstable repetitions cause comparison to abstain. An unusual observation
is a reason to investigate contention or configuration; it is not a causal
“sick node” diagnosis. The baseline is user-supplied evidence, not an attested
hardware reference.

## Optional same-host MPI probe

MPI is available only when the verified Heavy worker has at least two granted
CPUs, at least 256 MiB of memory, Linux anonymous-file and process-lifetime APIs,
and a trusted Open MPI 5 development/runtime installation. Raven refuses an
unavailable or incompatible toolchain during review. A one-CPU Heavy worker must
be relaunched with two CPUs before this probe can run; changing the scientific
job's CPU request does not change Raven's worker grant.

Choose **MPI** and, when needed, an explicit development prefix containing
`include/mpi.h` and `lib/libmpi.so`. A packaged Debian Open MPI development tree
is also recognized. Leaving the prefix empty uses bounded `mpicc --showme`
discovery. Raven does not download, install or execute a user-supplied probe.
Compiler wrappers, their selected compiler/libraries and the current user's
environment must be trusted. Shared writable toolchain paths are refused.
Review records file hashes and ownership; changes require a new review.

The fixed C program runs exactly two ranks on the authenticated worker's host,
using loopback TCP and no remote launcher. It exchanges 4 KiB–1 MiB messages,
verifies every received byte, and records excluded warmups, repeated raw timings
and the MPI library version. It does not launch an additional Slurm step.
These measurements cover same-host communication only; they say nothing about
inter-node fabrics, topology, GPU-aware MPI or scientific application scaling.

Compiler source, object and executable files use sealed anonymous files, each
bounded to 64 KiB. Open MPI session metadata uses a separate private directory:
individual files have a 1 MiB process limit, and the supervisor monitors a total
8 MiB/128-entry budget. The aggregate is a monitored threshold, not a filesystem
quota. Runtime output is bounded to 1 MiB. A separately owned cleanup process
watches the exact task lifetime and removes session metadata after completion,
cancellation or task death. Cleanup is verified before a successful return;
unverified cleanup remains an error. Kernel stalls and host failure can still
prevent prompt cleanup.

GPU probes require an additional qualified device adapter. Same-host MPI and
memory/scratch receipts do not establish GPU, interconnect or multi-node health.
