"""One cooperative writer per local mutable-state directory.

Metadata is diagnostic only. Kernel locks, never PID liveness or hostname
comparisons, decide ownership. Readers do not lock or create files.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import stat

LOCAL_FILESYSTEMS = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs",
                               "ramfs", "zfs", "overlay", "overlayfs"})


class StateOwnershipError(OSError):
    pass


def filesystem_kind(path):
    """Longest matching Linux mount, without assuming a remote PID is local."""
    target = os.path.realpath(path)
    candidates = []
    try:
        with open("/proc/mounts", encoding="utf-8") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mount = fields[1].replace("\\040", " ").replace("\\134", "\\")
                if target == mount or target.startswith(mount.rstrip("/") + "/"):
                    candidates.append((len(mount), fields[2]))
    except OSError:
        return "unknown"
    return max(candidates)[1] if candidates else "unknown"


class StateOwner:
    @staticmethod
    def preflight(path):
        """Read-only path/filesystem checks; never create or lock the store."""
        from .config_trust import directory_fd, read
        location = os.path.abspath(os.path.expanduser(os.fspath(path)))
        candidate = location
        while not os.path.exists(candidate):
            following = os.path.dirname(candidate)
            if following == candidate:
                break
            candidate = following
        try:
            with directory_fd(candidate):
                pass
            lock = os.path.join(location, '.writer.lock')
            if os.path.lexists(lock):
                read(lock, user_only=True, limit=65536)
            kind = filesystem_kind(candidate)
            if kind not in LOCAL_FILESYSTEMS:
                return {'state': 'read-only', 'path': location, 'filesystem': kind,
                        'message': f'Mutable state at {location} requires node-local storage; this path is {kind}. Retained evidence stays readable.'}
            return {'state': 'available', 'path': location, 'filesystem': kind,
                    'message': 'Path and local filesystem prerequisites pass; writer ownership is checked when a session opens.'}
        except (OSError, ValueError) as exc:
            return {'state': 'read-only', 'path': location, 'filesystem': filesystem_kind(candidate),
                    'message': 'State path trust check failed: ' + str(exc)}

    def __init__(self, path, *, read_only=False, strict=False):
        self.fd = None
        self.status = {"state": "read-only", "message": "Retained state is readable; this session does not write mutable state."}
        if read_only:
            return
        try:
            self._acquire(path)
        except (OSError, ValueError) as exc:
            self.close()
            self.status = {"state": "read-only", "message": str(exc)}
            if strict:
                raise StateOwnershipError(str(exc)) from exc

    @property
    def writable(self):
        return self.fd is not None

    def _acquire(self, path):
        import fcntl
        kind = filesystem_kind(path)
        if kind not in LOCAL_FILESYSTEMS:
            raise StateOwnershipError(f"Mutable state requires a verified local filesystem; {path} is {kind}. "
                                      "Choose a node-local state directory or use --no-state. Retained evidence remains readable.")
        from .config_trust import directory_fd
        lock_path = os.path.join(path, ".writer.lock")
        with directory_fd(path, create=True, _details=True) as (directory, shielded, _):
            fd = os.open(".writer.lock", os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        self.fd = fd
        info = os.fstat(fd)
        from .config_trust import unsafe_write
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or unsafe_write(info, shielded=shielded) or info.st_nlink != 1:
            raise StateOwnershipError(f"Unsafe state ownership file: {lock_path}; expected a user-owned regular file without group/other write access.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise StateOwnershipError(f"Another Raven writer owns {path}. Close it, choose an independent profile state namespace, "
                                          "or use --no-state. Headless readers may coexist.") from exc
            raise StateOwnershipError(f"Cannot acquire state ownership for {path}: {exc}") from exc
        raw = json.dumps({"host": socket.gethostname(), "pid": os.getpid(), "scope": os.path.realpath(path)}).encode()
        os.ftruncate(fd, 0)
        os.write(fd, raw)
        self.status = {"state": "writer", "message": "Exclusive local mutable-state ownership acquired."}

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __del__(self):
        self.close()
