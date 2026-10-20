# Trace lab: CPU, MPI, and CUDA experiments

Run a small scientific workload and inspect its Python execution in Raven.
Choose the standard CPU example, an MPI experiment, or a CUDA workload. Each
retains an exact launch receipt, Python trace streams, scientific measurements,
and results. The default pacing leaves approximately two minutes to explore the
live view, in addition to scheduling, startup, and computation time.

The CPU example uses Python's standard library and Raven's bundled metrics
client. MPI and CUDA are optional variants with the dependencies described
below. Automatic tracing requires CPython 3.12 or newer; Python 3.10/3.11 can use
the explicit legacy option. Raven supplies its metrics client to the workload;
the application's Python environment does not need a separate Raven install.

Install the current Raven release so `raven` and `ravenl` are on `PATH`, and
update this checkout. The Bash helpers also require `python3` 3.10 or newer on
`PATH`. `--python` selects the application's interpreter; it does not change the
Python interpreter running the helpers.

## Run on your Slurm workstation

After updating the installed Raven and checkout with `raven update`:

```bash
cd /home/rahel/Raven
bash examples/trace-lab/run.sh --slurm
```

The helper copies the example into a new private project under
`~/raven-examples/trace-lab/run-*`, submits one job, and prints its exact
`launch.json` receipt path. Each invocation gets a separate project, so another
run cannot replace its source or results. The default job requests **3 CPUs,
512 MiB, and 5 minutes**. The CPU example runs 80 steps on a 32 × 32 grid with two
pool workers and a 1.5-second pause between steps.

Use the receipt path printed by that invocation:

```bash
bash examples/trace-lab/view.sh /absolute/project/path/launch.json
```

The helper prints commands containing your actual project and run IDs, then
opens Raven. Follow those printed steps:

1. Enter the printed `:project ...` command and wait for the inventory to load.
2. Enter the printed `:run select ...` command to bind the exact attempt.
3. Open **Jobs**, select the job ID from the receipt, and choose **Trace** in
   Details. If it has finished, use **Recents** or **History** instead.
4. Select a process row to inspect the main process or a pool worker. The
   example requests helper visibility; **Show helpers** / **Hide helpers**
   toggles it later. For MPI, select the desired rank and PID; each rank has its
   own recording.

The **Timeline** view draws recorded nested calls. Zoom, pan, select a call, or
use `:trace find update_row` to inspect a particular function. **View in browser**
saves and opens a private offline timeline with the same retained evidence.
Browser snapshots are static; the terminal view follows the live recording.

To include software CPU-placement observations on Linux, start a new run with:

```bash
bash examples/trace-lab/run.sh --slurm --cpu-placement
```

This option also works with `--variant mpi` or `--variant cuda`. In Trace, choose
**CPU cores** to see the selected process's observed socket, physical core and
logical CPU lanes. Each point is a bracketed OS query from the emitting thread;
it does not prove continuous residency between events. Unobserved cores and
missing topology remain unknown. Existing recordings cannot recover placement
that was never recorded. Check `raven trace capabilities` on the execution host
for available capture APIs; see the [capture guide](../../docs/guides/trace-capabilities.md).

Keep the quotes in printed commands when a project path contains spaces. The
command palette accepts those quoted paths.

Raven Light is sufficient for this bounded trace view; opening it does not
request a separate Heavy allocation. Job scheduling can delay the start of
recording. An empty process list while the job is pending is expected.

The helper uses the project's private `raven-view.json` for submission and
viewing. This scopes the example to Slurm on the current host and preserves
your saved Raven connection and account settings. Private launch defaults also
keep unrelated recipes, `SBATCH_*` overrides, and inherited recording targets
out of this run. Choose its account and partition through the helper options,
or use the scheduler defaults. Each variant enables project Python tracing and
CPU/memory recording. The CUDA variant also requests GPU recording; CPU and MPI
leave it disabled. Automatic exports remain disabled.
Slurm configuration, module paths, credentials, and mandatory site policy still
apply. Your saved settings remain unchanged.

To print the exact instructions and paths without opening a dashboard:

```bash
bash examples/trace-lab/view.sh /absolute/project/path/launch.json --no-open
```

The viewer also avoids opening an interactive dashboard when no terminal is
available.

## Choose a workload

| Variant | Application requirements | Slurm request |
| --- | --- | --- |
| `cpu` (default) | CPython; standard library only | 1 task, 3 CPUs, 512 MiB, 5 minutes |
| `mpi` | `mpi4py`, an MPI runtime, and its compatible `mpiexec` | `--ranks` tasks, 1 CPU per task, 512 MiB, 5 minutes |
| `cuda` | A CUDA-enabled PyTorch build and an accessible NVIDIA GPU at execution | 1 task, 1 CPU, 2 GiB, 1 GPU, 5 minutes |

All variants use the same receipt-based viewer and History workflow. A requested
variant never silently substitutes another backend when its dependencies are
missing.

### MPI

Activate a Python environment containing `mpi4py` and the site's compatible MPI
runtime, then run:

```bash
bash examples/trace-lab/run.sh --slurm --variant mpi --ranks 2
```

Select an application interpreter and MPI launcher explicitly when needed:

```bash
bash examples/trace-lab/run.sh --slurm --variant mpi --ranks 2 \
  --python /absolute/path/to/python3 --mpiexec /absolute/path/to/mpiexec \
  --account LabX --partition YOUR_CPU_PARTITION
```

The batch script runs `mpiexec -n N` inside the allocation. Use this variant on a
cluster only when that launcher and its MPI runtime support the site's Slurm
allocation. The launcher, Python environment, runtime bundle, and copied
project must be accessible to the execution nodes. A successful import on the
submission host does not verify a multi-node launch, and this helper does not
provide a universal `srun` configuration. The helper accepts one to eight ranks;
two is the default.

In Trace, compare the rank and PID labels before choosing a stream. Independent
rank journals do not establish a distributed causal stack; automatic Python
tracing does not instrument MPI's native internals.

`mpi_heat_lab.py` partitions a 32 × 32 heat field by rows and exchanges periodic
halo rows. Follow `evolve_distributed` → `exchange_halos`,
`advance_partition` → `heat_lab.update_row`, and `collective_measurements`.
Rank 0 publishes the scientific metrics and final artifacts; every rank gets
its own trace. `results/summary.json` records the MPI version, world size, and
each rank's PID, node, and row range. The default 80 steps include 120 seconds
of deliberate pacing.

### CUDA

Activate an application environment with a CUDA-enabled PyTorch build, then
request a GPU job:

```bash
bash examples/trace-lab/run.sh --slurm --variant cuda
```

For a cluster GPU partition and lab allocation:

```bash
bash examples/trace-lab/run.sh --slurm --variant cuda \
  --python /absolute/path/to/python3 \
  --account LabX --partition YOUR_GPU_PARTITION
```

The preflight checks that PyTorch has CUDA support without requiring a GPU on
the submission host. The workload requires an accessible CUDA device inside its
allocation; it fails clearly if the device is unavailable. It does not run a CPU
replacement and label it CUDA.

Use Trace for nested Python calls that prepare tensors and dispatch GPU work.
Use the retained scientific measurements and GPU telemetry for their respective
observations. Automatic tracing does **not** record CUDA kernels, driver
internals, or a GPU execution timeline. GPU telemetry additionally depends on
the installed driver, device access, and available recording tools; missing
telemetry does not invalidate available CPU/memory data or Python traces.

`cuda_heat_lab.py` evolves a 128 × 128 periodic heat field in CUDA float64
tensors. Explore `evolve_cuda` → `advance_cuda_step` → `periodic_neighbors`, and
`analyze_cuda_snapshot` → `spectral_amplitude`. The latter computes Fourier
amplitudes with PyTorch's FFT. Its measurements use the same names as the CPU
example, and `results/summary.json` identifies the actual device, dtype, and
PyTorch version. `results/final-field.csv` retains the final field.

The small field and deliberate 1.5-second pauses can produce low GPU
utilization. This is a trace inspection example, not a GPU stress test.

## Inspect the completed run

Reopen the same receipt at any time:

```bash
bash examples/trace-lab/view.sh /absolute/project/path/launch.json --history
```

Select the receipt's job ID in History, then **Trace**. The project/run commands
remain the same after completion. Research **Experiment** shows scientific
measurements; the run's retained files remain available after the dashboard
closes.

For a terminal-only preview, including when Slurm has aged the job out of its
accounting window:

```bash
bash examples/trace-lab/view.sh /absolute/project/path/launch.json --inspect
```

This lists the exact run's registered processes and inspects one non-helper
stream by default: normally the main process for CPU/CUDA. For MPI, choose the
desired rank's displayed PID explicitly:

```bash
bash examples/trace-lab/view.sh /absolute/project/path/launch.json --inspect --process 12345
```

Use an actual PID from that run's list. The inspector verifies a bounded prefix;
it does not promise to print every event from a large recording. It never picks
another run because it is newer. The helper checks the registered stream and
manifest identities against the receipt before opening a recording.

The helper also displays the selected manifest path. Export that exact stream
to an external timeline viewer with a new output filename:

```bash
raven trace export /absolute/path/to/manifest.json \
  --format speedscope --output /absolute/path/to/new-trace.speedscope.json
```

`--format perfetto` is also supported. Exports retain their coverage limits and
refuse to overwrite existing files.

## Run on a cluster with a lab account

Run the helper on the cluster's submission host, with the checkout and generated
project on storage accessible to its compute nodes:

```bash
bash examples/trace-lab/run.sh --slurm \
  --account LabX --partition YOUR_CPU_PARTITION \
  --directory /shared/your-login/raven-trace-pilot
```

Replace the account, partition, and path with values approved for your login.
The destination must be new. `--account` charges this scientific job to that
account; it does not change the account used by an independently launched
`ravenh` worker. Personal Slurm installations can omit the account and partition
to use their scheduler defaults.

Load the site's Python module or activate its environment before running the
helper. Use `--python /absolute/path/to/python3` to select an interpreter that is
available on the execution nodes. Open the viewer on the project host; Raven's
trace view does not read local lookalike paths through an SSH monitoring profile.

## Run without Slurm

On a workstation where local computation is appropriate:

```bash
bash examples/trace-lab/run.sh --local
```

The optional variants also support explicit local execution when their
dependencies and devices are available:

```bash
bash examples/trace-lab/run.sh --local --variant mpi --ranks 2
bash examples/trace-lab/run.sh --local --variant cuda
```

This runs in the foreground and prints the receipt after the workload finishes.
Then use the printed viewer command:

```bash
bash examples/trace-lab/view.sh /absolute/project/path/launch.json
```

For a standalone run, the viewer defaults to the terminal trace inspector.
There is no scheduler job row, so Jobs/History cannot host this run's Trace
panel. To see its scientific measurements in Raven, open `ravenl --site desktop`
and enter the printed project/run commands. This selects the exact run in
Research **Experiment**.

On CPython 3.10 or 3.11, add explicit consent to the legacy profile adapter:

```bash
bash examples/trace-lab/run.sh --local --legacy-profile
```

The same option works with `--slurm`. Legacy profiling records Python calls and
returns, with reduced generator and exception-unwind distinctions.

## Explore the CPU workload

The main process advances a periodic grid. At intervals, spawned workers analyze
immutable snapshots; the coordinator gathers their observations and publishes
the scientific metrics. Look for these paths in the recordings:

| Process | Functions to explore |
| --- | --- |
| Main | `main` → `evolve` → `advance_step` → `update_row`: the diffusion stencil. |
| Main | `report_step` → `measured_residual` → `rms_difference`: a decorated measurement calling its numerical implementation. |
| Main | `analyze_snapshot`: submit snapshot regions and gather their results. |
| Pool worker | `analyze_region` → `summarize_region` → `histogram` / `spatial_modes`: nested analysis in a separate process journal. |
| Main | `numerical_checks` and `write_results`: validate conservation and temperature bounds, then publish complete artifacts. |

Research **Experiment** shows `residual`, `peak_temperature`, `total_heat`,
`thermal_variance`, `first_mode_amplitude`, and progress. The metric kinds
exercise residual, gauge, conserved-value, objective, and progress displays.
The initial residual is zero because no evolution has happened; a logarithmic
view correctly excludes that point.

The project's `results/summary.json` includes the parameters, process IDs,
snapshot histograms and Fourier amplitudes, measured elapsed time, and numerical
checks. `results/final-field.csv` contains one row per grid row, with no header.

Use **Events** to examine retained calls and reconstructed stacks. **Summaries**
shows matched retained durations. Those durations include waits and can overlap;
they are not CPU utilization or proof of causality between processes. Use the
independent CPU/memory recordings for resource measurements.

The pacing pause is deliberate. It gives the live view time to refresh and
keeps this example inexpensive. Changing it changes the workload duration;
**Pause view** in Raven pauses only the display. Closing the dashboard does not
cancel a Slurm job or stop its recording.

Adjust the workload after `--`, for example:

```bash
# Keep the live demonstration open for about three minutes plus computation.
bash examples/trace-lab/run.sh --slurm -- --steps 120 --pause 1.5

# A short installation check.
bash examples/trace-lab/run.sh --local --quick

# Short optional-backend checks in their intended allocations.
bash examples/trace-lab/run.sh --slurm --variant mpi --ranks 2 --quick
bash examples/trace-lab/run.sh --slurm --variant cuda --quick

# Exercise failed-run evidence deliberately.
bash examples/trace-lab/run.sh --local -- --fail-at-step 5
```

An intentionally failed run returns a failure status. Retain its receipt to
inspect the recorded work and failure; a nonzero exit is the expected outcome
for this last command. Run `bash examples/trace-lab/run.sh --help` for helper
options. The workload in `heat_lab.py` also accepts `--grid`, `--workers`,
`--seed`, and `--alpha`; the runner owns its output directory.

Workload options do not enlarge the variant's Slurm request. Keep at most two
CPU pool workers alongside the coordinator within its three allocated CPUs;
MPI's `--ranks` sets the requested task count. Leave time for computation,
tracing, and startup within five minutes for every variant. Longer pauses or
larger grids can exceed that limit. These examples are intended for small
inspection runs; prepare a suitably resourced batch script for larger studies.

## Scope and retained data

This is a tracing and reporting example, not a validated thermal model or a
performance benchmark. Python call events do not capture native instructions,
GPU execution, or every event that a process could emit after a recording
limit. Check each stream's status and coverage before interpreting a result.

The generated project contains its copied sources, exact launch receipt,
`runs/` inventories, trace journals, metrics, and outputs. Keep that directory
to revisit the run. Removing it removes its retained evidence; updating the
Raven checkout does not refresh or delete the copied experiment.

See [automatic tracing](../../docs/guides/stack-tracing.md),
[Python metrics](../../docs/guides/python-metrics.md), and
[recorded summary semantics](../../docs/guides/trace-summaries.md) for the
underlying contracts. Local verification does not establish live Slurm,
cluster filesystem, or GPU qualification.
