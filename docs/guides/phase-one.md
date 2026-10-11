# Phase one: measurement and submission checks

[README](../../README.md) · [Controls](../CONTROLS.md) ·
[Research](../RESEARCH.md) · [Project standard](../PROJECT_STANDARD.md) ·
[Roadmap](../ROADMAP.md)

See the [release validation report](phase-one-validation.md) for test coverage,
performance measurements, and site checks.

Tower 4.14 adds four features: a sampling inspector, optional GPU providers,
local shell checks, and scientific array manifests. The remaining proposals
are planned work. This release does not add a stack tracer.

The core application still uses the Python standard library. External GPU
commands and ShellCheck are optional. A missing command produces an explicit
unavailable result; it does not prevent normal Tower use.

## Update an existing installation

From a clean checkout, run:

```bash
git pull --ff-only
python3 scripts/setup.py --mode local --profile desktop
./scripts/tower --version
```

Use `--profile carc` for the supplied CARC profile. Retain your site's existing
profile and operating rules. Existing managed `tower` aliases continue to use
the checkout. A package installation must also be updated through the method
used to install that package.

These setup commands do not install vendor tools, alter scheduler services,
or submit jobs. See the [runbook](../runbook.md) for installation variants.

## Inspect sampling capabilities

Click **Metric sampling** on Sources, Analytics, or Research Experiment. The
same controls are available when the supported workspace is shown in Jobs or
History Details. **Help → Metric sampling capabilities** opens the inspector
from the toolbar.

Use `:telemetry` for the current selected job, or `:telemetry JOBID` for one
exact job. With no selected job, it shows cluster capabilities. Use
`:telemetry refresh` or **Refresh** to request fresh evidence.

In the inspector, arrows, Page Up, Page Down, Home, and End scroll the report.
Tab changes focus between the report and buttons; Left/Right chooses a button
and Enter activates it. `r` refreshes, `v` starts a line selection, and `y`
copies the clicked or keyboard-selected line, the selected range, or the full report when no lines are selected.
Right-click clears the selection. `Y` or **Copy** copies the complete report.
Esc or **Close** returns without changing pages.
`:telemetry copy` and `:telemetry close` perform the same actions.
For a machine-readable report, use `tower --run 'telemetry JOBID'`.

The inspector separates:

| Evidence | Meaning |
| --- | --- |
| Tower read interval | How often Tower requests an observation from that source |
| Configured Slurm task interval | The controller's exposed task-accounting configuration |
| Job-requested or inherited interval | A job override or inheritance supported by the returned job record |
| Last successful Tower read | Collector health, not the age of every individual metric |
| Backoff and error | Why a collector can take longer than its requested interval |
| Capability | Observed, configured, unsupported, disabled, or unknown evidence |

The report includes CPU time, resident memory, virtual-memory capability, live
GPU measurements, GPU trace files, and project metrics. CPU, RSS, and GPU rows
use the selected job's read interval. The project-metrics row shows the baseline
file-read interval; an individual graph can request faster reads. Tower does
not add a virtual-memory graph through this inspector.

Slurm's configured interval is not proof of the effective producer interval.
An individual step can override it. If the job/step evidence is unavailable,
the inspector reports unknown timing. A zero accounting interval means no
periodic collection; it is not an infinitely fast sampler.

Reading at 500 ms does not make Slurm produce a fresh measurement every 500 ms.
CPU rates require changing CPU-time observations. MaxRSS is a peak, so an
unchanged value can be correct. Project metrics and trace files change when
their producers write them; Tower does not infer a write interval from polling.

The inspection runs in the background. Configuration evidence is cached for up
to 120 seconds per connection; Refresh bypasses that cache. A job report is
bound to its selected identity and available attempt evidence. Inaccessible
configuration, completed-job controller records, and unsupported fields remain
explicit. The inspector does not change Slurm's configuration.

## Select a GPU provider

Click **GPU source** on Sources, Analytics, or Research Experiment. The toolbar
also provides **View → GPU source selection** and **Help → GPU source selection**.

Use `:gpuprovider` to open the provider selector. Choose **Auto**, **NVIDIA**,
**AMD**, or **Intel**. Arrow keys change the focused choice; Enter applies it.
Click a choice to apply it with the mouse. Esc returns to the previous view.

Direct commands are also available:

```text
:gpuprovider auto
:gpuprovider nvidia
:gpuprovider amd
:gpuprovider intel
:gpu on
```

The saved provider preference defaults to `auto`. Profile configuration can
set `gpu_provider` to `auto`, `nvidia`, `amd`, or `intel`.
The provider choice does not enable a disabled GPU collector. Use `:gpu on`
where allocation sampling is permitted. The supplied CARC profile disables it
by default; the desktop profile enables it.

| Provider | Required command on the compute node | Data source |
| --- | --- | --- |
| NVIDIA | `nvidia-smi` | Structured CSV GPU query |
| AMD | `amd-smi` | JSON inventory and utilization/memory measurements |
| Intel | `xpu-smi` | JSON discovery and per-device statistics |
| Auto | At least one supported command | Available supported providers within the allocation |

The allocation helper also requires `python3` on the compute node. Installing
a vendor command on the login node alone does not make it available inside a
job. Tower does not install GPU drivers or vendor utilities.

Changes apply to subsequent allocation samples. Existing recorded history is
retained. Device records keep vendor and node identity; stable device IDs are
used when exposed. Inventory reads are bounded and cached separately from
measurement reads, for up to 30 seconds per node and job attempt.

Tower checks the allocation identity before using a device observation.
Stable UUID visibility evidence is preferred. Numeric Slurm GRES IDs do not
establish vendor device order: a site can configure a different order.
Numeric-only attribution is therefore restricted to an entire visible
single-vendor device set whose size matches the allocation, without GPU
partitions or mixed-vendor ambiguity. An opaque numeric subset on a shared
multi-GPU node stays unavailable; this release does not infer its device mapping.
Common single-GPU desktop allocations and supported full-device-set allocations
remain eligible. Missing or ambiguous allocation evidence stays unavailable.
Unsupported or invalid counters remain
unknown; measured zero remains zero. Device counter definitions can differ
between vendors, so equal labels do not establish identical hardware behavior.
Intel tile-only utilization is not silently averaged into a device-wide
measurement. Tower does not fall back to unscoped node-wide SSH collection when
allocation ownership is unavailable.

The existing file-based GPU traces retain their own format and producer
cadence. Provider selection changes live collection; it does not convert a
trace file or generate missing historical samples.

## Check a batch script

Click **Shell checks** in Research Submit, including its inline Details view,
or use **Edit → Check prepared shell script** in the toolbar. The resource submission form
also has a **Shell checks** button and the `h` shortcut.
Validate the resource submission form first so that the check has a prepared
script snapshot to inspect.

Use `:shellcheck /absolute/path/to/run.sbatch` to inspect one local script.
Quote a path that contains spaces. With a prepared submission plan, use
`:shellcheck` to check that plan's script. Use `:shellcheck refresh` to rerun
the current check instead of reusing an eligible cached result.

In the result dialog, use arrows, `j`/`k`, Page Up, Page Down, Home, End, or
the wheel to scroll. Tab or Left/Right focuses **Rerun** or **Back**; Enter
activates the focused button. `r` reruns an idle check and `c` cancels a running
check. **Cancel** replaces **Rerun** while work is active. Esc or `q` cancels
pending work and returns.

The check reads a stable snapshot and reports its SHA-256 digest. When it is
associated with a prepared plan, the digest must match that plan. If the file
changed, prepare the plan again before checking or submitting it.
After preparing a changed script, use `:shellcheck` without an argument to bind
the new plan digest. Refresh retains the digest from the previous check.

| Check | Supported scope |
| --- | --- |
| Interpreter syntax | Explicit `bash`, `sh`, or `dash` shebang; matching local command must exist |
| ShellCheck | Optional `shellcheck` executable discovered on the local PATH |
| Findings | Tool, severity, line, column, rule where provided, and explanation |

Simple `/usr/bin/env bash` and `/usr/bin/env -S bash` shebangs are recognized.
Interpreter flags, environment assignments, unsupported interpreters, and a
missing shebang require manual review. They are not silently approximated.

Analysis does not execute or source the script. It does not submit a job,
follow sourced files, read user shell startup files, or change the script.
ShellCheck inline suppressions apply. ShellCheck configuration files and
external-source expansion do not apply to this isolated check.

Checks are explicitly local. SSH-backed and replay sessions reject this
command, including explicit paths, to prevent a local file being mistaken for
a remote job's script. Run Tower directly on the Slurm host to check scripts
there, or start a separate local session for a reviewed local copy.

A result with an unavailable analyzer is partial, not a complete pass.
Static checks cannot establish compute-node software availability, scientific
correctness, or successful execution. Existing Slurm submission preflight and
confirmation remain separate.

Inputs are limited to 8 MiB. Each analyzer has a five-second limit and combined
output is limited to 256 KiB. Results retain at most 512 findings. Eligible
results are cached by captured content, interpreter, tool identity, and analysis
policy. A cached result always describes those captured bytes.
The dialog renders at most 8,192 wrapped rows. Widen the terminal, or use
`tower --run 'shellcheck /absolute/path/to/run.sbatch'` for structured JSON
results when findings require more display space.

## Attach a scientific array manifest

A scientific array manifest maps actual Slurm task indices to project labels,
parameters, and declared paths. It does not derive identity from row order,
submission order, or completion order.

Use one document for one cluster and array parent. For example:

```json
{
  "schema": "tower.array-manifest/v1",
  "cluster": "desktop",
  "array_id": "12345",
  "entries": [
    {
      "index": 7,
      "id": "sample-liver-042-seed-7",
      "label": "liver-042 / seed 7",
      "parameters": {"sample": "liver-042", "seed": 7},
      "inputs": ["data/liver-042.csv"],
      "outputs": ["runs/liver-042-seed-7/result.json"]
    }
  ]
}
```

Replace the example cluster and array ID with the identities Tower displays.
An empty cluster matches only an empty cluster identity; it is not a wildcard.
`array_id` is the parent job ID as a string. `index` is a nonnegative integer.
Sparse indices are supported. Scientific `id` values and indices must each be
unique. The label defaults to the scientific ID when omitted.

Publish a complete UTF-8 JSON file, then attach its exact path:

```text
:arraymap /absolute/project/reports/arrays/desktop-12345.json
:arraymap inspect
:arraymap search liver-042
:arraymap reload
:arraymap clear
```

In Research Arrays, click **Load input map**, **Browse inputs**, **Reload map**,
or **Detach map**. The same controls appear in Jobs and History Details when
Research Arrays is selected. Click a mapped task row to inspect its scientific
entry. Use `:arraymap` to open the path prompt. **Edit → Array input mapping**
opens that prompt when no map is attached, or the browser when a map is attached.
`:arraymap load PATH` is an explicit alternative to `:arraymap PATH`.

Use the [JSON schema](../schemas/array-manifest.schema.json) and
[example manifest](../../examples/array-manifest.json) when adding this file to
another project.

In the browser, arrows, Page Up, Page Down, Home, and End move through entries.
Enter opens the selected entry; `/` opens search. Esc returns from entry details
or search to the list, and closes the list when already there. In the path or
search prompt, Enter applies the text and Ctrl-U clears it.

One manifest can be attached per session. The attachment is explicit. Tower
does not search arbitrary directories for a manifest or attach one by matching
a job name. Relative manifest paths use the
active file backend's path rules. Prefer an absolute path, especially over SSH.
Input and output paths inside an entry are declarations only: this feature
does not open, copy, validate, or create those files.

The attachment is scoped to the exact connection, cluster, and array parent.
When submission timestamps are available, Tower also binds the observed
submission attempt. Conflicting timestamps or a reused parent hide labels and
block mapped retries until explicit reattachment. When Slurm exposes no
submission timestamp, the browser states that limit: identity is scoped to the
connection and parent ID, without a verified submission-attempt distinction.

Tower validates and freezes a content revision. Background checks examine the
source stat no faster than every five seconds and report changes. Backend
limits and busy workers can delay the next check. They do not silently change
the scientific identity of existing tasks. Explicitly reload after reviewing a
replacement. A retry that uses the mapping must verify the
source revision; changed evidence requires review. A new Slurm array parent
needs its own explicit binding. This release does not automatically rewrite a
manifest for a new submission.
Resource edits retain the prepared plan's mapping metadata. The plan and
receipt preserve the selected scientific entries and revision; the passport
retains their source, revision, cluster, and original parent linkage. Existing
submission metadata limits still apply, even if the full manifest is valid.

The reader accepts at most 4 MiB and 10,000 explicit entries. Each entry allows
at most 64 scalar parameters and 32 input and 32 output declarations. Duplicate
JSON keys, nonfinite numbers, invalid identities, and a file changed during the
read are rejected. Missing entries stay unmapped rather than inheriting the
nearest label.

## Read results without changing the current task

All four features use the current interface's command and overlay conventions.
Esc closes an inspection without changing the selected job. Source collection,
static analysis, and manifest reads stay outside the rendering path. Moving the
mouse over a button is not an instruction to query Slurm or read a script.

Use the [Controls guide](../CONTROLS.md) for the complete command index, and the
[roadmap](../ROADMAP.md) for features that are still planned.
