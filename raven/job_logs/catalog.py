"""Bounded, job-scoped log discovery and an explicit portable log manifest.

Cataloguing never reads the logs themselves. Local and SSH reads stay on their
selected backend, and the UI shares ResearchHub's existing single worker.
"""

from __future__ import annotations

from collections import OrderedDict
import base64
import hashlib
import json
import math
import os
import posixpath
import re
import shlex
import stat
import threading
import time

from ..planning_io import load_json
from ..local_files import LocalFiles
from ..remote_files import RemoteFiles


MAX_MANIFEST_BYTES = 262144
MAX_LOGS = 256
MAX_DIRECTORY_NAMES = 4096
MAX_DIRECTORY_REPLY = 2 * 1024 * 1024
MAX_PATH = 4096
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_JOB = re.compile(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?\Z")


def _clean(value, limit=512):
    return "".join(c if c.isprintable() else " " for c in str(value))[:limit]


def _text(value, name, limit, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise ValueError(
            f"{name} must be a string of {'0' if empty else '1'} to {limit} characters"
        )
    if value and not value.isprintable():
        raise ValueError(f"{name} must contain only printable characters")
    return value


def _path(value, name, *, base=""):
    value = _text(value, name, MAX_PATH)
    if any(char in value for char in "\\*?[]"):
        raise ValueError(
            f"{name} must be an exact POSIX path without globs or backslashes"
        )
    if value.startswith("~"):
        raise ValueError(f"{name} must use an explicit path, without tilde expansion")
    if not posixpath.isabs(value) and base:
        value = posixpath.join(base, value)
    value = posixpath.normpath(value)
    if len(value) > MAX_PATH:
        raise ValueError(f"{name} resolves beyond the {MAX_PATH}-character path limit")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON value")


def _number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is outside the finite range")
    return number


def _remote_signature(files, path):
    """RemoteFiles supplies a regular-file check without following a symlink.

    File adapters used by embedders may expose only stat's size/identity pair;
    those still get an exact-length, before/after identity check.
    """
    managed = getattr(files, "manifest_signature", None)
    if callable(managed) and getattr(files, "remote", False):
        return managed(path)
    if isinstance(files, RemoteFiles):
        try:
            output, _ = files.ssh.run(
                ["stat", "-c", "%f %d %i %s %Y %Z", "--", path], files.timeout
            )
            if not isinstance(output, str) or len(output) > 512:
                raise ValueError("oversized remote stat response")
            parts = output.split()
            if len(parts) != 6:
                raise ValueError("unexpected remote stat response")
            mode = int(parts[0], 16)
            signature = tuple(int(part) for part in parts[1:])
        except (ValueError, OSError) as exc:
            raise ValueError("invalid remote log-manifest metadata") from exc
        if not stat.S_ISREG(mode):
            raise ValueError(
                "log manifest must be a regular file, not a symlink or device"
            )
        return signature
    return None


def _load_manifest(path, files):
    if not getattr(files, "remote", False):
        return load_json(path, max_bytes=MAX_MANIFEST_BYTES, max_depth=8)
    signature = _remote_signature(files, path)
    before = files.stat(path)
    size, _identity = before
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 <= size <= MAX_MANIFEST_BYTES
    ):
        raise ValueError(
            f"log manifest exceeds the {MAX_MANIFEST_BYTES}-byte read limit"
        )
    data = files.read(path, 0, size + 1)
    after = files.stat(path)
    if (
        before != after
        or signature != _remote_signature(files, path)
        or len(data) != size
    ):
        raise ValueError("log manifest changed during inspection")
    if not isinstance(data, bytes):
        raise ValueError("remote log-manifest reader must return bytes")
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_number,
            parse_constant=_constant,
        )
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("invalid UTF-8 log-manifest JSON") from exc


def _manifest_entries(value, job_id, manifest_file):
    if not isinstance(value, dict) or set(value) - {
        "schema",
        "logs",
        "run_id",
        "job_id",
    }:
        raise ValueError(
            "log manifest must be an object with only schema, logs, run_id, and job_id"
        )
    if value.get("schema") != "raven.logs/v1":
        raise ValueError("log manifest schema must be raven.logs/v1")
    if "job_id" in value:
        claimed = _text(value["job_id"], "manifest job_id", 128)
        if claimed != job_id:
            raise ValueError("log manifest job_id does not match the selected job")
    if "run_id" in value and not _ID.fullmatch(
        _text(value["run_id"], "manifest run_id", 128)
    ):
        raise ValueError("manifest run_id must be a safe ASCII basename")
    logs = value.get("logs")
    if not isinstance(logs, list) or len(logs) > MAX_LOGS:
        raise ValueError(
            f"log manifest logs must be a list with at most {MAX_LOGS} entries"
        )
    result, ids = [], set()
    base = posixpath.dirname(manifest_file) or "."
    for item in logs:
        if not isinstance(item, dict) or set(item) - {
            "id",
            "path",
            "label",
            "group",
            "description",
        }:
            raise ValueError(
                "log entry must contain only id, path, label, group, and description"
            )
        identifier = _text(item.get("id"), "log id", 128)
        if not _ID.fullmatch(identifier):
            raise ValueError("log id must be a safe ASCII identifier")
        if identifier in ids:
            raise ValueError("log manifest contains duplicate log IDs")
        ids.add(identifier)
        path = _path(item.get("path"), "log path", base=base)
        entry = {
            "id": "manifest." + identifier,
            "path": path,
            "label": _text(item.get("label", identifier), "log label", 160),
            "group": _text(item.get("group", "Application"), "log group", 160),
            "source": "manifest",
        }
        if "description" in item:
            entry["description"] = _text(
                item["description"], "log description", 512, empty=True
            )
        result.append(entry)
    return result


def _directory_names(files, directory):
    """Return at most 4096 names and explicitly report the cutoff."""
    if isinstance(files, RemoteFiles):
        quoted = shlex.quote(directory)
        script = (
            f"test -d {quoted} && test -r {quoted} && test -x {quoted} || exit 2; "
            f"find -- {quoted} -mindepth 1 -maxdepth 1 -type f -uid \"$(id -u)\" -printf '%f\\0' "
            f"| head -z -n {MAX_DIRECTORY_NAMES + 1} | base64"
        )
        output, _ = files.ssh.run(["sh", "-c", script], files.timeout)
        if not isinstance(output, str) or len(output) > MAX_DIRECTORY_REPLY:
            raise ValueError("remote directory listing exceeds the bounded reply limit")
        try:
            raw = base64.b64decode("".join(output.split()), validate=True)
            # POSIX names may contain invalid UTF-8; refuse rather than fabricate a path.
            names = raw.decode("utf-8").split("\0")
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid remote directory listing") from exc
        if names and names[-1] == "":
            names.pop()
    elif type(files) is LocalFiles:
        names = []
        limited = False
        with os.scandir(directory) as iterator:
            for index, entry in enumerate(iterator):
                if index >= MAX_DIRECTORY_NAMES:
                    limited = True
                    break
                if (
                    not entry.is_file(follow_symlinks=False)
                    or entry.stat(follow_symlinks=False).st_uid != os.getuid()
                ):
                    continue
                names.append(entry.name)
        return names, limited
    else:
        # Custom/fixture interfaces keep all I/O on their supplied backend.
        listed = files.listdir(directory)
        names = []
        for name in listed:
            names.append(name)
            if len(names) > MAX_DIRECTORY_NAMES:
                break
    return names[:MAX_DIRECTORY_NAMES], len(names) > MAX_DIRECTORY_NAMES


def _job_pattern(job_id):
    if not _JOB.fullmatch(job_id):
        return None
    if job_id.isdigit():
        # A selected array parent owns its task/component outputs. Exact digits
        # remain bounded so parent 77 cannot consume parent 777's files.
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(job_id) + r"(?![A-Za-z0-9])")
    identifiers = [job_id]
    parent = job_id.split("_", 1)[0].split("+", 1)[0]
    if parent != job_id:
        identifiers.append(parent)
    # _rank and .step suffixes are valid; _<task>/+<component> must match exactly.
    return re.compile(
        r"(?<![A-Za-z0-9])(?:"
        + "|".join(re.escape(i) for i in identifiers)
        + r")(?![A-Za-z0-9]|_[0-9]|\+[0-9])"
    )


def build_catalog(job_id, stdout, stderr, *, manifest_file="", files=None):
    """Inspect one job's scheduler parents and optional exact log manifest.

    Errors are visible messages and leave successfully identified scheduler logs
    usable. Manifest validation is atomic; malformed records never leak entries.
    """
    files = files or LocalFiles()
    result = {
        "status": "ready",
        "job_id": job_id,
        "entries": [],
        "messages": [],
        "manifest_file": manifest_file,
    }
    entries, messages = result["entries"], result["messages"]
    try:
        job_id = _text(job_id, "selected job ID", 128)
        result["job_id"] = job_id
    except ValueError as exc:
        result.update(status="error", job_id=_clean(job_id, 128), manifest_file="")
        messages.append(_clean(exc))
        return result
    paths, directories = {}, []

    def add(entry):
        path = entry["path"]
        identity = path if getattr(files, "remote", False) else posixpath.abspath(path)
        if identity in paths:
            if (
                entry["source"] == "scheduler"
                and paths[identity]["label"] != entry["label"]
            ):
                paths[identity]["label"] = "stdout / stderr"
            return
        if len(entries) >= MAX_LOGS:
            warning = f"Catalog reached the {MAX_LOGS}-log limit; remaining files were omitted."
            if warning not in messages:
                messages.append(warning)
            return
        paths[identity] = entry
        entries.append(entry)

    for field, value in (("stdout", stdout), ("stderr", stderr)):
        if value in (None, ""):
            continue
        try:
            path = _path(value, "scheduler " + field)
            add(
                {
                    "id": "scheduler." + field,
                    "path": path,
                    "label": field,
                    "group": "Scheduler",
                    "source": "scheduler",
                }
            )
            parent = posixpath.dirname(path) or "."
            if parent not in directories:
                directories.append(parent)
        except ValueError as exc:
            messages.append(_clean(exc))
    if manifest_file:
        try:
            manifest_file = _path(manifest_file, "log manifest")
            # Root resolves this already. Keeping a local relative API useful is
            # safe, while remote relative paths must retain the remote base.
            if not posixpath.isabs(manifest_file) and not getattr(
                files, "remote", False
            ):
                manifest_file = posixpath.abspath(manifest_file)
            result["manifest_file"] = manifest_file
            manifest_entries = _manifest_entries(
                _load_manifest(manifest_file, files), job_id, manifest_file
            )
            for entry in manifest_entries:
                add(entry)
        except Exception as exc:
            result["manifest_file"] = _clean(manifest_file, MAX_PATH)
            messages.append("Log manifest unavailable: " + _clean(exc))
    pattern = _job_pattern(job_id)
    if pattern:
        for directory in directories:
            try:
                names, limited = _directory_names(files, directory)
                if limited:
                    messages.append(
                        f"Directory discovery inspected only the first {MAX_DIRECTORY_NAMES} names in {_clean(directory)}."
                    )
                for name in sorted(names, key=lambda item: str(item)):
                    if (
                        not isinstance(name, str)
                        or not name
                        or not name.isprintable()
                        or "/" in name
                        or "\\" in name
                    ):
                        continue
                    if not pattern.search(name):
                        continue
                    try:
                        path = _path(
                            posixpath.join(directory, name), "discovered log path"
                        )
                    except ValueError:
                        continue
                    evidence = {}
                    if type(files) is LocalFiles:
                        try:
                            info = os.lstat(path)
                        except OSError:
                            continue
                        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                            continue
                        evidence = dict(
                            uid=info.st_uid,
                            mode=info.st_mode,
                            device=info.st_dev,
                            inode=info.st_ino,
                            size=info.st_size,
                            symlink=False,
                        )
                    add(
                        {
                            "id": "discovered."
                            + hashlib.sha256(path.encode("utf-8")).hexdigest()[:24],
                            "path": path,
                            "label": name,
                            "group": "Scheduler directory",
                            "source": "discovered",
                            "discovery": evidence,
                        }
                    )
            except Exception as exc:
                messages.append(
                    "Directory discovery unavailable for "
                    + _clean(directory)
                    + ": "
                    + _clean(exc)
                )
    if messages:
        result["status"] = "partial" if entries else "error"
    return result


class LogCatalog:
    """Eight exact contexts, thirty-second refreshes, and no new executor."""

    def __init__(self, files=None, *, ttl=30, max_entries=8):
        self.files = files or LocalFiles()
        self.ttl = max(0, float(ttl))
        self.polling_multiplier = 1
        self.max_entries = max(1, min(8, int(max_entries)))
        self.cache = OrderedDict()
        self.generation = 0
        self.active = None
        self.pending = None
        self.closed = False
        self.lock = threading.RLock()

    def configure(self, *, files=None):
        with self.lock:
            if files is not None:
                self.files = files
            self.generation += 1
            self.cache.clear()
            self.active = None

    def close(self):
        with self.lock:
            self.closed = True
            self.generation += 1
            self.cache.clear()

    def _key(self, job_id, stdout, stderr, manifest_file):
        return (job_id, stdout or "", stderr or "", manifest_file or "")

    def current(self, job_id, stdout, stderr, manifest_file=""):
        key = self._key(job_id, stdout, stderr, manifest_file)
        with self.lock:
            entry = self.cache.get(key)
            if entry:
                self.cache.move_to_end(key)
                return entry[1]
            return {
                "status": "error" if self.closed else "loading",
                "entries": [],
                "messages": ["Log catalog closed."] if self.closed else [],
                "job_id": job_id,
                "manifest_file": manifest_file,
            }

    def request(
        self,
        job_id,
        stdout,
        stderr,
        manifest_file="",
        *,
        worker=None,
        force=False,
        wait=False,
    ):
        key = self._key(job_id, stdout, stderr, manifest_file)
        with self.lock:
            if self.closed:
                return self.current(job_id, stdout, stderr, manifest_file)
            if key != self.active:
                self.generation += 1
                self.active = key
            entry = self.cache.get(key)
            from ..cadence import file_interval

            policy = getattr(getattr(self.files, "ledger", None), "config", None)
            base = policy["log_follow_seconds"] if policy is not None else self.ttl
            interval = file_interval(
                base,
                self.polling_multiplier,
                remote=bool(getattr(self.files, "remote", False)),
            )
            interval = max(interval, float(getattr(self.files, "min_refresh", 0.0)))
            if not force and entry and time.monotonic() - entry[0] < interval:
                self.cache.move_to_end(key)
                return entry[1]
            if self.pending is not None:
                return self.current(job_id, stdout, stderr, manifest_file)
            token = (self.generation, key)
            self.pending = token
            files = self.files

        def read():
            return build_catalog(
                job_id, stdout, stderr, manifest_file=manifest_file, files=files
            )

        def complete(value):
            with self.lock:
                if self.pending == token:
                    self.pending = None
                if self.closed or token != (self.generation, self.active):
                    return
                if isinstance(value, Exception):
                    value = {
                        "status": "error",
                        "entries": [],
                        "messages": [_clean(value)],
                        "job_id": job_id,
                        "manifest_file": manifest_file,
                    }
                self.cache[key] = (time.monotonic(), value)
                self.cache.move_to_end(key)
                while len(self.cache) > self.max_entries:
                    self.cache.popitem(last=False)

        if worker is None:
            try:
                value = read()
            except Exception as exc:
                value = exc
            complete(value)
        else:
            try:
                admitted = worker.start_task(read, complete)
            except Exception as exc:
                complete(exc)
                admitted = False
            if not admitted:
                with self.lock:
                    if self.pending == token:
                        self.pending = None
            elif wait:
                future = getattr(worker, "future", None)
                if future is not None:
                    # Used by explicit synchronous checks, never by UI frames.
                    try:
                        future.result(timeout=30)
                    except Exception:
                        # poll_task publishes task failures on the UI thread;
                        # a timed-out future remains pending for a later tick.
                        pass
                    worker.poll_task()
        return self.current(job_id, stdout, stderr, manifest_file)
