# Adversarial review implementation

The [source review](source-review.md) describes revision `b98742f`. Its claims
were checked against the current working tree rather than assumed to remain
true. The [coverage ledger](coverage.json) preserves every finding, its source
location, implementation, tests, adjustments, and qualification limits. Package
records retain the underlying evidence without adding their overlapping test
counts together.

The [evidence archive](evidence-archive.md) retains command captures and exact
qualification source payloads on an immutable evidence revision, separate from
the installed source tree.

## Changes and design decisions

Scheduler reads now count semantic records, bound accounting transfers, preserve
diagnostics and rejected-row identity, and distinguish temporary deferral from
failure backoff. Exact attempt proofs govern recording writes; visible historical
graphs do not authorize writes to a new attempt. Shared session traffic has
bounded queues and fair admission. SSH failures can fall back to an ordinary
connection without replaying an uncertain command.

Job actions preserve exact user-selected targets, require verified ownership,
record uncertain outcomes durably, and leave a recovery path. Heavy receipts
resolve controller identity even when a profile omits its cluster. Placement uses
verified cgroups or a fresh, bounded scheduler PID query; hostname similarity is
not placement evidence.

Terminal input has one foreground owner. Modal, drag, selection, cancellation,
and late-release behavior retain the original behavioral tests. Clipboard,
indexing, persistence, and analysis use existing worker lanes. Exports publish
only after complete private writes, without overwriting an existing destination.

Tracing callbacks enqueue bounded metadata; serialization and durable writes
run outside the application callback. Runtime archives are reusable and cleanup
requires exact connection, scheduler, and ownership evidence. CLI exports stream
the entire verified committed prefix within explicit traversal/output bounds.
Portable terminal and offline browser timelines show recorded stacks and CPU
placement observations; intervals between placement queries remain unknown.

Installation uses reviewed, pinned source exports, preserves configuration, and
restores recoverable source backups after pre-publication failure. Shared lab
configuration has an explicit revocable trust path. Bash 3.2/4.2 and actual
managed/pipx update/revert lifecycles are checked independently of mocked tests.

Scientific analysis retains cohort identity, refuses unsupported designs and
clock alignment, and reports bounded partial coverage. Greedy revalidation is
not claimed to be globally optimal. Statistical phase or prediction diagnostics
are not scientific ground truth.

## Measurements

Measurements use equivalent fixtures against `b98742f`; reports distinguish CPU
time, process maximum RSS, and separately traced Python allocations. Earlier
measurements identify their source snapshot and later changes. Controlled
windows exclude competing task tests/builds; external cloud activity is not
controlled. These are observations on this host, not performance guarantees.

| Fixture | Before | After | Tradeoff or limit |
| --- | ---: | ---: | --- |
| 4,000 artifacts/claims, 256 revalidation actions | 25.174 s | 0.895 s | Exact result digest preserved; greedy policy unchanged |
| 64 resource candidates, 10,000 history rows | 12.903 s | 3.696 s | Exact cohorts preserved; process RSS increases |
| Newest compact recording window | 5.176 s | 0.400 s | New result contains the correct newest window |
| 4,096 recording appends | 0.501 s | 0.121 s | 4,096 frames become 16 bounded batches |
| 35,000-row retention | 3.75 s | 7.31 s | Python peak 47.2 → 1.48 MB; process RSS 144 → 22.5 MB |
| 2,672-file source update | 5.336 s | 2.763 s | Same final payload digest; warm-cache local Git fixture |

See [analysis](measurements/analysis.json), [recordings](measurements/data.json),
[installation](measurements/installation.json), [terminal/UI](ui-log-terminal.md),
and [instrumentation](../injob-performance.json) for complete units, memory
costs, fidelity checks, commands, and environmental limits.

## Structural and deployment limits

The strict all-depth import graph now has no cycles and no unresolved runtime
imports. Actual subsystem implementations live in canonical packages, with
released import identities and saved feature keys preserved. Seven dependency
contracts and a mandatory global acyclicity gate enforce the boundaries. The
[architecture acceptance record](architecture-closure.md) documents the design,
compatibility checks, and deliberate deferred-loading decisions.

Real Slurm qualification uses disposable owned local containers, not a production
site. Physical GPU, multi-node transport, production SSH/shared filesystems,
unfamiliar-user usability, and scientific efficacy need their own qualification.
Simulated fixtures and local recordings do not establish those results.

Final integrated regression, built-artifact qualification, compatibility checks,
and hosted CI evidence are recorded in the coverage ledger by release integration.
