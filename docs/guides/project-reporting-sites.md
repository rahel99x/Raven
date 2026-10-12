# Project reporting on workstations and HPC clusters

[Reporting V2](../PROJECT_REPORTING_V2.md) · [Metric reference](../reference/project-reporting-metrics.md) · [File standard](../PROJECT_STANDARD.md)

The same application can report to Raven on a Linux workstation, USC CARC, and
UCI RCIC HPC3. Keep its scientific code and reporting hooks unchanged. Select the
scheduler account, resources, software environment, and storage for each site.

This guide separates those site choices from the application. Site commands and
policies below were checked against the linked official documentation on
**12 October 2026**. Access, available modules, partitions, and allocation
balances remain specific to your institution and lab. These examples are not a
claim that Raven has been qualified on either production cluster.

## Choose your environment

| Environment | Where the application runs | How reporting gets its identity | How to open Raven |
| --- | --- | --- | --- |
| Linux workstation with Slurm | In a local Slurm job | The actual `SLURM_JOB_ID`, array identity, and restart count | Use a Slurm profile with `connection.transport = local`. |
| Linux workstation without Slurm | As your ordinary local process | A unique local run ID, with no invented scheduler job ID | Use `ravenl --site desktop`; register the project and select the run. |
| USC CARC Discovery | In the compute allocation granted to your USC account | Actual Slurm job/array identity under your approved project account | Use a CARC Slurm profile and the site's permitted interface location. |
| UCI RCIC HPC3 | In a Slurm batch or interactive job | Actual Slurm job/array identity under your approved personal or lab account | Use an HPC3 Slurm profile and the site's permitted interface location. |

`--site desktop` means **standalone execution without Slurm**. A desktop or
workstation that already runs Slurm needs the first row, even if you call that
machine your desktop.

The V2 portable helper needs only Python 3.10 or later on the compute node; it
does not require installing Raven there. Copy the helper into your project using
the [main guide](../PROJECT_REPORTING_V2.md). If you choose the native
`raven report` CLI instead, its installed executable must be visible on both the
submission host and compute nodes. Follow the site's rules for installing
software. A shell alias does not make an executable available inside a batch job.

## A student's login and a lab's allocation are different

Suppose student **User A** is a member of **LabX**. They log in as their own
institutional user and request resources with LabX's approved Slurm account:

```text
User A's institutional login
  └── Slurm account approved for LabX
        └── allocation / job 12345
              └── scientific experiment solver-comparison
                    └── one recorded run attempt
```

| Identity | What it controls | What to record |
| --- | --- | --- |
| Login user | Authentication, job ownership, personal file access | The actual institutional user; students keep their own logins. |
| Slurm account | The allocation charged for the job | The exact account granted to the student, supplied with `--account`. |
| Unix group / shared directory | Access to lab files | The lab's approved group and storage path; these need not match the Slurm account. |
| Cluster | The scheduler that issued a job ID | The actual cluster identity supplied by Slurm. |
| Slurm job or array task | The scheduled work | The actual numeric job ID or `ARRAY_ID_TASK_INDEX`. |
| Experiment | The scientific comparison | A stable label such as `solver-comparison`. |
| Run attempt | One execution, retry, or restart | A distinct run directory and the exact job binding. |

`LabX` is an explanatory label, not a valid account to copy. Ask the PI or cluster
support to add your own user to the correct lab account. Lab membership does not
make you the owner of another student's job, grant access to all lab files, or
make their work an appropriate allocation for Raven to reuse.

Choose the account explicitly when working with more than one lab. The default
account can charge a personal or unrelated allocation. A CPU lab account may
also differ from the account permitted to request GPUs.

## Keep site choices in one place

Use one reviewed site file or launcher configuration per environment. The
[modular reporting example](../../examples/reporting-v2/README.md) shows how to
keep these choices outside application code. At minimum, record:

| Choice | Local Slurm | CARC Discovery | RCIC HPC3 |
| --- | --- | --- | --- |
| Login / SSH target | Your workstation | `discovery.usc.edu` with your USC NetID | `hpc3.rcic.uci.edu` with your UCInetID |
| Scheduler account | Local administrator's association, or the configured default | Your approved CARC project account, shown by `myaccount` | Your approved personal, lab, or GPU lab account |
| Partition | Your configured local partition | The approved Discovery partition; Endeavour requires its own condo partition | An approved CPU/GPU partition with the matching account |
| Shared project root | An absolute local directory | A directory inside your allocated `/project2` project space | Your `/pub/UCInetID` directory or an allocated lab DFS path |
| Software environment | Your Python/application environment | Modules available on the compute node plus your approved environment | Modules loaded inside the job plus your approved environment |
| Resource request | CPUs, memory, time, and optional GPUs that exist locally | An explicit, modest request appropriate to the experiment | An explicit request within partition limits and allocation balance |
| Retained evidence | Project run directories and backups | Preserve needed evidence outside purgeable scratch | Preserve needed evidence according to DFS/CRSP and lab retention rules |

A reporting site file is separate from Raven's own
[`raven.config/v1` configuration](../raven/README.md#inspect-effective-configuration).
The former controls the **scientific job**. Raven's `allocation` section controls
the **optional Heavy analysis worker**. Changing one does not automatically
change the other.

The recipes below use `RAVEN_REPORT_ACCOUNT` and `RAVEN_REPORT_PARTITION` as
launcher variables. Replace their `REPLACE_WITH_...` example values with the
site's actual names. They are passed explicitly to Slurm; the portable reporter
records the allocation declarations that Slurm supplies. Each invocation uses
the current project root, an existing `RAVEN_SCRIPT` source file, and its own
new run directory. Set those fields again when switching projects.

Use these read-only commands to inspect the local scheduler where permitted:

```bash
id -un
sinfo --format='%P %a %l %D'
sacctmgr -n -P show assoc where user="$USER" format=Cluster,Account,Partition,QOS
```

Some sites restrict accounting queries or report incomplete associations. Use
the site's account tool or contact support if that command is unavailable. An
empty association partition does not establish access to every partition, and
`sinfo` output does not grant an account permission to use one.

## Linux workstation with local Slurm

Use your existing local Slurm configuration. Check its available partitions and
associations before copying resource requests from a cluster example. A local
scheduler may have no lab account requirement; omit `--account` if the configured
default is appropriate.

The application remains a normal batch program. Copy the modular helper into
your project, then use the batch command below with your local partition. The
same helper works on a cluster without changes to the application. Native
`raven report submit` is an alternative when Raven is installed in the job's
environment.

Open Raven using a local Slurm profile. Validate that profile without requesting
resources:

```bash
ravenl --config /absolute/path/to/local-slurm.json doctor
ravenh --config /absolute/path/to/local-slurm.json --plan
```

The example path identifies your own reviewed configuration; create it using the
[Slurm configuration example](../examples/raven/slurm.json) and installation
guide. `ravenl` reads the scheduler. `ravenh` requests a separate Heavy allocation
unless you explicitly select a supported allocation for reuse.

For a workstation GPU, record the trace in the scientific job that owns the GPU.
A working `nvidia-smi` command does not establish that Slurm records GPU usage.
The [GPU guide](gpu-detection.md) distinguishes allocation records, accounting
counters, and time-series traces.

## Linux workstation without Slurm

After copying the [modular example](../../examples/reporting-v2/README.md), run
the application from your project root:

```bash
export RAVEN_PROJECT_ROOT="$PWD"
export RAVEN_EXPERIMENT=solver-comparison
export RAVEN_SCRIPT=src/main.py
bash .raven/reporting-v2/run.sh python3 src/main.py
```

Replace `RAVEN_SCRIPT` with an existing project-relative scientific source file
and `python3 src/main.py` with your real application command. A local run
records identity, logs, exit status, and elapsed wall time without claiming a
Slurm allocation. Application hooks can add measured metrics and outputs. The
helper reports its actual run path.

The current `raven report start` and `raven report exec` commands require a real
Slurm job identity. Do not supply a made-up `--job-id` to run them locally.

Open the standalone interface:

```bash
ravenl --site desktop
```

In Raven, register the absolute project root with `:project /absolute/project`
and select its actual run using `:run select RUN_ID`, replacing `RUN_ID` with
the generated directory name. Scheduler queue, accounting, and allocation metrics
remain unavailable; application measurements and declared evidence remain valid.

## USC CARC: Discovery

[Getting started](https://www.carc.usc.edu/user-guides/hpc-systems/discovery/getting-started-discovery)
explains account access, VPN requirements, and login. Use your own USC NetID:

```bash
ssh YOUR_USC_NETID@discovery.usc.edu
myaccount
```

`myaccount` shows your approved accounts and usage. CARC project identifiers are
often in the form `PI_username_id`; use the exact value assigned to your project.
The [allocation setup guide](https://www.carc.usc.edu/user-guides/quick-start-guides/project-and-allocation-setup)
explains how a PI establishes the project and adds its members.

### Interactive testing with the lab account

CARC documents `salloc` for interactive work. Set the next two values to the
account and partition you have verified:

```bash
RAVEN_REPORT_ACCOUNT='REPLACE_WITH_APPROVED_ACCOUNT'
RAVEN_REPORT_PARTITION='REPLACE_WITH_APPROVED_PARTITION'
salloc --account="${RAVEN_REPORT_ACCOUNT:?Set your approved Slurm account}" \
  --partition="${RAVEN_REPORT_PARTITION:?Set your approved Slurm partition}" \
  --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=4G --time=00:15:00
```

Wait for the grant. CARC's documented `salloc` setup opens a compute-node shell;
check the hostname and allocation before running the program:

```bash
hostname
printf 'Allocation: %s\n' "${SLURM_JOB_ID:?No Slurm allocation in this shell}"
```

Load your tested application environment **inside that allocation**, then change
to the project root and run the same helper:

```bash
export RAVEN_PROJECT_ROOT="$PWD"
export RAVEN_EXPERIMENT=solver-comparison
export RAVEN_SCRIPT=src/main.py
bash .raven/reporting-v2/run.sh python3 src/main.py
```

This runs one serial application in the compute shell. Replace the application
command and `RAVEN_SCRIPT` with your actual program and source file. For MPI or
other distributed work, use the site-approved application launcher. Each call
creates a fresh run directory, so several experiments can share the interactive
allocation without overwriting one another. Select the exact run in Raven when
several runs share a job ID. Exit the compute shell when finished to release the
interactive job. Keep one reporting coordinator per application invocation.

On other Slurm installations, `salloc` may grant resources while leaving the
shell on the submission host. Follow that site's documented `srun` step to
enter the allocation; the existence of `SLURM_JOB_ID` alone does not prove that
your process is on a compute node.

### Storage, modules, and GPUs

CARC's current shared project location is `/project2/PI_username_id`. Make a
project subdirectory inside the actual allocation granted to your lab. Older
examples using `/project` are obsolete: CARC documents its retirement in 2026.
`/scratch1/username` is temporary, unbacked storage with a capacity-triggered
purge policy. Preserve reports, manifests, final outputs, and relevant traces
in the lab's retained project storage; keep independent backups where required.
See [storage file systems](https://www.carc.usc.edu/user-guides/research-data-management/storage-file-systems).

Use `module avail` or `module spider` to find the site's current software, and
load the same tested versions in every batch job. See
[software modules](https://www.carc.usc.edu/user-guides/hpc-systems/software/software-modules-lmod).
Do not assume the workstation's environment exists on compute nodes.

For GPU jobs, CARC documents `--partition=gpu` with a suitable
`--gpus-per-task` request. GPU types, constraints, and availability are site
specific; use the current
[GPU programming guide](https://www.carc.usc.edu/user-guides/advanced-hpc-programming/gpu-programming).
Select the actual allocated device when enabling a trace. A workstation example
that hard-codes GPU index `0` is not a portable shared-cluster selection rule.

Endeavour uses a different login target and approved condo partition/account
combination. Follow its
[getting-started guide](https://www.carc.usc.edu/user-guides/hpc-systems/endeavour/getting-started-endeavour)
instead of assuming the Discovery choices apply.

## UCI RCIC: HPC3

Use your own UCInetID with the access method documented in
[logging in](https://rcic.uci.edu/account/login.html):

```bash
ssh YOUR_UCINETID@hpc3.rcic.uci.edu
sbank balance statement -u "$USER"
```

UCI distinguishes personal Slurm accounts, lab CPU accounts, and GPU-enabled lab
accounts. The professor requests a lab account and membership for their students.
Use the exact account supplied by RCIC; the lab's informal name and your Unix
group are not substitutes. See
[Slurm accounting](https://rcic.uci.edu/slurm/slurm.html#slurm-accounting).

For a lab account, inspect its balance with:

```bash
RAVEN_REPORT_ACCOUNT='REPLACE_WITH_APPROVED_LAB_ACCOUNT'
sbank balance statement -a "${RAVEN_REPORT_ACCOUNT:?Set your approved Slurm account}"
```

The balance is shared across lab members. A job can remain pending because the
lab's running and queued requests would exhaust the allocation, even when your
own use is small. Check the scheduler reason and the lab's allocation with your
PI; reporting does not bypass these limits.

### Interactive testing with the lab account

HPC3 documents `srun --pty` for interactive sessions. Its interactive jobs use a
single node. After confirming your account and allowed CPU partition, request a
short session from the login node:

```bash
RAVEN_REPORT_ACCOUNT='REPLACE_WITH_APPROVED_LAB_ACCOUNT'
RAVEN_REPORT_PARTITION='REPLACE_WITH_APPROVED_CPU_PARTITION'
srun --account="${RAVEN_REPORT_ACCOUNT:?Set your approved Slurm account}" \
  --partition="${RAVEN_REPORT_PARTITION:?Set your approved Slurm partition}" \
  --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=4G --time=00:15:00 \
  --pty /bin/bash -i
```

Wait for the compute-node prompt, then load modules, enter the project, and run:

```bash
export RAVEN_PROJECT_ROOT="$PWD"
export RAVEN_EXPERIMENT=solver-comparison
export RAVEN_SCRIPT=src/main.py
bash .raven/reporting-v2/run.sh python3 src/main.py
```

Replace the application command and `RAVEN_SCRIPT` with your real program and
source file. Each invocation gets a separate run directory; select the exact run
in Raven when reusing the interactive allocation. Type `exit` when finished. The
[job guide](https://rcic.uci.edu/slurm/jobs.html#interactive-job) provides the
current interactive syntax and additional examples. Use `sbatch` for unattended
or long-running work.

### Storage, preemption, and GPU accounts

HPC3's acceptable-use policy says to run jobs in `/pub/UCInetID`, or the lab's
approved DFS location, rather than the home directory. Obtain the exact shared
lab path from the PI. Do not construct a path by guessing the Slurm account name.
DFS is not a promise of durable backup; plan retention with the lab and consult
[DFS storage](https://rcic.uci.edu/storage/dfs.html) and
[CRSP storage](https://rcic.uci.edu/storage/crsp.html).

The `free` and `free-gpu*` partitions provide preemptible resources. They can be
appropriate for restartable tests, but jobs can be killed when allocated jobs
need those resources. Record failed/interrupted attempts, preserve checkpoints,
and create a distinct report for a restart. A successfully written partial
metric file does not establish successful scientific completion.

The allocated `gpu` and `gpu32` partitions require their corresponding approved
GPU lab accounts. A CPU lab account is insufficient. The current RCIC guide
specifies matching account suffixes and GPU-type rules; confirm them in
[GPU-enabled partitions](https://rcic.uci.edu/slurm/slurm.html#gpu-enabled)
before selecting resources. Avoid hard-coded GPU indices when recording traces.

HPC3 asks users to load modules in the batch script or after entering an
interactive node, not to depend on modules already loaded in the login shell.
See [software modules](https://rcic.uci.edu/software/modules.html).

## Batch reporting and allocation boundaries

On either cluster or a local Slurm workstation, use the portable batch script
and pass the site choices as Slurm options:

```bash
export RAVEN_PROJECT_ROOT="$PWD"
export RAVEN_EXPERIMENT=solver-comparison
export RAVEN_SCRIPT=src/main.py
sbatch --account="${RAVEN_REPORT_ACCOUNT:?Set your approved Slurm account}" \
  --partition="${RAVEN_REPORT_PARTITION:?Set your approved Slurm partition}" \
  --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=4G --time=00:15:00 \
  .raven/reporting-v2/batch.sh python3 src/main.py
```

Run this from the project root after copying the helper, setting the verified
account/partition values, and adapting the software environment in `batch.sh`.
Replace `python3 src/main.py` and `RAVEN_SCRIPT` with the actual command and
scientific source file. Submit from the project root, or set `sbatch --chdir`
to that root and pass the batch script's absolute path. The wrapper preserves
Slurm's actual WorkDir and runs the application there; `SLURM_SUBMIT_DIR` can
refer to a different submission directory. On a local scheduler with an appropriate default account, omit the
account option. The batch wrapper keeps one reporting coordinator and launches
the application through `srun`.

For a native Raven installation in the job environment, the alternative is:

```bash
raven report submit --root "$PWD" --experiment solver-comparison \
  --script jobs/run.sbatch -- \
  --account="${RAVEN_REPORT_ACCOUNT:?Set your approved Slurm account}" \
  --partition="${RAVEN_REPORT_PARTITION:?Set your approved Slurm partition}" \
  --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=4G --time=00:15:00
```

This uses a batch script adapted for `raven report exec` or `start`/`finish`, as
shown in the [automation guide](research-automation.md#existing-batch-scripts-and-launchers).
Use one lifecycle wrapper per attempt. Keep the project's files visible to both
the compute node and Raven's selected file backend. Restricted Slurm export
policies need explicit environment setup; the native CLI's required handshake
variables are documented in the automation guide.

Raven Heavy is a separate consumer of compute resources. Starting `ravenh` from
inside a scientific allocation does not automatically attach it to that job;
the default behavior requests its own allocation, potentially charging the
configured account again. Use a reviewed CPU worker profile and inspect
`ravenh --plan`. Explicit `--allocation` reuse is limited to verified, supported
allocations; the generic adapter rejects GPU, ambiguous array/heterogeneous,
and unknown per-node grants. It does not force an overlapping step into a busy
job. Reporting itself does not require starting Raven Heavy.

## Place the interface and retain the evidence

CARC prohibits heavy work on login nodes. HPC3's
[acceptable-use policy](https://rcic.uci.edu/account/acceptable-use.html)
restricts login nodes to lightweight work, prohibits computation and servers,
and prohibits long-running jobs there. Raven Light's bounded collection does
not establish site approval for an unattended login-node session. Follow the
site's policy for where and how long to run the interface; keep computational
analysis and the scientific program in approved allocations.

Where permitted, the interface can run on your workstation using Raven's
explicit SSH connection to the scheduler host. Its queries and remote file
reads still consume site resources and remain subject to site policy. The
remote reader needs Python 3.10 or later; it reads declared files and does not
run the project's program. Configure the permitted SSH alias and authentication
using the [Raven setup guide](../raven/README.md).

Keep the run manifest, metric stream, final summary, log index, declared logs,
outputs, and any GPU/profiler recordings together when archiving an experiment.
A scratch purge removes evidence that a later Research or Analytics view needs.
For comparisons across sites, keep cluster, hardware, environment, workload,
and input identity explicit; matching job numbers or experiment labels alone
do not establish comparable measurements.

Before scaling up, run one small job in your own approved allocation. Confirm
its actual job ID, account, attempt, project path, application metrics, and
terminal result in Raven. Check optional GPU or profiler evidence only when
that module is enabled. The [metric reference](../reference/project-reporting-metrics.md)
explains which missing fields indicate absent measurements and which require
an additional producer.
