# Adversarial review evidence archive

The [compact live Slurm report](evidence/live-slurm-review/summary.json) links to a separate, immutable [evidence archive](https://github.com/rahel99x/Raven/tree/3bdf0a3d6566b3810678339484b5936a2b0beca5). Keeping the raw artifacts off the product branch avoids expanding ordinary installation downloads.

The archive commit is `3bdf0a3d6566b3810678339484b5936a2b0beca5` on `evidence/adversarial-review-2026-10-20`. Its [manifest](https://github.com/rahel99x/Raven/blob/3bdf0a3d6566b3810678339484b5936a2b0beca5/artifact-manifest.json) lists 42 payload files totaling 5,426,869 bytes. The manifest's SHA-256 is `b0c832f8014bde4db93442c9125cca97d77005658bb3c6a58d2075dd7a71b17c`.

The payload includes actual disposable Slurm qualification across five versions, orphan cleanup, preserved initial failures, exact qualified source and instrumentation runtime archives, original bounded measurement reports, reproduction drivers, and source-provenance records. [The archive README](https://github.com/rahel99x/Raven/blob/3bdf0a3d6566b3810678339484b5936a2b0beca5/README.md) explains the scope of each group.

These runs do not establish production-site, physical GPU, multi-node, or live SSH qualification. Slurm qualification timings are not performance measurements. Performance reports retain their environment limits, source identities, process RSS and separate Python allocation evidence where measured. Explicitly discarded samples do not support speed claims. Some measured snapshots precede later correctness fixes; the reports distinguish those snapshots from the eventual integrated release.

Fetch and inspect the archive without switching an active checkout:

```bash
git fetch origin evidence/adversarial-review-2026-10-20
git show 3bdf0a3d6566b3810678339484b5936a2b0beca5:artifact-manifest.json
git show 3bdf0a3d6566b3810678339484b5936a2b0beca5:slurm/summary.json
```

For local integrity checks, compare each manifest entry's byte count and SHA-256 with the corresponding archived file. A checksum records data identity; it is not an independent signature or scientific attestation.

The earlier [implementation evidence archive](../../implementation/ARCHIVE.md) remains pinned separately to `b98742fb967f18131905696f4afd3a61f2ad03f2` on `evidence/pre-review-2026-10-20`. Neither archive replaces the final integrated regression, packaging, installed-command, or hosted CI checks.
