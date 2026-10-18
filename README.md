# Raven

Raven is a terminal workbench for Slurm-based research. It combines job
monitoring, resource graphs, logs, project evidence, and an embedded shell with
governed data collection and explicit ownership of analysis compute.

## Install

On Linux or WSL2, with Python 3.10+, `venv`, Git, and curl available:

```bash
curl -fsSL https://raw.githubusercontent.com/rahel99x/Raven/main/install.sh | bash
```

The installer creates a private environment for your user and opens guided setup
when a terminal is available. No pipx or administrator access is needed. Follow
the printed PATH instruction if required, then open Raven with `ravenl`.

## Update

Close Raven dashboards and their owned Heavy workers, then run:

```bash
raven update
```

Updates follow your source checkout's tracking branch and preserve configuration,
state, and installation choices. Local code edits stop the update for review;
changes to recognized user configuration are preserved.
Raven updates only when you request it; it does not update in the background.
See [installation and updates](docs/INSTALLATION.md) for existing installations,
pipx, custom paths, and explicit replacement of local code edits.

[Getting started](docs/guides/getting-started.md) · [Configuration migration](docs/MIGRATION.md) ·
[Runbook](docs/raven/README.md) · [Controls](docs/CONTROLS.md) ·
[Implementation status](docs/raven/implementation.md) · [Validation](docs/raven/validation.md)

## Commands

| Command | Behavior |
| --- | --- |
| `ravenl` | Opens Light without requesting compute. |
| `ravenh` | Opens the dashboard while Heavy starts in the background; analysis becomes available after verification. A first bare interactive launch opens setup in Light. |
| `raven` | Opens the governed Light interface; equivalent to `ravenl`. |
| `raven setup` | Guides workstation or cluster setup and reviews configuration before saving. |
| `raven update` | Updates through the owning installer from the source checkout's tracking branch. |
| `raven revert BACKUP_DIRECTORY` | Restores an updater backup while preserving current configuration and state. |
| `raven launch SCRIPT` | Submits an existing shell script with retained run evidence; use `--local` for explicit workstation execution. |
| `raven export-data` | Exports selected retained measurements without opening the dashboard. |

The Python distribution and import namespace are **`raven`**. Current evidence
schemas use the `raven.*` namespace. Install from this repository or a wheel
built from it; an identically named package on an index is not this implementation.

The core uses Python's standard library. Unicode and ASCII interfaces are
available; the embedded terminal uses optional `pyte` and `wcwidth` dependencies.
Linux and WSL2 support persistent local state. See the installation guide for
platform limits and stateless macOS use.

## Install from a checkout or try the demo

For an existing checkout, run its installer directly:

```bash
cd /path/to/Raven
python3 scripts/install.py
```

Run `raven setup` to reopen guided configuration later. The installer preserves
commands owned by other installations; existing pipx users should follow the
[pipx update instructions](docs/INSTALLATION.md#update-a-pipx-installation).
The [first-job guide](docs/guides/getting-started.md) covers local Slurm, a cluster
lab account, and explicit standalone execution.

For an offline demonstration from the source checkout:

```bash
python3 scripts/setup.py --mode demo
./scripts/ravenl --fake --no-state --no-plugins
```

The demonstration uses simulated jobs and submits no work. Source setup
prepares the checkout's launchers; it does not expose commands on `PATH`.
User configuration and state remain separate from the installed package.

## Configuration

A fresh installation defaults to local Slurm. Run `raven setup`, or open
**Setup → Installation and diagnostics → Getting started**, to choose the environment and review persistent
settings. **Sites and lab profiles** in the same category saves reusable connection and
Heavy-account choices. Workload billing remains a separate launch setting.
After saving, **Use this profile** opens Light on that profile; then choose
**File → Launch job**. Saving alone requests no compute and leaves the current
connection unchanged.

For direct configuration inspection:

```bash
ravenl config explain
ravenh --plan
```

Both launchers read `~/.config/raven/config.json`, or
`$XDG_CONFIG_HOME/raven/config.json`. The lower-level `ravenl --write-config`
command creates defaults without replacing an existing file. Review the cluster
identity, account, partition, QOS, resource request,
and worker Python path for your site before starting Heavy. The worker must
have the same Raven release available on the selected compute nodes.

For a standalone machine **without Slurm**, select the desktop adapter
explicitly:

```bash
ravenl --site desktop
ravenh --site desktop --plan
```

After reviewing a plan, `ravenh` requests the selected compute. A missing Slurm
command does not select standalone execution. The desktop adapter reports its
available process limits and rejects policies that require unavailable hard
aggregate enforcement. See the [runbook](docs/raven/README.md#choose-a-site)
for local, SSH, and site-policy configuration.

## Configuration migration

The Bash helpers preview a copy-only migration from a previous installation.
Select its configuration directory explicitly, then save and review a plan:

```bash
raven_migration_dir="$(mktemp -d)"
bash scripts/migrate-config.sh --source-config /path/to/previous-config \
  --save-plan "$raven_migration_dir/plan.json"
cat -- "$raven_migration_dir/plan.json"
```

Review the source paths, destinations, and omissions before applying that plan:

```bash
bash scripts/migrate-config.sh --apply --plan-file "$raven_migration_dir/plan.json"
```

The migration preserves original files and existing Raven configuration.
State and history imports are optional. The [migration guide](docs/MIGRATION.md)
covers custom paths, retained local Slurm profiles, restart recovery, and
rollback with `scripts/rollback-config.sh`. Previous package imports and evidence
schema identifiers require explicit conversion before use by this implementation.

## Execution model

| Component | Responsibility |
| --- | --- |
| Light interface | Displays observations, evidence, controls, and terminal sessions without requesting compute. |
| Collection owner | Admits source requests under interval, rate, concurrency, and retention limits. |
| Heavy worker | Executes supported admitted tasks after target and resource ownership are verified. |
| Project evidence | Associates logs, metrics, results, and artifacts with an exact job attempt. |

Compute admission and collection permission are independent. Light starts with
a 60-second queue interval and a 300-second history interval. Active Heavy uses
its selected telemetry preset; Default requests queue reads every five seconds
and live CPU/memory reads every two seconds. **Setup → Refresh and performance → Refresh settings**
opens queue, recording-read, and Heavy preset settings together. Changes apply
without a restart; recording frequency and observed data freshness remain distinct.
Site policy and request budgets still apply;
automatic GPU steps and weather probes remain disabled. See
[telemetry updates](docs/guides/telemetry-updates.md) for effective intervals,
retained recordings, and saved preferences.

The Light/Heavy toolbar control and **View → Compute allocation** expose
allocation review, request, cancellation, and release. Pending and draining
workers do not count as Heavy. Background-worker concurrency lives in advanced
preferences. Supported execution paths and deferred actions are listed
in the [execution contracts](docs/raven/execution-contracts.md).

Use **Setup** for connections, launch defaults, recordings, refresh, and diagnostics.
Each Research and Analytics view provides its own setup action. The
[Setup guide](docs/guides/setup.md) covers account discovery, personal and lab
billing, and local Slurm defaults. Saving preferences is separate from requesting
compute.

Job lists provide three controls: **View**, **Order**, and **Groups**, including
History and the browsers beside Research and Analytics. Switching to Sequential
preserves group assignments and the saved batch order. See
[batch views](docs/guides/batch-launches.md) for ordering, readable group names,
and selection behavior.

## Workflows

Open **File → Launch job**, or enter `:launchjob`, to select an existing script,
review its effective account and resources, choose recordings, and submit from
the final review. The launcher preserves the script and displays an equivalent
command. Previewing and saving preferences are separate from submission.

After submission, **Run home** keeps the exact attempt's status, logs,
recordings, outputs, and export actions together. **Run again** opens a new
review with retained settings. **Connect data** validates an actual sample and
provides application snippets with independent recording-readiness indicators.
See the [everyday workflow](docs/guides/everyday-workflow.md).

Run the launcher on the workstation or cluster submission host that owns the
project. An SSH monitoring profile does not upload and submit a local project
to a remote cluster.

Open **File → Research and cluster operations**, or enter `:workbench`.
Select Run, Investigate, Compare, Results, or Connect. `:workbench all` opens
the operation catalog; `:workbench pinned` opens your saved tools in the
current project and job context.

| Task | Guide |
| --- | --- |
| Install Raven, choose a site/account, and launch a first job | [Getting started](docs/guides/getting-started.md) |
| Inspect running jobs, history, and resource views | [Adaptive workspaces](docs/guides/adaptive-workspaces.md) |
| Read full errors, dismiss notices, and review suggested fixes | [Notifications](docs/guides/notifications.md) |
| Diagnose an exact failed or cancelled attempt | [Logs](docs/guides/log-view.md) · [Retained rerun](docs/guides/operations-cluster.md#rerun-one-cancelled-job) |
| Review investigations and compare results | [Scientific investigation](docs/guides/scientific-investigation.md) |
| Add application reporting on a workstation or HPC cluster | [Reporting V2](docs/PROJECT_REPORTING_V2.md) |
| Launch an existing script and export plotting-ready measurements | [Automated reporting and exports](docs/guides/automated-reporting-exports.md) |
| Attach project metrics, logs, and artifacts | [Project connection](docs/guides/project-connection.md) |
| Work in an embedded shell | [Terminal guide](docs/guides/terminal-fusion.md) |
| Inspect prepared Python or native recordings | [Stack tracing](docs/guides/stack-tracing.md) |
| Review and apply supported operations | [Operation catalog](docs/guides/operations.md) |
| Explore retained evidence, research tools and qualification limits | [Reliability and research guides](docs/guides/reliability-and-research.md) |

The **Details | Terminal** controls select the secondary pane. Press **Ctrl-]**,
then **t**, to return terminal input to Raven. Allocation changes do not move an
existing shell. Tables and graphs support mouse and keyboard navigation;
[complete controls](docs/CONTROLS.md) describe selection, sorting, resizing,
scrolling, and bounded graph zoom.

## Project evidence

For an existing Bash or batch script, start with
[automated reporting and exports](docs/guides/automated-reporting-exports.md):

```bash
raven launch test.sh --research experiment --check
raven launch test.sh --research experiment --export-on-finish
```

The first command previews without submitting; the second submits once and
retains an exact attempt independently of the dashboard. GPU recording and
supported application tracing are explicit choices. Scientific measurements
still need a producer; missing evidence remains visible. The
[runnable example](examples/automated-reporting/README.md) needs no handwritten
manifests.

Use the [reporting V2 guide](docs/PROJECT_REPORTING_V2.md) to connect an
existing application using a portable Bash launcher. It covers Linux, CARC,
UCI HPC3, student and lab allocations, exact file formats, and every Research
and Analytics data source. The application needs no Raven installation.

Keep your source layout and declare one execution directory per run attempt,
including its logs, metrics, results, and artifacts. For Slurm environments with
Raven installed, the reporting CLI can publish the same records:

```bash
raven report --help
raven report init --root /path/to/project
```

The [project standard](docs/PROJECT_STANDARD.md) defines the unchanged V1 file
contracts; the [automation guide](docs/guides/research-automation.md) covers the
installed Slurm CLI. Exact job and attempt
identities keep requeues and reused job numbers from inheriting another run's
evidence. Missing data is reported explicitly.

Advanced analysis, external adapters, runtime controls, and field views require
their declared contracts and supported formats. They do not infer scientific
acceptance thresholds. Prepared tracing has explicit coverage and gaps; it is
not a universal lossless debugger. Consult the
[implementation ledger](docs/FLAGSHIP_IMPLEMENTATION.md) and
[platform examples](examples/platform/README.md) before selecting an adapter.

## Validation

```bash
ravenl doctor
ravenh --plan
ravenl --fake --no-state --no-plugins --once --ascii
```

For development validation, follow [CONTRIBUTING.md](CONTRIBUTING.md): run the
regression suite, check generated references and schemas, build distributions,
and qualify the actual wheel outside the checkout.

The [validation record](docs/raven/validation.md) reports regression,
compatibility, packaging, terminal, and resource measurements with their test
conditions. Local qualification does not establish production Slurm, device,
or multi-node coverage. The [qualification guide](docs/QUALIFICATION.md)
defines the additional target-system checks. See
[GPU diagnosis](docs/guides/gpu-detection.md),
[performance methods](docs/guides/ui-performance.md), and
[administrator deployment](docs/ADMINISTRATION.md) for specific integrations.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) for development and review requirements.
Report security concerns through [SECURITY.md](SECURITY.md). Raven is
[MIT licensed](LICENSE).

The [research strategy](docs/research/flagship-strategy.md) records prior art and
research hypotheses. Implementation, measured outcomes, external validation,
and novelty claims are distinguished in the implementation and validation
records. Historical releases remain documented in [CHANGELOG.md](CHANGELOG.md).

## Citation

Use [CITATION.cff](CITATION.cff) to cite the software. Record the release version
and commit used in an experiment so the implementation can be identified.
