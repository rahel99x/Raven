"""Bounded, immutable scientific identities for exact Slurm array indices.

Manifest paths are declarations only: this module never opens inputs/outputs,
expands ranges, executes commands, or substitutes array launch order for indices.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import re
import stat
from types import MappingProxyType

SCHEMA = "raven.array-manifest/v1"
MAX_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_INDEX = (1 << 63) - 1


def _text(value, name, limit=4096, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value) or any(not c.isprintable() for c in value):
        raise ValueError(f"{name} must be a printable string of at most {limit} characters")
    return value


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate JSON key: " + key[:80])
        value[key] = item
    return value


@dataclass(frozen=True)
class Entry:
    index: int
    id: str
    label: str
    parameters: object
    inputs: tuple
    outputs: tuple
    search: str = field(repr=False)

    def __deepcopy__(self, memo):
        return self

    def document(self):
        return {"index": self.index, "id": self.id, "label": self.label,
                "parameters": dict(self.parameters), "inputs": list(self.inputs), "outputs": list(self.outputs)}


@dataclass(frozen=True)
class Manifest:
    cluster: str
    array_id: str
    entries: tuple
    revision: str
    by_index: object = field(repr=False)

    def __deepcopy__(self, memo):
        return self

    def matches(self, array_id, cluster):
        return str(array_id) == self.array_id and str(cluster or "") == self.cluster

    def document(self):
        return {"schema": SCHEMA, "cluster": self.cluster, "array_id": self.array_id,
                "entries": [entry.document() for entry in self.entries]}

    def lookup(self, job_id, cluster=""):
        match = re.fullmatch(r"([1-9][0-9]*)_([0-9]+)", str(job_id))
        if not match or not self.matches(match[1], cluster) or len(match[2]) > 19:
            return None
        return self.by_index.get(int(match[2]))

    def search(self, query):
        query = _text(query, "search", 256, empty=True).casefold()
        return tuple(entry for entry in self.entries if not query or query in entry.search)


def parse(data):
    """Validate a complete bounded JSON document and freeze its exact mapping."""
    if not isinstance(data, (bytes, str)) or len(data) > MAX_BYTES:
        raise ValueError("array manifest exceeds the 4 MiB limit")
    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if len(data.encode("utf-8")) > MAX_BYTES:
            raise ValueError("array manifest exceeds the 4 MiB limit")
        document = json.loads(data, object_pairs_hook=_pairs,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("array manifest is not bounded UTF-8 JSON") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "cluster", "array_id", "entries"} or document.get("schema") != SCHEMA:
        raise ValueError("expected raven.array-manifest/v1 with schema, cluster, array_id and entries")
    cluster = _text(document["cluster"], "cluster", 128, empty=True)
    array_id = document["array_id"]
    if not isinstance(array_id, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", array_id) or int(array_id) > MAX_INDEX:
        raise ValueError("array_id must be a positive canonical decimal parent job ID string")
    values = document["entries"]
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ENTRIES:
        raise ValueError("entries must contain 1 to 10000 explicit array-index records")
    entries, indices, identifiers = [], set(), set()
    for value in values:
        if not isinstance(value, dict) or not {"index", "id"} <= set(value) or set(value) - {"index", "id", "label", "parameters", "inputs", "outputs"}:
            raise ValueError("each entry needs index and id; unknown entry fields are not supported")
        index = value["index"]
        if type(index) is not int or not 0 <= index <= MAX_INDEX or index in indices:
            raise ValueError("entry indices must be unique nonnegative 63-bit integers")
        identifier = _text(value["id"], "entry id", 256)
        if identifier in identifiers:
            raise ValueError("scientific entry IDs must be unique")
        label = _text(value.get("label", identifier), "entry label", 512)
        params = value.get("parameters", {})
        if not isinstance(params, dict) or len(params) > 64:
            raise ValueError("parameters must contain at most 64 named scalar values")
        for key, item in params.items():
            _text(key, "parameter name", 128)
            if item is not None and type(item) not in (str, bool, int, float):
                raise ValueError("parameter values must be JSON scalars")
            if isinstance(item, str):
                _text(item, "parameter value", 1024, empty=True)
            elif type(item) in (int, float) and (abs(item) > 1e308 or not math.isfinite(item)):
                raise ValueError("parameter values must be finite bounded numbers")
        paths = []
        for key in ("inputs", "outputs"):
            items = value.get(key, [])
            if not isinstance(items, list) or len(items) > 32:
                raise ValueError(f"{key} must be a list of at most 32 declared paths")
            paths.append(tuple(_text(item, key) for item in items))
        entries.append(Entry(index, identifier, label, MappingProxyType(dict(params)), paths[0], paths[1],
                             (str(index) + " " + identifier + " " + label + " " + json.dumps(params, ensure_ascii=False)).casefold()))
        indices.add(index)
        identifiers.add(identifier)
    entries.sort(key=lambda entry: entry.index)
    normalized = {"schema": SCHEMA, "cluster": cluster, "array_id": array_id, "entries": [entry.document() for entry in entries]}
    revision = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    return Manifest(cluster, array_id, tuple(entries), revision, MappingProxyType({entry.index: entry for entry in entries}))


def read(path, files):
    """Read one regular local/remote file and reject concurrent replacement."""
    _text(path, "manifest path")
    before = files.snapshot_stat(path)
    size = before.get("size")
    if type(size) is not int or not 0 < size <= MAX_BYTES:
        raise ValueError("array manifest must be a nonempty regular file of at most 4 MiB")
    from .remote import LocalFiles
    if type(files) is LocalFiles:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != before.get("ident"):
                raise ValueError("array manifest changed or is not a regular file")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(size + 1)
        finally:
            os.close(fd)
    else:
        data = files.read(path, 0, size + 1)
    after = files.snapshot_stat(path)
    if before != after or len(data) != size:
        raise ValueError("array manifest changed during the read; retry when its writer has finished")
    return parse(data), before


def verify(path, files, manifest):
    fresh, stamp = read(path, files)
    if fresh.revision != manifest.revision:
        raise ValueError("array manifest changed; inspect and explicitly reload its revision before preparing a retry")
    return stamp


def retry_metadata(manifest, path, specification):
    """Keep original identities, without enumerating a possibly huge task range."""
    from .arrays import parse_range
    ranges = parse_range(specification)
    retained = [entry for entry in manifest.entries if any(r["start"] <= entry.index <= r["end"] and
                (entry.index - r["start"]) % r["step"] == 0 for r in ranges)]
    return {"schema": SCHEMA, "source": path, "revision": manifest.revision,
            "cluster": manifest.cluster, "array_id": manifest.array_id,
            "entries": [entry.document() for entry in retained],
            "unmapped_count": sum(r["count"] for r in ranges) - len(retained)}


def validate_retry(plan, files):
    """Verify review metadata and the still-pinned source immediately pre-submit."""
    retry = plan.get("array_retry")
    if not isinstance(retry, dict) or "manifest" not in retry:
        return
    metadata = retry["manifest"]
    if not isinstance(metadata, dict):
        raise ValueError("array retry has malformed scientific mapping metadata")
    source = metadata.get("source")
    fresh, _ = read(source, files)
    if fresh.revision != metadata.get("revision"):
        raise ValueError("array manifest changed after retry preparation; reload and prepare a new review")
    specification = retry.get("indices")
    if (not fresh.matches(retry.get("array"), retry.get("cluster", "")) or
            specification != plan.get("resources", {}).get("array") or
            metadata != retry_metadata(fresh, source, specification)):
        raise ValueError("array retry scientific mapping does not match its source or Slurm indices")
