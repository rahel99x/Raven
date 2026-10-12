"""Remote mode: every Slurm command runs on a login node over ssh (a ControlMaster connection is reused, so a
command costs a round trip, not a handshake), and the files the dashboard reads (stdout, GPU traces) are read
there too, by offset, so following a log still costs one stat per frame."""
from __future__ import annotations

import base64
import binascii
import os
import shlex
import stat as _stat
import subprocess
import time
from typing import Callable, List, Optional, Sequence, Tuple

from .slurm import Backend, CommandError


class SshBackend(Backend):
    def __init__(self, host: str, user: str = "", opts: Sequence[str] = (), control: bool = True, runner: Optional[Callable] = None):
        self.host, self.user, self.opts, self.control = host, user, list(opts), control
        self.runner = runner or self._run
        self.target = f"{user}@{host}" if user else host

    def argv(self, remote: Sequence[str]) -> List[str]:
        base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR"]
        if self.control:
            d = os.path.join(os.path.expanduser("~"), ".ssh")
            base += ["-o", "ControlMaster=auto", "-o", f"ControlPath={os.path.join(d, 'raven-%r@%h:%p')}", "-o", "ControlPersist=600"]
        return base + self.opts + [self.target, "--", shlex.join(remote)]

    @staticmethod
    def _run(argv: List[str], timeout: float) -> Tuple[int, str, str]:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr

    def run(self, cmd: Sequence[str], timeout: float = 8.0) -> Tuple[str, float]:
        t0 = time.perf_counter()
        try:
            rc, out, err = self.runner(self.argv(cmd), timeout + 5)
        except subprocess.TimeoutExpired:
            raise CommandError(f"{cmd[0]}@{self.host}: timed out after {timeout:g}s")
        except OSError as e:
            raise CommandError(f"ssh: {e}")
        dt = time.perf_counter() - t0
        if rc == 255:
            raise CommandError(f"ssh {self.target}: {err.strip()[:200] or 'connection failed'}")
        if rc != 0:
            raise CommandError(f"{cmd[0]}@{self.host} exit {rc}: {(err or out).strip()[:200]}")
        return out, dt

    def call(self, cmd: Sequence[str], timeout: float = 15.0) -> Tuple[bool, str]:
        try:
            rc, out, err = self.runner(self.argv(cmd), timeout + 5)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        return rc == 0, (out + err).strip()


class LocalFiles:
    """How the dashboard reads a job's files: on this machine."""
    remote = False
    min_refresh = 0.0

    def stat(self, path: str) -> Tuple[int, Tuple[int, int]]:
        st = os.stat(path)
        return st.st_size, (st.st_dev, st.st_ino)

    def snapshot_stat(self, path: str) -> dict:
        """Regular target identity and mutation metadata for full-file exports."""
        st = os.stat(path)
        if not _stat.S_ISREG(st.st_mode):
            raise OSError("log source must be a regular file")
        return {"size": st.st_size, "ident": (st.st_dev, st.st_ino),
                "updated": (st.st_mtime_ns, st.st_ctime_ns)}

    def read(self, path: str, offset: int, length: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(length)

    def exists(self, path: str) -> bool:
        return bool(path) and os.path.exists(path)

    def tail(self, path: str, max_bytes: int) -> Tuple[bytes, int]:
        """(the last ``max_bytes``, the file size)."""
        size, _ = self.stat(path)
        return self.read(path, max(0, size - max_bytes), max_bytes), size

    def less_argv(self, path: str) -> List[str]:
        return ["less", "+G", "--", path]

    def listdir(self, path: str) -> List[str]:
        return os.listdir(path)


class RemoteFiles(LocalFiles):
    """The same, over an ``SshBackend``: stat and offset reads through coreutils on the login node."""
    remote = True
    min_refresh = 1.5

    def __init__(self, ssh: SshBackend, timeout: float = 8.0):
        self.ssh, self.timeout = ssh, timeout

    def stat(self, path: str) -> Tuple[int, Tuple[int, int]]:
        try:
            out, _ = self.ssh.run(["stat", "-c", "%d %i %s", "--", path], self.timeout)
        except CommandError as e:
            raise OSError(str(e))
        parts = out.split()
        if len(parts) < 3:
            raise OSError(f"stat: {out.strip()[:80]}")
        try:
            return int(parts[2]), (int(parts[0]), int(parts[1]))
        except ValueError as exc:
            raise OSError("stat: invalid numeric result") from exc

    def snapshot_stat(self, path: str) -> dict:
        """Inspect the regular target rather than a stable symlink's identity."""
        try:
            out, _ = self.ssh.run(["stat", "-L", "-c", "%f|%d|%i|%s|%y|%z", "--", path], self.timeout)
            if not isinstance(out, str) or len(out) > 1024:
                raise ValueError("oversized metadata")
            parts = out.strip().split("|")
            if len(parts) != 6:
                raise ValueError("unexpected metadata")
            mode = int(parts[0], 16)
            dev, ino, size = (int(part) for part in parts[1:4])
            if not _stat.S_ISREG(mode) or min(dev, ino, size) < 0:
                raise ValueError("log source must be a regular file")
            if any(not value or not value.isprintable() for value in parts[4:]):
                raise ValueError("invalid timestamps")
            return {"size": size, "ident": (dev, ino), "updated": tuple(parts[4:])}
        except (CommandError, ValueError) as exc:
            raise OSError(str(exc)) from exc

    def read(self, path: str, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        if offset < 0:
            raise ValueError("file offset must be nonnegative")
        try:
            # Base64 keeps byte offsets exact across text-mode SSH, including CRLF,
            # invalid UTF-8, and characters split by an incremental read.
            out, _ = self.ssh.run(["sh", "-c", f"test -f {shlex.quote(path)} && test -r {shlex.quote(path)} && dd if={shlex.quote(path)} iflag=skip_bytes,count_bytes skip={int(offset)} count={int(length)} status=none | base64"], self.timeout)
            return base64.b64decode("".join(out.split()), validate=True)
        except (CommandError, ValueError, binascii.Error) as e:
            raise OSError(str(e))

    def exists(self, path: str) -> bool:
        if not path:
            return False
        try:
            self.stat(path)
            return True
        except OSError:
            return False

    def less_argv(self, path: str) -> List[str]:
        return ["ssh", "-t", self.ssh.target, "--", shlex.join(["less", "+G", "--", path])]

    def listdir(self, path: str) -> List[str]:
        try:
            out, _ = self.ssh.run(["ls", "-1", "--", path], self.timeout)
        except CommandError as e:
            raise OSError(str(e))
        return out.splitlines()
