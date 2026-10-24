"""One registration parser for legacy resolution and bounded metadata discovery.

File loading and source identity validation are injected by the protocol owner.
Candidate discovery validates registration paths/run indexes, never journals or
individual source manifests. Its expected fields remain descriptive constraints
until the normal source verifier checks the selected journal.
"""

from __future__ import annotations

from pathlib import Path

SCHEMA = "raven.trace/v1"
MAX_SOURCES = 512
MAX_METADATA_BYTES = 64 << 20


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Trace discovery cancelled")


class _Loader:
    def __init__(self, read_json, *, cancel=None, budget=None):
        self.read_json, self.cancel, self.remaining = read_json, cancel, budget

    def read(self, path, limit=1 << 20):
        _cancel(self.cancel)
        if self.remaining is not None:
            # Reserving the complete permitted read bounds actual input bytes
            # even if a file is replaced or enlarged between metadata reads.
            if limit > self.remaining:
                raise ValueError(
                    "Trace registration discovery exceeded its cumulative metadata read reservation budget"
                )
            self.remaining -= limit
        return self.read_json(path, limit=limit)


def _relative(value, *, source=False):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or not value.isprintable()
    ):
        raise ValueError("Invalid trace registration path")
    path = Path(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError(
            "Run trace registration escapes its run"
            if source
            else "Trace registration escapes the project"
        )
    return path


def _expected(value, name):
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 256
        or not value.isprintable()
    ):
        raise ValueError("Invalid bounded trace registration " + name)
    return value


def _document(root, load):
    index = root / ".raven" / "traces" / "index.json"
    if index.exists():
        doc = load.read(index, 4 << 20)
    elif (root / "trace/index.json").exists():
        manifest = load.read(root / "run.json")
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != "raven.run/v1"
            or manifest.get("run_id") != root.name
        ):
            raise ValueError("Trace discovery requires a verified retained run")
        doc = {
            "schema": "raven.trace-index/v1",
            "records": [
                {
                    "schema": "raven.trace-run/v1",
                    "job_id": str(manifest.get("job_id", "local")),
                    "attempt": root.name,
                    "path": "trace/index.json",
                }
            ],
        }
    else:
        return {"schema": "raven.trace-index/v1", "records": []}
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != "raven.trace-index/v1"
        or not isinstance(doc.get("records"), list)
        or len(doc["records"]) > 4096
    ):
        raise ValueError("Invalid bounded trace index")
    return doc


def _entries(project_root, job_id, attempt, load, maximum=None):
    _cancel(load.cancel)
    root = Path(project_root).absolute()
    doc = _document(root, load)
    count, registrations = 0, 0
    for record in doc["records"]:
        _cancel(load.cancel)
        if (
            not isinstance(record, dict)
            or record.get("job_id") != str(job_id)
            or attempt is not None
            and record.get("attempt") != str(attempt)
        ):
            continue
        registrations += 1
        if maximum is not None and registrations > maximum:
            raise ValueError(
                "Trace discovery exceeds its bounded relevant registration budget"
            )
        path = root / _relative(record.get("path"))
        if record.get("schema") == "raven.trace-run/v1":
            try:
                manifest = load.read(path)
            except FileNotFoundError:
                continue
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema") != "raven.trace-index/v1"
                or not isinstance(manifest.get("sources"), list)
                or len(manifest["sources"]) > 512
            ):
                raise ValueError("Invalid registered run trace index")
            entries = [
                source
                for source in manifest["sources"]
                if isinstance(source, dict) and source.get("schema") == SCHEMA
            ]
            if maximum is not None and count + len(entries) > maximum:
                raise ValueError(
                    f"Run exceeds the {maximum}-source workspace discovery budget; select a smaller registered source set"
                )
            run = path.parent.parent
            for source in entries:
                _cancel(load.cancel)
                source_path = run / _relative(source.get("path"), source=True)
                count += 1
                yield {
                    "path": str(source_path),
                    "expected": {
                        "job_id": str(job_id),
                        "attempt": _expected(record.get("attempt"), "attempt")
                        if maximum is not None
                        else record.get("attempt"),
                        "stream": _expected(source.get("stream"), "stream")
                        if maximum is not None
                        else source.get("stream"),
                    },
                    "run": True,
                }
        else:
            count += 1
            if maximum is not None and count > maximum:
                raise ValueError(
                    f"Run exceeds the {maximum}-source workspace discovery budget; select a smaller registered source set"
                )
            expected = {
                "job_id": str(job_id),
                "stream": _expected(record.get("stream"), "stream")
                if maximum is not None
                else record.get("stream"),
            }
            if attempt is not None:
                expected["attempt"] = str(attempt)
            yield {"path": str(path), "expected": expected, "run": False}


def discover_trace(project_root, job_id, attempt=None, *, read_json, validate_identity):
    """Legacy eager validation through the shared registration parser."""
    load, result = _Loader(read_json), []
    for entry in _entries(project_root, job_id, attempt, load):
        try:
            manifest = load.read(entry["path"])
        except FileNotFoundError:
            continue
        if entry["run"] and (
            not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
        ):
            raise ValueError("Registered run source is not a trace manifest")
        identity = validate_identity(
            manifest.get("identity") if isinstance(manifest, dict) else None
        )
        expected = entry["expected"]
        if any(
            identity.get(key) != value
            for key, value in expected.items()
            if key != "stream"
        ):
            raise ValueError(
                "Run trace identity does not match its manifest"
                if entry["run"]
                else "Trace registration identity does not match its manifest"
            )
        if identity.get("stream") != expected["stream"]:
            raise ValueError(
                "Run trace identity does not match its manifest"
                if entry["run"]
                else "Trace registration stream does not match its manifest"
            )
        result.append(entry["path"])
    return result


def discover_trace_candidates(
    project_root,
    job_id,
    attempt=None,
    *,
    read_json,
    validate_identity=None,
    max_sources=MAX_SOURCES,
    cancel=None,
    max_metadata_bytes=MAX_METADATA_BYTES,
):
    """Bound registrations before loading any individual source manifest.

    The cumulative budget conservatively reserves each loader's maximum input
    bytes. It therefore bounds actual input rather than estimating file sizes
    through a racy stat or retaining all decoded manifests in memory.
    """
    if type(max_sources) is not int or not 1 <= max_sources <= MAX_SOURCES:
        raise ValueError("Trace discovery source budget must be 1–512")
    if (
        type(max_metadata_bytes) is not int
        or not 1 <= max_metadata_bytes <= MAX_METADATA_BYTES
    ):
        raise ValueError("Trace discovery metadata budget must be 1 byte–64 MiB")
    load = _Loader(read_json, cancel=cancel, budget=max_metadata_bytes)
    return [
        {"path": entry["path"], "expected": entry["expected"]}
        for entry in _entries(project_root, job_id, attempt, load, maximum=max_sources)
    ]
