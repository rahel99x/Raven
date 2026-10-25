# Native Slurm qualification

The native adapter has an independent live-service gate in addition to parser
fixtures and the existing Slurm dashboard lifecycle tests. This gate runs only
in Raven's owned disposable container, as its unprivileged `raven` login, with
the fixture's `raven-ci` cluster and `labx` account. It never submits to the
invoking host's scheduler.

Review the scope without contacting Docker or creating state:

```bash
python3 scripts/qualify_native_slurm.py --plan --output /tmp/raven-native-review
```

Run the gate on a host with the managed/local Docker daemon and sufficient disk
space, using the same pinned build inputs as the existing qualification harness:

```bash
python3 scripts/qualify_native_slurm.py \
  --version 26.05.5 --output /tmp/raven-native-26.05.5
```

If that exact pinned image has already been built, `--skip-build` reuses it. The
output directory must be new. The image ID, actual Slurm version, delivered
Python source hashes, native command output hashes, checks and cleanup outcomes
are retained. A failed attempt stays available for diagnosis; reruns use a new
directory. A working-tree run records its actual source hashes and does not
claim that the base commit alone describes those files.

The gate exercises real native submission reviews, durable mutation claims,
user-scoped queue and accounting queries, selected detail, exact owner/attempt
checks, stale attempt rejection, cancellation, rejected replay, held-job release
and completion. Real attempts are linked into an experiment without inventing
missing accounting. A separately reviewed tiny compute job checks imports,
filesystem access, metric recording, selected tracing and worker startup, then
reconciles the reported compute identity with a fresh native observation.

Every test job has explicit one-node CPU, memory and wall-time bounds. Cleanup
revalidates the original exact identity and login; it does not fall back to a
recycled numeric ID or cancel unrelated jobs. An uncertain cleanup fails the
gate and leaves an explicit recovery note. The container and its internal
network are owned by the harness and removed after evidence collection.

The `real-slurm` CI workflow runs this gate and uploads its separate native
artifact. Successful parser fixtures do not discharge this live gate. One-node
container qualification does not establish production site policy, SSH,
multi-node behavior, physical GPU support, cgroup enforcement or qualification
of another scheduler. The fixture deliberately uses `task/none`.
