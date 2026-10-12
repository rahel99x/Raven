# Existing-script reporting and export example

This project runs a finite-series approximation of π. It contains an ordinary
Slurm/Bash script, an optional measurement producer, and a Python tracing hook.
No user-created Raven manifests are needed. Python 3.10+ and an installed Raven
command are sufficient for the local example. It is an integration check, not
a numerical benchmark or GPU workload.

| File | Role |
| --- | --- |
| `test.sh` | Ordinary batch script; also works without Raven. |
| `solve.py` | Computes actual values; uses the pinned helper when provided. |
| `traced.sh` | Adds `raven_exec` after application environment setup. |

## Prepare and preview

From your Raven checkout, copy into a new directory:

```bash
cp -R examples/automated-reporting /tmp/raven-reporting-demo
cd /tmp/raven-reporting-demo
raven launch test.sh --project "$PWD" --research experiment scaling forecast --gpu --trace --check
```

Choose another destination if that directory already exists. The preview
explains missing scaling context, pending-time observations, assigned-device
verification, and the tracing hook. It creates no run/export state and submits
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

Use the explicit hook on a supported CPython 3.12+ application runtime:

```bash
raven launch traced.sh --project "$PWD" --local --research experiment --trace -- --steps 3
```

The explicit `--trace-adapter python-legacy-profile` option supports reduced
Python 3.10+ profile semantics. Native, MPI, and GPU stacks are separate
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
