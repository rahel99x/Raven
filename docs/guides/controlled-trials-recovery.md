# Resource experiments and workflow recovery

Raven separates resource planning, native scheduler review, execution receipts,
and measured results. Preparing a resource study does not submit jobs. Applying
a trial submits one job, with an explicit account and a durable receipt. A retry
uses the same review boundary and always creates a new charged submission.

These workflows extend the existing factorial-design, diagnostic-campaign,
checkpoint-campaign, workflow, and statistics libraries. They use the same
native scheduler connections as the [scheduler workbench](native-schedulers.md).

## Choose a small resource study

Start with two resource variants and three independent repetitions. Keep the
script, account, placement policy, input references, and scientific meaning
constant. Specify how to verify equivalent scientific output before spending
resources. A fixed random seed makes the trial order reproducible.

The following Python snippet prepares a request file. Replace the script and
account with values valid at your site. `job.sh` is a Bash/POSIX-shell script
without scheduler directives; its resources belong in the variants. Ordinary
Raven launches continue to preserve `#SBATCH`, `#PBS`, and `#BSUB` directives.

```python
import getpass
import json
from pathlib import Path

request = {
    "scheduler": {"name": "slurm", "scope": {
        "cluster": "my-cluster", "user": getpass.getuser(), "account": "my-lab"
    }},
    "arguments": {"document": {
        "schema": "raven.resource-experiment/v1", "id": "solver-cpus",
        "seed": 17, "repetitions": 3,
        "variants": [
            {"id": "two", "submission": {
                "script": str(Path("job.sh").resolve()), "account": "my-lab",
                "cpus": 2, "nodes": 1, "memory_mib": 1024, "wall_seconds": 120
            }},
            {"id": "four", "submission": {
                "script": str(Path("job.sh").resolve()), "account": "my-lab",
                "cpus": 4, "nodes": 1, "memory_mib": 1024, "wall_seconds": 120
            }}
        ],
        "budget": {"max_jobs": 6, "max_concurrent": 1,
                   "cpu_seconds": 2160, "gpu_seconds": 0,
                   "reservation_seconds": 720},
        "equivalence": {"semantic": "final energy", "unit": "J",
                        "reference": -17.0, "absolute": 0.001, "relative": 0}
    }}
}
Path("study-request.json").write_text(json.dumps(request, indent=2) + "\n")
```

```bash
raven engineering optimization prepare \
  --request study-request.json --output study-plan.json
```

Read the plan before applying a trial. It lists every repetition, requested
resources, scientific checks, source hashes, and a digest. A full design that
exceeds any budget is rejected rather than silently truncated. CPU/GPU seconds
describe total requested CPU/GPU slots multiplied by requested duration; node
reservation seconds include the requested node count. They are not measured
consumption, site billing, or a guarantee against site allocation rounding.

Native plans expire after five minutes. Reprepare the same study to refresh its
review without resetting its journal. Changing code, scope, inputs, account, or
the design requires a new study ID. Already claimed repetitions cannot run
again through a refreshed plan.

To apply one reviewed trial, create an arguments envelope containing the saved
plan, its digest, the project root, and the same scheduler connection:

```python
import json
from pathlib import Path

request = json.loads(Path("study-request.json").read_text())
plan = json.loads(Path("study-plan.json").read_text())
request["arguments"] = {
    "plan": plan, "review_digest": plan["digest"],
    "project": str(Path.cwd())
}
Path("trial-request.json").write_text(json.dumps(request, indent=2) + "\n")
```

```bash
raven engineering optimization apply --request trial-request.json
```

An optional `trial_id` selects one planned repetition. Omitting it selects the
next unclaimed trial in the randomized schedule. No background loop submits
the remaining jobs. The maximum concurrency includes unresolved submissions.

## Reconcile and cancel

State is private to the project at
`.raven/engineering/optimization/STUDY_ID.json`. A durable `issuing` claim is
written before contacting the scheduler. A timeout, interruption, or ambiguous
submission remains `unknown`; it retains its concurrency reservation. An error
message is preserved with the receipt.

Use `optimization status` with arguments `project` and `experiment_id` to inspect
the journal. For `optimization reconcile`, supply those arguments and the same
scheduler connection. Native-ID-only receipts require an explicit `bindings`
object mapping trial IDs to exact identities from `raven scheduler jobs` or
`history`. This is an operator assertion about the original submission, checked
against current native identity and ownership. A recycled job number alone
never releases a reservation. Missing history remains unresolved.

`optimization review_cancel` accepts `project`, `experiment_id`, and `trial_id`.
It produces a separate native cancellation plan. `optimization cancel` accepts
that `plan`, its `review_digest`, and the same three identifying arguments.
Cancellation does not immediately release a reservation; reconcile the verified
terminal attempt. Deleting a journal does not establish that a job never ran.

## Compare measured results

Use `optimization analyze` with a `document` containing:

```json
{
  "schema": "raven.resource-experiment-results/v1",
  "plan": "REPLACE_WITH_THE_COMPLETE_SAVED_PLAN_OBJECT",
  "observations": []
}
```

Each observation contains the following fields. Unknown counters use JSON
`null`; failed jobs can have `output: null`.

| Field | Meaning |
| --- | --- |
| `trial_id` | Exact ID from the plan. |
| `identity` | Complete native scheduler identity, including attempt and scope. |
| `outcome` | `completed`, `scientific_failed`, `infrastructure_failed`, or `cancelled`. |
| `runtime_seconds` | Measured elapsed execution duration. |
| `queue_seconds` | Measured queue delay, kept separate from runtime. |
| `cpu_seconds`, `gpu_seconds` | Recorded consumption with the source's meaning; unavailable stays `null`. |
| `output` | `{semantic, unit, value, sha256}` for validated scientific output, or `null`. |
| `controls` | Optional `{identity, submission_sha256, evidence}` proving which effective request was checked. |

For comparable estimates, `controls.identity` must match the observation's
attempt. Verify its effective native request after site admission. Set
`controls.submission_sha256` using
`raven.engineering.optimization.submission_digest(trial["submission"])` only
after verifying that request. Explain the verification source in `evidence`.
The hash binds request bytes; it does not attest that hardware controls took
effect. A missing or mismatched control record excludes that observation from
comparable estimates rather than assuming the requested allocation occurred.

Raven reports separate runtime, queue, CPU, and GPU comparisons. Failed,
incomparable, and missing trials remain visible. Accounting summaries include
known counters from failed attempts and explicitly count unknown values.
Welch intervals reuse Raven's existing independent-unit statistics. Reusing one
scheduler attempt as multiple repetitions is rejected. Historical results remain
readable after the original script is removed.

Intervals are exploratory: interference, drift, unverified controls, and multiple
comparisons limit conclusions. A lower observed runtime is not proof of a
causal improvement or a globally optimal allocation. No automatic resource
recommendation or production deployment is claimed.

## Recover a failed attempt

`recovery prepare` accepts a `document` with:

```json
{
  "schema": "raven.workflow-recovery/v1",
  "id": "solver-retry-1",
  "source": {
    "identity": "REPLACE_WITH_THE_EXACT_SCHEDULER_IDENTITY_OBJECT",
    "owner": "alice",
    "state": "NODE_FAIL"
  },
  "failure": {
    "kind": "node_failure",
    "evidence": "Exact retained accounting record reports NODE_FAIL"
  },
  "submission": {
    "script": "/shared/project/restart.sh",
    "account": "my-lab", "cpus": 2,
    "memory_mib": 1024, "wall_seconds": 120
  },
  "mode": "restart"
}
```

Supply the scheduler envelope as in the resource example. Source state is the
native final state, not a guessed root cause. Failure kinds are `node_failure`,
`timeout`, `preemption`, `out_of_memory`, `application_failure`, and `cancelled`.
Timeout, OOM, general failure, and cancellation also require a printable
`correction` explaining the reviewed application, resources, or policy change.
A generic scheduler failure does not prove an application defect.

`recovery apply` accepts the reviewed `plan`, `review_digest`, and `project`.
It checks the source's current exact identity, owner, terminal state, script,
and retained artifacts again. An already issued recovery cannot be replayed.
`recovery status` accepts `project` and `recovery_id`. Unknown outcomes require
native reconciliation before considering a separately reviewed new attempt.

## Resume from a verified checkpoint

Choose `mode: "resume"`, supply `project` to preparation, and add:

```json
{
  "checkpoint": {
    "path": "/shared/project/state.ckpt",
    "sha256": "REPLACE_WITH_THE_64_CHARACTER_SHA256",
    "complete": true,
    "source_identity": "REPLACE_WITH_THE_EXACT_SOURCE_IDENTITY_OBJECT",
    "validation": {
      "passed": true,
      "method": "Application schema and scientific consistency check",
      "evidence": "Explicit validator report or retained artifact reference"
    },
    "compatibility": {
      "expected": {"application": "solver-v1", "format": "state-v2", "runtime": "python3"},
      "actual": {"application": "solver-v1", "format": "state-v2", "runtime": "python3"}
    }
  }
}
```

The script explicitly reads `RAVEN_CHECKPOINT`; Raven does not guess a restart
flag. Preparation stages a private content-addressed wrapper, preserving the
original script and its leading scheduler directives. The wrapper exports
`RAVEN_CHECKPOINT` and `RAVEN_CHECKPOINT_SHA256` and verifies stable, regular,
non-symlink checkpoint bytes on compute before starting the application.
The compute-side guard accepts checkpoints up to 1 GiB. This small local review
path has an aggregate **8 MiB / 1 second inspection budget**, including repeated
verification reads. Larger artifacts are rejected before hashing; use Raven's
existing Heavy `checkpoint` campaign for admitted large-checkpoint verification
and guarded restart. This is an explicit implementation limit of this local
workflow, not a missing credential. Application/format/runtime compatibility must
match, and a hash alone cannot replace scientific validation.

Optional reusable `outputs` use the same path/hash/completion/source/validation
fields, without checkpoint compatibility. A `workflow` object can declare nodes
with `id`, `dependencies`, `state`, and `outputs_validated`. The resulting DAG
report reuses only validated completed nodes whose dependencies remain valid;
changed upstream work invalidates downstream reuse. Active jobs retain their
native ownership and block duplicate work. Native dependencies are never
silently removed or replaced by this analysis.

## Platform and qualification limits

Bounded resource trials require an adapter capable of explicitly expressing all
reviewed resources. Current native limitations are returned before submission:
LSF does not have a universal memory override, Flux has no portable memory
request override, and HTCondor has no universal enforced wall-time override and
may create multiple processes from one submit file. Those adapters remain usable
for their supported scheduler operations; Raven does not present these missing
controls as qualified bounded experiments. Kubernetes trials require explicit
CPU/memory limits, digest-pinned images, `activeDeadlineSeconds`,
`restartPolicy: Never`, and `backoffLimit: 0`. Init-container and sidecar limits
are included conservatively. Site admission mutations and infrastructure
replacement remain qualification limits, not a guaranteed billing ceiling.

Checkpoint resume currently uses the shared-filesystem Bash/POSIX-shell path.
Kubernetes restart can use a reviewed Job manifest; application-specific mounted
checkpoint resume requires a dedicated adapter. Native resource units remain
visible. Multi-node filesystem visibility, application checkpoint efficacy,
production account policy, and physical GPU counters require live site evidence.
Fixture and subprocess tests do not establish those qualifications.
