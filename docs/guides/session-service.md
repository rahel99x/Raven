# Optional per-user observation service

The experimental session service keeps one managed Light sampler running while
read-only clients disconnect and reconnect. Installation and ordinary `raven`,
`ravenl` and `ravenh` launch do not start a daemon. The existing dashboard remains
the complete interactive workspace; this milestone provides a compact observation
terminal, JSON clients and an installed remote bridge.

## Start and connect

Choose a short, private directory on a local filesystem. Home directories on NFS
or another shared filesystem are unsuitable for service ownership. In one shell:

```bash
python -m raven.session_service serve "/tmp/raven-session-$UID" \
  --config "$HOME/.config/raven/config.json" --site my-profile
```

Use your configured profile name. Omit `--site` to use the configured default.
The service creates an owned directory with mode `0700` and a socket with mode
`0600`; existing unsafe permissions are reported rather than repaired. It uses
the normal managed collection budgets and never requests an allocation.

In another shell, using the same Raven installation:

```bash
python -m raven.session_service query "/tmp/raven-session-$UID"
python -m raven.session_service watch "/tmp/raven-session-$UID"
python -m raven.session_service tui "/tmp/raven-session-$UID"
```

The terminal supports arrow keys, `j`/`k`, Page Up/Page Down, and `q`. It reconnects
on its next read after a temporary outage and retains its previous observation.
Watch emits one JSON envelope per second, including explicit read errors and
replay gaps. Reads do not request scheduler refreshes. Query prints one envelope.
The terminal's sole background reader keeps input responsive during a slow SSH
exchange. Publication age and the actual queue observation age are labeled
separately, with queue and service errors available in the scrollable view.
For a local demonstration, start a separate directory with `serve --demo`; those
observations are simulated and do not qualify a scheduler deployment.

Stop the foreground service with Ctrl-C. Stop only the process you started;
Raven does not kill a process based on a stale PID file. Its socket is removed
after sampler cleanup. Restarting the same connection in the same directory
preserves its session identity and retained observation, initially marked stale.
A new epoch signals that older replay events are unavailable. A different
connection/profile requires a different directory. A second writer is refused
before it can start collection.

## Connect over SSH

Install a compatible Raven release on the remote host and start its service
explicitly there. From your local installation:

```bash
python -m raven.session_service watch /tmp/raven-session-REMOTE_UID \
  --ssh-host cluster-login --ssh-user your-login
```

Replace `REMOTE_UID` with the UID on that host. The bridge invokes remote
`python3 -m raven.session_service`; make that installed interpreter available in
the remote noninteractive environment. Version negotiation fails clearly for an
incompatible service. This path does not install packages, ship source, alter SSH
host-key policy or start remote collectors automatically. Existing Raven SSH
configuration and scoped connection reuse apply. An interrupted read is retried
on the next client update; no mutation is ever replayed.

## Integrate a notebook, editor or portal

A notebook on the same host can read the authorized observation without creating
another sampler:

```python
from raven.session_service import Client

session = Client("/tmp/raven-session-1000")  # use your real UID
view = session.query("snapshot")
jobs = view["snapshot"].get("jobs", [])
print(view["session"], view["sequence"], view["stale"], jobs)
```

An editor task can run the `query` command and parse its JSON. A portal process
running as the same Unix user can keep a `Client` and request events using its
retained cursor. Do not expose the socket or relay responses to another user
without a separate authorization layer. The optional shared collector has a
different security boundary; same-user authentication does not grant lab-wide
visibility.

## Contract, bounds and recovery

The `raven.session/v1` contract exposes `hello`, `snapshot` and `events` only.
There are no scheduler actions, arbitrary commands, file reads, settings writes
or allocation recovery commands. One daemon owns the existing Store/Sampler;
clients cannot multiply its scheduler request rate or submit duplicate actions.
Heavy analysis remains available through the ordinary, verified allocation path.
The service does not adopt, cancel or infer ownership of external workers.

The observation is an explicit projection: job identity, state, selected resources
and times; source health; nodes; bounded event descriptions. Logs, job commands,
work directories, private configuration and full telemetry histories are excluded.
Each list reports observed, returned and omitted counts. The default row bound is
2,000 per list. An oversized observation leaves the previous snapshot intact and
reports a stale state and actual service error.

Requests are at most 16 KiB and responses 2 MiB. A daemon accepts up to 64 clients,
keeps at most 8 MiB of queued output in total, and enforces an aggregate read rate
of 32 requests/second with a burst of 64. Slow clients disconnect independently.
The replay ring retains 64 update descriptors; missing older updates produce an
explicit gap and the latest snapshot. The ring is not an archival event log.
Linux peer credentials and a kernel writer lock establish ownership. PID reuse
does not authorize takeover. Retained state is local and private.

## Qualification and rollback

Current qualification covers real local Unix sockets, malformed/version-skew
requests, bounded queues and replay, stale socket recovery, and a real daemon
process using the explicitly simulated backend. The reproducible
`scripts/qualify_session_service.py` also builds and installs a wheel in a
disposable environment, connects through actual loopback SSH, kills/restarts the
owned daemon, and checks stable session identity, explicit replay gaps and protocol
rejection. It requires build dependencies and a local test `sshd`. The remapped
container flag relaxes only that isolated server's file-owner check; client host
keys remain strict. This does not establish live Slurm, remote-site, systemd
lifecycle or administrator-managed multi-user qualification.
The full interactive dashboard and verified Heavy worker have not been migrated
into the service. Those integration milestones remain experimental and separate.

To roll back, stop the foreground service, use the ordinary launchers, and retain
its directory if you want its last observation. Delete the directory only after
confirming no service owns it. No ordinary Raven configuration migration or
system-wide service installation is needed. Upgrade both ends before reconnecting
across a protocol version change; unsupported versions fail closed.
