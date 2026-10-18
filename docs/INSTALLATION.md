# Install the Raven commands

Install the `raven` distribution to get real `ravenl`, `ravenh`, and `raven`
executables. The installed commands run from any working directory without
aliases, virtual-environment activation, or changes to `.bashrc`. They use the
installed package, so launching them does not require the source checkout.

Use Linux, macOS, or WSL with Python 3.10 or later and curses support. Install
Python's `venv` support if it is packaged separately by your operating system.
The repository installer needs no pipx or administrator access. Package
installation needs access to the
configured Python package index. The core has no third-party runtime
dependencies; the optional embedded terminal uses `pyte` and `wcwidth`.

## Install from a checkout

```bash
git clone https://github.com/rahel99x/Raven.git
cd Raven
python3 scripts/install.py
```

The installer creates an isolated application environment under
`~/.local/share/raven/installations` (or `$XDG_DATA_HOME/raven/installations`)
and exposes `raven`, `ravenl`, and `ravenh` in `~/.local/bin`. It checks for existing
command ownership before installing; it does not replace a pipx installation or
an unrelated command. Read the printed executable paths and PATH guidance.
The installer does not edit shell-startup files or install Slurm services. It
includes the optional embedded-terminal dependencies and starts guided setup
when run in an interactive terminal.

To reopen setup or launch the dashboard later:

```bash
raven setup
ravenl
```

Setup checks local command availability, offers environment and account choices,
and reviews configuration before saving. **Setup → Installation and diagnostics → Getting started** opens the
dashboard workflow. Follow [Getting started](guides/getting-started.md) for a
local Slurm workstation, a cluster submission host, or explicit standalone
execution.

| Installer option | Purpose |
| --- | --- |
| `--plan` | Check destination ownership and print paths without writing or downloading. |
| `--prefix PATH` | Choose a dedicated user-owned directory for installed releases. |
| `--bin-dir PATH` | Choose a user-owned command directory, preferably already on PATH. |
| `--without-terminal` | Install the core without `pyte` and `wcwidth`. |
| `--no-setup` | Install or update without entering guided setup afterward. |

Use real directories rather than directory symlinks for installation paths.
For example, preview a custom command directory before installing:

```bash
python3 scripts/install.py --bin-dir /absolute/path/to/existing/bin --plan
python3 scripts/install.py --bin-dir /absolute/path/to/existing/bin
```

If the command directory is not on PATH, the installer prints an absolute setup
command and a one-session `export PATH=...` command. Use either; rerunning an
installer cannot change an already running parent shell's PATH.

### Existing pipx users and alternative installation

Keep using pipx if it already owns your Raven installation. For a new pipx
installation, install pipx through your operating system's package manager
first; for example, on Fedora use `sudo dnf install pipx`.

Check that pipx is available and that its executable directory is already on
your shell's `PATH`. Its normal user installation directory is `~/.local/bin`.
If that directory is absent from `PATH`, use the existing-directory or
administrator installation below. This procedure does not edit shell files.

```bash
command -v pipx
git clone https://github.com/rahel99x/Raven.git
cd Raven
pipx install '.[terminal]'
```

Use `pipx install .` instead if you do not need the embedded terminal. Pipx
creates an isolated application environment and exposes the package's console
commands. Install only the `raven` distribution; do not co-install another
distribution that owns the Python `raven` package in that environment.
Use this checkout or its built wheel as the installation source. Do not install
by the bare package-index name: that name can identify unrelated software.

Check the installed commands from outside the checkout:

```bash
cd "$HOME"
command -v ravenl
command -v ravenh
ravenl --version
ravenh --version
ravenl doctor
ravenh --plan
```

Doctor reports local capabilities. `--plan` prints the Heavy request without
submitting it. For an offline demonstration, run
`raven setup --demo`, or `ravenl --fake --no-state --no-plugins`. Press `q` to exit.

### Use an existing directory on PATH

When `~/.local/bin` is not on `PATH`, choose an existing directory that is on
`PATH` and writable by your user. Replace the example path with that directory:

```bash
PIPX_BIN_DIR=/absolute/path/to/existing/bin pipx install '.[terminal]'
```

Use the same `PIPX_BIN_DIR` value for later pipx reinstall or uninstall commands.
An installer cannot make a command discoverable in a shell whose `PATH` excludes
the executable's directory.

### Administrator installation for all users

With a pipx version that supports `--global`, an administrator can install once
for the machine:

```bash
sudo pipx install --global '.[terminal]'
```

Global pipx normally exposes commands in `/usr/local/bin`. That directory must
be on each user's `PATH`. Run Raven as your normal user after installation;
configuration and application state remain per user.

## Configure the existing scheduler

The default Raven site uses local Slurm commands, with no configured SSH host,
account, or partition. A workstation with Slurm already installed uses this
mode. The installation does not install or start Slurm services.

For guided configuration, run `raven setup`. Save and select named sites through
**Setup → Connections and accounts → Sites and lab profiles**. A profile can use local Slurm on either a
workstation or a cluster submission host, an SSH monitoring connection, or
explicit standalone execution. Saving a profile affects future launches and
does not switch the active connection or request compute.
Select **Use this profile** after saving in the dashboard to reopen Light on
that profile. Command-line setup prints the equivalent launch command. A first
bare interactive `raven`, `ravenl`, or `ravenh` without configuration opens setup;
this first-run `ravenh` path requests no Heavy allocation.

For a new default configuration without the guided workflow, run:

```bash
ravenl --write-config
ravenl config explain
ravenh --plan
```

`--write-config` creates `~/.config/raven/config.json`, or
`$XDG_CONFIG_HOME/raven/config.json` when `XDG_CONFIG_HOME` is set. It refuses to
replace an existing file. If you already have configuration, inspect and edit
that file instead. Both launchers load it automatically. Explicit `--config`
and an existing `RAVEN_CONFIG` binding take precedence.

For configuration from a previous installation, use the Bash preview, apply,
and rollback helpers in the [migration guide](MIGRATION.md). Select the source
directories explicitly and review the saved plan before applying it. Migration
retains originals and preserves an existing Raven configuration.

Review the cluster identity, account, partition, QOS, resource request, and
worker Python path for your scheduler. Empty account and partition values use
the scheduler's defaults; they do not create those defaults. The same release
and worker interpreter must be available on the selected compute nodes. See
the [Raven runbook](raven/README.md#choose-a-site) for site requirements.

`ravenl` opens Light without allocating compute. After reviewing the plan,
`ravenh` requests a Slurm allocation and starts Heavy only after the worker is
verified. On a machine **without Slurm**, select `--site desktop` explicitly;
that mode means standalone local execution. The supplied native configuration's
`--profile desktop` instead selects local Slurm.

### Import a native local-Slurm profile

The installed commands use Raven configuration discovery. To export the
supported effective settings from the supplied native local-Slurm profile into
a **new** default Raven configuration, use the actual absolute checkout path:

```bash
ravenl --config /absolute/path/to/Raven/docs/config.example.json --profile desktop --write-config
```

For a customized native file, use that file's path instead. This command refuses
to replace existing Raven configuration. It exports the settings supported by
Raven and applies Raven's collection limits; it does not copy every UI
or state setting. The original configuration remains available. See
[migration](raven/README.md#migrate-without-changing-evidence) before importing
legacy state.

## Update the installed package

The repository is now `rahel99x/Raven`. Existing checkouts can retain their
directory name. Update an old remote from that checkout:

```bash
git remote set-url origin https://github.com/rahel99x/Raven.git
```

Close Raven before replacing its installed package. Check `command -v ravenl`
and update through the same method that owns that executable. Configuration,
project reporting files, and application state remain outside the installed
package.

For the repository installer, update its checkout and rerun it:

```bash
cd /path/to/Raven
git pull --ff-only
python3 scripts/install.py --no-setup
hash -r
```

If Git reports local source edits, preserve or deliberately discard those edits
before pulling. The installer does not reset the checkout. If you selected
custom installation paths or `--without-terminal`, retain those choices for the
update. Each successful installation builds and checks a fresh environment
before switching the active release. The previous release is retained; a failed
build leaves it selected. The stable worker interpreter is
`PREFIX/current/bin/python`, using the release prefix shown by the installer.

### Update a pipx installation

Pipx records the source directory used during installation. Keep using that
directory for updates; the repository rename does not require moving it.

Pull changes in the checkout used for the original pipx installation, then
reinstall the application:

```bash
cd /path/to/Raven
git pull --ff-only
pipx reinstall raven
```

Pipx retains the original installation source and extras. Use the same
`PIPX_BIN_DIR` override if you selected one. For an administrator installation,
use `sudo pipx reinstall --global raven`.

Pulling source alone does not update a normally installed package. Reinstallation
preserves user configuration and state; it needs the recorded source checkout.
Repeat the version, doctor, and plan checks from outside the checkout afterward.

### Reliability revision compatibility

Use the ordinary Raven package and the installer that already owns your commands.
The review implementation is not a published release merely because its source
or evidence exists. Before publication, install only a reviewed local checkout
or its exact built wheel; do not assume a remote branch or `main` contains it.

Close old dashboards and their owned workers before changing the package.
Retain your config and state directories. Review the selected billing account,
partition and worker interpreter with `ravenl config explain` and `ravenh --plan`;
the latter does not request compute. A workstation with Slurm continues to use
local Slurm. Selecting `--site desktop` instead selects standalone execution.

The stricter [configuration trust checks](guides/configuration-trust.md) can
reject symlinked or group-writable configuration and plugin paths that previously
loaded. Inspect the reported path and use a reviewed regular file with trusted
ownership. Raven does not silently repair permissions or execute a rejected plugin.
Separate Raven writers need separate state directories; unsupported shared
filesystem ownership remains read-only rather than risking conflicting writes.

On a cluster whose home directory uses NFS or Lustre, ask the site administrator
for an approved local state location and select it with `RAVEN_STATE_DIR`.
Check the effective paths with `ravenl --show-paths config explain`. Project
scientific files can remain on shared storage. A temporary node-local directory
does not provide retention after logout, reboot or site cleanup; choose it only
when that loss of local cache is acceptable. Do not delete `.writer.lock` to
bypass a live owner; close that session or choose a separate state scope.

Native [recordings](guides/recording-storage.md) now distinguish exact execution
attempts. Older unattributed files remain preserved and visibly unverified; a
matching job number alone cannot attach them to a new run. JSONL remains the
default. Compact blocks, tiered working archives, historical recording, optional
services and raster rendering require explicit selection. Preserve originals
when trying them, and use the documented raw export before a downgrade that
cannot read a new format. Never run old and new writers against one state directory.

The [core/lab split](guides/distribution-experiment.md) is a separate unpublished
experiment. Its generated wheels belong only in its private qualification
environments. [MCP](guides/mcp-evidence.md) and [WASI](guides/wasi-analysis.md)
dependencies are optional; ordinary monitoring does not require them. New
analysis tools retain their individual experimental and site-qualification limits.

### Move from pipx to the repository installer

Changing installation methods is optional. The repository installer preserves
pipx-owned command links and reports conflicts; it does not silently take them
over. Keep the working pipx installation until you are ready to replace it.

For a user-owned pipx installation, close Raven and release its owned Heavy
worker, then use this sequence from the current checkout:

```bash
pipx list
pipx uninstall raven
python3 scripts/install.py
hash -r
```

This removes the pipx environment, not Raven's configuration or project evidence.
Review the saved worker Python path in setup: a path inside the removed pipx
environment is no longer valid. Use the new interpreter path printed by the
installer and verify compute-node access for Slurm. A global installation must
be updated or removed by its administrator; the user installer does not replace
system-owned commands.

### Replace an installation with the previous package name

The distribution and Python package are now both `raven`. An installation made
under a previous distribution name needs replacement before normal
`pipx reinstall raven` updates apply. Close its dashboards and release any owned
Heavy worker. Identify the previous distribution with `pipx list`, replace
`PREVIOUS_DISTRIBUTION` below with that name, and install Raven from its checkout:

```bash
pipx list
pipx uninstall PREVIOUS_DISTRIBUTION
cd /path/to/Raven
pipx install '.[terminal]'
hash -r
```

For a global installation, use `sudo pipx uninstall --global PREVIOUS_DISTRIBUTION`
and `sudo pipx install --global '.[terminal]'` instead.
Package removal preserves your application configuration and state. It does
remove the previous environment, so an existing `execution.worker_python`
value can refer to a Python executable that no longer exists.

For a standard user pipx installation, obtain the new worker executable with:

```bash
raven_worker_python="$(pipx environment --value PIPX_LOCAL_VENVS)/raven/bin/python"
"$raven_worker_python" -c 'import raven; print(raven.__version__)'
printf '%s\n' "$raven_worker_python"
```

Back up your Raven configuration and set `execution.worker_python` to that
printed path. A custom or global pipx environment needs its own executable path.
For Slurm, verify that the selected compute nodes can access the interpreter
and installed package. Run `ravenl --show-paths config explain`, `ravenl doctor`,
and `ravenh --plan` afterward. Configuration files are retained rather than
rewritten during a package update.

## Develop or install offline

For source development, keep using the checkout's launchers and the documented
offline setup:

```bash
python3 scripts/setup.py --mode demo
./scripts/ravenl --fake --no-state --no-plugins
```

This setup creates a local environment and validates the demonstration without
downloading packages. It does not expose commands on your shell's `PATH`.
Contributor tools and optional terminal dependencies need `--dev` and
`--terminal` respectively. See [CONTRIBUTING](../CONTRIBUTING.md).

For offline package installation, build or download the wheel and optional
dependencies on an approved connected machine, then install into an existing
virtual environment:

```bash
python -m pip install --no-index --find-links DIR \
  '/path/to/raven-7.0.0-py3-none-any.whl[terminal]'
```

Its commands are in that environment's `bin` directory; that directory must be
on `PATH` to invoke them by name.
