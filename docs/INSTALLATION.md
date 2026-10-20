# Install the Raven commands

Install the `raven` distribution to get real `ravenl`, `ravenh`, and `raven`
executables. The installed commands run from any working directory without
aliases, virtual-environment activation, or changes to `.bashrc`. They use the
installed package, so launching them does not require the source checkout.

Use Linux or WSL2 with Python 3.10 or later and curses support for an installation
with persistent local state. This revision verifies mutable-state filesystems
through Linux mount information. macOS persistent-state ownership is not yet
supported or qualified; use `ravenl --no-state` for a stateless session there.
macOS behavior has not been exercised in this qualification environment. Install
Python's `venv` support if it is packaged separately by your operating system.
The repository installer needs no pipx or administrator access. Package
installation needs access to the configured Python package index. The core has
no third-party runtime dependencies; the optional embedded terminal uses `pyte`
and `wcwidth`.

## Install with one command

With Python 3.10+, `venv`, Git, and curl available, run:

```bash
curl -fsSL https://raw.githubusercontent.com/rahel99x/Raven/main/install.sh | bash
```

The bootstrap downloads Raven's source and delegates to the same repository
installer used below. It keeps the checkout at
`~/.local/share/raven/source`, or `$XDG_DATA_HOME/raven/source`. An existing valid
checkout there is reused without pulling or resetting it. Guided setup opens
when a controlling terminal is available; otherwise the installer prints the
command to run next. Installation does not request an allocation.

The commands normally appear in `~/.local/bin`. If that directory is absent
from your current shell's PATH, follow the exact `export PATH=...` instruction
printed by the installer, or use the printed absolute command. An installer
cannot change its parent shell's environment and does not edit shell files.

To choose paths or a Python interpreter, pass options to Bash:

```bash
curl -fsSL https://raw.githubusercontent.com/rahel99x/Raven/main/install.sh | \
  bash -s -- --source-dir "$HOME/Raven" --clone --python python3 --no-setup
```

`--source-dir PATH` selects an existing bootstrap checkout. Add `--clone` to
create that explicit path; a missing path otherwise stops with a spelling hint.
`--python EXEC` selects Python. Automatic discovery tries Python 3.14 through
3.10 before `python3` and `python`. Other options are forwarded to the repository
installer. Bash 3.2 and later are supported by the bootstrap.
`--plan` makes no checkout, environment, or command changes and does not fetch
source. With an existing checkout, it also checks installation destinations;
without one, it previews where source would be cloned. The initial download of
`install.sh` still occurs when using the curl form.

## Install from a checkout

```bash
git clone https://github.com/rahel99x/Raven.git
cd Raven
bash install.sh
```

The local script installs its own checkout. You can also invoke
`python3 scripts/install.py` directly when Git and curl are unnecessary.

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

User-selected installation roots are resolved once; symbolic links inside the
chosen root remain prohibited.
For example, preview a custom command directory before installing:

```bash
python3 scripts/install.py --bin-dir /absolute/path/to/existing/bin --plan
python3 scripts/install.py --bin-dir /absolute/path/to/existing/bin
```

If the command directory is not on PATH, the installer prints an absolute setup
command and a one-session `export PATH=...` command. Use either; rerunning an
installer cannot change an already running parent shell's PATH.

### Existing pipx users and alternative installation

Keep using pipx if it already owns your Raven installation; `raven update`
supports verified user-owned pipx installations made from a local Git checkout.
The repository installer does not take over their commands. For a new pipx
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

Close Raven dashboards and their owned Heavy workers before updating. The
updater does not cancel scientific jobs or terminate running Raven processes.
Then run:

```bash
raven update
```

The command fetches the configured upstream for the source checkout's current
branch and updates through the installer that owns the running command. It
does not silently switch to `main`. A branch without an upstream, local code
edits, untracked source files, or local commits beyond upstream stop the update
with guidance. Changes to recognized user configuration are preserved and do
not block an ordinary update.
There is no background update check or automatic replacement during a session.

The default update policy requires the declared official Raven repository and
prints the full immutable source-commit range before building. A deliberately
configured fork requires `raven update --allow-fork`. Source is obtained through
the configured authenticated Git transport; a local checksum detects changes
but is not a publisher signature. Build-tool requirements are pinned.

For a release artifact with a GitHub/Sigstore provenance attestation, require
verification explicitly:

```bash
raven update --require-attestation --attested-artifact /path/to/downloaded-artifact.whl
```

This needs GitHub CLI `gh` and an artifact from the exact selected commit.
`--attestation-bundle FILE` optionally supplies its downloaded bundle. A missing
artifact, unavailable verifier, signer mismatch, or unsuccessful verification
stops before checkout mutation. `--plan` validates the policy without invoking
the verifier. Ordinary branch commits may have no release artifact: the default
source-channel policy and attested release verification are distinct guarantees.

Recognized configuration, application state, installation paths, and recorded
extras are preserved. Project data outside the source checkout is unaffected.
For the repository installer, a fresh release
is built and checked from a pristine export before the active release changes.
A failed build keeps the previous environment active and restores the source
backup automatically when source mutation occurred. If restoration itself cannot
complete, Raven prints the retained recovery path and actual active release.
Its stable worker interpreter remains
`PREFIX/current/bin/python`, using the prefix printed during installation.

```bash
raven update --plan
raven update --check
```

`--plan` shows the source, branch, installer, destinations, and cached upstream
revision without network access or changes. `--check` fetches upstream
information and reports available changes without updating the checkout or
installed package. Use
`raven update --reinstall` to rebuild an installation even when its recorded
revision is already current.

If you moved a repository-installer checkout, identify its new location:

```bash
raven update --source /new/path/to/Raven
```

For older installer markers, Raven uses the owned command link and installed
dependencies to recover installation choices where they are unambiguous.
Otherwise it prints a reinstall path rather than guessing. Ordinary virtual
environments, system installations, administrator-owned or other-user pipx
installations, and packages installed from a wheel or package index remain with
their existing installer. The updater does not replace them automatically.
The installation directory and command directory must also be outside the
source checkout. If either is inside it, use the original installer to choose
external destinations first; maintenance stops before changing that layout.

The default XDG configuration and `RAVEN_CONFIG` are recognized automatically.
If you keep additional configuration files inside the checkout, identify each
one explicitly, including during an ordinary update:

```bash
raven update --config /path/to/Raven/my-config.json
```

Repeat `--config PATH` for additional files. This preserves those paths while
updating surrounding source. Do not assume an arbitrary edited file is
configuration merely because it contains settings.

### Replace local source edits deliberately

Commit or copy development work you intend to keep. To explicitly replace local
code and untracked source with the fetched upstream revision, run:

```bash
raven update --overwrite
```

Raven first creates a backup outside the checkout and prints its location and
recovery command. User configuration, state, and ignored environments remain
preserved. `raven/config.py` is application code, so edits to it are source
changes; use a user configuration file for persistent settings.
Other nonignored files inside the source checkout count as source, including
personal notes or datasets; move them outside the checkout or explicitly protect
their paths before using `--overwrite`.

Include the same configuration paths when replacing local code deliberately:

```bash
raven update --overwrite --config /path/to/Raven/my-config.json
```

### Restore a backup

Use the exact recovery command printed by the updater. For example, with the
actual backup directory substituted for `/path/to/raven-backup`:

```bash
raven revert /path/to/raven-backup --plan
raven revert /path/to/raven-backup
```

The preview requires no network access. Reversion saves the current source
before restoring the selected backup and preserves your **current** user
configuration and state. A repository-installer installation reselects its
validated retained previous release. A supported pipx installation reinstalls
from the restored source; its recovery and failure guarantees remain those of
pipx. Review the printed outcome if that reinstall fails.
Raven retains the three newest completed recovery backups and any explicitly
selected recovery point, together with the releases those backups reference.
It prunes matching Git backup refs under the maintenance lock. A standalone
install without recovery backups keeps the current and previous releases.
Abandoned stages with Raven's private ownership marker are swept after one
hour when their recorded process no longer exists. Older unmarked directories
are preserved because Raven cannot establish their ownership history.
If you moved a managed checkout, pass `--source /new/path/to/Raven` when needed
for recovery and later updates: a retained release still records its original
source location.

After an update or reversion, check the installed commands outside the checkout:

```bash
raven --version
ravenl doctor
ravenh --plan
```

### Update a pipx installation

`raven update` supports a same-user pipx installation when its metadata identifies
the running environment and an original local Git checkout. It retains that
source and its extras and delegates package replacement to pipx. The updater
builds a pristine wheel and asks `pipx runpip` to install it, preserving pipx's
original source metadata. It does not reuse the checkout's `build/` directory.
The updater
derives `PIPX_HOME` and the command directory from that verified installation,
including a custom user-owned location. Pipx does not inherit the repository
installer's atomic release-switch guarantee.

Keep the original checkout path recorded by pipx. A moved pipx checkout cannot
be adopted with `--source`; follow the diagnostic to repair or reinstall through
pipx first. Wheel, package-index, suffixed, administrator-owned, other-user, or
mismatched pipx environments use their owning installation workflow instead.

For an older Raven version that does not yet provide `raven update`, update the
checkout used by pipx and reinstall:

```bash
cd /path/to/Raven
git pull --ff-only
pipx reinstall raven
```

Pipx retains the original installation source and extras. Use the same
`PIPX_HOME` and `PIPX_BIN_DIR` overrides if you selected them. For an administrator
installation, use `sudo pipx reinstall --global raven`.

Pulling source alone does not update a normally installed package. Reinstallation
preserves user configuration and state; it needs the recorded source checkout.

### Update an older repository installation

If `raven update` is not yet available, run the installer from the original
checkout after updating it:

```bash
cd /path/to/Raven
git pull --ff-only
python3 scripts/install.py --no-setup
hash -r
```

Keep any original `--prefix`, `--bin-dir`, or `--without-terminal` choices for
this manual step. If Git reports local edits, preserve them before pulling.
For a checkout whose remote still uses an old repository name, first run
`git remote set-url origin https://github.com/rahel99x/Raven.git` there.
The checkout directory itself does not need to move.

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
