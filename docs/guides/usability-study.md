# Prepare and run the G5 usability study

The existing [qualification protocol](../QUALIFICATION.md#5-evaluate-user-tasks-and-scientific-outcomes)
defines five journeys: find a running job, diagnose a failed job, inspect results,
connect a project, and prepare a reviewed rerun. This guide prepares those tasks;
scripted checks are not evidence of human usability.

## Prepare a private demo session

```bash
python3 scripts/prepare_usability_study.py --output /tmp/raven-study-session
raven --fake --no-state --no-plugins
```

The preparation command requires a new directory. It copies the existing
project template and runs its small CPU experiment to produce real example
measurements. It also writes task cards, an empty observation sheet and a
revision/hash manifest. It submits no Slurm job, records no GPU data and fills
in no participant answers. Use a separate directory for each session.

The demo backend supplies synthetic scheduler jobs for the first task. The
preparation also executes two tiny local shell programs (one successful and one
deliberately exiting 7), retains their real launch receipts/logs, opens their Run
home data and verifies the rerun review without submitting the review. Use the
generated project for connecting, failure diagnosis and inspecting results.
Record this automated work as fixture preparation, not participant success.
The participant stops at the final review; do not charge a cluster account in
the demo study. If a task's exact fixture is unavailable, mark it untested.

Use an ordinary 80-column terminal and keyboard access for a first pass; repeat
affected tasks with the participant's normal terminal, mouse and accessibility
settings. Do not reveal the navigation solution on the task card. Keep the
[everyday workflow](everyday-workflow.md) available and count each lookup.

## Observe five consenting participants

Recruitment and communication require separate authorization. Explain what
will be recorded, allow withdrawal, use pseudonyms, and collect no credentials,
private research data or terminal recording without explicit consent.

For each task, record completion, elapsed seconds, unnecessary decisions,
documentation lookups, errors/recovery, accessibility problems, and whether the
participant can identify the charged account and requested resources. Record
observer assistance separately: assisted completion is not unaided success.
An unsuccessful task has a reason, not an invented elapsed success time.

The existing G5 target is at least 80% unaided task completion. With five users,
report each task and the count out of five; this small study cannot establish
population-wide statistical claims. Rank observed friction by severity and
frequency, fix substantive issues, and rerun the affected task with fresh
participants or clearly label practice effects.

## Qualify the real-account phase separately

After demo sessions and site authorization, repeat tasks on the participant's
workstation Slurm or lab account at CARC, RCIC, or another selected site. Record
the site's Slurm version, account policy and allocation budget. Explain login
identity versus the billing account before an actual submit confirmation.
Retain separate demo and real-site results. Neither local automation nor a
single-node Slurm container establishes this human or site gate.

Preserve the reviewed revision, wheel digest, fixture manifest, anonymized
observations, assistance and failures. Publish only the consented record.
