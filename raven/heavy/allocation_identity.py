"""Pure allocation connection and receipt identities, independent of admission."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def connection_binding(plan):
    fields = {
        key: plan.get(key)
        for key in (
            "site_kind",
            "target",
            "transport",
            "ssh_target",
            "ssh_options",
            "owner_uid",
        )
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def receipt_path(directory, plan):
    key = receipt_key(plan)
    root = Path(
        plan.get("receipt_directory") or Path(directory).expanduser() / "allocations"
    ).expanduser()
    return root / (key + ".json")


def receipt_key(plan):
    """Controller discovery must not hide an earlier request's receipt."""
    fields = {
        key: plan.get(key) for key in ("site_kind", "target", "transport", "owner_uid")
    }
    if plan.get("transport") == "ssh":
        fields["ssh_target"] = plan.get("ssh_target")
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:32]


def recovery_plan(config):
    """Connection identity only: recovery never requests Heavy entitlement."""
    from .allocation_adapters import config_data, _ssh_options

    data = config_data(config)
    site, connection = data.get("site", {}), data.get("connection", {})
    kind = site.get("kind", "slurm")
    transport = connection.get("transport", "none" if kind == "desktop" else "local")
    host = (
        connection.get("host", "")
        if transport == "ssh"
        else "local desktop"
        if kind == "desktop"
        else "local Slurm gateway"
    )
    user = connection.get("user", "")
    return {
        "site_kind": kind,
        "cluster_id": site.get("cluster_id", ""),
        "target": host,
        "receipt_directory": data.get("storage", {}).get("allocation_receipts_dir", ""),
        "transport": transport,
        "ssh_target": (user + "@" if user else "") + host if transport == "ssh" else "",
        "ssh_options": _ssh_options(connection.get("ssh_options", []))
        if transport == "ssh"
        else [],
        "owner_uid": os.getuid(),
    }
