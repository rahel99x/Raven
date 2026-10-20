"""Bounded no-follow local file transport, independent of scheduler/UI."""

from __future__ import annotations
import os
import stat as _stat
from typing import List, Tuple

_CONFINED_WRAPPERS = {}


def register_confined_wrapper(wrapper_type, unwrap):
    """Composition owners register exact local wrapper types, never duck types."""
    if not isinstance(wrapper_type, type) or not callable(unwrap):
        raise TypeError("A confined wrapper requires an exact type and unwrap callable")
    _CONFINED_WRAPPERS[wrapper_type] = unwrap


def local_supported(files):
    if files is None or type(files) is LocalFiles:
        return True
    unwrap = _CONFINED_WRAPPERS.get(type(files))
    return unwrap is not None and type(unwrap(files)) is LocalFiles


class LocalFiles:
    """How the dashboard reads a job's files: on this machine."""

    remote = False
    min_refresh = 0.0

    def stat(self, path: str) -> Tuple[int, Tuple[int, int]]:
        st = os.lstat(path)
        if not _stat.S_ISREG(st.st_mode):
            raise OSError("log source must be a regular non-symlink file")
        return st.st_size, (st.st_dev, st.st_ino)

    def snapshot_stat(self, path: str) -> dict:
        """Regular target identity and mutation metadata for full-file exports."""
        st = os.lstat(path)
        if not _stat.S_ISREG(st.st_mode):
            raise OSError("log source must be a regular file")
        return {
            "size": st.st_size,
            "ident": (st.st_dev, st.st_ino),
            "updated": (st.st_mtime_ns, st.st_ctime_ns),
        }

    def read(self, path: str, offset: int, length: int) -> bytes:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError("log source must be a regular non-symlink file")
        with os.fdopen(fd, "rb") as f:
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
