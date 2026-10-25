# Engineering platform implementation

## Baseline and intent

This work starts from `90a2e65894d65877bfc7890b0467afe163e10554` on
`codex/engineering-platform`. The baseline is the integrated navigation, mouse,
theme, and trace-discovery release, not the earlier planning snapshots. Other
worktrees and user configuration are preserved. This document is the coverage
ledger; a registered command or adapter is not evidence of live qualification.

The goal is a continuous research workflow: connect a site, qualify the actual
compute path, launch a reproducible attempt, inspect trustworthy measurements,
and review the next action. The normal interface should explain what is known,
what is missing, what an action costs, and how to recover. Advanced mechanisms
must earn their UI space by helping that workflow.

## Priority and dependency sequence

The accepted proposals retain their original identifiers. Implementation order
changes because optimization and recovery depend on stable identity, bounded
observations, and verified provenance.

| Priority | Accepted item | Complete implementation boundary | Acceptance evidence |
| --- | --- | --- | --- |
| 0 | Scheduler modularity | Native contracts, capability discovery, scoped identities, bounded reads, reviewed mutations for Slurm, Kubernetes, IBM Spectrum LSF, OpenPBS, PBS Professional, HTCondor, and Flux | Native-format fixtures; exact argv; stale ownership and attempt tests; capability reasons; installed commands |
| 1 | 1. Continuous experiment | Link real attempts across retries, arrays, sites, and checkpoint relationships; immutable provenance; resource totals include failures and preserve unknowns | Concurrent persistence, duplicate/foreign/stale attempt rejection, accounting fidelity |
| 2 | 8. Site qualification | Review a bounded compute test, then verify its exact attempt receipt for imports, filesystem, recordings, worker startup, and available counters | Fixture and local process integration; live site evidence only where actually obtained |
| 3 | 5. Reproducible execution | Preserve bounded scripts, code changes, effective resources, explicit environment/modules/seeds, and versioned input references; compare before rerun | Source-change, symlink, privacy, partial-publication, and comparison tests |
| 4 | 7. Lab workspace | Reusable multi-site profiles and explicit cross-site inventory; separate login, billing, scientific job, and Heavy resources | Account/site identity tests, bounded fan-out, persisted profiles and capability feedback |
| 5 | 9. Research provenance | Data, units, calculations, filters, exact attempt, coverage, source integrity, and explicit comparison compatibility | Round-trip exports, missing source/units, incompatible comparisons |
| 6 | 2. Distributed performance | Reuse retained topology traces and causal/collective analysis; distinguish measured execution, elapsed duration, waiting, gaps, and clock uncertainty | Existing trace regressions plus incomplete/multi-clock/distributed fixtures |
| 7 | 3. Incident recorder | Reuse triggered capture and retained windows; immutable incident bundles with scheduler/log/sample/trace evidence, explicit bounds and gaps | Trigger, cancellation, overflow, identity and retention tests |
| 8 | 4. Controlled optimization | Reviewed finite resource trials, repeated comparable observations, budgets, uncertainty, exact-once submissions, explicit cancellation/reconciliation | Resource and trial bounds; stale review; uncertain submission; measured comparisons |
| 9 | 6. Workflow recovery | Classify failure evidence; verify outputs and checkpoint lineage; review exact retry target, resources and charges; no blind retry | Node/preemption/timeout/application distinctions, changed artifacts, ownership and duplicate actions |
| 10 | 10. Reliability and simplicity | One task-oriented workbench; accessible keyboard/mouse controls; explicit freshness/limits; regression, packaging and performance gates | Shared UI integration, input bounds, scheduler request counts, controlled timing/RSS measurements |

Adaptive diagnosis joins items 2–4: choose a declared next measurement under a
reviewed overhead budget, separate observations from hypotheses, and suggest an
informative experiment. Its diagnostic efficacy remains experimental until
measured against independent real incidents.

## Shared design decisions

### Native schedulers without false equivalence

Adapters share identity, read-result, review, receipt, and resource contracts.
They use Raven's existing bounded transport rather than a shell command string.
The native source stays attached to normalized fields. Slurm CPU allocation
granularity, Kubernetes millicores and requests/limits, PBS select chunks, LSF
slots, HTCondor cluster/process IDs, and Flux jobs are not interchangeable.
Unsupported hold, accounting, topology, or telemetry features explain why they
are unavailable. Slurm remains compatible with its established launch and
Heavy-allocation paths. A generic adapter does not authorize a Heavy worker on
another scheduler or imply parity with every site extension.

Every mutation freezes its connection, exact native identity, owner, operation,
and reviewed inputs. Apply revalidates identity. An uncertain submission is
retained for reconciliation and never automatically replayed. Kubernetes
deletion uses an immutable UID precondition. Polling is scoped and bounded;
rendering and pointer motion never run scheduler commands.

The simpler alternative—convert every scheduler into Slurm command output—would
reuse more UI immediately, but lose native semantics and weaken action safety.
The chosen adapter boundary keeps existing Slurm paths and incrementally shares
presentation where semantics agree. Adapter conformance is independent of live
site qualification.

### Reuse and persistence

Existing `projects`, Run Home, launch receipts, evidence receipts, scientific
lineage, bounded trace indexes, causal/collective analyses, triggered capture,
workflow review, and measured-trial statistics are the starting points. New
services bridge these facilities; they do not introduce another trace format,
run directory convention, or scheduler replacement.

New local records live in an explicit project namespace with private,
owner-checked, atomic state. Read-only commands do not create state. Expected
revisions detect lost updates. Immutable outputs publish completion receipts
last; interrupted directories cannot be mistaken for complete evidence. Native
external attempts remain distinct from verified Raven run inventory entries.

### UI, accessibility, and developer experience

Use the existing task-oriented Operations workbench for advanced forms, reviews,
progress, cancellation and errors. Group related tasks by outcome rather than
adding ten toolbar buttons. Keep exact values and missing-data explanations
alongside visual cues. Mouse, keyboard, small-terminal geometry and saved
navigation must continue to use shared controls. New CLI entry points are
available through `raven`, `ravenl`, and `ravenh`; help and pure inspection must
not request a Heavy allocation.

Services have bounded typed JSON inputs and stable public functions, with no
module names or executable plugin hooks loaded from descriptive configuration.
Examples and command help must match installed behavior. A source-format fixture
test is described as such, never as production scheduler, GPU or remote testing.

### Scientific limits

Missing counters stay unknown, including missing failed-attempt accounting.
Allocated core-seconds and measured process CPU time are separate. Trace span
duration is elapsed time unless a source actually measures CPU execution or
waiting. Cross-host causal order requires clock evidence. Input references do
not certify dataset truth or copy large datasets. A hash detects changes, not
source authenticity. Repeated trials report their sample count and uncertainty;
simulations and sampled traces cannot supply unmeasured ground truth.

## Validation and delivery gates

Each package records a baseline, focused regression/integration commands, actual
results, skeptical review conclusions, and qualification limits below. Test
assertions must preserve behavior as UI structure changes. Controlled benchmark
runs are isolated from heavy suites. Timing units, CPU, I/O, request counts,
process RSS, Python allocations, coverage and environmental limits are reported
separately where affected.

The integrated revision requires the repository's full regression suite,
quality/import-layer checks, generated references, schema examples, source and
wheel builds, installed commands, extracted-source validation and compatibility
CI. Live scheduler qualification is a separate gate. Credentials, hardware and
site deployment not available in this host are recorded with exact unblock
conditions; mocked tests do not discharge those gates.

No publication to main is authorized by this implementation request. Delivery
is review-ready changes and, when available, a draft pull request. Existing
installation/configuration must remain usable; experimental capabilities are
identified in user-facing documentation.

## Package evidence

Implementation in progress. This section is updated with actual delivered
revision, commands, measurements and unresolved qualification dependencies.
