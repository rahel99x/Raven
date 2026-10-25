# Engineering workflows

Raven connects scientific experiments to their actual execution attempts and
retained evidence. Existing Slurm monitoring, launch, tracing and Heavy behavior
remain available. The native adapters add explicit inspection and reviewed
actions for Slurm, Kubernetes, IBM Spectrum LSF, OpenPBS, PBS Professional,
HTCondor and Flux. Availability of an adapter is separate from qualification on
your site.

## Find a task

Open **File → Engineering workflows**, or type `:workbench engineering` inside
Raven. The shared workbench supports the mouse, keyboard, scrolling, pinned
tools, background inspection, cancellation and a separate action review. Run
Home includes contextual links for experiment lineage and execution packages.
Opening a form or reading help submits no work.

From a shell, discover the same service boundaries:

```bash
raven engineering catalog
raven scheduler catalog
raven engineering --help
raven scheduler --help
```

`raven`, `ravenl` and `ravenh` expose these commands. Native scheduler commands
bind an explicit connection rather than borrowing a different dashboard's
profile. Help, catalogs and local state inspection never request Heavy. Retained
trace analysis and trial statistics in a managed session require verified Heavy
compute; a missing worker produces a deferred result, not local fallback.
Heavy currently uses the established Slurm or explicit standalone allocation
path. Other schedulers need an ownership/placement adapter before Heavy can run
through them.

## Connect a native scheduler

Use the scheduler's own authenticated command-line client on the current host,
or an existing SSH alias with `--host`. Raven never discovers executable Python
plugins from a project. Native reads have timeout, byte, record, concurrency and
cache bounds. A read result includes the scope, supported operations and actual
read statistics. Rendering the result makes no further scheduler requests.

```bash
raven scheduler capabilities --scheduler slurm
raven scheduler jobs --scheduler slurm
raven scheduler history --scheduler slurm
```

For another adapter, replace `slurm` with `kubernetes`, `lsf`, `openpbs`, `pbspro`,
`htcondor` or `flux`. Supply the connection scope required by its capabilities.
Kubernetes requires an explicit context and namespace; mutations also pin the
API server. Flux requires a concrete broker URI. Read
[native scheduler contracts](native-schedulers.md) before applying changes.

Submission specifications preserve native meaning: Kubernetes consumes a
`batch/v1` Job manifest, HTCondor consumes its submission description, and batch
shell adapters consume an existing absolute script path with supported explicit
resource fields. Slurm cores/threads, Kubernetes CPU requests and limits, and
LSF slots are not synonymous.

Review a submission before applying its saved plan:

```bash
raven scheduler review submit --scheduler slurm --cluster YOUR_CLUSTER \
  --submission submission.json --output review.json
```

Inspect the printed account, resources, native arguments, script digest, scope,
expiry and review digest. Then run `raven scheduler apply --scheduler slurm
--plan review.json --review-digest DIGEST`, using that exact printed digest.
Use the same explicit cluster and other scope fields at apply; `YOUR_CLUSTER`
means the actual Slurm `ClusterName` reported by your site, not an arbitrary
profile label. Apply checks the script and connection again. Existing-job actions use an exact
identity returned by queue/history, not a guessed job number. A consumed token
cannot be replayed. An unknown outcome requires native-state reconciliation;
do not interpret it as proof that submission failed.

## Follow an experiment

An experiment is a stable scientific identity; each run remains a distinct
attempt. Create an index in an existing project, link a real retained run and
read the resource summary:

```bash
raven engineering experiments create --project /path/to/project \
  --id study-1 --title 'Resource-layout study'
raven engineering experiments link --project /path/to/project \
  --id study-1 --run EXISTING_RUN_ID
raven engineering experiments summarize --project /path/to/project --id study-1
```

Use a request file to declare retry/checkpoint ancestry, external exact native
identities, or measured accounting. Failed attempts count too. Allocation
summaries and step measurements remain separate to avoid charging both.
Missing counters remain unknown. Existing inventories are verified before a
Raven run is linked; an external scheduler observation cannot manufacture one.
See [experiment lineage and execution packages](experiments-and-capsules.md).

## Capture reproducibility before rerunning

```bash
raven engineering capsules capture --project /path/to/project \
  --run EXISTING_RUN_ID --destination /path/to/project/package-1
```

Packages retain the verified script and explicit declarations, not your ambient
environment or datasets. Declare modules, seeds, non-secret environment values
and versioned input references through a request. Capturing the current Git
checkout does not prove that a past job ran that revision. Local patch contents
are opt-in. Verify or compare packages before requesting `relaunch_draft`; the
result opens the ordinary launch review and explains changed or unknown inputs.

## Qualify sites and share lab defaults

Save a lab workspace with separate login, discovered/declared account
associations, job defaults and Heavy defaults. Saving makes no connection.
`inventory_from_workspace` explicitly reads only selected sites, with a site
count, record and elapsed-time budget. An account association is descriptive;
the scheduler still decides whether a submission is authorized.

`qualification prepare` creates a private probe script and a native submission
review. Applying that native review is a separate action. The probe checks the
actual compute interpreter/imports, scratch filesystem, process counters and
selected recording/worker features; `collect` binds results to the exact native
attempt and nonce. A login-node presence check is not compute qualification.
Unsupported Heavy/recording paths remain explicitly unqualified. See
[lab workspaces and qualification](engineering-labs-and-evidence.md).

## Investigate, retain evidence, and choose the next test

Performance analysis reuses Raven's indexed retained traces, topology lanes,
causal flows and counter evidence. Elapsed calls, measured on-CPU slices, device
activity and explicit wait states remain distinct. Missing GPU data does not
discard CPU/memory evidence. Cross-node clocks retain their uncertainty.

Incident bundles retain selected trace windows, scheduler evidence, logs and
samples under declared event and byte budgets. Existing cooperative triggered
capture can retain pre/post windows when an application declares an observed
condition. Exporting a bundle does not attach a debugger or retroactively
recover missing events. `incidents load` verifies retained bundles later.

Adaptive diagnosis proposes a bounded next measurement. It labels observations,
hypotheses, missing evidence and estimated overhead. Its effectiveness remains
experimental until evaluated on independent site incidents. See
[performance and incidents](performance-incidents.md).

## Compare resources and recover workflows

Controlled optimization reviews a whole finite experiment budget, comparable
workload definitions and repeated variants before any trial is submitted.
Unknown submissions occupy the concurrency budget until reconciled. Resource
requests are not measured consumption. Failed/missing trials are retained in
the analysis, and predicted advice is not presented as a measured improvement.

Recovery distinguishes node failure, preemption, timeout, OOM, cancellation and
application failure. Reusable outputs need explicit validation evidence;
checkpoint resume needs verified provenance and a compute-side hash guard.
Review the exact attempt, billing and corrected resources before retrying.
See [controlled trials and recovery](controlled-trials-recovery.md).

## Request files and automation

Advanced requests contain named arguments matching the service's public API:

```json
{
  "arguments": {
    "project": "/path/to/project",
    "config": {"imports": ["raven"], "gpu": false}
  },
  "scheduler": {
    "name": "slurm",
    "scope": {"cluster": "lab-cluster", "user": "alice", "account": "LabX"},
    "host": "lab-login"
  }
}
```

For example, pass this to `raven engineering qualification prepare --request
probe.json`. Local services also accept a plain arguments object. The compiled
registry rejects unknown domains, actions and arguments. User documents cannot
select imports or replace runtime dependencies. `--output FILE` writes a new
private result file and refuses to overwrite an existing file.

Reliability evaluation accepts observed measurements and explicit budgets. It
reports missing measurements, latency regressions, freshness, scheduler load,
process RSS and Python allocations separately. Budget defaults are targets,
not measured guarantees. Review the [coverage ledger](https://github.com/rahel99x/Raven/blob/main/docs/implementation/engineering-platform.md)
for actual test evidence and external qualification gates.

For reproducible protocol and scheduler checks, see the
[Kubernetes client wire guide](kubernetes-client-qualification.md) and
[disposable Slurm qualification guide](native-slurm-qualification.md).
