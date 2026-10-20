"""Configured scheduler connection identity; no file or scheduler probes."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time

from .user_identity import current_user


def selection_digest(environ=None):
    """Fingerprint environment selectors, not unobserved configuration bytes."""
    environ = os.environ if environ is None else environ
    selected = {
        key: environ.get(key, "") for key in ("SLURM_CONF", "SLURM_CONF_SERVER")
    }
    if any(
        not isinstance(value, str) or len(value) > 4096 or "\0" in value
        for value in selected.values()
    ):
        raise ValueError("Slurm configuration selection must be bounded text")
    return hashlib.sha256(
        json.dumps(
            selected, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def configured_collection_scope(data, *, environ=None):
    """Use the same explicit identity for collection and retained launches."""
    connection = data["connection"]
    remote = connection["transport"] == "ssh"
    options = (
        hashlib.sha256(
            json.dumps(
                connection["ssh_options"], ensure_ascii=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if remote
        else ""
    )
    return {
        "backend": "ssh" if remote else "local",
        "host": connection["host"] if remote else socket.gethostname(),
        "profile": data["profile"],
        "uid": os.getuid(),
        "user": current_user(),
        "ssh_user": connection["user"] if remote else "",
        "ssh_options_sha256": options,
        "scheduler_selection_sha256": "remote-login-environment"
        if remote
        else selection_digest(environ),
    }


def forecast_scope(backend, cfg, user: str):
    """Separate queue evidence by the actual connection, profile, and owner."""
    from .record import RecordingBackend, ReplayBackend
    from .remote import SshBackend
    from .slurm import FakeBackend
    from .platform_session import GovernedBackend

    while isinstance(backend, (RecordingBackend, GovernedBackend)):
        backend = backend.inner
    try:
        uid = os.getuid()
    except (AttributeError, OSError):
        return None
    if isinstance(backend, SshBackend):
        host, kind, ssh_user = backend.host, "ssh", backend.user
        options = hashlib.sha256(
            json.dumps(backend.opts, ensure_ascii=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    else:
        host, kind, ssh_user, options = socket.gethostname(), "local", "", ""
    if not host:
        return None
    scope = dict(
        backend=kind,
        host=host,
        profile=cfg.profile_name,
        uid=uid,
        user=user,
        ssh_user=ssh_user,
        ssh_options_sha256=options,
    )
    if cfg.get("raven", {}).get("managed") and isinstance(
        backend, (FakeBackend, ReplayBackend)
    ):
        # Simulations must never publish into the real connection's cache.
        # Each independent fixture/replay owns its clock and mutable answers.
        if not hasattr(backend, "_raven_fixture_identity"):
            backend._raven_fixture_identity = (
                f"{os.getpid()}:{id(backend)}:{time.monotonic_ns()}"
            )
        scope.update(
            backend="demo" if isinstance(backend, FakeBackend) else "replay",
            fixture=backend._raven_fixture_identity,
        )
    return scope


from .execution.scope import validate_scope


def execution_scope(app):
    """Capture a bounded connection identity using only already-known values."""
    slurm = getattr(getattr(app, "actions", None), "slurm", None)
    backend = getattr(slurm, "b", None)
    if backend is None:
        raise ValueError(
            "execution connection scope is unknown; prepare a fresh review in the original connection"
        )
    seen = set()
    for _ in range(8):
        if not hasattr(backend, "inner"):
            break
        if id(backend) in seen:
            raise ValueError("execution backend scope is unknown")
        seen.add(id(backend))
        backend = backend.inner
    else:
        raise ValueError("execution backend scope exceeds its wrapper limit")
    cfg = getattr(app, "cfg", None)
    getter = getattr(cfg, "get", lambda key, default=None: default)
    user = getattr(slurm, "user", None) or getattr(app, "user", "") or current_user()
    configured_host = getter("host", "")
    actual_host = getattr(backend, "host", None) or socket.gethostname()
    cluster = (
        getattr(slurm, "cluster_name", None)
        or getter("cluster_name", "")
        or getter("site.cluster_id", "")
        or os.environ.get("SLURM_CLUSTER_NAME", "")
    )
    try:
        uid = os.getuid()
    except (AttributeError, OSError) as exc:
        raise ValueError("execution owner scope is unknown") from exc
    return validate_scope(
        {
            "schema": "raven.execution-scope/v1",
            "backend": type(backend).__module__ + "." + type(backend).__qualname__,
            "connection_host": actual_host,
            "configured_host": configured_host,
            "profile": getattr(app, "profile_name", getattr(cfg, "profile_name", "")),
            "user": user,
            "uid": uid,
            "cluster": cluster,
        }
    )
