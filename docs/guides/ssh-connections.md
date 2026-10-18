# SSH connections and reuse

Raven Light and Heavy monitoring share scoped OpenSSH connections for scheduler
queries and retained-file reads. The external log pager uses the same target,
port, proxy and host-key options. Host-key verification remains OpenSSH's policy;
Raven never disables it to make a connection succeed.

Before each command, Raven asks `ssh -G` to resolve the original SSH configuration,
including `Host`, `Match` and `Include` directives. The target, login, command-line
options and complete effective configuration determine a private control-socket
name. Changing a proxy, identity or security setting therefore selects a different
connection. This costs one bounded local SSH process per command while avoiding
repeated authentication. It does not send another scheduler query.

Sockets are placed in an owned `0700` directory under the local temporary
directory. OpenSSH creates private sockets; the default idle persistence is ten
minutes. Existing `ControlPersist` settings remain effective. Raven does not kill
connections that another client may still use. Temporary resolution files are
removed immediately.

To disable Raven multiplexing, set `ControlMaster no` or `ControlPath none` in
your applicable SSH configuration, or supply the corresponding `ssh_options`
(`ssh_opts` in a legacy configuration). Explicit backend opt-outs remain disabled.
When enabled, a custom socket path is narrowed to Raven's private configuration
scope, rather than sharing an unrelated existing SSH session.

If OpenSSH cannot safely resolve configuration for multiplexing, Raven uses
separate connections and exposes `control_diagnostic` on its transport diagnostic
object. An unsafe socket directory is rejected with its path and reason; Raven
never changes its permissions automatically. A long or remote `TMPDIR` can be
unsuitable for Unix sockets: use a short, host-local temporary directory.

A disappeared socket causes OpenSSH to authenticate a new connection for the next
request. Raven does not replay a failed command, including a command whose result
was lost after it executed. Submission and cancellation recovery still require
their normal ownership and receipt checks.

Maintainers can run an isolated loopback qualification:

```bash
python scripts/qualify_ssh_reuse.py --output /tmp/raven-ssh-result.json
```

It starts an ephemeral server bound only to `127.0.0.1`, generates disposable test
keys, verifies the server key, compares authentication counts and latency,
checks reconnect and ambiguous mutation handling, and stops its own processes.
For an unprivileged container whose root filesystem ownership is remapped, add
`--remapped-root-fixture`; this relaxes only the fixture server's ownership check,
not client host-key verification. This is loopback OpenSSH evidence, not target
cluster or production SSH qualification.
