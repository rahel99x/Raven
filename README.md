# Project Raven

**Monitor jobs, inspect evidence, and run admitted analysis in your terminal.**

Project Raven is the next release of Slurm Tower. It retains the terminal
workspaces, job groups, resource graphs, logs, research reports, and embedded
shell. It adds governed collection and explicit ownership of analysis compute.
Unicode and ASCII modes remain available. Python 3.10 or later is required.

- **`ravenl` starts Light.** It opens the interface without requesting compute.
- **`ravenh` requests Heavy.** It waits for a verified worker before loading the
  dashboard. On Slurm, the worker must run in the requested allocation.
- **`tower` remains a compatibility command.** It starts the governed Light
  interface. Existing project files and `tower.*` evidence schemas keep their IDs.

The Python distribution remains **`slurm-tower`**. Install or upgrade that one
package. The public product name is Project Raven; the Python import is `tower`.

[Raven runbook](docs/raven/README.md) · [Controls](docs/CONTROLS.md) ·
[Delivery and qualification](docs/raven/implementation.md) ·
[Project integration](docs/PROJECT_STANDARD.md) · [Release history](CHANGELOG.md)

## Install and try the interface

```bash
git clone https://github.com/rahel99x/Slurm-Tower.git
cd Slurm-Tower
python3 scripts/setup.py --mode demo
./scripts/ravenl --fake --no-state --no-plugins
```

The demonstration uses simulated Slurm data. It does not submit jobs. To install
console commands in a virtual environment, use `python -m pip install .`.
Add `[terminal]` to install the optional embedded-terminal dependencies:
`python -m pip install '.[terminal]'`.

## Select the execution environment

For a cluster or a workstation with Slurm installed:

```bash
./scripts/ravenl --config docs/examples/raven/slurm.json
./scripts/ravenl --config docs/examples/raven/slurm.json config explain
./scripts/ravenh --config docs/examples/raven/slurm.json --plan
```

Copy and edit the example first. Set the account, partition, time limit, cluster
identity, and worker Python path for your site. `--plan` prints the request and
makes no allocation. Remove `--plan` to request the reviewed configuration.
A profile is a starting configuration, not a site's approval or qualification.

For a standalone Linux desktop **without Slurm**, select it explicitly:

```bash
./scripts/ravenl --site desktop
./scripts/ravenh --site desktop --plan
./scripts/ravenh --site desktop
```

A missing `squeue` command never changes a cluster profile into desktop Heavy.
The portable desktop adapter reports its process limits. A policy that requires
hard aggregate cgroup limits is rejected if those limits cannot be enforced.

## Use compute and terminals

Open the **Light/Heavy** toolbar control or **View → Compute allocation** to
inspect, request, cancel, or release compute. Pending and draining workers do
not count as Heavy. **Single/Multi** remains a separate background-worker
concurrency control. More compute does not grant faster scheduler polling.

The **Details | Terminal** buttons select the secondary pane. Both use the same
focus indicator, including narrow layouts and panes with scroll controls.
Press **Ctrl-]**, then **t**, to return terminal input to the dashboard.
Allocation changes do not move an existing shell or redirect queued paste.
See the [terminal guide](docs/guides/terminal-fusion.md).

The starter Slurm policy uses a 60-second queue interval and a 300-second history
interval. The refresh control reports actual requested and admitted intervals.
Automatic GPU steps and weather probes are disabled. Existing telemetry and
project metric files remain available. Read [collection and limits](docs/raven/README.md#collection-and-retention).

## Choose a task

Open **File → Research and cluster operations**, or enter `:workbench`.
Task controls select **Run**, **Investigate**, **Compare**, **Results** or
**Connect**. `:workbench all` shows the complete operation catalog.
Use **Pin tool** or press `p` on a tool to save it. Open those tools with
`:workbench pinned`; each opening uses the current job and project context.

| Goal | Start here | Guide |
| --- | --- | --- |
| Check a running job | Select it in Jobs, then use its Details controls | [Workspaces and advice](docs/guides/adaptive-workspaces.md) |
| Diagnose a failure | Select the exact attempt in Recents or History; open Logs or Investigate | [Log controls](docs/guides/log-view.md) |
| Rerun one cancelled job | Right-click its row in Recents or History; inspect **Rerun cancelled job**, then review and confirm | [Exact retained rerun](docs/guides/operations-cluster.md#rerun-one-cancelled-job) |
| Follow an investigation | Open `:ops investigation-workflow`; inspect evidence, review a diagnostic, then inspect its results | [Investigation workflow](docs/guides/scientific-investigation.md) |
| Connect metrics and outputs | Enter `:projectguide /path/to/project`; validate an exact sample and copy its Bash hooks | [Project connection](docs/guides/project-connection.md) |
| Compare performance and scientific results | Use Compare, Analytics or a declared investigation manifest | [Scientific investigation](docs/guides/scientific-investigation.md) |
| Inspect recorded stacks | Select Trace in job Details; attach a prepared recording or inspect supported capture options | [Stack tracing](docs/guides/stack-tracing.md) |
| Work in a shell beside the data | Select Terminal, then use Tools for sessions, copy mode, diagnostics and evidence | [Embedded terminal](docs/guides/terminal-fusion.md) |
| Prepare a change | Inspect the operation, review its exact scope, then confirm Apply | [Operation catalog](docs/guides/operations.md) |

Mouse and keyboard controls use the same selected job context. Tables support
cascading sorts, column resizing, range selection, manual and inferred groups,
and scrollbars. Graphs support bounded zoom, independent display and polling
controls, and right-click reset. Read the [complete controls](docs/CONTROLS.md)
and [pointer guide](docs/guides/pointer-navigation.md).

## Publish project evidence

Keep your current source layout. Add one execution directory per run attempt,
with declared logs, metrics, results and artifacts. `tower report` can create and
update those records from a Bash launcher without requiring application imports.

```bash
tower report --help
tower report init --root /path/to/project
```

Use the [project standard](docs/PROJECT_STANDARD.md),
[copyable template](examples/project-template/README.md), and
[automation guide](docs/guides/research-automation.md). Exact job and attempt
identities prevent a requeue or reused job number from inheriting another run's
evidence. Tower reports missing data explicitly.

## Advanced research and tracing

The workbench includes evidence imports, capability and policy checks, reviewed
external adapters, diagnostic trials, scientific acceptance and lineage,
constrained simulation, application safe-point controls, and bounded field views.
These features consume explicit contracts and supported formats. They do not
infer missing scientific thresholds or replace arbitrary workflow engines.
Use the [platform starter guide](examples/platform/README.md) for telemetry,
portable evidence, adapters and private support bundles.
Several managed Apply actions and external drivers remain deferred even with
Heavy. Check the [execution contracts and supported paths](docs/raven/execution-contracts.md)
before choosing a workflow.

Prepared stack recording supports declared Python events and explicit native
instrumentation. Late attachment provides only the supported current snapshot
or recording history. There is no universal lossless debugger for every language,
MPI implementation, or GPU. The interface reports coverage, gaps and limits;
read the [implementation ledger](docs/FLAGSHIP_IMPLEMENTATION.md) before selecting
an advanced adapter.

## Validate and troubleshoot

```bash
tower --doctor
tower --gpu-check JOBID
tower --ui-trace /tmp/tower-ui-trace.json
python3 scripts/qualify_flagship.py --output /tmp/tower-qualification.json
```

The [Raven 7.0 validation record](docs/raven/validation.md) records current
regression, compatibility, packaging, and local qualification results.
The [6.1 consolidation guide](docs/guides/consolidation.md) describes the shared
components, compatibility rules, and measured validation. The
[6.1 validation record](docs/guides/consolidation-validation.md) records the
regression and performance checks. The
[6.0 validation record](docs/guides/flagship-validation.md) retains the preceding
release's regression, interaction, packaging and performance results. The
[qualification guide](docs/QUALIFICATION.md) separates passed offline checks
from untested site, device and user-evaluation requirements. See
[GPU diagnosis](docs/guides/gpu-detection.md),
[display performance](docs/guides/ui-performance.md),
[administrator deployment](docs/ADMINISTRATION.md), and
[compatibility policy](docs/SUPPORT_POLICY.md).

Terminal transport, scheduler/accounting cadence, site permissions and runtime
instrumentation affect the available data. Display interpolation and faster reads
do not create additional measurements. Optional tools require their documented
versions and permissions.

## Contribute

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and validation. Report
security concerns through [SECURITY.md](SECURITY.md). Tower is
[MIT licensed](LICENSE).

The [research strategy](docs/research/flagship-strategy.md) records prior art and
research hypotheses. Implemented mechanisms, external validation, scientific
outcomes and novelty claims remain separate in the
[implementation ledger](docs/FLAGSHIP_IMPLEMENTATION.md).
