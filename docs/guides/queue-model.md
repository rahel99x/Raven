# Observable queue model

Raven's experimental queue model evaluates an **explicit scheduling subset**.
It supports inspectable what-if calculations and validation against independent
recordings. It cannot reconstruct hidden scheduler state or replace Slurm.

Open **Research → Operations → Observable queue model**, or use:

```bash
raven run ops run queue-model action=discover
raven run ops run queue-model action=simulate path=examples/queue-model.json
```

Discovery reads bounded `scontrol` configuration, node, partition and reservation
responses from the active connection. Missing permissions stay visible. It does
not automatically convert incomplete observations into a certified model.
Simulation uses only the explicit JSON file and issues no scheduler commands.
Managed Raven runs simulation in its admitted Heavy worker; discovery remains
an explicit governed scheduler read. Cancellation uses the existing operation
worker/process lifecycle.

The [complete synthetic example](../../examples/queue-model.json) has a 60-second
preparation followed by two concurrent fits, completing at 180 seconds. This is
a hand-computable contract check, not live scheduler evidence.

## Input contract

The input schema is `raven.queue-model/v1`. Times are seconds in one consistent
domain, either relative to a shared origin or epoch seconds. Memory is MiB.

| Field | Required contents |
| --- | --- |
| `id` | Bounded immutable model identity. |
| `nodes` | Up to 256 entries, each with unique `id`, `cpus`, `memory_mb`, `gpus`. |
| `policy` | `backfill`, `fairshare_weight`, `usage_half_life_seconds`, `accounts`, `qos`. |
| `policy.accounts` | Named entries with positive `weight` and nonnegative `usage_cpu_seconds`. |
| `policy.qos` | Named entries with `max_running`, `max_cpus`, `max_wall_seconds`. These limits apply across jobs using that QOS. |
| `jobs` | Up to 2,000 explicit records with the fields below. |
| `reservations` | Optional exclusive-node windows with `id`, `start`, `end`, `nodes`, and allowed `accounts`. |
| `traces` | Optional separate calibration and validation traces. |
| `hidden_delay_seconds` | Explicit sensitivity assumption that all nodes are initially unavailable; default 60. |
| `seed` | Reproducibility receipt field. The base model and tie ordering are deterministic and do not use random scheduling. |
| `metadata`, `observability` | Optional descriptive provenance and known gaps. |

Each job supplies `id`, `submit`, `duration`, `wall_limit`, `nodes`,
`cpus_per_node`, `memory_mb_per_node`, `gpus_per_node`, `priority`, `account`,
`qos`, and `depends`. `depends` contains exact in-model IDs with afterok semantics.
Optional `success: false` prevents dependent jobs from becoming eligible.
Unknown dependencies, cycles, missing policies and unsupported action fields
are rejected. Unplaceable jobs remain blocked with a reason.

The model chooses highest priority first, then arrival time and ID. Its optional
fairshare component adds a weighted, decayed account-usage score; this is not
Slurm's complete hierarchical fair-tree algorithm. Backfill protects the first
blocked eligible job using active jobs' declared wall limits. A lower-priority
job must finish within that protected window. Actual modeled completion can free
resources earlier. Reservations prevent a job from crossing an inaccessible
reserved-node window.

The declared duration is an input estimate. It must fit the requested wall limit.
A calibration factor that exceeds the wall limit clips execution to that limit
and blocks afterok dependents; shortened requests do not silently receive
unlimited runtime.

## Calibration and chronological validation

Each trace has a distinct `id`, a `role` (`calibration` or `validation`), an
`origin` (`recorded` or `synthetic`), and its own `jobs`. Trace jobs add
`actual_start` and `actual_end`; requests and duration estimates must have been
known before those outcomes. Optional `source_sha256` links a captured source.
An origin label is a declaration, not an authenticity proof.

Calibration estimates one median runtime scale from observed versus declared
durations. Validation starts after all calibration observations end. The same
attempt cannot appear in both sets. Validation computes start/wait mean absolute
error, 95th-percentile absolute start error, prediction coverage and pairwise
start-order accuracy. Ranking examines at most 50,000 chronological-input pairs;
the exact compared pair count is included. Ties in observed starts are excluded.

The result includes model/version/input digests, parameters, explicit blocked
jobs, event/placement counts and a hidden-state sensitivity scenario. Sensitivity
is not a calibrated confidence interval. Calibration and evaluation without
real independent traces remain labelled synthetic; no result grants production
qualification automatically.

## Boundaries and qualification

The simulator handles nonpreemptive homogeneous requests, explicit CPU/GPU/memory
capacity, afterok dependencies, simple priority, conservative backfill,
account-usage weighting, exclusive node reservations and the listed QOS limits.
It excludes hidden demand, licenses, topology, complete Slurm priority policy,
federation, heterogeneous components and preemption. Mixed unknown policy cannot
be interpreted as unlimited capacity.

Computation is capped at 10,000 events and five million placement operations per
run. Evaluation accepts at most 20 traces and 10,000 measured attempts. Exceeding
a bound reports an error; it does not publish a partial schedule as complete.

Real qualification uses a separate isolated Slurm container with two chronological
queues, known scripts and captured start/end/accounting records. Its scripts and
receipts live under `scripts/slurm/queue_observations.py` and the review evidence
directory. Small container cases establish exercised semantics only. A site's
product gate requires representative independent workloads, hidden-state
sensitivity and accepted error/coverage thresholds for that site.
