# Forecast request alternatives

Raven's Forecast view can compare explicit requests while preserving the original job. This is experimental observational analysis. A smaller request is not a promise of an earlier start, and shortening wall time can kill a workload.

Start with `examples/forecast-scenarios.json`:

```bash
raven run forecast --file examples/forecast-scenarios.json
```

Inside Raven, use `forecast --file PATH`; the existing Forecast workspace, retained source, background execution and export paths apply. Managed installations perform analysis in Heavy. The example deliberately contains no fabricated history, so Raven explains why it abstains.

## Input contract

The top-level schema is `raven.forecast-scenarios/v1`. Supply:

- `now`: observation epoch seconds.
- `request`: the current application name, partition, billing account, QOS, CPU/node/GPU counts, requested memory in bytes and wall time in seconds. GPU requests also require `gpu_type`.
- `alternatives`: at most eight unique `id`/`request` pairs. The application must stay the same. Optional `model_wait_seconds` and `scheduler_wait_seconds` are explicit scenario points, not outcome measurements.
- `context`: `policy_digest`, `reservations_visible` and `policy_complete`. Use an actual retained policy/configuration digest; unknown visibility must be false. Optional `queue_depth` and `idle_cpus` are descriptive evidence and are not fitted features in this model version.
- `observations`: at most 1,000 distinct completed attempts. Each requires `id`, the original `request` and `context`, `origin` (`recorded` or `synthetic`), `submit`, `issued_at`, `actual_start`, `actual_end`, and `state` (`completed` or `timeout`). Optional model/scheduler wait points must be the values captured at `issued_at`, before the actual start. Do not reconstruct predictions after completion.

All timestamps are finite epoch seconds. Request counts are concrete nonnegative integers; nodes and CPUs are positive. Observation identity must distinguish requeues and repeated numeric Slurm IDs. Preserve the measured provenance in optional `metadata`. Unknown action fields and ambiguous identities are rejected.

Optional controls: `coverage` (default 0.8), `wait_threshold_seconds` (300), and `maximum_overrun_probability` (0.1). Probability scores concern the event “starts within the threshold after the point was issued.”

An optional `queue_model` accepts the exact observable model described in [Queue model](queue-model.md), with one `target_job_id` and required `queue_model_issued_at` in that model's clock domain. The derived wait is predicted start minus this explicit forecast origin, not total time since submission. A target that the model already started before the origin is refused. Raven copies and changes only that target's declared resource request for each alternative. This path is limited to 256 queue jobs and homogeneous per-node requests. The original model is untouched. This inventory cannot represent a different partition or GPU type; those alternatives require their own independently observed scenario point without `queue_model`. A wall limit shorter than its declared application duration is refused. The result includes each model receipt. A model point alone cannot establish calibrated accuracy.

## What the result means

The new predictor calibrates queue-model residuals on comparable recorded attempts with the same complete resource request and observed policy/visibility context. At least 40 observations supply separate earlier fit and later calibration halves. Robust chronological drift triggers abstention. Out-of-distribution requests do not borrow unrelated workloads. Hidden reservations or incomplete policy prevent a preferred scenario.

Runtime risk uses comparable application/resource outcomes independently of requested wall time. It reports observed overrun frequency and a 95% Wilson upper bound. Unresolved timeout censoring, fewer than 20 observations or an upper bound above the declared tolerance prevent preference. No zero-failure sample becomes zero certain risk. Changes in scientific parameters must use a distinct application identity.

Chronological audits compare a simple cohort predictor, Raven's existing predictor, and the queue-residual model using only observations completed before each held-out prediction. Reports include mean/tail absolute error, prediction and interval coverage, Brier/log scores and their sample counts. At most the last 100 targets per origin are audited. Synthetic and recorded outcomes are never pooled, and synthetic samples never calibrate the recorded-data recommendation.

A preferred scenario is advice for review. Raven does not modify, resubmit or cancel a job. This model remains experimental until independently recorded site holdouts demonstrate useful calibration and acceptable runtime risk under that site's policies.


The [recorded qualification](../implementation/review-2026-10/evidence/W34.md) includes 65 real, preissued observations from an isolated Slurm fixture and 25 chronological held-out predictions. The residual model matched the simple cohort and did not improve the existing predictor. This negative result is retained; the feature remains experimental.
