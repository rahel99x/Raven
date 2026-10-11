# Contributing to Slurm Tower

Small fixes, cluster compatibility improvements, accessible visual design, and
clearer documentation are welcome. Open an issue for a substantial change so the
scope can be discussed before implementation.

## Start locally

```bash
git clone https://github.com/rahel99x/Slurm-Tower.git
cd Slurm-Tower
python3 scripts/setup.py --mode demo --dev --test
./scripts/tower --fake
.venv/bin/python -m build
```

Use a feature branch for your change. Keep the runtime dependency-free and compatible
with Python 3.10+. The demo backend makes development possible without an HPC account.
Test targeted behavior while iterating, then run `.venv/bin/python -m pytest -q`
before opening a pull request. CI also builds and smoke-tests the wheel.

## What makes a useful contribution

- Explain the problem, resulting behavior, and how you verified it in your pull request.
- Add a regression test for a bug or a meaningful new behavior. Use synthetic or sanitized Slurm output.
- Preserve confirmation before destructive job actions, bounded command timeouts, and source backoff.
- Keep charts readable without color; include units, empty states, and narrow-terminal behavior.
- Keep the application terminal-only with ASCII charts and plain-text reports; sanitize terminal control characters in untrusted text.
- Document flags and configuration changes in the runbook or reference.

Operation descriptors are the source for the [generated field reference](docs/reference/operations.md).
After changing a form or task descriptor, run
`python3 scripts/generate_operation_reference.py`. Use its `--check` option in
validation; do not maintain a second handwritten list of input defaults.

Use `tower.command_registry` for built-in command metadata and static toolbar
actions. Keep contextual validation in the appropriate command handler. Use
`tower.capabilities` to assign proposals to their primary owner, then run
`python3 scripts/generate_capability_reference.py --check`. Read the
[consolidation guide](docs/guides/consolidation.md) before adding a second
collection, pane, investigation, or action lifecycle.

Never include SSH keys, credentials, personal config, real job logs, or private
cluster information in an issue, fixture, or terminal capture. Report security issues
through the process in [SECURITY.md](SECURITY.md).

The [reference](docs/reference.md#architecture) maps the modules. Pure layout and
parsing functions sit alongside the store, sampler, controller, terminal renderer,
and ASCII report renderer; tests can exercise most behavior without curses or a live cluster.

## Preserve the interaction and evidence contracts

Do not start filesystem scans, scheduler commands, analysis, JSON serialization
or persistence from hover/render paths. Publish worker results with an exact
connection and job-attempt identity. Old replies must not change the current
page or selection. Keep accepted action outcomes available after their view closes.

Use the shared worker scheduler for native nested collection. A worker that waits
for its children must use cooperative gathering; a private executor must not
bypass the live Single/Multi budget. Long-lived recorders need explicit ownership,
transport and shutdown contracts.

Keep one owner for a pointer gesture. Preserve the byte decoder, painted hit-map
identity, active modal, lost-release recovery and generation checks. Test the
same control through direct App calls, native curses events and fragmented raw
terminal reports. Include a sequential case with selection, graph zoom, a menu,
resize and a source update. Keyboard and mouse controls must remain equivalent.

Native history deques publish immutable tuples by mutation revision. Standard
append, overwrite, deletion and reorder operations invalidate that revision.
External history containers retain a full-value fallback. Do not substitute
length/last-point comparisons: they miss corrected historical measurements.

Native metric records use owned publications with nested correction tracking.
Use the shared preparation helpers across a page and its Details view. Preserve
memory-only Inspector reads and explicit report readers. Unsupported mutable
extensions must retain the uncached path without running reducer or copy hooks
during rendering. Include a test with more source identities than one viewport
normally displays; byte and entry limits must remain effective together.

## Qualify an integration

Document supported runtimes/tool versions and exact data schemas. Add fixtures
for missing evidence, unsupported counters, stale attempts, malformed/oversized
input, partial writes, cancellation, clock changes and process failure. Keep
original observations distinct from derived summaries and inferred explanations.

Run the focused suite and the complete regression suite, build a wheel, then use:

```bash
python3 scripts/qualify_flagship.py --output /tmp/tower-qualification.json
```

Pass `--wheel` with the actual built artifact to check a clean offline
installation. Review the [qualification guide](docs/QUALIFICATION.md),
[support policy](docs/SUPPORT_POLICY.md), and
[implementation ledger](docs/FLAGSHIP_IMPLEMENTATION.md). Record untested hardware
and site requirements. A high test count is not a substitute for a target-system
check, an unfamiliar user's task completion, or measured scientific outcomes.

Research features need a scoped hypothesis, an explicit baseline, declared
assumptions and evaluation criteria. Report failed hypotheses and uncertainty.
Do not label an integration or a terminal rendering technique as unprecedented
without a separate prior-art review.

By contributing, you agree that your contribution is available under the project's
[MIT license](LICENSE). Retain attribution when adapting third-party code.

## Prepare release metadata

After the final artifact build and qualification, run
`scripts/release_inventory.py create` for the exact wheel and source archive.
Generate the member inventory, unsigned `SHA256SUMS`, and minimal SPDX 2.3 document;
then run its `verify` command before publication. Follow the
[release inventory guide](docs/guides/release-inventory.md) for commands, limits,
and external components that are excluded. Rebuilds require fresh metadata.
Checksums are not signatures or proof of build provenance.
