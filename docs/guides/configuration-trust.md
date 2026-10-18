# Configuration and plugin trust

Raven treats configuration containing commands and Python plugins as trusted
local input. Python plugins run with your account's full permissions; they are
not sandboxed.

Raven checks the file and every directory leading to it before reading:

- User configuration and plugins must belong to you or root. Neither files nor
  directories may be writable by another user or group.
- Mandatory site policy must belong to root and must be reached through
  administrator-owned directories. Readable site files such as mode `0644` are
  supported; public readability is different from permission to modify them.
- Symbolic links, nonregular files, oversized files and files changed during a
  read are rejected. Plugin execution uses the verified source bytes, without
  reopening the path or substituting a bytecode cache.
- Shared sticky temporary ancestors are permitted only when owned by the system
  administrator; the selected file and its directories still require trusted
  ownership. A private socket directory additionally requires mode `0700`.

New configuration files are created with mode `0600`; directories created by
Raven use `0700`. Existing files are never silently changed. An error identifies
the path and required correction. Inspect the file and its ownership first. If
it is yours and intended to be private, typical corrections are:

```bash
chmod 600 ~/.config/raven/config.json
chmod 700 ~/.config/raven ~/.config/raven/plugins
chmod go-w ~/.config/raven/plugins/*.py
```

Use only the commands applicable to directories and files that actually exist.
A symlinked configuration must be replaced by a regular reviewed copy; do not
change permissions on an unexpected target. Site-policy ownership is an
administrator task. Setup can save only your own configuration.

Readable `0644` configuration remains accepted for nonsecret preferences. Store
credentials in the supported agent/reference mechanism instead of embedding
secrets in shell hooks. Raven cannot infer whether arbitrary command text
contains a secret: make any such configuration private yourself. File integrity
checks do not protect against another process already running as your user or
against a malicious administrator.
