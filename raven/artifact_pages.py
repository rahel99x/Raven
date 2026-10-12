"""Confined, stable, bounded background pages for declared output artifacts."""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import stat

from . import projects
from .artifacts import _contract, _open_local, _signature
from .log_presentation import json_page

PAGE_ROWS = 128
PAGE_BYTES = 262144
RECORD_BYTES = 1048576
SORT_BYTES = 8388608
SORT_ROWS = 50000
JSON_BYTES = 8388608


def _natural(value):
    try:
        number = float(value)
        if math.isfinite(number):
            return (0, number)
    except (TypeError, ValueError, OverflowError):
        pass
    return (1, tuple((0, len(piece.lstrip("0") or "0"), piece.lstrip("0") or "0")
                     if piece.isascii() and piece.isdigit() else (1, piece.casefold())
                     for piece in re.split(r"(\d+)", value)))


def read_page(root, specification, *, files=None, offset=0, row=0, columns=None, sort=None, collapsed=()):
    """Publish one complete page; global CSV sort is limited and explicit.

    CSV records are parsed with their actual quoted newline boundaries. Text
    pages retain byte continuations for oversized lines. All opens remain
    anchored to the declared root, including the final identity recheck.
    """
    if getattr(files, "remote", False):
        from .project_remote import request
        return request(files, "read_artifact_page", root, specification, offset=offset, row=row,
                       columns=columns, sort=sort, collapsed=collapsed)
    projects._local(files)
    _contract({"version": 1, "outputs": [specification]})
    if type(offset) is not int or offset < 0 or type(row) is not int or row < 0:
        raise ValueError("Artifact page position must be a nonnegative integer")
    root, root_fd = projects._root(root)
    path = specification["path"]
    fd = parent = None
    try:
        fd, parent, name = _open_local(root_fd, path)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Artifact is not a regular file")
        if offset > before.st_size:
            raise ValueError("Artifact page is outside the current file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            result = _read(stream, before.st_size, specification.get("format", "text"), offset, row, columns, sort, collapsed)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        verify, verify_parent, _ = _open_local(root_fd, path)
        try:
            if any(_signature(before) != _signature(info) for info in (after, named, os.fstat(verify))):
                raise ValueError("Artifact changed during page inspection; refresh to retry")
        finally:
            os.close(verify)
            os.close(verify_parent)
        result.update(root=root, path=path, size=before.st_size, identity=_signature(before), status="ready")
        return result
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)
        os.close(root_fd)


def _decode(raw, *, first=False):
    if b"\x00" in raw:
        raise ValueError("Binary artifact has no text preview")
    try:
        return raw.decode("utf-8-sig" if first else "utf-8")
    except UnicodeError as exc:
        raise ValueError("Artifact is not valid UTF-8") from exc


def _csv_records(stream, budget):
    origin = stream.tell()
    def physical_lines():
        while True:
            raw = stream.readline(RECORD_BYTES + 1)
            if len(raw) > RECORD_BYTES or stream.tell() - origin > budget:
                raise ValueError("CSV record/page exceeds the inspection byte limit")
            if not raw:
                return
            yield _decode(raw, first=origin == 0 and stream.tell() == len(raw))
    try:
        yield from csv.reader(physical_lines(), strict=True)
    except csv.Error as exc:
        raise ValueError("Invalid CSV record: " + str(exc)) from exc


def _read(stream, size, format_, offset, row, columns, sort, collapsed):
    if format_ == "csv":
        return _csv(stream, size, offset, row, columns, sort)
    if format_ == "json" and size <= JSON_BYTES:
        raw = stream.read(JSON_BYTES + 1)
        value = projects._json(raw)
        nodes, more = json_page(value, collapsed, row, PAGE_ROWS)
        return dict(format="json", json_value=value, nodes=nodes, lines=[item["text"] for item in nodes],
                    offset=0, next_offset=size, row=row, next_row=row + len(nodes), bytes_read=len(raw), truncated=more,
                    summary=f"{size} source bytes; JSON nodes {row + 1 if nodes else 0}-{row + len(nodes)}; expandable tree", has_next=more)
    stream.seek(offset)
    pieces = []
    end = offset
    while len(pieces) < PAGE_ROWS and end - offset < PAGE_BYTES:
        raw = stream.readline(min(RECORD_BYTES, PAGE_BYTES - (end - offset)))
        if not raw:
            break
        # Keep a multibyte glyph intact at a byte-limited continuation boundary.
        if not raw.endswith(b"\n") and stream.tell() < size:
            for _ in range(3):
                try:
                    _decode(raw, first=offset == 0 and not pieces)
                    break
                except ValueError:
                    next_byte = stream.read(1)
                    if not next_byte:
                        break
                    raw += next_byte
        text = _decode(raw, first=offset == 0 and not pieces).rstrip("\r\n")
        pieces.append(text)
        end = stream.tell()
    warning = "; JSON exceeds 8 MiB structured limit; text pages" if format_ == "json" else ""
    return dict(format="text", lines=pieces, offset=offset, next_offset=end, row=row, next_row=row + len(pieces),
                bytes_read=end - offset, truncated=end < size, has_next=end < size,
                summary=f"Bytes {offset}-{end} of {size}; {len(pieces)} display rows{warning}")


def _csv(stream, size, offset, row, columns, sort):
    reader = _csv_records(stream, SORT_BYTES if sort else PAGE_BYTES + RECORD_BYTES)
    header = next(reader, [])
    header_end = stream.tell()
    if not header:
        return dict(format="csv", header=[], records=[], lines=[], offset=0, next_offset=size, row=0,
                    next_row=0, bytes_read=header_end, has_next=False, truncated=False, summary="Empty CSV")
    indexes = list(range(len(header))) if columns is None else list(columns)
    if not indexes or any(type(index) is not int or not 0 <= index < len(header) for index in indexes):
        raise ValueError("CSV columns must contain existing column indexes")
    if sort:
        column, direction = sort
        if type(column) is not int or not 0 <= column < len(header) or direction not in ("asc", "desc"):
            raise ValueError("CSV sort column or direction is invalid")
        if size > SORT_BYTES:
            raise ValueError("Global CSV sorting supports at most 8 MiB; use unsorted pages for this file")
        records = []
        for number, values in enumerate(reader):
            if number >= SORT_ROWS:
                raise ValueError("Global CSV sorting supports at most 50000 records; no partial sort was applied")
            records.append((number, values))
        known = [item for item in records if column < len(item[1]) and item[1][column] != ""]
        missing = [item for item in records if column >= len(item[1]) or item[1][column] == ""]
        known.sort(key=lambda item: _natural(item[1][column]), reverse=direction == "desc")
        records = known + missing
        total = len(records)
        selected = records[row:row + PAGE_ROWS]
        end = size
        has_next = row + len(selected) < total
        summary = f"Records {row + 1 if selected else 0}-{row + len(selected)} of {total}; global {header[column]} {direction}"
    else:
        stream.seek(offset or header_end)
        reader = _csv_records(stream, PAGE_BYTES + RECORD_BYTES)
        selected = []
        while len(selected) < PAGE_ROWS and stream.tell() - (offset or header_end) < PAGE_BYTES:
            values = next(reader, None)
            if values is None:
                break
            selected.append((row + len(selected), values))
        end = stream.tell()
        has_next = end < size
        summary = f"Records {row + 1 if selected else 0}-{row + len(selected)}; bytes {offset or header_end}-{end} of {size}; original order"
    display_header = [header[index] for index in indexes]
    lines = [" | ".join(display_header)] + [" | ".join(values[index] if index < len(values) else "" for index in indexes) for _, values in selected]
    return dict(format="csv", header=header, displayed_header=display_header, columns=indexes, records=selected,
                lines=lines, offset=offset, next_offset=end, row=row, next_row=row + len(selected),
                bytes_read=end if sort else header_end + end - (offset or header_end), has_next=has_next,
                truncated=has_next, sort=sort, summary=summary)
