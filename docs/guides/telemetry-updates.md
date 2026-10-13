# Configure telemetry updates

Raven separates queue updates, measurement reads, and the job's recording
frequency. The top-right slider controls queue updates. **Setup → Heavy telemetry
update settings** controls the other readers and their request budgets. These
controls work in `raven`, `ravenl`, and `ravenh`; the active Light or Heavy mode
determines which collection profile applies.

## Choose a Heavy preset

1. Open **Setup → Heavy telemetry update settings**, or enter
   `:telemetrysettings`.
2. Select **Default**, **Medium**, **High**, **Ultra**, or **Custom**.
3. Read the effective intervals and any site-policy limits on the page.
4. Select **Save settings** to keep the choice for future launches.

Selecting a preset changes the active Heavy readers immediately. No restart or
replacement allocation is needed. In Light, the choice is staged until a
verified Heavy worker becomes active; releasing Heavy restores the Light
collection settings. Selecting a preset also resets the queue slider to that
preset's starting interval. Closing the page keeps live changes; saving is an
explicit, separate action.

The following values are requested intervals in seconds. A source is read only
when needed. Slurm publication frequency, site policy, shared request budgets,
and read duration can make new observations arrive less often.

| Reader | Default | Medium | High | Ultra |
| --- | ---: | ---: | ---: | ---: |
| Jobs and queue | 5 | 2 | 1 | 0.5 |
| Start estimates | 15 | 10 | 5 | 2 |
| History and recents | 30 | 15 | 10 | 5 |
| Nodes and partitions | 60 | 30 | 20 | 15 |
| Job details | 10 | 5 | 3 | 2 |
| Live CPU and memory | 2 | 1 | 0.75 | 0.5 |
| Accounts and fair share | 300 | 180 | 120 | 60 |
| Log following | 1 | 0.75 | 0.5 | 0.5 |
| Research metrics | 1 | 0.75 | 0.5 | 0.5 |
| GPU and retained traces | 1 | 0.75 | 0.5 | 0.5 |

Presets also supply finite rolling budgets:

| Budget per minute | Default | Medium | High | Ultra |
| --- | ---: | ---: | ---: | ---: |
| Controller requests | 180 | 240 | 360 | 600 |
| Accounting requests | 12 | 24 | 36 | 60 |
| Live statistics requests | 60 | 90 | 120 | 180 |
| File metadata reads | 240 | 480 | 720 | 1,200 |
| File reads, MiB | 16 | 32 | 48 | 64 |
| Collection subprocesses | 300 | 420 | 600 | 900 |

The controller's short-window burst allowances are 4, 6, 8, and 12 requests per
second respectively. These accommodate the queue slider while retaining the
rolling limits above; mandatory site ceilings still apply.

Existing budget usage survives a preset change. The editor reports mandatory
limits and the 1.5-second minimum for remote file reads. Faster presets do not
enable disabled sources, launch GPU probes, or change an application's recorder.
On a shared cluster, choose intervals within the site's monitoring policy.

## Edit Custom

Custom initially contains the Default values and retains its edits when another
preset is selected. Choose **Custom**, then **⚙ Edit**; ASCII mode shows
**[gear] Edit**. This is a structured editor for Raven's telemetry configuration.

Select a field with the mouse or Tab/arrows, then press Enter to edit it. Values
show their unit, allowed range, current effective setting, and Default/Ultra
reference values. The color updates as you type: white corresponds to Default,
with a continuous transition toward pink at Ultra. Shorter intervals and larger
budgets increase the indicated intensity. A percentage supplies the same
information without color. Invalid or incomplete values have an explicit
validation message and cannot be applied.

Typing changes only the draft and its preview color; it performs no file writes
or source reads. Press Enter to retain a field in the draft or Esc to cancel
that field edit.
**Apply custom** validates the complete draft and updates the active readers.
**Save settings** applies a valid draft and writes it for later launches.
**Reset draft to Default** changes the draft; it takes effect after Apply or
Save. **Cancel edits / Back** discards unapplied edits.

Saving updates only the active profile's `telemetry` preferences and preserves
unrelated configuration values. The page shows the destination path. JSON and
TOML are supported; saving TOML rewrites comments and formatting. An external
change to the same preferences is reported instead of overwritten.

For a versioned JSON configuration, the corresponding fields look like this:

```json
{
  "schema": "raven.config/v1",
  "telemetry": {
    "preset": "custom",
    "custom": {
      "queue_seconds": 2,
      "live_seconds": 1,
      "research_seconds": 1,
      "trace_seconds": 1
    }
  }
}
```

Merge these fields into an existing configuration; retain its connection,
account, and allocation settings. Omitted Custom values inherit Default.
The editor writes the complete Custom selection. The
[configuration schema](../schemas/raven-config.schema.json) lists all fields
and bounds. `collection.research_seconds` and `collection.trace_seconds` set
the corresponding base readers outside Heavy; Heavy uses its selected preset.

## Adjust the queue slider

The top-right control reports the actual queue interval and labels both
endpoints for the current mode and site policy. Move right for faster updates,
left for slower updates. Click, drag, wheel, and keyboard controls use the same
value. `:rate N` selects a position from 1 to 50; `:rate reset` restores the
starting interval. The position is retained in normal UI state.

For managed `raven`, `ravenl`, and `ravenh` sessions, the range spans the active
queue base through a tenfold faster request, subject to the site's minimum.
With the starter configuration, Light spans 60–6 seconds and Default Heavy
spans 5–0.5 seconds. A site minimum can narrow or fix the range. The displayed
interval changes with the effective request; request admission can still defer
a particular read. Other telemetry readers keep their own settings.

Native compatibility sessions retain the existing global slider behavior:
five seconds through 500 milliseconds for ordinary native sources, with slower
configured bases scaled by at most ten. The Heavy editor requires a managed
session. See [update controls](live-workbench.md#set-the-update-rate).

## Read retained recordings

Selecting or attaching a reported run requests its accessible retained telemetry
immediately. Subsequent reads run independently of Slurm queue and accounting
polling. Under normal admission, accessible recordings refresh every 1–2 seconds.
The default local interval is one second; remote reads use at least 1.5 seconds.
Older configurations with a slower trace interval are capped at two seconds for
bound recordings, subject to any mandatory site minimum. Heavy presets and Custom can adjust the reader within
its source and policy limits. An attached recording is checked at least every
two seconds even if an older generic trace interval is slower, unless a
mandatory site limit or a temporary admission deferral requires a longer wait.

Analytics Job series and the corresponding Jobs inspector show **Retained
samples loaded: N CPU/memory, M GPU**. Read failures include their actual reason
and identify cached counts as the **last successful read**. A temporary admission
or “retry later” notice stays visible and is retried after its requested delay;
it does not accumulate failure backoff.
Real I/O and parsing failures remain visible with their source identity.
CPU and memory observations are displayed when available even if GPU recording
is absent or failed. Missing GPU values remain unavailable.

Changing a read interval cannot create measurements that the job never wrote.
For example, a recorder writing every five seconds still produces one new
sample every five seconds when Raven reads it every second. Completed runs need
recordings made during execution. See [launch and recording
setup](automated-reporting-exports.md#inspect-the-exact-retained-attempt) and
[GPU diagnostics](gpu-detection.md).
