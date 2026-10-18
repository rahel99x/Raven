"""Bounded updates of one explicitly configured Git tracking branch.

The installation coordinator owns the shared update lock and release switch.
This module changes source only; a subsequent installation failure must leave
the previous managed executable active. Backups are independent of the checkout.
"""
from __future__ import annotations

from bisect import bisect_left
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import subprocess
import threading
import time
import uuid

from . import config_trust as T

MAX_BACKUP_BYTES = 256 << 20
MAX_FILE_BYTES = 64 << 20
MAX_FILES = 50000
MAX_OUTPUT = 16 << 20
MAX_METADATA = 64 << 10
COMMAND_TIMEOUT = 60
RUNTIME_PATHS = (".raven", ".venv", "venv", ".tox", ".nox")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REDIRECT_ENV = frozenset({"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_IMPLICIT_WORK_TREE",
    "GIT_REPLACE_REF_BASE", "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE", "GIT_QUARANTINE_PATH", "GIT_CONFIG"})
_REDIRECT_CONFIG = frozenset({"core.worktree", "core.bare"})


class SourceUpdateError(ValueError):
    """An update was refused, without including remote URLs or Git stderr."""

    def __init__(self, message, *, backup=None):
        super().__init__(message)
        self.backup = backup


def _absolute(path):
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _relative(path):
    if (not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path or "\\" in path
            or path.startswith("/") or any(p in ("", ".", "..", ".git") for p in path.split("/"))):
        raise SourceUpdateError("Git returned an unsupported or unsafe source path")
    return path


def _within(path, parent):
    return path == parent or parent in path.parents


def _beneath(path, prefix):
    return path == prefix or path.startswith(prefix + "/")


def _trusted_directory(path, *, private=False, create=False):
    with T.directory_fd(path, private=private, create=create) as fd:
        if os.fstat(fd).st_uid != os.getuid():
            raise SourceUpdateError("The source and backup directories must belong to the current user")


def _git_environment():
    """Retain normal credentials/network configuration, isolate checkout selectors."""
    env = {key: value for key, value in os.environ.items() if key not in _REDIRECT_ENV}
    count = env.get("GIT_CONFIG_COUNT")
    if count is not None:
        if not count.isdigit() or len(count) > 3 or int(count) > 128:
            raise SourceUpdateError("Injected Git configuration exceeds its entry bound")
        entries = []
        total = 0
        for index in range(int(count)):
            key = env.get("GIT_CONFIG_KEY_" + str(index))
            value = env.get("GIT_CONFIG_VALUE_" + str(index))
            if key is None or value is None:
                raise SourceUpdateError("Injected Git configuration is incomplete")
            total += len(key) + len(value)
            if total > MAX_METADATA:
                raise SourceUpdateError("Injected Git configuration exceeds its byte bound")
            if key.lower() not in _REDIRECT_CONFIG:
                entries.append((key, value))
        env = {key: value for key, value in env.items() if not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
        env["GIT_CONFIG_COUNT"] = str(len(entries))
        for index, (key, value) in enumerate(entries):
            env["GIT_CONFIG_KEY_" + str(index)] = key
            env["GIT_CONFIG_VALUE_" + str(index)] = value
    parameters = env.get("GIT_CONFIG_PARAMETERS")
    if parameters:
        if len(parameters) > MAX_METADATA:
            raise SourceUpdateError("Injected Git configuration exceeds its byte bound")
        try:
            values = shlex.split(parameters)
        except ValueError as exc:
            raise SourceUpdateError("Injected Git configuration is malformed") from exc
        env["GIT_CONFIG_PARAMETERS"] = " ".join("'" + value.replace("'", "'\\''") + "'" for value in values
            if value.split("=", 1)[0].lower() not in _REDIRECT_CONFIG)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat", GIT_NO_REPLACE_OBJECTS="1")
    return env


def git(source, *arguments, index_file=None):
    """Run fixed Git arguments with bounded output/time and isolated source selectors."""
    env = _git_environment()
    if index_file is not None:
        index_file = _absolute(index_file)
        T.read(index_file, user_only=True, limit=MAX_FILE_BYTES)
        env["GIT_INDEX_FILE"] = str(index_file)
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-c", "protocol.ext.allow=never", "-c", "maintenance.auto=false",
               "-c", "gc.auto=0", "-C", str(source), *map(str, arguments)]
    process = None
    output = bytearray()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        deadline = time.monotonic() + COMMAND_TIMEOUT
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise SourceUpdateError("Git command timed out; source update stopped")
                for key, _ in selector.select(min(.2, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        raise SourceUpdateError("Git response exceeded its bounded output budget")
            process.wait(timeout=max(.01, deadline - time.monotonic()))
        if process.returncode:
            operation = str(arguments[0]) if arguments else "operation"
            raise SourceUpdateError(f"Git {operation} failed (exit {process.returncode}); check repository access and tracking configuration")
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceUpdateError("Git could not complete; check that Git is installed and the repository is accessible") from exc
    finally:
        if process is not None:
            # Do not poll here: an exited leader may still have a helper holding
            # stdout. Keep its unreaped PID reserved until the owned group has
            # been signalled, then reap the leader.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            if process.stdout is not None:
                process.stdout.close()


def _text(source, *args):
    try:
        return git(source, *args).decode("utf-8").strip()
    except UnicodeError as exc:
        raise SourceUpdateError("Git metadata is not valid UTF-8") from exc


def _paths(source, *args):
    try:
        paths = [_relative(value) for value in git(source, *args).decode("utf-8").split("\x00") if value]
    except UnicodeError as exc:
        raise SourceUpdateError("Git source paths must use UTF-8") from exc
    if len(paths) > MAX_FILES:
        raise SourceUpdateError("Source file inventory exceeds its count budget")
    return sorted(set(paths))


def _sha(value):
    if not _SHA.fullmatch(value):
        raise SourceUpdateError("Git did not return an exact commit identity")
    return value


def _repo(source):
    source = _absolute(source)
    _trusted_directory(source)
    dotgit = source / ".git"
    if dotgit.is_symlink():
        raise SourceUpdateError("A symbolic-link Git directory cannot be updated")
    if dotgit.is_file():
        raw, _ = T.read(dotgit, user_only=True, limit=4096)
        if not raw.startswith(b"gitdir: "):
            raise SourceUpdateError("Unsupported Git worktree directory file")
        try:
            git_dir = _absolute(source / raw[8:].decode("utf-8").strip())
        except UnicodeError as exc:
            raise SourceUpdateError("Invalid Git worktree directory") from exc
    else:
        git_dir = dotgit
    _trusted_directory(git_dir)
    common = git_dir
    if (git_dir / "commondir").exists() or (git_dir / "commondir").is_symlink():
        raw, _ = T.read(git_dir / "commondir", user_only=True, limit=4096)
        common = _absolute(git_dir / raw.decode("utf-8").strip())
        _trusted_directory(common)
    for candidate in (git_dir / "config", common / "config", git_dir / "config.worktree", git_dir / "HEAD"):
        if candidate.exists() or candidate.is_symlink():
            T.read(candidate, user_only=True, limit=MAX_FILE_BYTES)
    if _text(source, "rev-parse", "--show-toplevel") != str(source):
        raise SourceUpdateError("Select the exact checkout root, not a nested directory")
    if _text(source, "rev-parse", "--absolute-git-dir") != str(git_dir):
        raise SourceUpdateError("Git directory identity changed")
    if _absolute(source / _text(source, "rev-parse", "--git-common-dir")) != common:
        raise SourceUpdateError("Git common-directory identity changed")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "index.lock"):
        if (git_dir / marker).exists():
            raise SourceUpdateError("Finish the existing Git operation before updating Raven")
    return source, git_dir


def _protected(source, protected_paths):
    if not isinstance(protected_paths, (list, tuple)) or len(protected_paths) > 128:
        raise SourceUpdateError("Protected configuration must be a bounded list of paths")
    absolute, relative = [], []
    for value in protected_paths:
        path = _absolute(value)
        if path == source or _within(path, source / ".git"):
            raise SourceUpdateError("Protect individual configuration paths, not the checkout or Git directory")
        if path.is_symlink():
            raise SourceUpdateError("Protected configuration cannot be a symbolic link")
        if path.exists():
            if path.is_dir():
                with T.directory_fd(path):
                    pass
            else:
                T.read(path, user_only=_within(path, source), limit=MAX_FILE_BYTES)
        else:
            ancestor = path.parent
            while not ancestor.exists():
                if ancestor.is_symlink():
                    raise SourceUpdateError("Protected configuration has a symbolic-link ancestor")
                ancestor = ancestor.parent
            with T.directory_fd(ancestor):
                pass
        absolute.append(str(path))
        if _within(path, source):
            relative.append(_relative(path.relative_to(source).as_posix()))
    return sorted(set(absolute)), sorted(set(relative))


def _relationship(source, head, target):
    values = _text(source, "rev-list", "--left-right", "--count", head + "..." + target).split()
    if len(values) != 2 or any(not value.isdigit() for value in values):
        raise SourceUpdateError("Git could not determine branch ancestry")
    return tuple(map(int, values))


def inspect_source(source, *, protected_paths=()):
    """Network-free branch, ownership and working-tree inspection."""
    source, git_dir = _repo(source)
    branch = _text(source, "symbolic-ref", "--quiet", "--short", "HEAD")
    git(source, "check-ref-format", "refs/heads/" + branch)
    remote = _text(source, "config", "--get-all", "branch." + branch + ".remote")
    tracking = _text(source, "config", "--get-all", "branch." + branch + ".merge")
    if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", remote) or remote == "."
            or not tracking.startswith("refs/heads/") or "\n" in tracking):
        raise SourceUpdateError("The current branch needs one unambiguous named remote tracking branch")
    git(source, "check-ref-format", tracking)
    local_ref = _text(source, "rev-parse", "--symbolic-full-name", "@{upstream}")
    if not local_ref.startswith("refs/remotes/"):
        raise SourceUpdateError("The current tracking reference must be a remote branch")
    head = _sha(_text(source, "rev-parse", "HEAD^{commit}"))
    target = _sha(_text(source, "rev-parse", local_ref + "^{commit}"))
    tracked = _paths(source, "ls-files", "--cached", "-z")
    for row in git(source, "ls-files", "-v", "-z").split(b"\x00"):
        if row and (row[:1] == b"S" or row[:1].islower()):
            raise SourceUpdateError("Clear sparse/skip-worktree/assume-unchanged index flags before updating")
    if git(source, "ls-files", "--unmerged", "-z"):
        raise SourceUpdateError("Resolve the unmerged Git index before updating")
    protected, relatives = _protected(source, protected_paths)
    untracked = [p for p in _paths(source, "ls-files", "--others", "--exclude-standard", "-z")
                 if not any(_beneath(p, runtime) for runtime in RUNTIME_PATHS)]
    changed = set(_paths(source, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--"))
    changed.update(_paths(source, "diff", "--no-ext-diff", "--no-textconv", "--cached", "--name-only", "-z", "HEAD", "--"))
    ahead, behind = _relationship(source, head, target)
    source_dirty = any(not any(_beneath(path, prefix) for prefix in relatives)
                       for path in changed | set(untracked))
    return {"source": str(source), "git_dir": str(git_dir), "branch": branch,
            "tracking_remote": remote, "tracking_ref": tracking, "tracking_local_ref": local_ref,
            "original_head": head, "target_head": target, "dirty": bool(changed or untracked), "source_dirty": source_dirty,
            "ahead": ahead, "behind": behind, "divergent": bool(ahead and behind),
            "tracked_paths": tracked, "untracked_paths": untracked,
            "protected_paths": protected, "protected_relatives": relatives,
            "excluded_runtime_paths": list(RUNTIME_PATHS)}


def _entry(source, relative):
    path = source / _relative(relative)
    if not path.parent.exists():
        ancestor = path.parent
        while not ancestor.exists():
            if ancestor.is_symlink():
                raise SourceUpdateError("Source paths cannot traverse symbolic-link ancestors")
            ancestor = ancestor.parent
        with T.directory_fd(ancestor):
            pass
        return None, None
    with T.directory_fd(path.parent):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None, None
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != os.getuid():
                raise SourceUpdateError("Source symlinks must belong to the current user")
            raw = os.readlink(path).encode("utf-8")
            kind = "symlink"
        elif stat.S_ISREG(info.st_mode):
            raw, info = T.read(path, user_only=True, limit=MAX_FILE_BYTES)
            kind = "file"
        else:
            raise SourceUpdateError("Source backups support regular files and symlink leaves only")
    return {"path": relative, "kind": kind, "mode": stat.S_IMODE(info.st_mode), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "storage": "files/" + relative}, raw


def _walk_protected(source, relative):
    path = source / relative
    if not path.exists():
        return [relative]
    if not path.is_dir():
        return [relative]
    result, directories, visited = [], [path], 0
    while directories:
        directory = directories.pop()
        _trusted_directory(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_FILES:
                    raise SourceUpdateError("Protected configuration exceeds the backup file budget")
                kind = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(kind):
                    directories.append(Path(entry.path))
                elif stat.S_ISREG(kind):
                    result.append(Path(entry.path).relative_to(source).as_posix())
                else:
                    raise SourceUpdateError("Protected configuration trees must contain real directories and regular files")
    return result


def _snapshot(info, *, destination=None):
    source = Path(info["source"])
    paths = set(info["tracked_paths"] + info["untracked_paths"])
    for relative in info["protected_relatives"]:
        paths.update(_walk_protected(source, relative))
    if len(paths) > MAX_FILES:
        raise SourceUpdateError("Source backup exceeds its file-count budget")
    entries, missing, total = [], [], 0
    for relative in sorted(paths):
        entry, raw = _entry(source, relative)
        if entry is None:
            missing.append(relative)
            continue
        total += len(raw)
        if total > MAX_BACKUP_BYTES:
            raise SourceUpdateError("Source backup exceeds its 256 MiB budget; move large artifacts out of the checkout")
        entries.append(entry)
        if destination is not None:
            _write(destination / entry["storage"], raw)
    index = None
    index_path = Path(info["git_dir"]) / "index"
    if index_path.exists() or index_path.is_symlink():
        raw, _ = T.read(index_path, user_only=True, limit=MAX_FILE_BYTES)
        total += len(raw)
        if total > MAX_BACKUP_BYTES:
            raise SourceUpdateError("Source and index exceed the backup byte budget")
        index = {"storage": "git-index", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if destination is not None:
            _write(destination / "git-index", raw)
    tracked = set(info["tracked_paths"])
    return {"files": entries, "deleted_tracked_paths": [p for p in missing if p in tracked],
            "missing_paths": missing, "index": index, "backup_bytes": total}


def _write(path, raw):
    with T.directory_fd(path.parent, create=True) as parent:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)


def _json(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def create_backup(source, *, backup_root=None, protected_paths=(), target_head=None, installation=None):
    """Publish a complete private snapshot before any source mutation."""
    info = inspect_source(source, protected_paths=protected_paths)
    metadata = {} if installation is None else installation
    if not isinstance(metadata, dict) or len(_json(metadata)) > MAX_METADATA:
        raise SourceUpdateError("Installation backup metadata must be a bounded JSON object")
    target = info["target_head"] if target_head is None else _sha(target_head)
    source = Path(info["source"])
    root = _absolute(backup_root) if backup_root is not None else source.parent / ".raven-update-backups"
    if _within(root, source):
        raise SourceUpdateError("Source backups must be outside the checkout")
    _trusted_directory(root, private=True, create=True)
    name = "raven-source-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    destination = root / name
    destination.mkdir(mode=0o700)
    snapshot = _snapshot(info, destination=destination)
    ref = "refs/raven/update-backups/" + name
    manifest = {**info, **snapshot, "schema": "raven.source-backup/v1", "target_head": target,
                "created_utc": datetime.now(timezone.utc).isoformat(), "retained_ref": ref, "installation": metadata}
    assert_unchanged(source, manifest)
    raw = _json(manifest)
    if len(raw) > MAX_OUTPUT:
        raise SourceUpdateError("Source backup manifest exceeds its bound")
    git(source, "update-ref", ref, info["original_head"], "0" * len(info["original_head"]))
    _write(destination / "manifest.json", raw)
    _write(destination / "COMPLETE.json", _json({"schema": "raven.source-backup-complete/v1", "manifest_sha256": hashlib.sha256(raw).hexdigest()}))
    for directory, _, _ in os.walk(destination / "files", topdown=False):
        with T.directory_fd(directory) as fd:
            os.fsync(fd)
    with T.directory_fd(destination) as fd:
        os.fsync(fd)
    return {"path": str(destination), "manifest": manifest}


def assert_unchanged(source, manifest):
    """Check working bytes, index, branch and tracking identity immediately before mutation."""
    current = inspect_source(source, protected_paths=manifest["protected_paths"])
    fields = ("source", "git_dir", "branch", "tracking_remote", "tracking_ref", "tracking_local_ref",
              "original_head", "tracked_paths", "untracked_paths", "protected_paths", "protected_relatives")
    snapshot = _snapshot(current)
    if any(current[field] != manifest[field] for field in fields) or any(
            snapshot[field] != manifest[field] for field in ("files", "missing_paths", "index")):
        raise SourceUpdateError("Source changed concurrently; no update was applied. Stop other Git/editor changes and retry")


def content_fingerprint(source, *, protected_paths=()):
    """Read-only bounded content identity for installation before/after checks.

    Git index bytes and timestamps are deliberately excluded: Git may refresh
    cached stat data without changing source contents. The caller binds HEAD.
    """
    snapshot = _snapshot(inspect_source(source, protected_paths=protected_paths))
    return hashlib.sha256(_json({key: snapshot[key] for key in ("files", "missing_paths")})).hexdigest()


def _target_safety(info, target):
    source = Path(info["source"])
    target_paths = _paths(source, "ls-tree", "-r", "--name-only", "-z", target)
    protected = info["protected_relatives"]
    ignored = [p for p in _paths(source, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
               if not any(_beneath(p, protected_path) for protected_path in protected)]
    ignored_set = set(ignored)
    for path in target_paths:
        if any(_beneath(path, prefix) or _beneath(prefix, path) for prefix in RUNTIME_PATHS):
            raise SourceUpdateError("The fetched source overlaps a protected runtime directory")
        if any(_beneath(prefix, path) and (prefix != path or (source / prefix).is_dir()) for prefix in protected):
            raise SourceUpdateError("The fetched source would replace a protected configuration directory")
        parts = path.split("/")
        at = bisect_left(ignored, path + "/")
        if (any("/".join(parts[:end]) in ignored_set for end in range(1, len(parts) + 1))
                or at < len(ignored) and ignored[at].startswith(path + "/")):
            raise SourceUpdateError("The fetched source would overwrite ignored local data; relocate it before updating")
    # A newly introduced parent symlink must never redirect configuration restore.
    kinds = git(source, "ls-tree", "-r", "-z", target).split(b"\x00")
    links = []
    for row in kinds:
        if row.startswith((b"120000 ", b"160000 ")):
            links.append(row.split(b"\t", 1)[1].decode("utf-8"))
    if any(_beneath(p, link) for p in protected for link in links):
        raise SourceUpdateError("Fetched source would redirect a protected configuration path")


def _restore_protected(source, backup):
    manifest = backup["manifest"]
    protected = manifest["protected_relatives"]
    for entry in manifest["files"]:
        if not any(_beneath(entry["path"], p) for p in protected):
            continue
        if entry["kind"] != "file":
            raise SourceUpdateError("Protected configuration cannot be restored through a symlink")
        raw, _ = T.read(Path(backup["path"]) / entry["storage"], user_only=True, limit=MAX_FILE_BYTES)
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise SourceUpdateError("Protected backup changed; recover from the retained backup before continuing")
        destination = source / entry["path"]
        with T.directory_fd(destination.parent, create=True) as parent:
            if destination.is_symlink():
                raise SourceUpdateError("Updated source redirected protected configuration")
            temporary = destination.name + ".raven-restore-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                os.chmod(temporary, entry["mode"], dir_fd=parent)
                os.replace(temporary, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
    for relative in manifest["missing_paths"]:
        if any(_beneath(relative, p) for p in protected):
            path = source / relative
            if not path.parent.exists():
                continue
            with T.directory_fd(path.parent) as parent:
                try:
                    os.unlink(path.name, dir_fd=parent)
                except FileNotFoundError:
                    pass


@contextmanager
def _mutation_signals():
    """Allow SIGTERM, like Ctrl-C, to unwind through configuration restoration."""
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGTERM)
        def interrupted(signum, frame):
            raise SourceUpdateError("Source update interrupted by termination request")
        signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def update_source(source, *, mode="update", overwrite=False, protected_paths=(), backup_root=None, backup_metadata=None):
    """Plan, check or update this checkout's current named tracking branch."""
    if mode not in ("plan", "check", "update") or type(overwrite) is not bool:
        raise SourceUpdateError("Choose plan, check or update with an explicit overwrite boolean")
    info = inspect_source(source, protected_paths=protected_paths)
    result = {**info, "schema": "raven.source-update/v1", "status": "planned", "changed": False,
              "backup": None, "preserved_commit_ref": None, "network_checked": False}
    if mode == "plan":
        return result
    # The refspec is captured from this branch, never an implicit main branch.
    temporary_ref = "refs/raven/update-fetch/" + uuid.uuid4().hex
    try:
        git(source, "fetch", "--no-tags", "--no-recurse-submodules", "--", info["tracking_remote"],
            "+" + info["tracking_ref"] + ":" + temporary_ref,
            "+" + info["tracking_ref"] + ":" + info["tracking_local_ref"])
        target = _sha(_text(source, "rev-parse", temporary_ref + "^{commit}"))
    finally:
        # A failed fetch may not have created the private reference.
        try:
            git(source, "update-ref", "-d", temporary_ref)
        except SourceUpdateError:
            pass
    current = inspect_source(source, protected_paths=protected_paths)
    if any(current[key] != info[key] for key in ("source", "git_dir", "branch", "original_head", "tracking_remote", "tracking_ref", "tracking_local_ref")):
        raise SourceUpdateError("Branch or tracking configuration changed during fetch; no source update was applied")
    result.update(current, target_head=target, network_checked=True)
    ahead, behind = _relationship(source, current["original_head"], target)
    result.update(ahead=ahead, behind=behind, divergent=bool(ahead and behind))
    result["status"] = "up-to-date" if target == current["original_head"] else "update-available"
    if mode == "check":
        return result
    if not overwrite and (current["source_dirty"] or ahead):
        raise SourceUpdateError("Local source changes or commits prevent a clean fast-forward; review them or use --overwrite with a retained backup")
    if target == current["original_head"] and (not overwrite or not current["dirty"]):
        return result
    _target_safety(current, target)
    backup = create_backup(source, backup_root=backup_root, protected_paths=protected_paths,
                           target_head=target, installation=backup_metadata)
    assert_unchanged(source, backup["manifest"])
    source = Path(current["source"])
    failure = None
    with _mutation_signals():
        try:
            # With ahead=0 this is the verified fast-forward source tree. Reset
            # permits tracked configuration, restored below, to differ locally.
            git(source, "reset", "--hard", target)
            if overwrite:
                arguments = ["clean", "-fd"]
                for path in (*RUNTIME_PATHS, *current["protected_relatives"]):
                    arguments.extend(("-e", "/" + path))
                git(source, *arguments)
        except BaseException as exc:
            failure = exc
        finally:
            try:
                _restore_protected(source, backup)
            except BaseException as exc:
                failure = failure or exc
    if failure is not None:
        raise SourceUpdateError("Source update stopped after backup; inspect source and recover using backup " + backup["path"],
                                backup=backup["path"]) from failure
    result.update(status="updated", changed=True, backup=backup["path"],
                  preserved_commit_ref=backup["manifest"]["retained_ref"])
    return result
