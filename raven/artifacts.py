"""Bounded, declarative output checks for experiment artifacts.

Contracts name exact relative paths. No shell commands, globs, recursive scans,
or optional imports run here. Local opens are anchored to the working directory
and refuse symlinks. Readers that cannot prove confinement and file type report
incomplete checks instead of reading untrusted paths or claiming success.
"""
from __future__ import annotations

import csv
import datetime as dt
import errno
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from .remote import LocalFiles

CONTRACT_MAX_BYTES = 256 * 1024
MAX_OUTPUTS = 4096
MAX_READ_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 256
MAX_CHECK_VALUE = (1 << 63) - 1
_FIELDS = {"path", "required", "min_bytes", "max_bytes", "format", "columns",
           "rows", "min_rows", "max_rows", "required_keys", "sha256"}


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite JSON number: {value}")
    return number


def _json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs,
                          parse_constant=_nonfinite, parse_float=_float)
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc


def _names(value: Any, field: str) -> None:
    if (not isinstance(value, list) or len(value) > 256
            or any(not isinstance(x, str) or not x or len(x) > 1024
                   or any(ord(c) < 32 or ord(c) == 127 for c in x) for x in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{field} must be a list of up to 256 unique nonempty strings")


def _contract(contract: Any) -> list[dict[str, Any]]:
    if not isinstance(contract, dict) or set(contract) != {"version", "outputs"}:
        raise ValueError("contract must contain exactly version and outputs")
    if type(contract["version"]) is not int or contract["version"] != 1:
        raise ValueError("contract version must be 1")
    outputs = contract["outputs"]
    if not isinstance(outputs, list) or len(outputs) > MAX_OUTPUTS:
        raise ValueError(f"outputs must be a list of at most {MAX_OUTPUTS} entries")
    seen: set[str] = set()
    for output in outputs:
        if not isinstance(output, dict) or "path" not in output or set(output) - _FIELDS:
            raise ValueError("each output must contain path and only supported check fields")
        path = output["path"]
        if (not isinstance(path, str) or not path or len(path) > 4096
                or path.startswith("/") or "\\" in path
                or any(c in path for c in "*?[]")
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or any(part in {"", ".", ".."} for part in path.split("/"))):
            raise ValueError("output path must be an exact relative path without traversal, globs, or control characters")
        if path in seen:
            raise ValueError(f"duplicate output path: {path}")
        seen.add(path)
        if "required" in output and type(output["required"]) is not bool:
            raise ValueError("required must be a boolean")
        for field in ("min_bytes", "max_bytes", "rows", "min_rows", "max_rows"):
            if field in output and (type(output[field]) is not int or not 0 <= output[field] <= MAX_CHECK_VALUE):
                raise ValueError(f"{field} must be an integer from 0 to {MAX_CHECK_VALUE}")
        for low, high in (("min_bytes", "max_bytes"), ("min_rows", "max_rows")):
            if low in output and high in output and output[low] > output[high]:
                raise ValueError(f"{low} must not exceed {high}")
        if "rows" in output and (output["rows"] < output.get("min_rows", 0)
                or output["rows"] > output.get("max_rows", output["rows"])):
            raise ValueError("rows conflicts with min_rows or max_rows")
        fmt = output.get("format")
        if "format" in output and (not isinstance(fmt, str) or fmt not in {"json", "csv", "text"}):
            raise ValueError("format must be json, csv, or text")
        if any(field in output for field in ("columns", "rows", "min_rows", "max_rows")) and fmt != "csv":
            raise ValueError("columns and row checks require format csv")
        if "required_keys" in output and fmt != "json":
            raise ValueError("required_keys requires format json")
        for field in ("columns", "required_keys"):
            if field in output:
                _names(output[field], field)
        if "sha256" in output and (not isinstance(output["sha256"], str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", output["sha256"])):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
    return outputs


def load_contract(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load strict UTF-8 JSON, bounded to 256 KiB, and validate its schema.

    Symlinks and nonregular contract files are refused. Invalid contracts raise
    ``ValueError``; filesystem failures raise ``OSError``. Artifact validation
    does not begin until the entire contract has passed schema validation.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("contract must be a regular file")
        if before.st_size > CONTRACT_MAX_BYTES:
            raise ValueError(f"contract exceeds {CONTRACT_MAX_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = CONTRACT_MAX_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > CONTRACT_MAX_BYTES:
            raise ValueError(f"contract exceeds {CONTRACT_MAX_BYTES} bytes")
        try:
            named = os.stat(path, follow_symlinks=False)
            stable = _signature(before) == _signature(os.fstat(fd)) == _signature(named)
        except OSError:
            stable = False
        if not stable:
            raise ValueError("contract changed while being read")
        contract = _json(data)
        _contract(contract)
        return contract
    finally:
        os.close(fd)


def _signature(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def read_local_tail(path: str | os.PathLike[str], max_bytes: int = 131072) -> tuple[bytes, int, bool]:
    """Safely read the bounded tail of an explicitly selected regular log file.

    This reader is for scheduler-provided log paths, which can be absolute and
    need not live under an output-contract root. Final symlinks and all special
    files are refused; nonblocking open prevents FIFO replacement races from
    hanging the worker. The last return value indicates whether the opened file
    and current named file were unchanged throughout the read.
    """
    if type(max_bytes) is not int or not 0 <= max_bytes <= DEFAULT_MAX_BYTES:
        raise ValueError(f"max_bytes must be an integer from 0 to {DEFAULT_MAX_BYTES}")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Log must be a regular file; directories and special files are refused")
        os.lseek(fd, max(0, before.st_size - max_bytes), os.SEEK_SET)
        chunks = []
        remaining = min(max_bytes, before.st_size)
        expected = remaining
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        try:
            named = os.stat(path, follow_symlinks=False)
            stable = _signature(before) == _signature(after) == _signature(named)
        except OSError:
            stable = False
        data = b"".join(chunks)
        return data, before.st_size, stable and len(data) == expected
    finally:
        os.close(fd)


def _check(name: str, status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, **details}


def _finish(output: dict[str, Any]) -> dict[str, Any]:
    statuses = {item["status"] for item in output["checks"]}
    output["status"] = ("invalid" if "fail" in statuses else "error" if "error" in statuses
                        else "not_checked" if "not_checked" in statuses else "valid")
    return output


def _content_checks(spec: dict[str, Any], data: bytes, digest: str | None = None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if "sha256" in spec:
        actual = digest if digest is not None else hashlib.sha256(data).hexdigest()
        checks.append(_check("sha256", "pass" if actual == spec["sha256"].lower() else "fail",
                             "SHA-256 digest compared", expected=spec["sha256"].lower(), actual=actual))
    fmt = spec.get("format")
    if not fmt:
        return checks
    try:
        if fmt == "json":
            value = _json(data)
            checks.append(_check("format", "pass", "Valid strict JSON"))
            if "required_keys" in spec:
                missing = [key for key in spec["required_keys"] if not isinstance(value, dict) or key not in value]
                checks.append(_check("required_keys", "fail" if missing else "pass",
                                     "Required top-level JSON keys checked", missing=missing))
        else:
            text = data.decode("utf-8-sig")
            if fmt == "text":
                checks.append(_check("format", "pass", "Valid UTF-8 text"))
                return checks
            reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            header = next(reader, None)
            if not header or len(set(header)) != len(header):
                raise ValueError("CSV needs a nonempty, unique header")
            rows = 0
            for row in reader:
                if not row:
                    continue
                if len(row) != len(header):
                    raise ValueError(f"CSV row {rows + 2} has {len(row)} fields; expected {len(header)}")
                rows += 1
            checks.append(_check("format", "pass", "Valid UTF-8 CSV with consistent row widths", rows=rows))
            if "columns" in spec:
                missing = [key for key in spec["columns"] if key not in header]
                checks.append(_check("columns", "fail" if missing else "pass", "Required CSV columns checked", missing=missing))
            for field, compare in (("rows", lambda actual, expected: actual == expected),
                                   ("min_rows", lambda actual, expected: actual >= expected),
                                   ("max_rows", lambda actual, expected: actual <= expected)):
                if field in spec:
                    checks.append(_check(field, "pass" if compare(rows, spec[field]) else "fail",
                                         "CSV data row count checked", actual=rows, expected=spec[field]))
    except csv.Error as exc:
        if "field larger than field limit" in str(exc):
            checks.extend(_check(name, "not_checked", "CSV field exceeds the bounded parser field limit")
                          for name in _required_checks(spec) if name != "sha256")
        else:
            checks.append(_check("format", "fail", f"Invalid {fmt}: {str(exc)[:200]}"))
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        checks.append(_check("format", "fail", f"Invalid {fmt}: {str(exc)[:200]}"))
    return checks


def _required_checks(spec: dict[str, Any]) -> list[str]:
    return [field for field in ("format", "sha256", "columns", "rows", "min_rows", "max_rows", "required_keys") if field in spec]


def _missing(spec: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    required = spec.get("required", True)
    output["checks"].append(_check("presence", "fail" if required else "pass",
                                   "Required output is missing" if required else "Optional output is absent"))
    output["missing"] = True
    return _finish(output)


def _open_local(root_fd: int, path: str) -> tuple[int, int, str]:
    """Return a regular-file candidate fd and its independently owned parent fd."""
    parts = path.split("/")
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            info = os.stat(part, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Output parent must be a directory; symlinks are refused")
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = next_fd
        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Output must be a regular file; symlinks, directories, and special files are refused")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        return fd, parent, parts[-1]
    except BaseException:
        os.close(parent)
        raise


def _local(spec: dict[str, Any], root_fd: int, budget: dict[str, int]) -> dict[str, Any]:
    output: dict[str, Any] = {"path": spec["path"], "checks": [], "source": "local", "stable": None}
    fd = parent = None
    try:
        fd, parent, name = _open_local(root_fd, spec["path"])
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Output changed to a nonregular file before reading")
        output["size"] = before.st_size
        output["checks"].extend([_check("presence", "pass", "Output exists"),
                                 _check("regular_file", "pass", "Regular file opened without following symlinks")])
        for field, compare in (("min_bytes", lambda actual, expected: actual >= expected),
                               ("max_bytes", lambda actual, expected: actual <= expected)):
            if field in spec:
                output["checks"].append(_check(field, "pass" if compare(before.st_size, spec[field]) else "fail",
                                               "File size checked", expected=spec[field], actual=before.st_size))
        names = _required_checks(spec)
        if names:
            remaining = budget["max_bytes"] - budget["bytes_read"]
            if before.st_size > remaining:
                output["checks"].extend(_check(name, "not_checked", "File exceeds remaining shared read budget",
                                              required_bytes=before.st_size, remaining_bytes=remaining) for name in names)
            else:
                chunks: list[bytes] = []
                digest = hashlib.sha256() if "sha256" in spec else None
                remaining = before.st_size
                while remaining:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        break
                    if spec.get("format"):
                        chunks.append(chunk)
                    if digest is not None:
                        digest.update(chunk)
                    budget["bytes_read"] += len(chunk)
                    remaining -= len(chunk)
                if remaining:
                    output["checks"].extend(_check(name, "not_checked", "File was truncated during validation") for name in names)
                else:
                    output["checks"].extend(_content_checks(spec, b"".join(chunks), digest.hexdigest() if digest is not None else None))
        after = os.fstat(fd)
        try:
            if "/" not in spec["path"]:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            else:
                # A parent directory can itself be renamed or replaced while
                # its open fd remains valid. Re-open the declared path securely
                # from the root to verify that it still names this same file.
                verify_fd, verify_parent, _ = _open_local(root_fd, spec["path"])
                try:
                    current = os.fstat(verify_fd)
                finally:
                    os.close(verify_fd)
                    os.close(verify_parent)
            stable = _signature(before) == _signature(after) == _signature(current)
        except (OSError, ValueError):
            stable = False
        output["stable"] = stable
        if not stable:
            # Results from a changing file do not support either success or failure.
            output["checks"] = [_check(item["name"], "not_checked", "Output changed during validation")
                                for item in output["checks"]]
    except FileNotFoundError:
        return _missing(spec, output)
    except ValueError as exc:
        output["checks"].append(_check("safe_path", "fail", str(exc)))
    except OSError as exc:
        status = "fail" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "error"
        output["checks"].append(_check("read", status, f"Cannot inspect output: {str(exc)[:200]}"))
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)
    return _finish(output)


def _remote(spec: dict[str, Any], root: str, files: Any) -> dict[str, Any]:
    output: dict[str, Any] = {"path": spec["path"], "checks": [], "source": "remote", "stable": None}
    try:
        size, _ = files.stat(str(PurePosixPath(root) / spec["path"]))
        if type(size) is not int or size < 0:
            raise OSError("reader returned an invalid file size")
        output["size"] = size
        output["checks"].append(_check("presence", "pass", "Exact remote path has stat metadata"))
        for name in ["safe_path", "regular_file", "stability", *[field for field in ("min_bytes", "max_bytes") if field in spec], *_required_checks(spec)]:
            output["checks"].append(_check(name, "not_checked", "Remote reader cannot prove confinement, regular-file type, and stable content; no content was read"))
    except FileNotFoundError:
        return _missing(spec, output)
    except (OSError, ValueError, TypeError) as exc:
        output["checks"].append(_check("stat", "error", f"Cannot inspect remote output: {str(exc)[:200]}"))
    return _finish(output)


def validate_contract(contract: dict[str, Any], root: str | os.PathLike[str], *, files: Any = None,
                      max_bytes: int = DEFAULT_MAX_BYTES, max_entries: int = DEFAULT_MAX_ENTRIES,
                      root_fd: int | None = None) -> dict[str, Any]:
    """Validate declared outputs without executing user code.

    Bytes are a shared read budget, not a per-file allowance. Oversized files and
    entries beyond the inspection limit are explicitly ``not_checked`` and make
    the overall result ``incomplete``. A concurrent modification similarly
    invalidates observations. CSV row counts exclude the header and blank lines;
    ``columns`` checks a required subset, while ``required_keys`` checks top-level
    JSON keys. All files must be regular; local symlinks are refused throughout
    the path. Remote readers without equivalent safety guarantees are metadata-
    only. Results are JSON-serializable and do not conflate errors with failures.
    """
    result: dict[str, Any] = {"status": "error", "valid": False, "summary": "", "outputs": [], "errors": [],
                             "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                             "budget": {"max_bytes": max_bytes if type(max_bytes) is int and 0 <= max_bytes <= MAX_READ_BYTES else None,
                                        "bytes_read": 0,
                                        "max_entries": max_entries if type(max_entries) is int and 0 <= max_entries <= MAX_OUTPUTS else None,
                                        "entries_checked": 0}}
    supplied_root_fd, root_fd = root_fd, None
    try:
        specs = _contract(contract)
        if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_READ_BYTES:
            raise ValueError(f"max_bytes must be an integer from 0 to {MAX_READ_BYTES}")
        if type(max_entries) is not int or not 0 <= max_entries <= MAX_OUTPUTS:
            raise ValueError(f"max_entries must be an integer from 0 to {MAX_OUTPUTS}")
        reader = files if files is not None else LocalFiles()
        remote = bool(getattr(reader, "remote", False))
        root_path = os.fspath(root)
        if not isinstance(root_path, str) or not root_path or "\x00" in root_path:
            raise ValueError("root must be a nonempty filesystem path")
        if remote:
            if not PurePosixPath(root_path).is_absolute() or ".." in PurePosixPath(root_path).parts:
                raise ValueError("remote root must be an absolute path without traversal")
        elif specs and max_entries:
            root_fd = (os.dup(supplied_root_fd) if supplied_root_fd is not None
                       else os.open(Path(root_path).resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                raise ValueError("confined output root must be a directory")
        for index, spec in enumerate(specs):
            if index >= max_entries:
                output = _finish({"path": spec["path"], "checks": [_check("inspection", "not_checked", "Shared entry budget exhausted")],
                                  "source": "remote" if remote else "local", "stable": None})
            else:
                result["budget"]["entries_checked"] += 1
                output = _remote(spec, root_path, reader) if remote else _local(spec, root_fd, result["budget"])
            result["outputs"].append(output)
        states = {output["status"] for output in result["outputs"]}
        result["status"] = ("error" if "error" in states else "invalid" if "invalid" in states
                            else "incomplete" if "not_checked" in states else "valid")
        result["valid"] = result["status"] == "valid"
        counts = {state: sum(output["status"] == state for output in result["outputs"]) for state in sorted(states)}
        result["summary"] = ", ".join(f"{count} {state}" for state, count in counts.items()) or "No outputs declared"
        result["errors"] = [f"{output['path']}: {check['message']}" for output in result["outputs"]
                            for check in output["checks"] if check["status"] == "error"]
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        result["errors"].append(str(exc)[:300])
        result["summary"] = "Contract validation could not run"
    finally:
        if root_fd is not None:
            os.close(root_fd)
    return result
