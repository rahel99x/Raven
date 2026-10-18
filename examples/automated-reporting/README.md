# Existing-script reporting and export example

This project runs a finite-series approximation of π. It contains ordinary
Slurm/Bash scripts, optional measurement producers, and automatic Python tracing.
No user-created Raven manifests are needed. Python 3.10+ and an installed Raven
command are sufficient for the local example. It is an integration check, not
a numerical benchmark or GPU workload.

| File | Role |
| --- | --- |
| `test.sh` | Ordinary batch script; also works without Raven. |
| `solve.py` | Computes actual values; uses the pinned helper when provided. |
| `solve_client.py` | The same scientific calculation using `import raven` measurements. |
| `traced-auto.sh` | Ordinary Python command; automatic attachment needs no helper. |
| `traced.sh` | Explicit `raven_exec` override after application environment setup. |
| `train_ddp.py`, `ddp.sh` | Real two-process CPU PyTorch DDP and mean-loss reporting. |
| `rank_mpi.py`, `mpi.sh` | Real mpi4py exchange and explicit collective reporting. |
| `pool.py` | Real process-pool calculation with parent-only aggregate reporting. |

## Start with the Python client

The primary Python integration is one import and a decorator or explicit call:

```python
import raven
@raven.measure("absolute_error", kind="error", step="step")
def measured_error(estimate, step): return abs(math.pi - estimate)
```

`solve_client.py` also reports `pi_estimate`, phase timing and progress. The client
coalesces fast intermediate telemetry; metadata records omitted-point counts.
`solve.py` deliberately retains the manual producer so every explicitly submitted
step remains available for the existing exact-observation/export example.
Choose that manual path when each submitted observation must be retained.

Raven supplies the Python client inside the launch. To run these applications
directly in an environment without Raven, first run
`raven report vendor-client --root "$PWD"` from the copied example directory.
The resulting `raven.py` is inert until a Raven run or `RAVEN_METRICS_FILE` is
configured. See the [client guide](../../docs/guides/python-metrics.md) for kinds,
rank policy, coalescing, and failure behavior.

## Prepare and preview

From your Raven checkout, copy into a new directory:

```bash
cp -R examples/automated-reporting /tmp/raven-reporting-demo
cd /tmp/raven-reporting-demo
raven launch test.sh --project "$PWD" --research experiment scaling forecast --gpu --trace --check
```

Choose another destination if that directory already exists. The preview
explains missing scaling context, pending-time observations, assigned-device
verification, and Python tracing prerequisites. It creates no run/export state and submits
nothing. WorkDir remains the directory from which you submit.

## Run on a workstation

```bash
raven launch test.sh --project "$PWD" --local --research experiment --export-on-finish \
  --json --receipt-file launch-receipt.json \
  -- --steps 8 --terms-per-step 2000
```

This explicitly executes here. Use Slurm on a cluster login host. The reporting
runtime is separate from the application's environment and does not depend on
an open dashboard. Local `--json` requires a new receipt file so application
stdout and stderr retain their original streams.

Capture the exact run ID, without guessing which directory is newest:

```bash
run_id="$(python3 - <<'PY'
import json
from pathlib import Path
receipt = json.loads(Path('launch-receipt.json').read_text())
print(receipt['run_id'])
PY
)"
printf 'Selected exact run: %s\n' "$run_id"
```

To inspect later, open `ravenl --site desktop`, connect this project, and select
the exact run in Research/Results. A local run has no invented Slurm job row.

## Run on Slurm or a lab account

Copy the project to shared storage visible on submission and compute hosts.
Run from its directory so `solve.py` is found:

```bash
raven launch test.sh --project "$PWD" --account LabX --research experiment \
  --export-on-finish -- --steps 8 --terms-per-step 2000
```

Replace `LabX` with an account granted to your username, or omit it for the
site default. Supply a required partition or QoS in the script or repeated
`--sbatch=--partition=NAME` options. This workload account is separate from
Raven Heavy preferences. Slurm's stdout/error directives remain effective.
You can close Raven after submission; recording continues within the job.

For a real GPU workload, request resources through site-supported Slurm
directives and add `--gpu`. The flag records supported assigned-device evidence;
it does not reserve a GPU or make this CPU example generate GPU activity.

## Export three ways

Reuse the exact `run_id` from the receipt above:

```bash
# Default: a new bundle beneath this job's project/exports.
raven export-data --project "$PWD" --run "$run_id" \
  --include application,summary --metric absolute_error

# Relative parent: missing directories are created within this job's project.
raven export-data --project "$PWD" --run "$run_id" \
  --include application --metric pi_estimate --metric absolute_error \
  --relative-dir figures/experiment-a/data

# Absolute parent: preview the actual host/path before writing.
raven export-data --project "$PWD" --run "$run_id" \
  --include application --absolute-dir /tmp/raven-figure-data --check
raven export-data --project "$PWD" --run "$run_id" \
  --include application --absolute-dir /tmp/raven-figure-data --json \
  > export-receipt.json
```

Choose a new receipt filename if it exists. Every export has a unique bundle;
previous outputs remain unchanged. The dashboard's **Export data** panel uses
the same engine. Explicitly requesting unavailable GPU data produces a partial
receipt and exit code 3 rather than a zero-valued graph.

Read and verify the last bundle with standard-library tools:

```bash
python3 - <<'PY'
import csv
import hashlib
import json
from pathlib import Path

receipt = json.loads(Path('export-receipt.json').read_text())
for destination in receipt['outputs']:
    bundle = Path(destination)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    for item in manifest['files']:
        path = bundle / item['path']
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(65536), b''):
                digest.update(block)
        assert digest.hexdigest() == item['sha256']
        if path.suffix == '.csv':
            with path.open(newline='', encoding='utf-8') as stream:
                for row in csv.DictReader(stream):
                    print(row['run_id'], row['timestamp'], row['metric'], row['value'])
PY
```

For plotting, use the selected numeric `value` against epoch `timestamp`.
The CSV contains original retained observations. Its README and manifest
explain identity, units, clocks, missing cells, source cutoffs, and coverage.

## Trace, failure, and recovery

Use an unchanged Python command on a supported CPython 3.12+ application runtime:

```bash
raven launch traced-auto.sh --project "$PWD" --local --research experiment --trace -- --steps 3
```

On Python 3.10/3.11, explicitly add
`--trace-adapter python-legacy-profile` for reduced profile semantics.
The existing `traced.sh` keeps the explicit `raven_exec` form. To select only
explicitly hooked commands, set `RAVEN_TRACE_AUTO=0` before launching that script;
the automatic setting cannot retroactively untrace a previously started command.
Native, MPI, and GPU stacks are separate
integrations; see [stack tracing](../../docs/guides/stack-tracing.md).

Keep evidence from a controlled application failure:

```bash
raven launch test.sh --project "$PWD" --local --research experiment --export-on-finish \
  -- --steps 5 --fail-after 2
```

The command exits **7** and retains its first two observations. The application
outcome and export outcome are independent. Inspect durable completion status,
then reconcile terminal attempts without submitting another workload:

```bash
raven export-data --project "$PWD" --reconcile --check --json
raven export-data --project "$PWD" --reconcile --json
```

Pre-start cancellation may leave only launch/scheduler evidence. A killed
supervisor can remain pending until exact terminal evidence becomes available.
No observer means no immediate export guarantee. Requeues use separate exact
attempts; select their own run IDs. Cancellation/requeue, GPU identity, MPI
scope, Slurm signals, and shared filesystem behavior require a small pilot on
the target cluster. Local acceptance does not establish those site properties.

See the [complete workflow](../../docs/guides/automated-reporting-exports.md)
for readiness, all Research selections, export contracts, and operational limits.

## Exercise real process and rank workloads

For the standard-library process pool, run `python3 pool.py` from a script
launched with `--trace`. Its spawned helpers have separate trace coverage but
are hidden from the default trace view; parent metrics report the aggregate.
`--trace-helpers` includes helper processes in the default trace view.

With PyTorch installed in the application's environment:

```bash
raven launch ddp.sh --project "$PWD" --local --research experiment --trace
```

This uses two real CPU `gloo` ranks. Both call `reduce="mean"` in the same order.
It requires no GPU and makes no claim about CUDA, NCCL, multi-node networking,
or the site's GPU resource policy.

With site-compatible MPI and mpi4py installed, submit the supplied two-task
script on Slurm:

```bash
raven launch mpi.sh --project "$PWD" --account LabX --research experiment --trace
```

Use an account available to your login, or omit it for the default account.
For a local MPI check, a Bash script may instead run
`mpirun -np 2 python3 rank_mpi.py` with `--local`.
The metric reduction is an application collective: every rank must participate
in the same order. A local optional-dependency test that skips missing PyTorch,
mpi4py or an MPI executable does not qualify these integrations; verify them
with the installed runtime and actual launch policy before a production run.
