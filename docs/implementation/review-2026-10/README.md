# Raven review implementation handback

This handback covers the accepted **44 packages and 371 clauses** from the supplied
implementation handoff. The [coverage ledger](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/coverage.json) is the exact
requirement-by-requirement record; the package index below links the implementation,
review, validation, and qualification evidence. An implemented local milestone is
not automatically a production-qualified feature.

The reviewed implementation revision is `e3c7183286fccdda68660b5541ec2e8c6d2c7750`. Its application
runtime is unchanged from `0d138fd`; subsequent commits corrected source-archive
contents and two test-isolation assumptions. The complete Python 3.10–3.14
matrix and all packaging gates passed locally on the reviewed revision.
The ledger records **312 implemented, 35 already satisfied, 16 adjusted with
evidence, and 8 externally blocked clauses**. No internal implementation or local
validation gate remains pending. Experimental milestones retain their limits;
the eight external acceptance gates are listed below.

The final documentation/evidence commits preserve the qualified runtime, tests,
scripts, schemas, package metadata and CI configuration. This work has not been
pushed, published or merged. Local results do not imply a hosted CI run.

## Scope, sequence, and provenance

The baseline is `33316a08bd583a242356c7722a21c10e00cff21b`, reconciled against the
actual checkout before implementation on `codex/review-reliability-tranche-1`.
The preserved [source reviews](sources/04-source-reviews.md),
[accepted handoff](sources/02-implementation-handoff.md), and
[planning prompt](sources/01-optimized-planning-prompt.md) are the scope inputs.
Their hashes, source-item mappings, adjusted interpretations, and dependency
sequence are retained in the ledger. Reviewer claims were checked against source
and behavior; existing fragmentation tests, selector brokers, ownership mechanisms,
and scientific execution workflows were reused.

Correctness and identity work preceded new storage and recovery. Protocol and
ownership models preceded automatic cleanup; measurement preceded wakeup and
tracing changes. Real Slurm qualification proceeded independently and exposed
version-specific parser behavior. Historical replay moved forward when review
found future answers leaking into earlier views. Larger research and platform
features followed their explicit experimental or deployment milestones.

Evidence has three distinct revision bindings:

- **Runtime validation:** each full regression names its exact commit, commands,
  test identities, and source fingerprints. The serial/parallel timing comparison
  belongs to `bab48f6`; the later IPC and compatibility corrections require the
  separately recorded final integration results.
- **Controlled measurements:** each receipt binds the exact measured source
  files, fixture, host, units, and command. Some measurements precede later narrow
  correctness edits. Their numbers are not silently relabelled as measurements
  of every file in the final commit; affected package records explain the boundary.
- **External qualification:** scheduler, MPI, SSH, peer-credential, and hosted
  provenance results identify their actual environment. Local tests and recorded
  transcripts cannot substitute for credentials, devices, production policy,
  or human observations that were unavailable.

Only retained repository artifacts are cited here. Earlier scratch files lost
when the managed host restarted are not treated as available evidence. Failed
or contaminated measurement attempts remain identified in package records and
are excluded from comparative claims.

## Principal changes and design choices

| Area | Delivered behavior and reason for the design |
| --- | --- |
| State and recordings | Atomic tag publication, scoped kernel writer ownership, and captured attempt identities prevent conflicting writes and cross-attempt samples. Unknown legacy identities remain preserved and visibly unverified. A blanket application singleton or job-number filename would be simpler but would break independent scopes or confuse requeues. Completed recordings reopen through qualified accounting evidence after controller purge. |
| Scheduler and trust | Bounded user/time-scoped accounting, explicit parser coverage, capability-gated structured readers, trusted configuration/plugin paths, and reusable SSH connections retain existing workflows while rejecting ambiguous authority. Unsupported Slurm formats fall back visibly rather than being treated as fully understood. |
| Recovery | Durable, bounded Heavy recovery preserves verified ownership and uncertainty. Automatic cancellation requires qualified controller-side exact name/UID/job filters; command success alone does not clear an uncertain receipt. Retrying by numeric job ID would be shorter but could cancel recycled or unrelated work. Scientific jobs are never automatically resubmitted. |
| UI, tracing, and replay | Coalesced wakeups reuse existing selector owners; a maintenance deadline remains for providers without explicit deadlines. Self-tracing, bounded trace exports, terminal synchronization, and opt-in raster rendering have explicit capability limits. Historical inspection preserves live context and disables actions. Command replay cannot reveal a future response; ambiguous clocks or appended sessions are labelled reconstructed. |
| Evidence and storage | Read-only SQLite queries index immutable evidence while originals remain authoritative. Optional compact blocks use the simplest exact general array/zlib representation; JSONL remains the default. Migration is explicit copy/verify publication. Tiered archives retain fidelity and gap metadata, and preserve raw originals rather than claiming total disk savings. |
| Research tools | Anomalies, rank divergence, phase alignment, process profiling, checkpoint advice, queue models, forecast alternatives, and reviewed experiments retain exact inputs, uncertainty, bounds, and recovery context. Checkpoint advice separates failures, timeouts, and preemption. Sample-based profiling and simulations do not claim unobserved ground truth. |
| Platform extensions | Optional read-only session/shared-host services, signed evidence, WASI adapters, and MCP reuse bounded contracts. Shared-host projects use protected exact-attempt memberships, separately from lab accounts. Ordinary launchers do not start services or enable experiments automatically. |

The larger milestones have deliberate limits. [W35](evidence/W35.md) delivers
reviewed shadow admission policies; its local controls do not enforce future
scheduler submission times. [W38](evidence/W38.md) delivers a read-only observation
service, not migration of the complete curses application or Heavy ownership into
a daemon. [W42](evidence/W42.md) produces unpublished core/lab installation
prototypes: the first core still contains 310 modules and is not a few-dozen-module
audit core. Further decomposition and a production migration remain unfinished
behind the stated workflow gate. No new scheduler backend or Slurm replacement
was introduced.

## Validation of the integrated runtime

The same-selection comparison below measures `bab48f6`, before the final IPC and
Python 3.10 corrections. It remains a source-bound performance result, not a
substitute for the corrected revision's regression gates.

The [regression comparison](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/W11-final-comparison.json) records
**18,278 passed and 12 skipped in each mode**, with the same 18,290 JUnit case
identities, no failures, no serial exclusions, and unchanged source fingerprint
`10f3c29138e5eac365a27ab3bbdcc164a85ca530a11792d4f2fcb4ef8e155a30`.
The [serial profile](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/raw/W11-final-serial.json) and
[parallel profile](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/raw/W11-final-parallel.json) retain exact commands,
environment, CPU/I/O observations, and losslessly archived JUnit evidence.

| Python 3.12 full regression | Serial | Four workers |
| --- | ---: | ---: |
| Wall time | 585.55 s | 199.79 s |
| Waited child CPU | 325.28 s | 385.33 s |
| Peak sampled aggregate live RSS | 1,222,672 KiB | 2,562,596 KiB |

Parallel execution reduced wall time by 65.9% (2.93×), with higher CPU and
memory costs. It did **not** meet a three-minute target. This is one complete
run per mode on the same quiet host, with a four-core CPU quota and five visible
affinity CPUs. Aggregate RSS sums live processes, can count shared pages more
than once, and is neither Python allocations nor proportional set size. Sampling
can miss short-lived peaks. Other CI gates are outside these durations.

The [final integrated qualification](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/final-qualification.json) records
the corrected revision separately:

| Python | Passed | Skipped | Packaging and installed-command gates |
| --- | ---: | ---: | --- |
| 3.10 | 18,252 | 42 | 25 passed |
| 3.11 | 18,256 | 38 | 25 passed |
| 3.12 | 18,283 | 12 | 25 passed |
| 3.13 | 18,259 | 36 | 25 passed |
| 3.14 | 18,259 | 36 | 25 passed |

Every final full regression has zero failures/errors and the same recorded
runtime/test/script/schema fingerprint. Optional tool/dependency skips are listed
individually; the separate actual MPI qualification covers its conditional tests.
The final Python 3.13/3.14 and 3.10/3.11 runs used two interpreters concurrently,
two workers each; Python 3.12 used four workers. These are functional qualification
runs, not replacements for the controlled serial/parallel comparison above.

All five interpreters passed quality-boundary checks, generated references,
78 schemas and 41 published examples, all 48 offline setup view cases, wheel and
source builds, extracted-source fixture tests, optional-free installed commands,
the terminal extra, flagship CLI/documentation/PTY checks, and release-content
inventory verification. Build/install gates ran sequentially. CI contains the
corresponding checks, including extracted-source testing; hosted execution remains
unclaimed. The experimental full/core/lab installation qualification is retained
separately under W42.

A final [delivery packaging receipt](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/delivery-packaging.json) records
25 additional passing checks on distributions built from the completed
documentation commit `59221f7`. The five-interpreter regression binding
above remains unchanged. This outer receipt was added after the build.

The original Python 3.10 API failure, Python 3.13 watch-timeout failure and
Python 3.14 shared-directory fixture failure remain in the archived evidence.
Their corrections preserve behavioral assertions and retain independent reviews.
The [maintainer review](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/maintainer-review.json),
[final consistency audit](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/final-audit.json), and
[artifact index](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence-index.json) provide the final cross-checks.

## Measured benefits and costs

The following are selected results from retained controlled fixtures. Full raw
samples, commands, source hashes, and environmental limits are linked from each
package. They are not interchangeable with live scheduler or display latency.

| Comparison | Measured result | Cost or qualification limit |
| --- | --- | --- |
| [Setup validation, W11](evidence/W11.md) | Median wall 30.364 → 20.648 s; process-tree CPU 11.838 → 2.087 s; direct subprocesses 52 → 5. All 48 ASCII/Unicode view cases and independent launcher checks remain. | Peak aggregate RSS 50,392 → 54,612 KiB. Two alternating measurements per variant; not interactive startup or cluster latency. |
| [UI readiness, W18](evidence/W18.md) | With an actual embedded shell, idle CPU 16.644% → 6.222% of one core. Publication-to-completed-flush p95 206.384 → 13.649 ms; without a shell, 176.583 → 10.176 ms. | No-shell idle CPU rose 2.022% → 2.200%; its active probe CPU rose 10.398% → 14.998%. The sub-1% target was not met. Scheduler responses were fixtures; command counts stayed equal. |
| [Exact compact storage, W24](evidence/W24.md) | For 8,192 samples, 504,958 JSONL bytes → 142,113 compact bytes, a 71.9% reduction. Exact envelope query median 6.542 → 0.634 ms after a separate 7.793 ms build. | Encoding 20.636 → 54.867 ms. For 128 small fsynced appends, 0.929 → 7.246 ms and 7,858 → 25,660 bytes. JSONL therefore remains the default; the envelope timing excludes full UI preparation. |
| [Retention, W25](evidence/W25.md) | Working archive reduced to 121 raw samples and 114 rollups; build 138.859 ms, query 6.048 ms. | Raw originals remain protected. The additional archive increased fixture storage from 504,958 to 521,108 bytes; this is a bounded working-set benefit, not total disk reclamation. |
| [Shared observations, W39](evidence/W39.md) | At 200 logical clients, equivalent full-source reference reads 400 → 2 and CPU 8.05465 → 0.08632 s. Publication-time validation reduced the prior project-enabled 0.19409 s shared cost. | Injected source calls, not measured site traffic. Final CPU and 220,380-byte cache exceed the earlier narrower implementation. Real overload bursts enforce quotas; they do not serve 200 clients immediately. |
| [Forecast holdout, W34](evidence/W34.md) | Actual Slurm capture produced 65 preissued outcomes and 25 chronological held-out predictions per model. Existing predictor MAE 0.36000 s; new residual model 0.37580 s. | The new model did not improve on the existing predictor and matched the cohort baseline. Initial abstention leaves overall coverage 25/65. Production calibration and counterfactual efficacy remain unproved. |
| [Distribution footprint, W42](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/W42-controlled.json) | Complete → core: shipped Raven bytes 6,283,158 → 6,059,927 (3.6% smaller); installed logical bytes 15,202,904 → 14,670,010 (3.5% smaller). Workflow imports 202 → 183 modules. | Core still ships 310 modules. Footprint excludes Python, pip, and the standard library; the small-core goal and production migration remain open. |
| [Distribution common workflow, W42](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/W42-controlled.json) | Across eight alternating fresh-process pairs, median import-through-first-frame 144.22 → 129.53 ms; whole probe 715.95 → 696.17 ms; parent CPU 352.54 → 331.94 ms; parent peak RSS 34,444 → 32,392 KiB. | First-frame differences favored core in every pair, but whole-process differences ranged from 51.61 ms faster to 18.81 ms slower. Parent CPU also regressed in individual pairs. This is an instrumented probe render with warm caches and synthetic scheduler data, not interactive readiness or human launch time. |

Storage figures come from [series-final.json](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/series-final.json), UI
figures from [W18-final-measurements.json](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/W18-final-measurements.json),
and shared-service figures from the
[final optimized receipt](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/raw/W39-W40-cost-optimized.json). Their package
records distinguish later correctness edits from the measured snapshots. Python
allocation peaks, process RSS, physical I/O, and logical I/O remain separate
quantities; none is substituted for another to imply a gain.

The [W42 raw report and harness](measurements/distribution-final/) retain all
observations from the exclusive 01:59:14.579–01:59:40.100 UTC window, including
unnormalized frames and exact artifacts. The initial import phase changed only
61.32 → 59.66 ms, within ordinary variation. Reaped-child CPU stayed effectively
unchanged at 312.08 → 311.97 ms. Separate allocation runs measured parent Python
peaks of 27,464,927 → 25,463,836 bytes; these are not RSS or aggregate process-tree
memory. Identical logical job/log/run/worker/configuration predicates were checked,
while raw displays intentionally differ in the unavailable lab action and wall
clock. Physical block counters were zero under warm caches. Local job launch and
worker execution were real; no live Slurm request or human usability result is
inferred. These modest measurements do not close the further decomposition gate.

## What was actually qualified

[W14](evidence/W14.md) exercised real controller, compute, accounting, and database
services in owned disposable containers for **Slurm 23.02.8, 23.11.11, 24.05.8,
24.11.7, and 26.05.5**. All five vertical lifecycle runs passed, including retained
identity and numeric job-ID reuse checks. The
[reader matrix](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/W14-reader-matrix.json) binds source and image identities.
Historical unsupported JSON schemas use visible legacy fallback; 26.05.5 uses the
qualified structured reader. These runs reused exact-version compiled images
with the current read-only entrypoint. They do not establish a clean build of the
final snapshot Dockerfile or hosted CI execution. The fixtures do not qualify
production cgroups, physical GPUs, inter-node fabrics, or site account policy.

[W17](evidence/W17.md) additionally exercised the production owned-receipt recovery
path on actual Slurm 26.05.5, with an unrelated sentinel job preserved. Versioned
atomic cancellation guarantees are not inferred from client version alone.

[W31](evidence/W31.md) ran real local memory/scratch probes and a governed
review → Apply → receipt path using **Open MPI 5.0.7**, two same-host ranks,
verified exchanges, cancellation, and owner-death cleanup. This is actual local
MPI execution, not Slurm MPI, inter-node bandwidth, GPU performance, or a causal
node-health diagnosis. [W38](evidence/W38.md) used an installed wheel over actual
loopback SSH with synthetic scheduler observations. [W39](evidence/W39.md) used
actual kernel credentials for two UIDs in a disposable container, again with
synthetic scheduler records. Those results establish their stated transport and
isolation properties, not CARC/RCIC production deployment.

The subsequent [60-second lifecycle receipt](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/raw/W38-W39-lifecycle.json)
and [reproducible harness](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/raw/W38-W39-lifecycle-harness.py) exercise
actual processes and sockets on `0d138fd`, with simulated scheduler sources:

- **W38:** direct managed Store/Sampler and actual daemon phases retained equal
  complete job/history hashes. After warmup, roughly 56 steady seconds used
  0.05/0.31 service-process CPU seconds respectively; RSS moved
  34,936 → 34,944 KiB directly and 35,164 → 35,284 KiB through the daemon.
  All 598 reconnecting socket reads succeeded. A separate six-second restart
  served 59 reads, preserving session identity while reporting a new epoch and
  replay gap. Retained context appeared in 0.186 seconds; this was not a fresh
  scheduler observation. Source command counts differed at the stop boundary,
  so no equal-load scheduler efficiency claim follows.
- **W39:** an actual shared daemon handled 576 source replacements, 20 protected
  policy cycles, and 147 same-UID socket exchanges. The 128 successful reads
  exposed exactly the current project's 32 authorized attempts; 18 stale-session
  errors and one revocation disconnect preserved cursors and required explicit
  reconnection. After warmup, 56.04 seconds used 1.34 process CPU seconds; RSS
  moved 16,916 → 17,044 KiB. Descriptors and threads remained stable, and owned
  processes and sockets were cleaned up.

These are short local lifecycle observations, not proof against long-term leaks
or a 200-user production load. Service CPU excludes the separate driver/client;
RSS is process memory, not Python allocations or process-tree peak memory. The
[installed SSH requalification](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/raw/W38-installed-ssh-ipc-final-run.json)
and [two-UID requalification](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/evidence/raw/W39-actual-peers-ipc-final-run.json)
passed separately after the IPC fixes. Their concurrent functional window is not
used for timing claims. Shared-host production authorization still needs the
administrator gate below.

Synthetic fault injection, ownership models, analytical cases, and sample-based
research evaluations remain valuable regression evidence. They are explicitly
separate from the real scheduler/transport runs above. No five-participant study,
representative graphical-terminal visual study, independent scientific phase-label
corpus, or physical GPU qualification is claimed.

## Eight remaining external acceptance gates

These are exact clause IDs in [coverage.json](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/coverage.json), not eight hidden
implementation omissions. Several share the same human-study dependency. Package
records retain the full limit and unblock condition.

| Clause | Remaining gate and unblock condition |
| --- | --- |
| [W09-R04](evidence/W09.md) | Authorize a hosted OIDC provenance run and verify its signed artifacts against the expected repository, workflow, revision, and digests. Local verifier tests and unsigned inventories do not close this gate. |
| [W19-R06](evidence/W19.md) | Run representative graphical-terminal/manual journeys and record visual tearing observations separately from PTY, curses, and tmux byte/timing checks. |
| [W32-R09](evidence/W32.md) | Obtain an authorized held-out attempt corpus with independent scientific phase labels; evaluate the declared accuracy and false-correspondence gates before product qualification. |
| [W39-R09](evidence/W39.md) | Obtain target-administrator participation, approved service identity, entitlements, scheduler policy, and deployment access; qualify production multi-user behavior and resource limits. |
| [W42-R02](evidence/W42.md) | Obtain W44 workflow evidence, then complete further independent core extraction and production migration qualification. The 310-module prototype does not satisfy the small-core goal. |
| [W44-R03](evidence/W44.md) | Authorize outreach and recruit five consenting participants; obtain permitted real-site/account access for the non-demo journeys. |
| [W44-R06](evidence/W44.md) | Run the five specified G5 tasks with participants and record completion, time, decisions, documentation use, recovery, and account/resource understanding. Scripted walkthroughs cannot replace these observations. |
| [W44-R08](evidence/W44.md) | Rank observed human friction, implement the resulting focused fixes, and rerun affected journeys. No fabricated ranking or usability-efficacy claim is supplied. |

Experimental features also retain their documented deployment and hardware limits.
For example, raster rendering remains opt-in pending emulator-specific visual
qualification. A locally completed prototype does not remove its release gate.

## Install, update, and rollback

Use the [installation and update guide](../../INSTALLATION.md#update-the-installed-package),
including [reliability revision compatibility](../../INSTALLATION.md#reliability-revision-compatibility).
The implementation branch is unpublished unless separately made available; do
not assume that fetching `main` obtains this revision. Before publication, use
only a reviewed local checkout or its exact qualified wheel. Wheel and source
archive qualification passed as recorded above; pulling remote `main` alone
does not obtain these unpublished changes.

Close existing dashboards and their owned workers, preserve configuration and
state, and update through the installer that already owns the commands. Pulling
source alone does not update an installed package. Recheck effective paths,
configuration, and `ravenh --plan` before requesting compute; local Slurm remains
distinct from standalone desktop mode. Stricter path trust and writer ownership
may require a reviewed private configuration or approved local state directory.
Do not delete a live lock to bypass an owner.

Persistent state currently requires a qualified local filesystem on **Linux or
WSL2**: filesystem verification uses Linux mount information. **macOS persistent
state is unsupported in this revision**, and ordinary interactive ownership may
refuse it. The [installation guide](../../INSTALLATION.md) describes
`ravenl --no-state` as the stateless path; no macOS execution or compatibility
qualification was performed here. This ordinary-state limitation is separate
from the optional services' Linux peer-credential requirement. Restoring Darwin
persistent state needs an implementation and actual platform qualification.

JSONL stays the default, and older ambiguous recordings remain preserved. Compact
formats, retention working archives, historical recording, services, WASI/MCP,
and raster output require explicit selection. Preserve originals and use the
recording export/recovery instructions before downgrading. Do not run old and
new writers against one state directory. The core/lab wheels are separate
unpublished experiments, not a replacement for ordinary Raven installation.

## Package evidence index

Each entry records its baseline, implementation boundary, alternatives, skeptical
review, focused checks, measurements where relevant, and remaining qualifications.
The [coverage ledger](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/review-2026-10/coverage.json) carries the exact source traceability and all
371 clause outcomes; per-package status files provide the machine-readable inputs.

| Package | Scope and evidence |
| --- | --- |
| W01 | [Atomic tag persistence](evidence/W01.md) |
| W02 | [Legacy parser correctness and diagnostics](evidence/W02.md) |
| W03 | [Bounded accounting lookups](evidence/W03.md) |
| W04 | [Configuration and plugin trust policy](evidence/W04.md) |
| W05 | [State ownership and network-filesystem policy](evidence/W05.md) |
| W06 | [Attempt-safe series identity](evidence/W06.md) |
| W07 | [SSH connection reuse](evidence/W07.md) |
| W08 | [Perfetto self-trace](evidence/W08.md) |
| W09 | [Release provenance and signing](evidence/W09.md) |
| W10 | [Evidence ledger and documentation polish](evidence/W10.md) |
| W11 | [Properties, fault injection, and test efficiency](evidence/W11.md) |
| W12 | [Shared framing, explicit protocol policies](evidence/W12.md) |
| W13 | [Versioned schema and validation strategy](evidence/W13.md) |
| W14 | [Real Slurm qualification in CI](evidence/W14.md) |
| W15 | [Capability-gated structured Slurm readers](evidence/W15.md) |
| W16 | [Ownership and protocol state models](evidence/W16.md) |
| W17 | [Ownership-preserving automatic recovery](evidence/W17.md) |
| W18 | [Wakeable UI and deadline-driven waits](evidence/W18.md) |
| W19 | [Synchronized terminal output](evidence/W19.md) |
| W20 | [Tracer efficiency and exports](evidence/W20.md) |
| W21 | [Signed evidence bundles](evidence/W21.md) |
| W22 | [Incremental architecture and quality gates](evidence/W22.md) |
| W23 | [SQLite evidence query layer](evidence/W23.md) |
| W24 | [Compact series and indexed rendering](evidence/W24.md) |
| W25 | [Retention tiers and explicit fidelity](evidence/W25.md) |
| W26 | [Streaming anomaly detection](evidence/W26.md) |
| W27 | [Rank-divergence logs](evidence/W27.md) |
| W28 | [Historical replay and comparison](evidence/W28.md) |
| W29 | [Unprepared-process profiler](evidence/W29.md) |
| W30 | [Checkpoint advisor](evidence/W30.md) |
| W31 | [Allocation health fingerprint](evidence/W31.md) |
| W32 | [Phase segmentation and alignment](evidence/W32.md) |
| W33 | [Observable Slurm digital twin](evidence/W33.md) |
| W34 | [Backfill-aware forecasts](evidence/W34.md) |
| W35 | [Fairshare-aware submission shaping](evidence/W35.md) |
| W36 | [Trace summaries with query contracts](evidence/W36.md) |
| W37 | [Nondeterminism attribution campaigns](evidence/W37.md) |
| W38 | [Optional per-user session service](evidence/W38.md) |
| W39 | [Shared host collector](evidence/W39.md) |
| W40 | [Optional WASI adapter boundary](evidence/W40.md) |
| W41 | [Read-only MCP evidence tools](evidence/W41.md) |
| W42 | [Core/lab distribution split](evidence/W42.md) |
| W43 | [Optional terminal raster plots](evidence/W43.md) |
| W44 | [Five-journey user study](evidence/W44.md) |
