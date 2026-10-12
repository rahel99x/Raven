# Run Raven with local Slurm on Fedora

[README](../README.md) · [Installation](INSTALLATION.md) · [Runbook](runbook.md) · [Controls](CONTROLS.md)

Use this guide when Slurm is already installed directly on Fedora.
Run Raven in a terminal as your normal desktop user.
Raven uses the existing Slurm controller and command-line tools.

## 1. Check the local environment

Run these checks in your desktop terminal:

```bash
python3 --version
python3 -c 'import curses, venv; print("Python terminal modules are available")'
git --version
command -v squeue
command -v scontrol
command -v sinfo
command -v sacct
command -v sstat
scontrol ping
sinfo
squeue -u "$USER"
```

**Expected result:** Python is version 3.10 or later.
The Python module check succeeds.
Slurm commands resolve to the local installation.
The scheduler responds to the read-only checks.

An empty `squeue` result is normal when your user has no jobs.
A scheduler connection error requires attention to the existing Slurm installation.
Raven setup does not start or configure Slurm services.

Install Git, Python, and pipx through Fedora's package manager if needed:

```bash
sudo dnf install git python3 pipx
```

Use your site's installation process when package installation is managed.
Continue with your normal user after installing the missing tools.
Use a Python build with curses and venv support.
The offline source setup does not download runtime packages.

## 2. Get or update the checkout

For a new checkout:

```bash
git clone https://github.com/rahel99x/Raven.git
cd Raven
```

For an existing checkout:

```bash
cd /path/to/Raven
git pull --ff-only
```

Replace `/path/to/Raven` with your actual checkout path.
Resolve local Git changes if the update fails.
Keep existing configuration and state files.

## 3. Choose local Slurm settings

The installed `ravenl` and `ravenh` commands default to local Slurm, with empty
host, account and partition settings. Use this mode for your existing scheduler.
Raven's `--site desktop` means standalone execution **without Slurm**; do not
select it for the setup described here.

Existing legacy configurations remain supported. The legacy `desktop` profile
in `docs/config.example.json` means local Slurm. To validate that source workflow
without installing packages, you can still run:

```bash
python3 scripts/setup.py --mode local --profile desktop
```

**Expected result:** Source setup checks local capabilities and prints a desktop launch command.
Setup also validates simulated displays and writes its demonstration report.
The demonstration report does not replace live scheduler data.

The legacy `desktop` profile is defined in `docs/config.example.json`.
It clears configured host, user, account, and partition restrictions.
It therefore uses local Slurm commands and normal identity detection.

Raven applies its governed collection limits to installed and source launchers.
The default queue
interval is 60 seconds, accounting is 300 seconds, and automatic GPU steps are
disabled. See [collection and retention](raven/README.md#collection-and-retention).

Use `--profile desktop` explicitly for the legacy source setup.
The default setup profile remains `carc` for existing CARC installations.
Profile selection does not create a scheduler account or partition.
The profile enables `show_all_partitions`.
A nonempty configured `partitions` list still restricts the displayed partitions.

<a id="keep-desktop-state-separate"></a>

### Existing legacy desktop state

Older Tower installations used `state_namespace = "desktop"` to store desktop
preferences, log bookmarks, job tags, and recorded resource samples separately
from CARC state. Keep those existing files when installing Raven.

The default desktop state path is:

```text
~/.local/state/tower/profiles/desktop/<connection_digest>/
```

The digest identifies the connection, scheduler user, local owner, and profile.
This separation prevents another cluster's saved filters or reused job IDs from affecting desktop records.
Use the [state configuration reference](reference.md#configuration) for custom namespaces and base paths.
Installed Raven normally stores its own state under `~/.local/state/raven`.
Review [migration](raven/README.md#migrate-without-changing-evidence) before
importing legacy state; installing commands does not move existing records.

## 4. Install `ravenl` and `ravenh`

From the checkout, install the package and optional embedded terminal:

```bash
pipx install '.[terminal]'
cd "$HOME"
command -v ravenl
command -v ravenh
ravenl --version
ravenh --version
```

**Expected result:** Both names resolve to installed executables from outside
the checkout. No aliases, environment activation, or `.bashrc` edits are needed.
Use `pipx install .` if you do not need the embedded terminal. Pipx's executable
directory, normally `~/.local/bin`, must already be on `PATH`; see
[installation options](INSTALLATION.md#use-an-existing-directory-on-path) for
an existing writable directory on `PATH` or a machine-wide administrator install.

For a new persistent local Slurm configuration, run:

```bash
ravenl --write-config
ravenl config explain
ravenh --plan
```

The configuration is `~/.config/raven/config.json`, honoring `XDG_CONFIG_HOME`.
Both commands load it automatically. `--write-config` refuses to replace an
existing file; inspect and edit your existing configuration when present.
Set the cluster identity, account, partition, QOS and requested resources for
your scheduler. An empty account or partition uses Slurm's configured defaults.
Check the worker Python path is available on your selected compute nodes.

To export supported effective settings from the supplied legacy local-Slurm
profile into a **new** Raven configuration instead, use the actual checkout path:

```bash
ravenl --config /absolute/path/to/Raven/docs/config.example.json --profile desktop --write-config
```

Use your own legacy file's path if you customized it. This preserves the original
file and applies Raven's collection limits. It exports supported Raven settings,
not every legacy UI or state option, and refuses to overwrite existing Raven
configuration. Choose one configuration creation route.

After a later `git pull --ff-only`, run `pipx reinstall slurm-tower` to update
the installed package. The [installation guide](INSTALLATION.md#update-the-installed-package)
covers custom executable directories and administrator updates.

<a id="account-and-profile-overrides"></a>

### Account overrides

Use an explicit account for one invocation when your scheduler requires it:

```bash
ravenl --account YOUR_SLURM_ACCOUNT
ravenh --account YOUR_SLURM_ACCOUNT --plan
```

Replace `YOUR_SLURM_ACCOUNT` with a configured Slurm account. Set
`allocation.account` in Raven's configuration for a persistent default.
The installed commands do not inject `CARC_ACCOUNT` or `SLURM_TOWER_ACCOUNT`.

### Optional legacy `tower` alias

Existing aliases can remain for compatibility. The
[legacy Bash helper](../scripts/install_shell.py) is optional; it targets the
source checkout and edits Bash configuration only when invoked with `--apply`.
Preview its desktop block without changing shell files:

```bash
cd /path/to/Raven
python3 scripts/install_shell.py --profile desktop
```

Keep the checkout at its current location.
Rerun the helper if you move it.
Its launcher uses the checkout's `.venv` when that environment exists.

The desktop alias ignores `CARC_ACCOUNT`.
Use `SLURM_TOWER_ACCOUNT` for an explicit desktop account override:

```bash
export SLURM_TOWER_ACCOUNT=YOUR_SLURM_ACCOUNT
tower
```

Replace `YOUR_SLURM_ACCOUNT` with a configured Slurm account.
Use `unset SLURM_TOWER_ACCOUNT` to restore normal account detection for desktop.
Use `tower --account ACCOUNT` to override the account for one invocation.

`SLURM_TOWER_PROFILE` overrides the profile selected during alias installation.
`SLURM_TOWER_ACCOUNT` applies to any resolved profile.
The CARC alias uses `CARC_ACCOUNT` only when the generic override is unset.
An explicitly empty `SLURM_TOWER_ACCOUNT` suppresses that legacy account injection.

A custom `TOWER_CONFIG` also overrides the alias's example configuration path.
That custom file must define the selected profile.
These overrides belong to the optional alias. Use the direct legacy launcher
below to check the supplied desktop configuration independently.

## 5. Verify real desktop data

```bash
ravenl --version
ravenl doctor
ravenh --plan
ravenl --once --tab jobs
ravenl --once --tab sources
ravenl
```

**Expected result:** Jobs reflects `squeue -u "$USER"`.
Sources reports actual command status, freshness, latency, and errors.
Press `?` for help.
Press `q` to exit.

From the checkout, these legacy source commands explicitly select its supplied
local-Slurm configuration:

```bash
./scripts/tower --config docs/config.example.json --profile desktop --doctor
./scripts/tower --config docs/config.example.json --profile desktop --once --tab sources
./scripts/tower --config docs/config.example.json --profile desktop
```

Do not use `--fake` to verify desktop scheduler data.
That option selects simulated data.
An empty Jobs table is normal when no jobs are running or pending for your user.

### Read accounting availability correctly

A local Slurm installation can run jobs without an accounting backend.
In that installation, `sacct` can be present while its queries are unavailable.
History and completed-job efficiency then remain unavailable or empty.
Sources reports the accounting error.
Raven does not manufacture completed jobs to fill History.

If Sources shows `finished: sacct: error: Slurm accounting storage is disabled`,
the scheduler has no active accounting storage backend.
Installing the `sacct` command alone does not enable that backend.
Follow [Enable Slurm accounting on a Fedora desktop](FEDORA_ACCOUNTING.md)
to add persistent job history with MariaDB and `slurmdbd`.

To stop unavailable accounting queries for the current Raven session,
enter `:source finished off` and `:source fin_details off` in the command palette.
These source toggles do not persist across Raven restarts.
They suppress the queries; they do not supply completed-job history.

When accounting is delayed, Recents can show jobs as awaiting accounting.
That status does not prove success or failure.
Keep the exact job's logs for application evidence.
Use the [project reporting standard](PROJECT_STANDARD.md) to retain explicit run and log identities.

## 6. Adjust the terminal and profile

Use a UTF-8 locale and a compatible monospace font for block graphics.
Try the portable display if characters are misaligned:

```bash
ravenl --ascii --no-color
```

Use `:terminaldoctor` for terminal and path evidence.
Use `:terminaltest` to check glyphs, keys, and mouse input.
Use `:settings` to adjust display preferences and active sampling intervals.

### Use live inspection controls

Select a job in Jobs or Recents.
Click Inspector, Logs, Investigate, Research, or Analytics in Details to inspect that job in place.
Select Off to hide the inspection content.
Use `:jobpanel focus` for keyboard entry.
The selected job and available files update during the session.
Research includes all twelve workspaces; Analytics includes all five subviews.
Choose a subview and scroll its content vertically.
Inline views fit the panel width without horizontal panning.

Press F8 to focus visible controls and use arrow keys to choose one.
Press Enter or Space to activate it, or Esc to return to content.
Hover highlights controls when the terminal reports pointer movement.
Drag visible job rows to mark a range; Shift preserves existing marks.
Review the complete target list before confirming a bulk action.

The top-right update slider shows the effective Jobs fetching interval.
Press and drag its track to request shorter or longer intervals.
Its labels use `s`, `ms`, or `µs`; ASCII mode uses `us` for microseconds.
Use `:rate` to inspect the effective Jobs interval.
Its logarithmic scale spans five seconds to 500 milliseconds.
Use `:rate N` to select a position from one to 50, or `:rate reset` to restore five seconds.
Raven enforces its queue and statistics floors even when a slider
requests a shorter interval; inspect Sources for the admitted cadence.
GPU polling also spans five seconds to 500 milliseconds. Retry backoff and command duration can delay actual measurements.
Sources shows the effective cadence.
Running metric graphs have a separate polling slider next to their display-window slider.
The window spans 30 seconds to one second; its right-click reset restores 30 seconds.
The polling slider spans five seconds to 500 milliseconds; its right-click reset restores a five-second request.
See [chart controls](guides/charts.md#follow-a-running-metric) to adjust one exact job's reads.
See [Live workbench](guides/live-workbench.md) for the toolbar, controls, limits, and project reporting procedure.
Use `:smoothscroll off` for immediate wheel movement.
Use View → Disable startup animation or `:startup off` to suppress the welcome.
The reader theme and `animations = false` suppress both motion effects.
See [Mouse and button navigation](guides/pointer-navigation.md) for saved preferences and terminal input checks.

### Optional desktop clipboard

For a Fedora Wayland session, install the optional local clipboard helper:

```bash
sudo dnf install wl-clipboard
```

For an X11 session, use `sudo dnf install xclip` instead.
Run Raven as the user who owns that desktop session.
Raven detects these tools automatically.
Log selection uses `y`; complete log export uses `Y`.
If clipboard delivery is unavailable, the private exported file remains available in Activity and the export library.

The legacy desktop configuration requests GPU sampling for running GPU allocations.
CPU-only jobs do not trigger GPU sampling.
Use `:gpuprovider` or **GPU source** on Sources to select Auto, NVIDIA, AMD, or
Intel. The selected vendor utility and `python3` must be available inside the
allocation. See [provider setup](guides/phase-one.md#select-a-gpu-provider).
Live GPU sampling can create short `srun` steps inside an existing allocation.
Raven disables automatic GPU steps by default with both installed and source launchers.
Use `:gpu off` or `--no-gpu` to disable it.
An earlier saved off preference remains off until you enter `:gpu on`.

If GPU graphs are missing, run:

```bash
tower --gpu-check --gpu-check-output ./tower-gpu-report
```

The directory must not exist yet.
Read [GPU detection checks](guides/gpu-detection.md) for the complete procedure.

## 7. Use another scheduler environment

For Slurm inside a container, run Raven in the same container or scheduler namespace.
That environment must expose the Slurm commands and the referenced log paths.
Use your existing container entry procedure.

An existing SSH endpoint is another option when it can access the scheduler and logs:

```bash
./scripts/tower --config docs/config.example.json --profile desktop --host YOUR_SSH_ALIAS --doctor
./scripts/tower --config docs/config.example.json --profile desktop --host YOUR_SSH_ALIAS
```

Replace `YOUR_SSH_ALIAS` with a working alias from your SSH configuration.
Establish normal SSH access before you run the remote checks.

On Windows, use a suitable WSL environment with Python curses support.
Its Slurm command configuration must reach the intended scheduler.
A Slurm installation in another environment does not automatically configure WSL.
See [SSH transport](runbook.md#optional-ssh-transport) for remote prerequisites.

## 8. Troubleshoot desktop launch

| Symptom | Action |
| --- | --- |
| `ravenl` or `ravenh` is not found | Check pipx installation and that its executable directory is already on `PATH` |
| Installed commands show an older release after pulling | Run `pipx reinstall slurm-tower` from the updated installation source |
| An unexpected profile is selected | Inspect `ravenl config explain`, `RAVEN_CONFIG`, `RAVEN_PROFILE`, and legacy `TOWER_*` overrides |
| An unexpected account is selected | Inspect `allocation.account` and the invocation's `--account` value; optional aliases also read `SLURM_TOWER_ACCOUNT` |
| A custom configuration has no desktop profile | Add that profile or use the direct example-config launcher |
| The scheduler connection fails | Compare `scontrol ping`, `sinfo`, and the error shown in Sources |
| Jobs is empty | Compare `squeue -u "$USER"` and the current table filters |
| History is empty or errors | Inspect `sacct` availability and the configured accounting backend |
| Source commands are unavailable | Use the terminal environment that exposes the existing Slurm installation |
| Log paths are unavailable | Run Raven where those paths exist or register exact accessible log locations |
| GPU graphs are missing | Use `:gpu on`, then `tower --gpu-check` to inspect settings, allocation, driver, and sampling evidence |
| Unicode characters are misaligned | Use a compatible font or select `--ascii` |

Use the read-only checks in this guide to verify your existing desktop scheduler.
