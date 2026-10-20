"""Offset-based SSH file transport independent of scheduler and presentation."""

from __future__ import annotations
import base64
import binascii
import shlex
import stat as _stat
from typing import List, Tuple

from .local_files import LocalFiles
from .transport_errors import CommandError


class RemoteFiles(LocalFiles):
    """The same, over an ``SshBackend``: stat and offset reads through coreutils on the login node."""

    remote = True
    min_refresh = 1.5

    def __init__(self, ssh, timeout: float = 8.0):
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
            out, _ = self.ssh.run(
                ["stat", "-L", "-c", "%f|%d|%i|%s|%y|%z", "--", path], self.timeout
            )
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
            out, _ = self.ssh.run(
                [
                    "sh",
                    "-c",
                    f"test -f {shlex.quote(path)} && test -r {shlex.quote(path)} && dd if={shlex.quote(path)} iflag=skip_bytes,count_bytes skip={int(offset)} count={int(length)} status=none | base64",
                ],
                self.timeout,
            )
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
        argv = self.ssh.argv(["less", "+G", "--", path])
        return [argv[0], "-t", *argv[1:]]

    def listdir(self, path: str) -> List[str]:
        try:
            out, _ = self.ssh.run(["ls", "-1", "--", path], self.timeout)
        except CommandError as e:
            raise OSError(str(e))
        return out.splitlines()
