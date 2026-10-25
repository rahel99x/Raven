# Native batch scheduler semantics

Raven includes adapters for IBM Spectrum LSF, OpenPBS, PBS Professional,
HTCondor, and Flux alongside Slurm and Kubernetes. The shared
[connection and review contract](native-schedulers.md) controls bounded reads,
ownership checks, reviewed actions, durable receipts, and unknown outcomes.
This guide explains the native differences that matter when connecting a lab.

These adapters have native-format regression coverage. They have **not** been
qualified against live installations of these five scheduler variants on this
host. Check the actual client/server version, history configuration, permission
model, and command output on your site before relying on them operationally.

## Choose the connection, then inspect

Use commands installed on your workstation or the scheduler login host. For a
remote installation, `--host` selects an existing SSH alias; it is independent
of a native queue, accounting project, HTCondor schedd, or Flux instance.

```bash
raven scheduler catalog
raven scheduler capabilities --scheduler openpbs --cluster campus --user alice
raven scheduler jobs --scheduler openpbs --cluster campus --user alice
raven scheduler history --scheduler openpbs --cluster campus --user alice
```

Replace `openpbs` with `pbspro`, `lsf`, `htcondor`, or `flux`. Capability inspection
does not prove a login is authorized, submit a test job, or contact the scheduler.
Queue and history commands make bounded native reads. Missing or incompatible
identity fields produce the actual diagnostic; they are not replaced by guessed
identities. An unavailable optional counter retains its native value and read
diagnostic in `native.resource_diagnostics`; the job and other valid measurements
remain visible. Unevaluated HTCondor expressions or PBS word-size memory units
do not become fabricated numbers or hide otherwise verified jobs.

| Scheduler | Read commands | Exact execution identity | Native submission file |
| --- | --- | --- | --- |
| LSF | User-scoped `bjobs -json -a` with explicit columns | Job/array element, native submission timestamp, connection | Shell file containing `#BSUB` directives |
| OpenPBS / PBS Professional | User-scoped `qselect`, bounded `qstat -t -f -F json` chunks; `-x` for history | Server-qualified job/subjob ID, native `qtime`, `run_count`, connection | Shell file containing `#PBS` directives |
| HTCondor | Owner-filtered `condor_q` / `condor_history` ClassAd JSON | `ClusterId.ProcId`, `GlobalJobId`, `QDate`, `NumJobStarts`, schedd scope | HTCondor submit description, usually `.sub` |
| Flux | `flux jobs --json` through a pinned instance URI | Lossless job ID, submission timestamp, numeric owner, instance scope | Flux batch script containing `# flux:` directives |

Array parent and child rows must not both become accounting totals. PBS expands
subjobs and excludes parent rows; actionable IDs contain an exact array index.
LSF likewise rejects array ranges or wildcard IDs. Completed-job availability
depends on each server's retention: Raven does not treat a current queue list as
a complete historical accounting database.

## Resource requests preserve native meaning

The explicit `cpus` override means a **total requested CPU/slot count for one
submitted job**. It does not mean measured utilization or guarantee a physical
core. A multi-process HTCondor submit description may create multiple jobs;
its per-process requests and returned submission range remain explicit.

| Field | LSF | PBS variants | HTCondor | Flux |
| --- | --- | --- | --- | --- |
| `cpus` | Total `bsub -n` slots | Total `ncpus`, split evenly across `select` chunks | `request_cpus` per process | Total cores, split across node slots |
| `nodes` | Keep native placement directives | Number of homogeneous `select` chunks | No portable override | Explicit node count and slots |
| `memory_mib` | Keep native reservation directives; site units and scope differ | Total MiB, split evenly across chunks using native `mb` units | `request_memory` per process, native MiB | No stock portable memory request; keep native site configuration |
| `gpus` | Keep native directives; LSF `num=N` is ordinarily per host | Total GPUs, split evenly across chunks | `request_gpus` per process | Total GPUs, split across node slots |
| `wall_seconds` | Rounded **up** to whole minutes in reviewed `-W` | Exact native `walltime=HH:MM:SS` | No universal enforced wall limit; keep explicit site policy | Explicit duration with an `s` suffix |
| `account` | Native project `-P` | Native accounting string `-A` | Native `accounting_group` | Native bank; charging needs an accounting plugin |

For example, a PBS override of 8 CPUs, 2 nodes, 4,096 MiB, and 2 GPUs becomes
`select=2:ncpus=4:mem=2048mb:ngpus=1`. Raven rejects totals that cannot be divided
evenly. Heterogeneous chunks and advanced placement belong in the native script.
Unspecified fields retain the script's native directives; unsupported overrides
are rejected with a reason instead of being silently ignored.

Measured memory and allocated memory are separate from requested memory. PBS
binary sizes retain explicit units. LSF `nalloc_slot` remains an LSF slot count.
HTCondor fractional CPU requests are not rounded. Flux missing GPU or memory
attributes remain unknown. None of these sources alone measures CPU efficiency,
GPU utilization, or the actual bill charged by a lab.

## HTCondor: transport, schedd, and accounting are separate

Use a scope file when the schedd and login host differ:

```json
{
  "cluster": "campus-condor",
  "user": "alice",
  "schedd": "submit.campus.example",
  "pool": "manager.campus.example"
}
```

```bash
raven scheduler jobs --scheduler htcondor --scope condor-scope.json \
  --host campus-login
```

`--host` controls SSH. The scope's `schedd` and `pool` select HTCondor's native
`-name` and `-pool` targets. `accounting_group` comes from the submission settings
or native description; it does not change the authenticated login.

The adapter requests specific ClassAd attributes and a bounded result count.
Existing-job mutations include a schedd-evaluated constraint over the exact
process, `GlobalJobId`, submission time, start count, and owner. A recycled job
number or changed owner cannot match that reviewed constraint. Missing start
counts stay unknown and disable mutations.

A submit description may queue more than one process. Raven uses native
`-single-cluster` and a job-count bound, retains the returned first/last process
range, and does not expand a huge range into memory. Review the description's
`queue` statement and per-process resources before submitting; one invocation
is not necessarily one process or one resource reservation.

## Flux: pin the instance

Nested Flux instances can reuse the same locally unique job numbers. Select a
concrete broker URI, not a relative parent-job alias:

```bash
raven scheduler jobs --scheduler flux --cluster lab-flux --user alice \
  --uri local:///run/flux/lab-instance
```

Raven uses `flux proxy URI flux ...`, preserving the native instance boundary
through the same bounded transport. A missing URI permits inspection of the
current environment but disables submission and cancellation. Concrete
`local://` and `ssh://` targets are accepted; Flux authentication and site
permissions remain authoritative.

Decimal and F58 forms such as `58`, `f21`, and `ƒ21` are normalized to one exact
unsigned 64-bit decimal identity. This avoids browser integer rounding and
duplicate attempts caused by locale-dependent F58 prefixes. Numeric IDs and
the JSON `jobid` field must agree. Flux urgency changes are not exposed as a
portable hold/release pair: restoring a default urgency would lose a user's
original priority.

## PBS and LSF identity precision

OpenPBS's `qstat` formatter converts timestamp attributes to full `ctime` text
even in JSON output. A value such as `Sun Oct 25 12:00:01 2026` is retained
without inventing a timezone or converting it with the viewer's local timezone.
Numeric native epoch variants are supported too. Partial dates are rejected.
A missing PBS restart counter remains unknown and disables job mutations.

Some LSF versions/sites return submission dates with only month, day, hour, and
minute. Such rows can be inspected, but Raven does not infer a year or seconds
to authorize a mutation. The native evidence must provide year and seconds or
an authoritative epoch. Otherwise use the native site's controls after your
own review and keep the connection read-only in Raven.

PBS and LSF do not offer a portable atomic generation condition on their
command-line cancellation operations. Raven reads the exact attempt and owner
again immediately before issuing one command, but cannot remove a native
server-side race. HTCondor's full ClassAd constraint and Flux's instance-scoped
immutable job number provide stronger native target conditions. Hold/release
are supported for PBS; LSF hold/release is limited to pending `PEND`/`PSUSP`
jobs, so an ordinary hold cannot suspend a running LSF job.

## Reviewed bytes and recovery

Review records the native arguments, connection, owner, script digest, and
expiry. Apply verifies the source again and streams the verified text to native
stdin. This preserves `#BSUB`, `#PBS`, Flux, and HTCondor directives without a
path-changing race or shell interpolation of script contents. The original
file is untouched. Set a name explicitly or retain a native name directive;
an unnamed native stdin submission may otherwise be called `STDIN`.

A durable action claim precedes every mutation. Timeout, disconnect, output
overflow, or an unrecognized submission response can mean the scheduler already
accepted the action. Raven reports **unknown**, retains the consumed token,
and does not retry it. Inspect native state and the receipt before preparing
another plan. A missing result is not proof that no job was submitted.

## Source and qualification evidence

The parser audit used these upstream source revisions on 2026-10-25:

- OpenPBS [`cd7ab5e`](https://github.com/openpbs/openpbs/blob/cd7ab5edaf03dcac2f1cb9e2f42d75eb117d468a/src/cmds/qstat.c): timestamp formatting and JSON/alternate-format conflicts; the same revision's `qstat.1B` and `qselect.1B` documents describe `-t`, `-F json`, `-T`, `-u`, and `-x`.
- Flux [`40ecc8d`](https://github.com/flux-framework/flux-core/tree/40ecc8d80b20e8b23fba32db0e4d11f81178c254/doc/man1): `flux-batch`, `flux-jobs`, `flux-proxy`, `flux-cancel`, job slot units, native bank behavior, and duration suffixes; `src/common/libutil/fluid.c` defines the decimal/F58 identity relationship.
- HTCondor [`64c2d04`](https://github.com/htcondor/htcondor/tree/64c2d04edfe52cb69ebdf13e756bd793dad3698e/docs/man-pages): JSON projection, owner constraints, bounded history, native submit descriptions, submission ranges, and job-count limits.

LSF command variants and submission-time precision still require confirmation
against an actual licensed installation. The PBS Professional variant likewise
needs its own vendor-version qualification; shared OpenPBS formats do not prove
commercial-site compatibility. Native fixtures, literal-stdin subprocess tests,
and bounded workload tests live in `tests/test_scheduler_native_batch.py`.

From a source checkout with development dependencies, reproduce the focused
checks and absolute parser-cost measurement with:

```bash
python -m pytest -q tests/test_scheduler_native_batch.py
python scripts/benchmark_native_schedulers.py --records 1000 --repeats 5
```

The benchmark contacts no scheduler. It reports equivalent source-format
fixtures, process CPU and wall time, a separate Python allocation pass,
whole-process peak RSS, and the source hashes measured. There was no equivalent
native adapter before this revision, so it makes no before/after speedup claim.
