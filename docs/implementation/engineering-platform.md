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

The user subsequently authorized merging to main after completion. Delivery
requires a reviewed pull request and passing final checks before that merge. Existing
installation/configuration must remain usable; experimental capabilities are
identified in user-facing documentation.

## Package evidence

The foundation revision is `1f1e7acb681bc3ea2904c6ad56f9e66f8e561f9c`.
Compatibility CI then exposed a shared publication-receipt permission bug under
umask `022`. The final source creates private metadata explicitly and closes its
raw descriptor if stream initialization fails. The latest native qualification
records all 712 final source/harness hashes. [Pull request #10](https://github.com/rahel99x/Raven/pull/10)
carries the final integrated checks; use its final head rather than a superseded run.

| Accepted requirement | Delivered path and evidence | Qualification boundary |
| --- | --- | --- |
| 1. Continuous experiment | `engineering.experiments`: atomic first-use connect, immutable seven-part retained identity, native association checks, cross-site attempt links, ancestry, state refresh, failed-attempt allocation totals and explicit accounting coverage. Run Home opens the same verified run in the lineage form. | A recorded association is not scheduler attestation. Step counters do not inflate allocation totals; missing usage stays unknown. |
| 2. Distributed performance | `engineering.performance` composes the existing indexed workspace reader, function statistics, explicit flows, scheduling/device unions, counters and causal slice. CLI and workbench use admitted compute for analysis. | Call duration is elapsed, not measured CPU execution. Explicit source clocks and uncertainty remain separate. No invented occupancy, wait time or hardware ground truth. |
| 3. Incident recorder | `engineering.incidents` publishes/reopens immutable private evidence bundles with exact identity, selected logs, samples, source errors, byte/event bounds and explicit omissions. Cooperative arming reuses `TriggeredCapture`. | An application or existing trigger must declare the observed condition. No automatic universal OOM/watchdog capture or retroactive recovery of missing events is claimed. Runtime overhead percentages remain unqualified. |
| 4. Resource experiments | `engineering.optimization` plans repeated variants under whole-design requested-resource budgets, refuses opaque native directives, randomizes repeats, journals exact-once issuance, reconciles real receipts, and preserves failures, missing measurements and uncertainty. | Requested resource bounds are not a guarantee of billing, site rounding, application behavior or optimality. Unsupported native resource controls disable the affected study. |
| 5. Reproducible execution | `engineering.capsules` retains verified scripts, effective options, code revision, explicit modules/environment/seeds and versioned input references. Verify, compare and rerun drafts reuse the ordinary launch review. | Current Git state does not prove past execution. Patch contents require an explicit file; ambient environments and datasets are never copied. Declarations are not silently executed. |
| 6. Workflow recovery | `engineering.recovery` separates node failure, preemption, timeout, OOM, cancellation and application failure; verifies output/checkpoint evidence; prepares compute-side guards and reviewed retries. Exact owner, native action, resources and script hashes are revalidated. | Scientific compatibility needs application validation. Local inspection is bounded to 8 MiB/1 second; larger sources require the existing admitted Heavy checkpoint workflow. A new universal large-checkpoint orchestrator is not claimed. |
| 7. Lab workspace | `engineering.labs` saves private compare-and-swap site profiles with separate login, billing, job and Heavy settings. Explicit selected-site inventory opens/closes bounded native clients and keeps per-site errors. | Association declarations do not grant spending authority. Native account balances and association discovery vary by scheduler; absent sources remain unknown. |
| 8. Site qualification | `engineering.qualification` prepares a reviewed tiny compute script, records a bounded native attempt readback and validates current owner/identity at collection. Imports, scratch, metrics, tracing, worker placement and process counters have independent outcomes. | Five actual disposable Slurm versions qualified; production access, cross-node filesystems, GPUs and platform enforcement remain separate. A placement response is not full worker ownership verification. |
| 9. Research provenance | Existing immutable chart exports retain CSV, units, frozen source hashes/watermarks, filters and exact legacy attempts. `engineering.provenance` adds typed derived results, immutable export companions, exact integer/decimal filtering and compatibility checks. | Legacy exports retain their original identity; an optional recorded experiment bridge does not become independent native attestation. Automatic attachment of the new native companion to every old chart is a future integration, not required to read existing export provenance. |
| 10. Reliability and simplicity | The shared Operations workbench, existing mouse/keyboard controls, contextual Run Home actions and installed CLI expose these tasks without ten new navigation panes. `engineering.reliability` reports observed budgets; incomplete or unknown results cannot pass a release gate. | Targets require real measurements. Process RSS and Python allocations are different metrics; terminal emulator, SSH and user-study qualification remain external. |
| Scheduler modularity | Fixed audited adapters cover Slurm, Kubernetes, IBM Spectrum LSF, OpenPBS, PBS Professional, HTCondor and Flux. Native identity, quantities, read bounds, reviewed mutation, private claims/receipts and failure recovery are shared. | The full established dashboard/Heavy allocation path remains Slurm/explicit standalone. Native adapters provide an explicit workbench and CLI, not fabricated Slurm telemetry or full feature parity. Non-Slurm Heavy/recording ownership needs additional implementation and qualification. Oracle is excluded. |

### Baselines, alternatives and maintainer review

The baseline 261 selected existing workflow/platform tests passed before the new
integration. Existing trace indexes, fragmentation tests, selector brokers,
bounded transport, launch review, run inventories and evidence exports were
retained. Focused packages and independent integration tests cover real local
subprocesses and source-format fixtures; optional actual-client testing is
identified separately.

The smaller alternative for experiments was another run store; an atomic index
over verified existing inventories avoids duplicated ownership. The simpler
reproducibility path was an automatic worktree diff, but a malicious Git clean
filter reproduced code execution during metadata capture. Revision-only capture
plus an explicit patch file preserves a bounded, inspectable trust boundary.
The simpler native UI path was a Slurm-shaped facade; an explicit native
workbench preserves unsupported capabilities and avoids accidental Slurm actions.
For incident recording, reusing existing pre/post retention avoids a second
capture lifecycle. For resource studies and recovery, reusing the native reviewed
plan and immutable source snapshots keeps uncertainty and exact-once state in
one place.

Independent review reproduced and resolved: write-only Kubernetes scope
retargeting; unrelated submission replies; malformed native fields; optional
counter failures hiding otherwise valid jobs; real receipt scope mismatch;
redirected nested actions/accounts; cross-host script mismatch; recycled compute
attempts; foreign owners; intermediate-symlink result writes; large-integer
rounding; unit relabeling; first-use lineage navigation; invalid JSON argument
crashes; and incomplete/uncertain outcomes appearing successful. Assertions were
added for the positive paths as well as rejection cases. Native-format behavior
was checked against vendor source where fixtures were insufficient.

### Reproducible checks and evidence

Focused development checks culminated in 349 passed/one optional-client skip
before the last independent bridge/outcome regressions. The first broad local
integration run passed **21,213 tests**, with 54 optional skips and four existing
fork warnings. Affected final outcome/UI regressions passed separately. Final
frozen-source regression and Python 3.10–3.14 CI results are attached to PR #10;
the final PR head is the delivery gate, not these intermediate counts.

The first compatibility run failed the same two incident-privacy tests on all
five Python versions: the shared publisher's default-mode receipt conflicted
with the reader's strict private-file invariant. The original reader checks and
test assertions remain intact. Explicit `0600` metadata creation, six isolated
umask/failure-injection regressions, and 294 shared-file/campaign regressions
cover the correction. The final CI run must pass; the earlier failures are
preserved as evidence, not relabeled as qualification.

The next compatibility run passed the privacy assertions on every version but
exposed a concurrency-test assumption on Python 3.10: a client performing 16
durable updates in four threads did not handle the storage API's documented
250 ms busy rejection. The final test client retries only that exact rejection
within a bounded test deadline. Production admission timing is unchanged. All
16 identities, revision 17 and every accepted revision remain asserted; a new
held-lock regression proves rejected writes change nothing and an explicit retry
commits exactly once. Independent review and Python 3.10/3.13 focused checks
passed. This is a qualification correction, not a claim that busy writes always
succeed immediately or that native mutations may be replayed.

```bash
python -m pytest -q -n 2
python scripts/check_quality.py
python scripts/check_import_layers.py --require-acyclic
python scripts/generate_operation_reference.py --check
python scripts/generate_capability_reference.py --check
python scripts/validate_operation_schemas.py
python -m build
python scripts/qualify_flagship.py --wheel dist/raven-7.0.0-py3-none-any.whl --output NEW_REPORT.json
```

Quality checks retain seven architectural contracts, zero import cycles and
zero unresolved dynamic module expressions. The schema audit validates 82
schemas and 43 registered examples. Source/wheel builds and installed CLI,
documentation and private-PTY qualification passed. CI also checks extracted
source, installed commands, browser interaction, Bash compatibility, updates,
private-group umasks and distribution extraction.

Actual disposable Slurm qualification passed **21 checks on each of five
versions** (23.02.8, 23.11.11, 24.05.8, 24.11.7, 26.05.5): **105 checks** and six
compute checks per version. Every run matched all 712 recorded source/harness hashes.
See [the concise native evidence](../benchmarks/engineering-native-slurm-evidence.json)
and [reproduction guide](../guides/native-slurm-qualification.md). Initial
failures exposed a real `CLUSTER:` JSON prefix and harness CWD/accounting-lag
assumptions; the preserved failures are not counted as successes.

An official checksum-verified `kubectl` 1.32.0 passed 11 real-client checks
against an isolated HTTP API, including UID-conditioned deletion, replacement
protection and consumed-token rejection. Raw scoped reads reduced that equivalent
fixture from **38 to 10 HTTP requests**. This tests the client protocol, not a
live Kubernetes scheduler, RBAC or admission deployment. The pinned client wire
check now runs in CI.

Controlled benchmark artifacts and commands live in `docs/benchmarks` and the
`scripts/benchmark_*` harnesses. They report CPU/time and I/O separately from
Python allocations and process high-water RSS. New service envelopes are
compared with both their initial composition and their smaller existing
building blocks; a composition speedup does not imply the old parser was slower.

| Controlled fixture | Before / reference | Delivered measured path | Interpretation |
| --- | --- | --- | --- |
| Jobs render, 100 jobs/500 history, 80×30, 40 frames | 4.106 ms median; 4.220 ms p95 | 4.143 ms; 4.304 ms | About 0.9% median growth; terminal/SSH excluded; zero source I/O. |
| Hover, same inventory, 150 events | 0.260 ms median; 0.321 ms p95 | 0.263 ms; 0.324 ms | About 1% median growth; no composition, graph publication or source I/O. |
| Investigation, 2,000 events, 30 repeats | Initial equivalent composition 57.963 ms; smaller existing analyses 20.870 ms | 41.311 ms median; 44.666 ms p95 | Removing duplicate serialization improves the initial composition about 28.7%; identity/hash/coverage work still costs more than the smaller reference. |
| Native Slurm inventory, 500 records | Full-native-copy candidate 230.380 ms; bare existing normalizer 21.892 ms | 57.859 ms; 864,798 retained Python bytes | About 74.9% less time and 90.5% fewer Python retained bytes than the rejected candidate. The richer envelope is 2.64× the unchanged bare normalizer cost. |
| Eight variants sharing a 64 KiB script | 24 opens; 1,572,648 bytes; 2.512 ms | One open; 65,527 bytes; 1.670 ms | Equivalent source/resource results asserted; warm local filesystem only. |
| Validated lineage, 1,000 attempts | New capability; no invented earlier implementation | 27.559 ms median; 1,196,919 peak Python allocation bytes; 17,727,488 process peak RSS bytes | Allocation and RSS are separate measurements; no source I/O or scheduler requests. |

Measurements identify the implementation revision and exact measured sources.
The subsequent receipt-privacy correction adds explicit permission syscalls on
the publication path; these in-memory/parser measurements do not isolate its
export latency. Existing UI render and native parsing hot paths are unchanged
by that correction. The new native batch adapters measured about 65.9–89.5 ms
per 1,000 in-memory records; JSON decoding alone is a smaller reference cost,
not an equivalent old adapter. No scheduler-load or hardware speedup is inferred.

### Unblock conditions

* Live Kubernetes, LSF, OpenPBS/PBS Professional, HTCondor and Flux qualification
  requires an explicit authorized endpoint, installed native client, account or
  namespace, and permission for the reviewed bounded test. Fixture conformance
  remains available without those credentials.
* Non-Slurm Heavy and retained automatic recording require native allocation,
  step/process ownership and lifecycle adapters, then compute qualification.
  Supplying credentials alone does not implement those adapters.
* Physical GPU, multi-node clocks/interconnects, cross-host filesystems, remote
  authentication and production cgroup/resource enforcement require their real
  target hardware and site policy. Container/local tests do not discharge them.
* Scientific checkpoint compatibility, control equivalence and optimization
  efficacy need declared application validators and repeated real measurements.
  Adaptive diagnosis and capture-overhead estimates remain experimental until
  evaluated against independent representative incidents.
* Universal balance discovery, automatic new-format provenance attachment for
  all legacy plots, and unbounded checkpoint materialization are not shipped.
  Their bounded existing alternatives and current code boundaries are documented
  above; they are not mislabeled as missing credentials.
