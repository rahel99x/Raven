# Report Python measurements

Use `import raven` in a Python application to report a measured value without
choosing a run directory or writing JSON. A Raven launch supplies the client in
the application's environment. Your source file and its recorded hash remain
unchanged.

```python
import raven

@raven.measure("loss", kind="objective")
def train_step(batch):
    return compute_loss(batch)
```

Replace `compute_loss` with the application's calculation. The decorated
function returns its original result. Reporting failures do not become
application failures; inspect the run's diagnostics and metric metadata when a
measurement is unavailable.

Launch the existing Bash script on Slurm:

```bash
raven launch job.sh --research experiment
```

For a workstation run without Slurm, explicitly add `--local`. For a lab-funded
job, add `--account LabX` using an account available to your login. These are
the application's resources and account, separate from Raven Heavy's worker.
Automatic Python tracing is an independent choice: add `--trace`; see
[stack tracing](stack-tracing.md) for runtime requirements and gaps.

## Choose the measurement's meaning

| Kind | Use for | Default view |
| --- | --- | --- |
| `objective` | Loss, reward, likelihood | Linear values and trend |
| `residual` | Solver convergence | Log scale; positive observations only |
| `throughput` | Samples/s, cells/s, operations/s | Linear values and rolling median |
| `progress` | Completed work out of a known total | Progress and conditional ETA |
| `conserved` | Energy, mass, charge | Drift relative to the first reported value |
| `error` | Distance from a reference | Log scale; positive observations only |
| `gauge` | Any other numeric observation | Linear values |

Kinds describe measurements. They do not establish scientific acceptance
thresholds or prove convergence. A log view cannot display zero or a negative
value; the original observation remains available in raw data. Raw, trend and
range views remain explicit choices. A coalescing count means intermediate
observations were omitted, not that those observations were zero.

## Report values, phases and progress

```python
with raven.phase("assemble"):
    matrix = assemble_matrix()

for iteration in range(iterations):
    residual = solve_iteration(matrix)
    raven.metric("residual", residual, kind="residual", step=iteration)
    raven.progress(iteration + 1, iterations, unit="iterations")
```

`phase` tags measurements inside its block and reports
`phase.assemble.seconds` on exit. Nested blocks use the innermost phase name.
Progress uses a positive total with `0 <= completed <= total`; ETA requires
enough observed progress and is unavailable when those assumptions fail.

A measured function can return one finite number or a mapping of stable names
to finite numbers. `step="auto"` counts calls. Sample expensive or frequent
functions before converting their result:

```python
@raven.measure("residual", kind="residual", every=50)
def iterate(state):
    return solver_iteration(state)
```

Use `seconds=` for a minimum reporting interval. Tensor/scalar conversion uses
`.item()` or a finite numeric conversion. A GPU tensor conversion can synchronize
the device, so select an appropriate sampling interval. Multi-element arrays,
strings, Boolean values, NaN and infinity are unsupported observations.

When returning a value is inconvenient, a watch observes named locals at a
completed return:

```python
with raven.watch(train_epoch, "loss", "val_acc", kind="objective"):
    train_epoch()
```

It does not enable line-by-line tracing. A local absent on a return path
contributes no value; yields, coroutine suspension and exception unwinds do not
contribute completed-return observations. The watch chains an existing profile
callback and applies to the current thread. Install it inside each thread that
needs it, and use the context manager or returned handle's `close()` to remove
it. `measure` and `metric` are simpler for concurrent producers.

## Use it outside a Raven launch

With Raven installed, all reporting calls are inert when neither a run directory
nor an explicit metrics destination is configured. They start no writer and do
not create files. For local development, choose an existing directory:

```bash
RAVEN_METRICS_FILE="$PWD/metrics.jsonl" python3 solve.py
```

Alternatively, call `raven.configure(path="metrics.jsonl")`. The client does not
create parent directories. `metrics.meta.json` is an optional sidecar containing
API version, kinds, units, coalescing and coverage information. Every JSONL line
remains a normal timestamped observation; metadata is never a header line.
If you change reporting environment variables after importing/decorating code,
call `raven.configure()` to refresh the inactive state. Reconfiguration attempts
to drain the prior writer; a `False` result means it is still blocked and no
replacement writer was started. Explicit reconfiguration also retries a prior
storage failure after its cause is corrected.

If the application environment has no Raven installation, vendor the client
next to the application once:

```bash
raven report vendor-client --root "$PWD"
python3 solve.py
```

This copies the standalone, standard-library-only client as `raven.py` and records
its API version and SHA-256 under `metrics_client` in `.raven/config.json`.
`--path app/raven.py` selects another project-relative location. Existing
configuration keys are preserved. A modified or untracked destination is refused;
review it before deliberately replacing it with `--force`. An unchanged recorded
copy can be upgraded by running the same command again. Files are published
atomically; an interrupted update may require rerunning the command after
checking the stored hash.

A vendored `raven.py` takes import precedence when placed next to a script. It
provides the metrics API, not the full Raven dashboard package. Launch readiness
reports the detected client and possible shadowing; remove or relocate a stale
vendored copy when the application needs other Raven modules.

## Multiple processes and frameworks

| Program | Reporting pattern |
| --- | --- |
| Script, `python -m`, shebang | The same import and calls in the application. |
| PyTorch | Return a scalar from a sampled `train_step`, or call `metric` after computing a value. |
| DDP / `torchrun` | All ranks execute the same code; rank zero writes by default. |
| MPI / `srun` / `mpirun` | All ranks execute the same code; the discovered coordinator writes by default. |
| DataLoader / process pools | Helper children are suppressed by default; report aggregate results in the parent. |
| Threads / asyncio | Calls share a thread-safe writer; nested phase context remains local to the calling context. |
| Lightning / Keras / HF Trainer | Call `raven.metric` from the application's callback. Framework interception is not automatic. |

Rank discovery first uses an already initialized PyTorch or MPI runtime.
Without one, it checks `RANK`, `OMPI_COMM_WORLD_RANK`, `PMI_RANK`, then
`SLURM_PROCID`. Launcher-specific identity takes precedence over stale rank
variables inherited from an outer Slurm allocation. Discovery does not import
or initialize those frameworks. Use `raven.configure(all_ranks=True)`
only when separate rank streams are intended. Those streams use
`metrics.rank-N.jsonl`, with their own metadata and index entries. Explicitly
enabled helpers without unique ranks add a PID suffix to prevent collisions.
They are not
an inferred aggregate of the distributed job.

For an explicit global reduction:

```python
# Every participating rank must execute this call in the same order.
raven.metric("energy", local_energy, kind="conserved", step=step, reduce="sum")
```

`sum`, `mean`, `min` and `max` use the application's initialized MPI or PyTorch
communicator. **A reduction is a collective at the call site:** every rank must
participate in the same order. Skipping a call, conditionally reporting only on
rank zero, or exiting early can block other ranks. Raven never inserts an
unsolicited collective and never initializes a communicator. Without a usable
communicator it records coordinator-only coverage; that value is not a global
sum or mean. An explicit reduction performs a readiness agreement followed by
the value reduction. If any rank supplies an invalid scalar or metric field,
all participants omit that observation. A local recording failure does not
remove the rank from later collectives.

Keep `every=N` schedules identical across ranks. Wall-clock `seconds=` sampling
with `reduce=` is refused because independent clocks could make one rank skip
a collective. Missing calls, crashed ranks, or different reduction operations
still require the application communicator's failure handling.

The [runnable examples](../../examples/automated-reporting/README.md) include
CPU DDP, mpi4py and a process pool. Their optional runtimes must be installed in
the application's environment. Offline tests with shaped rank variables do not
qualify real multi-node launch, NCCL, GPUs, or a cluster filesystem.

### Register an existing framework callback

These callbacks read values already produced by the framework. Register the
callback with the application's existing Trainer or `fit` call; Raven does not
inject one automatically.

```python
# PyTorch Lightning: Trainer(callbacks=[RavenMetrics(), ...])
from lightning.pytorch import Callback

class RavenMetrics(Callback):
    def on_train_batch_end(self, trainer, module, outputs, batch, batch_idx):
        loss = outputs.get("loss") if isinstance(outputs, dict) else outputs
        if loss is not None:
            raven.metric("loss", loss, kind="objective", step=trainer.global_step)
```

```python
# Keras: model.fit(..., callbacks=[RavenMetrics(), ...])
from keras.callbacks import Callback

class RavenMetrics(Callback):
    def on_epoch_end(self, epoch, logs=None):
        for name in ("loss", "val_loss"):
            if name in (logs or {}):
                raven.metric(name, logs[name], kind="objective", step=epoch)
```

```python
# Hugging Face: Trainer(..., callbacks=[RavenMetrics(), ...])
from transformers import TrainerCallback

class RavenMetrics(TrainerCallback):
    def on_log(self, args, state, control, logs=None, **kwargs):
        if "loss" in (logs or {}):
            raven.metric("loss", logs["loss"], kind="objective", step=state.global_step)
```

These forms are application integration examples, not certification of every
framework version. Default coordinator-only reporting performs no collectives;
add a reduction only if the callback executes on every participating rank in
the same order. Dask, Ray and joblib helper suppression depends on observable
multiprocessing parent/fork identity; an arbitrary remote worker name alone
cannot establish that it is a helper.

## Cost, persistence and manual alternatives

The client queues observations and a daemon thread appends complete JSONL
records. A flush is requested every second or 64 records. Within a window,
latest values can replace intermediate observations; the sidecar reports those
counts. Names and retained work are bounded, with at most 64 names and a
20-observations/s per-name ceiling. This is telemetry, not an audit log of every
function call.

Normal interpreter shutdown attempts to drain the queue. Forked children reset
inherited pending state instead of flushing their parent's observations. A crash,
forced exit, or unavailable filesystem can lose buffered data. The flush interval
is a scheduling target, **not a hard durability guarantee** on a slow filesystem.
Storage errors are reported once and turn reporting into a counting no-op.

Existing manual producers remain supported: `raven.metrics.write_metric` writes
to an explicitly supplied file, and the Bash `raven_metric` helper reports from
non-Python jobs. Their synchronous error behavior differs from the optional
micro-API. See the [project standard](../PROJECT_STANDARD.md#live-metrics-metricsjsonl)
for the unchanged JSONL contract and [Connect data](connect-data.md) to validate
a real sample.
