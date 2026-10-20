# Raven adversarial review evidence · 20 October 2026

This orphan branch preserves bounded raw evidence outside the product source tree. The compact reports on the implementation branch bind this archive by immutable Git commit. Root integration owns publication; this local archive does not claim a deployment or a passing final CI run.

- `slurm/` preserves the five-version actual disposable Slurm matrix, orphan cleanup qualification, exact qualified source archive, initial failures, drivers, and its SHA-256 manifest. Read `slurm/README.md` and `slurm/summary.json` for scope and exclusions.
- `instrumentation/measured-runtime.pyz` is the exact runtime archive used for the final controlled tracing/startup measurements. Its SHA-256 is `0db372f9e384a8b130377dbe513e50ff70d24223172566d9f281e59e151ee60c`.
- `measurements/` preserves compact original reports. `drivers/` preserves their reproduction programs. `provenance/` separates known measured implementation identities from later capture-time harness bytes.
- `artifact-manifest.json` records every archived file's bytes and SHA-256, excluding its own necessarily self-referential hash. Nested manifests remain unchanged.

The live Slurm runs use owned single-node Docker fixtures. They do not establish production-site, GPU, multi-node, or live SSH qualification. Their timings are not performance measurements. Distributed client checks use actual local Gloo/MPI runtimes; their fixtures do not establish a production network qualification.

Controlled performance reports distinguish process RSS from Python allocation peaks and retain fixture/result identities. Explicitly discarded or contested samples remain labeled and do not support gains. The source/runtime snapshots are identified individually; later correctness changes are regression-qualified separately. Nothing here claims all archived measurements ran against the eventual final integrated commit.

The separate historical archive `evidence/pre-review-2026-10-20` remains pinned to `b98742fb967f18131905696f4afd3a61f2ad03f2`; this branch neither replaces nor rewrites it.

To inspect a file without switching an active checkout, use `git show ARCHIVE_COMMIT:artifact-manifest.json`, replacing `ARCHIVE_COMMIT` with the immutable commit in the linking review report. To verify this worktree, compare each `files` entry's SHA-256 and byte count to its local file. The manifest identifies data; it is not a signature or independent scientific attestation.
