"""Bounded, explicit, immutable evidence about a scientific run.

Only declared regular files and explicitly selected environment variables are
inspected. Relative file names are resolved against ``workdir``; explicit paths
outside it are allowed, and symlinks are resolved and recorded before opening.
Neither scripts nor module/container commands are executed. A passport ID
describes its evidence; the capture timestamp is deliberately excluded so
equivalent captures share an ID. A submission receipt belongs in a separate
record rather than modifying a saved passport.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

SCHEMA = "raven.run-passport"
VERSION = 1
MAX_PASSPORT_BYTES = 1024 * 1024
MAX_SCRIPT_BYTES = 8 * 1024 * 1024
MAX_INPUT_HASH_BYTES = 64 * 1024 * 1024
MAX_INPUTS = 128
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SECRET = re.compile(r"secret|(^|_)token($|_)|password|passwd|credential|private.?key|api.?key|authorization|access.?key|signing.?key|bearer|(^|_)auth($|_)|(^|_)key($|_)", re.I)
_CONTAINER_NAMES = {"APPTAINER_CONTAINER", "APPTAINER_IMAGE", "SINGULARITY_CONTAINER", "SINGULARITY_IMAGE"}


class ValidationError(ValueError):
    """The declared input or passport is invalid, unsafe, or incomplete."""


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 24:
        raise ValidationError("passport JSON is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("passport numbers must be finite")
        return
    if isinstance(value, list):
        if len(value) > 10000:
            raise ValidationError("passport list is too large")
        for item in value:
            _json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 10000 or any(not isinstance(k, str) for k in value):
            raise ValidationError("passport objects require bounded string keys")
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise ValidationError("passport values must be JSON data")


def _canonical(value: Any) -> bytes:
    _json_value(value)
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("invalid passport JSON") from exc
    if len(data) > MAX_PASSPORT_BYTES:
        raise ValidationError("passport exceeds the 1 MiB limit")
    return data


def _identity(passport: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in passport.items() if k not in ("id", "captured_at", "checksum")})).hexdigest()


def _checksum(passport: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in passport.items() if k != "checksum"})).hexdigest()


def _regular_fd(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise ValidationError(f"cannot open declared file {path}: {exc.strerror}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValidationError(f"declared path is not a regular file: {path}")
    return fd


def _file(workdir: Path, name: Any, hash_limit: int | None = None) -> dict:
    if not isinstance(name, (str, os.PathLike)) or not str(name):
        raise ValidationError("declared files require nonempty paths")
    source = Path(name).expanduser()
    source = Path(os.path.abspath(source if source.is_absolute() else workdir / source))
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"cannot resolve declared file: {source}") from exc
    fd = _regular_fd(resolved)
    try:
        before = os.fstat(fd)
        digest = None
        if hash_limit is not None:
            if before.st_size > hash_limit:
                raise ValidationError(f"declared file exceeds its {hash_limit}-byte hashing budget: {source}")
            hasher, read = hashlib.sha256(), 0
            while True:
                chunk = os.read(fd, min(128 * 1024, hash_limit + 1 - read))
                if not chunk:
                    break
                read += len(chunk)
                if read > hash_limit:
                    raise ValidationError(f"declared file grew beyond its hashing budget: {source}")
                hasher.update(chunk)
            digest = hasher.hexdigest()
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValidationError(f"declared file changed during capture: {source}")
        return {"path": str(source), "resolved_path": str(resolved), "symlink": source != resolved,
                "size": before.st_size, "mtime_ns": before.st_mtime_ns, "sha256": digest}
    finally:
        os.close(fd)


def _git(workdir: Path, args: list[str], limit: int = 1024 * 1024) -> bytes | None:
    # Disallow inherited Git overrides and fsmonitor commands; never read a
    # remote URL, credential configuration, patches, or untracked file contents.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
                                    cwd=workdir, env=environment, stdout=output, stderr=subprocess.DEVNULL,
                                    timeout=2, check=False)
            if result.returncode:
                return None
            output.seek(0)
            data = output.read(limit + 1)
            return data if len(data) <= limit else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_evidence(workdir: Path) -> dict:
    revision = _git(workdir, ["rev-parse", "--verify", "HEAD"], 128)
    text = revision.decode("ascii", "replace").strip() if revision else ""
    revision_text = text if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text) else None
    status_data = _git(workdir, ["status", "--porcelain=v1", "-z", "--untracked-files=no"])
    changed = None
    if status_data is not None:
        records = status_data.split(b"\0")
        changed, index = [], 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            changed.append(record[3:].decode("utf-8", "replace"))
            if b"R" in record[:2] or b"C" in record[:2]:
                index += 1  # Rename/copy source is the following NUL record.
    return {"revision": revision_text, "dirty": bool(status_data) if status_data is not None else None,
            "changed_paths": sorted(set(changed))[:100] if changed is not None else None,
            "changed_paths_truncated": len(set(changed)) > 100 if changed is not None else False}


def _declared_mapping(value: Any, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be a JSON object")
    _canonical(value)
    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if _SECRET.search(key):
                    raise ValidationError(f"{name} contains a secret-like field name")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(value)
    return json.loads(_canonical(value))


def _input_declaration(item: Any) -> tuple[Any, bool, int]:
    if isinstance(item, dict):
        if set(item) - {"path", "hash", "max_bytes"} or "path" not in item or not isinstance(item.get("hash", False), bool):
            raise ValidationError("input declarations require path and optional hash/max_bytes")
        name, hashing = item["path"], item.get("hash", False)
        budget = item.get("max_bytes", MAX_INPUT_HASH_BYTES)
    else:
        name, hashing, budget = item, False, MAX_INPUT_HASH_BYTES
    if not isinstance(name, (str, os.PathLike)) or not str(name) or "\x00" in str(name):
        raise ValidationError("declared files require nonempty paths without NUL")
    if not isinstance(budget, int) or isinstance(budget, bool) or not 0 <= budget <= MAX_INPUT_HASH_BYTES:
        raise ValidationError("input hash budget must be between 0 and 64 MiB")
    return name, hashing, budget


def validate_metadata(resources: dict | None = None, parameters: dict | None = None, inputs=()) -> None:
    """Validate declarations without reading files, environment, or Git.

    Preparation can call this before offering a submission for confirmation.
    File existence, stability, and actual hashing budgets are checked by capture.
    Iterators are consumed; pass a reusable collection if capturing afterward.
    """
    _declared_mapping(resources, "resources")
    _declared_mapping(parameters, "parameters")
    if isinstance(inputs, (str, bytes, dict)):
        raise ValidationError("inputs must be a collection")
    try:
        iterator = iter(inputs)
    except TypeError as exc:
        raise ValidationError("inputs must be a collection") from exc
    for index, item in enumerate(iterator):
        if index >= MAX_INPUTS:
            raise ValidationError("too many declared inputs (maximum 128)")
        _input_declaration(item)


def capture(workdir: str | os.PathLike, script: str | os.PathLike | None = None,
            resources: dict | None = None, parameters: dict | None = None, inputs=(),
            environment_names=(), host: str = "", job_id: str | int | None = None) -> dict:
    """Capture bounded evidence, with explicitly selected environment values.

    Input strings record size/mtime only. ``{"path": "data.csv", "hash": True,
    "max_bytes": 4096}`` requests SHA256 within a bounded budget (64 MiB maximum
    per file and across all declared hashes). Scripts are always hashed, within
    8 MiB. Missing, nonregular, or oversized declared files raise ValidationError.
    ``LOADEDMODULES`` and container variables are recorded only if selected.
    Untracked files do not make Git dirty; explicitly declare them as inputs.
    """
    try:
        root = Path(workdir).expanduser().resolve(strict=True)
    except (TypeError, OSError, RuntimeError) as exc:
        raise ValidationError("workdir must be an existing directory") from exc
    if not root.is_dir():
        raise ValidationError("workdir must be an existing directory")
    if isinstance(inputs, (str, bytes, dict)) or isinstance(environment_names, (str, bytes, dict)):
        raise ValidationError("inputs and environment_names must be collections")
    try:
        input_iterator, name_iterator = iter(inputs), iter(environment_names)
    except TypeError as exc:
        raise ValidationError("inputs and environment_names must be collections") from exc
    input_records, remaining = [], MAX_INPUT_HASH_BYTES
    for item in input_iterator:
        if len(input_records) >= MAX_INPUTS:
            raise ValidationError("too many declared inputs (maximum 128)")
        name, hashing, budget = _input_declaration(item)
        record = _file(root, name, min(budget, remaining) if hashing else None)
        if record["sha256"] is not None:
            remaining -= record["size"]
        input_records.append(record)
    names = set()
    for index, name in enumerate(name_iterator):
        if index >= 64:
            raise ValidationError("too many selected environment variables (maximum 64)")
        if not isinstance(name, str) or len(name) > 128 or not _ENV.fullmatch(name) or _SECRET.search(name):
            raise ValidationError("environment selection contains an invalid or secret-like name")
        names.add(name)
        if len(names) > 64:
            raise ValidationError("too many selected environment variables (maximum 64)")
    environment = {name: os.environ.get(name) for name in sorted(names)}
    if any(value is not None and len(value.encode("utf-8", "surrogatepass")) > 4096 for value in environment.values()):
        raise ValidationError("selected environment value exceeds 4096 bytes")
    if not isinstance(host, str) or len(host) > 1024:
        raise ValidationError("host must be a bounded string")
    if "://" in host:
        try:
            parts = urlsplit(host)
            host = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], "", "", ""))
        except ValueError as exc:
            raise ValidationError("invalid host") from exc
    else:
        host = host.rsplit("@", 1)[-1]
    if job_id is not None:
        if isinstance(job_id, bool) or not isinstance(job_id, (str, int)) or not re.fullmatch(r"[0-9][0-9A-Za-z_.\[\],%+_-]{0,127}", str(job_id)):
            raise ValidationError("invalid Slurm job ID")
        job_id = str(job_id)
    passport = {"schema": SCHEMA, "version": VERSION,
                "captured_at": datetime.now(timezone.utc).isoformat(), "workdir": str(root),
                "script": _file(root, script, MAX_SCRIPT_BYTES) if script is not None else None,
                "git": _git_evidence(root), "environment": environment,
                "modules": environment["LOADEDMODULES"].split(":") if environment.get("LOADEDMODULES") else ([] if environment.get("LOADEDMODULES") == "" else None),
                "container": {k: environment[k] for k in sorted(names & _CONTAINER_NAMES)} or None,
                "resources": _declared_mapping(resources, "resources"), "parameters": _declared_mapping(parameters, "parameters"),
                "inputs": input_records, "host": host or None, "job_id": job_id}
    passport["id"] = _identity(passport)
    passport["checksum"] = _checksum(passport)
    validate(passport)
    return passport


def _file_record(record: Any) -> bool:
    return (isinstance(record, dict) and set(record) == {"path", "resolved_path", "symlink", "size", "mtime_ns", "sha256"}
            and all(isinstance(record[k], str) and Path(record[k]).is_absolute() for k in ("path", "resolved_path"))
            and isinstance(record["symlink"], bool)
            and all(isinstance(record[k], int) and not isinstance(record[k], bool) for k in ("size", "mtime_ns")) and record["size"] >= 0
            and (record["sha256"] is None or isinstance(record["sha256"], str) and bool(_SHA.fullmatch(record["sha256"]))))


def validate(passport: Any) -> None:
    """Validate schema, bounded JSON, and the content identity."""
    _canonical(passport)
    keys = {"schema", "version", "id", "checksum", "captured_at", "workdir", "script", "git", "environment", "modules", "container", "resources", "parameters", "inputs", "host", "job_id"}
    if not isinstance(passport, dict) or set(passport) != keys or passport.get("schema") != SCHEMA or type(passport.get("version")) is not int or passport["version"] != VERSION:
        raise ValidationError("unsupported or malformed run passport schema")
    if not isinstance(passport["id"], str) or not _SHA.fullmatch(passport["id"]):
        raise ValidationError("invalid passport ID")
    if not isinstance(passport["checksum"], str) or not _SHA.fullmatch(passport["checksum"]):
        raise ValidationError("invalid passport checksum")
    try:
        timestamp = datetime.fromisoformat(passport["captured_at"])
        if timestamp.tzinfo is None:
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise ValidationError("invalid passport capture timestamp") from exc
    if not isinstance(passport["workdir"], str) or not Path(passport["workdir"]).is_absolute():
        raise ValidationError("invalid passport workdir")
    if passport["script"] is not None and (not _file_record(passport["script"]) or passport["script"]["sha256"] is None):
        raise ValidationError("invalid passport script fingerprint")
    if not isinstance(passport["inputs"], list) or len(passport["inputs"]) > MAX_INPUTS or not all(_file_record(item) for item in passport["inputs"]):
        raise ValidationError("invalid passport input fingerprints")
    git = passport["git"]
    if (not isinstance(git, dict) or set(git) != {"revision", "dirty", "changed_paths", "changed_paths_truncated"}
            or not (git["revision"] is None or isinstance(git["revision"], str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", git["revision"]))
            or not (git["dirty"] is None or isinstance(git["dirty"], bool)) or not isinstance(git["changed_paths_truncated"], bool)
            or not (git["changed_paths"] is None or isinstance(git["changed_paths"], list) and len(git["changed_paths"]) <= 100 and all(isinstance(v, str) for v in git["changed_paths"]))):
        raise ValidationError("invalid passport Git evidence")
    environment = passport["environment"]
    if not isinstance(environment, dict) or len(environment) > 64 or any(not _ENV.fullmatch(k) or _SECRET.search(k) or not (v is None or isinstance(v, str) and len(v.encode("utf-8", "surrogatepass")) <= 4096) for k, v in environment.items()):
        raise ValidationError("invalid passport environment selection")
    if not (passport["modules"] is None or isinstance(passport["modules"], list) and all(isinstance(v, str) for v in passport["modules"])):
        raise ValidationError("invalid passport modules")
    if not (passport["container"] is None or isinstance(passport["container"], dict) and set(passport["container"]) <= _CONTAINER_NAMES and all(v is None or isinstance(v, str) for v in passport["container"].values())):
        raise ValidationError("invalid passport container metadata")
    for name in ("resources", "parameters"):
        if not isinstance(passport[name], dict):
            raise ValidationError(f"invalid passport {name}")
        _declared_mapping(passport[name], name)
    if not (passport["host"] is None or isinstance(passport["host"], str) and len(passport["host"]) <= 1024):
        raise ValidationError("invalid passport host")
    if not (passport["job_id"] is None or isinstance(passport["job_id"], str) and re.fullmatch(r"[0-9][0-9A-Za-z_.\[\],%+_-]{0,127}", passport["job_id"])):
        raise ValidationError("invalid passport job ID")
    if _identity(passport) != passport["id"]:
        raise ValidationError("passport content does not match its ID (modified or corrupt)")
    if _checksum(passport) != passport["checksum"]:
        raise ValidationError("passport checksum mismatch (modified or corrupt)")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValidationError("duplicate passport JSON key")
        value[key] = item
    return value


def load(path: str | os.PathLike) -> dict:
    """Load a regular file, rejecting symlinks, duplicate keys and corruption.

    Plain hashes detect accidental changes; they do not authenticate an author
    who can replace the document and recompute its hashes. Use signed evidence
    when authenticity is required.
    """
    fd = _regular_fd(Path(path))
    try:
        from .source_budget import source_limit, source_read
        limit = source_limit(MAX_PASSPORT_BYTES)
        if os.fstat(fd).st_size > limit:
            raise ValidationError("passport exceeds the 1 MiB limit")
        source_read(os.fstat(fd).st_size)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValidationError("passport exceeds the 1 MiB limit")
        try:
            passport = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValidationError("invalid JSON number")))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValidationError("malformed passport JSON") from exc
        validate(passport)
        return passport
    finally:
        os.close(fd)


def save(passport: dict, directory: str | os.PathLike) -> Path:
    """Create a private content-addressed record without overwriting any file."""
    validate(passport)
    root = Path(directory).expanduser()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / f"{passport['id']}.json"
    data = _canonical(passport)
    temporary = None
    try:
        # Write privately first, then atomically publish with a no-overwrite
        # hard link. Concurrent readers can never see a partially written file.
        with tempfile.NamedTemporaryFile(prefix=".passport-", dir=root, delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing = load(target)
            if existing["id"] != passport["id"] or _identity(existing) != _identity(passport):
                raise ValidationError("existing passport conflicts with the capture")
            if stat.S_IMODE(target.stat().st_mode) & 0o077:
                raise ValidationError("existing passport is not private (expected mode 0600)")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def diff(left: dict | str | os.PathLike, right: dict | str | os.PathLike) -> list[dict]:
    """Compare evidence using JSON Pointer paths, excluding capture timestamps."""
    left = left if isinstance(left, dict) else load(left)
    right = right if isinstance(right, dict) else load(right)
    validate(left)
    validate(right)
    changes = []
    def walk(a: Any, b: Any, path: str, present_a: bool = True, present_b: bool = True) -> None:
        if present_a and present_b and isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() | b.keys()):
                escaped = key.replace("~", "~0").replace("/", "~1")
                walk(a.get(key), b.get(key), path + "/" + escaped, key in a, key in b)
        elif _canonical(a) != _canonical(b) or present_a != present_b:
            changes.append({"path": path, "left": a, "right": b, "left_present": present_a, "right_present": present_b})
    walk({k: v for k, v in left.items() if k not in ("id", "captured_at", "checksum")},
         {k: v for k, v in right.items() if k not in ("id", "captured_at", "checksum")}, "")
    return changes
