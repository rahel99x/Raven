"""Explicit, build-checked native symbols and revision-checked source excerpts.

Trace paths are descriptive, never executable inputs. A caller must select the
binary and source root; resolving a name does not establish source provenance.
Tool output, runtime and cache size are bounded independently of trace size.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from itertools import islice
from pathlib import Path

from ..bounded_command import _process
from ..job_logs.text import display_text
from ..transport_errors import CommandError

SCHEMA = "raven.trace-symbol-resolution/v1"
MAX_BINARY = 2 << 30
MAX_SOURCE = 1 << 20
MAX_CACHE_ENTRIES = 256
MAX_CACHE_BYTES = 16 << 20
MAX_OUTPUT = 128 << 10
_HEX = re.compile(r"[0-9a-fA-F]{4,128}\Z")


def _text(value, maximum=4096):
    return display_text(str(value)[:maximum])


def _fields(symbol):
    if not isinstance(symbol, dict):
        raise ValueError("Select one recorded symbol or stack frame.")
    data = symbol.get("data")
    return {**(data if isinstance(data, dict) else {}), **symbol}


def _number(value):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and len(value) <= 18:
        result = int(value, 16 if value.startswith("0x") else 10)
    else:
        raise ValueError("A recorded native address is required.")
    if not 0 <= result < (1 << 64):
        raise ValueError("Native addresses must be unsigned 64-bit integers.")
    return result


def _address(fields):
    if fields.get("elf_address") is not None:
        return _number(fields["elf_address"]), "recorded ELF virtual address"
    if fields.get("module_offset") is not None:
        # Producers must state the link-time base. A mapped file offset alone
        # is not an ELF address (different PT_LOAD segments may have bias).
        if fields.get("module_base_vaddr") is None:
            raise ValueError("module_offset needs the recorded module_base_vaddr.")
        value = _number(fields["module_offset"]) + _number(fields["module_base_vaddr"])
        return _number(value), "recorded module offset and link-time base"
    if fields.get("load_bias") is not None:
        runtime = fields.get("ip", fields.get("address"))
        # perf-script's IP column is hexadecimal even without a 0x prefix.
        if (
            fields.get("ip") is not None
            and isinstance(runtime, str)
            and re.fullmatch(r"(?:0x)?[0-9a-fA-F]{1,16}", runtime)
        ):
            runtime = int(runtime, 16)
        value = _number(runtime) - _number(fields["load_bias"])
        return _number(value), "recorded instruction pointer minus recorded load bias"
    raise ValueError(
        "Provide elf_address, or an address with its recorded relocation metadata."
    )


def _fingerprint(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BINARY:
        raise ValueError("The selected binary must be a regular file of at most 2 GiB.")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _cache_root(directory):
    root = (
        Path(directory)
        if directory is not None
        else Path(tempfile.gettempdir()) / f"raven-trace-symbols-{os.getuid()}"
    )
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            return None
        return root
    except OSError:
        return None


def _cached(root, key):
    if root is None:
        return None
    try:
        fd = os.open(root / (key + ".json"), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > MAX_OUTPUT
            ):
                return None
            data = json.loads(stream.read(MAX_OUTPUT + 1))
        if (
            isinstance(data, dict)
            and data.get("schema") == SCHEMA
            and isinstance(data.get("frames"), list)
            and len(data["frames"]) <= 64
            and all(
                isinstance(frame, dict)
                and isinstance(frame.get("name"), str)
                and len(frame["name"]) <= 4096
                and (
                    frame.get("file") is None
                    or isinstance(frame["file"], str)
                    and len(frame["file"]) <= 4096
                )
                and (
                    frame.get("line") is None
                    or type(frame["line"]) is int
                    and frame["line"] > 0
                )
                for frame in data["frames"]
            )
        ):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _save_cache(root, key, value):
    if root is None:
        return
    temporary = None
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        if len(encoded) > MAX_OUTPUT:
            return
        # Only private cache-owned regular JSON entries are eviction candidates.
        entries = []
        children = list(islice(root.iterdir(), 4097))
        if len(children) > 4096:
            return
        for child in children:
            if re.fullmatch(r"[0-9a-f]{64}\.json", child.name):
                info = child.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                    entries.append((info.st_mtime_ns, info.st_size, child))
        entries.sort()
        size = sum(item[1] for item in entries)
        while entries and (
            len(entries) >= MAX_CACHE_ENTRIES or size + len(encoded) > MAX_CACHE_BYTES
        ):
            _, length, child = entries.pop(0)
            child.unlink()
            size -= length
        fd, temporary = tempfile.mkstemp(prefix=".symbol-", dir=root)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
        os.replace(temporary, root / (key + ".json"))
    except (OSError, ValueError):
        pass  # Derived cache failure cannot invalidate a successful resolution.
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _frames(output):
    lines = output.splitlines()
    if len(lines) % 2 or len(lines) > 128:
        raise ValueError("Unexpected or excessive addr2line inline-frame output.")
    frames = []
    for index in range(0, len(lines), 2):
        location = lines[index + 1]
        match = re.fullmatch(r"(.*):(\d+|\?)(?: \(discriminator \d+\))?", location)
        file = match[1] if match and match[1] != "??" else None
        line = (
            int(match[2])
            if match and match[2].isdigit() and int(match[2]) > 0
            else None
        )
        frames.append(
            dict(
                name=_text(lines[index]), file=_text(file) if file else None, line=line
            )
        )
    return frames


def _source_path(fields, frame, root):
    value = (
        fields.get("source_relative_path") or fields.get("file") or frame.get("file")
    )
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or not value.isprintable()
    ):
        raise ValueError("No usable recorded source path.")
    path = Path(value)
    candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Recorded source path is outside the selected source root."
        ) from exc
    return candidate


def _git_proof(fields, root, path, raw):
    revision, blob = fields.get("source_revision"), fields.get("source_git_blob")
    if not isinstance(revision, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision
    ):
        return None
    if (
        not isinstance(blob, str)
        or len(blob) != len(revision)
        or not _HEX.fullmatch(blob)
    ):
        return None
    git = shutil.which("git")
    if git is None:
        return None
    repository = Path(
        _process(
            [git, "-C", str(root), "rev-parse", "--show-toplevel"], 2.0, 8192
        ).strip()
    ).resolve()
    relative = path.relative_to(repository).as_posix()
    tree = _process(
        [
            git,
            "-C",
            str(repository),
            "ls-tree",
            "-z",
            "--full-tree",
            revision,
            "--",
            relative,
        ],
        2.0,
        16384,
    )
    entries = tree.split("\0")
    expected = f"{blob}\t{relative}"
    if not any(row.endswith(expected) and row.startswith("100") for row in entries):
        return None
    algorithm = hashlib.sha1 if len(blob) == 40 else hashlib.sha256
    actual = algorithm(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    return (
        dict(method="recorded-git-blob-and-revision", revision=revision, blob=blob)
        if actual == blob.lower()
        else None
    )


def _source(fields, frame, source_root):
    report = dict(
        status="unavailable",
        reason="Select a source root and provide recorded source SHA-256 or exact Git blob/revision evidence.",
    )
    if source_root is None:
        return report
    try:
        root = Path(source_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Select an existing source directory.")
        path = _source_path(fields, frame, root)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE:
                raise ValueError(
                    "Source excerpts require a regular file no larger than 1 MiB."
                )
            raw = stream.read(MAX_SOURCE + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_SOURCE or (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Source changed while checking its provenance.")
        digest = hashlib.sha256(raw).hexdigest()
        expected = fields.get("source_sha256")
        proof = (
            dict(method="recorded-sha256", sha256=digest)
            if isinstance(expected, str) and expected.lower() == digest
            else _git_proof(fields, root, path, raw)
        )
        if proof is None:
            raise ValueError(
                "Source content does not match the recorded SHA-256 or Git blob/revision; no excerpt is shown."
            )
        line = fields.get("line", frame.get("line"))
        if type(line) is not int or not 1 <= line <= 1_000_000:
            raise ValueError(
                "A positive recorded source line is required for an excerpt."
            )
        lines = raw.decode("utf-8", "replace").splitlines()
        if line > len(lines):
            raise ValueError("The recorded line is outside the verified source file.")
        start, end = max(1, line - 5), min(len(lines), line + 5)
        return dict(
            status="verified",
            path=str(path),
            line=line,
            proof=proof,
            lines=[
                dict(line=number, text=_text(lines[number - 1], 4096))
                for number in range(start, end + 1)
            ],
        )
    except (OSError, ValueError, CommandError) as exc:
        report["reason"] = _text(exc)
        return report


def resolve_symbol(symbol, *, binary=None, source_root=None, cache_directory=None):
    """Resolve one address only against an explicitly selected matching ELF.

    Recorded ``elf_address`` is link-time virtual addressing. A ``module_offset``
    requires ``module_base_vaddr``; a runtime ``ip`` requires its ``load_bias``.
    Source text additionally requires content proof, independently of the ELF ID.
    No trace-supplied DSO, tool path, plugin, script or command is executed.
    """
    fields = _fields(symbol)
    result = dict(
        schema=SCHEMA,
        status="unavailable",
        name=_text(fields.get("name", "unresolved")),
        frames=[],
        source=dict(
            status="unavailable",
            reason="Native symbol resolution has not established source provenance.",
        ),
        cache_hit=False,
    )
    if source_root is not None:
        # Python/cooperative source proof is useful without an ELF address.
        # Its independent status never claims native symbol resolution.
        result["source"] = _source(fields, {}, source_root)
    try:
        expected = fields.get("build_id")
        if not isinstance(expected, str) or not _HEX.fullmatch(expected):
            raise ValueError(
                "The recording needs a hexadecimal build_id to verify the selected binary."
            )
        if binary is None:
            raise ValueError(
                "Select the matching binary explicitly; recorded DSO paths are never opened automatically."
            )
        path = Path(binary).resolve(strict=True)
        before = _fingerprint(path)
        address, address_provenance = _address(fields)
        readelf, addr2line = shutil.which("readelf"), shutil.which("addr2line")
        if readelf is None or addr2line is None:
            raise ValueError(
                "Native resolution requires readelf and addr2line on this viewing host."
            )
        notes = _process([readelf, "--notes", "--", str(path)], 2.0, MAX_OUTPUT)
        ids = re.findall(r"Build ID:\s*([0-9a-fA-F]+)", notes)
        if (
            len(set(value.lower() for value in ids)) != 1
            or ids[0].lower() != expected.lower()
        ):
            raise ValueError(
                "The selected binary build ID does not match the recording."
            )
        if _fingerprint(path) != before:
            raise ValueError("The selected binary changed while checking its build ID.")
        key = hashlib.sha256(
            json.dumps(
                [expected.lower(), address, before, str(path), readelf, addr2line],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        root = _cache_root(cache_directory)
        cached = _cached(root, key)
        frames = (
            cached["frames"]
            if cached
            else _frames(
                _process(
                    [addr2line, "-f", "-C", "-i", "-e", str(path), hex(address)],
                    2.0,
                    MAX_OUTPUT,
                )
            )
        )
        if _fingerprint(path) != before:
            raise ValueError("The selected binary changed while resolving the address.")
        if not cached:
            _save_cache(root, key, dict(schema=SCHEMA, frames=frames))
        result.update(
            status="resolved"
            if any(frame["name"] != "??" for frame in frames)
            else "unresolved",
            binary=str(path),
            build_id=expected.lower(),
            elf_address=address,
            address_provenance=address_provenance,
            frames=frames,
            cache_hit=cached is not None,
            tools=dict(readelf=readelf, addr2line=addr2line),
            qualification="ELF build ID checked on this viewing host; not a capture-adapter qualification",
        )
        result["source"] = _source(fields, frames[0] if frames else {}, source_root)
        if result["status"] == "unresolved":
            result["reason"] = (
                "The matching binary has no symbol/debug mapping for this address."
            )
    except (OSError, ValueError, TypeError, IndexError, KeyError, CommandError) as exc:
        result["reason"] = _text(exc)
    return result
