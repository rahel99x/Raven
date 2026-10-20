# Installer update benchmark

[Recorded measurements](installation.json) compare one local Git update across
2,672 identical small Python files. Normal elapsed time was 5.336 seconds before
and 2.763 seconds after; process peak RSS was 18,096 and 17,648 KiB respectively.
All payload files and the final commit matched. These results describe this warm,
local Linux fixture. They do not qualify scheduler, GPU, remote or NFS behavior.

The [portable runner](../../../../scripts/benchmark_install_review.py) preserves
the original fixture and measured region. It adds private fixture receipts,
single-use modes and payload verification outside that region. The recorded
numbers predate this packaging; the original driver remains in the local review
evidence archive. The portable runner was checked with `--help` and Ruff without
repeating performance measurements during the integrated test run.

Run from a checkout retaining baseline commit `b98742f`. Choose a work directory
that does not exist; `prepare` creates it with mode 0700 and refuses existing
paths. Each command runs in a separate process, preserving independent import
and process RSS accounting. Run these commands serially while builds, tests and
other benchmarks are idle:

```bash
python3 scripts/benchmark_install_review.py prepare \
  --workdir "$HOME/raven-install-benchmark"
python3 scripts/benchmark_install_review.py baseline-raw \
  --workdir "$HOME/raven-install-benchmark"
python3 scripts/benchmark_install_review.py current-raw \
  --workdir "$HOME/raven-install-benchmark"
```

Use `--source-root /path/to/Raven` and `--baseline REVISION` to select another
checkout or historical revision with the same updater/trust module interface.
Pass the same values to every invocation. The baseline modules are extracted
from the pinned commit and hashed in the private fixture receipt. The current
modules come from the selected source checkout. No Raven installation is updated.
The updater touches only its generated local Git fixtures and backups.

Each mode saves its result inside the work directory and refuses another run in
place, including after interruption. Use a fresh work directory for another
measurement. The separate `baseline` and `current` modes retain the original
syscall-tracing fixtures. For example:

```bash
strace -c -f -e trace=openat,newfstatat,fstat,fsync \
  python3 scripts/benchmark_install_review.py baseline \
    --workdir "$HOME/raven-install-benchmark"
strace -c -f -e trace=openat,newfstatat,fstat,fsync \
  python3 scripts/benchmark_install_review.py current \
    --workdir "$HOME/raven-install-benchmark"
```

Compare normal timings with normal timings. Traced wall times include `strace`
overhead. CPU values combine the parent and completed children in seconds;
process and child RSS are separate Linux high-water marks in KiB, not Python
allocation peaks. Input/output counters use Linux 512-byte blocks, not payload
bytes. Warming every payload file before each timed update makes no cold-cache
claim. Full pre-mutation verification and file synchronization remain enabled.
