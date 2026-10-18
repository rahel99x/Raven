# Checkpoint interval advice

Raven can compare periodic checkpoint intervals using **measured elapsed costs**
and an allocation-level failure model. The advisor is experimental and read-only.
It does not install an application checkpoint hook, change a job, or restart it.

Start with [the complete example](../../examples/checkpoint-advice.json):

```bash
raven run checkpoint-advice examples/checkpoint-advice.json --no-state --no-plugins
```

In the dashboard, enter `checkpoint-advice /path/to/advice.json`. Research opens
the same advice, assumptions and baseline comparison. The attached input path is
remembered through the existing planning settings. The example uses declared
estimates; replace those with your own measurements before relying on the advice.

## Input fields

All durations and exposures are in **seconds**. Inputs are bounded JSON with
schema `raven.checkpoint-advice/v1`.

| Field | Meaning |
| --- | --- |
| `scope` | `partition`, positive integral `nodes`, `application`, optional `cluster`. Use one comparable allocation/application cohort. |
| `support` | Explicit `compatible` and `restart_verified` booleans, optional evidence text. False means abstention. |
| `useful_seconds` | Remaining useful computation, excluding checkpoint and restart overhead. |
| `checkpoint_seconds` | Elapsed checkpoint-write duration measured around complete atomic publication. |
| `restart_seconds` | Elapsed recovery duration measured until useful work resumes. |
| `failure_model` | Exposure observations or an explicit allocation MTBF range, described below. |
| `fixed_interval_seconds` | Comparison baseline; default 3,600 seconds, capped at the remaining useful duration. |
| `preemption_notice_seconds` | Optional estimated warning budget, evaluated separately from node failures. |
| `remaining_wall_seconds` | Optional remaining scheduler wall-time budget; not a stochastic failure rate. |
| `metadata` | Optional descriptive JSON, preserved in the input digest. |

Each cost accepts one of these forms:

```json
{"samples_seconds": [1.9, 2.1, 2.0], "source": "monotonic timer around atomic checkpoint publication"}
```

```json
{"lower_seconds": 1, "upper_seconds": 4, "source": "explicit estimate before measurement"}
```

The measured envelope is a sensitivity range, not a confidence interval. A
single sample cannot characterize variability. File sizes and modification
times never count as duration measurements. Include data transfer, flush and
publication work in the measured checkpoint operation; measure restart with the
same filesystem and application configuration.

## Failure exposure and censoring

The simplest explicit assumption is:

```json
{"allocation_mtbf": {"lower_seconds": 80000, "upper_seconds": 120000, "source": "site estimate for this allocation size"}}
```

Alternatively, provide independently identified attempts from the same cohort:

```json
{
  "stationary": true,
  "representative": true,
  "observations": [
    {"id": "cluster/77/submit-start", "exposure_seconds": 10000, "outcome": "completed"},
    {"id": "cluster/78/submit-start", "exposure_seconds": 3000, "outcome": "node_failure"},
    {"id": "cluster/79/submit-start", "exposure_seconds": 1000, "outcome": "preempted"}
  ]
}
```

Allowed outcomes are `node_failure`, `preempted`, `timeout`, `completed` and
`censored`. Exposure is allocation wall time at risk, not CPU seconds or
node-seconds. Duplicate attempt IDs are rejected. Preemptions, timeouts and
observed completion contribute exposure but are censored for the node-failure
model. This requires independent censoring: biased early termination or changing
site conditions can invalidate the estimate. Mark uncertain cohorts accordingly;
the advisor then abstains unless an explicit independent MTBF estimate is given.

Zero node failures yield a one-sided 95% upper hazard bound and abstention,
not infinite reliability. Nonzero counts yield Poisson exposure intervals;
large counts use a labelled quantile approximation. Sparse evidence remains a
wide interval. Preemption and deadline advice only checks whether a checkpoint
fits the declared warning budget; warning delivery and application hooks require
separate verification.

## Launch and recovery review

Attach a project-relative input to a launch:

```bash
raven launch job.sh --project "$PWD" --checkpoint-advice advice.json --check
```

For repeated use, set `launch.checkpoint_advice` in `.raven/automation.json`, or
use **Launch → Recordings → Advanced project reporting settings → Optional
checkpoint advice JSON**. Both CLI and graphical review show the same receipt.
A changed input requires a fresh review before submission. No checkpoint signal
or periodic schedule is inserted into the script.

The existing **Operations → Checkpoint recovery** manifest can contain a
`checkpoint_advice` object with this same input. Its recovery review adds the
advice while retaining checkpoint digest, runtime compatibility and owned-attempt
checks. Advice never proves that a checkpoint is valid or a restart is safe.

## Interpretation and limits

The central model is Young's `sqrt(2 × write_cost × allocation_MTBF)` useful-work
interval, adjusted to a finite number of equal segments. It compares expected
write overhead, lost useful work and restart cost with fixed-interval and
no-checkpoint baselines. Input extremes produce a sensitivity envelope; some
plausible scenarios may favor no periodic write.

This first-order approximation excludes interruptions during writes/restarts
and repeated loss of replayed work. Raven abstains when a plausible scenario
exceeds 0.1 interruption probability per segment. Reported savings are model
expectations, not measured site savings. Analytical and independent synthetic
simulation tests validate the implementation; live efficacy needs held-out
application runs and measured recovery costs on the intended cluster.


Checkpoint/restart durations, useful work, MTBF estimates, fixed intervals and exposure observations use seconds with a minimum accepted resolution of `1e-9` s and a maximum of `1e9` s. This numerical bound prevents underflow; it does not claim nanosecond measurement precision. Values outside those bounds are rejected before cost arithmetic.


A run that already publishes a `{"version": 1, "kind": "raven.planning"}` report can place the same complete advice document under its `checkpoint_advice` field. The Checkpoint advice view reads that explicitly declared input through the selected run's existing confined local/SSH report path. A generic planning bundle without the field shows an incomplete setup state; an explicitly supplied malformed advice document still reports its validation error.
