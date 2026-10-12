# Scale and infrastructure diagnostics

[README](../../README.md) · [Project standard](../PROJECT_STANDARD.md)

These four operations run on demand in the background. They do not attach a
profiler, submit jobs, change the selected cluster connection, or change Slurm
configuration. Use the operation form to select its source. Run the inspection
again when you need a fresh snapshot. The result contains readable rows and
structured data that you can copy or export with the Operations controls.

Open **File → Research and cluster operations**, or enter `:ops catalog`.
Enter `:ops bottlenecks`, `:ops clusters`, `:ops incidents`, or `:ops energy`
to open a feature form. Set the fields, then select **Inspect / prepare**. Direct
inspection also accepts explicit fields, for example:

```text
:ops run energy path=examples/scale-energy.json
:ops run clusters path=my-clusters.json history_hours=48
```

These diagnostics have no Apply action because they are read-only.

| Operation | Proposal | Input |
| --- | --- | --- |
| Distributed bottleneck explorer | S02 | Profiler JSON or Darshan POSIX text |
| Concurrent cluster workspace | S05 | Workspace JSON or configured profiles |
| Historical incident correlation | A17 | Job identity plus Slurm events or event JSON |
| Energy per useful result | A18 | Campaign energy JSON |

## Distributed bottleneck explorer

Select **Distributed bottleneck explorer**. Set **Profiler export** and select
the format. For JSON, use [raven.profiler/v1](../schemas/profiler.schema.json).
The [example](../../examples/scale-profiler.json) contains two ranks.

1. Export exclusive phase wall durations from your profiling system. Include
   the cluster, job ID, exact Slurm submit time, rank, phase, and node.
2. Set `attempt` to the exact Slurm `SubmitTime/StartTime`. The start time
   distinguishes requeues. Use only `SubmitTime` when the start time is unknown.
   Raven checks the selected snapshot and reports an unknown cluster identity
   when full connection verification is unavailable.
3. Include `expected_ranks` so that missing ranks can be detected. Include each
   rank/phase pair once. Missing measurements must be absent or `null`.
4. Open the file in the operation form and run the inspection.

**Expected result:** Raven shows the slowest rank, median phase duration, maximum-to-median skew,
MPI wall-time fraction, I/O wall-time fraction, and per-rank byte rates. The
result preserves the normalized records for charting and further analysis.
Threaded CPU time can exceed wall time. MPI and I/O time may overlap; do not add
their fractions. Missing counters do not become zero.

When all expected ranks have each measured phase and
`ordered_barrier_phases=true`, Raven computes the sum of phase maxima. This is
the declared barrier-phase envelope. It is not an asynchronous critical-path
reconstruction. Do not set the declaration for overlapping pipeline phases.
Skew or a high I/O fraction does not establish a network or storage failure.

For Darshan, export text with `darshan-parser` outside Raven. Choose **darshan**
and enter the source cluster, job ID, and attempt in the form. Raven reads
POSIX bytes and read/write/metadata duration counters. It does not execute the
parser. Shared-file rank `-1` cannot identify individual ranks and is rejected;
export unaggregated records. Missing per-file counters keep the rank total
unknown. CPU, elapsed time, and rank-node mapping are not inferred from these
counters. Native Darshan binaries and Score-P/Cube binaries are not parsed;
export their exclusive rank/phase measurements into the JSON contract.

The export limit is 4 MiB and 20,000 rank/phase records. Missing-rank reports
include a count and at most 32 example ranks. They do not build a rank-by-phase
matrix, which can otherwise grow quadratically.

## Concurrent cluster workspace

Select **Concurrent cluster workspace**. Leave **Workspace JSON** empty to use
configured Raven profiles, or provide
[raven.cluster-workspace/v1](../schemas/cluster-workspace.schema.json).
Adapt the [example](../../examples/scale-clusters.json) to your login hosts.

Each source needs a unique display name. A source can refer to a configured
`profile`, or set `host`, `ssh_user`, `user`, and an optional Slurm `cluster`.
An empty explicit `host` means the machine running Raven. A profile inherits
the base host and trusted SSH options. JSON cannot add SSH transport options.
`cluster` selects Slurm's `-M` option; omit it when the login host has one
default cluster. The source `user` selects the Slurm owner. The form's **Slurm
user** overrides all source owners when entered.

The reader retrieves each queue and recent accounting history. The default
history window is 24 hours; the supported range is greater than zero and at
most 744 hours. It requests duplicate accounting IDs and preserves separate
submit/start attempts. A source failure does not hide other source results.
A failed history query also preserves a successful queue query.

Results retain a connection descriptor and the identity
`[cluster_key, job_id, submit/start]`. Numeric job IDs alone are not identities.
The cluster key includes the configured endpoint and source. To read all
captured fields for one job, enter its **Inspect source name**, **Inspect job
ID**, and, when ambiguous, its exact **Inspect submit/start identity**. The
active Raven connection remains unchanged. Cross-cluster rows cannot issue
scheduler mutations through this view.

There are at most eight sources and four concurrent readers. Single-worker
mode uses one reader. The worker setting is captured when the operation starts.
Every child reader is joined before its parent operation finishes, including
during cancellation. A Single/Multi transition drains the parent and its child
readers; it does not abandon a transport or launch a second overlapping read.
Each command has a four-second transport budget (SSH adds its bounded transport
allowance), a 1 MiB output limit, and at most 5,000 parsed job rows. These limits
apply per source and per query. This is an on-demand workspace snapshot, not an
always-running additional scheduler poller.

The combined result retains at most 5,000 job rows and 4 MiB of job data. Source
summaries still report each full collected count. If this budget is reached,
the report identifies omitted rows. Narrow the source set or history window
before looking for an omitted job. Source summaries do not duplicate the full
job records in the exported result.

## Historical incident correlation

Select a job, then open **Historical incident correlation**. Raven uses the
captured job nodes and actual start/end interval. For an active job, the end is
the current time, not the requested time limit. Pending jobs without a known
allocation cannot be correlated. Reused job IDs with ambiguous attempts are
rejected. Set explicit start/end overrides only when the recorded interval
needs correction; use timestamps with a timezone.

With no file path, Raven requests read-only node events from `sacctmgr`. It
does not ask for administrator access. If accounting permissions or event
retention prevent the query, export site-provided notices as
[raven.incidents/v1](../schemas/incidents.schema.json) and set **Events JSON**.
The [example](../../examples/scale-incidents.json) uses an explicit UTC interval.
Use exact expanded hostnames in the file. An absent or null event end means the
incident is ongoing. The cluster name must match the selected job connection.

Raven requires both node overlap and time overlap. Adjacent intervals with no
shared time do not match. Reports show the overlap duration, matched nodes,
state, and reason. An overlap is evidence for an investigation; it does not
prove why a job failed. An empty result does not prove infrastructure health.
Slurm timestamps without offsets are interpreted in Raven's local timezone.
When the login host and Raven use different timezones, use an explicit-offset
event export and interval overrides.

## Energy per useful result

Select **Energy per useful result** and load
[raven.energy/v1](../schemas/energy.schema.json). Start with the
[campaign example](../../examples/scale-energy.json). The producer defines the
scientific acceptance rule and the unit of useful work. Raven does not infer
accepted output from a successful scheduler exit code.

Include every attempt, including failures and retries, once under its unique
cluster/job/attempt identity. Set `accepted_work` to the number of accepted
units, zero for a known zero, or `null` if not known. Each attempt has one
accounting energy series. Do not sum overlapping node and GPU totals.

| Field | Meaning |
| --- | --- |
| `scope` | `job`, `node`, or `gpu` measurement boundary |
| `attribution=exclusive` | The measured boundary belongs exclusively to this attempt |
| `attribution=shared` | Job ownership cannot be established; excluded from attributed totals |
| `attribution=allocated_fraction` | Explicit resource-allocation estimate; requires `fraction` in `(0,1]` |
| `kind=total` | Producer total in J, kJ, Wh, or kWh |
| `kind=counter` | Monotonic cumulative readings in an energy unit |
| `kind=power` | Power samples in W, integrated with the trapezoidal rule |

A complete producer total requires `coverage_complete=true`. This is a
producer attestation that the total covers the whole attempt. For counter or
power series, provide the actual attempt `window_start` and `window_end`.
The first and last samples must match those boundaries. Sample timestamps
must strictly increase. The default maximum sample gap is 300 seconds; set
`max_gap_s` to the producer's meaningful gap limit.

Missing samples, long gaps, and counter-reset intervals remain unknown. Raven
does not interpolate across them or assume that a reset value is additional
energy. It continues integrating the valid intervals and labels the result
partial. A partial measurement window cannot be presented as whole-job energy.

Raven reports observed attributed joules, energy spent in non-success attempts,
accepted work, and joules per accepted unit. The ratio is unavailable if any
attempt has missing energy or work evidence, incomplete coverage, or a shared
measurement. A campaign with mixed job/node/GPU boundaries also has no ratio.
Zero accepted work makes the ratio undefined. Allocation fractions produce an
explicitly estimated result. Finite zero measurements remain valid.

## Validation and deployment limits

The unit tests cover malformed identities and times, duplicate attempts,
unknown measurements, shared energy, counter resets, incomplete windows,
missing ranks, bounded output, timeouts, cluster isolation, and cancellation.
Cross-module tests exercise repeated inspections and shared interface routing.
Real cluster authentication, historical event permissions, profiler exporter
semantics, and hardware energy attribution require site validation. Do not
mark shared hardware measurements as exclusive to make a result appear complete.
