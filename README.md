# Slurm Tower

**Inspect jobs, understand failures, and connect results to evidence—in your terminal.**

Slurm Tower brings queue and history views, logs, resource graphs, research
reports, and reviewed operations into one terminal workspace. Use it on a Slurm
workstation or through your cluster's normal SSH access. The core uses Python's
standard library. Unicode and ASCII modes are available.

[Install and operate](docs/runbook.md) · [Controls](docs/CONTROLS.md) ·
[Operation fields](docs/reference/operations.md) ·
[Project integration](docs/PROJECT_STANDARD.md) · [Implementation scope](docs/FLAGSHIP_IMPLEMENTATION.md) ·
[Capability ownership](docs/reference/capabilities.md) ·
[Release history](CHANGELOG.md)

The optional **Details | Terminal** pane keeps an interactive shell beside jobs,
graphs, logs and research. It includes named sessions, explicit SSH and allocation
connections, command evidence, and read-only recording playback. See the
[terminal guide](docs/guides/terminal-fusion.md) for installation, controls and
compatibility limits, and the [delivery matrix](docs/reference/terminal-fusion.md)
for the scope of the 100 design entries.

## Start with a safe demonstration

Use Python 3.10 or later with curses and venv support:

```bash
git clone https://github.com/rahel99x/Slurm-Tower.git
cd Slurm-Tower
python3 scripts/setup.py --mode demo
./scripts/tower --fake
```

Demo mode uses simulated Slurm data. The setup smoke checks disable plugins and
state. Use `./scripts/tower --fake --no-plugins --no-state` for the same isolated
application configuration.

## Connect to Slurm

On your cluster login node:

```bash
python3 scripts/setup.py --mode local --profile carc
./scripts/tower --config docs/config.example.json --profile carc
```

For a Fedora workstation with Slurm installed directly, select `--profile desktop`
in both commands. The CARC example uses more conservative optional collectors;
it is a starting profile, not institutional authorization. Follow your site's
polling and allocation policies.

To install the optional Bash alias, use the appropriate profile:

```bash
python3 scripts/install_shell.py --profile carc --apply
source ~/.bashrc
tower
```

The alias helper backs up `.bashrc` and preserves unrelated settings. See the
[runbook](docs/runbook.md) for SSH, offline installation, upgrades and recovery,
and the [Fedora guide](docs/DESKTOP.md) for desktop configuration.

To enable the embedded terminal after a pull:

```bash
python3 scripts/setup.py --mode demo --terminal
./scripts/tower terminal selftest
```

This installs the optional terminal dependencies into `.venv` and validates
simulated data. It does not change your Slurm profile or shell alias. Start Tower
normally, then select **Terminal** in a supported secondary pane. Press
**Ctrl-]**, then **t**, to return keyboard control to Tower.

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
