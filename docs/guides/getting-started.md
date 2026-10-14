# From installation to your first job

[README](../../README.md) · [Installation options](../INSTALLATION.md) · [Setup reference](setup.md) · [Launch and reporting reference](automated-reporting-exports.md)

Start with an existing Bash or POSIX-shell submission script. Raven can submit
it, retain its execution details, and collect supported measurements without
requiring an application rewrite. You choose the execution environment, billing
account, and recording options before anything runs.

## 1. Install the commands

Use Python 3.10 or later with the `venv` and curses modules. From a new checkout:

```bash
git clone https://github.com/rahel99x/Raven.git
cd Raven
python3 scripts/install.py
```

The installer uses a private application environment owned by your user. It
does not require pipx, administrator access, or shell aliases. Read its final
command locations and PATH instructions before continuing. If you already use
pipx, keep that installation and follow the [update instructions](../INSTALLATION.md#update-the-installed-package)
instead of installing a second copy.

## 2. Choose where you work

The installer opens guided setup automatically in an interactive terminal.
If you skipped it or want to return later, run:

```bash
raven setup
```

Inside Raven, the same workflow is available through **Setup → Getting
started**. Use **Setup → Sites and lab profiles** to return to saved connection
and account choices.

A first bare interactive launch of `raven`, `ravenl`, or `ravenh` without an
existing configuration opens setup. Even `ravenh` stays in Light during this
first-run flow; saving a profile does not authorize an allocation. After setup,
configured `ravenh` launches retain their usual Heavy request behavior.

| Environment | Select when | Where to launch a scientific job |
| --- | --- | --- |
| Local Slurm commands | Slurm is installed on your workstation, or you are already on a cluster submission host. | In Raven on that host; the script is submitted through its Slurm commands. |
| Cluster over SSH | Raven runs on another machine and monitors a cluster through SSH. | Open Raven separately on the approved submission host with access to the project and Slurm commands. |
| Standalone workstation | You explicitly want local execution without Slurm. | On your workstation, using the explicit local launch mode. |

A workstation with Slurm is a Slurm environment. The existing CLI option
`--site desktop` means standalone execution **without Slurm**; it is not the
correct shortcut for every desktop machine.

An SSH monitoring connection does not submit a project from your laptop to the
remote cluster. Open a terminal on the permitted submission host, install Raven
there, and open the project there to use the guided launcher. Keep the script,
runtime, and retained output on storage visible to the execution nodes. Cluster
module loads and application-environment activation remain in your script.

Setup checks local command availability and presents settings for review. These
checks do not query a scheduler or verify compute-node software. Saving
configuration does not submit a scientific job or request Heavy compute.

To explore simulated jobs first, choose **Try Raven with simulated jobs → Open
offline demo** in Getting started, or run `raven setup --demo`. The demo uses no
scheduler connection or saved application state. In interactive setup, `:draft` saves
progress without applying configuration; `raven setup --resume` reopens it.

## 3. Save an account and resource profile

Choose a descriptive profile name, such as “LabX CPU experiments.” Raven derives
a stable identifier, such as `labx-cpu-experiments`, for command-line selection.
Use your own login and an account actually
assigned to it. Raven can discover visible Slurm associations; a restricted
cluster may require entering the exact account manually.

Keep these identities distinct:

| Setting | Meaning |
| --- | --- |
| Login user | The person authenticated to the machine or cluster and owning the submitted job. |
| Workload account | The Slurm account charged for the scientific job. |
| Heavy account | The Slurm account charged for Raven's optional analysis worker. |
| Project launch defaults | Reusable choices for scripts launched from that project. |

For a student in a professor's lab, the login remains the student's username
while the workload account can be the lab allocation. Slurm association names
do not reliably identify “personal” versus “lab”; select the association granted
by the institution. Membership alone does not establish a remaining balance or
permission to use every partition.

On local Slurm, leave the account unspecified when the scheduler's default is
appropriate. This does not create an account or remove scheduler limits. On a
cluster, check which account the default charges.

Review CPU, memory, time, partition, and optional GPU requests. Guided resource
fields accept units such as `8 GiB` and `1h 30m`; the review shows their normalized
values. Heavy preferences
apply to Raven's worker; workload preferences apply to the submitted script.
One does not silently replace the other. Saved site profiles can be selected
with `ravenl --site PROFILE_NAME`. Saving alone leaves the current connection
unchanged. In the dashboard, select **Use this profile** after saving to close
the old Light session and open Light on the reviewed profile. Then choose
**File → Launch job**. An existing saved profile offers **Use saved profile
(without saving edits)**. Switching is blocked while Heavy is active or pending,
or a launch action is unresolved. Command-line setup prints the command to open
the saved profile instead.

## 4. Review and launch your script

Open Raven, choose **File → Launch job**, or enter `:launchjob`. The Launch
toolbar button opens the same workflow when there is enough screen space.

1. Choose the project and existing script.
2. Review the execution mode, workload account, and resource requests. Raven
   reads supported `#SBATCH` directives and shows effective values and their
   sources.
3. Choose recording options. CPU/memory observations, GPU recording, and
   application measurements have separate requirements.
4. Review the final request and equivalent command.
5. Submit explicitly from the review page.

The project folder does not need an existing Raven manifest. Script suggestions
come from the selected folder; enter a relative path for scripts in a
subdirectory. Application arguments accept shell-style quoting, but no variable
expansion, wildcards, or command substitution.

Blank resource fields inherit the script, scheduler environment, and saved
defaults. Values entered in the form override those sources for this launch.
**Save these resource defaults for this project** stores a named workload
profile; when reused as defaults, it fills missing settings and preserves
explicit script and environment choices. Select the saved profile on a later
launch, or use `--recipe NAME` with `raven launch`.

Reading a script and preparing a review do not execute it. The original script
is preserved; the launcher retains the submitted snapshot and exact attempt.
Save reusable choices separately when you want them to apply to later launches.

For the equivalent command-line workflow, run these commands from the directory
where you normally submit the script, replacing `batch.sh` with its path:

```bash
raven launch batch.sh --research experiment --check
```

Inspect the preview, then submit:

```bash
raven launch batch.sh --research experiment
```

For standalone execution on your own workstation, add `--local` to both
commands. Do not use it to run scientific workloads on a cluster login node.
For a workload account granted to your user, add `--account ACCOUNT_NAME`.

A preview validates the request and known sources. It cannot guarantee scheduler
admission, a compute-node Python environment, GPU counters, or measurements that
the application has not produced. Missing Slurm commands never cause an
automatic switch to standalone execution.

## 5. Inspect the run and add measurements

After submission, **Open recorded run** selects that exact attempt. If its
files are still initializing, retry that action after startup. Use Jobs to
inspect queue state and logs, then select the exact recorded run to
inspect its retained evidence. A recording is separate from a live scheduler
query: retained CPU/memory can be available even when GPU measurements are not.

Lifecycle and execution identity need no application changes. Scientific values
such as loss, accuracy, residual, and throughput need real application output.
Use **Setup → Project launch and recording defaults** to bind supported files
and save named launch recipes. The [reporting guide](automated-reporting-exports.md#4-add-scientific-measurements-when-needed)
provides copyable helper calls and exact JSONL examples; the
[metric catalog](../reference/project-reporting-metrics.md) lists the sources
required by every Research and Analytics view.

Heavy is optional for submitting a job. If you need its supported analysis
capabilities, review `ravenh --plan` or the dashboard's compute controls before
requesting it. The scientific job and Raven's Heavy allocation have independent
lifecycles and billing settings.

Closing Raven does not cancel an accepted workload. For a standalone launch
from the dashboard, the receipt identifies the local supervisor and retained
stdout/stderr files; use its displayed stop instructions when needed.

## Return to a working configuration

| Situation | Next step |
| --- | --- |
| `raven` is not found | Use the executable path printed by the installer and check its directory against PATH. |
| An older Raven command runs | Check `command -v raven`, `command -v ravenl`, and `ravenl --version`; update the installation that owns those commands. |
| The account list is empty | Check the selected host and login; use a verified account or the scheduler default where appropriate. |
| A script cannot be previewed | Read the exact error and confirm its supported shell, project path, directives, and interpreter. |
| Submission has an uncertain outcome | Inspect the retained launch record and scheduler before submitting again. |
| Metrics are missing | Inspect recording readiness and actual file/read errors; confirm the application produces the documented format. |

Use `ravenl doctor` for installation checks and `ravenl config explain` for the
effective Raven configuration. The [installation guide](../INSTALLATION.md)
covers package updates, existing pipx installations, and offline deployment.
