"""Explicit grants for administrator/lab-owned project roots."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

from . import config_trust as T


@contextmanager
def _registry_lock():
    target = T.trust_store()
    with T.directory_fd(target.parent, create=True, private=True, _allow_trust=False) as directory:
        fd = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise T.TrustError('The trust registry lock must be a private user-owned regular file.')
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise T.TrustError('Another trust update is in progress; retry shortly.')
                    time.sleep(.02)
            yield
        finally:
            os.close(fd)


def _main(argv=None):
    parser = argparse.ArgumentParser(prog='raven trust', description=__doc__, allow_abbrev=False)
    parser.add_argument('path', nargs='?', type=Path, help='Shared project root whose owners and group may supply trusted configuration/code')
    parser.add_argument('--remove', action='store_true', help='Remove this explicit grant')
    parser.add_argument('--list', action='store_true', help='List active grants without changing them')
    parser.add_argument('--yes', action='store_true', help='Confirm that the shared root owners/group may supply executable code')
    args = parser.parse_args(argv)
    if args.list:
        print('\n'.join(str(root) for root in T._trusted_roots()) or 'No shared roots are explicitly trusted.')
        return 0
    if args.path is None:
        parser.error('choose a shared root or --list')
    root = args.path.expanduser().resolve(strict=True)
    info = root.lstat()
    if root == Path('/') or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o002:
        parser.error('choose a real project directory without world write permission')
    if not args.yes:
        message = ('Remove trust for ' if args.remove else 'Trust the owners and group of ') + str(root)
        if not sys.stdin.isatty():
            parser.error(message + '; review this path, then add --yes to confirm')
        if input(message + '? [y/N] ').strip().lower() not in {'y', 'yes'}:
            return 1
    with _registry_lock():
        grants = []
        for existing in T._trusted_roots():
            if existing == root:
                continue
            current = existing.lstat()
            grants.append(dict(path=str(existing), uid=current.st_uid, dev=current.st_dev, ino=current.st_ino))
        if not args.remove:
            grants.append(dict(path=str(root), uid=info.st_uid, dev=info.st_dev, ino=info.st_ino))
        if len(grants) > 128:
            parser.error('at most 128 shared roots may be trusted')
        target = T.trust_store()
        raw = (json.dumps(dict(schema='raven.trusted-paths/v1', roots=grants), sort_keys=True) + '\n').encode()
        if len(raw) > 65536:
            parser.error('trust registry exceeds 64 KiB')
        with T.directory_fd(target.parent, create=True, private=True, _allow_trust=False) as directory:
            temporary = '.roots-' + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, target.name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    print(('Removed trust for ' if args.remove else 'Trusted shared root: ') + str(root))
    return 0


def main(argv=None):
    try:
        return _main(argv)
    except (OSError, ValueError) as exc:
        print('Raven trust: ' + str(exc), file=sys.stderr)
        return 2
