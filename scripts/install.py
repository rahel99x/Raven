#!/usr/bin/env python3
"""Install Raven in a user-owned venv without pipx, sudo, or shell edits.

Each installation is built and checked before a single current-release link is
switched. Existing executables belonging to other installations are preserved.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tarfile
import venv

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ("raven", "ravenl", "ravenh")
MARKER = ".raven-install.json"
MARKER_LIMIT = 64 * 1024
SOURCE_LIMIT = 256 * 1024 * 1024
SOURCE_FILES = 20000
_trust_spec = importlib.util.spec_from_file_location('_raven_install_trust', ROOT / 'raven/config_trust.py')
TRUST = importlib.util.module_from_spec(_trust_spec)
_trust_spec.loader.exec_module(TRUST)


class InstallError(RuntimeError):
    pass


class InstallInterrupted(InstallError):
    pass


def run(command, *, timeout=600, cwd=None):
    process = None
    try:
        process = subprocess.Popen([str(item) for item in command], cwd=ROOT if cwd is None else cwd, start_new_session=True)
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if process is not None:
            _stop(process)
        raise InstallError("Could not run " + shlex.join(map(str, command)) + ": " + str(exc)) from exc
    except BaseException:
        if process is not None:
            _stop(process)
        raise
    if process.returncode:
        raise InstallError("Command failed: " + shlex.join(map(str, command)))


def _stop(process):
    """Stop only the command session this installer created, then reap it."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT'):
            try:
                if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                    break
            except ChildProcessError:
                return
        time.sleep(0.02)
    # Keep the exited leader unreaped until the last group signal, so its PID
    # cannot be reused while a helper still owns a pipe or build directory.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _directory(path, *, create=False, owned=True):
    """Do not place persistent launchers through redirected directories."""
    try:
        if not create and not path.exists():
            ancestor = path.parent
            while not ancestor.exists():
                ancestor = ancestor.parent
            with TRUST.directory_fd(ancestor):
                return
        with TRUST.directory_fd(path, create=create) as descriptor:
            if owned and os.fstat(descriptor).st_uid != os.getuid():
                raise InstallError("Installation directories must belong to your user: " + str(path))
    except (OSError, ValueError) as exc:
        raise InstallError('Choose a real directory, not a symbolic link: ' + str(path) + '; ' + str(exc)) from exc


def _unique(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("Duplicate installer metadata field")
        value[key] = item
    return value


def _finite_number(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Installer metadata numbers must be finite")
    return value


def _absolute(value):
    return isinstance(value, str) and 0 < len(value) <= 8192 and "\x00" not in value and os.path.isabs(value) and os.path.abspath(value) == value


def read_marker(release, *, prefix=None, bin_dir=None):
    """Read bounded, integrity-checked v1 metadata, including older installers."""
    _directory(release)
    marker = release / MARKER
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or TRUST.unsafe_write(before, shielded=not release.stat().st_mode & 0o077) or before.st_size > MARKER_LIMIT):
            raise InstallError("The previous release has no trusted Raven installer marker.")
        raw = stream.read(MARKER_LIMIT + 1)
        after = os.fstat(stream.fileno())
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if len(raw) > MARKER_LIMIT or identity(before) != identity(after):
        raise InstallError("The previous installer marker changed or exceeded its read limit.")
    try:
        data = json.loads(raw, object_pairs_hook=_unique, parse_float=_finite_number,
                          parse_constant=_finite_number)
        pending, nodes = [(data, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 16 or nodes > 4096:
                raise ValueError("Installer metadata exceeds structural bounds")
            if isinstance(item, dict):
                pending.extend((value, depth + 1) for value in item.values())
            elif isinstance(item, list):
                pending.extend((value, depth + 1) for value in item)
        if not isinstance(data, dict) or data.get("schema") != "raven.user-install/v1" or not _absolute(data.get("source")):
            raise ValueError("Invalid installer schema or source")
        # Released v1 markers contained only schema/source and could be 0644.
        # New metadata is private; do not silently chmod an existing file.
        if any(key in data for key in ("prefix", "bin_dir", "terminal", "installed_commit")) and before.st_mode & 0o077:
            raise ValueError("Installer metadata must be private (mode 0600)")
        for key, expected in (("prefix", prefix), ("bin_dir", bin_dir)):
            if key in data and (not _absolute(data[key]) or expected is not None and data[key] != str(expected)):
                raise ValueError("Installer metadata disagrees with the " + key)
        if "terminal" in data and type(data["terminal"]) is not bool:
            raise ValueError("Invalid installer terminal preference")
        if data.get("source_dirty") is not None and type(data["source_dirty"]) is not bool:
            raise ValueError("Invalid installer source_dirty")
        commit = data.get("installed_commit")
        if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)):
            raise ValueError("Invalid installed Git commit")
        digest = data.get("source_snapshot_sha256")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Invalid source content digest")
        if data.get("previous_release") is not None and not _absolute(data["previous_release"]):
            raise ValueError("Invalid previous release")
        if data.get("previous_release") is not None and prefix is not None:
            previous = Path(data["previous_release"])
            if previous.parent != prefix or not previous.name.startswith("release-"):
                raise ValueError("Previous release is outside this installation")
        for key in ("source_ref", "source_remote"):
            if data.get(key) is not None and not _identifier(data[key]):
                raise ValueError("Invalid source identifier")
    except (ValueError, RecursionError) as exc:
        raise InstallError("The previous release has no valid Raven installer marker: " + str(exc)) from exc
    return data


def _identifier(value):
    return isinstance(value, str) and len(value) <= 1024 and bool(re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*", value))


def _git(*args):
    try:
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(ROOT), *args],
                                capture_output=True, text=True, timeout=5, check=False, env=_git_env())
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    return result.stdout.strip() if result.returncode == 0 and len(result.stdout) <= MARKER_LIMIT else None


def _git_env():
    # Caller repository overrides must not redirect provenance to another tree.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    return env


def source_metadata():
    if _git("rev-parse", "--show-toplevel") != str(ROOT):
        return dict(installed_commit=None, source_ref=None, source_remote=None, source_dirty=None)
    commit = _git("rev-parse", "--verify", "HEAD")
    if not commit or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        return dict(installed_commit=None, source_ref=None, source_remote=None, source_dirty=None)
    ref = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    ref = ref if _identifier(ref) else None
    remote = _git("config", "--get", "branch." + ref + ".remote") if ref else None
    # Store only the remote's identifier, never URLs or embedded credentials.
    remote = remote if _identifier(remote) else None
    dirty = _git("status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=all")
    ignored_inputs = _build_inputs() - _git_paths("ls-files", "--cached", "-z")
    return dict(installed_commit=commit, source_ref=ref, source_remote=remote,
                source_dirty=True if ignored_inputs else None if dirty is None else bool(dirty))


def _git_paths(*args):
    # Keep Git output off the Python heap until its bounded size is known.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(ROOT), *args],
                                    stdout=output, stderr=subprocess.DEVNULL, timeout=10, env=_git_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallError("Cannot inspect source file identities: " + str(exc)) from exc
        if result.returncode or output.tell() > 2 * 1024 * 1024:
            raise InstallError("Cannot inspect source files within the installer bounds.")
        output.seek(0)
        paths = {Path(os.fsdecode(item)) for item in output.read().split(b"\0") if item}
    if len(paths) > SOURCE_FILES or any(path.is_absolute() or ".." in path.parts for path in paths):
        raise InstallError("Source file list is unsafe or exceeds the installer bounds.")
    return paths


def _protected_paths(values):
    protected = {Path(".raven"), Path(".venv")}
    for value in values:
        path = Path(os.path.abspath(ROOT / Path(value).expanduser()))
        if path == ROOT or path in ROOT.parents:
            raise InstallError("Preserved configuration must not exempt the whole source checkout.")
        try:
            protected.add(path.relative_to(ROOT))
        except ValueError:
            pass  # An external configuration cannot excuse a source edit.
    return protected


def _check_pin(expected_commit, metadata, protected_paths):
    if expected_commit is None:
        return
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_commit):
        raise InstallError("--expected-commit needs the complete lowercase Git commit ID.")
    if metadata["installed_commit"] != expected_commit:
        raise InstallError("Source HEAD differs from the expected commit; the active release was preserved.")
    changed = _git_paths("diff", "--name-only", "-z", "HEAD", "--")
    changed |= _git_paths("ls-files", "--others", "--exclude-standard", "-z")
    changed |= _build_inputs() - _git_paths("ls-files", "--cached", "-z")
    unexpected = [path for path in changed if not any(path == kept or kept in path.parents for kept in protected_paths)]
    if unexpected:
        raise InstallError("Source has changes outside preserved configuration; the active release was preserved: " +
                           ", ".join(repr(str(path)) for path in sorted(unexpected)[:5]))


def _walk_source(directory, *, package=False):
    _directory(directory, owned=False)
    excluded = {".git", "__pycache__"} if package else {
        ".git", ".venv", "__pycache__", "build", "dist", ".pytest_cache", ".mypy_cache", ".ruff_cache", "htmlcov"}
    paths, directories = set(), 0
    for parent, dirs, files in os.walk(directory, followlinks=False):
        directories += 1
        if directories > SOURCE_FILES:
            raise InstallError("Source exceeds the installer directory-count bound.")
        dirs[:] = [name for name in dirs if name not in excluded and not name.endswith(".egg-info")
                   and not (Path(parent) == ROOT and name == ".raven")]
        if any((Path(parent) / name).is_symlink() for name in dirs):
            raise InstallError("Source directories must not be symbolic links.")
        for name in files:
            if name.endswith((".pyc", ".pyo")) or name == ".DS_Store":
                continue
            paths.add((Path(parent) / name).relative_to(ROOT))
            if len(paths) > SOURCE_FILES:
                raise InstallError("Source exceeds the installer file-count bound.")
    return paths


def _build_inputs():
    # Git ignore rules do not stop setuptools from packaging Python files or
    # executing an existing setup.py. Inspect these even when locally ignored.
    package = ROOT / "raven"
    paths = _walk_source(package, package=True) if os.path.lexists(package) else set()
    for name in ("pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in", "README.md", "LICENSE"):
        if os.path.lexists(ROOT / name):
            paths.add(Path(name))
    return paths


def source_snapshot(protected_paths=()):
    """Fingerprint bounded source inputs; never scan a venv or recording store."""
    _directory(ROOT, owned=False)
    if _git("rev-parse", "--show-toplevel") == str(ROOT):
        paths = _git_paths("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    else:
        paths = _walk_source(ROOT)
    paths |= _build_inputs()
    for relative in protected_paths:
        path = ROOT / relative
        if relative in (Path(".raven"), Path(".venv")):
            continue
        if path.is_dir():
            paths |= _walk_source(path)
        elif os.path.lexists(path):
            paths.add(relative)
    if len(paths) > SOURCE_FILES:
        raise InstallError("Source exceeds the installer file-count bound.")
    digest, total, checked = hashlib.sha256(), 0, set()
    shared = any(ROOT == root or root in ROOT.parents for root in TRUST._trusted_roots())
    with TRUST.directory_fd(ROOT, _details=True) as (_, source_private, _):
        pass
    problems = TRUST.audit_directories({(ROOT / relative).parent for relative in paths})
    for relative in sorted(paths):
        path = ROOT / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            problems[path] = str(path) + ': expected a regular source file, not a link or special file.'
        elif TRUST.unsafe_write(info, shielded=shared or source_private):
            problems[path] = str(path) + ': source file is group/world writable.'
        elif info.st_uid not in {0, os.getuid()} and not shared:
            problems[path] = str(path) + ': source file has an untrusted owner.'
    if problems:
        raise InstallError(TRUST.permission_report(problems))
    for relative in sorted(paths):
        path = ROOT / relative
        if path.parent not in checked:
            _directory(path.parent, owned=False)
            checked.add(path.parent)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            digest.update(b'missing\0' + os.fsencode(relative) + b'\0')
            continue
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024 * 1024
                    or before.st_uid not in {0, os.getuid()} and not shared or TRUST.unsafe_write(before, shielded=shared or source_private)):
                raise InstallError("Source input is not a bounded regular file: " + str(relative))
            name = os.fsencode(relative)
            digest.update(len(name).to_bytes(4, "big") + name + (before.st_mode & 0o777).to_bytes(4, "big")
                          + before.st_size.to_bytes(8, "big"))
            consumed = 0
            while True:
                block = stream.read(65536)
                if not block:
                    break
                total += len(block); consumed += len(block)
                if total > SOURCE_LIMIT:
                    raise InstallError("Source exceeds the 256 MiB installer input bound.")
                digest.update(block)
            after = os.fstat(stream.fileno())
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if consumed != before.st_size or identity(before) != identity(after):
                raise InstallError("Source input changed while reading: " + str(relative))
    return digest.hexdigest()


def _write_marker(stage, data):
    raw = (json.dumps(data, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MARKER_LIMIT:
        raise InstallError("Installer metadata exceeds its size limit.")
    fd = os.open(stage / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _stage_source(stage, metadata):
    """Build in a pristine private tree, never setuptools' stale ROOT/build."""
    destination = stage / '.source'
    destination.mkdir(mode=0o700)
    head = _git('rev-parse', '--verify', 'HEAD')
    if head and not metadata.get('source_dirty'):
        archive = stage / '.source.tar'
        try:
            process = subprocess.Popen(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(ROOT),
                'archive', '--format=tar', '--output=' + str(archive), head], env=_git_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
            try:
                _, errors = process.communicate(timeout=600)
            except BaseException:
                _stop(process)
                raise
            if process.returncode or archive.stat().st_size > SOURCE_LIMIT + SOURCE_FILES * 2048:
                raise InstallError('Cannot create a bounded pristine source archive.')
            with tarfile.open(archive) as source:
                members = source.getmembers()
                if len(members) > SOURCE_FILES * 2:
                    raise InstallError('Pristine source archive exceeds its member bound.')
                for member in members:
                    relative = Path(member.name)
                    if relative.is_absolute() or '..' in relative.parts or not (member.isdir() or member.isfile()):
                        raise InstallError('Pristine source archive contains an unsafe path or link.')
                    target = destination / relative
                    if member.isdir():
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with source.extractfile(member) as incoming, target.open('xb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
            expected = _git_paths('ls-tree', '-r', '--name-only', '-z', head, '--', 'raven')
            missing = [path for path in expected if (path.suffix in {'.py', '.pyi'} or path.name == 'py.typed')
                       and not (destination / path).is_file()]
            if missing:
                raise InstallError('Pristine archive omits tracked package inputs: ' + ', '.join(str(path) for path in sorted(missing)[:8]))
        finally:
            archive.unlink(missing_ok=True)
    else:
        # A deliberate first install from edited source stays supported. Copy
        # only verified inputs, never build caches or pre-existing egg-info.
        paths = (_git_paths('ls-files', '--cached', '--others', '--exclude-standard', '-z')
                 if _git('rev-parse', '--show-toplevel') == str(ROOT) else _walk_source(ROOT)) | _build_inputs()
        paths = {path for path in paths if not path.parts or path.parts[0] not in {'.raven', '.venv', 'venv', '.tox', '.nox', 'build', 'dist'}}
        consumed = 0
        for relative in sorted(paths):
            if not (ROOT / relative).exists():
                continue
            raw, info = TRUST.read(ROOT / relative, limit=64 << 20)
            consumed += len(raw)
            if consumed > SOURCE_LIMIT:
                raise InstallError('Pristine build source exceeds its byte bound.')
            target = destination / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(raw)
            target.chmod(0o755 if info.st_mode & 0o111 else 0o644)
    return destination


def _prune_releases(prefix, keep):
    """Prune only complete installer-owned releases under the held lock."""
    # Source backups refer to exact retained releases. The update coordinator
    # prunes those together; a standalone install must not strand a backup.
    if (prefix / 'backups').is_dir():
        return
    for candidate in prefix.glob('release-*'):
        if candidate in keep or candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            read_marker(candidate, prefix=prefix)
        except (OSError, ValueError, InstallError):
            continue
        shutil.rmtree(candidate)


def _sweep_staging(prefix):
    """Remove only identifiable, abandoned stages while the install lock is held."""
    for candidate in list(prefix.glob('release-*'))[:256]:
        if candidate.is_symlink() or not candidate.is_dir() or (candidate / MARKER).exists():
            continue
        try:
            raw, _ = TRUST.read(candidate / '.raven-staging.json', private=True, user_only=True, limit=4096)
            marker = json.loads(raw)
            if marker.get('schema') != 'raven.install-stage/v1' or marker.get('path') != str(candidate):
                continue
            pid = marker.get('pid')
            if type(pid) is not int or pid <= 1 or time.time() - marker.get('created', time.time()) < 3600:
                continue
            try:
                os.kill(pid, 0)
                continue
            except ProcessLookupError:
                pass
            shutil.rmtree(candidate)
        except (OSError, ValueError, TypeError):
            continue


def _owned_link(path, target):
    if not os.path.lexists(path):
        return False
    if (not path.is_symlink() or path.lstat().st_uid != os.getuid() or
            os.path.abspath(path.parent / os.readlink(path)) != str(target)):
        raise InstallError("Existing command preserved: " + str(path) +
                           ". Update it with its original installer (for example pipx install --force '.[terminal]'), "
                           "or choose a different --bin-dir.")
    return True


def inspect_destination(prefix, bin_dir):
    problems = TRUST.audit_directories((prefix, bin_dir, ROOT))
    if problems:
        raise InstallError(TRUST.permission_report(problems))
    _directory(prefix)
    _directory(bin_dir)
    current = prefix / "current"
    previous = None
    if os.path.lexists(current):
        if not current.is_symlink() or current.lstat().st_uid != os.getuid():
            raise InstallError("Existing installation path preserved: " + str(current))
        previous = current.resolve()
        if previous.parent != prefix or not previous.name.startswith("release-"):
            raise InstallError("The current installation is not owned by this installer: " + str(current))
        read_marker(previous, prefix=prefix, bin_dir=bin_dir)
    for name in COMMANDS:
        _owned_link(bin_dir / name, prefix / "current" / "bin" / name)
    return previous


def _restore_current(prefix, previous):
    """Undo this install's interrupted switch while retaining its lock."""
    if previous is None:
        (prefix / "current").unlink()
        return
    fd, name = tempfile.mkstemp(prefix=".restore-", dir=prefix)
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.unlink()
        temporary.symlink_to(previous.name)
        os.replace(temporary, prefix / "current")
    finally:
        temporary.unlink(missing_ok=True)


def install(prefix, bin_dir, *, terminal=True, expected_commit=None, expected_protected_paths=()):
    _directory(ROOT, owned=False)
    prefix, bin_dir = Path(os.path.abspath(prefix)), Path(os.path.abspath(bin_dir))
    if expected_protected_paths and expected_commit is None:
        raise InstallError("--preserve-config requires --expected-commit.")
    protected = _protected_paths(expected_protected_paths)
    if expected_commit is not None:
        _check_pin(expected_commit, source_metadata(), protected)
    inspect_destination(prefix, bin_dir)
    _directory(prefix, create=True)
    _directory(bin_dir, create=True)
    lock = os.open(prefix / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    stage = None
    created = []
    published = False
    temporary_link = None
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise InstallError("The installer lock must be a private file owned by your user.")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("Another Raven installation is in progress.") from exc
        previous = inspect_destination(prefix, bin_dir)
        _sweep_staging(prefix)
        metadata = source_metadata()
        _check_pin(expected_commit, metadata, protected)
        snapshot = source_snapshot(protected)
        stage = Path(tempfile.mkdtemp(prefix="release-", dir=prefix))
        fd = os.open(stage / '.raven-staging.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'schema':'raven.install-stage/v1', 'path':str(stage), 'pid':os.getpid(), 'created':time.time()}, stream)
        print("Creating isolated Raven environment: " + str(stage), flush=True)
        try:
            venv.EnvBuilder(with_pip=True, symlinks=True).create(stage)
        except InstallInterrupted:
            raise
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            raise InstallError("Python could not create an environment with pip. Use a Python 3.10+ installation with venv/ensurepip, "
                               "or use python3 scripts/setup.py --mode demo for the offline checkout demo. " + str(exc)) from exc
        python = stage / "bin/python"
        build_root = _stage_source(stage, metadata)
        package = str(build_root) + ("[terminal]" if terminal else "")
        run([python, "-m", "pip", "install", "--disable-pip-version-check", package])
        expected_payload = sorted(str(path.relative_to(build_root)) for path in (build_root / 'raven').rglob('*')
                                  if path.is_file() and (path.suffix in {'.py', '.pyi'} or path.name == 'py.typed'))
        run([python, "-I", "-c", "import curses,pathlib,sys,raven; "
             "assert pathlib.Path(raven.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()); "
             "from importlib.metadata import distribution; "
             "actual={str(path) for path in distribution('raven').files if str(path).startswith('raven/') and not str(path).endswith(('.pyc','.pyo'))}; "
             "assert actual == set(" + repr(expected_payload) + "), 'Installed RECORD differs from the pristine package source'; "
             "print('Raven ' + raven.__version__)"], cwd=stage)
        for name in COMMANDS:
            run([stage / "bin" / name, "--version"], timeout=30)
        current_metadata = source_metadata()
        _check_pin(expected_commit, current_metadata, protected)
        if current_metadata != metadata or source_snapshot(protected) != snapshot:
            raise InstallError("The source checkout changed while building. Retry from a stable checkout.")
        _write_marker(stage, dict(schema="raven.user-install/v1", source=str(ROOT), prefix=str(prefix),
                                 bin_dir=str(bin_dir), terminal=bool(terminal),
                                 source_snapshot_sha256=snapshot,
                                 previous_release=str(previous) if previous else None, **metadata))
        (stage / '.raven-staging.json').unlink()
        shutil.rmtree(build_root)
        # Every public command points through one atomically switched release.
        # New links use exclusive creation; a concurrent external installer is
        # never overwritten, even after the initial destination check.
        for name in COMMANDS:
            path, target = bin_dir / name, prefix / "current" / "bin" / name
            if not _owned_link(path, target):
                path.symlink_to(target)
                created.append(path)
        fd, temporary = tempfile.mkstemp(prefix=".current-", dir=prefix)
        os.close(fd)
        temporary_link = Path(temporary)
        temporary_link.unlink()
        temporary_link.symlink_to(stage.name)
        # The private installer lock serializes this installer's writers; check
        # the old destination again before publishing the fully built release.
        if inspect_destination(prefix, bin_dir) != previous:
            raise InstallError("The active installation changed while building. Retry after the other update finishes.")
        current_lock = (prefix / ".install.lock").lstat()
        if (not stat.S_ISREG(current_lock.st_mode) or current_lock.st_nlink != 1 or
                (current_lock.st_dev, current_lock.st_ino) != (info.st_dev, info.st_ino)):
            raise InstallError("The installer lock changed while building; the active release was preserved.")
        os.replace(temporary_link, prefix / "current")
        temporary_link = None
        published = True
        _prune_releases(prefix, {stage, previous})
        return {"release": stage, "previous": previous, "command": bin_dir / "raven"}
    except BaseException:
        current = prefix / "current"
        if (stage is not None and current.is_symlink() and current.lstat().st_uid == os.getuid()
                and os.path.abspath(current.parent / os.readlink(current)) == str(stage)):
            # A signal can arrive after replace() but before `published = True`.
            # Preserve a functioning selected release even if rollback itself
            # fails; never remove a directory still selected by current.
            published = True
            _restore_current(prefix, previous)
            published = False
        raise
    finally:
        try:
            if temporary_link is not None:
                temporary_link.unlink(missing_ok=True)
            if not published:
                for path in created:
                    target = prefix / "current" / "bin" / path.name
                    if path.is_symlink() and os.path.abspath(path.parent / os.readlink(path)) == str(target):
                        path.unlink()
                if stage is not None:
                    shutil.rmtree(stage)
        finally:
            os.close(lock)


def parser():
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", type=Path, default=data / "raven/installations", help="Dedicated user-owned release directory")
    ap.add_argument("--bin-dir", type=Path, default=home / ".local/bin", help="Public launcher directory; existing unrelated commands are preserved")
    ap.add_argument("--without-terminal", action="store_true", help="Skip optional embedded-terminal dependencies")
    ap.add_argument("--no-setup", action="store_true", help="Do not enter guided setup after installing")
    ap.add_argument("--expected-commit", metavar="SHA", help="Require this exact source Git HEAD before building and publishing")
    ap.add_argument("--preserve-config", action="append", default=[], type=Path, metavar="PATH",
                    help="Allow this exact local configuration path with --expected-commit (repeatable)")
    ap.add_argument("--plan", action="store_true", help="Check destinations and show the installation plan without writing")
    return ap


def main(argv=None):
    os.umask(0o022)
    args = parser().parse_args(argv)
    previous_handlers = {}
    def interrupt(signum, frame):
        # Popen.wait gives KeyboardInterrupt a grace wait that may reap the
        # leader before group cleanup. Keep ownership until run() stops it.
        raise InstallInterrupted("Installation interrupted")
    try:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
            previous_handlers[signum] = signal.signal(signum, interrupt)
    except ValueError:  # An embedding may call main outside its main thread.
        pass
    try:
        if sys.version_info < (3, 10) or os.name != "posix":
            raise InstallError("Use Python 3.10 or later on Linux, macOS, or WSL.")
        _directory(ROOT, owned=False)
        prefix, bin_dir = args.prefix.expanduser().resolve(), args.bin_dir.expanduser().resolve()
        if prefix == ROOT or prefix in ROOT.parents or bin_dir == prefix or prefix in bin_dir.parents:
            raise InstallError("Use dedicated release and launcher directories outside the checkout's parents.")
        previous = inspect_destination(prefix, bin_dir)
        if args.preserve_config and args.expected_commit is None:
            raise InstallError("--preserve-config requires --expected-commit.")
        if args.expected_commit is not None:
            _check_pin(args.expected_commit, source_metadata(), _protected_paths(args.preserve_config))
        print("Raven releases: " + str(prefix) + "\nCommands: " + str(bin_dir))
        print("Your configuration, shell startup files, and existing releases are preserved.")
        if args.plan:
            source_snapshot(_protected_paths(args.preserve_config))
            print("Ready to install" + ("; previous release: " + str(previous) if previous else ""))
            return 0
        result = install(prefix, bin_dir, terminal=not args.without_terminal,
                         expected_commit=args.expected_commit, expected_protected_paths=args.preserve_config)
        print("\nInstalled. Start setup with:\n  " + shlex.join([str(result["command"]), "setup"]))
        print("Worker Python on this host: " + str(prefix / "current/bin/python"))
        path_entries = {os.path.abspath(os.path.expanduser(part)) for part in os.environ.get("PATH", "").split(os.pathsep) if part}
        if str(bin_dir) not in path_entries:
            print("\nThe launcher directory is not on PATH. Use the absolute command above, or enable it in this shell:\n  " +
                  "export PATH=" + shlex.quote(str(bin_dir)) + ':"$PATH"')
        if result["previous"]:
            print("Previous release retained: " + str(result["previous"]))
        if not args.no_setup and sys.stdin.isatty() and sys.stdout.isatty():
            return subprocess.call([str(result["command"]), "setup"])
        return 0
    except (KeyboardInterrupt, InstallInterrupted):
        print("Raven installation interrupted; unpublished work was cleaned up.", file=sys.stderr)
        return 130
    except (OSError, ValueError, InstallError) as exc:
        print("Raven installation: " + str(exc), file=sys.stderr)
        return 2
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
