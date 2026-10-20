"""Governed backend composition shared by entrypoints; no UI construction."""

from __future__ import annotations
from ..configuration.policy import Resolved, to_raven_config


def configure_placement(resolved, manager):
    """Construct one governed collection owner; no sampler or history is loaded."""
    data = resolved.data if isinstance(resolved, Resolved) else resolved
    from ..slurm import Backend
    from ..remote import SshBackend
    from ..platform_session import wrap_backend
    from ..collection.ledger import make_placement_verifier

    connection = data["connection"]
    if connection["transport"] == "ssh":
        backend = SshBackend(
            connection["host"], user=connection["user"], opts=connection["ssh_options"]
        )
    else:
        backend = Backend()
    from ..connection_scope import configured_collection_scope

    scope = configured_collection_scope(data)
    governed = wrap_backend(
        backend, to_raven_config(resolved), scope, data["storage"]["state_dir"]
    )
    manager.collection_backend = governed
    if data["site"]["kind"] != "desktop":
        manager.set_placement_verifier(make_placement_verifier(governed))
    return manager
