# Heavy startup troubleshooting

An allocation can have sufficient scheduler memory while Python starts with an
address-space limit that is too small. Virtual address space includes mapped
interpreter libraries and untouched memory mappings. It differs from resident
memory, and Slurm's memory grant does not replace the process's inherited limits.

## Hostname resolution and the `idna` codec

Python loads its standard-library `encodings.idna` codec during hostname
resolution. Raven previously lowered the supervisor's virtual-memory limit to
64 MiB before this lookup. A larger interpreter could fail with `MemoryError`
or `LookupError: unknown encoding: idna`, despite a much larger Slurm grant.

The supervisor now reserves memory for its existing mappings and bounded working
headroom. The reservation is deducted from the same total budget used for task
admission. Increasing the supervisor allowance therefore reduces the per-task
share; it does not silently increase the total Heavy budget.

FQDN enrichment is optional. If DNS or codec lookup fails, Raven retains the
kernel hostname and a bounded `hostname_resolution_error` diagnostic. Successful
FQDN aliases remain supported. Fresh scheduler records must still verify the
allocation, step, user, node membership, and local cgroup or PID membership.
A hostname alone never authorizes Heavy execution.

## Check an existing installation

Close Raven dashboards and their owned Heavy workers, then update the installed
release through its owning installer:

```bash
raven update
ravenh --plan
```

For an intentionally configured fork or SSH redirect, use the documented
`raven update --allow-fork` policy. The updater follows the source checkout's
tracking branch. It can report “already up to date” while that branch still
points to a revision without the fix. Review the printed installed and available
revisions. See [installation and updates](../INSTALLATION.md#update-the-installed-package).

The dashboard and compute-node worker must use the same release. For a managed
installation, the stable worker interpreter is:

```bash
raven_worker_python="$HOME/.local/share/raven/installations/current/bin/python"
"$raven_worker_python" -c 'import sys, raven; print(sys.executable); print(raven.__version__)'
```

Run compute-node checks through your site's normal account and partition. A
`WorkerClient` startup-only probe can stop when `client.placement` is present,
print `client.error` and `client.diagnostics`, and call `client.close()` in
`finally`. A `placement` status means the worker produced a placement candidate;
it does not mean ownership verification finished or an analysis task ran.

## Interpret remaining errors

- **Memory budget cannot cover this runtime and concurrency:** increase Heavy's
  memory request or reduce `max_tasks`. The supervisor reservation and every
  concurrent task share must fit the configured total.
- **Inherited `RLIMIT_AS` is too small:** inspect `ulimit -v` inside the compute
  step and review the limit with the site administrator. Supervisor startup
  does not raise inherited soft or hard limits. Analysis children replace the
  supervisor's soft cap with their admitted task cap, within the inherited hard
  limit.
- **Process-memory measurement is unavailable:** safe supervisor admission needs
  Linux `/proc/self/statm`. The worker reports the missing measurement rather
  than guessing a reservation.
- **Host or process membership cannot be verified:** inspect the fresh Slurm
  job, step, and node records. FQDN fallback does not bypass placement checks.

Address-space limits and admission arithmetic do not establish hard aggregate
RSS enforcement. The detailed [execution contract](../raven/execution-contracts.md#worker-startup-memory)
describes the reservation and enforcement scope.
