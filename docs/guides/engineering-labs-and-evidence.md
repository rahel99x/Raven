# Lab workspaces, compute qualification, and research provenance

Raven keeps system login, billing account, scientific resources, and its Heavy
worker resources separate. Saving preferences does not request an allocation.
Reading a lab inventory does not submit jobs. Compute qualification produces a
review before any native scheduler mutation.

## Save a workspace

Start with [the lab workspace request](../examples/engineering-lab-save.json).
Replace the example login, cluster, account names, and SSH alias with your own.
The accounts are declarations until checked against your site's association
discovery. Existing **Setup / Connections and accounts** discovers Slurm
associations through its governed active connection. An association indicates
membership; it does not prove available credit, an allocation balance, or
permission to charge a particular project.

```bash
raven engineering labs save --project /home/alice/experiments \
  --request docs/examples/engineering-lab-save.json
raven engineering labs load --project /home/alice/experiments
```

Each `sites` entry contains:

| Field | Meaning |
| --- | --- |
| `id` | Stable local site identifier; unique in this workspace. |
| `scheduler` | `slurm`, `kubernetes`, `lsf`, `openpbs`, `pbspro`, `htcondor`, or `flux`. |
| `login` | The login that owns native jobs; never inferred to be a billing account. |
| `scope` | Native scheduler authority. `user` must equal `login`; pin the cluster and relevant native context. Kubernetes also pins context, namespace, and API URI; Flux pins its instance URI; HTCondor can pin `schedd` and `pool`. |
| `connection` | Optional explicit SSH `host` and `ssh_user`; otherwise native commands run locally. `scope.host` must match the SSH host. Existing SSH configuration supplies credentials. |
| `accounts` | Explicit account names associated with the login. An empty list uses scheduler defaults; it does not assert that a workstation has unlimited resources. |
| `association_source` | Required when accounts are listed. Record whether they were discovered or declared, where, and when. |
| `job_defaults` | Scientific-job draft resources. |
| `heavy_defaults` | Independently selected Raven Heavy draft resources. |
| `profile`, `lab`, `project` | Optional readable references to existing Raven profiles and scientific context. |

Resource dictionaries use `account`, `partition`, `cpus`, `memory_mib`,
`wall_seconds`, `nodes`, and `gpus`. These remain drafts: each scheduler validates
its own supported resource meanings when preparing a native submission. In
particular, Slurm CPUs, LSF slots, PBS ncpus, Flux cores, HTCondor requests, and
Kubernetes requests/limits are not interchangeable physical-core measurements.

Updates use compare-and-swap. Load the current `digest`, remove that field from
the edited document, and pass the loaded value as `expected_digest` beside
`document` in the save request. A concurrent edit produces a refresh message
instead of overwriting another change. State is stored privately under
`PROJECT/.raven/engineering/labs/`.

Use `defaults_for_workspace` with a request such as
`{"site_id":"lab-cluster","purpose":"job","account":"lab-x"}` and
`--project /home/alice/experiments`. Choose `"purpose":"heavy"` independently.
The result states the billing selection and requires native submission review.
It does not automatically change a running Heavy allocation or create credit.

Use `inventory_from_workspace` with a request containing
`{"sites":["workstation","lab-cluster"]}`. Only those explicit connections are
opened, and all are closed after collection. The default ceiling is eight sites,
10,000 retained jobs, and 30 seconds before another site read begins. Each
adapter separately enforces command time/output bounds and cancellation.
Disconnected, deferred, failed, and scope-mismatched sites remain individually
visible. Results are per-site observations, not an atomic cross-cluster snapshot.
Allocation balances remain unknown where a native source is unavailable.

## Verify a compute environment

[The compute probe request](../examples/engineering-site-probe.json) checks a tiny
real compute process. Set its scheduler scope and billing/partition fields first.
An explicit `python` can select a shared compute-host interpreter; the default is
the interpreter running Raven. Set `scratch` to an actual compute-host scratch
directory. The project/result path must be visible from both submission and
compute hosts.

```bash
raven engineering qualification prepare --project /home/alice/experiments \
  --request docs/examples/engineering-site-probe.json
```

Preparation writes an immutable private script and returns `id`, `script`, and
a native `review` object. Inspect the script, account, resources, and native
command. Save the returned `review` object as its own JSON file, then use the
native scheduler's reviewed apply path:

```bash
raven scheduler apply --scheduler slurm --scope /path/to/scope.json \
  --plan /path/to/review.json --review-digest THE_REVIEWED_DIGEST
```

Apply is a separate, explicit action. It requires the same connection scope and
a private durable action journal. An uncertain submission is reconciled with
native job state; never repeat it automatically. Use the submission receipt's
job ID to retrieve its exact native identity with `raven scheduler jobs` or
`history`, then collect using a request with that scheduler descriptor and
`{"project":"/home/alice/experiments","identifier":"probe-...","identity":{...}}`:

```bash
raven engineering qualification collect --request /path/to/collect-request.json
```

The probe checks explicit imports, scratch read-after-write, a real metrics
record/write/read, and process CPU/RSS counters. With Slurm, it also checks a
small retained trace and optionally worker startup through an actual `srun`
step. Worker startup reaching placement is distinct from fully verified worker
ownership; the probe executes no analysis tasks. GPU checking is opt-in and
identifies its actual provider. Missing provider support does not imply a broken
GPU. Process counters are not scheduler accounting or per-job GPU counters.

The reviewed probe defaults to one CPU, 512 MiB, and two minutes. PBS/Slurm
express these native limits. HTCondor uses a proper submit description and a
reviewed `periodic_remove` wall budget. LSF/Flux adapters do not invent portable
memory syntax: their prepared result explicitly marks memory enforcement
unverified so the site's effective default can be reviewed before applying.

Kubernetes requires an explicit Job manifest with its image and shared result
mount. Preparation returns `needs-manifest` until supplied. Its reviewed copy
uses one job/container, a bounded active deadline, and explicit CPU/memory
requests and limits. It refuses extra accelerator requests in this tiny portable
probe. The probe records namespace, Job controller UID, and the distinct Pod UID
via DownwardAPI. Collection rechecks the Job UID against the pinned API
server/context/namespace; matching names alone are insufficient.
Native Heavy ownership and retained trace identity outside Slurm remain
unqualified, with independent readiness indicators. Native runtime linkage that
does not expose the job ID remains `unverified`, even when imports succeeded.

The probe also makes an explicit bounded lookup of its own native job on compute
to capture the current submission/restart identity and owner. This lookup is
limited to three seconds, 1 MiB, and at most 64 parsed records; it does not sweep
all users' jobs. Missing tools or permissions remain an unverified native binding,
with their actual error. Collection uses a fresh native detail read, checks owner,
and compares the entire compute-observed identity. A matching numeric job ID alone
cannot qualify a replacement or requeued attempt.

Imports run in a bounded child process within the reviewed compute allocation;
they cannot add modules to Raven's in-process command registry. Slurm probes pin
their working directory and private output/error paths, so an unrelated read-only
submission directory cannot prevent startup.

Results include actual read/import errors, source hashes, nonce, host, Python,
check durations, and the exact scheduler identity. Repeated collection is
idempotent. A nonterminal job remains partial; a later terminal state publishes a
new immutable observation and updates the latest-receipt pointer. Scheduler
failure and unverified native memory limits remain visible even if individual
readiness checks passed. Hashes detect changes; they
are not remote attestation. Qualification is limited to the tested compute
process and conditions. It does not certify all nodes, a hardware family, remote
authentication, production scale, or missing metrics.

## Keep plot data and provenance together

Existing Raven exports retain full CSV data, unit contracts, exact run/attempt
metadata, source byte watermarks, payload hashes, and a `COMPLETE.json` marker.
`provenance from_export` verifies those existing records and builds a companion
without modifying the immutable export. Start with
[the provenance request](../examples/engineering-export-provenance.json), replace
its paths and identity with a real native lookup, and use:

```bash
raven engineering provenance from_export \
  --request docs/examples/engineering-export-provenance.json
```

The companion contains every selected numeric row, units, exact calculation name
and version, parameters, ordered filters, source hashes/formats/versions, clock
domain/uncertainty, and coverage. Missing numeric values remain `null`. Source
units are checked before values are labelled; units are never silently changed.
Multiple scientific metrics require an explicit metric filter. `eq`, `ne`,
`lt`, `le`, `gt`, `ge`, `in`, and inclusive `range` filters are applied to the
CSV, and the number of excluded rows is retained. The bounds are 2 MiB of input
CSV and 20,000 scanned rows; oversize input asks for a smaller explicit export
range instead of silently downsampling.

Persist a result with `provenance write`, passing `name` and `document` together
with `--project`. Published companions cannot overwrite an existing name.
`provenance compare` rejects mismatched units, calculation definitions, or
filters and reports differing sources, coverage, identities, and clock context.
Timing alignment requires a known matching clock domain/unit and known clock
uncertainty. Comparable metadata does not establish scientific equivalence.

The legacy export's attempt numbering does not reconstruct a generic scheduler
UID or restart generation. Supply the selected native identity explicitly;
the companion verifies the CSV job ID and a single exact exported run/attempt.
It retains the original source identity separately from that requested native
identity and returns `status: partial` with `association.status: unverified`.
An arbitrary matching numeric ID never becomes verified native lineage.
An optional `experiment_project` and `experiment_id` resolve an existing exact
run/native link in the private experiment index; that becomes `recorded-link`
and includes the source revision. The retained run is revalidated. A recorded
user association remains distinct from independent scheduler attestation, so
native lineage claims stay unverified in comparison results.

CSV integer cells retain their exact integer values, including counters above
2⁵³. Numeric filters compare exact decimal values instead of rounding through
binary floats. Source timestamp fields with an explicit Unix-second clock cannot
be relabelled nanoseconds without a real numerical transformation; an unknown
source clock retains unknown units. Existing native profiler
evidence retains its original receipt and semantics; attaching it additionally
requires the exact native connection scope to prevent same-number jobs on
different clusters from being conflated.
