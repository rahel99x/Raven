"""Copy/verify migration and format routing for native recording archives."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time

from . import series_blocks as blocks

MAX_SOURCE = 64 << 20
MAX_LINE = 1 << 20
MAX_RECORDS = 1000000


def compact_path(path):
    return str(path)[:-6] + ".rvb" if str(path).endswith(".jsonl") else str(path)


def json_path(path):
    return str(path)[:-4] + ".jsonl" if str(path).endswith(".rvb") else str(path)


def _canonical(row):
    return (
        json.dumps(row, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        + "\n"
    ).encode()


def append_jsonl(path, samples):
    """Append finite records without joining an earlier torn final line."""
    payload = b"".join(_canonical(sample) for sample in samples)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = blocks._open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND)
    initial = os.fstat(fd).st_size
    try:
        if initial and os.pread(fd, 1, initial - 1) != b"\n":
            payload = b"\n" + payload
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("Short JSONL recording append")
            view = view[written:]
        os.fdatasync(fd) if hasattr(os, "fdatasync") else os.fsync(fd)
    except BaseException:
        os.ftruncate(fd, initial)
        os.fsync(fd)
        raise
    finally:
        os.close(fd)


def json_rows(
    path, *, cancel=None, max_bytes=MAX_SOURCE, skip_invalid=False, diagnostics=None
):
    fd = blocks._open(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as source:
        size = os.fstat(source.fileno()).st_size
        if size > max_bytes:
            raise ValueError("JSONL source exceeds the migration byte budget")
        used = count = 0
        deadline = time.monotonic() + 30.0
        while True:
            if cancel is not None and cancel.is_set():
                raise ValueError("Recording migration cancelled; source is unchanged")
            if time.monotonic() > deadline:
                raise ValueError("Recording migration reached its time budget")
            raw = source.readline(MAX_LINE + 1)
            if not raw:
                break
            used += len(raw)
            if used > max_bytes or len(raw) > MAX_LINE:
                raise ValueError("JSONL record exceeds the migration read budget")
            if not raw.endswith(b"\n"):
                if skip_invalid:
                    if diagnostics is not None:
                        diagnostics["ignored_records"] = (
                            diagnostics.get("ignored_records", 0) + 1
                        )
                    continue
                raise ValueError(
                    "JSONL source has an incomplete final record; preserve it for explicit recovery"
                )
            if not raw.strip():
                continue
            try:
                row = blocks._json(raw)
                if not isinstance(row, dict):
                    raise ValueError("JSONL source contains a non-object record")
            except (ValueError, RecursionError):
                if not skip_invalid:
                    raise
                if diagnostics is not None:
                    diagnostics["ignored_records"] = (
                        diagnostics.get("ignored_records", 0) + 1
                    )
                continue
            count += 1
            if count > MAX_RECORDS:
                raise ValueError("Recording migration reached its row budget")
            yield row
        if os.fstat(source.fileno()).st_size != size:
            raise ValueError("Source changed during migration")


def block_rows(
    path, *, identity=None, cancel=None, max_bytes=MAX_SOURCE, max_records=MAX_RECORDS
):
    fd = blocks._open(path, os.O_RDONLY)
    used = decoded = count = 0
    deadline = time.monotonic() + 30.0
    with os.fdopen(fd, "rb") as source:
        while True:
            if cancel is not None and cancel.is_set():
                raise ValueError("Recording operation cancelled")
            if time.monotonic() > deadline:
                raise ValueError("Recording operation reached its time budget")
            header = source.read(blocks.HEADER.size)
            if not header:
                break
            if len(header) != blocks.HEADER.size:
                raise ValueError("Truncated compact header")
            magic, raw, size, rows, _, _ = blocks.HEADER.unpack(header)
            if (
                magic != blocks.MAGIC
                or not 0 < size <= blocks.MAX_PACKED
                or not 0 < raw <= blocks.MAX_RAW
                or not 0 < rows <= blocks.MAX_ROWS
            ):
                raise ValueError("Invalid compact header or bounds")
            used += blocks.HEADER.size + size + blocks.FOOTER.size
            decoded += raw
            if used > max_bytes or decoded > MAX_SOURCE:
                raise ValueError("Recording operation reached its byte budget")
            frame = header + source.read(size + blocks.FOOTER.size)
            values = blocks.decode(
                frame, blocks._identity(path) if identity is None else identity
            )
            count += len(values)
            if count > max_records:
                raise ValueError("Recording operation reached its row budget")
            yield from values


def _fingerprint(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Recording source must be a regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def migrate(
    source,
    target=None,
    *,
    apply=False,
    cancel=None,
    max_bytes=MAX_SOURCE,
    quota=None,
    staging_token=None,
    skip_invalid=True,
):
    """Copy to a new verified compact file; preserve JSONL, never overwrite.

    The caller must hold the Store scope lock for publication. Preview is
    read-only. A failed or cancelled conversion leaves no published target.
    """
    source = os.path.abspath(os.fspath(source))
    target = os.path.abspath(os.fspath(target or compact_path(source)))
    if not source.endswith(".jsonl") or not target.endswith(".rvb") or source == target:
        raise ValueError("Migration requires a JSONL source and separate .rvb target")
    if blocks._identity(source) != blocks._identity(target):
        raise ValueError("Keep the exact attempt basename when migrating recordings")
    before = _fingerprint(source)
    if os.path.lexists(target):
        raise ValueError(
            "Compact target already exists; source and target are preserved"
        )
    digest = hashlib.sha256()
    count = encoded = 0
    staging_token = staging_token or secrets.token_hex(8)
    if len(staging_token) != 16 or any(
        c not in "0123456789abcdef" for c in staging_token
    ):
        raise ValueError("Invalid staging token")
    staging = target + ".stage-" + staging_token
    output = None
    try:
        if apply:
            output = os.fdopen(
                blocks._open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY), "wb"
            )
        batch = []

        def flush():
            nonlocal encoded
            for frame in blocks.frames(batch, target):
                encoded += len(frame)
                if encoded > max_bytes or quota is not None and encoded > quota:
                    raise ValueError("Compact publication exceeds its storage quota")
                if output is not None:
                    output.write(frame)

        diagnostics = {}
        for row in json_rows(
            source,
            cancel=cancel,
            max_bytes=max_bytes,
            skip_invalid=skip_invalid,
            diagnostics=diagnostics,
        ):
            digest.update(_canonical(row))
            count += 1
            batch.append(row)
            if len(batch) == blocks.MAX_ROWS:
                flush()
                batch.clear()
        if batch:
            flush()
        if _fingerprint(source) != before:
            raise ValueError("Source changed before migration publication")
        result = {
            "source": source,
            "target": target,
            "raw_samples": count,
            "source_bytes": before[2],
            "compact_bytes": encoded,
            "sha256_values": digest.hexdigest(),
            "fidelity": "raw-lossless",
            "status": "preview",
            "source_preserved": True,
        }
        result["ignored_records"] = diagnostics.get("ignored_records", 0)
        result["source_coverage"] = (
            "partial" if result["ignored_records"] else "complete"
        )
        if output is None:
            return result
        output.flush()
        os.fsync(output.fileno())
        output.close()
        output = None
        verify = hashlib.sha256()
        verified = 0
        for row in block_rows(
            staging,
            identity=blocks._identity(target),
            cancel=cancel,
            max_bytes=max_bytes,
        ):
            verify.update(_canonical(row))
            verified += 1
        if (
            verified != count
            or verify.digest() != digest.digest()
            or _fingerprint(source) != before
        ):
            raise ValueError(
                "Compact copy verification failed; original source is unchanged"
            )
        # Hard-link publication is atomic and refuses an existing destination.
        # The private state writer lock supplies scope ownership; no overwrite.
        os.link(staging, target, follow_symlinks=False)
        parent = os.open(
            os.path.dirname(target), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        result["status"] = "published"
        return result
    finally:
        if output is not None:
            output.close()
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass


def uses_blocks(path, cache=None):
    packed = compact_path(path)
    value = (
        cache.get(path)
        if cache is not None and path in cache
        else (str(path).endswith(".rvb") or os.path.exists(packed))
    )
    if cache is not None:
        cache[path] = value
        while len(cache) > 510:
            cache.pop(next(iter(cache)))
    return value


def read_path(path):
    """Choose an existing canonical block file, otherwise the JSONL original."""
    packed = compact_path(path)
    return packed if os.path.exists(packed) else json_path(path)


def rekey(source, target):
    """Copy an owned session prefix into its now-proven exact attempt.

    Original evidence remains untouched. Prefix comparison makes retry after
    publication idempotent without a second, crash-sensitive sidecar commit.
    The caller serializes publication under its state writer ownership.
    """
    source = read_path(source)
    if not os.path.exists(source):
        return
    target = read_path(target) if os.path.exists(read_path(target)) else target
    before = _fingerprint(source)
    old = list(block_rows(source) if source.endswith(".rvb") else json_rows(source))
    if not old:
        return
    current = []
    if os.path.exists(target):
        current = list(
            block_rows(target) if target.endswith(".rvb") else json_rows(target)
        )
        if len(current) >= len(old) and all(
            _canonical(a) == _canonical(b) for a, b in zip(old, current)
        ):
            return
    temporary = target + ".promote-" + secrets.token_hex(8)
    fd = blocks._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as stream:
            if target.endswith(".rvb"):
                for payload in blocks.frames(old + current, target):
                    stream.write(payload)
            else:
                for row in old + current:
                    stream.write(_canonical(row))
            stream.flush()
            os.fsync(stream.fileno())
        if _fingerprint(source) != before:
            raise ValueError("Session recording changed during identity promotion")
        os.replace(temporary, target)
        parent = os.open(os.path.dirname(target), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def append(path, samples, *, writer=None, quota=None, cache=None, on_recovery=None):
    """New files follow the preference; conversion of existing JSONL is explicit."""
    packed = compact_path(path)
    use_blocks = uses_blocks(path, cache)
    if not use_blocks:
        return False
    original = json_path(path)
    if not os.path.exists(packed) and os.path.exists(original):
        # Never perform a bulk copy while publishing an ordinary sample.
        # Setup/CLI migration supplies its own bounded process and ownership.
        if writer is not None:
            for row in samples:
                writer(original, _canonical(row))
        else:
            append_jsonl(original, samples)
        return True
    blocks.append(packed, samples, writer=writer, cache=cache, on_recovery=on_recovery)
    return True
