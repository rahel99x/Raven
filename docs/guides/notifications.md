# Notifications

[README](../../README.md) · [Controls](../CONTROLS.md) · [Setup](setup.md)

Raven retains errors and warnings in a notification panel. When notifications
exist, the strip below the page tabs shows a separate count for each severity.
Click a count to open that list, or enter `:notifications` to see all levels.

| Level | Colour | Meaning |
| --- | --- | --- |
| Critical | Red | An operation reported a failure that needs attention. |
| Moderate | Orange | A warning or collection problem may limit the available results. |
| Mild | Yellow | A reported temporary deferral or unavailable observation explains missing data. |

The labels accompany the alert symbols so that severity remains identifiable
without colour. ASCII mode uses `!` for the alert symbol.

## Read and investigate a message

The panel wraps messages to the available width. Scroll to read the complete
retained text, including additional lines and the source or job context. Select
a notification and press Enter, or click it, to open its details.

The detail view distinguishes the reported issue from possible causes and
suggested fixes. Guidance covers collection budgets, connection failures, Slurm
account associations, allocation verification, GPU sources, and configuration
errors. An unfamiliar message is retained with an explicit statement that its
cause has not been diagnosed. Reading these suggestions does not run a command
or change scheduler settings.

Examples:

```text
:notifications critical
:notifications moderate
:notifications mild
:notifications all
```

## Dismiss and revisit

Use the `×` dismissal button (`x` in ASCII mode) or Delete to dismiss a selected
notification. Repeated observations of the same source failure keep the
dismissal in effect. If the source recovers and the problem later recurs, Raven
can show it again. A new failed operation can also produce a new notification.

Notification state belongs to the current connection and session. The existing
Activity view remains available through `:activity` for operation receipts,
exports, and its separately configured history.

## Retention and cost

The panel retains at most 256 notifications and 1 MiB of message text. A single
message is limited to 64 KiB; the panel identifies messages that exceed this
limit. Normal messages are wrapped rather than shortened to one terminal row.
Repeated notifications share a retained entry and show their occurrence count.

Noninteractive `--once` output includes a wrapped notification summary so that
full retained messages are available without clicking a panel control.

Raven sanitizes messages when they arrive and caches notification snapshots.
Source notifications use already collected observations. Opening, scrolling,
and dismissing notifications do not issue additional scheduler queries.
