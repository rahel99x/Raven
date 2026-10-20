# Installation, updates, and recovery

This increment extends the repository installer with explicit, recoverable
maintenance. It does not change scheduler admission or start background update
checks. User instructions are in [Installation](../INSTALLATION.md).

## Design and acceptance coverage

| Requirement | Implementation and acceptance evidence |
| --- | --- |
| One-command installation | `install.sh` selects Python, creates or validates a private checkout, then delegates to `scripts/install.py`. Local and piped invocation, literal arguments, destination races, cancellation, and terminal handoff have regression coverage. |
| Follow the selected branch | `raven update` resolves and fetches the current branch's configured upstream. `--plan` is local; `--check` fetches without replacing source or installation. It does not assume `main`. |
| Preserve installation ownership | Managed installations retain prefix, command directory, terminal preference, and prior release. Supported local-source pipx installations delegate to pipx with their verified paths and recorded extras. Unsupported or ambiguous owners receive a diagnostic. |
| Protect ordinary development work | Ordinary updates refuse unprotected source edits, untracked source, and local commits beyond upstream. Recognized configuration edits are preserved. |
| Explicit source replacement | `--overwrite` creates a bounded recovery backup before replacing tracked and nonignored untracked source with the fetched revision. Current configuration, state, and ignored environments survive. Additional configuration paths are explicit `--config PATH` arguments. |
| Restore a backup | `raven revert BACKUP_DIRECTORY` validates its complete manifest, content hashes, ownership, source identity, branch, and index. It first backs up the current source, then restores the saved source and staging while preserving current configuration and state. |
| Restore the installation | Managed recovery selects the verified retained release atomically. Pipx recovery reinstalls the restored source through pipx. Recovery prints another exact revert command. |
| Failure recovery | A failed managed build leaves the prior environment selected. Interruptions report completed backups. Concurrent index changes, unsafe paths, incomplete or tampered backups, changed ownership, and stale source content fail with diagnostics. |
| Independent command dispatch | `raven`, `ravenl`, and `ravenh` recognize maintenance before configuration or allocation. Local files named `update` or `revert` cannot redirect these commands into job submission. |

## Tradeoffs and limits

The simpler alternative was a wrapper around `git pull` and `pip install`.
Reusing the existing installer adds ownership checks and a verified release
switch without introducing another package manager. Separate source and
installation stages allow a failed build to retain the running managed release;
the printed source backup provides recovery for the checkout.

Backups retain source, the Git index, and installation metadata outside the
checkout. They are limited to 50,000 files, 64 MiB per file, and 256 MiB total;
oversized backups stop before destructive changes. They are recovery records,
not archives of ignored datasets or complete virtual environments. Keep retained
managed releases if their backups may be needed. Reversion requires the bound
source checkout and a valid retained installation; it does not adopt an
unrelated checkout or move a pipx environment.
Installation and command directories inside the source checkout are rejected
before maintenance. This prevents source replacement from removing the running
environment or public launchers before a replacement build succeeds.

Pipx retains its own replacement and failure behavior; it does not acquire the
managed installer's atomic switch. Build verification checks revision and bounded
source-content snapshots. Git authentication, proxies, and certificate settings
remain available; checkout/index redirection and automatic Git hooks are disabled
for maintenance operations.

Close dashboards and owned Heavy workers before maintenance. Scientific jobs
are not cancelled. No automatic polling or startup filesystem scan is added.
Installation and update time depend on package-index access and the owning
installer; this change makes no runtime performance claim.

## Validation

`scripts/qualify_updates.py --pipx --output DIRECTORY` exercises actual managed
and pipx installations against an owned local Git origin. Each lifecycle checks
planning, fetching, refusal of edits, overwrite update, configuration survival,
source recovery, and recovery of that recovery. It records command outcomes,
elapsed seconds, log hashes, source hashes, and source stability. Fixture version
changes occur only inside its temporary repositories.

CI runs focused and complete regression tests, builds and installs the wheel,
checks command help outside the checkout, and runs this lifecycle on Python
3.12. Failure artifacts are retained. The regular regression matrix covers Python
3.10–3.14. Live Slurm, GPU, remote cluster, macOS, and public download availability
are outside the local maintenance qualification.

Local qualification at `02ac2786bb2c882989d434f00eae174f95a12b24` passed:

- Full Python 3.12 regression: 18,487 passed, 12 skipped.
- Focused maintenance suite: 234 passed on each of Python 3.10, 3.11, 3.13,
  and 3.14; Python 3.12 runs these tests in the full suite.
- Actual installed lifecycles: all 31 checks passed for managed and pipx
  installations, including the piped bootstrap, overwrite, revert, second
  recovery, and terminal extras.

The [machine-readable receipt](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/installation-updates-evidence.json) records
source hashes, commands, elapsed seconds, log hashes, review conclusions, and
qualification limits. The first full run exposed a host-dependent profiler
fixture; its correction preserves the original ownership assertions and adds
explicit scan-exhaustion coverage. The production profiler is unchanged.

Final packaging and installed-wheel checks run after this documentation-only
receipt is committed. Earlier evidence under `review-2026-10/` remains bound to
its recorded historical revisions.

For publication, the unpublished commits were recreated with the account's
GitHub noreply address to respect its email privacy settings. Author names,
dates, messages, and source trees were preserved. The
[identity mapping](https://github.com/rahel99x/Raven/blob/evidence/pre-review-2026-10-20/docs/implementation/publication-identity-map.json) connects the recorded
qualification commits to their public equivalents; historical receipts retain
the exact hashes that were tested.

The initial published CI run
([39097589510](https://github.com/rahel99x/Raven/actions/runs/39097589510))
exposed an MPI trust-test fixture that selected an ambient `PATH` executable
and assumed its complete path was trusted from its leaf ownership alone.
GitHub's selected Python toolcache path failed the complete ownership and
write-permission checks. The fixture now constructs real, owned alternatives symlinks with
both relative and absolute targets, asserts exact content and filesystem
identity, and verifies rejection of writable original, intermediate, and target
directories. Production trust policy is unchanged. This also removes dependence
on an arbitrary host executable fitting the 4 MiB review bound.
The focused command `python -m pytest -q tests/test_mpi_health.py` passed on
each of Python 3.10–3.14: 11 passed, four explicitly gated MPI runtime tests
skipped. These fixture checks do not qualify a live MPI installation.

The subsequent [CI run](https://github.com/rahel99x/Raven/actions/runs/39098404015)
passed all five regression suites and four complete matrix jobs. Its additional
Python 3.12 extraction gate found a missing development prerequisite:
`build --no-isolation` could not import `setuptools.build_meta`. Isolated package
builds had installed that backend only in temporary build environments. The
`dev` extra now declares `setuptools>=77`, matching the build-system requirement.
The core runtime remains dependency-free. A fresh Python 3.12 environment,
initially verified to lack setuptools, passed the unchanged extraction qualifier
after installing `.[dev,terminal]`: all six installation, removal, reinstallation,
upgrade, and downgrade scenarios passed. The following managed and pipx
update/revert qualifier also passed all 31 checks with that environment's
commands on `PATH`.

The next [CI run](https://github.com/rahel99x/Raven/actions/runs/39099432845)
completed successfully on Python 3.11–3.14, including the Python 3.12 extraction
and maintenance gates. Python 3.10 exposed a test cleanup race: a process could
exit after opening `/proc/PID/stat` but before reading it, yielding
`ProcessLookupError` instead of `FileNotFoundError`. The test's liveness helper
now treats either missing-process result as exited. Timeout, owned-helper
termination, and unrelated-process survival assertions are unchanged, as is
production code. All 32 source-update tests passed on Python 3.10 and 3.12.
