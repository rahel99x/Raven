# Raven compatibility and support policy

This policy defines Raven's runtime, data, and adapter compatibility guarantees.
Supported environments are identified by release and recorded qualification.
Site permission and target-system validation remain separate from the presence
of an interface command or adapter.

## Core application

The terminal core uses Python 3.10 or later and the standard library. It requires
POSIX curses for interactive use. Linux is the main development environment;
macOS and Windows through WSL depend on their terminal and Slurm/SSH setup.
The repository CI configuration targets Python 3.10–3.14. The
[release validation record](raven/validation.md) identifies the checks actually
run and their outcomes. Optional native adapters have separate runtime and
hardware requirements.

Prepared Python tracing has a narrower contract: CPython 3.12+ monitoring is the
preferred adapter. An explicit older-Python fallback has reduced event semantics.
Read [Trace controls](guides/stack-tracing.md) before selecting an adapter.

## Data and adapter APIs

Versioned schema identifiers define the supported interchange boundary. Readers
must reject an unsupported major schema rather than guess its meaning. New
optional fields must retain the existing meaning of accepted records. A change
to identity, units, delivery semantics, or scientific interpretation needs a
schema revision and migration or a clear refusal.

Published schema `$id` values and `tower.*` interchange identifiers remain
stable when repository locations change. A repository URL update does not
change the identity or semantics of previously published evidence.

The supported external adapter protocol is separate from trusted Python plugins.
Where enabled, plugins execute Python inside the application and can access
implementation details. Managed Raven launchers require declared execution and
collection contracts; arbitrary in-process plugins are unavailable in that
profile. An external adapter runs only after explicit selection and review.
Its manifest pins executable/assets, supported methods,
environment, time and output budgets. Process isolation contains many failures;
it is not a sandbox for hostile code.

No stable ABI is promised for private Python names or internal UI dictionaries.
Use documented reporting files, structured commands and external adapter
contracts. Preserve schema versions and exact job-attempt identities in bug
reports and integration fixtures.

## Release and deprecation practice

- Add compatibility fixtures for old and new accepted documents before changing a reader.
- Announce removed commands and formats in the changelog. Provide a migration where it is safe; refuse ambiguous conversion.
- Preserve a tested rollback path for local settings and optional integrations.
- Keep native profiler, runtime, device and Slurm support matrices explicit.
- Do not download or replace tools when a user opens a graph or starts a capture.
- Record a maintainer/contact and reproducible fixture for contributed adapters.

UI settings use atomic replacement, a schema/checksum and one last-good backup.
Older unversioned settings remain readable. A newer unsupported settings schema
is preserved. Interactive saves coalesce on the shared worker budget; shutdown
drains the final setting before closing that budget. Synchronous headless saves
remain available. Filesystem failure is reported through preference status.

## Report an issue

Include the Raven version, OS/Python, terminal/multiplexer, command or control,
expected behavior, actual behavior, and the smallest sanitized reproduction.
Use `ravenl doctor`, `ravenl --gpu-check JOB_ID`, and `ravenl --ui-trace PATH` where
relevant. [Qualification](QUALIFICATION.md) produces a machine-readable coverage
record. [Security reporting](../SECURITY.md) is separate from public issues.
`JOB_ID` is the selected Slurm job ID. `PATH` is a private trace output file;
UI tracing requires an interactive terminal.

Share only selected evidence. Remove credentials, personal paths, job scripts,
logs and dataset identifiers unless they are needed and you are authorized to
share them. An offline qualification result must retain its untested fields.
