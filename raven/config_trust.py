"""Pinned, bounded reads of trusted local configuration and Python plugins.

Readable configuration is not necessarily secret. Integrity requires trusted
owners and controlled write access; confidentiality is a separate private policy.
Python plugins remain fully trusted code, never a sandbox.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import grp
import json
import os
from pathlib import Path
import pwd
import shlex
import stat


class TrustError(ValueError):
    """A path cannot safely supply configuration or executable code."""


def _user_path(path):
    """Resolve the selected HOME/XDG root once, retaining no-follow below it."""
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    home = Path(os.path.abspath(os.path.expanduser('~')))
    roots = [home]
    for name, default in (('XDG_CONFIG_HOME', home / '.config'), ('XDG_DATA_HOME', home / '.local/share'),
                          ('XDG_STATE_HOME', home / '.local/state'), ('XDG_CACHE_HOME', home / '.cache')):
        roots.append(Path(os.path.abspath(os.path.expanduser(os.environ.get(name) or default))))
    for root in sorted(set(roots), key=lambda item: len(item.parts), reverse=True):
        if path == root or root in path.parents:
            return root.resolve() / path.relative_to(root)
    return path


@lru_cache(maxsize=16)
def _private_group(uid, gid):
    """A user-private primary group grants no other account write access."""
    try:
        user, group = pwd.getpwuid(uid), grp.getgrgid(gid)
    except KeyError:
        return False
    return user.pw_gid == gid and group.gr_name == user.pw_name and not group.gr_mem


def unsafe_write(info, *, shielded=False):
    """World write is unsafe; private-group writes and private roots are safe."""
    return bool(info.st_mode & 0o002 or info.st_mode & 0o020
                and not (shielded or _private_group(os.getuid(), info.st_gid)))


def trust_store():
    root = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser().resolve()
    return root / 'raven' / 'trust' / 'roots.json'


def _trusted_roots(*, identities=False):
    """Load explicit grants without recursively consulting the grants themselves."""
    path = trust_store()
    try:
        path.lstat()
    except FileNotFoundError:
        return ()
    try:
        with directory_fd(path.parent, private=True, _allow_trust=False) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
                    raise TrustError('The explicit trust registry must be a private, bounded regular file.')
                raw = stream.read(65537)
                after = os.fstat(stream.fileno())
                if len(raw) > 65536 or (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise TrustError('The explicit trust registry changed while reading.')
                def pairs(items):
                    result = {}
                    for key, child in items:
                        if key in result:
                            raise TrustError('The explicit trust registry has duplicate fields.')
                        result[key] = child
                    return result
                value = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(value, dict) or value.get('schema') != 'raven.trusted-paths/v1' or not isinstance(value.get('roots'), list) or len(value['roots']) > 128:
            raise TrustError('The explicit trust registry has an invalid schema or root count.')
        roots = []
        for grant in value['roots']:
            if not isinstance(grant, dict) or set(grant) != {'path', 'uid', 'dev', 'ino'}:
                raise TrustError('The explicit trust registry contains an invalid grant.')
            root = grant['path']
            if not isinstance(root, str) or not root.startswith('/') or len(root) > 4096 or not root.isprintable() or os.path.normpath(root) != root or any(type(grant[key]) is not int or grant[key] < 0 for key in ('uid', 'dev', 'ino')):
                raise TrustError('The explicit trust registry contains an invalid root.')
            try:
                info = Path(root).lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode) and (info.st_uid, info.st_dev, info.st_ino) == (grant['uid'], grant['dev'], grant['ino']):
                roots.append(dict(grant) if identities else Path(root))
        return tuple(roots)
    except FileNotFoundError:
        return ()
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise TrustError('The explicit trust registry is not valid JSON.') from exc


def _owners(site=False):
    # An unprivileged user namespace can map the immutable root filesystem's
    # owner to an overflow uid. Trust that *ancestor* owner, not arbitrary files
    # belonging to the overflow uid. File ownership is checked separately.
    return {0, os.stat('/').st_uid} | (set() if site else {os.getuid()})


def _directory(info, path, *, site=False, private=False, shielded=False, explicitly_trusted=False):
    if not stat.S_ISDIR(info.st_mode):
        raise TrustError(f"{path}: expected a real directory, not a symbolic link.")
    if info.st_uid not in _owners(site) and not explicitly_trusted:
        raise TrustError(f"{path}: directory owner is not the current user or system administrator.")
    if private:
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise TrustError(f"{path}: private directory must belong to this user with mode 0700.")
    elif (bool(info.st_mode & 0o022) if site else unsafe_write(info, shielded=shielded or explicitly_trusted)) and not (info.st_mode & stat.S_ISVTX and info.st_uid in _owners(True)):
        raise TrustError(f"{path}: directory is group/world writable; inspect then run "
                         + shlex.join(['chmod', 'go-w', '--', str(path)]) + ' before loading trusted code or configuration.')


def audit_directories(paths):
    """Report independent bad ancestors together, with one metadata read each."""
    roots = _trusted_roots()
    checked, problems = {}, {}
    for path in paths:
        path = _user_path(path)
        chosen = next((root for root in roots if path == root or root in path.parents), None)
        shielded = False
        for current in (*reversed(path.parents), path):
            key = (current, chosen)
            if key in checked:
                shielded, blocked = checked[key]
                if blocked:
                    break
                continue
            try:
                info = current.lstat()
            except FileNotFoundError:
                checked[key] = (shielded, False)
                continue
            explicit = chosen is not None and (current == chosen or chosen in current.parents or current in chosen.parents)
            blocked = not stat.S_ISDIR(info.st_mode)
            try:
                _directory(info, current, shielded=shielded, explicitly_trusted=explicit)
            except TrustError as exc:
                problems[current] = str(exc)
            shielded = shielded or info.st_uid == os.getuid() and not info.st_mode & 0o077
            checked[key] = (shielded, blocked)
            if blocked:
                break
    return problems


def permission_report(problems):
    entries = list(problems.items())
    shown = entries[:64]
    text = '\n'.join(reason for _, reason in shown)
    if len(entries) > len(shown):
        text += f'\n... {len(entries) - len(shown)} more offending paths.'
    writes = [str(path) for path, reason in shown if 'writable' in reason or 'write permission' in reason]
    if writes:
        text += '\nFor unintended write permissions, review then run: ' + shlex.join(['chmod', 'go-w', '--', *writes])
    if any('owner' in reason for _, reason in shown):
        text += '\nFor an intentional PI/lab-owned project, review and explicitly grant its root with raven trust PATH.'
    return text


@contextmanager
def directory_fd(path, *, site=False, private=False, create=False, _allow_trust=True, _details=False):
    """Walk from / with openat + O_NOFOLLOW; each ancestor remains pinned."""
    path = _user_path(path)
    grants = _trusted_roots(identities=True) if _allow_trust and not site else ()
    grant = next((value for value in grants if path == Path(value['path']) or Path(value['path']) in path.parents), None)
    chosen = Path(grant['path']) if grant is not None else None
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _directory(os.fstat(fd), '/', site=site)
        current = Path('/')
        shielded = False
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
                info = os.fstat(child)
                if current == chosen and (info.st_uid, info.st_dev, info.st_ino) != (grant['uid'], grant['dev'], grant['ino']):
                    raise TrustError(f'{current}: the explicitly trusted root changed; review and grant it again.')
                # Access through ancestors of an explicit grant is pinned too;
                # no symlink is followed and the grant itself remains exact.
                explicit = chosen is not None and (current == chosen or chosen in current.parents or current in chosen.parents)
                _directory(info, current, site=site, shielded=shielded, explicitly_trusted=explicit)
                shielded = shielded or info.st_uid == os.getuid() and not info.st_mode & 0o077
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        _directory(os.fstat(fd), path, site=site, private=private, shielded=shielded, explicitly_trusted=chosen is not None)
        yield (fd, shielded, grant is not None) if _details else fd
    finally:
        os.close(fd)


def read(path, *, limit=256 * 1024, site=False, private=False, user_only=False):
    """Return bytes and stat of one verified inode, rejecting in-place changes."""
    path = _user_path(path)
    with directory_fd(path.parent, site=site, _details=True) as (parent, shielded, root_granted):
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            owners = {0} if site else {os.getuid()} if (private or user_only) else {0, os.getuid()}
            if not stat.S_ISREG(before.st_mode):
                raise TrustError(f"{path}: expected a regular file.")
            explicitly_trusted = not (site or private or user_only) and root_granted
            if before.st_uid not in owners and not explicitly_trusted:
                raise TrustError(f"{path}: file must belong to {'root' if site else 'the current user' if private or user_only else 'the current user or root'}.")
            if (before.st_mode & (0o077 if private else 0o022) if (site or private)
                    else unsafe_write(before, shielded=shielded or explicitly_trusted)):
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
