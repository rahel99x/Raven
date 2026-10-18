# External review implementation checkpoint

The authoritative accepted scope is the supplied handoff, reconciled against current source. Historical planning statements and reviewer claims are hypotheses, not executed commands or qualification evidence.

## Gate 0

- All four original handoff files are present and preserved with SHA-256 hashes in [coverage.json](coverage.json). The user supplied both complete reviews; no missing-text request is needed.
- Local clean `work` branch and remote `main` both identified `33316a08bd583a242356c7722a21c10e00cff21b` on inspection. Work is isolated on `codex/review-reliability-tranche-1`; no publish/merge is authorized for this scope.
- No `AGENTS.md` found. `CONTRIBUTING.md`, consolidation guide and current CI define the integration contracts.
- Host: Linux, Python 3.12.14, 5 available CPUs. Development venv restored from `.[dev,terminal]`. The core remains dependency-free.
- Baseline source snapshot: `/tmp/raven-review-baseline`. Controlled raw measurements: `/tmp/raven-review-evidence`. No concurrent heavy tests/builds during baseline capture.

## Dependency sequence

Gate 0 → W01/W02/W04/W06 correctness first, then W03/W05/W07, W08 measurement and W09/W10 release/documentation. W14 real Slurm setup proceeds independently when resource measurements are idle. W44 demo preparation does not substitute for participants.

Next: W11 characterization → W12/W13 contracts → W16 ownership models → W17 recovery; W08 precedes W18/W20. W19 is independent after terminal capability characterization. W22 moves only stable seams after contracts settle.

Later evidence packages W23–W31/W41 retain their handoff dependencies. W32–W37 require explicit experimental evaluation. W38–W40/W42 use tested contracts and deployment gates. W43 remains opt-in and W44 human/live evidence requires participants and site access. None is removed from scope by size.

## Coordination

- review_state: W01/W05/W06, Store/series/CLI; narrow acquisition identity regions in sampler.
- review_slurm: W02/W03/W15, parsers/accounting/source diagnostics.
- review_trust_ssh: W04/W07, config/plugins/transports.
- review_trace: W08/W20, timing/trace exports and screen hooks.
- review_qualification: W09/W10/W14/W44, release/docs/real scheduler harness.
- review_contracts: W11/W12/W13/W16, framing/properties/validators/ownership models.
- Root: integrated coverage, shared-file contracts, benchmark serialization, final review and subsequent package allocation.

No package is complete until each accepted subrequirement has evidence or an explicit dependency and unblock condition. Detailed status belongs in the machine-readable ledger and each package evidence record.

## Integration checkpoint

- Controlled 45-second simulated idle baseline: 2.07% of one CPU core, about 38 MiB RSS, 100 jobs. This corrects the stale 5–6% review figure for this fixture; it is not sustained performance qualification.
- W19 proceeded independently while tranche 1 owners worked, because its negotiation/renderer boundary does not depend on recovery or new storage. The existing real PTY/input regression suite passed after fixing an independent-review finding about late capability replies.
- Shared project-reader imports outgrew the SSH remote-command argument budget after state fixes. Added the actual transitive modules and bounded XZ source transfer, preserving the 120000-byte guard. Existing isolated remote/retained-telemetry tests: 76 passed. A preliminary compression comparison (not a controlled performance claim) showed 127776 to 107648 encoded bytes, with approximately 1.4 ms more decode time for this source snapshot.
- W03 controller resubmission uses exact retained attempt lookup; unknown-intent recovery supplies the durable intent time while retaining exact command/token/workdir verification. The focused parser/orchestrator integration passed 57 tests.
- W23 is now being implemented after the state/identity contracts stabilized. It indexes immutable completed exports transactionally and keeps originals authoritative; SQL runs through a read-only authorizer with explicit bounds. Package acceptance and integration remain in progress.

## Integration checkpoint: source-bounded evidence and historical correctness

The exact per-clause evidence maps for W01–W17 (where available), W20–W22 and
W44 have been reconciled into `coverage.json`. These are focused source-hash
results, not a claim of a completed final regression or deployed qualification.
The ledger retains external release-signing, site and human-study gates.

Independent work is now proceeding on W23–W27, W30–W43, with dependencies and
file owners agreed. W28 moved forward alongside W23 because inspection found a
correctness issue in the existing replay implementation: `_pick` returned the
first future command answer before its timestamp. The fix preserves unknown
intervals, and new recordings separate request and observation-completion times.
Legacy timestamp precision is explicitly labelled; it is not retroactively
upgraded into known-at-the-time evidence.

Measured regressions remain visible. Shared framing validation adds per-frame
cost, structured Slurm output is substantially larger than legacy text, and the
first W18 measurement window contained competing test work. Contaminated samples
are discarded; their favorable numbers are not acceptance evidence. Independent
SSH and signing measurements used strictly serialized fixtures. Final UI and
reader performance gates will use frozen source trees and another quiet window.

## Integrated qualification checkpoint (2026-10-18)

The managed runtime restarted after a disk-pressure recovery. The checkout and
repository evidence survived; `/tmp` was cleared. Every owner verified affected
source writes and restored the known truncated files before resuming. Lost
scratch artifacts are not treated as retained evidence. Completed measurements
were copied into this directory before the restart; missing compatibility
interpreters and temporary qualification tools must be reprovisioned.

W28 now has a complete bounded observation timeline, background-prepared
Jobs/History/Analytics inspection, exact-attempt restoration, disabled historical
actions, explicit archive publication and legacy reconstruction labels. The
focused replay/UI/source/registry integration passed188 tests. Independent
reviews resolved future-answer leakage, quadratic capture work, UI parsing,
identity conflicts, stale results and extreme clocks. Final gates remain open.

A new controlled measurement window began only after all six workers explicitly
acknowledged no active tests/builds/services and the root process audit was clear.
Measured source is frozen. W24/W25, W37, W39/W40 and planning harnesses run
sequentially; W27/W29/W36/W43, W28, W31 and final W08/W18 measurements follow.
Each owner retains source hashes, actual commands, units and qualification limits.
No worker may resume heavy tests until an explicit global release.

The coverage ledger has merged40 package status files. This is a focused-evidence
checkpoint, not a completed final regression or published revision. Remaining
per-package records and later source changes must be reconciled before delivery.

## Final integration and audit corrections

The exclusive measurement window ended at 00:42:47 UTC on 2026-10-18. Every
benchmark ran sequentially, and all six owners received an explicit release
before resuming functional work. Final UI measurements retain both improvements
and regressions: embedded-terminal idle CPU fell from 16.644% to 6.222% of one
core, while idle without a terminal rose from 2.022% to 2.200%. Publication-to-flush
p95 fell from 176.583 to 10.176 ms without a terminal and from 206.384 to 13.649 ms
with one. Scheduler command counts stayed equal. The sub-1% idle target was not
met. Later narrow correctness edits are recorded separately from that frozen
measurement source.

The exact-clause audit then identified additional accepted requirements that
could be completed locally. Completed native recordings now reopen after
controller purge using qualified accounting restart evidence. Shared-host
project grants now use an administrator-trusted mapping to exact native attempts,
separately from billing-account grants. MPI tooling is available, so the
conditional allocation-health MPI path is being completed rather than deferred.
Independent review is checking its reviewed CPU budget, inherited resource
limits, compiler trust and temporary-output cleanup.

Integration preserved the existing report no-UI-mutation assertions and corrected
Timeline value/copy semantics. An isolated SSH helper test caught an unnecessary
headless import of UI wake machinery; the headless Store now returns before that
import, preserving the existing SSH command-size ceiling. The all-view research
test caught a checkpoint-advice view reading a generic planning bundle; it now
requires an explicit advice input while preserving malformed-input diagnostics.

All 44 package records now reconcile to the original 371 clause IDs. The
reproducible `refresh-coverage.py` verifies source-document hashes and exact ID
sets. Internal full-regression, compatibility and installed-package gates remain
pending and cannot be converted into external blockers. A broad functional
four-worker regression run is finding integration issues before the final
controlled serial/parallel comparison.

Real historical Slurm qualification exposed and corrected the plural metadata
spelling in 23.02 JSON capability identification. That version's actual lifecycle
passed; later versions continue sequentially. Docker's `vfs` driver duplicated
entire filesystems for small fixture overlay images, consuming 4.19 GiB. Removing
only the four proven task-owned derivatives and their exact cache references
restored 7.03 GiB free. Original images, compatibility environments and evidence
were preserved. The runner now mounts the reviewed entrypoint without creating
those duplicate layers.

All five real Slurm lifecycle versions subsequently passed. The next exclusive
window ended at 01:09:55 UTC. A fixed-budget prospective forecast experiment
retained 65 actual outcomes and 25 chronological held-out predictions per model.
The new residual model did not improve on the existing predictor in that
fixture; its experimental status is retained. Trace-summary query timing now
excludes independent oracle calculation. The shared-host project implementation
showed a measurable CPU regression, traced to repeated private-attempt validation;
an independently reviewed publication-time validation change awaits its own
equivalent-fixture measurement.

The final functional integration pass repaired Quick Advisor's compact-recording
reader and stale same-time restart handling. Existing forecast and refresh
tests now model real state-owner close/reopen lifecycles without weakening the
trust policy. A 482-test affected selection passed. A separate MPI review found
toolchain ancestor and child-lifetime issues, which are being verified through
the actual governed worker path. Setup's repeated interpreter startup is being
consolidated while preserving every ASCII/Unicode view assertion and independent
installed-launcher checks. These are evidence-driven integration corrections;
they do not change the accepted scope.

## Committed-source regression and final review

Runtime revision `bab48f682b189004832e14eaa86113dbb44dbafb` passed the complete
Python 3.12 suite serially and with four workers: 18,278 passed and 12 skipped
in each run, with identical case identities and stable source fingerprints.
The comparison reduced wall time from 585.55 to 199.79 seconds, while increasing
child CPU and aggregate RSS. The three-minute target remains unmet. All 23
remaining Python 3.12 packaging, installed-command, quality, schema and inventory
gates passed. A separate actual MPI run passed 20 tests with no skips, including
the nine conditional cases omitted from the default suite.

The first Python 3.10 run found eight campaign-policy failures caused by the
Python 3.11-only `math.exp2` API. The reviewed replacement uses the supported
base-two power operation; its affected tests and final matrix rerun are pending.
A skeptical service review also found an absolute-timeout gap and incomplete
draining of coalesced requests in the new optional session service. Those fixes
receive dedicated regressions before the final source is frozen again. Earlier
passing results remain bound to their original revision.

The final clause audit verified all source hashes, 44 exact specifications and
371 unique clause IDs. It identified two uncompleted internal measurement tasks:
a controlled full/core startup comparison and persistent W38/W39 service resource
soaks. Both are assigned for completion on corrected source. Neither is being
classified as an external site-access blocker. Eight compound evidence citations
were split into individual paths while retaining their explanatory notes.


## Final local qualification and handback

Application runtime `0d138fd` remains unchanged. Metadata-only `c89808b`
restored omitted source-archive Slurm fixtures/build inputs and added extracted
archive checks. The Python3.13 full run then exposed a fixed-startup-timeout race
in an existing watch test; `6017669` separates bounded readiness from repeated
output and strengthens deterministic cadence assertions. A paired Python3.14 run
exposed a cleanup assertion against the shared per-user SSH root; `e3c7183`
isolates that fixture without changing real preparation or its assertions.
Both corrections received focused checks on all five versions and independent
review. Earlier failures and source-bound passes remain historical evidence.

All final Python3.10–3.14 full regressions and all25 non-regression gates per
interpreter passed on `e3c7183286fccdda68660b5541ec2e8c6d2c7750`. No runtime/test/script/schema changes
occurred during the runs. The full/core/lab comparison, actual60s service lifecycle
measurements, final actual SSH/two-UID checks and campaign-cost refresh also
completed; their original source bindings and negative results remain explicit.

The final ledger accounts for all44 packages and371 exact clauses:312 implemented,
35 already satisfied,16 adjusted with evidence,8 externally blocked. The internal
W22 packaging/matrix gate is closed. The remaining gates require hosted provenance,
graphical-terminal observations, independent scientific labels, administrator
deployment participation, or consenting human workflow evidence and subsequent
workflow-led core decomposition. macOS persistent state remains unsupported in
this revision and is documented separately. No push, PR, release or merge was
performed. The final handback, raw gate receipts and artifact index are review-ready.

## Final delivery packaging

All 25 sequential Python 3.12 delivery gates passed on documentation commit `59221f794591d2807a3d0e9080d22f66cfabd14c`. The completed handback was included in the wheel/source qualification; the exact artifacts, inventories and lossless logs are bound in [the delivery receipt](evidence/delivery-packaging.json). Runtime, tests, scripts, schemas, packaging metadata and CI remain identical to the final five-interpreter-qualified revision. The receipt is outer verification metadata, added after building; no hosted run or publication is implied.
