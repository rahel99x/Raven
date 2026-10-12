# Terminal workflows

[README](../../README.md) · [Allocation operations](operations-cluster.md) · [Project standard](../PROJECT_STANDARD.md)

Use Raven's command palette to open terminal sessions, review allocation targets, and prepare commands.
Each action uses an explicit destination. Selecting another job does not change an existing shell's host or directory.
Commands entered in an embedded terminal have the same authority as commands entered in a separate terminal window.

**Prerequisites:** Install the optional terminal engine described in the [terminal guide](terminal-fusion.md).
SSH, tmux, and container actions require their corresponding executables on the machine that runs Raven.

## Choose a destination

| Command | Result |
| --- | --- |
| `:terminal new` | Start a local shell on the machine that runs Raven. |
| `:terminal cwd "/path/with spaces"` | Start a new local shell in an accessible directory. Use the configured terminal shell. |
| `:terminal job-cwd 1234` | Start a local shell in that job's declared working directory. Refuse remote or ambiguous paths. |
| `:terminal ssh cluster-alias` | Start SSH with your normal host alias, SSH configuration, host verification, and interactive authentication. |
| `:terminal ssh-many node-a node-b` | Open a collection of up to four explicit SSH destinations. Validate all destinations before starting the first session. |
| `:terminal tmux analysis` | Create or attach to `analysis` on the dedicated local `tower-fusion` tmux server. |
| `:terminal container podman analysis /bin/bash` | Open an interactive shell in an existing container. |
| `:terminal container apptainer /project/image.sif /bin/sh` | Open a shell in an explicit image. Host and image paths are separate. |

`docker` and `singularity` are also accepted container adapters. The selected executable must be installed.
The Docker and Podman actions use `exec`, which requires an existing running container. They do not create or start containers.
The Apptainer and Singularity actions use `exec` with the explicit image.
These actions do not infer bind mounts or a remote host.

A Slurm `--host` setting is a data connection. It does not change the host of a local terminal.
An SSH session does not automatically reconnect or replay commands after a connection failure.
Inspect the child terminal's authentication messages and prompt before sending input. A running SSH process alone does not prove that authentication completed.

The tmux action uses a separate server socket so it does not attach to unrelated default-server sessions.
Use **Ctrl-B**, then **D** to detach with tmux's default key map. If your tmux configuration changes that map, use its detach command.
Closing an embedded tmux client leaves its tmux session running. A normal local shell does not have this persistence guarantee.
To attach outside Raven, use `tmux -L tower-fusion attach-session -t analysis`.

## Open a reviewed allocation terminal

Use `:terminal job 1234` to inspect and review a shell in one running allocation that you own.
Raven opens the existing Operations review. Read the review, select **Review**, and then select **Apply**.

| Command | Target |
| --- | --- |
| `:terminal job 1234` | One new shell step in this allocation; the scheduler selects its node. |
| `:terminal job 1234 node=compute-2` | One new shell step on an exact node in this allocation. |
| `:terminal job 1234_7` | The exact array task. |
| `:terminal job 1234+1` | The exact heterogeneous component. |
| `:terminal job 1234 step=0` | Attach to the existing running numeric step `1234.0`. |
| `:terminal job 1234 step=0 task=2` | Attach input and output to exact task rank 2 in that step. |
| `:terminal job 1234 display=fullscreen` | Use the original full-screen terminal handoff instead of the embedded pane. |

The default display is `embedded`. You can also set `shell=/absolute/path` for a new shell step.
The same options are available in the **Allocation shell or attach** Operations form.

Raven checks ownership, submission identity, running attempt, and step or node membership during preparation and immediately before launch.
The review expires if the job is requeued, changes owner, leaves the running state, or its exact target becomes unavailable.
An embedded launch is also refused if the completion callback is delayed for more than two seconds or the Operations view or connection changes.

A new shell uses `srun --overlap --exact`, one node, one task, one CPU, and a bounded immediate-start request.
It shares the existing allocation. It does not run `salloc` or submit another job.
An attachment uses `sattach`. It can send input to an existing application. Read the target and effect before applying it.
Task filtering requires the scheduler to expose a valid count for the exact numeric step. Raven refuses unknown ranks.
The `batch` and `extern` steps are not interactive attachment targets.

For these reviewed Slurm actions, run Raven on the target cluster host. Raven does not reinterpret a remote Slurm data connection as permission to start a shell on the local workstation.
An allocation session retains its original attempt identity. Job completion or ID reuse does not reassign it to a new attempt.

## Prepare commands without executing them

Use `:terminal prepare NAME` to display an insertion preview. Confirm that the selected terminal is at its shell prompt.
Select **Insert** to insert the command without Enter. Inspect or edit the command in the shell, then press Enter when ready.
Select **Cancel** to discard the preview.

| Diagnostic | Prepared command |
| --- | --- |
| `gpu` | One `nvidia-smi` utilization and memory report. |
| `amd-gpu` | One `rocm-smi` utilization and memory report. |
| `environment` | `uname -srmo`; this does not dump environment variables or credentials. |
| `python` | `python --version`. |
| `allocation 1234` | `scontrol show job -o 1234`. |
| `trace /project/trace.json` | `tower trace inspect /project/trace.json`. |

Prepared insertion requires a known shell profile. It is refused for unknown shells, SSH prompts without shell metadata, tmux sessions with unknown foreground programs, and application attachments.
A known launch profile does not prove that the shell is currently at a prompt. For example, you can start Python inside Bash. The preview requires you to check the current foreground program.

To prepare a short sequence, use:

```text
:terminal sequence start environment python gpu
:terminal sequence next
:terminal sequence next
:terminal sequence next
:terminal sequence cancel
```

A sequence has at most eight steps. Each **next** action creates one command preview. No command runs automatically.
Read the previous output before advancing. If the active terminal changes, the next step is refused and the sequence is cancelled.
Cancelling a sequence clears its preview. It cannot retract input that was already delivered or undo a command that you executed.
Use a separate trace preview when a trace manifest path is required.

## Save parameterized recipes

Recipes store a command template and a shell profile. A parameter is one complete argument.
Do not surround a parameter with template quotes or join it to another token. Raven quotes the parameter for the selected shell.

```text
:terminal recipe save read-output "cat {path}" shell=bash
:terminal recipe use read-output "path=/project/run 1/output.log"
:terminal recipe show read-output
:terminal recipe list
:terminal recipe export /project/terminal-recipes.json
```

The use action opens the same insertion preview. It does not execute the recipe.
The active terminal's known shell must match the recipe shell.
Bash, Zsh, Sh, Dash, Ksh, and Fish quoting are supported. Fish uses its own single-quote and backslash rules.
Parameterized recipes refuse nested shell substitutions, formatted placeholders, attribute access, and ambiguous quoting.

Recipes remain in the current Raven session until you export them.
Export creates a new private file and refuses to replace an existing path.
Use `:terminal recipe import PATH` to read a `tower.terminal-recipes/v1` regular JSON file.
Imports are bounded and atomic: an invalid record, duplicate name, conflicting existing name, or limit violation leaves the existing recipe collection unchanged.
Interactive file operations use a background worker and a short-lived helper process with a five-second deadline. They do not wait for project filesystem reads on the UI thread.
If an export times out, inspect its destination before retrying. Raven does not replay an uncertain file write.
An import is refused if the in-memory recipes change while the file operation is pending.
There are at most 32 recipes and 16 parameters per recipe. Import never executes a command.
Use `:terminal recipe delete NAME` before importing a replacement.

## Review insertion into several shells

Ordinary typing always has one recipient. For one explicit multi-session insertion, use:

```text
:terminal broadcast prepare "hostname" SESSION_ID_1 SESSION_ID_2
```

Use exact session IDs from the session information. Choose two to four distinct, running sessions with known shell profiles.
The preview lists every destination. Check that every target is at its shell prompt, then select **Insert**.
Raven queues the text without Enter and disarms the broadcast. Enter each shell separately to execute or edit its prepared line.
Changing focus, page, or active session cancels the armed preview. `:terminal broadcast cancel` also discards it.

Before sending any text, Raven rechecks every recipient's session identity, metadata, and running status.
If queueing fails partway through, Raven reports the sessions whose input was already queued. It never retries or replays a partial broadcast.
Inspect each destination before preparing another insertion. Already queued bytes cannot be recalled.

## Validation scope

The automated tests cover launch argument boundaries, exact allocation reviews, node and rank changes, stale callbacks, shell quoting, recipe imports, session changes, and partial broadcast receipts.
Local subprocess tests execute available Bash, Zsh, and Sh profiles. Tests for absent optional shells are marked as skipped.
Live SSH authentication, site-specific Slurm step policy, container installations, and tmux configuration need validation on the host where you use them.
