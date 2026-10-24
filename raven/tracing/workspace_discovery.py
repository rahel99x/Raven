"""Bounded metadata discovery; the workspace index is the sole journal reader.

Existing run-source, scheduler-association and process-description owners are
injected. Reading metadata never proves journal integrity or scheduler control.
"""

from __future__ import annotations

from pathlib import Path

MAX_SOURCES = 512
MAX_GAPS = 64
MAX_ERRORS = 64
HELPER_ROLES = frozenset(
    {
        "helper",
        "dataloader-worker",
        "pool-worker",
        "multiprocessing-helper",
        "resource-tracker",
    }
)


def _bounded_paths(rows):
    if not isinstance(rows, (tuple, list)) or len(rows) > MAX_SOURCES:
        raise ValueError(
            "Run exceeds the 512-source workspace discovery budget; select a smaller source set"
        )
    entries = {}
    for value in rows:
        entry = value if isinstance(value, dict) else {"path": value, "expected": {}}
        path = str(entry.get("path", ""))
        expected = entry.get("expected", {})
        if (
            not path
            or len(path) > 4096
            or not path.isprintable()
            or not isinstance(expected, dict)
            or set(expected) - {"job_id", "attempt", "stream"}
        ):
            raise ValueError("Invalid bounded workspace source registration")
        if any(
            not isinstance(value, str)
            or not 0 < len(value) <= 256
            or not value.isprintable()
            for value in expected.values()
        ):
            raise ValueError("Invalid bounded workspace source identity constraint")
        selected_path = Path(path).absolute()
        if selected_path.is_dir():
            selected_path /= "manifest.json"
        path = str(selected_path)
        if len(path) > 4096:
            raise ValueError("Workspace source path exceeds its absolute path budget")
        if path in entries and entries[path]["expected"] != expected:
            if entries[path]["expected"] and expected:
                raise ValueError("Conflicting workspace source registrations")
            expected = expected or entries[path]["expected"]
        entries[path] = {"path": path, "expected": expected}
    return list(entries.values())


def _summary(row):
    from ..job_logs.text import display_text

    if not isinstance(row, dict):
        raise ValueError("Invalid trace process metadata")
    result = {}
    for key in (
        "rank",
        "pid",
        "parent_pid",
        "local_rank",
        "world_size",
        "events",
        "checked",
    ):
        if key in row:
            value = row[key]
            result[key] = value if type(value) is int and 0 <= value < 1 << 63 else None
    for key in ("role", "launcher", "interpreter"):
        if key in row and isinstance(row[key], str):
            result[key] = display_text(row[key][:256])
    if "helpers_default" in row:
        result["helpers_default"] = row["helpers_default"] is True
    for key, maximum in (
        ("path", 4096),
        ("status", 128),
        ("coverage", 2048),
        ("reason", 2048),
    ):
        value = row.get(key, "")
        result[key] = display_text(
            (value if isinstance(value, str) else str(value))[:maximum]
        )
    return result


def read(
    context,
    explicit,
    index,
    event,
    source_cache=None,
    process_cache=None,
    *,
    launch_sources,
    matches,
    process,
):
    from .protocol import discover_trace_candidates, read_json, validate_identity

    if event.is_set():
        return None
    if context.get("remote"):
        raise ValueError(
            "Open Raven on the trace host to inspect its journal; remote views never substitute local files."
        )
    standalone = bool(context.get("standalone"))
    discovered = source_cache is None
    gaps, processes, errors, omitted_errors, omitted_gaps = [], [], [], 0, 0
    if standalone:
        entries = _bounded_paths([context["path"]])
    elif explicit:
        entries = _bounded_paths([explicit])
    elif source_cache is not None:
        entries = _bounded_paths(source_cache)
        processes = [
            _summary(row) for row in (process_cache or [])[: MAX_SOURCES + MAX_GAPS]
        ]
    elif context.get("root") or context.get("run_root"):
        paths, observed_gaps = launch_sources(context)
        entries = _bounded_paths(paths)
        gaps = [_summary(row) for row in observed_gaps[:MAX_GAPS]]
        omitted_gaps = max(0, len(observed_gaps) - MAX_GAPS)
        if context.get("root"):
            try:
                registered = discover_trace_candidates(
                    context["root"],
                    context["job_id"],
                    context.get("discovery_attempt"),
                    max_sources=MAX_SOURCES,
                    cancel=event,
                )
            except InterruptedError:
                if event.is_set():
                    return None
                raise
            # Bound each producer and their merged distinct set before opening
            # source manifests. Duplicate launch/project references stay one.
            merged = {entry["path"]: entry for entry in entries}
            for entry in _bounded_paths(registered):
                merged[entry["path"]] = entry
            entries = _bounded_paths(list(merged.values()))
    else:
        entries = []

    def metadata(entry):
        manifest = read_json(entry["path"], limit=65536)
        if not isinstance(manifest, dict) or manifest.get("schema") != "raven.trace/v1":
            raise ValueError("Expected a trace manifest")
        identity = validate_identity(manifest.get("identity"))
        if any(identity.get(key) != value for key, value in entry["expected"].items()):
            raise ValueError("Trace registration identity does not match its manifest")
        foreign_cluster = (
            not explicit
            and context.get("cluster")
            and identity.get("cluster") != context["cluster"]
        )
        associated = (
            False if standalone or foreign_cluster else bool(matches(identity, context))
        )
        return (
            manifest,
            identity,
            _summary(process(manifest, entry["path"])),
            associated,
        )

    def failed(path, exc):
        from ..job_logs.text import display_text

        nonlocal omitted_errors
        reason = display_text(str(exc)[:2048])
        if len(errors) < MAX_ERRORS:
            errors.append({"path": path, "read_error": reason})
        else:
            omitted_errors += 1
        return {
            "path": path,
            "status": "unavailable",
            "reason": reason,
            "events": None,
            "coverage": "metadata unavailable",
        }

    sources = []
    if discovered and not explicit and not standalone:
        for entry in entries:
            if event.is_set():
                return None
            try:
                manifest, identity, row, associated = metadata(entry)
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                processes.append(failed(entry["path"], exc))
                continue
            if associated:
                sources.append(entry)
                processes.append(row)
        processes.extend(gaps)
    else:
        sources = entries

    def result(snapshot=None, path="", selected=0, associated=False):
        return dict(
            sources=[row["path"] for row in sources],
            processes=processes,
            snapshot=snapshot,
            reader=None,
            path=path,
            verified=False,
            association_verified=associated,
            discovered=discovered,
            index=selected,
            association="declared recording; scheduler association not checked"
            if standalone
            else "metadata association only; journal integrity verification pending",
            discovery_errors=errors,
            discovery_errors_omitted=omitted_errors,
            discovery_gaps_omitted=omitted_gaps,
            workspace_catalog_only=True,
        )

    while sources:
        if event.is_set():
            return None
        selected = max(0, min(index, len(sources) - 1))
        entry = sources[selected]
        try:
            manifest, identity, current, associated = metadata(entry)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            if explicit or standalone:
                raise
            processes = [row for row in processes if row.get("path") != entry["path"]]
            processes.append(failed(entry["path"], exc))
            sources.pop(selected)
            continue
        if not associated and not (explicit or standalone):
            sources.pop(selected)
            continue
        # Replace cached role metadata before selecting the default visible
        # process. A helper role changing at this path must not select itself
        # repeatedly through an obsolete non-helper cache row.
        processes = [
            current if row.get("path") == entry["path"] else row for row in processes
        ]
        if not any(row.get("path") == entry["path"] for row in processes):
            processes.append(current)
        show_helpers = context.get("show_helpers")
        if show_helpers is None:
            show_helpers = any(row.get("helpers_default") is True for row in processes)
        if (
            not explicit
            and not standalone
            and not show_helpers
            and current.get("role") in HELPER_ROLES
        ):
            source_paths = {item["path"] for item in sources}
            visible = next(
                (
                    row
                    for row in processes
                    if row.get("path") in source_paths
                    and row.get("role") not in HELPER_ROLES
                ),
                None,
            )
            if visible is not None:
                index = next(
                    position
                    for position, row in enumerate(sources)
                    if row["path"] == visible["path"]
                )
                continue
        committed = manifest.get("committed_seq")
        committed = (
            committed if type(committed) is int and 0 <= committed < 1 << 63 else None
        )
        snapshot = dict(
            schema="raven.trace-snapshot/v1",
            identity=identity,
            manifest=manifest,
            events=[],
            stacks=[],
            status="WAITING",
            read_seq=0,
            committed_seq=committed,
            caught_up=False,
            complete=False,
            covered=manifest.get("coverage", {}),
            rows=[
                "Source metadata loaded; journal integrity verification is pending in the workspace index"
            ],
            warnings=[],
            process=current,
            sha256="",
            evicted=0,
            retained_bytes=0,
            window_start=1,
            window_end=0,
            workspace_catalog_only=True,
        )
        if event.is_set():
            return None
        if context.get("binding") and context.get("run_root"):
            import os
            from ..projects import read_bound_json

            read_bound_json(
                context["binding"],
                os.path.join(context["run_root"], "run.json"),
                max_bytes=65536,
            )
        return result(snapshot, entry["path"], selected, associated)
    return result()
