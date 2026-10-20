"""Stable interpreter identity for managed releases."""

import os
import sys
from pathlib import Path


def worker_python(executable=None):
    path = Path(executable or sys.executable).absolute()
    release = path.parent.parent
    current = release.parent / "current"
    try:
        if (
            path.parent.name == "bin"
            and release.name.startswith("release-")
            and current.is_symlink()
            and current.lstat().st_uid == os.getuid()
            and current.resolve() == release.resolve()
        ):
            return str(current / "bin/python")
    except OSError:
        pass
    return str(path)
