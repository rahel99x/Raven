"""Complete exact-byte log exports, independent of the rendered tail buffer.

The caller dispatches copy_full_log on ResearchHub's existing worker and publishes
its returned message on the UI thread. No additional executor or job action exists.
"""

from __future__ import annotations

import os
import stat
import uuid

from . import clipboard
from .local_files import LocalFiles
from .remote_files import RemoteFiles


CHUNK_BYTES = 1024 * 1024


def _clean(value, limit=4096):
    return "".join(c if c.isprintable() else " " for c in str(value))[:limit]


def _snapshot(files, path):
    method = getattr(files, "snapshot_stat", None)
    inherited_local = (
        getattr(type(files), "snapshot_stat", None) is LocalFiles.snapshot_stat
    )
    if method is not None and (
        type(files) is LocalFiles
        or isinstance(files, RemoteFiles)
        or not inherited_local
    ):
        value = method(path)
        if not isinstance(value, dict) or set(value) != {"size", "ident", "updated"}:
            raise OSError("log backend returned invalid snapshot metadata")
    else:
        # Adapters that expose only the original stat/read interface keep all I/O
        # on that backend; there is no local-path fallback in SSH mode.
        size, identity = files.stat(path)
        value = {"size": size, "ident": identity, "updated": None}
    if (
        isinstance(value["size"], bool)
        or not isinstance(value["size"], int)
        or value["size"] < 0
    ):
        raise OSError("log backend returned an invalid size")
    return value


def _local_fd_snapshot(fd):
    info = os.fstat(fd)
    return {
        "size": info.st_size,
        "ident": (info.st_dev, info.st_ino),
        "updated": (info.st_mtime_ns, info.st_ctime_ns),
    }


def _check_snapshot(before, now, last_size):
    if now["ident"] != before["ident"]:
        raise OSError(
            "selected log was replaced or rotated during copying; retry the current file"
        )
    if now["size"] < before["size"] or now["size"] < last_size:
        raise OSError(
            "selected log was truncated during copying; retry the current file"
        )
    if (
        now["size"] == before["size"]
        and before["updated"] is not None
        and now["updated"] != before["updated"]
    ):
        raise OSError(
            "selected log changed in place during copying; retry the current file"
        )
    return max(last_size, now["size"])


def _cancelled(cancel):
    if cancel is not None and cancel():
        raise InterruptedError("full log copy cancelled")


def copy_full_log(
    path,
    state_dir=None,
    *,
    files=None,
    use_osc52=True,
    use_tools=True,
    tty_path="/dev/tty",
    cancel=None,
    progress=None,
    destination="copy",
    editor_context=None,
) -> dict:
    """Stream all initial bytes into a private unique export, then copy that file.

    Active logs may grow: the export is the initial byte range and says so. A
    replacement, observed shrink, mutation without growth, or short read prevents
    clipboard delivery and removes the incomplete export. The producer is never
    paused or locked; concurrent appends cannot be an atomic application snapshot.
    """
    result = {
        "status": "error",
        "message": "",
        "export_path": "",
        "source_path": "",
        "bytes": 0,
        "lines": 0,
        "clipboard": {"methods": [], "warnings": []},
        "snapshot_bytes": 0,
        "grew": False,
    }
    files = files or LocalFiles()
    directory_fd = source_fd = None
    temporary = final_name = None
    export_base = ""
    try:
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 4096
            or not path.isprintable()
        ):
            raise ValueError("select an exact printable log file path before copying")
        result["source_path"] = path
        _cancelled(cancel)
        if type(files) is LocalFiles:
            # O_NONBLOCK prevents a mistakenly selected FIFO from freezing the UI
            # worker; the held descriptor protects against pathname substitution.
            source_fd = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise OSError("log source must be a regular file")
            before = _local_fd_snapshot(source_fd)
            _check_snapshot(before, _snapshot(files, path), before["size"])
        else:
            before = _snapshot(files, path)
        result["snapshot_bytes"] = before["size"]
        from .source_budget import source_read
        from .raven_execution import router_for

        if router_for().managed and before["size"] > 2 << 20:
            raise ValueError(
                "Full-log clipboard export exceeds Light's 2 MiB window; use the reviewed Heavy log-bundle export"
            )
        source_read(before["size"])
        if progress:
            progress(0, before["size"])
        from .export import export_dir

        export_base = export_dir(state_dir)
        directory_fd = os.open(
            export_base,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        token = uuid.uuid4().hex
        temporary = ".log-copy-" + token + ".tmp"
        final_name = "log-full-" + token + ".log"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        offset, last_size, last_byte = 0, before["size"], b""
        with os.fdopen(fd, "wb") as output:
            while offset < before["size"]:
                _cancelled(cancel)
                current = _snapshot(files, path)
                last_size = _check_snapshot(before, current, last_size)
                if source_fd is not None:
                    last_size = _check_snapshot(
                        before, _local_fd_snapshot(source_fd), last_size
                    )
                count = min(CHUNK_BYTES, before["size"] - offset)
                data = (
                    os.read(source_fd, count)
                    if source_fd is not None
                    else files.read(path, offset, count)
                )
                if not isinstance(data, bytes) or len(data) != count:
                    raise OSError(
                        "selected log returned a short or invalid read; no partial copy was published"
                    )
                output.write(data)
                result["lines"] += data.count(b"\n")
                last_byte = data[-1:]
                offset += len(data)
                result["bytes"] = offset
                if progress:
                    progress(offset, before["size"])
                _cancelled(cancel)
            current = _snapshot(files, path)
            last_size = _check_snapshot(before, current, last_size)
            if source_fd is not None:
                last_size = _check_snapshot(
                    before, _local_fd_snapshot(source_fd), last_size
                )
            result["grew"] = current["size"] > before["size"]
            if last_byte and last_byte != b"\n":
                result["lines"] += 1
            _cancelled(cancel)
            output.flush()
        # Publish without overwriting even if a generated name unexpectedly
        # collides with an existing export.
        os.link(
            temporary,
            final_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_fd)
        temporary = None
        result["export_path"] = os.path.join(export_base, final_name)
        _cancelled(cancel)
        if use_osc52 or use_tools or destination == "yank":
            try:
                delivered = clipboard.copy_file(
                    result["export_path"],
                    tty_path=tty_path,
                    use_osc52=use_osc52,
                    use_tools=use_tools,
                    cancel=cancel,
                    destination=destination,
                    editor_context=editor_context,
                )
            except Exception as exc:
                delivered = {
                    "methods": [],
                    "warnings": ["Clipboard delivery failed: " + _clean(exc)],
                    "bytes": result["bytes"],
                    "text": None,
                }
            result["clipboard"] = delivered
        else:
            result["clipboard"] = {
                "methods": [],
                "warnings": [],
                "bytes": result["bytes"],
                "text": None,
            }
        _cancelled(cancel)
        warnings = result["clipboard"].get("warnings", [])
        result["status"] = "partial" if warnings or result["grew"] else "ready"
        noun = "line" if result["lines"] == 1 else "lines"
        message = f"Saved full log: {result['lines']} {noun}, {result['bytes']} bytes to {result['export_path']}"
        methods = result["clipboard"].get("methods", [])
        if methods:
            message += "; " + ", ".join(methods)
        if result["grew"]:
            message += f"; log grew during copying: saved its initial {before['size']}-byte snapshot"
        if warnings:
            message += "; " + "; ".join(warnings)
        result["message"] = _clean(message, 8192)
        return result
    except Exception as exc:
        result["status"] = "error"
        result["message"] = "Full log copy failed: " + _clean(exc)
        # Nothing incomplete or cancelled is advertised as a usable export.
        if result["export_path"] and final_name and directory_fd is not None:
            try:
                os.unlink(final_name, dir_fd=directory_fd)
            except OSError:
                pass
        result["export_path"] = ""
        result["clipboard"] = {"methods": [], "warnings": []}
        return result
    finally:
        if temporary and directory_fd is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
        if source_fd is not None:
            os.close(source_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def copy_log_selection(
    chunks,
    state_dir=None,
    *,
    source_path="",
    use_osc52=True,
    use_tools=True,
    tty_path="/dev/tty",
    cancel=None,
    progress=None,
    destination="copy",
    editor_context=None,
) -> dict:
    """Export a pinned selection's immutable raw byte references on the worker.

    ``chunks`` contains the original selected line bytes and any exact newline
    separators captured by the UI. It is consumed once, without joining lines or
    consulting a source pathname that may have rotated since selection.
    """
    result = {
        "status": "error",
        "message": "",
        "export_path": "",
        "source_path": "",
        "bytes": 0,
        "lines": 0,
        "clipboard": {"methods": [], "warnings": []},
        "snapshot_bytes": 0,
        "grew": False,
    }
    directory_fd = None
    temporary = final_name = None
    try:
        if (
            not isinstance(source_path, str)
            or len(source_path) > 4096
            or (source_path and not source_path.isprintable())
        ):
            raise ValueError("selection source path must be an optional printable path")
        result["source_path"] = source_path
        _cancelled(cancel)
        from .export import export_dir

        export_base = export_dir(state_dir)
        directory_fd = os.open(
            export_base,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        token = uuid.uuid4().hex
        temporary = ".log-selection-" + token + ".tmp"
        final_name = "log-selected-" + token + ".log"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        last_byte = b""
        with os.fdopen(fd, "wb") as output:
            for chunk in chunks:
                _cancelled(cancel)
                if not isinstance(chunk, bytes):
                    raise ValueError("log selection chunks must be immutable bytes")
                # A selected unterminated line can be large. Memoryview slices
                # avoid copying that pinned byte reference into another buffer.
                view = memoryview(chunk)
                for offset in range(0, len(view), CHUNK_BYTES):
                    _cancelled(cancel)
                    block = view[offset : offset + CHUNK_BYTES]
                    output.write(block)
                    result["bytes"] += len(block)
                    if progress:
                        progress(result["bytes"], None)
                    _cancelled(cancel)
                result["lines"] += chunk.count(b"\n")
                if chunk:
                    last_byte = chunk[-1:]
            if last_byte and last_byte != b"\n":
                result["lines"] += 1
            result["snapshot_bytes"] = result["bytes"]
            _cancelled(cancel)
            output.flush()
        os.link(
            temporary,
            final_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_fd)
        temporary = None
        result["export_path"] = os.path.join(export_base, final_name)
        _cancelled(cancel)
        if use_osc52 or use_tools or destination == "yank":
            try:
                delivered = clipboard.copy_file(
                    result["export_path"],
                    tty_path=tty_path,
                    use_osc52=use_osc52,
                    use_tools=use_tools,
                    cancel=cancel,
                    destination=destination,
                    editor_context=editor_context,
                )
            except Exception as exc:
                delivered = {
                    "methods": [],
                    "warnings": ["Clipboard delivery failed: " + _clean(exc)],
                    "bytes": result["bytes"],
                    "text": None,
                }
            result["clipboard"] = delivered
        else:
            result["clipboard"] = {
                "methods": [],
                "warnings": [],
                "bytes": result["bytes"],
                "text": None,
            }
        _cancelled(cancel)
        warnings = result["clipboard"].get("warnings", [])
        result["status"] = "partial" if warnings else "ready"
        noun = "line" if result["lines"] == 1 else "lines"
        message = f"Saved selected log: {result['lines']} {noun}, {result['bytes']} bytes to {result['export_path']}"
        methods = result["clipboard"].get("methods", [])
        if methods:
            message += "; " + ", ".join(methods)
        if warnings:
            message += "; " + "; ".join(warnings)
        result["message"] = _clean(message, 8192)
        return result
    except Exception as exc:
        result["status"] = "error"
        result["message"] = "Selected log copy failed: " + _clean(exc)
        if result["export_path"] and final_name and directory_fd is not None:
            try:
                os.unlink(final_name, dir_fd=directory_fd)
            except OSError:
                pass
        result["export_path"] = ""
        result["clipboard"] = {"methods": [], "warnings": []}
        return result
    finally:
        if temporary and directory_fd is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)
