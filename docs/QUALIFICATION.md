# Qualify a Raven installation

Use this procedure to create an evaluation record for a specific Raven release,
artifact, and environment. Offline checks validate local behavior. Site checks
record the supported scheduler, transport, hardware, and authorization scope.
See the [release validation record](raven/validation.md) for the published 7.0.0
results and remaining target-system checks.

## Prerequisites

Use a POSIX environment with Python 3.10 or later, curses, and virtual-environment
support. Run the checker from the source checkout. Distribution builds also
require the development tools documented in [CONTRIBUTING](../CONTRIBUTING.md).
Choose a new output path for each run; the checker preserves existing reports.
Use the development environment's Python interpreter for the commands below.
Live checks require an explicitly selected existing allocation and site permission.

## 1. Run the offline checks

From the checkout, run:

```bash
python3 scripts/qualify_flagship.py --output /tmp/tower-qualification.json
```

The script uses simulated data, a temporary configuration, disabled plugins and
state, ten plain-text pages, and a real local pseudo-terminal. It checks local
documentation links and the Report, Trace, and Runtime command help. Commands
have deadlines and output limits. The script submits no jobs and installs no
external packages.

The output is a `tower.qualification-report/v1` JSON document. Each check has one
status: `qualified`, `failed`, or `untested`. `qualified-for-recorded-checks`
applies only to the checks in the document. `production_qualified` remains false.
The report explicitly lists real GPU, multi-node, emulator, production SSH,
external-user, and scientific-outcome checks as untested.

The repository CI is configured to run this qualification after its regression
suite, generated-reference check, distribution build and installed-wheel smoke
check for Python 3.10–3.14. The Python 3.12 job uploads the JSON qualification
record with its distributions. Record hosted CI results separately from local
checker results.

## 2. Check a built wheel

Build with the optional development tools, then pass the actual wheel path:

```bash
python3 -m build
python3 scripts/qualify_flagship.py \
  --wheel dist/slurm_tower-7.0.0-py3-none-any.whl \
  --output /tmp/tower-wheel-qualification.json
```

Use the actual artifact path when its version differs from the example.
The checker creates a temporary environment and installs only that local wheel
with `--no-index --no-deps`. It runs the installed package outside the checkout
and records the wheel hash. This does not certify optional profiler tools or
their native libraries.

## 3. Check one existing allocation

Create a manifest from a disposable allocation that you already started through
your normal site procedure. Use its exact current Slurm timestamps as reported
by the scheduler; do not convert their time zone or add a UTC suffix:

```json
{
  "schema": "tower.site-qualification/v1",
  "cluster": "my-cluster",
  "job_id": "1234",
  "user": "my-user",
  "submit_time": "2026-10-10T12:00:00",
  "start_time": "2026-10-10T12:01:00"
}
```

```bash
python3 scripts/qualify_flagship.py \
  --live-manifest /path/to/allocation.json \
  --output /tmp/tower-site-qualification.json
```

This adds one read-only `scontrol` query. It verifies the job or array task, owner,
submission time and start time. A requeue, reused ID, or mismatched record fails
the check. The script does not start a step, sample a GPU, attach a debugger, or
change the allocation. The report records an identity hash, not the job's paths
or environment.

## 4. Record target-system evidence

Use a small allocation and site-approved tools. Record Raven and Slurm versions,
OS, Python, terminal and multiplexer, backend/tool versions, hardware and policy.
Keep credentials and private datasets out of the report.

| Area | Required evidence |
| --- | --- |
| Jobs and history | Running → completed/failed transition, accounting delay, array tasks and a requeue |
| Logs and artifacts | Multiple exact-job files, rotation, disappearance, permission refusal and remote paths |
| Input | Mouse and keyboard selections, graph zoom/reset, menus, resize and delayed releases in the actual terminal |
| Performance | Sustained mixed input, p95/p99 processing time, slowest frames, memory and source request counts |
| GPU | Exact allocated device/UUID, unsupported counters, failed probes and real job samples |
| Trace | Supported adapter, declared scope, gaps, budget exhaustion, clean close and an interrupted recording |
| Operations | Reviewed disposable mutation, readback, stale identity, repeat Apply and interrupted-result reconciliation |
| Runtime controls | Application safe point, parameter epoch, expiration, failed handler and cooperative multi-rank coordination |

Use [UI performance tools](guides/ui-performance.md) for measurements. Record
terminal transport separately from Raven's processing. For a target-system
evaluation, an initial acceptance target is p95 ≤33 ms and p99 ≤75 ms on a
declared baseline workload, with no processing stall above 150 ms in a
30-minute mixed-input run. These are proposed criteria; compare each measured
result with its declared target. The 7.0.0 local pointer fixtures use different
workloads, durations, and thresholds, documented in the
[validation record](raven/validation.md#local-pointer-response).

## 5. Evaluate user tasks and scientific outcomes

Collect independent lab/site observations before claiming production coverage.
Ask unfamiliar users to find a running job, diagnose a failed job, inspect
results, connect a project, and prepare a reviewed rerun. Record task completion,
errors, time, accessibility failures, and observer cost. Evaluate scientific
outcomes against a stated hypothesis, baseline, and acceptance rule.

## Preserve the evaluation record

Publish enough information to reproduce each recorded check:

| Record | Required contents |
| --- | --- |
| Artifact identity | Source commit, package version, wheel hash, and verified [release inventory](guides/release-inventory.md) |
| Environment | OS, Python, terminal chain, optional dependencies, hardware, Slurm version, and site profile |
| Procedure | Exact commands, fixture or dataset identity, durations, resource budgets, and acceptance criteria |
| Outcomes | Passed, failed, skipped, and untested checks; corrected failures and rerun scope |
| Evidence | Qualification JSON, retained benchmark reports, relevant logs, and declared exclusions |

Keep original failed results alongside follow-up results. Remove credentials and
private site or dataset information before sharing. Limit conclusions to the
behaviors and workloads that the record actually exercises.
