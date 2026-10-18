"""Pinned, bounded reads of trusted local configuration and Python plugins.

Readable configuration is not necessarily secret. Integrity requires trusted
owners and no group/world writes; confidentiality is a separate private policy.
Python plugins remain fully trusted code, never a sandbox.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat


class TrustError(ValueError):
    """A path cannot safely supply configuration or executable code."""


def _owners(site=False):
    # An unprivileged user namespace can map the immutable root filesystem's
    # owner to an overflow uid. Trust that *ancestor* owner, not arbitrary files
    # belonging to the overflow uid. File ownership is checked separately.
    return {0, os.stat('/').st_uid} | (set() if site else {os.getuid()})


def _directory(info, path, *, site=False, private=False):
    if not stat.S_ISDIR(info.st_mode):
        raise TrustError(f"{path}: expected a real directory, not a symbolic link.")
    if info.st_uid not in _owners(site):
        raise TrustError(f"{path}: directory owner is not the current user or system administrator.")
    if private:
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise TrustError(f"{path}: private directory must belong to this user with mode 0700.")
    elif info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX and info.st_uid in _owners(True)):
        raise TrustError(f"{path}: directory is group/world writable; remove write access before loading trusted code or configuration.")


@contextmanager
def directory_fd(path, *, site=False, private=False, create=False):
    """Walk from / with openat + O_NOFOLLOW; each ancestor remains pinned."""
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _directory(os.fstat(fd), '/', site=site)
        current = Path('/')
        for part in path.parts[1:]:
            current /= part
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            try:
                _directory(os.fstat(child), current, site=site)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        _directory(os.fstat(fd), path, site=site, private=private)
        yield fd
    finally:
        os.close(fd)


def read(path, *, limit=256 * 1024, site=False, private=False, user_only=False):
    """Return bytes and stat of one verified inode, rejecting in-place changes."""
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    with directory_fd(path.parent, site=site) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            owners = {0} if site else {os.getuid()} if (private or user_only) else {0, os.getuid()}
            if not stat.S_ISREG(before.st_mode):
                raise TrustError(f"{path}: expected a regular file.")
            if before.st_uid not in owners:
                raise TrustError(f"{path}: file must belong to {'root' if site else 'the current user' if private or user_only else 'the current user or root'}.")
            if before.st_mode & (0o077 if private else 0o022):
                rule = 'mode 0600' if private else 'no group/world write permission'
                raise TrustError(f"{path}: trusted file requires {rule}; inspect the file before correcting its permissions.")
            if before.st_size > limit:
                raise TrustError(f"{path}: file exceeds the {limit}-byte limit.")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if len(raw) > limit or identity(before) != identity(after):
                raise TrustError(f"{path}: file changed or exceeded its limit while reading; retry after the writer finishes.")
            return raw, after
