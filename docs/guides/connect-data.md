# Connect and verify measurements

[Everyday workflow](everyday-workflow.md) · [First job](getting-started.md) · [Complete format reference](../reference/project-reporting-formats.md)

Open **Setup → Recordings and metrics → Connect data and validate a sample**, or
enter `:connectdata`. This workflow checks existing observations, explains
missing inputs, and previews the actual values before attaching a chart.
It does not submit jobs, start recorders, or rewrite application files.

## Check an existing run

1. Select an exact project run. Its declared application-metrics file appears
   automatically. Choose **Validate sample** to repeat the check.
2. Read the separate CPU/memory, GPU, scientific-metrics, and tracing results.
   Each reports its inspected sample count, source, and actual error or missing
   prerequisite. One unavailable recording does not suppress another source.
3. Choose **Preview chart** to inspect the observed values, numeric range, and
   timestamp range. The compact preview uses sample order; horizontal spacing
   does not represent elapsed time. One point cannot establish a trend.
4. Choose **Open chart in Research**. Raven revalidates the source and keeps
   the selected run's exact identity.

To inspect a file independently, enter its path or use
`:connectdata /absolute/path/to/metrics.jsonl`. A manual file is explicitly
unassigned. Opening its chart leaves the previous run context; Raven never
infers ownership from a similar filename or modifies `run.json` to attach it.

An SSH connection reads the connected host's files through the existing
governed reader. Use an absolute remote path. Local files with the same path
are never substituted. Retained CPU/memory and GPU samples and application
metrics can be checked over SSH. Trace manifests can be verified there, but
journal-event inspection currently requires Raven on the trace host.

## Produce scientific measurements

Choose **Generate Bash / Python snippet**, select the language, and enter a
metric name. **Copy complete snippet** copies an adapter function that accepts
your measured value. It does not execute the function or invent example
measurements.

Paste the function into your existing script or application. For Bash, call it
after your program has set `measured_loss`:

```bash
report_measurement "$measured_loss"
```

For Python, call it after computing `loss`; the step is optional:

```python
report_measurement(loss, step=step)
```

Launch the existing script through **Launch job** with application reporting,
or from the project directory:

```bash
raven launch job.sh --research experiment
```

Raven supplies the exact attempt directory and its portable reporting helper
through `RAVEN_RUN_DIR`, `RAVEN_REPORT_PYTHON`, and `RAVEN_REPORT_HELPER`. The
generated Python function uses that helper; the application environment does
not need an installed Raven package. The Bash function checks that these
variables exist before writing. Both adapters propagate reporting errors to
the caller; decide how your application handles an optional reporting failure.

These adapters write the run's default `metrics.jsonl`. If your project already
declares an external metrics file, keep its existing producer and validate that
declared file instead. Use one reporting coordinator per attempt. MPI ranks
must not all append to the same stream independently.

## Required file format

Application metrics are UTF-8 JSONL: one JSON object followed by a newline for
each observation. The relevant fields are:

| Field | Required value |
| --- | --- |
| `t` | Finite, nonnegative Unix epoch seconds from the observation. |
| `metrics` | An object mapping stable names to finite numeric values; at least one value is needed for a chart. |
| `step` | Optional nonnegative integer. |
| `phase` | Optional printable phase label. |
| `progress.completed` | Required when `progress` is present; finite, nonnegative completed work. |
| `progress.total` | Required when `progress` is present; finite and positive, and at least `completed`. |
| `progress.unit` | Optional unit such as `iterations`. |

Omit unavailable measurements. `null`, Boolean values, numeric strings, `NaN`,
and infinity are not accepted measurements. Zero means a measured zero. Keep
names and units consistent across observations. Each record may contain at
most 64 metrics and 65,536 bytes. An incomplete trailing line waits for its
newline. Valid complete observations remain available alongside parse errors.

The preview reads at most 256 KiB per source and retains at most 128 points per
metric. Large files show a bounded tail, with at most eight metrics charted in
the preview. This check does not certify every record in a large file. Open
Research or export the retained records for further analysis.

## Understand independent readiness

| Family | What establishes readiness |
| --- | --- |
| Scientific metrics | A real, valid numeric application observation. |
| CPU/memory | Retained process observations with an available CPU or RSS counter. |
| GPU | Retained device observations with an available utilization or used-memory counter. |
| Tracing | Verified journal events for the exact attempt; an SSH manifest alone is shown as declared. |

Choose CPU/memory or GPU recording in Launch job for future attempts. GPU
recording also requires supported device access and a driver. Native CPU/memory
and GPU graphs use their recording contracts; writing a scientific metric
named `cpu_pct` or `gpu_util` does not populate those graphs. Missing past
observations cannot be reconstructed by enabling a reader later.

Tracing requires its application hook and a compatible adapter. After your
script activates its application environment, source `"$RAVEN_SHELL_HELPER"`
and invoke a supported Python script with `raven_exec python3 program.py`.
Select the adapter in launch defaults: Python monitoring requires Python 3.12
or later; Python 3.10/3.11 requires explicit legacy-profile selection. A trace
toggle cannot instrument every runtime, remote rank, or GPU kernel.
