# Compare rank logs

Raven can compare the first disagreement in an explicitly identified set of rank
logs. This experimental view compares source order within role cohorts. It does
not decide which rank is correct or infer causal ordering from timestamps.

Connect the job to its exact run in Raven, and register each rank's log in the
run's existing log catalog. Give every attempt a unique run ID. Each log must
start with one complete JSON line declaring its rank, run ID, exact job ID, and
role cohort:

```json
{"raven_rank":{"rank":0,"attempt":"run-20261018-01","job_id":"1234","cohort":"workers"}}
```

For example, write this header once when each Python rank opens its own fresh
log. Set `RAVEN_RUN_ID` to the run Raven is connected to, and use an explicit
`RAVEN_RANK` supplied by your launcher:

```python
import json
import os

print(json.dumps({"raven_rank": {
    "rank": int(os.environ["RAVEN_RANK"]),
    "attempt": os.environ["RAVEN_RUN_ID"],
    "job_id": os.environ["SLURM_JOB_ID"],
    "cohort": "workers",
}}), flush=True)
```

Use a different cohort for coordinators, I/O servers, or other roles that
legitimately produce different messages. The header is producer-declared
association evidence, not authentication or proof that a launcher assigned the
rank correctly. Array jobs must declare their exact array-task job ID, matching
the connected run. Do not copy an old header into a new attempt's log.

Open Logs and press `O` to open the existing file catalog. Each source displays
its catalog ID in brackets. In Raven's command palette, name those IDs:

```text
:logdivergence rank0 rank1 rank2
```

The view loads through the existing background reader. It shows the last common
anchor, first disagreement, ties or a strict observed majority, and the original
rank lines. Select a line and press `o` to open its exact source; `Esc` returns
to the ordinary log view. Source changes or rotated files require a fresh
comparison. The existing refresh action clears the cached read for retry.

Comparison retains at most 16 ranks, 64 KiB and 240 rows per rank, and 8192
characters per line. An overlong line is reported, not silently shortened.
Truncated tails require a unique shared anchor; repeated text cannot identify a
unique event. A result then describes only the retained window. Missing logs,
stream endings and earlier omitted events remain unknown. Run identity changes
invalidate the displayed comparison. Source paths and rank selections are not
saved into another run's preferences.

Only timestamps and explicit matching rank prefixes are normalized. Numeric
values such as exit codes, iteration counts and error codes remain visible.
This conservative normalization may report harmless variable fields as a
difference; inspect the raw evidence before drawing a scientific conclusion.
