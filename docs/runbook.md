# Project Raven: compatibility runbook

Use the [Raven runbook](raven/README.md) for release 7.0 launchers, execution
profiles, allocation controls, collection limits, migration, and rollback.
The `tower` commands below remain compatibility examples. Their effective
polling and execution limits now follow the Raven policy. Use `--site` with
new Raven configuration files; retain `--profile` only with old Tower files.

[README](../README.md) · [Controls](CONTROLS.md) ·
[Desktop setup](DESKTOP.md) · [Tower 4.0 guides](QUALITY_OF_LIFE.md) ·
[Complete reference](reference.md)

The [generated operation reference](reference/operations.md) lists each current
operation key, input field, default, and allowed choice.

For the optional embedded terminal, run
`python3 scripts/setup.py --mode demo --terminal`, then
`./scripts/tower terminal selftest`. This reuses `.venv` and does not change the
selected Slurm profile. Open **Terminal** beside **Details** in a supported page.
Use **Ctrl-]**, then **t**, to return input to Tower. The
[terminal guide](guides/terminal-fusion.md) covers offline wheels, sessions,
copying, shell hooks, recordings and compatibility checks.

For the task-oriented workbench, use `:workbench run`, `:workbench investigate`,
`:workbench compare`, `:workbench results`, or `:workbench connect`.
`:workbench all` opens the complete operations catalog. Use
`:projectguide /path/to/project` to inspect reporting readiness before editing
a launcher. Opening a task does not submit a job or execute an adapter.
The [project connection guide](guides/project-connection.md) explains exact sample
selection, content checks and generated Bash hooks. Use **Pinned** or
`:workbench pinned` to return to your saved operation shortcuts.

Read the [implementation ledger](FLAGSHIP_IMPLEMENTATION.md) for exact advanced
feature scope. Prepared [stack tracing](guides/stack-tracing.md),
[scientific investigations](guides/scientific-investigation.md), and
[runtime research controls](guides/runtime-research.md) each have a separate
input contract. For deployments, follow [administration](ADMINISTRATION.md),
[qualification](QUALIFICATION.md), and the [support policy](SUPPORT_POLICY.md).

Run Tower on your CARC login node or in a terminal with working local Slurm tools.
Use the [desktop guide](DESKTOP.md) for Slurm installed directly on Fedora.
Setup is noninteractive and
repeatable. It never submits or changes jobs, installs system packages, replaces
your configuration, or asks for a password.

To instrument another application, follow the
[project reporting standard](PROJECT_STANDARD.md) and copy its
[project template](../examples/project-template/README.md). It defines the run
directories, metrics, final reports, contracts, and analysis exports Tower reads.

Release 4.14 adds optional measurement and submission checks. Follow the
[phase one setup guide](guides/phase-one.md) to select a GPU provider, inspect
upstream sampling, check local shell scripts, and attach array input manifests.
These features do not require Python packages. Vendor GPU tools and ShellCheck
are optional external commands; Tower setup does not install them or alter
Slurm services. Existing aliases and profiles remain valid after an update.

## 1. Connect to CARC and check the tools

Use your institution's documented SSH hostname and username. Complete the normal
key, passphrase, MFA, and host-key verification flow, then run these commands on
the login node:

```bash
python3 --version
command -v squeue
command -v scontrol
```

Use Python 3.10 or newer. If Python or Slurm is unavailable, check your site's
module documentation. Where a module system is provided, `module avail` lists
available modules; load the actual Python/Slurm modules your site specifies.
Module names and available versions vary, so no specific module name is assumed.

The application uses standard-library Python and POSIX curses on Linux, macOS,
or WSL. There are no runtime packages, databases, servers, or build tools to install.
Use the site's existing Slurm installation.
Tower setup does not start or configure a Slurm controller.

### Use Slurm already installed on Fedora

Check the local scheduler as your normal desktop user:

```bash
python3 --version
python3 -c 'import curses, venv; print("Python terminal modules are available")'
scontrol ping
sinfo
squeue -u "$USER"
```

These scheduler checks are read-only.
An empty user queue is normal.
Use the desktop setup procedure below when the existing local scheduler responds.
See [Fedora prerequisites](DESKTOP.md#1-check-the-local-environment) if Python or commands are missing.

## 2. Get the source and set up locally

```bash
git clone https://github.com/rahel99x/Slurm-Tower.git
cd Slurm-Tower
python3 scripts/setup.py --mode local
./scripts/tower --config docs/config.example.json --profile carc
```

If the login node cannot access GitHub, download the repository on a permitted
machine and transfer the source directory using your site's file-transfer procedure.
Then run setup from that directory. The default setup is offline and needs no
administrator access.

The `carc` example profile configures slower history and cluster-source bases,
disables in-allocation GPU sampling, disables forecast probes, and disables allocation-budget queries. Start
there, then adjust the profile to your site's usage guidelines. It does not assume
an account name, partition, scheduler version, or institution-specific hostname.
The global Jobs and live-resource controls use five-second polling by default and can request intervals down to 500 milliseconds.

Every mode validates simulated JSON output, all ten terminal tabs, all twelve
Research workspaces in Unicode and ASCII, and a plain-text report.
Local mode checks command availability; it does not query or
change your jobs. A successful diagnostic confirms tools, not the health of the
Slurm controller or your site's authorization rules. Verify real data in step 4.

Setup creates `.venv` without downloading pip or packages. Existing valid virtual
environments are reused; unrelated directories are rejected. Keep the checkout:
the `./scripts/tower` launcher runs its source with `.venv/bin/python` when present,
otherwise `python3`. To run without setup, use `python3 -m tower` from the checkout.

### Select the desktop profile

For Slurm installed directly on Fedora, run these commands from the checkout:

```bash
python3 scripts/setup.py --mode local --profile desktop
./scripts/tower --config docs/config.example.json --profile desktop
```

The desktop profile clears configured host, user, account, and partition restrictions.
It uses local commands and normal identity detection.
Cluster includes reported idle CPU-only partitions.
Jobs and live resources poll every 5 seconds at the default update setting; accounting polls every 10 seconds.
It enables GPU sampling for detected running GPU allocations.
Queue forecast probes and allocation-budget queries remain disabled.
The desktop profile also uses a separate state namespace.
Desktop preferences, log bookmarks, tags, and recorded resource samples stay separate from existing CARC state.
See [Desktop state](DESKTOP.md#keep-desktop-state-separate) for the directory layout.

Setup accepts `--profile PROFILE` from the supplied example configuration.
It checks the selected profile and prints the corresponding launcher.
The default profile remains `carc` for compatibility with existing installations.

### Use one `tower` shell command

After setting up the application, install the optional Bash alias from the new
Slurm-Tower checkout on CARC. If this is an older clone, run `git pull --ff-only`
first to obtain `scripts/install_shell.py`; preserve any conflicting local edits.

```bash
python3 scripts/install_shell.py --apply
source ~/.bashrc
type tower
tower
```

The helper backs up `.bashrc` before updating it atomically and prints the backup
location. Its managed block clears the old `tower`, `dash`, and `dash2` aliases or
functions, then defines one `tower` alias for this checkout's `scripts/tower` with
the low-impact `carc` profile and Unicode visuals. It preserves the rest of your
shell configuration.
Rerunning the helper updates the same managed block.

For desktop use, select the profile during alias installation:

```bash
python3 scripts/install_shell.py --profile desktop --apply
source ~/.bashrc
type tower
```

The desktop alias uses local scheduler settings.
It ignores `CARC_ACCOUNT`.
Use the helper without `--apply` to preview the selected profile's alias block.

The launcher uses the Slurm-Tower checkout's own `.venv`, independently of an
active virtual environment for another project. Run normal setup first so that `.venv` exists;
without it, the source launcher falls back to `python3` on `PATH`. Keep this
checkout at its current location, or rerun the helper after moving it.

The alias passes your arguments through.
`SLURM_TOWER_PROFILE` overrides the profile selected during alias installation.
`SLURM_TOWER_ACCOUNT` supplies an account override for any resolved profile.
An explicitly empty value suppresses legacy account injection.
When that variable is unset, only the CARC profile can use `CARC_ACCOUNT`.
Without an account override, Tower retains normal account detection.
Use `tower --account ACCOUNT` to override the account for one invocation.
Use the [desktop guide](DESKTOP.md#account-and-profile-overrides) for examples.

```bash
tower --doctor
tower --once --tab sources
tower --fake
tower --ascii --no-color     # portable fallback for a limited terminal
```

To inspect the alias block without changing a file, run the helper with no flags.
To create a complete updated-file preview, choose an output path:

```bash
python3 scripts/install_shell.py
python3 scripts/install_shell.py --output /tmp/tower-bashrc-preview
```

Use `--bashrc /path/to/bashrc` to select a different Bash configuration file for
preview or installation. The helper modifies shell startup configuration only
when you explicitly pass `--apply`. Source the file you updated, or open a new
Bash session, to load the alias.

### Preview and choose terminal visuals

Tower renders every chart inside the terminal; no browser, graphical display, or
image viewer is needed. A UTF-8 terminal and a font with block and braille glyphs
provide the fullest visuals. Unicode is the default, with an automatic ASCII
fallback for unsupported output encodings and dumb terminals. `--unicode` selects
the Unicode preference explicitly, even when `$TERM` is `dumb`, while an unsupported
output encoding still requires ASCII. `--ascii` selects portable characters. An
explicit `tower --ascii` also overrides the alias's Unicode preference.

Try the full dashboard without contacting the cluster:

```bash
./scripts/tower --fake --unicode
# Or print one simulated Analytics frame in the current terminal:
./scripts/tower --fake --unicode --once --tab analytics
```

Let the demo run briefly to build sample history, then use `Tab` for the ten pages
and the left/right arrows for each page's alternate views. `T` cycles themes.
The `mono` theme keeps the glyphs without colors; `reader` uses plain ASCII text.
`:theme dark` or `:theme light` selects coordinated terminal surfaces;
`:theme terminal` retains your terminal's background. `:density compact` suits
small screens, while `comfortable` and `focused` expose independently scrollable
Main/Details panels. `Ctrl-W` changes focus and `z` maximizes the focused panel
on non-Logs pages; `:maximize` is available as a command on Logs too.
`--no-color` or a nonempty `NO_COLOR` environment variable disables color.
The interactive curses display uses native truecolor when ncurses and the terminal
description expose direct colors; otherwise it uses 256 or basic colors.
ANSI output from `--once` or `--watch` also supports truecolor
when the terminal advertises it through `COLORTERM=truecolor`, `COLORTERM=24bit`,
or a direct-color `$TERM`. Missing data remains visibly unknown rather than
appearing as zero usage. Plain-text reports always use ASCII, independently of
the live dashboard's visual preference.

The interactive welcome can show a short Unicode animation.
Use View → Disable startup animation or `:startup off` to suppress it.
Use `:startup preview` to inspect it without changing the preference.
The reader theme and `animations = false` skip the welcome and use immediate scrolling.
Use [Mouse and button navigation](guides/pointer-navigation.md) for saved preferences and terminal support.

## 3. Adapt setup to your environment

| Your environment | Setup | Launch |
| --- | --- | --- |
| CARC or another Slurm login node | `python3 scripts/setup.py --mode local` | `./scripts/tower --config docs/config.example.json --profile carc` |
| Slurm installed directly on Fedora | `python3 scripts/setup.py --mode local --profile desktop` | `./scripts/tower --config docs/config.example.json --profile desktop` |
| Try the simulated cluster | `python3 scripts/setup.py --mode demo` | `./scripts/tower --fake` |
| Let setup detect it | `python3 scripts/setup.py` | Use the command it prints |
| Optional SSH transport from another machine | `python3 scripts/setup.py --mode remote --host your-ssh-alias` | `./scripts/tower --host your-ssh-alias` |

Automatic mode uses remote when given `--host`, local when `squeue` is on `PATH`,
and demo otherwise. If a Slurm command is present but the installation is incomplete,
setup reports the missing capability; it does not disguise a broken cluster as a demo.

```bash
# Choose a separate virtual environment; use the launch command setup prints.
python3 scripts/setup.py --mode demo --venv /path/to/tower-venv

# Read a complete ASCII snapshot in the terminal.
less .tower/demo.txt

# Save a fresh demo report without replacing an existing file.
python3 scripts/setup.py --mode demo --report /tmp/tower-preview.txt

# Optional contributor tools and full regression suite; requires a package index.
python3 scripts/setup.py --mode demo --dev --test
```

`--dev` bootstraps pip inside the venv if needed, then installs the editable project,
pytest, and packaging tools using the configured pip index with normal verification.
Development tools are unnecessary for everyday monitoring. Setup exits `0` when
requested checks pass and `1` with a specific failure otherwise (`2` indicates
invalid command-line syntax). Demo validation still completes if local or remote
prerequisites are missing. Successful local and remote setup prints the selected
example profile's launch command. The default profile is `carc`; select `desktop`
explicitly for the local desktop configuration. Demo setup prints `--fake`.

### Optional SSH transport

The primary workflow runs Tower directly on the login node. If you choose to run
its terminal interface on a different machine, `--host` sends Slurm commands and
log reads over your existing SSH connection:

```bash
ssh your-ssh-alias 'command -v squeue; command -v sacct'
./scripts/tower --doctor --host your-ssh-alias
./scripts/tower --host your-ssh-alias --no-gpu
```

Doctor uses a bounded, read-only SSH check in batch mode. Establish normal SSH
access first; do not disable host-key checking. Put a bastion, nondefault port, or
login username in your normal `~/.ssh/config`, then pass that SSH alias to Tower.
Remote log reads use Linux coreutils (`stat`, `dd`, `base64`, and `ls`).

Slurm must be on the remote noninteractive `PATH`, not only in an interactive
shell. Follow the site's module-loading instructions if those differ. No SSH
keys or tokens belong in the repository; Tower uses your existing credentials.

## 4. Verify real data with low-impact settings

```bash
./scripts/tower --doctor
./scripts/tower --config docs/config.example.json --profile carc --once
./scripts/tower --config docs/config.example.json --profile carc --once --tab sources
```

Jobs and Cluster should match your usual `squeue` and `sinfo` views. Sources shows
each command's status, latency, retries, and last error. Missing optional accounting,
allocation, or GPU features can leave those sections empty while the queue works.

The `carc` profile disables live GPU collection. If site policy allows it, enable
`gpu_sampling` in your personal profile. It runs short `srun` steps in an existing
allocation and may fall back to SSH to the assigned node. `--no-gpu` disables it
for one run. Forecasts use `sbatch --test-only` probes: these do not submit jobs but
do contact the scheduler, and are also disabled in the `carc` profile.

The dashboard reads job and log data. Job mutations are separate actions with
confirmations. `tower run ... --yes` is explicit unattended authorization for
that action; do not add it to a generic health check.

### Inspect experiments and plan the next run

Press `0` for Research and use Left/Right to move among its twelve workspaces.
The first six cover metric streams, arrays, failure evidence, output contracts,
passports and submission preparation. The next six show resource predictions,
queue-start forecast calibration, scheduler blockers, resource tradeoffs,
controlled scaling and workflow critical paths.

```bash
./scripts/tower --fake --tab research --research-view predict
./scripts/tower --fake --tab research --research-view scaling
./scripts/tower run scaling plan examples/planning/scaling.json --workdir "$PWD"
./scripts/tower run workflow analyze examples/planning/workflow.json
```

Preparing a scaling or workflow plan inspects explicit local inputs and prints
reviewable commands. It does not submit jobs or run scheduler probes. Evidence
gaps remain visible; resource predictions use matching completed runs, calibrated
queue windows require recorded start outcomes, and failed/censored scaling runs
cannot support relative performance claims. Use the [wave two guide](WAVE_TWO.md)
for the complete recipes, commands and assumptions, and the
[research guide](RESEARCH.md) for metric instrumentation and output validation.

To measure the bounded CPU example locally without Slurm, choose three new paths:

```bash
.venv/bin/python examples/planning/scaling_workload.py local --workers 1 --problem-size 10000 --output one.json
.venv/bin/python examples/planning/scaling_workload.py local --workers 2 --problem-size 10000 --output two.json
.venv/bin/python examples/planning/scaling_workload.py combine one.json two.json --output measurements.json
./scripts/tower run scaling analyze measurements.json --mode strong --baseline 1
```

The example writes actual measured elapsed times, including process startup;
it is a small synthetic demonstration. With one repeat per worker, observed
spread is unavailable. Use at least three separately measured repeats per worker
for empirical spread. `combine` reads 1..1000 explicitly named individual record
files, at most 64 KiB each and 8 MiB in aggregate; it performs no directory walk
and preserves existing output files. All analysis remains terminal only.

### Attach a project's reports during operation

Use the [project reporting standard](PROJECT_STANDARD.md) when creating a new project.
Its source map defines the required location and format for each Research workspace.
Keep each execution attempt in `runs/<run_id>/`.
Record the actual Slurm job ID in that attempt's `run.json`.
Append application measurements to `metrics.jsonl` and retain the attempt's log index.

Start Tower on the machine that can read those project files.
Use the CARC profile on the login node or the desktop profile for native Fedora Slurm.
Open the project with `:project /absolute/project/path`.
Use the [live project procedure](guides/live-workbench.md#connect-a-project-to-its-jobs) for job attachment, refresh behavior, and source errors.

### Inspect a job without leaving Jobs

1. Select the actual job in the active table or Recents.
2. Click Inspector, Logs, Investigate, Research, or Analytics in Details.
3. Use arrow keys and Enter to select and activate another visible button.
4. Click the content to scroll, or use `:jobpanel focus` for native panel keyboard controls.
5. Press Esc as needed to return to the job rows.

**Expected result:** Details follows the selected job and refreshes from background snapshots.
Select Off to hide inspection content.
Use `:jobpanel focus` for keyboard entry.
Use the full Logs page when you need complete-file search or copying.
Research provides all twelve project workspaces inside Details.
Analytics provides all five subviews.
Choose a subview and scroll its content vertically with the wheel or page keys.
The inline views fit the panel width without horizontal panning.

Press F8 to navigate visible controls with arrow keys.
Press Enter or Space to activate the focused control.
Press Esc to return to content navigation.
Hover highlights controls when the terminal reports pointer movement.
Direct menu choices remain available until the pointer leaves the dropdown.

Drag through visible job rows to mark a range for a supported group action.
Hold Shift to keep earlier marks.
Press Esc during capture to cancel the range.
Release the mouse button before opening cancellation review with `c`.
Check every target job ID before you confirm.

The top-right update control remains available on every page.
Press and drag its track to adjust the fetching interval.
Its labels show the effective Jobs polling interval in `s`, `ms`, or `µs`; ASCII mode uses `us`.
Use `:rate` to inspect that interval.
The logarithmic endpoints are five seconds and 500 milliseconds.
Use `:rate 50` to request 500-millisecond Jobs polling.
The command accepts positions from one to 50; position one requests five seconds.
Use `:rate reset` or right-click the update control to restore five seconds.
Source minimum intervals, timeouts, and retry backoff remain active.
Inspect Sources for the effective cadences.
See [Live workbench](guides/live-workbench.md) for the toolbar and complete panel controls.

## 5. Configure your cluster

All defaults work without a config file. Copy [config.example.json](config.example.json)
to a new personal file, preserve the `carc` profile, and add your account, partitions,
thresholds, or polling preferences. Use `--config /path/to/my-tower.json --profile carc`
to select it. Do not overwrite an existing personal configuration to follow this guide.

JSON works on every supported Python. Python 3.11+ also accepts TOML. The optional
`tower --write-config` command creates a default config only when none exists.
Tower normally reads `~/.config/tower/config.toml` or `config.json`, honoring
`XDG_CONFIG_HOME` and `TOWER_CONFIG`. State is under `~/.local/state/tower`, honoring
`XDG_STATE_HOME`. Use `--no-state` to skip state and `--no-plugins` to skip plugins.

Notification hooks and plugins execute code: load trusted files only. Demo mode
simulates Slurm, but normal config/plugin loading still applies. Setup's smoke test
uses an empty config and disables plugins and state explicitly.

Slurm accounting itself may update only every 30 seconds. Faster polling does not
guarantee fresher CPU metrics. Tune intervals to your site's guidance and use Sources
to identify slow or unavailable commands.

### Reports and automation

```bash
./scripts/tower --config /path/to/my-tower.json --profile carc --report report.txt
less report.txt
./scripts/tower --json > snapshot.json
./scripts/tower --csv --tab history > history.csv
./scripts/tower --doctor --json
```

Reports are plain ASCII snapshots for terminals, text editors, and attachments.
Exports and recordings may expose names, account usage, log output, and file paths;
review them before sharing and use `--fake` for public examples. The regular
`--report PATH` command replaces its destination. Choose a unique filename to
retain older reports. Setup's `--report` never replaces a file.

Inside the interactive application, `:export report` runs on the existing
background worker. It captures scheduler/UI state when requested, reads project
files during creation, and publishes the complete ASCII report atomically with
mode `0600`. Ctrl-A / `:activity` shows status and retains the final path; `c`
there or `:task cancel` stops between pages and bounded chunks without publishing
a partial report. Its Log page is a selected 40-line window; use `Y` in Logs for
the entire original file. Scripted `--report` still waits for completion.

For scheduled collection, use absolute paths to the venv's Python, the checkout's
`tower` directory, and your config. The interactive UI needs a terminal; use
`--once`, `--json`, or `--report` in automation. No background service is required.

### Queue departures and completion notices

If a job disappears from a successful live queue refresh, Tower retains its
last observed record in Jobs' Recents as **awaiting accounting** and requests
a bounded accounting refresh. It adds the matching record to History when Slurm
confirms a terminal state. Queue disappearance is never treated as proof of
successful completion; delayed or unavailable accounting remains visible.

The terminal shows a short block-particle departure motion and two brief History
pulses for a confirmed arrival, then retains a static unread badge until History
is opened. Set `"animations": false` in JSON (`animations = false` in TOML), or
use the `reader` theme, to keep static notices without animation. This changes
visual feedback rather than scheduler polling or accounting evidence.

### Active and historical job logs

Select an active Jobs row, a **Recents** row, or any History job and press `l`.
Logs remains attached to that exact job, including failed jobs. Press `O` for
the grouped file list; arrows/PgUp/PgDn/Home/End select an entry and Enter opens
it. Esc returns to the list, then closes the list. Lowercase `o` cycles files;
`e` switches scheduler stdout/stderr.

For multiple application/worker logs or files in other locations, follow the
[per-run log-index standard](PROJECT_STANDARD.md#log-locations-logsjson).
The copyable reporter creates `logs.json`; its `register_log` helper adds exact
relative or absolute files with stable groups and labels. Configure
`"logs": {"manifest_file": "logs.json"}` and select the concrete run directory
with `--workdir /absolute/project/runs/attempt-id`. Without a workdir override,
a relative index filename uses the selected job's actual scheduler WorkDir.
Relative entry paths resolve from the index directory, including explicit
sibling paths. Absolute entries retain their source-machine meaning when moved.
Tower lists actual stdout/stderr and bounded matching names from both output
directories alongside the index; it performs no recursive filesystem scan.

Some Slurm accounting installations do not retain historical stdout/stderr or
WorkDir. Tower keeps controller paths it actually observed, reports absent
evidence, and never substitutes a running job. Preserve failed-run files and
indexes, and bind their run directory explicitly when scheduler metadata is gone.

### Select and copy original log contents

The toolbar's Copy/Yank switch selects the destination for text-copy operations.
Copy uses clipboard transports; Yank targets an already-running local Vim or Neovim server.
Both preserve the complete private export when delivery is unavailable.
See [Editor yanking](guides/pane-navigation.md#send-text-to-vim-or-neovim) for server selection, size limits, and fallback results.

In the open log file, arrows move the line cursor before `v` begins selection.
Arrows/PgUp/PgDn/Home/End then extend it across pages; Esc cancels it. A right-edge
`›` marks the cursor, and orange `◆` marks selected lines (`>` and `*` in ASCII).
Press `y` to copy the original selected text, preserving tabs and CRLF endings
for UTF-8 logs, without Tower headers, row numbers, or wrapping.

Ranges above 256 KiB or 4,096 lines run on the shared worker, as do non-UTF-8
ranges. These copies save the pinned exact bytes as
`state/exports/log-selected-<uuid>.log` (or `./tower-exports/` with `--no-state`)
before clipboard delivery; non-UTF-8 skips text transport. Small valid UTF-8
ranges keep the immediate private `clipboard.txt` fallback.
If rotation, truncation, or tail eviction invalidates a range, `y` asks you to
reselect instead of copying the whole file. A stale mouse click also asks for
a fresh selection rather than choosing a changed screen row.

Use `V` then `y`, `Y`, or `:copy all` for the entire exact selected file. `y`
without a line selection in Logs also copies the entire file. This reads through
the selected local/SSH backend on the shared worker, independent of the retained
tail, search, scrolling, or wrapping. The private, unique export is
`~/.local/state/tower/exports/log-full-<uuid>.log`, honoring `XDG_STATE_HOME`.
With `--no-state`, use the reported file under `./tower-exports/` instead. Files
are mode `0600` and stream in 1 MiB chunks with bounded memory.

The copy captures the source's initial bytes; later appends are excluded and
reported. Detected rotation, replacement, truncation, mutation, or a short read
fails without publishing a partial file; retry the current log. An active writer
is not locked, so the exported byte range is not an application checkpoint.

Clipboard transport depends on your terminal and display. Tower sends OSC 52
only when the complete UTF-8 text fits its limit, never a shortened prefix.
The status reports a request rather than confirmed terminal acceptance. Large
or non-UTF-8 logs retain their complete raw export even if clipboard delivery is
skipped. A usable local clipboard tool may accept larger text files. Small valid
line selections keep the private `clipboard.txt` fallback; full-file copies use
the unique export so previous copies survive.

Completion and full-copy behavior have automated simulation, local-file, and
SSH-adapter coverage. Live CARC verification of these flows remains outstanding;
check the site's accounting delay and your terminal's clipboard settings when
adopting them.

## 6. Troubleshoot the specific failure

### Use the Tower 4.0 guides

Open `?` for contextual help.
Use the [Controls guide](CONTROLS.md) to find a key or command.
Use the [50-improvement index](QUALITY_OF_LIFE.md) to find a feature procedure.

| Task | Operating guide |
| --- | --- |
| Change column order, sort priority, filters, or marks | [Tables](guides/tables.md) |
| Read older content or search complete log files | [Log search](guides/log-search.md) |
| Compare logs, expand JSON, or fold repeated messages | [Log display](guides/log-view.md) |
| Change settings, bindings, command input, or navigation | [Navigation](guides/navigation.md) |
| Change chart windows, axes, ranges, events, or labels | [Charts](guides/charts.md) |
| Inspect completion notices, exports, terminals, or alert quiet periods | [Operations](guides/operations.md) |
| Page through declared text, CSV, or JSON output | [Artifacts](guides/artifacts.md) |

Start each procedure with the actual job, project run, or file selected.
Check the identity shown in the page header.
Read coverage and freshness notices before you interpret measurements.
Use the ASCII display if your terminal cannot render Unicode graphics correctly.

### Learn the Tower 3.0 workbench

Start with `tower --fake` to try navigation without scheduler access. Press `?`,
then `/` and a keyword to search the current page's help; arrows and page keys
scroll all matches. In `:`, edit anywhere with cursor keys, Tab completes supported
commands/arguments/paths, and PgUp/PgDn recalls command history. Quote paths with
spaces. Unicode input preserves actual filenames even with ASCII rendering.
`Ctrl-P` opens the searchable Research picker, `I` inspects the selected
job, and `Ctrl-B` returns to the previous location. `:activity` retains results
and export paths during the session.

For a useful recurring table, open History and enter:

```text
facet state=FAILED,TIMEOUT
columns
sortby name asc
sortby cpus desc
savedview save failures
```

Use Space in the column picker to toggle optional columns. `:savedview load failures`
restores the table, columns, facets, text filter, cascading sort, and day window.
The example orders names ascending, then CPU counts descending within each name.
You can also click each heading to cycle ascending, descending, and off; JOBID
is sortable too. Header marks such as `^1` and `v2` show direction and priority.
`:sortby cpus off` removes only the CPU rule; `:sortby clear` restores source
order. `s`, `S`, or `:sort name` resumes a single-key sort. Each of Jobs, Recents,
History, Group, My Nodes, Sources, and Cluster partitions keeps its own cascade;
use `:sortby recent id asc` to order the latest five Recents by numeric JOBID.
Cascades survive restart with ordinary state and restore with Back. Unknown
measurements remain last; Jobs pins stay first and array grouping still applies.
`:facet clear` clears field filters. `:layout split 50` and `:layout save work`
save a preferred density/split/focus arrangement separately from table views.

For a project following the reporting standard, run Tower on CARC and enter:

```text
project /absolute/project
run select my-run
workspace experiment
dashboard pin loss
chart loss
outputs
```

Select the actual run ID. Discovery reads bounded direct run inventories, not
the entire project tree; the SSH backend cannot perform this local picker.
The selected attempt binds its declared reports, log index, contract, and
verified passport. Runs without a scheduler job still expose their project
metrics, logs, and outputs. `:run clear` restores previous report settings.

Chart arrows choose samples, `+`/`-` zoom, `[`/`]` pan, and Tab changes metric
at the nearest available timestamp. `:timeline` links observed transitions,
phases, and evidence; `:diff` compares jobs or passports. In Logs, Left/Right
pans wide text, `:logpreview on` adds bounded file previews, and
`:logview split` / `:logview json` changes presentation. Esc returns to original
lines for logical selection. Failure Evidence now uses that same multi-location
catalog and reports inspected/omitted/unavailable sources.

Interactive local and SSH logs use background snapshots, including cold file
loads. Redraw and key handling consume the published buffer while a read is
pending, keeping slow CARC shared-filesystem reads and job-pane previews out of
the input loop. Scrolling avoids preference writes on every key; queued arrows
and wheel ticks share bounded redraws while preserving input order. Local log
changes are checked at most twice per second. Pending/source errors stay visible;
`r` requests a fresh observation. Noninteractive commands can wait for their result.

Each new log source and each Tower launch starts at column zero. If you pan a
wide log, the status begins with its offset and `:logpan 0` reset hint. ANSI
commands and embedded terminal controls are normalized only for display; copy
and exports preserve the original file bytes.

For batch preparation, `:preflight SCRIPT --workdir DIR` opens an editable local
form. Enter edits a field, `p` validates and refreshes the command preview, then
`s` opens explicit submission review. Validation itself does not contact Slurm.
Ordinary action review scrolls every target and starts on Cancel; Tab and Enter
choose a control, while `y` explicitly confirms.

The separate `:orchestrate workflow FILE --workdir DIR` or
`:orchestrate scaling FILE --workdir DIR` prepares a complete batch review.
Only explicit confirmation starts submissions, using actual scheduler IDs and
private durable receipts. This requires local cluster operation with state
enabled; it is unavailable for replay, SSH viewers, or `--no-state`. `c` during
execution stops further calls while accepted jobs continue. Resume/retry/recovery
require new reviews; uncertain scheduler outcomes block blind resubmission.
Read the [execution walkthrough](WORKBENCH.md#execute-a-reviewed-workflow-or-scaling-experiment)
before adopting it. No real jobs are submitted by setup or development tests;
live CARC submission/accounting still needs local verification.

The [workbench guide](WORKBENCH.md) covers every feature's bounds and complete
keys. The low-impact CARC profile remains the starting point for live monitoring.

| Symptom | Next step |
| --- | --- |
| Python is too old | Select a Python 3.10+ module/interpreter provided by the site. |
| `.venv` is not a virtual environment | Keep its contents; choose a new `--venv PATH`. |
| `No module named venv` | Select a site Python with venv support, or run `python3 -m tower --fake` directly. |
| `No module named curses` | Use a Python build with curses support, or WSL on Windows. |
| Slurm command missing | Load the site's Slurm module and rerun `--doctor` on the login node. |
| SSH check fails | Verify normal SSH, keys/MFA, host keys, network access, and remote noninteractive `PATH`. |
| TOML fails on Python 3.10 | Use JSON config or Python 3.11+. |
| Blank history or budget | Check Sources; `sacct`, `sreport`, or `sacctmgr` may be unavailable or restricted. |
| Historical logs have no path | Select the job's actual run directory with `--workdir`; configure its `logs.manifest_file` index. Accounting may not retain old paths. |
| Job stays "awaiting accounting" | Check the accounting source in Sources; queue absence alone cannot establish its terminal state. |
| Full log did not reach the clipboard | Read the reported complete `log-full-<uuid>.log` export; the terminal may disable OSC 52 or the full text may exceed its limit. |
| Log rotated during copying | Open the current file and retry; Tower does not publish an incomplete export. |
| Selected range changed or expired | Reselect after rotation, truncation, or retained-tail eviction; `y` will not substitute the whole file. |
| Log lines appear to lose their first characters | Use `:logpan 0` to restore the left edge; version 3.0.1 clears old saved offsets and resets position for each new source. |
| Scrolling stalls and keys replay in a burst | Update Tower, inspect source failures, and try `:smoothscroll off`; current wheel handling combines target movement and cached navigation avoids foreground file reads. |
| Fast pointer movement stalls or changes tabs | Install 4.9.0 or later. Use `tower --ui-trace ui-trace.json`, reproduce the issue, and quit normally. Review the phase timings and transitions in the new report. See [the latency audit](guides/ui-performance.md#tower-490-latency-audit). |
| Mouse hover or slider dragging is missing | Check reports with `:terminaltest`; inspect the terminal emulator and tmux connection, or use F8 and `:rate N`. |
| Arrows move buttons instead of document content | Press Esc or use `:focusbuttons off`; press F8 when you want directional control focus. |
| Details seem clipped or arrows move the wrong panel | Use Ctrl-W/F6 to focus Main or Details, then scroll or maximize that panel; `:density compact` restores the compact layout. |
| Project picker reports no compatible run | Use the actual project root; create direct `runs/<run_id>/run.json` inventories with matching IDs, and inspect `!` notices. Discovery is local and bounded. |
| Project sources cannot bind | Check selected-run notices and declared paths; missing reports remain missing, identity mismatches and symlink traversal are refused. |
| Evidence omits a worker log | Inspect reported coverage, preserve/register exact files in the run's `logs.json`, and open omitted sources in Logs directly. |
| Batch outcome is unknown | Preserve the receipt and verify the scheduler directly; reviewed recovery requires exact SubmitLine and WorkDir evidence, rather than retrying an uncertain submission. |
| Batch execution refuses `--no-state` | Relaunch with a writable private Tower state directory; durable receipts are required before scheduler calls. |
| Missing GPU metrics | Start with `--no-gpu`; enable sampling only for allocated GPUs where site policy permits. |
| Terminal looks broken | Check `$TERM`, widen the terminal, and try `--ascii --no-color`. |
| Blocks or braille look misaligned | Use a UTF-8 locale and a monospace terminal font with those glyphs, or use `--ascii`. |
| Setup's report looks unchanged | Existing reports are preserved; choose a new `--report` path. |
| Contributor tools cannot download | Runtime setup is offline; omit `--dev`, use a permitted package mirror, or provide development wheels. |

Network access is needed only for cloning, optional package installation, and SSH
to a real cluster. Standard pip downloads use `pypi.org` and
`files.pythonhosted.org` unless a different index is configured. Tower requires no
analytics or telemetry endpoint.

## 7. Update or remove

Pull reviewed updates with `git pull --ff-only` when your checkout has no conflicting
local edits, then rerun the same setup command. If you installed the Bash alias,
refresh its managed block and reload it as well:

```bash
git pull --ff-only
python3 scripts/setup.py --mode local
python3 scripts/install_shell.py --apply
source ~/.bashrc
tower
```

This refresh enables the current Unicode preference even if your old alias passed
`--ascii`. The source launcher sees application changes immediately. For a packaged
install, reinstall with that venv's
`python -m pip install .`. Preserve personal changes and review incoming code before
running it.

To remove Tower, remove only the checkout and virtual environment you created.
Personal configuration and state are separate; keep them for future use or remove
them intentionally. Core setup never edits shell startup files or creates services.
The optional
`scripts/install_shell.py --apply` command changes only the selected Bash startup
file after creating a backup.
