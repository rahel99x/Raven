# Core and lab distribution experiment

The supported installation remains the `raven` distribution with its existing
`raven`, `ravenl` and `ravenh` commands. The core/lab extraction is an unpublished
qualification prototype. It is not yet the proposed small administrative audit
surface, and it does not establish that new users prefer the split.

Raven currently has no mandatory third-party runtime dependencies. Adding a
`lab` extra alone would not reduce installed Raven source: extras select
dependencies, not modules inside a wheel. This experiment therefore builds two
wheels with disjoint file ownership. The core owns the package, launchers,
monitoring, launch/log/run-home paths and helper runtime. The lab wheel supplies
selected independent scientific adapters and kernels. Basic telemetry detection
stays in core when monitoring depends on it.

## Reproduce the extraction gate

From a development environment with `.[dev]` installed:

```bash
python scripts/distribution_prototype.py --output /tmp/raven-distribution-check
```

Choose a new directory. The command builds source and wheel artifacts from the
current checkout, creates private clean virtual environments, and exercises:

- The same 80-column synthetic monitor in complete and core installs.
- Actual local launch, retained logs, exact Run home and installed Heavy child.
- Existing plugin API with optional terminal dependencies absent.
- Explicit missing-lab feedback, matching lab restoration, lab removal and reinstall.
- Real package-version upgrade and downgrade, plus rejection of a mismatched lab dependency.
- Config/data preservation and disjoint wheel ownership.

The manifest records exact source digests, shipped/installed bytes, imported
modules, process peak RSS in KiB and workflow seconds. Python allocations are not
reported as RSS. These short local samples are not a general startup-speed or
human-usability claim. The complete installation is the equivalent baseline;
synthetic scheduler observations are not live Slurm qualification.

## Installation and migration boundary

Never install these experimental wheels over a production `raven` environment:
that would make two distributions own the same package files. Keep the generated
private virtual environments for review. The lab wheel requires the matching
experimental core version; install or remove it only while Raven is closed.
After changing extensions, start a new process so cached operation catalogs and
worker build identities are refreshed. Mixed profiles do not share a verified
worker session. Saved optional-tool favorites remain retained while that extension
is absent; its unavailable controls are hidden from the core workspace.

The experiment does not migrate, rename or delete user config, recordings,
project data or state. Existing installations should continue updating the
ordinary Raven wheel. A future umbrella package can preserve the familiar
commands, but a production distribution rename is gated on the full workflow,
helper-shipping, compatibility and human G5 evidence. The first extraction and
its honest footprint measurements remain useful while that evidence is absent.
