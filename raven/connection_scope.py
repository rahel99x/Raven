"""Configured scheduler connection identity; no file or scheduler probes."""

from __future__ import annotations

import hashlib
import json
import os
import socket

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
