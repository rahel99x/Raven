"""Verified local source recovery, preserving current configuration and runtime state.

Backups are trusted only after ownership, completion, path and content checks.
Their metadata supplies data to fixed Git operations, never executable commands.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid
import errno

from . import config_trust

MAX_FILES = 50000
MAX_FILE_BYTES = 64 << 20
MAX_TOTAL_BYTES = 256 << 20
MAX_MANIFEST_BYTES = 16 << 20
MAX_INDEX_BYTES = 64 << 20
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class RevertError(ValueError):
    """Recovery was refused or interrupted; any safety backup is retained."""

    def __init__(self, message, *, backup=None):
        super().__init__(message)
        self.backup = backup


def _relative(value):
    if (not isinstance(value, str) or not value or len(value.encode()) > 4096
            or "\x00" in value or "\\" in value or value.startswith("/")
            or any(part in ("", ".", "..", ".git") for part in value.split("/"))):
        raise RevertError("Backup contains an unsafe repository-relative path")
    if str(PurePosixPath(value)) != value:
        raise RevertError("Backup path is not canonical")
    return value


def _absolute(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise RevertError("Backup contains an invalid absolute path")
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        raise RevertError("Backup absolute path is not canonical")
    return path


def _object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RevertError("Backup JSON contains duplicate fields")
            result[key] = value
        return result
    try:
        def constant(value):
            raise RevertError("Backup JSON contains a non-finite number")
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RevertError("Backup JSON is invalid") from exc
    if not isinstance(value, dict):
        raise RevertError("Backup JSON must be an object")
    return value


def _read(path, maximum):
    try:
        raw, info = config_trust.read(path, limit=maximum, user_only=True)
    except (OSError, ValueError) as exc:
        raise RevertError(f"Cannot read trusted backup file: {path}") from exc
    if info.st_nlink != 1:
        raise RevertError("Backup files must not be hardlinked")
    return raw


def _path_list(value, name):
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise RevertError(f"Invalid backup {name}")
    result = [_relative(item) for item in value]
    if len(result) != len(set(result)):
        raise RevertError(f"Duplicate backup {name}")
    return result


def _payload(backup, item, storage, maximum):
    if (not isinstance(item, dict) or item.get("storage") != storage
            or type(item.get("bytes")) is not int or not 0 <= item["bytes"] <= maximum
            or not isinstance(item.get("sha256"), str) or not HEX.fullmatch(item["sha256"])):
        raise RevertError("Invalid backup payload descriptor")
    raw = _read(backup / storage, maximum)
    if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise RevertError("Backup payload hash or size does not match")
    return raw


def _verified_backup(backup):
    with config_trust.directory_fd(backup, private=True):
        complete = _object(_read(backup / "COMPLETE.json", 4096))
        if complete.get("schema") != "raven.source-backup-complete/v1":
            raise RevertError("Unsupported backup completion schema")
        raw = _read(backup / "manifest.json", MAX_MANIFEST_BYTES)
        if complete.get("manifest_sha256") != hashlib.sha256(raw).hexdigest():
            raise RevertError("Backup completion digest does not match its manifest")
        manifest = _object(raw)
        if manifest.get("schema") != "raven.source-backup/v1":
            raise RevertError("Unsupported source backup schema")
        _absolute(manifest.get("source"))
        _absolute(manifest.get("git_dir"))
        if (not isinstance(manifest.get("original_head"), str)
                or not COMMIT.fullmatch(manifest["original_head"])
                or not isinstance(manifest.get("branch"), str) or not manifest["branch"]
                or not isinstance(manifest.get("retained_ref"), str)
                or not manifest["retained_ref"].startswith("refs/raven/update-backups/")):
            raise RevertError("Invalid backup Git identity")
        for key in ("tracked_paths", "untracked_paths", "deleted_tracked_paths", "protected_relatives", "excluded_runtime_paths"):
            manifest[key] = _path_list(manifest.get(key, []), key)
        if set(manifest["tracked_paths"]) & set(manifest["untracked_paths"]):
            raise RevertError("Backup tracked and untracked paths overlap")
        if not set(manifest["deleted_tracked_paths"]) <= set(manifest["tracked_paths"]):
            raise RevertError("Deleted paths are not tracked in this backup")
        if not isinstance(manifest.get("protected_paths", []), list) or len(manifest.get("protected_paths", [])) > 128:
            raise RevertError("Invalid protected paths")
        for path in manifest.get("protected_paths", []):
            _absolute(path)
        installation = manifest.get("installation")
        if installation is not None and (not isinstance(installation, dict) or len(json.dumps(installation)) > 64 << 10):
            raise RevertError("Invalid retained installation metadata")
        entries = manifest.get("files")
        if not isinstance(entries, list) or len(entries) > MAX_FILES:
            raise RevertError("Backup contains too many files")
        files = {}
        total = 0
        for item in entries:
            if not isinstance(item, dict):
                raise RevertError("Invalid backup file entry")
            path = _relative(item.get("path"))
            if path in files or item.get("kind") not in ("file", "symlink"):
                raise RevertError("Duplicate or unsupported backup file")
            if type(item.get("mode")) is not int or not 0 <= item["mode"] <= 0o777:
                raise RevertError("Unsafe backup file mode")
            total += item.get("bytes", MAX_TOTAL_BYTES + 1) if type(item.get("bytes")) is int else MAX_TOTAL_BYTES + 1
            if total > MAX_TOTAL_BYTES:
                raise RevertError("Backup payloads exceed the total byte budget")
            data = _payload(backup, item, "files/" + path, MAX_FILE_BYTES)
            if item["kind"] == "symlink":
                try:
                    target = data.decode("utf-8")
                except UnicodeError as exc:
                    raise RevertError("Invalid backed-up symlink target") from exc
                if not target or "\x00" in target or len(data) > 4096:
                    raise RevertError("Invalid backed-up symlink target")
            files[path] = (item, data)
        names = set(files)
        for name in names:
            if any(str(parent) in names for parent in PurePosixPath(name).parents if str(parent) != "."):
                raise RevertError("Backup files collide with ancestor paths")
        expected = (set(manifest["tracked_paths"]) - set(manifest["deleted_tracked_paths"])) | set(manifest["untracked_paths"])
        if not expected <= names or names & set(manifest["deleted_tracked_paths"]):
            raise RevertError("Backup file inventory is incomplete or contradictory")
        # Additional entries are only explicit protected snapshots, never a
        # second channel for arbitrary restore paths.
        for name in names - expected:
            if not _overlaps(name, manifest["protected_relatives"]):
                raise RevertError("Backup payload is outside its declared source inventory")
        index = manifest.get("index")
        index_bytes = _payload(backup, index, "git-index", MAX_INDEX_BYTES) if index is not None else None
        if total + len(index_bytes or b"") > MAX_TOTAL_BYTES or (index_bytes is None and manifest["tracked_paths"]):
            raise RevertError("Backup index is missing or exceeds its total byte budget")
        return manifest, files, index_bytes


def _overlaps(path, protected):
    return any(path == name or path.startswith(name + "/") or name.startswith(path + "/") for name in protected)


def _overlap_checker(protected):
    # Avoid file-count × ignored-path-count comparisons on artifact-heavy trees.
    ancestors = set(protected)
    for name in protected:
        ancestors.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")
    def overlaps(name):
        return name in ancestors or any(str(parent) in protected for parent in PurePosixPath(name).parents if str(parent) != ".")
    return overlaps


def _git_paths(git, source, *arguments):
    paths = [_relative(os.fsdecode(value).rstrip("/")) for value in git(source, *arguments).split(b"\0") if value]
    if len(paths) > MAX_FILES:
        raise RevertError("Current source path inventory exceeds its count budget")
    return paths


def _validate_index(git, source, backup, manifest, index_bytes):
    if index_bytes is None:
        return
    rows = git(source, "ls-files", "--stage", "-z", index_file=backup / "git-index")
    paths = set()
    for entry in rows.split(b"\0"):
        if not entry:
            continue
        try:
            fields, path = entry.split(b"\t", 1)
            file_mode, oid, stage = fields.split(b" ")
        except ValueError as exc:
            raise RevertError("Backed-up Git index is malformed") from exc
        if file_mode not in (b"100644", b"100755", b"120000") or stage not in (b"0", b"1", b"2", b"3") or not COMMIT.fullmatch(oid.decode("ascii")):
            raise RevertError("Backed-up index contains unsupported entries")
        paths.add(_relative(os.fsdecode(path)))
    if paths != set(manifest["tracked_paths"]):
        raise RevertError("Backed-up Git index does not match its tracked inventory")


@contextmanager
def _parent(root, relative, *, create=False):
    parts = _relative(relative).split("/")
    descriptor = os.dup(root)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            info = os.fstat(following)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                os.close(following)
                raise RevertError("Unsafe source ancestor ownership or permissions")
            os.close(descriptor)
            descriptor = following
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _check_leaf(root, relative):
    try:
        with _parent(root, relative) as (parent, leaf):
            try:
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            if info.st_uid != os.getuid() or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise RevertError("Source restore target has unsafe ownership or hardlinks")
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise RevertError("Source restore target has an unsupported file type")
    except FileNotFoundError:
        pass
    except (NotADirectoryError, OSError) as exc:
        raise RevertError("Source restore path traverses a symlink or conflicting file") from exc


def _preflight(root, restore, remove):
    """Permit file/directory transitions only when all blockers are inventoried."""
    removals = set(remove)
    directories = set()
    for path in set(restore) | removals:
        pieces = path.split("/")
        blocked = False
        for length in range(1, len(pieces)):
            prefix = "/".join(pieces[:length])
            try:
                with _parent(root, prefix) as (parent, leaf):
                    info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                blocked = True
                break
            if not stat.S_ISDIR(info.st_mode):
                if prefix not in removals:
                    raise RevertError("Restore path has a source ancestor outside the removal inventory")
                _check_leaf(root, prefix)
                blocked = True
                break
        if blocked:
            continue
        _check_leaf(root, path)
        try:
            with _parent(root, path) as (parent, leaf):
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if path not in restore:
                        raise RevertError("Untracked nested repositories/directories cannot be removed")
                    pending = [path]
                    while pending:
                        directory = pending.pop()
                        directories.add(directory)
                        if len(directories) > MAX_FILES:
                            raise RevertError("Restore directory inventory exceeds its bound")
                        with _parent(root, directory + "/placeholder") as (descriptor, _):
                            for name in os.listdir(descriptor):
                                nested = directory + "/" + name
                                child = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                                if stat.S_ISDIR(child.st_mode):
                                    pending.append(nested)
                                elif nested not in removals:
                                    raise RevertError("Restore directory contains files outside the removal inventory")
                                else:
                                    _check_leaf(root, nested)
        except FileNotFoundError:
            pass
    return sorted(directories, key=lambda path: (-path.count("/"), path))


def _write(root, relative, data, mode, kind="file"):
    with _parent(root, relative, create=True) as (parent, leaf):
        temporary = ".raven-revert-" + uuid.uuid4().hex
        try:
            if kind == "symlink":
                os.symlink(data.decode("utf-8"), temporary, dir_fd=parent)
            else:
                descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode or 0o600, dir_fd=parent)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
            os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


@contextmanager
def _index_transaction(git_dir, expected):
    """Use Git's own lock so a concurrent Git writer cannot lose its index."""
    with config_trust.directory_fd(git_dir) as metadata:
        descriptor = os.open("index.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=metadata)
        lock_info = os.fstat(descriptor)
        published = False
        try:
            try:
                current = _read(git_dir / "index", MAX_INDEX_BYTES)
            except RevertError as exc:
                if not isinstance(exc.__cause__, FileNotFoundError):
                    raise
                current = None
            if ((current is None) != (expected is None)
                    or (current is not None and (len(current) != expected["bytes"] or hashlib.sha256(current).hexdigest() != expected["sha256"]))):
                raise RevertError("Git index changed after the safety snapshot; no source restore was applied")

            def publish(data):
                nonlocal published
                if data is None:
                    try:
                        os.unlink("index", dir_fd=metadata)
                    except FileNotFoundError:
                        pass
                else:
                    with os.fdopen(os.dup(descriptor), "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace("index.lock", "index", src_dir_fd=metadata, dst_dir_fd=metadata)
                    published = True
                os.fsync(metadata)
            yield publish
        finally:
            os.close(descriptor)
            if not published:
                try:
                    current_lock = os.stat("index.lock", dir_fd=metadata, follow_symlinks=False)
                    if (current_lock.st_dev, current_lock.st_ino) == (lock_info.st_dev, lock_info.st_ino):
                        os.unlink("index.lock", dir_fd=metadata)
                except FileNotFoundError:
                    pass


def revert_source(backupdir, *, mode="revert", protected_paths=(), backup_root=None,
                  source_override=None, backup_metadata=None):
    """Plan or restore one verified source snapshot, retaining a new recovery copy."""
    from . import update_source as source_api
    if mode not in ("revert", "plan"):
        raise RevertError("Revert mode must be plan or revert")
    backup = Path(os.path.abspath(os.path.expanduser(os.fspath(backupdir))))
    try:
        manifest, files, index_bytes = _verified_backup(backup)
    except RevertError:
        raise
    except (OSError, ValueError) as exc:
        raise RevertError("Backup directory or manifest does not satisfy the trusted local backup contract") from exc
    source = _absolute(manifest["source"])
    if backup == source or backup.is_relative_to(source) or source.is_relative_to(backup):
        raise RevertError("Source backups must be outside the source checkout")
    if source_override is not None and Path(os.path.abspath(os.fspath(source_override))) != source:
        raise RevertError("Backup belongs to a different source checkout")
    git = source_api.git
    current = source_api.inspect_source(source, protected_paths=protected_paths)
    with config_trust.directory_fd(source) as root:
        if os.fstat(root).st_uid != os.getuid():
            raise RevertError("Source checkout must belong to the current user")
        actual_git_dir = Path(os.fsdecode(git(source, "rev-parse", "--absolute-git-dir")).strip())
        branch = current["branch"]
        head = current["original_head"]
        if actual_git_dir != _absolute(manifest["git_dir"]) or branch != manifest["branch"]:
            raise RevertError("Backup source Git directory or branch does not match")
        git(source, "check-ref-format", "refs/heads/" + branch)
        git(source, "check-ref-format", manifest["retained_ref"])
        retained = os.fsdecode(git(source, "rev-parse", "--verify", manifest["retained_ref"] + "^{commit}")).strip()
        if retained != manifest["original_head"]:
            raise RevertError("Backup retained ref does not identify the original commit")
        _validate_index(git, source, backup, manifest, index_bytes)
        tracked = current["tracked_paths"]
        untracked = current["untracked_paths"]
        ignored = _git_paths(git, source, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
        protected = set(manifest["protected_relatives"] + manifest["excluded_runtime_paths"] + ignored)
        protected.update((".venv", ".raven"))
        for value in (*manifest.get("protected_paths", []), *protected_paths):
            path = Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
            try:
                relative = path.relative_to(source)
            except ValueError:
                continue
            if not relative.parts:
                raise RevertError("The entire checkout cannot be a protected path")
            protected.add(_relative(relative.as_posix()))
        overlaps = _overlap_checker(protected)
        restore = sorted(path for path in files if not overlaps(path))
        remove = sorted((set(tracked) | set(untracked)) - set(restore), key=lambda name: (-name.count("/"), name))
        remove = [path for path in remove if not overlaps(path)]
        remove_directories = _preflight(root, restore, remove)
        result = {"mode": mode, "source": str(source), "original_backup": str(backup),
                  "recovery_backup": None, "original_head": manifest["original_head"], "current_head": head,
                  "branch": branch, "restore_paths": restore, "remove_paths": remove,
                  "preserved_paths": sorted(protected), "installation": manifest.get("installation"),
                  "status": "planned"}
        if mode == "plan":
            return result
        safety = source_api.create_backup(source, backup_root=backup_root,
            protected_paths=tuple(dict.fromkeys((*manifest.get("protected_paths", []), *(os.fspath(value) for value in protected_paths)))),
            target_head=manifest["original_head"], installation=backup_metadata)
        result["recovery_backup"] = str(safety["path"])
        try:
            source_api.assert_unchanged(source, safety["manifest"])
            with source_api._mutation_signals(), _index_transaction(actual_git_dir, safety["manifest"]["index"]) as publish_index:
                current_root = os.stat(source, follow_symlinks=False)
                original_root = os.fstat(root)
                if (current_root.st_dev, current_root.st_ino) != (original_root.st_dev, original_root.st_ino):
                    raise RevertError("Source directory identity changed before restore")
                # No checkout/reset --hard: those would overwrite current ignored
                # files or configuration and invoke configured checkout filters.
                git(source, "update-ref", "refs/heads/" + branch, manifest["original_head"], head)
                for path in remove:
                    try:
                        with _parent(root, path) as (parent, leaf):
                            os.unlink(leaf, dir_fd=parent)
                    except FileNotFoundError:
                        pass
                for path in remove_directories:
                    with _parent(root, path) as (parent, leaf):
                        try:
                            os.rmdir(leaf, dir_fd=parent)
                        except OSError as exc:
                            if exc.errno != errno.ENOENT:
                                raise
                for path in restore:
                    item, data = files[path]
                    _write(root, path, data, item["mode"], item["kind"])
                publish_index(index_bytes)
            result["status"] = "reverted"
            return result
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                exc.backup = str(safety["path"])
                raise
            raise RevertError(f"Source recovery interrupted; current source safety backup retained at {safety['path']}: {exc}", backup=str(safety["path"])) from exc
