# Raven runbook

[README](../../README.md) · [Installation](../INSTALLATION.md) · [Implementation and qualification](implementation.md) ·
[Validation record](validation.md) · [Controls](../CONTROLS.md) ·
[Project reporting](../PROJECT_STANDARD.md)

Use this runbook to install Raven, configure an execution target, and operate
the Light interface and Heavy worker. Light owns the interface and governed
collection. Heavy adds explicitly admitted analysis tasks on a verified target.
See the [validation record](validation.md) for release-specific evidence.

## Install or update

Use Linux, Python 3.10 or later, and the Python curses and venv modules.
The core has no third-party runtime dependency. The optional terminal uses
`pyte` and `wcwidth`.

Install pipx through your operating system's package manager first. Its command
directory, normally `~/.local/bin`, must already be on `PATH`. The
[installation guide](../INSTALLATION.md) covers an existing writable directory
on `PATH` and an administrator installation. Pipx exposes installed executables
without aliases, environment activation, or shell-file edits.

Install the package and check it from outside the checkout:

```bash
git clone https://github.com/rahel99x/Raven.git
cd Raven
pipx install '.[terminal]'
cd "$HOME"
ravenl --version
ravenl doctor
ravenh --plan
```

Use `pipx install .` for the core without the embedded terminal.
The version commands identify the installed release. Doctor reports local
capabilities; the Heavy plan makes no allocation.

Update the original checkout and reinstall the installed package:

```bash
cd /path/to/Raven
git pull --ff-only
pipx reinstall raven
```

Use the `raven` distribution. Do not co-install another distribution that
owns the `raven` package. `ravenl`, `ravenh`, and `raven` come from the same wheel.
`raven` and `ravenl` both open Light. Install from this repository or its built
wheel; the bare package-index name does not identify this implementation.
The installed commands launch independently of the source checkout; updates
from the recorded local source need that checkout. For source development,
`python3 scripts/setup.py --mode demo` and
`./scripts/ravenl --fake --no-state --no-plugins` remain the offline workflow.
Source launchers use the checkout's `.venv` when it exists. Installing with
`python -m pip install .` into an existing virtual environment is also supported;
its `bin` directory must be on `PATH` to use bare command names.
For offline installation, build or download the wheel and optional dependencies on
an approved connected machine, then install with `--no-index --find-links`.
An installation under a previous distribution name needs the
[replacement procedure](../INSTALLATION.md#replace-an-installation-with-the-previous-package-name)
before normal reinstall updates. Existing configuration is retained, so repoint
`execution.worker_python` to the new environment's Python executable.

## Choose a site

`ravenl` means Light; it does not allocate compute. `ravenh` explicitly requests
Heavy. The default site is Slurm, even when Slurm commands are missing.
For Slurm installed directly on your workstation, keep this local Slurm mode.
Create a new persistent configuration with `ravenl --write-config`, then review
its scheduler settings. Both commands load `~/.config/raven/config.json`, or
`$XDG_CONFIG_HOME/raven/config.json`, automatically. Existing files are preserved.

```bash
ravenl config explain
ravenl doctor
ravenh --plan
```

Use the [Slurm example](../examples/raven/slurm.json) as a reference, replacing
its placeholder account, partition and cluster identity. Set:

- The cluster identity and local or SSH connection.
- The account, partition, QOS if required, CPUs, memory, and time limit.
- The Python executable available on the compute nodes, with this same release
  installed. A login-node virtual environment is usable only if the nodes can
  access it and its dependencies.
- The collection intervals and resource limits accepted by the site.

Use SSH configuration for authentication and jump hosts. Raven does not prompt
for passwords in its allocation protocol. Do not place credentials in profiles.
Worker startup rejects a mismatched build, target, owner, allocation, step,
resource grant, or policy. Missing compute-node software is an explicit failure.

A standalone desktop with no Slurm needs explicit selection:

```bash
ravenl --site desktop
ravenh --site desktop --plan
ravenh --site desktop
```

For Fedora **with** local Slurm, use a Slurm profile. Raven's `--site desktop`
selects standalone local execution. The supplied native configuration's
`--profile desktop` instead selects local Slurm.

## Inspect effective configuration

The [configuration schema](../schemas/raven-config.schema.json) is
`raven.config/v1`. JSON works on every supported Python version.
TOML input uses the standard-library parser on Python 3.11 and later.

```bash
raven_review_dir="$(mktemp -d)"
ravenl --site desktop --write-config "$raven_review_dir/desktop.json"
ravenl --config "$raven_review_dir/desktop.json" config explain
ravenl --config "$raven_review_dir/desktop.json" config explain --show-paths
```

`--write-config` creates a new file; it does not replace an existing file.
Its destination directory must be private and owned by the current user.
Explain shows the effective values, their origins, applied restrictions, warnings,
and policy digest. The active storage/executable paths and credential reference
are hidden unless requested. Policy origin labels still identify their files.
Mandatory site restrictions are applied after preferences. Users can choose
slower polling and lower budgets; they cannot relax mandatory limits.
Administrators can install a root-owned `/etc/raven/policy.json` that is not
writable by a group or other users. The [site policy schema](../schemas/raven-site-policy.schema.json)
and [example policy](../examples/raven/site-policy.json) specify supported rules.
`--site-policy PATH` adds restrictions; it does not replace this mandatory file.
The explicit `--account NAME` option is resolved before policy checks and sets
the account used for monitoring and a Heavy request.

`RAVEN_CONFIG`, `RAVEN_PROFILE`, `RAVEN_STATE_DIR`, and `RAVEN_SITE_POLICY` select
the corresponding configuration, profile, storage, and additional policy paths.
`RAVEN_WORKER_PYTHON` selects the installed worker executable.
Restart to load a changed connection/profile. An existing worker or terminal is
never silently transferred to a new connection.

## Request and release Heavy

The dashboard stays on its original host. A fresh worker runs on the explicit
execution target. It does not start a second dashboard or sampler.

1. Open the Light/Heavy toolbar control, View's compute entry, or `:raven inspect`.
2. Select Request. Review the target, account, requested resources, and limits.
3. Confirm the current request. Continue using the dashboard while it waits.
4. Select Cancel during preparation or waiting, or Release after admission.
5. Inspect the resulting state before making another request.

The states are Light, Preparing, Pending, Starting, Heavy, Draining, and
Reconciling. Only a verified ready worker is Heavy. Cancel and Release act on
Raven's owned lifetime. They do not cancel a job based only on a parsed job ID
or job name. Unknown cleanup remains Reconciling and blocks automatic retry.
The worker stops at its time limit or idle deadline; Raven does not auto-renew.

Run `ravenh` from a normal terminal with access to the configured Slurm commands.
It requests a new allocation with `salloc`, starts the worker through `srun`, and
verifies placement before opening the dashboard. An existing allocation is not
required. `--plan` makes no submission. An interrupted wait drains the owned helper.
`--help` and `--version` never allocate. Use `ravenh --allocation JOBID` for an
explicit reuse review. This checks current ownership, attempt, resources, and
placement before starting an owned step. A reuse `--plan` reports an unverified
identity; it does not query Slurm or create a reusable authorization receipt.
The generic Slurm adapter still needs a site-authorized real allocation test.
A full-interface transfer to a compute node is not supplied by this adapter;
use the persistent Light interface and its admitted worker.

## Use analysis and the embedded terminal

Choose a research or analysis operation in its existing page or Workbench.
Small retained summaries and bounded local views remain available in Light.
Advanced operations prepare a request or report why an execution target is
needed. Supported admitted operations execute as named worker handlers. They do
not deserialize Python objects or fall back to running on the interface host.

An operation may still require an external instrumented application, adapter,
site service, or exact job-local target. A generic analysis worker does not grant
permission to attach to arbitrary jobs or modify scheduler state. Inspect the
capability ledger in the [offline qualification report](#validate-locally).
The [execution contracts](execution-contracts.md) list supported handlers and
deferred Apply actions, external drivers, and transports. Heavy admission does
not make an undeclared adapter available.

Select Terminal beside Details to open the optional shell. A shell remains an
explicit user-controlled session on its displayed host. Switching Light/Heavy
or Single/Multi does not move it. Generated execution helpers pass the same
admission checks as other managed actions; ordinary typed shell commands remain
under the user's control. See [terminal controls](../guides/terminal-fusion.md).

## Collection and retention

Compute limits and collection permission are separate. The starter Slurm profile
uses these Light intervals and rolling ceilings. Active Heavy applies the selected
[telemetry preset](../guides/telemetry-updates.md); its Default requests queue
reads every five seconds and live CPU/memory reads every two seconds. Presets
and Custom values change live through **Setup → Heavy telemetry update settings**.

| Source or resource | Default |
| --- | ---: |
| Queue | 60 s |
| Predicted starts | 120 s |
| Accounting/history | 300 s |
| Nodes and partitions | 300 s |
| Selected details/live statistics | 60 s, on demand |
| Account/share | 900 s |
| Log follow | 5 s |
| Research files | 5 s |
| Retained telemetry | 1 s local; at least 1.5 s remote |
| Controller attempts | 12/minute, burst 2 |
| Accounting attempts | 2/minute |
| Managed Slurm step attempts | At least 10 s apart; 6 per rolling minute |
| Statistics attempts | 4/minute |
| Fresh action validation | 6/minute |
| Concurrent generated commands | 2, with a validation reservation |
| Automatic GPU steps and weather probes | Disabled |
| Selected log buffer | 2 MiB; at most 4 buffers |
| Retained queue/history rows | 5,000 each |
| Retained native series | 128 jobs; 16 MiB accounted data |
| Participating prepared/raster caches | 32 MiB aggregate |
| Persistence queue, including writes in flight | 8 MiB |
| Owned event/series storage | 256 MiB; 512 files |

The prepared-cache allowance covers participating preparation and raster caches.
Immutable Store publications have a separate 128-entry, 24 MiB guard. Archive
limits apply to each instance's owned storage; they are not a cross-process disk
quota. Light's two-worker ceiling applies across its background lanes combined.

The queue slider adjusts the active queue base through a tenfold faster request;
the starter Light range is 60–6 seconds. Heavy presets control the other readers
and budgets. Mandatory site floors remain effective, and the controls show their
admitted intervals. Sampling, display windows, and plot animation are distinct.
Manual refresh, reconnect, and Heavy mode do not reset used allowances. Failures count
as attempts. The first visible snapshot is requested immediately, including when
a previously unopened view needs its first sample. Shared admission still applies.
Periodic collections use jitter, idle intervals, demand gating, and endpoint
circuits to bound subsequent traffic.

Step admission uses `collection.step_launch_interval_seconds` and
`collection.step_launches_per_minute`. The default allows a retry after 10 seconds,
subject to the rolling ceiling and other admission limits. Same-user sessions
sharing an endpoint use the same allowance, and failed launches count as attempts.
Site policy can require a longer interval or a lower ceiling. Updating Raven does
not replace these settings in an existing configuration.
Explicit submission-test probes use the same configured per-minute ceiling in a
separate budget. Automatic weather probes remain disabled.

The private SQLite coordination directory must be host-local and owned by the
user. It must not be placed on NFS, Lustre, or another shared filesystem.
Same-user sessions can share observations. Different users and hosts still add
to cluster traffic. The optional site-operated service is not shipped in this
release; a service profile fails explicitly instead of falling back to queries.

Coverage notices identify truncation, eviction, stale sources, and quota refusal.
A partial queue does not imply omitted jobs have finished. Storage limits stop
new owned archive writes; Raven does not delete project evidence to make room.
These byte budgets cover declared retained structures, not a hard cap on total
Python RSS. Process-tree measurements and their limits are in the qualification
record. User-created terminal child processes are separately visible resources.

## Migrate without changing evidence

Current projects use `.raven` directories and `raven.*` schema identifiers.
Configuration formats supported by the native adapter can be read through a
governed view without rewriting their originals. Previous Python imports,
environment variables, and evidence schema identifiers are not aliases for the
Raven namespace. Preserve external artifacts and convert them explicitly before
using them as current Raven projects.

From the checkout, select the previous configuration root explicitly for migration.
The Bash wrapper inventories configuration only unless state is requested:

```bash
raven_migration_dir="$(mktemp -d)"
bash scripts/migrate-config.sh --source-config /path/to/previous-config \
  --save-plan "$raven_migration_dir/plan.json"
cat -- "$raven_migration_dir/plan.json"
```

Inspect the plan first. It lists source and destination paths and hashes. Apply
that saved plan after reviewing its conflicts and omissions:

```bash
bash scripts/migrate-config.sh --apply --plan-file "$raven_migration_dir/plan.json"
```

Large history is opt-in with `--include-history`. Migration preserves original
files. To inventory state, add `--include-state --source-state /path/to/previous-state`
when saving the plan; history also requires `--include-state`. Migration writes
journaled receipts. Interrupted application can resume against that
journal. Live sockets, active execution receipts, terminal transcripts, and
project evidence are not rewritten. Keep the returned journal path.

The initial inventory is limited to 256 files and 64 MiB across the selected
configuration and state roots. Each file is limited to 16 MiB; directories are
searched to a fixed depth. The plan lists omissions. Use smaller explicit roots
for a separate import when these limits are reached.

Copies are kept in `legacy-imports/<migration-id>` under the new state directory.
An existing Raven config is retained; it is not silently merged with old settings.
A completed migration can activate archived UI preferences when no new scoped UI
state exists. This reader accepts at most 256 KiB from one of 32 inventoried UI
files. It restores appearance and column preferences. Manual group identities
require the same configuration file hash and exact saved connection, profile,
and owner scope. Otherwise groups remain readable in the archive. Terminals,
active tasks, log bookmarks, and forecast observations are not adopted.

```bash
ravenl migrate --rollback /path/to/migration-journal.json
```

Rollback removes only unchanged migration-owned outputs. If an output has changed,
it reports a conflict instead of deleting the user's work. The old distribution
can be restored in a separate environment against its preserved configuration.
Do not run old and new packages from the same environment during replacement.

## Validate locally

```bash
ravenl doctor
ravenl qualify --offline > /tmp/raven-offline.json
ravenl terminal selftest
```

For contributor regression checks, use the checkout's development environment
as described in [CONTRIBUTING](../../CONTRIBUTING.md).

The offline cohort report exercises actual admission with distinct synthetic
user scopes at 1, 10, 50, 200, and 256 users. It sends no scheduler commands.
It does not certify controller RPC cost, cross-user authorization, GPU sampling,
real Slurm placement, SSH behavior, or a multi-user production rollout.

Open `:raven inspect` for cached self-observation: local process-tree RSS/PSS,
threads/descriptors, collector ages and cadence, admission rates, worker queues,
and retention coverage. Sampling occurs in the background at a low frequency.
It does not record keys, paste, full environments, or private command payloads.

Before a cluster rollout, ask the administrators to select numerical limits for
controller traffic, query/submission latency, login-node resource use, and storage.
Start with a disposable allocation and small approved cohort. Use the
[qualification matrix](implementation.md) and existing [site guide](../QUALIFICATION.md).
