# Qualify the real Slurm lifecycle

The fixture builds a pinned Slurm source archive in a digest-pinned Debian image
and starts one disposable controller, compute daemon, accounting daemon and
MariaDB instance. It submits only its own small jobs on an internal container
network. It never connects to a user's cluster or exposes a host service port.

```bash
python3 scripts/qualify_slurm.py --version 26.05.5 --output /tmp/raven-slurm-check
```

Use a new output directory. `--plan` shows the scope without contacting Docker;
`--skip-build` reuses an existing local image with the named version. The local
Docker socket must be available. The runner requires at least 8 GiB free before
a build and 4 GiB when reusing an image. Check the Docker storage driver: vfs
can copy an entire image for a small derived layer. The fixture requires disposable-container
administration rights; do not run it on an administrator's production scheduler.
It preserves Docker credentials, proxies and CA verification. In a managed
cloud environment it mounts the provided CA for networked build steps rather
than disabling TLS checks.

The image permits Slurm's process/user management inside a privileged disposable
container. The source checkout is read-only and the only writable host bind is
the new evidence directory. The current entrypoint is read from that checkout
under the image's existing tini reaper, so `--skip-build` does not silently run
an outdated entrypoint. The internal network has no external route. Only
the named container/network created by this invocation are removed on exit.
Failed commands and service logs remain available. Slurm uses task/none,
proctrack/linuxproc and Linux process accounting. Older releases load the v1
cgroup plugin without activating a controller; maintained releases explicitly
disable it. This fixture cannot qualify cgroup enforcement or its accounting
plugin. Node resources are deliberately small synthetic declarations.

## What is exercised

The lifecycle uses actual `sbatch`, `squeue`, `scontrol`, `sacct`, `scancel`,
`sprio` and `sshare` commands: held pending work, running work, accounting
completion and delay, exact stdout, arrays, cancellation, requeue and a retained
controller restart/reconnect. It includes account `labx`, paths/names with
spaces and literal `=` characters. It captures structured output when supported
alongside legacy forms, including unsupported-command stderr.

Each raw output has its command, exit status, SHA-256 and seconds in a manifest.
The report records the source revision/content hashes, image identity and exact
installed OS package versions. These synthetic usernames and paths contain no
real participant/site information. Only outcomes explicitly marked passed are
qualified. Numeric ID reuse is exercised by starting a clean generation of
this disposable controller with a new empty owned StateSaveLocation while
retaining accounting, then submitting the same
number with a different identity. That deliberately destructive fixture step
must never run against a production controller. Heterogeneous allocations remain
a separate check. Requeue and numeric-ID recycling are recorded separately.

## Version and CI policy

[`versions.json`](../../scripts/slurm/versions.json) pins source hashes for
23.02.8, 23.11.11, 24.05.8, 24.11.7 and the maintained 26.05.5 line selected from
upstream documentation on 17 October 2026. Historical versions are compatibility
candidates, not a declaration of vendor maintenance. Recheck upstream status
when changing the matrix; hashes obtained through TLS are not vendor signatures.

The `real-slurm` workflow runs the maintained smoke case on relevant trusted
repository PRs and the full matrix nightly/manually, with at most two jobs at
once. Fork PRs do not run privileged fixtures. Uploaded evidence is tied to the
workflow commit. A passing container cannot close physical-GPU, multi-node,
production-SSH, site-account-policy, network-filesystem or human-usability gates.

The image base, Slurm source and Debian package snapshot (13 October 2026) are
pinned. Package signatures and HTTPS certificate checks remain enabled; only
the historical snapshot's expiry check is disabled. Installed versions are
recorded in `system-packages.txt`, and the image digest identifies the exercised
build. Compiler/build timestamps can still change image bytes; no byte-for-byte
image reproducibility claim is made.

## Recorded qualification

The [five-version receipt](../implementation/review-2026-10/evidence/W14-reader-matrix.json)
and [review evidence](../implementation/review-2026-10/evidence/W14.md) record
actual local lifecycle runs. Their exact compiled images were reused with the
current read-only entrypoint; these receipts do not claim a clean rebuild from
the final Dockerfile or a hosted CI run. The corpus preserves successful and
unsupported commands separately, including visible historical JSON fallbacks.
