# Reproducibility, scientific acceptance, and result reuse

[Operations guide](operations.md) · [Project standard](../PROJECT_STANDARD.md) · [Examples](../../examples/operations-science/)

Use these operations to compare runtime environments, inspect process placement,
evaluate declared acceptance criteria, compare experimental groups, verify result
reuse, and repair dependency chains. Scientific criteria and independent
experimental units must come from the project.

Open **File → Research and cluster operations**, or enter `:ops FEATURE`.
Set the fields and select **Inspect / prepare** to collect evidence.
For an operation that writes files or changes jobs, select **Review action**,
check the plan, then select **Confirm apply**. Inspection and file hashing run
in a background worker; pointer movement and rendering use the retained result.

Each review belongs to one connection and expires after five minutes. A change
to the connection, selected job attempt, manifest, or reviewed source requires
a new review. Do not reuse an uncertain scheduler action without inspection.

## Recreate an environment: `:ops environment`

Set **Environment manifest** to a `tower.environment/v1` JSON file. The manifest
declares the exact Python patch version, operating system, machine architecture,
package versions, and a SHA256-verified requirements lockfile. The lockfile
accepts exact `name==version --hash=sha256:DIGEST` entries. Include every
transitive dependency. URLs, editable packages, nested requirement files, index
options, and version ranges are rejected.

Inspection compares the declaration with the Raven host. It reports missing
packages separately from matching versions. Optional native constraints declare
`{"libc":"glibc","minimum_version":"2.28"}`. An unavailable libc identity
does not pass that check.

Use one of these optional `container` declarations:

```json
{"kind":"oci","reference":"docker://registry.example/project/image@sha256:64_LOWERCASE_HEX_DIGITS"}
```

```json
{"kind":"sif","path":"images/run.sif","sha256":"64_LOWERCASE_HEX_DIGITS"}
```

The first form pins an immutable OCI reference. Inspection does not pull the
image or claim that its bytes are already available. The second form hashes a
local Apptainer image. Mutable image tags are not sufficient identity.

To measure an allocation, set **Probe compute host for running job**. Raven
starts a short, overlapping one-CPU step on each allocation node. It reads
Python, package, platform, architecture, and libc versions. The report keeps
this compute-host evidence separate from the local host and from the existing
workload or container runtime. A new Slurm step cannot certify the environment
of a process that already runs. The allocation must belong to the active user.
Python 3 and Slurm step access must be available on each node.

To prepare recreation, set **New bundle directory** to a path that does not
exist. Apply writes `recreate.sh`, `requirements.lock`, `environment.json`, and
a receipt. Raven does not execute the script or install software. After site
review, run:

```sh
/path/to/bundle/recreate.sh /path/to/new-venv
```

The script checks runtime identity and the lockfile digest, reserves a new
directory, installs with `--require-hashes --no-deps`, and runs `pip check`.
Container declarations cause the script to run these steps through
`apptainer exec --cleanenv`; local SIF bytes are reverified before use. The
site must permit the image and the destination bind. Apptainer, the declared
Python version, packages or network access, and a supported platform are
prerequisites. GPU drivers and external native-library compatibility require
additional site evidence. The script does not infer them.

Bundle creation and local runtime inspection require Raven on the target host.
An SSH view cannot substitute files from the local computer.

Schema: [environment](../schemas/science-environment.schema.json).
Example: [manifest](../../examples/operations-science/environment.json).

## Verify CPU, NUMA, and GPU placement: `:ops placement`

Enter one exact running job ID. Raven verifies ownership and the attempt, then
starts a short allocation-local observer. `scontrol listpids` identifies
existing processes. The observer reads allowed CPU masks, allowed NUMA nodes,
observed NUMA page residency, and the GPU visibility variables of those
processes. It reads no other environment variables. It rechecks process start
identity and allocation membership before publishing results.

Requested allocation values appear separately from measurements. A GPU
visibility variable does not prove GPU utilization or device binding. An
allowed NUMA mask does not prove where memory pages reside. Missing `/proc`
permissions, process exits, unsupported fields, and incomplete nodes remain
visible gaps. Raven never changes process affinity.

The operation supports up to 128 nodes. Each node inspects at most 256
processes and 32 MiB of process data within a five-second inspection budget.
The command has a separate timeout. Truncation is reported. Use a smaller
allocation or a dedicated profiler when that scope is insufficient.

## Scientific acceptance: `:ops acceptance`

Set **Acceptance policy** and **Result measurements**. The policy contains
named cases and required metrics. Each metric declares its expected value,
absolute tolerance, and relative tolerance. Raven evaluates:

```text
abs(observed - expected) <= max(absolute, relative * abs(expected))
```

The report is a case-by-metric regression matrix. Missing cases, missing
metrics, nonnumeric values, and nonfinite measurements cannot pass. Duplicate
case or run IDs are rejected. At expected zero, relative tolerance provides no
allowance; set the absolute tolerance that the science requires. A Slurm
`COMPLETED` state is not an acceptance result.

Schemas: [policy](../schemas/science-acceptance.schema.json),
[results](../schemas/science-results.schema.json).
Examples: [policy](../../examples/operations-science/acceptance.json),
[results](../../examples/operations-science/results.json).

## Statistical comparison: `:ops statistics`

Set **Comparison manifest** to a `tower.statistics/v1` document. Name the
independent experimental unit. Use `independent` for disjoint unit IDs, or
`paired` for exactly matching unit IDs. Repeated samples from one trajectory
are not independent experiments merely because they have different timestamps.

Raven reports each mean, treatment minus control, and a Student t confidence
interval. Independent groups use Welch's variance and degrees of freedom.
Paired groups use within-unit differences. The default confidence is 95%;
the schema supports 80% through 99.9%. The two-sided p-value is exploratory.
There is no automatic multiple-comparison correction or causal interpretation.

Missing pairs and duplicate unit IDs cause an error. Fewer than two units, or
zero observed variance, leave inferential confidence unavailable. Small-sample
reports state the distribution and independence assumptions. Raven does not
convert missing uncertainty to certainty.

Schema: [statistics](../schemas/science-statistics.schema.json).
Example: [paired comparison](../../examples/operations-science/statistics.json).

## Verified intermediate-result reuse: `:ops reuse`

Set **Reuse manifest**, **Requested identity JSON**, and **New result directory**.
The identity must match the project and exact code, environment, parameter,
and scientific input hashes. Its `inputs_sha256` map must exactly match each
input-role dependency path and hash. Use an empty map only when the computation
has no input-role dependencies. Declare each identity file and every scientific
input as a dependency.
Include random seeds, data versions, configuration, and lockfiles. Raven cannot
discover hidden dependencies.

The reuse manifest also declares output paths and hashes, plus the acceptance
policy and measured results with their hashes. Raven verifies all files,
reruns scientific acceptance, and prepares a copy plan only when every required
check passes. A cached success flag or Slurm completion is insufficient.

Apply revalidates reviewed source identities and hashes, streams the outputs
into a new private directory, and checks the digest of each copied stream. It
rechecks dependencies before writing the completion receipt. Output names
cannot escape the destination, overlap one another, or replace Raven metadata.
An existing destination is never overwritten. Cancellation or a detected
source change removes this operation's incomplete destination. A process crash
can leave `.tower-incomplete`; that directory is not a verified result.

This operation copies files; it does not hard-link mutable source files.
Inspection supports regular files up to 64 GiB each and metadata up to 4 MiB.
Use explicit artifact manifests for larger workflows. Local materialization
requires Raven on the target host.

Schemas: [reuse](../schemas/science-reuse.schema.json),
[requested identity](../schemas/science-identity.schema.json).
The [complete reuse example](../../examples/operations-science/reuse.json)
contains valid hashes for its adjacent example files. Editing any of those
files requires new hashes and a new scientific review.

## Repair a dependency chain: `:ops dependency-repair`

Set **Dependency repair manifest** to a document that lists exact pending job
IDs, their expected submission times, and their desired Slurm dependency
expressions. Use an empty dependency string only when the review is intended
to remove that dependency. AND and OR separators cannot be mixed. Ranges and
shell expressions are not accepted. A live prerequisite closure that contains
`singleton` is refused because its implicit name and federation edges cannot
be certified from explicit job IDs. Replace it with explicit prerequisites
before a cycle-verified repair.

Raven reads the target jobs and their prerequisite closure. It verifies exact
attempt identity and detects cycles before providing a review. The closure
must remain available in Slurm; purged prerequisites do not become assumed
successes. Completed branches are observed, not requeued. Each review supports
up to 128 target jobs and 256 prerequisite records.

Apply rereads the closure, then updates one pending job at a time. It rechecks
ownership, attempt, state, and the original dependency immediately before the
update, and reads the job again afterward. It does not cancel jobs, requeue
successful work, or release administrative holds.

Slurm updates are not a transaction. A job can start between a read and an
update. A timeout can leave an uncertain result. Raven stops after the first
failure and lists confirmed, uncertain, and unapplied jobs. Inspect the current
scheduler state before preparing another review.

Schema: [dependency repair](../schemas/science-dependency-repair.schema.json).
The [example](../../examples/operations-science/dependency-repair.json) uses
illustrative IDs and times. Replace them with the exact current jobs.

## Validation and limits

Unit tests cover tolerance boundaries, missing measurements, paired identity,
published Student t quantiles, bounded metadata, invalid paths, file changes
during review and copy, cancellation, immutable container requirements, pending
job races, cyclic dependency graphs, and partial scheduler updates. Sequential
tests exercise acceptance followed by reuse, then reject changed scientific
evidence. Hardware, site permissions, image binds, scheduler policies, and
real compute-node process visibility still require live site checks.
