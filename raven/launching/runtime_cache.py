"""Content-addressed runtime materialization below launch orchestration."""

from __future__ import annotations
import compileall
import hashlib
import os
from pathlib import Path
import re
import stat
import zipfile
from .. import reporting as R
from .io import _exclusive

MAX_BUNDLE_BYTES = 64 << 20
MAX_BUNDLE_FILES = 1024


def _private_directory(path):
    R._directory(path)
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Runtime cache directories must be private and owned")


def _runtime_marker(target, digest):
    try:
        _private_directory(target)
        descriptor = os.open(
            target / ".complete", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size != 64
            or os.read(descriptor, 65) != digest.encode()
        ):
            raise ValueError(
                "Runtime cache marker disagrees with its private content identity"
            )
        return True
    finally:
        os.close(descriptor)


def runtime_directory(plan):
    """Materialize one content-addressed runtime for all job interpreters.

    Source remains available to another Python minor version; matching runtimes
    reuse bytecode instead of compiling the full zip at every process startup.
    """
    import tempfile

    digest = plan["runtime_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid runtime content digest")
    parent = R._under(
        plan["project_root"], ".raven/runtime-cache/.unused", create_parent=True
    ).parent
    _private_directory(parent)
    target = parent / digest
    if _runtime_marker(target, digest):
        return target
    temporary = Path(tempfile.mkdtemp(prefix=".runtime-", dir=parent))
    try:
        archive_path = Path(plan["runtime_bundle"])
        data = archive_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Frozen runtime archive does not match its digest")
        with zipfile.ZipFile(archive_path) as archive:
            total = 0
            if len(archive.infolist()) > MAX_BUNDLE_FILES + 1:
                raise ValueError("Runtime cache exceeds its file budget")
            for info in archive.infolist():
                relative = Path(info.filename)
                if (
                    relative.is_absolute()
                    or any(part in {".", ".."} for part in relative.parts)
                    or not info.filename.endswith(".py")
                ):
                    raise ValueError("Runtime archive contains an unsafe member")
                total += info.file_size
                if total > MAX_BUNDLE_BYTES:
                    raise ValueError("Runtime cache exceeds its source byte budget")
                output = temporary / relative
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _exclusive(output, archive.read(info), mode=0o400)
        compileall.compile_dir(temporary, quiet=2, force=False, workers=1)
        _exclusive(temporary / ".complete", digest.encode(), mode=0o400)
        try:
            os.rename(temporary, target)
        except OSError:
            if not _runtime_marker(target, digest):
                raise
        return target
    finally:
        if temporary.exists():
            import shutil

            shutil.rmtree(temporary)
