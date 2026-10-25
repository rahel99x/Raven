# Continuous experiments and execution capsules

A Raven run is one execution attempt. An experiment is the scientific question
that can continue through retries, array tasks, checkpoints, and different
sites. Linking attempts preserves their existing run identities and does not
submit work, move recordings, or rewrite a run inventory.

An execution capsule is a small, immutable package containing the recorded job
script and explicit context for reproducing it. It stores versioned references
to datasets rather than copying them. A capsule verifies retained bytes; it
does not certify the truth of a declaration or scientific output.

## Link existing runs

Create an experiment in the project that should own its index:

```bash
raven engineering experiments create \
  --project "$HOME/research/project" --id convergence-study \
  --title 'Convergence under mesh refinement'

raven engineering experiments link \
  --project "$HOME/research/project" --id convergence-study \
  --run launch-YOUR-EXISTING-RUN-a1

raven engineering experiments summarize \
  --project "$HOME/research/project" --id convergence-study
```

Replace the run identity with the exact identity from Run Home or the launch
receipt. The linked attempt retains its original project, scientific experiment
label, scheduler job, launch token, and restart count. Repeatedly linking the
same attempt refreshes its evidence instead of counting its resources twice.
A changed identity or stale revision produces a clear error without rebinding.

For the first Run Home connection, `experiments connect` combines creation and
linking in one reviewed, atomic edit. Supply the same `--project`, `--id`, and
`--run` arguments. Existing titles, native accounting, and retry/checkpoint
relationships are preserved. Repeated or concurrent connections do not add a
second copy of the attempt.

Advanced requests use JSON function arguments through `--request FILE`. To link
a retry with explicit accounting, use:

```json
{
  "project": "/home/alex/research/project",
  "experiment_id": "convergence-study",
  "run_id": "launch-EXISTING-RETRY-a1",
  "relation": "retry",
  "parent": "THE-EARLIER-LINKED-ATTEMPT-SHA256",
  "expected_revision": 2,
  "usage": {
    "scope": "allocation",
    "source": "Exact sacct allocation record for this attempt",
    "complete": true,
    "cpu_seconds": 431.2,
    "allocated_cpu_seconds": 960,
    "gpu_seconds": null
  }
}
```

```bash
raven engineering experiments link --request retry.json
```

Relations are `submission`, `retry`, `checkpoint`, `array`, `resume`, or
`recovery`. A parent must already exist in the same experiment. Linking an
attempt on another project filesystem uses an explicit verified binding from
that project. An external scheduler observation instead supplies its native
`scheduler_identity` and omits a Raven binding. It is labelled an external
observation, with unknown run state; Raven does not manufacture a run inventory
or claim the site is connected.

## Interpret resource totals

Failed and successful attempts both consume resources. Summaries include all
linked attempts and present each counter as a known subtotal, its covered
attempt count, and a completeness flag. Unknown CPU or GPU accounting stays
unknown, including accounting for failures.

`cpu_seconds` is measured CPU consumption. `allocated_cpu_seconds` is allocated
CPU capacity over time. The two are separate. GPU counters follow the same
distinction. `memory_byte_seconds` uses byte-seconds; all other counters use
seconds. Values must be finite and nonnegative.

Use `scope: "allocation"` for one authoritative allocation summary. Step
counters may be retained with `scope: "step"`, but summaries exclude them to
avoid adding both a job summary and its component steps. Do not declare an
array-parent aggregate as an additional disjoint allocation when its children
are already counted. If no authoritative allocation summary exists, retain
that uncertainty rather than supplying a fabricated total.

## Capture an execution capsule

For a quick package with explicitly unknown environment and input context:

```bash
raven engineering capsules capture \
  --project "$HOME/research/project" --run launch-YOUR-EXISTING-RUN-a1
```

The command prints the private package path. A fully declared request looks
like this:

```json
{
  "project": "/home/alex/research/project",
  "run_id": "launch-YOUR-EXISTING-RUN-a1",
  "environment": {"OMP_NUM_THREADS": "8"},
  "modules": ["python/3.13", "cuda/12.8"],
  "seeds": {"numpy": 13},
  "inputs": [
    {
      "name": "training-data",
      "uri": "s3://lab-data/training",
      "version": "2026-10-25-release"
    }
  ],
  "include_diff": false
}
```

```bash
raven engineering capsules capture --request capsule-request.json
```

Omitted declarations mean unknown. An explicit empty object or list declares
that no values in that category are required. Raven never dumps the ambient
environment or scans datasets. Credential-like environment names are rejected.
Scripts and optional patches can themselves contain confidential source; review
them before sharing.

The retained launch payload is preferred, so editing the original source after
submission does not destroy the captured execution identity. A legacy run can
use its declared script only while its bytes still match the recorded SHA-256.
Missing original bytes are reported rather than replaced with today's script.

Git revision context is labelled **capture-time checkout state**, separately
from any recorded execution commit. Capture does not run `git diff`: even a diff
with external diffs and text conversions disabled can execute a repository's
configured clean/process filters. Current working-tree changes therefore stay
unknown unless a patch is supplied explicitly.

Create a patch through your reviewed source-control workflow, then add
`"patch_path": "/absolute/path/to/reviewed.patch"` to the request. The patch is
bounded to 1 MiB. `include_diff: true` retains its bytes; otherwise only its
digest and omission are recorded. Correspondence between a supplied patch and
the actual execution remains an explicit unverified declaration. Untracked
files and datasets are not scanned or copied. Patches are never applied
automatically.

## Verify, compare, and prepare a rerun

Use a request containing `{"path": "/absolute/path/to/capsule"}` with:

```bash
raven engineering capsules verify --request verify.json
raven engineering capsules relaunch_draft --request verify.json
```

Preparing a rerun returns the existing Launch job workflow's editable draft,
equivalent command, changed project/script locations and effective options,
unknown context, and declarations to review. It submits no work. Keep the
capsule inside the selected project; copy an externally received package into
that project explicitly before preparing its rerun. An optional `project`
argument selects the target project.

Modules, seeds, inputs, and environment declarations are comparison context.
Application setup stays visible in the retained script; Raven does not execute
metadata as hidden shell code. Use the ordinary launch review before submitting.
The current hardware, external libraries, input bytes, and scientific output
remain unverified.

Compare two retained packages with:

```json
{
  "left": "/absolute/path/to/first-capsule",
  "right": "/absolute/path/to/second-capsule"
}
```

```bash
raven engineering capsules compare --request compare.json
```

The result names each changed setting and missing declaration. Matching retained
settings do not establish identical data, process state, hardware, or results.

## Persistence and limits

Experiment indexes live under
`PROJECT/.raven/engineering/experiments/`. Default capsule exports live under
`PROJECT/.raven/engineering/capsules/`. Namespace directories are user-owned
and private; files are private. Local readers reject symbolic links, special
files, unexpected ownership, oversized JSON, and files replaced during reading.
Read-only commands create no state. Updates serialize through a bounded lock
and publish atomically. A stale expected revision requires reopening the review.

Experiment inventories have at most 4,096 linked attempts and a 2 MiB JSON/item
budget; the first reached bound applies. Capsules contain one script, an
optional tracked patch, and a manifest. Script and patch bytes are each bounded
to 1 MiB. Publication is exclusive, with a completion receipt written last. A
partial package cannot verify as complete; a synchronous failure removes the
newly created export. Existing exports are never overwritten.

These are local evidence and persistence facilities. Native-format tests do
not qualify remote storage, production schedulers, physical GPU accounting, or
scientific reproducibility. Site qualification remains a separate step.
