# Trace topology examples

Eight separate programs make Raven's thread, CPU, node and GPU views easy to
compare. Each evolves the same periodic heat field through nested functions,
records scientific measurements, validates its result and keeps a private run
receipt. Normal runs include **120 seconds of paced computation** so there is
time to inspect Live view. Startup, scheduling and final validation add time.

| Program | Actual execution | Default Slurm request |
|---|---|---|
| `single_thread.py` | One scientific thread | 1 node, 1 task, 1 CPU |
| `multi_thread.py` | Two cooperating worker threads | 1 node, 1 task, 2 CPUs |
| `single_core.py` | Two worker threads pinned to one allowed logical CPU | 1 node, 1 task, 1 CPU |
| `multi_core.py` | Two spawned processes pinned to distinct physical cores | 1 node, 1 task, 2 CPUs |
| `single_node.py` | Two real MPI ranks on one observed host | 1 node, 2 tasks, 1 CPU/task |
| `multi_node.py` | One MPI rank per node; at least two observed hosts | 2 nodes, 2 tasks, 1 CPU/task |
| `single_gpu.py` | One accessible CUDA/ROCm device and an explicit stream | 1 node, 1 task, 1 CPU, 1 GPU |
| `multi_gpu.py` | Two accessible devices with separate streams | 1 node, 1 task, 1 CPU, 2 GPUs |

CPU/MPI requests use 512 MiB; GPU requests use 2 GiB. All request five minutes.
`--workers` changes thread, process or GPU count where applicable. `--ranks`
changes single-node MPI count; `--nodes` changes multi-node count, with one rank
per node. GPU ordinals can represent partitions such as MIG devices; the summary
does not infer independent physical chips from visible device count.

## Launch on a workstation with Slurm

Update Raven and this checkout first. From the repository root:

```bash
bash examples/trace-topology/run.sh --slurm --example single_thread
bash examples/trace-topology/run.sh --slurm --example multi_thread
bash examples/trace-topology/run.sh --slurm --example single_core
bash examples/trace-topology/run.sh --slurm --example multi_core
```

Each command creates a different project below `~/raven-examples/trace-topology`,
submits one scientific job and prints its exact `launch.json` path. Use
`--directory /new/project/path` to choose a fresh destination. Existing projects
and results are refused rather than replaced. The helper uses private launch
defaults and a private view configuration, preserving your saved Raven setup.

Python tracing uses CPython 3.12 or newer. Select the application's interpreter
with `--python /absolute/path/to/python`. Python 3.10/3.11 requires explicit
`--legacy-profile` consent and has the documented reduced yield/unwind fidelity.
The frozen launch runtime supplies Raven's metrics and trace support; the
application environment does not need a second Raven installation.

For a short smoke run, append `--quick`: three steps, 64 cells and no deliberate
pacing. Override numerical settings after `--`, for example:

```bash
bash examples/trace-topology/run.sh --slurm --example multi_core --workers 4 \
  -- --seconds 120 --steps 80 --size 4096 --sweeps 4
```

Thread count and CPU-core count are different. Ordinary CPython's GIL limits
parallel Python computation in `multi_thread.py`. `multi_core.py` uses spawned
processes for actual CPU parallelism. Core examples require Linux affinity and
readable sysfs topology. They stay within the process's allowed CPU mask and
refuse to count SMT siblings as distinct physical cores. Slurm core jobs request
`--hint=nomultithread` and use an owned `srun --cpu-bind=cores` step. The site's
task/affinity or cgroup configuration must honor that binding; these examples
do not independently qualify scheduler enforcement.

Pinning covers the scientific phase. Local startup, cleanup and Raven's own
collectors can appear on other CPUs; select the scientific worker lanes when
comparing core placement.

## Run without Slurm

Use `--local` for the four CPU examples:

```bash
bash examples/trace-topology/run.sh --local --example multi_thread
bash examples/trace-topology/run.sh --local --example multi_core --quick
```

Local launches run synchronously. Open another terminal to view a normal
two-minute run while it executes. There is no scheduler Jobs/History row for a
standalone run; its receipt, project and retained recordings remain available.

## Use a lab allocation on a cluster

The login identity and billing account are separate. Load the site's matching
Python/MPI environment, then choose the lab account explicitly:

```bash
bash examples/trace-topology/run.sh --slurm --example single_node \
  --account LabX --partition CPU_PARTITION \
  --python /shared/environment/bin/python

bash examples/trace-topology/run.sh --slurm --example multi_node --nodes 2 \
  --account LabX --partition CPU_PARTITION \
  --python /shared/environment/bin/python --mpi pmix
```

Replace the account, partition, interpreter and MPI plugin with values supported
by your CARC, UCI RCIC or other site. Omit `--mpi` to use the site's default;
`srun --mpi=list` lists installed plugins. `mpi4py` must match that MPI runtime.
Preflight imports the `mpi4py` package without initializing MPI on a login node.
Application code, environment and project files must be accessible on every
execution node. No example obtains a nested allocation.

`single_node.py` verifies that every rank reports the same hostname.
`multi_node.py` verifies at least two distinct hostnames and fails collectively
when two local ranks are presented as two nodes. Rank zero alone writes the
scientific metrics and summary. Actual rank/PID/node/affinity information is
included in `results/summary.json`; numerical output is checked against the
serial stencil. These are demonstrations, not MPI scaling benchmarks.

For MPI on one workstation without Slurm:

```bash
bash examples/trace-topology/run.sh --local --example single_node --ranks 2 \
  --python /absolute/mpi-environment/bin/python --mpiexec /path/to/mpiexec
```

For site-specific QOS, reservations, typed GRES or launcher requirements, adapt
`job.sh` in your own submission workflow using Raven's existing
[`raven launch` options](../../docs/PROJECT_REPORTING_V2.md). Preserve the matching
node/task/CPU/GPU counts and pass the selected program its intended arguments.
The helper deliberately removes inherited `SBATCH_*` overrides so unrelated
shell settings cannot silently change its reservation.

These scientific-job resources are separate from Raven Heavy's worker resources.
Use the same receipt with `ravenl` for bounded inspection or your configured
`ravenh` dashboard; viewing does not charge another scientific experiment to
the lab account.

## Run one or multiple GPUs

Choose an application environment with GPU-enabled PyTorch and load the site's
driver/runtime modules. CPU-only PyTorch and missing devices fail clearly;
there is no CPU replacement labeled as GPU execution.

```bash
bash examples/trace-topology/run.sh --slurm --example single_gpu \
  --python /shared/gpu-environment/bin/python \
  --account LabX --partition GPU_PARTITION

bash examples/trace-topology/run.sh --slurm --example multi_gpu --workers 2 \
  --python /shared/gpu-environment/bin/python \
  --account LabX --partition GPU_PARTITION
```

The same programs support NVIDIA CUDA and AMD ROCm PyTorch builds through the
framework's device API. This host's validation used CPU tensor mathematics;
physical GPU capture requires a separate permitted hardware run.

One host thread queues all devices' work in their separate streams before
reading numerical results, making host operations visible to the framework
profiler while allowing asynchronous device execution. Result checks perform
the necessary readbacks; there is no device-wide synchronization added only to
make a trace easier to draw. The example does not claim a measured overlap or
speedup. It validates diffusion against an independent closed-form solution and
records actual visible ordinals, reported UUIDs and device properties.

GPU launches also enable optional framework activity recording using the
current job's verified frozen runtime. Every two cooperative steps exports and
registers a live activity window, up to 64 windows. Each report/import is limited
to 8 MiB, with the recorder's separate 256 MiB total retained-artifact budget.
Framework-internal buffers and temporary exports are tool-dependent. Available
device kernels, copies and CPU operators can then appear in the workspace's
GPU perspective. This is activity timing, not GPU device stack unwinding or
hardware occupancy measurement.

If profiling is unavailable, the scientific GPU computation continues and the
activity receipt explains the failure; Python and CPU/memory recordings remain
independent. `--no-activities` disables this optional profiler. Inspect the actual
receipt and source health before assuming device activity was captured.
For direct profiling and supported report formats, see the
[Trace Workspace guide](../../docs/guides/trace-workspace.md).

`single_gpu` and `multi_gpu` demonstrate one-node device placement. The separate
multi-node sample covers distributed CPU/MPI placement; no physical multi-node
GPU qualification is implied.

## View live or retained results

Use the **exact receipt printed by your launch**:

```bash
bash examples/trace-topology/view.sh /absolute/project/launch.json
bash examples/trace-topology/view.sh /absolute/project/launch.json --history
bash examples/trace-topology/view.sh /absolute/project/launch.json --no-open
bash examples/trace-topology/view.sh /absolute/project/launch.json --workspace
bash examples/trace-topology/view.sh /absolute/project/launch.json --browser
```

The viewer prints the actual `:project` and `:run select` commands. Select the
job in Jobs, Recents or History, then **Trace → Run workspace**. Use **Live Δ**
during execution and **Fit** or **Explore** after completion. The global
**Traces** destination can also open the retained exact manifest.

`--workspace` opens the private live range service directly; `--browser` also
requests browser opening. Both follow the project's exact attempt, including
later activity windows. Combine either with `--no-open` to print its actual
command without starting a service. Stop the serving command with Ctrl-C when
finished; this does not cancel the scientific job.

Choose Execution for node/process/thread lanes, CPU for reported core placement,
or GPU for recorded devices and streams. Linux CPU observations are points,
not proof of continuous residency. The optional **Live browser** button provides
zooming, hover timing, pinned details and the same retained index. On SSH hosts,
use the guide's loopback forwarding instructions.

Python calls and framework activities can have independent clock domains.
Select the recorded activity clock before choosing GPU lanes; unrelated clocks
are never silently aligned. On a finished run, use Fit or pause in Explore.

Results include `results/summary.json`, run-scoped scientific metrics,
CPU/memory samples and registered trace streams. GPU activity receipts and
windows are retained when enabled and available. An allocation that is still
queued has no execution recording yet; missing capture remains explicit rather
than being shown as zero work.

## Validation scope

The automated checks exercise real CPU threads/processes, affinity, numerical
equivalence, exclusive artifacts, MPI messages on one host, and launcher/receipt
identity boundaries. Optional GPU tests run only with actual devices. Multiple
physical nodes and GPUs require their own hardware qualification; simulated
topology or dependency mocks are not reported as live hardware evidence.
