# Adversarial review evidence archive

The [compact live Slurm report](evidence/live-slurm-review/summary.json) links to a separate, immutable [evidence archive](https://github.com/rahel99x/Raven/tree/3bdf0a3d6566b3810678339484b5936a2b0beca5). Keeping the raw artifacts off the product branch avoids expanding ordinary installation downloads.

The archive commit is `3bdf0a3d6566b3810678339484b5936a2b0beca5` on `evidence/adversarial-review-2026-10-20`. Its [manifest](https://github.com/rahel99x/Raven/blob/3bdf0a3d6566b3810678339484b5936a2b0beca5/artifact-manifest.json) lists 42 payload files totaling 5,426,869 bytes. The manifest's SHA-256 is `b0c832f8014bde4db93442c9125cca97d77005658bb3c6a58d2075dd7a71b17c`.

The payload includes actual disposable Slurm qualification across five versions, orphan cleanup, preserved initial failures, exact qualified source and instrumentation runtime archives, original bounded measurement reports, reproduction drivers, and source-provenance records. [The archive README](https://github.com/rahel99x/Raven/blob/3bdf0a3d6566b3810678339484b5936a2b0beca5/README.md) explains the scope of each group.

These runs do not establish production-site, physical GPU, multi-node, or live SSH qualification. Slurm qualification timings are not performance measurements. Performance reports retain their environment limits, source identities, process RSS and separate Python allocation evidence where measured. Explicitly discarded samples do not support speed claims. Some measured snapshots precede later correctness fixes; the reports distinguish those snapshots from the eventual integrated release.

The final checkpoint append is pinned at [`3b104ee`](https://github.com/rahel99x/Raven/tree/3b104ee64d060b5fbbe7d9da2f37345cd63c91cc). Its [second manifest](https://github.com/rahel99x/Raven/blob/3b104ee64d060b5fbbe7d9da2f37345cd63c91cc/artifact-manifest-v2.json) adds 34 payloads totaling 2,840,700 bytes; SHA-256 is `3b5d5fcdac5a6d437c54c45a8eee1057cd6fe78a583b4a95e0bc00175554e2c8`. The original manifest and all 42 original payloads remain byte-identical.

The append retains all 82 checks on five real disposable Slurm versions and orphan recovery at code checkpoint `003f3a5`, controlled architecture measurements, source manifests, and the experimental distribution prototype. Three subsequent UI-only runtime changes are explicitly recorded; the 48 qualified backend files remain unchanged. The archive does not label the entire later tree as live Slurm qualified.

The complete local delivery evidence is pinned at [`3a3288c`](https://github.com/rahel99x/Raven/tree/3a3288c8bb057cfdf8a050489ed13835e2940708). Its [third manifest](https://github.com/rahel99x/Raven/blob/3a3288c8bb057cfdf8a050489ed13835e2940708/artifact-manifest-v3.json) adds 60 payloads totaling 10,576,572 bytes, with SHA-256 `ed5c548067bfc594972ba9871dc5280312ac8faf7d216d229b8ae6bfbe1a3a2a`. All 76 prior payloads and both prior manifests remain unchanged.

This delivery retains the exact wheel and source distribution, all twelve completed local release gates, the full regression log/JUnit compressed without changing their bytes, installed/update/revert/package-extraction evidence, and the historical failed integration runs. The full suite at `f63d82e` passed 19,486 tests with 24 skips and zero failures/errors. Every one of the 626 runtime files matches the committed source and both distributions.

The final scope proof records six changes after the live Slurm checkpoint: pointer routing, workbench composition, the tracing UI, controller action completion, plugin discovery, and update source verification. The 48 qualified backend files remain unchanged. Hosted CI must separately qualify the delivered product revision; this archive makes no hosted-CI or whole-delivery live-Slurm claim.

Fetch and inspect the archive without switching an active checkout:

```bash
git fetch origin evidence/adversarial-review-2026-10-20
git show 3bdf0a3d6566b3810678339484b5936a2b0beca5:artifact-manifest.json
git show 3bdf0a3d6566b3810678339484b5936a2b0beca5:slurm/summary.json
```

For local integrity checks, compare each manifest entry's byte count and SHA-256 with the corresponding archived file. A checksum records data identity; it is not an independent signature or scientific attestation.

The earlier [implementation evidence archive](../../implementation/ARCHIVE.md) remains pinned separately to `b98742fb967f18131905696f4afd3a61f2ad03f2` on `evidence/pre-review-2026-10-20`. Neither archive replaces the final integrated regression, packaging, installed-command, or hosted CI checks.
