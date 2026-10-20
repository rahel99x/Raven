# Disposable Slurm review evidence

The six successful archives retain real scheduler and accounting output, every recorded command, exact source maps, image identities, and daemon logs. `qualified-source.tar.gz` contains the exact package and scripts used by the five-version matrix. The 24.05 run uses `review_findings_driver.py`, which adds bounded broad-accounting convergence; the other runs used `review_findings_initial_driver.py`. No product reader change was needed for that fixture timing difference.

The before-PID-proof archive reproduces the actual missing-membership failure. The initial-accounting-visibility archive preserves its real 29-row native output and failed assertion. Neither is represented as final acceptance evidence.

The source snapshot is an uncommitted integrated tree based on the recorded Git revision. Its package SHA256 and per-file mapping identify the executed source; the base revision alone does not. Later MCP resource/cancellation fixes are qualified by focused regression and are outside this matrix snapshot.

To qualify another checkout with the same evidence-only adapter, run:

```bash
python3 prepare_snapshot.py /path/to/Raven /path/to/new-private-snapshot
```

Then use the printed environment and command. Images are pinned by the repository version catalog and must already exist when using `--skip-build`. This harness owns an internal-network, privileged disposable local Docker container; it never targets a production cluster. It removes only its own named container/network and preserves evidence on failure. No GPU, SSH network, multinode, production-site or performance qualification is claimed. 26.05 warning coverage augments real JSON with a warning and labels that augmentation explicitly.
