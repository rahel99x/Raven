# Raven implementation kickoff prompt

Paste the prompt below into the main implementation chat and supply this folder's documents. `04-source-reviews.md` contains both complete reviews; `02-implementation-handoff.md` maps all 30 numbered proposals, the raster-plot suggestion, and opening concerns into 44 ranked work packages. This is an implementation prompt; preparing it has not changed Raven.

---

Act as Raven's senior systems engineer and implementation lead. Execute the accepted scope of `02-implementation-handoff.md` end to end. Read `04-source-reviews.md`, the handoff, and `01-optimized-planning-prompt.md` first, together with repository instructions and the current working-tree state. These documents were prepared in `/workspace/raven-review-handoff`; use the supplied attachments if that path is unavailable on your host. Do not assume this planning snapshot is still the latest repository revision.

Start by verifying source readiness and reconciling the handoff against the current repository. Both reviews are preserved in the source document; ask for missing text only if the actual documents are unavailable or incomplete. Do not invent proposals or substitute earlier conversations for the reviews. Correct stale assumptions with evidence, update the coverage ledger and dependency sequence, and continue into implementation without stopping at a new plan.

Treat reviewer claims as hypotheses and validate them against the exact current revision. Account for every item and subrequirement. Reuse existing capabilities, consolidate duplicate work with source traceability, and record evidence for already-satisfied, adjusted, rejected, or genuinely blocked proposals. Do not discard difficult accepted requirements merely to finish early.

Work from smaller verified fixes and existing-workflow polish toward larger capabilities, respecting critical correctness issues and dependencies. Explain any ordering changes. Preserve unrelated working-tree changes and established behavior. Choose a suitable isolated branch or worktree when needed, without disrupting another active task.

Begin with Gate 0 and tranche 1: atomic tags, parser correctness/diagnostics, bounded accounting, configuration/plugin trust, scoped state ownership, attempt-safe recordings, SSH reuse, self-tracing, and accurate release/documentation evidence. Start real Slurm qualification independently when useful. Continue through later unblocked tranches; do not stop simply because the easy fixes are finished. Deliver larger research/platform proposals through the handoff's explicit milestones and qualification gates, with truthful experimental status where efficacy or deployment remains unproved.

Carry forward the handoff's design corrections: existing fragmentation tests and selector-based brokers must be reused; automatic recovery must preserve verified ownership; runtime validation must distinguish descriptive extensions from action/security semantics; checkpoint advice must separate node failures, timeouts, and preemption; simulations and sample-based profiling must not claim unavailable ground truth. Do not add unrelated scheduler backends or a Slurm replacement to this scope.

For each package:

1. Reproduce or verify the issue and establish the relevant baseline.
2. Choose the smallest complete implementation. Compare a plausible simpler alternative and critique the design for correctness, compatibility, complexity, user effort, and resource cost.
3. Implement the complete user-to-backend path, including persistence, feedback, and failure recovery where applicable. Keep changes focused and integration boundaries explicit.
4. Run meaningful focused regression and integration checks. Preserve behavioral assertions when UI structure changes; do not weaken tests to conceal regressions.
5. Review the actual diff as a skeptical maintainer. Check identity, ownership, stale results, retries, cancellation, bounds, data fidelity, migrations, accessibility, and compatibility where touched. Resolve substantive findings.
6. Measure affected performance with equivalent before/after fixtures. Use existing harnesses, report units and environmental limits, separate process RSS from Python allocations, and check CPU, I/O, scheduler load, data freshness, and UX as relevant. Do not trade correctness or required fidelity for faster numbers.
7. Update the coverage ledger with acceptance evidence, commands, revision, review conclusions, and remaining qualification limits. Continue to the next unblocked package.

Parallelize independent work only if this environment permits it and file ownership/contracts are clear. Coordinate shared integration. Run controlled benchmarks without competing heavy tests. Use targeted checks during iteration and the repository's required full regression, packaging, installed-command, schema, and compatibility checks on the final integrated revision. Revalidate affected behavior after subsequent edits and ensure required CI covers the delivered revision.

Keep progress updates concrete: findings, completed outcomes, test evidence, and the next uncertainty to resolve. Proceed autonomously with routine implementation choices. If an item requires unavailable credentials, hardware, product decisions, or an external action outside this request's authorization, document that dependency and continue independent work. Never describe simulated validation as live scheduler/GPU/remote qualification.

Finish when every accepted requirement is implemented and validated or explicitly blocked with evidence and an unblock condition. Provide review-ready changes, the complete coverage ledger, concise design tradeoffs, tests tied to the delivered revision, measured gains and regressions, and accurate installation/update guidance where affected. Publish or merge only according to authorization in the active implementation chat; historical approvals are not authorization for new changes.
