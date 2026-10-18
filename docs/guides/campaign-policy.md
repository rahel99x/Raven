# Campaign policy comparisons

Raven can compare submission policies for an already declared workflow or scaling campaign. This first milestone produces **reviewed shadow decisions**. It does not submit jobs automatically, modify priorities or override a site's limits.

## Connect an existing review

1. Open the existing workflow/scaling review using `orchestrate workflow FILE` or `orchestrate scaling FILE` inside Raven. Every script must explicitly declare nodes, tasks/CPUs, memory per node, wall time, billing account and QOS. The initial policy subset uses homogeneous per-node requests; normalize GRES/per-task GPU variants before comparison.
2. Prepare a [queue model](queue-model.md) containing exactly the reviewed node IDs and dependencies. Its requests must match the sealed script resources. Use measured or clearly labelled estimated durations. Preserve the actual site configuration provenance; a synthetic model is a scenario, not qualification.
3. Prepare limits JSON as shown below, using the limits and external usage you are entitled to observe. Missing limits stay incomplete.
4. Export the current immutable review without submitting it:

   ```text
   execution policy queue.json limits.json campaign-policy.json
   ```

   Raven refuses to overwrite an existing output. If an execution receipt is open, it is included unchanged.
5. Open **Operations → Campaign submission policy**, select the exported file and **Compare**. The same read-only result is available with:

   ```bash
   raven run ops run campaign-policy action=compare path=campaign-policy.json
   ```

Managed installations perform comparison in Heavy. Local plan controls remain bound to the interface's original connection and private state.

## Limits and exact format

```json
{
  "max_running": 4,
  "max_submit_jobs": 16,
  "grp_cpus": 32,
  "grp_memory_mb": 65536,
  "grp_gpus": 2,
  "commands_per_minute": 30,
  "external_jobs": 0,
  "external_cpus": 0,
  "external_memory_mb": 0,
  "external_gpus": 0,
  "complete": false,
  "starvation_seconds": 3600,
  "fairshare_delay_seconds": 60,
  "source": "Replace with the actual account/QOS policy observation and its timestamp"
}
```

`complete: false` prevents actionable shadow candidates. Zero external usage is a declaration, not automatic detection of other lab members' work. Do not set completeness true unless the relevant policy and usage are known. Memory is MiB; CPUs and GPUs are integer totals. Limits conservatively count all outstanding jobs' full requested resources, including pending jobs. This stricter admission envelope can reduce utilization compared with relying on Slurm's later allocation checks; the comparison reports that tradeoff.

The exported `raven.campaign-policy/v1` document contains `review` (unchanged `raven.execution-review/v1`), `queue_model` (`raven.queue-model/v1`), `limits`, optional original `receipt`, optional `policy` and descriptive `metadata`. IDs, dependencies and resource requests must agree. A model cannot add work outside the reviewed inventory. At most 64 nodes and one million combined placement operations are allowed.

## Policies and interpretation

- **FIFO:** review order, with head-of-line blocking.
- **Submit all:** admit ready work within the same conservative admission envelope, skipping requests that cannot currently fit.
- **Shortest:** estimated shortest jobs first, retaining head-of-line constraints.
- **Backfill friendly:** smaller wall-time × CPU requests first, filling feasible gaps.
- **Fairshare:** declared decayed usage per account weight, with bounded optional admission delay. This is not Slurm's full hierarchical fair-tree calculation.

Every policy respects afterok dependencies, declared limits and command spacing. A starvation-age threshold restores older-work precedence. The report shows makespan, throughput, wait tails, starvation, modeled wasted CPU time, command counts/rate, coverage and positive/negative differences from FIFO. Sensitivity reruns all policies with runtime factors 0.5 and 2; over-limit attempts can fail and cannot release their afterok descendants. These are simulated comparisons, not measured benefits on a production cluster.

## Pause, cancel and override

The same Operations form offers **Pause**, **Resume**, **Cancel** and **Override**. Each opens a review and uses Raven's normal Apply action. These controls modify only the local shadow plan; they do not stop running scheduler jobs. Paused/cancelled plans expose no candidates. Override selects one of the inspected policies. Cancellation requires a new immutable campaign review before continuation.

Controls persist under the scoped private state directory. A changed source, connection, control revision or stale review requires another review. Existing execution receipts remain authoritative: accepted work is never proposed again; an unknown/lost acknowledgment or rejected attempt blocks candidates until the established execution recovery workflow reconciles it. No alternate submission engine is introduced.

Actual scientific execution continues through the separately reviewed existing workflow. The shadow result does not automatically enforce future timing on that executor, and its commands must not be described as an autonomous campaign scheduler. Deployment of automatic shaping requires a separate qualification gate with fresh limits, owned-attempt validation and real chronological site outcomes.


Local pause, resume, cancel and override controls require a private local state directory with reliable locking and atomic replacement. Raven rejects symlinked or writable-by-other-users control paths. Retained controls remain readable on network storage, but changing them requires selecting local state and preparing a fresh review; a failed action is never silently retried.
