"""Explicit project-following ports; no scheduler ownership or HTTP paths."""

from __future__ import annotations

from pathlib import Path
from itertools import islice


def source_paths(values):
    if isinstance(values, (str, bytes)):
        raise ValueError("Choose a bounded collection of trace source paths")
    selected = list(islice(iter(values), 513))
    if not selected or len(selected) > 512:
        raise ValueError("Choose 1–512 exact-attempt trace sources")
    paths = []
    for value in selected:
        path = Path(value).absolute()
        if path.is_dir():
            path /= "manifest.json"
        value = str(path)
        if not value or len(value) > 4096 or not value.isprintable():
            raise ValueError("Trace source paths must be bounded printable local paths")
        paths.append(value)
    return tuple(dict.fromkeys(paths))


class Cancellation:
    def __init__(self, *events):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self):
        return any(event.is_set() for event in self.events)


def verified_identity(catalog):
    declared = catalog.get("identity") or {}
    identity = {
        key: declared[key]
        for key in ("job_id", "attempt", "cluster", "run_id")
        if key in declared
    }
    if not {"job_id", "attempt", "cluster"} <= set(identity):
        raise ValueError(
            "Following a project requires one verified initial job/attempt/cluster identity"
        )
    if not any(
        source.get("validated") is True
        and source.get("read_seq", 0) > 0
        and all(
            source.get("identity", {}).get(key) == value
            for key, value in identity.items()
        )
        for source in catalog.get("sources", ())
    ):
        raise ValueError(
            "Following a project needs a verified committed initial source prefix; allow more initial indexing time"
        )
    return identity


def project_discovery(project_root, identity):
    """Return a trusted local discovery callback for one exact retained attempt.

    The identity is derived from the verified initial catalog. Matching metadata
    never authorizes Slurm controls, and the index remains the journal verifier.
    """
    root = Path(project_root).absolute()
    if not root.is_dir():
        raise ValueError(
            "The followed project root must be an existing local directory"
        )
    expected = dict(identity)
    if (
        not {"job_id", "attempt", "cluster"} <= set(expected)
        or set(expected) - {"job_id", "attempt", "cluster", "run_id"}
        or any(
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or not value.isprintable()
            for value in expected.values()
        )
    ):
        raise ValueError("Project following requires an exact common source identity")
    context = dict(
        root=str(root),
        run_root="",
        remote=False,
        show_helpers=True,
        job_id=expected["job_id"],
        cluster=expected["cluster"],
        submit="",
        start="",
        run_id="",
        discovery_attempt=expected["attempt"],
    )

    def describe(manifest, path):
        # This descriptor is only for bounded metadata presentation. The same
        # normal index reader validates all authoritative process fields.
        source = manifest["identity"]
        result = {
            key: source[key]
            for key in (
                "pid",
                "parent_pid",
                "role",
                "launcher",
                "rank",
                "local_rank",
                "world_size",
                "interpreter",
            )
            if key in source
        }
        coverage = manifest.get("coverage") or {}
        result.update(
            path=path,
            events=manifest.get("committed_seq"),
            status="declared",
            coverage=coverage.get("scope", "undeclared"),
            helpers_default=True,
        )
        return result

    def discover(cancel):
        from .workspace_discovery import read

        if (
            not (root / ".raven/traces/index.json").exists()
            and not (root / "trace/index.json").exists()
        ):
            raise FileNotFoundError(
                "Project trace registration is not yet available; retaining known verified sources"
            )
        return read(
            context,
            "",
            0,
            cancel,
            launch_sources=lambda context: ([], []),
            matches=lambda actual, context: all(
                actual.get(key) == value for key, value in expected.items()
            ),
            process=describe,
        )

    return discover


def deferred_project_discovery(project_root, catalog, *, on_identity=None):
    """Wait for a verified source prefix while the known index keeps updating."""
    root = Path(project_root).absolute()
    if not root.is_dir():
        raise ValueError(
            "The followed project root must be an existing local directory"
        )
    active = None

    def discover(cancel):
        nonlocal active
        if cancel.is_set():
            raise InterruptedError("Trace source discovery cancelled")
        if active is None:
            identity = verified_identity(catalog())
            active = project_discovery(root, identity)
            if on_identity is not None:
                on_identity(identity)
        return active(cancel)

    return discover
