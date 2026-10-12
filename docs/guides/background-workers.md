# Background workers

[README](../../README.md) · [Controls](../CONTROLS.md) · [Display performance](ui-performance.md)

Raven supports sequential and concurrent execution of background tasks.
Use the toolbar worker switch to change modes during a session.
The display uses a separate UI thread in both modes.

| Mode | Result |
| --- | --- |
| Single | Run one background task at a time. |
| Multi | Run independent background tasks concurrently, within shared and per-source limits. |

The switch governs scheduler reads, GPU probes, Research work, log operations,
and configured notification commands. It does not change job allocations or
application threads inside a Slurm job. Polling intervals and source timeouts
remain in effect.

## Controls

| Control | Action |
| --- | --- |
| Toolbar worker switch | Request the other mode. |
| `:workers single` | Request Single. |
| `:workers multi` | Request Multi. |
| `:workers toggle` | Reverse the most recently requested mode. |
| `:workers status` | Show the current mode and pending work. |
| `raven --workers single` | Start with Single. |
| `raven --workers multi` | Start with Multi. |

Use F8 and the arrow keys to reach the toolbar switch without a mouse.
Use Enter or Space to activate it.
The View menu also provides worker controls.

## Change mode

1. Select the worker switch.
2. If the control shows a pending change, continue using the interface.
3. Read the final mode after running work completes.

**Expected result:** The control shows the applied mode after any pending transition finishes.

Raven retains accepted tasks and their results. A change to Single first lets
running task groups finish, including their GPU child tasks and result
callbacks. Queued tasks then run with the Single limit. Switching does not
cancel, restart, or repeat those tasks. A second toggle changes the requested
destination while the first change is pending.

Single can increase data age when a slow command occupies the worker. The UI
continues to process input. Multi can improve throughput for independent I/O;
it does not make a Python rendering operation parallel.

## Save a preference

The interactive preference is saved with the session settings.
In a managed Raven profile, set `ui.worker_mode`:

```json
{
  "ui": {
    "worker_mode": "single"
  }
}
```

Add that field to the existing profile; see [effective configuration](../raven/README.md#inspect-effective-configuration).
The compatibility `raven` configuration uses a top-level TOML entry:

```toml
worker_mode = "single"
```

For a JSON configuration, use `"worker_mode": "single"`.
The default is `multi`. The `--workers` launch option takes precedence over a
saved preference for that launch.

## Diagnose a pending change

Use `:workers status` to inspect running and queued work. Open Sources to check
slow or failed scheduler commands. A pending change to Single waits for running work
to return; Raven cannot safely interrupt arbitrary Python plugin code.
Native Slurm and GPU commands retain their configured timeouts.

A mode change governs Raven's background executor work. The UI and timer
coordinator remain separate. External commands and plugins can create their
own processes or threads. Their internal concurrency is outside this switch.

See [display performance](ui-performance.md) for latency measurements and
[Controls](../CONTROLS.md) for launch and navigation commands.

To test transitions with simulated data and terminal input, run:

```bash
python3 -S scripts/benchmark_sustained_pointer.py \
  --source-root . --output /tmp/raven-worker-switch.json \
  --live --delta 5 --points 4000 --jobs 3 \
  --rates 250 --seconds 4 --workers multi --switch-workers --switch-control toolbar
```

The benchmark sends mouse press and release reports at the published toolbar
control during pointer traffic. It checks that both modes complete and that
the initial mode returns. Use `--switch-control command` to test typed palette
commands instead. The benchmark does not change cluster jobs. See the
performance guide for measurement limits.
