# Diagnose missing GPU measurements

[README](../../README.md) · [Desktop setup](../DESKTOP.md) · [Chart controls](charts.md)

Use this guide when a GPU job has no measurements or reports incomplete device
coverage. Raven verifies the job's allocation before collecting automatic GPU
measurements. A GPU visible on the desktop does not establish job ownership.
The checks distinguish collection policy, allocation detection, driver access,
trace files, and retained data.

## Managed Raven collection

The installed `raven`, `ravenl`, and `ravenh` commands use managed collection.
With `collection.gpu = "existing-telemetry"`, GPU rate graphs read the selected
job's existing `<WorkDir>/logs/gpu-util-<jobid>.csv` NVIDIA trace. The job must
already have a producer writing that file. Raven reads its rows without launching
another Slurm step. See the [trace producer example](../reference.md#the-gpu-trace).
With `collection.gpu = "off"`, the managed GPU trace source is disabled.

Managed Raven does not start automatic device probes. `:gpu on` and provider
preferences do not enable allocation probes in this mode. A GPU visible on the
desktop, or a job with `gres/gpu=1`, establishes neither measured utilization
nor an existing trace. Missing measurements remain unavailable.

`ravenl --gpu-check JOBID` checks allocation metadata, existing traces, and
retained measurements under the same collection limits. It reports this mode
explicitly and does not launch vendor helpers or GPU sampling steps.

Slurm's `gres/gpumem` and `gres/gpuutil` accounting also needs an upstream
producer: NVIDIA `AutoDetect=nvml` or AMD `AutoDetect=rsmi`, compatible drivers,
and GPU tasks that produce counters. `AccountingStorageTRES` alone does not prove
measurements exist. Slurm describes `TRESUsageInAve`, `TRESUsageInMax`,
`TRESUsageInMin`, and `TRESUsageInTot` as aggregations of GPU **high-water marks**
across ranks. These values cannot supply instantaneous per-device rate curves.
See [Slurm GPU accounting](https://slurm.schedmd.com/gres.html#Accounting).
NVIDIA MIG devices do not expose these accounting utilization metrics.

For NVIDIA installations using `AutoDetect=off`, GPU allocation can succeed
while `sstat` and `sacct` contain no GPU counters. Before selecting
`AutoDetect=nvml`, verify that `gpu_nvml.so` exists in the `PluginDir` reported by
`scontrol show config`. The plugin must match the installed Slurm version and
have its NVML runtime dependencies available. A missing plugin requires a
compatible package or a Slurm build with NVML support; changing `gres.conf`
alone cannot supply it. Validate the resulting GRES configuration with
`slurmd -G` before scheduling new GPU work.

The NVIDIA CSV producer uses the driver directly and does not require Slurm's
NVML plugin. It supplies Raven's GPU curves independently of GPU accounting.
Run it inside the actual GPU job as shown below; completed jobs retain only
measurements that were recorded during their execution.

The allocation-probe controls below describe the native collector and the
standalone diagnostic helper. Their live-probe behavior does not apply to
managed Raven commands.

Optional NVIDIA, AMD, and Intel providers were introduced in Raven 4.14.
Use **GPU source** on Sources or `:gpuprovider` to choose one. See
[provider requirements](phase-one.md#select-a-gpu-provider) for compute-node
tools, allocation identity checks, and unsupported-counter behavior.

Numeric Slurm GRES order can differ from vendor device order. Without stable
device evidence, Raven accepts only a matching complete visible
single-vendor device set, with no partition or mixed-vendor ambiguity. Opaque
numeric subsets on shared multi-GPU nodes remain unavailable; faster polling
cannot resolve that identity limit.

For NVIDIA, Raven can resolve numeric `CUDA_VISIBLE_DEVICES` values to stable
device UUIDs inside the allocation. This supports a Slurm NVIDIA GPU on a
desktop that also has an AMD or Intel display device.

## Enable sampling on Fedora

The supplied `desktop` profile enables GPU sampling from Raven 4.7.1.
Earlier versions supplied that profile with sampling disabled.
A custom configuration or a saved off preference can still disable it.

1. Open Raven with the desktop profile.
2. Enter `:gpu on`.
3. Open Sources. Check that the GPU source is enabled.
4. If required, enter `:source gpu on`.
5. Select a running GPU job in Analytics or Jobs Details.
6. Wait for a GPU sampling interval.

**Expected result:** Valid observed utilization produces a GPU rate curve and an observed busy-mean curve.
Unknown counters stay unknown. They do not become zero utilization.
The default GPU polling request is five seconds.
Confirmed allocation counts stay available across ordinary queue refreshes.
Raven refreshes that evidence once per GPU polling round.
Changed job attempts or resource settings invalidate the cached count.
Failed discovery cannot extend it indefinitely: its lifetime is twice the effective GPU interval, bounded from 10 to 120 seconds.
The cache retains at most 10,000 current running jobs.
The first graph slider changes only the displayed time window.
The adjacent polling slider requests more frequent GPU measurements for that exact running job.
Its value and endpoints show effective intervals in `s`, `ms`, or `µs`; ASCII mode uses `us`.
GPU utilization and busy-mean curves share a probe, so the fastest request for either curve applies.
The sampling request remains active while Live is off and can remain active when the graph moves offscreen.
It resets when Raven restarts or the job attempt ends.

The global and per-metric polling controls span five seconds to 500 milliseconds.
The fastest request sets the shared GPU probe interval; the requests do not multiply.
GPU polling cannot request more than two polls per second.
Command duration and source retry backoff can delay measurements further.
Read the effective polling interval and source age below the graph.
The window slider spans 30 seconds to one second.
Right-click it to restore 30 seconds, or right-click the polling slider to restore its five-second request.
Both resets preserve the other slider and graph selection.
For a CSV trace, a faster polling request increases file reads; it cannot increase the job's trace-writing cadence.
See [chart controls](charts.md#follow-a-running-metric) for commands, shared-probe behavior, and source limits.

Use `:gpu off` or `--no-gpu` when you want sampling disabled.
The saved toggle belongs to the connection's state scope.
Source enable/disable controls apply to the running session and are not saved.

## Run the complete check

From a terminal with the same Slurm configuration and permissions as Raven, run:

```bash
raven --gpu-check --gpu-check-output ./raven-gpu-report
```

The output directory must not exist.
The check preserves existing files and does not replace configuration.
To inspect one job, use its exact individual ID:

```bash
raven --gpu-check 123 --gpu-check-output ./raven-gpu-job-123
raven --gpu-check 123_4 --json > gpu-array-task-123_4.json
```

Replace the example IDs with your actual job IDs.
The check selects the current scheduler user by default.
Use `--user USER` for another authorized scheduler user.

To bypass an old alias or custom configuration, run from the checkout:

```bash
./scripts/raven --config docs/config.example.json --profile desktop \
  --gpu-check --gpu-check-output ./raven-gpu-report-direct
```

The standalone check file uses the same implementation:

```bash
python3 scripts/check_gpu.py --config docs/config.example.json --profile desktop \
  --gpu-check-output ./raven-gpu-report-script

python3 scripts/check_gpu.py 123 --profile desktop
```

The installed command works without the checkout script.
Both entry points accept normal connection and configuration options.
Use `--host LOGIN` for a remote scheduler connection.
Use `--json` for machine-readable output.

`--fake` selects simulation. `--replay FILE` selects recorded evidence.
These modes do not verify live desktop hardware.
The report labels them explicitly.
`--record` cannot be combined with a GPU check.
A configured session recording path is ignored; the diagnostic writes only its requested report folder.

## Read the report files

| File | Contents |
| --- | --- |
| `report.txt` | Human-readable checks, next steps, and captured command excerpts |
| `report.json` | Structured `raven.gpu-check/v1` report, job evidence, limits, and results |
| `commands.jsonl` | One record per attempted command, with arguments, status, return code, duration, stdout, and stderr |

The check captures both successful commands and failures.
Each captured stream records its original byte count and whether the excerpt was truncated.
It preserves timeout output when the process supplied it.
Terminal output replaces control characters instead of executing captured escape sequences.

The new directory has private permissions. Each report file has private permissions.
Reports can contain job names, project paths, node names, and command output.
Review them before sharing.
The check does not dump environment variables, configuration secrets, or SSH credentials.

An exit status of `0` means the check completed without an error-level result.
Warnings can still explain why a job has no graph data.
An exit status of `1` means a check or report operation failed.
Invalid command syntax returns `2`.
Read `summary.jobs_with_graph_data` and the job-specific checks; a successful exit alone does not prove GPU data exists.

## Interpret the checks

| Check | Meaning and action |
| --- | --- |
| `sampling_settings`, `saved_sampling` | Shows configuration, `--no-gpu`, and the saved toggle. Use `:gpu on` when off. |
| `command_paths` | Reports tool paths on the scheduler connection host. Check that Raven uses your normal Slurm environment. |
| `host_gpu_inventory` | Runs the selected vendor helper on the connection host. This is hardware evidence, not job attribution. A GPU-free cluster login host is normal. |
| `host_gpu_provider` | Reports a provider-specific host inventory limitation. Inspect the named optional tool or driver. |
| `pci_gpu_inventory` | Lists display/controller devices when vendor inventory fails. `lspci` is optional. |
| `gpu_provider_setup` | Hardware was observed without usable vendor inventory. Check provider choice, matching utility/driver, Slurm GRES, and compute-node `python3`. |
| `queue` | Shows whether the controller returned your jobs. Resolve a queue connection failure first. |
| `allocation_batch` | Reads per-job allocation TRES. This detects `--gpus` allocations omitted by the per-node `%b` field. |
| `queue_gpu_underreported` | Allocation details show more GPUs than the queue lookup. Compare the captured resource fields and batch-query result. |
| `allocation_missing` | Slurm reports no GPU allocation. Check compute-node GRES and the job's GPU request. |
| `allocation_mapping_unavailable` | GPU counters or hardware can be visible, but Raven cannot prove which devices belong to this allocation. Inspect the exact Slurm job ID, GPU IDs, runtime visibility, and device UUID evidence in the captured `srun` reply. Read any CUDA visibility resolution error. |
| `not_running` | Pending or completed jobs cannot produce a new live sample. Check recorded traces or retained samples. |
| `live_samples` | Counts observed devices with valid utilization. Raven can record new graph samples. |
| `utilization_unsupported` | Devices exist but the utilization counter is unavailable. Inspect the selected vendor output and device/partition/driver capabilities. |
| `driver_unavailable`, `command_missing`, `permission_denied` | Identifies the original command failure. Fix the named prerequisite. |
| `allocation_probe_failed`, `probe_timeout`, `sampling_failed` | Shows why the allocation probe failed. Read the corresponding `srun` records. |
| `gpu_partial_coverage` | Some provider output or allocation mapping was unavailable. The diagnostic retains only verified device attribution. |
| `trace_read`, `trace_unavailable`, `trace_workdir_missing` | Shows the exact optional trace location, read result, and valid utilization count. |
| `retained_samples` | Counts valid retained GPU measurements in this connection's local series cache. Malformed and unknown readings do not count as measurements. |
| `job_not_found`, `job_details_identity` | Rejects missing or mismatched exact job evidence. Check the profile, user, and job ID. |
| `nonindividual_queue_ids` | Skips compressed array groups and malformed IDs. Specify the exact array task. |
| `job_limit`, `node_limit`, `diagnostic_budget` | Reports incomplete coverage caused by the stated limits. Run another exact-job check if required. |

Live sampling uses Raven's ordinary `srun --jobid ... --overlap` allocation helper
with the selected provider. It requires `python3` and the relevant vendor tool
on the compute node. Missing or ambiguous allocation ownership does not fall
back to node-wide SSH measurements.
It can create a short step inside an existing allocation.
It does not submit, cancel, requeue, hold, or change jobs.
The check does not start the dashboard, its plugins, or the sampler background loop.

## Check a Fedora allocation

When `nvidia-smi` detects a GPU but `allocation_missing` appears for every job:

1. Inspect the job's captured `TRES`, `AllocTRES`, and `TresPerNode` fields.
2. Inspect the compute node's captured `Gres` and `CfgTRES` fields.
3. Confirm that Slurm's compute-node GRES configuration registers the GPU.
4. Request a GPU in the job's batch file or launch command.

For a node with GPU GRES configured, use one suitable request:

```bash
#SBATCH --gpus=1
```

or:

```bash
#SBATCH --gres=gpu:1
```

Use the GPU type and count configured on your node when a typed request is required.
Adding a request alone does not configure Slurm's GRES or install a driver.
Use your installed Slurm version's `slurm.conf` and `gres.conf` documentation for that configuration.
The diagnostic deliberately leaves scheduler configuration unchanged.

`AccountingStorageTRES=gres/gpu` records allocated GPU resources in accounting.
It does not create live utilization measurements.
See [Fedora accounting](../FEDORA_ACCOUNTING.md) for the accounting setup.

### NVIDIA GPU with another display adapter

A Fedora desktop can expose an AMD or Intel display adapter together with an
NVIDIA GPU. Slurm can report the allocated GPU as numeric ID `0`, and its job
step can set `CUDA_VISIBLE_DEVICES=0`. The numeric IDs alone do not identify a
vendor or prove that vendor enumeration matches Slurm GRES order.

Raven resolves this case inside the job's allocation. A bounded helper uses
the CUDA driver to read the UUIDs of the devices visible through
`CUDA_VISIBLE_DEVICES`. It matches those UUIDs to the NVIDIA measurements.
The helper does not create a CUDA context or run GPU kernels. It requires the
NVIDIA driver library `libcuda.so.1`; it does not require the CUDA toolkit,
PyTorch, or an AMD utility to collect NVIDIA measurements.
Raven caches identity discovery for at most 30 seconds within the same job
allocation and node. A changed visibility mask or NVIDIA inventory invalidates
that identity evidence. Utilization and memory counters remain fresh on each
successful probe.

If `allocation_mapping_unavailable` persists:
1. Open the recorded `srun` reply in `commands.jsonl`.
2. Confirm that its job ID identifies the selected allocation.
3. Read the GPU IDs, runtime visibility, and CUDA visibility resolution error.
4. Check that the NVIDIA driver library is accessible inside that allocation.
5. Run the exact-job check again after correcting the reported problem.

Keep the visibility assigned by Slurm. Do not disable the other display
adapter or expose unallocated devices to bypass an identity error. Raven
continues to reject ambiguous ownership and physical MIG parent counters
that cannot describe an allocated partition.

After updating Raven, restart the dashboard. A successful GPU poll adds new
graph samples without clearing retained history. Past failed measurements
remain unavailable; Raven does not recreate them from current utilization.
The optional `gpu-util-<job_id>.csv` file is not required when live sampling
works. A missing optional trace does not prevent the live GPU graphs from appearing.

## Provide an optional GPU trace

Raven looks for this exact path:

```text
<WorkDir>/logs/gpu-util-<job_id>.csv
```

Use the full array task ID when applicable.
The queue or accounting work directory preserves spaces.
Opening Inspector is not required to discover the trace.

Run the trace writer inside the actual job environment:

```bash
mkdir -p logs
job_key="${SLURM_ARRAY_JOB_ID:-${SLURM_JOB_ID}}"
if [ -n "${SLURM_ARRAY_TASK_ID:-}" ]; then
  job_key="${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"
fi
nvidia-smi --query-gpu=timestamp,index,utilization.gpu,memory.used \
  --format=csv,noheader,nounits --loop=5 > "logs/gpu-util-${job_key}.csv" &
gpu_trace_pid=$!
trap 'kill "$gpu_trace_pid" 2>/dev/null || true' EXIT

# Run your application here.
```

Use a cadence appropriate for your application and environment.
This minimal format identifies devices by index and suits one node.
Do not combine multiple nodes with duplicate device indices into one unlabeled file.
Automatic multi-node sampling retains separate device identities.
An unavailable or removed trace clears its current chart; older session samples remain separate retained evidence.
Application JSONL metrics remain available in Research and are not automatically converted into scheduler GPU samples.

## Check the limits

The check probes at most eight jobs, prioritizing running GPU allocations.
An exact job check avoids that queue limit.
It inspects at most four nodes per job and requests at most four nodes for its diagnostic allocation probe.
Larger jobs can therefore have partial device coverage.
Queue discovery uses one batch request instead of inspection commands for every CPU job.

Command work has a 60-second budget and a 128-command limit.
Each captured stream retains at most 16 KiB.
Total captured stream evidence is limited to 1 MiB.
Trace reads inspect at most the last 1 MiB; retained series reads inspect at most the last 2 MiB.
Saved UI preference reads are limited to 1 MiB.
Unread or truncated portions do not establish that data is absent from the complete file.
These limits bound diagnostic command and file work; scheduler and filesystem delays can still affect total elapsed time.

A successful probe measures the system at that time.
After updating Raven, restart it and let the GPU source run.
The check cannot inspect the source-enable switch of a different running Raven process.
Use Sources in that process to verify its current source state.
