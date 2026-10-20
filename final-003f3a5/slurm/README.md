# Final real Slurm qualification

Exact tested Git checkpoint: `003f3a5c515d70b48982d6c856c57a1d6607d613`. The source archive recursively includes all canonical families,625 Python files plus the tracked `transport/py.typed` marker. The complete per-file map is `source-manifest.json` inside `qualified-source.tar.gz`.

All82 checks passed across23.02.8,23.11.11,24.05.8,24.11.7 and26.05.5. The26.05.5 actual governed orphan-recovery scenario passed separately and preserved an unrelated owned sentinel. Six result archives contain all recorded commands and scheduler responses, manager snapshots, image identities and daemon logs. Private runtime cache/receipt directories are excluded; their relevant resulting ownership snapshots are retained in the manifests.

The26.05.5 warning case augments real scheduler JSON with declared warning metadata while retaining genuine job rows. It is not evidence of a naturally emitted Slurm warning. No physicalGPU, remoteSSH, multinode, production-site or performance qualification is claimed. Later UI-only pointer-routing/workbench/tracing-panel corrections are outside this exact tested checkpoint.

To reproduce, run `python3 prepare_snapshot.py /path/to/checkout /path/to/private/snapshot`, then the printed command for each pinned version and the `allocation-recovery` scenario on26.05.5. Cached images are used with `--skip-build`; the repository harness owns and removes only its private disposable container/network and preserves evidence on failure.

`qualified-backend-manifest.json` records 48 exact unchanged backend files. `post-checkpoint-diff-proof.json` and the corresponding patch delimit the later UI-only changes; these are qualified by the final integrated regression/CI rather than represented as this exact live snapshot.
