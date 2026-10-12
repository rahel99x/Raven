# Set up Raven from the toolbar

Use **Setup** in the top toolbar to configure Heavy allocation preferences and
find the data required by each Research and Analytics view. These pages are
included in the installed application; no browser or repository checkout is
needed to read them.

Open **Setup → Setup overview**, or enter `:setup`. Press F10 to reach the
toolbar without a mouse. Use Left/Right to change menus, Up/Down to choose an
entry, and Enter to open it. Setup pages support clickable controls, arrow-key
navigation, and scrolling. Esc returns from an account picker or closes the
page; while editing a field, Esc cancels that edit.

## Choose what to configure

| Setup entry | Purpose |
| --- | --- |
| Heavy allocation settings | Discover the connected user's Slurm associations and save account, partition, QoS, CPU, memory, GPU, and time preferences. |
| Project connection and reporting | Connect a project and select an exact reported run; inspect its files before attaching them. |
| Research: each named view | Read its prerequisites, exact source conventions, and available attachment controls. |
| Analytics: each named view | Identify its scheduler or retained-series inputs, measured units, and missing-data checks. |
| CPU, memory, and GPU telemetry | Distinguish job-produced GPU traces, native CPU/memory accounting, application metrics, and collection policy. |
| Installation, terminal, and diagnostics | Find installation and connection checks; open preferences for terminal and collection settings. |

Opening a guide reads embedded instructions. Opening allocation settings starts
a bounded account-discovery task on the existing connection. Rendering,
hovering, and editing do not query Slurm. Discovery retains source admission,
timeouts, and shared caching; a busy or restricted scheduler can leave a field
unavailable.

## Configure a student's lab allocation

Suppose a student has a personal account and an association with a professor's
lab account. The student remains the authenticated user and owner of the job.
The allocation's `--account` option selects the account charged by Slurm.

1. Start `ravenl` with the intended local or cluster profile.
2. Open **Setup → Heavy allocation settings**.
3. Check the detected login, cluster, and any discovery notices.
4. Open **Choose personal / lab association** and select the actual assigned
   account. The list shows the available cluster, partition, and QoS context.
5. Set the partition and QoS required by that association. Set the CPU count,
   memory in **MiB**, GPU count, and time limit in **seconds**.
6. Select **Save settings**. Review any validation error or site-policy limit.
7. Select **Review Heavy allocation request** to inspect the effective request.
   Requesting resources uses Raven's existing separate allocation review.

**Expected result:** The saved profile supplies the next Heavy request, including
future launches of `ravenh` with that profile. Saving alone submits no job and
starts no worker. Changes cannot modify a running allocation; use the compute
controls to return to Light before saving new allocation preferences.

Slurm supplies association names, not a reliable personal-versus-lab label.
Raven therefore lists the actual associations rather than inferring a lab from
the user's name. An association is not a guarantee of a positive balance,
partition access, or permission to inspect another student's files.

The current Heavy adapter uses one node and one task. Its resource limits and
site restrictions remain effective. Heavy settings configure Raven's analysis
worker; Account and Budget monitoring retain their current account until Raven
restarts. These settings do not rewrite the application's batch scripts or charge an
existing scientific job to another account.

## Use local Slurm or a scheduler default

For a workstation running Slurm, keep the Slurm profile and choose
**Use Slurm's default account** when an explicit billing account is unnecessary.
This leaves `allocation.account` empty and omits the account flag from the
request. A local scheduler can grant resources without an allocation balance;
its configured partition, memory, CPU, and time limits still apply.

An empty account does not mean the login name is a Slurm account. On a cluster,
check which default account will be charged before using this option. A site
policy that requires an explicit account can reject it.

A standalone workstation launched with `--site desktop` has no Slurm account
or allocation. It uses the explicit local-worker policy. This is a separate
environment from a desktop that runs a local Slurm scheduler.

## Handle unavailable discovery or locked settings

| Notice or situation | Action |
| --- | --- |
| No visible associations | Use the scheduler default if appropriate, or enter the exact account assigned by the administrator. |
| SSH identity unavailable | Check the selected connection and SSH access. The local login name does not establish the remote user's identity. |
| Discovery deferred or denied | Read the notice, retain current settings, and retry after the source recovers; manual entry remains available. |
| Several clusters or partition-specific associations | Use the association for the connected cluster and verify its partition and QoS. A shared account name does not make associations interchangeable. |
| A command-line override controls a field | Relaunch without that override before changing the saved preference. |
| Configuration changed outside Raven | Relaunch Raven to load the current file, then review allocation settings before saving. |
| Heavy is active or a request is pending | Use compute controls to release or cancel it, then return to the settings page. |
| Demo or replay session | Inspect the guides; use a real configured session to discover accounts or save allocation settings. |

Saving updates the selected profile and preserves other configuration values.
A save warning about directory synchronization means the new file is visible and
the active settings were updated, but the filesystem could not confirm persistence
across a crash.
JSON and TOML are supported; saving TOML rewrites its comments and formatting.
An older unversioned configuration requires the existing explicit migration
before this editor can save it. The editor does not change authentication,
Slurm services, site policy, or another user's configuration.

## Set up a Research or Analytics view

Choose its name directly from **Setup**. For example:

```text
:setup research experiment
:setup research scaling
:setup analytics job
:setup analytics compare
```

Each page states what produces its inputs and provides the relevant controls
for opening the view, connecting a project, attaching a source, or inspecting
source health. A file of arbitrary application metrics cannot populate native
CPU or GPU curves. Completed-job curves require measurements recorded during
execution; a final total or peak cannot reconstruct a missing time series.

For complete application hooks and file examples, use
[Project reporting V2](../PROJECT_REPORTING_V2.md), the
[format cookbook](../reference/project-reporting-formats.md), and the
[metric reference](../reference/project-reporting-metrics.md).
