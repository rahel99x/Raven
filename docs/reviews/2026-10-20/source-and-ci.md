# Source archive and isolated CI evidence

[The coverage record](source-and-ci-results.json) accounts for S-3 and the
T-1/T-2 environment harness. Product identity and shared-directory trust changes
remain linked to their implementation owners; this report does not substitute
fixture checks for live cluster qualification.

All 52 original implementation reports remain on the main source tree. Their
774 raw evidence files, totalling 19,534,267 bytes, exactly matched reviewed
commit `b98742fb967f18131905696f4afd3a61f2ad03f2` before archival. They are
preserved on `evidence/pre-review-2026-10-20`; 48 reports now link to that
reference. The [archive guide](../../implementation/ARCHIVE.md) explains how to
inspect it without changing an active checkout. Publishing that reference is a
required release step. New bootstrap clones use depth one and a single branch;
the installer regression checks the complete argument list. Installer
qualification also exercised a real shallow Git clone against an owned local
repository, without claiming public GitHub transport qualification.

The four original temporal-holdout model/result files are now deterministic
test fixtures: 86,504 bytes with recorded SHA-256 hashes. Both regressions retain
their negative-efficacy assertions. The source distribution excludes historical
implementation evidence. An actual extracted distribution passed all 44
environment, forecast and simulator tests, contained those exact fixture bytes,
and loaded its own Raven sources. This check caught a missing existing
`examples/queue-model.json`; the manifest was corrected and the distribution
rebuilt before recording success. The qualified artifacts measured 4,798,157
bytes for the source distribution and 2,072,357 bytes for the wheel. An isolated
build of the reviewed original commit, using the same build backend, measured
13,047,514 source-distribution bytes. The current source archive is 63.2% smaller;
it also includes the other integrated review changes, so that comparison is
not an isolated install-speed benchmark. Final integrated release builds are
checked separately.

Each test now has independent HOME and XDG roots outside its own `tmp_path`,
preserving assertions that a review makes no filesystem changes. Fixtures do
not invent USER or TZ values. The configured CI matrix runs with two pytest
workers; the Python 3.12 job also runs representative tests with USER unset and
`TZ=America/Los_Angeles`. That exact local environment run passed 222 tests.
An SSH-absent fixture explicitly forbids connection construction; it makes no
claim about a remote host or this machine's installed SSH client. Installer
jobs retain actual Bash 3.2/4.2 and umask-002 lifecycle qualification.

The quality gate now checks undefined/unused core names throughout Raven and
formats actual terminal, Heavy, UI and installation packages alongside reviewed
application/domain ports. The integrated local gate passed whole-core Ruff F,
formatting for 76 migrated implementation files, strict typing for ten source
files, and six dependency contracts. The architecture gate counts imports
inside functions and literal dynamic imports: 421 modules, 2,009 function-level
imports, a 109-module strongly connected component and eight unresolved dynamic
expressions. Those cycles remain explicit architecture debt. The complete
regression suite, hosted Python matrix, installed commands, release inventories
and checks against the final delivered commit remain release-integration checks.
